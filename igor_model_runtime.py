import json
import pickle
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor

import app_config
import model_generations
import price_model_runtime
from IGORMODEL import features as igor_features
from item_classification import (
    ITEM_KIND_WEAPON_SKIN,
    classify_market_hash_name,
)

IGOR_DIR = app_config.PROJECT_DIR / "IGORMODEL"
ARTIFACT_DIR = IGOR_DIR / "artifacts"
POINT_MODEL_PATH = ARTIFACT_DIR / "price_model_point.cbm"
LOW_MODEL_PATH = ARTIFACT_DIR / "price_model_q15.cbm"
HIGH_MODEL_PATH = ARTIFACT_DIR / "price_model_q85.cbm"
METADATA_PATH = ARTIFACT_DIR / "feature_metadata.pkl"

MODEL_CACHE = None
MODEL_MTIMES = None
_RUNTIME_SLOT = None
_RUNTIME_SLOT_LOCK = threading.Lock()
RUNTIME_STATS_CACHE = {}
RUNTIME_STATS_TTL_SECONDS = 600


def validate_metadata_contract(metadata):
    declared_kind = metadata.get("item_kind")
    if declared_kind is not None and declared_kind != ITEM_KIND_WEAPON_SKIN:
        raise RuntimeError("Igor artifact is not a weapon-skin model")
    artifact_version = int(metadata.get("artifact_version", 1))
    contract_version = int(metadata.get("feature_contract_version", 1))
    if artifact_version < 2 and contract_version < 2:
        return

    expected_hash = igor_features.FEATURE_CONTRACT_SHA256
    actual_hash = metadata.get("feature_contract_sha256")
    if actual_hash != expected_hash:
        raise RuntimeError(
            "Igor v2 artifact/code contract mismatch: "
            f"artifact={actual_hash!r}, runtime={expected_hash!r}"
        )
    if list(metadata.get("feature_cols") or []) != list(igor_features.ALL_FEATURES):
        raise RuntimeError("Igor v2 artifact feature list does not match runtime code")
    if list(metadata.get("cat_features") or []) != list(igor_features.CAT_FEATURES):
        raise RuntimeError(
            "Igor v2 artifact categorical feature list does not match runtime code"
        )


def load_bundle_from_directory(directory):
    """Load one Igor bundle locally; callers decide whether to expose it."""

    paths = [directory / path.name for path in (
        POINT_MODEL_PATH, LOW_MODEL_PATH, HIGH_MODEL_PATH, METADATA_PATH
    )]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Igor model artifacts not found. Run: cd IGORMODEL && ../.venv/bin/python train.py. "
            f"Missing: {missing}"
        )

    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise RuntimeError("Igor bundle contains an unsafe artifact")
    with paths[3].open("rb") as handle:
        metadata = pickle.load(handle)
    validate_metadata_contract(metadata)

    point = CatBoostRegressor()
    point.load_model(str(paths[0]))
    low = CatBoostRegressor()
    low.load_model(str(paths[1]))
    high = CatBoostRegressor()
    high.load_model(str(paths[2]))
    return {
        "point": point,
        "low": low,
        "high": high,
        "metadata": metadata,
        "_runtime_source_dir": str(directory),
        "_runtime_generation_id": (
            directory.name
            if directory.parent.name == "versions"
            and model_generations.SHA256_RE.fullmatch(directory.name)
            else None
        ),
    }


def smoke_bundle(bundle):
    """Exercise all Igor prediction heads before an atomic live swap."""

    metadata = bundle.get("metadata") or {}
    feature_cols = list(metadata.get("feature_cols") or [])
    cat_cols = set(metadata.get("cat_features") or [])
    if not feature_cols:
        raise RuntimeError("Igor bundle has no feature contract")
    frame = pd.DataFrame(
        [{column: "__missing__" if column in cat_cols else 0.0 for column in feature_cols}],
        columns=feature_cols,
    )
    values = [bundle[name].predict(frame)[0] for name in ("point", "low", "high")]
    if not all(np.isfinite(float(value)) for value in values):
        raise RuntimeError("Igor smoke prediction is not finite")


def _runtime_slot():
    global _RUNTIME_SLOT
    if _RUNTIME_SLOT is None:
        with _RUNTIME_SLOT_LOCK:
            if _RUNTIME_SLOT is None:
                _RUNTIME_SLOT = model_generations.RuntimeBundleSlot(
                    "igor",
                    ARTIFACT_DIR,
                    load_bundle=load_bundle_from_directory,
                    smoke_bundle=smoke_bundle,
                    project_dir=app_config.PROJECT_DIR,
                )
    return _RUNTIME_SLOT


