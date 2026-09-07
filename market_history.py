"""Isolated, provenance-first storage for multi-market CS2 price history.

This module intentionally does not read or mutate the legacy ``sales`` tables.
Its default database is ``.private/market_history.db`` and databases containing
the legacy ``sales`` table are rejected.  Consequently the existing model
training pipeline cannot silently consume these rows before a separate,
reviewed data contract is introduced.

Supported upstream contracts:

* CSPriceAPI Enterprise ``/v1/market-sales/export`` ZIP (three CSV files).
* CSPriceAPI Basic BUFF163 and YouPin bulk listing-price snapshots.
* ECB historical euro reference rates for auditable USD normalization.

Money is never stored as a binary floating-point value.  Native prices use an
integer plus an explicit decimal scale; USD is an optional derived value linked
to the exact FX-rate row used for conversion.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

import app_config

SCHEMA_VERSION = 1
PROVIDER_CSPRICEAPI = "cspriceapi"
ECB_PROVIDER_CODE = "ecb"
ECB_RATE_SOURCE = "ecb_reference"
ECB_HISTORY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
CSPRICEAPI_EXPORT_URL = "https://api.cspriceapi.com/v1/market-sales/export"
CSPRICEAPI_SNAPSHOT_URLS = {
    "buff163": "https://api.cspriceapi.com/v1/prices/buff163",
    "youpin": "https://api.cspriceapi.com/v1/prices/youpin",
}
ALLOWED_HTTP_URLS = frozenset(
    {ECB_HISTORY_URL, CSPRICEAPI_EXPORT_URL, *CSPRICEAPI_SNAPSHOT_URLS.values()}
)

MARKET_CURRENCIES = {
    "csfloat": "USD",
    "youpin": "CNY",
    "skinport": "EUR",
    "buff163": "CNY",
}
SALE_MARKETPLACES = frozenset({"csfloat", "youpin", "skinport"})
SNAPSHOT_MARKETPLACES = frozenset(CSPRICEAPI_SNAPSHOT_URLS)
REQUIRED_ZIP_MEMBERS = frozenset({"sales.csv", "stickers.csv", "keychains.csv"})
SCHEMA_TABLES = frozenset(
    {
        "market_history_meta",
        "source_datasets",
        "ingest_runs",
        "ingest_quarantine",
        "fx_rates",
        "market_sale_events",
        "market_sale_stickers",
        "market_sale_keychains",
        "price_snapshots",
        "sqlite_sequence",
    }
)

MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_ECB_BYTES = 16 * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 384 * 1024 * 1024
MAX_COMPRESSION_RATIO = 250
MAX_CSV_FIELD_BYTES = 64 * 1024
MAX_ROWS = {
    "sales.csv": 500_000,
    "stickers.csv": 2_000_000,
    "keychains.csv": 500_000,
}

UINT64_MAX = (1 << 64) - 1
MAX_NATIVE_MINOR = 10**15
CODE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
UNSIGNED_INTEGER_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
DECIMAL_PRICE_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.([0-9]{1,6}))?$")

SALES_HEADER = (
    "marketplace",
    "sale_id",
    "market_hash_name",
    "price",
    "sold_at",
    "def_index",
    "paint_index",
    "paint_seed",
    "float_value",
    "is_stattrak",
    "is_souvenir",
    "rarity",
    "quality",
    "cs2_img",
    "fetched_at",
)
STICKERS_HEADER = (
    "marketplace",
    "sale_id",
    "sold_at",
    "position_idx",
    "sticker_id",
    "slot",
    "sticker_name",
    "wear",
    "offset_x",
    "offset_y",
    "rotation",
    "reference_price",
    "reference_quantity",
    "reference_updated",
    "fetched_at",
)
KEYCHAINS_HEADER = (
    "marketplace",
    "sale_id",
    "sold_at",
    "position_idx",
    "keychain_id",
    "slot",
    "keychain_name",
    "pattern",
    "offset_x",
    "offset_y",
    "offset_z",
    "reference_price",
    "reference_quantity",
    "reference_updated",
    "fetched_at",
)
EXPECTED_HEADERS = {
    "sales.csv": SALES_HEADER,
    "stickers.csv": STICKERS_HEADER,
    "keychains.csv": KEYCHAINS_HEADER,
}


class MarketHistoryError(RuntimeError):
    """Base class for safe, user-displayable ingestion errors."""


class UnsafeDatabaseError(MarketHistoryError):
    """The configured database could mix new data with legacy/model data."""


class ValidationError(MarketHistoryError):
    """An upstream payload violates the pinned data contract."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        source_file: str | None = None,
        row_number: int | None = None,
        payload_sha256: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.source_file = source_file
        self.row_number = row_number
        self.payload_sha256 = payload_sha256


