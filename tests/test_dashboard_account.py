import http.client
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

import account_backend
import csfloat_listing_fetcher
import dashboard_server


def test_public_account_config_requires_https_and_secure_cookies(monkeypatch):
    monkeypatch.setattr(
        dashboard_server, "ACCOUNT_PUBLIC_URL", "http://accounts.example.com"
    )
    monkeypatch.setattr(dashboard_server, "ACCOUNT_COOKIE_SECURE", False)
    with pytest.raises(RuntimeError, match="HTTPS"):
        dashboard_server.validate_account_runtime_config()

    monkeypatch.setattr(
        dashboard_server, "ACCOUNT_PUBLIC_URL", "https://accounts.example.com"
    )
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        dashboard_server.validate_account_runtime_config()

    monkeypatch.setattr(dashboard_server, "ACCOUNT_COOKIE_SECURE", True)
    dashboard_server.validate_account_runtime_config()


def _create_trend_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE trend_daily_market (
                market_hash_name TEXT NOT NULL,
                day TEXT NOT NULL,
                sales_count INTEGER NOT NULL,
                median_price_cents REAL NOT NULL,
                mean_price_cents REAL NOT NULL,
                min_price_cents INTEGER NOT NULL,
                max_price_cents INTEGER NOT NULL,
                p15_price_cents REAL NOT NULL,
                p85_price_cents REAL NOT NULL,
                notional_cents INTEGER NOT NULL,
                source_max_fetched_at TEXT,
                refreshed_at TEXT NOT NULL,
                PRIMARY KEY (market_hash_name, day)
            );
            CREATE TABLE trend_skin_catalog (
                market_hash_name TEXT PRIMARY KEY,
                collection_name TEXT NOT NULL,
                observed_sales_count INTEGER NOT NULL,
                unique_asset_count INTEGER NOT NULL,
                min_float REAL,
                avg_float REAL,
                max_float REAL,
                latest_reference_quantity INTEGER,
                latest_reference_at TEXT,
                icon_url TEXT,
                mapping_coverage REAL NOT NULL,
                first_observed_day TEXT NOT NULL,
                last_observed_day TEXT NOT NULL,
                refreshed_at TEXT NOT NULL
            );
            INSERT INTO trend_daily_market VALUES
                ('AK-47 | Slate (Field-Tested)', '2026-08-13', 4, 700, 710, 650,
                 800, 680, 750, 2840, '2026-08-13T12:00:00+00:00',
                 '2026-08-14T00:00:00+00:00');
            INSERT INTO trend_skin_catalog VALUES
                ('AK-47 | Slate (Field-Tested)', 'The Snakebite Collection', 400,
                 350, 0.15, 0.25, 0.37, 1200, '2026-08-13T12:00:00+00:00',
                 'https://example.test/slate.png', 1.0, '2025-01-01',
                 '2026-08-13', '2026-08-14T00:00:00+00:00');
            """
        )


@contextmanager
def _account_server(monkeypatch, tmp_path, *, live_pricing=False):
    trend_path = tmp_path / "trend.db"
    _create_trend_db(trend_path)
    sender = account_backend.InMemoryPasswordResetSender()
    service = account_backend.AccountService(
        ":memory:",
        password_reset_sender=sender,
        reset_base_url="http://localhost:8010/reset-password",
    )
    monkeypatch.setattr(dashboard_server, "ACCOUNT_SERVICE", service)
    monkeypatch.setattr(dashboard_server, "ACCOUNT_COOKIE_SECURE", False)
    monkeypatch.setattr(dashboard_server, "TREND_DB_PATH", str(trend_path))
    monkeypatch.setattr(
        dashboard_server, "ACCOUNT_LIVE_PRICING_ENABLED", live_pricing
    )
    with dashboard_server._ACCOUNT_PRICE_SNAPSHOT_LOCK:
        previous_price_cache = dict(dashboard_server._ACCOUNT_PRICE_SNAPSHOT_CACHE)
        dashboard_server._ACCOUNT_PRICE_SNAPSHOT_CACHE.update(
            {"expires_at": 0.0, "payload": None}
        )
    dashboard_server.AUTH_RATE_BUCKETS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], service, sender
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        service.close()
        with dashboard_server._ACCOUNT_PRICE_SNAPSHOT_LOCK:
            dashboard_server._ACCOUNT_PRICE_SNAPSHOT_CACHE.clear()
            dashboard_server._ACCOUNT_PRICE_SNAPSHOT_CACHE.update(
                previous_price_cache
            )


def _request(port, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        content_type = response.getheader("Content-Type", "")
        payload = json.loads(raw) if raw and "application/json" in content_type else raw
        return response.status, payload, response.getheaders()
    finally:
        connection.close()


def _request_raw(port, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), response.getheaders()
    finally:
        connection.close()


def _auth_headers(port, action):
    return {
        "Content-Type": "application/json",
        "X-CS2-Action": action,
        "Sec-Fetch-Site": "same-origin",
        "Origin": f"http://127.0.0.1:{port}",
    }


def _cookie_jar(headers):
    jar = SimpleCookie()
    for name, value in headers:
        if name.casefold() == "set-cookie":
            jar.load(value)
    return jar


def _cookie_header(jar):
    return "; ".join(f"{name}={morsel.value}" for name, morsel in jar.items())


def _register(port, email, password="correct horse battery staple"):
    status, payload, headers = _request(
        port,
        "POST",
        "/api/auth/register",
        body=json.dumps(
            {"email": email, "password": password, "display_name": "Trader"}
        ),
        headers=_auth_headers(port, "auth-register"),
    )
    assert status == 201, payload
    assert payload["approval_required"] is True
    assert payload["user"]["access_state"] == "pending"
    assert dashboard_server.ACCOUNT_SESSION_COOKIE not in _cookie_jar(headers)
    dashboard_server.ACCOUNT_SERVICE.admin_approve_user(
        payload["user"]["id"],
        trusted_local_admin=True,
        expected_revision=payload["user"]["revision"],
    )
    status, login_payload, headers = _request(
        port,
        "POST",
        "/api/auth/login",
        body=json.dumps({"email": email, "password": password}),
        headers=_auth_headers(port, "auth-login"),
    )
    assert status == 200, login_payload
    jar = _cookie_jar(headers)
    assert jar[dashboard_server.ACCOUNT_SESSION_COOKIE]["httponly"]
    assert jar[dashboard_server.ACCOUNT_SESSION_COOKIE]["samesite"] == "Strict"
    assert not jar[dashboard_server.ACCOUNT_CSRF_COOKIE]["httponly"]
    return jar


def _account_headers(port, jar, *, include_csrf=True):
    headers = {
        **_auth_headers(port, "account"),
        "Cookie": _cookie_header(jar),
    }
    if include_csrf:
        headers["X-CSRF-Token"] = jar[dashboard_server.ACCOUNT_CSRF_COOKIE].value
    return headers


def _owner_headers(port, *, json_action=False):
    headers = {"X-Dashboard-Token": dashboard_server.DASHBOARD_TOKEN}
    if json_action:
        headers.update(_auth_headers(port, "admin-user-management"))
        headers["X-CS2-Action"] = "admin-user-management"
    return headers


def test_public_registration_waits_for_local_owner_approval(monkeypatch, tmp_path):
    with _account_server(monkeypatch, tmp_path) as (port, service, _sender):
        email = "pending@example.com"
        password = "correct horse battery staple"
        registration_headers = {
            **_auth_headers(port, "auth-register"),
            "X-Real-IP": "203.0.113.24",
            "User-Agent": "Approval test browser",
        }
        status, payload, response_headers = _request(
            port,
            "POST",
            "/api/auth/register",
            body=json.dumps(
                {"email": email, "password": password, "display_name": "Pending"}
            ),
            headers=registration_headers,
        )
        assert status == 201, payload
        assert payload["approval_required"] is True
        assert dashboard_server.ACCOUNT_SESSION_COOKIE not in _cookie_jar(
            response_headers
        )

        status, payload, _ = _request(
            port,
            "POST",
            "/api/auth/login",
            body=json.dumps({"email": email, "password": password}),
            headers=_auth_headers(port, "auth-login"),
        )
        assert status == 403
        assert payload["error_code"] == "access_pending"

        status, page, _ = _request(
            port, "GET", "/admin/users", headers=_owner_headers(port)
        )
        assert status == 200
        assert "Доступ пользователей" in page

        status, owner_workspace, _ = _request(
            port, "GET", "/deals", headers=_owner_headers(port)
        )
        assert status == 200
        assert 'href="/admin/users"' in owner_workspace

        web_admin = service.create_user(
            "web-admin@example.com",
            "correct horse battery staple",
            role="admin",
        )
        web_admin_session = service.create_session(web_admin["id"])
        web_admin_cookie = {
            "Cookie": (
                f"{dashboard_server.ACCOUNT_SESSION_COOKIE}="
                f"{web_admin_session['session_token']}"
            )
        }
        status, member_workspace, _ = _request(
            port,
            "GET",
            "/deals",
            headers=web_admin_cookie,
        )
        assert status == 200
        assert 'href="/admin/users"' not in member_workspace
        status, denied_payload, _ = _request(
            port,
            "GET",
            "/api/admin/users",
            headers=web_admin_cookie,
        )
        assert status == 401
        assert denied_payload["error_code"] == "owner_authorization_required"

        status, legacy_attempt, legacy_headers = _request(
            port,
            "POST",
            "/api/auth/register",
            body=json.dumps(
                {
                    "email": "legacy-admin-attempt@example.com",
                    "password": password,
                    "display_name": "Still member",
                }
            ),
            headers={
                **_auth_headers(port, "auth-register"),
                "X-Dashboard-Token": dashboard_server.DASHBOARD_TOKEN,
            },
        )
        assert status == 201, legacy_attempt
        assert legacy_attempt["user"]["role"] == "member"
        assert legacy_attempt["user"]["access_state"] == "pending"
        assert dashboard_server.ACCOUNT_SESSION_COOKIE not in _cookie_jar(
            legacy_headers
        )

        status, users_payload, _ = _request(
            port, "GET", "/api/admin/users", headers=_owner_headers(port)
        )
        assert status == 200, users_payload
        pending = next(
            user for user in users_payload["users"] if user["email"] == email
        )
        assert pending["access_state"] == "pending"
        assert pending["registration_ip"] == "203.0.113.24"
        assert "password_hash" not in json.dumps(users_payload)
        assert "token_hash" not in json.dumps(users_payload)

        status, rejected_role, _ = _request(
            port,
            "POST",
            "/api/admin/users",
            body=json.dumps(
                {
                    "action": "grant_admin",
                    "user_id": pending["id"],
                    "expected_revision": pending["revision"],
                }
            ),
            headers=_owner_headers(port, json_action=True),
        )
        assert status == 409
        assert rejected_role["error_code"] == "access_not_approved"

        status, public_payload, _ = _request(
            port,
            "GET",
            "/api/admin/users",
            headers={
                **_owner_headers(port),
                "Host": "csskinsprediction.online",
                "X-Real-IP": "198.51.100.8",
                "X-Forwarded-For": "198.51.100.8",
                "X-Forwarded-Proto": "https",
            },
        )
        assert status == 404
        assert public_payload["error_code"] == "local_only"

        status, approved, _ = _request(
            port,
            "POST",
            "/api/admin/users",
            body=json.dumps(
                {
                    "action": "approve",
                    "user_id": pending["id"],
                    "expected_revision": pending["revision"],
                }
            ),
            headers=_owner_headers(port, json_action=True),
        )
        assert status == 200, approved
        assert approved["user"]["access_state"] == "approved"

        status, _payload, login_headers = _request(
            port,
            "POST",
            "/api/auth/login",
            body=json.dumps({"email": email, "password": password}),
            headers=_auth_headers(port, "auth-login"),
        )
        assert status == 200
        jar = _cookie_jar(login_headers)
        status, _page, _ = _request(
            port,
            "GET",
            "/deals",
            headers={"Cookie": _cookie_header(jar)},
        )
        assert status == 200

        status, blocked, _ = _request(
            port,
            "POST",
            "/api/admin/users",
            body=json.dumps(
                {
                    "action": "block",
                    "user_id": pending["id"],
                    "expected_revision": approved["user"]["revision"],
                }
            ),
            headers=_owner_headers(port, json_action=True),
        )
        assert status == 200, blocked
        status, _payload, _ = _request(
            port,
            "GET",
            "/deals",
            headers={"Cookie": _cookie_header(jar)},
        )
        assert status == 401


def test_local_owner_can_reset_and_soft_delete_account(monkeypatch, tmp_path):
    with _account_server(monkeypatch, tmp_path) as (port, _service, sender):
        email = "managed@example.com"
        old_password = "correct horse battery staple"
        jar = _register(port, email, old_password)
        status, listing, _ = _request(
            port, "GET", "/api/admin/users", headers=_owner_headers(port)
        )
        assert status == 200
        user = next(item for item in listing["users"] if item["email"] == email)

        status, reset_payload, _ = _request(
            port,
            "POST",
            "/api/admin/users",
            body=json.dumps(
                {
                    "action": "send_password_reset",
                    "user_id": user["id"],
                    "expected_revision": user["revision"],
                }
            ),
            headers=_owner_headers(port, json_action=True),
        )
        assert status == 200, reset_payload
        assert reset_payload["delivery_state"] == "sent"
        assert len(sender.messages) == 1
        assert "reset_url" not in reset_payload
        reset_url = sender.messages[0].reset_url
        reset_token = parse_qs(urlparse(reset_url).query)["token"][0]

        status, _payload, _ = _request(
            port,
            "GET",
            "/account",
            headers={"Cookie": _cookie_header(jar)},
        )
        assert status == 303

        new_password = "a different correct horse battery staple"
        status, reset_result, _ = _request(
            port,
            "POST",
            "/api/auth/reset-password",
            body=json.dumps({"token": reset_token, "password": new_password}),
            headers=_auth_headers(port, "auth-reset"),
        )
        assert status == 200, reset_result

        status, listing, _ = _request(
            port, "GET", "/api/admin/users", headers=_owner_headers(port)
        )
        current = next(item for item in listing["users"] if item["email"] == email)
        status, deleted, _ = _request(
            port,
            "POST",
            "/api/admin/users",
            body=json.dumps(
                {
                    "action": "delete",
                    "user_id": current["id"],
                    "expected_revision": current["revision"],
                    "confirmation_email": email,
                }
            ),
            headers=_owner_headers(port, json_action=True),
        )
        assert status == 200, deleted
        assert deleted["user"]["access_state"] == "deleted"
        assert deleted["user"]["registration_ip"] is None

        status, payload, _ = _request(
            port,
            "POST",
            "/api/auth/login",
            body=json.dumps({"email": email, "password": new_password}),
            headers=_auth_headers(port, "auth-login"),
        )
        assert status == 401
        assert payload["error_code"] == "invalid_credentials"

        status, replacement, _ = _request(
            port,
            "POST",
            "/api/auth/register",
            body=json.dumps(
                {"email": email, "password": old_password, "display_name": "New"}
            ),
            headers=_auth_headers(port, "auth-register"),
        )
        assert status == 201, replacement
        assert replacement["approval_required"] is True


def test_local_owner_role_changes_refresh_sessions_and_control_db_download(
    monkeypatch, tmp_path
):
    database_path = tmp_path / "market.db"
    with sqlite3.connect(database_path) as conn:
        conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample VALUES ('safe snapshot')")
    monkeypatch.setattr(dashboard_server, "DB_PATH", str(database_path))

    password = "correct horse battery staple"
    email = "download-admin@example.com"
    with _account_server(monkeypatch, tmp_path) as (port, _service, _sender):
        member_jar = _register(port, email, password)
        member_cookie = {"Cookie": _cookie_header(member_jar)}

        status, denied, _ = _request(
            port, "GET", "/download/db", headers=member_cookie
        )
        assert status == 403
        assert denied["error"] == "admin role is required"

        status, listing, _ = _request(
            port, "GET", "/api/admin/users", headers=_owner_headers(port)
        )
        assert status == 200
        member = next(item for item in listing["users"] if item["email"] == email)
        assert member["role"] == "member"

        status, promoted, _ = _request(
            port,
            "POST",
            "/api/admin/users",
            body=json.dumps(
                {
                    "action": "grant_admin",
                    "user_id": member["id"],
                    "expected_revision": member["revision"],
                }
            ),
            headers=_owner_headers(port, json_action=True),
        )
        assert status == 200, promoted
        assert promoted["user"]["role"] == "admin"
        assert promoted["revoked_sessions"] == 1

        status, _denied, _ = _request(
            port, "GET", "/dashboard", headers=member_cookie
        )
        assert status == 401

        status, login_payload, login_headers = _request(
            port,
            "POST",
            "/api/auth/login",
            body=json.dumps({"email": email, "password": password}),
            headers=_auth_headers(port, "auth-login"),
        )
        assert status == 200, login_payload
        admin_jar = _cookie_jar(login_headers)
        admin_cookie = {"Cookie": _cookie_header(admin_jar)}

        status, dashboard_page, _ = _request(
            port, "GET", "/dashboard", headers=admin_cookie
        )
        assert status == 200
        assert 'id="downloadDbLink"' in dashboard_page
        assert ".page-dashboard #downloadDbLink" not in dashboard_page

        status, snapshot, download_headers = _request_raw(
            port, "GET", "/download/db", headers=admin_cookie
        )
        assert status == 200
        assert snapshot.startswith(b"SQLite format 3\x00")
        assert any(
            name.casefold() == "content-disposition" and "attachment" in value
            for name, value in download_headers
        )

        status, demoted, _ = _request(
            port,
            "POST",
            "/api/admin/users",
            body=json.dumps(
                {
                    "action": "revoke_admin",
                    "user_id": member["id"],
                    "expected_revision": promoted["user"]["revision"],
                }
            ),
            headers=_owner_headers(port, json_action=True),
        )
        assert status == 200, demoted
        assert demoted["user"]["role"] == "member"
        assert demoted["revoked_sessions"] == 1

        status, _denied, _ = _request_raw(
            port, "GET", "/download/db", headers=admin_cookie
        )
        assert status == 401

        status, login_payload, login_headers = _request(
            port,
            "POST",
            "/api/auth/login",
            body=json.dumps({"email": email, "password": password}),
            headers=_auth_headers(port, "auth-login"),
        )
        assert status == 200, login_payload
        member_jar = _cookie_jar(login_headers)
        status, denied, _ = _request(
            port,
            "GET",
            "/download/db",
            headers={"Cookie": _cookie_header(member_jar)},
        )
        assert status == 403
        assert denied["error"] == "admin role is required"


def test_account_http_inventory_pricing_csrf_roles_and_tenant_isolation(
    monkeypatch, tmp_path
):
    with _account_server(monkeypatch, tmp_path) as (port, _service, _sender):
        first = _register(port, "first@example.com")
        cookie = {"Cookie": _cookie_header(first)}

        status, payload, _ = _request(port, "GET", "/api/auth/me", headers=cookie)
        assert status == 200
        assert payload["user"]["email"] == "first@example.com"

        status, page, _ = _request(port, "GET", "/account", headers=cookie)
        assert status == 200
        assert "Мой инвентарь" in page

        status, _payload, _ = _request(port, "GET", "/deals", headers=cookie)
        assert status == 200
        status, payload, _ = _request(port, "GET", "/api/dashboard", headers=cookie)
        assert status == 403
        assert payload["error"] == "admin role is required"
        status, payload, _ = _request(
            port,
            "POST",
            "/api/scanner-control",
            body=json.dumps({"enabled": False, "interval_seconds": 600}),
            headers=_account_headers(port, first),
        )
        assert status == 403
        assert payload["error"] == "admin role is required"

        purchase = {
            "action": "create",
            "skin_name": "AK-47 | Slate (Field-Tested)",
            "buy_price_cents": 500,
            "quantity": 2,
            "bought_at": "2026-08-01",
            "strategy": "flip",
            "fee_percent": 4,
            "listed_price_cents": 800,
            "listed_at": "2026-08-02",
        }
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(purchase),
            headers=_account_headers(port, first, include_csrf=False),
        )
        assert status == 403
        assert payload["error"] == "invalid CSRF token"

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(purchase),
            headers=_account_headers(port, first),
        )
        assert status == 200, payload
        item_id = payload["item"]["id"]

        status, payload, _ = _request(
            port, "GET", "/api/account/inventory", headers=cookie
        )
        assert status == 200
        assert payload["summary"]["invested_cents"] == 1000
        assert payload["summary"]["market_value_cents"] == 1400
        assert payload["items"][0]["pricing"]["benchmark_is_live_ask"] is False
        assert payload["items"][0]["pricing"]["recommended_price_cents"]

        second = _register(port, "second@example.com")
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps({"action": "archive", "item_id": item_id}),
            headers=_account_headers(port, second),
        )
        assert status == 404
        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/inventory",
            headers={"Cookie": _cookie_header(second)},
        )
        assert status == 200
        assert payload["items"] == []


def test_account_csfloat_import_is_authenticated_csrf_safe_and_sanitized(
    monkeypatch, tmp_path
):
    calls = []

    def fake_fetch(text):
        calls.append(text)
        return {
            "ok": True,
            "key_file": "do-not-return-key-file",
            "proxy": "socks5://do-not-return-proxy",
            "raw_attempts": [{"body": "do-not-return-body"}],
            "deal_payload": {
                "listing_id": "12345678",
                "name": "AK-47 | Slate (Field-Tested)",
                "buy_price_cents": 725,
                "float_value": 0.234567,
                "paint_seed": 42,
                "paint_index": 1035,
                "listing_type": "buy_now",
                "item_collection_name": "The Snakebite Collection",
                "item_icon_url": "https://example.test/slate.png",
                "screenshot_url": "https://example.test/screenshot.png",
                "stickers": [
                    {
                        "sticker_id": 101,
                        "name": "Crown (Foil)",
                        "slot": 2,
                        "wear": 0.14,
                        "price_cents": 5_000,
                        "raw_secret": "must-not-pass-through",
                    }
                ],
                "keychains": [],
            },
        }

    monkeypatch.setattr(
        csfloat_listing_fetcher, "fetch_listing_from_text", fake_fetch
    )
    with _account_server(monkeypatch, tmp_path) as (port, _service, _sender):
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/import-csfloat",
            body=json.dumps({"url": "https://csfloat.com/item/12345678"}),
            headers=_auth_headers(port, "account"),
        )
        assert status == 401
        assert calls == []

        jar = _register(port, "import@example.com")
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/import-csfloat",
            body=json.dumps({"url": "https://csfloat.com/item/12345678"}),
            headers=_account_headers(port, jar, include_csrf=False),
        )
        assert status == 403
        assert calls == []

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/import-csfloat",
            body=json.dumps({"url": "https://csfloat.com/item/12345678"}),
            headers=_account_headers(port, jar),
        )
        assert status == 200, payload
        assert calls == ["https://csfloat.com/item/12345678"]
        assert payload == {
            "listing": {
                "listing_id": "12345678",
                "source_url": "https://csfloat.com/item/12345678",
                "skin_name": "AK-47 | Slate (Field-Tested)",
                "buy_price_cents": 725,
                "listing_price_cents": 725,
                "price_kind": "current_listing_ask",
                "price_is_purchase_price": False,
                "source": "csfloat_live",
                "listing_type": "buy_now",
                "float_value": 0.234567,
                "paint_seed": 42,
                "paint_index": 1035,
                "collection_name": "The Snakebite Collection",
                "icon_url": "https://example.test/slate.png",
                "screenshot_url": "https://example.test/screenshot.png",
                "stickers": [
                    {
                        "sticker_id": "101",
                        "name": "Crown (Foil)",
                        "slot": 2,
                        "wear": 0.14,
                        "price_cents": 5_000,
                    }
                ],
                "keychains": [],
                "sold_at": None,
                "observed_at": None,
                "warning_codes": ["verify_purchase_price"],
            }
        }
        encoded = json.dumps(payload)
        assert "do-not-return" not in encoded
        assert "must-not-pass-through" not in encoded

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/import-csfloat",
            body=json.dumps({"url": "https://attacker.example/item/12345678"}),
            headers=_account_headers(port, jar),
        )
        assert status == 400
        assert payload["error_code"] == "invalid_csfloat_url"
        assert calls == ["https://csfloat.com/item/12345678"]


def test_account_csfloat_import_falls_back_to_local_history_for_sold_listing(
    monkeypatch, tmp_path
):
    history_path = tmp_path / "history.db"
    with sqlite3.connect(history_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sales (
                listing_id TEXT PRIMARY KEY,
                name TEXT,
                price_cents INTEGER,
                float_value REAL,
                paint_seed INTEGER,
                paint_index INTEGER,
                item_collection_name TEXT,
                item_icon_url TEXT,
                item_cs2_screenshot_id TEXT,
                sale_type TEXT,
                sold_at TEXT,
                fetched_at TEXT
            );
            CREATE TABLE sale_stickers (
                listing_id TEXT,
                sticker_index INTEGER,
                sticker_id INTEGER,
                slot INTEGER,
                name TEXT,
                icon_url TEXT,
                steam_market_url TEXT,
                wear REAL,
                price_cents INTEGER,
                reference_price_cents INTEGER,
                reference_quantity INTEGER,
                offset_x REAL,
                offset_y REAL,
                PRIMARY KEY (listing_id, sticker_index)
            );
            INSERT INTO sales VALUES (
                '87654321', 'M4A1-S | Printstream (Field-Tested)', 990,
                0.2222, 7, 984, 'The Operation Broken Fang Collection',
                'https://example.test/printstream.png', 'shot-1', 'buy_now',
                '2026-08-20T12:00:00+00:00', '2026-08-20T12:01:00+00:00'
            );
            INSERT INTO sale_stickers VALUES (
                '87654321', 0, 55, 0, 'Test Sticker', NULL, NULL, 0.0,
                125, 125, 10, 0, 0
            );
            """
        )

    def missing_live_listing(_text):
        raise csfloat_listing_fetcher.CsfloatNotFoundError(
            "raw upstream attempts must never reach the response"
        )

    monkeypatch.setattr(dashboard_server, "DB_PATH", str(history_path))
    monkeypatch.setattr(
        csfloat_listing_fetcher,
        "fetch_listing_from_text",
        missing_live_listing,
    )
    with _account_server(monkeypatch, tmp_path) as (port, _service, _sender):
        jar = _register(port, "sold-import@example.com")
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/import-csfloat",
            body=json.dumps({"url": "https://csfloat.com/item/87654321"}),
            headers=_account_headers(port, jar),
        )

        assert status == 200, payload
        listing = payload["listing"]
        assert listing["source"] == "local_history"
        assert listing["price_kind"] == "historical_listing_ask"
        assert listing["price_is_purchase_price"] is False
        assert listing["buy_price_cents"] == 990
        assert listing["skin_name"] == "M4A1-S | Printstream (Field-Tested)"
        assert listing["stickers"][0]["name"] == "Test Sticker"
        assert "historical_price" in listing["warning_codes"]
        assert "raw upstream attempts" not in json.dumps(payload)


