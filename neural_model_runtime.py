"""Live inference adapter for the Nova neural price model."""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import torch

import app_config
import igor_model_runtime
import model_generations
import price_model_runtime
from item_classification import (
    ITEM_KIND_WEAPON_SKIN,
    classify_market_hash_name,
)
from NEURALMODEL.features import (
    CAT_FEATURES,
    FEATURE_CONTRACT_SHA256,
    NUMERIC_FEATURES,
)
from NEURALMODEL.model import NovaPriceNet

ARTIFACT_DIR = app_config.PROJECT_DIR / "NEURALMODEL" / "artifacts"
MODEL_PATH = ARTIFACT_DIR / "nova_price_model.pt"
METADATA_PATH = ARTIFACT_DIR / "nova_metadata.json"
MODEL_CACHE = None
MODEL_MTIMES = None
MODEL_LOCK = threading.Lock()
_RUNTIME_SLOT = None


def validate_metadata_contract(metadata):
    declared_kind = metadata.get("item_kind")
    if declared_kind is not None and declared_kind != ITEM_KIND_WEAPON_SKIN:
        raise RuntimeError("Nova artifact is not a weapon-skin model")
    artifact_version = int(metadata.get("artifact_version", 1))
    contract_version = int(metadata.get("feature_contract_version", 1))
    if artifact_version < 2 and contract_version < 2:
        return
    if metadata.get("feature_contract_sha256") != FEATURE_CONTRACT_SHA256:
        raise RuntimeError("Nova v2 artifact/code feature contract mismatch")
    if list(metadata.get("cat_features") or []) != list(CAT_FEATURES):
        raise RuntimeError("Nova v2 categorical features do not match runtime")
    if list(metadata.get("numeric_features") or []) != list(NUMERIC_FEATURES):
        raise RuntimeError("Nova v2 numeric features do not match runtime")
    expected = list(CAT_FEATURES) + list(NUMERIC_FEATURES)
    if list(metadata.get("feature_cols") or []) != expected:
        raise RuntimeError("Nova v2 ordered feature list does not match runtime")