def load_models():
    global MODEL_CACHE, MODEL_MTIMES

    MODEL_CACHE = _runtime_slot().get()
    source = Path(MODEL_CACHE.get("_runtime_source_dir") or ARTIFACT_DIR)
    MODEL_MTIMES = tuple(
        (source / path.name).stat().st_mtime_ns
        for path in (POINT_MODEL_PATH, LOW_MODEL_PATH, HIGH_MODEL_PATH, METADATA_PATH)
    )
    return MODEL_CACHE


def strip_prefixes(name):
    value = str(name or "")
    is_stattrak = int(value.startswith("StatTrak"))
    is_souvenir = int(value.startswith("Souvenir "))
    if value.startswith("StatTrak"):
        value = (
            value.replace("StatTrak™ ", "", 1)
            .replace("StatTrak\u2122 ", "", 1)
            .replace("StatTrak ", "", 1)
        )
    if value.startswith("Souvenir "):
        value = value[len("Souvenir ") :]
    return value, is_stattrak, is_souvenir


def get_runtime_stats(conn, weapon_type):
    if conn is None:
        return {
            "recent_premium": 0.0,
            "liquidity_cumcount": 0.0,
            "historical_sales_count": 0,
            "sales_per_day": 0.0,
            "liquidity_score": 0.0,
        }

    cached = RUNTIME_STATS_CACHE.get(weapon_type)
    if cached and time.monotonic() - cached[0] < RUNTIME_STATS_TTL_SECONDS:
        return cached[1]

    rows = conn.execute(
        """
        SELECT price_cents, reference_predicted_price
        FROM sales
        WHERE base_skin_name = ?
            AND price_cents IS NOT NULL
            AND price_cents > 0
            AND reference_predicted_price IS NOT NULL
            AND reference_predicted_price > 0
        ORDER BY sold_at DESC
        LIMIT 200
    """,
        (weapon_type,),
    ).fetchall()

    premiums = [
        np.log1p(float(row["price_cents"]))
        - np.log1p(float(row["reference_predicted_price"]))
        for row in rows
        if row["price_cents"] and row["reference_predicted_price"]
    ]
    count_row = conn.execute(
        """
        SELECT COUNT(*) AS count, MIN(sold_at) AS oldest, MAX(sold_at) AS newest
        FROM sales
        WHERE base_skin_name = ?
            AND price_cents IS NOT NULL
            AND price_cents > 0
    """,
        (weapon_type,),
    ).fetchone()

    count = int(count_row["count"] if count_row else 0)
    oldest = price_model_runtime.parse_iso_datetime(
        count_row["oldest"] if count_row else None
    )
    newest = price_model_runtime.parse_iso_datetime(
        count_row["newest"] if count_row else None
    )
    day_span = (
        max((newest - oldest).total_seconds() / 86400.0, 1.0)
        if oldest and newest
        else 1.0
    )
    sales_per_day = count / day_span
    liquidity_score = min(
        100.0, max(0.0, np.log1p(sales_per_day) / np.log1p(50.0) * 100.0)
    )
    stats = {
        "recent_premium": float(np.median(premiums)) if premiums else 0.0,
        "liquidity_cumcount": float(np.log1p(count)),
        "historical_sales_count": count,
        "sales_per_day": float(sales_per_day),
        "liquidity_score": float(liquidity_score),
    }
    RUNTIME_STATS_CACHE[weapon_type] = (time.monotonic(), stats)
    return stats


