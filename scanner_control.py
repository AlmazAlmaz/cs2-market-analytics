"""Persistent, process-safe control and runtime status for ``deal_scanner``.

The control file is intentionally small and contains no API keys, proxies, or
other secrets.  Dashboard writes and scanner reads are coordinated with a
process lock; readers only ever observe a fully fsynced old or new JSON file.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import app_config

try:
    import fcntl
except ImportError:  # pragma: no cover - the production host is macOS/Linux
    fcntl = None


CONTROL_SCHEMA_VERSION = 1
STATUS_SCHEMA_VERSION = 1
TARGET_CONTROL_SCHEMA_VERSION = 1
DEFAULT_INTERVAL_SECONDS = 600
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 86400

CONTROL_PATH = app_config.path(
    "CS2_SCANNER_CONTROL_PATH",
    "deal_scanner_control.json",
)
STATUS_PATH = app_config.path(
    "CS2_SCANNER_STATUS_PATH",
    "deal_scanner_status.json",
)
TARGET_CONTROL_PATH = app_config.path(
    "CS2_SCANNER_TARGET_CONTROL_PATH",
    "deal_scanner_targets.json",
)

TARGET_PROFILE_NAMES = (
    "sticker_guns",
    "liquid_under_10",
    "knives",
    "gloves",
)
TARGET_PROFILE_PREFIXES = {
    "sticker_guns": "sticker:",
    "liquid_under_10": "liquid_skin:",
    "knives": "knife:",
    "gloves": "glove:",
}
TARGET_SELECTION_MODES = frozenset({"recommended", "custom"})
MAX_INCLUDED_TARGETS = 250
MAX_EXCLUDED_TARGETS = 1000
MAX_TARGET_KEY_LENGTH = 512
TARGET_CONTROL_BODY_LIMIT = 768 * 1024
TARGET_CONTROL_FILE_KEYS = frozenset(
    {"schema_version", "revision", "profiles", "updated_at"}
)
TARGET_PROFILE_FILE_KEYS = frozenset(
    {"enabled", "selection_mode", "include", "exclude"}
)

PROCESS_STATES = frozenset({"starting", "scanning", "sleeping", "paused", "stopped"})
CONTROL_FILE_KEYS = frozenset(
    {"schema_version", "enabled", "interval_seconds", "updated_at"}
)
STATUS_FILE_KEYS = frozenset(
    {
        "schema_version",
        "process_state",
        "pid",
        "heartbeat_at",
        "cycle_started_at",
        "cycle_completed_at",
        "next_scan_at",
        "last_error",
        "cycle_number",
        "cycles_completed",
        "cycle_targets_total",
        "cycle_targets_scanned",
        "cycle_listings_returned",
        "cycle_candidates_saved",
    }
)

_THREAD_LOCK = threading.RLock()
_UNSET = object()
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|password|proxy|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_URL_USERINFO_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^/@\s]+)@")
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|authorization|password|proxy|token)=)[^&#\s]+"
)


class ScannerControlError(RuntimeError):
    """The persistent scanner control/status file is invalid or unavailable."""


class ScannerControlConflict(ScannerControlError):
    """A target-control update was based on an obsolete revision."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_path(path: str | Path | None, default: Path) -> Path:
    result = Path(path) if path is not None else Path(default)
    if not result.is_absolute():
        result = app_config.PROJECT_DIR / result
    return result


def _coerce_interval(value: Any, *, field_name: str = "interval_seconds") -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise TypeError(f"{field_name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field_name} must be an integer") from exc
    if not MIN_INTERVAL_SECONDS <= result <= MAX_INTERVAL_SECONDS:
        raise ValueError(
            f"{field_name} must be between "
            f"{MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS}"
        )
    return result


