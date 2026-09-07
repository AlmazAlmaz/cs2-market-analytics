import json
import sqlite3
import stat

import pytest

import deal_scanner


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    return conn


def scanner_config(**overrides):
    config = dict(deal_scanner.DEFAULT_CONFIG)
    config.update(overrides)
    return deal_scanner.validate_config(config)


def scanner_target_control(**profile_overrides):
    defaults = {
        "sticker_guns": True,
        "liquid_under_10": True,
        "knives": False,
        "gloves": False,
    }
    return {
        "schema_version": 1,
        "revision": 3,
        "updated_at": "2026-08-24T00:00:00+00:00",
        "profiles": {
            name: {
                "enabled": profile_overrides.get(name, {}).get(
                    "enabled", defaults[name]
                ),
                "selection_mode": profile_overrides.get(name, {}).get(
                    "selection_mode", "recommended"
                ),
                "include": profile_overrides.get(name, {}).get("include", []),
                "exclude": profile_overrides.get(name, {}).get("exclude", []),
            }
            for name in defaults
        },
    }


def test_legacy_interval_uses_same_bounds_as_persistent_control():
    with pytest.raises(ValueError, match="scan_interval_seconds must be between 60 and 86400"):
        scanner_config(scan_interval_seconds=59)

    assert scanner_config(scan_interval_seconds=60)["scan_interval_seconds"] == 60


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"liquid_skins_per_cycle": 6}, "liquid_skins_per_cycle must be between 1 and 5"),
        (
            {"listing_pages_per_liquid_skin": 3},
            "listing_pages_per_liquid_skin must be between 1 and 2",
        ),
        (
            {"liquid_skin_max_price_cents": 1001},
            "liquid_skin_max_price_cents must be between 1 and 1000",
        ),
        ({"knives_per_cycle": 3}, "knives_per_cycle must be between 1 and 2"),
        (
            {"listings_per_knife": 21},
            "listings_per_knife must be between 1 and 20",
        ),
        (
            {"listing_pages_per_knife": 3},
            "listing_pages_per_knife must be between 1 and 2",
        ),
        (
            {"max_knife_targets": 101},
            "max_knife_targets must be between 1 and 100",
        ),
        (
            {"knife_rediscovery_cycles": 0},
            "knife_rediscovery_cycles must be between 1 and 1440",
        ),
        ({"gloves_per_cycle": 3}, "gloves_per_cycle must be between 1 and 2"),
        (
            {"listings_per_glove": 21},
            "listings_per_glove must be between 1 and 20",
        ),
        (
            {"listing_pages_per_glove": 3},
            "listing_pages_per_glove must be between 1 and 2",
        ),
        (
            {"max_glove_targets": 101},
            "max_glove_targets must be between 1 and 100",
        ),
    ],
)
def test_liquid_skin_profile_config_is_explicitly_bounded(override, message):
    with pytest.raises(ValueError, match=message):
        scanner_config(**override)


