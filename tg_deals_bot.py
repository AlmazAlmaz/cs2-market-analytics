#!/usr/bin/env python3

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app_config

DB_PATH = str(app_config.path("CS2_DB_PATH", "csfloat_history.db"))
BOT_TOKEN_PATH = app_config.PROJECT_DIR / "telegram_bot_token.txt"
OFFSET_PATH = app_config.PROJECT_DIR / "telegram_deals_bot_offset.txt"
API_TIMEOUT_SECONDS = 60
MAX_DEALS_PER_MESSAGE = 10
DEFAULT_DEALS_LIMIT = 5
MSK = timezone(timedelta(hours=3))
WATCHERS = {}
DROP_OLD_UPDATES_ON_FIRST_START = True

ALLOWED_CHAT_IDS = {
    int(value.strip())
    for value in app_config.get("TELEGRAM_ALLOWED_CHAT_IDS").split(",")
    if value.strip().lstrip("-").isdigit()
}
TELEGRAM_ALLOW_ALL = app_config.get_bool("TELEGRAM_ALLOW_ALL", False)

MODEL_FILTERS = {
    "all", "almaz", "igor", "nova", "both", "all_three",
    "only_almaz", "only_igor", "only_nova",
}
SORT_MODES = {
    "seen_new",
    "created_new",
    "best_roi",
    "best_profit",
    "price_low",
    "price_high",
    "new_price_low",
    "liquidity_high",
    "sticker_value_high",
    "float_low",
}


def load_bot_token():
    token_path = Path(BOT_TOKEN_PATH)
    token = app_config.read_secret("TELEGRAM_BOT_TOKEN", token_path)
    if not token:
        raise RuntimeError(f"Set TELEGRAM_BOT_TOKEN in {app_config.CONFIG_PATH} or {BOT_TOKEN_PATH}")
    return token


BOT_TOKEN = app_config.read_secret("TELEGRAM_BOT_TOKEN", BOT_TOKEN_PATH)


def telegram_api_url():
    global BOT_TOKEN
    if not BOT_TOKEN:
        BOT_TOKEN = load_bot_token()
    return f"https://api.telegram.org/bot{BOT_TOKEN}"


def db_connect():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def telegram_request(method, data=None):
    body = urllib.parse.urlencode(data or {}).encode("utf-8")
    req = urllib.request.Request(f"{telegram_api_url()}/{method}", data=body)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as response:
            raw_response = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        description = f"HTTP {exc.code}"
        try:
            error_payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            description = error_payload.get("description") or description
        except (AttributeError, json.JSONDecodeError, OSError):
            pass
        raise RuntimeError(
            f"Telegram {method} failed: {safe_error_text(description)}"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"Telegram {method} connection failed: {safe_error_text(exc)}"
        ) from None

    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError:
        raise RuntimeError(f"Telegram {method} returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise TypeError(f"Telegram {method} returned an invalid response")
    if payload.get("ok") is False:
        raise RuntimeError(
            f"Telegram {method} failed: "
            f"{safe_error_text(payload.get('description') or 'unknown API error')}"
        )
    return payload


def safe_error_text(value):
    text = str(value or "unknown error")
    if BOT_TOKEN:
        text = text.replace(BOT_TOKEN, "***")
    text = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***", text)
    return text[:500]


def load_saved_offset():
    path = Path(OFFSET_PATH)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (TypeError, ValueError):
        return None


def save_offset(offset):
    path = Path(OFFSET_PATH)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as file:
            file.write(str(int(offset)))
            file.flush()
            os.fsync(file.fileno())
        temp_path.replace(path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def initial_offset():
    saved = load_saved_offset()
    if saved is not None:
        return saved
    if not DROP_OLD_UPDATES_ON_FIRST_START:
        return 0
    try:
        payload = telegram_request("getUpdates", {"timeout": 0})
        updates = payload.get("result") or []
        if not updates:
            return 0
        offset = max(update["update_id"] for update in updates) + 1
        save_offset(offset)
        print(f"Skipped old Telegram updates. Start offset: {offset}")
        return offset
    except Exception as exc:  # noqa: BLE001 - Telegram startup must remain recoverable
        print(f"Could not skip old Telegram updates: {safe_error_text(exc)}")
        return 0


def send_message(chat_id, text, disable_preview=True):
    chunks = split_telegram_text(text, 3900)
    for chunk in chunks:
        telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": "true" if disable_preview else "false",
        })


