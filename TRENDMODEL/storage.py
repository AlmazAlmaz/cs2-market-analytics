"""Persistent derived-data store for daily CS2 market aggregates."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
MODEL_VERSION = "trend_pooled_v2"
UNKNOWN_COLLECTION = "Unknown"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


@contextmanager
def open_source_snapshot(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open one WAL-aware, immutable-for-this-reader SQLite snapshot."""

    source_path = Path(path).expanduser().resolve()
    conn = sqlite3.connect(_readonly_uri(source_path), uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN")
        conn.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
        yield conn
    finally:
        conn.rollback()
        conn.close()


def connect_store(path: str | Path) -> sqlite3.Connection:
    store_path = Path(path).expanduser().resolve()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    initialize_store(conn)
    return conn


def initialize_store(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trend_daily_market (
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
        CREATE INDEX IF NOT EXISTS idx_trend_daily_day
            ON trend_daily_market(day, market_hash_name);

        CREATE TABLE IF NOT EXISTS trend_skin_catalog (
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
        CREATE INDEX IF NOT EXISTS idx_trend_skin_catalog_collection
            ON trend_skin_catalog(
                collection_name, observed_sales_count DESC, market_hash_name
            );

        CREATE TABLE IF NOT EXISTS trend_collection_daily (
            collection_name TEXT NOT NULL,
            day TEXT NOT NULL,
            index_value REAL NOT NULL,
            sales_count INTEGER NOT NULL,
            notional_cents INTEGER NOT NULL,
            member_count INTEGER NOT NULL,
            total_member_count INTEGER NOT NULL,
            member_coverage REAL NOT NULL,
            breadth_up REAL,
            comparable_member_count INTEGER NOT NULL,
            refreshed_at TEXT NOT NULL,
            PRIMARY KEY (collection_name, day)
        );
        CREATE INDEX IF NOT EXISTS idx_trend_collection_daily_day
            ON trend_collection_daily(day, collection_name);

        CREATE TABLE IF NOT EXISTS trend_store_meta (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trend_forecasts (
            as_of_day TEXT NOT NULL,
            market_hash_name TEXT NOT NULL,
            horizon_days INTEGER NOT NULL,
            generated_at TEXT NOT NULL,
            model_version TEXT NOT NULL,
            current_price_cents INTEGER NOT NULL,
            predicted_price_cents INTEGER NOT NULL,
            low_price_cents INTEGER NOT NULL,
            high_price_cents INTEGER NOT NULL,
            expected_return_percent REAL NOT NULL,
            downside_return_percent REAL NOT NULL,
            probability_up REAL NOT NULL,
            confidence REAL NOT NULL,
            history_rows INTEGER NOT NULL,
            last_observation_day TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (as_of_day, market_hash_name, horizon_days)
        );
        CREATE INDEX IF NOT EXISTS idx_trend_forecasts_latest
            ON trend_forecasts(
                as_of_day DESC, horizon_days, expected_return_percent DESC
            );
        CREATE INDEX IF NOT EXISTS idx_trend_forecasts_name
            ON trend_forecasts(market_hash_name, as_of_day DESC, horizon_days);

        CREATE TABLE IF NOT EXISTS trend_backtest_reports (
            report_id TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            model_version TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            evaluation_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            report_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trend_backtests_latest
            ON trend_backtest_reports(generated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_trend_backtests_config
            ON trend_backtest_reports(config_sha256, generated_at DESC);
        """
    )
    conn.commit()


def _require_source_contract(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='sales'"
    ).fetchone()
    if not table:
        raise ValueError("source database has no sales table")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sales)")}
    required = {"listing_id", "name", "price_cents", "state", "sold_at"}
    missing = sorted(required - columns)
    if missing:
        raise ValueError(f"sales table is missing columns: {missing}")


def _quantile(sorted_values: list[int], q: float) -> float:
    if not sorted_values:
        raise ValueError("cannot calculate a quantile of no values")
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    fraction = position - lower
    return float(
        sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction
    )


def _framed_hash_update(digest: Any, values: Iterable[Any]) -> None:
    for value in values:
        encoded = ("" if value is None else str(value)).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)


def _aggregate_row(
    name: str,
    day: str,
    prices: list[int],
    max_fetched_at: str | None,
    refreshed_at: str,
) -> tuple[Any, ...]:
    ordered = sorted(prices)
    return (
        name,
        day,
        len(ordered),
        float(statistics.median(ordered)),
        float(statistics.fmean(ordered)),
        ordered[0],
        ordered[-1],
        _quantile(ordered, 0.15),
        _quantile(ordered, 0.85),
        sum(ordered),
        max_fetched_at,
        refreshed_at,
    )


def _set_meta(conn: sqlite3.Connection, key: str, value: Any, updated_at: str) -> None:
    conn.execute(
        """
        INSERT INTO trend_store_meta(key, value_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value_json=excluded.value_json,
            updated_at=excluded.updated_at
        """,
        (key, canonical_json(value), updated_at),
    )


def _optional_source_expression(
    columns: set[str], column: str, *, default: str = "NULL"
) -> str:
    return column if column in columns else default


def _collection_source_expression(columns: set[str]) -> str:
    candidates = [
        f"NULLIF(TRIM({column}), '')"
        for column in ("item_collection_name", "item_collection")
        if column in columns
    ]
    if not candidates:
        return "NULL"
    if len(candidates) == 1:
        return candidates[0]
    return f"COALESCE({', '.join(candidates)})"


def _finite_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _build_collection_daily_rebuild(
    store: sqlite3.Connection,
    *,
    refreshed_at: str,
    insert_batch_size: int,
) -> int:
    """Build an equal-weight constituent index from each skin's own base price.

    A $1 skin and a $1,000 skin therefore both start at 100 and receive equal
    weight.  Only constituents with an observed sale on a day enter that day's
    value; ``member_coverage`` makes that sampling width explicit.
    """

    total_members = {
        str(row["collection_name"]): int(row["member_count"])
        for row in store.execute(
            """
            SELECT collection_name, COUNT(*) AS member_count
            FROM temp.trend_skin_catalog_rebuild
            GROUP BY collection_name
            """
        )
    }
    daily: dict[tuple[str, str], dict[str, float | int]] = {}
    current_name: str | None = None
    base_price = 0.0
    previous_price: float | None = None
    rows = store.execute(
        """
        SELECT c.collection_name,
               d.market_hash_name,
               d.day,
               d.sales_count,
               d.median_price_cents,
               d.notional_cents
        FROM temp.trend_daily_rebuild AS d
        JOIN temp.trend_skin_catalog_rebuild AS c
          ON c.market_hash_name=d.market_hash_name
        ORDER BY c.collection_name, d.market_hash_name, d.day
        """
    )
    for row in rows:
        name = str(row["market_hash_name"])
        price = float(row["median_price_cents"])
        if name != current_name:
            current_name = name
            base_price = price
            previous_price = None
        normalized = 100.0 * price / base_price
        collection = str(row["collection_name"])
        day = str(row["day"])
        value = daily.setdefault(
            (collection, day),
            {
                "normalized_sum": 0.0,
                "sales_count": 0,
                "notional_cents": 0,
                "member_count": 0,
                "comparable_member_count": 0,
                "up_member_count": 0,
            },
        )
        value["normalized_sum"] += normalized
        value["sales_count"] += int(row["sales_count"])
        value["notional_cents"] += int(row["notional_cents"])
        value["member_count"] += 1
        if previous_price is not None:
            value["comparable_member_count"] += 1
            if price > previous_price:
                value["up_member_count"] += 1
        previous_price = price

    inserted = 0
    batch: list[tuple[Any, ...]] = []
    insert_sql = (
        "INSERT INTO temp.trend_collection_daily_rebuild VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    for (collection, day), value in sorted(daily.items()):
        member_count = int(value["member_count"])
        collection_member_count = total_members[collection]
        comparable = int(value["comparable_member_count"])
        breadth_up = (
            100.0 * int(value["up_member_count"]) / comparable if comparable else None
        )
        batch.append(
            (
                collection,
                day,
                float(value["normalized_sum"]) / member_count,
                int(value["sales_count"]),
                int(value["notional_cents"]),
                member_count,
                collection_member_count,
                member_count / collection_member_count,
                breadth_up,
                comparable,
                refreshed_at,
            )
        )
        inserted += 1
        if len(batch) >= insert_batch_size:
            store.executemany(insert_sql, batch)
            batch = []
    if batch:
        store.executemany(insert_sql, batch)
    return inserted


def refresh_daily_aggregates(
    source_db_path: str | Path,
    store_db_path: str | Path,
    *,
    insert_batch_size: int = 2_000,
) -> dict[str, Any]:
    """Rebuild daily aggregates atomically from completed-sale event time.

    ``sold_at`` is intentionally the only clock used. Falling back to
    ``fetched_at`` would let observation delay alter historical labels.
    Production data is never modified: the destination must be a different
    SQLite file.
    """

    source_path = Path(source_db_path).expanduser().resolve()
    store_path = Path(store_db_path).expanduser().resolve()
    if source_path == store_path:
        raise ValueError("trend store must be separate from the source database")
    if insert_batch_size < 1:
        raise ValueError("insert_batch_size must be positive")

    refreshed_at = utc_now()
    store = connect_store(store_path)
    try:
        store.executescript(
            """
            DROP TABLE IF EXISTS temp.trend_daily_rebuild;
            DROP TABLE IF EXISTS temp.trend_skin_catalog_rebuild;
            DROP TABLE IF EXISTS temp.trend_collection_daily_rebuild;

            CREATE TEMP TABLE trend_daily_rebuild (
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
            ) WITHOUT ROWID;

            CREATE TEMP TABLE trend_skin_catalog_rebuild (
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
            ) WITHOUT ROWID;

            CREATE TEMP TABLE trend_collection_daily_rebuild (
                collection_name TEXT NOT NULL,
                day TEXT NOT NULL,
                index_value REAL NOT NULL,
                sales_count INTEGER NOT NULL,
                notional_cents INTEGER NOT NULL,
                member_count INTEGER NOT NULL,
                total_member_count INTEGER NOT NULL,
                member_coverage REAL NOT NULL,
                breadth_up REAL,
                comparable_member_count INTEGER NOT NULL,
                refreshed_at TEXT NOT NULL,
                PRIMARY KEY (collection_name, day)
            ) WITHOUT ROWID;
            """
        )

        source_digest = hashlib.sha256()
        source_rows = 0
        aggregate_rows = 0
        catalog_rows = 0
        first_day: str | None = None
        last_day: str | None = None
        source_max_fetched_at: str | None = None
        mapped_sale_rows = 0
        mapped_catalog_rows = 0
        daily_batch: list[tuple[Any, ...]] = []
        catalog_batch: list[tuple[Any, ...]] = []
        current_key: tuple[str, str] | None = None
        prices: list[int] = []
        group_max_fetched: str | None = None
        current_catalog_name: str | None = None
        catalog_sales_count = 0
        catalog_asset_ids: set[str] = set()
        catalog_float_count = 0
        catalog_float_sum = 0.0
        catalog_min_float: float | None = None
        catalog_max_float: float | None = None
        catalog_collection: str | None = None
        catalog_collection_key: tuple[str, str, str] | None = None
        catalog_mapped_sales = 0
        catalog_reference_quantity: int | None = None
        catalog_reference_at: str | None = None
        catalog_reference_key: tuple[str, str] | None = None
        catalog_icon_url: str | None = None
        catalog_icon_key: tuple[str, str] | None = None
        catalog_first_day: str | None = None
        catalog_last_day: str | None = None

        def flush_group() -> None:
            nonlocal aggregate_rows, daily_batch, prices
            if current_key is None:
                return
            daily_batch.append(
                _aggregate_row(
                    current_key[0],
                    current_key[1],
                    prices,
                    group_max_fetched,
                    refreshed_at,
                )
            )
            aggregate_rows += 1
            prices = []
            if len(daily_batch) >= insert_batch_size:
                store.executemany(
                    "INSERT INTO temp.trend_daily_rebuild VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    daily_batch,
                )
                daily_batch = []

        def reset_catalog(name: str) -> None:
            nonlocal current_catalog_name
            nonlocal catalog_sales_count, catalog_asset_ids
            nonlocal catalog_float_count, catalog_float_sum
            nonlocal catalog_min_float, catalog_max_float
            nonlocal catalog_collection, catalog_collection_key
            nonlocal catalog_mapped_sales
            nonlocal catalog_reference_quantity, catalog_reference_at
            nonlocal catalog_reference_key, catalog_icon_url, catalog_icon_key
            nonlocal catalog_first_day, catalog_last_day
            current_catalog_name = name
            catalog_sales_count = 0
            catalog_asset_ids = set()
            catalog_float_count = 0
            catalog_float_sum = 0.0
            catalog_min_float = None
            catalog_max_float = None
            catalog_collection = None
            catalog_collection_key = None
            catalog_mapped_sales = 0
            catalog_reference_quantity = None
            catalog_reference_at = None
            catalog_reference_key = None
            catalog_icon_url = None
            catalog_icon_key = None
            catalog_first_day = None
            catalog_last_day = None

        def flush_catalog() -> None:
            nonlocal catalog_rows, mapped_catalog_rows, catalog_batch
            if current_catalog_name is None or catalog_sales_count < 1:
                return
            collection = catalog_collection or UNKNOWN_COLLECTION
            if collection != UNKNOWN_COLLECTION:
                mapped_catalog_rows += 1
            catalog_batch.append(
                (
                    current_catalog_name,
                    collection,
                    catalog_sales_count,
                    len(catalog_asset_ids),
                    catalog_min_float,
                    (
                        catalog_float_sum / catalog_float_count
                        if catalog_float_count
                        else None
                    ),
                    catalog_max_float,
                    catalog_reference_quantity,
                    catalog_reference_at,
                    catalog_icon_url,
                    catalog_mapped_sales / catalog_sales_count,
                    catalog_first_day,
                    catalog_last_day,
                    refreshed_at,
                )
            )
            catalog_rows += 1
            if len(catalog_batch) >= insert_batch_size:
                store.executemany(
                    "INSERT INTO temp.trend_skin_catalog_rebuild VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    catalog_batch,
                )
                catalog_batch = []

        with open_source_snapshot(source_path) as source:
            _require_source_contract(source)
            columns = {row[1] for row in source.execute("PRAGMA table_info(sales)")}
            fetched_expression = _optional_source_expression(columns, "fetched_at")
            collection_expression = _collection_source_expression(columns)
            float_expression = _optional_source_expression(columns, "float_value")
            quantity_expression = _optional_source_expression(
                columns, "reference_quantity"
            )
            asset_expression = _optional_source_expression(columns, "item_asset_id")
            icon_expression = _optional_source_expression(columns, "item_icon_url")
            reference_updated = _optional_source_expression(
                columns, "reference_last_updated"
            )
            reference_at_expression = (
                f"COALESCE({reference_updated}, {fetched_expression}, sold_at)"
                if reference_updated != "NULL" or fetched_expression != "NULL"
                else "sold_at"
            )
            rows = source.execute(
                f"""
                SELECT listing_id,
                       TRIM(name) AS name,
                       DATE(sold_at) AS sold_day,
                       price_cents,
                       {fetched_expression} AS fetched_at,
                       {collection_expression} AS collection_name,
                       {float_expression} AS float_value,
                       {quantity_expression} AS reference_quantity,
                       {reference_at_expression} AS reference_at,
                       {asset_expression} AS item_asset_id,
                       {icon_expression} AS item_icon_url
                FROM sales
                WHERE state = 'sold'
                  AND name IS NOT NULL
                  AND TRIM(name) <> ''
                  AND price_cents IS NOT NULL
                  AND price_cents > 0
                  AND DATE(sold_at) IS NOT NULL
                ORDER BY TRIM(name), sold_day, listing_id
                """
            )
            for row in rows:
                name = str(row["name"])
                day = str(row["sold_day"])
                price = int(row["price_cents"])
                fetched_at = (
                    str(row["fetched_at"]) if row["fetched_at"] is not None else None
                )
                listing_id = str(row["listing_id"])
                key = (name, day)
                if current_key is not None and key != current_key:
                    flush_group()
                if key != current_key:
                    current_key = key
                    group_max_fetched = None
                prices.append(price)
                if fetched_at and (
                    group_max_fetched is None or fetched_at > group_max_fetched
                ):
                    group_max_fetched = str(fetched_at)
                if fetched_at and (
                    source_max_fetched_at is None or fetched_at > source_max_fetched_at
                ):
                    source_max_fetched_at = str(fetched_at)

                if current_catalog_name != name:
                    flush_catalog()
                    reset_catalog(name)
                catalog_sales_count += 1
                catalog_first_day = (
                    day
                    if catalog_first_day is None or day < catalog_first_day
                    else catalog_first_day
                )
                catalog_last_day = (
                    day
                    if catalog_last_day is None or day > catalog_last_day
                    else catalog_last_day
                )
                asset_id = str(row["item_asset_id"] or "").strip()
                if asset_id:
                    catalog_asset_ids.add(asset_id)
                float_value = _finite_optional_float(row["float_value"])
                if float_value is not None and 0 <= float_value <= 1:
                    catalog_float_count += 1
                    catalog_float_sum += float_value
                    catalog_min_float = (
                        float_value
                        if catalog_min_float is None
                        else min(catalog_min_float, float_value)
                    )
                    catalog_max_float = (
                        float_value
                        if catalog_max_float is None
                        else max(catalog_max_float, float_value)
                    )
                observation_at = str(fetched_at or day)
                observation_key = (observation_at, day, listing_id)
                collection = str(row["collection_name"] or "").strip()
                if collection:
                    mapped_sale_rows += 1
                    catalog_mapped_sales += 1
                    if (
                        catalog_collection_key is None
                        or observation_key >= catalog_collection_key
                    ):
                        catalog_collection = collection
                        catalog_collection_key = observation_key
                icon_url = str(row["item_icon_url"] or "").strip()
                if icon_url and (
                    catalog_icon_key is None
                    or (observation_at, listing_id) >= catalog_icon_key
                ):
                    catalog_icon_url = icon_url
                    catalog_icon_key = (observation_at, listing_id)
                reference_value = row["reference_quantity"]
                if reference_value is not None:
                    try:
                        reference_quantity = int(float(reference_value))
                    except (TypeError, ValueError, OverflowError):
                        reference_quantity = -1
                    reference_at = str(row["reference_at"] or observation_at)
                    reference_key = (reference_at, listing_id)
                    if reference_quantity >= 0 and (
                        catalog_reference_key is None
                        or reference_key >= catalog_reference_key
                    ):
                        catalog_reference_quantity = reference_quantity
                        catalog_reference_at = reference_at
                        catalog_reference_key = reference_key
                _framed_hash_update(
                    source_digest,
                    (
                        listing_id,
                        name,
                        day,
                        price,
                        fetched_at,
                        collection,
                        row["float_value"],
                        row["reference_quantity"],
                        row["reference_at"],
                        asset_id,
                        icon_url,
                    ),
                )
                source_rows += 1
                first_day = day if first_day is None or day < first_day else first_day
                last_day = day if last_day is None or day > last_day else last_day
            flush_group()
            flush_catalog()
            if daily_batch:
                store.executemany(
                    "INSERT INTO temp.trend_daily_rebuild VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    daily_batch,
                )
            if catalog_batch:
                store.executemany(
                    "INSERT INTO temp.trend_skin_catalog_rebuild VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    catalog_batch,
                )

        collection_daily_rows = _build_collection_daily_rebuild(
            store,
            refreshed_at=refreshed_at,
            insert_batch_size=insert_batch_size,
        )

        # Finish the private TEMP build before opening the short transaction
        # that swaps every related table into reader-visible state together.
        store.commit()

        source_summary = {
            "source_path": str(source_path),
            "eligible_sale_rows": source_rows,
            "daily_rows": aggregate_rows,
            "catalog_rows": catalog_rows,
            "collection_daily_rows": collection_daily_rows,
            "market_hash_names": catalog_rows,
            "mapped_market_hash_names": mapped_catalog_rows,
            "collection_mapping_coverage": (
                mapped_sale_rows / source_rows if source_rows else 0.0
            ),
            "first_day": first_day,
            "last_day": last_day,
            "source_max_fetched_at": source_max_fetched_at,
            "content_sha256": source_digest.hexdigest(),
            "event_time_column": "sold_at",
        }
        source_summary["logical_sha256"] = canonical_sha256(source_summary)

        store.execute("BEGIN IMMEDIATE")
        try:
            store.execute("DELETE FROM trend_daily_market")
            store.execute("DELETE FROM trend_skin_catalog")
            store.execute("DELETE FROM trend_collection_daily")
            store.execute(
                "INSERT INTO trend_daily_market "
                "SELECT * FROM temp.trend_daily_rebuild"
            )
            store.execute(
                "INSERT INTO trend_skin_catalog "
                "SELECT * FROM temp.trend_skin_catalog_rebuild"
            )
            store.execute(
                "INSERT INTO trend_collection_daily "
                "SELECT * FROM temp.trend_collection_daily_rebuild"
            )
            _set_meta(store, "daily_source", source_summary, refreshed_at)
            _set_meta(store, "collection_source", source_summary, refreshed_at)
            _set_meta(store, "schema_version", SCHEMA_VERSION, refreshed_at)
            store.commit()
        except Exception:
            store.rollback()
            raise

        return {
            "status": "ready",
            "schema_version": SCHEMA_VERSION,
            "refreshed_at": refreshed_at,
            **source_summary,
        }
    finally:
        store.close()


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    initialize_store(conn)
    row = conn.execute(
        "SELECT value_json FROM trend_store_meta WHERE key=?", (key,)
    ).fetchone()
    if not row:
        return default
    return json.loads(row[0])


def load_daily_aggregates(
    conn: sqlite3.Connection,
    *,
    start_day: str | None = None,
    end_day: str | None = None,
    market_hash_names: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    initialize_store(conn)
    conditions: list[str] = []
    params: list[Any] = []
    if start_day:
        conditions.append("day >= ?")
        params.append(start_day)
    if end_day:
        conditions.append("day <= ?")
        params.append(end_day)
    names = sorted(
        {str(name).strip() for name in market_hash_names or () if str(name).strip()}
    )
    if names:
        placeholders = ",".join("?" for _ in names)
        conditions.append(f"market_hash_name IN ({placeholders})")
        params.extend(names)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT market_hash_name, day, sales_count, median_price_cents,
                   mean_price_cents, min_price_cents, max_price_cents,
                   p15_price_cents, p85_price_cents, notional_cents,
                   source_max_fetched_at
            FROM trend_daily_market
            {where}
            ORDER BY day, market_hash_name
            """,
            params,
        )
    ]


def save_forecasts(
    conn: sqlite3.Connection, forecasts: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    initialize_store(conn)
    rows = [dict(row) for row in forecasts]
    if not rows:
        return {"saved": 0, "as_of_day": None}
    as_of_days = {str(row["as_of_day"]) for row in rows}
    if len(as_of_days) != 1:
        raise ValueError("one forecast write must contain exactly one as_of_day")
    as_of_day = as_of_days.pop()
    generated_at = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM trend_forecasts WHERE as_of_day=?", (as_of_day,))
        conn.executemany(
            """
            INSERT INTO trend_forecasts(
                as_of_day, market_hash_name, horizon_days, generated_at,
                model_version, current_price_cents, predicted_price_cents,
                low_price_cents, high_price_cents, expected_return_percent,
                downside_return_percent, probability_up, confidence,
                history_rows, last_observation_day, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["as_of_day"],
                    row["market_hash_name"],
                    row["horizon_days"],
                    generated_at,
                    row["model_version"],
                    row["current_price_cents"],
                    row["predicted_price_cents"],
                    row["low_price_cents"],
                    row["high_price_cents"],
                    row["expected_return_percent"],
                    row["downside_return_percent"],
                    row["probability_up"],
                    row["confidence"],
                    row["history_rows"],
                    row["last_observation_day"],
                    canonical_json(row),
                )
                for row in rows
            ],
        )
        _set_meta(
            conn,
            "latest_forecast",
            {
                "as_of_day": as_of_day,
                "generated_at": generated_at,
                "forecast_rows": len(rows),
                "model_version": rows[0]["model_version"],
            },
            generated_at,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"saved": len(rows), "as_of_day": as_of_day, "generated_at": generated_at}


def save_backtest_report(conn: sqlite3.Connection, report: Mapping[str, Any]) -> str:
    initialize_store(conn)
    value = dict(report)
    config_sha256 = canonical_sha256(value.get("config", {}))
    evaluation_sha256 = str(value.get("evaluation_sha256") or "")
    if len(evaluation_sha256) != 64:
        raise ValueError("backtest report has no valid evaluation_sha256")
    report_id = canonical_sha256(
        {
            "evaluation_sha256": evaluation_sha256,
            "generated_at": value.get("generated_at"),
            "model_version": value.get("model_version"),
        }
    )[:24]
    conn.execute(
        """
        INSERT OR REPLACE INTO trend_backtest_reports(
            report_id, generated_at, model_version, config_sha256,
            evaluation_sha256, status, report_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report_id,
            str(value["generated_at"]),
            str(value["model_version"]),
            config_sha256,
            evaluation_sha256,
            str(value.get("status", "unknown")),
            canonical_json(value),
        ),
    )
    conn.commit()
    return report_id
