import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import account_backend

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
PASSWORD = "correct horse battery staple"


@pytest.fixture
def accounts(tmp_path):
    service = account_backend.AccountService(tmp_path / "accounts.db")
    admin = service.create_user("admin@example.com", PASSWORD, role="admin", now=NOW)
    member = service.create_user(
        "member@example.com", PASSWORD, access_state="approved", now=NOW
    )
    other = service.create_user(
        "other@example.com", PASSWORD, access_state="approved", now=NOW
    )
    yield service, admin, member, other
    service.close()


def download_summary(service, user_id):
    return service.admin_get_user(user_id, trusted_local_admin=True, now=NOW)[
        "database_downloads"
    ]


def test_progress_counts_bytes_sent_and_preserves_account_revision(accounts):
    service, admin, member, _ = accounts
    download_id = service.start_database_download(
        member["id"], ip_address="2001:0db8::1", now=NOW
    )
    summary = download_summary(service, member["id"])
    assert summary["active_count"] == 1
    assert summary["total_bytes"] == 0
    assert summary["recent"][0]["status"] == "preparing"
    assert summary["recent"][0]["total_bytes"] is None

    service.update_database_download(
        download_id,
        status="downloading",
        bytes_sent=256,
        total_bytes=1024,
        filename=r"C:\private\snapshots\export.db",
        now=NOW + timedelta(seconds=1),
    )
    # A stale callback cannot reduce persisted progress or restart preparation.
    service.update_database_download(
        download_id, status="preparing", bytes_sent=64, now=NOW
    )
    summary = download_summary(service, member["id"])
    assert summary["total_bytes"] == 256
    assert summary["completed_count"] == 0
    record = summary["recent"][0]
    assert record == {
        "id": download_id,
        "status": "downloading",
        "bytes_sent": 256,
        "total_bytes": 1024,
        "filename": "export.db",
        "started_at": NOW.isoformat(),
        "updated_at": (NOW + timedelta(seconds=1)).isoformat(),
        "finished_at": None,
        "ip_address": "2001:db8::1",
    }
    assert service.get_user(member["id"])["revision"] == member["revision"]
    listing = service.admin_list_users(actor_user_id=admin["id"], now=NOW)
    assert listing["summary"]["downloading"] == 1
    assert "private" not in json.dumps(listing)


def test_totals_include_interrupted_downloads_and_isolate_users(accounts):
    service, admin, member, other = accounts
    completed = service.start_database_download(member["id"], now=NOW)
    service.update_database_download(
        completed, status="completed", bytes_sent=1000, total_bytes=1000, now=NOW
    )
    interrupted = service.start_database_download(member["id"], now=NOW)
    service.update_database_download(
        interrupted, status="interrupted", bytes_sent=300, total_bytes=1000, now=NOW
    )
    pending = service.start_database_download(member["id"], now=NOW)
    other_id = service.start_database_download(other["id"], now=NOW)
    service.update_database_download(
        other_id, status="downloading", bytes_sent=700, total_bytes=1200, now=NOW
    )
    unattributed = service.start_database_download(ip_address="203.0.113.8", now=NOW)
    service.update_database_download(
        unattributed, status="downloading", bytes_sent=125, total_bytes=900, now=NOW
    )
    failed = service.start_database_download(now=NOW)
    service.update_database_download(failed, status="failed", now=NOW)

    summary = download_summary(service, member["id"])
    assert summary["total_bytes"] == 1300
    assert summary["completed_count"] == 1
    assert summary["active_count"] == 1
    assert summary["recent"][0]["id"] == pending
    assert {row["id"] for row in summary["recent"]} == {
        completed, interrupted, pending
    }
    assert download_summary(service, other["id"])["total_bytes"] == 700
    # Global activity and legacy-token downloads remain visible with a filter.
    listing = service.admin_list_users(
        actor_user_id=admin["id"], query="member@", now=NOW
    )
    assert listing["summary"]["downloading"] == 3
    assert listing["unattributed_downloads"]["total_bytes"] == 125
    assert listing["unattributed_downloads"]["active_count"] == 1
    assert listing["unattributed_downloads"]["completed_count"] == 0
    assert listing["unattributed_downloads"]["recent"][0]["id"] == unattributed


