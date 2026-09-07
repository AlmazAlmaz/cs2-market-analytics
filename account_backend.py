"""Accounts, sessions and a private CS2 inventory ledger.

This module contains no HTTP framework code.  ``dashboard_server.py`` can use
``AccountService`` from request handlers without giving the database raw
passwords or bearer tokens.  Money is represented as integer cents throughout
the public API; percentages are used only for the marketplace fee.

The password-reset sender is deliberately injected.  SMTP and an opt-in,
permission-restricted development outbox are provided below.  Reset URLs are
never written to the normal application log.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, Protocol, Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 3
SCHEMA_COMPONENT = "account_backend"
ROLES = frozenset({"admin", "member"})
ACCESS_STATES = frozenset({"pending", "approved", "blocked", "deleted"})
INVENTORY_STATUSES = frozenset({"owned", "listed", "sold", "archived"})
INVENTORY_STRATEGIES = frozenset(
    {
        "investment",
        "float",
        "stickers",
        "balanced",
        "manual",
        "flip",
        "collection",
    }
)
TRADE_STATUSES = frozenset({"active", "voided"})
TRADE_DIRECTIONS = frozenset({"outgoing", "incoming"})
MAX_TRADE_LEGS_PER_SIDE = 100

_INVENTORY_STORAGE_COLUMNS = (
    "skin_name",
    "quantity",
    "buy_price_cents",
    "bought_at",
    "float_value",
    "stickers_json",
    "source_url",
    "strategy",
    "status",
    "listed_price_cents",
    "listed_at",
    "sold_price_cents",
    "sold_at",
    "market_price_cents",
    "market_price_updated_at",
    "fee_percent",
    "notes",
    "cost_basis_cents",
)

DEFAULT_SESSION_TTL = timedelta(days=30)
DEFAULT_RESET_TTL = timedelta(hours=1)
DEFAULT_STUCK_AFTER_DAYS = 14
DEFAULT_TARGET_ROI_PERCENT = 10.0
MAX_ACTIVE_SESSIONS = 20
MAX_PASSWORD_RESETS_PER_HOUR = 3
SESSION_ONLINE_WINDOW = timedelta(minutes=5)
SESSION_TOUCH_INTERVAL = timedelta(minutes=1)

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")
_DUMMY_HASH: str | None = None
_DUMMY_HASH_LOCK = threading.Lock()


class AccountError(RuntimeError):
    """Base class for expected account-domain failures."""

    default_code = "account_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = str(code or self.default_code)


class ValidationError(AccountError, ValueError):
    """A caller supplied malformed or unsafe data."""

    default_code = "invalid_request"


class ConflictError(AccountError):
    """A unique value or optimistic revision conflicts with stored data."""

    default_code = "conflict"


class AuthenticationError(AccountError):
    """Credentials, a session or a reset token could not be authenticated."""

    default_code = "invalid_credentials"


class AccountAccessError(AuthenticationError):
    """Valid credentials/session belong to an account without site access."""

    _CODES: ClassVar[dict[str, str]] = {
        "pending": "access_pending",
        "blocked": "access_blocked",
        "deleted": "account_deleted",
    }

    def __init__(self, access_state: str) -> None:
        state = str(access_state or "").strip().lower()
        code = self._CODES.get(state, "access_denied")
        super().__init__("account access is not approved", code=code)
        self.access_state = state or "blocked"


class AuthorizationError(AccountError):
    """An authenticated user does not have the required role."""

    default_code = "forbidden"


class NotFoundError(AccountError):
    """A user-owned resource does not exist (or belongs to another user)."""

    default_code = "not_found"


class EmailDeliveryError(AccountError):
    """Password-reset delivery is not configured or failed."""

    default_code = "email_delivery_failed"


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return _utc_now(value).isoformat()


def _parse_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalise_optional_datetime(value: Any, *, field: str) -> str | None:
    if value in (None, ""):
        return None
    return _iso(_parse_datetime(value, field=field))


def _normalise_email(email: Any) -> str:
    text = unicodedata.normalize("NFKC", str(email or "")).strip().lower()
    if len(text) > 254 or not _EMAIL_RE.fullmatch(text) or text.count("@") != 1:
        raise ValidationError("email is invalid")
    local, domain = text.rsplit("@", 1)
    if not local or len(local) > 64 or not domain or domain.startswith("."):
        raise ValidationError("email is invalid")
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValidationError("email is invalid") from exc
    result = f"{local}@{ascii_domain}"
    if len(result) > 254:
        raise ValidationError("email is invalid")
    return result


def _normalise_ip_address(value: Any) -> str | None:
    """Return one canonical address, ignoring malformed/untrusted metadata."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        return ipaddress.ip_address(text).compressed
    except ValueError:
        return None


def _normalise_country_code(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if re.fullmatch(r"[A-Z]{2}", text) else None


def _normalise_user_agent(value: Any) -> str | None:
    text = "".join(
        character if character >= " " and character != "\x7f" else " "
        for character in str(value or "")
    ).strip()
    return text[:512] or None


def _request_metadata(
    *,
    ip_address: Any = None,
    country_code: Any = None,
    user_agent: Any = None,
) -> tuple[str | None, str | None, str | None]:
    """Normalise the privacy-minimal metadata kept on a login session.

    The service deliberately stores no request path, query, referrer or
    fingerprint.  Invalid network hints are discarded instead of making an
    otherwise valid login fail.
    """

    return (
        _normalise_ip_address(ip_address),
        _normalise_country_code(country_code),
        _normalise_user_agent(user_agent),
    )


def _validate_password(password: Any) -> str:
    if not isinstance(password, str):
        raise ValidationError("password must be a string")
    if "\x00" in password or len(password) < 10 or len(password) > 256:
        raise ValidationError("password must contain between 10 and 256 characters")
    if len(password.encode("utf-8")) > 1024:
        raise ValidationError("password is too long")
    return password


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash a password with stdlib scrypt and return a self-describing value."""

    password = _validate_password(password)
    salt_bytes = salt or secrets.token_bytes(16)
    if len(salt_bytes) < 16:
        raise ValidationError("password salt must contain at least 16 bytes")
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt_bytes,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            salt_bytes.hex(),
            digest.hex(),
        )
    )


def verify_password(password: Any, encoded_hash: Any) -> bool:
    """Return False for malformed hashes as well as incorrect passwords."""

    if not isinstance(password, str) or not isinstance(encoded_hash, str):
        return False
    if len(password) > 256 or len(password.encode("utf-8")) > 1024:
        return False
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded_hash.split("$")
        if algorithm != "scrypt":
            return False
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        # Never let a database value turn verification into a memory DoS.
        if n < 2**12 or n > 2**16 or r < 1 or r > 16 or p < 1 or p > 4:
            return False
        salt = bytes.fromhex(raw_salt)
        expected = bytes.fromhex(raw_digest)
        if len(salt) < 16 or len(expected) != _SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def _dummy_password_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        with _DUMMY_HASH_LOCK:
            if _DUMMY_HASH is None:
                _DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
    return _DUMMY_HASH


def _token_digest(token: Any) -> str:
    if not isinstance(token, str) or not 16 <= len(token) <= 512:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token(prefix: str) -> tuple[str, str]:
    raw = prefix + secrets.token_urlsafe(32)
    return raw, _token_digest(raw)


def _bounded_int(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be an integer") from exc
    if result < minimum or result > maximum:
        raise ValidationError(f"{field} must be between {minimum} and {maximum}")
    return result


def _optional_cents(value: Any, *, field: str) -> int | None:
    if value in (None, ""):
        return None
    return _bounded_int(value, field=field, minimum=1, maximum=100_000_000_00)


def _normalise_text(
    value: Any,
    *,
    field: str,
    maximum: int,
    required: bool = False,
) -> str | None:
    text = str(value or "").strip()
    if required and not text:
        raise ValidationError(f"{field} is required")
    if len(text) > maximum:
        raise ValidationError(f"{field} must contain at most {maximum} characters")
    return text or None


def _normalise_url(value: Any) -> str | None:
    text = _normalise_text(value, field="source_url", maximum=2_048)
    if not text:
        return None
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError("source_url must be an http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError("source_url must not contain credentials")
    return text


def _normalise_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("float_value must be a number") from exc
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValidationError("float_value must be between 0 and 1")
    return result


def _normalise_fee(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("fee_percent must be a number") from exc
    if not math.isfinite(result) or not 0 <= result <= 50:
        raise ValidationError("fee_percent must be between 0 and 50")
    return round(result, 4)


def _normalise_stickers(value: Any) -> str:
    stickers = [] if value in (None, "") else value
    if isinstance(stickers, str):
        try:
            stickers = json.loads(stickers)
        except json.JSONDecodeError:
            # The account form is intentionally friendly: comma/newline
            # separated names become structured entries.  JSON remains the
            # lossless path for sticker prices, positions and wear metadata.
            stickers = [
                {"name": name.strip()}
                for name in re.split(r"[,\n;]+", value)
                if name.strip()
            ]
    if not isinstance(stickers, list) or len(stickers) > 20:
        raise ValidationError("stickers must be an array with at most 20 entries")
    encoded = json.dumps(
        stickers, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(encoded.encode("utf-8")) > 32_768:
        raise ValidationError("stickers payload is too large")
    return encoded


def _json_loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _ttl_seconds(value: timedelta | float, *, field: str, maximum_days: int) -> int:
    seconds = value.total_seconds() if isinstance(value, timedelta) else value
    try:
        result = int(seconds)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a duration in seconds") from exc
    if result < 60 or result > maximum_days * 86_400:
        raise ValidationError(
            f"{field} must be between 60 seconds and {maximum_days} days"
        )
    return result


def _user_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "stale_after_days": int(row["stale_after_days"]),
        "default_fee_percent": float(row["default_fee_percent"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
        "access_state": row["access_state"],
        "access_changed_at": row["access_changed_at"],
        "revision": int(row["revision"]),
        "disabled": row["disabled_at"] is not None,
    }


def _inventory_dict(row: sqlite3.Row) -> dict[str, Any]:
    columns = set(row.keys())
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "skin_name": row["skin_name"],
        "quantity": int(row["quantity"]),
        "buy_price_cents": int(row["buy_price_cents"]),
        "cost_basis_cents": (
            int(row["cost_basis_cents"])
            if "cost_basis_cents" in columns
            and row["cost_basis_cents"] is not None
            else None
        ),
        "bought_at": row["bought_at"],
        "float_value": row["float_value"],
        "stickers": _json_loads(row["stickers_json"], []),
        "source_url": row["source_url"],
        "strategy": row["strategy"],
        "status": row["status"],
        "listed_price_cents": row["listed_price_cents"],
        "listed_at": row["listed_at"],
        "sold_price_cents": row["sold_price_cents"],
        "sold_at": row["sold_at"],
        "market_price_cents": row["market_price_cents"],
        "market_price_updated_at": row["market_price_updated_at"],
        "fee_percent": float(row["fee_percent"]),
        "notes": row["notes"],
        "revision": int(row["revision"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _inventory_cost_basis(item: dict[str, Any]) -> int:
    value = item.get("cost_basis_cents")
    if value is None:
        return int(item["buy_price_cents"]) * int(item.get("quantity") or 1)
    return int(value)


def _trade_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "traded_at": row["traded_at"],
        "notes": row["notes"],
        "status": row["status"],
        "revision": int(row["revision"]),
        "current_version": int(row["current_version"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "voided_at": row["voided_at"],
    }


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply additive account migrations without committing the connection."""

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_schema_versions (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        )
        """
    )
    row = conn.execute(
        "SELECT version FROM app_schema_versions WHERE component = ?",
        (SCHEMA_COMPONENT,),
    ).fetchone()
    current = int(row[0]) if row else 0
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"account database schema {current} is newer than supported {SCHEMA_VERSION}"
        )
    if current < 1:
        _migrate_v1(conn)
    if current < 2:
        _migrate_v2(conn)
    if current < 3:
        _migrate_v3(conn)
    conn.execute(
        """
        INSERT INTO app_schema_versions(component, version, migrated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(component) DO UPDATE SET
            version = excluded.version,
            migrated_at = excluded.migrated_at
        WHERE app_schema_versions.version < excluded.version
        """,
        (SCHEMA_COMPONENT, SCHEMA_VERSION, _iso()),
    )


def _migrate_v1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT,
            role TEXT NOT NULL DEFAULT 'member'
                CHECK(role IN ('admin', 'member')),
            password_hash TEXT NOT NULL,
            stale_after_days INTEGER NOT NULL DEFAULT 14
                CHECK(stale_after_days BETWEEN 1 AND 365),
            default_fee_percent REAL NOT NULL DEFAULT 4.0
                CHECK(default_fee_percent >= 0 AND default_fee_percent <= 50),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT,
            disabled_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES account_users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            csrf_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            revoked_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_sessions_user_active "
        "ON account_sessions(user_id, revoked_at, expires_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES account_users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            requested_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            delivered_at TEXT,
            used_at TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending'
                CHECK(delivery_state IN ('pending', 'sent', 'failed'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_resets_user_time "
        "ON account_password_resets(user_id, requested_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_inventory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES account_users(id) ON DELETE CASCADE,
            skin_name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity BETWEEN 1 AND 10000),
            buy_price_cents INTEGER NOT NULL CHECK(buy_price_cents > 0),
            bought_at TEXT NOT NULL,
            float_value REAL CHECK(float_value IS NULL OR (float_value >= 0 AND float_value <= 1)),
            stickers_json TEXT NOT NULL DEFAULT '[]',
            source_url TEXT,
            strategy TEXT NOT NULL DEFAULT 'manual'
                CHECK(strategy IN (
                    'investment', 'float', 'stickers', 'balanced', 'manual',
                    'flip', 'collection'
                )),
            status TEXT NOT NULL DEFAULT 'owned'
                CHECK(status IN ('owned', 'listed', 'sold', 'archived')),
            listed_price_cents INTEGER CHECK(listed_price_cents IS NULL OR listed_price_cents > 0),
            listed_at TEXT,
            sold_price_cents INTEGER CHECK(sold_price_cents IS NULL OR sold_price_cents > 0),
            sold_at TEXT,
            market_price_cents INTEGER CHECK(market_price_cents IS NULL OR market_price_cents > 0),
            market_price_updated_at TEXT,
            fee_percent REAL NOT NULL DEFAULT 4.0 CHECK(fee_percent >= 0 AND fee_percent <= 50),
            notes TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_inventory_user_status "
        "ON account_inventory_items(user_id, status, updated_at DESC, id DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES account_users(id) ON DELETE CASCADE,
            inventory_item_id INTEGER REFERENCES account_inventory_items(id),
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_activity_user_time "
        "ON account_activity(user_id, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_activity_item_time "
        "ON account_activity(inventory_item_id, id DESC)"
    )
    # History is an append-only audit trail.  Application bugs cannot silently
    # rewrite it; a future explicit migration can drop/recreate the triggers.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_account_activity_no_update
        BEFORE UPDATE ON account_activity
        BEGIN
            SELECT RAISE(ABORT, 'account activity is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_account_activity_no_delete
        BEFORE DELETE ON account_activity
        BEGIN
            SELECT RAISE(ABORT, 'account activity is immutable');
        END
        """
    )


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Add explicit approval and privacy-minimal session presence metadata.

    Every legacy account is intentionally demoted to ``member`` and moved to
    ``pending`` so an old public sign-up that happened to receive an admin role
    through the former dashboard-cookie flow cannot retain either privilege.
    The local master-token owner can review and explicitly promote/approve the
    real accounts.  Legacy disabled rows stay blocked, and all pre-migration
    sessions are revoked.
    """

    user_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(account_users)").fetchall()
    }
    additions = {
        "access_state": (
            "TEXT NOT NULL DEFAULT 'pending' "
            "CHECK(access_state IN ('pending', 'approved', 'blocked', 'deleted'))"
        ),
        "access_changed_at": "TEXT",
        "access_changed_by": "INTEGER",
        "access_reason": "TEXT",
        "deleted_at": "TEXT",
        "revision": "INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)",
        "registration_ip_address": "TEXT",
        "registration_country_code": "TEXT",
        "registration_user_agent": "TEXT",
    }
    for column, declaration in additions.items():
        if column not in user_columns:
            conn.execute(
                f"ALTER TABLE account_users ADD COLUMN {column} {declaration}"
            )

    migrated_at = _iso()
    conn.execute(
        """
        UPDATE account_users
        SET role = 'member',
            access_state = CASE
                WHEN disabled_at IS NOT NULL THEN 'blocked'
                ELSE 'pending'
            END,
            access_changed_at = COALESCE(access_changed_at, ?),
            revision = CASE WHEN revision < 1 THEN 1 ELSE revision END
        """,
        (migrated_at,),
    )
    conn.execute(
        """
        UPDATE account_sessions
        SET revoked_at = COALESCE(revoked_at, ?)
        """,
        (migrated_at,),
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_users_access "
        "ON account_users(access_state, role, created_at DESC, id DESC)"
    )

    session_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(account_sessions)").fetchall()
    }
    session_additions = {
        "ip_address": "TEXT",
        "country_code": "TEXT",
        "user_agent": "TEXT",
    }
    for column, declaration in session_additions.items():
        if column not in session_columns:
            conn.execute(
                f"ALTER TABLE account_sessions ADD COLUMN {column} {declaration}"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_sessions_presence "
        "ON account_sessions(last_seen_at DESC, revoked_at, expires_at)"
    )

    reset_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(account_password_resets)"
        ).fetchall()
    }
    if "issued_by_kind" not in reset_columns:
        conn.execute(
            "ALTER TABLE account_password_resets ADD COLUMN issued_by_kind TEXT"
        )
    if "issued_by_user_id" not in reset_columns:
        conn.execute(
            "ALTER TABLE account_password_resets ADD COLUMN issued_by_user_id INTEGER"
        )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Add a reversible, versioned inventory-exchange ledger.

    Inventory rows remain the source of truth for positions.  Trade legs point
    at immutable inventory artifacts and keep the before snapshots needed to
    reverse a trade without deleting its history.  ``cost_basis_cents`` is a
    total-position amount; unlike the legacy per-unit buy price it can preserve
    every cent when one position is distributed across several received legs.
    """

    inventory_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(account_inventory_items)"
        ).fetchall()
    }
    if "cost_basis_cents" not in inventory_columns:
        conn.execute(
            "ALTER TABLE account_inventory_items "
            "ADD COLUMN cost_basis_cents INTEGER "
            "CHECK(cost_basis_cents IS NULL OR cost_basis_cents >= 0)"
        )
    conn.execute(
        """
        UPDATE account_inventory_items
        SET cost_basis_cents = buy_price_cents * quantity
        WHERE cost_basis_cents IS NULL
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_inventory_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL
                REFERENCES account_users(id) ON DELETE CASCADE,
            traded_at TEXT NOT NULL,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'voided')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
            current_version INTEGER NOT NULL DEFAULT 1
                CHECK(current_version >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            voided_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_inventory_trades_user_time "
        "ON account_inventory_trades(user_id, status, traded_at DESC, id DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_inventory_trade_legs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL
                REFERENCES account_inventory_trades(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL
                REFERENCES account_users(id) ON DELETE CASCADE,
            trade_version INTEGER NOT NULL CHECK(trade_version >= 1),
            direction TEXT NOT NULL
                CHECK(direction IN ('outgoing', 'incoming')),
            inventory_item_id INTEGER NOT NULL
                REFERENCES account_inventory_items(id),
            source_inventory_item_id INTEGER
                REFERENCES account_inventory_items(id),
            quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 10000),
            unit_value_cents INTEGER NOT NULL CHECK(unit_value_cents > 0),
            cost_basis_cents INTEGER NOT NULL CHECK(cost_basis_cents >= 0),
            item_snapshot_json TEXT NOT NULL,
            source_snapshot_json TEXT,
            inventory_revision INTEGER NOT NULL CHECK(inventory_revision >= 1),
            source_revision INTEGER CHECK(
                source_revision IS NULL OR source_revision >= 1
            ),
            created_at TEXT NOT NULL,
            UNIQUE(trade_id, trade_version, direction, inventory_item_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_trade_legs_trade_version "
        "ON account_inventory_trade_legs(trade_id, trade_version, direction, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_trade_legs_inventory "
        "ON account_inventory_trade_legs(inventory_item_id, trade_id, trade_version)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_trade_legs_source "
        "ON account_inventory_trade_legs(source_inventory_item_id, trade_id, trade_version)"
    )
    # Historical legs are never rewritten.  A correction creates a new trade
    # version, and voiding only changes the parent trade state.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_account_trade_legs_no_update
        BEFORE UPDATE ON account_inventory_trade_legs
        BEGIN
            SELECT RAISE(ABORT, 'inventory trade legs are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_account_trade_legs_no_delete
        BEFORE DELETE ON account_inventory_trade_legs
        BEGIN
            SELECT RAISE(ABORT, 'inventory trade legs are immutable');
        END
        """
    )


