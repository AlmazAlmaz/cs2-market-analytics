#!/usr/bin/env python3

import argparse
import hashlib
import json
import math
import os
import sqlite3
import struct
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import TargetEncoder

import app_config
import model_generations
from item_classification import (
    ITEM_KIND_GLOVE,
    ITEM_KIND_KNIFE,
    ITEM_KIND_WEAPON_SKIN,
    STRICT_ITEM_KIND_POLICY,
    sql_item_kind_expression,
)
from price_model_data_contract import (
    OBSERVATION_TIMESTAMP_KEYS,
    time_safe_reference_sql,
)

DB_PATH = str(app_config.path("CS2_DB_PATH", "csfloat_history.db"))
MODEL_DIR = app_config.PROJECT_DIR / "models"
MODEL_PATH = MODEL_DIR / "csfloat_price_model.joblib"
METRICS_PATH = MODEL_DIR / "csfloat_price_model_metrics.json"
DEFAULT_STAGING_DIR = MODEL_DIR / "staging" / "almaz"
KNIFE_MODEL_DIR = app_config.PROJECT_DIR / "KNIFEMODEL" / "artifacts"
KNIFE_MODEL_PATH = KNIFE_MODEL_DIR / "knife_price_model.joblib"
KNIFE_METRICS_PATH = KNIFE_MODEL_DIR / "knife_price_model_metrics.json"
DEFAULT_KNIFE_STAGING_DIR = app_config.PROJECT_DIR / "KNIFEMODEL" / "staging" / "knives"
GLOVE_MODEL_DIR = app_config.PROJECT_DIR / "GLOVEMODEL" / "artifacts"
GLOVE_MODEL_PATH = GLOVE_MODEL_DIR / "glove_price_model.joblib"
GLOVE_METRICS_PATH = GLOVE_MODEL_DIR / "glove_price_model_metrics.json"
DEFAULT_GLOVE_STAGING_DIR = app_config.PROJECT_DIR / "GLOVEMODEL" / "staging" / "gloves"

MAX_STICKERS = 5
TEST_FRACTION = 0.20
VALIDATION_FRACTION = 0.10
RANDOM_STATE = 42
ARTIFACT_VERSION = 3
TARGET_MODE = "log_premium"

# These values are present on completed-sale rows, but cannot be reproduced at
# the moment a fresh listing first appears.  Keeping them in a backtest makes
# it look better than the live model really is.
RUNTIME_UNSAFE_FEATURES = {"listing_age_hours", "watchers"}

SOURCE_CONTENT_FINGERPRINT_POLICY = (
    "sha256_item_kind_time_safe_rows_and_weapon_stickers_ordered_v1"
)
SOURCE_SALES_COLUMNS = (
    "listing_id",
    "name",
    "base_skin_name",
    "wear_name",
    "price_cents",
    "reference_base_price",
    "reference_predicted_price",
    "reference_float_factor",
    "reference_quantity",
    "float_value",
    "paint_seed",
    "paint_index",
    "created_at",
    "sold_at",
    "fetched_at",
    "first_seen_at",
    "reference_last_updated",
    "watchers",
    "item_quality",
    "item_rarity",
    "item_rarity_name",
    "item_def_index",
    "item_wear_name",
    "item_is_souvenir",
    "item_is_stattrak",
    "item_collection_name",
    "item_type_name",
)
SOURCE_STICKER_COLUMNS = (
    "listing_id",
    "sticker_index",
    "sticker_id",
    "slot",
    "name",
    "wear",
    "price_cents",
    "reference_price_cents",
    "reference_quantity",
    "offset_x",
    "offset_y",
)


@dataclass(frozen=True)
class TrainingProfile:
    model_name: str
    item_kind: str
    model_dir: Path
    model_path: Path
    metrics_path: Path
    staging_dir: Path


TRAINING_PROFILES = {
    ITEM_KIND_WEAPON_SKIN: TrainingProfile(
        model_name="almaz",
        item_kind=ITEM_KIND_WEAPON_SKIN,
        model_dir=MODEL_DIR,
        model_path=MODEL_PATH,
        metrics_path=METRICS_PATH,
        staging_dir=DEFAULT_STAGING_DIR,
    ),
    ITEM_KIND_KNIFE: TrainingProfile(
        model_name="knives",
        item_kind=ITEM_KIND_KNIFE,
        model_dir=KNIFE_MODEL_DIR,
        model_path=KNIFE_MODEL_PATH,
        metrics_path=KNIFE_METRICS_PATH,
        staging_dir=DEFAULT_KNIFE_STAGING_DIR,
    ),
    ITEM_KIND_GLOVE: TrainingProfile(
        model_name="gloves",
        item_kind=ITEM_KIND_GLOVE,
        model_dir=GLOVE_MODEL_DIR,
        model_path=GLOVE_MODEL_PATH,
        metrics_path=GLOVE_METRICS_PATH,
        staging_dir=DEFAULT_GLOVE_STAGING_DIR,
    ),
}


def training_profile(item_kind: str) -> TrainingProfile:
    try:
        return TRAINING_PROFILES[item_kind]
    except KeyError as exc:
        raise ValueError(f"unsupported training item kind: {item_kind!r}") from exc


def extract_wear_name(market_hash_name):
    if isinstance(market_hash_name, str) and market_hash_name.endswith(")") and "(" in market_hash_name:
        return market_hash_name.rsplit("(", 1)[1][:-1]
    return None


def extract_base_skin_name(market_hash_name):
    if isinstance(market_hash_name, str) and market_hash_name.endswith(")") and "(" in market_hash_name:
        return market_hash_name.rsplit("(", 1)[0].strip()
    return market_hash_name


def sticker_wear_multiplier(series):
    wear = pd.to_numeric(series, errors="coerce")
    wear = wear.where(wear <= 1, wear / 100)
    wear = wear.clip(lower=0, upper=1)
    multiplier = (1 - wear).pow(2.35).clip(lower=0.05, upper=1.0)
    return multiplier.fillna(1.0)


def load_data(db_path=DB_PATH, *, item_kind=ITEM_KIND_WEAPON_SKIN):
    training_profile(item_kind)
    with sqlite3.connect(str(db_path)) as conn:
        item_kind_expression, item_kind_parameters = sql_item_kind_expression(
            conn,
            name_expression="sales.name",
            unknown_as_weapon_skin=False,
        )
        sales = pd.read_sql_query(
            f"""
            SELECT listing_id,
                   name,
                   base_skin_name,
                   wear_name,
                   price_cents,
                   reference_base_price,
                   reference_predicted_price,
                   reference_float_factor,
                   reference_quantity,
                   float_value,
                   paint_seed,
                   paint_index,
                   created_at,
                   sold_at,
                   fetched_at,
                   first_seen_at,
                   reference_last_updated,
                   watchers,
                   item_quality,
                   item_rarity,
                   item_rarity_name,
                   item_def_index,
                   item_wear_name,
                   item_is_souvenir,
                   item_is_stattrak,
                   item_collection_name,
                   item_type_name
            FROM sales
            WHERE state = 'sold'
              AND price_cents IS NOT NULL
              AND price_cents > 0
              AND name IS NOT NULL
              AND ({item_kind_expression}) = ?
              AND {time_safe_reference_sql("sales")}
            """,
            conn,
            params=(*item_kind_parameters, item_kind),
        )

        sticker_kind_expression, sticker_kind_parameters = sql_item_kind_expression(
            conn,
            name_expression="sticker_sales.name",
            unknown_as_weapon_skin=False,
        )
        stickers = pd.read_sql_query(
            f"""
            SELECT listing_id,
                   sticker_index,
                   sticker_id,
                   slot,
                   name AS sticker_name,
                   wear AS sticker_wear,
                   price_cents AS sticker_price_cents,
                   reference_price_cents,
                   reference_quantity,
                   offset_x,
                   offset_y
            FROM sale_stickers AS stickers
            WHERE ? = ?
              AND EXISTS (
                  SELECT 1
                  FROM sales AS sticker_sales
                  WHERE sticker_sales.listing_id = stickers.listing_id
                    AND sticker_sales.state = 'sold'
                    AND sticker_sales.price_cents IS NOT NULL
                    AND sticker_sales.price_cents > 0
                    AND sticker_sales.name IS NOT NULL
                    AND ({sticker_kind_expression}) = ?
                    AND {time_safe_reference_sql("sticker_sales")}
              )
            """,
            conn,
            params=(
                item_kind,
                ITEM_KIND_WEAPON_SKIN,
                *sticker_kind_parameters,
                item_kind,
            ),
        )

    return sales, stickers


