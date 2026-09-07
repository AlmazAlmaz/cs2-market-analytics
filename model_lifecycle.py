"""Cooperative model-runtime barrier and durable promotion journal.

Dashboard and deal-scanner processes hold a shared lock for their complete
lifetime.  A model promoter must hold the exclusive side while it validates,
backs up, and commits production artifacts.  The journal is deliberately
small: model-specific recovery code validates every path and backup before it
uses a record written here.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import app_config

try:
    import fcntl
except ImportError:  # pragma: no cover - production is macOS/Linux.
    fcntl = None


LOCK_FILENAME = "model_runtime.lock"
STARTUP_LOCK_FILENAME = "start_all.lock"
JOURNAL_FILENAME = "model_promotion_journal.json"
JOURNAL_SCHEMA_VERSION = 1


class ModelLifecycleError(RuntimeError):
    """The runtime barrier or durable promotion journal is unsafe."""


def _project(project_dir: str | Path | None = None) -> Path:
    return Path(project_dir or app_config.PROJECT_DIR).expanduser().resolve()


def _run_dir(project: Path) -> Path:
    return project / ".run" / "model_lifecycle"


def _secure_directory(path: Path, project: Path) -> None:
    try:
        relative = path.relative_to(project)
    except ValueError as exc:
        raise ModelLifecycleError("model lifecycle path escapes the project") from exc
    cursor = project
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ModelLifecycleError(
                f"refusing symlink model lifecycle directory: {cursor}"
            )
        created = False
        try:
            cursor.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            if not cursor.is_dir():
                raise ModelLifecycleError(
                    f"model lifecycle path is not a directory: {cursor}"
                )
        except OSError as exc:
            raise ModelLifecycleError(
                f"cannot create model lifecycle directory: {cursor}"
            ) from exc
        try:
            os.chmod(cursor, 0o700)
        except OSError as exc:
            raise ModelLifecycleError(
                f"cannot secure model lifecycle directory: {cursor}"
            ) from exc
        if created:
            try:
                _fsync_directory(cursor.parent)
            except OSError as exc:
                raise ModelLifecycleError(
                    f"cannot persist model lifecycle directory: {cursor}"
                ) from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ModelLifecycleError("model lifecycle lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except ModelLifecycleError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ModelLifecycleError(
            f"cannot securely open model lifecycle lock: {path}"
        ) from exc


@contextmanager
def _lifecycle_lock(
    operation: int,
    *,
    project_dir: str | Path | None = None,
    unavailable_message: str,
    require_clean_journal: bool = False,
    lock_filename: str = LOCK_FILENAME,
) -> Iterator[None]:
    if fcntl is None:
        raise ModelLifecycleError("model lifecycle locking requires POSIX fcntl")
    project = _project(project_dir)
    run_dir = _run_dir(project)
    _secure_directory(run_dir, project)
    descriptor = _open_lock(run_dir / lock_filename)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise ModelLifecycleError(unavailable_message) from exc
        if require_clean_journal and read_promotion_journal(project_dir=project):
            raise ModelLifecycleError(
                "service cannot start until the interrupted model promotion "
                "journal is recovered"
            )
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        else:
            os.close(descriptor)


def service_lifetime_lock(
    service: str, *, project_dir: str | Path | None = None
):
    """Return a shared lock context held for one model-consuming service."""

    if service not in {"dashboard", "deal_scanner"}:
        raise ValueError("service must be dashboard or deal_scanner")
    return _lifecycle_lock(
        fcntl.LOCK_SH if fcntl is not None else 0,
        project_dir=project_dir,
        unavailable_message=(
            f"{service} cannot start while a production model update is in progress"
        ),
        require_clean_journal=True,
    )


def production_model_read_lock(*, project_dir: str | Path | None = None):
    """Protect one coherent production-bundle inspection from promotion."""

    return _lifecycle_lock(
        fcntl.LOCK_SH if fcntl is not None else 0,
        project_dir=project_dir,
        unavailable_message="production model update is in progress",
        require_clean_journal=True,
    )


def exclusive_model_update_lock(*, project_dir: str | Path | None = None):
    """Return the one canonical exclusive production-update lock context."""

    return _lifecycle_lock(
        fcntl.LOCK_EX if fcntl is not None else 0,
        project_dir=project_dir,
        unavailable_message=(
            "production model update requires dashboard and deal_scanner to be offline"
        ),
    )


def startup_lifetime_lock(*, project_dir: str | Path | None = None):
    """Return the canonical singleton lock for one complete start_all run."""

    return _lifecycle_lock(
        fcntl.LOCK_EX if fcntl is not None else 0,
        project_dir=project_dir,
        unavailable_message="another start_all command is already running",
        lock_filename=STARTUP_LOCK_FILENAME,
    )


def _journal_path(project: Path) -> Path:
    return _run_dir(project) / JOURNAL_FILENAME


def write_promotion_journal(
    record: dict[str, Any], *, project_dir: str | Path | None = None
) -> dict[str, Any]:
    """Durably write the single canonical in-progress promotion record."""

    project = _project(project_dir)
    run_dir = _run_dir(project)
    _secure_directory(run_dir, project)
    path = _journal_path(project)
    if path.exists() or path.is_symlink():
        raise ModelLifecycleError(
            "an unresolved model promotion journal already exists"
        )
    if "schema_version" in record:
        raise ModelLifecycleError("journal record must not override schema_version")
    value = {**record, "schema_version": JOURNAL_SCHEMA_VERSION}
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=run_dir
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(run_dir)
    except (OSError, TypeError, ValueError) as exc:
        raise ModelLifecycleError("cannot persist model promotion journal") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return value


def read_promotion_journal(
    *, project_dir: str | Path | None = None
) -> dict[str, Any] | None:
    project = _project(project_dir)
    path = _journal_path(project)
    cursor = project
    for part in path.parent.relative_to(project).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ModelLifecycleError(
                f"refusing symlink model lifecycle directory: {cursor}"
            )
        if not cursor.exists():
            return None
        if not cursor.is_dir():
            raise ModelLifecycleError(
                f"model lifecycle path is not a directory: {cursor}"
            )
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ModelLifecycleError("model promotion journal is unsafe")
        if info.st_size <= 0 or info.st_size > 64 * 1024:
            raise ModelLifecycleError("model promotion journal size is invalid")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            value = json.load(handle)
    except FileNotFoundError:
        if path.is_symlink():
            raise ModelLifecycleError(
                "model promotion journal is a dangling symlink"
            )
        return None
    except ModelLifecycleError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if path.is_symlink():
            raise ModelLifecycleError(
                "model promotion journal is a dangling symlink"
            ) from exc
        raise ModelLifecycleError("model promotion journal is unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != JOURNAL_SCHEMA_VERSION
    ):
        raise ModelLifecycleError("model promotion journal schema is invalid")
    return value


def clear_promotion_journal(
    *,
    expected_model: str,
    expected_backup: str | Path,
    project_dir: str | Path | None = None,
) -> None:
    """Remove a journal only if it still identifies the recovered transaction."""

    project = _project(project_dir)
    path = _journal_path(project)
    value = read_promotion_journal(project_dir=project)
    if value is None:
        raise ModelLifecycleError("model promotion journal disappeared")
    if (
        value.get("model") != expected_model
        or value.get("backup_directory") != str(expected_backup)
    ):
        raise ModelLifecycleError("model promotion journal changed unexpectedly")
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except OSError as exc:
        raise ModelLifecycleError("cannot clear model promotion journal") from exc
