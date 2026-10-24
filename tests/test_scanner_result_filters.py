import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

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


def add_opinion(conn, listing, model="almaz", *, verdict="good", name="AK-47 | Redline (Field-Tested)", source="sticker", age=0):
    observed = (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat()
    conn.execute(
        """INSERT INTO model_deal_predictions (
            listing_id, model_name, first_seen_at, last_seen_at, scan_count,
            skin_name, buy_price_cents, predicted_price_cents, profit_cents,
            roi_percent, verdict, source_type, deal_payload_json, prediction_json
        ) VALUES (?, ?, ?, ?, 1, ?, 1000, 1200, 100, 10, ?, ?, ?, '{}')""",
        (listing, model, observed, observed, name, verdict, source,
         json.dumps({"name": name, "listing_type": "buy_now"})),
    )


def fetch(conn, **filters):
    return dashboard_server.fetch_model_deal_comparison_page(
        conn, 0, 10, "", filters.pop("category", "all"), **filters
    )


def test_vega_filter_requires_fresh_positive_advisory_and_preserves_core_comparison(comparison_db):
    conn = comparison_db
    for listing in ("positive", "negative", "stale", "missing"):
        add_opinion(conn, listing)
        add_opinion(conn, listing, "igor", verdict="bad")
    add_opinion(conn, "positive", "vega", verdict="watch")
    add_opinion(conn, "negative", "vega", verdict="bad")
    add_opinion(conn, "stale", "vega", age=48)
    add_opinion(conn, "advisory-only", "vega")

    result = fetch(conn, category="vega", opinion_stale_hours=24)

    assert result["total"] == 1
    row = result["rows"][0]
    assert row["listing_id"] == "positive"
    assert row["best"]["model_name"] == "almaz"
    assert row["confidence"] == "disagreement"
    assert row["dissenting_models"] == ["igor"]
    assert row["positive_model_count"] == 1
    assert result["summary"]["vega"] == 1


@pytest.mark.parametrize("kind,name", [
    ("weapon_skin", "AK-47 | Redline (Field-Tested)"),
    ("knife", "★ Karambit | Doppler (Factory New)"),
    ("glove", "★ Sport Gloves | Vice (Field-Tested)"),
])
def test_item_type_filters_distinguish_weapon_knife_glove(comparison_db, kind, name):
    add_opinion(comparison_db, "match", name=name)
    add_opinion(comparison_db, "unknown", name="Sticker | Crown (Foil)")
    result = fetch(comparison_db, asset_type=kind)
    assert result["asset_type"] == kind
    assert [row["listing_id"] for row in result["rows"]] == ["match"]
    assert result["rows"][0]["item_kind"] == kind
    assert result["summary"]["all"] == 1


def test_item_type_uses_authoritative_catalog(comparison_db):
    conn = comparison_db
    conn.execute("CREATE TABLE cs2_item_catalog (market_hash_name TEXT PRIMARY KEY, item_kind TEXT)")
    conn.execute("INSERT INTO cs2_item_catalog VALUES ('New catalog weapon', 'weapon_skin')")
    add_opinion(conn, "catalog", name="New catalog weapon")
    assert fetch(conn, asset_type="weapon_skin")["rows"][0]["listing_id"] == "catalog"


def test_source_filter_matches_secondary_routes_without_losing_dissent(comparison_db):
    conn = comparison_db
    add_opinion(conn, "cross-source")
    add_opinion(conn, "cross-source", "igor", verdict="bad")
    add_opinion(conn, "other")
    deal_scanner.upsert_listing_source(conn, "cross-source", {
        "source_type": "liquid_skin", "market_hash_name": "AK-47 | Redline (Field-Tested)"
    })

    result = fetch(conn, discovery_source="liquid_skin")

    assert result["total"] == 1
    row = result["rows"][0]
    assert row["listing_id"] == "cross-source"
    assert row["confidence"] == "disagreement"
    assert row["dissenting_models"] == ["igor"]
    assert row["discovery_sources"][0]["source_type"] == "liquid_skin"


def test_source_filter_keeps_all_opinions_when_only_one_matches(comparison_db):
    add_opinion(comparison_db, "mixed", source="liquid_skin")
    add_opinion(comparison_db, "mixed", "igor", verdict="bad", source="sticker")
    comparison_db.execute("DROP TABLE model_deal_listing_sources")

    result = fetch(comparison_db, discovery_source="liquid_skin")

    assert set(result["rows"][0]["models"]) == {"almaz", "igor"}
    assert result["rows"][0]["confidence"] == "disagreement"


def test_invalid_filters_fall_back_and_empty_database_reports_filters(comparison_db):
    result = fetch(comparison_db, asset_type="'; DROP TABLE sales;--", discovery_source="invalid")
    assert result["asset_type"] == result["discovery_source"] == "all"
    comparison_db.execute("DROP TABLE model_deal_predictions")
    result = fetch(comparison_db, asset_type="knife", discovery_source="knife")
    assert result["total"] == 0
    assert result["asset_type"] == result["discovery_source"] == "knife"


def test_comparison_http_route_passes_new_filters_to_results(comparison_db, monkeypatch, tmp_path):
    add_opinion(comparison_db, "match", source="liquid_skin")
    add_opinion(comparison_db, "match", "vega", source="liquid_skin")
    add_opinion(comparison_db, "no-vega", source="liquid_skin")
    database = tmp_path / "scanner.db"
    comparison_db.commit()
    with sqlite3.connect(database) as destination:
        comparison_db.backup(destination)

    @contextmanager
    def connect():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(dashboard_server, "connect_db", connect)
    monkeypatch.setattr(dashboard_server, "is_authorized", lambda *_: True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        client.request("GET", "/api/model-deal-comparison?category=vega&asset_type=weapon_skin&discovery_source=liquid_skin")
        response = client.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert [row["listing_id"] for row in payload["rows"]] == ["match"]
        assert payload["asset_type"] == "weapon_skin"
        assert payload["discovery_source"] == "liquid_skin"
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