def build_raw_row_with_keychains(payload, conn, *, contract_version=1):
    name = (
        payload.get("name")
        or price_model_runtime.parse_market_hash_name(payload.get("url_text") or "")
    ).strip()
    if not name:
        raise ValueError("Skin name is required.")

    defaults = price_model_runtime.lookup_skin_defaults(
        conn, name, payload.get("float_value")
    )
    now = datetime.now(timezone.utc).isoformat()
    base_skin_name = (
        payload.get("base_skin_name")
        or defaults.get("base_skin_name")
        or price_model_runtime.extract_base_skin_name(name)
        or name
    )
    wear_name = (
        payload.get("wear_name")
        or defaults.get("wear_name")
        or price_model_runtime.extract_wear_name(name)
        or "unknown"
    )
    stickers = price_model_runtime.enrich_stickers_from_db(
        conn, price_model_runtime.parse_stickers(payload.get("stickers"))
    )
    keychains = price_model_runtime.enrich_keychains_from_db(
        conn,
        price_model_runtime.parse_keychains(
            payload.get("keychains") or payload.get("charms")
        ),
    )

    if contract_version < 2:
        # Preserve production Igor/Nova v1 adapter semantics byte-for-byte if
        # the v2 candidate fails its promotion gate.
        live_keychain_component = price_model_runtime.to_float(
            payload.get("reference_keychain_price"),
            defaults.get("reference_keychain_price") or 0,
        )
        live_predicted_reference = price_model_runtime.to_float(
            payload.get("reference_predicted_price"),
            defaults.get("reference_predicted_price") or np.nan,
        )
        keychain_component_source = "legacy_v1"
    else:
        explicit_keychain_component = payload.get("reference_keychain_price")
        if explicit_keychain_component not in (None, ""):
            live_keychain_component = max(
                0.0,
                price_model_runtime.to_float(explicit_keychain_component, 0.0),
            )
            keychain_component_source = "live_payload"
        else:
            # This value is listing-specific.  Never borrow it from the nearest
            # historical sale or synthesize it from child prices: neither is the
            # listing-level aggregate used by the training contract.
            live_keychain_component = 0.0
            keychain_component_source = "none"

        explicit_predicted = payload.get("reference_predicted_price")
        if explicit_predicted not in (None, ""):
            live_predicted_reference = price_model_runtime.to_float(
                explicit_predicted, np.nan
            )
        else:
            historical_predicted = price_model_runtime.to_float(
                defaults.get("reference_predicted_price"), np.nan
            )
            historical_keychain_component = max(
                0.0,
                price_model_runtime.to_float(
                    defaults.get("reference_keychain_price"), 0.0
                ),
            )
            if (
                np.isfinite(historical_predicted)
                and historical_predicted > 0
                and historical_keychain_component < historical_predicted
            ):
                historical_predicted -= historical_keychain_component
            live_predicted_reference = historical_predicted + live_keychain_component

        coherent_component = igor_features.contract.coherent_keychain_component_series(
            pd.Series([live_predicted_reference]),
            pd.Series([live_keychain_component]),
        ).iloc[0]
        if live_keychain_component > 0 and coherent_component == 0:
            keychain_component_source = "incoherent_ignored"
        live_keychain_component = float(coherent_component)

    row = {
        "listing_id": payload.get("listing_id") or "live",
        "name": name,
        "base_skin_name": base_skin_name,
        "wear_name": wear_name,
        "price_cents": max(1, int(payload.get("buy_price_cents") or 1)),
        "reference_base_price": price_model_runtime.to_float(
            payload.get("reference_base_price"),
            defaults.get("reference_base_price") or np.nan,
        ),
        "reference_predicted_price": price_model_runtime.to_float(
            live_predicted_reference,
            np.nan,
        ),
        "reference_float_factor": price_model_runtime.to_float(
            payload.get("reference_float_factor"),
            defaults.get("reference_float_factor") or 1,
        ),
        "reference_quantity": price_model_runtime.to_float(
            payload.get("reference_quantity"), defaults.get("reference_quantity") or 0
        ),
        "reference_keychain_price": price_model_runtime.to_float(
            live_keychain_component,
            0,
        ),
        "reference_keychain_component_source": keychain_component_source,
        "reference_last_updated": (
            payload.get("reference_last_updated")
            or defaults.get("reference_last_updated")
        ),
        # A live API reference is available at inference time even when its
        # upstream payload omits an explicit updated_at field.
        "reference_is_runtime_observed": 1,
        "float_value": price_model_runtime.to_float(payload.get("float_value"), -1),
        "paint_seed": (
            payload.get("paint_seed")
            if payload.get("paint_seed") not in (None, "")
            else -1
        ),
        "paint_index": (
            payload.get("paint_index")
            if payload.get("paint_index") not in (None, "")
            else -1
        ),
        "state": "listed",
        "created_at": payload.get("created_at") or payload.get("listed_at") or now,
        "sold_at": now,
        "fetched_at": now,
        "sale_type": payload.get("sale_type") or "live",
        "is_seller": 0,
        "is_watchlisted": 0,
        "watchers": price_model_runtime.to_float(payload.get("watchers"), 0),
        "item_def_index": price_model_runtime.to_float(
            payload.get("item_def_index"), defaults.get("item_def_index") or -1
        ),
        "item_rarity_name": payload.get("item_rarity_name")
        or defaults.get("item_rarity_name")
        or "unknown",
        "item_collection_name": payload.get("item_collection_name")
        or defaults.get("item_collection_name")
        or "unknown",
        "item_is_stattrak": price_model_runtime.normalize_bool(
            payload.get("item_is_stattrak", defaults.get("item_is_stattrak", 0))
        ),
        "item_is_souvenir": price_model_runtime.normalize_bool(
            payload.get("item_is_souvenir", defaults.get("item_is_souvenir", 0))
        ),
    }

    reference_value = row["reference_predicted_price"]
    base_value = row["reference_base_price"]
    if not (
        np.isfinite(reference_value)
        and reference_value > 0
        or np.isfinite(base_value)
        and base_value > 0
    ):
        raise ValueError(
            "Reference price is unavailable. Fetch the CSFloat listing first or provide reference_predicted_price."
        )

    for idx in range(igor_features.MAX_STICKER_SLOTS):
        if idx < len(stickers):
            sticker = stickers[idx]
            raw_price = sticker.get("raw_price_cents", sticker.get("price_cents"))
            sticker["raw_price_cents"] = int(raw_price or 0)
            sticker["price_cents"] = int(raw_price or 0)
            sticker["wear"] = price_model_runtime.normalize_sticker_wear(
                sticker.get("wear")
            )
            sticker["wear_multiplier"] = price_model_runtime.sticker_wear_multiplier(
                sticker.get("wear")
            )
            row[f"sticker_{idx}_id"] = (
                sticker.get("sticker_id")
                if sticker.get("sticker_id") not in (None, "")
                else -1
            )
            row[f"sticker_{idx}_name"] = sticker.get("name") or "none"
            row[f"sticker_{idx}_wear"] = sticker["wear"]
            row[f"sticker_{idx}_price_cents"] = int(raw_price or 0)
            row[f"sticker_{idx}_ref_price_cents"] = int(raw_price or 0)
        else:
            row[f"sticker_{idx}_id"] = -1
            row[f"sticker_{idx}_name"] = "none"
            row[f"sticker_{idx}_wear"] = -1.0
            row[f"sticker_{idx}_price_cents"] = 0.0
            row[f"sticker_{idx}_ref_price_cents"] = 0.0

    sticker_prices = [float(sticker.get("price_cents") or 0) for sticker in stickers]
    sticker_ids = [str(sticker.get("sticker_id") or -1) for sticker in stickers]
    sticker_names = [str(sticker.get("name") or "") for sticker in stickers]
    sticker_wears = [
        price_model_runtime.normalize_sticker_wear(sticker.get("wear"))
        for sticker in stickers
    ]
    known_wears = [wear for wear in sticker_wears if wear >= 0]

    row.update(
        {
            "sticker_count": len(stickers),
            "n_distinct_stickers": len(set(sticker_ids)) if stickers else 0,
            "sum_ref_sticker_price": sum(sticker_prices),
            "max_ref_sticker_price": max(sticker_prices) if sticker_prices else 0.0,
            "mean_ref_sticker_price": float(np.mean(sticker_prices))
            if sticker_prices
            else 0.0,
            "min_ref_sticker_price": min(sticker_prices) if sticker_prices else 0.0,
            "mean_sticker_wear": float(np.mean(known_wears)) if known_wears else -1.0,
            "has_katowice_2014": int(
                any("Katowice 2014" in name for name in sticker_names)
            ),
            "has_premium_finish": int(
                any(
                    any(tag in name for tag in ("Holo", "Foil", "Gold", "Glitter"))
                    for name in sticker_names
                )
            ),
            "has_slot_ge5": int(
                any(
                    price_model_runtime.to_int(sticker.get("slot"), 0) >= 5
                    for sticker in stickers
                )
            ),
            "sticker_reference_safe_count": len(stickers),
            "sticker_reference_future_count": 0,
            "sticker_reference_unknown_count": 0,
        }
    )

    for idx in range(igor_features.MAX_KEYCHAIN_SLOTS):
        if idx < len(keychains):
            keychain = keychains[idx]
            price = max(
                0,
                price_model_runtime.to_int(
                    keychain.get("raw_price_cents", keychain.get("price_cents")), 0
                ),
            )
            row[f"keychain_{idx}_id"] = (
                keychain.get("keychain_id")
                if keychain.get("keychain_id") not in (None, "")
                else -1
            )
            row[f"keychain_{idx}_name"] = keychain.get("name") or "none"
            row[f"keychain_{idx}_pattern"] = (
                keychain.get("pattern")
                if keychain.get("pattern") not in (None, "")
                else -1
            )
            row[f"keychain_{idx}_ref_price_cents"] = price
            row[f"keychain_{idx}_raw_ref_price_cents"] = price
        else:
            row[f"keychain_{idx}_id"] = -1
            row[f"keychain_{idx}_name"] = "none"
            row[f"keychain_{idx}_pattern"] = -1
            row[f"keychain_{idx}_ref_price_cents"] = 0.0
            row[f"keychain_{idx}_raw_ref_price_cents"] = 0.0

    keychain_prices = [
        max(0.0, float(value.get("raw_price_cents", value.get("price_cents")) or 0))
        for value in keychains
    ]
    keychain_ids = [str(value.get("keychain_id") or -1) for value in keychains]
    row.update(
        {
            "keychain_count": len(keychains),
            "n_distinct_keychains": len(set(keychain_ids)) if keychains else 0,
            "sum_ref_keychain_price": sum(keychain_prices),
            "max_ref_keychain_price": max(keychain_prices) if keychain_prices else 0.0,
            "raw_sum_ref_keychain_price": sum(keychain_prices),
            "keychain_reference_safe_count": len(keychains),
            "keychain_reference_future_count": 0,
            "keychain_reference_unknown_count": 0,
        }
    )

    return row, stickers, keychains


