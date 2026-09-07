import http.client
import json
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import dashboard_server


def _job(model, *, state="idle", job_id=None):
    return {
        "job_id": job_id,
        "model": model,
        "state": state,
        "phase": state,
        "progress_percent": 0 if state == "idle" else 42.5,
        "eta_seconds": None if state == "idle" else 120,
        "error": None,
        "can_cancel": state in {"starting", "running", "cancelling"},
        "candidate_ready_for_review": state == "succeeded",
        "promotion_allowed": False,
        "log_tail": ["safe log line"] if state != "idle" else [],
    }


def _gate(model, *, eligible=True):
    return {
        "model": model,
        "eligible": eligible,
        "reasons": [] if eligible else ["not_enough_new_rows"],
        "trained_rows": 100_000,
        "current_rows": 130_000,
        "new_rows": 30_000,
        "growth_percent": 30.0,
        "runtime_valid_new_rows": 8_000,
        "trainer_available": True,
        "thresholds": {
            "min_new_rows": 25_000,
            "min_growth_percent": 5.0,
            "min_runtime_valid_new_rows": 5_000,
        },
    }


def _api_payload():
    return {
        "schema_version": 1,
        "updated_at": "2026-08-11T10:00:00+00:00",
        "server_time": "2026-08-11T10:00:00+00:00",
        "active_job_id": None,
        "training_active": False,
        "models": {
            model: {**_job(model), "eligibility": _gate(model)}
            for model in dashboard_server.model_training_jobs.MODEL_NAMES
        },
        "promotion_policy": "quality_gate_then_next_offline_start",
        "activation_available": True,
    }


def test_training_payload_merges_public_jobs_and_cached_eligibility(monkeypatch):
    overview_calls = []
    eligibility_calls = []

    def overview():
        overview_calls.append(True)
        return {
            "schema_version": 1,
            "updated_at": "2026-08-11T10:00:00+00:00",
            "active_job_id": "a" * 32,
            "training_active": True,
            "models": {
                "almaz": _job("almaz", state="running", job_id="a" * 32),
                "igor": _job("igor"),
                "nova": _job("nova"),
                "knives": _job("knives"),
                "gloves": _job("gloves"),
            },
            "promotion_policy": "quality_gate_then_next_offline_start",
        }

    def eligibility():
        eligibility_calls.append(True)
        return {
            model: _gate(model)
            for model in dashboard_server.model_training_jobs.MODEL_NAMES
        }

    monkeypatch.setattr(
        dashboard_server.model_training_jobs, "get_training_overview", overview
    )
    monkeypatch.setattr(
        dashboard_server.model_training_jobs,
        "get_all_training_eligibility",
        eligibility,
    )
    dashboard_server.MODEL_TRAINING_ELIGIBILITY_CACHE.update(
        {"loaded_monotonic": 0.0, "value": None, "refreshing": False}
    )

    first = dashboard_server.build_model_training_payload(force_eligibility=True)
    second = dashboard_server.build_model_training_payload()

    assert len(overview_calls) == 2
    assert len(eligibility_calls) == 1
    assert first["models"]["almaz"]["state"] == "running"
    assert first["models"]["nova"]["eligibility"]["eligible"] is True
    assert first["activation_available"] is True
    assert second["promotion_policy"] == "quality_gate_then_next_offline_start"
    assert "worker_pid" not in json.dumps(first)


def test_empty_eligibility_cache_returns_fail_closed_pending_without_blocking(
    monkeypatch,
):
    started = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            started.append(self.name)

    monkeypatch.setattr(dashboard_server.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        dashboard_server.model_training_jobs,
        "get_all_training_eligibility",
        lambda: pytest.fail("eligibility must run only in the background thread"),
    )
    dashboard_server.MODEL_TRAINING_ELIGIBILITY_CACHE.update(
        {"loaded_monotonic": 0.0, "value": None, "refreshing": False}
    )

    result = dashboard_server._cached_model_training_eligibility()

    assert started == ["model-training-eligibility"]
    assert all(item["eligible"] is False for item in result.values())
    assert all(item["reasons"] == ["eligibility_refreshing"] for item in result.values())
    assert result["knives"]["bootstrap"] is True
    assert result["knives"]["item_kind"] == "knife"
    assert result["knives"]["thresholds"] == {
        "min_new_rows": (
            dashboard_server.model_training_jobs.KNIFE_BOOTSTRAP_MIN_ROWS
        ),
        "min_growth_percent": (
            dashboard_server.model_training_jobs.MIN_DATA_GROWTH_PERCENT
        ),
        "min_runtime_valid_new_rows": (
            dashboard_server.model_training_jobs.KNIFE_BOOTSTRAP_MIN_RUNTIME_VALID_ROWS
        ),
    }