def test_account_csfloat_import_maps_upstream_failures_without_leaking_details(
    monkeypatch, tmp_path
):
    with _account_server(monkeypatch, tmp_path) as (port, _service, _sender):
        jar = _register(port, "import-errors@example.com")
        headers = _account_headers(port, jar)

        def rate_limited(_text):
            raise csfloat_listing_fetcher.CsfloatRateLimitError(
                "secret-key proxy-password raw-body"
            )

        monkeypatch.setattr(
            csfloat_listing_fetcher, "fetch_listing_from_text", rate_limited
        )
        status, payload, response_headers = _request(
            port,
            "POST",
            "/api/account/import-csfloat",
            body=json.dumps({"url": "https://csfloat.com/item/12345678"}),
            headers=headers,
        )
        assert status == 429
        assert payload["error_code"] == "csfloat_rate_limited"
        assert payload["retryable"] is True
        assert "secret-key" not in json.dumps(payload)
        assert dict(response_headers)["Retry-After"] == "60"

        def unavailable(_text):
            raise RuntimeError("another-secret raw-upstream-response")

        monkeypatch.setattr(
            csfloat_listing_fetcher, "fetch_listing_from_text", unavailable
        )
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/import-csfloat",
            body=json.dumps({"url": "https://csfloat.com/item/12345678"}),
            headers=headers,
        )
        assert status == 502
        assert payload["error_code"] == "csfloat_unavailable"
        assert "another-secret" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("skin_name", "source_url", "float_value", "stickers"),
    [
        (
            "AK-47 | Slate (Field-Tested)",
            "",
            "",
            "",
        ),
        (
            "M4A1-S | Printstream (Field-Tested)",
            "https://csfloat.com/item/12345678",
            "0.234567",
            [
                {
                    "sticker_id": "101",
                    "name": "Crown (Foil)",
                    "slot": 2,
                    "wear": 0.14,
                    "price_cents": 5_000,
                }
            ],
        ),
    ],
    ids=("manual", "csfloat-imported"),
)
def test_account_inventory_accepts_exact_new_item_form_payload(
    monkeypatch,
    tmp_path,
    skin_name,
    source_url,
    float_value,
    stickers,
):
    """Blank controls emitted by FormData must not turn create into update."""

    with _account_server(monkeypatch, tmp_path) as (port, _service, _sender):
        jar = _register(port, f"{skin_name[:3].lower()}@example.com")

        # This mirrors submitInventory after its dollar-to-cents conversion.
        # FormData includes blank hidden/optional controls as empty strings.
        browser_payload = {
            "action": "create",
            "item_id": "",
            "source_url": source_url,
            "skin_name": skin_name,
            "buy_price_cents": 725,
            "quantity": "1",
            "bought_at": "2026-08-21",
            "strategy": "flip",
            "float_value": float_value,
            "fee_percent": "4",
            "listed_price_cents": None,
            "listed_at": None,
            "stickers": stickers,
            "notes": "",
        }
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(browser_payload),
            headers=_account_headers(port, jar),
        )

        assert status == 200, payload
        item = payload["item"]
        assert item["skin_name"] == skin_name
        assert item["buy_price_cents"] == 725
        assert item["source_url"] == (source_url or None)
        assert item["float_value"] == (float(float_value) if float_value else None)
        assert item["stickers"] == (stickers or [])


