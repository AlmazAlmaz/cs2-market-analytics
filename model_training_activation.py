"""Queue quality-gated model candidates and activate them while services are offline."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import app_config
import model_lifecycle

try:
    import fcntl
except ImportError:  # pragma: no cover - production is macOS/Linux.
    fcntl = None


SCHEMA_VERSION = 1
MODEL_NAMES = ("almaz", "igor", "nova", "knives")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ActivationError(RuntimeError):
    """A pending activation record or offline promotion is unsafe."""


class CandidateActivationIntegrityError(ActivationError):
    """A pending candidate failed an integrity/provenance check."""

    classification = "integrity"


class CandidateActivationQualityError(ActivationError):
    """A pending candidate has valid evidence but insufficient quality."""

    classification = "quality"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project(project_dir: str | Path | None = None) -> Path:
    return Path(project_dir or app_config.PROJECT_DIR).expanduser().resolve()


def _pending_dir(project: Path) -> Path:
    return project / ".run" / "model_training" / "pending"


def _lock_path(project: Path) -> Path:
    return project / ".run" / "model_training" / "activation.lock"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secure_directory(path: Path, project: Path) -> None:
    try:
        relative = path.relative_to(project)
    except ValueError as exc:
        raise ActivationError("activation directory escapes the project") from exc
    cursor = project
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ActivationError(f"refusing symlink activation directory: {cursor}")
        try:
            cursor.mkdir(mode=0o700)
        except FileExistsError:
            if not cursor.is_dir():
                raise ActivationError(f"activation path is not a directory: {cursor}")
        except OSError as exc:
            raise ActivationError(f"cannot create activation directory: {cursor}") from exc
        try:
            os.chmod(cursor, 0o700)
        except OSError as exc:
            raise ActivationError(f"cannot secure activation directory: {cursor}") from exc


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ActivationError(f"refusing symlink activation record: {path}")
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except (OSError, TypeError, ValueError) as exc:
        raise ActivationError(f"cannot write pending activation: {path}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _activation_lock(project: Path, *, blocking: bool = False):
    if fcntl is None:
        raise ActivationError("model activation requires POSIX fcntl locking")
    path = _lock_path(project)
    _secure_directory(path.parent, project)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    acquired = False
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        operation = fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(descriptor, operation)
        acquired = True
    except BlockingIOError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ActivationError("another model activation is already running") from exc
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ActivationError("cannot lock model activation") from exc
    try:
        yield
    finally:
        try:
            if acquired and descriptor is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            if descriptor is not None:
                os.close(descriptor)


@contextmanager
def _training_idle_lock(project: Path):
    """Block activation while any inherited model-training lock is alive."""

    if fcntl is None:
        raise ActivationError("model activation requires POSIX fcntl locking")
    path = project / ".run" / "model_training" / "resource.lock"
    _secure_directory(path.parent, project)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    acquired = False
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
    except BlockingIOError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ActivationError("model training is still running; activation was deferred") from exc
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ActivationError("cannot prove model training is idle") from exc
    try:
        yield
    finally:
        try:
            if acquired and descriptor is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _candidate_root(project: Path, model: str) -> Path:
    roots = {
        "almaz": project / "models" / "staging" / "training_jobs",
        "igor": project / "IGORMODEL" / "staging" / "training_jobs",
        "nova": project / "NEURALMODEL" / "staging" / "training_jobs",
        "knives": project / "KNIFEMODEL" / "staging" / "training_jobs",
    }
    return roots[model].resolve(strict=False)


def _validated_candidate_path(
    project: Path, model: str, job_id: str, candidate_dir: str | Path
) -> Path:
    if model not in MODEL_NAMES or not JOB_ID_RE.fullmatch(job_id):
        raise ActivationError("invalid pending model or job id")
    path = Path(candidate_dir)
    if not path.is_absolute():
        path = project / path
    raw = Path(os.path.abspath(path.expanduser()))
    root = _candidate_root(project, model)
    try:
        relative = raw.relative_to(root)
    except ValueError as exc:
        raise ActivationError("candidate is outside its managed training root") from exc
    if relative.parts != (job_id,):
        raise ActivationError("candidate directory does not match its immutable job id")
    cursor = project
    for part in raw.relative_to(project).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ActivationError(f"refusing symlink candidate path: {cursor}")
    if not raw.is_dir():
        raise ActivationError("pending candidate directory is missing")
    return raw


def _validate_manifest(model: str, candidate: Path) -> dict[str, Any]:
    try:
        if model == "igor":
            from IGORMODEL.promote_candidate import validate_candidate

            manifest = validate_candidate(candidate)
        else:
            from model_candidate_promotion import validate_candidate

            manifest = validate_candidate(model, candidate)
    except Exception as exc:
        if getattr(exc, "classification", None) == "quality":
            raise CandidateActivationQualityError(
                f"{model} candidate failed its quality gate: {exc}"
            ) from exc
        raise CandidateActivationIntegrityError(
            f"{model} candidate failed its integrity check: {exc}"
        ) from exc
    bundle_sha = manifest.get("bundle_sha256")
    if not isinstance(bundle_sha, str) or not SHA256_RE.fullmatch(bundle_sha):
        raise CandidateActivationIntegrityError(
            "candidate validator did not return a bundle SHA-256"
        )
    if not isinstance(manifest.get("quality_gate"), dict):
        raise CandidateActivationIntegrityError(
            "candidate validator did not return a quality gate"
        )
    return manifest


def schedule_pending_activation(
    model: str,
    job_id: str,
    candidate_dir: str | Path,
    *,
    manifest: dict[str, Any] | None = None,
    project_dir: str | Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Persist one internally-created, already quality-gated candidate."""

    project = _project(project_dir)
    pending = _pending_dir(project)
    _secure_directory(pending, project)
    candidate = _validated_candidate_path(project, model, job_id, candidate_dir)
    checked = manifest or _validate_manifest(model, candidate)
    # Never trust a manifest supplied by a caller without checking the bytes again.
    fresh = _validate_manifest(model, candidate)
    if checked.get("bundle_sha256") != fresh.get("bundle_sha256"):
        raise ActivationError("candidate changed between quality gate and scheduling")
    if cancel_check is not None and cancel_check():
        raise ActivationError("activation scheduling was cancelled")
    record = {
        "schema_version": SCHEMA_VERSION,
        "model": model,
        "job_id": job_id,
        "candidate_dir": str(candidate.relative_to(project)),
        "candidate_bundle_sha256": fresh["bundle_sha256"],
        "quality_gate": fresh["quality_gate"],
        "scheduled_at": _utc_now(),
        "activation_policy": "next_offline_start",
    }
    with _activation_lock(project):
        if cancel_check is not None and cancel_check():
            raise ActivationError("activation scheduling was cancelled")
        _atomic_json(pending / f"{model}.json", record)
    return record


