from pathlib import Path

import joblib
import pytest

import glove_model_runtime


def test_loader_requires_dedicated_identity(tmp_path: Path):
    joblib.dump(
        {"model_name": "knives", "item_kind": "knife"},
        tmp_path / glove_model_runtime.MODEL_PATH.name,
    )

    with pytest.raises(glove_model_runtime.GloveModelUnavailable):
        glove_model_runtime.load_bundle_from_directory(tmp_path)


def test_predict_rejects_non_glove_before_loading_model(monkeypatch):
    monkeypatch.setattr(
        glove_model_runtime,
        "load_model",
        lambda: pytest.fail("non-glove must not load the glove artifact"),
    )

    with pytest.raises(ValueError, match="glove items only"):
        glove_model_runtime.predict_deal(
            {"name": "AK-47 | Redline (Field-Tested)"}
        )


def test_missing_artifact_has_explicit_unavailable_error(tmp_path: Path):
    with pytest.raises(
        glove_model_runtime.GloveModelUnavailable,
        match="artifact is unavailable",
    ):
        glove_model_runtime.load_bundle_from_directory(tmp_path)


def test_smoke_rejects_wrong_identity_before_pipeline(monkeypatch):
    monkeypatch.setattr(
        glove_model_runtime.price_model_runtime,
        "smoke_bundle",
        lambda _artifact: pytest.fail("wrong identity must not reach the pipeline"),
    )

    with pytest.raises(
        glove_model_runtime.GloveModelUnavailable,
        match="identity is invalid",
    ):
        glove_model_runtime.smoke_bundle(
            {"model_name": "gloves", "item_kind": "knife"}
        )


def test_predict_uses_only_the_dedicated_glove_artifact(monkeypatch):
    artifact = {"model_name": "gloves", "item_kind": "glove"}
    observed = []
    monkeypatch.setattr(glove_model_runtime, "load_model", lambda: artifact)
    monkeypatch.setattr(
        glove_model_runtime.price_model_runtime,
        "predict_deal_with_artifact",
        lambda payload, selected, conn=None: (
            observed.append((payload, selected, conn))
            or {"model_name": "unexpected", "item_kind": "unexpected"}
        ),
    )
    payload = {"name": "★ Sport Gloves | Vice (Field-Tested)"}

    result = glove_model_runtime.predict_deal(payload)

    assert observed == [(payload, artifact, None)]
    assert result["model_name"] == "gloves"
    assert result["item_kind"] == "glove"
