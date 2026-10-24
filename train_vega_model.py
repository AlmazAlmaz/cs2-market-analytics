"""Train the CUDA-first Vega feature-token Transformer on chronological sales."""

from __future__ import annotations

import argparse
import gc
import json
import math
import random
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import DataLoader

import app_config
import model_generations
from IGORMODEL import build_cache
from IGORMODEL import train as igor_train
from item_classification import ITEM_KIND_WEAPON_SKIN, STRICT_ITEM_KIND_POLICY
from train_neural_model import (
    apply_production_v2_frozen_features,
    atomic_json_dump,
    atomic_torch_save,
    build_vocabularies,
    calibrate_quantiles,
    calibrated_prices,
    fit_numeric_scaler,
    load_splits,
    make_loader,
    price_metrics,
    training_loss,
    transform_frame,
)
from VEGAMODEL.features import (
    CAT_FEATURES,
    FEATURE_CONTRACT_SHA256,
    FEATURE_CONTRACT_VERSION,
    NUMERIC_FEATURES,
)
from VEGAMODEL.model import ARCHITECTURE_ID, VegaPriceNet

PRODUCTION_ARTIFACT_DIR = app_config.PROJECT_DIR / "VEGAMODEL" / "artifacts"
STAGING_ROOT = app_config.PROJECT_DIR / "VEGAMODEL" / "staging"
DEFAULT_ARTIFACT_DIR = STAGING_ROOT / "vega_candidate"
DEFAULT_CACHE_PATH = build_cache.CACHE_PATH
RANDOM_SEED = 42
VEGA_TRAINING_CONTRACT_VERSION = 1
DEFAULT_TEST_ROWS = 25_000
VALIDATION_POINT_OUTSIDE_TARGET = 0.006
MAX_EVALUATION_BATCH_SIZE = 2_048

MODEL_FILENAME = "vega_price_model.pt"
METADATA_FILENAME = "vega_metadata.json"
METRICS_FILENAME = "vega_metrics.json"

