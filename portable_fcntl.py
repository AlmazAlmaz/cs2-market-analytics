"""Small cross-platform subset of :mod:`fcntl` used by this project.

Windows does not ship Python's POSIX ``fcntl`` module.  The services only need
whole-file advisory locks, so this module exposes the four ``flock`` flags and
implements the same calls with Win32 ``LockFileEx``.  Unlike
``msvcrt.locking``, that API has real shared locks, which lets all serving
processes coexist while still excluding an offline model update.
"""

from __future__ import annotations

import time

try:  # pragma: no cover - exercised on macOS/Linux.
    import fcntl as _posix_fcntl
except ImportError:  # pragma: no cover - the Windows branch is exercised here.
    _posix_fcntl = None


if _posix_fcntl is not None:  # Keep native values for callers and tests.
    LOCK_SH = _posix_fcntl.LOCK_SH
    LOCK_EX = _posix_fcntl.LOCK_EX
    LOCK_NB = _posix_fcntl.LOCK_NB
    LOCK_UN = _posix_fcntl.LOCK_UN
else:
    # These values deliberately mirror the common POSIX values.  Only bitwise
    # composition and comparisons inside ``flock`` matter on Windows.
    LOCK_SH = 1
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8


def _windows_lock(descriptor: int, *, exclusive: bool, non_blocking: bool) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Offset(ctypes.Structure):
        _fields_ = [
            ("offset", wintypes.DWORD),
            ("offset_high", wintypes.DWORD),
        ]

    class OffsetUnion(ctypes.Union):
        _anonymous_ = ("parts",)
        _fields_ = [  # noqa: RUF012 - ctypes declares fields this way
            ("parts", Offset),
            ("pointer", wintypes.LPVOID),
        ]

    class Overlapped(ctypes.Structure):
        _anonymous_ = ("position",)
        _fields_ = [
            ("internal", ctypes.c_size_t),
            ("internal_high", ctypes.c_size_t),
            ("position", OffsetUnion),
            ("event", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    kernel32.LockFileEx.restype = wintypes.BOOL
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    flags = (0x2 if exclusive else 0) | (0x1 if non_blocking else 0)

    while True:
        overlapped = Overlapped()
        if kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            return
        error = ctypes.get_last_error()
        if error == 33:  # ERROR_LOCK_VIOLATION
            if non_blocking:
                raise BlockingIOError(11, "lock is already held")
            time.sleep(0.05)
            continue
        raise ctypes.WinError(error)


def _windows_unlock(descriptor: int) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Offset(ctypes.Structure):
        _fields_ = [
            ("offset", wintypes.DWORD),
            ("offset_high", wintypes.DWORD),
        ]

    class OffsetUnion(ctypes.Union):
        _anonymous_ = ("parts",)
        _fields_ = [  # noqa: RUF012 - ctypes declares fields this way
            ("parts", Offset),
            ("pointer", wintypes.LPVOID),
        ]

    class Overlapped(ctypes.Structure):
        _anonymous_ = ("position",)
        _fields_ = [
            ("internal", ctypes.c_size_t),
            ("internal_high", ctypes.c_size_t),
            ("position", OffsetUnion),
            ("event", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    kernel32.UnlockFileEx.restype = wintypes.BOOL
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    overlapped = Overlapped()
    if not kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
        raise ctypes.WinError(ctypes.get_last_error())


def flock(descriptor: int, operation: int) -> None:
    """Apply or release a whole-file advisory lock.

    Non-blocking Windows contention is normalized to
    :class:`BlockingIOError`, matching ``fcntl.flock``.
    """

    if _posix_fcntl is not None:  # pragma: no cover - platform dependent.
        _posix_fcntl.flock(descriptor, operation)
        return

    if operation & LOCK_UN:
        _windows_unlock(descriptor)
        return
    _windows_lock(
        descriptor,
        exclusive=bool(operation & LOCK_EX),
        non_blocking=bool(operation & LOCK_NB),
    )