def test_password_reset_is_generic_one_time_and_revokes_old_session(
    monkeypatch, tmp_path
):
    old_password = "old correct horse battery staple"
    new_password = "new correct horse battery staple"
    with _account_server(monkeypatch, tmp_path) as (port, _service, sender):
        old_session = _register(port, "reset@example.com", old_password)
        status, payload, _ = _request(
            port,
            "POST",
            "/api/auth/forgot-password",
            body=json.dumps({"email": "reset@example.com"}),
            headers=_auth_headers(port, "auth-forgot"),
        )
        assert status == 202
        forgot_message = "Если аккаунт существует, ссылка отправлена на почту."
        assert payload["message"] == forgot_message
        for _ in range(100):
            if sender.messages:
                break
            time.sleep(0.01)
        assert len(sender.messages) == 1
        token = parse_qs(urlparse(sender.messages[0].reset_url).query)["token"][0]

        status, payload, _ = _request(
            port,
            "POST",
            "/api/auth/reset-password",
            body=json.dumps({"token": token, "password": new_password}),
            headers=_auth_headers(port, "auth-reset"),
        )
        assert status == 200, payload
        status, _payload, _ = _request(
            port,
            "GET",
            "/api/auth/me",
            headers={"Cookie": _cookie_header(old_session)},
        )
        assert status == 401

        status, _payload, _ = _request(
            port,
            "POST",
            "/api/auth/login",
            body=json.dumps({"email": "reset@example.com", "password": old_password}),
            headers=_auth_headers(port, "auth-login"),
        )
        assert status == 401
        status, payload, _ = _request(
            port,
            "POST",
            "/api/auth/login",
            body=json.dumps({"email": "reset@example.com", "password": new_password}),
            headers=_auth_headers(port, "auth-login"),
        )
        assert status == 200, payload

        status, _payload, _ = _request(
            port,
            "POST",
            "/api/auth/reset-password",
            body=json.dumps({"token": token, "password": old_password}),
            headers=_auth_headers(port, "auth-reset"),
        )
        assert status == 401

        status, generic, _ = _request(
            port,
            "POST",
            "/api/auth/forgot-password",
            body=json.dumps({"email": "missing@example.com"}),
            headers=_auth_headers(port, "auth-forgot"),
        )
        assert status == 202
        assert generic["message"] == forgot_message


