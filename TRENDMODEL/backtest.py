"""Walk-forward economic backtest for precomputed daily market data."""

from __future__ import annotations

import bisect
import hashlib
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

from .model import PooledTrendForecaster, TrendModelConfig, _as_day
from .storage import MODEL_VERSION, canonical_sha256, utc_now

BACKTEST_CONTRACT_VERSION = 1
ECONOMIC_GATE_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class BacktestConfig:
    holding_days: int = 7
    top_k: int = 20
    start_day: str | None = None
    end_day: str | None = None
    rebalance_every_days: int = 9
    max_rebalances: int = 52
    fee_percent: float = 4.0
    buy_slippage_percent: float = 0.50
    sell_slippage_percent: float = 0.50
    fill_probability: float = 0.80
    minimum_daily_sales: int = 1
    minimum_expected_net_return_percent: float = 0.0
    minimum_downside_net_return_percent: float = -100.0
    exit_tolerance_days: int = 2
    stale_exit_haircut_percent: float = 10.0
    initial_capital_cents: int = 100_000
    fill_seed: str = "cs2-trend-fill-v1"

    def __post_init__(self) -> None:
        if self.holding_days not in (7, 14, 30):
            raise ValueError("holding_days must be 7, 14, or 30")
        if self.top_k < 1 or self.top_k > 500:
            raise ValueError("top_k must be between 1 and 500")
        if self.exit_tolerance_days < 0 or self.exit_tolerance_days > 14:
            raise ValueError("exit_tolerance_days must be between 0 and 14")
        if self.rebalance_every_days < self.holding_days + self.exit_tolerance_days:
            raise ValueError(
                "rebalance_every_days must cover holding_days + exit_tolerance_days"
            )
        if self.max_rebalances < 1 or self.max_rebalances > 1_000:
            raise ValueError("max_rebalances must be between 1 and 1000")
        numeric_values = (
            self.fee_percent,
            self.buy_slippage_percent,
            self.sell_slippage_percent,
            self.fill_probability,
            self.minimum_expected_net_return_percent,
            self.minimum_downside_net_return_percent,
            self.stale_exit_haircut_percent,
        )
        if not all(math.isfinite(value) for value in numeric_values):
            raise ValueError("backtest numeric assumptions must be finite")
        if not 0 <= self.fee_percent < 100:
            raise ValueError("fee_percent must be in [0, 100)")
        if not 0 <= self.buy_slippage_percent < 100:
            raise ValueError("buy_slippage_percent must be in [0, 100)")
        if not 0 <= self.sell_slippage_percent < 100:
            raise ValueError("sell_slippage_percent must be in [0, 100)")
        if not 0 <= self.fill_probability <= 1:
            raise ValueError("fill_probability must be in [0, 1]")
        if self.minimum_daily_sales < 1:
            raise ValueError("minimum_daily_sales must be positive")
        if not 0 <= self.stale_exit_haircut_percent <= 100:
            raise ValueError("stale_exit_haircut_percent must be in [0, 100]")
        if self.initial_capital_cents < 1:
            raise ValueError("initial_capital_cents must be positive")
        if not self.fill_seed:
            raise ValueError("fill_seed must not be empty")
        if self.start_day:
            _as_day(self.start_day)
        if self.end_day:
            _as_day(self.end_day)
        if (
            self.start_day
            and self.end_day
            and _as_day(self.start_day) > _as_day(self.end_day)
        ):
            raise ValueError("start_day must not be later than end_day")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EconomicGatePolicy:
    minimum_completed_trades: int = 20
    minimum_precision_at_k: float = 0.50
    minimum_net_return_percent: float = 0.0
    maximum_drawdown_percent: float = 30.0
    maximum_precision_regression: float = 0.0
    maximum_return_regression_percent: float = 0.0
    maximum_drawdown_regression_percent: float = 2.0

    def __post_init__(self) -> None:
        numeric_values = (
            self.minimum_precision_at_k,
            self.minimum_net_return_percent,
            self.maximum_drawdown_percent,
            self.maximum_precision_regression,
            self.maximum_return_regression_percent,
            self.maximum_drawdown_regression_percent,
        )
        if not all(math.isfinite(value) for value in numeric_values):
            raise ValueError("economic gate thresholds must be finite")
        if self.minimum_completed_trades < 1:
            raise ValueError("minimum_completed_trades must be positive")
        if not 0 <= self.minimum_precision_at_k <= 1:
            raise ValueError("minimum_precision_at_k must be in [0, 1]")
        if not 0 <= self.maximum_drawdown_percent <= 100:
            raise ValueError("maximum_drawdown_percent must be in [0, 100]")


