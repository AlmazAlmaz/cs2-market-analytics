import http.client
import json
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

import account_backend
import dashboard_server
import scanner_control
import scanner_target_catalog


def _request(port, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        content_type = response.getheader("Content-Type", "")
        payload = json.loads(raw) if raw and "application/json" in content_type else raw
        return response.status, payload
    finally:
        connection.close()


def _request_with_declared_length(port, path, *, length, headers):
    """Send headers only so a rejecting server need not drain a huge body."""

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest("POST", path)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(length))
        connection.endheaders()
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        return response.status, json.loads(raw)
    finally:
        connection.close()


def _session_cookie(session):
    return f"{dashboard_server.ACCOUNT_SESSION_COOKIE}={session['session_token']}"


def _scanner_headers(port, session, *, action="scanner-targets", origin=None):
    headers = {
        "Content-Type": "application/json",
        "Cookie": _session_cookie(session),
        "Sec-Fetch-Site": "same-origin",
    }
    if action is not None:
        headers["X-CS2-Action"] = action
    headers["Origin"] = origin or f"http://127.0.0.1:{port}"
    return headers


@contextmanager
def _scanner_target_server(monkeypatch, tmp_path):
    service = account_backend.AccountService(tmp_path / "accounts.db")
    admin = service.create_user(
        "scanner-admin@example.com",
        "correct horse battery staple",
        role="admin",
    )
    member = service.create_user(
        "scanner-member@example.com",
        "correct horse battery staple",
        role="member",
        access_state="approved",
    )
    admin_session = service.create_session(admin["id"])
    member_session = service.create_session(member["id"])

    monkeypatch.setattr(dashboard_server, "ACCOUNT_SERVICE", service)
    monkeypatch.setattr(
        scanner_control,
        "TARGET_CONTROL_PATH",
        tmp_path / "scanner-target-control.json",
    )
    monkeypatch.setattr(
        scanner_target_catalog,
        "load_preview_config",
        lambda path=None: {
            "liquid_skins_enabled": True,
            "knives_enabled": False,
            "gloves_enabled": False,
        },
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], admin_session, member_session
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        service.close()


def test_scanner_targets_route_is_admin_only_and_returns_bounded_contract(
    monkeypatch, tmp_path
):
    with _scanner_target_server(monkeypatch, tmp_path) as (
        port,
        admin_session,
        member_session,
    ):
        status, payload = _request(port, "GET", "/api/scanner-targets")
        assert status == 401
        assert payload["error"] == "unauthorized"

        status, payload = _request(
            port,
            "GET",
            "/api/scanner-targets",
            headers={"Cookie": _session_cookie(member_session)},
        )
        assert status == 403
        assert payload["error"] == "admin role is required"

        status, payload = _request(
            port,
            "POST",
            "/api/scanner-targets",
            body=json.dumps(
                {
                    "expected_revision": 0,
                    "profiles": {"knives": {"enabled": True}},
                }
            ),
            headers=_scanner_headers(port, member_session),
        )
        assert status == 403
        assert payload["error"] == "admin role is required"

        status, payload = _request(
            port,
            "GET",
            "/api/scanner-targets",
            headers={"Cookie": _session_cookie(admin_session)},
        )
        assert status == 200
        assert payload["control"]["revision"] == 0
        assert payload["profile_order"] == [
            "sticker_guns",
            "liquid_under_10",
            "knives",
            "gloves",
        ]
        assert payload["limits"] == {
            "selection_modes": ["custom", "recommended"],
            "max_included_targets": scanner_control.MAX_INCLUDED_TARGETS,
            "max_excluded_targets": scanner_control.MAX_EXCLUDED_TARGETS,
            "max_target_key_length": scanner_control.MAX_TARGET_KEY_LENGTH,
            "max_request_body_bytes": scanner_control.TARGET_CONTROL_BODY_LIMIT,
            "targets_per_cycle": {
                "sticker_guns": {"min": 1, "max": 250, "default": 5},
                "liquid_under_10": {"min": 1, "max": 25, "default": 2},
                "knives": {"min": 1, "max": 10, "default": 1},
                "gloves": {"min": 1, "max": 10, "default": 1},
            },
            "listings_per_target": {
                "sticker_guns": {"min": 1, "max": 50, "default": 50},
                "liquid_under_10": {"min": 1, "max": 25, "default": 20},
                "knives": {"min": 1, "max": 20, "default": 10},
                "gloves": {"min": 1, "max": 20, "default": 10},
            },
            "max_included_groups": scanner_control.MAX_INCLUDED_GROUPS,
        }


