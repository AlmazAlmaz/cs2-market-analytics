import re
import shutil
import sqlite3
import subprocess

import pytest

import catalog_browser

CATALOG_DDL = """
CREATE TABLE cs2_item_catalog (
    market_hash_name TEXT PRIMARY KEY,
    item_kind TEXT NOT NULL,
    base_name TEXT,
    weapon_name TEXT,
    paint_name TEXT,
    phase_name TEXT,
    wear_name TEXT,
    min_float REAL,
    max_float REAL,
    stattrak INTEGER,
    souvenir INTEGER,
    collection_names_json TEXT,
    crate_names_json TEXT,
    image_url TEXT,
    source_id TEXT,
    source_updated_at TEXT,
    catalog_build_id TEXT
);
CREATE INDEX idx_cs2_item_catalog_kind
ON cs2_item_catalog(item_kind, market_hash_name);
CREATE INDEX idx_cs2_item_catalog_weapon
ON cs2_item_catalog(weapon_name, paint_name, wear_name);

CREATE TABLE sale_stickers (
    listing_id TEXT NOT NULL,
    sticker_index INTEGER NOT NULL,
    sticker_id INTEGER,
    name TEXT,
    reference_price_cents INTEGER,
    reference_quantity INTEGER,
    icon_url TEXT,
    PRIMARY KEY (listing_id, sticker_index)
);
CREATE INDEX idx_sale_stickers_sticker_id_name
ON sale_stickers(sticker_id, name);
"""


TREND_DDL = """
CREATE TABLE trend_daily_market (
    market_hash_name TEXT NOT NULL,
    day TEXT NOT NULL,
    sales_count INTEGER NOT NULL,
    median_price_cents REAL NOT NULL,
    PRIMARY KEY (market_hash_name, day)
);
CREATE INDEX idx_trend_daily_day
ON trend_daily_market(day, market_hash_name);
CREATE TABLE trend_skin_catalog (
    market_hash_name TEXT PRIMARY KEY,
    observed_sales_count INTEGER,
    unique_asset_count INTEGER,
    latest_reference_quantity INTEGER,
    latest_reference_at TEXT,
    icon_url TEXT
);
"""


def _databases(tmp_path):
    main = tmp_path / "main.db"
    trend = tmp_path / "trend.db"
    redline = "AK-47 | Redline (Field-Tested)"
    unseen = "AK-47 | Unseen (Factory New)"
    exact_collection = "M4A1-S | Test (Minimal Wear)"
    mac10 = "MAC-10 | Neon Rider (Field-Tested)"
    false_positive = "XM1014 | Bone Machine (Field-Tested)"
    glove = "★ Sport Gloves | Vice (Field-Tested)"
    with sqlite3.connect(main) as connection:
        connection.executescript(CATALOG_DDL)
        connection.executemany(
            "INSERT INTO cs2_item_catalog VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    redline,
                    "weapon_skin",
                    "AK-47 | Redline",
                    "AK-47",
                    "Redline",
                    None,
                    "Field-Tested",
                    0.1,
                    0.7,
                    0,
                    0,
                    '["Dust"]',
                    '["Phoenix Case"]',
                    "https://cdn.example/redline.png",
                    "catalog-test",
                    "2026-08-20T00:00:00Z",
                    "build-1",
                ),
                (
                    unseen,
                    "weapon_skin",
                    "AK-47 | Unseen",
                    "AK-47",
                    "Unseen",
                    None,
                    "Factory New",
                    0.0,
                    0.07,
                    0,
                    0,
                    '["Dust II"]',
                    '["Test Case"]',
                    "javascript:alert(1)",
                    "catalog-test",
                    "2026-08-20T00:00:00Z",
                    "build-1",
                ),
                (
                    exact_collection,
                    "weapon_skin",
                    "M4A1-S | Test",
                    "M4A1-S",
                    "Test",
                    None,
                    "Minimal Wear",
                    0.07,
                    0.15,
                    0,
                    0,
                    '["Dust"]',
                    '["Test Case"]',
                    None,
                    "catalog-test",
                    "2026-08-20T00:00:00Z",
                    "build-1",
                ),
                (
                    mac10,
                    "weapon_skin",
                    "MAC-10 | Neon Rider",
                    "MAC-10",
                    "Neon Rider",
                    None,
                    "Field-Tested",
                    0.15,
                    0.38,
                    0,
                    0,
                    '["Chroma"]',
                    '["Chroma 2 Case"]',
                    None,
                    "catalog-test",
                    "2026-08-20T00:00:00Z",
                    "build-1",
                ),
                (
                    false_positive,
                    "weapon_skin",
                    "XM1014 | Bone Machine",
                    "XM1014",
                    "Bone Machine",
                    None,
                    "Field-Tested",
                    0.15,
                    0.38,
                    0,
                    0,
                    '["Cache"]',
                    "[]",
                    None,
                    "catalog-test",
                    "2026-08-20T00:00:00Z",
                    "build-1",
                ),
                (
                    glove,
                    "glove",
                    "★ Sport Gloves | Vice",
                    "Sport Gloves",
                    "Vice",
                    None,
                    "Field-Tested",
                    0.06,
                    0.8,
                    0,
                    0,
                    "[]",
                    '["Clutch Case"]',
                    None,
                    "catalog-test",
                    "2026-08-20T00:00:00Z",
                    "build-1",
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO sale_stickers VALUES (?,?,?,?,?,?,?)",
            [
                (
                    "listing-1",
                    0,
                    62,
                    "Sticker | Team LDLC.com (Holo) | Katowice 2014",
                    123_456,
                    7,
                    "https://cdn.example/ldlc.png",
                ),
                (
                    "listing-2",
                    0,
                    62,
                    "Sticker | Team LDLC.com (Holo) | Katowice 2014",
                    123_456,
                    7,
                    "https://cdn.example/ldlc.png",
                ),
                (
                    "listing-3",
                    0,
                    99,
                    "Sticker | Plain Team | Paris 2023",
                    42,
                    100,
                    "javascript:alert(1)",
                ),
            ],
        )
    with sqlite3.connect(trend) as connection:
        connection.executescript(TREND_DDL)
        connection.executemany(
            "INSERT INTO trend_daily_market VALUES (?,?,?,?)",
            [
                (redline, "2026-07-21", 1, 1_000),
                (redline, "2026-08-13", 2, 1_100),
                (redline, "2026-08-20", 3, 1_210),
                (exact_collection, "2026-08-20", 5, 500),
            ],
        )
        connection.execute(
            "INSERT INTO trend_skin_catalog VALUES (?,?,?,?,?,?)",
            (redline, 91, 44, 1200, "2026-08-20T01:00:00Z", None),
        )
    catalog_browser._clear_sticker_cache_for_tests()
    return main, trend, redline, unseen, glove


