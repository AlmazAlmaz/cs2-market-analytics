import json
import math
import threading
from copy import copy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import TargetEncoder

import app_config
import model_generations
from item_classification import (
    ITEM_KIND_GLOVE,
    ITEM_KIND_KNIFE,
    ITEM_KIND_WEAPON_SKIN,
    classify_market_hash_name,
)
from price_model_data_contract import OBSERVATION_TIMESTAMP_KEYS

MODEL_PATH = app_config.PROJECT_DIR / "models" / "csfloat_price_model.joblib"
MODEL_CACHE = None
MODEL_MTIME = None
_RUNTIME_SLOT = None
_RUNTIME_SLOT_LOCK = threading.Lock()


class _RuntimeStringTargetEncoder(TargetEncoder):
    """A fitted string encoder that keeps its category lookups between rows.

    sklearn rebuilds the full vocabulary lookup for each transform. Live
    inference has only a few rows, so reuse the immutable fitted encodings.
    Other input types still go through sklearn's validation and transform.
    """

    def transform(self, X):
        if (
            not isinstance(X, pd.DataFrame)
            or len(X) == 0
            or list(X.columns) != list(self.feature_names_in_)
        ):
            return super().transform(X)
        columns = [X.iloc[:, index].to_numpy() for index in range(X.shape[1])]
        if any(not isinstance(value, str) for column in columns for value in column):
            return super().transform(X)
        result = np.empty(X.shape, dtype=np.float64)
        for index, (column, lookup) in enumerate(zip(columns, self._runtime_lookups)):
            result[:, index] = [lookup.get(value, self.target_mean_) for value in column]
        return result


def _prepare_live_prediction_pipeline(pipeline):
    """Copy a supported fitted pipeline; never change an on-disk artifact."""

    if type(pipeline) is not Pipeline:
        return pipeline
    steps = []
    changed = False
    for name, step in pipeline.steps:
        if type(step) is ColumnTransformer:
            transformers = []
            for label, encoder, columns in step.transformers_:
                if (
                    type(encoder) is TargetEncoder
                    and encoder.target_type_ == "continuous"
                    and hasattr(encoder, "feature_names_in_")
                    and all(
                        isinstance(category, str)
                        for categories in encoder.categories_
                        for category in categories
                    )
                ):
                    runtime_encoder = _RuntimeStringTargetEncoder(**encoder.get_params())
                    runtime_encoder.__dict__.update(encoder.__dict__)
                    runtime_encoder._runtime_lookups = [
                        dict(zip(categories, encodings))
                        for categories, encodings in zip(
                            encoder.categories_, encoder.encodings_
                        )
                    ]
                    encoder = runtime_encoder
                    changed = True
                transformers.append((label, encoder, columns))
            step = copy(step)
            step.transformers_ = transformers
        steps.append((name, step))
    if not changed:
        return pipeline
    prepared = copy(pipeline)
    prepared.steps = steps
    return prepared


def extract_wear_name(market_hash_name):
    if (
        isinstance(market_hash_name, str)
        and market_hash_name.endswith(")")
        and "(" in market_hash_name
    ):
        return market_hash_name.rsplit("(", 1)[1][:-1]
    return None


def extract_base_skin_name(market_hash_name):
    if (
        isinstance(market_hash_name, str)
        and market_hash_name.endswith(")")
        and "(" in market_hash_name
    ):
        return market_hash_name.rsplit("(", 1)[0].strip()
    return market_hash_name


def load_bundle_from_directory(directory):
    """Load one complete Almaz generation without changing live state."""

    path = directory / MODEL_PATH.name
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"Almaz model artifact is missing or unsafe: {path}")
    artifact = joblib.load(path)
    if not isinstance(artifact, dict) or artifact.get("model_name") != "almaz":
        raise RuntimeError("Almaz artifact has the wrong model identity")
    declared_kind = artifact.get("item_kind")
    if declared_kind is not None and declared_kind != ITEM_KIND_WEAPON_SKIN:
        raise RuntimeError("Almaz artifact is not a weapon-skin model")
    for name in ("pipeline", "low_pipeline", "high_pipeline"):
        if artifact.get(name) is not None:
            artifact[name] = _prepare_live_prediction_pipeline(artifact[name])
    artifact["_runtime_source_dir"] = str(directory)
    artifact["_runtime_generation_id"] = (
        directory.name
        if directory.parent.name == "versions"
        and model_generations.SHA256_RE.fullmatch(directory.name)
        else None
    )
    return artifact


