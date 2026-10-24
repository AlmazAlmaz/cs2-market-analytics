import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import torch

import train_vega_model
import vega_model_runtime
from item_classification import ITEM_KIND_WEAPON_SKIN
from VEGAMODEL.features import (
    CAT_FEATURES,
    FEATURE_CONTRACT_SHA256,
    FEATURE_CONTRACT_VERSION,
    FEATURE_TOKEN_COUNT,
    NUMERIC_FEATURES,
)
from VEGAMODEL.model import ARCHITECTURE_ID, VegaPriceNet, VegaPriceTransformer


def _model_config():
    return {
        **train_vega_model.MODEL_CONFIG,
        "category_cardinalities": [2] * len(CAT_FEATURES),
    }


def _metadata():
    return {
        "artifact_version": 1,
        "trained_at": "2026-08-31T12:00:00+00:00",
        "model_name": "vega",
        "item_kind": ITEM_KIND_WEAPON_SKIN,
        "architecture": ARCHITECTURE_ID,
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": FEATURE_CONTRACT_SHA256,
        "feature_cols": list(CAT_FEATURES) + list(NUMERIC_FEATURES),
        "cat_features": list(CAT_FEATURES),
        "numeric_features": list(NUMERIC_FEATURES),
        "model_config": _model_config(),
        "vocabularies": {column: {} for column in CAT_FEATURES},
        "numeric_medians": {column: 0.0 for column in NUMERIC_FEATURES},
        "numeric_scales": {column: 1.0 for column in NUMERIC_FEATURES},
        "seed_encoder_counts": {},
        "runtime_history": {
            "policy": "frozen_at_fit_train_boundary",
            "premium_by_weapon": {},
            "count_by_weapon": {},
            "global_premium": 0.0,
        },
        "calibration": {"low": -0.1, "center": 0.0, "high": 0.1},
        "premium_scale": 1.0,
    }


