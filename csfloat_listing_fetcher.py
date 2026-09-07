#!/usr/bin/env python3

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import app_config

KEY_PATHS = [app_config.PROJECT_DIR / name for name in ("key4", "key2", "key", "key3")]
KEY_PROXY_CONFIG_PATH = app_config.PROJECT_DIR / "api_keys_proxies.txt"
LISTING_API_BASE = "https://csfloat.com/api/v1/listings"
PRICE_LIST_API_URL = f"{LISTING_API_BASE}/price-list"
USER_AGENT = "Mozilla/5.0"
STEAM_IMAGE_BASE_URL = "https://community.akamai.steamstatic.com/economy/image/"
CSFLOAT_SCREENSHOT_BASE_URL = "https://s.csfloat.com/m"
MAX_ACTIVE_LISTING_LIMIT = 50
CHEAP_SKIN_MAX_PRICE_CENTS = 1000
PRICE_LIST_CACHE_TTL_SECONDS = 300
PRICE_LIST_SOURCE = "csfloat_price_list"
ACTIVE_BUY_NOW_SOURCE = "csfloat_active_buy_now"
ALLOWED_ACTIVE_LISTING_SORTS = frozenset(
    {"most_recent", "lowest_price", "highest_discount"}
)
ALLOWED_LISTING_TYPES = frozenset({"buy_now", "auction"})

_PRICE_LIST_CACHE_LOCK = threading.Lock()
_PRICE_LIST_CACHE = None


class CsfloatFetchError(RuntimeError):
    pass


class CsfloatRateLimitError(CsfloatFetchError):
    """All usable CSFloat routes are currently rate limited."""


class CsfloatNotFoundError(CsfloatFetchError):
    """The requested CSFloat listing no longer exists in the live API."""


class CsfloatRouteConfigError(ValueError):
    """Raised for an invalid route file without exposing key contents."""


def mask_proxy(proxy_url):
    if not proxy_url:
        return "direct"
    try:
        parsed = urllib.parse.urlsplit(proxy_url)
        if parsed.hostname:
            host = parsed.hostname
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return (
                f"{parsed.scheme}://***@{host}"
                if parsed.username
                else f"{parsed.scheme}://{host}"
            )
    except ValueError:
        return "invalid proxy"
    return "proxy"


def normalize_icon_url(icon_url):
    if not icon_url:
        return None
    if str(icon_url).startswith(("http://", "https://")):
        return icon_url
    return STEAM_IMAGE_BASE_URL + str(icon_url)


def build_csfloat_screenshot_url(
    screenshot_id=None, listing_id=None, has_screenshot=False
):
    if screenshot_id:
        return f"{CSFLOAT_SCREENSHOT_BASE_URL}/{screenshot_id}/playside.png?v=3"
    if has_screenshot and listing_id:
        return f"{CSFLOAT_SCREENSHOT_BASE_URL}/{listing_id}/playside.png?v=3"
    return None


def is_socks_proxy(proxy_url):
    if not proxy_url:
        return False
    lowered = proxy_url.lower()
    return lowered.startswith(("socks4://", "socks5://", "socks5h://"))


def _route_record(path_label, value, proxy_url=None):
    return {
        "path": str(path_label),
        "value": value,
        "proxy": proxy_url,
        "proxy_label": mask_proxy(proxy_url),
    }


def _deduplicate_routes(records):
    result = []
    seen = set()
    for record in records:
        identity = (record["value"], record.get("proxy"))
        if identity in seen:
            continue
        seen.add(identity)
        result.append(record)
    return result


def _route_config_path():
    configured_path = app_config.get("CSFLOAT_ROUTES_PATH")
    if configured_path:
        return app_config.path("CSFLOAT_ROUTES_PATH", KEY_PROXY_CONFIG_PATH)
    return Path(KEY_PROXY_CONFIG_PATH)


def _route_config_error(config_path, line_number, message):
    return CsfloatRouteConfigError(f"{config_path}:{line_number}: {message}")


