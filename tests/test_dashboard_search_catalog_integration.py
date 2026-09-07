import sqlite3

import dashboard_server
import listing_search


def test_weapon_explorer_reads_complete_catalog_without_sales_table(tmp_path):
    main_path = tmp_path / "main.db"
    trend_path = tmp_path / "trend.db"
    connection = sqlite3.connect(main_path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE cs2_item_catalog (
            market_hash_name TEXT PRIMARY KEY,
            item_kind TEXT NOT NULL,
            weapon_name TEXT,
            image_url TEXT,
            collection_names_json TEXT,
            crate_names_json TEXT,
            wear_name TEXT,
            paint_name TEXT,
            stattrak INTEGER,
            souvenir INTEGER
        )
        """
    )
    connection.executemany(
        "INSERT INTO cs2_item_catalog VALUES (?, 'weapon_skin', 'AK-47', ?, '[]', '[]', ?, ?, 0, 0)",
        (
            ("AK-47 | Slate (Factory New)", "https://img.test/slate.png", "Factory New", "Slate"),
            ("AK-47 | Slate (Field-Tested)", None, "Field-Tested", "Slate"),
        ),
    )
    connection.commit()

    trend = sqlite3.connect(trend_path)
    trend.execute(
        """
        CREATE TABLE trend_skin_catalog (
            market_hash_name TEXT PRIMARY KEY,
            observed_sales_count INTEGER,
            icon_url TEXT
        )
        """
    )
    trend.execute(
        """
        CREATE TABLE trend_daily_market (
            market_hash_name TEXT,
            day TEXT,
            mean_price_cents INTEGER,
            median_price_cents INTEGER,
            source_max_fetched_at TEXT
        )
        """
    )
    trend.execute(
        "INSERT INTO trend_skin_catalog VALUES (?, ?, ?)",
        ("AK-47 | Slate (Factory New)", 17, "https://trend.test/slate.png"),
    )
    trend.execute(
        "INSERT INTO trend_daily_market VALUES (?, ?, ?, ?, ?)",
        (
            "AK-47 | Slate (Factory New)",
            "2026-08-24",
            512,
            500,
            "2026-08-24T03:00:00+00:00",
        ),
    )
    trend.commit()
    trend.close()

    rows = dashboard_server.fetch_weapon_skins(
        connection, "AK-47", trend_db_path=trend_path
    )

    assert [row["skin_name"] for row in rows] == [
        "AK-47 | Slate (Factory New)",
        "AK-47 | Slate (Field-Tested)",
    ]
    assert rows[0]["sales_count"] == 17
    assert rows[0]["median_price_cents"] == 500
    assert rows[1]["sales_count"] == 0
    assert rows[1]["median_price_cents"] is None
    assert connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='sales'"
    ).fetchone() is None
    connection.close()


def test_fast_listing_wrapper_exposes_pagination_cap(monkeypatch):
    expected = {"offset": 0, "limit": 50, "total": 0, "rows": []}

    def fake_fetch(connection, offset, limit, search, scope):
        assert connection is sentinel
        assert (offset, limit, search, scope) == (0, 50, "MAC 10", "skin")
        return dict(expected)

    sentinel = object()
    monkeypatch.setattr(listing_search, "fetch_listings_page", fake_fetch)

    payload = dashboard_server.fetch_listings_page_fast(
        sentinel, 0, 50, "MAC 10", "skin"
    )

    assert payload["max_offset"] == listing_search.MAX_OFFSET
    assert payload["rows"] == []


def test_database_browser_is_lazy_scoped_and_does_not_retry_terminal_errors():
    source = dashboard_server.HTML

    assert 'id="dbSearchScope"' in source
    assert "scope=${encodeURIComponent(scope)}" in source
    assert "activeDashboardView === 'system' && !dbBrowserLoaded && !dbBrowserLoading" in source
    assert "dbBrowserLoaded = true;\n        text('dbBrowserInfo', `ошибка:" in source
    assert "dbOffset + dbLimit > maxOffset" in source
    assert "document.getElementById('dbSearchScope').addEventListener('change'" in source
    assert "refreshDbBrowser();" in source
    assert "get('view')" in source


def test_scanner_target_editor_exposes_four_profiles_and_bounded_api():
    source = dashboard_server.HTML

    for profile in ("sticker_guns", "liquid_under_10", "knives", "gloves"):
        assert f"{profile}: {{" in source
    assert source.count("/api/scanner-targets") >= 2
    assert "'X-CS2-Action': 'scanner-targets'" in source
    assert 'id="scannerTargetMode"' in source
    assert 'id="scannerTargetEnabled"' in source
    assert 'id="scannerTargetBody"' in source
    assert "Сканер применит его на границе следующего цикла" in source


def test_catalog_navigation_is_injected_into_knives_workspace():
    russian = dashboard_server.inject_catalog_navigation(
        dashboard_server.knives_dashboard.render_knives_page("ru"), "ru"
    )
    english = dashboard_server.inject_catalog_navigation(
        dashboard_server.knives_dashboard.render_knives_page("en"), "en"
    )

    assert russian.count('data-nav-path="/catalog"') == 1
    assert 'data-i18n="navCatalog"' in russian
    assert 'href="/catalog">Каталог</a>' in russian
    assert english.count('data-nav-path="/catalog"') == 1
    assert 'data-i18n="navCatalog"' in english
    assert 'href="/catalog">Catalogue</a>' in english
