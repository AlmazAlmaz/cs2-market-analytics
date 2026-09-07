import inspect
import json
import math
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

import dashboard_server
import knives_dashboard

CATALOG_DDL = """
CREATE TABLE cs2_item_catalog (
    market_hash_name TEXT PRIMARY KEY,
    item_kind TEXT NOT NULL,
    base_name TEXT,
    weapon_id TEXT,
    weapon_name TEXT,
    weapon_def_index INTEGER,
    paint_id TEXT,
    paint_index INTEGER,
    paint_name TEXT,
    phase_id TEXT,
    phase_name TEXT,
    wear_id TEXT,
    wear_name TEXT,
    min_float REAL,
    max_float REAL,
    stattrak INTEGER,
    souvenir INTEGER,
    collection_ids_json TEXT,
    collection_names_json TEXT,
    crate_ids_json TEXT,
    crate_names_json TEXT,
    image_url TEXT,
    source_id TEXT,
    source_updated_at TEXT,
    catalog_build_id TEXT,
    paint_indices_json TEXT,
    phase_ids_json TEXT,
    phase_names_json TEXT,
    variant_count INTEGER
);
CREATE TABLE cs2_item_catalog_variants (
    market_hash_name TEXT NOT NULL,
    source_variant_id TEXT NOT NULL,
    paint_id TEXT,
    paint_index INTEGER,
    paint_name TEXT,
    phase_id TEXT,
    phase_name TEXT,
    image_url TEXT,
    source_id TEXT,
    PRIMARY KEY (market_hash_name, source_variant_id)
);
CREATE TABLE cs2_item_catalog_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


TREND_DDL = """
CREATE TABLE trend_daily_market (
    market_hash_name TEXT NOT NULL,
    day TEXT NOT NULL,
    sales_count INTEGER NOT NULL,
    median_price_cents INTEGER,
    PRIMARY KEY (market_hash_name, day)
);
CREATE TABLE trend_skin_catalog (
    market_hash_name TEXT PRIMARY KEY,
    collection_name TEXT,
    observed_sales_count INTEGER,
    unique_asset_count INTEGER,
    min_float REAL,
    max_float REAL,
    latest_reference_quantity INTEGER,
    latest_reference_at TEXT,
    icon_url TEXT
);
"""


def _build_catalog(path):
    karambit = "★ Karambit | Doppler (Factory New)"
    m9 = "★ M9 Bayonet | Fade (Minimal Wear)"
    odd = "Catalog Knife Without Star"
    with sqlite3.connect(path) as connection:
        connection.executescript(CATALOG_DDL)
        rows = [
            (
                karambit,
                "knife",
                "Karambit",
                "weapon_karambit",
                "Karambit",
                507,
                "doppler",
                None,
                "Doppler",
                None,
                None,
                "factory_new",
                "Factory New",
                0.0,
                0.08,
                0,
                0,
                '["collection-doppler"]',
                '["Doppler Collection"]',
                '["crate-one"]',
                '["Chroma Case"]',
                "https://cdn.example/karambit.png",
                "catalog-test",
                "2026-02-01T00:00:00+00:00",
                "build-1",
                "[415, 416]",
                '["ruby", "sapphire"]',
                '["Ruby", "Sapphire"]',
                2,
            ),
            (
                m9,
                "knife",
                "M9 Bayonet",
                "weapon_m9",
                "M9 Bayonet",
                508,
                "fade",
                38,
                "Fade",
                None,
                None,
                "minimal_wear",
                "Minimal Wear",
                0.0,
                0.08,
                0,
                0,
                "[]",
                "[]",
                "[]",
                "[]",
                "javascript:alert(1)",
                "catalog-test",
                "2026-02-01T00:00:00+00:00",
                "build-1",
                "[38]",
                "[]",
                "[]",
                1,
            ),
            (
                odd,
                "knife",
                "Test Knife",
                "weapon_test",
                "Test Knife",
                999,
                "test",
                777,
                "Test Finish",
                None,
                None,
                None,
                None,
                None,
                None,
                0,
                0,
                "[]",
                "[]",
                "[]",
                "[]",
                None,
                "catalog-test",
                "2026-02-01T00:00:00+00:00",
                "build-1",
                "[777]",
                "[]",
                "[]",
                1,
            ),
            (
                "★ Sport Gloves | Vice (Minimal Wear)",
                "glove",
                "Sport Gloves",
                None,
                "Sport Gloves",
                None,
                None,
                None,
                "Vice",
                None,
                None,
                None,
                "Minimal Wear",
                None,
                None,
                0,
                0,
                "[]",
                "[]",
                "[]",
                "[]",
                None,
                "catalog-test",
                None,
                "build-1",
                "[]",
                "[]",
                "[]",
                1,
            ),
        ]
        connection.executemany(
            "INSERT INTO cs2_item_catalog VALUES ("
            + ",".join("?" for _ in range(29))
            + ")",
            rows,
        )
        connection.executemany(
            "INSERT INTO cs2_item_catalog_variants VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    karambit,
                    "ruby-415",
                    "doppler",
                    415,
                    "Doppler",
                    "ruby",
                    "Ruby",
                    "https://cdn.example/ruby.png",
                    "catalog-test",
                ),
                (
                    karambit,
                    "sapphire-416",
                    "doppler",
                    416,
                    "Doppler",
                    "sapphire",
                    "Sapphire",
                    "https://cdn.example/sapphire.png",
                    "catalog-test",
                ),
                (
                    "orphan",
                    "orphan-phase",
                    None,
                    999,
                    None,
                    "orphan",
                    "Orphan",
                    None,
                    "catalog-test",
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO cs2_item_catalog_meta VALUES (?,?,?)",
            [
                ("active_build_id", "build-1", "2026-02-01T00:01:00+00:00"),
                ("kind_counts_json", '{"knife":3,"glove":1}', "2026-02-01T00:01:00+00:00"),
                (
                    "primary_source_url",
                    "https://catalog.example/items.json?token=private",
                    "2026-02-01T00:01:00+00:00",
                ),
            ],
        )
    return karambit, m9, odd


def _build_trend(path, names):
    karambit, _m9, odd = names
    with sqlite3.connect(path) as connection:
        connection.executescript(TREND_DDL)
        connection.executemany(
            "INSERT INTO trend_daily_market VALUES (?,?,?,?)",
            [
                (karambit, "2026-01-02", 100, 10_000),
                (karambit, "2026-01-15", 5, 12_000),
                (karambit, "2026-01-25", 4, 16_000),
                (karambit, "2026-02-01", 3, 20_000),
                (odd, "2026-01-01", 1, 9_000),
                (odd, "2026-02-01", 2, 10_000),
            ],
        )
        connection.executemany(
            "INSERT INTO trend_skin_catalog VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    karambit,
                    "Legacy Trend Collection",
                    112,
                    77,
                    0.001,
                    0.079,
                    19,
                    "2026-02-01T00:00:00+00:00",
                    "https://cdn.example/trend.png",
                ),
                (odd, None, 3, 3, None, None, None, None, None),
            ],
        )


def test_parse_and_fallback_classification_are_conservative():
    parsed = knives_dashboard.parse_knife_name(
        "★ StatTrak™ Butterfly Knife | Doppler Phase 2 (Factory New)"
    )
    assert parsed == {
        "market_hash_name": "★ StatTrak™ Butterfly Knife | Doppler Phase 2 (Factory New)",
        "knife_type": "Butterfly Knife",
        "finish": "Doppler Phase 2",
        "phase": "Phase 2",
        "wear": "Factory New",
        "stattrak": True,
    }
    assert knives_dashboard.is_canonical_knife_name(
        "★ Karambit | Fade (Factory New)"
    )
    assert not knives_dashboard.is_canonical_knife_name(
        "★ Sport Gloves | Vice (Minimal Wear)"
    )
    assert not knives_dashboard.is_canonical_knife_name(
        "AK-47 | Slate (Field-Tested)"
    )


def test_authoritative_catalog_groups_variants_and_attaches_history(tmp_path):
    catalog_path = tmp_path / "catalog.db"
    trend_path = tmp_path / "trend.db"
    names = _build_catalog(catalog_path)
    _build_trend(trend_path, names)

    payload = knives_dashboard.build_knives_payload(catalog_path, trend_path)

    assert payload["status"] == "ready"
    assert payload["summary"]["total_knives"] == 3
    assert {item["market_hash_name"] for item in payload["items"]} == set(names)
    assert payload["sources"]["catalog_fallback_used"] is False
    assert payload["sources"]["catalog"]["metadata"]["kind_counts_json"] == {
        "knife": 3,
        "glove": 1,
    }
    assert "primary_source_url" not in payload["sources"]["catalog"]["metadata"]

    items = {item["market_hash_name"]: item for item in payload["items"]}
    karambit = items[names[0]]
    assert karambit["phases"] == ["Ruby", "Sapphire"]
    assert karambit["phase_ids"] == ["ruby", "sapphire"]
    assert karambit["paint_indices"] == [415, 416]
    assert [variant["source_variant_id"] for variant in karambit["variants"]] == [
        "ruby-415",
        "sapphire-416",
    ]
    assert karambit["variant_count"] == 2
    assert karambit["current_price_cents"] == 20_000
    assert karambit["trend_7d_percent"] == 25.0
    assert karambit["trend_30d_percent"] == 100.0
    assert karambit["sales_30d"] == 12
    assert math.isclose(
        karambit["liquidity_score"],
        100 * math.log1p(12 / 30) / math.log1p(30),
        abs_tol=0.0001,
    )
    assert karambit["collections"] == ["Doppler Collection"]
    assert karambit["unique_asset_count"] == 77
    assert karambit["knife_type"] == "Karambit"

    assert items[names[1]]["current_price_cents"] is None
    assert items[names[1]]["icon_url"] is None
    assert items[names[2]]["current_price_cents"] == 10_000
    assert payload["model_status"] == {
        "model_id": "knives",
        "item_kind": "knife",
        "state": "collecting_data",
        "dedicated_model_available": False,
        "training_action_available": False,
        "forecast_used": False,
        "fallback": "observed_market_history",
        "observed_horizons_days": [7, 30],
    }


def test_case_metadata_is_a_collection_or_case_facet(tmp_path):
    catalog_path = tmp_path / "catalog.db"
    trend_path = tmp_path / "trend.db"
    names = _build_catalog(catalog_path)
    _build_trend(trend_path, names)

    payload = knives_dashboard.build_knives_payload(catalog_path, trend_path)
    facet_values = {row["value"] for row in payload["facets"]["collections"]}
    assert {"Doppler Collection", "Chroma Case"} <= facet_values

    filtered = knives_dashboard.build_knives_payload(
        catalog_path, trend_path, collection="Chroma Case"
    )
    assert [row["market_hash_name"] for row in filtered["items"]] == [names[0]]


def test_market_windows_use_global_as_of_and_fail_closed_for_stale_price(tmp_path):
    catalog_path = tmp_path / "catalog.db"
    trend_path = tmp_path / "trend.db"
    names = _build_catalog(catalog_path)
    _build_trend(trend_path, names)
    with sqlite3.connect(trend_path) as connection:
        connection.execute(
            "INSERT INTO trend_daily_market VALUES (?,?,?,?)",
            ("AK-47 | Slate (Field-Tested)", "2026-03-15", 20, 700),
        )

    payload = knives_dashboard.build_knives_payload(catalog_path, trend_path)
    items = {row["market_hash_name"]: row for row in payload["items"]}
    stale = items[names[0]]

    assert payload["summary"]["as_of_day"] == "2026-03-15"
    assert stale["as_of_day"] == "2026-03-15"
    assert stale["last_observed_day"] == "2026-02-01"
    assert stale["last_observed_price_cents"] == 20_000
    assert stale["price_is_stale"] is True
    assert stale["current_price_cents"] is None
    assert stale["sales_30d"] == 0
    assert stale["trend_7d_percent"] is None
    assert stale["trend_30d_percent"] is None
    assert stale["direction"] == "no_data"


def test_knives_payload_includes_only_fresh_knife_scanner_memberships(tmp_path):
    catalog_path = tmp_path / "catalog.db"
    trend_path = tmp_path / "trend.db"
    names = _build_catalog(catalog_path)
    _build_trend(trend_path, names)
    now = datetime.now(timezone.utc)
    fresh = now.isoformat()
    stale = (now - timedelta(days=3)).isoformat()
    with sqlite3.connect(catalog_path) as connection:
        connection.executescript(
            """
            CREATE TABLE model_deal_predictions (
                listing_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                skin_name TEXT,
                buy_price_cents INTEGER,
                verdict TEXT,
                profit_cents INTEGER,
                roi_percent REAL,
                float_value REAL,
                csfloat_url TEXT,
                PRIMARY KEY (listing_id, model_name)
            );
            CREATE TABLE model_deal_listing_sources (
                listing_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO model_deal_predictions VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "fresh-knife",
                    "knives",
                    fresh,
                    names[0],
                    15_000,
                    "good",
                    2_000,
                    13.33,
                    0.012345,
                    "https://csfloat.com/item/fresh-knife",
                ),
                (
                    "stale-knife",
                    "knives",
                    stale,
                    names[0],
                    14_000,
                    "good",
                    3_000,
                    21.4,
                    0.01,
                    "https://csfloat.com/item/stale-knife",
                ),
                (
                    "wrong-source",
                    "knives",
                    fresh,
                    names[0],
                    13_000,
                    "good",
                    4_000,
                    30.7,
                    0.009,
                    "https://evil.example/not-csfloat",
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO model_deal_listing_sources VALUES (?,?,?)",
            [
                ("fresh-knife", "knife", fresh),
                ("stale-knife", "knife", stale),
                ("wrong-source", "sticker", fresh),
            ],
        )

    payload = knives_dashboard.build_knives_payload(catalog_path, trend_path)
    offers = payload["opportunities"]

    assert offers["state"] == "ready"
    assert offers["availability"] == "recently_observed"
    assert offers["availability_guaranteed"] is False
    assert [row["listing_id"] for row in offers["items"]] == ["fresh-knife"]
    assert offers["items"][0]["model_name"] == "knives"
    assert offers["items"][0]["csfloat_url"] == (
        "https://csfloat.com/item/fresh-knife"
    )


