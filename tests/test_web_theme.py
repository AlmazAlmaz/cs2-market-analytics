import gzip
import io
import re
import threading
import time

import pytest

import dashboard_server
import web_theme
from TRENDMODEL.model import TrendModelConfig, build_latest_forecasts


class _CaptureHandler:
    def __init__(self, headers=None):
        self.headers = {"Cookie": "cs2_locale=ru", **(headers or {})}
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        pass

    def header(self, name):
        return next(
            (value for key, value in self.response_headers if key.lower() == name.lower()),
            None,
        )


def test_inject_theme_is_idempotent_and_marks_page_and_plain_brand():
    document = (
        "<html><head><style>body{}</style></head>"
        '<body class="existing"><header><div class="top wrap">'
        '<a class="brand" href="/">◆ CS2 Market</a></div></header></body></html>'
    )

    themed = web_theme.inject_theme(document, page="catalog")

    assert themed.count('id="cs2-unified-theme"') == 1
    assert themed.count('id="cs2-theme-boot"') == 1
    assert themed.count('id="cs2-unified-theme-script"') == 1
    assert '<body class="existing page-catalog">' in themed
    assert '<a class="brand cs-brand-plain" href="/">CS2 Market</a>' in themed
    # The theme must come after page styles so it wins the cascade.
    assert themed.index("body{}") < themed.index('id="cs2-unified-theme"')
    assert web_theme.inject_theme(themed, page="catalog") == themed


def test_body_class_is_added_without_duplicates():
    assert web_theme._add_body_class("<body>", "page-auth") == '<body class="page-auth">'
    assert (
        web_theme._add_body_class('<body data-mode="login">', "page-auth")
        == '<body data-mode="login" class="page-auth">'
    )
    assert (
        web_theme._add_body_class('<body class="a page-auth">', "page-auth")
        == '<body class="a page-auth">'
    )


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_every_private_template_gets_the_unified_theme(locale):
    for template in (
        dashboard_server.HTML,
        dashboard_server.DEAL_HTML,
        dashboard_server.DEALS_HTML,
        dashboard_server.STRATEGY_HTML,
    ):
        rendered = dashboard_server.render_private_html(template, locale)
        assert rendered.count('id="cs2-unified-theme"') == 1
        assert 'rel="icon"' in rendered


@pytest.mark.parametrize(
    "response_wrapper",
    [
        dashboard_server.knives_html_response,
        dashboard_server.catalog_html_response,
        dashboard_server.market_history_html_response,
    ],
)
def test_standalone_pages_are_themed(response_wrapper):
    handler = _CaptureHandler()
    response_wrapper(handler)
    document = handler.wfile.getvalue().decode("utf-8")
    assert handler.status == 200
    assert document.count('id="cs2-unified-theme"') == 1


def test_auth_page_is_themed():
    handler = _CaptureHandler()
    dashboard_server.auth_html_response(handler, "login")
    document = handler.wfile.getvalue().decode("utf-8")
    assert 'id="cs2-unified-theme"' in document
    assert re.search(r'<body[^>]*class="[^"]*page-auth', document)


def test_text_responses_are_gzipped_only_when_accepted():
    body = "x" * 50_000

    plain = _CaptureHandler()
    dashboard_server.html_body_response(plain, body)
    assert plain.header("Content-Encoding") is None
    assert plain.wfile.getvalue() == body.encode("utf-8")
    assert plain.header("Vary") == "Accept-Encoding"

    compressed = _CaptureHandler({"Accept-Encoding": "br, gzip;q=0.8"})
    dashboard_server.html_body_response(compressed, body)
    assert compressed.header("Content-Encoding") == "gzip"
    raw = compressed.wfile.getvalue()
    assert int(compressed.header("Content-Length")) == len(raw)
    assert gzip.decompress(raw) == body.encode("utf-8")

    refused = _CaptureHandler({"Accept-Encoding": "gzip;q=0"})
    dashboard_server.html_body_response(refused, body)
    assert refused.header("Content-Encoding") is None


def test_small_json_is_sent_uncompressed():
    handler = _CaptureHandler({"Accept-Encoding": "gzip"})
    dashboard_server.json_response(handler, 200, {"ok": True})
    assert handler.header("Content-Encoding") is None
    assert handler.wfile.getvalue() == b'{"ok": true}'


def test_favicon_is_served_without_authentication():
    handler = _CaptureHandler()
    dashboard_server.favicon_response(handler)
    assert handler.status == 200
    assert handler.header("Content-Type") == "image/svg+xml"
    assert handler.wfile.getvalue().startswith(b"<svg")


