import copy
import json
import os
import sqlite3
from datetime import date, timedelta

import pytest

import trend_worker
from trend_worker import WorkerStatus, model_training_is_active, run_cycle
from TRENDMODEL.api import (
    get_backtest_report,
    get_trend_pipeline_status,
    get_trend_summary,
)
from TRENDMODEL.backtest import (
    BacktestConfig,
    EconomicGatePolicy,
    evaluate_promotion_economic_gate,
    run_walk_forward_backtest,
)
from TRENDMODEL.model import TrendModelConfig, build_latest_forecasts
from TRENDMODEL.storage import (
    connect_store,
    load_daily_aggregates,
    refresh_daily_aggregates,
    save_backtest_report,
    save_forecasts,
)


def _daily_rows(days=180):
    start = date(2026, 1, 1)
    rows = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        rows.extend(
            [
                {
                    "market_hash_name": "Rising",
                    "day": day.isoformat(),
                    "sales_count": 8,
                    "median_price_cents": 1_000 * 1.004**offset,
                },
                {
                    "market_hash_name": "Flat",
                    "day": day.isoformat(),
                    "sales_count": 5,
                    "median_price_cents": 2_000,
                },
            ]
        )
    return rows


def test_daily_aggregate_refresh_is_separate_atomic_and_idempotent(tmp_path):
    source = tmp_path / "source.db"
    store_path = tmp_path / "trend.db"
    with sqlite3.connect(source) as conn:
        conn.executescript(
            """
            CREATE TABLE sales (
                listing_id TEXT PRIMARY KEY,
                name TEXT,
                price_cents INTEGER,
                state TEXT,
                sold_at TEXT,
                fetched_at TEXT
            );
            INSERT INTO sales VALUES
                ('a', 'Skin A', 100, 'sold', '2026-01-01T01:00:00Z', '2026-01-02'),
                ('b', 'Skin A', 300, 'sold', '2026-01-01T02:00:00Z', '2026-01-03'),
                ('c', 'Skin A', 900, 'listed', '2026-01-01T03:00:00Z', '2026-01-04'),
                ('d', 'Skin B', 500, 'sold', 'bad-time', '2026-01-05'),
                ('e', 'Skin B', 600, 'sold', '2026-01-02T00:00:00Z', '2026-01-05');
            """
        )

    first = refresh_daily_aggregates(source, store_path, insert_batch_size=1)
    second = refresh_daily_aggregates(source, store_path, insert_batch_size=2)
    assert first["eligible_sale_rows"] == 3
    assert first["daily_rows"] == 2
    assert second["content_sha256"] == first["content_sha256"]

    conn = connect_store(store_path)
    try:
        rows = load_daily_aggregates(conn)
    finally:
        conn.close()
    assert len(rows) == 2
    skin_a = next(row for row in rows if row["market_hash_name"] == "Skin A")
    assert skin_a["sales_count"] == 2
    assert skin_a["median_price_cents"] == 200
    assert skin_a["p15_price_cents"] == 130
    assert skin_a["p85_price_cents"] == 270

    with pytest.raises(ValueError, match="separate"):
        refresh_daily_aggregates(source, source)


def test_trend_forecast_is_leakage_safe_and_has_all_horizons():
    rows = _daily_rows(140)
    as_of = "2026-04-15"
    config = TrendModelConfig(lookback_days=60)
    before = build_latest_forecasts(rows, as_of_day=as_of, config=config)
    poisoned = rows + [
        {
            "market_hash_name": name,
            "day": "2026-12-31",
            "sales_count": 100,
            "median_price_cents": 999_999_999,
        }
        for name in ("Rising", "Flat", "Future only")
    ]
    after = build_latest_forecasts(poisoned, as_of_day=as_of, config=config)

    assert before == after
    rising = [row for row in before if row["market_hash_name"] == "Rising"]
    assert {row["horizon_days"] for row in rising} == {7, 14, 30}
    assert all(row["max_feature_day"] <= as_of for row in before)
    assert all(
        row["predicted_price_cents"] > row["current_price_cents"] for row in rising
    )