@dataclass(frozen=True)
class PasswordResetEmail:
    recipient: str
    reset_url: str
    expires_at: str


class PasswordResetSender(Protocol):
    """Callback contract used by ``request_password_reset``."""

    def send_password_reset(self, message: PasswordResetEmail) -> None:
        """Deliver one reset message or raise an exception."""


class DisabledPasswordResetSender:
    def send_password_reset(self, message: PasswordResetEmail) -> None:
        del message
        raise EmailDeliveryError("password-reset email delivery is not configured")


class InMemoryPasswordResetSender:
    """Explicit test/dev sender; it never prints a sensitive URL."""

    def __init__(self) -> None:
        self.messages: list[PasswordResetEmail] = []

    def send_password_reset(self, message: PasswordResetEmail) -> None:
        self.messages.append(message)


class DevelopmentOutboxPasswordResetSender:
    """Opt-in local outbox stored with mode 0600, never in ordinary logs."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send_password_reset(self, message: PasswordResetEmail) -> None:
        payload = (
            json.dumps(
                {
                    "recipient": message.recipient,
                    "reset_url": message.reset_url,
                    "expires_at": message.expires_at,
                    "created_at": _iso(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        try:
            os.write(descriptor, payload.encode("utf-8"))
        finally:
            os.close(descriptor)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


class SMTPPasswordResetSender:
    """Small stdlib SMTP sender with TLS enabled by default."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        from_address: str,
        username: str = "",
        password: str = "",
        use_ssl: bool = False,
        starttls: bool = True,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.host = str(host).strip()
        self.port = int(port)
        self.from_address = _normalise_email(from_address)
        self.username = str(username)
        self.password = str(password)
        self.use_ssl = bool(use_ssl)
        self.starttls = bool(starttls)
        self.timeout_seconds = float(timeout_seconds)
        if not self.host or not 1 <= self.port <= 65_535:
            raise ValidationError("SMTP host/port is invalid")
        if self.use_ssl and self.starttls:
            raise ValidationError("choose SMTP SSL or STARTTLS, not both")
        if not self.use_ssl and not self.starttls:
            raise ValidationError("unencrypted SMTP is not allowed")
        if bool(self.username) != bool(self.password):
            raise ValidationError(
                "SMTP username and password must be configured together"
            )

    def send_password_reset(self, message: PasswordResetEmail) -> None:
        email = EmailMessage()
        email["Subject"] = "Сброс пароля CS2 Analytics"
        email["From"] = self.from_address
        email["To"] = message.recipient
        email.set_content(
            "Для смены пароля откройте ссылку:\n\n"
            f"{message.reset_url}\n\n"
            f"Ссылка действует до {message.expires_at}. "
            "Если это были не вы, ничего не делайте."
        )
        context = ssl.create_default_context()
        smtp_type = smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP
        with (
            smtp_type(
                self.host,
                self.port,
                timeout=self.timeout_seconds,
                context=context,
            )
            if self.use_ssl
            else smtp_type(
                self.host,
                self.port,
                timeout=self.timeout_seconds,
            ) as client
        ):
            if self.starttls:
                client.starttls(context=context)
            if self.username:
                client.login(self.username, self.password)
            client.send_message(email)


def password_reset_sender_from_env(
    *, env: dict[str, str] | None = None
) -> PasswordResetSender:
    """Build SMTP, an explicitly enabled dev outbox, or a disabled sender.

    Supported keys use the ``CS2_ACCOUNT_`` prefix: ``SMTP_HOST``,
    ``SMTP_PORT``, ``SMTP_FROM``, ``SMTP_USERNAME``, ``SMTP_PASSWORD``,
    ``SMTP_SSL``, ``SMTP_STARTTLS`` and ``DEV_EMAIL_OUTBOX``.  The development
    outbox is never selected implicitly in production.
    """

    values = os.environ if env is None else env
    host = values.get("CS2_ACCOUNT_SMTP_HOST", "").strip()
    if host:
        raw_ssl = values.get("CS2_ACCOUNT_SMTP_SSL", "0").strip().lower()
        use_ssl = raw_ssl in {"1", "true", "yes", "on"}
        raw_tls = (
            values.get("CS2_ACCOUNT_SMTP_STARTTLS", "0" if use_ssl else "1")
            .strip()
            .lower()
        )
        starttls = raw_tls in {"1", "true", "yes", "on"}
        return SMTPPasswordResetSender(
            host=host,
            port=int(values.get("CS2_ACCOUNT_SMTP_PORT", "465" if use_ssl else "587")),
            from_address=values.get("CS2_ACCOUNT_SMTP_FROM", ""),
            username=values.get("CS2_ACCOUNT_SMTP_USERNAME", ""),
            password=values.get("CS2_ACCOUNT_SMTP_PASSWORD", ""),
            use_ssl=use_ssl,
            starttls=starttls,
        )
    outbox = values.get("CS2_ACCOUNT_DEV_EMAIL_OUTBOX", "").strip()
    if outbox:
        return DevelopmentOutboxPasswordResetSender(outbox)
    return DisabledPasswordResetSender()


