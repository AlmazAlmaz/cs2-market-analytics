import hashlib
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

import price_model_runtime
import train_glove_price_model
import train_price_model
from IGORMODEL import build_cache as igor_build_cache


def _create_almaz_training_database(path, market_hash_name):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sales (
                listing_id TEXT, name TEXT, base_skin_name TEXT, wear_name TEXT,
                price_cents INTEGER, reference_base_price INTEGER,
                reference_predicted_price INTEGER, reference_float_factor REAL,
                reference_quantity INTEGER, float_value REAL, paint_seed INTEGER,
                paint_index INTEGER, created_at TEXT, sold_at TEXT, fetched_at TEXT,
                first_seen_at TEXT, reference_last_updated TEXT, watchers INTEGER,
                item_quality TEXT, item_rarity INTEGER, item_rarity_name TEXT,
                item_def_index INTEGER, item_wear_name TEXT, item_is_souvenir INTEGER,
                item_is_stattrak INTEGER, item_collection_name TEXT,
                item_type_name TEXT, state TEXT
            );
            CREATE TABLE sale_stickers (
                listing_id TEXT, sticker_index INTEGER, sticker_id INTEGER,
                slot INTEGER, name TEXT, wear REAL, price_cents INTEGER,
                reference_price_cents INTEGER, reference_quantity INTEGER,
                offset_x REAL, offset_y REAL
            );
            """
        )
        common = (
            market_hash_name,
            market_hash_name.rsplit(" (", 1)[0],
            "Factory New",
            10_000,
            9_000,
            9_500,
            1.0,
            10,
            0.01,
            1,
            2,
            "2026-01-01T00:00:00+00:00",
            "2026-01-10T00:00:00+00:00",
            "2026-01-03T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
        )
        tail = (0, "normal", 1, "rare", 7, "Factory New", 0, 0, "Test", "Test", "sold")
        conn.executemany(
            "INSERT INTO sales VALUES (" + ", ".join("?" for _ in range(28)) + ")",
            [
                ("safe", *common, "2026-01-01T12:00:00+00:00", *tail),
                ("future", *common, "2026-01-11T00:00:00+00:00", *tail),
                ("missing", *common, None, *tail),
            ],
        )


def test_training_market_stats_use_only_prior_sales():
    frame = pd.DataFrame(
        {
            "listing_id": ["a", "b", "c"],
            "name": ["Skin", "Skin", "Skin"],
            "sold_dt": pd.to_datetime(
                ["2026-01-01", "2026-01-02", "2026-01-03"], utc=True
            ),
            "price_cents": [100, 300, 900],
            "float_value": [0.1, 0.2, 0.3],
            "sticker_count": [0, 1, 0],
            "sticker_price_sum": [0, 50, 0],
        }
    )
    market_stats = {
        "global": {
            "avg_price_cents": 400,
            "median_price_cents": 300,
            "max_sales_per_day": 10,
        }
    }
    result = train_price_model.apply_time_safe_training_market_stats(frame, market_stats)

    assert result["skin_sales_count"].tolist() == [0, 1, 2]
    assert result["skin_avg_price_cents"].tolist() == [400, 100, 200]
    assert result["skin_median_price_cents"].tolist() == [300, 100, 200]
    assert result["skin_min_price_cents"].tolist() == [300, 100, 100]
    assert result["skin_max_price_cents"].tolist() == [300, 100, 300]


def test_almaz_feature_contract_excludes_post_sale_runtime_features():
    categorical, numeric = train_price_model.get_feature_columns()
    columns = set(categorical + numeric)

    assert "listing_age_hours" not in columns
    assert "watchers" not in columns
    assert not columns.intersection(train_price_model.RUNTIME_UNSAFE_FEATURES)


@pytest.mark.parametrize(
    ("item_kind", "market_hash_name"),
    [
        ("weapon_skin", "AK-47 | Test (Factory New)"),
        ("knife", "★ Karambit | Test (Factory New)"),
        ("glove", "★ Sport Gloves | Test (Field-Tested)"),
    ],
)
def test_almaz_like_training_rejects_future_and_missing_reference_snapshots(
    tmp_path, item_kind, market_hash_name
):
    db_path = tmp_path / f"{item_kind}.db"
    _create_almaz_training_database(db_path, market_hash_name)

    sales, stickers = train_price_model.load_data(db_path, item_kind=item_kind)
    fingerprint = train_price_model.source_database_fingerprint(
        db_path, item_kind=item_kind
    )
    with sqlite3.connect(db_path) as conn:
        unfiltered = pd.read_sql_query(
            """
            SELECT listing_id, sold_at, reference_last_updated,
                   reference_predicted_price, reference_base_price, price_cents
            FROM sales
            """,
            conn,
        )

    assert sales["listing_id"].tolist() == ["safe"]
    assert stickers.empty
    assert fingerprint["source_valid_price_rows"] == 3
    assert fingerprint["valid_price_rows"] == 1
    assert fingerprint["min_sold_at"] == "2026-01-10T00:00:00+00:00"
    assert fingerprint["max_sold_at"] == "2026-01-10T00:00:00+00:00"
    assert train_price_model.add_reference_target(unfiltered)[
        "listing_id"
    ].tolist() == ["safe"]


@pytest.mark.parametrize(
    "market_hash_name",
    ["AK-47 | Test (Factory New)", "★ Karambit | Test (Factory New)"],
)
def test_almaz_training_and_runtime_timestamp_features_have_observation_parity(
    market_hash_name,
):
    frame = pd.DataFrame(
        {
            "listing_id": ["first", "fetched", "created"],
            "sold_at": ["2099-12-31T23:00:00+00:00"] * 3,
            "first_seen_at": ["2026-04-01T01:02:03+00:00", None, None],
            "fetched_at": [
                "2026-04-02T02:03:04+00:00",
                "2026-05-03T03:04:05+00:00",
                None,
            ],
            "created_at": [
                "2026-03-01T00:00:00+00:00",
                "2026-03-02T00:00:00+00:00",
                "2026-06-04T04:05:06+00:00",
            ],
        }
    )
    training = train_price_model.add_time_features(frame)
    artifact = {"market_stats": {}, "max_stickers": 0}
    columns = (
        "sold_year",
        "sold_month",
        "sold_dayofweek",
        "sold_hour",
        "sold_days_since_epoch",
    )

    for index, source in frame.iterrows():
        runtime_row, _stickers = price_model_runtime.build_feature_row(
            {
                "name": market_hash_name,
                "sold_at": source["sold_at"],
                "first_seen_at": source["first_seen_at"],
                "fetched_at": source["fetched_at"],
                "listing_created_at": source["created_at"],
            },
            artifact,
        )
        for column in columns:
            assert runtime_row[column] == pytest.approx(training.loc[index, column])

    assert training["sold_year"].tolist() == [2026, 2026, 2026]
    assert training["sold_month"].tolist() == [4, 5, 6]


def test_chronological_split_keeps_test_newer_and_untouched():
    frame = pd.DataFrame(
        {
            "listing_id": [str(index) for index in range(20_000)],
            "sold_dt": pd.date_range("2026-01-01", periods=20_000, freq="min", tz="UTC"),
        }
    )
    cutoff = frame["sold_dt"].iloc[15_999].isoformat()
    train, validation, test, contract = (
        train_price_model.chronological_train_validation_test_split(
            frame,
            holdout_after=cutoff,
            validation_fraction=0.10,
        )
    )

    assert train["sold_dt"].max() < validation["sold_dt"].min()
    assert validation["sold_dt"].max() < test["sold_dt"].min()
    assert len(test) == 4_000
    assert contract["test_untouched_until_final_evaluation"] is True


def test_training_and_runtime_log_premium_reconstruction_are_identical():
    frame = pd.DataFrame(
        {
            "reference_predicted_price": [10_000.0],
            "reference_base_price": [9_000.0],
        }
    )
    raw = np.array([0.20])
    calibration = {"premium_scale": 0.85, "center_offset": -0.01}
    training_value = train_price_model.prediction_to_absolute_log(
        raw,
        frame,
        target_mode="log_premium",
        calibration=calibration,
    )[0]
    runtime_value = price_model_runtime.absolute_log_prediction(
        raw[0],
        frame.iloc[0].to_dict(),
        {"target_mode": "log_premium", "prediction_calibration": calibration},
    )

    assert runtime_value == training_value


def test_database_fingerprint_changes_when_logical_rows_change(tmp_path):
    db_path = tmp_path / "tiny.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sales (
                listing_id TEXT, sold_at TEXT, fetched_at TEXT, price_cents INTEGER,
                state TEXT, name TEXT, reference_last_updated TEXT,
                reference_predicted_price INTEGER, reference_base_price INTEGER
            );
            CREATE TABLE sale_stickers (listing_id TEXT);
            INSERT INTO sales VALUES (
                'a', '2026-01-01', '2026-01-02', 100, 'sold', 'AK-47 | Test',
                '2025-12-31', 100, 100
            );
            """
        )
    before = train_price_model.source_database_fingerprint(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "b", "2026-01-03", "2026-01-04", 200, "sold", "M4A4 | Test",
                "2026-01-02", 200, 200,
            ),
        )
    after = train_price_model.source_database_fingerprint(db_path)

    assert before["sales_rows"] == 1
    assert after["sales_rows"] == 2
    assert before["logical_sha256"] != after["logical_sha256"]