def load_bundle_from_directory(directory):
    """Load one Nova bundle without exposing it to prediction requests."""

    paths = (directory / MODEL_PATH.name, directory / METADATA_PATH.name)
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Nova model artifacts are missing. Run: python train_neural_model.py. "
            f"Missing: {missing}"
        )
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise RuntimeError("Nova bundle contains an unsafe artifact")
    metadata = json.loads(paths[1].read_text(encoding="utf-8"))
    validate_metadata_contract(metadata)
    model = NovaPriceNet(**metadata["model_config"])
    state = torch.load(paths[0], map_location="cpu", weights_only=True)
    model.load_state_dict(state)
    model.eval()
    return {
        "model": model,
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
    """Exercise Nova's tensor contract before an atomic live swap."""

    metadata = bundle.get("metadata") or {}
    category_count = len(metadata.get("cat_features") or [])
    numeric_count = len(metadata.get("numeric_features") or [])
    if category_count < 1 or numeric_count < 1:
        raise RuntimeError("Nova bundle has no usable tensor contract")
    categories = torch.zeros((1, category_count), dtype=torch.int64)
    numeric = torch.zeros((1, numeric_count), dtype=torch.float32)
    with torch.inference_mode():
        output = bundle["model"](categories, numeric)
    values = output.detach().cpu().numpy()
    if values.shape != (1, 3) or not np.isfinite(values).all():
        raise RuntimeError("Nova smoke prediction has an invalid shape or value")


def _runtime_slot():
    global _RUNTIME_SLOT
    if _RUNTIME_SLOT is None:
        with MODEL_LOCK:
            if _RUNTIME_SLOT is None:
                _RUNTIME_SLOT = model_generations.RuntimeBundleSlot(
                    "nova",
                    ARTIFACT_DIR,
                    load_bundle=load_bundle_from_directory,
                    smoke_bundle=smoke_bundle,
                    project_dir=app_config.PROJECT_DIR,
                )
    return _RUNTIME_SLOT


def load_model():
    global MODEL_CACHE, MODEL_MTIMES

    MODEL_CACHE = _runtime_slot().get()
    source = Path(MODEL_CACHE.get("_runtime_source_dir") or ARTIFACT_DIR)
    MODEL_MTIMES = tuple(
        (source / path.name).stat().st_mtime_ns for path in (MODEL_PATH, METADATA_PATH)
    )
    return MODEL_CACHE


def build_feature_frame(payload, conn, metadata):
    contract_version = int(metadata.get("feature_contract_version", 1))
    if contract_version >= 2:
        frame, _feature_cols, stickers, _keychains = (
            igor_model_runtime.build_feature_frame(payload, conn, metadata)
        )
        weapon_type = str(frame["weapon_type"].iloc[0])
        runtime_stats = igor_model_runtime.get_runtime_stats(conn, weapon_type)
    else:
        raw_row, stickers = igor_model_runtime.build_raw_row(payload, conn)
        frame = pd.DataFrame([raw_row])
        features = igor_model_runtime.igor_features
        frame = features.parse_weapon_identity(frame)
        frame = features.build_seed_keys(frame)
        frame = features.build_reference_features(frame)
        frame = features.build_staleness_features(frame)
        frame = features.build_sticker_derived_features(frame)

        weapon_type = str(frame["weapon_type"].iloc[0])
        runtime_stats = igor_model_runtime.get_runtime_stats(conn, weapon_type)
        frame["weapon_type_recent_premium"] = runtime_stats["recent_premium"]
        frame["skin_liquidity_cumcount"] = runtime_stats["liquidity_cumcount"]

    category_values = np.empty((1, len(metadata["cat_features"])), dtype=np.int64)
    for index, column in enumerate(metadata["cat_features"]):
        value = frame[column].iloc[0] if column in frame else "__missing__"
        if value is None or (isinstance(value, float) and math.isnan(value)) or str(value) in {"", "nan"}:
            value = "__missing__"
        category_values[0, index] = metadata["vocabularies"][column].get(str(value), 1)

    numeric_values = np.empty((1, len(metadata["numeric_features"])), dtype=np.float32)
    input_features = {}
    for index, column in enumerate(metadata["numeric_features"]):
        value = pd.to_numeric(frame[column], errors="coerce").iloc[0] if column in frame else np.nan
        if not np.isfinite(value):
            value = float(metadata["numeric_medians"][column])
        input_features[column] = float(value)
        numeric_values[0, index] = np.clip(
            (float(value) - float(metadata["numeric_medians"][column]))
            / float(metadata["numeric_scales"][column]),
            -12.0,
            12.0,
        )

    input_features.update({
        "weapon_type": weapon_type,
        "wear_name": str(frame["wear_name"].iloc[0]),
        "skin_seed_key": str(frame["skin_seed_key"].iloc[0]),
    })
    return frame, category_values, numeric_values, stickers, runtime_stats, input_features


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
            f"Nova weapon-skin model cannot evaluate {actual_kind} items"
        )
    bundle = load_model()
    metadata = bundle["metadata"]
    frame, categories, numeric, stickers, runtime_stats, input_features = build_feature_frame(
        payload, conn, metadata
    )
    with torch.inference_mode():
        output = bundle["model"](torch.from_numpy(categories), torch.from_numpy(numeric))[0].numpy()

    calibration = metadata.get("calibration") or {}
    premium_scale = float(metadata.get("premium_scale", 1.0))
    low_premium = float(output[0]) * premium_scale + float(calibration.get("low", 0))
    center_premium = float(output[1]) * premium_scale + float(calibration.get("center", 0))
    high_premium = float(output[2]) * premium_scale + float(calibration.get("high", 0))
    low_premium = min(low_premium, center_premium)
    high_premium = max(high_premium, center_premium)
    log_reference = float(frame["log_reference_predicted_price"].iloc[0])

    def restore_price(log_premium):
        return max(1, round(np.expm1(np.clip(log_premium + log_reference, 0, 22))))

    predicted_cents = restore_price(center_premium)
    low_cents = restore_price(low_premium)
    high_cents = max(low_cents, restore_price(high_premium))
    buy_price_cents = price_model_runtime.parse_price_cents(payload.get("buy_price_usd"))
    if buy_price_cents is None and payload.get("buy_price_cents") not in (None, ""):
        buy_price_cents = max(1, price_model_runtime.to_int(payload.get("buy_price_cents"), 1))
    fee_percent = min(100.0, max(0.0, price_model_runtime.to_float(payload.get("fee_percent"), 4.0)))
    fee_multiplier = 1 - fee_percent / 100
    net_expected_cents = round(predicted_cents * fee_multiplier)
    net_low_cents = round(low_cents * fee_multiplier)
    profit_cents = net_expected_cents - buy_price_cents if buy_price_cents else None
    low_profit_cents = net_low_cents - buy_price_cents if buy_price_cents else None
    roi_percent = profit_cents / buy_price_cents * 100 if buy_price_cents else None
    downside_roi_percent = low_profit_cents / buy_price_cents * 100 if buy_price_cents else None
    liquidity_score = float(runtime_stats["liquidity_score"])

    if buy_price_cents is None:
        verdict = "enter_buy_price"
    elif roi_percent >= 8 and downside_roi_percent >= 0 and liquidity_score >= 40:
        verdict = "good"
    elif roi_percent >= 3 and downside_roi_percent >= -5 and liquidity_score >= 20:
        verdict = "watch"
    elif roi_percent >= 0:
        verdict = "thin"
    else:
        verdict = "bad"

    sticker_value = round(float(np.expm1(frame["log_sum_ref_sticker_price"].iloc[0])))
    return {
        "model_name": "nova",
        "item_kind": "weapon_skin",
        "active_bundle_version": bundle.get("_runtime_generation_id"),
        "artifact_version": metadata.get("artifact_version", 1),
        "feature_contract_version": metadata.get("feature_contract_version", 1),
        "feature_contract_sha256": metadata.get("feature_contract_sha256"),
        "trained_at": metadata.get("trained_at"),
        "name": raw_name(payload, frame),
        "base_skin_name": str(frame["weapon_type"].iloc[0]),
        "wear_name": str(frame["wear_name"].iloc[0]),
        "predicted_price_cents": predicted_cents,
        "ml_predicted_price_cents": predicted_cents,
        "low_price_cents": low_cents,
        "high_price_cents": high_cents,
        "buy_price_cents": buy_price_cents,
        "fee_percent": fee_percent,
        "net_expected_cents": net_expected_cents,
        "net_low_cents": net_low_cents,
        "profit_cents": profit_cents,
        "low_profit_cents": low_profit_cents,
        "roi_percent": roi_percent,
        "downside_roi_percent": downside_roi_percent,
        "liquidity_score": liquidity_score,
        "sales_per_day": float(runtime_stats["sales_per_day"]),
        "historical_sales_count": int(runtime_stats["historical_sales_count"]),
        "sticker_value_cents": sticker_value,
        "sticker_count": int(frame["sticker_count"].iloc[0]),
        "verdict": verdict,
        "pricing_method": "nova_neural_residual_q15_q50_q85",
        "test_metrics": metadata.get("test_metrics") or {},
        "test_interval_coverage_15_85": metadata.get("test_interval_coverage_15_85"),
        "comparable_sales": {"count": 0, "recent": []},
        "input_features": input_features,
        "stickers": stickers,
    }


def raw_name(payload, frame):
    value = payload.get("name") or price_model_runtime.parse_market_hash_name(payload.get("url_text") or "")
    if value:
        return str(value)
    weapon = str(frame["weapon_type"].iloc[0])
    wear = str(frame["wear_name"].iloc[0])
    return f"{weapon} ({wear})"


def model_status():
    try:
        bundle = load_model()
        metadata = bundle["metadata"]
        source = Path(bundle.get("_runtime_source_dir") or ARTIFACT_DIR)
        return {
            "available": True,
            "model_name": "nova",
            "trained_at": metadata.get("trained_at"),
            "test_metrics": metadata.get("test_metrics") or {},
            "test_interval_coverage_15_85": metadata.get("test_interval_coverage_15_85"),
            "artifact_size_mb": round(
                (source / MODEL_PATH.name).stat().st_size / 1024 / 1024, 2
            ),
            **_runtime_slot().status(),
        }
    except Exception as exc:  # noqa: BLE001 - status endpoint must fail closed
        return {"available": False, "model_name": "nova", "error": str(exc)}


def predict_deal_json(payload, conn=None):
    return json.loads(json.dumps(predict_deal(payload, conn), ensure_ascii=False, default=str))
