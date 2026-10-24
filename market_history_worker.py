#!/usr/bin/env python3
"""Private multi-market history importer and optional background worker.

Automatic network access is fail-closed: it requires an explicit enable flag,
an explicit licence/terms acknowledgement, and ``CSPRICEAPI_KEY``.  A key alone
never enables collection.  Manual ZIP import is an offline, explicit action and
does not require those network flags.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import app_config
import market_history


@dataclass(frozen=True)
class WorkerSettings:
    enabled: bool
    license_confirmed: bool
    database_path: Path
    status_path: Path
    refresh_seconds: int
    fetch_snapshots: bool
    fetch_sales: bool
    sales_contract_confirmed: bool
    sales_contract_version: str
    sales_timestamps_are_utc: bool
    api_key: str

    @classmethod
    def from_environment(cls) -> WorkerSettings:
        return cls(
            enabled=app_config.get_bool("CS2_MARKET_HISTORY_ENABLED", False),
            license_confirmed=app_config.get_bool(
                "CS2_MARKET_HISTORY_LICENSE_CONFIRMED", False
            ),
            database_path=app_config.path(
                "CS2_MARKET_HISTORY_DB_PATH", ".private/market_history.db"
            ),
            status_path=app_config.path(
                "CS2_MARKET_HISTORY_STATUS_PATH",
                ".private/market_history_status.json",
            ),
            refresh_seconds=app_config.get_int(
                "CS2_MARKET_HISTORY_REFRESH_SECONDS",
                86_400,
                minimum=900,
                maximum=604_800,
            ),
            fetch_snapshots=app_config.get_bool(
                "CS2_MARKET_HISTORY_FETCH_SNAPSHOTS", True
            ),
            fetch_sales=app_config.get_bool("CS2_MARKET_HISTORY_FETCH_SALES", True),
            sales_contract_confirmed=app_config.get_bool(
                "CS2_MARKET_HISTORY_SALES_CONTRACT_CONFIRMED", False
            ),
            sales_contract_version=app_config.get(
                "CS2_MARKET_HISTORY_SALES_CONTRACT_VERSION"
            ),
            sales_timestamps_are_utc=app_config.get_bool(
                "CS2_MARKET_HISTORY_SALES_TIMESTAMPS_ARE_UTC", False
            ),
            api_key=app_config.get("CSPRICEAPI_KEY"),
        )


def _safe_failure(error: Exception) -> dict[str, Any]:
    if isinstance(error, market_history.DownloadError):
        return {
            "status": "error",
            "error_code": error.code,
            "http_status": error.status_code,
        }
    if isinstance(error, market_history.ValidationError):
        return {
            "status": "quarantined",
            "error_code": error.code,
            "source_file": error.source_file,
            "row_number": error.row_number,
        }
    return {"status": "error", "error_code": type(error).__name__}


def _result_dict(result: market_history.ImportResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "run_id": result.run_id,
        "source_rows": result.source_rows,
        "inserted_rows": result.inserted_rows,
        "duplicate_rows": result.duplicate_rows,
        "attachment_rows": result.attachment_rows,
        "reused_run": result.reused_run,
    }


def _network_gate(settings: WorkerSettings) -> tuple[bool, str | None]:
    if not settings.enabled:
        return False, "disabled"
    if not settings.license_confirmed:
        return False, "license_not_confirmed"
    if not settings.api_key.strip():
        return False, "missing_api_key"
    if not settings.fetch_snapshots and not settings.fetch_sales:
        return False, "all_fetches_disabled"
    return True, None


def _confirmed_sales_contract(
    settings: WorkerSettings,
) -> market_history.SalesExportContract | None:
    if not (
        settings.sales_contract_confirmed
        and bool(settings.sales_contract_version)
        and settings.sales_timestamps_are_utc
    ):
        return None
    return market_history.SalesExportContract.confirmed_cspriceapi(
        settings.sales_contract_version
    )


def run_once(
    settings: WorkerSettings,
    *,
    session: Any = None,
) -> dict[str, Any]:
    """Run independent bounded fetches; a sales 403 never rolls back snapshots."""

    permitted, reason = _network_gate(settings)
    if not permitted:
        return {
            "state": "disabled"
            if reason in {"disabled", "all_fetches_disabled"}
            else "blocked",
            "reason": reason,
            "network_attempted": False,
            "completed_at": market_history.utc_now(),
        }

    connection = market_history.connect_database(settings.database_path)
    report: dict[str, Any] = {
        "state": "running",
        "started_at": market_history.utc_now(),
        "database": str(settings.database_path),
        "fx": None,
        "snapshots": {},
        "sales": None,
    }
    try:
        try:
            downloaded_fx = market_history.download_ecb_rates(session=session)
            saved = market_history.import_ecb_reference_rates(
                connection, downloaded_fx.body
            )
            report["fx"] = {"status": "complete", "saved_rows": saved}
        except (market_history.MarketHistoryError, sqlite3.Error, OSError) as exc:
            # Native prices remain valid and USD columns intentionally stay NULL.
            report["fx"] = _safe_failure(exc)

        if settings.fetch_snapshots:
            for marketplace in ("buff163", "youpin"):
                try:
                    downloaded = market_history.download_cspriceapi_snapshot(
                        marketplace, settings.api_key, session=session
                    )
                    result = market_history.import_cspriceapi_snapshot(
                        connection,
                        marketplace,
                        downloaded.body,
                        source_uri=market_history.CSPRICEAPI_SNAPSHOT_URLS[marketplace],
                    )
                    report["snapshots"][marketplace] = _result_dict(result)
                except (
                    market_history.MarketHistoryError,
                    sqlite3.Error,
                    OSError,
                ) as exc:
                    report["snapshots"][marketplace] = _safe_failure(exc)
        else:
            report["snapshots"] = {"status": "disabled"}

        if settings.fetch_sales:
            contract = _confirmed_sales_contract(settings)
            if contract is None:
                report["sales"] = {
                    "status": "skipped",
                    "reason": "unverified_sales_export_contract",
                }
            else:
                try:
                    downloaded_sales = market_history.download_cspriceapi_sales_zip(
                        settings.api_key, session=session
                    )
                    result = market_history.import_cspriceapi_sales_zip(
                        connection,
                        downloaded_sales.body,
                        response_headers=downloaded_sales.headers,
                        contract=contract,
                        require_response_headers=True,
                    )
                    report["sales"] = _result_dict(result)
                except market_history.DownloadError as exc:
                    if exc.status_code == 403:
                        report["sales"] = {
                            "status": "skipped",
                            "reason": "enterprise_export_not_authorized",
                            "http_status": 403,
                        }
                    else:
                        report["sales"] = _safe_failure(exc)
                except (
                    market_history.MarketHistoryError,
                    OSError,
                    sqlite3.Error,
                ) as exc:
                    report["sales"] = _safe_failure(exc)
        else:
            report["sales"] = {"status": "disabled"}

        try:
            report["usd_backfill"] = market_history.backfill_missing_usd_prices(
                connection
            )
        except (market_history.MarketHistoryError, sqlite3.Error, OSError) as exc:
            report["usd_backfill"] = _safe_failure(exc)

        outcomes: list[str] = []
        for snapshot in (
            report["snapshots"].values()
            if isinstance(report["snapshots"], dict)
            else ()
        ):
            if isinstance(snapshot, dict) and "status" in snapshot:
                outcomes.append(str(snapshot["status"]))
        if isinstance(report["sales"], dict):
            outcomes.append(str(report["sales"].get("status", "error")))
        report["state"] = (
            "ready"
            if any(value in {"complete", "skipped", "disabled"} for value in outcomes)
            else "degraded"
        )
        report["completed_at"] = market_history.utc_now()
        return report
    finally:
        connection.close()


def _atomic_status(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except (OSError, TypeError, ValueError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def run_loop(settings: WorkerSettings, *, session: Any = None) -> int:
    """Run daily by default and update a secret-free heartbeat while waiting."""

    stop_event = threading.Event()

    def stop_handler(_signum: int, _frame: Any) -> None:
        stop_event.set()

    previous_term = signal.signal(signal.SIGTERM, stop_handler)
    previous_int = signal.signal(signal.SIGINT, stop_handler)
    import windows_service_control
    windows_service_control.register_stop_callback(stop_event.set)
    started_at = market_history.utc_now()
    last_report: dict[str, Any] | None = None
    try:
        while not stop_event.is_set():
            last_report = run_once(settings, session=session)
            status = {
                "schema_version": 1,
                "pid": os.getpid(),
                "state": last_report["state"],
                "started_at": started_at,
                "heartbeat": market_history.utc_now(),
                "last_cycle": last_report,
                "next_cycle_in_seconds": settings.refresh_seconds,
            }
            _atomic_status(settings.status_path, status)
            if last_report["state"] in {"disabled", "blocked"}:
                return 0 if last_report["state"] == "disabled" else 2
            remaining = settings.refresh_seconds
            while remaining > 0 and not stop_event.is_set():
                interval = min(60, remaining)
                stop_event.wait(interval)
                remaining -= interval
                status["heartbeat"] = market_history.utc_now()
                status["state"] = "stopping" if stop_event.is_set() else "sleeping"
                status["next_cycle_in_seconds"] = max(remaining, 0)
                _atomic_status(settings.status_path, status)
        return 0
    finally:
        if last_report is not None:
            try:
                _atomic_status(
                    settings.status_path,
                    {
                        "schema_version": 1,
                        "pid": os.getpid(),
                        "state": "stopped",
                        "started_at": started_at,
                        "heartbeat": market_history.utc_now(),
                        "last_cycle": last_report,
                        "next_cycle_in_seconds": None,
                    },
                )
            except OSError:
                pass
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="override CS2_MARKET_HISTORY_DB_PATH",
    )
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--once", action="store_true", help="run one automatic cycle")
    actions.add_argument("--loop", action="store_true", help="run periodic cycles")
    actions.add_argument(
        "--migrate-only",
        action="store_true",
        help="initialize/upgrade the isolated database without network access",
    )
    actions.add_argument(
        "--import-zip",
        type=Path,
        metavar="PATH",
        help="privately import a previously obtained Enterprise ZIP",
    )
    actions.add_argument(
        "--import-ecb-xml",
        type=Path,
        metavar="PATH",
        help="offline import of a previously downloaded ECB XML fixture",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = WorkerSettings.from_environment()
    if args.db is not None:
        settings = WorkerSettings(**{**settings.__dict__, "database_path": args.db})
    if args.migrate_only:
        connection = market_history.connect_database(settings.database_path)
        connection.close()
        print(
            json.dumps(
                {
                    "status": "ready",
                    "schema_version": market_history.SCHEMA_VERSION,
                    "database": str(settings.database_path),
                    "network_attempted": False,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.import_zip is not None:
        connection = market_history.connect_database(settings.database_path)
        try:
            payload = market_history.read_zip_file_bounded(args.import_zip)
            result = market_history.import_cspriceapi_sales_zip(
                connection,
                payload,
                source_filename=args.import_zip.name,
                source_uri="manual-private-import",
                contract=_confirmed_sales_contract(settings),
            )
            print(json.dumps(_result_dict(result), ensure_ascii=False))
            return 0
        except (market_history.MarketHistoryError, sqlite3.Error, OSError) as exc:
            print(json.dumps(_safe_failure(exc), ensure_ascii=False), file=sys.stderr)
            return 1
        finally:
            connection.close()
    if args.import_ecb_xml is not None:
        connection = market_history.connect_database(settings.database_path)
        try:
            payload = market_history.read_ecb_xml_file_bounded(args.import_ecb_xml)
            saved = market_history.import_ecb_reference_rates(connection, payload)
            print(json.dumps({"status": "complete", "saved_rows": saved}))
            return 0
        except (market_history.MarketHistoryError, sqlite3.Error, OSError) as exc:
            print(json.dumps(_safe_failure(exc), ensure_ascii=False), file=sys.stderr)
            return 1
        finally:
            connection.close()
    if args.loop:
        return run_loop(settings)
    report = run_once(settings)
    print(json.dumps(report, ensure_ascii=False))
    return 2 if report["state"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
