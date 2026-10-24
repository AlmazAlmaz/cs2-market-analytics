"""Outcome-aware deal scoring and portfolio construction.

The module deliberately has no network or trading side effects.  It stores an
immutable snapshot when a scanner recommendation is first opened, reconciles
that snapshot only against confirmed rows in ``sales``, and exposes bounded
read helpers suitable for the dashboard.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 2
ANNOTATION_SCHEMA_VERSION = 1
DEFAULT_FEE_PERCENT = 4.0
DEFAULT_OUTCOME_HORIZON_DAYS = 30
MAX_HISTORY_ROWS = 20_000
MAX_ANNOTATION_ROWS = 500
MAX_PORTFOLIO_CANDIDATES = 500
MIN_SEGMENT_MODEL_SAMPLES = 24
MIN_SEGMENT_SALE_OUTCOMES = 30
ADVISORY_MODEL_NAMES = frozenset({"vega"})

RISK_PROFILES = {
    "conservative": {
        "max_risk_score": 35.0,
        "min_conservative_roi_percent": 0.0,
    },
    "balanced": {
        "max_risk_score": 60.0,
        "min_conservative_roi_percent": -5.0,
    },
    "aggressive": {
        "max_risk_score": 85.0,
        "min_conservative_roi_percent": -20.0,
    },
}


def _utc_now(now=None):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value=None):
    return _utc_now(value).isoformat()


def _parse_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif value in (None, ""):
        return None
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finite(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value, default=None):
    number = _finite(value)
    return round(number) if number is not None else default


def _clamp(value, minimum, maximum):
    return min(max(float(value), float(minimum)), float(maximum))


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _json_object(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _table_exists(conn, table_name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (str(table_name),),
    ).fetchone() is not None


def _bounded_int(value, *, minimum, maximum, name):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def init_schema(conn):
    """Create the additive, idempotent intelligence schema without committing."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_schema_versions (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_deal_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT NOT NULL,
            model_name TEXT NOT NULL,
            artifact_version TEXT,
            model_trained_at TEXT,
            recommended_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            observation_count INTEGER NOT NULL DEFAULT 1,
            evaluation_horizon_days INTEGER NOT NULL DEFAULT 30,
            segment_key TEXT NOT NULL,
            segment_family TEXT NOT NULL,
            segment_variant TEXT NOT NULL,
            segment_price_band TEXT NOT NULL,
            segment_sticker_band TEXT NOT NULL,
            skin_name TEXT,
            base_skin_name TEXT,
            collection_name TEXT,
            buy_price_cents INTEGER NOT NULL,
            predicted_price_cents INTEGER NOT NULL,
            low_price_cents INTEGER,
            high_price_cents INTEGER,
            fee_percent REAL NOT NULL DEFAULT 4.0,
            liquidity_score REAL,
            sales_per_day REAL,
            outcome_state TEXT NOT NULL DEFAULT 'open'
                CHECK(outcome_state IN ('open', 'sold', 'not_sold')),
            outcome_at TEXT,
            sold_at TEXT,
            sale_price_cents INTEGER,
            days_to_sell REAL,
            realized_profit_cents INTEGER,
            realized_roi_percent REAL,
            outcome_source TEXT,
            deal_payload_json TEXT NOT NULL,
            prediction_json TEXT NOT NULL
        )
        """
    )
    # Version 2 keys open recommendations by model build as well as model name.
    # Dropping the superseded index changes no rows and permits clean upgrades.
    conn.execute("DROP INDEX IF EXISTS idx_deal_recommendations_one_open")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_deal_recommendations_one_open_version
        ON model_deal_recommendations(
            listing_id,
            model_name,
            COALESCE(artifact_version, ''),
            COALESCE(model_trained_at, '')
        )
        WHERE outcome_state = 'open'
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_deal_recommendations_outcome_segment
        ON model_deal_recommendations(outcome_state, segment_key, model_name, id DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_deal_recommendations_open_age
        ON model_deal_recommendations(recommended_at, id)
        WHERE outcome_state = 'open'
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_deal_intelligence (
            listing_id TEXT PRIMARY KEY,
            computed_at TEXT NOT NULL,
            segment_key TEXT NOT NULL,
            segment_family TEXT NOT NULL,
            skin_name TEXT,
            collection_name TEXT,
            csfloat_url TEXT,
            buy_price_cents INTEGER NOT NULL,
            model_count INTEGER NOT NULL,
            model_names_json TEXT NOT NULL,
            model_weights_json TEXT NOT NULL,
            weight_source TEXT NOT NULL,
            weight_sample_count INTEGER NOT NULL DEFAULT 0,
            ensemble_predicted_price_cents INTEGER NOT NULL,
            ensemble_low_price_cents INTEGER NOT NULL,
            ensemble_high_price_cents INTEGER NOT NULL,
            expected_profit_cents INTEGER NOT NULL,
            expected_roi_percent REAL NOT NULL,
            conservative_profit_cents INTEGER NOT NULL,
            conservative_roi_percent REAL NOT NULL,
            sell_probability_7d REAL NOT NULL,
            sell_probability_30d REAL NOT NULL,
            estimated_days_to_sell REAL NOT NULL,
            sell_estimate_source TEXT NOT NULL,
            sell_estimate_sample_count INTEGER NOT NULL DEFAULT 0,
            uncertainty_percent REAL NOT NULL,
            model_disagreement_percent REAL NOT NULL,
            risk_score REAL NOT NULL,
            risk_level TEXT NOT NULL,
            ranking_score REAL NOT NULL,
            details_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_deal_intelligence_rank
        ON model_deal_intelligence(ranking_score DESC, computed_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_deal_intelligence_risk
        ON model_deal_intelligence(risk_score, conservative_roi_percent DESC)
        """
    )
    conn.execute(
        """
        INSERT INTO app_schema_versions(component, version, migrated_at)
        VALUES ('deal_intelligence', ?, ?)
        ON CONFLICT(component) DO UPDATE SET
            version = excluded.version,
            migrated_at = excluded.migrated_at
        WHERE app_schema_versions.version < excluded.version
        """,
        (SCHEMA_VERSION, _iso()),
    )


