import http.client
import json
import re
import shutil
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import dashboard_server


def test_strategy_page_is_private_responsive_localized_and_in_navigation():
    templates = (
        dashboard_server.HTML,
        dashboard_server.DEAL_HTML,
        dashboard_server.DEALS_HTML,
        dashboard_server.STRATEGY_HTML,
    )
    for template in templates:
        assert 'data-nav-path="/strategy"' in template

    rendered = dashboard_server.render_private_html(dashboard_server.STRATEGY_HTML)
    assert dashboard_server.DASHBOARD_TOKEN not in rendered
    assert "__DASHBOARD_TOKEN__" not in rendered
    assert "От выгодного лота к взвешенному решению" in rendered
    assert "Собрать портфель" in rendered
    assert "готовый сохранённый отчёт" in rendered
    assert rendered.count('id="backtestHolding"') == 1
    assert '<option value="7" selected>7 дней</option>' in rendered
    assert '<option value="14">14 days</option>' not in rendered
    assert "@media (max-width: 780px)" in rendered
    assert 'role="img"' in rendered
    assert 'aria-live="polite"' in rendered
    assert 'id="trendFreshness"' in rendered
    assert "pipeline.process_alive" in rendered
    assert "'X-CS2-Action': 'strategy-read'" in rendered
    assert "hot_activation: 'безопасная горячая установка'" in dashboard_server.HTML
    assert "переключит модель без перезапуска" in dashboard_server.HTML
    assert "только при следующем ./start_all.sh" not in dashboard_server.HTML


def test_strategy_trend_reads_only_existing_configured_store_and_is_bounded(
    monkeypatch, tmp_path
):
    missing = tmp_path / "missing-trend.db"
    monkeypatch.setattr(dashboard_server, "TREND_DB_PATH", str(missing))

    unavailable = dashboard_server.build_strategy_trend_payload(limit=3)

    assert unavailable["status"] == "unavailable"
    assert unavailable["items"] == []
    assert not missing.exists()

    store = tmp_path / "trend.db"
    store.touch()
    monkeypatch.setattr(dashboard_server, "TREND_DB_PATH", str(store))
    calls = []

    def get_summary(path, name, *, ranking_horizon_days, limit):
        calls.append((path, name, ranking_horizon_days, limit))
        return {"status": "ready", "items": []}

    monkeypatch.setattr(dashboard_server, "get_trend_summary", get_summary)

    result = dashboard_server.build_strategy_trend_payload(
        "AK-47 | Test", horizon="14", limit="25"
    )

    assert result["status"] == "ready"
    assert calls == [(str(store), "AK-47 | Test", 14, 25)]
    for invalid in (
        {"horizon": 8},
        {"limit": 0},
        {"limit": dashboard_server.STRATEGY_MAX_TREND_ITEMS + 1},
    ):
        with pytest.raises(ValueError):
            dashboard_server.build_strategy_trend_payload(**invalid)


