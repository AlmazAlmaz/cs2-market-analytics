import hashlib
import inspect
import json
import pickle
from pathlib import Path
from types import SimpleNamespace

import pytest

import model_generations
from IGORMODEL import features
from IGORMODEL import promote_candidate as promotion

PRODUCTION_TRAINED_AT = "2026-08-09T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _production_quality_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr(promotion, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(
        promotion,
        "_read_production_metrics",
        lambda _path: {
            "artifact_version": 2,
            "model_name": "igor",
            "item_kind": "weapon_skin",
            "stage": "production",
            "production_promoted": True,
            "trained_at": PRODUCTION_TRAINED_AT,
            "source_database": {
                "item_kind": "weapon_skin",
                "item_kind_policy": "strict_catalog_or_known_name_v1",
            },
        },
    )


class FakeCatBoostModel:
    def __init__(self, feature_names, cat_indices, tree_count, loss):
        self.feature_names_ = list(feature_names)
        self.tree_count_ = tree_count
        self._cat_indices = list(cat_indices)
        self._loss = loss

    def get_cat_feature_indices(self):
        return list(self._cat_indices)

    def get_all_params(self):
        return {"loss_function": self._loss}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bundle_documents():
    source = {
        "logical_sha256": _sha("source"),
        "content_sha256": _sha("content"),
        "valid_price_rows": 3_000,
        "item_kind": "weapon_skin",
        "item_kind_policy": "strict_catalog_or_known_name_v1",
    }
    split = {
        "method": "fixed_shared_truly_new_boundary",
        "holdout_after": "2026-08-09T22:34:09.594496+00:00",
        "train_rows": 1_500,
        "validation_rows": 500,
        "test_rows": 1_000,
        "test_untouched_until_final_evaluation": True,
    }
    shared = {
        "artifact_version": 2,
        "model_name": "igor",
        "item_kind": "weapon_skin",
        "trained_at": "2026-08-10T12:00:00+00:00",
        "stage": "staging",
        "feature_contract_sha256": features.FEATURE_CONTRACT_SHA256,
        "source_database": source,
        "cache_sha256": _sha("cache"),
        "rows": 3_000,
        "fit_train_rows": 1_500,
        "row_exclusions": {"invalid": 7},
        "split": split,
        "production_promoted": False,
    }
    metadata = {
        **shared,
        "feature_contract_version": 2,
        "feature_contract": features.feature_contract_definition(),
        "feature_cols": list(features.ALL_FEATURES),
        "cat_features": list(features.CAT_FEATURES),
        "model_iterations": {"point": 11, "q15": 12, "q85": 13},
    }
    production = {
        "median_ape_%": 10.0,
        "mean_ape_%": 13.0,
        "p90_ape_%": 30.0,
        "mae_cents": 120.0,
    }
    staging = {
        "median_ape_%": 8.0,
        "mean_ape_%": 11.0,
        "p90_ape_%": 27.0,
        "mae_cents": 100.0,
    }
    comparison = {
        "holdout_rows": 1_000,
        "production_trained_at": PRODUCTION_TRAINED_AT,
        "production": production,
        "staging": staging,
        "staging_minus_production": {
            key: staging[key] - production[key] for key in production
        },
        "production_interval_coverage_15_85": 0.64,
        "staging_interval_coverage_15_85": 0.68,
        "candidate_row_win_percent": 62.0,
        "staging_proves_primary_improvement": True,
    }
    metrics = {
        **shared,
        "test_rows": 1_000,
        "test": staging,
        "test_raw_interval_coverage_15_85": 0.68,
        "test_raw_quantile_crossing_rate": 0.0,
        "test_point_outside_raw_interval_rate": 0.002,
        "shared_holdout_comparison": comparison,
    }
    return metadata, metrics