def test_training_fingerprint_keeps_skin_and_knife_rows_isolated(tmp_path):
    db_path = tmp_path / "segments.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sales (
                listing_id TEXT, sold_at TEXT, fetched_at TEXT, price_cents INTEGER,
                state TEXT, name TEXT, reference_last_updated TEXT,
                reference_predicted_price INTEGER, reference_base_price INTEGER
            );
            CREATE TABLE sale_stickers (listing_id TEXT);
            """
        )
        conn.executemany(
            "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "skin", "2026-01-01", "2026-01-02", 100, "sold",
                    "AK-47 | Test", "2025-12-31", 100, 100,
                ),
                (
                    "knife",
                    "2026-01-03",
                    "2026-01-04",
                    10_000,
                    "sold",
                    "★ Karambit | Fade (Factory New)",
                    "2026-01-02",
                    10_000,
                    10_000,
                ),
                (
                    "glove",
                    "2026-01-05",
                    "2026-01-06",
                    20_000,
                    "sold",
                    "★ Sport Gloves | Vice (Field-Tested)",
                    "2026-01-04",
                    20_000,
                    20_000,
                ),
            ],
        )

    skins = train_price_model.source_database_fingerprint(
        db_path, item_kind="weapon_skin"
    )
    knives = train_price_model.source_database_fingerprint(
        db_path, item_kind="knife"
    )

    assert skins["sales_rows"] == knives["sales_rows"] == 1
    assert skins["segment_sales_rows"] == 1
    assert knives["segment_sales_rows"] == 1
    assert skins["valid_price_rows"] == knives["valid_price_rows"] == 1
    assert skins["logical_sha256"] != knives["logical_sha256"]


def test_weapon_fingerprint_ignores_glove_and_unknown_rows(tmp_path):
    db_path = tmp_path / "strict-segment.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sales (
                listing_id TEXT, sold_at TEXT, fetched_at TEXT, price_cents INTEGER,
                state TEXT, name TEXT, reference_last_updated TEXT,
                reference_predicted_price INTEGER, reference_base_price INTEGER
            );
            CREATE TABLE sale_stickers (listing_id TEXT);
            INSERT INTO sales VALUES (
                'skin', '2026-01-01', '2026-01-02', 100, 'sold',
                'AK-47 | Test (Field-Tested)', '2025-12-31', 100, 100
            );
            """
        )

    before = train_price_model.source_database_fingerprint(
        db_path,
        item_kind="weapon_skin",
    )
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "glove", "2026-01-03", "2026-01-04", 20_000, "sold",
                    "★ Sport Gloves | Vice (Field-Tested)", "2026-01-02",
                    20_000, 20_000,
                ),
                (
                    "case", "2026-01-05", "2026-01-06", 500, "sold",
                    "Revolution Case", "2026-01-04", 500, 500,
                ),
            ],
        )

    after = train_price_model.source_database_fingerprint(
        db_path,
        item_kind="weapon_skin",
    )

    assert before == after
    assert before["segment_sales_rows"] == after["segment_sales_rows"] == 1
    assert after["sales_rows"] == 1


