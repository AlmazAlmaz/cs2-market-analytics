"""Read-only multi-market history API adapter and standalone dashboard page.

The module is deliberately isolated from :mod:`dashboard_server`.  Its public
builders open the dedicated market-history database read-only and expose only
the normalized, allow-listed contract needed by the UI.  Completed sales and
listing snapshots are separate event kinds: callers must select exactly one
kind and no price statistic is ever calculated across both.

Raw provider payloads, authentication material and ingestion diagnostics are
outside this presentation contract.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import app_config

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = app_config.path(
    "CS2_MARKET_HISTORY_DB_PATH", ".private/market_history.db"
)
DEFAULT_DAYS = 90
DEFAULT_LIMIT = 100
MAX_DAYS = 3_650
MAX_LIMIT = 250
MAX_OFFSET = 100_000
MAX_FILTER_TEXT = 180
EVENT_KINDS = frozenset({"confirmed_sale", "listing_snapshot"})
_SOURCE_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
_UNKNOWN_CURRENCIES = frozenset({"", "UNK", "UNKNOWN"})
_BASE_REQUIRED_COLUMNS = frozenset(
    {
        "provider_code",
        "marketplace_code",
        "event_kind",
        "granularity",
        "market_hash_name",
        "native_price_minor",
        "currency",
        "currency_verified",
        "minor_scale",
        "price_usd_micros",
        "price_usd_cents",
        "fx_rate_id",
        "fx_rate_to_usd",
        "fx_rate_date",
        "fx_rate_source",
        "source_event_at",
        "provider_observed_at",
        "imported_at",
    }
)
_REQUIRED_TABLE_COLUMNS = {
    "market_history_meta": frozenset({"key", "value", "updated_at"}),
    "source_datasets": frozenset(
        {
            "dataset_code",
            "marketplace_code",
            "provider_code",
            "storage_ref",
            "native_currency",
            "price_field",
            "model_training_eligible",
        }
    ),
    "fx_rates": frozenset(
        {
            "fx_rate_id",
            "provider_code",
            "source_code",
            "base_currency",
            "quote_currency",
            "rate_date",
            "rate_to_usd",
        }
    ),
    "market_sale_events": _BASE_REQUIRED_COLUMNS
    | frozenset(
        {
            "event_id",
            "provider_sale_id",
            "float_value",
            "paint_seed",
            "paint_index",
            "source_event_at_raw",
            "timestamp_assumption",
        }
    ),
    "price_snapshots": _BASE_REQUIRED_COLUMNS
    | frozenset({"snapshot_id", "listing_count", "liquidity"}),
}
_KIND_SPECS = {
    "confirmed_sale": {
        "table": "market_sale_events",
        "id": "event_id",
        "stored_kind": "sale",
        "granularity": "event",
    },
    "listing_snapshot": {
        "table": "price_snapshots",
        "id": "snapshot_id",
        "stored_kind": "listing_snapshot",
        "granularity": "point",
    },
}


def _bounded_int(
    value: Any, *, name: str, default: int, minimum: int, maximum: int
) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")  # noqa: TRY004
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _filter_text(value: Any, *, name: str) -> str:
    result = str(value or "").strip()
    if len(result) > MAX_FILTER_TEXT:
        raise ValueError(f"{name} must contain at most {MAX_FILTER_TEXT} characters")
    if any(ord(character) < 32 for character in result):
        raise ValueError(f"{name} contains control characters")
    return result


def _source_code(value: Any, *, name: str) -> str:
    result = str(value or "").strip().casefold()
    if result and _SOURCE_CODE_RE.fullmatch(result) is None:
        raise ValueError(f"{name} is invalid")
    return result


def _event_kind(value: Any) -> str:
    result = str(value or "confirmed_sale").strip().casefold()
    if result not in EVENT_KINDS:
        raise ValueError("kind must be confirmed_sale or listing_snapshot")
    return result


def _public_db_label(path: str | Path) -> str:
    """Return a non-sensitive file label instead of an absolute host path."""

    try:
        return Path(path).expanduser().name or "market_history.db"
    except (TypeError, ValueError, OSError):
        return "market_history.db"


def _is_existing_file(path: str | Path) -> bool:
    try:
        return Path(path).expanduser().is_file()
    except (TypeError, ValueError, OSError):
        return False


@contextmanager
def _read_only_connection(path: str | Path):
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        yield None
        return
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro", uri=True, timeout=10
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA busy_timeout=10000")
        yield connection
    finally:
        connection.close()


def _unavailable_status(path: str | Path, reason: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "unavailable",
        "reason": reason,
        "database": {
            "name": _public_db_label(path),
            "available": False,
        },
        "counts": {
            "confirmed_sale": 0,
            "listing_snapshot": 0,
        },
        "latest_observed_at": None,
        "sources": [],
        "datasets": [],
        "fx": {"eligible": 0, "covered": 0, "coverage": None},
    }


def _unavailable_payload(
    path: str | Path,
    *,
    reason: str,
    kind: str,
    search: str,
    marketplace: str,
    provider: str,
    days: int,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "unavailable",
        "reason": reason,
        "kind": kind,
        "truth": {
            "mixed_event_kinds": False,
            "price_meaning": (
                "executed_sale_price"
                if kind == "confirmed_sale"
                else "observed_listing_ask"
            ),
        },
        "filters": {
            "search": search,
            "marketplace": marketplace,
            "provider": provider,
            "days": days,
        },
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": 0,
            "has_more": False,
        },
        "summary": {
            "event_count": 0,
            "source_count": 0,
            "latest_event_at": None,
            "usd_coverage": None,
        },
        "facets": {"marketplaces": [], "providers": []},
        "series": [],
        "rows": [],
        "datasets": [],
        "database": {
            "name": _public_db_label(path),
            "available": False,
        },
    }


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _safe_integer(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _storage_schema_reason(connection: sqlite3.Connection) -> str | None:
    """Return ``None`` only for the pinned isolated storage contract."""

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        )
    }
    if "sales" in tables:
        return "unsafe_legacy_database"
    if not set(_REQUIRED_TABLE_COLUMNS).issubset(tables):
        return "schema_unavailable"
    for table, required in _REQUIRED_TABLE_COLUMNS.items():
        if not required.issubset(_table_columns(connection, table)):
            return "schema_unavailable"
    metadata = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT key, value FROM market_history_meta"
        )
    }
    if metadata.get("schema_version") != str(SCHEMA_VERSION):
        return "unsupported_schema_version"
    if metadata.get("database_purpose") != "isolated_multi_market_history":
        return "unsafe_database_purpose"
    if metadata.get("model_training_eligible") != "false":
        return "unsafe_model_training_contract"
    if metadata.get("legacy_sales_imported") != "false":
        return "unsafe_legacy_mixing"
    return None


def _currency_verification_sql(
    connection: sqlite3.Connection, table: str, *, alias: str = "e"
) -> str:
    columns = _table_columns(connection, table)
    if "currency_verified" in columns:
        return f"COALESCE({alias}.currency_verified, 0)"
    # Schema-v1 databases created before the explicit verification flag use
    # the ISO user-assigned code UNK for an undocumented currency contract.
    return (
        f"CASE WHEN UPPER(TRIM(COALESCE({alias}.currency, ''))) "
        "IN ('', 'UNK', 'UNKNOWN') THEN 0 ELSE 1 END"
    )


def _native_unit_sql(
    connection: sqlite3.Connection, table: str, *, alias: str = "e"
) -> str:
    columns = _table_columns(connection, table)
    if "native_unit_label" in columns:
        return f"NULLIF(TRIM({alias}.native_unit_label), '')"
    return "'provider minor units'"


def _source_datasets(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Expose the registry only; never open a referenced legacy database."""

    columns = _table_columns(connection, "source_datasets")
    count_column = next(
        (
            column
            for column in ("row_count", "observed_rows", "registered_rows")
            if column in columns
        ),
        None,
    )
    count_sql = count_column if count_column else "NULL"
    rows = connection.execute(
        f"""
        SELECT dataset_code, marketplace_code, provider_code, native_currency,
               model_training_eligible, {count_sql} AS row_count
        FROM source_datasets
        ORDER BY dataset_code
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        dataset_code = str(row["dataset_code"])
        result.append(
            {
                "dataset_code": dataset_code,
                "marketplace_code": str(row["marketplace_code"]),
                "provider_code": str(row["provider_code"]),
                "native_currency": str(row["native_currency"]),
                "row_count": _safe_integer(row["row_count"]),
                "model_training_eligible": bool(row["model_training_eligible"]),
                "registry_only": True,
                "available_in_current_skin_detail": (
                    dataset_code == "legacy_csfloat_sales"
                ),
            }
        )
    return result


def build_market_history_status(
    path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Return a small read-only status without leaking raw provider material."""

    if not _is_existing_file(path):
        return _unavailable_status(path, "database_missing")
    try:
        with _read_only_connection(path) as connection:
            if connection is None:
                return _unavailable_status(path, "database_missing")
            reason = _storage_schema_reason(connection)
            if reason:
                return _unavailable_status(path, reason)
            datasets = _source_datasets(connection)
            sources: list[dict[str, Any]] = []
            counts: dict[str, int] = {}
            latest_observed: str | None = None
            fx_eligible = 0
            fx_covered = 0
            for kind, spec in _KIND_SPECS.items():
                table = str(spec["table"])
                verified = _currency_verification_sql(connection, table)
                aggregate = connection.execute(
                    f"""
                    SELECT COUNT(*) AS event_count,
                           MAX(provider_observed_at) AS latest_observed_at,
                           COUNT(*) AS fx_eligible,
                           SUM(CASE WHEN ({verified}) = 1
                                         AND price_usd_micros IS NOT NULL
                                    THEN 1 ELSE 0 END) AS fx_covered
                    FROM {table} AS e
                    WHERE e.event_kind=? AND e.granularity=?
                    """,
                    (spec["stored_kind"], spec["granularity"]),
                ).fetchone()
                counts[kind] = int(aggregate["event_count"] or 0)
                observed = str(aggregate["latest_observed_at"] or "") or None
                if observed and (latest_observed is None or observed > latest_observed):
                    latest_observed = observed
                fx_eligible += int(aggregate["fx_eligible"] or 0)
                fx_covered += int(aggregate["fx_covered"] or 0)
                source_rows = connection.execute(
                    f"""
                    SELECT marketplace_code, provider_code, currency,
                           ({verified}) AS currency_verified,
                           COUNT(*) AS event_count,
                           MAX(source_event_at) AS latest_event_at
                    FROM {table} AS e
                    WHERE e.event_kind=? AND e.granularity=?
                    GROUP BY marketplace_code, provider_code, currency,
                             currency_verified
                    ORDER BY marketplace_code, provider_code, currency
                    """,
                    (spec["stored_kind"], spec["granularity"]),
                ).fetchall()
                for row in source_rows:
                    verified_value = bool(row["currency_verified"])
                    currency = str(row["currency"] or "").upper()
                    sources.append(
                        {
                            "kind": kind,
                            "marketplace_code": str(row["marketplace_code"]),
                            "provider_code": str(row["provider_code"]),
                            "native_currency": currency if verified_value else None,
                            "currency_verified": verified_value,
                            "event_count": int(row["event_count"] or 0),
                            "latest_event_at": row["latest_event_at"],
                        }
                    )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return _unavailable_status(path, "database_unavailable")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "reason": None,
        "database": {"name": _public_db_label(path), "available": True},
        "counts": counts,
        "latest_observed_at": latest_observed,
        "sources": sources,
        "datasets": datasets,
        "fx": {
            "eligible": fx_eligible,
            "covered": fx_covered,
            "coverage": fx_covered / fx_eligible if fx_eligible else None,
        },
    }


