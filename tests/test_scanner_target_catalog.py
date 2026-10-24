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


def test_liquid_preview_batches_catalog_lookups_and_preserves_classification(tmp_path):
    names = [f"AK-47 | Bulk {index} (Field-Tested)" for index in range(1100)]
    names.extend(["Catalog-only weapon", "AK-47 | Invalid kind (Field-Tested)"])
    (tmp_path / "skins.json").write_text(
        json.dumps(
            [
                {"market_hash_name": name, "base_price_usd": 5, "quantity_on_market": 1}
                for name in names
            ]
        ),
        encoding="utf-8",
    )
    conn = memory_db()
    conn.execute(
        "CREATE TABLE cs2_item_catalog "
        "(market_hash_name TEXT PRIMARY KEY, item_kind TEXT)"
    )
    conn.executemany(
        "INSERT INTO cs2_item_catalog VALUES (?, ?)",
        [
            (names[0], "glove"),
            ("Catalog-only weapon", "weapon_skin"),
            ("AK-47 | Invalid kind (Field-Tested)", "invalid"),
        ],
    )
    traces = []
    conn.set_trace_callback(traces.append)

    rows = scanner_target_catalog.load_profile_candidates(
        conn, "liquid_under_10", config(tmp_path), control()
    )

    assert {row["name"] for row in rows} == set(names) - {names[0]}
    catalog_lookups = [
        query for query in traces if "SELECT market_hash_name, item_kind" in query
    ]
    assert len(catalog_lookups) == 3
    assert not any("WHERE market_hash_name = " in query for query in traces)


def test_sticker_group_cache_refreshes_selection_expiry_and_database_changes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(scanner_target_catalog, "_sticker_group_cache", {})
    db_path = tmp_path / "history.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sale_stickers (sticker_id INTEGER, name TEXT)")
        conn.execute(
            "INSERT INTO sale_stickers VALUES (1, 'Sticker | Team | Katowice 2014')"
        )
    calls = []
    original = scanner_target_catalog._sticker_group_catalog

    def counted_catalog(conn):
        calls.append(True)
        return original(conn)

    clock = [100.0]
    monkeypatch.setattr(scanner_target_catalog, "_sticker_group_catalog", counted_catalog)
    monkeypatch.setattr(scanner_target_catalog.time, "monotonic", lambda: clock[0])
    target_control = control()

    def preview():
        return scanner_target_catalog.build_scanner_target_preview(
            db_path,
            "sticker_guns",
            config=config(tmp_path),
            target_control=target_control,
        )

    assert preview()["groups"][0]["selected"] is False
    target_control["profiles"]["sticker_guns"]["include_groups"] = ["Katowice 2014"]
    assert preview()["groups"][0]["selected"] is True
    assert len(calls) == 1

    clock[0] += scanner_target_catalog.STICKER_GROUP_CACHE_TTL_SECONDS + 1
    assert preview()["groups"][0]["target_count"] == 1
    assert len(calls) == 2

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sale_stickers VALUES (2, 'Sticker | Second | Katowice 2014')"
        )
    assert preview()["groups"][0]["target_count"] == 2
    assert len(calls) == 3

    second_path = tmp_path / "other.db"
    with sqlite3.connect(second_path) as conn:
        conn.execute("CREATE TABLE sale_stickers (sticker_id INTEGER, name TEXT)")
    empty = scanner_target_catalog.build_scanner_target_preview(
        second_path,
        "sticker_guns",
        config=config(tmp_path),
        target_control=target_control,
    )
    assert empty["groups"] == []
    assert len(calls) == 4


def test_sticker_group_cache_refreshes_wal_updates_without_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner_target_catalog, "_sticker_group_cache", {})
    clock = [100.0]
    monkeypatch.setattr(scanner_target_catalog.time, "monotonic", lambda: clock[0])
    db_path = tmp_path / "wal-history.db"
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE sale_stickers (sticker_id INTEGER, name TEXT)")
        writer.execute(
            "INSERT INTO sale_stickers VALUES (1, 'Sticker | Team | Katowice 2014')"
        )
        writer.commit()

        def preview():
            return scanner_target_catalog.build_scanner_target_preview(
                db_path,
                "sticker_guns",
                config=config(tmp_path),
                target_control=control(),
            )

        first = preview()
        assert first["groups"][0]["target_count"] == 1
        # Mutating a response must not mutate the shared cached catalog.
        first["groups"][0]["target_count"] = 999
        assert preview()["groups"][0]["target_count"] == 1
        main_stat = db_path.stat()
        writer.execute(
            "INSERT INTO sale_stickers VALUES (2, 'Sticker | Second | Katowice 2014')"
        )
        writer.commit()
        assert db_path.stat().st_mtime_ns == main_stat.st_mtime_ns
        assert db_path.stat().st_size == main_stat.st_size
        clock[0] += scanner_target_catalog.STICKER_GROUP_CACHE_TTL_SECONDS + 1
        assert preview()["groups"][0]["target_count"] == 2
    finally:
        writer.close()


def test_sticker_group_cache_stays_bounded_across_databases(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner_target_catalog, "_sticker_group_cache", {})
    monkeypatch.setattr(scanner_target_catalog, "MAX_STICKER_GROUP_CACHE_ENTRIES", 2)
    calls = []
    original = scanner_target_catalog._sticker_group_catalog

    def counted_catalog(conn):
        calls.append(True)
        return original(conn)

    monkeypatch.setattr(scanner_target_catalog, "_sticker_group_catalog", counted_catalog)
    paths = [tmp_path / f"history-{index}.db" for index in range(3)]
    for path in paths:
        sqlite3.connect(path).close()
        scanner_target_catalog.build_scanner_target_preview(
            path,
            "sticker_guns",
            config=config(tmp_path),
            target_control=control(),
        )
        assert len(scanner_target_catalog._sticker_group_cache) <= 2
    assert len(calls) == 3
    scanner_target_catalog.build_scanner_target_preview(
        paths[0],
        "sticker_guns",
        config=config(tmp_path),
        target_control=control(),
    )
    assert len(calls) == 4
    assert len(scanner_target_catalog._sticker_group_cache) <= 2