def test_complete_account_trade_lifecycle_preserves_unit_prices_quantity_and_metadata(
    monkeypatch, tmp_path
):
    old_password = "old correct horse battery staple"
    new_password = "new correct horse battery staple"
    with _account_server(monkeypatch, tmp_path) as (port, _service, _sender):
        jar = _register(port, "lifecycle@example.com", old_password)
        headers = _account_headers(port, jar)
        skin_name = "AK-47 | Slate (Field-Tested)"
        stickers = [
            {
                "name": "Crown (Foil)",
                "slot": 2,
                "wear": 0.14,
                "price_cents": 5_000,
            }
        ]

        # Prices in this contract are positive unit prices.  Missing listing
        # data is represented by null, never by a valid-looking zero price.
        invalid_purchase = {
            "action": "create",
            "skin_name": skin_name,
            "buy_price_cents": 0,
            "quantity": 3,
            "bought_at": "2026-08-01",
        }
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(invalid_purchase),
            headers=headers,
        )
        assert status == 400
        assert "buy_price_cents" in payload["error"]

        purchase = {
            **invalid_purchase,
            "buy_price_cents": 500,
            "listed_price_cents": None,
            "listed_at": None,
            "fee_percent": 4,
            "strategy": "stickers",
            "stickers": stickers,
        }
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(purchase),
            headers=headers,
        )
        assert status == 200, payload
        item = payload["item"]
        assert item["status"] == "owned"
        assert item["listed_price_cents"] is None
        assert item["listed_at"] is None
        assert item["bought_at"].startswith("2026-08-01")
        assert item["stickers"] == stickers

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(
                {
                    "action": "update",
                    "item_id": item["id"],
                    "expected_revision": item["revision"],
                    "listed_price_cents": 800,
                    "listed_at": "2026-08-02",
                    # An unrelated edit must round-trip all structured sticker
                    # fields, not reduce the entry to its display name.
                    "stickers": stickers,
                }
            ),
            headers=headers,
        )
        assert status == 200, payload
        listed = payload["item"]
        assert listed["status"] == "listed"
        assert listed["listed_at"].startswith("2026-08-02")
        assert listed["stickers"] == stickers

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/inventory?status=listed&search=slate&sort=value_high",
            headers={"Cookie": _cookie_header(jar)},
        )
        assert status == 200, payload
        assert [row["id"] for row in payload["items"]] == [item["id"]]
        assert payload["summary"]["active_count"] == 3
        assert payload["summary"]["invested_cents"] == 1_500
        assert payload["summary"]["market_value_cents"] == 2_100

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(
                {
                    "action": "mark_sold",
                    "item_id": item["id"],
                    "expected_revision": listed["revision"],
                    "sold_price_cents": 0,
                    "sold_at": "2026-08-10",
                }
            ),
            headers=headers,
        )
        assert status == 400
        assert "sold_price_cents" in payload["error"]

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(
                {
                    "action": "mark_sold",
                    "item_id": item["id"],
                    "expected_revision": listed["revision"],
                    "sold_price_cents": 1_000,
                    "sold_at": "2026-08-10",
                }
            ),
            headers=headers,
        )
        assert status == 200, payload
        sold = payload["item"]
        assert sold["status"] == "sold"
        assert sold["sold_at"].startswith("2026-08-10")
        assert sold["analysis"]["profit_cents"] == 1_380
        assert sold["analysis"]["roi_percent"] == 92

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/inventory?status=sold&search=Slate&sort=profit_high",
            headers={"Cookie": _cookie_header(jar)},
        )
        assert status == 200, payload
        sold_view = payload["items"][0]
        assert sold_view["realized_profit_cents"] == 1_380
        assert sold_view["analysis"]["roi_percent"] == 92
        assert sold_view["pricing"]["unrealized_profit_cents"] != 1_380
        assert payload["summary"]["sold_count"] == 3
        assert payload["summary"]["realized_profit_cents"] == 1_380

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(
                {
                    "action": "archive",
                    "item_id": item["id"],
                    "expected_revision": sold["revision"],
                }
            ),
            headers=headers,
        )
        assert status == 200, payload
        archived = payload["item"]
        assert archived["status"] == "archived"

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/inventory?status=archived",
            headers={"Cookie": _cookie_header(jar)},
        )
        assert status == 200
        assert [row["id"] for row in payload["items"]] == [item["id"]]

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(
                {
                    "action": "restore",
                    "item_id": item["id"],
                    "expected_revision": archived["revision"],
                }
            ),
            headers=headers,
        )
        assert status == 200, payload
        assert payload["item"]["status"] == "sold"
        assert payload["item"]["stickers"] == stickers

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/profile",
            body=json.dumps(
                {
                    "action": "change_password",
                    "current_password": old_password,
                    "new_password": new_password,
                }
            ),
            headers=headers,
        )
        assert status == 200, payload

        # A password change revokes the current session too, preventing the
        # old bearer token from surviving a credential rotation.
        status, payload, _ = _request(
            port,
            "GET",
            "/api/auth/me",
            headers={"Cookie": _cookie_header(jar)},
        )
        assert status == 401, payload
        status, _payload, _ = _request(
            port,
            "POST",
            "/api/auth/login",
            body=json.dumps(
                {"email": "lifecycle@example.com", "password": old_password}
            ),
            headers=_auth_headers(port, "auth-login"),
        )
        assert status == 401
        status, payload, _ = _request(
            port,
            "POST",
            "/api/auth/login",
            body=json.dumps(
                {"email": "lifecycle@example.com", "password": new_password}
            ),
            headers=_auth_headers(port, "auth-login"),
        )
        assert status == 200, payload


