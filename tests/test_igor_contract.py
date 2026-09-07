import json
import math
import pickle
import sqlite3

import pandas as pd
import pytest

import igor_model_runtime
from IGORMODEL import build_cache, data_loading, feature_contract, features
from IGORMODEL import train as igor_train


def sale_frame(**overrides):
    row = {
        "listing_id": "sale-1",
        "name": "AK-47 | Test (Field-Tested)",
        "base_skin_name": "AK-47 | Test",
        "wear_name": "Field-Tested",
        "price_cents": 15_000,
        "reference_base_price": 10_000,
        "reference_predicted_price": 11_000,
        "reference_float_factor": 1.0,
        "reference_quantity": 100,
        "reference_keychain_price": 1_000,
        "reference_last_updated": "2026-08-01T11:00:00Z",
        "float_value": 0.2,
        "paint_seed": 7,
        "paint_index": 9,
        "state": "sold",
        "created_at": "2026-08-01T10:00:00Z",
        "sold_at": "2026-08-01T12:00:00Z",
        "fetched_at": "2026-08-01T12:30:00Z",
        "sale_type": "buy_now",
        "is_seller": 0,
        "is_watchlisted": 0,
        "watchers": 0,
        "item_def_index": 7,
        "item_rarity_name": "Classified",
        "item_collection_name": "Test",
        "item_is_stattrak": 0,
        "item_is_souvenir": 0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def sticker_frame(*, updated_at="2026-08-01T11:30:00Z", wear=50):
    return pd.DataFrame(
        [
            {
                "listing_id": "sale-1",
                "sticker_index": 0,
                "sticker_id": 42,
                "slot": 0,
                "name": "Sticker | Test (Holo)",
                "wear": wear,
                "price_cents": 500,
                "reference_price_cents": 500,
                "reference_updated_at": updated_at,
            }
        ]
    )


def keychain_frame(*, updated_at="2026-08-01T11:30:00Z"):
    return pd.DataFrame(
        [
            {
                "listing_id": "sale-1",
                "keychain_index": 0,
                "keychain_id": 501,
                "slot": 0,
                "name": "Charm | Test",
                "pattern": 17,
                "price_cents": 1_000,
                "reference_quantity": 20,
                "reference_updated_at": updated_at,
            }
        ]
    )


def test_sticker_wear_normalization_preserves_missing_sentinel_once():
    assert feature_contract.normalize_sticker_wear(-1) == -1
    assert feature_contract.normalize_sticker_wear(None) == -1
    assert feature_contract.normalize_sticker_wear(50) == 0.5
    assert feature_contract.normalize_sticker_wear(0.5) == 0.5

    prepared = data_loading.prepare_stickers(
        pd.concat([sticker_frame(wear=-1), sticker_frame(wear=50)], ignore_index=True),
        sale_frame(),
    )
    assert math.isnan(prepared.loc[0, "wear"])
    assert prepared.loc[1, "wear"] == 0.5


def test_future_child_references_are_tagged_and_masked():
    sales = sale_frame()
    stickers = data_loading.prepare_stickers(
        sticker_frame(updated_at="2026-08-02T00:00:00Z"), sales
    )
    keychains = data_loading.prepare_keychains(
        keychain_frame(updated_at="2026-08-02T00:00:00Z"), sales
    )

    assert stickers.loc[0, "raw_reference_price_cents"] == 500
    assert stickers.loc[0, "safe_reference_price_cents"] == 0
    assert stickers.loc[0, "reference_asof_future"] == 1
    assert keychains.loc[0, "raw_reference_price_cents"] == 1_000
    assert keychains.loc[0, "safe_reference_price_cents"] == 0
    assert keychains.loc[0, "reference_asof_future"] == 1


def test_v2_reference_contract_decomposes_charm_and_marks_missing_prediction():
    raw = data_loading.assemble_dataset(sale_frame(), sticker_frame(), keychain_frame())
    frame = features.build_time_safe_features(raw)

    assert frame.loc[0, "reference_contract_valid"] == 1
    assert frame.loc[0, "has_reference"] == 1
    assert frame.loc[0, "reference_predicted_price_filled"] == 10_000
    assert frame.loc[0, "reference_keychain_price_safe"] == 1_000
    assert frame.loc[0, "keychain_count"] == 1
    assert frame.loc[0, "keychain_0_id_cat"] == "501"

    missing_predicted = sale_frame(
        reference_predicted_price=None, reference_keychain_price=0
    )
    missing_raw = data_loading.assemble_dataset(
        missing_predicted, sticker_frame().iloc[0:0], keychain_frame().iloc[0:0]
    )
    missing = features.build_time_safe_features(missing_raw)
    assert missing.loc[0, "has_reference"] == 0
    assert missing.loc[0, "has_reference_anchor"] == 1
    assert missing.loc[0, "reference_predicted_price_filled"] == 10_000


def test_v2_runtime_sticker_and_keychain_contract_matches_training():
    raw = data_loading.assemble_dataset(sale_frame(), sticker_frame(), keychain_frame())
    trained = features.build_time_safe_features(raw)
    metadata = {
        "feature_contract_version": 2,
        "feature_cols": [
            "has_reference",
            "has_reference_anchor",
            "log_reference_predicted_price",
            "log_reference_keychain_price",
            "sticker_0_wear",
            "log_sticker_0_ref_price",
            "sticker_reference_safe_count",
            "keychain_0_id_cat",
            "keychain_0_pattern_cat",
            "log_keychain_0_ref_price",
            "keychain_reference_safe_count",
            "weapon_type_recent_premium",
            "skin_liquidity_cumcount",
        ],
        "cat_features": ["keychain_0_id_cat", "keychain_0_pattern_cat"],
        "seed_encoder_counts": {},
        "runtime_history": {
            "premium_by_weapon": {"AK-47 | Test": 0.1},
            "count_by_weapon": {"AK-47 | Test": 10},
            "global_premium": 0.0,
        },
    }
    payload = {
        "name": "AK-47 | Test (Field-Tested)",
        "base_skin_name": "AK-47 | Test",
        "wear_name": "Field-Tested",
        "reference_base_price": 10_000,
        "reference_predicted_price": 11_000,
        "reference_keychain_price": 1_000,
        "reference_last_updated": "2026-08-01T11:00:00Z",
        "float_value": 0.2,
        "paint_seed": 7,
        "paint_index": 9,
        "stickers": [
            {
                "sticker_id": 42,
                "name": "Sticker | Test (Holo)",
                "wear": 50,
                "reference_price_cents": 500,
            }
        ],
        "keychains": [
            {
                "keychain_id": 501,
                "name": "Charm | Test",
                "pattern": 17,
                "price_cents": 1_000,
            }
        ],
    }

    runtime, columns, _stickers, keychains = igor_model_runtime.build_feature_frame(
        payload, None, metadata
    )
    assert columns == metadata["feature_cols"]
    assert len(keychains) == 1
    for column in (
        "has_reference",
        "has_reference_anchor",
        "log_reference_predicted_price",
        "log_reference_keychain_price",
        "sticker_0_wear",
        "log_sticker_0_ref_price",
        "sticker_reference_safe_count",
        "keychain_0_id_cat",
        "keychain_0_pattern_cat",
        "log_keychain_0_ref_price",
        "keychain_reference_safe_count",
    ):
        expected = trained.loc[0, column]
        actual = runtime.loc[0, column]
        if isinstance(expected, str):
            assert actual == expected
        else:
            assert math.isclose(
                float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-9
            )


def test_single_sqlite_read_transaction_is_an_immutable_snapshot(tmp_path):
    path = tmp_path / "snapshot.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE values_table(value INTEGER)")
        conn.execute("INSERT INTO values_table VALUES (1)")

    with data_loading.open_read_snapshot(path) as snapshot:
        assert snapshot.execute("SELECT COUNT(*) FROM values_table").fetchone()[0] == 1
        with sqlite3.connect(path) as writer:
            writer.execute("INSERT INTO values_table VALUES (2)")
        assert snapshot.execute("SELECT COUNT(*) FROM values_table").fetchone()[0] == 1


