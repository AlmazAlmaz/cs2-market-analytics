"""Cooperative stop requests for Python services owned by the Windows launcher.

A fresh random token identifies each service lifetime.  Requests are inherited
by training workers, but never affect independently started Python processes.
"""

from __future__ import annotations

import _thread
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

_stop_event = threading.Event()
_callbacks: list[Callable[[], None]] = []
_children: list[subprocess.Popen] = []
_lock = threading.Lock()
_watching = False


def stop_requested() -> bool:
    token = os.environ.get("CS2_SERVICE_STOP_TOKEN", "")
    path = os.environ.get("CS2_SERVICE_STOP_FILE", "")
    if not token or not path:
        return False
    try:
        return Path(path).read_text(encoding="utf-8").strip() == token
    except (OSError, UnicodeError):
        return False


def register_stop_callback(callback: Callable[[], None]) -> None:
    """Register a thread-safe wake-up for a worker's existing stop event."""
    if not _watching:
        return
    with _lock:
        _callbacks.append(callback)
        stopped = _stop_event.is_set()
    if stopped:
        callback()


def wait_for_stop(seconds: float) -> None:
    """Keep the parser's long sleep interruptible in a hidden Windows process."""
    if _watching:
        _stop_event.wait(seconds)
    else:
        time.sleep(seconds)


def register_child(process: subprocess.Popen) -> None:
    """Keep the real Popen handle so shutdown cannot target a reused PID."""
    if not _watching:
        return
    with _lock:
        _children[:] = [child for child in _children if child.poll() is None]
        _children.append(process)


def start_watcher(stop_file: Path, token: str) -> threading.Event:
    global _watching
    os.environ["CS2_SERVICE_STOP_FILE"] = str(stop_file)
    os.environ["CS2_SERVICE_STOP_TOKEN"] = token
    _watching = True
    finished = threading.Event()

    def watch() -> None:
        while not finished.wait(0.1):
            if not stop_requested():
                continue
            with _lock:
                _stop_event.set()
                callbacks = tuple(_callbacks)
            for callback in callbacks:
                callback()
            if not callbacks:
                # HTTP serve_forever wakes every 0.5s.  Existing main-thread
                # KeyboardInterrupt/finally handlers close active connections.
                _thread.interrupt_main(signal.SIGINT)
            return

    threading.Thread(target=watch, name="windows-service-stop", daemon=True).start()
    return finished


def finish_children(stop_file: Path, token: str) -> None:
    """Wait for owned training workers to cancel, reap trainers and save state.

    The supervisor bounds this wait and retains its identity-checked process
    tree fallback.  Keeping this parent alive also keeps that tree verifiable.
    """
    with _lock:
        children = tuple(_children)
    if not any(child.poll() is None for child in children):
        return
    stop_file.write_text(token, encoding="utf-8")
    for child in children:
        child.wait()
