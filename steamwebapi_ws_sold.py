import argparse
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import app_config

DB_PATH = str(app_config.path("CS2_DB_PATH", "csfloat_history.db"))
KEY_PATH = app_config.PROJECT_DIR / "steamwebapi_key.txt"
WS_URL_PATH = app_config.PROJECT_DIR / "steamwebapi_ws_url.txt"
DEFAULT_SUBSCRIBE = {
    "action": "subscribe",
    "markets": ["csfloat"],
    "game": "cs2",
}


packet_queue = asyncio.Queue(maxsize=10000)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def dumps_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_key(arg_key):
    if arg_key:
        return arg_key.strip()

    key = app_config.read_secret("STEAMWEBAPI_KEY", KEY_PATH)
    if key:
        return key

    return ""


def load_ws_url(arg_url, api_key):
    if arg_url:
        url = arg_url.strip()
    elif Path(WS_URL_PATH).exists():
        url = Path(WS_URL_PATH).read_text(encoding="utf-8").strip()
    else:
        raise SystemExit(
            f"WebSocket URL not found. Put full wss:// URL into {WS_URL_PATH}, "
            f"or run with --url FULL_WSS_URL"
        )

    if "{key}" in url:
        url = url.replace("{key}", api_key)

    if not url.startswith("wss://") and not url.startswith("ws://"):
        raise SystemExit(f"Bad websocket URL: {url}. It must start with ws:// or wss://")

    return url


def load_subscribe_packet(path):
    if not path:
        return DEFAULT_SUBSCRIBE

    return json.loads(Path(path).read_text(encoding="utf-8"))


def connect_db(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS steamwebapi_ws_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            event_type TEXT,
            market TEXT,
            raw_json TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS steamwebapi_sold_events (
            event_id TEXT PRIMARY KEY,
            received_at TEXT NOT NULL,
            sold_at TEXT,
            market TEXT,
            skin_name TEXT,
            price_usd REAL,
            price_cents INTEGER,
            currency TEXT,
            float_value REAL,
            paint_seed INTEGER,
            paint_index INTEGER,
            item_icon_url TEXT,
            inspect_link TEXT,
            stickers_count INTEGER,
            stickers_raw TEXT,
            item_raw_json TEXT,
            raw_json TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS steamwebapi_sold_stickers (
            event_id TEXT NOT NULL,
            sticker_index INTEGER NOT NULL,
            sticker_id INTEGER,
            slot INTEGER,
            name TEXT,
            wear REAL,
            price_cents INTEGER,
            icon_url TEXT,
            raw_json TEXT,
            PRIMARY KEY (event_id, sticker_index)
        )
    """)

    conn.commit()


def get_nested(data, path, default=None):
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def first_value(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def event_type_of(packet):
    return first_value(
        packet.get("event"),
        packet.get("type"),
        packet.get("action"),
        get_nested(packet, ["data", "event"]),
        get_nested(packet, ["data", "type"]),
        get_nested(packet, ["data", "state"]),
    )


def market_of(packet):
    return first_value(
        packet.get("market"),
        packet.get("source"),
        get_nested(packet, ["data", "market"]),
        get_nested(packet, ["data", "source"]),
        get_nested(packet, ["data", "marketplace"]),
    )


def item_of(packet):
    data = packet.get("data") if isinstance(packet.get("data"), dict) else packet

    return first_value(
        data.get("item") if isinstance(data, dict) else None,
        data.get("asset") if isinstance(data, dict) else None,
        get_nested(data, ["listing", "item"]) if isinstance(data, dict) else None,
        get_nested(data, ["sale", "item"]) if isinstance(data, dict) else None,
        {},
    )


def data_of(packet):
    return packet.get("data") if isinstance(packet.get("data"), dict) else packet


def looks_sold(packet):
    data = data_of(packet)
    event_type = str(event_type_of(packet) or "").lower()
    state = str(first_value(data.get("state"), data.get("status"), data.get("sale_status")) or "").lower()

    return (
        "sold" in event_type
        or "sale" in event_type
        or "sell" in event_type
        or state in ("sold", "sale", "completed", "complete")
        or data.get("sold_at") is not None
    )


def normalize_price(data):
    price_cents = first_value(
        data.get("price_cents"),
        data.get("priceInCents"),
        data.get("price_c"),
    )

    if price_cents is None:
        price = first_value(data.get("price"), data.get("price_usd"), data.get("priceUSD"))
        if isinstance(price, int) and price > 1000:
            price_cents = price
            price_usd = price / 100
        elif price is not None:
            price_usd = float(price)
            price_cents = int(round(price_usd * 100))
        else:
            price_usd = None
    else:
        price_cents = int(price_cents)
        price_usd = price_cents / 100

    return price_usd, price_cents


def stable_event_id(packet, received_at):
    data = data_of(packet)
    item = item_of(packet)

    event_id = first_value(
        data.get("id"),
        data.get("sale_id"),
        data.get("listing_id"),
        data.get("event_id"),
        packet.get("id"),
        packet.get("event_id"),
    )

    if event_id:
        return str(event_id)

    skin_name = first_value(item.get("market_hash_name"), item.get("marketHashName"), item.get("name"))
    sold_at = first_value(data.get("sold_at"), data.get("soldAt"), data.get("created_at"), data.get("time"))
    price_usd, price_cents = normalize_price(data)
    return f"generated:{skin_name}:{sold_at}:{price_cents}:{received_at}"


def normalize_sold_event(packet, received_at):
    data = data_of(packet)
    item = item_of(packet)
    stickers = item.get("stickers") if isinstance(item.get("stickers"), list) else []
    price_usd, price_cents = normalize_price(data)
    event_id = stable_event_id(packet, received_at)

    row = {
        "event_id": event_id,
        "received_at": received_at,
        "sold_at": first_value(data.get("sold_at"), data.get("soldAt"), data.get("created_at"), data.get("time")),
        "market": market_of(packet),
        "skin_name": first_value(item.get("market_hash_name"), item.get("marketHashName"), item.get("name")),
        "price_usd": price_usd,
        "price_cents": price_cents,
        "currency": first_value(data.get("currency"), data.get("currency_code")),
        "float_value": first_value(item.get("float_value"), item.get("float"), item.get("wear")),
        "paint_seed": first_value(item.get("paint_seed"), item.get("paintSeed")),
        "paint_index": first_value(item.get("paint_index"), item.get("paintIndex")),
        "item_icon_url": first_value(item.get("icon_url"), item.get("image"), item.get("image_url")),
        "inspect_link": first_value(item.get("inspect_link"), item.get("inspectLink")),
        "stickers_count": len(stickers),
        "stickers_raw": dumps_json(stickers),
        "item_raw_json": dumps_json(item),
        "raw_json": dumps_json(packet),
    }

    return row, stickers


def insert_ws_message(conn, packet, received_at):
    conn.execute("""
        INSERT INTO steamwebapi_ws_messages (
            received_at,
            event_type,
            market,
            raw_json
        )
        VALUES (?, ?, ?, ?)
    """, (
        received_at,
        event_type_of(packet),
        market_of(packet),
        dumps_json(packet),
    ))


def insert_sold_event(conn, row, stickers):
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "event_id")

    conn.execute(f"""
        INSERT INTO steamwebapi_sold_events (
            {", ".join(columns)}
        )
        VALUES ({placeholders})
        ON CONFLICT(event_id) DO UPDATE SET
            {updates}
    """, [row[column] for column in columns])

    conn.execute("DELETE FROM steamwebapi_sold_stickers WHERE event_id = ?", (row["event_id"],))

    for index, sticker in enumerate(stickers):
        reference = sticker.get("reference") or sticker.get("scm") or {}
        conn.execute("""
            INSERT INTO steamwebapi_sold_stickers (
                event_id,
                sticker_index,
                sticker_id,
                slot,
                name,
                wear,
                price_cents,
                icon_url,
                raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["event_id"],
            index,
            first_value(sticker.get("stickerId"), sticker.get("sticker_id"), sticker.get("id")),
            sticker.get("slot"),
            sticker.get("name"),
            sticker.get("wear"),
            first_value(sticker.get("price_cents"), sticker.get("price"), reference.get("price")),
            first_value(sticker.get("icon_url"), sticker.get("image"), sticker.get("image_url")),
            dumps_json(sticker),
        ))


async def socket_receiver(ws_url, subscribe_packet):
    import websockets

    while True:
        try:
            print(f"Connecting to {ws_url}", flush=True)

            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20, max_queue=None) as websocket:
                if subscribe_packet:
                    await websocket.send(dumps_json(subscribe_packet))
                    print(f"Subscribed: {subscribe_packet}", flush=True)

                async for message in websocket:
                    await packet_queue.put(message)

        except Exception as exc:
            print(f"WebSocket error: {exc}. Reconnecting in 5 seconds...", flush=True)
            await asyncio.sleep(5)