def segment_for(deal_payload, prediction=None):
    """Return stable coarse segments; coarse keys avoid tiny data buckets."""

    payload = deal_payload if isinstance(deal_payload, dict) else {}
    prediction = prediction if isinstance(prediction, dict) else {}
    name = str(prediction.get("name") or payload.get("name") or "").strip()
    lower_name = name.lower()
    buy_price = max(
        0,
        _int(prediction.get("buy_price_cents"), _int(payload.get("buy_price_cents"), 0)),
    )
    if buy_price < 2_000:
        price_band = "under_20"
    elif buy_price < 10_000:
        price_band = "20_100"
    elif buy_price < 50_000:
        price_band = "100_500"
    else:
        price_band = "500_plus"

    stickers = payload.get("stickers") if isinstance(payload.get("stickers"), list) else []
    sticker_count = max(0, _int(prediction.get("sticker_count"), len(stickers)))
    if sticker_count == 0:
        sticker_band = "none"
    elif sticker_count == 1:
        sticker_band = "single"
    elif sticker_count < 4:
        sticker_band = "two_three"
    else:
        sticker_band = "four_plus"

    if _truthy(payload.get("item_is_souvenir")) or "souvenir" in lower_name:
        variant = "souvenir"
    elif _truthy(payload.get("item_is_stattrak")) or "stattrak" in lower_name:
        variant = "stattrak"
    else:
        variant = "standard"

    family = f"{variant}|{price_band}"
    return {
        "key": f"{family}|{sticker_band}",
        "family": family,
        "variant": variant,
        "price_band": price_band,
        "sticker_band": sticker_band,
        "skin_name": name or None,
        "base_skin_name": (
            str(prediction.get("base_skin_name") or payload.get("base_skin_name") or "").strip()
            or None
        ),
        "collection_name": (
            str(payload.get("item_collection_name") or "").strip() or None
        ),
    }


def record_recommendation(
    conn,
    listing_data,
    prediction,
    model_name,
    *,
    observed_at=None,
    evaluation_horizon_days=DEFAULT_OUTCOME_HORIZON_DAYS,
):
    """Open or touch one recommendation while preserving its first snapshot."""

    payload = (
        listing_data.get("deal_payload", {})
        if isinstance(listing_data, dict)
        else {}
    )
    listing_id = str(
        (listing_data or {}).get("listing_id")
        or payload.get("listing_id")
        or ""
    ).strip()
    if not listing_id:
        raise ValueError("listing_id is required to record a recommendation")
    model_name = str(model_name or "").strip().lower()
    if not model_name:
        raise ValueError("model_name is required to record a recommendation")
    buy_price = _int(prediction.get("buy_price_cents"))
    predicted_price = _int(prediction.get("predicted_price_cents"))
    if buy_price is None or buy_price <= 0 or predicted_price is None or predicted_price <= 0:
        raise ValueError("positive buy and predicted prices are required")
    horizon = _bounded_int(
        evaluation_horizon_days,
        minimum=1,
        maximum=365,
        name="evaluation_horizon_days",
    )
    now_text = _iso(_parse_datetime(observed_at) or _utc_now())
    artifact_version = str(prediction.get("artifact_version") or "").strip()
    model_trained_at = str(prediction.get("trained_at") or "").strip()
    existing = conn.execute(
        """
        SELECT id FROM model_deal_recommendations
        WHERE listing_id = ? AND model_name = ? AND outcome_state = 'open'
            AND COALESCE(artifact_version, '') = ?
            AND COALESCE(model_trained_at, '') = ?
        """,
        (listing_id, model_name, artifact_version, model_trained_at),
    ).fetchone()
    if existing is not None:
        conn.execute(
            """
            UPDATE model_deal_recommendations
            SET last_observed_at = ?, observation_count = observation_count + 1
            WHERE id = ?
            """,
            (now_text, existing[0]),
        )
        return {"recommendation_id": int(existing[0]), "created": False}

    segment = segment_for(payload, prediction)
    fee_percent = _clamp(
        _finite(prediction.get("fee_percent"), DEFAULT_FEE_PERCENT), 0, 100
    )
    cursor = conn.execute(
        """
        INSERT INTO model_deal_recommendations (
            listing_id, model_name, artifact_version, model_trained_at,
            recommended_at, last_observed_at, evaluation_horizon_days,
            segment_key, segment_family, segment_variant, segment_price_band,
            segment_sticker_band, skin_name, base_skin_name, collection_name,
            buy_price_cents, predicted_price_cents, low_price_cents,
            high_price_cents, fee_percent, liquidity_score, sales_per_day,
            deal_payload_json, prediction_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            listing_id,
            model_name,
            artifact_version or None,
            model_trained_at or None,
            now_text,
            now_text,
            horizon,
            segment["key"],
            segment["family"],
            segment["variant"],
            segment["price_band"],
            segment["sticker_band"],
            segment["skin_name"],
            segment["base_skin_name"],
            segment["collection_name"],
            buy_price,
            predicted_price,
            _int(prediction.get("low_price_cents")),
            _int(prediction.get("high_price_cents")),
            fee_percent,
            _finite(prediction.get("liquidity_score")),
            _finite(prediction.get("sales_per_day")),
            _json_dumps(payload),
            _json_dumps(prediction),
        ),
    )
    return {"recommendation_id": int(cursor.lastrowid), "created": True}


def _confirmed_sales(conn, listing_ids):
    if not listing_ids or not _table_exists(conn, "sales"):
        return {}
    result = {}
    ids = list(dict.fromkeys(str(value) for value in listing_ids))
    for offset in range(0, len(ids), 400):
        batch = ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT listing_id, price_cents, sold_at
            FROM sales
            WHERE listing_id IN ({placeholders})
                AND LOWER(COALESCE(state, '')) = 'sold'
                AND price_cents IS NOT NULL
                AND sold_at IS NOT NULL
            """,
            batch,
        )
        for row in rows:
            result[str(row[0])] = {
                "price_cents": _int(row[1]),
                "sold_at": row[2],
            }
    return result


