from pathlib import Path

import joblib
import pytest

import knife_model_runtime


def test_loader_requires_dedicated_identity(tmp_path: Path):
    joblib.dump(
        {"model_name": "almaz", "item_kind": "weapon_skin"},
        tmp_path / knife_model_runtime.MODEL_PATH.name,
    )
    with pytest.raises(knife_model_runtime.KnifeModelUnavailable):
        knife_model_runtime.load_bundle_from_directory(tmp_path)


def test_predict_rejects_non_knife_before_loading_model(monkeypatch):
    monkeypatch.setattr(
        knife_model_runtime,
        "load_model",
        lambda: pytest.fail("weapon skin must not load the knife artifact"),
    )
    with pytest.raises(ValueError, match="knife items only"):
        knife_model_runtime.predict_deal(
            {"name": "AK-47 | Redline (Field-Tested)"}
        )


def test_missing_artifact_has_explicit_unavailable_error(tmp_path: Path):
    with pytest.raises(
        knife_model_runtime.KnifeModelUnavailable,
        match="artifact is unavailable",
    ):
        knife_model_runtime.load_bundle_from_directory(tmp_path)


def test_predict_uses_only_the_dedicated_knife_artifact(monkeypatch):
    artifact = {"model_name": "knives", "item_kind": "knife"}
    observed = []
    monkeypatch.setattr(knife_model_runtime, "load_model", lambda: artifact)
    monkeypatch.setattr(
        knife_model_runtime.price_model_runtime,
        "predict_deal_with_artifact",
        lambda payload, selected, conn=None: (
            observed.append((payload, selected, conn))
            or {"model_name": "unexpected", "item_kind": "unexpected"}
        ),
    )
    payload = {"name": "★ Karambit | Fade (Factory New)"}

    result = knife_model_runtime.predict_deal(payload)

    assert observed == [(payload, artifact, None)]
    assert result["model_name"] == "knives"
    assert result["item_kind"] == "knife"