def test_strategy_portfolio_and_backtest_actions_are_strict_and_precomputed(
    monkeypatch, tmp_path
):
    deals_db = tmp_path / "deals.db"
    sqlite3.connect(deals_db).close()
    monkeypatch.setattr(dashboard_server, "DB_PATH", str(deals_db))
    portfolio_calls = []

    def build_portfolio(conn, **kwargs):
        portfolio_calls.append(kwargs)
        return {
            "item_count": 0,
            "items": [],
            "spent_cents": 0,
            "cash_remaining_cents": kwargs["budget_cents"],
        }

    monkeypatch.setattr(
        dashboard_server.deal_intelligence, "build_portfolio", build_portfolio
    )
    portfolio = dashboard_server.build_strategy_portfolio_payload(
        {
            "action": "build_portfolio",
            "budget_cents": 125_000,
            "risk": "balanced",
            "max_items": 7,
            "max_per_skin": 1,
            "max_per_collection": 2,
            "max_candidates": 150,
            "max_age_hours": 48,
        }
    )

    assert portfolio["trading_actions"] is False
    assert portfolio["precomputed_candidates_only"] is True
    assert portfolio_calls == [
        {
            "budget_cents": 125_000,
            "risk": "balanced",
            "max_items": 7,
            "max_per_skin": 1,
            "max_per_collection": 2,
            "max_candidates": 150,
            "max_age_hours": 48,
        }
    ]
    with pytest.raises(ValueError):
        dashboard_server.build_strategy_portfolio_payload(
            {"action": "build_portfolio", "budget_cents": 100, "url": "https://evil"}
        )

    trend_db = tmp_path / "trend.db"
    trend_db.touch()
    monkeypatch.setattr(dashboard_server, "TREND_DB_PATH", str(trend_db))
    backtest_calls = []

    def get_report(path, config):
        backtest_calls.append((path, config.to_dict()))
        return {"status": "ready", "metrics": {}, "report_id": "stored-1"}

    monkeypatch.setattr(dashboard_server, "get_backtest_report", get_report)
    report = dashboard_server.build_strategy_backtest_payload(
        {
            "action": "read_precomputed_backtest",
            "config": {
                "holding_days": 7,
                "top_k": 20,
                "rebalance_every_days": 9,
                "initial_capital_cents": 100_000,
            },
        }
    )

    assert report["status"] == "ready"
    assert report["precomputed_only"] is True
    assert report["network_actions"] is False
    assert report["trading_actions"] is False
    assert backtest_calls[0][0] == str(trend_db)
    assert backtest_calls[0][1]["holding_days"] == 7
    with pytest.raises(ValueError):
        dashboard_server.build_strategy_backtest_payload(
            {"action": "run_backtest", "config": {}}
        )


def test_strategy_post_security_rejects_cross_origin_and_unconfirmed_requests():
    base = {
        "Content-Type": "application/json; charset=utf-8",
        "X-CS2-Action": "strategy-read",
        "Sec-Fetch-Site": "same-origin",
        "Host": "dashboard.example:8010",
        "Origin": "https://dashboard.example:8010",
    }
    assert (
        dashboard_server.strategy_request_violation(SimpleNamespace(headers=base))
        is None
    )
    cases = (
        ({**base, "Content-Type": "text/plain"}, 415),
        ({**base, "X-CS2-Action": "model-training"}, 403),
        ({**base, "Sec-Fetch-Site": "cross-site"}, 403),
        ({**base, "Origin": "https://evil.example"}, 403),
        ({**base, "Transfer-Encoding": "chunked"}, 400),
    )
    for headers, expected in cases:
        status, _message = dashboard_server.strategy_request_violation(
            SimpleNamespace(headers=headers)
        )
        assert status == expected


