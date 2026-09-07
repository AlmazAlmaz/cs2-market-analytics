import json
import sqlite3

import pytest

import inventory_item_catalog as catalog


def _database(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE cs2_item_catalog (
                market_hash_name TEXT PRIMARY KEY,
                item_kind TEXT NOT NULL,
                image_url TEXT,
                crate_names_json TEXT
            );
            CREATE TABLE steam_market_assets (
                market_hash_name TEXT PRIMARY KEY,
                asset_kind TEXT,
                icon_url TEXT,
                icon_url_large TEXT,
                found INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO cs2_item_catalog VALUES
                ('AWP | Graphite (Factory New)', 'weapon_skin',
                 'https://images.test/graphite.png', '["Bravo Case"]'),
                ('AK-47 | Case Hardened (Field-Tested)', 'weapon_skin',
                 'https://images.test/ak.png', '[]');
            INSERT INTO steam_market_assets VALUES
                ('Sticker | Test', 'sticker', NULL,
                 'https://images.test/sticker.png', 1);
            """
        )


def _containers(path):
    path.write_text(
        json.dumps(
            [
                {
                    "market_hash_name": "Kilowatt Case",
                    "item_kind": "case",
                    "icon_url": "https://images.test/kilowatt.png",
                },
                {
                    "market_hash_name": "10 Year Birthday Sticker Capsule",
                    "item_kind": "capsule",
                    "icon_url": "https://images.test/capsule.png",
                },
                {
                    "market_hash_name": None,
                    "name": "Unreleased Case",
                    "item_kind": "case",
                    "icon_url": "https://images.test/unreleased.png",
                },
            ]
        ),
        encoding="utf-8",
    )


def test_unified_search_finds_skins_stickers_cases_and_capsules(tmp_path):
    database = tmp_path / "items.db"
    containers = tmp_path / "bymykel_crates.json"
    _database(database)
    _containers(containers)

    graphite = catalog.search_items(
        database, "graphite", container_cache_path=containers
    )
    assert graphite == [
        {
            "market_hash_name": "AWP | Graphite (Factory New)",
            "item_kind": "weapon_skin",
            "icon_url": "https://images.test/graphite.png",
        }
    ]
    assert catalog.search_items(
        database, "Sticker | Test", container_cache_path=containers
    )[0]["icon_url"] == "https://images.test/sticker.png"
    assert catalog.lookup_item(
        database, "kilowatt case", container_cache_path=containers
    ) == {
        "market_hash_name": "Kilowatt Case",
        "item_kind": "case",
        "icon_url": "https://images.test/kilowatt.png",
    }
    assert catalog.search_items(
        database, "birthday", container_cache_path=containers
    )[0]["item_kind"] == "capsule"
    assert not catalog.search_items(
        database, "Unreleased", container_cache_path=containers
    )


def test_search_ranks_exact_then_prefix_then_contains_and_escapes_wildcards(
    tmp_path,
):
    database = tmp_path / "items.db"
    containers = tmp_path / "bymykel_crates.json"
    _database(database)
    containers.write_text(
        json.dumps(
            [
                {
                    "market_hash_name": "Case",
                    "item_kind": "case",
                    "icon_url": "https://images.test/exact.png",
                },
                {
                    "market_hash_name": "Case Alpha",
                    "item_kind": "case",
                    "icon_url": "https://images.test/prefix.png",
                },
                {
                    "market_hash_name": "Gamma Case",
                    "item_kind": "case",
                    "icon_url": "https://images.test/contains.png",
                },
            ]
        ),
        encoding="utf-8",
    )
    names = [
        item["market_hash_name"]
        for item in catalog.search_items(
            database, "Case", limit=10, container_cache_path=containers
        )
    ]
    assert names[:2] == ["Case", "Case Alpha"]
    assert "Gamma Case" in names[2:]
    assert catalog.search_items(
        database, "%", container_cache_path=containers
    ) == []


def test_missing_or_corrupt_sources_fail_closed_and_inputs_are_bounded(tmp_path):
    corrupt = tmp_path / "bymykel_crates.json"
    corrupt.write_text("not json", encoding="utf-8")

    assert catalog.search_items(
        tmp_path / "missing.db", "case", container_cache_path=corrupt
    ) == []
    assert catalog.lookup_item(
        tmp_path / "missing.db", "Kilowatt Case", container_cache_path=corrupt
    ) is None
    with pytest.raises(ValueError, match="at most"):
        catalog.search_items(None, "x" * (catalog.MAX_QUERY_LENGTH + 1))
    with pytest.raises(ValueError, match="between 1 and"):
        catalog.search_items(None, "test", limit=0)


def test_real_local_cache_contains_market_images_for_case_and_capsule():
    cache = (
        catalog.PROJECT_DIR
        / ".private"
        / "item_catalog_cache"
        / "bymykel_crates.json"
    )
    if not cache.is_file():
        pytest.skip("local container cache has not been refreshed")

    case = catalog.lookup_item(
        catalog.PROJECT_DIR / "csfloat_history.db", "Kilowatt Case"
    )
    capsule = catalog.lookup_item(
        catalog.PROJECT_DIR / "csfloat_history.db",
        "10 Year Birthday Sticker Capsule",
    )
    assert case and case["item_kind"] == "case" and case["icon_url"]
    assert capsule and capsule["item_kind"] == "capsule" and capsule["icon_url"]
