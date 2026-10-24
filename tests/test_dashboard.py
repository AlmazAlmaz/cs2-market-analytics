import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import dashboard_server
import deal_scanner
import model_generations
import scanner_control


def test_dashboard_cookie_auth_keeps_secret_out_of_html():
    handler = SimpleNamespace(
        headers={"Cookie": f"other=x; cs2_dashboard={dashboard_server.DASHBOARD_TOKEN}"}
    )
    assert dashboard_server.is_authorized(handler, {})

    rendered = dashboard_server.render_private_html(
        '<a href="/deals?token=__DASHBOARD_TOKEN__">x</a>'
        '<script>const token = "__DASHBOARD_TOKEN__";</script>'
    )
    assert dashboard_server.DASHBOARD_TOKEN not in rendered
    assert "__DASHBOARD_TOKEN__" not in rendered
    assert 'href="/deals"' in rendered


def test_dashboard_startup_log_does_not_expose_admin_token():
    messages = "\n".join(
        dashboard_server.dashboard_startup_messages(reset_email_configured=True)
    )

    assert dashboard_server.DASHBOARD_TOKEN not in messages
    assert "?token=" not in messages
    assert str(dashboard_server.TOKEN_PATH) in messages
    assert "password-reset email: configured" in messages


def test_health_reports_dedicated_knife_model_separately(monkeypatch, tmp_path):
    database = tmp_path / "health.db"
    sqlite3.connect(database).close()
    knife_dir = tmp_path / "KNIFEMODEL" / "artifacts"
    knife_dir.mkdir(parents=True)
    (knife_dir / "knife_price_model.joblib").write_bytes(b"knife-model")
    monkeypatch.setattr(dashboard_server, "DB_PATH", str(database))
    monkeypatch.setattr(dashboard_server.app_config, "PROJECT_DIR", tmp_path)

    payload = dashboard_server.build_health_payload()

    assert payload["ok"] is True
    assert set(payload["models"]) == {
        "almaz",
        "igor",
        "nova",
        "vega",
        "knives",
        "gloves",
    }
    assert payload["models"]["knives"] is True
    assert payload["models"]["vega"] is False


def test_private_pages_share_navigation_and_design_without_token_leaks():
    pages = (dashboard_server.HTML, dashboard_server.DEAL_HTML, dashboard_server.DEALS_HTML)
    for template in pages:
        rendered = dashboard_server.render_private_html(template)
        assert dashboard_server.DASHBOARD_TOKEN not in rendered
        assert "__DASHBOARD_TOKEN__" not in rendered
        assert 'class="site-nav lean-nav"' in rendered
        assert 'data-nav-path="/deals"' in rendered
        assert 'href="/neural"' not in rendered
        assert "--radius-lg: 22px" in rendered
        assert "className = 'back-to-top'" in rendered


def test_dashboard_progressively_discloses_logical_workspaces():
    assert dashboard_server.HTML.count('data-dashboard-tab=') == 2
    assert 'data-dashboard-tab="overview"' in dashboard_server.HTML
    assert 'data-dashboard-tab="system"' in dashboard_server.HTML
    assert 'data-dashboard-tab="parser"' not in dashboard_server.HTML
    assert 'data-dashboard-tab="database"' not in dashboard_server.HTML
    assert 'data-dashboard-tab="market"' not in dashboard_server.HTML
    assert dashboard_server.HTML.count('data-dashboard-view="system"') >= 6
    assert "dashboardViews = new Set(['overview', 'system'])" in dashboard_server.HTML
    assert (
        "if (['parser', 'database'].includes(initialDashboardView)) "
        "initialDashboardView = 'system';"
    ) in dashboard_server.HTML
    assert (
        "if (initialDashboardView === 'market') initialDashboardView = 'overview';"
        in dashboard_server.HTML
    )
    assert 'id="systemHealthBanner"' in dashboard_server.HTML
    assert "function renderSystemHealth(data)" in dashboard_server.HTML


def test_collection_expression_prefers_new_name_and_falls_back_to_legacy_column():
    both = dashboard_server.optional_collection_name_expr(
        {"item_collection_name", "item_collection"}
    )
    legacy = dashboard_server.optional_collection_name_expr({"item_collection"})

    assert both == (
        "COALESCE(NULLIF(TRIM(s.item_collection_name), ''), "
        "NULLIF(TRIM(s.item_collection), '')) AS item_collection_name"
    )
    assert legacy == "NULLIF(TRIM(s.item_collection), '') AS item_collection_name"


def test_deal_scanner_controls_are_visible_separate_and_csrf_hardened():
    assert 'id="dealScannerControlsMount"' in dashboard_server.HTML
    assert 'id="scannerToggleButton"' in dashboard_server.HTML
    assert 'id="scannerIntervalMinutes"' in dashboard_server.HTML
    assert "Основной парсер истории продаж продолжит работать" in dashboard_server.HTML
    assert "'X-CS2-Action': 'scanner-control'" in dashboard_server.HTML
    assert dashboard_server.HTML.count("/api/scanner-control") >= 2