def test_liquid_skin_discovery_deduplicates_and_ranks_schedule_with_catalog(
    tmp_path,
):
    catalog_path = tmp_path / "skins.json"
    catalog_path.write_text(
        json.dumps(
            [
                {
                    "market_hash_name": "AK-47 | Alpha (Field-Tested)",
                    "base_price_usd": 4.0,
                    "quantity_on_market": 300,
                },
                {
                    "market_hash_name": "AK-47 | Alpha (Field-Tested)",
                    "base_price_usd": 5.0,
                    "quantity_on_market": 500,
                },
                {
                    "market_hash_name": "M4A1-S | Beta (Minimal Wear)",
                    "base_price_usd": 9.5,
                    "quantity_on_market": 1000,
                },
                {
                    "market_hash_name": "MP9 | Unknown (Factory New)",
                    "base_price_usd": 1.0,
                    "quantity_on_market": 2000,
                },
                {
                    "market_hash_name": "P250 | Slow (Field-Tested)",
                    "base_price_usd": 2.0,
                    "quantity_on_market": 900,
                },
                {
                    "market_hash_name": "AWP | Too Expensive (Field-Tested)",
                    "base_price_usd": 10.01,
                    "quantity_on_market": 5000,
                },
                {
                    "market_hash_name": "★ Sport Gloves | Vice (Field-Tested)",
                    "base_price_usd": 5.0,
                    "quantity_on_market": 5000,
                },
                {
                    "market_hash_name": "Sticker | Crown (Foil)",
                    "base_price_usd": 2.0,
                    "quantity_on_market": 5000,
                },
            ]
        ),
        encoding="utf-8",
    )
    conn = memory_db()
    conn.execute(
        """
        CREATE TABLE skin_schedule (
            market_hash_name TEXT PRIMARY KEY,
            estimated_sales_per_day REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO skin_schedule VALUES (?, ?)",
        [
            ("AK-47 | Alpha (Field-Tested)", 10.0),
            ("M4A1-S | Beta (Minimal Wear)", 2.0),
            ("P250 | Slow (Field-Tested)", 0.25),
        ],
    )

    targets = deal_scanner.discover_liquid_skin_targets(
        conn,
        scanner_config(
            liquid_skin_catalog_path=str(catalog_path),
            liquid_skin_min_quantity=100,
            liquid_skin_min_estimated_sales_per_day=1.0,
            max_liquid_skin_targets=10,
        ),
    )

    assert [target["market_hash_name"] for target in targets] == [
        "AK-47 | Alpha (Field-Tested)",
        "M4A1-S | Beta (Minimal Wear)",
    ]
    assert targets[0]["quantity_on_market"] == 500
    assert targets[0]["base_price_cents"] == 500
    assert all(target["source_type"] == deal_scanner.SOURCE_LIQUID_SKIN for target in targets)
    assert all(target["target_key"].startswith("liquid_skin:") for target in targets)


def test_knife_discovery_is_opt_in_local_catalog_only_and_velocity_ranked():
    conn = memory_db()
    conn.execute(
        """
        CREATE TABLE cs2_item_catalog (
            market_hash_name TEXT PRIMARY KEY,
            item_kind TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO cs2_item_catalog VALUES (?, ?)",
        [
            ("★ Bayonet | Slow (Field-Tested)", "knife"),
            ("★ Karambit | Fast (Factory New)", "knife"),
            ("★ Butterfly Knife | Unknown (Minimal Wear)", "knife"),
            ("AK-47 | Not A Knife (Field-Tested)", "weapon_skin"),
        ],
    )
    conn.execute(
        """
        CREATE TABLE skin_schedule (
            market_hash_name TEXT PRIMARY KEY,
            estimated_sales_per_day REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO skin_schedule VALUES (?, ?)",
        [
            ("★ Bayonet | Slow (Field-Tested)", 0.2),
            ("★ Karambit | Fast (Factory New)", 4.5),
        ],
    )

    assert deal_scanner.discover_knife_targets(conn, scanner_config()) == []

    targets = deal_scanner.discover_knife_targets(
        conn,
        scanner_config(knives_enabled=True, max_knife_targets=2),
    )

    assert [target["market_hash_name"] for target in targets] == [
        "★ Karambit | Fast (Factory New)",
        "★ Bayonet | Slow (Field-Tested)",
        "★ Butterfly Knife | Unknown (Minimal Wear)",
    ]
    assert all(target["source_type"] == deal_scanner.SOURCE_KNIFE for target in targets)
    assert all(target["target_key"].startswith("knife:") for target in targets)

    state = deal_scanner.load_knife_rotation_state(conn)
    window = deal_scanner.refresh_knife_rotation(
        conn,
        scanner_config(knives_enabled=True, max_knife_targets=2),
        targets,
        state,
        rerank=True,
    )
    assert [target["market_hash_name"] for target in window] == [
        "★ Bayonet | Slow (Field-Tested)",
        "★ Butterfly Knife | Unknown (Minimal Wear)",
    ]


def test_glove_discovery_uses_exact_catalog_and_custom_selection_only():
    conn = memory_db()
    conn.execute(
        "CREATE TABLE cs2_item_catalog "
        "(market_hash_name TEXT PRIMARY KEY, item_kind TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO cs2_item_catalog VALUES (?, ?)",
        [
            ("★ Sport Gloves | Vice (Field-Tested)", "glove"),
            ("★ Driver Gloves | King Snake (Minimal Wear)", "glove"),
            ("★ Karambit | Doppler (Factory New)", "knife"),
        ],
    )
    chosen = "★ Sport Gloves | Vice (Field-Tested)"
    control = scanner_target_control(
        gloves={
            "enabled": True,
            "selection_mode": "custom",
            "include": [f"glove:{chosen}"],
        }
    )

    targets = deal_scanner.discover_glove_targets(
        conn,
        scanner_config(gloves_enabled=False),
        control,
    )

    assert [target["market_hash_name"] for target in targets] == [chosen]
    assert targets[0]["target_key"] == f"glove:{chosen}"


def test_glove_scan_fails_closed_before_network_without_dedicated_model(monkeypatch):
    conn = memory_db()
    name = "★ Sport Gloves | Vice (Field-Tested)"
    conn.execute(
        "CREATE TABLE cs2_item_catalog "
        "(market_hash_name TEXT PRIMARY KEY, item_kind TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO cs2_item_catalog VALUES (?, 'glove')", (name,))

    def unavailable():
        raise deal_scanner.glove_model_runtime.GloveModelUnavailable("not trained")

    monkeypatch.setattr(deal_scanner.glove_model_runtime, "load_model", unavailable)
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings",
        lambda **_kwargs: pytest.fail("unavailable glove model must spend no quota"),
    )

    returned, saved, meta, model_saved = deal_scanner.scan_glove(
        conn,
        {"market_hash_name": name},
        scanner_config(gloves_enabled=True),
    )

    assert (returned, saved) == (0, 0)
    assert meta["model_name"] == "gloves"
    assert meta["model_status"] == "unavailable"
    assert model_saved == {"gloves": 0}
    log = conn.execute(
        "SELECT source_type, target_key, error FROM model_deal_scans"
    ).fetchone()
    assert tuple(log) == ("glove", f"glove:{name}", "glove_model_unavailable")


def test_glove_scan_uses_exact_client_and_dedicated_model_only(monkeypatch):
    conn = memory_db()
    name = "★ Sport Gloves | Vice (Field-Tested)"
    conn.execute(
        "CREATE TABLE cs2_item_catalog "
        "(market_hash_name TEXT PRIMARY KEY, item_kind TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO cs2_item_catalog VALUES (?, 'glove')", (name,))
    monkeypatch.setattr(
        deal_scanner.glove_model_runtime,
        "load_model",
        lambda: {"model_name": "gloves", "item_kind": "glove"},
    )
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings",
        lambda **_kwargs: (
            [{"id": "glove-listing"}],
            None,
            {},
            "https://example.invalid/gloves",
        ),
    )
    monkeypatch.setattr(
        deal_scanner,
        "normalize_listing_to_deal_payload",
        lambda *_args: {
            "listing_id": "glove-listing",
            "deal_payload": {
                "listing_id": "glove-listing",
                "name": name,
                "buy_price_cents": 50_000,
                "listing_type": "buy_now",
                "stickers": [],
            },
        },
    )
    for model_name in ("almaz", "igor", "nova"):
        monkeypatch.setitem(
            deal_scanner.MODEL_RUNNERS,
            model_name,
            lambda *_args, model_name=model_name: pytest.fail(
                f"glove reached {model_name}"
            ),
        )
    monkeypatch.setattr(
        deal_scanner.knife_model_runtime,
        "predict_deal",
        lambda *_args: pytest.fail("glove reached knife model"),
    )
    calls = []
    monkeypatch.setattr(
        deal_scanner.glove_model_runtime,
        "predict_deal",
        lambda payload, _conn: calls.append(payload["listing_id"])
        or {
            "model_name": "gloves",
            "item_kind": "glove",
            "verdict": "good",
            "name": payload["name"],
            "buy_price_cents": payload["buy_price_cents"],
            "predicted_price_cents": 60_000,
            "profit_cents": 7_600,
            "roi_percent": 15.2,
            "liquidity_score": 35,
        },
    )

    returned, saved, _meta, model_saved = deal_scanner.scan_glove(
        conn,
        {"market_hash_name": name},
        scanner_config(gloves_enabled=True),
    )

    assert (returned, saved, model_saved) == (1, 1, {"gloves": 1})
    assert calls == ["glove-listing"]
    assert conn.execute(
        "SELECT model_name FROM model_deal_predictions WHERE listing_id=?",
        ("glove-listing",),
    ).fetchone()[0] == "gloves"


def test_liquid_profile_rejects_non_weapon_target_before_network(monkeypatch):
    conn = memory_db()
    name = "★ Driver Gloves | King Snake (Minimal Wear)"
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_skin",
        lambda *_args, **_kwargs: pytest.fail(
            "non-weapon liquid target must spend no quota"
        ),
    )

    returned, saved, meta, model_saved = deal_scanner.scan_liquid_skin(
        conn,
        {"market_hash_name": name},
        scanner_config(enabled_models=["almaz", "igor", "nova"]),
    )

    assert (returned, saved) == (0, 0)
    assert meta["model_status"] == "invalid_item_kind"
    assert meta["item_kind"] == "glove"
    assert model_saved == {"almaz": 0, "igor": 0, "nova": 0}


def test_target_preview_reports_exact_selection_and_bounded_budget(monkeypatch):
    conn = memory_db()
    conn.execute(
        "CREATE TABLE cs2_item_catalog "
        "(market_hash_name TEXT PRIMARY KEY, item_kind TEXT NOT NULL)"
    )
    names = [
        "★ Sport Gloves | Vice (Field-Tested)",
        "★ Driver Gloves | King Snake (Minimal Wear)",
    ]
    conn.executemany(
        "INSERT INTO cs2_item_catalog VALUES (?, 'glove')",
        [(name,) for name in names],
    )
    control = scanner_target_control(gloves={"enabled": True})
    monkeypatch.setattr(
        deal_scanner,
        "_glove_model_scan_status",
        lambda: {
            "model_name": "gloves",
            "model_available": False,
            "model_status": "unavailable",
        },
    )

    payload = deal_scanner.build_scanner_target_preview(
        conn,
        "gloves",
        config=scanner_config(gloves_enabled=True),
        target_control=control,
        search="Vice",
        limit=20,
    )

    assert payload["profile"] == "gloves"
    assert payload["selection"]["eligible_count"] == 2
    assert payload["selection"]["selected_count"] == 2
    assert [item["name"] for item in payload["items"]] == [names[0]]
    assert payload["budget"] == {
        "targets_per_cycle": 1,
        "listings_per_target": 10,
        "pages_per_target": 1,
        "max_requests_per_cycle": 1,
    }
    assert payload["blocked_reason"] == "dedicated_glove_model_unavailable"


def test_knife_rotation_eventually_covers_catalog_larger_than_window():
    conn = memory_db()
    conn.execute(
        "CREATE TABLE cs2_item_catalog "
        "(market_hash_name TEXT PRIMARY KEY, item_kind TEXT NOT NULL)"
    )
    names = [f"★ Test Knife {index:02d} | Finish (Factory New)" for index in range(31)]
    conn.executemany(
        "INSERT INTO cs2_item_catalog VALUES (?, 'knife')",
        [(name,) for name in names],
    )
    config = scanner_config(knives_enabled=True, max_knife_targets=24)
    targets = deal_scanner.discover_knife_targets(conn, config)
    state = deal_scanner.load_knife_rotation_state(conn)

    first_window = deal_scanner.refresh_knife_rotation(
        conn, config, targets, state, rerank=True
    )
    assert len(targets) == 31
    assert len(first_window) == 24
    for target in first_window:
        deal_scanner.advance_knife_rotation(conn, state, target["target_key"])

    second_window = deal_scanner.refresh_knife_rotation(
        conn, config, targets, state
    )
    first_names = {target["market_hash_name"] for target in first_window}
    new_names = [
        target["market_hash_name"]
        for target in second_window
        if target["market_hash_name"] not in first_names
    ]

    assert len(second_window) == 7
    assert new_names == names[24:]
    assert first_names | set(new_names) == set(names)


def test_knife_rotation_resumes_pending_window_after_restart_and_reranks(tmp_path):
    database_path = tmp_path / "scanner.db"
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    conn.execute(
        "CREATE TABLE cs2_item_catalog "
        "(market_hash_name TEXT PRIMARY KEY, item_kind TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE skin_schedule "
        "(market_hash_name TEXT PRIMARY KEY, estimated_sales_per_day REAL)"
    )
    names = [f"★ Restart Knife {index} | Finish (Factory New)" for index in range(5)]
    conn.executemany(
        "INSERT INTO cs2_item_catalog VALUES (?, 'knife')",
        [(name,) for name in names],
    )
    conn.executemany(
        "INSERT INTO skin_schedule VALUES (?, ?)",
        [(name, float(5 - index)) for index, name in enumerate(names)],
    )
    conn.commit()
    config = scanner_config(knives_enabled=True, max_knife_targets=5)
    targets = deal_scanner.discover_knife_targets(conn, config)
    state = deal_scanner.load_knife_rotation_state(conn)
    window = deal_scanner.refresh_knife_rotation(
        conn, config, targets, state, rerank=True
    )
    consumed_key = window[0]["target_key"]
    deal_scanner.advance_knife_rotation(conn, state, consumed_key)
    expected_pending = list(state["pending_target_keys"])
    conn.close()

    restarted = sqlite3.connect(database_path)
    restarted.row_factory = sqlite3.Row
    loaded = deal_scanner.load_knife_rotation_state(restarted)
    assert loaded["pending_target_keys"] == expected_pending
    assert consumed_key not in loaded["pending_target_keys"]

    restarted.execute(
        "UPDATE skin_schedule SET estimated_sales_per_day = 100 WHERE market_hash_name = ?",
        (names[-1],),
    )
    refreshed_targets = deal_scanner.discover_knife_targets(restarted, config)
    reranked = deal_scanner.refresh_knife_rotation(
        restarted, config, refreshed_targets, loaded, rerank=True
    )

    assert reranked[0]["market_hash_name"] == names[-1]
    assert {target["target_key"] for target in reranked} == set(expected_pending)


def test_scanner_instance_lock_rejects_a_second_owner(tmp_path):
    lock_path = tmp_path / "deal_scanner.lock"

    with deal_scanner.scanner_instance_lock(lock_path):
        assert lock_path.read_text(encoding="utf-8") == str(deal_scanner.os.getpid())
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        with (
            pytest.raises(RuntimeError, match="deal_scanner is already running"),
            deal_scanner.scanner_instance_lock(lock_path),
        ):
            pass

    with deal_scanner.scanner_instance_lock(lock_path):
        pass


def insert_prediction(
    conn,
    listing_id,
    model_name="almaz",
    verdict="good",
    roi=10,
    profit=200,
    liquidity=30,
):
    conn.execute(
        """
        INSERT INTO model_deal_predictions (
            listing_id, model_name, first_seen_at, last_seen_at, scan_count,
            buy_price_cents, predicted_price_cents, profit_cents, roi_percent,
            liquidity_score, verdict, deal_payload_json, prediction_json
        ) VALUES (?, ?, ?, ?, 1, 1000, 1300, ?, ?, ?, ?, '{}', '{}')
        """,
        (
            listing_id,
            model_name,
            "2026-08-10T00:00:00+00:00",
            "2026-08-10T00:00:00+00:00",
            profit,
            roi,
            liquidity,
            verdict,
        ),
    )


def test_unknown_listing_never_reaches_a_model_and_invalidates_old_signals(
    monkeypatch,
):
    conn = memory_db()
    listing_id = "unknown-item"
    for model_name in ("almaz", "igor", "nova", "knives", "gloves"):
        insert_prediction(conn, listing_id, model_name=model_name)
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        lambda *_args, **_kwargs: (
            [{"id": listing_id}],
            None,
            {},
            "https://example.invalid/sticker",
        ),
    )
    monkeypatch.setattr(
        deal_scanner,
        "normalize_listing_to_deal_payload",
        lambda *_args: {
            "listing_id": listing_id,
            "deal_payload": {
                "listing_id": listing_id,
                "name": "Sticker | Crown (Foil)",
                "buy_price_cents": 1_000,
                "listing_type": "buy_now",
                "stickers": [],
            },
        },
    )
    for model_name in ("almaz", "igor", "nova"):
        monkeypatch.setitem(
            deal_scanner.MODEL_RUNNERS,
            model_name,
            lambda *_args, model_name=model_name: pytest.fail(
                f"unknown item reached {model_name}"
            ),
        )
    monkeypatch.setattr(
        deal_scanner.knife_model_runtime,
        "predict_deal",
        lambda *_args: pytest.fail("unknown item reached knife model"),
    )
    monkeypatch.setattr(
        deal_scanner.glove_model_runtime,
        "predict_deal",
        lambda *_args: pytest.fail("unknown item reached glove model"),
    )

    returned, saved, _meta, _model_saved = deal_scanner.scan_sticker(
        conn,
        {"sticker_id": 77, "name": "Sticker | Route Guard"},
        scanner_config(enabled_models=["almaz", "igor", "nova"]),
    )

    assert (returned, saved) == (1, 0)
    assert dict(
        conn.execute(
            "SELECT model_name, verdict FROM model_deal_predictions "
            "WHERE listing_id=? ORDER BY model_name",
            (listing_id,),
        ).fetchall()
    ) == {
        "almaz": "error",
        "gloves": "error",
        "igor": "error",
        "knives": "error",
        "nova": "error",
    }


def test_policy_filters_below_threshold_and_disallowed_visible_verdicts():
    conn = memory_db()
    insert_prediction(conn, "low-roi", verdict="good", roi=7)
    insert_prediction(conn, "not-allowed", verdict="watch", roi=12)
    insert_prediction(conn, "eligible", verdict="good", roi=12)
    conn.execute(
        """
        INSERT INTO model_deal_candidates (
            listing_id, first_seen_at, last_seen_at, roi_percent, profit_cents,
            liquidity_score, verdict
        ) VALUES ('low-roi', ?, ?, 7, 200, 30, 'good')
        """,
        ("2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00"),
    )
    config = scanner_config(save_verdicts=["good"])

    result = deal_scanner.enforce_deal_policy(conn, config)

    verdicts = dict(
        conn.execute(
            "SELECT listing_id, verdict FROM model_deal_predictions ORDER BY listing_id"
        )
    )
    assert result == {"predictions_filtered": 2, "candidates_removed": 1}
    assert verdicts == {
        "eligible": "good",
        "low-roi": "filtered",
        "not-allowed": "filtered",
    }
    assert conn.execute("SELECT COUNT(*) FROM model_deal_candidates").fetchone()[0] == 0


def test_filtered_snapshot_keeps_original_model_verdict_for_audit():
    prediction = {
        "verdict": "watch",
        "roi_percent": 5,
        "profit_cents": 50,
        "liquidity_score": 10,
    }

    stored, eligible = deal_scanner.prediction_for_storage(
        prediction,
        scanner_config(),
    )

    assert not eligible
    assert stored["verdict"] == "filtered"
    assert stored["scanner_original_verdict"] == "watch"
    assert set(stored["scanner_filter_reasons"]) == {
        "roi_percent",
        "profit_cents",
        "liquidity_score",
    }
    assert prediction["verdict"] == "watch"


def test_fresh_model_error_invalidates_old_positive_signal(monkeypatch):
    conn = memory_db()
    insert_prediction(conn, "listing-1", model_name="igor", verdict="good")
    conn.execute(
        """
        UPDATE model_deal_predictions
        SET low_price_cents = 1100,
            high_price_cents = 1500,
            net_expected_cents = 1248,
            historical_sales_count = 99,
            sales_per_day = 2.5,
            pricing_method = 'old-model'
        WHERE listing_id = 'listing-1' AND model_name = 'igor'
        """
    )
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        lambda *_args, **_kwargs: (
            [{"id": "listing-1"}],
            None,
            {"key_file": "test-key", "rate_remaining": "100"},
            "https://example.invalid/listings",
        ),
    )
    monkeypatch.setattr(
        deal_scanner,
        "normalize_listing_to_deal_payload",
        lambda *_args: {
            "listing_id": "listing-1",
            "deal_payload": {
                "listing_id": "listing-1",
                "name": "AK-47 | Test (Field-Tested)",
                "buy_price_cents": 1000,
            },
        },
    )

    def broken_model(_payload, _conn):
        raise RuntimeError("evaluation failed")

    monkeypatch.setitem(deal_scanner.MODEL_RUNNERS, "igor", broken_model)
    returned, saved, _meta, model_saved = deal_scanner.scan_sticker(
        conn,
        {"sticker_id": 1, "name": "Sticker | Test"},
        scanner_config(enabled_models=["igor"]),
    )

    row = conn.execute(
        """
        SELECT verdict, scan_count, predicted_price_cents, low_price_cents,
            high_price_cents, net_expected_cents, profit_cents, roi_percent,
            liquidity_score, historical_sales_count, sales_per_day,
            pricing_method, prediction_json
        FROM model_deal_predictions
        WHERE listing_id = 'listing-1'
        """
    ).fetchone()
    assert (returned, saved, model_saved) == (1, 0, {"igor": 0})
    assert row["verdict"] == "error"
    assert row["scan_count"] == 2
    assert all(
        row[field] is None
        for field in (
            "predicted_price_cents",
            "low_price_cents",
            "high_price_cents",
            "net_expected_cents",
            "profit_cents",
            "roi_percent",
            "liquidity_score",
            "historical_sales_count",
            "sales_per_day",
            "pricing_method",
        )
    )
    assert row["prediction_json"] == "{}"
    assert "evaluation failed" in conn.execute(
        "SELECT error FROM model_deal_scans ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]


def test_model_and_legacy_candidate_write_is_atomic(monkeypatch):
    conn = memory_db()
    insert_prediction(conn, "listing-1", model_name="almaz", verdict="good")
    listing_data = {
        "listing_id": "listing-1",
        "deal_payload": {
            "listing_id": "listing-1",
            "name": "AK-47 | Test (Field-Tested)",
            "buy_price_cents": 1000,
            "stickers": [],
        },
    }
    prediction = {
        "verdict": "good",
        "buy_price_cents": 1000,
        "predicted_price_cents": 9999,
        "profit_cents": 500,
        "roi_percent": 50,
        "liquidity_score": 50,
    }

    def fail_legacy_write(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated candidate failure")

    monkeypatch.setattr(deal_scanner, "upsert_candidate", fail_legacy_write)
    with pytest.raises(sqlite3.OperationalError, match="simulated candidate failure"):
        deal_scanner.persist_model_result(
            conn,
            listing_data,
            prediction,
            {"sticker_id": 1, "name": "Sticker | Test"},
            {},
            "almaz",
            True,
        )

    row = conn.execute(
        """
        SELECT predicted_price_cents, scan_count
        FROM model_deal_predictions
        WHERE listing_id = 'listing-1' AND model_name = 'almaz'
        """
    ).fetchone()
    assert row["predicted_price_cents"] == 1300
    assert row["scan_count"] == 1


def test_actionable_deal_index_is_created():
    conn = memory_db()
    index_names = {row[1] for row in conn.execute("PRAGMA index_list(model_deal_predictions)")}
    assert "idx_model_deal_predictions_actionable_seen" in index_names


def test_stale_cleanup_handles_legacy_null_timestamps():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE model_deal_predictions (verdict TEXT, last_seen_at TEXT)"
    )
    conn.execute("CREATE TABLE model_deal_candidates (last_seen_at TEXT)")
    conn.execute(
        "INSERT INTO model_deal_predictions (verdict, last_seen_at) VALUES ('bad', NULL)"
    )
    conn.execute(
        "INSERT INTO model_deal_predictions (verdict, last_seen_at) VALUES ('expired', NULL)"
    )
    conn.execute("INSERT INTO model_deal_candidates (last_seen_at) VALUES (NULL)")

    expired = deal_scanner.expire_stale_predictions(conn, 24)

    assert expired == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM model_deal_predictions WHERE verdict = 'expired'"
    ).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM model_deal_candidates").fetchone()[0] == 0


def test_repricing_does_not_fake_a_fresh_listing_observation():
    conn = memory_db()
    insert_prediction(conn, "listing-1", model_name="igor", verdict="good")
    original = conn.execute(
        """
        SELECT last_seen_at, scan_count
        FROM model_deal_predictions
        WHERE listing_id = 'listing-1' AND model_name = 'igor'
        """
    ).fetchone()
    listing_data = {
        "listing_id": "listing-1",
        "deal_payload": {
            "listing_id": "listing-1",
            "name": "AK-47 | Test (Field-Tested)",
            "buy_price_cents": 1000,
            "stickers": [],
        },
    }

    deal_scanner.upsert_model_prediction(
        conn,
        listing_data,
        {
            "verdict": "good",
            "buy_price_cents": 1000,
            "predicted_price_cents": 1500,
            "profit_cents": 300,
            "roi_percent": 30,
            "liquidity_score": 40,
        },
        {"sticker_id": 1, "name": "Sticker | Test"},
        {},
        "igor",
        refresh_observation=False,
        observed_at="2099-01-01T00:00:00+00:00",
    )

    repriced = conn.execute(
        """
        SELECT last_seen_at, scan_count, predicted_price_cents
        FROM model_deal_predictions
        WHERE listing_id = 'listing-1' AND model_name = 'igor'
        """
    ).fetchone()
    assert repriced["last_seen_at"] == original["last_seen_at"]
    assert repriced["scan_count"] == original["scan_count"]
    assert repriced["predicted_price_cents"] == 1500


def test_scan_sticker_defers_writes_until_all_model_work_is_ready(monkeypatch):
    conn = memory_db()
    events = []
    listings = [{"id": "listing-1"}, {"id": "listing-2"}]
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        lambda *_args, **_kwargs: (
            listings,
            None,
            {"key_file": "test-key", "rate_remaining": "100"},
            "https://example.invalid/listings",
        ),
    )

    def normalize(listing, _request_url, _meta):
        assert not conn.in_transaction
        events.append(f"normalize:{listing['id']}")
        return {
            "listing_id": listing["id"],
            "deal_payload": {
                "listing_id": listing["id"],
                "name": "AK-47 | Test (Field-Tested)",
                "buy_price_cents": 1000,
                "stickers": [],
            },
        }

    def model(payload, model_conn):
        assert model_conn is conn
        assert not conn.in_transaction
        model_conn.execute("SELECT 1").fetchone()
        assert not conn.in_transaction
        events.append(f"model:{payload['listing_id']}")
        return {
            "verdict": "good",
            "name": payload["name"],
            "buy_price_cents": 1000,
            "predicted_price_cents": 1400,
            "profit_cents": 200,
            "roi_percent": 20,
            "liquidity_score": 30,
        }

    original_persist = deal_scanner.persist_model_result

    def traced_persist(*args, **kwargs):
        assert conn.in_transaction
        listing_data = args[1]
        events.append(f"persist:{listing_data['listing_id']}")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(deal_scanner, "normalize_listing_to_deal_payload", normalize)
    monkeypatch.setitem(deal_scanner.MODEL_RUNNERS, "almaz", model)
    monkeypatch.setattr(deal_scanner, "persist_model_result", traced_persist)

    returned, saved, _meta, model_saved = deal_scanner.scan_sticker(
        conn,
        {"sticker_id": 1, "name": "Sticker | Test"},
        scanner_config(enabled_models=["almaz"]),
    )

    assert (returned, saved, model_saved) == (2, 2, {"almaz": 2})
    assert events == [
        "normalize:listing-1",
        "model:listing-1",
        "normalize:listing-2",
        "model:listing-2",
        "persist:listing-1",
        "persist:listing-2",
    ]
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM model_deal_predictions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM model_deal_scans").fetchone()[0] == 1


def test_scan_sticker_does_not_hold_writer_lock_during_model_inference(monkeypatch, tmp_path):
    db_path = tmp_path / "scanner-lock.db"
    conn = sqlite3.connect(db_path, timeout=0.1)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    deal_scanner.init_db(conn)
    competing_writer = sqlite3.connect(db_path, timeout=0.1)
    competing_writer.execute("PRAGMA journal_mode=WAL")

    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        lambda *_args, **_kwargs: (
            [{"id": "listing-1"}, {"id": "listing-2"}],
            None,
            {},
            "https://example.invalid/listings",
        ),
    )
    monkeypatch.setattr(
        deal_scanner,
        "normalize_listing_to_deal_payload",
        lambda listing, *_args: {
            "listing_id": listing["id"],
            "deal_payload": {
                "listing_id": listing["id"],
                "name": "AK-47 | Test (Field-Tested)",
                "buy_price_cents": 1000,
                "stickers": [],
            },
        },
    )

    writer_commits = []

    def model(payload, _model_conn):
        competing_writer.execute(
            """
            INSERT INTO app_schema_versions (component, version, migrated_at)
            VALUES ('lock-probe', 1, ?)
            ON CONFLICT(component) DO UPDATE SET migrated_at = excluded.migrated_at
            """,
            (payload["listing_id"],),
        )
        competing_writer.commit()
        writer_commits.append(payload["listing_id"])
        return {
            "verdict": "good",
            "buy_price_cents": 1000,
            "predicted_price_cents": 1400,
            "profit_cents": 200,
            "roi_percent": 20,
            "liquidity_score": 30,
        }

    monkeypatch.setitem(deal_scanner.MODEL_RUNNERS, "almaz", model)
    try:
        deal_scanner.scan_sticker(
            conn,
            {"sticker_id": 1, "name": "Sticker | Test"},
            scanner_config(enabled_models=["almaz"]),
        )
    finally:
        competing_writer.close()
        conn.close()

    assert writer_commits == ["listing-1", "listing-2"]


def test_scan_sticker_rolls_back_prediction_batch_when_scan_log_fails(monkeypatch):
    conn = memory_db()
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        lambda *_args, **_kwargs: (
            [{"id": "listing-1"}],
            None,
            {},
            "https://example.invalid/listings",
        ),
    )
    monkeypatch.setattr(
        deal_scanner,
        "normalize_listing_to_deal_payload",
        lambda *_args: {
            "listing_id": "listing-1",
            "deal_payload": {
                "listing_id": "listing-1",
                "name": "AK-47 | Test (Field-Tested)",
                "buy_price_cents": 1000,
                "stickers": [],
            },
        },
    )
    monkeypatch.setitem(
        deal_scanner.MODEL_RUNNERS,
        "almaz",
        lambda *_args: {
            "verdict": "good",
            "buy_price_cents": 1000,
            "predicted_price_cents": 1400,
            "profit_cents": 200,
            "roi_percent": 20,
            "liquidity_score": 30,
        },
    )
    monkeypatch.setattr(
        deal_scanner,
        "save_scan_log",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("simulated scan log failure")
        ),
    )

    with pytest.raises(sqlite3.OperationalError, match="simulated scan log failure"):
        deal_scanner.scan_sticker(
            conn,
            {"sticker_id": 1, "name": "Sticker | Test"},
            scanner_config(enabled_models=["almaz"]),
        )

    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM model_deal_predictions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM model_deal_candidates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM model_deal_scans").fetchone()[0] == 0


def test_scan_sticker_keeps_per_model_write_isolation(monkeypatch):
    conn = memory_db()
    insert_prediction(conn, "listing-1", model_name="igor", verdict="good")
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        lambda *_args, **_kwargs: (
            [{"id": "listing-1"}],
            None,
            {},
            "https://example.invalid/listings",
        ),
    )
    monkeypatch.setattr(
        deal_scanner,
        "normalize_listing_to_deal_payload",
        lambda *_args: {
            "listing_id": "listing-1",
            "deal_payload": {
                "listing_id": "listing-1",
                "name": "AK-47 | Test (Field-Tested)",
                "buy_price_cents": 1000,
                "stickers": [],
            },
        },
    )

    prediction = {
        "verdict": "good",
        "buy_price_cents": 1000,
        "predicted_price_cents": 1400,
        "profit_cents": 200,
        "roi_percent": 20,
        "liquidity_score": 30,
    }
    monkeypatch.setitem(deal_scanner.MODEL_RUNNERS, "almaz", lambda *_args: prediction)
    monkeypatch.setitem(deal_scanner.MODEL_RUNNERS, "igor", lambda *_args: prediction)
    original_upsert = deal_scanner.upsert_model_prediction

    def fail_only_igor(*args, **kwargs):
        model_name = args[5]
        if model_name == "igor":
            raise sqlite3.OperationalError("simulated Igor write failure")
        return original_upsert(*args, **kwargs)

    monkeypatch.setattr(deal_scanner, "upsert_model_prediction", fail_only_igor)

    returned, saved, _meta, model_saved = deal_scanner.scan_sticker(
        conn,
        {"sticker_id": 1, "name": "Sticker | Test"},
        scanner_config(enabled_models=["almaz", "igor"]),
    )

    verdicts = dict(
        conn.execute(
            "SELECT model_name, verdict FROM model_deal_predictions WHERE listing_id = ?",
            ("listing-1",),
        )
    )
    assert (returned, saved, model_saved) == (1, 1, {"almaz": 1, "igor": 0})
    assert verdicts == {"almaz": "good", "igor": "error"}
    assert conn.execute("SELECT COUNT(*) FROM model_deal_candidates").fetchone()[0] == 1
    error = conn.execute(
        "SELECT error FROM model_deal_scans ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert "simulated Igor write failure" in error


def test_disabled_guard_prevents_csfloat_request_and_inference(monkeypatch):
    conn = memory_db()
    requests = []
    inferences = []
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        lambda *_args, **_kwargs: requests.append("request"),
    )
    monkeypatch.setitem(
        deal_scanner.MODEL_RUNNERS,
        "almaz",
        lambda *_args: inferences.append("inference"),
    )

    with pytest.raises(deal_scanner.ScanControlInterrupted, match="paused"):
        deal_scanner.scan_sticker(
            conn,
            {"sticker_id": 1, "name": "Sticker | Test"},
            scanner_config(enabled_models=["almaz"]),
            should_continue=lambda: False,
        )

    assert requests == []
    assert inferences == []
    assert conn.execute("SELECT COUNT(*) FROM model_deal_scans").fetchone()[0] == 0


def test_pause_between_models_stops_further_inference(monkeypatch):
    conn = memory_db()
    enabled = True
    calls = []
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        lambda *_args, **_kwargs: (
            [{"id": "listing-1"}],
            None,
            {},
            "https://example.invalid/listings",
        ),
    )
    monkeypatch.setattr(
        deal_scanner,
        "normalize_listing_to_deal_payload",
        lambda *_args: {
            "listing_id": "listing-1",
            "deal_payload": {
                "listing_id": "listing-1",
                "name": "AK-47 | Test (Field-Tested)",
                "buy_price_cents": 1000,
            },
        },
    )

    def first_model(*_args):
        nonlocal enabled
        calls.append("almaz")
        enabled = False
        return {
            "verdict": "good",
            "buy_price_cents": 1000,
            "predicted_price_cents": 1400,
            "profit_cents": 200,
            "roi_percent": 20,
            "liquidity_score": 30,
        }

    monkeypatch.setitem(deal_scanner.MODEL_RUNNERS, "almaz", first_model)
    monkeypatch.setitem(
        deal_scanner.MODEL_RUNNERS,
        "igor",
        lambda *_args: calls.append("igor"),
    )

    with pytest.raises(deal_scanner.ScanControlInterrupted, match="paused"):
        deal_scanner.scan_sticker(
            conn,
            {"sticker_id": 1, "name": "Sticker | Test"},
            scanner_config(enabled_models=["almaz", "igor"]),
            should_continue=lambda: enabled,
        )

    assert calls == ["almaz"]
    assert conn.execute("SELECT COUNT(*) FROM model_deal_predictions").fetchone()[0] == 0


def test_pause_after_page_stops_further_network_requests(monkeypatch):
    conn = memory_db()
    enabled = True
    requests = []

    def fetch_page(*_args, **_kwargs):
        nonlocal enabled
        requests.append("request")
        enabled = False
        return ([{"id": "listing-1"}], "next-page", {}, "https://example.invalid")

    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        fetch_page,
    )

    with pytest.raises(deal_scanner.ScanControlInterrupted, match="paused"):
        deal_scanner.scan_sticker(
            conn,
            {"sticker_id": 1, "name": "Sticker | Test"},
            scanner_config(listing_pages_per_sticker=3),
            should_continue=lambda: enabled,
        )

    assert requests == ["request"]
    assert conn.execute("SELECT COUNT(*) FROM model_deal_scans").fetchone()[0] == 0


def test_paginated_listings_keep_their_own_request_provenance(monkeypatch):
    conn = memory_db()
    normalized_with = []

    def fetch_page(*_args, cursor=None, **_kwargs):
        if cursor is None:
            return (
                [{"id": "page-1"}],
                "next",
                {"key_file": "key-1", "proxy": "proxy-1"},
                "https://example.invalid/page-1",
            )
        return (
            [{"id": "page-2"}],
            None,
            {"key_file": "key-2", "proxy": "proxy-2"},
            "https://example.invalid/page-2",
        )

    def normalize(listing, request_url, meta):
        normalized_with.append((listing["id"], request_url, meta["key_file"]))
        return {"listing_id": listing["id"], "deal_payload": {}}

    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        fetch_page,
    )
    monkeypatch.setattr(deal_scanner, "normalize_listing_to_deal_payload", normalize)

    deal_scanner.scan_sticker(
        conn,
        {"sticker_id": 1, "name": "Sticker | Test"},
        scanner_config(
            listings_per_sticker=1,
            listing_pages_per_sticker=2,
            enabled_models=["almaz"],
        ),
    )

    assert normalized_with == [
        ("page-1", "https://example.invalid/page-1", "key-1"),
        ("page-2", "https://example.invalid/page-2", "key-2"),
    ]


def _insert_test_knife_catalog_row(conn, market_hash_name):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cs2_item_catalog (
            market_hash_name TEXT PRIMARY KEY,
            item_kind TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO cs2_item_catalog VALUES (?, 'knife')",
        (market_hash_name,),
    )


def test_knife_scan_fails_closed_before_network_when_model_is_unavailable(monkeypatch):
    conn = memory_db()
    market_hash_name = "★ Karambit | Doppler (Factory New)"
    _insert_test_knife_catalog_row(conn, market_hash_name)
    monkeypatch.setattr(
        deal_scanner.knife_model_runtime,
        "load_model",
        lambda: (_ for _ in ()).throw(
            deal_scanner.knife_model_runtime.KnifeModelUnavailable("not ready")
        ),
    )
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings",
        lambda **_kwargs: pytest.fail("unavailable knife model must not spend API quota"),
    )

    returned, saved, meta, model_saved = deal_scanner.scan_knife(
        conn,
        {"market_hash_name": market_hash_name},
        scanner_config(knives_enabled=True),
    )

    assert (returned, saved, model_saved) == (0, 0, {"knives": 0})
    assert meta["model_status"] == "unavailable"
    scan = conn.execute(
        """
        SELECT source_type, source_name, target_key, request_url, error
        FROM model_deal_scans ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    assert dict(scan) == {
        "source_type": "knife",
        "source_name": market_hash_name,
        "target_key": f"knife:{market_hash_name}",
        "request_url": "",
        "error": "knife_model_unavailable",
    }


def test_knife_scan_uses_exact_client_and_dedicated_model_only(monkeypatch):
    conn = memory_db()
    market_hash_name = "★ Butterfly Knife | Fade (Factory New)"
    _insert_test_knife_catalog_row(conn, market_hash_name)
    fetch_calls = []
    dedicated_calls = []

    monkeypatch.setattr(
        deal_scanner.knife_model_runtime,
        "load_model",
        lambda: {"model_name": "knives", "item_kind": "knife"},
    )

    def fetch_page(**kwargs):
        fetch_calls.append(kwargs)
        return (
            [
                {"id": "valid", "name": market_hash_name, "price": 125_000},
                {"id": "wrong", "name": "★ Bayonet | Fade (Factory New)", "price": 100_000},
                {"id": "auction", "name": market_hash_name, "price": 110_000},
                {"id": "fractional", "name": market_hash_name, "price": 99_999.5},
            ],
            None,
            {"key_file": "test-key", "rate_remaining": "11"},
            "https://example.invalid/knives",
        )

    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings",
        fetch_page,
    )

    def normalize(listing, _request_url, _meta):
        return {
            "listing_id": listing["id"],
            "deal_payload": {
                "listing_id": listing["id"],
                "name": listing["name"],
                "buy_price_cents": listing["price"],
                "listing_type": (
                    "auction" if listing["id"] == "auction" else "buy_now"
                ),
                "stickers": [],
            },
        }

    monkeypatch.setattr(deal_scanner, "normalize_listing_to_deal_payload", normalize)
    for model_name in ("almaz", "igor", "nova"):
        monkeypatch.setitem(
            deal_scanner.MODEL_RUNNERS,
            model_name,
            lambda *_args, model_name=model_name: pytest.fail(
                f"knife reached {model_name}"
            ),
        )

    def knife_model(payload, _conn):
        dedicated_calls.append(payload["listing_id"])
        return {
            "model_name": "knives",
            "item_kind": "knife",
            "verdict": "good",
            "name": payload["name"],
            "buy_price_cents": payload["buy_price_cents"],
            "predicted_price_cents": 160_000,
            "profit_cents": 11_000,
            "roi_percent": 8.8,
            "liquidity_score": 35,
        }

    monkeypatch.setattr(deal_scanner.knife_model_runtime, "predict_deal", knife_model)

    returned, saved, _meta, model_saved = deal_scanner.scan_knife(
        conn,
        {"market_hash_name": market_hash_name},
        scanner_config(knives_enabled=True, enabled_models=["almaz", "igor", "nova"]),
    )

    assert (returned, saved, model_saved) == (4, 1, {"knives": 1})
    assert dedicated_calls == ["valid"]
    assert fetch_calls == [
        {
            "market_hash_name": market_hash_name,
            "limit": 10,
            "sort_by": "lowest_price",
            "cursor": None,
        }
    ]
    row = conn.execute(
        """
        SELECT model_name, source_type, source_name, target_key
        FROM model_deal_predictions WHERE listing_id = 'valid'
        """
    ).fetchone()
    assert dict(row) == {
        "model_name": "knives",
        "source_type": "knife",
        "source_name": market_hash_name,
        "target_key": f"knife:{market_hash_name}",
    }


def test_sticker_discovered_knife_never_reaches_weapon_skin_models(monkeypatch):
    conn = memory_db()
    market_hash_name = "★ Bayonet | Marble Fade (Factory New)"
    _insert_test_knife_catalog_row(conn, market_hash_name)
    calls = []
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        lambda *_args, **_kwargs: (
            [{"id": "knife-from-sticker"}],
            None,
            {},
            "https://example.invalid/sticker",
        ),
    )
    monkeypatch.setattr(
        deal_scanner,
        "normalize_listing_to_deal_payload",
        lambda *_args: {
            "listing_id": "knife-from-sticker",
            "deal_payload": {
                "listing_id": "knife-from-sticker",
                "name": market_hash_name,
                "buy_price_cents": 80_000,
                "listing_type": "buy_now",
                "stickers": [],
            },
        },
    )
    for model_name in ("almaz", "igor", "nova"):
        monkeypatch.setitem(
            deal_scanner.MODEL_RUNNERS,
            model_name,
            lambda *_args, model_name=model_name: pytest.fail(
                f"knife reached {model_name}"
            ),
        )
    monkeypatch.setattr(
        deal_scanner.knife_model_runtime,
        "predict_deal",
        lambda payload, _conn: calls.append(payload["listing_id"])
        or {
            "model_name": "knives",
            "item_kind": "knife",
            "verdict": "bad",
            "name": payload["name"],
            "buy_price_cents": payload["buy_price_cents"],
            "predicted_price_cents": 70_000,
            "profit_cents": -10_000,
            "roi_percent": -12.5,
            "liquidity_score": 20,
        },
    )

    returned, saved, _meta, model_saved = deal_scanner.scan_sticker(
        conn,
        {"sticker_id": 77, "name": "Sticker | Knife Route"},
        scanner_config(enabled_models=["almaz", "igor", "nova"]),
    )

    assert (returned, saved) == (1, 0)
    assert calls == ["knife-from-sticker"]
    assert model_saved == {"almaz": 0, "igor": 0, "nova": 0, "knives": 0}
    assert conn.execute(
        "SELECT model_name FROM model_deal_predictions WHERE listing_id = ?",
        ("knife-from-sticker",),
    ).fetchone()[0] == "knives"


