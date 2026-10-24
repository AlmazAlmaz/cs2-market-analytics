import sqlite3
from datetime import datetime, timezone

import pytest

import account_backend

NOW = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
PASSWORD = "correct horse battery staple"


def make_service(tmp_path):
    service = account_backend.AccountService(tmp_path / "private" / "accounts.db")
    owner = service.create_user(
        "trade-owner@example.com",
        PASSWORD,
        access_state="approved",
        now=NOW,
    )
    return service, owner


def create_item(
    service,
    user_id,
    name,
    *,
    quantity=1,
    buy_price_cents=1_000,
):
    return service.create_inventory_item(
        user_id,
        skin_name=name,
        quantity=quantity,
        buy_price_cents=buy_price_cents,
        bought_at=NOW,
        strategy="manual",
        now=NOW,
    )


def outgoing(item, *, quantity=None, unit_value_cents=1_000):
    return {
        "inventory_item_id": item["id"],
        "quantity": item["quantity"] if quantity is None else quantity,
        "expected_revision": item["revision"],
        "unit_value_cents": unit_value_cents,
    }


def incoming(name, *, quantity=1, unit_value_cents=1_000):
    return {
        "skin_name": name,
        "quantity": quantity,
        "unit_value_cents": unit_value_cents,
    }


def database_state(service):
    """Return all trade-relevant mutable rows for atomicity assertions."""

    with sqlite3.connect(service.db_path) as conn:
        inventory = conn.execute(
            """
            SELECT id, user_id, skin_name, quantity, buy_price_cents,
                   cost_basis_cents, status, revision, notes
            FROM account_inventory_items
            ORDER BY id
            """
        ).fetchall()
        trades = conn.execute(
            """
            SELECT id, user_id, traded_at, notes, status, revision,
                   current_version, voided_at
            FROM account_inventory_trades
            ORDER BY id
            """
        ).fetchall()
        legs = conn.execute(
            """
            SELECT id, trade_id, user_id, trade_version, direction,
                   inventory_item_id, source_inventory_item_id, quantity,
                   unit_value_cents, cost_basis_cents, inventory_revision,
                   source_revision
            FROM account_inventory_trade_legs
            ORDER BY id
            """
        ).fetchall()
        activity = conn.execute(
            """
            SELECT id, user_id, inventory_item_id, event_type, payload_json
            FROM account_activity
            ORDER BY id
            """
        ).fetchall()
    return inventory, trades, legs, activity


