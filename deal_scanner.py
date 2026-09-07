#!/usr/bin/env python3

import fcntl
import json
import math
import os
import signal
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app_config
import csfloat_listing_fetcher
import deal_intelligence
import item_classification
import model_lifecycle
import scanner_control
import scanner_target_profiles

try:
    import glove_model_runtime
    import igor_model_runtime
    import knife_model_runtime
    import neural_model_runtime
    import price_model_runtime
except ModuleNotFoundError as exc:
    missing = exc.name or "model dependency"
    print(
        f"Missing Python package: {missing}\n"
        "Install model dependencies into this venv:\n"
        "python -m pip install joblib numpy pandas scikit-learn requests[socks]\n",
        flush=True,
    )
    raise SystemExit(1) from exc


DB_PATH = str(app_config.path("CS2_DB_PATH", "csfloat_history.db"))
CONFIG_PATH = app_config.PROJECT_DIR / "deal_scanner_config.json"
SKINS_PATH = app_config.path("CS2_SKINS_PATH", "ALL_SKINS.json")
SCANNER_LOCK_PATH = app_config.path(
    "CS2_DEAL_SCANNER_LOCK_PATH",
    ".run/deal_scanner.lock",
)
CSFLOAT_ITEM_BASE_URL = "https://csfloat.com/item/"

DEFAULT_CONFIG = {
    "scan_interval_seconds": 600,
    "stickers_per_cycle": 5,
    "listings_per_sticker": 50,
    "listing_pages_per_sticker": 1,
    "stale_prediction_hours": 24,
    "sort_by": "most_recent",
    "max_sticker_targets": 120,
    # Separate, intentionally small profile for high-turnover skins under $10.
    # At the defaults it adds at most two bounded CSFloat requests per cycle.
    "liquid_skins_enabled": True,
    "liquid_skins_per_cycle": 2,
    "listings_per_liquid_skin": 20,
    "listing_pages_per_liquid_skin": 1,
    "max_liquid_skin_targets": 80,
    "liquid_skin_max_price_cents": 1000,
    "liquid_skin_min_estimated_sales_per_day": 0.5,
    "liquid_skin_min_quantity": 250,
    "liquid_skin_sort_by": "lowest_price",
    "liquid_skin_catalog_path": "ALL_SKINS.json",
    # Knives are a separate, opt-in scanner profile.  It only reads exact
    # targets from the synchronized local catalog and only invokes the
    # dedicated knife model.  The deliberately small limits protect CSFloat
    # quota even after an operator enables the profile.
    "knives_enabled": False,
    "knives_per_cycle": 1,
    "listings_per_knife": 10,
    "listing_pages_per_knife": 1,
    # Local queue window, not a lifetime catalog cap.  Its cursor and pending
    # names are durable, so every catalog knife eventually enters a cycle.
    "max_knife_targets": 24,
    # Refresh local velocity ranks periodically without adding network calls.
    "knife_rediscovery_cycles": 6,
    "knife_sort_by": "lowest_price",
    # Gloves have their own target profile and must have their own model.  The
    # default remains off; enabling it before a valid dedicated artifact exists
    # produces a local "model unavailable" scan log and zero network calls.
    "gloves_enabled": False,
    "gloves_per_cycle": 1,
    "listings_per_glove": 10,
    "listing_pages_per_glove": 1,
    "max_glove_targets": 24,
    "glove_rediscovery_cycles": 6,
    "glove_sort_by": "lowest_price",
    "tournament_patterns": [
        "Katowice 2014",
        "Katowice 2015",
    ],
    "extra_sticker_names": [
        "Sticker | Crown (Foil)",
    ],
    "min_roi_percent": 8.0,
    "min_profit_usd": 1.0,
    "min_liquidity_score": 20.0,
    "save_verdicts": ["good", "watch"],
    "save_all_predictions": False,
    "enabled_models": ["almaz", "igor", "nova"],
}

MODEL_RUNNERS = {
    "almaz": price_model_runtime.predict_deal,
    "igor": igor_model_runtime.predict_deal,
    "nova": neural_model_runtime.predict_deal,
}
MODEL_DEAL_SCHEMA_VERSION = 5
VISIBLE_DEAL_VERDICTS = frozenset({"good", "watch"})
SOURCE_STICKER = "sticker"
SOURCE_LIQUID_SKIN = "liquid_skin"
SOURCE_KNIFE = "knife"
SOURCE_GLOVE = "glove"
KNIFE_MODEL_NAME = "knives"
GLOVE_MODEL_NAME = "gloves"
ITEM_KIND_MODEL_NAMES = {
    item_classification.ITEM_KIND_WEAPON_SKIN: frozenset(MODEL_RUNNERS),
    item_classification.ITEM_KIND_KNIFE: frozenset({KNIFE_MODEL_NAME}),
    item_classification.ITEM_KIND_GLOVE: frozenset({GLOVE_MODEL_NAME}),
}
ALL_ROUTED_MODEL_NAMES = frozenset().union(*ITEM_KIND_MODEL_NAMES.values())
CONTROL_POLL_SECONDS = 1.0
STATUS_HEARTBEAT_SECONDS = 5.0
_STOP_EVENT = threading.Event()


class ScanControlInterrupted(RuntimeError):
    """A scan stopped between external/model operations due to live control."""