def add_sticker_features(sales, stickers):
    result = sales.copy()

    base_numeric_defaults = {
        "sticker_count": 0,
        "distinct_sticker_count": 0,
        "sticker_price_sum": 0,
        "sticker_price_max": 0,
        "sticker_price_mean": 0,
        "sticker_price_median": 0,
        "same_sticker_max_count": 0,
        "katowice_2014_count": 0,
        "katowice_2015_count": 0,
        "crown_count": 0,
        "holo_count": 0,
        "foil_count": 0,
        "gold_count": 0,
        "known_sticker_price_count": 0,
        "known_sticker_wear_count": 0,
    }

    if stickers.empty:
        for col, value in base_numeric_defaults.items():
            result[col] = value
        for idx in range(MAX_STICKERS):
            add_empty_sticker_slot(result, idx)
        return result

    stickers = stickers.copy()
    stickers["sticker_raw_price_cents"] = (
        stickers["sticker_price_cents"]
        .fillna(stickers["reference_price_cents"])
        .fillna(0)
    )
    stickers["sticker_effective_price_cents"] = (
        stickers["sticker_raw_price_cents"] * sticker_wear_multiplier(stickers["sticker_wear"])
    )
    stickers["sticker_name"] = stickers["sticker_name"].fillna("none")
    stickers["sticker_id"] = stickers["sticker_id"].fillna("none").astype(str)
    stickers["known_sticker_price"] = (
        stickers["sticker_price_cents"].notna()
        | stickers["reference_price_cents"].notna()
    ).astype(int)
    stickers["known_sticker_wear"] = stickers["sticker_wear"].notna().astype(int)

    dupes = (
        stickers
        .groupby(["listing_id", "sticker_id"], dropna=False)
        .size()
        .groupby("listing_id")
        .max()
        .rename("same_sticker_max_count")
        .reset_index()
    )

    grouped = stickers.groupby("listing_id", dropna=False)
    aggregate = grouped.agg(
        sticker_count=("sticker_id", "count"),
        distinct_sticker_count=("sticker_id", "nunique"),
        sticker_price_sum=("sticker_effective_price_cents", "sum"),
        sticker_price_max=("sticker_effective_price_cents", "max"),
        sticker_price_mean=("sticker_effective_price_cents", "mean"),
        sticker_price_median=("sticker_effective_price_cents", "median"),
        katowice_2014_count=("sticker_name", lambda x: x.str.contains("Katowice 2014", na=False).sum()),
        katowice_2015_count=("sticker_name", lambda x: x.str.contains("Katowice 2015", na=False).sum()),
        crown_count=("sticker_name", lambda x: x.str.contains("Crown", case=False, na=False).sum()),
        holo_count=("sticker_name", lambda x: x.str.contains(r"\(Holo\)", regex=True, na=False).sum()),
        foil_count=("sticker_name", lambda x: x.str.contains(r"\(Foil\)", regex=True, na=False).sum()),
        gold_count=("sticker_name", lambda x: x.str.contains(r"\(Gold\)", regex=True, na=False).sum()),
        known_sticker_price_count=("known_sticker_price", "sum"),
        known_sticker_wear_count=("known_sticker_wear", "sum"),
    ).reset_index()
    aggregate = aggregate.merge(dupes, on="listing_id", how="left")

    result = result.merge(aggregate, on="listing_id", how="left")

    ordered_stickers = stickers.sort_values(["listing_id", "sticker_index", "slot"], na_position="last")
    for idx in range(MAX_STICKERS):
        one = ordered_stickers[ordered_stickers["sticker_index"] == idx][
            [
                "listing_id",
                "sticker_id",
                "slot",
                "sticker_name",
                "sticker_wear",
                "sticker_effective_price_cents",
                "reference_quantity",
                "offset_x",
                "offset_y",
            ]
        ].copy()

        one = one.rename(columns={
            "sticker_id": f"sticker_id_{idx}",
            "slot": f"sticker_slot_{idx}",
            "sticker_name": f"sticker_name_{idx}",
            "sticker_wear": f"sticker_wear_{idx}",
            "sticker_effective_price_cents": f"sticker_price_{idx}",
            "reference_quantity": f"sticker_reference_quantity_{idx}",
            "offset_x": f"sticker_offset_x_{idx}",
            "offset_y": f"sticker_offset_y_{idx}",
        })

        result = result.merge(one, on="listing_id", how="left")

    for col, value in base_numeric_defaults.items():
        result[col] = result[col].fillna(value)

    for idx in range(MAX_STICKERS):
        fill_sticker_slot(result, idx)

    return result


def add_empty_sticker_slot(df, idx):
    df[f"sticker_id_{idx}"] = "none"
    df[f"sticker_name_{idx}"] = "none"
    df[f"sticker_slot_{idx}"] = -1
    df[f"sticker_wear_{idx}"] = -1.0
    df[f"sticker_price_{idx}"] = 0
    df[f"sticker_reference_quantity_{idx}"] = 0
    df[f"sticker_offset_x_{idx}"] = 0
    df[f"sticker_offset_y_{idx}"] = 0


def fill_sticker_slot(df, idx):
    df[f"sticker_id_{idx}"] = df[f"sticker_id_{idx}"].fillna("none").astype(str)
    df[f"sticker_name_{idx}"] = df[f"sticker_name_{idx}"].fillna("none").astype(str)
    df[f"sticker_slot_{idx}"] = df[f"sticker_slot_{idx}"].fillna(-1)
    df[f"sticker_wear_{idx}"] = df[f"sticker_wear_{idx}"].fillna(-1)
    df[f"sticker_price_{idx}"] = df[f"sticker_price_{idx}"].fillna(0)
    df[f"sticker_reference_quantity_{idx}"] = df[f"sticker_reference_quantity_{idx}"].fillna(0)
    df[f"sticker_offset_x_{idx}"] = df[f"sticker_offset_x_{idx}"].fillna(0)
    df[f"sticker_offset_y_{idx}"] = df[f"sticker_offset_y_{idx}"].fillna(0)