def test_scanner_control_payload_distinguishes_desired_state_from_runtime(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(scanner_control, "CONTROL_PATH", tmp_path / "control.json")
    monkeypatch.setattr(scanner_control, "STATUS_PATH", tmp_path / "status.json")

    offline = dashboard_server.build_scanner_control_payload()
    assert offline["control"]["enabled"] is True
    assert offline["runtime"]["effective_state"] == "offline"
    assert offline["health"] == "offline"
    assert "pid" not in offline["runtime"]

    paused = dashboard_server.update_scanner_control_from_payload(
        {"enabled": False, "interval_seconds": 300}
    )
    assert paused["control"]["enabled"] is False
    assert paused["control"]["interval_seconds"] == 300
    assert paused["health"] == "paused"


def test_scanner_control_payload_reports_fresh_live_cycle(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(scanner_control, "CONTROL_PATH", tmp_path / "control.json")
    monkeypatch.setattr(scanner_control, "STATUS_PATH", tmp_path / "status.json")
    scanner_control.get_scanner_control(path=tmp_path / "control.json")
    scanner_control.update_scanner_status(
        path=tmp_path / "status.json",
        process_state="scanning",
        pid=dashboard_server.os.getpid(),
        heartbeat_at=now.isoformat(),
        cycle_started_at=now.isoformat(),
        next_scan_at=(now + timedelta(minutes=5)).isoformat(),
        cycle_number=2,
        cycle_targets_total=5,
        cycle_targets_scanned=2,
        cycle_listings_returned=40,
        cycle_candidates_saved=3,
    )

    payload = dashboard_server.build_scanner_control_payload(now=now)

    assert payload["health"] == "scanning"
    assert payload["runtime"]["online"] is True
    assert payload["runtime"]["effective_state"] == "scanning"
    assert payload["runtime"]["next_scan_in_seconds"] == 300


def test_live_pid_with_stale_heartbeat_is_unresponsive_not_offline(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(scanner_control, "CONTROL_PATH", tmp_path / "control.json")
    monkeypatch.setattr(scanner_control, "STATUS_PATH", tmp_path / "status.json")
    scanner_control.get_scanner_control(path=tmp_path / "control.json")
    scanner_control.update_scanner_status(
        path=tmp_path / "status.json",
        process_state="scanning",
        pid=dashboard_server.os.getpid(),
        heartbeat_at=(now - timedelta(minutes=5)).isoformat(),
    )

    payload = dashboard_server.build_scanner_control_payload(now=now)

    assert payload["health"] == "unresponsive"
    assert payload["runtime"]["pid_alive"] is True
    assert payload["runtime"]["online"] is False
    assert payload["runtime"]["effective_state"] == "unresponsive"


def test_future_heartbeat_cannot_keep_reused_pid_online(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(scanner_control, "CONTROL_PATH", tmp_path / "control.json")
    monkeypatch.setattr(scanner_control, "STATUS_PATH", tmp_path / "status.json")
    scanner_control.get_scanner_control(path=tmp_path / "control.json")
    scanner_control.update_scanner_status(
        path=tmp_path / "status.json",
        process_state="scanning",
        pid=dashboard_server.os.getpid(),
        heartbeat_at=(now + timedelta(days=1)).isoformat(),
    )

    payload = dashboard_server.build_scanner_control_payload(now=now)

    assert payload["health"] == "unresponsive"
    assert payload["runtime"]["heartbeat_in_future"] is True
    assert payload["runtime"]["online"] is False


def test_scanner_control_update_rejects_ambiguous_or_unsafe_values():
    for payload in (
        {},
        {"enabled": 1},
        {"interval_seconds": True},
        {"interval_seconds": 59},
        {"interval_seconds": 86401},
        {"unknown": "value"},
    ):
        try:
            dashboard_server.update_scanner_control_from_payload(payload)
        except ValueError:
            pass
        else:  # pragma: no cover - each bad payload must fail before persistence
            raise AssertionError(f"unsafe scanner payload accepted: {payload}")


def test_scanner_update_reports_committed_control_when_status_read_fails(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(scanner_control, "CONTROL_PATH", tmp_path / "control.json")
    monkeypatch.setattr(scanner_control, "STATUS_PATH", tmp_path / "status.json")
    scanner_control.get_scanner_control(path=tmp_path / "control.json")

    def fail_snapshot():
        raise scanner_control.ScannerControlError("simulated status lock failure")

    monkeypatch.setattr(dashboard_server, "build_scanner_control_payload", fail_snapshot)
    result = dashboard_server.update_scanner_control_from_payload({"enabled": False})

    stored = scanner_control.get_scanner_control(path=tmp_path / "control.json")
    assert stored["enabled"] is False
    assert result["control"]["enabled"] is False
    assert result["health"] == "status_unavailable"


def test_deal_and_deals_pages_keep_critical_controls_unique():
    for control_id in (
        "urlText",
        "skinName",
        "buyPrice",
        "feePercent",
        "floatValue",
        "stickersJson",
        "predictButton",
    ):
        assert dashboard_server.DEAL_HTML.count(f'id="{control_id}"') == 1

    for control_id in (
        "search",
        "sortSelect",
        "categorySelect",
        "confidenceSelect",
        "resetFiltersButton",
        "dealSummaryTotal",
    ):
        assert dashboard_server.DEALS_HTML.count(f'id="{control_id}"') == 1
    assert 'class="disclosure filter-disclosure"' in dashboard_server.DEALS_HTML


def manual_prediction(model_name, predicted, roi, verdict):
    return {
        "model_name": model_name,
        "artifact_version": 2,
        "predicted_price_cents": predicted,
        "low_price_cents": predicted - 100,
        "high_price_cents": predicted + 100,
        "net_expected_cents": round(predicted * 0.96),
        "profit_cents": round(predicted * 0.96) - 1000,
        "roi_percent": roi,
        "liquidity_score": 50,
        "historical_sales_count": 100,
        "sales_per_day": 2,
        "sticker_value_cents": 0,
        "sticker_count": 0,
        "fee_percent": 4,
        "verdict": verdict,
    }


def test_manual_comparison_leads_with_conservative_model_not_highest_roi(
    monkeypatch,
):
    import igor_model_runtime
    import neural_model_runtime
    import price_model_runtime

    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(dashboard_server, "connect_db", lambda: conn)
    monkeypatch.setattr(
        price_model_runtime,
        "predict_deal",
        lambda _payload, _conn: manual_prediction("almaz", 1400, 34.4, "good"),
    )
    monkeypatch.setattr(
        igor_model_runtime,
        "predict_deal",
        lambda _payload, _conn: manual_prediction("igor", 1100, 5.6, "watch"),
    )
    monkeypatch.setattr(
        neural_model_runtime,
        "predict_deal",
        lambda _payload, _conn: manual_prediction("nova", 1300, 24.8, "good"),
    )

    result = dashboard_server.build_predict_deal_payload(
        {"name": "AK-47 | Slate (Field-Tested)", "buy_price_cents": 1000}
    )

    assert result["selected_model"] == "igor"
    assert result["selection_method"] == "minimum_available_predicted_price"
    assert result["predicted_price_cents"] == 1100
    assert result["comparison"]["confidence"] == "unanimous_three"
    assert result["comparison"]["prediction_spread_cents"] == 300
    assert set(result["models"]) == {"almaz", "igor", "nova"}


def test_manual_comparison_routes_knives_only_to_dedicated_model(monkeypatch):
    import igor_model_runtime
    import knife_model_runtime
    import neural_model_runtime
    import price_model_runtime

    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(dashboard_server, "connect_db", lambda: conn)
    monkeypatch.setattr(
        knife_model_runtime,
        "predict_deal",
        lambda _payload, _conn: manual_prediction(
            "knives", 22_000, 8.0, "watch"
        ),
    )
    for runtime in (price_model_runtime, igor_model_runtime, neural_model_runtime):
        monkeypatch.setattr(
            runtime,
            "predict_deal",
            lambda *_args: pytest.fail("weapon model must not receive a knife"),
        )

    result = dashboard_server.build_predict_deal_payload(
        {"name": "★ Karambit | Doppler (Factory New)"}
    )

    assert result["selected_model"] == "knives"
    assert set(result["models"]) == {"knives"}
    assert result["comparison"]["active_models"] == ["knives"]
    assert result["comparison"]["confidence"] == "single_signal"


def test_manual_comparison_keeps_active_vega_advisory_out_of_core_decision(
    monkeypatch,
):
    import igor_model_runtime
    import neural_model_runtime
    import price_model_runtime

    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(dashboard_server, "connect_db", lambda: conn)
    monkeypatch.setattr(
        model_generations,
        "read_active_pointer",
        lambda model, **_kwargs: {"generation_id": "a" * 64}
        if model == "vega"
        else None,
    )
    monkeypatch.setitem(
        sys.modules,
        "vega_model_runtime",
        SimpleNamespace(
            predict_deal=lambda _payload, _conn: manual_prediction(
                "vega", 9_000, 760, "bad"
            )
        ),
    )
    monkeypatch.setattr(
        price_model_runtime,
        "predict_deal",
        lambda _payload, _conn: manual_prediction("almaz", 1_400, 34.4, "good"),
    )
    monkeypatch.setattr(
        igor_model_runtime,
        "predict_deal",
        lambda _payload, _conn: manual_prediction("igor", 1_100, 5.6, "watch"),
    )
    monkeypatch.setattr(
        neural_model_runtime,
        "predict_deal",
        lambda _payload, _conn: manual_prediction("nova", 1_300, 24.8, "good"),
    )

    result = dashboard_server.build_predict_deal_payload(
        {"name": "AK-47 | Slate (Field-Tested)", "buy_price_cents": 1_000}
    )

    assert result["selected_model"] == "igor"
    assert result["comparison"]["confidence"] == "unanimous_three"
    assert result["comparison"]["dissenting_models"] == []
    assert result["comparison"]["advisory_models"] == ["vega"]
    assert result["comparison"]["positive_advisory_models"] == []
    assert result["comparison"]["prediction_spread_cents"] == 300


def test_manual_comparison_never_promotes_vega_when_all_core_models_fail(
    monkeypatch,
):
    import igor_model_runtime
    import neural_model_runtime
    import price_model_runtime

    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(dashboard_server, "connect_db", lambda: conn)
    monkeypatch.setattr(
        model_generations,
        "read_active_pointer",
        lambda model, **_kwargs: {"generation_id": "a" * 64}
        if model == "vega"
        else None,
    )
    monkeypatch.setitem(
        sys.modules,
        "vega_model_runtime",
        SimpleNamespace(
            predict_deal=lambda _payload, _conn: manual_prediction(
                "vega", 900, 12.0, "good"
            )
        ),
    )

    def fail_core(*_args):
        raise RuntimeError("core unavailable")

    for runtime in (price_model_runtime, igor_model_runtime, neural_model_runtime):
        monkeypatch.setattr(runtime, "predict_deal", fail_core)

    result = dashboard_server.build_predict_deal_payload(
        {"name": "AK-47 | Slate (Field-Tested)", "buy_price_cents": 1_000}
    )

    assert "core prediction unavailable" in result["error"]
    assert set(result["models"]) == {"vega"}
    assert set(result["model_errors"]) == {"almaz", "igor", "nova"}
    assert "selected_model" not in result


def test_manual_comparison_routes_gloves_only_to_dedicated_model(monkeypatch):
    import glove_model_runtime
    import igor_model_runtime
    import knife_model_runtime
    import neural_model_runtime
    import price_model_runtime

    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(dashboard_server, "connect_db", lambda: conn)
    monkeypatch.setattr(
        glove_model_runtime,
        "predict_deal",
        lambda _payload, _conn: manual_prediction(
            "gloves", 48_000, 7.0, "watch"
        ),
    )
    for runtime in (
        price_model_runtime,
        igor_model_runtime,
        neural_model_runtime,
        knife_model_runtime,
    ):
        monkeypatch.setattr(
            runtime,
            "predict_deal",
            lambda *_args: pytest.fail("another model must not receive a glove"),
        )

    result = dashboard_server.build_predict_deal_payload(
        {"name": "★ Sport Gloves | Vice (Field-Tested)"}
    )

    assert result["selected_model"] == "gloves"
    assert set(result["models"]) == {"gloves"}
    assert result["comparison"]["active_models"] == ["gloves"]
    assert result["comparison"]["confidence"] == "single_signal"


def test_manual_comparison_fails_closed_for_unknown_item_kind(monkeypatch):
    import glove_model_runtime
    import igor_model_runtime
    import knife_model_runtime
    import neural_model_runtime
    import price_model_runtime

    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(dashboard_server, "connect_db", lambda: conn)
    for runtime in (
        price_model_runtime,
        igor_model_runtime,
        neural_model_runtime,
        knife_model_runtime,
        glove_model_runtime,
    ):
        monkeypatch.setattr(
            runtime,
            "predict_deal",
            lambda *_args: pytest.fail("unknown item must not reach any model"),
        )

    result = dashboard_server.build_predict_deal_payload(
        {"name": "Not a canonical CS2 market item"}
    )

    assert result == {
        "error": "item type is unknown; choose an exact skin, knife, or glove name"
    }


def test_manual_comparison_keeps_working_model_results_and_surfaces_dissent(
    monkeypatch,
):
    import igor_model_runtime
    import neural_model_runtime
    import price_model_runtime

    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(dashboard_server, "connect_db", lambda: conn)
    monkeypatch.setattr(
        price_model_runtime,
        "predict_deal",
        lambda _payload, _conn: manual_prediction("almaz", 1200, 15, "good"),
    )

    def fail_igor(_payload, _conn):
        raise RuntimeError("simulated contract failure")

    monkeypatch.setattr(igor_model_runtime, "predict_deal", fail_igor)
    monkeypatch.setattr(
        neural_model_runtime,
        "predict_deal",
        lambda _payload, _conn: manual_prediction("nova", 900, -14, "bad"),
    )
    # Pin the advisory Vega opinion to "active but failing on this empty
    # database" so the result does not depend on whether trained Vega weights
    # exist on the machine running the test (they are not in the repository).
    import vega_model_runtime

    real_pointer = dashboard_server.model_generations.read_active_pointer
    monkeypatch.setattr(
        dashboard_server.model_generations,
        "read_active_pointer",
        lambda model, **kwargs: (
            {"generation_id": "test-vega"} if model == "vega" else real_pointer(model, **kwargs)
        ),
    )

    def fail_vega(_payload, _conn):
        raise sqlite3.OperationalError("no such table: sales")

    monkeypatch.setattr(vega_model_runtime, "predict_deal", fail_vega)

    result = dashboard_server.build_predict_deal_payload(
        {"name": "AK-47 | Slate (Field-Tested)", "buy_price_cents": 1000}
    )

    assert result["selected_model"] == "nova"
    assert result["comparison"]["confidence"] == "disagreement"
    assert result["comparison"]["dissenting_models"] == ["nova"]
    assert result["comparison"]["unavailable_models"] == ["igor", "vega"]
    assert result["model_errors"] == {
        "igor": "RuntimeError",
        "vega": "OperationalError",
    }
    assert set(result["models"]) == {"almaz", "nova"}


def test_manual_comparison_ui_is_explicit_and_does_not_sort_by_max_roi():
    rendered = dashboard_server.render_private_html(dashboard_server.DEAL_HTML, "ru")
    assert rendered.count('data-nav-path="/deal"') == 1
    assert 'data-lean-label="evaluate"' in rendered
    assert 'href="/neural"' not in rendered
    assert 'id="comparisonCallout"' in dashboard_server.DEAL_HTML
    assert 'id="modelComparisonGrid"' in dashboard_server.DEAL_HTML
    assert "Headline figures use" in dashboard_server.DEAL_HTML
    assert "resultRows" not in dashboard_server.DEAL_HTML
    assert "Compare models" in dashboard_server.DEAL_HTML


def prediction_row(
    conn,
    model_name,
    roi,
    profit,
    *,
    listing_id="listing-1",
    verdict="good",
    last_seen_at=None,
    predicted=1200,
):
    observed_at = last_seen_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO model_deal_predictions (
            listing_id, model_name, first_seen_at, last_seen_at, scan_count,
            skin_name, buy_price_cents, predicted_price_cents, profit_cents,
            roi_percent, verdict, deal_payload_json, prediction_json
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            listing_id,
            model_name,
            observed_at,
            observed_at,
            f"Skin {listing_id} from {model_name}",
            1000,
            predicted,
            profit,
            roi,
            verdict,
            json.dumps({"name": "AK-47 | Test (Field-Tested)", "listing_type": "buy_now"}),
            "{}",
        ),
    )


def test_best_model_uses_roi_then_profit_and_tracks_nova(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    prediction_row(conn, "almaz", 10, 100)
    prediction_row(conn, "igor", 9, 1000)
    prediction_row(conn, "nova", 8, 2000)
    monkeypatch.setattr(dashboard_server, "add_deal_visual_fallbacks", lambda _conn, _rows: None)

    result = dashboard_server.fetch_model_deal_comparison_page(
        conn=conn,
        offset=0,
        limit=10,
        search="",
        category="all",
        seen_hours=0,
    )
    assert result["total"] == 1
    assert result["rows"][0]["best"]["model_name"] == "almaz"
    assert result["rows"][0]["category"] == "all_three"
    assert result["summary"]["nova"] == 1
    assert result["summary"]["all_three"] == 1
    assert result["rows"][0]["confidence"] == "unanimous_three"


def test_stored_vega_opinion_is_visible_but_never_changes_core_consensus(
    monkeypatch,
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    prediction_row(conn, "almaz", 10, 100)
    prediction_row(conn, "igor", 9, 90)
    prediction_row(conn, "nova", 8, 80)
    prediction_row(conn, "vega", 100, 10_000)
    monkeypatch.setattr(
        dashboard_server, "add_deal_visual_fallbacks", lambda _conn, _rows: None
    )

    result = dashboard_server.fetch_model_deal_comparison_page(
        conn=conn,
        offset=0,
        limit=10,
        search="",
        category="all",
        seen_hours=0,
    )
    row = result["rows"][0]

    assert set(row["models"]) == {"almaz", "igor", "nova", "vega"}
    assert row["best"]["model_name"] == "almaz"
    assert row["category"] == "all_three"
    assert row["confidence"] == "unanimous_three"
    assert row["advisory_models"] == ["vega"]
    assert row["positive_advisory_models"] == ["vega"]
    assert row["conservative_roi_percent"] == 8


def test_comparison_keeps_dissenting_models_visible(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    prediction_row(conn, "almaz", 15, 150, verdict="good")
    prediction_row(conn, "igor", -20, -200, verdict="bad")
    prediction_row(conn, "nova", -5, -50, verdict="thin")
    monkeypatch.setattr(dashboard_server, "add_deal_visual_fallbacks", lambda _conn, _rows: None)

    result = dashboard_server.fetch_model_deal_comparison_page(conn, 0, 10, "", "all")
    row = result["rows"][0]

    assert set(row["models"]) == {"almaz", "igor", "nova"}
    assert row["best"]["model_name"] == "almaz"
    assert row["category"] == "only_almaz"
    assert row["confidence"] == "disagreement"
    assert row["dissenting_models"] == ["igor", "nova"]
    assert row["conservative_roi_percent"] == -20
    assert result["summary"]["almaz"] == 1
    assert result["summary"]["igor"] == 0
    assert result["summary"]["disagreement"] == 1


def test_comparison_ignores_stale_opinions_but_keeps_fresh_dissent(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    prediction_row(conn, "almaz", 15, 150, verdict="good")
    prediction_row(conn, "igor", -20, -200, verdict="bad", last_seen_at=old)
    monkeypatch.setattr(dashboard_server, "add_deal_visual_fallbacks", lambda _conn, _rows: None)

    result = dashboard_server.fetch_model_deal_comparison_page(
        conn, 0, 10, "", "all", opinion_stale_hours=24
    )
    row = result["rows"][0]

    assert set(row["models"]) == {"almaz"}
    assert row["confidence"] == "single_signal"
    assert row["dissenting_models"] == []
    assert row["conservative_roi_percent"] == 15


def test_zero_scores_remain_valid_when_selecting_best_model(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    prediction_row(conn, "almaz", 0, 0, verdict="watch")
    prediction_row(conn, "igor", -1, -1, verdict="watch")
    monkeypatch.setattr(dashboard_server, "add_deal_visual_fallbacks", lambda _conn, _rows: None)

    row = dashboard_server.fetch_model_deal_comparison_page(
        conn, 0, 10, "", "all"
    )["rows"][0]

    assert row["best"]["model_name"] == "almaz"
    assert row["max_roi_percent"] == 0
    assert row["max_profit_cents"] == 0
    assert row["conservative_roi_percent"] == -1


def test_unanimous_three_requires_exact_known_trio_without_dissent(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    for model_name in ("almaz", "igor", "nova"):
        prediction_row(conn, model_name, 10, 100, listing_id="known-plus-dissent")
    prediction_row(
        conn, "future", -5, -50, listing_id="known-plus-dissent", verdict="bad"
    )
    for model_name in ("foo", "bar", "baz"):
        prediction_row(conn, model_name, 10, 100, listing_id="unknown-trio")
    monkeypatch.setattr(dashboard_server, "add_deal_visual_fallbacks", lambda _conn, _rows: None)

    result = dashboard_server.fetch_model_deal_comparison_page(conn, 0, 10, "", "all")
    rows = {row["listing_id"]: row for row in result["rows"]}

    assert rows["known-plus-dissent"]["confidence"] == "disagreement"
    assert rows["known-plus-dissent"]["dissenting_models"] == ["future"]
    assert rows["unknown-trio"]["confidence"] == "consensus"


def test_incomplete_conservative_metrics_sort_last(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    prediction_row(conn, "almaz", 10, 100, listing_id="complete")
    prediction_row(conn, "igor", 5, 50, listing_id="complete")
    prediction_row(conn, "almaz", 80, 800, listing_id="incomplete")
    prediction_row(
        conn,
        "igor",
        None,
        None,
        listing_id="incomplete",
        verdict="bad",
        predicted=None,
    )
    monkeypatch.setattr(dashboard_server, "add_deal_visual_fallbacks", lambda _conn, _rows: None)

    result = dashboard_server.fetch_model_deal_comparison_page(
        conn, 0, 10, "", "all", sort_mode="conservative_roi"
    )

    assert [row["listing_id"] for row in result["rows"]] == ["complete", "incomplete"]
    incomplete = result["rows"][1]
    assert incomplete["conservative_roi_percent"] is None
    assert incomplete["conservative_profit_cents"] is None
    assert incomplete["prediction_spread_percent"] is None


def test_confidence_filters_and_conservative_sort(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    prediction_row(conn, "almaz", 20, 200, listing_id="consensus")
    prediction_row(conn, "igor", 10, 100, listing_id="consensus")
    prediction_row(conn, "almaz", 80, 800, listing_id="mixed")
    prediction_row(conn, "igor", -10, -100, listing_id="mixed", verdict="bad")
    prediction_row(conn, "nova", 30, 300, listing_id="single")
    monkeypatch.setattr(dashboard_server, "add_deal_visual_fallbacks", lambda _conn, _rows: None)

    consensus = dashboard_server.fetch_model_deal_comparison_page(
        conn, 0, 10, "", "all", confidence="consensus"
    )
    disagreement = dashboard_server.fetch_model_deal_comparison_page(
        conn, 0, 10, "", "all", confidence="disagreement"
    )
    single = dashboard_server.fetch_model_deal_comparison_page(
        conn, 0, 10, "", "all", confidence="single_signal"
    )
    safely_sorted = dashboard_server.fetch_model_deal_comparison_page(
        conn, 0, 10, "", "all", sort_mode="conservative_roi"
    )

    assert [row["listing_id"] for row in consensus["rows"]] == ["consensus"]
    assert [row["listing_id"] for row in disagreement["rows"]] == ["mixed"]
    assert [row["listing_id"] for row in single["rows"]] == ["single"]
    assert [row["listing_id"] for row in safely_sorted["rows"]] == [
        "single",
        "consensus",
        "mixed",
    ]


def test_non_finite_filters_do_not_crash(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    monkeypatch.setattr(dashboard_server, "add_deal_visual_fallbacks", lambda _conn, _rows: None)
    result = dashboard_server.fetch_model_deal_comparison_page(
        conn, 0, 10, "", "all", seen_hours="nan", created_hours="inf", min_price="nan"
    )
    assert result["rows"] == []
    assert result["seen_hours"] == 0
    assert result["created_hours"] == 0


def test_price_bounds_and_page_offset_are_normalized(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    for index in range(15):
        prediction_row(conn, "almaz", 10, 100, listing_id=f"listing-{index:02d}")
    monkeypatch.setattr(dashboard_server, "add_deal_visual_fallbacks", lambda _conn, _rows: None)

    result = dashboard_server.fetch_model_deal_comparison_page(
        conn,
        10**100,
        10,
        "",
        "all",
        min_price="20",
        max_price="10",
    )

    assert result["offset"] == 10
    assert result["total"] == 15
    assert len(result["rows"]) == 5
    assert result["min_price_cents"] == 1000
    assert result["max_price_cents"] == 2000


def test_deals_url_state_validates_offset_and_applies_server_price_normalization():
    assert "Number.isSafeInteger(parsed)" in dashboard_server.DEALS_HTML
    assert "parsed - (parsed % limit)" in dashboard_server.DEALS_HTML
    assert "function applyNormalizedFilterState(data, requestedMinPrice" in dashboard_server.DEALS_HTML
    assert "priceInputFromCents(data.min_price_cents)" in dashboard_server.DEALS_HTML


def test_model_freshness_compares_artifact_metrics_with_current_db(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "csfloat_price_model.joblib").write_bytes(b"model")
    (model_dir / "csfloat_price_model_metrics.json").write_text(
        json.dumps({
            "artifact_version": 3,
            "trained_at": "2026-08-01T00:00:00+00:00",
            "rows": 100,
            "test": {
                "mae_usd": 1.25,
                "median_absolute_percentage_error": 0.05,
                "p90_absolute_percentage_error": 0.25,
            },
        }),
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sales (fetched_at TEXT)")

    result = dashboard_server.build_model_freshness(conn, 121, project_dir=tmp_path)
    almaz = next(model for model in result["models"] if model["name"] == "almaz")

    assert almaz["training_rows"] == 100
    assert almaz["new_rows_since_training"] == 21
    assert almaz["data_growth_percent"] == 21
    assert almaz["freshness"] == "retrain_recommended"
    assert almaz["quality"]["median_ape_percent"] == 5
    assert almaz["quality"]["p90_ape_percent"] == 25


def test_model_freshness_reads_active_generation_instead_of_legacy_baseline(tmp_path):
    legacy = tmp_path / "models"
    legacy.mkdir()
    (legacy / "csfloat_price_model.joblib").write_bytes(b"legacy")
    (legacy / "csfloat_price_model_metrics.json").write_text(
        json.dumps({"trained_at": "2026-07-01T00:00:00+00:00", "rows": 100}),
        encoding="utf-8",
    )
    job_id = "9" * 32
    candidate = tmp_path / model_generations.STAGING_ROOTS["almaz"] / job_id
    candidate.mkdir(parents=True)
    (candidate / "csfloat_price_model.joblib").write_bytes(b"active")
    (candidate / "csfloat_price_model_metrics.json").write_text(
        json.dumps({"trained_at": "2026-08-11T00:00:00+00:00", "rows": 200}),
        encoding="utf-8",
    )
    records = {}
    for filename in model_generations.MODEL_FILES["almaz"]:
        path = candidate / filename
        records[filename] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }
    manifest = {
        "files": records,
        "bundle_sha256": model_generations._bundle_sha256(records),
        "item_kind": "weapon_skin",
        "quality_gate": {"strictly_improved_metrics": ["primary"]},
    }
    model_generations._publish_candidate(
        "almaz",
        candidate,
        manifest=manifest,
        job_id=job_id,
        project_dir=tmp_path,
        validator=lambda *_args: manifest,
        load_and_smoke=lambda *_args: None,
    )
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sales (fetched_at TEXT)")

    result = dashboard_server.build_model_freshness(conn, 210, project_dir=tmp_path)
    almaz = next(model for model in result["models"] if model["name"] == "almaz")

    assert almaz["training_rows"] == 200
    assert almaz["new_rows_since_training"] == 10
    assert almaz["active_bundle_version"] == manifest["bundle_sha256"]
    assert almaz["generation_error"] is None


def test_model_freshness_uses_authoritative_source_snapshot_rows(tmp_path):
    artifact_dir = tmp_path / "IGORMODEL" / "artifacts"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "price_model_point.cbm").write_bytes(b"model")
    (artifact_dir / "igor_metrics.json").write_text(
        json.dumps({
            "trained_at": "2026-08-10T00:00:00+00:00",
            "source_database": {"valid_price_rows": 800},
            "split": {"train_rows": 640, "validation_rows": 80, "test_rows": 80},
            "test": {"mae_usd": 1.1, "median_ape_%": 5.0, "p90_ape_%": 25.0},
        }),
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sales (fetched_at TEXT)")

    result = dashboard_server.build_model_freshness(conn, 820, project_dir=tmp_path)
    igor = next(model for model in result["models"] if model["name"] == "igor")

    assert igor["training_rows"] == 800
    assert igor["training_rows_estimated"] is False
    assert igor["new_rows_since_training"] == 20
    assert igor["freshness"] == "fresh"
    assert igor["quality"]["mae_usd"] == 1.1


def test_model_freshness_reports_database_mismatch(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "csfloat_price_model.joblib").write_bytes(b"model")
    (model_dir / "csfloat_price_model_metrics.json").write_text(
        json.dumps({"trained_at": "2026-08-01T00:00:00+00:00", "rows": 100}),
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sales (fetched_at TEXT)")

    result = dashboard_server.build_model_freshness(conn, 50, project_dir=tmp_path)
    almaz = next(model for model in result["models"] if model["name"] == "almaz")

    assert almaz["freshness"] == "data_mismatch"
    assert almaz["row_count_mismatch"] is True
    assert almaz["new_rows_since_training"] is None
    assert "data_mismatch: ['bad', 'data mismatch']" in dashboard_server.HTML


def test_model_freshness_marks_mtime_row_estimate_as_estimated(tmp_path):
    artifact_dir = tmp_path / "IGORMODEL" / "artifacts"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "price_model_point.cbm").write_bytes(b"model")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sales (fetched_at TEXT)")
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.executemany("INSERT INTO sales (fetched_at) VALUES (?)", [(old,), (old,)])

    result = dashboard_server.build_model_freshness(conn, 3, project_dir=tmp_path)
    igor = next(model for model in result["models"] if model["name"] == "igor")

    assert igor["training_rows"] == 2
    assert igor["training_rows_estimated"] is True
    assert igor["freshness"] == "estimated"
    assert "estimated: ['warn', 'freshness estimated']" in dashboard_server.HTML


def test_model_freshness_keeps_knife_rows_out_of_weapon_models(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE sales "
        "(name TEXT, state TEXT, price_cents INTEGER, fetched_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO sales VALUES (?, 'sold', 1000, ?)",
        [
            ("AK-47 | Redline (Field-Tested)", "2026-08-01T00:00:00+00:00"),
            ("M4A4 | Asiimov (Field-Tested)", "2026-08-01T00:00:00+00:00"),
            ("★ Karambit | Doppler (Factory New)", "2026-08-01T00:00:00+00:00"),
            ("★ Butterfly Knife | Fade (Factory New)", "2026-08-01T00:00:00+00:00"),
            ("★ Sport Gloves | Vice (Field-Tested)", "2026-08-01T00:00:00+00:00"),
        ],
    )

    result = dashboard_server.build_model_freshness(
        conn, 5, project_dir=tmp_path
    )
    by_name = {model["name"]: model for model in result["models"]}

    assert result["current_rows_by_item_kind"] == {
        "weapon_skin": 2,
        "knife": 2,
        "glove": 1,
    }
    assert result["current_rows_scope"] == "indexed_starred_item_partition"
    assert by_name["almaz"]["current_rows"] == 2
    assert by_name["igor"]["current_rows"] == 2
    assert by_name["nova"]["current_rows"] == 2
    assert by_name["vega"]["current_rows"] == 2
    assert by_name["knives"]["current_rows"] == 2
    assert by_name["knives"]["item_kind"] == "knife"


def test_stale_deal_predictions_expire_every_non_expired_verdict():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    deal_scanner.init_db(conn)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    stale_verdicts = ["good", "watch", "thin", "bad", "filtered", "error", None]
    for index, verdict in enumerate(stale_verdicts):
        prediction_row(
            conn,
            "nova",
            10,
            100,
            listing_id=f"stale-{index}",
            verdict=verdict,
            last_seen_at=old,
        )
    prediction_row(conn, "nova", 10, 100, listing_id="fresh", verdict="bad")
    prediction_row(
        conn,
        "nova",
        10,
        100,
        listing_id="already-expired",
        verdict="expired",
        last_seen_at=old,
    )

    expired = deal_scanner.expire_stale_predictions(conn, 1)
    verdicts = dict(
        conn.execute("SELECT listing_id, verdict FROM model_deal_predictions")
    )

    assert expired == len(stale_verdicts)
    assert all(verdicts[f"stale-{index}"] == "expired" for index in range(len(stale_verdicts)))
    assert verdicts["fresh"] == "bad"
    assert verdicts["already-expired"] == "expired"


def test_parser_dashboard_separates_key_and_proxy_cooldowns():
    source = dashboard_server.HTML

    assert "key.transport_blocked_seconds_left" in source
    assert "key.blocked_reason === 'authentication'" in source
    assert "proxy cooling" in source
    assert "`${seconds(transportSeconds)} · proxy`" in source
