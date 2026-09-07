"""Read-only market discovery and budget recommendation helpers.

The dashboard calls these helpers with bounded inputs.  Heavy historical work
is read from the derived Trend store; the 5+ GB source database is only touched
for the small set of already-scored, current scanner listings.

Supply fields are deliberately explicit.  ``quantity_on_market_estimate`` is
the snapshot bundled in ``ALL_SKINS.json`` and ``observed_sales_count`` is the
number of sale rows observed by this project.  Neither field is a claim about
the number of items that exist worldwide.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

LIQUID_MAX_LIMIT = 100
LIQUID_CANDIDATE_SCAN_LIMIT = 25_000
ADVISOR_MAX_ITEMS = 30
ADVISOR_MODES = frozenset({"investment", "flip_float", "flip_stickers", "balanced"})
RISK_NAMES = frozenset({"conservative", "balanced", "aggressive"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _integer(value: Any, default: int = 0) -> int:
    number = _finite(value)
    return int(number) if number is not None else default


def _bounded_int(value: Any, *, minimum: int, maximum: int, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _bounded_float(
    value: Any,
    *,
    minimum: float,
    maximum: float,
    name: str,
) -> float:
    result = _finite(value)
    if result is None or not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


@contextmanager
def _read_connection(path: str | Path):
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        yield None
        return
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(conn, name):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})")}


def _parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value in (None, ""):
        return None
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


def _safe_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_skin_snapshot(path: str | Path) -> dict[str, Any]:
    """Load and deduplicate the bounded static CSFloat/market skin snapshot."""

    snapshot_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        raw = []
    if not isinstance(raw, list):
        raw = []

    grouped: dict[str, dict[str, Any]] = {}
    duplicate_names = Counter()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("market_hash_name") or "").strip()
        if not name:
            continue
        quantity = max(0, _integer(item.get("quantity_on_market")))
        price_usd = _finite(item.get("base_price_usd"))
        existing = grouped.get(name)
        if existing is None:
            grouped[name] = {
                "market_hash_name": name,
                "weapon_name": str(item.get("weapon_name") or "").strip() or None,
                "min_float": _finite(item.get("min_float")),
                "max_float": _finite(item.get("max_float")),
                "base_price_cents": (
                    max(0, round(price_usd * 100)) if price_usd is not None else None
                ),
                "quantity_on_market_estimate": quantity,
                "csfloat_url": str(item.get("csfloat_url") or "").strip() or None,
                "paint_indexes": [
                    _integer(item.get("paint_index"), -1)
                ] if item.get("paint_index") is not None else [],
            }
            continue

        duplicate_names[name] += 1
        existing["quantity_on_market_estimate"] += quantity
        candidate_cents = max(0, round(price_usd * 100)) if price_usd is not None else None
        if candidate_cents is not None and (
            existing["base_price_cents"] is None
            or candidate_cents < existing["base_price_cents"]
        ):
            existing["base_price_cents"] = candidate_cents
        paint_index = item.get("paint_index")
        if paint_index is not None:
            existing["paint_indexes"].append(_integer(paint_index, -1))
        minimum = _finite(item.get("min_float"))
        maximum = _finite(item.get("max_float"))
        if minimum is not None:
            existing["min_float"] = (
                minimum
                if existing["min_float"] is None
                else min(existing["min_float"], minimum)
            )
        if maximum is not None:
            existing["max_float"] = (
                maximum
                if existing["max_float"] is None
                else max(existing["max_float"], maximum)
            )

    try:
        snapshot_at = datetime.fromtimestamp(
            snapshot_path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    except OSError:
        snapshot_at = None
    return {
        "items": grouped,
        "source": snapshot_path.name,
        "snapshot_at": snapshot_at,
        "raw_rows": len(raw),
        "unique_names": len(grouped),
        "duplicate_names": len(duplicate_names),
    }


def _trend_market_rows(
    conn: sqlite3.Connection,
    *,
    max_price_cents: int,
) -> tuple[str | None, list[dict[str, Any]]]:
    daily_columns = _table_columns(conn, "trend_daily_market")
    required_daily = {"market_hash_name", "day", "sales_count", "median_price_cents"}
    if not required_daily.issubset(daily_columns):
        return None, []
    latest = conn.execute("SELECT MAX(day) FROM trend_daily_market").fetchone()
    as_of_day = latest[0] if latest else None
    if not as_of_day:
        return None, []

    notional_expression = (
        "SUM(notional_cents)" if "notional_cents" in daily_columns else "NULL"
    )
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            WITH recent AS (
                SELECT market_hash_name,
                       SUM(sales_count) AS sales_30d,
                       SUM(CASE WHEN day >= DATE(?, '-6 day')
                                THEN sales_count ELSE 0 END) AS sales_7d,
                       COUNT(*) AS active_days_30d,
                       {notional_expression} AS notional_30d,
                       MAX(day) AS latest_day
                FROM trend_daily_market
                WHERE day >= DATE(?, '-29 day')
                GROUP BY market_hash_name
            )
            SELECT r.market_hash_name,
                   r.sales_7d,
                   r.sales_30d,
                   r.active_days_30d,
                   r.notional_30d,
                   r.latest_day,
                   (SELECT d.median_price_cents
                      FROM trend_daily_market d
                     WHERE d.market_hash_name = r.market_hash_name
                     ORDER BY d.day DESC LIMIT 1) AS current_price_cents,
                   (SELECT d.median_price_cents
                      FROM trend_daily_market d
                     WHERE d.market_hash_name = r.market_hash_name
                       AND d.day <= DATE(?, '-7 day')
                     ORDER BY d.day DESC LIMIT 1) AS price_7d_cents,
                   (SELECT d.median_price_cents
                      FROM trend_daily_market d
                     WHERE d.market_hash_name = r.market_hash_name
                       AND d.day <= DATE(?, '-30 day')
                     ORDER BY d.day DESC LIMIT 1) AS price_30d_cents
            FROM recent r
            WHERE (SELECT d.median_price_cents
                     FROM trend_daily_market d
                    WHERE d.market_hash_name = r.market_hash_name
                    ORDER BY d.day DESC LIMIT 1) <= ?
            ORDER BY r.sales_30d DESC, r.active_days_30d DESC,
                     r.market_hash_name
            LIMIT ?
            """,
            (
                as_of_day,
                as_of_day,
                as_of_day,
                as_of_day,
                max_price_cents,
                LIQUID_CANDIDATE_SCAN_LIMIT,
            ),
        )
    ]

    forecasts: dict[tuple[str, int], dict[str, Any]] = {}
    forecast_columns = _table_columns(conn, "trend_forecasts")
    required_forecast = {
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
    }
    if required_forecast.issubset(forecast_columns):
        forecast_rows = conn.execute(
            """
            SELECT market_hash_name, horizon_days, predicted_price_cents,
                   low_price_cents, high_price_cents, expected_return_percent,
                   downside_return_percent, probability_up, confidence,
                   history_rows
            FROM trend_forecasts
            WHERE as_of_day = (SELECT MAX(as_of_day) FROM trend_forecasts)
              AND horizon_days IN (7, 30)
              AND current_price_cents <= ?
            """,
            (max_price_cents,),
        )
        for row in forecast_rows:
            value = dict(row)
            forecasts[(value["market_hash_name"], int(value["horizon_days"]))] = value

    catalog: dict[str, dict[str, Any]] = {}
    catalog_table = (
        "trend_skin_catalog"
        if _table_exists(conn, "trend_skin_catalog")
        else "trend_item_catalog"
        if _table_exists(conn, "trend_item_catalog")
        else None
    )
    if catalog_table:
        columns = _table_columns(conn, catalog_table)
        requested = [
            name
            for name in (
                "market_hash_name",
                "collection_name",
                "observed_sales_count",
                "db_sale_rows",
                "unique_asset_count",
                "min_float",
                "avg_float",
                "max_float",
                "latest_reference_quantity",
                "latest_reference_at",
                "icon_url",
            )
            if name in columns
        ]
        if "market_hash_name" in requested:
            for row in conn.execute(f"SELECT {', '.join(requested)} FROM {catalog_table}"):
                value = dict(row)
                if value.get("observed_sales_count") is None:
                    value["observed_sales_count"] = value.get("db_sale_rows")
                catalog[value["market_hash_name"]] = value

    for row in rows:
        current = _finite(row.get("current_price_cents"), 0) or 0
        price_7d = _finite(row.get("price_7d_cents"))
        price_30d = _finite(row.get("price_30d_cents"))
        row["return_7d_percent"] = (
            (current / price_7d - 1) * 100 if price_7d and price_7d > 0 else None
        )
        row["return_30d_percent"] = (
            (current / price_30d - 1) * 100 if price_30d and price_30d > 0 else None
        )
        row["sales_per_day"] = (_finite(row.get("sales_30d"), 0) or 0) / 30
        row["liquidity_score"] = min(
            100.0,
            100.0 * math.log1p(row["sales_per_day"]) / math.log1p(30.0),
        )
        row["forecast_7d"] = forecasts.get((row["market_hash_name"], 7))
        row["forecast_30d"] = forecasts.get((row["market_hash_name"], 30))
        row["current_price_observed_day"] = row.get("latest_day")
        row["current_price_source"] = "latest_observed_daily_sale_median"
        row["current_price_is_live_ask"] = False
        row.update(catalog.get(row["market_hash_name"], {}))
    return as_of_day, rows