def send_photo(chat_id, photo_url, caption):
    return telegram_request("sendPhoto", {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption[:1024],
    })


def split_telegram_text(text, limit):
    if len(text) <= limit:
        return [text]
    chunks = []
    current = []
    size = 0
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                size = 0
            chunks.append(line[:limit])
            line = line[limit:]
        extra = len(line) + 1
        if current and size + extra > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks or [text[:limit]]


def money(cents):
    if cents is None:
        return "unknown"
    try:
        return f"${round(float(cents)) / 100:.2f}"
    except (TypeError, ValueError):
        return "unknown"


def percent(value):
    if value is None:
        return "unknown"
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "unknown"


def number(value, digits=6):
    if value is None or value == "":
        return "unknown"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def msk(value):
    if not value:
        return "unknown"
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S MSK")
    except (TypeError, ValueError):
        return str(value)


def parse_price_cents(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        price = float(text)
    except ValueError:
        return None
    if price < 0:
        return None
    return round(price * 100)


def parse_deals_command(text, default_category="all"):
    tokens = text.strip().split()
    options = {
        "limit": DEFAULT_DEALS_LIMIT,
        "category": default_category,
        "sort": "seen_new",
        "seen_hours": 24,
        "created_hours": 0,
        "min_price": None,
        "max_price": None,
        "search": "",
    }
    numeric_tokens = []
    search_tokens = []

    for token in tokens[1:]:
        raw = token.strip()
        lower = raw.lower()

        if "=" in raw:
            key, value = raw.split("=", 1)
            key = key.lower().strip()
            value = value.strip()
            if key in {"n", "limit", "count"}:
                options["limit"] = parse_limit(value)
            elif key in {"model", "category", "cat"} and value.lower() in MODEL_FILTERS:
                options["category"] = value.lower()
            elif key == "sort" and value.lower() in SORT_MODES:
                options["sort"] = value.lower()
            elif key in {"min", "min_price"}:
                options["min_price"] = value
            elif key in {"max", "max_price"}:
                options["max_price"] = value
            elif key in {"seen", "seen_hours"}:
                options["seen_hours"] = parse_float(value)
            elif key in {"created", "created_hours"}:
                options["created_hours"] = parse_float(value)
            elif key in {"q", "search"}:
                options["search"] = value
            continue

        if lower in MODEL_FILTERS:
            options["category"] = lower
        elif lower in SORT_MODES:
            options["sort"] = lower
        elif re.fullmatch(r"\d+([.,]\d+)?", raw):
            numeric_tokens.append(raw)
        else:
            search_tokens.append(raw)

    if numeric_tokens:
        options["limit"] = parse_limit(numeric_tokens[0])
    if len(numeric_tokens) >= 3:
        options["min_price"] = numeric_tokens[1]
        options["max_price"] = numeric_tokens[2]
    elif len(numeric_tokens) == 2:
        options["min_price"] = numeric_tokens[1]

    if search_tokens:
        options["search"] = " ".join(search_tokens)

    options["limit"] = min(max(int(options["limit"]), 1), MAX_DEALS_PER_MESSAGE)
    options["seen_hours"] = max(float(options["seen_hours"] or 0), 0)
    options["created_hours"] = max(float(options["created_hours"] or 0), 0)
    return options


def parse_limit(value):
    try:
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return DEFAULT_DEALS_LIMIT


def parse_float(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0


def load_deals(options):
    import dashboard_server

    with db_connect() as conn:
        return dashboard_server.fetch_model_deal_comparison_page(
            conn=conn,
            offset=0,
            limit=options["limit"],
            search=options["search"],
            category=options["category"],
            listing_type="all",
            sort_mode=options["sort"],
            seen_hours=options["seen_hours"],
            created_hours=options["created_hours"],
            min_price=options["min_price"],
            max_price=options["max_price"],
        )


def model_line(label, model):
    if not model:
        return f"{label}: not found"
    return (
        f"{label}: {money(model.get('predicted_price_cents'))} pred | "
        f"net {money(model.get('net_expected_cents'))} | "
        f"profit {money(model.get('profit_cents'))} | "
        f"ROI {percent(model.get('roi_percent'))} | "
        f"{model.get('verdict') or 'unknown'}"
    )


def stickers_text(stickers, max_count=5):
    if not stickers:
        return "Stickers: none"
    parts = []
    for sticker in stickers[:max_count]:
        name = sticker.get("name") or "unknown"
        price = money(sticker.get("price_cents") or sticker.get("raw_price_cents"))
        wear = number(sticker.get("wear"), 3)
        parts.append(f"{name} {price} wear {wear}")
    suffix = f" +{len(stickers) - max_count}" if len(stickers) > max_count else ""
    return "Stickers: " + "; ".join(parts) + suffix


def deal_caption(row, index=None):
    models = row.get("models") or {}
    title_prefix = f"#{index} " if index is not None else ""
    lines = [
        f"{title_prefix}{row.get('skin_name') or 'unknown'}",
        f"Buy: {money(row.get('buy_price_cents'))}",
        model_line("Almaz", models.get("almaz")),
        model_line("Igor", models.get("igor")),
        model_line("Nova", models.get("nova")),
        f"Found: {row.get('category') or 'all'}",
        f"Float: {number(row.get('float_value'), 6)} | seed {row.get('paint_seed') if row.get('paint_seed') is not None else 'unknown'} | paint {row.get('paint_index') if row.get('paint_index') is not None else 'unknown'}",
        stickers_text(row.get("stickers") or []),
        f"Created: {msk(row.get('listing_created_at'))}",
        f"Seen: {msk(row.get('last_seen_at'))}",
    ]
    if row.get("csfloat_url"):
        lines.append(row["csfloat_url"])
    return "\n".join(lines)


def send_deal(chat_id, row, index=None):
    caption = deal_caption(row, index)
    photo = row.get("screenshot_url") or row.get("item_icon_url")
    if photo:
        try:
            send_photo(chat_id, photo, caption)
            return True
        except Exception as exc:  # noqa: BLE001 - text fallback is intentional
            print(f"sendPhoto failed: {safe_error_text(exc)}")
    send_message(chat_id, caption, disable_preview=False)
    return True


def handle_deals(chat_id, text, default_category="all"):
    options = parse_deals_command(text, default_category)
    data = load_deals(options)
    rows = data.get("rows") or []
    summary = data.get("summary") or {}

    header = (
        f"Deals: {len(rows)} shown / {data.get('total', 0)} total\n"
        f"Almaz {summary.get('almaz', 0)} | Igor {summary.get('igor', 0)} | "
        f"Nova {summary.get('nova', 0)} | "
        f"Both {summary.get('both', 0)} | Only A {summary.get('only_almaz', 0)} | Only I {summary.get('only_igor', 0)}\n"
        f"Filter: {options['category']} | sort {options['sort']} | "
        f"min {options['min_price'] or '-'} | max {options['max_price'] or '-'}"
    )
    send_message(chat_id, header)

    if not rows:
        return

    for index, row in enumerate(rows, start=1):
        send_deal(chat_id, row, index)
        time.sleep(0.25)


def get_stats():
    if not Path(DB_PATH).exists():
        return f"DB not found: {DB_PATH}"

    with db_connect() as conn:
        rows = conn.execute("""
            SELECT
                COUNT(DISTINCT listing_id) AS total,
                COUNT(DISTINCT CASE WHEN model_name = 'almaz' THEN listing_id END) AS almaz,
                COUNT(DISTINCT CASE WHEN model_name = 'igor' THEN listing_id END) AS igor,
                COUNT(DISTINCT CASE WHEN verdict = 'good' THEN listing_id END) AS good,
                COUNT(DISTINCT CASE WHEN verdict = 'watch' THEN listing_id END) AS watch,
                MAX(last_seen_at) AS latest_seen
            FROM model_deal_predictions
            WHERE verdict IN ('good', 'watch')
        """).fetchone()
        scans = conn.execute("""
            SELECT scanned_at, sticker_name, listings_returned, candidates_saved, error
            FROM model_deal_scans
            ORDER BY scanned_at DESC
            LIMIT 5
        """).fetchall()

    lines = [
        "Best Deals stats",
        f"Total listings: {rows['total'] or 0}",
        f"Almaz: {rows['almaz'] or 0}",
        f"Igor: {rows['igor'] or 0}",
        f"Good: {rows['good'] or 0}",
        f"Watch: {rows['watch'] or 0}",
        f"Latest seen: {msk(rows['latest_seen'])}",
        "",
        "Last scans:",
    ]
    for scan in scans:
        status = scan["error"] or f"{scan['listings_returned']} listings, {scan['candidates_saved']} saved"
        lines.append(f"{msk(scan['scanned_at'])} | {scan['sticker_name'] or 'unknown'} | {status}")
    return "\n".join(lines)


def predict_csfloat_link(text):
    import csfloat_listing_fetcher
    import dashboard_server

    fetched = csfloat_listing_fetcher.fetch_listing_from_text(text)
    if fetched.get("error"):
        return fetched, {"error": fetched["error"]}
    payload = fetched.get("deal_payload") or {}
    prediction = dashboard_server.build_predict_deal_payload(payload)
    return fetched, prediction


def handle_link(chat_id, text):
    send_message(chat_id, "Fetching listing and running all three models...")
    try:
        fetched, prediction = predict_csfloat_link(text)
    except Exception as exc:  # noqa: BLE001 - report model/fetch errors to the requester
        send_message(chat_id, f"Error: {safe_error_text(exc)}")
        return

    if prediction.get("error"):
        send_message(chat_id, f"Error: {prediction['error']}")
        return

    payload = fetched.get("deal_payload") or {}
    row = {
        "listing_id": fetched.get("listing_id") or payload.get("listing_id"),
        "skin_name": payload.get("name"),
        "buy_price_cents": payload.get("buy_price_cents"),
        "models": prediction.get("models") or {},
        "category": f"best {prediction.get('best_model') or 'unknown'}",
        "float_value": payload.get("float_value"),
        "paint_seed": payload.get("paint_seed"),
        "paint_index": payload.get("paint_index"),
        "stickers": payload.get("stickers") or [],
        "listing_created_at": payload.get("created_at"),
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "screenshot_url": payload.get("screenshot_url"),
        "item_icon_url": payload.get("item_icon_url"),
        "csfloat_url": f"https://csfloat.com/item/{fetched.get('listing_id')}" if fetched.get("listing_id") else text,
    }
    send_deal(chat_id, row)
    rate = fetched.get("rate_remaining")
    if rate is not None:
        send_message(chat_id, f"CSFloat key: {fetched.get('key_file')} | remaining: {rate}")


def help_text():
    return (
        "Commands:\n"
        "/deals - last profitable deals\n"
        "/deals 10 igor 20 300 - 10 Igor deals from $20 to $300\n"
        "/deals 5 almaz sort=best_roi min=10 max=250\n"
        "/deals_almaz - all deals found by Almaz\n"
        "/deals_igor - all deals found by Igor\n"
        "/deals_nova - all deals found by Nova Neural\n"
        "/deals_both - deals found by Almaz and Igor\n"
        "/watch 10 igor min=20 max=300 - send new deals every 10 minutes\n"
        "/unwatch - stop auto notifications\n"
        "/latest - newest scanned deals\n"
        "/stats - scanner/model stats\n"
        "Send a CSFloat listing link to evaluate it with all three models."
    )


def handle_watch(chat_id, text):
    options = parse_deals_command(text.replace("/watch", "/deals", 1))
    parts = text.strip().split()
    interval_minutes = 10
    if len(parts) >= 2:
        parsed = parse_float(parts[1])
        if parsed > 0:
            interval_minutes = max(1, min(1440, parsed))
    options["limit"] = min(options["limit"], 5)
    options["sort"] = "seen_new"
    WATCHERS[chat_id] = {
        "options": options,
        "interval_seconds": int(interval_minutes * 60),
        "next_at": 0,
        "sent_listing_ids": set(),
    }
    send_message(
        chat_id,
        f"Auto deals enabled: every {interval_minutes:g} min | {options['category']} | "
        f"min {options['min_price'] or '-'} | max {options['max_price'] or '-'}"
    )


def run_watchers():
    now = time.time()
    for chat_id, watcher in list(WATCHERS.items()):
        if now < watcher["next_at"]:
            continue
        watcher["next_at"] = now + watcher["interval_seconds"]
        options = dict(watcher["options"])
        options["seen_hours"] = max(options.get("seen_hours") or 0, watcher["interval_seconds"] / 3600 * 3)
        try:
            data = load_deals(options)
            new_rows = [
                row for row in data.get("rows") or []
                if row.get("listing_id") not in watcher["sent_listing_ids"]
            ][:options["limit"]]
            if not new_rows:
                continue
            send_message(chat_id, f"New profitable deals: {len(new_rows)}")
            for index, row in enumerate(new_rows, start=1):
                send_deal(chat_id, row, index)
                watcher["sent_listing_ids"].add(row.get("listing_id"))
                time.sleep(0.25)
        except Exception as exc:  # noqa: BLE001 - isolate independent watchers
            print(f"Watcher error for {chat_id}: {safe_error_text(exc)}")
            try:
                send_message(chat_id, f"Watcher error: {safe_error_text(exc)}")
            except Exception as notify_exc:  # noqa: BLE001 - no further delivery channel
                print(
                    f"Could not notify watcher {chat_id}: {safe_error_text(notify_exc)}"
                )


def process_updates(payload, offset):
    """Process each Telegram update independently and durably skip poison updates."""
    updates = payload.get("result") or []
    if not isinstance(updates, list):
        raise TypeError("Telegram getUpdates returned an invalid result")

    current_offset = int(offset or 0)
    for update in updates:
        if not isinstance(update, dict):
            continue
        try:
            update_id = int(update["update_id"])
        except (KeyError, TypeError, ValueError):
            print("Skipped Telegram update without a valid update_id")
            continue
        if update_id < current_offset:
            continue

        next_offset = update_id + 1
        try:
            message = update.get("message")
            if message:
                handle_message(message)
        except Exception as exc:  # noqa: BLE001 - acknowledge poison updates and continue
            # A malformed command or one failed reply must not block every
            # later update forever.  The failure is logged and this update is
            # acknowledged in the durable offset below.
            print(f"Telegram update {update_id} failed: {safe_error_text(exc)}")
        current_offset = next_offset
        save_offset(current_offset)
    return current_offset


def handle_message(message):
    chat_id = message["chat"]["id"]
    if not TELEGRAM_ALLOW_ALL and chat_id not in ALLOWED_CHAT_IDS:
        send_message(chat_id, "Access denied.")
        return

    text = (message.get("text") or "").strip()
    if not text:
        return

    command = text.split()[0].split("@")[0].lower()
    if command in {"/start", "/help"}:
        send_message(chat_id, help_text())
    elif command == "/stats":
        send_message(chat_id, get_stats())
    elif command == "/deals":
        handle_deals(chat_id, text)
    elif command == "/latest":
        handle_deals(chat_id, text.replace("/latest", "/deals", 1))
    elif command == "/deals_almaz":
        handle_deals(chat_id, text.replace("/deals_almaz", "/deals", 1), "almaz")
    elif command == "/deals_igor":
        handle_deals(chat_id, text.replace("/deals_igor", "/deals", 1), "igor")
    elif command == "/deals_nova":
        handle_deals(chat_id, text.replace("/deals_nova", "/deals", 1), "nova")
    elif command == "/deals_both":
        handle_deals(chat_id, text.replace("/deals_both", "/deals", 1), "both")
    elif command == "/watch":
        handle_watch(chat_id, text)
    elif command == "/unwatch":
        WATCHERS.pop(chat_id, None)
        send_message(chat_id, "Auto deals disabled.")
    elif "csfloat.com" in text or re.search(r"\b\d{12,}\b", text):
        handle_link(chat_id, text)
    else:
        send_message(chat_id, "Unknown command. Send /help")


def main():
    load_bot_token()
    if not TELEGRAM_ALLOW_ALL and not ALLOWED_CHAT_IDS:
        raise RuntimeError(
            f"Telegram bot is locked. Set TELEGRAM_ALLOWED_CHAT_IDS in {app_config.CONFIG_PATH}, "
            "or explicitly set TELEGRAM_ALLOW_ALL=true."
        )
    offset = initial_offset()
    print("Telegram deals bot started.")
    print("Commands: /deals, /deals_igor, /deals_almaz, /deals_nova, /stats, CSFloat link")

    while True:
        try:
            payload = telegram_request("getUpdates", {
                "offset": offset,
                "timeout": 30,
            })
            offset = process_updates(payload, offset)
            run_watchers()

        except KeyboardInterrupt:
            print("Telegram deals bot stopped.")
            break
        except Exception as exc:  # noqa: BLE001 - long-running bot boundary
            print(f"Bot error: {safe_error_text(exc)}")
            time.sleep(5)


if __name__ == "__main__":
    main()
