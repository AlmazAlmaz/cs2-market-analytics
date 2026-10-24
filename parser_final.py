#!/usr/bin/env python3

import argparse

try:
    import fcntl
except ImportError:  # Windows
    import portable_fcntl as fcntl
import hashlib
import json
import math
import os
import signal
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app_config
import cs2_item_catalog
from csfloat_listing_fetcher import load_key_records

DB_PATH = str(app_config.path("CS2_DB_PATH", "csfloat_history.db"))
SKINS_PATH = str(app_config.path("CS2_SKINS_PATH", "ALL_SKINS.json"))
BASE_URL = "https://csfloat.com/api/v1/history"
STATUS_PATH = str(app_config.path("CS2_STATUS_PATH", "parser_status.json"))
LOCK_PATH = app_config.PROJECT_DIR / ".run" / "parser_final.lock"
CATALOG_CACHE_DIR = app_config.path(
    "CS2_CATALOG_CACHE_DIR",
    ".private/item_catalog_cache",
)
CATALOG_REFRESH_ENABLED = app_config.get_bool("CS2_CATALOG_REFRESH_ENABLED", True)
CATALOG_ENRICHMENT_ENABLED = app_config.get_bool(
    "CS2_CATALOG_ENRICHMENT_ENABLED", True
)
CATALOG_FALLBACK_ENABLED = app_config.get_bool("CS2_CATALOG_FALLBACK_ENABLED", False)
CATALOG_REFRESH_INTERVAL_SECONDS = app_config.get_int(
    "CS2_CATALOG_REFRESH_INTERVAL_SECONDS",
    cs2_item_catalog.DEFAULT_REFRESH_INTERVAL_SECONDS,
    minimum=60 * 60,
    maximum=30 * 24 * 60 * 60,
)
CATALOG_TIMEOUT_SECONDS = app_config.get_int(
    "CS2_CATALOG_TIMEOUT_SECONDS",
    cs2_item_catalog.DEFAULT_TIMEOUT_SECONDS,
    minimum=5,
    maximum=60,
)
CATALOG_TOTAL_TIMEOUT_SECONDS = app_config.get_int(
    "CS2_CATALOG_TOTAL_TIMEOUT_SECONDS",
    cs2_item_catalog.DEFAULT_TOTAL_TIMEOUT_SECONDS,
    minimum=5,
    maximum=180,
)
CATALOG_BACKGROUND_CHECK_SECONDS = 15 * 60
CATALOG_PRIMARY_URL = app_config.get(
    "CS2_CATALOG_PRIMARY_URL",
    cs2_item_catalog.PRIMARY_SOURCE_URL,
)
CATALOG_ENRICHMENT_URL = app_config.get(
    "CS2_CATALOG_ENRICHMENT_URL",
    cs2_item_catalog.ENRICHMENT_SOURCE_URL,
)
STEAM_IMAGE_BASE_URL = "https://community.akamai.steamstatic.com/economy/image/"
SAVE_LAST_RESPONSE_JSON = False

PROGRESS_ID = 2
REQUEST_DELAY_SECONDS = 1
NETWORK_ERROR_WAIT_SECONDS = 5
PROXY_ERROR_WAIT_SECONDS = 5 * 60
TRANSPORT_ERROR_BACKOFF_BASE_SECONDS = 30
TRANSPORT_ERROR_BACKOFF_MAX_SECONDS = PROXY_ERROR_WAIT_SECONDS
CONNECT_TIMEOUT_SECONDS = app_config.get_int(
    "PARSER_CONNECT_TIMEOUT_SECONDS",
    15,
    minimum=3,
    maximum=60,
)
READ_TIMEOUT_SECONDS = app_config.get_int(
    "PARSER_READ_TIMEOUT_SECONDS",
    60,
    minimum=10,
    maximum=180,
)
RATE_LIMIT_WAIT_SECONDS = 60 * 60
MIN_RATE_LIMIT_BACKOFF_SECONDS = 60
TOO_MANY_IPS_WAIT_SECONDS = 25 * 60 * 60
MOSCOW_TZ = timezone(timedelta(hours=3))

SCHEDULER_PROGRESS_ID = 3
SCHEDULER_ALPHA = 0.35
SCHEDULER_PRIORITY_SALES_WEIGHT_SECONDS = 5 * 60
SCHEDULER_PRIORITY_NEW_SALES_WEIGHT_SECONDS = 15 * 60
SCHEDULER_MAX_OVERDUE_PRIORITY_SECONDS = 24 * 60 * 60
SALES_PAGE_SIZE = 40
TARGET_NEW_SALES_PER_REQUEST = app_config.get_int(
    "PARSER_TARGET_NEW_SALES",
    20,
    minimum=5,
    maximum=30,
)
SATURATED_TARGET_NEW_SALES = app_config.get_int(
    "PARSER_SATURATED_TARGET_NEW_SALES",
    10,
    minimum=3,
    maximum=20,
)
MAX_CURSOR_PAGES = app_config.get_int("PARSER_MAX_CURSOR_PAGES", 5, minimum=1, maximum=20)
USE_PAINT_INDEX_FILTER = app_config.get_bool("PARSER_USE_PAINT_INDEX", False)
INITIAL_BOOTSTRAP_DUE_COUNT = 1000
INITIAL_MIN_FETCH_DELAY_SECONDS = 0
INITIAL_EXPLORATION_WINDOW_SECONDS = app_config.get_int(
    "PARSER_INITIAL_EXPLORATION_DAYS",
    7,
    minimum=1,
    maximum=60,
) * 24 * 60 * 60
HISTORY_STATS_RECENT_SALES_LIMIT = 160
MIN_SKIN_INTERVAL_SECONDS = 30 * 60
MAX_SKIN_INTERVAL_SECONDS = 60 * 24 * 60 * 60
ERROR_RETRY_BASE_SECONDS = 5 * 60
ERROR_RETRY_MAX_SECONDS = 6 * 60 * 60
SCHEDULE_JITTER_FRACTION = 0.05
PARSER_STATUS_VERSION = 3

SALES_EXTRA_COLUMNS = {
    "sale_type": "TEXT",
    "is_seller": "INTEGER",
    "is_watchlisted": "INTEGER",
    "watchers": "INTEGER",
    "item_asset_id": "TEXT",
    "item_icon_url": "TEXT",
    "item_name": "TEXT",
    "item_description": "TEXT",
    "item_collection": "TEXT",
    "item_type": "TEXT",
    "item_type_name": "TEXT",
    "item_quality": "TEXT",
    "item_rarity": "INTEGER",
    "item_rarity_name": "TEXT",
    "item_def_index": "INTEGER",
    "item_wear_name": "TEXT",
    "item_tradable": "INTEGER",
    "item_is_commodity": "INTEGER",
    "item_is_souvenir": "INTEGER",
    "item_is_stattrak": "INTEGER",
    "item_cs2_screenshot_id": "TEXT",
    "item_cs2_screenshot_at": "TEXT",
    "item_gs_sig": "TEXT",
    "raw_json": "TEXT",
    "item_raw_json": "TEXT",
    "reference_raw_json": "TEXT",
    "first_seen_at": "TEXT",
    "last_seen_at": "TEXT",
    "seen_count": "INTEGER NOT NULL DEFAULT 1",
    "payload_sha256": "TEXT",
    "last_payload_sha256": "TEXT",
    "reference_keychain_price": "INTEGER",
    "reference_last_updated": "TEXT",
}

STICKER_EXTRA_COLUMNS = {
    "icon_url": "TEXT",
    "offset_x": "REAL",
    "offset_y": "REAL",
    "reference_price_cents": "INTEGER",
    "reference_quantity": "INTEGER",
    "raw_json": "TEXT",
    "reference_raw_json": "TEXT",
    "rotation": "REAL",
    "reference_updated_at": "TEXT",
    "steam_market_url": "TEXT",
}

SCHEDULE_EXTRA_COLUMNS = {
    "paint_index": "INTEGER",
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "estimated_sales_per_day": "REAL NOT NULL DEFAULT 0",
    "saturation_streak": "INTEGER NOT NULL DEFAULT 0",
    "last_page_saturated": "INTEGER NOT NULL DEFAULT 0",
    "coverage_status": "TEXT",
    "last_attempt_at": "INTEGER",
    "last_success_at": "INTEGER",
    "consecutive_errors": "INTEGER NOT NULL DEFAULT 0",
    "last_outcome": "TEXT",
    "last_http_status": "INTEGER",
    "last_error": "TEXT",
    "last_error_at": "INTEGER",
    "last_duration_ms": "INTEGER",
}

REQUEST_LOG_EXTRA_COLUMNS = {
    "outcome": "TEXT",
    "http_status": "INTEGER",
    "duration_ms": "INTEGER",
    "updated_sales_count": "INTEGER NOT NULL DEFAULT 0",
    "overlap_count": "INTEGER NOT NULL DEFAULT 0",
    "rejected_sales_count": "INTEGER NOT NULL DEFAULT 0",
    "page_count": "INTEGER NOT NULL DEFAULT 1",
    "page_saturated": "INTEGER NOT NULL DEFAULT 0",
    "coverage_status": "TEXT",
    "error": "TEXT",
}


class ParserStopRequested(Exception):
    """Cooperative shutdown requested by SIGINT/SIGTERM."""


class SalesFetchError(RuntimeError):
    def __init__(self, message, *, outcome="transient_error", http_status=None, duration_ms=None):
        super().__init__(message)
        self.outcome = outcome
        self.http_status = http_status
        self.duration_ms = duration_ms


@dataclass
class SalesPage:
    sales: list
    cursor: str | None = None
    http_status: int = 200
    duration_ms: int = 0


@dataclass
class SalesWindow:
    sales: list = field(default_factory=list)
    page_count: int = 0
    cursor_supported: bool = False
    cursor_exhausted: bool = True
    overlap_ids: set = field(default_factory=set)
    http_status: int = 200
    duration_ms: int = 0
    page_sizes: list = field(default_factory=list)


@dataclass
class IngestStats:
    returned_count: int = 0
    new_count: int = 0
    updated_count: int = 0
    overlap_count: int = 0
    rejected_count: int = 0
    duplicate_response_count: int = 0
    new_sales: list = field(default_factory=list)


def mask_proxy(proxy_url):
    if not proxy_url:
        return "direct"

    try:
        parsed = urllib.parse.urlsplit(proxy_url)
    except ValueError:
        return "invalid-proxy"
    if parsed.hostname:
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://***@{host}" if parsed.username else f"{parsed.scheme}://{host}"

    return "invalid-proxy"


def is_socks_proxy(proxy_url):
    if not proxy_url:
        return False

    lowered = proxy_url.lower()
    return lowered.startswith(("socks4://", "socks5://", "socks5h://"))


def load_requests_for_socks():
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError(
            "SOCKS5 proxy requires requests with PySocks. Install it:\n"
            "py -m pip install requests[socks]"
        ) from exc

    return requests


def build_http_client(proxy_url):
    if is_socks_proxy(proxy_url):
        requests = load_requests_for_socks()
        session = requests.Session()
        session.trust_env = False
        session.proxies = {
            "http": proxy_url,
            "https": proxy_url,
        }
        return {
            "type": "requests",
            "session": session,
            "requests": requests,
        }

    if not proxy_url:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url,
        }))

    return {
        "type": "urllib",
        "opener": opener,
    }


def make_key_record(path, value, proxy_url=None):
    client = build_http_client(proxy_url)

    return {
        "path": path,
        "value": value,
        "proxy": proxy_url,
        "proxy_label": mask_proxy(proxy_url),
        "client": client,
        # Key cooldown is exclusively for responses from CSFloat that identify
        # a key problem (429, 401, or an explicit invalid-token 403). Transport
        # failures have their own cooldown so a dead SOCKS route never poisons
        # a healthy API key.
        "blocked_until": 0,
        "blocked_reason": None,
        "transport_blocked_until": 0,
        "transport_failure_count": 0,
        "transport_last_error": None,
        "transport_last_error_at": None,
        "rate_limit": None,
        "rate_remaining": None,
        "rate_reset": None,
        "last_http_status": None,
        "last_seen_at": None,
    }


def load_api_keys():
    return [
        make_key_record(record["path"], record["value"], record.get("proxy"))
        for record in load_key_records(strict=True)
    ]


API_KEYS = []
CURRENT_KEY_INDEX = 0
STOP_REQUESTED = False
PARSER_STATE = {
    "status_version": PARSER_STATUS_VERSION,
    "running": False,
    "started_at": None,
    "stopped_at": None,
    "cycle_started_at": None,
    "scheduler_mode": "dynamic_popularity",
    "total_skins": None,
    "skin_index": 0,
    "current_skin": None,
    "last_saved_skin": None,
    "last_sales_count": None,
    "last_new_sales_count": None,
    "last_interval_seconds": None,
    "last_next_fetch_at": None,
    "last_outcome": None,
    "last_page_saturated": None,
    "last_coverage_status": None,
    "last_error": None,
    "request_count_cycle": 0,
    "sales_count_cycle": 0,
    "sleeping_until": None,
    "sleep_reason": None,
    "updated_at": None,
}