def test_liquid_skin_scan_reuses_models_filters_fail_closed_and_labels_source(
    monkeypatch,
):
    conn = memory_db()
    fetch_calls = []
    model_calls = []
    market_hash_name = "AK-47 | Liquid Test (Field-Tested)"

    def fetch_skin(name, **kwargs):
        fetch_calls.append((name, kwargs))
        return (
            [
                {"id": "good", "name": market_hash_name, "price": 500},
                {"id": "already", "name": market_hash_name, "price": 450},
                {"id": "expensive", "name": market_hash_name, "price": 1001},
                {
                    "id": "fractional",
                    "name": market_hash_name,
                    "price": 1000.9,
                },
                {"id": "auction", "name": market_hash_name, "price": 400},
                {"id": "wrong-name", "name": "M4A4 | Wrong", "price": 400},
            ],
            None,
            {"key_file": "test-key", "rate_remaining": "12"},
            "https://example.invalid/listings",
        )

    def normalize(listing, _request_url, _meta):
        return {
            "listing_id": listing["id"],
            "deal_payload": {
                "listing_id": listing["id"],
                "name": listing["name"],
                "buy_price_cents": listing["price"],
                "listing_type": (
                    "auction" if listing["id"] == "auction" else "buy_now"
                ),
                "stickers": [],
            },
        }

    def model(payload, _conn):
        model_calls.append(payload["listing_id"])
        return {
            "verdict": "good",
            "name": payload["name"],
            "buy_price_cents": payload["buy_price_cents"],
            "predicted_price_cents": 800,
            "profit_cents": 200,
            "roi_percent": 40,
            "liquidity_score": 30,
        }

    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_skin",
        fetch_skin,
    )
    monkeypatch.setattr(deal_scanner, "normalize_listing_to_deal_payload", normalize)
    monkeypatch.setitem(deal_scanner.MODEL_RUNNERS, "almaz", model)
    cycle_seen = {"already"}

    returned, saved, _meta, model_saved = deal_scanner.scan_liquid_skin(
        conn,
        {"market_hash_name": market_hash_name},
        scanner_config(enabled_models=["almaz"]),
        cycle_seen_listing_ids=cycle_seen,
    )

    assert (returned, saved, model_saved) == (6, 1, {"almaz": 1})
    assert model_calls == ["good"]
    assert fetch_calls == [
        (
            market_hash_name,
            {
                "limit": 20,
                "sort_by": "lowest_price",
                "cursor": None,
                "max_price": 1000,
            },
        )
    ]
    assert cycle_seen == {"already", "good"}
    prediction = conn.execute(
        """
        SELECT source_type, source_name, target_key,
            source_sticker_id, source_sticker_name
        FROM model_deal_predictions
        WHERE listing_id = 'good' AND model_name = 'almaz'
        """
    ).fetchone()
    assert dict(prediction) == {
        "source_type": "liquid_skin",
        "source_name": market_hash_name,
        "target_key": f"liquid_skin:{market_hash_name}",
        "source_sticker_id": None,
        "source_sticker_name": None,
    }
    scan = conn.execute(
        """
        SELECT source_type, source_name, sticker_id, sticker_name
        FROM model_deal_scans ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    assert dict(scan) == {
        "source_type": "liquid_skin",
        "source_name": market_hash_name,
        "sticker_id": None,
        "sticker_name": None,
    }
    sources = [
        tuple(row)
        for row in conn.execute(
            """
            SELECT listing_id, source_type, source_name
            FROM model_deal_listing_sources ORDER BY listing_id
            """
        )
    ]
    assert sources == [
        ("already", "liquid_skin", market_hash_name),
        ("good", "liquid_skin", market_hash_name),
    ]


def test_liquid_skin_scan_stops_pagination_at_rate_limit(monkeypatch):
    conn = memory_db()
    requests = []

    def fetch_page(*_args, **_kwargs):
        requests.append("request")
        return (
            [],
            "next-page",
            {"key_file": "test-key", "rate_remaining": "0"},
            "https://example.invalid/listings",
        )

    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_skin",
        fetch_page,
    )

    returned, saved, _meta, model_saved = deal_scanner.scan_liquid_skin(
        conn,
        {"market_hash_name": "AK-47 | Test (Field-Tested)"},
        scanner_config(
            enabled_models=["almaz"],
            listing_pages_per_liquid_skin=2,
        ),
    )

    assert (returned, saved, model_saved) == (0, 0, {"almaz": 0})
    assert requests == ["request"]
    assert "rate limit exhausted" in conn.execute(
        "SELECT error FROM model_deal_scans ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]


def test_liquid_skin_scan_marks_second_page_429_as_rate_exhausted(monkeypatch):
    conn = memory_db()
    requests = []

    def fetch_page(*_args, **_kwargs):
        requests.append("request")
        if len(requests) == 1:
            return (
                [{"id": "first"}],
                "next-page",
                {"rate_remaining": "10"},
                "https://example.invalid/page-1",
            )
        raise deal_scanner.csfloat_listing_fetcher.CsfloatRateLimitError("429")

    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_skin",
        fetch_page,
    )
    monkeypatch.setattr(
        deal_scanner,
        "normalize_listing_to_deal_payload",
        lambda *_args: {"listing_id": "first", "deal_payload": {}},
    )

    returned, saved, meta, model_saved = deal_scanner.scan_liquid_skin(
        conn,
        {"market_hash_name": "AK-47 | Test (Field-Tested)"},
        scanner_config(
            enabled_models=["almaz"],
            listings_per_liquid_skin=1,
            listing_pages_per_liquid_skin=2,
        ),
    )

    assert (returned, saved, model_saved) == (1, 0, {"almaz": 0})
    assert requests == ["request", "request"]
    assert meta["rate_exhausted"] is True
    assert "429" in conn.execute(
        "SELECT error FROM model_deal_scans ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]


def test_cross_profile_dedupe_preserves_both_source_memberships(monkeypatch):
    conn = memory_db()
    market_hash_name = "AK-47 | Shared (Field-Tested)"
    listing = {"id": "shared", "name": market_hash_name, "price": 500}
    model_calls = []
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        lambda *_args, **_kwargs: ([listing], None, {}, "https://example.invalid/sticker"),
    )
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_skin",
        lambda *_args, **_kwargs: ([listing], None, {}, "https://example.invalid/skin"),
    )
    monkeypatch.setattr(
        deal_scanner,
        "normalize_listing_to_deal_payload",
        lambda raw, *_args: {
            "listing_id": raw["id"],
            "deal_payload": {
                "listing_id": raw["id"],
                "name": raw["name"],
                "buy_price_cents": raw["price"],
                "listing_type": "buy_now",
                "stickers": [],
            },
        },
    )

    def model(payload, _conn):
        model_calls.append(payload["listing_id"])
        return {
            "verdict": "good",
            "name": payload["name"],
            "buy_price_cents": 500,
            "predicted_price_cents": 800,
            "profit_cents": 200,
            "roi_percent": 40,
            "liquidity_score": 30,
        }

    monkeypatch.setitem(deal_scanner.MODEL_RUNNERS, "almaz", model)
    config = scanner_config(enabled_models=["almaz"])
    cycle_seen = set()

    sticker_result = deal_scanner.scan_sticker(
        conn,
        {"sticker_id": 7, "name": "Sticker | Shared"},
        config,
        cycle_seen_listing_ids=cycle_seen,
    )
    skin_result = deal_scanner.scan_liquid_skin(
        conn,
        {"market_hash_name": market_hash_name},
        config,
        cycle_seen_listing_ids=cycle_seen,
    )

    assert sticker_result[0:2] == (1, 1)
    assert skin_result[0:2] == (1, 0)
    assert model_calls == ["shared"]
    assert conn.execute(
        "SELECT scan_count FROM model_deal_predictions WHERE listing_id = 'shared'"
    ).fetchone()[0] == 1
    assert [
        tuple(row)
        for row in conn.execute(
            """
            SELECT source_type, target_key
            FROM model_deal_listing_sources
            WHERE listing_id = 'shared' ORDER BY source_type
            """
        )
    ] == [
        ("liquid_skin", f"liquid_skin:{market_hash_name}"),
        ("sticker", "sticker:7"),
    ]


class FakeStopEvent:
    def __init__(self, clock, *, stop_after_waits=None):
        self.clock = clock
        self.stop_after_waits = stop_after_waits
        self.waits = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock[0] += seconds
        if self.stop_after_waits is not None and len(self.waits) >= self.stop_after_waits:
            self.stopped = True
        return self.stopped


class FakeReporter:
    def __init__(self):
        self.state = {"cycles_completed": 0}
        self.reports = []

    def report(self, process_state=None, *, force=False, **changes):
        del force
        if process_state is not None:
            changes["process_state"] = process_state
        self.state.update(changes)
        self.reports.append(dict(self.state))
        return self.state


def test_wait_hot_reloads_pause_then_resumes_within_poll_interval():
    clock = [0.0]
    event = FakeStopEvent(clock)
    reporter = FakeReporter()
    controls = iter(
        [
            {"enabled": False, "interval_seconds": 600},
            {"enabled": True, "interval_seconds": 600},
        ]
    )

    control = deal_scanner.wait_for_scan_permission(
        None,
        None,
        default_interval_seconds=600,
        stop_event=event,
        reporter=reporter,
        control_reader=lambda _default: next(controls),
        monotonic_fn=lambda: clock[0],
    )

    assert control["enabled"] is True
    assert event.waits == [deal_scanner.CONTROL_POLL_SECONDS]
    assert reporter.reports[-1]["process_state"] == "paused"


def test_interval_change_recomputes_deadline_from_cycle_start():
    clock = [0.0]
    event = FakeStopEvent(clock)

    def read_control(_default):
        interval = 600 if clock[0] < 1 else 60
        return {"enabled": True, "interval_seconds": interval}

    control = deal_scanner.wait_for_scan_permission(
        0.0,
        1000.0,
        default_interval_seconds=600,
        stop_event=event,
        control_reader=read_control,
        monotonic_fn=lambda: clock[0],
    )

    assert control["interval_seconds"] == 60
    assert clock[0] == 60
    assert max(event.waits) <= deal_scanner.CONTROL_POLL_SECONDS


def test_longer_interval_postpones_deadline_without_restart():
    clock = [0.0]
    event = FakeStopEvent(clock)

    def read_control(_default):
        interval = 60 if clock[0] < 1 else 120
        return {"enabled": True, "interval_seconds": interval}

    control = deal_scanner.wait_for_scan_permission(
        0.0,
        1000.0,
        default_interval_seconds=600,
        stop_event=event,
        control_reader=read_control,
        monotonic_fn=lambda: clock[0],
    )

    assert control["interval_seconds"] == 120
    assert clock[0] == 120
    assert max(event.waits) <= deal_scanner.CONTROL_POLL_SECONDS


def test_overdue_cycle_starts_immediately_without_completion_delay():
    clock = [90.0]
    event = FakeStopEvent(clock)

    control = deal_scanner.wait_for_scan_permission(
        0.0,
        1000.0,
        default_interval_seconds=600,
        stop_event=event,
        control_reader=lambda _default: {"enabled": True, "interval_seconds": 60},
        monotonic_fn=lambda: clock[0],
    )

    assert control["enabled"] is True
    assert event.waits == []


def test_run_forever_stays_alive_while_paused_without_scanning(monkeypatch):
    clock = [0.0]
    event = FakeStopEvent(clock, stop_after_waits=2)
    reporter = FakeReporter()
    conn = memory_db()
    monkeypatch.setattr(deal_scanner, "RuntimeStatusReporter", lambda: reporter)
    monkeypatch.setattr(deal_scanner, "load_config", scanner_config)
    monkeypatch.setattr(deal_scanner, "connect_db", lambda: conn)
    monkeypatch.setattr(
        deal_scanner,
        "enforce_deal_policy",
        lambda *_args: {"predictions_filtered": 0, "candidates_removed": 0},
    )
    monkeypatch.setattr(deal_scanner, "expire_stale_predictions", lambda *_args: 0)
    monkeypatch.setattr(
        deal_scanner,
        "discover_sticker_targets",
        lambda *_args: [{"sticker_id": 1, "name": "Sticker | Test"}],
    )
    monkeypatch.setattr(
        deal_scanner,
        "read_live_control",
        lambda _default: {"enabled": False, "interval_seconds": 600},
    )
    monkeypatch.setattr(
        deal_scanner,
        "scan_sticker",
        lambda *_args, **_kwargs: pytest.fail("paused scanner must not scan"),
    )

    deal_scanner.run_forever(stop_event=event)

    states = [report["process_state"] for report in reporter.reports]
    assert states[:2] == ["starting", "paused"]
    assert states[-1] == "stopped"
    assert len(event.waits) == 2


def test_run_forever_applies_target_revision_at_next_cycle_boundary(monkeypatch):
    clock = [0.0]
    event = FakeStopEvent(clock)
    reporter = FakeReporter()
    conn = memory_db()
    config = scanner_config(
        liquid_skins_enabled=False,
        knives_enabled=False,
        gloves_enabled=False,
        stickers_per_cycle=1,
    )
    first = scanner_target_control(
        sticker_guns={
            "enabled": True,
            "selection_mode": "custom",
            "include": ["sticker:1"],
        },
        liquid_under_10={"enabled": False},
        knives={"enabled": False},
        gloves={"enabled": False},
    )
    second = json.loads(json.dumps(first))
    second["revision"] = first["revision"] + 1
    second["profiles"]["sticker_guns"]["include"] = ["sticker:2"]
    control_reads = []
    permission_calls = []
    scanned = []

    def read_targets(*_args, **_kwargs):
        control_reads.append(True)
        return first if len(control_reads) <= 2 else second

    def discover(_conn, _config, target_control, **_kwargs):
        key = target_control["profiles"]["sticker_guns"]["include"][0]
        identifier = int(key.split(":", 1)[1])
        return [
            {
                "sticker_id": identifier,
                "name": f"Sticker | {identifier}",
                "source_type": "sticker",
                "target_key": key,
            }
        ]

    def permission(*_args, **_kwargs):
        permission_calls.append(True)
        if len(permission_calls) > 2:
            event.stopped = True
            return None
        return {"enabled": True, "interval_seconds": 60}

    def scan(_conn, target, _config, **_kwargs):
        scanned.append(target["sticker_id"])
        return 0, 0, {}, {"almaz": 0}

    monkeypatch.setattr(deal_scanner, "RuntimeStatusReporter", lambda: reporter)
    monkeypatch.setattr(deal_scanner, "load_config", lambda: config)
    monkeypatch.setattr(deal_scanner, "connect_db", lambda: conn)
    monkeypatch.setattr(deal_scanner, "load_scanner_target_control", read_targets)
    monkeypatch.setattr(deal_scanner, "discover_sticker_targets", discover)
    monkeypatch.setattr(deal_scanner, "wait_for_scan_permission", permission)
    monkeypatch.setattr(
        deal_scanner,
        "read_live_control",
        lambda _default: {"enabled": True, "interval_seconds": 60},
    )
    monkeypatch.setattr(
        deal_scanner,
        "enforce_deal_policy",
        lambda *_args: {"predictions_filtered": 0, "candidates_removed": 0},
    )
    monkeypatch.setattr(deal_scanner, "expire_stale_predictions", lambda *_args: 0)
    monkeypatch.setattr(deal_scanner, "maintain_deal_intelligence", lambda *_args: {})
    monkeypatch.setattr(deal_scanner, "scan_sticker", scan)

    deal_scanner.run_forever(stop_event=event)

    assert scanned == [1, 2]


def test_run_forever_rediscovery_preserves_budget_and_covers_next_knife_window(
    monkeypatch,
):
    clock = [0.0]
    event = FakeStopEvent(clock)
    reporter = FakeReporter()
    conn = memory_db()
    conn.execute(
        "CREATE TABLE cs2_item_catalog "
        "(market_hash_name TEXT PRIMARY KEY, item_kind TEXT NOT NULL)"
    )
    names = [f"★ Cycle Knife {index} | Finish (Factory New)" for index in range(5)]
    conn.executemany(
        "INSERT INTO cs2_item_catalog VALUES (?, 'knife')",
        [(name,) for name in names],
    )
    config = scanner_config(
        liquid_skins_enabled=False,
        knives_enabled=True,
        knives_per_cycle=2,
        max_knife_targets=3,
        knife_rediscovery_cycles=1,
        tournament_patterns=[],
        extra_sticker_names=[],
    )
    permission_calls = []
    discovery_calls = []
    scans_by_cycle = {}
    original_discovery = deal_scanner.discover_knife_targets

    def permission(*_args, **_kwargs):
        permission_calls.append(True)
        if len(permission_calls) > 3:
            event.stopped = True
            return None
        return {"enabled": True, "interval_seconds": 60}

    def discover(*args, **kwargs):
        discovery_calls.append(True)
        return original_discovery(*args, **kwargs)

    def scan(_conn, target, _config, **_kwargs):
        cycle = int(reporter.state.get("cycle_number") or 0)
        scans_by_cycle.setdefault(cycle, []).append(target["market_hash_name"])
        return 0, 0, {"model_status": "ready"}, {"knives": 0}

    monkeypatch.setattr(deal_scanner, "RuntimeStatusReporter", lambda: reporter)
    monkeypatch.setattr(deal_scanner, "load_config", lambda: config)
    monkeypatch.setattr(deal_scanner, "connect_db", lambda: conn)
    monkeypatch.setattr(
        deal_scanner,
        "load_scanner_target_control",
        lambda *_args, **_kwargs: scanner_target_control(
            sticker_guns={"enabled": False},
            liquid_under_10={"enabled": False},
            knives={"enabled": True},
            gloves={"enabled": False},
        ),
    )
    monkeypatch.setattr(
        deal_scanner,
        "enforce_deal_policy",
        lambda *_args: {"predictions_filtered": 0, "candidates_removed": 0},
    )
    monkeypatch.setattr(deal_scanner, "expire_stale_predictions", lambda *_args: 0)
    monkeypatch.setattr(deal_scanner, "maintain_deal_intelligence", lambda *_args: {})
    monkeypatch.setattr(deal_scanner, "wait_for_scan_permission", permission)
    monkeypatch.setattr(
        deal_scanner,
        "read_live_control",
        lambda _default: {"enabled": True, "interval_seconds": 60},
    )
    monkeypatch.setattr(deal_scanner, "discover_knife_targets", discover)
    monkeypatch.setattr(deal_scanner, "scan_knife", scan)

    deal_scanner.run_forever(stop_event=event)

    assert len(discovery_calls) == 3
    assert all(len(scans) <= 2 for scans in scans_by_cycle.values())
    assert set().union(*map(set, scans_by_cycle.values())) == set(names)
    persisted = deal_scanner.load_knife_rotation_state(conn)
    assert persisted["pending_target_keys"] == []


def test_run_forever_stops_current_cycle_after_page_one_429(monkeypatch):
    clock = [0.0]
    event = FakeStopEvent(clock, stop_after_waits=2)
    reporter = FakeReporter()
    conn = memory_db()
    calls = []
    monkeypatch.setattr(deal_scanner, "RuntimeStatusReporter", lambda: reporter)
    monkeypatch.setattr(
        deal_scanner,
        "load_config",
        lambda: scanner_config(
            liquid_skins_enabled=False,
            stickers_per_cycle=2,
        ),
    )
    monkeypatch.setattr(deal_scanner, "connect_db", lambda: conn)
    monkeypatch.setattr(
        deal_scanner,
        "load_scanner_target_control",
        lambda *_args, **_kwargs: scanner_target_control(
            sticker_guns={"enabled": True},
            liquid_under_10={"enabled": False},
            knives={"enabled": False},
            gloves={"enabled": False},
        ),
    )
    monkeypatch.setattr(
        deal_scanner,
        "enforce_deal_policy",
        lambda *_args: {"predictions_filtered": 0, "candidates_removed": 0},
    )
    monkeypatch.setattr(deal_scanner, "expire_stale_predictions", lambda *_args: 0)
    monkeypatch.setattr(deal_scanner, "maintain_deal_intelligence", lambda *_args: {})
    monkeypatch.setattr(
        deal_scanner,
        "discover_sticker_targets",
        lambda *_args: [
            {"sticker_id": 1, "name": "Sticker | One"},
            {"sticker_id": 2, "name": "Sticker | Two"},
        ],
    )
    monkeypatch.setattr(
        deal_scanner,
        "read_live_control",
        lambda _default: {"enabled": True, "interval_seconds": 60},
    )

    def rate_limited(*_args, **_kwargs):
        calls.append("scan")
        if len(calls) > 1:
            pytest.fail("rate-limited cycle must not request the next target")
        raise deal_scanner.csfloat_listing_fetcher.CsfloatRateLimitError("429")

    monkeypatch.setattr(deal_scanner, "scan_sticker", rate_limited)

    deal_scanner.run_forever(stop_event=event)

    assert calls == ["scan"]
    assert any(report.get("last_error") == "rate_limit_exhausted" for report in reporter.reports)