@contextmanager
def scanner_instance_lock(path=None):
    """Prevent two scanners from spending API quota and racing one status file."""

    lock_path = Path(path or SCANNER_LOCK_PATH)
    if not lock_path.is_absolute():
        lock_path = app_config.PROJECT_DIR / lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "unknown"
            raise RuntimeError(
                f"deal_scanner is already running (lock owner: {owner})"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_config():
    path = Path(CONFIG_PATH)
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        return dict(DEFAULT_CONFIG)

    config = dict(DEFAULT_CONFIG)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise TypeError(f"{path} must contain a JSON object")
    config.update(loaded)
    return validate_config(config)


def validate_config(config):
    result = dict(config)
    integer_ranges = {
        # This legacy value only seeds scanner_control.json on first use.
        # Persistent UI updates use the same safety bounds.
        "scan_interval_seconds": (
            scanner_control.MIN_INTERVAL_SECONDS,
            scanner_control.MAX_INTERVAL_SECONDS,
        ),
        "stickers_per_cycle": (1, 1000),
        "listings_per_sticker": (1, 50),
        "listing_pages_per_sticker": (1, 10),
        "stale_prediction_hours": (1, 720),
        "max_sticker_targets": (1, 5000),
        # Cheap-skin discovery is bounded independently from sticker scanning.
        "liquid_skins_per_cycle": (1, 5),
        "listings_per_liquid_skin": (1, 25),
        "listing_pages_per_liquid_skin": (1, 2),
        "max_liquid_skin_targets": (1, 500),
        "liquid_skin_max_price_cents": (
            1,
            csfloat_listing_fetcher.CHEAP_SKIN_MAX_PRICE_CENTS,
        ),
        "liquid_skin_min_quantity": (0, 1_000_000_000),
        # Knife scanning remains much smaller than the general profiles.
        "knives_per_cycle": (1, 2),
        "listings_per_knife": (1, 20),
        "listing_pages_per_knife": (1, 2),
        "max_knife_targets": (1, 100),
        "knife_rediscovery_cycles": (1, 1440),
        # Gloves share the conservative request envelope but never a model.
        "gloves_per_cycle": (1, 2),
        "listings_per_glove": (1, 20),
        "listing_pages_per_glove": (1, 2),
        "max_glove_targets": (1, 100),
        "glove_rediscovery_cycles": (1, 1440),
    }
    for name, (minimum, maximum) in integer_ranges.items():
        try:
            value = int(result[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        result[name] = value

    for name in (
        "min_roi_percent",
        "min_profit_usd",
        "min_liquidity_score",
        "liquid_skin_min_estimated_sales_per_day",
    ):
        try:
            value = float(result[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a number") from exc
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if name == "liquid_skin_min_estimated_sales_per_day" and value < 0:
            raise ValueError(f"{name} must be at least 0")
        result[name] = value

    if not isinstance(result.get("liquid_skins_enabled"), bool):
        raise TypeError("liquid_skins_enabled must be true or false")
    if not isinstance(result.get("knives_enabled"), bool):
        raise TypeError("knives_enabled must be true or false")
    if not isinstance(result.get("gloves_enabled"), bool):
        raise TypeError("gloves_enabled must be true or false")

    catalog_path = str(result.get("liquid_skin_catalog_path") or "").strip()
    if not catalog_path:
        raise ValueError("liquid_skin_catalog_path must not be empty")
    result["liquid_skin_catalog_path"] = catalog_path

    for field_name in ("tournament_patterns", "extra_sticker_names"):
        raw_values = result.get(field_name)
        if not isinstance(raw_values, list):
            raise TypeError(f"{field_name} must be a JSON list")
        if len(raw_values) > 100:
            raise ValueError(f"{field_name} may contain at most 100 values")
        normalized_values = []
        for raw_value in raw_values:
            if not isinstance(raw_value, str):
                raise TypeError(f"{field_name} values must be strings")
            value = raw_value.strip()
            if (
                not value
                or len(value) > 160
                or any(character in value for character in ("\x00", "\r", "\n"))
            ):
                raise ValueError(f"{field_name} contains an invalid value")
            normalized_values.append(value)
        result[field_name] = list(dict.fromkeys(normalized_values))

    models = result.get("enabled_models")
    if not isinstance(models, list) or not models:
        raise ValueError("enabled_models must be a non-empty JSON list")
    normalized_models = [str(name).strip().lower() for name in models]
    unknown_models = sorted(set(normalized_models) - set(MODEL_RUNNERS))
    if unknown_models:
        raise ValueError(f"Unknown enabled_models: {', '.join(unknown_models)}")
    result["enabled_models"] = list(dict.fromkeys(normalized_models))

    verdicts = result.get("save_verdicts")
    if not isinstance(verdicts, list) or not verdicts:
        raise ValueError("save_verdicts must be a non-empty JSON list")
    result["save_verdicts"] = [str(value).strip() for value in verdicts]
    if result.get("sort_by") not in {"most_recent", "lowest_price", "highest_discount"}:
        raise ValueError("sort_by must be most_recent, lowest_price, or highest_discount")
    if result.get("liquid_skin_sort_by") not in csfloat_listing_fetcher.ALLOWED_ACTIVE_LISTING_SORTS:
        raise ValueError(
            "liquid_skin_sort_by must be most_recent, lowest_price, or highest_discount"
        )
    if result.get("knife_sort_by") not in csfloat_listing_fetcher.ALLOWED_ACTIVE_LISTING_SORTS:
        raise ValueError(
            "knife_sort_by must be most_recent, lowest_price, or highest_discount"
        )
    if result.get("glove_sort_by") not in csfloat_listing_fetcher.ALLOWED_ACTIVE_LISTING_SORTS:
        raise ValueError(
            "glove_sort_by must be most_recent, lowest_price, or highest_discount"
        )
    return result


def scanner_target_profile_defaults(config):
    """Translate legacy profile flags into first-run target-control defaults."""

    return {
        scanner_target_profiles.PROFILE_STICKER_GUNS: True,
        scanner_target_profiles.PROFILE_LIQUID_UNDER_10: bool(
            config.get("liquid_skins_enabled")
        ),
        scanner_target_profiles.PROFILE_KNIVES: bool(config.get("knives_enabled")),
        scanner_target_profiles.PROFILE_GLOVES: bool(config.get("gloves_enabled")),
    }


def load_scanner_target_control(config, *, create_if_missing=False):
    """Read durable profile choices; malformed state remains fail-closed."""

    return scanner_control.get_scanner_target_control(
        profile_defaults=scanner_target_profile_defaults(config),
        create_if_missing=create_if_missing,
    )


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_columns(conn, table_name, columns):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    for column_name, declaration in columns.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}")


def init_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_schema_versions (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_deal_candidates (
            listing_id TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            scan_count INTEGER NOT NULL DEFAULT 1,
            source_sticker_id INTEGER,
            source_sticker_name TEXT,
            skin_name TEXT,
            buy_price_cents INTEGER,
            predicted_price_cents INTEGER,
            low_price_cents INTEGER,
            high_price_cents INTEGER,
            net_expected_cents INTEGER,
            profit_cents INTEGER,
            roi_percent REAL,
            liquidity_score REAL,
            historical_sales_count INTEGER,
            sales_per_day REAL,
            verdict TEXT,
            pricing_method TEXT,
            sticker_count INTEGER,
            sticker_value_cents INTEGER,
            float_value REAL,
            paint_seed INTEGER,
            paint_index INTEGER,
            csfloat_url TEXT,
            inspect_link TEXT,
            key_file TEXT,
            proxy TEXT,
            rate_remaining TEXT,
            deal_payload_json TEXT,
            prediction_json TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_deal_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TEXT NOT NULL,
            sticker_id INTEGER,
            sticker_name TEXT,
            request_url TEXT,
            listings_returned INTEGER,
            candidates_saved INTEGER,
            key_file TEXT,
            proxy TEXT,
            rate_remaining TEXT,
            error TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_deal_scanner_profile_state (
            profile TEXT PRIMARY KEY,
            cursor_offset INTEGER NOT NULL DEFAULT 0,
            pending_target_keys_json TEXT NOT NULL DEFAULT '[]',
            catalog_size INTEGER NOT NULL DEFAULT 0,
            last_discovered_at TEXT,
            updated_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_deal_listing_sources (
            listing_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            target_key TEXT NOT NULL,
            source_name TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            scan_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (listing_id, source_type, target_key)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_deal_predictions (
            listing_id TEXT NOT NULL,
            model_name TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            scan_count INTEGER NOT NULL DEFAULT 1,
            source_sticker_id INTEGER,
            source_sticker_name TEXT,
            skin_name TEXT,
            buy_price_cents INTEGER,
            predicted_price_cents INTEGER,
            low_price_cents INTEGER,
            high_price_cents INTEGER,
            net_expected_cents INTEGER,
            profit_cents INTEGER,
            roi_percent REAL,
            liquidity_score REAL,
            historical_sales_count INTEGER,
            sales_per_day REAL,
            verdict TEXT,
            pricing_method TEXT,
            sticker_count INTEGER,
            sticker_value_cents INTEGER,
            float_value REAL,
            paint_seed INTEGER,
            paint_index INTEGER,
            listing_created_at TEXT,
            item_icon_url TEXT,
            screenshot_url TEXT,
            csfloat_url TEXT,
            inspect_link TEXT,
            key_file TEXT,
            proxy TEXT,
            rate_remaining TEXT,
            deal_payload_json TEXT,
            prediction_json TEXT,
            PRIMARY KEY (listing_id, model_name)
        )
    """)

    ensure_columns(
        conn,
        "model_deal_predictions",
        {
            "listing_created_at": "TEXT",
            "item_icon_url": "TEXT",
            "screenshot_url": "TEXT",
            "source_type": "TEXT NOT NULL DEFAULT 'sticker'",
            "source_name": "TEXT",
            "target_key": "TEXT",
        },
    )
    ensure_columns(
        conn,
        "model_deal_candidates",
        {
            "source_type": "TEXT NOT NULL DEFAULT 'sticker'",
            "source_name": "TEXT",
            "target_key": "TEXT",
        },
    )
    ensure_columns(
        conn,
        "model_deal_scans",
        {
            "source_type": "TEXT NOT NULL DEFAULT 'sticker'",
            "source_name": "TEXT",
            "target_key": "TEXT",
        },
    )
    for table_name in ("model_deal_predictions", "model_deal_candidates"):
        conn.execute(
            f"""
            UPDATE {table_name}
            SET source_name = COALESCE(source_name, source_sticker_name),
                target_key = COALESCE(
                    target_key,
                    CASE
                        WHEN source_sticker_id IS NOT NULL
                        THEN 'sticker:' || source_sticker_id
                    END
                )
            WHERE source_name IS NULL OR target_key IS NULL
            """
        )
    conn.execute(
        """
        UPDATE model_deal_scans
        SET source_name = COALESCE(source_name, sticker_name),
            target_key = COALESCE(
                target_key,
                CASE WHEN sticker_id IS NOT NULL THEN 'sticker:' || sticker_id END
            )
        WHERE source_name IS NULL OR target_key IS NULL
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO model_deal_listing_sources (
            listing_id, source_type, target_key, source_name,
            first_seen_at, last_seen_at, scan_count
        )
        SELECT
            listing_id,
            COALESCE(source_type, 'sticker'),
            target_key,
            MAX(source_name),
            MIN(first_seen_at),
            MAX(last_seen_at),
            MAX(scan_count)
        FROM model_deal_predictions
        WHERE target_key IS NOT NULL AND target_key != ''
        GROUP BY listing_id, COALESCE(source_type, 'sticker'), target_key
        """
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_deal_candidates_roi ON model_deal_candidates(roi_percent DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_deal_candidates_last_seen ON model_deal_candidates(last_seen_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_deal_scans_scanned_at ON model_deal_scans(scanned_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_deal_predictions_roi ON model_deal_predictions(model_name, roi_percent DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_deal_predictions_listing ON model_deal_predictions(listing_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_deal_predictions_source "
        "ON model_deal_predictions(source_type, last_seen_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_deal_listing_sources_source "
        "ON model_deal_listing_sources(source_type, last_seen_at DESC, listing_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_deal_predictions_visible "
        "ON model_deal_predictions(verdict, last_seen_at DESC, model_name, roi_percent DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_deal_predictions_actionable_seen "
        "ON model_deal_predictions(last_seen_at DESC, roi_percent DESC, profit_cents DESC) "
        "WHERE verdict IN ('good', 'watch')"
    )
    deal_intelligence.init_schema(conn)
    conn.execute(
        """
        INSERT INTO app_schema_versions(component, version, migrated_at)
        VALUES ('model_deals', ?, ?)
        ON CONFLICT(component) DO UPDATE SET
            version = excluded.version,
            migrated_at = excluded.migrated_at
        """,
        (MODEL_DEAL_SCHEMA_VERSION, utc_now()),
    )
    conn.commit()


def discover_sticker_targets(conn, config, target_control=None, *, apply_control=True):
    patterns = [str(item).strip() for item in config.get("tournament_patterns", []) if str(item).strip()]
    extra_names = [str(item).strip() for item in config.get("extra_sticker_names", []) if str(item).strip()]
    where_parts = []
    params = []
    manual_ids = []

    for pattern in patterns:
        where_parts.append("name LIKE ?")
        params.append(f"%{pattern}%")

    for name in extra_names:
        where_parts.append("name = ?")
        params.append(name)

    if target_control is not None:
        settings = scanner_target_profiles.profile_settings(
            target_control,
            scanner_target_profiles.PROFILE_STICKER_GUNS,
        )
        if not settings["enabled"] and apply_control:
            return []
        for key in settings["include"]:
            try:
                sticker_id = int(key.split(":", 1)[1])
            except (IndexError, TypeError, ValueError):
                continue
            if sticker_id >= 0:
                manual_ids.append(sticker_id)
        if manual_ids:
            placeholders = ",".join("?" for _ in manual_ids)
            where_parts.append(f"sticker_id IN ({placeholders})")
            params.extend(manual_ids)

    if not where_parts:
        return []

    limit = int(config.get("max_sticker_targets") or 120)
    manual_order = ""
    order_params = []
    if manual_ids:
        placeholders = ",".join("?" for _ in manual_ids)
        manual_order = f"CASE WHEN sticker_id IN ({placeholders}) THEN 0 ELSE 1 END,"
        order_params = list(manual_ids)
    rows = [
        {
            **dict(row),
            "source_type": SOURCE_STICKER,
            "source_name": str(row["name"]),
            "target_key": f"{SOURCE_STICKER}:{row['sticker_id']}",
        }
        for row in conn.execute(f"""
            SELECT
                sticker_id,
                name,
                COUNT(*) AS seen_count,
                AVG(COALESCE(price_cents, reference_price_cents, 0)) AS avg_price_cents,
                MAX(COALESCE(price_cents, reference_price_cents, 0)) AS max_price_cents
            FROM sale_stickers
            WHERE sticker_id IS NOT NULL
                AND name IS NOT NULL
                AND ({' OR '.join(where_parts)})
            GROUP BY sticker_id, name
            ORDER BY {manual_order}
                     max_price_cents DESC, avg_price_cents DESC, seen_count DESC
            LIMIT ?
        """, [*params, *order_params, limit + scanner_control.MAX_INCLUDED_TARGETS])
    ]
    if target_control is None:
        return rows[:limit]
    if not apply_control:
        return rows
    selected, _selection = scanner_target_profiles.apply_profile_selection(
        scanner_target_profiles.PROFILE_STICKER_GUNS,
        rows,
        target_control,
        recommended_limit=limit,
    )
    return selected


def _catalog_path(config):
    configured = str(config.get("liquid_skin_catalog_path") or "ALL_SKINS.json")
    if configured == "ALL_SKINS.json":
        return Path(SKINS_PATH)
    path = Path(configured).expanduser()
    return path if path.is_absolute() else app_config.PROJECT_DIR / path


def _table_columns(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if row is None:
        return set()
    return {str(info[1]) for info in conn.execute(f"PRAGMA table_info({table_name})")}


def _finite_float(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def discover_liquid_skin_targets(
    conn,
    config,
    target_control=None,
    *,
    apply_control=True,
):
    """Select a bounded, deterministic set of liquid exact-skin targets.

    ``ALL_SKINS.json`` supplies the current price/market quantity boundary and
    ``skin_schedule`` supplies measured sales velocity when available.  Exact
    market hash names are deduplicated before ranking.
    """

    if target_control is not None:
        settings = scanner_target_profiles.profile_settings(
            target_control,
            scanner_target_profiles.PROFILE_LIQUID_UNDER_10,
        )
        if not settings["enabled"] and apply_control:
            return []
        manually_included_names = {
            key.split(":", 1)[1]
            for key in settings["include"]
            if ":" in key
        }
    else:
        manually_included_names = set()
    if not config.get("liquid_skins_enabled") and target_control is None:
        return []

    path = _catalog_path(config)
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"Could not read liquid skin catalog: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in liquid skin catalog {path}: {exc}") from exc
    if not isinstance(catalog, list):
        raise TypeError(f"{path} must contain a JSON list")

    max_price_cents = int(config["liquid_skin_max_price_cents"])
    minimum_quantity = int(config["liquid_skin_min_quantity"])
    catalog_by_name = {}
    for catalog_order, raw in enumerate(catalog):
        if not isinstance(raw, dict):
            continue
        market_hash_name = str(raw.get("market_hash_name") or "").strip()
        if not market_hash_name:
            continue
        if (
            item_classification.classify_market_hash_name(
                market_hash_name,
                conn=conn,
            )
            != item_classification.ITEM_KIND_WEAPON_SKIN
        ):
            continue
        base_price_usd = _finite_float(raw.get("base_price_usd"), -1)
        price_cents = round(base_price_usd * 100)
        try:
            quantity = max(int(raw.get("quantity_on_market") or 0), 0)
        except (TypeError, ValueError):
            quantity = 0
        is_manual = market_hash_name in manually_included_names
        if (
            not 0 < base_price_usd <= max_price_cents / 100
            or price_cents < 1
            or (quantity < minimum_quantity and not is_manual)
        ):
            continue

        candidate = {
            "market_hash_name": market_hash_name,
            "base_price_usd": base_price_usd,
            "base_price_cents": price_cents,
            "quantity_on_market": quantity,
            "catalog_order": catalog_order,
        }
        previous = catalog_by_name.get(market_hash_name)
        if previous is None or (
            candidate["quantity_on_market"], -candidate["base_price_cents"]
        ) > (previous["quantity_on_market"], -previous["base_price_cents"]):
            catalog_by_name[market_hash_name] = candidate

    schedule_by_name = {}
    schedule_columns = _table_columns(conn, "skin_schedule")
    if "market_hash_name" in schedule_columns:
        if "estimated_sales_per_day" in schedule_columns:
            velocity_expression = "COALESCE(estimated_sales_per_day, 0)"
        elif "avg_new_sales_count" in schedule_columns:
            velocity_expression = "COALESCE(avg_new_sales_count, 0)"
        else:
            velocity_expression = "0"
        for row in conn.execute(
            f"""
            SELECT market_hash_name, {velocity_expression} AS estimated_sales_per_day
            FROM skin_schedule
            WHERE market_hash_name IS NOT NULL
            """
        ):
            name = str(row[0] or "").strip()
            if name:
                schedule_by_name[name] = max(_finite_float(row[1], 0), 0.0)

    minimum_velocity = float(config["liquid_skin_min_estimated_sales_per_day"])
    targets = []
    for market_hash_name, target in catalog_by_name.items():
        measured_velocity = schedule_by_name.get(market_hash_name)
        is_manual = market_hash_name in manually_included_names
        if measured_velocity is None and minimum_velocity > 0 and not is_manual:
            continue
        if (
            measured_velocity is not None
            and measured_velocity < minimum_velocity
            and not is_manual
        ):
            continue
        target = dict(target)
        target.update(
            {
                "estimated_sales_per_day": measured_velocity or 0.0,
                "has_schedule_velocity": measured_velocity is not None,
                "source_type": SOURCE_LIQUID_SKIN,
                "source_name": market_hash_name,
                "target_key": f"{SOURCE_LIQUID_SKIN}:{market_hash_name}",
            }
        )
        targets.append(target)

    targets.sort(
        key=lambda target: (
            -int(target["has_schedule_velocity"]),
            -target["estimated_sales_per_day"],
            -target["quantity_on_market"],
            target["base_price_cents"],
            target["catalog_order"],
            target["market_hash_name"],
        )
    )
    limit = int(config["max_liquid_skin_targets"])
    if target_control is None:
        return targets[:limit]
    if not apply_control:
        return targets
    selected, _selection = scanner_target_profiles.apply_profile_selection(
        scanner_target_profiles.PROFILE_LIQUID_UNDER_10,
        targets,
        target_control,
        recommended_limit=limit,
    )
    return selected


def _catalog_has_exact_knife(conn, market_hash_name):
    """Check the published catalog without falling back to name heuristics."""

    name = str(market_hash_name or "").strip()
    columns = _table_columns(conn, "cs2_item_catalog")
    if not name or not {"market_hash_name", "item_kind"}.issubset(columns):
        return False
    try:
        return bool(
            conn.execute(
                """
                SELECT 1
                FROM cs2_item_catalog
                WHERE market_hash_name = ? AND item_kind = 'knife'
                LIMIT 1
                """,
                (name,),
            ).fetchone()
        )
    except sqlite3.Error:
        return False


def _catalog_has_exact_glove(conn, market_hash_name):
    """Check the published catalog without accepting a name heuristic."""

    name = str(market_hash_name or "").strip()
    columns = _table_columns(conn, "cs2_item_catalog")
    if not name or not {"market_hash_name", "item_kind"}.issubset(columns):
        return False
    try:
        return bool(
            conn.execute(
                """
                SELECT 1
                FROM cs2_item_catalog
                WHERE market_hash_name = ? AND item_kind = 'glove'
                LIMIT 1
                """,
                (name,),
            ).fetchone()
        )
    except sqlite3.Error:
        return False


def discover_knife_targets(conn, config, target_control=None, *, apply_control=True):
    """Return every exact knife from the already-published local catalog.

    Catalog refresh is intentionally outside the scanner.  This read path
    cannot download data, and it will never schedule a star-prefixed name that
    is absent from the authoritative ``item_kind='knife'`` relation.  Network
    work remains bounded later by the persistent rotating window and the
    per-cycle knife limit; discovery itself is local and intentionally complete.
    """

    if target_control is not None:
        settings = scanner_target_profiles.profile_settings(
            target_control,
            scanner_target_profiles.PROFILE_KNIVES,
        )
        if not settings["enabled"] and apply_control:
            return []
    elif not config.get("knives_enabled"):
        return []

    catalog_columns = _table_columns(conn, "cs2_item_catalog")
    if not {"market_hash_name", "item_kind"}.issubset(catalog_columns):
        return []

    schedule_by_name = {}
    schedule_columns = _table_columns(conn, "skin_schedule")
    if "market_hash_name" in schedule_columns:
        if "estimated_sales_per_day" in schedule_columns:
            velocity_expression = "COALESCE(estimated_sales_per_day, 0)"
        elif "avg_new_sales_count" in schedule_columns:
            velocity_expression = "COALESCE(avg_new_sales_count, 0)"
        else:
            velocity_expression = "0"
        for row in conn.execute(
            f"""
            SELECT market_hash_name, MAX({velocity_expression})
            FROM skin_schedule
            WHERE market_hash_name IS NOT NULL
            GROUP BY market_hash_name
            """
        ):
            name = str(row[0] or "").strip()
            if name:
                schedule_by_name[name] = max(_finite_float(row[1], 0), 0.0)

    targets = []
    for catalog_order, row in enumerate(
        conn.execute(
            """
            SELECT market_hash_name
            FROM cs2_item_catalog
            WHERE item_kind = 'knife' AND TRIM(market_hash_name) != ''
            ORDER BY market_hash_name COLLATE NOCASE
            """
        )
    ):
        market_hash_name = str(row[0] or "").strip()
        if not market_hash_name:
            continue
        measured_velocity = schedule_by_name.get(market_hash_name)
        targets.append(
            {
                "market_hash_name": market_hash_name,
                "estimated_sales_per_day": measured_velocity or 0.0,
                "has_schedule_velocity": measured_velocity is not None,
                "catalog_order": catalog_order,
                "source_type": SOURCE_KNIFE,
                "source_name": market_hash_name,
                "target_key": f"{SOURCE_KNIFE}:{market_hash_name}",
            }
        )

    targets.sort(
        key=lambda target: (
            -int(target["has_schedule_velocity"]),
            -target["estimated_sales_per_day"],
            target["catalog_order"],
            target["market_hash_name"],
        )
    )
    if target_control is None or not apply_control:
        return targets
    selected, _selection = scanner_target_profiles.apply_profile_selection(
        scanner_target_profiles.PROFILE_KNIVES,
        targets,
        target_control,
        recommended_limit=None,
    )
    return selected


def discover_glove_targets(conn, config, target_control=None, *, apply_control=True):
    """Return locally catalogued gloves selected for a dedicated profile."""

    if target_control is not None:
        settings = scanner_target_profiles.profile_settings(
            target_control,
            scanner_target_profiles.PROFILE_GLOVES,
        )
        if not settings["enabled"] and apply_control:
            return []
    elif not config.get("gloves_enabled"):
        return []

    catalog_columns = _table_columns(conn, "cs2_item_catalog")
    if not {"market_hash_name", "item_kind"}.issubset(catalog_columns):
        return []

    schedule_by_name = {}
    schedule_columns = _table_columns(conn, "skin_schedule")
    if "market_hash_name" in schedule_columns:
        if "estimated_sales_per_day" in schedule_columns:
            velocity_expression = "COALESCE(estimated_sales_per_day, 0)"
        elif "avg_new_sales_count" in schedule_columns:
            velocity_expression = "COALESCE(avg_new_sales_count, 0)"
        else:
            velocity_expression = "0"
        for row in conn.execute(
            f"""
            SELECT market_hash_name, MAX({velocity_expression})
            FROM skin_schedule
            WHERE market_hash_name IS NOT NULL
            GROUP BY market_hash_name
            """
        ):
            name = str(row[0] or "").strip()
            if name:
                schedule_by_name[name] = max(_finite_float(row[1], 0), 0.0)

    targets = []
    for catalog_order, row in enumerate(
        conn.execute(
            """
            SELECT market_hash_name
            FROM cs2_item_catalog
            WHERE item_kind = 'glove' AND TRIM(market_hash_name) != ''
            ORDER BY market_hash_name COLLATE NOCASE
            """
        )
    ):
        market_hash_name = str(row[0] or "").strip()
        if not market_hash_name:
            continue
        measured_velocity = schedule_by_name.get(market_hash_name)
        targets.append(
            {
                "market_hash_name": market_hash_name,
                "estimated_sales_per_day": measured_velocity or 0.0,
                "has_schedule_velocity": measured_velocity is not None,
                "catalog_order": catalog_order,
                "source_type": SOURCE_GLOVE,
                "source_name": market_hash_name,
                "target_key": f"{SOURCE_GLOVE}:{market_hash_name}",
            }
        )

    targets.sort(key=_knife_rank_key)
    if target_control is None or not apply_control:
        return targets
    selected, _selection = scanner_target_profiles.apply_profile_selection(
        scanner_target_profiles.PROFILE_GLOVES,
        targets,
        target_control,
        recommended_limit=None,
    )
    return selected


def _knife_rank_key(target):
    return (
        -int(target.get("has_schedule_velocity") is True),
        -_finite_float(target.get("estimated_sales_per_day"), 0),
        int(target.get("catalog_order") or 0),
        str(target.get("market_hash_name") or ""),
    )


def load_catalog_rotation_state(conn, source_type):
    """Load a durable exact-item window; malformed state fails to empty."""

    if source_type not in {SOURCE_KNIFE, SOURCE_GLOVE}:
        raise ValueError("catalog rotation is only supported for knives and gloves")

    row = conn.execute(
        """
        SELECT cursor_offset, pending_target_keys_json, catalog_size,
               last_discovered_at
        FROM model_deal_scanner_profile_state
        WHERE profile = ?
        """,
        (source_type,),
    ).fetchone()
    if row is None:
        return {
            "cursor_offset": 0,
            "pending_target_keys": [],
            "catalog_size": 0,
            "last_discovered_at": None,
        }
    try:
        cursor_offset = max(int(row[0]), 0)
        pending = json.loads(row[1])
        catalog_size = max(int(row[2]), 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {
            "cursor_offset": 0,
            "pending_target_keys": [],
            "catalog_size": 0,
            "last_discovered_at": None,
        }
    if (
        not isinstance(pending, list)
        or len(pending) > 100
        or any(
            not isinstance(value, str)
            or not value.startswith(f"{source_type}:")
            or len(value) > 512
            for value in pending
        )
    ):
        pending = []
    return {
        "cursor_offset": cursor_offset,
        "pending_target_keys": list(dict.fromkeys(pending)),
        "catalog_size": catalog_size,
        "last_discovered_at": row[3],
    }


def save_catalog_rotation_state(conn, source_type, state, *, commit=True):
    """Atomically persist enough queue state to resume after a restart."""

    if source_type not in {SOURCE_KNIFE, SOURCE_GLOVE}:
        raise ValueError("catalog rotation is only supported for knives and gloves")

    now = utc_now()
    pending = list(state.get("pending_target_keys") or [])
    conn.execute(
        """
        INSERT INTO model_deal_scanner_profile_state (
            profile, cursor_offset, pending_target_keys_json, catalog_size,
            last_discovered_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(profile) DO UPDATE SET
            cursor_offset = excluded.cursor_offset,
            pending_target_keys_json = excluded.pending_target_keys_json,
            catalog_size = excluded.catalog_size,
            last_discovered_at = excluded.last_discovered_at,
            updated_at = excluded.updated_at
        """,
        (
            source_type,
            max(int(state.get("cursor_offset") or 0), 0),
            json.dumps(pending, ensure_ascii=False, separators=(",", ":")),
            max(int(state.get("catalog_size") or 0), 0),
            state.get("last_discovered_at"),
            now,
        ),
    )
    if commit:
        conn.commit()


def refresh_catalog_rotation(
    conn,
    config,
    catalog_targets,
    state,
    *,
    source_type,
    rerank=False,
):
    """Resume or create a ranked window in stable full-catalog rotation."""

    if source_type not in {SOURCE_KNIFE, SOURCE_GLOVE}:
        raise ValueError("catalog rotation is only supported for knives and gloves")

    by_key = {
        str(target.get("target_key") or ""): target
        for target in catalog_targets
        if str(target.get("target_key") or "")
    }
    stable_targets = sorted(
        by_key.values(),
        key=lambda target: (
            int(target.get("catalog_order") or 0),
            str(target.get("market_hash_name") or ""),
        ),
    )
    pending_targets = [
        by_key[key]
        for key in state.get("pending_target_keys") or []
        if key in by_key
    ]
    if rerank:
        pending_targets.sort(key=_knife_rank_key)

    catalog_size = len(stable_targets)
    state["catalog_size"] = catalog_size
    state["last_discovered_at"] = utc_now()
    if not pending_targets and catalog_size:
        start = max(int(state.get("cursor_offset") or 0), 0) % catalog_size
        window_size = min(
            int(
                config[
                    "max_knife_targets"
                    if source_type == SOURCE_KNIFE
                    else "max_glove_targets"
                ]
            ),
            catalog_size - start,
        )
        pending_targets = [
            stable_targets[(start + offset) % catalog_size]
            for offset in range(window_size)
        ]
        pending_targets.sort(key=_knife_rank_key)
        state["cursor_offset"] = (start + window_size) % catalog_size
    elif not catalog_size:
        state["cursor_offset"] = 0

    state["pending_target_keys"] = [
        str(target["target_key"]) for target in pending_targets
    ]
    save_catalog_rotation_state(conn, source_type, state)
    return pending_targets


def advance_catalog_rotation(conn, source_type, state, target_key):
    """Durably consume exactly one attempted target from the current window."""

    key = str(target_key or "")
    pending = list(state.get("pending_target_keys") or [])
    try:
        pending.remove(key)
    except ValueError:
        pass
    state["pending_target_keys"] = pending
    save_catalog_rotation_state(conn, source_type, state)


def load_knife_rotation_state(conn):
    return load_catalog_rotation_state(conn, SOURCE_KNIFE)


def save_knife_rotation_state(conn, state, *, commit=True):
    return save_catalog_rotation_state(
        conn,
        SOURCE_KNIFE,
        state,
        commit=commit,
    )


def refresh_knife_rotation(conn, config, catalog_targets, state, *, rerank=False):
    return refresh_catalog_rotation(
        conn,
        config,
        catalog_targets,
        state,
        source_type=SOURCE_KNIFE,
        rerank=rerank,
    )


def advance_knife_rotation(conn, state, target_key):
    return advance_catalog_rotation(conn, SOURCE_KNIFE, state, target_key)


def load_glove_rotation_state(conn):
    return load_catalog_rotation_state(conn, SOURCE_GLOVE)


def refresh_glove_rotation(conn, config, catalog_targets, state, *, rerank=False):
    return refresh_catalog_rotation(
        conn,
        config,
        catalog_targets,
        state,
        source_type=SOURCE_GLOVE,
        rerank=rerank,
    )


def advance_glove_rotation(conn, state, target_key):
    return advance_catalog_rotation(conn, SOURCE_GLOVE, state, target_key)


def target_source_fields(target):
    """Return explicit source metadata while keeping sticker columns honest."""

    source_type = str(target.get("source_type") or "").strip()
    if not source_type:
        source_type = SOURCE_STICKER if target.get("sticker_id") is not None else "target"
    if source_type == SOURCE_STICKER:
        source_name = str(
            target.get("source_name") or target.get("name") or ""
        ).strip()
        sticker_id = target.get("sticker_id")
        target_key = str(
            target.get("target_key")
            or (f"{SOURCE_STICKER}:{sticker_id}" if sticker_id is not None else "")
        ).strip()
        return {
            "source_type": source_type,
            "source_name": source_name,
            "target_key": target_key,
            "source_sticker_id": sticker_id,
            "source_sticker_name": source_name or None,
        }

    source_name = str(
        target.get("source_name") or target.get("market_hash_name") or target.get("name") or ""
    ).strip()
    target_key = str(
        target.get("target_key") or f"{source_type}:{source_name}"
    ).strip()
    return {
        "source_type": source_type,
        "source_name": source_name,
        "target_key": target_key,
        "source_sticker_id": None,
        "source_sticker_name": None,
    }


def upsert_listing_source(conn, listing_id, target, *, observed_at=None):
    """Record every discovery route without duplicating model inference."""

    if not listing_id:
        return False
    source = target_source_fields(target)
    if not source["target_key"]:
        return False
    now = str(observed_at or utc_now())
    conn.execute(
        """
        INSERT INTO model_deal_listing_sources (
            listing_id, source_type, target_key, source_name,
            first_seen_at, last_seen_at, scan_count
        ) VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(listing_id, source_type, target_key) DO UPDATE SET
            source_name = excluded.source_name,
            last_seen_at = excluded.last_seen_at,
            scan_count = model_deal_listing_sources.scan_count + 1
        """,
        (
            str(listing_id),
            source["source_type"],
            source["target_key"],
            source["source_name"],
            now,
            now,
        ),
    )
    return True


def normalize_listing_to_deal_payload(listing, request_url, meta):
    key_record = {
        "path": meta.get("key_file") or "",
        "proxy_label": meta.get("proxy") or "direct",
    }
    headers = {
        "X-Ratelimit-Limit": meta.get("rate_limit"),
        "X-Ratelimit-Remaining": meta.get("rate_remaining"),
        "X-Ratelimit-Reset": meta.get("rate_reset"),
    }
    return csfloat_listing_fetcher.normalize_listing_for_deal(listing, request_url, key_record, headers)


def should_save_prediction(prediction, config):
    if config.get("save_all_predictions"):
        return True

    verdicts = set(config.get("save_verdicts") or [])
    if prediction.get("verdict") not in verdicts:
        return False

    try:
        roi = float(prediction.get("roi_percent"))
        profit_cents = float(prediction.get("profit_cents"))
        liquidity = float(prediction.get("liquidity_score") or 0)
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(value) for value in (roi, profit_cents, liquidity)):
        return False

    return (
        roi >= float(config.get("min_roi_percent") or 0)
        and profit_cents >= round(float(config.get("min_profit_usd") or 0) * 100)
        and liquidity >= float(config.get("min_liquidity_score") or 0)
    )


def prediction_for_storage(prediction, config):
    """Return a safe persisted snapshot and whether it is an actionable signal.

    Model verdicts and scanner policy are deliberately separate.  A model can
    call a deal ``watch`` while it is still below the configured ROI, profit or
    liquidity floor.  Persisting that verdict unchanged makes the dashboard and
    Telegram treat the rejected deal as actionable.
    """
    stored = dict(prediction)
    eligible = should_save_prediction(prediction, config)
    verdict = str(prediction.get("verdict") or "").strip()
    if eligible or config.get("save_all_predictions"):
        return stored, eligible

    if verdict in VISIBLE_DEAL_VERDICTS:
        reasons = []
        if verdict not in set(config.get("save_verdicts") or []):
            reasons.append("verdict")

        numeric_checks = (
            ("roi_percent", float(config.get("min_roi_percent") or 0)),
            ("profit_cents", round(float(config.get("min_profit_usd") or 0) * 100)),
            ("liquidity_score", float(config.get("min_liquidity_score") or 0)),
        )
        for field, minimum in numeric_checks:
            try:
                value = float(prediction.get(field))
            except (TypeError, ValueError):
                value = math.nan
            if not math.isfinite(value) or value < minimum:
                reasons.append(field)

        stored["scanner_original_verdict"] = verdict
        stored["scanner_filter_reasons"] = reasons or ["policy"]
        stored["verdict"] = "filtered"
    return stored, eligible


def upsert_candidate(conn, listing_data, prediction, source_sticker, meta):
    now = utc_now()
    deal_payload = listing_data["deal_payload"]
    listing_id = listing_data.get("listing_id") or deal_payload.get("listing_id")
    if not listing_id:
        return False

    csfloat_url = f"{CSFLOAT_ITEM_BASE_URL}{listing_id}"
    stickers = deal_payload.get("stickers") or []
    source = target_source_fields(source_sticker)

    conn.execute("""
        INSERT INTO model_deal_candidates (
            listing_id,
            first_seen_at,
            last_seen_at,
            scan_count,
            source_sticker_id,
            source_sticker_name,
            skin_name,
            buy_price_cents,
            predicted_price_cents,
            low_price_cents,
            high_price_cents,
            net_expected_cents,
            profit_cents,
            roi_percent,
            liquidity_score,
            historical_sales_count,
            sales_per_day,
            verdict,
            pricing_method,
            sticker_count,
            sticker_value_cents,
            float_value,
            paint_seed,
            paint_index,
            csfloat_url,
            inspect_link,
            key_file,
            proxy,
            rate_remaining,
            deal_payload_json,
            prediction_json
        )
        VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(listing_id) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            scan_count = model_deal_candidates.scan_count + 1,
            source_sticker_id = excluded.source_sticker_id,
            source_sticker_name = excluded.source_sticker_name,
            skin_name = excluded.skin_name,
            buy_price_cents = excluded.buy_price_cents,
            predicted_price_cents = excluded.predicted_price_cents,
            low_price_cents = excluded.low_price_cents,
            high_price_cents = excluded.high_price_cents,
            net_expected_cents = excluded.net_expected_cents,
            profit_cents = excluded.profit_cents,
            roi_percent = excluded.roi_percent,
            liquidity_score = excluded.liquidity_score,
            historical_sales_count = excluded.historical_sales_count,
            sales_per_day = excluded.sales_per_day,
            verdict = excluded.verdict,
            pricing_method = excluded.pricing_method,
            sticker_count = excluded.sticker_count,
            sticker_value_cents = excluded.sticker_value_cents,
            float_value = excluded.float_value,
            paint_seed = excluded.paint_seed,
            paint_index = excluded.paint_index,
            csfloat_url = excluded.csfloat_url,
            inspect_link = excluded.inspect_link,
            key_file = excluded.key_file,
            proxy = excluded.proxy,
            rate_remaining = excluded.rate_remaining,
            deal_payload_json = excluded.deal_payload_json,
            prediction_json = excluded.prediction_json
    """, (
        str(listing_id),
        now,
        now,
        source["source_sticker_id"],
        source["source_sticker_name"],
        deal_payload.get("name"),
        prediction.get("buy_price_cents"),
        prediction.get("predicted_price_cents"),
        prediction.get("low_price_cents"),
        prediction.get("high_price_cents"),
        prediction.get("net_expected_cents"),
        prediction.get("profit_cents"),
        prediction.get("roi_percent"),
        prediction.get("liquidity_score"),
        prediction.get("historical_sales_count"),
        prediction.get("sales_per_day"),
        prediction.get("verdict"),
        prediction.get("pricing_method"),
        len(stickers),
        prediction.get("sticker_value_cents"),
        deal_payload.get("float_value"),
        deal_payload.get("paint_seed"),
        deal_payload.get("paint_index"),
        csfloat_url,
        deal_payload.get("inspect_link"),
        meta.get("key_file"),
        meta.get("proxy"),
        meta.get("rate_remaining"),
        json.dumps(deal_payload, ensure_ascii=False),
        json.dumps(prediction, ensure_ascii=False),
    ))
    conn.execute(
        """
        UPDATE model_deal_candidates
        SET source_type = ?, source_name = ?, target_key = ?
        WHERE listing_id = ?
        """,
        (
            source["source_type"],
            source["source_name"],
            source["target_key"],
            str(listing_id),
        ),
    )
    return True


def upsert_model_prediction(
    conn,
    listing_data,
    prediction,
    source_sticker,
    meta,
    model_name,
    *,
    refresh_observation=True,
    observed_at=None,
):
    now = str(observed_at or utc_now())
    deal_payload = listing_data["deal_payload"]
    listing_id = listing_data.get("listing_id") or deal_payload.get("listing_id")
    if not listing_id:
        return False

    stickers = deal_payload.get("stickers") or []
    csfloat_url = f"{CSFLOAT_ITEM_BASE_URL}{listing_id}"
    source = target_source_fields(source_sticker)

    conn.execute("""
        INSERT INTO model_deal_predictions (
            listing_id,
            model_name,
            first_seen_at,
            last_seen_at,
            scan_count,
            source_sticker_id,
            source_sticker_name,
            skin_name,
            buy_price_cents,
            predicted_price_cents,
            low_price_cents,
            high_price_cents,
            net_expected_cents,
            profit_cents,
            roi_percent,
            liquidity_score,
            historical_sales_count,
            sales_per_day,
            verdict,
            pricing_method,
            sticker_count,
            sticker_value_cents,
            float_value,
            paint_seed,
            paint_index,
            listing_created_at,
            item_icon_url,
            screenshot_url,
            csfloat_url,
            inspect_link,
            key_file,
            proxy,
            rate_remaining,
            deal_payload_json,
            prediction_json
        )
        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(listing_id, model_name) DO UPDATE SET
            last_seen_at = CASE
                WHEN ? THEN excluded.last_seen_at
                ELSE model_deal_predictions.last_seen_at
            END,
            scan_count = CASE
                WHEN ? THEN model_deal_predictions.scan_count + 1
                ELSE model_deal_predictions.scan_count
            END,
            source_sticker_id = excluded.source_sticker_id,
            source_sticker_name = excluded.source_sticker_name,
            skin_name = excluded.skin_name,
            buy_price_cents = excluded.buy_price_cents,
            predicted_price_cents = excluded.predicted_price_cents,
            low_price_cents = excluded.low_price_cents,
            high_price_cents = excluded.high_price_cents,
            net_expected_cents = excluded.net_expected_cents,
            profit_cents = excluded.profit_cents,
            roi_percent = excluded.roi_percent,
            liquidity_score = excluded.liquidity_score,
            historical_sales_count = excluded.historical_sales_count,
            sales_per_day = excluded.sales_per_day,
            verdict = excluded.verdict,
            pricing_method = excluded.pricing_method,
            sticker_count = excluded.sticker_count,
            sticker_value_cents = excluded.sticker_value_cents,
            float_value = excluded.float_value,
            paint_seed = excluded.paint_seed,
            paint_index = excluded.paint_index,
            listing_created_at = excluded.listing_created_at,
            item_icon_url = excluded.item_icon_url,
            screenshot_url = excluded.screenshot_url,
            csfloat_url = excluded.csfloat_url,
            inspect_link = excluded.inspect_link,
            key_file = excluded.key_file,
            proxy = excluded.proxy,
            rate_remaining = excluded.rate_remaining,
            deal_payload_json = excluded.deal_payload_json,
            prediction_json = excluded.prediction_json
    """, (
        str(listing_id),
        model_name,
        now,
        now,
        source["source_sticker_id"],
        source["source_sticker_name"],
        prediction.get("name") or deal_payload.get("name"),
        prediction.get("buy_price_cents"),
        prediction.get("predicted_price_cents"),
        prediction.get("low_price_cents"),
        prediction.get("high_price_cents"),
        prediction.get("net_expected_cents"),
        prediction.get("profit_cents"),
        prediction.get("roi_percent"),
        prediction.get("liquidity_score"),
        prediction.get("historical_sales_count"),
        prediction.get("sales_per_day"),
        prediction.get("verdict"),
        prediction.get("pricing_method"),
        len(stickers),
        prediction.get("sticker_value_cents"),
        deal_payload.get("float_value"),
        deal_payload.get("paint_seed"),
        deal_payload.get("paint_index"),
        deal_payload.get("created_at") or deal_payload.get("listed_at"),
        deal_payload.get("item_icon_url"),
        deal_payload.get("screenshot_url"),
        csfloat_url,
        deal_payload.get("inspect_link"),
        meta.get("key_file"),
        meta.get("proxy"),
        meta.get("rate_remaining"),
        json.dumps(deal_payload, ensure_ascii=False, default=str),
        json.dumps(prediction, ensure_ascii=False, default=str),
        int(bool(refresh_observation)),
        int(bool(refresh_observation)),
    ))
    conn.execute(
        """
        UPDATE model_deal_predictions
        SET source_type = ?, source_name = ?, target_key = ?
        WHERE listing_id = ? AND model_name = ?
        """,
        (
            source["source_type"],
            source["source_name"],
            source["target_key"],
            str(listing_id),
            model_name,
        ),
    )
    return True


def enabled_model_names(config):
    names = []
    for name in config.get("enabled_models") or ["almaz", "igor"]:
        name = str(name).strip().lower()
        if name in MODEL_RUNNERS and name not in names:
            names.append(name)
    return names


def enabled_model_names_for_item_kind(item_kind, config):
    """Return only models whose artifact contract matches ``item_kind``."""

    if item_kind == item_classification.ITEM_KIND_WEAPON_SKIN:
        return enabled_model_names(config)
    if item_kind == item_classification.ITEM_KIND_KNIFE:
        return [KNIFE_MODEL_NAME]
    if item_kind == item_classification.ITEM_KIND_GLOVE:
        return [GLOVE_MODEL_NAME]
    return []


def enforce_deal_policy(conn, config):
    """Hide stored signals that no longer satisfy the scanner configuration."""
    if config.get("save_all_predictions"):
        return {"predictions_filtered": 0, "candidates_removed": 0}

    allowed = sorted(
        VISIBLE_DEAL_VERDICTS.intersection(
            str(value).strip() for value in (config.get("save_verdicts") or [])
        )
    )
    allowed_sql = ", ".join("?" for _ in allowed)
    verdict_rejected_sql = f"verdict NOT IN ({allowed_sql})" if allowed else "1 = 1"
    minimum_roi = float(config.get("min_roi_percent") or 0)
    minimum_profit = round(float(config.get("min_profit_usd") or 0) * 100)
    minimum_liquidity = float(config.get("min_liquidity_score") or 0)
    policy_sql = f"""
        verdict IN ('good', 'watch')
        AND (
            {verdict_rejected_sql}
            OR roi_percent IS NULL
            OR roi_percent < ?
            OR profit_cents IS NULL
            OR profit_cents < ?
            OR COALESCE(liquidity_score, 0) < ?
        )
    """
    params = [*allowed, minimum_roi, minimum_profit, minimum_liquidity]
    filtered = conn.execute(
        f"UPDATE model_deal_predictions SET verdict = 'filtered' WHERE {policy_sql}",
        params,
    )
    removed = conn.execute(
        f"DELETE FROM model_deal_candidates WHERE {policy_sql}",
        params,
    )
    conn.commit()
    return {
        "predictions_filtered": max(int(filtered.rowcount), 0),
        "candidates_removed": max(int(removed.rowcount), 0),
    }


def persist_model_result(
    conn,
    listing_data,
    prediction,
    source_sticker,
    meta,
    model_name,
    eligible,
):
    """Persist one model result atomically with its legacy Almaz candidate."""
    listing_id = str(
        listing_data.get("listing_id")
        or listing_data.get("deal_payload", {}).get("listing_id")
        or ""
    )
    observed_at = utc_now()
    conn.execute("SAVEPOINT model_prediction_write")
    try:
        saved = upsert_model_prediction(
            conn,
            listing_data,
            prediction,
            source_sticker,
            meta,
            model_name,
            observed_at=observed_at,
        )
        if (
            eligible
            and saved
            and str(prediction.get("verdict") or "") in VISIBLE_DEAL_VERDICTS
        ):
            deal_intelligence.record_recommendation(
                conn,
                listing_data,
                prediction,
                model_name,
                observed_at=observed_at,
            )
        if model_name == "almaz":
            if eligible:
                upsert_candidate(conn, listing_data, prediction, source_sticker, meta)
            elif listing_id:
                conn.execute(
                    "DELETE FROM model_deal_candidates WHERE listing_id = ?",
                    (listing_id,),
                )
        conn.execute("RELEASE SAVEPOINT model_prediction_write")
        return saved
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT model_prediction_write")
        conn.execute("RELEASE SAVEPOINT model_prediction_write")
        raise


def invalidate_model_signal(conn, listing_id, model_name):
    """Make an old positive signal non-actionable after a fresh model failure."""
    if not listing_id:
        return 0

    conn.execute("SAVEPOINT invalidate_model_signal")
    try:
        cursor = conn.execute(
            """
            UPDATE model_deal_predictions
            SET
                last_seen_at = ?,
                scan_count = scan_count + 1,
                predicted_price_cents = NULL,
                low_price_cents = NULL,
                high_price_cents = NULL,
                net_expected_cents = NULL,
                profit_cents = NULL,
                roi_percent = NULL,
                liquidity_score = NULL,
                historical_sales_count = NULL,
                sales_per_day = NULL,
                pricing_method = NULL,
                verdict = 'error'
            WHERE listing_id = ? AND model_name = ?
            """,
            (utc_now(), str(listing_id), model_name),
        )
        if model_name == "almaz":
            conn.execute(
                "DELETE FROM model_deal_candidates WHERE listing_id = ?",
                (str(listing_id),),
            )
        conn.execute("RELEASE SAVEPOINT invalidate_model_signal")
        return max(int(cursor.rowcount), 0)
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT invalidate_model_signal")
        conn.execute("RELEASE SAVEPOINT invalidate_model_signal")
        raise


def save_scan_log(
    conn,
    sticker,
    request_url,
    returned,
    saved,
    meta=None,
    error=None,
    *,
    commit=True,
):
    meta = meta or {}
    source = target_source_fields(sticker)
    cursor = conn.execute("""
        INSERT INTO model_deal_scans (
            scanned_at,
            sticker_id,
            sticker_name,
            request_url,
            listings_returned,
            candidates_saved,
            key_file,
            proxy,
            rate_remaining,
            error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        utc_now(),
        source["source_sticker_id"],
        source["source_sticker_name"],
        request_url,
        returned,
        saved,
        meta.get("key_file"),
        meta.get("proxy"),
        meta.get("rate_remaining"),
        error,
    ))
    conn.execute(
        """
        UPDATE model_deal_scans
        SET source_type = ?, source_name = ?, target_key = ?
        WHERE id = ?
        """,
        (
            source["source_type"],
            source["source_name"],
            source["target_key"],
            cursor.lastrowid,
        ),
    )
    if commit:
        conn.commit()


def expire_stale_predictions(conn, stale_hours):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(stale_hours))).isoformat()
    cursor = conn.execute(
        """
        UPDATE model_deal_predictions
        SET verdict = 'expired'
        WHERE COALESCE(verdict, '') != 'expired'
            AND (last_seen_at IS NULL OR last_seen_at < ?)
        """,
        (cutoff,),
    )
    conn.execute(
        "DELETE FROM model_deal_candidates WHERE last_seen_at IS NULL OR last_seen_at < ?",
        (cutoff,),
    )
    conn.commit()
    return max(int(cursor.rowcount), 0)


def maintain_deal_intelligence(conn):
    """Run bounded local maintenance; this never performs external requests."""

    outcomes = deal_intelligence.reconcile_outcomes(conn, max_rows=5_000)
    pruned = deal_intelligence.prune_stale_intelligence(conn)
    conn.commit()
    return {**outcomes, "annotations_pruned": pruned}


def _check_scan_control(should_continue=None, heartbeat=None):
    if should_continue is not None and not should_continue():
        raise ScanControlInterrupted("scanner paused")
    if heartbeat is not None:
        heartbeat()


def _rate_remaining_exhausted(meta):
    if (meta or {}).get("rate_exhausted") is True:
        return True
    value = (meta or {}).get("rate_remaining")
    try:
        return value is not None and int(value) <= 0
    except (TypeError, ValueError):
        return False


def _knife_model_scan_status():
    """Return a small local readiness snapshot without exposing model paths."""

    try:
        knife_model_runtime.load_model()
    except knife_model_runtime.KnifeModelUnavailable as exc:
        return {
            "model_name": KNIFE_MODEL_NAME,
            "model_available": False,
            "model_status": "unavailable",
            "model_error_type": type(exc).__name__,
        }
    return {
        "model_name": KNIFE_MODEL_NAME,
        "model_available": True,
        "model_status": "ready",
    }


def _glove_model_scan_status():
    """Return glove readiness without falling back to another item model."""

    try:
        glove_model_runtime.load_model()
    except glove_model_runtime.GloveModelUnavailable as exc:
        return {
            "model_name": GLOVE_MODEL_NAME,
            "model_available": False,
            "model_status": "unavailable",
            "model_error_type": type(exc).__name__,
        }
    return {
        "model_name": GLOVE_MODEL_NAME,
        "model_available": True,
        "model_status": "ready",
    }


def build_scanner_target_preview(
    conn,
    profile,
    *,
    config=None,
    target_control=None,
    search="",
    limit=50,
    offset=0,
):
    """Return a local-only preview of what one scanner profile will inspect."""

    config = validate_config(dict(config or load_config()))
    target_control = target_control or load_scanner_target_control(
        config,
        create_if_missing=False,
    )
    profile = str(profile or "").strip()

    if profile == scanner_target_profiles.PROFILE_STICKER_GUNS:
        candidates = discover_sticker_targets(
            conn,
            config,
            target_control,
            apply_control=False,
        )
        pool_limit = int(config["max_sticker_targets"])
        budget = {
            "targets_per_cycle": config["stickers_per_cycle"],
            "listings_per_target": config["listings_per_sticker"],
            "pages_per_target": config["listing_pages_per_sticker"],
        }
        scan_ready = bool(config.get("enabled_models"))
        blocked_reason = None if scan_ready else "weapon_models_disabled"
    elif profile == scanner_target_profiles.PROFILE_LIQUID_UNDER_10:
        candidates = discover_liquid_skin_targets(
            conn,
            config,
            target_control,
            apply_control=False,
        )
        pool_limit = int(config["max_liquid_skin_targets"])
        budget = {
            "targets_per_cycle": config["liquid_skins_per_cycle"],
            "listings_per_target": config["listings_per_liquid_skin"],
            "pages_per_target": config["listing_pages_per_liquid_skin"],
        }
        scan_ready = bool(config.get("enabled_models"))
        blocked_reason = None if scan_ready else "weapon_models_disabled"
    elif profile == scanner_target_profiles.PROFILE_KNIVES:
        candidates = discover_knife_targets(
            conn,
            config,
            target_control,
            apply_control=False,
        )
        pool_limit = None
        budget = {
            "targets_per_cycle": config["knives_per_cycle"],
            "listings_per_target": config["listings_per_knife"],
            "pages_per_target": config["listing_pages_per_knife"],
        }
        status = _knife_model_scan_status()
        scan_ready = bool(status["model_available"])
        blocked_reason = None if scan_ready else "dedicated_knife_model_unavailable"
    elif profile == scanner_target_profiles.PROFILE_GLOVES:
        candidates = discover_glove_targets(
            conn,
            config,
            target_control,
            apply_control=False,
        )
        pool_limit = None
        budget = {
            "targets_per_cycle": config["gloves_per_cycle"],
            "listings_per_target": config["listings_per_glove"],
            "pages_per_target": config["listing_pages_per_glove"],
        }
        status = _glove_model_scan_status()
        scan_ready = bool(status["model_available"])
        blocked_reason = None if scan_ready else "dedicated_glove_model_unavailable"
    else:
        raise ValueError(f"unknown scanner target profile: {profile}")

    return scanner_target_profiles.build_profile_preview(
        profile,
        candidates,
        target_control,
        recommended_limit=pool_limit,
        budget=budget,
        search=search,
        limit=limit,
        offset=offset,
        scan_ready=scan_ready,
        blocked_reason=blocked_reason,
    )


def _scan_target(
    conn,
    sticker,
    config,
    *,
    should_continue=None,
    heartbeat=None,
    cycle_seen_listing_ids=None,
):
    # A previous caller may have left a transaction open.  Historically this
    # function committed it at the end through ``save_scan_log``.  Commit it up
    # front instead so network work and model inference never extend a SQLite
    # writer lock.
    if conn.in_transaction:
        conn.commit()

    _check_scan_control(should_continue, heartbeat)
    source = target_source_fields(sticker)
    is_liquid_skin = source["source_type"] == SOURCE_LIQUID_SKIN
    is_knife = source["source_type"] == SOURCE_KNIFE
    is_glove = source["source_type"] == SOURCE_GLOVE
    if is_knife:
        # Direct calls are held to the same catalog contract as discovery, so
        # a hand-crafted star-prefixed name cannot spend quota or reach a model.
        if not _catalog_has_exact_knife(conn, sticker.get("market_hash_name")):
            meta = {
                "model_name": KNIFE_MODEL_NAME,
                "model_available": False,
                "model_status": "invalid_catalog_target",
            }
            save_scan_log(
                conn,
                sticker,
                "",
                0,
                0,
                meta,
                "knife_target_not_in_local_catalog",
            )
            return 0, 0, meta, {KNIFE_MODEL_NAME: 0}
        meta = _knife_model_scan_status()
        if not meta["model_available"]:
            # Missing knife artifacts are an expected warm-up state.  Record
            # it as scanner provenance, but do not make a network request and
            # never fall back to a weapon-skin model.
            save_scan_log(
                conn,
                sticker,
                "",
                0,
                0,
                meta,
                "knife_model_unavailable",
            )
            return 0, 0, meta, {KNIFE_MODEL_NAME: 0}
        per_page = int(config.get("listings_per_knife", 10))
        page_count = int(config.get("listing_pages_per_knife", 1))
        sort_by = config.get("knife_sort_by", "lowest_price")
    elif is_glove:
        if not _catalog_has_exact_glove(conn, sticker.get("market_hash_name")):
            meta = {
                "model_name": GLOVE_MODEL_NAME,
                "model_available": False,
                "model_status": "invalid_catalog_target",
            }
            save_scan_log(
                conn,
                sticker,
                "",
                0,
                0,
                meta,
                "glove_target_not_in_local_catalog",
            )
            return 0, 0, meta, {GLOVE_MODEL_NAME: 0}
        meta = _glove_model_scan_status()
        if not meta["model_available"]:
            save_scan_log(
                conn,
                sticker,
                "",
                0,
                0,
                meta,
                "glove_model_unavailable",
            )
            return 0, 0, meta, {GLOVE_MODEL_NAME: 0}
        per_page = int(config.get("listings_per_glove", 10))
        page_count = int(config.get("listing_pages_per_glove", 1))
        sort_by = config.get("glove_sort_by", "lowest_price")
    elif is_liquid_skin:
        target_kind = item_classification.classify_market_hash_name(
            sticker.get("market_hash_name"),
            conn=conn,
        )
        if target_kind != item_classification.ITEM_KIND_WEAPON_SKIN:
            meta = {
                "model_available": False,
                "model_status": "invalid_item_kind",
                "item_kind": target_kind,
            }
            save_scan_log(
                conn,
                sticker,
                "",
                0,
                0,
                meta,
                "liquid_target_is_not_weapon_skin",
            )
            return 0, 0, meta, {
                name: 0 for name in enabled_model_names(config)
            }
        per_page = int(config.get("listings_per_liquid_skin", 20))
        page_count = int(config.get("listing_pages_per_liquid_skin", 1))
        sort_by = config.get("liquid_skin_sort_by", "lowest_price")
    else:
        per_page = int(config.get("listings_per_sticker", 50))
        page_count = int(config.get("listing_pages_per_sticker", 1))
        sort_by = config.get("sort_by", "most_recent")

    listings = []
    seen_listing_ids = set()
    shared_listing_ids = (
        cycle_seen_listing_ids if cycle_seen_listing_ids is not None else set()
    )
    cursor = None
    meta = {}
    request_url = ""
    fetch_errors = []
    rate_limited_during_fetch = False
    for page_index in range(page_count):
        _check_scan_control(should_continue, heartbeat)
        try:
            if is_knife or is_glove:
                # The shared client still enforces exact market-name, cursor,
                # page-size and sort validation.  Knives intentionally omit
                # the cheap-skin $10 ceiling.
                page, next_cursor, page_meta, page_url = (
                    csfloat_listing_fetcher.fetch_active_listings(
                        market_hash_name=sticker["market_hash_name"],
                        limit=per_page,
                        sort_by=sort_by,
                        cursor=cursor,
                    )
                )
            elif is_liquid_skin:
                page, next_cursor, page_meta, page_url = (
                    csfloat_listing_fetcher.fetch_active_listings_for_skin(
                        sticker["market_hash_name"],
                        limit=per_page,
                        sort_by=sort_by,
                        cursor=cursor,
                        max_price=config.get("liquid_skin_max_price_cents", 1000),
                    )
                )
            else:
                page, next_cursor, page_meta, page_url = (
                    csfloat_listing_fetcher.fetch_active_listings_for_sticker(
                        sticker["sticker_id"],
                        limit=per_page,
                        sort_by=sort_by,
                        cursor=cursor,
                    )
                )
        except Exception as exc:
            if page_index == 0:
                raise
            fetch_errors.append(f"page {page_index + 1}: {exc}")
            if isinstance(exc, csfloat_listing_fetcher.CsfloatRateLimitError):
                rate_limited_during_fetch = True
            break
        if not request_url:
            request_url = page_url
        meta = page_meta
        for listing in page:
            listing_id = ""
            if isinstance(listing, dict):
                listing_id = str(
                    listing.get("id") or listing.get("listing_id") or ""
                )
            dedupe_key = listing_id or json.dumps(listing, sort_keys=True, default=str)
            if dedupe_key in seen_listing_ids:
                continue
            seen_listing_ids.add(dedupe_key)
            listings.append((listing, page_url, page_meta))
        cursor = next_cursor
        if _rate_remaining_exhausted(meta):
            if cursor:
                fetch_errors.append("rate limit exhausted before next page")
            break
        if not cursor or len(page) < per_page:
            break
    if rate_limited_during_fetch:
        meta = dict(meta)
        meta["rate_exhausted"] = True

    model_names = (
        [KNIFE_MODEL_NAME]
        if is_knife
        else ([GLOVE_MODEL_NAME] if is_glove else enabled_model_names(config))
    )
    profile_item_kind = (
        item_classification.ITEM_KIND_KNIFE
        if is_knife
        else (
            item_classification.ITEM_KIND_GLOVE
            if is_glove
            else (
                item_classification.ITEM_KIND_WEAPON_SKIN
                if is_liquid_skin
                else None
            )
        )
    )
    model_saved = {name: 0 for name in model_names}
    actions = []
    source_observations = set()
    for listing, listing_request_url, listing_meta in listings:
        _check_scan_control(should_continue, heartbeat)
        try:
            listing_data = normalize_listing_to_deal_payload(
                listing,
                listing_request_url,
                listing_meta,
            )
        except Exception as exc:  # noqa: BLE001 - isolate malformed external listings
            actions.append({"kind": "error", "error": f"normalize: {exc}"})
            continue
        deal_payload = listing_data["deal_payload"]
        if not deal_payload.get("buy_price_cents"):
            continue
        if is_liquid_skin or is_knife or is_glove:
            raw_buy_price = deal_payload["buy_price_cents"]
            if isinstance(raw_buy_price, bool):
                continue
            try:
                numeric_buy_price = float(raw_buy_price)
            except (TypeError, ValueError):
                continue
            if (
                deal_payload.get("name") != sticker.get("market_hash_name")
                or deal_payload.get("listing_type") != "buy_now"
                or not math.isfinite(numeric_buy_price)
                or not numeric_buy_price.is_integer()
                or numeric_buy_price < 1
                or (
                    is_liquid_skin
                    and numeric_buy_price
                    > int(config.get("liquid_skin_max_price_cents", 1000))
                )
            ):
                # The API filters are authoritative, but this client-side
                # check fails closed if an upstream response ever violates an
                # exact-name/buy-now contract (and the cheap profile's $10 cap).
                continue

        listing_id = str(listing_data.get("listing_id") or deal_payload.get("listing_id") or "")
        if listing_id:
            source_observations.add(listing_id)
            if listing_id in shared_listing_ids:
                continue
            shared_listing_ids.add(listing_id)
        item_kind = item_classification.classify_market_hash_name(
            deal_payload.get("name"),
            conn=conn,
        )
        category_model_names = ITEM_KIND_MODEL_NAMES.get(item_kind, frozenset())
        listing_model_names = enabled_model_names_for_item_kind(item_kind, config)
        if profile_item_kind is not None and item_kind != profile_item_kind:
            listing_model_names = []
            category_model_names = frozenset()
            actions.append(
                {
                    "kind": "error",
                    "error": (
                        f"{listing_id or 'unknown'}: expected {profile_item_kind}, "
                        f"got {item_kind}"
                    ),
                }
            )
        elif not listing_model_names:
            actions.append(
                {
                    "kind": "error",
                    "error": (
                        f"{listing_id or 'unknown'}: no model route for "
                        f"item kind {item_kind}"
                    ),
                }
            )

        # A listing can outlive a catalog correction.  Remove any previously
        # stored signal from a now-incompatible model family in the same
        # transaction as the fresh observation.
        for stale_model_name in sorted(
            ALL_ROUTED_MODEL_NAMES - category_model_names
        ):
            actions.append(
                {
                    "kind": "invalidate_stale",
                    "listing_id": listing_id,
                    "model_name": stale_model_name,
                }
            )
        for model_name in listing_model_names:
            model_saved.setdefault(model_name, 0)
            _check_scan_control(should_continue, heartbeat)
            try:
                runner = (
                    knife_model_runtime.predict_deal
                    if model_name == KNIFE_MODEL_NAME
                    else (
                        glove_model_runtime.predict_deal
                        if model_name == GLOVE_MODEL_NAME
                        else MODEL_RUNNERS[model_name]
                    )
                )
                prediction = runner(deal_payload, conn)
            except Exception as exc:  # noqa: BLE001 - isolate independent model failures
                actions.append({
                    "kind": "invalidate",
                    "listing_id": listing_id,
                    "model_name": model_name,
                    "error": f"{listing_id or 'unknown'}:{model_name}: {exc}",
                })
                continue

            try:
                stored_prediction, eligible = prediction_for_storage(prediction, config)
            except Exception as exc:  # noqa: BLE001 - keep other model results usable
                actions.append({
                    "kind": "invalidate",
                    "listing_id": listing_id,
                    "model_name": model_name,
                    "error": f"{listing_id or 'unknown'}:{model_name}:store: {exc}",
                })
                continue

            actions.append({
                "kind": "persist",
                "listing_id": listing_id,
                "model_name": model_name,
                "listing_data": listing_data,
                "prediction": stored_prediction,
                "eligible": eligible,
                "meta": listing_meta,
            })

    _check_scan_control(should_continue, heartbeat)
    errors = []
    historical_profile = deal_intelligence.load_history_profile(conn)
    touched_listing_ids = set()
    conn.execute("BEGIN IMMEDIATE")
    try:
        source_observed_at = utc_now()
        for listing_id in sorted(source_observations):
            upsert_listing_source(
                conn,
                listing_id,
                sticker,
                observed_at=source_observed_at,
            )
        for action in actions:
            kind = action["kind"]
            if kind == "error":
                errors.append(action["error"])
                continue

            listing_id = action["listing_id"]
            model_name = action["model_name"]
            if kind in {"invalidate", "invalidate_stale"}:
                if kind == "invalidate":
                    errors.append(action["error"])
                try:
                    invalidate_model_signal(conn, listing_id, model_name)
                    if listing_id:
                        touched_listing_ids.add(listing_id)
                except Exception as invalidate_exc:  # noqa: BLE001 - preserve the scan log
                    errors.append(
                        f"{listing_id or 'unknown'}:{model_name}:invalidate: {invalidate_exc}"
                    )
                continue

            try:
                saved = persist_model_result(
                    conn,
                    action["listing_data"],
                    action["prediction"],
                    sticker,
                    action["meta"],
                    model_name,
                    action["eligible"],
                )
            except Exception as exc:  # noqa: BLE001 - keep other model results usable
                errors.append(f"{listing_id or 'unknown'}:{model_name}:store: {exc}")
                try:
                    invalidate_model_signal(conn, listing_id, model_name)
                except Exception as invalidate_exc:  # noqa: BLE001 - preserve the scan log
                    errors.append(
                        f"{listing_id or 'unknown'}:{model_name}:invalidate: {invalidate_exc}"
                    )
                continue
            if action["eligible"] and saved:
                model_saved[model_name] += 1
            if listing_id:
                touched_listing_ids.add(listing_id)

        for listing_id in sorted(touched_listing_ids):
            conn.execute("SAVEPOINT deal_intelligence_refresh")
            try:
                deal_intelligence.refresh_listing_intelligence(
                    conn,
                    listing_id,
                    profile=historical_profile,
                )
                conn.execute("RELEASE SAVEPOINT deal_intelligence_refresh")
            except Exception as exc:  # noqa: BLE001 - fail closed for this annotation
                conn.execute("ROLLBACK TO SAVEPOINT deal_intelligence_refresh")
                conn.execute("RELEASE SAVEPOINT deal_intelligence_refresh")
                conn.execute(
                    "DELETE FROM model_deal_intelligence WHERE listing_id = ?",
                    (listing_id,),
                )
                errors.append(f"{listing_id}:intelligence: {exc}")

        all_errors = fetch_errors + errors
        error_text = "; ".join(all_errors[:10]) if all_errors else None
        if len(all_errors) > 10:
            error_text += f"; +{len(all_errors) - 10} more"
        total_saved = sum(model_saved.values())
        save_scan_log(
            conn,
            sticker,
            request_url,
            len(listings),
            total_saved,
            meta,
            error_text,
            commit=False,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    return len(listings), total_saved, meta, model_saved


def scan_sticker(
    conn,
    sticker,
    config,
    *,
    should_continue=None,
    heartbeat=None,
    cycle_seen_listing_ids=None,
):
    return _scan_target(
        conn,
        sticker,
        config,
        should_continue=should_continue,
        heartbeat=heartbeat,
        cycle_seen_listing_ids=cycle_seen_listing_ids,
    )


def scan_liquid_skin(
    conn,
    skin,
    config,
    *,
    should_continue=None,
    heartbeat=None,
    cycle_seen_listing_ids=None,
):
    target = dict(skin)
    target["source_type"] = SOURCE_LIQUID_SKIN
    target.setdefault("source_name", target.get("market_hash_name"))
    target.setdefault(
        "target_key", f"{SOURCE_LIQUID_SKIN}:{target.get('market_hash_name', '')}"
    )
    return _scan_target(
        conn,
        target,
        config,
        should_continue=should_continue,
        heartbeat=heartbeat,
        cycle_seen_listing_ids=cycle_seen_listing_ids,
    )


def scan_knife(
    conn,
    knife,
    config,
    *,
    should_continue=None,
    heartbeat=None,
    cycle_seen_listing_ids=None,
):
    target = dict(knife)
    target["source_type"] = SOURCE_KNIFE
    target.setdefault("source_name", target.get("market_hash_name"))
    target.setdefault(
        "target_key", f"{SOURCE_KNIFE}:{target.get('market_hash_name', '')}"
    )
    return _scan_target(
        conn,
        target,
        config,
        should_continue=should_continue,
        heartbeat=heartbeat,
        cycle_seen_listing_ids=cycle_seen_listing_ids,
    )


def scan_glove(
    conn,
    glove,
    config,
    *,
    should_continue=None,
    heartbeat=None,
    cycle_seen_listing_ids=None,
):
    target = dict(glove)
    target["source_type"] = SOURCE_GLOVE
    target.setdefault("source_name", target.get("market_hash_name"))
    target.setdefault(
        "target_key", f"{SOURCE_GLOVE}:{target.get('market_hash_name', '')}"
    )
    return _scan_target(
        conn,
        target,
        config,
        should_continue=should_continue,
        heartbeat=heartbeat,
        cycle_seen_listing_ids=cycle_seen_listing_ids,
    )


class RuntimeStatusReporter:
    """Coalesce heartbeat writes while persisting every state transition."""

    def __init__(self, *, monotonic_fn=None):
        self.monotonic_fn = monotonic_fn or time.monotonic
        try:
            self.state = scanner_control.get_scanner_status()
        except (OSError, scanner_control.ScannerControlError):
            self.state = {
                "cycles_completed": 0,
                "cycle_number": 0,
                "cycle_targets_total": 0,
                "cycle_targets_scanned": 0,
                "cycle_listings_returned": 0,
                "cycle_candidates_saved": 0,
            }
        self.last_write_monotonic = float("-inf")
        self.write_failure_reported = False

    def report(self, process_state=None, *, force=False, **changes):
        if process_state is not None:
            changes["process_state"] = process_state
        changes["pid"] = os.getpid()
        material_change = any(self.state.get(key) != value for key, value in changes.items())
        now_monotonic = self.monotonic_fn()
        if (
            not force
            and not material_change
            and now_monotonic - self.last_write_monotonic < STATUS_HEARTBEAT_SECONDS
        ):
            return self.state

        self.state.update(changes)
        self.state["heartbeat_at"] = utc_now()
        status_payload = {
            key: value
            for key, value in self.state.items()
            if key in scanner_control.STATUS_FILE_KEYS and key != "schema_version"
        }
        try:
            self.state = scanner_control.update_scanner_status(**status_payload)
            self.write_failure_reported = False
        except (OSError, scanner_control.ScannerControlError):
            if not self.write_failure_reported:
                print("Warning: scanner runtime status could not be persisted", flush=True)
                self.write_failure_reported = True
        self.last_write_monotonic = now_monotonic
        return self.state


def read_live_control(default_interval_seconds):
    try:
        return scanner_control.get_scanner_control(
            default_interval_seconds=default_interval_seconds,
        )
    except (OSError, scanner_control.ScannerControlError):
        return {
            "schema_version": scanner_control.CONTROL_SCHEMA_VERSION,
            "enabled": False,
            "interval_seconds": default_interval_seconds,
            "updated_at": None,
            "status": "paused",
            "error": "control_state_unavailable",
        }


def next_scan_timestamp(cycle_started_epoch, interval_seconds):
    return datetime.fromtimestamp(
        cycle_started_epoch + interval_seconds,
        tz=timezone.utc,
    ).isoformat()


def wait_for_scan_permission(
    cycle_started_monotonic,
    cycle_started_epoch,
    *,
    default_interval_seconds,
    stop_event,
    reporter=None,
    control_reader=None,
    monotonic_fn=None,
):
    """Wait responsively for enabled state and the next start-based deadline."""

    read_control = control_reader or read_live_control
    get_monotonic = monotonic_fn or time.monotonic
    while not stop_event.is_set():
        control = read_control(default_interval_seconds)
        if not control.get("enabled"):
            if reporter is not None:
                paused_changes = {"next_scan_at": None}
                if control.get("error"):
                    paused_changes["last_error"] = control["error"]
                reporter.report("paused", **paused_changes)
            if stop_event.wait(CONTROL_POLL_SECONDS):
                return None
            continue

        if cycle_started_monotonic is None:
            return control
        interval_seconds = int(control["interval_seconds"])
        remaining = cycle_started_monotonic + interval_seconds - get_monotonic()
        if remaining <= 0:
            return control
        if reporter is not None:
            reporter.report(
                "sleeping",
                next_scan_at=next_scan_timestamp(cycle_started_epoch, interval_seconds),
            )
        if stop_event.wait(min(CONTROL_POLL_SECONDS, remaining)):
            return None
    return None


def install_signal_handlers(stop_event=None):
    event = stop_event or _STOP_EVENT

    def request_stop(_signum, _frame):
        event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def run_forever(*, stop_event=None):
    event = stop_event or _STOP_EVENT
    reporter = RuntimeStatusReporter()
    fatal_error = None
    reporter.report(
        "starting",
        force=True,
        next_scan_at=None,
        last_error=None,
        cycle_started_at=None,
        cycle_targets_total=0,
        cycle_targets_scanned=0,
        cycle_listings_returned=0,
        cycle_candidates_saved=0,
    )

    try:
        config = load_config()
        target_control = load_scanner_target_control(
            config,
            create_if_missing=False,
        )
        profile_enabled = {
            name: scanner_target_profiles.profile_settings(
                target_control,
                name,
            )["enabled"]
            for name in scanner_target_profiles.PROFILE_ORDER
        }
        default_interval = int(config.get("scan_interval_seconds") or 600)
        stickers_per_cycle = (
            int(config.get("stickers_per_cycle") or 5)
            if profile_enabled[scanner_target_profiles.PROFILE_STICKER_GUNS]
            else 0
        )
        liquid_skins_per_cycle = (
            int(config.get("liquid_skins_per_cycle") or 1)
            if profile_enabled[scanner_target_profiles.PROFILE_LIQUID_UNDER_10]
            else 0
        )
        knives_per_cycle = (
            int(config.get("knives_per_cycle") or 1)
            if profile_enabled[scanner_target_profiles.PROFILE_KNIVES]
            else 0
        )
        gloves_per_cycle = (
            int(config.get("gloves_per_cycle") or 1)
            if profile_enabled[scanner_target_profiles.PROFILE_GLOVES]
            else 0
        )
        knife_rediscovery_cycles = int(
            config.get("knife_rediscovery_cycles") or 6
        )
        glove_rediscovery_cycles = int(
            config.get("glove_rediscovery_cycles") or 6
        )

        with connect_db() as conn:
            init_db(conn)
            cleanup = enforce_deal_policy(conn, config)
            if cleanup["predictions_filtered"] or cleanup["candidates_removed"]:
                print(
                    "Applied deal policy: "
                    f"filtered {cleanup['predictions_filtered']} predictions, "
                    f"removed {cleanup['candidates_removed']} legacy candidates",
                    flush=True,
                )
            expired = expire_stale_predictions(conn, config.get("stale_prediction_hours", 24))
            if expired:
                print(f"Expired {expired} stale model predictions", flush=True)
            maintain_deal_intelligence(conn)
            sticker_targets = discover_sticker_targets(
                conn,
                config,
                target_control,
            )
            liquid_discovery_error = None
            try:
                liquid_skin_targets = discover_liquid_skin_targets(
                    conn,
                    config,
                    target_control,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                # A broken optional catalog must not disable the established
                # sticker profile.  Surface a stable status error and retry on
                # service restart after the catalog/config is repaired.
                liquid_skin_targets = []
                liquid_discovery_error = (
                    f"liquid_skin_discovery_failed:{type(exc).__name__}"
                )
                print(f"Liquid <=$10 profile disabled: {exc}", flush=True)
            knife_discovery_error = None
            knife_rotation_state = load_knife_rotation_state(conn)
            knife_catalog_targets = []
            try:
                knife_catalog_targets = discover_knife_targets(
                    conn,
                    config,
                    target_control,
                )
                knife_targets = (
                    refresh_knife_rotation(
                        conn,
                        config,
                        knife_catalog_targets,
                        knife_rotation_state,
                        rerank=True,
                    )
                    if profile_enabled[scanner_target_profiles.PROFILE_KNIVES]
                    else []
                )
            except (RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
                knife_targets = []
                knife_discovery_error = (
                    f"knife_discovery_failed:{type(exc).__name__}"
                )
                print(f"Knife profile disabled: {exc}", flush=True)
            glove_discovery_error = None
            glove_rotation_state = load_glove_rotation_state(conn)
            glove_catalog_targets = []
            try:
                glove_catalog_targets = discover_glove_targets(
                    conn,
                    config,
                    target_control,
                )
                glove_targets = (
                    refresh_glove_rotation(
                        conn,
                        config,
                        glove_catalog_targets,
                        glove_rotation_state,
                        rerank=True,
                    )
                    if profile_enabled[scanner_target_profiles.PROFILE_GLOVES]
                    else []
                )
            except (RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
                glove_targets = []
                glove_discovery_error = (
                    f"glove_discovery_failed:{type(exc).__name__}"
                )
                print(f"Glove profile disabled: {exc}", flush=True)
            if (
                not sticker_targets
                and not liquid_skin_targets
                and not knife_targets
                and not glove_targets
            ):
                print(
                    "No deal scanner targets are selected; waiting for target-control changes",
                    flush=True,
                )

            print(
                f"Loaded {len(sticker_targets)} sticker targets and "
                f"{len(liquid_skin_targets)} liquid <=$10 skin targets and "
                f"{len(knife_catalog_targets)} dedicated knife catalog targets "
                f"({len(knife_targets)} in the durable window) and "
                f"{len(glove_catalog_targets)} dedicated glove catalog targets "
                f"({len(glove_targets)} in the durable window)",
                flush=True,
            )
            sticker_index = 0
            liquid_skin_index = 0
            target_control_identity = (
                target_control.get("revision"),
                target_control.get("error"),
            )
            cycle_started_monotonic = None
            cycle_started_epoch = None
            cycles_completed = int(reporter.state.get("cycles_completed") or 0)

            while not event.is_set():
                control = wait_for_scan_permission(
                    cycle_started_monotonic,
                    cycle_started_epoch,
                    default_interval_seconds=default_interval,
                    stop_event=event,
                    reporter=reporter,
                )
                if control is None:
                    break

                latest_target_control = load_scanner_target_control(
                    config,
                    create_if_missing=False,
                )
                latest_target_identity = (
                    latest_target_control.get("revision"),
                    latest_target_control.get("error"),
                )
                if latest_target_identity != target_control_identity:
                    # Apply dashboard target changes at a cycle boundary.  All
                    # rediscovery is local; an invalid control disables every
                    # profile and cannot spend quota on the previous list.
                    target_control = latest_target_control
                    target_control_identity = latest_target_identity
                    profile_enabled = {
                        name: scanner_target_profiles.profile_settings(
                            target_control,
                            name,
                        )["enabled"]
                        for name in scanner_target_profiles.PROFILE_ORDER
                    }
                    stickers_per_cycle = (
                        int(config.get("stickers_per_cycle") or 5)
                        if profile_enabled[
                            scanner_target_profiles.PROFILE_STICKER_GUNS
                        ]
                        else 0
                    )
                    liquid_skins_per_cycle = (
                        int(config.get("liquid_skins_per_cycle") or 1)
                        if profile_enabled[
                            scanner_target_profiles.PROFILE_LIQUID_UNDER_10
                        ]
                        else 0
                    )
                    knives_per_cycle = (
                        int(config.get("knives_per_cycle") or 1)
                        if profile_enabled[scanner_target_profiles.PROFILE_KNIVES]
                        else 0
                    )
                    gloves_per_cycle = (
                        int(config.get("gloves_per_cycle") or 1)
                        if profile_enabled[scanner_target_profiles.PROFILE_GLOVES]
                        else 0
                    )
                    sticker_targets = []
                    liquid_skin_targets = []
                    knife_catalog_targets = []
                    knife_targets = []
                    glove_catalog_targets = []
                    glove_targets = []
                    liquid_discovery_error = None
                    knife_discovery_error = None
                    glove_discovery_error = None
                    try:
                        sticker_targets = discover_sticker_targets(
                            conn,
                            config,
                            target_control,
                        )
                    except (RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
                        print(f"Sticker target update deferred: {exc}", flush=True)
                    try:
                        liquid_skin_targets = discover_liquid_skin_targets(
                            conn,
                            config,
                            target_control,
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        liquid_discovery_error = (
                            f"liquid_skin_discovery_failed:{type(exc).__name__}"
                        )
                    try:
                        knife_catalog_targets = discover_knife_targets(
                            conn,
                            config,
                            target_control,
                        )
                        if profile_enabled[scanner_target_profiles.PROFILE_KNIVES]:
                            knife_targets = refresh_knife_rotation(
                                conn,
                                config,
                                knife_catalog_targets,
                                knife_rotation_state,
                                rerank=True,
                            )
                    except (RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
                        knife_discovery_error = (
                            f"knife_discovery_failed:{type(exc).__name__}"
                        )
                    try:
                        glove_catalog_targets = discover_glove_targets(
                            conn,
                            config,
                            target_control,
                        )
                        if profile_enabled[scanner_target_profiles.PROFILE_GLOVES]:
                            glove_targets = refresh_glove_rotation(
                                conn,
                                config,
                                glove_catalog_targets,
                                glove_rotation_state,
                                rerank=True,
                            )
                    except (RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
                        glove_discovery_error = (
                            f"glove_discovery_failed:{type(exc).__name__}"
                        )
                    sticker_index = 0
                    liquid_skin_index = 0
                    print(
                        "Applied target-control revision "
                        f"{target_control.get('revision')}: "
                        f"{len(sticker_targets)} stickers, "
                        f"{len(liquid_skin_targets)} liquid skins, "
                        f"{len(knife_catalog_targets)} knives, "
                        f"{len(glove_catalog_targets)} gloves",
                        flush=True,
                    )

                cycle_started_monotonic = time.monotonic()
                cycle_started_epoch = time.time()
                cycle_started = utc_now()
                cycle_number = cycles_completed + 1
                cycle_targets_scanned = 0
                cycle_listings_returned = 0
                cycle_candidates_saved = 0
                cycle_error = (
                    liquid_discovery_error
                    or knife_discovery_error
                    or glove_discovery_error
                )
                cycle_seen_listing_ids = set()
                cycle_jobs = []
                if profile_enabled[scanner_target_profiles.PROFILE_KNIVES]:
                    rediscovery_due = bool(
                        not knife_catalog_targets
                        or (
                            cycles_completed
                            and cycles_completed % knife_rediscovery_cycles == 0
                        )
                    )
                    try:
                        if rediscovery_due:
                            knife_catalog_targets = discover_knife_targets(
                                conn,
                                config,
                                target_control,
                            )
                        if rediscovery_due or not knife_targets:
                            knife_targets = refresh_knife_rotation(
                                conn,
                                config,
                                knife_catalog_targets,
                                knife_rotation_state,
                                rerank=rediscovery_due,
                            )
                            knife_discovery_error = None
                    except (RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
                        knife_discovery_error = (
                            f"knife_discovery_failed:{type(exc).__name__}"
                        )
                        print(f"Knife rediscovery deferred: {exc}", flush=True)
                    cycle_error = (
                        liquid_discovery_error
                        or knife_discovery_error
                        or glove_discovery_error
                    )
                if profile_enabled[scanner_target_profiles.PROFILE_GLOVES]:
                    glove_rediscovery_due = bool(
                        not glove_catalog_targets
                        or (
                            cycles_completed
                            and cycles_completed % glove_rediscovery_cycles == 0
                        )
                    )
                    try:
                        if glove_rediscovery_due:
                            glove_catalog_targets = discover_glove_targets(
                                conn,
                                config,
                                target_control,
                            )
                        if glove_rediscovery_due or not glove_targets:
                            glove_targets = refresh_glove_rotation(
                                conn,
                                config,
                                glove_catalog_targets,
                                glove_rotation_state,
                                rerank=glove_rediscovery_due,
                            )
                            glove_discovery_error = None
                    except (RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
                        glove_discovery_error = (
                            f"glove_discovery_failed:{type(exc).__name__}"
                        )
                        print(f"Glove rediscovery deferred: {exc}", flush=True)
                    cycle_error = (
                        liquid_discovery_error
                        or knife_discovery_error
                        or glove_discovery_error
                    )
                if sticker_targets:
                    for offset in range(min(stickers_per_cycle, len(sticker_targets))):
                        cycle_jobs.append(
                            (
                                SOURCE_STICKER,
                                sticker_targets[
                                    (sticker_index + offset) % len(sticker_targets)
                                ],
                            )
                        )
                if liquid_skin_targets:
                    for offset in range(
                        min(liquid_skins_per_cycle, len(liquid_skin_targets))
                    ):
                        cycle_jobs.append(
                            (
                                SOURCE_LIQUID_SKIN,
                                liquid_skin_targets[
                                    (liquid_skin_index + offset)
                                    % len(liquid_skin_targets)
                                ],
                            )
                        )
                if knife_targets:
                    for offset in range(min(knives_per_cycle, len(knife_targets))):
                        cycle_jobs.append(
                            (
                                SOURCE_KNIFE,
                                knife_targets[offset],
                            )
                        )
                if glove_targets:
                    for offset in range(min(gloves_per_cycle, len(glove_targets))):
                        cycle_jobs.append(
                            (
                                SOURCE_GLOVE,
                                glove_targets[offset],
                            )
                        )
                print(f"Starting deal scan cycle at {cycle_started}", flush=True)
                reporter.report(
                    "scanning",
                    force=True,
                    cycle_started_at=cycle_started,
                    next_scan_at=next_scan_timestamp(
                        cycle_started_epoch,
                        int(control["interval_seconds"]),
                    ),
                    last_error=None,
                    cycle_number=cycle_number,
                    cycle_targets_total=len(cycle_jobs),
                    cycle_targets_scanned=0,
                    cycle_listings_returned=0,
                    cycle_candidates_saved=0,
                )

                live_control_holder = {"value": control}

                def scan_allowed(
                    control_holder=live_control_holder,
                    scanner_event=event,
                    legacy_interval=default_interval,
                ):
                    control_holder["value"] = read_live_control(legacy_interval)
                    return (
                        not scanner_event.is_set()
                        and control_holder["value"].get("enabled") is True
                    )

                def scan_heartbeat(
                    control_holder=live_control_holder,
                    started_epoch=cycle_started_epoch,
                    status_reporter=reporter,
                ):
                    status_reporter.report(
                        "scanning",
                        next_scan_at=next_scan_timestamp(
                            started_epoch,
                            int(control_holder["value"]["interval_seconds"]),
                        ),
                    )

                interrupted = False
                rate_exhausted = False
                for target_index, (profile, target) in enumerate(cycle_jobs):
                    if not scan_allowed():
                        interrupted = True
                        break
                    try:
                        if profile == SOURCE_LIQUID_SKIN:
                            scan_function = scan_liquid_skin
                        elif profile == SOURCE_KNIFE:
                            scan_function = scan_knife
                        elif profile == SOURCE_GLOVE:
                            scan_function = scan_glove
                        else:
                            scan_function = scan_sticker
                        returned, saved, meta, model_saved = scan_function(
                            conn,
                            target,
                            config,
                            should_continue=scan_allowed,
                            heartbeat=scan_heartbeat,
                            cycle_seen_listing_ids=cycle_seen_listing_ids,
                        )
                        cycle_listings_returned += returned
                        cycle_candidates_saved += saved
                        source = target_source_fields(target)
                        print(
                            f"{source['source_type']} | {source['source_name']} -> "
                            f"returned {returned}, saved {saved}, models {model_saved}, "
                            f"key {meta.get('key_file')} "
                            f"remaining {meta.get('rate_remaining')}",
                            flush=True,
                        )
                        if _rate_remaining_exhausted(meta):
                            cycle_error = "rate_limit_exhausted"
                            rate_exhausted = True
                        elif meta.get("model_status") == "unavailable":
                            cycle_error = (
                                "glove_model_unavailable"
                                if profile == SOURCE_GLOVE
                                else "knife_model_unavailable"
                            )
                    except ScanControlInterrupted:
                        interrupted = True
                        break
                    except csfloat_listing_fetcher.CsfloatRateLimitError as exc:
                        cycle_error = "rate_limit_exhausted"
                        rate_exhausted = True
                        source = target_source_fields(target)
                        print(
                            f"{source['source_type']} | {source['source_name']} "
                            "-> rate limit exhausted",
                            flush=True,
                        )
                        save_scan_log(conn, target, "", 0, 0, error=str(exc))
                    except Exception as exc:  # noqa: BLE001 - long-running scanner boundary
                        cycle_error = f"scan_failed:{type(exc).__name__}"
                        source = target_source_fields(target)
                        print(
                            f"{source['source_type']} | {source['source_name']} "
                            f"-> error: {exc}",
                            flush=True,
                        )
                        save_scan_log(conn, target, "", 0, 0, error=str(exc))

                    if profile == SOURCE_LIQUID_SKIN:
                        liquid_skin_index += 1
                    elif profile == SOURCE_KNIFE:
                        source = target_source_fields(target)
                        advance_knife_rotation(
                            conn,
                            knife_rotation_state,
                            source["target_key"],
                        )
                        knife_targets = [
                            item
                            for item in knife_targets
                            if item.get("target_key") != source["target_key"]
                        ]
                    elif profile == SOURCE_GLOVE:
                        source = target_source_fields(target)
                        advance_glove_rotation(
                            conn,
                            glove_rotation_state,
                            source["target_key"],
                        )
                        glove_targets = [
                            item
                            for item in glove_targets
                            if item.get("target_key") != source["target_key"]
                        ]
                    else:
                        sticker_index += 1
                    cycle_targets_scanned = target_index + 1
                    latest_control = read_live_control(default_interval)
                    reporter.report(
                        "scanning",
                        next_scan_at=next_scan_timestamp(
                            cycle_started_epoch,
                            int(latest_control["interval_seconds"]),
                        ),
                        last_error=cycle_error,
                        cycle_targets_scanned=cycle_targets_scanned,
                        cycle_listings_returned=cycle_listings_returned,
                        cycle_candidates_saved=cycle_candidates_saved,
                    )
                    if event.wait(CONTROL_POLL_SECONDS):
                        interrupted = True
                        break
                    if rate_exhausted:
                        break

                if interrupted:
                    if event.is_set():
                        break
                    paused_control = read_live_control(default_interval)
                    reporter.report(
                        "paused",
                        force=True,
                        next_scan_at=None,
                        last_error=paused_control.get("error"),
                    )
                    # An interrupted cycle is incomplete; resume it immediately
                    # instead of waiting from an aborted start timestamp.
                    cycle_started_monotonic = None
                    cycle_started_epoch = None
                    continue

                expired = expire_stale_predictions(
                    conn,
                    config.get("stale_prediction_hours", 24),
                )
                if expired:
                    print(f"Expired {expired} stale model predictions", flush=True)
                maintain_deal_intelligence(conn)

                cycles_completed += 1
                completed_at = utc_now()
                latest_control = read_live_control(default_interval)
                next_scan_at = None
                process_state = "paused"
                if latest_control.get("enabled"):
                    process_state = "sleeping"
                    next_scan_at = next_scan_timestamp(
                        cycle_started_epoch,
                        int(latest_control["interval_seconds"]),
                    )
                reporter.report(
                    process_state,
                    force=True,
                    cycle_completed_at=completed_at,
                    next_scan_at=next_scan_at,
                    last_error=cycle_error or latest_control.get("error"),
                    cycles_completed=cycles_completed,
                )
    except BaseException as exc:
        fatal_error = f"fatal:{type(exc).__name__}"
        raise
    finally:
        stopped_changes = {"next_scan_at": None}
        if fatal_error:
            stopped_changes["last_error"] = fatal_error
        reporter.report("stopped", force=True, **stopped_changes)


def main():
    install_signal_handlers()
    try:
        with (
            model_lifecycle.service_lifetime_lock("deal_scanner"),
            scanner_instance_lock(),
        ):
            run_forever()
    except model_lifecycle.ModelLifecycleError as exc:
        raise SystemExit(f"Deal scanner startup blocked: {exc}") from exc


if __name__ == "__main__":
    main()