def reconcile_outcomes(conn, *, now=None, max_rows=5_000):
    """Resolve open recommendations from confirmed sales or their horizon."""

    limit = _bounded_int(max_rows, minimum=1, maximum=20_000, name="max_rows")
    current = _utc_now(now)
    rows = list(
        conn.execute(
            """
            SELECT id, listing_id, recommended_at, evaluation_horizon_days,
                buy_price_cents, fee_percent
            FROM model_deal_recommendations
            WHERE outcome_state = 'open'
            ORDER BY recommended_at, id
            LIMIT ?
            """,
            (limit,),
        )
    )
    sales = _confirmed_sales(conn, [row[1] for row in rows])
    sold_updates = []
    expired_updates = []
    for row in rows:
        recommendation_id = int(row[0])
        listing_id = str(row[1])
        recommended_at = _parse_datetime(row[2])
        if recommended_at is None:
            continue
        sale = sales.get(listing_id)
        sold_at = _parse_datetime(sale.get("sold_at")) if sale else None
        sale_price = _int(sale.get("price_cents")) if sale else None
        if sold_at is not None and sold_at >= recommended_at and sale_price and sale_price > 0:
            fee = _clamp(_finite(row[5], DEFAULT_FEE_PERCENT), 0, 100)
            net_sale = round(sale_price * (1 - fee / 100))
            buy_price = max(1, _int(row[4], 1))
            profit = net_sale - buy_price
            days = max(0.0, (sold_at - recommended_at).total_seconds() / 86_400)
            sold_updates.append(
                (
                    sold_at.isoformat(),
                    sold_at.isoformat(),
                    sale_price,
                    days,
                    profit,
                    profit / buy_price * 100,
                    "sales_table",
                    recommendation_id,
                )
            )
            continue

        horizon_days = max(1, _int(row[3], DEFAULT_OUTCOME_HORIZON_DAYS))
        if current >= recommended_at + timedelta(days=horizon_days):
            expired_updates.append(
                (current.isoformat(), "evaluation_horizon", recommendation_id)
            )

    if sold_updates:
        conn.executemany(
            """
            UPDATE model_deal_recommendations
            SET outcome_state = 'sold', outcome_at = ?, sold_at = ?,
                sale_price_cents = ?, days_to_sell = ?,
                realized_profit_cents = ?, realized_roi_percent = ?,
                outcome_source = ?
            WHERE id = ? AND outcome_state = 'open'
            """,
            sold_updates,
        )
    if expired_updates:
        conn.executemany(
            """
            UPDATE model_deal_recommendations
            SET outcome_state = 'not_sold', outcome_at = ?, outcome_source = ?
            WHERE id = ? AND outcome_state = 'open'
            """,
            expired_updates,
        )
    remaining = conn.execute(
        "SELECT COUNT(*) FROM model_deal_recommendations WHERE outcome_state = 'open'"
    ).fetchone()[0]
    return {
        "checked": len(rows),
        "sold": len(sold_updates),
        "not_sold": len(expired_updates),
        "remaining_open": int(remaining),
    }


def load_history_profile(conn, *, max_rows=MAX_HISTORY_ROWS):
    """Load a bounded closed-outcome sample once for many scoring calls."""

    limit = _bounded_int(max_rows, minimum=1, maximum=MAX_HISTORY_ROWS, name="max_rows")
    if not _table_exists(conn, "model_deal_recommendations"):
        return {"events": [], "truncated": False, "loaded_rows": 0}
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, listing_id, model_name, artifact_version, model_trained_at,
                segment_key, segment_family, outcome_state,
                predicted_price_cents, sale_price_cents, days_to_sell,
                realized_profit_cents, realized_roi_percent
            FROM model_deal_recommendations
            WHERE outcome_state IN ('sold', 'not_sold')
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit + 1,),
        )
    ]
    truncated = len(rows) > limit
    events = rows[:limit]
    return {
        "events": events,
        "truncated": truncated,
        "loaded_rows": len(events),
    }