def smoke_bundle(artifact):
    """Run all Almaz heads on a synthetic contract-valid row."""

    columns = list(artifact.get("feature_columns") or [])
    categorical = set(artifact.get("categorical_columns") or [])
    if not columns or not callable(getattr(artifact.get("pipeline"), "predict", None)):
        raise RuntimeError("Almaz artifact has no usable feature/pipeline contract")
    frame = pd.DataFrame(
        [{column: "__missing__" if column in categorical else 0.0 for column in columns}],
        columns=columns,
    )
    predictions = [artifact["pipeline"].predict(frame)[0]]
    for name in ("low_pipeline", "high_pipeline"):
        pipeline = artifact.get(name)
        if pipeline is not None:
            predictions.append(pipeline.predict(frame)[0])
    if not all(np.isfinite(float(value)) for value in predictions):
        raise RuntimeError("Almaz smoke prediction is not finite")


def _runtime_slot():
    global _RUNTIME_SLOT
    if _RUNTIME_SLOT is None:
        with _RUNTIME_SLOT_LOCK:
            if _RUNTIME_SLOT is None:
                _RUNTIME_SLOT = model_generations.RuntimeBundleSlot(
                    "almaz",
                    MODEL_PATH.parent,
                    load_bundle=load_bundle_from_directory,
                    smoke_bundle=smoke_bundle,
                    project_dir=app_config.PROJECT_DIR,
                )
    return _RUNTIME_SLOT


def load_model():
    global MODEL_CACHE, MODEL_MTIME

    MODEL_CACHE = _runtime_slot().get()
    source = MODEL_CACHE.get("_runtime_source_dir")
    path = (Path(source) if source else MODEL_PATH.parent) / MODEL_PATH.name
    MODEL_MTIME = path.stat().st_mtime
    return MODEL_CACHE


def parse_price_cents(value):
    if value in (None, ""):
        return None
    try:
        number = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return int(round(number * 100))


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def inference_observation_datetime(payload, *, fallback=None):
    """Return the earliest timestamp that a live inference request can know."""

    for key in OBSERVATION_TIMESTAMP_KEYS:
        parsed = parse_iso_datetime(payload.get(key))
        if parsed is not None:
            return parsed
    return fallback or datetime.now(timezone.utc)


def to_float(value, default=0.0):
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return default


def to_int(value, default=0):
    if value in (None, ""):
        return default
    try:
        return int(round(float(str(value).replace(",", ".").strip())))
    except (TypeError, ValueError):
        return default


def normalize_sticker_wear(value):
    if value in (None, ""):
        return -1.0
    try:
        wear = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return -1.0
    if wear < 0:
        return -1.0
    if wear > 1:
        wear = wear / 100
    return min(1.0, max(0.0, wear))


def sticker_wear_multiplier(value):
    wear = normalize_sticker_wear(value)
    if wear < 0:
        return 1.0
    if wear <= 0.001:
        return 1.0
    return max(0.05, (1.0 - wear) ** 2.35)


def effective_sticker_price_cents(price_cents, wear):
    price = to_float(price_cents, 0)
    if price <= 0:
        return 0
    return int(round(price * sticker_wear_multiplier(wear)))


def parse_market_hash_name(text):
    text = (text or "").strip()
    if not text:
        return ""

    if " | " in text and not text.lower().startswith("http"):
        return text

    try:
        parsed = urlparse(text)
    except ValueError:
        return text if " | " in text else ""

    query = parse_qs(parsed.query)
    for key in ("market_hash_name", "name", "item_name", "hash_name"):
        value = query.get(key, [""])[0]
        if value:
            return unquote(value)

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    for idx, part in enumerate(parts):
        if part == "history" and idx + 1 < len(parts):
            return parts[idx + 1]
        if " | " in part:
            return part

    return text if " | " in text else ""


def parse_stickers(raw_stickers):
    if not raw_stickers:
        return []

    if isinstance(raw_stickers, str):
        raw_stickers = raw_stickers.strip()
        if not raw_stickers:
            return []
        parsed = json.loads(raw_stickers)
    else:
        parsed = raw_stickers

    if isinstance(parsed, dict):
        for key_path in (
            ("item", "stickers"),
            ("data", "item", "stickers"),
            ("listing", "item", "stickers"),
            ("stickers",),
        ):
            current = parsed
            for key in key_path:
                if not isinstance(current, dict):
                    current = None
                    break
                current = current.get(key)
            if isinstance(current, list):
                parsed = current
                break

    if not isinstance(parsed, list):
        return []

    stickers = []
    for index, sticker in enumerate(parsed[:5]):
        if not isinstance(sticker, dict):
            continue
        reference = sticker.get("reference") or sticker.get("scm") or {}
        if not isinstance(reference, dict):
            reference = {}
        sticker_id = (
            sticker.get("sticker_id")
            or sticker.get("stickerId")
            or sticker.get("id")
            or sticker.get("i")
        )
        name = sticker.get("name") or sticker.get("n") or ""
        price_cents = (
            sticker.get("price_cents")
            or sticker.get("reference_price_cents")
            or sticker.get("price")
            or reference.get("price")
        )
        price_usd = sticker.get("price_usd")
        if price_usd not in (None, ""):
            price_cents = parse_price_cents(price_usd)
        elif price_cents not in (None, ""):
            try:
                price_cents = int(round(float(price_cents)))
            except (TypeError, ValueError):
                price_cents = 0
        else:
            price_cents = 0

        wear = normalize_sticker_wear(sticker.get("wear", sticker.get("w", -1)))
        effective_price = effective_sticker_price_cents(price_cents or 0, wear)

        stickers.append(
            {
                "sticker_id": sticker_id,
                "name": name,
                "slot": sticker.get("slot", sticker.get("s", index)),
                "wear": wear,
                "price_cents": effective_price,
                "raw_price_cents": price_cents or 0,
                "wear_multiplier": sticker_wear_multiplier(wear),
                "reference_quantity": sticker.get("reference_quantity", 0),
                "reference_updated_at": (
                    sticker.get("reference_updated_at") or reference.get("updated_at")
                ),
                "offset_x": sticker.get("offset_x", 0),
                "offset_y": sticker.get("offset_y", 0),
            }
        )

    return stickers