def test_igor_source_excludes_knives_and_gloves_from_skin_training():
    connection = sqlite3.connect(":memory:")
    definitions = ", ".join(
        f'"{column}" {"INTEGER" if column == "price_cents" else "TEXT"}'
        for column in data_loading.SALES_COLUMN_NAMES
    )
    connection.execute(f"CREATE TABLE sales ({definitions})")
    connection.executemany(
        "INSERT INTO sales(listing_id, name, price_cents, state) VALUES (?, ?, ?, ?)",
        [
            ("skin", "AK-47 | Redline (Field-Tested)", 1000, "sold"),
            ("knife", "★ Karambit | Fade (Factory New)", 100000, "sold"),
            ("glove", "★ Sport Gloves | Vice (Field-Tested)", 50000, "sold"),
        ],
    )

    frame = data_loading.load_sales(conn=connection)

    assert frame["listing_id"].tolist() == ["skin"]


def test_cache_freshness_compares_source_and_contract(monkeypatch, tmp_path):
    cache_path = tmp_path / "features.pkl"
    metadata_path = tmp_path / "features.metadata.json"
    cache_path.write_bytes(b"immutable-cache")
    metadata_path.write_text(
        json.dumps(
                {
                    "cache_schema_version": build_cache.CACHE_SCHEMA_VERSION,
                    "item_kind": "weapon_skin",
                    "feature_contract_sha256": features.FEATURE_CONTRACT_SHA256,
                    "source_database": {
                        "logical_sha256": "db-snapshot",
                        "item_kind": "weapon_skin",
                        "item_kind_policy": "strict_catalog_or_known_name_v1",
                    },
                "database_changed_during_build": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(build_cache, "CACHE_PATH", cache_path)
    monkeypatch.setattr(build_cache, "CACHE_METADATA_PATH", metadata_path)
    monkeypatch.setattr(
        build_cache,
        "database_fingerprint",
        lambda _path: {"source_logical_sha256": "db-snapshot"},
    )
    assert build_cache.is_cache_current(tmp_path / "source.db")

    wrong_kind = json.loads(metadata_path.read_text(encoding="utf-8"))
    wrong_kind["source_database"]["item_kind"] = "glove"
    metadata_path.write_text(json.dumps(wrong_kind), encoding="utf-8")
    assert not build_cache.is_cache_current(tmp_path / "source.db")
    wrong_kind["source_database"]["item_kind"] = "weapon_skin"
    metadata_path.write_text(json.dumps(wrong_kind), encoding="utf-8")

    monkeypatch.setattr(
        build_cache,
        "database_fingerprint",
        lambda _path: {"source_logical_sha256": "newer-db"},
    )
    assert not build_cache.is_cache_current(tmp_path / "source.db")


def test_streaming_source_fingerprint_is_order_stable_and_content_sensitive(tmp_path):
    def create(path, sales_rows):
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE sales (
                    listing_id TEXT, sold_at TEXT, fetched_at TEXT,
                    price_cents INTEGER, state TEXT, name TEXT
                );
                CREATE TABLE sale_stickers (
                    listing_id TEXT, sticker_index INTEGER,
                    reference_price_cents INTEGER
                );
                CREATE TABLE sale_keychains (
                    listing_id TEXT, keychain_index INTEGER, price_cents INTEGER
                );
                """
            )
            conn.executemany("INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?)", sales_rows)
            conn.executemany(
                "INSERT INTO sale_stickers VALUES (?, ?, ?)",
                [("b", 0, 10), ("a", 0, 20)],
            )
            conn.executemany(
                "INSERT INTO sale_keychains VALUES (?, ?, ?)",
                [("b", 0, 5), ("a", 0, 6)],
            )

    rows = [
        (
            "a", "2026-01-01", "2026-01-02", 100, "sold",
            "AK-47 | Test (Field-Tested)",
        ),
        (
            "b", "2026-01-03", "2026-01-04", 200, "sold",
            "M4A4 | Test (Factory New)",
        ),
    ]
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    create(first_path, rows)
    create(second_path, list(reversed(rows)))

    def fingerprint(path):
        with data_loading.open_read_snapshot(path) as conn:
            return data_loading.stream_source_content_sha256(conn, chunk_size=1)

    before = fingerprint(first_path)
    assert before == fingerprint(second_path)

    with sqlite3.connect(first_path) as conn:
        conn.execute("UPDATE sales SET price_cents = 999 WHERE listing_id = 'a'")
        conn.execute(
            "UPDATE sale_stickers SET reference_price_cents = 888 "
            "WHERE listing_id = 'a'"
        )
        conn.execute(
            "UPDATE sale_keychains SET price_cents = 777 WHERE listing_id = 'a'"
        )
    assert before != fingerprint(first_path)


def test_igor_weapon_source_hash_ignores_glove_rows_and_children(tmp_path):
    path = tmp_path / "kind-isolated.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sales (
                listing_id TEXT, name TEXT, state TEXT, price_cents INTEGER,
                sold_at TEXT, fetched_at TEXT
            );
            CREATE TABLE sale_stickers (
                listing_id TEXT, sticker_index INTEGER,
                reference_price_cents INTEGER
            );
            CREATE TABLE sale_keychains (
                listing_id TEXT, keychain_index INTEGER, price_cents INTEGER
            );
            INSERT INTO sales VALUES (
                'skin', 'AK-47 | Test (Field-Tested)', 'sold', 100,
                '2026-01-01', '2026-01-02'
            );
            INSERT INTO sale_stickers VALUES ('skin', 0, 10);
            """
        )

    def fingerprint():
        with data_loading.open_read_snapshot(path) as conn:
            return data_loading.stream_source_content_sha256(conn, chunk_size=1)

    before = fingerprint()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?)",
            (
                "glove",
                "★ Sport Gloves | Vice (Field-Tested)",
                "sold",
                20_000,
                "2026-01-03",
                "2026-01-04",
            ),
        )
        conn.execute("INSERT INTO sale_stickers VALUES ('glove', 0, 999)")
        conn.execute("INSERT INTO sale_keychains VALUES ('glove', 0, 999)")

    assert fingerprint() == before
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE sale_stickers SET reference_price_cents=11 "
            "WHERE listing_id='skin'"
        )
    assert fingerprint() != before


