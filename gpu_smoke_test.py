"""Run real Nova/Vega steps and short CatBoost fits on the NVIDIA GPU."""

from __future__ import annotations

import argparse
import gc
import json
import time

import numpy as np
import torch
from catboost import CatBoostRegressor, Pool
from catboost.utils import get_gpu_device_count

from NEURALMODEL.features import CAT_FEATURES, NUMERIC_FEATURES, embedding_dim
from NEURALMODEL.model import NovaPriceNet
from train_neural_model import training_loss
from VEGAMODEL.model import VegaPriceNet


def run_smoke_test(device_index: int = 0) -> dict:
    if not torch.cuda.is_available():
        raise RuntimeError("PyTorch CUDA is unavailable")
    if int(get_gpu_device_count()) <= device_index:
        raise RuntimeError("CatBoost cannot see the requested NVIDIA GPU")

    device = torch.device(f"cuda:{device_index}")
    torch.cuda.set_device(device)
    torch.cuda.reset_peak_memory_stats(device)
    cardinalities = [128 + index for index in range(len(CAT_FEATURES))]
    model = NovaPriceNet(
        category_cardinalities=cardinalities,
        embedding_dims=[embedding_dim(value) for value in cardinalities],
        numeric_count=len(NUMERIC_FEATURES),
        width=512,
        block_count=5,
        dropout=0.08,
    ).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=8e-4)
    batch_size = 2048
    categories = torch.stack(
        [
            torch.randint(0, cardinality, (batch_size,), device=device)
            for cardinality in cardinalities
        ],
        dim=1,
    )
    numeric = torch.randn(
        batch_size, len(NUMERIC_FEATURES), device=device, dtype=torch.float32
    )
    target = torch.randn(batch_size, device=device)
    weight = torch.ones(batch_size, device=device)
    started = time.perf_counter()
    optimizer.zero_grad(set_to_none=True)
    loss = training_loss(model(categories, numeric), target, weight)
    loss.backward()
    optimizer.step()
    torch.cuda.synchronize(device)
    nova_seconds = time.perf_counter() - started
    nova_peak = int(torch.cuda.max_memory_allocated(device))
    if nova_peak <= 0:
        raise RuntimeError("Nova did not allocate CUDA memory")
    nova_loss = float(loss.detach().cpu())

    # Vega is deliberately tested separately: retaining Nova's optimizer state
    # would make the peak-memory result depend on whichever model ran first.
    del loss, optimizer, model, categories, numeric, target, weight
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats(device)

    vega_model = VegaPriceNet(
        category_cardinalities=cardinalities,
        numeric_count=len(NUMERIC_FEATURES),
        d_token=192,
        block_count=4,
        head_count=8,
        ffn_hidden=768,
        attention_dropout=0.05,
        residual_dropout=0.08,
        head_dropout=0.05,
    ).to(device)
    try:
        vega_optimizer = torch.optim.AdamW(
            vega_model.parameters(), lr=6e-4, fused=True
        )
        vega_fused_adamw = True
    except (RuntimeError, TypeError):
        vega_optimizer = torch.optim.AdamW(vega_model.parameters(), lr=6e-4)
        vega_fused_adamw = False
    try:
        scaler = torch.amp.GradScaler("cuda")
    except (AttributeError, TypeError):  # pragma: no cover - old PyTorch fallback
        scaler = torch.cuda.amp.GradScaler()
    categories = torch.stack(
        [
            torch.randint(0, cardinality, (batch_size,), device=device)
            for cardinality in cardinalities
        ],
        dim=1,
    )
    numeric = torch.randn(
        batch_size, len(NUMERIC_FEATURES), device=device, dtype=torch.float32
    )
    target = torch.randn(batch_size, device=device)
    weight = torch.ones(batch_size, device=device)
    started = time.perf_counter()
    vega_optimizer.zero_grad(set_to_none=True)
    with torch.autocast(device_type="cuda", dtype=torch.float16):
        vega_output = vega_model(categories, numeric)
        vega_loss = training_loss(vega_output, target, weight)
    scaler.scale(vega_loss).backward()
    scaler.step(vega_optimizer)
    scaler.update()
    torch.cuda.synchronize(device)
    vega_seconds = time.perf_counter() - started
    vega_peak = int(torch.cuda.max_memory_allocated(device))
    if vega_peak <= 0:
        raise RuntimeError("Vega did not allocate CUDA memory")
    if not torch.isfinite(vega_loss):
        raise RuntimeError("Vega produced a non-finite training loss")
    ordered_quantiles = bool(
        torch.all(vega_output[:, 0] <= vega_output[:, 1]).item()
        and torch.all(vega_output[:, 1] <= vega_output[:, 2]).item()
    )
    if not ordered_quantiles:
        raise RuntimeError("Vega produced unordered quantiles")
    vega_loss_value = float(vega_loss.detach().cpu())
    del (
        scaler,
        vega_loss,
        vega_optimizer,
        vega_model,
        vega_output,
        categories,
        numeric,
        target,
        weight,
    )
    gc.collect()
    torch.cuda.empty_cache()

    rng = np.random.default_rng(42)
    rows = 16_384
    features = rng.normal(size=(rows, 12)).astype(np.float32)
    labels = (
        features[:, 0] * 0.8
        - features[:, 1] * 0.3
        + rng.normal(scale=0.2, size=rows)
    ).astype(np.float32)
    train_pool = Pool(features[:12_000], labels[:12_000])
    validation_pool = Pool(features[12_000:], labels[12_000:])
    catboost_results = {}
    for name, loss_function in (
        ("mae", "MAE"),
        ("quantile", "Quantile:alpha=0.15"),
    ):
        started = time.perf_counter()
        estimator = CatBoostRegressor(
            loss_function=loss_function,
            iterations=12,
            depth=6,
            learning_rate=0.1,
            task_type="GPU",
            devices=str(device_index),
            allow_writing_files=False,
            verbose=False,
        )
        estimator.fit(train_pool, eval_set=validation_pool)
        catboost_results[name] = {
            "trees": int(estimator.tree_count_),
            "seconds": round(time.perf_counter() - started, 4),
        }

    return {
        "ok": True,
        "torch": torch.__version__,
        "torch_cuda": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(device),
        "nova": {
            "loss": nova_loss,
            "seconds": round(nova_seconds, 4),
            "peak_cuda_memory_mb": round(nova_peak / 1024**2, 1),
        },
        "vega": {
            "loss": vega_loss_value,
            "seconds": round(vega_seconds, 4),
            "peak_cuda_memory_mb": round(vega_peak / 1024**2, 1),
            "batch_size": batch_size,
            "mixed_precision": "float16",
            "fused_adamw": vega_fused_adamw,
            "ordered_quantiles": ordered_quantiles,
        },
        "catboost": catboost_results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_smoke_test(args.device)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
