import hashlib
import inspect
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import joblib
import pytest

import model_candidate_promotion as promotion
import model_generations

PRODUCTION_TIME = "2026-08-10T10:00:00+00:00"
CANDIDATE_TIME = "2026-08-11T10:00:00+00:00"


class FakeEstimator:
    def __init__(self, loss, quantile=None):
        self.loss = loss
        self.quantile = quantile


class FakePipeline:
    def __init__(self, features, loss, quantile=None):
        self.feature_names_in_ = list(features)
        self.named_steps = {"model": FakeEstimator(loss, quantile)}

    def predict(self, values):
        return [0.0] * len(values)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _profile(monkeypatch, tmp_path: Path, model: str):
    monkeypatch.setattr(promotion, "PROJECT_DIR", tmp_path)
    candidate = tmp_path / model / "candidate"
    production = tmp_path / model / "production"
    backups = tmp_path / model / "backups"
    candidate.mkdir(parents=True)
    production.mkdir(parents=True)
    profile = replace(
        promotion.MODEL_PROFILES[model],
        candidate_dir=candidate,
        production_dir=production,
        backup_root=backups,
    )
    monkeypatch.setitem(promotion.MODEL_PROFILES, model, profile)
    return profile


def _comparison(model: str):
    if model == "almaz":
        old = {
            "median_absolute_percentage_error": 0.12,
            "mean_absolute_percentage_error": 0.18,
            "p90_absolute_percentage_error": 0.35,
            "mae_cents": 200.0,
        }
        new = {
            "median_absolute_percentage_error": 0.10,
            "mean_absolute_percentage_error": 0.16,
            "p90_absolute_percentage_error": 0.30,
            "mae_cents": 180.0,
        }
        primary = (
            "median_absolute_percentage_error",
            "mean_absolute_percentage_error",
            "p90_absolute_percentage_error",
        )
    else:
        old = {
            "median_ape_percent": 7.0,
            "mean_ape_percent": 13.0,
            "p90_ape_percent": 29.0,
            "mae_cents": 200.0,
        }
        new = {
            "median_ape_percent": 6.0,
            "mean_ape_percent": 12.0,
            "p90_ape_percent": 27.0,
            "mae_cents": 180.0,
        }
        primary = ("median_ape_percent", "mean_ape_percent", "p90_ape_percent")
    deltas = {key: new[key] - old[key] for key in primary}
    result = {
        "holdout_rows": 2_000,
        "production_trained_at": PRODUCTION_TIME,
        "production": old,
        "staging": new,
        "staging_minus_production": deltas,
        "staging_proves_primary_improvement": True,
    }
    if model == "nova":
        result.update(
            {
                "production_interval_coverage_15_85": 0.67,
                "staging_interval_coverage_15_85": 0.69,
            }
        )
    return result


def _write_production(profile, *, model_bytes=b"old-production-model"):
    (profile.production_dir / profile.model_filename).write_bytes(model_bytes)
    for filename in profile.metadata_filenames:
        (profile.production_dir / filename).write_text(
            json.dumps(
                {
                    "artifact_version": 1,
                    "model_name": profile.name,
                    "item_kind": model_generations.MODEL_ITEM_KINDS[profile.name],
                    "trained_at": PRODUCTION_TIME,
                    "old": filename,
                }
            ),
            encoding="utf-8",
        )
    metrics = {
        "trained_at": PRODUCTION_TIME,
        "item_kind": model_generations.MODEL_ITEM_KINDS[profile.name],
        "old": "metrics",
    }
    if profile.name == "almaz":
        metrics["model_sha256"] = hashlib.sha256(model_bytes).hexdigest()
    (profile.production_dir / profile.metrics_filename).write_text(
        json.dumps(metrics),
        encoding="utf-8",
    )


def _publish_active_almaz_baseline(
    profile,
    project: Path,
    *,
    declared_model_sha: str | None,
):
    job_id = "f" * 32
    candidate = project / model_generations.STAGING_ROOTS["almaz"] / job_id
    candidate.mkdir(parents=True)
    model_path = candidate / profile.model_filename
    model_path.write_bytes(b"active-almaz-model")
    metrics = {"trained_at": PRODUCTION_TIME, "item_kind": "weapon_skin"}
    if declared_model_sha is not None:
        metrics["model_sha256"] = declared_model_sha
    (candidate / profile.metrics_filename).write_text(
        json.dumps(metrics), encoding="utf-8"
    )
    records = {}
    for filename in profile.bundle_files:
        path = candidate / filename
        records[filename] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }
    manifest = {
        "files": records,
        "bundle_sha256": model_generations._bundle_sha256(records),
        "quality_gate": {"strictly_improved_metrics": ["primary"]},
        "item_kind": "weapon_skin",
    }
    model_generations._publish_candidate(
        "almaz",
        candidate,
        manifest=manifest,
        job_id=job_id,
        project_dir=project,
        validator=lambda *_args: manifest,
        load_and_smoke=lambda *_args: None,
    )