def test_account_http_sale_update_and_undo_are_revision_and_tenant_safe(
    monkeypatch, tmp_path
):
    with _account_server(monkeypatch, tmp_path) as (port, _service, _sender):
        owner_jar = _register(port, "sale-http-owner@example.com")
        owner_headers = _account_headers(port, owner_jar)
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(
                {
                    "action": "create",
                    "skin_name": "AK-47 | Slate (Field-Tested)",
                    "buy_price_cents": 1_000,
                    "quantity": 1,
                    "bought_at": "2026-08-01",
                    "listed_price_cents": 1_400,
                    "listed_at": "2026-08-02",
                    "fee_percent": 4,
                }
            ),
            headers=owner_headers,
        )
        assert status == 200, payload
        listed = payload["item"]

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(
                {
                    "action": "mark_sold",
                    "item_id": listed["id"],
                    "expected_revision": listed["revision"],
                    "sold_price_cents": 1_500,
                    "sold_at": "2026-08-10",
                    "fee_percent": 4,
                }
            ),
            headers=owner_headers,
        )
        assert status == 200, payload
        sold = payload["item"]
        assert sold["analysis"]["profit_cents"] == 440

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(
                {
                    "action": "update_sale",
                    "item_id": listed["id"],
                    "expected_revision": sold["revision"],
                    "sold_price_cents": 1_700,
                    "sold_at": "2026-08-11",
                    "fee_percent": 5,
                }
            ),
            headers=owner_headers,
        )
        assert status == 200, payload
        corrected = payload["item"]
        assert corrected["sold_price_cents"] == 1_700
        assert corrected["sold_at"].startswith("2026-08-11")
        assert corrected["fee_percent"] == 5
        assert corrected["analysis"]["profit_cents"] == 615

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(
                {
                    "action": "update_sale",
                    "item_id": listed["id"],
                    "expected_revision": sold["revision"],
                    "sold_price_cents": 1_800,
                }
            ),
            headers=owner_headers,
        )
        assert status == 409, payload

        stranger_jar = _register(port, "sale-http-stranger@example.com")
        stranger_headers = _account_headers(port, stranger_jar)
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(
                {
                    "action": "undo_sale",
                    "item_id": listed["id"],
                    "expected_revision": corrected["revision"],
                }
            ),
            headers=stranger_headers,
        )
        assert status == 404, payload

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(
                {
                    "action": "undo_sale",
                    "item_id": listed["id"],
                    "expected_revision": corrected["revision"],
                }
            ),
            headers=owner_headers,
        )
        assert status == 200, payload
        restored = payload["item"]
        assert restored["status"] == "listed"
        assert restored["listed_price_cents"] == 1_400
        assert restored["sold_price_cents"] is None
        assert restored["sold_at"] is None

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/inventory?status=all",
            headers={"Cookie": _cookie_header(owner_jar)},
        )
        assert status == 200, payload
        assert payload["summary"]["active_count"] == 1
        assert payload["summary"]["sold_count"] == 0
        assert payload["summary"]["realized_profit_cents"] == 0