class DownloadError(MarketHistoryError):
    """A bounded HTTP request failed without exposing credentials."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class FxRate:
    id: int
    currency: str
    rate_date: str
    rate_to_usd: Decimal
    source_code: str


@dataclass(frozen=True)
class SalesExportContract:
    """Out-of-band provider confirmation missing from the public OpenAPI file."""

    version: str
    marketplace_currencies: Mapping[str, str]
    naive_timestamps_are_utc: bool

    @classmethod
    def confirmed_cspriceapi(cls, version: str) -> SalesExportContract:
        confirmed_version = _required_text(
            version, "sales export contract version", maximum=100
        )
        return cls(
            version=confirmed_version,
            marketplace_currencies={
                "csfloat": "USD",
                "youpin": "CNY",
                "skinport": "EUR",
            },
            naive_timestamps_are_utc=True,
        )


@dataclass(frozen=True)
class ImportResult:
    run_id: str
    status: str
    source_rows: int
    inserted_rows: int
    duplicate_rows: int
    attachment_rows: int = 0
    payload_sha256: str = ""
    reused_run: bool = False


@dataclass(frozen=True)
class DownloadedPayload:
    body: bytes
    status_code: int
    headers: Mapping[str, str]


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def default_database_path() -> Path:
    return app_config.path("CS2_MARKET_HISTORY_DB_PATH", ".private/market_history.db")


def _known_legacy_path() -> Path:
    return app_config.path("CS2_DB_PATH", "csfloat_history.db")


def _resolve_without_requirement(path: Path) -> Path:
    return path.expanduser().absolute().resolve(strict=False)


def _validate_code(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if not CODE_RE.fullmatch(normalized):
        raise ValidationError("invalid_code", f"invalid {field}")
    return normalized


def _ensure_private_file(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise UnsafeDatabaseError(
            "cannot enforce 0600 permissions on history DB"
        ) from exc


def _assert_isolated_path(path: Path) -> None:
    if path.is_symlink():
        raise UnsafeDatabaseError("market history DB path must not be a symlink")
    resolved = _resolve_without_requirement(path)
    if resolved == _resolve_without_requirement(_known_legacy_path()):
        raise UnsafeDatabaseError(
            "market history DB must be separate from CS2_DB_PATH/legacy sales"
        )


def _secure_sqlite_files(database: Path) -> None:
    for candidate in (
        database,
        database.with_name(f"{database.name}-wal"),
        database.with_name(f"{database.name}-shm"),
    ):
        if not candidate.exists():
            continue
        if candidate.is_symlink() or not candidate.is_file():
            raise UnsafeDatabaseError("unsafe SQLite database sidecar")
        _ensure_private_file(candidate)


def connect_database(path: str | Path | None = None) -> sqlite3.Connection:
    """Open an isolated market-history database and initialize its schema."""

    configured_default = default_database_path()
    database = Path(path) if path is not None else configured_default
    _assert_isolated_path(database)
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    is_default_location = _resolve_without_requirement(
        database
    ) == _resolve_without_requirement(configured_default)
    if is_default_location:
        try:
            database.parent.chmod(0o700)
        except OSError as exc:
            raise UnsafeDatabaseError(
                "cannot enforce 0700 permissions on private history directory"
            ) from exc
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "sales" in existing:
            raise UnsafeDatabaseError("refusing database containing legacy sales table")
        unknown = existing - SCHEMA_TABLES
        if unknown:
            raise UnsafeDatabaseError("refusing database containing unrelated tables")
        if existing and "market_history_meta" not in existing:
            raise UnsafeDatabaseError(
                "existing database is not an initialized market-history store"
            )
        if "market_history_meta" in existing:
            purpose = connection.execute(
                "SELECT value FROM market_history_meta WHERE key='database_purpose'"
            ).fetchone()
            version = connection.execute(
                "SELECT value FROM market_history_meta WHERE key='schema_version'"
            ).fetchone()
            if purpose is None or purpose[0] != "isolated_multi_market_history":
                raise UnsafeDatabaseError("market history database purpose mismatch")
            if version is None or str(version[0]) != str(SCHEMA_VERSION):
                raise UnsafeDatabaseError("unsupported market history schema version")
        _ensure_private_file(database)
        journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        if journal_mode.lower() != "wal":
            raise UnsafeDatabaseError("market history DB requires WAL journal mode")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA wal_autocheckpoint=1000")
        initialize_schema(connection)
        connection.commit()
        _secure_sqlite_files(database)
        return connection
    except Exception:
        connection.close()
        raise


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create versioned tables without exposing them to legacy model code."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS market_history_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS source_datasets (
            dataset_code TEXT PRIMARY KEY,
            marketplace_code TEXT NOT NULL,
            provider_code TEXT NOT NULL,
            storage_ref TEXT NOT NULL,
            native_currency TEXT NOT NULL,
            price_field TEXT NOT NULL,
            model_training_eligible INTEGER NOT NULL DEFAULT 0
                CHECK (model_training_eligible IN (0, 1)),
            notes TEXT NOT NULL,
            registered_at TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS ingest_runs (
            run_id TEXT PRIMARY KEY,
            provider_code TEXT NOT NULL,
            ingest_kind TEXT NOT NULL,
            marketplace_code TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('started', 'complete', 'quarantined', 'failed', 'skipped')
            ),
            payload_sha256 TEXT NOT NULL,
            source_uri TEXT,
            source_filename TEXT,
            contract_version TEXT,
            currency_contract_status TEXT,
            timestamp_assumption TEXT,
            window_start TEXT,
            window_end TEXT,
            provider_observed_at TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            source_rows INTEGER NOT NULL DEFAULT 0 CHECK (source_rows >= 0),
            inserted_rows INTEGER NOT NULL DEFAULT 0 CHECK (inserted_rows >= 0),
            duplicate_rows INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_rows >= 0),
            sticker_rows INTEGER NOT NULL DEFAULT 0 CHECK (sticker_rows >= 0),
            keychain_rows INTEGER NOT NULL DEFAULT 0 CHECK (keychain_rows >= 0),
            rejected_rows INTEGER NOT NULL DEFAULT 0 CHECK (rejected_rows >= 0),
            error_code TEXT,
            error_message TEXT,
            UNIQUE(provider_code, ingest_kind, marketplace_code, payload_sha256)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS ingest_quarantine (
            quarantine_id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES ingest_runs(run_id) ON DELETE CASCADE,
            source_file TEXT,
            row_number INTEGER,
            error_code TEXT NOT NULL,
            error_message TEXT NOT NULL,
            row_payload_sha256 TEXT,
            created_at TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS fx_rates (
            fx_rate_id INTEGER PRIMARY KEY,
            provider_code TEXT NOT NULL,
            source_code TEXT NOT NULL,
            base_currency TEXT NOT NULL,
            quote_currency TEXT NOT NULL CHECK (quote_currency = 'USD'),
            rate_date TEXT NOT NULL,
            rate_to_usd TEXT NOT NULL,
            source_uri TEXT NOT NULL,
            provider_observed_at TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            UNIQUE(provider_code, source_code, base_currency, quote_currency, rate_date)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS market_sale_events (
            event_id INTEGER PRIMARY KEY,
            provider_code TEXT NOT NULL,
            marketplace_code TEXT NOT NULL,
            provider_sale_id TEXT NOT NULL,
            event_kind TEXT NOT NULL CHECK (event_kind = 'sale'),
            granularity TEXT NOT NULL CHECK (granularity = 'event'),
            market_hash_name TEXT NOT NULL,
            native_price_minor INTEGER NOT NULL CHECK (native_price_minor > 0),
            currency TEXT NOT NULL,
            currency_verified INTEGER NOT NULL DEFAULT 0
                CHECK (currency_verified IN (0, 1)),
            minor_scale INTEGER NOT NULL CHECK (minor_scale BETWEEN 0 AND 6),
            price_usd_micros INTEGER,
            price_usd_cents INTEGER,
            fx_rate_id INTEGER REFERENCES fx_rates(fx_rate_id),
            fx_rate_to_usd TEXT,
            fx_rate_date TEXT,
            fx_rate_source TEXT,
            source_event_at TEXT NOT NULL,
            source_event_at_raw TEXT NOT NULL,
            timestamp_assumption TEXT NOT NULL,
            provider_observed_at TEXT NOT NULL,
            provider_observed_at_raw TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            def_index INTEGER,
            paint_index INTEGER,
            paint_seed INTEGER,
            float_value TEXT,
            is_stattrak INTEGER NOT NULL CHECK (is_stattrak IN (0, 1)),
            is_souvenir INTEGER NOT NULL CHECK (is_souvenir IN (0, 1)),
            rarity INTEGER,
            quality INTEGER,
            image_url TEXT,
            payload_sha256 TEXT NOT NULL,
            archive_sha256 TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_row_number INTEGER NOT NULL,
            ingest_run_id TEXT NOT NULL REFERENCES ingest_runs(run_id),
            UNIQUE(
                provider_code, marketplace_code, provider_sale_id,
                source_event_at, event_kind, granularity
            )
        ) STRICT;

        CREATE TABLE IF NOT EXISTS market_sale_stickers (
            sale_sticker_id INTEGER PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES market_sale_events(event_id) ON DELETE CASCADE,
            position_idx INTEGER NOT NULL,
            provider_sticker_id TEXT NOT NULL,
            slot INTEGER,
            sticker_name TEXT NOT NULL,
            wear TEXT,
            offset_x TEXT,
            offset_y TEXT,
            rotation TEXT,
            native_reference_price_minor INTEGER,
            currency TEXT NOT NULL,
            currency_verified INTEGER NOT NULL DEFAULT 0
                CHECK (currency_verified IN (0, 1)),
            minor_scale INTEGER NOT NULL CHECK (minor_scale BETWEEN 0 AND 6),
            reference_price_usd_micros INTEGER,
            reference_price_usd_cents INTEGER,
            fx_rate_id INTEGER REFERENCES fx_rates(fx_rate_id),
            fx_rate_to_usd TEXT,
            fx_rate_date TEXT,
            fx_rate_source TEXT,
            reference_quantity INTEGER,
            reference_updated_at TEXT,
            provider_observed_at TEXT NOT NULL,
            provider_observed_at_raw TEXT NOT NULL,
            timestamp_assumption TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            archive_sha256 TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_row_number INTEGER NOT NULL,
            ingest_run_id TEXT NOT NULL REFERENCES ingest_runs(run_id),
            UNIQUE(event_id, position_idx)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS market_sale_keychains (
            sale_keychain_id INTEGER PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES market_sale_events(event_id) ON DELETE CASCADE,
            position_idx INTEGER NOT NULL,
            provider_keychain_id TEXT NOT NULL,
            slot INTEGER,
            keychain_name TEXT NOT NULL,
            pattern INTEGER,
            offset_x TEXT,
            offset_y TEXT,
            offset_z TEXT,
            native_reference_price_minor INTEGER,
            currency TEXT NOT NULL,
            currency_verified INTEGER NOT NULL DEFAULT 0
                CHECK (currency_verified IN (0, 1)),
            minor_scale INTEGER NOT NULL CHECK (minor_scale BETWEEN 0 AND 6),
            reference_price_usd_micros INTEGER,
            reference_price_usd_cents INTEGER,
            fx_rate_id INTEGER REFERENCES fx_rates(fx_rate_id),
            fx_rate_to_usd TEXT,
            fx_rate_date TEXT,
            fx_rate_source TEXT,
            reference_quantity INTEGER,
            reference_updated_at TEXT,
            provider_observed_at TEXT NOT NULL,
            provider_observed_at_raw TEXT NOT NULL,
            timestamp_assumption TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            archive_sha256 TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_row_number INTEGER NOT NULL,
            ingest_run_id TEXT NOT NULL REFERENCES ingest_runs(run_id),
            UNIQUE(event_id, position_idx)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS price_snapshots (
            snapshot_id INTEGER PRIMARY KEY,
            provider_code TEXT NOT NULL,
            marketplace_code TEXT NOT NULL,
            provider_item_id TEXT,
            event_kind TEXT NOT NULL CHECK (event_kind = 'listing_snapshot'),
            granularity TEXT NOT NULL CHECK (granularity = 'point'),
            market_hash_name TEXT NOT NULL,
            native_price_minor INTEGER NOT NULL CHECK (native_price_minor > 0),
            currency TEXT NOT NULL,
            currency_verified INTEGER NOT NULL DEFAULT 1
                CHECK (currency_verified IN (0, 1)),
            minor_scale INTEGER NOT NULL CHECK (minor_scale BETWEEN 0 AND 6),
            price_usd_micros INTEGER,
            price_usd_cents INTEGER,
            fx_rate_id INTEGER REFERENCES fx_rates(fx_rate_id),
            fx_rate_to_usd TEXT,
            fx_rate_date TEXT,
            fx_rate_source TEXT,
            listing_count INTEGER,
            liquidity TEXT,
            source_event_at TEXT NOT NULL,
            provider_observed_at TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            provider_source_name TEXT,
            payload_sha256 TEXT NOT NULL,
            response_sha256 TEXT NOT NULL,
            source_uri TEXT NOT NULL,
            source_row_number INTEGER NOT NULL,
            ingest_run_id TEXT NOT NULL REFERENCES ingest_runs(run_id),
            UNIQUE(
                provider_code, marketplace_code, market_hash_name,
                source_event_at, event_kind, granularity
            )
        ) STRICT;

        CREATE INDEX IF NOT EXISTS market_sale_events_item_time
            ON market_sale_events(market_hash_name, source_event_at DESC);
        CREATE INDEX IF NOT EXISTS market_sale_events_market_time
            ON market_sale_events(marketplace_code, source_event_at DESC);
        CREATE INDEX IF NOT EXISTS market_sale_stickers_name
            ON market_sale_stickers(sticker_name, event_id);
        CREATE INDEX IF NOT EXISTS market_sale_keychains_name
            ON market_sale_keychains(keychain_name, event_id);
        CREATE INDEX IF NOT EXISTS price_snapshots_item_time
            ON price_snapshots(market_hash_name, source_event_at DESC);
        CREATE INDEX IF NOT EXISTS price_snapshots_market_time
            ON price_snapshots(marketplace_code, source_event_at DESC);
        CREATE INDEX IF NOT EXISTS fx_rates_lookup
            ON fx_rates(base_currency, quote_currency, rate_date DESC);
        """
    )
    now = utc_now()
    metadata = {
        "schema_version": str(SCHEMA_VERSION),
        "database_purpose": "isolated_multi_market_history",
        "model_training_eligible": "false",
        "legacy_sales_imported": "false",
        "money_contract": "integer_native_minor_plus_scale",
    }
    for key, value in metadata.items():
        connection.execute(
            """
            INSERT INTO market_history_meta(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, now),
        )
    datasets = (
        (
            "legacy_csfloat_sales",
            "csfloat",
            "csfloat_direct",
            "csfloat_history.db:sales",
            "USD",
            "price_cents",
            0,
            (
                "External registry only; legacy database is not attached or modified. "
                "Excluded from this multi-market data contract and model input."
            ),
        ),
        (
            "cspriceapi_market_sales",
            "multi",
            PROVIDER_CSPRICEAPI,
            "market_history.db:market_sale_events",
            "MULTI",
            "native_price_minor/minor_scale",
            0,
            "Validated Enterprise sale events with per-row marketplace provenance.",
        ),
        (
            "cspriceapi_listing_snapshots",
            "multi",
            PROVIDER_CSPRICEAPI,
            "market_history.db:price_snapshots",
            "MULTI",
            "native_price_minor/minor_scale",
            0,
            "Point-in-time listings; these rows are not confirmed sales.",
        ),
    )
    for dataset in datasets:
        connection.execute(
            """
            INSERT INTO source_datasets(
                dataset_code, marketplace_code, provider_code, storage_ref,
                native_currency, price_field, model_training_eligible, notes,
                registered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_code) DO UPDATE SET
                marketplace_code=excluded.marketplace_code,
                provider_code=excluded.provider_code,
                storage_ref=excluded.storage_ref,
                native_currency=excluded.native_currency,
                price_field=excluded.price_field,
                model_training_eligible=excluded.model_training_eligible,
                notes=excluded.notes
            """,
            (*dataset, now),
        )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_row_sha256(row: Mapping[str, str]) -> str:
    encoded = json.dumps(
        dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256(encoded)


def _safe_error_message(message: str, *, limit: int = 500) -> str:
    compact = " ".join(str(message).split())
    return compact[:limit] or "payload validation failed"


def _normalize_timestamp(raw: str, field: str) -> str:
    value = raw.strip()
    if not value or len(value) > 40:
        raise ValidationError("invalid_timestamp", f"invalid {field}")
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValidationError("invalid_timestamp", f"invalid {field}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _export_timestamp(
    raw: str,
    field: str,
    contract: SalesExportContract | None,
) -> tuple[str, str, str]:
    """Normalize an export timestamp while preserving its unverified assumption."""

    source_raw = raw.strip()
    has_explicit_zone = bool(
        source_raw.endswith(("Z", "z"))
        or re.search(r"[+-][0-9]{2}:[0-9]{2}$", source_raw)
    )
    if has_explicit_zone:
        assumption = "explicit_source_timezone"
    elif contract is not None and contract.naive_timestamps_are_utc:
        assumption = f"UTC_verified:{contract.version}"
    else:
        assumption = "UTC_unverified"
    return _normalize_timestamp(source_raw, field), source_raw, assumption


def _export_currency(
    marketplace: str,
    contract: SalesExportContract | None,
) -> tuple[str, bool]:
    if contract is None:
        return "UNK", False
    currency = str(contract.marketplace_currencies.get(marketplace, "")).upper()
    if currency not in {"USD", "CNY", "EUR"}:
        raise ValidationError(
            "invalid_sales_currency_contract",
            "confirmed sales currency contract is incomplete",
        )
    return currency, True


def _parse_uint(
    raw: str,
    field: str,
    *,
    optional: bool = False,
    maximum: int = UINT64_MAX,
) -> int | None:
    value = raw.strip()
    if optional and not value:
        return None
    if not UNSIGNED_INTEGER_RE.fullmatch(value):
        raise ValidationError("invalid_integer", f"invalid {field}")
    parsed = int(value)
    if parsed > maximum:
        raise ValidationError("integer_out_of_range", f"{field} is out of range")
    return parsed


def _parse_flag(raw: str, field: str) -> int:
    value = _parse_uint(raw, field, maximum=1)
    assert value is not None
    return value


def _parse_decimal_text(
    raw: str,
    field: str,
    *,
    optional: bool = False,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> str | None:
    value = raw.strip()
    if optional and not value:
        return None
    if not value or len(value) > 64:
        raise ValidationError("invalid_decimal", f"invalid {field}")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError("invalid_decimal", f"invalid {field}") from exc
    if not parsed.is_finite():
        raise ValidationError("invalid_decimal", f"invalid {field}")
    if minimum is not None and parsed < minimum:
        raise ValidationError("decimal_out_of_range", f"{field} is out of range")
    if maximum is not None and parsed > maximum:
        raise ValidationError("decimal_out_of_range", f"{field} is out of range")
    return format(parsed, "f")


def _required_text(raw: str, field: str, *, maximum: int = 512) -> str:
    value = raw.strip()
    if not value or len(value) > maximum or "\x00" in value:
        raise ValidationError("invalid_text", f"invalid {field}")
    return value


def _optional_https_url(raw: str, field: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if len(value) > 2048 or not value.startswith("https://") or "\x00" in value:
        raise ValidationError("invalid_url", f"invalid {field}")
    return value


def _parse_native_decimal(raw: str, field: str) -> tuple[int, int]:
    """Convert a provider decimal string to an exact integer and scale."""

    value = raw.strip()
    match = DECIMAL_PRICE_RE.fullmatch(value)
    if not match:
        raise ValidationError("invalid_price", f"invalid {field}")
    fraction = match.group(1) or ""
    scale = len(fraction)
    minor = int(value.replace(".", ""))
    if minor <= 0:
        raise ValidationError("invalid_price", f"{field} must be greater than zero")
    if minor > MAX_NATIVE_MINOR:
        raise ValidationError("price_out_of_range", f"{field} is out of range")
    return minor, scale


def _money_usd(
    native_minor: int,
    minor_scale: int,
    fx_rate: FxRate | None,
) -> tuple[int | None, int | None]:
    if fx_rate is None:
        return None, None
    with localcontext() as context:
        context.prec = 50
        native = Decimal(native_minor) / (Decimal(10) ** minor_scale)
        usd = native * fx_rate.rate_to_usd
        micros = int(
            (usd * Decimal(1_000_000)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        )
        cents = int((usd * Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return micros, cents


def _fx_columns(fx_rate: FxRate | None) -> tuple[Any, Any, Any, Any]:
    if fx_rate is None:
        return None, None, None, None
    return (
        fx_rate.id,
        format(fx_rate.rate_to_usd, "f"),
        fx_rate.rate_date,
        fx_rate.source_code,
    )


def resolve_fx_rate(
    connection: sqlite3.Connection,
    currency: str,
    on_date: str | date,
) -> FxRate | None:
    """Resolve the most recent official rate on or before a source date."""

    normalized_currency = currency.upper()
    target_date = on_date.isoformat() if isinstance(on_date, date) else str(on_date)
    row = connection.execute(
        """
        SELECT fx_rate_id, base_currency, rate_date, rate_to_usd, source_code
          FROM fx_rates
         WHERE base_currency=? AND quote_currency='USD' AND rate_date<=?
         ORDER BY rate_date DESC
         LIMIT 1
        """,
        (normalized_currency, target_date),
    ).fetchone()
    if row is None:
        return None
    return FxRate(
        id=int(row["fx_rate_id"]),
        currency=str(row["base_currency"]),
        rate_date=str(row["rate_date"]),
        rate_to_usd=Decimal(str(row["rate_to_usd"])),
        source_code=str(row["source_code"]),
    )


def import_ecb_reference_rates(
    connection: sqlite3.Connection,
    payload: bytes,
    *,
    observed_at: str | None = None,
) -> int:
    """Validate and upsert EUR/CNY/USD -> USD cross-rates from ECB XML."""

    if not payload or len(payload) > MAX_ECB_BYTES:
        raise ValidationError("invalid_ecb_size", "ECB payload size is invalid")
    payload_hash = _sha256(payload)
    try:
        upper_payload = payload.upper()
        if b"<!DOCTYPE" in upper_payload or b"<!ENTITY" in upper_payload:
            raise ValidationError(
                "unsafe_ecb_xml", "ECB XML document declarations are not allowed"
            )
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ValidationError("invalid_ecb_xml", "ECB XML is malformed") from exc
    imported_at = utc_now()
    provider_observed_at = _normalize_timestamp(
        observed_at or imported_at, "ECB observed_at"
    )
    daily: dict[str, dict[str, Decimal]] = {}
    for element in root.iter():
        raw_day = element.attrib.get("time")
        if raw_day is None:
            continue
        try:
            parsed_day = date.fromisoformat(raw_day)
        except ValueError as exc:
            raise ValidationError(
                "invalid_ecb_date", "ECB rate date is invalid"
            ) from exc
        values: dict[str, Decimal] = {}
        for child in list(element):
            currency = child.attrib.get("currency", "").upper()
            raw_rate = child.attrib.get("rate", "")
            if currency not in {"USD", "CNY"}:
                continue
            try:
                rate = Decimal(raw_rate)
            except InvalidOperation as exc:
                raise ValidationError(
                    "invalid_ecb_rate", "ECB rate is invalid"
                ) from exc
            if not rate.is_finite() or rate <= 0:
                raise ValidationError("invalid_ecb_rate", "ECB rate is invalid")
            if currency in values:
                raise ValidationError(
                    "duplicate_ecb_rate", "duplicate ECB currency/date"
                )
            values[currency] = rate
        if values:
            if parsed_day.isoformat() in daily:
                raise ValidationError("duplicate_ecb_date", "duplicate ECB rate date")
            daily[parsed_day.isoformat()] = values
    if not daily:
        raise ValidationError("empty_ecb_rates", "ECB XML contains no daily rates")

    saved = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        for rate_date, values in sorted(daily.items()):
            eur_to_usd = values.get("USD")
            if eur_to_usd is None:
                continue
            rates = {"USD": Decimal(1), "EUR": eur_to_usd}
            cny_per_eur = values.get("CNY")
            if cny_per_eur is not None:
                with localcontext() as context:
                    context.prec = 50
                    rates["CNY"] = eur_to_usd / cny_per_eur
            for currency, rate_to_usd in rates.items():
                cursor = connection.execute(
                    """
                    INSERT INTO fx_rates(
                        provider_code, source_code, base_currency, quote_currency,
                        rate_date, rate_to_usd, source_uri, provider_observed_at,
                        imported_at, payload_sha256
                    ) VALUES (?, ?, ?, 'USD', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        provider_code, source_code, base_currency,
                        quote_currency, rate_date
                    ) DO UPDATE SET
                        rate_to_usd=excluded.rate_to_usd,
                        source_uri=excluded.source_uri,
                        provider_observed_at=excluded.provider_observed_at,
                        imported_at=excluded.imported_at,
                        payload_sha256=excluded.payload_sha256
                    """,
                    (
                        ECB_PROVIDER_CODE,
                        ECB_RATE_SOURCE,
                        currency,
                        rate_date,
                        format(rate_to_usd, "f"),
                        ECB_HISTORY_URL,
                        provider_observed_at,
                        imported_at,
                        payload_hash,
                    ),
                )
                saved += max(cursor.rowcount, 0)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    if not connection.execute(
        "SELECT 1 FROM fx_rates WHERE source_code=? LIMIT 1", (ECB_RATE_SOURCE,)
    ).fetchone():
        raise ValidationError("empty_ecb_rates", "ECB XML has no usable USD rates")
    return saved


def _existing_run(
    connection: sqlite3.Connection,
    *,
    provider_code: str,
    ingest_kind: str,
    marketplace_code: str | None,
    payload_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM ingest_runs
         WHERE provider_code=? AND ingest_kind=?
           AND marketplace_code IS ? AND payload_sha256=?
        """,
        (provider_code, ingest_kind, marketplace_code, payload_sha256),
    ).fetchone()