def get_outcome_summary(conn, *, max_rows=MAX_HISTORY_ROWS):
    """Return bounded audit metrics for completed recommendation outcomes."""

    profile = load_history_profile(conn, max_rows=max_rows)
    events = profile["events"]
    by_model = defaultdict(list)
    for event in events:
        by_model[str(event.get("model_name") or "unknown")].append(event)

    def summarize(rows):
        sold = [row for row in rows if row.get("outcome_state") == "sold"]
        percentage_errors = []
        days = []
        realized_profits = []
        for row in sold:
            actual = _finite(row.get("sale_price_cents"))
            predicted = _finite(row.get("predicted_price_cents"))
            if actual and actual > 0 and predicted is not None:
                percentage_errors.append(abs(predicted - actual) / actual * 100)
            value = _finite(row.get("days_to_sell"))
            if value is not None:
                days.append(value)
            profit = _int(row.get("realized_profit_cents"))
            if profit is not None:
                realized_profits.append(profit)
        return {
            "closed_recommendations": len(rows),
            "sold_recommendations": len(sold),
            "not_sold_recommendations": len(rows) - len(sold),
            "sell_through_rate": len(sold) / len(rows) if rows else None,
            "median_days_to_sell": float(statistics.median(days)) if days else None,
            "median_absolute_percentage_error": (
                float(statistics.median(percentage_errors))
                if percentage_errors
                else None
            ),
            "realized_profit_cents": sum(realized_profits),
            "profitable_sale_rate": (
                sum(value > 0 for value in realized_profits) / len(realized_profits)
                if realized_profits
                else None
            ),
        }

    summary = summarize(events)
    unique_listings = len({str(row.get("listing_id")) for row in events})
    return {
        "outcome_schema_version": 1,
        **summary,
        "unique_listings": unique_listings,
        "history_ready": unique_listings >= MIN_SEGMENT_SALE_OUTCOMES,
        "loaded_rows": profile["loaded_rows"],
        "truncated": profile["truncated"],
        "models": {
            model_name: summarize(rows)
            for model_name, rows in sorted(by_model.items())
        },
    }


def _prediction_rows(predictions):
    if isinstance(predictions, dict):
        iterable = predictions.values()
    else:
        iterable = predictions or []
    by_model = {}
    for raw in iterable:
        if not isinstance(raw, dict):
            continue
        model_name = str(raw.get("model_name") or "").strip().lower()
        point = _int(raw.get("predicted_price_cents"))
        if (
            not model_name
            or model_name in ADVISORY_MODEL_NAMES
            or point is None
            or point <= 0
        ):
            continue
        item = dict(raw)
        item["model_name"] = model_name
        item["predicted_price_cents"] = point
        stored = _json_object(item.get("prediction_json"))
        item["artifact_version"] = item.get("artifact_version") or stored.get(
            "artifact_version"
        )
        item["trained_at"] = item.get("trained_at") or stored.get("trained_at")
        by_model[model_name] = item
    return list(by_model.values())


def _scope_errors(events, predictions, scope, segment, minimum):
    errors = defaultdict(list)
    listing_ids = set()
    wanted_models = {row["model_name"] for row in predictions}
    current_versions = {
        row["model_name"]: (
            str(row.get("artifact_version") or ""),
            str(row.get("trained_at") or ""),
        )
        for row in predictions
    }
    for event in events:
        if event.get("outcome_state") != "sold":
            continue
        if scope == "segment" and event.get("segment_key") != segment["key"]:
            continue
        if scope == "family" and event.get("segment_family") != segment["family"]:
            continue
        model = str(event.get("model_name") or "").lower()
        if model not in wanted_models:
            continue
        current_artifact, current_trained_at = current_versions[model]
        event_artifact = str(event.get("artifact_version") or "")
        event_trained_at = str(event.get("model_trained_at") or "")
        if event_artifact and current_artifact and event_artifact != current_artifact:
            continue
        if event_trained_at and current_trained_at and event_trained_at != current_trained_at:
            continue
        actual = _finite(event.get("sale_price_cents"))
        predicted = _finite(event.get("predicted_price_cents"))
        if not actual or actual <= 0 or predicted is None:
            continue
        errors[model].append(abs(predicted - actual) / actual)
        listing_ids.add(str(event.get("listing_id") or event.get("id")))
    if not wanted_models or any(len(errors[name]) < minimum for name in wanted_models):
        return None
    if len(listing_ids) < minimum:
        return None
    return errors, len(listing_ids)


def dynamic_ensemble_weights(
    predictions,
    segment,
    profile=None,
    *,
    min_model_samples=MIN_SEGMENT_MODEL_SAMPLES,
):
    """Choose segment/family/global inverse-error weights or equal weights."""

    rows = _prediction_rows(predictions)
    if not rows:
        raise ValueError("at least one valid model prediction is required")
    minimum = _bounded_int(
        min_model_samples, minimum=2, maximum=10_000, name="min_model_samples"
    )
    events = (profile or {}).get("events") or []
    selected = None
    source = "equal"
    for scope in ("segment", "family", "global"):
        candidate = _scope_errors(events, rows, scope, segment, minimum)
        if candidate is not None:
            selected = candidate
            source = scope
            break

    model_names = [row["model_name"] for row in rows]
    if selected is None:
        equal = 1 / len(model_names)
        return {
            "weights": {name: equal for name in model_names},
            "source": source,
            "sample_count": 0,
            "segment_key": segment["key"],
            "model_errors": {},
        }

    errors, distinct_listings = selected
    median_errors = {
        name: max(0.02, float(statistics.median(values)))
        for name, values in errors.items()
    }
    raw_weights = {name: 1 / error for name, error in median_errors.items()}
    total = sum(raw_weights.values()) or 1.0
    weights = {name: value / total for name, value in raw_weights.items()}
    # Avoid one historical bucket completely silencing a model after drift.
    floor = 0.10 if len(weights) > 1 else 1.0
    weights = {name: max(floor, value) for name, value in weights.items()}
    total = sum(weights.values())
    weights = {name: value / total for name, value in weights.items()}
    return {
        "weights": weights,
        "source": source,
        "sample_count": int(distinct_listings),
        "segment_key": segment["key"],
        "model_errors": {
            name: {
                "median_absolute_percentage_error": median_errors[name],
                "samples": len(errors[name]),
            }
            for name in sorted(median_errors)
        },
    }


