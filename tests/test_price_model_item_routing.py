import pytest

import glove_model_runtime
import igor_model_runtime
import knife_model_runtime
import neural_model_runtime
import price_model_runtime


def test_public_runtime_routes_knives_to_dedicated_model(monkeypatch):
    calls = []
    monkeypatch.setattr(
        knife_model_runtime,
        "predict_deal",
        lambda payload, conn=None: calls.append((payload, conn)) or {"model_name": "knives"},
    )
    monkeypatch.setattr(
        price_model_runtime,
        "load_model",
        lambda: pytest.fail("knife must not load the weapon-skin model"),
    )

    payload = {"name": "★ Karambit | Doppler (Factory New)"}
    assert price_model_runtime.predict_deal(payload) == {"model_name": "knives"}
    assert calls == [(payload, None)]


def test_public_runtime_routes_gloves_to_dedicated_model_without_skin_fallback(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        glove_model_runtime,
        "predict_deal",
        lambda payload, conn=None: calls.append((payload, conn))
        or {"model_name": "gloves"},
    )
    monkeypatch.setattr(
        price_model_runtime,
        "load_model",
        lambda: pytest.fail("gloves must not load the weapon-skin model"),
    )
    payload = {"name": "★ Sport Gloves | Vice (Field-Tested)"}

    assert price_model_runtime.predict_deal(payload) == {"model_name": "gloves"}
    assert calls == [(payload, None)]


def test_artifact_kind_guard_rejects_cross_category_before_prediction(monkeypatch):
    monkeypatch.setattr(
        price_model_runtime,
        "build_feature_row",
        lambda *_args, **_kwargs: (
            {"name": "★ Butterfly Knife | Fade (Factory New)"},
            [],
        ),
    )
    with pytest.raises(ValueError, match="weapon_skin model cannot evaluate knife"):
        price_model_runtime.predict_deal_with_artifact(
            {}, {"model_name": "almaz", "item_kind": "weapon_skin"}
        )


def test_almaz_loader_rejects_explicit_non_weapon_artifact_kind(
    tmp_path,
    monkeypatch,
):
    (tmp_path / price_model_runtime.MODEL_PATH.name).write_bytes(b"candidate")
    monkeypatch.setattr(
        price_model_runtime.joblib,
        "load",
        lambda _path: {"model_name": "almaz", "item_kind": "knife"},
    )

    with pytest.raises(RuntimeError, match="not a weapon-skin model"):
        price_model_runtime.load_bundle_from_directory(tmp_path)


@pytest.mark.parametrize(
    ("runtime", "loader_name", "expected_message"),
    [
        (igor_model_runtime, "load_models", "Igor weapon-skin model"),
        (neural_model_runtime, "load_model", "Nova weapon-skin model"),
    ],
)
def test_weapon_only_runtimes_reject_knives_before_loading_artifacts(
    monkeypatch, runtime, loader_name, expected_message
):
    monkeypatch.setattr(
        runtime,
        loader_name,
        lambda: pytest.fail("knife must be rejected before loading weapon artifacts"),
    )

    with pytest.raises(ValueError, match=expected_message):
        runtime.predict_deal({"name": "★ M9 Bayonet | Fade (Factory New)"})


@pytest.mark.parametrize(
    ("runtime", "loader_name", "expected_message"),
    [
        (price_model_runtime, "load_model", "item identity is unknown"),
        (igor_model_runtime, "load_models", "cannot evaluate unknown items"),
        (neural_model_runtime, "load_model", "cannot evaluate unknown items"),
    ],
)
def test_weapon_runtimes_fail_closed_for_unknown_item_identity(
    monkeypatch,
    runtime,
    loader_name,
    expected_message,
):
    monkeypatch.setattr(
        runtime,
        loader_name,
        lambda: pytest.fail("unknown items must not load a model artifact"),
    )

    with pytest.raises(ValueError, match=expected_message):
        runtime.predict_deal({"name": "Sticker | Crown (Foil)"})