def test_account_trade_http_lifecycle_enforces_csrf_revisions_and_hides_artifacts(
    monkeypatch, tmp_path
):
    with _account_server(monkeypatch, tmp_path) as (port, _service, _sender):
        jar = _register(port, "trade-http@example.com")
        headers = _account_headers(port, jar)
        cookie_headers = {"Cookie": _cookie_header(jar)}

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/inventory",
            body=json.dumps(
                {
                    "action": "create",
                    "skin_name": "AWP | Graphite (Factory New)",
                    "quantity": 3,
                    "buy_price_cents": 1_001,
                    "bought_at": "2026-08-20",
                    "strategy": "investment",
                }
            ),
            headers=headers,
        )
        assert status == 200, payload
        source = payload["item"]

        create_request = {
            "action": "create",
            "traded_at": "2026-08-21",
            "notes": "AWP for cases",
            "outgoing": [
                {
                    "inventory_item_id": source["id"],
                    "quantity": 2,
                    "expected_revision": source["revision"],
                    "unit_value_cents": 1_200,
                }
            ],
            "incoming": [
                {
                    "skin_name": "Kilowatt Case",
                    "quantity": 2,
                    "unit_value_cents": 1_200,
                }
            ],
        }
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/trades",
            body=json.dumps(create_request),
            headers=_account_headers(port, jar, include_csrf=False),
        )
        assert status == 403, payload

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/trades",
            body=json.dumps(create_request),
            headers=headers,
        )
        assert status == 200, payload
        created = payload["trade"]
        assert created["revision"] == 1
        assert created["current_version"] == 1
        assert created["totals"]["outgoing_value_cents"] == 2_400
        assert created["totals"]["incoming_value_cents"] == 2_400
        assert created["outgoing"][0]["inventory_item_id"] == source["id"]
        assert created["incoming"][0]["item"]["skin_name"] == "Kilowatt Case"

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/trades",
            headers=cookie_headers,
        )
        assert status == 200, payload
        assert [trade["id"] for trade in payload["items"]] == [created["id"]]

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/inventory?status=all",
            headers=cookie_headers,
        )
        assert status == 200, payload
        assert {
            (item["skin_name"], item["quantity"], item["status"])
            for item in payload["items"]
        } == {
            ("AWP | Graphite (Factory New)", 1, "owned"),
            ("Kilowatt Case", 2, "owned"),
        }
        incoming = next(
            item for item in payload["items"] if item["skin_name"] == "Kilowatt Case"
        )
        assert incoming["acquisition_type"] == "trade"
        assert incoming["trade_id"] == created["id"]

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/inventory?status=archived",
            headers=cookie_headers,
        )
        assert status == 200, payload
        assert payload["items"] == []


        artifact_id = created["outgoing"][0]["artifact_inventory_item_id"]
        artifact_revision = created["outgoing"][0]["item"]["revision"]
        artifact_mutations = [
            {
                "action": "restore",
                "item_id": artifact_id,
                "expected_revision": artifact_revision,
            },
            {
                "action": "update",
                "item_id": artifact_id,
                "expected_revision": artifact_revision,
                "notes": "must not mutate an internal trade row",
            },
            {
                "action": "mark_sold",
                "item_id": artifact_id,
                "expected_revision": artifact_revision,
                "sold_price_cents": 2_500,
                "sold_at": "2026-08-21",
            },
        ]
        for mutation in artifact_mutations:
            status, mutation_payload, _ = _request(
                port,
                "POST",
                "/api/account/inventory",
                body=json.dumps(mutation),
                headers=headers,
            )
            assert status == 409, mutation_payload
            assert mutation_payload["error_code"] == "trade_managed_item"

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/trades",
            headers=cookie_headers,
        )
        assert status == 200, payload
        unchanged = payload["items"][0]
        assert unchanged["revision"] == created["revision"]
        assert unchanged["current_version"] == created["current_version"]
        assert unchanged["totals"] == created["totals"]
        assert [leg["id"] for leg in unchanged["legs"]] == [
            leg["id"] for leg in created["legs"]
        ]

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/inventory?status=all",
            headers=cookie_headers,
        )
        assert status == 200, payload
        assert {
            (item["skin_name"], item["quantity"], item["status"])
            for item in payload["items"]
        } == {
            ("AWP | Graphite (Factory New)", 1, "owned"),
            ("Kilowatt Case", 2, "owned"),
        }

        update_request = {
            "action": "update",
            "trade_id": created["id"],
            "expected_revision": created["revision"],
            "traded_at": "2026-08-22",
            "notes": "corrected to capsules",
            "outgoing": [
                {
                    "inventory_item_id": source["id"],
                    "quantity": 1,
                    "expected_revision": created["outgoing"][0][
                        "expected_revision"
                    ],
                    "unit_value_cents": 1_200,
                }
            ],
            "incoming": [
                {
                    "skin_name": "Paris 2023 Contenders Sticker Capsule",
                    "quantity": 3,
                    "unit_value_cents": 400,
                }
            ],
        }
        stale_update = {**update_request, "expected_revision": created["revision"] + 9}
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/trades",
            body=json.dumps(stale_update),
            headers=headers,
        )
        assert status == 409, payload
        assert payload["error_code"] == "conflict"

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/trades",
            body=json.dumps(update_request),
            headers=headers,
        )
        assert status == 200, payload
        updated = payload["trade"]
        assert updated["revision"] == 2
        assert updated["current_version"] == 2
        assert updated["notes"] == "corrected to capsules"
        assert updated["totals"]["value_difference_cents"] == 0

        for inventory_status in ("archived", "all"):
            status, inventory_payload, _ = _request(
                port,
                "GET",
                f"/api/account/inventory?status={inventory_status}",
                headers=cookie_headers,
            )
            assert status == 200, inventory_payload
            if inventory_status == "archived":
                assert inventory_payload["items"] == []
            else:
                assert {
                    (item["skin_name"], item["quantity"], item["status"])
                    for item in inventory_payload["items"]
                } == {
                    ("AWP | Graphite (Factory New)", 2, "owned"),
                    ("Paris 2023 Contenders Sticker Capsule", 3, "owned"),
                }

        stale_void = {
            "action": "void",
            "trade_id": updated["id"],
            "expected_revision": created["revision"],
        }
        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/trades",
            body=json.dumps(stale_void),
            headers=headers,
        )
        assert status == 409, payload

        status, payload, _ = _request(
            port,
            "POST",
            "/api/account/trades",
            body=json.dumps(
                {
                    "action": "void",
                    "trade_id": updated["id"],
                    "expected_revision": updated["revision"],
                }
            ),
            headers=headers,
        )
        assert status == 200, payload
        voided = payload["trade"]
        assert voided["status"] == "voided"
        assert voided["revision"] == 3

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/trades",
            headers=cookie_headers,
        )
        assert status == 200, payload
        assert payload["items"] == []

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/inventory?status=all",
            headers=cookie_headers,
        )
        assert status == 200, payload
        assert [
            (item["skin_name"], item["quantity"], item["status"])
            for item in payload["items"]
        ] == [("AWP | Graphite (Factory New)", 3, "owned")]
        assert payload["items"][0]["acquisition_type"] == "purchase"
        assert payload["items"][0]["trade_id"] is None

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/inventory?status=archived",
            headers=cookie_headers,
        )
        assert status == 200, payload
        assert payload["items"] == []


