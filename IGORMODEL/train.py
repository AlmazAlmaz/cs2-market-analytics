"""Leakage-controlled staging trainer for Igor CatBoost residual models."""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor, Pool
from catboost.utils import get_gpu_device_count

try:
    from . import build_cache, feature_contract, features
    from . import evaluate_metrics as em
except ImportError:  # Direct execution: cd IGORMODEL && ../.venv/bin/python train.py
    import build_cache
    import evaluate_metrics as em
    import feature_contract
    import features


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
import app_config
import model_generations
from item_classification import (
    ITEM_KIND_WEAPON_SKIN,
    STRICT_ITEM_KIND_POLICY,
)

PRODUCTION_ARTIFACT_DIR = SCRIPT_DIR / "artifacts"
DEFAULT_STAGING_DIR = SCRIPT_DIR / "staging" / "igor_candidate"

TEST_FRACTION = 0.20
VALIDATION_FRACTION = 0.10
RANDOM_STATE = 42
THREAD_COUNT = 8
ARTIFACT_VERSION = 2
PRIMARY_RUNTIME_MAX_FETCH_LAG_SECONDS = 24 * 60 * 60
MIN_PRIMARY_HOLDOUT_ROWS = 500
MIN_SEGMENT_HOLDOUT_ROWS = 30
# Leave a material validation-side buffer below the promotion limit (1%).
# CatBoost trains point/q15/q85 independently, so an otherwise excellent point
# model can occasionally sit just outside the two raw quantiles.  The deployed
# runtime already envelopes all three predictions; calibrating the raw interval
# here makes the validation and promotion contracts agree without looking at
# the untouched test set.
VALIDATION_POINT_OUTSIDE_TARGET = 0.006


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Train Igor into an isolated staging directory"
    )
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_STAGING_DIR)
    parser.add_argument("--cache-path", type=Path, default=build_cache.CACHE_PATH)
    parser.add_argument(
        "--cache-metadata", type=Path, default=build_cache.CACHE_METADATA_PATH
    )
    parser.add_argument(
        "--holdout-after",
        help="ISO sold_at boundary shared by every peer model; newer rows are untouched test",
    )
    parser.add_argument("--point-iterations", type=int, default=2500)
    parser.add_argument("--quantile-iterations", type=int, default=2000)
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
    parser.add_argument(
        "--verbose-period",
        type=int,
        default=250,
        help="CatBoost progress line interval",
    )
    parser.add_argument("--max-train-rows", type=int)
    parser.add_argument("--thread-count", type=int, default=THREAD_COUNT)
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda"),
        default=app_config.get("CS2_TRAINING_DEVICE", "auto").lower() or "auto",
        help="Training accelerator: auto prefers an NVIDIA GPU when CatBoost can use it",
    )
    parser.add_argument(
        "--gpu-devices",
        default=app_config.get("CS2_CUDA_DEVICES", "0") or "0",
        help="CatBoost CUDA device list, for example 0 or 0:1",
    )
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="Atomically rebuild the default cache from one SQLite snapshot before fit",
    )
    return parser.parse_args(argv)


def choose_training_device(requested: str) -> str:
    """Resolve the CatBoost task type without silently ignoring explicit CUDA."""

    requested = str(requested).strip().lower()
    if requested not in {"auto", "cpu", "cuda"}:
        raise ValueError(f"unsupported training device: {requested!r}")
    if requested == "cpu":
        return "CPU"
    try:
        gpu_count = int(get_gpu_device_count())
    except Exception as exc:
        if requested == "cuda":
            raise RuntimeError("CatBoost could not query CUDA devices") from exc
        gpu_count = 0
    if gpu_count > 0:
        return "GPU"
    if requested == "cuda":
        raise RuntimeError(
            "CUDA was requested for Igor, but CatBoost found no compatible NVIDIA GPU"
        )
    return "CPU"


def load_cache(path: Path) -> pd.DataFrame:
    started = time.time()
    frame = pd.read_pickle(path)
    print(
        f"[cache] {len(frame):,} rows loaded in {time.time() - started:.1f}s",
        flush=True,
    )
    return frame