async def packet_processor(db_path, save_raw, only_sold):
    conn = connect_db(db_path)
    init_db(conn)
    processed = 0
    sold_count = 0
    last_commit = time.monotonic()

    try:
        while True:
            message = await packet_queue.get()
            received_at = utc_now()

            try:
                packet = json.loads(message)

                if save_raw:
                    insert_ws_message(conn, packet, received_at)

                if (not only_sold) or looks_sold(packet):
                    row, stickers = normalize_sold_event(packet, received_at)

                    if row["skin_name"]:
                        insert_sold_event(conn, row, stickers)
                        sold_count += 1
                        print(
                            f"sold #{sold_count}: {row['skin_name']} | "
                            f"${row['price_usd'] or 0:.2f} | stickers={row['stickers_count']}",
                            flush=True,
                        )

                processed += 1

                if processed % 100 == 0 or time.monotonic() - last_commit > 5:
                    conn.commit()
                    last_commit = time.monotonic()

            except Exception as exc:
                print(f"Processor error: {exc}", flush=True)

            finally:
                packet_queue.task_done()

    finally:
        conn.commit()
        conn.close()


async def main_async(args):
    api_key = load_key(args.key)
    ws_url = load_ws_url(args.url, api_key)
    subscribe_packet = load_subscribe_packet(args.subscribe_file)

    await asyncio.gather(
        socket_receiver(ws_url, subscribe_packet),
        packet_processor(args.db, save_raw=not args.no_raw, only_sold=not args.all_events),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--key")
    parser.add_argument("--url", help="Full SteamWebAPI websocket URL. Supports {key} placeholder.")
    parser.add_argument("--subscribe-file", help="JSON file with subscribe packet. Default subscribes to csfloat/cs2.")
    parser.add_argument("--no-raw", action="store_true", help="Do not save raw websocket messages.")
    parser.add_argument("--all-events", action="store_true", help="Normalize every event, not only sold-looking events.")
    args = parser.parse_args()

    try:
        import websockets  # noqa: F401
    except ImportError:
        raise SystemExit("Missing dependency. Install it: py -m pip install websockets")

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
