import numpy as np
import pandas as pd
import pytest

import neural_model_runtime
import train_neural_model
from IGORMODEL import features as igor_features
from NEURALMODEL import features as nova_features


def _cached_frame(rows=1_200):
    timestamps = pd.date_range("2026-01-01", periods=rows, freq="min", tz="UTC")
    records = []
    for index, sold_at in enumerate(timestamps):
        # A real schema-4 cache intentionally omits seed_popularity: it is
        # learned from the chronological train split, never from all rows.
        row = {
            column: 0.0
            for column in igor_features.NUMERIC_FEATURES
            if column != "seed_popularity"
        }
        row.update({column: "none" for column in igor_features.CAT_FEATURES})
        row.update(
            {
                "listing_id": f"sale-{index:05d}",
                "sold_dt": sold_at,
                "sold_at": sold_at.isoformat(),
                "fetched_at": (sold_at + pd.Timedelta(minutes=1)).isoformat(),
                "price_cents": 1_000 + index % 50,
                "log_premium": float(np.log1p(1_000 + index % 50) - np.log1p(1_000)),
                "reference_contract_valid": 1,
                "reference_asof_future": 0,
                "reference_asof_unknown": 0,
                "weapon_type": "AK-47 | Test",
                "wear_name": "Field-Tested",
                "skin_seed_key": f"AK-47 | Test_{index % 5}",
                "item_rarity_name": "Classified",
                "paint_index_cat": "1",
                "log_reference_predicted_price": float(np.log1p(1_000)),
            }
        )
        records.append(row)
    records[0]["reference_contract_valid"] = 0
    return pd.DataFrame(records)


def test_nova_v2_uses_complete_igor_contract_without_runtime_unsafe_fields():
    assert nova_features.FEATURE_CONTRACT_VERSION == 2
    assert nova_features.FEATURE_CONTRACT_SHA256 == igor_features.FEATURE_CONTRACT_SHA256
    assert nova_features.CAT_FEATURES == list(igor_features.CAT_FEATURES)
    assert nova_features.NUMERIC_FEATURES == list(igor_features.NUMERIC_FEATURES)
    assert "keychain_0_id_cat" in nova_features.CAT_FEATURES
    assert "log_keychain_0_ref_price" in nova_features.NUMERIC_FEATURES
    assert "watchers" not in nova_features.NUMERIC_FEATURES
    assert "is_watchlisted" not in nova_features.NUMERIC_FEATURES


def test_nova_price_metrics_publish_log_quality_for_normalized_comparison():
    metrics = train_neural_model.price_metrics(
        np.array([100.0, 200.0, 400.0]),
        np.array([110.0, 190.0, 420.0]),
    )

    assert metrics["rmse_log_price"] > 0
    assert metrics["r2_log_price"] <= 1


def test_nova_v2_split_filters_invalid_anchors_and_reserves_timestamp_safe_test(
    tmp_path,
):
    cache_path = tmp_path / "features.pkl"
    _cached_frame().to_pickle(cache_path)

    (
        train,
        validation,
        test,
        production_test,
        split,
        exclusions,
        seed_counts,
        runtime_history,
    ) = train_neural_model.load_splits(cache_path, 0, test_rows=500)

    assert exclusions["source_rows"] == 1_200
    assert exclusions["runtime_anchor_valid_rows"] == 1_199
    assert len(test) >= 500
    assert len(production_test) == len(test)
    assert len(train) + len(validation) + len(test) == 1_199
    assert train["sold_dt"].max() < validation["sold_dt"].min()
    assert validation["sold_dt"].max() < test["sold_dt"].min()
    assert split["method"] == "fixed_newest_rows_with_timestamp_ties"
    assert seed_counts
    assert runtime_history["policy"] == "frozen_at_fit_train_boundary"
    expected_premium = runtime_history["premium_by_weapon"]["AK-47 | Test"]
    assert test["weapon_type_recent_premium"].eq(expected_premium).all()
    assert "seed_popularity" in train
    assert "seed_popularity" in validation
    assert "seed_popularity" in test
    assert "seed_popularity" not in production_test
    expected_seed_popularity = np.log1p(seed_counts["AK-47 | Test_0"])
    matching_train = train[train["skin_seed_key"] == "AK-47 | Test_0"]
    assert matching_train["seed_popularity"].eq(expected_seed_popularity).all()


def test_nova_v2_cache_still_rejects_missing_source_feature(tmp_path):
    cache_path = tmp_path / "features.pkl"
    _cached_frame().drop(columns=["float_value"]).to_pickle(cache_path)

    with pytest.raises(ValueError, match="float_value"):
        train_neural_model.load_splits(cache_path, 0, test_rows=500)


def test_nova_v2_production_features_use_production_frozen_metadata():
    frame = _cached_frame(1).drop(
        columns=[
            "weapon_type_recent_premium",
            "skin_liquidity_cumcount",
        ]
    )
    metadata = {
        "seed_encoder_counts": {"AK-47 | Test_0": 17},
        "runtime_history": {
            "policy": "frozen_at_fit_train_boundary",
            "premium_by_weapon": {"AK-47 | Test": 0.25},
            "count_by_weapon": {"AK-47 | Test": 99},
            "global_premium": 0.0,
        },
    }

    production_frame = train_neural_model.apply_production_v2_frozen_features(
        frame, metadata
    )

    assert production_frame["seed_popularity"].iloc[0] == pytest.approx(
        np.log1p(17)
    )
    assert production_frame["weapon_type_recent_premium"].iloc[0] == 0.25
    assert production_frame["skin_liquidity_cumcount"].iloc[0] == pytest.approx(
        np.log1p(99)
    )


def test_nova_v2_runtime_metadata_fails_closed_on_contract_drift():
    valid = {
        "artifact_version": 2,
        "feature_contract_version": 2,
        "feature_contract_sha256": nova_features.FEATURE_CONTRACT_SHA256,
        "feature_cols": nova_features.CAT_FEATURES + nova_features.NUMERIC_FEATURES,
        "cat_features": nova_features.CAT_FEATURES,
        "numeric_features": nova_features.NUMERIC_FEATURES,
    }
    neural_model_runtime.validate_metadata_contract(valid)

    with pytest.raises(RuntimeError, match="feature contract mismatch"):
        neural_model_runtime.validate_metadata_contract(
            {**valid, "feature_contract_sha256": "stale"}
        )
    with pytest.raises(RuntimeError, match="numeric features"):
        neural_model_runtime.validate_metadata_contract(
            {**valid, "numeric_features": valid["numeric_features"][:-1]}
        )

    neural_model_runtime.validate_metadata_contract({"artifact_version": 1})