def _result_from_run(row: sqlite3.Row, *, reused: bool) -> ImportResult:
    return ImportResult(
        run_id=str(row["run_id"]),
        status=str(row["status"]),
        source_rows=int(row["source_rows"]),
        inserted_rows=int(row["inserted_rows"]),
        duplicate_rows=int(row["duplicate_rows"]),
        attachment_rows=int(row["sticker_rows"]) + int(row["keychain_rows"]),
        payload_sha256=str(row["payload_sha256"]),
        reused_run=reused,
    )


def _start_run(
    connection: sqlite3.Connection,
    *,
    provider_code: str,
    ingest_kind: str,
    marketplace_code: str | None,
    payload_sha256: str,
    source_uri: str | None,
    source_filename: str | None,
    provider_observed_at: str | None = None,
    contract_version: str | None = None,
    currency_contract_status: str | None = None,
    timestamp_assumption: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> tuple[str, ImportResult | None]:
    existing = _existing_run(
        connection,
        provider_code=provider_code,
        ingest_kind=ingest_kind,
        marketplace_code=marketplace_code,
        payload_sha256=payload_sha256,
    )
    if existing is not None:
        if existing["status"] == "complete":
            return str(existing["run_id"]), _result_from_run(existing, reused=True)
        if existing["status"] == "quarantined":
            raise ValidationError(
                str(existing["error_code"] or "quarantined_payload"),
                str(existing["error_message"] or "payload was quarantined previously"),
            )
        if existing["status"] == "started":
            connection.execute(
                """
                UPDATE ingest_runs
                   SET status='failed', completed_at=?, error_code='interrupted_run',
                       error_message='previous ingestion did not complete'
                 WHERE run_id=?
                """,
                (utc_now(), existing["run_id"]),
            )
            connection.commit()
        else:
            raise MarketHistoryError("payload has a previous failed ingestion run")
    run_id = uuid.uuid4().hex
    connection.execute(
        """
        INSERT INTO ingest_runs(
            run_id, provider_code, ingest_kind, marketplace_code, status,
            payload_sha256, source_uri, source_filename, window_start, window_end,
            provider_observed_at, contract_version, currency_contract_status,
            timestamp_assumption, started_at
        ) VALUES (?, ?, ?, ?, 'started', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            provider_code,
            ingest_kind,
            marketplace_code,
            payload_sha256,
            source_uri,
            source_filename,
            window_start,
            window_end,
            provider_observed_at,
            contract_version,
            currency_contract_status,
            timestamp_assumption,
            utc_now(),
        ),
    )
    connection.commit()
    return run_id, None


def _quarantine_run(
    connection: sqlite3.Connection,
    run_id: str,
    error: ValidationError,
) -> None:
    message = _safe_error_message(str(error))
    connection.execute(
        """
        UPDATE ingest_runs
           SET status='quarantined', completed_at=?, rejected_rows=1,
               error_code=?, error_message=?
         WHERE run_id=?
        """,
        (utc_now(), error.code, message, run_id),
    )
    connection.execute(
        """
        INSERT INTO ingest_quarantine(
            run_id, source_file, row_number, error_code, error_message,
            row_payload_sha256, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            error.source_file,
            error.row_number,
            error.code,
            message,
            error.payload_sha256,
            utc_now(),
        ),
    )
    connection.commit()


def _fail_run(connection: sqlite3.Connection, run_id: str, error: Exception) -> None:
    connection.execute(
        """
        UPDATE ingest_runs
           SET status='failed', completed_at=?, error_code='internal_error',
               error_message=?
         WHERE run_id=?
        """,
        (utc_now(), _safe_error_message(type(error).__name__), run_id),
    )
    connection.commit()


def _validate_zip(blob: bytes) -> zipfile.ZipFile:
    if not blob or len(blob) > MAX_DOWNLOAD_BYTES:
        raise ValidationError("invalid_zip_size", "ZIP payload size is invalid")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValidationError(
            "invalid_zip", "payload is not a valid ZIP archive"
        ) from exc
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        archive.close()
        raise ValidationError("duplicate_zip_member", "ZIP contains duplicate members")
    if set(names) != REQUIRED_ZIP_MEMBERS:
        archive.close()
        raise ValidationError(
            "invalid_zip_members", "ZIP member list does not match contract"
        )
    total_uncompressed = 0
    for info in infos:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or len(path.parts) != 1 or ".." in path.parts:
            archive.close()
            raise ValidationError("unsafe_zip_path", "ZIP contains an unsafe path")
        if info.is_dir() or info.flag_bits & 0x1:
            archive.close()
            raise ValidationError(
                "unsafe_zip_member", "ZIP member is encrypted or invalid"
            )
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if unix_mode and stat.S_ISLNK(unix_mode):
            archive.close()
            raise ValidationError("unsafe_zip_member", "ZIP symlinks are not allowed")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            archive.close()
            raise ValidationError(
                "unsupported_zip_compression", "unsupported ZIP compression"
            )
        if info.file_size > MAX_ZIP_MEMBER_BYTES:
            archive.close()
            raise ValidationError("zip_member_too_large", "ZIP member is too large")
        total_uncompressed += info.file_size
        if info.file_size and info.compress_size == 0:
            archive.close()
            raise ValidationError("zip_bomb", "invalid ZIP compression ratio")
        ratio = info.file_size / max(info.compress_size, 1)
        if ratio > MAX_COMPRESSION_RATIO:
            archive.close()
            raise ValidationError("zip_bomb", "ZIP compression ratio exceeds limit")
    if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
        archive.close()
        raise ValidationError("zip_too_large", "ZIP uncompressed size exceeds limit")
    return archive


def _csv_rows(
    archive: zipfile.ZipFile, filename: str
) -> Iterable[tuple[int, dict[str, str]]]:
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(MAX_CSV_FIELD_BYTES)
    try:
        with (
            archive.open(filename, "r") as binary,
            io.TextIOWrapper(
                binary, encoding="utf-8-sig", errors="strict", newline=""
            ) as text,
        ):
            reader = csv.DictReader(text)
            if (
                reader.fieldnames is None
                or tuple(reader.fieldnames) != EXPECTED_HEADERS[filename]
            ):
                raise ValidationError(
                    "invalid_csv_header",
                    f"{filename} header does not match the pinned contract",
                    source_file=filename,
                    row_number=1,
                )
            for count, row in enumerate(reader, start=1):
                row_number = count + 1
                if count > MAX_ROWS[filename]:
                    raise ValidationError(
                        "too_many_rows",
                        f"{filename} exceeds row limit",
                        source_file=filename,
                        row_number=row_number,
                    )
                if None in row or any(value is None for value in row.values()):
                    raise ValidationError(
                        "invalid_csv_row",
                        f"{filename} row has the wrong column count",
                        source_file=filename,
                        row_number=row_number,
                    )
                yield row_number, {str(key): str(value) for key, value in row.items()}
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "invalid_csv_encoding", f"{filename} is not UTF-8", source_file=filename
        ) from exc
    except csv.Error as exc:
        raise ValidationError(
            "invalid_csv", f"{filename} is malformed", source_file=filename
        ) from exc
    finally:
        csv.field_size_limit(previous_limit)


