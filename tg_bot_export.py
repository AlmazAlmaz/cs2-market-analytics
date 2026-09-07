import json
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import app_config

DB_PATH = str(app_config.path("API_DB_PATH", "csfloat_sales_history.db"))
BOT_TOKEN = app_config.read_secret("TELEGRAM_BOT_TOKEN", "telegram_bot_token.txt")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

ALLOWED_CHAT_IDS = {
    int(value.strip())
    for value in app_config.get("TELEGRAM_ALLOWED_CHAT_IDS").split(",")
    if value.strip()
}
ALLOW_ALL = app_config.get_bool("TELEGRAM_ALLOW_ALL", False)

MAX_EXPORT_ROWS = 500000


def db_connect():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def telegram_request(method, data=None):
    body = urllib.parse.urlencode(data or {}).encode("utf-8")
    req = urllib.request.Request(f"{API_URL}/{method}", data=body)
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def send_message(chat_id, text):
    telegram_request("sendMessage", {
        "chat_id": chat_id,
        "text": text,
    })


def send_document(chat_id, file_path, caption):
    boundary = "----csfloatboundary"
    file_bytes = Path(file_path).read_bytes()
    file_name = Path(file_path).name

    parts = []

    def add_field(name, value):
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(str(value).encode())
        parts.append(b"\r\n")

    add_field("chat_id", chat_id)
    add_field("caption", caption)

    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="document"; filename="{file_name}"\r\n'
        f"Content-Type: application/zip\r\n\r\n"
        .encode()
    )
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(parts)

    req = urllib.request.Request(
        f"{API_URL}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    with urllib.request.urlopen(req, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def get_count():
    with db_connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]


def export_range(start, end):
    count = end - start

    with db_connect() as conn:
        sales = conn.execute("""
                             SELECT listing_id,
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
                             ORDER BY fetched_at, listing_id LIMIT ?
                             OFFSET ?
                             """, (count, start)).fetchall()

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

    payload = {
        "start": start,
        "end": end,
        "count": len(data),
        "data": data,
    }

    temp_dir = Path(tempfile.gettempdir())
    json_path = temp_dir / f"sales_{start}_{end}.json"
    zip_path = temp_dir / f"sales_{start}_{end}.zip"

    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.write(json_path, arcname=json_path.name)

    return zip_path, len(data)


def parse_export_command(text):
    parts = text.strip().split()

    if len(parts) != 3:
        raise ValueError("Формат: /export 0 1000")

    start = int(parts[1])
    end = int(parts[2])

    if start < 0:
        raise ValueError("start не может быть меньше 0")

    if end <= start:
        raise ValueError("end должен быть больше start")

    if end - start > MAX_EXPORT_ROWS:
        raise ValueError(f"За раз можно максимум {MAX_EXPORT_ROWS} строк")

    return start, end


def handle_message(message):
    chat_id = message["chat"]["id"]

    if not ALLOW_ALL and chat_id not in ALLOWED_CHAT_IDS:
        send_message(chat_id, "Access denied.")
        return

    text = message.get("text", "").strip()

    if text == "/start":
        send_message(
            chat_id,
            "Команды:\n"
            "/count - сколько строк в базе\n"
            "/export 0 1000 - выгрузить строки 0-1000\n"
            "/export 12000 13000 - выгрузить строки 12000-13000"
        )
        return

    if text == "/count":
        send_message(chat_id, f"Rows in sales: {get_count()}")
        return

    if text.startswith("/export"):
        zip_path = None
        try:
            start, end = parse_export_command(text)
            send_message(chat_id, f"Готовлю экспорт {start}-{end}...")

            zip_path, exported_count = export_range(start, end)

            send_document(
                chat_id,
                zip_path,
                f"Export {start}-{end}, rows: {exported_count}"
            )

        except Exception as exc:
            send_message(chat_id, f"Ошибка: {exc}")
        finally:
            if zip_path:
                zip_path.unlink(missing_ok=True)
                zip_path.with_suffix(".json").unlink(missing_ok=True)

        return

    send_message(chat_id, "Неизвестная команда. Напиши /start")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or telegram_bot_token.txt")
    if not ALLOW_ALL and not ALLOWED_CHAT_IDS:
        raise RuntimeError(
            "Telegram export bot is locked. Set TELEGRAM_ALLOWED_CHAT_IDS "
            "or explicitly set TELEGRAM_ALLOW_ALL=true."
        )
    offset = 0

    print("Telegram export bot started.")

    while True:
        try:
            payload = telegram_request("getUpdates", {
                "offset": offset,
                "timeout": 30,
            })

            for update in payload.get("result", []):
                message = update.get("message")
                if message:
                    handle_message(message)
                offset = update["update_id"] + 1

        except Exception as exc:
            print(f"Bot error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
