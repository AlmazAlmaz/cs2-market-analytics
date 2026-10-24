"""Train the Nova residual-embedding neural network on chronological sales."""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import pickle
import random
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

import app_config
import model_generations
import portable_os
from IGORMODEL import build_cache
from IGORMODEL import train as igor_train
from item_classification import ITEM_KIND_WEAPON_SKIN, STRICT_ITEM_KIND_POLICY
from NEURALMODEL.features import (
    CAT_FEATURES,
    FEATURE_CONTRACT_SHA256,
    FEATURE_CONTRACT_VERSION,
    NUMERIC_FEATURES,
    embedding_dim,
)
from NEURALMODEL.model import NovaPriceNet

PRODUCTION_ARTIFACT_DIR = app_config.PROJECT_DIR / "NEURALMODEL" / "artifacts"
STAGING_ROOT = app_config.PROJECT_DIR / "NEURALMODEL" / "staging"
DEFAULT_ARTIFACT_DIR = STAGING_ROOT / "nova_candidate"
DEFAULT_CACHE_PATH = build_cache.CACHE_PATH
RANDOM_SEED = 42
NOVA_TRAINING_CONTRACT_VERSION = 2
POST_SPLIT_DERIVED_FEATURES = frozenset(
    {
        "seed_popularity",
        "weapon_type_recent_premium",
        "skin_liquidity_cumcount",
    }
)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE_PATH)
    parser.add_argument(
        "--cache-metadata",
        type=Path,
        default=build_cache.CACHE_METADATA_PATH,
    )
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="Atomically rebuild the current Igor feature cache before training",
    )
    parser.add_argument("--epochs", type=int, default=18)
    parser.add_argument("--batch-size", type=int, default=4096)
    parser.add_argument("--patience", type=int, default=4)
    parser.add_argument("--learning-rate", type=float, default=8e-4)
    parser.add_argument("--max-train-rows", type=int, default=0, help="0 uses the full chronological train set")
    parser.add_argument(
        "--test-rows",
        type=int,
        default=None,
        help="Reserve at least this many newest rows (timestamp ties stay together)",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda", "mps"),
        default=app_config.get("CS2_TRAINING_DEVICE", "auto").lower() or "auto",
        help="Training accelerator: auto prefers CUDA, then Apple MPS, then CPU",
    )
    parser.add_argument(
        "--require-gpu",
        action="store_true",
        default=app_config.get_bool("CS2_TRAINING_REQUIRE_GPU", False),
        help="Fail instead of silently using CPU when no GPU backend is available",
    )
    return parser.parse_args(argv)


def choose_device(requested: str, *, require_gpu: bool = False) -> torch.device:
    requested = str(requested).strip().lower()
    if requested == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA was requested but this PyTorch build cannot use the NVIDIA GPU. "
                "Run setup_windows.ps1 to install the CUDA wheel."
            )
        return torch.device("cuda")
    if requested == "mps":
        mps = getattr(torch.backends, "mps", None)
        if mps is None or not mps.is_available():
            raise RuntimeError("MPS was requested but is not available")
        return torch.device("mps")
    if requested not in {"auto", "cpu"}:
        raise ValueError(f"unsupported training device: {requested!r}")
    if requested == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return torch.device("mps")
        if require_gpu:
            raise RuntimeError(
                "GPU training is required, but neither CUDA nor Apple MPS is available"
            )
    elif require_gpu:
        raise RuntimeError("--require-gpu cannot be combined with --device cpu")
    return torch.device("cpu")