def test_runtime_import_uses_case_correct_igor_package():
    assert igor_model_runtime.IGOR_DIR.name == "IGORMODEL"
    assert "IGORMODEL/features.py" in igor_model_runtime.igor_features.__file__


def test_v2_runtime_fails_closed_on_artifact_contract_drift():
    valid = {
        "artifact_version": 2,
        "feature_contract_version": 2,
        "feature_contract_sha256": features.FEATURE_CONTRACT_SHA256,
        "feature_cols": list(features.ALL_FEATURES),
        "cat_features": list(features.CAT_FEATURES),
    }
    igor_model_runtime.validate_metadata_contract(valid)

    mismatched = dict(valid, feature_contract_sha256="stale")
    with pytest.raises(RuntimeError, match="contract mismatch"):
        igor_model_runtime.validate_metadata_contract(mismatched)

    wrong_features = dict(valid, feature_cols=list(features.ALL_FEATURES[:-1]))
    with pytest.raises(RuntimeError, match="feature list"):
        igor_model_runtime.validate_metadata_contract(wrong_features)

    # Current production Igor/Nova metadata predates v2 hashes and remains
    # deliberately permissive for rollback safety.
    igor_model_runtime.validate_metadata_contract({"artifact_version": 1})


def test_shared_interval_contract_always_contains_point_and_quantiles():
    low, high = feature_contract.order_prediction_interval(2.0, 3.0, 4.0)
    assert float(low) == 2.0
    assert float(high) == 4.0

    crossed_low, crossed_high = feature_contract.order_prediction_interval(
        4.0, 5.0, 3.0
    )
    assert float(crossed_low) == 3.0
    assert float(crossed_high) == 5.0


