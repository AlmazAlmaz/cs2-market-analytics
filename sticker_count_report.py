import argparse
import csv
import sqlite3
from pathlib import Path

import app_config

DEFAULT_DB_CANDIDATES = [
    str(app_config.PROJECT_DIR / "cs_float.db"),
    str(app_config.path("CS2_DB_PATH", "csfloat_history.db")),
    str(app_config.path("API_DB_PATH", "csfloat_sales_history.db")),
]


def pick_default_db():
    for db_path in DEFAULT_DB_CANDIDATES:
        if Path(db_path).exists():
            return db_path

    return DEFAULT_DB_CANDIDATES[0]


def fetch_rows(conn, order_direction, limit, exclude_zero):
    where = ""
    if exclude_zero:
        where = "WHERE sticker_count > 0"

    query = f"""
        WITH per_skin AS (
            SELECT
                s.name AS skin_name,
                s.base_skin_name AS base_skin_name,
                s.wear_name AS wear_name,
                COUNT(DISTINCT s.listing_id) AS listing_count,
                COUNT(st.sticker_id) AS sticker_count,
                COUNT(DISTINCT CASE WHEN st.sticker_id IS NOT NULL THEN s.listing_id END) AS listings_with_stickers
            FROM sales s
            LEFT JOIN sale_stickers st ON st.listing_id = s.listing_id
            GROUP BY
                s.name,
                s.base_skin_name,
                s.wear_name
        )
        SELECT
            skin_name,
            base_skin_name,
            wear_name,
            listing_count,
            sticker_count,
            listings_with_stickers,
            ROUND(CAST(sticker_count AS REAL) / NULLIF(listing_count, 0), 3) AS avg_stickers_per_listing,
            ROUND(100.0 * listings_with_stickers / NULLIF(listing_count, 0), 2) AS stickered_listing_percent
        FROM per_skin
        {where}
        ORDER BY
            sticker_count {order_direction},
            listing_count {order_direction},
            skin_name ASC
        LIMIT ?
    """

    conn.row_factory = sqlite3.Row
    return conn.execute(query, (limit,)).fetchall()


def fetch_weapon_rows(conn, order_direction, limit, exclude_zero):
    where = ""
    if exclude_zero:
        where = "WHERE sticker_count > 0"

    query = f"""
        WITH per_weapon AS (
            SELECT
                CASE
                    WHEN INSTR(COALESCE(s.base_skin_name, s.name), ' | ') > 0
                    THEN SUBSTR(COALESCE(s.base_skin_name, s.name), 1, INSTR(COALESCE(s.base_skin_name, s.name), ' | ') - 1)
                    ELSE COALESCE(s.base_skin_name, s.name)
                END AS weapon_name,
                COUNT(DISTINCT s.name) AS skin_variant_count,
                COUNT(DISTINCT s.listing_id) AS listing_count,
                COUNT(st.sticker_id) AS sticker_count,
                COUNT(DISTINCT CASE WHEN st.sticker_id IS NOT NULL THEN s.listing_id END) AS listings_with_stickers
            FROM sales s
            LEFT JOIN sale_stickers st ON st.listing_id = s.listing_id
            GROUP BY weapon_name
        )
        SELECT
            weapon_name,
            skin_variant_count,
            listing_count,
            sticker_count,
            listings_with_stickers,
            ROUND(CAST(sticker_count AS REAL) / NULLIF(listing_count, 0), 3) AS avg_stickers_per_listing,
            ROUND(100.0 * listings_with_stickers / NULLIF(listing_count, 0), 2) AS stickered_listing_percent
        FROM per_weapon
        {where}
        ORDER BY
            sticker_count {order_direction},
            listing_count {order_direction},
            weapon_name ASC
        LIMIT ?
    """

    conn.row_factory = sqlite3.Row
    return conn.execute(query, (limit,)).fetchall()


def print_table(title, rows):
    print()
    print(title)
    print("-" * len(title))

    for index, row in enumerate(rows, start=1):
        print(
            f"{index:3}. {row['skin_name']} | "
            f"stickers={row['sticker_count']} | "
            f"listings={row['listing_count']} | "
            f"with_stickers={row['listings_with_stickers']} | "
            f"avg={row['avg_stickers_per_listing']} | "
            f"stickered={row['stickered_listing_percent']}%"
        )


def print_weapon_table(title, rows):
    print()
    print(title)
    print("-" * len(title))

    for index, row in enumerate(rows, start=1):
        print(
            f"{index:3}. {row['weapon_name']} | "
            f"stickers={row['sticker_count']} | "
            f"listings={row['listing_count']} | "
            f"skins={row['skin_variant_count']} | "
            f"with_stickers={row['listings_with_stickers']} | "
            f"avg={row['avg_stickers_per_listing']} | "
            f"stickered={row['stickered_listing_percent']}%"
        )


def write_csv(path, rows):
    if not rows:
        return

    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=pick_default_db())
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--exclude-zero", action="store_true")
    parser.add_argument("--csv-prefix", default="sticker_count")
    args = parser.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"DB file not found: {args.db}")

    with sqlite3.connect(args.db) as conn:
        most_rows = fetch_rows(conn, "DESC", args.limit, args.exclude_zero)
        least_rows = fetch_rows(conn, "ASC", args.limit, args.exclude_zero)
        weapon_most_rows = fetch_weapon_rows(conn, "DESC", args.limit, args.exclude_zero)
        weapon_least_rows = fetch_weapon_rows(conn, "ASC", args.limit, args.exclude_zero)

    print(f"DB: {args.db}")
    print_table(f"TOP {args.limit} MOST STICKERS", most_rows)
    print_table(f"TOP {args.limit} LEAST STICKERS", least_rows)
    print_weapon_table(f"TOP {args.limit} WEAPON TYPES MOST STICKERS", weapon_most_rows)
    print_weapon_table(f"TOP {args.limit} WEAPON TYPES LEAST STICKERS", weapon_least_rows)

    most_csv = f"{args.csv_prefix}_most_{args.limit}.csv"
    least_csv = f"{args.csv_prefix}_least_{args.limit}.csv"
    weapon_most_csv = f"{args.csv_prefix}_weapon_most_{args.limit}.csv"
    weapon_least_csv = f"{args.csv_prefix}_weapon_least_{args.limit}.csv"

    write_csv(most_csv, most_rows)
    write_csv(least_csv, least_rows)
    write_csv(weapon_most_csv, weapon_most_rows)
    write_csv(weapon_least_csv, weapon_least_rows)

    print()
    print(f"Saved: {most_csv}")
    print(f"Saved: {least_csv}")
    print(f"Saved: {weapon_most_csv}")
    print(f"Saved: {weapon_least_csv}")


if __name__ == "__main__":
    main()