def prepare_runtime_valid_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    frame = df.copy()
    frame["sold_dt"] = pd.to_datetime(frame["sold_dt"], errors="coerce", utc=True)
    frame["fetched_dt"] = pd.to_datetime(frame["fetched_at"], errors="coerce", utc=True)
    frame["runtime_fetch_lag_seconds"] = (
        frame["fetched_dt"] - frame["sold_dt"]
    ).dt.total_seconds()
    counts = {
        "source_rows": len(frame),
        "invalid_sold_at": int(frame["sold_dt"].isna().sum()),
        "anchor_invalid": int((frame.get("reference_contract_valid", 0) != 1).sum()),
        "reference_future": int(
            frame.get("reference_asof_future", pd.Series(0, index=frame.index)).sum()
        ),
        "reference_unknown": int(
            frame.get("reference_asof_unknown", pd.Series(0, index=frame.index)).sum()
        ),
        "sticker_future_values_masked": int(
            frame.get(
                "sticker_reference_future_count", pd.Series(0, index=frame.index)
            ).sum()
        ),
        "sticker_unknown_values_masked": int(
            frame.get(
                "sticker_reference_unknown_count", pd.Series(0, index=frame.index)
            ).sum()
        ),
        "keychain_future_values_masked": int(
            frame.get(
                "keychain_reference_future_count", pd.Series(0, index=frame.index)
            ).sum()
        ),
        "keychain_unknown_values_masked": int(
            frame.get(
                "keychain_reference_unknown_count", pd.Series(0, index=frame.index)
            ).sum()
        ),
    }
    valid = frame[
        frame["sold_dt"].notna()
        & (frame["reference_contract_valid"] == 1)
        & np.isfinite(frame["log_premium"])
    ].copy()
    valid = valid.sort_values(["sold_dt", "listing_id"]).reset_index(drop=True)

    # Recompute target-derived history only after invalid mixed-target rows are removed.
    valid = features.add_weapon_type_premium_trend(valid, premium_col="log_premium")
    valid = features.add_liquidity_cumcount(valid)
    counts["runtime_anchor_valid_rows"] = len(valid)
    return valid, counts


