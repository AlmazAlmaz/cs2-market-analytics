import hashlib
import json
import os
from pathlib import Path

import pytest
import torch

from item_classification import ITEM_KIND_WEAPON_SKIN, STRICT_ITEM_KIND_POLICY
from VEGAMODEL import promote_candidate as promotion
from VEGAMODEL.features import (
    CAT_FEATURES,
    FEATURE_CONTRACT_SHA256,
    FEATURE_CONTRACT_VERSION,
    NUMERIC_FEATURES,
)
from VEGAMODEL.model import ARCHITECTURE_ID, VegaPriceNet

BENCHMARK_IDENTITY = {
    "model_name": "nova",
    "active_generation_id": "a" * 64,
    "model_sha256": "b" * 64,
    "trained_at": "2026-08-29T10:00:00+00:00",
}
CANDIDATE_TRAINED_AT = "2026-08-30T10:00:00+00:00"


def _model_config():
    return {
        "category_cardinalities": [2] * len(CAT_FEATURES),
        "numeric_count": len(NUMERIC_FEATURES),
        "d_token": 8,
        "block_count": 1,
        "head_count": 2,
        "ffn_hidden": 16,
        "attention_dropout": 0.0,
        "residual_dropout": 0.0,
        "head_dropout": 0.0,
    }


def _write_documents(candidate, metadata, metrics):
    (candidate / promotion.METADATA_FILENAME).write_text(
        json.dumps(metadata, sort_keys=True), encoding="utf-8"
    )
    (candidate / promotion.METRICS_FILENAME).write_text(
        json.dumps(metrics, sort_keys=True), encoding="utf-8"
    )


def _write_bundle(candidate: Path, *, benchmark=None):
    candidate.mkdir(parents=True)
    config = _model_config()
    model = VegaPriceNet(**config)
    state = {name: value.detach().float() for name, value in model.state_dict().items()}
    model_path = candidate / promotion.MODEL_FILENAME
    torch.save(state, model_path)
    model_sha256 = promotion._sha256_file(model_path)

    calibration = {"low": -0.1, "center": 0.0, "high": 0.1}
    containment = {
        "target_max_outside_rate": 0.006,
        "validation_point_outside_raw_rate_before": 0.004,
        "validation_point_outside_raw_rate_after": 0.0,
        "low_point_containment_margin": 0.0,
        "high_point_containment_margin": 0.0,
    }
    source = {
        "item_kind": ITEM_KIND_WEAPON_SKIN,
        "item_kind_policy": STRICT_ITEM_KIND_POLICY,
        "logical_sha256": "c" * 64,
        "valid_price_rows": 100_000,
    }
    split = {
        "method": "fixed_newest_rows_with_timestamp_ties",
        "train_rows": 60_000,
        "validation_rows": 15_000,
        "test_rows": 25_000,
        "test_untouched_until_final_evaluation": True,
    }
    common = {
        "artifact_version": 1,
        "trained_at": CANDIDATE_TRAINED_AT,
        "model_name": "vega",
        "item_kind": ITEM_KIND_WEAPON_SKIN,
        "stage": "staging",
        "production_promoted": False,
        "rows": 100_000,
        "fit_train_rows": 60_000,
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": FEATURE_CONTRACT_SHA256,
        "source_database": source,
        "cache_sha256": "d" * 64,
        "cache_schema_version": 5,
        "split": split,
        "row_exclusions": {"source_rows": 100_000},
        "architecture": ARCHITECTURE_ID,
        "calibration": calibration,
        "calibration_containment": containment,
        "premium_scale": 1.0,
        "test_interval_coverage_15_85": 0.70,
        "model_sha256": model_sha256,
    }
    production = {
        "median_ape_percent": 10.0,
        "mean_ape_percent": 20.0,
        "p90_ape_percent": 40.0,
        "mae_cents": 100.0,
    }
    staging = {
        "median_ape_percent": 9.0,
        "mean_ape_percent": 18.0,
        "p90_ape_percent": 35.0,
        "mae_cents": 105.0,
    }
    deltas = {key: staging[key] - production[key] for key in promotion.COMPARISON_METRICS}
    benchmark = dict(benchmark or BENCHMARK_IDENTITY)
    comparison = {
        "holdout_rows": 25_000,
        "production_model_name": benchmark["model_name"],
        "production_active_generation_id": benchmark["active_generation_id"],
        "production_model_sha256": benchmark["model_sha256"],
        "production_trained_at": benchmark["trained_at"],
        "production": production,
        "staging": staging,
        "staging_minus_production": deltas,
        "production_interval_coverage_15_85": 0.68,
        "staging_interval_coverage_15_85": 0.69,
        "candidate_row_win_percent": 55.0,
        "staging_proves_primary_improvement": True,
    }
    metrics = {
        **common,
        "parameters": sum(value.numel() for value in state.values()),
        "device": "cuda",
        "device_name": "NVIDIA GeForce RTX 4070 SUPER",
        "mixed_precision_training": True,
        "training_autocast_dtype": "float16",
        "quality_evaluation_dtype": "float32",
        "quality_evaluation_tf32": False,
        "train_rows": 60_000,
        "validation_rows": 15_000,
        "test_rows": 25_000,
        "test": staging,
        "test_raw_interval_coverage_15_85": 0.69,
        "test_raw_quantile_crossing_rate": 0.0,
        "test_point_outside_raw_interval_rate": 0.0,
        "bootstrap_benchmark": benchmark,
        "shared_holdout_comparison": comparison,
    }
    metadata = {
        **common,
        "feature_cols": list(CAT_FEATURES) + list(NUMERIC_FEATURES),
        "model_config": config,
        "cat_features": list(CAT_FEATURES),
        "numeric_features": list(NUMERIC_FEATURES),
        "vocabularies": {column: {} for column in CAT_FEATURES},
        "numeric_medians": {column: 0.0 for column in NUMERIC_FEATURES},
        "numeric_scales": {column: 1.0 for column in NUMERIC_FEATURES},
        "seed_encoder_counts": {},
        "runtime_history": {},
        "target": "log_premium_over_reference_without_keychain_component",
        "test_metrics": staging,
    }
    _write_documents(candidate, metadata, metrics)
    return metadata, metrics