def restore_key_cooldowns(keys):
    status_path = Path(STATUS_PATH)
    if not status_path.exists():
        return keys
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return keys

    status_version = safe_int(payload.get("status_version")) or 0
    previous = {
        (str(row.get("path")), str(row.get("proxy"))): row
        for row in payload.get("keys", [])
        if isinstance(row, dict)
    }
    now = unix_now()
    for key in keys:
        row = previous.get((str(key.get("path")), str(key.get("proxy_label"))))
        if not row:
            continue
        blocked_until = parse_csfloat_time(row.get("blocked_until"))
        transport_blocked_until = parse_csfloat_time(
            row.get("transport_blocked_until")
        )

        if status_version >= 3:
            if blocked_until and blocked_until > now:
                key["blocked_until"] = int(blocked_until)
                key["blocked_reason"] = row.get("blocked_reason")
            if transport_blocked_until and transport_blocked_until > now:
                key["transport_blocked_until"] = int(transport_blocked_until)
                key["transport_failure_count"] = max(
                    1,
                    safe_int(row.get("transport_failure_count")) or 1,
                )
                key["transport_last_error"] = row.get("transport_last_error")
                key["transport_last_error_at"] = row.get(
                    "transport_last_error_at"
                )
            continue

        # Version 2 stored both key limits and proxy failures in blocked_until.
        # Recover an old cooldown only when its persisted evidence identifies
        # either a key failure or a transport failure. Ambiguous states are
        # allowed to revalidate instead of poisoning a healthy route.
        if not blocked_until or blocked_until <= now:
            continue
        raw_last_status = str(row.get("last_http_status") or "").casefold()
        last_status = safe_int(row.get("last_http_status"))
        if last_status == 401:
            key["blocked_until"] = int(blocked_until)
            key["blocked_reason"] = "authentication"
        elif last_status == 429:
            key["blocked_until"] = int(blocked_until)
            key["blocked_reason"] = "rate_limit"
        elif raw_last_status in {"proxy_error", "network_error"}:
            key["transport_blocked_until"] = int(blocked_until)
            key["transport_failure_count"] = 1
            key["transport_last_error"] = "legacy_network_error"
        elif last_status is None and row.get("rate_remaining") == 0:
            # Some old rate-limit snapshots were written before the HTTP
            # status was persisted.  Only that status-less shape is safe to
            # recover from the remaining counter.  In particular, never let
            # an ambiguous legacy 403 poison a key even when its response
            # happened to report zero remaining requests.
            key["blocked_until"] = int(blocked_until)
            key["blocked_reason"] = "rate_limit"
        # A legacy 403 is deliberately not restored.  Version 2 could not tell
        # item-level/WAF forbiddance from an invalid credential, and replaying
        # that ambiguous 24-hour block would recreate the outage on restart.
    return keys


def initialize_api_keys(force=False):
    global API_KEYS, CURRENT_KEY_INDEX
    if API_KEYS and not force:
        return API_KEYS
    API_KEYS = restore_key_cooldowns(load_api_keys())
    CURRENT_KEY_INDEX = 0
    switch_to_available_key(include_current=True)
    return API_KEYS


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def unix_to_iso(timestamp):
    if not timestamp:
        return None

    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()


def safe_int(value):
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def bool_to_int(value):
    if value is None:
        return None

    return 1 if bool(value) else 0


def dumps_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_icon_url(icon_url):
    if not icon_url:
        return None

    if icon_url.startswith(("http://", "https://")):
        return icon_url

    return STEAM_IMAGE_BASE_URL + icon_url