def test_almaz_like_fingerprint_detects_same_count_feature_mutation(tmp_path):
    db_path = tmp_path / "content-mutation.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sales (
                listing_id TEXT PRIMARY KEY, sold_at TEXT, fetched_at TEXT,
                price_cents INTEGER, state TEXT, name TEXT,
                reference_last_updated TEXT, reference_predicted_price INTEGER,
                reference_base_price INTEGER, float_value REAL
            );
            CREATE TABLE sale_stickers (listing_id TEXT);
            INSERT INTO sales VALUES (
                'skin', '2026-01-01', '2026-01-02', 100, 'sold',
                'AK-47 | Test (Field-Tested)', '2025-12-31', 100, 100, 0.15
            );
            """
        )

    before = train_price_model.source_database_fingerprint(
        db_path,
        item_kind="weapon_skin",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE sales SET float_value = ?, reference_predicted_price = ? "
            "WHERE listing_id = ?",
            (0.16, 101, "skin"),
        )
    after = train_price_model.source_database_fingerprint(
        db_path,
        item_kind="weapon_skin",
    )

    for key in (
        "sales_rows",
        "segment_sales_rows",
        "valid_price_rows",
        "min_sold_at",
        "max_sold_at",
    ):
        assert before[key] == after[key]
    assert before["content_sha256"] != after["content_sha256"]
    assert before["logical_sha256"] != after["logical_sha256"]


def test_weapon_content_fingerprint_hashes_only_weapon_stickers(tmp_path):
    db_path = tmp_path / "sticker-content.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sales (
                listing_id TEXT PRIMARY KEY, sold_at TEXT, fetched_at TEXT,
                price_cents INTEGER, state TEXT, name TEXT,
                reference_last_updated TEXT, reference_predicted_price INTEGER,
                reference_base_price INTEGER
            );
            CREATE TABLE sale_stickers (
                listing_id TEXT, sticker_index INTEGER, sticker_id INTEGER,
                slot INTEGER, name TEXT, wear REAL, price_cents INTEGER,
                reference_price_cents INTEGER, reference_quantity INTEGER,
                offset_x REAL, offset_y REAL,
                PRIMARY KEY (listing_id, sticker_index)
            );
            INSERT INTO sales VALUES (
                'skin', '2026-01-01', '2026-01-02', 100, 'sold',
                'AK-47 | Test (Field-Tested)', '2025-12-31', 100, 100
            );
            INSERT INTO sales VALUES (
                'knife', '2026-01-01', '2026-01-02', 10000, 'sold',
                '★ Karambit | Test (Field-Tested)', '2025-12-31', 10000, 10000
            );
            INSERT INTO sale_stickers VALUES (
                'skin', 0, 10, 0, 'Sticker | Skin', 0.0, 100, 100, 1, 0, 0
            );
            INSERT INTO sale_stickers VALUES (
                'knife', 0, 20, 0, 'Sticker | Knife', 0.0, 200, 200, 1, 0, 0
            );
            """
        )

    before = train_price_model.source_database_fingerprint(
        db_path,
        item_kind="weapon_skin",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE sale_stickers SET price_cents = 201 WHERE listing_id = 'knife'"
        )
    unrelated = train_price_model.source_database_fingerprint(
        db_path,
        item_kind="weapon_skin",
    )
    assert unrelated == before

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE sale_stickers SET price_cents = 101 WHERE listing_id = 'skin'"
        )
    relevant = train_price_model.source_database_fingerprint(
        db_path,
        item_kind="weapon_skin",
    )
    assert relevant["content_sha256"] != before["content_sha256"]
    assert relevant["logical_sha256"] != before["logical_sha256"]