def build_password_reset_url(base_url: str, token: str) -> str:
    parsed = urlsplit(str(base_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError("reset_base_url must be an absolute http(s) URL")
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValidationError("reset_base_url must use HTTPS outside localhost")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["token"] = token
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


SenderCallback = Callable[[PasswordResetEmail], None]


def _send_password_reset(
    sender: PasswordResetSender | SenderCallback,
    message: PasswordResetEmail,
) -> None:
    callback = getattr(sender, "send_password_reset", None)
    if callable(callback):
        callback(message)
        return
    if callable(sender):
        sender(message)
        return
    raise EmailDeliveryError("invalid password-reset sender")


def _record_activity(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
    inventory_item_id: int | None = None,
    occurred_at: datetime | None = None,
) -> int:
    safe_event = _normalise_text(
        event_type,
        field="event_type",
        maximum=80,
        required=True,
    )
    encoded = json.dumps(
        payload or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    if len(encoded.encode("utf-8")) > 131_072:
        raise ValidationError("activity payload is too large")
    cursor = conn.execute(
        """
        INSERT INTO account_activity(
            user_id, inventory_item_id, event_type, occurred_at, payload_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (int(user_id), inventory_item_id, safe_event, _iso(occurred_at), encoded),
    )
    return int(cursor.lastrowid)


def _inventory_owned_row(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    item_id: int,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM account_inventory_items WHERE id = ? AND user_id = ?",
        (int(item_id), int(user_id)),
    ).fetchone()
    if row is None:
        # Deliberately indistinguishable from another user's row.
        raise NotFoundError("inventory item not found")
    return row


def _trade_owned_row(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    trade_id: int,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM account_inventory_trades WHERE id = ? AND user_id = ?",
        (int(trade_id), int(user_id)),
    ).fetchone()
    if row is None:
        # Deliberately indistinguishable from another user's trade.
        raise NotFoundError("inventory trade not found")
    return row


def _ensure_inventory_user_mutable(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    item_id: int,
) -> None:
    """Reject direct mutations of rows whose lifecycle belongs to a trade.

    Partial trades create an archived clone for the quantity that left the
    inventory.  That clone is an implementation detail in every historical
    version.  A full outgoing position is likewise managed by the active
    current trade until that trade is edited or voided.  Letting the regular
    inventory CRUD restore either row would duplicate ownership and make the
    atomic trade rollback impossible.
    """

    managed = conn.execute(
        """
        SELECT 1
        FROM account_inventory_trade_legs AS legs
        JOIN account_inventory_trades AS trades
          ON trades.id = legs.trade_id
         AND trades.user_id = legs.user_id
        WHERE legs.user_id = ?
          AND legs.inventory_item_id = ?
          AND legs.direction = 'outgoing'
          AND (
              legs.inventory_item_id <> legs.source_inventory_item_id
              OR (
                  trades.status = 'active'
                  AND legs.trade_version = trades.current_version
              )
          )
        LIMIT 1
        """,
        (int(user_id), int(item_id)),
    ).fetchone()
    if managed is not None:
        raise ConflictError(
            "inventory item is managed by an active trade",
            code="trade_managed_item",
        )


def analyze_inventory_item(
    item: dict[str, Any],
    *,
    now: datetime | None = None,
    stale_after_days: int = DEFAULT_STUCK_AFTER_DAYS,
    target_roi_percent: float = DEFAULT_TARGET_ROI_PERCENT,
) -> dict[str, Any]:
    """Calculate exact-cent P/L and a transparent listing-price suggestion.

    The suggestion uses a target ROI, an optional observed market price and a
    small markdown for a stale listing.  It is analytical guidance only: the
    result explicitly says when a competitive price falls below break-even.
    """

    stale_days = _bounded_int(
        stale_after_days,
        field="stale_after_days",
        minimum=1,
        maximum=365,
    )
    try:
        target_roi = float(target_roi_percent)
    except (TypeError, ValueError) as exc:
        raise ValidationError("target_roi_percent must be a number") from exc
    if not math.isfinite(target_roi) or not -99 <= target_roi <= 1_000:
        raise ValidationError("target_roi_percent must be between -99 and 1000")

    quantity = _bounded_int(
        item.get("quantity", 1), field="quantity", minimum=1, maximum=10_000
    )
    buy_price = _bounded_int(
        item.get("buy_price_cents"),
        field="buy_price_cents",
        minimum=1,
        maximum=100_000_000_00,
    )
    fee_percent = _normalise_fee(item.get("fee_percent", 4.0))
    fee_basis_points = round(fee_percent * 100)
    fee_multiplier = 10_000 - fee_basis_points
    if fee_multiplier <= 0:  # guarded by the 50% validator, kept explicit
        raise ValidationError("fee_percent leaves no sale proceeds")

    def net_total(unit_price: int) -> int:
        # Inventory quantity represents separate CS2 items/listings.  Apply
        # marketplace fee rounding per item, then sum, so realized and
        # unrealized P&L use the same cent semantics.
        return ((unit_price * fee_multiplier) // 10_000) * quantity

    raw_cost_basis = item.get("cost_basis_cents")
    if raw_cost_basis is None:
        total_cost = buy_price * quantity
    else:
        total_cost = _bounded_int(
            raw_cost_basis,
            field="cost_basis_cents",
            minimum=0,
            maximum=100_000_000_00 * 10_000,
        )
    break_even = math.ceil(total_cost * 10_000 / (fee_multiplier * quantity))
    target_price = math.ceil(
        total_cost * (100 + target_roi) * 100 / (fee_multiplier * quantity)
    )
    target_price = max(1, target_price)
    current_time = _utc_now(now)
    bought_at = _parse_datetime(item.get("bought_at"), field="bought_at")
    days_held = max(0.0, (current_time - bought_at).total_seconds() / 86_400)
    listed_at = (
        _parse_datetime(item.get("listed_at"), field="listed_at")
        if item.get("listed_at")
        else None
    )
    days_listed = (
        max(0.0, (current_time - listed_at).total_seconds() / 86_400)
        if listed_at
        else None
    )
    status = str(item.get("status") or "owned")
    is_stale = (
        status == "listed" and days_listed is not None and days_listed >= stale_days
    )

    listed_price = _optional_cents(
        item.get("listed_price_cents"), field="listed_price_cents"
    )
    sold_price = _optional_cents(item.get("sold_price_cents"), field="sold_price_cents")
    market_price = _optional_cents(
        item.get("market_price_cents"), field="market_price_cents"
    )

    suggestion: int | None = None
    suggestion_reason = "already_sold" if status == "sold" else "target_roi"
    if status != "sold":
        suggestion = target_price
        if market_price:
            suggestion = min(suggestion, max(1, market_price - 1))
            suggestion_reason = "competitive_market_price"
        if is_stale and listed_price:
            markdown = max(1, math.ceil(listed_price * 0.02))
            suggestion = min(suggestion, max(1, listed_price - markdown))
            suggestion_reason = "stale_listing_markdown"

    reference_price = sold_price if status == "sold" else market_price or listed_price
    net_value = net_total(reference_price) if reference_price else None
    gross_value = reference_price * quantity if reference_price else None
    profit = net_value - total_cost if net_value is not None else None
    roi = (profit / total_cost * 100) if profit is not None and total_cost else None
    suggested_net = net_total(suggestion) if suggestion else None
    suggested_profit = suggested_net - total_cost if suggested_net is not None else None

    return {
        "quantity": quantity,
        "total_cost_cents": total_cost,
        "reference_price_cents": reference_price,
        "gross_value_cents": gross_value,
        "net_value_cents": net_value,
        "profit_cents": profit,
        "roi_percent": round(roi, 4) if roi is not None else None,
        "profit_kind": "realized" if status == "sold" else "unrealized",
        "fee_percent": fee_percent,
        "break_even_price_cents": break_even,
        "target_price_cents": target_price,
        "suggested_price_cents": suggestion,
        "suggested_net_cents": suggested_net,
        "suggested_profit_cents": suggested_profit,
        "suggestion_reason": suggestion_reason,
        "suggestion_below_break_even": bool(suggestion and suggestion < break_even),
        "days_held": round(days_held, 2),
        "days_listed": round(days_listed, 2) if days_listed is not None else None,
        "stale_after_days": stale_days,
        "is_stale": is_stale,
    }


class AccountService:
    """Transactional account service backed by one SQLite database file.

    A new connection is opened per operation, so the object is safe to share
    between ``ThreadingHTTPServer`` request threads.  ``:memory:`` is supported
    for tests through a private shared-cache URI retained by an anchor
    connection.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        password_reset_sender: PasswordResetSender | SenderCallback | None = None,
        reset_base_url: str = "",
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.db_path = str(db_path)
        self.password_reset_sender = (
            password_reset_sender or DisabledPasswordResetSender()
        )
        self.reset_base_url = str(reset_base_url or "").strip()
        self.busy_timeout_ms = _bounded_int(
            busy_timeout_ms,
            field="busy_timeout_ms",
            minimum=100,
            maximum=60_000,
        )
        self._uri = False
        self._anchor: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self._database = f"file:cs2_account_{id(self)}?mode=memory&cache=shared"
            self._uri = True
            self._anchor = self._open_connection()
        else:
            path = Path(self.db_path).expanduser()
            parent_existed = path.parent.exists()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not parent_existed:
                try:
                    os.chmod(path.parent, 0o700)
                except OSError:
                    pass
            self._database = str(path)

        conn = self._anchor or self._open_connection()
        try:
            if not self._uri:
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            init_schema(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if conn is not self._anchor:
                conn.close()
        self._harden_database_files()

    def close(self) -> None:
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._database,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
            uri=self._uri,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        self._harden_database_files()
        return conn

    def _harden_database_files(self) -> None:
        if self._uri or not hasattr(self, "_database"):
            return
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(self._database + suffix, 0o600)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self._open_connection()
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()
            self._harden_database_files()

    @staticmethod
    def _normalise_user_id(user_id: Any) -> int:
        return _bounded_int(user_id, field="user_id", minimum=1, maximum=2**63 - 1)

    @staticmethod
    def _normalise_role(role: Any) -> str:
        result = str(role or "").strip().lower()
        if result not in ROLES:
            raise ValidationError("role must be admin or member")
        return result

    @staticmethod
    def _normalise_access_state(access_state: Any) -> str:
        result = str(access_state or "").strip().lower()
        if result not in ACCESS_STATES:
            raise ValidationError(
                "access_state must be pending, approved, blocked or deleted"
            )
        return result

    @staticmethod
    def _effective_access_state(row: sqlite3.Row) -> str:
        state = str(row["access_state"] or "pending")
        if row["deleted_at"] is not None or state == "deleted":
            return "deleted"
        if row["disabled_at"] is not None or state == "blocked":
            return "blocked"
        return state if state in ACCESS_STATES else "pending"

    @classmethod
    def _require_approved(cls, row: sqlite3.Row) -> None:
        state = cls._effective_access_state(row)
        if state != "approved":
            raise AccountAccessError(state)

    def _require_admin(
        self,
        conn: sqlite3.Connection,
        *,
        actor_user_id: int | None,
        trusted_local_admin: bool = False,
    ) -> dict[str, Any]:
        """Resolve an approved admin or an explicitly trusted local owner.

        ``trusted_local_admin`` is deliberately opt-in and accepts only the
        literal boolean ``True``.  HTTP code must establish both localhost and
        master-token trust before setting it.
        """

        if trusted_local_admin is True:
            return {"actor_kind": "local_dashboard", "actor_user_id": None}
        if actor_user_id is None:
            raise AuthorizationError(
                "approved admin role is required", code="admin_required"
            )
        actor_id = self._normalise_user_id(actor_user_id)
        actor = conn.execute(
            "SELECT * FROM account_users WHERE id = ?", (actor_id,)
        ).fetchone()
        if actor is None or actor["role"] != "admin":
            raise AuthorizationError(
                "approved admin role is required", code="admin_required"
            )
        try:
            self._require_approved(actor)
        except AccountAccessError as exc:
            raise AuthorizationError(
                "approved admin role is required", code="admin_required"
            ) from exc
        return {"actor_kind": "account_admin", "actor_user_id": actor_id}

    def _approved_user_row(
        self, conn: sqlite3.Connection, user_id: int
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM account_users WHERE id = ?", (int(user_id),)
        ).fetchone()
        if row is None:
            raise NotFoundError("user not found")
        self._require_approved(row)
        return row

    @staticmethod
    def _check_revision(row: sqlite3.Row, expected_revision: int | None) -> None:
        if expected_revision is None:
            return
        revision = _bounded_int(
            expected_revision,
            field="expected_revision",
            minimum=1,
            maximum=2**63 - 1,
        )
        if int(row["revision"]) != revision:
            raise ConflictError(
                "user was changed by another administrator",
                code="revision_conflict",
            )

    def _admin_user_payload(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: datetime,
        include_sessions: bool = False,
        session_limit: int = 50,
    ) -> dict[str, Any]:
        """Build a sanitised admin view without credential material."""

        user_id = int(row["id"])
        sessions = conn.execute(
            """
            SELECT id, created_at, expires_at, last_seen_at, revoked_at,
                   ip_address, country_code, user_agent,
                   COUNT(*) OVER () AS total_session_count
            FROM account_sessions
            WHERE user_id = ?
            ORDER BY last_seen_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, session_limit if include_sessions else 1),
        ).fetchall()
        session_payloads: list[dict[str, Any]] = []
        for session in sessions:
            expires_at = _parse_datetime(session["expires_at"], field="expires_at")
            active = session["revoked_at"] is None and expires_at > now
            session_payloads.append(
                {
                    "id": int(session["id"]),
                    "created_at": session["created_at"],
                    "expires_at": session["expires_at"],
                    "last_seen_at": session["last_seen_at"],
                    "revoked_at": session["revoked_at"],
                    "active": active,
                    "is_active": active,
                    "ip_address": session["ip_address"],
                    "country_code": session["country_code"],
                    "user_agent": session["user_agent"],
                }
            )
        active_row = conn.execute(
            """
            SELECT id, created_at, expires_at, last_seen_at, revoked_at,
                   ip_address, country_code, user_agent,
                   COUNT(*) OVER () AS active_count
            FROM account_sessions
            WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
            ORDER BY last_seen_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, _iso(now)),
        ).fetchone()
        active_count = int(active_row["active_count"]) if active_row else 0
        latest = session_payloads[0] if session_payloads else None
        total_session_count = (
            int(sessions[0]["total_session_count"]) if sessions else 0
        )
        online = bool(
            active_row
            and now
            - _parse_datetime(active_row["last_seen_at"], field="last_seen_at")
            <= SESSION_ONLINE_WINDOW
            and self._effective_access_state(row) == "approved"
        )
        inventory = conn.execute(
            """
            SELECT COUNT(*) AS positions,
                   COALESCE(SUM(CASE WHEN status IN ('owned', 'listed')
                                    THEN quantity ELSE 0 END), 0) AS active_items
            FROM account_inventory_items WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        credential_events = conn.execute(
            """
            SELECT event_type, occurred_at, payload_json
            FROM account_activity
            WHERE user_id = ? AND event_type IN (
                'password_changed', 'password_reset_completed',
                'password_reset_requested', 'admin_password_reset_delivery'
            )
            ORDER BY id DESC
            LIMIT 100
            """,
            (user_id,),
        ).fetchall()
        password_changed_at = None
        password_reset_sent_at = None
        for event in credential_events:
            if password_changed_at is None and event["event_type"] in {
                "password_changed",
                "password_reset_completed",
            }:
                password_changed_at = event["occurred_at"]
            if password_reset_sent_at is None:
                if event["event_type"] == "password_reset_requested":
                    password_reset_sent_at = event["occurred_at"]
                elif event["event_type"] == "admin_password_reset_delivery":
                    delivery = _json_loads(event["payload_json"], {})
                    if delivery.get("delivery_state") == "sent":
                        password_reset_sent_at = event["occurred_at"]
        payload = {
            **_user_dict(row),
            "access_state": self._effective_access_state(row),
            "access_status": self._effective_access_state(row),
            "access_reason": row["access_reason"],
            "access_changed_by": row["access_changed_by"],
            "deleted_at": row["deleted_at"],
            "active_session_count": active_count,
            "active_sessions": active_count,
            "total_session_count": total_session_count,
            "total_sessions": total_session_count,
            "online": online,
            "is_online": online,
            "last_seen_at": latest["last_seen_at"] if latest else None,
            "last_ip_address": latest["ip_address"] if latest else None,
            "last_country_code": latest["country_code"] if latest else None,
            "last_user_agent": latest["user_agent"] if latest else None,
            "current_ip_address": active_row["ip_address"] if active_row else None,
            "current_country_code": active_row["country_code"] if active_row else None,
            "current_country": active_row["country_code"] if active_row else None,
            "current_user_agent": active_row["user_agent"] if active_row else None,
            "last_country": latest["country_code"] if latest else None,
            "approved_at": (
                row["access_changed_at"]
                if self._effective_access_state(row) == "approved"
                else None
            ),
            "blocked_at": (
                row["access_changed_at"]
                if self._effective_access_state(row) == "blocked"
                else None
            ),
            "registration_ip_address": row["registration_ip_address"],
            "registration_country_code": row["registration_country_code"],
            "registration_user_agent": row["registration_user_agent"],
            "inventory_positions": int(inventory["positions"]),
            "active_inventory_items": int(inventory["active_items"]),
            "password_changed_at": password_changed_at,
            "password_reset_sent_at": password_reset_sent_at,
        }
        if include_sessions:
            payload["sessions"] = session_payloads
            payload["recent_sessions"] = session_payloads
        return payload

    def create_user(
        self,
        email: str,
        password: str,
        *,
        role: str = "member",
        display_name: str = "",
        access_state: str | None = None,
        ip_address: Any = None,
        country_code: Any = None,
        user_agent: Any = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Create a user; ordinary members start pending administrator review.

        ``access_state`` exists for trusted imports/tests.  Public registration
        must not expose it.  Administrators default to approved so a locally
        provisioned owner is not locked out during migration or bootstrap.
        """

        normal_email = _normalise_email(email)
        password_hash = hash_password(password)
        normal_role = self._normalise_role(role)
        normal_access = (
            self._normalise_access_state(access_state)
            if access_state is not None
            else ("approved" if normal_role == "admin" else "pending")
        )
        if normal_access == "deleted":
            raise ValidationError("new accounts cannot start deleted")
        normal_ip, normal_country, normal_user_agent = _request_metadata(
            ip_address=ip_address,
            country_code=country_code,
            user_agent=user_agent,
        )
        name = _normalise_text(display_name, field="display_name", maximum=100)
        timestamp = _iso(now)
        with self._connection(write=True) as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO account_users(
                        email, display_name, role, password_hash, created_at, updated_at,
                        access_state, access_changed_at, disabled_at,
                        registration_ip_address, registration_country_code,
                        registration_user_agent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normal_email,
                        name,
                        normal_role,
                        password_hash,
                        timestamp,
                        timestamp,
                        normal_access,
                        timestamp,
                        timestamp if normal_access == "blocked" else None,
                        normal_ip,
                        normal_country,
                        normal_user_agent,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "an account with this email already exists"
                ) from exc
            user_id = int(cursor.lastrowid)
            _record_activity(
                conn,
                user_id=user_id,
                event_type="account_created",
                payload={"role": normal_role, "access_state": normal_access},
                occurred_at=now,
            )
            row = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (user_id,)
            ).fetchone()
        return _user_dict(row)

    def get_user(self, user_id: int) -> dict[str, Any]:
        user_id = self._normalise_user_id(user_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("user not found")
        return _user_dict(row)

    def admin_list_users(
        self,
        *,
        actor_user_id: int | None = None,
        trusted_local_admin: bool = False,
        query: str = "",
        access_state: str | None = None,
        role: str | None = None,
        online_only: bool = False,
        limit: int = 100,
        offset: int = 0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """List accounts and their current presence for the admin panel."""

        search = _normalise_text(query, field="query", maximum=200) or ""
        normal_state = (
            self._normalise_access_state(access_state)
            if access_state is not None
            else None
        )
        normal_role = self._normalise_role(role) if role is not None else None
        page_limit = _bounded_int(limit, field="limit", minimum=1, maximum=200)
        page_offset = _bounded_int(
            offset, field="offset", minimum=0, maximum=10_000_000
        )
        timestamp = _utc_now(now)
        clauses: list[str] = []
        params: list[Any] = []
        if search:
            clauses.append(
                "(email LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR COALESCE(display_name, '') LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace(
                "_", "\\_"
            )
            params.extend((f"%{escaped}%", f"%{escaped}%"))
        if normal_state is not None:
            clauses.append("access_state = ?")
            params.append(normal_state)
        if normal_role is not None:
            clauses.append("role = ?")
            params.append(normal_role)
        online_clause = (
            "access_state = 'approved' AND disabled_at IS NULL "
            "AND deleted_at IS NULL AND EXISTS ("
            "SELECT 1 FROM account_sessions AS online_session "
            "WHERE online_session.user_id = account_users.id "
            "AND online_session.revoked_at IS NULL "
            "AND online_session.expires_at > ? "
            "AND online_session.last_seen_at >= ?)"
        )
        online_params = [
            _iso(timestamp),
            _iso(timestamp - SESSION_ONLINE_WINDOW),
        ]
        if online_only:
            clauses.append(online_clause)
            params.extend(online_params)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as conn:
            self._require_admin(
                conn,
                actor_user_id=actor_user_id,
                trusted_local_admin=trusted_local_admin,
            )
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM account_users" + where, params
                ).fetchone()[0]
            )
            rows = conn.execute(
                """
                SELECT * FROM account_users
                """
                + where
                + " ORDER BY CASE access_state "
                "WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 "
                "WHEN 'blocked' THEN 2 ELSE 3 END, created_at DESC, id DESC "
                "LIMIT ? OFFSET ?",
                [*params, page_limit, page_offset],
            ).fetchall()
            users = [
                self._admin_user_payload(
                    conn,
                    row,
                    now=timestamp,
                    include_sessions=True,
                    session_limit=8,
                )
                for row in rows
            ]
            online_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM account_users WHERE " + online_clause,
                    online_params,
                ).fetchone()[0]
            )
            state_counts = {
                state: 0 for state in ("pending", "approved", "blocked", "deleted")
            }
            for count_row in conn.execute(
                "SELECT access_state, COUNT(*) AS amount FROM account_users "
                "GROUP BY access_state"
            ).fetchall():
                state = str(count_row["access_state"])
                if state in state_counts:
                    state_counts[state] = int(count_row["amount"])
            global_total = sum(state_counts.values())
        return {
            "users": users,
            "total": total,
            "total_count": total,
            "limit": page_limit,
            "offset": page_offset,
            "online_count": online_count,
            "summary": {
                "total": global_total,
                **state_counts,
                "online": online_count,
            },
        }

    def admin_get_user(
        self,
        target_user_id: int,
        *,
        actor_user_id: int | None = None,
        trusted_local_admin: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return one sanitised account, inventory counts and recent sessions."""

        target_id = self._normalise_user_id(target_user_id)
        timestamp = _utc_now(now)
        with self._connection() as conn:
            self._require_admin(
                conn,
                actor_user_id=actor_user_id,
                trusted_local_admin=trusted_local_admin,
            )
            row = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (target_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("user not found")
            return self._admin_user_payload(
                conn, row, now=timestamp, include_sessions=True
            )

    def admin_set_access(
        self,
        target_user_id: int,
        access_state: str,
        *,
        actor_user_id: int | None = None,
        trusted_local_admin: bool = False,
        reason: str = "",
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Approve, return to pending, or block an account atomically."""

        target_id = self._normalise_user_id(target_user_id)
        normal_state = self._normalise_access_state(access_state)
        if normal_state == "deleted":
            raise ValidationError("use admin_soft_delete_user for deleted accounts")
        normal_reason = _normalise_text(reason, field="reason", maximum=500)
        timestamp = _iso(now)
        with self._connection(write=True) as conn:
            actor = self._require_admin(
                conn,
                actor_user_id=actor_user_id,
                trusted_local_admin=trusted_local_admin,
            )
            target = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (target_id,)
            ).fetchone()
            if target is None:
                raise NotFoundError("user not found")
            previous = self._effective_access_state(target)
            if previous == "deleted":
                raise ConflictError(
                    "deleted accounts cannot be restored", code="account_deleted"
                )
            self._check_revision(target, expected_revision)
            if actor["actor_user_id"] == target_id and normal_state != "approved":
                raise ConflictError(
                    "administrators cannot remove their own access",
                    code="cannot_modify_self",
                )
            conn.execute(
                """
                UPDATE account_users
                SET access_state = ?, access_changed_at = ?,
                    access_changed_by = ?, access_reason = ?,
                    disabled_at = CASE WHEN ? = 'blocked' THEN ? ELSE NULL END,
                    revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    normal_state,
                    timestamp,
                    actor["actor_user_id"],
                    normal_reason,
                    normal_state,
                    timestamp,
                    timestamp,
                    target_id,
                ),
            )
            revoked_sessions = 0
            invalidated_resets = 0
            if normal_state != "approved":
                revoked_sessions = conn.execute(
                    """
                    UPDATE account_sessions SET revoked_at = ?
                    WHERE user_id = ? AND revoked_at IS NULL
                    """,
                    (timestamp, target_id),
                ).rowcount
                invalidated_resets = conn.execute(
                    """
                    UPDATE account_password_resets SET used_at = COALESCE(used_at, ?)
                    WHERE user_id = ? AND used_at IS NULL
                    """,
                    (timestamp, target_id),
                ).rowcount
            _record_activity(
                conn,
                user_id=target_id,
                event_type="access_changed",
                payload={
                    **actor,
                    "from": previous,
                    "to": normal_state,
                    "reason": normal_reason,
                    "revoked_sessions": int(revoked_sessions),
                    "invalidated_password_resets": int(invalidated_resets),
                },
                occurred_at=now,
            )
            updated = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (target_id,)
            ).fetchone()
        return _user_dict(updated)

    def admin_approve_user(self, target_user_id: int, **kwargs: Any) -> dict[str, Any]:
        return self.admin_set_access(target_user_id, "approved", **kwargs)

    def admin_block_user(self, target_user_id: int, **kwargs: Any) -> dict[str, Any]:
        return self.admin_set_access(target_user_id, "blocked", **kwargs)

    def admin_revoke_sessions(
        self,
        target_user_id: int,
        *,
        actor_user_id: int | None = None,
        trusted_local_admin: bool = False,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Revoke every bearer session for one user and append an audit row."""

        target_id = self._normalise_user_id(target_user_id)
        timestamp = _iso(now)
        with self._connection(write=True) as conn:
            actor = self._require_admin(
                conn,
                actor_user_id=actor_user_id,
                trusted_local_admin=trusted_local_admin,
            )
            target = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (target_id,)
            ).fetchone()
            if target is None:
                raise NotFoundError("user not found")
            self._check_revision(target, expected_revision)
            count = int(
                conn.execute(
                    """
                    UPDATE account_sessions SET revoked_at = ?
                    WHERE user_id = ? AND revoked_at IS NULL
                    """,
                    (timestamp, target_id),
                ).rowcount
            )
            conn.execute(
                "UPDATE account_users SET revision = revision + 1, updated_at = ? "
                "WHERE id = ?",
                (timestamp, target_id),
            )
            _record_activity(
                conn,
                user_id=target_id,
                event_type="sessions_revoked_by_admin",
                payload={**actor, "revoked_sessions": count},
                occurred_at=now,
            )
        return {
            "revoked_sessions": count,
            "revision": int(target["revision"]) + 1,
        }

    def admin_soft_delete_user(
        self,
        target_user_id: int,
        *,
        actor_user_id: int | None = None,
        trusted_local_admin: bool = False,
        reason: str = "",
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Disable login permanently while retaining inventory and audit data."""

        target_id = self._normalise_user_id(target_user_id)
        normal_reason = _normalise_text(reason, field="reason", maximum=500)
        timestamp = _iso(now)
        with self._connection(write=True) as conn:
            actor = self._require_admin(
                conn,
                actor_user_id=actor_user_id,
                trusted_local_admin=trusted_local_admin,
            )
            target = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (target_id,)
            ).fetchone()
            if target is None:
                raise NotFoundError("user not found")
            self._check_revision(target, expected_revision)
            if actor["actor_user_id"] == target_id:
                raise ConflictError(
                    "administrators cannot delete themselves",
                    code="cannot_modify_self",
                )
            if self._effective_access_state(target) == "deleted":
                return _user_dict(target)
            tombstone_email = (
                f"deleted-{target_id}-{secrets.token_hex(8)}@invalid.invalid"
            )
            tombstone_password = hash_password(secrets.token_urlsafe(48))
            conn.execute(
                """
                UPDATE account_users
                SET email = ?, display_name = NULL, password_hash = ?,
                    registration_ip_address = NULL,
                    registration_country_code = NULL,
                    registration_user_agent = NULL,
                    access_state = 'deleted', access_changed_at = ?,
                    access_changed_by = ?, access_reason = ?, deleted_at = ?,
                    disabled_at = COALESCE(disabled_at, ?),
                    revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    tombstone_email,
                    tombstone_password,
                    timestamp,
                    actor["actor_user_id"],
                    normal_reason,
                    timestamp,
                    timestamp,
                    timestamp,
                    target_id,
                ),
            )
            revoked_sessions = int(
                conn.execute(
                    "SELECT COUNT(*) FROM account_sessions "
                    "WHERE user_id = ? AND revoked_at IS NULL",
                    (target_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                UPDATE account_sessions
                SET revoked_at = COALESCE(revoked_at, ?), ip_address = NULL,
                    country_code = NULL, user_agent = NULL
                WHERE user_id = ?
                """,
                (timestamp, target_id),
            )
            invalidated_resets = int(
                conn.execute(
                    """
                    UPDATE account_password_resets SET used_at = COALESCE(used_at, ?)
                    WHERE user_id = ? AND used_at IS NULL
                    """,
                    (timestamp, target_id),
                ).rowcount
            )
            _record_activity(
                conn,
                user_id=target_id,
                event_type="account_soft_deleted",
                payload={
                    **actor,
                    "reason": normal_reason,
                    "revoked_sessions": revoked_sessions,
                    "invalidated_password_resets": invalidated_resets,
                },
                occurred_at=now,
            )
            updated = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (target_id,)
            ).fetchone()
        return _user_dict(updated)

    def set_user_role(
        self,
        *,
        actor_user_id: int | None = None,
        target_user_id: int,
        role: str,
        trusted_local_admin: bool = False,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Local-owner-only role mutation with atomic session revocation."""

        if trusted_local_admin is not True:
            raise AuthorizationError(
                "local owner authorization is required",
                code="owner_authorization_required",
            )

        target_id = self._normalise_user_id(target_user_id)
        normal_role = self._normalise_role(role)
        revision = (
            _bounded_int(
                expected_revision,
                field="expected_revision",
                minimum=1,
                maximum=2**63 - 1,
            )
            if expected_revision is not None
            else None
        )
        timestamp = _iso(now)
        with self._connection(write=True) as conn:
            actor = self._require_admin(
                conn,
                actor_user_id=None,
                trusted_local_admin=True,
            )
            target = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (target_id,)
            ).fetchone()
            if target is None:
                raise NotFoundError("user not found")
            if self._effective_access_state(target) == "deleted":
                raise ConflictError(
                    "deleted accounts cannot change role", code="account_deleted"
                )
            if (
                normal_role == "admin"
                and self._effective_access_state(target) != "approved"
            ):
                raise ConflictError(
                    "approve the account before granting administrator access",
                    code="access_not_approved",
                )
            if revision is not None and int(target["revision"]) != revision:
                raise ConflictError(
                    "user was changed by another administrator",
                    code="revision_conflict",
                )
            revoked_sessions = int(
                conn.execute(
                    """
                    UPDATE account_sessions
                    SET revoked_at = ?
                    WHERE user_id = ? AND revoked_at IS NULL
                    """,
                    (timestamp, target_id),
                ).rowcount
            )
            conn.execute(
                """
                UPDATE account_users
                SET role = ?,
                    revision = revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    normal_role,
                    timestamp,
                    target_id,
                ),
            )
            _record_activity(
                conn,
                user_id=target_id,
                event_type="role_changed",
                payload={
                    **actor,
                    "from": target["role"],
                    "to": normal_role,
                    "revoked_sessions": revoked_sessions,
                },
                occurred_at=now,
            )
            row = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (target_id,)
            ).fetchone()
        result = _user_dict(row)
        result["revoked_sessions"] = revoked_sessions
        return result

    def update_profile(
        self,
        user_id: int,
        *,
        display_name: str | None = None,
        stale_after_days: int | None = None,
        default_fee_percent: float | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Update the small set of user-owned dashboard preferences."""

        user_id = self._normalise_user_id(user_id)
        changes: dict[str, Any] = {}
        if display_name is not None:
            changes["display_name"] = _normalise_text(
                display_name, field="display_name", maximum=100
            )
        if stale_after_days is not None:
            changes["stale_after_days"] = _bounded_int(
                stale_after_days,
                field="stale_after_days",
                minimum=1,
                maximum=365,
            )
        if default_fee_percent is not None:
            changes["default_fee_percent"] = _normalise_fee(default_fee_percent)
        if not changes:
            return self.get_user(user_id)
        timestamp = _iso(now)
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self._connection(write=True) as conn:
            self._approved_user_row(conn, user_id)
            conn.execute(
                f"UPDATE account_users SET {assignments}, revision = revision + 1, "
                "updated_at = ? WHERE id = ?",
                [*changes.values(), timestamp, user_id],
            )
            _record_activity(
                conn,
                user_id=user_id,
                event_type="profile_updated",
                payload={"changed_fields": sorted(changes)},
                occurred_at=now,
            )
            row = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (user_id,)
            ).fetchone()
        return _user_dict(row)

    def _insert_session(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        now: datetime,
        session_ttl: timedelta | float,
        ip_address: Any = None,
        country_code: Any = None,
        user_agent: Any = None,
    ) -> dict[str, Any]:
        ttl_seconds = _ttl_seconds(session_ttl, field="session_ttl", maximum_days=365)
        timestamp = _iso(now)
        expires_at = _iso(now + timedelta(seconds=ttl_seconds))
        conn.execute("DELETE FROM account_sessions WHERE expires_at <= ?", (timestamp,))
        active_rows = conn.execute(
            """
            SELECT id FROM account_sessions
            WHERE user_id = ? AND revoked_at IS NULL
            ORDER BY last_seen_at DESC, id DESC
            """,
            (user_id,),
        ).fetchall()
        for stale in active_rows[MAX_ACTIVE_SESSIONS - 1 :]:
            conn.execute(
                "UPDATE account_sessions SET revoked_at = ? WHERE id = ?",
                (timestamp, int(stale["id"])),
            )
        raw_token, token_hash = _new_token("cs2s_")
        raw_csrf, csrf_hash = _new_token("cs2c_")
        normal_ip, normal_country, normal_user_agent = _request_metadata(
            ip_address=ip_address,
            country_code=country_code,
            user_agent=user_agent,
        )
        cursor = conn.execute(
            """
            INSERT INTO account_sessions(
                user_id, token_hash, csrf_hash, created_at,
                expires_at, last_seen_at, revoked_at,
                ip_address, country_code, user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                user_id,
                token_hash,
                csrf_hash,
                timestamp,
                expires_at,
                timestamp,
                normal_ip,
                normal_country,
                normal_user_agent,
            ),
        )
        return {
            "id": int(cursor.lastrowid),
            "session_token": raw_token,
            "csrf_token": raw_csrf,
            "created_at": timestamp,
            "expires_at": expires_at,
        }

    def authenticate(
        self,
        email: str,
        password: str,
        *,
        session_ttl: timedelta | float = DEFAULT_SESSION_TTL,
        ip_address: Any = None,
        country_code: Any = None,
        user_agent: Any = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Verify credentials and return user + one-time session/CSRF secrets.

        Every failure has the same public ``invalid email or password`` error.
        """

        try:
            normal_email = _normalise_email(email)
        except ValidationError:
            normal_email = ""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM account_users WHERE email = ?", (normal_email,)
            ).fetchone()
        encoded = row["password_hash"] if row is not None else _dummy_password_hash()
        valid = verify_password(password, encoded)
        if row is None or not valid:
            raise AuthenticationError(
                "invalid email or password", code="invalid_credentials"
            )
        self._require_approved(row)

        timestamp = _utc_now(now)
        with self._connection(write=True) as conn:
            current = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (int(row["id"]),)
            ).fetchone()
            if current is None:
                raise AuthenticationError(
                    "invalid email or password", code="invalid_credentials"
                )
            self._require_approved(current)
            # Close the small race where the password was changed after the read.
            if current["password_hash"] != encoded and not verify_password(
                password, current["password_hash"]
            ):
                raise AuthenticationError("invalid email or password")
            session = self._insert_session(
                conn,
                user_id=int(current["id"]),
                now=timestamp,
                session_ttl=session_ttl,
                ip_address=ip_address,
                country_code=country_code,
                user_agent=user_agent,
            )
            conn.execute(
                "UPDATE account_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (_iso(timestamp), _iso(timestamp), int(current["id"])),
            )
            current = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (int(current["id"]),)
            ).fetchone()
        return {"user": _user_dict(current), **session}

    def create_session(
        self,
        user_id: int,
        *,
        session_ttl: timedelta | float = DEFAULT_SESSION_TTL,
        ip_address: Any = None,
        country_code: Any = None,
        user_agent: Any = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Create a session for an already trusted authentication flow."""

        user_id = self._normalise_user_id(user_id)
        timestamp = _utc_now(now)
        with self._connection(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (user_id,)
            ).fetchone()
            if row is None:
                raise AuthenticationError(
                    "user cannot start a session", code="invalid_credentials"
                )
            self._require_approved(row)
            session = self._insert_session(
                conn,
                user_id=user_id,
                now=timestamp,
                session_ttl=session_ttl,
                ip_address=ip_address,
                country_code=country_code,
                user_agent=user_agent,
            )
        return {"user": _user_dict(row), **session}

    def validate_session(
        self,
        session_token: str,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
        touch: bool = True,
        ip_address: Any = None,
        country_code: Any = None,
        user_agent: Any = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Resolve a bearer session; state-changing handlers set require_csrf."""

        token_hash = _token_digest(session_token)
        csrf_hash = _token_digest(csrf_token) if csrf_token is not None else ""
        timestamp = _utc_now(now)
        if not token_hash:
            raise AuthenticationError(
                "invalid or expired session", code="invalid_session"
            )
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT
                    s.id AS session_id, s.csrf_hash, s.created_at AS session_created_at,
                    s.expires_at, s.last_seen_at, s.revoked_at,
                    s.ip_address, s.country_code, s.user_agent,
                    u.*
                FROM account_sessions AS s
                JOIN account_users AS u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
        if row is None:
            raise AuthenticationError(
                "invalid or expired session", code="invalid_session"
            )
        self._require_approved(row)
        if row["revoked_at"] is not None or (
            _parse_datetime(row["expires_at"], field="expires_at") <= timestamp
        ):
            raise AuthenticationError(
                "invalid or expired session", code="invalid_session"
            )
        if require_csrf and (
            not csrf_hash or not hmac.compare_digest(csrf_hash, row["csrf_hash"])
        ):
            raise AuthorizationError("invalid CSRF token", code="invalid_csrf")

        last_seen = _parse_datetime(row["last_seen_at"], field="last_seen_at")
        normal_ip, normal_country, normal_user_agent = _request_metadata(
            ip_address=ip_address,
            country_code=country_code,
            user_agent=user_agent,
        )
        metadata_changed = any(
            value is not None and value != row[column]
            for value, column in (
                (normal_ip, "ip_address"),
                (normal_country, "country_code"),
                (normal_user_agent, "user_agent"),
            )
        )
        if touch and (
            timestamp - last_seen >= SESSION_TOUCH_INTERVAL or metadata_changed
        ):
            with self._connection(write=True) as conn:
                conn.execute(
                    """
                    UPDATE account_sessions
                    SET last_seen_at = ?,
                        ip_address = COALESCE(?, ip_address),
                        country_code = COALESCE(?, country_code),
                        user_agent = COALESCE(?, user_agent)
                    WHERE id = ? AND revoked_at IS NULL
                    """,
                    (
                        _iso(timestamp),
                        normal_ip,
                        normal_country,
                        normal_user_agent,
                        int(row["session_id"]),
                    ),
                )
        return {
            "user": _user_dict(row),
            "session": {
                "id": int(row["session_id"]),
                "created_at": row["session_created_at"],
                "expires_at": row["expires_at"],
                "last_seen_at": _iso(timestamp) if touch else row["last_seen_at"],
                "ip_address": normal_ip or row["ip_address"],
                "country_code": normal_country or row["country_code"],
                "user_agent": normal_user_agent or row["user_agent"],
            },
        }

    def revoke_session(
        self, session_token: str, *, now: datetime | None = None
    ) -> bool:
        token_hash = _token_digest(session_token)
        if not token_hash:
            return False
        with self._connection(write=True) as conn:
            cursor = conn.execute(
                """
                UPDATE account_sessions SET revoked_at = ?
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (_iso(now), token_hash),
            )
        return cursor.rowcount > 0

    def revoke_all_sessions(
        self,
        user_id: int,
        *,
        except_session_token: str | None = None,
        now: datetime | None = None,
    ) -> int:
        user_id = self._normalise_user_id(user_id)
        except_hash = (
            _token_digest(except_session_token) if except_session_token else ""
        )
        sql = (
            "UPDATE account_sessions SET revoked_at = ? "
            "WHERE user_id = ? AND revoked_at IS NULL"
        )
        params: list[Any] = [_iso(now), user_id]
        if except_hash:
            sql += " AND token_hash <> ?"
            params.append(except_hash)
        with self._connection(write=True) as conn:
            cursor = conn.execute(sql, params)
        return int(cursor.rowcount)

    def update_password(
        self,
        user_id: int,
        current_password: str,
        new_password: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Change a known password and revoke every existing session."""

        user_id = self._normalise_user_id(user_id)
        new_hash = hash_password(new_password)
        timestamp = _iso(now)
        with self._connection(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM account_users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row is None or not verify_password(
                current_password, row["password_hash"]
            ):
                raise AuthenticationError(
                    "invalid current password", code="invalid_credentials"
                )
            self._require_approved(row)
            conn.execute(
                "UPDATE account_users SET password_hash = ?, revision = revision + 1, "
                "updated_at = ? WHERE id = ?",
                (new_hash, timestamp, user_id),
            )
            conn.execute(
                "UPDATE account_sessions SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (timestamp, user_id),
            )
            conn.execute(
                "UPDATE account_password_resets SET used_at = COALESCE(used_at, ?) "
                "WHERE user_id = ?",
                (timestamp, user_id),
            )
            _record_activity(
                conn,
                user_id=user_id,
                event_type="password_changed",
                occurred_at=now,
            )

    def change_password(
        self,
        user_id: int,
        current_password: str,
        new_password: str,
        *,
        current_session_token: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Change a known password and revoke every bearer session.

        ``current_session_token`` is retained only for source compatibility;
        credential changes intentionally require a fresh login so an existing
        bearer token cannot survive the rotation.
        """

        del current_session_token
        self.update_password(user_id, current_password, new_password, now=now)

    def request_password_reset(
        self,
        email: str,
        *,
        sender: PasswordResetSender | SenderCallback | None = None,
        reset_base_url: str | None = None,
        reset_ttl: timedelta | float = DEFAULT_RESET_TTL,
        now: datetime | None = None,
    ) -> dict[str, bool]:
        """Create and deliver a reset URL without revealing account existence.

        The return value is deliberately always ``{"accepted": True}``, even
        for malformed/unknown emails, throttled accounts and delivery failures.
        The injected sender is the only place where the raw reset token exists.
        """

        try:
            normal_email = _normalise_email(email)
        except ValidationError:
            # Perform comparable password work before returning the same result.
            verify_password("not-a-valid-user-password", _dummy_password_hash())
            return {"accepted": True}
        timestamp = _utc_now(now)
        ttl_seconds = _ttl_seconds(reset_ttl, field="reset_ttl", maximum_days=1)
        active_sender = sender or self.password_reset_sender
        active_base_url = str(
            self.reset_base_url if reset_base_url is None else reset_base_url
        ).strip()

        reset_id: int | None = None
        raw_token = ""
        user_id: int | None = None
        with self._connection(write=True) as conn:
            user = conn.execute(
                "SELECT * FROM account_users WHERE email = ?",
                (normal_email,),
            ).fetchone()
            if user is None or self._effective_access_state(user) in {
                "blocked",
                "deleted",
            }:
                # No reset row and no delivery for nonexistent/blocked users.
                return {"accepted": True}
            user_id = int(user["id"])
            cutoff = _iso(timestamp - timedelta(hours=1))
            recent_count = conn.execute(
                """
                SELECT COUNT(*) FROM account_password_resets
                WHERE user_id = ? AND requested_at >= ?
                """,
                (user_id, cutoff),
            ).fetchone()[0]
            if int(recent_count) >= MAX_PASSWORD_RESETS_PER_HOUR:
                return {"accepted": True}
            raw_token, token_hash = _new_token("cs2r_")
            expires_at = _iso(timestamp + timedelta(seconds=ttl_seconds))
            # A newer request invalidates older unused links.
            conn.execute(
                """
                UPDATE account_password_resets
                SET used_at = COALESCE(used_at, ?)
                WHERE user_id = ? AND used_at IS NULL
                """,
                (_iso(timestamp), user_id),
            )
            cursor = conn.execute(
                """
                INSERT INTO account_password_resets(
                    user_id, token_hash, requested_at, expires_at, delivery_state
                ) VALUES (?, ?, ?, ?, 'pending')
                """,
                (user_id, token_hash, _iso(timestamp), expires_at),
            )
            reset_id = int(cursor.lastrowid)

        try:
            reset_url = build_password_reset_url(active_base_url, raw_token)
            message = PasswordResetEmail(
                recipient=normal_email,
                reset_url=reset_url,
                expires_at=_iso(timestamp + timedelta(seconds=ttl_seconds)),
            )
            _send_password_reset(active_sender, message)
        except Exception as exc:  # noqa: BLE001 - keep the public response generic
            with self._connection(write=True) as conn:
                conn.execute(
                    """
                    UPDATE account_password_resets
                    SET delivery_state = 'failed', used_at = COALESCE(used_at, ?)
                    WHERE id = ?
                    """,
                    (_iso(timestamp), reset_id),
                )
            # Never interpolate exception text: SMTP errors can echo content.
            LOGGER.warning(
                "Password reset delivery failed for user_id=%s (%s)",
                user_id,
                type(exc).__name__,
            )
            return {"accepted": True}

        with self._connection(write=True) as conn:
            conn.execute(
                """
                UPDATE account_password_resets
                SET delivery_state = 'sent', delivered_at = ?
                WHERE id = ? AND delivery_state = 'pending'
                """,
                (_iso(timestamp), reset_id),
            )
            _record_activity(
                conn,
                user_id=int(user_id),
                event_type="password_reset_requested",
                payload={"expires_at": message.expires_at},
                occurred_at=now,
            )
        return {"accepted": True}

    def admin_issue_password_reset(
        self,
        target_user_id: int,
        *,
        actor_user_id: int | None = None,
        trusted_local_admin: bool = False,
        deliver: bool = True,
        sender: PasswordResetSender | SenderCallback | None = None,
        reset_base_url: str | None = None,
        reset_ttl: timedelta | float = timedelta(minutes=30),
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Issue one short-lived reset link and revoke the user's sessions.

        The raw URL is returned exactly once to the trusted caller and is never
        persisted or logged.  The database stores only SHA-256 of the token.
        HTTP code should expose this operation only on the localhost owner
        panel.  Email delivery is optional and its configuration/result is
        explicit in the response.
        """

        target_id = self._normalise_user_id(target_user_id)
        timestamp = _utc_now(now)
        ttl_seconds = _ttl_seconds(reset_ttl, field="reset_ttl", maximum_days=1)
        expires_at = _iso(timestamp + timedelta(seconds=ttl_seconds))
        active_sender = sender or self.password_reset_sender
        active_base_url = str(
            self.reset_base_url if reset_base_url is None else reset_base_url
        ).strip()
        raw_token, token_hash = _new_token("cs2r_")
        # Validate the configured URL before mutating sessions or reset rows.
        reset_url = build_password_reset_url(active_base_url, raw_token)

        with self._connection(write=True) as conn:
            actor = self._require_admin(
                conn,
                actor_user_id=actor_user_id,
                trusted_local_admin=trusted_local_admin,
            )
            target = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (target_id,)
            ).fetchone()
            if target is None:
                raise NotFoundError("user not found")
            if self._effective_access_state(target) == "deleted":
                raise ConflictError(
                    "deleted accounts cannot reset passwords",
                    code="account_deleted",
                )
            self._check_revision(target, expected_revision)
            conn.execute(
                """
                UPDATE account_password_resets
                SET used_at = COALESCE(used_at, ?)
                WHERE user_id = ? AND used_at IS NULL
                """,
                (_iso(timestamp), target_id),
            )
            reset_id = int(
                conn.execute(
                    """
                    INSERT INTO account_password_resets(
                        user_id, token_hash, requested_at, expires_at,
                        delivery_state, issued_by_kind, issued_by_user_id
                    ) VALUES (?, ?, ?, ?, 'sent', ?, ?)
                    """,
                    (
                        target_id,
                        token_hash,
                        _iso(timestamp),
                        expires_at,
                        actor["actor_kind"],
                        actor["actor_user_id"],
                    ),
                ).lastrowid
            )
            revoked_sessions = int(
                conn.execute(
                    """
                    UPDATE account_sessions SET revoked_at = ?
                    WHERE user_id = ? AND revoked_at IS NULL
                    """,
                    (_iso(timestamp), target_id),
                ).rowcount
            )
            conn.execute(
                """
                UPDATE account_users
                SET revision = revision + 1, updated_at = ? WHERE id = ?
                """,
                (_iso(timestamp), target_id),
            )
            _record_activity(
                conn,
                user_id=target_id,
                event_type="admin_password_reset_issued",
                payload={
                    **actor,
                    "expires_at": expires_at,
                    "delivery_requested": bool(deliver),
                    "revoked_sessions": revoked_sessions,
                },
                occurred_at=now,
            )
            email = target["email"]
            updated_revision = int(target["revision"]) + 1

        delivery_configured = not isinstance(
            active_sender, DisabledPasswordResetSender
        )
        delivery_state = "not_requested"
        if deliver and not delivery_configured:
            delivery_state = "not_configured"
        elif deliver:
            try:
                _send_password_reset(
                    active_sender,
                    PasswordResetEmail(
                        recipient=email,
                        reset_url=reset_url,
                        expires_at=expires_at,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - never leak sender details
                delivery_state = "failed"
                with self._connection(write=True) as conn:
                    conn.execute(
                        """
                        UPDATE account_password_resets
                        SET delivery_state = 'failed'
                        WHERE id = ? AND used_at IS NULL
                        """,
                        (reset_id,),
                    )
                LOGGER.warning(
                    "Admin reset delivery failed for user_id=%s (%s)",
                    target_id,
                    type(exc).__name__,
                )
            else:
                delivery_state = "sent"
                with self._connection(write=True) as conn:
                    conn.execute(
                        """
                        UPDATE account_password_resets SET delivered_at = ?
                        WHERE id = ? AND used_at IS NULL
                        """,
                        (_iso(timestamp), reset_id),
                    )

        with self._connection(write=True) as conn:
            _record_activity(
                conn,
                user_id=target_id,
                event_type="admin_password_reset_delivery",
                payload={
                    "actor_user_id": actor["actor_user_id"],
                    "actor_kind": actor["actor_kind"],
                    "delivery_state": delivery_state,
                    "delivery_configured": delivery_configured,
                },
                occurred_at=now,
            )
        return {
            "reset_url": reset_url,
            "expires_at": expires_at,
            "delivery_requested": bool(deliver),
            "delivery_configured": delivery_configured,
            "delivery_state": delivery_state,
            "revoked_sessions": revoked_sessions,
            "revision": updated_revision,
        }

    def reset_password(
        self,
        reset_token: str,
        new_password: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Consume one reset token, replace the password and revoke sessions."""

        token_hash = _token_digest(reset_token)
        if not token_hash:
            raise AuthenticationError(
                "invalid or expired reset token", code="invalid_reset_token"
            )
        new_hash = hash_password(new_password)
        timestamp = _utc_now(now)
        with self._connection(write=True) as conn:
            row = conn.execute(
                """
                SELECT r.*, u.access_state, u.disabled_at, u.deleted_at
                FROM account_password_resets AS r
                JOIN account_users AS u ON u.id = r.user_id
                WHERE r.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            invalid = (
                row is None
                or row["used_at"] is not None
                or not (
                    row["delivery_state"] == "sent"
                    or (
                        row["delivery_state"] == "failed"
                        and row["issued_by_kind"]
                        in {"account_admin", "local_dashboard"}
                    )
                )
                or self._effective_access_state(row) in {"blocked", "deleted"}
                or _parse_datetime(row["expires_at"], field="expires_at") <= timestamp
            )
            if invalid:
                raise AuthenticationError(
                    "invalid or expired reset token", code="invalid_reset_token"
                )
            user_id = int(row["user_id"])
            cursor = conn.execute(
                """
                UPDATE account_password_resets SET used_at = ?
                WHERE id = ? AND used_at IS NULL
                """,
                (_iso(timestamp), int(row["id"])),
            )
            if cursor.rowcount != 1:
                raise AuthenticationError(
                    "invalid or expired reset token", code="invalid_reset_token"
                )
            conn.execute(
                "UPDATE account_users SET password_hash = ?, revision = revision + 1, "
                "updated_at = ? WHERE id = ?",
                (new_hash, _iso(timestamp), user_id),
            )
            conn.execute(
                """
                UPDATE account_password_resets
                SET used_at = COALESCE(used_at, ?)
                WHERE user_id = ?
                """,
                (_iso(timestamp), user_id),
            )
            conn.execute(
                """
                UPDATE account_sessions SET revoked_at = ?
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (_iso(timestamp), user_id),
            )
            _record_activity(
                conn,
                user_id=user_id,
                event_type="password_reset_completed",
                occurred_at=now,
            )
            user = conn.execute(
                "SELECT * FROM account_users WHERE id = ?", (user_id,)
            ).fetchone()
        return _user_dict(user)

    @staticmethod
    def _inventory_values(
        payload: dict[str, Any],
        *,
        default_fee_percent: float,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _utc_now(now)
        skin_name = _normalise_text(
            payload.get("skin_name"),
            field="skin_name",
            maximum=255,
            required=True,
        )
        quantity = _bounded_int(
            payload.get("quantity", 1),
            field="quantity",
            minimum=1,
            maximum=10_000,
        )
        buy_price = _bounded_int(
            payload.get("buy_price_cents"),
            field="buy_price_cents",
            minimum=1,
            maximum=100_000_000_00,
        )
        bought_at = _normalise_optional_datetime(
            payload.get("bought_at") or timestamp,
            field="bought_at",
        )
        float_value = _normalise_float(payload.get("float_value"))
        stickers_json = _normalise_stickers(payload.get("stickers", []))
        source_url = _normalise_url(payload.get("source_url"))
        strategy = str(payload.get("strategy") or "manual").strip().lower()
        if strategy not in INVENTORY_STRATEGIES:
            raise ValidationError("unknown inventory strategy")
        fee_percent = _normalise_fee(payload.get("fee_percent", default_fee_percent))
        notes = _normalise_text(payload.get("notes"), field="notes", maximum=4_000)
        listed_price = _optional_cents(
            payload.get("listed_price_cents"), field="listed_price_cents"
        )
        sold_price = _optional_cents(
            payload.get("sold_price_cents"), field="sold_price_cents"
        )
        market_price = _optional_cents(
            payload.get("market_price_cents"), field="market_price_cents"
        )
        raw_status = str(payload.get("status") or "").strip().lower()
        if not raw_status:
            raw_status = "listed" if listed_price else "owned"
        status = raw_status
        if status not in INVENTORY_STATUSES:
            raise ValidationError("unknown inventory status")
        listed_at = _normalise_optional_datetime(
            payload.get("listed_at"), field="listed_at"
        )
        sold_at = _normalise_optional_datetime(payload.get("sold_at"), field="sold_at")
        market_updated_at = _normalise_optional_datetime(
            payload.get("market_price_updated_at"),
            field="market_price_updated_at",
        )

        if status == "owned":
            listed_price = None
            listed_at = None
            sold_price = None
            sold_at = None
        elif status == "listed":
            if listed_price is None:
                raise ValidationError(
                    "listed_price_cents is required for a listed item"
                )
            listed_at = listed_at or _iso(timestamp)
            sold_price = None
            sold_at = None
        elif status == "sold":
            if sold_price is None:
                raise ValidationError("sold_price_cents is required for a sold item")
            sold_at = sold_at or _iso(timestamp)
        # Archived rows retain their previous commercial details.

        if market_price is not None and market_updated_at is None:
            market_updated_at = _iso(timestamp)
        if market_price is None:
            market_updated_at = None
        return {
            "skin_name": skin_name,
            "quantity": quantity,
            "buy_price_cents": buy_price,
            "bought_at": bought_at,
            "float_value": float_value,
            "stickers_json": stickers_json,
            "source_url": source_url,
            "strategy": strategy,
            "status": status,
            "listed_price_cents": listed_price,
            "listed_at": listed_at,
            "sold_price_cents": sold_price,
            "sold_at": sold_at,
            "market_price_cents": market_price,
            "market_price_updated_at": market_updated_at,
            "fee_percent": fee_percent,
            "notes": notes,
        }

    def create_inventory_item(
        self,
        user_id: int,
        *,
        skin_name: str,
        buy_price_cents: int,
        quantity: int = 1,
        bought_at: str | datetime | None = None,
        float_value: float | None = None,
        stickers: Any = None,
        source_url: str | None = None,
        strategy: str = "manual",
        status: str = "",
        listed_price_cents: int | None = None,
        listed_at: str | datetime | None = None,
        sold_price_cents: int | None = None,
        sold_at: str | datetime | None = None,
        market_price_cents: int | None = None,
        market_price_updated_at: str | datetime | None = None,
        fee_percent: float | None = None,
        notes: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        user_id = self._normalise_user_id(user_id)
        timestamp = _iso(now)
        payload = {
            "skin_name": skin_name,
            "buy_price_cents": buy_price_cents,
            "quantity": quantity,
            "bought_at": bought_at,
            "float_value": float_value,
            "stickers": stickers,
            "source_url": source_url,
            "strategy": strategy,
            "status": status,
            "listed_price_cents": listed_price_cents,
            "listed_at": listed_at,
            "sold_price_cents": sold_price_cents,
            "sold_at": sold_at,
            "market_price_cents": market_price_cents,
            "market_price_updated_at": market_price_updated_at,
            "notes": notes,
        }
        with self._connection(write=True) as conn:
            user = self._approved_user_row(conn, user_id)
            payload["fee_percent"] = (
                float(user["default_fee_percent"])
                if fee_percent is None
                else fee_percent
            )
            values = self._inventory_values(
                payload,
                default_fee_percent=float(user["default_fee_percent"]),
                now=now,
            )
            cost_basis_cents = int(values["buy_price_cents"]) * int(
                values["quantity"]
            )
            columns = [
                "user_id",
                *values,
                "cost_basis_cents",
                "revision",
                "created_at",
                "updated_at",
            ]
            cursor = conn.execute(
                f"INSERT INTO account_inventory_items({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [
                    user_id,
                    *values.values(),
                    cost_basis_cents,
                    1,
                    timestamp,
                    timestamp,
                ],
            )
            item_id = int(cursor.lastrowid)
            row = _inventory_owned_row(conn, user_id=user_id, item_id=item_id)
            public = _inventory_dict(row)
            _record_activity(
                conn,
                user_id=user_id,
                inventory_item_id=item_id,
                event_type="inventory_created",
                payload={"item": public},
                occurred_at=now,
            )
        public["analysis"] = analyze_inventory_item(
            public,
            now=now,
            stale_after_days=DEFAULT_STUCK_AFTER_DAYS,
        )
        return public

    def get_inventory_item(
        self,
        user_id: int,
        item_id: int,
        *,
        include_analysis: bool = True,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        user_id = self._normalise_user_id(user_id)
        item_id = _bounded_int(item_id, field="item_id", minimum=1, maximum=2**63 - 1)
        with self._connection() as conn:
            row = _inventory_owned_row(conn, user_id=user_id, item_id=item_id)
            user = conn.execute(
                "SELECT stale_after_days FROM account_users WHERE id = ?", (user_id,)
            ).fetchone()
            origin = conn.execute(
                """
                SELECT legs.trade_id
                FROM account_inventory_trade_legs AS legs
                JOIN account_inventory_trades AS trades
                  ON trades.id = legs.trade_id
                 AND trades.user_id = legs.user_id
                WHERE legs.user_id = ?
                  AND legs.inventory_item_id = ?
                  AND legs.direction = 'incoming'
                  AND trades.status = 'active'
                  AND legs.trade_version = trades.current_version
                ORDER BY legs.id DESC
                LIMIT 1
                """,
                (user_id, item_id),
            ).fetchone()
        item = _inventory_dict(row)
        if origin is not None:
            item["acquisition_type"] = "trade"
            item["trade_id"] = int(origin["trade_id"])
        else:
            item["acquisition_type"] = "purchase"
            item["trade_id"] = None
        if include_analysis:
            item["analysis"] = analyze_inventory_item(
                item,
                now=now,
                stale_after_days=int(user["stale_after_days"]),
            )
        return item

    def list_inventory(
        self,
        user_id: int,
        *,
        status: str = "active",
        search: str = "",
        limit: int = 200,
        offset: int = 0,
        include_analysis: bool = True,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        user_id = self._normalise_user_id(user_id)
        limit = _bounded_int(limit, field="limit", minimum=1, maximum=1_000)
        offset = _bounded_int(offset, field="offset", minimum=0, maximum=1_000_000)
        normal_status = str(status or "active").strip().lower()
        if normal_status not in INVENTORY_STATUSES | {"active", "all"}:
            raise ValidationError("unknown inventory status filter")
        normal_search = _normalise_text(search, field="search", maximum=200) or ""
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        # Internal rows produced while applying/reversing a trade must not
        # clutter the user's Deleted tab.  A manually archived *current*
        # incoming item is intentionally still visible so it can be restored
        # before correcting or voiding its trade.
        clauses.append(
            """
            NOT (
                account_inventory_items.status = 'archived'
                AND EXISTS (
                    SELECT 1
                    FROM account_inventory_trade_legs AS hidden_legs
                    JOIN account_inventory_trades AS hidden_trades
                      ON hidden_trades.id = hidden_legs.trade_id
                     AND hidden_trades.user_id = hidden_legs.user_id
                    WHERE hidden_legs.user_id = account_inventory_items.user_id
                      AND hidden_legs.inventory_item_id = account_inventory_items.id
                      AND (
                          (
                              hidden_legs.direction = 'outgoing'
                              AND (
                                  hidden_legs.inventory_item_id
                                      <> hidden_legs.source_inventory_item_id
                                  OR (
                                      hidden_trades.status = 'active'
                                      AND hidden_legs.trade_version
                                          = hidden_trades.current_version
                                  )
                              )
                          )
                          OR (
                              hidden_legs.direction = 'incoming'
                              AND (
                                  hidden_trades.status = 'voided'
                                  OR hidden_legs.trade_version <> hidden_trades.current_version
                              )
                          )
                      )
                )
            )
            """
        )
        if normal_status == "active":
            clauses.append("status IN ('owned', 'listed')")
        elif normal_status != "all":
            clauses.append("status = ?")
            params.append(normal_status)
        if normal_search:
            clauses.append("skin_name LIKE ? ESCAPE '\\'")
            escaped = (
                normal_search.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            params.append(f"%{escaped}%")
        params.extend((limit, offset))
        with self._connection() as conn:
            user = conn.execute(
                "SELECT stale_after_days FROM account_users WHERE id = ?", (user_id,)
            ).fetchone()
            if user is None:
                raise NotFoundError("user not found")
            rows = conn.execute(
                "SELECT * FROM account_inventory_items WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            item_ids = [int(row["id"]) for row in rows]
            trade_origins: dict[int, int] = {}
            if item_ids:
                placeholders = ",".join("?" for _ in item_ids)
                origin_rows = conn.execute(
                    f"""
                    SELECT legs.inventory_item_id, legs.trade_id
                    FROM account_inventory_trade_legs AS legs
                    JOIN account_inventory_trades AS trades
                      ON trades.id = legs.trade_id
                     AND trades.user_id = legs.user_id
                    WHERE legs.user_id = ?
                      AND legs.inventory_item_id IN ({placeholders})
                      AND legs.direction = 'incoming'
                      AND trades.status = 'active'
                      AND legs.trade_version = trades.current_version
                    ORDER BY legs.id DESC
                    """,
                    (user_id, *item_ids),
                ).fetchall()
                trade_origins = {
                    int(origin["inventory_item_id"]): int(origin["trade_id"])
                    for origin in origin_rows
                }
        items = [_inventory_dict(row) for row in rows]
        for item in items:
            trade_id = trade_origins.get(item["id"])
            item["acquisition_type"] = "trade" if trade_id is not None else "purchase"
            item["trade_id"] = trade_id
        if include_analysis:
            stale_days = int(user["stale_after_days"])
            for item in items:
                item["analysis"] = analyze_inventory_item(
                    item, now=now, stale_after_days=stale_days
                )
        return items

    @staticmethod
    def _normalise_trade_outgoing(value: Any) -> list[dict[str, int]]:
        if not isinstance(value, (list, tuple)):
            raise ValidationError("outgoing must be an array")
        if not 1 <= len(value) <= MAX_TRADE_LEGS_PER_SIDE:
            raise ValidationError(
                f"outgoing must contain between 1 and {MAX_TRADE_LEGS_PER_SIDE} entries"
            )
        result: list[dict[str, int]] = []
        seen: set[int] = set()
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                raise ValidationError(f"outgoing[{index}] must be an object")
            allowed = {
                "inventory_item_id",
                "item_id",
                "quantity",
                "unit_value_cents",
                "declared_unit_value_cents",
                "expected_revision",
            }
            unknown = set(raw) - allowed
            if unknown:
                raise ValidationError(
                    f"unknown outgoing[{index}] fields: {', '.join(sorted(unknown))}"
                )
            raw_item_id = raw.get("inventory_item_id", raw.get("item_id"))
            item_id = _bounded_int(
                raw_item_id,
                field=f"outgoing[{index}].inventory_item_id",
                minimum=1,
                maximum=2**63 - 1,
            )
            if item_id in seen:
                raise ValidationError("an inventory item can appear only once in outgoing")
            seen.add(item_id)
            quantity = (
                None
                if raw.get("quantity") in (None, "")
                else _bounded_int(
                    raw.get("quantity"),
                    field=f"outgoing[{index}].quantity",
                    minimum=1,
                    maximum=10_000,
                )
            )
            raw_unit_value = raw.get(
                "unit_value_cents", raw.get("declared_unit_value_cents")
            )
            unit_value = _bounded_int(
                raw_unit_value,
                field=f"outgoing[{index}].unit_value_cents",
                minimum=1,
                maximum=100_000_000_00,
            )
            expected_revision = _bounded_int(
                raw.get("expected_revision"),
                field=f"outgoing[{index}].expected_revision",
                minimum=1,
                maximum=2**31 - 1,
            )
            result.append(
                {
                    "inventory_item_id": item_id,
                    "quantity": quantity,  # type: ignore[dict-item]
                    "unit_value_cents": unit_value,
                    "expected_revision": expected_revision,
                }
            )
        return result

    @staticmethod
    def _normalise_trade_incoming(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, (list, tuple)):
            raise ValidationError("incoming must be an array")
        if not 1 <= len(value) <= MAX_TRADE_LEGS_PER_SIDE:
            raise ValidationError(
                f"incoming must contain between 1 and {MAX_TRADE_LEGS_PER_SIDE} entries"
            )
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                raise ValidationError(f"incoming[{index}] must be an object")
            allowed = {
                "skin_name",
                "quantity",
                "unit_value_cents",
                "declared_unit_value_cents",
                "float_value",
                "stickers",
                "source_url",
                "strategy",
                "fee_percent",
                "notes",
            }
            unknown = set(raw) - allowed
            if unknown:
                raise ValidationError(
                    f"unknown incoming[{index}] fields: {', '.join(sorted(unknown))}"
                )
            skin_name = _normalise_text(
                raw.get("skin_name"),
                field=f"incoming[{index}].skin_name",
                maximum=255,
                required=True,
            )
            quantity = _bounded_int(
                raw.get("quantity", 1),
                field=f"incoming[{index}].quantity",
                minimum=1,
                maximum=10_000,
            )
            raw_unit_value = raw.get(
                "unit_value_cents", raw.get("declared_unit_value_cents")
            )
            unit_value = _bounded_int(
                raw_unit_value,
                field=f"incoming[{index}].unit_value_cents",
                minimum=1,
                maximum=100_000_000_00,
            )
            result.append(
                {
                    "skin_name": skin_name,
                    "quantity": quantity,
                    "unit_value_cents": unit_value,
                    "float_value": raw.get("float_value"),
                    "stickers": raw.get("stickers", []),
                    "source_url": raw.get("source_url"),
                    "strategy": raw.get("strategy") or "manual",
                    "fee_percent": raw.get("fee_percent"),
                    "notes": raw.get("notes"),
                }
            )
        return result

    @staticmethod
    def _trade_payload_alias(
        primary: Any,
        alias: Any,
        *,
        field: str,
    ) -> Any:
        if primary is not None and alias is not None:
            raise ValidationError(f"use either {field} or {field}_items, not both")
        value = primary if primary is not None else alias
        if value is None:
            raise ValidationError(f"{field} is required")
        return value

    @staticmethod
    def _inventory_snapshot_json(item: dict[str, Any]) -> str:
        return json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _restore_inventory_snapshot(
        conn: sqlite3.Connection,
        *,
        user_id: int,
        item_id: int,
        expected_revision: int,
        snapshot: dict[str, Any],
        updated_at: str,
    ) -> dict[str, Any]:
        stored: dict[str, Any] = {}
        for column in _INVENTORY_STORAGE_COLUMNS:
            if column == "stickers_json":
                stored[column] = _normalise_stickers(snapshot.get("stickers", []))
            elif column == "cost_basis_cents":
                stored[column] = _inventory_cost_basis(snapshot)
            else:
                stored[column] = snapshot.get(column)
        assignments = ", ".join(f"{column} = ?" for column in stored)
        cursor = conn.execute(
            f"""
            UPDATE account_inventory_items
            SET {assignments}, revision = revision + 1, updated_at = ?
            WHERE id = ? AND user_id = ? AND revision = ?
            """,
            [
                *stored.values(),
                updated_at,
                int(item_id),
                int(user_id),
                int(expected_revision),
            ],
        )
        if cursor.rowcount != 1:
            raise ConflictError(
                "inventory trade dependency changed",
                code="trade_dependency_conflict",
            )
        return _inventory_dict(
            _inventory_owned_row(conn, user_id=user_id, item_id=item_id)
        )

    @staticmethod
    def _allocate_trade_cost_basis(
        total_cost_basis: int,
        incoming: list[dict[str, Any]],
    ) -> list[int]:
        weights = [
            int(entry["unit_value_cents"]) * int(entry["quantity"])
            for entry in incoming
        ]
        total_weight = sum(weights)
        if total_weight <= 0:
            raise ValidationError("incoming declared value must be positive")
        allocations = [
            int(total_cost_basis) * weight // total_weight for weight in weights
        ]
        remaining = int(total_cost_basis) - sum(allocations)
        order = sorted(
            range(len(incoming)),
            key=lambda index: (
                -((int(total_cost_basis) * weights[index]) % total_weight),
                index,
            ),
        )
        for index in order[:remaining]:
            allocations[index] += 1
        return allocations

    def _validate_trade_update_outgoing(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        trade_id: int,
        trade_version: int,
        outgoing: list[dict[str, Any]],
    ) -> None:
        current_legs = conn.execute(
            """
            SELECT * FROM account_inventory_trade_legs
            WHERE trade_id = ? AND user_id = ? AND trade_version = ?
            ORDER BY id
            """,
            (trade_id, user_id, trade_version),
        ).fetchall()
        outgoing_sources = {
            int(row["source_inventory_item_id"]): row
            for row in current_legs
            if row["direction"] == "outgoing"
            and row["source_inventory_item_id"] is not None
        }
        current_incoming = {
            int(row["inventory_item_id"])
            for row in current_legs
            if row["direction"] == "incoming"
        }
        for request in outgoing:
            item_id = int(request["inventory_item_id"])
            if item_id in current_incoming:
                raise ValidationError(
                    "a trade cannot receive and send the same current-version item"
                )
            row = _inventory_owned_row(conn, user_id=user_id, item_id=item_id)
            if int(row["revision"]) != int(request["expected_revision"]):
                raise ConflictError(
                    "outgoing inventory item was changed in another request",
                    code="trade_dependency_conflict",
                )
            old_leg = outgoing_sources.get(item_id)
            if old_leg is None:
                if row["status"] not in {"owned", "listed"}:
                    raise ValidationError(
                        "only an owned or listed item can be sent in a trade"
                    )
                available = int(row["quantity"])
            else:
                source_snapshot = _json_loads(old_leg["source_snapshot_json"], {})
                available = int(source_snapshot.get("quantity") or 0)
            requested_quantity = request.get("quantity") or available
            if int(requested_quantity) > available:
                raise ValidationError("outgoing quantity exceeds the available position")

    def _apply_trade_outgoing(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        trade_id: int,
        trade_version: int,
        outgoing: list[dict[str, Any]],
        timestamp: str,
        occurred_at: datetime | None,
        enforce_expected_revision: bool,
    ) -> tuple[list[sqlite3.Row], int]:
        legs: list[sqlite3.Row] = []
        total_cost_basis = 0
        for request in outgoing:
            item_id = int(request["inventory_item_id"])
            source_row = _inventory_owned_row(
                conn, user_id=user_id, item_id=item_id
            )
            source_before = _inventory_dict(source_row)
            if source_before["status"] not in {"owned", "listed"}:
                raise ValidationError(
                    "only an owned or listed item can be sent in a trade"
                )
            if enforce_expected_revision and source_before["revision"] != int(
                request["expected_revision"]
            ):
                raise ConflictError(
                    "outgoing inventory item was changed in another request",
                    code="trade_dependency_conflict",
                )
            source_quantity = int(source_before["quantity"])
            quantity = int(request.get("quantity") or source_quantity)
            if quantity < 1 or quantity > source_quantity:
                raise ValidationError("outgoing quantity exceeds the available position")
            source_cost_basis = _inventory_cost_basis(source_before)

            if quantity == source_quantity:
                cursor = conn.execute(
                    """
                    UPDATE account_inventory_items
                    SET status = 'archived', revision = revision + 1,
                        updated_at = ?
                    WHERE id = ? AND user_id = ? AND revision = ?
                    """,
                    (timestamp, item_id, user_id, source_before["revision"]),
                )
                if cursor.rowcount != 1:
                    raise ConflictError(
                        "outgoing inventory item was changed in another request",
                        code="trade_dependency_conflict",
                    )
                artifact_row = _inventory_owned_row(
                    conn, user_id=user_id, item_id=item_id
                )
                artifact = _inventory_dict(artifact_row)
                source_after = artifact
                outgoing_cost_basis = source_cost_basis
                artifact_item_id = item_id
            else:
                outgoing_cost_basis = source_cost_basis * quantity // source_quantity
                remaining_cost_basis = source_cost_basis - outgoing_cost_basis
                cursor = conn.execute(
                    """
                    UPDATE account_inventory_items
                    SET quantity = ?, cost_basis_cents = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ? AND user_id = ? AND revision = ?
                    """,
                    (
                        source_quantity - quantity,
                        remaining_cost_basis,
                        timestamp,
                        item_id,
                        user_id,
                        source_before["revision"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConflictError(
                        "outgoing inventory item was changed in another request",
                        code="trade_dependency_conflict",
                    )
                source_after = _inventory_dict(
                    _inventory_owned_row(conn, user_id=user_id, item_id=item_id)
                )
                clone_values = {
                    column: source_row[column]
                    for column in _INVENTORY_STORAGE_COLUMNS
                    if column not in {"quantity", "status", "cost_basis_cents"}
                }
                clone_values.update(
                    {
                        "quantity": quantity,
                        "status": "archived",
                        "cost_basis_cents": outgoing_cost_basis,
                    }
                )
                clone_columns = [
                    "user_id",
                    *clone_values,
                    "revision",
                    "created_at",
                    "updated_at",
                ]
                cursor = conn.execute(
                    f"INSERT INTO account_inventory_items({', '.join(clone_columns)}) "
                    f"VALUES ({', '.join('?' for _ in clone_columns)})",
                    [user_id, *clone_values.values(), 1, timestamp, timestamp],
                )
                artifact_item_id = int(cursor.lastrowid)
                artifact_row = _inventory_owned_row(
                    conn, user_id=user_id, item_id=artifact_item_id
                )
                artifact = _inventory_dict(artifact_row)

            total_cost_basis += outgoing_cost_basis
            cursor = conn.execute(
                """
                INSERT INTO account_inventory_trade_legs(
                    trade_id, user_id, trade_version, direction,
                    inventory_item_id, source_inventory_item_id, quantity,
                    unit_value_cents, cost_basis_cents, item_snapshot_json,
                    source_snapshot_json, inventory_revision, source_revision,
                    created_at
                ) VALUES (?, ?, ?, 'outgoing', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    user_id,
                    trade_version,
                    artifact_item_id,
                    item_id,
                    quantity,
                    int(request["unit_value_cents"]),
                    outgoing_cost_basis,
                    self._inventory_snapshot_json(artifact),
                    self._inventory_snapshot_json(source_before),
                    artifact["revision"],
                    source_after["revision"],
                    timestamp,
                ),
            )
            leg = conn.execute(
                "SELECT * FROM account_inventory_trade_legs WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
            legs.append(leg)
            _record_activity(
                conn,
                user_id=user_id,
                inventory_item_id=item_id,
                event_type="inventory_trade_outgoing",
                payload={
                    "trade_id": trade_id,
                    "trade_version": trade_version,
                    "quantity": quantity,
                    "artifact_inventory_item_id": artifact_item_id,
                    "cost_basis_cents": outgoing_cost_basis,
                },
                occurred_at=occurred_at,
            )
        return legs, total_cost_basis

    def _apply_trade_incoming(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        user: sqlite3.Row,
        trade_id: int,
        trade_version: int,
        incoming: list[dict[str, Any]],
        allocations: list[int],
        traded_at: str,
        timestamp: str,
        occurred_at: datetime | None,
    ) -> list[sqlite3.Row]:
        legs: list[sqlite3.Row] = []
        for entry, cost_basis_cents in zip(incoming, allocations, strict=True):
            quantity = int(entry["quantity"])
            buy_price_cents = max(
                1, (int(cost_basis_cents) * 2 + quantity) // (2 * quantity)
            )
            fee_percent = entry.get("fee_percent")
            payload = {
                "skin_name": entry["skin_name"],
                "quantity": quantity,
                "buy_price_cents": buy_price_cents,
                "bought_at": traded_at,
                "float_value": entry.get("float_value"),
                "stickers": entry.get("stickers", []),
                "source_url": entry.get("source_url"),
                "strategy": entry.get("strategy") or "manual",
                "status": "owned",
                "market_price_cents": int(entry["unit_value_cents"]),
                "market_price_updated_at": traded_at,
                "fee_percent": (
                    float(user["default_fee_percent"])
                    if fee_percent is None
                    else fee_percent
                ),
                "notes": entry.get("notes"),
            }
            values = self._inventory_values(
                payload,
                default_fee_percent=float(user["default_fee_percent"]),
                now=occurred_at,
            )
            columns = [
                "user_id",
                *values,
                "cost_basis_cents",
                "revision",
                "created_at",
                "updated_at",
            ]
            cursor = conn.execute(
                f"INSERT INTO account_inventory_items({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [
                    user_id,
                    *values.values(),
                    int(cost_basis_cents),
                    1,
                    timestamp,
                    timestamp,
                ],
            )
            item_id = int(cursor.lastrowid)
            item = _inventory_dict(
                _inventory_owned_row(conn, user_id=user_id, item_id=item_id)
            )
            cursor = conn.execute(
                """
                INSERT INTO account_inventory_trade_legs(
                    trade_id, user_id, trade_version, direction,
                    inventory_item_id, source_inventory_item_id, quantity,
                    unit_value_cents, cost_basis_cents, item_snapshot_json,
                    source_snapshot_json, inventory_revision, source_revision,
                    created_at
                ) VALUES (?, ?, ?, 'incoming', ?, NULL, ?, ?, ?, ?, NULL, ?, NULL, ?)
                """,
                (
                    trade_id,
                    user_id,
                    trade_version,
                    item_id,
                    quantity,
                    int(entry["unit_value_cents"]),
                    int(cost_basis_cents),
                    self._inventory_snapshot_json(item),
                    item["revision"],
                    timestamp,
                ),
            )
            leg = conn.execute(
                "SELECT * FROM account_inventory_trade_legs WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
            legs.append(leg)
            _record_activity(
                conn,
                user_id=user_id,
                inventory_item_id=item_id,
                event_type="inventory_trade_incoming",
                payload={
                    "trade_id": trade_id,
                    "trade_version": trade_version,
                    "quantity": quantity,
                    "unit_value_cents": int(entry["unit_value_cents"]),
                    "cost_basis_cents": int(cost_basis_cents),
                },
                occurred_at=occurred_at,
            )
        return legs

    def _reverse_inventory_trade(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        trade_id: int,
        trade_version: int,
        timestamp: str,
        occurred_at: datetime | None,
    ) -> None:
        legs = conn.execute(
            """
            SELECT * FROM account_inventory_trade_legs
            WHERE trade_id = ? AND user_id = ? AND trade_version = ?
            ORDER BY CASE direction WHEN 'incoming' THEN 0 ELSE 1 END, id
            """,
            (trade_id, user_id, trade_version),
        ).fetchall()
        if not legs or {row["direction"] for row in legs} != TRADE_DIRECTIONS:
            raise ConflictError(
                "inventory trade has incomplete current legs",
                code="trade_dependency_conflict",
            )

        # Validate every dependency before mutating anything.  This produces a
        # deterministic conflict instead of partially reversing a trade whose
        # received item has already been sold, edited or traded onward.
        for leg in legs:
            item_row = _inventory_owned_row(
                conn,
                user_id=user_id,
                item_id=int(leg["inventory_item_id"]),
            )
            if int(item_row["revision"]) != int(leg["inventory_revision"]):
                raise ConflictError(
                    "inventory trade dependency changed",
                    code="trade_dependency_conflict",
                )
            if leg["direction"] == "outgoing":
                source_row = _inventory_owned_row(
                    conn,
                    user_id=user_id,
                    item_id=int(leg["source_inventory_item_id"]),
                )
                if int(source_row["revision"]) != int(leg["source_revision"]):
                    raise ConflictError(
                        "inventory trade dependency changed",
                        code="trade_dependency_conflict",
                    )

        for leg in legs:
            if leg["direction"] == "incoming":
                item_id = int(leg["inventory_item_id"])
                cursor = conn.execute(
                    """
                    UPDATE account_inventory_items
                    SET status = 'archived', revision = revision + 1,
                        updated_at = ?
                    WHERE id = ? AND user_id = ? AND revision = ?
                    """,
                    (
                        timestamp,
                        item_id,
                        user_id,
                        int(leg["inventory_revision"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConflictError(
                        "inventory trade dependency changed",
                        code="trade_dependency_conflict",
                    )
                _record_activity(
                    conn,
                    user_id=user_id,
                    inventory_item_id=item_id,
                    event_type="inventory_trade_incoming_archived",
                    payload={
                        "trade_id": trade_id,
                        "trade_version": trade_version,
                    },
                    occurred_at=occurred_at,
                )
                continue

            source_id = int(leg["source_inventory_item_id"])
            source_snapshot = _json_loads(leg["source_snapshot_json"], {})
            if not isinstance(source_snapshot, dict) or not source_snapshot:
                raise ConflictError(
                    "inventory trade restore snapshot is unavailable",
                    code="trade_dependency_conflict",
                )
            restored = self._restore_inventory_snapshot(
                conn,
                user_id=user_id,
                item_id=source_id,
                expected_revision=int(leg["source_revision"]),
                snapshot=source_snapshot,
                updated_at=timestamp,
            )
            _record_activity(
                conn,
                user_id=user_id,
                inventory_item_id=source_id,
                event_type="inventory_trade_outgoing_restored",
                payload={
                    "trade_id": trade_id,
                    "trade_version": trade_version,
                    "restored_revision": restored["revision"],
                },
                occurred_at=occurred_at,
            )

    def _inventory_trade_public(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_history: bool = False,
    ) -> dict[str, Any]:
        trade = _trade_dict(row)
        if include_history:
            legs = conn.execute(
                """
                SELECT * FROM account_inventory_trade_legs
                WHERE trade_id = ? AND user_id = ?
                ORDER BY trade_version, id
                """,
                (trade["id"], trade["user_id"]),
            ).fetchall()
        else:
            legs = conn.execute(
                """
                SELECT * FROM account_inventory_trade_legs
                WHERE trade_id = ? AND user_id = ? AND trade_version = ?
                ORDER BY CASE direction WHEN 'outgoing' THEN 0 ELSE 1 END, id
                """,
                (trade["id"], trade["user_id"], trade["current_version"]),
            ).fetchall()
        public_legs: list[dict[str, Any]] = []
        for leg in legs:
            snapshot = _json_loads(leg["item_snapshot_json"], {})
            source_id = (
                int(leg["source_inventory_item_id"])
                if leg["source_inventory_item_id"] is not None
                else None
            )
            public_item_id = (
                source_id if leg["direction"] == "outgoing" else int(leg["inventory_item_id"])
            )
            current_item = None
            try:
                current_item = _inventory_dict(
                    _inventory_owned_row(
                        conn,
                        user_id=trade["user_id"],
                        item_id=int(public_item_id),
                    )
                )
            except NotFoundError:
                pass
            public_legs.append(
                {
                    "id": int(leg["id"]),
                    "trade_id": int(leg["trade_id"]),
                    "trade_version": int(leg["trade_version"]),
                    "direction": leg["direction"],
                    "inventory_item_id": int(public_item_id),
                    "artifact_inventory_item_id": int(leg["inventory_item_id"]),
                    "source_inventory_item_id": source_id,
                    "quantity": int(leg["quantity"]),
                    "unit_value_cents": int(leg["unit_value_cents"]),
                    "total_value_cents": int(leg["unit_value_cents"])
                    * int(leg["quantity"]),
                    "cost_basis_cents": int(leg["cost_basis_cents"]),
                    "skin_name": (
                        snapshot.get("skin_name")
                        if isinstance(snapshot, dict)
                        else None
                    )
                    or (
                        current_item.get("skin_name")
                        if current_item is not None
                        else None
                    ),
                    "inventory_revision": (
                        current_item["revision"] if current_item is not None else None
                    ),
                    "expected_revision": (
                        current_item["revision"] if current_item is not None else None
                    ),
                    "item": snapshot if isinstance(snapshot, dict) else {},
                    "current_item": current_item,
                    "created_at": leg["created_at"],
                }
            )
        current_legs = [
            leg
            for leg in public_legs
            if leg["trade_version"] == trade["current_version"]
        ]
        outgoing = [leg for leg in current_legs if leg["direction"] == "outgoing"]
        incoming = [leg for leg in current_legs if leg["direction"] == "incoming"]
        outgoing_value = sum(leg["total_value_cents"] for leg in outgoing)
        incoming_value = sum(leg["total_value_cents"] for leg in incoming)
        outgoing_basis = sum(leg["cost_basis_cents"] for leg in outgoing)
        incoming_basis = sum(leg["cost_basis_cents"] for leg in incoming)
        trade.update(
            {
                "outgoing": outgoing,
                "incoming": incoming,
                "legs": public_legs if include_history else current_legs,
                "totals": {
                    "outgoing_value_cents": outgoing_value,
                    "incoming_value_cents": incoming_value,
                    "value_difference_cents": incoming_value - outgoing_value,
                    "outgoing_cost_basis_cents": outgoing_basis,
                    "incoming_cost_basis_cents": incoming_basis,
                },
                "outgoing_value_cents": outgoing_value,
                "incoming_value_cents": incoming_value,
                "value_difference_cents": incoming_value - outgoing_value,
            }
        )
        return trade

    def create_inventory_trade(
        self,
        user_id: int,
        *,
        outgoing: Any = None,
        incoming: Any = None,
        outgoing_items: Any = None,
        incoming_items: Any = None,
        traded_at: str | datetime | None = None,
        notes: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        user_id = self._normalise_user_id(user_id)
        outgoing_value = self._trade_payload_alias(
            outgoing, outgoing_items, field="outgoing"
        )
        incoming_value = self._trade_payload_alias(
            incoming, incoming_items, field="incoming"
        )
        outgoing_requests = self._normalise_trade_outgoing(outgoing_value)
        incoming_requests = self._normalise_trade_incoming(incoming_value)
        timestamp = _iso(now)
        trade_time = _normalise_optional_datetime(
            traded_at or now or _utc_now(), field="traded_at"
        )
        safe_notes = _normalise_text(notes, field="notes", maximum=4_000)
        with self._connection(write=True) as conn:
            user = self._approved_user_row(conn, user_id)
            cursor = conn.execute(
                """
                INSERT INTO account_inventory_trades(
                    user_id, traded_at, notes, status, revision,
                    current_version, created_at, updated_at, voided_at
                ) VALUES (?, ?, ?, 'active', 1, 1, ?, ?, NULL)
                """,
                (user_id, trade_time, safe_notes, timestamp, timestamp),
            )
            trade_id = int(cursor.lastrowid)
            _outgoing_legs, outgoing_basis = self._apply_trade_outgoing(
                conn,
                user_id=user_id,
                trade_id=trade_id,
                trade_version=1,
                outgoing=outgoing_requests,
                timestamp=timestamp,
                occurred_at=now,
                enforce_expected_revision=True,
            )
            allocations = self._allocate_trade_cost_basis(
                outgoing_basis, incoming_requests
            )
            self._apply_trade_incoming(
                conn,
                user_id=user_id,
                user=user,
                trade_id=trade_id,
                trade_version=1,
                incoming=incoming_requests,
                allocations=allocations,
                traded_at=trade_time,
                timestamp=timestamp,
                occurred_at=now,
            )
            row = _trade_owned_row(conn, user_id=user_id, trade_id=trade_id)
            public = self._inventory_trade_public(conn, row)
            _record_activity(
                conn,
                user_id=user_id,
                event_type="inventory_trade_created",
                payload={
                    "trade_id": trade_id,
                    "trade_version": 1,
                    "totals": public["totals"],
                },
                occurred_at=now,
            )
        return public

    def get_inventory_trade(
        self,
        user_id: int,
        trade_id: int,
        *,
        include_history: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        del now
        user_id = self._normalise_user_id(user_id)
        trade_id = _bounded_int(
            trade_id, field="trade_id", minimum=1, maximum=2**63 - 1
        )
        with self._connection() as conn:
            row = _trade_owned_row(conn, user_id=user_id, trade_id=trade_id)
            return self._inventory_trade_public(
                conn, row, include_history=bool(include_history)
            )

    def list_inventory_trades(
        self,
        user_id: int,
        *,
        status: str = "active",
        limit: int = 100,
        offset: int = 0,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        del now
        user_id = self._normalise_user_id(user_id)
        normal_status = str(status or "active").strip().lower()
        if normal_status not in TRADE_STATUSES | {"all"}:
            raise ValidationError("unknown inventory trade status filter")
        limit = _bounded_int(limit, field="limit", minimum=1, maximum=200)
        offset = _bounded_int(offset, field="offset", minimum=0, maximum=1_000_000)
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if normal_status != "all":
            clauses.append("status = ?")
            params.append(normal_status)
        params.extend((limit, offset))
        with self._connection() as conn:
            user = conn.execute(
                "SELECT 1 FROM account_users WHERE id = ?", (user_id,)
            ).fetchone()
            if user is None:
                raise NotFoundError("user not found")
            rows = conn.execute(
                "SELECT * FROM account_inventory_trades WHERE "
                + " AND ".join(clauses)
                + " ORDER BY traded_at DESC, id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            return [self._inventory_trade_public(conn, row) for row in rows]

    def update_inventory_trade(
        self,
        user_id: int,
        trade_id: int,
        *,
        outgoing: Any = None,
        incoming: Any = None,
        outgoing_items: Any = None,
        incoming_items: Any = None,
        traded_at: str | datetime | None = None,
        notes: str | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        user_id = self._normalise_user_id(user_id)
        trade_id = _bounded_int(
            trade_id, field="trade_id", minimum=1, maximum=2**63 - 1
        )
        expected = _bounded_int(
            expected_revision,
            field="expected_revision",
            minimum=1,
            maximum=2**31 - 1,
        )
        outgoing_requests = self._normalise_trade_outgoing(
            self._trade_payload_alias(outgoing, outgoing_items, field="outgoing")
        )
        incoming_requests = self._normalise_trade_incoming(
            self._trade_payload_alias(incoming, incoming_items, field="incoming")
        )
        timestamp = _iso(now)
        safe_notes = _normalise_text(notes, field="notes", maximum=4_000)
        with self._connection(write=True) as conn:
            user = self._approved_user_row(conn, user_id)
            current_row = _trade_owned_row(
                conn, user_id=user_id, trade_id=trade_id
            )
            current = _trade_dict(current_row)
            if current["status"] != "active":
                raise ValidationError("only an active inventory trade can be updated")
            if current["revision"] != expected:
                raise ConflictError("inventory trade was changed in another request")
            self._validate_trade_update_outgoing(
                conn,
                user_id=user_id,
                trade_id=trade_id,
                trade_version=current["current_version"],
                outgoing=outgoing_requests,
            )
            self._reverse_inventory_trade(
                conn,
                user_id=user_id,
                trade_id=trade_id,
                trade_version=current["current_version"],
                timestamp=timestamp,
                occurred_at=now,
            )
            next_version = current["current_version"] + 1
            trade_time = (
                current["traded_at"]
                if traded_at in (None, "")
                else _normalise_optional_datetime(traded_at, field="traded_at")
            )
            _outgoing_legs, outgoing_basis = self._apply_trade_outgoing(
                conn,
                user_id=user_id,
                trade_id=trade_id,
                trade_version=next_version,
                outgoing=outgoing_requests,
                timestamp=timestamp,
                occurred_at=now,
                enforce_expected_revision=False,
            )
            allocations = self._allocate_trade_cost_basis(
                outgoing_basis, incoming_requests
            )
            self._apply_trade_incoming(
                conn,
                user_id=user_id,
                user=user,
                trade_id=trade_id,
                trade_version=next_version,
                incoming=incoming_requests,
                allocations=allocations,
                traded_at=trade_time,
                timestamp=timestamp,
                occurred_at=now,
            )
            cursor = conn.execute(
                """
                UPDATE account_inventory_trades
                SET traded_at = ?, notes = ?, current_version = ?,
                    revision = revision + 1, updated_at = ?
                WHERE id = ? AND user_id = ? AND revision = ? AND status = 'active'
                """,
                (
                    trade_time,
                    safe_notes,
                    next_version,
                    timestamp,
                    trade_id,
                    user_id,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("inventory trade was changed in another request")
            row = _trade_owned_row(conn, user_id=user_id, trade_id=trade_id)
            public = self._inventory_trade_public(conn, row)
            _record_activity(
                conn,
                user_id=user_id,
                event_type="inventory_trade_updated",
                payload={
                    "trade_id": trade_id,
                    "previous_version": current["current_version"],
                    "trade_version": next_version,
                    "totals": public["totals"],
                },
                occurred_at=now,
            )
        return public

    def void_inventory_trade(
        self,
        user_id: int,
        trade_id: int,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        user_id = self._normalise_user_id(user_id)
        trade_id = _bounded_int(
            trade_id, field="trade_id", minimum=1, maximum=2**63 - 1
        )
        expected = _bounded_int(
            expected_revision,
            field="expected_revision",
            minimum=1,
            maximum=2**31 - 1,
        )
        timestamp = _iso(now)
        with self._connection(write=True) as conn:
            self._approved_user_row(conn, user_id)
            current_row = _trade_owned_row(
                conn, user_id=user_id, trade_id=trade_id
            )
            current = _trade_dict(current_row)
            if current["status"] != "active":
                raise ValidationError("only an active inventory trade can be voided")
            if current["revision"] != expected:
                raise ConflictError("inventory trade was changed in another request")
            self._reverse_inventory_trade(
                conn,
                user_id=user_id,
                trade_id=trade_id,
                trade_version=current["current_version"],
                timestamp=timestamp,
                occurred_at=now,
            )
            cursor = conn.execute(
                """
                UPDATE account_inventory_trades
                SET status = 'voided', voided_at = ?, revision = revision + 1,
                    updated_at = ?
                WHERE id = ? AND user_id = ? AND revision = ? AND status = 'active'
                """,
                (timestamp, timestamp, trade_id, user_id, expected),
            )
            if cursor.rowcount != 1:
                raise ConflictError("inventory trade was changed in another request")
            row = _trade_owned_row(conn, user_id=user_id, trade_id=trade_id)
            public = self._inventory_trade_public(conn, row)
            _record_activity(
                conn,
                user_id=user_id,
                event_type="inventory_trade_voided",
                payload={
                    "trade_id": trade_id,
                    "trade_version": current["current_version"],
                    "totals": public["totals"],
                },
                occurred_at=now,
            )
        return public

    def update_inventory_item(
        self,
        user_id: int,
        item_id: int,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        user_id = self._normalise_user_id(user_id)
        item_id = _bounded_int(item_id, field="item_id", minimum=1, maximum=2**63 - 1)
        allowed = {
            "skin_name",
            "quantity",
            "buy_price_cents",
            "bought_at",
            "float_value",
            "stickers",
            "source_url",
            "strategy",
            "status",
            "listed_price_cents",
            "listed_at",
            "sold_price_cents",
            "sold_at",
            "market_price_cents",
            "market_price_updated_at",
            "fee_percent",
            "notes",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(
                f"unknown inventory fields: {', '.join(sorted(unknown))}"
            )
        if not changes:
            return self.get_inventory_item(user_id, item_id, now=now)
        with self._connection(write=True) as conn:
            user = self._approved_user_row(conn, user_id)
            existing_row = _inventory_owned_row(conn, user_id=user_id, item_id=item_id)
            _ensure_inventory_user_mutable(
                conn,
                user_id=user_id,
                item_id=item_id,
            )
            existing = _inventory_dict(existing_row)
            if expected_revision is not None:
                revision = _bounded_int(
                    expected_revision,
                    field="expected_revision",
                    minimum=1,
                    maximum=2**31 - 1,
                )
                if revision != existing["revision"]:
                    raise ConflictError("inventory item was changed in another request")
            merged = {**existing, **changes}
            # The form has no separate status selector.  Listing/unlisting can
            # therefore be expressed by setting/clearing listed_price_cents.
            if "status" not in changes and "listed_price_cents" in changes:
                if (
                    changes["listed_price_cents"] in (None, "")
                    and existing["status"] == "listed"
                ):
                    merged["status"] = "owned"
                elif changes["listed_price_cents"] not in (None, "") and existing[
                    "status"
                ] in {
                    "owned",
                    "listed",
                }:
                    merged["status"] = "listed"
            values = self._inventory_values(
                merged,
                default_fee_percent=float(user["default_fee_percent"]),
                now=now,
            )
            cost_basis_cents = existing.get("cost_basis_cents")
            if (
                cost_basis_cents is None
                or "buy_price_cents" in changes
                or "quantity" in changes
            ):
                cost_basis_cents = int(values["buy_price_cents"]) * int(
                    values["quantity"]
                )
            timestamp = _iso(now)
            assignments = ", ".join(f"{column} = ?" for column in values)
            cursor = conn.execute(
                f"""
                UPDATE account_inventory_items
                SET {assignments}, cost_basis_cents = ?,
                    revision = revision + 1, updated_at = ?
                WHERE id = ? AND user_id = ? AND revision = ?
                """,
                [
                    *values.values(),
                    cost_basis_cents,
                    timestamp,
                    item_id,
                    user_id,
                    existing["revision"],
                ],
            )
            if cursor.rowcount != 1:
                raise ConflictError("inventory item was changed in another request")
            updated_row = _inventory_owned_row(conn, user_id=user_id, item_id=item_id)
            updated = _inventory_dict(updated_row)
            changed_fields = sorted(
                key for key in allowed if existing.get(key) != updated.get(key)
            )
            _record_activity(
                conn,
                user_id=user_id,
                inventory_item_id=item_id,
                event_type="inventory_updated",
                payload={
                    "changed_fields": changed_fields,
                    "before": existing,
                    "after": updated,
                },
                occurred_at=now,
            )
        updated["analysis"] = analyze_inventory_item(
            updated,
            now=now,
            stale_after_days=int(user["stale_after_days"]),
        )
        return updated

    def _require_inventory_user_mutable(self, user_id: int, item_id: int) -> None:
        """Run the trade-managed-row guard before wrapper-specific checks."""

        normal_user_id = self._normalise_user_id(user_id)
        normal_item_id = _bounded_int(
            item_id,
            field="item_id",
            minimum=1,
            maximum=2**63 - 1,
        )
        with self._connection() as conn:
            _inventory_owned_row(
                conn,
                user_id=normal_user_id,
                item_id=normal_item_id,
            )
            _ensure_inventory_user_mutable(
                conn,
                user_id=normal_user_id,
                item_id=normal_item_id,
            )

    def mark_inventory_listed(
        self,
        user_id: int,
        item_id: int,
        listed_price_cents: int,
        *,
        listed_at: str | datetime | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self.update_inventory_item(
            user_id,
            item_id,
            expected_revision=expected_revision,
            now=now,
            status="listed",
            listed_price_cents=listed_price_cents,
            listed_at=listed_at or now or _utc_now(),
        )

    def mark_inventory_sold(
        self,
        user_id: int,
        item_id: int,
        sold_price_cents: int,
        *,
        sold_at: str | datetime | None = None,
        fee_percent: float | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self._require_inventory_user_mutable(user_id, item_id)
        before = self.get_inventory_item(
            user_id, item_id, include_analysis=False, now=now
        )
        if before["status"] not in {"owned", "listed"}:
            raise ValidationError("only an owned or listed item can be marked sold")
        changes: dict[str, Any] = {
            "status": "sold",
            "sold_price_cents": sold_price_cents,
            "sold_at": sold_at or now or _utc_now(),
        }
        if fee_percent is not None:
            changes["fee_percent"] = fee_percent
        item = self.update_inventory_item(
            user_id,
            item_id,
            expected_revision=(
                before["revision"] if expected_revision is None else expected_revision
            ),
            now=now,
            **changes,
        )
        # Add the semantic event separately from the full update snapshot.
        with self._connection(write=True) as conn:
            _inventory_owned_row(conn, user_id=int(user_id), item_id=int(item_id))
            _record_activity(
                conn,
                user_id=int(user_id),
                inventory_item_id=int(item_id),
                event_type="inventory_sold",
                payload={
                    "previous_status": before["status"],
                    "sold_price_cents": item["sold_price_cents"],
                    "sold_at": item["sold_at"],
                    "fee_percent": item["fee_percent"],
                    "quantity": item["quantity"],
                    "profit_cents": item["analysis"]["profit_cents"],
                },
                occurred_at=now,
            )
        return item

    def update_inventory_sale(
        self,
        user_id: int,
        item_id: int,
        sold_price_cents: int,
        *,
        sold_at: str | datetime | None = None,
        fee_percent: float | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Change commercial details for an item that is already sold."""

        self._require_inventory_user_mutable(user_id, item_id)
        before = self.get_inventory_item(user_id, item_id, now=now)
        if before["status"] != "sold":
            raise ValidationError("only a sold item can have its sale updated")
        changes: dict[str, Any] = {
            "status": "sold",
            "sold_price_cents": sold_price_cents,
        }
        # Omitting the date means "keep it", not "replace it with now".
        if sold_at not in (None, ""):
            changes["sold_at"] = sold_at
        if fee_percent is not None:
            changes["fee_percent"] = fee_percent
        item = self.update_inventory_item(
            user_id,
            item_id,
            expected_revision=(
                before["revision"] if expected_revision is None else expected_revision
            ),
            now=now,
            **changes,
        )
        with self._connection(write=True) as conn:
            _inventory_owned_row(conn, user_id=int(user_id), item_id=int(item_id))
            _record_activity(
                conn,
                user_id=int(user_id),
                inventory_item_id=int(item_id),
                event_type="inventory_sale_updated",
                payload={
                    "before": {
                        "sold_price_cents": before["sold_price_cents"],
                        "sold_at": before["sold_at"],
                        "fee_percent": before["fee_percent"],
                        "profit_cents": before["analysis"]["profit_cents"],
                    },
                    "after": {
                        "sold_price_cents": item["sold_price_cents"],
                        "sold_at": item["sold_at"],
                        "fee_percent": item["fee_percent"],
                        "profit_cents": item["analysis"]["profit_cents"],
                    },
                },
                occurred_at=now,
            )
        return item

    def undo_inventory_sale(
        self,
        user_id: int,
        item_id: int,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Undo a sale and return the position to its last sensible active state."""

        self._require_inventory_user_mutable(user_id, item_id)
        before = self.get_inventory_item(user_id, item_id, now=now)
        if before["status"] != "sold":
            raise ValidationError("only a sold item can have its sale undone")
        with self._connection() as conn:
            sold_event = conn.execute(
                """
                SELECT payload_json FROM account_activity
                WHERE user_id = ? AND inventory_item_id = ?
                    AND event_type = 'inventory_sold'
                ORDER BY id DESC LIMIT 1
                """,
                (int(user_id), int(item_id)),
            ).fetchone()
            transition_events = conn.execute(
                """
                SELECT payload_json FROM account_activity
                WHERE user_id = ? AND inventory_item_id = ?
                    AND event_type = 'inventory_updated'
                ORDER BY id DESC
                """,
                (int(user_id), int(item_id)),
            ).fetchall()
        sale_payload = (
            _json_loads(sold_event["payload_json"], {}) if sold_event else {}
        )
        # inventory_updated is committed atomically with the row transition,
        # while inventory_sold is appended just afterwards.  Prefer the newest
        # real active -> sold transition so an interrupted legacy write cannot
        # make us reuse previous_status from an older sale cycle.
        restore_status = ""
        for event in transition_events:
            transition = _json_loads(event["payload_json"], {})
            transition_before = transition.get("before") or {}
            transition_after = transition.get("after") or {}
            candidate = str(transition_before.get("status") or "").strip()
            if (
                candidate in {"owned", "listed"}
                and transition_after.get("status") == "sold"
            ):
                restore_status = candidate
                break
        if restore_status not in {"owned", "listed"}:
            restore_status = str(sale_payload.get("previous_status") or "").strip()
        if restore_status not in {"owned", "listed"}:
            # Final fallback for imported/legacy rows with no transition audit.
            restore_status = "listed" if before["listed_price_cents"] else "owned"
        if restore_status == "listed" and before["listed_price_cents"] is None:
            restore_status = "owned"
        item = self.update_inventory_item(
            user_id,
            item_id,
            expected_revision=(
                before["revision"] if expected_revision is None else expected_revision
            ),
            now=now,
            status=restore_status,
            sold_price_cents=None,
            sold_at=None,
        )
        with self._connection(write=True) as conn:
            _inventory_owned_row(conn, user_id=int(user_id), item_id=int(item_id))
            _record_activity(
                conn,
                user_id=int(user_id),
                inventory_item_id=int(item_id),
                event_type="inventory_sale_undone",
                payload={
                    "restored_status": restore_status,
                    "previous_sale": {
                        "sold_price_cents": before["sold_price_cents"],
                        "sold_at": before["sold_at"],
                        "fee_percent": before["fee_percent"],
                        "profit_cents": before["analysis"]["profit_cents"],
                    },
                },
                occurred_at=now,
            )
        return item

    def archive_inventory_item(
        self,
        user_id: int,
        item_id: int,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self._require_inventory_user_mutable(user_id, item_id)
        before = self.get_inventory_item(user_id, item_id, include_analysis=False)
        if before["status"] == "archived":
            return self.get_inventory_item(user_id, item_id, now=now)
        item = self.update_inventory_item(
            user_id,
            item_id,
            expected_revision=expected_revision,
            now=now,
            status="archived",
        )
        with self._connection(write=True) as conn:
            _record_activity(
                conn,
                user_id=int(user_id),
                inventory_item_id=int(item_id),
                event_type="inventory_archived",
                payload={"previous_status": before["status"]},
                occurred_at=now,
            )
        return item

    def delete_inventory_item(
        self,
        user_id: int,
        item_id: int,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Compatibility name: deletion is deliberately a recoverable archive."""

        return self.archive_inventory_item(
            user_id,
            item_id,
            expected_revision=expected_revision,
            now=now,
        )

    def restore_inventory_item(
        self,
        user_id: int,
        item_id: int,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self._require_inventory_user_mutable(user_id, item_id)
        before = self.get_inventory_item(user_id, item_id, include_analysis=False)
        if before["status"] != "archived":
            raise ValidationError("only an archived item can be restored")
        with self._connection() as conn:
            archived_event = conn.execute(
                """
                SELECT payload_json FROM account_activity
                WHERE user_id = ? AND inventory_item_id = ?
                    AND event_type = 'inventory_archived'
                ORDER BY id DESC LIMIT 1
                """,
                (int(user_id), int(item_id)),
            ).fetchone()
        archive_payload = (
            _json_loads(archived_event["payload_json"], {}) if archived_event else {}
        )
        restore_status = str(archive_payload.get("previous_status") or "owned")
        if restore_status not in {"owned", "listed", "sold"}:
            restore_status = "owned"
        item = self.update_inventory_item(
            user_id,
            item_id,
            expected_revision=expected_revision,
            now=now,
            status=restore_status,
        )
        with self._connection(write=True) as conn:
            _record_activity(
                conn,
                user_id=int(user_id),
                inventory_item_id=int(item_id),
                event_type="inventory_restored",
                payload={"restored_status": restore_status},
                occurred_at=now,
            )
        return item

    def inventory_summary(
        self,
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        items = self.list_inventory(
            user_id,
            status="all",
            limit=1_000,
            include_analysis=True,
            now=now,
        )
        counts = {status: 0 for status in INVENTORY_STATUSES}
        invested = market_value = unrealized = realized = stale_count = 0
        active_count = sold_count = 0
        for item in items:
            counts[item["status"]] += 1
            analysis = item["analysis"]
            if item["status"] in {"owned", "listed"}:
                active_count += item["quantity"]
                invested += analysis["total_cost_cents"]
                if analysis["net_value_cents"] is not None:
                    market_value += analysis["gross_value_cents"]
                    unrealized += analysis["profit_cents"]
                if analysis["is_stale"]:
                    stale_count += 1
            elif item["status"] == "sold":
                sold_count += item["quantity"]
                realized += analysis["profit_cents"] or 0
        return {
            "positions_count": len(items),
            "counts_by_status": counts,
            "active_count": active_count,
            "sold_count": sold_count,
            "invested_cents": invested,
            "market_value_cents": market_value,
            "unrealized_profit_cents": unrealized,
            "realized_profit_cents": realized,
            "stale_count": stale_count,
        }

    def admin_list_audit(
        self,
        *,
        actor_user_id: int | None = None,
        trusted_local_admin: bool = False,
        target_user_id: int | None = None,
        before_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return immutable account/access administration events only."""

        audit_events = (
            "account_created",
            "role_changed",
            "access_changed",
            "sessions_revoked_by_admin",
            "account_soft_deleted",
            "admin_password_reset_issued",
            "admin_password_reset_delivery",
        )
        clauses = [
            "event_type IN (" + ",".join("?" for _ in audit_events) + ")"
        ]
        params: list[Any] = list(audit_events)
        if target_user_id is not None:
            clauses.append("user_id = ?")
            params.append(self._normalise_user_id(target_user_id))
        if before_id is not None:
            clauses.append("id < ?")
            params.append(
                _bounded_int(
                    before_id, field="before_id", minimum=1, maximum=2**63 - 1
                )
            )
        page_limit = _bounded_int(limit, field="limit", minimum=1, maximum=500)
        params.append(page_limit)
        with self._connection() as conn:
            self._require_admin(
                conn,
                actor_user_id=actor_user_id,
                trusted_local_admin=trusted_local_admin,
            )
            rows = conn.execute(
                "SELECT id, user_id, event_type, occurred_at, payload_json "
                "FROM account_activity WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "target_user_id": int(row["user_id"]),
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "payload": _json_loads(row["payload_json"], {}),
            }
            for row in rows
        ]

    def admin_purge_presence_metadata(
        self,
        *,
        actor_user_id: int | None = None,
        trusted_local_admin: bool = False,
        older_than_days: int = 30,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Erase IP/country/device fields from old expired or revoked sessions."""

        days = _bounded_int(
            older_than_days, field="older_than_days", minimum=1, maximum=3650
        )
        timestamp = _utc_now(now)
        cutoff = _iso(timestamp - timedelta(days=days))
        with self._connection(write=True) as conn:
            self._require_admin(
                conn,
                actor_user_id=actor_user_id,
                trusted_local_admin=trusted_local_admin,
            )
            count = int(
                conn.execute(
                    """
                    UPDATE account_sessions
                    SET ip_address = NULL, country_code = NULL, user_agent = NULL
                    WHERE last_seen_at < ?
                      AND (revoked_at IS NOT NULL OR expires_at <= ?)
                      AND (ip_address IS NOT NULL OR country_code IS NOT NULL
                           OR user_agent IS NOT NULL)
                    """,
                    (cutoff, _iso(timestamp)),
                ).rowcount
            )
        return {"purged_sessions": count}

    def list_activity(
        self,
        user_id: int,
        *,
        inventory_item_id: int | None = None,
        before_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        user_id = self._normalise_user_id(user_id)
        limit = _bounded_int(limit, field="limit", minimum=1, maximum=500)
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if inventory_item_id is not None:
            item_id = _bounded_int(
                inventory_item_id,
                field="inventory_item_id",
                minimum=1,
                maximum=2**63 - 1,
            )
            clauses.append("inventory_item_id = ?")
            params.append(item_id)
        if before_id is not None:
            cursor_id = _bounded_int(
                before_id, field="before_id", minimum=1, maximum=2**63 - 1
            )
            clauses.append("id < ?")
            params.append(cursor_id)
        params.append(limit)
        with self._connection() as conn:
            user = conn.execute(
                "SELECT 1 FROM account_users WHERE id = ?", (user_id,)
            ).fetchone()
            if user is None:
                raise NotFoundError("user not found")
            rows = conn.execute(
                "SELECT * FROM account_activity WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "user_id": int(row["user_id"]),
                "inventory_item_id": row["inventory_item_id"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "payload": _json_loads(row["payload_json"], {}),
            }
            for row in rows
        ]
