"""Fast, read-only catalogue browser for weapon skins, gloves and stickers.

The public catalogue contract deliberately separates two kinds of coverage:

* weapon skins and gloves are enumerated from ``cs2_item_catalog`` and include
  market variants that have never appeared in this project's sales history;
* stickers are an *observed* catalogue assembled from ``sale_stickers``.  It is
  not advertised as the complete Valve sticker schema.

No request in this module scans or aggregates the raw ``sales`` table.  Trend
data is read only for the current (at most 100 item) page from the materialized
``trend_daily_market`` and ``trend_skin_catalog`` tables.  Sticker identities
come from a covering index and are cached briefly in process; placement counts
are index lookups for only the visible page.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
DEFAULT_LIMIT = 48
MAX_LIMIT = 100
MAX_OFFSET = 100_000
MAX_FILTER_TEXT = 180
STICKER_IDENTITY_CACHE_SECONDS = 300.0
STICKER_IDENTITY_CACHE_PATHS = 8

_TAB_ALIASES = {
    "skin": "skins",
    "skins": "skins",
    "weapon_skin": "skins",
    "weapon_skins": "skins",
    "glove": "gloves",
    "gloves": "gloves",
    "sticker": "stickers",
    "stickers": "stickers",
}
_CATALOG_KIND = {"skins": "weapon_skin", "gloves": "glove"}
_STICKER_TYPE_RE = re.compile(
    r"\((Holo|Foil|Gold|Glitter|Lenticular|Paper)\)(?:\s*\|[^|]+)?\s*$",
    re.IGNORECASE,
)
_SEARCH_ALIAS_REPLACEMENTS = (
    (re.compile(r"(?<!\w)мак(?=(?:\W|_|10|$))", re.IGNORECASE), "mac "),
    (re.compile(r"(?<!\w)ак(?=(?:\W|_|47|$))", re.IGNORECASE), "ak "),
)
_SEARCH_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_COMPACT_WEAPON_TOKENS = {"ak47": ("ak", "47"), "mac10": ("mac", "10")}
_SEARCH_WEAPON_ALIASES = (
    ("AK-47", ("ak", "47")),
    ("MAC-10", ("mac", "10")),
)
MAX_SEARCH_TOKENS = 16
_STICKER_CACHE_LOCK = threading.Lock()
_STICKER_CACHE: dict[str, tuple[float, tuple[tuple[int | None, str], ...]]] = {}


def _bounded_int(
    value: Any, *, name: str, default: int, minimum: int, maximum: int
) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")  # noqa: TRY004
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _text(value: Any, *, name: str) -> str:
    result = str(value or "").strip()
    if len(result) > MAX_FILTER_TEXT:
        raise ValueError(f"{name} must contain at most {MAX_FILTER_TEXT} characters")
    if any(ord(character) < 32 for character in result):
        raise ValueError(f"{name} contains control characters")
    return result


def _optional_text(value: Any) -> str | None:
    result = str(value or "").strip()
    return result or None


def _optional_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _safe_image_url(value: Any) -> str | None:
    text = _optional_text(value)
    if not text:
        return None
    parsed = urlsplit(text)
    if (
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not (parsed.username or parsed.password)
    ):
        return text
    return None


def _json_list(value: Any) -> list[str]:
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
    for item in parsed:
        text = _optional_text(item)
        folded = text.casefold() if text else ""
        if text and folded not in seen:
            seen.add(folded)
            result.append(text)
    return result


def _json_array_contains(value: Any, needle: Any) -> int:
    expected = _optional_text(needle)
    if not expected:
        return 0
    expected_folded = expected.casefold()
    return int(any(item.casefold() == expected_folded for item in _json_list(value)))


def _search_tokens(value: str) -> list[str]:
    """Return separator-insensitive tokens with small Russian weapon aliases.

    Market names use forms such as ``AK-47`` and ``MAC-10`` while people often
    type ``AK 47``, ``mac/10``, ``ак 47`` or ``мак 10``.  Matching every token
    independently keeps the raw catalogue scan bounded (~20k rows) and avoids
    a second normalized-name table or a scan of the much larger sales table.
    """

    normalized = value.casefold()
    for pattern, replacement in _SEARCH_ALIAS_REPLACEMENTS:
        normalized = pattern.sub(replacement, normalized)
    tokens: list[str] = []
    for token in _SEARCH_TOKEN_RE.findall(normalized):
        folded = token.casefold()
        expanded = _COMPACT_WEAPON_TOKENS.get(folded, (folded,))
        for candidate in expanded:
            if candidate and candidate not in tokens:
                tokens.append(candidate)
        if len(tokens) >= MAX_SEARCH_TOKENS:
            break
    # A bare weapon-family query should not accidentally match words such as
    # "Snakebite" merely because they contain the letters "ak".
    if "ak" in tokens and "47" not in tokens:
        tokens.insert(min(tokens.index("ak") + 1, len(tokens)), "47")
    if "mac" in tokens and "10" not in tokens:
        tokens.insert(min(tokens.index("mac") + 1, len(tokens)), "10")
    return tokens


def _search_weapon_alias(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Resolve canonical weapon aliases and leave finish/wear tokens behind."""

    for weapon_name, required in _SEARCH_WEAPON_ALIASES:
        if all(token in tokens for token in required):
            remaining = list(tokens)
            for token in required:
                remaining.remove(token)
            return weapon_name, remaining
    return None, tokens