def test_scanner_targets_mutation_requires_json_action_origin_and_bounded_body(
    monkeypatch, tmp_path
):
    with _scanner_target_server(monkeypatch, tmp_path) as (
        port,
        admin_session,
        _member_session,
    ):
        body = json.dumps(
            {
                "expected_revision": 0,
                "profiles": {"knives": {"enabled": True}},
            }
        )

        missing_action = _scanner_headers(port, admin_session, action=None)
        status, payload = _request(
            port,
            "POST",
            "/api/scanner-targets",
            body=body,
            headers=missing_action,
        )
        assert status == 403
        assert payload["error"] == "missing scanner action confirmation"

        wrong_origin = _scanner_headers(
            port,
            admin_session,
            origin="https://cross-site.example",
        )
        status, payload = _request(
            port,
            "POST",
            "/api/scanner-targets",
            body=body,
            headers=wrong_origin,
        )
        assert status == 403
        assert payload["error"] == "request Origin does not match this dashboard"

        wrong_type = _scanner_headers(port, admin_session)
        wrong_type["Content-Type"] = "text/plain"
        status, payload = _request(
            port,
            "POST",
            "/api/scanner-targets",
            body=body,
            headers=wrong_type,
        )
        assert status == 415
        assert payload["error"] == "Content-Type must be application/json"

        status, payload = _request_with_declared_length(
            port,
            "/api/scanner-targets",
            length=dashboard_server.SCANNER_TARGET_BODY_LIMIT + 1,
            headers=_scanner_headers(port, admin_session),
        )
        assert status == 400
        assert payload["error"] == "invalid request body"


def test_scanner_targets_revision_conflict_and_client_validation_are_distinct(
    monkeypatch, tmp_path
):
    with _scanner_target_server(monkeypatch, tmp_path) as (
        port,
        admin_session,
        _member_session,
    ):
        headers = _scanner_headers(port, admin_session)
        update = {
            "expected_revision": 0,
            "profiles": {
                "knives": {
                    "enabled": True,
                    "selection_mode": "custom",
                    "include": ["knife:Karambit | Doppler (Factory New)"],
                    "exclude": [],
                }
            },
        }
        status, payload = _request(
            port,
            "POST",
            "/api/scanner-targets",
            body=json.dumps(update),
            headers=headers,
        )
        assert status == 200
        assert payload["control"]["revision"] == 1
        assert payload["control"]["profiles"]["knives"]["include"] == [
            "knife:Karambit | Doppler (Factory New)"
        ]

        status, payload = _request(
            port,
            "POST",
            "/api/scanner-targets",
            body=json.dumps(update),
            headers=headers,
        )
        assert status == 409
        assert "changed" in payload["error"]

        invalid = {
            "expected_revision": 1,
            "profiles": {
                "knives": {
                    "include": ["glove:Sport Gloves | Vice (Field-Tested)"]
                }
            },
        }
        status, payload = _request(
            port,
            "POST",
            "/api/scanner-targets",
            body=json.dumps(invalid),
            headers=headers,
        )
        assert status == 400
        assert "invalid target key" in payload["error"]

        stored = scanner_control.get_scanner_target_control()
        assert stored["revision"] == 1
        assert stored["profiles"]["knives"]["include"] == [
            "knife:Karambit | Doppler (Factory New)"
        ]


def _javascript_function(source, name):
    match = re.search(rf"\bfunction\s+{re.escape(name)}\s*\(", source)
    if match is None:
        raise AssertionError(f"missing JavaScript function {name}")
    brace = source.find("{", match.end())
    if brace < 0:
        raise AssertionError(f"missing JavaScript body for {name}")
    depth = 0
    for index in range(brace, len(source)):
        character = source[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
    raise AssertionError(f"unterminated JavaScript function {name}")


def test_scanner_target_javascript_uses_saved_selection_and_explicit_overrides(
    tmp_path,
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")

    helpers = "\n".join(
        _javascript_function(dashboard_server.HTML, name)
        for name in (
            "scannerTargetSelectedWithOverrides",
            "scannerTargetSelected",
            "applyScannerTargetChoice",
        )
    )
    assertions = r"""
const cappedRecommendation = {
  target_key: 'liquid_skin:b',
  selected: false,
  recommended: true,
};
if (scannerTargetSelected(cappedRecommendation, {dirty: false}) !== false) {
  throw new Error('clean UI must use backend selected, not recommended');
}
const include = new Set();
const exclude = new Set(['liquid_skin:b']);
applyScannerTargetChoice(cappedRecommendation, true, 'recommended', include, exclude);
if (!include.has('liquid_skin:b') || exclude.has('liquid_skin:b')) {
  throw new Error('checking must create an explicit include');
}
applyScannerTargetChoice(cappedRecommendation, false, 'recommended', include, exclude);
if (include.has('liquid_skin:b') || !exclude.has('liquid_skin:b')) {
  throw new Error('unchecking recommended must create an explicit exclude');
}
"""
    script = tmp_path / "scanner-target-selection.js"
    script.write_text(f"'use strict';\n{helpers}\n{assertions}", encoding="utf-8")
    completed = subprocess.run(
        [node, str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_scanner_target_ui_keeps_all_drafts_and_blocks_inflight_edits():
    source = dashboard_server.HTML
    assert (
        "Array.from(scannerTargetDrafts.values()).some(draft => draft.dirty)"
        in source
    )
    assert "if (!draft || !item || scannerTargetBusy) return;" in source
    assert "const submittedProfile = scannerTargetProfile;" in source
    assert "include: [...draft.include]" in source
    assert "if (!scannerTargetPreview)" in source
    assert 'id="scannerTargetResetOverrides"' in source