def test_catalog_base_name_with_finish_does_not_fragment_knife_type(tmp_path):
    catalog_path = tmp_path / "catalog.db"
    trend_path = tmp_path / "trend.db"
    names = _build_catalog(catalog_path)
    with sqlite3.connect(catalog_path) as connection:
        connection.execute(
            "UPDATE cs2_item_catalog SET base_name=? WHERE market_hash_name=?",
            ("★ Karambit | Doppler", names[0]),
        )
    _build_trend(trend_path, names)

    payload = knives_dashboard.build_knives_payload(catalog_path, trend_path)
    item = next(row for row in payload["items"] if row["market_hash_name"] == names[0])

    assert item["knife_type"] == "Karambit"
    assert {facet["value"] for facet in payload["facets"]["knife_types"]} >= {
        "Karambit",
        "M9 Bayonet",
    }


def test_phase_search_filter_and_pagination_work_on_grouped_parent(tmp_path):
    catalog_path = tmp_path / "catalog.db"
    trend_path = tmp_path / "trend.db"
    names = _build_catalog(catalog_path)
    _build_trend(trend_path, names)

    ruby = knives_dashboard.build_knives_payload(
        catalog_path, trend_path, phase="Ruby"
    )
    assert [item["market_hash_name"] for item in ruby["items"]] == [names[0]]
    assert {facet["value"] for facet in ruby["facets"]["phases"]} == {
        "Ruby",
        "Sapphire",
    }

    searched = knives_dashboard.build_knives_payload(
        catalog_path, trend_path, search="sapphire"
    )
    assert [item["market_hash_name"] for item in searched["items"]] == [names[0]]

    priced = knives_dashboard.build_knives_payload(
        catalog_path,
        trend_path,
        min_price="150",
        max_price="250",
        direction="up",
    )
    assert [item["market_hash_name"] for item in priced["items"]] == [names[0]]

    paged = knives_dashboard.build_knives_payload(
        catalog_path, trend_path, sort="name", limit=1, offset=1
    )
    assert paged["summary"]["filtered_count"] == 3
    assert paged["summary"]["shown_count"] == 1
    assert paged["pagination"]["has_more"] is True


