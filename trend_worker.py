#!/usr/bin/env python3
"""Low-priority singleton worker that precomputes Trend research artifacts."""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import app_config
from TRENDMODEL.backtest import BacktestConfig, run_walk_forward_backtest
from TRENDMODEL.model import build_latest_forecasts
from TRENDMODEL.storage import (
    connect_store,
    load_daily_aggregates,
    refresh_daily_aggregates,
    save_backtest_report,
    save_forecasts,
)

try:
    import fcntl
except ImportError:  # Windows
    import portable_fcntl as fcntl


PROJECT_DIR = app_config.PROJECT_DIR
DEFAULT_SOURCE_DB = app_config.path("CS2_DB_PATH", "csfloat_history.db")
DEFAULT_STORE_DB = app_config.path("CS2_TREND_DB_PATH", "trend_market.db")
DEFAULT_STATUS_PATH = app_config.path(
    "CS2_TREND_STATUS_PATH", "trend_worker_status.json"
)
DEFAULT_LOCK_PATH = app_config.path("CS2_TREND_LOCK_PATH", ".trend_worker.lock")
MODEL_TRAINING_RESOURCE_LOCK = PROJECT_DIR / ".run" / "model_training" / "resource.lock"
DEFAULT_REFRESH_SECONDS = app_config.get_int(
    "CS2_TREND_REFRESH_SECONDS", 21_600, minimum=900
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class WorkerStatus:
    def __init__(
        self,
        path: Path,
        source_db: Path,
        store_db: Path,
        refresh_interval_seconds: int = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._value: dict[str, Any] = {
            "schema_version": 1,
            "state": "starting",
            "pid": os.getpid(),
            "started_at": _utc_now(),
            "heartbeat": _utc_now(),
            "phase": "starting",
            "progress_percent": 0.0,
            "last_error": None,
            "last_success": None,
            "next_refresh": None,
            "refresh_interval_seconds": refresh_interval_seconds,
            "source_db": str(source_db.resolve()),
            "store_db": str(store_db.resolve()),
            "aggregate_rows": None,
            "forecast_rows": None,
            "backtest_report_id": None,
        }
        self.write()

    def update(self, **changes: Any) -> None:
        with self._lock:
            self._value.update(changes)
            self._value["heartbeat"] = _utc_now()
            _atomic_json(self.path, self._value)

    def write(self) -> None:
        with self._lock:
            self._value["heartbeat"] = _utc_now()
            _atomic_json(self.path, self._value)


class SingletonLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        if fcntl is None:
            raise RuntimeError("singleton lock requires fcntl on this platform")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            return False
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return True

    def close(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def model_training_is_active(
    resource_lock_path: Path = MODEL_TRAINING_RESOURCE_LOCK,
) -> bool:
    """Probe the trainer EX lock without holding it during Trend work."""

    if fcntl is None:
        raise RuntimeError("model-training resource guard requires fcntl")
    path = Path(resource_lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError(
            "cannot safely inspect model-training resource lock"
        ) from exc
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            return False
        except BlockingIOError:
            return True
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def run_cycle(
    status: WorkerStatus,
    *,
    source_db: Path,
    store_db: Path,
) -> dict[str, Any]:
    status.update(
        state="running",
        phase="daily_aggregates",
        progress_percent=2.0,
        next_refresh=None,
    )
    aggregate = refresh_daily_aggregates(source_db, store_db)
    status.update(
        phase="forecasts",
        progress_percent=68.0,
        aggregate_rows=aggregate["daily_rows"],
    )
    conn = connect_store(store_db)
    try:
        daily_rows = load_daily_aggregates(conn)
        forecasts = build_latest_forecasts(daily_rows)
        forecast_write = save_forecasts(conn, forecasts)
        status.update(
            phase="backtest",
            progress_percent=82.0,
            forecast_rows=forecast_write["saved"],
        )
        report = run_walk_forward_backtest(daily_rows, BacktestConfig())
        report_id = save_backtest_report(conn, report)
    finally:
        conn.close()
    completed = {
        "aggregate_rows": aggregate["daily_rows"],
        "forecast_rows": forecast_write["saved"],
        "backtest_report_id": report_id,
        "backtest_status": report["status"],
    }
    status.update(
        state="ready",
        phase="idle",
        progress_percent=100.0,
        last_error=None,
        last_success=_utc_now(),
        **completed,
    )
    return completed


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--store-db", type=Path, default=DEFAULT_STORE_DB)
    parser.add_argument("--status-path", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--refresh-seconds", type=int, default=DEFAULT_REFRESH_SECONDS)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if args.refresh_seconds < 900 and not args.once:
        raise SystemExit("--refresh-seconds must be at least 900")
    singleton = SingletonLock(args.lock_path)
    if not singleton.acquire():
        print("Trend worker is already running")
        return 0
    try:
        try:
            os.nice(10)
        except (AttributeError, OSError):
            pass
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_args: stop.set())
        import windows_service_control
        windows_service_control.register_stop_callback(stop.set)
        status = WorkerStatus(
            args.status_path,
            args.source_db,
            args.store_db,
            args.refresh_seconds,
        )

        def heartbeat() -> None:
            while not stop.wait(15):
                status.write()

        heartbeat_thread = threading.Thread(
            target=heartbeat, name="trend-heartbeat", daemon=True
        )
        heartbeat_thread.start()
        while not stop.is_set():
            try:
                if model_training_is_active():
                    retry_seconds = 60
                    next_refresh = datetime.now(timezone.utc) + timedelta(
                        seconds=retry_seconds
                    )
                    status.update(
                        state="waiting",
                        phase="model_training_active",
                        progress_percent=0.0,
                        next_refresh=next_refresh.isoformat(),
                    )
                    if args.once:
                        break
                    stop.wait(retry_seconds)
                    continue
                run_cycle(status, source_db=args.source_db, store_db=args.store_db)
                retry_seconds = args.refresh_seconds
            # A daemon records unexpected failures and retries instead of
            # disappearing; the status file preserves the traceback.
            except Exception as exc:  # noqa: BLE001
                retry_seconds = min(args.refresh_seconds, 900)
                status.update(
                    state="degraded",
                    phase="idle",
                    progress_percent=0.0,
                    last_error={
                        "message": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(limit=12),
                        "at": _utc_now(),
                    },
                )
            if args.once:
                break
            next_refresh = datetime.now(timezone.utc) + timedelta(seconds=retry_seconds)
            status.update(next_refresh=next_refresh.isoformat())
            stop.wait(retry_seconds)
        status.update(
            state="stopped",
            phase="stopped",
            next_refresh=None,
        )
        stop.set()
        heartbeat_thread.join(timeout=2)
        return 0
    finally:
        singleton.close()


if __name__ == "__main__":
    raise SystemExit(main())