def _deduplicated_sale_events(events, scope, segment):
    result = {}
    for event in events:
        if scope == "segment" and event.get("segment_key") != segment["key"]:
            continue
        if scope == "family" and event.get("segment_family") != segment["family"]:
            continue
        listing_id = str(event.get("listing_id") or event.get("id"))
        result.setdefault(listing_id, event)
    return list(result.values())


def estimate_sale_probability(
    prediction,
    segment,
    profile=None,
    *,
    min_segment_outcomes=MIN_SEGMENT_SALE_OUTCOMES,
):
    """Estimate 7/30-day sale probability with explicit empirical fallback."""

    minimum = _bounded_int(
        min_segment_outcomes,
        minimum=5,
        maximum=10_000,
        name="min_segment_outcomes",
    )
    events = (profile or {}).get("events") or []
    selected = []
    source = "heuristic"
    for scope in ("segment", "family", "global"):
        candidates = _deduplicated_sale_events(events, scope, segment)
        if len(candidates) >= minimum:
            selected = candidates
            source = scope
            break

    if selected:
        total = len(selected)
        sold = [event for event in selected if event.get("outcome_state") == "sold"]
        sold_7d = [
            event
            for event in sold
            if _finite(event.get("days_to_sell"), math.inf) <= 7
        ]
        # Mild beta smoothing keeps small qualified buckets away from 0/1.
        probability_30d = (len(sold) + 2) / (total + 4)
        probability_7d = (len(sold_7d) + 1) / (total + 4)
        sale_days = [
            _finite(event.get("days_to_sell"))
            for event in sold
            if _finite(event.get("days_to_sell")) is not None
        ]
        estimated_days = (
            float(statistics.median(sale_days))
            if sale_days
            else float(DEFAULT_OUTCOME_HORIZON_DAYS)
        )
        return {
            "within_7d": _clamp(probability_7d, 0.01, probability_30d),
            "within_30d": _clamp(probability_30d, 0.02, 0.98),
            "estimated_days": _clamp(estimated_days, 0.25, 365),
            "source": source,
            "sample_count": total,
        }

    liquidity = _clamp(_finite(prediction.get("liquidity_score"), 0), 0, 100) / 100
    sales_per_day = max(0.0, _finite(prediction.get("sales_per_day"), 0))
    velocity = 1 - math.exp(-sales_per_day / 5)
    probability_30d = _clamp(0.05 + 0.70 * liquidity + 0.20 * velocity, 0.03, 0.95)
    probability_7d = _clamp(
        probability_30d * (0.20 + 0.40 * velocity), 0.01, probability_30d
    )
    estimated_days = 30 * (1 - 0.70 * liquidity) / (0.50 + velocity)
    return {
        "within_7d": probability_7d,
        "within_30d": probability_30d,
        "estimated_days": _clamp(estimated_days, 0.5, 90),
        "source": "heuristic",
        "sample_count": 0,
    }