def test_missing_catalog_uses_only_canonical_knife_names(tmp_path):
    trend_path = tmp_path / "trend.db"
    with sqlite3.connect(trend_path) as connection:
        connection.executescript(TREND_DDL)
        connection.executemany(
            "INSERT INTO trend_daily_market VALUES (?,?,?,?)",
            [
                ("★ Butterfly Knife | Slaughter (Factory New)", "2026-02-01", 4, 30_000),
                ("★ Sport Gloves | Vice (Minimal Wear)", "2026-02-01", 9, 80_000),
                ("AK-47 | Slate (Field-Tested)", "2026-02-01", 20, 500),
            ],
        )

    payload = knives_dashboard.build_knives_payload(
        tmp_path / "missing.db", trend_path
    )

    assert payload["sources"]["catalog_fallback_used"] is True
    assert [item["market_hash_name"] for item in payload["items"]] == [
        "★ Butterfly Knife | Slaughter (Factory New)"
    ]
    assert not (tmp_path / "missing.db").exists()


def test_empty_ready_stores_report_collecting_without_a_fake_model(tmp_path):
    catalog_path = tmp_path / "catalog.db"
    trend_path = tmp_path / "trend.db"
    with sqlite3.connect(catalog_path) as connection:
        connection.executescript(CATALOG_DDL)
    with sqlite3.connect(trend_path) as connection:
        connection.executescript(TREND_DDL)

    payload = knives_dashboard.build_knives_payload(catalog_path, trend_path)

    assert payload["status"] == "collecting_data"
    assert payload["items"] == []
    assert payload["model_status"]["state"] == "collecting_data"
    assert payload["model_status"]["dedicated_model_available"] is False
    assert payload["model_status"]["forecast_used"] is False