@pytest.fixture(autouse=True)
def active_benchmark(monkeypatch):
    monkeypatch.setattr(
        promotion,
        "_active_benchmark_identity",
        lambda _project: dict(BENCHMARK_IDENTITY),
    )
    monkeypatch.setattr(
        promotion,
        "EXPECTED_ARCHITECTURE_CONFIG",
        {
            key: value
            for key, value in _model_config().items()
            if key not in {"category_cardinalities", "numeric_count"}
        },
    )


def _validate(candidate):
    return promotion.validate_candidate(candidate, project_dir=candidate.parent)


def test_valid_candidate_is_read_only_and_returns_exact_manifest(tmp_path):
    candidate = tmp_path / "candidate"
    _write_bundle(candidate)
    before = {path.name: path.read_bytes() for path in candidate.iterdir()}

    manifest = _validate(candidate)

    assert set(manifest["files"]) == set(promotion.BUNDLE_FILES)
    assert manifest["bundle_sha256"] == promotion._canonical_sha256(manifest["files"])
    assert manifest["benchmark"] == BENCHMARK_IDENTITY
    assert manifest["quality_gate"]["strictly_improved_metrics"] == list(
        promotion.PRIMARY_METRICS
    )
    assert {path.name: path.read_bytes() for path in candidate.iterdir()} == before


