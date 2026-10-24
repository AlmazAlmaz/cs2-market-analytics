"""Minimal SSH_ASKPASS bridge for the Windows reverse tunnel.

The long-lived ssh.exe process receives only the private configuration path.
This short-lived helper reads the password when OpenSSH asks for it and writes
it to OpenSSH's private pipe.  It deliberately has no command-line interface.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _read_password(config_path: Path) -> str:
    password: str | None = None
    for raw_line in config_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "VPS_PASSWORD":
            continue
        if password is not None:
            raise ValueError("duplicate VPS_PASSWORD")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        password = value
    if not password:
        raise ValueError("VPS_PASSWORD is empty")
    return password


def main() -> int:
    config_value = os.environ.get("CS2_SSH_ASKPASS_CONFIG", "")
    if not config_value:
        return 1
    try:
        password = _read_password(Path(config_value))
        sys.stdout.buffer.write(password.encode("utf-8"))
        sys.stdout.buffer.flush()
    except (OSError, UnicodeError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
