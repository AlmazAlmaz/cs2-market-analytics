"""Best Deals contracts that keep filtering work independent of heavy model JSON."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import dashboard_server
import deal_scanner


@pytest.fixture
def comparison_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    monkeypatch.setattr(dashboard_server, "add_deal_visual_fallbacks", lambda *_: None)
    yield conn
    conn.close()


def add_opinion(
    conn,
    listing_id,
    model="almaz",
    *,
    verdict="good",
    age_minutes=0,
    name="AK-47 | Redline (Field-Tested)",
    price=1000,
    payload=None,
    prediction=None,
):
    observed = (
        datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    ).isoformat()
    if payload is None:
        payload = {"name": name, "listing_type": "buy_now", "marker": listing_id}
    if prediction is None:
        prediction = {"stickers": [{"name": f"Sticker {listing_id} {model}"}]}
    raw_payload = payload if isinstance(payload, str) else json.dumps(payload)
    raw_prediction = prediction if isinstance(prediction, str) else json.dumps(prediction)
    positive = verdict in {"good", "watch"}
    conn.execute(
        """
        INSERT INTO model_deal_predictions (
            listing_id, model_name, first_seen_at, last_seen_at, scan_count,
            skin_name, buy_price_cents, predicted_price_cents, profit_cents,
            roi_percent, verdict, deal_payload_json, prediction_json
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            listing_id, model, observed, observed, name, price,
            1200 if positive else 800, 100 if positive else -200,
            10 if positive else -20, verdict, raw_payload, raw_prediction,
        ),
    )
    return raw_payload, raw_prediction


def fetch(conn, *, offset=0, limit=10, search="", category="all", **filters):
    return dashboard_server.fetch_model_deal_comparison_page(
        conn, offset, limit, search, category, opinion_stale_hours=24, **filters
    )


def record_json_reads(monkeypatch):
    reads = []
    original_loads = json.loads

    def loads(value, *args, **kwargs):
        reads.append(value)
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(dashboard_server.json, "loads", loads)
    return reads


def test_newer_dissent_supplies_payload_and_stickers_independently_of_best_model(
    comparison_db,
):
    add_opinion(comparison_db, "mixed", age_minutes=2)
    newer_payload = {
        "name": "Payload from dissent",
        "listing_type": "auction",
        "float_value": 0.123,
        "stickers": [{"name": "Fallback payload sticker"}],
    }
    expected_stickers = [{"name": "Prediction sticker from dissent", "slot": 2}]
    add_opinion(
        comparison_db, "mixed", "igor", verdict="bad", age_minutes=1,
        payload=newer_payload, prediction={"stickers": expected_stickers},
    )

    row = fetch(comparison_db)["rows"][0]

    assert row["best"]["model_name"] == "almaz"
    assert row["payload"] == newer_payload
    assert row["stickers"] == expected_stickers
    assert row["listing_type"] == "auction"
    assert row["float_value"] == 0.123
    assert row["confidence"] == "disagreement"
    assert row["conservative_roi_percent"] == -20
    assert set(row["models"]) == {"almaz", "igor"}
    assert all("prediction_json" not in model for model in row["models"].values())
    assert all("deal_payload_json" not in model for model in row["models"].values())


@pytest.mark.parametrize("empty_payload", ["", "{}", "{malformed"])
def test_empty_or_malformed_newest_payload_uses_next_payload_stickers(
    comparison_db, empty_payload,
):
    expected_stickers = [{"name": "Sticker from first valid payload"}]
    add_opinion(
        comparison_db, "fallback", "igor", verdict="bad",
        payload=empty_payload, prediction={"stickers": [{"name": "Wrong newest"}]},
    )
    add_opinion(
        comparison_db, "fallback", "almaz", age_minutes=1,
        payload={"listing_type": "buy_now", "stickers": expected_stickers},
        prediction={"stickers": []},
    )
    add_opinion(
        comparison_db, "fallback", "nova", age_minutes=2,
        payload={"listing_type": "auction"},
        prediction={"stickers": [{"name": "Wrong oldest"}]},
    )

    row = fetch(comparison_db)["rows"][0]

    assert row["payload"]["listing_type"] == "buy_now"
    assert row["stickers"] == expected_stickers
    assert row["confidence"] == "disagreement"


def test_category_confidence_and_summary_are_computed_before_pagination(comparison_db):
    add_opinion(comparison_db, "single")
    add_opinion(comparison_db, "dissent", age_minutes=10)
    add_opinion(comparison_db, "dissent", "igor", verdict="bad", age_minutes=10)
    for model in ("almaz", "igor"):
        add_opinion(comparison_db, "consensus", model, age_minutes=20)
    for model in ("almaz", "igor", "nova"):
        add_opinion(comparison_db, "unanimous", model, age_minutes=30)

    unfiltered = fetch(comparison_db, limit=1)
    filtered = fetch(comparison_db, limit=1, category="both", confidence="consensus")
    second_page = fetch(
        comparison_db, offset=1, limit=1, category="both", confidence="consensus",
    )

    assert [row["listing_id"] for row in unfiltered["rows"]] == ["single"]
    assert filtered["total"] == second_page["total"] == 2
    assert [row["listing_id"] for row in filtered["rows"]] == ["consensus"]
    assert [row["listing_id"] for row in second_page["rows"]] == ["unanimous"]
    assert unfiltered["summary"] == filtered["summary"] == second_page["summary"]
    assert filtered["summary"]["all"] == 4
    assert filtered["summary"]["both"] == 2
    assert filtered["summary"]["disagreement"] == 1
    assert filtered["summary"]["single_signal"] == 1
    assert filtered["summary"]["consensus"] == 1
    assert filtered["summary"]["unanimous_three"] == 1