def parse_keychains(raw_keychains):
    """Parse CS2 charms/keychains without mixing them into sticker slots."""

    if not raw_keychains:
        return []
    if isinstance(raw_keychains, str):
        raw_keychains = raw_keychains.strip()
        if not raw_keychains:
            return []
        parsed = json.loads(raw_keychains)
    else:
        parsed = raw_keychains
    if isinstance(parsed, dict):
        for key_path in (
            ("item", "keychains"),
            ("data", "item", "keychains"),
            ("listing", "item", "keychains"),
            ("keychains",),
            ("charms",),
        ):
            current = parsed
            for key in key_path:
                if not isinstance(current, dict):
                    current = None
                    break
                current = current.get(key)
            if isinstance(current, list):
                parsed = current
                break
    if not isinstance(parsed, list):
        return []

    result = []
    for index, keychain in enumerate(parsed[:2]):
        if not isinstance(keychain, dict):
            continue
        reference = keychain.get("reference") or keychain.get("scm") or {}
        if not isinstance(reference, dict):
            reference = {}
        price = (
            keychain.get("price_cents")
            or keychain.get("reference_price_cents")
            or keychain.get("price")
            or reference.get("price")
            or 0
        )
        try:
            price = max(0, int(round(float(price))))
        except (TypeError, ValueError):
            price = 0
        result.append(
            {
                "keychain_id": (
                    keychain.get("keychain_id")
                    or keychain.get("stickerId")
                    or keychain.get("id")
                ),
                "name": keychain.get("name") or "",
                "slot": keychain.get("slot", index),
                "pattern": keychain.get("pattern", -1),
                "price_cents": price,
                "raw_price_cents": price,
                "reference_quantity": (
                    keychain.get("reference_quantity") or reference.get("quantity") or 0
                ),
                "reference_updated_at": (
                    keychain.get("reference_updated_at") or reference.get("updated_at")
                ),
            }
        )
    return result


def lookup_skin_defaults(conn, name, float_value=None):
    if conn is None or not name:
        return {}

    requested_float = to_float(float_value, np.nan)
    requested_float = float(requested_float) if np.isfinite(requested_float) else None
    row = conn.execute(
        """
        SELECT
            base_skin_name,
            wear_name,
            reference_base_price,
            reference_predicted_price,
            reference_float_factor,
            reference_quantity,
            reference_keychain_price,
            reference_last_updated,
            item_quality,
            item_rarity,
            item_rarity_name,
            item_def_index,
            item_collection_name,
            item_type_name,
            item_is_souvenir,
            item_is_stattrak
        FROM sales
        WHERE name = ?
        ORDER BY
            CASE
                WHEN ? IS NULL OR float_value IS NULL THEN 1
                ELSE 0
            END,
            CASE
                WHEN ? IS NULL OR float_value IS NULL THEN 0
                ELSE ABS(float_value - ?)
            END,
            fetched_at DESC,
            sold_at DESC
        LIMIT 1
    """,
        (name, requested_float, requested_float, requested_float),
    ).fetchone()

    return dict(row) if row else {}


