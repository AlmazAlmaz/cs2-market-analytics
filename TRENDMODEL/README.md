# Trend model and economic backtest

This package is an offline, read-only research layer over completed sales. It
does not query a marketplace, submit an order, or replace a production model.

## Pipeline

```bash
python3 trend_pipeline.py all
```

The command rebuilds `trend_market.db` from `csfloat_history.db`, writes pooled
7/14/30-day forecasts, and stores a walk-forward report. Source and derived
databases are intentionally separate. Override them with `--source-db` and
`--store-db`, or the `CS2_DB_PATH` and `CS2_TREND_DB_PATH` environment values.

The daily series uses only valid completed sales and `sold_at` event time.
Each item/day contains count, median, mean, min/max, p15/p85, and notional.
Refreshing is atomic: readers see either the previous complete daily table or
the next complete one.

The same atomic refresh materializes `trend_skin_catalog` and
`trend_collection_daily`. Collection names prefer `item_collection_name` and
fall back to the legacy `item_collection` field. A collection price chart is a
normalized constituent index (base 100), not an average of unrelated absolute
skin prices. Its rows also retain volume, active/total member counts, coverage,
and the percentage of constituents moving up.

## Forecast contract

`TRENDMODEL.api.get_trend_summary(conn, name=None)` is a bounded read of
precomputed data. Its top-level keys are:

- `status`, `reason`, `schema_version`, `model_version`
- `as_of_day`, `generated_at`, `requested_name`
- `items`

Each item has `market_hash_name`, `current_price_cents`,
`last_observation_day`, `history_rows`, and a `forecasts` object keyed by
`"7"`, `"14"`, and `"30"`. A forecast has point/low/high prices, expected and
downside return, probability of growth, target day, and confidence.

Bounded read APIs for charts and discovery are:

- `list_collections(path, search=None, include_unknown=True, limit=100, offset=0)`;
- `get_collection_detail(path, name, days=365, member_limit=200)`;
- `get_skin_daily_series(path, name, days=365, start_day=None, end_day=None)`.

They read only the derived store and never aggregate the multi-gigabyte source
database during an HTTP request. Coverage is explicit because older source rows
may not contain collection metadata. `days` is an inclusive calendar window,
not a count of trading rows. Collection returns fail closed when either endpoint
covers less than 20% of constituents; in that case the return is `null` and the
direction is `insufficient_coverage`.

## Backtest contract

`TRENDMODEL.api.get_backtest_report(conn, config=None)` only loads a persisted
report; it never performs expensive work in a web request. The report records
all assumptions and includes net profit/return, precision@K, maximum drawdown,
annualized Sharpe and Sortino, turnover, costs, fill counts, and stale exits.

The signal for day D may use data only through D. Entry and exit slippage,
sale fee, deterministic fill probability, minimum liquidity, indivisible
positions, and missing-exit haircut are explicit configuration values.

`evaluate_promotion_economic_gate(candidate_report, production_report)` is a
fail-closed callable for a future promotion workflow. It only returns a gate
decision; it cannot activate or replace a model. Candidate and production
reports must share the same `comparison_sha256`.