def test_glove_profile_is_isolated_from_weapon_and_knife_profiles():
    glove = train_price_model.training_profile("glove")

    assert glove.model_name == "gloves"
    assert glove.item_kind == "glove"
    assert glove.model_path.name == "glove_price_model.joblib"
    assert glove.metrics_path.name == "glove_price_model_metrics.json"
    assert glove.model_dir.name == "artifacts"
    assert glove.model_dir.parent.name == "GLOVEMODEL"


def test_glove_trainer_wrapper_rejects_cross_kind_override():
    with pytest.raises(ValueError, match="glove data only"):
        train_glove_price_model.main(["--item-kind", "weapon_skin"])


def test_reused_point_artifact_requires_exact_kind_and_strict_source_policy():
    profile = train_price_model.training_profile("glove")
    feature_columns = ["name", "float_value"]
    split_contract = {"holdout_after": "2026-01-01T00:00:00+00:00"}
    artifact = {
        "model_name": "gloves",
        "item_kind": "glove",
        "source_database": {
            "item_kind": "glove",
            "item_kind_policy": train_price_model.STRICT_ITEM_KIND_POLICY,
        },
        "target_mode": train_price_model.TARGET_MODE,
        "feature_columns": feature_columns,
        "split_contract": split_contract,
    }

    train_price_model._validate_reused_point_artifact(
        artifact,
        profile=profile,
        feature_columns=feature_columns,
        split_contract=split_contract,
    )

    cross_kind = json.loads(json.dumps(artifact))
    cross_kind["item_kind"] = "weapon_skin"
    with pytest.raises(ValueError, match="another item kind"):
        train_price_model._validate_reused_point_artifact(
            cross_kind,
            profile=profile,
            feature_columns=feature_columns,
            split_contract=split_contract,
        )

    permissive = json.loads(json.dumps(artifact))
    permissive["source_database"]["item_kind_policy"] = "legacy_permissive"
    with pytest.raises(ValueError, match="not strictly classified"):
        train_price_model._validate_reused_point_artifact(
            permissive,
            profile=profile,
            feature_columns=feature_columns,
            split_contract=split_contract,
        )


