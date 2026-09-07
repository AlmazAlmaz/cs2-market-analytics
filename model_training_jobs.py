"""Safe, persistent background jobs for staging-only model retraining.

This module deliberately contains no HTTP/authentication policy.  The
dashboard can call the public helpers after it has authenticated and checked
the request.  Browser input is limited to one of the fixed model names; paths,
commands and trainer arguments are never accepted from the request.

Only one memory-heavy trainer may run at a time.  A POSIX ``flock`` descriptor
is inherited by both the worker and its trainer, so the lock survives a
dashboard restart and even a worker crash while the trainer is still alive.
Successful jobs create staging candidates, pass them through a strict
same-holdout quality gate, publish an immutable generation, and atomically
select it for runtime hot reload.  The legacy offline activation path remains
readable for jobs queued by an older application version.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import app_config
import model_generations
from item_classification import (
    ITEM_KIND_GLOVE,
    ITEM_KIND_KNIFE,
    ITEM_KIND_WEAPON_SKIN,
    STRICT_ITEM_KIND_POLICY,
    sql_item_kind_expression,
)
from price_model_data_contract import time_safe_reference_sql

try:
    import fcntl
except ImportError:  # pragma: no cover - production is macOS/Linux
    fcntl = None


SCHEMA_VERSION = 1
MODEL_NAMES = ("almaz", "igor", "nova", "knives", "gloves")
LEGACY_SCHEMA_V1_MODEL_NAMES = ("almaz", "igor", "nova")
PRE_GLOVE_SCHEMA_V1_MODEL_NAMES = ("almaz", "igor", "nova", "knives")
LEGACY_ALMAZ_SHA_ERROR = (
    "candidate quality gate rejected almaz: "
    "Almaz production metrics do not identify the model SHA-256"
)
CORRECTED_ALMAZ_SHA_ERROR = (
    "candidate integrity check rejected almaz: "
    "Almaz production metrics do not identify the model SHA-256"
)
ACTIVE_STATES = frozenset({"starting", "running", "cancelling"})
TERMINAL_STATES = frozenset(
    {"idle", "succeeded", "rejected", "failed", "cancelled", "interrupted"}
)
MAX_LOG_TAIL_BYTES = 24 * 1024
MAX_LOG_TAIL_LINES = 40
HEARTBEAT_INTERVAL_SECONDS = 2.0
WORKER_CANCEL_GRACE_SECONDS = 12.0
STARTUP_RECOVERY_GRACE_SECONDS = 30.0
MIN_NEW_ROWS = 25_000
MIN_DATA_GROWTH_PERCENT = 5.0
MIN_RUNTIME_VALID_NEW_ROWS = 5_000
# The knife trainer reserves 5,000 untouched test rows, then requires at least
# 10,000 development rows.  A 1,000-row margin absorbs timestamp-boundary ties
# in the chronological split instead of advertising a bootstrap that must fail.
KNIFE_BOOTSTRAP_MIN_ROWS = 16_000
KNIFE_RETRAIN_MIN_NEW_ROWS = 5_000
KNIFE_BOOTSTRAP_MIN_RUNTIME_VALID_ROWS = 16_000
KNIFE_RETRAIN_MIN_RUNTIME_VALID_ROWS = 1_000
GLOVE_BOOTSTRAP_MIN_ROWS = KNIFE_BOOTSTRAP_MIN_ROWS
GLOVE_RETRAIN_MIN_NEW_ROWS = KNIFE_RETRAIN_MIN_NEW_ROWS
GLOVE_BOOTSTRAP_MIN_RUNTIME_VALID_ROWS = KNIFE_BOOTSTRAP_MIN_RUNTIME_VALID_ROWS
GLOVE_RETRAIN_MIN_RUNTIME_VALID_ROWS = KNIFE_RETRAIN_MIN_RUNTIME_VALID_ROWS
RESERVED_TEST_ROWS = 5_000
MODEL_ESTIMATED_TOTAL_SECONDS = {
    "almaz": 2_400,
    "igor": 4_200,
    "nova": 2_700,
    "knives": 2_400,
    "gloves": 2_400,
}
MODEL_MIN_FREE_DISK_BYTES = {
    "almaz": 6 * 1024**3,
    "igor": 12 * 1024**3,
    "nova": 7 * 1024**3,
    "knives": 6 * 1024**3,
    "gloves": 6 * 1024**3,
}

_THREAD_LOCK = threading.RLock()
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([a-z0-9_.-]*(?:api[_-]?key|password|proxy|secret|token))"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_URL_USERINFO_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^/@\s]+)@")
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|authorization|password|proxy|secret|token)=)[^&#\s]+"
)
_CATBOOST_ITERATION_RE = re.compile(r"^\s*(\d+):\s+.*\bremaining:\s*(\S.*)$")
_SKLEARN_ITERATION_RE = re.compile(r"^\s*\[(\d+)\s*/\s*(\d+)\]")
_NOVA_EPOCH_RE = re.compile(r"^epoch\s+(\d+)\b", re.IGNORECASE)


class TrainingJobError(RuntimeError):
    """Persistent training state is invalid or a safe operation failed."""


class TrainingConflictError(TrainingJobError):
    """Another model training process already owns the global resource lock."""


class TrainingCancelled(TrainingJobError):
    """The operator cancelled a job before or during trainer execution."""


def _model_item_kind(model: str) -> str:
    if model == "knives":
        return ITEM_KIND_KNIFE
    if model == "gloves":
        return ITEM_KIND_GLOVE
    if model in {"almaz", "igor", "nova"}:
        return ITEM_KIND_WEAPON_SKIN
    raise ValueError(f"unknown model: {model!r}")


class CandidateRejectedError(TrainingJobError):
    """A completed trainer produced a candidate that cannot be published."""

    rejection_kind = "integrity"

    def __init__(
        self,
        message: str,
        *,
        quality_comparison: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.quality_comparison = quality_comparison


class CandidateIntegrityRejectedError(CandidateRejectedError):
    """Candidate evidence is missing, stale, or internally inconsistent."""


class CandidateQualityRejectedError(CandidateRejectedError):
    """Valid comparison evidence shows that candidate quality is insufficient."""

    rejection_kind = "quality"


@dataclass(frozen=True)
class JobPaths:
    project: Path
    run_dir: Path
    log_dir: Path
    snapshot_dir: Path
    state: Path
    state_lock: Path
    resource_lock: Path


@dataclass(frozen=True)
class ModelSpec:
    name: str
    candidate_dir: Path
    command: tuple[str, ...]
    required_files: tuple[str, ...]
    metrics_file: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _redact(value: Any, *, limit: int = 1000) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").replace("\r", " ").strip()
    if not text:
        return None
    text = _URL_USERINFO_RE.sub(r"\1***@", text)
    text = _SENSITIVE_QUERY_RE.sub(r"\1***", text)
    text = _AUTHORIZATION_RE.sub(r"\1***", text)
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1\2***", text)
    text = _BEARER_RE.sub(r"\1***", text)
    return text[:limit]


def _paths(project_dir: str | Path | None = None) -> JobPaths:
    project = Path(project_dir or app_config.PROJECT_DIR).expanduser().resolve()
    run_dir = project / ".run" / "model_training"
    log_dir = project / "logs" / "model_training"
    snapshot_dir = run_dir / "snapshots"
    return JobPaths(
        project=project,
        run_dir=run_dir,
        log_dir=log_dir,
        snapshot_dir=snapshot_dir,
        state=run_dir / "state.json",
        state_lock=run_dir / "state.lock",
        resource_lock=run_dir / "resource.lock",
    )


def _ensure_within(path: Path, root: Path, label: str) -> Path:
    root = root.resolve()
    raw = path.expanduser()
    if raw.is_symlink():
        raise TrainingJobError(f"refusing symlink {label}: {raw}")
    resolved = raw.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise TrainingJobError(f"{label} escapes managed root: {resolved}") from exc
    return resolved


def _secure_directory(path: Path, root: Path) -> Path:
    root = root.resolve()
    raw = path.expanduser()
    if not raw.is_absolute():
        raw = root / raw
    raw = Path(os.path.abspath(raw))
    try:
        relative = raw.relative_to(root)
    except ValueError as exc:
        raise TrainingJobError(f"directory escapes managed root: {raw}") from exc
    cursor = root.resolve()
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise TrainingJobError(f"refusing symlink directory: {cursor}")
        try:
            cursor.mkdir(mode=0o700)
        except FileExistsError:
            if not cursor.is_dir():
                raise TrainingJobError(f"managed directory is not a directory: {cursor}")
        except OSError as exc:
            raise TrainingJobError(f"cannot prepare managed directory: {cursor}") from exc
        try:
            os.chmod(cursor, 0o700)
        except OSError as exc:
            raise TrainingJobError(f"cannot secure managed directory: {cursor}") from exc
    return _ensure_within(raw, root, "directory")


def _secure_open(path: Path, flags: int, mode: int = 0o600) -> int:
    if path.is_symlink():
        raise TrainingJobError(f"refusing symlink state/log path: {path}")
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, mode)
        os.fchmod(descriptor, mode)
        return descriptor
    except OSError as exc:
        raise TrainingJobError(f"cannot securely open {path}") from exc


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise TrainingJobError(f"refusing symlink state path: {path}")
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except (OSError, TypeError, ValueError) as exc:
        raise TrainingJobError(f"cannot atomically write training state: {path}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _idle_job(model: str) -> dict[str, Any]:
    return {
        "job_id": None,
        "model": model,
        "state": "idle",
        "phase": "idle",
        "progress_percent": 0.0,
        "eta_seconds": None,
        "created_at": None,
        "started_at": None,
        "updated_at": None,
        "heartbeat_at": None,
        "finished_at": None,
        "worker_pid": None,
        "worker_start_token": None,
        "trainer_pid": None,
        "trainer_start_token": None,
        "exit_code": None,
        "error": None,
        "rejection_kind": None,
        "cancel_requested": False,
        "candidate_dir": None,
        "snapshot_name": None,
        "snapshot_state": "none",
        "snapshot_rows": None,
        "quality_gate": None,
        "quality_comparison": None,
        "candidate_bundle_sha256": None,
        "activation_state": "none",
        "active_bundle_version": None,
        "activation_error": None,
        "rollback_error": None,
        "retention_error": None,
    }


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "active_job_id": None,
        "models": {name: _idle_job(name) for name in MODEL_NAMES},
    }


def _validate_state(
    payload: Any,
    *,
    expected_model_names: tuple[str, ...] = MODEL_NAMES,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise TrainingJobError("invalid or unsupported training state")
    models = payload.get("models")
    if not isinstance(models, dict) or set(models) != set(expected_model_names):
        raise TrainingJobError("training state has an invalid model set")
    active_job_id = payload.get("active_job_id")
    if active_job_id is not None and not _JOB_ID_RE.fullmatch(str(active_job_id)):
        raise TrainingJobError("training state has an invalid active job id")
    for name, job in models.items():
        if not isinstance(job, dict) or job.get("model") != name:
            raise TrainingJobError("training state has an invalid job record")
        if job.get("state") not in ACTIVE_STATES | TERMINAL_STATES:
            raise TrainingJobError("training state contains an unknown status")
        job_id = job.get("job_id")
        if job_id is not None and not _JOB_ID_RE.fullmatch(str(job_id)):
            raise TrainingJobError("training state has an invalid job id")
        progress = job.get("progress_percent")
        if isinstance(progress, bool) or not isinstance(progress, (int, float)):
            raise TrainingJobError("training progress must be numeric")
        if not math.isfinite(float(progress)) or not 0 <= float(progress) <= 100:
            raise TrainingJobError("training progress is outside 0..100")
        for key in ("worker_pid", "trainer_pid"):
            pid = job.get(key)
            if pid is not None and (
                isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            ):
                raise TrainingJobError(f"invalid {key} in training state")
        if job.get("rejection_kind") not in {None, "integrity", "quality"}:
            raise TrainingJobError("training state has an invalid rejection kind")
        if job.get("quality_comparison") is not None and not isinstance(
            job.get("quality_comparison"), dict
        ):
            raise TrainingJobError("training state has an invalid quality comparison")
    return payload


def _migrate_state(payload: Any) -> tuple[dict[str, Any], bool]:
    """Upgrade an exact legacy schema-v1 model set without losing job history.

    Schema v1 originally persisted only Almaz, Igor, and Nova; Knives and then
    Gloves were added without otherwise changing the state format.  A narrowly
    scoped in-version migration is safer than resetting terminal job records.
    Unknown, partial, or future model sets remain fail-closed and are never
    rewritten.
    """

    migrated = payload
    changed = False
    legacy_model_sets = (
        set(LEGACY_SCHEMA_V1_MODEL_NAMES),
        set(PRE_GLOVE_SCHEMA_V1_MODEL_NAMES),
    )
    if (
        isinstance(payload, dict)
        and payload.get("schema_version") == SCHEMA_VERSION
        and isinstance(payload.get("models"), dict)
        and set(payload["models"]) in legacy_model_sets
    ):
        existing_names = tuple(payload["models"])
        _validate_state(
            payload,
            expected_model_names=existing_names,
        )
        migrated = copy.deepcopy(payload)
        for name in MODEL_NAMES:
            if name not in migrated["models"]:
                migrated["models"][name] = _idle_job(name)
        changed = True
    else:
        migrated = _validate_state(payload)

    almaz = migrated["models"]["almaz"]
    if (
        almaz.get("state") == "rejected"
        and almaz.get("error") == LEGACY_ALMAZ_SHA_ERROR
    ):
        if not changed:
            migrated = copy.deepcopy(migrated)
            almaz = migrated["models"]["almaz"]
        almaz["legacy_error"] = LEGACY_ALMAZ_SHA_ERROR
        almaz["error"] = CORRECTED_ALMAZ_SHA_ERROR
        almaz["phase"] = "validation_failed"
        almaz["rejection_kind"] = "integrity"
        changed = True

    return _validate_state(migrated), changed


def _read_state_unlocked(paths: JobPaths, *, create: bool = True) -> dict[str, Any]:
    if not paths.state.exists():
        if not create:
            return _default_state()
        state = _default_state()
        _atomic_json(paths.state, state)
        return state
    if paths.state.is_symlink():
        raise TrainingJobError(f"refusing symlink training state: {paths.state}")
    try:
        payload = json.loads(paths.state.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TrainingJobError("training state is unreadable; refusing to start") from exc
    try:
        if stat.S_IMODE(paths.state.stat().st_mode) != 0o600:
            os.chmod(paths.state, 0o600)
    except OSError as exc:
        raise TrainingJobError("cannot secure training state") from exc
    state, migrated = _migrate_state(payload)
    if migrated:
        _atomic_json(paths.state, state)
    return state


@contextmanager
def _locked_state(paths: JobPaths):
    if fcntl is None:
        raise TrainingJobError("model training jobs require POSIX fcntl locking")
    _secure_directory(paths.run_dir, paths.project)
    with _THREAD_LOCK:
        descriptor = _secure_open(paths.state_lock, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _mutate_state(paths: JobPaths, callback: Callable[[dict[str, Any]], Any]) -> Any:
    with _locked_state(paths):
        state = _read_state_unlocked(paths)
        result = callback(state)
        state["updated_at"] = _utc_now()
        _atomic_json(paths.state, state)
        return result


def _acquire_resource_lock(paths: JobPaths) -> int:
    if fcntl is None:
        raise TrainingJobError("model training jobs require POSIX fcntl locking")
    _secure_directory(paths.run_dir, paths.project)
    descriptor = _secure_open(paths.resource_lock, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        return descriptor
    except BlockingIOError as exc:
        os.close(descriptor)
        raise TrainingConflictError("another model training job is already running") from exc
    except OSError:
        os.close(descriptor)
        raise


def _release_resource_lock(descriptor: int) -> None:
    # Do not call LOCK_UN here: after ``pass_fds`` the worker shares this open
    # file description, and an explicit unlock in the parent would also drop
    # the worker/trainer's protection.  Closing the last inherited descriptor
    # releases the lock naturally.
    os.close(descriptor)


def _resource_lock_is_held(paths: JobPaths) -> bool:
    """Confirm a trainer lifetime even when process-table inspection is stale."""

    if fcntl is None:
        raise TrainingJobError("model training jobs require POSIX fcntl locking")
    if not paths.resource_lock.exists():
        if paths.resource_lock.is_symlink():
            raise TrainingJobError("refusing dangling training resource-lock symlink")
        return False
    descriptor = _secure_open(paths.resource_lock, os.O_RDWR)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            return True
        return False
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _assert_no_symlink_components(path: Path, root: Path) -> None:
    root = root.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise TrainingJobError(f"candidate path escapes project: {path}") from exc
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise TrainingJobError(f"refusing symlink candidate path: {cursor}")


def _model_spec(
    model: str,
    paths: JobPaths,
    python_bin: str | Path | None = None,
    snapshot_path: Path | None = None,
) -> ModelSpec:
    if model not in MODEL_NAMES:
        raise ValueError(f"unknown model: {model!r}")
    python = Path(
        python_bin
        or app_config.get("CS2_TRAINING_PYTHON")
        or paths.project / ".venv" / "bin" / "python"
    ).expanduser()
    if not python.is_absolute():
        python = paths.project / python
    # Preserve the venv launcher path.  Resolving its symlink on macOS would
    # invoke the base Anaconda interpreter and silently lose the project's
    # installed environment.
    python = Path(os.path.abspath(python))
    if not python.is_file() or not os.access(python, os.X_OK):
        raise TrainingJobError(f"training Python is not executable: {python}")
    if snapshot_path is None:
        snapshot_path = paths.snapshot_dir / ("0" * 32 + ".db")
    snapshot_path = _ensure_within(snapshot_path, paths.snapshot_dir, "snapshot")
    job_id = snapshot_path.stem
    if not _JOB_ID_RE.fullmatch(job_id):
        raise TrainingJobError("candidate requires a valid immutable job id")

    if model == "almaz":
        staging_root = paths.project / "models" / "staging"
        candidate = staging_root / "training_jobs" / job_id
        command = (
            str(python),
            str(paths.project / "train_price_model.py"),
            "--db-path",
            str(snapshot_path),
            "--output-dir",
            str(candidate),
            "--test-rows",
            str(RESERVED_TEST_ROWS),
            "--progress",
        )
        required = ("csfloat_price_model.joblib", "csfloat_price_model_metrics.json")
        metrics = "csfloat_price_model_metrics.json"
    elif model == "knives":
        staging_root = paths.project / "KNIFEMODEL" / "staging"
        candidate = staging_root / "training_jobs" / job_id
        command = (
            str(python),
            str(paths.project / "train_knife_price_model.py"),
            "--db-path",
            str(snapshot_path),
            "--output-dir",
            str(candidate),
            "--test-rows",
            str(RESERVED_TEST_ROWS),
            "--progress",
        )
        required = ("knife_price_model.joblib", "knife_price_model_metrics.json")
        metrics = "knife_price_model_metrics.json"
    elif model == "gloves":
        staging_root = paths.project / "GLOVEMODEL" / "staging"
        candidate = staging_root / "training_jobs" / job_id
        command = (
            str(python),
            str(paths.project / "train_glove_price_model.py"),
            "--db-path",
            str(snapshot_path),
            "--output-dir",
            str(candidate),
            "--test-rows",
            str(RESERVED_TEST_ROWS),
            "--progress",
        )
        required = ("glove_price_model.joblib", "glove_price_model_metrics.json")
        metrics = "glove_price_model_metrics.json"
    elif model == "igor":
        staging_root = paths.project / "IGORMODEL" / "staging"
        candidate = staging_root / "training_jobs" / job_id
        command = (
            str(python),
            str(paths.project / "IGORMODEL" / "train.py"),
            "--artifact-dir",
            str(candidate),
            "--rebuild-cache",
            "--test-rows",
            str(RESERVED_TEST_ROWS),
            "--verbose-period",
            "25",
        )
        required = (
            "price_model_point.cbm",
            "price_model_q15.cbm",
            "price_model_q85.cbm",
            "feature_metadata.pkl",
            "igor_metrics.json",
        )
        metrics = "igor_metrics.json"
    else:
        staging_root = paths.project / "NEURALMODEL" / "staging"
        candidate = staging_root / "training_jobs" / job_id
        trainer_source = (paths.project / "train_neural_model.py").read_text(
            encoding="utf-8"
        )
        if (
            "NOVA_TRAINING_CONTRACT_VERSION = 2" not in trainer_source
            or '"--test-rows"' not in trainer_source
        ):
            raise TrainingJobError(
                "Nova v2 time-safe training/runtime contract is not installed yet"
            )
        command = (
            str(python),
            str(paths.project / "train_neural_model.py"),
            "--artifact-dir",
            str(candidate),
            "--rebuild-cache",
            "--test-rows",
            str(RESERVED_TEST_ROWS),
        )
        required = ("nova_price_model.pt", "nova_metadata.json", "nova_metrics.json")
        metrics = "nova_metrics.json"

    _assert_no_symlink_components(candidate, paths.project)
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(staging_root.resolve())
    except ValueError as exc:
        raise TrainingJobError("training candidate escapes its staging root") from exc
    production_dirs = {
        (paths.project / "models").resolve(),
        (paths.project / "IGORMODEL" / "artifacts").resolve(),
        (paths.project / "NEURALMODEL" / "artifacts").resolve(),
        (paths.project / "KNIFEMODEL" / "artifacts").resolve(),
        (paths.project / "GLOVEMODEL" / "artifacts").resolve(),
    }
    if candidate in production_dirs:
        raise TrainingJobError("training candidate unexpectedly points at production")
    if candidate.exists():
        raise TrainingJobError("unique training candidate directory already exists")
    for script in (Path(command[1]),):
        if not script.is_file() or script.is_symlink():
            raise TrainingJobError(f"trusted trainer script is missing or unsafe: {script}")
    return ModelSpec(model, candidate, command, required, metrics)


def _read_metrics(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _finite_metric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _first_metric(
    document: dict[str, Any],
    keys: tuple[str, ...],
) -> tuple[str | None, float | None]:
    for key in keys:
        value = _finite_metric(document.get(key))
        if value is not None:
            return key, value
    return None, None


def _normalized_quality_comparison(
    model: str,
    metrics: dict[str, Any],
) -> dict[str, Any] | None:
    """Normalize trainer-specific evidence into one safe public schema."""

    comparison = metrics.get("shared_holdout_comparison")
    comparison = comparison if isinstance(comparison, dict) else {}
    candidate = comparison.get("staging") or comparison.get("candidate")
    production = comparison.get("production")
    candidate = candidate if isinstance(candidate, dict) else {}
    production = production if isinstance(production, dict) else {}
    test = metrics.get("test")
    test = test if isinstance(test, dict) else {}
    if not comparison and not test:
        return None
    if not candidate:
        candidate = test

    percent_keys = (
        "median_absolute_percentage_error",
        "median_ape_%",
        "median_ape_percent",
    )
    mean_percent_keys = (
        "mean_absolute_percentage_error",
        "mean_ape_%",
        "mean_ape_percent",
    )
    p90_percent_keys = (
        "p90_absolute_percentage_error",
        "p90_ape_%",
        "p90_ape_percent",
    )

    def normalized_pair(
        keys: tuple[str, ...],
        *,
        unit: str,
        lower_is_better: bool,
        fraction_key: str | None = None,
    ) -> dict[str, Any]:
        production_key, production_value = _first_metric(production, keys)
        candidate_key, candidate_value = _first_metric(candidate, keys)
        if production_key == fraction_key and production_value is not None:
            production_value *= 100.0
        if candidate_key == fraction_key and candidate_value is not None:
            candidate_value *= 100.0
        delta = (
            candidate_value - production_value
            if production_value is not None and candidate_value is not None
            else None
        )
        return {
            "production": production_value,
            "candidate": candidate_value,
            "delta": delta,
            "lower_is_better": lower_is_better,
            "unit": unit,
        }

    def usd_pair() -> dict[str, Any]:
        _production_key, production_value = _first_metric(production, ("mae_usd",))
        _candidate_key, candidate_value = _first_metric(candidate, ("mae_usd",))
        if production_value is None:
            _key, cents = _first_metric(production, ("mae_cents",))
            production_value = cents / 100.0 if cents is not None else None
        if candidate_value is None:
            _key, cents = _first_metric(candidate, ("mae_cents",))
            candidate_value = cents / 100.0 if cents is not None else None
        return {
            "production": production_value,
            "candidate": candidate_value,
            "delta": (
                candidate_value - production_value
                if production_value is not None and candidate_value is not None
                else None
            ),
            "lower_is_better": True,
            "unit": "usd",
        }

    production_interval = _finite_metric(
        comparison.get("production_interval_coverage_15_85")
    )
    candidate_interval = _finite_metric(
        comparison.get("staging_interval_coverage_15_85")
    )
    if candidate_interval is None:
        for key in (
            "test_raw_interval_coverage_15_85",
            "test_interval_coverage_15_85_log",
            "test_interval_coverage_15_85",
        ):
            candidate_interval = _finite_metric(metrics.get(key))
            if candidate_interval is not None:
                break
    production_interval = (
        production_interval * 100.0 if production_interval is not None else None
    )
    candidate_interval = (
        candidate_interval * 100.0 if candidate_interval is not None else None
    )
    production_crossing = _finite_metric(
        comparison.get("production_quantile_crossing_rate")
    )
    candidate_crossing = _finite_metric(
        comparison.get("staging_quantile_crossing_rate")
    )
    if candidate_crossing is None:
        candidate_crossing = _finite_metric(
            metrics.get("test_raw_quantile_crossing_rate")
        )
    production_crossing = (
        production_crossing * 100.0
        if production_crossing is not None
        else None
    )
    candidate_crossing = (
        candidate_crossing * 100.0 if candidate_crossing is not None else None
    )
    candidate_row_win = _finite_metric(comparison.get("candidate_row_win_percent"))
    row_win_baseline = 50.0 if candidate_row_win is not None else None

    holdout_rows = comparison.get("holdout_rows", metrics.get("test_rows"))
    try:
        holdout_rows = int(holdout_rows)
    except (TypeError, ValueError):
        holdout_rows = None
    if isinstance(holdout_rows, int) and holdout_rows < 0:
        holdout_rows = None

    improvement_proof = comparison.get("staging_proves_primary_improvement")
    if improvement_proof is True:
        outcome = "improved"
    elif not comparison.get("production_trained_at") and test:
        outcome = "bootstrap"
    elif improvement_proof is False and production and candidate:
        outcome = "not_improved"
    else:
        outcome = "unknown"

    return {
        "item_kind": _model_item_kind(model),
        "holdout_rows": holdout_rows,
        "production_trained_at": comparison.get("production_trained_at"),
        "candidate_trained_at": metrics.get("trained_at"),
        "primary_improvement_proven": improvement_proof is True,
        "outcome": outcome,
        "metrics": {
            "median_ape": normalized_pair(
                percent_keys,
                unit="percent",
                lower_is_better=True,
                fraction_key="median_absolute_percentage_error",
            ),
            "mean_ape": normalized_pair(
                mean_percent_keys,
                unit="percent",
                lower_is_better=True,
                fraction_key="mean_absolute_percentage_error",
            ),
            "p90_ape": normalized_pair(
                p90_percent_keys,
                unit="percent",
                lower_is_better=True,
                fraction_key="p90_absolute_percentage_error",
            ),
            "mae_usd": usd_pair(),
            "rmse_log": normalized_pair(
                ("rmse_log_price", "rmse_log", "rmse"),
                unit="log",
                lower_is_better=True,
            ),
            "r2": normalized_pair(
                ("r2_log_price", "r2_log", "r2"),
                unit="score",
                lower_is_better=False,
            ),
            "interval_coverage": {
                "production": production_interval,
                "candidate": candidate_interval,
                "delta": (
                    candidate_interval - production_interval
                    if production_interval is not None
                    and candidate_interval is not None
                    else None
                ),
                "lower_is_better": False,
                "unit": "percent",
            },
            "quantile_crossing_rate": {
                "production": production_crossing,
                "candidate": candidate_crossing,
                "delta": (
                    candidate_crossing - production_crossing
                    if production_crossing is not None
                    and candidate_crossing is not None
                    else None
                ),
                "lower_is_better": True,
                "unit": "percent",
            },
            "candidate_row_win_percent": {
                "production": row_win_baseline,
                "candidate": candidate_row_win,
                "delta": (
                    candidate_row_win - row_win_baseline
                    if candidate_row_win is not None
                    and row_win_baseline is not None
                    else None
                ),
                "lower_is_better": False,
                "unit": "percent",
            },
        },
    }


def _production_metrics(model: str, paths: JobPaths) -> tuple[Path, dict[str, Any]]:
    legacy_dirs = {
        "almaz": paths.project / "models",
        "igor": paths.project / "IGORMODEL" / "artifacts",
        "nova": paths.project / "NEURALMODEL" / "artifacts",
        "knives": paths.project / "KNIFEMODEL" / "artifacts",
        "gloves": paths.project / "GLOVEMODEL" / "artifacts",
    }
    metrics_names = {
        "almaz": "csfloat_price_model_metrics.json",
        "igor": "igor_metrics.json",
        "nova": "nova_metrics.json",
        "knives": "knife_price_model_metrics.json",
        "gloves": "glove_price_model_metrics.json",
    }
    try:
        production_dir = model_generations.active_or_legacy_bundle_dir(
            model,
            legacy_dirs[model],
            project_dir=paths.project,
            verify_files=False,
        )
    except model_generations.GenerationError as exc:
        raise TrainingJobError(f"active model generation is unsafe: {exc}") from exc
    location = production_dir / metrics_names[model]
    return location, _read_metrics(location)


def _metric_source(metrics: dict[str, Any]) -> dict[str, Any]:
    for key in ("source_database", "source_database_before", "source_database_after"):
        value = metrics.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _metric_training_rows(metrics: dict[str, Any], source: dict[str, Any]) -> int | None:
    # Freshness must compare like with like.  The eligibility SQL below counts
    # every sold, positive-price row in the requested item-kind segment before
    # applying the stricter runtime-reference predicate.  Current Almaz-like
    # fingerprints expose that matching population as source_valid_price_rows;
    # valid_price_rows is the time-safe fit population and would inflate
    # apparent growth whenever old rows lack a usable reference snapshot.
    candidates = (
        source.get("source_valid_price_rows"),
        source.get("valid_price_rows"),
        source.get("sales_rows"),
        metrics.get("rows"),
        metrics.get("total_rows"),
        metrics.get("training_rows"),
    )
    for value in candidates:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    split_values = []
    for key in ("train_rows", "validation_rows", "test_rows"):
        try:
            split_values.append(int(metrics[key]))
        except (KeyError, TypeError, ValueError):
            pass
    return sum(split_values) if split_values and sum(split_values) > 0 else None


def _production_provenance_reasons(
    model: str,
    metrics: dict[str, Any],
    source: dict[str, Any],
) -> list[str]:
    """Reject explicit cross-kind/corrupt provenance while tolerating legacy absence.

    Older installed Igor/Nova generations predate the item_kind field.  Their
    missing declaration remains usable as a one-way compatibility bridge, but
    any present declaration must match the model's fixed kind and any present
    logical fingerprint must be a real lowercase SHA-256 value.
    """

    expected = _model_item_kind(model)
    reasons: list[str] = []
    for declared in (metrics.get("item_kind"), source.get("item_kind")):
        if declared is not None and declared != expected:
            reasons.append("production_item_kind_mismatch")
            break

    declared_policy = source.get("item_kind_policy")
    if declared_policy is not None and declared_policy != STRICT_ITEM_KIND_POLICY:
        reasons.append("production_item_kind_policy_mismatch")

    logical_sha256 = source.get("logical_sha256")
    if logical_sha256 is not None and (
        not isinstance(logical_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", logical_sha256)
    ):
        reasons.append("production_source_fingerprint_invalid")

    metrics_rows = metrics.get("rows")
    fingerprint_rows = source.get("valid_price_rows")
    try:
        parsed_metrics_rows = int(metrics_rows)
        parsed_fingerprint_rows = int(fingerprint_rows)
    except (TypeError, ValueError):
        pass
    else:
        if (
            parsed_metrics_rows > 0
            and parsed_fingerprint_rows > 0
            and parsed_metrics_rows != parsed_fingerprint_rows
        ):
            reasons.append("production_source_row_count_mismatch")
    return reasons


def _legacy_item_kind_unverified(
    metrics: dict[str, Any], source: dict[str, Any]
) -> bool:
    return bool(
        metrics
        and (
            metrics.get("item_kind") is None
            or source.get("item_kind") is None
        )
    )


def _contract_migration_warnings(
    metrics: dict[str, Any], source: dict[str, Any]
) -> list[str]:
    """Describe legacy provenance that must be replaced with a strict model.

    A legacy generation remains available as a serving/baseline compatibility
    bridge, but its old row count and timestamp boundary are not comparable to
    strict item-kind counts.  Treating them as a delta baseline can otherwise
    make retraining impossible after unknown rows are correctly excluded.
    """

    if not metrics:
        return []
    warnings: list[str] = []
    if _legacy_item_kind_unverified(metrics, source):
        warnings.append("production_item_kind_unverified_legacy")
    if source.get("item_kind_policy") is None:
        warnings.append("production_item_kind_policy_unverified_legacy")
    return warnings


@contextmanager
def _readonly_database(path: Path):
    if path.is_symlink() or not path.is_file():
        raise TrainingJobError(f"training database is missing or unsafe: {path}")
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=10,
        )
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise TrainingJobError("cannot inspect the training database") from exc


def get_model_training_eligibility(
    model: str,
    *,
    project_dir: str | Path | None = None,
    _database_counts: tuple[int, int] | None = None,
    _database_error: bool = False,
) -> dict[str, Any]:
    """Return the fail-closed, item-kind-isolated Start-button threshold."""

    if model not in MODEL_NAMES:
        raise ValueError(f"unknown model: {model!r}")
    paths = _paths(project_dir)
    metrics_path, metrics = _production_metrics(model, paths)
    source = _metric_source(metrics)
    contract_warnings = _contract_migration_warnings(metrics, source)
    contract_migration = bool(contract_warnings)
    trained_rows = _metric_training_rows(metrics, source)
    boundary = source.get("max_sold_at") or metrics.get("trained_at")
    boundary_dt = _parse_timestamp(boundary)
    threshold_trained_rows = None if contract_migration else trained_rows
    threshold_boundary_dt = None if contract_migration else boundary_dt
    isolated_model = model in {"knives", "gloves"}
    bootstrap = isolated_model and not metrics
    min_new_rows = (
        (
            KNIFE_BOOTSTRAP_MIN_ROWS
            if model == "knives"
            else GLOVE_BOOTSTRAP_MIN_ROWS
        )
        if bootstrap
        else (
            KNIFE_RETRAIN_MIN_NEW_ROWS
            if model == "knives"
            else GLOVE_RETRAIN_MIN_NEW_ROWS
        )
        if isolated_model
        else MIN_NEW_ROWS
    )
    min_runtime_valid_rows = (
        (
            KNIFE_BOOTSTRAP_MIN_RUNTIME_VALID_ROWS
            if model == "knives"
            else GLOVE_BOOTSTRAP_MIN_RUNTIME_VALID_ROWS
        )
        if bootstrap
        else (
            KNIFE_RETRAIN_MIN_RUNTIME_VALID_ROWS
            if model == "knives"
            else GLOVE_RETRAIN_MIN_RUNTIME_VALID_ROWS
        )
        if isolated_model
        else MIN_RUNTIME_VALID_NEW_ROWS
    )
    provenance_reasons = _production_provenance_reasons(model, metrics, source)
    reasons: list[str] = list(provenance_reasons)
    if not metrics and not bootstrap:
        reasons.append("production_metrics_missing")
    if trained_rows is None and not bootstrap and not contract_migration:
        reasons.append("production_row_count_unknown")
    if boundary_dt is None and not bootstrap and not contract_migration:
        reasons.append("production_boundary_unknown")

    trainer_script = {
        "almaz": paths.project / "train_price_model.py",
        "igor": paths.project / "IGORMODEL" / "train.py",
        "nova": paths.project / "train_neural_model.py",
        "knives": paths.project / "train_knife_price_model.py",
        "gloves": paths.project / "train_glove_price_model.py",
    }[model]
    trainer_available = (
        trainer_script.is_file() and not trainer_script.is_symlink()
    )
    if not trainer_available:
        reasons.append("trainer_unavailable")
    if model == "nova":
        try:
            source_text = trainer_script.read_text(encoding="utf-8")
            trainer_available = trainer_available and (
                "NOVA_TRAINING_CONTRACT_VERSION = 2" in source_text
                and '"--test-rows"' in source_text
            )
        except OSError:
            trainer_available = False
        if not trainer_available:
            reasons.append("nova_v2_contract_pending")

    current_rows = 0
    runtime_valid_new_rows = 0
    database_path = app_config.path("CS2_DB_PATH", "csfloat_history.db")
    if _database_counts is not None:
        current_rows, runtime_valid_new_rows = _database_counts
    elif _database_error:
        reasons.append("database_eligibility_check_failed")
    elif (
        bootstrap
        or contract_migration
        or (
            threshold_trained_rows is not None
            and threshold_boundary_dt is not None
        )
    ):
        try:
            with _readonly_database(database_path) as connection:
                has_sales_time_index = bool(
                    connection.execute(
                        """
                        SELECT 1 FROM sqlite_master
                        WHERE type = 'index' AND name = 'idx_sales_name_sold_at'
                        """
                    ).fetchone()
                )
                sales_source = (
                    "sales INDEXED BY idx_sales_name_sold_at"
                    if has_sales_time_index
                    else "sales"
                )
                item_kind_expression, item_kind_parameters = sql_item_kind_expression(
                    connection,
                    name_expression="sales.name",
                    unknown_as_weapon_skin=False,
                )
                current_rows = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(*) FROM {sales_source}
                        WHERE state = 'sold'
                          AND price_cents IS NOT NULL AND price_cents > 0
                          AND name IS NOT NULL
                          AND ({item_kind_expression}) = ?
                        """,
                        (*item_kind_parameters, _model_item_kind(model)),
                    ).fetchone()[0]
                )
                boundary_clause = (
                    "AND julianday(sold_at) > julianday(?)"
                    if threshold_boundary_dt is not None
                    else ""
                )
                runtime_parameters = [
                    *item_kind_parameters,
                    _model_item_kind(model),
                ]
                if threshold_boundary_dt is not None:
                    runtime_parameters.append(threshold_boundary_dt.isoformat())
                runtime_valid_new_rows = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(*)
                        FROM {sales_source}
                        WHERE state = 'sold'
                          AND price_cents IS NOT NULL AND price_cents > 0
                          AND name IS NOT NULL
                          AND ({item_kind_expression}) = ?
                          {boundary_clause}
                          AND {time_safe_reference_sql()}
                        """,
                        runtime_parameters,
                    ).fetchone()[0]
                )
        except (sqlite3.Error, TrainingJobError):
            reasons.append("database_eligibility_check_failed")

    new_rows = current_rows if bootstrap or contract_migration else (
        max(current_rows - threshold_trained_rows, 0)
        if threshold_trained_rows is not None
        and current_rows >= threshold_trained_rows
        else 0
    )
    growth_percent = (
        100.0
        if contract_migration
        else 100.0
        if bootstrap and current_rows
        else new_rows / threshold_trained_rows * 100.0
        if threshold_trained_rows and current_rows >= threshold_trained_rows
        else 0.0
    )
    if (
        not contract_migration
        and threshold_trained_rows is not None
        and current_rows < threshold_trained_rows
    ):
        reasons.append("database_row_count_mismatch")
    if new_rows < min_new_rows:
        reasons.append("not_enough_new_rows")
    if (
        not bootstrap
        and not contract_migration
        and growth_percent < MIN_DATA_GROWTH_PERCENT
    ):
        reasons.append("not_enough_growth")
    if runtime_valid_new_rows < min_runtime_valid_rows:
        reasons.append("not_enough_runtime_valid_rows")

    reasons = list(dict.fromkeys(reasons))
    return {
        "model": model,
        "item_kind": _model_item_kind(model),
        "bootstrap": bootstrap,
        "eligible": not reasons,
        "reasons": reasons,
        "warnings": contract_warnings,
        "contract_migration": contract_migration,
        "retrain_required": bool(
            contract_migration or provenance_reasons
        ),
        "metrics_file": str(metrics_path.relative_to(paths.project)),
        "trained_rows": trained_rows,
        "threshold_baseline_rows": threshold_trained_rows,
        "current_rows": current_rows,
        "new_rows": new_rows,
        "growth_percent": round(growth_percent, 3),
        "production_boundary": boundary_dt.isoformat() if boundary_dt else None,
        "runtime_valid_new_rows": runtime_valid_new_rows,
        "trainer_available": trainer_available,
        "source_item_kind": source.get("item_kind"),
        "source_item_kind_verified": bool(
            source.get("item_kind") == _model_item_kind(model)
            and source.get("item_kind_policy") == STRICT_ITEM_KIND_POLICY
        ),
        "source_fingerprint_sha256": source.get("logical_sha256"),
        "source_item_kind_policy": source.get("item_kind_policy"),
        "item_kind_policy": STRICT_ITEM_KIND_POLICY,
        "thresholds": {
            "min_new_rows": min_new_rows,
            "min_growth_percent": MIN_DATA_GROWTH_PERCENT,
            "min_runtime_valid_new_rows": min_runtime_valid_rows,
        },
    }


def get_all_training_eligibility(
    *, project_dir: str | Path | None = None
) -> dict[str, dict[str, Any]]:
    """Evaluate every model with one database scan instead of six.

    Dashboard refreshes this summary periodically.  Combining the common
    valid-row count and all three time-safe boundaries avoids repeatedly
    walking the multi-gigabyte sales table.
    """

    paths = _paths(project_dir)
    boundaries: dict[str, datetime] = {}
    for name in MODEL_NAMES:
        _metrics_path, metrics = _production_metrics(name, paths)
        source = _metric_source(metrics)
        boundary = _parse_timestamp(source.get("max_sold_at") or metrics.get("trained_at"))
        if (
            not _contract_migration_warnings(metrics, source)
            and _metric_training_rows(metrics, source) is not None
            and boundary is not None
        ):
            boundaries[name] = boundary

    counts: dict[str, tuple[int, int]] = {}
    database_error = False
    database_path = app_config.path("CS2_DB_PATH", "csfloat_history.db")
    ordered_names = list(MODEL_NAMES)
    try:
        with _readonly_database(database_path) as connection:
            item_kind_expression, item_kind_parameters = sql_item_kind_expression(
                connection,
                name_expression="sales.name",
                unknown_as_weapon_skin=False,
            )
            cases = []
            case_parameters: list[str] = []
            for name in ordered_names:
                boundary = boundaries.get(name)
                date_clause = (
                    "AND julianday(sold_at) > julianday(?)"
                    if boundary is not None
                    else ""
                )
                cases.append(
                    f"""
                    COALESCE(SUM(CASE
                        WHEN item_kind = ?
                         {date_clause}
                         AND {time_safe_reference_sql()}
                        THEN 1 ELSE 0 END), 0)
                    """
                )
                case_parameters.append(_model_item_kind(name))
                if boundary is not None:
                    case_parameters.append(boundary.isoformat())
            row = connection.execute(
                f"""
                WITH eligible AS (
                    SELECT sold_at, reference_last_updated,
                           reference_predicted_price, reference_base_price,
                           {item_kind_expression} AS item_kind
                    FROM sales
                    WHERE state = 'sold'
                      AND price_cents IS NOT NULL AND price_cents > 0
                      AND name IS NOT NULL
                )
                SELECT
                    COALESCE(SUM(item_kind = ?), 0),
                    COALESCE(SUM(item_kind = ?), 0),
                    COALESCE(SUM(item_kind = ?), 0),
                    {", ".join(cases)}
                FROM eligible
                """,
                (
                    *item_kind_parameters,
                    ITEM_KIND_WEAPON_SKIN,
                    ITEM_KIND_KNIFE,
                    ITEM_KIND_GLOVE,
                    *case_parameters,
                ),
            ).fetchone()
        current_by_kind = {
            ITEM_KIND_WEAPON_SKIN: int(row[0]),
            ITEM_KIND_KNIFE: int(row[1]),
            ITEM_KIND_GLOVE: int(row[2]),
        }
        for index, name in enumerate(ordered_names, start=3):
            counts[name] = (
                current_by_kind[_model_item_kind(name)],
                int(row[index]),
            )
    except (sqlite3.Error, TrainingJobError, TypeError, ValueError):
        database_error = True

    return {
        name: get_model_training_eligibility(
            name,
            project_dir=paths.project,
            _database_counts=counts.get(name),
            _database_error=database_error,
        )
        for name in MODEL_NAMES
    }


def _snapshot_path(paths: JobPaths, job_id: str) -> Path:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise TrainingJobError("invalid snapshot job identity")
    _secure_directory(paths.snapshot_dir, paths.project)
    return _ensure_within(paths.snapshot_dir / f"{job_id}.db", paths.snapshot_dir, "snapshot")


def _create_database_snapshot(
    paths: JobPaths,
    model: str,
    job_id: str,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[Path, int]:
    """Create an atomic, read-only SQLite backup for one training job."""

    source_path = app_config.path("CS2_DB_PATH", "csfloat_history.db")
    if source_path.is_symlink() or not source_path.is_file():
        raise TrainingJobError(f"training database is missing or unsafe: {source_path}")
    destination = _snapshot_path(paths, job_id)
    if destination.exists() or destination.is_symlink():
        raise TrainingJobError("training snapshot path already exists")
    free_bytes = shutil.disk_usage(paths.snapshot_dir).free
    required_bytes = max(
        source_path.stat().st_size + 512 * 1024 * 1024,
        MODEL_MIN_FREE_DISK_BYTES[model],
    )
    if free_bytes < required_bytes:
        raise TrainingJobError(
            f"not enough free space for immutable training snapshot "
            f"(need {required_bytes}, have {free_bytes})"
        )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{job_id}.", suffix=".db.tmp", dir=paths.snapshot_dir
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    last_reported = -1

    def progress(_status: int, remaining: int, total: int) -> None:
        nonlocal last_reported
        if cancel_check is not None and cancel_check():
            raise TrainingCancelled("training cancelled while creating snapshot")
        completed = 0 if total <= 0 else int((total - remaining) / total * 100)
        if completed < last_reported + 5 and remaining:
            return
        last_reported = completed
        _update_worker_job(
            paths,
            model,
            job_id,
            phase="creating_snapshot",
            progress_percent=round(1.0 + min(max(completed, 0), 100) * 0.04, 1),
            eta_seconds=int(
                MODEL_ESTIMATED_TOTAL_SECONDS[model]
                * (1.0 - min(max(completed, 0), 100) / 100.0 * 0.05)
            ),
            heartbeat_at=_utc_now(),
            snapshot_state="creating",
        )

    try:
        source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30) as source:
            source.execute("PRAGMA query_only=ON")
            with sqlite3.connect(temporary, timeout=30) as target:
                source.backup(target, pages=16_384, progress=progress, sleep=0.05)
                check = target.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise TrainingJobError("immutable training snapshot failed quick_check")
                rows = int(
                    target.execute(
                        """
                        SELECT COUNT(*) FROM sales
                        WHERE state = 'sold' AND price_cents > 0 AND name IS NOT NULL
                        """
                    ).fetchone()[0]
                )
        os.chmod(temporary, 0o400)
        os.replace(temporary, destination)
        _fsync_directory(paths.snapshot_dir)
        _update_worker_job(
            paths,
            model,
            job_id,
            phase="snapshot_ready",
            progress_percent=5.0,
            heartbeat_at=_utc_now(),
            snapshot_state="ready",
        )
        return destination, rows
    except (OSError, sqlite3.Error) as exc:
        raise TrainingJobError("could not create immutable training snapshot") from exc
    finally:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)


def _delete_snapshot(paths: JobPaths, job_id: str) -> None:
    snapshot = _snapshot_path(paths, job_id)
    if snapshot.is_symlink():
        raise TrainingJobError("refusing to remove symlink snapshot")
    if snapshot.exists():
        try:
            os.chmod(snapshot, 0o600)
            snapshot.unlink()
            _fsync_directory(paths.snapshot_dir)
        except OSError as exc:
            raise TrainingJobError("cannot remove completed training snapshot") from exc


def _candidate_roots(paths: JobPaths) -> dict[str, Path]:
    return {
        "almaz": paths.project / "models" / "staging" / "training_jobs",
        "igor": paths.project / "IGORMODEL" / "staging" / "training_jobs",
        "nova": paths.project / "NEURALMODEL" / "staging" / "training_jobs",
        "knives": paths.project / "KNIFEMODEL" / "staging" / "training_jobs",
        "gloves": paths.project / "GLOVEMODEL" / "staging" / "training_jobs",
    }


def _remove_candidate_tree(path: Path, root: Path) -> None:
    if path.parent != root or path.is_symlink() or not path.is_dir():
        raise TrainingJobError(f"refusing unsafe candidate cleanup target: {path}")
    for directory, names, filenames in os.walk(path, topdown=True, followlinks=False):
        directory_path = Path(directory)
        info = directory_path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise TrainingJobError(f"unsafe directory in candidate cleanup: {directory_path}")
        for name in [*names, *filenames]:
            child = directory_path / name
            child_info = child.lstat()
            if stat.S_ISLNK(child_info.st_mode) or child_info.st_uid != os.getuid():
                raise TrainingJobError(f"unsafe entry in candidate cleanup: {child}")
    for directory, _names, filenames in os.walk(path, topdown=False):
        directory_path = Path(directory)
        for filename in filenames:
            os.chmod(directory_path / filename, 0o600)
        os.chmod(directory_path, 0o700)
    shutil.rmtree(path)
    _fsync_directory(root)


def cleanup_training_candidates(
    *, project_dir: str | Path | None = None
) -> dict[str, list[str]]:
    """Bound staging to active/pending jobs plus one latest failed job per model."""

    paths = _paths(project_dir)
    with _locked_state(paths):
        state = _read_state_unlocked(paths)
    keep: dict[str, set[str]] = {name: set() for name in MODEL_NAMES}
    for model, job in state["models"].items():
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
            continue
        if job.get("state") in ACTIVE_STATES or job.get("state") in {
            "rejected",
            "failed",
            "cancelled",
            "interrupted",
        }:
            keep[model].add(job_id)
    try:
        from model_training_activation import get_pending_activations

        pending = get_pending_activations(project_dir=paths.project)
    except Exception as exc:
        raise TrainingJobError(
            "cannot prove which staging candidates are pending activation"
        ) from exc
    for model, record in pending.items():
        job_id = record.get("job_id")
        if isinstance(job_id, str) and _JOB_ID_RE.fullmatch(job_id):
            keep[model].add(job_id)

    removed = {name: [] for name in MODEL_NAMES}
    for model, root in _candidate_roots(paths).items():
        if not root.exists() and not root.is_symlink():
            continue
        _assert_no_symlink_components(root, paths.project)
        root_info = root.lstat()
        if root.is_symlink() or not root.is_dir():
            raise TrainingJobError(f"candidate retention root is unsafe: {root}")
        if root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) & 0o022:
            raise TrainingJobError(f"candidate retention root is not private: {root}")
        for entry in sorted(root.iterdir(), key=lambda value: value.name):
            if not _JOB_ID_RE.fullmatch(entry.name) or entry.name in keep[model]:
                continue
            _remove_candidate_tree(entry, root)
            removed[model].append(entry.name)
    return removed


def _duration_seconds(value: str) -> float | None:
    value = value.strip().lower()
    total = 0.0
    matches = re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*(ms|us|h|m|s)", value)
    if not matches:
        return None
    for number, unit in matches:
        total += float(number) * {
            "h": 3600,
            "m": 60,
            "s": 1,
            "ms": 0.001,
            "us": 0.000001,
        }[unit]
    return total


class ProgressTracker:
    """Translate trusted trainer output into monotonic percent/ETA updates."""

    def __init__(self, model: str, started_monotonic: float | None = None):
        if model not in MODEL_NAMES:
            raise ValueError(f"unknown model: {model}")
        self.model = model
        self.started = started_monotonic if started_monotonic is not None else time.monotonic()
        self.percent = 1.0
        self.phase = "preparing"
        self.explicit_eta: float | None = None
        self.igor_component = "point"
        self.nova_epochs = 18

    def eta_seconds(self) -> int:
        elapsed = max(time.monotonic() - self.started, 0.0)
        baseline = max(float(MODEL_ESTIMATED_TOTAL_SECONDS[self.model]) - elapsed, 0.0)
        # CatBoost's phase ETA is extrapolated to all remaining weighted
        # phases above.  Let a slower measured phase extend the baseline.
        return int(max(baseline, float(self.explicit_eta or 0.0)))

    def feed(self, line: str) -> tuple[str, float, int | None]:
        lower = line.strip().lower()
        percent = self.percent
        phase = self.phase

        if self.model in {"almaz", "knives", "gloves"}:
            if "building features" in lower:
                phase, percent = "building_features", 8
            elif "training point model" in lower:
                phase, percent = "training_point", 25
            elif "training q15 model" in lower:
                phase, percent = "training_low_interval", 50
            elif "training q85 model" in lower:
                phase, percent = "training_high_interval", 74
            elif "comparing production" in lower:
                phase, percent = "quality_check", 92
            elif "saved staging metrics" in lower:
                phase, percent = "saving_candidate", 98
            else:
                match = _SKLEARN_ITERATION_RE.match(line)
                if match:
                    iteration, total = int(match.group(1)), max(int(match.group(2)), 1)
                    base, span = {
                        "training_point": (25.0, 24.0),
                        "training_low_interval": (50.0, 23.0),
                        "training_high_interval": (74.0, 17.0),
                    }.get(self.phase, (self.percent, 0.0))
                    percent = base + span * min(iteration, total) / total
        elif self.model == "igor":
            if "data_loading:" in lower:
                phase, percent = "building_cache", 7
            elif lower.startswith("features:"):
                phase, percent = "building_features", 13
            elif "кэш сохранён" in lower or "cache saved" in lower:
                phase, percent = "verifying_cache", 18
            elif lower.startswith("[cache]"):
                phase, percent = "loading_cache", 20
            elif lower.startswith("[split]"):
                self.igor_component = "point"
                self.explicit_eta = None
                phase, percent = "training_point", 22
            elif lower.startswith("[point]"):
                self.igor_component = "q15"
                self.explicit_eta = None
                phase, percent = "training_low_interval", 51
            elif lower.startswith("[q15]"):
                self.igor_component = "q85"
                self.explicit_eta = None
                phase, percent = "training_high_interval", 73
            elif lower.startswith("[q85]"):
                self.explicit_eta = None
                phase, percent = "quality_check", 94
            else:
                match = _CATBOOST_ITERATION_RE.match(line)
                if match:
                    iteration = int(match.group(1))
                    base, span, total = {
                        "point": (22.0, 28.0, 2500),
                        "q15": (51.0, 21.0, 2000),
                        "q85": (73.0, 20.0, 2000),
                    }[self.igor_component]
                    percent = base + span * min(iteration + 1, total) / total
                    phase_eta = _duration_seconds(match.group(2))
                    phase_percent_left = max(base + span - percent, 0.1)
                    self.explicit_eta = (
                        phase_eta
                        * max(100.0 - percent, 0.1)
                        / phase_percent_left
                        if phase_eta is not None
                        else None
                    )
        else:
            if "data_loading:" in lower:
                phase, percent = "building_cache", 7
            elif lower.startswith("features:"):
                phase, percent = "building_features", 13
            elif "кэш сохранён" in lower or "cache saved" in lower:
                phase, percent = "loading_cache", 16
            elif lower.startswith("rows train="):
                phase, percent = "preparing_features", 20
            elif lower.startswith("vocab "):
                phase, percent = "building_vocabularies", min(max(percent + 0.3, 21), 25)
            elif "encoding arrays" in lower:
                phase, percent = "encoding_arrays", 26
            elif lower.startswith("parameters:"):
                phase, percent = "training_epochs", 29
            elif lower.startswith("early stopping"):
                phase, percent = "quality_check", 90
            elif lower.startswith("interval coverage"):
                phase, percent = "saving_candidate", 97
            elif "saved staging candidate" in lower:
                phase, percent = "saving_candidate", 99
            else:
                match = _NOVA_EPOCH_RE.match(line)
                if match:
                    epoch = int(match.group(1))
                    phase = "training_epochs"
                    percent = 29 + 60 * min(epoch, self.nova_epochs) / self.nova_epochs

        self.phase = phase
        self.percent = min(99.0, max(self.percent, float(percent)))
        return self.phase, round(self.percent, 1), self.eta_seconds()


def _log_path(paths: JobPaths, model: str, job_id: str) -> Path:
    if model not in MODEL_NAMES or not _JOB_ID_RE.fullmatch(job_id):
        raise TrainingJobError("invalid log identity")
    _secure_directory(paths.log_dir, paths.project)
    return _ensure_within(paths.log_dir / f"{model}-{job_id}.log", paths.log_dir, "log")


def _read_log_tail(paths: JobPaths, model: str, job_id: str) -> list[str]:
    path = _log_path(paths, model, job_id)
    if not path.exists():
        return []
    if path.is_symlink():
        raise TrainingJobError("refusing symlink training log")
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - MAX_LOG_TAIL_BYTES))
            raw = handle.read(MAX_LOG_TAIL_BYTES)
    except OSError as exc:
        raise TrainingJobError("cannot read training log") from exc
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > MAX_LOG_TAIL_BYTES and lines:
        lines = lines[1:]
    return [value for line in lines[-MAX_LOG_TAIL_LINES:] if (value := _redact(line))]


def _process_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _process_command(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    command = result.stdout.strip()
    return command if result.returncode == 0 and command else None


def _process_start_token(pid: int) -> str | None:
    """Return an OS process-birth token to defend cancellation from PID reuse."""

    proc_stat = Path("/proc") / str(pid) / "stat"
    try:
        if proc_stat.is_file():
            fields = proc_stat.read_text(encoding="utf-8").split()
            return f"proc:{fields[21]}" if len(fields) > 21 else None
    except OSError:
        return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join(result.stdout.split())
    return f"ps:{value}" if result.returncode == 0 and value else None


def _pid_matches(job: dict[str, Any], kind: str) -> bool:
    pid = job.get(f"{kind}_pid")
    if not _process_alive(pid):
        return False
    command = _process_command(pid)
    if command is None:
        return False
    expected_start = job.get(f"{kind}_start_token")
    if not expected_start or _process_start_token(pid) != expected_start:
        return False
    if kind == "worker":
        return (
            "model_training_jobs.py" in command
            and "--worker" in command
            and str(job["job_id"]) in command
        )
    script = {
        "almaz": "train_price_model.py",
        "igor": "IGORMODEL/train.py",
        "nova": "train_neural_model.py",
        "knives": "train_knife_price_model.py",
        "gloves": "train_glove_price_model.py",
    }[job["model"]]
    return script in command or (
        "model_training_jobs.py" in command
        and "--trainer-gate" in command
        and str(job["job_id"]) in command
    )


def _recover_state_unlocked(
    state: dict[str, Any], paths: JobPaths | None = None
) -> bool:
    active_id = state.get("active_job_id")
    if not active_id:
        return False
    job = next(
        (value for value in state["models"].values() if value.get("job_id") == active_id),
        None,
    )
    if job is None or job.get("state") not in ACTIVE_STATES:
        state["active_job_id"] = None
        return True
    if paths is not None and _resource_lock_is_held(paths):
        return False
    if job.get("state") == "starting" and job.get("worker_pid") is None:
        started = _parse_timestamp(job.get("started_at"))
        if started is not None:
            age = (datetime.now(timezone.utc) - started).total_seconds()
            if age < STARTUP_RECOVERY_GRACE_SECONDS:
                return False
    if _pid_matches(job, "worker") or _pid_matches(job, "trainer"):
        return False
    now = _utc_now()
    if paths is not None and job.get("candidate_bundle_sha256"):
        try:
            pointer = model_generations.read_active_pointer(
                job["model"], project_dir=paths.project
            )
        except model_generations.GenerationError:
            pointer = None
        if (
            pointer is not None
            and pointer.get("job_id") == job.get("job_id")
            and pointer.get("generation_id") == job.get("candidate_bundle_sha256")
        ):
            job.update(
                {
                    "state": "succeeded",
                    "phase": "active",
                    "progress_percent": 100.0,
                    "eta_seconds": None,
                    "updated_at": now,
                    "heartbeat_at": now,
                    "finished_at": now,
                    "error": None,
                    "activation_state": "activated",
                    "active_bundle_version": pointer["generation_id"],
                    "activation_error": None,
                    "rollback_error": None,
                }
            )
            state["active_job_id"] = None
            if paths is not None and job.get("job_id"):
                try:
                    _delete_snapshot(paths, job["job_id"])
                    job["snapshot_state"] = "removed"
                except TrainingJobError:
                    job["snapshot_state"] = "cleanup_failed"
            return True
    cancelled = bool(job.get("cancel_requested"))
    job.update(
        {
            "state": "cancelled" if cancelled else "interrupted",
            "phase": "cancelled" if cancelled else "interrupted",
            "eta_seconds": None,
            "updated_at": now,
            "heartbeat_at": now,
            "finished_at": now,
            "error": (
                None
                if cancelled
                else "training process disappeared; candidate was not promoted"
            ),
        }
    )
    state["active_job_id"] = None
    if paths is not None and job.get("job_id"):
        try:
            _delete_snapshot(paths, job["job_id"])
            job["snapshot_state"] = "removed"
        except TrainingJobError:
            job["snapshot_state"] = "cleanup_failed"
    return True


def recover_training_jobs(*, project_dir: str | Path | None = None) -> dict[str, Any]:
    paths = _paths(project_dir)

    def recover(state: dict[str, Any]) -> None:
        _recover_state_unlocked(state, paths)

    _mutate_state(paths, recover)
    try:
        cleanup_training_candidates(project_dir=paths.project)
    except TrainingJobError as exc:
        cleanup_error = _redact(exc, limit=512)

        def record_cleanup_error(state: dict[str, Any]) -> None:
            for job in state["models"].values():
                if job.get("job_id") and job.get("state") in TERMINAL_STATES:
                    job["retention_error"] = cleanup_error

        _mutate_state(paths, record_cleanup_error)
    return get_training_overview(project_dir=paths.project, recover=False)


def _public_job(
    paths: JobPaths,
    job: dict[str, Any],
    pending_activations: dict[str, dict[str, Any]],
    activation_statuses: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = {
        key: value
        for key, value in job.items()
        if key
        not in {
            "worker_pid",
            "worker_start_token",
            "trainer_pid",
            "trainer_start_token",
            "snapshot_name",
            "legacy_error",
        }
    }
    result["item_kind"] = _model_item_kind(job["model"])
    active_metrics: dict[str, Any] = {}
    try:
        _metrics_path, active_metrics = _production_metrics(job["model"], paths)
    except (TrainingJobError, ValueError, TypeError):
        # Status must remain available while an installation is incomplete or
        # its active generation is unsafe.  Eligibility performs the blocking
        # check; this public view only reports evidence that could be read.
        active_metrics = {}
    active_source = _metric_source(active_metrics)
    contract_warnings = _contract_migration_warnings(active_metrics, active_source)
    provenance_reasons = _production_provenance_reasons(
        job["model"], active_metrics, active_source
    )
    result["warnings"] = contract_warnings
    result["contract_migration"] = bool(contract_warnings)
    result["retrain_required"] = bool(
        contract_warnings or provenance_reasons
    )
    quality_comparison = job.get("quality_comparison")
    if quality_comparison is None and job.get("state") == "succeeded":
        try:
            quality_comparison = _normalized_quality_comparison(
                job["model"], active_metrics
            )
        except (ValueError, TypeError):
            quality_comparison = None
    result["quality_comparison"] = quality_comparison
    result["can_cancel"] = job.get("state") in ACTIVE_STATES
    pending = pending_activations.get(job["model"])
    pending_matches = bool(
        isinstance(pending, dict)
        and pending.get("job_id") == job.get("job_id")
        and pending.get("candidate_bundle_sha256")
        == job.get("candidate_bundle_sha256")
    )
    result["candidate_ready_for_review"] = bool(job.get("state") == "succeeded" and pending_matches)
    result["pending_activation"] = pending_matches
    active = (activation_statuses or {}).get(job["model"], {})
    stored_activation = str(job.get("activation_state") or "none")
    has_live_activation_status = bool(
        activation_statuses is not None and job["model"] in activation_statuses
    )
    current_version = (
        active.get("active_bundle_version")
        if has_live_activation_status
        else job.get("active_bundle_version")
    )
    failure = active.get("last_activation_failure")
    failure_matches = bool(
        isinstance(failure, dict)
        and failure.get("failed_generation_id")
        in {job.get("candidate_bundle_sha256"), job.get("active_bundle_version")}
    )
    if pending_matches:
        public_activation = "pending_restart"
    elif failure_matches and failure.get("rollback_error"):
        public_activation = "rollback_failed"
    elif failure_matches and current_version != failure.get("failed_generation_id"):
        public_activation = "rolled_back"
    elif stored_activation == "none" and current_version:
        public_activation = "activated"
    else:
        public_activation = stored_activation
    result["activation_state"] = public_activation
    result["active_bundle_version"] = current_version
    result["active_generation_activated_at"] = active.get("activated_at")
    result["activation_error"] = (
        failure.get("error") if failure_matches else job.get("activation_error")
    )
    result["rollback_error"] = (
        failure.get("rollback_error") if failure_matches else job.get("rollback_error")
    )
    if failure_matches and failure.get("retention_error"):
        result["retention_error"] = failure["retention_error"]
    result["promotion_allowed"] = False
    result["log_tail"] = (
        _read_log_tail(paths, job["model"], job["job_id"])
        if job.get("job_id")
        else []
    )
    return result


def get_training_overview(
    *,
    project_dir: str | Path | None = None,
    recover: bool = True,
) -> dict[str, Any]:
    paths = _paths(project_dir)
    recovered_state = False
    with _locked_state(paths):
        state = _read_state_unlocked(paths)
        if recover and _recover_state_unlocked(state, paths):
            recovered_state = True
            state["updated_at"] = _utc_now()
            _atomic_json(paths.state, state)
    if recovered_state:
        retention_errors = []
        try:
            cleanup_training_candidates(project_dir=paths.project)
        except TrainingJobError as exc:
            retention_errors.append(str(exc))
        for name in MODEL_NAMES:
            try:
                model_generations.cleanup_generations(
                    name, project_dir=paths.project
                )
            except model_generations.GenerationError as exc:
                retention_errors.append(f"{name}: {exc}")
        if retention_errors:
            message = _redact("; ".join(retention_errors), limit=512)

            def record_recovery_retention_error(payload: dict[str, Any]) -> None:
                for job in payload["models"].values():
                    if job.get("job_id") and job.get("state") in TERMINAL_STATES:
                        job["retention_error"] = message

            _mutate_state(paths, record_recovery_retention_error)
        with _locked_state(paths):
            state = _read_state_unlocked(paths)
    try:
        from model_training_activation import get_pending_activations

        pending_activations = get_pending_activations(project_dir=paths.project)
    except Exception as exc:
        raise TrainingJobError("pending model activation state is unavailable") from exc
    try:
        activation_statuses = {
            name: model_generations.get_activation_status(
                name, project_dir=paths.project
            )
            for name in MODEL_NAMES
        }
    except model_generations.GenerationError as exc:
        raise TrainingJobError("hot model activation state is unavailable") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": state["updated_at"],
        "active_job_id": state["active_job_id"],
        "training_active": state["active_job_id"] is not None,
        "models": {
            name: _public_job(
                paths,
                state["models"][name],
                pending_activations,
                activation_statuses,
            )
            for name in MODEL_NAMES
        },
        "promotion_policy": "quality_gate_then_hot_activation",
    }


def get_model_training_status(
    model: str,
    *,
    project_dir: str | Path | None = None,
) -> dict[str, Any]:
    if model not in MODEL_NAMES:
        raise ValueError(f"unknown model: {model!r}")
    return get_training_overview(project_dir=project_dir)["models"][model]


def _new_job(
    model: str,
    job_id: str,
    spec: ModelSpec,
    project: Path,
) -> dict[str, Any]:
    now = _utc_now()
    return {
        "job_id": job_id,
        "model": model,
        "state": "starting",
        "phase": "preparing",
        "progress_percent": 1.0,
        "eta_seconds": MODEL_ESTIMATED_TOTAL_SECONDS[model],
        "created_at": now,
        "started_at": now,
        "updated_at": now,
        "heartbeat_at": now,
        "finished_at": None,
        "worker_pid": None,
        "worker_start_token": None,
        "trainer_pid": None,
        "trainer_start_token": None,
        "exit_code": None,
        "error": None,
        "rejection_kind": None,
        "cancel_requested": False,
        "candidate_dir": str(spec.candidate_dir.relative_to(project)),
        "snapshot_name": f"{job_id}.db",
        "snapshot_state": "pending",
        "snapshot_rows": None,
        "quality_gate": None,
        "quality_comparison": None,
        "candidate_bundle_sha256": None,
        "activation_state": "none",
        "active_bundle_version": None,
        "activation_error": None,
        "rollback_error": None,
        "retention_error": None,
    }


def start_model_training(
    model: str,
    *,
    project_dir: str | Path | None = None,
    python_bin: str | Path | None = None,
) -> dict[str, Any]:
    """Start one whitelisted staging trainer and return its public job state."""

    paths = _paths(project_dir)
    eligibility = get_model_training_eligibility(model, project_dir=paths.project)
    if not eligibility["eligible"]:
        reason = ", ".join(eligibility["reasons"])
        raise TrainingJobError(f"model is not eligible for retraining: {reason}")
    try:
        from model_training_activation import get_pending_activations

        pending_activations = get_pending_activations(project_dir=paths.project)
    except Exception as exc:
        raise TrainingJobError(
            "pending model activation state is unavailable; refusing to retrain"
        ) from exc
    if model in pending_activations:
        raise TrainingConflictError(
            "this model already has a quality-gated update waiting for restart"
        )
    job_id = uuid.uuid4().hex
    snapshot = _snapshot_path(paths, job_id)
    spec = _model_spec(model, paths, python_bin, snapshot)
    _secure_directory(paths.log_dir, paths.project)
    resource_fd = _acquire_resource_lock(paths)
    worker: subprocess.Popen[Any] | None = None
    try:
        def prepare(state: dict[str, Any]) -> None:
            _recover_state_unlocked(state, paths)
            if state.get("active_job_id"):
                raise TrainingConflictError("another model training job is active")
            state["models"][model] = _new_job(
                model, job_id, spec, paths.project
            )
            state["active_job_id"] = job_id

        _mutate_state(paths, prepare)
        worker_command = [
            str(spec.command[0]),
            str(Path(__file__).resolve()),
            "--worker",
            model,
            job_id,
            str(resource_fd),
            str(paths.project),
        ]
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        environment["CS2_TRAINING_INTERNAL_WORKER"] = "1"
        worker = subprocess.Popen(
            worker_command,
            cwd=str(paths.project),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            pass_fds=(resource_fd,),
            start_new_session=True,
            env=environment,
        )

        def record_worker(state: dict[str, Any]) -> None:
            job = state["models"][model]
            if job.get("job_id") != job_id:
                raise TrainingJobError("training job was replaced during startup")
            job["worker_pid"] = worker.pid
            job["worker_start_token"] = _process_start_token(worker.pid)
            if not job["worker_start_token"]:
                raise TrainingJobError("cannot record worker process birth token")
            job["updated_at"] = _utc_now()

        _mutate_state(paths, record_worker)
    except Exception as exc:
        error_message = _redact(exc, limit=512)
        if worker is not None and worker.poll() is None:
            worker.terminate()

        def fail_start(state: dict[str, Any]) -> None:
            job = state["models"][model]
            if job.get("job_id") == job_id:
                now = _utc_now()
                job.update(
                    {
                        "state": "failed",
                        "phase": "startup_failed",
                        "finished_at": now,
                        "updated_at": now,
                        "heartbeat_at": now,
                        "error": error_message,
                    }
                )
                state["active_job_id"] = None

        try:
            _mutate_state(paths, fail_start)
        except TrainingJobError:
            pass
        raise
    finally:
        # The worker inherited this locked open-file description.  Closing the
        # dashboard copy does not release the lock while worker/trainer lives.
        _release_resource_lock(resource_fd)
    return get_model_training_status(model, project_dir=paths.project)


def cancel_model_training(
    model: str,
    *,
    job_id: str | None = None,
    project_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Request cancellation, refusing to signal a reused/unverified PID."""

    if model not in MODEL_NAMES:
        raise ValueError(f"unknown model: {model!r}")
    paths = _paths(project_dir)
    signal_target: tuple[str, int] | None = None
    pending_job_id: str | None = None

    def mark(state: dict[str, Any]) -> None:
        nonlocal pending_job_id, signal_target
        job = state["models"][model]
        if job_id is not None and job.get("job_id") != job_id:
            raise TrainingConflictError("training job id no longer matches")
        if job.get("state") not in ACTIVE_STATES:
            raise TrainingConflictError("model training is not active")
        if job.get("state") == "starting" and job.get("worker_pid") is None:
            signal_target = ("pending", 0)
        elif _pid_matches(job, "worker"):
            signal_target = ("worker", job["worker_pid"])
        elif _pid_matches(job, "trainer"):
            signal_target = ("trainer", job["trainer_pid"])
        else:
            raise TrainingJobError("refusing to signal an unverified or reused PID")
        pending_job_id = str(job["job_id"])
        now = _utc_now()
        job.update(
            {
                "state": "cancelling",
                "phase": "cancelling",
                "cancel_requested": True,
                "updated_at": now,
                "heartbeat_at": now,
                "eta_seconds": None,
            }
        )

    _mutate_state(paths, mark)
    kind, pid = signal_target  # type: ignore[misc]
    try:
        from model_training_activation import discard_pending_activation

        discard_pending_activation(
            model, pending_job_id or "", project_dir=paths.project
        )
    except Exception as exc:
        raise TrainingJobError(
            "cancellation was recorded but its pending activation could not be cleared"
        ) from exc
    try:
        if kind == "pending":
            pass
        elif kind == "worker":
            os.kill(pid, signal.SIGTERM)
        else:
            os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        recover_training_jobs(project_dir=paths.project)
    except OSError as exc:
        raise TrainingJobError("could not signal the verified training process") from exc
    return get_model_training_status(model, project_dir=paths.project)


