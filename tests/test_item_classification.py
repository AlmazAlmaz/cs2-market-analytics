import sqlite3

import pytest

import item_classification as kinds


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("★ Karambit | Doppler (Factory New)", "knife"),
        ("StatTrak™ ★ Butterfly Knife | Fade (Factory New)", "knife"),
        ("★ Sport Gloves | Vice (Field-Tested)", "glove"),
        ("AK-47 | Redline (Field-Tested)", "weapon_skin"),
        ("StatTrak™ AK-47 | Redline (Field-Tested)", "weapon_skin"),
        ("Sticker | Crown (Foil)", "unknown"),
        ("Revolution Case", "unknown"),
        (None, "unknown"),
    ],
)
def test_fallback_classification(name, expected):
    assert kinds.classify_name_fallback(name) == expected


def test_catalog_is_authoritative_over_name_fallback():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE cs2_item_catalog (
            market_hash_name TEXT PRIMARY KEY,
            item_kind TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO cs2_item_catalog VALUES (?, ?)",
        [
            ("Catalog Knife Without Star", "knife"),
            ("★ Catalog Override", "weapon_skin"),
        ],
    )

    assert (
        kinds.classify_market_hash_name("Catalog Knife Without Star", conn=conn)
        == "knife"
    )
    assert (
        kinds.classify_market_hash_name("★ Catalog Override", conn=conn)
        == "weapon_skin"
    )
    assert kinds.names_for_item_kind(conn, "knife") == {"Catalog Knife Without Star"}


def test_missing_catalog_falls_back_without_mutating_database():
    conn = sqlite3.connect(":memory:")
    assert (
        kinds.classify_market_hash_name(
            "★ M9 Bayonet | Gamma Doppler (Factory New)", conn=conn
        )
        == "knife"
    )
    assert kinds.names_for_item_kind(conn, "knife") == set()
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'cs2_item_catalog'"
    ).fetchone()


def test_sql_expression_matches_python_fallback():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE samples(name TEXT)")
    names = [
        "★ Karambit | Fade (Factory New)",
        "StatTrak™ ★ Bayonet | Doppler (Factory New)",
        "★ Driver Gloves | King Snake (Minimal Wear)",
        "M4A1-S | Printstream (Field-Tested)",
        "StatTrak™ AK-47 | Redline (Field-Tested)",
        "Sticker | Crown (Foil)",
    ]
    conn.executemany("INSERT INTO samples VALUES (?)", [(name,) for name in names])
    expression, parameters = kinds.sql_item_kind_expression(
        conn, name_expression="samples.name"
    )
    rows = conn.execute(
        f"SELECT name, {expression} FROM samples ORDER BY rowid", parameters
    ).fetchall()
    assert rows == [
        (name, kinds.classify_name_fallback(name)) for name in names
    ]


def test_sql_expression_uses_catalog_and_rejects_unsafe_identifier():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE samples(name TEXT)")
    conn.execute("CREATE TABLE cs2_item_catalog(market_hash_name TEXT, item_kind TEXT)")
    conn.execute("INSERT INTO samples VALUES ('Odd Catalog Name')")
    conn.execute("INSERT INTO cs2_item_catalog VALUES ('Odd Catalog Name', 'knife')")
    expression, parameters = kinds.sql_item_kind_expression(conn)
    assert conn.execute(
        f"SELECT {expression} FROM samples", parameters
    ).fetchone()[0] == "knife"
    with pytest.raises(ValueError):
        kinds.sql_item_kind_expression(conn, name_expression="name); DROP TABLE samples")
