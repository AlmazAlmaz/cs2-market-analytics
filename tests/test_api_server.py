import sqlite3

import api_server


def create_export_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE sales (
                listing_id TEXT PRIMARY KEY,
                name TEXT,
                base_skin_name TEXT,
                wear_name TEXT,
                price_cents INTEGER,
                float_value REAL,
                paint_seed INTEGER,
                paint_index INTEGER,
                inspect_link TEXT,
                serialized_inspect TEXT,
                state TEXT,
                created_at TEXT,
                sold_at TEXT,
                fetched_at TEXT
            );
            CREATE INDEX idx_sales_fetched_listing
            ON sales(fetched_at, listing_id);
            CREATE TABLE sale_stickers (
                listing_id TEXT,
                sticker_index INTEGER,
                sticker_id INTEGER,
                slot INTEGER,
                name TEXT,
                wear REAL,
                price_cents INTEGER,
                PRIMARY KEY (listing_id, sticker_index)
            );
        """)
        conn.executemany(
            """
                INSERT INTO sales (
                    listing_id, name, price_cents, sold_at, fetched_at
                ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                ("a", "Skin A", 100, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
                ("b", "Skin B", 200, "2026-01-01T00:00:01Z", "2026-01-02T00:00:00Z"),
                ("c", "Skin C", 300, "2026-01-01T00:00:02Z", "2026-01-03T00:00:00Z"),
            ),
        )
        conn.execute("""
            INSERT INTO sale_stickers (
                listing_id, sticker_index, sticker_id, slot, name, wear, price_cents
            ) VALUES ('b', 0, 7, 1, 'Sticker B', 0.1, 25)
        """)


def test_sales_export_uses_stable_composite_cursor(tmp_path, monkeypatch):
    db_path = tmp_path / "export.db"
    create_export_db(db_path)
    monkeypatch.setattr(api_server, "DB_PATH", str(db_path))

    first = api_server.get_sales("0000-01-01T00:00:00Z", "", 2)
    assert [row["listing_id"] for row in first["data"]] == ["a", "b"]
    assert first["data"][1]["stickers"][0]["name"] == "Sticker B"

    cursor = first["next_cursor"]
    second = api_server.get_sales(
        cursor["after_fetched_at"],
        cursor["after_listing_id"],
        2,
    )
    assert [row["listing_id"] for row in second["data"]] == ["c"]

    final_cursor = second["next_cursor"]
    empty = api_server.get_sales(
        final_cursor["after_fetched_at"],
        final_cursor["after_listing_id"],
        2,
    )
    assert empty["data"] == []
    assert empty["next_cursor"] == final_cursor


def test_composite_cursor_query_uses_range_seek(tmp_path):
    db_path = tmp_path / "plan.db"
    create_export_db(db_path)

    with sqlite3.connect(db_path) as conn:
        plan = " ".join(
            str(row[3])
            for row in conn.execute("""
                EXPLAIN QUERY PLAN
                SELECT listing_id
                FROM sales
                WHERE (fetched_at, listing_id) > (?, ?)
                ORDER BY fetched_at, listing_id
                LIMIT ?
            """, ("2026-01-02T00:00:00Z", "a", 10))
        )

    assert "idx_sales_fetched_listing" in plan
    assert "(fetched_at,listing_id)>(?,?)" in plan.replace(" ", "")