def test_training_action_dispatch_is_whitelisted_and_never_accepts_paths(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        dashboard_server.model_training_jobs,
        "start_model_training",
        lambda model: calls.append(("start", model)),
    )
    monkeypatch.setattr(
        dashboard_server.model_training_jobs,
        "cancel_model_training",
        lambda model, *, job_id: calls.append(("cancel", model, job_id)),
    )
    monkeypatch.setattr(
        dashboard_server, "build_model_training_payload", lambda: _api_payload()
    )

    dashboard_server.update_model_training_from_payload(
        {"action": "start", "model": "igor"}
    )
    dashboard_server.update_model_training_from_payload(
        {"action": "cancel", "model": "nova", "job_id": "b" * 32}
    )
    dashboard_server.update_model_training_from_payload(
        {"action": "start", "model": "knives"}
    )

    assert calls == [
        ("start", "igor"),
        ("cancel", "nova", "b" * 32),
        ("start", "knives"),
    ]
    rejected = (
        {"action": "activate", "model": "almaz"},
        {"action": "start", "model": "almaz", "candidate_dir": "/tmp/evil"},
        {"action": "start", "model": "unknown"},
        {"action": "cancel", "model": "nova", "job_id": "B" * 32},
    )
    for payload in rejected:
        with pytest.raises(ValueError):
            dashboard_server.update_model_training_from_payload(payload)
    assert calls == [
        ("start", "igor"),
        ("cancel", "nova", "b" * 32),
        ("start", "knives"),
    ]


def test_training_request_security_checks_content_type_csrf_and_origin():
    base = {
        "Content-Type": "application/json; charset=utf-8",
        "X-CS2-Action": "model-training",
        "Sec-Fetch-Site": "same-origin",
        "Host": "dashboard.example:8010",
        "Origin": "https://dashboard.example:8010",
    }
    assert (
        dashboard_server.model_training_request_violation(
            SimpleNamespace(headers=base)
        )
        is None
    )

    cases = (
        ({**base, "Content-Type": "text/plain"}, 415),
        ({**base, "X-CS2-Action": "scanner-control"}, 403),
        ({**base, "Sec-Fetch-Site": "cross-site"}, 403),
        ({**base, "Origin": "https://evil.example"}, 403),
        ({**base, "Origin": "null"}, 403),
        ({**base, "Transfer-Encoding": "chunked"}, 400),
    )
    for headers, expected_status in cases:
        status, _message = dashboard_server.model_training_request_violation(
            SimpleNamespace(headers=headers)
        )
        assert status == expected_status


@contextmanager
def _dashboard_http_server(monkeypatch):
    updates = []
    monkeypatch.setattr(
        dashboard_server, "build_model_training_payload", lambda: _api_payload()
    )

    def update(payload):
        updates.append(payload)
        return _api_payload()

    monkeypatch.setattr(dashboard_server, "update_model_training_from_payload", update)
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], updates
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload
    finally:
        connection.close()


def test_model_training_http_routes_require_auth_and_reject_cross_origin(
    monkeypatch,
):
    with _dashboard_http_server(monkeypatch) as (port, updates):
        status, _payload = _request(port, "GET", "/api/model-training")
        assert status == 401

        auth = {"X-Dashboard-Token": dashboard_server.DASHBOARD_TOKEN}
        status, payload = _request(
            port, "GET", "/api/model-training", headers=auth
        )
        assert status == 200
        assert set(payload["models"]) == {
            "almaz",
            "igor",
            "nova",
            "knives",
            "gloves",
        }

        body = json.dumps({"action": "start", "model": "almaz"})
        safe_headers = {
            **auth,
            "Content-Type": "application/json",
            "X-CS2-Action": "model-training",
            "Sec-Fetch-Site": "same-origin",
            "Origin": f"http://127.0.0.1:{port}",
        }
        status, payload = _request(
            port, "POST", "/api/model-training", body=body, headers=safe_headers
        )
        assert status == 200
        assert payload["activation_available"] is True
        assert updates == [{"action": "start", "model": "almaz"}]

        status, _payload = _request(
            port,
            "POST",
            "/api/model-training",
            body=body,
            headers={**safe_headers, "Origin": "https://evil.example"},
        )
        assert status == 403
        assert updates == [{"action": "start", "model": "almaz"}]


def test_training_ui_is_keyed_accessible_responsive_and_does_not_offer_activation():
    template = dashboard_server.HTML
    rendered = dashboard_server.render_private_html(template)

    assert "function ensureModelHealthCard(modelName)" in template
    assert "data-model-name" in template
    assert "document.createElement('article')" in template
    assert ".innerHTML = cards" not in template
    assert 'role="progressbar"' in template
    assert 'aria-live="polite"' in template
    assert "Переобучить" in template
    assert "Последние строки обучения" in template
    assert "test_raw_interval_coverage_15_85" not in template
    assert "MODEL_TRAINING_ACTIVE_STATES" in template
    assert "TRAINABLE_MODEL_NAMES" in template
    assert "validation_failed: 'ошибка проверки целостности'" in template
    assert "rejectionKind === 'integrity'" in template
    assert "не подтверждена целостность/SHA-256" in template
    assert "document.hidden ? 10000 : (active ? 1000 : 10000)" in template
    assert "scheduleModelTrainingPoll(10000)" in template
    assert "'X-CS2-Action': 'model-training'" in template
    assert template.count("/api/model-training") >= 2
    assert ".model-training-actions { align-items: stretch; flex-direction: column; }" in template
    assert "data-training-action=\"activate\"" not in template
    assert dashboard_server.DASHBOARD_TOKEN not in rendered


def test_rendered_dashboard_inline_javascript_passes_node_syntax_check(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    rendered = dashboard_server.render_private_html(dashboard_server.HTML).replace(
        "__LIVE_REFRESH_MS__", "10000"
    )
    scripts = re.findall(
        r"<script(?:\s[^>]*)?>(.*?)</script>", rendered, flags=re.IGNORECASE | re.DOTALL
    )
    assert scripts
    script_path = tmp_path / "dashboard-inline.js"
    script_path.write_text("\n".join(scripts), encoding="utf-8")

    completed = subprocess.run(
        [node, "--check", str(script_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
