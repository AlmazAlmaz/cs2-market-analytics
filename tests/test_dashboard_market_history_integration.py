import http.client
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import account_backend
import dashboard_server


def _request(port, path, *, cookie=""):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Cookie": cookie} if cookie else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        if "application/json" in response.getheader("Content-Type", ""):
            body = json.loads(body)
        return response.status, body
    finally:
        connection.close()


@contextmanager
def _server(monkeypatch, tmp_path):
    service = account_backend.AccountService(tmp_path / "accounts.db")
    user = service.create_user(
        "history-member@example.com",
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


def test_market_history_is_reachable_once_from_localized_market_workspace():
    for locale, label in (("ru", "Источники цен ↗"), ("en", "Price sources ↗")):
        document = dashboard_server.catalog_browser.render_catalog_page(locale)
        assert document.count('href="/market-history"') == 1
        assert 'data-i18n="historyWorkspace"' in document
        assert f"historyWorkspace:'{label}'" in document


def test_market_history_wrapper_forwards_only_bounded_query_fields(monkeypatch):
    captured = {}

    def fake_builder(path, **kwargs):
        captured.update(path=path, **kwargs)
        return {"status": "ready", "rows": []}

    monkeypatch.setattr(
        dashboard_server.market_history_dashboard,
        "build_market_history_payload",
        fake_builder,
    )
    monkeypatch.setattr(
        dashboard_server, "MARKET_HISTORY_DB_PATH", "/private/history.db"
    )

    payload = dashboard_server.build_market_history_dashboard_payload(
        kind="listing_snapshot",
        search="AK-47",
        marketplace="buff163",
        provider="cspriceapi",
        days="30",
        limit="25",
        offset="50",
    )

    assert payload == {"status": "ready", "rows": []}
    assert captured == {
        "path": "/private/history.db",
        "kind": "listing_snapshot",
        "search": "AK-47",
        "marketplace": "buff163",
        "provider": "cspriceapi",
        "days": "30",
        "limit": "25",
        "offset": "50",
    }


def test_market_history_page_and_api_are_member_safe(monkeypatch, tmp_path):
    expected = {
        "status": "ready",
        "kind": "confirmed_sale",
        "rows": [],
        "series": [],
    }
    monkeypatch.setattr(
        dashboard_server,
        "build_market_history_dashboard_payload",
        lambda **_kwargs: expected,
    )
    with _server(monkeypatch, tmp_path) as (port, cookie):
        status, payload = _request(port, "/api/market-history")
        assert status == 401
        assert payload["error"] == "unauthorized"

        status, html = _request(
            port,
            "/market-history",
            cookie=f"{cookie}; cs2_locale=en",
        )
        assert status == 200
        assert '<html lang="en">' in html
        assert "Confirmed sales" in html

        status, payload = _request(
            port,
            "/api/market-history?kind=confirmed_sale&search=AK-47",
            cookie=cookie,
        )
        assert status == 200
        assert payload == expected