def discard_pending_activation(
    model: str,
    job_id: str,
    *,
    project_dir: str | Path | None = None,
) -> bool:
    """Remove only the pending record owned by one cancelled training job."""

    if model not in MODEL_NAMES or not JOB_ID_RE.fullmatch(job_id):
        raise ActivationError("invalid pending model or job id")
    project = _project(project_dir)
    path = _pending_dir(project) / f"{model}.json"
    with _activation_lock(project, blocking=True):
        if not path.exists():
            if path.is_symlink():
                raise ActivationError("pending activation is a dangling symlink")
            return False
        record = _read_record(path, project, require_candidate=False)
        if record["job_id"] != job_id:
            return False
        path.unlink()
        _fsync_directory(path.parent)
        return True


def _read_record(
    path: Path, project: Path, *, require_candidate: bool = True
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ActivationError(f"pending activation is missing or unsafe: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ActivationError(f"pending activation is invalid: {path.name}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ActivationError(f"pending activation schema is invalid: {path.name}")
    model = value.get("model")
    job_id = value.get("job_id")
    expected_sha = value.get("candidate_bundle_sha256")
    if model not in MODEL_NAMES or path.name != f"{model}.json":
        raise ActivationError("pending activation has an invalid model")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise ActivationError("pending activation has an invalid job id")
    if not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
        raise ActivationError("pending activation has an invalid bundle hash")
    if require_candidate:
        _validated_candidate_path(
            project, model, job_id, value.get("candidate_dir", "")
        )
    return value


def get_pending_activations(
    *, project_dir: str | Path | None = None
) -> dict[str, dict[str, Any]]:
    project = _project(project_dir)
    pending = _pending_dir(project)
    if not pending.exists():
        if pending.is_symlink():
            raise ActivationError("pending activation directory is a dangling symlink")
        return {}
    if pending.is_symlink() or not pending.is_dir():
        raise ActivationError("pending activation directory is unsafe")
    result = {}
    for model in MODEL_NAMES:
        path = pending / f"{model}.json"
        if path.exists() or path.is_symlink():
            result[model] = _read_record(path, project)
    return result


def activate_pending(
    *,
    offline_confirm: bool = False,
    project_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Promote every pending candidate; each model retains its own full backup."""

    if offline_confirm is not True:
        raise ActivationError("pending activation requires --offline-confirm")
    project = _project(project_dir)
    configured_project = _project(app_config.PROJECT_DIR)
    if project != configured_project:
        raise ActivationError(
            "activation project must match the configured production project"
        )
    results: dict[str, Any] = {}
    with _activation_lock(project), _training_idle_lock(project):
        try:
            journal = model_lifecycle.read_promotion_journal(project_dir=project)
            if journal is not None:
                if journal.get("kind") == "generic_files_v1":
                    from model_candidate_promotion import recover_interrupted_promotion

                    recover_interrupted_promotion()
                elif journal.get("kind") == "igor_directory_v1":
                    from IGORMODEL.promote_candidate import (
                        recover_interrupted_promotion,
                    )

                    recover_interrupted_promotion()
                else:
                    raise ActivationError(
                        "an unknown model promotion journal requires manual recovery"
                    )
        except model_lifecycle.ModelLifecycleError as exc:
            raise ActivationError(
                f"cannot inspect interrupted model promotion: {exc}"
            ) from exc
        except ActivationError:
            raise
        except Exception as exc:
            raise ActivationError(
                f"could not recover interrupted model promotion: {exc}"
            ) from exc
        records = get_pending_activations(project_dir=project)
        if records:
            from model_candidate_promotion import find_live_service_processes

            live = find_live_service_processes()
            if live:
                raise ActivationError(
                    "dashboard or deal scanner is already running; activation was deferred"
                )
        for model, record in records.items():
            try:
                if model == "igor":
                    from IGORMODEL.promote_candidate import (
                        installed_candidate_bundle_sha256,
                    )

                    installed_sha = installed_candidate_bundle_sha256()
                else:
                    from model_candidate_promotion import (
                        installed_candidate_bundle_sha256,
                    )

                    installed_sha = installed_candidate_bundle_sha256(model)
            except Exception as exc:
                raise ActivationError(
                    f"could not verify installed {model} promotion receipt: {exc}"
                ) from exc
            if installed_sha == record["candidate_bundle_sha256"]:
                record_path = _pending_dir(project) / f"{model}.json"
                record_path.unlink()
                _fsync_directory(record_path.parent)
                results[model] = {
                    "status": "already_promoted",
                    "candidate_bundle_sha256": installed_sha,
                }
                continue
            candidate = _validated_candidate_path(
                project, model, record["job_id"], record["candidate_dir"]
            )
            fresh = _validate_manifest(model, candidate)
            if fresh["bundle_sha256"] != record["candidate_bundle_sha256"]:
                raise ActivationError(f"pending {model} candidate bytes have changed")
            try:
                if model == "igor":
                    from IGORMODEL.promote_candidate import promote_candidate

                    result = promote_candidate(candidate, offline_confirm=True)
                else:
                    from model_candidate_promotion import promote_candidate

                    result = promote_candidate(
                        model, candidate, offline_confirm=True
                    )
            except Exception as exc:
                raise ActivationError(f"could not activate pending {model}: {exc}") from exc
            record_path = _pending_dir(project) / f"{model}.json"
            record_path.unlink()
            _fsync_directory(record_path.parent)
            results[model] = result
    return {"status": "complete", "activated": results, "count": len(results)}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Activate quality-gated model candidates before services start"
    )
    parser.add_argument("--activate-pending", action="store_true", required=True)
    parser.add_argument("--offline-confirm", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = activate_pending(offline_confirm=args.offline_confirm)
    except ActivationError as exc:
        raise SystemExit(f"pending model activation error: {exc}") from exc
    if result["count"]:
        names = ", ".join(sorted(result["activated"]))
        print(f"activated quality-gated model update(s): {names}")
    else:
        print("model updates: no pending candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
