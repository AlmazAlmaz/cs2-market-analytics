"""Bounded, local-only catalogue lookup for account inventory items.

The account UI needs canonical Steam market names for more than float-bearing
skins.  This module combines three local sources without making network calls
from search or lookup code:

* ``cs2_item_catalog`` for weapon skins, knives and gloves;
* ``steam_market_assets`` for authoritative Steam icons and observed stickers;
* an optional local ByMykel ``crates.json`` cache for cases, capsules and other
  containers.  The already cached grouped-skins feed is also understood as a
  limited fallback for cases and souvenir packages referenced by skins.

``refresh_container_cache`` is the only network-capable function.  It is an
explicit maintenance operation, is restricted to the allow-listed HTTPS raw
GitHub feed, validates and shrinks the response, then atomically publishes it.
Normal request handlers should call only :func:`search_items` and
:func:`lookup_item`.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent
CONTAINER_SOURCE_URL = (
    "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/"
    "public/api/en/crates.json"
)
CONTAINER_SOURCE_ATTRIBUTION = {
    "project": "ByMykel/CSGO-API",
    "repository_url": "https://github.com/ByMykel/CSGO-API",
    "license": "MIT",
    "license_url": "https://github.com/ByMykel/CSGO-API/blob/main/LICENSE",
    "dataset_url": CONTAINER_SOURCE_URL,
}

DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 50
MAX_QUERY_LENGTH = 180
MAX_MARKET_HASH_NAME_LENGTH = 512
MAX_SOURCE_CANDIDATES = 250
MAX_LOCAL_CACHE_BYTES = 16 * 1024 * 1024
MAX_LOCAL_CACHE_ROWS = 100_000
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
MIN_REFRESH_ROWS = 100
MAX_REFRESH_ROWS = 2_000
DOWNLOAD_CHUNK_BYTES = 128 * 1024
MIN_REFRESH_TIMEOUT_SECONDS = 1
MAX_REFRESH_TIMEOUT_SECONDS = 60

_CACHE_DIR = PROJECT_DIR / ".private" / "item_catalog_cache"
DEFAULT_CONTAINER_CACHE_PATHS = (
    _CACHE_DIR / "bymykel_crates.json",
    _CACHE_DIR / "crates.json",
    _CACHE_DIR / "containers.json",
    _CACHE_DIR / "bymykel_crates.sqlite",
    _CACHE_DIR / "containers.sqlite",
    # This file exists in the current project.  It contains only container
    # references attached to skins, so it is useful but deliberately treated
    # as a lower-priority, incomplete fallback.
    _CACHE_DIR / "bymykel_skins_grouped.json",
)

_CONTAINER_SQLITE_TABLES = (
    "inventory_item_catalog",
    "cs2_container_catalog",
    "container_catalog",
    "containers",
    "crates",
)
_CONTAINER_KIND_MARKERS = (
    "case",
    "capsule",
    "crate",
    "container",
    "souvenir",
    "graffiti",
    "music kit box",
    "pins",
    "package",
)


class ContainerCacheError(RuntimeError):
    """Base error for an explicit container-cache refresh failure."""


class ContainerCacheDownloadError(ContainerCacheError):
    """The allow-listed upstream response failed bounded transport checks."""


class ContainerCacheValidationError(ContainerCacheError):
    """Downloaded JSON did not contain a safe canonical container catalogue."""


@dataclass(frozen=True)
class _Candidate:
    market_hash_name: str
    item_kind: str
    icon_url: str | None
    source_priority: int
    icon_priority: int


def _bounded_int(
    value: Any, *, name: str, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    try:
        if not isinstance(value, int) and float(value) != result:
            raise ValueError(f"{name} must be an integer")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _clean_text(value: Any, *, maximum: int) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    if not text or len(text) > maximum:
        return None
    if any(ord(character) < 32 for character in text):
        return None
    return text


def _safe_icon_url(value: Any) -> str | None:
    text = _clean_text(value, maximum=8_192)
    if text is None:
        return None
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return text


def _infer_container_kind(name: str, raw_kind: Any = None) -> str:
    folded_name = name.casefold()
    folded_kind = str(raw_kind or "").strip().casefold().replace("_", " ")
    combined = f"{folded_kind} {folded_name}"
    if "capsule" in combined or folded_kind == "pins":
        return "capsule"
    if folded_kind == "case" or folded_name.endswith(" case"):
        return "case"
    if "souvenir" in folded_kind or "souvenir package" in folded_name:
        return "souvenir_package"
    return "container"


def _normalise_item_kind(raw_kind: Any, name: str) -> str:
    folded = str(raw_kind or "").strip().casefold().replace("-", "_").replace(
        " ", "_"
    )
    aliases = {
        "weapon_skin": "weapon_skin",
        "skin": "weapon_skin",
        "knife": "knife",
        "glove": "glove",
        "sticker": "sticker",
        "case": "case",
        "weapon_case": "case",
        "capsule": "capsule",
        "sticker_capsule": "capsule",
        "autograph_capsule": "capsule",
        "patch_capsule": "capsule",
        "souvenir": "souvenir_package",
        "souvenir_package": "souvenir_package",
        "container": "container",
        "crate": "container",
    }
    if folded in aliases:
        return aliases[folded]
    if any(marker in folded.replace("_", " ") for marker in _CONTAINER_KIND_MARKERS):
        return _infer_container_kind(name, raw_kind)
    return _infer_container_kind(name) if raw_kind is not None else "unknown"


def _match_rank(name: str, query: str) -> int | None:
    folded_name = name.casefold()
    folded_query = query.casefold()
    if not folded_query:
        return 1
    if folded_name == folded_query:
        return 0
    if folded_name.startswith(folded_query):
        return 1
    if folded_query in folded_name:
        return 2
    return None


def _escaped_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _as_existing_file(path: str | Path | None) -> Path | None:
    if path in (None, ""):
        return None
    try:
        resolved = Path(path).expanduser().resolve()
        return resolved if resolved.is_file() else None
    except (OSError, TypeError, ValueError):
        return None


def _open_read_only(path: str | Path | None) -> sqlite3.Connection | None:
    database = _as_existing_file(path)
    if database is None:
        return None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro",
            uri=True,
            timeout=3,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=3000")
        try:
            connection.execute("PRAGMA trusted_schema=OFF")
        except sqlite3.DatabaseError:
            pass
        return connection
    except (OSError, sqlite3.Error):
        if connection is not None:
            connection.close()
        return None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            return set()
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM pragma_table_info(?)", (table,)
            )
        }
    except sqlite3.Error:
        return set()


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_match_parts(query: str, *, exact_only: bool) -> tuple[str, tuple[Any, ...]]:
    if exact_only:
        return "= ? COLLATE NOCASE", (query,)
    escaped = _escaped_like(query)
    return "LIKE ? ESCAPE '\\' COLLATE NOCASE", (f"%{escaped}%",)


def _sql_order(column: str, query: str, *, exact_only: bool) -> tuple[str, list[Any]]:
    identifier = _quoted_identifier(column)
    if exact_only:
        return f"{identifier} COLLATE NOCASE, {identifier}", []
    prefix = f"{_escaped_like(query)}%"
    return (
        "CASE "
        f"WHEN {identifier} = ? COLLATE NOCASE THEN 0 "
        f"WHEN {identifier} LIKE ? ESCAPE '\\' COLLATE NOCASE THEN 1 "
        "ELSE 2 END, "
        f"{identifier} COLLATE NOCASE, {identifier}",
        [query, prefix],
    )


def _query_main_catalog(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    exact_only: bool,
) -> list[_Candidate]:
    columns = _table_columns(connection, "cs2_item_catalog")
    if not {"market_hash_name", "item_kind"}.issubset(columns):
        return []
    icon_expression = "image_url" if "image_url" in columns else "NULL"
    match_sql, match_params = _sql_match_parts(query, exact_only=exact_only)
    order_sql, order_params = _sql_order(
        "market_hash_name", query, exact_only=exact_only
    )
    try:
        rows = connection.execute(
            f"""
            SELECT market_hash_name, item_kind, {icon_expression} AS icon_url
            FROM cs2_item_catalog
            WHERE market_hash_name {match_sql}
            ORDER BY {order_sql}
            LIMIT ?
            """,
            (*match_params, *order_params, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    result: list[_Candidate] = []
    for row in rows:
        name = _clean_text(row["market_hash_name"], maximum=MAX_MARKET_HASH_NAME_LENGTH)
        if name is None:
            continue
        result.append(
            _Candidate(
                name,
                _normalise_item_kind(row["item_kind"], name),
                _safe_icon_url(row["icon_url"]),
                0,
                3,
            )
        )
    return result


def _query_steam_assets(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    exact_only: bool,
) -> list[_Candidate]:
    columns = _table_columns(connection, "steam_market_assets")
    if "market_hash_name" not in columns:
        return []
    kind_expression = "asset_kind" if "asset_kind" in columns else "NULL"
    icon_columns = [
        column for column in ("icon_url_large", "icon_url") if column in columns
    ]
    if icon_columns:
        icon_expression = "COALESCE(" + ", ".join(
            f"NULLIF(TRIM({_quoted_identifier(column)}), '')"
            for column in icon_columns
        ) + ")"
    else:
        icon_expression = "NULL"
    found_sql = "AND found=1" if "found" in columns else ""
    match_sql, match_params = _sql_match_parts(query, exact_only=exact_only)
    order_sql, order_params = _sql_order(
        "market_hash_name", query, exact_only=exact_only
    )
    try:
        rows = connection.execute(
            f"""
            SELECT market_hash_name, {kind_expression} AS item_kind,
                   {icon_expression} AS icon_url
            FROM steam_market_assets
            WHERE market_hash_name {match_sql} {found_sql}
            ORDER BY {order_sql}
            LIMIT ?
            """,
            (*match_params, *order_params, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    result: list[_Candidate] = []
    for row in rows:
        name = _clean_text(row["market_hash_name"], maximum=MAX_MARKET_HASH_NAME_LENGTH)
        if name is None:
            continue
        result.append(
            _Candidate(
                name,
                _normalise_item_kind(row["item_kind"], name),
                _safe_icon_url(row["icon_url"]),
                3,
                0,
            )
        )
    return result


def _query_linked_containers(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    exact_only: bool,
) -> list[_Candidate]:
    columns = _table_columns(connection, "cs2_item_catalog")
    if "crate_names_json" not in columns:
        return []
    match_sql, match_params = _sql_match_parts(query, exact_only=exact_only)
    order_sql, order_params = _sql_order(
        "market_hash_name", query, exact_only=exact_only
    )
    try:
        rows = connection.execute(
            f"""
            WITH valid_crates AS (
                SELECT DISTINCT CAST(j.value AS TEXT) AS market_hash_name
                FROM cs2_item_catalog AS c
                JOIN json_each(
                    CASE WHEN json_valid(c.crate_names_json)
                         THEN c.crate_names_json ELSE '[]' END
                ) AS j
                WHERE j.type='text'
            )
            SELECT market_hash_name
            FROM valid_crates
            WHERE market_hash_name {match_sql}
            ORDER BY {order_sql}
            LIMIT ?
            """,
            (*match_params, *order_params, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    result: list[_Candidate] = []
    for row in rows:
        name = _clean_text(row["market_hash_name"], maximum=MAX_MARKET_HASH_NAME_LENGTH)
        if name is not None:
            result.append(
                _Candidate(name, _infer_container_kind(name), None, 4, 9)
            )
    return result


def _database_candidates(
    database_path: str | Path | None,
    query: str,
    *,
    limit: int,
    exact_only: bool,
) -> list[_Candidate]:
    connection = _open_read_only(database_path)
    if connection is None:
        return []
    try:
        return [
            *_query_main_catalog(
                connection, query, limit=limit, exact_only=exact_only
            ),
            *_query_steam_assets(
                connection, query, limit=limit, exact_only=exact_only
            ),
            *_query_linked_containers(
                connection, query, limit=limit, exact_only=exact_only
            ),
        ]
    finally:
        connection.close()


def _mapping_icon(entry: dict[str, Any]) -> str | None:
    for key in ("icon_url_large", "icon_url", "image_url", "image"):
        icon = _safe_icon_url(entry.get(key))
        if icon is not None:
            return icon
    return None


def _looks_like_container(entry: dict[str, Any], *, assume_container: bool) -> bool:
    identifier = str(entry.get("id") or "").strip().casefold()
    if identifier.startswith("crate-"):
        return True
    raw_kind = entry.get("item_kind", entry.get("type"))
    folded_kind = str(raw_kind or "").strip().casefold().replace("_", " ")
    if any(marker in folded_kind for marker in _CONTAINER_KIND_MARKERS):
        return True
    return assume_container and "market_hash_name" in entry


def _candidate_from_container_mapping(
    entry: dict[str, Any],
    *,
    assume_container: bool,
    source_priority: int,
) -> _Candidate | None:
    if not _looks_like_container(entry, assume_container=assume_container):
        return None
    if "market_hash_name" in entry:
        # An explicit null means that Valve/Steam has no canonical market name
        # for this schema row yet.  Never silently turn ``name`` into one.
        name = _clean_text(
            entry.get("market_hash_name"), maximum=MAX_MARKET_HASH_NAME_LENGTH
        )
    else:
        # Nested ``crates`` references in the grouped-skins cache predate that
        # field, but their ``name`` values are the crate market names.
        name = _clean_text(entry.get("name"), maximum=MAX_MARKET_HASH_NAME_LENGTH)
    if name is None:
        return None
    raw_kind = entry.get("item_kind", entry.get("type"))
    kind = _infer_container_kind(name, raw_kind)
    return _Candidate(name, kind, _mapping_icon(entry), source_priority, 1)


def _top_level_json_entries(payload: Any) -> tuple[list[Any], bool]:
    if isinstance(payload, list):
        return payload, False
    if not isinstance(payload, dict):
        return [], False
    for key in ("crates", "containers", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value, key in {"crates", "containers"}
        if isinstance(value, dict):
            return list(value.values()), key in {"crates", "containers"}
    values = list(payload.values())
    if values and all(isinstance(value, dict) for value in values):
        return values, False
    return [], False


def _json_container_candidates(
    payload: Any, *, filename: str
) -> tuple[_Candidate, ...]:
    entries, object_marks_containers = _top_level_json_entries(payload)
    if len(entries) > MAX_LOCAL_CACHE_ROWS:
        return ()
    folded_filename = filename.casefold()
    grouped_skins = "skins_grouped" in folded_filename
    assume_container = object_marks_containers or (
        not grouped_skins
        and ("crate" in folded_filename or "container" in folded_filename)
    )
    source_priority = 2 if grouped_skins else 1
    result: list[_Candidate] = []
    nested_count = 0
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        entry = {str(key): value for key, value in raw_entry.items()}
        direct = _candidate_from_container_mapping(
            entry,
            assume_container=assume_container,
            source_priority=source_priority,
        )
        if direct is not None:
            result.append(direct)
        nested = entry.get("crates")
        if not isinstance(nested, list):
            continue
        for raw_container in nested:
            nested_count += 1
            if nested_count > MAX_LOCAL_CACHE_ROWS:
                return ()
            if not isinstance(raw_container, dict):
                continue
            container = _candidate_from_container_mapping(
                {str(key): value for key, value in raw_container.items()},
                assume_container=True,
                source_priority=2,
            )
            if container is not None:
                result.append(container)
    return tuple(result)


@lru_cache(maxsize=12)
def _load_json_candidates_cached(
    resolved_path: str, mtime_ns: int, size: int
) -> tuple[_Candidate, ...]:
    del mtime_ns  # These values intentionally form the cache invalidation key.
    if not 0 < size <= MAX_LOCAL_CACHE_BYTES:
        return ()
    try:
        raw = Path(resolved_path).read_bytes()
        if len(raw) != size or len(raw) > MAX_LOCAL_CACHE_BYTES:
            return ()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return ()
    return _json_container_candidates(payload, filename=Path(resolved_path).name)


def _load_json_candidates(path: Path) -> tuple[_Candidate, ...]:
    try:
        stat = path.stat()
        if not path.is_file():
            return ()
        return _load_json_candidates_cached(
            str(path.resolve()), stat.st_mtime_ns, stat.st_size
        )
    except (OSError, ValueError):
        return ()


def _first_column(columns: set[str], names: tuple[str, ...]) -> str | None:
    return next((name for name in names if name in columns), None)


def _query_container_sqlite(
    path: Path,
    query: str,
    *,
    limit: int,
    exact_only: bool,
) -> list[_Candidate]:
    connection = _open_read_only(path)
    if connection is None:
        return []
    result: list[_Candidate] = []
    try:
        for table in _CONTAINER_SQLITE_TABLES:
            columns = _table_columns(connection, table)
            if "market_hash_name" not in columns:
                continue
            kind_column = _first_column(
                columns, ("item_kind", "container_type", "type", "asset_kind")
            )
            icon_columns = [
                column
                for column in ("icon_url_large", "icon_url", "image_url", "image")
                if column in columns
            ]
            kind_expression = _quoted_identifier(kind_column) if kind_column else "NULL"
            if icon_columns:
                icon_expression = "COALESCE(" + ", ".join(
                    f"NULLIF(TRIM({_quoted_identifier(column)}), '')"
                    for column in icon_columns
                ) + ")"
            else:
                icon_expression = "NULL"
            found_sql = "AND found=1" if "found" in columns else ""
            match_sql, match_params = _sql_match_parts(query, exact_only=exact_only)
            order_sql, order_params = _sql_order(
                "market_hash_name", query, exact_only=exact_only
            )
            try:
                rows = connection.execute(
                    f"""
                    SELECT market_hash_name, {kind_expression} AS item_kind,
                           {icon_expression} AS icon_url
                    FROM {_quoted_identifier(table)}
                    WHERE market_hash_name {match_sql} {found_sql}
                    ORDER BY {order_sql}
                    LIMIT ?
                    """,
                    (*match_params, *order_params, limit),
                ).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                name = _clean_text(
                    row["market_hash_name"], maximum=MAX_MARKET_HASH_NAME_LENGTH
                )
                if name is None:
                    continue
                result.append(
                    _Candidate(
                        name,
                        _infer_container_kind(name, row["item_kind"]),
                        _safe_icon_url(row["icon_url"]),
                        1,
                        1,
                    )
                )
    finally:
        connection.close()
    return result


def _container_paths(
    container_cache_path: str | Path | None,
) -> tuple[Path, ...]:
    if container_cache_path is None:
        candidates = DEFAULT_CONTAINER_CACHE_PATHS
    else:
        try:
            supplied = Path(container_cache_path).expanduser()
        except (TypeError, ValueError):
            return ()
        if supplied.is_dir():
            candidates = tuple(
                supplied / path.name for path in DEFAULT_CONTAINER_CACHE_PATHS
            )
        else:
            candidates = (supplied,)
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return tuple(result)


def _local_container_candidates(
    container_cache_path: str | Path | None,
    query: str,
    *,
    limit: int,
    exact_only: bool,
) -> list[_Candidate]:
    result: list[_Candidate] = []
    for path in _container_paths(container_cache_path):
        if not path.is_file():
            continue
        if path.suffix.casefold() in {".sqlite", ".sqlite3", ".db"}:
            result.extend(
                _query_container_sqlite(
                    path, query, limit=limit, exact_only=exact_only
                )
            )
            continue
        for candidate in _load_json_candidates(path):
            rank = _match_rank(candidate.market_hash_name, query)
            if rank is None or (exact_only and rank != 0):
                continue
            result.append(candidate)
    return result


def _kind_specificity(kind: str) -> int:
    return 0 if kind in {"unknown", "container"} else 1


def _merge_candidates(candidates: list[_Candidate]) -> list[_Candidate]:
    merged: dict[str, _Candidate] = {}
    for candidate in candidates:
        key = candidate.market_hash_name.casefold()
        current = merged.get(key)
        if current is None:
            merged[key] = candidate
            continue
        canonical = (
            candidate
            if candidate.source_priority < current.source_priority
            else current
        )
        kind = canonical.item_kind
        if _kind_specificity(candidate.item_kind) > _kind_specificity(kind):
            kind = candidate.item_kind
        if _kind_specificity(current.item_kind) > _kind_specificity(kind):
            kind = current.item_kind
        icon_source = min(
            (item for item in (current, candidate) if item.icon_url is not None),
            key=lambda item: item.icon_priority,
            default=None,
        )
        merged[key] = _Candidate(
            canonical.market_hash_name,
            kind,
            icon_source.icon_url if icon_source is not None else None,
            min(current.source_priority, candidate.source_priority),
            icon_source.icon_priority if icon_source is not None else 9,
        )
    return list(merged.values())


def _public_item(candidate: _Candidate) -> dict[str, Any]:
    return {
        "market_hash_name": candidate.market_hash_name,
        "item_kind": candidate.item_kind,
        "icon_url": candidate.icon_url,
    }


def _validate_query(query: Any) -> str:
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    value = query.strip()
    if len(value) > MAX_QUERY_LENGTH:
        raise ValueError(f"query may contain at most {MAX_QUERY_LENGTH} characters")
    if any(ord(character) < 32 for character in value):
        raise ValueError("query contains control characters")
    return value


def search_items(
    database_path: str | Path | None,
    query: str = "",
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    container_cache_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Search the unified local catalogue with exact/prefix/contains ranking.

    ``limit`` and the per-source SQL candidate count are hard bounded.  Missing,
    partial, locked or corrupt local sources contribute no rows; they are never
    created or repaired here.  This function never accesses the network.
    """

    query_value = _validate_query(query)
    limit_value = _bounded_int(
        limit, name="limit", minimum=1, maximum=MAX_SEARCH_LIMIT
    )
    source_limit = min(
        MAX_SOURCE_CANDIDATES, max(limit_value * 4, DEFAULT_SEARCH_LIMIT)
    )
    candidates = _database_candidates(
        database_path,
        query_value,
        limit=source_limit,
        exact_only=False,
    )
    candidates.extend(
        _local_container_candidates(
            container_cache_path,
            query_value,
            limit=source_limit,
            exact_only=False,
        )
    )
    ranked = [
        (_match_rank(candidate.market_hash_name, query_value), candidate)
        for candidate in _merge_candidates(candidates)
    ]
    ranked = [(rank, candidate) for rank, candidate in ranked if rank is not None]
    ranked.sort(
        key=lambda pair: (
            pair[0],
            pair[1].market_hash_name.casefold(),
            pair[1].market_hash_name,
        )
    )
    return [_public_item(candidate) for _, candidate in ranked[:limit_value]]