def create_v2_database(path):
    """Create a genuine pre-trade account schema without invoking v3."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            CREATE TABLE app_schema_versions (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                migrated_at TEXT NOT NULL
            )
            """
        )
        account_backend._migrate_v1(conn)
        account_backend._migrate_v2(conn)
        timestamp = NOW.isoformat()
        user_id = conn.execute(
            """
            INSERT INTO account_users(
                email, display_name, role, password_hash,
                stale_after_days, default_fee_percent,
                created_at, updated_at, access_state, access_changed_at
            ) VALUES (?, ?, 'member', ?, 14, 4, ?, ?, 'approved', ?)
            """,
            (
                "legacy-trader@example.com",
                "Legacy trader",
                "test-password-hash",
                timestamp,
                timestamp,
                timestamp,
            ),
        ).lastrowid
        item_id = conn.execute(
            """
            INSERT INTO account_inventory_items(
                user_id, skin_name, quantity, buy_price_cents, bought_at,
                strategy, status, fee_percent, notes, revision,
                created_at, updated_at
            ) VALUES (?, ?, 3, 1001, ?, 'manual', 'owned', 4,
                      'keep this legacy row exactly', 7, ?, ?)
            """,
            (
                user_id,
                "AWP | Graphite (Factory New)",
                timestamp,
                timestamp,
                timestamp,
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO app_schema_versions(component, version, migrated_at)
            VALUES (?, 2, ?)
            """,
            (account_backend.SCHEMA_COMPONENT, timestamp),
        )
    return int(user_id), int(item_id)


def test_v3_migration_is_additive_backfills_exact_basis_and_is_idempotent(tmp_path):
    path = tmp_path / "legacy-v2.db"
    user_id, item_id = create_v2_database(path)

    service = account_backend.AccountService(path)
    service.close()
    # A second startup must be a no-op for both schema and legacy data.
    account_backend.AccountService(path).close()

    with sqlite3.connect(path) as conn:
        assert account_backend.SCHEMA_VERSION >= 3
        assert conn.execute(
            "SELECT version FROM app_schema_versions WHERE component = ?",
            (account_backend.SCHEMA_COMPONENT,),
        ).fetchone()[0] == account_backend.SCHEMA_VERSION
        assert {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        } >= {
            "account_inventory_items",
            "account_inventory_trades",
            "account_inventory_trade_legs",
        }
        assert "cost_basis_cents" in {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(account_inventory_items)"
            ).fetchall()
        }
        assert conn.execute(
            """
            SELECT user_id, skin_name, quantity, buy_price_cents,
                   cost_basis_cents, status, notes, revision
            FROM account_inventory_items WHERE id = ?
            """,
            (item_id,),
        ).fetchone() == (
            user_id,
            "AWP | Graphite (Factory New)",
            3,
            1_001,
            3_003,
            "owned",
            "keep this legacy row exactly",
            7,
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM account_inventory_trades"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM account_inventory_trade_legs"
        ).fetchone()[0] == 0
        assert {
            row[0]
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND name LIKE 'trg_account_trade_legs_%'
                """
            )
        } == {
            "trg_account_trade_legs_no_update",
            "trg_account_trade_legs_no_delete",
        }


