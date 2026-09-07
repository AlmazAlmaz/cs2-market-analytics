import sqlite3
from pathlib import Path

import pytest

import inventory_market


def _create_trend_store(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE trend_daily_market (
                market_hash_name TEXT NOT NULL,
                day TEXT NOT NULL,
                sales_count INTEGER NOT NULL,
                median_price_cents REAL NOT NULL,
                p15_price_cents REAL NOT NULL,
                p85_price_cents REAL NOT NULL
            );
            CREATE TABLE trend_skin_catalog (
                market_hash_name TEXT PRIMARY KEY,
                collection_name TEXT NOT NULL,
                observed_sales_count INTEGER NOT NULL,
                unique_asset_count INTEGER NOT NULL,
                latest_reference_quantity INTEGER,
                latest_reference_at TEXT,
                icon_url TEXT
            );
            CREATE TABLE trend_forecasts (
                as_of_day TEXT NOT NULL,
                market_hash_name TEXT NOT NULL,
                horizon_days INTEGER NOT NULL,
                generated_at TEXT,
                model_version TEXT,
                current_price_cents INTEGER NOT NULL,
                predicted_price_cents INTEGER NOT NULL,
                low_price_cents INTEGER NOT NULL,
                high_price_cents INTEGER NOT NULL,
                expected_return_percent REAL NOT NULL,
                downside_return_percent REAL NOT NULL,
                probability_up REAL NOT NULL,
                confidence REAL NOT NULL,
                history_rows INTEGER NOT NULL,
                last_observation_day TEXT NOT NULL
            );
            """
        )
        conn.executemany(
            "INSERT INTO trend_daily_market VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("AK-47 | Alpha (Field-Tested)", "2026-01-01", 90, 900, 800, 1000),
                ("AK-47 | Alpha (Field-Tested)", "2026-01-15", 3, 1000, 900, 1100),
                ("AK-47 | Alpha (Field-Tested)", "2026-02-01", 5, 1200, 1000, 1500),
                ("Glock-18 | Beta (Factory New)", "2026-01-31", 2, 450, 400, 500),
                ("Literal_100% Skin", "2026-02-01", 1, 100, 90, 110),
            ],
        )
        conn.executemany(
            "INSERT INTO trend_skin_catalog VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "AK-47 | Alpha (Field-Tested)",
                    "The Alpha Collection",
                    123,
                    101,
                    456,
                    "2026-02-01T12:00:00Z",
                    "https://example.test/alpha.png",
                ),
                (
                    "Catalog-only Skin",
                    "The Catalog Collection",
                    0,
                    0,
                    None,
                    None,
                    None,
                ),
            ],
        )
        forecast_rows = [
            (
                "2026-01-20",
                "AK-47 | Alpha (Field-Tested)",
                30,
                "2026-01-20T01:00:00Z",
                "old",
                1000,
                1050,
                900,
                1200,
                5,
                -10,
                0.6,
                0.6,
                30,
                "2026-01-20",
            ),
            (
                "2026-02-01",
                "AK-47 | Alpha (Field-Tested)",
                30,
                "2026-02-01T01:00:00Z",
                "latest",
                1200,
                1500,
                1000,
                1700,
                25,
                -16.67,
                0.7,
                0.8,
                50,
                "2026-02-01",
            ),
            (
                "2026-02-01",
                "AK-47 | Alpha (Field-Tested)",
                7,
                "2026-02-01T01:00:00Z",
                "seven-day",
                1200,
                1300,
                1100,
                1400,
                8.33,
                -8.33,
                0.65,
                0.8,
                50,
                "2026-02-01",
            ),
        ]
        conn.executemany(
            "INSERT INTO trend_forecasts VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            forecast_rows,
        )


def test_search_skin_names_is_bounded_ranked_and_treats_wildcards_literally(
    tmp_path,
):
    store = tmp_path / "trend.db"
    _create_trend_store(store)

    assert inventory_market.search_skin_names(store, "alpha", limit=5) == [
        "AK-47 | Alpha (Field-Tested)"
    ]
    assert inventory_market.search_skin_names(store, "_100%", limit=5) == [
        "Literal_100% Skin"
    ]
    assert inventory_market.search_skin_names(
        store, "%' OR 1=1 --", limit=5
    ) == []
    # The injection-shaped input was only a parameter; the table still exists.
    assert inventory_market.search_skin_names(store, "beta", limit=5) == [
        "Glock-18 | Beta (Factory New)"
    ]

    with pytest.raises(ValueError, match="between 1 and 20"):
        inventory_market.search_skin_names(store, "", limit=21)
    with pytest.raises(ValueError, match="at most 120"):
        inventory_market.search_skin_names(store, "x" * 121)


def test_batch_snapshot_uses_latest_history_rolling_sales_and_latest_30d_forecast(
    tmp_path,
):
    store = tmp_path / "trend.db"
    _create_trend_store(store)

    result = inventory_market.get_market_snapshots(
        store,
        [
            "AK-47 | Alpha (Field-Tested)",
            "Glock-18 | Beta (Factory New)",
            "Missing Skin",
            "AK-47 | Alpha (Field-Tested)",  # deduplicated, stable order
        ],
    )

    assert list(result) == [
        "AK-47 | Alpha (Field-Tested)",
        "Glock-18 | Beta (Factory New)",
        "Missing Skin",
    ]
    alpha = result["AK-47 | Alpha (Field-Tested)"]
    assert alpha["market_data_available"] is True
    assert alpha["market_as_of"] == "2026-02-01"
    assert alpha["market_store_as_of"] == "2026-02-01"
    assert alpha["current_price_cents"] == 1200
    assert alpha["market_median_cents"] == 1200
    assert alpha["p15_price_cents"] == 1000
    assert alpha["p85_price_cents"] == 1500
    # January 1 is outside the global Feb 1 trailing 30-day window.
    assert alpha["sales_30d"] == 8
    assert alpha["collection_name"] == "The Alpha Collection"
    assert alpha["icon_url"] == "https://example.test/alpha.png"
    assert alpha["latest_reference_quantity"] == 456
    assert alpha["observed_sales_count"] == 123
    assert alpha["unique_asset_count"] == 101
    assert alpha["supply"]["is_world_inventory"] is False
    assert "не число предметов в мире" in alpha["supply_note"]
    assert alpha["price_is_live_ask"] is False
    assert "не live ask" in alpha["pricing_label"]

    forecast = alpha["forecast_30d"]
    assert forecast is alpha["forecast"]
    assert forecast["as_of_day"] == "2026-02-01"
    assert forecast["horizon_days"] == 30
    assert forecast["predicted_price_cents"] == 1500
    assert forecast["model_version"] == "latest"
    assert forecast["is_live_ask"] is False

    beta = result["Glock-18 | Beta (Factory New)"]
    assert beta["current_price_cents"] == 450
    assert beta["sales_30d"] == 2
    assert beta["forecast_30d"] is None

    missing = result["Missing Skin"]
    assert missing["market_data_available"] is False
    assert missing["current_price_cents"] is None
    assert missing["market_median_cents"] is None
    assert missing["sales_30d"] is None
    assert missing["forecast"] is None


def test_missing_or_partial_schema_fails_closed_without_creating_a_database(tmp_path):
    missing_path = tmp_path / "does-not-exist.db"
    value = inventory_market.get_market_snapshots(missing_path, ["Skin A"])
    assert value["Skin A"]["current_price_cents"] is None
    assert not missing_path.exists()
    assert inventory_market.search_skin_names(missing_path, "Skin") == []
    assert not missing_path.exists()

    partial = tmp_path / "partial.db"
    with sqlite3.connect(partial) as conn:
        conn.executescript(
            """
            CREATE TABLE trend_daily_market (
                market_hash_name TEXT, day TEXT, sales_count INTEGER,
                median_price_cents INTEGER, p15_price_cents INTEGER
            );
            INSERT INTO trend_daily_market VALUES ('Skin A', '2026-01-01', 1, 100, 90);
            CREATE TABLE trend_skin_catalog (
                market_hash_name TEXT, collection_name TEXT
            );
            INSERT INTO trend_skin_catalog VALUES ('Skin A', 'Collection A');
            CREATE TABLE trend_forecasts (
                as_of_day TEXT, market_hash_name TEXT, horizon_days INTEGER
            );
            INSERT INTO trend_forecasts VALUES ('2026-01-01', 'Skin A', 30);
            """
        )

    snapshot = inventory_market.get_market_snapshots(partial, ["Skin A"])["Skin A"]
    assert snapshot["market_data_available"] is False
    assert snapshot["current_price_cents"] is None
    assert snapshot["collection_name"] is None
    assert snapshot["forecast"] is None
    # Name search has a complete one-column contract and remains safe/useful.
    assert inventory_market.search_skin_names(partial, "Skin") == ["Skin A"]

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    assert inventory_market.search_skin_names(corrupt, "Skin") == []
    assert inventory_market.get_market_snapshots(corrupt, ["Skin A"])["Skin A"][
        "current_price_cents"
    ] is None


def _snapshot(*, median=1000, p15=900, p85=1200, forecast=None):
    return {
        "market_data_available": True,
        "median_price_cents": median,
        "p15_price_cents": p15,
        "p85_price_cents": p85,
        "forecast_30d": forecast,
    }


def test_price_recommendation_accounts_for_fee_staleness_strategy_and_profit():
    balanced = inventory_market.recommend_listing_price(
        _snapshot(),
        buy_price_cents=800,
        fee_percent=10,
        listed_price_cents=1300,
        listed_at="2026-01-01T00:00:00Z",
        strategy="balanced",
        stale_days=7,
        now="2026-01-11T00:00:00Z",
    )

    assert balanced["is_stale"] is True
    assert balanced["listing_age_days"] == 10
    assert balanced["break_even_price_cents"] == 889
    assert balanced["market_target_price_cents"] == 970
    assert balanced["suggested_price_cents"] == 970
    assert balanced["seller_proceeds_cents"] == 873
    assert balanced["projected_profit_cents"] == 73
    assert balanced["unrealized_profit_cents"] == 100
    assert balanced["unrealized_roi_percent"] == 12.5
    assert balanced["listed_seller_proceeds_cents"] == 1170
    assert balanced["listed_profit_cents"] == 370
    assert balanced["listed_roi_percent"] == 46.25
    assert balanced["roi_percent"] == 9.12
    assert balanced["action"] == "lower"
    assert balanced["recommendation_short"] == "Снизить цену"
    assert balanced["benchmark_is_live_ask"] is False
    assert "не текущий live ask" in balanced["warnings"][0]

    quick = inventory_market.recommend_listing_price(
        _snapshot(),
        buy_price_cents=1000,
        fee_percent=10,
        strategy="flip",  # supported account/UI alias
        stale_days=7,
        now="2026-01-11T00:00:00Z",
    )
    assert quick["strategy"] == "quick_sale"
    assert quick["suggested_price_cents"] == 900
    assert quick["projected_profit_cents"] == -190
    assert quick["roi_percent"] == -19.0
    assert quick["market_below_break_even"] is True


def test_recommendation_caps_model_values_and_requires_manual_special_premium():
    forecast = {
        "predicted_price_cents": 50_000,
        "confidence": 0.9,
        "expected_return_percent": 4900,
    }
    recommendation = inventory_market.recommend_listing_price(
        _snapshot(forecast=forecast),
        buy_price_cents=800,
        fee_percent=2,
        strategy="investment",
        special_float=True,
        has_special_stickers=True,
    )

    assert recommendation["suggested_price_cents"] == 1500
    assert recommendation["benchmark_source"] == (
        "historical_p85_plus_capped_30d_model"
    )
    assert recommendation["manual_premium_required"] is True
    assert recommendation["action"] == "manual_review"
    assert recommendation["recommendation_short"] == "Проверь премию вручную"
    assert "float" in recommendation["special_premium_note"]
    assert any("ограничен 150%" in note for note in recommendation["warnings"])

    sticker_category = inventory_market.recommend_listing_price(
        _snapshot(),
        buy_price_cents=800,
        fee_percent=2,
        strategy="stickers",
    )
    assert sticker_category["requested_strategy"] == "stickers"
    assert sticker_category["strategy"] == "balanced"
    assert sticker_category["manual_premium_required"] is True
    assert sticker_category["action"] == "manual_review"


def test_recommendation_never_turns_missing_market_data_into_zero_price():
    recommendation = inventory_market.recommend_listing_price(
        {"market_data_available": False},
        buy_price_cents=500,
        fee_percent=2,
    )

    assert recommendation["action"] == "unavailable"
    assert recommendation["suggested_price_cents"] is None
    assert recommendation["market_target_price_cents"] is None
    assert recommendation["market_median_cents"] is None
    assert recommendation["unrealized_profit_cents"] is None
    assert "не подставлена как $0" in recommendation["reason"]

    with pytest.raises(ValueError, match="between 0 and 50"):
        inventory_market.recommend_listing_price(
            _snapshot(), buy_price_cents=500, fee_percent=100
        )
    with pytest.raises(ValueError, match="strategy must be one of"):
        inventory_market.recommend_listing_price(
            _snapshot(), buy_price_cents=500, fee_percent=2, strategy="magic"
        )


def test_batch_input_is_bounded_before_opening_the_database(tmp_path):
    too_many = [f"Skin {index}" for index in range(inventory_market.MAX_BATCH_NAMES + 1)]
    with pytest.raises(ValueError, match="at most 200"):
        inventory_market.get_market_snapshots(tmp_path / "missing.db", too_many)
