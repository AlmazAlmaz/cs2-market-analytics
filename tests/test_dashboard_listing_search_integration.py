import inspect

import dashboard_server
import listing_search


def _section(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_fast_listing_wrapper_forwards_scope_and_decorates_market_urls(monkeypatch):
    captured = {}

    def fake_fetch(conn, offset, limit, search, scope):
        captured["args"] = (conn, offset, limit, search, scope)
        return {
            "offset": offset,
            "limit": limit,
            "total": 1,
            "rows": [
                {
                    "listing_id": "listing-1",
                    "name": "AK-47 | Slate (Field-Tested)",
                    "item_steam_market_url": None,
                    "stickers": [
                        {
                            "name": "Sticker | Crown (Foil)",
                            "steam_market_url": None,
                        }
                    ],
                }
            ],
        }

    monkeypatch.setattr(
        dashboard_server.listing_search,
        "fetch_listings_page",
        fake_fetch,
    )
    connection = object()

    payload = dashboard_server.fetch_listings_page_fast(
        connection,
        50,
        25,
        "Crown",
        "sticker",
    )

    assert captured["args"] == (connection, 50, 25, "Crown", "sticker")
    assert payload["max_offset"] == listing_search.MAX_OFFSET
    row = payload["rows"][0]
    assert row["item_steam_market_url"].startswith(
        dashboard_server.STEAM_MARKET_BASE_URL
    )
    assert "%7C%20Slate" in row["item_steam_market_url"]
    assert row["stickers"][0]["steam_market_url"].startswith(
        dashboard_server.STEAM_MARKET_BASE_URL
    )
    assert "%7C%20Crown" in row["stickers"][0]["steam_market_url"]


def test_database_browser_is_lazy_and_has_a_single_in_flight_request():
    html = dashboard_server.HTML
    refresh = _section(
        html,
        "async function refreshDbBrowser()",
        "async function refreshDealCandidates()",
    )
    dashboard_refresh = _section(
        html,
        "async function refresh()",
        "document.getElementById('weaponSelect')",
    )

    assert "let activeDashboardView = 'overview';" in html
    assert "let dbBrowserLoaded = false;" in html
    assert "let dbBrowserLoading = false;" in html
    assert "const generation = ++dbBrowserGeneration;" in refresh
    assert "if (dbBrowserController) dbBrowserController.abort();" in refresh
    assert "signal: dbBrowserController.signal" in refresh
    assert (
        "activeDashboardView === 'system' && "
        "!dbBrowserLoaded && !dbBrowserLoading"
    ) in dashboard_refresh
    assert "dbBrowserBody').children.length" not in dashboard_refresh


def test_scope_change_invalidates_and_immediately_replaces_in_flight_search():
    handler = _section(
        dashboard_server.HTML,
        "document.getElementById('dbSearchScope').addEventListener('change'",
        "document.getElementById('dbPrevButton').addEventListener",
    )

    assert handler.index("dbOffset = 0;") < handler.index("dbBrowserLoaded = false;")
    assert handler.index("dbBrowserLoaded = false;") < handler.index(
        "refreshDbBrowser();"
    )


def test_completed_listing_error_is_terminal_until_the_next_user_action():
    refresh = _section(
        dashboard_server.HTML,
        "async function refreshDbBrowser()",
        "async function refreshDealCandidates()",
    )
    handled_error = _section(
        refresh,
        "} catch (error) {",
        "} finally {",
    )

    assert "dbBrowserLoaded = true;" in handled_error
    assert "dbBrowserLoaded = false;" not in handled_error
    assert "dbPrevButton').disabled = true" in handled_error
    assert "dbNextButton').disabled = true" in handled_error


def test_listing_pagination_honors_the_server_offset_cap():
    refresh = _section(
        dashboard_server.HTML,
        "async function refreshDbBrowser()",
        "async function refreshDealCandidates()",
    )

    assert "const maxOffset = Number(data.max_offset ?? 100000);" in refresh
    assert "dbOffset + dbLimit > maxOffset" in refresh


def test_listing_route_uses_scope_and_does_not_disclose_sqlite_errors():
    source = inspect.getsource(dashboard_server.Handler.do_GET)
    route = _section(
        source,
        'if parsed.path == "/api/listings":',
        'if parsed.path == "/api/deal-candidates":',
    )

    assert 'scope = query.get("scope", ["skin"])[0]' in route
    assert "fetch_listings_page_fast(" in route
    assert "conn, offset, limit, search, scope" in " ".join(route.split())
    assert "except listing_search.ListingSearchError as exc:" in route
    assert "except sqlite3.Error:" in route
    assert '"listing database is unavailable"' in route
    sqlite_handler = route.split("except sqlite3.Error:", 1)[1]
    assert "str(exc)" not in sqlite_handler