def load_splits(
    cache_path: Path,
    max_train_rows: int,
    *,
    test_rows: int | None = None,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict,
    dict,
    dict,
    dict,
]:
    """Load schema-4 data and build the same time-safe split as Igor v2.

    The returned ``production_test`` keeps strict historical features for a
    read-only v1 production comparison.  Candidate validation/test history is
    frozen at the fit-train boundary, matching live v2 inference.
    """

    cache_path = cache_path.expanduser().resolve()
    if not cache_path.exists():
        raise FileNotFoundError(
            f"Feature cache not found: {cache_path}. Re-run with --rebuild-cache."
        )
    with cache_path.open("rb") as handle:
        cache = pickle.load(handle)

    if not isinstance(cache, pd.DataFrame):
        raise TypeError(
            "Nova v2 requires the current schema-4 feature cache; rebuild it first"
        )
    # These three features depend on the chronological train boundary.  In
    # particular, seed_popularity must be fitted on train only, so a valid raw
    # schema-4 cache intentionally does not contain it.
    required = {
        "sold_dt",
        "listing_id",
        "log_premium",
        "log_reference_predicted_price",
        "price_cents",
        "reference_contract_valid",
        *CAT_FEATURES,
        *(set(NUMERIC_FEATURES) - POST_SPLIT_DERIVED_FEATURES),
    }
    missing = sorted(required - set(cache.columns))
    if missing:
        raise ValueError(f"Feature cache is missing Nova v2 columns: {missing}")

    valid, exclusions = igor_train.prepare_runtime_valid_rows(cache)
    train, validation, test, split_manifest = (
        igor_train.chronological_train_validation_test_split(
            valid,
            test_rows=test_rows,
        )
    )
    production_test = test.copy()
    if max_train_rows > 0 and len(train) > max_train_rows:
        split_manifest["train_rows_before_limit"] = len(train)
        train = train.iloc[-max_train_rows:].copy()
        split_manifest["train_rows"] = len(train)
        split_manifest["max_train_rows"] = int(max_train_rows)

    seed_encoder = igor_train.fit_seed_encoder(train, validation, test)
    runtime_history = igor_train.fit_runtime_history(train)
    igor_train.apply_frozen_runtime_history(validation, runtime_history)
    igor_train.apply_frozen_runtime_history(test, runtime_history)
    complete_features = set(CAT_FEATURES) | set(NUMERIC_FEATURES)
    for split_name, frame in (
        ("train", train),
        ("validation", validation),
        ("test", test),
    ):
        missing = sorted(complete_features - set(frame.columns))
        if missing:
            raise ValueError(
                f"Nova {split_name} split is missing derived features: {missing}"
            )
    return (
        train,
        validation,
        test,
        production_test,
        split_manifest,
        exclusions,
        seed_encoder.counts_,
        runtime_history,
    )


def resolve_staging_dir(path: Path) -> Path:
    """Allow trainer output only below ``NEURALMODEL/staging``."""

    result = path.expanduser().resolve()
    staging_root = STAGING_ROOT.resolve()
    if result == PRODUCTION_ARTIFACT_DIR.resolve():
        raise ValueError("Training directly into Nova production artifacts is forbidden")
    try:
        result.relative_to(staging_root)
    except ValueError as exc:
        raise ValueError(
            f"Nova training output must stay below the staging root: {staging_root}"
        ) from exc
    return result


def _fsync_directory(path: Path) -> None:
    portable_os.fsync_directory(path)


