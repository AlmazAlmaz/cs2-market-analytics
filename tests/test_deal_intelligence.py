import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import deal_intelligence
import deal_scanner


def memory_db(*, scanner_schema=False):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if scanner_schema:
        deal_scanner.init_db(conn)
    else:
        deal_intelligence.init_schema(conn)
        conn.commit()
    return conn


def recommendation_inputs(listing_id="listing-1", model_name="almaz"):
    payload = {
        "listing_id": listing_id,
        "name": "StatTrak™ AK-47 | Test (Field-Tested)",
        "buy_price_cents": 10_000,
        "item_collection_name": "Test Collection",
        "item_is_stattrak": True,
        "stickers": [{"name": "Sticker A"}, {"name": "Sticker B"}],
    }
    prediction = {
        "model_name": model_name,
        "artifact_version": "v1",
        "trained_at": "2026-08-01T00:00:00+00:00",
        "name": payload["name"],
        "base_skin_name": "AK-47 | Test",
        "buy_price_cents": 10_000,
        "predicted_price_cents": 13_000,
        "low_price_cents": 11_500,
        "high_price_cents": 14_500,
        "fee_percent": 4,
        "net_expected_cents": 12_480,
        "profit_cents": 2_480,
        "roi_percent": 24.8,
        "liquidity_score": 70,
        "sales_per_day": 4,
        "sticker_count": 2,
        "verdict": "good",
    }
    return {"listing_id": listing_id, "deal_payload": payload}, prediction


def test_schema_is_idempotent_and_keeps_existing_component():
    conn = memory_db()
    conn.execute(
        "INSERT INTO app_schema_versions VALUES ('existing', 7, '2026-01-01')"
    )

    deal_intelligence.init_schema(conn)
    deal_intelligence.init_schema(conn)

    versions = dict(
        conn.execute("SELECT component, version FROM app_schema_versions ORDER BY component")
    )
    assert versions == {"deal_intelligence": 2, "existing": 7}
    assert conn.execute(
        "SELECT COUNT(*) FROM model_deal_recommendations"
    ).fetchone()[0] == 0


def test_recommendation_snapshot_is_idempotent_and_reconciles_confirmed_sale():
    conn = memory_db()
    conn.execute(
        """
        CREATE TABLE sales (
            listing_id TEXT PRIMARY KEY, state TEXT, price_cents INTEGER, sold_at TEXT
        )
        """
    )
    listing_data, prediction = recommendation_inputs()
    recommended_at = datetime(2026, 8, 1, tzinfo=timezone.utc)

    first = deal_intelligence.record_recommendation(
        conn, listing_data, prediction, "almaz", observed_at=recommended_at
    )
    changed = dict(prediction, predicted_price_cents=99_999)
    second = deal_intelligence.record_recommendation(
        conn,
        listing_data,
        changed,
        "almaz",
        observed_at=recommended_at + timedelta(hours=1),
    )
    conn.execute(
        "INSERT INTO sales VALUES (?, 'sold', ?, ?)",
        (
            "listing-1",
            12_000,
            (recommended_at + timedelta(days=3)).isoformat(),
        ),
    )

    result = deal_intelligence.reconcile_outcomes(
        conn, now=recommended_at + timedelta(days=4)
    )
    row = conn.execute("SELECT * FROM model_deal_recommendations").fetchone()

    assert first["created"] is True
    assert second == {"recommendation_id": first["recommendation_id"], "created": False}
    assert row["observation_count"] == 2
    assert row["predicted_price_cents"] == 13_000
    assert row["outcome_state"] == "sold"
    assert row["days_to_sell"] == pytest.approx(3)
    assert row["realized_profit_cents"] == 1_520
    assert row["realized_roi_percent"] == pytest.approx(15.2)
    assert result["sold"] == 1
    summary = deal_intelligence.get_outcome_summary(conn)
    assert summary["sold_recommendations"] == 1
    assert summary["models"]["almaz"]["realized_profit_cents"] == 1_520


def test_sale_before_recommendation_is_not_attributed_and_horizon_closes_it():
    conn = memory_db()
    conn.execute(
        """
        CREATE TABLE sales (
            listing_id TEXT PRIMARY KEY, state TEXT, price_cents INTEGER, sold_at TEXT
        )
        """
    )
    listing_data, prediction = recommendation_inputs()
    recommended_at = datetime(2026, 8, 10, tzinfo=timezone.utc)
    deal_intelligence.record_recommendation(
        conn,
        listing_data,
        prediction,
        "almaz",
        observed_at=recommended_at,
        evaluation_horizon_days=7,
    )
    conn.execute(
        "INSERT INTO sales VALUES (?, 'sold', ?, ?)",
        ("listing-1", 12_000, (recommended_at - timedelta(days=1)).isoformat()),
    )

    result = deal_intelligence.reconcile_outcomes(
        conn, now=recommended_at + timedelta(days=8)
    )

    row = conn.execute(
        "SELECT outcome_state, sale_price_cents FROM model_deal_recommendations"
    ).fetchone()
    assert tuple(row) == ("not_sold", None)
    assert result["sold"] == 0
    assert result["not_sold"] == 1


