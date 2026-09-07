import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import dashboard_server
import deal_intelligence
import deal_scanner
import market_analytics


def _trend_store(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE trend_daily_market (
                market_hash_name TEXT, day TEXT, sales_count INTEGER,
                median_price_cents REAL, mean_price_cents REAL,
                min_price_cents INTEGER, max_price_cents INTEGER,
                p15_price_cents REAL, p85_price_cents REAL,
                notional_cents INTEGER
            );
            CREATE TABLE trend_forecasts (
                as_of_day TEXT, market_hash_name TEXT, horizon_days INTEGER,
                current_price_cents INTEGER, predicted_price_cents INTEGER,
                low_price_cents INTEGER, high_price_cents INTEGER,
                expected_return_percent REAL, downside_return_percent REAL,
                probability_up REAL, confidence REAL, history_rows INTEGER
            );
            """
        )
        for day, alpha_price, beta_price in (
            ("2026-01-01", 800, 400),
            ("2026-01-24", 900, 420),
            ("2026-01-30", 1_000, 450),
        ):
            conn.executemany(
                "INSERT INTO trend_daily_market VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("AK-47 | Alpha (Field-Tested)", day, 30, alpha_price, alpha_price, alpha_price, alpha_price, alpha_price, alpha_price, alpha_price * 30),
                    ("Glock-18 | Beta (Field-Tested)", day, 3, beta_price, beta_price, beta_price, beta_price, beta_price, beta_price, beta_price * 3),
                ],
            )
        for name, current, expected, probability in (
            ("AK-47 | Alpha (Field-Tested)", 1_000, 12, 0.7),
            ("Glock-18 | Beta (Field-Tested)", 450, 5, 0.55),
        ):
            for horizon in (7, 30):
                conn.execute(
                    "INSERT INTO trend_forecasts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "2026-01-30", name, horizon, current,
                        round(current * (1 + expected / 100)),
                        round(current * 0.92), round(current * 1.2),
                        expected, -8, probability, 0.7, 90,
                    ),
                )


def test_skin_snapshot_deduplicates_phase_rows_and_labels_source(tmp_path):
    path = tmp_path / "skins.json"
    path.write_text(
        json.dumps(
            [
                {"market_hash_name": "Skin A", "quantity_on_market": 10, "base_price_usd": 9, "paint_index": 1},
                {"market_hash_name": "Skin A", "quantity_on_market": 7, "base_price_usd": 8, "paint_index": 2},
                {"market_hash_name": "Skin B", "quantity_on_market": 2, "base_price_usd": 11},
            ]
        ),
        encoding="utf-8",
    )

    value = market_analytics.load_skin_snapshot(path)

    assert value["raw_rows"] == 3
    assert value["unique_names"] == 2
    assert value["duplicate_names"] == 1
    assert value["items"]["Skin A"]["quantity_on_market_estimate"] == 17
    assert value["items"]["Skin A"]["base_price_cents"] == 800
    assert value["snapshot_at"]


def test_liquid_market_applies_inclusive_price_velocity_and_search(tmp_path):
    trend = tmp_path / "trend.db"
    history = tmp_path / "history.db"
    skins = tmp_path / "skins.json"
    _trend_store(trend)
    sqlite3.connect(history).close()
    skins.write_text(
        json.dumps(
            [
                {"market_hash_name": "AK-47 | Alpha (Field-Tested)", "quantity_on_market": 123, "base_price_usd": 10},
                {"market_hash_name": "Glock-18 | Beta (Field-Tested)", "quantity_on_market": 50, "base_price_usd": 4.5},
            ]
        ),
        encoding="utf-8",
    )

    payload = market_analytics.build_liquid_market_payload(
        history,
        trend,
        skins,
        max_price_cents=1_000,
        min_sales_per_day=3,
        min_liquidity_score=0,
        search="alpha",
        limit=10,
    )

    assert [item["market_hash_name"] for item in payload["items"]] == [
        "AK-47 | Alpha (Field-Tested)"
    ]
    item = payload["items"][0]
    assert item["current_price_cents"] == 1_000
    assert item["current_price_observed_day"] == "2026-01-30"
    assert item["current_price_is_live_ask"] is False
    assert item["quantity_on_market_estimate"] == 123
    assert payload["pricing"]["is_live_ask"] is False
    assert payload["supply"]["is_world_inventory"] is False
    assert payload["live_deals"] == []

    with pytest.raises(ValueError, match="between 1 and 1000"):
        market_analytics.build_liquid_market_payload(
            history,
            trend,
            skins,
            max_price_cents=1_001,
        )


def test_liquid_search_reaches_candidates_beyond_the_old_two_thousand_cutoff(
    tmp_path,
):
    trend = tmp_path / "trend.db"
    with sqlite3.connect(trend) as conn:
        conn.execute(
            """
            CREATE TABLE trend_daily_market (
                market_hash_name TEXT NOT NULL,
                day TEXT NOT NULL,
                sales_count INTEGER NOT NULL,
                median_price_cents INTEGER NOT NULL,
                notional_cents INTEGER NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO trend_daily_market VALUES (?, '2026-08-14', 1, 500, 500)",
            [(f"Skin {index:04d}",) for index in range(2_001)],
        )
        conn.execute(
            "INSERT INTO trend_daily_market VALUES "
            "('ZZZ target after old cutoff', '2026-08-14', 1, 500, 500)"
        )
    skins = tmp_path / "skins.json"
    skins.write_text("[]", encoding="utf-8")

    payload = market_analytics.build_liquid_market_payload(
        tmp_path / "missing-history.db",
        trend,
        skins,
        max_price_cents=1_000,
        min_sales_per_day=0,
        search="ZZZ target",
    )

    assert [item["market_hash_name"] for item in payload["items"]] == [
        "ZZZ target after old cutoff"
    ]
    assert payload["summary"]["candidate_scan_truncated"] is False


def _prediction(
    conn,
    listing_id,
    model_name,
    *,
    roi,
    profit,
    liquidity,
    float_value,
    sticker_value,
):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO model_deal_predictions (
            listing_id, model_name, first_seen_at, last_seen_at, scan_count,
            skin_name, buy_price_cents, predicted_price_cents, profit_cents,
            roi_percent, verdict, deal_payload_json, prediction_json,
            liquidity_score, float_value, sticker_value_cents
        ) VALUES (?, ?, ?, ?, 1, ?, 1000, 1200, ?, ?, 'good', '{}', '{}', ?, ?, ?)
        """,
        (
            listing_id,
            model_name,
            now,
            now,
            f"Skin {listing_id}",
            profit,
            roi,
            liquidity,
            float_value,
            sticker_value,
        ),
    )


def test_deal_thresholds_use_conservative_merged_metrics_and_are_inclusive(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    for model, roi, profit, liquidity in (
        ("almaz", 20, 200, 70),
        ("igor", 10, 100, 60),
    ):
        _prediction(
            conn,
            "safe",
            model,
            roi=roi,
            profit=profit,
            liquidity=liquidity,
            float_value=0.10,
            sticker_value=500,
        )
    _prediction(
        conn,
        "optimistic-only",
        "almaz",
        roi=100,
        profit=1000,
        liquidity=90,
        float_value=0.01,
        sticker_value=1000,
    )
    _prediction(
        conn,
        "optimistic-only",
        "igor",
        roi=-1,
        profit=-10,
        liquidity=10,
        float_value=0.01,
        sticker_value=1000,
    )
    monkeypatch.setattr(
        dashboard_server, "add_deal_visual_fallbacks", lambda _conn, _rows: None
    )

    result = dashboard_server.fetch_model_deal_comparison_page(
        conn,
        0,
        100,
        "",
        "all",
        min_roi="10",
        min_profit="1.00",
        min_liquidity="60",
        max_float="0.10",
        min_sticker_value="5.00",
    )

    assert [row["listing_id"] for row in result["rows"]] == ["safe"]
    assert result["rows"][0]["conservative_roi_percent"] == 10
    assert result["rows"][0]["conservative_profit_cents"] == 100
    assert result["rows"][0]["conservative_liquidity_score"] == 60
    assert result["min_sticker_value_cents"] == 500


def test_liquidity_threshold_fails_closed_when_an_active_model_has_no_score(
    monkeypatch,
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    _prediction(
        conn,
        "missing-liquidity",
        "almaz",
        roi=20,
        profit=200,
        liquidity=70,
        float_value=0.10,
        sticker_value=500,
    )
    _prediction(
        conn,
        "missing-liquidity",
        "igor",
        roi=10,
        profit=100,
        liquidity=None,
        float_value=0.10,
        sticker_value=500,
    )
    monkeypatch.setattr(
        dashboard_server, "add_deal_visual_fallbacks", lambda _conn, _rows: None
    )

    unfiltered = dashboard_server.fetch_model_deal_comparison_page(
        conn, 0, 100, "", "all"
    )
    filtered = dashboard_server.fetch_model_deal_comparison_page(
        conn, 0, 100, "", "all", min_liquidity="60"
    )

    assert unfiltered["rows"][0]["conservative_liquidity_score"] is None
    assert filtered["rows"] == []


def test_threshold_inputs_and_strategy_tabs_are_wired_end_to_end():
    for parameter in (
        "min_roi",
        "min_profit",
        "min_liquidity",
        "max_float",
        "min_sticker_value",
    ):
        assert f"{parameter}=${{encodeURIComponent" in dashboard_server.DEALS_HTML
    for element_id in (
        "minRoi",
        "minProfit",
        "minLiquidity",
        "maxFloat",
        "minStickerValue",
    ):
        assert f'id="{element_id}"' in dashboard_server.DEALS_HTML
    assert 'data-lab-tab="liquid"' in dashboard_server.STRATEGY_HTML
    assert 'data-lab-tab="collections"' in dashboard_server.STRATEGY_HTML
    assert 'data-lab-tab="advisor"' in dashboard_server.STRATEGY_HTML
    assert "/api/market/liquid" in dashboard_server.STRATEGY_HTML
    assert "/api/strategy/advisor" in dashboard_server.STRATEGY_HTML


def test_budget_item_cap_adapts_to_small_and_large_banks():
    assert market_analytics._adaptive_item_cap(500, "balanced") == 500
    assert market_analytics._adaptive_item_cap(5_000, "balanced") == 1_000
    assert market_analytics._adaptive_item_cap(500_000, "balanced") == 150_000
    assert market_analytics._adaptive_item_cap(500_000, "aggressive") == 225_000


def test_float_and_sticker_modes_require_their_own_evidence():
    base = {
        "ranking_score": 10,
        "ensemble_prediction": {"conservative_profit_cents": 100},
        "sale_estimate": {"within_30d": 0.8},
        "risk": {"score": 20},
        "sticker_count": 0,
        "sticker_value_to_price_percent": 0,
        "float_range_percentile": 50,
    }
    assert market_analytics._rank_live_candidate(base, "flip_float") is None
    assert market_analytics._rank_live_candidate(base, "flip_stickers") is None
    assert market_analytics._rank_live_candidate(
        {**base, "float_range_percentile": 10}, "flip_float"
    ) is not None
    assert market_analytics._rank_live_candidate(
        {**base, "sticker_count": 2, "sticker_value_to_price_percent": 25},
        "flip_stickers",
    ) is not None


def test_liquid_store_is_ready_even_when_price_filter_has_no_matches(tmp_path):
    trend = tmp_path / "trend.db"
    history = tmp_path / "history.db"
    skins = tmp_path / "skins.json"
    _trend_store(trend)
    sqlite3.connect(history).close()
    skins.write_text("[]", encoding="utf-8")

    payload = market_analytics.build_liquid_market_payload(
        history,
        trend,
        skins,
        max_price_cents=1,
        min_sales_per_day=0,
    )

    assert payload["status"] == "ready"
    assert payload["items"] == []


def test_liquid_market_tolerates_optional_columns_and_partial_forecast_schema(
    tmp_path,
):
    trend = tmp_path / "trend.db"
    history = tmp_path / "history.db"
    skins = tmp_path / "skins.json"
    with sqlite3.connect(trend) as conn:
        conn.execute(
            """
            CREATE TABLE trend_daily_market (
                market_hash_name TEXT,
                day TEXT,
                sales_count INTEGER,
                median_price_cents REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO trend_daily_market VALUES ('Legacy Skin', '2026-01-30', 30, 999)"
        )
        conn.execute("CREATE TABLE trend_forecasts (as_of_day TEXT)")
    sqlite3.connect(history).close()
    skins.write_text("[]", encoding="utf-8")

    payload = market_analytics.build_liquid_market_payload(
        history,
        trend,
        skins,
        max_price_cents=999,
        min_sales_per_day=1,
    )

    assert payload["status"] == "ready"
    assert payload["items"][0]["market_hash_name"] == "Legacy Skin"
    assert payload["items"][0]["notional_30d"] is None
    assert payload["items"][0]["forecast_30d"] is None


def test_liquid_live_rows_obey_the_same_velocity_thresholds(monkeypatch, tmp_path):
    trend = tmp_path / "trend.db"
    history = tmp_path / "history.db"
    skins = tmp_path / "skins.json"
    _trend_store(trend)
    sqlite3.connect(history).close()
    skins.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        market_analytics,
        "_live_deals",
        lambda *_args, **_kwargs: [
            {"listing_id": "slow", "sales_per_day": 0.99, "liquidity_score": 100},
            {"listing_id": "thin", "sales_per_day": 1, "liquidity_score": 59.99},
            {"listing_id": "boundary", "sales_per_day": 1, "liquidity_score": 60},
        ],
    )

    payload = market_analytics.build_liquid_market_payload(
        history,
        trend,
        skins,
        min_sales_per_day=1,
        min_liquidity_score=60,
    )

    assert [row["listing_id"] for row in payload["live_deals"]] == ["boundary"]


