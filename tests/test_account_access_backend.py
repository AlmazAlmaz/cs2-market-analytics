import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

import account_backend

NOW = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
PASSWORD = "correct horse battery staple"


def make_service(tmp_path, *, sender=None):
    return account_backend.AccountService(
        tmp_path / "accounts.db",
        password_reset_sender=sender,
        reset_base_url="http://localhost/reset-password",
    )


def token_from_url(url):
    return parse_qs(urlsplit(url).query)["token"][0]


def test_v1_migration_makes_every_legacy_account_pending_and_revokes_sessions(
    tmp_path,
):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE app_schema_versions (
            component TEXT PRIMARY KEY, version INTEGER NOT NULL, migrated_at TEXT NOT NULL
        )
        """
    )
    account_backend._migrate_v1(conn)
    password_hash = account_backend.hash_password(PASSWORD)
    for email, role in (("member@example.com", "member"), ("admin@example.com", "admin")):
        user_id = conn.execute(
            """
            INSERT INTO account_users(
                email, role, password_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (email, role, password_hash, NOW.isoformat(), NOW.isoformat()),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO account_sessions(
                user_id, token_hash, csrf_hash, created_at,
                expires_at, last_seen_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                user_id,
                hashlib.sha256(f"session-{user_id}".encode()).hexdigest(),
                hashlib.sha256(f"csrf-{user_id}".encode()).hexdigest(),
                NOW.isoformat(),
                (NOW + timedelta(days=30)).isoformat(),
                NOW.isoformat(),
            ),
        )
    conn.execute(
        "INSERT INTO app_schema_versions VALUES (?, 1, ?)",
        (account_backend.SCHEMA_COMPONENT, NOW.isoformat()),
    )
    conn.commit()
    conn.close()

    service = account_backend.AccountService(path)
    with sqlite3.connect(path) as migrated:
        states = migrated.execute(
            "SELECT role, access_state FROM account_users ORDER BY id"
        ).fetchall()
        revoked = migrated.execute(
            "SELECT COUNT(*) FROM account_sessions WHERE revoked_at IS NOT NULL"
        ).fetchone()[0]
        columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(account_users)")
        }

    assert states == [("member", "pending"), ("member", "pending")]
    assert revoked == 2
    assert {
        "access_state",
        "revision",
        "registration_ip_address",
        "registration_country_code",
        "registration_user_agent",
    } <= columns
    assert service.admin_list_users(trusted_local_admin=True)["summary"] == {
        "total": 2,
        "pending": 2,
        "approved": 0,
        "blocked": 0,
        "deleted": 0,
        "online": 0,
        "downloading": 0,
    }

    # The privilege drop is a one-time v1 -> v2 migration.  An administrator
    # explicitly provisioned after it remains approved on subsequent starts.
    fresh_admin = service.create_user(
        "fresh-admin@example.com", PASSWORD, role="admin", now=NOW
    )
    assert fresh_admin["role"] == "admin"
    assert fresh_admin["access_state"] == "approved"
    service.close()
    reopened = account_backend.AccountService(path)
    persisted = reopened.get_user(fresh_admin["id"])
    assert persisted["role"] == "admin"
    assert persisted["access_state"] == "approved"


def test_registration_is_pending_and_local_owner_must_explicitly_approve(tmp_path):
    service = make_service(tmp_path)
    member = service.create_user(
        "member@example.com",
        PASSWORD,
        ip_address="203.0.113.7",
        country_code="ru",
        user_agent="Test Browser\x00/1",
        now=NOW,
    )
    admin = service.create_user(
        "admin@example.com", PASSWORD, role="admin", now=NOW
    )

    assert member["access_state"] == "pending"
    assert admin["access_state"] == "approved"
    with pytest.raises(account_backend.AccountAccessError) as pending:
        service.authenticate("member@example.com", PASSWORD, now=NOW)
    assert pending.value.code == "access_pending"
    with pytest.raises(account_backend.AuthenticationError) as wrong_password:
        service.authenticate("member@example.com", "wrong password value", now=NOW)
    assert wrong_password.value.code == "invalid_credentials"
    with pytest.raises(account_backend.AccountAccessError) as trusted_session:
        service.create_session(member["id"], now=NOW)
    assert trusted_session.value.code == "access_pending"
    with pytest.raises(account_backend.AccountAccessError):
        service.update_profile(member["id"], display_name="Bypass", now=NOW)
    with pytest.raises(account_backend.AccountAccessError):
        service.create_inventory_item(
            member["id"], skin_name="AK-47 | Redline", buy_price_cents=1000, now=NOW
        )
    with pytest.raises(account_backend.AuthorizationError) as untrusted:
        service.admin_list_users()
    assert untrusted.value.code == "admin_required"
    with pytest.raises(account_backend.AuthorizationError):
        service.admin_list_users(trusted_local_admin=1)

    listing = service.admin_list_users(trusted_local_admin=True, now=NOW)
    pending_row = next(user for user in listing["users"] if user["id"] == member["id"])
    assert pending_row["registration_ip_address"] == "203.0.113.7"
    assert pending_row["registration_country_code"] == "RU"
    assert pending_row["registration_user_agent"] == "Test Browser /1"
    assert "password_hash" not in json.dumps(listing)
    assert "token_hash" not in json.dumps(listing)

    approved = service.admin_approve_user(
        member["id"],
        trusted_local_admin=True,
        expected_revision=member["revision"],
        now=NOW + timedelta(minutes=1),
    )
    assert approved["access_state"] == "approved"
    assert service.authenticate("member@example.com", PASSWORD, now=NOW)["user"][
        "id"
    ] == member["id"]


