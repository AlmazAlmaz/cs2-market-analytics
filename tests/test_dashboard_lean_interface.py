import http.client
import io
import json
import re
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

import account_backend
import dashboard_server
import neural_model_runtime

PRIMARY_PATHS = ("/deals", "/deal", "/strategy", "/catalog", "/account")
MARKET_SUBPAGES = ("/knives", "/market-history")


def _lean_nav(document):
    match = re.search(
        r'<nav\b(?=[^>]*\blean-nav\b)[^>]*>(.*?)</nav>',
        document,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def _assert_unified_navigation(document, *, include_system=False):
    nav = _lean_nav(document)
    assert document.count('class="site-nav lean-nav"') == 1
    for path in PRIMARY_PATHS:
        assert nav.count(f'href="{path}"') == 1
        assert nav.count(f'data-nav-path="{path}"') == 1
    for redundant_path in ("/neural", *MARKET_SUBPAGES):
        assert f'href="{redundant_path}"' not in nav
        assert f'data-nav-path="{redundant_path}"' not in nav
    assert nav.count('class="lean-more"') == (1 if include_system else 0)
    assert nav.count('href="/dashboard"') == (1 if include_system else 0)


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_private_templates_share_one_lean_navigation_without_neural(locale):
    templates = (
        (dashboard_server.HTML, True),
        (dashboard_server.DEAL_HTML, False),
        (dashboard_server.DEALS_HTML, False),
        (dashboard_server.STRATEGY_HTML, False),
    )
    for template, include_system in templates:
        rendered = dashboard_server.render_private_html(template, locale)
        _assert_unified_navigation(rendered, include_system=include_system)
        assert 'id="cs2-lean-interface"' in rendered
        assert 'id="cs2-lean-interface-script"' in rendered


class _CaptureHandler:
    def __init__(self, locale="en"):
        self.headers = {"Cookie": f"cs2_locale={locale}"}
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        pass


@pytest.mark.parametrize(
    "response_wrapper",
    [
        dashboard_server.account_html_response,
        dashboard_server.knives_html_response,
        dashboard_server.catalog_html_response,
        dashboard_server.market_history_html_response,
    ],
)
def test_standalone_private_response_wrappers_apply_unified_navigation(
    response_wrapper,
):
    handler = _CaptureHandler()

    response_wrapper(handler)

    assert handler.status == 200
    document = handler.wfile.getvalue().decode("utf-8")
    _assert_unified_navigation(document)
    assert 'id="cs2-lean-interface"' in document
    assert 'id="cs2-lean-interface-script"' in document


def _request(port, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        content_type = response.getheader("Content-Type", "")
        payload = json.loads(raw) if raw and "application/json" in content_type else raw
        return response.status, payload, dict(response.getheaders())
    finally:
        connection.close()


@contextmanager
def _member_server(monkeypatch, tmp_path):
    service = account_backend.AccountService(tmp_path / "lean-accounts.db")
    user = service.create_user(
        "lean-member@example.com",
        "correct horse battery staple",
        role="member",
        access_state="approved",
    )
    session = service.create_session(user["id"])
    monkeypatch.setattr(dashboard_server, "ACCOUNT_SERVICE", service)
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cookie = (
            f"{dashboard_server.ACCOUNT_SESSION_COOKIE}="
            f"{session['session_token']}"
        )
        yield server.server_address[1], cookie
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        service.close()


def test_member_root_and_legacy_neural_route_redirect_to_canonical_pages(
    monkeypatch, tmp_path
):
    with _member_server(monkeypatch, tmp_path) as (port, cookie):
        status, payload, headers = _request(port, "GET", "/neural")
        assert status == 401
        assert payload["error"] == "unauthorized"
        assert "Location" not in headers

        member_headers = {"Cookie": cookie}
        status, body, headers = _request(
            port, "GET", "/neural", headers=member_headers
        )
        assert status == 303
        assert body == ""
        assert headers["Location"] == "/deal"
        assert headers["Cache-Control"] == "no-store"

        status, body, headers = _request(port, "GET", "/", headers=member_headers)
        assert status == 303
        assert body == ""
        assert headers["Location"] == "/deals"

        status, document, _headers = _request(
            port, "GET", "/deal", headers=member_headers
        )
        assert status == 200
        _assert_unified_navigation(document)
        assert 'id="modelComparisonGrid"' in document


def test_nova_api_and_deal_comparison_remain_available(monkeypatch, tmp_path):
    expected_prediction = {
        "model_name": "nova",
        "predicted_price_cents": 1234,
        "verdict": "watch",
    }
    expected_status = {"available": True, "model_name": "nova"}
    monkeypatch.setattr(
        dashboard_server,
        "build_predict_neural_payload",
        lambda payload: {**expected_prediction, "received": payload},
    )
    monkeypatch.setattr(neural_model_runtime, "model_status", lambda: expected_status)

    with _member_server(monkeypatch, tmp_path) as (port, cookie):
        body = json.dumps({"name": "AK-47 | Slate (Field-Tested)"})
        status, payload, _headers = _request(
            port,
            "POST",
            "/api/predict-neural",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        assert status == 401
        assert payload["error"] == "unauthorized"

        auth_headers = {"Cookie": cookie, "Content-Type": "application/json"}
        status, payload, _headers = _request(
            port,
            "POST",
            "/api/predict-neural",
            body=body,
            headers=auth_headers,
        )
        assert status == 200
        assert payload == {
            **expected_prediction,
            "received": {"name": "AK-47 | Slate (Field-Tested)"},
        }

        status, payload, _headers = _request(
            port,
            "GET",
            "/api/neural-status",
            headers={"Cookie": cookie},
        )
        assert status == 200
        assert payload == expected_status

    rendered = dashboard_server.render_private_html(dashboard_server.DEAL_HTML, "en")
    assert 'id="modelComparisonGrid"' in rendered
    assert "['almaz', 'igor', 'nova', 'vega']" in rendered
    assert "Vega (advisory)" in rendered
    assert "/api/predict-deal?" in rendered
    assert 'href="/neural"' not in _lean_nav(rendered)