def _decorate_validation_error(
    error: ValidationError,
    *,
    filename: str,
    row_number: int,
    row: Mapping[str, str],
) -> ValidationError:
    if error.source_file is not None:
        return error
    return ValidationError(
        error.code,
        str(error),
        source_file=filename,
        row_number=row_number,
        payload_sha256=_canonical_row_sha256(row),
    )


def _sale_identity(
    row: Mapping[str, str],
    contract: SalesExportContract | None,
) -> tuple[str, str, str]:
    marketplace = row["marketplace"].strip().lower()
    if marketplace not in SALE_MARKETPLACES:
        raise ValidationError("unsupported_marketplace", "unsupported sale marketplace")
    sale_id_int = _parse_uint(row["sale_id"], "sale_id")
    assert sale_id_int is not None
    sale_id = str(sale_id_int)
    sold_at, _, _ = _export_timestamp(row["sold_at"], "sold_at", contract)
    return marketplace, sale_id, sold_at


def _insert_sale_row(
    connection: sqlite3.Connection,
    row: Mapping[str, str],
    *,
    row_number: int,
    run_id: str,
    archive_sha256: str,
    imported_at: str,
    contract: SalesExportContract | None,
) -> tuple[tuple[str, str, str], int, bool]:
    marketplace, sale_id, sold_at = _sale_identity(row, contract)
    market_hash_name = _required_text(row["market_hash_name"], "market_hash_name")
    price = _parse_uint(row["price"], "price", maximum=MAX_NATIVE_MINOR)
    assert price is not None
    if price <= 0:
        raise ValidationError("invalid_price", "price must be greater than zero")
    currency, currency_verified = _export_currency(marketplace, contract)
    sold_at, sold_at_raw, timestamp_assumption = _export_timestamp(
        row["sold_at"], "sold_at", contract
    )
    observed_at, observed_at_raw, observed_assumption = _export_timestamp(
        row["fetched_at"], "fetched_at", contract
    )
    if timestamp_assumption != observed_assumption:
        timestamp_assumption = (
            f"sold={timestamp_assumption};fetched={observed_assumption}"
        )
    float_value = _parse_decimal_text(
        row["float_value"],
        "float_value",
        optional=True,
        minimum=Decimal(0),
        maximum=Decimal(1),
    )
    payload_hash = _canonical_row_sha256(row)
    event_day = date.fromisoformat(sold_at[:10])
    fx_rate = (
        resolve_fx_rate(connection, currency, event_day) if currency_verified else None
    )
    usd_micros, usd_cents = _money_usd(price, 2, fx_rate)
    fx_id, fx_text, fx_date, fx_source = _fx_columns(fx_rate)
    values = (
        PROVIDER_CSPRICEAPI,
        marketplace,
        sale_id,
        "sale",
        "event",
        market_hash_name,
        price,
        currency,
        int(currency_verified),
        2,
        usd_micros,
        usd_cents,
        fx_id,
        fx_text,
        fx_date,
        fx_source,
        sold_at,
        sold_at_raw,
        timestamp_assumption,
        observed_at,
        observed_at_raw,
        imported_at,
        _parse_uint(row["def_index"], "def_index", optional=True, maximum=2**31 - 1),
        _parse_uint(
            row["paint_index"], "paint_index", optional=True, maximum=2**31 - 1
        ),
        _parse_uint(row["paint_seed"], "paint_seed", optional=True, maximum=2**31 - 1),
        float_value,
        _parse_flag(row["is_stattrak"], "is_stattrak"),
        _parse_flag(row["is_souvenir"], "is_souvenir"),
        _parse_uint(row["rarity"], "rarity", optional=True, maximum=2**31 - 1),
        _parse_uint(row["quality"], "quality", optional=True, maximum=2**31 - 1),
        _optional_https_url(row["cs2_img"], "cs2_img"),
        payload_hash,
        archive_sha256,
        "sales.csv",
        row_number,
        run_id,
    )
    cursor = connection.execute(
        """
        INSERT INTO market_sale_events(
            provider_code, marketplace_code, provider_sale_id, event_kind,
            granularity, market_hash_name, native_price_minor, currency,
            currency_verified, minor_scale, price_usd_micros, price_usd_cents, fx_rate_id,
            fx_rate_to_usd, fx_rate_date, fx_rate_source, source_event_at,
            source_event_at_raw, timestamp_assumption, provider_observed_at,
            provider_observed_at_raw, imported_at, def_index, paint_index, paint_seed,
            float_value, is_stattrak, is_souvenir, rarity, quality, image_url,
            payload_sha256, archive_sha256, source_file, source_row_number,
            ingest_run_id
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(
            provider_code, marketplace_code, provider_sale_id,
            source_event_at, event_kind, granularity
        ) DO NOTHING
        """,
        values,
    )
    inserted = cursor.rowcount == 1
    existing = connection.execute(
        """
        SELECT event_id, payload_sha256
          FROM market_sale_events
         WHERE provider_code=? AND marketplace_code=? AND provider_sale_id=?
           AND source_event_at=? AND event_kind='sale' AND granularity='event'
        """,
        (PROVIDER_CSPRICEAPI, marketplace, sale_id, sold_at),
    ).fetchone()
    if existing is None:
        raise MarketHistoryError("sale upsert did not produce a row")
    if str(existing["payload_sha256"]) != payload_hash:
        raise ValidationError(
            "sale_identity_collision",
            "same sale identity has different payload",
            source_file="sales.csv",
            row_number=row_number,
            payload_sha256=payload_hash,
        )
    return (marketplace, sale_id, sold_at), int(existing["event_id"]), inserted