def _write_almaz(profile):
    features = ["skin", "float_value"]
    source = {
        "logical_sha256": _sha("database"),
        "sales_rows": 10_000,
        "valid_price_rows": 10_000,
        "item_kind": "weapon_skin",
        "item_kind_policy": promotion.STRICT_ITEM_KIND_POLICY,
    }
    split = {
        "method": "chronological",
        "holdout_after": "2026-08-10T09:00:00+00:00",
        "test_rows": 2_000,
    }
    calibration = {"premium_scale": 1.0, "low_offset": -0.1, "high_offset": 0.1}
    artifact = {
        "artifact_version": 3,
        "model_name": "almaz",
        "item_kind": "weapon_skin",
        "trained_at": CANDIDATE_TIME,
        "stage": "staging",
        "production_promoted": False,
        "pipeline": FakePipeline(features, "squared_error"),
        "low_pipeline": FakePipeline(features, "quantile", 0.15),
        "high_pipeline": FakePipeline(features, "quantile", 0.85),
        "target_mode": "log_premium",
        "prediction_calibration": calibration,
        "feature_columns": features,
        "categorical_columns": ["skin"],
        "numeric_columns": ["float_value"],
        "source_database": source,
        "split_contract": split,
    }
    comparison = _comparison("almaz")
    metrics = {
        "artifact_version": 3,
        "model_name": "almaz",
        "item_kind": "weapon_skin",
        "trained_at": CANDIDATE_TIME,
        "stage": "staging",
        "production_promoted": False,
        "rows": 10_000,
        "test_rows": 2_000,
        "source_database_before": source,
        "source_database_after": source,
        "database_changed_during_training": False,
        "point_only": False,
        "split": split,
        "prediction_calibration": calibration,
        "test": comparison["staging"],
        "test_interval_coverage_15_85_log": 0.70,
        "test_raw_quantile_crossing_rate": 0.0,
        "shared_holdout_comparison": comparison,
    }
    model_path = profile.candidate_dir / profile.model_filename
    joblib.dump(artifact, model_path)
    metrics["model_sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
    (profile.candidate_dir / profile.metrics_filename).write_text(
        json.dumps(metrics), encoding="utf-8"
    )
    return artifact, metrics


def _write_isolated_bootstrap(
    profile,
    *,
    item_kind,
    train_rows=7_000,
):
    features = ["skin", "float_value"]
    source = {
        "logical_sha256": _sha(f"{item_kind}-database"),
        "sales_rows": 8_000,
        "segment_sales_rows": 8_000,
        "valid_price_rows": 8_000,
        "item_kind": item_kind,
        "item_kind_policy": promotion.STRICT_ITEM_KIND_POLICY,
    }
    split = {
        "method": "chronological",
        "holdout_after": "2026-08-10T09:00:00+00:00",
        "test_rows": 1_000,
    }
    calibration = {"premium_scale": 1.0, "low_offset": -0.1, "high_offset": 0.1}
    artifact = {
        "artifact_version": 3,
        "model_name": profile.name,
        "item_kind": item_kind,
        "trained_at": CANDIDATE_TIME,
        "stage": "staging",
        "production_promoted": False,
        "pipeline": FakePipeline(features, "squared_error"),
        "low_pipeline": FakePipeline(features, "quantile", 0.15),
        "high_pipeline": FakePipeline(features, "quantile", 0.85),
        "target_mode": "log_premium",
        "prediction_calibration": calibration,
        "feature_columns": features,
        "categorical_columns": ["skin"],
        "numeric_columns": ["float_value"],
        "source_database": source,
        "split_contract": split,
    }
    metrics = {
        "artifact_version": 3,
        "model_name": profile.name,
        "item_kind": item_kind,
        "trained_at": CANDIDATE_TIME,
        "stage": "staging",
        "production_promoted": False,
        "rows": 8_000,
        "train_rows": train_rows,
        "test_rows": 1_000,
        "source_database_before": source,
        "source_database_after": source,
        "database_changed_during_training": False,
        "point_only": False,
        "split": split,
        "prediction_calibration": calibration,
        "test": {
            "median_absolute_percentage_error": 0.10,
            "mean_absolute_percentage_error": 0.18,
            "p90_absolute_percentage_error": 0.38,
            "mae_cents": 500.0,
        },
        "test_interval_coverage_15_85_log": 0.70,
        "test_raw_quantile_crossing_rate": 0.0,
        "shared_holdout_comparison": None,
    }
    model_path = profile.candidate_dir / profile.model_filename
    joblib.dump(artifact, model_path)
    metrics["model_sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
    (profile.candidate_dir / profile.metrics_filename).write_text(
        json.dumps(metrics), encoding="utf-8"
    )
    return artifact, metrics


def _write_knives_bootstrap(profile, *, train_rows=7_000):
    return _write_isolated_bootstrap(
        profile,
        item_kind="knife",
        train_rows=train_rows,
    )


def _write_gloves_bootstrap(profile, *, train_rows=7_000):
    return _write_isolated_bootstrap(
        profile,
        item_kind="glove",
        train_rows=train_rows,
    )


def _write_nova(profile):
    cats = list(promotion.NOVA_CAT_FEATURES)
    nums = list(promotion.NOVA_NUMERIC_FEATURES)
    comparison = _comparison("nova")
    calibration = {"low": -0.1, "center": 0.0, "high": 0.1}
    source = {
        "logical_sha256": _sha("nova-database"),
        "valid_price_rows": 10_000,
        "item_kind": "weapon_skin",
        "item_kind_policy": promotion.STRICT_ITEM_KIND_POLICY,
    }
    split = {"method": "chronological", "test_rows": 2_000}
    row_exclusions = {"invalid": 5}
    cache_sha256 = _sha("nova-cache")
    model_bytes = b"fake-nova-state"
    model_sha256 = hashlib.sha256(model_bytes).hexdigest()
    metadata = {
        "artifact_version": 2,
        "model_name": "nova",
        "item_kind": "weapon_skin",
        "trained_at": CANDIDATE_TIME,
        "stage": "staging",
        "production_promoted": False,
        "feature_contract_version": promotion.NOVA_FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": promotion.NOVA_FEATURE_CONTRACT_SHA256,
        "feature_cols": cats + nums,
        "model_config": {
            "category_cardinalities": [4] * len(cats),
            "embedding_dims": [2] * len(cats),
            "numeric_count": len(nums),
            "width": 8,
            "block_count": 1,
            "dropout": 0.0,
        },
        "cat_features": cats,
        "numeric_features": nums,
        "vocabularies": {column: {"__missing__": 2} for column in cats},
        "numeric_medians": {column: 0.0 for column in nums},
        "numeric_scales": {column: 1.0 for column in nums},
        "calibration": calibration,
        "premium_scale": 1.0,
        "test_metrics": comparison["staging"],
        "test_interval_coverage_15_85": 0.69,
        "source_database": source,
        "cache_sha256": cache_sha256,
        "cache_schema_version": 4,
        "split": split,
        "row_exclusions": row_exclusions,
        "rows": 10_000,
        "fit_train_rows": 7_000,
        "model_sha256": model_sha256,
    }
    metrics = {
        "artifact_version": 2,
        "model_name": "nova",
        "item_kind": "weapon_skin",
        "trained_at": CANDIDATE_TIME,
        "stage": "staging",
        "production_promoted": False,
        "feature_contract_version": promotion.NOVA_FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": promotion.NOVA_FEATURE_CONTRACT_SHA256,
        "source_database": source,
        "cache_sha256": cache_sha256,
        "cache_schema_version": 4,
        "split": split,
        "row_exclusions": row_exclusions,
        "rows": 10_000,
        "fit_train_rows": 7_000,
        "train_rows": 7_000,
        "validation_rows": 1_000,
        "test_rows": 2_000,
        "architecture": "categorical_embeddings_residual_mlp_ordered_quantiles",
        "parameters": 123,
        "calibration": calibration,
        "premium_scale": 1.0,
        "test": comparison["staging"],
        "test_interval_coverage_15_85": 0.69,
        "test_raw_interval_coverage_15_85": 0.69,
        "test_raw_quantile_crossing_rate": 0.0,
        "test_point_outside_raw_interval_rate": 0.0,
        "shared_holdout_comparison": comparison,
        "model_sha256": model_sha256,
    }
    (profile.candidate_dir / profile.model_filename).write_bytes(model_bytes)
    (profile.candidate_dir / "nova_metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    (profile.candidate_dir / profile.metrics_filename).write_text(
        json.dumps(metrics), encoding="utf-8"
    )
    return metadata, metrics


def _fake_nova_inspection(_path, _metadata):
    return {"parameters": 123, "state_keys": ["head.weight"]}


def test_almaz_check_is_read_only_and_returns_full_sha_manifest(tmp_path, monkeypatch):
    profile = _profile(monkeypatch, tmp_path, "almaz")
    _write_production(profile)
    _write_almaz(profile)
    candidate_before = {
        path.name: path.read_bytes() for path in profile.candidate_dir.iterdir()
    }
    production_before = {
        path.name: path.read_bytes() for path in profile.production_dir.iterdir()
    }

    manifest = promotion.validate_candidate("almaz")

    assert manifest["model_name"] == "almaz"
    assert manifest["stage"] == "staging"
    assert manifest["quality_gate"]["interval_coverage"] == 0.70
    assert set(manifest["files"]) == set(profile.bundle_files)
    assert manifest["bundle_sha256"] == promotion._canonical_sha256(manifest["files"])
    assert candidate_before == {
        path.name: path.read_bytes() for path in profile.candidate_dir.iterdir()
    }
    assert production_before == {
        path.name: path.read_bytes() for path in profile.production_dir.iterdir()
    }
    assert not (profile.candidate_dir / promotion.MANIFEST_FILENAME).exists()


def test_first_knives_generation_uses_absolute_bootstrap_gate(tmp_path, monkeypatch):
    profile = _profile(monkeypatch, tmp_path, "knives")
    _write_knives_bootstrap(profile)

    manifest = promotion.validate_candidate("knives")

    assert manifest["model_name"] == "knives"
    assert manifest["quality_gate"]["bootstrap"] is True
    assert manifest["quality_gate"]["holdout_rows"] == 1_000
    assert not any(profile.production_dir.iterdir())


def test_first_knives_generation_rejects_too_little_training_data(
    tmp_path, monkeypatch
):
    profile = _profile(monkeypatch, tmp_path, "knives")
    _write_knives_bootstrap(profile, train_rows=4_999)

    with pytest.raises(
        promotion.CandidateQualityError, match="at least 5000 train rows"
    ):
        promotion.validate_candidate("knives")


def test_first_gloves_generation_uses_glove_only_bootstrap_identity(
    tmp_path,
    monkeypatch,
):
    profile = _profile(monkeypatch, tmp_path, "gloves")
    _write_gloves_bootstrap(profile)

    manifest = promotion.validate_candidate("gloves")

    assert manifest["model_name"] == "gloves"
    assert manifest["item_kind"] == "glove"
    assert manifest["quality_gate"]["bootstrap"] is True
    assert manifest["quality_gate"]["holdout_rows"] == 1_000
    assert not any(profile.production_dir.iterdir())


def test_glove_candidate_rejects_knife_artifact_identity(tmp_path, monkeypatch):
    profile = _profile(monkeypatch, tmp_path, "gloves")
    artifact, metrics = _write_gloves_bootstrap(profile)
    artifact["item_kind"] = "knife"
    model_path = profile.candidate_dir / profile.model_filename
    joblib.dump(artifact, model_path)
    metrics["model_sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
    (profile.candidate_dir / profile.metrics_filename).write_text(
        json.dumps(metrics),
        encoding="utf-8",
    )

    with pytest.raises(
        promotion.CandidateIntegrityError,
        match="artifact.item_kind must be 'glove'",
    ):
        promotion.validate_candidate("gloves")


@pytest.mark.parametrize("model", ("almaz", "nova"))
def test_weapon_candidate_requires_explicit_item_kind_in_every_document(
    model,
    tmp_path,
    monkeypatch,
):
    profile = _profile(monkeypatch, tmp_path, model)
    _write_production(profile)
    if model == "almaz":
        artifact, metrics = _write_almaz(profile)
        artifact.pop("item_kind")
        model_path = profile.candidate_dir / profile.model_filename
        joblib.dump(artifact, model_path)
        metrics["model_sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
        (profile.candidate_dir / profile.metrics_filename).write_text(
            json.dumps(metrics), encoding="utf-8"
        )
        message = "artifact.item_kind must be 'weapon_skin'"
    else:
        metadata, _metrics = _write_nova(profile)
        metadata.pop("item_kind")
        (profile.candidate_dir / "nova_metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        monkeypatch.setattr(promotion, "_inspect_nova_model", _fake_nova_inspection)
        message = "metadata.item_kind must be 'weapon_skin'"

    with pytest.raises(promotion.CandidateIntegrityError, match=message):
        promotion.validate_candidate(model)


@pytest.mark.parametrize("model", ("almaz", "nova"))
def test_weapon_candidate_requires_weapon_only_source_provenance(
    model,
    tmp_path,
    monkeypatch,
):
    profile = _profile(monkeypatch, tmp_path, model)
    _write_production(profile)
    if model == "almaz":
        artifact, metrics = _write_almaz(profile)
        artifact["source_database"]["item_kind"] = "knife"
        model_path = profile.candidate_dir / profile.model_filename
        joblib.dump(artifact, model_path)
        metrics["model_sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
        (profile.candidate_dir / profile.metrics_filename).write_text(
            json.dumps(metrics), encoding="utf-8"
        )
        message = "artifact.source_database.item_kind must be 'weapon_skin'"
    else:
        metadata, _metrics = _write_nova(profile)
        metadata["source_database"]["item_kind"] = "glove"
        (profile.candidate_dir / "nova_metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        monkeypatch.setattr(promotion, "_inspect_nova_model", _fake_nova_inspection)
        message = "metadata.source_database.item_kind must be 'weapon_skin'"

    with pytest.raises(promotion.CandidateIntegrityError, match=message):
        promotion.validate_candidate(model)


@pytest.mark.parametrize("model", ("almaz", "nova"))
def test_weapon_candidate_rejects_non_strict_source_policy(
    model,
    tmp_path,
    monkeypatch,
):
    profile = _profile(monkeypatch, tmp_path, model)
    _write_production(profile)
    if model == "almaz":
        artifact, metrics = _write_almaz(profile)
        artifact["source_database"]["item_kind_policy"] = "legacy_permissive"
        model_path = profile.candidate_dir / profile.model_filename
        joblib.dump(artifact, model_path)
        metrics["model_sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
        (profile.candidate_dir / profile.metrics_filename).write_text(
            json.dumps(metrics), encoding="utf-8"
        )
        message = "artifact.source_database.item_kind_policy must be"
    else:
        metadata, _metrics = _write_nova(profile)
        metadata["source_database"]["item_kind_policy"] = "legacy_permissive"
        (profile.candidate_dir / "nova_metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        monkeypatch.setattr(promotion, "_inspect_nova_model", _fake_nova_inspection)
        message = "metadata.source_database.item_kind_policy must be"

    with pytest.raises(promotion.CandidateIntegrityError, match=message):
        promotion.validate_candidate(model)


@pytest.mark.parametrize("model", ("almaz", "nova"))
def test_legacy_weapon_production_missing_kind_is_inferred_with_warning(
    model,
    tmp_path,
    monkeypatch,
):
    profile = _profile(monkeypatch, tmp_path, model)
    _write_production(profile)
    metrics_path = profile.production_dir / profile.metrics_filename
    production_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    production_metrics.pop("item_kind")
    metrics_path.write_text(json.dumps(production_metrics), encoding="utf-8")
    if model == "almaz":
        _write_almaz(profile)
    else:
        metadata_path = profile.production_dir / "nova_metadata.json"
        production_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        production_metadata.pop("item_kind")
        metadata_path.write_text(json.dumps(production_metadata), encoding="utf-8")
        _write_nova(profile)
        monkeypatch.setattr(promotion, "_inspect_nova_model", _fake_nova_inspection)

    with pytest.warns(RuntimeWarning, match="inferred from fixed model id"):
        manifest = promotion.validate_candidate(model)

    assert manifest["production_item_kind_compatibility"] == {
        "mode": "inferred_from_fixed_model_id",
        "item_kind": "weapon_skin",
        "fields": (
            ["production metrics.item_kind"]
            if model == "almaz"
            else [
                "production Nova metadata.item_kind",
                "production metrics.item_kind",
            ]
        ),
    }


@pytest.mark.parametrize("model", ("almaz", "nova"))
def test_legacy_weapon_production_explicit_wrong_kind_is_rejected(
    model,
    tmp_path,
    monkeypatch,
):
    profile = _profile(monkeypatch, tmp_path, model)
    _write_production(profile)
    metrics_path = profile.production_dir / profile.metrics_filename
    production_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    production_metrics["item_kind"] = "knife"
    metrics_path.write_text(json.dumps(production_metrics), encoding="utf-8")
    if model == "almaz":
        _write_almaz(profile)
    else:
        _write_nova(profile)
        monkeypatch.setattr(promotion, "_inspect_nova_model", _fake_nova_inspection)

    with pytest.raises(
        promotion.CandidateIntegrityError,
        match="production metrics.item_kind must be 'weapon_skin'",
    ):
        promotion.validate_candidate(model)


@pytest.mark.parametrize("model", ("almaz", "nova"))
def test_legacy_weapon_production_source_policy_bridge_is_fail_closed(
    model,
    tmp_path,
    monkeypatch,
):
    profile = _profile(monkeypatch, tmp_path, model)
    _write_production(profile)
    metrics_path = profile.production_dir / profile.metrics_filename
    production_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    source = {"item_kind": "weapon_skin"}
    if model == "almaz":
        production_metrics["source_database_before"] = dict(source)
        production_metrics["source_database_after"] = dict(source)
        _write_almaz(profile)
    else:
        production_metrics["source_database"] = dict(source)
        metadata_path = profile.production_dir / "nova_metadata.json"
        production_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        production_metadata["source_database"] = dict(source)
        metadata_path.write_text(json.dumps(production_metadata), encoding="utf-8")
        _write_nova(profile)
        monkeypatch.setattr(promotion, "_inspect_nova_model", _fake_nova_inspection)
    metrics_path.write_text(json.dumps(production_metrics), encoding="utf-8")

    with pytest.warns(RuntimeWarning, match="inferred from fixed model id"):
        manifest = promotion.validate_candidate(model)
    assert any(
        field.endswith("item_kind_policy")
        for field in manifest["production_item_kind_compatibility"]["fields"]
    )

    production_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    source_key = "source_database_before" if model == "almaz" else "source_database"
    production_metrics[source_key]["item_kind_policy"] = "legacy_permissive"
    metrics_path.write_text(json.dumps(production_metrics), encoding="utf-8")
    with pytest.raises(
        promotion.CandidateIntegrityError,
        match="item_kind_policy must be",
    ):
        promotion.validate_candidate(model)


def test_almaz_candidate_metrics_must_identify_model_bytes(tmp_path, monkeypatch):
    profile = _profile(monkeypatch, tmp_path, "almaz")
    _write_production(profile)
    _artifact, metrics = _write_almaz(profile)
    metrics.pop("model_sha256")
    (profile.candidate_dir / profile.metrics_filename).write_text(
        json.dumps(metrics), encoding="utf-8"
    )

    with pytest.raises(
        promotion.CandidateIntegrityError, match="metrics.model_sha256"
    ):
        promotion.validate_candidate("almaz")


def test_almaz_legacy_production_without_model_sha_is_rejected(
    tmp_path, monkeypatch
):
    profile = _profile(monkeypatch, tmp_path, "almaz")
    _write_production(profile)
    production_metrics_path = profile.production_dir / profile.metrics_filename
    production_metrics = json.loads(
        production_metrics_path.read_text(encoding="utf-8")
    )
    production_metrics.pop("model_sha256")
    production_metrics_path.write_text(
        json.dumps(production_metrics), encoding="utf-8"
    )
    _write_almaz(profile)

    with pytest.raises(
        promotion.CandidateIntegrityError,
        match="production metrics do not identify",
    ):
        promotion.validate_candidate("almaz")


def test_verified_active_almaz_generation_can_bridge_missing_metrics_sha(
    tmp_path, monkeypatch
):
    profile = _profile(monkeypatch, tmp_path, "almaz")
    _publish_active_almaz_baseline(
        profile, tmp_path, declared_model_sha=None
    )
    _write_almaz(profile)

    manifest = promotion.validate_candidate("almaz")

    assert manifest["quality_gate"]["strictly_improved_metrics"] == [
        "median_absolute_percentage_error",
        "mean_absolute_percentage_error",
        "p90_absolute_percentage_error",
    ]


def test_active_generation_does_not_override_declared_model_sha_mismatch(
    tmp_path, monkeypatch
):
    profile = _profile(monkeypatch, tmp_path, "almaz")
    _publish_active_almaz_baseline(
        profile, tmp_path, declared_model_sha="0" * 64
    )
    _write_almaz(profile)

    with pytest.raises(
        promotion.CandidateIntegrityError,
        match="production model bytes differ",
    ):
        promotion.validate_candidate("almaz")


def test_almaz_real_quality_failure_has_quality_type(tmp_path, monkeypatch):
    profile = _profile(monkeypatch, tmp_path, "almaz")
    _write_production(profile)
    _artifact, metrics = _write_almaz(profile)
    metrics["shared_holdout_comparison"][
        "staging_proves_primary_improvement"
    ] = False
    # Old trainers omitted this field. A genuinely worse candidate remains a
    # quality rejection; a quality-passing candidate still cannot publish
    # without the required SHA (covered above).
    metrics.pop("model_sha256")
    (profile.candidate_dir / profile.metrics_filename).write_text(
        json.dumps(metrics), encoding="utf-8"
    )

    with pytest.raises(
        promotion.CandidateQualityError,
        match="does not explicitly prove",
    ):
        promotion.validate_candidate("almaz")


def test_nova_check_validates_metadata_and_model_coherence(tmp_path, monkeypatch):
    profile = _profile(monkeypatch, tmp_path, "nova")
    _write_production(profile)
    _write_nova(profile)
    monkeypatch.setattr(promotion, "_inspect_nova_model", _fake_nova_inspection)

    manifest = promotion.validate_candidate("nova")

    assert manifest["model_name"] == "nova"
    assert manifest["parameter_count"] == 123
    assert len(manifest["feature_contract_sha256"]) == 64
    assert manifest["quality_gate"]["strictly_improved_metrics"] == [
        "median_ape_percent",
        "mean_ape_percent",
        "p90_ape_percent",
    ]


@pytest.mark.parametrize("model", ["almaz", "nova"])
def test_check_fails_closed_without_shared_holdout(model, tmp_path, monkeypatch):
    profile = _profile(monkeypatch, tmp_path, model)
    _write_production(profile)
    if model == "almaz":
        _artifact, metrics = _write_almaz(profile)
    else:
        _metadata, metrics = _write_nova(profile)
        monkeypatch.setattr(promotion, "_inspect_nova_model", _fake_nova_inspection)
    metrics.pop("shared_holdout_comparison")
    (profile.candidate_dir / profile.metrics_filename).write_text(
        json.dumps(metrics), encoding="utf-8"
    )

    with pytest.raises(promotion.PromotionError, match="no shared_holdout_comparison"):
        promotion.validate_candidate(model)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("not_strict", "not strictly better"),
        ("wrong_delta", "delta.*inconsistent"),
        ("low_interval", "must be at least"),
        ("old_baseline", "currently installed"),
    ],
)
def test_almaz_quality_gate_rejects_unproven_candidate(
    mutation, message, tmp_path, monkeypatch
):
    profile = _profile(monkeypatch, tmp_path, "almaz")
    _write_production(profile)
    _artifact, metrics = _write_almaz(profile)
    comparison = metrics["shared_holdout_comparison"]
    key = "median_absolute_percentage_error"
    if mutation == "not_strict":
        comparison["staging"][key] = comparison["production"][key]
        metrics["test"][key] = comparison["production"][key]
        comparison["staging_minus_production"][key] = 0.0
    elif mutation == "wrong_delta":
        comparison["staging_minus_production"][key] = -999.0
    elif mutation == "low_interval":
        metrics["test_interval_coverage_15_85_log"] = 0.59
    else:
        comparison["production_trained_at"] = "2026-08-09T10:00:00+00:00"
    (profile.candidate_dir / profile.metrics_filename).write_text(
        json.dumps(metrics), encoding="utf-8"
    )

    with pytest.raises(promotion.PromotionError, match=message):
        promotion.validate_candidate("almaz")


def test_check_rejects_declared_sha_mismatch_and_symlinks(tmp_path, monkeypatch):
    profile = _profile(monkeypatch, tmp_path, "nova")
    _write_production(profile)
    _metadata, metrics = _write_nova(profile)
    monkeypatch.setattr(promotion, "_inspect_nova_model", _fake_nova_inspection)
    metrics["model_sha256"] = "0" * 64
    (profile.candidate_dir / profile.metrics_filename).write_text(
        json.dumps(metrics), encoding="utf-8"
    )
    with pytest.raises(promotion.PromotionError, match="SHA-256 differs"):
        promotion.validate_candidate("nova")

    metrics.pop("model_sha256")
    (profile.candidate_dir / profile.metrics_filename).write_text(
        json.dumps(metrics), encoding="utf-8"
    )
    model_path = profile.candidate_dir / profile.model_filename
    real_path = profile.candidate_dir / "real-state.pt"
    model_path.replace(real_path)
    model_path.symlink_to(real_path.name)
    with pytest.raises(promotion.PromotionError, match="symlink"):
        promotion.validate_candidate("nova")


def test_check_detects_candidate_mutation_during_model_inspection(
    tmp_path, monkeypatch
):
    profile = _profile(monkeypatch, tmp_path, "nova")
    _write_production(profile)
    _write_nova(profile)

    def mutating_inspector(path, _metadata):
        path.write_bytes(b"changed-during-check")
        return _fake_nova_inspection(path, _metadata)

    monkeypatch.setattr(promotion, "_inspect_nova_model", mutating_inspector)
    with pytest.raises(promotion.PromotionError, match="changed while"):
        promotion.validate_candidate("nova")


def test_promote_requires_confirmation_and_refuses_live_services(tmp_path, monkeypatch):
    profile = _profile(monkeypatch, tmp_path, "almaz")
    _write_production(profile)
    _write_almaz(profile)

    with pytest.raises(promotion.PromotionError, match="offline-confirm"):
        promotion.promote_candidate("almaz")

    monkeypatch.setattr(
        promotion,
        "find_live_service_processes",
        lambda: [
            {
                "pid": 55,
                "service": "dashboard_server",
                "command": "python dashboard_server.py",
            }
        ],
    )
    with pytest.raises(promotion.PromotionError, match="while.*live"):
        promotion.promote_candidate("almaz", offline_confirm=True)
    assert (
        profile.production_dir / profile.model_filename
    ).read_bytes() == b"old-production-model"
    assert not profile.backup_root.exists()


@pytest.mark.parametrize("model", ["almaz", "nova"])
def test_promote_backs_up_full_bundle_and_activates_metadata_last(
    model, tmp_path, monkeypatch
):
    profile = _profile(monkeypatch, tmp_path, model)
    _write_production(profile)
    old_files = {
        filename: (profile.production_dir / filename).read_bytes()
        for filename in profile.bundle_files
    }
    if model == "almaz":
        _write_almaz(profile)
    else:
        _write_nova(profile)
        monkeypatch.setattr(promotion, "_inspect_nova_model", _fake_nova_inspection)
    candidate_before = {
        filename: (profile.candidate_dir / filename).read_bytes()
        for filename in profile.bundle_files
    }
    monkeypatch.setattr(promotion, "find_live_service_processes", list)
    real_replace = promotion.os.replace
    activation_order = []

    def recording_replace(source, destination):
        destination = Path(destination)
        if (
            destination.parent == profile.production_dir
            and destination.name in profile.activation_order
        ):
            activation_order.append(destination.name)
        return real_replace(source, destination)

    monkeypatch.setattr(promotion.os, "replace", recording_replace)
    result = promotion.promote_candidate(model, offline_confirm=True)

    backup = Path(result["backup_directory"])
    assert backup.name.startswith(f"{model}_")
    assert (backup / promotion.BACKUP_MANIFEST_FILENAME).is_file()
    for filename, old_content in old_files.items():
        assert (backup / filename).read_bytes() == old_content
    assert activation_order == list(profile.activation_order)
    assert candidate_before == {
        filename: (profile.candidate_dir / filename).read_bytes()
        for filename in profile.bundle_files
    }
    metrics = json.loads(
        (profile.production_dir / profile.metrics_filename).read_text(encoding="utf-8")
    )
    assert metrics["stage"] == "production"
    assert metrics["production_promoted"] is True
    assert metrics["promotion"]["backup_directory"] == str(backup)
    assert len(metrics["model_sha256"]) == 64
    manifest = json.loads(
        (profile.production_dir / promotion.MANIFEST_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["bundle_sha256"] == result["production_bundle_sha256"]
    if model == "almaz":
        artifact = joblib.load(profile.production_dir / profile.model_filename)
        assert artifact["stage"] == "production"
        assert artifact["production_promoted"] is True
    else:
        metadata = json.loads(
            (profile.production_dir / "nova_metadata.json").read_text(encoding="utf-8")
        )
        assert metadata["stage"] == "production"
        assert metadata["production_promoted"] is True


def test_partial_activation_failure_restores_exact_old_production(
    tmp_path, monkeypatch
):
    profile = _profile(monkeypatch, tmp_path, "almaz")
    _write_production(profile)
    _write_almaz(profile)
    old_files = {
        filename: (profile.production_dir / filename).read_bytes()
        for filename in profile.bundle_files
    }
    monkeypatch.setattr(promotion, "find_live_service_processes", list)
    real_replace = promotion.os.replace
    failed = False

    def fail_metrics_activation(source, destination):
        nonlocal failed
        source = Path(source)
        destination = Path(destination)
        if (
            not failed
            and source.parent.name.startswith(".almaz-next-")
            and destination == profile.production_dir / profile.metrics_filename
        ):
            failed = True
            raise OSError("simulated activation failure")
        return real_replace(source, destination)

    monkeypatch.setattr(promotion.os, "replace", fail_metrics_activation)
    with pytest.raises(promotion.PromotionError, match="rolled back"):
        promotion.promote_candidate("almaz", offline_confirm=True)

    for filename, old_content in old_files.items():
        assert (profile.production_dir / filename).read_bytes() == old_content
    assert not (profile.production_dir / promotion.MANIFEST_FILENAME).exists()
    assert not list(profile.production_dir.glob(".almaz-next-*"))


def test_interrupted_file_commit_is_durably_recovered(tmp_path, monkeypatch):
    profile = _profile(monkeypatch, tmp_path, "almaz")
    _write_production(profile)
    _write_almaz(profile)
    monkeypatch.setattr(promotion, "find_live_service_processes", list)
    old_files = {
        filename: (profile.production_dir / filename).read_bytes()
        for filename in profile.bundle_files
    }
    validated = promotion._validate_candidate_internal("almaz", None)
    backup, _manifest = promotion._create_backup(validated)
    next_dir, _production_manifest = promotion._prepare_next_bundle(
        validated, backup
    )
    record = {
        "kind": "generic_files_v1",
        "model": "almaz",
        "production_directory": str(profile.production_dir),
        "backup_directory": str(backup),
        "candidate_bundle_sha256": validated.manifest["bundle_sha256"],
        "backup_manifest_sha256": promotion._sha256_file(
            backup / promotion.BACKUP_MANIFEST_FILENAME
        ),
        "created_at": promotion._utc_now(),
    }
    promotion.model_lifecycle.write_promotion_journal(
        record, project_dir=tmp_path
    )
    promotion.os.replace(
        next_dir / profile.model_filename,
        profile.production_dir / profile.model_filename,
    )

    recovered = promotion.recover_interrupted_promotion()

    assert recovered["status"] == "recovered"
    for filename, content in old_files.items():
        assert (profile.production_dir / filename).read_bytes() == content
    assert promotion.model_lifecycle.read_promotion_journal(
        project_dir=tmp_path
    ) is None


def test_clear_failure_after_unlink_recreates_journal_and_rolls_back(
    tmp_path, monkeypatch
):
    profile = _profile(monkeypatch, tmp_path, "almaz")
    _write_production(profile)
    _write_almaz(profile)
    old_files = {
        filename: (profile.production_dir / filename).read_bytes()
        for filename in profile.bundle_files
    }
    monkeypatch.setattr(promotion, "find_live_service_processes", list)
    real_clear = promotion.model_lifecycle.clear_promotion_journal
    failed = False

    def fail_after_unlink(**kwargs):
        nonlocal failed
        if not failed:
            failed = True
            journal = (
                tmp_path
                / ".run"
                / "model_lifecycle"
                / promotion.model_lifecycle.JOURNAL_FILENAME
            )
            journal.unlink()
            raise promotion.model_lifecycle.ModelLifecycleError(
                "simulated journal directory fsync failure"
            )
        return real_clear(**kwargs)

    monkeypatch.setattr(
        promotion.model_lifecycle, "clear_promotion_journal", fail_after_unlink
    )
    with pytest.raises(promotion.PromotionError, match="fsync failure"):
        promotion.promote_candidate("almaz", offline_confirm=True)

    for filename, content in old_files.items():
        assert (profile.production_dir / filename).read_bytes() == content
    assert promotion.model_lifecycle.read_promotion_journal(
        project_dir=tmp_path
    ) is None


def test_promotion_api_has_no_caller_selected_lock_path():
    assert "lock_path" not in inspect.signature(
        promotion.promote_candidate
    ).parameters


def test_process_scan_reports_only_target_python_services(monkeypatch):
    output = (
        " 101 /project/.venv/bin/python dashboard_server.py\n"
        " 102 python3 -m deal_scanner\n"
        " 103 node dashboard_server.py\n"
        " 104 python parser_final.py\n"
        " 105 python harmless.py --text=deal_scanner.py\n"
    )
    monkeypatch.setattr(
        promotion.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=output),
    )

    live = promotion.find_live_service_processes()

    assert [(item["pid"], item["service"]) for item in live] == [
        (101, "dashboard_server"),
        (102, "deal_scanner"),
    ]


def test_cli_requires_operation_and_model_and_has_no_production_override():
    args = promotion.parse_args(
        ["--check", "--model", "nova", "--candidate-dir", "/tmp/candidate"]
    )
    assert args.check is True
    assert args.model == "nova"
    assert args.candidate_dir == Path("/tmp/candidate")
    assert not hasattr(args, "production_dir")