def test_create_two_for_two_partial_trade_preserves_every_cost_basis_cent(tmp_path):
    service, owner = make_service(tmp_path)
    awp = create_item(
        service,
        owner["id"],
        "AWP | Graphite (Factory New)",
        quantity=3,
        buy_price_cents=1_001,
    )
    ak = create_item(
        service,
        owner["id"],
        "AK-47 | Slate (Field-Tested)",
        quantity=2,
        buy_price_cents=499,
    )

    trade = service.create_inventory_trade(
        owner["id"],
        outgoing=[
            outgoing(awp, quantity=2, unit_value_cents=1_200),
            outgoing(ak, quantity=1, unit_value_cents=800),
        ],
        incoming=[
            incoming("Kilowatt Case", quantity=3, unit_value_cents=600),
            incoming(
                "Paris 2023 Contenders Sticker Capsule",
                quantity=2,
                unit_value_cents=350,
            ),
        ],
        traded_at=NOW,
        notes="two skins for cases and capsules",
        now=NOW,
    )

    assert trade["status"] == "active"
    assert trade["revision"] == 1
    assert trade["current_version"] == 1
    assert trade["notes"] == "two skins for cases and capsules"
    assert len(trade["outgoing"]) == 2
    assert len(trade["incoming"]) == 2
    assert trade["totals"] == {
        "outgoing_value_cents": 3_200,
        "incoming_value_cents": 2_500,
        "value_difference_cents": -700,
        "outgoing_cost_basis_cents": 2_501,
        "incoming_cost_basis_cents": 2_501,
    }

    outgoing_basis = sum(row["cost_basis_cents"] for row in trade["outgoing"])
    incoming_basis = sum(row["cost_basis_cents"] for row in trade["incoming"])
    assert outgoing_basis == incoming_basis == 2_501
    # 2,501 cents distributed in declared-value proportions 1,800:700.
    assert [row["cost_basis_cents"] for row in trade["incoming"]] == [1_801, 700]

    remaining_awp = service.get_inventory_item(
        owner["id"], awp["id"], include_analysis=False
    )
    remaining_ak = service.get_inventory_item(
        owner["id"], ak["id"], include_analysis=False
    )
    assert (remaining_awp["quantity"], remaining_awp["cost_basis_cents"]) == (
        1,
        1_001,
    )
    assert (remaining_ak["quantity"], remaining_ak["cost_basis_cents"]) == (1, 499)

    received = [
        service.get_inventory_item(
            owner["id"], row["inventory_item_id"], include_analysis=False
        )
        for row in trade["incoming"]
    ]
    assert [(row["quantity"], row["cost_basis_cents"]) for row in received] == [
        (3, 1_801),
        (2, 700),
    ]
    # Legacy unit prices necessarily round here; the exact ledger must not.
    assert sum(row["buy_price_cents"] * row["quantity"] for row in received) == 2_500
    assert sum(row["cost_basis_cents"] for row in received) == 2_501
    assert service.inventory_summary(owner["id"], now=NOW)["invested_cents"] == 4_001

    listed = service.list_inventory_trades(owner["id"], status="active", now=NOW)
    assert [row["id"] for row in listed] == [trade["id"]]

    with sqlite3.connect(service.db_path) as conn:
        leg_id = conn.execute(
            "SELECT id FROM account_inventory_trade_legs ORDER BY id LIMIT 1"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE account_inventory_trade_legs SET quantity = 99 WHERE id = ?",
                (leg_id,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM account_inventory_trade_legs WHERE id = ?", (leg_id,)
            )


def test_update_creates_new_version_and_soft_void_rolls_back_current_effects(tmp_path):
    service, owner = make_service(tmp_path)
    source = create_item(
        service,
        owner["id"],
        "M4A1-S | Printstream (Field-Tested)",
        quantity=4,
        buy_price_cents=101,
    )
    created = service.create_inventory_trade(
        owner["id"],
        outgoing=[outgoing(source, quantity=3, unit_value_cents=200)],
        incoming=[incoming("Kilowatt Case", quantity=2, unit_value_cents=300)],
        traded_at=NOW,
        notes="first version",
        now=NOW,
    )
    old_incoming_id = created["incoming"][0]["inventory_item_id"]
    remaining = service.get_inventory_item(
        owner["id"], source["id"], include_analysis=False
    )

    updated = service.update_inventory_trade(
        owner["id"],
        created["id"],
        outgoing=[outgoing(remaining, quantity=1, unit_value_cents=250)],
        incoming=[
            incoming(
                "Paris 2023 Legends Sticker Capsule",
                quantity=1,
                unit_value_cents=250,
            )
        ],
        traded_at=NOW,
        notes="corrected version",
        expected_revision=created["revision"],
        now=NOW,
    )

    assert updated["revision"] == 2
    assert updated["current_version"] == 2
    assert updated["notes"] == "corrected version"
    assert updated["totals"]["outgoing_cost_basis_cents"] == 101
    assert updated["totals"]["incoming_cost_basis_cents"] == 101
    current_source = service.get_inventory_item(
        owner["id"], source["id"], include_analysis=False
    )
    assert (current_source["quantity"], current_source["cost_basis_cents"]) == (
        3,
        303,
    )
    assert service.get_inventory_item(
        owner["id"], old_incoming_id, include_analysis=False
    )["status"] == "archived"
    current_incoming_id = updated["incoming"][0]["inventory_item_id"]
    assert service.get_inventory_item(
        owner["id"], current_incoming_id, include_analysis=False
    )["status"] == "owned"

    voided = service.void_inventory_trade(
        owner["id"],
        updated["id"],
        expected_revision=updated["revision"],
        now=NOW,
    )
    assert voided["status"] == "voided"
    assert voided["revision"] == 3
    assert voided["current_version"] == 2
    assert voided["voided_at"] is not None
    restored_source = service.get_inventory_item(
        owner["id"], source["id"], include_analysis=False
    )
    assert (
        restored_source["quantity"],
        restored_source["cost_basis_cents"],
        restored_source["status"],
    ) == (4, 404, "owned")
    assert service.get_inventory_item(
        owner["id"], current_incoming_id, include_analysis=False
    )["status"] == "archived"
    assert service.list_inventory_trades(owner["id"], status="active", now=NOW) == []
    assert [
        row["id"]
        for row in service.list_inventory_trades(
            owner["id"], status="voided", now=NOW
        )
    ] == [created["id"]]

    with sqlite3.connect(service.db_path) as conn:
        assert conn.execute(
            "SELECT status FROM account_inventory_trades WHERE id = ?",
            (created["id"],),
        ).fetchone()[0] == "voided"
        assert conn.execute(
            """
            SELECT trade_version, COUNT(*)
            FROM account_inventory_trade_legs
            WHERE trade_id = ?
            GROUP BY trade_version ORDER BY trade_version
            """,
            (created["id"],),
        ).fetchall() == [(1, 2), (2, 2)]
        parent_events = {
            row[0]
            for row in conn.execute(
                """
                SELECT event_type FROM account_activity
                WHERE user_id = ? AND inventory_item_id IS NULL
                """,
                (owner["id"],),
            )
        }
    assert parent_events >= {
        "inventory_trade_created",
        "inventory_trade_updated",
        "inventory_trade_voided",
    }


def test_trade_revision_and_tenant_boundaries_do_not_mutate_owner_state(tmp_path):
    service, owner = make_service(tmp_path)
    stranger = service.create_user(
        "trade-stranger@example.com",
        PASSWORD,
        access_state="approved",
        now=NOW,
    )
    owner_source = create_item(
        service, owner["id"], "AWP | Asiimov (Field-Tested)", quantity=2
    )
    stranger_source = create_item(
        service, stranger["id"], "Glock-18 | Gamma Doppler (Factory New)"
    )
    trade = service.create_inventory_trade(
        owner["id"],
        outgoing=[outgoing(owner_source, quantity=1)],
        incoming=[incoming("Kilowatt Case")],
        traded_at=NOW,
        now=NOW,
    )
    owner_remaining = service.get_inventory_item(
        owner["id"], owner_source["id"], include_analysis=False
    )

    before = database_state(service)
    with pytest.raises(account_backend.ConflictError):
        service.update_inventory_trade(
            owner["id"],
            trade["id"],
            outgoing=[outgoing(owner_remaining, quantity=1)],
            incoming=[incoming("Revolution Case")],
            expected_revision=trade["revision"] + 1,
            now=NOW,
        )
    assert database_state(service) == before

    assert service.list_inventory_trades(stranger["id"], now=NOW) == []
    for operation in (
        lambda: service.update_inventory_trade(
            stranger["id"],
            trade["id"],
            outgoing=[outgoing(stranger_source)],
            incoming=[incoming("Revolution Case")],
            expected_revision=trade["revision"],
            now=NOW,
        ),
        lambda: service.void_inventory_trade(
            stranger["id"],
            trade["id"],
            expected_revision=trade["revision"],
            now=NOW,
        ),
        lambda: service.create_inventory_trade(
            stranger["id"],
            outgoing=[outgoing(owner_remaining, quantity=1)],
            incoming=[incoming("Revolution Case")],
            now=NOW,
        ),
    ):
        with pytest.raises(account_backend.NotFoundError):
            operation()
        assert database_state(service) == before


def test_edit_or_void_rejects_a_changed_received_dependency_atomically(tmp_path):
    service, owner = make_service(tmp_path)
    source = create_item(
        service, owner["id"], "Desert Eagle | Printstream (Field-Tested)", quantity=2
    )
    trade = service.create_inventory_trade(
        owner["id"],
        outgoing=[outgoing(source, quantity=1)],
        incoming=[incoming("Kilowatt Case", quantity=2, unit_value_cents=500)],
        now=NOW,
    )
    received_id = trade["incoming"][0]["inventory_item_id"]
    received = service.get_inventory_item(
        owner["id"], received_id, include_analysis=False
    )
    service.update_inventory_item(
        owner["id"],
        received_id,
        expected_revision=received["revision"],
        notes="used or corrected after the trade",
        now=NOW,
    )
    source_after_trade = service.get_inventory_item(
        owner["id"], source["id"], include_analysis=False
    )
    before = database_state(service)

    for operation in (
        lambda: service.update_inventory_trade(
            owner["id"],
            trade["id"],
            outgoing=[outgoing(source_after_trade, quantity=1)],
            incoming=[incoming("Revolution Case")],
            expected_revision=trade["revision"],
            now=NOW,
        ),
        lambda: service.void_inventory_trade(
            owner["id"],
            trade["id"],
            expected_revision=trade["revision"],
            now=NOW,
        ),
    ):
        with pytest.raises(account_backend.ConflictError) as caught:
            operation()
        assert caught.value.code == "trade_dependency_conflict"
        assert database_state(service) == before


def test_void_rejects_an_incoming_item_used_by_a_later_trade(tmp_path):
    service, owner = make_service(tmp_path)
    original = create_item(
        service, owner["id"], "USP-S | Printstream (Field-Tested)"
    )
    first = service.create_inventory_trade(
        owner["id"],
        outgoing=[outgoing(original)],
        incoming=[incoming("Kilowatt Case", quantity=2, unit_value_cents=500)],
        now=NOW,
    )
    received = service.get_inventory_item(
        owner["id"], first["incoming"][0]["inventory_item_id"], include_analysis=False
    )
    service.create_inventory_trade(
        owner["id"],
        outgoing=[outgoing(received, quantity=1, unit_value_cents=500)],
        incoming=[incoming("Revolution Case", unit_value_cents=500)],
        now=NOW,
    )
    before = database_state(service)

    with pytest.raises(account_backend.ConflictError) as caught:
        service.void_inventory_trade(
            owner["id"],
            first["id"],
            expected_revision=first["revision"],
            now=NOW,
        )
    assert caught.value.code == "trade_dependency_conflict"
    assert database_state(service) == before


def test_invalid_payload_and_mid_write_failure_roll_back_every_trade_effect(tmp_path):
    service, owner = make_service(tmp_path)
    first = create_item(
        service, owner["id"], "AK-47 | Redline (Field-Tested)", quantity=2
    )
    second = create_item(
        service, owner["id"], "M4A4 | Temukau (Field-Tested)", quantity=1
    )
    initial = database_state(service)

    with pytest.raises(account_backend.ValidationError):
        service.create_inventory_trade(
            owner["id"],
            outgoing=[
                outgoing(first, quantity=1),
                outgoing(second, quantity=2),
            ],
            incoming=[incoming("Kilowatt Case")],
            now=NOW,
        )
    assert database_state(service) == initial

    with pytest.raises(account_backend.ValidationError):
        service.create_inventory_trade(
            owner["id"],
            outgoing=[outgoing(first, quantity=1)],
            incoming=[incoming("Kilowatt Case", unit_value_cents=0)],
            now=NOW,
        )
    assert database_state(service) == initial

    # Force SQLite to fail only after the service has entered its write path.
    # BEGIN IMMEDIATE must roll back the trade header, source split and audit.
    with sqlite3.connect(service.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_received_trade_item
            BEFORE INSERT ON account_inventory_items
            WHEN NEW.skin_name = 'Force transaction rollback'
            BEGIN
                SELECT RAISE(ABORT, 'forced received-item failure');
            END
            """
        )
    before_forced_failure = database_state(service)
    with pytest.raises(sqlite3.IntegrityError, match="forced received-item failure"):
        service.create_inventory_trade(
            owner["id"],
            outgoing=[outgoing(first, quantity=1)],
            incoming=[incoming("Force transaction rollback")],
            now=NOW,
        )
    assert database_state(service) == before_forced_failure