def _validate_timestamp(value: Any, field_name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ScannerControlError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ScannerControlError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ScannerControlError(f"{field_name} must include a timezone")
    return value


def _validate_nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScannerControlError(f"{field_name} must be a non-negative integer")
    return value


def _safe_error(value: Any) -> str | None:
    if value is None:
        return None
    message = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not message:
        return None
    message = _URL_USERINFO_RE.sub(r"\1***@", message)
    message = _SENSITIVE_QUERY_RE.sub(r"\1***", message)
    message = _SECRET_ASSIGNMENT_RE.sub(r"\1\2***", message)
    return message[:512]


def _default_control(default_interval_seconds: int) -> dict[str, Any]:
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "enabled": True,
        "interval_seconds": _coerce_interval(
            default_interval_seconds,
            field_name="default_interval_seconds",
        ),
        "updated_at": _utc_now(),
    }


def _default_status() -> dict[str, Any]:
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "process_state": "stopped",
        "pid": None,
        "heartbeat_at": None,
        "cycle_started_at": None,
        "cycle_completed_at": None,
        "next_scan_at": None,
        "last_error": None,
        "cycle_number": 0,
        "cycles_completed": 0,
        "cycle_targets_total": 0,
        "cycle_targets_scanned": 0,
        "cycle_listings_returned": 0,
        "cycle_candidates_saved": 0,
    }


def _profile_enabled_defaults(
    profile_defaults: dict[str, Any] | None = None,
) -> dict[str, bool]:
    defaults = {
        "sticker_guns": True,
        "liquid_under_10": True,
        "knives": False,
        "gloves": False,
    }
    for name, value in (profile_defaults or {}).items():
        if name not in TARGET_PROFILE_NAMES:
            raise ValueError(f"unknown scanner target profile: {name}")
        if not isinstance(value, bool):
            raise TypeError(f"profile default {name} must be true or false")
        defaults[name] = value
    return defaults