def enrich_stickers_from_db(conn, stickers):
    if conn is None:
        return stickers

    enriched = []
    for sticker in stickers:
        item = dict(sticker)
        sticker_id = item.get("sticker_id")
        name = item.get("name") or ""
        params = []
        where = ""
        if sticker_id not in (None, ""):
            where = "sticker_id = ?"
            params.append(sticker_id)
        elif name:
            where = "name = ?"
            params.append(name)

        if where:
            row = conn.execute(
                f"""
                SELECT
                    sticker_id,
                    name,
                    AVG(COALESCE(price_cents, reference_price_cents)) AS avg_price_cents,
                    MAX(reference_quantity) AS reference_quantity
                FROM sale_stickers
                WHERE {where}
                GROUP BY sticker_id, name
                ORDER BY COUNT(*) DESC
                LIMIT 1
            """,
                params,
            ).fetchone()
            if row:
                if item.get("sticker_id") in (None, ""):
                    item["sticker_id"] = row["sticker_id"]
                if not item.get("name"):
                    item["name"] = row["name"]
                if not item.get("price_cents"):
                    raw_price = int(round(row["avg_price_cents"] or 0))
                    item["raw_price_cents"] = raw_price
                    item["price_cents"] = effective_sticker_price_cents(
                        raw_price, item.get("wear")
                    )
                    item["wear_multiplier"] = sticker_wear_multiplier(item.get("wear"))
                if not item.get("reference_quantity"):
                    item["reference_quantity"] = row["reference_quantity"] or 0
        enriched.append(item)

    return enriched


def enrich_keychains_from_db(conn, keychains):
    if conn is None:
        return keychains
    enriched = []
    for keychain in keychains:
        item = dict(keychain)
        keychain_id = item.get("keychain_id")
        name = item.get("name") or ""
        if keychain_id not in (None, ""):
            where, value = "keychain_id = ?", keychain_id
        elif name:
            where, value = "name = ?", name
        else:
            enriched.append(item)
            continue
        row = conn.execute(
            f"""
            SELECT keychain_id, name, AVG(price_cents) AS avg_price_cents,
                   MAX(reference_quantity) AS reference_quantity,
                   MAX(reference_updated_at) AS reference_updated_at
            FROM sale_keychains
            WHERE {where}
            GROUP BY keychain_id, name
            ORDER BY COUNT(*) DESC
            LIMIT 1
        """,
            (value,),
        ).fetchone()
        if row:
            if item.get("keychain_id") in (None, ""):
                item["keychain_id"] = row["keychain_id"]
            if not item.get("name"):
                item["name"] = row["name"]
            if not item.get("price_cents"):
                item["price_cents"] = int(round(row["avg_price_cents"] or 0))
                item["raw_price_cents"] = item["price_cents"]
            if not item.get("reference_quantity"):
                item["reference_quantity"] = row["reference_quantity"] or 0
            if not item.get("reference_updated_at"):
                item["reference_updated_at"] = row["reference_updated_at"]
        enriched.append(item)
    return enriched


