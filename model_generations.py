"""Immutable model generations and atomic hot-activation pointers.

Legacy artifact directories remain the bootstrap/fallback source.  A newly
trained candidate is copied into a content-addressed, read-only generation,
loaded and smoke-tested with the live runtime code, and only then selected by
one atomically replaced pointer.  Runtime adapters keep their loaded bundle in
one Python reference, so requests that already captured the previous bundle
finish on it while later requests see the new generation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import app_config
import portable_os

try:
    import fcntl
except ImportError:  # Windows
    import portable_fcntl as fcntl


SCHEMA_VERSION = 1
GENERATION_MANIFEST = "generation_manifest.json"
ACTIVE_POINTER = "active.json"
FAILURE_RECORD = "last_activation_failure.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_JSON_BYTES = 2 * 1024 * 1024

MODEL_FILES: dict[str, tuple[str, ...]] = {
    "almaz": (
        "csfloat_price_model.joblib",
        "csfloat_price_model_metrics.json",
    ),
    "igor": (
        "price_model_point.cbm",
        "price_model_q15.cbm",
        "price_model_q85.cbm",
        "feature_metadata.pkl",
        "igor_metrics.json",
    ),
    "nova": (
        "nova_price_model.pt",
        "nova_metadata.json",
        "nova_metrics.json",
    ),
    "vega": (
        "vega_price_model.pt",
        "vega_metadata.json",
        "vega_metrics.json",
    ),
    "knives": (
        "knife_price_model.joblib",
        "knife_price_model_metrics.json",
    ),
    "gloves": (
        "glove_price_model.joblib",
        "glove_price_model_metrics.json",
    ),
}

LEGACY_DIRECTORIES = {
    "almaz": Path("models"),
    "igor": Path("IGORMODEL/artifacts"),
    "nova": Path("NEURALMODEL/artifacts"),
    "vega": Path("VEGAMODEL/artifacts"),
    "knives": Path("KNIFEMODEL/artifacts"),
    "gloves": Path("GLOVEMODEL/artifacts"),
}

STAGING_ROOTS = {
    "almaz": Path("models/staging/training_jobs"),
    "igor": Path("IGORMODEL/staging/training_jobs"),
    "nova": Path("NEURALMODEL/staging/training_jobs"),
    "vega": Path("VEGAMODEL/staging/training_jobs"),
    "knives": Path("KNIFEMODEL/staging/training_jobs"),
    "gloves": Path("GLOVEMODEL/staging/training_jobs"),
}

MODEL_ITEM_KINDS = {
    "almaz": "weapon_skin",
    "igor": "weapon_skin",
    "nova": "weapon_skin",
    "vega": "weapon_skin",
    "knives": "knife",
    "gloves": "glove",
}

# Kept as a narrow semantic set for callers that need to distinguish the two
# dedicated non-weapon runtimes.  Generation identity itself is fixed for all
# models through MODEL_ITEM_KINDS.
ISOLATED_ITEM_KINDS = {
    model: MODEL_ITEM_KINDS[model] for model in ("knives", "gloves")
}

_THREAD_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class GenerationError(RuntimeError):
    """A generation cannot be trusted, loaded, or selected safely."""


class GenerationCancelled(GenerationError):
    """Activation was cancelled before it became visible, or was rolled back."""


class GenerationActivationError(GenerationError):
    """Activation failed, with an optional rollback failure for public status."""

    def __init__(
        self,
        message: str,
        *,
        rollback_error: str | None = None,
        retention_error: str | None = None,
    ):
        super().__init__(message)
        self.rollback_error = rollback_error
        self.retention_error = retention_error


@dataclass(frozen=True)
class LoadedGeneration:
    key: tuple[Any, ...]
    bundle: Any
    source_dir: Path
    generation_id: str | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project(project_dir: str | Path | None = None) -> Path:
    project = Path(project_dir or app_config.PROJECT_DIR).expanduser().resolve()
    if project.is_symlink() or not project.is_dir():
        raise GenerationError(f"project directory is missing or unsafe: {project}")
    return project


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _root(project: Path) -> Path:
    return project / ".model_generations"


def _model_root(project: Path, model: str) -> Path:
    _require_model(model)
    return _root(project) / model


def _versions_root(project: Path, model: str) -> Path:
    return _model_root(project, model) / "versions"


def _require_model(model: str) -> None:
    if model not in MODEL_FILES:
        raise ValueError(f"unknown model: {model!r}")


def _fsync_directory(path: Path) -> None:
    portable_os.fsync_directory(path)


def _secure_directory(path: Path, project: Path) -> None:
    try:
        relative = path.relative_to(project)
    except ValueError as exc:
        raise GenerationError("generation path escapes the project") from exc
    cursor = project
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise GenerationError(f"refusing symlink generation directory: {cursor}")
        created = False
        try:
            cursor.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            if not cursor.is_dir():
                raise GenerationError(f"generation path is not a directory: {cursor}")
        except OSError as exc:
            raise GenerationError(f"cannot create generation directory: {cursor}") from exc
        try:
            os.chmod(cursor, 0o700)
        except OSError as exc:
            raise GenerationError(f"cannot secure generation directory: {cursor}") from exc
        if created:
            _fsync_directory(cursor.parent)


def _assert_no_symlink_components(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise GenerationError(f"path escapes trusted root: {path}") from exc
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise GenerationError(f"refusing symlink path component: {cursor}")


def _require_private_directory(path: Path, project: Path) -> None:
    """Require every managed component below project to be owned and non-writable."""

    _assert_no_symlink_components(path, project)
    relative = path.relative_to(project)
    cursor = project
    for part in relative.parts:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except OSError as exc:
            raise GenerationError(f"managed generation directory is missing: {cursor}") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or not portable_os.owned_by_current_user(info)
            or portable_os.has_unsafe_group_or_world_write(info)
        ):
            raise GenerationError(f"managed generation directory is unsafe: {cursor}")


def _read_json(path: Path, *, optional: bool = False) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        if optional:
            return None
        raise GenerationError(f"required generation document is missing: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or not portable_os.owned_by_current_user(info)
            or portable_os.has_unsafe_group_or_world_write(info)
            or info.st_size > MAX_JSON_BYTES
        ):
            raise GenerationError(f"generation document is unsafe: {path.name}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            value = json.load(handle)
    except GenerationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GenerationError(f"generation document is unreadable: {path.name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise GenerationError(f"generation document must be an object: {path.name}")
    return value


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except (OSError, TypeError, ValueError) as exc:
        raise GenerationError(f"cannot atomically write {path.name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    # Explicit binary mode is required for descriptors opened through the
    # Windows CRT; otherwise arbitrary PyTorch bytes can be translated before
    # hashing and produce a false SHA-256 mismatch.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor: int | None = None
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise GenerationError(f"artifact is not a regular file: {path.name}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except GenerationError:
        raise
    except OSError as exc:
        raise GenerationError(f"cannot hash artifact: {path.name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return digest.hexdigest()


def _bundle_sha256(records: dict[str, Any]) -> str:
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _thread_lock(project: Path, model: str) -> threading.RLock:
    key = (str(project), model)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


class _GenerationLock:
    def __init__(self, project: Path, model: str):
        self.project = project
        self.model = model
        self.thread_lock = _thread_lock(project, model)
        self.descriptor: int | None = None

    def __enter__(self):
        if fcntl is None:
            raise GenerationError("generation activation requires POSIX fcntl")
        self.thread_lock.acquire()
        try:
            root = _model_root(self.project, self.model)
            _secure_directory(root, self.project)
            path = root / "activation.lock"
            try:
                self.descriptor = os.open(
                    path,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except OSError as exc:
                raise GenerationError("cannot securely open generation lock") from exc
            info = os.fstat(self.descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise GenerationError("generation activation lock is not a regular file")
            portable_os.fchmod(self.descriptor, 0o600)
            fcntl.flock(self.descriptor, fcntl.LOCK_EX)
            return self
        except BaseException:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None
            self.thread_lock.release()
            raise

    def __exit__(self, _kind, _value, _traceback):
        try:
            if self.descriptor is not None:
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
                os.close(self.descriptor)
                self.descriptor = None
        finally:
            self.thread_lock.release()


def _validate_pointer(value: dict[str, Any], model: str) -> dict[str, Any]:
    if value.get("schema_version") != SCHEMA_VERSION or value.get("model") != model:
        raise GenerationError("active generation pointer has the wrong identity")
    generation_id = value.get("generation_id")
    if not isinstance(generation_id, str) or not SHA256_RE.fullmatch(generation_id):
        raise GenerationError("active generation pointer has an invalid version")
    if value.get("relative_directory") != f"versions/{generation_id}":
        raise GenerationError("active generation pointer has an unsafe directory")
    previous = value.get("previous")
    if not isinstance(previous, dict) or previous.get("kind") not in {"legacy", "generation"}:
        raise GenerationError("active generation pointer has invalid rollback state")
    if previous.get("kind") == "generation":
        previous_id = previous.get("generation_id")
        if not isinstance(previous_id, str) or not SHA256_RE.fullmatch(previous_id):
            raise GenerationError("active pointer has an invalid previous version")
        if previous.get("relative_directory") != f"versions/{previous_id}":
            raise GenerationError("active pointer has an unsafe previous directory")
    return value


def read_active_pointer(
    model: str, *, project_dir: str | Path | None = None
) -> dict[str, Any] | None:
    """Return the verified atomic pointer, or ``None`` for legacy bootstrap."""

    _require_model(model)
    project = _project(project_dir)
    root = _model_root(project, model)
    if not root.exists() and not root.is_symlink():
        return None
    _require_private_directory(root, project)
    pointer = _read_json(root / ACTIVE_POINTER, optional=True)
    return _validate_pointer(pointer, model) if pointer is not None else None


def active_or_legacy_bundle_dir(
    model: str,
    legacy_dir: str | Path | None = None,
    *,
    project_dir: str | Path | None = None,
    verify_files: bool = True,
) -> Path:
    """Resolve the comparison/read-only source used by trainers and status."""

    project = _project(project_dir)
    pointer = read_active_pointer(model, project_dir=project)
    if pointer is None:
        directory = Path(legacy_dir) if legacy_dir is not None else project / LEGACY_DIRECTORIES[model]
        directory = _absolute(directory)
        _assert_no_symlink_components(directory, project)
        return directory
    directory = _model_root(project, model) / str(pointer["relative_directory"])
    verify_generation(
        model,
        directory,
        pointer=pointer,
        project_dir=project,
        verify_files=verify_files,
    )
    return directory


def verify_generation(
    model: str,
    generation_dir: str | Path,
    *,
    pointer: dict[str, Any] | None = None,
    project_dir: str | Path | None = None,
    verify_files: bool = True,
) -> dict[str, Any]:
    """Verify the immutable generation manifest and every artifact SHA-256."""

    _require_model(model)
    project = _project(project_dir)
    directory = _absolute(generation_dir)
    versions = _absolute(_versions_root(project, model))
    _require_private_directory(versions, project)
    _require_private_directory(directory, project)
    if directory.parent != versions or not SHA256_RE.fullmatch(directory.name):
        raise GenerationError("generation directory is not content addressed")
    manifest = _read_json(directory / GENERATION_MANIFEST)
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("model") != model
        or manifest.get("generation_id") != directory.name
    ):
        raise GenerationError("generation manifest has the wrong identity")
    expected_item_kind = MODEL_ITEM_KINDS[model]
    declared_item_kind = manifest.get("item_kind")
    if declared_item_kind is None:
        # Compatibility for immutable generations published before item-kind
        # provenance became part of the manifest.  The fixed model id is the
        # only inference source; newly published generations are strict below.
        manifest = {
            **manifest,
            "item_kind": expected_item_kind,
            "item_kind_compatibility": "inferred_from_fixed_model_id",
        }
    elif declared_item_kind != expected_item_kind:
        raise GenerationError("generation manifest has the wrong item kind")
    if pointer is not None and pointer.get("generation_id") != directory.name:
        raise GenerationError("generation differs from the active pointer")
    records = manifest.get("files")
    if not isinstance(records, dict) or set(records) != set(MODEL_FILES[model]):
        raise GenerationError("generation manifest has an invalid file set")
    if _bundle_sha256(records) != directory.name:
        raise GenerationError("generation id does not match its file manifest")
    for filename in MODEL_FILES[model]:
        record = records.get(filename)
        path = directory / filename
        if not isinstance(record, dict):
            raise GenerationError(f"generation record is missing: {filename}")
        expected_sha = record.get("sha256")
        expected_size = record.get("size_bytes")
        if not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
            raise GenerationError(f"generation SHA-256 is invalid: {filename}")
        if not verify_files:
            if path.is_symlink() or not path.is_file():
                raise GenerationError(f"generation artifact is unsafe: {filename}")
            continue
        try:
            size = path.stat(follow_symlinks=False).st_size
        except OSError as exc:
            raise GenerationError(f"generation artifact is missing: {filename}") from exc
        if path.is_symlink() or size != expected_size or _sha256_file(path) != expected_sha:
            raise GenerationError(f"generation artifact changed: {filename}")
    return manifest


def _pointer_summary(pointer: dict[str, Any] | None) -> dict[str, Any]:
    if pointer is None:
        return {"kind": "legacy"}
    return {
        "kind": "generation",
        "generation_id": pointer["generation_id"],
        "relative_directory": pointer["relative_directory"],
        "activated_at": pointer.get("activated_at"),
        "job_id": pointer.get("job_id"),
    }


def _pointer_from_summary(model: str, summary: dict[str, Any]) -> dict[str, Any] | None:
    if summary.get("kind") == "legacy":
        return None
    generation_id = summary["generation_id"]
    return {
        "schema_version": SCHEMA_VERSION,
        "model": model,
        "generation_id": generation_id,
        "relative_directory": f"versions/{generation_id}",
        "activated_at": summary.get("activated_at") or _utc_now(),
        "job_id": summary.get("job_id"),
        # A second rollback is intentionally fail-closed; it must not guess a
        # historical predecessor that is absent from the durable pointer.
        "previous": {"kind": "legacy"},
    }


def _remove_managed_tree(path: Path, root: Path) -> None:
    """Delete one exact managed directory without ever following a link."""

    if path.parent != root or path.is_symlink() or not path.is_dir():
        raise GenerationError(f"refusing unsafe generation cleanup target: {path}")
    for directory, names, filenames in os.walk(path, topdown=True, followlinks=False):
        directory_path = Path(directory)
        info = directory_path.lstat()
        if not stat.S_ISDIR(info.st_mode) or not portable_os.owned_by_current_user(info):
            raise GenerationError(f"unsafe directory in generation cleanup: {directory_path}")
        for name in [*names, *filenames]:
            child = directory_path / name
            child_info = child.lstat()
            if stat.S_ISLNK(child_info.st_mode) or not portable_os.owned_by_current_user(child_info):
                raise GenerationError(f"unsafe entry in generation cleanup: {child}")
    for directory, _names, filenames in os.walk(path, topdown=False):
        directory_path = Path(directory)
        for filename in filenames:
            os.chmod(directory_path / filename, 0o600)
        os.chmod(directory_path, 0o700)
    shutil.rmtree(path)
    _fsync_directory(root)


def _cleanup_generations_locked(
    project: Path,
    model: str,
    *,
    extra_keep: set[str] | None = None,
) -> list[str]:
    """Keep only active, its rollback predecessor, and explicitly retained failures."""

    versions = _versions_root(project, model)
    if not versions.exists() and not versions.is_symlink():
        return []
    _require_private_directory(versions, project)
    pointer = read_active_pointer(model, project_dir=project)
    keep = set(extra_keep or ())
    if pointer is not None:
        keep.add(pointer["generation_id"])
        previous = pointer.get("previous") or {}
        if previous.get("kind") == "generation":
            keep.add(previous["generation_id"])
    removed = []
    for entry in sorted(versions.iterdir(), key=lambda value: value.name):
        incomplete = entry.name.startswith(".staging-")
        if not incomplete and (
            not SHA256_RE.fullmatch(entry.name) or entry.name in keep
        ):
            continue
        _remove_managed_tree(entry, versions)
        removed.append(entry.name)
    return removed


def cleanup_generations(
    model: str,
    *,
    project_dir: str | Path | None = None,
    keep_failed_generation: str | None = None,
) -> list[str]:
    """Apply the bounded active+previous(+latest failed) generation policy."""

    _require_model(model)
    project = _project(project_dir)
    extra = {keep_failed_generation} if keep_failed_generation else set()
    if any(not SHA256_RE.fullmatch(value) for value in extra):
        raise GenerationError("retained failed generation id is invalid")
    root = _model_root(project, model)
    if not root.exists() and not root.is_symlink():
        return []
    with _GenerationLock(project, model):
        return _cleanup_generations_locked(project, model, extra_keep=extra)


def _replace_pointer(project: Path, model: str, pointer: dict[str, Any] | None) -> None:
    root = _model_root(project, model)
    _secure_directory(root, project)
    path = root / ACTIVE_POINTER
    if pointer is None:
        if path.is_symlink():
            raise GenerationError("refusing symlink active generation pointer")
        try:
            path.unlink(missing_ok=True)
            _fsync_directory(root)
        except OSError as exc:
            raise GenerationError("cannot restore the legacy generation pointer") from exc
    else:
        _atomic_json(path, _validate_pointer(pointer, model))


def _record_failure(project: Path, model: str, payload: dict[str, Any]) -> None:
    try:
        root = _model_root(project, model)
        _secure_directory(root, project)
        _atomic_json(
            root / FAILURE_RECORD,
            {"schema_version": SCHEMA_VERSION, "model": model, "at": _utc_now(), **payload},
        )
    except GenerationError:
        # The original activation/rollback exception is more actionable.  A
        # missing convenience record must never mask it.
        pass


def rollback_active_generation(
    model: str,
    failed_generation_id: str,
    *,
    error: str,
    project_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """CAS rollback: never undo a newer activation selected by another worker."""

    if not SHA256_RE.fullmatch(str(failed_generation_id)):
        raise GenerationError("rollback generation id is invalid")
    project = _project(project_dir)
    with _GenerationLock(project, model):
        current = read_active_pointer(model, project_dir=project)
        if current is None or current.get("generation_id") != failed_generation_id:
            raise GenerationError("active generation changed; refusing stale rollback")
        replacement = _pointer_from_summary(model, current["previous"])
        if replacement is not None:
            directory = _model_root(project, model) / replacement["relative_directory"]
            verify_generation(model, directory, pointer=replacement, project_dir=project)
        _replace_pointer(project, model, replacement)
        retention_error = None
        try:
            _cleanup_generations_locked(
                project, model, extra_keep={failed_generation_id}
            )
        except GenerationError as exc:
            retention_error = str(exc)
        _record_failure(
            project,
            model,
            {
                "failed_generation_id": failed_generation_id,
                "error": str(error)[:1024],
                "rolled_back_to": replacement.get("generation_id") if replacement else "legacy",
                "retention_error": retention_error,
            },
        )
        return replacement


def _copy_candidate_generation(
    project: Path,
    model: str,
    candidate: Path,
    manifest: dict[str, Any],
    job_id: str,
) -> tuple[Path, dict[str, Any]]:
    generation_id = str(manifest.get("bundle_sha256") or "")
    if not SHA256_RE.fullmatch(generation_id):
        raise GenerationError("quality-gate manifest has an invalid bundle SHA-256")
    file_manifest = manifest.get("files")
    if not isinstance(file_manifest, dict):
        raise GenerationError("quality-gate manifest has no file identities")
    if _bundle_sha256(file_manifest) != generation_id:
        raise GenerationError("quality-gate bundle SHA-256 is inconsistent")
    expected_item_kind = MODEL_ITEM_KINDS[model]
    if manifest.get("item_kind") != expected_item_kind:
        raise GenerationError("quality-gate manifest has the wrong item kind")
    versions = _versions_root(project, model)
    _secure_directory(versions, project)
    destination = versions / generation_id
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise GenerationError("existing generation path is unsafe")
        return destination, verify_generation(model, destination, project_dir=project)

    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".staging-{job_id}-", dir=versions)
    )
    records: dict[str, dict[str, Any]] = {}
    try:
        for filename in MODEL_FILES[model]:
            source = candidate / filename
            expected = file_manifest.get(filename)
            if not isinstance(expected, dict):
                raise GenerationError(f"quality-gate file identity is missing: {filename}")
            destination_file = temporary / filename
            binary = getattr(os, "O_BINARY", 0)
            source_fd = os.open(
                source,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | binary,
            )
            try:
                source_info = os.fstat(source_fd)
                if not stat.S_ISREG(source_info.st_mode):
                    raise GenerationError(f"candidate artifact is unsafe: {filename}")
                destination_fd = os.open(
                    destination_file,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | binary,
                    0o400,
                )
                digest = hashlib.sha256()
                size = 0
                try:
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(destination_fd, view)
                            if written <= 0:
                                raise GenerationError(
                                    f"short write while copying candidate: {filename}"
                                )
                            view = view[written:]
                    os.fsync(destination_fd)
                finally:
                    os.close(destination_fd)
            finally:
                os.close(source_fd)
            actual_sha = digest.hexdigest()
            if actual_sha != expected.get("sha256") or size != expected.get("size_bytes"):
                raise GenerationError(f"candidate changed after quality gate: {filename}")
            records[filename] = {"sha256": actual_sha, "size_bytes": size}

        generation_manifest = {
            "schema_version": SCHEMA_VERSION,
            "model": model,
            "generation_id": generation_id,
            "created_at": _utc_now(),
            "job_id": job_id,
            "quality_gate": manifest.get("quality_gate"),
            "files": records,
        }
        generation_manifest["item_kind"] = expected_item_kind
        _atomic_json(temporary / GENERATION_MANIFEST, generation_manifest)
        os.chmod(temporary / GENERATION_MANIFEST, 0o400)
        _fsync_directory(temporary)
        os.chmod(temporary, 0o500)
        try:
            os.replace(temporary, destination)
            temporary = None
        except FileExistsError:
            # A byte-identical concurrent publisher won the content-addressed
            # name.  Its complete manifest is authoritative.
            pass
        _fsync_directory(versions)
        verified = verify_generation(model, destination, project_dir=project)
        return destination, verified
    finally:
        if temporary is not None and temporary.exists():
            try:
                os.chmod(temporary, 0o700)
                shutil.rmtree(temporary)
            except OSError:
                pass


def _default_validate(model: str, candidate: Path) -> dict[str, Any]:
    if model == "igor":
        from IGORMODEL.promote_candidate import validate_candidate

        return validate_candidate(candidate)
    if model == "vega":
        from VEGAMODEL.promote_candidate import validate_candidate

        return validate_candidate(candidate)
    from model_candidate_promotion import validate_candidate

    return validate_candidate(model, candidate)


def _default_load_and_smoke(model: str, directory: Path) -> Any:
    if model == "almaz":
        import price_model_runtime as runtime
    elif model == "igor":
        import igor_model_runtime as runtime
    elif model == "knives":
        import knife_model_runtime as runtime
    elif model == "gloves":
        import glove_model_runtime as runtime
    elif model == "vega":
        import vega_model_runtime as runtime
    else:
        import neural_model_runtime as runtime
    bundle = runtime.load_bundle_from_directory(directory)
    runtime.smoke_bundle(bundle)
    return bundle


def _managed_candidate(project: Path, model: str, candidate_dir: str | Path, job_id: str) -> Path:
    if not JOB_ID_RE.fullmatch(job_id):
        raise GenerationError("activation job id is invalid")
    root = _absolute(project / STAGING_ROOTS[model])
    candidate = _absolute(candidate_dir)
    _assert_no_symlink_components(candidate, project)
    if candidate != root / job_id or not candidate.is_dir():
        raise GenerationError("hot activation accepts only its managed training candidate")
    return candidate


def _publish_candidate(
    model: str,
    candidate_dir: str | Path,
    *,
    manifest: dict[str, Any],
    job_id: str,
    project_dir: str | Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
    validator: Callable[[str, Path], dict[str, Any]],
    load_and_smoke: Callable[[str, Path], Any],
) -> dict[str, Any]:
    """Implementation seam used by isolated tests; public callers cannot bypass gates."""

    _require_model(model)
    project = _project(project_dir)
    candidate = _managed_candidate(project, model, candidate_dir, job_id)
    cancelled = cancel_check or (lambda: False)
    if cancelled():
        raise GenerationCancelled("activation cancelled before validation")
    with _GenerationLock(project, model):
        # A previous SIGKILL or failed pre-load may have left an unreferenced
        # complete/incomplete generation.  Bound it before allocating another
        # large candidate copy.
        _cleanup_generations_locked(project, model)
        try:
            fresh = validator(model, candidate)
        except GenerationError:
            raise
        except Exception as exc:
            raise GenerationError(
                f"candidate failed strict activation revalidation: {exc}"
            ) from exc
        if fresh.get("bundle_sha256") != manifest.get("bundle_sha256"):
            raise GenerationError("candidate changed since the strict quality gate")
        if not isinstance(fresh.get("quality_gate"), dict):
            raise GenerationError("candidate has no strict quality-gate receipt")
        if cancelled():
            raise GenerationCancelled("activation cancelled during validation")
        generation_dir, generation_manifest = _copy_candidate_generation(
            project, model, candidate, fresh, job_id
        )
        if cancelled():
            raise GenerationCancelled("activation cancelled while staging generation")
        try:
            load_and_smoke(model, generation_dir)
        except Exception as exc:
            try:
                retention_error = None
                _cleanup_generations_locked(project, model)
            except GenerationError as cleanup_exc:
                retention_error = str(cleanup_exc)
            _record_failure(
                project,
                model,
                {
                    "failed_generation_id": generation_dir.name,
                    "error": str(exc)[:1024],
                    "rolled_back_to": "not_switched",
                    "retention_error": retention_error,
                },
            )
            raise GenerationActivationError(
                f"candidate could not be loaded and smoke-tested: {exc}",
                retention_error=retention_error,
            ) from exc
        if cancelled():
            raise GenerationCancelled("activation cancelled during model smoke test")
        previous = read_active_pointer(model, project_dir=project)
        pointer = {
            "schema_version": SCHEMA_VERSION,
            "model": model,
            "generation_id": generation_manifest["generation_id"],
            "relative_directory": f"versions/{generation_manifest['generation_id']}",
            "activated_at": _utc_now(),
            "job_id": job_id,
            "previous": _pointer_summary(previous),
        }
        _replace_pointer(project, model, pointer)
        if cancelled():
            try:
                replacement = _pointer_from_summary(model, pointer["previous"])
                if replacement is not None:
                    restored_dir = _model_root(project, model) / replacement["relative_directory"]
                    verify_generation(
                        model,
                        restored_dir,
                        pointer=replacement,
                        project_dir=project,
                    )
                _replace_pointer(project, model, replacement)
                try:
                    retention_error = None
                    _cleanup_generations_locked(project, model)
                except GenerationError as cleanup_exc:
                    retention_error = str(cleanup_exc)
                _record_failure(
                    project,
                    model,
                    {
                        "failed_generation_id": pointer["generation_id"],
                        "error": "operator cancelled activation after pointer switch",
                        "rolled_back_to": (
                            replacement.get("generation_id") if replacement else "legacy"
                        ),
                        "retention_error": retention_error,
                    },
                )
            except GenerationError as exc:
                raise GenerationActivationError(
                    "activation was cancelled but pointer rollback failed",
                    rollback_error=str(exc),
                ) from exc
            raise GenerationCancelled("activation cancelled and rolled back")
        try:
            retention_error = None
            removed_generations = _cleanup_generations_locked(project, model)
        except GenerationError as exc:
            retention_error = str(exc)
            removed_generations = []
        return {
            "status": "activated",
            "model": model,
            "activation_state": "activated",
            "generation_id": pointer["generation_id"],
            "active_bundle_version": pointer["generation_id"],
            "generation_directory": str(generation_dir),
            "activated_at": pointer["activated_at"],
            "previous_generation_id": previous.get("generation_id") if previous else None,
            "removed_generation_ids": removed_generations,
            "retention_error": retention_error,
        }


def publish_candidate(
    model: str,
    candidate_dir: str | Path,
    *,
    manifest: dict[str, Any],
    job_id: str,
    project_dir: str | Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Strictly revalidate, pre-load, and hot-activate a managed candidate."""

    return _publish_candidate(
        model,
        candidate_dir,
        manifest=manifest,
        job_id=job_id,
        project_dir=project_dir,
        cancel_check=cancel_check,
        validator=_default_validate,
        load_and_smoke=_default_load_and_smoke,
    )


