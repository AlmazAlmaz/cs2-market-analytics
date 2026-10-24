"""Bounded read-only contracts intended for dashboard/API integration.

These functions never train or backtest during a request. They only read the
latest precomputed rows from the Trend store.

``get_trend_summary`` keys::

    status, schema_version, model_version, as_of_day, generated_at,
    requested_name, items[].{market_hash_name,current_price_cents,
    last_observation_day,history_rows,forecasts}

``get_backtest_report`` returns the persisted report contract plus
``report_id``. When no result exists both functions return ``status`` equal to
``unavailable`` and a stable ``reason``.

Collection contracts are also precomputed-only: ``list_collections`` returns
a bounded leaderboard, ``get_collection_detail`` returns one bounded index and
member view, and ``get_skin_daily_series`` returns one bounded price history.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import app_config
import portable_os

from .backtest import BacktestConfig
from .storage import SCHEMA_VERSION, canonical_sha256

DEFAULT_STATUS_PATH = app_config.path(
    "CS2_TREND_STATUS_PATH", "trend_worker_status.json"
)
MIN_COLLECTION_MEMBER_COVERAGE = 0.20


def get_trend_pipeline_status(
    status_path: str | Path = DEFAULT_STATUS_PATH,
) -> dict[str, Any]:
    """Read worker readiness/freshness without starting any work."""

    path = Path(status_path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "state": "unavailable",
            "pid": None,
            "heartbeat": None,
            "phase": "not_started",
            "progress_percent": 0.0,
            "last_error": None,
            "last_success": None,
            "next_refresh": None,
            "heartbeat_age_seconds": None,
            "last_success_age_seconds": None,
            "process_alive": False,
            "heartbeat_fresh": False,
            "data_fresh": False,
            "fresh": False,
        }
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "state": "degraded",
            "pid": None,
            "heartbeat": None,
            "phase": "status_error",
            "progress_percent": 0.0,
            "last_error": {"message": f"{type(exc).__name__}: {exc}"},
            "last_success": None,
            "next_refresh": None,
            "heartbeat_age_seconds": None,
            "last_success_age_seconds": None,
            "process_alive": False,
            "heartbeat_fresh": False,
            "data_fresh": False,
            "fresh": False,
        }
    heartbeat = value.get("heartbeat")
    age_seconds: float | None = None
    try:
        parsed = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_seconds = max(
            0.0,
            (
                datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
            ).total_seconds(),
        )
    except (TypeError, ValueError):
        pass
    last_success_age: float | None = None
    try:
        last_success = datetime.fromisoformat(
            str(value.get("last_success")).replace("Z", "+00:00")
        )
        if last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=timezone.utc)
        last_success_age = max(
            0.0,
            (
                datetime.now(timezone.utc) - last_success.astimezone(timezone.utc)
            ).total_seconds(),
        )
    except (TypeError, ValueError):
        pass
    pid = value.get("pid")
    process_alive = portable_os.process_alive(pid)
    value["heartbeat_age_seconds"] = (
        round(age_seconds, 3) if age_seconds is not None else None
    )
    value["last_success_age_seconds"] = (
        round(last_success_age, 3) if last_success_age is not None else None
    )
    value["process_alive"] = process_alive
    value["heartbeat_fresh"] = bool(
        process_alive and age_seconds is not None and age_seconds <= 90
    )
    refresh_interval = value.get("refresh_interval_seconds")
    try:
        freshness_limit = max(1_800, float(refresh_interval) * 2)
    except (TypeError, ValueError):
        freshness_limit = 43_200
    value["data_fresh"] = bool(
        last_success_age is not None and last_success_age <= freshness_limit
    )
    value["fresh"] = value["data_fresh"]
    return value


@contextmanager
def _connection(
    conn_or_path: sqlite3.Connection | str | Path,
) -> Iterator[sqlite3.Connection | None]:
    if isinstance(conn_or_path, sqlite3.Connection):
        previous_factory = conn_or_path.row_factory
        conn_or_path.row_factory = sqlite3.Row
        try:
            yield conn_or_path
        finally:
            conn_or_path.row_factory = previous_factory
        return
    path = Path(conn_or_path).expanduser().resolve()
    if not path.is_file():
        yield None
        return
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
    )


def _collections_are_materialized(conn: sqlite3.Connection) -> bool:
    if not all(
        _has_table(conn, table_name)
        for table_name in (
            "trend_skin_catalog",
            "trend_collection_daily",
            "trend_store_meta",
        )
    ):
        return False
    return bool(
        conn.execute(
            "SELECT 1 FROM trend_store_meta WHERE key='collection_source'"
        ).fetchone()
    )


def _bounded_integer(value: int, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _iso_day(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _return_percent(current: Any, previous: Any) -> float | None:
    if current is None or previous is None:
        return None
    current_value = float(current)
    previous_value = float(previous)
    if previous_value <= 0:
        return None
    return round((current_value / previous_value - 1.0) * 100.0, 4)


def _collection_direction(item: Mapping[str, Any]) -> str:
    change = item.get("return_30d")
    if change is None:
        change = item.get("return_7d")
    if change is None:
        change = item.get("return_90d")
    if change is None or abs(float(change)) < 0.5:
        return "flat"
    return "up" if float(change) > 0 else "down"


def _collection_summary_query(
    conn: sqlite3.Connection,
    *,
    conditions: list[str],
    params: list[Any],
    limit: int,
    offset: int = 0,
) -> list[sqlite3.Row]:
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return list(
        conn.execute(
            f"""
            WITH latest_day AS (
                SELECT collection_name, MAX(day) AS day
                FROM trend_collection_daily
                GROUP BY collection_name
            ),
            latest AS (
                SELECT d.*
                FROM trend_collection_daily AS d
                JOIN latest_day AS x
                  ON x.collection_name=d.collection_name AND x.day=d.day
            ),
            catalog AS (
                SELECT collection_name,
                       COUNT(*) AS member_count,
                       SUM(observed_sales_count) AS observed_sales_count,
                       SUM(unique_asset_count) AS unique_asset_count,
                       SUM(COALESCE(latest_reference_quantity, 0))
                           AS latest_reference_quantity,
                       SUM(CASE WHEN latest_reference_quantity IS NOT NULL
                                THEN 1 ELSE 0 END) AS quantity_member_count,
                       MAX(latest_reference_at) AS latest_reference_at,
                       CASE WHEN SUM(observed_sales_count) > 0
                            THEN SUM(mapping_coverage * observed_sales_count)
                                 / SUM(observed_sales_count)
                            ELSE 0 END AS mapping_coverage
                FROM trend_skin_catalog
                GROUP BY collection_name
            )
            SELECT l.collection_name,
                   l.day AS as_of_day,
                   l.index_value AS current_index,
                   l.breadth_up,
                   l.member_count AS active_member_count,
                   l.member_coverage,
                   l.refreshed_at,
                   c.member_count,
                   c.observed_sales_count,
                   c.unique_asset_count,
                   c.latest_reference_quantity,
                   c.quantity_member_count,
                   c.latest_reference_at,
                   c.mapping_coverage,
                   (SELECT h.index_value
                    FROM trend_collection_daily AS h
                    WHERE h.collection_name=l.collection_name
                      AND h.day <= DATE(l.day, '-7 day')
                    ORDER BY h.day DESC LIMIT 1) AS index_7d,
                   (SELECT h.index_value
                    FROM trend_collection_daily AS h
                    WHERE h.collection_name=l.collection_name
                      AND h.day <= DATE(l.day, '-30 day')
                    ORDER BY h.day DESC LIMIT 1) AS index_30d,
                   (SELECT h.index_value
                    FROM trend_collection_daily AS h
                    WHERE h.collection_name=l.collection_name
                      AND h.day <= DATE(l.day, '-90 day')
                    ORDER BY h.day DESC LIMIT 1) AS index_90d,
                   (SELECT h.member_coverage
                    FROM trend_collection_daily AS h
                    WHERE h.collection_name=l.collection_name
                      AND h.day <= DATE(l.day, '-7 day')
                    ORDER BY h.day DESC LIMIT 1) AS coverage_7d,
                   (SELECT h.member_coverage
                    FROM trend_collection_daily AS h
                    WHERE h.collection_name=l.collection_name
                      AND h.day <= DATE(l.day, '-30 day')
                    ORDER BY h.day DESC LIMIT 1) AS coverage_30d,
                   (SELECT h.member_coverage
                    FROM trend_collection_daily AS h
                    WHERE h.collection_name=l.collection_name
                      AND h.day <= DATE(l.day, '-90 day')
                    ORDER BY h.day DESC LIMIT 1) AS coverage_90d,
                   (SELECT COALESCE(SUM(h.sales_count), 0)
                    FROM trend_collection_daily AS h
                    WHERE h.collection_name=l.collection_name
                      AND h.day >= DATE(l.day, '-29 day')
                      AND h.day <= l.day) AS sales_30d,
                   (SELECT COALESCE(SUM(h.notional_cents), 0)
                    FROM trend_collection_daily AS h
                    WHERE h.collection_name=l.collection_name
                      AND h.day >= DATE(l.day, '-29 day')
                      AND h.day <= l.day) AS notional_30d_cents
            FROM latest AS l
            JOIN catalog AS c USING (collection_name)
            {where}
            ORDER BY c.observed_sales_count DESC, l.collection_name ASC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )
    )