def _insert_attachment_row(
    connection: sqlite3.Connection,
    row: Mapping[str, str],
    *,
    kind: str,
    row_number: int,
    event_id: int,
    run_id: str,
    archive_sha256: str,
    imported_at: str,
    contract: SalesExportContract | None,
) -> bool:
    filename = "stickers.csv" if kind == "sticker" else "keychains.csv"
    id_field = "sticker_id" if kind == "sticker" else "keychain_id"
    name_field = "sticker_name" if kind == "sticker" else "keychain_name"
    provider_item_id_int = _parse_uint(row[id_field], id_field)
    assert provider_item_id_int is not None
    provider_item_id = str(provider_item_id_int)
    position_idx = _parse_uint(row["position_idx"], "position_idx", maximum=255)
    assert position_idx is not None
    slot = _parse_uint(row["slot"], "slot", optional=True, maximum=255)
    item_name = _required_text(row[name_field], name_field)
    marketplace, _, sold_at = _sale_identity(row, contract)
    currency, currency_verified = _export_currency(marketplace, contract)
    observed_at, observed_at_raw, timestamp_assumption = _export_timestamp(
        row["fetched_at"], "fetched_at", contract
    )
    reference_updated = None
    if row["reference_updated"].strip():
        reference_updated, _, reference_assumption = _export_timestamp(
            row["reference_updated"], "reference_updated", contract
        )
        if reference_assumption != timestamp_assumption:
            timestamp_assumption = (
                f"fetched={timestamp_assumption};reference={reference_assumption}"
            )
    reference_price = _parse_uint(
        row["reference_price"],
        "reference_price",
        optional=True,
        maximum=MAX_NATIVE_MINOR,
    )
    fx_on = date.fromisoformat((reference_updated or sold_at)[:10])
    fx_rate = (
        resolve_fx_rate(connection, currency, fx_on) if currency_verified else None
    )
    usd_micros, usd_cents = (
        _money_usd(reference_price, 2, fx_rate)
        if reference_price is not None
        else (None, None)
    )
    fx_id, fx_text, fx_date, fx_source = _fx_columns(fx_rate)
    payload_hash = _canonical_row_sha256(row)
    common_values = {
        "event_id": event_id,
        "position_idx": position_idx,
        "provider_item_id": provider_item_id,
        "slot": slot,
        "item_name": item_name,
        "reference_price": reference_price,
        "currency": currency,
        "usd_micros": usd_micros,
        "usd_cents": usd_cents,
        "fx_id": fx_id,
        "fx_text": fx_text,
        "fx_date": fx_date,
        "fx_source": fx_source,
        "reference_quantity": _parse_uint(
            row["reference_quantity"],
            "reference_quantity",
            optional=True,
            maximum=2**31 - 1,
        ),
        "reference_updated": reference_updated,
        "observed_at": observed_at,
        "payload_hash": payload_hash,
    }
    if kind == "sticker":
        cursor = connection.execute(
            """
            INSERT INTO market_sale_stickers(
                event_id, position_idx, provider_sticker_id, slot, sticker_name,
                wear, offset_x, offset_y, rotation,
                native_reference_price_minor, currency, minor_scale,
                currency_verified,
                reference_price_usd_micros, reference_price_usd_cents,
                fx_rate_id, fx_rate_to_usd, fx_rate_date, fx_rate_source,
                reference_quantity, reference_updated_at, provider_observed_at,
                provider_observed_at_raw, timestamp_assumption,
                imported_at, payload_sha256, archive_sha256, source_file,
                source_row_number, ingest_run_id
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?
            ) ON CONFLICT(event_id, position_idx) DO NOTHING
            """,
            (
                event_id,
                position_idx,
                provider_item_id,
                slot,
                item_name,
                _parse_decimal_text(
                    row["wear"],
                    "wear",
                    optional=True,
                    minimum=Decimal(0),
                    maximum=Decimal(1),
                ),
                _parse_decimal_text(row["offset_x"], "offset_x", optional=True),
                _parse_decimal_text(row["offset_y"], "offset_y", optional=True),
                _parse_decimal_text(row["rotation"], "rotation", optional=True),
                reference_price,
                currency,
                int(currency_verified),
                usd_micros,
                usd_cents,
                fx_id,
                fx_text,
                fx_date,
                fx_source,
                common_values["reference_quantity"],
                reference_updated,
                observed_at,
                observed_at_raw,
                timestamp_assumption,
                imported_at,
                payload_hash,
                archive_sha256,
                filename,
                row_number,
                run_id,
            ),
        )
        existing = connection.execute(
            """
            SELECT payload_sha256 FROM market_sale_stickers
             WHERE event_id=? AND position_idx=?
            """,
            (event_id, position_idx),
        ).fetchone()
    else:
        cursor = connection.execute(
            """
            INSERT INTO market_sale_keychains(
                event_id, position_idx, provider_keychain_id, slot, keychain_name,
                pattern, offset_x, offset_y, offset_z,
                native_reference_price_minor, currency, minor_scale,
                currency_verified,
                reference_price_usd_micros, reference_price_usd_cents,
                fx_rate_id, fx_rate_to_usd, fx_rate_date, fx_rate_source,
                reference_quantity, reference_updated_at, provider_observed_at,
                provider_observed_at_raw, timestamp_assumption,
                imported_at, payload_sha256, archive_sha256, source_file,
                source_row_number, ingest_run_id
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?
            ) ON CONFLICT(event_id, position_idx) DO NOTHING
            """,
            (
                event_id,
                position_idx,
                provider_item_id,
                slot,
                item_name,
                _parse_uint(
                    row["pattern"], "pattern", optional=True, maximum=2**31 - 1
                ),
                _parse_decimal_text(row["offset_x"], "offset_x", optional=True),
                _parse_decimal_text(row["offset_y"], "offset_y", optional=True),
                _parse_decimal_text(row["offset_z"], "offset_z", optional=True),
                reference_price,
                currency,
                int(currency_verified),
                usd_micros,
                usd_cents,
                fx_id,
                fx_text,
                fx_date,
                fx_source,
                common_values["reference_quantity"],
                reference_updated,
                observed_at,
                observed_at_raw,
                timestamp_assumption,
                imported_at,
                payload_hash,
                archive_sha256,
                filename,
                row_number,
                run_id,
            ),
        )
        existing = connection.execute(
            """
            SELECT payload_sha256 FROM market_sale_keychains
             WHERE event_id=? AND position_idx=?
            """,
            (event_id, position_idx),
        ).fetchone()
    if existing is None:
        raise MarketHistoryError("attachment upsert did not produce a row")
    if str(existing["payload_sha256"]) != payload_hash:
        raise ValidationError(
            "attachment_identity_collision",
            "same attachment position has different payload",
            source_file=filename,
            row_number=row_number,
            payload_sha256=payload_hash,
        )
    return cursor.rowcount == 1