def _write_bundle(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    metadata, metrics = _bundle_documents()
    for filename, content in (
        ("price_model_point.cbm", b"fake-point-model"),
        ("price_model_q15.cbm", b"fake-low-model"),
        ("price_model_q85.cbm", b"fake-high-model"),
    ):
        (path / filename).write_bytes(content)
    with (path / "feature_metadata.pkl").open("wb") as handle:
        pickle.dump(metadata, handle)
    (path / "igor_metrics.json").write_text(
        json.dumps(metrics), encoding="utf-8"
    )
    return metadata, metrics


def _model_loader(*, overrides=None):
    metadata, _metrics = _bundle_documents()
    feature_names = metadata["feature_cols"]
    cat_set = set(metadata["cat_features"])
    cat_indices = [
        index for index, name in enumerate(feature_names) if name in cat_set
    ]
    specs = {
        "price_model_point.cbm": (11, "MAE"),
        "price_model_q15.cbm": (12, "Quantile:alpha=0.15"),
        "price_model_q85.cbm": (13, "Quantile:alpha=0.85"),
    }
    overrides = overrides or {}

    def load(path):
        tree_count, loss = specs[Path(path).name]
        values = {
            "feature_names": feature_names,
            "cat_indices": cat_indices,
            "tree_count": tree_count,
            "loss": loss,
        }
        values.update(overrides.get(Path(path).name, {}))
        return FakeCatBoostModel(**values)

    return load


def test_check_is_read_only_and_returns_sha256_manifest(tmp_path):
    candidate = tmp_path / "candidate"
    _write_bundle(candidate)
    names_before = sorted(path.name for path in candidate.iterdir())

    manifest = promotion.validate_candidate(
        candidate, model_loader=_model_loader()
    )

    assert manifest["stage"] == "staging"
    assert manifest["bundle_sha256"] == promotion._canonical_sha256(
        manifest["files"]
    )
    assert set(manifest["files"]) == set(promotion.BUNDLE_FILES)
    assert all(
        len(file_record["sha256"]) == 64
        for file_record in manifest["files"].values()
    )
    assert sorted(path.name for path in candidate.iterdir()) == names_before
    assert not (candidate / "artifact_manifest.json").exists()


def test_second_candidate_uses_verified_hot_staging_generation_as_baseline(tmp_path):
    first_job_id = "a" * 32
    first = (
        tmp_path
        / model_generations.STAGING_ROOTS["igor"]
        / first_job_id
    )
    _write_bundle(first)
    first_manifest = promotion.validate_candidate(
        first, model_loader=_model_loader()
    )
    model_generations._publish_candidate(
        "igor",
        first,
        manifest=first_manifest,
        job_id=first_job_id,
        project_dir=tmp_path,
        validator=lambda *_args: first_manifest,
        load_and_smoke=lambda *_args: None,
    )

    second_job_id = "b" * 32
    second = (
        tmp_path
        / model_generations.STAGING_ROOTS["igor"]
        / second_job_id
    )
    metadata, metrics = _write_bundle(second)
    second_trained_at = "2026-08-11T12:00:00+00:00"
    for document in (metadata, metrics):
        document["trained_at"] = second_trained_at
    metrics["shared_holdout_comparison"]["production_trained_at"] = (
        "2026-08-10T12:00:00+00:00"
    )
    with (second / "feature_metadata.pkl").open("wb") as handle:
        pickle.dump(metadata, handle)
    (second / "igor_metrics.json").write_text(
        json.dumps(metrics), encoding="utf-8"
    )

    second_manifest = promotion.validate_candidate(
        second, model_loader=_model_loader()
    )

    assert second_manifest["quality_gate"]["production_trained_at"] == (
        "2026-08-10T12:00:00+00:00"
    )


def test_check_rejects_training_log_only_and_partial_bundle(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "training.log").write_text("still training", encoding="utf-8")

    with pytest.raises(promotion.PromotionError, match="incomplete"):
        promotion.validate_candidate(candidate, model_loader=_model_loader())

    _write_bundle(candidate)
    (candidate / "price_model_q85.cbm").unlink()
    with pytest.raises(promotion.PromotionError, match="incomplete"):
        promotion.validate_candidate(candidate, model_loader=_model_loader())


@pytest.mark.parametrize("field", ["trained_at", "cache_sha256", "source_database", "split"])
def test_check_rejects_mixed_metadata_and_metrics(tmp_path, field):
    candidate = tmp_path / "candidate"
    _metadata, metrics = _write_bundle(candidate)
    metrics[field] = "mixed" if field not in {"source_database", "split"} else {"mixed": True}
    (candidate / "igor_metrics.json").write_text(
        json.dumps(metrics), encoding="utf-8"
    )

    with pytest.raises(promotion.PromotionError, match="mixed candidate bundle"):
        promotion.validate_candidate(candidate, model_loader=_model_loader())


def test_check_rejects_non_weapon_candidate_provenance(tmp_path):
    candidate = tmp_path / "candidate"
    metadata, metrics = _write_bundle(candidate)
    metadata["item_kind"] = "glove"
    metrics["item_kind"] = "glove"
    metadata["source_database"]["item_kind"] = "glove"
    metrics["source_database"]["item_kind"] = "glove"
    with (candidate / "feature_metadata.pkl").open("wb") as handle:
        pickle.dump(metadata, handle)
    (candidate / "igor_metrics.json").write_text(
        json.dumps(metrics),
        encoding="utf-8",
    )

    with pytest.raises(promotion.PromotionError, match="item_kind"):
        promotion.validate_candidate(candidate, model_loader=_model_loader())


def test_check_does_not_apply_legacy_bridge_to_new_candidate(tmp_path):
    candidate = tmp_path / "candidate"
    metadata, metrics = _write_bundle(candidate)
    metadata["source_database"].pop("item_kind_policy")
    with (candidate / "feature_metadata.pkl").open("wb") as handle:
        pickle.dump(metadata, handle)
    (candidate / "igor_metrics.json").write_text(
        json.dumps(metrics),
        encoding="utf-8",
    )

    with pytest.raises(promotion.PromotionError, match="not strictly item-kind isolated"):
        promotion.validate_candidate(candidate, model_loader=_model_loader())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"price_model_q15.cbm": {"feature_names": ["wrong"]}},
            "feature names",
        ),
        (
            {"price_model_point.cbm": {"tree_count": 99}},
            "tree count",
        ),
        (
            {"price_model_q85.cbm": {"loss": "Quantile:alpha=0.15"}},
            "loss",
        ),
    ],
)
def test_check_rejects_mismatched_catboost_models(tmp_path, overrides, message):
    candidate = tmp_path / "candidate"
    _write_bundle(candidate)

    with pytest.raises(promotion.PromotionError, match=message):
        promotion.validate_candidate(
            candidate, model_loader=_model_loader(overrides=overrides)
        )