def test_skins_enumerate_catalog_including_unobserved_and_join_page_trends(tmp_path):
    main, trend, redline, unseen, _ = _databases(tmp_path)

    payload = catalog_browser.build_catalog_payload(
        main,
        trend,
        kind="weapon_skin",
        filters={"weapon": "AK-47"},
        pagination={"limit": 10, "offset": 0},
    )

    assert payload["status"] == "ready"
    assert payload["tab"] == "skins"
    assert payload["pagination"]["total"] == 2
    assert {item["market_hash_name"] for item in payload["items"]} == {
        redline,
        unseen,
    }
    by_name = {item["market_hash_name"]: item for item in payload["items"]}
    assert by_name[redline]["current_price_cents"] == 1_210
    assert by_name[redline]["trend_7d_percent"] == 10.0
    assert by_name[redline]["trend_30d_percent"] == 21.0
    assert by_name[redline]["sales_30d"] == 5
    assert by_name[redline]["unique_asset_count"] == 44
    assert by_name[unseen]["observed"] is False
    assert by_name[unseen]["current_price_cents"] is None
    assert by_name[unseen]["icon_url"] is None
    assert payload["coverage"]["includes_unobserved"] is True
    assert payload["sources"]["trend"]["queried_market_names"] == 2


def test_catalog_filters_are_exact_and_facets_cover_authoritative_tab(tmp_path):
    main, trend, _, _, _ = _databases(tmp_path)

    dust = catalog_browser.build_catalog_payload(
        main, trend, filters={"collection": "Dust"}, limit=10
    )
    assert dust["pagination"]["total"] == 2
    assert all("Dust" in item["collections"] for item in dust["items"])
    assert all("Dust II" not in item["collections"] for item in dust["items"])
    assert {entry["value"] for entry in dust["facets"]["collections"]} == {
        "Dust",
        "Dust II",
        "Chroma",
        "Cache",
    }

    case = catalog_browser.build_catalog_payload(
        main, trend, search="test", filters={"case": "Test Case"}, limit=10
    )
    assert case["pagination"]["total"] == 1
    assert case["items"][0]["weapon"] == "M4A1-S"


@pytest.mark.parametrize(
    ("search", "expected_weapon", "expected_total"),
    [
        ("MAC 10", "MAC-10", 1),
        ("MAC10", "MAC-10", 1),
        ("mac/10", "MAC-10", 1),
        ("мак 10", "MAC-10", 1),
        ("мак", "MAC-10", 1),
        ("мак10", "MAC-10", 1),
        ("AK", "AK-47", 2),
        ("AK47", "AK-47", 2),
        ("ак 47", "AK-47", 2),
        ("ак", "AK-47", 2),
        ("ак47 unseen", "AK-47", 1),
    ],
)
def test_search_normalizes_weapon_separators_and_russian_aliases(
    tmp_path, search, expected_weapon, expected_total
):
    main, trend, *_ = _databases(tmp_path)

    payload = catalog_browser.build_catalog_payload(
        main, trend, search=search, limit=20
    )

    assert payload["pagination"]["total"] == expected_total
    assert payload["items"]
    assert {item["weapon"] for item in payload["items"]} == {expected_weapon}
    assert payload["filters"]["search"] == search