def lookup_item(
    database_path: str | Path | None,
    market_hash_name: str,
    *,
    container_cache_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return one case-insensitive exact canonical-name match, or ``None``."""

    if not isinstance(market_hash_name, str):
        raise TypeError("market_hash_name must be a string")
    requested = market_hash_name.strip()
    if not requested or len(requested) > MAX_MARKET_HASH_NAME_LENGTH:
        raise ValueError(
            "market_hash_name must contain between 1 and "
            f"{MAX_MARKET_HASH_NAME_LENGTH} characters"
        )
    if any(ord(character) < 32 for character in requested):
        raise ValueError("market_hash_name contains control characters")
    candidates = _database_candidates(
        database_path,
        requested,
        limit=MAX_SEARCH_LIMIT,
        exact_only=True,
    )
    candidates.extend(
        _local_container_candidates(
            container_cache_path,
            requested,
            limit=MAX_SEARCH_LIMIT,
            exact_only=True,
        )
    )
    exact = [
        candidate
        for candidate in _merge_candidates(candidates)
        if candidate.market_hash_name.casefold() == requested.casefold()
    ]
    if not exact:
        return None
    exact.sort(
        key=lambda candidate: (
            candidate.source_priority,
            candidate.market_hash_name.casefold(),
            candidate.market_hash_name,
        )
    )
    return _public_item(exact[0])


def _validate_container_source_url(source_url: Any) -> str:
    text = _clean_text(source_url, maximum=2_048)
    if text is None:
        raise ContainerCacheDownloadError("container source URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ContainerCacheDownloadError("container source URL is invalid") from exc
    expected = urllib.parse.urlsplit(CONTAINER_SOURCE_URL)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "raw.githubusercontent.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != expected.path
        or parsed.query
        or parsed.fragment
    ):
        raise ContainerCacheDownloadError(
            "only the allow-listed ByMykel HTTPS crates.json feed is permitted"
        )
    return text


def _download_container_payload(source_url: str, *, timeout: int) -> bytes:
    request = urllib.request.Request(
        source_url,
        headers={
            "Accept": "application/json,text/plain;q=0.9",
            "Accept-Encoding": "identity",
            "User-Agent": "CS2-inventory-catalog/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url_getter = getattr(response, "geturl", None)
            final_url = final_url_getter() if callable(final_url_getter) else source_url
            _validate_container_source_url(final_url)
            status = getattr(response, "status", 200)
            if status != 200:
                raise ContainerCacheDownloadError(
                    f"container source returned HTTP {status}"
                )
            content_length = response.headers.get("Content-Length")
            if content_length not in (None, ""):
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise ContainerCacheDownloadError(
                        "container source sent an invalid Content-Length"
                    ) from exc
                if declared_length < 1 or declared_length > MAX_DOWNLOAD_BYTES:
                    raise ContainerCacheDownloadError(
                        "container source exceeds the download size limit"
                    )
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise ContainerCacheDownloadError(
                        "container source exceeds the download size limit"
                    )
                chunks.append(chunk)
    except ContainerCacheError:
        raise
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise ContainerCacheDownloadError(
            "could not download the container catalogue"
        ) from exc
    payload = b"".join(chunks)
    if not payload:
        raise ContainerCacheDownloadError("container source returned an empty body")
    return payload


def _normalise_refreshed_containers(payload: bytes) -> list[dict[str, Any]]:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContainerCacheValidationError(
            "container source is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(decoded, list):
        raise ContainerCacheValidationError("container source must be a JSON array")
    if not MIN_REFRESH_ROWS <= len(decoded) <= MAX_REFRESH_ROWS:
        raise ContainerCacheValidationError(
            f"container source must contain between {MIN_REFRESH_ROWS} and "
            f"{MAX_REFRESH_ROWS} rows"
        )
    normalised: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_entry in decoded:
        if not isinstance(raw_entry, dict):
            raise ContainerCacheValidationError(
                "every container source row must be an object"
            )
        # Only the explicit schema field is authoritative.  In particular,
        # upcoming/non-marketable rows often have a display ``name`` but a
        # null ``market_hash_name`` and must not enter inventory autocomplete.
        name = _clean_text(
            raw_entry.get("market_hash_name"), maximum=MAX_MARKET_HASH_NAME_LENGTH
        )
        if name is None:
            continue
        key = name.casefold()
        if key in seen:
            raise ContainerCacheValidationError(
                f"duplicate canonical market_hash_name: {name}"
            )
        seen.add(key)
        icon_url = _safe_icon_url(raw_entry.get("image"))
        if icon_url is None:
            raise ContainerCacheValidationError(
                f"container row has no safe HTTPS image: {name}"
            )
        normalised.append(
            {
                "market_hash_name": name,
                "item_kind": _infer_container_kind(name, raw_entry.get("type")),
                "icon_url": icon_url,
            }
        )
    if not MIN_REFRESH_ROWS <= len(normalised) <= MAX_REFRESH_ROWS:
        raise ContainerCacheValidationError(
            "too few canonical market_hash_name rows after validation"
        )
    normalised.sort(
        key=lambda item: (
            str(item["market_hash_name"]).casefold(),
            str(item["market_hash_name"]),
        )
    )
    return normalised


def _atomic_write_cache(path: Path, payload: bytes) -> None:
    try:
        parent = path.parent.expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / path.name
        if target.is_symlink():
            raise ContainerCacheError("refusing to replace a symlink cache path")
        if target.exists() and not target.is_file():
            raise ContainerCacheError("container cache path is not a regular file")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=parent
        )
    except (OSError, ValueError) as exc:
        raise ContainerCacheError("could not prepare the container cache path") from exc
    temporary = Path(temporary_name)
    try:
        try:
            getattr(os, "fchmod", lambda _descriptor, _mode: None)(descriptor, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ContainerCacheError("could not publish the container cache") from exc


def refresh_container_cache(
    cache_path: str | Path,
    *,
    source_url: str = CONTAINER_SOURCE_URL,
    timeout: int = 20,
) -> dict[str, Any]:
    """Explicitly refresh a sanitized local container cache.

    The URL is allow-listed, redirects are revalidated, response bytes and row
    counts are bounded, and the old cache survives every validation/download
    failure.  Search and lookup never call this function implicitly.
    """

    validated_url = _validate_container_source_url(source_url)
    timeout_value = _bounded_int(
        timeout,
        name="timeout",
        minimum=MIN_REFRESH_TIMEOUT_SECONDS,
        maximum=MAX_REFRESH_TIMEOUT_SECONDS,
    )
    try:
        target = Path(cache_path).expanduser()
    except (TypeError, ValueError) as exc:
        raise ContainerCacheError("container cache path is invalid") from exc
    if not target.name:
        raise ContainerCacheError("container cache path must name a file")
    raw_payload = _download_container_payload(validated_url, timeout=timeout_value)
    rows = _normalise_refreshed_containers(raw_payload)
    published = (
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    _atomic_write_cache(target, published)
    return {
        "path": str(target.resolve()),
        "source_url": validated_url,
        "row_count": len(rows),
        "sha256": hashlib.sha256(published).hexdigest(),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "attribution": dict(CONTAINER_SOURCE_ATTRIBUTION),
    }


# Descriptive aliases for callers that prefer the inventory/catalogue wording.
search_inventory_items = search_items
lookup_inventory_item = lookup_item


__all__ = [
    "CONTAINER_SOURCE_ATTRIBUTION",
    "CONTAINER_SOURCE_URL",
    "ContainerCacheDownloadError",
    "ContainerCacheError",
    "ContainerCacheValidationError",
    "lookup_inventory_item",
    "lookup_item",
    "refresh_container_cache",
    "search_inventory_items",
    "search_items",
]