def _collection_item(row: Mapping[str, Any]) -> dict[str, Any]:
    current = float(row["current_index"])
    current_coverage = float(row["member_coverage"])

    def covered_return(index_key: str, coverage_key: str) -> float | None:
        comparison_coverage = row[coverage_key]
        if (
            current_coverage < MIN_COLLECTION_MEMBER_COVERAGE
            or comparison_coverage is None
            or float(comparison_coverage) < MIN_COLLECTION_MEMBER_COVERAGE
        ):
            return None
        return _return_percent(current, row[index_key])

    item = {
        "collection_name": str(row["collection_name"]),
        "member_count": int(row["member_count"]),
        "observed_sales_count": int(row["observed_sales_count"]),
        "unique_asset_count": int(row["unique_asset_count"]),
        "latest_reference_quantity": int(row["latest_reference_quantity"]),
        "quantity_member_count": int(row["quantity_member_count"]),
        "latest_reference_at": (
            str(row["latest_reference_at"])
            if row["latest_reference_at"] is not None
            else None
        ),
        "mapping_coverage": round(float(row["mapping_coverage"]), 6),
        "as_of_day": str(row["as_of_day"]),
        "current_index": round(current, 6),
        "return_7d": covered_return("index_7d", "coverage_7d"),
        "return_30d": covered_return("index_30d", "coverage_30d"),
        "return_90d": covered_return("index_90d", "coverage_90d"),
        "coverage_7d": (
            round(float(row["coverage_7d"]), 6)
            if row["coverage_7d"] is not None
            else None
        ),
        "coverage_30d": (
            round(float(row["coverage_30d"]), 6)
            if row["coverage_30d"] is not None
            else None
        ),
        "coverage_90d": (
            round(float(row["coverage_90d"]), 6)
            if row["coverage_90d"] is not None
            else None
        ),
        "sales_30d": int(row["sales_30d"]),
        "notional_30d_cents": int(row["notional_30d_cents"]),
        "breadth_up": (
            round(float(row["breadth_up"]), 4)
            if row["breadth_up"] is not None
            else None
        ),
        "active_member_count": int(row["active_member_count"]),
        "member_coverage": round(current_coverage, 6),
        "minimum_member_coverage": MIN_COLLECTION_MEMBER_COVERAGE,
        "refreshed_at": str(row["refreshed_at"]),
    }
    coverage_30d = item["coverage_30d"]
    item["direction"] = (
        "insufficient_coverage"
        if current_coverage < MIN_COLLECTION_MEMBER_COVERAGE
        or coverage_30d is None
        or float(coverage_30d) < MIN_COLLECTION_MEMBER_COVERAGE
        else _collection_direction(item)
    )
    return item


