"""Shared local configuration for the CS2 services.

The project historically relied on the current working directory and many
small secret files.  This module keeps those files backwards compatible while
also supporting one private ``config.local.env`` file and normal environment
variables.  It deliberately has no third-party dependencies.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.local.env"


def _decode_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        try:
            decoded = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"invalid quoted value: {raw_value!r}") from exc
        return str(decoded)

    comment_index = value.find(" #")
    if comment_index >= 0:
        value = value[:comment_index]
    return value.strip()


def load_env_file(path: str | Path | None = None, *, override: bool = False) -> Path:
    """Load KEY=VALUE pairs, leaving real environment variables in control."""

    config_path = Path(path or os.environ.get("CS2_CONFIG_FILE") or DEFAULT_CONFIG_PATH)
    if not config_path.is_absolute():
        config_path = PROJECT_DIR / config_path
    if not config_path.exists():
        return config_path

    for line_number, raw_line in enumerate(config_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{config_path}:{line_number}: expected KEY=VALUE")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "A").isalnum() or not key[0].isalpha():
            raise ValueError(f"{config_path}:{line_number}: invalid variable name {key!r}")
        if override or key not in os.environ:
            os.environ[key] = _decode_env_value(raw_value)
    return config_path


CONFIG_PATH = load_env_file()


def get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def get_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = get(name)
    try:
        value = int(raw) if raw else int(default)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def get_bool(name: str, default: bool = False) -> bool:
    raw = get(name)
    if not raw:
        return default
    if raw.lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true/false, got {raw!r}")


def path(name: str, default: str | Path) -> Path:
    raw = get(name)
    result = Path(raw).expanduser() if raw else Path(default)
    return result if result.is_absolute() else PROJECT_DIR / result


def read_secret(name: str, legacy_file: str | Path | None = None, *, required: bool = False) -> str:
    """Read a secret from the environment, then from an optional legacy file."""

    value = get(name)
    if value:
        return value
    if legacy_file is not None:
        secret_path = Path(legacy_file)
        if not secret_path.is_absolute():
            secret_path = PROJECT_DIR / secret_path
        if secret_path.exists():
            value = secret_path.read_text(encoding="utf-8").strip()
            if value:
                return value
    if required:
        fallback = f" or {legacy_file}" if legacy_file else ""
        raise RuntimeError(f"Missing secret {name}{fallback}")
    return ""