def test_check_rejects_candidate_that_changes_during_validation(tmp_path):
    candidate = tmp_path / "candidate"
    _write_bundle(candidate)
    ordinary_loader = _model_loader()
    changed = False

    def mutating_loader(path):
        nonlocal changed
        if not changed:
            changed = True
            (candidate / "price_model_q85.cbm").write_bytes(b"changed-model")
        return ordinary_loader(path)

    with pytest.raises(promotion.PromotionError, match="changed while"):
        promotion.validate_candidate(candidate, model_loader=mutating_loader)


def test_check_rejects_candidate_without_strict_shared_holdout_improvement(tmp_path):
    candidate = tmp_path / "candidate"
    _metadata, metrics = _write_bundle(candidate)
    comparison = metrics["shared_holdout_comparison"]
    comparison["staging"]["median_ape_%"] = comparison["production"][
        "median_ape_%"
    ]
    comparison["staging_minus_production"]["median_ape_%"] = 0.0
    metrics["test"]["median_ape_%"] = comparison["staging"]["median_ape_%"]
    (candidate / "igor_metrics.json").write_text(
        json.dumps(metrics), encoding="utf-8"
    )

    with pytest.raises(promotion.PromotionError, match="not strictly better"):
        promotion.validate_candidate(candidate, model_loader=_model_loader())


def test_check_binds_comparison_to_current_production(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    _write_bundle(candidate)
    monkeypatch.setattr(
        promotion,
        "_read_production_metrics",
        lambda _path: {
            "item_kind": "weapon_skin",
            "trained_at": "2026-08-09T13:00:00+00:00",
            "source_database": {
                "item_kind": "weapon_skin",
                "item_kind_policy": "strict_catalog_or_known_name_v1",
            },
        },
    )

    with pytest.raises(promotion.PromotionError, match="currently installed"):
        promotion.validate_candidate(candidate, model_loader=_model_loader())


def test_check_accepts_missing_legacy_production_provenance_with_receipt(
    tmp_path,
    monkeypatch,
):
    candidate = tmp_path / "candidate"
    _write_bundle(candidate)
    monkeypatch.setattr(
        promotion,
        "_read_production_metrics",
        lambda _path: {
            "artifact_version": 2,
            "model_name": "igor",
            "stage": "production",
            "production_promoted": True,
            "trained_at": PRODUCTION_TRAINED_AT,
            "source_database": {},
        },
    )

    with pytest.warns(RuntimeWarning, match="inferred from fixed model id"):
        manifest = promotion.validate_candidate(
            candidate,
            model_loader=_model_loader(),
        )

    assert manifest["production_item_kind_compatibility"] == {
        "mode": "inferred_from_fixed_model_id",
        "item_kind": "weapon_skin",
        "fields": [
            "production metrics.item_kind",
            "production metrics.source_database.item_kind",
            "production metrics.source_database.item_kind_policy",
        ],
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("item_kind", "glove", r"production metrics\.item_kind must be"),
        (
            "source_item_kind",
            "knife",
            r"production metrics\.source_database\.item_kind must be",
        ),
        (
            "item_kind_policy",
            "legacy_permissive",
            r"production metrics\.source_database\.item_kind_policy must be",
        ),
    ],
)
def test_check_rejects_explicit_production_provenance_conflict(
    tmp_path,
    monkeypatch,
    field,
    value,
    message,
):
    candidate = tmp_path / "candidate"
    _write_bundle(candidate)
    production_metrics = {
        "artifact_version": 2,
        "model_name": "igor",
        "item_kind": "weapon_skin",
        "stage": "production",
        "production_promoted": True,
        "trained_at": PRODUCTION_TRAINED_AT,
        "source_database": {
            "item_kind": "weapon_skin",
            "item_kind_policy": "strict_catalog_or_known_name_v1",
        },
    }
    if field == "source_item_kind":
        production_metrics["source_database"]["item_kind"] = value
    elif field == "item_kind_policy":
        production_metrics["source_database"]["item_kind_policy"] = value
    else:
        production_metrics[field] = value
    monkeypatch.setattr(
        promotion,
        "_read_production_metrics",
        lambda _path: production_metrics,
    )

    with pytest.raises(promotion.PromotionError, match=message):
        promotion.validate_candidate(candidate, model_loader=_model_loader())