def score_prediction_group(predictions, deal_payload=None, profile=None, *, now=None):
    """Pure scoring core used by scanner writes and request-time previews."""

    rows = _prediction_rows(predictions)
    if not rows:
        raise ValueError("at least one valid model prediction is required")
    payload = deal_payload if isinstance(deal_payload, dict) else {}
    segment = segment_for(payload, rows[0])
    weight_info = dynamic_ensemble_weights(rows, segment, profile)
    weights = weight_info["weights"]

    def weighted(field, fallback_field="predicted_price_cents"):
        total = 0.0
        for row in rows:
            fallback = row.get(fallback_field)
            value = _finite(row.get(field), _finite(fallback, 0))
            total += weights[row["model_name"]] * value
        return total

    point = max(1, round(weighted("predicted_price_cents")))
    low = max(1, min(point, round(weighted("low_price_cents"))))
    high = max(point, round(weighted("high_price_cents")))
    buy_price = _int(rows[0].get("buy_price_cents"), _int(payload.get("buy_price_cents")))
    if buy_price is None or buy_price <= 0:
        raise ValueError("positive buy_price_cents is required")

    fee_values = []
    for row in rows:
        prediction_payload = _json_object(row.get("prediction_json"))
        fee_values.append(
            _finite(row.get("fee_percent"), _finite(prediction_payload.get("fee_percent")))
        )
    fee_values = [value for value in fee_values if value is not None]
    fee_percent = _clamp(
        statistics.median(fee_values) if fee_values else DEFAULT_FEE_PERCENT,
        0,
        100,
    )
    fee_multiplier = 1 - fee_percent / 100
    expected_net = round(point * fee_multiplier)
    conservative_net = round(low * fee_multiplier)
    expected_profit = expected_net - buy_price
    conservative_profit = conservative_net - buy_price
    expected_roi = expected_profit / buy_price * 100
    conservative_roi = conservative_profit / buy_price * 100

    representative = max(
        rows,
        key=lambda row: weights.get(row["model_name"], 0),
    )
    sale = estimate_sale_probability(representative, segment, profile)
    uncertainty_percent = (high - low) / max(point, 1) * 100
    points = [row["predicted_price_cents"] for row in rows]
    disagreement_percent = (max(points) - min(points)) / max(point, 1) * 100

    downside_risk = _clamp(max(0.0, -conservative_roi) / 25, 0, 1) * 35
    liquidity_risk = (1 - sale["within_30d"]) * 30
    interval_risk = _clamp(uncertainty_percent / 80, 0, 1) * 20
    disagreement_risk = _clamp(disagreement_percent / 50, 0, 1) * 15
    history_penalty = 5 if sale["source"] == "heuristic" else 0
    risk_score = _clamp(
        downside_risk + liquidity_risk + interval_risk + disagreement_risk + history_penalty,
        0,
        100,
    )
    risk_level = "low" if risk_score <= 33 else "medium" if risk_score <= 66 else "high"
    reasons = []
    if conservative_roi < 0:
        reasons.append("downside_loss")
    if sale["within_30d"] < 0.50:
        reasons.append("low_sale_probability")
    if uncertainty_percent > 35:
        reasons.append("wide_prediction_interval")
    if disagreement_percent > 20:
        reasons.append("model_disagreement")
    if sale["source"] == "heuristic":
        reasons.append("limited_outcome_history")

    profit_basis = conservative_profit * 0.65 + expected_profit * 0.35
    ranking_score = (
        profit_basis
        * sale["within_30d"]
        / math.sqrt(max(sale["estimated_days"], 0.5))
        * (1 - risk_score / 125)
    )
    model_names = sorted(row["model_name"] for row in rows)
    return {
        "intelligence_schema_version": ANNOTATION_SCHEMA_VERSION,
        "computed_at": _iso(_parse_datetime(now) or _utc_now()),
        "listing_id": str(payload.get("listing_id") or rows[0].get("listing_id") or ""),
        "skin_name": segment["skin_name"],
        "base_skin_name": segment["base_skin_name"],
        "collection_name": segment["collection_name"],
        "csfloat_url": rows[0].get("csfloat_url"),
        "segment": {
            "key": segment["key"],
            "family": segment["family"],
            "variant": segment["variant"],
            "price_band": segment["price_band"],
            "sticker_band": segment["sticker_band"],
        },
        "buy_price_cents": buy_price,
        "ensemble_prediction": {
            "predicted_price_cents": point,
            "low_price_cents": low,
            "high_price_cents": high,
            "fee_percent": fee_percent,
            "expected_profit_cents": expected_profit,
            "expected_roi_percent": expected_roi,
            "conservative_profit_cents": conservative_profit,
            "conservative_roi_percent": conservative_roi,
            "model_count": len(rows),
            "model_names": model_names,
            "weights": weights,
            "weight_source": weight_info["source"],
            "weight_sample_count": weight_info["sample_count"],
            "model_errors": weight_info["model_errors"],
        },
        "sale_estimate": sale,
        "risk": {
            "score": risk_score,
            "level": risk_level,
            "reasons": reasons,
            "uncertainty_percent": uncertainty_percent,
            "model_disagreement_percent": disagreement_percent,
        },
        "ranking_score": ranking_score,
    }