def _live_deals(
    history_db_path: str | Path,
    *,
    max_price_cents: int,
    max_age_hours: int,
    snapshot_items: dict[str, dict[str, Any]],
    limit: int = 100,
) -> list[dict[str, Any]]:
    now = _utc_now()
    cutoff_at = now - timedelta(hours=max_age_hours)
    cutoff = cutoff_at.isoformat()
    with _read_connection(history_db_path) as conn:
        if conn is None:
            return []
        intelligence_columns = _table_columns(conn, "model_deal_intelligence")
        prediction_columns = _table_columns(conn, "model_deal_predictions")
        required_intelligence = {
            "listing_id",
            "computed_at",
            "buy_price_cents",
            "conservative_profit_cents",
            "ranking_score",
            "details_json",
        }
        required_prediction = {"listing_id", "model_name", "last_seen_at", "verdict"}
        if not required_intelligence.issubset(
            intelligence_columns
        ) or not required_prediction.issubset(prediction_columns):
            return []

        prediction_selects = []
        for column in (
            "float_value",
            "sticker_value_cents",
            "sticker_count",
            "liquidity_score",
            "historical_sales_count",
            "sales_per_day",
            "deal_payload_json",
            "prediction_json",
            "item_icon_url",
            "screenshot_url",
        ):
            expression = f"p.{column}" if column in prediction_columns else "NULL"
            prediction_selects.append(f"{expression} AS {column}")

        intelligence_selects = []
        for column in (
            "skin_name",
            "collection_name",
            "csfloat_url",
            "risk_score",
            "sell_probability_30d",
        ):
            expression = f"i.{column}" if column in intelligence_columns else "NULL"
            intelligence_selects.append(f"{expression} AS {column}")

        rows = list(
            conn.execute(
                f"""
                SELECT i.listing_id,
                       i.computed_at AS intelligence_computed_at,
                       i.buy_price_cents,
                       i.conservative_profit_cents,
                       i.ranking_score,
                       i.details_json,
                       p.last_seen_at AS listing_observed_at,
                       {", ".join(intelligence_selects)},
                       {", ".join(prediction_selects)}
                FROM model_deal_intelligence i
                JOIN model_deal_predictions p
                  ON p.rowid = (
                    SELECT p2.rowid FROM model_deal_predictions p2
                    WHERE p2.listing_id = i.listing_id
                      AND p2.last_seen_at >= ?
                      AND p2.verdict IN ('good', 'watch')
                    ORDER BY CASE p2.model_name
                               WHEN 'igor' THEN 1 WHEN 'almaz' THEN 2 ELSE 3 END,
                             p2.last_seen_at DESC
                    LIMIT 1
                  )
                WHERE i.computed_at >= ?
                  AND i.buy_price_cents BETWEEN 1 AND ?
                  AND i.conservative_profit_cents > 0
                ORDER BY i.ranking_score DESC, i.listing_id
                LIMIT ?
                """,
                (cutoff, cutoff, max_price_cents, min(max(limit, 1), 200)),
            )
        )

    result = []
    for raw in rows:
        details = _safe_json(raw["details_json"])
        if not details:
            continue
        observed_at = _parse_utc(raw["listing_observed_at"])
        computed_at = _parse_utc(raw["intelligence_computed_at"])
        if (
            observed_at is None
            or observed_at < cutoff_at
            or observed_at > now + timedelta(minutes=5)
            or computed_at is None
            or computed_at < cutoff_at
            or computed_at > now + timedelta(minutes=5)
            or computed_at < observed_at - timedelta(minutes=1)
        ):
            continue
        payload = _safe_json(raw["deal_payload_json"])
        prediction = _safe_json(raw["prediction_json"])
        stickers = prediction.get("stickers") or payload.get("stickers") or []
        if not isinstance(stickers, list):
            stickers = []
        name = str(
            raw["skin_name"]
            or details.get("skin_name")
            or payload.get("name")
            or ""
        ).strip()
        snapshot = snapshot_items.get(name, {})
        item_min = _finite(snapshot.get("min_float"))
        item_max = _finite(snapshot.get("max_float"))
        float_value = _finite(raw["float_value"])
        float_position = None
        if (
            float_value is not None
            and item_min is not None
            and item_max is not None
            and item_max > item_min
            and item_min <= float_value <= item_max
        ):
            float_position = (float_value - item_min) / (item_max - item_min) * 100
        sticker_value = max(0, _integer(raw["sticker_value_cents"]))
        buy_price = max(1, _integer(raw["buy_price_cents"], 1))
        sticker_ratio = sticker_value / buy_price * 100
        if stickers and sticker_ratio >= 20:
            opportunity_type = "stickers"
            reason = f"stickers are {sticker_ratio:.0f}% of the asking price before applied-craft discount"
        elif float_position is not None and float_position <= 10:
            opportunity_type = "float"
            reason = f"float is in the lowest {float_position:.1f}% of this skin's allowed wear range"
        else:
            opportunity_type = "misprice"
            reason = "the ensemble sees a positive conservative fee-adjusted edge"
        ensemble = (
            dict(details.get("ensemble_prediction"))
            if isinstance(details.get("ensemble_prediction"), dict)
            else {}
        )
        ensemble["conservative_profit_cents"] = _integer(
            raw["conservative_profit_cents"]
        )
        risk = dict(details.get("risk")) if isinstance(details.get("risk"), dict) else {}
        if raw["risk_score"] is not None:
            risk["score"] = _finite(raw["risk_score"])
        sale_estimate = (
            dict(details.get("sale_estimate"))
            if isinstance(details.get("sale_estimate"), dict)
            else {}
        )
        if raw["sell_probability_30d"] is not None:
            sale_estimate["within_30d"] = _finite(raw["sell_probability_30d"])
        details.update(
            {
                "listing_id": str(raw["listing_id"]),
                "skin_name": name or None,
                "collection_name": raw["collection_name"] or details.get("collection_name"),
                "csfloat_url": (
                    raw["csfloat_url"]
                    or details.get("csfloat_url")
                    or f"https://csfloat.com/item/{quote(str(raw['listing_id']), safe='')}"
                ),
                "buy_price_cents": buy_price,
                "ensemble_prediction": ensemble,
                "risk": risk,
                "sale_estimate": sale_estimate,
                "ranking_score": _finite(raw["ranking_score"], 0) or 0,
                "float_value": float_value,
                "float_range_percentile": float_position,
                "sticker_value_cents": sticker_value,
                "sticker_value_to_price_percent": sticker_ratio,
                "sticker_count": _integer(raw["sticker_count"], len(stickers)),
                "stickers": stickers[:5],
                "liquidity_score": _finite(raw["liquidity_score"]),
                "historical_sales_count": _integer(raw["historical_sales_count"]),
                "sales_per_day": _finite(raw["sales_per_day"]),
                "item_icon_url": raw["item_icon_url"],
                "screenshot_url": raw["screenshot_url"],
                "opportunity_type": opportunity_type,
                "reason": reason,
                "fresh_listing": True,
                "listing_observed_at": observed_at.isoformat(),
                "intelligence_computed_at": computed_at.isoformat(),
                "availability": {
                    "status": "recently_observed_active",
                    "observed_at": observed_at.isoformat(),
                    "max_age_hours": max_age_hours,
                    "currently_active_guaranteed": False,
                },
            }
        )
        result.append(details)
    return result