def test_walk_forward_backtest_reports_economic_metrics_and_ignores_late_future():
    rows = _daily_rows(190)
    config = BacktestConfig(
        holding_days=7,
        top_k=1,
        start_day="2026-02-15",
        end_day="2026-05-31",
        rebalance_every_days=7,
        max_rebalances=8,
        fee_percent=0,
        buy_slippage_percent=0,
        sell_slippage_percent=0,
        fill_probability=1,
        exit_tolerance_days=0,
        initial_capital_cents=100_000,
    )
    model_config = TrendModelConfig(horizons_days=(7,), lookback_days=30)
    report = run_walk_forward_backtest(rows, config, model_config=model_config)
    assert report["status"] == "ready"
    metrics = report["metrics"]
    assert metrics["net_profit_cents"] > 0
    assert metrics["precision_at_k"] == 1
    assert metrics["completed_trades"] == 8
    assert metrics["max_drawdown_percent"] == 0
    assert metrics["turnover_ratio"] > 0
    assert "sharpe_annualized" in metrics
    assert "sortino_annualized" in metrics

    late_rows = rows + [
        {
            "market_hash_name": "Rising",
            "day": "2027-01-01",
            "sales_count": 50,
            "median_price_cents": 1,
        }
    ]
    repeated = run_walk_forward_backtest(late_rows, config, model_config=model_config)
    assert repeated["metrics"] == report["metrics"]
    assert repeated["comparison_sha256"] == report["comparison_sha256"]


def test_costs_and_deterministic_fill_are_applied():
    rows = _daily_rows(160)
    common = {
        "holding_days": 7,
        "top_k": 2,
        "rebalance_every_days": 7,
        "max_rebalances": 4,
        "exit_tolerance_days": 0,
        "fill_probability": 0.5,
        "fill_seed": "repeatable",
        "fee_percent": 0,
        "buy_slippage_percent": 0,
        "sell_slippage_percent": 0,
        "minimum_expected_net_return_percent": -100,
    }
    model_config = TrendModelConfig(horizons_days=(7,), lookback_days=30)
    without_cost = run_walk_forward_backtest(
        rows,
        BacktestConfig(**common),
        model_config=model_config,
    )
    repeated = run_walk_forward_backtest(
        rows,
        BacktestConfig(**common),
        model_config=model_config,
    )
    with_cost = run_walk_forward_backtest(
        rows,
        BacktestConfig(
            **{
                **common,
                "fee_percent": 10,
                "buy_slippage_percent": 2,
                "sell_slippage_percent": 2,
            }
        ),
        model_config=model_config,
    )
    assert repeated["metrics"] == without_cost["metrics"]
    assert (
        with_cost["metrics"]["net_profit_cents"]
        < without_cost["metrics"]["net_profit_cents"]
    )
    assert with_cost["metrics"]["fees_paid_cents"] > 0
    assert with_cost["metrics"]["estimated_slippage_cents"] > 0


def test_economic_gate_is_fail_closed_and_requires_same_evaluation():
    rows = _daily_rows(170)
    config = BacktestConfig(
        holding_days=7,
        top_k=1,
        rebalance_every_days=7,
        max_rebalances=4,
        fee_percent=0,
        buy_slippage_percent=0,
        sell_slippage_percent=0,
        fill_probability=1,
        exit_tolerance_days=0,
    )
    report = run_walk_forward_backtest(
        rows,
        config,
        model_config=TrendModelConfig(horizons_days=(7,), lookback_days=30),
    )
    policy = EconomicGatePolicy(minimum_completed_trades=1)
    assert evaluate_promotion_economic_gate(report, report, policy)["passed"] is True

    tampered = copy.deepcopy(report)
    tampered["metrics"]["net_return_percent"] = 999
    gate = evaluate_promotion_economic_gate(tampered, report, policy)
    assert gate["eligible"] is False
    assert gate["passed"] is False

    other_evaluation = copy.deepcopy(report)
    other_evaluation["comparison_sha256"] = "f" * 64
    other_evaluation["evaluation_sha256"] = "0" * 64
    assert (
        evaluate_promotion_economic_gate(report, other_evaluation, policy)["passed"]
        is False
    )