def get_activation_status(
    model: str, *, project_dir: str | Path | None = None
) -> dict[str, Any]:
    """Return pointer/failure metadata without loading heavyweight models."""

    project = _project(project_dir)
    pointer = read_active_pointer(model, project_dir=project)
    failure = _read_json(_model_root(project, model) / FAILURE_RECORD, optional=True)
    return {
        "activation_state": "activated" if pointer else "legacy",
        "active_bundle_version": pointer.get("generation_id") if pointer else None,
        "activated_at": pointer.get("activated_at") if pointer else None,
        "activation_job_id": pointer.get("job_id") if pointer else None,
        "last_activation_failure": failure,
    }


class RuntimeBundleSlot:
    """Thread-safe lazy reload with atomic object-reference hand-off."""

    def __init__(
        self,
        model: str,
        legacy_dir: str | Path,
        *,
        load_bundle: Callable[[Path], Any],
        smoke_bundle: Callable[[Any], None],
        project_dir: str | Path | None = None,
    ):
        _require_model(model)
        self.model = model
        self.project = _project(project_dir)
        self.legacy_dir = _absolute(legacy_dir)
        _assert_no_symlink_components(self.legacy_dir, self.project)
        self._load_bundle = load_bundle
        self._smoke_bundle = smoke_bundle
        self._reload_lock = threading.RLock()
        self._state: LoadedGeneration | None = None
        self._history: dict[tuple[Any, ...], LoadedGeneration] = {}

    def _target(self) -> tuple[tuple[Any, ...], Path, str | None, dict[str, Any] | None]:
        pointer = read_active_pointer(self.model, project_dir=self.project)
        if pointer is not None:
            directory = _model_root(self.project, self.model) / pointer["relative_directory"]
            return ("generation", pointer["generation_id"]), directory, pointer["generation_id"], pointer
        fingerprints = []
        for filename in MODEL_FILES[self.model]:
            path = self.legacy_dir / filename
            try:
                info = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise GenerationError(f"legacy model artifact is missing: {filename}") from exc
            if path.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise GenerationError(f"legacy model artifact is unsafe: {filename}")
            fingerprints.append((filename, info.st_mtime_ns, info.st_size))
        return ("legacy", *fingerprints), self.legacy_dir, None, None

    def get(self) -> Any:
        target_key, directory, generation_id, pointer = self._target()
        current = self._state
        if current is not None and current.key == target_key:
            return current.bundle
        with self._reload_lock:
            target_key, directory, generation_id, pointer = self._target()
            current = self._state
            if current is not None and current.key == target_key:
                return current.bundle
            cached = self._history.get(target_key)
            if cached is not None:
                self._state = cached
                return cached.bundle
            try:
                if generation_id is not None:
                    verify_generation(
                        self.model, directory, pointer=pointer, project_dir=self.project
                    )
                bundle = self._load_bundle(directory)
                self._smoke_bundle(bundle)
                # Loading can be slow.  Never swap a bundle for a pointer that
                # changed while it was being prepared.
                check_key, _check_dir, _check_id, _check_pointer = self._target()
                if check_key != target_key:
                    return self.get()
            except Exception as exc:
                if generation_id is None:
                    raise
                try:
                    rollback_active_generation(
                        self.model,
                        generation_id,
                        error=str(exc),
                        project_dir=self.project,
                    )
                except Exception as rollback_exc:  # noqa: BLE001 - preserve rollback evidence.
                    try:
                        current_pointer = read_active_pointer(
                            self.model, project_dir=self.project
                        )
                    except GenerationError:
                        current_pointer = {"generation_id": generation_id}
                    if (
                        current_pointer is not None
                        and current_pointer.get("generation_id") == generation_id
                    ):
                        _record_failure(
                            self.project,
                            self.model,
                            {
                                "failed_generation_id": generation_id,
                                "error": str(exc)[:1024],
                                "rolled_back_to": None,
                                "rollback_error": str(rollback_exc)[:1024],
                            },
                        )
                        raise GenerationActivationError(
                            f"{self.model} generation failed to load and rollback failed",
                            rollback_error=str(rollback_exc),
                        ) from exc
                if current is not None:
                    return current.bundle
                # First request in this process: load the restored generation
                # (or legacy bootstrap) and fail if that also cannot load.
                return self.get()
            loaded = LoadedGeneration(target_key, bundle, directory, generation_id)
            self._state = loaded
            self._history[target_key] = loaded
            if len(self._history) > 3:
                oldest = next(iter(self._history))
                if oldest != target_key:
                    self._history.pop(oldest, None)
            return bundle

    def status(self) -> dict[str, Any]:
        pointer = read_active_pointer(self.model, project_dir=self.project)
        state = self._state
        return {
            "active_bundle_version": pointer.get("generation_id") if pointer else None,
            "loaded_bundle_version": state.generation_id if state else None,
            "bundle_source": "generation" if pointer else "legacy",
            "loaded_source_directory": str(state.source_dir) if state else None,
        }