def test_account_item_market_auth_validation_and_container_floors(
    monkeypatch, tmp_path
):
    catalog = {
        "Kilowatt Case": {
            "market_hash_name": "Kilowatt Case",
            "item_kind": "case",
            "icon_url": "https://assets.example.test/kilowatt-case.png",
        },
        "Paris 2023 Challengers Sticker Capsule": {
            "market_hash_name": "Paris 2023 Challengers Sticker Capsule",
            "item_kind": "capsule",
            "icon_url": "https://assets.example.test/paris-capsule.png",
        },
    }
    price_values = {
        "Kilowatt Case": {
            "current_price_cents": 84,
            "active_quantity": 12_345,
        },
        "Paris 2023 Challengers Sticker Capsule": {
            "current_price_cents": 219,
            "active_quantity": 678,
        },
    }
    price_list_calls = []
    floor_calls = []

    def fake_search_items(_database_path, query, *, limit):
        assert limit == dashboard_server.ACCOUNT_ITEM_SEARCH_LIMIT
        folded = query.casefold()
        return [
            dict(item)
            for name, item in catalog.items()
            if not folded or folded in name.casefold()
        ]

    def fake_lookup_items(_database_path, names, *, limit):
        assert limit == 200
        return {name: dict(catalog[name]) for name in names if name in catalog}

    def fake_lookup_item(_database_path, name):
        item = catalog.get(name)
        return dict(item) if item else None

    def fake_price_list():
        price_list_calls.append("price-list")
        return {
            "items": price_values,
            "price_as_of": "2026-08-29T09:15:00+00:00",
            "price_source": "csfloat_price_list",
        }

    def fake_floor(name):
        floor_calls.append(name)
        values = price_values.get(name)
        if values is None:
            return None
        return {
            "market_hash_name": name,
            **values,
            "price_as_of": "2026-08-29T09:16:00+00:00",
            "price_source": "csfloat_price_list",
        }

    monkeypatch.setattr(
        dashboard_server.inventory_item_catalog, "search_items", fake_search_items
    )
    monkeypatch.setattr(
        dashboard_server.inventory_item_catalog,
        "lookup_items",
        fake_lookup_items,
        raising=False,
    )
    monkeypatch.setattr(
        dashboard_server.inventory_item_catalog, "lookup_item", fake_lookup_item
    )
    monkeypatch.setattr(
        dashboard_server.csfloat_listing_fetcher,
        "fetch_csfloat_price_list",
        fake_price_list,
    )
    monkeypatch.setattr(
        dashboard_server.csfloat_listing_fetcher,
        "fetch_csfloat_floor",
        fake_floor,
    )
    with _account_server(
        monkeypatch, tmp_path, live_pricing=True
    ) as (port, _service, _sender):
        for path in (
            "/api/account/item-search?q=case",
            "/api/account/item-quote?market_hash_name=Kilowatt%20Case",
        ):
            status, payload, _ = _request(port, "GET", path)
            assert status == 401
            assert payload["error"] == "unauthorized"
        assert price_list_calls == []
        assert floor_calls == []

        jar = _register(port, "item-market@example.com")
        cookie = {"Cookie": _cookie_header(jar)}

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/item-search?q=case",
            headers=cookie,
        )
        assert status == 200, payload
        assert payload == {
            "items": [
                {
                    "market_hash_name": "Kilowatt Case",
                    "item_kind": "case",
                    "item_type": "case",
                    "icon_url": "https://assets.example.test/kilowatt-case.png",
                    "current_price_cents": 84,
                    "active_quantity": 12_345,
                    "price_source": "csfloat_price_list",
                    "price_as_of": "2026-08-29T09:15:00+00:00",
                    "price_is_live_ask": True,
                }
            ]
        }

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/item-search?q=capsule",
            headers=cookie,
        )
        assert status == 200, payload
        capsule = payload["items"][0]
        assert capsule["market_hash_name"] == (
            "Paris 2023 Challengers Sticker Capsule"
        )
        assert capsule["item_kind"] == "capsule"
        assert capsule["icon_url"] == (
            "https://assets.example.test/paris-capsule.png"
        )
        assert capsule["current_price_cents"] == 219
        assert capsule["active_quantity"] == 678
        assert capsule["price_is_live_ask"] is True
        # Both autocomplete requests reuse the short-lived aggregate snapshot.
        assert price_list_calls == ["price-list"]

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/item-quote?market_hash_name=Kilowatt%20Case",
            headers=cookie,
        )
        assert status == 200, payload
        assert payload["quote_available"] is True
        assert payload["item"]["market_hash_name"] == "Kilowatt Case"
        assert payload["item"]["item_kind"] == "case"
        assert payload["item"]["icon_url"] == (
            "https://assets.example.test/kilowatt-case.png"
        )
        assert payload["item"]["current_price_cents"] == 84
        assert payload["item"]["price_is_live_ask"] is True
        assert floor_calls == ["Kilowatt Case"]

        status, payload, _ = _request(
            port,
            "GET",
            f"/api/account/item-search?q={'x' * 181}",
            headers=cookie,
        )
        assert status == 400
        assert "too long" in payload["error"]

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/item-quote",
            headers=cookie,
        )
        assert status == 400
        assert payload["error"] == "invalid market_hash_name"