def build_liquid_market_payload(
    history_db_path: str | Path,
    trend_db_path: str | Path,
    skins_path: str | Path,
    *,
    max_price_cents: int = 1_000,
    min_sales_per_day: float = 1.0,
    min_liquidity_score: float = 0.0,
    search: str = "",
    sort: str = "liquidity",
    limit: int = 50,
    live_age_hours: int = 24,
) -> dict[str, Any]:
    """Return liquid market targets plus separately labelled fresh listings."""

    max_price = _bounded_int(
        max_price_cents, minimum=1, maximum=1_000, name="max_price_cents"
    )
    minimum_sales = _bounded_float(
        min_sales_per_day, minimum=0, maximum=100_000, name="min_sales_per_day"
    )
    minimum_liquidity = _bounded_float(
        min_liquidity_score,
        minimum=0,
        maximum=100,
        name="min_liquidity_score",
    )
    row_limit = _bounded_int(limit, minimum=1, maximum=LIQUID_MAX_LIMIT, name="limit")
    age = _bounded_int(live_age_hours, minimum=1, maximum=720, name="live_age_hours")
    query = str(search or "").strip().casefold()
    if len(query) > 240:
        raise ValueError("search must not exceed 240 characters")
    sort_name = str(sort or "liquidity").strip().lower()
    if sort_name not in {"liquidity", "return_7d", "return_30d", "price", "supply"}:
        raise ValueError("sort must be liquidity, return_7d, return_30d, price, or supply")

    snapshot = load_skin_snapshot(skins_path)
    with _read_connection(trend_db_path) as conn:
        if conn is None:
            as_of_day, rows = None, []
        else:
            as_of_day, rows = _trend_market_rows(conn, max_price_cents=max_price)

    filtered = []
    for row in rows:
        name = str(row.get("market_hash_name") or "")
        if query and query not in name.casefold():
            continue
        if (_finite(row.get("sales_per_day"), 0) or 0) < minimum_sales:
            continue
        if (_finite(row.get("liquidity_score"), 0) or 0) < minimum_liquidity:
            continue
        supply = snapshot["items"].get(name, {})
        for key, value in supply.items():
            if key not in row or row.get(key) in (None, "", []):
                row[key] = value
        row["quantity_snapshot_at"] = snapshot["snapshot_at"]
        row["quantity_source"] = snapshot["source"]
        filtered.append(row)

    def score(value: dict[str, Any], field: str, missing: float = -math.inf) -> float:
        return _finite(value.get(field), missing) or 0.0

    if sort_name == "price":
        filtered.sort(
            key=lambda row: (score(row, "current_price_cents", math.inf), -score(row, "sales_30d"), row["market_hash_name"])
        )
    elif sort_name == "supply":
        filtered.sort(
            key=lambda row: (score(row, "quantity_on_market_estimate"), score(row, "sales_30d"), row["market_hash_name"]),
            reverse=True,
        )
    else:
        field = {"liquidity": "liquidity_score", "return_7d": "return_7d_percent", "return_30d": "return_30d_percent"}[sort_name]
        filtered.sort(
            key=lambda row: (score(row, field), score(row, "sales_30d"), row["market_hash_name"]),
            reverse=True,
        )

    live = _live_deals(
        history_db_path,
        max_price_cents=max_price,
        max_age_hours=age,
        snapshot_items=snapshot["items"],
        limit=200,
    )
    live = [
        row
        for row in live
        if (_finite(row.get("sales_per_day"), 0) or 0) >= minimum_sales
        and (_finite(row.get("liquidity_score"), 0) or 0) >= minimum_liquidity
    ]
    if query:
        live = [row for row in live if query in str(row.get("skin_name") or "").casefold()]
    live = live[:100]
    return {
        "market_schema_version": 1,
        "status": "ready" if as_of_day is not None else "unavailable",
        "generated_at": _iso_now(),
        "as_of_day": as_of_day,
        "filters": {
            "max_price_cents": max_price,
            "min_sales_per_day": minimum_sales,
            "min_liquidity_score": minimum_liquidity,
            "search": str(search or "").strip(),
            "sort": sort_name,
            "limit": row_limit,
            "live_age_hours": age,
        },
        "summary": {
            "matching_skins": len(filtered),
            "shown_skins": min(len(filtered), row_limit),
            "fresh_live_deals": len(live),
            "snapshot_names": snapshot["unique_names"],
            "candidate_scan_limit": LIQUID_CANDIDATE_SCAN_LIMIT,
            "candidate_scan_truncated": len(rows) >= LIQUID_CANDIDATE_SCAN_LIMIT,
        },
        "items": filtered[:row_limit],
        "live_deals": live,
        "pricing": {
            "field": "current_price_cents",
            "source": "latest observed daily sale median",
            "is_live_ask": False,
            "note": "Only rows in live_deals represent recently observed concrete listings.",
        },
        "supply": {
            "field": "quantity_on_market_estimate",
            "source": snapshot["source"],
            "snapshot_at": snapshot["snapshot_at"],
            "is_world_inventory": False,
            "note": "Approximate market-listing snapshot; not the number of items that exist worldwide.",
            "observed_sales_count_note": "Sale rows observed by this project's history database.",
            "unique_asset_count_note": "Distinct asset IDs observed in this project's history database.",
        },
    }


