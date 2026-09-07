# ruff: noqa: N999 - preserve the project's established uppercase model packages.
"""Leakage-safe market trend forecasts and economic backtesting.

The package is deliberately independent from the live price-model runtimes.
It reads completed sales, writes derived data to a separate SQLite database,
and never sends marketplace requests or places trades.
"""

from .api import (
    get_backtest_report,
    get_collection_detail,
    get_skin_daily_series,
    get_trend_pipeline_status,
    get_trend_summary,
    list_collections,
)
from .backtest import (
    BacktestConfig,
    EconomicGatePolicy,
    evaluate_promotion_economic_gate,
    run_walk_forward_backtest,
)
from .model import TrendModelConfig, build_latest_forecasts
from .storage import refresh_daily_aggregates

__all__ = [
    "BacktestConfig",
    "EconomicGatePolicy",
    "TrendModelConfig",
    "build_latest_forecasts",
    "evaluate_promotion_economic_gate",
    "get_backtest_report",
    "get_collection_detail",
    "get_skin_daily_series",
    "get_trend_pipeline_status",
    "get_trend_summary",
    "list_collections",
    "refresh_daily_aggregates",
    "run_walk_forward_backtest",
]