def lookup_comparable_sales(conn, skin_name, stickers):
    if conn is None or not skin_name or not stickers:
        return {
            "count": 0,
            "median_price_cents": None,
            "avg_price_cents": None,
            "min_price_cents": None,
            "max_price_cents": None,
            "recent": [],
        }

    sticker_ids = [
        sticker.get("sticker_id")
        for sticker in stickers
        if sticker.get("sticker_id") not in (None, "")
    ]
    sticker_names = [sticker.get("name") for sticker in stickers if sticker.get("name")]
    conditions = []
    params = [skin_name]

    if sticker_ids:
        placeholders = ", ".join("?" for _ in sticker_ids)
        conditions.append(f"st.sticker_id IN ({placeholders})")
        params.extend(sticker_ids)
    if sticker_names:
        placeholders = ", ".join("?" for _ in sticker_names)
        conditions.append(f"st.name IN ({placeholders})")
        params.extend(sticker_names)

    if not conditions:
        return {
            "count": 0,
            "median_price_cents": None,
            "avg_price_cents": None,
            "min_price_cents": None,
            "max_price_cents": None,
            "recent": [],
        }

    where = " OR ".join(conditions)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            WITH matched AS (
                SELECT
                    s.listing_id,
                    s.name,
                    s.price_cents,
                    s.float_value,
                    s.paint_seed,
                    s.paint_index,
                    s.sold_at,
                    COUNT(st.sticker_index) AS matched_sticker_rows,
                    (SELECT COUNT(*) FROM sale_stickers all_st WHERE all_st.listing_id = s.listing_id)
                        AS total_sticker_rows,
                    SUM(COALESCE(st.price_cents, st.reference_price_cents, 0)) AS matched_sticker_value_cents
                FROM sales s
                JOIN sale_stickers st ON st.listing_id = s.listing_id
                WHERE s.name = ?
                    AND s.state = 'sold'
                    AND ({where})
                GROUP BY s.listing_id
            )
            SELECT *
            FROM matched
            ORDER BY sold_at DESC
            LIMIT 200
        """,
            params,
        )
    ]

    required_matches = max(1, math.ceil(len(stickers) * 0.60))
    rows = [
        row
        for row in rows
        if int(row.get("matched_sticker_rows") or 0) >= required_matches
        and abs(int(row.get("total_sticker_rows") or 0) - len(stickers)) <= 1
    ]
    prices = sorted(int(row["price_cents"]) for row in rows if row.get("price_cents"))
    if not prices:
        return {
            "count": 0,
            "median_price_cents": None,
            "avg_price_cents": None,
            "min_price_cents": None,
            "max_price_cents": None,
            "recent": [],
        }

    median = float(np.median(prices))
    return {
        "count": len(prices),
        "median_price_cents": median,
        "avg_price_cents": float(np.mean(prices)),
        "min_price_cents": int(min(prices)),
        "max_price_cents": int(max(prices)),
        "recent": rows[:25],
    }


def estimate_craft_floor_cents(row, stickers):
    sticker_value = float(row.get("sticker_price_sum") or 0)
    if sticker_value < 2000 or not stickers:
        return None, None

    names = [str(sticker.get("name") or "") for sticker in stickers]
    known_wears = [normalize_sticker_wear(sticker.get("wear")) for sticker in stickers]
    known_wears = [wear for wear in known_wears if wear >= 0]
    avg_wear = float(np.mean(known_wears)) if known_wears else 0.0
    sticker_count = len(stickers)
    kato14_count = sum("Katowice 2014" in name for name in names)
    kato15_count = sum("Katowice 2015" in name for name in names)
    holo_count = sum(
        "(Holo)" in name or "(Gold)" in name or "(Foil)" in name for name in names
    )
    same_max = int(row.get("same_sticker_max_count") or 0)

    if sticker_value >= 10_000_000:
        rate = 0.0065
        cap = 0.012
    elif sticker_value >= 1_000_000:
        rate = 0.014
        cap = 0.026
    elif sticker_value >= 100_000:
        rate = 0.037
        cap = 0.095
    elif sticker_value >= 10_000:
        rate = 0.090
        cap = 0.220
    else:
        rate = 0.120
        cap = 0.280

    if kato14_count and sticker_value < 1_000_000:
        rate = max(rate, 0.030)
    elif kato14_count:
        rate = max(rate, 0.010)
    elif kato15_count and sticker_value < 1_000_000:
        rate = max(rate, 0.020)

    if sticker_count >= 4:
        rate += 0.002 if sticker_value >= 1_000_000 else 0.006
    elif sticker_count == 1:
        rate -= 0.002 if sticker_value >= 1_000_000 else 0.004

    if same_max >= 3:
        rate += 0.002 if sticker_value >= 1_000_000 else 0.006

    if holo_count:
        rate += 0.0015 if sticker_value >= 1_000_000 else 0.004

    if avg_wear >= 0.45:
        rate *= 0.55
    elif avg_wear >= 0.25:
        rate *= 0.72
    elif avg_wear >= 0.10:
        rate *= 0.86

    rate = min(max(rate, 0.006), cap)
    base_price = max(
        float(row.get("reference_base_price") or 0),
        float(row.get("skin_median_price_cents") or 0),
        float(row.get("skin_avg_price_cents") or 0) * 0.85,
    )
    floor = int(round(base_price + sticker_value * rate))

    return max(1, floor), rate


def market_stat_defaults(artifact):
    global_stats = artifact.get("market_stats", {}).get("global", {})
    return {
        "skin_sales_count": 0,
        "skin_avg_price_cents": global_stats.get("avg_price_cents", 0),
        "skin_median_price_cents": global_stats.get("median_price_cents", 0),
        "skin_min_price_cents": global_stats.get("median_price_cents", 0),
        "skin_max_price_cents": global_stats.get("median_price_cents", 0),
        "skin_avg_float": -1,
        "skin_stickered_share": 0,
        "skin_avg_sticker_value_cents": 0,
        "skin_days_span": 0,
        "skin_sales_per_day": 0,
        "skin_liquidity_score": 0,
    }


def get_skin_market_stats(artifact, name):
    market_stats = artifact.get("market_stats", {})
    global_stats = market_stats.get("global", {})
    max_sales_per_day = max(float(global_stats.get("max_sales_per_day") or 1), 1)
    stats = market_stat_defaults(artifact)
    stats.update(market_stats.get("skin", {}).get(name, {}))
    stats["skin_liquidity_score"] = min(
        100,
        max(
            0,
            np.log1p(float(stats.get("skin_sales_per_day") or 0))
            / np.log1p(max_sales_per_day)
            * 100,
        ),
    )
    return stats


def normalize_bool(value):
    if isinstance(value, bool):
        return int(value)
    if value in (None, ""):
        return 0
    return 1 if str(value).strip().lower() in ("1", "true", "yes", "on") else 0


def build_feature_row(payload, artifact, conn=None):
    url_text = (
        payload.get("url_text") or payload.get("url") or payload.get("link") or ""
    )
    name = (payload.get("name") or parse_market_hash_name(url_text)).strip()
    if not name:
        raise ValueError(
            "Skin name is required. Paste exact market_hash_name or fill Name."
        )

    defaults = lookup_skin_defaults(conn, name, payload.get("float_value"))
    now = datetime.now(timezone.utc)
    observation_dt = inference_observation_datetime(payload, fallback=now)
    stickers = enrich_stickers_from_db(conn, parse_stickers(payload.get("stickers")))

    reference_base_price = to_float(
        payload.get("reference_base_price"), defaults.get("reference_base_price") or 0
    )
    sticker_prices = [
        effective_sticker_price_cents(
            sticker.get("raw_price_cents", sticker.get("price_cents")),
            sticker.get("wear"),
        )
        for sticker in stickers
    ]
    for sticker, effective_price in zip(stickers, sticker_prices):
        if "raw_price_cents" not in sticker:
            sticker["raw_price_cents"] = int(sticker.get("price_cents") or 0)
        sticker["price_cents"] = effective_price
        sticker["wear"] = normalize_sticker_wear(sticker.get("wear"))
        sticker["wear_multiplier"] = sticker_wear_multiplier(sticker.get("wear"))
    sticker_price_sum = sum(sticker_prices)
    sticker_price_max = max(sticker_prices) if sticker_prices else 0
    sticker_price_mean = (
        sticker_price_sum / len(sticker_prices) if sticker_prices else 0
    )
    sticker_price_median = float(np.median(sticker_prices)) if sticker_prices else 0
    sticker_names = [str(sticker.get("name") or "") for sticker in stickers]
    sticker_ids = [str(sticker.get("sticker_id") or "none") for sticker in stickers]

    created_at = parse_iso_datetime(
        payload.get("created_at") or payload.get("listed_at")
    )
    listing_age_hours = (
        max(0.0, (observation_dt - created_at).total_seconds() / 3600)
        if created_at
        else -1.0
    )
    row = {
        "name": name,
        "base_skin_name": payload.get("base_skin_name")
        or defaults.get("base_skin_name")
        or extract_base_skin_name(name)
        or "unknown",
        "wear_name": payload.get("wear_name")
        or defaults.get("wear_name")
        or extract_wear_name(name)
        or "unknown",
        "paint_index": str(
            payload.get("paint_index")
            if payload.get("paint_index") not in (None, "")
            else -1
        ),
        "float_value": to_float(payload.get("float_value"), -1),
        "paint_seed": to_int(payload.get("paint_seed"), -1),
        "reference_base_price": reference_base_price,
        "reference_predicted_price": to_float(
            payload.get("reference_predicted_price"),
            defaults.get("reference_predicted_price") or 0,
        ),
        "reference_float_factor": to_float(
            payload.get("reference_float_factor"),
            defaults.get("reference_float_factor") or 1,
        ),
        "reference_quantity": to_float(
            payload.get("reference_quantity"), defaults.get("reference_quantity") or 0
        ),
        "watchers": to_float(payload.get("watchers"), 0),
        "item_quality": payload.get("item_quality")
        or defaults.get("item_quality")
        or "unknown",
        "item_rarity": to_float(
            payload.get("item_rarity"), defaults.get("item_rarity") or -1
        ),
        "item_rarity_name": payload.get("item_rarity_name")
        or defaults.get("item_rarity_name")
        or "unknown",
        "item_def_index": to_float(
            payload.get("item_def_index"), defaults.get("item_def_index") or -1
        ),
        "item_collection_name": payload.get("item_collection_name")
        or defaults.get("item_collection_name")
        or "unknown",
        "item_type_name": payload.get("item_type_name")
        or defaults.get("item_type_name")
        or "unknown",
        "item_is_souvenir": normalize_bool(
            payload.get("item_is_souvenir", defaults.get("item_is_souvenir", 0))
        ),
        "item_is_stattrak": normalize_bool(
            payload.get("item_is_stattrak", defaults.get("item_is_stattrak", 0))
        )
        or int("StatTrak" in name),
        "sticker_count": len(stickers),
        "distinct_sticker_count": len(set(sticker_ids)),
        "sticker_price_sum": sticker_price_sum,
        "sticker_price_max": sticker_price_max,
        "sticker_price_mean": sticker_price_mean,
        "sticker_price_median": sticker_price_median,
        "same_sticker_max_count": max(
            [sticker_ids.count(sticker_id) for sticker_id in set(sticker_ids)] or [0]
        ),
        "katowice_2014_count": sum("Katowice 2014" in name for name in sticker_names),
        "katowice_2015_count": sum("Katowice 2015" in name for name in sticker_names),
        "crown_count": sum("crown" in name.lower() for name in sticker_names),
        "holo_count": sum("(Holo)" in name for name in sticker_names),
        "foil_count": sum("(Foil)" in name for name in sticker_names),
        "gold_count": sum("(Gold)" in name for name in sticker_names),
        "known_sticker_price_count": sum(1 for value in sticker_prices if value > 0),
        "known_sticker_wear_count": sum(
            1 for sticker in stickers if sticker.get("wear") not in (None, "", -1)
        ),
        "sold_year": observation_dt.year,
        "sold_month": observation_dt.month,
        "sold_dayofweek": observation_dt.weekday(),
        "sold_hour": observation_dt.hour,
        "sold_days_since_epoch": observation_dt.timestamp() / 86400,
        "listing_age_hours": listing_age_hours,
        "sticker_value_to_base": sticker_price_sum / reference_base_price
        if reference_base_price > 0
        else 0,
    }
    row.update(get_skin_market_stats(artifact, name))

    for idx in range(artifact.get("max_stickers", 5)):
        if idx < len(stickers):
            sticker = stickers[idx]
            row[f"sticker_id_{idx}"] = str(sticker.get("sticker_id") or "none")
            row[f"sticker_name_{idx}"] = sticker.get("name") or "none"
            row[f"sticker_slot_{idx}"] = to_float(sticker.get("slot"), idx)
            row[f"sticker_wear_{idx}"] = to_float(sticker.get("wear"), -1)
            row[f"sticker_price_{idx}"] = to_float(sticker.get("price_cents"), 0)
            row[f"sticker_reference_quantity_{idx}"] = to_float(
                sticker.get("reference_quantity"), 0
            )
            row[f"sticker_offset_x_{idx}"] = to_float(sticker.get("offset_x"), 0)
            row[f"sticker_offset_y_{idx}"] = to_float(sticker.get("offset_y"), 0)
        else:
            row[f"sticker_id_{idx}"] = "none"
            row[f"sticker_name_{idx}"] = "none"
            row[f"sticker_slot_{idx}"] = -1
            row[f"sticker_wear_{idx}"] = -1
            row[f"sticker_price_{idx}"] = 0
            row[f"sticker_reference_quantity_{idx}"] = 0
            row[f"sticker_offset_x_{idx}"] = 0
            row[f"sticker_offset_y_{idx}"] = 0

    return row, stickers


def prepare_feature_frame(row, artifact):
    feature_columns = list(artifact["feature_columns"])
    categorical_columns = set(artifact.get("categorical_columns") or [])
    numeric_columns = set(artifact.get("numeric_columns") or [])

    frame = pd.DataFrame([row])
    for column in feature_columns:
        if column not in frame.columns:
            frame[column] = None

    x = frame[feature_columns].copy()

    for column in feature_columns:
        if column in categorical_columns:
            x[column] = x[column].where(pd.notna(x[column]), "none").astype(str)
        elif column in numeric_columns:
            x[column] = pd.to_numeric(x[column], errors="coerce").replace(
                [np.inf, -np.inf], np.nan
            )
        else:
            x[column] = x[column].where(pd.notna(x[column]), "none")

    return x


def reference_anchor_cents(row):
    reference = to_float(row.get("reference_predicted_price"), 0)
    base = to_float(row.get("reference_base_price"), 0)
    return reference if reference > 0 else base if base > 0 else 1.0


def absolute_log_prediction(raw_prediction, row, artifact, interval="center"):
    """Apply the training artifact's target/reconstruction contract exactly."""

    calibration = artifact.get("prediction_calibration") or {}
    offset = float(
        calibration.get(f"{interval}_offset", calibration.get("center_offset", 0.0))
    )
    if artifact.get("target_mode", "log_price") == "log_premium":
        scale = float(calibration.get("premium_scale", 1.0))
        return (
            math.log1p(reference_anchor_cents(row))
            + float(raw_prediction) * scale
            + offset
        )
    return float(raw_prediction) + offset