def atomic_json_dump(payload: dict, path: Path, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=None if compact else 2,
                separators=(",", ":") if compact else None,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_torch_save(payload: dict[str, torch.Tensor], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        torch.save(payload, temporary)
        # FlushFileBuffers requires a writable handle on Windows.
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def clean_category(series: pd.Series) -> pd.Series:
    return series.fillna("__missing__").astype(str).replace({"": "__missing__", "nan": "__missing__"})


def build_vocabularies(train: pd.DataFrame) -> dict[str, dict[str, int]]:
    vocabularies: dict[str, dict[str, int]] = {}
    for column in CAT_FEATURES:
        counts = clean_category(train[column]).value_counts()
        max_items = 60_000 if column == "skin_seed_key" else 12_000
        minimum_count = 2 if column == "skin_seed_key" else 1
        values = counts[counts >= minimum_count].head(max_items).index.tolist()
        vocabularies[column] = {value: index + 2 for index, value in enumerate(values)}
        print(f"vocab {column}: {len(values):,}", flush=True)
    return vocabularies


def fit_numeric_scaler(train: pd.DataFrame) -> tuple[dict[str, float], dict[str, float]]:
    medians: dict[str, float] = {}
    scales: dict[str, float] = {}
    for column in NUMERIC_FEATURES:
        values = pd.to_numeric(train[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
        median = float(values.median()) if values.notna().any() else 0.0
        lower = float(values.quantile(0.25)) if values.notna().any() else median
        upper = float(values.quantile(0.75)) if values.notna().any() else median
        scale = upper - lower
        if not math.isfinite(scale) or scale < 1e-5:
            scale = float(values.std()) if values.notna().any() else 1.0
        if not math.isfinite(scale) or scale < 1e-5:
            scale = 1.0
        medians[column] = median
        scales[column] = scale
    return medians, scales


def transform_frame(
    frame: pd.DataFrame,
    vocabularies: dict[str, dict[str, int]],
    medians: dict[str, float],
    scales: dict[str, float],
    *,
    cat_features: list[str] | tuple[str, ...] = CAT_FEATURES,
    numeric_features: list[str] | tuple[str, ...] = NUMERIC_FEATURES,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    categories = np.empty((len(frame), len(cat_features)), dtype=np.int64)
    for index, column in enumerate(cat_features):
        mapped = clean_category(frame[column]).map(vocabularies[column]).fillna(1)
        categories[:, index] = mapped.to_numpy(dtype=np.int64, copy=False)

    numeric = np.empty((len(frame), len(numeric_features)), dtype=np.float32)
    for index, column in enumerate(numeric_features):
        values = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
        values = values.fillna(medians[column])
        numeric[:, index] = np.clip(
            (values.to_numpy(dtype=np.float32, copy=False) - medians[column]) / scales[column],
            -12.0,
            12.0,
        )

    target = np.clip(frame["log_premium"].to_numpy(dtype=np.float32, copy=False), -6.0, 9.0)
    log_reference = frame["log_reference_predicted_price"].to_numpy(dtype=np.float32, copy=False)
    price = frame["price_cents"].to_numpy(dtype=np.float32, copy=False)
    # Percentage/log metrics treat every sale equally. Keeping the main loss
    # unweighted prevents a few very expensive crafts from pulling the median
    # estimate away from the majority of live listings. Rare segments are
    # reported separately and guarded by the conservative lower quantile.
    weight = np.ones(len(frame), dtype=np.float32)
    return categories, numeric, target, weight, np.stack((log_reference, price), axis=1)


def make_loader(
    arrays: tuple[np.ndarray, ...],
    batch_size: int,
    *,
    shuffle: bool,
    pin_memory: bool = False,
) -> DataLoader:
    tensors = tuple(torch.from_numpy(array) for array in arrays)
    return DataLoader(
        TensorDataset(*tensors),
        batch_size=batch_size,
        shuffle=shuffle,
        num_workers=0,
        pin_memory=pin_memory,
        drop_last=shuffle,
    )


def pinball(prediction: torch.Tensor, target: torch.Tensor, quantile: float) -> torch.Tensor:
    error = target - prediction
    return torch.maximum(quantile * error, (quantile - 1.0) * error)


def training_loss(outputs: torch.Tensor, target: torch.Tensor, weight: torch.Tensor) -> torch.Tensor:
    loss = (
        0.45 * pinball(outputs[:, 0], target, 0.15)
        + 2.5 * pinball(outputs[:, 1], target, 0.50)
        + 0.45 * pinball(outputs[:, 2], target, 0.85)
        + nn.functional.smooth_l1_loss(outputs[:, 1], target, reduction="none", beta=0.15)
    )
    return (loss * weight).sum() / weight.sum().clamp_min(1.0)


@torch.inference_mode()
def collect_predictions(model: NovaPriceNet, loader: DataLoader, device: torch.device) -> tuple[np.ndarray, ...]:
    model.eval()
    predictions: list[np.ndarray] = []
    targets: list[np.ndarray] = []
    references: list[np.ndarray] = []
    prices: list[np.ndarray] = []
    for categories, numeric, target, _weight, auxiliary in loader:
        non_blocking = device.type == "cuda"
        output = model(
            categories.to(device, non_blocking=non_blocking),
            numeric.to(device, non_blocking=non_blocking),
        ).cpu().numpy()
        predictions.append(output)
        targets.append(target.numpy())
        references.append(auxiliary[:, 0].numpy())
        prices.append(auxiliary[:, 1].numpy())
    return (
        np.concatenate(predictions),
        np.concatenate(targets),
        np.concatenate(references),
        np.concatenate(prices),
    )


def price_metrics(true_price: np.ndarray, predicted_price: np.ndarray) -> dict[str, float]:
    true_price = np.asarray(true_price, dtype=float)
    predicted_price = np.maximum(predicted_price, 1.0)
    absolute_error = np.abs(predicted_price - true_price)
    ape = absolute_error / np.maximum(true_price, 1.0)
    true_log = np.log1p(true_price)
    predicted_log = np.log1p(predicted_price)
    residual_sum_squares = float(np.square(true_log - predicted_log).sum())
    total_sum_squares = float(np.square(true_log - true_log.mean()).sum())
    return {
        "mae_cents": float(np.mean(absolute_error)),
        "mae_usd": float(np.mean(absolute_error) / 100),
        "median_ape_percent": float(np.median(ape) * 100),
        "mean_ape_percent": float(np.mean(ape) * 100),
        "p75_ape_percent": float(np.quantile(ape, 0.75) * 100),
        "p90_ape_percent": float(np.quantile(ape, 0.90) * 100),
        "rmse_log_price": float(np.sqrt(np.mean(np.square(true_log - predicted_log)))),
        "r2_log_price": (
            float(1.0 - residual_sum_squares / total_sum_squares)
            if total_sum_squares > 0
            else 0.0
        ),
        "within_10_percent": float(np.mean(ape <= 0.10)),
        "within_20_percent": float(np.mean(ape <= 0.20)),
    }


def calibrated_prices(
    outputs: np.ndarray,
    log_reference: np.ndarray,
    *,
    premium_scale: float,
    calibration: dict[str, float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    calibrated = np.asarray(outputs, dtype=float).copy() * float(premium_scale)
    calibrated[:, 0] += float(calibration.get("low", 0))
    calibrated[:, 1] += float(calibration.get("center", 0))
    calibrated[:, 2] += float(calibration.get("high", 0))
    calibrated[:, 0] = np.minimum(calibrated[:, 0], calibrated[:, 1])
    calibrated[:, 2] = np.maximum(calibrated[:, 2], calibrated[:, 1])
    reference = np.asarray(log_reference, dtype=float)
    point = np.expm1(np.clip(calibrated[:, 1] + reference, 0, 22))
    low = np.expm1(np.clip(calibrated[:, 0] + reference, 0, 22))
    high = np.expm1(np.clip(calibrated[:, 2] + reference, 0, 22))
    return point, low, high


def apply_production_v2_frozen_features(
    frame: pd.DataFrame, metadata: dict
) -> pd.DataFrame:
    """Rebuild learned features with the installed model's own metadata."""

    result = frame.copy()
    seed_counts = metadata.get("seed_encoder_counts") or {}
    if not isinstance(seed_counts, dict):
        raise TypeError("Production Nova seed encoder metadata is invalid")
    result["seed_popularity"] = np.log1p(
        result["skin_seed_key"].map(seed_counts).fillna(0).astype(int)
    )
    runtime_history = metadata.get("runtime_history")
    if not isinstance(runtime_history, dict) or not runtime_history:
        raise ValueError("Production Nova runtime history metadata is missing")
    igor_train.apply_frozen_runtime_history(result, runtime_history)
    return result


def evaluate_production_model(
    frame: pd.DataFrame,
    *,
    batch_size: int,
    device: torch.device,
) -> tuple[dict[str, float], float, np.ndarray, str | None] | None:
    """Evaluate deployed Nova on the candidate's untouched chronological test."""

    production_dir = model_generations.active_or_legacy_bundle_dir(
        "nova",
        PRODUCTION_ARTIFACT_DIR,
        project_dir=PRODUCTION_ARTIFACT_DIR.parent.parent,
    )
    model_path = production_dir / "nova_price_model.pt"
    metadata_path = production_dir / "nova_metadata.json"
    if not model_path.is_file() or not metadata_path.is_file():
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    declared_kind = metadata.get("item_kind")
    if declared_kind is not None and declared_kind != ITEM_KIND_WEAPON_SKIN:
        raise RuntimeError(
            "Cannot quality-gate Nova against a non-weapon production bundle"
        )
    production_frame = frame.copy()
    production_version = int(metadata.get("feature_contract_version", 1))
    if production_version >= 2:
        production_frame = apply_production_v2_frozen_features(
            production_frame, metadata
        )
    else:
        from IGORMODEL import features as igor_features

        production_frame = igor_features.build_reference_features(
            production_frame, contract_version=1
        )
        production_frame = igor_features.build_sticker_derived_features(
            production_frame, contract_version=1
        )
        for unsafe in ("watchers", "is_watchlisted"):
            if unsafe in production_frame:
                production_frame[unsafe] = 0

    cat_features = list(metadata["cat_features"])
    numeric_features = list(metadata["numeric_features"])
    missing = sorted(
        (set(cat_features) | set(numeric_features)) - set(production_frame.columns)
    )
    if missing:
        raise ValueError(f"Production Nova comparison is missing features: {missing}")
    arrays = transform_frame(
        production_frame,
        metadata["vocabularies"],
        metadata["numeric_medians"],
        metadata["numeric_scales"],
        cat_features=cat_features,
        numeric_features=numeric_features,
    )
    loader = make_loader(
        arrays,
        batch_size * 2,
        shuffle=False,
        pin_memory=device.type == "cuda",
    )
    old_model = NovaPriceNet(**metadata["model_config"]).to(device)
    old_state = torch.load(model_path, map_location="cpu", weights_only=True)
    old_model.load_state_dict(old_state)
    outputs, _target, reference, price = collect_predictions(
        old_model, loader, device
    )
    point, low, high = calibrated_prices(
        outputs,
        reference,
        premium_scale=float(metadata.get("premium_scale", 1.0)),
        calibration=metadata.get("calibration") or {},
    )
    coverage = float(np.mean((price >= low) & (price <= high)))
    del old_model, old_state, arrays, loader
    gc.collect()
    return price_metrics(price, point), coverage, point, metadata.get("trained_at")


def calibrate_quantiles(
    predictions: np.ndarray,
    target: np.ndarray,
    log_reference: np.ndarray,
    true_price: np.ndarray,
) -> tuple[float, dict[str, float]]:
    """Choose residual shrinkage on validation only, then conformal offsets."""

    best = None
    for scale in np.arange(0.65, 1.151, 0.01):
        center_offset = float(np.median(target - predictions[:, 1] * scale))
        predicted_price = np.expm1(
            np.clip(log_reference + predictions[:, 1] * scale + center_offset, 0, 22)
        )
        current = price_metrics(true_price, predicted_price)
        score = (
            current["median_ape_percent"]
            + 0.25 * current["mean_ape_percent"]
            + 0.10 * current["p90_ape_percent"]
        )
        if best is None or score < best[0]:
            best = (score, float(scale), center_offset)

    scale = best[1]
    calibration = {
        "low": float(np.quantile(target - predictions[:, 0] * scale, 0.15)),
        "center": best[2],
        "high": float(np.quantile(target - predictions[:, 2] * scale, 0.85)),
    }
    return scale, calibration


def main(argv=None) -> None:
    args = parse_args(argv)
    if args.test_rows is not None and args.test_rows < 1_000:
        raise ValueError("--test-rows must be at least 1000")
    artifact_dir = resolve_staging_dir(args.artifact_dir)
    cache_path = args.cache_path.expanduser().resolve()
    cache_metadata_path = args.cache_metadata.expanduser().resolve()
    if args.rebuild_cache:
        if (
            cache_path != DEFAULT_CACHE_PATH.resolve()
            or cache_metadata_path != build_cache.CACHE_METADATA_PATH.resolve()
        ):
            raise ValueError(
                "--rebuild-cache requires the default feature-cache paths"
            )
        build_cache.build_and_cache(force=True)

    try:
        cache_metadata = json.loads(cache_metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Nova cache metadata is missing or invalid") from exc
    if cache_metadata.get("cache_schema_version") != build_cache.CACHE_SCHEMA_VERSION:
        raise ValueError("Nova requires the current feature-cache schema")
    source_database = cache_metadata.get("source_database") or {}
    if (
        cache_metadata.get("item_kind") != ITEM_KIND_WEAPON_SKIN
        or source_database.get("item_kind") != ITEM_KIND_WEAPON_SKIN
        or source_database.get("item_kind_policy") != STRICT_ITEM_KIND_POLICY
    ):
        raise ValueError("Nova cache is not an isolated weapon-skin snapshot")
    if cache_metadata.get("feature_contract_sha256") != FEATURE_CONTRACT_SHA256:
        raise ValueError("Nova cache feature contract does not match runtime code")
    actual_cache_sha256 = build_cache.file_sha256(cache_path)
    if actual_cache_sha256 != cache_metadata.get("cache_sha256"):
        raise ValueError("Nova cache bytes do not match cache metadata")

    model_path = artifact_dir / "nova_price_model.pt"
    metadata_path = artifact_dir / "nova_metadata.json"
    metrics_path = artifact_dir / "nova_metrics.json"
    random.seed(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)
    torch.manual_seed(RANDOM_SEED)
    device = choose_device(args.device, require_gpu=args.require_gpu)
    device_name = (
        torch.cuda.get_device_name(device) if device.type == "cuda" else str(device)
    )
    if device.type == "cuda":
        torch.cuda.manual_seed_all(RANDOM_SEED)
        torch.set_float32_matmul_precision("high")
    print(
        f"device: {device} ({device_name})"
        + (f", torch CUDA {torch.version.cuda}" if device.type == "cuda" else ""),
        flush=True,
    )
    print(f"loading chronological cache: {cache_path}", flush=True)
    (
        train,
        validation,
        test,
        production_test,
        split_manifest,
        exclusions,
        seed_encoder_counts,
        runtime_history,
    ) = load_splits(
        cache_path,
        args.max_train_rows,
        test_rows=args.test_rows,
    )
    source_rows = int(
        (cache_metadata.get("source_database") or {}).get(
            "valid_price_rows", exclusions.get("source_rows", 0)
        )
    )
    train_bounds = igor_train.frame_bounds(train)
    validation_bounds = igor_train.frame_bounds(validation)
    test_bounds = igor_train.frame_bounds(test)
    print(f"rows train={len(train):,} validation={len(validation):,} test={len(test):,}", flush=True)
    print(f"train dates {train['sold_dt'].min()} -> {train['sold_dt'].max()}", flush=True)
    print(f"test dates  {test['sold_dt'].min()} -> {test['sold_dt'].max()}", flush=True)

    vocabularies = build_vocabularies(train)
    medians, scales = fit_numeric_scaler(train)
    print("encoding arrays...", flush=True)
    train_arrays = transform_frame(train, vocabularies, medians, scales)
    validation_arrays = transform_frame(validation, vocabularies, medians, scales)
    test_arrays = transform_frame(test, vocabularies, medians, scales)
    del train, validation, test
    gc.collect()

    pin_memory = device.type == "cuda"
    train_loader = make_loader(
        train_arrays, args.batch_size, shuffle=True, pin_memory=pin_memory
    )
    validation_loader = make_loader(
        validation_arrays,
        args.batch_size * 2,
        shuffle=False,
        pin_memory=pin_memory,
    )
    test_loader = make_loader(
        test_arrays,
        args.batch_size * 2,
        shuffle=False,
        pin_memory=pin_memory,
    )
    cardinalities = [len(vocabularies[column]) + 2 for column in CAT_FEATURES]
    embedding_dims = [embedding_dim(value) for value in cardinalities]
    model_config = {
        "category_cardinalities": cardinalities,
        "embedding_dims": embedding_dims,
        "numeric_count": len(NUMERIC_FEATURES),
        "width": 512,
        "block_count": 5,
        "dropout": 0.08,
    }
    model = NovaPriceNet(**model_config).to(device)
    parameter_count = sum(parameter.numel() for parameter in model.parameters())
    print(f"parameters: {parameter_count:,}", flush=True)

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.55, patience=1)
    best_state: dict[str, torch.Tensor] | None = None
    best_validation = math.inf
    stale_epochs = 0
    history = []

    for epoch in range(1, args.epochs + 1):
        started = time.time()
        model.train()
        total_loss = 0.0
        total_rows = 0
        for categories, numeric, target, weight, _auxiliary in train_loader:
            non_blocking = device.type == "cuda"
            categories = categories.to(device, non_blocking=non_blocking)
            numeric = numeric.to(device, non_blocking=non_blocking)
            target = target.to(device, non_blocking=non_blocking)
            weight = weight.to(device, non_blocking=non_blocking)
            optimizer.zero_grad(set_to_none=True)
            outputs = model(categories, numeric)
            loss = training_loss(outputs, target, weight)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=2.0)
            optimizer.step()
            rows = len(target)
            total_loss += float(loss.detach().cpu()) * rows
            total_rows += rows

        validation_predictions, validation_target, _reference, _price = collect_predictions(
            model, validation_loader, device
        )
        validation_loss = float(np.mean(np.abs(validation_predictions[:, 1] - validation_target)))
        scheduler.step(validation_loss)
        epoch_result = {
            "epoch": epoch,
            "train_loss": total_loss / max(total_rows, 1),
            "validation_mae_log_premium": validation_loss,
            "seconds": time.time() - started,
            "learning_rate": optimizer.param_groups[0]["lr"],
        }
        history.append(epoch_result)
        print(
            f"epoch {epoch:02d} train={epoch_result['train_loss']:.5f} "
            f"val={validation_loss:.5f} lr={epoch_result['learning_rate']:.2e} "
            f"time={epoch_result['seconds']:.1f}s",
            flush=True,
        )
        if validation_loss < best_validation - 1e-4:
            best_validation = validation_loss
            best_state = {name: value.detach().cpu().clone() for name, value in model.state_dict().items()}
            stale_epochs = 0
        else:
            stale_epochs += 1
            if stale_epochs >= args.patience:
                print("early stopping", flush=True)
                break

    if best_state is None:
        raise RuntimeError("Training did not produce a model")
    model.load_state_dict(best_state)
    model.to(device)

    validation_predictions, validation_target, validation_reference, validation_price = collect_predictions(
        model, validation_loader, device
    )
    premium_scale, calibration = calibrate_quantiles(
        validation_predictions, validation_target, validation_reference, validation_price
    )
    test_predictions, _test_target, test_reference, test_price = collect_predictions(
        model, test_loader, device
    )
    raw_calibrated = test_predictions.copy() * premium_scale
    raw_calibrated[:, 0] += calibration["low"]
    raw_calibrated[:, 1] += calibration["center"]
    raw_calibrated[:, 2] += calibration["high"]
    raw_low_premium = np.minimum(raw_calibrated[:, 0], raw_calibrated[:, 2])
    raw_high_premium = np.maximum(raw_calibrated[:, 0], raw_calibrated[:, 2])
    raw_low_price = np.expm1(np.clip(raw_low_premium + test_reference, 0, 22))
    raw_high_price = np.expm1(np.clip(raw_high_premium + test_reference, 0, 22))
    raw_interval_coverage = float(
        np.mean((test_price >= raw_low_price) & (test_price <= raw_high_price))
    )
    raw_crossing_rate = float(
        np.mean(raw_calibrated[:, 0] > raw_calibrated[:, 2])
    )
    point_outside_raw_rate = float(
        np.mean(
            (raw_calibrated[:, 1] < raw_low_premium)
            | (raw_calibrated[:, 1] > raw_high_premium)
        )
    )
    predicted_price, low_price, high_price = calibrated_prices(
        test_predictions,
        test_reference,
        premium_scale=premium_scale,
        calibration=calibration,
    )
    interval_coverage = float(np.mean((test_price >= low_price) & (test_price <= high_price)))

    candidate_test_metrics = price_metrics(test_price, predicted_price)
    production_evaluation = evaluate_production_model(
        production_test,
        batch_size=args.batch_size,
        device=device,
    )
    comparison = None
    if production_evaluation is not None:
        (
            production_metrics,
            production_coverage,
            production_prediction,
            production_trained_at,
        ) = (
            production_evaluation
        )
        metric_keys = (
            "median_ape_percent",
            "mean_ape_percent",
            "p90_ape_percent",
            "mae_cents",
        )
        deltas = {
            key: candidate_test_metrics[key] - production_metrics[key]
            for key in metric_keys
        }
        comparison = {
            "holdout_rows": len(test_price),
            "production_trained_at": production_trained_at,
            "production": production_metrics,
            "staging": candidate_test_metrics,
            "staging_minus_production": deltas,
            "production_interval_coverage_15_85": production_coverage,
            "staging_interval_coverage_15_85": raw_interval_coverage,
            "candidate_row_win_percent": float(
                np.mean(
                    np.abs(predicted_price - test_price)
                    < np.abs(production_prediction - test_price)
                )
                * 100
            ),
            "staging_proves_primary_improvement": bool(
                deltas["median_ape_percent"] < 0
                and deltas["mean_ape_percent"] < 0
                and deltas["p90_ape_percent"] < 0
            ),
        }

    trained_at = datetime.now(timezone.utc).isoformat()
    metrics = {
        "artifact_version": 2,
        "trained_at": trained_at,
        "model_name": "nova",
        "item_kind": ITEM_KIND_WEAPON_SKIN,
        "stage": "staging",
        "production_promoted": False,
        "rows": source_rows,
        "fit_train_rows": len(train_arrays[2]),
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": FEATURE_CONTRACT_SHA256,
        "source_database": cache_metadata.get("source_database"),
        "cache_sha256": actual_cache_sha256,
        "cache_schema_version": cache_metadata.get("cache_schema_version"),
        "split": split_manifest,
        "row_exclusions": exclusions,
        "architecture": "categorical_embeddings_residual_mlp_ordered_quantiles",
        "parameters": parameter_count,
        "device": str(device),
        "device_name": device_name,
        "torch_cuda_version": torch.version.cuda,
        "train_rows": len(train_arrays[2]),
        "validation_rows": len(validation_arrays[2]),
        "test_rows": len(test_arrays[2]),
        "best_validation_mae_log_premium": best_validation,
        "test": candidate_test_metrics,
        "reference_baseline_test": price_metrics(test_price, np.expm1(test_reference)),
        "test_interval_coverage_15_85": interval_coverage,
        "test_raw_interval_coverage_15_85": raw_interval_coverage,
        "test_raw_quantile_crossing_rate": raw_crossing_rate,
        "test_point_outside_raw_interval_rate": point_outside_raw_rate,
        "shared_holdout_comparison": comparison,
        "calibration": calibration,
        "premium_scale": premium_scale,
        "history": history,
    }
    metadata = {
        "artifact_version": 2,
        "trained_at": trained_at,
        "model_name": "nova",
        "item_kind": ITEM_KIND_WEAPON_SKIN,
        "stage": "staging",
        "production_promoted": False,
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": FEATURE_CONTRACT_SHA256,
        "feature_cols": list(CAT_FEATURES) + list(NUMERIC_FEATURES),
        "model_config": model_config,
        "cat_features": CAT_FEATURES,
        "numeric_features": NUMERIC_FEATURES,
        "vocabularies": vocabularies,
        "numeric_medians": medians,
        "numeric_scales": scales,
        "seed_encoder_counts": seed_encoder_counts,
        "runtime_history": runtime_history,
        "source_database": cache_metadata.get("source_database"),
        "cache_sha256": actual_cache_sha256,
        "cache_schema_version": cache_metadata.get("cache_schema_version"),
        "split": split_manifest,
        "row_exclusions": exclusions,
        "rows": source_rows,
        "fit_train_rows": len(train_arrays[2]),
        "train_bounds": train_bounds,
        "validation_bounds": validation_bounds,
        "test_bounds": test_bounds,
        "target": "log_premium_over_reference_without_keychain_component",
        "calibration": calibration,
        "premium_scale": premium_scale,
        "test_metrics": metrics["test"],
        "test_interval_coverage_15_85": interval_coverage,
    }

    artifact_dir.mkdir(parents=True, exist_ok=True)
    atomic_torch_save(best_state, model_path)
    model_sha256 = build_cache.file_sha256(model_path)
    metadata["model_sha256"] = model_sha256
    metrics["model_sha256"] = model_sha256
    atomic_json_dump(metadata, metadata_path, compact=True)
    atomic_json_dump(metrics, metrics_path)
    print(json.dumps(metrics["test"], indent=2), flush=True)
    print(f"interval coverage: {interval_coverage:.3f}", flush=True)
    print(f"saved staging candidate: {model_path}", flush=True)


if __name__ == "__main__":
    main()