def chronological_train_validation_test_split(
    df: pd.DataFrame,
    *,
    holdout_after: str | None = None,
    test_fraction: float = TEST_FRACTION,
    test_rows: int | None = None,
    validation_fraction: float = VALIDATION_FRACTION,
    minimum_test_rows: int = MIN_PRIMARY_HOLDOUT_ROWS,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    frame = df.sort_values(["sold_dt", "listing_id"]).reset_index(drop=True)
    if test_rows is not None:
        requested_test_rows = int(test_rows)
        if requested_test_rows < 1 or requested_test_rows >= len(frame):
            raise ValueError("test_rows must leave non-empty development and test sets")
        boundary = frame.iloc[-requested_test_rows]["sold_dt"]
        development = frame[frame["sold_dt"] < boundary].copy()
        test = frame[frame["sold_dt"] >= boundary].copy()
        split_method = "fixed_newest_rows_with_timestamp_ties"
    elif holdout_after:
        boundary = pd.Timestamp(holdout_after)
        if boundary.tzinfo is None:
            boundary = boundary.tz_localize("UTC")
        else:
            boundary = boundary.tz_convert("UTC")
        development = frame[frame["sold_dt"] <= boundary].copy()
        test = frame[frame["sold_dt"] > boundary].copy()
        split_method = "fixed_shared_truly_new_boundary"
    else:
        split_index = int(len(frame) * (1 - test_fraction))
        development = frame.iloc[:split_index].copy()
        test = frame.iloc[split_index:].copy()
        boundary = development["sold_dt"].max()
        split_method = "chronological_fraction"
    if development.empty or test.empty:
        raise ValueError(
            "Chronological split produced an empty development or test frame"
        )
    if len(test) < minimum_test_rows:
        raise ValueError(
            f"Truly-new holdout has only {len(test)} rows; "
            f"at least {minimum_test_rows} are required"
        )
    validation_rows = max(1, int(len(development) * validation_fraction))
    validation_start = development.iloc[-validation_rows]["sold_dt"]
    # Keep identical sold_at timestamps on one side of the boundary.
    train = development[development["sold_dt"] < validation_start].copy()
    validation = development[development["sold_dt"] >= validation_start].copy()
    if train.empty:
        raise ValueError("Chronological split produced an empty train frame")
    manifest = {
        "method": split_method,
        "holdout_after": boundary.isoformat(),
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
    return train, validation, test, manifest


def fit_seed_encoder(train: pd.DataFrame, *frames: pd.DataFrame):
    encoder = features.SeedPopularityEncoder().fit(train)
    train["seed_popularity"] = np.log1p(encoder.transform(train))
    for frame in frames:
        frame["seed_popularity"] = np.log1p(encoder.transform(frame))
    return encoder


def fit_runtime_history(train: pd.DataFrame) -> dict:
    ordered = train.sort_values(["sold_dt", "listing_id"])
    tail = ordered.groupby("weapon_type", group_keys=False).tail(
        features.ROLLING_TREND_WINDOW
    )
    premium_by_weapon = tail.groupby("weapon_type")["log_premium"].median().to_dict()
    count_by_weapon = ordered["weapon_type"].value_counts().astype(int).to_dict()
    return {
        "policy": "frozen_at_fit_train_boundary",
        "window": features.ROLLING_TREND_WINDOW,
        "premium_by_weapon": {str(k): float(v) for k, v in premium_by_weapon.items()},
        "count_by_weapon": {str(k): int(v) for k, v in count_by_weapon.items()},
        "global_premium": float(ordered["log_premium"].median()),
        "max_sold_at": ordered["sold_dt"].max().isoformat(),
    }


def apply_frozen_runtime_history(frame: pd.DataFrame, history: dict) -> None:
    premium = frame["weapon_type"].map(history["premium_by_weapon"])
    frame["weapon_type_recent_premium"] = premium.fillna(history["global_premium"])
    counts = frame["weapon_type"].map(history["count_by_weapon"]).fillna(0)
    frame["skin_liquidity_cumcount"] = np.log1p(counts.astype(float))


def clean_cat_features(df: pd.DataFrame, cat_cols: list[str]) -> pd.DataFrame:
    result = df.copy()
    for column in cat_cols:
        result[column] = result[column].fillna("none").astype(str)
    return result


def make_pools(train, validation, feature_cols, cat_cols):
    train_clean = clean_cat_features(train, cat_cols)
    validation_clean = clean_cat_features(validation, cat_cols)
    return (
        Pool(
            train_clean[feature_cols], train_clean["log_premium"], cat_features=cat_cols
        ),
        Pool(
            validation_clean[feature_cols],
            validation_clean["log_premium"],
            cat_features=cat_cols,
        ),
    )


def model_params(
    iterations: int,
    loss_function: str,
    thread_count: int,
    *,
    task_type: str = "CPU",
    gpu_devices: str = "0",
    verbose_period: int = 250,
) -> dict:
    params = {
        "loss_function": loss_function,
        "iterations": iterations,
        "learning_rate": 0.06 if loss_function == "MAE" else 0.07,
        "depth": 6,
        "l2_leaf_reg": 3.0,
        "random_seed": RANDOM_STATE,
        "early_stopping_rounds": 100,
        "thread_count": thread_count,
        "verbose": max(1, int(verbose_period)),
        "allow_writing_files": False,
    }
    if task_type == "GPU":
        params.update({"task_type": "GPU", "devices": gpu_devices})
    else:
        params["task_type"] = "CPU"
    return params


def train_model(
    train_pool,
    validation_pool,
    *,
    iterations,
    loss,
    thread_count,
    task_type="CPU",
    gpu_devices="0",
    verbose_period=250,
):
    model = CatBoostRegressor(
        **model_params(
            iterations,
            loss,
            thread_count,
            task_type=task_type,
            gpu_devices=gpu_devices,
            verbose_period=verbose_period,
        )
    )
    model.fit(train_pool, eval_set=validation_pool, use_best_model=True)
    return model


def reconstruct_price(prediction, log_reference):
    return np.expm1(np.clip(np.asarray(prediction) + np.asarray(log_reference), 0, 22))


def calibrate_predictions(point, low, high, validation: pd.DataFrame) -> dict:
    target = validation["log_premium"].to_numpy(float)
    log_reference = validation["log_reference_predicted_price"].to_numpy(float)
    true_price = validation["price_cents"].to_numpy(float)
    best = None
    for scale in np.arange(0.70, 1.151, 0.01):
        center_offset = float(np.median(target - point * scale))
        prediction = reconstruct_price(point * scale + center_offset, log_reference)
        metrics = em.compute_metrics(true_price, prediction)
        score = (
            metrics["median_ape_%"]
            + 0.25 * metrics["mean_ape_%"]
            + 0.10 * metrics["p90_ape_%"]
        )
        if best is None or score < best[0]:
            best = (score, float(scale), center_offset, metrics)
    scale = best[1]
    center_offset = best[2]
    low_offset = float(np.quantile(target - low * scale, 0.15))
    high_offset = float(np.quantile(target - high * scale, 0.85))

    calibrated_center = point * scale + center_offset
    calibrated_low = low * scale + low_offset
    calibrated_high = high * scale + high_offset
    ordered_low = np.minimum(calibrated_low, calibrated_high)
    ordered_high = np.maximum(calibrated_low, calibrated_high)
    outside_before = float(
        np.mean(
            (calibrated_center < ordered_low)
            | (calibrated_center > ordered_high)
        )
    )
    # ``method='higher'`` is deliberately conservative: the selected observed
    # gap is never interpolated down.  Calibrate the named q15 and q85 sides
    # separately: if they cross, widening only the current min/max can move the
    # wrong named side and leave the point outside after the swap.
    side_quantile = 1.0 - VALIDATION_POINT_OUTSIDE_TARGET / 2.0
    low_margin = float(
        np.quantile(
            np.maximum(calibrated_low - calibrated_center, 0),
            side_quantile,
            method="higher",
        )
    )
    high_margin = float(
        np.quantile(
            np.maximum(calibrated_center - calibrated_high, 0),
            side_quantile,
            method="higher",
        )
    )
    if low_margin > 0:
        # Make the widened boundary inclusive even after the two independent
        # floating-point additions below.
        low_margin = float(np.nextafter(low_margin, np.inf))
    if high_margin > 0:
        high_margin = float(np.nextafter(high_margin, np.inf))
    low_offset -= low_margin
    high_offset += high_margin
    widened_low = np.minimum(
        low * scale + low_offset,
        high * scale + high_offset,
    )
    widened_high = np.maximum(
        low * scale + low_offset,
        high * scale + high_offset,
    )
    outside_after = float(
        np.mean(
            (calibrated_center < widened_low)
            | (calibrated_center > widened_high)
        )
    )
    return {
        "premium_scale": scale,
        "center_offset": center_offset,
        "low_offset": low_offset,
        "high_offset": high_offset,
        "point_containment_margin": max(low_margin, high_margin),
        "low_point_containment_margin": low_margin,
        "high_point_containment_margin": high_margin,
        "validation_point_outside_raw_rate_before": outside_before,
        "validation_point_outside_raw_rate_after": outside_after,
        "validation_metrics": best[3],
    }


def predict_bundle(
    point_model, low_model, high_model, frame, feature_cols, calibration
):
    x = clean_cat_features(
        frame, [c for c in features.CAT_FEATURES if c in feature_cols]
    )[feature_cols]
    scale = float(calibration["premium_scale"])
    center = point_model.predict(x) * scale + float(calibration["center_offset"])
    low = low_model.predict(x) * scale + float(calibration["low_offset"])
    high = high_model.predict(x) * scale + float(calibration["high_offset"])
    raw_low = np.minimum(low, high)
    raw_high = np.maximum(low, high)
    low_ordered, high_ordered = feature_contract.order_prediction_interval(
        center, low, high
    )
    reference = frame["log_reference_predicted_price"].to_numpy(float)
    return {
        "point": reconstruct_price(center, reference),
        "raw_low": reconstruct_price(raw_low, reference),
        "raw_high": reconstruct_price(raw_high, reference),
        "low": reconstruct_price(low_ordered, reference),
        "high": reconstruct_price(high_ordered, reference),
        "raw_crossing_rate": float(np.mean(low > high)),
        "point_outside_raw_rate": float(
            np.mean((center < raw_low) | (center > raw_high))
        ),
    }


def evaluate_production_bundle(test: pd.DataFrame) -> dict:
    """Replay the installed Igor bundle on the candidate's untouched holdout.

    The comparison deliberately uses the production model's own frozen seed
    counts, history and calibration.  This mirrors live runtime behaviour and
    prevents a candidate from being compared on its more recent metadata.
    """

    production_project = (
        PROJECT_DIR
        if PRODUCTION_ARTIFACT_DIR.resolve() == (SCRIPT_DIR / "artifacts").resolve()
        else PRODUCTION_ARTIFACT_DIR.resolve().parent
    )
    production_dir = model_generations.active_or_legacy_bundle_dir(
        "igor",
        PRODUCTION_ARTIFACT_DIR,
        project_dir=production_project,
    )
    required = {
        "point": production_dir / "price_model_point.cbm",
        "q15": production_dir / "price_model_q15.cbm",
        "q85": production_dir / "price_model_q85.cbm",
        "metadata": production_dir / "feature_metadata.pkl",
        "metrics": production_dir / "igor_metrics.json",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        raise RuntimeError(
            "Cannot quality-gate Igor: production bundle is incomplete ("
            + ", ".join(missing)
            + ")"
        )
    with required["metadata"].open("rb") as handle:
        metadata = pickle.load(handle)
    production_metrics_document = json.loads(
        required["metrics"].read_text(encoding="utf-8")
    )
    if not isinstance(metadata, dict) or not isinstance(
        production_metrics_document, dict
    ):
        raise TypeError("Cannot quality-gate Igor: production metadata is invalid")
    for document in (metadata, production_metrics_document):
        declared_kind = document.get("item_kind")
        if declared_kind is not None and declared_kind != ITEM_KIND_WEAPON_SKIN:
            raise RuntimeError(
                "Cannot quality-gate Igor against a non-weapon production bundle"
            )
    if (
        metadata.get("artifact_version") != ARTIFACT_VERSION
        or metadata.get("feature_contract_sha256")
        != features.FEATURE_CONTRACT_SHA256
    ):
        raise RuntimeError(
            "Cannot quality-gate Igor against an incompatible production contract"
        )

    frame = test.copy()
    seed_counts = metadata.get("seed_encoder_counts") or {}
    frame["seed_popularity"] = np.log1p(
        frame["skin_seed_key"].map(seed_counts).fillna(0).astype(int)
    )
    history = metadata.get("runtime_history")
    if not isinstance(history, dict) or not history:
        raise RuntimeError("Cannot quality-gate Igor without production runtime history")
    apply_frozen_runtime_history(frame, history)

    feature_cols = list(metadata.get("feature_cols") or [])
    cat_cols = list(metadata.get("cat_features") or [])
    if feature_cols != list(features.ALL_FEATURES) or cat_cols != list(
        features.CAT_FEATURES
    ):
        raise RuntimeError("Production Igor feature columns do not match runtime code")
    x = clean_cat_features(frame, cat_cols)[feature_cols]

    raw_predictions = {}
    for role in ("point", "q15", "q85"):
        model = CatBoostRegressor()
        model.load_model(str(required[role]))
        raw_predictions[role] = model.predict(x)
        del model

    calibration = metadata.get("prediction_calibration") or {}
    scale = float(calibration["premium_scale"])
    center = raw_predictions["point"] * scale + float(calibration["center_offset"])
    low = raw_predictions["q15"] * scale + float(calibration["low_offset"])
    high = raw_predictions["q85"] * scale + float(calibration["high_offset"])
    raw_low = np.minimum(low, high)
    raw_high = np.maximum(low, high)
    ordered_low, ordered_high = feature_contract.order_prediction_interval(
        center, low, high
    )
    reference = frame["log_reference_predicted_price"].to_numpy(float)
    return {
        "trained_at": production_metrics_document.get("trained_at"),
        "point": reconstruct_price(center, reference),
        "raw_low": reconstruct_price(raw_low, reference),
        "raw_high": reconstruct_price(raw_high, reference),
        "low": reconstruct_price(ordered_low, reference),
        "high": reconstruct_price(ordered_high, reference),
    }


def frame_bounds(frame: pd.DataFrame) -> dict:
    return {
        "rows": len(frame),
        "min_sold_at": frame["sold_dt"].min().isoformat(),
        "max_sold_at": frame["sold_dt"].max().isoformat(),
        "max_fetched_at": frame["fetched_dt"].max().isoformat(),
    }


def segment_metrics(frame: pd.DataFrame, predicted_price: np.ndarray) -> dict:
    masks = {
        "without_stickers": frame["sticker_count"] == 0,
        "with_stickers": frame["sticker_count"] > 0,
        "with_keychains": frame["keychain_count"] > 0,
        "high_sticker_value": frame["sum_ref_sticker_price"]
        > frame["sum_ref_sticker_price"].quantile(0.95),
    }
    result = {}
    for name, mask in masks.items():
        positions = np.flatnonzero(mask.to_numpy())
        if len(positions) < MIN_SEGMENT_HOLDOUT_ROWS:
            result[name] = {
                "rows": len(positions),
                "gate_eligible": False,
                "minimum_rows": MIN_SEGMENT_HOLDOUT_ROWS,
            }
            continue
        metrics = em.compute_metrics(
            frame.iloc[positions]["price_cents"].to_numpy(),
            np.asarray(predicted_price)[positions],
        )
        result[name] = {"rows": len(positions), "gate_eligible": True, **metrics}
    return result


def atomic_pickle(value, path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(value, path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def atomic_save_model(model, path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    model.save_model(str(temporary), format="cbm")
    os.replace(temporary, path)


def main(argv=None):
    args = parse_args(argv)
    task_type = choose_training_device(args.device)
    device_label = (
        f"cuda (CatBoost GPU devices={args.gpu_devices})"
        if task_type == "GPU"
        else "cpu"
    )
    print(f"device: {device_label}", flush=True)
    if not 0.01 <= float(args.test_fraction) <= 0.25:
        raise ValueError("--test-fraction must be between 0.01 and 0.25")
    if args.test_rows is not None and args.test_rows < MIN_PRIMARY_HOLDOUT_ROWS:
        raise ValueError(
            f"--test-rows must be at least {MIN_PRIMARY_HOLDOUT_ROWS}"
        )
    if args.test_rows is not None and args.holdout_after:
        raise ValueError("--test-rows and --holdout-after are mutually exclusive")
    if args.verbose_period < 1:
        raise ValueError("--verbose-period must be at least 1")
    artifact_dir = args.artifact_dir.resolve()
    if artifact_dir == PRODUCTION_ARTIFACT_DIR.resolve():
        raise ValueError(
            "Training directly into production artifacts is forbidden; use staging"
        )
    artifact_dir.mkdir(parents=True, exist_ok=True)

    uses_default_cache = (
        args.cache_path.resolve() == build_cache.CACHE_PATH.resolve()
        and args.cache_metadata.resolve()
        == build_cache.CACHE_METADATA_PATH.resolve()
    )
    if args.rebuild_cache:
        if not uses_default_cache:
            raise ValueError("--rebuild-cache can only be used with the default cache")
        build_cache.build_and_cache(force=True)
    elif uses_default_cache and not build_cache.is_cache_current():
        raise RuntimeError(
            "Default Igor cache is stale. Re-run with --rebuild-cache, or pass an "
            "explicit immutable --cache-path and --cache-metadata pair."
        )

    cache_metadata = json.loads(args.cache_metadata.read_text(encoding="utf-8"))
    source_database = cache_metadata.get("source_database") or {}
    if cache_metadata.get("cache_schema_version") != build_cache.CACHE_SCHEMA_VERSION:
        raise RuntimeError("Igor requires the current feature-cache schema")
    if (
        cache_metadata.get("item_kind") != ITEM_KIND_WEAPON_SKIN
        or source_database.get("item_kind") != ITEM_KIND_WEAPON_SKIN
        or source_database.get("item_kind_policy") != STRICT_ITEM_KIND_POLICY
    ):
        raise RuntimeError("Igor cache is not an isolated weapon-skin snapshot")
    if (
        cache_metadata.get("feature_contract_sha256")
        != features.FEATURE_CONTRACT_SHA256
    ):
        raise RuntimeError(
            "Cache feature contract does not match the current Igor code"
        )
    actual_cache_sha256 = build_cache.file_sha256(args.cache_path)
    if actual_cache_sha256 != cache_metadata.get("cache_sha256"):
        raise RuntimeError("Cache bytes do not match the recorded immutable snapshot")
    frame, exclusions = prepare_runtime_valid_rows(load_cache(args.cache_path))
    train, validation, test, split_manifest = chronological_train_validation_test_split(
        frame,
        holdout_after=args.holdout_after,
        test_fraction=float(args.test_fraction),
        test_rows=args.test_rows,
    )
    if args.max_train_rows and len(train) > args.max_train_rows:
        train = train.iloc[-args.max_train_rows :].copy()
        split_manifest["train_rows_before_limit"] = split_manifest["train_rows"]
        split_manifest["train_rows"] = len(train)
        split_manifest["max_train_rows"] = int(args.max_train_rows)

    untouched_test = test.copy()
    seed_encoder = fit_seed_encoder(train, validation, test)
    runtime_history = fit_runtime_history(train)
    # Validation and test use exactly the frozen runtime metadata contract;
    # never accumulate their own labels into model features.
    apply_frozen_runtime_history(validation, runtime_history)
    apply_frozen_runtime_history(test, runtime_history)

    feature_cols = list(features.ALL_FEATURES)
    missing = sorted(set(feature_cols) - set(train.columns))
    if missing:
        raise RuntimeError(f"Missing contract features: {missing}")
    cat_cols = list(features.CAT_FEATURES)
    train_pool, validation_pool = make_pools(train, validation, feature_cols, cat_cols)
    print(
        f"[split] train={len(train):,}, validation={len(validation):,}, "
        f"untouched test={len(test):,}; features={len(feature_cols)}",
        flush=True,
    )

    started = time.time()
    point = train_model(
        train_pool,
        validation_pool,
        iterations=args.point_iterations,
        loss="MAE",
        thread_count=args.thread_count,
        task_type=task_type,
        gpu_devices=args.gpu_devices,
        verbose_period=args.verbose_period,
    )
    print(
        f"[point] {time.time() - started:.1f}s best={point.get_best_iteration()}",
        flush=True,
    )
    started = time.time()
    low = train_model(
        train_pool,
        validation_pool,
        iterations=args.quantile_iterations,
        loss="Quantile:alpha=0.15",
        thread_count=args.thread_count,
        task_type=task_type,
        gpu_devices=args.gpu_devices,
        verbose_period=args.verbose_period,
    )
    print(
        f"[q15] {time.time() - started:.1f}s best={low.get_best_iteration()}",
        flush=True,
    )
    started = time.time()
    high = train_model(
        train_pool,
        validation_pool,
        iterations=args.quantile_iterations,
        loss="Quantile:alpha=0.85",
        thread_count=args.thread_count,
        task_type=task_type,
        gpu_devices=args.gpu_devices,
        verbose_period=args.verbose_period,
    )
    print(
        f"[q85] {time.time() - started:.1f}s best={high.get_best_iteration()}",
        flush=True,
    )

    validation_x = clean_cat_features(validation, cat_cols)[feature_cols]
    calibration = calibrate_predictions(
        point.predict(validation_x),
        low.predict(validation_x),
        high.predict(validation_x),
        validation,
    )
    # The test frame is touched only here, after fitting and calibration are final.
    test_predictions = predict_bundle(point, low, high, test, feature_cols, calibration)
    test_metrics = em.compute_metrics(
        test["price_cents"].to_numpy(), test_predictions["point"]
    )
    raw_coverage = float(
        np.mean(
            (test["price_cents"].to_numpy() >= test_predictions["raw_low"])
            & (test["price_cents"].to_numpy() <= test_predictions["raw_high"])
        )
    )
    deployed_coverage = float(
        np.mean(
            (test["price_cents"].to_numpy() >= test_predictions["low"])
            & (test["price_cents"].to_numpy() <= test_predictions["high"])
        )
    )
    runtime_like = test[
        test["runtime_fetch_lag_seconds"].between(
            0, PRIMARY_RUNTIME_MAX_FETCH_LAG_SECONDS, inclusive="both"
        )
    ]
    if len(runtime_like) < MIN_PRIMARY_HOLDOUT_ROWS:
        raise RuntimeError(
            f"Runtime-like holdout has only {len(runtime_like)} rows; "
            f"at least {MIN_PRIMARY_HOLDOUT_ROWS} are required"
        )
    runtime_predictions = predict_bundle(
        point, low, high, runtime_like, feature_cols, calibration
    )
    runtime_metrics = em.compute_metrics(
        runtime_like["price_cents"].to_numpy(), runtime_predictions["point"]
    )
    runtime_raw_coverage = float(
        np.mean(
            (runtime_like["price_cents"].to_numpy() >= runtime_predictions["raw_low"])
            & (
                runtime_like["price_cents"].to_numpy()
                <= runtime_predictions["raw_high"]
            )
        )
    )
    runtime_deployed_coverage = float(
        np.mean(
            (runtime_like["price_cents"].to_numpy() >= runtime_predictions["low"])
            & (runtime_like["price_cents"].to_numpy() <= runtime_predictions["high"])
        )
    )
    print(
        "[quality] comparing production and staging on the same untouched holdout",
        flush=True,
    )
    production_predictions = evaluate_production_bundle(untouched_test)
    production_test_metrics = em.compute_metrics(
        test["price_cents"].to_numpy(), production_predictions["point"]
    )
    comparison_keys = ("median_ape_%", "mean_ape_%", "p90_ape_%", "mae_cents")
    comparison_deltas = {
        key: float(test_metrics[key] - production_test_metrics[key])
        for key in comparison_keys
    }
    production_raw_coverage = float(
        np.mean(
            (test["price_cents"].to_numpy() >= production_predictions["raw_low"])
            & (test["price_cents"].to_numpy() <= production_predictions["raw_high"])
        )
    )
    shared_holdout_comparison = {
        "holdout_rows": len(test),
        "production_trained_at": production_predictions["trained_at"],
        "production": production_test_metrics,
        "staging": test_metrics,
        "staging_minus_production": comparison_deltas,
        "production_interval_coverage_15_85": production_raw_coverage,
        "staging_interval_coverage_15_85": raw_coverage,
        "candidate_row_win_percent": float(
            np.mean(
                np.abs(test_predictions["point"] - test["price_cents"].to_numpy())
                < np.abs(
                    production_predictions["point"]
                    - test["price_cents"].to_numpy()
                )
            )
            * 100
        ),
        "staging_proves_primary_improvement": bool(
            comparison_deltas["median_ape_%"] < 0
            and comparison_deltas["mean_ape_%"] < 0
            and comparison_deltas["p90_ape_%"] < 0
        ),
    }
    trained_at = datetime.now(timezone.utc).isoformat()
    source_rows = int(
        (cache_metadata.get("source_database") or {}).get(
            "valid_price_rows", exclusions["source_rows"]
        )
    )

    metadata = {
        "artifact_version": ARTIFACT_VERSION,
        "model_name": "igor",
        "item_kind": ITEM_KIND_WEAPON_SKIN,
        "trained_at": trained_at,
        "stage": "staging",
        "production_promoted": False,
        "feature_contract_version": feature_contract.FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": features.FEATURE_CONTRACT_SHA256,
        "feature_contract": features.feature_contract_definition(),
        "feature_cols": feature_cols,
        "cat_features": cat_cols,
        "seed_encoder_counts": seed_encoder.counts_,
        "runtime_history": runtime_history,
        "prediction_calibration": calibration,
        "uncertainty_contract": {
            "quantiles": [0.15, 0.85],
            "nominal_raw_coverage": 0.70,
            "calibration_gate_interval": "ordered_raw_q15_q85",
            "deployed_interval": "envelope_of_point_q15_q85",
        },
        "target": "log_premium_over_reference_without_keychain_component",
        "split": split_manifest,
        "source_database": cache_metadata.get("source_database"),
        "cache_sha256": cache_metadata.get("cache_sha256"),
        "cache_integrity_verified": True,
        "cache_schema_version": cache_metadata.get("cache_schema_version"),
        "rows": source_rows,
        "fit_train_rows": len(train),
        "row_exclusions": exclusions,
        "train_bounds": frame_bounds(train),
        "validation_bounds": frame_bounds(validation),
        "test_bounds": frame_bounds(test),
        "model_iterations": {
            "point": int(point.tree_count_),
            "q15": int(low.tree_count_),
            "q85": int(high.tree_count_),
        },
        "training_device": device_label,
    }
    metrics = {
        "artifact_version": ARTIFACT_VERSION,
        "model_name": "igor",
        "item_kind": ITEM_KIND_WEAPON_SKIN,
        "trained_at": trained_at,
        "stage": "staging",
        "feature_contract_sha256": features.FEATURE_CONTRACT_SHA256,
        "source_database": cache_metadata.get("source_database"),
        "cache_sha256": cache_metadata.get("cache_sha256"),
        "cache_integrity_verified": True,
        # ``rows`` is the complete immutable source snapshot used for model
        # freshness.  Keep the actual fit subset explicit instead of making
        # the dashboard infer it from split bookkeeping.
        "rows": source_rows,
        "fit_train_rows": len(train),
        "validation_rows": len(validation),
        "test_rows": len(test),
        "row_exclusions": exclusions,
        "split": split_manifest,
        "validation_calibration": calibration,
        "test": test_metrics,
        "runtime_like_test_rows": len(runtime_like),
        "runtime_like_test": runtime_metrics,
        "shared_holdout_comparison": shared_holdout_comparison,
        "runtime_like_segments": segment_metrics(
            runtime_like, runtime_predictions["point"]
        ),
        "uncertainty_contract": {
            "quantiles": [0.15, 0.85],
            "nominal_raw_coverage": 0.70,
            "calibration_gate_interval": "ordered_raw_q15_q85",
            "deployed_interval": "envelope_of_point_q15_q85",
        },
        "test_interval_coverage_15_85": raw_coverage,
        "test_raw_interval_coverage_15_85": raw_coverage,
        "test_deployed_envelope_coverage": deployed_coverage,
        "test_raw_quantile_crossing_rate": test_predictions["raw_crossing_rate"],
        "test_point_outside_raw_interval_rate": test_predictions[
            "point_outside_raw_rate"
        ],
        "runtime_like_raw_interval_coverage_15_85": runtime_raw_coverage,
        "runtime_like_deployed_envelope_coverage": runtime_deployed_coverage,
        "runtime_like_raw_quantile_crossing_rate": runtime_predictions[
            "raw_crossing_rate"
        ],
        "runtime_like_point_outside_raw_interval_rate": runtime_predictions[
            "point_outside_raw_rate"
        ],
        "production_promoted": False,
        "training_device": device_label,
    }

    atomic_save_model(point, artifact_dir / "price_model_point.cbm")
    atomic_save_model(low, artifact_dir / "price_model_q15.cbm")
    atomic_save_model(high, artifact_dir / "price_model_q85.cbm")
    atomic_pickle(metadata, artifact_dir / "feature_metadata.pkl")
    atomic_json(metrics, artifact_dir / "igor_metrics.json")
    print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)
    return metrics


if __name__ == "__main__":
    main()