def _write_bundle(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    metadata = _metadata()
    model = VegaPriceNet(**metadata["model_config"])
    model_path = directory / vega_model_runtime.MODEL_PATH.name
    torch.save(model.state_dict(), model_path)
    metadata["model_sha256"] = vega_model_runtime.build_cache.file_sha256(
        model_path
    )
    (directory / vega_model_runtime.METADATA_PATH.name).write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    return metadata, model_path


def test_vega_architecture_tokenizes_all_features_and_orders_quantiles():
    model = VegaPriceNet(
        [4] * len(CAT_FEATURES),
        len(NUMERIC_FEATURES),
        d_token=32,
        block_count=2,
        head_count=4,
        ffn_hidden=64,
        attention_dropout=0.0,
        residual_dropout=0.0,
        head_dropout=0.0,
    ).eval()
    categories = torch.zeros((3, len(CAT_FEATURES)), dtype=torch.int64)
    numeric = torch.randn((3, len(NUMERIC_FEATURES)), dtype=torch.float32)

    tokens = model.tokenize(categories, numeric)
    with torch.inference_mode():
        output = model(categories, numeric)

    assert VegaPriceNet is VegaPriceTransformer
    assert FEATURE_TOKEN_COUNT == 69
    assert tokens.shape == (3, FEATURE_TOKEN_COUNT, 32)
    assert output.shape == (3, 3)
    assert torch.all(output[:, 0] <= output[:, 1])
    assert torch.all(output[:, 1] <= output[:, 2])


def test_validation_calibration_makes_point_containment_structural():
    predictions = np.array(
        [[-0.05, 0.0, 0.04], [-0.3, 0.2, 0.22], [0.1, 0.11, 0.5]],
        dtype=np.float64,
    )

    calibration, diagnostics = train_vega_model.enforce_ordered_calibration(
        predictions,
        premium_scale=0.8,
        calibration={"low": 0.4, "center": 0.1, "high": -0.2},
    )

    assert calibration == {"low": 0.1, "center": 0.1, "high": 0.1}
    assert diagnostics["validation_point_outside_raw_rate_before"] > 0
    assert diagnostics["validation_point_outside_raw_rate_after"] == 0
    assert diagnostics["target_max_outside_rate"] == pytest.approx(0.006)
    # Once the offsets themselves are ordered, every intrinsically ordered
    # future output is contained too; no test-row-derived margin is involved.
    future = np.array([[-1e-8, 0.0, 1e-8], [-10.0, 2.0, 3.0]]) * 0.8
    future[:, 0] += calibration["low"]
    future[:, 1] += calibration["center"]
    future[:, 2] += calibration["high"]
    assert np.all(future[:, 0] <= future[:, 1])
    assert np.all(future[:, 1] <= future[:, 2])


def test_benchmark_selection_ignores_unactivated_legacy_vega(monkeypatch):
    monkeypatch.setattr(
        train_vega_model.model_generations,
        "read_active_pointer",
        lambda model, **_kwargs: None,
    )

    assert train_vega_model.select_benchmark_model() == "nova"
    with pytest.raises(RuntimeError, match="exact active nova generation"):
        train_vega_model._load_benchmark_identity("nova")


def test_argument_validation_rejects_negative_training_sample():
    args = train_vega_model.parse_args(
        ["--allow-cpu", "--device", "cpu", "--max-train-rows", "-1"]
    )
    with pytest.raises(ValueError, match="cannot be negative"):
        train_vega_model._validate_arguments(args)


def test_fp32_evaluation_batch_is_bounded_and_cuda_cache_is_released(monkeypatch):
    assert train_vega_model.evaluation_batch_size(1_024) == 1_024
    assert train_vega_model.evaluation_batch_size(2_048) == 2_048
    assert train_vega_model.evaluation_batch_size(8_192) == 2_048
    with pytest.raises(ValueError, match="batch size must be positive"):
        train_vega_model.evaluation_batch_size(0)

    calls = []
    monkeypatch.setattr(
        train_vega_model.torch.cuda,
        "empty_cache",
        lambda: calls.append("released"),
    )
    train_vega_model.release_evaluation_cuda_cache(torch.device("cpu"))
    train_vega_model.release_evaluation_cuda_cache(torch.device("cuda"))
    assert calls == ["released"]


def test_runtime_loads_weights_only_and_checks_model_sha(monkeypatch, tmp_path):
    _metadata_value, model_path = _write_bundle(tmp_path)
    original_load = torch.load
    calls = []

    def audited_load(*args, **kwargs):
        calls.append(kwargs.copy())
        return original_load(*args, **kwargs)

    monkeypatch.setattr(vega_model_runtime.torch, "load", audited_load)
    bundle = vega_model_runtime.load_bundle_from_directory(tmp_path)

    assert calls == [{"map_location": "cpu", "weights_only": True}]
    assert bundle["model"].training is False
    vega_model_runtime.smoke_bundle(bundle)

    with model_path.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(RuntimeError, match="SHA-256"):
        vega_model_runtime.load_bundle_from_directory(tmp_path)


def test_runtime_metadata_requires_frozen_history_and_ordered_calibration():
    metadata = _metadata()
    metadata["model_sha256"] = "0" * 64
    vega_model_runtime.validate_metadata_contract(metadata)

    without_history = {**metadata, "runtime_history": {}}
    with pytest.raises(RuntimeError, match="frozen runtime-history"):
        vega_model_runtime.validate_metadata_contract(without_history)

    unordered = {
        **metadata,
        "calibration": {"low": 0.2, "center": 0.0, "high": 0.1},
    }
    with pytest.raises(RuntimeError, match="not ordered"):
        vega_model_runtime.validate_metadata_contract(unordered)


def test_runtime_refuses_legacy_bundle_without_active_pointer(monkeypatch):
    monkeypatch.setattr(
        vega_model_runtime.model_generations,
        "read_active_pointer",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        vega_model_runtime,
        "_runtime_slot",
        lambda: pytest.fail("legacy Vega must not be loaded"),
    )

    with pytest.raises(RuntimeError, match="no active generation"):
        vega_model_runtime.load_model()


def test_prediction_exposes_ordered_point_low_high(monkeypatch):
    class OrderedModel(torch.nn.Module):
        def forward(self, _categories, _numeric):
            return torch.tensor([[-0.2, 0.0, 0.3]], dtype=torch.float32)

    metadata = {
        "artifact_version": 1,
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": FEATURE_CONTRACT_SHA256,
        "trained_at": "2026-08-31T12:00:00+00:00",
        "premium_scale": 1.0,
        "calibration": {"low": -0.1, "center": 0.0, "high": 0.1},
        "test_metrics": {},
    }
    frame = pd.DataFrame(
        [
            {
                "log_reference_predicted_price": np.log1p(1_000),
                "weapon_type": "AK-47 | Test",
                "wear_name": "Field-Tested",
                "log_sum_ref_sticker_price": 0.0,
                "sticker_count": 0,
            }
        ]
    )
    monkeypatch.setattr(
        vega_model_runtime,
        "classify_market_hash_name",
        lambda *_args, **_kwargs: ITEM_KIND_WEAPON_SKIN,
    )
    monkeypatch.setattr(
        vega_model_runtime,
        "load_model",
        lambda: {
            "model": OrderedModel(),
            "metadata": metadata,
            "_runtime_generation_id": "a" * 64,
        },
    )
    monkeypatch.setattr(
        vega_model_runtime,
        "build_feature_frame",
        lambda *_args, **_kwargs: (
            frame,
            np.zeros((1, 1), dtype=np.int64),
            np.zeros((1, 1), dtype=np.float32),
            [],
            {
                "liquidity_score": 50.0,
                "sales_per_day": 1.0,
                "historical_sales_count": 10,
            },
            {},
        ),
    )

    result = vega_model_runtime.predict_deal(
        {"name": "AK-47 | Test (Field-Tested)", "buy_price_cents": 900}
    )

    assert result["model_name"] == "vega"
    assert result["advisory_only"] is True
    assert result["low_price_cents"] <= result["point_price_cents"]
    assert result["point_price_cents"] == result["predicted_price_cents"]
    assert result["point_price_cents"] <= result["high_price_cents"]