def test_validator_uses_weights_only_and_strict_state_loading(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    _write_bundle(candidate)
    real_load = promotion.torch.load
    calls = []

    def recording_load(*args, **kwargs):
        calls.append(kwargs.copy())
        return real_load(*args, **kwargs)

    monkeypatch.setattr(promotion.torch, "load", recording_load)

    _validate(candidate)

    assert calls and all(call.get("weights_only") is True for call in calls)
    assert all(call.get("map_location") == "cpu" for call in calls)


@pytest.mark.parametrize("missing", promotion.BUNDLE_FILES)
def test_validator_rejects_incomplete_bundle(tmp_path, missing):
    candidate = tmp_path / "candidate"
    _write_bundle(candidate)
    (candidate / missing).unlink()

    with pytest.raises(promotion.PromotionError, match="exactly its three"):
        _validate(candidate)


def test_validator_rejects_unexpected_fourth_file(tmp_path):
    candidate = tmp_path / "candidate"
    _write_bundle(candidate)
    (candidate / "training.log").write_text("extra", encoding="utf-8")

    with pytest.raises(promotion.PromotionError, match="unexpected"):
        _validate(candidate)


def test_validator_rejects_model_sha256_mismatch(tmp_path):
    candidate = tmp_path / "candidate"
    metadata, metrics = _write_bundle(candidate)
    metadata["model_sha256"] = "e" * 64
    metrics["model_sha256"] = "e" * 64
    _write_documents(candidate, metadata, metrics)

    with pytest.raises(promotion.PromotionError, match="model bytes differ"):
        _validate(candidate)


def test_validator_rejects_unsafe_architecture_before_loading_state(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    metadata, metrics = _write_bundle(candidate)
    metadata["model_config"]["d_token"] = 4096
    _write_documents(candidate, metadata, metrics)

    def should_not_load(*_args, **_kwargs):
        raise AssertionError("unsafe config reached torch.load")

    monkeypatch.setattr(promotion.torch, "load", should_not_load)
    with pytest.raises(promotion.PromotionError, match="d_token must be at most"):
        _validate(candidate)


def test_validator_rejects_bounded_but_nonproduction_architecture(
    tmp_path, monkeypatch
):
    candidate = tmp_path / "candidate"
    metadata, metrics = _write_bundle(candidate)
    metadata["model_config"]["block_count"] = 2
    _write_documents(candidate, metadata, metrics)

    def should_not_load(*_args, **_kwargs):
        raise AssertionError("nonproduction config reached torch.load")

    monkeypatch.setattr(promotion.torch, "load", should_not_load)
    with pytest.raises(promotion.PromotionError, match="optimized production"):
        _validate(candidate)


def test_validator_rejects_state_that_does_not_strictly_match_config(tmp_path):
    candidate = tmp_path / "candidate"
    metadata, metrics = _write_bundle(candidate)
    model_path = candidate / promotion.MODEL_FILENAME
    state = torch.load(model_path, map_location="cpu", weights_only=True)
    state.pop(next(iter(state)))
    torch.save(state, model_path)
    sha256 = promotion._sha256_file(model_path)
    metadata["model_sha256"] = sha256
    metrics["model_sha256"] = sha256
    metrics["parameters"] = sum(value.numel() for value in state.values())
    _write_documents(candidate, metadata, metrics)

    with pytest.raises(promotion.PromotionError, match="state dict and model_config"):
        _validate(candidate)


def test_validator_rejects_nonfinite_state(tmp_path):
    candidate = tmp_path / "candidate"
    metadata, metrics = _write_bundle(candidate)
    model_path = candidate / promotion.MODEL_FILENAME
    state = torch.load(model_path, map_location="cpu", weights_only=True)
    first = next(iter(state))
    state[first].view(-1)[0] = float("nan")
    torch.save(state, model_path)
    sha256 = promotion._sha256_file(model_path)
    metadata["model_sha256"] = sha256
    metrics["model_sha256"] = sha256
    _write_documents(candidate, metadata, metrics)

    with pytest.raises(promotion.PromotionError, match="non-finite"):
        _validate(candidate)


def test_validator_requires_exact_active_benchmark_identity(tmp_path):
    candidate = tmp_path / "candidate"
    metadata, metrics = _write_bundle(candidate)
    metrics["bootstrap_benchmark"]["model_sha256"] = "f" * 64
    _write_documents(candidate, metadata, metrics)

    with pytest.raises(promotion.PromotionError, match="exact active benchmark"):
        _validate(candidate)


def test_validator_accepts_exact_active_vega_identity(tmp_path, monkeypatch):
    identity = {
        "model_name": "vega",
        "active_generation_id": "1" * 64,
        "model_sha256": "2" * 64,
        "trained_at": "2026-08-29T12:00:00+00:00",
    }
    monkeypatch.setattr(
        promotion, "_active_benchmark_identity", lambda _project: dict(identity)
    )
    candidate = tmp_path / "candidate"
    _write_bundle(candidate, benchmark=identity)

    manifest = _validate(candidate)

    assert manifest["benchmark"] == identity


def test_validator_rejects_candidate_older_than_active_benchmark(tmp_path, monkeypatch):
    identity = {
        **BENCHMARK_IDENTITY,
        "trained_at": "2026-08-31T10:00:00+00:00",
    }
    monkeypatch.setattr(
        promotion, "_active_benchmark_identity", lambda _project: dict(identity)
    )
    candidate = tmp_path / "candidate"
    _write_bundle(candidate, benchmark=identity)

    with pytest.raises(promotion.PromotionError, match="must be newer"):
        _validate(candidate)


def test_validator_detects_active_benchmark_change(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    _write_bundle(candidate)
    calls = 0

    def changing_identity(_project):
        nonlocal calls
        calls += 1
        if calls == 1:
            return dict(BENCHMARK_IDENTITY)
        return {**BENCHMARK_IDENTITY, "active_generation_id": "9" * 64}

    monkeypatch.setattr(promotion, "_active_benchmark_identity", changing_identity)

    with pytest.raises(promotion.PromotionError, match="changed while Vega"):
        _validate(candidate)


def test_validator_detects_candidate_change_during_model_inspection(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    _write_bundle(candidate)
    real_inspect = promotion._inspect_model

    def mutating_inspect(path, config):
        result = real_inspect(path, config)
        metrics_path = candidate / promotion.METRICS_FILENAME
        metrics_path.write_bytes(metrics_path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(promotion, "_inspect_model", mutating_inspect)

    with pytest.raises(promotion.PromotionError, match="changed while"):
        _validate(candidate)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("proof", "does not prove"),
        ("median", "median_ape_percent is not strictly better"),
        ("coverage", "interval coverage must be"),
        ("raw_coverage", "raw interval coverage must be"),
        ("crossing", "crossing rate exceeds"),
        ("point_outside", "point-outside-interval rate exceeds"),
        ("holdout", "at least 25000"),
    ],
)
def test_numerical_gate_failures_are_classified_as_quality(
    tmp_path, mutation, message
):
    candidate = tmp_path / "candidate"
    metadata, metrics = _write_bundle(candidate)
    comparison = metrics["shared_holdout_comparison"]
    if mutation == "proof":
        comparison["staging_proves_primary_improvement"] = False
    elif mutation == "median":
        value = comparison["production"]["median_ape_percent"]
        comparison["staging"]["median_ape_percent"] = value
        comparison["staging_minus_production"]["median_ape_percent"] = 0.0
        metrics["test"]["median_ape_percent"] = value
        metadata["test_metrics"]["median_ape_percent"] = value
    elif mutation == "coverage":
        metrics["test_interval_coverage_15_85"] = 0.59
        metadata["test_interval_coverage_15_85"] = 0.59
    elif mutation == "raw_coverage":
        metrics["test_raw_interval_coverage_15_85"] = 0.59
        comparison["staging_interval_coverage_15_85"] = 0.59
    elif mutation == "crossing":
        metrics["test_raw_quantile_crossing_rate"] = 0.011
    elif mutation == "point_outside":
        metrics["test_point_outside_raw_interval_rate"] = 0.011
    else:
        comparison["holdout_rows"] = 24_999
        metrics["test_rows"] = 24_999
        metadata["split"]["test_rows"] = 24_999
        metrics["split"]["test_rows"] = 24_999
    _write_documents(candidate, metadata, metrics)

    with pytest.raises(promotion.PromotionError, match=message) as caught:
        _validate(candidate)
    assert caught.value.classification == "quality"


def test_calibration_must_be_ordered_and_containment_coherent(tmp_path):
    candidate = tmp_path / "candidate"
    metadata, metrics = _write_bundle(candidate)
    metadata["calibration"] = {"low": 0.1, "center": 0.0, "high": 0.2}
    metrics["calibration"] = dict(metadata["calibration"])
    _write_documents(candidate, metadata, metrics)

    with pytest.raises(promotion.PromotionError, match="low <= center <= high"):
        _validate(candidate)

    metadata, metrics = _write_bundle(tmp_path / "second")
    metrics["calibration_containment"]["validation_point_outside_raw_rate_after"] = 0.01
    metadata["calibration_containment"] = dict(metrics["calibration_containment"])
    _write_documents(tmp_path / "second", metadata, metrics)
    with pytest.raises(promotion.PromotionError, match="containment target"):
        _validate(tmp_path / "second")


@pytest.mark.skipif(not hasattr(os, "O_BINARY"), reason="Windows CRT regression")
def test_hash_and_copy_preserve_binary_bytes_on_windows(tmp_path, monkeypatch):
    source = tmp_path / "source.pt"
    destination = tmp_path / "destination.pt"
    payload = b"torch\r\n\x1a\x00\xff\r\nstate"
    source.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    real_open = os.open
    observed_flags = []

    def recording_open(path, flags, mode=0o777):
        observed_flags.append(flags)
        return real_open(path, flags, mode)

    monkeypatch.setattr(promotion.os, "open", recording_open)

    assert promotion._sha256_file(source) == expected
    promotion._copy_verified(source, destination, expected)

    assert destination.read_bytes() == payload
    assert observed_flags
    assert all(flags & os.O_BINARY for flags in observed_flags)