def _load_routes_file(config_path, *, strict):
    records = []
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        if strict:
            raise CsfloatRouteConfigError(
                f"Could not read CSFloat routes file: {config_path}"
            ) from exc
        return records

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.count("|") > 1:
            if strict:
                raise _route_config_error(
                    config_path, line_number, "expected KEY_FILE or KEY_FILE|PROXY_URL"
                )
            continue

        key_path, separator, proxy_url = line.partition("|")
        key_path = key_path.strip()
        proxy_url = proxy_url.strip() if separator else ""
        if not key_path:
            if strict:
                raise _route_config_error(
                    config_path, line_number, "key file path is empty"
                )
            continue

        path = Path(key_path)
        if not path.is_absolute():
            path = app_config.PROJECT_DIR / path
        if not path.is_file():
            if strict:
                raise _route_config_error(
                    config_path, line_number, f"key file not found: {path.name}"
                )
            continue

        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            if strict:
                raise _route_config_error(
                    config_path, line_number, f"could not read key file: {path.name}"
                ) from exc
            continue
        if not value:
            if strict:
                raise _route_config_error(
                    config_path, line_number, f"key file is empty: {path.name}"
                )
            continue

        records.append(_route_record(path.name, value, proxy_url or None))

    return _deduplicate_routes(records)


def load_key_records(*, strict=False):
    """Load CSFloat routes using routes-file, environment, then legacy fallback.

    ``strict=True`` is intended for long-running services and startup
    preflight: malformed configured route entries fail fast.  Error messages
    contain file labels only and never include API-key contents.
    """

    config_path = _route_config_path()
    if config_path.exists():
        records = _load_routes_file(config_path, strict=strict)
        if records:
            return records
    elif strict and app_config.get("CSFLOAT_ROUTES_PATH"):
        raise CsfloatRouteConfigError(f"CSFloat routes file not found: {config_path}")

    environment_key = app_config.get("CSFLOAT_API_KEY")
    if environment_key:
        return [_route_record("CSFLOAT_API_KEY", environment_key)]

    records = []
    for key_path in KEY_PATHS:
        path = Path(key_path)
        if not path.is_file():
            continue
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            if strict:
                raise CsfloatRouteConfigError(
                    f"Could not read legacy CSFloat key file: {path.name}"
                ) from exc
            continue
        if value:
            records.append(_route_record(path.name, value))
        elif strict:
            raise CsfloatRouteConfigError(
                f"Legacy CSFloat key file is empty: {path.name}"
            )

    records = _deduplicate_routes(records)
    if not records:
        raise FileNotFoundError(
            "No CSFloat API routes found. Configure CSFLOAT_ROUTES_PATH, "
            "CSFLOAT_API_KEY, or a legacy key file."
        )

    return records


