"""Bounded, index-aware search for the dashboard's listing browser.

The history database is large enough that a single ``LIKE '%query%'`` over
``sales`` combined with a correlated sticker lookup can hold a read snapshot
for minutes.  This module resolves human search text against the much smaller
item/sticker name catalogues first, then performs exact indexed lookups.

The public function accepts an existing SQLite connection.  It never creates
tables or indexes and remains usable with reduced/legacy schemas by returning
``None`` for optional columns.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any

ALLOWED_SCOPES = frozenset({"skin", "sticker", "listing", "all"})
MAX_LIMIT = 100
MAX_OFFSET = 100_000
MAX_SEARCH_LENGTH = 180
MIN_SEARCH_LENGTH = 2
MAX_SKIN_NAMES = 2_000
MAX_STICKER_NAMES = 500
MAX_ALL_RESOLVED_NAMES = 400
MAX_ALL_LISTINGS = 25_000
RECENT_INDEX_SCAN_THRESHOLD = 5_000
RESOLVED_PREVIEW_LIMIT = 20

SALES_FIELDS = (
    "listing_id",
    "name",
    "item_icon_url",
    "item_steam_market_url",
    "item_rarity_color",
    "item_name_color",
    "price_cents",
    "float_value",
    "paint_seed",
    "paint_index",
    "inspect_link",
    "serialized_inspect",
    "sold_at",
    "fetched_at",
)
STICKER_FIELDS = (
    "listing_id",
    "sticker_index",
    "sticker_id",
    "slot",
    "name",
    "icon_url",
    "steam_market_url",
    "wear",
    "price_cents",
)

_SEPARATOR_RE = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)
_LISTING_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")
_WEAPON_ALIASES = {
    "ak": "ak 47",
    "ak47": "ak 47",
    "ak 47": "ak 47",
    "ак": "ak 47",
    "ак47": "ak 47",
    "ак 47": "ak 47",
    "mac": "mac 10",
    "mac10": "mac 10",
    "mac 10": "mac 10",
    "мак": "mac 10",
    "мак10": "mac 10",
    "мак 10": "mac 10",
}


class ListingSearchError(ValueError):
    """Base class for API-friendly validation/search errors."""


class SearchTooBroadError(ListingSearchError):
    """Raised instead of running an unbounded multi-branch query."""


@dataclass(frozen=True)
class _IdBranch:
    kind: str
    sql: str
    params: tuple[Any, ...]
    count: int
    match_mode: str | None = None


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    names = [column[0] for column in cursor.description or ()]
    result: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        if isinstance(row, sqlite3.Row):
            result.append(dict(row))
        else:
            result.append(dict(zip(names, row, strict=True)))
    return result


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        _scalar(
            conn,
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table,),
        )
        is not None
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    quoted = _quote_identifier(table)
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({quoted})")}


def _leading_index(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> str | None:
    """Return a non-partial index whose leading columns match ``columns``."""

    quoted_table = _quote_identifier(table)
    candidates: list[tuple[int, str]] = []
    for row in conn.execute(f"PRAGMA index_list({quoted_table})"):
        index_name = str(row[1])
        partial = bool(row[4]) if len(row) > 4 else False
        if partial:
            continue
        quoted_index = _quote_identifier(index_name)
        indexed_columns = tuple(
            str(info[2])
            for info in conn.execute(f"PRAGMA index_info({quoted_index})")
            if info[2] is not None
        )
        if indexed_columns[: len(columns)] == columns:
            candidates.append((len(indexed_columns), index_name))
    if not candidates:
        return None
    return min(candidates)[1]


def _index_hint(index_name: str | None) -> str:
    return f" INDEXED BY {_quote_identifier(index_name)}" if index_name else ""


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(_SEPARATOR_RE.sub(" ", text).split())


def _validate_request(
    offset: Any,
    limit: Any,
    search: Any,
    scope: Any,
) -> tuple[int, int, str, str]:
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ListingSearchError("offset must be an integer")
    if offset < 0 or offset > MAX_OFFSET:
        raise ListingSearchError(f"offset must be between 0 and {MAX_OFFSET}")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ListingSearchError("limit must be an integer")
    if limit < 1 or limit > MAX_LIMIT:
        raise ListingSearchError(f"limit must be between 1 and {MAX_LIMIT}")
    if not isinstance(search, str):
        raise ListingSearchError("search must be a string")
    query = search.strip()
    if "\x00" in query:
        raise ListingSearchError("search must not contain NUL bytes")
    if len(query) > MAX_SEARCH_LENGTH:
        raise ListingSearchError(
            f"search must not exceed {MAX_SEARCH_LENGTH} characters"
        )
    if not isinstance(scope, str) or scope not in ALLOWED_SCOPES:
        allowed = ", ".join(sorted(ALLOWED_SCOPES))
        raise ListingSearchError(f"scope must be one of: {allowed}")
    if query:
        normalized = _normalize(query)
        if len(normalized.replace(" ", "")) < MIN_SEARCH_LENGTH:
            raise ListingSearchError(
                f"search must contain at least {MIN_SEARCH_LENGTH} letters or digits"
            )
    return offset, limit, query, scope


def _bounded_matches(
    matches: list[str],
    *,
    maximum: int,
    kind: str,
) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for match in matches:
        if match not in seen:
            unique.append(match)
            seen.add(match)
    if len(unique) > maximum:
        raise SearchTooBroadError(
            f"{kind} search matched {len(unique)} names; refine the query "
            f"(maximum {maximum})"
        )
    return unique


def _catalog_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = _table_columns(conn, "cs2_item_catalog")
    if "market_hash_name" not in columns:
        return []
    weapon = "weapon_name" if "weapon_name" in columns else "NULL AS weapon_name"
    return _rows(
        conn.execute(
            f"""
            SELECT market_hash_name, {weapon}
            FROM cs2_item_catalog
            WHERE market_hash_name IS NOT NULL
              AND TRIM(market_hash_name) <> ''
            ORDER BY market_hash_name
            """
        )
    )


def _distinct_sales_names(conn: sqlite3.Connection) -> list[str]:
    columns = _table_columns(conn, "sales")
    if "name" not in columns:
        return []
    name_index = _leading_index(conn, "sales", ("name",))
    cursor = conn.execute(
        f"""
        SELECT DISTINCT name
        FROM sales{_index_hint(name_index)}
        WHERE name IS NOT NULL AND TRIM(name) <> ''
        ORDER BY name
        """
    )
    return [str(row[0]) for row in cursor if row[0] is not None]


def _resolve_skin_names(conn: sqlite3.Connection, query: str) -> list[str]:
    normalized_query = _normalize(query)
    target = _WEAPON_ALIASES.get(normalized_query, normalized_query)
    catalog = _catalog_rows(conn)

    if catalog:
        weapon_names = {
            _normalize(str(row.get("weapon_name") or ""))
            for row in catalog
            if row.get("weapon_name")
        }
        exact_weapons = {name for name in weapon_names if name == target}
        prefix_weapons = {
            name
            for name in weapon_names
            if target and name.startswith((f"{target} ", target))
        }
        selected_weapons = exact_weapons or prefix_weapons
        if selected_weapons:
            matches = [
                str(row["market_hash_name"])
                for row in catalog
                if _normalize(str(row.get("weapon_name") or ""))
                in selected_weapons
            ]
            return _bounded_matches(
                matches,
                maximum=MAX_SKIN_NAMES,
                kind="skin",
            )

        tokens = target.split()
        matches = [
            str(row["market_hash_name"])
            for row in catalog
            if all(
                token in _normalize(str(row.get("market_hash_name") or ""))
                for token in tokens
            )
        ]
        return _bounded_matches(matches, maximum=MAX_SKIN_NAMES, kind="skin")

    tokens = target.split()
    matches = [
        name
        for name in _distinct_sales_names(conn)
        if all(token in _normalize(name) for token in tokens)
    ]
    return _bounded_matches(matches, maximum=MAX_SKIN_NAMES, kind="skin")


def _resolve_sticker_names(conn: sqlite3.Connection, query: str) -> list[str]:
    columns = _table_columns(conn, "sale_stickers")
    if "name" not in columns:
        return []
    name_index = _leading_index(conn, "sale_stickers", ("name",))
    cursor = conn.execute(
        f"""
        SELECT DISTINCT name
        FROM sale_stickers{_index_hint(name_index)}
        WHERE name IS NOT NULL AND TRIM(name) <> ''
        ORDER BY name
        """
    )
    tokens = _normalize(query).split()
    matches = [
        str(row[0])
        for row in cursor
        if row[0] is not None
        and all(token in _normalize(str(row[0])) for token in tokens)
    ]
    exact = _normalize(query)
    matches.sort(
        key=lambda name: (
            _normalize(name) != exact,
            not _normalize(name).startswith(exact),
            _normalize(name),
            name,
        )
    )
    return _bounded_matches(matches, maximum=MAX_STICKER_NAMES, kind="sticker")


def _placeholders(values: list[str] | tuple[Any, ...]) -> str:
    return ", ".join("?" for _ in values)


def _ensure_variable_budget(
    conn: sqlite3.Connection,
    required: int,
    *,
    kind: str,
) -> None:
    try:
        available = conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    except AttributeError:  # pragma: no cover - supported by project Python
        available = 999
    if required > available:
        raise SearchTooBroadError(
            f"{kind} search needs {required} SQL parameters but this SQLite "
            f"build permits {available}; refine the query"
        )


def _sales_select(columns: set[str], alias: str = "s") -> str:
    expressions = []
    for field in SALES_FIELDS:
        if field in columns:
            expressions.append(f"{alias}.{_quote_identifier(field)} AS {field}")
        else:
            expressions.append(f"NULL AS {field}")
    return ",\n                ".join(expressions)


def _order_sql(sales_columns: set[str], alias: str = "s") -> str:
    if "fetched_at" in sales_columns:
        return f"{alias}.fetched_at DESC, {alias}.listing_id DESC"
    return f"{alias}.listing_id DESC"


def _skin_branch(
    conn: sqlite3.Connection,
    names: list[str],
    *,
    count: int | None = None,
) -> _IdBranch | None:
    if not names or "name" not in _table_columns(conn, "sales"):
        return None
    _ensure_variable_budget(conn, len(names) + 2, kind="skin")
    name_index = _leading_index(conn, "sales", ("name",))
    sql = (
        "SELECT s.listing_id FROM sales AS s"
        f"{_index_hint(name_index)} "
        f"WHERE s.name IN ({_placeholders(names)})"
    )
    if count is None:
        count = int(
            _scalar(
                conn,
                "/* listing_search:count:skin */ "
                "SELECT COUNT(*) FROM sales AS s"
                f"{_index_hint(name_index)} "
                f"WHERE s.name IN ({_placeholders(names)})",
                tuple(names),
            )
            or 0
        )
    return _IdBranch("skin", sql, tuple(names), count)


def _sticker_branch(
    conn: sqlite3.Connection,
    names: list[str],
    *,
    count: int | None = None,
) -> _IdBranch | None:
    sticker_columns = _table_columns(conn, "sale_stickers")
    if not names or not {"listing_id", "name"}.issubset(sticker_columns):
        return None
    _ensure_variable_budget(conn, len(names) + 2, kind="sticker")
    name_index = _leading_index(conn, "sale_stickers", ("name",))
    exact_sql = (
        "SELECT DISTINCT st.listing_id FROM sale_stickers AS st"
        f"{_index_hint(name_index)} "
        f"WHERE st.name IN ({_placeholders(names)})"
    )
    if count is None:
        count = int(
            _scalar(
                conn,
                "/* listing_search:count:sticker */ "
                "SELECT COUNT(*) FROM ("
                f"{exact_sql}"
                ") AS matched JOIN sales AS s "
                "ON s.listing_id = matched.listing_id",
                tuple(names),
            )
            or 0
        )
    return _IdBranch("sticker", exact_sql, tuple(names), count)


def _prefix_bounds(prefix: str) -> tuple[str, str | None]:
    for index in range(len(prefix) - 1, -1, -1):
        codepoint = ord(prefix[index])
        if codepoint < 0x10FFFF:
            return prefix, prefix[:index] + chr(codepoint + 1)
    return prefix, None


def _listing_branch(
    conn: sqlite3.Connection,
    query: str,
    *,
    allow_short: bool,
) -> _IdBranch | None:
    if not _LISTING_ID_RE.fullmatch(query):
        if allow_short:
            raise ListingSearchError(
                "listing search may contain only letters, digits, '_' and '-'"
            )
        return None
    if not allow_short and len(query) < 6:
        return None

    exact = _scalar(
        conn,
        "SELECT 1 FROM sales WHERE listing_id = ? LIMIT 1",
        (query,),
    )
    if exact is not None:
        return _IdBranch(
            "listing",
            "SELECT listing_id FROM sales WHERE listing_id = ?",
            (query,),
            1,
            "exact",
        )

    lower, upper = _prefix_bounds(query)
    if upper is None:
        sql = "SELECT listing_id FROM sales WHERE listing_id >= ?"
        params: tuple[Any, ...] = (lower,)
    else:
        sql = (
            "SELECT listing_id FROM sales "
            "WHERE listing_id >= ? AND listing_id < ?"
        )
        params = (lower, upper)
    count = int(
        _scalar(
            conn,
            "/* listing_search:count:listing */ "
            f"SELECT COUNT(*) FROM ({sql}) AS matched",
            params,
        )
        or 0
    )
    return _IdBranch("listing", sql, params, count, "prefix")


def _fetch_branch_rows(
    conn: sqlite3.Connection,
    branch: _IdBranch,
    *,
    sales_columns: set[str],
    offset: int,
    limit: int,
) -> tuple[list[dict[str, Any]], str]:
    recent_index = (
        _leading_index(conn, "sales", ("fetched_at", "listing_id"))
        if "fetched_at" in sales_columns
        else None
    )
    select_sql = _sales_select(sales_columns)
    order_sql = _order_sql(sales_columns)
    if branch.kind == "skin":
        name_index = _leading_index(conn, "sales", ("name",))
        if branch.count > RECENT_INDEX_SCAN_THRESHOLD and recent_index:
            source_hint = recent_index
            strategy = "recent_index_scan"
            marker = "broad"
        else:
            source_hint = name_index
            strategy = "matched_index_sort"
            marker = "rare"
        sql = f"""
            /* listing_search:page:skin:{marker} */
            SELECT {select_sql}
            FROM sales AS s{_index_hint(source_hint)}
            WHERE s.name IN ({_placeholders(branch.params)})
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
        return (
            _rows(conn.execute(sql, (*branch.params, limit, offset))),
            strategy,
        )

    if branch.count > RECENT_INDEX_SCAN_THRESHOLD and recent_index:
        sql = f"""
            /* listing_search:page:{branch.kind}:broad */
            SELECT {select_sql}
            FROM sales AS s{_index_hint(recent_index)}
            WHERE s.listing_id IN ({branch.sql})
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
        params = (*branch.params, limit, offset)
        strategy = "recent_index_scan"
    else:
        sql = f"""
            /* listing_search:page:{branch.kind}:rare */
            SELECT {select_sql}
            FROM ({branch.sql}) AS matched
            JOIN sales AS s ON s.listing_id = matched.listing_id
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
        params = (*branch.params, limit, offset)
        strategy = "matched_index_sort"
    return _rows(conn.execute(sql, params)), strategy


