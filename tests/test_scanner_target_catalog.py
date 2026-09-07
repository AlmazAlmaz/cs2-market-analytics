import json
import sqlite3

import scanner_control
import scanner_target_catalog


def config(tmp_path, **overrides):
    result = dict(scanner_target_catalog._CONFIG_DEFAULTS)
    result["liquid_skin_catalog_path"] = str(tmp_path / "skins.json")
    result.update(overrides)
    return result


def control(**overrides):
    enabled_defaults = {
        "sticker_guns": True,
        "liquid_under_10": True,
        "knives": False,
        "gloves": False,
    }
    return {
        "schema_version": 1,
        "revision": 4,
        "updated_at": "2026-08-24T00:00:00+00:00",
        "profiles": {
            profile: {
                "enabled": overrides.get(profile, {}).get(
                    "enabled", enabled_defaults[profile]
                ),
                "selection_mode": overrides.get(profile, {}).get(
                    "selection_mode", "recommended"
                ),
                "include": overrides.get(profile, {}).get("include", []),
                "exclude": overrides.get(profile, {}).get("exclude", []),
            }
            for profile in enabled_defaults
        },
    }


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_lightweight_sticker_preview_can_select_observed_non_default_sticker(tmp_path):
    conn = memory_db()
    conn.execute(
        """
        CREATE TABLE sale_stickers (
            listing_id TEXT,
            sticker_index INTEGER,
            sticker_id INTEGER,
            name TEXT,
            price_cents INTEGER,
            reference_price_cents INTEGER
        )
        """
    )
    conn.executemany(
        "INSERT INTO sale_stickers VALUES (?, 0, ?, ?, ?, ?)",
        [
            ("a", 1, "Sticker | Katowice 2014", 1000, 900),
            ("b", 2, "Sticker | User Choice", 200, 250),
        ],
    )
    target_control = control(
        sticker_guns={
            "selection_mode": "custom",
            "include": ["sticker:2"],
        }
    )

    payload = scanner_target_catalog.build_preview_with_connection(
        conn,
        "sticker_guns",
        config=config(tmp_path),
        target_control=target_control,
        search="User",
    )

    assert payload["local_only"] is True
    assert payload["selection"]["selected_count"] == 1
    assert payload["items"][0]["target_key"] == "sticker:2"
    assert payload["items"][0]["selected"] is True