def test_mixed_keychain_rows_use_same_categories_as_runtime():
    sales = pd.concat(
        [
            sale_frame(),
            sale_frame(
                listing_id="sale-without-charm",
                reference_predicted_price=10_000,
                reference_keychain_price=0,
            ),
        ],
        ignore_index=True,
    )
    raw = data_loading.assemble_dataset(
        sales, sticker_frame().iloc[0:0], keychain_frame()
    )
    trained = features.build_time_safe_features(raw)
    with_charm = trained.loc[trained["listing_id"] == "sale-1"].iloc[0]
    without_charm = trained.loc[
        trained["listing_id"] == "sale-without-charm"
    ].iloc[0]

    assert with_charm["keychain_0_id_cat"] == "501"
    assert with_charm["keychain_0_pattern_cat"] == "17"
    assert without_charm["keychain_0_id_cat"] == "-1"
    assert without_charm["keychain_0_pattern_cat"] == "-1"

    metadata = {
        "feature_contract_version": 2,
        "feature_cols": ["keychain_0_id_cat", "keychain_0_pattern_cat"],
        "cat_features": ["keychain_0_id_cat", "keychain_0_pattern_cat"],
        "seed_encoder_counts": {},
        "runtime_history": {"global_premium": 0.0},
    }
    runtime, *_ = igor_model_runtime.build_feature_frame(
        {
            "name": "AK-47 | Test (Field-Tested)",
            "reference_base_price": 10_000,
            "reference_predicted_price": 11_000,
            "reference_keychain_price": 1_000,
            "keychains": [
                {
                    "keychain_id": 501,
                    "name": "Charm | Test",
                    "pattern": 17,
                    "price_cents": 1_000,
                }
            ],
        },
        None,
        metadata,
    )
    assert runtime.loc[0, "keychain_0_id_cat"] == with_charm["keychain_0_id_cat"]
    assert (
        runtime.loc[0, "keychain_0_pattern_cat"]
        == with_charm["keychain_0_pattern_cat"]
    )