def test_precomputed_read_api_is_bounded_and_reports_worker_freshness(tmp_path):
    store_path = tmp_path / "trend.db"
    conn = connect_store(store_path)
    try:
        forecasts = build_latest_forecasts(
            _daily_rows(100),
            as_of_day="2026-04-10",
            config=TrendModelConfig(lookback_days=60),
        )
        save_forecasts(conn, forecasts)
        report = run_walk_forward_backtest(
            _daily_rows(150),
            BacktestConfig(
                holding_days=7,
                top_k=1,
                rebalance_every_days=7,
                max_rebalances=2,
                exit_tolerance_days=0,
            ),
            model_config=TrendModelConfig(horizons_days=(7,), lookback_days=30),
        )
        report_id = save_backtest_report(conn, report)
        summary = get_trend_summary(conn, "Rising")
        saved_report = get_backtest_report(conn, report["config"])
    finally:
        conn.close()

    assert summary["status"] == "ready"
    assert len(summary["items"]) == 1
    assert set(summary["items"][0]["forecasts"]) == {"7", "14", "30"}
    assert saved_report["report_id"] == report_id

    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "state": "ready",
                "pid": os.getpid(),
                "heartbeat": "2999-01-01T00:00:00+00:00",
                "phase": "idle",
                "progress_percent": 100,
                "last_error": None,
                "last_success": "2999-01-01T00:00:00+00:00",
                "next_refresh": None,
                "refresh_interval_seconds": 21_600,
            }
        ),
        encoding="utf-8",
    )
    status = get_trend_pipeline_status(status_path)
    assert status["process_alive"] is True
    assert status["heartbeat_fresh"] is True
    assert status["data_fresh"] is True
    assert status["fresh"] is True

    unrelated = sqlite3.connect(tmp_path / "unrelated.db")
    try:
        assert get_trend_summary(unrelated)["status"] == "unavailable"
        assert get_backtest_report(unrelated)["status"] == "unavailable"
        tables = {
            row[0]
            for row in unrelated.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
    finally:
        unrelated.close()
    assert not any(name.startswith("trend_") for name in tables)


def test_background_cycle_persists_forecast_backtest_and_status(tmp_path):
    source_path = tmp_path / "source.db"
    store_path = tmp_path / "trend.db"
    status_path = tmp_path / "status.json"
    start = date(2026, 1, 1)
    sales = []
    for offset in range(125):
        day = start + timedelta(days=offset)
        for index, name in enumerate(("Rising", "Flat")):
            price = round(1_000 * 1.004**offset) if index == 0 else 2_000
            sales.append(
                (
                    f"{offset}-{index}",
                    name,
                    price,
                    "sold",
                    f"{day.isoformat()}T12:00:00Z",
                    f"{day.isoformat()}T13:00:00Z",
                )
            )
    with sqlite3.connect(source_path) as conn:
        conn.execute(
            """
            CREATE TABLE sales (
                listing_id TEXT PRIMARY KEY, name TEXT, price_cents INTEGER,
                state TEXT, sold_at TEXT, fetched_at TEXT
            )
            """
        )
        conn.executemany("INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?)", sales)

    worker_status = WorkerStatus(status_path, source_path, store_path)
    result = run_cycle(worker_status, source_db=source_path, store_db=store_path)
    persisted_status = json.loads(status_path.read_text(encoding="utf-8"))
    assert result["aggregate_rows"] == 250
    assert result["forecast_rows"] == 6
    assert result["backtest_report_id"]
    assert persisted_status["state"] == "ready"
    assert persisted_status["phase"] == "idle"
    assert persisted_status["last_success"]
    assert get_trend_summary(store_path)["status"] == "ready"
    assert get_backtest_report(store_path)["status"] in {
        "ready",
        "insufficient_data",
    }


def test_trend_worker_defers_when_model_training_lock_is_held(tmp_path):
    if trend_worker.fcntl is None:
        pytest.skip("POSIX flock is required")
    resource_lock = tmp_path / "model_training" / "resource.lock"
    resource_lock.parent.mkdir(parents=True)
    descriptor = os.open(resource_lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        trend_worker.fcntl.flock(descriptor, trend_worker.fcntl.LOCK_EX)
        assert model_training_is_active(resource_lock) is True
    finally:
        trend_worker.fcntl.flock(descriptor, trend_worker.fcntl.LOCK_UN)
        os.close(descriptor)
    assert model_training_is_active(resource_lock) is False