def test_sticker_candidate_query_filters_before_grouping_large_observation_table(
    tmp_path,
):
    conn = memory_db()
    conn.execute(
        """
        CREATE TABLE sale_stickers (
            listing_id TEXT,
            sticker_index INTEGER,
            sticker_id INTEGER,
            name TEXT,
            price_cents INTEGER,
            reference_price_cents INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX idx_test_sticker_name ON sale_stickers(name)")
    conn.executemany(
        "INSERT INTO sale_stickers VALUES (?, 0, ?, ?, 10, 10)",
        [
            (f"irrelevant-{index}", 10_000 + index, f"Sticker | Other {index}")
            for index in range(20_000)
        ],
    )
    conn.execute(
        "INSERT INTO sale_stickers VALUES "
        "('wanted', 0, 1, 'Sticker | Katowice 2014', 1000, 1000)"
    )
    traces = []
    conn.set_trace_callback(traces.append)

    payload = scanner_target_catalog.build_preview_with_connection(
        conn,
        "sticker_guns",
        config=config(
            tmp_path,
            tournament_patterns=[],
            extra_sticker_names=["Sticker | Katowice 2014"],
        ),
        target_control=control(),
    )

    target_query = next(
        statement
        for statement in traces
        if "FROM sale_stickers" in statement and "GROUP BY sticker_id" in statement
    )
    assert target_query.index("AND (name =") < target_query.index("GROUP BY")
    query_plan = [
        row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {target_query}")
    ]
    assert any(
        "SEARCH sale_stickers USING INDEX idx_test_sticker_name" in detail
        for detail in query_plan
    )
    assert payload["selection"]["eligible_count"] == 1
    assert payload["items"][0]["target_key"] == "sticker:1"


def test_liquid_preview_custom_mode_allows_slow_skin_but_never_over_ten_dollars(
    tmp_path,
):
    skin_path = tmp_path / "skins.json"
    skin_path.write_text(
        json.dumps(
            [
                {
                    "market_hash_name": "AK-47 | Slow (Field-Tested)",
                    "base_price_usd": 5,
                    "quantity_on_market": 2,
                },
                {
                    "market_hash_name": "AWP | Expensive (Factory New)",
                    "base_price_usd": 11,
                    "quantity_on_market": 5000,
                },
                {
                    "market_hash_name": "★ Sport Gloves | Vice (Field-Tested)",
                    "base_price_usd": 5,
                    "quantity_on_market": 5000,
                },
                {
                    "market_hash_name": "Sticker | Crown (Foil)",
                    "base_price_usd": 2,
                    "quantity_on_market": 5000,
                },
            ]
        ),
        encoding="utf-8",
    )
    conn = memory_db()
    conn.execute(
        "CREATE TABLE skin_schedule "
        "(market_hash_name TEXT PRIMARY KEY, estimated_sales_per_day REAL)"
    )
    conn.execute(
        "INSERT INTO skin_schedule VALUES ('AK-47 | Slow (Field-Tested)', 0.01)"
    )
    key = "liquid_skin:AK-47 | Slow (Field-Tested)"
    target_control = control(
        liquid_under_10={
            "selection_mode": "custom",
            "include": [key],
        }
    )

    payload = scanner_target_catalog.build_preview_with_connection(
        conn,
        "liquid_under_10",
        config=config(tmp_path),
        target_control=target_control,
    )

    assert payload["selection"]["eligible_count"] == 1
    assert payload["selection"]["selected_count"] == 1
    assert payload["items"][0]["target_key"] == key
    assert payload["items"][0]["base_price_cents"] == 500


def test_path_builder_opens_database_read_only_and_reports_durable_rotation(tmp_path):
    db_path = tmp_path / "history.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE cs2_item_catalog "
            "(market_hash_name TEXT PRIMARY KEY, item_kind TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO cs2_item_catalog VALUES "
            "('★ Karambit | Doppler (Factory New)', 'knife')"
        )
    target_control = control(knives={"enabled": True})

    payload = scanner_target_catalog.build_scanner_target_preview(
        db_path,
        "knives",
        config=config(tmp_path),
        target_control=target_control,
        readiness={"scan_ready": False, "blocked_reason": "collecting_data"},
    )

    assert payload["rotation"] == {
        "durable": True,
        "window_size": 24,
        "eventual_catalog_coverage": True,
    }
    assert payload["items"][0]["name"] == "★ Karambit | Doppler (Factory New)"
    assert payload["blocked_reason"] == "collecting_data"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='model_deal_scanner_profile_state'"
        ).fetchone()[0] == 0


def test_post_contract_is_strict_revisioned_and_returns_ui_metadata(tmp_path):
    path = tmp_path / "target-control.json"
    isolated = scanner_control.get_scanner_target_control(
        path=path,
        profile_defaults=scanner_target_catalog.profile_defaults(config(tmp_path)),
    )
    initial = scanner_target_catalog.build_target_control_payload(
        config=config(tmp_path),
        target_control=isolated,
    )

    changed = scanner_target_catalog.update_target_control_from_payload(
        {
            "expected_revision": isolated["revision"],
            "profiles": {
                "knives": {
                    "enabled": True,
                    "selection_mode": "custom",
                    "include": ["knife:★ Karambit | Doppler (Factory New)"],
                }
            },
        },
        config=config(tmp_path),
        path=path,
    )

    assert initial["limits"]["max_included_targets"] == 250
    assert changed["control"]["revision"] == 1
    assert changed["control"]["profiles"]["knives"]["selection_mode"] == "custom"
    assert changed["profile_order"] == [
        "sticker_guns",
        "liquid_under_10",
        "knives",
        "gloves",
    ]