def _fetch_unfiltered_rows(
    conn: sqlite3.Connection,
    *,
    sales_columns: set[str],
    offset: int,
    limit: int,
) -> tuple[int, list[dict[str, Any]], str]:
    total = int(
        _scalar(
            conn,
            "/* listing_search:count:recent */ SELECT COUNT(*) FROM sales",
        )
        or 0
    )
    recent_index = (
        _leading_index(conn, "sales", ("fetched_at", "listing_id"))
        if "fetched_at" in sales_columns
        else None
    )
    rows = _rows(
        conn.execute(
            f"""
            /* listing_search:page:recent */
            SELECT {_sales_select(sales_columns)}
            FROM sales AS s{_index_hint(recent_index)}
            ORDER BY {_order_sql(sales_columns)}
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
    )
    return total, rows, "recent_index_scan" if recent_index else "recent_sort"


def _fetch_all_rows(
    conn: sqlite3.Connection,
    branches: list[_IdBranch],
    *,
    sales_columns: set[str],
    offset: int,
    limit: int,
) -> tuple[int, list[dict[str, Any]], str]:
    nonempty = [branch for branch in branches if branch.count]
    upper_bound = sum(branch.count for branch in nonempty)
    if upper_bound > MAX_ALL_LISTINGS:
        raise SearchTooBroadError(
            f"all-scope search may touch up to {upper_bound} listings; choose a "
            f"specific scope or refine the query (maximum {MAX_ALL_LISTINGS})"
        )
    if not nonempty:
        return 0, [], "empty"

    union_sql = " UNION ".join(branch.sql for branch in nonempty)
    union_params = tuple(value for branch in nonempty for value in branch.params)
    total = int(
        _scalar(
            conn,
            "/* listing_search:count:all */ "
            f"SELECT COUNT(*) FROM ({union_sql}) AS matched "
            "JOIN sales AS s ON s.listing_id = matched.listing_id",
            union_params,
        )
        or 0
    )

    recent_index = (
        _leading_index(conn, "sales", ("fetched_at", "listing_id"))
        if "fetched_at" in sales_columns
        else None
    )
    select_sql = _sales_select(sales_columns)
    order_sql = _order_sql(sales_columns)
    if total > RECENT_INDEX_SCAN_THRESHOLD and recent_index:
        sql = f"""
            /* listing_search:page:all:broad */
            SELECT {select_sql}
            FROM sales AS s{_index_hint(recent_index)}
            WHERE s.listing_id IN ({union_sql})
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
        strategy = "recent_index_scan"
    else:
        sql = f"""
            /* listing_search:page:all:rare */
            SELECT {select_sql}
            FROM ({union_sql}) AS matched
            JOIN sales AS s ON s.listing_id = matched.listing_id
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
        strategy = "matched_index_sort"
    rows = _rows(conn.execute(sql, (*union_params, limit, offset)))
    return total, rows, strategy


def _attach_stickers(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> None:
    listing_ids = [str(row["listing_id"]) for row in rows if row.get("listing_id")]
    for row in rows:
        row["stickers"] = []
    if not listing_ids:
        return

    columns = _table_columns(conn, "sale_stickers")
    if "listing_id" not in columns:
        return
    select_parts = []
    for field in STICKER_FIELDS:
        if field in columns:
            select_parts.append(f"st.{_quote_identifier(field)} AS {field}")
        else:
            select_parts.append(f"NULL AS {field}")
    order = "st.listing_id"
    if "sticker_index" in columns:
        order += ", st.sticker_index"
    sticker_rows = _rows(
        conn.execute(
            f"""
            SELECT {', '.join(select_parts)}
            FROM sale_stickers AS st
            WHERE st.listing_id IN ({_placeholders(listing_ids)})
            ORDER BY {order}
            """,
            tuple(listing_ids),
        )
    )
    grouped = {listing_id: [] for listing_id in listing_ids}
    for sticker in sticker_rows:
        grouped.setdefault(str(sticker.get("listing_id")), []).append(sticker)
    for row in rows:
        row["stickers"] = grouped.get(str(row.get("listing_id")), [])


def _empty_payload(
    *,
    offset: int,
    limit: int,
    query: str,
    scope: str,
) -> dict[str, Any]:
    return {
        "offset": offset,
        "limit": limit,
        "total": 0,
        "rows": [],
        "search": query,
        "scope": scope,
        "strategy": "empty",
        "resolved": {
            "skin_name_count": 0,
            "sticker_name_count": 0,
            "listing_match_mode": None,
            "skin_names_preview": [],
            "sticker_names_preview": [],
        },
    }


def fetch_listings_page(
    conn: sqlite3.Connection,
    offset: int,
    limit: int,
    search: str,
    scope: str = "skin",
) -> dict[str, Any]:
    """Return one bounded page of listings using an explicit search scope.

    ``skin`` resolves item names through ``cs2_item_catalog`` (with a
    distinct-name fallback), ``sticker`` resolves sticker names once and then
    uses exact indexed lookups, ``listing`` performs a primary-key exact/prefix
    lookup, and ``all`` combines only bounded branches.  A blank query always
    returns the newest listings irrespective of scope.
    """

    offset, limit, query, scope = _validate_request(offset, limit, search, scope)
    sales_columns = _table_columns(conn, "sales")
    if "listing_id" not in sales_columns:
        return _empty_payload(
            offset=offset,
            limit=limit,
            query=query,
            scope=scope,
        )

    resolved: dict[str, Any] = {
        "skin_name_count": 0,
        "sticker_name_count": 0,
        "listing_match_mode": None,
        "skin_names_preview": [],
        "sticker_names_preview": [],
    }

    if not query:
        total, rows, strategy = _fetch_unfiltered_rows(
            conn,
            sales_columns=sales_columns,
            offset=offset,
            limit=limit,
        )
    elif scope == "skin":
        names = _resolve_skin_names(conn, query)
        resolved["skin_name_count"] = len(names)
        resolved["skin_names_preview"] = names[:RESOLVED_PREVIEW_LIMIT]
        branch = _skin_branch(conn, names)
        if branch is None:
            total, rows, strategy = 0, [], "empty"
        else:
            total = branch.count
            rows, strategy = _fetch_branch_rows(
                conn,
                branch,
                sales_columns=sales_columns,
                offset=offset,
                limit=limit,
            )
    elif scope == "sticker":
        names = _resolve_sticker_names(conn, query)
        resolved["sticker_name_count"] = len(names)
        resolved["sticker_names_preview"] = names[:RESOLVED_PREVIEW_LIMIT]
        branch = _sticker_branch(conn, names)
        if branch is None:
            total, rows, strategy = 0, [], "empty"
        else:
            total = branch.count
            rows, strategy = _fetch_branch_rows(
                conn,
                branch,
                sales_columns=sales_columns,
                offset=offset,
                limit=limit,
            )
    elif scope == "listing":
        branch = _listing_branch(conn, query, allow_short=True)
        if branch is None:
            total, rows, strategy = 0, [], "empty"
        else:
            resolved["listing_match_mode"] = branch.match_mode
            total = branch.count
            rows, strategy = _fetch_branch_rows(
                conn,
                branch,
                sales_columns=sales_columns,
                offset=offset,
                limit=limit,
            )
    else:
        skin_names = _resolve_skin_names(conn, query)
        sticker_names = _resolve_sticker_names(conn, query)
        if len(skin_names) + len(sticker_names) > MAX_ALL_RESOLVED_NAMES:
            raise SearchTooBroadError(
                "all-scope name resolution is too broad; choose skin or sticker"
            )
        resolved["skin_name_count"] = len(skin_names)
        resolved["sticker_name_count"] = len(sticker_names)
        resolved["skin_names_preview"] = skin_names[:RESOLVED_PREVIEW_LIMIT]
        resolved["sticker_names_preview"] = sticker_names[:RESOLVED_PREVIEW_LIMIT]
        branches = [
            branch
            for branch in (
                _skin_branch(conn, skin_names),
                _sticker_branch(conn, sticker_names),
                _listing_branch(conn, query, allow_short=False),
            )
            if branch is not None
        ]
        listing_branch = next(
            (branch for branch in branches if branch.kind == "listing"),
            None,
        )
        if listing_branch:
            resolved["listing_match_mode"] = listing_branch.match_mode
        total, rows, strategy = _fetch_all_rows(
            conn,
            branches,
            sales_columns=sales_columns,
            offset=offset,
            limit=limit,
        )

    _attach_stickers(conn, rows)
    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "rows": rows,
        "search": query,
        "scope": scope,
        "strategy": strategy,
        "resolved": resolved,
    }


__all__ = [
    "ALLOWED_SCOPES",
    "ListingSearchError",
    "SearchTooBroadError",
    "fetch_listings_page",
]