def test_missing_seed_and_float_use_same_train_runtime_sentinel():
    mixed_sales = pd.concat(
        [
            sale_frame(paint_seed=None, float_value=None),
            sale_frame(listing_id="complete", paint_seed=123, float_value=0.25),
        ],
        ignore_index=True,
    )
    raw = data_loading.assemble_dataset(
        mixed_sales, sticker_frame().iloc[0:0], keychain_frame().iloc[0:0]
    )
    trained = features.build_time_safe_features(raw)
    missing = trained.loc[trained["listing_id"] == "sale-1"].iloc[0]
    assert missing["paint_seed"] == -1
    assert missing["float_value"] == -1
    assert missing["skin_seed_key"] == "AK-47 | Test_-1"

    metadata = {
        "feature_contract_version": 2,
        "feature_cols": ["paint_seed", "float_value", "skin_seed_key"],
        "cat_features": ["skin_seed_key"],
        "seed_encoder_counts": {},
        "runtime_history": {"global_premium": 0.0},
    }
    runtime, *_ = igor_model_runtime.build_feature_frame(
        {
            "name": "AK-47 | Test (Field-Tested)",
            "reference_base_price": 10_000,
            "reference_predicted_price": 11_000,
        },
        None,
        metadata,
    )
    for column in metadata["feature_cols"]:
        assert runtime.loc[0, column] == missing[column]


def test_missing_rarity_and_wear_use_same_train_runtime_category():
    training_sale = sale_frame(
        name="AK-47 | Test",
        wear_name=None,
        item_rarity_name=None,
    )
    raw = data_loading.assemble_dataset(
        training_sale, sticker_frame().iloc[0:0], keychain_frame().iloc[0:0]
    )
    trained = features.build_time_safe_features(raw)
    assert trained.loc[0, "wear_name"] == "unknown"
    assert trained.loc[0, "item_rarity_name"] == "unknown"

    metadata = {
        "feature_contract_version": 2,
        "feature_cols": ["wear_name", "item_rarity_name"],
        "cat_features": ["wear_name", "item_rarity_name"],
        "seed_encoder_counts": {},
        "runtime_history": {"global_premium": 0.0},
    }
    runtime, *_ = igor_model_runtime.build_feature_frame(
        {
            "name": "AK-47 | Test",
            "reference_base_price": 10_000,
            "reference_predicted_price": 11_000,
        },
        None,
        metadata,
    )
    assert runtime.loc[0, "wear_name"] == trained.loc[0, "wear_name"]
    assert (
        runtime.loc[0, "item_rarity_name"]
        == trained.loc[0, "item_rarity_name"]
    )


def test_missing_identity_uses_shared_market_name_fallback():
    raw = data_loading.assemble_dataset(
        sale_frame(base_skin_name=None, wear_name=None),
        sticker_frame().iloc[0:0],
        keychain_frame().iloc[0:0],
    )
    trained = features.build_time_safe_features(raw)
    metadata = {
        "feature_contract_version": 2,
        "feature_cols": ["weapon_type", "wear_name"],
        "cat_features": ["weapon_type", "wear_name"],
        "seed_encoder_counts": {},
        "runtime_history": {"global_premium": 0.0},
    }
    runtime, *_ = igor_model_runtime.build_feature_frame(
        {
            "name": "AK-47 | Test (Field-Tested)",
            "reference_base_price": 10_000,
            "reference_predicted_price": 11_000,
        },
        None,
        metadata,
    )
    assert trained.loc[0, "weapon_type"] == "AK-47 | Test"
    assert trained.loc[0, "wear_name"] == "Field-Tested"
    assert runtime.loc[0, "weapon_type"] == trained.loc[0, "weapon_type"]
    assert runtime.loc[0, "wear_name"] == trained.loc[0, "wear_name"]


def test_fractional_integer_categories_share_missing_sentinel():
    raw = data_loading.assemble_dataset(
        sale_frame(paint_seed=7.5, paint_index=9.5),
        sticker_frame().iloc[0:0],
        keychain_frame().iloc[0:0],
    )
    raw["sticker_0_id"] = raw["sticker_0_id"].astype(float)
    raw["keychain_0_id"] = raw["keychain_0_id"].astype(float)
    raw["keychain_0_pattern"] = raw["keychain_0_pattern"].astype(float)
    raw.loc[0, "sticker_0_id"] = 42.5
    raw.loc[0, "keychain_0_id"] = 501.5
    raw.loc[0, "keychain_0_pattern"] = 17.5
    trained = features.build_time_safe_features(raw)
    metadata = {
        "feature_contract_version": 2,
        "feature_cols": [
            "paint_seed",
            "skin_seed_key",
            "paint_index_cat",
            "sticker_0_id_cat",
            "keychain_0_id_cat",
            "keychain_0_pattern_cat",
        ],
        "cat_features": [
            "skin_seed_key",
            "paint_index_cat",
            "sticker_0_id_cat",
            "keychain_0_id_cat",
            "keychain_0_pattern_cat",
        ],
        "seed_encoder_counts": {},
        "runtime_history": {"global_premium": 0.0},
    }
    runtime, *_ = igor_model_runtime.build_feature_frame(
        {
            "name": "AK-47 | Test (Field-Tested)",
            "reference_base_price": 10_000,
            "reference_predicted_price": 11_000,
            "paint_seed": 7.5,
            "paint_index": 9.5,
            "stickers": [{"sticker_id": 42.5, "name": "fractional"}],
            "keychains": [
                {"keychain_id": 501.5, "pattern": 17.5, "name": "fractional"}
            ],
        },
        None,
        metadata,
    )
    for column in metadata["feature_cols"]:
        assert runtime.loc[0, column] == trained.loc[0, column]
    assert runtime.loc[0, "paint_seed"] == -1
    assert runtime.loc[0, "sticker_0_id_cat"] == "-1"


