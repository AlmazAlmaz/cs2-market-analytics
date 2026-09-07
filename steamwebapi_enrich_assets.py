import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import app_config

DB_PATH = str(app_config.path("CS2_DB_PATH", "csfloat_history.db"))
KEY_PATH = app_config.PROJECT_DIR / "steamwebapi_key.txt"
STEAMWEBAPI_ITEMS_URL = "https://www.steamwebapi.com/steam/api/items"
STEAM_MARKET_BASE_URL = "https://steamcommunity.com/market/listings/730/"
REQUEST_DELAY_SECONDS = 2
ERROR_WAIT_SECONDS = 60


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def dumps_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def first_value(item, names):
    for name in names:
        value = item.get(name)
        if value not in (None, ""):
            return value

    return None


def normalize_market_name(value):
    if not value:
        return None

    return str(value).strip()


def steam_market_url(market_hash_name):
    if not market_hash_name:
        return None

    return STEAM_MARKET_BASE_URL + urllib.parse.quote(market_hash_name, safe="")


def load_api_key(arg_key):
    if arg_key:
        return arg_key.strip()

    key = app_config.read_secret("STEAMWEBAPI_KEY", KEY_PATH)
    if key:
        return key

    raise SystemExit(
        f"SteamWebAPI key not found. Create {KEY_PATH} with your key, "
        f"or run with --key YOUR_KEY"
    )