def _validate_candidate(spec: ModelSpec, started_at: str) -> dict[str, Any]:
    started = _parse_timestamp(started_at)
    if started is None:
        raise CandidateIntegrityRejectedError("training start timestamp is invalid")
    try:
        _assert_no_symlink_components(
            spec.candidate_dir, spec.candidate_dir.parents[2]
        )
    except TrainingJobError as exc:
        raise CandidateIntegrityRejectedError(str(exc)) from exc
    for name in spec.required_files:
        path = spec.candidate_dir / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise CandidateIntegrityRejectedError(
                f"staging candidate is missing {name}"
            )
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified < started.replace(microsecond=0):
            raise CandidateIntegrityRejectedError(
                f"staging candidate file was not refreshed: {name}"
            )
    metrics_path = spec.candidate_dir / spec.metrics_file
    try:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateIntegrityRejectedError("staging metrics are invalid") from exc
    if metrics.get("stage") != "staging" or metrics.get("production_promoted") is not False:
        raise CandidateIntegrityRejectedError(
            "trainer did not produce an unpromoted staging candidate"
        )
    model_name = metrics.get("model_name", spec.name)
    if model_name != spec.name:
        raise CandidateIntegrityRejectedError(
            "staging metrics belong to another model"
        )
    quality_comparison = _normalized_quality_comparison(spec.name, metrics)
    try:
        if spec.name == "igor":
            from IGORMODEL.promote_candidate import validate_candidate

            manifest = validate_candidate(spec.candidate_dir)
        else:
            from model_candidate_promotion import validate_candidate

            manifest = validate_candidate(spec.name, spec.candidate_dir)
    except Exception as exc:
        if getattr(exc, "classification", None) == "quality":
            raise CandidateQualityRejectedError(
                f"candidate quality gate rejected {spec.name}: {exc}",
                quality_comparison=quality_comparison,
            ) from exc
        raise CandidateIntegrityRejectedError(
            f"candidate integrity check rejected {spec.name}: {exc}",
            quality_comparison=quality_comparison,
        ) from exc
    if not isinstance(manifest.get("quality_gate"), dict):
        raise CandidateIntegrityRejectedError(
            "candidate validator returned no quality gate"
        )
    bundle_sha = manifest.get("bundle_sha256")
    if not isinstance(bundle_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", bundle_sha):
        raise CandidateIntegrityRejectedError(
            "candidate validator returned no bundle SHA-256"
        )
    if quality_comparison is not None:
        manifest = dict(manifest)
        manifest["quality_comparison"] = quality_comparison
    return manifest


def _update_worker_job(
    paths: JobPaths,
    model: str,
    job_id: str,
    **changes: Any,
) -> bool:
    updated = False

    def mutate(state: dict[str, Any]) -> None:
        nonlocal updated
        job = state["models"][model]
        if job.get("job_id") != job_id:
            return
        if "progress_percent" in changes:
            changes["progress_percent"] = max(
                float(job.get("progress_percent") or 0),
                float(changes["progress_percent"]),
            )
        job.update(changes)
        job["updated_at"] = _utc_now()
        updated = True

    _mutate_state(paths, mutate)
    return updated


def _job_cancel_requested(paths: JobPaths, model: str, job_id: str) -> bool:
    with _locked_state(paths):
        state = _read_state_unlocked(paths)
    job = state["models"][model]
    return job.get("job_id") != job_id or bool(job.get("cancel_requested"))


def _terminate_group(pid: int, *, force: bool = False) -> None:
    try:
        os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return


def _stop_and_reap(
    process: subprocess.Popen[Any],
    *,
    grace_seconds: float = WORKER_CANCEL_GRACE_SECONDS,
) -> bool:
    """Stop a child process group and reap it before destructive cleanup."""

    if process.poll() is not None:
        return True
    _terminate_group(process.pid)
    deadline = time.monotonic() + max(float(grace_seconds), 0.0)
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if process.poll() is None:
        _terminate_group(process.pid, force=True)
    try:
        process.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return process.poll() is not None


def _run_trainer_gate(
    model: str,
    job_id: str,
    resource_fd: int,
    project_dir: Path,
    gate_fd: int,
) -> int:
    """Exec the trainer only after its PID is durably recorded by the worker.

    If the worker dies in the Popen-to-state-write window, its write side of
    the pipe closes and this process exits without ever starting a trainer.
    """

    if os.environ.get("CS2_TRAINING_INTERNAL_WORKER") != "1":
        raise TrainingJobError("trainer gate mode is internal")
    if model not in MODEL_NAMES or not _JOB_ID_RE.fullmatch(job_id):
        raise TrainingJobError("invalid trainer gate identity")
    try:
        os.fstat(resource_fd)
        os.fstat(gate_fd)
        release = os.read(gate_fd, 1)
    except OSError as exc:
        raise TrainingJobError("trainer gate descriptors are invalid") from exc
    finally:
        try:
            os.close(gate_fd)
        except OSError:
            pass
    if release != b"1":
        return 125
    paths = _paths(project_dir)
    snapshot = _snapshot_path(paths, job_id)
    if not snapshot.is_file() or snapshot.is_symlink():
        raise TrainingJobError("immutable trainer snapshot is unavailable")
    spec = _model_spec(model, paths, Path(sys.executable), snapshot)
    environment = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "CS2_DB_PATH": str(snapshot),
    }
    os.execve(spec.command[0], list(spec.command), environment)
    return 126  # pragma: no cover - successful exec never returns