def test_runtime_never_borrows_listing_specific_keychain_component(monkeypatch):
    monkeypatch.setattr(
        igor_model_runtime.price_model_runtime,
        "lookup_skin_defaults",
        lambda *_args, **_kwargs: {
            "base_skin_name": "AK-47 | Test",
            "wear_name": "Field-Tested",
            "reference_base_price": 156,
            "reference_predicted_price": 512,
            "reference_keychain_price": 356,
        },
    )
    payload = {
        "listing_id": "1006125637620076061",
        "name": "AK-47 | Test (Field-Tested)",
        "reference_base_price": 156,
        "reference_predicted_price": 156,
        "keychains": [],
    }

    row, _stickers, keychains = igor_model_runtime.build_raw_row_with_keychains(
        payload, object(), contract_version=2
    )
    assert keychains == []
    assert row["reference_keychain_price"] == 0
    assert row["reference_keychain_component_source"] == "none"

    frame = features.build_time_safe_features(pd.DataFrame([row]))
    assert frame.loc[0, "reference_predicted_price_filled"] == 156
    assert frame.loc[0, "has_reference_keychain_price"] == 0

    fallback_payload = dict(payload)
    fallback_payload.pop("reference_predicted_price")
    fallback_row, *_ = igor_model_runtime.build_raw_row_with_keychains(
        fallback_payload, object(), contract_version=2
    )
    assert fallback_row["reference_predicted_price"] == 156
    assert fallback_row["reference_keychain_price"] == 0

    legacy_row, *_ = igor_model_runtime.build_raw_row_with_keychains(
        fallback_payload, object(), contract_version=1
    )
    assert legacy_row["reference_predicted_price"] == 512
    assert legacy_row["reference_keychain_price"] == 356
    assert legacy_row["reference_keychain_component_source"] == "legacy_v1"