def predict_deal_with_artifact(payload, artifact, conn=None):
    row, stickers = build_feature_row(payload, artifact, conn)
    artifact_item_kind = artifact.get("item_kind") or ITEM_KIND_WEAPON_SKIN
    actual_item_kind = classify_market_hash_name(row["name"], conn=conn)
    if actual_item_kind != artifact_item_kind:
        raise ValueError(
            f"{artifact_item_kind} model cannot evaluate {actual_item_kind} items"
        )
    x = prepare_feature_frame(row, artifact)
    predicted_log = absolute_log_prediction(
        artifact["pipeline"].predict(x)[0], row, artifact, interval="center"
    )
    low_log = (
        absolute_log_prediction(
            artifact["low_pipeline"].predict(x)[0], row, artifact, interval="low"
        )
        if artifact.get("low_pipeline")
        else predicted_log
    )
    high_log = (
        absolute_log_prediction(
            artifact["high_pipeline"].predict(x)[0], row, artifact, interval="high"
        )
        if artifact.get("high_pipeline")
        else predicted_log
    )

    ml_predicted_cents = max(1, int(round(np.expm1(predicted_log))))
    low_cents = max(1, int(round(np.expm1(min(low_log, high_log)))))
    high_cents = max(low_cents, int(round(np.expm1(max(low_log, high_log)))))
    comparable_sales = lookup_comparable_sales(conn, row["name"], stickers)

    craft_floor_cents, craft_applied_rate = estimate_craft_floor_cents(row, stickers)

    predicted_cents = ml_predicted_cents
    pricing_method = "model"
    if comparable_sales["count"] >= 12:
        predicted_cents = int(
            round(
                ml_predicted_cents * 0.35
                + comparable_sales["median_price_cents"] * 0.65
            )
        )
        pricing_method = "model_plus_strong_comps"
    elif comparable_sales["count"] >= 4:
        predicted_cents = int(
            round(
                ml_predicted_cents * 0.60
                + comparable_sales["median_price_cents"] * 0.40
            )
        )
        pricing_method = "model_plus_comps"

    if craft_floor_cents and craft_floor_cents > predicted_cents:
        predicted_cents = int(round(predicted_cents * 0.45 + craft_floor_cents * 0.55))
        pricing_method = f"{pricing_method}_plus_craft_blend"

    low_cents = min(low_cents, predicted_cents)
    high_cents = max(high_cents, predicted_cents)
    if comparable_sales["count"] >= 4:
        low_cents = min(low_cents, int(comparable_sales["min_price_cents"]))
        high_cents = max(high_cents, int(comparable_sales["max_price_cents"]))

    buy_price_cents = parse_price_cents(payload.get("buy_price_usd")) or payload.get(
        "buy_price_cents"
    )
    buy_price_cents = (
        int(buy_price_cents) if buy_price_cents not in (None, "") else None
    )
    fee_percent = min(100.0, max(0.0, to_float(payload.get("fee_percent"), 4.0)))
    net_expected_cents = int(round(predicted_cents * (1 - fee_percent / 100)))
    profit_cents = net_expected_cents - buy_price_cents if buy_price_cents else None
    roi_percent = profit_cents / buy_price_cents * 100 if buy_price_cents else None
    liquidity_score = float(row.get("skin_liquidity_score") or 0)

    if buy_price_cents is None:
        verdict = "enter_buy_price"
    elif roi_percent >= 8 and liquidity_score >= 40:
        verdict = "good"
    elif roi_percent >= 3 and liquidity_score >= 20:
        verdict = "watch"
    elif roi_percent >= 0:
        verdict = "thin"
    else:
        verdict = "bad"

    return {
        "model_name": artifact.get("model_name") or "almaz",
        "item_kind": artifact_item_kind,
        "active_bundle_version": artifact.get("_runtime_generation_id"),
        "artifact_version": artifact.get("artifact_version"),
        "trained_at": artifact.get("trained_at"),
        "name": row["name"],
        "base_skin_name": row["base_skin_name"],
        "wear_name": row["wear_name"],
        "predicted_price_cents": predicted_cents,
        "ml_predicted_price_cents": ml_predicted_cents,
        "low_price_cents": low_cents,
        "high_price_cents": high_cents,
        "buy_price_cents": buy_price_cents,
        "fee_percent": fee_percent,
        "net_expected_cents": net_expected_cents,
        "profit_cents": profit_cents,
        "roi_percent": roi_percent,
        "liquidity_score": liquidity_score,
        "sales_per_day": float(row.get("skin_sales_per_day") or 0),
        "historical_sales_count": int(row.get("skin_sales_count") or 0),
        "sticker_value_cents": int(row.get("sticker_price_sum") or 0),
        "sticker_count": int(row.get("sticker_count") or 0),
        "verdict": verdict,
        "pricing_method": pricing_method,
        "craft_floor_cents": craft_floor_cents,
        "craft_applied_rate": craft_applied_rate,
        "comparable_sales": comparable_sales,
        "input_features": row,
        "stickers": stickers,
    }


def predict_deal(payload, conn=None):
    name = str(
        payload.get("name")
        or parse_market_hash_name(payload.get("url") or payload.get("text") or "")
        or ""
    ).strip()
    item_kind = classify_market_hash_name(name, conn=conn)
    if item_kind == ITEM_KIND_KNIFE:
        # Lazy import avoids a module cycle: the knife adapter reuses the
        # feature/prediction implementation above but owns a separate artifact.
        import knife_model_runtime

        return knife_model_runtime.predict_deal(payload, conn)
    if item_kind == ITEM_KIND_GLOVE:
        import glove_model_runtime

        return glove_model_runtime.predict_deal(payload, conn)
    if item_kind != ITEM_KIND_WEAPON_SKIN:
        raise ValueError("item identity is unknown; no price model can evaluate it")
    return predict_deal_with_artifact(payload, load_model(), conn)