@contextmanager
def _read_connection(path: str | Path) -> Iterator[sqlite3.Connection | None]:
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        yield None
        return
    connection = sqlite3.connect(
        f"{database.as_uri()}?mode=ro", uri=True, timeout=5, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.create_function(
        "catalog_json_contains", 2, _json_array_contains, deterministic=True
    )
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


def _select_expression(columns: set[str], name: str) -> str:
    return name if name in columns else f"NULL AS {name}"


def _catalog_facets(
    connection: sqlite3.Connection, item_kind: str, columns: set[str]
) -> dict[str, list[dict[str, Any]]]:
    selected = [
        _select_expression(columns, "weapon_name"),
        _select_expression(columns, "wear_name"),
        _select_expression(columns, "collection_names_json"),
        _select_expression(columns, "crate_names_json"),
    ]
    counts: dict[str, dict[str, int]] = {
        "weapons": {},
        "wears": {},
        "collections": {},
        "cases": {},
    }
    for row in connection.execute(
        f"SELECT {', '.join(selected)} FROM cs2_item_catalog WHERE item_kind=?",
        (item_kind,),
    ):
        weapon = _optional_text(row["weapon_name"])
        wear = _optional_text(row["wear_name"])
        if weapon:
            counts["weapons"][weapon] = counts["weapons"].get(weapon, 0) + 1
        if wear:
            counts["wears"][wear] = counts["wears"].get(wear, 0) + 1
        for collection in _json_list(row["collection_names_json"]):
            counts["collections"][collection] = (
                counts["collections"].get(collection, 0) + 1
            )
        for case in _json_list(row["crate_names_json"]):
            counts["cases"][case] = counts["cases"].get(case, 0) + 1
    return {
        key: [
            {"value": value, "count": count}
            for value, count in sorted(
                values.items(), key=lambda pair: (-pair[1], pair[0].casefold())
            )
        ]
        for key, values in counts.items()
    }


def _catalog_query(
    connection: sqlite3.Connection,
    *,
    item_kind: str,
    search: str,
    weapon: str,
    wear: str,
    collection: str,
    case: str,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int, dict[str, list[dict[str, Any]]], dict[str, Any]]:
    columns = _table_columns(connection, "cs2_item_catalog")
    required = {"market_hash_name", "item_kind"}
    if not required.issubset(columns):
        return (
            [],
            0,
            {},
            {
                "state": "unavailable",
                "reason": "table_missing" if not columns else "contract_incomplete",
            },
        )

    where = ["item_kind=?"]
    parameters: list[Any] = [item_kind]
    if search:
        tokens = _search_tokens(search)
        search_weapon, residual_tokens = _search_weapon_alias(tokens)
        if search_weapon and "weapon_name" in columns:
            where.append("weapon_name=? COLLATE NOCASE")
            parameters.append(search_weapon)
            tokens = residual_tokens
        if tokens:
            for token in tokens:
                where.append("market_hash_name LIKE ? ESCAPE '\\' COLLATE NOCASE")
                escaped = (
                    token.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                parameters.append(f"%{escaped}%")
        elif not search_weapon:
            # Preserve useful literal searches made entirely from punctuation.
            where.append("market_hash_name LIKE ? ESCAPE '\\' COLLATE NOCASE")
            escaped = (
                search.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            parameters.append(f"%{escaped}%")
    if weapon:
        if "weapon_name" not in columns:
            where.append("0")
        else:
            where.append("weapon_name=? COLLATE NOCASE")
            parameters.append(weapon)
    if wear:
        if "wear_name" not in columns:
            where.append("0")
        else:
            where.append("wear_name=? COLLATE NOCASE")
            parameters.append(wear)
    if collection:
        if "collection_names_json" not in columns:
            where.append("0")
        else:
            where.append("catalog_json_contains(collection_names_json, ?)=1")
            parameters.append(collection)
    if case:
        if "crate_names_json" not in columns:
            where.append("0")
        else:
            where.append("catalog_json_contains(crate_names_json, ?)=1")
            parameters.append(case)

    where_sql = " AND ".join(where)
    total = int(
        connection.execute(
            f"SELECT COUNT(*) FROM cs2_item_catalog WHERE {where_sql}", parameters
        ).fetchone()[0]
    )
    supported = (
        "market_hash_name",
        "item_kind",
        "base_name",
        "weapon_name",
        "paint_name",
        "phase_name",
        "wear_name",
        "min_float",
        "max_float",
        "stattrak",
        "souvenir",
        "collection_names_json",
        "crate_names_json",
        "image_url",
        "source_id",
        "source_updated_at",
        "catalog_build_id",
    )
    selected = [_select_expression(columns, name) for name in supported]
    rows = connection.execute(
        f"SELECT {', '.join(selected)} FROM cs2_item_catalog "
        f"WHERE {where_sql} ORDER BY market_hash_name COLLATE NOCASE LIMIT ? OFFSET ?",
        (*parameters, limit, offset),
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "market_hash_name": str(row["market_hash_name"]),
                "item_kind": str(row["item_kind"]),
                "base_name": _optional_text(row["base_name"]),
                "weapon": _optional_text(row["weapon_name"]),
                "finish": _optional_text(row["paint_name"]),
                "phase": _optional_text(row["phase_name"]),
                "wear": _optional_text(row["wear_name"]),
                "min_float": _optional_float(row["min_float"]),
                "max_float": _optional_float(row["max_float"]),
                "stattrak": bool(row["stattrak"]),
                "souvenir": bool(row["souvenir"]),
                "collections": _json_list(row["collection_names_json"]),
                "cases": _json_list(row["crate_names_json"]),
                "icon_url": _safe_image_url(row["image_url"]),
                "source_id": _optional_text(row["source_id"]),
                "source_updated_at": _optional_text(row["source_updated_at"]),
                "catalog_build_id": _optional_text(row["catalog_build_id"]),
            }
        )
    facets = _catalog_facets(connection, item_kind, columns)
    return (
        items,
        total,
        facets,
        {
            "state": "ready",
            "reason": None,
            "table": "cs2_item_catalog",
            "authoritative_enumeration": True,
        },
    )


def _percent(current: Any, previous: Any) -> float | None:
    current_value = _optional_float(current)
    previous_value = _optional_float(previous)
    if current_value is None or previous_value is None or previous_value <= 0:
        return None
    return round((current_value / previous_value - 1.0) * 100.0, 4)


def _comparison_price(
    rows: list[dict[str, Any]], as_of: date, days: int
) -> float | None:
    target = as_of - timedelta(days=days)
    minimum = target - timedelta(days=6)
    candidates = [
        row
        for row in rows
        if minimum <= date.fromisoformat(str(row["day"])) <= target
    ]
    return _optional_float(candidates[-1]["median_price_cents"]) if candidates else None


def _page_trends(
    path: str | Path, names: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not names:
        return {}, {"state": "ready", "reason": None, "as_of_day": None}
    with _read_connection(path) as connection:
        if connection is None:
            return {}, {"state": "unavailable", "reason": "database_missing"}
        daily_columns = _table_columns(connection, "trend_daily_market")
        required = {"market_hash_name", "day", "sales_count", "median_price_cents"}
        if not required.issubset(daily_columns):
            return (
                {},
                {
                    "state": "unavailable",
                    "reason": "table_missing"
                    if not daily_columns
                    else "contract_incomplete",
                },
            )
        latest_row = connection.execute(
            "SELECT day FROM trend_daily_market ORDER BY day DESC LIMIT 1"
        ).fetchone()
        if not latest_row:
            return {}, {"state": "ready", "reason": "no_daily_rows", "as_of_day": None}
        as_of_day = str(latest_row[0])
        try:
            as_of = date.fromisoformat(as_of_day)
        except ValueError:
            return {}, {"state": "unavailable", "reason": "invalid_day"}

        placeholders = ",".join("?" for _ in names)
        cutoff = (as_of - timedelta(days=37)).isoformat()
        daily_rows: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
        for row in connection.execute(
            "SELECT market_hash_name, day, sales_count, median_price_cents "
            "FROM trend_daily_market "
            f"WHERE market_hash_name IN ({placeholders}) AND day>=? "
            "ORDER BY market_hash_name, day",
            (*names, cutoff),
        ):
            daily_rows[str(row["market_hash_name"])].append(dict(row))

        catalog_rows: dict[str, dict[str, Any]] = {}
        trend_catalog_columns = _table_columns(connection, "trend_skin_catalog")
        catalog_supported = [
            name
            for name in (
                "market_hash_name",
                "observed_sales_count",
                "unique_asset_count",
                "latest_reference_quantity",
                "latest_reference_at",
                "icon_url",
            )
            if name in trend_catalog_columns
        ]
        if "market_hash_name" in catalog_supported:
            for row in connection.execute(
                f"SELECT {', '.join(catalog_supported)} FROM trend_skin_catalog "
                f"WHERE market_hash_name IN ({placeholders})",
                names,
            ):
                catalog_rows[str(row["market_hash_name"])] = dict(row)

        result: dict[str, dict[str, Any]] = {}
        for name in names:
            rows = daily_rows.get(name) or []
            extra = catalog_rows.get(name, {})
            if not rows and not extra:
                continue
            latest = rows[-1] if rows else None

            latest_day = date.fromisoformat(str(latest["day"])) if latest else None
            last_price = (
                _optional_float(latest["median_price_cents"]) if latest else None
            )
            current_price = (
                last_price
                if latest_day is not None and latest_day >= as_of - timedelta(days=6)
                else None
            )
            price_7d = _comparison_price(rows, as_of, 7)
            price_30d = _comparison_price(rows, as_of, 30)
            sales_30d = sum(
                max(0, _optional_int(row["sales_count"]) or 0)
                for row in rows
                if date.fromisoformat(str(row["day"])) >= as_of - timedelta(days=29)
            )
            result[name] = {
                "observed": bool(rows or extra),
                "current_price_cents": current_price,
                "last_observed_price_cents": last_price,
                "last_observed_day": latest_day.isoformat() if latest_day else None,
                "price_is_stale": bool(latest_day and current_price is None),
                "trend_7d_percent": _percent(current_price, price_7d),
                "trend_30d_percent": _percent(current_price, price_30d),
                "sales_30d": sales_30d,
                "observed_sales_count": _optional_int(
                    extra.get("observed_sales_count")
                ),
                "unique_asset_count": _optional_int(extra.get("unique_asset_count")),
                "latest_reference_quantity": _optional_int(
                    extra.get("latest_reference_quantity")
                ),
                "latest_reference_at": _optional_text(extra.get("latest_reference_at")),
                "trend_icon_url": _safe_image_url(extra.get("icon_url")),
            }
        return (
            result,
            {
                "state": "ready",
                "reason": None,
                "table": "trend_daily_market",
                "as_of_day": as_of_day,
                "queried_market_names": len(names),
            },
        )


def _sticker_type(name: str) -> str | None:
    match = _STICKER_TYPE_RE.search(name)
    return match.group(1).title() if match else None


def _sticker_identities(
    path: str | Path, connection: sqlite3.Connection
) -> tuple[tuple[int | None, str], ...]:
    cache_key = str(Path(path).expanduser().resolve())
    now = time.monotonic()
    with _STICKER_CACHE_LOCK:
        cached = _STICKER_CACHE.get(cache_key)
        if cached and now - cached[0] < STICKER_IDENTITY_CACHE_SECONDS:
            return cached[1]

    indexes = {
        str(row[1]) for row in connection.execute("PRAGMA index_list(sale_stickers)")
    }
    index_hint = (
        " INDEXED BY idx_sale_stickers_sticker_id_name"
        if "idx_sale_stickers_sticker_id_name" in indexes
        else ""
    )
    rows = connection.execute(
        "SELECT sticker_id, name FROM sale_stickers"
        f"{index_hint} WHERE name IS NOT NULL AND TRIM(name)<>'' "
        "GROUP BY sticker_id, name ORDER BY name COLLATE NOCASE, sticker_id"
    )
    identities = tuple(
        (_optional_int(row["sticker_id"]), str(row["name"]).strip()) for row in rows
    )
    with _STICKER_CACHE_LOCK:
        if len(_STICKER_CACHE) >= STICKER_IDENTITY_CACHE_PATHS:
            oldest = min(_STICKER_CACHE, key=lambda key: _STICKER_CACHE[key][0])
            _STICKER_CACHE.pop(oldest, None)
        _STICKER_CACHE[cache_key] = (now, identities)
    return identities


def _sticker_page_details(
    connection: sqlite3.Connection,
    identities: list[tuple[int | None, str]],
    columns: set[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for sticker_id, name in identities:
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM sale_stickers "
                "WHERE sticker_id IS ? AND name=?",
                (sticker_id, name),
            ).fetchone()[0]
        )
        optional = [
            column
            for column in (
                "reference_price_cents",
                "reference_quantity",
                "icon_url",
            )
            if column in columns
        ]
        representative: Mapping[str, Any] = {}
        if optional:
            row = connection.execute(
                f"SELECT {', '.join(optional)} FROM sale_stickers "
                "WHERE sticker_id IS ? AND name=? LIMIT 1",
                (sticker_id, name),
            ).fetchone()
            representative = dict(row) if row else {}
        result.append(
            {
                "sticker_id": sticker_id,
                "name": name,
                "sticker_type": _sticker_type(name),
                "observed_placements": count,
                "reference_price_cents": _optional_int(
                    representative.get("reference_price_cents")
                ),
                "reference_quantity": _optional_int(
                    representative.get("reference_quantity")
                ),
                "icon_url": _safe_image_url(representative.get("icon_url")),
                "coverage": "observed_on_items",
            }
        )
    return result


def _stickers_query(
    path: str | Path,
    *,
    search: str,
    sticker_type: str,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int, dict[str, list[dict[str, Any]]], dict[str, Any]]:
    with _read_connection(path) as connection:
        if connection is None:
            return [], 0, {}, {"state": "unavailable", "reason": "database_missing"}
        columns = _table_columns(connection, "sale_stickers")
        if not {"sticker_id", "name"}.issubset(columns):
            return (
                [],
                0,
                {},
                {
                    "state": "unavailable",
                    "reason": "table_missing" if not columns else "contract_incomplete",
                },
            )
        identities = _sticker_identities(path, connection)
        search_folded = search.casefold()
        type_folded = sticker_type.casefold()
        selected = [
            identity
            for identity in identities
            if (not search_folded or search_folded in identity[1].casefold())
            and (
                not type_folded
                or (_sticker_type(identity[1]) or "").casefold() == type_folded
            )
        ]
        page = selected[offset : offset + limit]
        type_counts: dict[str, int] = {}
        for _, name in identities:
            value = _sticker_type(name)
            if value:
                type_counts[value] = type_counts.get(value, 0) + 1
        facets = {
            "sticker_types": [
                {"value": value, "count": count}
                for value, count in sorted(
                    type_counts.items(), key=lambda pair: (-pair[1], pair[0].casefold())
                )
            ]
        }
        return (
            _sticker_page_details(connection, page, columns),
            len(selected),
            facets,
            {
                "state": "ready",
                "reason": None,
                "table": "sale_stickers",
                "authoritative_enumeration": False,
                "coverage": "observed_on_items",
                "identity_count": len(identities),
                "cache_seconds": STICKER_IDENTITY_CACHE_SECONDS,
            },
        )


def build_catalog_payload(
    main_db_path: str | Path,
    trend_db_path: str | Path,
    tab: str = "skins",
    search: Any = "",
    filters: Mapping[str, Any] | None = None,
    pagination: Mapping[str, Any] | None = None,
    *,
    kind: str | None = None,
    weapon: Any = None,
    wear: Any = None,
    collection: Any = None,
    case: Any = None,
    sticker_type: Any = None,
    limit: Any = None,
    offset: Any = None,
) -> dict[str, Any]:
    """Build one JSON-safe page for ``GET /api/catalog``.

    ``filters`` and ``pagination`` are convenient HTTP-adapter mappings.  The
    explicit keyword arguments are equivalent and take precedence when set.
    ``kind`` is an alias for ``tab``.  Valid tabs are ``skins``, ``gloves`` and
    ``stickers``; the page links to the richer existing ``/knives`` workspace.
    """

    requested_tab = str(kind if kind is not None else tab or "skins").strip().lower()
    normalized_tab = _TAB_ALIASES.get(requested_tab)
    if normalized_tab is None:
        raise ValueError("tab must be skins, gloves, or stickers")
    filter_values = dict(filters or {})
    page_values = dict(pagination or {})

    def choice(explicit: Any, key: str, default: Any = "") -> Any:
        return explicit if explicit is not None else filter_values.get(key, default)

    search_value = _text(
        search if search not in (None, "") else filter_values.get("search", ""),
        name="search",
    )
    weapon_value = _text(choice(weapon, "weapon"), name="weapon")
    wear_value = _text(choice(wear, "wear"), name="wear")
    collection_value = _text(choice(collection, "collection"), name="collection")
    case_value = _text(choice(case, "case"), name="case")
    sticker_type_value = _text(
        choice(sticker_type, "sticker_type"), name="sticker_type"
    )
    limit_value = _bounded_int(
        limit if limit is not None else page_values.get("limit"),
        name="limit",
        default=DEFAULT_LIMIT,
        minimum=1,
        maximum=MAX_LIMIT,
    )
    offset_value = _bounded_int(
        offset if offset is not None else page_values.get("offset"),
        name="offset",
        default=0,
        minimum=0,
        maximum=MAX_OFFSET,
    )

    if normalized_tab == "stickers":
        items, total, facets, catalog_status = _stickers_query(
            main_db_path,
            search=search_value,
            sticker_type=sticker_type_value,
            limit=limit_value,
            offset=offset_value,
        )
        trend_status = {
            "state": "not_applicable",
            "reason": "sticker_daily_market_not_materialized",
        }
        coverage = {
            "scope": "observed_on_items",
            "authoritative": False,
            "includes_unobserved": False,
            "disclaimer_ru": "Только наклейки, замеченные на предметах в локальной базе; это не полный список всех наклеек CS2.",
            "disclaimer_en": "Only stickers observed on items in the local database; this is not the complete CS2 sticker schema.",
        }
    else:
        with _read_connection(main_db_path) as connection:
            if connection is None:
                items, total, facets, catalog_status = (
                    [],
                    0,
                    {},
                    {
                        "state": "unavailable",
                        "reason": "database_missing",
                    },
                )
            else:
                items, total, facets, catalog_status = _catalog_query(
                    connection,
                    item_kind=_CATALOG_KIND[normalized_tab],
                    search=search_value,
                    weapon=weapon_value,
                    wear=wear_value,
                    collection=collection_value,
                    case=case_value,
                    limit=limit_value,
                    offset=offset_value,
                )
        trends, trend_status = _page_trends(
            trend_db_path, [str(item["market_hash_name"]) for item in items]
        )
        for item in items:
            trend = trends.get(str(item["market_hash_name"]))
            if trend:
                item.update(trend)
                if not item.get("icon_url"):
                    item["icon_url"] = trend.get("trend_icon_url")
            else:
                item.update(
                    {
                        "observed": False,
                        "current_price_cents": None,
                        "last_observed_price_cents": None,
                        "last_observed_day": None,
                        "price_is_stale": False,
                        "trend_7d_percent": None,
                        "trend_30d_percent": None,
                        "sales_30d": 0,
                        "observed_sales_count": None,
                        "unique_asset_count": None,
                        "latest_reference_quantity": None,
                        "latest_reference_at": None,
                    }
                )
            item.pop("trend_icon_url", None)
        coverage = {
            "scope": "authoritative_local_catalog_snapshot",
            "authoritative": catalog_status.get("state") == "ready",
            "includes_unobserved": True,
            "disclaimer_ru": "Все варианты из опубликованного локального снимка каталога, включая ещё не встречавшиеся в продажах проекта.",
            "disclaimer_en": "Every variant in the published local catalogue snapshot, including variants not yet seen in project sales.",
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready" if catalog_status.get("state") == "ready" else "unavailable",
        "tab": normalized_tab,
        "items": items,
        "summary": {
            "filtered_count": total,
            "shown_count": len(items),
            "observed_count_on_page": sum(
                1
                for item in items
                if item.get("observed") or item.get("observed_placements")
            ),
            "as_of_day": trend_status.get("as_of_day"),
        },
        "facets": facets,
        "filters": {
            "search": search_value,
            "weapon": weapon_value,
            "wear": wear_value,
            "collection": collection_value,
            "case": case_value,
            "sticker_type": sticker_type_value,
        },
        "pagination": {
            "limit": limit_value,
            "offset": offset_value,
            "total": total,
            "has_more": offset_value + len(items) < total,
        },
        "coverage": coverage,
        "sources": {"catalog": catalog_status, "trend": trend_status},
        "links": {"knives": "/knives", "api": "/api/catalog"},
    }


CATALOG_HTML = r"""<!doctype html>
<html lang="__LOCALE__">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>Каталог · CS2 Market Lab</title>
  <style>
    :root{--bg:#080b12;--panel:#111722;--panel2:#171f2c;--line:#293549;--text:#f4f7fc;--muted:#99a8bd;--accent:#7960ff;--good:#45d69e;--warn:#efb85b;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(800px 450px at 0 0,rgba(121,96,255,.19),transparent 65%),var(--bg);color:var(--text)}button,input,select{font:inherit}a{color:inherit}.wrap{width:min(1400px,calc(100% - 32px));margin:auto}header{position:sticky;top:0;z-index:10;border-bottom:1px solid var(--line);background:rgba(8,11,18,.88);backdrop-filter:blur(16px)}.top{min-height:68px;display:flex;align-items:center;gap:20px}.brand{text-decoration:none;font-weight:850}.nav{display:flex;gap:6px;overflow:auto}.nav a,.tab{padding:9px 12px;border-radius:10px;color:var(--muted);text-decoration:none;font-size:12px;font-weight:750}.nav a:hover,.nav a.active,.tab.active{background:rgba(121,96,255,.15);color:var(--text)}.locale{margin-left:auto;padding:7px;border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--text)}main{padding:32px 0 70px}.hero{padding:26px;border:1px solid var(--line);border-radius:23px;background:linear-gradient(145deg,rgba(121,96,255,.1),var(--panel))}.eyebrow{color:var(--good);font-size:11px;font-weight:850;text-transform:uppercase;letter-spacing:.12em}.hero h1{margin:9px 0 8px;font-size:clamp(30px,5vw,52px);letter-spacing:-.045em}.hero p{max-width:850px;margin:0;color:var(--muted);line-height:1.65}.tabs{display:flex;gap:7px;margin:18px 0;overflow:auto}.tab{border:1px solid var(--line);background:var(--panel);cursor:pointer;white-space:nowrap}.panel{overflow:hidden;border:1px solid var(--line);border-radius:20px;background:var(--panel)}.filters{display:grid;grid-template-columns:2fr repeat(4,minmax(130px,1fr)) auto;gap:9px;padding:17px;border-bottom:1px solid var(--line)}.field{display:grid;gap:5px}.field span{color:var(--muted);font-size:10px}.field input,.field select,.apply{min-height:41px;border:1px solid var(--line);border-radius:10px;background:var(--panel2);color:var(--text);padding:0 11px;min-width:0}.apply{align-self:end;padding:0 16px;background:var(--accent);border-color:var(--accent);font-weight:800;cursor:pointer}.truth{padding:12px 17px;border-bottom:1px solid var(--line);color:var(--muted);font-size:11px}.truth strong{color:var(--warn)}.meta{display:flex;justify-content:space-between;gap:12px;padding:13px 17px;color:var(--muted);font-size:11px}.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:11px;padding:0 17px 17px}.card{display:grid;grid-template-columns:96px minmax(0,1fr);gap:13px;min-height:156px;padding:13px;border:1px solid var(--line);border-radius:15px;background:var(--panel2)}.art{width:96px;height:88px;border-radius:12px;background:radial-gradient(circle,rgba(121,96,255,.18),transparent 65%),var(--bg);object-fit:contain}.placeholder{display:grid;place-items:center;color:var(--muted);font-size:25px}.card h2{margin:0;font-size:13px;line-height:1.4}.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}.tag{padding:3px 6px;border:1px solid var(--line);border-radius:6px;color:var(--muted);font-size:9px}.price{margin-top:10px;font-size:19px;font-weight:850}.stats{display:flex;gap:10px;margin-top:6px;color:var(--muted);font-size:10px}.pagination{display:flex;justify-content:center;align-items:center;gap:10px;padding:0 17px 18px}.pagination button{padding:8px 13px;border:1px solid var(--line);border-radius:9px;background:var(--panel2);color:var(--text);cursor:pointer}.pagination button:disabled{opacity:.4}.empty,.loading,.error{margin:0 17px 17px;padding:36px;border:1px dashed var(--line);border-radius:14px;text-align:center;color:var(--muted)}.error{border-color:#7a3643;color:#ff8392}@media(max-width:1080px){.filters{grid-template-columns:repeat(3,1fr)}.cards{grid-template-columns:repeat(2,1fr)}}@media(max-width:700px){.wrap{width:min(100% - 18px,1400px)}.top{flex-wrap:wrap;padding:10px 0}.nav{order:3;width:100%}.filters{grid-template-columns:1fr 1fr}.field:first-child{grid-column:1/-1}.cards{grid-template-columns:1fr;padding:0 11px 11px}.meta,.truth{padding-left:11px;padding-right:11px}}@media(max-width:430px){.filters{grid-template-columns:1fr}.field:first-child{grid-column:auto}.card{grid-template-columns:76px minmax(0,1fr)}.art{width:76px;height:70px}}
  </style>
  <style>
    .field[hidden]{display:none!important}.card{cursor:pointer;transition:transform .15s ease,border-color .15s ease,box-shadow .15s ease}.card:hover,.card:focus-visible{transform:translateY(-2px);border-color:color-mix(in srgb,var(--accent) 62%,var(--line));box-shadow:0 15px 34px rgba(0,0,0,.2);outline:none}.card:focus-visible{box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 35%,transparent),0 15px 34px rgba(0,0,0,.2)}
    dialog{width:min(900px,calc(100% - 24px));max-height:calc(100vh - 30px);padding:0;border:1px solid var(--line);border-radius:21px;background:var(--panel);color:var(--text);box-shadow:0 30px 100px rgba(0,0,0,.58)}dialog::backdrop{background:rgba(2,5,10,.76);backdrop-filter:blur(5px)}.detail-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:19px 21px;border-bottom:1px solid var(--line)}.detail-head h2{margin:0;font-size:18px;line-height:1.4}.detail-head p{margin:5px 0 0;color:var(--muted);font-size:11px}.detail-close{flex:0 0 auto;width:38px;height:38px;border:1px solid var(--line);border-radius:10px;background:var(--panel2);color:var(--text);font-size:19px;cursor:pointer}.detail-body{padding:19px 21px;overflow:auto}.detail-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-bottom:15px}.detail-stat{padding:11px;border:1px solid var(--line);border-radius:11px;background:var(--panel2)}.detail-stat span{display:block;color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.04em}.detail-stat strong{display:block;margin-top:5px;font-size:12px;overflow-wrap:anywhere}.detail-chart-note,.detail-truth{color:var(--muted);font-size:10px;line-height:1.55}.detail-chart-note{margin:0 0 8px}.detail-truth{margin:13px 0 0}.chart-shell{min-height:285px;padding:13px;border:1px solid var(--line);border-radius:14px;background:var(--bg)}.chart-shell .loading,.chart-shell .error,.chart-shell .empty{margin:0;min-height:255px;display:grid;place-items:center;padding:24px}.chart-title{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}.chart-title strong{font-size:12px}.chart-title span{color:var(--muted);font-size:10px}.chart-grid{stroke:var(--line);stroke-width:1}.chart-line{fill:none;stroke:var(--good);stroke-width:3;stroke-linejoin:round;stroke-linecap:round}.chart-area{fill:color-mix(in srgb,var(--good) 18%,transparent)}.chart-label{fill:var(--muted);font-size:10px}.steam-link{display:inline-flex;margin-top:14px;padding:9px 12px;border:1px solid color-mix(in srgb,var(--good) 42%,var(--line));border-radius:9px;color:var(--good);font-size:11px;font-weight:800;text-decoration:none}.steam-link[hidden]{display:none}@media(max-width:700px){.detail-grid{grid-template-columns:1fr 1fr}.detail-body,.detail-head{padding-left:14px;padding-right:14px}}@media(max-width:430px){.detail-grid{grid-template-columns:1fr}}
  </style>
</head>
<body>
  <header><div class="top wrap"><a class="brand" href="/">◆ CS2 Market Lab</a><nav class="nav"><a href="/" data-i18n="overview">Обзор</a><a href="/deals" data-i18n="deals">Сделки</a><a class="active" aria-current="page" href="/catalog" data-i18n="catalog">Каталог</a><a href="/knives" data-i18n="knives">Ножи</a><a href="/account" data-i18n="inventory">Инвентарь</a></nav><select class="locale" id="locale"><option value="ru">RU</option><option value="en">EN</option></select></div></header>
  <main class="wrap"><section class="hero"><span class="eyebrow" data-i18n="eyebrow">быстрый локальный справочник</span><h1 data-i18n="title">Все скины — без ожидания тяжёлой БД.</h1><p data-i18n="lead">Скины и перчатки берутся из полного локального снимка каталога. Цены подгружаются из готовых дневных агрегатов только для видимой страницы.</p></section>
  <div class="tabs" role="tablist"><button class="tab active" data-tab="skins" data-i18n="skins">Скины</button><button class="tab" data-tab="gloves" data-i18n="gloves">Перчатки</button><button class="tab" data-tab="stickers" data-i18n="stickers">Наклейки</button><a class="tab" href="/knives" data-i18n="knivesWorkspace">Ножи и графики ↗</a><a class="tab" href="/market-history" data-i18n="historyWorkspace">Источники цен ↗</a></div>
  <section class="panel"><form class="filters" id="filters"><label class="field"><span data-i18n="search">Поиск</span><input id="search" name="search" placeholder="AK-47, Asiimov…"></label><label class="field catalog-filter"><span data-i18n="weapon">Оружие</span><select id="weapon"><option value="" data-i18n="all">Все</option></select></label><label class="field catalog-filter"><span data-i18n="wear">Износ</span><select id="wear"><option value="" data-i18n="all">Все</option></select></label><label class="field catalog-filter"><span data-i18n="collection">Коллекция</span><select id="collection"><option value="" data-i18n="all">Все</option></select></label><label class="field catalog-filter"><span data-i18n="case">Кейс</span><select id="case"><option value="" data-i18n="all">Все</option></select></label><label class="field sticker-filter" hidden><span data-i18n="stickerType">Тип</span><select id="stickerType"><option value="" data-i18n="all">Все</option></select></label><button class="apply" type="submit" data-i18n="apply">Найти</button></form><div class="truth" id="truth"></div><div class="meta"><span id="count"></span><span id="asOf"></span></div><div id="results" class="cards"><div class="loading" data-i18n="loading">Загрузка…</div></div><div class="pagination"><button id="previous" type="button" data-i18n="previous">Назад</button><span id="page"></span><button id="next" type="button" data-i18n="next">Дальше</button></div></section></main>
  <dialog id="catalogDetail" aria-labelledby="catalogDetailTitle">
    <div class="detail-head"><div><h2 id="catalogDetailTitle">—</h2><p id="catalogDetailSubtitle"></p></div><button class="detail-close" id="catalogDetailClose" type="button" aria-label="Закрыть">×</button></div>
    <div class="detail-body" aria-live="polite"><div class="detail-grid" id="catalogDetailStats"></div><p class="detail-chart-note" id="catalogDetailChartNote"></p><div class="chart-shell" id="catalogDetailChart"></div><a class="steam-link" id="catalogDetailSteam" href="#" target="_blank" rel="noopener noreferrer" hidden></a><p class="detail-truth" id="catalogDetailTruth"></p></div>
  </dialog>
  <script>
  (()=>{'use strict';const $=s=>document.querySelector(s),$$=s=>Array.from(document.querySelectorAll(s));const I={ru:{overview:'Обзор',deals:'Сделки',catalog:'Каталог',knives:'Ножи',inventory:'Инвентарь',eyebrow:'быстрый локальный справочник',title:'Все скины — без ожидания тяжёлой БД.',lead:'Скины и перчатки берутся из полного локального снимка каталога. Цены подгружаются из готовых дневных агрегатов только для видимой страницы.',skins:'Скины',gloves:'Перчатки',stickers:'Наклейки',knivesWorkspace:'Ножи и графики ↗',historyWorkspace:'Источники цен ↗',search:'Поиск',weapon:'Оружие',wear:'Износ',collection:'Коллекция',case:'Кейс',stickerType:'Тип',all:'Все',apply:'Найти',loading:'Загрузка…',previous:'Назад',next:'Дальше',found:'Найдено: {n}',page:'Страница {n}',asOf:'Данные на {day}',noData:'Нет результатов',loadError:'Не удалось загрузить каталог',observed:'наблюдений: {n}',sales:'продаж за 30 дн.: {n}',noPrice:'цены пока нет',authoritative:'Полный опубликованный снимок локального каталога; непроданные варианты тоже показаны.',stickersTruth:'Только наклейки, замеченные на предметах в локальной базе. Это не полный список всех наклеек CS2.'},en:{overview:'Overview',deals:'Deals',catalog:'Catalogue',knives:'Knives',inventory:'Inventory',eyebrow:'fast local catalogue',title:'Every skin without a slow raw-database scan.',lead:'Skins and gloves come from the complete local catalogue snapshot. Prices are loaded from daily aggregates only for the visible page.',skins:'Skins',gloves:'Gloves',stickers:'Stickers',knivesWorkspace:'Knives and charts ↗',historyWorkspace:'Price sources ↗',search:'Search',weapon:'Weapon',wear:'Wear',collection:'Collection',case:'Case',stickerType:'Type',all:'All',apply:'Search',loading:'Loading…',previous:'Previous',next:'Next',found:'Found: {n}',page:'Page {n}',asOf:'Data as of {day}',noData:'No results',loadError:'Could not load catalogue',observed:'observations: {n}',sales:'30d sales: {n}',noPrice:'no price yet',authoritative:'Complete published local catalogue snapshot; unseen variants are included.',stickersTruth:'Only stickers observed on items in the local database. This is not the complete CS2 sticker schema.'}};let locale='__LOCALE__',payload=null,tab=new URLSearchParams(location.search).get('tab')||'skins';if(!['skins','gloves','stickers'].includes(tab))tab='skins';const tr=(key,vars={})=>{let value=I[locale][key]||key;Object.entries(vars).forEach(([k,v])=>value=value.replace(`{${k}}`,v));return value};const money=c=>Number.isFinite(Number(c))?'$'+(Number(c)/100).toLocaleString(locale,{minimumFractionDigits:2,maximumFractionDigits:2}):tr('noPrice');const clear=node=>{while(node.firstChild)node.removeChild(node.firstChild)};function option(select,entry){const node=document.createElement('option');node.value=entry.value;node.textContent=`${entry.value} (${entry.count})`;select.appendChild(node)}function fill(id,values,current){const select=$(id),first=select.firstElementChild;clear(select);select.appendChild(first);(values||[]).forEach(v=>option(select,v));select.value=current||''}function applyLocale(){document.documentElement.lang=locale;$$('[data-i18n]').forEach(node=>node.textContent=tr(node.dataset.i18n));$('#locale').value=locale;if(payload)render(payload)}function render(data){payload=data;const stickers=data.tab==='stickers';$$('.catalog-filter').forEach(n=>n.hidden=stickers);$$('.sticker-filter').forEach(n=>n.hidden=!stickers);$('#truth').innerHTML=`<strong>${stickers?'OBSERVED':'CATALOG'}</strong> · ${tr(stickers?'stickersTruth':'authoritative')}`;$('#count').textContent=tr('found',{n:Number(data.pagination?.total||0).toLocaleString(locale)});$('#asOf').textContent=data.summary?.as_of_day?tr('asOf',{day:data.summary.as_of_day}):'';const current=data.filters||{};if(stickers)fill('#stickerType',data.facets?.sticker_types,current.sticker_type);else{fill('#weapon',data.facets?.weapons,current.weapon);fill('#wear',data.facets?.wears,current.wear);fill('#collection',data.facets?.collections,current.collection);fill('#case',data.facets?.cases,current.case)}const results=$('#results');clear(results);if(!data.items?.length){const empty=document.createElement('div');empty.className='empty';empty.textContent=tr('noData');results.appendChild(empty)}else data.items.forEach(item=>{const card=document.createElement('article');card.className='card';const art=item.icon_url?document.createElement('img'):document.createElement('div');art.className='art'+(item.icon_url?'':' placeholder');if(item.icon_url){art.src=item.icon_url;art.alt='';art.loading='lazy';art.referrerPolicy='no-referrer'}else art.textContent=stickers?'●':'◆';const body=document.createElement('div'),title=document.createElement('h2');title.textContent=stickers?item.name:item.market_hash_name;body.appendChild(title);const tags=document.createElement('div');tags.className='tags';[stickers?item.sticker_type:item.weapon,stickers?null:item.wear,...(stickers?[]:(item.collections||[]).slice(0,1))].filter(Boolean).forEach(value=>{const tag=document.createElement('span');tag.className='tag';tag.textContent=value;tags.appendChild(tag)});body.appendChild(tags);const price=document.createElement('div');price.className='price';price.textContent=money(stickers?item.reference_price_cents:item.current_price_cents);body.appendChild(price);const stats=document.createElement('div');stats.className='stats';stats.textContent=stickers?tr('observed',{n:item.observed_placements||0}):tr('sales',{n:item.sales_30d||0});body.appendChild(stats);card.append(art,body);results.appendChild(card)});const limit=Number(data.pagination?.limit||48),offset=Number(data.pagination?.offset||0);$('#page').textContent=tr('page',{n:Math.floor(offset/limit)+1});$('#previous').disabled=offset<=0;$('#next').disabled=!data.pagination?.has_more;$$('[data-tab]').forEach(n=>n.classList.toggle('active',n.dataset.tab===data.tab))}function params(reset=false){const old=new URLSearchParams(location.search),p=new URLSearchParams();p.set('tab',tab);const values={search:$('#search').value,weapon:$('#weapon').value,wear:$('#wear').value,collection:$('#collection').value,case:$('#case').value,sticker_type:$('#stickerType').value};Object.entries(values).forEach(([k,v])=>{if(v&&((tab==='stickers')===['search','sticker_type'].includes(k)||k==='search'))p.set(k,v)});if(!reset&&old.get('offset'))p.set('offset',old.get('offset'));return p}async function load(reset=false){const p=params(reset);history.replaceState(null,'',`${location.pathname}?${p}`);$('#results').innerHTML=`<div class="loading">${tr('loading')}</div>`;try{const response=await fetch(`/api/catalog?${p}`,{credentials:'same-origin',headers:{Accept:'application/json'}});if(response.status===401){location.assign('/login?next=%2Fcatalog');return}const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||tr('loadError'));render(data)}catch(error){$('#results').innerHTML='';const node=document.createElement('div');node.className='error';node.textContent=error.message||tr('loadError');$('#results').appendChild(node)}}$$('[data-tab]').forEach(n=>n.addEventListener('click',()=>{tab=n.dataset.tab;$('#filters').reset();load(true)}));$('#filters').addEventListener('submit',event=>{event.preventDefault();load(true)});$('#previous').addEventListener('click',()=>{const p=params();p.set('offset',String(Math.max(0,Number(payload?.pagination?.offset||0)-Number(payload?.pagination?.limit||48))));history.replaceState(null,'',`${location.pathname}?${p}`);load()});$('#next').addEventListener('click',()=>{const p=params();p.set('offset',String(Number(payload?.pagination?.offset||0)+Number(payload?.pagination?.limit||48)));history.replaceState(null,'',`${location.pathname}?${p}`);load()});$('#locale').addEventListener('change',event=>{locale=event.target.value;document.cookie=`cs2_locale=${locale}; Path=/; SameSite=Lax`;applyLocale()});const initial=new URLSearchParams(location.search);$('#search').value=initial.get('search')||'';applyLocale();load()})();
  </script>
  <script>
  (()=>{
    'use strict';
    const dialog=document.getElementById('catalogDetail');
    const results=document.getElementById('results');
    const closeButton=document.getElementById('catalogDetailClose');
    const title=document.getElementById('catalogDetailTitle');
    const subtitle=document.getElementById('catalogDetailSubtitle');
    const statsMount=document.getElementById('catalogDetailStats');
    const chartNote=document.getElementById('catalogDetailChartNote');
    const chartMount=document.getElementById('catalogDetailChart');
    const steamLink=document.getElementById('catalogDetailSteam');
    const truth=document.getElementById('catalogDetailTruth');
    const copy={
      ru:{documentTitle:'Каталог · CS2 Market Lab',openDetails:'Открыть подробности: {name}',close:'Закрыть',loading:'Загружаем историю…',error:'Не удалось загрузить подробности.',noHistory:'Для этого предмета пока нет истории цен.',skinSubtitle:'Наблюдаемая история рынка · 90 дней',stickerSubtitle:'Наблюдавшиеся размещения на скинах',skinChart:'Медианная цена скина',skinChartNote:'График показывает медианную цену состоявшихся продаж этого вида скина.',stickerChart:'Средняя цена скинов с этой наклейкой',stickerChartNote:'Важно: график показывает цены скинов, на которых была эта наклейка, а не отдельную рыночную цену наклейки.',skinTruth:'История основана только на данных, собранных этим проектом. Оценка количества на маркете не равна числу предметов во всём мире.',stickerTruth:'Это наблюдения локальной базы. Они не доказывают общее количество наклеек и не являются графиком отдельной цены наклейки.',steam:'Открыть в Steam Market ↗',lastPrice:'Последняя медиана',asOf:'Последняя дата',sales30:'Продаж за 30 дней',observedSales:'Продаж в базе',uniqueAssets:'Уникальных asset ID',marketQuantity:'На маркете, оценка',floatRange:'Диапазон float',collection:'Коллекция',placements:'Размещений наклейки',listings:'Лотов со стикером',carryingSkins:'Видов скинов',avgSkinPrice:'Средняя цена скина',medianSkinPrice:'Медиана цены скина',latestSale:'Последняя продажа',points:'точек: {n}'},
      en:{documentTitle:'Catalogue · CS2 Market Lab',openDetails:'Open details: {name}',close:'Close',loading:'Loading price history…',error:'Could not load item details.',noHistory:'There is no price history for this item yet.',skinSubtitle:'Observed market history · 90 days',stickerSubtitle:'Observed placements on skins',skinChart:'Median skin price',skinChartNote:'The chart shows the median price of completed sales for this skin variant.',stickerChart:'Average price of skins carrying this sticker',stickerChartNote:'Important: this chart shows prices of skins carrying the sticker, not the standalone market price of the sticker.',skinTruth:'History uses only data collected by this project. The market quantity estimate is not the number of items that exist worldwide.',stickerTruth:'These are observations from the local database. They do not establish total sticker supply and are not a standalone sticker-price chart.',steam:'Open in Steam Market ↗',lastPrice:'Latest median',asOf:'Latest date',sales30:'30-day sales',observedSales:'Observed sales',uniqueAssets:'Unique asset IDs',marketQuantity:'Market estimate',floatRange:'Float range',collection:'Collection',placements:'Sticker placements',listings:'Listings with sticker',carryingSkins:'Skin variants',avgSkinPrice:'Average skin price',medianSkinPrice:'Median skin price',latestSale:'Latest sale',points:'points: {n}'}
    };
    let requestNumber=0;
    let controller=null;
    let detailState=null;
    const locale=()=>document.documentElement.lang==='en'?'en':'ru';
    const tr=(key,values={})=>(copy[locale()][key]||key).replace(/\{(\w+)\}/g,(_,name)=>String(values[name]??''));
    const clear=node=>{while(node.firstChild)node.removeChild(node.firstChild);};
    const hasNumber=value=>value!==null&&value!==undefined&&value!==''&&Number.isFinite(Number(value));
    const number=value=>hasNumber(value)?new Intl.NumberFormat(locale()==='ru'?'ru-RU':'en-US').format(Number(value)):'—';
    const money=value=>hasNumber(value)?new Intl.NumberFormat(locale()==='ru'?'ru-RU':'en-US',{style:'currency',currency:'USD'}).format(Number(value)/100):'—';
    function currentTab(){const value=new URLSearchParams(location.search).get('tab');return value==='stickers'?'stickers':value==='gloves'?'gloves':'skins';}
    function prepareCards(){results.querySelectorAll('.card').forEach(card=>{const heading=card.querySelector('h2');const name=String(heading?.textContent||'').trim();if(!name)return;card.tabIndex=0;card.setAttribute('role','button');card.dataset.detailName=name;card.dataset.detailTab=currentTab();card.setAttribute('aria-label',tr('openDetails',{name}));});}
    function safeSteam(value){try{const url=new URL(String(value||''));const host=url.hostname.toLowerCase();return url.protocol==='https:'&&(host==='steamcommunity.com'||host==='www.steamcommunity.com')&&url.pathname.startsWith('/market/listings/730/')&&!url.username&&!url.password?url.href:'';}catch(_){return '';}}
    function fallbackSteam(name){return `https://steamcommunity.com/market/listings/730/${encodeURIComponent(name)}`;}
    function setSteam(name,candidate){const href=safeSteam(candidate)||safeSteam(fallbackSteam(name));steamLink.textContent=tr('steam');steamLink.hidden=!href;if(href)steamLink.href=href;else steamLink.removeAttribute('href');}
    function addStat(label,value){const box=document.createElement('div');box.className='detail-stat';const caption=document.createElement('span');caption.textContent=label;const strong=document.createElement('strong');strong.textContent=value??'—';box.append(caption,strong);statsMount.appendChild(box);}
    function showMessage(kind){clear(chartMount);const node=document.createElement('div');node.className=kind;node.setAttribute('role',kind==='error'?'alert':'status');node.textContent=tr(kind==='loading'?'loading':kind==='error'?'error':'noHistory');chartMount.appendChild(node);}
    function svgElement(name,attributes={}){const node=document.createElementNS('http://www.w3.org/2000/svg',name);Object.entries(attributes).forEach(([key,value])=>node.setAttribute(key,String(value)));return node;}
    function drawChart(rawRows,valueField,titleKey){const rows=(Array.isArray(rawRows)?rawRows:[]).map(row=>({day:String(row?.day||''),value:Number(row?.[valueField])})).filter(row=>row.day&&Number.isFinite(row.value)).slice(-90);clear(chartMount);if(!rows.length){showMessage('empty');return;}const width=820,height=270,pad={left:62,right:18,top:18,bottom:34};const values=rows.map(row=>row.value);let minimum=Math.min(...values),maximum=Math.max(...values);if(minimum===maximum){const spread=Math.max(Math.abs(minimum)*.02,1);minimum-=spread;maximum+=spread;}const x=index=>pad.left+(rows.length===1?.5:index/(rows.length-1))*(width-pad.left-pad.right);const y=value=>pad.top+(maximum-value)/(maximum-minimum)*(height-pad.top-pad.bottom);const coordinates=rows.map((row,index)=>[x(index),y(row.value)]);const line=coordinates.map((point,index)=>`${index?'L':'M'}${point[0].toFixed(1)},${point[1].toFixed(1)}`).join(' ');const area=`${line} L${coordinates[coordinates.length-1][0].toFixed(1)},${height-pad.bottom} L${coordinates[0][0].toFixed(1)},${height-pad.bottom} Z`;const header=document.createElement('div');header.className='chart-title';const heading=document.createElement('strong');heading.textContent=tr(titleKey);const meta=document.createElement('span');meta.textContent=tr('points',{n:number(rows.length)});header.append(heading,meta);chartMount.appendChild(header);const svg=svgElement('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':tr(titleKey)});[0,.5,1].forEach(ratio=>{const yy=pad.top+ratio*(height-pad.top-pad.bottom);const price=maximum-ratio*(maximum-minimum);svg.appendChild(svgElement('line',{class:'chart-grid',x1:pad.left,x2:width-pad.right,y1:yy,y2:yy}));const label=svgElement('text',{class:'chart-label',x:4,y:yy+4});label.textContent=money(price);svg.appendChild(label);});svg.appendChild(svgElement('path',{class:'chart-area',d:area}));svg.appendChild(svgElement('path',{class:'chart-line',d:line}));const first=svgElement('text',{class:'chart-label',x:pad.left,y:height-8});first.textContent=rows[0].day;const last=svgElement('text',{class:'chart-label','text-anchor':'end',x:width-pad.right,y:height-8});last.textContent=rows[rows.length-1].day;svg.append(first,last);chartMount.appendChild(svg);}
    function floatRange(catalog){if(!hasNumber(catalog?.min_float)&&!hasNumber(catalog?.max_float))return '—';return `${hasNumber(catalog?.min_float)?Number(catalog.min_float).toFixed(4):'—'} – ${hasNumber(catalog?.max_float)?Number(catalog.max_float).toFixed(4):'—'}`;}
    function salesLast30(series,asOfDay){const end=Date.parse(`${asOfDay||''}T00:00:00Z`);if(!Number.isFinite(end))return null;const start=end-29*86400000;return series.reduce((sum,row)=>{const timestamp=Date.parse(`${row?.day||''}T00:00:00Z`);return timestamp>=start&&timestamp<=end?sum+Math.max(0,Number(row?.sales_count)||0):sum;},0);}
    function renderSkinDetail(state){const data=state.data||{},catalog=data.catalog||{},series=Array.isArray(data.series)?data.series:[];const priceRows=series.filter(row=>hasNumber(row?.median_price_cents));const latest=priceRows.length?priceRows[priceRows.length-1]:null;subtitle.textContent=tr('skinSubtitle');clear(statsMount);addStat(tr('lastPrice'),money(latest?.median_price_cents));addStat(tr('asOf'),data.as_of_day||latest?.day||'—');addStat(tr('sales30'),number(salesLast30(series,data.as_of_day||latest?.day)));addStat(tr('observedSales'),number(catalog.observed_sales_count));addStat(tr('uniqueAssets'),number(catalog.unique_asset_count));addStat(tr('marketQuantity'),number(catalog.quantity_on_market_estimate));addStat(tr('floatRange'),floatRange(catalog));addStat(tr('collection'),catalog.collection_name||'—');chartNote.textContent=tr('skinChartNote');truth.textContent=tr('skinTruth');setSteam(state.name,catalog.steam_market_url);drawChart(series,'median_price_cents','skinChart');}
    function renderStickerDetail(state){const data=state.data||{},stats=data.stats||{},daily=Array.isArray(data.daily)?data.daily:[];subtitle.textContent=tr('stickerSubtitle');clear(statsMount);addStat(tr('placements'),number(stats.sticker_rows));addStat(tr('listings'),number(stats.listing_count));addStat(tr('carryingSkins'),number(stats.skin_count));addStat(tr('avgSkinPrice'),money(stats.avg_skin_price_cents));addStat(tr('medianSkinPrice'),money(stats.median_skin_price_cents));addStat(tr('latestSale'),stats.latest_sold_at||'—');chartNote.textContent=tr('stickerChartNote');truth.textContent=tr('stickerTruth');setSteam(state.name,data.steam_market_url);drawChart(daily,'avg_price_cents','stickerChart');}
    function renderLoadedDetail(){if(!detailState?.data)return;if(detailState.tab==='stickers')renderStickerDetail(detailState);else renderSkinDetail(detailState);}
    function renderLoadingDetail(name,tab){title.textContent=name;subtitle.textContent=tr(tab==='stickers'?'stickerSubtitle':'skinSubtitle');clear(statsMount);chartNote.textContent=tr(tab==='stickers'?'stickerChartNote':'skinChartNote');truth.textContent=tr(tab==='stickers'?'stickerTruth':'skinTruth');setSteam(name,'');showMessage('loading');closeButton.setAttribute('aria-label',tr('close'));}
    async function openDetail(card){const name=String(card.dataset.detailName||'').trim();const tab=card.dataset.detailTab==='stickers'?'stickers':card.dataset.detailTab==='gloves'?'gloves':'skins';if(!name)return;requestNumber+=1;const requestId=requestNumber;if(controller)controller.abort();controller=new AbortController();detailState={name,tab,data:null,loading:true};renderLoadingDetail(name,tab);if(!dialog.open){if(typeof dialog.showModal==='function')dialog.showModal();else dialog.setAttribute('open','');}const params=new URLSearchParams();params.set('name',name);let endpoint;if(tab==='stickers'){params.set('sticker_id',card.dataset.stickerId||'');endpoint=`/api/sticker-detail?${params}`;}else{params.set('days','90');endpoint=`/api/market/skin-series?${params}`;}try{const response=await fetch(endpoint,{credentials:'same-origin',headers:{Accept:'application/json'},signal:controller.signal});if(response.status===401){location.assign('/login?next=%2Fcatalog');return;}const data=await response.json().catch(()=>({}));if(!response.ok||data.error)throw new Error('detail unavailable');if(requestId!==requestNumber)return;detailState={name,tab,data,loading:false};renderLoadedDetail();}catch(error){if(error?.name==='AbortError'||requestId!==requestNumber)return;detailState={name,tab,data:null,loading:false,error:true};showMessage('error');}}
    function closeDialog(){requestNumber+=1;if(controller)controller.abort();controller=null;if(typeof dialog.close==='function'&&dialog.open)dialog.close();else dialog.removeAttribute('open');}
    results.addEventListener('click',event=>{const card=event.target.closest('.card[data-detail-name]');if(card)openDetail(card);});
    results.addEventListener('keydown',event=>{if(event.key!=='Enter'&&event.key!==' ')return;const card=event.target.closest('.card[data-detail-name]');if(!card)return;event.preventDefault();openDetail(card);});
    closeButton.addEventListener('click',closeDialog);
    dialog.addEventListener('click',event=>{if(event.target===dialog)closeDialog();});
    dialog.addEventListener('close',()=>{requestNumber+=1;if(controller)controller.abort();controller=null;});
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&dialog.hasAttribute('open')&&typeof dialog.close!=='function')closeDialog();});
    document.getElementById('locale').addEventListener('change',()=>setTimeout(()=>{document.title=tr('documentTitle');closeButton.setAttribute('aria-label',tr('close'));prepareCards();if(detailState?.loading)renderLoadingDetail(detailState.name,detailState.tab);else if(detailState?.data)renderLoadedDetail();else if(detailState?.error)showMessage('error');},0));
    new MutationObserver(prepareCards).observe(results,{childList:true,subtree:true});
    document.title=tr('documentTitle');
    prepareCards();
  })();
  </script>
</body>
</html>"""


def render_catalog_page(locale: str = "ru") -> str:
    """Return the self-contained ``/catalog`` page in Russian or English."""

    normalized = locale if locale in {"ru", "en"} else "ru"
    rendered = CATALOG_HTML.replace("__LOCALE__", normalized)
    rendered = rendered.replace(
        "const I={ru:{overview:'Обзор'",
        "const I={ru:{navHistory:'История рынков',overview:'Обзор'",
        1,
    ).replace(
        "},en:{overview:'Overview'",
        "},en:{navHistory:'Market history',overview:'Overview'",
        1,
    )
    # JavaScript's ``Number(null)`` is zero.  Guard the nullable API contract
    # before numeric coercion so unobserved catalogue variants never render as
    # a misleading $0.00 item.
    rendered = rendered.replace(
        "const money=c=>Number.isFinite(Number(c))?",
        "const money=c=>c!==null&&c!==undefined&&c!==''&&Number.isFinite(Number(c))?",
        1,
    )
    rendered = rendered.replace(
        "noPrice:'цены пока нет',authoritative:",
        "noPrice:'цены пока нет',referencePrice:'справочно',authoritative:",
        1,
    ).replace(
        "noPrice:'no price yet',authoritative:",
        "noPrice:'no price yet',referencePrice:'reference',authoritative:",
        1,
    )
    rendered = rendered.replace(
        "price.textContent=money(stickers?item.reference_price_cents:item.current_price_cents);",
        "price.textContent=stickers?(item.reference_price_cents===null||item.reference_price_cents===undefined?tr('noPrice'):`${tr('referencePrice')} ${money(item.reference_price_cents)}`):money(item.current_price_cents);",
        1,
    )
    rendered = rendered.replace(
        "const rows=(Array.isArray(rawRows)?rawRows:[]).map(row=>({day:String(row?.day||''),value:Number(row?.[valueField])})).filter(row=>row.day&&Number.isFinite(row.value)).slice(-90);",
        "const rows=(Array.isArray(rawRows)?rawRows:[]).filter(row=>String(row?.day||'')&&hasNumber(row?.[valueField])).map(row=>({day:String(row.day),value:Number(row[valueField])})).slice(-90);",
        1,
    )
    # Sticker names are not globally unique (for example two production IDs
    # exist for some map Gold stickers).  Preserve the identity on the card so
    # the detail endpoint does not silently select the more frequent sibling.
    rendered = rendered.replace(
        "card.className='card';const art=",
        "card.className='card';if(stickers&&item.sticker_id!==null&&item.sticker_id!==undefined)card.dataset.stickerId=String(item.sticker_id);const art=",
        1,
    )
    # Preserve shareable filtered URLs.  Select facets arrive with the first
    # API response, so seed a temporary option before that request and let the
    # normal renderer replace it with the authoritative facet list afterward.
    rendered = rendered.replace(
        "const initial=new URLSearchParams(location.search);$('#search').value=initial.get('search')||'';applyLocale();load()})();",
        "const initial=new URLSearchParams(location.search);"
        "const initialFields={search:'#search',weapon:'#weapon',wear:'#wear',collection:'#collection',case:'#case',sticker_type:'#stickerType'};"
        "Object.entries(initialFields).forEach(([key,selector])=>{const value=initial.get(key)||'';const node=$(selector);"
        "if(!node||!value)return;if(node.tagName==='SELECT'&&!Array.from(node.options).some(option=>option.value===value))node.add(new Option(value,value));node.value=value;});"
        "applyLocale();load()})();",
        1,
    )
    if normalized == "en":
        rendered = rendered.replace(
            "<title>Каталог · CS2 Market Lab</title>",
            "<title>Catalogue · CS2 Market Lab</title>",
            1,
        )
    return rendered


def _clear_sticker_cache_for_tests() -> None:
    with _STICKER_CACHE_LOCK:
        _STICKER_CACHE.clear()


__all__ = [
    "CATALOG_HTML",
    "MAX_LIMIT",
    "build_catalog_payload",
    "render_catalog_page",
]
