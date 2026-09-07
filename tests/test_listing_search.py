import sqlite3

import pytest

import listing_search


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sales (
            listing_id TEXT PRIMARY KEY,
            name TEXT,
            price_cents INTEGER,
            float_value REAL,
            paint_seed INTEGER,
            paint_index INTEGER,
            inspect_link TEXT,
            serialized_inspect TEXT,
            sold_at TEXT,
            fetched_at TEXT,
            item_icon_url TEXT,
            item_steam_market_url TEXT,
            item_rarity_color TEXT,
            item_name_color TEXT
        );
        CREATE INDEX idx_sales_name ON sales(name);
        CREATE INDEX idx_sales_fetched_listing ON sales(fetched_at, listing_id);

        CREATE TABLE sale_stickers (
            listing_id TEXT,
            sticker_index INTEGER,
            sticker_id INTEGER,
            slot INTEGER,
            name TEXT,
            icon_url TEXT,
            steam_market_url TEXT,
            wear REAL,
            price_cents INTEGER,
            PRIMARY KEY (listing_id, sticker_index)
        );
        CREATE INDEX idx_sale_stickers_name ON sale_stickers(name);
        CREATE INDEX idx_sale_stickers_listing_id ON sale_stickers(listing_id);

        CREATE TABLE cs2_item_catalog (
            market_hash_name TEXT PRIMARY KEY,
            weapon_name TEXT,
            item_kind TEXT
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO sales (
            listing_id, name, price_cents, float_value, paint_seed,
            paint_index, inspect_link, serialized_inspect, sold_at,
            fetched_at, item_icon_url, item_steam_market_url,
            item_rarity_color, item_name_color
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "1001",
                "AK-47 | Slate (Field-Tested)",
                900,
                0.2,
                1,
                1035,
                "steam://inspect/1001",
                "inspect-1001",
                "2026-01-02T00:00:00Z",
                "2026-01-03T00:00:00Z",
                "https://example.test/ak.png",
                "https://steam.test/ak",
                "4b69ff",
                "abcdef",
            ),
            (
                "1002",
                "StatTrak™ AK-47 | Redline (Field-Tested)",
                1_500,
                0.25,
                2,
                282,
                None,
                None,
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
                None,
                None,
                None,
                None,
            ),
            (
                "1003",
                "MAC-10 | Neon Rider (Minimal Wear)",
                700,
                0.1,
                3,
                433,
                None,
                None,
                "2026-01-03T00:00:00Z",
                "2026-01-04T00:00:00Z",
                None,
                None,
                None,
                None,
            ),
            (
                "2001",
                "M4A1-S | Guardian (Factory New)",
                2_000,
                0.02,
                4,
                257,
                None,
                None,
                "2026-01-04T00:00:00Z",
                "2026-01-05T00:00:00Z",
                None,
                None,
                None,
                None,
            ),
            (
                "2002",
                "Glock-18 | Crowned (Factory New)",
                1_000,
                0.01,
                5,
                999,
                None,
                None,
                "2025-12-31T00:00:00Z",
                "2026-01-01T00:00:00Z",
                None,
                None,
                None,
                None,
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO cs2_item_catalog VALUES (?, ?, ?)",
        [
            ("AK-47 | Slate (Field-Tested)", "AK-47", "weapon_skin"),
            (
                "StatTrak™ AK-47 | Redline (Field-Tested)",
                "AK-47",
                "weapon_skin",
            ),
            ("AK-47 | Catalog Only (Factory New)", "AK-47", "weapon_skin"),
            ("MAC-10 | Neon Rider (Minimal Wear)", "MAC-10", "weapon_skin"),
            ("M4A1-S | Guardian (Factory New)", "M4A1-S", "weapon_skin"),
            ("Glock-18 | Crowned (Factory New)", "Glock-18", "weapon_skin"),
        ],
    )
    conn.executemany(
        "INSERT INTO sale_stickers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "2001",
                0,
                37,
                0,
                "Sticker | Crown (Foil)",
                "https://example.test/crown.png",
                "https://steam.test/crown",
                0.0,
                90_000,
            ),
            ("2001", 1, 37, 1, "Sticker | Crown (Foil)", None, None, 0.01, 90_000),
            ("1001", 0, 10, 0, "Sticker | High Heat", None, None, 0.0, 50),
        ],
    )
    return conn