def _default_target_control(
    profile_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    enabled = _profile_enabled_defaults(profile_defaults)
    return {
        "schema_version": TARGET_CONTROL_SCHEMA_VERSION,
        "revision": 0,
        "profiles": {
            name: {
                "enabled": enabled[name],
                "selection_mode": "recommended",
                "include": [],
                "exclude": [],
            }
            for name in TARGET_PROFILE_NAMES
        },
        "updated_at": _utc_now(),
    }


def _validate_target_keys(value: Any, profile: str, field_name: str) -> list[str]:
    maximum = (
        MAX_INCLUDED_TARGETS if field_name == "include" else MAX_EXCLUDED_TARGETS
    )
    if not isinstance(value, list):
        raise ScannerControlError(f"{profile}.{field_name} must be a JSON list")
    if len(value) > maximum:
        raise ScannerControlError(
            f"{profile}.{field_name} may contain at most {maximum} targets"
        )
    prefix = TARGET_PROFILE_PREFIXES[profile]
    normalized: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise ScannerControlError(
                f"{profile}.{field_name} target keys must be strings"
            )
        key = raw.strip()
        if (
            not key.startswith(prefix)
            or len(key) <= len(prefix)
            or len(key) > MAX_TARGET_KEY_LENGTH
            or any(character in key for character in ("\x00", "\r", "\n"))
        ):
            raise ScannerControlError(
                f"{profile}.{field_name} contains an invalid target key"
            )
        normalized.append(key)
    return list(dict.fromkeys(normalized))


def _validate_target_control_file(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ScannerControlError("scanner target control must contain a JSON object")
    if set(payload) != TARGET_CONTROL_FILE_KEYS:
        raise ScannerControlError(
            "scanner target control has unexpected or missing fields"
        )
    if payload.get("schema_version") != TARGET_CONTROL_SCHEMA_VERSION:
        raise ScannerControlError("unsupported scanner target control schema_version")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ScannerControlError("scanner target control revision must be non-negative")
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(TARGET_PROFILE_NAMES):
        raise ScannerControlError("scanner target control profiles are incomplete")

    normalized_profiles: dict[str, dict[str, Any]] = {}
    for name in TARGET_PROFILE_NAMES:
        profile = profiles[name]
        if not isinstance(profile, dict) or set(profile) != TARGET_PROFILE_FILE_KEYS:
            raise ScannerControlError(f"scanner target profile {name} is invalid")
        if not isinstance(profile.get("enabled"), bool):
            raise ScannerControlError(f"{name}.enabled must be true or false")
        selection_mode = profile.get("selection_mode")
        if selection_mode not in TARGET_SELECTION_MODES:
            raise ScannerControlError(
                f"{name}.selection_mode must be recommended or custom"
            )
        include = _validate_target_keys(profile.get("include"), name, "include")
        exclude = _validate_target_keys(profile.get("exclude"), name, "exclude")
        overlap = set(include) & set(exclude)
        if overlap:
            raise ScannerControlError(
                f"{name}.include and {name}.exclude must not overlap"
            )
        normalized_profiles[name] = {
            "enabled": profile["enabled"],
            "selection_mode": selection_mode,
            "include": include,
            "exclude": exclude,
        }

    return {
        "schema_version": TARGET_CONTROL_SCHEMA_VERSION,
        "revision": revision,
        "profiles": normalized_profiles,
        "updated_at": _validate_timestamp(payload.get("updated_at"), "updated_at"),
    }


def _validate_control_file(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ScannerControlError("scanner control must contain a JSON object")
    if set(payload) != CONTROL_FILE_KEYS:
        raise ScannerControlError("scanner control has unexpected or missing fields")
    if payload.get("schema_version") != CONTROL_SCHEMA_VERSION:
        raise ScannerControlError("unsupported scanner control schema_version")
    if not isinstance(payload.get("enabled"), bool):
        raise ScannerControlError("enabled must be true or false")
    try:
        interval = _coerce_interval(payload.get("interval_seconds"))
    except (TypeError, ValueError) as exc:
        raise ScannerControlError(str(exc)) from exc
    updated_at = _validate_timestamp(payload.get("updated_at"), "updated_at")
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "enabled": payload["enabled"],
        "interval_seconds": interval,
        "updated_at": updated_at,
    }


def _validate_status_file(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ScannerControlError("scanner status must contain a JSON object")
    if set(payload) != STATUS_FILE_KEYS:
        raise ScannerControlError("scanner status has unexpected or missing fields")
    if payload.get("schema_version") != STATUS_SCHEMA_VERSION:
        raise ScannerControlError("unsupported scanner status schema_version")
    process_state = payload.get("process_state")
    if process_state not in PROCESS_STATES:
        raise ScannerControlError("invalid scanner process_state")
    pid = payload.get("pid")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
        raise ScannerControlError("pid must be a positive integer or null")

    result = dict(payload)
    result["process_state"] = process_state
    result["pid"] = pid
    result["heartbeat_at"] = _validate_timestamp(
        payload.get("heartbeat_at"),
        "heartbeat_at",
        optional=True,
    )
    for field_name in ("cycle_started_at", "cycle_completed_at", "next_scan_at"):
        result[field_name] = _validate_timestamp(
            payload.get(field_name),
            field_name,
            optional=True,
        )
    result["last_error"] = _safe_error(payload.get("last_error"))
    for field_name in (
        "cycle_number",
        "cycles_completed",
        "cycle_targets_total",
        "cycle_targets_scanned",
        "cycle_listings_returned",
        "cycle_candidates_saved",
    ):
        result[field_name] = _validate_nonnegative_int(payload.get(field_name), field_name)
    return result


def _read_json(path: Path) -> Any:
    if path.is_symlink():
        raise ScannerControlError(f"refusing symlink state path: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScannerControlError(f"cannot read valid scanner state from {path}") from exc


def _ensure_private_mode(path: Path) -> None:
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            os.chmod(path, 0o600)
    except OSError as exc:
        raise ScannerControlError(f"cannot secure scanner state at {path}") from exc


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ScannerControlError(f"refusing symlink state path: {path}")
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(temp_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise ScannerControlError(f"cannot atomically write scanner state to {path}") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


@contextmanager
def _state_lock(path: Path):
    lock_path = path.with_name(f".{path.name}.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScannerControlError(f"cannot prepare scanner state path at {path}") from exc
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with _THREAD_LOCK:
        try:
            lock_fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ScannerControlError(f"cannot lock scanner state at {path}") from exc
        try:
            if stat.S_IMODE(os.fstat(lock_fd).st_mode) != 0o600:
                os.fchmod(lock_fd, 0o600)
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def _public_control(payload: dict[str, Any], *, error: str | None = None) -> dict[str, Any]:
    result = dict(payload)
    # Enabled is desired control state, not proof that a process is alive.
    # Actual running/scanning/sleeping state comes from get_scanner_status().
    result["status"] = "enabled" if payload["enabled"] else "paused"
    if error:
        result["error"] = error
    return result


def get_scanner_control(
    *,
    default_interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Return JSON-ready scanner control, creating it on the first read.

    Corruption fails closed: callers receive ``enabled=false`` and an opaque
    error code, while the original invalid file remains untouched for repair.
    """

    default_control = _default_control(default_interval_seconds)
    control_path = _resolve_path(path, CONTROL_PATH)
    with _state_lock(control_path):
        try:
            payload = _validate_control_file(_read_json(control_path))
            _ensure_private_mode(control_path)
        except FileNotFoundError:
            payload = default_control
            _atomic_write_json(control_path, payload)
        except ScannerControlError:
            failed_closed = dict(default_control)
            failed_closed["enabled"] = False
            failed_closed["updated_at"] = None
            return _public_control(failed_closed, error="invalid_control_state")
    return _public_control(payload)


def update_scanner_control(
    *,
    enabled: bool | object = _UNSET,
    interval_seconds: int | object = _UNSET,
    default_interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically update one or both persistent scanner controls."""

    if enabled is _UNSET and interval_seconds is _UNSET:
        raise ValueError("enabled or interval_seconds must be provided")
    if enabled is not _UNSET and not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    normalized_interval = _UNSET
    if interval_seconds is not _UNSET:
        normalized_interval = _coerce_interval(interval_seconds)

    control_path = _resolve_path(path, CONTROL_PATH)
    with _state_lock(control_path):
        try:
            payload = _validate_control_file(_read_json(control_path))
        except FileNotFoundError:
            payload = _default_control(default_interval_seconds)
        except ScannerControlError as exc:
            raise ScannerControlError(
                "scanner control is invalid; preserve or remove it before updating"
            ) from exc
        if enabled is not _UNSET:
            payload["enabled"] = enabled
        if normalized_interval is not _UNSET:
            payload["interval_seconds"] = normalized_interval
        payload["updated_at"] = _utc_now()
        payload = _validate_control_file(payload)
        _atomic_write_json(control_path, payload)
    return _public_control(payload)


def get_scanner_target_control(
    *,
    path: str | Path | None = None,
    profile_defaults: dict[str, Any] | None = None,
    create_if_missing: bool = True,
) -> dict[str, Any]:
    """Return durable profile selection without exposing scanner secrets.

    ``create_if_missing=False`` is useful to the scanner process: legacy
    installations inherit their validated config until the dashboard makes the
    first target-selection change.  Invalid persisted state always fails
    closed and disables every profile.
    """

    default_control = _default_target_control(profile_defaults)
    target_path = _resolve_path(path, TARGET_CONTROL_PATH)
    with _state_lock(target_path):
        try:
            payload = _validate_target_control_file(_read_json(target_path))
            _ensure_private_mode(target_path)
        except FileNotFoundError:
            payload = default_control
            if create_if_missing:
                _atomic_write_json(target_path, payload)
        except ScannerControlError:
            failed_closed = _default_target_control(
                {name: False for name in TARGET_PROFILE_NAMES}
            )
            failed_closed["updated_at"] = None
            failed_closed["error"] = "invalid_target_control_state"
            return failed_closed
    return payload


def validate_scanner_target_control(payload: Any) -> dict[str, Any]:
    """Validate and normalize an in-memory target-control document."""

    return _validate_target_control_file(payload)


def update_scanner_target_control(
    profile_changes: dict[str, Any],
    *,
    expected_revision: int | None = None,
    path: str | Path | None = None,
    profile_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a validated partial profile patch with optimistic concurrency.

    Each profile patch may contain any subset of ``enabled``,
    ``selection_mode``, ``include`` and ``exclude``.  Lists replace the old
    list rather than being appended, which keeps repeated HTTP requests
    idempotent.
    """

    if not isinstance(profile_changes, dict) or not profile_changes:
        raise ValueError("profiles must be a non-empty JSON object")
    unknown_profiles = sorted(set(profile_changes) - set(TARGET_PROFILE_NAMES))
    if unknown_profiles:
        raise ValueError(f"unknown scanner target profiles: {', '.join(unknown_profiles)}")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be a non-negative integer")

    target_path = _resolve_path(path, TARGET_CONTROL_PATH)
    with _state_lock(target_path):
        try:
            payload = _validate_target_control_file(_read_json(target_path))
        except FileNotFoundError:
            payload = _default_target_control(profile_defaults)
        except ScannerControlError as exc:
            raise ScannerControlError(
                "scanner target control is invalid; preserve or remove it before updating"
            ) from exc
        if expected_revision is not None and payload["revision"] != expected_revision:
            raise ScannerControlConflict(
                "scanner target control changed; refresh it before saving"
            )

        for name, raw_changes in profile_changes.items():
            if not isinstance(raw_changes, dict) or not raw_changes:
                raise ValueError(f"profile {name} changes must be a non-empty object")
            unknown_fields = sorted(set(raw_changes) - TARGET_PROFILE_FILE_KEYS)
            if unknown_fields:
                raise ValueError(
                    f"unexpected {name} fields: {', '.join(unknown_fields)}"
                )
            profile = dict(payload["profiles"][name])
            profile.update(raw_changes)
            payload["profiles"][name] = profile

        payload["revision"] += 1
        payload["updated_at"] = _utc_now()
        payload = _validate_target_control_file(payload)
        _atomic_write_json(target_path, payload)
    return payload


def get_scanner_status(*, path: str | Path | None = None) -> dict[str, Any]:
    """Return the last runtime heartbeat; corrupt/missing status is stopped."""

    status_path = _resolve_path(path, STATUS_PATH)
    with _state_lock(status_path):
        try:
            payload = _validate_status_file(_read_json(status_path))
            _ensure_private_mode(status_path)
        except FileNotFoundError:
            return _default_status()
        except ScannerControlError:
            payload = _default_status()
            payload["last_error"] = "invalid_status_state"
    return payload


def update_scanner_status(
    *,
    path: str | Path | None = None,
    **changes: Any,
) -> dict[str, Any]:
    """Merge and atomically persist a scanner-owned runtime heartbeat."""

    unknown = set(changes) - (STATUS_FILE_KEYS - {"schema_version"})
    if unknown:
        raise ValueError(f"unknown scanner status fields: {', '.join(sorted(unknown))}")
    status_path = _resolve_path(path, STATUS_PATH)
    with _state_lock(status_path):
        try:
            payload = _validate_status_file(_read_json(status_path))
        except (FileNotFoundError, ScannerControlError):
            payload = _default_status()
        payload.update(changes)
        payload["schema_version"] = STATUS_SCHEMA_VERSION
        if "last_error" in payload:
            payload["last_error"] = _safe_error(payload["last_error"])
        payload = _validate_status_file(payload)
        _atomic_write_json(status_path, payload)
    return payload


def get_scanner_snapshot(
    *,
    default_interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Convenience payload for the dashboard API."""

    return {
        "control": get_scanner_control(default_interval_seconds=default_interval_seconds),
        "runtime": get_scanner_status(),
    }