def test_invalid_filters_fail_before_unbounded_work(tmp_path):
    path = tmp_path / "missing.db"
    cases = [
        {"min_price": "20", "max_price": "10"},
        {"min_liquidity": "nan"},
        {"direction": "future"},
        {"sort": "forecast"},
        {"limit": 201},
        {"offset": -1},
        {"search": "x" * 181},
    ]
    for filters in cases:
        try:
            knives_dashboard.build_knives_payload(path, path, **filters)
        except ValueError:
            pass
        else:
            raise AssertionError(f"filter should fail: {filters}")


def test_page_is_bilingual_honest_and_uses_existing_series_api():
    ru = knives_dashboard.render_knives_page("ru")
    en = knives_dashboard.render_knives_page("en")

    assert '<html lang="ru">' in ru
    assert '<html lang="en">' in en
    assert "__LOCALE__" not in ru + en
    assert "/api/market/knives" in ru
    assert "/api/market/skin-series" in ru
    assert "Отдельной модели ножей пока нет" in ru
    assert "There is no dedicated knife model yet" in en
    assert 'data-training-action' not in ru
    assert "method:'POST'" not in ru
    assert "forecast_used" not in ru
    assert 'href="/knives"' in ru

    if shutil.which("node"):
        scripts = re.findall(r"<script>(.*?)</script>", ru, flags=re.DOTALL)
        assert scripts
        subprocess.run(
            ["node", "--check"],
            input=scripts[-1],
            text=True,
            check=True,
            capture_output=True,
        )