@pytest.mark.parametrize("query", ["AK", "ak47", "ак", "ак47"])
def test_skin_scope_prioritizes_authoritative_weapon_aliases(query):
    conn = _connection()

    payload = listing_search.fetch_listings_page(conn, 0, 20, query, "skin")

    assert payload["total"] == 2
    assert payload["resolved"]["skin_name_count"] == 3
    assert [row["listing_id"] for row in payload["rows"]] == ["1001", "1002"]
    assert payload["rows"][0]["stickers"][0]["name"] == "Sticker | High Heat"


@pytest.mark.parametrize("query", ["MAC", "MAC 10", "mac10", "мак", "мак10"])
def test_skin_scope_normalizes_mac10_aliases(query):
    conn = _connection()

    payload = listing_search.fetch_listings_page(conn, 0, 20, query, "skin")

    assert payload["total"] == 1
    assert payload["rows"][0]["name"] == "MAC-10 | Neon Rider (Minimal Wear)"


def test_skin_scope_uses_catalog_contains_fallback_and_keeps_optional_fields():
    conn = _connection()

    payload = listing_search.fetch_listings_page(conn, 0, 10, "slate", "skin")

    assert payload["total"] == 1
    row = payload["rows"][0]
    assert row["listing_id"] == "1001"
    assert row["price_cents"] == 900
    assert row["item_icon_url"] == "https://example.test/ak.png"
    assert row["stickers"][0]["sticker_id"] == 10


def test_sticker_scope_resolves_names_once_and_deduplicates_listing():
    conn = _connection()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    payload = listing_search.fetch_listings_page(conn, 0, 20, "crown", "sticker")
    conn.set_trace_callback(None)

    assert payload["total"] == 1
    assert payload["resolved"]["sticker_name_count"] == 1
    assert [row["listing_id"] for row in payload["rows"]] == ["2001"]
    assert len(payload["rows"][0]["stickers"]) == 2
    search_statements = "\n".join(
        statement for statement in statements if "listing_search:" in statement
    )
    assert "LIKE" not in search_statements.upper()
    assert "EXISTS" not in search_statements.upper()


def test_listing_scope_supports_exact_then_primary_key_prefix():
    conn = _connection()

    exact = listing_search.fetch_listings_page(conn, 0, 20, "1001", "listing")
    prefix = listing_search.fetch_listings_page(conn, 0, 20, "100", "listing")

    assert exact["total"] == 1
    assert exact["resolved"]["listing_match_mode"] == "exact"
    assert [row["listing_id"] for row in exact["rows"]] == ["1001"]
    assert prefix["total"] == 3
    assert prefix["resolved"]["listing_match_mode"] == "prefix"
    assert [row["listing_id"] for row in prefix["rows"]] == [
        "1003",
        "1001",
        "1002",
    ]


def test_all_scope_unions_skin_sticker_and_listing_without_duplicates():
    conn = _connection()

    payload = listing_search.fetch_listings_page(conn, 0, 20, "crown", "all")

    assert payload["total"] == 2
    assert payload["resolved"]["skin_name_count"] == 1
    assert payload["resolved"]["sticker_name_count"] == 1
    assert [row["listing_id"] for row in payload["rows"]] == ["2001", "2002"]


def test_blank_search_uses_recent_index_order_and_ignores_scope():
    conn = _connection()

    payload = listing_search.fetch_listings_page(conn, 1, 2, "", "sticker")

    assert payload["total"] == 5
    assert payload["strategy"] == "recent_index_scan"
    assert [row["listing_id"] for row in payload["rows"]] == ["1003", "1001"]


def test_reduced_schema_returns_null_optional_fields_and_empty_stickers():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE sales (listing_id TEXT PRIMARY KEY, name TEXT, fetched_at TEXT)"
    )
    conn.execute("CREATE INDEX idx_sales_name ON sales(name)")
    conn.execute(
        "CREATE INDEX idx_sales_fetched_listing ON sales(fetched_at, listing_id)"
    )
    conn.execute("INSERT INTO sales VALUES ('x1', 'Fallback Skin', '2026-01-01')")

    payload = listing_search.fetch_listings_page(conn, 0, 10, "fallback", "skin")

    assert payload["total"] == 1
    row = payload["rows"][0]
    assert row["listing_id"] == "x1"
    assert row["price_cents"] is None
    assert row["paint_index"] is None
    assert row["stickers"] == []