def test_new_model_build_opens_a_separate_auditable_recommendation():
    conn = memory_db()
    listing_data, prediction = recommendation_inputs()
    observed_at = datetime(2026, 8, 10, tzinfo=timezone.utc)
    first = deal_intelligence.record_recommendation(
        conn, listing_data, prediction, "almaz", observed_at=observed_at
    )
    new_build = dict(prediction, trained_at="2026-08-11T00:00:00+00:00")
    second = deal_intelligence.record_recommendation(
        conn,
        listing_data,
        new_build,
        "almaz",
        observed_at=observed_at + timedelta(days=1),
    )

    assert first["recommendation_id"] != second["recommendation_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM model_deal_recommendations WHERE outcome_state = 'open'"
    ).fetchone()[0] == 2


def test_segment_weights_require_history_and_favour_lower_error_model():
    listing_data, prediction = recommendation_inputs()
    segment = deal_intelligence.segment_for(listing_data["deal_payload"], prediction)
    predictions = [prediction, dict(prediction, model_name="igor")]
    events = []
    for index in range(6):
        actual = 10_000 + index * 100
        for model_name, predicted in (("almaz", actual * 1.02), ("igor", actual * 1.25)):
            events.append(
                {
                    "id": f"{index}-{model_name}",
                    "listing_id": f"listing-{index}",
                    "model_name": model_name,
                    "segment_key": segment["key"],
                    "segment_family": segment["family"],
                    "outcome_state": "sold",
                    "predicted_price_cents": predicted,
                    "sale_price_cents": actual,
                    "days_to_sell": 2,
                }
            )

    weighted = deal_intelligence.dynamic_ensemble_weights(
        predictions,
        segment,
        {"events": events},
        min_model_samples=5,
    )
    fallback = deal_intelligence.dynamic_ensemble_weights(
        predictions,
        segment,
        {"events": events[:4]},
        min_model_samples=5,
    )

    assert weighted["source"] == "segment"
    assert weighted["sample_count"] == 6
    assert weighted["weights"]["almaz"] > weighted["weights"]["igor"]
    assert sum(weighted["weights"].values()) == pytest.approx(1)
    assert fallback["source"] == "equal"
    assert fallback["weights"] == pytest.approx({"almaz": 0.5, "igor": 0.5})


def test_empirical_sale_estimate_deduplicates_model_rows():
    listing_data, prediction = recommendation_inputs()
    segment = deal_intelligence.segment_for(listing_data["deal_payload"], prediction)
    events = []
    for index in range(5):
        state = "sold" if index < 4 else "not_sold"
        for model_name in ("almaz", "igor", "nova"):
            events.append(
                {
                    "id": f"{index}-{model_name}",
                    "listing_id": f"listing-{index}",
                    "model_name": model_name,
                    "segment_key": segment["key"],
                    "segment_family": segment["family"],
                    "outcome_state": state,
                    "days_to_sell": 3 if state == "sold" else None,
                }
            )

    estimate = deal_intelligence.estimate_sale_probability(
        prediction,
        segment,
        {"events": events},
        min_segment_outcomes=5,
    )

    assert estimate["source"] == "segment"
    assert estimate["sample_count"] == 5
    assert estimate["within_30d"] == pytest.approx(6 / 9)
    assert estimate["estimated_days"] == 3


def test_scanner_persistence_creates_outcome_and_current_risk_annotation():
    conn = memory_db(scanner_schema=True)
    listing_data, prediction = recommendation_inputs()

    assert deal_scanner.persist_model_result(
        conn,
        listing_data,
        prediction,
        {"sticker_id": 1, "name": "Sticker A"},
        {},
        "almaz",
        True,
    )
    annotation = deal_intelligence.refresh_listing_intelligence(conn, "listing-1")

    assert conn.execute(
        "SELECT COUNT(*) FROM model_deal_recommendations"
    ).fetchone()[0] == 1
    assert annotation["listing_id"] == "listing-1"
    assert annotation["ensemble_prediction"]["model_names"] == ["almaz"]
    assert 0 <= annotation["risk"]["score"] <= 100
    assert annotation["sale_estimate"]["source"] == "heuristic"
    assert deal_intelligence.get_risk_annotations(conn, ["listing-1"])[
        "listing-1"
    ]["ranking_score"] == pytest.approx(annotation["ranking_score"])