def test_account_inventory_valuation_source_order_and_catalog_icon(
    monkeypatch, tmp_path
):
    live_name = "Kilowatt Case"
    historical_name = "AK-47 | Slate (Field-Tested)"
    saved_name = "Paris 2023 Challengers Sticker Capsule"
    catalog = {
        live_name: {
            "market_hash_name": live_name,
            "item_kind": "case",
            "icon_url": "https://assets.example.test/kilowatt-case.png",
        },
        historical_name: {
            "market_hash_name": historical_name,
            "item_kind": "weapon_skin",
            "icon_url": "https://assets.example.test/slate-catalog.png",
        },
        saved_name: {
            "market_hash_name": saved_name,
            "item_kind": "capsule",
            "icon_url": "https://assets.example.test/paris-capsule.png",
        },
    }
    price_list_calls = []

    def fake_lookup_items(_database_path, names, *, limit):
        assert limit == 200
        return {name: dict(catalog[name]) for name in names if name in catalog}

    def fake_price_list():
        price_list_calls.append("price-list")
        return {
            "items": {
                live_name: {
                    "current_price_cents": 900,
                    "active_quantity": 321,
                }
            },
            "price_as_of": "2026-08-29T10:00:00+00:00",
            "price_source": "csfloat_price_list",
        }

    monkeypatch.setattr(
        dashboard_server.inventory_item_catalog,
        "lookup_items",
        fake_lookup_items,
        raising=False,
    )
    monkeypatch.setattr(
        dashboard_server.csfloat_listing_fetcher,
        "fetch_csfloat_price_list",
        fake_price_list,
    )
    with _account_server(
        monkeypatch, tmp_path, live_pricing=True
    ) as (port, service, _sender):
        jar = _register(port, "valuation-order@example.com")
        cookie = {"Cookie": _cookie_header(jar)}
        status, me, _ = _request(port, "GET", "/api/auth/me", headers=cookie)
        assert status == 200, me
        user_id = me["user"]["id"]

        service.create_inventory_item(
            user_id,
            skin_name=live_name,
            buy_price_cents=500,
            market_price_cents=650,
            strategy="investment",
        )
        service.create_inventory_item(
            user_id,
            skin_name=historical_name,
            buy_price_cents=500,
            market_price_cents=650,
            strategy="investment",
        )
        service.create_inventory_item(
            user_id,
            skin_name=saved_name,
            buy_price_cents=500,
            market_price_cents=400,
            strategy="investment",
        )

        status, payload, _ = _request(
            port,
            "GET",
            "/api/account/inventory",
            headers=cookie,
        )
        assert status == 200, payload
        items = {item["skin_name"]: item for item in payload["items"]}

        live = items[live_name]
        assert live["pricing"]["market_median_cents"] == 900
        assert live["pricing"]["valuation_source"] == "csfloat_price_list"
        assert live["pricing"]["valuation_is_live_ask"] is True
        assert live["pricing"]["valuation_is_manual"] is False
        assert live["pricing"]["active_quantity"] == 321
        assert live["icon_url"] == "https://assets.example.test/kilowatt-case.png"
        assert live["item_kind"] == "case"

        historical = items[historical_name]
        assert historical["pricing"]["market_median_cents"] == 700
        assert historical["pricing"]["valuation_source"] == "historical_market"
        assert historical["pricing"]["valuation_is_live_ask"] is False
        assert historical["pricing"]["valuation_is_manual"] is False
        # The historical catalogue image remains authoritative when present.
        assert historical["icon_url"] == "https://example.test/slate.png"

        saved = items[saved_name]
        assert saved["pricing"]["market_median_cents"] == 400
        assert saved["pricing"]["valuation_source"] == "saved_valuation"
        assert saved["pricing"]["valuation_is_live_ask"] is False
        assert saved["pricing"]["valuation_is_manual"] is True
        assert saved["icon_url"] == "https://assets.example.test/paris-capsule.png"
        assert saved["item_kind"] == "capsule"

        assert payload["summary"]["invested_cents"] == 1_500
        assert payload["summary"]["market_value_cents"] == 2_000
        assert payload["summary"]["market_value_positions"] == 3
        assert price_list_calls == ["price-list"]