def refresh_listing_intelligence(conn, listing_id, *, profile=None, now=None):
    """Recompute one current annotation from persisted model opinions."""

    listing_id = str(listing_id or "").strip()
    if not listing_id:
        raise ValueError("listing_id is required")
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM model_deal_predictions
            WHERE listing_id = ? AND verdict IN ('good', 'watch')
                AND model_name != 'vega'
            ORDER BY model_name
            """,
            (listing_id,),
        )
    ]
    if not rows:
        conn.execute(
            "DELETE FROM model_deal_intelligence WHERE listing_id = ?", (listing_id,)
        )
        return None
    payload = {}
    for row in rows:
        candidate = _json_object(row.get("deal_payload_json"))
        if candidate:
            payload = candidate
            break
    payload.setdefault("listing_id", listing_id)
    annotation = score_prediction_group(rows, payload, profile, now=now)
    ensemble = annotation["ensemble_prediction"]
    sale = annotation["sale_estimate"]
    risk = annotation["risk"]
    segment = annotation["segment"]
    conn.execute(
        """
        INSERT INTO model_deal_intelligence (
            listing_id, computed_at, segment_key, segment_family, skin_name,
            collection_name, csfloat_url, buy_price_cents, model_count,
            model_names_json, model_weights_json, weight_source,
            weight_sample_count, ensemble_predicted_price_cents,
            ensemble_low_price_cents, ensemble_high_price_cents,
            expected_profit_cents, expected_roi_percent,
            conservative_profit_cents, conservative_roi_percent,
            sell_probability_7d, sell_probability_30d,
            estimated_days_to_sell, sell_estimate_source,
            sell_estimate_sample_count, uncertainty_percent,
            model_disagreement_percent, risk_score, risk_level,
            ranking_score, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(listing_id) DO UPDATE SET
            computed_at = excluded.computed_at,
            segment_key = excluded.segment_key,
            segment_family = excluded.segment_family,
            skin_name = excluded.skin_name,
            collection_name = excluded.collection_name,
            csfloat_url = excluded.csfloat_url,
            buy_price_cents = excluded.buy_price_cents,
            model_count = excluded.model_count,
            model_names_json = excluded.model_names_json,
            model_weights_json = excluded.model_weights_json,
            weight_source = excluded.weight_source,
            weight_sample_count = excluded.weight_sample_count,
            ensemble_predicted_price_cents = excluded.ensemble_predicted_price_cents,
            ensemble_low_price_cents = excluded.ensemble_low_price_cents,
            ensemble_high_price_cents = excluded.ensemble_high_price_cents,
            expected_profit_cents = excluded.expected_profit_cents,
            expected_roi_percent = excluded.expected_roi_percent,
            conservative_profit_cents = excluded.conservative_profit_cents,
            conservative_roi_percent = excluded.conservative_roi_percent,
            sell_probability_7d = excluded.sell_probability_7d,
            sell_probability_30d = excluded.sell_probability_30d,
            estimated_days_to_sell = excluded.estimated_days_to_sell,
            sell_estimate_source = excluded.sell_estimate_source,
            sell_estimate_sample_count = excluded.sell_estimate_sample_count,
            uncertainty_percent = excluded.uncertainty_percent,
            model_disagreement_percent = excluded.model_disagreement_percent,
            risk_score = excluded.risk_score,
            risk_level = excluded.risk_level,
            ranking_score = excluded.ranking_score,
            details_json = excluded.details_json
        """,
        (
            listing_id,
            annotation["computed_at"],
            segment["key"],
            segment["family"],
            annotation["skin_name"],
            annotation["collection_name"],
            annotation["csfloat_url"],
            annotation["buy_price_cents"],
            ensemble["model_count"],
            _json_dumps(ensemble["model_names"]),
            _json_dumps(ensemble["weights"]),
            ensemble["weight_source"],
            ensemble["weight_sample_count"],
            ensemble["predicted_price_cents"],
            ensemble["low_price_cents"],
            ensemble["high_price_cents"],
            ensemble["expected_profit_cents"],
            ensemble["expected_roi_percent"],
            ensemble["conservative_profit_cents"],
            ensemble["conservative_roi_percent"],
            sale["within_7d"],
            sale["within_30d"],
            sale["estimated_days"],
            sale["source"],
            sale["sample_count"],
            risk["uncertainty_percent"],
            risk["model_disagreement_percent"],
            risk["score"],
            risk["level"],
            annotation["ranking_score"],
            _json_dumps(annotation),
        ),
    )
    return annotation


def prune_stale_intelligence(conn):
    """Remove annotations that no longer have an actionable model opinion."""

    if not _table_exists(conn, "model_deal_intelligence"):
        return 0
    cursor = conn.execute(
        """
        DELETE FROM model_deal_intelligence
        WHERE NOT EXISTS (
            SELECT 1 FROM model_deal_predictions AS prediction
            WHERE prediction.listing_id = model_deal_intelligence.listing_id
                AND prediction.verdict IN ('good', 'watch')
        )
        """
    )
    return max(int(cursor.rowcount), 0)


def get_risk_annotations(conn, listing_ids, *, limit=MAX_ANNOTATION_ROWS):
    """Bounded read contract: ``{listing_id: annotation}`` for requested IDs."""

    maximum = _bounded_int(limit, minimum=1, maximum=MAX_ANNOTATION_ROWS, name="limit")
    ids = list(dict.fromkeys(str(value) for value in (listing_ids or []) if str(value)))
    if len(ids) > maximum:
        raise ValueError(f"listing_ids exceeds limit={maximum}")
    if not ids or not _table_exists(conn, "model_deal_intelligence"):
        return {}
    result = {}
    for offset in range(0, len(ids), 400):
        batch = ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"""
            SELECT listing_id, details_json
            FROM model_deal_intelligence
            WHERE listing_id IN ({placeholders})
            """,
            batch,
        ):
            details = _json_object(row[1])
            if details:
                result[str(row[0])] = details
    return result


def attach_risk_annotations(deals, annotations):
    """Pure merge helper; original deal dictionaries are never modified."""

    mapping = annotations if isinstance(annotations, dict) else {}
    result = []
    for raw in deals or []:
        if not isinstance(raw, dict):
            continue
        deal = dict(raw)
        listing_id = str(deal.get("listing_id") or "")
        deal["intelligence"] = mapping.get(listing_id)
        result.append(deal)
    return result


def list_portfolio_candidates(
    conn,
    *,
    max_candidates=MAX_PORTFOLIO_CANDIDATES,
    max_age_hours=24,
):
    """Return a bounded, current list; no outcome aggregation occurs here."""

    limit = _bounded_int(
        max_candidates,
        minimum=1,
        maximum=MAX_PORTFOLIO_CANDIDATES,
        name="max_candidates",
    )
    hours = _bounded_int(max_age_hours, minimum=1, maximum=720, name="max_age_hours")
    if not _table_exists(conn, "model_deal_intelligence"):
        return []
    cutoff = (_utc_now() - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        """
        SELECT details_json FROM model_deal_intelligence
        WHERE computed_at >= ? AND expected_profit_cents > 0
        ORDER BY ranking_score DESC, listing_id
        LIMIT ?
        """,
        (cutoff, limit),
    )
    return [details for row in rows if (details := _json_object(row[0]))]


def build_portfolio_from_candidates(
    candidates,
    *,
    budget_cents,
    risk="balanced",
    max_items=10,
    max_per_skin=1,
    max_per_collection=3,
):
    """Pure, deterministic constrained greedy portfolio builder."""

    budget = _bounded_int(
        budget_cents, minimum=1, maximum=2_000_000_000, name="budget_cents"
    )
    item_limit = _bounded_int(max_items, minimum=1, maximum=50, name="max_items")
    skin_limit = _bounded_int(max_per_skin, minimum=1, maximum=50, name="max_per_skin")
    collection_limit = _bounded_int(
        max_per_collection,
        minimum=1,
        maximum=50,
        name="max_per_collection",
    )
    risk_name = str(risk or "").strip().lower()
    if risk_name not in RISK_PROFILES:
        raise ValueError("risk must be conservative, balanced, or aggressive")
    policy = RISK_PROFILES[risk_name]

    normalized = []
    seen_ids = set()
    rejected = Counter()
    for raw in candidates or []:
        if not isinstance(raw, dict):
            rejected["invalid"] += 1
            continue
        listing_id = str(raw.get("listing_id") or "").strip()
        if not listing_id or listing_id in seen_ids:
            rejected["duplicate_or_missing_id"] += 1
            continue
        seen_ids.add(listing_id)
        ensemble = raw.get("ensemble_prediction") or {}
        risk_info = raw.get("risk") or {}
        price = _int(raw.get("buy_price_cents"))
        expected_profit = _int(ensemble.get("expected_profit_cents"))
        conservative_profit = _int(ensemble.get("conservative_profit_cents"))
        conservative_roi = _finite(ensemble.get("conservative_roi_percent"))
        risk_score = _finite(risk_info.get("score"))
        if None in (price, expected_profit, conservative_profit, conservative_roi, risk_score):
            rejected["invalid"] += 1
            continue
        if price <= 0 or expected_profit <= 0:
            rejected["not_profitable"] += 1
            continue
        if risk_score > policy["max_risk_score"]:
            rejected["risk"] += 1
            continue
        if conservative_roi < policy["min_conservative_roi_percent"]:
            rejected["downside"] += 1
            continue
        item = dict(raw)
        item["_price"] = price
        item["_expected_profit"] = expected_profit
        item["_conservative_profit"] = conservative_profit
        item["_risk_score"] = risk_score
        item["_utility"] = _finite(raw.get("ranking_score"), 0) / price
        normalized.append(item)

    normalized.sort(
        key=lambda row: (
            row["_utility"],
            row["_conservative_profit"] / row["_price"],
            -row["_risk_score"],
            row["listing_id"],
        ),
        reverse=True,
    )
    selected = []
    spent = 0
    skin_counts = Counter()
    collection_counts = Counter()
    for item in normalized:
        if len(selected) >= item_limit:
            rejected["max_items"] += 1
            continue
        if spent + item["_price"] > budget:
            rejected["budget"] += 1
            continue
        skin_key = str(
            item.get("base_skin_name") or item.get("skin_name") or item["listing_id"]
        )
        collection_key = str(item.get("collection_name") or item["listing_id"])
        if skin_counts[skin_key] >= skin_limit:
            rejected["skin_diversification"] += 1
            continue
        if collection_counts[collection_key] >= collection_limit:
            rejected["collection_diversification"] += 1
            continue
        clean = {key: value for key, value in item.items() if not key.startswith("_")}
        selected.append(clean)
        spent += item["_price"]
        skin_counts[skin_key] += 1
        collection_counts[collection_key] += 1

    total_expected = sum(
        item["ensemble_prediction"]["expected_profit_cents"] for item in selected
    )
    total_conservative = sum(
        item["ensemble_prediction"]["conservative_profit_cents"] for item in selected
    )
    weighted_risk = (
        sum(item["risk"]["score"] * item["buy_price_cents"] for item in selected) / spent
        if spent
        else 0.0
    )
    return {
        "portfolio_schema_version": 1,
        "risk_profile": risk_name,
        "budget_cents": budget,
        "spent_cents": spent,
        "cash_remaining_cents": budget - spent,
        "item_count": len(selected),
        "total_expected_profit_cents": total_expected,
        "total_conservative_profit_cents": total_conservative,
        "weighted_risk_score": weighted_risk,
        "items": selected,
        "constraints": {
            "max_items": item_limit,
            "max_per_skin": skin_limit,
            "max_per_collection": collection_limit,
            "max_risk_score": policy["max_risk_score"],
            "min_conservative_roi_percent": policy["min_conservative_roi_percent"],
        },
        "rejected_counts": dict(sorted(rejected.items())),
    }


def build_portfolio(
    conn,
    *,
    budget_cents,
    risk="balanced",
    max_items=10,
    max_per_skin=1,
    max_per_collection=3,
    max_candidates=MAX_PORTFOLIO_CANDIDATES,
    max_age_hours=24,
):
    """Stable DB wrapper for the pure portfolio builder."""

    candidates = list_portfolio_candidates(
        conn,
        max_candidates=max_candidates,
        max_age_hours=max_age_hours,
    )
    return build_portfolio_from_candidates(
        candidates,
        budget_cents=budget_cents,
        risk=risk,
        max_items=max_items,
        max_per_skin=max_per_skin,
        max_per_collection=max_per_collection,
    )
