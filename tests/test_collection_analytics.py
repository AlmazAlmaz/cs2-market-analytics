import sqlite3

import pytest

from TRENDMODEL.api import (
    get_collection_detail,
    get_skin_daily_series,
    list_collections,
)
from TRENDMODEL.storage import connect_store, refresh_daily_aggregates


def _create_enriched_source(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sales (
                listing_id TEXT PRIMARY KEY,
                name TEXT,
                price_cents INTEGER,
                state TEXT,
                sold_at TEXT,
                fetched_at TEXT,
                item_collection_name TEXT,
                item_collection TEXT,
                float_value REAL,
                reference_quantity INTEGER,
                reference_last_updated TEXT,
                item_asset_id TEXT,
                item_icon_url TEXT
            );
            INSERT INTO sales VALUES
                ('a-1', 'Cheap Skin', 100, 'sold',
                 '2026-01-01T01:00:00Z', '2026-01-02T00:00:00Z',
                 NULL, 'Test Collection', 0.10, 1000,
                 '2026-01-02T12:00:00Z', 'asset-a-1', 'old-icon'),
                ('a-2', 'Cheap Skin', 200, 'sold',
                 '2026-01-31T01:00:00Z', '2026-02-01T00:00:00Z',
                 'Test Collection', 'Legacy Wrong Value', 0.05, 900,
                 '2026-02-01T12:00:00Z', 'asset-a-2', 'new-icon'),
                ('b-1', 'Expensive Skin', 10000, 'sold',
                 '2026-01-01T02:00:00Z', '2026-01-02T00:00:00Z',
                 NULL, 'Test Collection', 0.20, 500,
                 '2026-01-02T12:00:00Z', 'asset-b', 'b-icon'),
                ('b-2', 'Expensive Skin', 10000, 'sold',
                 '2026-01-31T02:00:00Z', '2026-02-01T00:00:00Z',
                 'Test Collection', NULL, 0.30, 450,
                 '2026-02-01T12:00:00Z', 'asset-b', 'b-icon');
            """
        )


def test_collection_materialization_uses_constituent_index_and_catalog(tmp_path):
    source_path = tmp_path / "source.db"
    store_path = tmp_path / "trend.db"
    _create_enriched_source(source_path)

    result = refresh_daily_aggregates(source_path, store_path, insert_batch_size=1)
    assert result["catalog_rows"] == 2
    assert result["collection_daily_rows"] == 2
    assert result["collection_mapping_coverage"] == 1

    conn = connect_store(store_path)
    try:
        cheap = dict(
            conn.execute(
                "SELECT * FROM trend_skin_catalog WHERE market_hash_name=?",
                ("Cheap Skin",),
            ).fetchone()
        )
        collection_days = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM trend_collection_daily
                WHERE collection_name='Test Collection'
                ORDER BY day
                """
            )
        ]
    finally:
        conn.close()

    assert cheap["collection_name"] == "Test Collection"
    assert cheap["observed_sales_count"] == 2
    assert cheap["unique_asset_count"] == 2
    assert cheap["min_float"] == pytest.approx(0.05)
    assert cheap["avg_float"] == pytest.approx(0.075)
    assert cheap["max_float"] == pytest.approx(0.10)
    assert cheap["latest_reference_quantity"] == 900
    assert cheap["latest_reference_at"] == "2026-02-01T12:00:00Z"
    assert cheap["icon_url"] == "new-icon"
    assert cheap["mapping_coverage"] == 1

    # Raw-price averaging would barely move because one skin costs 100x more.
    # Constituent normalization gives each skin a base of 100: (200 + 100) / 2.
    assert [row["index_value"] for row in collection_days] == pytest.approx([100, 150])
    assert collection_days[-1]["sales_count"] == 2
    assert collection_days[-1]["notional_cents"] == 10_200
    assert collection_days[-1]["member_count"] == 2
    assert collection_days[-1]["total_member_count"] == 2
    assert collection_days[-1]["member_coverage"] == 1
    assert collection_days[-1]["breadth_up"] == 50