@dataclass(frozen=True)
class _ObservedPoint:
    day: date
    price_cents: float
    sales_count: int


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _normalized_rows(
    daily_rows: Iterable[Mapping[str, Any]], *, end_day: date | None = None
) -> list[dict[str, Any]]:
    values: dict[tuple[str, date], dict[str, Any]] = {}
    for raw in daily_rows:
        name = str(raw.get("market_hash_name") or "").strip()
        if not name:
            continue
        day = _as_day(raw.get("day"))
        if end_day is not None and day > end_day:
            continue
        price = _safe_float(raw.get("median_price_cents"))
        if price <= 0:
            continue
        sales_count = max(1, int(raw.get("sales_count") or 1))
        key = (name, day)
        existing = values.get(key)
        if existing is None:
            values[key] = {
                "market_hash_name": name,
                "day": day.isoformat(),
                "median_price_cents": price,
                "sales_count": sales_count,
            }
        else:
            total = existing["sales_count"] + sales_count
            existing["median_price_cents"] = (
                existing["median_price_cents"] * existing["sales_count"]
                + price * sales_count
            ) / total
            existing["sales_count"] = total
    return sorted(
        values.values(), key=lambda row: (row["day"], row["market_hash_name"])
    )


def _build_observation_index(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[
    dict[str, tuple[_ObservedPoint, ...]], dict[tuple[str, date], _ObservedPoint]
]:
    grouped: dict[str, list[_ObservedPoint]] = defaultdict(list)
    exact: dict[tuple[str, date], _ObservedPoint] = {}
    for row in rows:
        point = _ObservedPoint(
            day=_as_day(row["day"]),
            price_cents=float(row["median_price_cents"]),
            sales_count=int(row["sales_count"]),
        )
        name = str(row["market_hash_name"])
        grouped[name].append(point)
        exact[(name, point.day)] = point
    return (
        {
            name: tuple(sorted(points, key=lambda point: point.day))
            for name, points in grouped.items()
        },
        exact,
    )


def _daily_rows_sha256(rows: Iterable[Mapping[str, Any]]) -> str:
    """Hash normalized rows without materializing one giant JSON string."""

    digest = hashlib.sha256()
    for row in rows:
        for value in (
            row["day"],
            row["market_hash_name"],
            format(float(row["median_price_cents"]), ".17g"),
            int(row["sales_count"]),
        ):
            encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _deterministic_fill(name: str, day: date, probability: float, seed: str) -> bool:
    if probability <= 0:
        return False
    if probability >= 1:
        return True
    payload = f"{seed}\0{name}\0{day.isoformat()}".encode()
    sample = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") / 2**64
    return sample < probability


def _exit_observation(
    points: tuple[_ObservedPoint, ...],
    target: date,
    tolerance_days: int,
) -> _ObservedPoint | None:
    days = [point.day for point in points]
    index = bisect.bisect_left(days, target)
    if index >= len(points):
        return None
    point = points[index]
    return point if point.day <= target + timedelta(days=tolerance_days) else None


def _ratio(value: float, denominator: float) -> float | None:
    return value / denominator if denominator else None


def _risk_metrics(
    cycle_returns: list[float], periods_per_year: float
) -> tuple[float | None, float | None]:
    if len(cycle_returns) < 2:
        return None, None
    mean = statistics.fmean(cycle_returns)
    std = statistics.stdev(cycle_returns)
    sharpe = mean / std * math.sqrt(periods_per_year) if std > 0 else None
    downside = [min(0.0, value) for value in cycle_returns]
    downside_deviation = math.sqrt(
        statistics.fmean(value * value for value in downside)
    )
    sortino = (
        mean / downside_deviation * math.sqrt(periods_per_year)
        if downside_deviation > 0
        else None
    )
    return sharpe, sortino


def _empty_report(
    config: BacktestConfig,
    model_config: TrendModelConfig,
    *,
    status: str,
    reason: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    source_sha = _daily_rows_sha256(rows)
    comparison_sha = canonical_sha256(
        {"source_sha256": source_sha, "config": config.to_dict()}
    )
    value = {
        "contract_version": BACKTEST_CONTRACT_VERSION,
        "report_type": "walk_forward_economic_backtest",
        "status": status,
        "reason": reason,
        "generated_at": utc_now(),
        "model_version": MODEL_VERSION,
        "config": config.to_dict(),
        "model_config": model_config.to_dict(),
        "source": {
            "daily_rows": len(rows),
            "market_hash_names": len({row["market_hash_name"] for row in rows}),
            "first_day": rows[0]["day"] if rows else None,
            "last_day": rows[-1]["day"] if rows else None,
            "content_sha256": source_sha,
        },
        "assumptions": {
            "signal_timing": "after daily close",
            "entry_price": "same-day median plus buy slippage",
            "exit_price": "first median on/after target within tolerance",
            "missing_exit": "entry median less stale-exit haircut",
            "fill_simulation": "deterministic SHA-256 sampling",
            "position_size": "at most one indivisible item per signal",
            "fees_apply_to": "sale proceeds",
            "drawdown_sampling": "realized equity at cycle close",
        },
        "metrics": {},
        "cycles": [],
        "comparison_sha256": comparison_sha,
    }
    value["evaluation_sha256"] = canonical_sha256(
        {
            "comparison_sha256": comparison_sha,
            "model_version": MODEL_VERSION,
            "metrics": value["metrics"],
            "cycles": value["cycles"],
        }
    )
    return value


def run_walk_forward_backtest(
    daily_rows: Iterable[Mapping[str, Any]],
    config: BacktestConfig | None = None,
    *,
    model_config: TrendModelConfig | None = None,
) -> dict[str, Any]:
    """Run a reproducible, non-overlapping walk-forward simulation.

    Features for a signal on day *D* are cut at ``D``. Future rows are used
    only after candidates have been ranked, to measure their exits.
    """

    cfg = config or BacktestConfig()
    trend_cfg = model_config or TrendModelConfig()
    if cfg.holding_days not in trend_cfg.horizons_days:
        trend_cfg = TrendModelConfig(
            **{**trend_cfg.to_dict(), "horizons_days": (cfg.holding_days,)}
        )

    raw_rows = list(daily_rows)
    if not raw_rows:
        return _empty_report(
            cfg, trend_cfg, status="insufficient_data", reason="no daily rows", rows=[]
        )
    raw_max_day = max(_as_day(row.get("day")) for row in raw_rows)
    requested_end = _as_day(cfg.end_day) if cfg.end_day else raw_max_day
    needed_until = requested_end + timedelta(
        days=cfg.holding_days + cfg.exit_tolerance_days
    )
    rows = _normalized_rows(raw_rows, end_day=needed_until)
    if not rows:
        return _empty_report(
            cfg,
            trend_cfg,
            status="insufficient_data",
            reason="no valid daily rows",
            rows=[],
        )

    all_days = sorted({_as_day(row["day"]) for row in rows})
    first_data_day = all_days[0]
    last_data_day = all_days[-1]
    earliest_signal = max(
        _as_day(cfg.start_day) if cfg.start_day else first_data_day,
        first_data_day + timedelta(days=trend_cfg.lookback_days),
    )
    latest_complete_signal = last_data_day - timedelta(
        days=cfg.holding_days + cfg.exit_tolerance_days
    )
    latest_signal = min(requested_end, latest_complete_signal)
    available_days = [
        day for day in all_days if earliest_signal <= day <= latest_signal
    ]
    signal_days: list[date] = []
    next_allowed = earliest_signal
    for day in available_days:
        if day >= next_allowed:
            signal_days.append(day)
            next_allowed = day + timedelta(days=cfg.rebalance_every_days)
    if len(signal_days) > cfg.max_rebalances:
        signal_days = signal_days[-cfg.max_rebalances :]
    if not signal_days:
        return _empty_report(
            cfg,
            trend_cfg,
            status="insufficient_data",
            reason="history does not cover lookback plus holding period",
            rows=rows,
        )

    source_sha = _daily_rows_sha256(rows)
    comparison_sha = canonical_sha256(
        {
            "source_sha256": source_sha,
            "config": cfg.to_dict(),
            "signal_days": [day.isoformat() for day in signal_days],
        }
    )
    forecaster = PooledTrendForecaster(rows, trend_cfg)
    observations, exact = _build_observation_index(rows)
    buy_multiplier = 1 + cfg.buy_slippage_percent / 100
    sell_multiplier = (1 - cfg.sell_slippage_percent / 100) * (
        1 - cfg.fee_percent / 100
    )
    stale_multiplier = 1 - cfg.stale_exit_haircut_percent / 100

    capital = float(cfg.initial_capital_cents)
    equity_curve = [capital]
    cycle_returns: list[float] = []
    total_buy_notional = 0.0
    total_sell_notional = 0.0
    recommendation_count = 0
    recommendation_winners = 0
    filled_count = 0
    completed_trades = 0
    unfilled_count = 0
    unaffordable_count = 0
    stale_exit_count = 0
    executed_stale_exit_count = 0
    gross_profit = 0.0
    fees_paid = 0.0
    slippage_cost = 0.0
    cycles: list[dict[str, Any]] = []

    for signal_day in signal_days:
        forecasts = [
            row
            for row in forecaster.forecast(signal_day)
            if row["horizon_days"] == cfg.holding_days
        ]
        candidates: list[dict[str, Any]] = []
        for forecast in forecasts:
            name = forecast["market_hash_name"]
            entry_observation = exact.get((name, signal_day))
            if (
                entry_observation is None
                or entry_observation.sales_count < cfg.minimum_daily_sales
            ):
                continue
            entry_mid = entry_observation.price_cents
            entry_cost = entry_mid * buy_multiplier
            expected_net = forecast["predicted_price_cents"] * sell_multiplier
            downside_net = forecast["low_price_cents"] * sell_multiplier
            expected_net_return = (expected_net / entry_cost - 1) * 100
            downside_net_return = (downside_net / entry_cost - 1) * 100
            if expected_net_return < cfg.minimum_expected_net_return_percent:
                continue
            if downside_net_return < cfg.minimum_downside_net_return_percent:
                continue
            candidates.append(
                {
                    **forecast,
                    "entry_mid_cents": entry_mid,
                    "entry_cost_cents": entry_cost,
                    "expected_net_return_percent": expected_net_return,
                    "downside_net_return_percent": downside_net_return,
                }
            )
        candidates.sort(
            key=lambda row: (
                row["downside_net_return_percent"],
                row["expected_net_return_percent"],
                row["confidence"],
                row["market_hash_name"],
            ),
            reverse=True,
        )
        selected = candidates[: cfg.top_k]
        cycle_start_capital = capital
        available_cash = capital
        cycle_pnl = 0.0
        cycle_buys = 0.0
        cycle_sells = 0.0
        cycle_filled = 0
        cycle_winners = 0
        cycle_stale_exits = 0
        target_day = signal_day + timedelta(days=cfg.holding_days)

        for candidate in selected:
            name = candidate["market_hash_name"]
            exit_point = _exit_observation(
                observations[name], target_day, cfg.exit_tolerance_days
            )
            if exit_point is None:
                exit_mid = candidate["entry_mid_cents"] * stale_multiplier
                cycle_stale_exits += 1
                stale_exit_count += 1
            else:
                exit_mid = exit_point.price_cents
            hypothetical_net = (
                exit_mid * sell_multiplier - candidate["entry_cost_cents"]
            )
            recommendation_count += 1
            if hypothetical_net > 0:
                recommendation_winners += 1
                cycle_winners += 1

            if not _deterministic_fill(
                name, signal_day, cfg.fill_probability, cfg.fill_seed
            ):
                unfilled_count += 1
                continue
            filled_count += 1
            cost = float(round(candidate["entry_cost_cents"]))
            if cost > available_cash:
                unaffordable_count += 1
                continue
            available_cash -= cost
            raw_exit = exit_mid
            if exit_point is None:
                executed_stale_exit_count += 1
            sell_after_slippage = raw_exit * (1 - cfg.sell_slippage_percent / 100)
            proceeds = float(round(sell_after_slippage * (1 - cfg.fee_percent / 100)))
            fee = sell_after_slippage * cfg.fee_percent / 100
            slip_cost = (
                candidate["entry_mid_cents"] * cfg.buy_slippage_percent / 100
                + raw_exit * cfg.sell_slippage_percent / 100
            )
            pnl = proceeds - cost
            cycle_pnl += pnl
            cycle_buys += cost
            cycle_sells += raw_exit
            gross_profit += raw_exit - candidate["entry_mid_cents"]
            fees_paid += fee
            slippage_cost += slip_cost
            completed_trades += 1
            cycle_filled += 1

        capital += cycle_pnl
        cycle_return = cycle_pnl / cycle_start_capital if cycle_start_capital else 0.0
        cycle_returns.append(cycle_return)
        equity_curve.append(capital)
        total_buy_notional += cycle_buys
        total_sell_notional += cycle_sells
        cycles.append(
            {
                "signal_day": signal_day.isoformat(),
                "target_day": target_day.isoformat(),
                "eligible_candidates": len(candidates),
                "recommended": len(selected),
                "recommendation_winners": cycle_winners,
                "filled": cycle_filled,
                "stale_exits": cycle_stale_exits,
                "pnl_cents": round(cycle_pnl),
                "return_percent": round(cycle_return * 100, 6),
                "equity_cents": round(capital),
            }
        )

    peak = equity_curve[0]
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    sharpe, sortino = _risk_metrics(cycle_returns, 365 / cfg.rebalance_every_days)
    average_equity = statistics.fmean(equity_curve)
    elapsed_days = max(1, (signal_days[-1] - signal_days[0]).days + cfg.holding_days)
    turnover = (total_buy_notional + total_sell_notional) / 2 / average_equity
    annualized_turnover = turnover * 365 / elapsed_days
    net_profit = capital - cfg.initial_capital_cents
    precision = _ratio(recommendation_winners, recommendation_count)

    metrics = {
        "initial_capital_cents": cfg.initial_capital_cents,
        "final_equity_cents": round(capital),
        "net_profit_cents": round(net_profit),
        "net_return_percent": round(net_profit / cfg.initial_capital_cents * 100, 6),
        "gross_profit_before_costs_cents": round(gross_profit),
        "fees_paid_cents": round(fees_paid),
        "estimated_slippage_cents": round(slippage_cost),
        "precision_at_k": round(precision, 8) if precision is not None else None,
        "recommendations": recommendation_count,
        "recommendation_winners": recommendation_winners,
        "filled_signals": filled_count,
        "completed_trades": completed_trades,
        "unfilled_signals": unfilled_count,
        "unaffordable_signals": unaffordable_count,
        "stale_exit_recommendations": stale_exit_count,
        "stale_exit_trades": executed_stale_exit_count,
        "max_drawdown_percent": round(max_drawdown * 100, 6),
        "sharpe_annualized": round(sharpe, 6) if sharpe is not None else None,
        "sortino_annualized": round(sortino, 6) if sortino is not None else None,
        "turnover_ratio": round(turnover, 8),
        "turnover_annualized": round(annualized_turnover, 8),
        "rebalance_cycles": len(signal_days),
    }
    report = {
        "contract_version": BACKTEST_CONTRACT_VERSION,
        "report_type": "walk_forward_economic_backtest",
        "status": "ready",
        "reason": None,
        "generated_at": utc_now(),
        "model_version": MODEL_VERSION,
        "config": cfg.to_dict(),
        "model_config": trend_cfg.to_dict(),
        "source": {
            "daily_rows": len(rows),
            "market_hash_names": len({row["market_hash_name"] for row in rows}),
            "first_day": rows[0]["day"],
            "last_day": rows[-1]["day"],
            "content_sha256": source_sha,
        },
        "assumptions": {
            "signal_timing": "after daily close",
            "entry_price": "same-day median plus buy slippage",
            "exit_price": "first median on/after target within tolerance",
            "missing_exit": "entry median less stale-exit haircut",
            "fill_simulation": "deterministic SHA-256 sampling",
            "position_size": "at most one indivisible item per signal",
            "fees_apply_to": "sale proceeds",
            "drawdown_sampling": "realized equity at cycle close",
        },
        "metrics": metrics,
        "cycles": cycles,
        "comparison_sha256": comparison_sha,
    }
    report["evaluation_sha256"] = canonical_sha256(
        {
            "comparison_sha256": comparison_sha,
            "model_version": MODEL_VERSION,
            "metrics": metrics,
            "cycles": cycles,
        }
    )
    return report


def evaluate_promotion_economic_gate(
    candidate_report: Mapping[str, Any],
    production_report: Mapping[str, Any] | None = None,
    policy: EconomicGatePolicy | None = None,
) -> dict[str, Any]:
    """Fail-closed economic promotion contract; it never promotes anything."""

    gate = policy or EconomicGatePolicy()
    checks: dict[str, bool] = {}
    reasons: list[str] = []
    candidate = dict(candidate_report)
    metrics = candidate.get("metrics")
    expected_evaluation_sha = canonical_sha256(
        {
            "comparison_sha256": candidate.get("comparison_sha256"),
            "model_version": candidate.get("model_version"),
            "metrics": metrics,
            "cycles": candidate.get("cycles"),
        }
    )
    if (
        candidate.get("status") != "ready"
        or not isinstance(metrics, Mapping)
        or candidate.get("evaluation_sha256") != expected_evaluation_sha
    ):
        return {
            "contract_version": ECONOMIC_GATE_CONTRACT_VERSION,
            "eligible": False,
            "passed": False,
            "reasons": ["candidate backtest is not ready or failed integrity checks"],
            "checks": {},
            "policy": asdict(gate),
        }

    def check(name: str, passed: bool, failure: str) -> None:
        checks[name] = bool(passed)
        if not passed:
            reasons.append(failure)

    completed = int(metrics.get("completed_trades") or 0)
    precision = _safe_float(metrics.get("precision_at_k"), -math.inf)
    net_return = _safe_float(metrics.get("net_return_percent"), -math.inf)
    drawdown = _safe_float(metrics.get("max_drawdown_percent"), math.inf)
    check(
        "minimum_completed_trades",
        completed >= gate.minimum_completed_trades,
        f"completed trades {completed} < {gate.minimum_completed_trades}",
    )
    check(
        "minimum_precision_at_k",
        precision >= gate.minimum_precision_at_k,
        f"precision@K {precision:.4f} < {gate.minimum_precision_at_k:.4f}",
    )
    check(
        "minimum_net_return",
        net_return > gate.minimum_net_return_percent,
        f"net return {net_return:.4f}% <= {gate.minimum_net_return_percent:.4f}%",
    )
    check(
        "maximum_drawdown",
        drawdown <= gate.maximum_drawdown_percent,
        f"drawdown {drawdown:.4f}% > {gate.maximum_drawdown_percent:.4f}%",
    )

    comparable = True
    if production_report is not None:
        production = dict(production_report)
        production_metrics = production.get("metrics")
        production_evaluation_sha = canonical_sha256(
            {
                "comparison_sha256": production.get("comparison_sha256"),
                "model_version": production.get("model_version"),
                "metrics": production_metrics,
                "cycles": production.get("cycles"),
            }
        )
        comparable = bool(
            production.get("status") == "ready"
            and isinstance(production_metrics, Mapping)
            and production.get("evaluation_sha256") == production_evaluation_sha
            and production.get("comparison_sha256")
            and production.get("comparison_sha256")
            == candidate.get("comparison_sha256")
        )
        check(
            "same_evaluation_contract",
            comparable,
            "candidate and production were not evaluated on the same data/config",
        )
        if comparable and isinstance(production_metrics, Mapping):
            old_precision = _safe_float(production_metrics.get("precision_at_k"), 0.0)
            old_return = _safe_float(production_metrics.get("net_return_percent"), 0.0)
            old_drawdown = _safe_float(
                production_metrics.get("max_drawdown_percent"), math.inf
            )
            check(
                "precision_not_regressed",
                precision >= old_precision - gate.maximum_precision_regression,
                "candidate precision@K regressed",
            )
            check(
                "return_not_regressed",
                net_return >= old_return - gate.maximum_return_regression_percent,
                "candidate net return regressed",
            )
            check(
                "drawdown_not_materially_worse",
                drawdown <= old_drawdown + gate.maximum_drawdown_regression_percent,
                "candidate drawdown regressed beyond tolerance",
            )

    return {
        "contract_version": ECONOMIC_GATE_CONTRACT_VERSION,
        "eligible": comparable,
        "passed": comparable and not reasons,
        "reasons": reasons,
        "checks": checks,
        "policy": asdict(gate),
        "candidate_evaluation_sha256": candidate.get("evaluation_sha256"),
        "comparison_sha256": candidate.get("comparison_sha256"),
    }