def test_unknown_commodity_is_not_loaded_as_weapon_training_data(tmp_path):
    db_path = tmp_path / "unknown.db"
    _create_almaz_training_database(db_path, "Revolution Case")

    sales, stickers = train_price_model.load_data(
        db_path,
        item_kind="weapon_skin",
    )
    fingerprint = train_price_model.source_database_fingerprint(
        db_path,
        item_kind="weapon_skin",
    )

    assert sales.empty
    assert stickers.empty
    assert fingerprint["segment_sales_rows"] == 0
    assert fingerprint["valid_price_rows"] == 0


def test_almaz_staging_metrics_identify_saved_model_bytes(tmp_path):
    model_path = tmp_path / "almaz.joblib"
    metrics_path = tmp_path / "almaz_metrics.json"
    original_metrics = {"model_name": "almaz", "stage": "staging"}

    saved = train_price_model.save_staging_candidate(
        {"model_name": "almaz", "payload": [1, 2, 3]},
        original_metrics,
        model_path,
        metrics_path,
    )

    actual_sha = hashlib.sha256(model_path.read_bytes()).hexdigest()
    persisted = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert "model_sha256" not in original_metrics
    assert saved["model_sha256"] == actual_sha
    assert persisted["model_sha256"] == actual_sha


def test_igor_cache_is_invalidated_when_database_grows(tmp_path, monkeypatch):
    db_path = tmp_path / "igor.db"
    cache_path = tmp_path / "features.pkl"
    metadata_path = tmp_path / "features.metadata.json"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sales (
                listing_id TEXT, name TEXT, state TEXT, price_cents INTEGER,
                sold_at TEXT, fetched_at TEXT
            );
            CREATE TABLE sale_stickers (listing_id TEXT);
            INSERT INTO sales VALUES (
                'a', 'AK-47 | Test (Field-Tested)', 'sold', 100,
                '2026-01-01', '2026-01-02'
            );
            """
        )
    cache_path.write_bytes(b"cache")
    fingerprint = igor_build_cache.database_fingerprint(db_path)
    metadata_path.write_text(
        json.dumps(
            {
                "item_kind": "weapon_skin",
                "cache_schema_version": igor_build_cache.CACHE_SCHEMA_VERSION,
                "feature_contract_sha256": fingerprint["feature_contract_sha256"],
                "source_database": fingerprint,
                "database_changed_during_build": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(igor_build_cache, "CACHE_PATH", cache_path)
    monkeypatch.setattr(igor_build_cache, "CACHE_METADATA_PATH", metadata_path)

    assert igor_build_cache.is_cache_current(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?)",
            (
                "b",
                "M4A4 | Test (Factory New)",
                "sold",
                200,
                "2026-01-03",
                "2026-01-04",
            ),
        )
    assert not igor_build_cache.is_cache_current(db_path)
