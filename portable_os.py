"""OS helpers for durability and ownership checks across POSIX and Windows."""

from __future__ import annotations

import os
import stat
from pathlib import Path

IS_WINDOWS = os.name == "nt"


def process_alive(pid: object) -> bool:
    """Return whether *pid* is alive without changing process state.

    ``os.kill(pid, 0)`` is a harmless existence probe on POSIX.  CPython maps
    it to ``TerminateProcess`` on Windows, so using it there can kill the very
    service whose status is being queried.
    """

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (OSError, ValueError):
            return False

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        process = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not process:
            # Access denied still proves that the process exists.  Invalid
            # parameter is what OpenProcess reports for a vanished PID.
            return ctypes.get_last_error() == 5
        try:
            wait_result = kernel32.WaitForSingleObject(process, 0)
            return wait_result == 0x102  # WAIT_TIMEOUT means still running.
        finally:
            kernel32.CloseHandle(process)
    except (OSError, TypeError, ValueError):
        return False


def fchmod(descriptor: int, mode: int) -> None:
    """Apply POSIX descriptor permissions when the platform supports them.

    Windows access is governed by ACLs and Python does not expose ``fchmod``.
    Files created inside the user's project directory retain that ACL, so the
    corresponding operation is intentionally a no-op there.
    """

    operation = getattr(os, "fchmod", None)
    if operation is not None:
        operation(descriptor, mode)


def fsync_directory(path: str | Path) -> None:
    """Persist directory metadata where Python exposes directory handles."""

    if IS_WINDOWS:
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def owned_by_current_user(info: os.stat_result) -> bool:
    """Return the POSIX ownership check, or rely on Windows ACL enforcement."""

    getuid = getattr(os, "getuid", None)
    return getuid is None or info.st_uid == getuid()


def has_unsafe_group_or_world_write(info: os.stat_result) -> bool:
    """Apply POSIX mode-bit privacy checks where those bits are meaningful."""

    return not IS_WINDOWS and bool(stat.S_IMODE(info.st_mode) & 0o022)


def venv_python(project: str | Path) -> Path:
    """Return the native interpreter path for a virtual environment."""

    root = Path(project) / ".venv"
    return root / "Scripts" / "python.exe" if IS_WINDOWS else root / "bin" / "python"