def test_live_deals_require_recent_listing_observation_and_conservative_profit(
    tmp_path,
):
    history = tmp_path / "history.db"
    now = datetime.now(timezone.utc)
    with sqlite3.connect(history) as conn:
        conn.row_factory = sqlite3.Row
        deal_scanner.init_db(conn)
        for listing_id in ("fresh", "stale", "optimistic-only"):
            _prediction(
                conn,
                listing_id,
                "almaz",
                roi=20,
                profit=200,
                liquidity=70,
                float_value=0.1,
                sticker_value=0,
            )
            deal_intelligence.refresh_listing_intelligence(
                conn, listing_id, now=now
            )
        conn.execute(
            "UPDATE model_deal_predictions SET last_seen_at = ? WHERE listing_id = ?",
            ((now - timedelta(hours=48)).isoformat(), "stale"),
        )
        conn.execute(
            """
            UPDATE model_deal_intelligence
            SET conservative_profit_cents = -1
            WHERE listing_id = 'optimistic-only'
            """
        )
        conn.commit()

    rows = market_analytics._live_deals(
        history,
        max_price_cents=1_000,
        max_age_hours=24,
        snapshot_items={},
    )

    assert [row["listing_id"] for row in rows] == ["fresh"]
    assert rows[0]["csfloat_url"] == "https://csfloat.com/item/fresh"
    assert rows[0]["availability"]["currently_active_guaranteed"] is False
    assert rows[0]["listing_observed_at"]