def build_raw_row(payload, conn):
    """Backward-compatible two-value contract used by the current Nova model."""

    row, stickers, _keychains = build_raw_row_with_keychains(payload, conn)
    return row, stickers


def build_feature_frame(payload, conn, metadata):
    contract_version = int(metadata.get("feature_contract_version", 1))
    raw_row, stickers, keychains = build_raw_row_with_keychains(
        payload, conn, contract_version=contract_version
    )
    frame = pd.DataFrame([raw_row])
    frame = igor_features.parse_weapon_identity(frame)
    frame = igor_features.build_seed_keys(frame)
    frame = igor_features.build_reference_features(
        frame, contract_version=contract_version
    )
    frame = igor_features.build_staleness_features(frame)
    frame = igor_features.build_sticker_derived_features(
        frame, contract_version=contract_version
    )
    if contract_version >= 2:
        frame = igor_features.build_keychain_derived_features(frame)

    seed_counts = metadata.get("seed_encoder_counts") or {}
    frame["seed_popularity"] = np.log1p(
        frame["skin_seed_key"].map(seed_counts).fillna(0).astype(int)
    )

    weapon_type = str(frame["weapon_type"].iloc[0])
    history_contract = metadata.get("runtime_history") or {}
    if contract_version >= 2 and history_contract:
        premium_by_weapon = history_contract.get("premium_by_weapon") or {}
        count_by_weapon = history_contract.get("count_by_weapon") or {}
        frame["weapon_type_recent_premium"] = float(
            premium_by_weapon.get(
                weapon_type, history_contract.get("global_premium", 0.0)
            )
        )
        frame["skin_liquidity_cumcount"] = float(
            np.log1p(count_by_weapon.get(weapon_type, 0))
        )
    else:
        runtime_stats = get_runtime_stats(conn, weapon_type)
        frame["weapon_type_recent_premium"] = runtime_stats["recent_premium"]
        frame["skin_liquidity_cumcount"] = runtime_stats["liquidity_cumcount"]

    feature_cols = list(metadata["feature_cols"])
    cat_cols = set(metadata.get("cat_features") or [])
    for column in feature_cols:
        if column not in frame.columns:
            frame[column] = "none" if column in cat_cols else 0
        if column in cat_cols:
            frame[column] = frame[column].fillna("none").astype(str)
        else:
            frame[column] = (
                pd.to_numeric(frame[column], errors="coerce")
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
            )

    return frame, feature_cols, stickers, keychains


