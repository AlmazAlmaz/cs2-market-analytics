"""Build one Igor row per sale from a single immutable SQLite read snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from item_classification import (
    ITEM_KIND_WEAPON_SKIN,
    STRICT_ITEM_KIND_POLICY,
    sql_item_kind_expression,
)

try:
    from . import feature_contract as contract
except ImportError:  # Direct execution from IGORMODEL/
    import feature_contract as contract


SCRIPT_DIR = Path(__file__).parent
PROJECT_DB_PATH = SCRIPT_DIR.parent / "csfloat_history.db"
LOCAL_DB_PATH = SCRIPT_DIR / "csfloat_history.db"
_LEGACY_DEFAULT_DB_PATH = (
    PROJECT_DB_PATH if PROJECT_DB_PATH.exists() else LOCAL_DB_PATH
)
# The dashboard training worker points this at a per-job immutable SQLite
# backup.  Normal CLI/runtime behaviour remains unchanged when the variable is
# absent.
_CONFIGURED_DB_PATH = os.environ.get("CS2_DB_PATH", "").strip()
DB_PATH = (
    Path(_CONFIGURED_DB_PATH).expanduser().resolve()
    if _CONFIGURED_DB_PATH
    else _LEGACY_DEFAULT_DB_PATH
)

MAX_STICKER_SLOTS = contract.MAX_STICKER_SLOTS
MAX_KEYCHAIN_SLOTS = contract.MAX_KEYCHAIN_SLOTS

SALES_COLUMNS = """
    listing_id, name, base_skin_name, wear_name, price_cents,
    reference_base_price, reference_predicted_price, reference_float_factor,
    reference_quantity, reference_keychain_price, reference_last_updated,
    float_value, paint_seed, paint_index, state, created_at, sold_at, fetched_at,
    sale_type, is_seller, is_watchlisted, watchers,
    item_def_index, item_rarity_name, item_collection_name,
    item_is_stattrak, item_is_souvenir