def test_non_candidates_never_decode_payloads_and_dissent_remains_visible(
    comparison_db, monkeypatch,
):
    rejected_json = set()
    for index in range(8):
        for prefix, model, verdict, age in (
            ("negative", "almaz", "bad", 0),
            ("thin", "igor", "thin", 0),
            ("expired", "almaz", "expired", 0),
            ("advisory", "vega", "good", 0),
            ("stale", "almaz", "good", 25 * 60),
        ):
            rejected_json.update(add_opinion(
                comparison_db, f"{prefix}-{index}", model,
                verdict=verdict, age_minutes=age,
            ))
    best_payload, best_prediction = add_opinion(
        comparison_db, "eligible", age_minutes=2,
        payload={"marker": "older-best", "listing_type": "buy_now"},
    )
    dissent_payload, dissent_prediction = add_opinion(
        comparison_db, "eligible", "igor", verdict="bad", age_minutes=1,
        payload={"marker": "newer-dissent", "listing_type": "buy_now"},
    )
    reads = record_json_reads(monkeypatch)

    result = fetch(comparison_db)

    assert result["total"] == 1
    assert result["rows"][0]["dissenting_models"] == ["igor"]
    assert result["rows"][0]["conservative_roi_percent"] == -20
    assert not rejected_json.intersection(reads)
    assert reads.count(dissent_payload) == 1
    assert reads.count(dissent_prediction) == 1
    assert best_payload not in reads
    assert best_prediction not in reads


def test_prediction_json_is_decoded_only_for_selected_page(comparison_db, monkeypatch):
    prediction_by_listing = {}
    all_prediction_json = set()
    for index in range(6):
        listing_id = f"listing-{index}"
        _, best_prediction = add_opinion(
            comparison_db, listing_id, age_minutes=index * 10 + 2,
        )
        _, dissent_prediction = add_opinion(
            comparison_db, listing_id, "igor", verdict="bad",
            age_minutes=index * 10 + 1,
        )
        prediction_by_listing[listing_id] = dissent_prediction
        all_prediction_json.update((best_prediction, dissent_prediction))
    reads = record_json_reads(monkeypatch)

    result = fetch(comparison_db, offset=2, limit=2)

    assert result["total"] == 6
    assert [row["listing_id"] for row in result["rows"]] == ["listing-2", "listing-3"]
    decoded_predictions = [value for value in reads if value in all_prediction_json]
    assert len(decoded_predictions) == 2
    assert set(decoded_predictions) == {
        prediction_by_listing["listing-2"], prediction_by_listing["listing-3"],
    }
    for row in result["rows"]:
        assert row["stickers"] == [{"name": f"Sticker {row['listing_id']} igor"}]


def test_catalog_item_kind_lookup_is_batched_for_distinct_market_names(comparison_db):
    comparison_db.execute(
        "CREATE TABLE cs2_item_catalog (market_hash_name TEXT PRIMARY KEY, item_kind TEXT)"
    )
    for index in range(12):
        name = f"New catalog item {index}"
        comparison_db.execute("INSERT INTO cs2_item_catalog VALUES (?, 'knife')", (name,))
        add_opinion(comparison_db, f"catalog-{index}", name=name, age_minutes=index)
    statements = []
    comparison_db.set_trace_callback(statements.append)
    try:
        result = fetch(comparison_db, limit=5, asset_type="knife")
    finally:
        comparison_db.set_trace_callback(None)

    assert result["total"] == result["summary"]["all"] == 12
    assert len(result["rows"]) == 5
    assert all(row["item_kind"] == "knife" for row in result["rows"])
    catalog_selects = [
        statement for statement in statements
        if "from cs2_item_catalog" in " ".join(statement.lower().split())
    ]
    assert len(catalog_selects) == 1


@pytest.mark.parametrize("filters", [{"search": "matching"}, {"max_price": "10"}])
def test_candidate_preselection_preserves_opinion_search_and_price_filters(
    comparison_db, filters,
):
    add_opinion(comparison_db, "eligible", name="matching skin", price=1000)
    add_opinion(
        comparison_db, "eligible", "igor", verdict="bad", name="other skin", price=2000,
    )
    add_opinion(comparison_db, "positive-filtered-out", name="other skin", price=2000)
    add_opinion(
        comparison_db, "positive-filtered-out", "igor", verdict="bad",
        name="matching skin", price=1000,
    )

    result = fetch(comparison_db, **filters)

    assert result["total"] == 1
    assert result["rows"][0]["listing_id"] == "eligible"
    assert set(result["rows"][0]["models"]) == {"almaz"}
    assert result["rows"][0]["confidence"] == "single_signal"