def test_promote_requires_offline_confirmation_and_refuses_live_services(
    tmp_path, monkeypatch
):
    candidate = tmp_path / "candidate"
    production = tmp_path / "production"
    backups = tmp_path / "backups"
    _write_bundle(candidate)
    production.mkdir()
    (production / "old-model").write_bytes(b"old")

    with pytest.raises(promotion.PromotionError, match="offline-confirm"):
        promotion.promote_candidate(
            candidate,
            production,
            backups,
            model_loader=_model_loader(),
        )

    monkeypatch.setattr(
        promotion,
        "find_live_service_processes",
        lambda: [{"pid": 123, "command": "python dashboard_server.py"}],
    )
    with pytest.raises(promotion.PromotionError, match="while.*live"):
        promotion.promote_candidate(
            candidate,
            production,
            backups,
            offline_confirm=True,
            model_loader=_model_loader(),
        )
    assert (production / "old-model").read_bytes() == b"old"
    assert not backups.exists()


def test_process_scan_only_reports_target_python_services(monkeypatch):
    ps_output = (
        " 101 /project/.venv/bin/python dashboard_server.py\n"
        " 102 python3 -m deal_scanner\n"
        " 103 node dashboard_server.py\n"
        " 104 python parser_final.py\n"
        " 105 python harmless.py --text=deal_scanner.py"
    )
    monkeypatch.setattr(
        promotion.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=ps_output),
    )

    live = promotion.find_live_service_processes()

    assert [(item["pid"], item["command"].split()[-1]) for item in live] == [
        (101, "dashboard_server.py"),
        (102, "deal_scanner"),
    ]


