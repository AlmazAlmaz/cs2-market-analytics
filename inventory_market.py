"""Safe, read-only market helpers for a user's inventory.

The derived trend store contains historical sales and model output.  It does
not contain a live order book, so every public result labels prices as
historical/model benchmarks rather than live asks.

The module deliberately has no dependency on the account database.  Callers
pass an explicit, bounded collection of skin names and can therefore enrich
inventory rows without joining private data into the market database.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_SEARCH_LIMIT = 20
MAX_SEARCH_QUERY_LENGTH = 120
MAX_BATCH_NAMES = 200
MAX_MARKET_HASH_NAME_LENGTH = 300

HISTORICAL_BENCHMARK_NOTE = (
    "Цена рассчитана по историческим продажам и, где указано, прогнозу модели. "
    "Это не текущий live ask и не гарантия продажи."
)
SUPPLY_NOTE = (
    "Количество — последний справочный snapshot CSFloat, а строки/asset ID — "
    "только наблюдения этой базы. Это не число предметов в мире."
)
SPECIAL_PREMIUM_NOTE = (
    "Редкий float, ценные стикеры и их расположение не включены в базовый "
    "ориентир: возможную премию нужно проверить вручную."
)

_DAILY_COLUMNS = frozenset(
    {
        "market_hash_name",
        "day",
        "sales_count",
        "median_price_cents",
        "p15_price_cents",
        "p85_price_cents",
    }
)
_CATALOG_COLUMNS = frozenset(
    {
        "market_hash_name",
        "collection_name",
        "observed_sales_count",
        "unique_asset_count",
        "latest_reference_quantity",
        "latest_reference_at",
        "icon_url",
    }
)
_FORECAST_COLUMNS = frozenset(
    {
        "as_of_day",
        "market_hash_name",
        "horizon_days",
        "current_price_cents",
        "predicted_price_cents",
        "low_price_cents",
        "high_price_cents",
        "expected_return_percent",
        "downside_return_percent",
        "probability_up",
        "confidence",
        "history_rows",
        "last_observation_day",
    }
)

_STRATEGY_ALIASES = {
    "quick": "quick_sale",
    "quick_sale": "quick_sale",
    "fast": "quick_sale",
    "flip": "quick_sale",
    "balanced": "balanced",
    "balance": "balanced",
    "patient": "patient",
    "max_profit": "patient",
    "collection": "patient",
    "investment": "investment",
    "invest": "investment",
    "hold": "investment",
    # Account inventory categories use the balanced historical benchmark;
    # their item-specific premium remains a separate manual decision.
    "float": "balanced",
    "stickers": "balanced",
    "manual": "balanced",
}
_MANUAL_PREMIUM_STRATEGIES = frozenset({"float", "stickers", "manual"})


def _connect_read_only(path: str | Path) -> sqlite3.Connection | None:
    """Open an existing SQLite file without ever creating or mutating it."""

    try:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            return None
        conn = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        # Prevent application-defined functions in a hostile schema from being
        # trusted.  Older SQLite builds may not support this pragma.
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
        except sqlite3.DatabaseError:
            pass
        conn.execute("BEGIN")
        return conn
    except (OSError, sqlite3.Error):
        try:
            conn.close()  # type: ignore[possibly-undefined]
        except (NameError, sqlite3.Error):
            pass
        return None


def _close_read_only(conn: sqlite3.Connection | None) -> None:
    if conn is None:
        return
    try:
        conn.rollback()
    except sqlite3.Error:
        pass
    conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return columns only for a real table, using parameterized introspection."""

    if not conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
        (table,),
    ).fetchone():
        return set()
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))
    }