def write_parser_status():
    PARSER_STATE["updated_at"] = iso_now()

    keys = []
    now = int(time.time())

    for index, key in enumerate(API_KEYS):
        blocked_until = key.get("blocked_until") or 0
        transport_blocked_until = key.get("transport_blocked_until") or 0
        rate_reset = key.get("rate_reset")
        keys.append({
            "index": index,
            "path": key["path"],
            "proxy": key["proxy_label"],
            "current": index == CURRENT_KEY_INDEX,
            "blocked_until": unix_to_iso(blocked_until),
            "blocked_seconds_left": max(0, int(blocked_until) - now) if blocked_until else 0,
            "blocked_reason": key.get("blocked_reason"),
            "transport_blocked_until": unix_to_iso(transport_blocked_until),
            "transport_blocked_seconds_left": (
                max(0, int(transport_blocked_until) - now)
                if transport_blocked_until
                else 0
            ),
            "transport_failure_count": key.get("transport_failure_count", 0),
            "transport_last_error": key.get("transport_last_error"),
            "transport_last_error_at": key.get("transport_last_error_at"),
            "rate_limit": key.get("rate_limit"),
            "rate_remaining": key.get("rate_remaining"),
            "rate_reset": unix_to_iso(rate_reset),
            "rate_reset_seconds_left": max(0, int(rate_reset) - now) if rate_reset else None,
            "last_http_status": key.get("last_http_status"),
            "last_seen_at": key.get("last_seen_at"),
        })

    status_path = Path(STATUS_PATH)
    if not API_KEYS and status_path.exists():
        try:
            previous_payload = json.loads(status_path.read_text(encoding="utf-8"))
            previous_keys = previous_payload.get("keys")
            if isinstance(previous_keys, list):
                keys = previous_keys
        except (OSError, json.JSONDecodeError):
            pass

    payload = dict(PARSER_STATE)
    payload["keys"] = keys

    tmp_path = status_path.with_name(f"{status_path.stem}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.chmod(0o600)
        tmp_path.replace(status_path)
    except OSError as exc:
        print(f"Status file write failed: {exc}", flush=True)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def update_parser_state(**kwargs):
    PARSER_STATE.update(kwargs)
    write_parser_status()


def update_current_key_rate(headers, http_status):
    current_key = get_current_key()
    current_key["rate_limit"] = safe_int(headers.get("X-Ratelimit-Limit"))
    current_key["rate_remaining"] = safe_int(headers.get("X-Ratelimit-Remaining"))
    current_key["rate_reset"] = safe_int(headers.get("X-Ratelimit-Reset"))
    current_key["last_http_status"] = http_status
    current_key["last_seen_at"] = iso_now()
    if (safe_int(current_key.get("blocked_until")) or 0) <= unix_now():
        current_key["blocked_until"] = 0
        current_key["blocked_reason"] = None
    # Receiving any HTTP response proves that DNS/TCP/TLS/SOCKS transport is
    # healthy.  It says nothing about key quota, which is updated above.
    current_key["transport_blocked_until"] = 0
    current_key["transport_failure_count"] = 0
    current_key["transport_last_error"] = None
    current_key["transport_last_error_at"] = None
    write_parser_status()


def route_blocked_until(key):
    return max(
        safe_int(key.get("blocked_until")) or 0,
        safe_int(key.get("transport_blocked_until")) or 0,
    )


def get_current_key():
    if not API_KEYS:
        initialize_api_keys()
    now = unix_now()
    if (
        route_blocked_until(API_KEYS[CURRENT_KEY_INDEX]) > now
        and not switch_to_available_key(include_current=True)
    ):
        sleep_with_message(
            seconds_until_any_key_available(),
            "All CSFloat routes are cooling down",
        )
        if not switch_to_available_key(include_current=True):
            raise SalesFetchError(
                "No CSFloat route became available after cooldown",
                outcome="route_unavailable",
            )
    return API_KEYS[CURRENT_KEY_INDEX]


def print_loaded_key_routes():
    print("Loaded API key routes:", flush=True)
    for index, key in enumerate(API_KEYS, start=1):
        print(
            f"  {index}. {key['path']} -> {key['proxy_label']}",
            flush=True,
        )


def switch_to_available_key(include_current=False, *, excluded_indices=None):
    global CURRENT_KEY_INDEX

    now = int(time.time())
    key_count = len(API_KEYS)
    excluded_indices = set(excluded_indices or ())

    start_offset = 0 if include_current else 1
    for offset in range(start_offset, key_count + start_offset):
        next_index = (CURRENT_KEY_INDEX + offset) % key_count
        if next_index in excluded_indices:
            continue
        if route_blocked_until(API_KEYS[next_index]) <= now:
            CURRENT_KEY_INDEX = next_index
            print(
                f"Switched to API key: {API_KEYS[next_index]['path']} "
                f"via {API_KEYS[next_index]['proxy_label']}",
                flush=True,
            )
            write_parser_status()
            return True

    return False


def is_too_many_ips_error(body):
    return "too many requests from too many ips" in (body or "").lower()


def is_explicit_auth_error(http_status, body):
    """Return true only when CSFloat identifies the credential as invalid.

    A bare HTTP 403 can be tied to one market_hash_name (or an upstream WAF
    rule), so treating every 403 as a bad key disables every route for 24 hours.
    HTTP 401 is unambiguously authentication-related; for 403 we require an
    explicit credential marker from the response body.
    """

    if http_status == 401:
        return True
    if http_status != 403:
        return False

    normalized = " ".join(str(body or "").casefold().replace("_", " ").split())
    return any(
        marker in normalized
        for marker in (
            "invalid api key",
            "api key is invalid",
            "invalid or expired api key",
            "invalid token",
            "token is invalid",
            "token expired",
            "expired token",
            "jwt expired",
            "missing authorization",
            "invalid authorization",
            "authentication required",
            "unauthorized api key",
        )
    )


def mark_current_key_auth_failed(http_status):
    current_key = get_current_key()
    blocked_until = unix_now() + 24 * 60 * 60
    current_key["blocked_until"] = blocked_until
    current_key["blocked_reason"] = "authentication"
    current_key["last_http_status"] = http_status
    current_key["last_seen_at"] = iso_now()
    wake_at = datetime.fromtimestamp(blocked_until, tz=MOSCOW_TZ)
    print(
        f"API key {current_key['path']} rejected authentication (HTTP {http_status}); "
        f"key paused until {wake_at:%Y-%m-%d %H:%M:%S} MSK",
        flush=True,
    )
    write_parser_status()


def mark_current_key_limited(reset_header, body=None):
    current_key = get_current_key()
    now = int(time.time())
    parsed_reset = safe_int(reset_header)

    if parsed_reset is not None and parsed_reset > now:
        blocked_until = parsed_reset + 10
    elif parsed_reset is not None and 0 < parsed_reset <= 24 * 60 * 60:
        blocked_until = now + parsed_reset + 10
    elif is_too_many_ips_error(body):
        blocked_until = now + TOO_MANY_IPS_WAIT_SECONDS
    else:
        blocked_until = now + RATE_LIMIT_WAIT_SECONDS

    blocked_until = max(blocked_until, now + MIN_RATE_LIMIT_BACKOFF_SECONDS)

    current_key["blocked_until"] = blocked_until
    current_key["blocked_reason"] = (
        "too_many_ips" if is_too_many_ips_error(body) else "rate_limit"
    )

    wake_at = datetime.fromtimestamp(blocked_until, tz=MOSCOW_TZ)
    if is_too_many_ips_error(body):
        print(
            f"API key {current_key['path']} got too-many-IPs cooldown until "
            f"{wake_at:%Y-%m-%d %H:%M:%S} MSK",
            flush=True,
        )
    else:
        print(
            f"API key {current_key['path']} limited until {wake_at:%Y-%m-%d %H:%M:%S} MSK",
            flush=True,
        )
    write_parser_status()


def mark_current_key_network_failed(exc):
    """Pause a failed transport route without changing API-key cooldown."""

    current_key = get_current_key()
    failure_count = min(
        16,
        (safe_int(current_key.get("transport_failure_count")) or 0) + 1,
    )
    backoff_seconds = min(
        TRANSPORT_ERROR_BACKOFF_MAX_SECONDS,
        TRANSPORT_ERROR_BACKOFF_BASE_SECONDS * (2 ** (failure_count - 1)),
    )
    blocked_until = int(time.time()) + backoff_seconds
    current_key["transport_blocked_until"] = blocked_until
    current_key["transport_failure_count"] = failure_count
    current_key["transport_last_error"] = type(exc).__name__
    current_key["transport_last_error_at"] = iso_now()
    current_key["last_http_status"] = "proxy_error" if current_key.get("proxy") else "network_error"
    current_key["last_seen_at"] = iso_now()

    wake_at = datetime.fromtimestamp(blocked_until, tz=MOSCOW_TZ)
    route_kind = "Proxy route" if current_key.get("proxy") else "Direct network route"
    print(
        f"{route_kind} {current_key['proxy_label']} failed for API key "
        f"{current_key['path']}: {exc}. Transport paused until "
        f"{wake_at:%Y-%m-%d %H:%M:%S} MSK; API key quota was not changed",
        flush=True,
    )
    write_parser_status()


def fail_over_after_transport_error(exc, attempted_indices):
    """Try each healthy route at most once for one logical sales request."""

    failed_index = CURRENT_KEY_INDEX
    mark_current_key_network_failed(exc)
    attempted_indices.add(failed_index)
    return switch_to_available_key(excluded_indices=attempted_indices)


def seconds_until_any_key_available():
    now = int(time.time())
    earliest = min(route_blocked_until(key) for key in API_KEYS)
    return max(1, earliest - now)


def sleep_with_message(seconds, reason):
    seconds = max(0.0, float(seconds))
    if seconds <= 0:
        return
    wake_at = datetime.now(MOSCOW_TZ) + timedelta(seconds=seconds)
    wake_at_utc = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    print(
        f"{reason}. Sleeping {math.ceil(seconds)} seconds. Wake up at {wake_at:%Y-%m-%d %H:%M:%S} MSK",
        flush=True,
    )

    update_parser_state(
        sleeping_until=wake_at_utc.isoformat(),
        sleep_reason=reason,
    )

    deadline = time.monotonic() + seconds
    while True:
        if STOP_REQUESTED:
            raise ParserStopRequested()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        step = min(60.0, remaining)
        # Hidden Windows services receive a cooperative request instead of a
        # console Ctrl+C, so their long quota sleeps must be wakeable as well.
        import windows_service_control
        windows_service_control.wait_for_stop(step)

        remaining_after_sleep = deadline - time.monotonic()
        if remaining_after_sleep > 0 and seconds >= 60:
            update_parser_state(
                sleeping_until=wake_at_utc.isoformat(),
                sleep_reason=reason,
            )
            print(
                f"Still sleeping: {math.ceil(remaining_after_sleep)} seconds left. "
                f"Wake up at {wake_at:%Y-%m-%d %H:%M:%S} MSK",
                flush=True,
            )

    update_parser_state(
        sleeping_until=None,
        sleep_reason=None,
    )


def load_skins():
    skins = json.loads(Path(SKINS_PATH).read_text(encoding="utf-8"))

    if not isinstance(skins, list):
        raise TypeError(f"{SKINS_PATH} must contain a JSON array")

    result = []
    seen = set()

    for skin in skins:
        if isinstance(skin, str):
            skin = {"market_hash_name": skin}

        market_hash_name = skin.get("market_hash_name")
        if not market_hash_name or market_hash_name in seen:
            continue

        seen.add(market_hash_name)
        result.append(skin)

    return result


def extract_wear_name(market_hash_name):
    if isinstance(market_hash_name, str) and market_hash_name.endswith(")") and "(" in market_hash_name:
        return market_hash_name.rsplit("(", 1)[1][:-1]

    return None


def extract_base_skin_name(market_hash_name):
    if isinstance(market_hash_name, str) and market_hash_name.endswith(")") and "(" in market_hash_name:
        return market_hash_name.rsplit("(", 1)[0].strip()

    return market_hash_name


def unix_now():
    return int(time.time())


def unix_to_msk_text(timestamp):
    if not timestamp:
        return "unknown"

    return datetime.fromtimestamp(int(timestamp), tz=MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S")


def historical_count_to_seed_score(count):
    count = int(count or 0)
    if count >= 1000:
        return 40.0
    if count >= 500:
        return 34.0
    if count >= 250:
        return 26.0
    if count >= 100:
        return 18.0
    if count >= 50:
        return 11.0
    if count >= 20:
        return 6.0
    if count >= 5:
        return 3.0
    if count > 0:
        return 1.0
    return 0.0


def stable_schedule_fraction(market_hash_name, skin_order=0):
    text = f"{market_hash_name or ''}\0{int(skin_order or 0)}".encode()
    value = int.from_bytes(hashlib.blake2b(text, digest_size=8).digest(), "big")
    return value / float(2**64 - 1)


def initial_next_fetch_offset_seconds(seed_score, skin_order, market_hash_name=None):
    if seed_score >= 18:
        window_seconds = 3 * 24 * 60 * 60
    elif seed_score >= 11:
        window_seconds = 7 * 24 * 60 * 60
    elif seed_score >= 6:
        window_seconds = 14 * 24 * 60 * 60
    elif seed_score >= 3:
        window_seconds = 30 * 24 * 60 * 60
    elif seed_score >= 1:
        window_seconds = 45 * 24 * 60 * 60
    else:
        window_seconds = MAX_SKIN_INTERVAL_SECONDS

    spread_seconds = max(1, window_seconds - INITIAL_MIN_FETCH_DELAY_SECONDS)
    return INITIAL_MIN_FETCH_DELAY_SECONDS + int(
        stable_schedule_fraction(market_hash_name, skin_order) * spread_seconds
    )


def parse_csfloat_time(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def sale_sold_at_timestamp(sale):
    if not isinstance(sale, dict):
        return None

    return parse_csfloat_time(sale.get("sold_at"))


def sales_time_span_seconds(sales):
    timestamps = [
        timestamp
        for timestamp in (sale_sold_at_timestamp(sale) for sale in sales)
        if timestamp is not None
    ]

    if len(timestamps) < 2:
        return None

    return max(60, int(max(timestamps) - min(timestamps)))


def clamp_interval(interval):
    return max(MIN_SKIN_INTERVAL_SECONDS, min(MAX_SKIN_INTERVAL_SECONDS, int(interval)))


def compute_target_interval_seconds(sample_count, span_seconds):
    if not sample_count or sample_count <= 0 or not span_seconds:
        return None

    interval = TARGET_NEW_SALES_PER_REQUEST * float(span_seconds) / float(sample_count)
    return clamp_interval(interval)


def compute_skin_interval_seconds(
    sales_count,
    new_sales_count,
    avg_sales_count,
    avg_new_sales_count,
    sales_span_seconds=None,
    new_sales_span_seconds=None,
):
    interval = compute_target_interval_seconds(new_sales_count, new_sales_span_seconds)
    if interval is not None:
        return interval, "target_from_new_sales"

    interval = compute_target_interval_seconds(sales_count, sales_span_seconds)
    if interval is not None:
        if not new_sales_count:
            interval = max(12 * 60 * 60, interval * 8)
        return clamp_interval(interval), "target_from_all_sales_no_new" if not new_sales_count else "target_from_all_sales"

    new_demand = max(float(new_sales_count or 0), float(avg_new_sales_count or 0))
    demand = max(float(sales_count or 0), float(avg_sales_count or 0))

    if new_demand >= 10:
        interval = 12 * 60 * 60
    elif new_demand >= 3:
        interval = 24 * 60 * 60
    elif new_demand >= 1 or demand >= 30:
        interval = 3 * 24 * 60 * 60
    elif demand >= 20:
        interval = 7 * 24 * 60 * 60
    elif demand >= 10:
        interval = 14 * 24 * 60 * 60
    elif demand >= 3:
        interval = 30 * 24 * 60 * 60
    else:
        interval = MAX_SKIN_INTERVAL_SECONDS

    return clamp_interval(interval), "fallback_demand"


def latest_sold_at(sales):
    values = [
        sale.get("sold_at")
        for sale in sales
        if isinstance(sale, dict) and sale.get("sold_at")
    ]
    return max(values) if values else None


def build_http_error_text(url, market_hash_name, current_key, status_code, headers, body):
    return (
        f"URL: {url}\n"
        f"Skin: {market_hash_name}\n"
        f"API key file: {current_key['path']}\n"
        f"Proxy: {current_key['proxy_label']}\n"
        f"HTTP: {status_code}\n"
        f"X-Ratelimit-Limit: {headers.get('X-Ratelimit-Limit')}\n"
        f"X-Ratelimit-Remaining: {headers.get('X-Ratelimit-Remaining')}\n"
        f"X-Ratelimit-Reset: {headers.get('X-Ratelimit-Reset')}\n"
        f"Body:\n{body}\n"
    )


def decode_sales_response(body, url, market_hash_name, current_key):
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SalesFetchError(
            f"CSFloat returned invalid JSON for {market_hash_name}: {exc}",
            outcome="invalid_payload",
        ) from exc

    if isinstance(payload, list):
        return SalesPage(sales=payload)

    if isinstance(payload, dict):
        for key in ("data", "sales"):
            if isinstance(payload.get(key), list):
                cursor = payload.get("cursor") or payload.get("next_cursor")
                return SalesPage(
                    sales=payload[key],
                    cursor=str(cursor) if cursor not in (None, "") else None,
                )

    error_text = (
        f"URL: {url}\n"
        f"Skin: {market_hash_name}\n"
        f"API key file: {current_key['path']}\n"
        f"Proxy: {current_key['proxy_label']}\n"
        f"Unexpected JSON type: {type(payload).__name__}\n"
        f"Body:\n{body}\n"
    )
    Path("last_sales_error.txt").write_text(error_text, encoding="utf-8")
    print(error_text, flush=True)
    raise SalesFetchError(
        f"Unexpected CSFloat response shape for {market_hash_name}",
        outcome="invalid_payload",
    )


def request_sales(market_hash_name, *, cursor=None, paint_index=None):
    started = time.monotonic()
    encoded_name = urllib.parse.quote(market_hash_name, safe="")
    url = f"{BASE_URL}/{encoded_name}/sales"
    query = {}
    if paint_index is not None:
        query["paint_index"] = int(paint_index)
    if cursor:
        query["cursor"] = str(cursor)
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    attempted_transport_indices = set()

    while True:
        current_key = get_current_key()
        print(
            f"Using API key: {current_key['path']} via {current_key['proxy_label']}",
            flush=True,
        )

        headers = {
            "Authorization": current_key["value"],
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }

        try:
            if current_key["client"]["type"] == "requests":
                response = current_key["client"]["session"].get(
                    url,
                    headers=headers,
                    timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                )
                update_current_key_rate(response.headers, response.status_code)
                body = response.text

                if response.status_code < 400:
                    if SAVE_LAST_RESPONSE_JSON:
                        Path("last_sales_response.json").write_text(body, encoding="utf-8")
                    page = decode_sales_response(body, url, market_hash_name, current_key)
                    page.http_status = response.status_code
                    page.duration_ms = int((time.monotonic() - started) * 1000)
                    return page

                error_text = build_http_error_text(
                    url,
                    market_hash_name,
                    current_key,
                    response.status_code,
                    response.headers,
                    body,
                )
                Path("last_sales_error.txt").write_text(error_text, encoding="utf-8")

                if response.status_code == 429:
                    reset = response.headers.get("X-Ratelimit-Reset")
                    mark_current_key_limited(reset, body)

                    if switch_to_available_key():
                        continue

                    print(error_text, flush=True)
                    sleep_with_message(seconds_until_any_key_available(), "All API keys are rate limited")
                    continue

                if response.status_code == 404:
                    print(f"Not found: {market_hash_name}", flush=True)
                    return SalesPage(
                        sales=[],
                        http_status=404,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )

                if is_explicit_auth_error(response.status_code, body):
                    mark_current_key_auth_failed(response.status_code)
                    if switch_to_available_key():
                        continue
                    raise SalesFetchError(
                        f"All CSFloat routes rejected authentication (HTTP {response.status_code})",
                        outcome="auth_error",
                        http_status=response.status_code,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )

                if response.status_code == 403:
                    print(error_text, flush=True)
                    raise SalesFetchError(
                        f"CSFloat forbids history for {market_hash_name}",
                        outcome="forbidden_item",
                        http_status=403,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )

                print(error_text, flush=True)
                raise SalesFetchError(
                    f"CSFloat HTTP {response.status_code} for {market_hash_name}",
                    http_status=response.status_code,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            request = urllib.request.Request(url, headers=headers)

            with current_key["client"]["opener"].open(request, timeout=60) as response:
                update_current_key_rate(response.headers, response.status)
                body = response.read().decode("utf-8", errors="replace")
                if SAVE_LAST_RESPONSE_JSON:
                    Path("last_sales_response.json").write_text(body, encoding="utf-8")
                page = decode_sales_response(body, url, market_hash_name, current_key)
                page.http_status = response.status
                page.duration_ms = int((time.monotonic() - started) * 1000)
                return page

        except urllib.error.HTTPError as exc:
            update_current_key_rate(exc.headers, exc.code)
            body = exc.read().decode("utf-8", errors="replace")

            error_text = build_http_error_text(
                url,
                market_hash_name,
                current_key,
                exc.code,
                exc.headers,
                body,
            )
            Path("last_sales_error.txt").write_text(error_text, encoding="utf-8")

            if exc.code == 429:
                reset = exc.headers.get("X-Ratelimit-Reset")
                mark_current_key_limited(reset, body)

                if switch_to_available_key():
                    continue

                print(error_text, flush=True)
                sleep_with_message(seconds_until_any_key_available(), "All API keys are rate limited")
                continue

            if exc.code == 404:
                print(f"Not found: {market_hash_name}", flush=True)
                return SalesPage(
                    sales=[],
                    http_status=404,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            if is_explicit_auth_error(exc.code, body):
                mark_current_key_auth_failed(exc.code)
                if switch_to_available_key():
                    continue
                raise SalesFetchError(
                    f"All CSFloat routes rejected authentication (HTTP {exc.code})",
                    outcome="auth_error",
                    http_status=exc.code,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            if exc.code == 403:
                print(error_text, flush=True)
                raise SalesFetchError(
                    f"CSFloat forbids history for {market_hash_name}",
                    outcome="forbidden_item",
                    http_status=403,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            print(error_text, flush=True)
            raise SalesFetchError(
                f"CSFloat HTTP {exc.code} for {market_hash_name}",
                http_status=exc.code,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        except KeyError as exc:
            error_text = (
                f"URL: {url}\n"
                f"Skin: {market_hash_name}\n"
                f"API key file: {current_key['path']}\n"
                f"Proxy: {current_key['proxy_label']}\n"
                f"Client config error: {exc}\n"
            )
            Path("last_sales_error.txt").write_text(error_text, encoding="utf-8")
            print(error_text, flush=True)
            raise SalesFetchError(
                f"CSFloat client configuration error for {market_hash_name}",
                outcome="config_error",
                duration_ms=int((time.monotonic() - started) * 1000),
            ) from exc

        except (urllib.error.URLError, ConnectionResetError, TimeoutError) as exc:
            error_text = (
                f"URL: {url}\n"
                f"Skin: {market_hash_name}\n"
                f"API key file: {current_key['path']}\n"
                f"Proxy: {current_key['proxy_label']}\n"
                f"Network/timeout error: {exc}\n"
            )
            Path("last_sales_error.txt").write_text(error_text, encoding="utf-8")
            print(error_text, flush=True)

            if fail_over_after_transport_error(exc, attempted_transport_indices):
                continue
            raise SalesFetchError(
                "No healthy CSFloat transport route remains for this request",
                outcome="transport_error",
                duration_ms=int((time.monotonic() - started) * 1000),
            ) from exc
        except json.JSONDecodeError as exc:
            current_key["last_http_status"] = "json_error"
            current_key["last_seen_at"] = iso_now()
            write_parser_status()
            error_text = (
                f"URL: {url}\n"
                f"Skin: {market_hash_name}\n"
                f"JSON decode error: {exc}\n"
            )
            Path("last_sales_error.txt").write_text(error_text, encoding="utf-8")
            print(error_text, flush=True)
            sleep_with_message(NETWORK_ERROR_WAIT_SECONDS, "JSON decode error")
            continue

        except Exception as exc:
            if current_key["client"]["type"] != "requests":
                raise

            requests = current_key["client"]["requests"]
            if not isinstance(exc, requests.exceptions.RequestException):
                raise

            error_text = (
                f"URL: {url}\n"
                f"Skin: {market_hash_name}\n"
                f"API key file: {current_key['path']}\n"
                f"Proxy: {current_key['proxy_label']}\n"
                f"Network/timeout error: {exc}\n"
            )
            Path("last_sales_error.txt").write_text(error_text, encoding="utf-8")
            print(error_text, flush=True)

            if fail_over_after_transport_error(exc, attempted_transport_indices):
                continue
            raise SalesFetchError(
                "No healthy CSFloat transport route remains for this request",
                outcome="transport_error",
                duration_ms=int((time.monotonic() - started) * 1000),
            ) from exc


def existing_listing_ids(conn, listing_ids):
    clean_ids = [str(value) for value in dict.fromkeys(listing_ids) if value not in (None, "")]
    if not clean_ids:
        return set()
    placeholders = ",".join("?" for _ in clean_ids)
    return {
        str(row[0])
        for row in conn.execute(
            f"SELECT listing_id FROM sales WHERE listing_id IN ({placeholders})",
            clean_ids,
        )
    }


def fetch_sales_window(conn, market_hash_name, *, paint_index=None):
    """Fetch a history window and follow only cursors explicitly returned by CSFloat."""

    window = SalesWindow()
    cursor = None
    seen_cursors = set()
    seen_ids = set()

    for page_number in range(MAX_CURSOR_PAGES):
        page = request_sales(
            market_hash_name,
            cursor=cursor,
            paint_index=paint_index if USE_PAINT_INDEX_FILTER else None,
        )
        window.page_count += 1
        window.page_sizes.append(len(page.sales))
        window.http_status = page.http_status
        window.duration_ms += page.duration_ms
        if page.http_status == 404 and page_number > 0:
            raise SalesFetchError(
                f"CSFloat rejected continuation cursor for {market_hash_name}",
                outcome="cursor_error",
                http_status=404,
                duration_ms=window.duration_ms,
            )

        page_ids = [
            str(sale.get("id"))
            for sale in page.sales
            if isinstance(sale, dict) and sale.get("id") not in (None, "")
        ]
        overlap = existing_listing_ids(conn, page_ids)
        window.overlap_ids.update(overlap)

        for sale in page.sales:
            if not isinstance(sale, dict):
                window.sales.append(sale)
                continue
            listing_id = sale.get("id")
            key = str(listing_id) if listing_id not in (None, "") else None
            if key and key in seen_ids:
                continue
            if key:
                seen_ids.add(key)
            window.sales.append(sale)

        next_cursor = page.cursor
        if not next_cursor:
            window.cursor_exhausted = True
            break

        window.cursor_supported = True
        if overlap:
            window.cursor_exhausted = True
            break
        if next_cursor in seen_cursors:
            window.cursor_exhausted = False
            break

        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        window.cursor_exhausted = False

    return window


def classify_sales_window(window, overlap_count):
    """Describe whether the fetched window reached a known history boundary."""

    if window.http_status == 404:
        return "not_found", False
    if window.cursor_supported:
        if window.cursor_exhausted:
            return "cursor_complete", False
        return "cursor_limit", True

    first_page_full = bool(window.page_sizes and window.page_sizes[0] >= SALES_PAGE_SIZE)
    if first_page_full and overlap_count <= 0:
        return "coverage_uncertain", True
    if first_page_full and overlap_count < 8:
        return "low_overlap", False
    if first_page_full:
        return "overlap_complete", False
    return "complete", False


def create_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            listing_id TEXT PRIMARY KEY,
            name TEXT,
            base_skin_name TEXT,
            wear_name TEXT,
            price_cents INTEGER,
            reference_base_price INTEGER,
            reference_predicted_price INTEGER,
            reference_float_factor REAL,
            reference_quantity INTEGER,
            float_value REAL,
            paint_seed INTEGER,
            paint_index INTEGER,
            inspect_link TEXT,
            serialized_inspect TEXT,
            state TEXT,
            created_at TEXT,
            sold_at TEXT,
            fetched_at TEXT,
            sale_type TEXT,
            is_seller INTEGER,
            is_watchlisted INTEGER,
            watchers INTEGER,
            item_asset_id TEXT,
            item_icon_url TEXT,
            item_name TEXT,
            item_description TEXT,
            item_collection TEXT,
            item_type TEXT,
            item_type_name TEXT,
            item_quality TEXT,
            item_rarity INTEGER,
            item_rarity_name TEXT,
            item_def_index INTEGER,
            item_wear_name TEXT,
            item_tradable INTEGER,
            item_is_commodity INTEGER,
            item_is_souvenir INTEGER,
            item_is_stattrak INTEGER,
            item_cs2_screenshot_id TEXT,
            item_cs2_screenshot_at TEXT,
            item_gs_sig TEXT,
            raw_json TEXT,
            item_raw_json TEXT,
            reference_raw_json TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sale_stickers (
            listing_id TEXT,
            sticker_index INTEGER,
            sticker_id INTEGER,
            slot INTEGER,
            name TEXT,
            wear REAL,
            price_cents INTEGER,
            icon_url TEXT,
            offset_x REAL,
            offset_y REAL,
            reference_price_cents INTEGER,
            reference_quantity INTEGER,
            raw_json TEXT,
            reference_raw_json TEXT,
            PRIMARY KEY (listing_id, sticker_index)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sale_keychains (
            listing_id TEXT NOT NULL,
            keychain_index INTEGER NOT NULL,
            keychain_id INTEGER,
            slot INTEGER,
            name TEXT,
            pattern INTEGER,
            offset_x REAL,
            offset_y REAL,
            offset_z REAL,
            price_cents INTEGER,
            reference_quantity INTEGER,
            reference_updated_at TEXT,
            icon_url TEXT,
            steam_market_url TEXT,
            raw_json TEXT,
            reference_raw_json TEXT,
            PRIMARY KEY (listing_id, keychain_index),
            FOREIGN KEY (listing_id) REFERENCES sales(listing_id) ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sale_keychains_name
        ON sale_keychains(name)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sale_keychains_keychain_id
        ON sale_keychains(keychain_id, name)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            id INTEGER PRIMARY KEY,
            skin_index INTEGER,
            market_hash_name TEXT,
            updated_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS skin_schedule (
            market_hash_name TEXT PRIMARY KEY,
            skin_order INTEGER,
            base_skin_name TEXT,
            wear_name TEXT,
            request_count INTEGER NOT NULL DEFAULT 0,
            total_sales_returned INTEGER NOT NULL DEFAULT 0,
            total_new_sales INTEGER NOT NULL DEFAULT 0,
            last_sales_count INTEGER,
            last_new_sales_count INTEGER,
            avg_sales_count REAL NOT NULL DEFAULT 0,
            avg_new_sales_count REAL NOT NULL DEFAULT 0,
            interval_seconds INTEGER NOT NULL DEFAULT 604800,
            interval_reason TEXT,
            sales_span_seconds INTEGER,
            new_sales_span_seconds INTEGER,
            last_fetch_at INTEGER,
            next_fetch_at INTEGER NOT NULL DEFAULT 0,
            last_sold_at TEXT,
            updated_at TEXT
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_skin_schedule_next_fetch
        ON skin_schedule(next_fetch_at)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_skin_schedule_order
        ON skin_schedule(skin_order)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS parser_request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requested_at INTEGER NOT NULL,
            market_hash_name TEXT NOT NULL,
            sales_count INTEGER NOT NULL,
            new_sales_count INTEGER NOT NULL,
            interval_seconds INTEGER,
            interval_reason TEXT,
            next_fetch_at INTEGER
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_parser_request_log_requested_at
        ON parser_request_log(requested_at)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS parser_quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quarantined_at TEXT NOT NULL,
            market_hash_name TEXT,
            listing_id TEXT,
            reason TEXT NOT NULL,
            payload_json TEXT
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_parser_quarantine_quarantined_at
        ON parser_quarantine(quarantined_at)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS parser_meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT NOT NULL
        )
    """)

    conn.commit()


def fix_progress_table(conn):
    row = conn.execute("""
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = 'progress'
    """).fetchone()

    if row and row[0] and "CHECK (id = 1)" in row[0]:
        conn.execute("ALTER TABLE progress RENAME TO progress_old")
        conn.execute("""
            CREATE TABLE progress (
                id INTEGER PRIMARY KEY,
                skin_index INTEGER,
                market_hash_name TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            INSERT INTO progress (
                id,
                skin_index,
                market_hash_name,
                updated_at
            )
            SELECT
                id,
                skin_index,
                market_hash_name,
                updated_at
            FROM progress_old
        """)
        conn.execute("DROP TABLE progress_old")
        conn.commit()


def ensure_columns(conn, table_name, new_columns):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}

    for column_name, column_type in new_columns.items():
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    conn.commit()


def ensure_sales_columns(conn):
    new_columns = {
        "reference_base_price": "INTEGER",
        "reference_predicted_price": "INTEGER",
        "reference_float_factor": "REAL",
        "reference_quantity": "INTEGER",
        **SALES_EXTRA_COLUMNS,
    }
    ensure_columns(conn, "sales", new_columns)


def ensure_sticker_columns(conn):
    ensure_columns(conn, "sale_stickers", STICKER_EXTRA_COLUMNS)


def ensure_schedule_columns(conn):
    ensure_columns(conn, "skin_schedule", {
        "interval_reason": "TEXT",
        "sales_span_seconds": "INTEGER",
        "new_sales_span_seconds": "INTEGER",
        **SCHEDULE_EXTRA_COLUMNS,
    })


def ensure_request_log_columns(conn):
    ensure_columns(conn, "parser_request_log", REQUEST_LOG_EXTRA_COLUMNS)


def ensure_parser_indexes(conn):
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sales_fetched_listing
        ON sales(fetched_at, listing_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sales_name_sold_at
        ON sales(name, sold_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_skin_schedule_due_risk
        ON skin_schedule(next_fetch_at, saturation_streak, estimated_sales_per_day)
    """)
    conn.commit()


def get_parser_meta(conn, key):
    row = conn.execute("SELECT value FROM parser_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_parser_meta(conn, key, value):
    conn.execute("""
        INSERT INTO parser_meta (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
    """, (key, str(value), iso_now()))


def migrate_legacy_schedule_v2(conn):
    """Spread legacy capped histories over a short recovery window once."""

    marker = "parser_v2_schedule_recovery"
    if get_parser_meta(conn, marker):
        return 0

    latest_rows = conn.execute("""
        WITH latest AS (
            SELECT market_hash_name, MAX(id) AS id
            FROM parser_request_log
            GROUP BY market_hash_name
        )
        SELECT
            log.market_hash_name,
            log.sales_count,
            log.new_sales_count,
            schedule.interval_seconds,
            schedule.next_fetch_at,
            schedule.skin_order,
            schedule.saturation_streak
        FROM latest
        JOIN parser_request_log AS log ON log.id = latest.id
        JOIN skin_schedule AS schedule
            ON schedule.market_hash_name = log.market_hash_name
        WHERE log.sales_count >= ?
            AND COALESCE(log.outcome, 'success') = 'success'
    """, (SALES_PAGE_SIZE,)).fetchall()
    capped_log_count = int(conn.execute("""
        WITH latest AS (
            SELECT market_hash_name, MAX(id) AS id
            FROM parser_request_log
            GROUP BY market_hash_name
        )
        SELECT COUNT(*)
        FROM latest
        JOIN parser_request_log AS log ON log.id = latest.id
        WHERE log.sales_count >= ?
            AND COALESCE(log.outcome, 'success') = 'success'
    """, (SALES_PAGE_SIZE,)).fetchone()[0])

    now = unix_now()
    updates = []
    saturated_count = 0
    for row in latest_rows:
        market_hash_name = row[0]
        all_new = int(row[2] or 0) >= SALES_PAGE_SIZE
        old_interval = int(row[3] or MAX_SKIN_INTERVAL_SECONDS)
        old_next_fetch_at = int(row[4] or 0)
        skin_order = int(row[5] or 0)
        old_streak = int(row[6] or 0)
        recovery_window = 6 * 60 * 60 if all_new else INITIAL_EXPLORATION_WINDOW_SECONDS
        due_at = now + int(
            stable_schedule_fraction(market_hash_name, skin_order) * recovery_window
        )
        if old_next_fetch_at > now:
            due_at = min(old_next_fetch_at, due_at)
        interval_factor = 0.5 if all_new else TARGET_NEW_SALES_PER_REQUEST / 35.0
        interval_seconds = clamp_interval(old_interval * interval_factor)
        coverage_status = "coverage_uncertain" if all_new else "legacy_cap_recheck"
        interval_reason = "saturated_catchup" if all_new else "legacy_cap_recheck"
        saturation_streak = max(1, old_streak) if all_new else old_streak
        saturated_count += int(all_new)
        updates.append((
            interval_seconds,
            interval_reason,
            due_at,
            coverage_status,
            saturation_streak,
            iso_now(),
            market_hash_name,
        ))

    if updates:
        conn.executemany("""
            UPDATE skin_schedule
            SET
                interval_seconds = ?,
                interval_reason = ?,
                next_fetch_at = ?,
                coverage_status = ?,
                saturation_streak = ?,
                updated_at = ?
            WHERE market_hash_name = ?
        """, updates)

    if len(updates) == capped_log_count:
        set_parser_meta(conn, marker, dumps_json({
            "capped_skins": len(updates),
            "fully_new_skins": saturated_count,
            "target_new_sales": TARGET_NEW_SALES_PER_REQUEST,
        }))
    conn.commit()
    return len(updates)


def backfill_derived_metadata(conn, *, force=False):
    """Populate compact keychain/sticker fields without rewriting sale snapshots."""

    marker = "parser_v2_derived_metadata"
    if get_parser_meta(conn, marker) and not force:
        return {"keychains": 0, "stickers": 0, "already_done": True}

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""
            INSERT OR IGNORE INTO sale_keychains (
                listing_id,
                keychain_index,
                keychain_id,
                slot,
                name,
                pattern,
                offset_x,
                offset_y,
                offset_z,
                price_cents,
                reference_quantity,
                reference_updated_at,
                icon_url,
                steam_market_url,
                raw_json,
                reference_raw_json
            )
            SELECT
                sale.listing_id,
                CAST(keychain.key AS INTEGER),
                COALESCE(
                    json_extract(keychain.value, '$.stickerId'),
                    json_extract(keychain.value, '$.keychain_id'),
                    json_extract(keychain.value, '$.id')
                ),
                json_extract(keychain.value, '$.slot'),
                json_extract(keychain.value, '$.name'),
                json_extract(keychain.value, '$.pattern'),
                json_extract(keychain.value, '$.offset_x'),
                json_extract(keychain.value, '$.offset_y'),
                json_extract(keychain.value, '$.offset_z'),
                COALESCE(
                    json_extract(keychain.value, '$.reference.price'),
                    json_extract(keychain.value, '$.scm.price')
                ),
                COALESCE(
                    json_extract(keychain.value, '$.reference.quantity'),
                    json_extract(keychain.value, '$.scm.quantity')
                ),
                COALESCE(
                    json_extract(keychain.value, '$.reference.updated_at'),
                    json_extract(keychain.value, '$.scm.updated_at')
                ),
                json_extract(keychain.value, '$.icon_url'),
                json_extract(keychain.value, '$.steam_market_url'),
                keychain.value,
                COALESCE(
                    json_extract(keychain.value, '$.reference'),
                    json_extract(keychain.value, '$.scm')
                )
            FROM sales AS sale
            JOIN json_each(
                CASE
                    WHEN json_valid(sale.item_raw_json) THEN sale.item_raw_json
                    ELSE '{}'
                END,
                '$.keychains'
            ) AS keychain
            WHERE keychain.type = 'object'
        """)
        keychain_count = int(conn.execute("SELECT changes()").fetchone()[0])

        conn.execute("""
            UPDATE sale_stickers
            SET
                rotation = COALESCE(
                    rotation,
                    json_extract(
                        CASE WHEN json_valid(raw_json) THEN raw_json ELSE '{}'
                        END,
                        '$.rotation'
                    )
                ),
                reference_updated_at = COALESCE(
                    reference_updated_at,
                    json_extract(
                        CASE
                            WHEN json_valid(reference_raw_json) THEN reference_raw_json
                            ELSE '{}'
                        END,
                        '$.updated_at'
                    ),
                    json_extract(
                        CASE WHEN json_valid(raw_json) THEN raw_json ELSE '{}'
                        END,
                        '$.reference.updated_at'
                    ),
                    json_extract(
                        CASE WHEN json_valid(raw_json) THEN raw_json ELSE '{}'
                        END,
                        '$.scm.updated_at'
                    )
                )
            WHERE
                (
                    rotation IS NULL
                    AND json_extract(
                        CASE WHEN json_valid(raw_json) THEN raw_json ELSE '{}'
                        END,
                        '$.rotation'
                    ) IS NOT NULL
                )
                OR (
                    reference_updated_at IS NULL
                    AND (
                        json_extract(
                            CASE
                                WHEN json_valid(reference_raw_json) THEN reference_raw_json
                                ELSE '{}'
                            END,
                            '$.updated_at'
                        ) IS NOT NULL
                        OR COALESCE(
                            json_extract(
                                CASE WHEN json_valid(raw_json) THEN raw_json ELSE '{}'
                                END,
                                '$.reference.updated_at'
                            ),
                            json_extract(
                                CASE WHEN json_valid(raw_json) THEN raw_json ELSE '{}'
                                END,
                                '$.scm.updated_at'
                            )
                        ) IS NOT NULL
                    )
                )
        """)
        sticker_count = int(conn.execute("SELECT changes()").fetchone()[0])
        result = {
            "keychains": keychain_count,
            "stickers": sticker_count,
            "already_done": False,
        }
        set_parser_meta(conn, marker, dumps_json(result))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def backfill_sale_metadata(conn, *, force=False):
    """Normalize legacy observation/reference metadata in one versioned pass."""

    marker = "parser_v3_sale_metadata"
    if get_parser_meta(conn, marker) and not force:
        return {
            "rows_updated": 0,
            "first_seen": 0,
            "last_seen": 0,
            "reference_updated": 0,
            "keychain_price": 0,
            "already_done": True,
        }

    counts = conn.execute("""
        SELECT
            SUM(first_seen_at IS NULL AND fetched_at IS NOT NULL),
            SUM(last_seen_at IS NULL AND fetched_at IS NOT NULL),
            SUM(
                reference_last_updated IS NULL
                AND json_extract(
                    CASE
                        WHEN json_valid(reference_raw_json) THEN reference_raw_json
                        ELSE '{}'
                    END,
                    '$.last_updated'
                ) IS NOT NULL
            ),
            SUM(
                reference_keychain_price IS NULL
                AND json_extract(
                    CASE
                        WHEN json_valid(reference_raw_json) THEN reference_raw_json
                        ELSE '{}'
                    END,
                    '$.keychain_price'
                ) IS NOT NULL
            )
        FROM sales
    """).fetchone()
    result = {
        "first_seen": int(counts[0] or 0),
        "last_seen": int(counts[1] or 0),
        "reference_updated": int(counts[2] or 0),
        "keychain_price": int(counts[3] or 0),
        "already_done": False,
    }

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""
            UPDATE sales
            SET
                first_seen_at = COALESCE(first_seen_at, fetched_at),
                last_seen_at = COALESCE(last_seen_at, fetched_at),
                reference_last_updated = COALESCE(
                    reference_last_updated,
                    CASE
                        WHEN json_valid(reference_raw_json)
                        THEN json_extract(reference_raw_json, '$.last_updated')
                    END
                ),
                reference_keychain_price = COALESCE(
                    reference_keychain_price,
                    CASE
                        WHEN json_valid(reference_raw_json)
                        THEN json_extract(reference_raw_json, '$.keychain_price')
                    END
                )
            WHERE
                (first_seen_at IS NULL AND fetched_at IS NOT NULL)
                OR (last_seen_at IS NULL AND fetched_at IS NOT NULL)
                OR (
                    reference_last_updated IS NULL
                    AND json_extract(
                        CASE
                            WHEN json_valid(reference_raw_json) THEN reference_raw_json
                            ELSE '{}'
                        END,
                        '$.last_updated'
                    ) IS NOT NULL
                )
                OR (
                    reference_keychain_price IS NULL
                    AND json_extract(
                        CASE
                            WHEN json_valid(reference_raw_json) THEN reference_raw_json
                            ELSE '{}'
                        END,
                        '$.keychain_price'
                    ) IS NOT NULL
                )
        """)
        result["rows_updated"] = int(conn.execute("SELECT changes()").fetchone()[0])
        set_parser_meta(conn, marker, dumps_json(result))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def run_schema_migrations(conn):
    create_tables(conn)
    fix_progress_table(conn)
    ensure_sales_columns(conn)
    ensure_sticker_columns(conn)
    ensure_schedule_columns(conn)
    ensure_request_log_columns(conn)
    ensure_parser_indexes(conn)


def save_request_log(
    conn,
    market_hash_name,
    sales_count,
    new_sales_count,
    interval_seconds,
    interval_reason,
    next_fetch_at,
    *,
    outcome="success",
    http_status=None,
    duration_ms=None,
    updated_sales_count=0,
    overlap_count=0,
    rejected_sales_count=0,
    page_count=1,
    page_saturated=False,
    coverage_status=None,
    error=None,
):
    conn.execute("""
        INSERT INTO parser_request_log (
            requested_at,
            market_hash_name,
            sales_count,
            new_sales_count,
            interval_seconds,
            interval_reason,
            next_fetch_at,
            outcome,
            http_status,
            duration_ms,
            updated_sales_count,
            overlap_count,
            rejected_sales_count,
            page_count,
            page_saturated,
            coverage_status,
            error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        unix_now(),
        market_hash_name,
        sales_count,
        new_sales_count,
        interval_seconds,
        interval_reason,
        next_fetch_at,
        outcome,
        http_status,
        duration_ms,
        updated_sales_count,
        overlap_count,
        rejected_sales_count,
        page_count,
        bool_to_int(page_saturated),
        coverage_status,
        str(error)[:1000] if error else None,
    ))


def load_historical_skin_stats(conn, market_hash_names=None):
    stats = {}

    names = None if market_hash_names is None else list(dict.fromkeys(market_hash_names))
    if names == []:
        return stats

    join_sql = ""
    if names is not None:
        conn.execute("DROP TABLE IF EXISTS temp.parser_requested_skin_names")
        conn.execute("""
            CREATE TEMP TABLE parser_requested_skin_names (
                name TEXT PRIMARY KEY
            ) WITHOUT ROWID
        """)
        conn.executemany(
            "INSERT INTO parser_requested_skin_names(name) VALUES (?)",
            ((name,) for name in names),
        )
        join_sql = "JOIN parser_requested_skin_names requested ON requested.name = sales.name"

    rows = conn.execute(f"""
        WITH ranked_sales AS (
            SELECT
                sales.name,
                sales.sold_at,
                sales.fetched_at,
                ROW_NUMBER() OVER (
                    PARTITION BY sales.name
                    ORDER BY sales.sold_at DESC, sales.fetched_at DESC
                ) AS rn
            FROM sales
            {join_sql}
            WHERE sales.name IS NOT NULL
                AND sales.sold_at IS NOT NULL
        )
        SELECT
            name,
            COUNT(*) AS recent_sales_count,
            MIN(sold_at) AS oldest_recent_sold_at,
            MAX(sold_at) AS newest_recent_sold_at,
            MAX(fetched_at) AS last_fetch_at
        FROM ranked_sales
        WHERE rn <= ?
        GROUP BY name
    """, (HISTORY_STATS_RECENT_SALES_LIMIT,)).fetchall()

    if names is not None:
        conn.execute("DROP TABLE temp.parser_requested_skin_names")

    for row in rows:
        oldest_sold_at = row[2]
        newest_sold_at = row[3]
        oldest_ts = parse_csfloat_time(oldest_sold_at)
        newest_ts = parse_csfloat_time(newest_sold_at)
        sales_span_seconds = None
        if oldest_ts is not None and newest_ts is not None and newest_ts > oldest_ts:
            sales_span_seconds = max(60, int(newest_ts - oldest_ts))

        last_fetch_at = row[4]
        last_fetch_ts = parse_csfloat_time(last_fetch_at)

        stats[row[0]] = {
            "sales_count": row[1] or 0,
            "oldest_sold_at": oldest_sold_at,
            "last_sold_at": newest_sold_at,
            "sales_span_seconds": sales_span_seconds,
            "last_fetch_at": last_fetch_at,
            "last_fetch_ts": int(last_fetch_ts) if last_fetch_ts is not None else None,
        }

    return stats


def ensure_skin_schedule(conn, skins):
    now = unix_now()
    updated_at = iso_now()
    existing = {
        row[0]: {
            "request_count": int(row[1] or 0),
            "last_fetch_at": row[2],
            "next_fetch_at": row[3],
        }
        for row in conn.execute("""
            SELECT market_hash_name, request_count, last_fetch_at, next_fetch_at
            FROM skin_schedule
        """)
    }
    missing_names = [
        skin["market_hash_name"]
        for skin in skins
        if skin["market_hash_name"] not in existing
    ]
    historical_stats = load_historical_skin_stats(conn, missing_names)
    seed_rows = []

    for skin_order, skin in enumerate(skins):
        market_hash_name = skin["market_hash_name"]
        if market_hash_name in existing:
            continue
        stats = historical_stats.get(market_hash_name, {})
        demand_seed = stats.get("sales_count", 0) or safe_int(skin.get("quantity_on_market")) or 0
        seed_score = historical_count_to_seed_score(demand_seed)
        if seed_score > 0:
            seed_rows.append((seed_score, skin_order, market_hash_name))

    bootstrap_due_names = {
        market_hash_name
        for seed_score, skin_order, market_hash_name in sorted(
            seed_rows,
            key=lambda row: (-row[0], row[1]),
        )[:INITIAL_BOOTSTRAP_DUE_COUNT]
    }

    for skin_order, skin in enumerate(skins):
        market_hash_name = skin["market_hash_name"]
        base_skin_name = extract_base_skin_name(market_hash_name)
        wear_name = extract_wear_name(market_hash_name)
        paint_index = safe_int(skin.get("paint_index"))

        if market_hash_name in existing:
            exploration_next_fetch_at = now + int(
                stable_schedule_fraction(market_hash_name, skin_order)
                * INITIAL_EXPLORATION_WINDOW_SECONDS
            )
            conn.execute("""
                UPDATE skin_schedule
                SET
                    skin_order = ?,
                    base_skin_name = ?,
                    wear_name = ?,
                    paint_index = ?,
                    next_fetch_at = CASE
                        WHEN COALESCE(request_count, 0) = 0
                        THEN MIN(COALESCE(next_fetch_at, ?), ?)
                        ELSE next_fetch_at
                    END,
                    interval_reason = CASE
                        WHEN COALESCE(request_count, 0) = 0
                            AND COALESCE(interval_reason, '') NOT IN (
                                'bootstrap_now',
                                'exploration_v2'
                            )
                        THEN 'exploration_v2'
                        ELSE interval_reason
                    END
                WHERE market_hash_name = ?
                    AND (
                        skin_order IS NOT ?
                        OR base_skin_name IS NOT ?
                        OR wear_name IS NOT ?
                        OR paint_index IS NOT ?
                        OR (
                            COALESCE(request_count, 0) = 0
                            AND (
                                next_fetch_at IS NULL
                                OR next_fetch_at > ?
                                OR COALESCE(interval_reason, '') NOT IN (
                                    'bootstrap_now',
                                    'exploration_v2'
                                )
                            )
                        )
                    )
            """, (
                skin_order,
                base_skin_name,
                wear_name,
                paint_index,
                exploration_next_fetch_at,
                exploration_next_fetch_at,
                market_hash_name,
                skin_order,
                base_skin_name,
                wear_name,
                paint_index,
                exploration_next_fetch_at,
            ))
            continue

        stats = historical_stats.get(market_hash_name, {})
        demand_seed = stats.get("sales_count", 0) or safe_int(skin.get("quantity_on_market")) or 0
        seed_score = historical_count_to_seed_score(demand_seed)
        history_sales_count = stats.get("sales_count", 0)
        history_span_seconds = stats.get("sales_span_seconds")
        last_fetch_ts = stats.get("last_fetch_ts")
        estimated_sales_per_day = (
            float(history_sales_count) * 86400.0 / float(history_span_seconds)
            if history_sales_count and history_span_seconds
            else 0.0
        )

        if history_sales_count and history_span_seconds:
            interval_seconds = compute_target_interval_seconds(
                history_sales_count,
                history_span_seconds,
            )
            interval_reason = "history_from_db"
            initial_next_fetch_at = (last_fetch_ts or now) + interval_seconds
        elif market_hash_name in bootstrap_due_names:
            interval_seconds, _ = compute_skin_interval_seconds(seed_score, 0, seed_score, 0)
            initial_next_fetch_at = now
            interval_reason = "bootstrap_now"
        else:
            interval_seconds, interval_reason = compute_skin_interval_seconds(seed_score, 0, seed_score, 0)
            initial_next_fetch_at = now + initial_next_fetch_offset_seconds(
                seed_score,
                skin_order,
                market_hash_name,
            )

        conn.execute("""
            INSERT INTO skin_schedule (
                market_hash_name,
                skin_order,
                base_skin_name,
                wear_name,
                avg_sales_count,
                avg_new_sales_count,
                interval_seconds,
                interval_reason,
                sales_span_seconds,
                last_fetch_at,
                next_fetch_at,
                last_sold_at,
                updated_at,
                paint_index,
                estimated_sales_per_day,
                last_success_at,
                coverage_status,
                last_outcome
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            market_hash_name,
            skin_order,
            base_skin_name,
            wear_name,
            history_sales_count or seed_score,
            interval_seconds,
            interval_reason,
            history_span_seconds,
            last_fetch_ts,
            initial_next_fetch_at,
            stats.get("last_sold_at"),
            updated_at,
            paint_index,
            estimated_sales_per_day,
            last_fetch_ts,
            "seeded_history" if history_sales_count else "never_fetched",
            "seeded" if history_sales_count else None,
        ))

    for market_hash_name in bootstrap_due_names:
        conn.execute("""
            UPDATE skin_schedule
            SET next_fetch_at = ?, interval_reason = 'bootstrap_now', updated_at = ?
            WHERE market_hash_name = ? AND COALESCE(request_count, 0) = 0
        """, (now, updated_at, market_hash_name))

    conn.execute("""
        UPDATE skin_schedule
        SET
            next_fetch_at = ?,
            interval_seconds = ?,
            interval_reason = 'initial_no_signal'
        WHERE next_fetch_at IS NULL
    """, (now + MAX_SKIN_INTERVAL_SECONDS, MAX_SKIN_INTERVAL_SECONDS))

    conn.commit()


def get_next_scheduled_skin(conn):
    now = unix_now()
    select_sql = """
        SELECT
            market_hash_name,
            skin_order,
            base_skin_name,
            wear_name,
            request_count,
            avg_sales_count,
            avg_new_sales_count,
            interval_seconds,
            last_fetch_at,
            next_fetch_at,
            paint_index,
            saturation_streak,
            estimated_sales_per_day,
            coverage_status
        FROM skin_schedule
    """
    row = conn.execute(select_sql + """
        WHERE next_fetch_at <= ?
        ORDER BY
            COALESCE(saturation_streak, 0) DESC,
            CASE WHEN COALESCE(request_count, 0) = 0 THEN 1 ELSE 0 END DESC,
            CAST((? - next_fetch_at) AS REAL)
                / MAX(COALESCE(interval_seconds, 1), 1) DESC,
            COALESCE(estimated_sales_per_day, 0) DESC,
            next_fetch_at ASC,
            skin_order ASC
        LIMIT 1
    """, (now, now)).fetchone()
    if row:
        return row
    return conn.execute(select_sql + """
        WHERE next_fetch_at IS NOT NULL
        ORDER BY next_fetch_at ASC
        LIMIT 1
    """).fetchone()


def deterministic_interval_jitter(
    interval_seconds,
    market_hash_name,
    request_count,
    *,
    minimum=MIN_SKIN_INTERVAL_SECONDS,
    maximum=MAX_SKIN_INTERVAL_SECONDS,
):
    interval = float(interval_seconds)
    fraction = stable_schedule_fraction(market_hash_name, request_count)
    factor = 1.0 + ((fraction * 2.0) - 1.0) * SCHEDULE_JITTER_FRACTION
    return max(int(minimum), min(int(maximum), int(interval * factor)))


def adaptive_schedule_values(
    *,
    market_hash_name,
    now,
    sales_count,
    new_sales_count,
    overlap_count,
    page_saturated,
    old_rate_per_day,
    old_interval_seconds,
    old_saturation_streak,
    previous_success_at,
    sales_span_seconds=None,
    new_sales_span_seconds=None,
    request_count=0,
    not_found=False,
):
    if not_found:
        return {
            "interval_seconds": MAX_SKIN_INTERVAL_SECONDS,
            "interval_reason": "not_found",
            "estimated_sales_per_day": max(0.0, float(old_rate_per_day or 0) * 0.5),
            "saturation_streak": 0,
        }

    elapsed_seconds = None
    if previous_success_at is not None and int(previous_success_at) < int(now):
        elapsed_seconds = max(1, int(now) - int(previous_success_at))

    elapsed_rate = None
    if elapsed_seconds is not None:
        elapsed_rate = max(0.0, float(new_sales_count or 0) * 86400.0 / elapsed_seconds)

    span_rate = None
    if new_sales_count and new_sales_span_seconds:
        span_rate = float(new_sales_count) * 86400.0 / max(60.0, float(new_sales_span_seconds))
    elif sales_count and sales_span_seconds:
        span_rate = float(sales_count) * 86400.0 / max(60.0, float(sales_span_seconds))

    old_rate = max(0.0, float(old_rate_per_day or 0))
    if page_saturated:
        observed_rate = max(elapsed_rate or 0.0, span_rate or 0.0)
        estimated_rate = max(old_rate, observed_rate)
        if estimated_rate > 0:
            estimated_rate *= 1.05
    elif elapsed_rate is not None:
        if old_rate > 0:
            estimated_rate = old_rate * (1 - SCHEDULER_ALPHA) + elapsed_rate * SCHEDULER_ALPHA
        else:
            estimated_rate = elapsed_rate
        if not new_sales_count:
            estimated_rate *= 0.9
    elif span_rate is not None:
        estimated_rate = old_rate * (1 - SCHEDULER_ALPHA) + span_rate * SCHEDULER_ALPHA if old_rate else span_rate
    else:
        estimated_rate = old_rate * 0.9

    estimated_rate = max(0.0, estimated_rate)
    if page_saturated:
        saturation_streak = int(old_saturation_streak or 0) + 1
        if estimated_rate > 0:
            interval = SATURATED_TARGET_NEW_SALES * 86400.0 / estimated_rate
        else:
            interval = max(MIN_SKIN_INTERVAL_SECONDS, float(old_interval_seconds or 86400) * 0.5)
        if old_interval_seconds:
            interval = min(interval, float(old_interval_seconds) * 0.5)
        interval *= 0.7 ** min(max(saturation_streak - 1, 0), 3)
        interval_reason = "saturated_catchup"
    else:
        saturation_streak = 0
        if estimated_rate > 0:
            interval = TARGET_NEW_SALES_PER_REQUEST * 86400.0 / estimated_rate
            interval_reason = "adaptive_rate"
        else:
            interval, interval_reason = compute_skin_interval_seconds(
                sales_count,
                new_sales_count,
                sales_count,
                new_sales_count,
                sales_span_seconds=sales_span_seconds,
                new_sales_span_seconds=new_sales_span_seconds,
            )

        if sales_count >= SALES_PAGE_SIZE and overlap_count < 8 and old_interval_seconds:
            interval = min(interval, float(old_interval_seconds) * 0.75)
            interval_reason = "low_overlap"

    interval = deterministic_interval_jitter(
        clamp_interval(interval),
        market_hash_name,
        request_count,
    )
    return {
        "interval_seconds": interval,
        "interval_reason": interval_reason,
        "estimated_sales_per_day": estimated_rate,
        "saturation_streak": saturation_streak,
    }


def update_skin_schedule(
    conn,
    market_hash_name,
    sales_count,
    new_sales_count,
    last_sold_at,
    sales_span_seconds=None,
    new_sales_span_seconds=None,
    *,
    overlap_count=0,
    page_saturated=False,
    coverage_status="complete",
    outcome="success",
    http_status=200,
    duration_ms=0,
):
    now = unix_now()
    row = conn.execute("""
        SELECT
            request_count,
            total_sales_returned,
            total_new_sales,
            avg_sales_count,
            avg_new_sales_count,
            last_sold_at,
            interval_seconds,
            estimated_sales_per_day,
            saturation_streak,
            COALESCE(last_success_at, last_fetch_at),
            attempt_count
        FROM skin_schedule
        WHERE market_hash_name = ?
    """, (market_hash_name,)).fetchone()

    if row:
        request_count = int(row[0] or 0) + 1
        total_sales_returned = int(row[1] or 0) + sales_count
        total_new_sales = int(row[2] or 0) + new_sales_count
        old_avg_sales = float(row[3] or 0)
        old_avg_new_sales = float(row[4] or 0)
        previous_last_sold_at = row[5]
        old_interval_seconds = row[6]
        old_rate_per_day = row[7]
        old_saturation_streak = row[8]
        previous_success_at = row[9]
        attempt_count = int(row[10] or 0) + 1
    else:
        request_count = 1
        total_sales_returned = sales_count
        total_new_sales = new_sales_count
        old_avg_sales = 0
        old_avg_new_sales = 0
        previous_last_sold_at = None
        old_interval_seconds = None
        old_rate_per_day = 0
        old_saturation_streak = 0
        previous_success_at = None
        attempt_count = 1

    if request_count == 1 and old_avg_sales == 0:
        avg_sales_count = float(sales_count)
        avg_new_sales_count = float(new_sales_count)
    else:
        avg_sales_count = old_avg_sales * (1 - SCHEDULER_ALPHA) + sales_count * SCHEDULER_ALPHA
        avg_new_sales_count = old_avg_new_sales * (1 - SCHEDULER_ALPHA) + new_sales_count * SCHEDULER_ALPHA

    adaptive = adaptive_schedule_values(
        market_hash_name=market_hash_name,
        now=now,
        sales_count=sales_count,
        new_sales_count=new_sales_count,
        overlap_count=overlap_count,
        page_saturated=page_saturated,
        old_rate_per_day=old_rate_per_day,
        old_interval_seconds=old_interval_seconds,
        old_saturation_streak=old_saturation_streak,
        previous_success_at=previous_success_at,
        sales_span_seconds=sales_span_seconds,
        new_sales_span_seconds=new_sales_span_seconds,
        request_count=request_count,
        not_found=outcome == "not_found",
    )
    interval_seconds = adaptive["interval_seconds"]
    interval_reason = adaptive["interval_reason"]
    next_fetch_at = now + interval_seconds

    if previous_last_sold_at and last_sold_at:
        last_sold_at = max(previous_last_sold_at, last_sold_at)
    elif previous_last_sold_at:
        last_sold_at = previous_last_sold_at

    conn.execute("""
        UPDATE skin_schedule
        SET
            request_count = ?,
            total_sales_returned = ?,
            total_new_sales = ?,
            last_sales_count = ?,
            last_new_sales_count = ?,
            avg_sales_count = ?,
            avg_new_sales_count = ?,
            interval_seconds = ?,
            interval_reason = ?,
            sales_span_seconds = ?,
            new_sales_span_seconds = ?,
            last_fetch_at = ?,
            next_fetch_at = ?,
            last_sold_at = ?,
            updated_at = ?,
            attempt_count = ?,
            estimated_sales_per_day = ?,
            saturation_streak = ?,
            last_page_saturated = ?,
            coverage_status = ?,
            last_attempt_at = ?,
            last_success_at = ?,
            consecutive_errors = 0,
            last_outcome = ?,
            last_http_status = ?,
            last_error = NULL,
            last_error_at = NULL,
            last_duration_ms = ?
        WHERE market_hash_name = ?
    """, (
        request_count,
        total_sales_returned,
        total_new_sales,
        sales_count,
        new_sales_count,
        avg_sales_count,
        avg_new_sales_count,
        interval_seconds,
        interval_reason,
        sales_span_seconds,
        new_sales_span_seconds,
        now,
        next_fetch_at,
        last_sold_at,
        iso_now(),
        attempt_count,
        adaptive["estimated_sales_per_day"],
        adaptive["saturation_streak"],
        bool_to_int(page_saturated),
        coverage_status,
        now,
        now,
        outcome,
        http_status,
        duration_ms,
        market_hash_name,
    ))

    return interval_seconds, next_fetch_at, avg_sales_count, avg_new_sales_count, interval_reason


def record_skin_failure(
    conn,
    market_hash_name,
    error,
    *,
    sales_count=0,
    rejected_sales_count=0,
    page_count=0,
):
    now = unix_now()
    row = conn.execute("""
        SELECT
            COALESCE(attempt_count, 0),
            COALESCE(consecutive_errors, 0),
            COALESCE(interval_seconds, ?)
        FROM skin_schedule
        WHERE market_hash_name = ?
    """, (MAX_SKIN_INTERVAL_SECONDS, market_hash_name)).fetchone()
    attempt_count = int(row[0] or 0) + 1 if row else 1
    consecutive_errors = int(row[1] or 0) + 1 if row else 1
    retry_seconds = min(
        ERROR_RETRY_MAX_SECONDS,
        ERROR_RETRY_BASE_SECONDS * (2 ** min(consecutive_errors - 1, 6)),
    )
    retry_seconds = deterministic_interval_jitter(
        retry_seconds,
        market_hash_name,
        attempt_count,
        minimum=ERROR_RETRY_BASE_SECONDS,
        maximum=ERROR_RETRY_MAX_SECONDS,
    )
    next_fetch_at = now + retry_seconds
    error_text = str(error)[:1000]
    outcome = getattr(error, "outcome", "transient_error")
    http_status = getattr(error, "http_status", None)
    duration_ms = getattr(error, "duration_ms", None)

    conn.execute("""
        UPDATE skin_schedule
        SET
            attempt_count = ?,
            next_fetch_at = ?,
            last_attempt_at = ?,
            consecutive_errors = ?,
            last_outcome = ?,
            last_http_status = ?,
            last_error = ?,
            last_error_at = ?,
            last_duration_ms = ?,
            updated_at = ?
        WHERE market_hash_name = ?
    """, (
        attempt_count,
        next_fetch_at,
        now,
        consecutive_errors,
        outcome,
        http_status,
        error_text,
        now,
        duration_ms,
        iso_now(),
        market_hash_name,
    ))
    save_request_log(
        conn,
        market_hash_name,
        sales_count,
        0,
        retry_seconds,
        "error_backoff",
        next_fetch_at,
        outcome=outcome,
        http_status=http_status,
        duration_ms=duration_ms,
        rejected_sales_count=rejected_sales_count,
        page_count=page_count,
        coverage_status="error",
        error=error_text,
    )
    return retry_seconds, next_fetch_at


def get_progress(conn):
    row = conn.execute("""
        SELECT skin_index
        FROM progress
        WHERE id = ?
    """, (PROGRESS_ID,)).fetchone()

    return row[0] if row else 0


def save_progress(conn, skin_index, market_hash_name, *, commit=True):
    conn.execute("""
        INSERT INTO progress (
            id,
            skin_index,
            market_hash_name,
            updated_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            skin_index = excluded.skin_index,
            market_hash_name = excluded.market_hash_name,
            updated_at = excluded.updated_at
    """, (
        PROGRESS_ID,
        skin_index,
        market_hash_name,
        datetime.now(timezone.utc).isoformat(),
    ))
    if commit:
        conn.commit()


def reset_progress(conn):
    conn.execute("""
        INSERT INTO progress (
            id,
            skin_index,
            market_hash_name,
            updated_at
        )
        VALUES (?, 0, NULL, ?)
        ON CONFLICT(id) DO UPDATE SET
            skin_index = 0,
            market_hash_name = NULL,
            updated_at = excluded.updated_at
    """, (
        PROGRESS_ID,
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()


def text_value(value):
    if isinstance(value, (dict, list)):
        return dumps_json(value)

    return value


def upsert_sparse_row(conn, table_name, primary_key_columns, row):
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    update_columns = [column for column in columns if column not in primary_key_columns]
    update_sql = ", ".join(
        f"{column} = COALESCE({table_name}.{column}, excluded.{column})"
        for column in update_columns
    )
    conn.execute(f"""
        INSERT INTO {table_name} ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT({", ".join(primary_key_columns)}) DO UPDATE SET
            {update_sql}
    """, [row[column] for column in columns])


def canonical_sale_hash(sale):
    immutable = dict(sale)
    for key in ("reference", "watchers", "is_watchlisted", "is_seller"):
        immutable.pop(key, None)
    payload = json.dumps(
        immutable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_sale_payload(sale):
    if not isinstance(sale, dict):
        raise TypeError("sale must be an object")
    listing_id = sale.get("id")
    if listing_id in (None, ""):
        raise ValueError("sale id is missing")
    if isinstance(listing_id, bool) or not isinstance(listing_id, (str, int)):
        raise TypeError("sale id must be a string or integer")
    item = sale.get("item")
    if not isinstance(item, dict):
        raise TypeError("sale item is missing")
    name = item.get("market_hash_name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("item market_hash_name is missing")
    try:
        price_cents = int(sale.get("price"))
    except (TypeError, ValueError) as exc:
        raise ValueError("sale price is invalid") from exc
    if price_cents <= 0:
        raise ValueError("sale price must be positive")
    if parse_csfloat_time(sale.get("sold_at")) is None:
        raise ValueError("sale sold_at is invalid")
    float_value = item.get("float_value")
    if float_value not in (None, ""):
        try:
            numeric_float = float(float_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("item float_value is invalid") from exc
        if not math.isfinite(numeric_float) or not 0 <= numeric_float <= 1:
            raise ValueError("item float_value is outside [0, 1]")
    return str(listing_id)


def quarantine_payload(conn, market_hash_name, sale, reason):
    listing_id = sale.get("id") if isinstance(sale, dict) else None
    try:
        payload_json = dumps_json(sale)
    except (TypeError, ValueError):
        payload_json = repr(sale)
    conn.execute("""
        INSERT INTO parser_quarantine (
            quarantined_at, market_hash_name, listing_id, reason, payload_json
        ) VALUES (?, ?, ?, ?, ?)
    """, (
        iso_now(),
        market_hash_name,
        str(listing_id) if listing_id not in (None, "") else None,
        str(reason)[:1000],
        payload_json[:100000],
    ))


def save_sale(
    conn,
    sale,
    base_skin_name=None,
    wear_name=None,
    *,
    existed=None,
    observed_at=None,
    previous_payload_hash=None,
):
    listing_id = validate_sale_payload(sale)
    item = sale["item"]
    reference_value = sale.get("reference")
    reference = reference_value if isinstance(reference_value, dict) else {}
    observed_at = observed_at or iso_now()
    actual_name = item.get("market_hash_name")
    base_skin_name = extract_base_skin_name(actual_name) or base_skin_name
    wear_name = extract_wear_name(actual_name) or wear_name
    payload_hash = canonical_sale_hash(sale)

    if existed is None:
        existing = conn.execute(
            "SELECT payload_sha256, last_payload_sha256 FROM sales WHERE listing_id = ?",
            (listing_id,),
        ).fetchone()
        existed = existing is not None
        if existing and previous_payload_hash is None:
            previous_payload_hash = existing[1] or existing[0]

    sale_row = {
        "listing_id": listing_id,
        "name": actual_name,
        "base_skin_name": base_skin_name,
        "wear_name": wear_name,
        "price_cents": sale.get("price"),
        "reference_base_price": reference.get("base_price"),
        "reference_predicted_price": reference.get("predicted_price"),
        "reference_float_factor": reference.get("float_factor"),
        "reference_quantity": reference.get("quantity"),
        "float_value": item.get("float_value"),
        "paint_seed": item.get("paint_seed"),
        "paint_index": item.get("paint_index"),
        "inspect_link": item.get("inspect_link"),
        "serialized_inspect": item.get("serialized_inspect"),
        "state": sale.get("state"),
        "created_at": sale.get("created_at"),
        "sold_at": sale.get("sold_at"),
        "fetched_at": observed_at,
        "sale_type": sale.get("type"),
        "is_seller": bool_to_int(sale.get("is_seller")),
        "is_watchlisted": bool_to_int(sale.get("is_watchlisted")),
        "watchers": sale.get("watchers"),
        "item_asset_id": item.get("asset_id"),
        "item_icon_url": normalize_icon_url(item.get("icon_url")),
        "item_name": item.get("item_name"),
        "item_description": item.get("description"),
        "item_collection": text_value(item.get("collection")),
        "item_type": text_value(item.get("type")),
        "item_type_name": item.get("type_name"),
        "item_quality": text_value(item.get("quality")),
        "item_rarity": item.get("rarity"),
        "item_rarity_name": item.get("rarity_name"),
        "item_def_index": item.get("def_index"),
        "item_wear_name": item.get("wear_name"),
        "item_tradable": bool_to_int(item.get("tradable")),
        "item_is_commodity": bool_to_int(item.get("is_commodity")),
        "item_is_souvenir": bool_to_int(item.get("is_souvenir")),
        "item_is_stattrak": bool_to_int(item.get("is_stattrak")),
        "item_cs2_screenshot_id": item.get("cs2_screenshot_id"),
        "item_cs2_screenshot_at": item.get("cs2_screenshot_at"),
        "item_gs_sig": item.get("gs_sig"),
        "raw_json": dumps_json(sale),
        "item_raw_json": dumps_json(item),
        "reference_raw_json": dumps_json(reference) if reference_value is not None else None,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "seen_count": 1,
        "payload_sha256": payload_hash,
        "last_payload_sha256": payload_hash,
        "reference_keychain_price": reference.get("keychain_price"),
        "reference_last_updated": reference.get("last_updated"),
    }

    if not existed:
        columns = list(sale_row)
        cursor = conn.execute(f"""
            INSERT INTO sales ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            ON CONFLICT(listing_id) DO NOTHING
        """, [sale_row[column] for column in columns])
        existed = cursor.rowcount == 0

    if existed:
        conn.execute("""
            UPDATE sales
            SET
                first_seen_at = COALESCE(first_seen_at, fetched_at, ?),
                last_seen_at = ?,
                seen_count = COALESCE(seen_count, 1) + 1,
                last_payload_sha256 = ?
            WHERE listing_id = ?
        """, (
            observed_at,
            observed_at,
            payload_hash,
            listing_id,
        ))
        return "updated" if previous_payload_hash not in (None, payload_hash) else "seen"

    stickers = item.get("stickers")
    if stickers is not None and not isinstance(stickers, list):
        raise ValueError("item stickers must be an array when present")
    for sticker_index, sticker in enumerate(stickers or []):
        if not isinstance(sticker, dict):
            quarantine_payload(
                conn,
                actual_name,
                sale,
                f"sticker {sticker_index} is not an object",
            )
            continue
        sticker_reference = sticker.get("reference") or sticker.get("scm") or {}
        if not isinstance(sticker_reference, dict):
            sticker_reference = {}

        sticker_row = {
            "listing_id": listing_id,
            "sticker_index": sticker_index,
            "sticker_id": sticker.get("stickerId"),
            "slot": sticker.get("slot"),
            "name": sticker.get("name"),
            "wear": sticker.get("wear"),
            "price_cents": sticker_reference.get("price"),
            "icon_url": normalize_icon_url(sticker.get("icon_url")),
            "offset_x": sticker.get("offset_x"),
            "offset_y": sticker.get("offset_y"),
            "reference_price_cents": sticker_reference.get("price"),
            "reference_quantity": sticker_reference.get("quantity"),
            "raw_json": dumps_json(sticker),
            "reference_raw_json": dumps_json(sticker_reference),
            "rotation": sticker.get("rotation"),
            "reference_updated_at": sticker_reference.get("updated_at"),
            "steam_market_url": sticker.get("steam_market_url"),
        }

        upsert_sparse_row(conn, "sale_stickers", ["listing_id", "sticker_index"], sticker_row)

    keychains = item.get("keychains")
    if keychains is not None and not isinstance(keychains, list):
        raise ValueError("item keychains must be an array when present")
    for keychain_index, keychain in enumerate(keychains or []):
        if not isinstance(keychain, dict):
            quarantine_payload(
                conn,
                actual_name,
                sale,
                f"keychain {keychain_index} is not an object",
            )
            continue
        keychain_reference = keychain.get("reference") or keychain.get("scm") or {}
        if not isinstance(keychain_reference, dict):
            keychain_reference = {}
        keychain_row = {
            "listing_id": listing_id,
            "keychain_index": keychain_index,
            "keychain_id": keychain.get("stickerId") or keychain.get("keychain_id") or keychain.get("id"),
            "slot": keychain.get("slot"),
            "name": keychain.get("name"),
            "pattern": keychain.get("pattern"),
            "offset_x": keychain.get("offset_x"),
            "offset_y": keychain.get("offset_y"),
            "offset_z": keychain.get("offset_z"),
            "price_cents": keychain_reference.get("price"),
            "reference_quantity": keychain_reference.get("quantity"),
            "reference_updated_at": keychain_reference.get("updated_at"),
            "icon_url": normalize_icon_url(keychain.get("icon_url")),
            "steam_market_url": keychain.get("steam_market_url"),
            "raw_json": dumps_json(keychain),
            "reference_raw_json": dumps_json(keychain_reference),
        }
        upsert_sparse_row(conn, "sale_keychains", ["listing_id", "keychain_index"], keychain_row)

    return "new"


def load_existing_sale_hashes(conn, listing_ids):
    clean_ids = [str(value) for value in dict.fromkeys(listing_ids) if value not in (None, "")]
    if not clean_ids:
        return {}
    placeholders = ",".join("?" for _ in clean_ids)
    return {
        str(row[0]): row[2] or row[1]
        for row in conn.execute(f"""
            SELECT listing_id, payload_sha256, last_payload_sha256
            FROM sales
            WHERE listing_id IN ({placeholders})
        """, clean_ids)
    }


def save_sales_batch(conn, sales, market_hash_name, base_skin_name=None, wear_name=None, observed_at=None):
    observed_at = observed_at or iso_now()
    stats = IngestStats(returned_count=len(sales))
    unique_sales = []
    seen_response_ids = set()

    for sale in sales:
        listing_id = sale.get("id") if isinstance(sale, dict) else None
        key = str(listing_id) if listing_id not in (None, "") else None
        if key and key in seen_response_ids:
            stats.duplicate_response_count += 1
            continue
        if key:
            seen_response_ids.add(key)
        unique_sales.append(sale)

    existing_hashes = load_existing_sale_hashes(conn, seen_response_ids)
    for index, sale in enumerate(unique_sales):
        savepoint = f"parser_sale_{index}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            listing_id = validate_sale_payload(sale)
            actual_name = sale["item"]["market_hash_name"]
            if market_hash_name and actual_name != market_hash_name:
                raise ValueError(
                    f"sale skin mismatch: expected {market_hash_name!r}, got {actual_name!r}"
                )
            existed = listing_id in existing_hashes
            status = save_sale(
                conn,
                sale,
                base_skin_name,
                wear_name,
                existed=existed,
                observed_at=observed_at,
                previous_payload_hash=existing_hashes.get(listing_id),
            )
            if status == "new":
                stats.new_count += 1
                stats.new_sales.append(sale)
            else:
                stats.overlap_count += 1
                if status == "updated":
                    stats.updated_count += 1
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except (TypeError, ValueError, OverflowError) as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            quarantine_payload(conn, market_hash_name, sale, exc)
            stats.rejected_count += 1

    return stats


def run_scheduler(
    conn,
    skins,
    *,
    max_requests=None,
    schedule_ready=False,
    catalog_refresh_callback=None,
):
    request_count = 0
    sales_count = 0
    next_catalog_check_at = 0.0
    if not schedule_ready:
        ensure_skin_schedule(conn, skins)

    while True:
        if STOP_REQUESTED:
            raise ParserStopRequested()

        if catalog_refresh_callback is not None and time.monotonic() >= next_catalog_check_at:
            next_catalog_check_at = time.monotonic() + CATALOG_BACKGROUND_CHECK_SECONDS
            try:
                refreshed_skins = catalog_refresh_callback(conn, skins)
                if refreshed_skins is not None:
                    skins = refreshed_skins
            except ParserStopRequested:
                raise
            except Exception as exc:  # noqa: BLE001 - preserve the last good schedule
                conn.rollback()
                print(f"Background item catalog refresh warning: {exc}", flush=True)

        row = get_next_scheduled_skin(conn)
        if not row:
            if max_requests is not None:
                return request_count
            sleep_with_message(60, "No scheduled skins found")
            continue

        market_hash_name = row[0]
        skin_index = int(row[1] or 0)
        base_skin_name = row[2] or extract_base_skin_name(market_hash_name)
        wear_name = row[3] or extract_wear_name(market_hash_name)
        next_fetch_at = int(row[9] or 0)
        paint_index = row[10]

        now = unix_now()
        if next_fetch_at > now:
            if max_requests is not None:
                print(
                    f"No skin is due; next is {market_hash_name} at "
                    f"{unix_to_msk_text(next_fetch_at)} MSK",
                    flush=True,
                )
                return request_count
            update_parser_state(
                total_skins=len(skins),
                skin_index=skin_index,
                current_skin=f"waiting: {market_hash_name}",
            )
            sleep_with_message(
                min(
                    next_fetch_at - now,
                    CATALOG_BACKGROUND_CHECK_SECONDS
                    if catalog_refresh_callback is not None
                    else next_fetch_at - now,
                ),
                f"Next scheduled skin is not due yet: {market_hash_name}",
            )
            continue

        update_parser_state(
            total_skins=len(skins),
            skin_index=skin_index + 1,
            current_skin=market_hash_name,
            last_error=None,
        )
        print(
            f"[dynamic {request_count + 1}] "
            f"[skin {skin_index + 1}/{len(skins)}] "
            f"Requesting: {market_hash_name}",
            flush=True,
        )

        try:
            window = fetch_sales_window(
                conn,
                market_hash_name,
                paint_index=paint_index,
            )
        except ParserStopRequested:
            raise
        except SalesFetchError as exc:
            conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            try:
                interval_seconds, next_fetch_at = record_skin_failure(
                    conn,
                    market_hash_name,
                    exc,
                )
                save_progress(conn, skin_index + 1, market_hash_name, commit=False)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            request_count += 1
            update_parser_state(
                request_count_cycle=request_count,
                sales_count_cycle=sales_count,
                last_saved_skin=market_hash_name,
                last_sales_count=0,
                last_new_sales_count=0,
                last_interval_seconds=interval_seconds,
                last_next_fetch_at=unix_to_iso(next_fetch_at),
                last_outcome=exc.outcome,
                last_error=str(exc)[:1000],
            )
            print(
                f"Request failed for {market_hash_name}: {exc}. "
                f"Retry at {unix_to_msk_text(next_fetch_at)} MSK",
                flush=True,
            )
            if max_requests is not None and request_count >= max_requests:
                return request_count
            sleep_with_message(REQUEST_DELAY_SECONDS, "Request delay")
            continue

        returned_count = sum(window.page_sizes)
        outcome = "not_found" if window.http_status == 404 else "success"
        stats = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            stats = save_sales_batch(
                conn,
                window.sales,
                market_hash_name,
                base_skin_name,
                wear_name,
                observed_at=iso_now(),
            )
            if stats.returned_count and stats.rejected_count == stats.returned_count:
                error = SalesFetchError(
                    f"Every returned sale was rejected for {market_hash_name}",
                    outcome="invalid_payload",
                    http_status=window.http_status,
                    duration_ms=window.duration_ms,
                )
                interval_seconds, next_fetch_at = record_skin_failure(
                    conn,
                    market_hash_name,
                    error,
                    sales_count=returned_count,
                    rejected_sales_count=stats.rejected_count,
                    page_count=window.page_count,
                )
                coverage_status = "invalid_payload"
                avg_sales_count = float(row[5] or 0)
                avg_new_sales_count = float(row[6] or 0)
                interval_reason = "error_backoff"
                outcome = error.outcome
            else:
                coverage_overlap_count = max(
                    stats.overlap_count,
                    len(window.overlap_ids),
                )
                coverage_status, page_saturated = classify_sales_window(
                    window,
                    coverage_overlap_count,
                )
                (
                    interval_seconds,
                    next_fetch_at,
                    avg_sales_count,
                    avg_new_sales_count,
                    interval_reason,
                ) = update_skin_schedule(
                    conn,
                    market_hash_name,
                    stats.returned_count,
                    stats.new_count,
                    latest_sold_at(window.sales),
                    sales_span_seconds=sales_time_span_seconds(window.sales),
                    new_sales_span_seconds=sales_time_span_seconds(stats.new_sales),
                    overlap_count=coverage_overlap_count,
                    page_saturated=page_saturated,
                    coverage_status=coverage_status,
                    outcome=outcome,
                    http_status=window.http_status,
                    duration_ms=window.duration_ms,
                )
                save_request_log(
                    conn,
                    market_hash_name,
                    returned_count,
                    stats.new_count,
                    interval_seconds,
                    interval_reason,
                    next_fetch_at,
                    outcome=outcome,
                    http_status=window.http_status,
                    duration_ms=window.duration_ms,
                    updated_sales_count=stats.updated_count,
                    overlap_count=coverage_overlap_count,
                    rejected_sales_count=stats.rejected_count,
                    page_count=window.page_count,
                    page_saturated=page_saturated,
                    coverage_status=coverage_status,
                )
            save_progress(conn, skin_index + 1, market_hash_name, commit=False)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - isolate one scheduler cycle in a daemon
            conn.rollback()
            db_error = SalesFetchError(
                f"Database write failed for {market_hash_name}: {exc}",
                outcome="db_error",
                http_status=window.http_status,
                duration_ms=window.duration_ms,
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                interval_seconds, next_fetch_at = record_skin_failure(
                    conn,
                    market_hash_name,
                    db_error,
                    sales_count=returned_count,
                    page_count=window.page_count,
                )
                save_progress(conn, skin_index + 1, market_hash_name, commit=False)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            coverage_status = "error"
            interval_reason = "error_backoff"
            avg_sales_count = float(row[5] or 0)
            avg_new_sales_count = float(row[6] or 0)
            outcome = db_error.outcome
            stats = IngestStats(returned_count=returned_count)

        request_count += 1
        sales_count += returned_count
        page_saturated = coverage_status in {"coverage_uncertain", "cursor_limit"}
        update_parser_state(
            request_count_cycle=request_count,
            sales_count_cycle=sales_count,
            last_saved_skin=market_hash_name,
            last_sales_count=returned_count,
            last_new_sales_count=stats.new_count,
            last_interval_seconds=interval_seconds,
            last_next_fetch_at=unix_to_iso(next_fetch_at),
            last_outcome=outcome,
            last_page_saturated=page_saturated,
            last_coverage_status=coverage_status,
            last_error=None if outcome in {"success", "not_found"} else outcome,
        )
        print(
            f"Ingested {returned_count} sales for {market_hash_name}: "
            f"new={stats.new_count}, overlap={stats.overlap_count}, "
            f"changed={stats.updated_count}, rejected={stats.rejected_count}, "
            f"coverage={coverage_status}. "
            f"EMA sales={avg_sales_count:.2f}, EMA new={avg_new_sales_count:.2f}; "
            f"next fetch {unix_to_msk_text(next_fetch_at)} MSK",
            flush=True,
        )

        if max_requests is not None and request_count >= max_requests:
            return request_count
        sleep_with_message(REQUEST_DELAY_SECONDS, "Request delay")


@contextmanager
def parser_instance_lock():
    lock_path = Path(LOCK_PATH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            try:
                handle.seek(0)
                owner = handle.read().strip() or "unknown"
            except OSError:
                # Windows byte-range locks also block reads of the locked byte.
                owner = "unknown"
            raise RuntimeError(
                f"parser_final is already running (lock owner: {owner})"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def request_parser_stop(signum, _frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print(f"Stop requested by signal {signum}; finishing safely...", flush=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Adaptive CSFloat sales-history parser",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="process at most one due skin and exit",
    )
    mode.add_argument(
        "--migrate-only",
        action="store_true",
        help="apply database schema/scheduler migrations and exit without API calls",
    )
    mode.add_argument(
        "--refresh-catalog-only",
        action="store_true",
        help=(
            "refresh the bounded local item catalog, merge its parser schedule, "
            "and exit without CSFloat history requests"
        ),
    )
    parser.add_argument(
        "--backfill-derived",
        action="store_true",
        help="backfill compact keychain and sticker metadata from existing JSON",
    )
    parser.add_argument(
        "--force-backfill",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def refresh_item_catalog(conn, *, force=False):
    return cs2_item_catalog.refresh_catalog_if_due(
        conn,
        cache_dir=CATALOG_CACHE_DIR,
        primary_url=CATALOG_PRIMARY_URL,
        enrichment_enabled=CATALOG_ENRICHMENT_ENABLED,
        enrichment_url=CATALOG_ENRICHMENT_URL,
        fallback_enabled=CATALOG_FALLBACK_ENABLED,
        refresh_interval_seconds=CATALOG_REFRESH_INTERVAL_SECONDS,
        timeout_seconds=CATALOG_TIMEOUT_SECONDS,
        total_timeout_seconds=CATALOG_TOTAL_TIMEOUT_SECONDS,
        force=force,
    )


def refresh_item_catalog_schedule(conn, skins):
    """Periodically publish new catalog names into the live parser schedule."""

    result = refresh_item_catalog(conn)
    if result.get("status") != "updated":
        if result.get("error"):
            print(f"Item catalog refresh warning: {result['error']}", flush=True)
        return skins

    catalog_skins = cs2_item_catalog.load_schedule_items(conn)
    merged = cs2_item_catalog.merge_schedule_items(skins, catalog_skins)
    ensure_skin_schedule(conn, merged)
    update_parser_state(total_skins=len(merged))
    print(
        "Item catalog background update: "
        f"rows={result.get('row_count', 0)}, scheduled_names={len(merged)}",
        flush=True,
    )
    return merged


def main(argv=None):
    global STOP_REQUESTED
    args = parse_args(argv)
    STOP_REQUESTED = False
    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)
    stop_reason = "finished"
    status_owned = False

    try:
        with parser_instance_lock():
            status_owned = True
            signal.signal(signal.SIGINT, request_parser_stop)
            signal.signal(signal.SIGTERM, request_parser_stop)
            import windows_service_control
            windows_service_control.register_stop_callback(
                lambda: request_parser_stop(signal.SIGINT, None)
            )
            started_at = iso_now()
            update_parser_state(
                status_version=PARSER_STATUS_VERSION,
                running=True,
                started_at=started_at,
                stopped_at=None,
                total_skins=0,
                current_skin=None,
                last_error=None,
                sleeping_until=None,
                sleep_reason=None,
            )

            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA foreign_keys=ON")
                run_schema_migrations(conn)
                cs2_item_catalog.ensure_catalog_schema(conn)
                if (
                    not args.migrate_only
                    and (CATALOG_REFRESH_ENABLED or args.refresh_catalog_only)
                ):
                    catalog_refresh = refresh_item_catalog(
                        conn,
                        force=args.refresh_catalog_only,
                    )
                    print(
                        "Item catalog refresh: "
                        f"status={catalog_refresh['status']}, "
                        f"rows={catalog_refresh.get('row_count', 0)}",
                        flush=True,
                    )
                    if catalog_refresh.get("error"):
                        print(
                            f"Item catalog refresh warning: {catalog_refresh['error']}",
                            flush=True,
                        )

                base_skins = load_skins()
                catalog_skins = cs2_item_catalog.load_schedule_items(conn)
                skins = cs2_item_catalog.merge_schedule_items(base_skins, catalog_skins)
                print(
                    f"Loaded {len(base_skins)} skins from {SKINS_PATH}; "
                    f"merged {len(catalog_skins)} catalog targets into "
                    f"{len(skins)} scheduled names",
                    flush=True,
                )
                update_parser_state(total_skins=len(skins))
                ensure_skin_schedule(conn, skins)
                recovered = migrate_legacy_schedule_v2(conn)
                if recovered:
                    print(
                        f"Scheduled {recovered} legacy capped skins for parser-v2 recovery.",
                        flush=True,
                    )

                if args.backfill_derived:
                    derived_result = backfill_derived_metadata(
                        conn,
                        force=args.force_backfill,
                    )
                    sale_result = backfill_sale_metadata(
                        conn,
                        force=args.force_backfill,
                    )
                    print(
                        "Derived metadata backfill: "
                        f"keychains={derived_result['keychains']}, "
                        f"stickers={derived_result['stickers']}, "
                        f"already_done={derived_result['already_done']}",
                        flush=True,
                    )
                    print(
                        "Sale metadata backfill: "
                        f"rows={sale_result['rows_updated']}, "
                        f"first_seen={sale_result['first_seen']}, "
                        f"last_seen={sale_result['last_seen']}, "
                        f"reference_updated={sale_result['reference_updated']}, "
                        f"keychain_price={sale_result['keychain_price']}, "
                        f"already_done={sale_result['already_done']}",
                        flush=True,
                    )

                if args.migrate_only:
                    stop_reason = "migration_complete"
                    print("Parser database migrations complete; no API calls made.", flush=True)
                    return 0

                if args.refresh_catalog_only:
                    stop_reason = "catalog_refresh_complete"
                    print(
                        "Item catalog refresh and schedule merge complete; "
                        "no CSFloat history requests made.",
                        flush=True,
                    )
                    return 0

                initialize_api_keys()
                print_loaded_key_routes()
                print(f"Starting adaptive scheduler at {started_at}", flush=True)
                update_parser_state(
                    cycle_started_at=started_at,
                    scheduler_mode="adaptive_overlap_v2",
                    request_count_cycle=0,
                    sales_count_cycle=0,
                )
                run_scheduler(
                    conn,
                    skins,
                    max_requests=1 if args.once else None,
                    schedule_ready=True,
                    catalog_refresh_callback=(
                        refresh_item_catalog_schedule
                        if CATALOG_REFRESH_ENABLED and not args.once
                        else None
                    ),
                )
                stop_reason = "once_complete" if args.once else "finished"
                return 0
    except (KeyboardInterrupt, ParserStopRequested):
        stop_reason = "stopped_by_user"
        print("Stopped safely. Progress is saved.", flush=True)
        return 0
    except Exception as exc:
        stop_reason = "error"
        if status_owned:
            update_parser_state(last_error=str(exc)[:1000])
        raise
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        if status_owned:
            update_parser_state(
                running=False,
                stopped_at=iso_now(),
                current_skin=None,
                sleeping_until=None,
                sleep_reason=stop_reason,
            )


if __name__ == "__main__":
    main()
