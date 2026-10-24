"""Operation-count regressions for live inference, without network requests."""

import math
import sqlite3

import numpy as np
import pandas as pd
import pytest
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import TargetEncoder

import deal_scanner
import igor_model_runtime
import price_model_runtime


def _metadata(seed_counts=None, *, contract_version=2):
    return {
        "feature_contract_version": contract_version,
        "feature_cols": ["weapon_type", "seed_popularity"],
        "cat_features": ["weapon_type"],
        "seed_encoder_counts": seed_counts or {},
        "runtime_history": {"global_premium": 0.0},
    }


def _payload(listing_id="listing-1"):
    return {
        "listing_id": listing_id,
        "name": "AK-47 | Test (Field-Tested)",
        "buy_price_cents": 1000,
        "reference_base_price": 1200,
        "reference_predicted_price": 1300,
        "float_value": 0.25,
        "paint_seed": 42,
        "stickers": [],
    }


def test_seed_popularity_looks_up_only_live_keys(monkeypatch):
    class LookupOnlyCounts(dict):
        def keys(self):
            raise AssertionError("must not index the entire training vocabulary")

        def values(self):
            raise AssertionError("must not scan the entire training vocabulary")

    counts = LookupOnlyCounts({f"seed-{index}": index for index in range(10_000)})
    base = pd.DataFrame(
        {"skin_seed_key": ["seed-42", "unknown", "seed-0"],
         "weapon_type": ["AK-47"] * 3}
    )
    monkeypatch.setattr(
        igor_model_runtime,
        "_build_base_feature_frame",
        lambda *_args: (base, [], []),
    )

    frame, _, _, _ = igor_model_runtime.build_feature_frame(
        {}, None, _metadata(counts)
    )

    assert frame["seed_popularity"].tolist() == [math.log1p(42), 0.0, 0.0]


def test_scanner_shares_base_features_only_within_one_listing(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    listings = [{"id": "listing-1"}, {"id": "listing-2"}]
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        lambda *_args, **_kwargs: (
            listings, None, {}, "https://example.invalid/listings"
        ),
    )
    monkeypatch.setattr(
        deal_scanner, "normalize_listing_to_deal_payload",
        lambda listing, *_args: {
            "listing_id": listing["id"],
            "deal_payload": _payload(listing["id"]),
        },
    )
    monkeypatch.setattr(
        deal_scanner.model_generations, "read_active_pointer", lambda *_a, **_k: {}
    )
    preparations = []
    original_prepare = igor_model_runtime._build_base_feature_frame

    def prepare(payload, model_conn, contract_version):
        preparations.append(payload["listing_id"])
        return original_prepare(payload, model_conn, contract_version)

    monkeypatch.setattr(igor_model_runtime, "_build_base_feature_frame", prepare)
    observed_frames = []

    def model(payload, _conn):
        frame, _, _, _ = igor_model_runtime.build_feature_frame(
            payload, None, _metadata()
        )
        assert "previous_model_only" not in frame
        frame["previous_model_only"] = True
        observed_frames.append(frame)
        return {
            "name": payload["name"], "buy_price_cents": 1000,
            "predicted_price_cents": 1400, "profit_cents": 200,
            "roi_percent": 20, "liquidity_score": 30, "verdict": "good",
        }

    for name in ("igor", "nova", "vega"):
        monkeypatch.setitem(deal_scanner.MODEL_RUNNERS, name, model)
    config = deal_scanner.validate_config(
        {**deal_scanner.DEFAULT_CONFIG, "enabled_models": ["igor", "nova", "vega"]}
    )

    try:
        result = deal_scanner.scan_sticker(
            conn, {"sticker_id": 1, "name": "Sticker | Test"}, config
        )
        assert result[0:2] == (2, 6)
        assert preparations == ["listing-1", "listing-2"]
        assert len(observed_frames) == 6
        assert igor_model_runtime._FEATURE_PREPARATION_CACHE.get() is None
    finally:
        conn.close()


def test_feature_reuse_keeps_artifact_encodings_and_contracts_separate(monkeypatch):
    preparations = []

    def prepare(payload, conn, contract_version):
        preparations.append(contract_version)
        return pd.DataFrame(
            {"skin_seed_key": ["shared"], "weapon_type": ["AK-47"]}
        ), [], []

    monkeypatch.setattr(igor_model_runtime, "_build_base_feature_frame", prepare)
    payload = _payload()
    with igor_model_runtime.feature_preparation_cache():
        first, *_ = igor_model_runtime.build_feature_frame(
            payload, None, _metadata({"shared": 10})
        )
        second, *_ = igor_model_runtime.build_feature_frame(
            payload, None, _metadata({"shared": 100})
        )
        igor_model_runtime.build_feature_frame(
            payload, None, _metadata({"shared": 200}, contract_version=1)
        )

    assert preparations == [2, 1]
    assert first["seed_popularity"].iloc[0] == pytest.approx(math.log1p(10))
    assert second["seed_popularity"].iloc[0] == pytest.approx(math.log1p(100))