def test_live_deals_fail_closed_on_partial_legacy_schema(tmp_path):
    history = tmp_path / "history.db"
    with sqlite3.connect(history) as conn:
        conn.execute("CREATE TABLE model_deal_intelligence (listing_id TEXT)")

    assert market_analytics._live_deals(
        history,
        max_price_cents=1_000,
        max_age_hours=24,
        snapshot_items={},
    ) == []


def test_investment_sales_window_is_anchored_to_forecast_and_zero_downside_is_valid(
    tmp_path,
):
    trend = tmp_path / "trend.db"
    skins = tmp_path / "skins.json"
    _trend_store(trend)
    skins.write_text("[]", encoding="utf-8")
    with sqlite3.connect(trend) as conn:
        conn.executemany(
            "INSERT INTO trend_daily_market VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("Zero Downside", "2026-01-30", 10, 500, 500, 500, 500, 500, 500, 5_000),
                ("Future Volume", "2026-01-30", 1, 500, 500, 500, 500, 500, 500, 500),
                ("Future Volume", "2026-02-01", 100, 500, 500, 500, 500, 500, 500, 50_000),
            ],
        )
        for name, downside in (("Zero Downside", 0), ("Future Volume", -5)):
            conn.execute(
                "INSERT INTO trend_forecasts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "2026-01-30",
                    name,
                    30,
                    500,
                    550,
                    500,
                    600,
                    10,
                    downside,
                    0.7,
                    0.7,
                    90,
                ),
            )

    rows = market_analytics._investment_candidates(
        trend,
        skins,
        item_cap_cents=1_000,
        risk="balanced",
    )
    names = {row["market_hash_name"] for row in rows}

    assert "Zero Downside" in names
    assert "Future Volume" not in names


