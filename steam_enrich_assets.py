import argparse
import html
import json
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import app_config

DB_PATH = str(app_config.path("CS2_DB_PATH", "csfloat_history.db"))
STEAM_SEARCH_URL = "https://steamcommunity.com/market/search/render/"
STEAM_IMAGE_BASE_URL = "https://community.akamai.steamstatic.com/economy/image/"
STEAM_MARKET_BASE_URL = "https://steamcommunity.com/market/listings/730/"
REQUEST_DELAY_SECONDS = 1
ERROR_WAIT_SECONDS = 60


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_icon_url(icon_url):
    if not icon_url:
        return None

    if icon_url.startswith("http://") or icon_url.startswith("https://"):
        return icon_url

    return STEAM_IMAGE_BASE_URL + icon_url


def dumps_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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


def load_existing_assets(conn):
    return {
        row["market_hash_name"]
        for row in conn.execute("""
            SELECT market_hash_name
            FROM steam_market_assets
            WHERE found = 1
                AND (icon_url IS NOT NULL OR icon_url_large IS NOT NULL)
                AND (rarity_color IS NOT NULL OR name_color IS NOT NULL)
        """)
    }


def fetch_names(conn, asset_kind, missing_only):
    if asset_kind == "stickers":
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

        rows = conn.execute(f"""
            SELECT
                st.name AS market_hash_name,
                COUNT(*) AS row_count
            FROM sale_stickers st
            LEFT JOIN steam_market_assets sma ON sma.market_hash_name = st.name
            {where}
            GROUP BY st.name
            ORDER BY row_count DESC, market_hash_name ASC
        """).fetchall()
        return [(row["market_hash_name"], "sticker", row["row_count"]) for row in rows]

    where = "WHERE s.name IS NOT NULL"
    if missing_only:
        where += " AND (s.item_icon_url IS NULL OR s.item_icon_url = '' OR s.item_rarity_color IS NULL OR s.item_rarity_color = '')"

    rows = conn.execute(f"""
        SELECT
            s.name AS market_hash_name,
            COUNT(*) AS row_count
        FROM sales s
        {where}
        GROUP BY s.name
        ORDER BY row_count DESC, market_hash_name ASC
    """).fetchall()
    return [(row["market_hash_name"], "skin", row["row_count"]) for row in rows]


def steam_search(market_hash_name):
    params = {
        "query": market_hash_name,
        "start": "0",
        "count": "10",
        "search_descriptions": "0",
        "sort_column": "popular",
        "sort_dir": "desc",
        "appid": "730",
        "norender": "1",
    }
    url = STEAM_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    })

    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")
        return json.loads(body)


def steam_listing_page(market_hash_name):
    url = STEAM_MARKET_BASE_URL + urllib.parse.quote(market_hash_name, safe="")
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml",
    })

    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def extract_js_object(script_text, variable_name):
    marker = f"var {variable_name} = "
    start = script_text.find(marker)
    if start == -1:
        return None

    start += len(marker)
    end = script_text.find(";\n", start)
    if end == -1:
        end = script_text.find(";", start)
    if end == -1:
        return None

    raw = script_text[start:end].strip()
    if not raw or raw == "false":
        return None

    return json.loads(raw)


def payload_from_listing_page(market_hash_name):
    html = steam_listing_page(market_hash_name)
    assets = extract_js_object(html, "g_rgAssets")
    if not assets:
        return payload_from_ssr_listing_page(market_hash_name, html)

    for app_assets in assets.values():
        if not isinstance(app_assets, dict):
            continue

        for context_assets in app_assets.values():
            if not isinstance(context_assets, dict):
                continue

            for asset_description in context_assets.values():
                if not isinstance(asset_description, dict):
                    continue

                result = {
                    "name": asset_description.get("market_hash_name") or asset_description.get("name"),
                    "hash_name": asset_description.get("market_hash_name") or market_hash_name,
                    "asset_description": asset_description,
                }
                return {"results": [result], "source": "listing_page"}

    return {"results": []}


def unescape_steam_text(value):
    if value is None:
        return None

    value = value.replace("\\/", "/")
    value = value.replace('\\"', '"')
    return html.unescape(value)


