"""Deterministic pooled 7/14/30-day market trend baseline.

This is intentionally a transparent statistical model rather than a large
neural network. Sparse item histories shrink toward the cross-item market
trend, while prediction intervals widen with observed volatility. Every
forecast records the newest feature date so leakage can be audited directly.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any

from .storage import MODEL_VERSION

SUPPORTED_HORIZONS = (7, 14, 30)
NORMAL_Q15_Z = 1.0364333894937898


def _as_day(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid calendar day: {value!r}") from exc


def _finite_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


@dataclass(frozen=True)
class TrendModelConfig:
    horizons_days: tuple[int, ...] = SUPPORTED_HORIZONS
    lookback_days: int = 90
    smoothing_days: int = 3
    max_staleness_days: int = 7
    min_item_observations: int = 3
    prior_strength: float = 8.0
    max_item_weight: float = 0.90
    max_abs_daily_log_return: float = 0.12
    minimum_daily_volatility: float = 0.005
    # Momentum measured over a few weeks decays; extrapolating it linearly to
    # 30 days turned a short spike into +2000% forecasts.  The expected log
    # return uses a damped horizon tau * (1 - exp(-h / tau)) instead of h.
    trend_damping_days: float = 7.0

    def __post_init__(self) -> None:
        horizons = tuple(int(value) for value in self.horizons_days)
        if not horizons or any(value not in SUPPORTED_HORIZONS for value in horizons):
            raise ValueError("horizons_days must contain only 7, 14, and/or 30")
        if tuple(sorted(set(horizons))) != horizons:
            raise ValueError("horizons_days must be unique and increasing")
        if self.lookback_days < max(horizons):
            raise ValueError("lookback_days must cover the longest horizon")
        if self.smoothing_days < 1:
            raise ValueError("smoothing_days must be positive")
        if self.max_staleness_days < 0:
            raise ValueError("max_staleness_days must be non-negative")
        if self.min_item_observations < 2:
            raise ValueError("min_item_observations must be at least 2")
        if not math.isfinite(self.prior_strength) or self.prior_strength <= 0:
            raise ValueError("prior_strength must be positive")
        if not math.isfinite(self.max_item_weight) or not 0 < self.max_item_weight <= 1:
            raise ValueError("max_item_weight must be in (0, 1]")
        if (
            not math.isfinite(self.max_abs_daily_log_return)
            or self.max_abs_daily_log_return <= 0
        ):
            raise ValueError("max_abs_daily_log_return must be positive")
        if (
            not math.isfinite(self.minimum_daily_volatility)
            or self.minimum_daily_volatility <= 0
        ):
            raise ValueError("minimum_daily_volatility must be positive")
        if (
            not math.isfinite(self.trend_damping_days)
            or self.trend_damping_days <= 0
        ):
            raise ValueError("trend_damping_days must be positive")

    def damped_horizon(self, horizon_days: int) -> float:
        tau = float(self.trend_damping_days)
        return tau * (1.0 - math.exp(-float(horizon_days) / tau))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["horizons_days"] = list(self.horizons_days)
        return value


@dataclass(frozen=True)
class DailyPoint:
    day: date
    median_price_cents: float
    sales_count: int


@dataclass(frozen=True)
class _ItemState:
    name: str
    current_price: float
    last_day: date
    history_rows: int
    history_days: int
    raw_slope: float
    volatility: float


def _median(values: Sequence[float], default: float = 0.0) -> float:
    return float(statistics.median(values)) if values else float(default)


def _clip(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


class PooledTrendForecaster:
    """Index aggregate rows once, then produce strictly as-of forecasts."""

    def __init__(
        self,
        daily_rows: Iterable[Mapping[str, Any]],
        config: TrendModelConfig | None = None,
    ) -> None:
        self.config = config or TrendModelConfig()
        grouped: dict[str, dict[date, tuple[list[float], int]]] = defaultdict(dict)
        for raw in daily_rows:
            name = str(raw.get("market_hash_name") or "").strip()
            if not name:
                continue
            day = _as_day(raw.get("day"))
            price = _finite_float(raw.get("median_price_cents"), "median_price_cents")
            if price <= 0:
                continue
            try:
                sales_count = max(1, int(raw.get("sales_count") or 1))
            except (TypeError, ValueError) as exc:
                raise ValueError("sales_count must be an integer") from exc
            prices, existing_count = grouped[name].setdefault(day, ([], 0))
            prices.append(price)
            grouped[name][day] = (prices, existing_count + sales_count)

        self._points: dict[str, tuple[DailyPoint, ...]] = {}
        self._days: dict[str, tuple[date, ...]] = {}
        for name, values_by_day in grouped.items():
            points = tuple(
                DailyPoint(day, _median(prices), count)
                for day, (prices, count) in sorted(values_by_day.items())
            )
            self._points[name] = points
            self._days[name] = tuple(point.day for point in points)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._points))

    def _history(self, name: str, as_of: date) -> tuple[DailyPoint, ...]:
        points = self._points.get(name, ())
        days = self._days.get(name, ())
        cutoff = bisect.bisect_right(days, as_of)
        if not cutoff:
            return ()
        first_day = as_of - timedelta(days=self.config.lookback_days)
        start = bisect.bisect_left(days, first_day, hi=cutoff)
        return points[start:cutoff]

    def _item_state(self, name: str, as_of: date) -> _ItemState | None:
        history = self._history(name, as_of)
        if len(history) < self.config.min_item_observations:
            return None
        last_day = history[-1].day
        if (as_of - last_day).days > self.config.max_staleness_days:
            return None

        smooth_after = last_day - timedelta(days=self.config.smoothing_days - 1)
        recent_prices = [
            point.median_price_cents for point in history if point.day >= smooth_after
        ]
        current_price = _median(recent_prices, history[-1].median_price_cents)

        slopes: list[float] = []
        for point in history[:-1]:
            elapsed = (last_day - point.day).days
            if elapsed < self.config.smoothing_days:
                continue
            raw = math.log(current_price / point.median_price_cents) / elapsed
            slopes.append(
                _clip(
                    raw,
                    -self.config.max_abs_daily_log_return,
                    self.config.max_abs_daily_log_return,
                )
            )
        if not slopes:
            return None

        normalized_returns: list[float] = []
        for previous, current in pairwise(history):
            elapsed = max(1, (current.day - previous.day).days)
            value = math.log(
                current.median_price_cents / previous.median_price_cents
            ) / math.sqrt(elapsed)
            normalized_returns.append(
                _clip(
                    value,
                    -self.config.max_abs_daily_log_return * math.sqrt(elapsed),
                    self.config.max_abs_daily_log_return * math.sqrt(elapsed),
                )
            )
        center = _median(normalized_returns)
        mad = _median([abs(value - center) for value in normalized_returns])
        volatility = max(self.config.minimum_daily_volatility, 1.4826 * mad)
        return _ItemState(
            name=name,
            current_price=current_price,
            last_day=last_day,
            history_rows=len(history),
            history_days=max(1, (last_day - history[0].day).days),
            raw_slope=_median(slopes),
            volatility=volatility,
        )

    def forecast(
        self,
        as_of_day: str | date | datetime,
        *,
        market_hash_names: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        as_of = _as_day(as_of_day)
        requested = (
            sorted(
                {str(name).strip() for name in market_hash_names if str(name).strip()}
            )
            if market_hash_names is not None
            else list(self.names)
        )

        # Pooled priors must always use the whole eligible universe. Limiting
        # output names must not alter a skin's prediction.
        states = [
            state
            for name in self.names
            if (state := self._item_state(name, as_of)) is not None
        ]
        if not states:
            return []
        by_name = {state.name: state for state in states}
        pooled_slope = _median([state.raw_slope for state in states])
        pooled_volatility = _median(
            [state.volatility for state in states],
            self.config.minimum_daily_volatility,
        )

        forecasts: list[dict[str, Any]] = []
        for name in requested:
            state = by_name.get(name)
            if state is None:
                continue
            evidence = max(
                0, state.history_rows - self.config.min_item_observations + 1
            )
            item_weight = min(
                self.config.max_item_weight,
                evidence / (evidence + self.config.prior_strength),
            )
            slope = item_weight * state.raw_slope + (1 - item_weight) * pooled_slope
            volatility = math.sqrt(
                item_weight * state.volatility**2
                + (1 - item_weight) * pooled_volatility**2
            )
            staleness = (as_of - state.last_day).days
            freshness = math.exp(-staleness / max(1, self.config.max_staleness_days))
            volatility_penalty = math.exp(-volatility * math.sqrt(30))
            confidence = _clip(item_weight * freshness * volatility_penalty, 0.0, 1.0)

            for horizon in self.config.horizons_days:
                expected_log_return = slope * self.config.damped_horizon(horizon)
                uncertainty = volatility * math.sqrt(horizon)
                point = state.current_price * math.exp(expected_log_return)
                low = state.current_price * math.exp(
                    expected_log_return - NORMAL_Q15_Z * uncertainty
                )
                high = state.current_price * math.exp(
                    expected_log_return + NORMAL_Q15_Z * uncertainty
                )
                probability_up = (
                    _normal_cdf(expected_log_return / uncertainty)
                    if uncertainty > 0
                    else (1.0 if expected_log_return > 0 else 0.5)
                )
                current_cents = max(1, round(state.current_price))
                point_cents = max(1, round(point))
                low_cents = min(point_cents, max(1, round(low)))
                high_cents = max(point_cents, round(high))
                forecasts.append(
                    {
                        "schema_version": 1,
                        "model_version": MODEL_VERSION,
                        "market_hash_name": name,
                        "as_of_day": as_of.isoformat(),
                        "max_feature_day": state.last_day.isoformat(),
                        "last_observation_day": state.last_day.isoformat(),
                        "horizon_days": horizon,
                        "target_day": (as_of + timedelta(days=horizon)).isoformat(),
                        "current_price_cents": current_cents,
                        "predicted_price_cents": point_cents,
                        "low_price_cents": low_cents,
                        "high_price_cents": high_cents,
                        "expected_return_percent": round(
                            (point / state.current_price - 1) * 100, 6
                        ),
                        "downside_return_percent": round(
                            (low / state.current_price - 1) * 100, 6
                        ),
                        "probability_up": round(probability_up, 8),
                        "confidence": round(confidence, 8),
                        "history_rows": state.history_rows,
                        "history_days": state.history_days,
                        "item_weight": round(item_weight, 8),
                        "daily_log_trend": round(slope, 10),
                        "daily_volatility": round(volatility, 10),
                    }
                )
        return forecasts


def build_latest_forecasts(
    daily_rows: Iterable[Mapping[str, Any]],
    *,
    as_of_day: str | date | datetime | None = None,
    config: TrendModelConfig | None = None,
    market_hash_names: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    rows = list(daily_rows)
    if not rows:
        return []
    effective_as_of = as_of_day or max(_as_day(row.get("day")) for row in rows)
    return PooledTrendForecaster(rows, config).forecast(
        effective_as_of, market_hash_names=market_hash_names
    )