def _run_worker(model: str, job_id: str, resource_fd: int, project_dir: Path) -> int:
    if os.environ.get("CS2_TRAINING_INTERNAL_WORKER") != "1":
        raise TrainingJobError("worker mode is internal")
    if model not in MODEL_NAMES or not _JOB_ID_RE.fullmatch(job_id):
        raise TrainingJobError("invalid worker job identity")
    try:
        os.fstat(resource_fd)
    except OSError as exc:
        raise TrainingJobError("worker did not inherit the global training lock") from exc

    paths = _paths(project_dir)
    log_path = _log_path(paths, model, job_id)
    cancel_signal = False

    def on_signal(_signum, _frame):
        nonlocal cancel_signal
        cancel_signal = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def cancellation_requested() -> bool:
        return cancel_signal or _job_cancel_requested(paths, model, job_id)

    # Parent records our PID immediately after Popen.  Wait briefly instead of
    # racing it and accidentally running an untracked trainer.
    claimed = False
    for _ in range(50):
        with _locked_state(paths):
            state = _read_state_unlocked(paths)
        job = state["models"][model]
        if job.get("job_id") != job_id:
            raise TrainingJobError("worker job is no longer active")
        if job.get("worker_pid") == os.getpid():
            claimed = True
            break
        time.sleep(0.1)
    if not claimed:
        raise TrainingJobError("worker could not confirm its persisted PID")

    trainer: subprocess.Popen[Any] | None = None
    tracker = ProgressTracker(model)
    last_heartbeat = 0.0
    cancel_started: float | None = None
    snapshot_path: Path | None = None
    gate_read_fd: int | None = None
    gate_write_fd: int | None = None
    try:
        if cancellation_requested():
            raise TrainingCancelled("training cancelled before snapshot")
        snapshot_path, snapshot_rows = _create_database_snapshot(
            paths,
            model,
            job_id,
            cancel_check=cancellation_requested,
        )
        _update_worker_job(
            paths,
            model,
            job_id,
            snapshot_state="ready",
            snapshot_rows=snapshot_rows,
            heartbeat_at=_utc_now(),
        )
        tracker.percent = max(tracker.percent, 5.0)
        spec = _model_spec(model, paths, Path(sys.executable), snapshot_path)
        log_fd = _secure_open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        with os.fdopen(log_fd, "w", encoding="utf-8", buffering=1) as log_writer:
            trainer_environment = {
                **os.environ,
                "PYTHONUNBUFFERED": "1",
                "CS2_DB_PATH": str(snapshot_path),
            }
            gate_read_fd, gate_write_fd = os.pipe()
            gate_command = [
                str(Path(sys.executable)),
                str(Path(__file__).resolve()),
                "--trainer-gate",
                model,
                job_id,
                str(resource_fd),
                str(paths.project),
                str(gate_read_fd),
            ]
            trainer = subprocess.Popen(
                gate_command,
                cwd=str(paths.project),
                stdin=subprocess.DEVNULL,
                stdout=log_writer,
                stderr=subprocess.STDOUT,
                shell=False,
                close_fds=True,
                pass_fds=(resource_fd, gate_read_fd),
                start_new_session=True,
                env=trainer_environment,
            )
            os.close(gate_read_fd)
            gate_read_fd = None
            trainer_start_token = _process_start_token(trainer.pid)
            if not trainer_start_token:
                _terminate_group(trainer.pid)
                raise TrainingJobError("cannot record trainer process birth token")
            if not _update_worker_job(
                paths,
                model,
                job_id,
                state="running",
                phase="preparing",
                trainer_pid=trainer.pid,
                trainer_start_token=trainer_start_token,
                heartbeat_at=_utc_now(),
            ):
                raise TrainingJobError("trainer PID could not be persisted")
            os.write(gate_write_fd, b"1")
            os.close(gate_write_fd)
            gate_write_fd = None
            with log_path.open("r", encoding="utf-8", errors="replace") as log_reader:
                while True:
                    while True:
                        line = log_reader.readline()
                        if not line:
                            break
                        phase, percent, eta = tracker.feed(line)
                        _update_worker_job(
                            paths,
                            model,
                            job_id,
                            phase=phase,
                            progress_percent=percent,
                            eta_seconds=eta,
                            heartbeat_at=_utc_now(),
                        )
                        last_heartbeat = time.monotonic()

                    requested = cancel_signal or _job_cancel_requested(paths, model, job_id)
                    if requested and trainer.poll() is None:
                        if cancel_started is None:
                            cancel_started = time.monotonic()
                            _terminate_group(trainer.pid)
                        elif time.monotonic() - cancel_started >= WORKER_CANCEL_GRACE_SECONDS:
                            _terminate_group(trainer.pid, force=True)

                    return_code = trainer.poll()
                    now_mono = time.monotonic()
                    if now_mono - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                        _update_worker_job(
                            paths,
                            model,
                            job_id,
                            heartbeat_at=_utc_now(),
                            eta_seconds=tracker.eta_seconds(),
                        )
                        last_heartbeat = now_mono
                    if return_code is not None:
                        # Drain bytes flushed just before process exit.
                        for line in log_reader:
                            phase, percent, eta = tracker.feed(line)
                            _update_worker_job(
                                paths,
                                model,
                                job_id,
                                phase=phase,
                                progress_percent=percent,
                                eta_seconds=eta,
                                heartbeat_at=_utc_now(),
                            )
                        break
                    time.sleep(0.5)

        cancelled = cancellation_requested()
        quality_gate = None
        quality_comparison = None
        candidate_bundle_sha256 = None
        rejection_kind = None
        activation_state = "none"
        active_bundle_version = None
        activation_error = None
        rollback_error = None
        retention_error = None
        if return_code == 0 and not cancelled:
            _update_worker_job(
                paths,
                model,
                job_id,
                phase="quality_gate",
                progress_percent=99.0,
                eta_seconds=None,
                heartbeat_at=_utc_now(),
            )
            try:
                manifest = _validate_candidate(spec, job["started_at"])
            except CandidateRejectedError as exc:
                quality_comparison = exc.quality_comparison
                if cancellation_requested():
                    final_state, phase, progress, error = (
                        "cancelled",
                        "cancelled",
                        tracker.percent,
                        None,
                    )
                else:
                    rejection_kind = exc.rejection_kind
                    final_state, phase, progress, error = (
                        "rejected",
                        f"{rejection_kind}_rejected",
                        100.0,
                        _redact(exc, limit=512),
                    )
            else:
                if cancellation_requested():
                    raise TrainingCancelled(
                        "training cancelled during candidate quality validation"
                    )
                quality_gate = manifest["quality_gate"]
                quality_comparison = manifest.get("quality_comparison")
                candidate_bundle_sha256 = manifest["bundle_sha256"]
                activation_state = "loading"
                _update_worker_job(
                    paths,
                    model,
                    job_id,
                    phase="hot_activation",
                    progress_percent=99.5,
                    eta_seconds=None,
                    heartbeat_at=_utc_now(),
                    quality_gate=quality_gate,
                    quality_comparison=quality_comparison,
                    candidate_bundle_sha256=candidate_bundle_sha256,
                    activation_state=activation_state,
                )
                try:
                    activated = model_generations.publish_candidate(
                        model,
                        spec.candidate_dir,
                        manifest=manifest,
                        job_id=job_id,
                        project_dir=paths.project,
                        cancel_check=cancellation_requested,
                    )
                except model_generations.GenerationCancelled:
                    raise TrainingCancelled(
                        "training cancelled during hot activation"
                    )
                except model_generations.GenerationActivationError as exc:
                    activation_state = "rollback_failed" if exc.rollback_error else "failed"
                    activation_error = _redact(exc, limit=512)
                    rollback_error = (
                        _redact(exc.rollback_error, limit=512)
                        if exc.rollback_error
                        else None
                    )
                    retention_error = (
                        _redact(exc.retention_error, limit=512)
                        if exc.retention_error
                        else None
                    )
                    if cancellation_requested():
                        final_state, phase, progress, error = (
                            "cancelled",
                            "cancelled",
                            tracker.percent,
                            None,
                        )
                    else:
                        final_state, phase, progress, error = (
                            "failed",
                            "hot_activation_failed",
                            100.0,
                            activation_error,
                        )
                except model_generations.GenerationError as exc:
                    activation_state = "failed"
                    activation_error = _redact(exc, limit=512)
                    final_state, phase, progress, error = (
                        "failed",
                        "hot_activation_failed",
                        100.0,
                        activation_error,
                    )
                else:
                    activation_state = "activated"
                    active_bundle_version = activated["active_bundle_version"]
                    retention_error = activated.get("retention_error")
                    final_state, phase, progress, error = (
                        "succeeded",
                        "active",
                        100.0,
                        None,
                    )
        elif cancelled:
            final_state, phase, progress, error = (
                "cancelled",
                "cancelled",
                tracker.percent,
                None,
            )
        else:
            tail = _read_log_tail(paths, model, job_id)
            final_state, phase, progress, error = (
                "failed",
                "failed",
                tracker.percent,
                _redact(tail[-1] if tail else f"trainer exited with code {return_code}", limit=512),
            )
        now = _utc_now()

        try:
            _delete_snapshot(paths, job_id)
            snapshot_state = "removed"
        except TrainingJobError:
            snapshot_state = "cleanup_failed"

        def finish(state: dict[str, Any]) -> None:
            current = state["models"][model]
            if current.get("job_id") != job_id:
                return
            effective_state = final_state
            effective_phase = phase
            effective_progress = progress
            effective_error = error
            effective_rejection_kind = rejection_kind
            effective_quality_gate = quality_gate
            effective_quality_comparison = quality_comparison
            effective_candidate_sha = candidate_bundle_sha256
            if (
                current.get("cancel_requested")
                and final_state != "cancelled"
                and activation_state != "activated"
            ):
                effective_state = "cancelled"
                effective_phase = "cancelled"
                effective_progress = tracker.percent
                effective_error = None
                effective_rejection_kind = None
                effective_quality_gate = None
                effective_quality_comparison = None
                effective_candidate_sha = None
            current.update(
                {
                    "state": effective_state,
                    "phase": effective_phase,
                    "progress_percent": effective_progress,
                    "eta_seconds": None,
                    "updated_at": now,
                    "heartbeat_at": now,
                    "finished_at": now,
                    "exit_code": return_code,
                    "error": effective_error,
                    "rejection_kind": effective_rejection_kind,
                    "snapshot_state": snapshot_state,
                    "quality_gate": effective_quality_gate,
                    "quality_comparison": effective_quality_comparison,
                    "candidate_bundle_sha256": effective_candidate_sha,
                    "activation_state": activation_state,
                    "active_bundle_version": active_bundle_version,
                    "activation_error": activation_error,
                    "rollback_error": rollback_error,
                    "retention_error": retention_error,
                }
            )
            if state.get("active_job_id") == job_id:
                state["active_job_id"] = None

        _mutate_state(paths, finish)
        try:
            cleanup_training_candidates(project_dir=paths.project)
        except TrainingJobError as exc:
            cleanup_error = _redact(exc, limit=512)

            def record_retention_error(state: dict[str, Any]) -> None:
                current = state["models"][model]
                if current.get("job_id") == job_id:
                    existing = current.get("retention_error")
                    current["retention_error"] = (
                        f"{existing}; {cleanup_error}" if existing else cleanup_error
                    )
                    current["updated_at"] = _utc_now()

            _mutate_state(paths, record_retention_error)
        return 0 if final_state in {"succeeded", "cancelled"} else 1
    except BaseException as exc:  # noqa: BLE001 - reap children on interrupts too.
        error_message = _redact(exc, limit=512)
        for descriptor in (gate_read_fd, gate_write_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        child_stopped = trainer is None or _stop_and_reap(trainer)
        now = _utc_now()

        was_cancelled = isinstance(exc, TrainingCancelled) or cancel_signal
        if not was_cancelled:
            try:
                was_cancelled = _job_cancel_requested(paths, model, job_id)
            except TrainingJobError:
                was_cancelled = False
        if child_stopped:
            try:
                _delete_snapshot(paths, job_id)
                snapshot_state = "removed"
            except TrainingJobError:
                snapshot_state = "cleanup_failed"
        else:
            snapshot_state = "retained_for_live_child"

        def fail(state: dict[str, Any]) -> None:
            current = state["models"][model]
            if current.get("job_id") != job_id:
                return
            current.update(
                {
                    "state": (
                        "cancelled"
                        if child_stopped and was_cancelled
                        else "failed"
                        if child_stopped
                        else "cancelling"
                    ),
                    "phase": (
                        "cancelled"
                        if child_stopped and was_cancelled
                        else "worker_failed"
                        if child_stopped
                        else "cleanup_waiting_for_child"
                    ),
                    "eta_seconds": None,
                    "updated_at": now,
                    "heartbeat_at": now,
                    "finished_at": now if child_stopped else None,
                    "error": (
                        None
                        if child_stopped and was_cancelled
                        else error_message
                        if child_stopped
                        else "trainer could not be stopped; snapshot retained"
                    ),
                    "snapshot_state": snapshot_state,
                }
            )
            if child_stopped and state.get("active_job_id") == job_id:
                state["active_job_id"] = None

        try:
            _mutate_state(paths, fail)
        except TrainingJobError:
            pass
        if child_stopped:
            try:
                cleanup_training_candidates(project_dir=paths.project)
            except TrainingJobError:
                pass
        return 1


def _parse_cli(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--trainer-gate", action="store_true")
    parser.add_argument("model", nargs="?")
    parser.add_argument("job_id", nargs="?")
    parser.add_argument("resource_fd", nargs="?", type=int)
    parser.add_argument("project_dir", nargs="?", type=Path)
    parser.add_argument("gate_fd", nargs="?", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_cli(argv)
    if args.worker == args.trainer_gate:
        raise SystemExit("model_training_jobs.py is an internal dashboard worker")
    if None in (args.model, args.job_id, args.resource_fd, args.project_dir):
        raise SystemExit("incomplete worker arguments")
    if args.trainer_gate:
        if args.gate_fd is None:
            raise SystemExit("incomplete trainer gate arguments")
        return _run_trainer_gate(
            args.model,
            args.job_id,
            args.resource_fd,
            args.project_dir,
            args.gate_fd,
        )
    return _run_worker(
        args.model,
        args.job_id,
        args.resource_fd,
        args.project_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
