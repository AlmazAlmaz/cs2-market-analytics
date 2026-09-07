from __future__ import annotations

import re
import shutil
import subprocess

import pytest

import admin_users_ui


def test_api_contract_is_explicit_and_stable():
    contract = admin_users_ui.ADMIN_USERS_API_CONTRACT

    assert contract["endpoint"] == "/api/admin/users"
    assert contract["required_post_fields"] == (
        "action",
        "user_id",
        "expected_revision",
    )
    assert contract["access_states"] == (
        "pending",
        "approved",
        "blocked",
        "deleted",
    )
    assert contract["actions"] == (
        "approve",
        "block",
        "grant_admin",
        "revoke_admin",
        "revoke_sessions",
        "send_password_reset",
        "delete",
    )


@pytest.mark.parametrize(
    ("locale", "title", "heading", "default_access_copy"),
    [
        (
            "ru",
            "Пользователи · CS2 Market",
            "Доступ пользователей",
            "По умолчанию у каждого нового аккаунта отказ в доступе",
        ),
        (
            "en",
            "Users · CS2 Market",
            "User access",
            "Every new account is denied by default",
        ),
    ],
)
def test_page_is_bilingual_and_explains_deny_by_default(
    locale, title, heading, default_access_copy
):
    page = admin_users_ui.render_admin_users_page(locale)

    assert f'<html lang="{locale}">' in page
    assert f"<title>{title}</title>" in page
    assert heading in page
    assert default_access_copy in page
    assert 'id="localeSelect"' in page
    assert "cs2_locale" in page
    assert "localStorage.setItem('cs2_locale',value)" in page


def test_unknown_locale_falls_back_to_russian():
    page = admin_users_ui.render_admin_users_page("de")

    assert '<html lang="ru">' in page
    assert "<title>Пользователи · CS2 Market</title>" in page


def test_page_covers_search_filters_summary_and_live_activity():
    page = admin_users_ui.render_admin_users_page()

    for element_id in (
        "searchInput",
        "activityFilter",
        "sortSelect",
        "metricTotal",
        "metricPending",
        "metricApproved",
        "metricBlocked",
        "metricOnline",
        "usersList",
    ):
        assert f'id="{element_id}"' in page

    for status in ("all", "pending", "approved", "blocked", "deleted"):
        assert f'data-status="{status}"' in page

    assert "const ONLINE_WINDOW_SECONDS=300" in page
    assert "window.setInterval" in page
    assert "20000" in page
    assert "last_seen_at" in page
    assert "last_login_at" in page
    assert "active_sessions" in page
    assert "recent_sessions" in page


def test_page_displays_sanitized_network_security_and_account_metadata():
    page = admin_users_ui.render_admin_users_page("en")

    for field in (
        "current_ip",
        "current_ip_address",
        "last_ip",
        "last_ip_address",
        "registration_ip",
        "registration_ip_address",
        "current_country",
        "country_code",
        "last_user_agent",
        "password_changed_at",
        "password_reset_sent_at",
        "created_at",
        "approved_at",
        "blocked_at",
        "revision",
    ):
        assert field in page

    assert "Country is an estimate derived from a trusted proxy header or IP" in page
    assert "password_hash" not in page
    assert "session_token" not in page
    assert "dashboard_token" not in page


def test_mutations_use_one_endpoint_revision_and_allowlisted_action_names():
    page = admin_users_ui.render_admin_users_page()

    assert "const API_ENDPOINT='/api/admin/users'" in page
    assert "const ACTIONS=new Set([" in page
    assert "method:'POST'" in page
    assert "credentials:'same-origin'" in page
    assert "cookieValue('cs2_csrf')" in page
    assert "headers['X-CSRF-Token']=csrf" in page
    assert "headers:mutationHeaders()" in page
    assert (
        "const requestBody={action,user_id:user.id,"
        "expected_revision:safeNumber(user.revision)}" in page
    )
    assert "requestBody.confirmation_email=" in page
    assert "body:JSON.stringify(requestBody)" in page
    assert "admin-user-management" in page
    assert "payload.error" not in page
    for action in admin_users_ui.ADMIN_USERS_API_CONTRACT["actions"]:
        assert f'"{action}"' in page

    assert "user.role==='admin'" in page
    assert "grantAdminConfirm" in page
    assert "revokeAdminConfirm" in page
    assert "system dashboard and database download" in page


def test_delete_requires_typing_the_exact_email_and_self_destruction_is_disabled():
    page = admin_users_ui.render_admin_users_page()

    assert 'id="confirmField" hidden' in page
    assert "pendingAction.user.email" in page
    assert "event.target.value" in page
    assert "pendingAction?.action!=='delete'" in page
    assert "user.is_self===true&&(action==='block'||action==='delete')" in page
    assert "byId('submitConfirm').disabled=isDelete" in page


def test_password_reset_link_is_transient_copyable_and_never_html_injected():
    page = admin_users_ui.render_admin_users_page("en")

    assert 'id="resetLinkDialog"' in page
    assert 'id="resetLinkValue" type="text" readonly' in page
    assert "showResetLink(payload.reset_url)" in page
    assert "navigator.clipboard.writeText(value)" in page
    assert "byId('resetLinkValue').value=''" in page
    assert "new URL(raw,location.origin)" in page
    assert "parsed.protocol==='https:'||parsed.protocol==='http:'" in page
    assert "reset_url" not in re.sub(
        r"showResetLink\(payload\.reset_url\)", "", page
    ).split("<script>", 1)[0]
    assert ".innerHTML" not in page
    assert "insertAdjacentHTML" not in page
    assert "document.write" not in page
    assert ".outerHTML" not in page
    assert ".textContent" in page
    assert "replaceChildren()" in page


def test_local_admin_boundary_is_prominent_but_not_mistaken_for_enforcement():
    page = admin_users_ui.render_admin_users_page()

    assert "Только localhost · администратор" in page
    assert "local_required" in page
    assert "localhost + administrator access before rendering" not in page
    assert "Authorization is intentionally not accepted" not in page


def test_page_has_responsive_layout_and_no_horizontal_overflow_defaults():
    page = admin_users_ui.render_admin_users_page()

    assert "html,body{max-width:100%;overflow-x:hidden}" in page
    assert "@media(max-width:760px)" in page
    assert "@media(max-width:480px)" in page
    assert "minmax(0,1fr)" in page
    assert "overflow-wrap:anywhere" in page


def test_inline_browser_program_has_valid_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    page = admin_users_ui.render_admin_users_page()
    match = re.search(r"<script>(.*?)</script>", page, flags=re.DOTALL)
    assert match

    result = subprocess.run(
        [node, "--check"],
        input=match.group(1),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
