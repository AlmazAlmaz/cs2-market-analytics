#!/usr/bin/env python3
"""Offline CLI for Trend aggregates, forecasts, and economic backtests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import app_config
from TRENDMODEL.backtest import BacktestConfig, run_walk_forward_backtest
from TRENDMODEL.model import TrendModelConfig, build_latest_forecasts
from TRENDMODEL.storage import (
    connect_store,
    load_daily_aggregates,
    refresh_daily_aggregates,
    save_backtest_report,
    save_forecasts,
)

DEFAULT_SOURCE_DB = app_config.path("CS2_DB_PATH", "csfloat_history.db")
DEFAULT_TREND_DB = app_config.path("CS2_TREND_DB_PATH", "trend_market.db")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build leakage-safe CS2 market-trend research artifacts"
    )
    parser.add_argument("command", choices=("refresh", "forecast", "backtest", "all"))
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--store-db", type=Path, default=DEFAULT_TREND_DB)
    parser.add_argument("--as-of")
    parser.add_argument("--start-day")
    parser.add_argument("--end-day")
    parser.add_argument("--holding-days", type=int, choices=(7, 14, 30), default=7)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--max-rebalances", type=int, default=52)
    parser.add_argument("--fee-percent", type=float, default=4.0)
    parser.add_argument("--buy-slippage-percent", type=float, default=0.5)
    parser.add_argument("--sell-slippage-percent", type=float, default=0.5)
    parser.add_argument("--fill-probability", type=float, default=0.8)
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    output: dict[str, Any] = {"command": args.command}
    if args.command in {"refresh", "all"}:
        output["refresh"] = refresh_daily_aggregates(args.source_db, args.store_db)
    if args.command in {"forecast", "backtest", "all"}:
        conn = connect_store(args.store_db)
        try:
            daily_rows = load_daily_aggregates(conn)
            if args.command in {"forecast", "all"}:
                forecasts = build_latest_forecasts(
                    daily_rows,
                    as_of_day=args.as_of,
                    config=TrendModelConfig(),
                )
                output["forecast"] = save_forecasts(conn, forecasts)
            if args.command in {"backtest", "all"}:
                exit_tolerance_days = 2
                backtest_config = BacktestConfig(
                    holding_days=args.holding_days,
                    top_k=args.top_k,
                    start_day=args.start_day,
                    end_day=args.end_day,
                    rebalance_every_days=args.holding_days + exit_tolerance_days,
                    max_rebalances=args.max_rebalances,
                    fee_percent=args.fee_percent,
                    buy_slippage_percent=args.buy_slippage_percent,
                    sell_slippage_percent=args.sell_slippage_percent,
                    fill_probability=args.fill_probability,
                    exit_tolerance_days=exit_tolerance_days,
                )
                report = run_walk_forward_backtest(daily_rows, backtest_config)
                report_id = save_backtest_report(conn, report)
                output["backtest"] = {
                    "report_id": report_id,
                    "status": report["status"],
                    "metrics": report["metrics"],
                }
        finally:
            conn.close()
    return output


def main() -> int:
    args = _parser().parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