MODEL_CONFIG = {
    "category_cardinalities": None,
    "numeric_count": len(NUMERIC_FEATURES),
    "d_token": 192,
    "block_count": 4,
    "head_count": 8,
    "ffn_hidden": 768,
    "attention_dropout": 0.05,
    "residual_dropout": 0.08,
    "head_dropout": 0.05,
}


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
        help="Atomically rebuild the audited Igor/Nova feature cache first",
    )
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=2048)
    parser.add_argument("--patience", type=int, default=5)
    parser.add_argument("--warmup-epochs", type=float, default=2.0)
    parser.add_argument("--learning-rate", type=float, default=6e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument(
        "--max-train-rows",
        type=int,
        default=0,
        help="0 uses the full chronological train set",
    )
    parser.add_argument(
        "--test-rows",
        type=int,
        default=DEFAULT_TEST_ROWS,
        help="Reserve at least this many newest rows (timestamp ties stay together)",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda", "mps"),
        default=app_config.get("CS2_TRAINING_DEVICE", "cuda").lower() or "cuda",
        help="Training accelerator; Vega defaults to CUDA and never silently falls back",
    )
    parser.add_argument(
        "--require-gpu",
        dest="require_gpu",
        action="store_true",
        help="Fail instead of training on CPU",
    )
    parser.add_argument(
        "--allow-cpu",
        dest="require_gpu",
        action="store_false",
        help="Explicitly permit a CPU run (intended only for development)",
    )
    parser.set_defaults(
        require_gpu=app_config.get_bool("CS2_TRAINING_REQUIRE_GPU", True)
    )
    return parser.parse_args(argv)


def choose_device(requested: str, *, require_gpu: bool = True) -> torch.device:
    """Resolve the accelerator without a silent CPU fallback."""

    requested = str(requested).strip().lower()
    if requested == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError(
                "Vega requires CUDA, but this PyTorch build cannot use the NVIDIA "
                "GPU. Run setup_windows.ps1 to install the CUDA wheel."
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
            raise RuntimeError("Vega GPU training is required, but no GPU is available")
    elif require_gpu:
        raise RuntimeError("--require-gpu cannot be combined with --device cpu")
    return torch.device("cpu")


def resolve_staging_dir(path: Path) -> Path:
    """Allow trainer output only below ``VEGAMODEL/staging``."""

    result = path.expanduser().resolve()
    staging_root = STAGING_ROOT.resolve()
    if result == PRODUCTION_ARTIFACT_DIR.resolve():
        raise ValueError("Training directly into Vega production is forbidden")
    try:
        result.relative_to(staging_root)
    except ValueError as exc:
        raise ValueError(
            f"Vega training output must stay below the staging root: {staging_root}"
        ) from exc
    return result


def configure_cuda(device: torch.device) -> dict[str, bool]:
    """Enable RTX-friendly training kernels; evaluation disables TF32 separately."""

    enabled = device.type == "cuda"
    if enabled:
        torch.cuda.manual_seed_all(RANDOM_SEED)
        torch.set_float32_matmul_precision("high")
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
    return {
        "tf32_matmul": enabled,
        "tf32_cudnn": enabled,
        "fp16_autocast": enabled,
    }


@contextmanager
def fp32_quality_mode(device: torch.device):
    """Run validation and quality gates in true FP32, including CUDA matmuls."""

    if device.type != "cuda":
        yield
        return
    old_matmul = bool(torch.backends.cuda.matmul.allow_tf32)
    old_cudnn = bool(torch.backends.cudnn.allow_tf32)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    try:
        with torch.autocast(device_type="cuda", enabled=False):
            yield
    finally:
        torch.backends.cuda.matmul.allow_tf32 = old_matmul
        torch.backends.cudnn.allow_tf32 = old_cudnn


def _training_autocast(device: torch.device):
    if device.type == "cuda":
        return torch.autocast(device_type="cuda", dtype=torch.float16)
    return nullcontext()


def create_grad_scaler(device: torch.device):
    enabled = device.type == "cuda"
    try:
        return torch.amp.GradScaler("cuda", enabled=enabled)
    except (AttributeError, TypeError):  # pragma: no cover - old PyTorch fallback
        return torch.cuda.amp.GradScaler(enabled=enabled)


def create_optimizer(
    model: nn.Module,
    *,
    learning_rate: float,
    weight_decay: float,
    device: torch.device,
) -> tuple[torch.optim.Optimizer, bool]:
    """Prefer fused AdamW on CUDA and fall back without weakening training."""

    options = {
        "lr": float(learning_rate),
        "weight_decay": float(weight_decay),
        "betas": (0.9, 0.98),
    }
    if device.type == "cuda":
        try:
            return torch.optim.AdamW(model.parameters(), fused=True, **options), True
        except (RuntimeError, TypeError):
            pass
    return torch.optim.AdamW(model.parameters(), **options), False


def evaluation_batch_size(training_batch_size: int) -> int:
    """Bound FP32 attention batches below the RTX 4070 SUPER WDDM limit."""

    value = int(training_batch_size)
    if value < 1:
        raise ValueError("training batch size must be positive")
    return min(value, MAX_EVALUATION_BATCH_SIZE)


def release_evaluation_cuda_cache(device: torch.device) -> None:
    """Return temporary FP32 attention buffers after each full validation."""

    if device.type == "cuda":
        torch.cuda.empty_cache()


def warmup_cosine_multiplier(
    step: int,
    *,
    warmup_steps: int,
    total_steps: int,
    minimum_ratio: float = 0.08,
) -> float:
    """Linear warmup followed by one smooth cosine decay."""

    total_steps = max(1, int(total_steps))
    warmup_steps = min(max(0, int(warmup_steps)), total_steps)
    current = min(max(0, int(step)), total_steps - 1)
    if warmup_steps and current < warmup_steps:
        return max(1.0 / warmup_steps, (current + 1) / warmup_steps)
    decay_steps = max(1, total_steps - warmup_steps - 1)
    progress = min(1.0, (current - warmup_steps) / decay_steps)
    cosine = 0.5 * (1.0 + math.cos(math.pi * progress))
    return float(minimum_ratio + (1.0 - minimum_ratio) * cosine)


def enforce_ordered_calibration(
    predictions: np.ndarray,
    *,
    premium_scale: float,
    calibration: dict[str, float],
) -> tuple[dict[str, float], dict[str, float]]:
    """Keep calibrated q15/point/q85 ordered without looking at test rows.

    The network output is ordered by construction.  Independent conformal
    offsets can nevertheless move q15 above the point estimate or q85 below
    it.  Ordering the three *global validation offsets* preserves containment
    for every future network output, rather than tuning a margin on the test
    set.
    """

    values = np.asarray(predictions, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 3 or not np.isfinite(values).all():
        raise ValueError("Vega calibration predictions must be a finite Nx3 array")
    scale = float(premium_scale)
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Vega premium calibration scale must be positive")
    if np.any(values[:, 0] > values[:, 1]) or np.any(
        values[:, 1] > values[:, 2]
    ):
        raise ValueError("Vega validation predictions violate ordered quantiles")
    try:
        original = {
            key: float(calibration[key]) for key in ("low", "center", "high")
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Vega calibration offsets are incomplete") from exc
    if not all(math.isfinite(value) for value in original.values()):
        raise ValueError("Vega calibration offsets must be finite")

    def outside_rate(offsets: dict[str, float]) -> float:
        calibrated = values * scale
        calibrated[:, 0] += offsets["low"]
        calibrated[:, 1] += offsets["center"]
        calibrated[:, 2] += offsets["high"]
        return float(
            np.mean(
                (calibrated[:, 1] < calibrated[:, 0])
                | (calibrated[:, 1] > calibrated[:, 2])
            )
        )

    corrected = {
        "low": min(original["low"], original["center"]),
        "center": original["center"],
        "high": max(original["high"], original["center"]),
    }
    diagnostics = {
        "target_max_outside_rate": VALIDATION_POINT_OUTSIDE_TARGET,
        "validation_point_outside_raw_rate_before": outside_rate(original),
        "validation_point_outside_raw_rate_after": outside_rate(corrected),
        "low_point_containment_margin": original["low"] - corrected["low"],
        "high_point_containment_margin": corrected["high"] - original["high"],
    }
    if diagnostics["validation_point_outside_raw_rate_after"] > 0:
        raise RuntimeError("Vega validation calibration failed point containment")
    return corrected, diagnostics


@torch.inference_mode()
def collect_predictions(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
) -> tuple[np.ndarray, ...]:
    """Collect quality predictions in FP32, never in training autocast."""

    model.eval()
    predictions: list[np.ndarray] = []
    targets: list[np.ndarray] = []
    references: list[np.ndarray] = []
    prices: list[np.ndarray] = []
    with fp32_quality_mode(device):
        for categories, numeric, target, _weight, auxiliary in loader:
            non_blocking = device.type == "cuda"
            output = model(
                categories.to(device, non_blocking=non_blocking),
                numeric.to(device, dtype=torch.float32, non_blocking=non_blocking),
            )
            predictions.append(output.float().cpu().numpy())
            targets.append(target.float().numpy())
            references.append(auxiliary[:, 0].float().numpy())
            prices.append(auxiliary[:, 1].float().numpy())
    return (
        np.concatenate(predictions),
        np.concatenate(targets),
        np.concatenate(references),
        np.concatenate(prices),
    )


def _legacy_artifact_dir(model_name: str) -> Path:
    if model_name == "vega":
        return PRODUCTION_ARTIFACT_DIR
    if model_name == "nova":
        return app_config.PROJECT_DIR / "NEURALMODEL" / "artifacts"
    raise ValueError(f"unsupported Vega benchmark model: {model_name}")


def _benchmark_filenames(model_name: str) -> tuple[str, str]:
    if model_name == "vega":
        return MODEL_FILENAME, METADATA_FILENAME
    if model_name == "nova":
        return "nova_price_model.pt", "nova_metadata.json"
    raise ValueError(f"unsupported Vega benchmark model: {model_name}")


def select_benchmark_model() -> str:
    """Use active Vega after bootstrap; otherwise benchmark active Nova."""

    pointer = model_generations.read_active_pointer(
        "vega", project_dir=app_config.PROJECT_DIR
    )
    if pointer is not None:
        return "vega"
    return "nova"


def _load_benchmark_identity(
    model_name: str,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    legacy = _legacy_artifact_dir(model_name)
    pointer = model_generations.read_active_pointer(
        model_name, project_dir=app_config.PROJECT_DIR
    )
    if pointer is None:
        raise RuntimeError(
            f"Vega requires an exact active {model_name} generation benchmark"
        )
    directory = model_generations.active_or_legacy_bundle_dir(
        model_name,
        legacy,
        project_dir=app_config.PROJECT_DIR,
    )
    if directory.name != pointer["generation_id"]:
        raise RuntimeError(
            f"Active {model_name} benchmark changed while it was being resolved"
        )
    current_pointer = model_generations.read_active_pointer(
        model_name, project_dir=app_config.PROJECT_DIR
    )
    if (
        current_pointer is None
        or current_pointer.get("generation_id") != pointer["generation_id"]
    ):
        raise RuntimeError(
            f"Active {model_name} benchmark changed while it was being resolved"
        )
    model_filename, metadata_filename = _benchmark_filenames(model_name)
    model_path = directory / model_filename
    metadata_path = directory / metadata_filename
    if not model_path.is_file() or not metadata_path.is_file():
        raise FileNotFoundError(
            f"Active {model_name} benchmark artifacts are missing: {directory}"
        )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("model_name") != model_name:
        raise RuntimeError("Benchmark metadata belongs to another model")
    if metadata.get("item_kind") not in {None, ITEM_KIND_WEAPON_SKIN}:
        raise RuntimeError("Benchmark artifact is not a weapon-skin model")
    trained_at = metadata.get("trained_at")
    if not isinstance(trained_at, str) or not trained_at:
        raise RuntimeError("Benchmark artifact has no exact training identity")
    actual_sha = build_cache.file_sha256(model_path)
    if metadata.get("model_sha256") != actual_sha:
        raise RuntimeError("Benchmark model bytes do not match its metadata SHA-256")
    identity = {
        "model_name": model_name,
        "active_generation_id": str(pointer["generation_id"]),
        "model_sha256": actual_sha,
        "trained_at": trained_at,
    }
    return directory, model_path, metadata, identity


def evaluate_benchmark_model(
    frame: pd.DataFrame,
    *,
    batch_size: int,
    device: torch.device,
) -> tuple[dict[str, float], float, np.ndarray, dict[str, Any]]:
    """Evaluate the exact active Nova/Vega bytes on Vega's shared holdout."""

    model_name = select_benchmark_model()
    _directory, model_path, metadata, identity = _load_benchmark_identity(model_name)
    benchmark_frame = frame.copy()
    if int(metadata.get("feature_contract_version", 1)) >= 2:
        benchmark_frame = apply_production_v2_frozen_features(
            benchmark_frame, metadata
        )
    else:
        from IGORMODEL import features as igor_features

        benchmark_frame = igor_features.build_reference_features(
            benchmark_frame, contract_version=1
        )
        benchmark_frame = igor_features.build_sticker_derived_features(
            benchmark_frame, contract_version=1
        )
        for unsafe in ("watchers", "is_watchlisted"):
            if unsafe in benchmark_frame:
                benchmark_frame[unsafe] = 0

    cat_features = list(metadata["cat_features"])
    numeric_features = list(metadata["numeric_features"])
    missing = sorted(
        (set(cat_features) | set(numeric_features)) - set(benchmark_frame.columns)
    )
    if missing:
        raise ValueError(f"Benchmark comparison is missing features: {missing}")
    arrays = transform_frame(
        benchmark_frame,
        metadata["vocabularies"],
        metadata["numeric_medians"],
        metadata["numeric_scales"],
        cat_features=cat_features,
        numeric_features=numeric_features,
    )
    loader = make_loader(
        arrays,
        evaluation_batch_size(batch_size),
        shuffle=False,
        pin_memory=device.type == "cuda",
    )
    if model_name == "vega":
        benchmark_model = VegaPriceNet(**metadata["model_config"])
    else:
        from NEURALMODEL.model import NovaPriceNet

        benchmark_model = NovaPriceNet(**metadata["model_config"])
    state = torch.load(model_path, map_location="cpu", weights_only=True)
    benchmark_model.load_state_dict(state)
    benchmark_model = benchmark_model.float().to(device)
    outputs, _target, reference, price = collect_predictions(
        benchmark_model, loader, device
    )
    point, low, high = calibrated_prices(
        outputs,
        reference,
        premium_scale=float(metadata.get("premium_scale", 1.0)),
        calibration=metadata.get("calibration") or {},
    )
    coverage = float(np.mean((price >= low) & (price <= high)))
    del benchmark_model, state, arrays, loader
    gc.collect()
    if device.type == "cuda":
        torch.cuda.empty_cache()
    return price_metrics(price, point), coverage, point, identity


def _cache_metadata(path: Path, cache_path: Path) -> tuple[dict[str, Any], str]:
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Vega cache metadata is missing or invalid") from exc
    if metadata.get("cache_schema_version") != build_cache.CACHE_SCHEMA_VERSION:
        raise ValueError("Vega requires the current feature-cache schema")
    source_database = metadata.get("source_database") or {}
    if (
        metadata.get("item_kind") != ITEM_KIND_WEAPON_SKIN
        or source_database.get("item_kind") != ITEM_KIND_WEAPON_SKIN
        or source_database.get("item_kind_policy") != STRICT_ITEM_KIND_POLICY
    ):
        raise ValueError("Vega cache is not an isolated weapon-skin snapshot")
    if metadata.get("feature_contract_sha256") != FEATURE_CONTRACT_SHA256:
        raise ValueError("Vega cache feature contract does not match runtime code")
    actual_sha = build_cache.file_sha256(cache_path)
    if actual_sha != metadata.get("cache_sha256"):
        raise ValueError("Vega cache bytes do not match cache metadata")
    return metadata, actual_sha


def _validate_arguments(args: argparse.Namespace) -> None:
    if args.test_rows is None or args.test_rows < 1_000:
        raise ValueError("--test-rows must be at least 1000")
    if args.epochs < 1:
        raise ValueError("--epochs must be positive")
    if args.batch_size < 1:
        raise ValueError("--batch-size must be positive")
    if args.max_train_rows < 0:
        raise ValueError("--max-train-rows cannot be negative")
    if args.patience < 1:
        raise ValueError("--patience must be positive")
    if not 0 <= args.warmup_epochs < args.epochs:
        raise ValueError("--warmup-epochs must be in [0, epochs)")
    if args.learning_rate <= 0 or args.weight_decay < 0:
        raise ValueError("optimizer settings are invalid")


def main(argv=None) -> None:
    args = parse_args(argv)
    _validate_arguments(args)
    artifact_dir = resolve_staging_dir(args.artifact_dir)
    cache_path = args.cache_path.expanduser().resolve()
    cache_metadata_path = args.cache_metadata.expanduser().resolve()
    if args.rebuild_cache:
        if (
            cache_path != DEFAULT_CACHE_PATH.resolve()
            or cache_metadata_path != build_cache.CACHE_METADATA_PATH.resolve()
        ):
            raise ValueError("--rebuild-cache requires the default cache paths")
        build_cache.build_and_cache(force=True)
    cache_metadata, actual_cache_sha256 = _cache_metadata(
        cache_metadata_path, cache_path
    )

    model_path = artifact_dir / MODEL_FILENAME
    metadata_path = artifact_dir / METADATA_FILENAME
    metrics_path = artifact_dir / METRICS_FILENAME
    random.seed(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)
    torch.manual_seed(RANDOM_SEED)
    device = choose_device(args.device, require_gpu=args.require_gpu)
    acceleration = configure_cuda(device)
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)
    device_name = (
        torch.cuda.get_device_name(device) if device.type == "cuda" else str(device)
    )
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
    print(
        f"rows train={len(train):,} validation={len(validation):,} "
        f"test={len(test):,}",
        flush=True,
    )
    print(
        f"train dates {train['sold_dt'].min()} -> {train['sold_dt'].max()}",
        flush=True,
    )
    print(
        f"test dates  {test['sold_dt'].min()} -> {test['sold_dt'].max()}",
        flush=True,
    )

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
        train_arrays,
        args.batch_size,
        shuffle=True,
        pin_memory=pin_memory,
    )
    validation_loader = make_loader(
        validation_arrays,
        evaluation_batch_size(args.batch_size),
        shuffle=False,
        pin_memory=pin_memory,
    )
    test_loader = make_loader(
        test_arrays,
        evaluation_batch_size(args.batch_size),
        shuffle=False,
        pin_memory=pin_memory,
    )
    cardinalities = [len(vocabularies[column]) + 2 for column in CAT_FEATURES]
    model_config = {**MODEL_CONFIG, "category_cardinalities": cardinalities}
    model = VegaPriceNet(**model_config).float().to(device)
    parameter_count = sum(parameter.numel() for parameter in model.parameters())
    print(f"parameters: {parameter_count:,}", flush=True)

    optimizer, fused_optimizer = create_optimizer(
        model,
        learning_rate=args.learning_rate,
        weight_decay=args.weight_decay,
        device=device,
    )
    scaler = create_grad_scaler(device)
    updates_per_epoch = max(1, len(train_loader))
    total_updates = max(1, args.epochs * updates_per_epoch)
    warmup_updates = min(
        total_updates,
        max(0, round(args.warmup_epochs * updates_per_epoch)),
    )
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lr_lambda=lambda step: warmup_cosine_multiplier(
            step,
            warmup_steps=warmup_updates,
            total_steps=total_updates,
        ),
    )
    best_state: dict[str, torch.Tensor] | None = None
    best_validation = math.inf
    best_epoch = 0
    stale_epochs = 0
    history: list[dict[str, float | int]] = []

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
            with _training_autocast(device):
                outputs = model(categories, numeric)
                loss = training_loss(outputs, target, weight)
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.5)
            scaler.step(optimizer)
            scaler.update()
            scheduler.step()
            rows = len(target)
            total_loss += float(loss.detach().float().cpu()) * rows
            total_rows += rows

        validation_predictions, validation_target, _reference, _price = (
            collect_predictions(model, validation_loader, device)
        )
        release_evaluation_cuda_cache(device)
        validation_loss = float(
            np.mean(np.abs(validation_predictions[:, 1] - validation_target))
        )
        epoch_result = {
            "epoch": epoch,
            "train_loss": total_loss / max(total_rows, 1),
            "validation_mae_log_premium": validation_loss,
            "seconds": time.time() - started,
            "learning_rate": float(optimizer.param_groups[0]["lr"]),
        }
        history.append(epoch_result)
        print(
            f"epoch {epoch:02d} train={epoch_result['train_loss']:.5f} "
            f"val={validation_loss:.5f} "
            f"lr={epoch_result['learning_rate']:.2e} "
            f"time={epoch_result['seconds']:.1f}s",
            flush=True,
        )
        if validation_loss < best_validation - 1e-4:
            best_validation = validation_loss
            best_epoch = epoch
            best_state = {
                name: value.detach().float().cpu().clone()
                for name, value in model.state_dict().items()
            }
            stale_epochs = 0
        else:
            stale_epochs += 1
            if stale_epochs >= args.patience:
                print("early stopping", flush=True)
                break

    if best_state is None:
        raise RuntimeError("Training did not produce a Vega model")
    model.load_state_dict(best_state)
    model.float().to(device)
    validation_predictions, validation_target, validation_reference, validation_price = (
        collect_predictions(model, validation_loader, device)
    )
    release_evaluation_cuda_cache(device)
    premium_scale, calibration = calibrate_quantiles(
        validation_predictions,
        validation_target,
        validation_reference,
        validation_price,
    )
    calibration, calibration_containment = enforce_ordered_calibration(
        validation_predictions,
        premium_scale=premium_scale,
        calibration=calibration,
    )
    test_predictions, _test_target, test_reference, test_price = collect_predictions(
        model, test_loader, device
    )
    raw_calibrated = test_predictions.astype(np.float64) * premium_scale
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
    interval_coverage = float(
        np.mean((test_price >= low_price) & (test_price <= high_price))
    )
    candidate_test_metrics = price_metrics(test_price, predicted_price)

    if device.type == "cuda":
        torch.cuda.synchronize(device)
        peak_allocated_bytes = int(torch.cuda.max_memory_allocated(device))
        peak_reserved_bytes = int(torch.cuda.max_memory_reserved(device))
    else:
        peak_allocated_bytes = 0
        peak_reserved_bytes = 0

    model.to("cpu")
    if device.type == "cuda":
        torch.cuda.empty_cache()
    (
        production_metrics,
        production_coverage,
        production_prediction,
        benchmark_identity,
    ) = evaluate_benchmark_model(
        production_test,
        batch_size=args.batch_size,
        device=device,
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
        "production_model_name": benchmark_identity["model_name"],
        "production_active_generation_id": benchmark_identity[
            "active_generation_id"
        ],
        "production_model_sha256": benchmark_identity["model_sha256"],
        "production_trained_at": benchmark_identity["trained_at"],
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
    common = {
        "artifact_version": 1,
        "trained_at": trained_at,
        "model_name": "vega",
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
        "training_device": str(device),
        "training_device_name": device_name,
        "torch_version": torch.__version__,
        "torch_cuda_version": torch.version.cuda,
        "training_acceleration": acceleration,
        "training_peak_allocated_bytes": peak_allocated_bytes,
        "training_peak_reserved_bytes": peak_reserved_bytes,
    }
    metrics = {
        **common,
        "architecture": ARCHITECTURE_ID,
        "parameters": parameter_count,
        "device": str(device),
        "device_name": device_name,
        "torch_cuda_version": torch.version.cuda,
        "mixed_precision_training": device.type == "cuda",
        "training_autocast_dtype": "float16" if device.type == "cuda" else None,
        "quality_evaluation_dtype": "float32",
        "quality_evaluation_tf32": False,
        "tf32_training": acceleration,
        "optimizer": "AdamW",
        "optimizer_fused": fused_optimizer,
        "scheduler": "linear_warmup_cosine_decay",
        "warmup_epochs": args.warmup_epochs,
        "train_rows": len(train_arrays[2]),
        "validation_rows": len(validation_arrays[2]),
        "test_rows": len(test_arrays[2]),
        "best_epoch": best_epoch,
        "best_validation_mae_log_premium": best_validation,
        "test": candidate_test_metrics,
        "reference_baseline_test": price_metrics(
            test_price, np.expm1(test_reference)
        ),
        "test_interval_coverage_15_85": interval_coverage,
        "test_raw_interval_coverage_15_85": raw_interval_coverage,
        "test_raw_quantile_crossing_rate": raw_crossing_rate,
        "test_point_outside_raw_interval_rate": point_outside_raw_rate,
        "bootstrap_benchmark": benchmark_identity,
        "shared_holdout_comparison": comparison,
        "calibration": calibration,
        "calibration_containment": calibration_containment,
        "premium_scale": premium_scale,
        "history": history,
    }
    metadata = {
        **common,
        "architecture": ARCHITECTURE_ID,
        "feature_cols": list(CAT_FEATURES) + list(NUMERIC_FEATURES),
        "model_config": model_config,
        "cat_features": CAT_FEATURES,
        "numeric_features": NUMERIC_FEATURES,
        "vocabularies": vocabularies,
        "numeric_medians": medians,
        "numeric_scales": scales,
        "seed_encoder_counts": seed_encoder_counts,
        "runtime_history": runtime_history,
        "train_bounds": train_bounds,
        "validation_bounds": validation_bounds,
        "test_bounds": test_bounds,
        "target": "log_premium_over_reference_without_keychain_component",
        "calibration": calibration,
        "calibration_containment": calibration_containment,
        "premium_scale": premium_scale,
        "test_metrics": candidate_test_metrics,
        "test_interval_coverage_15_85": interval_coverage,
    }

    artifact_dir.mkdir(parents=True, exist_ok=True)
    atomic_torch_save(best_state, model_path)
    model_sha256 = build_cache.file_sha256(model_path)
    metadata["model_sha256"] = model_sha256
    metrics["model_sha256"] = model_sha256
    atomic_json_dump(metadata, metadata_path, compact=True)
    atomic_json_dump(metrics, metrics_path)
    print(json.dumps(candidate_test_metrics, indent=2), flush=True)
    print(f"interval coverage: {interval_coverage:.3f}", flush=True)
    print(f"benchmark: {json.dumps(benchmark_identity, sort_keys=True)}", flush=True)
    print(f"saved staging candidate: {model_path}", flush=True)


if __name__ == "__main__":
    main()