def test_dashboard_wrapper_nav_and_route_contract(monkeypatch, tmp_path):
    dashboard_server.PAYLOAD_CACHE.clear()
    captured = {}

    def fake_builder(catalog_path, trend_path, **filters):
        captured.update(
            {"catalog_path": catalog_path, "trend_path": trend_path, **filters}
        )
        return {"status": "ready", "items": []}

    monkeypatch.setattr(dashboard_server.knives_dashboard, "build_knives_payload", fake_builder)
    monkeypatch.setattr(dashboard_server, "DB_PATH", str(tmp_path / "catalog.db"))
    monkeypatch.setattr(dashboard_server, "TREND_DB_PATH", str(tmp_path / "trend.db"))

    result = dashboard_server.build_knives_dashboard_payload(
        search="ruby", phase="Ruby", limit="25", offset="50"
    )
    assert result["status"] == "ready"
    assert captured["catalog_path"].endswith("catalog.db")
    assert captured["trend_path"].endswith("trend.db")
    assert captured["search"] == "ruby"
    assert captured["phase"] == "Ruby"
    assert captured["limit"] == "25"
    assert captured["offset"] == "50"

    legacy = dashboard_server.render_private_html(dashboard_server.DEAL_HTML, "en")
    assert 'data-nav-path="/knives"' not in legacy
    assert legacy.count('data-nav-path="/catalog"') == 1
    catalog = dashboard_server.simplify_private_interface(
        dashboard_server.catalog_browser.render_catalog_page("en"),
        "en",
        page="catalog",
    )
    assert catalog.count('href="/knives"') == 1
    assert 'data-i18n="knivesWorkspace"' in catalog
    assert "knivesWorkspace:'Knives and charts ↗'" in catalog

    handler_source = inspect.getsource(dashboard_server.Handler.do_GET)
    assert 'parsed.path == "/knives"' in handler_source
    assert 'parsed.path == "/api/market/knives"' in handler_source
    assert "/api/market/knives" not in inspect.getsource(
        dashboard_server.is_admin_authorized
    )