def _normalize_price(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    minor = _safe_integer(value.get("amount_minor"))
    raw_currency = str(value.get("currency") or "").strip().upper()
    verified = bool(value.get("currency_verified"))
    exponent = _safe_integer(value.get("exponent"))
    if minor is None or minor < 0:
        return None
    if exponent is None or not 0 <= exponent <= 6:
        return None
    if verified and re.fullmatch(r"[A-Z]{3}", raw_currency) is None:
        return None
    if not verified and raw_currency not in _UNKNOWN_CURRENCIES:
        # An unverified provider label is not upgraded to an ISO currency just
        # because it happens to contain three letters.
        raw_currency = ""
    unit_label = str(value.get("unit_label") or "provider minor units").strip()
    if not unit_label or len(unit_label) > 80 or any(ord(char) < 32 for char in unit_label):
        unit_label = "provider minor units"
    return {
        "amount_minor": minor,
        "currency": raw_currency if verified else None,
        "currency_verified": verified,
        "exponent": exponent,
        "unit_label": unit_label if not verified else None,
    }


def _normalize_usd(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    micros = _safe_integer(value.get("amount_micros"))
    cents = _safe_integer(value.get("amount_cents"))
    if micros is None or micros < 0:
        return None
    if cents is None or cents < 0:
        cents = (micros + 5_000) // 10_000
    return {"amount_micros": micros, "amount_cents": cents, "currency": "USD"}


def _normalize_fx(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    provider = str(value.get("provider_code") or value.get("provider") or "").strip()
    effective_date = str(value.get("effective_date") or "").strip()
    base = str(value.get("base_currency") or "").strip().upper()
    quote = str(value.get("quote_currency") or "").strip().upper()
    if not provider or not effective_date or not base or quote != "USD":
        return None
    return {
        "provider_code": provider,
        "source_code": str(value.get("source_code") or "").strip() or None,
        "effective_date": effective_date,
        "base_currency": base,
        "quote_currency": quote,
        "rate_to_usd": str(value.get("rate_to_usd") or "").strip() or None,
        "policy": str(value.get("policy") or "").strip() or None,
    }


def _normalize_event(value: Any, *, expected_kind: str) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    kind = str(value.get("kind") or value.get("event_kind") or "")
    if kind != expected_kind:
        return None
    marketplace = str(value.get("marketplace_code") or "").strip().casefold()
    provider = str(value.get("provider_code") or "").strip().casefold()
    name = str(value.get("market_hash_name") or "").strip()
    event_at = str(value.get("event_at") or "").strip()
    native_price = _normalize_price(value.get("native_price"))
    if not marketplace or not provider or not name or not event_at or native_price is None:
        return None
    normalized_usd = (
        _normalize_usd(value.get("normalized_usd"))
        if native_price["currency_verified"]
        else None
    )
    normalized_fx = (
        _normalize_fx(value.get("fx"))
        if native_price["currency_verified"]
        else None
    )
    timestamp_assumption = str(value.get("timestamp_assumption") or "").strip()
    if (
        not timestamp_assumption
        or len(timestamp_assumption) > 160
        or any(ord(character) < 32 for character in timestamp_assumption)
    ):
        timestamp_assumption = "unverified"
    return {
        "event_id": str(value.get("event_id") or "").strip() or None,
        "kind": kind,
        "marketplace_code": marketplace,
        "provider_code": provider,
        "market_hash_name": name,
        "event_at": event_at,
        "observed_at": str(value.get("observed_at") or "").strip() or None,
        "timestamp_assumption": timestamp_assumption,
        "native_price": native_price,
        "normalized_usd": normalized_usd,
        "fx": normalized_fx,
        "float_value": _finite_number(value.get("float_value")),
        "paint_seed": _safe_integer(value.get("paint_seed")),
        "paint_index": _safe_integer(value.get("paint_index")),
        "listing_count": _safe_integer(value.get("listing_count")),
        "liquidity": _finite_number(value.get("liquidity")),
    }


def _normalize_series(value: Any, *, expected_kind: str) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    kind = str(value.get("kind") or value.get("event_kind") or "")
    if kind != expected_kind:
        return None
    marketplace = str(value.get("marketplace_code") or "").strip().casefold()
    provider = str(value.get("provider_code") or "").strip().casefold()
    currency = str(value.get("native_currency") or "").strip().upper()
    currency_verified = bool(value.get("currency_verified"))
    exponent = _safe_integer(value.get("native_exponent"))
    if (
        not marketplace
        or not provider
        or exponent is None
        or not 0 <= exponent <= 6
    ):
        return None
    if currency_verified and re.fullmatch(r"[A-Z]{3}", currency) is None:
        return None
    if not currency_verified:
        currency = ""
    raw_points = value.get("points") if isinstance(value.get("points"), list) else []
    points: list[dict[str, Any]] = []
    for point in raw_points:
        if not isinstance(point, Mapping):
            continue
        day = str(point.get("day") or "").strip()
        count = _safe_integer(point.get("event_count"))
        native = _finite_number(point.get("average_native_minor"))
        usd = _finite_number(point.get("average_usd_micros"))
        eligible = max(0, _safe_integer(point.get("fx_eligible")) or 0)
        covered = max(0, min(eligible, _safe_integer(point.get("fx_covered")) or 0))
        if not day or count is None or count < 0 or native is None or native < 0:
            continue
        points.append(
            {
                "day": day,
                "event_count": count,
                "average_native_minor": native,
                "average_usd_micros": (
                    usd
                    if currency_verified and usd is not None and usd >= 0
                    else None
                ),
                "fx_eligible": eligible,
                "fx_covered": covered,
            }
        )
    return {
        "kind": kind,
        "marketplace_code": marketplace,
        "provider_code": provider,
        "native_currency": currency if currency_verified else None,
        "currency_verified": currency_verified,
        "native_unit_label": (
            str(value.get("native_unit_label") or "provider minor units")
            if not currency_verified
            else None
        ),
        "native_exponent": exponent,
        "points": points,
    }


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _where_sql(
    spec: Mapping[str, str],
    *,
    cutoff: str,
    search: str,
    marketplace: str = "",
    provider: str = "",
) -> tuple[str, list[Any]]:
    clauses = [
        "e.event_kind=?",
        "e.granularity=?",
        "e.source_event_at>=?",
    ]
    parameters: list[Any] = [spec["stored_kind"], spec["granularity"], cutoff]
    if search:
        clauses.append("e.market_hash_name LIKE ? ESCAPE '\\' COLLATE NOCASE")
        parameters.append(_like_pattern(search))
    if marketplace:
        clauses.append("e.marketplace_code=?")
        parameters.append(marketplace)
    if provider:
        clauses.append("e.provider_code=?")
        parameters.append(provider)
    return " AND ".join(clauses), parameters


def _event_rows(
    connection: sqlite3.Connection,
    *,
    kind: str,
    where_sql: str,
    parameters: list[Any],
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    spec = _KIND_SPECS[kind]
    table = spec["table"]
    identifier = spec["id"]
    verified = _currency_verification_sql(connection, table)
    unit_label = _native_unit_sql(connection, table)
    if kind == "confirmed_sale":
        float_sql = "e.float_value"
        paint_seed_sql = "e.paint_seed"
        paint_index_sql = "e.paint_index"
        listing_count_sql = "NULL"
        liquidity_sql = "NULL"
        timestamp_assumption_sql = "e.timestamp_assumption"
    else:
        float_sql = "NULL"
        paint_seed_sql = "NULL"
        paint_index_sql = "NULL"
        listing_count_sql = "e.listing_count"
        liquidity_sql = "e.liquidity"
        timestamp_assumption_sql = "'UTC_verified:cspriceapi_snapshot_v1'"
    rows = connection.execute(
        f"""
        SELECT e.{identifier} AS record_id,
               e.marketplace_code, e.provider_code, e.market_hash_name,
               e.native_price_minor, e.currency, e.minor_scale,
               ({verified}) AS currency_verified,
               {unit_label} AS native_unit_label,
               CASE WHEN ({verified})=1 THEN e.price_usd_micros END
                   AS price_usd_micros,
               CASE WHEN ({verified})=1 THEN e.price_usd_cents END
                   AS price_usd_cents,
               e.source_event_at, e.provider_observed_at,
               {timestamp_assumption_sql} AS timestamp_assumption,
               {float_sql} AS float_value,
               {paint_seed_sql} AS paint_seed,
               {paint_index_sql} AS paint_index,
               {listing_count_sql} AS listing_count,
               {liquidity_sql} AS liquidity,
               CASE WHEN ({verified})=1 THEN fx.provider_code END
                   AS fx_provider_code,
               CASE WHEN ({verified})=1 THEN fx.source_code END
                   AS fx_source_code,
               CASE WHEN ({verified})=1 THEN fx.base_currency END
                   AS fx_base_currency,
               CASE WHEN ({verified})=1 THEN fx.quote_currency END
                   AS fx_quote_currency,
               CASE WHEN ({verified})=1 THEN fx.rate_date END AS fx_rate_date,
               CASE WHEN ({verified})=1 THEN fx.rate_to_usd END AS fx_rate_to_usd
        FROM {table} AS e
        LEFT JOIN fx_rates AS fx ON fx.fx_rate_id=e.fx_rate_id
        WHERE {where_sql}
        ORDER BY e.source_event_at DESC, e.{identifier} DESC
        LIMIT ? OFFSET ?
        """,
        (*parameters, limit, offset),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        currency_verified = bool(row["currency_verified"])
        native_price = {
            "amount_minor": row["native_price_minor"],
            "currency": row["currency"] if currency_verified else None,
            "currency_verified": currency_verified,
            "exponent": row["minor_scale"],
            "unit_label": row["native_unit_label"],
        }
        normalized_usd = (
            {
                "amount_micros": row["price_usd_micros"],
                "amount_cents": row["price_usd_cents"],
            }
            if currency_verified and row["price_usd_micros"] is not None
            else None
        )
        fx = (
            {
                "provider_code": row["fx_provider_code"],
                "source_code": row["fx_source_code"],
                "effective_date": row["fx_rate_date"],
                "base_currency": row["fx_base_currency"],
                "quote_currency": row["fx_quote_currency"],
                "rate_to_usd": row["fx_rate_to_usd"],
                "policy": "on_or_before_source_event_v1",
            }
            if currency_verified
            and row["fx_provider_code"]
            and row["fx_rate_date"]
            else None
        )
        result.append(
            {
                "event_id": f"{kind}:{row['record_id']}",
                "kind": kind,
                "marketplace_code": row["marketplace_code"],
                "provider_code": row["provider_code"],
                "market_hash_name": row["market_hash_name"],
                "event_at": row["source_event_at"],
                "observed_at": row["provider_observed_at"],
                "timestamp_assumption": row["timestamp_assumption"],
                "native_price": native_price,
                "normalized_usd": normalized_usd,
                "fx": fx,
                "float_value": row["float_value"],
                "paint_seed": row["paint_seed"],
                "paint_index": row["paint_index"],
                "listing_count": row["listing_count"],
                "liquidity": row["liquidity"],
            }
        )
    return result


def _event_series(
    connection: sqlite3.Connection,
    *,
    kind: str,
    where_sql: str,
    parameters: list[Any],
) -> list[dict[str, Any]]:
    spec = _KIND_SPECS[kind]
    table = spec["table"]
    verified = _currency_verification_sql(connection, table)
    unit_label = _native_unit_sql(connection, table)
    rows = connection.execute(
        f"""
        SELECT e.marketplace_code, e.provider_code, e.currency, e.minor_scale,
               ({verified}) AS currency_verified,
               {unit_label} AS native_unit_label,
               SUBSTR(e.source_event_at, 1, 10) AS day,
               COUNT(*) AS event_count,
               AVG(e.native_price_minor) AS average_native_minor,
               AVG(CASE WHEN ({verified})=1 THEN e.price_usd_micros END)
                   AS average_usd_micros,
               COUNT(*) AS fx_eligible,
               SUM(CASE WHEN ({verified})=1 AND e.price_usd_micros IS NOT NULL
                        THEN 1 ELSE 0 END) AS fx_covered
        FROM {table} AS e
        WHERE {where_sql}
        GROUP BY e.marketplace_code, e.provider_code, e.currency, e.minor_scale,
                 currency_verified, native_unit_label, day
        ORDER BY e.marketplace_code, e.provider_code, e.currency,
                 e.minor_scale, day
        """,
        parameters,
    ).fetchall()
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        currency_verified = bool(row["currency_verified"])
        key = (
            str(row["marketplace_code"]),
            str(row["provider_code"]),
            str(row["currency"] if currency_verified else ""),
            int(row["minor_scale"]),
            currency_verified,
            str(row["native_unit_label"] or "provider minor units"),
        )
        group = grouped.setdefault(
            key,
            {
                "kind": kind,
                "marketplace_code": key[0],
                "provider_code": key[1],
                "native_currency": key[2] if currency_verified else None,
                "currency_verified": currency_verified,
                "native_exponent": key[3],
                "native_unit_label": key[5] if not currency_verified else None,
                "points": [],
            },
        )
        group["points"].append(
            {
                "day": row["day"],
                "event_count": row["event_count"],
                "average_native_minor": row["average_native_minor"],
                "average_usd_micros": (
                    row["average_usd_micros"] if currency_verified else None
                ),
                "fx_eligible": row["fx_eligible"],
                "fx_covered": row["fx_covered"],
            }
        )
    return list(grouped.values())


def _read_dashboard_history(
    connection: sqlite3.Connection,
    *,
    kind: str,
    search: str,
    marketplace: str,
    provider: str,
    days: int,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    spec = _KIND_SPECS[kind]
    cutoff_day = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    cutoff = f"{cutoff_day}T00:00:00Z"
    base_where, base_parameters = _where_sql(
        spec, cutoff=cutoff, search=search
    )
    where, parameters = _where_sql(
        spec,
        cutoff=cutoff,
        search=search,
        marketplace=marketplace,
        provider=provider,
    )
    table = spec["table"]
    verified = _currency_verification_sql(connection, table)
    aggregate = connection.execute(
        f"""
        SELECT COUNT(*) AS event_count,
               MAX(e.source_event_at) AS latest_event_at,
               SUM(CASE WHEN ({verified})=1
                             AND e.price_usd_micros IS NOT NULL
                        THEN 1 ELSE 0 END) AS usd_covered
        FROM {table} AS e
        WHERE {where}
        """,
        parameters,
    ).fetchone()
    total = int(aggregate["event_count"] or 0)
    latest_event_at = (
        str(aggregate["latest_event_at"])
        if aggregate["latest_event_at"]
        else None
    )
    usd_covered = int(aggregate["usd_covered"] or 0)
    filtered_sources = connection.execute(
        f"""
        SELECT DISTINCT e.marketplace_code, e.provider_code
        FROM {table} AS e
        WHERE {where}
        """,
        parameters,
    ).fetchall()
    facets = connection.execute(
        f"""
        SELECT DISTINCT marketplace_code, provider_code
        FROM {table} AS e
        WHERE {base_where}
        ORDER BY marketplace_code, provider_code
        """,
        base_parameters,
    ).fetchall()
    rows = _event_rows(
        connection,
        kind=kind,
        where_sql=where,
        parameters=parameters,
        limit=limit,
        offset=offset,
    )
    distinct_names = [
        str(row[0])
        for row in connection.execute(
            f"""
            SELECT DISTINCT e.market_hash_name
            FROM {table} AS e
            WHERE {where}
            ORDER BY e.market_hash_name
            LIMIT 2
            """,
            parameters,
        ).fetchall()
    ]
    exact_series_name = distinct_names[0] if len(distinct_names) == 1 else None
    series = (
        _event_series(
            connection,
            kind=kind,
            where_sql=where,
            parameters=parameters,
        )
        if exact_series_name is not None
        else []
    )
    return {
        "status": "ready",
        "total": total,
        "latest_event_at": latest_event_at,
        "source_count": len(filtered_sources),
        "usd_eligible": total,
        "usd_covered": usd_covered,
        "marketplaces": sorted({str(row["marketplace_code"]) for row in facets}),
        "providers": sorted({str(row["provider_code"]) for row in facets}),
        "rows": rows,
        "series": series,
        "series_scope": (
            "exact_name"
            if exact_series_name
            else "no_matches" if not distinct_names else "multiple_names"
        ),
        "series_market_hash_name": exact_series_name,
    }


def build_market_history_payload(
    path: str | Path = DEFAULT_DB_PATH,
    *,
    kind: str = "confirmed_sale",
    search: str = "",
    marketplace: str = "",
    provider: str = "",
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Return one strictly single-kind market-history page.

    Backend rows that claim a different event kind or omit explicit native
    currency/source provenance are discarded.  If the storage backend cannot
    prove its schema, the entire response fails closed rather than returning a
    plausible-looking partial history.
    """

    normalized_kind = _event_kind(kind)
    normalized_search = _filter_text(search, name="search")
    normalized_marketplace = _source_code(marketplace, name="marketplace")
    normalized_provider = _source_code(provider, name="provider")
    normalized_days = _bounded_int(
        days, name="days", default=DEFAULT_DAYS, minimum=1, maximum=MAX_DAYS
    )
    normalized_limit = _bounded_int(
        limit, name="limit", default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT
    )
    normalized_offset = _bounded_int(
        offset, name="offset", default=0, minimum=0, maximum=MAX_OFFSET
    )
    unavailable_arguments = {
        "kind": normalized_kind,
        "search": normalized_search,
        "marketplace": normalized_marketplace,
        "provider": normalized_provider,
        "days": normalized_days,
        "limit": normalized_limit,
        "offset": normalized_offset,
    }
    if not _is_existing_file(path):
        return _unavailable_payload(
            path, reason="database_missing", **unavailable_arguments
        )
    try:
        with _read_only_connection(path) as connection:
            if connection is None:
                return _unavailable_payload(
                    path, reason="database_missing", **unavailable_arguments
                )
            reason = _storage_schema_reason(connection)
            if reason:
                return _unavailable_payload(
                    path, reason=reason, **unavailable_arguments
                )
            datasets = _source_datasets(connection)
            raw = _read_dashboard_history(
                connection,
                kind=normalized_kind,
                search=normalized_search,
                marketplace=normalized_marketplace,
                provider=normalized_provider,
                days=normalized_days,
                limit=normalized_limit,
                offset=normalized_offset,
            )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return _unavailable_payload(
            path, reason="database_unavailable", **unavailable_arguments
        )
    if not isinstance(raw, Mapping) or str(raw.get("status") or "") != "ready":
        return _unavailable_payload(
            path,
            reason=str(raw.get("reason") or "schema_unavailable")
            if isinstance(raw, Mapping)
            else "schema_unavailable",
            **unavailable_arguments,
        )

    raw_rows = raw.get("rows") if isinstance(raw.get("rows"), list) else []
    rows = [
        event
        for value in raw_rows
        if (event := _normalize_event(value, expected_kind=normalized_kind)) is not None
    ]
    raw_series = raw.get("series") if isinstance(raw.get("series"), list) else []
    series = [
        group
        for value in raw_series
        if (group := _normalize_series(value, expected_kind=normalized_kind)) is not None
    ]
    raw_total = _safe_integer(raw.get("total"))
    if raw_total is None or raw_total < len(rows):
        return _unavailable_payload(
            path, reason="invalid_backend_contract", **unavailable_arguments
        )
    marketplaces = sorted(
        {
            str(value).strip().casefold()
            for value in (raw.get("marketplaces") or [])
            if _SOURCE_CODE_RE.fullmatch(str(value).strip().casefold())
        }
    )
    providers = sorted(
        {
            str(value).strip().casefold()
            for value in (raw.get("providers") or [])
            if _SOURCE_CODE_RE.fullmatch(str(value).strip().casefold())
        }
    )
    fx_total = _safe_integer(raw.get("usd_eligible"))
    fx_covered = _safe_integer(raw.get("usd_covered"))
    source_count = _safe_integer(raw.get("source_count"))
    if (
        fx_total is None
        or fx_total < 0
        or fx_covered is None
        or not 0 <= fx_covered <= fx_total
        or source_count is None
        or source_count < 0
    ):
        return _unavailable_payload(
            path, reason="invalid_backend_contract", **unavailable_arguments
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "reason": None,
        "kind": normalized_kind,
        "truth": {
            "mixed_event_kinds": False,
            "price_meaning": (
                "executed_sale_price"
                if normalized_kind == "confirmed_sale"
                else "observed_listing_ask"
            ),
            "series_scope": raw.get("series_scope"),
            "series_market_hash_name": raw.get("series_market_hash_name"),
        },
        "filters": {
            "search": normalized_search,
            "marketplace": normalized_marketplace,
            "provider": normalized_provider,
            "days": normalized_days,
        },
        "pagination": {
            "limit": normalized_limit,
            "offset": normalized_offset,
            "total": raw_total,
            "has_more": normalized_offset + len(rows) < raw_total,
        },
        "summary": {
            "event_count": raw_total,
            "shown_count": len(rows),
            "source_count": source_count,
            "latest_event_at": raw.get("latest_event_at"),
            "usd_coverage": fx_covered / fx_total if fx_total else None,
        },
        "facets": {"marketplaces": marketplaces, "providers": providers},
        "series": series,
        "rows": rows,
        "datasets": datasets,
        "database": {"name": _public_db_label(path), "available": True},
    }


MARKET_HISTORY_HTML = r"""<!doctype html>
<html lang="__LOCALE__">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>История рынков · CS2 Market Lab</title>
  <style>
    :root{--bg:#080b12;--panel:#111722;--panel2:#171f2c;--line:#293549;--text:#f4f7fc;--muted:#9aa9be;--accent:#8068ff;--good:#45d69e;--warn:#efb85b;--bad:#ff7285;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(900px 500px at 0 0,rgba(128,104,255,.18),transparent 65%),var(--bg);color:var(--text)}button,input,select{font:inherit}button{cursor:pointer}a{color:inherit}.wrap{width:min(1420px,calc(100% - 32px));margin:auto}header{position:sticky;top:0;z-index:10;border-bottom:1px solid var(--line);background:rgba(8,11,18,.9);backdrop-filter:blur(16px)}.top{min-height:68px;display:flex;align-items:center;gap:20px}.brand{text-decoration:none;font-weight:850}.nav{display:flex;gap:6px;overflow:auto}.nav a{padding:9px 12px;border-radius:10px;color:var(--muted);text-decoration:none;font-size:12px;font-weight:750;white-space:nowrap}.nav a:hover,.nav a.active{background:rgba(128,104,255,.15);color:var(--text)}.locale{margin-left:auto;padding:7px;border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--text)}main{padding:32px 0 70px}.hero{padding:28px;border:1px solid var(--line);border-radius:23px;background:linear-gradient(145deg,rgba(128,104,255,.1),var(--panel))}.eyebrow{color:var(--good);font-size:11px;font-weight:850;text-transform:uppercase;letter-spacing:.12em}.hero h1{margin:9px 0 8px;font-size:clamp(30px,5vw,52px);letter-spacing:-.045em}.hero p{max-width:920px;margin:0;color:var(--muted);line-height:1.65}.truth{margin-top:17px;padding:14px 16px;border:1px solid rgba(239,184,91,.42);border-radius:14px;background:rgba(239,184,91,.08);color:#ffd998;line-height:1.5}.truth strong{color:#fff}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}.metric{padding:16px;border:1px solid var(--line);border-radius:16px;background:var(--panel)}.metric span{display:block;color:var(--muted);font-size:11px}.metric strong{display:block;margin-top:7px;font-size:24px}.panel{overflow:hidden;border:1px solid var(--line);border-radius:20px;background:var(--panel)}.filters{display:grid;grid-template-columns:2fr repeat(4,minmax(135px,1fr)) auto;gap:9px;padding:17px;border-bottom:1px solid var(--line)}.field{display:grid;gap:5px}.field span{color:var(--muted);font-size:10px}.field input,.field select,.apply{min-height:42px;border:1px solid var(--line);border-radius:10px;background:var(--panel2);color:var(--text);padding:9px 11px}.apply{align-self:end;background:var(--accent);border-color:transparent;font-weight:800}.section{padding:18px;border-bottom:1px solid var(--line)}.section:last-child{border:0}.section-head{display:flex;align-items:end;justify-content:space-between;gap:12px;margin-bottom:14px}.section-head h2{margin:0;font-size:20px}.section-head p{margin:4px 0 0;color:var(--muted);font-size:12px}.source-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.source-card{min-width:0;padding:16px;border:1px solid var(--line);border-radius:16px;background:var(--panel2)}.source-title{display:flex;align-items:center;justify-content:space-between;gap:10px}.source-title h3{margin:0;font-size:15px}.badge{padding:5px 8px;border-radius:999px;background:rgba(128,104,255,.18);color:#cfc7ff;font-size:10px;font-weight:850;text-transform:uppercase;letter-spacing:.06em}.source-meta{margin:7px 0 12px;color:var(--muted);font-size:11px}.chart{min-height:245px}.chart svg{display:block;width:100%;height:auto}.grid-line{stroke:#293549;stroke-width:1}.chart-line{fill:none;stroke:var(--good);stroke-width:2.3;stroke-linecap:round;stroke-linejoin:round}.chart-area{fill:rgba(69,214,158,.1)}.chart-label{fill:var(--muted);font-size:10px}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:14px}table{width:100%;border-collapse:collapse;min-width:1000px}th,td{padding:11px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:12px}th{position:sticky;top:0;background:var(--panel2);color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em}tbody tr:last-child td{border-bottom:0}.item-name{font-weight:750}.subtle{display:block;margin-top:4px;color:var(--muted);font-size:10px}.native{font-weight:800}.usd{display:block;margin-top:3px;color:var(--good)}.empty,.error,.loading{padding:32px;text-align:center;color:var(--muted)}.error{color:#ff9daa}.pager{display:flex;justify-content:center;align-items:center;gap:12px;margin-top:15px}.pager button{padding:9px 13px;border:1px solid var(--line);border-radius:10px;background:var(--panel2);color:var(--text)}.pager button:disabled{opacity:.4;cursor:default}@media(max-width:1000px){.metrics{grid-template-columns:repeat(2,1fr)}.filters{grid-template-columns:repeat(2,1fr)}.field:first-child{grid-column:1/-1}.apply{grid-column:1/-1}.source-grid{grid-template-columns:1fr}}@media(max-width:620px){.wrap{width:min(100% - 20px,1420px)}.top{gap:10px}.brand{display:none}.metrics{grid-template-columns:1fr}.filters{grid-template-columns:1fr}.field:first-child,.apply{grid-column:auto}.hero{padding:20px}}
  </style>
</head>
<body>
  <header><div class="wrap top"><a class="brand" href="/">CS2 Market Lab</a><nav class="nav" aria-label="Primary"><a href="/" data-i18n="overview">Обзор</a><a href="/catalog" data-i18n="catalog">Каталог</a><a href="/knives" data-i18n="knives">Ножи</a><a class="active" aria-current="page" href="/market-history" data-i18n="history">История рынков</a><a href="/account" data-i18n="inventory">Инвентарь</a></nav><select class="locale" id="locale" aria-label="Language"><option value="ru">RU</option><option value="en">EN</option></select></div></header>
  <main class="wrap">
    <section class="hero"><div class="eyebrow" data-i18n="eyebrow">проверяемое происхождение цен</div><h1 data-i18n="title">История продаж по каждому рынку.</h1><p data-i18n="lead">Исходная цена сохраняется в валюте площадки. Долларовая оценка показывается только вместе с зафиксированным курсом.</p><div class="truth" id="truth" role="note"><strong data-i18n="truthTitle">Два разных факта.</strong> <span data-i18n="truthSale">Состоявшаяся продажа — исполненная цена.</span> <span data-i18n="truthListing">Снимок листинга — лишь цена предложения. Они никогда не смешиваются в одном графике или показателе.</span></div></section>
    <section class="metrics" aria-label="Summary"><article class="metric"><span data-i18n="events">Событий</span><strong id="eventCount">—</strong></article><article class="metric"><span data-i18n="sources">Источников</span><strong id="sourceCount">—</strong></article><article class="metric"><span data-i18n="fxCoverage">Покрытие USD</span><strong id="fxCoverage">—</strong></article><article class="metric"><span data-i18n="latest">Последнее событие</span><strong id="latestEvent">—</strong></article></section>
    <section class="panel">
      <form class="filters" id="filters"><label class="field"><span data-i18n="search">Поиск скина</span><input id="search" name="search" maxlength="180" autocomplete="off"></label><label class="field"><span data-i18n="kind">Тип факта</span><select id="kind" name="kind"><option value="confirmed_sale" data-i18n="confirmedSale">Состоявшиеся продажи</option><option value="listing_snapshot" data-i18n="listingSnapshot">Снимки листингов</option></select></label><label class="field"><span data-i18n="marketplace">Площадка</span><select id="marketplace" name="marketplace"><option value="" data-i18n="all">Все</option></select></label><label class="field"><span data-i18n="provider">Провайдер данных</span><select id="provider" name="provider"><option value="" data-i18n="all">Все</option></select></label><label class="field"><span data-i18n="days">Период, дней</span><input id="days" name="days" type="number" min="1" max="3650" value="90"></label><button class="apply" type="submit" data-i18n="apply">Показать</button></form>
      <section class="section"><div class="section-head"><div><h2 data-i18n="registeredDatasets">Зарегистрированные наборы</h2><p data-i18n="registryTruth">Реестр показывает происхождение данных, но сам по себе не добавляет их в этот график.</p></div></div><div class="source-grid" id="datasets"></div></section>
      <section class="section"><div class="section-head"><div><h2 data-i18n="charts">Графики по источникам</h2><p id="chartTruth" data-i18n="saleChartTruth">Только исполненные цены состоявшихся продаж.</p></div></div><div class="source-grid" id="series"><div class="loading" data-i18n="loading">Загрузка…</div></div></section>
      <section class="section"><div class="section-head"><div><h2 data-i18n="eventsTable">Последние события</h2><p data-i18n="nativeTruth">Исходная цена не заменяется пересчитанной долларовой.</p></div></div><div class="table-wrap"><table><thead><tr><th data-i18n="time">Время</th><th data-i18n="item">Предмет</th><th data-i18n="fact">Факт</th><th data-i18n="source">Источник</th><th data-i18n="nativePrice">Цена площадки</th><th data-i18n="usdEstimate">Оценка USD</th><th data-i18n="fx">Курс</th><th>Float / Seed</th></tr></thead><tbody id="rows"></tbody></table></div><div class="pager"><button id="previous" type="button" data-i18n="previous">Назад</button><span id="page">—</span><button id="next" type="button" data-i18n="next">Дальше</button></div></section>
    </section>
  </main>
  <script>
  (()=>{'use strict';
    const $=selector=>document.querySelector(selector);
    const copy={
      ru:{documentTitle:'История рынков · CS2 Market Lab',overview:'Обзор',catalog:'Каталог',knives:'Ножи',history:'История рынков',inventory:'Инвентарь',eyebrow:'проверяемое происхождение цен',title:'История продаж по каждому рынку.',lead:'Исходная цена сохраняется в валюте площадки. Долларовая оценка показывается только вместе с зафиксированным курсом.',truthTitle:'Два разных факта.',truthSale:'Состоявшаяся продажа — исполненная цена.',truthListing:'Снимок листинга — лишь цена предложения. Они никогда не смешиваются в одном графике или показателе.',events:'Событий',sources:'Источников',fxCoverage:'Покрытие USD',latest:'Последнее событие',search:'Поиск скина',kind:'Тип факта',confirmedSale:'Состоявшиеся продажи',listingSnapshot:'Снимки листингов',marketplace:'Площадка',provider:'Провайдер данных',days:'Период, дней',all:'Все',apply:'Показать',registeredDatasets:'Зарегистрированные наборы',registryTruth:'Реестр показывает происхождение данных, но сам по себе не добавляет их в этот график.',legacyDataset:'Существующий legacy-набор; доступен в текущей карточке скина и не смешивается с новой БД.',registryDataset:'Изолированный набор новой истории; обучение моделей отключено.',charts:'Графики по источникам',chooseExactSkin:'Введите точное название одного скина: цены разных предметов нельзя усреднять в одном графике.',saleChartTruth:'Только исполненные цены состоявшихся продаж.',listingChartTruth:'Только наблюдавшиеся цены предложений; это не доказательство продажи.',eventsTable:'Последние события',nativeTruth:'Исходная цена не заменяется пересчитанной долларовой.',time:'Время',item:'Предмет',fact:'Факт',source:'Источник',nativePrice:'Цена площадки',usdEstimate:'Оценка USD',fx:'Курс',previous:'Назад',next:'Дальше',loading:'Загрузка…',empty:'Для выбранного фильтра данных пока нет.',unavailable:'История рынков пока недоступна.',saleBadge:'исполненная продажа',listingBadge:'снимок предложения',eventsShort:'событий: {n}',nativeAxis:'исходные единицы',usdAxis:'нормализовано в USD',page:'Страница {n}',noFx:'нет подтверждённого FX',currencyUnverified:'валюта не подтверждена',timestampUnverified:'время источника не подтверждено'},
      en:{documentTitle:'Market history · CS2 Market Lab',overview:'Overview',catalog:'Catalogue',knives:'Knives',history:'Market history',inventory:'Inventory',eyebrow:'auditable price provenance',title:'Sale history for each marketplace.',lead:'The native marketplace amount is preserved. A USD estimate is shown only with a recorded exchange-rate provenance.',truthTitle:'Two different facts.',truthSale:'A confirmed sale is an executed price.',truthListing:'A listing snapshot is only an observed ask. They are never mixed in one chart or metric.',events:'Events',sources:'Sources',fxCoverage:'USD coverage',latest:'Latest event',search:'Skin search',kind:'Fact type',confirmedSale:'Confirmed sales',listingSnapshot:'Listing snapshots',marketplace:'Marketplace',provider:'Data provider',days:'Period, days',all:'All',apply:'Show',registeredDatasets:'Registered datasets',registryTruth:'The registry records provenance but does not add a dataset to this chart.',legacyDataset:'Existing legacy dataset; available in the current skin detail and never mixed into the new database.',registryDataset:'Isolated new-history dataset; model training is disabled.',charts:'Charts by source',chooseExactSkin:'Enter the exact name of one skin: prices of different items must not be averaged into one chart.',saleChartTruth:'Executed prices from confirmed sales only.',listingChartTruth:'Observed listing asks only; these do not prove that a sale occurred.',eventsTable:'Latest events',nativeTruth:'The native amount is never replaced by a converted dollar value.',time:'Time',item:'Item',fact:'Fact',source:'Source',nativePrice:'Marketplace price',usdEstimate:'USD estimate',fx:'FX provenance',previous:'Previous',next:'Next',loading:'Loading…',empty:'There is no data for these filters yet.',unavailable:'Market history is currently unavailable.',saleBadge:'executed sale',listingBadge:'listing observation',eventsShort:'events: {n}',nativeAxis:'native units',usdAxis:'normalized USD',page:'Page {n}',noFx:'no verified FX',currencyUnverified:'currency not verified',timestampUnverified:'source time not verified'}
    };
    let locale='__LOCALE__',payload=null,offset=0,controller=null,requestId=0;
    const tr=(key,values={})=>(copy[locale][key]||key).replace(/\{(\w+)\}/g,(_,name)=>String(values[name]??''));
    const clear=node=>node.replaceChildren();
    const hasNumber=value=>value!==null&&value!==undefined&&value!==''&&Number.isFinite(Number(value));
    const number=value=>hasNumber(value)?new Intl.NumberFormat(locale==='ru'?'ru-RU':'en-US').format(Number(value)):'—';
    const percent=value=>hasNumber(value)?new Intl.NumberFormat(locale==='ru'?'ru-RU':'en-US',{style:'percent',maximumFractionDigits:1}).format(Number(value)):'—';
    function minor(value,currency,exponent,verified=true,unitLabel=''){if(!hasNumber(value))return '—';const exp=Number.isInteger(Number(exponent))?Math.min(6,Math.max(0,Number(exponent))):2;const major=Number(value)/(10**exp);if(!verified)return `${major.toFixed(exp)} · ${tr('currencyUnverified')}`;try{return new Intl.NumberFormat(locale==='ru'?'ru-RU':'en-US',{style:'currency',currency:String(currency),minimumFractionDigits:exp,maximumFractionDigits:exp}).format(major);}catch(_){return `${String(currency||unitLabel||'')} ${major.toFixed(exp)}`.trim();}}
    const usd=value=>hasNumber(value)?new Intl.NumberFormat(locale==='ru'?'ru-RU':'en-US',{style:'currency',currency:'USD'}).format(Number(value)/1e6):'—';
    const when=value=>{if(!value)return '—';const parsed=new Date(value);return Number.isNaN(parsed.getTime())?String(value):parsed.toLocaleString(locale==='ru'?'ru-RU':'en-US',{timeZone:'Europe/Moscow'});};
    const cell=(row,value,className='')=>{const node=document.createElement('td');if(className)node.className=className;node.textContent=value;row.appendChild(node);return node;};
    function option(select,value){if(!value||Array.from(select.options).some(item=>item.value===value))return;const node=document.createElement('option');node.value=value;node.textContent=value;select.appendChild(node);}
    function fill(select,values,current){const first=select.firstElementChild;clear(select);select.appendChild(first);(values||[]).forEach(value=>option(select,value));select.value=current||'';}
    function kindLabel(kind){return tr(kind==='confirmed_sale'?'saleBadge':'listingBadge');}
    function renderSummary(data){$('#eventCount').textContent=number(data.summary?.event_count);$('#sourceCount').textContent=number(data.summary?.source_count);$('#fxCoverage').textContent=percent(data.summary?.usd_coverage);$('#latestEvent').textContent=when(data.summary?.latest_event_at);$('#chartTruth').textContent=tr(data.kind==='confirmed_sale'?'saleChartTruth':'listingChartTruth');}
    function svg(name,attributes={}){const node=document.createElementNS('http://www.w3.org/2000/svg',name);Object.entries(attributes).forEach(([key,value])=>node.setAttribute(key,String(value)));return node;}
    function drawChart(mount,group){const usableUsd=(group.points||[]).filter(point=>hasNumber(point.average_usd_micros));const useUsd=usableUsd.length>0;const points=(group.points||[]).filter(point=>point.day&&hasNumber(useUsd?point.average_usd_micros:point.average_native_minor));if(!points.length){const empty=document.createElement('div');empty.className='empty';empty.textContent=tr('empty');mount.appendChild(empty);return;}const width=640,height=230,pad={left:72,right:16,top:16,bottom:32};const values=points.map(point=>Number(useUsd?point.average_usd_micros:point.average_native_minor));let low=Math.min(...values),high=Math.max(...values);if(low===high){const spread=Math.max(Math.abs(low)*.02,1);low-=spread;high+=spread;}const x=index=>pad.left+(points.length===1?.5:index/(points.length-1))*(width-pad.left-pad.right);const y=value=>pad.top+(high-value)/(high-low)*(height-pad.top-pad.bottom);const coordinates=values.map((value,index)=>[x(index),y(value)]);const line=coordinates.map((point,index)=>`${index?'L':'M'}${point[0].toFixed(1)},${point[1].toFixed(1)}`).join(' ');const area=`${line} L${coordinates.at(-1)[0].toFixed(1)},${height-pad.bottom} L${coordinates[0][0].toFixed(1)},${height-pad.bottom} Z`;const image=svg('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':useUsd?tr('usdAxis'):tr('nativeAxis')});[0,.5,1].forEach(ratio=>{const yy=pad.top+ratio*(height-pad.top-pad.bottom),value=high-ratio*(high-low);image.appendChild(svg('line',{class:'grid-line',x1:pad.left,x2:width-pad.right,y1:yy,y2:yy}));const label=svg('text',{class:'chart-label',x:4,y:yy+4});label.textContent=useUsd?usd(value):minor(value,group.native_currency,group.native_exponent,group.currency_verified,group.native_unit_label);image.appendChild(label);});image.appendChild(svg('path',{class:'chart-area',d:area}));image.appendChild(svg('path',{class:'chart-line',d:line}));const first=svg('text',{class:'chart-label',x:pad.left,y:height-8});first.textContent=points[0].day;const last=svg('text',{class:'chart-label','text-anchor':'end',x:width-pad.right,y:height-8});last.textContent=points.at(-1).day;image.append(first,last);mount.appendChild(image);}
    function renderDatasets(data){const mount=$('#datasets');clear(mount);(data.datasets||[]).forEach(dataset=>{const card=document.createElement('article');card.className='source-card';const head=document.createElement('div');head.className='source-title';const title=document.createElement('h3');title.textContent=dataset.dataset_code;const badge=document.createElement('span');badge.className='badge';badge.textContent=`${dataset.marketplace_code} · ${dataset.provider_code}`;head.append(title,badge);const meta=document.createElement('div');meta.className='source-meta';meta.textContent=[dataset.native_currency,hasNumber(dataset.row_count)?tr('eventsShort',{n:number(dataset.row_count)}):null].filter(Boolean).join(' · ');const note=document.createElement('div');note.className='subtle';note.textContent=tr(dataset.available_in_current_skin_detail?'legacyDataset':'registryDataset');card.append(head,meta,note);mount.appendChild(card);});if(!mount.childElementCount){const empty=document.createElement('div');empty.className='empty';empty.textContent=tr(data.status==='ready'?'empty':'unavailable');mount.appendChild(empty);}}
    function renderSeries(data){const mount=$('#series');clear(mount);if(!data.series?.length){const empty=document.createElement('div');empty.className=data.status==='ready'?'empty':'error';empty.textContent=tr(data.status!=='ready'?'unavailable':data.truth?.series_scope==='multiple_names'?'chooseExactSkin':'empty');mount.appendChild(empty);return;}data.series.forEach(group=>{const card=document.createElement('article');card.className='source-card';const head=document.createElement('div');head.className='source-title';const title=document.createElement('h3');title.textContent=`${group.marketplace_code} · ${group.provider_code}`;const badge=document.createElement('span');badge.className='badge';badge.textContent=kindLabel(group.kind);head.append(title,badge);const count=(group.points||[]).reduce((sum,point)=>sum+Math.max(0,Number(point.event_count)||0),0);const meta=document.createElement('div');meta.className='source-meta';meta.textContent=`${group.currency_verified?group.native_currency:tr('currencyUnverified')} · ${tr('eventsShort',{n:number(count)})}`;const chart=document.createElement('div');chart.className='chart';card.append(head,meta,chart);mount.appendChild(card);drawChart(chart,group);});}
    function renderRows(data){const mount=$('#rows');clear(mount);if(!data.rows?.length){const row=document.createElement('tr');const value=cell(row,tr(data.status==='ready'?'empty':'unavailable'));value.colSpan=8;value.className=data.status==='ready'?'empty':'error';mount.appendChild(row);return;}data.rows.forEach(item=>{const row=document.createElement('tr');const time=cell(row,when(item.event_at));if(String(item.timestamp_assumption||'').includes('unverified')){const warning=document.createElement('span');warning.className='subtle';warning.textContent=tr('timestampUnverified');time.appendChild(warning);}const name=cell(row,item.market_hash_name,'item-name');const observed=document.createElement('span');observed.className='subtle';observed.textContent=item.observed_at?`${tr('latest')}: ${when(item.observed_at)}`:'';name.appendChild(observed);cell(row,kindLabel(item.kind));cell(row,`${item.marketplace_code} · ${item.provider_code}`);cell(row,minor(item.native_price?.amount_minor,item.native_price?.currency,item.native_price?.exponent,item.native_price?.currency_verified,item.native_price?.unit_label),'native');cell(row,item.normalized_usd?`≈ ${usd(item.normalized_usd.amount_micros)}`:'—','usd');const fx=item.fx?`${item.fx.provider_code} · ${item.fx.source_code||''} · ${item.fx.effective_date}`:tr('noFx');cell(row,fx);cell(row,`${hasNumber(item.float_value)?Number(item.float_value).toFixed(8):'—'} / ${item.paint_seed??'—'}`);mount.appendChild(row);});}
    function render(data){payload=data;renderSummary(data);fill($('#marketplace'),data.facets?.marketplaces,data.filters?.marketplace);fill($('#provider'),data.facets?.providers,data.filters?.provider);renderDatasets(data);renderSeries(data);renderRows(data);const limit=Number(data.pagination?.limit||100),currentOffset=Number(data.pagination?.offset||0);$('#page').textContent=tr('page',{n:Math.floor(currentOffset/limit)+1});$('#previous').disabled=currentOffset<=0;$('#next').disabled=!data.pagination?.has_more;}
    function query(){const params=new URLSearchParams();params.set('kind',$('#kind').value);params.set('days',$('#days').value||'90');params.set('limit','100');params.set('offset',String(offset));[['search','#search'],['marketplace','#marketplace'],['provider','#provider']].forEach(([key,selector])=>{const value=$(selector).value.trim();if(value)params.set(key,value);});return params;}
    async function load(reset=false){if(reset)offset=0;requestId+=1;const current=requestId;if(controller)controller.abort();controller=new AbortController();const params=query();history.replaceState(null,'',`${location.pathname}?${params}`);clear($('#series'));const loading=document.createElement('div');loading.className='loading';loading.textContent=tr('loading');$('#series').appendChild(loading);clear($('#rows'));try{const response=await fetch(`/api/market-history?${params}`,{credentials:'same-origin',headers:{Accept:'application/json'},signal:controller.signal});if(response.status===401){location.assign('/login?next=%2Fmarket-history');return;}const data=await response.json().catch(()=>({status:'unavailable'}));if(current!==requestId)return;if(!response.ok)throw new Error('unavailable');render(data);}catch(error){if(error?.name==='AbortError'||current!==requestId)return;render({status:'unavailable',kind:$('#kind').value,filters:{marketplace:$('#marketplace').value,provider:$('#provider').value},facets:{marketplaces:[],providers:[]},summary:{},pagination:{limit:100,offset,total:0,has_more:false},series:[],rows:[]});}}
    function applyLocale(){document.documentElement.lang=locale;document.title=tr('documentTitle');document.querySelectorAll('[data-i18n]').forEach(node=>{node.textContent=tr(node.dataset.i18n);});$('#locale').value=locale;if(payload)render(payload);}
    const initial=new URLSearchParams(location.search);$('#search').value=initial.get('search')||'';$('#kind').value=['confirmed_sale','listing_snapshot'].includes(initial.get('kind'))?initial.get('kind'):'confirmed_sale';$('#days').value=initial.get('days')||'90';offset=Math.max(0,Number(initial.get('offset'))||0);[['marketplace','#marketplace'],['provider','#provider']].forEach(([key,selector])=>{const value=initial.get(key)||'';if(value)option($(selector),value);$(selector).value=value;});
    $('#filters').addEventListener('submit',event=>{event.preventDefault();load(true);});$('#previous').addEventListener('click',()=>{offset=Math.max(0,offset-100);load();});$('#next').addEventListener('click',()=>{offset+=100;load();});$('#locale').addEventListener('change',event=>{locale=event.target.value==='en'?'en':'ru';document.cookie=`cs2_locale=${locale}; Path=/; SameSite=Lax`;applyLocale();});applyLocale();load();
  })();
  </script>
</body>
</html>"""


def render_market_history_page(locale: str = "ru") -> str:
    """Return the self-contained market-history page in Russian or English."""

    normalized = "en" if locale == "en" else "ru"
    rendered = MARKET_HISTORY_HTML.replace("__LOCALE__", normalized)
    if normalized == "en":
        rendered = rendered.replace(
            "<title>История рынков · CS2 Market Lab</title>",
            "<title>Market history · CS2 Market Lab</title>",
            1,
        )
    return rendered


__all__ = [
    "DEFAULT_DB_PATH",
    "EVENT_KINDS",
    "build_market_history_payload",
    "build_market_history_status",
    "render_market_history_page",
]