def test_missing_main_keychain_component_keeps_child_features_separate(monkeypatch):
    monkeypatch.setattr(
        igor_model_runtime.price_model_runtime,
        "lookup_skin_defaults",
        lambda *_args, **_kwargs: {},
    )
    training_sale = sale_frame(reference_keychain_price=0)
    training_raw = data_loading.assemble_dataset(
        training_sale, sticker_frame().iloc[0:0], keychain_frame()
    )
    trained = features.build_time_safe_features(training_raw)

    metadata = {
        "feature_contract_version": 2,
        "feature_cols": [
            "log_reference_predicted_price",
            "log_reference_keychain_price",
            "has_reference_keychain_price",
            "log_keychain_0_ref_price",
        ],
        "cat_features": [],
        "seed_encoder_counts": {},
        "runtime_history": {"global_premium": 0.0},
    }
    runtime, *_ = igor_model_runtime.build_feature_frame(
        {
            "name": "AK-47 | Test (Field-Tested)",
            "reference_base_price": 10_000,
            "reference_predicted_price": 11_000,
            "keychains": [
                {
                    "keychain_id": 501,
                    "name": "Charm | Test",
                    "pattern": 17,
                    "price_cents": 1_000,
                }
            ],
        },
        None,
        metadata,
    )

    for column in metadata["feature_cols"]:
        assert math.isclose(
            float(runtime.loc[0, column]),
            float(trained.loc[0, column]),
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
    assert runtime.loc[0, "has_reference_keychain_price"] == 0
    assert runtime.loc[0, "log_keychain_0_ref_price"] > 0


def test_incoherent_keychain_component_is_ignored_in_train_and_runtime(monkeypatch):
    monkeypatch.setattr(
        igor_model_runtime.price_model_runtime,
        "lookup_skin_defaults",
        lambda *_args, **_kwargs: {},
    )
    raw = data_loading.assemble_dataset(
        sale_frame(
            reference_base_price=1_000,
            reference_predicted_price=1_000,
            reference_keychain_price=1_000,
        ),
        sticker_frame().iloc[0:0],
        keychain_frame().iloc[0:0],
    )
    trained = features.build_time_safe_features(raw)
    metadata = {
        "feature_contract_version": 2,
        "feature_cols": [
            "log_reference_predicted_price",
            "log_reference_keychain_price",
            "has_reference_keychain_price",
        ],
        "cat_features": [],
        "seed_encoder_counts": {},
        "runtime_history": {"global_premium": 0.0},
    }
    runtime, *_ = igor_model_runtime.build_feature_frame(
        {
            "name": "AK-47 | Test (Field-Tested)",
            "reference_base_price": 1_000,
            "reference_predicted_price": 1_000,
            "reference_keychain_price": 1_000,
        },
        None,
        metadata,
    )
    for column in metadata["feature_cols"]:
        assert runtime.loc[0, column] == trained.loc[0, column]
    assert trained.loc[0, "reference_predicted_price_filled"] == 1_000
    assert trained.loc[0, "has_reference_keychain_price"] == 0


def test_fixed_split_keeps_test_untouched_and_strictly_newer():
    frame = pd.DataFrame(
        {
            "listing_id": [f"sale-{index}" for index in range(12)],
            "sold_dt": pd.date_range("2026-01-01", periods=12, tz="UTC"),
        }
    )
    train, validation, test, manifest = (
        igor_train.chronological_train_validation_test_split(
            frame,
            holdout_after="2026-01-09T00:00:00Z",
            validation_fraction=0.25,
            minimum_test_rows=1,
        )
    )

    assert set(train["listing_id"]).isdisjoint(validation["listing_id"])
    assert set(train["listing_id"]).isdisjoint(test["listing_id"])
    assert set(validation["listing_id"]).isdisjoint(test["listing_id"])
    assert test["sold_dt"].min() > pd.Timestamp(manifest["holdout_after"])
    assert manifest["test_untouched_until_final_evaluation"] is True


def test_frozen_history_features_do_not_use_validation_or_test_targets():
    train = pd.DataFrame(
        {
            "weapon_type": ["AK", "AK", "M4"],
            "log_premium": [0.1, 0.3, -0.2],
            "sold_dt": pd.date_range("2026-01-01", periods=3, tz="UTC"),
            "listing_id": ["a", "b", "c"],
        }
    )
    history = igor_train.fit_runtime_history(train)
    first = pd.DataFrame(
        {"weapon_type": ["AK", "unknown"], "log_premium": [99.0, -99.0]}
    )
    second = first.copy()
    second["log_premium"] = [-500.0, 500.0]

    igor_train.apply_frozen_runtime_history(first, history)
    igor_train.apply_frozen_runtime_history(second, history)

    assert (
        first["weapon_type_recent_premium"].tolist()
        == second["weapon_type_recent_premium"].tolist()
    )
    assert (
        first["skin_liquidity_cumcount"].tolist()
        == second["skin_liquidity_cumcount"].tolist()
    )


def test_history_is_invariant_within_identical_timestamps():
    frame = pd.DataFrame(
        {
            "weapon_type": ["AK", "AK", "AK", "AK"],
            "log_premium": [0.0, -10.0, 10.0, 3.0],
            "sold_dt": pd.to_datetime(
                [
                    "2026-01-01T00:00:00Z",
                    "2026-01-02T00:00:00Z",
                    "2026-01-02T00:00:00Z",
                    "2026-01-03T00:00:00Z",
                ]
            ),
            "listing_id": ["prior", "same-a", "same-b", "later"],
        }
    )

    def transformed(value):
        value = features.add_weapon_type_premium_trend(
            value, "log_premium", window=200, min_periods=1
        )
        value = features.add_liquidity_cumcount(value)
        return value.set_index("listing_id").sort_index()

    first = transformed(frame)
    permuted = transformed(frame.iloc[[0, 2, 1, 3]].reset_index(drop=True))
    pd.testing.assert_series_equal(
        first["weapon_type_recent_premium"],
        permuted["weapon_type_recent_premium"],
    )
    pd.testing.assert_series_equal(
        first["skin_liquidity_cumcount"],
        permuted["skin_liquidity_cumcount"],
    )
    assert first.loc["same-a", "weapon_type_recent_premium"] == 0.0
    assert first.loc["same-b", "weapon_type_recent_premium"] == 0.0
    assert first.loc["same-a", "skin_liquidity_cumcount"] == math.log1p(1)
    assert first.loc["same-b", "skin_liquidity_cumcount"] == math.log1p(1)

    cross_weapon = pd.DataFrame(
        {
            "weapon_type": ["AK", "M4"] + [f"prior-{index}" for index in range(8)]
            + ["AK", "M4"],
            "log_premium": [float(index) for index in range(10)] + [-10.0, 50.0],
            "sold_dt": pd.to_datetime(
                ["2026-01-01T00:00:00Z"] * 10
                + ["2026-01-02T00:00:00Z"] * 2
            ),
            "listing_id": [f"prior-{index}" for index in range(10)]
            + ["same-ak", "same-m4"],
        }
    )

    def global_fallback(value):
        return features.add_weapon_type_premium_trend(
            value, "log_premium", window=200, min_periods=10
        ).set_index("listing_id").sort_index()

    cross_first = global_fallback(cross_weapon)
    cross_permuted = global_fallback(
        cross_weapon.iloc[list(reversed(range(10))) + [11, 10]].reset_index(drop=True)
    )
    pd.testing.assert_series_equal(
        cross_first["weapon_type_recent_premium"],
        cross_permuted["weapon_type_recent_premium"],
    )
    assert cross_first.loc["same-ak", "weapon_type_recent_premium"] == 4.5
    assert cross_first.loc["same-m4", "weapon_type_recent_premium"] == 4.5


def test_runtime_valid_training_rows_exclude_mixed_absolute_targets():
    raw = data_loading.assemble_dataset(sale_frame(), sticker_frame(), keychain_frame())
    valid = features.build_time_safe_features(raw)
    invalid_raw = data_loading.assemble_dataset(
        sale_frame(
            listing_id="unsafe",
            reference_last_updated="2026-08-02T00:00:00Z",
        ),
        sticker_frame().iloc[0:0],
        keychain_frame().iloc[0:0],
    )
    invalid = features.build_time_safe_features(invalid_raw)
    frame, counts = igor_train.prepare_runtime_valid_rows(
        pd.concat([valid, invalid], ignore_index=True)
    )

    assert frame["listing_id"].tolist() == ["sale-1"]
    assert counts["anchor_invalid"] == 1
    assert counts["runtime_anchor_valid_rows"] == 1


def test_production_comparison_uses_installed_frozen_metadata(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for filename in (
        "price_model_point.cbm",
        "price_model_q15.cbm",
        "price_model_q85.cbm",
    ):
        (artifacts / filename).write_bytes(b"model")
    metadata = {
        "artifact_version": 2,
        "feature_contract_sha256": features.FEATURE_CONTRACT_SHA256,
        "feature_cols": list(features.ALL_FEATURES),
        "cat_features": list(features.CAT_FEATURES),
        "seed_encoder_counts": {"AK_7": 9},
        "runtime_history": {
            "premium_by_weapon": {"AK": 0.25},
            "count_by_weapon": {"AK": 99},
            "global_premium": 0.0,
        },
        "prediction_calibration": {
            "premium_scale": 1.0,
            "center_offset": 0.0,
            "low_offset": 0.0,
            "high_offset": 0.0,
        },
    }
    with (artifacts / "feature_metadata.pkl").open("wb") as handle:
        pickle.dump(metadata, handle)
    (artifacts / "igor_metrics.json").write_text(
        json.dumps({"trained_at": "2026-08-10T00:00:00+00:00"}),
        encoding="utf-8",
    )

    class FakeModel:
        def load_model(self, _path):
            return None

        def predict(self, frame):
            assert frame["seed_popularity"].iloc[0] == pytest.approx(math.log1p(9))
            assert frame["weapon_type_recent_premium"].iloc[0] == 0.25
            assert frame["skin_liquidity_cumcount"].iloc[0] == pytest.approx(
                math.log1p(99)
            )
            return pd.Series([0.0]).to_numpy()

    row = {
        column: ("none" if column in features.CAT_FEATURES else 0.0)
        for column in features.ALL_FEATURES
    }
    row.update(
        {
            "skin_seed_key": "AK_7",
            "weapon_type": "AK",
            "log_reference_predicted_price": math.log1p(1_000),
            "price_cents": 1_000,
        }
    )
    monkeypatch.setattr(igor_train, "PRODUCTION_ARTIFACT_DIR", artifacts)
    monkeypatch.setattr(igor_train, "CatBoostRegressor", FakeModel)

    result = igor_train.evaluate_production_bundle(pd.DataFrame([row]))

    assert result["trained_at"] == "2026-08-10T00:00:00+00:00"
    assert result["point"][0] == pytest.approx(1_000)
    assert result["raw_low"][0] <= result["point"][0] <= result["raw_high"][0]