def test_advisor_modes_fail_closed_on_profit_and_requested_risk():
    base = {
        "ranking_score": 10,
        "ensemble_prediction": {"conservative_profit_cents": 100},
        "sale_estimate": {"within_30d": 0.8},
        "risk": {"score": 50},
        "float_range_percentile": 10,
    }

    assert market_analytics._rank_live_candidate(
        base, "flip_float", "conservative"
    ) is None
    assert market_analytics._rank_live_candidate(
        base, "flip_float", "aggressive"
    ) is not None
    assert market_analytics._rank_live_candidate(
        {
            **base,
            "risk": {"score": 0},
            "ensemble_prediction": {"conservative_profit_cents": -1},
        },
        "flip_float",
        "aggressive",
    ) is None
    assert market_analytics._rank_live_candidate(
        {**base, "float_range_percentile": -1},
        "flip_float",
        "aggressive",
    ) is None


def test_advisor_partial_stores_return_labelled_empty_payload(tmp_path):
    trend = tmp_path / "trend.db"
    history = tmp_path / "history.db"
    skins = tmp_path / "skins.json"
    with sqlite3.connect(trend) as conn:
        conn.execute("CREATE TABLE trend_forecasts (as_of_day TEXT)")
    sqlite3.connect(history).close()
    skins.write_text("[]", encoding="utf-8")

    payload = market_analytics.build_budget_advice(
        history,
        trend,
        skins,
        budget_cents=5_000,
        mode="balanced",
    )

    assert payload["items"] == []
    assert payload["supply"]["is_world_inventory"] is False
    assert payload["safety"]["live_listing_availability_guaranteed"] is False