def test_missing_sales_schema_fails_closed():
    conn = sqlite3.connect(":memory:")

    payload = listing_search.fetch_listings_page(conn, 0, 10, "AK", "skin")

    assert payload["total"] == 0
    assert payload["rows"] == []
    assert payload["strategy"] == "empty"


def test_skin_scope_fails_closed_when_legacy_sales_has_no_name_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sales (listing_id TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE cs2_item_catalog "
        "(market_hash_name TEXT PRIMARY KEY, weapon_name TEXT)"
    )
    conn.execute("INSERT INTO sales VALUES ('x1')")
    conn.execute("INSERT INTO cs2_item_catalog VALUES ('AK-47 | Slate', 'AK-47')")

    payload = listing_search.fetch_listings_page(conn, 0, 10, "AK", "skin")

    assert payload["total"] == 0
    assert payload["rows"] == []


@pytest.mark.parametrize(
    ("offset", "limit", "search", "scope"),
    [
        (-1, 10, "AK", "skin"),
        (listing_search.MAX_OFFSET + 1, 10, "AK", "skin"),
        (True, 10, "AK", "skin"),
        (0, 0, "AK", "skin"),
        (0, listing_search.MAX_LIMIT + 1, "AK", "skin"),
        (0, True, "AK", "skin"),
        (0, 10, "x", "skin"),
        (0, 10, "%", "skin"),
        (0, 10, "AK\x00", "skin"),
        (0, 10, "AK", "unknown"),
    ],
)
def test_request_bounds_are_api_friendly(offset, limit, search, scope):
    conn = _connection()

    with pytest.raises(listing_search.ListingSearchError):
        listing_search.fetch_listings_page(conn, offset, limit, search, scope)


def test_listing_scope_rejects_non_identifier_text():
    conn = _connection()

    with pytest.raises(listing_search.ListingSearchError, match="listing search"):
        listing_search.fetch_listings_page(conn, 0, 10, "100 1", "listing")


def test_all_scope_rejects_broad_resolution_before_union(monkeypatch):
    conn = _connection()
    monkeypatch.setattr(listing_search, "MAX_ALL_RESOLVED_NAMES", 1)

    with pytest.raises(listing_search.SearchTooBroadError, match="all-scope"):
        listing_search.fetch_listings_page(conn, 0, 10, "crown", "all")


def test_broad_skin_search_uses_indexes_with_bounded_vm_work():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE sales (
            listing_id TEXT PRIMARY KEY,
            name TEXT,
            fetched_at TEXT,
            sold_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE cs2_item_catalog (
            market_hash_name TEXT PRIMARY KEY,
            weapon_name TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO cs2_item_catalog VALUES ('AK-47 | Fast (Field-Tested)', 'AK-47')"
    )
    rows = []
    for index in range(20_000):
        is_ak = index >= 13_900
        rows.append(
            (
                f"{index:020d}",
                "AK-47 | Fast (Field-Tested)" if is_ak else "Other Skin",
                f"2026-01-{index:08d}",
                f"2025-12-{index:08d}",
            )
        )
    conn.executemany("INSERT INTO sales VALUES (?, ?, ?, ?)", rows)
    conn.execute("CREATE INDEX idx_sales_name ON sales(name)")
    conn.execute(
        "CREATE INDEX idx_sales_fetched_listing ON sales(fetched_at, listing_id)"
    )

    traced: list[str] = []
    vm_steps = 0

    def progress() -> int:
        nonlocal vm_steps
        vm_steps += 100
        return int(vm_steps > 300_000)

    conn.set_trace_callback(traced.append)
    conn.set_progress_handler(progress, 100)
    payload = listing_search.fetch_listings_page(conn, 0, 50, "AK", "skin")
    conn.set_progress_handler(None, 0)
    conn.set_trace_callback(None)

    assert payload["total"] == 6_100
    assert payload["strategy"] == "recent_index_scan"
    assert len(payload["rows"]) == 50
    assert vm_steps <= 300_000

    count_sql = next(
        statement
        for statement in traced
        if "listing_search:count:skin" in statement
    )
    page_sql = next(
        statement
        for statement in traced
        if "listing_search:page:skin:broad" in statement
    )
    count_plan = "\n".join(
        str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {count_sql}")
    )
    page_plan = "\n".join(
        str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {page_sql}")
    )
    assert "idx_sales_name" in count_plan
    assert "idx_sales_fetched_listing" in page_plan
    assert "CORRELATED" not in count_plan + page_plan
    assert "USE TEMP B-TREE FOR ORDER BY" not in page_plan
