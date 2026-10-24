"""Run one managed Python service with cooperative Windows shutdown support."""

from __future__ import annotations

import argparse
import os
import re
import runpy
import sys
from pathlib import Path

import windows_service_control


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--stop-token", required=True)
    parser.add_argument("script")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{32}", args.stop_token):
        parser.error("invalid service stop token")
    if os.name == "nt":
        import ctypes

        # The desktop controller (0x3ff) gets the Windows end-session message
        # first and can finish application cleanup before Windows reaches us.
        ctypes.windll.kernel32.SetProcessShutdownParameters(0x100, 1)
    finished = windows_service_control.start_watcher(
        args.stop_file.resolve(), args.stop_token
    )
    sys.argv = [args.script, *args.arguments]
    sys.path[0] = str(Path(args.script).resolve().parent)
    try:
        runpy.run_path(args.script, run_name="__main__")
    except KeyboardInterrupt:
        # Imports can be interrupted before the service's own handler exists.
        return 0
    finally:
        finished.set()
        windows_service_control.finish_children(args.stop_file, args.stop_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