def test_save_all_audit_row_is_not_mislabeled_as_a_recommendation():
    conn = memory_db(scanner_schema=True)
    listing_data, prediction = recommendation_inputs()
    prediction = dict(prediction, verdict="thin")

    deal_scanner.persist_model_result(
        conn,
        listing_data,
        prediction,
        {"sticker_id": 1, "name": "Sticker A"},
        {},
        "igor",
        True,
    )

    assert conn.execute(
        "SELECT COUNT(*) FROM model_deal_recommendations"
    ).fetchone()[0] == 0


def test_scan_refreshes_intelligence_in_the_same_transaction(monkeypatch):
    conn = memory_db(scanner_schema=True)
    listing_data, prediction = recommendation_inputs()
    monkeypatch.setattr(
        deal_scanner.item_classification,
        "classify_market_hash_name",
        lambda *_args, **_kwargs: (
            deal_scanner.item_classification.ITEM_KIND_WEAPON_SKIN
        ),
    )
    monkeypatch.setattr(
        deal_scanner.csfloat_listing_fetcher,
        "fetch_active_listings_for_sticker",
        lambda *_args, **_kwargs: (
            [{"id": "listing-1"}],
            None,
            {"key_file": "test", "rate_remaining": "10"},
            "https://example.invalid",
        ),
    )
    monkeypatch.setattr(
        deal_scanner,
        "normalize_listing_to_deal_payload",
        lambda *_args: listing_data,
    )
    monkeypatch.setitem(
        deal_scanner.MODEL_RUNNERS,
        "almaz",
        lambda *_args: prediction,
    )
    config = deal_scanner.validate_config(
        {**deal_scanner.DEFAULT_CONFIG, "enabled_models": ["almaz"]}
    )

    returned, saved, _meta, model_saved = deal_scanner.scan_sticker(
        conn,
        {"sticker_id": 1, "name": "Sticker A"},
        config,
    )

    assert (returned, saved, model_saved) == (1, 1, {"almaz": 1})
    assert conn.execute(
        "SELECT COUNT(*) FROM model_deal_recommendations"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM model_deal_intelligence"
    ).fetchone()[0] == 1


def portfolio_candidate(
    listing_id,
    *,
    price=1_000,
    expected=300,
    conservative=150,
    conservative_roi=15,
    risk=20,
    skin="AK-47 | Test",
    collection="Collection A",
    score=100,
):
    return {
        "listing_id": listing_id,
        "skin_name": skin,
        "base_skin_name": skin,
        "collection_name": collection,
        "buy_price_cents": price,
        "ensemble_prediction": {
            "expected_profit_cents": expected,
            "conservative_profit_cents": conservative,
            "conservative_roi_percent": conservative_roi,
        },
        "sale_estimate": {"within_30d": 0.8},
        "risk": {"score": risk, "level": "low", "reasons": []},
        "ranking_score": score,
    }


def test_portfolio_enforces_budget_risk_and_diversification():
    candidates = [
        portfolio_candidate("one", score=300),
        portfolio_candidate("same-skin", score=250),
        portfolio_candidate(
            "other",
            price=1_500,
            skin="M4A1-S | Other",
            collection="Collection B",
            score=200,
        ),
        portfolio_candidate(
            "risky",
            risk=70,
            skin="AWP | Risky",
            collection="Collection C",
            score=1_000,
        ),
    ]

    portfolio = deal_intelligence.build_portfolio_from_candidates(
        candidates,
        budget_cents=2_500,
        risk="balanced",
        max_items=3,
        max_per_skin=1,
        max_per_collection=2,
    )

    assert [item["listing_id"] for item in portfolio["items"]] == ["one", "other"]
    assert portfolio["spent_cents"] == 2_500
    assert portfolio["cash_remaining_cents"] == 0
    assert portfolio["rejected_counts"]["risk"] == 1
    assert portfolio["rejected_counts"]["skin_diversification"] == 1


def test_annotation_read_rejects_unbounded_id_list():
    conn = memory_db()
    with pytest.raises(ValueError, match="exceeds limit"):
        deal_intelligence.get_risk_annotations(conn, ["a", "b"], limit=1)


def test_annotation_merge_does_not_mutate_existing_deals():
    deals = [{"listing_id": "one", "verdict": "good"}]
    annotations = {"one": {"risk": {"score": 12}}}

    merged = deal_intelligence.attach_risk_annotations(deals, annotations)

    assert "intelligence" not in deals[0]
    assert merged[0]["intelligence"]["risk"]["score"] == 12