def test_active_downloads_remain_visible_ahead_of_limited_history(accounts):
    service, _, member, _ = accounts
    active = service.start_database_download(member["id"], now=NOW)
    for seconds in range(1, 9):
        download_id = service.start_database_download(
            member["id"], now=NOW + timedelta(seconds=seconds)
        )
        service.update_database_download(
            download_id, status="completed", bytes_sent=10, total_bytes=10, now=NOW
        )
    summary = download_summary(service, member["id"])
    assert len(summary["recent"]) == 5
    assert summary["recent"][0]["id"] == active
    assert summary["total_bytes"] == 80
    assert summary["completed_count"] == 8
    assert [row["id"] for row in summary["recent"][1:]] == list(
        range(active + 8, active + 4, -1)
    )


def test_current_downloaders_remain_visible_on_first_users_page(accounts):
    service, admin, _, _ = accounts
    first_pending = service.create_user("pending-first@example.com", PASSWORD, now=NOW)
    latest_pending = service.create_user(
        "pending-latest@example.com", PASSWORD, now=NOW + timedelta(seconds=1)
    )

    def first_page():
        return service.admin_list_users(actor_user_id=admin["id"], limit=1, now=NOW)

    assert first_page()["users"][0]["id"] == latest_pending["id"]
    download_id = service.start_database_download(admin["id"], now=NOW)
    preparing_page = first_page()
    assert preparing_page["users"][0]["id"] == admin["id"]
    assert preparing_page["total"] == 5
    service.update_database_download(
        download_id, status="downloading", bytes_sent=5, total_bytes=10, now=NOW
    )
    assert first_page()["users"][0]["id"] == admin["id"]
    next_page = service.admin_list_users(
        actor_user_id=admin["id"], limit=2, offset=1, now=NOW
    )
    assert [user["id"] for user in next_page["users"]] == [
        latest_pending["id"], first_pending["id"]
    ]
    service.update_database_download(
        download_id, status="completed", bytes_sent=10, now=NOW
    )
    assert first_page()["users"][0]["id"] == latest_pending["id"]


def test_concurrent_updates_never_reduce_bytes_and_terminal_state_is_final(accounts):
    service, _, member, _ = accounts
    download_id = service.start_database_download(member["id"], now=NOW)

    def report(sent):
        service.update_database_download(
            download_id,
            status="downloading",
            bytes_sent=sent,
            total_bytes=1000,
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(report, [400, 100, 900, 600, 200]))
    assert download_summary(service, member["id"])["total_bytes"] == 900
    service.update_database_download(
        download_id, status="completed", bytes_sent=1000, now=NOW
    )
    expected = download_summary(service, member["id"])
    for status in ("downloading", "interrupted", "failed"):
        service.update_database_download(
            download_id, status=status, bytes_sent=0, now=NOW + timedelta(seconds=5)
        )
    assert download_summary(service, member["id"]) == expected
    assert expected["active_count"] == 0
    assert expected["completed_count"] == 1
    assert expected["recent"][0]["finished_at"] == NOW.isoformat()


def test_restart_preserves_counts_and_recovers_abandoned_downloads(accounts):
    service, _, member, _ = accounts
    active = service.start_database_download(member["id"], now=NOW)
    service.update_database_download(
        active, status="downloading", bytes_sent=321, total_bytes=1000, now=NOW
    )
    preparing = service.start_database_download(now=NOW)
    completed = service.start_database_download(member["id"], now=NOW)
    service.update_database_download(
        completed, status="completed", bytes_sent=500, total_bytes=500, now=NOW
    )
    before = download_summary(service, member["id"])
    finished = next(row for row in before["recent"] if row["id"] == completed)
    service.close()
    with account_backend.AccountService(service.db_path) as reopened:
        summary = download_summary(reopened, member["id"])
        assert summary["total_bytes"] == 821
        assert summary["active_count"] == 0
        assert summary["completed_count"] == 1
        records = {row["id"]: row for row in summary["recent"]}
        assert records[completed] == finished
        assert records[active]["status"] == "interrupted"
        assert records[active]["bytes_sent"] == 321
        assert records[active]["finished_at"] is not None
        listing = reopened.admin_list_users(trusted_local_admin=True)
        assert listing["summary"]["downloading"] == 0
        legacy = listing["unattributed_downloads"]["recent"][0]
        assert legacy["id"] == preparing
        assert legacy["status"] == "interrupted"