def predict_deal(payload, conn=None):
    name = str(
        payload.get("name")
        or price_model_runtime.parse_market_hash_name(
            payload.get("url") or payload.get("text") or ""
        )
        or ""
    ).strip()
    actual_kind = classify_market_hash_name(name, conn=conn)
    if actual_kind != ITEM_KIND_WEAPON_SKIN:
        raise ValueError(
            f"Igor weapon-skin model cannot evaluate {actual_kind} items"
        )
    bundle = load_models()
    metadata = bundle["metadata"]
    frame, feature_cols, stickers, keychains = build_feature_frame(
        payload, conn, metadata
    )
    x = frame[feature_cols]

    log_reference = float(frame["log_reference_predicted_price"].iloc[0])
    calibration = metadata.get("prediction_calibration") or {}
    premium_scale = float(calibration.get("premium_scale", 1.0))
    predicted_log_premium = float(
        bundle["point"].predict(x)[0]
    ) * premium_scale + float(calibration.get("center_offset", 0.0))
    low_log_premium = float(bundle["low"].predict(x)[0]) * premium_scale + float(
        calibration.get("low_offset", 0.0)
    )
    high_log_premium = float(bundle["high"].predict(x)[0]) * premium_scale + float(
        calibration.get("high_offset", 0.0)
    )

    predicted_cents = max(
        1, int(round(np.expm1(predicted_log_premium + log_reference)))
    )
    ordered_low, ordered_high = igor_features.contract.order_prediction_interval(
        predicted_log_premium, low_log_premium, high_log_premium
    )
    low_cents = max(1, int(round(np.expm1(float(ordered_low) + log_reference))))
    high_cents = max(
        low_cents,
        int(round(np.expm1(float(ordered_high) + log_reference))),
    )

    buy_price_cents = price_model_runtime.parse_price_cents(
        payload.get("buy_price_usd")
    ) or payload.get("buy_price_cents")
    buy_price_cents = (
        int(buy_price_cents) if buy_price_cents not in (None, "") else None
    )
    fee_percent = min(
        100.0, max(0.0, price_model_runtime.to_float(payload.get("fee_percent"), 4.0))
    )
    net_expected_cents = int(round(predicted_cents * (1 - fee_percent / 100)))
    profit_cents = net_expected_cents - buy_price_cents if buy_price_cents else None
    roi_percent = profit_cents / buy_price_cents * 100 if buy_price_cents else None

    runtime_stats = get_runtime_stats(conn, str(frame["weapon_type"].iloc[0]))
    sales_per_day = runtime_stats["sales_per_day"]
    liquidity_score = runtime_stats["liquidity_score"]

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

    row = frame.iloc[0].to_dict()
    return {
        "model_name": "igor",
        "item_kind": "weapon_skin",
        "active_bundle_version": bundle.get("_runtime_generation_id"),
        "artifact_version": int(metadata.get("artifact_version", 1)),
        "trained_at": metadata.get("trained_at")
        or datetime.fromtimestamp(
            (
                Path(bundle.get("_runtime_source_dir") or ARTIFACT_DIR)
                / POINT_MODEL_PATH.name
            ).stat().st_mtime,
            timezone.utc,
        ).isoformat(),
        "name": payload.get("name") or row.get("name"),
        "base_skin_name": payload.get("base_skin_name")
        or price_model_runtime.extract_base_skin_name(payload.get("name")),
        "wear_name": payload.get("wear_name")
        or price_model_runtime.extract_wear_name(payload.get("name")),
        "predicted_price_cents": predicted_cents,
        "ml_predicted_price_cents": predicted_cents,
        "low_price_cents": low_cents,
        "high_price_cents": high_cents,
        "buy_price_cents": buy_price_cents,
        "fee_percent": fee_percent,
        "net_expected_cents": net_expected_cents,
        "profit_cents": profit_cents,
        "roi_percent": roi_percent,
        "liquidity_score": liquidity_score,
        "sales_per_day": sales_per_day,
        "historical_sales_count": runtime_stats["historical_sales_count"],
        "sticker_value_cents": int(row.get("sum_ref_sticker_price") or 0),
        "sticker_count": int(row.get("sticker_count") or 0),
        "verdict": verdict,
        "pricing_method": "igor_catboost_log_premium",
        "craft_floor_cents": None,
        "craft_applied_rate": None,
        "comparable_sales": {"count": 0, "recent": []},
        "input_features": row,
        "stickers": stickers,
        "keychains": keychains,
        "feature_contract_version": metadata.get("feature_contract_version", 1),
        "feature_contract_sha256": metadata.get("feature_contract_sha256"),
    }


def predict_deal_json(payload, conn=None):
    return json.loads(
        json.dumps(predict_deal(payload, conn), ensure_ascii=False, default=str)
    )