def regex_first(pattern, text, flags=0):
    match = re.search(pattern, text, flags)
    if not match:
        return None

    return unescape_steam_text(match.group(1))


def payload_from_ssr_listing_page(market_hash_name, page_html):
    image_url = regex_first(r'<meta property="og:image" content="([^"]+)"', page_html)
    name_color = regex_first(r'name_color\\+":\\+"([0-9a-fA-F]{6})', page_html)
    steam_type = regex_first(r'name_color\\+":\\+"[0-9a-fA-F]{6}.*?type\\+":\\+"([^\\"]+)', page_html, re.DOTALL)

    rarity_match = re.search(
        r'category\\+":\\+"Rarity.*?'
        r'localized_tag_name\\+":\\+"([^\\"]+).*?'
        r'color\\+":\\+"([0-9a-fA-F]{6})',
        page_html,
        re.DOTALL,
    )

    rarity_tag = {}
    if rarity_match:
        rarity_tag = {
            "category": "Rarity",
            "localized_tag_name": unescape_steam_text(rarity_match.group(1)),
            "color": rarity_match.group(2),
        }

    collection_name = regex_first(
        r'category\\+":\\+"ItemSet.*?localized_tag_name\\+":\\+"([^\\"]+)',
        page_html,
        re.DOTALL,
    )
    exterior_name = regex_first(
        r'category\\+":\\+"Exterior.*?localized_tag_name\\+":\\+"([^\\"]+)',
        page_html,
        re.DOTALL,
    )

    tags = []
    if rarity_tag:
        tags.append(rarity_tag)
    if collection_name:
        tags.append({
            "category": "ItemSet",
            "localized_tag_name": collection_name,
        })
    if exterior_name:
        tags.append({
            "category": "Exterior",
            "localized_tag_name": exterior_name,
        })

    if not image_url and not name_color and not steam_type and not tags:
        return {"results": []}

    asset_description = {
        "market_hash_name": market_hash_name,
        "name": market_hash_name,
        "icon_url": image_url,
        "icon_url_large": image_url,
        "name_color": name_color,
        "type": steam_type,
        "tags": tags,
    }

    return {
        "results": [{
            "name": market_hash_name,
            "hash_name": market_hash_name,
            "asset_description": asset_description,
        }],
        "source": "listing_page_ssr",
    }


def pick_exact_result(payload, market_hash_name):
    results = payload.get("results") or []
    if not results:
        return None

    normalized_target = market_hash_name.casefold()

    for result in results:
        hash_name = result.get("hash_name") or result.get("market_hash_name")
        if isinstance(hash_name, str) and hash_name.casefold() == normalized_target:
            return result

        asset_description = result.get("asset_description") or {}
        asset_hash_name = asset_description.get("market_hash_name")
        if isinstance(asset_hash_name, str) and asset_hash_name.casefold() == normalized_target:
            return result

    for result in results:
        result_name = result.get("name")
        if isinstance(result_name, str) and result_name.casefold() == normalized_target:
            return result

    return results[0]


def get_tag(asset_description, category):
    for tag in asset_description.get("tags") or []:
        if tag.get("category") == category:
            return tag

    return {}