def connect_db(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_column(conn, table_name, column_name, column_type):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS steam_market_assets (
            market_hash_name TEXT PRIMARY KEY,
            asset_kind TEXT NOT NULL,
            found INTEGER NOT NULL,
            icon_url TEXT,
            icon_url_large TEXT,
            name_color TEXT,
            background_color TEXT,
            steam_type TEXT,
            rarity_name TEXT,
            rarity_internal_name TEXT,
            rarity_color TEXT,
            exterior_name TEXT,
            collection_name TEXT,
            market_url TEXT,
            raw_json TEXT,
            fetched_at TEXT NOT NULL
        )
    """)

    ensure_column(conn, "steam_market_assets", "source", "TEXT")

    ensure_column(conn, "sales", "item_icon_url", "TEXT")
    ensure_column(conn, "sales", "item_name_color", "TEXT")
    ensure_column(conn, "sales", "item_background_color", "TEXT")
    ensure_column(conn, "sales", "item_steam_type", "TEXT")
    ensure_column(conn, "sales", "item_rarity_color", "TEXT")
    ensure_column(conn, "sales", "item_exterior_name", "TEXT")
    ensure_column(conn, "sales", "item_collection_name", "TEXT")
    ensure_column(conn, "sales", "item_steam_market_url", "TEXT")

    ensure_column(conn, "sale_stickers", "icon_url", "TEXT")
    ensure_column(conn, "sale_stickers", "steam_market_url", "TEXT")

    conn.commit()


def needed_names(conn, mode, missing_only):
    names = {}

    if mode in ("stickers", "all"):
        where = "WHERE st.name IS NOT NULL"
        if missing_only:
            where += """
                AND (
                    st.icon_url IS NULL
                    OR st.icon_url = ''
                    OR sma.market_hash_name IS NULL
                    OR sma.found = 0
                    OR (
                        (sma.rarity_color IS NULL OR sma.rarity_color = '')
                        AND (sma.name_color IS NULL OR sma.name_color = '')
                    )
                )
            """

        for row in conn.execute(f"""
            SELECT st.name AS market_hash_name, COUNT(*) AS row_count
            FROM sale_stickers st
            LEFT JOIN steam_market_assets sma ON sma.market_hash_name = st.name
            {where}
            GROUP BY st.name
        """):
            names[row["market_hash_name"]] = {
                "asset_kind": "sticker",
                "row_count": row["row_count"],
            }

    if mode in ("skins", "all"):
        where = "WHERE s.name IS NOT NULL"
        if missing_only:
            where += """
                AND (
                    s.item_icon_url IS NULL
                    OR s.item_icon_url = ''
                    OR s.item_rarity_color IS NULL
                    OR s.item_rarity_color = ''
                )
            """

        for row in conn.execute(f"""
            SELECT s.name AS market_hash_name, COUNT(*) AS row_count
            FROM sales s
            {where}
            GROUP BY s.name
        """):
            names[row["market_hash_name"]] = {
                "asset_kind": "skin",
                "row_count": row["row_count"],
            }

    return names


def fetch_catalog_page(api_key, page, max_items, extra_params):
    params = {
        "key": api_key,
        "game": "cs2",
        "page": str(page),
        "max": str(max_items),
        "production": "1",
        "select": ",".join([
            "markethashname",
            "market_hash_name",
            "itemimage",
            "image",
            "steamurl",
            "rarity",
            "color",
            "itemtype",
            "type",
            "itemname",
            "wear",
            "collection",
            "stattrak",
            "souvenir",
        ]),
    }
    params.update(extra_params)

    url = STEAMWEBAPI_ITEMS_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    })

    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8", errors="replace")
        payload = json.loads(body)

    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in ("items", "data", "results", "response"):
            value = payload.get(key)
            if isinstance(value, list):
                return value

    raise ValueError(f"Unexpected SteamWebAPI response shape: {type(payload).__name__}")


def asset_from_catalog_item(item, fallback_name, asset_kind):
    market_hash_name = normalize_market_name(first_value(item, [
        "markethashname",
        "market_hash_name",
        "marketHashName",
        "market_hash_name_full",
        "name",
    ]) or fallback_name)

    rarity = first_value(item, ["rarity", "rarity_name", "quality", "quality_name"])
    color = first_value(item, ["color", "rarity_color", "name_color", "quality_color"])

    return {
        "market_hash_name": market_hash_name,
        "asset_kind": asset_kind,
        "found": 1,
        "icon_url": first_value(item, ["itemimage", "image", "icon_url", "imageurl", "image_url"]),
        "icon_url_large": first_value(item, ["itemimage", "image", "icon_url_large", "imageurl", "image_url"]),
        "name_color": color,
        "background_color": first_value(item, ["background_color", "background"]),
        "steam_type": first_value(item, ["itemtype", "type", "type_name"]),
        "rarity_name": rarity,
        "rarity_internal_name": first_value(item, ["rarity_internal_name", "rarity_internal"]),
        "rarity_color": color,
        "exterior_name": first_value(item, ["wear", "exterior", "exterior_name"]),
        "collection_name": first_value(item, ["collection", "collection_name", "itemset"]),
        "market_url": first_value(item, ["steamurl", "steam_url", "market_url"]) or steam_market_url(market_hash_name),
        "raw_json": dumps_json(item),
        "fetched_at": utc_now(),
        "source": "steamwebapi.com",
    }


def save_asset(conn, asset):
    columns = list(asset.keys())
    placeholders = ", ".join("?" for _ in columns)
    update_columns = [column for column in columns if column != "market_hash_name"]
    update_sql = ", ".join(f"{column} = excluded.{column}" for column in update_columns)

    conn.execute(f"""
        INSERT INTO steam_market_assets (
            {", ".join(columns)}
        )
        VALUES ({placeholders})
        ON CONFLICT(market_hash_name) DO UPDATE SET
            {update_sql}
    """, [asset[column] for column in columns])

    if asset["asset_kind"] == "sticker":
        conn.execute("""
            UPDATE sale_stickers
            SET
                icon_url = COALESCE(?, icon_url),
                steam_market_url = COALESCE(?, steam_market_url)
            WHERE name = ?
        """, (
            asset["icon_url"] or asset["icon_url_large"],
            asset["market_url"],
            asset["market_hash_name"],
        ))
    else:
        conn.execute("""
            UPDATE sales
            SET
                item_icon_url = COALESCE(?, item_icon_url),
                item_name_color = COALESCE(?, item_name_color),
                item_background_color = COALESCE(?, item_background_color),
                item_steam_type = COALESCE(?, item_steam_type),
                item_rarity_name = COALESCE(?, item_rarity_name),
                item_rarity_color = COALESCE(?, item_rarity_color),
                item_exterior_name = COALESCE(?, item_exterior_name),
                item_collection_name = COALESCE(?, item_collection_name),
                item_steam_market_url = COALESCE(?, item_steam_market_url)
            WHERE name = ?
        """, (
            asset["icon_url"] or asset["icon_url_large"],
            asset["name_color"],
            asset["background_color"],
            asset["steam_type"],
            asset["rarity_name"],
            asset["rarity_color"],
            asset["exterior_name"],
            asset["collection_name"],
            asset["market_url"],
            asset["market_hash_name"],
        ))


def enrich_from_catalog(conn, api_key, wanted, max_items, limit, extra_params):
    found = 0
    processed_pages = 0
    page = 1

    wanted_remaining = dict(wanted)

    while wanted_remaining:
        print(f"Loading SteamWebAPI catalog page {page}...", flush=True)

        try:
            items = fetch_catalog_page(api_key, page, max_items, extra_params)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"HTTP {exc.code}: {body[:300]}", flush=True)
            time.sleep(ERROR_WAIT_SECONDS)
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            print(f"SteamWebAPI error: {exc}", flush=True)
            time.sleep(ERROR_WAIT_SECONDS)
            continue

        processed_pages += 1
        if not items:
            break

        for item in items:
            market_hash_name = normalize_market_name(first_value(item, [
                "markethashname",
                "market_hash_name",
                "marketHashName",
                "name",
            ]))

            if not market_hash_name or market_hash_name not in wanted_remaining:
                continue

            meta = wanted_remaining.pop(market_hash_name)
            asset = asset_from_catalog_item(item, market_hash_name, meta["asset_kind"])
            save_asset(conn, asset)
            conn.commit()
            found += 1

            print(
                f"  found {asset['asset_kind']}: {market_hash_name} | "
                f"icon={'yes' if asset['icon_url'] else 'no'} | "
                f"rarity={asset['rarity_name'] or 'unknown'} | "
                f"color={asset['rarity_color'] or asset['name_color'] or 'unknown'}",
                flush=True,
            )

            if limit and found >= limit:
                print(f"Limit reached. Found: {found}", flush=True)
                return found, len(wanted_remaining), processed_pages

        if len(items) < max_items:
            break

        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    return found, len(wanted_remaining), processed_pages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--key")
    parser.add_argument("--mode", choices=["stickers", "skins", "all"], default="all")
    parser.add_argument("--all-existing", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-items", type=int, default=50000)
    parser.add_argument("--param", action="append", default=[], help="Extra SteamWebAPI query param, format key=value")
    args = parser.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"DB not found: {args.db}")

    api_key = load_api_key(args.key)
    extra_params = {}
    for item in args.param:
        if "=" not in item:
            raise SystemExit(f"Bad --param value: {item}. Expected key=value")
        key, value = item.split("=", 1)
        extra_params[key] = value

    with connect_db(args.db) as conn:
        ensure_schema(conn)
        wanted = needed_names(conn, args.mode, missing_only=not args.all_existing)

        if not wanted:
            print("Nothing to enrich.", flush=True)
            return

        print(f"Need assets: {len(wanted)}", flush=True)
        found, missing, pages = enrich_from_catalog(
            conn,
            api_key,
            wanted,
            args.max_items,
            args.limit,
            extra_params,
        )

        print(
            f"Done. Found={found}, still missing={missing}, catalog pages={pages}",
            flush=True,
        )


if __name__ == "__main__":
    main()