def add_time_features(df):
    result = df.copy()

    def timestamp_column(name):
        values = (
            result[name]
            if name in result
            else pd.Series(pd.NaT, index=result.index)
        )
        return pd.to_datetime(values, errors="coerce", utc=True)

    result["sold_dt"] = timestamp_column("sold_at")
    result["created_dt"] = timestamp_column("created_at")
    result["fetched_dt"] = timestamp_column("fetched_at")
    result["first_seen_dt"] = timestamp_column("first_seen_at")
    result["observation_dt"] = (
        result["first_seen_dt"]
        .fillna(result["fetched_dt"])
        .fillna(result["created_dt"])
    )

    # Names stay stable for compatibility with existing artifact schemas, but
    # values describe what was observable at inference, not the future sale.
    result["sold_year"] = result["observation_dt"].dt.year.fillna(1970)
    result["sold_month"] = result["observation_dt"].dt.month.fillna(1)
    result["sold_dayofweek"] = result["observation_dt"].dt.dayofweek.fillna(0)
    result["sold_hour"] = result["observation_dt"].dt.hour.fillna(0)
    result["sold_days_since_epoch"] = (
        result["observation_dt"] - pd.Timestamp("1970-01-01", tz="UTC")
    ) / pd.Timedelta(days=1)
    result["sold_days_since_epoch"] = result["sold_days_since_epoch"].fillna(0)

    age_hours = (
        result["observation_dt"] - result["created_dt"]
    ).dt.total_seconds() / 3600
    result["listing_age_hours"] = age_hours.replace([np.inf, -np.inf], np.nan).fillna(-1)

    return result


def clean_features(df):
    result = df.copy()

    result["base_skin_name"] = result["base_skin_name"].fillna(result["name"].map(extract_base_skin_name))
    result["wear_name"] = result["wear_name"].fillna(result["item_wear_name"])
    result["wear_name"] = result["wear_name"].fillna(result["name"].map(extract_wear_name))
    result["paint_index"] = result["paint_index"].fillna(-1).astype(int).astype(str)
    result["paint_seed"] = result["paint_seed"].fillna(-1)
    result["float_value"] = result["float_value"].fillna(-1)

    text_columns = [
        "name",
        "base_skin_name",
        "wear_name",
        "item_quality",
        "item_rarity_name",
        "item_collection_name",
        "item_type_name",
    ]
    for col in text_columns:
        result[col] = result[col].fillna("unknown").astype(str)

    numeric_defaults = {
        "reference_base_price": 0,
        "reference_predicted_price": 0,
        "reference_float_factor": 1,
        "reference_quantity": 0,
        "watchers": 0,
        "item_rarity": -1,
        "item_def_index": -1,
        "item_is_souvenir": 0,
        "item_is_stattrak": 0,
    }
    for col, value in numeric_defaults.items():
        result[col] = result[col].fillna(value)

    result["sticker_value_to_base"] = result["sticker_price_sum"] / result["reference_base_price"].replace(0, np.nan)
    result["sticker_value_to_base"] = result["sticker_value_to_base"].replace([np.inf, -np.inf], np.nan).fillna(0)

    return result


def build_dataset(db_path=DB_PATH, *, item_kind=ITEM_KIND_WEAPON_SKIN):
    sales, stickers = load_data(db_path, item_kind=item_kind)
    df = add_sticker_features(sales, stickers)
    df = add_time_features(df)
    df = clean_features(df)
    df = df.sort_values(["sold_dt", "listing_id"]).reset_index(drop=True)
    df = df[df["price_cents"] > 0].reset_index(drop=True)
    return df


def _fingerprint_scalar(digest, value) -> None:
    """Append one SQLite value without ambiguous text delimiters."""

    if value is None:
        digest.update(b"N")
    elif isinstance(value, int):
        digest.update(b"I")
        digest.update(struct.pack(">q", value))
    elif isinstance(value, float):
        digest.update(b"F")
        digest.update(struct.pack(">d", value))
    else:
        payload = value if isinstance(value, bytes) else str(value).encode("utf-8")
        digest.update(b"B" if isinstance(value, bytes) else b"T")
        digest.update(struct.pack(">Q", len(payload)))
        digest.update(payload)