def test_collection_and_skin_read_apis_are_bounded(tmp_path):
    source_path = tmp_path / "source.db"
    store_path = tmp_path / "trend.db"
    _create_enriched_source(source_path)
    refresh_daily_aggregates(source_path, store_path)

    collections = list_collections(store_path, search="Test", limit=1)
    assert collections["status"] == "ready"
    assert collections["total_count"] == 1
    assert collections["has_more"] is False
    summary = collections["items"][0]
    assert summary["collection_name"] == "Test Collection"
    assert summary["member_count"] == 2
    assert summary["observed_sales_count"] == 4
    assert summary["latest_reference_quantity"] == 1_350
    assert summary["quantity_member_count"] == 2
    assert summary["latest_reference_at"] == "2026-02-01T12:00:00Z"
    assert summary["current_index"] == 150
    assert summary["return_30d"] == 50
    assert summary["sales_30d"] == 2
    assert summary["breadth_up"] == 50
    assert summary["direction"] == "up"

    detail = get_collection_detail(
        store_path, "Test Collection", days=1, member_limit=1
    )
    assert detail["status"] == "ready"
    assert len(detail["series"]) == 1
    assert detail["series_truncated"] is True
    assert len(detail["members"]) == 1
    assert detail["members_truncated"] is True
    assert detail["members"][0]["return_7d"] == 100
    assert detail["members"][0]["return_30d"] == 100
    assert detail["members"][0]["sales_30d"] == 1
    assert detail["members"][0]["direction"] == "up"

    calendar_detail = get_collection_detail(store_path, "Test Collection", days=7)
    assert [row["day"] for row in calendar_detail["series"]] == ["2026-01-31"]

    skin = get_skin_daily_series(store_path, "Cheap Skin", days=1)
    assert skin["status"] == "ready"
    assert skin["catalog"]["collection_name"] == "Test Collection"
    assert len(skin["series"]) == 1
    assert skin["series"][0]["median_price_cents"] == 200
    assert skin["truncated"] is True
    calendar_skin = get_skin_daily_series(store_path, "Cheap Skin", days=7)
    assert [row["day"] for row in calendar_skin["series"]] == ["2026-01-31"]

    assert get_collection_detail(store_path, "Missing")["status"] == "not_found"
    assert get_skin_daily_series(store_path, "Missing")["status"] == "not_found"
    with pytest.raises(ValueError, match="between 1 and 5000"):
        get_skin_daily_series(store_path, "Cheap Skin", days=5_001)


def test_collection_returns_fail_closed_when_comparison_coverage_is_low(tmp_path):
    source_path = tmp_path / "source.db"
    store_path = tmp_path / "trend.db"
    _create_enriched_source(source_path)
    refresh_daily_aggregates(source_path, store_path)
    conn = connect_store(store_path)
    try:
        conn.execute(
            """
            UPDATE trend_collection_daily
            SET member_coverage=0.10
            WHERE collection_name='Test Collection' AND day='2026-01-01'
            """
        )
        conn.commit()
    finally:
        conn.close()

    summary = list_collections(store_path)["items"][0]
    assert summary["member_coverage"] == 1
    assert summary["coverage_30d"] == 0.10
    assert summary["minimum_member_coverage"] == 0.20
    assert summary["return_7d"] is None
    assert summary["return_30d"] is None
    assert summary["direction"] == "insufficient_coverage"

    conn = connect_store(store_path)
    try:
        conn.execute(
            """
            UPDATE trend_collection_daily
            SET member_coverage=CASE day
                WHEN '2026-01-01' THEN 1.0
                WHEN '2026-01-31' THEN 0.10
                ELSE member_coverage END
            WHERE collection_name='Test Collection'
            """
        )
        conn.commit()
    finally:
        conn.close()
    current_low = list_collections(store_path)["items"][0]
    assert current_low["member_coverage"] == 0.10
    assert current_low["return_30d"] is None
    assert current_low["direction"] == "insufficient_coverage"


def test_minimal_source_schema_materializes_unknown_collection(tmp_path):
    source_path = tmp_path / "source.db"
    store_path = tmp_path / "trend.db"
    with sqlite3.connect(source_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sales (
                listing_id TEXT PRIMARY KEY,
                name TEXT,
                price_cents INTEGER,
                state TEXT,
                sold_at TEXT
            );
            INSERT INTO sales VALUES
                ('minimal', 'Minimal Skin', 123, 'sold',
                 '2026-01-01T00:00:00Z');
            """
        )

    refresh_daily_aggregates(source_path, store_path)
    result = get_skin_daily_series(store_path, "Minimal Skin")
    assert result["status"] == "ready"
    assert result["catalog"]["collection_name"] == "Unknown"
    assert result["catalog"]["unique_asset_count"] == 0
    assert result["catalog"]["min_float"] is None
    assert result["catalog"]["latest_reference_quantity"] is None
    assert result["catalog"]["mapping_coverage"] == 0
    assert list_collections(store_path)["items"][0]["collection_name"] == "Unknown"


def test_collection_read_api_does_not_initialize_unrelated_database(tmp_path):
    unrelated_path = tmp_path / "unrelated.db"
    with sqlite3.connect(unrelated_path) as conn:
        assert list_collections(conn)["status"] == "unavailable"
        assert get_collection_detail(conn, "Anything")["status"] == "unavailable"
        assert get_skin_daily_series(conn, "Anything")["status"] == "unavailable"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'trend_%'"
            ).fetchone()[0]
            == 0
        )