def test_heavy_dashboard_aggregates_are_cached_and_refreshed_in_background(monkeypatch):
    calls = []

    def fake_build():
        calls.append(time.monotonic())
        return {"overview": {"sales_count": len(calls)}, "model_freshness": {"models": []}}

    monkeypatch.setattr(dashboard_server, "build_dashboard_heavy_payload", fake_build)
    key = dashboard_server.DASHBOARD_HEAVY_CACHE_KEY
    with dashboard_server.CACHE_LOCK:
        dashboard_server.PAYLOAD_CACHE.pop(key, None)
        dashboard_server.BACKGROUND_REFRESHING_KEYS.discard(key)
    try:
        first = dashboard_server.get_dashboard_heavy_payload()
        assert first["overview"]["sales_count"] == 1
        # Fresh cache: no rebuild.
        assert dashboard_server.get_dashboard_heavy_payload() is first
        assert len(calls) == 1

        # Stale cache: the old value is served immediately while a refresh runs.
        with dashboard_server.CACHE_LOCK:
            created_at, payload = dashboard_server.PAYLOAD_CACHE[key]
            dashboard_server.PAYLOAD_CACHE[key] = (
                created_at - dashboard_server.DASHBOARD_HEAVY_CACHE_TTL_SECONDS - 1,
                payload,
            )
        stale = dashboard_server.get_dashboard_heavy_payload()
        assert stale["overview"]["sales_count"] == 1
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            refreshed, _age = dashboard_server.cache_get_with_age(key)
            if refreshed["overview"]["sales_count"] == 2:
                break
            time.sleep(0.02)
        assert refreshed["overview"]["sales_count"] == 2
    finally:
        with dashboard_server.CACHE_LOCK:
            dashboard_server.PAYLOAD_CACHE.pop(key, None)
            dashboard_server.BACKGROUND_REFRESHING_KEYS.discard(key)


def test_heavy_dashboard_waits_briefly_for_startup_warmup(monkeypatch):
    release = threading.Event()

    def slow_build():
        release.wait(5)
        return {"overview": {"sales_count": 7}, "model_freshness": {"models": []}}

    monkeypatch.setattr(dashboard_server, "build_dashboard_heavy_payload", slow_build)
    key = dashboard_server.DASHBOARD_HEAVY_CACHE_KEY
    with dashboard_server.CACHE_LOCK:
        dashboard_server.PAYLOAD_CACHE.pop(key, None)
        dashboard_server.BACKGROUND_REFRESHING_KEYS.discard(key)
    try:
        dashboard_server.start_background_refresh(key, dashboard_server.build_dashboard_heavy_payload)
        started = time.monotonic()
        assert dashboard_server.get_dashboard_heavy_payload(wait_seconds=0.3) is None
        assert time.monotonic() - started < 2
        release.set()
        assert dashboard_server.get_dashboard_heavy_payload(wait_seconds=3)["overview"]["sales_count"] == 7
    finally:
        release.set()
        with dashboard_server.CACHE_LOCK:
            dashboard_server.PAYLOAD_CACHE.pop(key, None)
            dashboard_server.BACKGROUND_REFRESHING_KEYS.discard(key)


def test_trend_forecast_damps_long_horizon_momentum():
    # A short, very steep spike: linear extrapolation used to turn it into a
    # multi-thousand-percent 30-day forecast.
    rows = []
    for offset in range(20):
        rows.append(
            {
                "market_hash_name": "Spike",
                "day": f"2026-03-{offset + 1:02d}",
                "sales_count": 10,
                "median_price_cents": 1_000 * (1.11 ** offset),
            }
        )
        rows.append(
            {
                "market_hash_name": "Flat",
                "day": f"2026-03-{offset + 1:02d}",
                "sales_count": 10,
                "median_price_cents": 1_000,
            }
        )
    forecasts = build_latest_forecasts(rows, as_of_day="2026-03-20")
    spike = {
        row["horizon_days"]: row["expected_return_percent"]
        for row in forecasts
        if row["market_hash_name"] == "Spike"
    }
    assert 0 < spike[7] < spike[14] < spike[30]
    tau = TrendModelConfig().trend_damping_days
    assert TrendModelConfig().damped_horizon(30) < tau
    # Even the maximum allowed slope cannot exceed exp(0.12 * tau) growth.
    assert spike[30] < (2.72 ** (0.12 * tau) - 1) * 100


def test_trend_damping_must_be_positive():
    with pytest.raises(ValueError, match="trend_damping_days"):
        TrendModelConfig(trend_damping_days=0)