def _update_source_table_fingerprint(
    digest,
    conn: sqlite3.Connection,
    *,
    table: str,
    requested_columns: tuple[str, ...],
    where_sql: str,
    parameters: tuple[object, ...],
    order_columns: tuple[str, ...],
) -> None:
    """Hash the exact columns consumed by training in deterministic order."""

    available = {
        str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
    }
    columns = tuple(column for column in requested_columns if column in available)
    header = json.dumps(
        {
            "table": table,
            "columns": columns,
            "where_contract": where_sql,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest.update(b"H")
    digest.update(struct.pack(">Q", len(header)))
    digest.update(header)
    if not columns:
        return

    selected = ", ".join(f'"{column}"' for column in columns)
    ordering = [column for column in order_columns if column in available]
    # All names are internal constants.  Quoting them still keeps the helper
    # safe if a future schema happens to use a reserved SQL word.
    order_sql = ", ".join(f'"{column}"' for column in ordering)
    query = f'SELECT {selected} FROM "{table}" WHERE {where_sql}'
    if order_sql:
        query += f" ORDER BY {order_sql}"
    cursor = conn.execute(query, parameters)
    while rows := cursor.fetchmany(10_000):
        for row in rows:
            digest.update(b"R")
            for value in row:
                _fingerprint_scalar(digest, value)


def _source_content_sha256(
    conn: sqlite3.Connection,
    *,
    item_kind: str,
    item_kind_expression: str,
    item_kind_parameters: tuple[object, ...],
) -> str:
    """Hash only this model's immutable, time-safe training source rows."""

    digest = hashlib.sha256()
    for label in (item_kind, STRICT_ITEM_KIND_POLICY):
        payload = label.encode("utf-8")
        digest.update(b"K")
        digest.update(struct.pack(">Q", len(payload)))
        digest.update(payload)

    sales_where = (
        "state = 'sold' AND price_cents IS NOT NULL AND price_cents > 0 "
        "AND name IS NOT NULL "
        f"AND ({item_kind_expression}) = ? "
        f"AND {time_safe_reference_sql()}"
    )
    sales_parameters = (*item_kind_parameters, item_kind)
    _update_source_table_fingerprint(
        digest,
        conn,
        table="sales",
        requested_columns=SOURCE_SALES_COLUMNS,
        where_sql=sales_where,
        parameters=sales_parameters,
        order_columns=("listing_id",),
    )

    if item_kind == ITEM_KIND_WEAPON_SKIN:
        sticker_kind_expression, sticker_kind_parameters = sql_item_kind_expression(
            conn,
            name_expression="source_sales.name",
            unknown_as_weapon_skin=False,
        )
        sticker_where = (
            "EXISTS (SELECT 1 FROM sales AS source_sales "
            "WHERE source_sales.listing_id = sale_stickers.listing_id "
            "AND source_sales.state = 'sold' "
            "AND source_sales.price_cents IS NOT NULL "
            "AND source_sales.price_cents > 0 "
            "AND source_sales.name IS NOT NULL "
            f"AND ({sticker_kind_expression}) = ? "
            f"AND {time_safe_reference_sql('source_sales')})"
        )
        _update_source_table_fingerprint(
            digest,
            conn,
            table="sale_stickers",
            requested_columns=SOURCE_STICKER_COLUMNS,
            where_sql=sticker_where,
            parameters=(*sticker_kind_parameters, item_kind),
            order_columns=("listing_id", "sticker_index"),
        )
    return digest.hexdigest()


def source_database_fingerprint(
    db_path: str | Path,
    *,
    item_kind: str = ITEM_KIND_WEAPON_SKIN,
) -> dict:
    """Return a cheap logical fingerprint that also sees SQLite WAL changes.

    Hashing a live 4+ GB SQLite file on every training run is both slow and
    unreliable when recent commits are still in the WAL.  Item-kind-scoped
    counts and temporal boundaries queried through SQLite form the logical
    cache/artifact provenance contract without reacting to another model's
    rows.
    """

    path = Path(db_path).expanduser().resolve()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        # Keep aggregates and streamed row content on one WAL-aware snapshot.
        conn.execute("BEGIN")
        training_profile(item_kind)
        item_kind_expression, item_kind_parameters = sql_item_kind_expression(
            conn,
            name_expression="sales.name",
            unknown_as_weapon_skin=False,
        )
        sales = conn.execute(
            f"""
            WITH segment AS (
                SELECT listing_id, sold_at, fetched_at,
                       CASE WHEN state = 'sold'
                              AND price_cents IS NOT NULL AND price_cents > 0
                              AND name IS NOT NULL
                            THEN 1 ELSE 0 END AS source_valid,
                       CASE WHEN state = 'sold'
                              AND price_cents IS NOT NULL AND price_cents > 0
                              AND name IS NOT NULL
                              AND {time_safe_reference_sql("sales")}
                            THEN 1 ELSE 0 END AS training_valid
                FROM sales
                WHERE state = 'sold'
                  AND price_cents IS NOT NULL AND price_cents > 0
                  AND name IS NOT NULL
                  AND ({item_kind_expression}) = ?
            )
            SELECT COUNT(*), COUNT(DISTINCT listing_id),
                   MIN(CASE WHEN training_valid THEN sold_at END),
                   MAX(CASE WHEN training_valid THEN sold_at END),
                   MAX(CASE WHEN training_valid THEN fetched_at END),
                   COALESCE(SUM(source_valid), 0),
                   COALESCE(SUM(training_valid), 0)
            FROM segment
            """,
            (*item_kind_parameters, item_kind),
        ).fetchone()
        if item_kind == ITEM_KIND_WEAPON_SKIN:
            sticker_kind_expression, sticker_kind_parameters = sql_item_kind_expression(
                conn,
                name_expression="sticker_sales.name",
                unknown_as_weapon_skin=False,
            )
            stickers = conn.execute(
                f"""
                SELECT COUNT(*), COUNT(DISTINCT stickers.listing_id)
                FROM sale_stickers AS stickers
                WHERE EXISTS (
                    SELECT 1
                    FROM sales AS sticker_sales
                    WHERE sticker_sales.listing_id = stickers.listing_id
                      AND sticker_sales.state = 'sold'
                      AND sticker_sales.price_cents IS NOT NULL
                      AND sticker_sales.price_cents > 0
                      AND sticker_sales.name IS NOT NULL
                      AND ({sticker_kind_expression}) = ?
                      AND {time_safe_reference_sql("sticker_sales")}
                )
                """,
                (*sticker_kind_parameters, item_kind),
            ).fetchone()
        else:
            stickers = (0, 0)
        content_sha256 = _source_content_sha256(
            conn,
            item_kind=item_kind,
            item_kind_expression=item_kind_expression,
            item_kind_parameters=item_kind_parameters,
        )
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]

    logical = {
        "resolved_path": str(path),
        "sales_rows": int(sales[0]),
        "segment_sales_rows": int(sales[0]),
        "distinct_listing_rows": int(sales[1]),
        "min_sold_at": sales[2],
        "max_sold_at": sales[3],
        "max_fetched_at": sales[4],
        "source_valid_price_rows": int(sales[5]),
        "valid_price_rows": int(sales[6]),
        "sticker_rows": int(stickers[0]),
        "sticker_listing_rows": int(stickers[1]),
        "sqlite_user_version": int(schema_version),
        "item_kind": item_kind,
        "item_kind_policy": STRICT_ITEM_KIND_POLICY,
        "content_sha256": content_sha256,
        "content_fingerprint_policy": SOURCE_CONTENT_FINGERPRINT_POLICY,
    }
    # The path is provenance rather than content identity.  Every other field
    # is scoped to this exact item-kind segment so unrelated knives/gloves do
    # not invalidate a weapon candidate (and vice versa).
    identity = {
        key: value
        for key, value in logical.items()
        if key != "resolved_path"
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    logical["logical_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return logical


def add_reference_target(df: pd.DataFrame) -> pd.DataFrame:
    """Add a reference anchor that was observable no later than the sale."""

    result = df.copy()
    missing_timestamps = pd.Series(pd.NaT, index=result.index)
    reference_updated = pd.to_datetime(
        result.get("reference_last_updated", missing_timestamps),
        errors="coerce",
        utc=True,
    )
    sold = pd.to_datetime(
        result.get("sold_at", missing_timestamps), errors="coerce", utc=True
    )
    result = result[
        reference_updated.notna()
        & sold.notna()
        & reference_updated.le(sold)
    ].copy()
    reference = pd.to_numeric(result["reference_predicted_price"], errors="coerce")
    base = pd.to_numeric(result["reference_base_price"], errors="coerce")
    anchor = reference.where(reference > 0).fillna(base.where(base > 0))
    result["reference_anchor_cents"] = anchor
    result = result[result["reference_anchor_cents"] > 0].copy()
    result["log_reference_anchor"] = np.log1p(result["reference_anchor_cents"])
    result["log_price"] = np.log1p(result["price_cents"].astype(float))
    result["log_premium"] = result["log_price"] - result["log_reference_anchor"]
    return result


def compute_market_stats(df):
    source = df.copy()
    source = source[source["price_cents"] > 0]

    if source.empty:
        return {
            "skin": {},
            "global": {
                "median_price_cents": 0,
                "avg_price_cents": 0,
                "sales_per_day": 0,
                "max_sales_per_day": 1,
            },
        }

    min_ts = source["sold_dt"].min()
    max_ts = source["sold_dt"].max()
    global_days = max(1.0, (max_ts - min_ts).total_seconds() / 86400)
    global_median = float(source["price_cents"].median())
    global_avg = float(source["price_cents"].mean())

    grouped = source.groupby("name", dropna=False)
    stats = grouped.agg(
        skin_sales_count=("price_cents", "count"),
        skin_avg_price_cents=("price_cents", "mean"),
        skin_median_price_cents=("price_cents", "median"),
        skin_min_price_cents=("price_cents", "min"),
        skin_max_price_cents=("price_cents", "max"),
        skin_avg_float=("float_value", "mean"),
        skin_stickered_share=("sticker_count", lambda x: float((x > 0).mean())),
        skin_avg_sticker_value_cents=("sticker_price_sum", "mean"),
        skin_oldest_ts=("sold_dt", "min"),
        skin_latest_ts=("sold_dt", "max"),
    ).reset_index()

    stats["skin_days_span"] = (
        (stats["skin_latest_ts"] - stats["skin_oldest_ts"]).dt.total_seconds() / 86400
    ).clip(lower=1)
    stats["skin_sales_per_day"] = stats["skin_sales_count"] / stats["skin_days_span"]

    max_sales_per_day = max(float(stats["skin_sales_per_day"].max()), 1.0)

    skin_stats = {}
    for row in stats.to_dict("records"):
        skin_stats[str(row["name"])] = {
            "skin_sales_count": int(row["skin_sales_count"]),
            "skin_avg_price_cents": float(row["skin_avg_price_cents"]),
            "skin_median_price_cents": float(row["skin_median_price_cents"]),
            "skin_min_price_cents": float(row["skin_min_price_cents"]),
            "skin_max_price_cents": float(row["skin_max_price_cents"]),
            "skin_avg_float": float(row["skin_avg_float"]) if not pd.isna(row["skin_avg_float"]) else -1,
            "skin_stickered_share": float(row["skin_stickered_share"]),
            "skin_avg_sticker_value_cents": float(row["skin_avg_sticker_value_cents"]),
            "skin_days_span": float(row["skin_days_span"]),
            "skin_sales_per_day": float(row["skin_sales_per_day"]),
        }

    return {
        "skin": skin_stats,
        "global": {
            "median_price_cents": global_median,
            "avg_price_cents": global_avg,
            "sales_per_day": len(source) / global_days,
            "max_sales_per_day": max_sales_per_day,
            "data_oldest_sold_at": min_ts.isoformat(),
            "data_latest_sold_at": max_ts.isoformat(),
        },
    }


def apply_market_stats(df, market_stats):
    result = df.copy()
    global_stats = market_stats["global"]
    skin_stats = market_stats["skin"]

    defaults = {
        "skin_sales_count": 0,
        "skin_avg_price_cents": global_stats["avg_price_cents"],
        "skin_median_price_cents": global_stats["median_price_cents"],
        "skin_min_price_cents": global_stats["median_price_cents"],
        "skin_max_price_cents": global_stats["median_price_cents"],
        "skin_avg_float": -1,
        "skin_stickered_share": 0,
        "skin_avg_sticker_value_cents": 0,
        "skin_days_span": 0,
        "skin_sales_per_day": 0,
    }

    for col, default in defaults.items():
        result[col] = result["name"].map(
            lambda name, column=col, fallback=default: skin_stats.get(str(name), {}).get(
                column, fallback
            )
        )

    result["skin_liquidity_score"] = (
        np.log1p(result["skin_sales_per_day"])
        / np.log1p(max(global_stats["max_sales_per_day"], 1))
        * 100
    ).clip(0, 100)

    return result


def apply_time_safe_training_market_stats(df, market_stats):
    """Build per-skin aggregates from strictly earlier rows only.

    Production sees sales that happened before a live listing. Using one
    full-train aggregate for every training row leaks that row's target and
    later prices into numeric features. Expanding and shifted aggregates match
    the information boundary used at inference time.
    """

    result = df.sort_values(["sold_dt", "listing_id"]).copy()
    names = result["name"]
    grouped = result.groupby("name", sort=False, dropna=False)
    prior_count = grouped.cumcount().astype(float)
    price = result["price_cents"].astype(float)
    prior_price_sum = grouped["price_cents"].cumsum().astype(float) - price
    prior_price = grouped["price_cents"].shift(1).astype(float)
    global_stats = market_stats["global"]

    result["skin_sales_count"] = prior_count
    result["skin_avg_price_cents"] = (prior_price_sum / prior_count.replace(0, np.nan)).fillna(
        global_stats["avg_price_cents"]
    )
    result["skin_median_price_cents"] = grouped["price_cents"].transform(
        lambda values: values.expanding().median().shift(1)
    ).fillna(global_stats["median_price_cents"])
    result["skin_min_price_cents"] = prior_price.groupby(names).cummin().fillna(
        global_stats["median_price_cents"]
    )
    result["skin_max_price_cents"] = prior_price.groupby(names).cummax().fillna(
        global_stats["median_price_cents"]
    )

    float_value = result["float_value"].where(result["float_value"] >= 0)
    float_known = float_value.notna().astype(float)
    prior_float_count = float_known.groupby(names).cumsum() - float_known
    prior_float_sum = float_value.fillna(0).groupby(names).cumsum() - float_value.fillna(0)
    result["skin_avg_float"] = (prior_float_sum / prior_float_count.replace(0, np.nan)).fillna(-1)

    stickered = (result["sticker_count"] > 0).astype(float)
    prior_stickered = stickered.groupby(names).cumsum() - stickered
    result["skin_stickered_share"] = (prior_stickered / prior_count.replace(0, np.nan)).fillna(0)

    sticker_value = result["sticker_price_sum"].astype(float)
    prior_sticker_value = sticker_value.groupby(names).cumsum() - sticker_value
    result["skin_avg_sticker_value_cents"] = (
        prior_sticker_value / prior_count.replace(0, np.nan)
    ).fillna(0)

    oldest = grouped["sold_dt"].transform("min")
    result["skin_days_span"] = (
        (result["sold_dt"] - oldest).dt.total_seconds() / 86400
    ).clip(lower=1)
    result["skin_sales_per_day"] = prior_count / result["skin_days_span"]
    result["skin_liquidity_score"] = (
        np.log1p(result["skin_sales_per_day"])
        / np.log1p(max(global_stats["max_sales_per_day"], 1))
        * 100
    ).clip(0, 100)
    return result


def get_feature_columns():
    categorical = [
        "name",
        "base_skin_name",
        "wear_name",
        "paint_index",
        "item_quality",
        "item_rarity_name",
        "item_collection_name",
        "item_type_name",
    ]

    for idx in range(MAX_STICKERS):
        categorical.extend([
            f"sticker_id_{idx}",
            f"sticker_name_{idx}",
        ])

    numeric = [
        "float_value",
        "paint_seed",
        "reference_base_price",
        "reference_predicted_price",
        "reference_float_factor",
        "reference_quantity",
        "item_rarity",
        "item_def_index",
        "item_is_souvenir",
        "item_is_stattrak",
        "sticker_value_to_base",
        "sticker_count",
        "distinct_sticker_count",
        "sticker_price_sum",
        "sticker_price_max",
        "sticker_price_mean",
        "sticker_price_median",
        "same_sticker_max_count",
        "katowice_2014_count",
        "katowice_2015_count",
        "crown_count",
        "holo_count",
        "foil_count",
        "gold_count",
        "known_sticker_price_count",
        "known_sticker_wear_count",
        "sold_year",
        "sold_month",
        "sold_dayofweek",
        "sold_hour",
        "sold_days_since_epoch",
        "skin_sales_count",
        "skin_avg_price_cents",
        "skin_median_price_cents",
        "skin_min_price_cents",
        "skin_max_price_cents",
        "skin_avg_float",
        "skin_stickered_share",
        "skin_avg_sticker_value_cents",
        "skin_days_span",
        "skin_sales_per_day",
        "skin_liquidity_score",
    ]

    for idx in range(MAX_STICKERS):
        numeric.extend([
            f"sticker_slot_{idx}",
            f"sticker_wear_{idx}",
            f"sticker_price_{idx}",
            f"sticker_reference_quantity_{idx}",
            f"sticker_offset_x_{idx}",
            f"sticker_offset_y_{idx}",
        ])

    return categorical, numeric


def make_preprocessor(categorical, numeric):
    return ColumnTransformer(
        transformers=[
            (
                "cat",
                TargetEncoder(target_type="continuous", smooth="auto", cv=5, random_state=RANDOM_STATE),
                categorical,
            ),
            ("num", SimpleImputer(strategy="median"), numeric),
        ],
        remainder="drop",
        verbose_feature_names_out=False,
    )


def make_price_pipeline(
    categorical,
    numeric,
    loss="squared_error",
    quantile=None,
    *,
    max_iter=700,
    verbose=0,
):
    model_kwargs = {
        "loss": loss,
        "learning_rate": 0.045,
        "max_iter": max_iter,
        "max_leaf_nodes": 31,
        "min_samples_leaf": 28,
        "l2_regularization": 0.05,
        # Validation is an explicit later chronological slice.  An internal
        # random validation fraction would violate the temporal contract.
        "early_stopping": False,
        "random_state": RANDOM_STATE,
        "verbose": int(verbose),
    }
    if quantile is not None:
        model_kwargs["quantile"] = quantile

    return Pipeline([
        ("features", make_preprocessor(categorical, numeric)),
        ("model", HistGradientBoostingRegressor(**model_kwargs)),
    ])


def evaluate(y_true_log, y_pred_log):
    y_true = np.expm1(y_true_log)
    y_pred = np.maximum(np.expm1(y_pred_log), 1)

    absolute_percentage_error = np.abs(y_pred - y_true) / np.maximum(y_true, 1)

    return {
        "mae_cents": float(mean_absolute_error(y_true, y_pred)),
        "mae_usd": float(mean_absolute_error(y_true, y_pred) / 100),
        "median_absolute_percentage_error": float(np.median(absolute_percentage_error)),
        "mean_absolute_percentage_error": float(np.mean(absolute_percentage_error)),
        "p75_absolute_percentage_error": float(np.quantile(absolute_percentage_error, 0.75)),
        "p90_absolute_percentage_error": float(np.quantile(absolute_percentage_error, 0.90)),
        "rmse_log_price": float(math.sqrt(mean_squared_error(y_true_log, y_pred_log))),
        "r2_log_price": float(r2_score(y_true_log, y_pred_log)),
    }


def chronological_train_validation_test_split(
    df: pd.DataFrame,
    *,
    holdout_after: str | None,
    validation_fraction: float = VALIDATION_FRACTION,
    test_fraction: float = TEST_FRACTION,
    test_rows: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    """Create a later validation and an untouched newest shared holdout."""

    ordered = df.sort_values(["sold_dt", "listing_id"]).reset_index(drop=True)
    cutoff = pd.to_datetime(holdout_after, utc=True) if holdout_after else None
    if test_rows is not None:
        requested_test_rows = int(test_rows)
        if requested_test_rows < 1 or requested_test_rows >= len(ordered):
            raise ValueError("test_rows must leave non-empty development and test sets")
        test_start = ordered.iloc[-requested_test_rows]["sold_dt"]
        development = ordered[ordered["sold_dt"] < test_start].copy()
        test = ordered[ordered["sold_dt"] >= test_start].copy()
        cutoff = test_start
        split_method = "fixed_newest_rows_with_timestamp_ties"
    elif cutoff is not None:
        development = ordered[ordered["sold_dt"] <= cutoff].copy()
        test = ordered[ordered["sold_dt"] > cutoff].copy()
        split_method = "production_trained_at_shared_holdout"
    else:
        test_rows = max(1, int(len(ordered) * test_fraction))
        test_start = ordered.iloc[-test_rows]["sold_dt"]
        development = ordered[ordered["sold_dt"] < test_start].copy()
        test = ordered[ordered["sold_dt"] >= test_start].copy()
        split_method = "chronological_fraction"

    if len(development) < 10_000 or len(test) < 1_000:
        raise ValueError(
            f"Chronological split is too small: development={len(development)}, test={len(test)}"
        )
    validation_rows = max(1_000, int(len(development) * validation_fraction))
    validation_start = development.iloc[-validation_rows]["sold_dt"]
    train = development[development["sold_dt"] < validation_start].copy()
    validation = development[development["sold_dt"] >= validation_start].copy()
    contract = {
        "method": split_method,
        "holdout_after": cutoff.isoformat() if cutoff is not None else None,
        "train_rows": len(train),
        "validation_rows": len(validation),
        "test_rows": len(test),
        "train_min_sold_at": train["sold_dt"].min().isoformat(),
        "train_max_sold_at": train["sold_dt"].max().isoformat(),
        "validation_min_sold_at": validation["sold_dt"].min().isoformat(),
        "validation_max_sold_at": validation["sold_dt"].max().isoformat(),
        "test_min_sold_at": test["sold_dt"].min().isoformat(),
        "test_max_sold_at": test["sold_dt"].max().isoformat(),
        "test_untouched_until_final_evaluation": True,
    }
    return train, validation, test, contract


def reference_log(frame: pd.DataFrame) -> np.ndarray:
    if "log_reference_anchor" in frame:
        return frame["log_reference_anchor"].to_numpy(dtype=float)
    reference = pd.to_numeric(frame["reference_predicted_price"], errors="coerce")
    base = pd.to_numeric(frame["reference_base_price"], errors="coerce")
    anchor = reference.where(reference > 0).fillna(base.where(base > 0)).fillna(1.0)
    return np.log1p(anchor.to_numpy(dtype=float))


def prediction_to_absolute_log(
    raw_prediction: np.ndarray,
    frame: pd.DataFrame,
    *,
    target_mode: str,
    calibration: dict | None = None,
    interval: str = "center",
) -> np.ndarray:
    calibration = calibration or {}
    raw = np.asarray(raw_prediction, dtype=float)
    if target_mode == "log_premium":
        scale = float(calibration.get("premium_scale", 1.0))
        offset = float(calibration.get(f"{interval}_offset", calibration.get("center_offset", 0.0)))
        return reference_log(frame) + raw * scale + offset
    return raw + float(calibration.get(f"{interval}_offset", calibration.get("center_offset", 0.0)))


def fit_validation_calibration(
    validation_df: pd.DataFrame,
    raw_prediction: np.ndarray,
    *,
    target_mode: str,
) -> dict:
    """Fit shrinkage and centering on validation, never on the test slice."""

    y_log = validation_df["log_price"].to_numpy(dtype=float)
    scales = np.arange(0.70, 1.151, 0.01) if target_mode == "log_premium" else np.array([1.0])
    best = None
    for scale in scales:
        base_prediction = (
            reference_log(validation_df) + np.asarray(raw_prediction) * scale
            if target_mode == "log_premium"
            else np.asarray(raw_prediction)
        )
        offset = float(np.median(y_log - base_prediction))
        current = evaluate(y_log, base_prediction + offset)
        score = (
            current["median_absolute_percentage_error"]
            + 0.25 * current["mean_absolute_percentage_error"]
            + 0.10 * current["p90_absolute_percentage_error"]
        )
        if best is None or score < best[0]:
            best = (score, float(scale), offset, current)
    return {
        "premium_scale": best[1],
        "center_offset": best[2],
        "validation_metrics": best[3],
    }


def evaluate_artifact_pipeline(artifact: dict, frame: pd.DataFrame) -> tuple[dict, np.ndarray]:
    columns = list(artifact["feature_columns"])
    x = frame[columns]
    raw = artifact["pipeline"].predict(x)
    predicted_log = prediction_to_absolute_log(
        raw,
        frame,
        target_mode=artifact.get("target_mode", "log_price"),
        calibration=artifact.get("prediction_calibration"),
    )
    return evaluate(frame["log_price"].to_numpy(dtype=float), predicted_log), predicted_log


def prepare_runtime_parity_backtest(frame: pd.DataFrame) -> pd.DataFrame:
    """Simulate values known when a listing first appears, not after it sells."""

    result = frame.copy()
    if "listing_age_hours" in result:
        result["listing_age_hours"] = 0.0
    if "watchers" in result:
        result["watchers"] = 0.0
    return result


def atomic_joblib_dump(value, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        joblib.dump(value, temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json_dump(value, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_staging_candidate(
    artifact: dict,
    metrics: dict,
    model_path: Path,
    metrics_path: Path,
) -> dict:
    """Persist one Almaz-like candidate with metrics tied to model bytes."""

    atomic_joblib_dump(artifact, model_path)
    saved_metrics = dict(metrics)
    saved_metrics["model_sha256"] = file_sha256(model_path)
    atomic_json_dump(saved_metrics, metrics_path)
    return saved_metrics


def production_trained_at(metrics_path: Path) -> str | None:
    if not metrics_path.exists():
        return None
    try:
        return json.loads(metrics_path.read_text(encoding="utf-8")).get("trained_at")
    except (OSError, ValueError, TypeError):
        return None


def _validate_reused_point_artifact(
    artifact: dict,
    *,
    profile: TrainingProfile,
    feature_columns: list[str],
    split_contract: dict,
) -> None:
    """Prevent a point-only shortcut from crossing item-kind boundaries."""

    if artifact.get("model_name") != profile.model_name:
        raise ValueError("Reused point artifact belongs to another model")
    if artifact.get("item_kind") != profile.item_kind:
        raise ValueError("Reused point artifact belongs to another item kind")
    source = artifact.get("source_database")
    if not isinstance(source, dict) or source.get("item_kind") != profile.item_kind:
        raise ValueError("Reused point artifact source belongs to another item kind")
    if source.get("item_kind_policy") != STRICT_ITEM_KIND_POLICY:
        raise ValueError("Reused point artifact source is not strictly classified")
    if artifact.get("target_mode") != TARGET_MODE:
        raise ValueError("Reused point artifact has a different target_mode")
    if list(artifact.get("feature_columns") or []) != feature_columns:
        raise ValueError("Reused point artifact has a different feature contract")
    if artifact.get("split_contract", {}).get("holdout_after") != split_contract.get(
        "holdout_after"
    ):
        raise ValueError("Reused point artifact has a different holdout boundary")


def parse_args(
    argv=None,
    *,
    default_item_kind: str = ITEM_KIND_WEAPON_SKIN,
) -> argparse.Namespace:
    training_profile(default_item_kind)
    parser = argparse.ArgumentParser(
        description="Train an item-kind-isolated leakage-resistant price model"
    )
    parser.add_argument("--db-path", type=Path, default=Path(DB_PATH))
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--item-kind",
        choices=tuple(TRAINING_PROFILES),
        default=default_item_kind,
        help="Train only this catalog category; knives never share skin rows",
    )
    parser.add_argument("--holdout-after", default=None, help="ISO timestamp; defaults to production trained_at")
    parser.add_argument("--fractional-holdout", action="store_true", help="Use newest 20%% instead of production timestamp")
    parser.add_argument(
        "--test-fraction",
        type=float,
        default=TEST_FRACTION,
        help="Newest chronological fraction reserved as untouched test data",
    )
    parser.add_argument(
        "--test-rows",
        type=int,
        default=None,
        help="Reserve at least this many newest rows (timestamp ties stay together)",
    )
    parser.add_argument("--max-iter", type=int, default=700)
    parser.add_argument("--point-only", action="store_true", help="Skip q15/q85 models for a lower-memory experiment")
    parser.add_argument(
        "--reuse-point-model",
        type=Path,
        default=None,
        help="Reuse a proven staging point artifact and train only q15/q85 on the identical split",
    )
    parser.add_argument("--max-train-rows", type=int, default=0, help="Use only newest N train rows (smoke tests only)")
    parser.add_argument("--skip-production-comparison", action="store_true")
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print per-iteration sklearn progress for the dashboard job monitor",
    )
    return parser.parse_args(argv)


def run_training(args: argparse.Namespace) -> dict:
    if not 0.01 <= float(args.test_fraction) <= 0.25:
        raise ValueError("--test-fraction must be between 0.01 and 0.25")
    if args.test_rows is not None and args.test_rows < 1_000:
        raise ValueError("--test-rows must be at least 1000")
    profile = training_profile(
        getattr(args, "item_kind", ITEM_KIND_WEAPON_SKIN)
    )
    configured_output_dir = args.output_dir or profile.staging_dir
    output_dir = configured_output_dir.expanduser().resolve()
    output_model_path = output_dir / profile.model_path.name
    output_metrics_path = output_dir / profile.metrics_path.name
    production_dir = model_generations.active_or_legacy_bundle_dir(
        profile.model_name,
        profile.model_dir,
        project_dir=app_config.PROJECT_DIR,
    )
    production_model_path = production_dir / profile.model_path.name
    production_metrics_path = production_dir / profile.metrics_path.name
    db_path = args.db_path.expanduser().resolve()
    fingerprint_before = source_database_fingerprint(
        db_path, item_kind=profile.item_kind
    )

    print(f"Source DB: {db_path}", flush=True)
    print(
        f"DB rows={fingerprint_before['sales_rows']:,}, max sold_at={fingerprint_before['max_sold_at']}",
        flush=True,
    )
    print("Building features...", flush=True)
    df = add_reference_target(build_dataset(db_path, item_kind=profile.item_kind))
    dropped_without_reference = (
        fingerprint_before["source_valid_price_rows"] - len(df)
    )

    holdout_after = None
    if args.test_rows is None and not args.fractional_holdout:
        holdout_after = args.holdout_after or production_trained_at(
            production_metrics_path
        )
    base_train, base_validation, base_test, split_contract = chronological_train_validation_test_split(
        df,
        holdout_after=holdout_after,
        test_fraction=float(args.test_fraction),
        test_rows=args.test_rows,
    )
    if args.max_train_rows and len(base_train) > args.max_train_rows:
        base_train = base_train.iloc[-args.max_train_rows:].copy()
        split_contract["train_rows_before_smoke_limit"] = split_contract["train_rows"]
        split_contract["train_rows"] = len(base_train)
        split_contract["smoke_train_limit"] = int(args.max_train_rows)

    categorical, numeric = get_feature_columns()
    feature_columns = categorical + numeric
    if RUNTIME_UNSAFE_FEATURES.intersection(feature_columns):
        raise AssertionError("Runtime-unsafe features entered the staging contract")

    train_market_stats = compute_market_stats(base_train)
    development_market_stats = compute_market_stats(
        pd.concat([base_train, base_validation], ignore_index=True)
    )
    # Test rows receive only pre-holdout aggregates.  The staging artifact,
    # however, is an inference snapshot and may use every sale already known at
    # deployment time.  Keeping these two contracts separate prevents leakage
    # in metrics without shipping stale runtime liquidity/price statistics.
    inference_market_stats = compute_market_stats(df)
    train_df = apply_time_safe_training_market_stats(base_train, train_market_stats)
    validation_df = apply_market_stats(base_validation, train_market_stats)
    test_df = apply_market_stats(base_test, development_market_stats)
    train_df = prepare_runtime_parity_backtest(train_df)
    validation_df = prepare_runtime_parity_backtest(validation_df)
    test_df = prepare_runtime_parity_backtest(test_df)

    x_train = train_df[feature_columns]
    target_column = "log_premium" if TARGET_MODE == "log_premium" else "log_price"
    y_train = train_df[target_column].to_numpy(dtype=float)
    print(
        f"Split: train={len(train_df):,}, validation={len(validation_df):,}, "
        f"untouched test={len(test_df):,}",
        flush=True,
    )
    reused_point_path = None
    if args.reuse_point_model:
        reused_point_path = args.reuse_point_model.expanduser().resolve()
        proven_artifact = joblib.load(reused_point_path)
        _validate_reused_point_artifact(
            proven_artifact,
            profile=profile,
            feature_columns=feature_columns,
            split_contract=split_contract,
        )
        print(f"Reusing proven point model: {reused_point_path}", flush=True)
        price_pipeline = proven_artifact["pipeline"]
        calibration = dict(proven_artifact.get("prediction_calibration") or {})
    else:
        print(f"Training point model ({args.max_iter} iterations, target={TARGET_MODE})...", flush=True)
        price_pipeline = make_price_pipeline(
            categorical,
            numeric,
            max_iter=args.max_iter,
            verbose=1 if args.progress else 0,
        )
        price_pipeline.fit(x_train, y_train)
        validation_raw = price_pipeline.predict(validation_df[feature_columns])
        calibration = fit_validation_calibration(
            validation_df,
            validation_raw,
            target_mode=TARGET_MODE,
        )
    low_pipeline = None
    high_pipeline = None
    if not args.point_only:
        print("Training q15 model...", flush=True)
        low_pipeline = make_price_pipeline(
            categorical,
            numeric,
            loss="quantile",
            quantile=0.15,
            max_iter=args.max_iter,
            verbose=1 if args.progress else 0,
        )
        low_pipeline.fit(x_train, y_train)
        print("Training q85 model...", flush=True)
        high_pipeline = make_price_pipeline(
            categorical,
            numeric,
            loss="quantile",
            quantile=0.85,
            max_iter=args.max_iter,
            verbose=1 if args.progress else 0,
        )
        high_pipeline.fit(x_train, y_train)

        validation_target = validation_df[target_column].to_numpy(dtype=float)
        scale = calibration["premium_scale"] if TARGET_MODE == "log_premium" else 1.0
        calibration["low_offset"] = float(
            np.quantile(validation_target - low_pipeline.predict(validation_df[feature_columns]) * scale, 0.15)
        )
        calibration["high_offset"] = float(
            np.quantile(validation_target - high_pipeline.predict(validation_df[feature_columns]) * scale, 0.85)
        )

    trained_at = datetime.now(timezone.utc).isoformat()
    artifact = {
        "artifact_version": ARTIFACT_VERSION,
        "trained_at": trained_at,
        "model_name": profile.model_name,
        "item_kind": profile.item_kind,
        "stage": "staging",
        "production_promoted": False,
        "pipeline": price_pipeline,
        "low_pipeline": low_pipeline,
        "high_pipeline": high_pipeline,
        "target_mode": TARGET_MODE,
        "prediction_calibration": calibration,
        "feature_columns": feature_columns,
        "categorical_columns": categorical,
        "numeric_columns": numeric,
        "max_stickers": MAX_STICKERS,
        "market_stats": inference_market_stats,
        "temporal_feature_contract": {
            "legacy_feature_prefix": "sold_",
            "source_priority": list(OBSERVATION_TIMESTAMP_KEYS),
            "sold_at_use": "chronological_split_and_label_only",
        },
        "evaluation_market_stats_cutoff": split_contract["holdout_after"],
        "source_database": fingerprint_before,
        "split_contract": split_contract,
    }

    staging_test_metrics, staging_test_log = evaluate_artifact_pipeline(artifact, test_df)
    validation_metrics, _ = evaluate_artifact_pipeline(artifact, validation_df)
    comparison = None
    if not args.skip_production_comparison and production_model_path.exists():
        print("Comparing production and staging on the identical untouched holdout...", flush=True)
        production = joblib.load(production_model_path)
        production_kind = production.get("item_kind") or ITEM_KIND_WEAPON_SKIN
        if production_kind != profile.item_kind:
            raise ValueError("production model belongs to another item kind")
        old_test = apply_market_stats(base_test, production.get("market_stats", {}))
        old_test = add_reference_target(prepare_runtime_parity_backtest(old_test))
        production_metrics, _ = evaluate_artifact_pipeline(production, old_test)
        score_keys = (
            "median_absolute_percentage_error",
            "mean_absolute_percentage_error",
            "p90_absolute_percentage_error",
            "mae_cents",
        )
        deltas = {key: staging_test_metrics[key] - production_metrics[key] for key in score_keys}
        comparison = {
            "holdout_rows": len(test_df),
            "production_model_path": str(production_model_path.resolve()),
            "production_trained_at": production.get("trained_at"),
            "production": production_metrics,
            "staging": staging_test_metrics,
            "staging_minus_production": deltas,
            "staging_proves_primary_improvement": bool(
                deltas["median_absolute_percentage_error"] < 0
                and deltas["mean_absolute_percentage_error"] < 0
                and deltas["p90_absolute_percentage_error"] < 0
            ),
        }

    interval_coverage = None
    interval_crossing_rate = None
    if low_pipeline is not None and high_pipeline is not None:
        low_log = prediction_to_absolute_log(
            low_pipeline.predict(test_df[feature_columns]),
            test_df,
            target_mode=TARGET_MODE,
            calibration=calibration,
            interval="low",
        )
        high_log = prediction_to_absolute_log(
            high_pipeline.predict(test_df[feature_columns]),
            test_df,
            target_mode=TARGET_MODE,
            calibration=calibration,
            interval="high",
        )
        interval_crossing_rate = float(np.mean(low_log > high_log))
        low_log = np.minimum(low_log, staging_test_log)
        high_log = np.maximum(high_log, staging_test_log)
        y_test_log = test_df["log_price"].to_numpy(dtype=float)
        interval_coverage = float(np.mean((y_test_log >= low_log) & (y_test_log <= high_log)))

    fingerprint_after = source_database_fingerprint(
        db_path, item_kind=profile.item_kind
    )
    database_changed_during_training = (
        fingerprint_before["logical_sha256"] != fingerprint_after["logical_sha256"]
    )
    metrics = {
        "artifact_version": ARTIFACT_VERSION,
        "trained_at": trained_at,
        "model_name": profile.model_name,
        "item_kind": profile.item_kind,
        "stage": "staging",
        "production_promoted": False,
        "db_path": str(db_path),
        # `rows` is the exact time-safe source snapshot used by this trainer.
        # The fingerprint and row count are both scoped to this model's kind.
        "rows": fingerprint_before["valid_price_rows"],
        "usable_rows": len(df),
        "train_rows": len(train_df),
        "validation_rows": len(validation_df),
        "test_rows": len(test_df),
        "source_database_before": fingerprint_before,
        "source_database_after": fingerprint_after,
        "database_changed_during_training": database_changed_during_training,
        "rows_after_reference_filter": len(df),
        "dropped_rows_without_runtime_reference": int(dropped_without_reference),
        "split": split_contract,
        "target": "log1p(price_cents) - log1p(reference_anchor_cents)",
        "temporal_feature_contract": artifact["temporal_feature_contract"],
        "runtime_unsafe_features_removed": sorted(RUNTIME_UNSAFE_FEATURES),
        "max_iter": int(args.max_iter),
        "test_fraction": float(args.test_fraction),
        "requested_test_rows": args.test_rows,
        "point_only": bool(args.point_only),
        "reused_point_model": str(reused_point_path) if reused_point_path else None,
        "validation": validation_metrics,
        "test": staging_test_metrics,
        "test_interval_coverage_15_85_log": interval_coverage,
        "test_raw_quantile_crossing_rate": interval_crossing_rate,
        "prediction_calibration": calibration,
        "shared_holdout_comparison": comparison,
        "market_stats_contract": {
            "test_features": "development_only_before_holdout",
            "saved_artifact_runtime": "full_source_snapshot_at_training_time",
        },
        "notes": [
            "Train, validation, and test are chronological; test is untouched by fitting and calibration.",
            "The saved artifact is staging-only and is not loaded by the production runtime path.",
            "Only CSFloat reference snapshots updated no later than sold_at are eligible.",
            "Legacy sold_* calendar features use first-observation time; sold_at is reserved for chronological splitting and labels.",
        ],
    }
    metrics = save_staging_candidate(
        artifact,
        metrics,
        output_model_path,
        output_metrics_path,
    )
    print(f"Saved staging model: {output_model_path}", flush=True)
    print(f"Saved staging metrics: {output_metrics_path}", flush=True)
    print(json.dumps({"test": staging_test_metrics, "comparison": comparison}, indent=2), flush=True)
    return metrics


def main() -> None:
    run_training(parse_args())


if __name__ == "__main__":
    main()