def parse_listing_url_or_id(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("CSFloat link or listing id is required.")

    if text.startswith(("https://", "http://")):
        direct = urllib.parse.urlparse(text)
        if (
            direct.scheme == "https"
            and direct.hostname == "csfloat.com"
            and direct.path.startswith("/api/")
        ):
            return text
        if direct.path.startswith("/api/"):
            raise ValueError(
                "Only HTTPS requests to https://csfloat.com/api/ are allowed."
            )

    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", text):
        return f"{LISTING_API_BASE}/{urllib.parse.quote(text, safe='')}"

    parsed = urllib.parse.urlparse(text)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("listing_id", "listingId", "id"):
        value = query.get(key, [""])[0]
        if value:
            return f"{LISTING_API_BASE}/{urllib.parse.quote(value, safe='')}"

    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    for marker in ("item", "listing", "listings"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts):
                return f"{LISTING_API_BASE}/{urllib.parse.quote(parts[index + 1], safe='')}"

    ids = re.findall(r"\b\d{8,}\b", text)
    if ids:
        return f"{LISTING_API_BASE}/{ids[-1]}"

    raise ValueError("Could not find listing id in CSFloat link.")


def request_with_key(url, key_record):
    headers = {
        "Authorization": key_record["value"],
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    proxy_url = key_record.get("proxy")
    if is_socks_proxy(proxy_url):
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError(
                "SOCKS proxy requires: py -m pip install requests[socks]"
            ) from exc
        response = requests.get(
            url,
            headers=headers,
            timeout=30,
            proxies={"http": proxy_url, "https": proxy_url},
        )
        return response.status_code, dict(response.headers), response.text

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(
            {"http": proxy_url, "https": proxy_url} if proxy_url else {}
        )
    )
    request = urllib.request.Request(url, headers=headers)

    try:
        with opener.open(request, timeout=30) as response:
            return (
                response.status,
                dict(response.headers),
                response.read().decode("utf-8", errors="replace"),
            )
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", errors="replace")


def request_json_with_keys(url):
    errors = []

    for key_record in load_key_records():
        try:
            status, headers, body = request_with_key(url, key_record)
        except Exception as exc:  # noqa: BLE001 - isolate independent route/client failures
            errors.append(
                {
                    "key_file": key_record["path"],
                    "proxy": key_record["proxy_label"],
                    "error": str(exc),
                }
            )
            continue

        if status == 200:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise CsfloatFetchError(
                    f"CSFloat returned non-JSON body: {exc}"
                ) from exc

            return payload, {
                "key_file": key_record["path"],
                "proxy": key_record["proxy_label"],
                "rate_limit": header_value(headers, "X-Ratelimit-Limit"),
                "rate_remaining": header_value(headers, "X-Ratelimit-Remaining"),
                "rate_reset": header_value(headers, "X-Ratelimit-Reset"),
                "http_status": status,
            }

        errors.append(
            {
                "key_file": key_record["path"],
                "proxy": key_record["proxy_label"],
                "http_status": status,
                "rate_limit": header_value(headers, "X-Ratelimit-Limit"),
                "rate_remaining": header_value(headers, "X-Ratelimit-Remaining"),
                "rate_reset": header_value(headers, "X-Ratelimit-Reset"),
                "body": body[:500],
            }
        )

        if status == 404 or status not in (401, 403, 429):
            break

    if any(attempt.get("http_status") == 404 for attempt in errors):
        error_type = CsfloatNotFoundError
    elif any(attempt.get("http_status") == 429 for attempt in errors):
        error_type = CsfloatRateLimitError
    else:
        error_type = CsfloatFetchError
    raise error_type(
        json.dumps({"error": "all keys failed", "attempts": errors}, ensure_ascii=False)
    )


def header_value(headers, name):
    expected = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == expected:
            return value
    return None


def unwrap_listing(payload):
    current = payload
    for key in ("data", "listing"):
        if isinstance(current, dict) and isinstance(current.get(key), dict):
            current = current[key]
    return current


def get_nested(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first_present(*values):
    """Return the first value that is not None/empty while preserving zero."""
    for value in values:
        if value is not None and value != "":
            return value
    return None


def normalize_listing_type(listing):
    if not isinstance(listing, dict):
        return "unknown"

    for key in ("is_auction", "auction"):
        value = listing.get(key)
        if isinstance(value, bool):
            return "auction" if value else "buy_now"
        if isinstance(value, dict):
            return "auction"

    auction_markers = (
        "auction_ends_at",
        "auction_end_at",
        "auction_end_time",
        "auction_details",
        "highest_bid",
        "reserve_price",
        "minimum_bid",
    )
    if any(listing.get(key) not in (None, "", False) for key in auction_markers):
        return "auction"

    for key in ("sale_type", "listing_type", "type", "kind"):
        value = listing.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        if "auction" in text or text in {"bid", "bidding"}:
            return "auction"
        if text in {"buy_now", "buynow", "fixed", "fixed_price", "instant", "listing"}:
            return "buy_now"

    if listing.get("price") is not None or listing.get("price_cents") is not None:
        return "buy_now"

    return "unknown"


def normalize_sticker_wear(value):
    if value in (None, ""):
        return -1.0
    try:
        wear = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return -1.0
    if wear < 0:
        return -1.0
    if wear > 1:
        wear = wear / 100
    return min(1.0, max(0.0, wear))


def normalize_sticker(sticker, index):
    if not isinstance(sticker, dict):
        return None

    price_cents = first_present(
        sticker.get("price_cents"),
        sticker.get("reference_price_cents"),
        get_nested(sticker, "reference", "price"),
        get_nested(sticker, "reference", "base_price"),
        0,
    )
    wear = normalize_sticker_wear(
        sticker.get("wear")
        if sticker.get("wear") not in (None, "")
        else (
            sticker.get("w")
            if sticker.get("w") not in (None, "")
            else (
                sticker.get("scrape")
                if sticker.get("scrape") not in (None, "")
                else sticker.get("scratches")
            )
        )
    )

    return {
        "sticker_id": first_present(
            sticker.get("stickerId"), sticker.get("sticker_id"), sticker.get("id")
        ),
        "name": sticker.get("name") or sticker.get("market_hash_name") or "",
        "slot": sticker.get("slot", index),
        "wear": wear,
        "price_cents": price_cents or 0,
        "raw_price_cents": price_cents or 0,
        "icon_url": normalize_icon_url(
            sticker.get("icon_url") or sticker.get("image_url") or sticker.get("image")
        ),
        "steam_market_url": sticker.get("steam_market_url") or sticker.get("steam_url"),
        "reference_quantity": first_present(
            get_nested(sticker, "reference", "quantity"),
            sticker.get("reference_quantity"),
            0,
        ),
        "reference_updated_at": first_present(
            get_nested(sticker, "reference", "updated_at"),
            sticker.get("reference_updated_at"),
        ),
        "offset_x": sticker.get("offset_x", 0),
        "offset_y": sticker.get("offset_y", 0),
    }


def normalize_keychain(keychain, index):
    if not isinstance(keychain, dict):
        return None
    price_cents = first_present(
        keychain.get("price_cents"),
        keychain.get("reference_price_cents"),
        get_nested(keychain, "reference", "price"),
        get_nested(keychain, "scm", "price"),
        0,
    )
    return {
        "keychain_id": first_present(
            keychain.get("stickerId"),
            keychain.get("keychain_id"),
            keychain.get("id"),
        ),
        "name": keychain.get("name") or "",
        "slot": keychain.get("slot", index),
        "pattern": keychain.get("pattern", -1),
        "price_cents": price_cents or 0,
        "raw_price_cents": price_cents or 0,
        "reference_quantity": first_present(
            get_nested(keychain, "reference", "quantity"),
            get_nested(keychain, "scm", "quantity"),
            keychain.get("reference_quantity"),
            0,
        ),
        "reference_updated_at": first_present(
            get_nested(keychain, "reference", "updated_at"),
            get_nested(keychain, "scm", "updated_at"),
            keychain.get("reference_updated_at"),
        ),
    }


def normalize_listing_for_deal(raw_payload, source_url, key_record, headers):
    listing = unwrap_listing(raw_payload)
    if not isinstance(listing, dict):
        raise CsfloatFetchError("CSFloat returned unexpected JSON shape.")

    item = listing.get("item") if isinstance(listing.get("item"), dict) else listing
    reference = (
        listing.get("reference") if isinstance(listing.get("reference"), dict) else {}
    )

    stickers = []
    for index, sticker in enumerate(item.get("stickers") or []):
        normalized = normalize_sticker(sticker, index)
        if normalized:
            stickers.append(normalized)

    keychains = []
    for index, keychain in enumerate(item.get("keychains") or item.get("charms") or []):
        normalized = normalize_keychain(keychain, index)
        if normalized:
            keychains.append(normalized)

    market_hash_name = (
        item.get("market_hash_name")
        or item.get("marketHashName")
        or item.get("name")
        or listing.get("market_hash_name")
        or ""
    )

    listing_id = first_present(listing.get("id"), item.get("id"))
    screenshot_id = (
        item.get("cs2_screenshot_id")
        or item.get("screenshot_id")
        or get_nested(item, "cs2_screenshot", "id")
        or get_nested(item, "screenshot", "id")
    )
    collection = item.get("collection")
    collection_name = (
        collection.get("name") if isinstance(collection, dict) else collection
    )
    payload = {
        "listing_id": listing_id,
        "url_text": source_url,
        "name": market_hash_name,
        "listing_type": normalize_listing_type(listing),
        "created_at": listing.get("created_at")
        or listing.get("createdAt")
        or listing.get("listed_at")
        or listing.get("listedAt"),
        "buy_price_cents": first_present(
            listing.get("price"), listing.get("price_cents")
        ),
        "float_value": first_present(item.get("float_value"), item.get("float")),
        "paint_seed": first_present(item.get("paint_seed"), item.get("paintSeed")),
        "paint_index": first_present(item.get("paint_index"), item.get("paintIndex")),
        "inspect_link": item.get("inspect_link"),
        "reference_base_price": first_present(
            reference.get("base_price"), item.get("reference_base_price")
        ),
        "reference_predicted_price": first_present(
            reference.get("predicted_price"), item.get("reference_predicted_price")
        ),
        "reference_float_factor": first_present(
            reference.get("float_factor"), item.get("reference_float_factor")
        ),
        "reference_quantity": first_present(
            reference.get("quantity"), item.get("reference_quantity")
        ),
        "reference_keychain_price": first_present(
            reference.get("keychain_price"), item.get("reference_keychain_price")
        ),
        "reference_last_updated": first_present(
            reference.get("last_updated"), item.get("reference_last_updated")
        ),
        "item_quality": item.get("quality"),
        "item_rarity": item.get("rarity"),
        "item_rarity_name": item.get("rarity_name"),
        "item_def_index": item.get("def_index"),
        "item_collection_name": first_present(
            collection_name, item.get("collection_name")
        ),
        "item_type_name": item.get("type_name"),
        "item_is_souvenir": item.get("is_souvenir"),
        "item_is_stattrak": item.get("is_stattrak"),
        "item_icon_url": normalize_icon_url(
            item.get("icon_url") or item.get("image_url") or item.get("image")
        ),
        "item_cs2_screenshot_id": screenshot_id,
        "item_cs2_screenshot_at": item.get("cs2_screenshot_at")
        or get_nested(item, "cs2_screenshot", "created_at"),
        "screenshot_url": (
            item.get("screenshot_url")
            or get_nested(item, "cs2_screenshot", "url")
            or get_nested(item, "screenshot", "url")
            or build_csfloat_screenshot_url(
                screenshot_id, listing_id, item.get("has_screenshot")
            )
        ),
        "stickers": stickers,
        "keychains": keychains,
    }

    return {
        "ok": True,
        "request_url": source_url,
        "listing_id": listing_id,
        "listing_api_url": f"{LISTING_API_BASE}/{urllib.parse.quote(str(listing_id), safe='')}"
        if listing_id
        else source_url,
        "key_file": key_record["path"],
        "proxy": key_record["proxy_label"],
        "rate_limit": headers.get("X-Ratelimit-Limit"),
        "rate_remaining": headers.get("X-Ratelimit-Remaining"),
        "rate_reset": headers.get("X-Ratelimit-Reset"),
        "deal_payload": payload,
    }


def fetch_listing_from_text(text):
    url = parse_listing_url_or_id(text)
    payload, meta = request_json_with_keys(url)
    key_record = {"path": meta["key_file"], "proxy_label": meta["proxy"]}
    headers = {
        "X-Ratelimit-Limit": meta.get("rate_limit"),
        "X-Ratelimit-Remaining": meta.get("rate_remaining"),
        "X-Ratelimit-Reset": meta.get("rate_reset"),
    }
    return normalize_listing_for_deal(payload, text, key_record, headers)


def sticker_filter(sticker_id):
    return json.dumps([{"i": int(sticker_id)}], separators=(",", ":"))


def fetch_active_listings(
    *,
    limit=50,
    sort_by="most_recent",
    cursor=None,
    market_hash_name=None,
    max_price=None,
    stickers=None,
    listing_type=None,
):
    """Fetch one bounded page of active listings with validated filters.

    Keeping URL construction here makes every scanner profile use the same
    cursor, limit and sort guarantees.  Callers pass already-serialized
    CSFloat sticker filters; skin callers should use
    :func:`fetch_active_listings_for_skin` so the $10 ceiling cannot drift.
    """

    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    limit = min(max(limit, 1), MAX_ACTIVE_LISTING_LIMIT)

    sort_by = str(sort_by or "").strip()
    if sort_by not in ALLOWED_ACTIVE_LISTING_SORTS:
        allowed = ", ".join(sorted(ALLOWED_ACTIVE_LISTING_SORTS))
        raise ValueError(f"sort_by must be one of: {allowed}")

    params = {
        "limit": limit,
        "sort_by": sort_by,
    }
    if market_hash_name not in (None, ""):
        market_hash_name = str(market_hash_name).strip()
        if not market_hash_name:
            raise ValueError("market_hash_name must not be empty")
        params["market_hash_name"] = market_hash_name
    if max_price is not None:
        try:
            max_price = int(max_price)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_price must be an integer number of cents") from exc
        if max_price < 1:
            raise ValueError("max_price must be at least 1 cent")
        params["max_price"] = max_price
    if stickers not in (None, ""):
        params["stickers"] = str(stickers)
    if listing_type not in (None, ""):
        listing_type = str(listing_type).strip()
        if listing_type not in ALLOWED_LISTING_TYPES:
            allowed = ", ".join(sorted(ALLOWED_LISTING_TYPES))
            raise ValueError(f"listing_type must be one of: {allowed}")
        params["type"] = listing_type
    if cursor not in (None, ""):
        params["cursor"] = str(cursor)
    url = LISTING_API_BASE + "?" + urllib.parse.urlencode(params)
    payload, meta = request_json_with_keys(url)
    listings = payload.get("data", []) if isinstance(payload, dict) else payload
    cursor = payload.get("cursor") if isinstance(payload, dict) else None
    return listings or [], cursor, meta, url


def fetch_active_listings_for_sticker(
    sticker_id, limit=50, sort_by="most_recent", cursor=None
):
    return fetch_active_listings(
        limit=limit,
        sort_by=sort_by,
        cursor=cursor,
        stickers=sticker_filter(sticker_id),
    )


def fetch_active_listings_for_skin(
    market_hash_name,
    *,
    limit=20,
    sort_by="lowest_price",
    cursor=None,
    max_price=CHEAP_SKIN_MAX_PRICE_CENTS,
):
    """Fetch active listings for one exact cheap skin, capped server-side."""

    market_hash_name = str(market_hash_name or "").strip()
    if not market_hash_name:
        raise ValueError("market_hash_name is required")
    try:
        max_price = int(max_price)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_price must be an integer number of cents") from exc
    if not 1 <= max_price <= CHEAP_SKIN_MAX_PRICE_CENTS:
        raise ValueError(
            f"cheap skin max_price must be between 1 and {CHEAP_SKIN_MAX_PRICE_CENTS} cents"
        )

    return fetch_active_listings(
        limit=limit,
        sort_by=sort_by,
        cursor=cursor,
        market_hash_name=market_hash_name,
        max_price=max_price,
    )


def clear_price_list_cache():
    """Drop the in-process CSFloat price-list cache.

    This is useful after configuration changes and keeps tests from reaching
    into the cache implementation.  It never performs a network request.
    """

    global _PRICE_LIST_CACHE
    with _PRICE_LIST_CACHE_LOCK:
        _PRICE_LIST_CACHE = None


def _validated_market_hash_name(value):
    if not isinstance(value, str):
        raise TypeError("market_hash_name must be a string")
    name = value.strip()
    if not name:
        raise ValueError("market_hash_name is required")
    return name


def _validated_positive_api_int(value, *, field):
    # bool is an int subclass in Python, but never a valid JSON price/quantity.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CsfloatFetchError(
            f"CSFloat returned invalid {field}; expected a positive integer"
        )
    return value


def _normalize_price_list_payload(payload):
    if not isinstance(payload, list):
        raise CsfloatFetchError("CSFloat price-list returned unexpected JSON shape")

    items = {}
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise CsfloatFetchError(
                f"CSFloat price-list row {index} is not an object"
            )

        name = row.get("market_hash_name")
        if not isinstance(name, str) or not name or name != name.strip():
            raise CsfloatFetchError(
                f"CSFloat price-list row {index} has invalid market_hash_name"
            )
        if name in items:
            raise CsfloatFetchError(
                f"CSFloat price-list contains duplicate market_hash_name: {name!r}"
            )

        price_cents = _validated_positive_api_int(
            row.get("min_price"), field=f"min_price in row {index}"
        )
        quantity = _validated_positive_api_int(
            row.get("quantity"), field=f"quantity in row {index}"
        )
        items[name] = {
            "current_price_cents": price_cents,
            "active_quantity": quantity,
        }

    return items


def _price_list_snapshot(*, force_refresh=False, ttl_seconds=None):
    """Return one validated immutable-by-convention price-list snapshot."""

    global _PRICE_LIST_CACHE

    if ttl_seconds is None:
        ttl_seconds = PRICE_LIST_CACHE_TTL_SECONDS
    if isinstance(ttl_seconds, bool):
        raise TypeError("ttl_seconds must be a non-negative number")
    try:
        ttl_seconds = float(ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("ttl_seconds must be a non-negative number") from exc
    if ttl_seconds < 0:
        raise ValueError("ttl_seconds must be a non-negative number")

    with _PRICE_LIST_CACHE_LOCK:
        now = time.monotonic()
        cached = _PRICE_LIST_CACHE
        if (
            not force_refresh
            and cached is not None
            and cached["expires_at"] > now
        ):
            return cached

        payload, _meta = request_json_with_keys(PRICE_LIST_API_URL)
        items = _normalize_price_list_payload(payload)
        observed_at = datetime.now(timezone.utc).isoformat()
        cached = {
            "items": items,
            "observed_at": observed_at,
            "expires_at": time.monotonic() + ttl_seconds,
        }
        _PRICE_LIST_CACHE = cached
        return cached


def fetch_csfloat_price_list(*, force=False):
    """Return the cached, validated CSFloat floor-price snapshot.

    ``items`` is an exact-name mapping.  Metadata intentionally contains no
    route, proxy or API-key information, so the result is safe to pass through
    an application service boundary.
    """

    snapshot = _price_list_snapshot(force_refresh=force)
    return {
        "items": {
            name: dict(values) for name, values in snapshot["items"].items()
        },
        "price_as_of": snapshot["observed_at"],
        "price_source": PRICE_LIST_SOURCE,
    }


def lookup_csfloat_price_list_floor(
    market_hash_name,
    *,
    force=False,
):
    """Look up an exact active buy-now floor in CSFloat's aggregate list.

    The returned price is an active asking price, not a completed sale, model
    forecast or buy-order bid.  ``None`` means the exact name was absent from
    the current price-list snapshot.
    """

    name = _validated_market_hash_name(market_hash_name)
    snapshot = _price_list_snapshot(force_refresh=force)
    values = snapshot["items"].get(name)
    if values is None:
        return None
    return {
        "market_hash_name": name,
        "current_price_cents": values["current_price_cents"],
        "active_quantity": values["active_quantity"],
        "price_source": PRICE_LIST_SOURCE,
        "price_as_of": snapshot["observed_at"],
    }


def fetch_lowest_active_buy_now_floor(market_hash_name):
    """Fetch and validate one exact live ``buy_now`` listing floor."""

    name = _validated_market_hash_name(market_hash_name)
    listings, _cursor, _meta, _url = fetch_active_listings(
        limit=1,
        sort_by="lowest_price",
        market_hash_name=name,
        listing_type="buy_now",
    )
    if not isinstance(listings, list):
        raise CsfloatFetchError("CSFloat listings endpoint returned unexpected data")
    if not listings:
        return None

    listing = listings[0]
    if not isinstance(listing, dict):
        raise CsfloatFetchError("CSFloat lowest listing is not an object")
    if listing.get("type") != "buy_now" or listing.get("state") != "listed":
        raise CsfloatFetchError(
            "CSFloat lowest listing is not an active buy-now listing"
        )

    item = listing.get("item")
    actual_name = item.get("market_hash_name") if isinstance(item, dict) else None
    if actual_name != name:
        raise CsfloatFetchError(
            "CSFloat lowest listing market_hash_name does not match the request"
        )
    price_cents = _validated_positive_api_int(
        listing.get("price"), field="listing price"
    )

    return {
        "market_hash_name": name,
        "current_price_cents": price_cents,
        # A one-row listing query proves that at least one ask exists, but it
        # does not report the total active quantity.  Do not invent that value.
        "active_quantity": None,
        "price_source": ACTIVE_BUY_NOW_SOURCE,
        "price_as_of": datetime.now(timezone.utc).isoformat(),
    }


def fetch_csfloat_floor(
    market_hash_name,
    *,
    allow_fallback=True,
):
    """Return CSFloat's lowest active buy-now ask for an exact item name.

    The cached aggregate price-list is preferred because one request covers
    commodities (including cases and capsules) as well as skins.  If that
    endpoint is unavailable or omits the exact name, a bounded live listings
    query is used.  No sale-history/model value is substituted for the ask.
    """

    name = _validated_market_hash_name(market_hash_name)
    try:
        result = lookup_csfloat_price_list_floor(name)
    except CsfloatFetchError:
        if not allow_fallback:
            raise
        result = None

    if result is not None or not allow_fallback:
        return result
    return fetch_lowest_active_buy_now_floor(name)