def test_presence_uses_any_recent_active_session_and_block_is_immediate(tmp_path):
    service = make_service(tmp_path)
    member = service.create_user("member@example.com", PASSWORD, now=NOW)
    member = service.admin_approve_user(
        member["id"], trusted_local_admin=True, now=NOW
    )
    first = service.authenticate(
        "member@example.com",
        PASSWORD,
        ip_address="198.51.100.4",
        country_code="DE",
        user_agent="First browser",
        now=NOW,
    )
    second = service.authenticate(
        "member@example.com",
        PASSWORD,
        ip_address="198.51.100.5",
        country_code="FR",
        user_agent="Second browser",
        now=NOW + timedelta(minutes=1),
    )
    service.revoke_session(second["session_token"], now=NOW + timedelta(minutes=2))
    service.validate_session(
        first["session_token"],
        ip_address="198.51.100.6",
        country_code="NL",
        user_agent="First browser updated",
        now=NOW + timedelta(minutes=3),
    )

    listing = service.admin_list_users(
        trusted_local_admin=True,
        online_only=True,
        now=NOW + timedelta(minutes=4),
    )
    assert listing["total"] == 1
    assert listing["summary"]["online"] == 1
    assert listing["users"][0]["online"] is True
    assert len(listing["users"][0]["sessions"]) == 2
    active_session = next(
        session for session in listing["users"][0]["sessions"] if session["active"]
    )
    assert active_session["ip_address"] == "198.51.100.6"
    assert active_session["country_code"] == "NL"

    blocked = service.admin_block_user(
        member["id"],
        trusted_local_admin=True,
        reason="manual review",
        expected_revision=member["revision"],
        now=NOW + timedelta(minutes=5),
    )
    assert blocked["access_state"] == "blocked"
    with pytest.raises(account_backend.AccountAccessError) as login_error:
        service.authenticate("member@example.com", PASSWORD, now=NOW)
    assert login_error.value.code == "access_blocked"
    with pytest.raises(account_backend.AccountAccessError) as session_error:
        service.validate_session(first["session_token"], now=NOW)
    assert session_error.value.code == "access_blocked"
    with pytest.raises(account_backend.ConflictError) as stale_revision:
        service.admin_approve_user(
            member["id"],
            trusted_local_admin=True,
            expected_revision=member["revision"],
            now=NOW,
        )
    assert stale_revision.value.code == "revision_conflict"


def test_soft_delete_keeps_inventory_and_audit_but_scrubs_identity(tmp_path):
    service = make_service(tmp_path)
    user = service.create_user(
        "person@example.com",
        PASSWORD,
        display_name="Person",
        access_state="approved",
        ip_address="203.0.113.20",
        user_agent="Browser",
        now=NOW,
    )
    login = service.authenticate("person@example.com", PASSWORD, now=NOW)
    service.create_inventory_item(
        user["id"], skin_name="AK-47 | Redline", buy_price_cents=1000, now=NOW
    )

    deleted = service.admin_soft_delete_user(
        user["id"],
        trusted_local_admin=True,
        reason="owner request",
        expected_revision=user["revision"],
        now=NOW + timedelta(minutes=1),
    )
    assert deleted["access_state"] == "deleted"
    assert deleted["email"].endswith("@invalid.invalid")
    assert deleted["display_name"] is None
    with pytest.raises(account_backend.AccountAccessError) as old_session:
        service.validate_session(login["session_token"], now=NOW)
    assert old_session.value.code == "account_deleted"

    with sqlite3.connect(service.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM account_users WHERE id = ?", (user["id"],)
        ).fetchone()
        inventory_count = conn.execute(
            "SELECT COUNT(*) FROM account_inventory_items WHERE user_id = ?",
            (user["id"],),
        ).fetchone()[0]
    assert inventory_count == 1
    assert row["registration_ip_address"] is None
    assert row["registration_user_agent"] is None
    assert not account_backend.verify_password(PASSWORD, row["password_hash"])

    replacement = service.create_user("person@example.com", PASSWORD, now=NOW)
    assert replacement["access_state"] == "pending"
    audit = service.admin_list_audit(
        trusted_local_admin=True, target_user_id=user["id"]
    )
    deletion = next(event for event in audit if event["event_type"] == "account_soft_deleted")
    assert deletion["payload"]["reason"] == "owner request"
    encoded_audit = json.dumps(audit).lower()
    assert "person@example.com" not in encoded_audit
    assert "password_hash" not in encoded_audit
    assert "reset_url" not in encoded_audit
    assert "token_hash" not in encoded_audit