def parse_asset(market_hash_name, asset_kind, payload):
    result = pick_exact_result(payload, market_hash_name)
    fetched_at = utc_now()
    market_url = STEAM_MARKET_BASE_URL + urllib.parse.quote(market_hash_name, safe="")

    if not result:
        return {
            "market_hash_name": market_hash_name,
            "asset_kind": asset_kind,
            "found": 0,
            "icon_url": None,
            "icon_url_large": None,
            "name_color": None,
            "background_color": None,
            "steam_type": None,
            "rarity_name": None,
            "rarity_internal_name": None,
            "rarity_color": None,
            "exterior_name": None,
            "collection_name": None,
            "market_url": market_url,
            "raw_json": dumps_json(payload),
            "fetched_at": fetched_at,
        }

    asset_description = result.get("asset_description") or {}
    rarity_tag = get_tag(asset_description, "Rarity")
    exterior_tag = get_tag(asset_description, "Exterior")
    collection_tag = get_tag(asset_description, "ItemSet")

    return {
        "market_hash_name": market_hash_name,
        "asset_kind": asset_kind,
        "found": 1,
        "icon_url": normalize_icon_url(asset_description.get("icon_url")),
        "icon_url_large": normalize_icon_url(asset_description.get("icon_url_large")),
        "name_color": asset_description.get("name_color"),
        "background_color": asset_description.get("background_color"),
        "steam_type": asset_description.get("type") or result.get("type"),
        "rarity_name": rarity_tag.get("localized_tag_name"),
        "rarity_internal_name": rarity_tag.get("internal_name"),
        "rarity_color": rarity_tag.get("color") or asset_description.get("name_color"),
        "exterior_name": exterior_tag.get("localized_tag_name"),
        "collection_name": collection_tag.get("localized_tag_name"),
        "market_url": market_url,
        "raw_json": dumps_json(result),
        "fetched_at": fetched_at,
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
                steam_market_url = ?
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
                item_name_color = ?,
                item_background_color = ?,
                item_steam_type = ?,
                item_rarity_name = COALESCE(?, item_rarity_name),
                item_rarity_color = ?,
                item_exterior_name = ?,
                item_collection_name = ?,
                item_steam_market_url = ?
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


def sleep_with_message(seconds, reason):
    print(f"{reason}. Waiting {seconds} seconds...", flush=True)
    time.sleep(seconds)


def enrich(conn, names, skip_existing, limit):
    existing_assets = load_existing_assets(conn) if skip_existing else set()
    done = 0

    for index, (market_hash_name, asset_kind, row_count) in enumerate(names, start=1):
        if limit and done >= limit:
            break

        if market_hash_name in existing_assets:
            continue

        print(
            f"[{index}/{len(names)}] Steam {asset_kind}: {market_hash_name} "
            f"(rows={row_count})",
            flush=True,
        )

        try:
            payload = steam_search(market_hash_name)
            asset = parse_asset(market_hash_name, asset_kind, payload)

            if not asset["found"] or not (asset["icon_url"] or asset["icon_url_large"]):
                print("  search weak/missing, trying listing page...", flush=True)
                listing_payload = payload_from_listing_page(market_hash_name)
                listing_asset = parse_asset(market_hash_name, asset_kind, listing_payload)

                if listing_asset["found"]:
                    asset = listing_asset

            save_asset(conn, asset)
            conn.commit()
            done += 1

            status = "found" if asset["found"] else "not found"
            print(
                f"  {status} | icon={'yes' if asset['icon_url'] else 'no'} | "
                f"rarity={asset['rarity_name'] or 'unknown'} | color={asset['rarity_color'] or asset['name_color'] or 'unknown'}",
                flush=True,
            )

            time.sleep(REQUEST_DELAY_SECONDS)

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"  HTTP {exc.code}: {body[:300]}", flush=True)

            if exc.code == 429:
                sleep_with_message(ERROR_WAIT_SECONDS, "Steam rate limit")
            else:
                sleep_with_message(REQUEST_DELAY_SECONDS, "HTTP error")

        except (urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            print(f"  Error: {exc}", flush=True)
            sleep_with_message(ERROR_WAIT_SECONDS, "Network/JSON error")

    print(f"Done. Steam assets processed: {done}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--mode", choices=["stickers", "skins", "all"], default="all")
    parser.add_argument("--all-existing", action="store_true", help="Refresh even assets already saved in steam_market_assets")
    parser.add_argument("--limit", type=int, default=0, help="0 means no limit")
    args = parser.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"DB not found: {args.db}")

    with connect_db(args.db) as conn:
        ensure_schema(conn)

        names = []
        if args.mode in ("stickers", "all"):
            names.extend(fetch_names(conn, "stickers", missing_only=not args.all_existing))
        if args.mode in ("skins", "all"):
            names.extend(fetch_names(conn, "skins", missing_only=not args.all_existing))

        if not names:
            print("Nothing to enrich.", flush=True)
            return

        enrich(
            conn,
            names,
            skip_existing=not args.all_existing,
            limit=args.limit,
        )


if __name__ == "__main__":
    main()