def _adaptive_item_cap(budget_cents: int, risk: str) -> int:
    if budget_cents <= 10_000:
        return min(budget_cents, 1_000)
    share = {"conservative": 0.20, "balanced": 0.30, "aggressive": 0.45}[risk]
    return max(1_000, min(budget_cents, round(budget_cents * share)))


def _investment_candidates(
    trend_db_path: str | Path,
    skins_path: str | Path,
    *,
    item_cap_cents: int,
    risk: str,
    skin_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    snapshot = skin_snapshot or load_skin_snapshot(skins_path)
    minimum_probability = {
        "conservative": 0.62,
        "balanced": 0.55,
        "aggressive": 0.50,
    }[risk]
    maximum_downside = {
        "conservative": -12.0,
        "balanced": -22.0,
        "aggressive": -40.0,
    }[risk]
    minimum_confidence = {
        "conservative": 0.50,
        "balanced": 0.35,
        "aggressive": 0.20,
    }[risk]
    maximum_expected_return = {
        "conservative": 30.0,
        "balanced": 60.0,
        "aggressive": 100.0,
    }[risk]
    with _read_connection(trend_db_path) as conn:
        if conn is None:
            return []
        forecast_columns = _table_columns(conn, "trend_forecasts")
        daily_columns = _table_columns(conn, "trend_daily_market")
        required_forecast = {
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
        }
        required_daily = {"market_hash_name", "day", "sales_count"}
        if not required_forecast.issubset(
            forecast_columns
        ) or not required_daily.issubset(daily_columns):
            return []
        catalog = {}
        catalog_table = (
            "trend_skin_catalog"
            if _table_exists(conn, "trend_skin_catalog")
            else "trend_item_catalog"
            if _table_exists(conn, "trend_item_catalog")
            else None
        )
        if catalog_table:
            columns = _table_columns(conn, catalog_table)
            if {"market_hash_name", "collection_name"}.issubset(columns):
                catalog = dict(
                    conn.execute(
                        f"SELECT market_hash_name, collection_name FROM {catalog_table}"
                    )
                )
        last_observation_expr = (
            "f.last_observation_day"
            if "last_observation_day" in forecast_columns
            else "f.as_of_day"
        )
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                WITH latest AS (SELECT MAX(as_of_day) AS day FROM trend_forecasts),
                volume AS (
                    SELECT d.market_hash_name, SUM(d.sales_count) AS sales_30d,
                           COUNT(*) AS active_days_30d
                    FROM trend_daily_market d
                    CROSS JOIN latest
                    WHERE d.day BETWEEN DATE(latest.day, '-29 day') AND latest.day
                    GROUP BY d.market_hash_name
                )
                SELECT f.as_of_day AS forecast_as_of_day,
                       {last_observation_expr} AS price_observed_day,
                       f.market_hash_name, f.current_price_cents,
                       f.predicted_price_cents, f.low_price_cents,
                       f.high_price_cents, f.expected_return_percent,
                       f.downside_return_percent, f.probability_up,
                       f.confidence, f.history_rows,
                       COALESCE(v.sales_30d, 0) AS sales_30d,
                       COALESCE(v.active_days_30d, 0) AS active_days_30d
                FROM trend_forecasts f
                JOIN latest ON f.as_of_day = latest.day
                LEFT JOIN volume v USING(market_hash_name)
                WHERE f.horizon_days = 30
                  AND f.current_price_cents BETWEEN 1 AND ?
                  AND f.expected_return_percent > 0
                  AND f.expected_return_percent <= ?
                  AND f.probability_up BETWEEN ? AND 1
                  AND f.confidence BETWEEN ? AND 1
                  AND f.downside_return_percent >= ?
                  AND f.history_rows >= 30
                  AND COALESCE(v.sales_30d, 0) >= 10
                  AND {last_observation_expr} >= DATE(latest.day, '-7 day')
                ORDER BY f.expected_return_percent DESC
                LIMIT 2000
                """,
                (
                    item_cap_cents,
                    maximum_expected_return,
                    minimum_probability,
                    minimum_confidence,
                    maximum_downside,
                ),
            )
        ]
    result = []
    for row in rows:
        probability = _finite(row.get("probability_up"))
        downside = _finite(row.get("downside_return_percent"))
        confidence = _finite(row.get("confidence"))
        expected = _finite(row.get("expected_return_percent"))
        history_rows = _integer(row.get("history_rows"))
        sales_30d = _integer(row.get("sales_30d"))
        if (
            probability is None
            or downside is None
            or confidence is None
            or expected is None
            or probability < minimum_probability
            or downside < maximum_downside
            or confidence < minimum_confidence
            or expected > maximum_expected_return
            or history_rows < 30
            or sales_30d < 10
        ):
            continue
        sales_per_day = (_finite(row.get("sales_30d"), 0) or 0) / 30
        liquidity = min(100.0, 100 * math.log1p(sales_per_day) / math.log1p(30))
        score = expected * 2.2 + probability * 45 + confidence * 20 + liquidity * 0.35 + max(downside, -50) * 0.7
        name = row["market_hash_name"]
        supply = snapshot["items"].get(name, {})
        result.append(
            {
                "candidate_type": "market_target",
                "mode": "investment",
                "market_hash_name": name,
                "skin_name": name,
                "collection_name": catalog.get(name),
                "price_cents": _integer(row.get("current_price_cents")),
                "current_price_cents": _integer(row.get("current_price_cents")),
                "predicted_price_cents": _integer(row.get("predicted_price_cents")),
                "expected_return_percent": expected,
                "downside_return_percent": downside,
                "probability_up": probability,
                "confidence": confidence,
                "sales_30d": _integer(row.get("sales_30d")),
                "sales_per_day": sales_per_day,
                "liquidity_score": liquidity,
                "score": score,
                "quantity_on_market_estimate": supply.get("quantity_on_market_estimate"),
                "quantity_snapshot_at": snapshot["snapshot_at"],
                "quantity_source": snapshot["source"],
                "quantity_is_world_inventory": False,
                "csfloat_url": supply.get("csfloat_url"),
                "steam_market_url": f"https://steamcommunity.com/market/listings/730/{quote(name, safe='')}",
                "reason": f"30d forecast {expected:+.1f}%, probability up {probability * 100:.0f}%, about {sales_per_day:.1f} observed sales/day",
                "fresh_listing": False,
                "forecast_as_of_day": row.get("forecast_as_of_day"),
                "price_observed_day": row.get("price_observed_day"),
            }
        )
    result.sort(key=lambda row: (row["score"], row["market_hash_name"]), reverse=True)
    return result


def _rank_live_candidate(
    item: dict[str, Any],
    mode: str,
    risk_profile: str | None = None,
) -> float | None:
    ensemble = (
        item.get("ensemble_prediction")
        if isinstance(item.get("ensemble_prediction"), dict)
        else {}
    )
    risk = item.get("risk") if isinstance(item.get("risk"), dict) else {}
    sale = (
        item.get("sale_estimate")
        if isinstance(item.get("sale_estimate"), dict)
        else {}
    )
    base = _finite(item.get("ranking_score"), 0) or 0
    conservative_profit = _finite(ensemble.get("conservative_profit_cents"), 0) or 0
    probability = max(0.0, min(1.0, _finite(sale.get("within_30d"), 0) or 0))
    parsed_risk = _finite(risk.get("score"))
    risk_score = parsed_risk if parsed_risk is not None else 100.0
    if conservative_profit <= 0:
        return None
    if risk_profile is not None:
        maximum_risk = {
            "conservative": 35.0,
            "balanced": 60.0,
            "aggressive": 85.0,
        }[risk_profile]
        if risk_score > maximum_risk:
            return None
    if mode == "flip_float":
        percentile = _finite(item.get("float_range_percentile"))
        if percentile is None or not 0 <= percentile <= 20:
            return None
        return base + (20 - percentile) * 35 + conservative_profit * probability - risk_score * 4
    if mode == "flip_stickers":
        ratio = _finite(item.get("sticker_value_to_price_percent"), 0) or 0
        if not item.get("sticker_count") or ratio < 10:
            return None
        return base + ratio * 18 + conservative_profit * probability - risk_score * 4
    return base + conservative_profit * probability - risk_score * 3


def _select_budget_items(
    candidates: list[dict[str, Any]],
    *,
    budget_cents: int,
    max_items: int,
    max_per_skin: int = 1,
    max_per_collection: int = 3,
) -> tuple[list[dict[str, Any]], int, Counter]:
    selected = []
    spent = 0
    skin_counts = Counter()
    collection_counts = Counter()
    rejected = Counter()
    for item in candidates:
        if len(selected) >= max_items:
            rejected["max_items"] += 1
            continue
        price = _integer(item.get("price_cents") or item.get("buy_price_cents"))
        if price <= 0:
            rejected["invalid_price"] += 1
            continue
        if spent + price > budget_cents:
            rejected["budget"] += 1
            continue
        skin = str(item.get("base_skin_name") or item.get("skin_name") or item.get("market_hash_name") or "")
        collection = str(item.get("collection_name") or skin)
        if skin_counts[skin] >= max_per_skin:
            rejected["skin_diversification"] += 1
            continue
        if collection_counts[collection] >= max_per_collection:
            rejected["collection_diversification"] += 1
            continue
        clean = dict(item)
        clean["price_cents"] = price
        selected.append(clean)
        spent += price
        skin_counts[skin] += 1
        collection_counts[collection] += 1
    return selected, spent, rejected


def build_budget_advice(
    history_db_path: str | Path,
    trend_db_path: str | Path,
    skins_path: str | Path,
    *,
    budget_cents: int,
    mode: str = "balanced",
    risk: str = "balanced",
    max_items: int = 10,
    live_age_hours: int = 24,
) -> dict[str, Any]:
    """Build deterministic, mode-specific suggestions without placing orders."""

    budget = _bounded_int(
        budget_cents, minimum=1, maximum=2_000_000_000, name="budget_cents"
    )
    mode_name = str(mode or "").strip().lower()
    if mode_name not in ADVISOR_MODES:
        raise ValueError("mode must be investment, flip_float, flip_stickers, or balanced")
    risk_name = str(risk or "").strip().lower()
    if risk_name not in RISK_NAMES:
        raise ValueError("risk must be conservative, balanced, or aggressive")
    item_limit = _bounded_int(
        max_items, minimum=1, maximum=ADVISOR_MAX_ITEMS, name="max_items"
    )
    age = _bounded_int(live_age_hours, minimum=1, maximum=720, name="live_age_hours")
    item_cap = _adaptive_item_cap(budget, risk_name)

    snapshot = load_skin_snapshot(skins_path)
    investments = _investment_candidates(
        trend_db_path,
        skins_path,
        item_cap_cents=item_cap,
        risk=risk_name,
        skin_snapshot=snapshot,
    )
    live = _live_deals(
        history_db_path,
        max_price_cents=item_cap,
        max_age_hours=age,
        snapshot_items=snapshot["items"],
        limit=200,
    )
    for item in live:
        item["candidate_type"] = "live_listing"
        item["price_cents"] = _integer(item.get("buy_price_cents"))

    if mode_name == "investment":
        candidates = investments
    elif mode_name in {"flip_float", "flip_stickers"}:
        candidates = []
        for item in live:
            score = _rank_live_candidate(item, mode_name, risk_name)
            if score is not None:
                item["mode"] = mode_name
                item["score"] = score
                candidates.append(item)
        candidates.sort(key=lambda item: (item["score"], str(item.get("listing_id") or "")), reverse=True)
    else:
        candidates = investments[: max(20, item_limit * 4)]
        for item in live:
            score = _rank_live_candidate(item, "balanced", risk_name)
            if score is not None:
                item["mode"] = "balanced"
                item["score"] = score
                candidates.append(item)
        candidates.sort(key=lambda item: (item.get("score", 0), str(item.get("skin_name") or "")), reverse=True)

    selected, spent, rejected = _select_budget_items(
        candidates,
        budget_cents=budget,
        max_items=item_limit,
    )
    candidate_types = Counter(item.get("candidate_type") or "unknown" for item in selected)
    return {
        "advisor_schema_version": 1,
        "generated_at": _iso_now(),
        "budget_cents": budget,
        "mode": mode_name,
        "risk": risk_name,
        "adaptive_item_cap_cents": item_cap,
        "spent_cents": spent,
        "cash_remaining_cents": budget - spent,
        "item_count": len(selected),
        "candidate_types": dict(candidate_types),
        "items": selected,
        "rejected_counts": dict(sorted(rejected.items())),
        "live_candidate_count": len(live),
        "investment_candidate_count": len(investments),
        "supply": {
            "field": "quantity_on_market_estimate",
            "source": snapshot["source"],
            "snapshot_at": snapshot["snapshot_at"],
            "is_world_inventory": False,
            "note": "Approximate market-listing snapshot; not worldwide item supply.",
        },
        "safety": {
            "trading_actions": False,
            "investment_targets_are_live_listings": False,
            "live_listing_max_age_hours": age,
            "live_listing_availability_guaranteed": False,
            "live_listing_meaning": "Observed active within the requested freshness window; availability is not rechecked at response time.",
            "profit_guaranteed": False,
        },
    }