def _bounded_int(value: Any, *, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if str(value).strip() != str(result) and not isinstance(value, int):
        # Do not silently truncate floats such as 1.5.
        try:
            if float(value) != result:
                raise ValueError(f"{name} must be an integer")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _nonnegative_int(value: Any) -> int | None:
    number = _finite_float(value)
    if number is None or number < 0:
        return None
    return round(number)


def _positive_price(value: Any) -> int | None:
    result = _nonnegative_int(value)
    return result if result is not None and result > 0 else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _safe_icon_url(value: Any) -> str | None:
    url = _optional_text(value)
    if url is None or len(url) > 2048:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _valid_iso_day(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return text


def _normalize_names(market_hash_names: Iterable[str]) -> list[str]:
    if isinstance(market_hash_names, (str, bytes)):
        raise TypeError("market_hash_names must be an iterable of names")
    try:
        raw_names = list(market_hash_names)
    except TypeError as exc:
        raise ValueError("market_hash_names must be an iterable of names") from exc
    if len(raw_names) > MAX_BATCH_NAMES:
        raise ValueError(
            f"market_hash_names may contain at most {MAX_BATCH_NAMES} entries"
        )

    names: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_names:
        if not isinstance(raw_name, str):
            raise TypeError("each market hash name must be a string")
        name = raw_name.strip()
        if not name or "\x00" in name:
            raise ValueError("market hash names must be non-empty")
        if len(name) > MAX_MARKET_HASH_NAME_LENGTH:
            raise ValueError(
                f"market hash names may be at most {MAX_MARKET_HASH_NAME_LENGTH} characters"
            )
        if name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _escaped_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_skin_names(
    db_path: str | Path,
    query: str = "",
    *,
    limit: int = MAX_SEARCH_LIMIT,
) -> list[str]:
    """Search known skin names with a hard maximum of twenty results.

    Missing/corrupt databases and schemas that cannot provide a name column
    fail closed to an empty list.  Percent and underscore in ``query`` are
    treated literally rather than as caller-controlled SQL wildcards.
    """

    limit_value = _bounded_int(
        limit,
        minimum=1,
        maximum=MAX_SEARCH_LIMIT,
        name="limit",
    )
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    query_value = query.strip()
    if len(query_value) > MAX_SEARCH_QUERY_LENGTH:
        raise ValueError(
            f"query may be at most {MAX_SEARCH_QUERY_LENGTH} characters"
        )

    conn = _connect_read_only(db_path)
    if conn is None:
        return []
    try:
        sources: list[str] = []
        if "market_hash_name" in _table_columns(conn, "trend_skin_catalog"):
            sources.append("SELECT market_hash_name FROM trend_skin_catalog")
        if "market_hash_name" in _table_columns(conn, "trend_daily_market"):
            sources.append("SELECT market_hash_name FROM trend_daily_market")
        if not sources:
            return []

        exact = query_value
        contains = f"%{_escaped_like(query_value)}%"
        prefix = f"{_escaped_like(query_value)}%"
        rows = conn.execute(
            f"""
            WITH known_names AS ({' UNION '.join(sources)})
            SELECT market_hash_name
            FROM known_names
            WHERE market_hash_name LIKE ? ESCAPE '\\' COLLATE NOCASE
            ORDER BY
                CASE
                    WHEN market_hash_name = ? COLLATE NOCASE THEN 0
                    WHEN market_hash_name LIKE ? ESCAPE '\\' COLLATE NOCASE THEN 1
                    ELSE 2
                END,
                market_hash_name COLLATE NOCASE,
                market_hash_name
            LIMIT ?
            """,
            (contains, exact, prefix, limit_value),
        ).fetchall()
        return [str(row[0]) for row in rows if _optional_text(row[0])]
    except sqlite3.Error:
        return []
    finally:
        _close_read_only(conn)


def _empty_snapshot(name: str) -> dict[str, Any]:
    supply = {
        "latest_reference_quantity": None,
        "latest_reference_at": None,
        "observed_sales_count": None,
        "unique_asset_count": None,
        "is_world_inventory": False,
        "label": "Справочные наблюдения, не мировой supply",
        "note": SUPPLY_NOTE,
    }
    historical = {
        "market_as_of": None,
        "median_price_cents": None,
        "p15_price_cents": None,
        "p85_price_cents": None,
        "sales_30d": None,
        "is_live_ask": False,
        "label": "Исторический benchmark, не live ask",
        "note": HISTORICAL_BENCHMARK_NOTE,
    }
    return {
        "market_hash_name": name,
        "market_data_available": False,
        "market_as_of": None,
        "market_store_as_of": None,
        "latest_historical_day": None,
        "current_price_cents": None,
        "market_median_cents": None,
        "median_price_cents": None,
        "p15_price_cents": None,
        "p85_price_cents": None,
        "sales_30d": None,
        "forecast": None,
        "forecast_30d": None,
        "collection_name": None,
        "icon_url": None,
        "latest_reference_quantity": None,
        "latest_reference_at": None,
        "observed_sales_count": None,
        "unique_asset_count": None,
        "price_is_live_ask": False,
        "pricing_label": "Исторический benchmark, не live ask",
        "benchmark_note": HISTORICAL_BENCHMARK_NOTE,
        "supply_label": "Справочные наблюдения, не мировой supply",
        "supply_note": SUPPLY_NOTE,
        "special_premium_note": SPECIAL_PREMIUM_NOTE,
        "historical": historical,
        "supply": supply,
    }


def _daily_rows(
    conn: sqlite3.Connection,
    names: list[str],
) -> tuple[str | None, dict[str, sqlite3.Row]]:
    if not _DAILY_COLUMNS.issubset(_table_columns(conn, "trend_daily_market")):
        return None, {}
    placeholders = ",".join("?" for _ in names)
    global_row = conn.execute(
        "SELECT MAX(day) FROM trend_daily_market"
    ).fetchone()
    market_store_as_of = _valid_iso_day(global_row[0] if global_row else None)
    if market_store_as_of is None:
        return None, {}

    rows = conn.execute(
        f"""
        WITH requested_latest AS (
            SELECT market_hash_name, MAX(day) AS latest_day
            FROM trend_daily_market
            WHERE market_hash_name IN ({placeholders})
            GROUP BY market_hash_name
        ), recent_sales AS (
            SELECT market_hash_name, SUM(sales_count) AS sales_30d
            FROM trend_daily_market
            WHERE market_hash_name IN ({placeholders})
              AND day >= DATE(?, '-29 day')
              AND day <= ?
            GROUP BY market_hash_name
        )
        SELECT d.market_hash_name, d.day, d.median_price_cents,
               d.p15_price_cents, d.p85_price_cents,
               COALESCE(r.sales_30d, 0) AS sales_30d
        FROM requested_latest l
        JOIN trend_daily_market d
          ON d.market_hash_name = l.market_hash_name AND d.day = l.latest_day
        LEFT JOIN recent_sales r ON r.market_hash_name = d.market_hash_name
        """,
        (*names, *names, market_store_as_of, market_store_as_of),
    ).fetchall()
    return market_store_as_of, {str(row["market_hash_name"]): row for row in rows}


def _catalog_rows(
    conn: sqlite3.Connection,
    names: list[str],
) -> dict[str, sqlite3.Row]:
    if not _CATALOG_COLUMNS.issubset(_table_columns(conn, "trend_skin_catalog")):
        return {}
    placeholders = ",".join("?" for _ in names)
    rows = conn.execute(
        f"""
        SELECT market_hash_name, collection_name, observed_sales_count,
               unique_asset_count, latest_reference_quantity,
               latest_reference_at, icon_url
        FROM trend_skin_catalog
        WHERE market_hash_name IN ({placeholders})
        """,
        names,
    ).fetchall()
    return {str(row["market_hash_name"]): row for row in rows}


def _forecast_rows(
    conn: sqlite3.Connection,
    names: list[str],
) -> dict[str, sqlite3.Row]:
    columns = _table_columns(conn, "trend_forecasts")
    if not _FORECAST_COLUMNS.issubset(columns):
        return {}
    placeholders = ",".join("?" for _ in names)
    generated_expression = (
        "f.generated_at" if "generated_at" in columns else "NULL"
    )
    model_expression = "f.model_version" if "model_version" in columns else "NULL"
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT market_hash_name, MAX(as_of_day) AS as_of_day
            FROM trend_forecasts
            WHERE horizon_days = 30
              AND market_hash_name IN ({placeholders})
            GROUP BY market_hash_name
        )
        SELECT f.market_hash_name, f.as_of_day, f.current_price_cents,
               f.predicted_price_cents, f.low_price_cents, f.high_price_cents,
               f.expected_return_percent, f.downside_return_percent,
               f.probability_up, f.confidence, f.history_rows,
               f.last_observation_day,
               {generated_expression} AS generated_at,
               {model_expression} AS model_version
        FROM trend_forecasts f
        JOIN latest l
          ON l.market_hash_name = f.market_hash_name
         AND l.as_of_day = f.as_of_day
        WHERE f.horizon_days = 30
        """,
        names,
    ).fetchall()
    return {str(row["market_hash_name"]): row for row in rows}


def _valid_daily(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    day = _valid_iso_day(row["day"])
    median = _positive_price(row["median_price_cents"])
    p15 = _positive_price(row["p15_price_cents"])
    p85 = _positive_price(row["p85_price_cents"])
    sales = _nonnegative_int(row["sales_30d"])
    if (
        day is None
        or median is None
        or p15 is None
        or p85 is None
        or sales is None
        or not p15 <= median <= p85
    ):
        return None
    return {
        "market_as_of": day,
        "median_price_cents": median,
        "p15_price_cents": p15,
        "p85_price_cents": p85,
        "sales_30d": sales,
        "is_live_ask": False,
        "label": "Исторический benchmark, не live ask",
        "note": HISTORICAL_BENCHMARK_NOTE,
    }


def _valid_forecast(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    as_of = _valid_iso_day(row["as_of_day"])
    last_observation = _valid_iso_day(row["last_observation_day"])
    current = _positive_price(row["current_price_cents"])
    predicted = _positive_price(row["predicted_price_cents"])
    low = _positive_price(row["low_price_cents"])
    high = _positive_price(row["high_price_cents"])
    expected = _finite_float(row["expected_return_percent"])
    downside = _finite_float(row["downside_return_percent"])
    probability = _finite_float(row["probability_up"])
    confidence = _finite_float(row["confidence"])
    history_rows = _nonnegative_int(row["history_rows"])
    if (
        as_of is None
        or last_observation is None
        or current is None
        or predicted is None
        or low is None
        or high is None
        or expected is None
        or downside is None
        or probability is None
        or confidence is None
        or history_rows is None
        or not low <= predicted <= high
        or not 0 <= probability <= 1
        or not 0 <= confidence <= 1
    ):
        return None
    return {
        "horizon_days": 30,
        "as_of_day": as_of,
        "generated_at": _optional_text(row["generated_at"]),
        "model_version": _optional_text(row["model_version"]),
        "current_price_cents": current,
        "predicted_price_cents": predicted,
        "low_price_cents": low,
        "high_price_cents": high,
        "expected_return_percent": expected,
        "downside_return_percent": downside,
        "probability_up": probability,
        "confidence": confidence,
        "history_rows": history_rows,
        "last_observation_day": last_observation,
        "is_live_ask": False,
        "label": "30-дневный прогноз модели, не live ask",
    }


def get_market_snapshots(
    db_path: str | Path,
    market_hash_names: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Return a bounded map of historical market snapshots by exact name.

    At most :data:`MAX_BATCH_NAMES` names are accepted.  Every requested name
    is returned, including names with no market data; unavailable numeric
    values are ``None`` and are never represented as a misleading zero price.
    Optional catalog/forecast tables are used only when their full read
    contract is present.
    """

    names = _normalize_names(market_hash_names)
    snapshots = {name: _empty_snapshot(name) for name in names}
    if not names:
        return snapshots

    conn = _connect_read_only(db_path)
    if conn is None:
        return snapshots
    try:
        market_store_as_of, daily_rows = _daily_rows(conn, names)
        catalog_rows = _catalog_rows(conn, names)
        forecast_rows = _forecast_rows(conn, names)
    except sqlite3.Error:
        return snapshots
    finally:
        _close_read_only(conn)

    for name, snapshot in snapshots.items():
        snapshot["market_store_as_of"] = market_store_as_of
        historical = _valid_daily(daily_rows.get(name))
        if historical is not None:
            snapshot.update(
                {
                    "market_data_available": True,
                    "market_as_of": historical["market_as_of"],
                    "latest_historical_day": historical["market_as_of"],
                    "current_price_cents": historical["median_price_cents"],
                    "market_median_cents": historical["median_price_cents"],
                    "median_price_cents": historical["median_price_cents"],
                    "p15_price_cents": historical["p15_price_cents"],
                    "p85_price_cents": historical["p85_price_cents"],
                    "sales_30d": historical["sales_30d"],
                    "historical": historical,
                }
            )

        catalog = catalog_rows.get(name)
        if catalog is not None:
            reference_quantity = _nonnegative_int(
                catalog["latest_reference_quantity"]
            )
            observed_sales = _nonnegative_int(catalog["observed_sales_count"])
            unique_assets = _nonnegative_int(catalog["unique_asset_count"])
            supply = {
                "latest_reference_quantity": reference_quantity,
                "latest_reference_at": _optional_text(
                    catalog["latest_reference_at"]
                ),
                "observed_sales_count": observed_sales,
                "unique_asset_count": unique_assets,
                "is_world_inventory": False,
                "label": "Справочные наблюдения, не мировой supply",
                "note": SUPPLY_NOTE,
            }
            snapshot.update(
                {
                    "collection_name": _optional_text(catalog["collection_name"]),
                    "icon_url": _safe_icon_url(catalog["icon_url"]),
                    "latest_reference_quantity": reference_quantity,
                    "latest_reference_at": supply["latest_reference_at"],
                    "observed_sales_count": observed_sales,
                    "unique_asset_count": unique_assets,
                    "supply": supply,
                }
            )

        forecast = _valid_forecast(forecast_rows.get(name))
        snapshot["forecast"] = forecast
        snapshot["forecast_30d"] = forecast

    return snapshots


# Clear aliases for callers that prefer the operation rather than getter name.
batch_market_snapshots = get_market_snapshots


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _required_cents(value: Any, *, name: str, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    return _bounded_int(value, minimum=minimum, maximum=1_000_000_000, name=name)


def _snapshot_price(snapshot: Mapping[str, Any], key: str) -> int | None:
    direct = _positive_price(snapshot.get(key))
    if direct is not None:
        return direct
    historical = snapshot.get("historical")
    if isinstance(historical, Mapping):
        return _positive_price(historical.get(key))
    return None


def _snapshot_forecast(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = snapshot.get("forecast_30d", snapshot.get("forecast"))
    return value if isinstance(value, Mapping) else None


def recommend_listing_price(
    snapshot: Mapping[str, Any] | None,
    *,
    buy_price_cents: int,
    fee_percent: float,
    listed_price_cents: int | None = None,
    listed_at: str | datetime | None = None,
    strategy: str = "balanced",
    stale_days: int = 7,
    special_float: bool = False,
    has_special_stickers: bool = False,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    """Recommend a fee-aware listing price from a market snapshot.

    ``fee_percent`` is expressed in percentage points (2 means 2%, not 0.02).
    Balanced/patient/investment strategies protect the per-unit break-even
    floor.  ``quick_sale`` may recommend the historical p15 even when realizing
    a loss; the result marks that trade-off explicitly.

    A recommendation is a benchmark only.  It never includes an automatic
    float/sticker premium and never claims to be a live executable ask.
    """

    if snapshot is None:
        snapshot = {}
    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot must be a mapping")
    buy_price = _required_cents(
        buy_price_cents,
        name="buy_price_cents",
        allow_zero=True,
    )
    listed_price = (
        None
        if listed_price_cents is None
        else _required_cents(listed_price_cents, name="listed_price_cents")
    )
    fee = _finite_float(fee_percent)
    if fee is None or not 0 <= fee <= 50:
        raise ValueError("fee_percent must be between 0 and 50")
    stale_after = _bounded_int(
        stale_days,
        minimum=1,
        maximum=3650,
        name="stale_days",
    )
    if not isinstance(strategy, str):
        raise TypeError("strategy must be a string")
    requested_strategy = strategy.strip().lower()
    canonical_strategy = _STRATEGY_ALIASES.get(requested_strategy)
    if canonical_strategy is None:
        allowed = ", ".join(sorted(set(_STRATEGY_ALIASES.values())))
        raise ValueError(f"strategy must be one of: {allowed}")

    now_value = _parse_datetime(now) if now is not None else datetime.now(timezone.utc)
    if now_value is None:
        raise ValueError("now must be an ISO datetime")
    listed_datetime = _parse_datetime(listed_at)
    if listed_at not in (None, "") and listed_datetime is None:
        raise ValueError("listed_at must be an ISO datetime")
    listing_age_days = (
        max(0, (now_value - listed_datetime).days)
        if listed_datetime is not None
        else None
    )
    is_stale = listing_age_days is not None and listing_age_days >= stale_after

    net_fraction = 1 - fee / 100
    break_even = math.ceil(buy_price / net_fraction) if buy_price else 0
    median = _snapshot_price(snapshot, "median_price_cents")
    if median is None:
        median = _snapshot_price(snapshot, "market_median_cents")
    p15 = _snapshot_price(snapshot, "p15_price_cents")
    p85 = _snapshot_price(snapshot, "p85_price_cents")
    if snapshot.get("market_data_available", True) is False:
        median = p15 = p85 = None
    market_ready = (
        median is not None
        and p15 is not None
        and p85 is not None
        and p15 <= median <= p85
        and snapshot.get("market_data_available", True) is not False
    )

    base_target: int | None = None
    benchmark_source: str | None = None
    model_was_capped = False
    if market_ready:
        if canonical_strategy == "quick_sale":
            base_target = p15
            benchmark_source = "historical_p15"
        elif canonical_strategy == "balanced":
            base_target = median
            benchmark_source = "historical_median"
        elif canonical_strategy == "patient":
            base_target = p85
            benchmark_source = "historical_p85"
        else:
            base_target = p85
            benchmark_source = "historical_p85"
            forecast = _snapshot_forecast(snapshot)
            predicted = (
                _positive_price(forecast.get("predicted_price_cents"))
                if forecast is not None
                else None
            )
            confidence = (
                _finite_float(forecast.get("confidence"))
                if forecast is not None
                else None
            )
            expected_return = (
                _finite_float(forecast.get("expected_return_percent"))
                if forecast is not None
                else None
            )
            # Model output is only allowed to lift a patient benchmark when it
            # has meaningful confidence.  Cap its effect to avoid turning an
            # unstable forecast into an extreme listing price.
            if (
                predicted is not None
                and confidence is not None
                and confidence >= 0.55
                and expected_return is not None
                and expected_return > 0
            ):
                model_cap = max(p85, round(median * 1.5))
                capped_prediction = min(predicted, model_cap)
                model_was_capped = predicted > capped_prediction
                base_target = max(p85, capped_prediction)
                benchmark_source = "historical_p85_plus_capped_30d_model"

        if is_stale:
            if canonical_strategy == "quick_sale":
                base_target = max(1, round(p15 * 0.98))
                benchmark_source = "stale_historical_p15"
            elif canonical_strategy == "balanced":
                base_target = max(p15, round(median * 0.97))
                benchmark_source = "stale_between_p15_and_median"
            elif canonical_strategy == "patient":
                base_target = median
                benchmark_source = "stale_historical_median"
            else:
                base_target = p85
                benchmark_source = "stale_historical_p85"

    market_target = base_target
    if base_target is None:
        suggested = None
    elif canonical_strategy == "quick_sale":
        suggested = base_target
    else:
        suggested = max(base_target, break_even)

    seller_proceeds = (
        math.floor(suggested * net_fraction) if suggested is not None else None
    )
    projected_profit = (
        seller_proceeds - buy_price if seller_proceeds is not None else None
    )
    roi = (
        round(projected_profit / buy_price * 100, 2)
        if projected_profit is not None and buy_price > 0
        else None
    )
    unrealized_profit = (
        math.floor(median * net_fraction) - buy_price
        if median is not None
        else None
    )
    unrealized_roi = (
        round(unrealized_profit / buy_price * 100, 2)
        if unrealized_profit is not None and buy_price > 0
        else None
    )
    listed_seller_proceeds = (
        math.floor(listed_price * net_fraction) if listed_price is not None else None
    )
    listed_profit = (
        listed_seller_proceeds - buy_price
        if listed_seller_proceeds is not None
        else None
    )
    listed_roi = (
        round(listed_profit / buy_price * 100, 2)
        if listed_profit is not None and buy_price > 0
        else None
    )
    market_below_break_even = (
        market_target is not None and market_target < break_even
    )

    manual_premium = bool(
        special_float
        or has_special_stickers
        or requested_strategy in _MANUAL_PREMIUM_STRATEGIES
    )
    if suggested is None:
        action = "unavailable"
        short = "Недостаточно рыночных данных"
        reason = (
            "Исторический median/p15/p85 недоступен или схема trend store "
            "неполна; цена намеренно не подставлена как $0."
        )
    elif manual_premium:
        action = "manual_review"
        short = "Проверь премию вручную"
        reason = (
            "Базовый ориентир рассчитан, но редкий float или стикеры могут "
            "существенно изменить цену."
        )
    elif market_below_break_even and canonical_strategy != "quick_sale":
        action = "review_loss_or_wait"
        short = "Безубыток выше рынка"
        reason = (
            "Безубыточная цена после комиссии выше исторического ориентира. "
            "Ожидание сохраняет цену покупки, а ускорение продажи может "
            "потребовать осознанного убытка."
        )
    elif listed_price is None:
        action = "list"
        short = "Можно выставлять"
        reason = "Использован выбранный исторический ценовой ориентир."
    elif is_stale and listed_price > suggested:
        action = "lower"
        short = "Снизить цену"
        reason = (
            f"Лот стоит {listing_age_days} дн., дольше порога {stale_after} дн., "
            "и его цена выше обновлённого ориентира."
        )
    elif listed_price > round(suggested * 1.05):
        action = "wait_or_lower"
        short = "Цена выше ориентира"
        reason = "Текущая цена более чем на 5% выше расчётного benchmark."
    elif listed_price < round(suggested * 0.95):
        action = "review_low_price"
        short = "Цена ниже ориентира"
        reason = "Текущая цена более чем на 5% ниже расчётного benchmark."
    else:
        action = "keep"
        short = "Оставить цену"
        reason = "Текущая цена находится в пределах 5% от ориентира."

    warnings = [HISTORICAL_BENCHMARK_NOTE, SPECIAL_PREMIUM_NOTE]
    if model_was_capped:
        warnings.append(
            "30-дневный прогноз был ограничен 150% median из-за риска "
            "нестабильного модельного значения."
        )
    if market_below_break_even:
        warnings.append(
            "Исторический рыночный ориентир ниже безубыточной цены после комиссии."
        )

    return {
        "requested_strategy": requested_strategy,
        "strategy": canonical_strategy,
        "action": action,
        "recommendation_short": short,
        "reason": reason,
        "suggested_price_cents": suggested,
        "market_target_price_cents": market_target,
        "market_median_cents": median,
        "break_even_price_cents": break_even,
        "listed_price_cents": listed_price,
        "listed_seller_proceeds_cents": listed_seller_proceeds,
        "listed_profit_cents": listed_profit,
        "listed_roi_percent": listed_roi,
        "listed_at": listed_datetime.isoformat() if listed_datetime else None,
        "listing_age_days": listing_age_days,
        "stale_after_days": stale_after,
        "is_stale": is_stale,
        "fee_percent": fee,
        "seller_proceeds_cents": seller_proceeds,
        "projected_profit_cents": projected_profit,
        "unrealized_profit_cents": unrealized_profit,
        "unrealized_roi_percent": unrealized_roi,
        "unrealized_profit_basis": "historical_median_after_fee",
        "roi_percent": roi,
        "market_below_break_even": market_below_break_even,
        "benchmark_source": benchmark_source,
        "benchmark_is_live_ask": False,
        "manual_premium_required": manual_premium,
        "special_premium_note": SPECIAL_PREMIUM_NOTE,
        "warnings": warnings,
    }


__all__ = [
    "HISTORICAL_BENCHMARK_NOTE",
    "MAX_BATCH_NAMES",
    "MAX_SEARCH_LIMIT",
    "SPECIAL_PREMIUM_NOTE",
    "SUPPLY_NOTE",
    "batch_market_snapshots",
    "get_market_snapshots",
    "recommend_listing_price",
    "search_skin_names",
]