def import_cspriceapi_sales_zip(
    connection: sqlite3.Connection,
    blob: bytes,
    *,
    source_filename: str = "market-sales-export.zip",
    source_uri: str = CSPRICEAPI_EXPORT_URL,
    response_headers: Mapping[str, str] | None = None,
    contract: SalesExportContract | None = None,
    require_response_headers: bool = False,
) -> ImportResult:
    """Atomically import one validated Enterprise export, or quarantine it."""

    provider_code = PROVIDER_CSPRICEAPI
    payload_hash = _sha256(blob)
    normalized_headers = {
        str(key).lower(): str(value) for key, value in (response_headers or {}).items()
    }
    provider_observed_at = normalized_headers.get("x-window-end")
    if provider_observed_at:
        provider_observed_at = _normalize_timestamp(
            provider_observed_at, "X-Window-End"
        )
    window_start = normalized_headers.get("x-window-start")
    if window_start:
        window_start = _normalize_timestamp(window_start, "X-Window-Start")
    run_id, reused = _start_run(
        connection,
        provider_code=provider_code,
        ingest_kind="sales_zip",
        marketplace_code=None,
        payload_sha256=payload_hash,
        source_uri=source_uri,
        source_filename=Path(source_filename).name,
        provider_observed_at=provider_observed_at,
        contract_version=contract.version if contract is not None else None,
        currency_contract_status=(
            "verified_out_of_band" if contract is not None else "unverified"
        ),
        timestamp_assumption=(
            f"UTC_verified:{contract.version}"
            if contract is not None and contract.naive_timestamps_are_utc
            else "UTC_unverified"
        ),
        window_start=window_start,
        window_end=provider_observed_at,
    )
    if reused is not None:
        return reused

    archive: zipfile.ZipFile | None = None
    imported_at = utc_now()
    try:
        if require_response_headers:
            required_headers = {
                "x-sales-row-count",
                "x-sticker-row-count",
                "x-keychain-row-count",
                "x-window-hours",
                "x-window-start",
                "x-window-end",
            }
            missing_headers = sorted(required_headers - normalized_headers.keys())
            if missing_headers:
                raise ValidationError(
                    "missing_response_header",
                    "Enterprise export response is missing required metadata",
                )
            try:
                window_hours = int(normalized_headers["x-window-hours"])
            except ValueError as exc:
                raise ValidationError(
                    "invalid_response_header", "invalid x-window-hours"
                ) from exc
            if (
                window_hours != 24
                or window_start is None
                or provider_observed_at is None
            ):
                raise ValidationError(
                    "invalid_export_window", "Enterprise export window must be 24 hours"
                )
            start_time = datetime.fromisoformat(window_start.replace("Z", "+00:00"))
            end_time = datetime.fromisoformat(
                provider_observed_at.replace("Z", "+00:00")
            )
            if (end_time - start_time).total_seconds() != 24 * 60 * 60:
                raise ValidationError(
                    "invalid_export_window",
                    "Enterprise export timestamps are not 24 hours apart",
                )
        archive = _validate_zip(blob)
        sale_events: dict[tuple[str, str, str], int] = {}
        sale_payloads: dict[tuple[str, str, str], str] = {}
        inserted_sales = 0
        duplicate_sales = 0
        inserted_stickers = 0
        inserted_keychains = 0
        source_sales = 0
        source_stickers = 0
        source_keychains = 0
        connection.execute("BEGIN IMMEDIATE")
        for row_number, row in _csv_rows(archive, "sales.csv"):
            source_sales += 1
            try:
                identity = _sale_identity(row, contract)
                row_hash = _canonical_row_sha256(row)
                previous_hash = sale_payloads.get(identity)
                if previous_hash is not None:
                    raise ValidationError(
                        "duplicate_sale_row",
                        "sales.csv contains a duplicate sale identity",
                    )
                sale_payloads[identity] = row_hash
                identity, event_id, inserted = _insert_sale_row(
                    connection,
                    row,
                    row_number=row_number,
                    run_id=run_id,
                    archive_sha256=payload_hash,
                    imported_at=imported_at,
                    contract=contract,
                )
                sale_events[identity] = event_id
                inserted_sales += int(inserted)
                duplicate_sales += int(not inserted)
            except ValidationError as exc:
                raise _decorate_validation_error(
                    exc, filename="sales.csv", row_number=row_number, row=row
                ) from exc

        for filename, kind in (
            ("stickers.csv", "sticker"),
            ("keychains.csv", "keychain"),
        ):
            seen_positions: set[tuple[tuple[str, str, str], int]] = set()
            for row_number, row in _csv_rows(archive, filename):
                if kind == "sticker":
                    source_stickers += 1
                else:
                    source_keychains += 1
                try:
                    identity = _sale_identity(row, contract)
                    event_id = sale_events.get(identity)
                    if event_id is None:
                        raise ValidationError(
                            "orphan_attachment",
                            f"{filename} row has no matching sale in this archive",
                        )
                    position = _parse_uint(
                        row["position_idx"], "position_idx", maximum=255
                    )
                    assert position is not None
                    position_key = (identity, position)
                    if position_key in seen_positions:
                        raise ValidationError(
                            "duplicate_attachment_row",
                            f"{filename} contains duplicate sale/position",
                        )
                    seen_positions.add(position_key)
                    inserted = _insert_attachment_row(
                        connection,
                        row,
                        kind=kind,
                        row_number=row_number,
                        event_id=event_id,
                        run_id=run_id,
                        archive_sha256=payload_hash,
                        imported_at=imported_at,
                        contract=contract,
                    )
                    if kind == "sticker":
                        inserted_stickers += int(inserted)
                    else:
                        inserted_keychains += int(inserted)
                except ValidationError as exc:
                    raise _decorate_validation_error(
                        exc, filename=filename, row_number=row_number, row=row
                    ) from exc

        total_source = source_sales + source_stickers + source_keychains
        total_inserted = inserted_sales + inserted_stickers + inserted_keychains
        for header, actual in (
            ("x-sales-row-count", source_sales),
            ("x-sticker-row-count", source_stickers),
            ("x-keychain-row-count", source_keychains),
        ):
            expected_raw = normalized_headers.get(header)
            if expected_raw is None:
                continue
            try:
                expected = int(expected_raw)
            except ValueError as exc:
                raise ValidationError(
                    "invalid_response_header", f"invalid {header}"
                ) from exc
            if expected < 0 or expected != actual:
                raise ValidationError(
                    "row_count_mismatch", f"{header} does not match archive"
                )
        connection.execute(
            """
            UPDATE ingest_runs
               SET status='complete', completed_at=?, source_rows=?, inserted_rows=?,
                   duplicate_rows=?, sticker_rows=?, keychain_rows=?
             WHERE run_id=?
            """,
            (
                utc_now(),
                total_source,
                total_inserted,
                duplicate_sales,
                source_stickers,
                source_keychains,
                run_id,
            ),
        )
        connection.commit()
        return ImportResult(
            run_id=run_id,
            status="complete",
            source_rows=total_source,
            inserted_rows=total_inserted,
            duplicate_rows=duplicate_sales,
            attachment_rows=source_stickers + source_keychains,
            payload_sha256=payload_hash,
        )
    except ValidationError as exc:
        connection.rollback()
        _quarantine_run(connection, run_id, exc)
        raise
    except Exception as exc:
        connection.rollback()
        _fail_run(connection, run_id, exc)
        raise
    finally:
        if archive is not None:
            archive.close()