def list_collections(
    conn_or_path: sqlite3.Connection | str | Path,
    *,
    search: str | None = None,
    include_unknown: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Return a bounded collection leaderboard from materialized rows only."""

    limit = _bounded_integer(limit, "limit", minimum=1, maximum=500)
    offset = _bounded_integer(offset, "offset", minimum=0, maximum=100_000)
    requested_search = str(search or "").strip()
    if len(requested_search) > 200:
        raise ValueError("search must be at most 200 characters")
    conditions: list[str] = []
    params: list[Any] = []
    if requested_search:
        conditions.append("l.collection_name LIKE ? ESCAPE '\\' COLLATE NOCASE")
        params.append(f"%{_escape_like(requested_search)}%")
    if not include_unknown:
        conditions.append("l.collection_name <> 'Unknown'")

    with _connection(conn_or_path) as conn:
        if conn is None or not _collections_are_materialized(conn):
            return {
                "status": "unavailable",
                "reason": "collection analytics have not been materialized",
                "schema_version": SCHEMA_VERSION,
                "items": [],
                "total_count": 0,
                "limit": limit,
                "offset": offset,
                "has_more": False,
            }
        rows = _collection_summary_query(
            conn,
            conditions=conditions,
            params=params,
            limit=limit + 1,
            offset=offset,
        )
        count_conditions: list[str] = []
        count_params: list[Any] = []
        if requested_search:
            count_conditions.append("collection_name LIKE ? ESCAPE '\\' COLLATE NOCASE")
            count_params.append(f"%{_escape_like(requested_search)}%")
        if not include_unknown:
            count_conditions.append("collection_name <> 'Unknown'")
        count_where = (
            f"WHERE {' AND '.join(count_conditions)}" if count_conditions else ""
        )
        total_count = int(
            conn.execute(
                f"""
                SELECT COUNT(DISTINCT collection_name)
                FROM trend_collection_daily
                {count_where}
                """,
                count_params,
            ).fetchone()[0]
        )

    has_more = len(rows) > limit
    items = [_collection_item(row) for row in rows[:limit]]
    return {
        "status": "ready",
        "reason": None,
        "schema_version": SCHEMA_VERSION,
        "items": items,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }


def get_collection_detail(
    conn_or_path: sqlite3.Connection | str | Path,
    collection_name: str,
    *,
    days: int = 365,
    member_limit: int = 200,
) -> dict[str, Any]:
    """Read a collection summary, index series, and bounded constituent list."""

    requested_name = str(collection_name or "").strip()
    if not requested_name:
        raise ValueError("collection_name is required")
    if len(requested_name) > 300:
        raise ValueError("collection_name must be at most 300 characters")
    days = _bounded_integer(days, "days", minimum=1, maximum=5_000)
    member_limit = _bounded_integer(
        member_limit, "member_limit", minimum=1, maximum=1_000
    )
    with _connection(conn_or_path) as conn:
        if conn is None or not _collections_are_materialized(conn):
            return {
                "status": "unavailable",
                "reason": "collection analytics have not been materialized",
                "schema_version": SCHEMA_VERSION,
                "collection_name": requested_name,
                "summary": None,
                "series": [],
                "members": [],
            }
        summary_rows = _collection_summary_query(
            conn,
            conditions=["l.collection_name = ?"],
            params=[requested_name],
            limit=1,
        )
        if not summary_rows:
            return {
                "status": "not_found",
                "reason": "collection was not found",
                "schema_version": SCHEMA_VERSION,
                "collection_name": requested_name,
                "summary": None,
                "series": [],
                "members": [],
            }
        as_of_day = str(summary_rows[0]["as_of_day"])
        first_series_day = (
            date.fromisoformat(as_of_day) - timedelta(days=days - 1)
        ).isoformat()
        series_rows = list(
            conn.execute(
                """
                SELECT day, index_value, sales_count, notional_cents,
                       member_count, total_member_count, member_coverage,
                       breadth_up, comparable_member_count
                FROM trend_collection_daily
                WHERE collection_name=?
                  AND day>=?
                  AND day<=?
                ORDER BY day ASC
                LIMIT ?
                """,
                (requested_name, first_series_day, as_of_day, days + 1),
            )
        )
        series_truncated = bool(
            conn.execute(
                """
                SELECT 1 FROM trend_collection_daily
                WHERE collection_name=? AND day<?
                LIMIT 1
                """,
                (requested_name, first_series_day),
            ).fetchone()
        )
        member_rows = list(
            conn.execute(
                """
                SELECT c.market_hash_name,
                       c.collection_name,
                       c.observed_sales_count,
                       c.unique_asset_count,
                       c.min_float,
                       c.avg_float,
                       c.max_float,
                       c.latest_reference_quantity,
                       c.latest_reference_at,
                       c.icon_url,
                       c.mapping_coverage,
                       c.first_observed_day,
                       c.last_observed_day,
                       (SELECT d.median_price_cents
                        FROM trend_daily_market AS d
                        WHERE d.market_hash_name=c.market_hash_name
                        ORDER BY d.day DESC LIMIT 1) AS current_median_price_cents,
                       (SELECT d.median_price_cents
                        FROM trend_daily_market AS d
                        WHERE d.market_hash_name=c.market_hash_name
                          AND d.day <= DATE(c.last_observed_day, '-7 day')
                        ORDER BY d.day DESC LIMIT 1) AS price_7d,
                       (SELECT d.median_price_cents
                        FROM trend_daily_market AS d
                        WHERE d.market_hash_name=c.market_hash_name
                          AND d.day <= DATE(c.last_observed_day, '-30 day')
                        ORDER BY d.day DESC LIMIT 1) AS price_30d,
                       (SELECT COALESCE(SUM(d.sales_count), 0)
                        FROM trend_daily_market AS d
                        WHERE d.market_hash_name=c.market_hash_name
                          AND d.day >= DATE(c.last_observed_day, '-29 day')
                          AND d.day <= c.last_observed_day) AS sales_30d
                FROM trend_skin_catalog AS c
                WHERE c.collection_name=?
                ORDER BY c.observed_sales_count DESC, c.market_hash_name ASC
                LIMIT ?
                """,
                (requested_name, member_limit + 1),
            )
        )

    member_truncated = len(member_rows) > member_limit
    series = [dict(row) for row in series_rows[:days]]
    members = []
    for row in member_rows[:member_limit]:
        member = dict(row)
        price_7d = member.pop("price_7d")
        price_30d = member.pop("price_30d")
        current_price = member["current_median_price_cents"]
        member["current_price_cents"] = current_price
        member["return_7d"] = _return_percent(current_price, price_7d)
        member["return_30d"] = _return_percent(current_price, price_30d)
        member["sales_30d"] = int(member["sales_30d"])
        member["direction"] = _collection_direction(member)
        members.append(member)
    return {
        "status": "ready",
        "reason": None,
        "schema_version": SCHEMA_VERSION,
        "collection_name": requested_name,
        "summary": _collection_item(summary_rows[0]),
        "series": series,
        "series_truncated": series_truncated,
        "members": members,
        "members_truncated": member_truncated,
    }


def get_skin_daily_series(
    conn_or_path: sqlite3.Connection | str | Path,
    market_hash_name: str,
    *,
    days: int = 365,
    start_day: str | None = None,
    end_day: str | None = None,
) -> dict[str, Any]:
    """Read the most recent bounded daily price series for one exact skin."""

    requested_name = str(market_hash_name or "").strip()
    if not requested_name:
        raise ValueError("market_hash_name is required")
    if len(requested_name) > 500:
        raise ValueError("market_hash_name must be at most 500 characters")
    days = _bounded_integer(days, "days", minimum=1, maximum=5_000)
    normalized_start = _iso_day(start_day, "start_day")
    normalized_end = _iso_day(end_day, "end_day")
    if normalized_start and normalized_end and normalized_start > normalized_end:
        raise ValueError("start_day must not be after end_day")
    with _connection(conn_or_path) as conn:
        if conn is None or not _has_table(conn, "trend_daily_market"):
            return {
                "status": "unavailable",
                "reason": "daily skin analytics have not been materialized",
                "schema_version": SCHEMA_VERSION,
                "market_hash_name": requested_name,
                "catalog": None,
                "series": [],
            }
        latest_available_row = conn.execute(
            """
            SELECT MAX(day) AS day
            FROM trend_daily_market
            WHERE market_hash_name=?
            """,
            (requested_name,),
        ).fetchone()
        latest_available_day = (
            str(latest_available_row["day"])
            if latest_available_row and latest_available_row["day"]
            else None
        )
        window_end = normalized_end or latest_available_day
        rows: list[sqlite3.Row] = []
        truncated = False
        if window_end is not None:
            calendar_start = (
                date.fromisoformat(window_end) - timedelta(days=days - 1)
            ).isoformat()
            window_start = max(calendar_start, normalized_start or calendar_start)
            rows = list(
                conn.execute(
                    """
                    SELECT day, sales_count, median_price_cents, mean_price_cents,
                           min_price_cents, max_price_cents, p15_price_cents,
                           p85_price_cents, notional_cents, source_max_fetched_at
                    FROM trend_daily_market
                    WHERE market_hash_name=?
                      AND day>=?
                      AND day<=?
                    ORDER BY day ASC
                    LIMIT ?
                    """,
                    (requested_name, window_start, window_end, days + 1),
                )
            )
            truncated = bool(
                conn.execute(
                    """
                    SELECT 1 FROM trend_daily_market
                    WHERE market_hash_name=? AND day<?
                    LIMIT 1
                    """,
                    (requested_name, window_start),
                ).fetchone()
            )
        catalog_row = None
        if _has_table(conn, "trend_skin_catalog"):
            catalog_row = conn.execute(
                """
                SELECT market_hash_name, collection_name, observed_sales_count,
                       unique_asset_count, min_float, avg_float, max_float,
                       latest_reference_quantity, latest_reference_at, icon_url,
                       mapping_coverage, first_observed_day, last_observed_day
                FROM trend_skin_catalog
                WHERE market_hash_name=?
                """,
                (requested_name,),
            ).fetchone()

    if latest_available_day is None and catalog_row is None:
        return {
            "status": "not_found",
            "reason": "skin was not found",
            "schema_version": SCHEMA_VERSION,
            "market_hash_name": requested_name,
            "catalog": None,
            "series": [],
        }
    series = [dict(row) for row in rows[:days]]
    return {
        "status": "ready",
        "reason": None,
        "schema_version": SCHEMA_VERSION,
        "market_hash_name": requested_name,
        "catalog": dict(catalog_row) if catalog_row is not None else None,
        "as_of_day": series[-1]["day"] if series else None,
        "series": series,
        "truncated": truncated,
    }


def get_trend_summary(
    conn_or_path: sqlite3.Connection | str | Path,
    name: str | None = None,
    *,
    ranking_horizon_days: int = 7,
    limit: int = 100,
) -> dict[str, Any]:
    """Read one skin or a bounded latest-trend leaderboard."""

    if ranking_horizon_days not in (7, 14, 30):
        raise ValueError("ranking_horizon_days must be 7, 14, or 30")
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    requested_name = str(name or "").strip() or None
    with _connection(conn_or_path) as conn:
        if conn is None or not _has_table(conn, "trend_forecasts"):
            return {
                "status": "unavailable",
                "reason": "trend forecasts have not been precomputed",
                "schema_version": SCHEMA_VERSION,
                "model_version": None,
                "as_of_day": None,
                "generated_at": None,
                "requested_name": requested_name,
                "items": [],
            }
        latest = conn.execute(
            """
            SELECT as_of_day, MAX(generated_at) AS generated_at,
                   MAX(model_version) AS model_version
            FROM trend_forecasts
            WHERE as_of_day=(SELECT MAX(as_of_day) FROM trend_forecasts)
            """
        ).fetchone()
        if not latest or not latest["as_of_day"]:
            return {
                "status": "unavailable",
                "reason": "trend forecasts have not been precomputed",
                "schema_version": SCHEMA_VERSION,
                "model_version": None,
                "as_of_day": None,
                "generated_at": None,
                "requested_name": requested_name,
                "items": [],
            }
        as_of_day = latest["as_of_day"]
        if requested_name:
            names = [requested_name]
        else:
            names = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT market_hash_name
                    FROM trend_forecasts
                    WHERE as_of_day=? AND horizon_days=?
                    ORDER BY downside_return_percent DESC,
                             expected_return_percent DESC,
                             market_hash_name ASC
                    LIMIT ?
                    """,
                    (as_of_day, ranking_horizon_days, limit),
                )
            ]
        if not names:
            rows: list[sqlite3.Row] = []
        else:
            placeholders = ",".join("?" for _ in names)
            rows = list(
                conn.execute(
                    f"""
                    SELECT payload_json
                    FROM trend_forecasts
                    WHERE as_of_day=?
                      AND market_hash_name IN ({placeholders})
                    ORDER BY market_hash_name, horizon_days
                    """,
                    [as_of_day, *names],
                )
            )

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        forecast = json.loads(row["payload_json"])
        item_name = forecast["market_hash_name"]
        item = grouped.setdefault(
            item_name,
            {
                "market_hash_name": item_name,
                "current_price_cents": forecast["current_price_cents"],
                "last_observation_day": forecast["last_observation_day"],
                "history_rows": forecast["history_rows"],
                "forecasts": {},
            },
        )
        horizon = str(forecast["horizon_days"])
        item["forecasts"][horizon] = {
            "target_day": forecast["target_day"],
            "predicted_price_cents": forecast["predicted_price_cents"],
            "low_price_cents": forecast["low_price_cents"],
            "high_price_cents": forecast["high_price_cents"],
            "expected_return_percent": forecast["expected_return_percent"],
            "downside_return_percent": forecast["downside_return_percent"],
            "probability_up": forecast["probability_up"],
            "confidence": forecast["confidence"],
        }
    ordered_items = [grouped[value] for value in names if value in grouped]
    return {
        "status": "ready",
        "reason": None,
        "schema_version": SCHEMA_VERSION,
        "model_version": latest["model_version"],
        "as_of_day": as_of_day,
        "generated_at": latest["generated_at"],
        "requested_name": requested_name,
        "items": ordered_items,
    }