def test_gloves_have_a_separate_authoritative_tab(tmp_path):
    main, trend, _, _, glove = _databases(tmp_path)

    payload = catalog_browser.build_catalog_payload(main, trend, tab="gloves")

    assert payload["pagination"]["total"] == 1
    assert payload["items"][0]["market_hash_name"] == glove
    assert payload["items"][0]["item_kind"] == "glove"
    assert payload["links"]["knives"] == "/knives"


def test_stickers_are_observed_only_and_counts_are_indexed_placements(tmp_path):
    main, trend, *_ = _databases(tmp_path)

    payload = catalog_browser.build_catalog_payload(
        main,
        trend,
        tab="stickers",
        search="Katowice",
        filters={"sticker_type": "Holo"},
    )

    assert payload["pagination"]["total"] == 1
    sticker = payload["items"][0]
    assert sticker["sticker_id"] == 62
    assert sticker["sticker_type"] == "Holo"
    assert sticker["observed_placements"] == 2
    assert sticker["reference_price_cents"] == 123_456
    assert payload["coverage"]["authoritative"] is False
    assert payload["coverage"]["scope"] == "observed_on_items"
    assert "not the complete CS2 sticker schema" in payload["coverage"]["disclaimer_en"]
    assert payload["sources"]["trend"]["state"] == "not_applicable"


def test_pagination_and_public_validation_are_bounded(tmp_path):
    main, trend, *_ = _databases(tmp_path)

    payload = catalog_browser.build_catalog_payload(
        main, trend, pagination={"limit": 1, "offset": 1}
    )
    assert payload["pagination"] == {
        "limit": 1,
        "offset": 1,
        "total": 5,
        "has_more": True,
    }
    assert len(payload["items"]) == 1

    with pytest.raises(ValueError, match="limit must be between"):
        catalog_browser.build_catalog_payload(main, trend, limit=101)
    with pytest.raises(ValueError, match="tab must be"):
        catalog_browser.build_catalog_payload(main, trend, tab="knives")
    with pytest.raises(ValueError, match="control characters"):
        catalog_browser.build_catalog_payload(main, trend, search="AK\n47")


def test_missing_databases_fail_closed_without_creating_files(tmp_path):
    main = tmp_path / "missing-main.db"
    trend = tmp_path / "missing-trend.db"

    payload = catalog_browser.build_catalog_payload(main, trend)

    assert payload["status"] == "unavailable"
    assert payload["items"] == []
    assert payload["sources"]["catalog"]["reason"] == "database_missing"
    assert not main.exists()
    assert not trend.exists()


def test_catalog_page_is_self_contained_and_localized():
    russian = catalog_browser.render_catalog_page("ru")
    english = catalog_browser.render_catalog_page("en")

    assert '<html lang="ru">' in russian
    assert "Все скины" in russian
    assert '<html lang="en">' in english
    assert "Every skin" in english
    assert "<title>Catalogue · CS2 Market Lab</title>" in english
    assert "/api/catalog" in english
    assert "/api/market/skin-series" in english
    assert "/api/sticker-detail" in english
    assert 'href="/knives"' in english
    assert 'class="active" aria-current="page" href="/catalog"' in english
    assert 'id="catalogDetail"' in english
    assert ".field[hidden]{display:none!important}" in english
    assert "card.tabIndex=0" in english
    assert "event.key!=='Enter'&&event.key!==' '" in english
    assert "prices of skins carrying the sticker" in english
    assert "not the standalone market price of the sticker" in english
    assert "steamcommunity.com/market/listings/730/" in english
    assert (
        "const money=c=>c!==null&&c!==undefined&&c!==''&&Number.isFinite(Number(c))?"
        in english
    )
    assert "const money=c=>Number.isFinite(Number(c))?" not in english
    assert "card.dataset.stickerId=String(item.sticker_id)" in english
    assert "params.set('sticker_id',card.dataset.stickerId||'')" in english
    assert "referencePrice:'reference'" in english
    assert "${tr('referencePrice')} ${money(item.reference_price_cents)}" in english
    assert "hasNumber(row?.[valueField])" in english
    assert (
        "initialFields={search:'#search',weapon:'#weapon',wear:'#wear',"
        "collection:'#collection',case:'#case',sticker_type:'#stickerType'}"
        in english
    )
    assert "__LOCALE__" not in russian + english


def test_catalog_inline_javascript_passes_node_syntax_check(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    page = catalog_browser.render_catalog_page("en")
    scripts = re.findall(r"<script>(.*?)</script>", page, flags=re.DOTALL)
    assert len(scripts) == 2
    script_path = tmp_path / "catalog-inline.js"
    script_path.write_text("\n".join(scripts), encoding="utf-8")

    result = subprocess.run(
        [node, "--check", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