def test_feature_reuse_is_cleared_after_interruption():
    with (
        pytest.raises(deal_scanner.ScanControlInterrupted),
        igor_model_runtime.feature_preparation_cache(),
    ):
        raise deal_scanner.ScanControlInterrupted("paused")
    assert igor_model_runtime._FEATURE_PREPARATION_CACHE.get() is None


def test_feature_reuse_isolates_database_connections_and_mutable_outputs(monkeypatch):
    preparations = []

    def prepare(payload, conn, contract_version):
        preparations.append(conn)
        return (
            pd.DataFrame({"skin_seed_key": ["shared"], "weapon_type": ["AK-47"]}),
            [{"name": "original", "nested": {"value": 1}}],
            [{"name": "original"}],
        )

    monkeypatch.setattr(igor_model_runtime, "_build_base_feature_frame", prepare)
    payload = _payload()
    first_conn, second_conn = object(), object()
    with igor_model_runtime.feature_preparation_cache():
        _, _, stickers, keychains = igor_model_runtime.build_feature_frame(
            payload, first_conn, _metadata()
        )
        stickers[0]["nested"]["value"] = 99
        keychains[0]["name"] = "changed"
        _, _, stickers, keychains = igor_model_runtime.build_feature_frame(
            payload, first_conn, _metadata()
        )
        assert stickers[0]["nested"]["value"] == 1
        assert keychains[0]["name"] == "original"
        igor_model_runtime.build_feature_frame(payload, second_conn, _metadata())
    assert preparations == [first_conn, second_conn]


@pytest.mark.parametrize("offset", [-2.0, 0.0, 2.0])
def test_live_target_encoder_matches_fitted_pipeline_and_unknown_categories(offset):
    training = pd.DataFrame({
        "skin": ["ak", "m4", "awp"] * 10,
        "sticker": ["none", "holo"] * 15,
        "price": np.arange(30, dtype=float),
    })
    pipeline = Pipeline([
        ("features", ColumnTransformer([
            ("cat", TargetEncoder(target_type="continuous", random_state=0),
             ["skin", "sticker"]),
            ("num", SimpleImputer(), ["price"]),
        ])),
        ("model", LinearRegression()),
    ]).fit(training, np.arange(30, dtype=float) + offset)
    original_encoder = pipeline.named_steps["features"].named_transformers_["cat"]
    prepared = price_model_runtime._prepare_live_prediction_pipeline(pipeline)
    assert prepared is not pipeline
    assert type(original_encoder) is TargetEncoder
    sample = pd.DataFrame({
        "skin": ["ak", "unseen", "m4"],
        "sticker": ["unseen", "none", "holo"],
        "price": [1.0, np.nan, 10.0],
    }, index=[4, 7, 12])
    np.testing.assert_array_equal(
        prepared.named_steps["features"].transform(sample),
        pipeline.named_steps["features"].transform(sample),
    )
    np.testing.assert_array_equal(prepared.predict(sample), pipeline.predict(sample))
    # Missing values deliberately use sklearn's original validation/semantics.
    sample.loc[7, "skin"] = None
    np.testing.assert_array_equal(prepared.predict(sample), pipeline.predict(sample))


def test_live_target_encoder_reuses_vocabulary_and_keeps_feature_validation(monkeypatch):
    training = pd.DataFrame({"skin": [f"skin-{i}" for i in range(1000)]})
    pipeline = Pipeline([
        ("features", ColumnTransformer([
            ("cat", TargetEncoder(target_type="continuous"), ["skin"]),
        ])),
    ]).fit(training, np.arange(1000, dtype=float))
    prepared = price_model_runtime._prepare_live_prediction_pipeline(pipeline)
    encoder = prepared.named_steps["features"].named_transformers_["cat"]
    sample = pd.DataFrame({"skin": ["skin-5", "new-skin"]})
    expected = pipeline.transform(sample)

    def no_vocabulary_scan(*args, **kwargs):
        raise AssertionError("live string categories must use the prepared lookup")

    monkeypatch.setattr(encoder, "_transform", no_vocabulary_scan)
    np.testing.assert_array_equal(prepared.transform(sample), expected)
    np.testing.assert_array_equal(prepared.transform(sample), expected)
    monkeypatch.undo()
    with pytest.raises(ValueError, match="feature names"):
        encoder.transform(sample.rename(columns={"skin": "wrong"}))


def test_unsupported_target_encoder_keeps_original_pipeline():
    training = pd.DataFrame({"category": [1, 2, 3] * 10})
    pipeline = Pipeline([
        ("features", ColumnTransformer([
            ("cat", TargetEncoder(target_type="continuous"), ["category"]),
        ])),
    ]).fit(training, np.arange(30, dtype=float))
    assert price_model_runtime._prepare_live_prediction_pipeline(pipeline) is pipeline
