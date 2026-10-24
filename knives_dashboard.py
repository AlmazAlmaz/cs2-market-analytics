# ruff: noqa: E501
"""Read-only knife-market catalogue and dashboard page.

The dashboard deliberately does not pretend that a dedicated knife model
exists.  It combines the authoritative item catalogue (when published) with
already-materialized daily market observations.  Seven- and thirty-day values
are backward-looking price changes, never forecasts.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 2
MAX_LIMIT = 200
MAX_OFFSET = 100_000
MAX_TEXT_FILTER = 180
CURRENT_PRICE_MAX_AGE_DAYS = 7
COMPARISON_PRICE_MAX_LAG_DAYS = 7
LIVE_OFFER_MAX_AGE_HOURS = 24
MAX_LIVE_OPPORTUNITIES = 12
PUBLIC_CATALOG_META_KEYS = frozenset(
    {
        "active_build_id",
        "active_source_id",
        "kind_counts_json",
        "last_checked_at_epoch",
        "refreshed_at",
        "refreshed_at_epoch",
        "row_count",
        "schema_version",
        "variant_count",
    }
)

KNIFE_BASE_NAMES = (
    "Bayonet",
    "Bowie Knife",
    "Butterfly Knife",
    "Classic Knife",
    "Falchion Knife",
    "Flip Knife",
    "Gut Knife",
    "Huntsman Knife",
    "Karambit",
    "Kukri Knife",
    "M9 Bayonet",
    "Navaja Knife",
    "Nomad Knife",
    "Paracord Knife",
    "Shadow Daggers",
    "Skeleton Knife",
    "Stiletto Knife",
    "Survival Knife",
    "Talon Knife",
    "Ursus Knife",
)
KNOWN_WEAR_NAMES = (
    "Factory New",
    "Minimal Wear",
    "Field-Tested",
    "Well-Worn",
    "Battle-Scarred",
)
KNOWN_PHASE_NAMES = (
    "Phase 1",
    "Phase 2",
    "Phase 3",
    "Phase 4",
    "Ruby",
    "Sapphire",
    "Black Pearl",
    "Emerald",
)
SORT_VALUES = frozenset(
    {
        "liquidity",
        "sales_30d",
        "price_low",
        "price_high",
        "trend_7d",
        "trend_30d",
        "name",
    }
)
DIRECTION_VALUES = frozenset({"all", "up", "down", "flat", "no_data"})


def _bounded_int(
    value: Any, *, name: str, default: int, minimum: int, maximum: int
) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        # Public query validators intentionally expose one ValueError contract.
        raise ValueError(f"{name} must be an integer")  # noqa: TRY004
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _bounded_float(
    value: Any, *, name: str, default: float, minimum: float, maximum: float
) -> float:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        # Public query validators intentionally expose one ValueError contract.
        raise ValueError(f"{name} must be a number")  # noqa: TRY004
    try:
        result = float(str(value).replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _price_cents(value: Any, *, name: str) -> int | None:
    if value in (None, ""):
        return None
    dollars = _bounded_float(
        value, name=name, default=0, minimum=0, maximum=100_000_000
    )
    return round(dollars * 100)


def _text_filter(value: Any, *, name: str) -> str:
    result = str(value or "").strip()
    if len(result) > MAX_TEXT_FILTER:
        raise ValueError(f"{name} must contain at most {MAX_TEXT_FILTER} characters")
    return result


@contextmanager
def _read_connection(path: str | Path) -> Iterator[sqlite3.Connection | None]:
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        yield None
        return
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        yield connection
    finally:
        connection.close()


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return set()
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _optional_text(value: Any) -> str | None:
    result = str(value or "").strip()
    return result or None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _optional_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_text_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in parsed:
        text = _optional_text(raw)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result


def _json_int_list(value: Any) -> list[int]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    result: list[int] = []
    for raw in parsed:
        number = _optional_int(raw)
        if number is not None and number not in result:
            result.append(number)
    return result


def _safe_image_url(value: Any) -> str | None:
    text = _optional_text(value)
    if not text:
        return None
    parsed = urlsplit(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc and not (
        parsed.username or parsed.password
    ):
        return text
    return None


def _safe_csfloat_url(value: Any) -> str | None:
    text = _optional_text(value)
    if not text:
        return None
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme == "https"
        and host in {"csfloat.com", "www.csfloat.com"}
        and not (parsed.username or parsed.password)
    ):
        return text
    return None


def parse_knife_name(market_hash_name: Any) -> dict[str, Any]:
    """Parse only information that is explicit in one canonical market name."""

    original = str(market_hash_name or "").strip()
    value = original
    if value.startswith("★"):
        value = value[1:].strip()
    stattrak = False
    if value.startswith("StatTrak™"):
        stattrak = True
        value = value[len("StatTrak™") :].strip()

    wear = None
    wear_match = re.search(r"\(([^()]*)\)\s*$", value)
    if wear_match and wear_match.group(1) in KNOWN_WEAR_NAMES:
        wear = wear_match.group(1)
        value = value[: wear_match.start()].strip()

    left, separator, right = value.partition("|")
    base_name = left.strip() or None
    finish = right.strip() if separator and right.strip() else None
    phase = None
    if finish:
        for candidate in KNOWN_PHASE_NAMES:
            if re.search(
                rf"(?:^|\s){re.escape(candidate)}(?:$|\s)",
                finish,
                re.IGNORECASE,
            ):
                phase = candidate
                break
    return {
        "market_hash_name": original,
        "knife_type": base_name,
        "finish": finish,
        "phase": phase,
        "wear": wear,
        "stattrak": stattrak,
    }


def is_canonical_knife_name(value: Any) -> bool:
    name = str(value or "").strip()
    if not name.startswith("★"):
        return False
    parsed = parse_knife_name(name)
    base = str(parsed.get("knife_type") or "").casefold()
    return any(base == expected.casefold() for expected in KNIFE_BASE_NAMES)


def _load_catalog(path: str | Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    with _read_connection(path) as connection:
        if connection is None:
            return {}, {"state": "unavailable", "reason": "database_missing"}
        columns = _table_columns(connection, "cs2_item_catalog")
        required = {"market_hash_name", "item_kind"}
        if not required.issubset(columns):
            reason = "table_missing" if not columns else "contract_incomplete"
            return {}, {"state": "unavailable", "reason": reason}
        supported = (
            "market_hash_name",
            "item_kind",
            "base_name",
            "weapon_id",
            "weapon_name",
            "weapon_def_index",
            "paint_id",
            "paint_index",
            "paint_name",
            "phase_id",
            "phase_name",
            "wear_id",
            "wear_name",
            "min_float",
            "max_float",
            "stattrak",
            "souvenir",
            "collection_ids_json",
            "collection_names_json",
            "crate_ids_json",
            "crate_names_json",
            "image_url",
            "source_id",
            "source_updated_at",
            "catalog_build_id",
            "paint_indices_json",
            "phase_ids_json",
            "phase_names_json",
            "variant_count",
        )
        selected = [column for column in supported if column in columns]
        variants_by_name: dict[str, list[dict[str, Any]]] = {}
        variant_columns = _table_columns(connection, "cs2_item_catalog_variants")
        if {"market_hash_name", "source_variant_id"}.issubset(variant_columns):
            selected_variant_columns = [
                column
                for column in (
                    "market_hash_name",
                    "source_variant_id",
                    "paint_id",
                    "paint_index",
                    "paint_name",
                    "phase_id",
                    "phase_name",
                    "image_url",
                    "source_id",
                )
                if column in variant_columns
            ]
            for row in connection.execute(
                f"SELECT {', '.join(selected_variant_columns)} "
                "FROM cs2_item_catalog_variants "
                "WHERE market_hash_name IN ("
                "SELECT market_hash_name FROM cs2_item_catalog WHERE item_kind='knife'"
                ") ORDER BY market_hash_name, source_variant_id"
            ):
                value = dict(row)
                variant_name = _optional_text(value.get("market_hash_name"))
                if not variant_name:
                    continue
                variants_by_name.setdefault(variant_name, []).append(
                    {
                        "source_variant_id": _optional_text(
                            value.get("source_variant_id")
                        ),
                        "paint_id": _optional_text(value.get("paint_id")),
                        "paint_index": _optional_int(value.get("paint_index")),
                        "paint_name": _optional_text(value.get("paint_name")),
                        "phase_id": _optional_text(value.get("phase_id")),
                        "phase_name": _optional_text(value.get("phase_name")),
                        "image_url": _safe_image_url(value.get("image_url")),
                        "source_id": _optional_text(value.get("source_id")),
                    }
                )
        rows = connection.execute(
            f"SELECT {', '.join(selected)} FROM cs2_item_catalog "
            "WHERE item_kind=? ORDER BY market_hash_name",
            ("knife",),
        )
        items: dict[str, dict[str, Any]] = {}
        for raw_row in rows:
            raw = dict(raw_row)
            name = _optional_text(raw.get("market_hash_name"))
            if not name:
                continue
            parsed = parse_knife_name(name)
            collections = _json_text_list(raw.get("collection_names_json"))
            crates = _json_text_list(raw.get("crate_names_json"))
            variants = variants_by_name.get(name, [])
            phases = _json_text_list(raw.get("phase_names_json"))
            phase_ids = _json_text_list(raw.get("phase_ids_json"))
            paint_indices = _json_int_list(raw.get("paint_indices_json"))
            for variant in variants:
                variant_phase = _optional_text(variant.get("phase_name"))
                variant_phase_id = _optional_text(variant.get("phase_id"))
                variant_paint_index = _optional_int(variant.get("paint_index"))
                if variant_phase and variant_phase not in phases:
                    phases.append(variant_phase)
                if variant_phase_id and variant_phase_id not in phase_ids:
                    phase_ids.append(variant_phase_id)
                if (
                    variant_paint_index is not None
                    and variant_paint_index not in paint_indices
                ):
                    paint_indices.append(variant_paint_index)
            singular_phase = _optional_text(raw.get("phase_name")) or parsed["phase"]
            if singular_phase and singular_phase not in phases:
                phases.insert(0, singular_phase)
            singular_phase_id = _optional_text(raw.get("phase_id"))
            if singular_phase_id and singular_phase_id not in phase_ids:
                phase_ids.insert(0, singular_phase_id)
            singular_paint_index = _optional_int(raw.get("paint_index"))
            if singular_paint_index is not None and singular_paint_index not in paint_indices:
                paint_indices.insert(0, singular_paint_index)
            variant_count = max(
                _optional_int(raw.get("variant_count")) or 0,
                len(variants),
                len(phases),
                len(phase_ids),
                len(paint_indices),
                1,
            )
            items[name] = {
                "market_hash_name": name,
                "item_kind": "knife",
                # ``base_name`` in the synchronized catalogue is the market
                # name without the wear suffix (for example
                # ``Karambit | Doppler``), not the knife family.  Prefer the
                # structured weapon field and the canonical-name parser so
                # the facet remains ``Karambit`` instead of one entry per
                # finish.
                "knife_type": _optional_text(raw.get("weapon_name"))
                or parsed["knife_type"]
                or _optional_text(raw.get("base_name")),
                "weapon_id": _optional_text(raw.get("weapon_id")),
                "weapon_def_index": _optional_int(raw.get("weapon_def_index")),
                "paint_id": _optional_text(raw.get("paint_id")),
                "paint_index": singular_paint_index,
                "paint_indices": paint_indices,
                "finish": _optional_text(raw.get("paint_name")) or parsed["finish"],
                "phase_id": singular_phase_id,
                "phase": singular_phase,
                "phase_ids": phase_ids,
                "phases": phases,
                "variant_count": variant_count,
                "variants": variants,
                "wear_id": _optional_text(raw.get("wear_id")),
                "wear": _optional_text(raw.get("wear_name")) or parsed["wear"],
                "min_float": _optional_float(raw.get("min_float")),
                "max_float": _optional_float(raw.get("max_float")),
                "stattrak": bool(raw.get("stattrak")) or bool(parsed["stattrak"]),
                "souvenir": bool(raw.get("souvenir")),
                "collections": collections,
                "collection_name": collections[0] if collections else None,
                "crates": crates,
                "icon_url": _safe_image_url(raw.get("image_url")),
                "source_id": _optional_text(raw.get("source_id")),
                "source_updated_at": _optional_text(raw.get("source_updated_at")),
                "catalog_build_id": _optional_text(raw.get("catalog_build_id")),
            }

        metadata: dict[str, Any] = {}
        meta_columns = _table_columns(connection, "cs2_item_catalog_meta")
        if {"key", "value", "updated_at"}.issubset(meta_columns):
            for row in connection.execute(
                "SELECT key, value, updated_at FROM cs2_item_catalog_meta "
                f"WHERE key IN ({','.join('?' for _ in PUBLIC_CATALOG_META_KEYS)})",
                tuple(sorted(PUBLIC_CATALOG_META_KEYS)),
            ):
                key = str(row["key"])
                value: Any = row["value"]
                if key.endswith("_json"):
                    try:
                        value = json.loads(str(value))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                metadata[key] = value
            metadata["meta_updated_at"] = connection.execute(
                "SELECT MAX(updated_at) FROM cs2_item_catalog_meta"
            ).fetchone()[0]
        return items, {
            "state": "ready",
            "reason": None,
            "table": "cs2_item_catalog",
            "knife_count": len(items),
            "metadata": metadata,
        }


def _return_percent(current: Any, previous: Any) -> float | None:
    current_value = _optional_float(current)
    previous_value = _optional_float(previous)
    if current_value is None or previous_value is None or previous_value <= 0:
        return None
    return round((current_value / previous_value - 1.0) * 100.0, 4)


def _trend_direction(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "no_data"
    if number > 0.05:
        return "up"
    if number < -0.05:
        return "down"
    return "flat"


def _load_trend_rows(
    path: str | Path,
    *,
    authoritative_names: set[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    with _read_connection(path) as connection:
        if connection is None:
            return {}, {"state": "unavailable", "reason": "database_missing"}
        daily_columns = _table_columns(connection, "trend_daily_market")
        required = {"market_hash_name", "day", "sales_count", "median_price_cents"}
        if not required.issubset(daily_columns):
            reason = "table_missing" if not daily_columns else "contract_incomplete"
            return {}, {"state": "unavailable", "reason": reason}
        catalog_columns = _table_columns(connection, "trend_skin_catalog")
        trend_catalog: dict[str, dict[str, Any]] = {}
        optional_catalog_columns = [
            column
            for column in (
                "market_hash_name",
                "collection_name",
                "observed_sales_count",
                "unique_asset_count",
                "min_float",
                "max_float",
                "latest_reference_quantity",
                "latest_reference_at",
                "icon_url",
            )
            if column in catalog_columns
        ]
        if "market_hash_name" in optional_catalog_columns:
            for row in connection.execute(
                f"SELECT {', '.join(optional_catalog_columns)} FROM trend_skin_catalog"
            ):
                value = dict(row)
                catalog_name = str(value["market_hash_name"])
                if authoritative_names is not None:
                    if catalog_name not in authoritative_names:
                        continue
                elif not is_canonical_knife_name(catalog_name):
                    continue
                trend_catalog[catalog_name] = value

        store_as_of_row = connection.execute(
            "SELECT MAX(day) AS as_of_day FROM trend_daily_market"
        ).fetchone()
        store_as_of_day = _optional_text(
            store_as_of_row["as_of_day"] if store_as_of_row is not None else None
        )
        if store_as_of_day is None:
            return {}, {
                "state": "ready",
                "reason": "no_daily_rows",
                "table": "trend_daily_market",
                "knife_count": 0,
                "as_of_day": None,
            }

        query_template = f"""
            WITH latest AS (
                SELECT market_hash_name, MAX(day) AS last_observed_day
                FROM trend_daily_market
                WHERE day<=? AND {{where_clause}}
                GROUP BY market_hash_name
            )
            SELECT l.market_hash_name, ? AS as_of_day, l.last_observed_day,
                   d.median_price_cents AS last_observed_price_cents,
                   CASE WHEN l.last_observed_day>=DATE(?, '-{CURRENT_PRICE_MAX_AGE_DAYS - 1} day')
                        THEN d.median_price_cents END AS current_price_cents,
                   CASE WHEN l.last_observed_day>=DATE(?, '-{CURRENT_PRICE_MAX_AGE_DAYS - 1} day')
                        THEN d.sales_count ELSE 0 END AS latest_day_sales,
                   (SELECT h.median_price_cents FROM trend_daily_market AS h
                    WHERE h.market_hash_name=l.market_hash_name
                      AND h.day<=DATE(?, '-7 day')
                      AND h.day>=DATE(?, '-{7 + COMPARISON_PRICE_MAX_LAG_DAYS - 1} day')
                    ORDER BY h.day DESC LIMIT 1) AS price_7d_cents,
                   (SELECT h.median_price_cents FROM trend_daily_market AS h
                    WHERE h.market_hash_name=l.market_hash_name
                      AND h.day<=DATE(?, '-30 day')
                      AND h.day>=DATE(?, '-{30 + COMPARISON_PRICE_MAX_LAG_DAYS - 1} day')
                    ORDER BY h.day DESC LIMIT 1) AS price_30d_cents,
                   (SELECT COALESCE(SUM(h.sales_count), 0)
                    FROM trend_daily_market AS h
                    WHERE h.market_hash_name=l.market_hash_name
                      AND h.day>=DATE(?, '-29 day')
                      AND h.day<=?) AS sales_30d,
                   (SELECT COUNT(*) FROM trend_daily_market AS h
                    WHERE h.market_hash_name=l.market_hash_name) AS history_days,
                   (SELECT MIN(h.day) FROM trend_daily_market AS h
                    WHERE h.market_hash_name=l.market_hash_name) AS first_observed_day
            FROM latest AS l
            JOIN trend_daily_market AS d
              ON d.market_hash_name=l.market_hash_name AND d.day=l.last_observed_day
            """
        query_batches: list[tuple[str, tuple[Any, ...]]] = []
        if authoritative_names is None:
            query_batches.append(("market_hash_name LIKE ?", ("★ %",)))
        else:
            ordered_names = sorted(authoritative_names, key=str.casefold)
            for start in range(0, len(ordered_names), 400):
                batch = ordered_names[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                query_batches.append(
                    (f"market_hash_name IN ({placeholders})", tuple(batch))
                )
        result: dict[str, dict[str, Any]] = {}
        for where_clause, params in query_batches:
            date_params = (store_as_of_day,) * 10
            rows = connection.execute(
                query_template.format(where_clause=where_clause),
                (store_as_of_day, *params, *date_params[1:]),
            )
            for raw_row in rows:
                raw = dict(raw_row)
                name = str(raw["market_hash_name"])
                if authoritative_names is None and not is_canonical_knife_name(name):
                    continue
                sales_30d = max(0, _optional_int(raw.get("sales_30d")) or 0)
                sales_per_day = sales_30d / 30.0
                liquidity = min(
                    100.0,
                    100.0 * math.log1p(sales_per_day) / math.log1p(30.0),
                )
                extra = trend_catalog.get(name, {})
                trend_7d = _return_percent(
                    raw.get("current_price_cents"), raw.get("price_7d_cents")
                )
                trend_30d = _return_percent(
                    raw.get("current_price_cents"), raw.get("price_30d_cents")
                )
                last_observed_day = _optional_text(raw.get("last_observed_day"))
                result[name] = {
                    "current_price_cents": _optional_float(
                        raw.get("current_price_cents")
                    ),
                    "price_7d_cents": _optional_float(raw.get("price_7d_cents")),
                    "price_30d_cents": _optional_float(raw.get("price_30d_cents")),
                    "last_observed_price_cents": _optional_float(
                        raw.get("last_observed_price_cents")
                    ),
                    "last_observed_day": last_observed_day,
                    "price_is_stale": raw.get("current_price_cents") is None,
                    "trend_7d_percent": trend_7d,
                    "trend_30d_percent": trend_30d,
                    "direction": _trend_direction(
                        trend_30d if trend_30d is not None else trend_7d
                    ),
                    "sales_30d": sales_30d,
                    "sales_per_day_30d": round(sales_per_day, 4),
                    "liquidity_score": round(liquidity, 4),
                    "latest_day_sales": max(
                        0, _optional_int(raw.get("latest_day_sales")) or 0
                    ),
                    "history_days": max(
                        0, _optional_int(raw.get("history_days")) or 0
                    ),
                    "first_observed_day": _optional_text(
                        raw.get("first_observed_day")
                    ),
                    "as_of_day": _optional_text(raw.get("as_of_day")),
                    "observed_sales_count": _optional_int(
                        extra.get("observed_sales_count")
                    ),
                    "unique_asset_count": _optional_int(
                        extra.get("unique_asset_count")
                    ),
                    "latest_reference_quantity": _optional_int(
                        extra.get("latest_reference_quantity")
                    ),
                    "latest_reference_at": _optional_text(
                        extra.get("latest_reference_at")
                    ),
                    "trend_collection_name": _optional_text(
                        extra.get("collection_name")
                    ),
                    "trend_icon_url": _safe_image_url(extra.get("icon_url")),
                    "trend_min_float": _optional_float(extra.get("min_float")),
                    "trend_max_float": _optional_float(extra.get("max_float")),
                }
        return result, {
            "state": "ready",
            "reason": None,
            "table": "trend_daily_market",
            "knife_count": len(result),
            "as_of_day": store_as_of_day,
        }


def _fallback_item(name: str) -> dict[str, Any]:
    parsed = parse_knife_name(name)
    return {
        "market_hash_name": name,
        "item_kind": "knife",
        "knife_type": parsed["knife_type"],
        "weapon_id": None,
        "weapon_def_index": None,
        "paint_id": None,
        "paint_index": None,
        "paint_indices": [],
        "finish": parsed["finish"],
        "phase_id": None,
        "phase": parsed["phase"],
        "phase_ids": [],
        "phases": [parsed["phase"]] if parsed["phase"] else [],
        "variant_count": 1,
        "variants": [],
        "wear_id": None,
        "wear": parsed["wear"],
        "min_float": None,
        "max_float": None,
        "stattrak": parsed["stattrak"],
        "souvenir": False,
        "collections": [],
        "collection_name": None,
        "crates": [],
        "icon_url": None,
        "source_id": None,
        "source_updated_at": None,
        "catalog_build_id": None,
    }


def _load_knife_opportunities(
    path: str | Path,
    *,
    now: datetime | None = None,
    limit: int = MAX_LIVE_OPPORTUNITIES,
) -> dict[str, Any]:
    """Read recently observed knife offers without claiming current availability."""

    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None:
        observed_now = observed_now.replace(tzinfo=timezone.utc)
    observed_now = observed_now.astimezone(timezone.utc)
    cutoff = observed_now - timedelta(hours=LIVE_OFFER_MAX_AGE_HOURS)
    status = {
        "items": [],
        "count": 0,
        "state": "unavailable",
        "reason": "predictions_unavailable",
        "freshness_hours": LIVE_OFFER_MAX_AGE_HOURS,
        "observed_after": cutoff.isoformat(),
        "availability": "recently_observed",
        "availability_guaranteed": False,
    }
    with _read_connection(path) as connection:
        if connection is None:
            return status
        prediction_columns = _table_columns(connection, "model_deal_predictions")
        required = {
            "listing_id",
            "model_name",
            "last_seen_at",
            "skin_name",
            "buy_price_cents",
            "verdict",
        }
        if not required.issubset(prediction_columns):
            return status

        source_columns = _table_columns(connection, "model_deal_listing_sources")
        if {"listing_id", "source_type", "last_seen_at"}.issubset(source_columns):
            source_membership = (
                "EXISTS (SELECT 1 FROM model_deal_listing_sources AS s "
                "WHERE s.listing_id=p.listing_id AND s.source_type='knife' "
                "AND s.last_seen_at>=?)"
            )
            source_params: tuple[Any, ...] = (cutoff.isoformat(),)
        elif "source_type" in prediction_columns:
            source_membership = "p.source_type='knife'"
            source_params = ()
        else:
            return status

        optional = (
            "predicted_price_cents",
            "low_price_cents",
            "profit_cents",
            "roi_percent",
            "liquidity_score",
            "historical_sales_count",
            "sales_per_day",
            "float_value",
            "paint_seed",
            "paint_index",
            "sticker_count",
            "sticker_value_cents",
            "item_icon_url",
            "screenshot_url",
            "csfloat_url",
        )
        selected = sorted(required) + [
            column for column in optional if column in prediction_columns
        ]
        roi_order = (
            "COALESCE(p.roi_percent, -1000000)"
            if "roi_percent" in prediction_columns
            else "0"
        )
        profit_order = (
            "COALESCE(p.profit_cents, -1000000)"
            if "profit_cents" in prediction_columns
            else "0"
        )
        rows = connection.execute(
            f"SELECT {', '.join('p.' + column for column in selected)} "
            "FROM model_deal_predictions AS p "
            "WHERE p.model_name='knives' "
            "AND p.verdict IN ('good','watch') "
            "AND p.last_seen_at>=? "
            "AND p.buy_price_cents>0 "
            f"AND {source_membership} "
            f"ORDER BY {roi_order} DESC, {profit_order} DESC, p.last_seen_at DESC "
            "LIMIT ?",
            (cutoff.isoformat(), *source_params, max(1, min(limit, MAX_LIVE_OPPORTUNITIES))),
        )
        items: list[dict[str, Any]] = []
        for raw_row in rows:
            raw = dict(raw_row)
            items.append(
                {
                    "listing_id": _optional_text(raw.get("listing_id")),
                    "market_hash_name": _optional_text(raw.get("skin_name")),
                    "buy_price_cents": _optional_int(raw.get("buy_price_cents")),
                    "predicted_price_cents": _optional_int(
                        raw.get("predicted_price_cents")
                    ),
                    "low_price_cents": _optional_int(raw.get("low_price_cents")),
                    "estimated_profit_cents": _optional_int(raw.get("profit_cents")),
                    "estimated_roi_percent": _optional_float(raw.get("roi_percent")),
                    "liquidity_score": _optional_float(raw.get("liquidity_score")),
                    "historical_sales_count": _optional_int(
                        raw.get("historical_sales_count")
                    ),
                    "sales_per_day": _optional_float(raw.get("sales_per_day")),
                    "float_value": _optional_float(raw.get("float_value")),
                    "paint_seed": _optional_int(raw.get("paint_seed")),
                    "paint_index": _optional_int(raw.get("paint_index")),
                    "sticker_count": max(
                        0, _optional_int(raw.get("sticker_count")) or 0
                    ),
                    "sticker_value_cents": _optional_int(
                        raw.get("sticker_value_cents")
                    ),
                    "icon_url": _safe_image_url(raw.get("item_icon_url")),
                    "screenshot_url": _safe_image_url(raw.get("screenshot_url")),
                    "csfloat_url": _safe_csfloat_url(raw.get("csfloat_url")),
                    "verdict": _optional_text(raw.get("verdict")),
                    "observed_at": _optional_text(raw.get("last_seen_at")),
                    "source_type": "knife",
                    "model_name": "knives",
                    "availability": "recently_observed",
                    "availability_guaranteed": False,
                }
            )
        status.update(
            {
                "items": items,
                "count": len(items),
                "state": "ready",
                "reason": None if items else "no_recent_actionable_offers",
            }
        )
        return status


def _facet(items: list[Mapping[str, Any]], field: str) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for item in items:
        value = item.get(field)
        if value:
            counter[str(value)] += 1
    return [
        {"value": value, "count": count}
        for value, count in sorted(counter.items(), key=lambda pair: pair[0].casefold())
    ]


def _collection_facet(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for item in items:
        for value in [*(item.get("collections") or []), *(item.get("crates") or [])]:
            if value:
                counter[str(value)] += 1
    return [
        {"value": value, "count": count}
        for value, count in sorted(counter.items(), key=lambda pair: pair[0].casefold())
    ]


def _list_facet(items: list[Mapping[str, Any]], field: str) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for item in items:
        for value in item.get(field) or []:
            if value:
                counter[str(value)] += 1
    return [
        {"value": value, "count": count}
        for value, count in sorted(counter.items(), key=lambda pair: pair[0].casefold())
    ]


def build_knives_payload(
    catalog_db_path: str | Path,
    trend_db_path: str | Path,
    *,
    search: Any = "",
    knife_type: Any = "",
    finish: Any = "",
    phase: Any = "",
    wear: Any = "",
    collection: Any = "",
    min_price: Any = None,
    max_price: Any = None,
    min_liquidity: Any = 0,
    direction: Any = "all",
    sort: Any = "liquidity",
    limit: Any = 100,
    offset: Any = 0,
) -> dict[str, Any]:
    """Build one bounded, deterministic knife-market response."""

    filters = {
        "search": _text_filter(search, name="search"),
        "knife_type": _text_filter(knife_type, name="knife_type"),
        "finish": _text_filter(finish, name="finish"),
        "phase": _text_filter(phase, name="phase"),
        "wear": _text_filter(wear, name="wear"),
        "collection": _text_filter(collection, name="collection"),
    }
    minimum_price = _price_cents(min_price, name="min_price")
    maximum_price = _price_cents(max_price, name="max_price")
    if (
        minimum_price is not None
        and maximum_price is not None
        and minimum_price > maximum_price
    ):
        raise ValueError("min_price must not exceed max_price")
    minimum_liquidity = _bounded_float(
        min_liquidity,
        name="min_liquidity",
        default=0,
        minimum=0,
        maximum=100,
    )
    direction_name = str(direction or "all").strip().lower()
    if direction_name not in DIRECTION_VALUES:
        raise ValueError("direction must be all, up, down, flat, or no_data")
    sort_name = str(sort or "liquidity").strip().lower()
    if sort_name not in SORT_VALUES:
        raise ValueError(
            "sort must be liquidity, sales_30d, price_low, price_high, "
            "trend_7d, trend_30d, or name"
        )
    limit_value = _bounded_int(
        limit, name="limit", default=100, minimum=1, maximum=MAX_LIMIT
    )
    offset_value = _bounded_int(
        offset, name="offset", default=0, minimum=0, maximum=MAX_OFFSET
    )

    catalog, catalog_status = _load_catalog(catalog_db_path)
    catalog_ready = catalog_status["state"] == "ready"
    trend, trend_status = _load_trend_rows(
        trend_db_path,
        authoritative_names=set(catalog) if catalog_ready else None,
    )
    opportunities = _load_knife_opportunities(catalog_db_path)
    names = set(catalog) if catalog_ready else set(trend)
    all_items: list[dict[str, Any]] = []
    for name in sorted(names, key=str.casefold):
        item = dict(catalog.get(name) or _fallback_item(name))
        observed = trend.get(name)
        if observed:
            item.update(observed)
            if not item.get("icon_url"):
                item["icon_url"] = observed.get("trend_icon_url")
            if not item.get("collections") and observed.get("trend_collection_name"):
                item["collections"] = [observed["trend_collection_name"]]
                item["collection_name"] = observed["trend_collection_name"]
            if item.get("min_float") is None:
                item["min_float"] = observed.get("trend_min_float")
            if item.get("max_float") is None:
                item["max_float"] = observed.get("trend_max_float")
        else:
            item.update(
                {
                    "current_price_cents": None,
                    "price_7d_cents": None,
                    "price_30d_cents": None,
                    "last_observed_price_cents": None,
                    "last_observed_day": None,
                    "price_is_stale": False,
                    "trend_7d_percent": None,
                    "trend_30d_percent": None,
                    "direction": "no_data",
                    "sales_30d": 0,
                    "sales_per_day_30d": 0.0,
                    "liquidity_score": 0.0,
                    "latest_day_sales": 0,
                    "history_days": 0,
                    "first_observed_day": None,
                    "as_of_day": None,
                    "observed_sales_count": None,
                    "unique_asset_count": None,
                    "latest_reference_quantity": None,
                    "latest_reference_at": None,
                }
            )
        item.pop("trend_icon_url", None)
        item.pop("trend_collection_name", None)
        item.pop("trend_min_float", None)
        item.pop("trend_max_float", None)
        item["price_source"] = (
            "observed_daily_median" if item.get("current_price_cents") is not None else None
        )
        all_items.append(item)

    facets = {
        "knife_types": _facet(all_items, "knife_type"),
        "finishes": _facet(all_items, "finish"),
        "phases": _list_facet(all_items, "phases"),
        "wears": _facet(all_items, "wear"),
        "collections": _collection_facet(all_items),
    }

    search_folded = filters["search"].casefold()
    selected = []
    for item in all_items:
        haystack = " ".join(
            str(value or "")
            for value in (
                item.get("market_hash_name"),
                item.get("knife_type"),
                item.get("finish"),
                item.get("phase"),
                " ".join(item.get("phases") or []),
                item.get("wear"),
                " ".join(item.get("collections") or []),
                " ".join(item.get("crates") or []),
            )
        ).casefold()
        if search_folded and search_folded not in haystack:
            continue
        if filters["knife_type"] and item.get("knife_type") != filters["knife_type"]:
            continue
        if filters["finish"] and item.get("finish") != filters["finish"]:
            continue
        if filters["phase"] and filters["phase"] not in (item.get("phases") or []):
            continue
        if filters["wear"] and item.get("wear") != filters["wear"]:
            continue
        if filters["collection"] and filters["collection"] not in (
            [*(item.get("collections") or []), *(item.get("crates") or [])]
        ):
            continue
        price = _optional_float(item.get("current_price_cents"))
        if minimum_price is not None and (price is None or price < minimum_price):
            continue
        if maximum_price is not None and (price is None or price > maximum_price):
            continue
        if float(item.get("liquidity_score") or 0) < minimum_liquidity:
            continue
        if direction_name != "all" and item.get("direction") != direction_name:
            continue
        selected.append(item)

    def missing_last(value: Any, *, low: bool = False) -> float:
        number = _optional_float(value)
        if number is None:
            return math.inf if low else -math.inf
        return number

    if sort_name == "name":
        selected.sort(key=lambda item: str(item["market_hash_name"]).casefold())
    elif sort_name == "price_low":
        selected.sort(
            key=lambda item: (
                missing_last(item.get("current_price_cents"), low=True),
                str(item["market_hash_name"]).casefold(),
            )
        )
    else:
        field = {
            "liquidity": "liquidity_score",
            "sales_30d": "sales_30d",
            "price_high": "current_price_cents",
            "trend_7d": "trend_7d_percent",
            "trend_30d": "trend_30d_percent",
        }[sort_name]
        selected.sort(
            key=lambda item: (
                missing_last(item.get(field)),
                float(item.get("sales_30d") or 0),
                str(item["market_hash_name"]).casefold(),
            ),
            reverse=True,
        )

    total_filtered = len(selected)
    page = selected[offset_value : offset_value + limit_value]
    priced = [
        float(item["current_price_cents"])
        for item in selected
        if item.get("current_price_cents") is not None
    ]
    observed_count = sum(1 for item in all_items if item.get("history_days"))
    model_state = (
        "collecting_data"
        if catalog_ready or trend_status["state"] == "ready"
        else "unavailable"
    )
    status = "ready" if all_items else (
        "collecting_data" if catalog_ready or trend_status["state"] == "ready" else "unavailable"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason": None if all_items else "no_knife_catalog_or_market_history",
        "items": page,
        "summary": {
            "total_knives": len(all_items),
            "filtered_count": total_filtered,
            "shown_count": len(page),
            "priced_count": len(priced),
            "history_count": observed_count,
            "sales_30d": sum(int(item.get("sales_30d") or 0) for item in selected),
            "median_price_cents": round(statistics.median(priced), 2) if priced else None,
            "average_liquidity_score": (
                round(
                    sum(float(item.get("liquidity_score") or 0) for item in selected)
                    / len(selected),
                    4,
                )
                if selected
                else None
            ),
            "as_of_day": trend_status.get("as_of_day"),
        },
        "facets": facets,
        "filters": {
            **filters,
            "min_price": min_price,
            "max_price": max_price,
            "min_liquidity": minimum_liquidity,
            "direction": direction_name,
            "sort": sort_name,
            "limit": limit_value,
            "offset": offset_value,
        },
        "pagination": {
            "limit": limit_value,
            "offset": offset_value,
            "has_more": offset_value + len(page) < total_filtered,
        },
        "sources": {
            "catalog": catalog_status,
            "trend": trend_status,
            "catalog_fallback_used": not catalog_ready,
            "liquidity_scope": "observed_project_sales_30d",
            "supply_is_world_inventory": False,
        },
        "opportunities": opportunities,
        "model_status": {
            "model_id": "knives",
            "item_kind": "knife",
            "state": model_state,
            "dedicated_model_available": False,
            "training_action_available": False,
            "forecast_used": False,
            "fallback": "observed_market_history",
            "observed_horizons_days": [7, 30],
        },
    }


# The document is kept in this module so the read contract and its honest
# model/fallback copy evolve together.  It is intentionally self-contained and
# uses the same locale cookie as the rest of the site.
KNIVES_HTML = r"""<!doctype html>
<html lang="__LOCALE__">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>Ножи · CS2 Market Lab</title>
  <style>
    :root{--bg:#070a11;--panel:#101620;--panel-2:#151d29;--line:#263244;--text:#f4f7fb;--muted:#98a6ba;--accent:#7c5cff;--accent-2:#45d6b0;--good:#47d18c;--bad:#ff6b7c;--warn:#f3ba63;--shadow:0 22px 60px rgba(0,0,0,.28);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    body.light{--bg:#f3f6fb;--panel:#fff;--panel-2:#f8faff;--line:#dce3ee;--text:#172033;--muted:#657087;--accent:#6548e8;--accent-2:#0f9f7b;--good:#16865e;--bad:#c73c52;--warn:#9b650b;--shadow:0 18px 45px rgba(31,48,79,.10)}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;min-height:100vh;background:radial-gradient(900px 480px at 0 -10%,rgba(124,92,255,.19),transparent 65%),radial-gradient(700px 430px at 100% 0,rgba(69,214,176,.10),transparent 68%),var(--bg);color:var(--text)}
    button,input,select{font:inherit}button,a{-webkit-tap-highlight-color:transparent}:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 38%,transparent);outline-offset:2px}[hidden]{display:none!important}
    header{position:sticky;top:0;z-index:20;border-bottom:1px solid color-mix(in srgb,var(--line) 78%,transparent);background:color-mix(in srgb,var(--bg) 84%,transparent);backdrop-filter:blur(18px)}
    .topbar,.wrap{width:min(1440px,calc(100% - 36px));margin:auto}.topbar{min-height:72px;display:flex;align-items:center;gap:22px}.brand{display:flex;align-items:center;gap:11px;color:var(--text);text-decoration:none;white-space:nowrap}.mark{display:grid;place-items:center;width:38px;height:38px;border:1px solid color-mix(in srgb,var(--accent) 50%,var(--line));border-radius:12px;background:linear-gradient(145deg,color-mix(in srgb,var(--accent) 35%,var(--panel)),var(--panel));font-size:18px}.brand-copy{display:grid}.brand-copy strong{font-size:14px}.brand-copy small{color:var(--muted);font-size:10px;letter-spacing:.08em;text-transform:uppercase}
    nav{display:flex;align-items:center;gap:4px;flex:1;overflow-x:auto}nav a{padding:9px 11px;border-radius:10px;color:var(--muted);font-size:12px;font-weight:750;text-decoration:none;white-space:nowrap}nav a:hover,nav a.active{background:color-mix(in srgb,var(--accent) 14%,transparent);color:var(--text)}
    .top-actions{display:flex;align-items:center;gap:8px}.top-actions select,.icon-button{min-height:36px;border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--text)}.top-actions select{padding:0 9px}.icon-button{min-width:38px;padding:0 10px;cursor:pointer}
    main{padding:34px 0 70px}.hero{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(300px,.6fr);gap:22px;align-items:stretch}.hero-copy,.model-note,.panel,.metric{border:1px solid var(--line);background:linear-gradient(145deg,color-mix(in srgb,var(--panel) 96%,var(--accent) 4%),var(--panel));box-shadow:var(--shadow)}.hero-copy{padding:30px;border-radius:24px}.eyebrow{color:var(--accent-2);font-size:11px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.hero h1{max-width:760px;margin:9px 0 10px;font-size:clamp(30px,5vw,56px);line-height:1.02;letter-spacing:-.045em}.lead{max-width:760px;margin:0;color:var(--muted);font-size:15px;line-height:1.7}.model-note{padding:24px;border-radius:24px}.status-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.status-pill{display:inline-flex;align-items:center;gap:7px;padding:7px 10px;border:1px solid color-mix(in srgb,var(--warn) 38%,var(--line));border-radius:999px;background:color-mix(in srgb,var(--warn) 10%,transparent);color:var(--warn);font-size:11px;font-weight:800}.status-pill::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}.model-note h2{margin:18px 0 7px;font-size:18px}.model-note p{margin:0;color:var(--muted);font-size:13px;line-height:1.6}.truth-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.truth-chip{padding:6px 9px;border:1px solid var(--line);border-radius:9px;color:var(--muted);font-size:10px;font-weight:750}
    .metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:18px 0}.metric{min-height:116px;padding:17px;border-radius:17px;box-shadow:none}.metric span{display:block;color:var(--muted);font-size:11px;font-weight:700}.metric strong{display:block;margin-top:8px;font-size:25px;letter-spacing:-.03em}.metric small{display:block;margin-top:5px;color:var(--muted);font-size:10px}
    .panel{border-radius:22px;overflow:hidden}.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:20px 22px;border-bottom:1px solid var(--line)}.panel-head h2{margin:0;font-size:19px}.panel-head p{margin:5px 0 0;color:var(--muted);font-size:12px}.view-toggle{display:flex;gap:4px;padding:3px;border:1px solid var(--line);border-radius:11px;background:var(--bg)}.view-toggle button{border:0;border-radius:8px;padding:7px 10px;background:transparent;color:var(--muted);cursor:pointer;font-size:11px;font-weight:750}.view-toggle button.active{background:var(--panel-2);color:var(--text)}
    .filters{display:grid;grid-template-columns:2fr repeat(4,minmax(125px,1fr));gap:10px;padding:18px 22px;border-bottom:1px solid var(--line)}.field{display:grid;gap:6px;min-width:0}.field span{color:var(--muted);font-size:10px;font-weight:750}.field input,.field select{width:100%;min-height:40px;padding:0 11px;border:1px solid var(--line);border-radius:10px;background:var(--panel-2);color:var(--text)}.field input::placeholder{color:color-mix(in srgb,var(--muted) 68%,transparent)}.range-row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.filter-actions{display:flex;align-items:flex-end;gap:8px}.button{min-height:40px;padding:0 14px;border:1px solid var(--line);border-radius:10px;background:var(--panel-2);color:var(--text);font-weight:750;cursor:pointer}.button.primary{border-color:color-mix(in srgb,var(--accent) 62%,var(--line));background:var(--accent);color:#fff}.button:hover{filter:brightness(1.08)}
    .result-meta{display:flex;align-items:center;justify-content:space-between;gap:15px;padding:13px 22px;color:var(--muted);font-size:11px}.source-note{display:flex;align-items:center;gap:7px}.source-dot{width:7px;height:7px;border-radius:50%;background:var(--good)}.source-dot.fallback{background:var(--warn)}
    .opportunity-panel{margin:18px 0}.opportunity-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:18px 22px 22px}.opportunity-card{padding:16px;border:1px solid var(--line);border-radius:15px;background:var(--panel-2)}.opportunity-card h3{margin:0;font-size:13px;line-height:1.45}.opportunity-price{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;margin-top:13px}.opportunity-price strong{font-size:20px}.opportunity-price span{color:var(--good);font-size:12px;font-weight:800}.opportunity-meta{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0;color:var(--muted);font-size:10px}.opportunity-link{display:inline-flex;color:var(--accent-2);font-size:11px;font-weight:800;text-decoration:none}.opportunity-empty{padding:24px 22px;color:var(--muted);font-size:12px;line-height:1.6}
    .cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:0 22px 22px}.knife-card{display:grid;grid-template-columns:104px minmax(0,1fr);gap:15px;min-height:190px;padding:15px;border:1px solid var(--line);border-radius:16px;background:var(--panel-2);cursor:pointer;transition:transform .15s,border-color .15s}.knife-card:hover{transform:translateY(-2px);border-color:color-mix(in srgb,var(--accent) 55%,var(--line))}.knife-image,.knife-placeholder{width:104px;height:92px;align-self:start;border-radius:13px;background:radial-gradient(circle at 50% 40%,rgba(124,92,255,.22),transparent 65%),var(--bg);object-fit:contain}.knife-placeholder{display:grid;place-items:center;color:var(--muted);font-size:27px}.knife-card h3{margin:0;font-size:13px;line-height:1.45}.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}.tag{padding:4px 6px;border:1px solid var(--line);border-radius:7px;color:var(--muted);font-size:9px}.card-price{margin-top:12px;font-size:21px;font-weight:850}.card-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-top:10px}.card-stat{padding:7px;border-radius:9px;background:var(--bg)}.card-stat span{display:block;color:var(--muted);font-size:8px;text-transform:uppercase}.card-stat strong{display:block;margin-top:3px;font-size:11px}.up{color:var(--good)!important}.down{color:var(--bad)!important}.flat{color:var(--muted)!important}.no-data{color:var(--muted)!important}
    .table-wrap{padding:0 22px 22px;overflow:auto}table{width:100%;border-collapse:separate;border-spacing:0;font-size:12px}th{position:sticky;top:72px;padding:10px 9px;border-bottom:1px solid var(--line);background:var(--panel);color:var(--muted);font-size:9px;text-align:left;text-transform:uppercase}td{padding:12px 9px;border-bottom:1px solid var(--line);vertical-align:middle}.name-cell{min-width:260px}.name-cell strong{display:block}.name-cell small{display:block;margin-top:3px;color:var(--muted)}tbody tr{cursor:pointer}tbody tr:hover{background:color-mix(in srgb,var(--accent) 7%,transparent)}
    .empty{margin:0 22px 22px;padding:44px 20px;border:1px dashed var(--line);border-radius:16px;text-align:center}.empty-icon{font-size:32px}.empty h3{margin:10px 0 6px}.empty p{max-width:600px;margin:auto;color:var(--muted);font-size:12px;line-height:1.6}.loading{padding:60px;text-align:center;color:var(--muted)}.pagination{display:flex;align-items:center;justify-content:center;gap:10px;padding:0 22px 22px}.pagination span{min-width:100px;text-align:center;color:var(--muted);font-size:11px}.error-banner{margin:0 22px 18px;padding:12px 14px;border:1px solid color-mix(in srgb,var(--bad) 50%,var(--line));border-radius:11px;background:color-mix(in srgb,var(--bad) 10%,transparent);color:var(--bad);font-size:12px}
    dialog{width:min(900px,calc(100% - 24px));max-height:calc(100vh - 32px);padding:0;border:1px solid var(--line);border-radius:22px;background:var(--panel);color:var(--text);box-shadow:0 30px 100px rgba(0,0,0,.5)}dialog::backdrop{background:rgba(1,4,10,.76);backdrop-filter:blur(5px)}.dialog-head{display:flex;justify-content:space-between;gap:14px;padding:20px 22px;border-bottom:1px solid var(--line)}.dialog-head h2{margin:0;font-size:18px}.dialog-head p{margin:5px 0 0;color:var(--muted);font-size:11px}.close{width:36px;height:36px;border:1px solid var(--line);border-radius:10px;background:var(--panel-2);color:var(--text);cursor:pointer}.dialog-content{padding:20px 22px;overflow:auto}.detail-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px;margin-bottom:18px}.detail-box{padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--panel-2)}.detail-box span{display:block;color:var(--muted);font-size:9px;text-transform:uppercase}.detail-box strong{display:block;margin-top:5px;font-size:12px}.chart-shell{min-height:290px;padding:14px;border:1px solid var(--line);border-radius:15px;background:var(--bg)}.chart-title{display:flex;justify-content:space-between;gap:12px;margin-bottom:9px}.chart-title strong{font-size:12px}.chart-title span{color:var(--muted);font-size:10px}svg{display:block;width:100%;height:auto;overflow:visible}.chart-grid{stroke:var(--line);stroke-width:1}.chart-path{fill:none;stroke:var(--accent-2);stroke-width:3;stroke-linejoin:round;stroke-linecap:round}.chart-area{fill:url(#knifeGradient);opacity:.28}.chart-label{fill:var(--muted);font-size:10px}.disclaimer{margin:15px 0 0;color:var(--muted);font-size:10px;line-height:1.55}
    @media(max-width:1100px){.metrics{grid-template-columns:repeat(3,1fr)}.filters{grid-template-columns:repeat(3,1fr)}.filters .search-field{grid-column:span 2}.cards,.opportunity-grid{grid-template-columns:repeat(2,1fr)}}
    @media(max-width:760px){.topbar,.wrap{width:min(100% - 20px,1440px)}.topbar{flex-wrap:wrap;padding:11px 0;gap:10px}.brand-copy small{display:none}nav{order:3;width:100%;flex-basis:100%}.top-actions{margin-left:auto}.hero{grid-template-columns:1fr}.hero-copy{padding:22px}.metrics{grid-template-columns:1fr 1fr}.filters{grid-template-columns:1fr 1fr;padding:15px}.filters .search-field{grid-column:1/-1}.filter-actions{grid-column:1/-1}.cards,.opportunity-grid{grid-template-columns:1fr;padding:0 15px 15px}.panel-head,.result-meta{padding-left:15px;padding-right:15px}.table-wrap{padding:0 15px 15px}.detail-grid{grid-template-columns:1fr 1fr}}
    @media(max-width:430px){.metrics{grid-template-columns:1fr}.filters{grid-template-columns:1fr}.filters .search-field{grid-column:auto}.knife-card{grid-template-columns:78px minmax(0,1fr)}.knife-image,.knife-placeholder{width:78px;height:72px}.card-stats{grid-template-columns:1fr 1fr}.detail-grid{grid-template-columns:1fr}.hero h1{font-size:34px}}
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <a class="brand" href="/"><span class="mark">◆</span><span class="brand-copy"><strong>CS2 Market Lab</strong><small data-i18n="brandSub">аналитика рынка</small></span></a>
      <nav aria-label="Primary">
        <a data-nav-path="/" href="/" data-i18n="navOverview">Обзор</a>
        <a data-nav-path="/deals" href="/deals" data-i18n="navDeals">Сделки</a>
        <a data-nav-path="/strategy" href="/strategy" data-i18n="navStrategy">Стратегия</a>
        <a class="active" aria-current="page" data-nav-path="/knives" href="/knives" data-i18n="navKnives">Ножи</a>
        <a data-nav-path="/account" href="/account" data-i18n="navInventory">Инвентарь</a>
      </nav>
      <div class="top-actions"><select id="localeSelect" aria-label="Language"><option value="ru">RU</option><option value="en">EN</option></select><button class="icon-button" id="themeButton" type="button" aria-label="Theme">◐</button></div>
    </div>
  </header>
  <main class="wrap" id="main-content">
    <section class="hero">
      <div class="hero-copy"><span class="eyebrow" data-i18n="eyebrow">Ножи · наблюдаемый рынок</span><h1 data-i18n="heroTitle">Фазы, ликвидность и история цены — без выдуманного прогноза.</h1><p class="lead" data-i18n="heroLead">Ищи ножи по типу, покрытию, износу, коллекции или кейсу. Изменения за 7 и 30 дней считаются только по уже состоявшимся продажам.</p></div>
      <aside class="model-note" aria-live="polite"><div class="status-head"><span class="status-pill" id="modelState">Сбор данных</span><span class="eyebrow">model_id: knives</span></div><h2 id="modelTitle" data-i18n="modelTitle">Отдельной модели ножей пока нет</h2><p id="modelCopy" data-i18n="modelCopy">Здесь используется резервный режим: наблюдаемая история рынка. Значения 7/30 дней — прошлое изменение цены, а не прогноз будущей доходности.</p><div class="truth-row"><span class="truth-chip" data-i18n="noForecast">Без прогноза</span><span class="truth-chip" data-i18n="readOnly">Только чтение</span><span class="truth-chip" id="trainingStatus" data-i18n="noTraining">Без запуска обучения</span><span class="truth-chip" data-i18n="parentAggregate">Цена агрегирована по market name</span></div></aside>
    </section>
    <section class="metrics" aria-label="Summary">
      <article class="metric"><span data-i18n="metricTotal">Ножей в каталоге</span><strong id="metricTotal">—</strong><small data-i18n="catalogRows">уникальные market names</small></article>
      <article class="metric"><span data-i18n="metricPriced">С историей цены</span><strong id="metricPriced">—</strong><small data-i18n="observedOnly">наблюдаемые продажи</small></article>
      <article class="metric"><span data-i18n="metricSales">Продаж за 30 дней</span><strong id="metricSales">—</strong><small data-i18n="projectScope">в базе проекта</small></article>
      <article class="metric"><span data-i18n="metricMedian">Медианная цена</span><strong id="metricMedian">—</strong><small data-i18n="filteredSet">по текущему фильтру</small></article>
      <article class="metric"><span data-i18n="metricLiquidity">Средняя ликвидность</span><strong id="metricLiquidity">—</strong><small data-i18n="liquidityScale">шкала 0–100</small></article>
    </section>
    <section class="panel opportunity-panel" aria-labelledby="opportunityTitle">
      <div class="panel-head"><div><h2 id="opportunityTitle" data-i18n="opportunityTitle">Свежие предложения по ножам</h2><p data-i18n="opportunityLead">Только недавно замеченные CSFloat-лоты, оценённые отдельной knife-моделью. Наличие перепроверяй перед покупкой.</p></div><span class="truth-chip" data-i18n="recentlyObserved">Недавно замечены</span></div>
      <div id="liveOpportunities"><div class="opportunity-empty" data-i18n="opportunityLoading">Проверяем локальные результаты сканера…</div></div>
    </section>
    <section class="panel">
      <div class="panel-head"><div><h2 data-i18n="catalogTitle">Каталог ножей</h2><p data-i18n="catalogLead">Карточка открывает полный график наблюдаемой цены.</p></div><div class="view-toggle" role="group" aria-label="View"><button class="active" type="button" data-view="cards" data-i18n="cards">Карточки</button><button type="button" data-view="table" data-i18n="table">Таблица</button></div></div>
      <form class="filters" id="filterForm">
        <label class="field search-field"><span data-i18n="search">Поиск</span><input name="search" type="search" maxlength="180" placeholder="Karambit, Doppler, Ruby…" data-i18n-placeholder="searchPlaceholder"></label>
        <label class="field"><span data-i18n="knifeType">Тип ножа</span><select name="knife_type" id="knifeType"><option value="" data-i18n="all">Все</option></select></label>
        <label class="field"><span data-i18n="finish">Покрытие</span><select name="finish" id="finish"><option value="" data-i18n="all">Все</option></select></label>
        <label class="field"><span data-i18n="phase">Фаза</span><select name="phase" id="phase"><option value="" data-i18n="all">Все</option></select></label>
        <label class="field"><span data-i18n="wear">Износ</span><select name="wear" id="wear"><option value="" data-i18n="all">Все</option></select></label>
        <label class="field"><span data-i18n="collection">Коллекция / кейс</span><select name="collection" id="collection"><option value="" data-i18n="all">Все</option></select></label>
        <label class="field"><span data-i18n="priceRange">Цена, $</span><span class="range-row"><input name="min_price" inputmode="decimal" placeholder="от" data-i18n-placeholder="minPricePlaceholder" aria-label="Minimum price"><input name="max_price" inputmode="decimal" placeholder="до" data-i18n-placeholder="maxPricePlaceholder" aria-label="Maximum price"></span></label>
        <label class="field"><span data-i18n="minLiquidity">Ликвидность от</span><input name="min_liquidity" type="number" min="0" max="100" step="1" value="0"></label>
        <label class="field"><span data-i18n="direction">Движение 30 дней</span><select name="direction"><option value="all" data-i18n="all">Все</option><option value="up" data-i18n="up">Растёт</option><option value="down" data-i18n="down">Падает</option><option value="flat" data-i18n="flat">Без изменения</option><option value="no_data" data-i18n="noData">Нет данных</option></select></label>
        <label class="field"><span data-i18n="sort">Сортировка</span><select name="sort"><option value="liquidity" data-i18n="sortLiquidity">Ликвидность</option><option value="sales_30d" data-i18n="sortSales">Продажи за 30 дней</option><option value="price_low" data-i18n="sortPriceLow">Цена: дешевле</option><option value="price_high" data-i18n="sortPriceHigh">Цена: дороже</option><option value="trend_7d" data-i18n="sortTrend7">Рост за 7 дней</option><option value="trend_30d" data-i18n="sortTrend30">Рост за 30 дней</option><option value="name" data-i18n="sortName">Название</option></select></label>
        <div class="filter-actions"><button class="button primary" type="submit" data-i18n="apply">Применить</button><button class="button" id="resetFilters" type="button" data-i18n="reset">Сбросить</button></div>
      </form>
      <div class="result-meta"><span id="resultCount">—</span><span class="source-note"><i class="source-dot" id="sourceDot"></i><span id="sourceText">—</span></span></div>
      <div class="error-banner" id="errorBanner" role="alert" hidden></div>
      <div id="results"><div class="loading" data-i18n="loading">Загрузка…</div></div>
      <div class="pagination" id="pagination" hidden><button class="button" id="previousPage" type="button" data-i18n="previous">Назад</button><span id="pageLabel">—</span><button class="button" id="nextPage" type="button" data-i18n="next">Далее</button></div>
    </section>
  </main>
  <dialog id="detailDialog"><div class="dialog-head"><div><h2 id="detailName">—</h2><p id="detailSubtitle">—</p></div><button class="close" id="closeDialog" type="button" aria-label="Close">×</button></div><div class="dialog-content"><div class="detail-grid" id="detailGrid"></div><div class="chart-shell" id="chartShell"><div class="loading" data-i18n="chartLoading">Загружаем историю…</div></div><p class="disclaimer" data-i18n="detailDisclaimer">История отражает только данные, собранные проектом. Количество ассетов не является оценкой всех существующих предметов в мире.</p></div></dialog>
  <script>
  (()=>{
    'use strict';
    const INITIAL_LOCALE='__LOCALE__';
    const I18N={
      ru:{documentTitle:'Ножи · CS2 Market Lab',brandSub:'аналитика рынка',navOverview:'Обзор',navDeals:'Сделки',navStrategy:'Стратегия',navCatalog:'Каталог',navKnives:'Ножи',navInventory:'Инвентарь',eyebrow:'Ножи · наблюдаемый рынок',heroTitle:'Фазы, ликвидность и история цены — без выдуманного прогноза.',heroLead:'Ищи ножи по типу, покрытию, износу и коллекции. Изменения за 7 и 30 дней считаются только по уже состоявшимся продажам.',modelCollecting:'Сбор данных',modelUnavailable:'Недоступно',modelTitle:'Отдельной модели ножей пока нет',modelCopy:'Здесь используется резервный режим: наблюдаемая история рынка. Значения 7/30 дней — прошлое изменение цены, а не прогноз будущей доходности.',noForecast:'Без прогноза',readOnly:'Только чтение',noTraining:'Без запуска обучения',parentAggregate:'Цена агрегирована по market name',metricTotal:'Ножей в каталоге',catalogRows:'уникальные market names',metricPriced:'С историей цены',observedOnly:'наблюдаемые продажи',metricSales:'Продаж за 30 дней',projectScope:'в базе проекта',metricMedian:'Медианная цена',filteredSet:'по текущему фильтру',metricLiquidity:'Средняя ликвидность',liquidityScale:'шкала 0–100',catalogTitle:'Каталог ножей',catalogLead:'Карточка открывает полный график наблюдаемой цены.',cards:'Карточки',table:'Таблица',search:'Поиск',searchPlaceholder:'Karambit, Doppler, Ruby…',knifeType:'Тип ножа',finish:'Покрытие',phase:'Фаза',wear:'Износ',collection:'Коллекция',priceRange:'Цена, $',minPricePlaceholder:'от',maxPricePlaceholder:'до',minLiquidity:'Ликвидность от',direction:'Движение 30 дней',sort:'Сортировка',all:'Все',up:'Растёт',down:'Падает',flat:'Без изменения',noData:'Нет данных',sortLiquidity:'Ликвидность',sortSales:'Продажи за 30 дней',sortPriceLow:'Цена: дешевле',sortPriceHigh:'Цена: дороже',sortTrend7:'Рост за 7 дней',sortTrend30:'Рост за 30 дней',sortName:'Название',apply:'Применить',reset:'Сбросить',loading:'Загрузка…',previous:'Назад',next:'Далее',catalogSource:'Авторитетный каталог + история рынка',fallbackSource:'Каталог ещё не опубликован · резерв по именам',results:'Найдено: {filtered} · показано: {shown}',price:'Цена',liquidity:'Ликвидность',sales30:'Продажи 30д',trend7:'История 7д',trend30:'История 30д',name:'Название',variants:'Варианты',multiplePhases:'{count} фаз',noPrice:'Нет цены',openDetails:'Открыть историю',emptyTitle:'Ножи пока не появились в данных',emptyCopy:'Каталог или наблюдаемая история ножей ещё не опубликованы. Страница обновится после безопасного обновления парсера — модель и прогноз не подменяют отсутствующие данные.',emptyFilteredTitle:'По фильтрам ничего не найдено',emptyFilteredCopy:'Сбрось часть фильтров или расширь диапазон цены.',loadError:'Не удалось загрузить данные ножей.',page:'Страница {page}',chartLoading:'Загружаем историю…',chartTitle:'Наблюдаемая медианная цена',chartRange:'{count} дней',chartEmpty:'Для этого ножа пока нет дневной истории цены.',chartError:'Не удалось загрузить график.',asOf:'Данные на',historyDays:'Дней истории',assetsObserved:'Уникальных ассетов в базе',floatRange:'Диапазон float',collections:'Коллекции',phases:'Фазы',detailDisclaimer:'История отражает только данные, собранные проектом. Количество ассетов не является оценкой всех существующих предметов в мире.',unknown:'—'},
      en:{documentTitle:'Knives · CS2 Market Lab',brandSub:'market analytics',navOverview:'Overview',navDeals:'Deals',navStrategy:'Strategy',navCatalog:'Catalogue',navKnives:'Knives',navInventory:'Inventory',eyebrow:'Knives · observed market',heroTitle:'Phases, liquidity and price history — without an invented forecast.',heroLead:'Search knives by type, finish, wear and collection. The 7- and 30-day changes use completed observed sales only.',modelCollecting:'Collecting data',modelUnavailable:'Unavailable',modelTitle:'There is no dedicated knife model yet',modelCopy:'This page uses observed market history as a fallback. The 7/30-day values are past price changes, not forecasts of future returns.',noForecast:'No forecast',readOnly:'Read only',noTraining:'No training action',parentAggregate:'Price aggregated by market name',metricTotal:'Knives in catalogue',catalogRows:'unique market names',metricPriced:'With price history',observedOnly:'observed sales',metricSales:'Sales in 30 days',projectScope:'in the project database',metricMedian:'Median price',filteredSet:'for the current filter',metricLiquidity:'Average liquidity',liquidityScale:'0–100 scale',catalogTitle:'Knife catalogue',catalogLead:'Open a card to see the complete observed price chart.',cards:'Cards',table:'Table',search:'Search',searchPlaceholder:'Karambit, Doppler, Ruby…',knifeType:'Knife type',finish:'Finish',phase:'Phase',wear:'Wear',collection:'Collection',priceRange:'Price, $',minPricePlaceholder:'min',maxPricePlaceholder:'max',minLiquidity:'Minimum liquidity',direction:'30-day movement',sort:'Sort',all:'All',up:'Up',down:'Down',flat:'Flat',noData:'No data',sortLiquidity:'Liquidity',sortSales:'30-day sales',sortPriceLow:'Price: low first',sortPriceHigh:'Price: high first',sortTrend7:'7-day rise',sortTrend30:'30-day rise',sortName:'Name',apply:'Apply',reset:'Reset',loading:'Loading…',previous:'Previous',next:'Next',catalogSource:'Authoritative catalogue + market history',fallbackSource:'Catalogue not published yet · name fallback',results:'Found: {filtered} · shown: {shown}',price:'Price',liquidity:'Liquidity',sales30:'30d sales',trend7:'7d history',trend30:'30d history',name:'Name',variants:'Variants',multiplePhases:'{count} phases',noPrice:'No price',openDetails:'Open history',emptyTitle:'Knives have not appeared in the data yet',emptyCopy:'The knife catalogue or observed history has not been published yet. This page will populate after a safe parser refresh; a model or forecast does not replace missing data.',emptyFilteredTitle:'Nothing matches these filters',emptyFilteredCopy:'Reset some filters or widen the price range.',loadError:'Could not load knife data.',page:'Page {page}',chartLoading:'Loading history…',chartTitle:'Observed median price',chartRange:'{count} days',chartEmpty:'This knife has no daily price history yet.',chartError:'Could not load the chart.',asOf:'Data as of',historyDays:'History days',assetsObserved:'Unique assets in database',floatRange:'Float range',collections:'Collections',phases:'Phases',detailDisclaimer:'History reflects only data collected by this project. Asset counts are not an estimate of every existing item worldwide.',unknown:'—'}
    };
    Object.assign(I18N.ru,{navHistory:'История рынков'});
    Object.assign(I18N.en,{navHistory:'Market history'});
    Object.assign(I18N.ru,{modelReady:'Готова к обучению',modelTraining:'Обучается',modelRetraining:'Переобучается',modelActive:'Активна',candidateReady:'Кандидат готов',modelReadyTitle:'Данных достаточно для knife-модели',modelTrainingTitle:'Knife-модель обучается',modelActiveTitle:'Отдельная knife-модель активна',modelReadyCopy:'Пороги данных и time-safe строк пройдены. Запуск остаётся административным действием.',modelTrainingCopy:'Идёт изолированное обучение. До активации карточки показывают наблюдаемую историю.',modelActiveCopy:'Активное поколение прошло SHA-256-проверку. Карточки 7/30 дней по-прежнему показывают историю, а не прогноз.',trainingReady:'Можно запустить обучение',trainingInProgress:'Обучение идёт'});
    Object.assign(I18N.en,{modelReady:'Ready to train',modelTraining:'Training',modelRetraining:'Retraining',modelActive:'Active',candidateReady:'Candidate ready',modelReadyTitle:'Enough data for the first knife model',modelTrainingTitle:'The knife model is training',modelActiveTitle:'The dedicated knife model is active',modelReadyCopy:'The data and time-safe row thresholds are met. Starting training remains an administrative action.',modelTrainingCopy:'Isolated knife-model training is running. Until activation, cards continue to show observed history only.',modelActiveCopy:'The active generation passed SHA-256 verification. The 7/30-day cards still show observed history, not a forecast.',trainingReady:'Training can be started',trainingInProgress:'Training in progress'});
    Object.assign(I18N.ru,{heroLead:'Ищи ножи по типу, покрытию, износу, коллекции или кейсу. Изменения за 7 и 30 дней считаются только по уже состоявшимся продажам.',collection:'Коллекция / кейс',collections:'Коллекции / кейсы',opportunityTitle:'Свежие предложения по ножам',opportunityLead:'Только недавно замеченные CSFloat-лоты, оценённые отдельной knife-моделью. Наличие перепроверяй перед покупкой.',recentlyObserved:'Недавно замечены',opportunityLoading:'Проверяем локальные результаты сканера…',opportunityEmpty:'Свежих выгодных ножей пока нет. Они появятся после активации отдельной модели и включения knife-сканера.',estimatedRoi:'расчётный ROI',observed:'Замечен',openOffer:'Открыть лот',notLiveGuarantee:'Лот мог уже исчезнуть — это не гарантия наличия.'});
    Object.assign(I18N.en,{heroLead:'Search knives by type, finish, wear, collection, or case. The 7- and 30-day changes use completed observed sales only.',collection:'Collection / case',collections:'Collections / cases',opportunityTitle:'Fresh knife opportunities',opportunityLead:'Only recently observed CSFloat listings scored by the dedicated knife model. Recheck availability before buying.',recentlyObserved:'Recently observed',opportunityLoading:'Checking local scanner results…',opportunityEmpty:'There are no recent actionable knife offers yet. They will appear after the dedicated model is active and the knife scanner is enabled.',estimatedRoi:'estimated ROI',observed:'Observed',openOffer:'Open listing',notLiveGuarantee:'The listing may already be gone; availability is not guaranteed.'});
    const $=selector=>document.querySelector(selector);
    const $$=selector=>Array.from(document.querySelectorAll(selector));
    const validLocales=new Set(['ru','en']);
    const cookieValue=name=>document.cookie.split(';').map(v=>v.trim()).find(v=>v.startsWith(name+'='))?.slice(name.length+1)||'';
    function storedLocale(){let cookie='';try{cookie=decodeURIComponent(cookieValue('cs2_locale'));}catch(_){ }let local='';try{local=localStorage.getItem('cs2_locale')||'';}catch(_){ }return validLocales.has(cookie)?cookie:(validLocales.has(local)?local:(validLocales.has(INITIAL_LOCALE)?INITIAL_LOCALE:'ru'));}
    let locale=storedLocale(); let payload=null; let view='cards'; let selectedItem=null;
    const tr=(key,values={})=>(I18N[locale]?.[key]??I18N.ru[key]??key).replace(/\{(\w+)\}/g,(_,name)=>String(values[name]??''));
    const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    const hasNumber=value=>value!==null&&value!==undefined&&value!==''&&Number.isFinite(Number(value));
    const number=value=>hasNumber(value)?new Intl.NumberFormat(locale==='ru'?'ru-RU':'en-US').format(Number(value)):'—';
    const score=value=>hasNumber(value)?new Intl.NumberFormat(locale==='ru'?'ru-RU':'en-US',{maximumFractionDigits:1}).format(Number(value)):'—';
    const money=cents=>hasNumber(cents)?new Intl.NumberFormat(locale==='ru'?'ru-RU':'en-US',{style:'currency',currency:'USD'}).format(Number(cents)/100):tr('noPrice');
    const percent=value=>hasNumber(value)?`${Number(value)>0?'+':''}${Number(value).toFixed(1)}%`:'—';
    const trendClass=value=>!hasNumber(value)?'no-data':Number(value)>.05?'up':Number(value)<-.05?'down':'flat';
    function persistLocale(value){document.cookie=`cs2_locale=${encodeURIComponent(value)}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol==='https:'?'; Secure':''}`;try{localStorage.setItem('cs2_locale',value);}catch(_){}}
    function applyLocale(value,persist=false){locale=validLocales.has(value)?value:'ru';document.documentElement.lang=locale;document.title=tr('documentTitle');$$('[data-i18n]').forEach(node=>node.textContent=tr(node.dataset.i18n));$$('[data-i18n-placeholder]').forEach(node=>node.placeholder=tr(node.dataset.i18nPlaceholder));$('#localeSelect').value=locale;if(persist)persistLocale(locale);if(payload)render(payload);}
    function safeImage(value){try{const url=new URL(String(value||''),location.href);return ['http:','https:'].includes(url.protocol)&&!url.username&&!url.password?url.href:'';}catch(_){return '';}}
    function tags(item){const values=[];if(item.phase)values.push(item.phase);else if((item.phases||[]).length>1)values.push(tr('multiplePhases',{count:item.phases.length}));if(item.wear)values.push(item.wear);const group=[item.collection_name,...(item.crates||[])].find(value=>value&&String(value).trim().toLowerCase()!=='unknown');if(group)values.push(group);return values.slice(0,3).map(value=>`<span class="tag">${esc(value)}</span>`).join('');}
    function card(item){const image=safeImage(item.icon_url);return `<article class="knife-card" tabindex="0" role="button" data-name="${esc(item.market_hash_name)}" aria-label="${esc(tr('openDetails'))}: ${esc(item.market_hash_name)}">${image?`<img class="knife-image" src="${esc(image)}" alt="" loading="lazy" referrerpolicy="no-referrer">`:'<div class="knife-placeholder" aria-hidden="true">◇</div>'}<div><h3>${esc(item.market_hash_name)}</h3><div class="tags">${tags(item)}</div><div class="card-price">${money(item.current_price_cents)}</div><div class="card-stats"><span class="card-stat"><span>${esc(tr('liquidity'))}</span><strong>${score(item.liquidity_score)}</strong></span><span class="card-stat"><span>${esc(tr('trend7'))}</span><strong class="${trendClass(item.trend_7d_percent)}">${percent(item.trend_7d_percent)}</strong></span><span class="card-stat"><span>${esc(tr('trend30'))}</span><strong class="${trendClass(item.trend_30d_percent)}">${percent(item.trend_30d_percent)}</strong></span></div></div></article>`;}
    function tableRow(item){const phase=item.phase||((item.phases||[]).length?tr('multiplePhases',{count:item.phases.length}):'—');return `<tr tabindex="0" data-name="${esc(item.market_hash_name)}"><td class="name-cell"><strong>${esc(item.market_hash_name)}</strong><small>${esc([item.knife_type,item.finish,item.wear].filter(Boolean).join(' · '))}</small></td><td>${esc(phase)}</td><td><strong>${money(item.current_price_cents)}</strong></td><td>${score(item.liquidity_score)}</td><td>${number(item.sales_30d)}</td><td class="${trendClass(item.trend_7d_percent)}">${percent(item.trend_7d_percent)}</td><td class="${trendClass(item.trend_30d_percent)}">${percent(item.trend_30d_percent)}</td></tr>`;}
    function renderResults(data){const mount=$('#results');if(!data.items?.length){const filtered=Number(data.summary?.filtered_count||0);mount.innerHTML=`<div class="empty"><div class="empty-icon">◇</div><h3>${esc(tr(filtered?'emptyFilteredTitle':'emptyTitle'))}</h3><p>${esc(tr(filtered?'emptyFilteredCopy':'emptyCopy'))}</p></div>`;return;}if(view==='table'){mount.innerHTML=`<div class="table-wrap"><table><thead><tr><th>${esc(tr('name'))}</th><th>${esc(tr('phase'))}</th><th>${esc(tr('price'))}</th><th>${esc(tr('liquidity'))}</th><th>${esc(tr('sales30'))}</th><th>${esc(tr('trend7'))}</th><th>${esc(tr('trend30'))}</th></tr></thead><tbody>${data.items.map(tableRow).join('')}</tbody></table></div>`;}else{mount.innerHTML=`<div class="cards">${data.items.map(card).join('')}</div>`;}}
    function selectedValue(name){return $('#filterForm').elements[name]?.value||'';}
    function fillFacet(id,items,name){const node=$(id);if(!node)return;const current=selectedValue(name);node.innerHTML=`<option value="">${esc(tr('all'))}</option>${(items||[]).map(item=>`<option value="${esc(item.value)}">${esc(item.value)} (${number(item.count)})</option>`).join('')}`;node.value=current;}
    function renderOpportunities(value={}){const mount=$('#liveOpportunities');if(!mount)return;const items=Array.isArray(value.items)?value.items:[];if(!items.length){mount.innerHTML=`<div class="opportunity-empty">${esc(tr('opportunityEmpty'))}</div>`;return;}mount.innerHTML=`<div class="opportunity-grid">${items.map(item=>{const link=safeImage(item.csfloat_url);const roi=hasNumber(item.estimated_roi_percent)?`${Number(item.estimated_roi_percent)>0?'+':''}${Number(item.estimated_roi_percent).toFixed(1)}%`:'—';const floatValue=hasNumber(item.float_value)?`float ${Number(item.float_value).toFixed(6)}`:'float —';return `<article class="opportunity-card"><h3>${esc(item.market_hash_name||item.listing_id||'—')}</h3><div class="opportunity-price"><strong>${money(item.buy_price_cents)}</strong><span>${esc(tr('estimatedRoi'))} ${esc(roi)}</span></div><div class="opportunity-meta"><span>${esc(floatValue)}</span><span>${esc(tr('observed'))}: ${esc(item.observed_at||'—')}</span></div>${link?`<a class="opportunity-link" href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(tr('openOffer'))} ↗</a>`:''}<p class="disclaimer">${esc(tr('notLiveGuarantee'))}</p></article>`;}).join('')}</div>`;}
    function renderModelStatus(status={}){const state=status.state||'collecting_data';const stateKeys={unavailable:'modelUnavailable',ready_to_train:'modelReady',training:'modelTraining',retraining:'modelRetraining',active:'modelActive',candidate_ready:'candidateReady'};const active=state==='active'||state==='retraining';const training=state==='training'||state==='retraining';$('#modelState').textContent=tr(stateKeys[state]||'modelCollecting');$('#modelTitle').textContent=tr(active?'modelActiveTitle':training?'modelTrainingTitle':state==='ready_to_train'||state==='candidate_ready'?'modelReadyTitle':'modelTitle');$('#modelCopy').textContent=tr(active?'modelActiveCopy':training?'modelTrainingCopy':state==='ready_to_train'||state==='candidate_ready'?'modelReadyCopy':'modelCopy');$('#trainingStatus').textContent=tr(status.training_action_available?'trainingReady':status.training_active?'trainingInProgress':'noTraining');renderOpportunities(payload?.opportunities);}
    function render(data){payload=data;const summary=data.summary||{};$('#metricTotal').textContent=number(summary.total_knives);$('#metricPriced').textContent=number(summary.history_count);$('#metricSales').textContent=number(summary.sales_30d);$('#metricMedian').textContent=money(summary.median_price_cents);$('#metricLiquidity').textContent=hasNumber(summary.average_liquidity_score)?Number(summary.average_liquidity_score).toFixed(1):'—';renderModelStatus(data.model_status);$('#resultCount').textContent=tr('results',{filtered:number(summary.filtered_count),shown:number(summary.shown_count)});const fallback=Boolean(data.sources?.catalog_fallback_used);$('#sourceDot').classList.toggle('fallback',fallback);$('#sourceText').textContent=tr(fallback?'fallbackSource':'catalogSource');fillFacet('#knifeType',data.facets?.knife_types,'knife_type');fillFacet('#finish',data.facets?.finishes,'finish');fillFacet('#phase',data.facets?.phases,'phase');fillFacet('#wear',data.facets?.wears,'wear');fillFacet('#collection',data.facets?.collections,'collection');renderResults(data);const offset=Number(data.pagination?.offset||0),limit=Number(data.pagination?.limit||100);$('#pageLabel').textContent=tr('page',{page:Math.floor(offset/limit)+1});$('#previousPage').disabled=offset<=0;$('#nextPage').disabled=!data.pagination?.has_more;$('#pagination').hidden=offset<=0&&!data.pagination?.has_more;}
    function paramsFromForm(){const data=new FormData($('#filterForm'));const params=new URLSearchParams();for(const [key,value] of data){if(String(value).trim()!==''&&!(key==='min_liquidity'&&String(value)==='0'))params.set(key,String(value).trim());}params.set('limit','60');const existing=new URLSearchParams(location.search);if(existing.get('offset'))params.set('offset',existing.get('offset'));return params;}
    async function load(resetOffset=false){const error=$('#errorBanner');error.hidden=true;const params=paramsFromForm();if(resetOffset)params.delete('offset');history.replaceState(null,'',`${location.pathname}${params.toString()?`?${params}`:''}`);$('#results').innerHTML=`<div class="loading">${esc(tr('loading'))}</div>`;try{const response=await fetch(`/api/market/knives?${params}`,{credentials:'same-origin',headers:{Accept:'application/json'}});if(response.status===401){location.assign('/login?next=%2Fknives');return;}const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||tr('loadError'));render(data);}catch(err){error.textContent=err.message||tr('loadError');error.hidden=false;$('#results').innerHTML='';}}
    function setInitialFilters(){const params=new URLSearchParams(location.search);for(const element of $('#filterForm').elements){if(!element.name||!params.has(element.name))continue;const value=params.get(element.name);if(element.tagName==='SELECT'&&!Array.from(element.options).some(option=>option.value===value))element.add(new Option(value,value));element.value=value;}}
    function detailBox(label,value){return `<div class="detail-box"><span>${esc(label)}</span><strong>${esc(value??'—')}</strong></div>`;}
    function drawChart(series){const points=(series||[]).filter(row=>hasNumber(row.median_price_cents));if(!points.length){$('#chartShell').innerHTML=`<div class="empty"><p>${esc(tr('chartEmpty'))}</p></div>`;return;}const width=780,height=260,pad={l:58,r:18,t:18,b:34};const values=points.map(row=>Number(row.median_price_cents));let min=Math.min(...values),max=Math.max(...values);if(min===max){min*=.98;max*=1.02;if(min===max)max=min+1;}const x=index=>pad.l+(points.length===1?.5:index/(points.length-1))*(width-pad.l-pad.r);const y=value=>pad.t+(max-value)/(max-min)*(height-pad.t-pad.b);const coords=points.map((row,index)=>[x(index),y(Number(row.median_price_cents))]);const line=coords.map((point,index)=>`${index?'L':'M'}${point[0].toFixed(1)},${point[1].toFixed(1)}`).join(' ');const area=`${line} L${coords.at(-1)[0].toFixed(1)},${height-pad.b} L${coords[0][0].toFixed(1)},${height-pad.b} Z`;const grids=[0,.5,1].map(ratio=>{const yy=pad.t+ratio*(height-pad.t-pad.b),price=max-ratio*(max-min);return `<line class="chart-grid" x1="${pad.l}" x2="${width-pad.r}" y1="${yy}" y2="${yy}"/><text class="chart-label" x="4" y="${yy+4}">${esc(money(price))}</text>`;}).join('');$('#chartShell').innerHTML=`<div class="chart-title"><strong>${esc(tr('chartTitle'))}</strong><span>${esc(tr('chartRange',{count:points.length}))}</span></div><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(tr('chartTitle'))}"><defs><linearGradient id="knifeGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--accent-2)"/><stop offset="1" stop-color="var(--accent-2)" stop-opacity="0"/></linearGradient></defs>${grids}<path class="chart-area" d="${area}"/><path class="chart-path" d="${line}"/><text class="chart-label" x="${pad.l}" y="${height-8}">${esc(points[0].day)}</text><text class="chart-label" text-anchor="end" x="${width-pad.r}" y="${height-8}">${esc(points.at(-1).day)}</text></svg>`;}
    async function openDetail(name){selectedItem=(payload?.items||[]).find(item=>item.market_hash_name===name);if(!selectedItem)return;$('#detailName').textContent=selectedItem.market_hash_name;$('#detailSubtitle').textContent=[selectedItem.knife_type,selectedItem.finish,selectedItem.wear].filter(Boolean).join(' · ')||'—';const floats=selectedItem.min_float!=null||selectedItem.max_float!=null?`${selectedItem.min_float??'—'} – ${selectedItem.max_float??'—'}`:'—';$('#detailGrid').innerHTML=detailBox(tr('price'),money(selectedItem.current_price_cents))+detailBox(tr('liquidity'),hasNumber(selectedItem.liquidity_score)?Number(selectedItem.liquidity_score).toFixed(1):'—')+detailBox(tr('sales30'),number(selectedItem.sales_30d))+detailBox(tr('asOf'),selectedItem.as_of_day||'—')+detailBox(tr('historyDays'),number(selectedItem.history_days))+detailBox(tr('assetsObserved'),number(selectedItem.unique_asset_count))+detailBox(tr('floatRange'),floats)+detailBox(tr('phases'),(selectedItem.phases||[]).join(', ')||'—')+detailBox(tr('collections'),(selectedItem.collections||[]).join(', ')||'—')+detailBox(tr('variants'),number(selectedItem.variant_count));$('#chartShell').innerHTML=`<div class="loading">${esc(tr('chartLoading'))}</div>`;const dialog=$('#detailDialog');if(typeof dialog.showModal==='function')dialog.showModal();else dialog.setAttribute('open','');try{const response=await fetch(`/api/market/skin-series?name=${encodeURIComponent(name)}&days=365`,{credentials:'same-origin',headers:{Accept:'application/json'}});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error();drawChart(data.series);}catch(_){$('#chartShell').innerHTML=`<div class="empty"><p>${esc(tr('chartError'))}</p></div>`;}}
    function closeDialog(){const dialog=$('#detailDialog');if(typeof dialog.close==='function')dialog.close();else dialog.removeAttribute('open');}
    $('#filterForm').addEventListener('submit',event=>{event.preventDefault();load(true);});$('#resetFilters').addEventListener('click',()=>{$('#filterForm').reset();history.replaceState(null,'',location.pathname);load(true);});$('#localeSelect').addEventListener('change',event=>applyLocale(event.target.value,true));$('#themeButton').addEventListener('click',()=>{document.body.classList.toggle('light');try{localStorage.setItem('cs2_theme',document.body.classList.contains('light')?'light':'dark');}catch(_){}});$$('[data-view]').forEach(button=>button.addEventListener('click',()=>{view=button.dataset.view;$$('[data-view]').forEach(node=>node.classList.toggle('active',node===button));renderResults(payload||{items:[],summary:{}});}));$('#results').addEventListener('click',event=>{const row=event.target.closest('[data-name]');if(row)openDetail(row.dataset.name);});$('#results').addEventListener('keydown',event=>{if((event.key==='Enter'||event.key===' ')&&event.target.closest('[data-name]')){event.preventDefault();openDetail(event.target.closest('[data-name]').dataset.name);}});$('#previousPage').addEventListener('click',()=>{const params=new URLSearchParams(location.search),limit=Number(payload?.pagination?.limit||60),offset=Math.max(0,Number(payload?.pagination?.offset||0)-limit);if(offset)params.set('offset',String(offset));else params.delete('offset');history.replaceState(null,'',`${location.pathname}?${params}`);load();});$('#nextPage').addEventListener('click',()=>{const params=new URLSearchParams(location.search),offset=Number(payload?.pagination?.offset||0)+Number(payload?.pagination?.limit||60);params.set('offset',String(offset));history.replaceState(null,'',`${location.pathname}?${params}`);load();});$('#closeDialog').addEventListener('click',closeDialog);$('#detailDialog').addEventListener('click',event=>{if(event.target===$('#detailDialog'))closeDialog();});document.addEventListener('keydown',event=>{if(event.key==='Escape'&&$('#detailDialog').hasAttribute('open'))closeDialog();});
    try{if(localStorage.getItem('cs2_theme')==='light')document.body.classList.add('light');}catch(_){ }setInitialFilters();applyLocale(locale);load();
  })();
  </script>
</body>
</html>"""


def render_knives_page(locale: str = "ru") -> str:
    normalized = locale if locale in {"ru", "en"} else "ru"
    return KNIVES_HTML.replace("__LOCALE__", normalized)


__all__ = [
    "KNIVES_HTML",
    "build_knives_payload",
    "is_canonical_knife_name",
    "parse_knife_name",
    "render_knives_page",
]