def test_admin_reset_is_one_time_hashed_revokes_sessions_and_reports_delivery(tmp_path):
    sender = account_backend.InMemoryPasswordResetSender()
    service = make_service(tmp_path, sender=sender)
    user = service.create_user(
        "person@example.com", PASSWORD, access_state="approved", now=NOW
    )
    login = service.authenticate("person@example.com", PASSWORD, now=NOW)
    issued = service.admin_issue_password_reset(
        user["id"],
        trusted_local_admin=True,
        deliver=True,
        expected_revision=user["revision"],
        now=NOW + timedelta(minutes=1),
    )
    token = token_from_url(issued["reset_url"])

    assert issued["delivery_state"] == "sent"
    assert issued["delivery_configured"] is True
    assert issued["revoked_sessions"] == 1
    assert sender.messages[0].reset_url == issued["reset_url"]
    issued_detail = service.admin_get_user(
        user["id"], trusted_local_admin=True, now=NOW + timedelta(minutes=1)
    )
    assert issued_detail["password_reset_sent_at"] == (
        NOW + timedelta(minutes=1)
    ).isoformat()
    assert issued_detail["password_changed_at"] is None
    with sqlite3.connect(service.db_path) as conn:
        stored = conn.execute(
            "SELECT token_hash, issued_by_kind FROM account_password_resets"
        ).fetchone()
    assert stored == (hashlib.sha256(token.encode()).hexdigest(), "local_dashboard")
    assert token not in stored
    with pytest.raises(account_backend.AuthenticationError) as revoked:
        service.validate_session(login["session_token"], now=NOW)
    assert revoked.value.code == "invalid_session"

    changed = service.reset_password(
        token, "a replacement password", now=NOW + timedelta(minutes=2)
    )
    assert changed["revision"] == issued["revision"] + 1
    changed_detail = service.admin_get_user(
        user["id"], trusted_local_admin=True, now=NOW + timedelta(minutes=2)
    )
    assert changed_detail["password_changed_at"] == (
        NOW + timedelta(minutes=2)
    ).isoformat()
    with pytest.raises(account_backend.AuthenticationError):
        service.reset_password(
            token, "another replacement password", now=NOW + timedelta(minutes=3)
        )
    service.authenticate(
        "person@example.com", "a replacement password", now=NOW + timedelta(minutes=3)
    )


def test_local_manual_reset_works_without_smtp_and_presence_can_be_purged(tmp_path):
    service = make_service(tmp_path)
    user = service.create_user(
        "person@example.com", PASSWORD, access_state="approved", now=NOW
    )
    service.authenticate(
        "person@example.com",
        PASSWORD,
        ip_address="192.0.2.10",
        country_code="US",
        user_agent="Old Browser",
        now=NOW,
    )
    revoked = service.admin_revoke_sessions(
        user["id"],
        trusted_local_admin=True,
        expected_revision=user["revision"],
        now=NOW + timedelta(minutes=1),
    )
    assert revoked == {"revoked_sessions": 1, "revision": user["revision"] + 1}
    with pytest.raises(account_backend.ConflictError) as stale:
        service.admin_revoke_sessions(
            user["id"],
            trusted_local_admin=True,
            expected_revision=user["revision"],
            now=NOW + timedelta(minutes=2),
        )
    assert stale.value.code == "revision_conflict"

    purged = service.admin_purge_presence_metadata(
        trusted_local_admin=True,
        older_than_days=30,
        now=NOW + timedelta(days=31),
    )
    assert purged == {"purged_sessions": 1}
    detail = service.admin_get_user(
        user["id"], trusted_local_admin=True, now=NOW + timedelta(days=31)
    )
    assert detail["sessions"][0]["ip_address"] is None
    assert detail["sessions"][0]["country_code"] is None
    assert detail["sessions"][0]["user_agent"] is None

    issued = service.admin_issue_password_reset(
        user["id"],
        trusted_local_admin=True,
        deliver=True,
        expected_revision=revoked["revision"],
        now=NOW + timedelta(days=31),
    )
    assert issued["delivery_state"] == "not_configured"
    service.reset_password(
        token_from_url(issued["reset_url"]),
        "manual reset password",
        now=NOW + timedelta(days=31, minutes=1),
    )