def get_backtest_report(
    conn_or_path: sqlite3.Connection | str | Path,
    config: BacktestConfig | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the latest report, optionally for one exact config."""

    config_value: dict[str, Any] | None
    if isinstance(config, BacktestConfig):
        config_value = config.to_dict()
    elif config is None:
        config_value = None
    else:
        config_value = dict(config)
    with _connection(conn_or_path) as conn:
        if conn is None or not _has_table(conn, "trend_backtest_reports"):
            return {
                "status": "unavailable",
                "reason": "matching backtest has not been precomputed",
                "report_id": None,
                "config": config_value,
            }
        if config_value is None:
            row = conn.execute(
                """
                SELECT report_id, report_json
                FROM trend_backtest_reports
                ORDER BY generated_at DESC, report_id DESC
                LIMIT 1
                """
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT report_id, report_json
                FROM trend_backtest_reports
                WHERE config_sha256=?
                ORDER BY generated_at DESC, report_id DESC
                LIMIT 1
                """,
                (canonical_sha256(config_value),),
            ).fetchone()
    if not row:
        return {
            "status": "unavailable",
            "reason": "matching backtest has not been precomputed",
            "report_id": None,
            "config": config_value,
        }
    report = json.loads(row["report_json"])
    report["report_id"] = row["report_id"]
    return report
