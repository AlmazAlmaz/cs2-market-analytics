"""Stable item-kind routing shared by parsing, training, and inference.

The synchronized catalog is authoritative.  Name heuristics exist only so a
fresh install and old databases fail into a sensible, conservative category
before the first catalog refresh.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable

ITEM_KIND_KNIFE = "knife"
ITEM_KIND_WEAPON_SKIN = "weapon_skin"
ITEM_KIND_GLOVE = "glove"
ITEM_KIND_UNKNOWN = "unknown"
STRICT_ITEM_KIND_POLICY = "strict_catalog_or_known_name_v1"

CATALOG_ITEM_KINDS = frozenset(
    {ITEM_KIND_KNIFE, ITEM_KIND_WEAPON_SKIN, ITEM_KIND_GLOVE}
)

_GLOVE_MARKERS = (
    "bloodhound gloves",
    "broken fang gloves",
    "driver gloves",
    "hand wraps",
    "hydra gloves",
    "moto gloves",
    "specialist gloves",
    "sport gloves",
)

_WEAPON_NAMES = frozenset(
    {
        "ak-47",
        "aug",
        "awp",
        "cz75-auto",
        "desert eagle",
        "dual berettas",
        "famas",
        "five-seven",
        "g3sg1",
        "galil ar",
        "glock-18",
        "m249",
        "m4a1-s",
        "m4a4",
        "mac-10",
        "mag-7",
        "mp5-sd",
        "mp7",
        "mp9",
        "negev",
        "nova",
        "p2000",
        "p250",
        "p90",
        "pp-bizon",
        "r8 revolver",
        "sawed-off",
        "scar-20",
        "sg 553",
        "ssg 08",
        "tec-9",
        "ump-45",
        "usp-s",
        "xm1014",
        "zeus x27",
    }
)


def _normalized_name(value: object) -> str:
    return str(value or "").strip()


def classify_name_fallback(market_hash_name: object) -> str:
    """Classify an exact market name without trusting broad substring matches."""

    name = _normalized_name(market_hash_name)
    if not name:
        return ITEM_KIND_UNKNOWN

    folded = name.casefold()
    if folded.startswith("stattrak™ "):
        folded = folded[len("stattrak™ ") :].lstrip()
    elif folded.startswith("stattrak "):
        folded = folded[len("stattrak ") :].lstrip()

    if folded.startswith("★"):
        without_star = folded[1:].lstrip()
        if any(marker in without_star for marker in _GLOVE_MARKERS):
            return ITEM_KIND_GLOVE
        return ITEM_KIND_KNIFE

    # Paintable weapon market names consistently include the weapon/finish
    # separator.  Other CS2 commodities (stickers, cases, agents) must not be
    # silently routed into the price model.
    if (
        " | " in folded
        and folded.split(" | ", 1)[0].strip() in _WEAPON_NAMES
    ):
        return ITEM_KIND_WEAPON_SKIN
    return ITEM_KIND_UNKNOWN


def catalog_table_available(conn: sqlite3.Connection) -> bool:
    try:
        return bool(
            conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'cs2_item_catalog'
                """
            ).fetchone()
        )
    except sqlite3.Error:
        return False


def classify_market_hash_name(
    market_hash_name: object,
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Return catalog-backed ``knife``/``weapon_skin``/``glove``/``unknown``."""

    name = _normalized_name(market_hash_name)
    if conn is not None and name and catalog_table_available(conn):
        try:
            row = conn.execute(
                """
                SELECT item_kind
                FROM cs2_item_catalog
                WHERE market_hash_name = ?
                """,
                (name,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0] in CATALOG_ITEM_KINDS:
            return str(row[0])
    return classify_name_fallback(name)


def names_for_item_kind(
    conn: sqlite3.Connection,
    item_kind: str,
) -> set[str]:
    """Read exact catalog names for one trainable kind, or return an empty set."""

    if item_kind not in CATALOG_ITEM_KINDS:
        raise ValueError(f"unsupported item kind: {item_kind!r}")
    if not catalog_table_available(conn):
        return set()
    try:
        rows: Iterable[tuple[object]] = conn.execute(
            """
            SELECT market_hash_name
            FROM cs2_item_catalog
            WHERE item_kind = ?
            """,
            (item_kind,),
        )
        return {
            str(row[0]).strip()
            for row in rows
            if row and str(row[0] or "").strip()
        }
    except sqlite3.Error:
        return set()


def sql_item_kind_expression(
    conn: sqlite3.Connection,
    *,
    name_expression: str = "name",
    unknown_as_weapon_skin: bool = False,
) -> tuple[str, tuple[object, ...]]:
    """Build a parameterized SQLite expression matching the Python router.

    ``name_expression`` is deliberately restricted to an internal SQL
    identifier (optionally qualified) so callers cannot turn this helper into
    a query-construction injection surface.
    """

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", name_expression):
        raise ValueError("name_expression must be a simple SQL identifier")

    normalized = f"LOWER(TRIM(COALESCE({name_expression}, '')))"
    without_prefix = (
        f"CASE WHEN {normalized} LIKE 'stattrak™ %' "
        f"THEN LTRIM(SUBSTR({normalized}, LENGTH('stattrak™ ') + 1)) "
        f"WHEN {normalized} LIKE 'stattrak %' "
        f"THEN LTRIM(SUBSTR({normalized}, LENGTH('stattrak ') + 1)) "
        f"ELSE {normalized} END"
    )
    glove_checks = " OR ".join(
        f"{without_prefix} LIKE ?" for _marker in _GLOVE_MARKERS
    )
    fallback_unknown = (
        ITEM_KIND_WEAPON_SKIN if unknown_as_weapon_skin else ITEM_KIND_UNKNOWN
    )
    weapon_clause = ""
    weapon_parameters: tuple[object, ...] = ()
    if not unknown_as_weapon_skin:
        weapon_placeholders = ", ".join("?" for _name in sorted(_WEAPON_NAMES))
        weapon_clause = (
            f"WHEN INSTR({without_prefix}, ' | ') > 0 "
            f"AND SUBSTR({without_prefix}, 1, INSTR({without_prefix}, ' | ') - 1) "
            f"IN ({weapon_placeholders}) THEN '{ITEM_KIND_WEAPON_SKIN}' "
        )
        weapon_parameters = tuple(sorted(_WEAPON_NAMES))
    fallback = (
        "CASE "
        f"WHEN {without_prefix} LIKE '★%' AND ({glove_checks}) "
        f"THEN '{ITEM_KIND_GLOVE}' "
        f"WHEN {without_prefix} LIKE '★%' THEN '{ITEM_KIND_KNIFE}' "
        f"{weapon_clause}"
        f"ELSE '{fallback_unknown}' END"
    )
    parameters: tuple[object, ...] = tuple(
        f"%{marker}%" for marker in _GLOVE_MARKERS
    ) + weapon_parameters

    if not catalog_table_available(conn):
        return fallback, parameters

    expression = (
        "COALESCE((SELECT CASE WHEN catalog.item_kind IN "
        f"('{ITEM_KIND_KNIFE}', '{ITEM_KIND_WEAPON_SKIN}', '{ITEM_KIND_GLOVE}') "
        "THEN catalog.item_kind END FROM cs2_item_catalog AS catalog "
        f"WHERE catalog.market_hash_name = {name_expression}), {fallback})"
    )
    return expression, parameters
