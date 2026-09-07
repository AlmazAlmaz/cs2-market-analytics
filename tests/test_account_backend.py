import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

import account_backend

NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
PASSWORD = "correct horse battery staple"


def make_service(tmp_path, *, sender=None):
    return account_backend.AccountService(
        tmp_path / "private" / "accounts.db",
        password_reset_sender=sender,
        reset_base_url="http://localhost/reset-password",
    )


def reset_token(message):
    return parse_qs(urlsplit(message.reset_url).query)["token"][0]


def test_schema_migration_is_idempotent_and_activity_is_immutable():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE app_schema_versions (
            component TEXT PRIMARY KEY, version INTEGER NOT NULL, migrated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("INSERT INTO app_schema_versions VALUES ('existing', 7, '2026-01-01')")

    account_backend.init_schema(conn)
    account_backend.init_schema(conn)
    versions = dict(conn.execute("SELECT component, version FROM app_schema_versions"))

    assert versions == {
        "existing": 7,
        "account_backend": account_backend.SCHEMA_VERSION,
    }
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(account_users)").fetchall()
    }
    assert {"role", "stale_after_days", "default_fee_percent"} <= columns

    password_hash = account_backend.hash_password(PASSWORD)
    conn.execute(
        """
        INSERT INTO account_users(
            email, role, password_hash, created_at, updated_at
        ) VALUES ('person@example.com', 'member', ?, ?, ?)
        """,
        (password_hash, NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO account_activity(user_id, event_type, occurred_at, payload_json)
        VALUES (1, 'test', ?, '{}')
        """,
        (NOW.isoformat(),),
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE account_activity SET event_type = 'rewritten' WHERE id = 1"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM account_activity WHERE id = 1")


def test_password_hash_has_salt_and_rejects_malformed_values():
    first = account_backend.hash_password(PASSWORD)
    second = account_backend.hash_password(PASSWORD)

    assert first != second
    assert PASSWORD not in first
    assert account_backend.verify_password(PASSWORD, first)
    assert not account_backend.verify_password("definitely wrong", first)
    assert not account_backend.verify_password(PASSWORD, "scrypt$999999$8$1$aa$bb")


def test_roles_login_session_hash_csrf_expiry_and_generic_failure(tmp_path):
    service = make_service(tmp_path)
    member = service.create_user(
        "  PERSON@Example.COM ", PASSWORD, role="member", display_name="Person", now=NOW
    )
    admin = service.create_user("admin@example.com", PASSWORD, role="admin", now=NOW)

    assert member["email"] == "person@example.com"
    assert member["role"] == "member"
    with pytest.raises(account_backend.AuthorizationError):
        service.set_user_role(
            actor_user_id=member["id"],
            target_user_id=admin["id"],
            role="member",
            now=NOW,
        )
    with pytest.raises(account_backend.AuthorizationError) as owner_only:
        service.set_user_role(
            actor_user_id=admin["id"],
            target_user_id=member["id"],
            role="admin",
            now=NOW,
        )
    assert owner_only.value.code == "owner_authorization_required"
    with pytest.raises(account_backend.ConflictError) as pending_role:
        service.set_user_role(
            trusted_local_admin=True,
            target_user_id=member["id"],
            role="admin",
            expected_revision=member["revision"],
            now=NOW,
        )
    assert pending_role.value.code == "access_not_approved"
    approved = service.admin_approve_user(
        member["id"],
        actor_user_id=admin["id"],
        expected_revision=member["revision"],
        now=NOW,
    )
    changed = service.set_user_role(
        trusted_local_admin=True,
        target_user_id=member["id"],
        role="admin",
        expected_revision=approved["revision"],
        now=NOW,
    )
    assert changed["role"] == "admin"
    assert changed["revoked_sessions"] == 0

    login = service.authenticate(
        "person@example.com", PASSWORD, session_ttl=600, now=NOW
    )
    assert login["session_token"].startswith("cs2s_")
    assert login["csrf_token"].startswith("cs2c_")
    with sqlite3.connect(service.db_path) as conn:
        row = conn.execute(
            "SELECT token_hash, csrf_hash FROM account_sessions WHERE id = ?",
            (login["id"],),
        ).fetchone()
    assert row[0] == hashlib.sha256(login["session_token"].encode()).hexdigest()
    assert row[1] == hashlib.sha256(login["csrf_token"].encode()).hexdigest()
    assert login["session_token"] not in row
    assert login["csrf_token"] not in row

    context = service.validate_session(
        login["session_token"],
        csrf_token=login["csrf_token"],
        require_csrf=True,
        now=NOW + timedelta(minutes=1),
    )
    assert context["user"]["id"] == member["id"]
    with pytest.raises(account_backend.AuthorizationError, match="CSRF"):
        service.validate_session(
            login["session_token"],
            csrf_token="wrong-csrf-value-long-enough",
            require_csrf=True,
            now=NOW + timedelta(minutes=1),
        )
    with pytest.raises(account_backend.AuthenticationError, match="expired session"):
        service.validate_session(
            login["session_token"], now=NOW + timedelta(minutes=11)
        )

    errors = []
    for email, password in (
        ("unknown@example.com", PASSWORD),
        ("person@example.com", "wrong password is long"),
        ("not-an-email", PASSWORD),
    ):
        with pytest.raises(account_backend.AuthenticationError) as caught:
            service.authenticate(email, password, now=NOW)
        errors.append(str(caught.value))
    assert errors == ["invalid email or password"] * 3


def test_change_password_revokes_every_session(tmp_path):
    service = make_service(tmp_path)
    user = service.create_user(
        "person@example.com", PASSWORD, access_state="approved", now=NOW
    )
    current = service.authenticate("person@example.com", PASSWORD, now=NOW)
    other = service.authenticate("person@example.com", PASSWORD, now=NOW)

    service.change_password(
        user["id"],
        PASSWORD,
        "new password with enough length",
        current_session_token=current["session_token"],
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(account_backend.AuthenticationError):
        service.validate_session(
            current["session_token"], now=NOW + timedelta(minutes=2)
        )
    with pytest.raises(account_backend.AuthenticationError):
        service.validate_session(other["session_token"], now=NOW + timedelta(minutes=2))
    with pytest.raises(
        account_backend.AuthenticationError, match="invalid email or password"
    ):
        service.authenticate("person@example.com", PASSWORD, now=NOW)
    assert (
        service.authenticate(
            "person@example.com", "new password with enough length", now=NOW
        )["user"]["id"]
        == user["id"]
    )


def test_password_reset_is_generic_one_time_hashed_and_revokes_sessions(tmp_path):
    sender = account_backend.InMemoryPasswordResetSender()
    service = make_service(tmp_path, sender=sender)
    user = service.create_user(
        "person@example.com", PASSWORD, access_state="approved", now=NOW
    )
    login = service.authenticate("person@example.com", PASSWORD, now=NOW)

    unknown = service.request_password_reset("missing@example.com", now=NOW)
    known = service.request_password_reset("person@example.com", now=NOW)
    assert unknown == known == {"accepted": True}
    assert len(sender.messages) == 1
    token = reset_token(sender.messages[0])
    with sqlite3.connect(service.db_path) as conn:
        stored = conn.execute(
            "SELECT token_hash, delivery_state FROM account_password_resets"
        ).fetchone()
    assert stored == (hashlib.sha256(token.encode()).hexdigest(), "sent")
    assert token not in stored

    result = service.reset_password(
        token, "new reset password long enough", now=NOW + timedelta(minutes=20)
    )
    assert result["id"] == user["id"]
    with pytest.raises(account_backend.AuthenticationError):
        service.reset_password(
            token, "another reset password long enough", now=NOW + timedelta(minutes=21)
        )
    with pytest.raises(account_backend.AuthenticationError):
        service.validate_session(
            login["session_token"], now=NOW + timedelta(minutes=21)
        )
    assert service.authenticate(
        "person@example.com", "new reset password long enough", now=NOW
    )


def test_expired_or_failed_reset_token_cannot_change_password(tmp_path):
    sender = account_backend.InMemoryPasswordResetSender()
    service = make_service(tmp_path, sender=sender)
    service.create_user("person@example.com", PASSWORD, now=NOW)
    service.request_password_reset("person@example.com", reset_ttl=300, now=NOW)
    token = reset_token(sender.messages[0])

    with pytest.raises(
        account_backend.AuthenticationError, match="expired reset token"
    ):
        service.reset_password(
            token, "new reset password long enough", now=NOW + timedelta(minutes=6)
        )

    disabled_service = account_backend.AccountService(
        tmp_path / "disabled.db",
        reset_base_url="http://localhost/reset",
    )
    disabled_service.create_user("other@example.com", PASSWORD, now=NOW)
    assert disabled_service.request_password_reset("other@example.com", now=NOW) == {
        "accepted": True
    }
    with sqlite3.connect(disabled_service.db_path) as conn:
        assert conn.execute(
            "SELECT delivery_state, used_at IS NOT NULL FROM account_password_resets"
        ).fetchone() == ("failed", 1)


def test_inventory_crud_ownership_soft_archive_and_profit(tmp_path):
    service = make_service(tmp_path)
    owner = service.create_user(
        "owner@example.com", PASSWORD, access_state="approved", now=NOW
    )
    stranger = service.create_user(
        "stranger@example.com", PASSWORD, access_state="approved", now=NOW
    )
    service.update_profile(
        owner["id"], stale_after_days=7, default_fee_percent=10, now=NOW
    )
    item = service.create_inventory_item(
        owner["id"],
        skin_name="AK-47 | Slate (Field-Tested)",
        quantity=2,
        buy_price_cents=1_000,
        bought_at=NOW - timedelta(days=30),
        listed_price_cents=1_400,
        listed_at=NOW - timedelta(days=8),
        stickers="Crown (Foil), Katowice 2014",
        strategy="flip",
        now=NOW,
    )

    assert item["status"] == "listed"
    assert item["fee_percent"] == 10
    assert item["stickers"] == [
        {"name": "Crown (Foil)"},
        {"name": "Katowice 2014"},
    ]
    assert (
        service.get_inventory_item(owner["id"], item["id"], now=NOW)["analysis"][
            "is_stale"
        ]
        is True
    )
    for operation in (
        lambda: service.get_inventory_item(stranger["id"], item["id"]),
        lambda: service.update_inventory_item(
            stranger["id"], item["id"], notes="steal"
        ),
        lambda: service.archive_inventory_item(stranger["id"], item["id"]),
    ):
        with pytest.raises(account_backend.NotFoundError):
            operation()

    updated = service.update_inventory_item(
        owner["id"],
        item["id"],
        expected_revision=item["revision"],
        notes="price checked",
        now=NOW,
    )
    assert updated["revision"] == 2
    with pytest.raises(account_backend.ConflictError):
        service.update_inventory_item(
            owner["id"],
            item["id"],
            expected_revision=item["revision"],
            notes="stale browser tab",
            now=NOW,
        )

    sold = service.mark_inventory_sold(
        owner["id"],
        item["id"],
        1_500,
        expected_revision=updated["revision"],
        sold_at=NOW,
        now=NOW,
    )
    assert sold["analysis"]["profit_cents"] == 700
    assert sold["analysis"]["roi_percent"] == 35
    archived = service.delete_inventory_item(owner["id"], item["id"], now=NOW)
    assert archived["status"] == "archived"
    assert service.get_inventory_item(owner["id"], item["id"])["status"] == "archived"
    restored = service.restore_inventory_item(owner["id"], item["id"], now=NOW)
    assert restored["status"] == "sold"

    summary = service.inventory_summary(owner["id"], now=NOW)
    assert summary["sold_count"] == 2
    assert summary["realized_profit_cents"] == 700
    events = service.list_activity(owner["id"], inventory_item_id=item["id"])
    assert {event["event_type"] for event in events} >= {
        "inventory_created",
        "inventory_updated",
        "inventory_sold",
        "inventory_archived",
        "inventory_restored",
    }
    assert service.list_activity(stranger["id"], inventory_item_id=item["id"]) == []


def test_sale_can_be_corrected_and_undone_with_revision_ownership_and_audit(
    tmp_path,
):
    service = make_service(tmp_path)
    owner = service.create_user(
        "sale-owner@example.com", PASSWORD, access_state="approved", now=NOW
    )
    stranger = service.create_user(
        "sale-stranger@example.com", PASSWORD, access_state="approved", now=NOW
    )
    item = service.create_inventory_item(
        owner["id"],
        skin_name="AK-47 | Slate (Field-Tested)",
        quantity=2,
        buy_price_cents=1_000,
        listed_price_cents=1_400,
        listed_at=NOW - timedelta(days=2),
        fee_percent=10,
        now=NOW,
    )
    sold = service.mark_inventory_sold(
        owner["id"],
        item["id"],
        1_500,
        sold_at=NOW,
        fee_percent=10,
        expected_revision=item["revision"],
        now=NOW,
    )
    assert sold["analysis"]["profit_cents"] == 700

    corrected = service.update_inventory_sale(
        owner["id"],
        item["id"],
        1_700,
        sold_at=NOW + timedelta(days=1),
        fee_percent=5,
        expected_revision=sold["revision"],
        now=NOW + timedelta(days=1),
    )
    assert corrected["revision"] == sold["revision"] + 1
    assert corrected["sold_price_cents"] == 1_700
    assert corrected["sold_at"].startswith("2026-08-02")
    assert corrected["fee_percent"] == 5
    assert corrected["analysis"]["profit_cents"] == 1_230
    assert corrected["analysis"]["roi_percent"] == 61.5
    summary = service.inventory_summary(owner["id"], now=NOW)
    assert summary["sold_count"] == 2
    assert summary["realized_profit_cents"] == 1_230

    with pytest.raises(account_backend.ConflictError):
        service.update_inventory_sale(
            owner["id"],
            item["id"],
            1_800,
            expected_revision=sold["revision"],
            now=NOW,
        )
    for operation in (
        lambda: service.update_inventory_sale(
            stranger["id"], item["id"], 1_800, now=NOW
        ),
        lambda: service.undo_inventory_sale(stranger["id"], item["id"], now=NOW),
    ):
        with pytest.raises(account_backend.NotFoundError):
            operation()

    restored = service.undo_inventory_sale(
        owner["id"],
        item["id"],
        expected_revision=corrected["revision"],
        now=NOW + timedelta(days=2),
    )
    assert restored["revision"] == corrected["revision"] + 1
    assert restored["status"] == "listed"
    assert restored["listed_price_cents"] == 1_400
    assert restored["sold_price_cents"] is None
    assert restored["sold_at"] is None
    summary = service.inventory_summary(owner["id"], now=NOW)
    assert summary["active_count"] == 2
    assert summary["sold_count"] == 0
    assert summary["realized_profit_cents"] == 0
    with pytest.raises(account_backend.ValidationError, match="only a sold item"):
        service.undo_inventory_sale(owner["id"], item["id"], now=NOW)
    resold = service.mark_inventory_sold(
        owner["id"],
        item["id"],
        1_500,
        expected_revision=restored["revision"],
        now=NOW,
    )
    with pytest.raises(account_backend.ValidationError, match="owned or listed"):
        service.mark_inventory_sold(
            owner["id"],
            item["id"],
            1_600,
            expected_revision=resold["revision"],
            now=NOW,
        )

    events = service.list_activity(owner["id"], inventory_item_id=item["id"])
    by_type = {event["event_type"]: event["payload"] for event in events}
    assert by_type["inventory_sold"]["previous_status"] == "listed"
    assert by_type["inventory_sale_updated"]["before"]["profit_cents"] == 700
    assert by_type["inventory_sale_updated"]["after"]["profit_cents"] == 1_230
    assert by_type["inventory_sale_undone"]["restored_status"] == "listed"
    assert by_type["inventory_sale_undone"]["previous_sale"]["profit_cents"] == 1_230


def test_undo_sale_uses_transition_audit_for_legacy_sold_event(tmp_path):
    service = make_service(tmp_path)
    owner = service.create_user(
        "legacy-sale@example.com", PASSWORD, access_state="approved", now=NOW
    )
    owned = service.create_inventory_item(
        owner["id"],
        skin_name="M4A1-S | Night Terror (Field-Tested)",
        buy_price_cents=800,
        now=NOW,
    )
    sold = service.mark_inventory_sold(
        owner["id"],
        owned["id"],
        1_000,
        expected_revision=owned["revision"],
        now=NOW,
    )
    # A sold row can carry stale listing metadata.  That must not override the
    # immutable owned -> sold transition when a legacy semantic event omitted
    # previous_status.
    sold_with_stale_listing = service.update_inventory_item(
        owner["id"],
        owned["id"],
        expected_revision=sold["revision"],
        listed_price_cents=1_100,
        listed_at=NOW,
        now=NOW,
    )
    with sqlite3.connect(service.db_path) as conn:
        conn.execute(
            """
            INSERT INTO account_activity(
                user_id, inventory_item_id, event_type, occurred_at, payload_json
            ) VALUES (?, ?, 'inventory_sold', ?, '{}')
            """,
            (owner["id"], owned["id"], NOW.isoformat()),
        )

    restored = service.undo_inventory_sale(
        owner["id"],
        owned["id"],
        expected_revision=sold_with_stale_listing["revision"],
        now=NOW,
    )

    assert restored["status"] == "owned"
    assert restored["listed_price_cents"] is None
    assert restored["sold_price_cents"] is None


def test_undo_sale_uses_latest_transition_if_semantic_event_is_stale(tmp_path):
    service = make_service(tmp_path)
    owner = service.create_user(
        "stale-sale-event@example.com", PASSWORD, access_state="approved", now=NOW
    )
    item = service.create_inventory_item(
        owner["id"],
        skin_name="M4A1-S | Printstream (Field-Tested)",
        buy_price_cents=1_000,
        now=NOW,
    )
    first_sale = service.mark_inventory_sold(
        owner["id"], item["id"], 1_200, expected_revision=item["revision"], now=NOW
    )
    owned = service.undo_inventory_sale(
        owner["id"],
        item["id"],
        expected_revision=first_sale["revision"],
        now=NOW,
    )
    listed = service.mark_inventory_listed(
        owner["id"],
        item["id"],
        1_400,
        expected_revision=owned["revision"],
        now=NOW,
    )

    # Model an interrupted legacy writer: the atomic listed -> sold update was
    # committed, but the semantic inventory_sold event from this cycle was not.
    legacy_sale = service.update_inventory_item(
        owner["id"],
        item["id"],
        expected_revision=listed["revision"],
        status="sold",
        sold_price_cents=1_500,
        sold_at=NOW,
        now=NOW,
    )
    restored = service.undo_inventory_sale(
        owner["id"],
        item["id"],
        expected_revision=legacy_sale["revision"],
        now=NOW,
    )

    assert restored["status"] == "listed"
    assert restored["listed_price_cents"] == 1_400
    assert restored["sold_price_cents"] is None


def test_listing_recommendation_can_flag_below_break_even():
    item = {
        "quantity": 1,
        "buy_price_cents": 1_000,
        "bought_at": NOW - timedelta(days=40),
        "status": "listed",
        "listed_price_cents": 1_100,
        "listed_at": NOW - timedelta(days=20),
        "market_price_cents": 900,
        "fee_percent": 10,
    }
    result = account_backend.analyze_inventory_item(
        item, now=NOW, stale_after_days=7, target_roi_percent=10
    )

    assert result["break_even_price_cents"] == 1_112
    assert result["suggested_price_cents"] == 899
    assert result["suggestion_reason"] == "stale_listing_markdown"
    assert result["suggestion_below_break_even"] is True
    assert result["is_stale"] is True


def test_quantity_fee_rounding_is_applied_per_separate_item():
    result = account_backend.analyze_inventory_item(
        {
            "quantity": 3,
            "buy_price_cents": 50,
            "bought_at": NOW,
            "status": "sold",
            "sold_price_cents": 101,
            "fee_percent": 4,
        },
        now=NOW,
    )

    assert result["net_value_cents"] == 96 * 3
    assert result["profit_cents"] == 138


def test_development_outbox_is_opt_in_and_private(tmp_path):
    outbox = tmp_path / "mail" / "resets.jsonl"
    sender = account_backend.password_reset_sender_from_env(
        env={"CS2_ACCOUNT_DEV_EMAIL_OUTBOX": str(outbox)}
    )
    sender.send_password_reset(
        account_backend.PasswordResetEmail(
            recipient="person@example.com",
            reset_url="http://localhost/reset?token=secret",
            expires_at=NOW.isoformat(),
        )
    )

    assert json.loads(outbox.read_text())["recipient"] == "person@example.com"
    if os.name == "posix":
        assert outbox.stat().st_mode & 0o777 == 0o600
    assert isinstance(
        account_backend.password_reset_sender_from_env(env={}),
        account_backend.DisabledPasswordResetSender,
    )
    ssl_sender = account_backend.password_reset_sender_from_env(
        env={
            "CS2_ACCOUNT_SMTP_HOST": "smtp.example.com",
            "CS2_ACCOUNT_SMTP_SSL": "true",
            "CS2_ACCOUNT_SMTP_FROM": "noreply@example.com",
        }
    )
    assert ssl_sender.use_ssl is True
    assert ssl_sender.starttls is False
    with pytest.raises(account_backend.ValidationError, match="HTTPS"):
        account_backend.build_password_reset_url(
            "http://public.example/reset", "long-enough-reset-token"
        )
