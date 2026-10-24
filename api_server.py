#!/usr/bin/env python3

import json
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import app_config

DB_PATH = str(app_config.path("API_DB_PATH", "csfloat_sales_history.db"))
TOKEN_PATH = app_config.path("API_TOKEN_PATH", "api_token.txt")
HOST = app_config.get("API_HOST", "127.0.0.1")
PORT = app_config.get_int("API_PORT", 8000, minimum=1, maximum=65535)
API_TOKEN = app_config.read_secret("API_TOKEN", TOKEN_PATH)
API_ALLOW_QUERY_TOKEN = app_config.get_bool("API_ALLOW_QUERY_TOKEN", False)


def connect_db():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def get_sales(after_fetched_at, after_listing_id, limit):
    limit = min(max(int(limit), 1), 5000)

    with connect_db() as conn:
        sales = conn.execute("""
                             SELECT
                                 listing_id,
                                 name,
                                 base_skin_name,
                                 wear_name,
                                 price_cents,
                                 float_value,
                                 paint_seed,
                                 paint_index,
                                 inspect_link,
                                 serialized_inspect,
                                 state,
                                 created_at,
                                 sold_at,
                                 fetched_at
                             FROM sales
                             WHERE (fetched_at, listing_id) > (?, ?)
                             ORDER BY fetched_at, listing_id
                                 LIMIT ?
                             """, (
                                 after_fetched_at,
                                 after_listing_id,
                                 limit,
                             )).fetchall()

        listing_ids = [row["listing_id"] for row in sales]
        stickers_by_listing = {listing_id: [] for listing_id in listing_ids}

        if listing_ids:
            placeholders = ",".join("?" for _ in listing_ids)

            stickers = conn.execute(f"""
                SELECT
                    listing_id,
                    sticker_index,
                    sticker_id,
                    slot,
                    name,
                    wear,
                    price_cents
                FROM sale_stickers
                WHERE listing_id IN ({placeholders})
                ORDER BY listing_id, sticker_index
            """, listing_ids).fetchall()

            for sticker in stickers:
                stickers_by_listing[sticker["listing_id"]].append(dict(sticker))

        data = []

        for row in sales:
            item = dict(row)
            item["stickers"] = stickers_by_listing.get(row["listing_id"], [])
            data.append(item)

        if data:
            last = data[-1]
            next_cursor = {
                "after_fetched_at": last["fetched_at"],
                "after_listing_id": last["listing_id"],
            }
        else:
            next_cursor = {
                "after_fetched_at": after_fetched_at,
                "after_listing_id": after_listing_id,
            }

        return {
            "count": len(data),
            "next_cursor": next_cursor,
            "data": data,
        }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def is_authorized(self, query):
        header_token = self.headers.get("X-API-Token")
        query_token = query.get("token", [""])[0]

        return bool(API_TOKEN) and (
            secrets.compare_digest(str(header_token or ""), API_TOKEN)
            or (
                API_ALLOW_QUERY_TOKEN
                and secrets.compare_digest(str(query_token or ""), API_TOKEN)
            )
        )

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/health":
            try:
                with connect_db() as conn:
                    conn.execute("SELECT 1").fetchone()
                self.send_json(200, {"ok": True, "service": "cs2-export-api", "database": True})
            except sqlite3.Error as exc:
                self.send_json(
                    503,
                    {
                        "ok": False,
                        "service": "cs2-export-api",
                        "database": False,
                        "error": str(exc),
                    },
                )
            return

        if not self.is_authorized(query):
            self.send_json(401, {"error": "unauthorized"})
            return

        if parsed.path == "/sales":
            after_fetched_at = query.get("after_fetched_at", ["0000-01-01T00:00:00+00:00"])[0]
            after_listing_id = query.get("after_listing_id", [""])[0]
            limit = query.get("limit", ["1000"])[0]

            try:
                payload = get_sales(after_fetched_at, after_listing_id, limit)
            except ValueError:
                self.send_json(400, {"error": "limit must be an integer between 1 and 5000"})
                return
            except sqlite3.Error as exc:
                self.send_json(500, {"error": f"database error: {exc}"})
                return
            self.send_json(200, payload)
            return

        self.send_json(404, {"error": "not found"})


def main():
    if not API_TOKEN:
        raise RuntimeError(
            f"API token is empty. Set API_TOKEN in {app_config.CONFIG_PATH} "
            f"or create {TOKEN_PATH}."
        )
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    print(f"API running on http://{HOST}:{PORT}")
    print("Endpoint:")
    print(f"curl -H 'X-API-Token: YOUR_TOKEN' 'http://{HOST}:{PORT}/sales?limit=1000'")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("API stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