def _snapshot_price_field(marketplace: str) -> str:
    return "price_rmb" if marketplace == "youpin" else "price"


def _json_decimal_text(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValidationError("invalid_json_type", f"{field} must be a decimal string")
    return _parse_decimal_text(value, field, optional=optional)


def import_cspriceapi_snapshot(
    connection: sqlite3.Connection,
    marketplace: str,
    payload: bytes,
    *,
    source_uri: str | None = None,
) -> ImportResult:
    """Import one BUFF163/YouPin listing snapshot (never a sale event)."""

    market = marketplace.strip().lower()
    if market not in SNAPSHOT_MARKETPLACES:
        raise ValidationError(
            "unsupported_marketplace", "unsupported snapshot marketplace"
        )
    if not payload or len(payload) > MAX_SNAPSHOT_BYTES:
        raise ValidationError(
            "invalid_snapshot_size", "snapshot payload size is invalid"
        )
    response_hash = _sha256(payload)
    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError(
            "invalid_snapshot_json", "snapshot JSON is malformed"
        ) from exc
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        raise ValidationError(
            "invalid_snapshot_shape", "snapshot response shape is invalid"
        )
    data = document["data"]
    if len(data) > 100_000:
        raise ValidationError("too_many_rows", "snapshot exceeds row limit")
    provider_observed_at = _normalize_timestamp(
        str(document.get("timestamp", "")), "snapshot timestamp"
    )
    provider_source_name = document.get("source")
    if provider_source_name is not None:
        provider_source_name = _required_text(
            str(provider_source_name), "snapshot source", maximum=100
        )
    uri = source_uri or CSPRICEAPI_SNAPSHOT_URLS[market]
    run_id, reused = _start_run(
        connection,
        provider_code=PROVIDER_CSPRICEAPI,
        ingest_kind="listing_snapshot",
        marketplace_code=market,
        payload_sha256=response_hash,
        source_uri=uri,
        source_filename=None,
        provider_observed_at=provider_observed_at,
    )
    if reused is not None:
        return reused
    imported_at = utc_now()
    inserted = 0
    duplicates = 0
    seen: set[tuple[str, str]] = set()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for index, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                raise ValidationError(
                    "invalid_snapshot_row",
                    "snapshot item must be an object",
                    source_file=f"{market}.json",
                    row_number=index,
                )
            row_hash = _sha256(
                json.dumps(
                    item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            )
            try:
                market_hash_name = _required_text(
                    str(item.get("market_hash_name", "")), "market_hash_name"
                )
                source_event_at = _normalize_timestamp(
                    str(item.get("updated_at", "")), "updated_at"
                )
                identity = (market_hash_name, source_event_at)
                if identity in seen:
                    raise ValidationError(
                        "duplicate_snapshot_row",
                        "snapshot contains duplicate item/timestamp",
                    )
                seen.add(identity)
                price_field = _snapshot_price_field(market)
                price_raw = item.get(price_field)
                if not isinstance(price_raw, str):
                    raise ValidationError(
                        "invalid_json_type", f"{price_field} must be a decimal string"
                    )
                native_minor, minor_scale = _parse_native_decimal(
                    price_raw, price_field
                )
                currency = MARKET_CURRENCIES[market]
                fx_rate = resolve_fx_rate(
                    connection, currency, date.fromisoformat(source_event_at[:10])
                )
                usd_micros, usd_cents = _money_usd(native_minor, minor_scale, fx_rate)
                fx_id, fx_text, fx_date, fx_source = _fx_columns(fx_rate)
                count_value = item.get("count")
                if isinstance(count_value, bool):
                    raise ValidationError(
                        "invalid_json_type", "count must be an integer"
                    )
                listing_count = (
                    _parse_uint(str(count_value), "count", maximum=2**31 - 1)
                    if count_value is not None
                    else None
                )
                provider_item = item.get("id")
                provider_item_id = (
                    str(_parse_uint(str(provider_item), "id"))
                    if provider_item is not None
                    else None
                )
                liquidity = _json_decimal_text(
                    item.get("liquidity"), "liquidity", optional=True
                )
                cursor = connection.execute(
                    """
                    INSERT INTO price_snapshots(
                        provider_code, marketplace_code, provider_item_id,
                        event_kind, granularity, market_hash_name,
                        native_price_minor, currency, minor_scale,
                        price_usd_micros, price_usd_cents, fx_rate_id,
                        fx_rate_to_usd, fx_rate_date, fx_rate_source,
                        listing_count, liquidity, source_event_at,
                        provider_observed_at, imported_at, provider_source_name,
                        payload_sha256, response_sha256, source_uri,
                        source_row_number, ingest_run_id
                    ) VALUES (
                        ?, ?, ?, 'listing_snapshot', 'point', ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    ) ON CONFLICT(
                        provider_code, marketplace_code, market_hash_name,
                        source_event_at, event_kind, granularity
                    ) DO NOTHING
                    """,
                    (
                        PROVIDER_CSPRICEAPI,
                        market,
                        provider_item_id,
                        market_hash_name,
                        native_minor,
                        currency,
                        minor_scale,
                        usd_micros,
                        usd_cents,
                        fx_id,
                        fx_text,
                        fx_date,
                        fx_source,
                        listing_count,
                        liquidity,
                        source_event_at,
                        provider_observed_at,
                        imported_at,
                        provider_source_name,
                        row_hash,
                        response_hash,
                        uri,
                        index,
                        run_id,
                    ),
                )
                existing = connection.execute(
                    """
                    SELECT payload_sha256 FROM price_snapshots
                     WHERE provider_code=? AND marketplace_code=?
                       AND market_hash_name=? AND source_event_at=?
                       AND event_kind='listing_snapshot' AND granularity='point'
                    """,
                    (PROVIDER_CSPRICEAPI, market, market_hash_name, source_event_at),
                ).fetchone()
                if existing is None:
                    raise MarketHistoryError("snapshot upsert did not produce a row")
                if str(existing["payload_sha256"]) != row_hash:
                    raise ValidationError(
                        "snapshot_identity_collision",
                        "same snapshot identity has different payload",
                    )
                inserted += int(cursor.rowcount == 1)
                duplicates += int(cursor.rowcount != 1)
            except ValidationError as exc:
                raise ValidationError(
                    exc.code,
                    str(exc),
                    source_file=f"{market}.json",
                    row_number=index,
                    payload_sha256=row_hash,
                ) from exc
        connection.execute(
            """
            UPDATE ingest_runs
               SET status='complete', completed_at=?, source_rows=?,
                   inserted_rows=?, duplicate_rows=?
             WHERE run_id=?
            """,
            (utc_now(), len(data), inserted, duplicates, run_id),
        )
        connection.commit()
        return ImportResult(
            run_id=run_id,
            status="complete",
            source_rows=len(data),
            inserted_rows=inserted,
            duplicate_rows=duplicates,
            payload_sha256=response_hash,
        )
    except ValidationError as exc:
        connection.rollback()
        _quarantine_run(connection, run_id, exc)
        raise
    except Exception as exc:
        connection.rollback()
        _fail_run(connection, run_id, exc)
        raise


def read_zip_file_bounded(path: str | Path) -> bytes:
    """Read a manual ZIP without following symlinks or accepting oversized files."""

    source = Path(path)
    try:
        file_stat = source.lstat()
    except OSError as exc:
        raise ValidationError("zip_unreadable", "cannot read ZIP file") from exc
    if not stat.S_ISREG(file_stat.st_mode) or source.is_symlink():
        raise ValidationError("unsafe_zip_file", "ZIP path must be a regular file")
    if file_stat.st_size <= 0 or file_stat.st_size > MAX_DOWNLOAD_BYTES:
        raise ValidationError("invalid_zip_size", "ZIP payload size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            payload = handle.read(MAX_DOWNLOAD_BYTES + 1)
    except OSError as exc:
        raise ValidationError("zip_unreadable", "cannot read ZIP file") from exc
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise ValidationError("invalid_zip_size", "ZIP payload size is invalid")
    return payload


def read_ecb_xml_file_bounded(path: str | Path) -> bytes:
    """Read a local ECB fixture with the same regular-file/symlink safeguards."""

    source = Path(path)
    try:
        file_stat = source.lstat()
    except OSError as exc:
        raise ValidationError("ecb_xml_unreadable", "cannot read ECB XML file") from exc
    if not stat.S_ISREG(file_stat.st_mode) or source.is_symlink():
        raise ValidationError(
            "unsafe_ecb_xml_file", "ECB XML path must be a regular file"
        )
    if file_stat.st_size <= 0 or file_stat.st_size > MAX_ECB_BYTES:
        raise ValidationError("invalid_ecb_size", "ECB XML file size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            payload = handle.read(MAX_ECB_BYTES + 1)
    except OSError as exc:
        raise ValidationError("ecb_xml_unreadable", "cannot read ECB XML file") from exc
    if len(payload) > MAX_ECB_BYTES:
        raise ValidationError("invalid_ecb_size", "ECB XML file size is invalid")
    return payload


def bounded_http_get(
    url: str,
    *,
    api_key: str | None = None,
    max_bytes: int,
    timeout: tuple[float, float] = (5.0, 60.0),
    accept: str,
    allowed_content_types: tuple[str, ...],
    session: Any = None,
) -> DownloadedPayload:
    """GET one allowlisted endpoint with redirect and body-size limits."""

    if url not in ALLOWED_HTTP_URLS:
        raise DownloadError("url_not_allowed", "HTTP endpoint is not allowlisted")
    if api_key is not None and not api_key.strip():
        raise DownloadError("missing_api_key", "CSPRICEAPI_KEY is not configured")
    headers = {
        "Accept": accept,
        "User-Agent": "CS2MarketHistory/1.0",
    }
    if api_key is not None:
        headers["x-api-key"] = api_key.strip()
    if session is None:
        import requests

        session = requests
    try:
        response = session.get(
            url,
            headers=headers,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
        )
    except Exception as exc:
        raise DownloadError("network_error", "upstream request failed") from exc
    with closing(response):
        status_code = int(getattr(response, "status_code", 0))
        if status_code in {301, 302, 303, 307, 308}:
            raise DownloadError(
                "redirect_rejected",
                "upstream redirect was rejected",
                status_code=status_code,
            )
        if status_code != 200:
            if status_code in {401, 403}:
                code = "not_authorized"
            elif status_code == 429:
                code = "rate_limited"
            else:
                code = "http_error"
            raise DownloadError(
                code, f"upstream returned HTTP {status_code}", status_code=status_code
            )
        raw_headers = {
            str(key): str(value)
            for key, value in dict(getattr(response, "headers", {})).items()
        }
        response_content_type = next(
            (
                value.split(";", 1)[0].strip().lower()
                for key, value in raw_headers.items()
                if key.lower() == "content-type"
            ),
            "",
        )
        if response_content_type not in allowed_content_types:
            raise DownloadError(
                "invalid_content_type", "upstream Content-Type is invalid"
            )
        content_length = None
        for key, value in raw_headers.items():
            if key.lower() == "content-length":
                try:
                    content_length = int(value)
                except ValueError as exc:
                    raise DownloadError(
                        "invalid_content_length", "invalid Content-Length"
                    ) from exc
                break
        if content_length is not None and (
            content_length < 0 or content_length > max_bytes
        ):
            raise DownloadError(
                "download_too_large", "upstream payload exceeds size limit"
            )
        body = bytearray()
        try:
            chunks = response.iter_content(chunk_size=64 * 1024)
            for chunk in chunks:
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise DownloadError(
                        "download_too_large", "upstream payload exceeds size limit"
                    )
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError(
                "network_error", "upstream response stream failed"
            ) from exc
        if not body:
            raise DownloadError("empty_response", "upstream response is empty")
        return DownloadedPayload(bytes(body), status_code, raw_headers)


def download_cspriceapi_sales_zip(
    api_key: str,
    *,
    session: Any = None,
) -> DownloadedPayload:
    return bounded_http_get(
        CSPRICEAPI_EXPORT_URL,
        api_key=api_key,
        max_bytes=MAX_DOWNLOAD_BYTES,
        timeout=(5.0, 90.0),
        accept="application/zip",
        allowed_content_types=("application/zip", "application/octet-stream"),
        session=session,
    )


def download_cspriceapi_snapshot(
    marketplace: str,
    api_key: str,
    *,
    session: Any = None,
) -> DownloadedPayload:
    market = marketplace.strip().lower()
    if market not in CSPRICEAPI_SNAPSHOT_URLS:
        raise ValidationError(
            "unsupported_marketplace", "unsupported snapshot marketplace"
        )
    return bounded_http_get(
        CSPRICEAPI_SNAPSHOT_URLS[market],
        api_key=api_key,
        max_bytes=MAX_SNAPSHOT_BYTES,
        timeout=(5.0, 60.0),
        accept="application/json",
        allowed_content_types=("application/json",),
        session=session,
    )


def download_ecb_rates(*, session: Any = None) -> DownloadedPayload:
    return bounded_http_get(
        ECB_HISTORY_URL,
        max_bytes=MAX_ECB_BYTES,
        timeout=(5.0, 60.0),
        accept="application/xml,text/xml;q=0.9",
        allowed_content_types=("application/xml", "text/xml"),
        session=session,
    )


def backfill_missing_usd_prices(connection: sqlite3.Connection) -> dict[str, int]:
    """Fill only missing derived USD values; native/provenance columns stay intact."""

    updated_sales = 0
    updated_snapshots = 0
    updated_attachments = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        for (
            table,
            primary_key,
            price_column,
            micros_column,
            cents_column,
            timestamp_column,
        ) in (
            (
                "market_sale_events",
                "event_id",
                "native_price_minor",
                "price_usd_micros",
                "price_usd_cents",
                "source_event_at",
            ),
            (
                "price_snapshots",
                "snapshot_id",
                "native_price_minor",
                "price_usd_micros",
                "price_usd_cents",
                "source_event_at",
            ),
            (
                "market_sale_stickers",
                "sale_sticker_id",
                "native_reference_price_minor",
                "reference_price_usd_micros",
                "reference_price_usd_cents",
                "reference_updated_at",
            ),
            (
                "market_sale_keychains",
                "sale_keychain_id",
                "native_reference_price_minor",
                "reference_price_usd_micros",
                "reference_price_usd_cents",
                "reference_updated_at",
            ),
        ):
            rows = connection.execute(
                f"""
                SELECT {primary_key} AS row_id, {price_column} AS native_minor,
                       minor_scale, currency, {timestamp_column} AS source_at
                  FROM {table}
                 WHERE {micros_column} IS NULL
                   AND {price_column} IS NOT NULL
                   AND currency_verified=1
                """
            ).fetchall()
            for row in rows:
                source_at = row["source_at"]
                if not source_at:
                    continue
                fx_rate = resolve_fx_rate(
                    connection,
                    str(row["currency"]),
                    date.fromisoformat(str(source_at)[:10]),
                )
                if fx_rate is None:
                    continue
                micros, cents = _money_usd(
                    int(row["native_minor"]), int(row["minor_scale"]), fx_rate
                )
                fx_id, fx_text, fx_date, fx_source = _fx_columns(fx_rate)
                connection.execute(
                    f"""
                    UPDATE {table}
                       SET {micros_column}=?, {cents_column}=?, fx_rate_id=?,
                           fx_rate_to_usd=?, fx_rate_date=?, fx_rate_source=?
                     WHERE {primary_key}=? AND {micros_column} IS NULL
                    """,
                    (micros, cents, fx_id, fx_text, fx_date, fx_source, row["row_id"]),
                )
                if table == "market_sale_events":
                    updated_sales += 1
                elif table == "price_snapshots":
                    updated_snapshots += 1
                else:
                    updated_attachments += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "sales": updated_sales,
        "snapshots": updated_snapshots,
        "attachments": updated_attachments,
    }