def test_promote_keeps_full_backup_and_installs_only_coherent_bundle(
    tmp_path, monkeypatch
):
    candidate = tmp_path / "candidate"
    production = tmp_path / "production"
    backups = tmp_path / "backups"
    original_metadata, original_metrics = _write_bundle(candidate)
    production.mkdir()
    (production / "legacy-extra.pkl").write_bytes(b"recover-me")
    (production / "price_model_point.cbm").write_bytes(b"old-point")
    monkeypatch.setattr(promotion, "find_live_service_processes", list)
    monkeypatch.setattr(
        promotion,
        "_read_production_metrics",
        lambda _path: {
            "artifact_version": 2,
            "model_name": "igor",
            "stage": "production",
            "production_promoted": True,
            "trained_at": PRODUCTION_TRAINED_AT,
            "source_database": {},
        },
    )

    with pytest.warns(RuntimeWarning, match="inferred from fixed model id"):
        result = promotion.promote_candidate(
            candidate,
            production,
            backups,
            offline_confirm=True,
            model_loader=_model_loader(),
        )

    backup = Path(result["backup_directory"])
    assert backup.parent == backups
    assert backup.name.startswith("igor_")
    assert (backup / "legacy-extra.pkl").read_bytes() == b"recover-me"
    assert (backup / "price_model_point.cbm").read_bytes() == b"old-point"
    assert {path.name for path in production.iterdir()} == {
        *promotion.BUNDLE_FILES,
        "artifact_manifest.json",
    }
    assert not (production / "legacy-extra.pkl").exists()
    with (production / "feature_metadata.pkl").open("rb") as handle:
        promoted_metadata = pickle.load(handle)
    promoted_metrics = json.loads(
        (production / "igor_metrics.json").read_text(encoding="utf-8")
    )
    assert promoted_metadata["stage"] == "production"
    assert promoted_metadata["production_promoted"] is True
    assert promoted_metrics["stage"] == "production"
    assert promoted_metrics["production_promoted"] is True
    assert promoted_metadata["trained_at"] == original_metadata["trained_at"]
    assert promoted_metrics["cache_sha256"] == original_metrics["cache_sha256"]
    manifest = json.loads(
        (production / "artifact_manifest.json").read_text(encoding="utf-8")
    )
    compatibility_receipt = {
        "mode": "inferred_from_fixed_model_id",
        "item_kind": "weapon_skin",
        "fields": [
            "production metrics.item_kind",
            "production metrics.source_database.item_kind",
            "production metrics.source_database.item_kind_policy",
        ],
    }
    assert result["production_item_kind_compatibility"] == compatibility_receipt
    assert manifest["production_item_kind_compatibility"] == compatibility_receipt
    assert manifest["stage"] == "production"
    assert manifest["promotion"]["backup_directory"] == str(backup)
    assert (candidate / "feature_metadata.pkl").read_bytes() != (
        production / "feature_metadata.pkl"
    ).read_bytes()
    candidate_metrics = json.loads(
        (candidate / "igor_metrics.json").read_text(encoding="utf-8")
    )
    assert candidate_metrics["stage"] == "staging"
    assert candidate_metrics["production_promoted"] is False
    assert promotion.installed_candidate_bundle_sha256(
        production, model_loader=_model_loader()
    ) == result["candidate_bundle_sha256"]

    manifest["promotion"]["candidate_bundle_sha256"] = "0" * 64
    (production / "artifact_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    with pytest.raises(promotion.PromotionError, match="receipts are incoherent"):
        promotion.installed_candidate_bundle_sha256(
            production, model_loader=_model_loader()
        )


def test_activation_failure_restores_old_production(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    production = tmp_path / "production"
    backups = tmp_path / "backups"
    _write_bundle(candidate)
    production.mkdir()
    (production / "old-marker").write_bytes(b"old-production")
    monkeypatch.setattr(promotion, "find_live_service_processes", list)
    real_replace = promotion.os.replace

    def fail_new_directory_activation(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.startswith(".igor-next-") and destination_path == production:
            raise OSError("simulated directory activation failure")
        return real_replace(source, destination)

    monkeypatch.setattr(promotion.os, "replace", fail_new_directory_activation)
    with pytest.raises(promotion.PromotionError, match="rolled back"):
        promotion.promote_candidate(
            candidate,
            production,
            backups,
            offline_confirm=True,
            model_loader=_model_loader(),
        )

    assert (production / "old-marker").read_bytes() == b"old-production"
    assert not list(production.parent.glob(".igor-next-*"))
    assert not list(backups.glob("igor_*"))


def test_igor_promotion_api_has_no_caller_selected_lock_path():
    assert "lock_path" not in inspect.signature(
        promotion.promote_candidate
    ).parameters


def test_interrupted_igor_directory_swap_restores_original_inode(tmp_path):
    production = tmp_path / "production"
    backups = tmp_path / "backups"
    backup = backups / "igor_20260811T100000.000000Z"
    next_dir = tmp_path / ".igor-next-crashed"
    recovery = tmp_path / ".igor-recovery-test"
    production.mkdir()
    backups.mkdir()
    next_dir.mkdir()
    (production / "old-marker").write_bytes(b"old-production")
    (next_dir / "new-marker").write_bytes(b"new-production")
    old_identity = promotion._directory_identity(production, "production")
    record = {
        "kind": "igor_directory_v1",
        "model": "igor",
        "production_directory": str(production),
        "backup_root": str(backups),
        "backup_directory": str(backup),
        "next_directory": str(next_dir),
        "recovery_directory": str(recovery),
        "old_directory_identity": old_identity,
        "candidate_bundle_sha256": "a" * 64,
        "created_at": "2026-08-11T10:00:00+00:00",
    }
    promotion.model_lifecycle.write_promotion_journal(
        record, project_dir=tmp_path
    )
    promotion.os.replace(production, backup)

    result = promotion.recover_interrupted_promotion()

    assert result["status"] == "recovered"
    assert (production / "old-marker").read_bytes() == b"old-production"
    assert promotion._directory_identity(production, "production") == old_identity
    assert not backup.exists()
    assert not next_dir.exists()
    assert promotion.model_lifecycle.read_promotion_journal(
        project_dir=tmp_path
    ) is None