def test_download_details_are_admin_only_and_deleted_account_ips_are_erased(accounts):
    service, admin, member, other = accounts
    download_id = service.start_database_download(
        member["id"], ip_address="203.0.113.77", now=NOW
    )
    service.update_database_download(
        download_id, status="interrupted", bytes_sent=5, total_bytes=10, now=NOW
    )
    with pytest.raises(account_backend.AuthorizationError):
        service.admin_list_users(actor_user_id=other["id"], now=NOW)
    with pytest.raises(account_backend.AuthorizationError):
        service.admin_get_user(member["id"], actor_user_id=member["id"], now=NOW)
    assert "database_downloads" not in service.get_user(member["id"])
    session = service.authenticate(member["email"], PASSWORD, now=NOW)
    assert "database_downloads" not in session["user"]
    service.admin_soft_delete_user(member["id"], actor_user_id=admin["id"], now=NOW)
    summary = download_summary(service, member["id"])
    assert summary["total_bytes"] == 5
    assert summary["recent"][0]["ip_address"] is None
    with pytest.raises(account_backend.AccountAccessError):
        service.start_database_download(member["id"], now=NOW)


def test_invalid_progress_and_unapproved_accounts_do_not_create_bad_records(accounts):
    service, _, member, _ = accounts
    pending = service.create_user("pending@example.com", PASSWORD, now=NOW)
    with pytest.raises(account_backend.AccountAccessError):
        service.start_database_download(pending["id"], now=NOW)
    with pytest.raises(account_backend.NotFoundError):
        service.start_database_download(999999, now=NOW)
    download_id = service.start_database_download(
        member["id"], ip_address="untrusted garbage", now=NOW
    )
    assert download_summary(service, member["id"])["recent"][0]["ip_address"] is None
    for values in (
        {"status": "unknown"},
        {"status": "downloading", "bytes_sent": -1},
        {"status": "downloading", "bytes_sent": True},
        {"status": "downloading", "bytes_sent": 20, "total_bytes": 10},
        {"status": "completed", "bytes_sent": 5, "total_bytes": 10},
        {"status": "completed"},
    ):
        with pytest.raises(account_backend.ValidationError):
            service.update_database_download(download_id, **values, now=NOW)
    assert download_summary(service, member["id"])["recent"][0]["status"] == "preparing"
    with pytest.raises(account_backend.NotFoundError):
        service.update_database_download(999999, status="failed", now=NOW)


def test_v4_migration_is_additive_and_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE app_schema_versions "
            "(component TEXT PRIMARY KEY, version INTEGER, migrated_at TEXT)"
        )
        account_backend._migrate_v1(conn)
        account_backend._migrate_v2(conn)
        account_backend._migrate_v3(conn)
        conn.execute(
            "INSERT INTO app_schema_versions VALUES (?, 3, ?)",
            (account_backend.SCHEMA_COMPONENT, NOW.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO account_users(
                email, role, password_hash, access_state, created_at, updated_at
            ) VALUES ('legacy@example.com', 'admin', 'untouched', 'approved', ?, ?)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
    with account_backend.AccountService(path) as service:
        download_id = service.start_database_download(1, now=NOW)
        service.update_database_download(
            download_id, status="completed", bytes_sent=123, total_bytes=123, now=NOW
        )
    with account_backend.AccountService(path) as reopened:
        user = reopened.admin_get_user(1, trusted_local_admin=True, now=NOW)
        assert user["role"] == "admin"
        assert user["access_state"] == "approved"
        assert user["database_downloads"]["total_bytes"] == 123
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT version FROM app_schema_versions WHERE component = ?",
            (account_backend.SCHEMA_COMPONENT,),
        ).fetchone()[0] == account_backend.SCHEMA_VERSION
        assert conn.execute("SELECT password_hash FROM account_users").fetchone()[0] == "untouched"