"""
SALES_COLUMN_NAMES = tuple(
    column.strip() for column in SALES_COLUMNS.replace("\n", " ").split(",")
)
STICKER_COLUMN_NAMES = (
    "listing_id",
    "sticker_index",
    "sticker_id",
    "slot",
    "name",
    "wear",
    "price_cents",
    "reference_price_cents",
    "reference_updated_at",
)
KEYCHAIN_COLUMN_NAMES = (
    "listing_id",
    "keychain_index",
    "keychain_id",
    "slot",
    "name",
    "pattern",
    "price_cents",
    "reference_quantity",
    "reference_updated_at",
)
SOURCE_CONTENT_FINGERPRINT_POLICY = (
    "sha256_weapon_skin_rows_and_children_ordered_primary_key_snapshot_v2"
)


def _readonly_uri(path: Path) -> str:
    return f"{Path(path).resolve().as_uri()}?mode=ro"


@contextmanager
def open_read_snapshot(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    """Hold one WAL-aware read transaction across every source table."""

    conn = sqlite3.connect(_readonly_uri(Path(db_path)), uri=True, timeout=30)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN")
        # Materialize the snapshot now, before pandas starts several queries.
        conn.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _weapon_skin_kind_clause(
    conn: sqlite3.Connection,
    *,
    name_expression: str,
) -> tuple[str, tuple[object, ...]]:
    """Return the exact, fail-closed weapon-skin predicate for one sales alias."""

    expression, parameters = sql_item_kind_expression(
        conn,
        name_expression=name_expression,
        unknown_as_weapon_skin=False,
    )
    return f"({expression}) = ?", (*parameters, ITEM_KIND_WEAPON_SKIN)


def _eligible_weapon_skin_sales_clause(
    conn: sqlite3.Connection,
    *,
    sales_alias: str,
) -> tuple[str, tuple[object, ...]]:
    sales_columns = {row[1] for row in conn.execute("PRAGMA table_info(sales)")}
    if not {"listing_id", "state", "price_cents", "name"}.issubset(sales_columns):
        return "0 = 1", ()
    kind_clause, parameters = _weapon_skin_kind_clause(
        conn,
        name_expression=f"{sales_alias}.name",
    )
    return (
        (
            f"{sales_alias}.state = 'sold' "
            f"AND {sales_alias}.price_cents IS NOT NULL "
            f"AND {sales_alias}.price_cents > 0 "
            f"AND {sales_alias}.name IS NOT NULL "
            f"AND {kind_clause}"
        ),
        parameters,
    )


def _eligible_weapon_child_clause(
    conn: sqlite3.Connection,
    *,
    child_alias: str,
) -> tuple[str, tuple[object, ...]]:
    sales_clause, parameters = _eligible_weapon_skin_sales_clause(
        conn,
        sales_alias="source_sales",
    )
    return (
        (
            "EXISTS (SELECT 1 FROM sales AS source_sales "
            f"WHERE source_sales.listing_id = {child_alias}.listing_id "
            f"AND {sales_clause})"
        ),
        parameters,
    )


def snapshot_fingerprint(conn: sqlite3.Connection, db_path: Path = DB_PATH) -> dict:
    sales_columns = {row[1] for row in conn.execute("PRAGMA table_info(sales)")}
    if {"listing_id", "name", "state", "price_cents"}.issubset(sales_columns):
        item_kind_where, item_kind_query_parameters = _eligible_weapon_skin_sales_clause(
            conn,
            sales_alias="sales",
        )
    else:
        # A malformed/legacy source without names cannot prove item identity.
        item_kind_where = "0 = 1"
        item_kind_query_parameters = ()
    valid_expression = "COUNT(*)"
    total_sales_rows = int(conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0])
    sales = conn.execute(
        f"""
        SELECT COUNT(*), COUNT(DISTINCT listing_id), MIN(sold_at), MAX(sold_at),
               MAX(fetched_at), {valid_expression}
        FROM sales
        WHERE {item_kind_where}
        """,
        item_kind_query_parameters,
    ).fetchone()
    sticker_clause, sticker_parameters = _eligible_weapon_child_clause(
        conn,
        child_alias="sale_stickers",
    )
    stickers = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT listing_id) FROM sale_stickers "
        f"WHERE {sticker_clause}",
        sticker_parameters,
    ).fetchone()
    has_keychains = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='sale_keychains'"
    ).fetchone()
    if has_keychains:
        keychain_clause, keychain_parameters = _eligible_weapon_child_clause(
            conn,
            child_alias="sale_keychains",
        )
        keychains = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT listing_id) FROM sale_keychains "
            f"WHERE {keychain_clause}",
            keychain_parameters,
        ).fetchone()
    else:
        keychains = (0, 0)
    value = {
        "resolved_path": str(Path(db_path).resolve()),
        "database_sales_rows": total_sales_rows,
        "sales_rows": int(sales[0]),
        "segment_sales_rows": int(sales[0]),
        "distinct_listing_rows": int(sales[1]),
        "min_sold_at": sales[2],
        "max_sold_at": sales[3],
        "max_fetched_at": sales[4],
        "valid_price_rows": int(sales[5] or 0),
        "sticker_rows": int(stickers[0]),
        "sticker_listing_rows": int(stickers[1]),
        "keychain_rows": int(keychains[0]),
        "keychain_listing_rows": int(keychains[1]),
        "sqlite_user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "item_kind": ITEM_KIND_WEAPON_SKIN,
        "item_kind_policy": STRICT_ITEM_KIND_POLICY,
    }
    value["logical_sha256"] = contract.canonical_sha256(
        {
            key: item
            for key, item in value.items()
            if key not in {"resolved_path", "database_sales_rows"}
        }
    )
    return value


def _update_framed(digest, prefix: bytes, payload: bytes) -> None:
    digest.update(prefix)
    digest.update(struct.pack(">Q", len(payload)))
    digest.update(payload)


def _update_scalar(digest, value) -> None:
    if value is None:
        digest.update(b"N")
    elif isinstance(value, int):
        digest.update(b"I")
        digest.update(struct.pack(">q", value))
    elif isinstance(value, float):
        digest.update(b"F")
        digest.update(struct.pack(">d", value))
    elif isinstance(value, bytes):
        _update_framed(digest, b"B", value)
    else:
        _update_framed(digest, b"T", str(value).encode("utf-8"))


def _source_query(
    conn: sqlite3.Connection,
    table_name: str,
    requested,
    where: str,
    parameters: tuple[object, ...] = (),
):
    available = {row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")')}
    selected = tuple(column for column in requested if column in available)
    if not selected:
        return selected, None, "", ()
    quoted = ", ".join(f'"{column}"' for column in selected)
    keys = {
        "sales": ("listing_id",),
        "sale_stickers": ("listing_id", "sticker_index"),
        "sale_keychains": ("listing_id", "keychain_index"),
    }[table_name]
    order_columns = [column for column in keys if column in available]
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    supports_rowid = not (
        table_sql and "WITHOUT ROWID" in str(table_sql[0] or "").upper()
    )
    order_parts = [f'"{column}"' for column in order_columns]
    if supports_rowid:
        order_parts.append("rowid")
    elif not order_parts:
        order_parts = [f'"{column}"' for column in selected]
    effective_where = str(where or "").strip()
    sql = f'SELECT {quoted} FROM "{table_name}"'
    if effective_where:
        sql += f" WHERE {effective_where}"
    if order_parts:
        sql += " ORDER BY " + ", ".join(order_parts)
    return selected, sql, effective_where, parameters


def stream_source_content_sha256(
    conn: sqlite3.Connection, *, chunk_size: int = 10_000
) -> str:
    """Hash exact training source values with bounded memory in this snapshot."""

    digest = hashlib.sha256()
    sales_where, sales_parameters = _eligible_weapon_skin_sales_clause(
        conn,
        sales_alias="sales",
    )
    sticker_where, sticker_parameters = _eligible_weapon_child_clause(
        conn,
        child_alias="sale_stickers",
    )
    keychain_where, keychain_parameters = _eligible_weapon_child_clause(
        conn,
        child_alias="sale_keychains",
    )
    specs = (
        (
            "sales",
            SALES_COLUMN_NAMES,
            sales_where,
            sales_parameters,
        ),
        ("sale_stickers", STICKER_COLUMN_NAMES, sticker_where, sticker_parameters),
        ("sale_keychains", KEYCHAIN_COLUMN_NAMES, keychain_where, keychain_parameters),
    )
    _update_framed(digest, b"K", ITEM_KIND_WEAPON_SKIN.encode("utf-8"))
    for table_name, requested, where, parameters in specs:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if not exists:
            _update_framed(
                digest,
                b"H",
                json.dumps(
                    {"table": table_name, "columns": [], "where": ""},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            continue
        selected, sql, effective_where, query_parameters = _source_query(
            conn,
            table_name,
            requested,
            where,
            parameters,
        )
        _update_framed(
            digest,
            b"H",
            json.dumps(
                {
                    "table": table_name,
                    "columns": selected,
                    "where": effective_where,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        if not sql:
            continue
        cursor = conn.execute(sql, query_parameters)
        while rows := cursor.fetchmany(chunk_size):
            for row in rows:
                digest.update(b"R")
                for value in row:
                    _update_scalar(digest, value)
    return digest.hexdigest()


def with_content_fingerprint(provenance: dict, content_sha256: str) -> dict:
    value = dict(provenance)
    value["content_sha256"] = content_sha256
    value["content_fingerprint_policy"] = SOURCE_CONTENT_FINGERPRINT_POLICY
    value["logical_sha256"] = contract.canonical_sha256(
        {
            key: item
            for key, item in value.items()
            if key
            not in {"logical_sha256", "resolved_path", "database_sales_rows"}
        }
    )
    return value


def load_sales(
    db_path: Path = DB_PATH, *, conn: sqlite3.Connection | None = None
) -> pd.DataFrame:
    if conn is None:
        with open_read_snapshot(db_path) as snapshot:
            return load_sales(db_path, conn=snapshot)
    weapon_where, weapon_parameters = _eligible_weapon_skin_sales_clause(
        conn,
        sales_alias="sales",
    )
    return pd.read_sql_query(
        f"""
        SELECT {SALES_COLUMNS}
        FROM sales
        WHERE {weapon_where}
        """,
        conn,
        params=weapon_parameters,
    )


def load_stickers(
    db_path: Path = DB_PATH, *, conn: sqlite3.Connection | None = None
) -> pd.DataFrame:
    if conn is None:
        with open_read_snapshot(db_path) as snapshot:
            return load_stickers(db_path, conn=snapshot)
    child_where, child_parameters = _eligible_weapon_child_clause(
        conn,
        child_alias="sale_stickers",
    )
    return pd.read_sql_query(
        f"""
        SELECT listing_id, sticker_index, sticker_id, slot, name, wear,
               price_cents, reference_price_cents, reference_updated_at
        FROM sale_stickers
        WHERE {child_where}
        """,
        conn,
        params=child_parameters,
    )


def load_keychains(
    db_path: Path = DB_PATH, *, conn: sqlite3.Connection | None = None
) -> pd.DataFrame:
    if conn is None:
        with open_read_snapshot(db_path) as snapshot:
            return load_keychains(db_path, conn=snapshot)
    child_where, child_parameters = _eligible_weapon_child_clause(
        conn,
        child_alias="sale_keychains",
    )
    return pd.read_sql_query(
        f"""
        SELECT listing_id, keychain_index, keychain_id, slot, name, pattern,
               price_cents, reference_quantity, reference_updated_at
        FROM sale_keychains
        WHERE {child_where}
        """,
        conn,
        params=child_parameters,
    )


def _attach_asof_contract(children: pd.DataFrame, sales: pd.DataFrame) -> pd.DataFrame:
    result = children.merge(
        sales[["listing_id", "sold_at"]],
        on="listing_id",
        how="left",
        validate="many_to_one",
    )
    flags = contract.reference_asof_flags(
        result["reference_updated_at"], result["sold_at"]
    )
    result["reference_asof_safe"] = flags["safe"].to_numpy()
    result["reference_asof_future"] = flags["future"].to_numpy()
    result["reference_asof_unknown"] = flags["unknown"].to_numpy()
    return result


def prepare_stickers(stickers: pd.DataFrame, sales: pd.DataFrame) -> pd.DataFrame:
    if stickers.empty:
        return stickers.copy()
    result = _attach_asof_contract(stickers, sales)
    raw_reference = pd.to_numeric(
        result["reference_price_cents"], errors="coerce"
    ).fillna(pd.to_numeric(result["price_cents"], errors="coerce"))
    result["raw_reference_price_cents"] = raw_reference.fillna(0).clip(lower=0)
    result["safe_reference_price_cents"] = result["raw_reference_price_cents"].where(
        result["reference_asof_safe"].astype(bool), 0.0
    )
    result["wear"] = contract.normalize_sticker_wear_series(result["wear"])
    return result


def pivot_sticker_slots(
    stickers: pd.DataFrame, max_slots: int = MAX_STICKER_SLOTS
) -> pd.DataFrame:
    stickers = stickers.copy()
    if stickers.empty:
        return pd.DataFrame(columns=["listing_id"])
    stickers["sticker_index"] = pd.to_numeric(
        stickers["sticker_index"], errors="coerce"
    )
    stickers["sticker_index"] = stickers["sticker_index"].clip(
        lower=0, upper=max_slots - 1
    )
    frames = []
    for idx in range(max_slots):
        one = stickers[stickers["sticker_index"] == idx][
            [
                "listing_id",
                "sticker_id",
                "name",
                "wear",
                "price_cents",
                "safe_reference_price_cents",
                "raw_reference_price_cents",
            ]
        ].copy()
        one = one.drop_duplicates(subset="listing_id", keep="first").rename(
            columns={
                "sticker_id": f"sticker_{idx}_id",
                "name": f"sticker_{idx}_name",
                "wear": f"sticker_{idx}_wear",
                "price_cents": f"sticker_{idx}_price_cents",
                "safe_reference_price_cents": f"sticker_{idx}_ref_price_cents",
                "raw_reference_price_cents": f"sticker_{idx}_raw_ref_price_cents",
            }
        )
        frames.append(one.set_index("listing_id"))
    return pd.concat(frames, axis=1).reset_index()


def build_sticker_aggregates(stickers: pd.DataFrame) -> pd.DataFrame:
    if stickers.empty:
        return pd.DataFrame(columns=["listing_id"])
    s = stickers.copy()
    s["name"] = s["name"].fillna("")
    s["is_katowice"] = s["name"].str.contains("Katowice 2014", na=False).astype(int)
    s["is_premium_finish"] = (
        s["name"]
        .str.contains(r"Holo|Foil|Gold|Glitter", na=False, regex=True)
        .astype(int)
    )
    grouped = s.groupby("listing_id")
    agg = grouped.agg(
        sticker_count=("sticker_index", "size"),
        n_distinct_stickers=("sticker_id", "nunique"),
        sum_ref_sticker_price=("safe_reference_price_cents", "sum"),
        max_ref_sticker_price=("safe_reference_price_cents", "max"),
        mean_ref_sticker_price=("safe_reference_price_cents", "mean"),
        min_ref_sticker_price=("safe_reference_price_cents", "min"),
        raw_sum_ref_sticker_price=("raw_reference_price_cents", "sum"),
        raw_max_ref_sticker_price=("raw_reference_price_cents", "max"),
        raw_mean_ref_sticker_price=("raw_reference_price_cents", "mean"),
        raw_min_ref_sticker_price=("raw_reference_price_cents", "min"),
        mean_sticker_wear=("wear", "mean"),
        max_slot_seen=("slot", "max"),
        has_katowice_2014=("is_katowice", "max"),
        has_premium_finish=("is_premium_finish", "max"),
        sticker_reference_safe_count=("reference_asof_safe", "sum"),
        sticker_reference_future_count=("reference_asof_future", "sum"),
        sticker_reference_unknown_count=("reference_asof_unknown", "sum"),
    ).reset_index()
    agg["has_slot_ge5"] = (agg["max_slot_seen"] >= 5).astype(int)
    return agg.drop(columns=["max_slot_seen"])


def prepare_keychains(keychains: pd.DataFrame, sales: pd.DataFrame) -> pd.DataFrame:
    if keychains.empty:
        return keychains.copy()
    result = _attach_asof_contract(keychains, sales)
    raw_price = (
        pd.to_numeric(result["price_cents"], errors="coerce").fillna(0).clip(lower=0)
    )
    result["raw_reference_price_cents"] = raw_price
    result["safe_reference_price_cents"] = raw_price.where(
        result["reference_asof_safe"].astype(bool), 0.0
    )
    return result


def pivot_keychain_slots(
    keychains: pd.DataFrame, max_slots: int = MAX_KEYCHAIN_SLOTS
) -> pd.DataFrame:
    if keychains.empty:
        return pd.DataFrame(columns=["listing_id"])
    frames = []
    for idx in range(max_slots):
        one = keychains[keychains["keychain_index"] == idx][
            [
                "listing_id",
                "keychain_id",
                "name",
                "pattern",
                "safe_reference_price_cents",
                "raw_reference_price_cents",
            ]
        ].copy()
        one = one.drop_duplicates(subset="listing_id", keep="first").rename(
            columns={
                "keychain_id": f"keychain_{idx}_id",
                "name": f"keychain_{idx}_name",
                "pattern": f"keychain_{idx}_pattern",
                "safe_reference_price_cents": f"keychain_{idx}_ref_price_cents",
                "raw_reference_price_cents": f"keychain_{idx}_raw_ref_price_cents",
            }
        )
        frames.append(one.set_index("listing_id"))
    return pd.concat(frames, axis=1).reset_index()


def build_keychain_aggregates(keychains: pd.DataFrame) -> pd.DataFrame:
    if keychains.empty:
        return pd.DataFrame(columns=["listing_id"])
    return (
        keychains.groupby("listing_id")
        .agg(
            keychain_count=("keychain_index", "size"),
            n_distinct_keychains=("keychain_id", "nunique"),
            sum_ref_keychain_price=("safe_reference_price_cents", "sum"),
            max_ref_keychain_price=("safe_reference_price_cents", "max"),
            raw_sum_ref_keychain_price=("raw_reference_price_cents", "sum"),
            keychain_reference_safe_count=("reference_asof_safe", "sum"),
            keychain_reference_future_count=("reference_asof_future", "sum"),
            keychain_reference_unknown_count=("reference_asof_unknown", "sum"),
        )
        .reset_index()
    )


def assemble_dataset(
    sales: pd.DataFrame, stickers: pd.DataFrame, keychains: pd.DataFrame
) -> pd.DataFrame:
    prepared_stickers = prepare_stickers(stickers, sales)
    prepared_keychains = prepare_keychains(keychains, sales)
    df = sales.merge(
        pivot_sticker_slots(prepared_stickers), on="listing_id", how="left"
    )
    df = df.merge(
        build_sticker_aggregates(prepared_stickers), on="listing_id", how="left"
    )
    df = df.merge(pivot_keychain_slots(prepared_keychains), on="listing_id", how="left")
    df = df.merge(
        build_keychain_aggregates(prepared_keychains), on="listing_id", how="left"
    )

    numeric_defaults = {
        "sticker_count": 0,
        "n_distinct_stickers": 0,
        "sum_ref_sticker_price": 0.0,
        "max_ref_sticker_price": 0.0,
        "mean_ref_sticker_price": 0.0,
        "min_ref_sticker_price": 0.0,
        "raw_sum_ref_sticker_price": 0.0,
        "raw_max_ref_sticker_price": 0.0,
        "raw_mean_ref_sticker_price": 0.0,
        "raw_min_ref_sticker_price": 0.0,
        "mean_sticker_wear": contract.MISSING_WEAR,
        "has_katowice_2014": 0,
        "has_premium_finish": 0,
        "has_slot_ge5": 0,
        "sticker_reference_safe_count": 0,
        "sticker_reference_future_count": 0,
        "sticker_reference_unknown_count": 0,
        "keychain_count": 0,
        "n_distinct_keychains": 0,
        "sum_ref_keychain_price": 0.0,
        "max_ref_keychain_price": 0.0,
        "raw_sum_ref_keychain_price": 0.0,
        "keychain_reference_safe_count": 0,
        "keychain_reference_future_count": 0,
        "keychain_reference_unknown_count": 0,
    }
    for col, value in numeric_defaults.items():
        if col not in df:
            df[col] = value
        else:
            df[col] = df[col].fillna(value)

    for idx in range(MAX_STICKER_SLOTS):
        defaults = {
            f"sticker_{idx}_id": -1,
            f"sticker_{idx}_name": "none",
            f"sticker_{idx}_wear": contract.MISSING_WEAR,
            f"sticker_{idx}_price_cents": 0.0,
            f"sticker_{idx}_ref_price_cents": 0.0,
            f"sticker_{idx}_raw_ref_price_cents": 0.0,
        }
        for col, value in defaults.items():
            if col not in df:
                df[col] = value
            else:
                df[col] = df[col].fillna(value)
        df[f"sticker_{idx}_id"] = (
            pd.to_numeric(df[f"sticker_{idx}_id"], errors="coerce")
            .fillna(-1)
            .astype("int64")
        )

    for idx in range(MAX_KEYCHAIN_SLOTS):
        defaults = {
            f"keychain_{idx}_id": -1,
            f"keychain_{idx}_name": "none",
            f"keychain_{idx}_pattern": -1,
            f"keychain_{idx}_ref_price_cents": 0.0,
            f"keychain_{idx}_raw_ref_price_cents": 0.0,
        }
        for col, value in defaults.items():
            if col not in df:
                df[col] = value
            else:
                df[col] = df[col].fillna(value)
    return df


def load_source_snapshot(db_path: Path = DB_PATH):
    with open_read_snapshot(db_path) as conn:
        provenance = snapshot_fingerprint(conn, db_path)
        content_sha256 = stream_source_content_sha256(conn)
        sales = load_sales(db_path, conn=conn)
        stickers = load_stickers(db_path, conn=conn)
        keychains = load_keychains(db_path, conn=conn)
        provenance = with_content_fingerprint(
            provenance, content_sha256
        )
    return sales, stickers, keychains, provenance


def build_dataset(db_path: Path = DB_PATH, *, return_provenance: bool = False):
    sales, stickers, keychains, provenance = load_source_snapshot(db_path)
    df = assemble_dataset(sales, stickers, keychains)
    if return_provenance:
        return df, provenance
    return df


if __name__ == "__main__":
    frame, provenance = build_dataset(return_provenance=True)
    print(json.dumps(provenance, ensure_ascii=False, indent=2))
    print(f"rows: {len(frame)}, cols: {len(frame.columns)}")
    print(f"with stickers: {(frame['sticker_count'] > 0).sum()}")
    print(f"with keychains: {(frame['keychain_count'] > 0).sum()}")
