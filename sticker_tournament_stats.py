import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

import app_config

SOURCE_DB_CANDIDATES = [
    str(app_config.path("CS2_DB_PATH", "csfloat_history.db")),
    str(app_config.path("API_DB_PATH", "csfloat_sales_history.db")),
    str(app_config.PROJECT_DIR / "cs_float.db"),
]
DEFAULT_STATS_DB = str(app_config.path("CS2_STATS_DB_PATH", "sticker_tournament_stats.db"))

STICKER_TYPES = [
    "Holo",
    "Foil",
    "Gold",
    "Glitter",
    "Lenticular",
    "Paper",
]


def pick_source_db():
    for db_path in SOURCE_DB_CANDIDATES:
        if Path(db_path).exists():
            return db_path

    return SOURCE_DB_CANDIDATES[0]


def connect_source(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def extract_tournament(sticker_name):
    if not sticker_name:
        return "Unknown"

    parts = [part.strip() for part in sticker_name.split(" | ")]
    if len(parts) >= 3 and parts[-1]:
        return parts[-1]

    return "No Tournament"


def extract_sticker_label(sticker_name):
    if not sticker_name:
        return "Unknown"

    parts = [part.strip() for part in sticker_name.split(" | ")]
    if len(parts) >= 3:
        return parts[1]
    if len(parts) >= 2:
        return parts[-1]
    return sticker_name.strip()


def extract_sticker_type(sticker_name):
    if not sticker_name:
        return "Unknown"

    lowered = sticker_name.lower()
    for sticker_type in STICKER_TYPES:
        if f"({sticker_type.lower()})" in lowered:
            return sticker_type

    return "Regular"


def weapon_from_skin(base_skin_name, skin_name):
    name = base_skin_name or skin_name or "Unknown"
    if " | " in name:
        return name.split(" | ", 1)[0].strip()
    return name.strip()


def dollars(cents):
    if cents is None:
        cents = 0
    return f"${cents / 100:.2f}"


def average(total, count):
    return total / count if count else 0


def fetch_stickers(source_conn):
    return source_conn.execute("""
        SELECT
            st.listing_id,
            st.sticker_id,
            st.name AS sticker_name,
            st.slot,
            st.wear,
            st.price_cents AS sticker_price_cents,
            s.name AS skin_name,
            s.base_skin_name,
            s.wear_name,
            s.sold_at,
            s.price_cents AS skin_price_cents
        FROM sale_stickers st
        JOIN sales s ON s.listing_id = st.listing_id
        WHERE st.name IS NOT NULL
    """).fetchall()


def new_price_bucket():
    return {
        "sticker_count": 0,
        "listing_ids": set(),
        "skin_names": set(),
        "sticker_ids": set(),
        "sticker_price_sum": 0,
        "sticker_price_count": 0,
        "max_sticker_price": 0,
        "skin_price_sum": 0,
        "skin_price_count": 0,
    }


def build_stats(rows):
    tournament_stats = defaultdict(new_price_bucket)
    sticker_stats = defaultdict(new_price_bucket)
    weapon_tournament_stats = defaultdict(new_price_bucket)
    type_tournament_stats = defaultdict(new_price_bucket)

    sticker_meta = {}

    for row in rows:
        sticker_name = row["sticker_name"] or "Unknown"
        tournament = extract_tournament(sticker_name)
        sticker_label = extract_sticker_label(sticker_name)
        sticker_type = extract_sticker_type(sticker_name)
        sticker_id = row["sticker_id"]
        listing_id = row["listing_id"]
        skin_name = row["skin_name"] or "Unknown"
        sticker_price = row["sticker_price_cents"] or 0
        skin_price = row["skin_price_cents"] or 0
        weapon_name = weapon_from_skin(row["base_skin_name"], row["skin_name"])

        sticker_key = (sticker_id, sticker_name)
        sticker_meta[sticker_key] = {
            "tournament": tournament,
            "sticker_label": sticker_label,
            "sticker_type": sticker_type,
        }

        add_row(tournament_stats[tournament], listing_id, skin_name, sticker_id, sticker_price, skin_price)
        add_row(sticker_stats[sticker_key], listing_id, skin_name, sticker_id, sticker_price, skin_price)
        add_row(weapon_tournament_stats[(weapon_name, tournament)], listing_id, skin_name, sticker_id, sticker_price, skin_price)
        add_row(type_tournament_stats[(tournament, sticker_type)], listing_id, skin_name, sticker_id, sticker_price, skin_price)

    return tournament_stats, sticker_stats, weapon_tournament_stats, type_tournament_stats, sticker_meta


def add_row(bucket, listing_id, skin_name, sticker_id, sticker_price, skin_price):
    bucket["sticker_count"] += 1
    bucket["listing_ids"].add(listing_id)
    bucket["skin_names"].add(skin_name)
    bucket["sticker_ids"].add(sticker_id)
    bucket["sticker_price_sum"] += sticker_price
    bucket["sticker_price_count"] += 1 if sticker_price else 0
    bucket["max_sticker_price"] = max(bucket["max_sticker_price"], sticker_price)
    bucket["skin_price_sum"] += skin_price
    bucket["skin_price_count"] += 1 if skin_price else 0


def create_stats_tables(conn):
    conn.execute("DROP TABLE IF EXISTS tournament_stats")
    conn.execute("DROP TABLE IF EXISTS tournament_sticker_stats")
    conn.execute("DROP TABLE IF EXISTS weapon_tournament_stats")
    conn.execute("DROP TABLE IF EXISTS tournament_type_stats")
    conn.execute("DROP TABLE IF EXISTS meta")

    conn.execute("""
        CREATE TABLE tournament_stats (
            tournament TEXT PRIMARY KEY,
            sticker_count INTEGER NOT NULL,
            listing_count INTEGER NOT NULL,
            skin_count INTEGER NOT NULL,
            sticker_id_count INTEGER NOT NULL,
            total_sticker_price_cents INTEGER NOT NULL,
            avg_sticker_price_cents REAL NOT NULL,
            max_sticker_price_cents INTEGER NOT NULL,
            avg_skin_price_cents REAL NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE tournament_sticker_stats (
            tournament TEXT NOT NULL,
            sticker_id INTEGER,
            sticker_name TEXT NOT NULL,
            sticker_label TEXT NOT NULL,
            sticker_type TEXT NOT NULL,
            sticker_count INTEGER NOT NULL,
            listing_count INTEGER NOT NULL,
            skin_count INTEGER NOT NULL,
            total_sticker_price_cents INTEGER NOT NULL,
            avg_sticker_price_cents REAL NOT NULL,
            max_sticker_price_cents INTEGER NOT NULL,
            avg_skin_price_cents REAL NOT NULL,
            PRIMARY KEY (tournament, sticker_id, sticker_name)
        )
    """)

    conn.execute("""
        CREATE TABLE weapon_tournament_stats (
            weapon_name TEXT NOT NULL,
            tournament TEXT NOT NULL,
            sticker_count INTEGER NOT NULL,
            listing_count INTEGER NOT NULL,
            skin_count INTEGER NOT NULL,
            sticker_id_count INTEGER NOT NULL,
            avg_stickers_per_listing REAL NOT NULL,
            PRIMARY KEY (weapon_name, tournament)
        )
    """)

    conn.execute("""
        CREATE TABLE tournament_type_stats (
            tournament TEXT NOT NULL,
            sticker_type TEXT NOT NULL,
            sticker_count INTEGER NOT NULL,
            listing_count INTEGER NOT NULL,
            skin_count INTEGER NOT NULL,
            sticker_id_count INTEGER NOT NULL,
            avg_stickers_per_listing REAL NOT NULL,
            PRIMARY KEY (tournament, sticker_type)
        )
    """)

    conn.execute("""
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    conn.execute("CREATE INDEX idx_tournament_sticker_count ON tournament_sticker_stats (tournament, sticker_count DESC)")
    conn.execute("CREATE INDEX idx_weapon_tournament_count ON weapon_tournament_stats (sticker_count DESC)")
    conn.commit()


def bucket_to_common_values(bucket):
    return (
        bucket["sticker_count"],
        len(bucket["listing_ids"]),
        len(bucket["skin_names"]),
        len(bucket["sticker_ids"]),
        bucket["sticker_price_sum"],
        average(bucket["sticker_price_sum"], bucket["sticker_price_count"]),
        bucket["max_sticker_price"],
        average(bucket["skin_price_sum"], bucket["skin_price_count"]),
    )


def save_stats(conn, source_db, rows_count, tournament_stats, sticker_stats, weapon_tournament_stats, type_tournament_stats, sticker_meta):
    tournament_rows = []
    for tournament, bucket in tournament_stats.items():
        tournament_rows.append((tournament, *bucket_to_common_values(bucket)))

    conn.executemany("""
        INSERT INTO tournament_stats (
            tournament,
            sticker_count,
            listing_count,
            skin_count,
            sticker_id_count,
            total_sticker_price_cents,
            avg_sticker_price_cents,
            max_sticker_price_cents,
            avg_skin_price_cents
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, tournament_rows)

    sticker_rows = []
    for (sticker_id, sticker_name), bucket in sticker_stats.items():
        meta = sticker_meta[(sticker_id, sticker_name)]
        sticker_rows.append((
            meta["tournament"],
            sticker_id,
            sticker_name,
            meta["sticker_label"],
            meta["sticker_type"],
            bucket["sticker_count"],
            len(bucket["listing_ids"]),
            len(bucket["skin_names"]),
            bucket["sticker_price_sum"],
            average(bucket["sticker_price_sum"], bucket["sticker_price_count"]),
            bucket["max_sticker_price"],
            average(bucket["skin_price_sum"], bucket["skin_price_count"]),
        ))

    conn.executemany("""
        INSERT INTO tournament_sticker_stats (
            tournament,
            sticker_id,
            sticker_name,
            sticker_label,
            sticker_type,
            sticker_count,
            listing_count,
            skin_count,
            total_sticker_price_cents,
            avg_sticker_price_cents,
            max_sticker_price_cents,
            avg_skin_price_cents
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, sticker_rows)

    weapon_rows = []
    for (weapon_name, tournament), bucket in weapon_tournament_stats.items():
        listing_count = len(bucket["listing_ids"])
        weapon_rows.append((
            weapon_name,
            tournament,
            bucket["sticker_count"],
            listing_count,
            len(bucket["skin_names"]),
            len(bucket["sticker_ids"]),
            average(bucket["sticker_count"], listing_count),
        ))

    conn.executemany("""
        INSERT INTO weapon_tournament_stats (
            weapon_name,
            tournament,
            sticker_count,
            listing_count,
            skin_count,
            sticker_id_count,
            avg_stickers_per_listing
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, weapon_rows)

    type_rows = []
    for (tournament, sticker_type), bucket in type_tournament_stats.items():
        listing_count = len(bucket["listing_ids"])
        type_rows.append((
            tournament,
            sticker_type,
            bucket["sticker_count"],
            listing_count,
            len(bucket["skin_names"]),
            len(bucket["sticker_ids"]),
            average(bucket["sticker_count"], listing_count),
        ))

    conn.executemany("""
        INSERT INTO tournament_type_stats (
            tournament,
            sticker_type,
            sticker_count,
            listing_count,
            skin_count,
            sticker_id_count,
            avg_stickers_per_listing
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, type_rows)

    conn.executemany("""
        INSERT INTO meta (key, value)
        VALUES (?, ?)
    """, [
        ("source_db", source_db),
        ("processed_sticker_rows", str(rows_count)),
    ])

    conn.commit()


def print_tournaments(conn, limit):
    print()
    print(f"TOP {limit} TOURNAMENTS")
    print("-" * 80)
    for index, row in enumerate(conn.execute("""
        SELECT
            tournament,
            sticker_count,
            listing_count,
            skin_count,
            sticker_id_count,
            avg_sticker_price_cents,
            max_sticker_price_cents
        FROM tournament_stats
        ORDER BY sticker_count DESC, listing_count DESC
        LIMIT ?
    """, (limit,)), start=1):
        print(
            f"{index:3}. {row['tournament']} | stickers={row['sticker_count']} | "
            f"listings={row['listing_count']} | skins={row['skin_count']} | "
            f"ids={row['sticker_id_count']} | avg_sticker={dollars(row['avg_sticker_price_cents'])} | "
            f"max_sticker={dollars(row['max_sticker_price_cents'])}"
        )


def print_stickers(conn, limit):
    print()
    print(f"TOP {limit} STICKERS OVERALL")
    print("-" * 80)
    for index, row in enumerate(conn.execute("""
        SELECT
            sticker_id,
            sticker_name,
            tournament,
            sticker_type,
            sticker_count,
            listing_count,
            skin_count,
            avg_sticker_price_cents
        FROM tournament_sticker_stats
        ORDER BY sticker_count DESC, listing_count DESC
        LIMIT ?
    """, (limit,)), start=1):
        print(
            f"{index:3}. id={row['sticker_id']} | {row['sticker_name']} | "
            f"{row['tournament']} | {row['sticker_type']} | stickers={row['sticker_count']} | "
            f"listings={row['listing_count']} | skins={row['skin_count']} | "
            f"avg_sticker={dollars(row['avg_sticker_price_cents'])}"
        )


def print_top_stickers_inside_tournaments(conn, tournament_limit, per_tournament_limit):
    print()
    print(f"TOP {per_tournament_limit} STICKERS INSIDE TOP {tournament_limit} TOURNAMENTS")
    print("-" * 80)

    tournaments = conn.execute("""
        SELECT tournament
        FROM tournament_stats
        ORDER BY sticker_count DESC, listing_count DESC
        LIMIT ?
    """, (tournament_limit,)).fetchall()

    for tournament_row in tournaments:
        tournament = tournament_row["tournament"]
        print()
        print(tournament)
        rows = conn.execute("""
            SELECT
                sticker_id,
                sticker_name,
                sticker_type,
                sticker_count,
                listing_count,
                skin_count,
                avg_sticker_price_cents
            FROM tournament_sticker_stats
            WHERE tournament = ?
            ORDER BY sticker_count DESC, listing_count DESC
            LIMIT ?
        """, (tournament, per_tournament_limit)).fetchall()

        for index, row in enumerate(rows, start=1):
            print(
                f"  {index:2}. id={row['sticker_id']} | {row['sticker_name']} | "
                f"{row['sticker_type']} | stickers={row['sticker_count']} | "
                f"listings={row['listing_count']} | skins={row['skin_count']} | "
                f"avg={dollars(row['avg_sticker_price_cents'])}"
            )


def print_weapon_tournaments(conn, limit):
    print()
    print(f"TOP {limit} WEAPON + TOURNAMENT")
    print("-" * 80)
    for index, row in enumerate(conn.execute("""
        SELECT
            weapon_name,
            tournament,
            sticker_count,
            listing_count,
            skin_count,
            sticker_id_count,
            avg_stickers_per_listing
        FROM weapon_tournament_stats
        ORDER BY sticker_count DESC, listing_count DESC
        LIMIT ?
    """, (limit,)), start=1):
        print(
            f"{index:3}. {row['weapon_name']} + {row['tournament']} | "
            f"stickers={row['sticker_count']} | listings={row['listing_count']} | "
            f"skins={row['skin_count']} | ids={row['sticker_id_count']} | "
            f"avg_per_listing={row['avg_stickers_per_listing']:.3f}"
        )


def print_type_tournaments(conn, limit):
    print()
    print(f"TOP {limit} TOURNAMENT + STICKER TYPE")
    print("-" * 80)
    for index, row in enumerate(conn.execute("""
        SELECT
            tournament,
            sticker_type,
            sticker_count,
            listing_count,
            skin_count,
            sticker_id_count,
            avg_stickers_per_listing
        FROM tournament_type_stats
        ORDER BY sticker_count DESC, listing_count DESC
        LIMIT ?
    """, (limit,)), start=1):
        print(
            f"{index:3}. {row['tournament']} + {row['sticker_type']} | "
            f"stickers={row['sticker_count']} | listings={row['listing_count']} | "
            f"skins={row['skin_count']} | ids={row['sticker_id_count']} | "
            f"avg_per_listing={row['avg_stickers_per_listing']:.3f}"
        )


def print_report(conn, limit, per_tournament_limit):
    conn.row_factory = sqlite3.Row
    print_tournaments(conn, limit)
    print_stickers(conn, limit)
    print_top_stickers_inside_tournaments(conn, min(limit, 20), per_tournament_limit)
    print_weapon_tournaments(conn, limit)
    print_type_tournaments(conn, limit)


def normalize_int(value, name):
    if value < 1:
        raise argparse.ArgumentTypeError(f"{name} must be >= 1")
    return value


def positive_int(value):
    return normalize_int(int(value), "value")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", default=pick_source_db())
    parser.add_argument("--stats-db", default=DEFAULT_STATS_DB)
    parser.add_argument("--limit", type=positive_int, default=100)
    parser.add_argument("--per-tournament-limit", type=positive_int, default=10)
    args = parser.parse_args()

    if not Path(args.source_db).exists():
        raise SystemExit(f"Source DB not found: {args.source_db}")

    with connect_source(args.source_db) as source_conn:
        rows = fetch_stickers(source_conn)

    stats = build_stats(rows)

    with sqlite3.connect(args.stats_db) as stats_conn:
        stats_conn.row_factory = sqlite3.Row
        create_stats_tables(stats_conn)
        save_stats(stats_conn, args.source_db, len(rows), *stats)

        print(f"Source DB: {args.source_db}")
        print(f"Stats DB: {args.stats_db}")
        print(f"Processed sticker rows: {len(rows)}")
        print_report(stats_conn, args.limit, args.per_tournament_limit)


if __name__ == "__main__":
    main()