def test_active_knife_model_requires_verified_generation(monkeypatch, tmp_path):
    bundle = tmp_path / "verified"
    bundle.mkdir()
    (bundle / "knife_price_model.joblib").write_bytes(b"model")
    (bundle / "knife_price_model_metrics.json").write_text(
        json.dumps({"model_name": "knives", "item_kind": "knife"}),
        encoding="utf-8",
    )
    pointer = {
        "generation_id": "a" * 64,
        "activated_at": "2026-08-21T10:00:00+00:00",
    }
    calls = []
    monkeypatch.setattr(
        dashboard_server.model_generations,
        "read_active_pointer",
        lambda *_args, **_kwargs: pointer,
    )

    def resolve(*_args, **kwargs):
        calls.append(kwargs.get("verify_files"))
        return bundle

    monkeypatch.setattr(
        dashboard_server.model_generations,
        "active_or_legacy_bundle_dir",
        resolve,
    )

    result = dashboard_server._verified_active_knife_generation(tmp_path)

    # Other UI tests may still have a bounded dashboard health refresh finishing
    # in the background; those calls use verify_files=False.  This contract only
    # requires the active knife-generation path itself to verify exactly once.
    assert calls.count(True) == 1
    assert result["dedicated_model_available"] is True
    assert result["activation_state"] == "activated"
    assert result["active_bundle_version"] == "a" * 64


def test_knife_status_uses_real_job_and_eligibility_without_forecast(monkeypatch):
    monkeypatch.setattr(
        dashboard_server,
        "_verified_active_knife_generation",
        lambda: {
            "dedicated_model_available": False,
            "activation_state": "not_activated",
            "active_bundle_version": None,
            "activated_at": None,
            "generation_error": None,
        },
    )
    monkeypatch.setattr(
        dashboard_server.model_training_jobs,
        "get_training_overview",
        lambda **_kwargs: {
            "training_active": False,
            "models": {"knives": {"state": "idle", "phase": "idle"}},
        },
    )
    monkeypatch.setattr(
        dashboard_server,
        "_cached_model_training_eligibility",
        lambda **_kwargs: {
            "knives": {
                "bootstrap": True,
                "eligible": True,
                "reasons": [],
                "current_rows": 12_500,
                "runtime_valid_new_rows": 8_100,
                "thresholds": {
                    "min_new_rows": 12_000,
                    "min_runtime_valid_new_rows": 8_000,
                },
            }
        },
    )

    result = dashboard_server._knife_model_dashboard_status("collecting_data")

    assert result["state"] == "ready_to_train"
    assert result["training_action_available"] is True
    assert result["eligibility"]["current_rows"] == 12_500
    assert result["forecast_used"] is False
    assert result["fallback"] == "observed_market_history"