def _comparison_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE model_deal_predictions (
            listing_id TEXT, model_name TEXT, verdict TEXT, last_seen_at TEXT,
            roi_percent REAL, profit_cents INTEGER, predicted_price_cents INTEGER,
            buy_price_cents INTEGER, skin_name TEXT, source_sticker_name TEXT,
            source_sticker_id TEXT, deal_payload_json TEXT, prediction_json TEXT,
            item_icon_url TEXT, screenshot_url TEXT, float_value REAL,
            paint_seed INTEGER, paint_index INTEGER, listing_created_at TEXT,
            csfloat_url TEXT, inspect_link TEXT, liquidity_score REAL,
            sticker_value_cents INTEGER, net_expected_cents INTEGER,
            pricing_method TEXT
        )
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO model_deal_predictions VALUES (
            ?, 'nova', 'good', ?, 20, 2000, 12000, 10000,
            'AK-47 | Test', 'Sticker | Test', '1', ?, ?,
            'https://example.com/icon.png', 'https://example.com/shot.png',
            0.12, 7, 1, ?, 'https://csfloat.com/item/one',
            'steam://inspect/one', 70, 1000, 11520, 'test'
        )
        """,
        (
            "listing-1",
            now,
            json.dumps({"listing_id": "listing-1", "name": "AK-47 | Test"}),
            json.dumps({"stickers": []}),
            now,
        ),
    )
    return conn


def test_existing_deals_page_attaches_one_bounded_risk_annotation_query(monkeypatch):
    conn = _comparison_db()
    calls = []
    annotation = {
        "listing_id": "listing-1",
        "risk": {"score": 22, "level": "low"},
        "sale_estimate": {"within_30d": 0.8, "estimated_days": 5},
        "ensemble_prediction": {"conservative_profit_cents": 1500, "weights": {}},
        "ranking_score": 123,
    }

    def get_annotations(_conn, listing_ids, *, limit):
        calls.append((listing_ids, limit))
        return {"listing-1": annotation}

    original_attach = dashboard_server.deal_intelligence.attach_risk_annotations
    monkeypatch.setattr(
        dashboard_server.deal_intelligence,
        "get_risk_annotations",
        get_annotations,
    )
    monkeypatch.setattr(
        dashboard_server.deal_intelligence,
        "attach_risk_annotations",
        original_attach,
    )

    payload = dashboard_server.fetch_model_deal_comparison_page(conn, 0, 100, "", "all")

    assert calls == [(["listing-1"], 1)]
    assert payload["rows"][0]["intelligence"] == annotation
    assert "intelligenceDetailLines(row)" in dashboard_server.DEALS_HTML
    assert "sale 30d / ETA" in dashboard_server.DEALS_HTML
    assert "ensemble safe profit" in dashboard_server.DEALS_HTML
    conn.close()


@contextmanager
def _strategy_http_server(monkeypatch):
    monkeypatch.setattr(
        dashboard_server,
        "build_strategy_overview_payload",
        lambda: {"strategy_schema_version": 1, "safety": {"trading_actions": False}},
    )
    monkeypatch.setattr(
        dashboard_server,
        "build_strategy_portfolio_payload",
        lambda payload: {"received": payload, "trading_actions": False},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        content_type = response.getheader("Content-Type", "")
        payload = json.loads(raw) if "application/json" in content_type else raw
        return response.status, payload
    finally:
        connection.close()


def test_strategy_http_routes_require_auth_and_apply_same_origin(monkeypatch):
    with _strategy_http_server(monkeypatch) as port:
        status, _payload = _request(port, "GET", "/strategy")
        assert status == 401
        status, _payload = _request(port, "GET", "/api/strategy/overview")
        assert status == 401

        auth = {"X-Dashboard-Token": dashboard_server.DASHBOARD_TOKEN}
        status, payload = _request(port, "GET", "/api/strategy/overview", headers=auth)
        assert status == 200
        assert payload["safety"]["trading_actions"] is False

        body = json.dumps({"action": "build_portfolio", "budget_cents": 100_000})
        safe_headers = {
            **auth,
            "Content-Type": "application/json",
            "X-CS2-Action": "strategy-read",
            "Sec-Fetch-Site": "same-origin",
            "Origin": f"http://127.0.0.1:{port}",
        }
        status, payload = _request(
            port,
            "POST",
            "/api/strategy/portfolio",
            body=body,
            headers=safe_headers,
        )
        assert status == 200
        assert payload["received"]["budget_cents"] == 100_000

        status, _payload = _request(
            port,
            "POST",
            "/api/strategy/portfolio",
            body=body,
            headers={**safe_headers, "Origin": "https://evil.example"},
        )
        assert status == 403


def test_strategy_inline_javascript_passes_node_syntax_check(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    rendered = dashboard_server.render_private_html(dashboard_server.STRATEGY_HTML)
    scripts = re.findall(
        r"<script(?:\s[^>]*)?>(.*?)</script>",
        rendered,
        flags=re.IGNORECASE | re.DOTALL,
    )
    script_path = tmp_path / "strategy-inline.js"
    script_path.write_text("\n".join(scripts), encoding="utf-8")

    completed = subprocess.run(
        [node, "--check", str(script_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
