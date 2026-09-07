import html
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

import account_backend
import account_ui
import admin_users_ui
import app_config
import catalog_browser
import csfloat_listing_fetcher
import deal_intelligence
import inventory_market
import inventory_item_catalog
import knives_dashboard
import listing_search
import market_analytics
import market_history_dashboard
import model_generations
import model_lifecycle
import model_training_jobs
import scanner_control
import scanner_target_catalog
import web_i18n
from item_classification import (
    ITEM_KIND_GLOVE,
    ITEM_KIND_KNIFE,
    ITEM_KIND_WEAPON_SKIN,
    classify_market_hash_name,
    sql_item_kind_expression,
)
from TRENDMODEL.api import (
    get_backtest_report,
    get_collection_detail,
    get_skin_daily_series,
    get_trend_pipeline_status,
    get_trend_summary,
    list_collections,
)
from TRENDMODEL.backtest import BacktestConfig

DB_PATH = str(app_config.path("CS2_DB_PATH", "csfloat_history.db"))
TREND_DB_PATH = str(app_config.path("CS2_TREND_DB_PATH", "trend_market.db"))
TREND_STATUS_PATH = str(
    app_config.path("CS2_TREND_STATUS_PATH", "trend_worker_status.json")
)
STATUS_PATH = str(app_config.path("CS2_STATUS_PATH", "parser_status.json"))
SKINS_PATH = str(app_config.path("CS2_SKINS_PATH", "ALL_SKINS.json"))
TOKEN_PATH = app_config.path("DASHBOARD_TOKEN_PATH", "dashboard_token.txt")
HOST = app_config.get("DASHBOARD_HOST", "127.0.0.1")
PORT = app_config.get_int("DASHBOARD_PORT", 8010, minimum=1, maximum=65535)
ACCOUNT_DB_PATH = str(app_config.path("CS2_ACCOUNT_DB_PATH", ".private/account_data.db"))
MARKET_HISTORY_DB_PATH = str(
    app_config.path("CS2_MARKET_HISTORY_DB_PATH", ".private/market_history.db")
)
ACCOUNT_PUBLIC_URL = app_config.get(
    "CS2_ACCOUNT_PUBLIC_URL", f"http://localhost:{PORT}"
).rstrip("/")
ACCOUNT_RESET_URL = f"{ACCOUNT_PUBLIC_URL}/reset-password"
ACCOUNT_REGISTRATION_ENABLED = app_config.get_bool(
    "CS2_ACCOUNT_REGISTRATION_ENABLED", True
)
ACCOUNT_COOKIE_SECURE = app_config.get_bool(
    "CS2_ACCOUNT_COOKIE_SECURE", ACCOUNT_PUBLIC_URL.startswith("https://")
)
ACCOUNT_TRUST_CF_COUNTRY = app_config.get_bool(
    "CS2_ACCOUNT_TRUST_CF_COUNTRY", False
)
ACCOUNT_BODY_LIMIT = 64 * 1024
ADMIN_USERS_BODY_LIMIT = 16 * 1024
ACCOUNT_SESSION_COOKIE = "cs2_session"
ACCOUNT_CSRF_COOKIE = "cs2_csrf"
ACCOUNT_ITEM_SEARCH_LIMIT = 20
ACCOUNT_CONTAINER_CACHE_PATH = app_config.path(
    "CS2_ACCOUNT_CONTAINER_CACHE_PATH",
    ".private/item_catalog_cache/bymykel_crates.json",
)
ACCOUNT_CONTAINER_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
ACCOUNT_LIVE_PRICING_ENABLED = app_config.get_bool(
    "CS2_ACCOUNT_LIVE_PRICING_ENABLED",
    True,
)
PROGRESS_ID = 2
STEAM_MARKET_BASE_URL = "https://steamcommunity.com/market/listings/730/"
CSFLOAT_SCREENSHOT_BASE_URL = "https://s.csfloat.com/m"
DASHBOARD_STATIC_CACHE_TTL_SECONDS = 300
DETAIL_CACHE_TTL_SECONDS = 120
LIVE_REFRESH_MS = 10000
# A listing fetch can try several 30-second routes synchronously.  PID liveness
# still detects a stopped process immediately; this threshold only separates a
# fresh scanner from a live-but-unresponsive one.
SCANNER_HEARTBEAT_STALE_SECONDS = 150
SCANNER_HEARTBEAT_FUTURE_TOLERANCE_SECONDS = 30
SCANNER_CONTROL_BODY_LIMIT = 4096
SCANNER_TARGET_BODY_LIMIT = scanner_control.TARGET_CONTROL_BODY_LIMIT
MODEL_TRAINING_BODY_LIMIT = 2048
STRATEGY_BODY_LIMIT = 8192
STRATEGY_MAX_TREND_ITEMS = 100
STRATEGY_MAX_DEAL_ITEMS = 100
STRATEGY_MAX_OUTCOME_ROWS = 10_000
MARKET_CACHE_TTL_SECONDS = 60
MODEL_TRAINING_ELIGIBILITY_CACHE_SECONDS = 300
TARGET_NEW_SALES_PER_REQUEST = app_config.get_int(
    "PARSER_TARGET_NEW_SALES",
    20,
    minimum=5,
    maximum=30,
)
DEAL_POSITIVE_VERDICTS = frozenset({"good", "watch"})
DEAL_MODEL_NAMES = frozenset({"almaz", "igor", "nova"})
DEFAULT_DEAL_OPINION_STALE_HOURS = 24
MAX_SAFE_PAGE_OFFSET = 9_007_199_254_740_991
DEAL_CONFIDENCE_FILTERS = frozenset({
    "all",
    "consensus",
    "unanimous_three",
    "disagreement",
    "single_signal",
})

PAYLOAD_CACHE = {}
CACHE_LOCK = threading.Lock()
BACKGROUND_REFRESHING_KEYS = set()
DB_SNAPSHOT_LOCK = threading.Lock()
MODEL_TRAINING_CACHE_LOCK = threading.Lock()
MODEL_TRAINING_ELIGIBILITY_CACHE = {
    "loaded_monotonic": 0.0,
    "value": None,
    "refreshing": False,
}
ACCOUNT_SERVICE_LOCK = threading.Lock()
ACCOUNT_SERVICE = None
AUTH_RATE_LOCK = threading.Lock()
AUTH_RATE_BUCKETS = defaultdict(list)


def validate_account_runtime_config():
    parsed = urlparse(ACCOUNT_PUBLIC_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("CS2_ACCOUNT_PUBLIC_URL must be an absolute http(s) origin")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise RuntimeError("CS2_ACCOUNT_PUBLIC_URL must not contain a path or query")
    hostname = (parsed.hostname or "").casefold()
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and hostname not in local_hosts:
        raise RuntimeError("CS2_ACCOUNT_PUBLIC_URL must use HTTPS outside localhost")
    if parsed.scheme == "https" and not ACCOUNT_COOKIE_SECURE:
        raise RuntimeError("CS2_ACCOUNT_COOKIE_SECURE must be true for an HTTPS account site")


def build_csfloat_screenshot_url(screenshot_id):
    if not screenshot_id:
        return None
    return f"{CSFLOAT_SCREENSHOT_BASE_URL}/{screenshot_id}/playside.png?v=3"


def normalize_listing_type(data):
    if not isinstance(data, dict):
        return "unknown"

    for key in ("listing_type", "sale_type", "type", "kind"):
        value = data.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        if "auction" in text or text in {"bid", "bidding"}:
            return "auction"
        if text in {"buy_now", "buynow", "fixed", "fixed_price", "instant", "listing"}:
            return "buy_now"

    for key in ("is_auction", "auction"):
        value = data.get(key)
        if isinstance(value, bool):
            return "auction" if value else "buy_now"
        if isinstance(value, dict):
            return "auction"

    auction_markers = (
        "auction_ends_at",
        "auction_end_at",
        "auction_end_time",
        "auction_details",
        "highest_bid",
        "reserve_price",
        "minimum_bid",
    )
    if any(data.get(key) not in (None, "", False) for key in auction_markers):
        return "auction"

    return "unknown"


def parse_iso_timestamp(value):
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_price_filter_cents(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        price = float(text)
    except ValueError:
        return None
    if not math.isfinite(price) or price < 0 or price > 100_000_000:
        return None
    return int(round(price * 100))


def parse_number_filter(value, *, minimum, maximum):
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


PUBLIC_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CSSkinsPrediction</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f1012;
      --panel: #17191e;
      --panel-2: #20232a;
      --control-bg: #111318;
      --subpanel-bg: #13151a;
      --visual-bg: #0c0e12;
      --image-bg: #0b0d10;
      --text: #f1f3f6;
      --muted: #a2a9b4;
      --line: #30343d;
      --accent: #8068ff;
      --good: #22c55e;
      --warn: #f59e0b;
    }
    body.theme-light {
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --panel-2: #eef2f7;
      --control-bg: #ffffff;
      --subpanel-bg: #f7f9fc;
      --visual-bg: #eef2f7;
      --image-bg: #f7f9fc;
      --text: #111827;
      --muted: #667085;
      --line: #d6dce7;
      --accent: #6d50e6;
      --good: #15803d;
      --warn: #b45309;
    }
    body.theme-midnight {
      color-scheme: dark;
      --bg: #070b12;
      --panel: #101722;
      --panel-2: #162131;
      --control-bg: #0b111b;
      --subpanel-bg: #0e1622;
      --visual-bg: #080d15;
      --image-bg: #080d15;
      --text: #edf6ff;
      --muted: #93a4b8;
      --line: #253447;
      --accent: #8b7aff;
      --good: #2dd4bf;
      --warn: #fbbf24;
    }
    body.theme-graphite {
      color-scheme: dark;
      --bg: #151515;
      --panel: #202020;
      --panel-2: #2a2a2a;
      --control-bg: #181818;
      --subpanel-bg: #1b1b1b;
      --visual-bg: #151515;
      --image-bg: #101010;
      --text: #f2f2f2;
      --muted: #a8a8a8;
      --line: #3a3a3a;
      --accent: #a78bfa;
      --good: #84cc16;
      --warn: #f97316;
    }
    * { box-sizing: border-box; }
    html, body { max-width: 100%; overflow-x: hidden; }
    body { --accent: #8068ff !important; --accent-2: #9d8cff !important; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 15px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    a { color: inherit; }
    .wrap {
      width: min(1180px, calc(100vw - 32px));
      margin: 0 auto;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 3;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--bg) 94%, transparent);
      backdrop-filter: blur(10px);
    }
    .topbar {
      min-height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
    }
    .brand {
      font-size: 19px;
      font-weight: 780;
    }
    .nav {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .button {
      min-height: 36px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      background: var(--control-bg);
      color: var(--text);
      text-decoration: none;
      white-space: nowrap;
      font-weight: 650;
    }
    .button:hover { border-color: var(--accent); }
    select {
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--control-bg);
      color: var(--text);
      padding: 0 9px;
      font: inherit;
      outline: none;
    }
    .control {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 36px;
    }
    .control-label {
      color: var(--muted);
      font-size: 12px;
      line-height: 1;
      white-space: nowrap;
    }
    main { padding: 18px 0 34px; }
    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(360px, 0.95fr);
      gap: 14px;
      align-items: stretch;
    }
    .panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      overflow: hidden;
    }
    .intro {
      padding: 24px;
      display: grid;
      align-content: space-between;
      gap: 22px;
      min-height: 290px;
    }
    h1 {
      margin: 0;
      font-size: clamp(32px, 5vw, 62px);
      line-height: 1.02;
      letter-spacing: 0;
    }
    .lead {
      max-width: 720px;
      color: var(--muted);
      font-size: 17px;
      margin: 14px 0 0;
    }
    .status-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 0 10px;
      background: var(--control-bg);
      color: var(--muted);
      white-space: nowrap;
    }
    .visual-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      padding: 10px;
      background: var(--visual-bg);
    }
    .skin-tile {
      min-height: 170px;
      position: relative;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: var(--control-bg);
    }
    .skin-tile img {
      width: 100%;
      height: 132px;
      object-fit: contain;
      display: block;
      background: var(--image-bg);
    }
    .skin-tile .tile-title {
      padding: 8px;
      font-size: 12px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }
    .metric {
      padding: 14px;
    }
    .metric-value {
      font-size: 26px;
      font-weight: 780;
    }
    .metric-label {
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
    }
    .sections {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 14px;
    }
    .section-head {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-2);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    h2 {
      margin: 0;
      font-size: 15px;
      font-weight: 740;
    }
    .list {
      display: grid;
    }
    .row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
    }
    .row:last-child { border-bottom: 0; }
    .name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 650;
    }
    .sub {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    .value {
      color: var(--accent);
      font-weight: 760;
      white-space: nowrap;
    }
    .wide {
      grid-column: span 2;
    }
    .privacy {
      padding: 14px;
      color: var(--muted);
    }
    @media (max-width: 920px) {
      .hero, .sections, .metrics {
        grid-template-columns: 1fr;
      }
      .intro {
        min-height: auto;
      }
      .wide {
        grid-column: auto;
      }
    }
    @media (max-width: 560px) {
      .wrap {
        width: min(100vw - 18px, 1180px);
      }
      .topbar {
        align-items: flex-start;
        flex-direction: column;
        padding: 10px 0;
      }
      .nav {
        width: 100%;
        justify-content: stretch;
      }
      .button {
        flex: 1 1 auto;
      }
      .control {
        width: 100%;
      }
      .control select {
        flex: 1 1 auto;
      }
      main {
        padding-top: 10px;
      }
      .intro {
        padding: 18px;
      }
      h1 {
        font-size: 36px;
      }
      .lead {
        font-size: 15px;
      }
      .visual-grid {
        grid-template-columns: 1fr;
      }
      .skin-tile img {
        height: 190px;
      }
      .row {
        grid-template-columns: 1fr;
      }
      .value {
        white-space: normal;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <div class="brand">CSSkinsPrediction</div>
      <nav class="nav">
        <label class="control">
          <span class="control-label">Theme</span>
          <select id="themeSelect" title="Theme">
            <option value="dark">Dark</option>
            <option value="light">Light</option>
            <option value="midnight">Midnight</option>
            <option value="graphite">Graphite</option>
          </select>
        </label>
        <a class="button" id="accountLoginLink" href="/login?next=/deals">Войти</a>
        <a class="button" id="accountRegisterLink" href="/register">Регистрация</a>
      </nav>
    </div>
  </header>

  <main class="wrap">
    <section class="hero">
      <div class="panel intro">
        <div>
          <h1>CS2 skin price intelligence</h1>
          <p class="lead">A private analytics stack for CSFloat sales history, sticker crafts, liquidity checks, and model-based pricing signals.</p>
        </div>
        <div class="status-row">
          <span class="pill">Updated: __LATEST_FETCHED__</span>
          <span class="pill">Models: Almaz + Igor + Nova Neural</span>
          <span class="pill">Access: secure accounts</span>
        </div>
      </div>
      <div class="panel visual-grid">
        __RECENT_TILES__
      </div>
    </section>

    <section class="metrics">
      <div class="panel metric">
        <div class="metric-value">__SALES_COUNT__</div>
        <div class="metric-label">historical sales</div>
      </div>
      <div class="panel metric">
        <div class="metric-value">__SKIN_VARIANT_COUNT__</div>
        <div class="metric-label">skin variants</div>
      </div>
      <div class="panel metric">
        <div class="metric-value">__STICKER_ROWS__</div>
        <div class="metric-label">sticker rows</div>
      </div>
      <div class="panel metric">
        <div class="metric-value">__DEAL_COUNT__</div>
        <div class="metric-label">saved model candidates</div>
      </div>
    </section>

    <section class="sections">
      <div class="panel">
        <div class="section-head"><h2>Popular Weapons</h2><span class="sub">by sticker rows</span></div>
        <div class="list">__TOP_WEAPONS__</div>
      </div>
      <div class="panel">
        <div class="section-head"><h2>Popular Stickers</h2><span class="sub">in history</span></div>
        <div class="list">__TOP_STICKERS__</div>
      </div>
    </section>
  </main>
  <script>
    function applyTheme() {
      const select = document.getElementById('themeSelect');
      if (!select) return;
      const value = select.value;
      document.body.classList.remove('theme-light', 'theme-midnight', 'theme-graphite');
      if (value !== 'dark') {
        document.body.classList.add(`theme-${value}`);
      }
      try {
        localStorage.setItem('siteTheme', value);
      } catch (error) {}
    }

    try {
      const savedTheme = localStorage.getItem('siteTheme') || localStorage.getItem('bestDealsTheme');
      if (savedTheme && ['dark', 'light', 'midnight', 'graphite'].includes(savedTheme)) {
        document.getElementById('themeSelect').value = savedTheme;
      }
    } catch (error) {}
    applyTheme();
    document.getElementById('themeSelect').addEventListener('change', applyTheme);
    fetch('/api/auth/me', { credentials: 'same-origin', cache: 'no-store' })
      .then(response => response.ok ? response.json() : null)
      .then(payload => {
        if (!payload || !payload.user) return;
        const login = document.getElementById('accountLoginLink');
        const register = document.getElementById('accountRegisterLink');
        if (login) { login.href = '/account'; login.textContent = 'Мой инвентарь'; }
        if (register) register.hidden = true;
      })
      .catch(() => {});
  </script>
</body>
</html>
"""


HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CSFloat Parser Dashboard</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101114;
      --panel: #181a1f;
      --panel-2: #20232a;
      --control-bg: #111318;
      --table-head: #15171c;
      --subpanel-bg: #13151a;
      --image-bg: #0d0f13;
      --modal-bg: #0d0f13;
      --modal-backdrop: rgba(0, 0, 0, 0.78);
      --text: #eef0f4;
      --muted: #9ca3af;
      --line: #30343d;
      --good: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
      --accent: #38bdf8;
    }
    body.theme-light {
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --panel-2: #eef2f7;
      --control-bg: #ffffff;
      --table-head: #e8edf5;
      --subpanel-bg: #f7f9fc;
      --image-bg: #f7f9fc;
      --modal-bg: #ffffff;
      --modal-backdrop: rgba(15, 23, 42, 0.56);
      --text: #111827;
      --muted: #667085;
      --line: #d6dce7;
      --good: #15803d;
      --warn: #b45309;
      --bad: #b91c1c;
      --accent: #0369a1;
    }
    body.theme-midnight {
      color-scheme: dark;
      --bg: #070b12;
      --panel: #101722;
      --panel-2: #162131;
      --control-bg: #0b111b;
      --table-head: #111a27;
      --subpanel-bg: #0e1622;
      --image-bg: #080d15;
      --modal-bg: #080d15;
      --modal-backdrop: rgba(0, 0, 0, 0.84);
      --text: #edf6ff;
      --muted: #93a4b8;
      --line: #253447;
      --good: #2dd4bf;
      --warn: #fbbf24;
      --bad: #fb7185;
      --accent: #60a5fa;
    }
    body.theme-graphite {
      color-scheme: dark;
      --bg: #151515;
      --panel: #202020;
      --panel-2: #2a2a2a;
      --control-bg: #181818;
      --table-head: #242424;
      --subpanel-bg: #1b1b1b;
      --image-bg: #101010;
      --modal-bg: #111111;
      --modal-backdrop: rgba(0, 0, 0, 0.8);
      --text: #f2f2f2;
      --muted: #a8a8a8;
      --line: #3a3a3a;
      --good: #84cc16;
      --warn: #f97316;
      --bad: #ef4444;
      --accent: #eab308;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    header {
      position: sticky;
      top: 0;
      z-index: 2;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--bg) 94%, transparent);
      backdrop-filter: blur(10px);
    }

    .wrap {
      width: min(1500px, calc(100vw - 32px));
      margin: 0 auto;
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      min-height: 64px;
    }

    .top-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 750;
      letter-spacing: 0;
    }

    .sub {
      color: var(--muted);
      font-size: 13px;
      margin-top: 2px;
    }

    main {
      padding: 18px 0 28px;
    }

    .grid {
      display: grid;
      gap: 12px;
    }

    .metrics {
      grid-template-columns: repeat(6, minmax(140px, 1fr));
    }

    .two {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      margin-top: 12px;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }

    .metric {
      padding: 14px;
      min-height: 86px;
    }

    .metric .label {
      color: var(--muted);
      font-size: 12px;
    }

    .metric .value {
      margin-top: 8px;
      font-size: 24px;
      font-weight: 760;
      white-space: nowrap;
    }

    .metric .hint {
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-2);
    }

    h2 {
      margin: 0;
      font-size: 14px;
      font-weight: 720;
    }

    .pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }

    .button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--control-bg);
      color: var(--text);
      padding: 0 11px;
      text-decoration: none;
      font: inherit;
      white-space: nowrap;
    }

    .button:hover {
      border-color: var(--accent);
    }

    a.market-link {
      color: var(--text);
      text-decoration: none;
    }

    a.market-link:hover {
      color: var(--accent);
      text-decoration: underline;
    }

    select {
      min-width: 220px;
      max-width: min(420px, 46vw);
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--control-bg);
      color: var(--text);
      padding: 0 9px;
      font: inherit;
      outline: none;
    }

    input {
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--control-bg);
      color: var(--text);
      padding: 0 9px;
      font: inherit;
      outline: none;
    }

    .control {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 34px;
    }

    .control-label {
      color: var(--muted);
      font-size: 12px;
      line-height: 1;
      white-space: nowrap;
    }

    .section-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      flex-wrap: wrap;
    }

    .progress {
      height: 9px;
      border-radius: 999px;
      background: #0b0c0f;
      overflow: hidden;
      border: 1px solid var(--line);
      margin-top: 10px;
    }

    .progress > div {
      height: 100%;
      background: var(--accent);
      width: 0%;
      transition: width 250ms ease;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }

    th, td {
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      background: var(--table-head);
    }

    tr:last-child td {
      border-bottom: 0;
    }

    tr.rarity-row {
      --rarity: var(--line);
      background:
        linear-gradient(90deg, color-mix(in srgb, var(--rarity) 22%, transparent), transparent 42%);
    }

    tr.rarity-row td:first-child {
      border-left: 4px solid var(--rarity);
    }

    .status {
      display: inline-flex;
      align-items: center;
      gap: 7px;
    }

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--muted);
      flex: 0 0 auto;
    }

    .dot.good { background: var(--good); }
    .dot.warn { background: var(--warn); }
    .dot.bad { background: var(--bad); }

    .muted {
      color: var(--muted);
    }

    .mono {
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
    }

    .thumb {
      width: 42px;
      height: 42px;
      object-fit: contain;
      display: block;
      border-radius: 6px;
      background: #0b0c0f;
      border: 1px solid var(--line);
    }

    .no-thumb {
      width: 42px;
      height: 42px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      background: #0b0c0f;
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 10px;
    }

    .full {
      margin-top: 12px;
    }

    .model-health-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      padding: 14px;
    }

    .model-isolation-note {
      margin: -2px 14px 0;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.5;
    }

    .model-health-card {
      min-width: 0;
      padding: 15px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--subpanel-bg);
      overflow: hidden;
    }

    .model-health-card:last-child:nth-child(odd) {
      grid-column: 1 / -1;
    }

    .model-health-title, .model-health-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }

    .model-health-title {
      font-size: 16px;
      font-weight: 760;
      text-transform: capitalize;
    }

    .model-title-copy { display: grid; min-width: 0; gap: 2px; }
    .model-title-copy small {
      color: var(--muted);
      font-size: 10px;
      font-weight: 650;
      text-transform: none;
    }

    .model-baseline {
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: color-mix(in srgb, var(--control-bg) 65%, transparent);
    }
    .model-baseline summary {
      padding: 9px 11px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
    }
    .model-baseline-body { padding: 0 11px 10px; }

    .model-health-row {
      margin-top: 7px;
      color: var(--muted);
      font-size: 12px;
    }

    .model-health-row strong {
      color: var(--text);
      text-align: right;
      font-weight: 680;
    }

    .freshness-good { color: var(--good); }
    .freshness-warn { color: var(--warn); }
    .freshness-bad { color: var(--bad); }

    .model-training {
      display: grid;
      gap: 9px;
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
    }

    .model-training-summary, .model-training-meta, .model-training-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }

    .model-training-summary strong { font-size: 12px; }
    .model-training-summary span, .model-training-meta {
      color: var(--muted);
      font-size: 11px;
    }

    .model-training-progress {
      height: 8px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--control-bg);
    }

    .model-training-progress > span {
      display: block;
      width: 0;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--accent), var(--good));
      transition: width 220ms ease;
    }

    .model-training-stages {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 5px;
    }
    .model-training-stage {
      display: grid;
      justify-items: center;
      gap: 5px;
      min-width: 0;
      color: var(--muted);
      font-size: 9px;
      text-align: center;
    }
    .model-training-stage::before {
      content: "";
      width: 9px;
      height: 9px;
      border: 2px solid var(--line);
      border-radius: 50%;
      background: var(--control-bg);
    }
    .model-training-stage[data-state="done"] { color: var(--good); }
    .model-training-stage[data-state="done"]::before { border-color: var(--good); background: var(--good); }
    .model-training-stage[data-state="active"] { color: var(--accent); font-weight: 800; }
    .model-training-stage[data-state="active"]::before {
      border-color: var(--accent);
      background: var(--accent);
      box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 18%, transparent);
    }
    .model-training-stage[data-state="failed"] { color: var(--bad); font-weight: 800; }
    .model-training-stage[data-state="failed"]::before { border-color: var(--bad); background: var(--bad); }

    .model-training-reason, .model-training-feedback {
      min-height: 16px;
      margin: 0;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    .model-training-feedback[data-tone="good"] { color: var(--good); }
    .model-training-feedback[data-tone="warn"] { color: var(--warn); }
    .model-training-feedback[data-tone="bad"] { color: var(--bad); }
    .model-training-actions .button { flex: 1 1 0; min-width: 0; }

    .model-quality-comparison {
      border: 1px solid var(--line);
      border-radius: 11px;
      background: color-mix(in srgb, var(--control-bg) 76%, transparent);
    }
    .model-quality-comparison > summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 10px 11px;
      cursor: pointer;
      font-size: 11px;
      font-weight: 760;
    }
    .model-quality-comparison > summary small { color: var(--muted); font-weight: 650; }
    .model-quality-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      padding: 0 11px 9px;
    }
    .model-quality-meta span {
      padding: 4px 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      font-size: 9px;
    }
    .model-quality-table-wrap { overflow-x: auto; border-top: 1px solid var(--line); }
    .model-quality-table { width: 100%; min-width: 500px; border-collapse: collapse; }
    .model-quality-table th, .model-quality-table td {
      padding: 8px 9px;
      border-bottom: 1px solid var(--line);
      font-size: 10px;
      text-align: right;
      white-space: nowrap;
    }
    .model-quality-table th { color: var(--muted); font-weight: 700; }
    .model-quality-table th:first-child, .model-quality-table td:first-child { text-align: left; }
    .model-quality-table tr:last-child td { border-bottom: 0; }
    .model-quality-table .metric-help {
      display: block;
      max-width: 190px;
      margin-top: 2px;
      color: var(--muted);
      font-size: 8px;
      font-weight: 550;
      line-height: 1.35;
      white-space: normal;
    }
    .quality-better { color: var(--good); font-weight: 800; }
    .quality-worse { color: var(--bad); font-weight: 800; }
    .quality-equal { color: var(--muted); font-weight: 700; }
    .quality-notComparable { color: var(--muted); font-weight: 650; }

    .model-training-log {
      margin-top: 1px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--control-bg);
    }

    .model-training-log summary {
      padding: 8px 10px;
      color: var(--muted);
      font-size: 11px;
      cursor: pointer;
    }

    .model-training-log pre {
      max-height: 170px;
      margin: 0;
      padding: 9px 10px;
      overflow: auto;
      border-top: 1px solid var(--line);
      color: var(--text);
      font-size: 10px;
      line-height: 1.5;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .scroll {
      max-height: 560px;
      overflow: auto;
    }

    .scroll thead th {
      position: sticky;
      top: 0;
      z-index: 1;
    }

    .schedule-summary {
      display: grid;
      grid-template-columns: repeat(7, minmax(140px, 1fr));
      gap: 10px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--subpanel-bg);
    }

    .mini-stat {
      min-width: 0;
    }

    .mini-stat .label {
      color: var(--muted);
      font-size: 12px;
    }

    .mini-stat .value {
      margin-top: 4px;
      font-size: 18px;
      font-weight: 730;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .db-search {
      width: min(460px, 52vw);
    }

    .sticker-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-width: 240px;
      white-space: normal;
    }

    .sticker-pill {
      display: grid;
      grid-template-columns: 30px minmax(120px, 1fr);
      gap: 8px;
      align-items: center;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: rgba(0, 0, 0, 0.12);
      padding: 4px 6px;
    }

    .sticker-pill img,
    .sticker-pill .no-thumb {
      width: 28px;
      height: 28px;
    }

    .sticker-meta {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }

    .skin-link {
      display: inline;
      border: 0;
      background: transparent;
      color: var(--text);
      padding: 0;
      font: inherit;
      text-align: left;
      cursor: pointer;
    }

    .sticker-link {
      display: inline;
      border: 0;
      background: transparent;
      color: var(--text);
      padding: 0;
      font: inherit;
      text-align: left;
      cursor: pointer;
    }

    .skin-link:hover {
      color: var(--accent);
      text-decoration: underline;
    }

    .sticker-link:hover {
      color: var(--accent);
      text-decoration: underline;
    }

    .skin-detail-summary {
      display: grid;
      grid-template-columns: repeat(8, minmax(120px, 1fr));
      gap: 10px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--subpanel-bg);
    }

    .skin-detail-layout {
      display: grid;
      grid-template-columns: minmax(0, 1.7fr) minmax(360px, 0.8fr);
      gap: 12px;
      padding: 12px 14px 14px;
    }

    .chart-wrap {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--subpanel-bg);
      overflow: hidden;
    }

    .chart-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 40px;
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
    }

    .price-chart {
      width: 100%;
      height: 380px;
      display: block;
      background: #0b0c0f;
    }

    .chart-point {
      cursor: pointer;
    }

    .chart-point:hover {
      stroke-width: 3;
    }

    .sale-detail {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--subpanel-bg);
      overflow: hidden;
    }

    .sale-detail-body {
      padding: 12px;
      display: grid;
      gap: 10px;
    }

    .detail-row {
      display: grid;
      grid-template-columns: 128px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
    }

    .detail-row .label {
      color: var(--muted);
      font-size: 12px;
    }

    .detail-row .value {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .detail-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    @media (max-width: 1100px) {
      .metrics {
        grid-template-columns: repeat(3, minmax(140px, 1fr));
      }

      .schedule-summary {
        grid-template-columns: repeat(3, minmax(140px, 1fr));
      }

      .skin-detail-summary {
        grid-template-columns: repeat(4, minmax(120px, 1fr));
      }

      .skin-detail-layout {
        grid-template-columns: 1fr;
      }

      .two {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 680px) {
      .wrap {
        width: min(100vw - 20px, 1500px);
      }

      .topbar {
        align-items: flex-start;
        flex-direction: column;
      }

      .top-actions, .control {
        width: 100%;
      }

      .top-actions .button {
        flex: 1 1 130px;
      }

      .control select {
        flex: 1 1 auto;
        max-width: none;
      }

      .metrics {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .model-health-grid {
        grid-template-columns: 1fr;
      }

      .model-health-card:last-child:nth-child(odd) {
        grid-column: auto;
      }

      .model-training-actions { align-items: stretch; flex-direction: column; }
      .model-training-actions .button { width: 100%; }

      .schedule-summary {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .skin-detail-summary {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .price-chart {
        height: 300px;
      }

      .metric .value {
        font-size: 20px;
      }

      th, td {
        padding: 8px;
      }
    }
  </style>
</head>
<body class="page-dashboard">
  <header>
    <div class="wrap topbar">
      <a class="site-brand" href="/" aria-label="CS2 Intelligence home">
        <span class="brand-mark" aria-hidden="true">C2</span>
        <span><strong>CS2 Intelligence</strong><small id="lastUpdate">waiting for data</small></span>
      </a>
      <nav class="site-nav" aria-label="Main navigation">
        <a class="nav-link" data-nav-path="/" href="/">Overview</a>
        <a class="nav-link" data-nav-path="/deals" href="/deals?token=__DASHBOARD_TOKEN__">Deals</a>
        <a class="nav-link" data-nav-path="/strategy" href="/strategy?token=__DASHBOARD_TOKEN__">Strategy</a>
        <a class="nav-link" data-nav-path="/deal" href="/deal?token=__DASHBOARD_TOKEN__">Сравнение</a>
        <a class="nav-link" data-nav-path="/account" href="/account">Инвентарь</a>
      </nav>
      <div class="top-actions header-tools">
        <label class="control">
          <span class="control-label">Theme</span>
          <select id="themeSelect" title="Theme">
            <option value="dark">Dark</option>
            <option value="light">Light</option>
            <option value="midnight">Midnight</option>
            <option value="graphite">Graphite</option>
          </select>
        </label>
        <a class="button button-quiet" id="downloadDbLink" href="/download/db?token=__DASHBOARD_TOKEN__">Export DB</a>
        <div class="pill" id="dbName">csfloat_history.db</div>
      </div>
    </div>
  </header>

  <main class="wrap">
    <section class="dashboard-hero" aria-labelledby="dashboardTitle">
      <div>
        <div class="eyebrow">LIVE MARKET WORKSPACE</div>
        <h1 id="dashboardTitle">Everything important, one glance away.</h1>
        <p>Monitor collection health, investigate sales and move from signal to decision without digging through one endless page.</p>
      </div>
      <div class="hero-actions">
        <a class="button button-primary" href="/deals?token=__DASHBOARD_TOKEN__">Open profitable deals</a>
        <a class="button" href="/deal?token=__DASHBOARD_TOKEN__">Check a skin</a>
      </div>
    </section>

    <div class="health-banner" id="systemHealthBanner" data-tone="neutral" role="status" aria-live="polite">
      <span class="health-icon" aria-hidden="true"></span>
      <div><strong id="systemHealthTitle">Checking system health</strong><span id="systemHealthText">Waiting for the first dashboard update.</span></div>
    </div>

    <nav class="workspace-tabs" aria-label="Dashboard sections" role="tablist">
      <button class="workspace-tab" type="button" role="tab" data-dashboard-tab="overview" aria-selected="true">
        <span>Overview</span><small>health &amp; models</small>
      </button>
      <button class="workspace-tab" type="button" role="tab" data-dashboard-tab="system" aria-selected="false">
        <span>System</span><small>scanner, parser &amp; database</small>
      </button>
    </nav>

    <div class="view-heading" data-dashboard-view="overview">
      <div><span class="eyebrow">OVERVIEW</span><h2>System at a glance</h2></div>
      <span class="view-description">Live collection totals, model health and recent opportunities.</span>
    </div>

    <section class="grid metrics" data-dashboard-view="overview">
      <div class="panel metric">
        <div class="label">Progress</div>
        <div class="value" id="progressValue">0 / 0</div>
        <div class="progress"><div id="progressBar"></div></div>
        <div class="hint" id="progressHint">current skin</div>
      </div>
      <div class="panel metric">
        <div class="label">Sales Rows</div>
        <div class="value" id="salesCount">0</div>
        <div class="hint" id="latestFetched">latest fetched</div>
      </div>
      <div class="panel metric">
        <div class="label">Skin Variants</div>
        <div class="value" id="skinCount">0</div>
        <div class="hint">unique market_hash_name</div>
      </div>
      <div class="panel metric">
        <div class="label">Sticker Rows</div>
        <div class="value" id="stickerCount">0</div>
        <div class="hint" id="stickeredListings">with stickers</div>
      </div>
      <div class="panel metric metric-technical">
        <div class="label">Cycle Requests</div>
        <div class="value" id="cycleRequests">0</div>
        <div class="hint" id="cycleSales">sales this cycle</div>
      </div>
      <div class="panel metric metric-technical">
        <div class="label">Sleep</div>
        <div class="value" id="sleepState">no</div>
        <div class="hint" id="sleepHint">parser active</div>
      </div>
    </section>

    <section class="panel full" data-dashboard-view="overview">
      <div class="section-head">
        <h2>Model Freshness &amp; Quality</h2>
        <span class="pill" id="modelFreshnessInfo">compared with current DB</span>
      </div>
      <p class="model-isolation-note">Almaz, Igor and Nova use weapon skins only. Knives and gloves have isolated data, artifacts and quality gates.</p>
      <div class="model-health-grid" id="modelHealthCards">
        <div class="muted">loading model artifacts</div>
      </div>
    </section>

    <div class="view-heading" data-dashboard-view="system" hidden>
      <div><span class="eyebrow">SYSTEM</span><h2>Services and diagnostics</h2></div>
      <span class="view-description">Scanner targets, parser health and raw data tools in one place.</span>
    </div>

    <section class="panel full service-control-slot" id="dealScannerControlPanel" data-dashboard-view="system" data-scanner-control-slot hidden>
      <div class="section-head">
        <div><span class="eyebrow">SERVICE</span><h2>Deal Scanner</h2></div>
        <span class="pill" id="dealScannerRuntimeBadge">runtime controls</span>
      </div>
      <div class="service-control-body" id="dealScannerControlsMount">
        <div class="scanner-control-layout">
          <div class="scanner-control-copy">
            <strong>Поиск выгодных скинов</strong>
            <p>Останавливает только поиск активных сделок. Основной парсер истории продаж продолжит работать.</p>
            <div class="scanner-runtime-summary" aria-live="polite">
              <span><small>Процесс</small><b id="scannerProcessState">проверяем…</b></span>
              <span><small>Последний цикл</small><b id="scannerLastCycle">нет данных</b></span>
              <span><small>Следующий запуск</small><b id="scannerNextCycle">нет данных</b></span>
              <span><small>Результат цикла</small><b id="scannerCycleResult">—</b></span>
            </div>
          </div>
          <div class="scanner-control-actions">
            <label for="scannerIntervalMinutes">
              <span>Интервал между началами циклов</span>
              <span class="scanner-interval-input">
                <input id="scannerIntervalMinutes" type="number" min="1" max="1440" step="1" inputmode="numeric" value="10" list="scannerIntervalPresets" disabled>
                <span>мин</span>
              </span>
            </label>
            <datalist id="scannerIntervalPresets">
              <option value="1"></option><option value="5"></option><option value="10"></option>
              <option value="15"></option><option value="30"></option><option value="60"></option>
            </datalist>
            <div class="scanner-action-row">
              <button class="button" id="scannerApplyInterval" type="button" disabled>Применить интервал</button>
              <button class="button button-primary" id="scannerToggleButton" type="button" disabled>Загрузка…</button>
            </div>
            <span class="scanner-feedback" id="scannerControlFeedback" role="status" aria-live="polite">Изменения применяются без перезапуска.</span>
          </div>
        </div>
        <div class="scanner-target-shell">
          <div class="scanner-target-head">
            <div>
              <strong>Что именно искать</strong>
              <p>Выбери профиль, посмотри точные цели и при необходимости собери собственный список. Лимиты запросов на цикл от выбора не увеличиваются.</p>
            </div>
            <button class="button" id="scannerTargetsRefresh" type="button">Обновить список</button>
          </div>
          <div class="scanner-profile-grid" id="scannerProfileGrid">
            <div class="muted">Загружаем профили…</div>
          </div>
          <section class="scanner-target-editor" id="scannerTargetEditor" hidden>
            <div class="scanner-target-editor-head">
              <div>
                <span class="eyebrow" id="scannerTargetEyebrow">ПРОФИЛЬ</span>
                <h3 id="scannerTargetTitle">Цели сканирования</h3>
                <p id="scannerTargetDescription"></p>
              </div>
              <span class="pill" id="scannerTargetReadiness">проверяем…</span>
            </div>
            <div class="scanner-target-toolbar">
              <label><span>Режим</span><select id="scannerTargetMode"><option value="recommended">Рекомендованные</option><option value="custom">Свой список</option></select></label>
              <label class="scanner-target-enabled"><input id="scannerTargetEnabled" type="checkbox"><span>Профиль включён</span></label>
              <label class="scanner-target-search"><span>Поиск по точному названию</span><input id="scannerTargetSearch" maxlength="160" placeholder="Название скина, ножа или наклейки"></label>
              <button class="button" id="scannerTargetSearchButton" type="button">Найти</button>
            </div>
            <div class="scanner-target-summary" id="scannerTargetSummary"></div>
            <div class="scanner-target-bulk">
              <button class="button" id="scannerTargetSelectVisible" type="button">Выбрать показанные</button>
              <button class="button" id="scannerTargetClearVisible" type="button">Убрать показанные</button>
              <button class="button" id="scannerTargetResetOverrides" type="button">Сбросить ручные правки</button>
              <span class="scanner-feedback" id="scannerTargetFeedback" role="status" aria-live="polite">Изменения применятся со следующего цикла.</span>
              <button class="button button-primary" id="scannerTargetSave" type="button" disabled>Сохранить профиль</button>
            </div>
            <div class="scroll scanner-target-scroll">
              <table>
                <thead><tr><th style="width:52px;">Парсить</th><th>Точная цель</th><th style="width:230px;">Почему в списке</th><th style="width:160px;">Статистика</th></tr></thead>
                <tbody id="scannerTargetBody"></tbody>
              </table>
            </div>
            <div class="scanner-target-pages">
              <button class="button" id="scannerTargetPrev" type="button">Назад</button>
              <span class="pill" id="scannerTargetPageInfo">—</span>
              <button class="button" id="scannerTargetNext" type="button">Далее</button>
            </div>
          </section>
        </div>
      </div>
    </section>

    <section class="panel full" data-dashboard-view="system" hidden>
      <div class="section-head">
        <h2>API Keys</h2>
        <span class="pill">limit, remaining, reset</span>
      </div>
      <table>
        <thead>
          <tr>
            <th style="width: 80px;">State</th>
            <th>Key File</th>
            <th>Proxy</th>
            <th style="width: 100px;">Limit</th>
            <th style="width: 120px;">Remaining</th>
            <th style="width: 180px;">Reset MSK</th>
            <th style="width: 130px;">Blocked</th>
            <th style="width: 120px;">HTTP</th>
          </tr>
        </thead>
        <tbody id="keysBody"></tbody>
      </table>
    </section>

    <section class="panel full legacy-dashboard-duplicate" data-dashboard-view="overview">
      <div class="section-head">
        <h2>Model Deal Candidates</h2>
        <div class="section-actions">
          <input class="db-search" id="dealCandidateSearch" placeholder="Search skin or source sticker">
          <button class="button" id="dealCandidateSearchButton" type="button">Search</button>
          <button class="button" id="dealCandidatePrevButton" type="button">Prev</button>
          <button class="button" id="dealCandidateNextButton" type="button">Next</button>
          <span class="pill" id="dealCandidateInfo">scanner results</span>
        </div>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th style="width: 52px;">#</th>
              <th>Skin</th>
              <th style="width: 120px;">Buy</th>
              <th style="width: 120px;">Predicted</th>
              <th style="width: 120px;">Net</th>
              <th style="width: 120px;">Profit</th>
              <th style="width: 95px;">ROI</th>
              <th style="width: 95px;">Liquid</th>
              <th style="width: 150px;">Verdict</th>
              <th style="width: 180px;">Source Sticker</th>
              <th style="width: 160px;">Seen MSK</th>
              <th style="width: 130px;">Links</th>
            </tr>
          </thead>
          <tbody id="dealCandidatesBody"></tbody>
        </table>
      </div>
    </section>

    <section class="panel full" data-dashboard-view="system" hidden>
      <div class="section-head">
        <h2>Parser Schedule</h2>
        <div class="section-actions">
          <input class="db-search" id="scheduleSearch" placeholder="Search scheduled skin">
          <button class="button" id="scheduleSearchButton" type="button">Search</button>
          <button class="button" id="schedulePrevButton" type="button">Prev</button>
          <button class="button" id="scheduleNextButton" type="button">Next</button>
          <span class="pill" id="scheduleBrowserInfo">target 20 new sales</span>
        </div>
      </div>
      <div class="schedule-summary">
        <div class="mini-stat">
          <div class="label">Due Now</div>
          <div class="value" id="scheduleDueNow">0</div>
        </div>
        <div class="mini-stat">
          <div class="label">Never Fetched</div>
          <div class="value" id="scheduleNeverFetched">0</div>
        </div>
        <div class="mini-stat">
          <div class="label">Median Interval</div>
          <div class="value" id="scheduleMedianInterval">unknown</div>
        </div>
        <div class="mini-stat">
          <div class="label">Min / Max Interval</div>
          <div class="value" id="scheduleIntervalRange">unknown</div>
        </div>
        <div class="mini-stat">
          <div class="label">Next Request</div>
          <div class="value" id="scheduleNextRequest">unknown</div>
        </div>
        <div class="mini-stat">
          <div class="label">New Last 100</div>
          <div class="value" id="requestLogNew100">0</div>
        </div>
        <div class="mini-stat">
          <div class="label">Avg New / Request</div>
          <div class="value" id="requestLogAvgNew100">0</div>
        </div>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th style="width: 52px;">#</th>
              <th>Skin</th>
              <th style="width: 140px;">State</th>
              <th style="width: 130px;">Next In</th>
              <th style="width: 180px;">Next MSK</th>
              <th style="width: 130px;">Interval</th>
              <th style="width: 110px;">Last</th>
              <th style="width: 110px;">New</th>
              <th style="width: 120px;">EMA New</th>
              <th style="width: 150px;">Reason</th>
            </tr>
          </thead>
          <tbody id="scheduleBody"></tbody>
        </table>
      </div>
    </section>

    <section class="panel full" data-dashboard-view="system" hidden>
      <div class="section-head">
        <h2>Recent Parser Requests</h2>
        <span class="pill">latest 20, stats use latest 100</span>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th style="width: 52px;">#</th>
              <th>Skin</th>
              <th style="width: 160px;">Requested MSK</th>
              <th style="width: 110px;">Returned</th>
              <th style="width: 110px;">New</th>
              <th style="width: 130px;">Next In</th>
              <th style="width: 130px;">Interval</th>
              <th style="width: 160px;">Reason</th>
              <th style="width: 190px;">Coverage</th>
            </tr>
          </thead>
          <tbody id="requestLogBody"></tbody>
        </table>
      </div>
    </section>

    <div class="view-heading" data-dashboard-view="market" hidden>
      <div><span class="eyebrow">MARKET EXPLORER</span><h2>Understand the inventory</h2></div>
      <span class="view-description">Compare weapons, tournament stickers and individual sale history.</span>
    </div>

    <section class="panel full" data-dashboard-view="market" hidden>
      <div class="section-head">
        <h2>Weapon Skins</h2>
        <div class="section-actions">
          <select id="weaponSelect"></select>
          <span class="pill" id="weaponInfo">complete local catalog</span>
        </div>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th style="width: 52px;">#</th>
              <th style="width: 68px;">Image</th>
              <th>Skin</th>
              <th style="width: 130px;">Sales Rows</th>
              <th style="width: 130px;">Sticker Rows</th>
              <th style="width: 130px;">With Stickers</th>
              <th style="width: 110px;">Latest Median</th>
              <th style="width: 180px;">Latest Fetched</th>
            </tr>
          </thead>
          <tbody id="weaponSkinsBody"></tbody>
        </table>
      </div>
    </section>

    <section class="panel full" data-dashboard-view="market" hidden>
      <div class="section-head">
        <h2>Tournament Stickers</h2>
        <div class="section-actions">
          <select id="tournamentSelect"></select>
          <span class="pill" id="tournamentInfo">all stickers sorted</span>
        </div>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th style="width: 52px;">#</th>
              <th style="width: 68px;">Image</th>
              <th>Sticker</th>
              <th style="width: 90px;">Type</th>
              <th style="width: 120px;">Rows</th>
              <th style="width: 120px;">Listings</th>
              <th style="width: 110px;">Skins</th>
              <th style="width: 110px;">Avg Price</th>
            </tr>
          </thead>
          <tbody id="tournamentStickersBody"></tbody>
        </table>
      </div>
    </section>

    <section class="grid two" data-dashboard-view="market" hidden>
      <div class="panel">
        <div class="section-head">
          <h2>Popular Stickers</h2>
          <span class="pill">top 30</span>
        </div>
        <table>
          <thead>
            <tr>
              <th style="width: 52px;">#</th>
              <th style="width: 68px;">Image</th>
              <th>Sticker</th>
              <th style="width: 110px;">Rows</th>
              <th style="width: 110px;">Listings</th>
              <th style="width: 100px;">Skins</th>
            </tr>
          </thead>
          <tbody id="stickersBody"></tbody>
        </table>
      </div>

      <div class="panel">
        <div class="section-head">
          <h2>Popular Weapons</h2>
          <span class="pill">by sticker rows</span>
        </div>
        <table>
          <thead>
            <tr>
              <th style="width: 52px;">#</th>
              <th>Weapon</th>
              <th style="width: 110px;">Stickers</th>
              <th style="width: 110px;">Listings</th>
              <th style="width: 100px;">Skins</th>
            </tr>
          </thead>
          <tbody id="weaponsBody"></tbody>
        </table>
      </div>
    </section>

    <section class="panel full" data-dashboard-view="overview">
      <div class="section-head">
        <h2>Recent Sales Saved</h2>
        <span class="pill">latest fetched rows</span>
      </div>
      <table>
        <thead>
          <tr>
            <th style="width: 68px;">Image</th>
            <th>Skin</th>
            <th style="width: 110px;">Price</th>
            <th style="width: 100px;">Float</th>
            <th style="width: 180px;">Sold MSK</th>
            <th style="width: 180px;">Fetched MSK</th>
          </tr>
        </thead>
        <tbody id="recentBody"></tbody>
      </table>
    </section>

    <section class="panel full" id="skinDetailPanel" data-dashboard-view="market" hidden>
      <div class="section-head">
        <h2>Skin Detail</h2>
        <div class="section-actions">
          <input class="db-search" id="skinDetailSearch" placeholder="Exact skin name, e.g. AK-47 | Slate (Field-Tested)">
          <button class="button" id="skinDetailSearchButton" type="button">Open</button>
          <span class="pill" id="skinDetailInfo">click a skin name</span>
        </div>
      </div>
      <div class="skin-detail-summary">
        <div class="mini-stat">
          <div class="label">Sales</div>
          <div class="value" id="skinDetailSales">0</div>
        </div>
        <div class="mini-stat">
          <div class="label">Avg Price</div>
          <div class="value" id="skinDetailAvgPrice">unknown</div>
        </div>
        <div class="mini-stat">
          <div class="label">Median Price</div>
          <div class="value" id="skinDetailMedianPrice">unknown</div>
        </div>
        <div class="mini-stat">
          <div class="label">Min / Max Price</div>
          <div class="value" id="skinDetailRange">unknown</div>
        </div>
        <div class="mini-stat">
          <div class="label">Avg Float</div>
          <div class="value" id="skinDetailAvgFloat">unknown</div>
        </div>
        <div class="mini-stat">
          <div class="label">Stickered</div>
          <div class="value" id="skinDetailStickered">0</div>
        </div>
        <div class="mini-stat">
          <div class="label">Avg Sticker Value</div>
          <div class="value" id="skinDetailStickerValue">unknown</div>
        </div>
        <div class="mini-stat">
          <div class="label">Next Parse</div>
          <div class="value" id="skinDetailNextParse">unknown</div>
        </div>
      </div>
      <div class="skin-detail-layout">
        <div class="chart-wrap">
          <div class="chart-title">
            <span id="skinChartTitle">price history</span>
            <span id="skinChartMeta">no skin selected</span>
          </div>
          <svg class="price-chart" id="skinPriceChart" role="img" aria-label="Skin price chart"></svg>
        </div>
        <div class="sale-detail">
          <div class="chart-title">
            <span>Selected Sale</span>
            <span id="selectedSaleMeta">click a chart point</span>
          </div>
          <div class="sale-detail-body" id="selectedSaleBody">
            <div class="muted">Open a skin and click a point on the chart.</div>
          </div>
        </div>
      </div>
    </section>

    <section class="panel full" id="stickerDetailPanel" data-dashboard-view="market" hidden>
      <div class="section-head">
        <h2>Sticker Detail</h2>
        <div class="section-actions">
          <input class="db-search" id="stickerDetailSearch" placeholder="Sticker name, e.g. Sticker | iBUYPOWER | Katowice 2014">
          <button class="button" id="stickerDetailSearchButton" type="button">Open</button>
          <span class="pill" id="stickerDetailInfo">click a sticker name</span>
        </div>
      </div>
      <div class="skin-detail-summary">
        <div class="mini-stat">
          <div class="label">Sticker Rows</div>
          <div class="value" id="stickerDetailRows">0</div>
        </div>
        <div class="mini-stat">
          <div class="label">Listings</div>
          <div class="value" id="stickerDetailListings">0</div>
        </div>
        <div class="mini-stat">
          <div class="label">Skins</div>
          <div class="value" id="stickerDetailSkins">0</div>
        </div>
        <div class="mini-stat">
          <div class="label">Avg Skin Price</div>
          <div class="value" id="stickerDetailAvgSkinPrice">unknown</div>
        </div>
        <div class="mini-stat">
          <div class="label">Median Skin Price</div>
          <div class="value" id="stickerDetailMedianSkinPrice">unknown</div>
        </div>
        <div class="mini-stat">
          <div class="label">Avg Sticker Price</div>
          <div class="value" id="stickerDetailAvgStickerPrice">unknown</div>
        </div>
        <div class="mini-stat">
          <div class="label">Max Sticker Stack</div>
          <div class="value" id="stickerDetailMaxStickerValue">unknown</div>
        </div>
        <div class="mini-stat">
          <div class="label">Latest Sale</div>
          <div class="value" id="stickerDetailLatestSale">unknown</div>
        </div>
      </div>
      <div class="skin-detail-layout">
        <div class="grid">
          <div class="chart-wrap">
            <div class="chart-title">
              <span id="stickerChartTitle">sales with sticker</span>
              <span id="stickerChartMeta">no sticker selected</span>
            </div>
            <svg class="price-chart" id="stickerPriceChart" role="img" aria-label="Sticker sales chart"></svg>
          </div>
          <div class="chart-wrap">
            <div class="chart-title">
              <span>Skins With This Sticker</span>
              <span id="stickerTopSkinsMeta">sorted by listings</span>
            </div>
            <svg class="price-chart" id="stickerSkinChart" style="height: 260px;" role="img" aria-label="Skins with sticker chart"></svg>
            <div class="scroll" style="max-height: 360px;">
              <table>
                <thead>
                  <tr>
                    <th style="width: 52px;">#</th>
                    <th style="width: 68px;">Image</th>
                    <th>Skin</th>
                    <th style="width: 110px;">Listings</th>
                    <th style="width: 110px;">Sticker Rows</th>
                    <th style="width: 110px;">Avg Price</th>
                    <th style="width: 180px;">Latest Sold</th>
                  </tr>
                </thead>
                <tbody id="stickerTopSkinsBody"></tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="sale-detail">
          <div class="chart-title">
            <span>Selected Sticker Sale</span>
            <span id="selectedStickerSaleMeta">click a chart point</span>
          </div>
          <div class="sale-detail-body" id="selectedStickerSaleBody">
            <div class="muted">Open a sticker and click a point on the chart.</div>
          </div>
        </div>
      </div>
    </section>

    <div class="view-heading legacy-dashboard-duplicate" data-dashboard-view="system" hidden>
      <div><span class="eyebrow">DATABASE</span><h2>Search the source data</h2></div>
      <span class="view-description">Inspect normalized listings and applied sticker metadata.</span>
    </div>

    <section class="panel full" data-dashboard-view="system" hidden>
      <div class="section-head">
        <h2>Database Browser</h2>
        <div class="section-actions">
          <select id="dbSearchScope" aria-label="Search scope">
            <option value="skin">Скин</option>
            <option value="sticker">Наклейка на скине</option>
            <option value="listing">ID листинга</option>
            <option value="all">Везде</option>
          </select>
          <input class="db-search" id="dbSearch" placeholder="Например: AK, MAC-10 или Asiimov">
          <button class="button" id="dbSearchButton" type="button">Search</button>
          <button class="button" id="dbPrevButton" type="button">Prev</button>
          <button class="button" id="dbNextButton" type="button">Next</button>
          <span class="pill" id="dbBrowserInfo">loading</span>
        </div>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th style="width: 68px;">Image</th>
              <th style="width: 320px;">Skin</th>
              <th style="width: 110px;">Price</th>
              <th style="width: 100px;">Float</th>
              <th style="width: 90px;">Seed</th>
              <th style="width: 90px;">Paint</th>
              <th>Applied Stickers</th>
              <th style="width: 180px;">Sold MSK</th>
              <th style="width: 180px;">Fetched MSK</th>
            </tr>
          </thead>
          <tbody id="dbBrowserBody"></tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
    const fmt = new Intl.NumberFormat('__CS2_INTL_LOCALE__');
    const moneyFmt = new Intl.NumberFormat('__CS2_INTL_LOCALE__', { style: 'currency', currency: 'USD' });
    const dashboardToken = new URLSearchParams(window.location.search).get('token') || '__DASHBOARD_TOKEN__';
    document.getElementById('downloadDbLink').href = `/download/db?token=${encodeURIComponent(dashboardToken)}`;
    let selectedWeapon = '';
    let selectedTournament = '';
    let loadedWeapon = '';
    let loadedTournament = '';
    let detailRefreshRunning = false;
    let scheduleOffset = 0;
    const scheduleLimit = 100;
    let dbOffset = 0;
    const dbLimit = 50;
    let activeDashboardView = 'overview';
    let dbBrowserLoaded = false;
    let dbBrowserLoading = false;
    let dbBrowserGeneration = 0;
    let dbBrowserController = null;
    let dealCandidateOffset = 0;
    const dealCandidateLimit = 50;
    let selectedSkinName = '';
    let selectedSkinDetail = null;
    let selectedSkinSales = [];
    let selectedStickerName = '';
    let selectedStickerId = '';
    let selectedStickerDetail = null;
    let selectedStickerSales = [];
    let scannerControlBusy = false;
    let scannerControlGeneration = 0;
    let scannerControlPayload = null;
    let scannerTargetsPayload = null;
    let scannerTargetPreview = null;
    let scannerTargetProfile = 'sticker_guns';
    let scannerTargetOffset = 0;
    const scannerTargetLimit = 50;
    let scannerTargetBusy = false;
    let scannerTargetGeneration = 0;
    const scannerTargetDrafts = new Map();
    let modelTrainingPayload = null;
    let modelTrainingTimer = null;
    let modelTrainingRequestRunning = false;
    const modelTrainingBusy = new Set();
    const modelFreshnessByName = new Map();
    const SCANNER_TARGET_COPY = Object.freeze({
      sticker_guns: {
        title: 'Оружие со стикерами',
        short: 'Лоты с выбранными наклейками: Katowice, Crown и ваш список.',
        description: 'Цель здесь — наклейка. Сканер ищет активные оружейные скины, на которых она установлена, а не отдельную наклейку.',
      },
      liquid_under_10: {
        title: 'Ликвидные скины до $10',
        short: 'Дешёвые активные лоты с оборотом, float- или sticker-возможностью.',
        description: 'Точные market hash names дешевле или равные $10 ранжируются по цене, количеству на рынке и измеренной скорости продаж.',
      },
      knives: {
        title: 'Ножи',
        short: 'Полный каталог ножей через безопасную ротацию и отдельную модель.',
        description: 'Ножи обходятся окнами по всему локальному каталогу и никогда не оцениваются моделями обычных оружейных скинов.',
      },
      gloves: {
        title: 'Перчатки',
        short: 'Отдельный профиль без подмены моделью ножей или оружия.',
        description: 'Цели уже можно выбрать, но сетевые запросы останутся заблокированы, пока не появится проверенная отдельная модель перчаток.',
      },
    });
    const TRAINABLE_MODEL_NAMES = Object.freeze(['almaz', 'igor', 'nova', 'knives', 'gloves']);
    const MODEL_TRAINING_ACTIVE_STATES = new Set(['starting', 'running', 'cancelling']);
    const TRAINING_UI_COPY = Object.freeze({
      ru: {
        baseline: 'Рабочая модель и обучающие данные',
        weaponSkin: 'только оружейные скины', knife: 'только ножи', glove: 'только перчатки',
        data: 'Данные', features: 'Признаки', train: 'Обучение', quality: 'Сравнение', activation: 'Установка',
        comparison: 'Новая модель против рабочей', noComparison: 'Подробное сравнение появится после общего holdout-теста.',
        metric: 'Метрика', old: 'Рабочая', candidate: 'Новая', change: 'Изменение', result: 'Итог',
        better: 'лучше', worse: 'хуже', equal: 'без изменений',
        notComparable: 'нет рабочей базы',
        holdout: 'общий holdout', rows: 'строк', passed: 'порог пройден', rejected: 'порог не пройден',
        bootstrap: 'первая отдельная модель — рабочей базы ещё нет',
        integrity: 'качество не сравнивалось: техническая проверка целостности не пройдена',
        medianApe: 'Медианная ошибка', meanApe: 'Средняя ошибка', p90Ape: 'P90 ошибки',
        maeUsd: 'MAE, $', rmseLog: 'RMSE log-price', r2: 'R² log-price',
        intervalCoverage: 'Покрытие q15–q85', quantileCrossing: 'Пересечение квантилей', rowWin: 'Побед по строкам',
        medianApeHelp: 'типичная относительная ошибка', meanApeHelp: 'учитывает дорогие промахи и выбросы',
        p90ApeHelp: 'ошибка в сложных 10% примеров', maeUsdHelp: 'средний промах цены в долларах',
        rmseLogHelp: 'штрафует крупные относительные промахи', r2Help: 'выше и ближе к 1 — лучше',
        intervalCoverageHelp: 'доля цены внутри прогноза q15–q85', quantileCrossingHelp: 'должно быть близко к 0%',
        rowWinHelp: 'доля примеров, где новая модель точнее',
        legacyKindWarning: 'Рабочая версия создана до полного контракта разделения типов. Она доступна временно, но требуется строгое переобучение.',
      },
      en: {
        baseline: 'Active model and training data',
        weaponSkin: 'weapon skins only', knife: 'knives only', glove: 'gloves only',
        data: 'Data', features: 'Features', train: 'Training', quality: 'Comparison', activation: 'Activation',
        comparison: 'Candidate versus active model', noComparison: 'A detailed comparison appears after the shared holdout test.',
        metric: 'Metric', old: 'Active', candidate: 'Candidate', change: 'Change', result: 'Result',
        better: 'better', worse: 'worse', equal: 'unchanged',
        notComparable: 'no active baseline',
        holdout: 'shared holdout', rows: 'rows', passed: 'gate passed', rejected: 'gate rejected',
        bootstrap: 'first isolated model — no active baseline yet',
        integrity: 'quality was not compared: the technical integrity check failed',
        medianApe: 'Median error', meanApe: 'Mean error', p90Ape: 'P90 error',
        maeUsd: 'MAE, $', rmseLog: 'RMSE log-price', r2: 'R² log-price',
        intervalCoverage: 'q15–q85 coverage', quantileCrossing: 'Quantile crossing', rowWin: 'Row wins',
        medianApeHelp: 'typical relative error', meanApeHelp: 'includes costly misses and outliers',
        p90ApeHelp: 'error across the hardest 10% of examples', maeUsdHelp: 'average price miss in dollars',
        rmseLogHelp: 'penalizes large relative misses', r2Help: 'higher and closer to 1 is better',
        intervalCoverageHelp: 'share of prices inside the q15–q85 interval', quantileCrossingHelp: 'should stay close to 0%',
        rowWinHelp: 'examples where the candidate is more accurate',
        legacyKindWarning: 'The active version predates the complete item-kind contract. It remains temporarily available, but a strict retrain is required.',
      },
    });
    const trainingUiText = key => {
      const locale = document.documentElement.lang.toLowerCase().startsWith('en') ? 'en' : 'ru';
      return TRAINING_UI_COPY[locale][key] || key;
    };
    const dashboardViews = new Set(['overview', 'system']);

    function setDashboardView(requestedView, persist = true) {
      const view = dashboardViews.has(requestedView) ? requestedView : 'overview';
      activeDashboardView = view;
      document.querySelectorAll('[data-dashboard-view]').forEach(element => {
        element.hidden = element.dataset.dashboardView !== view;
      });
      document.querySelectorAll('[data-dashboard-tab]').forEach(tab => {
        const active = tab.dataset.dashboardTab === view;
        tab.setAttribute('aria-selected', active ? 'true' : 'false');
        tab.tabIndex = active ? 0 : -1;
      });
      if (persist) {
        try { localStorage.setItem('dashboardView', view); } catch (error) {}
      }
      if (view === 'system' && !scannerTargetsPayload && !scannerTargetBusy) {
        refreshScannerTargets({ refreshControl: true });
      }
      if (view === 'system') refreshScheduleBrowser();
      if (view === 'system' && !dbBrowserLoaded && !dbBrowserLoading) {
        refreshDbBrowser();
      }
    }

    function renderSystemHealth(data) {
      const status = data.status || {};
      const age = secondsSince(status.updated_at);
      const keys = Array.isArray(status.keys) ? status.keys : [];
      const availableKeys = keys.filter(key =>
        !Number(key.blocked_seconds_left || 0) &&
        !Number(key.transport_blocked_seconds_left || 0) &&
        Number(key.rate_remaining ?? 1) !== 0
      ).length;
      const modelStates = ((data.model_freshness || {}).models || [])
        .map(model => model.freshness)
        .filter(Boolean);
      const staleModels = modelStates.filter(state =>
        ['retrain_recommended', 'stale', 'data_mismatch', 'missing'].includes(state)
      ).length;
      const banner = document.getElementById('systemHealthBanner');
      let tone = 'good';
      let title = 'All systems look healthy';
      let message = `${fmt.format(availableKeys)} API routes ready · parser heartbeat ${age === null ? 'unknown' : `${seconds(age)} ago`}.`;

      if (age === null || age > 300 || (keys.length && !availableKeys)) {
        tone = 'bad';
        title = age === null || age > 300 ? 'Parser needs attention' : 'No API routes are ready';
        message = age === null
          ? 'No parser heartbeat is available yet. Open the Parser tab for diagnostics.'
          : `Last parser heartbeat was ${seconds(age)} ago. Open the Parser tab to inspect keys and schedule.`;
      } else if (age > 120 || staleModels) {
        tone = 'warn';
        title = staleModels ? `${staleModels} model artifact${staleModels === 1 ? '' : 's'} need attention` : 'Parser heartbeat is delayed';
        message = staleModels
          ? 'Fresh data is available after training. Review model quality cards before relying on estimates.'
          : `No parser update for ${seconds(age)}. Collection may be sleeping or temporarily delayed.`;
      }

      banner.dataset.tone = tone;
      text('systemHealthTitle', title);
      text('systemHealthText', message);
    }

    function applyTheme() {
      const select = document.getElementById('themeSelect');
      if (!select) return;
      const value = select.value;
      document.body.classList.remove('theme-light', 'theme-midnight', 'theme-graphite');
      if (value !== 'dark') document.body.classList.add(`theme-${value}`);
      try {
        localStorage.setItem('siteTheme', value);
      } catch (error) {}
    }

    try {
      const savedTheme = localStorage.getItem('siteTheme') || localStorage.getItem('bestDealsTheme');
      if (savedTheme && ['dark', 'light', 'midnight', 'graphite'].includes(savedTheme)) {
        document.getElementById('themeSelect').value = savedTheme;
      }
    } catch (error) {}
    applyTheme();
    document.getElementById('themeSelect').addEventListener('change', applyTheme);

    function text(id, value) {
      const element = document.getElementById(id);
      const normalized = String(value);
      if (element.textContent !== normalized) element.textContent = normalized;
    }

    function msk(value) {
      if (!value) return 'unknown';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return escapeHtml(value);
      return date.toLocaleString('__CS2_INTL_LOCALE__', { timeZone: 'Europe/Moscow' });
    }

    function money(cents) {
      return moneyFmt.format((cents || 0) / 100);
    }

    function seconds(value) {
      if (value === null || value === undefined) return 'unknown';
      value = Math.max(0, Number(value) || 0);
      const d = Math.floor(value / 86400);
      const h = Math.floor(value / 3600);
      const m = Math.floor((value % 3600) / 60);
      const s = value % 60;
      if (d) return `${d}d ${Math.floor((value % 86400) / 3600)}h`;
      if (h) return `${h}h ${m}m`;
      if (m) return `${m}m ${s}s`;
      return `${s}s`;
    }

    function signedSeconds(value) {
      value = Number(value) || 0;
      if (value < 0) return `${seconds(Math.abs(value))} overdue`;
      return seconds(value);
    }

    function secondsSince(value) {
      if (!value) return null;
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return null;
      return Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
    }

    function setScannerFeedback(message, tone = '') {
      const feedback = document.getElementById('scannerControlFeedback');
      if (feedback.textContent === message && (feedback.dataset.tone || '') === tone) return;
      feedback.textContent = message;
      if (tone) feedback.dataset.tone = tone;
      else delete feedback.dataset.tone;
    }

    function scannerStateLabel(state) {
      return ({
        starting: 'запускается',
        scanning: 'сканирует',
        sleeping: 'ожидает',
        paused: 'на паузе',
        stopped: 'остановлен',
        offline: 'не запущен',
        unresponsive: 'нет heartbeat',
      })[state] || 'неизвестно';
    }

    function renderScannerControl(data, { preserveFeedback = false } = {}) {
      scannerControlPayload = data;
      const control = data.control || {};
      const runtime = data.runtime || {};
      const health = data.health || 'offline';
      const enabled = control.enabled === true;
      const controlError = Boolean(control.error);
      const badge = document.getElementById('dealScannerRuntimeBadge');
      const intervalInput = document.getElementById('scannerIntervalMinutes');
      const toggle = document.getElementById('scannerToggleButton');
      const apply = document.getElementById('scannerApplyInterval');

      const badgeStates = {
        scanning: ['Сканирование', 'good'],
        ready: ['Активен', 'good'],
        paused: ['На паузе', 'warn'],
        offline: ['Не запущен', 'bad'],
        unresponsive: ['Нет heartbeat', 'warn'],
        status_unavailable: ['Статус недоступен', 'warn'],
        control_error: ['Ошибка настроек', 'bad'],
      };
      const [badgeText, badgeTone] = badgeStates[health] || ['Неизвестно', 'warn'];
      badge.textContent = badgeText;
      badge.dataset.tone = badgeTone;

      let processText = scannerStateLabel(runtime.effective_state);
      if (health === 'status_unavailable') processText = 'статус недоступен';
      if (runtime.effective_state === 'scanning') {
        processText += ` ${runtime.cycle_targets_scanned || 0}/${runtime.cycle_targets_total || 0}`;
      }
      text('scannerProcessState', processText);
      text('scannerLastCycle', runtime.cycle_completed_at ? msk(runtime.cycle_completed_at) : 'ещё не завершался');
      if (!enabled) {
        text('scannerNextCycle', 'поиск отключён');
      } else if (health === 'status_unavailable') {
        text('scannerNextCycle', 'статус недоступен');
      } else if (runtime.effective_state === 'unresponsive') {
        text('scannerNextCycle', 'ожидаем heartbeat');
      } else if (!runtime.online) {
        text('scannerNextCycle', 'после запуска процесса');
      } else if (runtime.next_scan_in_seconds !== null && runtime.next_scan_in_seconds !== undefined) {
        text('scannerNextCycle', `через ${seconds(runtime.next_scan_in_seconds)}`);
      } else if (runtime.effective_state === 'scanning') {
        text('scannerNextCycle', 'после текущего цикла');
      } else {
        text('scannerNextCycle', 'скоро');
      }
      const scanned = Number(runtime.cycle_targets_scanned || 0);
      const total = Number(runtime.cycle_targets_total || 0);
      const listings = Number(runtime.cycle_listings_returned || 0);
      const saved = Number(runtime.cycle_candidates_saved || 0);
      text('scannerCycleResult', `${scanned}/${total} целей · ${listings} лотов · ${saved} сохранено`);

      if (document.activeElement !== intervalInput && intervalInput.dataset.dirty !== 'true') {
        const minutes = Number(control.interval_seconds || 600) / 60;
        intervalInput.value = Number(minutes.toFixed(2));
      }
      intervalInput.disabled = scannerControlBusy || controlError;
      apply.disabled = scannerControlBusy || controlError;
      toggle.disabled = scannerControlBusy || controlError;
      toggle.textContent = scannerControlBusy
        ? 'Применяем…'
        : (enabled ? 'Приостановить поиск' : 'Включить поиск');
      toggle.classList.toggle('button-primary', !enabled && !scannerControlBusy);

      if (preserveFeedback) return;
      if (controlError) {
        setScannerFeedback('Файл управления повреждён. Поиск безопасно остановлен; проверь deal_scanner_control.json.', 'bad');
      } else if (health === 'status_unavailable') {
        setScannerFeedback('Настройка сохранена, но runtime-статус временно не читается. Не повторяй изменение вслепую.', 'warn');
      } else if (runtime.last_error) {
        setScannerFeedback(`Последняя ошибка: ${runtime.last_error}`, 'bad');
      } else if (!enabled) {
        setScannerFeedback('Новые запросы выгодных лотов приостановлены. Парсер истории продолжает работать.');
      } else if (runtime.effective_state === 'unresponsive') {
        setScannerFeedback('Процесс жив, но давно не обновлял heartbeat. Не запускай второй экземпляр; проверь logs/deal_scanner.log.', 'warn');
      } else if (!runtime.online) {
        setScannerFeedback('Поиск включён, но процесс не запущен. Запусти проект командой ./start_all.sh.', 'bad');
      } else {
        setScannerFeedback(`Интервал ${seconds(control.interval_seconds)} применяется без перезапуска.`, 'good');
      }
    }

    async function refreshScannerControl({ quiet = true } = {}) {
      if (scannerControlBusy) return;
      const generation = ++scannerControlGeneration;
      try {
        const response = await fetch('/api/scanner-control', {
          cache: 'no-store',
          headers: { Accept: 'application/json' },
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        if (generation !== scannerControlGeneration) return;
        renderScannerControl(data, {
          preserveFeedback: quiet && document.getElementById('scannerIntervalMinutes').dataset.dirty === 'true',
        });
      } catch (error) {
        if (generation !== scannerControlGeneration) return;
        document.getElementById('dealScannerRuntimeBadge').textContent = 'Недоступен';
        document.getElementById('dealScannerRuntimeBadge').dataset.tone = 'bad';
        setScannerFeedback(`Не удалось прочитать состояние: ${error.message}`, 'bad');
      }
    }

    async function changeScannerControl(changes, successMessage) {
      scannerControlBusy = true;
      const generation = ++scannerControlGeneration;
      if (scannerControlPayload) renderScannerControl(scannerControlPayload, { preserveFeedback: true });
      setScannerFeedback('Применяем изменение…');
      try {
        const response = await fetch('/api/scanner-control', {
          method: 'POST',
          cache: 'no-store',
          headers: {
            Accept: 'application/json',
            'Content-Type': 'application/json',
            'X-CS2-Action': 'scanner-control',
          },
          body: JSON.stringify(changes),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        if (generation !== scannerControlGeneration) return;
        const intervalInput = document.getElementById('scannerIntervalMinutes');
        if (Object.prototype.hasOwnProperty.call(changes, 'interval_seconds')) {
          delete intervalInput.dataset.dirty;
        }
        scannerControlBusy = false;
        renderScannerControl(data, { preserveFeedback: true });
        setScannerFeedback(successMessage, 'good');
      } catch (error) {
        if (generation !== scannerControlGeneration) return;
        scannerControlBusy = false;
        if (scannerControlPayload) renderScannerControl(scannerControlPayload, { preserveFeedback: true });
        setScannerFeedback(`Изменение не применено: ${error.message}`, 'bad');
      }
    }

    function scannerTargetCopy(profile) {
      return SCANNER_TARGET_COPY[profile] || {
        title: profile,
        short: 'Локальный профиль целей.',
        description: 'Точный список целей Deal Scanner.',
      };
    }

    function scannerUiText(message) {
      return window.cs2T ? window.cs2T(message) : message;
    }

    function scannerTargetSettings(profile) {
      return scannerTargetsPayload?.control?.profiles?.[profile] || null;
    }

    function scannerTargetDraft(profile = scannerTargetProfile) {
      if (!scannerTargetDrafts.has(profile)) {
        const saved = scannerTargetSettings(profile);
        if (!saved) return null;
        scannerTargetDrafts.set(profile, {
          enabled: Boolean(saved.enabled),
          selection_mode: saved.selection_mode || 'recommended',
          include: Array.from(new Set(saved.include || [])),
          exclude: Array.from(new Set(saved.exclude || [])),
          dirty: false,
        });
      }
      return scannerTargetDrafts.get(profile);
    }

    function setScannerTargetFeedback(message, tone = '') {
      const node = document.getElementById('scannerTargetFeedback');
      node.textContent = message;
      node.dataset.tone = tone;
    }

    function scannerTargetBlockedText(reason) {
      const labels = {
        profile_disabled: 'Профиль выключен',
        weapon_models_disabled: 'Модели скинов выключены',
        dedicated_knife_model_unavailable: 'Отдельная модель ножей ещё не готова',
        dedicated_glove_model_unavailable: 'Отдельная модель перчаток ещё не готова',
        dedicated_knife_model_readiness_not_checked: 'Проверяем модель ножей',
      };
      return labels[reason] || (reason ? String(reason) : 'Готов к сканированию');
    }

    function scannerTargetSelectedWithOverrides(item, mode, include, exclude) {
      const key = String(item.target_key || '');
      if (exclude.has(key)) return false;
      if (include.has(key)) return true;
      return mode === 'recommended' && Boolean(item.recommended);
    }

    function scannerTargetSelected(item, draft) {
      if (!draft?.dirty) return Boolean(item.selected);
      return scannerTargetSelectedWithOverrides(
        item,
        draft?.selection_mode,
        new Set(draft?.include || []),
        new Set(draft?.exclude || []),
      );
    }

    function applyScannerTargetChoice(item, checked, mode, include, exclude) {
      const key = String(item.target_key || '');
      if (checked) {
        exclude.delete(key);
        include.add(key);
      } else {
        include.delete(key);
        if (mode === 'recommended') exclude.add(key);
      }
    }

    function renderScannerProfileCards() {
      if (!scannerTargetsPayload) return;
      const profiles = scannerTargetsPayload.profile_order || Object.keys(scannerTargetsPayload.control?.profiles || {});
      const grid = document.getElementById('scannerProfileGrid');
      grid.innerHTML = profiles.map(profile => {
        const copy = scannerTargetCopy(profile);
        const draft = scannerTargetDraft(profile) || {};
        const mode = draft.selection_mode === 'custom' ? 'свой список' : 'рекомендованные';
        const state = draft.enabled ? 'включён' : 'выключен';
        const tone = draft.enabled ? 'good' : 'warn';
        const changed = draft.dirty ? ' · не сохранено' : '';
        return `<button class="scanner-profile-card${profile === scannerTargetProfile ? ' active' : ''}" type="button" data-scanner-profile="${escapeHtml(profile)}" ${scannerTargetBusy ? 'disabled' : ''}>
          <span class="scanner-profile-card-head"><strong>${escapeHtml(copy.title)}</strong><span class="status"><span class="dot ${tone}"></span>${state}</span></span>
          <p>${escapeHtml(copy.short)}</p>
          <span class="scanner-profile-card-foot"><span>${mode}</span><span>+${(draft.include || []).length} вручную</span><span>−${(draft.exclude || []).length}${changed}</span></span>
        </button>`;
      }).join('');
    }

    function scannerTargetStats(profile, item) {
      if (profile === 'sticker_guns') {
        return `${fmt.format(item.seen_count || 0)} наблюдений · ср. ${money(item.avg_price_cents)}`;
      }
      if (profile === 'liquid_under_10') {
        return `${money(item.base_price_cents)} · ${fmt.format(item.quantity_on_market || 0)} на рынке · ${num(item.estimated_sales_per_day, 1)}/день`;
      }
      return item.has_schedule_velocity
        ? `${num(item.estimated_sales_per_day, 2)} продаж/день`
        : 'скорость ещё не измерена';
    }

    function scannerTargetReason(item) {
      const draft = scannerTargetDraft();
      const key = String(item.target_key || '');
      if (draft?.dirty) {
        if ((draft.include || []).includes(key)) return 'добавлено вручную';
        if ((draft.exclude || []).includes(key)) return 'исключено вручную';
        if (item.recommended_candidate || item.recommended) return 'рекомендация по локальным данным';
        return 'найдено по вашему поиску';
      }
      if (item.manually_included) return 'добавлено вручную';
      if (item.excluded) return 'исключено вручную';
      if (item.recommended_candidate || item.recommended) return 'рекомендация по локальным данным';
      return 'найдено по вашему поиску';
    }

    function renderScannerTargetPreview() {
      const preview = scannerTargetPreview;
      const draft = scannerTargetDraft();
      if (!preview || !draft) return;
      const copy = scannerTargetCopy(scannerTargetProfile);
      const editor = document.getElementById('scannerTargetEditor');
      editor.hidden = false;
      text('scannerTargetTitle', copy.title);
      text('scannerTargetDescription', copy.description);
      text('scannerTargetEyebrow', String(scannerTargetProfile).toUpperCase());
      const modeInput = document.getElementById('scannerTargetMode');
      const enabledInput = document.getElementById('scannerTargetEnabled');
      modeInput.value = draft.selection_mode;
      modeInput.disabled = scannerTargetBusy;
      enabledInput.checked = draft.enabled;
      enabledInput.disabled = scannerTargetBusy;
      document.getElementById('scannerTargetSearch').disabled = scannerTargetBusy;
      document.getElementById('scannerTargetSearchButton').disabled = scannerTargetBusy;
      document.getElementById('scannerTargetsRefresh').disabled = scannerTargetBusy;

      const readiness = document.getElementById('scannerTargetReadiness');
      const blocked = preview.blocked_reason;
      const readinessText = preview.scan_ready
        ? scannerUiText('готов к запросам')
        : scannerUiText(scannerTargetBlockedText(blocked));
      const readinessTone = preview.scan_ready
        ? 'good'
        : (blocked === 'profile_disabled' ? 'warn' : 'bad');
      readiness.textContent = draft.dirty
        ? `${readinessText} · ${scannerUiText('не сохранено')}`
        : readinessText;
      readiness.dataset.tone = draft.dirty && readinessTone === 'good'
        ? 'warn'
        : readinessTone;

      const selection = preview.selection || {};
      const budget = preview.budget || {};
      const selectedCount = Number(selection.selected_count || 0);
      const perCycle = Math.max(1, Number(budget.targets_per_cycle || 1));
      const cycles = Math.ceil(selectedCount / perCycle);
      const interval = Number(scannerControlPayload?.control?.interval_seconds || 0);
      const roundTime = interval && cycles ? seconds(interval * cycles) : '—';
      const rotationNote = preview.rotation?.eventual_catalog_coverage
        ? ` · ${scannerUiText('ротация по всему каталогу')}`
        : '';
      const selectedDisplay = draft.dirty
        ? scannerUiText('после сохранения')
        : fmt.format(selectedCount);
      const roundDisplay = draft.dirty
        ? '—'
        : `${fmt.format(cycles)} циклов · ${escapeHtml(roundTime)}${escapeHtml(rotationNote)}`;
      document.getElementById('scannerTargetSummary').innerHTML = `
        <span><small>Доступно локально</small><b>${fmt.format(selection.eligible_count || preview.total_matching || 0)}</b></span>
        <span><small>Выбрано сейчас</small><b>${escapeHtml(selectedDisplay)}</b></span>
        <span><small>Лимит цикла</small><b>${fmt.format(budget.targets_per_cycle || 0)} целей · ${fmt.format(budget.max_requests_per_cycle || 0)} запросов</b></span>
        <span><small>Оценка полного круга</small><b>${roundDisplay}</b></span>`;

      const items = preview.items || [];
      rows('scannerTargetBody', items, item => {
        const selected = scannerTargetSelected(item, draft);
        return `<tr>
          <td><input class="scanner-target-row-check" type="checkbox" data-target-key="${escapeHtml(item.target_key || '')}" ${selected ? 'checked' : ''} ${scannerTargetBusy ? 'disabled' : ''}></td>
          <td><div class="scanner-target-name"><strong>${escapeHtml(item.name || item.market_hash_name || '—')}</strong><small>${escapeHtml(item.target_key || '')}</small></div></td>
          <td>${escapeHtml(scannerTargetReason(item))}</td>
          <td>${escapeHtml(scannerTargetStats(scannerTargetProfile, item))}</td>
        </tr>`;
      });
      if (!items.length) {
        document.getElementById('scannerTargetBody').innerHTML = '<tr><td colspan="4" class="muted">По этому поиску целей нет.</td></tr>';
      }

      const from = preview.total_matching ? Number(preview.offset || 0) + 1 : 0;
      const to = Number(preview.offset || 0) + items.length;
      text('scannerTargetPageInfo', `${fmt.format(from)}–${fmt.format(to)} / ${fmt.format(preview.total_matching || 0)}`);
      document.getElementById('scannerTargetPrev').disabled = scannerTargetBusy || Number(preview.offset || 0) <= 0;
      document.getElementById('scannerTargetNext').disabled = scannerTargetBusy || preview.next_offset === null || preview.next_offset === undefined;
      document.getElementById('scannerTargetSave').disabled = scannerTargetBusy || !draft.dirty;
      document.getElementById('scannerTargetSelectVisible').disabled = scannerTargetBusy || !items.length;
      document.getElementById('scannerTargetClearVisible').disabled = scannerTargetBusy || !items.length;
      document.getElementById('scannerTargetResetOverrides').disabled = scannerTargetBusy ||
        (!(draft.include || []).length && !(draft.exclude || []).length);
      renderScannerProfileCards();
    }

    async function refreshScannerTargets({ refreshControl = false } = {}) {
      if (scannerTargetBusy) return false;
      const generation = ++scannerTargetGeneration;
      let succeeded = false;
      scannerTargetBusy = true;
      setScannerTargetFeedback('Загружаем точный список…');
      renderScannerProfileCards();
      renderScannerTargetPreview();
      try {
        if (refreshControl || !scannerTargetsPayload) {
          const controlResponse = await fetch('/api/scanner-targets', {
            cache: 'no-store',
            headers: { Accept: 'application/json' },
          });
          const controlData = await controlResponse.json();
          if (!controlResponse.ok) throw new Error(controlData.error || `HTTP ${controlResponse.status}`);
          if (generation !== scannerTargetGeneration) return false;
          scannerTargetsPayload = controlData;
          scannerTargetDrafts.clear();
          renderScannerProfileCards();
        }
        const search = document.getElementById('scannerTargetSearch').value.trim();
        const params = new URLSearchParams({
          profile: scannerTargetProfile,
          search,
          limit: String(scannerTargetLimit),
          offset: String(scannerTargetOffset),
        });
        const response = await fetch(`/api/scanner-targets?${params}`, {
          cache: 'no-store',
          headers: { Accept: 'application/json' },
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        if (generation !== scannerTargetGeneration) return false;
        scannerTargetPreview = data;
        succeeded = true;
        const unresolved = data.selection?.unresolved_include || [];
        if (!search && unresolved.length) {
          setScannerTargetFeedback(
            `${unresolved.length} сохранённых целей не найдены локально. Их можно удалить кнопкой «Сбросить ручные правки».`,
            'warn',
          );
        } else {
          setScannerTargetFeedback(
            data.blocked_reason === 'dedicated_glove_model_unavailable'
              ? 'Список можно настроить, но запросы перчаток заблокированы до отдельной модели.'
              : 'Отмечены точные цели следующего цикла.',
            data.scan_ready ? 'good' : 'warn',
          );
        }
      } catch (error) {
        if (generation !== scannerTargetGeneration) return false;
        const message = `Не удалось загрузить цели: ${error.message}`;
        setScannerTargetFeedback(message, 'bad');
        if (!scannerTargetPreview) {
          document.getElementById('scannerProfileGrid').innerHTML =
            `<p class="muted">${escapeHtml(message)}</p>`;
        }
      } finally {
        if (generation === scannerTargetGeneration) {
          scannerTargetBusy = false;
          renderScannerTargetPreview();
        }
      }
      return succeeded;
    }

    function updateScannerTargetSelection(key, checked, { render = true } = {}) {
      const draft = scannerTargetDraft();
      const item = (scannerTargetPreview?.items || []).find(row => row.target_key === key);
      if (!draft || !item || scannerTargetBusy) return;
      const before = scannerTargetSelected(item, draft);
      const include = new Set(draft.include || []);
      const exclude = new Set(draft.exclude || []);
      applyScannerTargetChoice(item, checked, draft.selection_mode, include, exclude);
      const after = scannerTargetSelectedWithOverrides(
        item, draft.selection_mode, include, exclude
      );
      if (before === after) return;
      const limits = scannerTargetsPayload?.limits || {};
      if (include.size > Number(limits.max_included_targets || 250)) {
        setScannerTargetFeedback(`Можно добавить вручную не больше ${limits.max_included_targets || 250} целей.`, 'bad');
        renderScannerTargetPreview();
        return;
      }
      if (exclude.size > Number(limits.max_excluded_targets || 1000)) {
        setScannerTargetFeedback(`Можно исключить не больше ${limits.max_excluded_targets || 1000} целей.`, 'bad');
        renderScannerTargetPreview();
        return;
      }
      draft.include = Array.from(include);
      draft.exclude = Array.from(exclude);
      draft.dirty = true;
      setScannerTargetFeedback('Есть несохранённые изменения.', 'warn');
      if (render) renderScannerTargetPreview();
    }

    function updateVisibleScannerTargets(selected) {
      const draft = scannerTargetDraft();
      if (!draft || scannerTargetBusy) return;
      const include = new Set(draft.include || []);
      const exclude = new Set(draft.exclude || []);
      let changed = false;
      for (const item of scannerTargetPreview?.items || []) {
        if (scannerTargetSelected(item, draft) === selected) continue;
        applyScannerTargetChoice(item, selected, draft.selection_mode, include, exclude);
        changed = true;
      }
      if (!changed) return;
      const limits = scannerTargetsPayload?.limits || {};
      if (include.size > Number(limits.max_included_targets || 250)) {
        setScannerTargetFeedback(`Можно добавить вручную не больше ${limits.max_included_targets || 250} целей.`, 'bad');
        return;
      }
      if (exclude.size > Number(limits.max_excluded_targets || 1000)) {
        setScannerTargetFeedback(`Можно исключить не больше ${limits.max_excluded_targets || 1000} целей.`, 'bad');
        return;
      }
      draft.include = Array.from(include);
      draft.exclude = Array.from(exclude);
      draft.dirty = true;
      setScannerTargetFeedback('Есть несохранённые изменения.', 'warn');
      renderScannerTargetPreview();
    }

    async function saveScannerTargetProfile() {
      const draft = scannerTargetDraft();
      if (!draft || !draft.dirty || scannerTargetBusy) return;
      const submittedProfile = scannerTargetProfile;
      const submitted = {
        enabled: Boolean(draft.enabled),
        selection_mode: draft.selection_mode,
        include: [...draft.include],
        exclude: [...draft.exclude],
      };
      scannerTargetBusy = true;
      renderScannerTargetPreview();
      setScannerTargetFeedback('Сохраняем профиль…');
      try {
        const response = await fetch('/api/scanner-targets', {
          method: 'POST',
          cache: 'no-store',
          headers: {
            Accept: 'application/json',
            'Content-Type': 'application/json',
            'X-CS2-Action': 'scanner-targets',
          },
          body: JSON.stringify({
            expected_revision: scannerTargetsPayload.control.revision,
            profiles: {
              [submittedProfile]: submitted,
            },
          }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        scannerTargetsPayload = data;
        scannerTargetDrafts.delete(submittedProfile);
        scannerTargetBusy = false;
        const refreshed = await refreshScannerTargets();
        if (refreshed) {
          setScannerTargetFeedback('Профиль сохранён. Сканер применит его на границе следующего цикла.', 'good');
        } else {
          setScannerTargetFeedback('Профиль сохранён, но список не удалось перечитать. Нажми «Обновить список».', 'warn');
        }
      } catch (error) {
        scannerTargetBusy = false;
        setScannerTargetFeedback(`Профиль не сохранён: ${error.message}`, 'bad');
        renderScannerTargetPreview();
      }
    }

    function rows(tbodyId, items, render) {
      const body = document.getElementById(tbodyId);
      body.innerHTML = items.map(render).join('');
    }

    function setOptions(selectId, items, valueKey, labelFn, selectedValue) {
      const select = document.getElementById(selectId);
      const previous = selectedValue || select.value;
      const values = new Set(items.map(item => String(item[valueKey] ?? '')));
      let nextValue = previous && values.has(previous) ? previous : String(items[0]?.[valueKey] ?? '');

      select.innerHTML = items.map(item => {
        const value = String(item[valueKey] ?? '');
        const selected = value === nextValue ? 'selected' : '';
        return `<option value="${escapeHtml(value)}" ${selected}>${escapeHtml(labelFn(item))}</option>`;
      }).join('');

      select.value = nextValue;
      return nextValue;
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }

    function safeExternalUrl(value, allowSteam = false) {
      if (!value) return '';
      try {
        const parsed = new URL(String(value), window.location.origin);
        const allowed = parsed.protocol === 'https:' || parsed.protocol === 'http:' ||
          (allowSteam && parsed.protocol === 'steam:');
        return allowed ? parsed.href : '';
      } catch (error) {
        return '';
      }
    }

    function thumb(url, title) {
      const normalizedUrl = safeExternalUrl(url);
      if (!normalizedUrl) return '<span class="no-thumb">no img</span>';
      const safeUrl = escapeHtml(normalizedUrl);
      const safeTitle = escapeHtml(title || '');
      return `<img class="thumb" src="${safeUrl}" alt="" title="${safeTitle}" loading="lazy" referrerpolicy="no-referrer">`;
    }

    function marketLink(url, label) {
      const safeLabel = escapeHtml(label || 'Unknown');
      const normalizedUrl = safeExternalUrl(url);
      if (!normalizedUrl) return safeLabel;
      return `<a class="market-link" href="${escapeHtml(normalizedUrl)}" target="_blank" rel="noopener noreferrer" title="${safeLabel}">${safeLabel}</a>`;
    }

    function skinButton(label) {
      const safeLabel = escapeHtml(label || 'Unknown');
      return `<button class="skin-link" type="button" data-skin-name="${safeLabel}" title="${safeLabel}">${safeLabel}</button>`;
    }

    function stickerButton(label, stickerId = '') {
      const safeLabel = escapeHtml(label || 'Unknown');
      const safeId = escapeHtml(stickerId ?? '');
      return `<button class="sticker-link" type="button" data-sticker-name="${safeLabel}" data-sticker-id="${safeId}" title="${safeLabel}">${safeLabel}</button>`;
    }

    function stickerNameWithSteam(url, label, stickerId = '') {
      const normalizedUrl = safeExternalUrl(url);
      const steam = normalizedUrl
        ? ` <a class="market-link" href="${escapeHtml(normalizedUrl)}" target="_blank" rel="noopener noreferrer" title="Open on Steam">Steam</a>`
        : '';
      return `${stickerButton(label, stickerId)}${steam}`;
    }

    function skinNameWithSteam(url, label) {
      const safeLabel = escapeHtml(label || 'Unknown');
      const normalizedUrl = safeExternalUrl(url);
      const steam = normalizedUrl
        ? ` <a class="market-link" href="${escapeHtml(normalizedUrl)}" target="_blank" rel="noopener noreferrer" title="Open on Steam">Steam</a>`
        : '';
      return `${skinButton(label)}${steam}`;
    }

    function rarityStyle(color) {
      if (!color) return '';
      const normalized = String(color).trim().replace(/^#/, '');
      if (!/^[0-9a-fA-F]{6}$/.test(normalized)) return '';
      return ` class="rarity-row" style="--rarity:#${normalized}"`;
    }

    function num(value, digits = 6) {
      if (value === null || value === undefined || value === '') return 'unknown';
      const parsed = Number(value);
      if (Number.isNaN(parsed)) return escapeHtml(value);
      return parsed.toFixed(digits);
    }

    function stickerList(stickers) {
      if (!stickers || !stickers.length) {
        return '<span class="muted">no stickers</span>';
      }

      return `<div class="sticker-list">${stickers.map(sticker => `
        <div class="sticker-pill">
          ${thumb(sticker.icon_url, sticker.name)}
          <div>
            ${stickerNameWithSteam(sticker.steam_market_url, sticker.name, sticker.sticker_id)}
            <div class="sticker-meta">
              slot=${escapeHtml(sticker.slot ?? 'unknown')} |
              price=${money(sticker.price_cents)} |
              wear=${sticker.wear === null || sticker.wear === undefined ? 'unknown' : num(sticker.wear, 4)}
            </div>
          </div>
        </div>
      `).join('')}</div>`;
    }

    function plainLink(url, label) {
      const normalizedUrl = safeExternalUrl(url, true);
      if (!normalizedUrl) return '<span class="muted">none</span>';
      return `<a class="market-link" href="${escapeHtml(normalizedUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
    }

    function setSvgAttr(el, attrs) {
      Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
      return el;
    }

    function svgEl(name, attrs = {}) {
      return setSvgAttr(document.createElementNS('http://www.w3.org/2000/svg', name), attrs);
    }

    function detailRow(label, value) {
      return `<div class="detail-row"><div class="label">${escapeHtml(label)}</div><div class="value">${value}</div></div>`;
    }

    function renderSelectedSale(sale) {
      if (!sale) {
        text('selectedSaleMeta', 'click a chart point');
        document.getElementById('selectedSaleBody').innerHTML = '<div class="muted">Open a skin and click a point on the chart.</div>';
        return;
      }

      text('selectedSaleMeta', `${money(sale.price_cents)} | ${msk(sale.sold_at)}`);
      const actions = [];
      const inspectUrl = safeExternalUrl(sale.inspect_link, true);
      const steamUrl = safeExternalUrl(sale.item_steam_market_url);
      if (inspectUrl) {
        actions.push(`<a class="button" href="${escapeHtml(inspectUrl)}" target="_blank" rel="noopener noreferrer">Inspect</a>`);
      }
      if (steamUrl) {
        actions.push(`<a class="button" href="${escapeHtml(steamUrl)}" target="_blank" rel="noopener noreferrer">Steam</a>`);
      }

      document.getElementById('selectedSaleBody').innerHTML = [
        detailRow('Skin', skinNameWithSteam(sale.item_steam_market_url, sale.name)),
        detailRow('Price', money(sale.price_cents)),
        detailRow('Sold', msk(sale.sold_at)),
        detailRow('Fetched', msk(sale.fetched_at)),
        detailRow('Float', num(sale.float_value, 8)),
        detailRow('Pattern Seed', escapeHtml(sale.paint_seed ?? 'unknown')),
        detailRow('Paint Index', escapeHtml(sale.paint_index ?? 'unknown')),
        detailRow('Sticker Count', fmt.format(sale.stickers_count || 0)),
        detailRow('Sticker Value', money(sale.stickers_total_cents || 0)),
        detailRow('Listing ID', `<span class="mono">${escapeHtml(sale.listing_id || '')}</span>`),
        actions.length ? `<div class="detail-actions">${actions.join('')}</div>` : '',
        `<div>${stickerList(sale.stickers || [])}</div>`,
      ].join('');
    }

    function renderSkinChart(sales) {
      const svg = document.getElementById('skinPriceChart');
      svg.innerHTML = '';
      const width = svg.clientWidth || 900;
      const height = svg.clientHeight || 380;
      const pad = { left: 58, right: 18, top: 18, bottom: 38 };
      setSvgAttr(svg, { viewBox: `0 0 ${width} ${height}` });

      if (!sales.length) {
        const label = svgEl('text', {
          x: width / 2,
          y: height / 2,
          fill: '#9ca3af',
          'text-anchor': 'middle',
          'font-size': '13',
        });
        label.textContent = 'No sales for this skin';
        svg.appendChild(label);
        return;
      }

      const prices = sales.map(row => Number(row.price_cents || 0) / 100).filter(value => value > 0);
      const times = sales.map((row, index) => {
        const parsed = Date.parse(row.sold_at || row.fetched_at || '');
        return Number.isNaN(parsed) ? index : parsed;
      });
      const minPrice = Math.min(...prices);
      const maxPrice = Math.max(...prices);
      const minTime = Math.min(...times);
      const maxTime = Math.max(...times);
      const yMin = minPrice === maxPrice ? minPrice * 0.95 : minPrice;
      const yMax = minPrice === maxPrice ? maxPrice * 1.05 + 0.01 : maxPrice;
      const plotW = Math.max(1, width - pad.left - pad.right);
      const plotH = Math.max(1, height - pad.top - pad.bottom);
      const xScale = (time, index) => {
        if (maxTime === minTime) return pad.left + plotW * (sales.length <= 1 ? 0.5 : index / (sales.length - 1));
        return pad.left + ((time - minTime) / (maxTime - minTime)) * plotW;
      };
      const yScale = (price) => pad.top + (1 - ((price - yMin) / (yMax - yMin || 1))) * plotH;

      for (let i = 0; i <= 4; i += 1) {
        const y = pad.top + (plotH / 4) * i;
        const price = yMax - ((yMax - yMin) / 4) * i;
        svg.appendChild(svgEl('line', { x1: pad.left, y1: y, x2: width - pad.right, y2: y, stroke: '#20232a', 'stroke-width': '1' }));
        const label = svgEl('text', { x: 8, y: y + 4, fill: '#9ca3af', 'font-size': '11' });
        label.textContent = `$${price.toFixed(2)}`;
        svg.appendChild(label);
      }

      svg.appendChild(svgEl('line', { x1: pad.left, y1: height - pad.bottom, x2: width - pad.right, y2: height - pad.bottom, stroke: '#30343d' }));
      svg.appendChild(svgEl('line', { x1: pad.left, y1: pad.top, x2: pad.left, y2: height - pad.bottom, stroke: '#30343d' }));

      const points = sales.map((sale, index) => {
        const price = Number(sale.price_cents || 0) / 100;
        return {
          x: xScale(times[index], index),
          y: yScale(price),
          sale,
        };
      });

      svg.appendChild(svgEl('polyline', {
        points: points.map(point => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' '),
        fill: 'none',
        stroke: '#38bdf8',
        'stroke-width': '2',
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round',
      }));

      points.forEach((point, index) => {
        const hasStickers = (point.sale.stickers_count || 0) > 0;
        const circle = svgEl('circle', {
          class: 'chart-point',
          cx: point.x,
          cy: point.y,
          r: hasStickers ? 4.5 : 3.2,
          fill: hasStickers ? '#22c55e' : '#38bdf8',
          stroke: '#0b0c0f',
          'stroke-width': '1.5',
        });
        circle.addEventListener('click', () => renderSelectedSale(point.sale));
        const title = svgEl('title');
        title.textContent = `${money(point.sale.price_cents)} | ${msk(point.sale.sold_at)} | stickers ${point.sale.stickers_count || 0}`;
        circle.appendChild(title);
        svg.appendChild(circle);

        if (index === points.length - 1) {
          renderSelectedSale(point.sale);
        }
      });
    }

    function renderStickerSaleDetail(sale) {
      if (!sale) {
        text('selectedStickerSaleMeta', 'click a chart point');
        document.getElementById('selectedStickerSaleBody').innerHTML = '<div class="muted">Open a sticker and click a point on the chart.</div>';
        return;
      }

      text('selectedStickerSaleMeta', `${money(sale.price_cents)} | ${msk(sale.sold_at)}`);
      const actions = [];
      const inspectUrl = safeExternalUrl(sale.inspect_link, true);
      const steamUrl = safeExternalUrl(sale.item_steam_market_url);
      if (inspectUrl) {
        actions.push(`<a class="button" href="${escapeHtml(inspectUrl)}" target="_blank" rel="noopener noreferrer">Inspect</a>`);
      }
      if (steamUrl) {
        actions.push(`<a class="button" href="${escapeHtml(steamUrl)}" target="_blank" rel="noopener noreferrer">Steam</a>`);
      }

      document.getElementById('selectedStickerSaleBody').innerHTML = [
        detailRow('Skin', skinNameWithSteam(sale.item_steam_market_url, sale.name)),
        detailRow('Price', money(sale.price_cents)),
        detailRow('Sold', msk(sale.sold_at)),
        detailRow('Fetched', msk(sale.fetched_at)),
        detailRow('Float', num(sale.float_value, 8)),
        detailRow('Pattern Seed', escapeHtml(sale.paint_seed ?? 'unknown')),
        detailRow('Paint Index', escapeHtml(sale.paint_index ?? 'unknown')),
        detailRow('This Sticker', `${fmt.format(sale.matching_stickers_count || 0)} rows | ${money(sale.matching_stickers_total_cents || 0)}`),
        detailRow('All Stickers', `${fmt.format(sale.stickers_count || 0)} rows | ${money(sale.stickers_total_cents || 0)}`),
        detailRow('Listing ID', `<span class="mono">${escapeHtml(sale.listing_id || '')}</span>`),
        actions.length ? `<div class="detail-actions">${actions.join('')}</div>` : '',
        `<div>${stickerList(sale.stickers || [])}</div>`,
      ].join('');
    }

    function renderStickerPriceChart(sales) {
      const svg = document.getElementById('stickerPriceChart');
      svg.innerHTML = '';
      const width = svg.clientWidth || 900;
      const height = svg.clientHeight || 380;
      const pad = { left: 58, right: 18, top: 18, bottom: 38 };
      setSvgAttr(svg, { viewBox: `0 0 ${width} ${height}` });

      if (!sales.length) {
        const label = svgEl('text', {
          x: width / 2,
          y: height / 2,
          fill: '#9ca3af',
          'text-anchor': 'middle',
          'font-size': '13',
        });
        label.textContent = 'No sales for this sticker';
        svg.appendChild(label);
        return;
      }

      const prices = sales.map(row => Number(row.price_cents || 0) / 100).filter(value => value > 0);
      const times = sales.map((row, index) => {
        const parsed = Date.parse(row.sold_at || row.fetched_at || '');
        return Number.isNaN(parsed) ? index : parsed;
      });
      const minPrice = Math.min(...prices);
      const maxPrice = Math.max(...prices);
      const minTime = Math.min(...times);
      const maxTime = Math.max(...times);
      const yMin = minPrice === maxPrice ? minPrice * 0.95 : minPrice;
      const yMax = minPrice === maxPrice ? maxPrice * 1.05 + 0.01 : maxPrice;
      const plotW = Math.max(1, width - pad.left - pad.right);
      const plotH = Math.max(1, height - pad.top - pad.bottom);
      const xScale = (time, index) => {
        if (maxTime === minTime) return pad.left + plotW * (sales.length <= 1 ? 0.5 : index / (sales.length - 1));
        return pad.left + ((time - minTime) / (maxTime - minTime)) * plotW;
      };
      const yScale = (price) => pad.top + (1 - ((price - yMin) / (yMax - yMin || 1))) * plotH;

      for (let i = 0; i <= 4; i += 1) {
        const y = pad.top + (plotH / 4) * i;
        const price = yMax - ((yMax - yMin) / 4) * i;
        svg.appendChild(svgEl('line', { x1: pad.left, y1: y, x2: width - pad.right, y2: y, stroke: '#20232a', 'stroke-width': '1' }));
        const label = svgEl('text', { x: 8, y: y + 4, fill: '#9ca3af', 'font-size': '11' });
        label.textContent = `$${price.toFixed(2)}`;
        svg.appendChild(label);
      }

      svg.appendChild(svgEl('line', { x1: pad.left, y1: height - pad.bottom, x2: width - pad.right, y2: height - pad.bottom, stroke: '#30343d' }));
      svg.appendChild(svgEl('line', { x1: pad.left, y1: pad.top, x2: pad.left, y2: height - pad.bottom, stroke: '#30343d' }));

      const points = sales.map((sale, index) => {
        const price = Number(sale.price_cents || 0) / 100;
        return {
          x: xScale(times[index], index),
          y: yScale(price),
          sale,
        };
      });

      svg.appendChild(svgEl('polyline', {
        points: points.map(point => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' '),
        fill: 'none',
        stroke: '#f59e0b',
        'stroke-width': '2',
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round',
      }));

      points.forEach((point, index) => {
        const circle = svgEl('circle', {
          class: 'chart-point',
          cx: point.x,
          cy: point.y,
          r: Math.min(6, 3.2 + Number(point.sale.matching_stickers_count || 1) * 0.7),
          fill: '#f59e0b',
          stroke: '#0b0c0f',
          'stroke-width': '1.5',
        });
        circle.addEventListener('click', () => renderStickerSaleDetail(point.sale));
        const title = svgEl('title');
        title.textContent = `${point.sale.name} | ${money(point.sale.price_cents)} | ${msk(point.sale.sold_at)} | this sticker ${point.sale.matching_stickers_count || 0}`;
        circle.appendChild(title);
        svg.appendChild(circle);

        if (index === points.length - 1) {
          renderStickerSaleDetail(point.sale);
        }
      });
    }

    function renderStickerSkinChart(skins) {
      const svg = document.getElementById('stickerSkinChart');
      svg.innerHTML = '';
      const width = svg.clientWidth || 900;
      const height = svg.clientHeight || 260;
      const pad = { left: 170, right: 22, top: 14, bottom: 20 };
      setSvgAttr(svg, { viewBox: `0 0 ${width} ${height}` });
      const rows = (skins || []).slice(0, 12);

      if (!rows.length) {
        const label = svgEl('text', {
          x: width / 2,
          y: height / 2,
          fill: '#9ca3af',
          'text-anchor': 'middle',
          'font-size': '13',
        });
        label.textContent = 'No skin stats for this sticker';
        svg.appendChild(label);
        return;
      }

      const maxValue = Math.max(...rows.map(row => Number(row.listing_count || 0)), 1);
      const rowH = Math.max(16, (height - pad.top - pad.bottom) / rows.length);
      rows.forEach((row, index) => {
        const y = pad.top + index * rowH;
        const barW = ((width - pad.left - pad.right) * Number(row.listing_count || 0)) / maxValue;
        const labelText = String(row.skin_name || 'Unknown').slice(0, 24);
        const label = svgEl('text', { x: 8, y: y + rowH * 0.65, fill: '#eef0f4', 'font-size': '11' });
        label.textContent = labelText;
        svg.appendChild(label);
        const rect = svgEl('rect', {
          class: 'chart-point',
          x: pad.left,
          y: y + 3,
          width: Math.max(2, barW),
          height: Math.max(6, rowH - 6),
          rx: 3,
          fill: '#38bdf8',
        });
        rect.addEventListener('click', () => openSkinDetail(row.skin_name));
        const title = svgEl('title');
        title.textContent = `${row.skin_name} | listings ${row.listing_count || 0} | avg ${money(row.avg_price_cents)}`;
        rect.appendChild(title);
        svg.appendChild(rect);
        const value = svgEl('text', { x: pad.left + Math.max(8, barW) + 7, y: y + rowH * 0.65, fill: '#9ca3af', 'font-size': '11' });
        value.textContent = fmt.format(row.listing_count || 0);
        svg.appendChild(value);
      });
    }

    function renderSkinDetail(data) {
      selectedSkinDetail = data;
      selectedSkinSales = data.sales || [];
      const stats = data.stats || {};
      const schedule = data.schedule || {};
      const name = data.name || selectedSkinName;

      text('skinDetailInfo', name ? `${name} | ${fmt.format(selectedSkinSales.length)} chart points` : 'skin not selected');
      text('skinDetailSales', fmt.format(stats.sales_count || 0));
      text('skinDetailAvgPrice', money(stats.avg_price_cents));
      text('skinDetailMedianPrice', money(stats.median_price_cents));
      text('skinDetailRange', `${money(stats.min_price_cents)} / ${money(stats.max_price_cents)}`);
      text('skinDetailAvgFloat', num(stats.avg_float_value, 6));
      text('skinDetailStickered', `${fmt.format(stats.stickered_sales_count || 0)} / ${fmt.format(stats.sales_count || 0)}`);
      text('skinDetailStickerValue', money(stats.avg_sticker_value_cents));
      text('skinDetailNextParse', schedule.next_fetch_at_iso ? msk(schedule.next_fetch_at_iso) : 'unknown');
      text('skinChartTitle', name || 'price history');
      text(
        'skinChartMeta',
        selectedSkinSales.length
          ? `oldest ${msk(selectedSkinSales[0].sold_at)} | latest ${msk(selectedSkinSales[selectedSkinSales.length - 1].sold_at)}`
          : 'no sales'
      );

      renderSkinChart(selectedSkinSales);
      if (!selectedSkinSales.length) {
        renderSelectedSale(null);
      }
    }

    async function openSkinDetail(name, scrollIntoView = true) {
      selectedSkinName = String(name || '').trim();
      if (!selectedSkinName) return;
      setDashboardView('market');
      document.getElementById('skinDetailSearch').value = selectedSkinName;
      text('skinDetailInfo', 'loading');
      const response = await fetch(`/api/skin-detail?token=${encodeURIComponent(dashboardToken)}&name=${encodeURIComponent(selectedSkinName)}`, { cache: 'no-store' });
      const data = await response.json();
      if (data.error) {
        text('skinDetailInfo', data.error);
        return;
      }
      renderSkinDetail(data);
      if (scrollIntoView) {
        document.getElementById('skinDetailPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }

    function renderStickerDetail(data) {
      selectedStickerDetail = data;
      selectedStickerSales = data.sales || [];
      const stats = data.stats || {};
      const topSkins = data.top_skins || [];
      const name = data.name || selectedStickerName;

      text('stickerDetailInfo', name ? `${name} | ${fmt.format(selectedStickerSales.length)} chart points` : 'sticker not selected');
      text('stickerDetailRows', fmt.format(stats.sticker_rows || 0));
      text('stickerDetailListings', fmt.format(stats.listing_count || 0));
      text('stickerDetailSkins', fmt.format(stats.skin_count || 0));
      text('stickerDetailAvgSkinPrice', money(stats.avg_skin_price_cents));
      text('stickerDetailMedianSkinPrice', money(stats.median_skin_price_cents));
      text('stickerDetailAvgStickerPrice', money(stats.avg_sticker_price_cents));
      text('stickerDetailMaxStickerValue', money(stats.max_matching_sticker_value_cents));
      text('stickerDetailLatestSale', msk(stats.latest_sold_at));
      text('stickerChartTitle', name || 'sales with sticker');
      text(
        'stickerChartMeta',
        selectedStickerSales.length
          ? `oldest ${msk(selectedStickerSales[0].sold_at)} | latest ${msk(selectedStickerSales[selectedStickerSales.length - 1].sold_at)}`
          : 'no sales'
      );
      text('stickerTopSkinsMeta', `${fmt.format(topSkins.length)} skins sorted by listings`);

      renderStickerPriceChart(selectedStickerSales);
      renderStickerSkinChart(topSkins);
      if (!selectedStickerSales.length) {
        renderStickerSaleDetail(null);
      }

      rows('stickerTopSkinsBody', topSkins, (row, i) => `<tr${rarityStyle(row.item_rarity_color || row.item_name_color)}>
        <td>${i + 1}</td>
        <td>${thumb(row.item_icon_url, row.skin_name)}</td>
        <td>${skinNameWithSteam(row.item_steam_market_url, row.skin_name)}</td>
        <td>${fmt.format(row.listing_count || 0)}</td>
        <td>${fmt.format(row.sticker_rows || 0)}</td>
        <td>${money(row.avg_price_cents)}</td>
        <td>${msk(row.latest_sold_at)}</td>
      </tr>`);
    }

    async function openStickerDetail(name, stickerId = '', scrollIntoView = true) {
      selectedStickerName = String(name || '').trim();
      selectedStickerId = String(stickerId || '').trim();
      if (!selectedStickerName && !selectedStickerId) return;
      setDashboardView('market');
      document.getElementById('stickerDetailSearch').value = selectedStickerName;
      text('stickerDetailInfo', 'loading');
      const params = new URLSearchParams();
      params.set('token', dashboardToken);
      if (selectedStickerName) params.set('name', selectedStickerName);
      if (selectedStickerId) params.set('sticker_id', selectedStickerId);
      const response = await fetch(`/api/sticker-detail?${params.toString()}`, { cache: 'no-store' });
      const data = await response.json();
      if (data.error) {
        text('stickerDetailInfo', data.error);
        return;
      }
      renderStickerDetail(data);
      if (scrollIntoView) {
        document.getElementById('stickerDetailPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }

    function keyStatus(key) {
      if (Number(key.blocked_seconds_left || 0) > 0) {
        return ['bad', key.blocked_reason === 'authentication' ? 'key rejected' : 'key limited'];
      }
      if (Number(key.transport_blocked_seconds_left || 0) > 0) return ['warn', 'proxy cooling'];
      if (key.current) return ['good', 'current'];
      return ['warn', 'ready'];
    }

    function keyCooldown(key) {
      const keySeconds = Number(key.blocked_seconds_left || 0);
      if (keySeconds > 0) return `${seconds(keySeconds)} · key`;
      const transportSeconds = Number(key.transport_blocked_seconds_left || 0);
      if (transportSeconds > 0) return `${seconds(transportSeconds)} · proxy`;
      return 'no';
    }

    function ensureModelHealthCard(modelName) {
      const mount = document.getElementById('modelHealthCards');
      const name = String(modelName || 'model').toLowerCase();
      let card = Array.from(mount.querySelectorAll('[data-model-name]'))
        .find(element => element.dataset.modelName === name);
      if (card) return card;
      const safeId = name.replace(/[^a-z0-9_-]/g, '-');
      card = document.createElement('article');
      card.className = 'model-health-card';
      card.dataset.modelName = name;
      card.setAttribute('aria-labelledby', `model-${safeId}-title`);
      card.innerHTML = `
        <div class="model-health-title">
          <span class="model-title-copy"><span id="model-${safeId}-title" data-role="model-name">model</span><small data-role="item-kind">—</small></span>
          <span class="status freshness-warn" data-role="freshness"><span class="dot warn"></span><span data-role="freshness-label">loading</span></span>
        </div>
        <details class="model-baseline">
          <summary data-role="baseline-summary">Рабочая модель и обучающие данные</summary>
          <div class="model-baseline-body">
            <div class="model-health-row"><span>artifact</span><strong data-role="trained-at">unknown</strong></div>
            <div class="model-health-row"><span>training data</span><strong data-role="training-rows">unknown</strong></div>
            <div class="model-health-row"><span>new DB data</span><strong data-role="new-rows">unknown</strong></div>
            <div class="model-health-row"><span>test median / p90 error</span><strong data-role="test-errors">not saved</strong></div>
            <div class="model-health-row"><span>test MAE</span><strong data-role="test-mae">not saved</strong></div>
          </div>
        </details>
        <div class="model-training" data-role="training-panel">
          <div class="model-training-summary">
            <strong data-role="training-state">Проверяем возможность обучения</strong>
            <span data-role="training-percent">0%</span>
          </div>
          <div class="model-training-progress" data-role="training-progress" role="progressbar" aria-label="Прогресс обучения ${safeId}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span></span></div>
          <div class="model-training-stages" data-role="training-stages" aria-label="Этапы обучения"></div>
          <div class="model-training-meta"><span data-role="training-phase">ожидание</span><span data-role="training-eta">ETA —</span></div>
          <p class="model-training-reason" data-role="training-reason">Загружаем критерии достаточности данных.</p>
          <details class="model-quality-comparison" data-role="quality-comparison" hidden>
            <summary><span data-role="quality-title">Новая модель против рабочей</span><small data-role="quality-summary">—</small></summary>
            <div class="model-quality-meta" data-role="quality-meta"></div>
            <div class="model-quality-table-wrap" data-role="quality-table"></div>
          </details>
          <div class="model-training-actions">
            <button class="button button-primary" type="button" data-training-action="start">Переобучить</button>
            <button class="button" type="button" data-training-action="cancel" hidden>Отменить</button>
          </div>
          <details class="model-training-log" data-role="training-log" hidden>
            <summary>Последние строки обучения</summary>
            <pre aria-label="Журнал обучения ${safeId}" data-role="training-log-tail"></pre>
          </details>
          <p class="model-training-feedback" data-role="training-feedback" role="status" aria-live="polite"></p>
        </div>`;
      card.querySelectorAll('[data-training-action]').forEach(button => {
        button.dataset.model = name;
      });
      const loading = mount.querySelector(':scope > .muted');
      if (loading) loading.remove();
      mount.appendChild(card);
      return card;
    }

    function setCardText(card, role, value) {
      const element = card.querySelector(`[data-role="${role}"]`);
      if (element && element.textContent !== String(value)) element.textContent = String(value);
    }

    function updateModelFreshnessCard(model) {
      const card = ensureModelHealthCard(model.name);
      const states = {
        fresh: ['good', 'fresh'],
        update_soon: ['warn', 'more data available'],
        retrain_recommended: ['bad', 'retrain recommended'],
        estimated: ['warn', 'freshness estimated'],
        data_mismatch: ['bad', 'data mismatch'],
        missing: ['bad', 'artifact missing'],
        unknown: ['warn', 'freshness unknown'],
      };
      const [stateClass, stateLabel] = states[model.freshness] || states.unknown;
      const quality = model.quality || {};
      const rowsLabel = model.training_rows === null || model.training_rows === undefined
        ? 'unknown'
        : `${model.training_rows_estimated ? '≈' : ''}${fmt.format(model.training_rows)} rows`;
      const gapLabel = model.new_rows_since_training === null || model.new_rows_since_training === undefined
        ? 'unknown'
        : `+${fmt.format(model.new_rows_since_training)} (${Number(model.data_growth_percent || 0).toFixed(1)}%)`;
      const medianLabel = quality.median_ape_percent === null || quality.median_ape_percent === undefined
        ? 'not saved'
        : `${Number(quality.median_ape_percent).toFixed(2)}%`;
      const p90Label = quality.p90_ape_percent === null || quality.p90_ape_percent === undefined
        ? 'not saved'
        : `${Number(quality.p90_ape_percent).toFixed(2)}%`;
      const maeLabel = quality.mae_usd === null || quality.mae_usd === undefined
        ? 'not saved'
        : money(Number(quality.mae_usd) * 100);
      const status = card.querySelector('[data-role="freshness"]');
      const dot = status.querySelector('.dot');
      status.className = `status freshness-${stateClass}`;
      dot.className = `dot ${stateClass}`;
      setCardText(card, 'model-name', model.name || 'model');
      setCardText(card, 'item-kind', modelItemKindLabel(model.name, model.item_kind));
      setCardText(card, 'baseline-summary', trainingUiText('baseline'));
      setCardText(card, 'freshness-label', stateLabel);
      setCardText(card, 'trained-at', msk(model.trained_at));
      setCardText(card, 'training-rows', rowsLabel);
      setCardText(card, 'new-rows', gapLabel);
      setCardText(card, 'test-errors', `${medianLabel} / ${p90Label}`);
      setCardText(card, 'test-mae', maeLabel);
    }

    function trainingPhaseLabel(phase) {
      const labels = {
        idle: 'ожидание', preparing: 'подготовка', creating_snapshot: 'снимок базы',
        snapshot_ready: 'снимок готов', building_cache: 'сбор кэша', loading_cache: 'загрузка кэша',
        verifying_cache: 'проверка кэша', building_features: 'признаки', preparing_features: 'подготовка признаков',
        building_vocabularies: 'словари', encoding_arrays: 'кодирование', training_point: 'основная модель',
        training_low_interval: 'нижняя граница q15', training_high_interval: 'верхняя граница q85',
        training_epochs: 'эпохи Nova', quality_check: 'проверка качества', saving_candidate: 'сохранение кандидата',
        quality_gate: 'сравнение со старой моделью', quality_rejected: 'не прошла порог качества',
        validation_failed: 'ошибка проверки целостности', integrity_rejected: 'не прошла SHA-256/проверку целостности',
        hot_activation: 'безопасная горячая установка', active: 'новая модель активна',
        hot_activation_failed: 'горячая установка откатилась', awaiting_restart: 'ожидает резервной активации',
        activation_schedule_failed: 'ошибка резервной активации',
        cancelling: 'отмена', cancelled: 'отменено', failed: 'ошибка', interrupted: 'прервано',
        startup_failed: 'ошибка запуска', succeeded: 'кандидат готов',
      };
      return labels[phase] || String(phase || 'ожидание').replaceAll('_', ' ');
    }

    function trainingStateLabel(state, activationState = 'none', rejectionKind = null) {
      if (activationState === 'loading') return 'Качество проверено — модель горячо устанавливается';
      if (state === 'succeeded' && activationState === 'pending_restart') return 'Кандидат ждёт резервной активации';
      if (state === 'succeeded' && activationState === 'activated') return 'Новая модель установлена';
      if (activationState === 'rolled_back') return 'Установка откачена — старая модель активна';
      if (activationState === 'failed' || activationState === 'rollback_failed') return 'Ошибка горячей установки';
      if (state === 'rejected' && rejectionKind === 'integrity') return 'Кандидат отклонён: не подтверждена целостность/SHA-256';
      if (state === 'rejected' && rejectionKind === 'quality') return 'Кандидат не прошёл порог качества';
      const labels = {
        idle: 'Готово к запуску', starting: 'Запускаем обучение', running: 'Модель обучается',
        cancelling: 'Останавливаем обучение', succeeded: 'Новый кандидат готов', failed: 'Обучение завершилось ошибкой',
        rejected: 'Новая модель не прошла проверку качества', cancelled: 'Обучение отменено',
        interrupted: 'Обучение было прервано',
      };
      return labels[state] || 'Состояние неизвестно';
    }

    function trainingEligibilityMessage(gate) {
      if (!gate || typeof gate !== 'object') return 'Критерии достаточности данных пока недоступны.';
      const thresholds = gate.thresholds || {};
      if (gate.eligible) {
        return `Данных достаточно: +${fmt.format(gate.new_rows || 0)} строк, рост ${Number(gate.growth_percent || 0).toFixed(1)}%, time-safe новых ${fmt.format(gate.runtime_valid_new_rows || 0)}.`;
      }
      const reasons = Array.isArray(gate.reasons) ? gate.reasons : [];
      const messages = [];
      if (reasons.includes('not_enough_new_rows')) {
        messages.push(`нужно ещё ${fmt.format(Math.max(Number(thresholds.min_new_rows || 0) - Number(gate.new_rows || 0), 0))} новых продаж`);
      }
      if (reasons.includes('not_enough_growth')) {
        messages.push(`рост ${Number(gate.growth_percent || 0).toFixed(1)}% из ${Number(thresholds.min_growth_percent || 0).toFixed(1)}%`);
      }
      if (reasons.includes('not_enough_runtime_valid_rows')) {
        messages.push(`time-safe строк ${fmt.format(gate.runtime_valid_new_rows || 0)} из ${fmt.format(thresholds.min_runtime_valid_new_rows || 0)}`);
      }
      const blocking = {
        production_metrics_missing: 'нет production-метрик', production_row_count_unknown: 'неизвестен размер обучающей выборки',
        production_boundary_unknown: 'неизвестна временная граница production', nova_v2_contract_pending: 'контракт Nova v2 ещё не готов',
        database_eligibility_check_failed: 'не удалось безопасно проверить базу', database_row_count_mismatch: 'размер базы меньше production-снимка',
        eligibility_refreshing: 'считаем новые time-safe строки',
      };
      reasons.forEach(reason => { if (blocking[reason]) messages.push(blocking[reason]); });
      return messages.length ? `Пока рано: ${messages.join('; ')}.` : 'Порог новых качественных данных ещё не достигнут.';
    }

    function trainingCardReason(job, gate) {
      const base = trainingEligibilityMessage(gate);
      const warnings = [
        ...(Array.isArray(job?.warnings) ? job.warnings : []),
        ...(Array.isArray(gate?.warnings) ? gate.warnings : []),
      ];
      const needsStrictRetrain = Boolean(job?.retrain_required)
        || warnings.some(value => String(value).startsWith('production_item_kind'));
      return needsStrictRetrain
        ? `${trainingUiText('legacyKindWarning')} ${base}`
        : base;
    }

    function modelItemKindLabel(modelName, itemKind = '') {
      const kind = String(itemKind || '').toLowerCase();
      if (kind === 'knife' || modelName === 'knives') return trainingUiText('knife');
      if (kind === 'glove' || modelName === 'gloves') return trainingUiText('glove');
      return trainingUiText('weaponSkin');
    }

    function trainingStageIndex(phase) {
      const value = String(phase || 'idle');
      if (['preparing', 'creating_snapshot', 'snapshot_ready', 'building_cache', 'loading_cache', 'verifying_cache'].includes(value)) return 0;
      if (['building_features', 'preparing_features', 'building_vocabularies', 'encoding_arrays'].includes(value)) return 1;
      if (['training_point', 'training_low_interval', 'training_high_interval', 'training_epochs', 'saving_candidate'].includes(value)) return 2;
      if (['quality_check', 'quality_gate', 'quality_rejected', 'integrity_rejected', 'validation_failed'].includes(value)) return 3;
      if (['hot_activation', 'active', 'hot_activation_failed', 'awaiting_restart', 'activation_schedule_failed'].includes(value)) return 4;
      return -1;
    }

    function renderTrainingStages(card, job) {
      const mount = card.querySelector('[data-role="training-stages"]');
      if (!mount) return;
      const state = job?.state || 'idle';
      const phase = job?.phase || 'idle';
      const current = trainingStageIndex(phase);
      const terminalSuccess = state === 'succeeded' && job?.activation_state === 'activated';
      const failed = ['failed', 'rejected', 'cancelled', 'interrupted'].includes(state);
      const labels = ['data', 'features', 'train', 'quality', 'activation'];
      mount.innerHTML = labels.map((key, index) => {
        let stageState = 'pending';
        if (terminalSuccess || index < current) stageState = 'done';
        else if (index === current && failed) stageState = 'failed';
        else if (index === current && MODEL_TRAINING_ACTIVE_STATES.has(state)) stageState = 'active';
        else if (index === current && state === 'succeeded') stageState = 'done';
        return `<span class="model-training-stage" data-state="${stageState}">${escapeHtml(trainingUiText(key))}</span>`;
      }).join('');
    }

    function qualityMetricLabel(key) {
      const labels = {
        median_ape: 'medianApe', mean_ape: 'meanApe', p90_ape: 'p90Ape',
        mae_usd: 'maeUsd', rmse_log: 'rmseLog', r2: 'r2',
        interval_coverage: 'intervalCoverage', quantile_crossing_rate: 'quantileCrossing',
        candidate_row_win_percent: 'rowWin',
      };
      return trainingUiText(labels[key] || key);
    }

    function qualityMetricHelp(key) {
      const labels = {
        median_ape: 'medianApeHelp', mean_ape: 'meanApeHelp', p90_ape: 'p90ApeHelp',
        mae_usd: 'maeUsdHelp', rmse_log: 'rmseLogHelp', r2: 'r2Help',
        interval_coverage: 'intervalCoverageHelp', quantile_crossing_rate: 'quantileCrossingHelp',
        candidate_row_win_percent: 'rowWinHelp',
      };
      return trainingUiText(labels[key] || key);
    }

    function qualityNumber(value, unit) {
      if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
      const number = Number(value);
      if (unit === 'usd') return money(number * 100);
      if (unit === 'percent') return `${number.toFixed(Math.abs(number) < 1 ? 3 : 2)}%`;
      if (unit === 'fraction') return `${(number * 100).toFixed(2)}%`;
      return number.toFixed(Math.abs(number) < 1 ? 4 : 3);
    }

    function normalizedQualityMetrics(comparison) {
      const raw = comparison?.metrics;
      const finiteMetric = value => value !== null && value !== undefined
        && value !== '' && Number.isFinite(Number(value));
      const hasEvidence = value => value && typeof value === 'object'
        && (finiteMetric(value.production) || finiteMetric(value.candidate));
      if (Array.isArray(raw)) return raw.filter(hasEvidence);
      if (raw && typeof raw === 'object') {
        return Object.entries(raw)
          .map(([key, value]) => ({ key, ...(value || {}) }))
          .filter(hasEvidence);
      }
      return [];
    }

    function qualityOutcome(metric) {
      if (['better', 'worse', 'equal', 'notComparable'].includes(metric?.outcome)) return metric.outcome;
      if (metric?.production === null || metric?.production === undefined
          || metric?.candidate === null || metric?.candidate === undefined) return 'notComparable';
      const oldValue = Number(metric?.production);
      const newValue = Number(metric?.candidate);
      if (!Number.isFinite(oldValue) || !Number.isFinite(newValue)) return 'notComparable';
      const tolerance = Math.max(Math.abs(oldValue), Math.abs(newValue), 1) * 1e-9;
      if (Math.abs(newValue - oldValue) <= tolerance) return 'equal';
      const lowerIsBetter = metric?.lower_is_better !== false;
      return (newValue < oldValue) === lowerIsBetter ? 'better' : 'worse';
    }

    function renderQualityComparison(card, job) {
      const panel = card.querySelector('[data-role="quality-comparison"]');
      if (!panel) return;
      const comparison = job?.quality_comparison || job?.metric_comparison || null;
      const metrics = normalizedQualityMetrics(comparison);
      const integrityRejected = job?.rejection_kind === 'integrity';
      const qualityRejected = job?.rejection_kind === 'quality';
      const hasEvidence = Boolean(comparison || job?.quality_gate || integrityRejected || qualityRejected);
      panel.hidden = !hasEvidence;
      if (!hasEvidence) return;

      setCardText(card, 'quality-title', trainingUiText('comparison'));
      const outcomes = metrics.map(qualityOutcome);
      const better = outcomes.filter(value => value === 'better').length;
      const worse = outcomes.filter(value => value === 'worse').length;
      let summary = integrityRejected
        ? trainingUiText('integrity')
        : `${better} ${trainingUiText('better')} · ${worse} ${trainingUiText('worse')}`;
      if (qualityRejected && !metrics.length) summary = trainingUiText('rejected');
      setCardText(card, 'quality-summary', summary);

      const meta = card.querySelector('[data-role="quality-meta"]');
      const holdoutRows = Number(comparison?.holdout_rows || job?.quality_gate?.holdout_rows || 0);
      const proof = comparison?.primary_improvement_proven;
      const bootstrap = comparison?.outcome === 'bootstrap';
      const gateLabel = integrityRejected
        ? trainingUiText('integrity')
        : bootstrap
          ? trainingUiText('bootstrap')
          : (qualityRejected || proof === false) ? trainingUiText('rejected') : trainingUiText('passed');
      meta.innerHTML = [
        holdoutRows ? `${trainingUiText('holdout')}: ${fmt.format(holdoutRows)} ${trainingUiText('rows')}` : '',
        gateLabel,
        comparison?.candidate_trained_at ? msk(comparison.candidate_trained_at) : '',
      ].filter(Boolean).map(value => `<span>${escapeHtml(value)}</span>`).join('');

      const table = card.querySelector('[data-role="quality-table"]');
      if (!metrics.length) {
        table.innerHTML = `<p class="model-training-reason" style="padding:11px">${escapeHtml(integrityRejected ? trainingUiText('integrity') : trainingUiText('noComparison'))}</p>`;
      } else {
        const rows = metrics.map(metric => {
          const outcome = qualityOutcome(metric);
          const delta = metric.delta ?? (Number(metric.candidate) - Number(metric.production));
          const deltaPrefix = Number(delta) > 0 ? '+' : '';
          return `<tr><td><span>${escapeHtml(qualityMetricLabel(metric.key))}</span><small class="metric-help">${escapeHtml(qualityMetricHelp(metric.key))}</small></td>`
            + `<td>${escapeHtml(qualityNumber(metric.production, metric.unit))}</td>`
            + `<td>${escapeHtml(qualityNumber(metric.candidate, metric.unit))}</td>`
            + `<td class="quality-${outcome}">${escapeHtml(`${deltaPrefix}${qualityNumber(delta, metric.unit)}`)}</td>`
            + `<td class="quality-${outcome}">${escapeHtml(trainingUiText(outcome))}</td></tr>`;
        }).join('');
        table.innerHTML = `<table class="model-quality-table"><thead><tr><th>${escapeHtml(trainingUiText('metric'))}</th><th>${escapeHtml(trainingUiText('old'))}</th><th>${escapeHtml(trainingUiText('candidate'))}</th><th>${escapeHtml(trainingUiText('change'))}</th><th>${escapeHtml(trainingUiText('result'))}</th></tr></thead><tbody>${rows}</tbody></table>`;
      }
      if (!panel.dataset.initialized) {
        panel.open = Boolean(job?.rejection_kind);
        panel.dataset.initialized = 'true';
      }
    }

    function updateModelTrainingCard(modelName, job) {
      const card = ensureModelHealthCard(modelName);
      const gate = job?.eligibility || null;
      const state = job?.state || 'idle';
      const active = MODEL_TRAINING_ACTIVE_STATES.has(state);
      const percent = Math.min(100, Math.max(0, Number(job?.progress_percent || 0)));
      const globalActive = Boolean(modelTrainingPayload?.training_active);
      const start = card.querySelector('[data-training-action="start"]');
      const cancel = card.querySelector('[data-training-action="cancel"]');
      const progress = card.querySelector('[data-role="training-progress"]');
      const progressFill = progress.querySelector('span');
      const log = card.querySelector('[data-role="training-log"]');
      const logLines = Array.isArray(job?.log_tail) ? job.log_tail : [];
      const isBusy = modelTrainingBusy.has(modelName);
      const candidateReady = Boolean(job?.candidate_ready_for_review);

      setCardText(card, 'item-kind', modelItemKindLabel(modelName, gate?.item_kind || job?.item_kind));
      setCardText(card, 'baseline-summary', trainingUiText('baseline'));
      setCardText(card, 'training-state', trainingStateLabel(state, job?.activation_state, job?.rejection_kind));
      setCardText(card, 'training-percent', `${percent.toFixed(percent % 1 ? 1 : 0)}%`);
      setCardText(card, 'training-phase', trainingPhaseLabel(job?.phase));
      setCardText(card, 'training-eta', active && job?.eta_seconds !== null && job?.eta_seconds !== undefined ? `осталось ≈ ${seconds(job.eta_seconds)}` : 'ETA —');
      setCardText(card, 'training-reason', trainingCardReason(job, gate));
      progress.setAttribute('aria-valuenow', String(percent));
      progressFill.style.width = `${percent}%`;
      renderTrainingStages(card, job);
      renderQualityComparison(card, job);

      start.hidden = active;
      cancel.hidden = !active;
      cancel.disabled = isBusy || state === 'cancelling';
      cancel.textContent = state === 'cancelling' ? 'Отменяем…' : 'Отменить';
      const blockedByOther = globalActive && !active;
      start.disabled = isBusy || !gate?.eligible || blockedByOther || candidateReady;
      start.classList.toggle('button-primary', !start.disabled);
      start.textContent = isBusy
        ? 'Отправляем…'
        : candidateReady
          ? 'Кандидат готов'
          : blockedByOther
            ? 'Другая модель обучается'
            : gate?.eligible ? 'Переобучить' : 'Недостаточно данных';
      start.title = start.disabled ? trainingEligibilityMessage(gate) : `Переобучить модель ${modelName}`;

      log.hidden = logLines.length === 0;
      setCardText(card, 'training-log-tail', logLines.join('\n'));
      if (!isBusy) {
        if (job?.error) {
          setCardText(card, 'training-feedback', job.error);
          card.querySelector('[data-role="training-feedback"]').dataset.tone = 'bad';
        } else if (candidateReady && job?.pending_activation) {
          const holdoutRows = Number(job?.quality_gate?.holdout_rows || 0);
          const holdout = holdoutRows ? ` на ${fmt.format(holdoutRows)} новых продажах` : '';
          setCardText(card, 'training-feedback', `Качество лучше рабочей модели${holdout}. Горячая активация не завершилась, поэтому рабочая версия не изменена; кандидат ожидает резервной активации.`);
          card.querySelector('[data-role="training-feedback"]').dataset.tone = 'good';
        } else if (state === 'succeeded' && job?.activation_state === 'activated') {
          setCardText(card, 'training-feedback', 'Проверенная модель уже установлена. При необходимости предыдущая версия сохранена в backup.');
          card.querySelector('[data-role="training-feedback"]').dataset.tone = 'good';
        } else if (!active) {
          setCardText(card, 'training-feedback', 'Обучается только одна модель за раз; production во время обучения не меняется.');
          card.querySelector('[data-role="training-feedback"]').dataset.tone = '';
        }
      }
    }

    function renderModelTraining(payload) {
      modelTrainingPayload = payload && typeof payload === 'object' ? payload : null;
      const jobs = modelTrainingPayload?.models || {};
      TRAINABLE_MODEL_NAMES.forEach(name => updateModelTrainingCard(name, jobs[name] || { model: name, state: 'idle' }));
    }

    function renderModelFreshness(data) {
      const freshness = data.model_freshness || {};
      const models = Array.isArray(freshness.models) ? freshness.models : [];
      text('modelFreshnessInfo', `${fmt.format(freshness.current_rows || 0)} current sales rows`);
      models.forEach(model => {
        modelFreshnessByName.set(model.name, model);
        updateModelFreshnessCard(model);
      });
      if (modelTrainingPayload) renderModelTraining(modelTrainingPayload);
    }

    function scheduleModelTrainingPoll(delay = null) {
      if (modelTrainingTimer) clearTimeout(modelTrainingTimer);
      const active = Boolean(modelTrainingPayload?.training_active || modelTrainingPayload?.eligibility_refreshing);
      const nextDelay = delay === null ? (document.hidden ? 10000 : (active ? 1000 : 10000)) : delay;
      modelTrainingTimer = setTimeout(refreshModelTraining, nextDelay);
    }

    async function refreshModelTraining() {
      if (modelTrainingRequestRunning) {
        scheduleModelTrainingPoll();
        return;
      }
      modelTrainingRequestRunning = true;
      try {
        const response = await fetch('/api/model-training', {
          cache: 'no-store',
          headers: { Accept: 'application/json' },
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        renderModelTraining(data);
      } catch (error) {
        TRAINABLE_MODEL_NAMES.forEach(name => {
          const card = ensureModelHealthCard(name);
          setCardText(card, 'training-feedback', `Не удалось прочитать обучение: ${error.message}`);
          card.querySelector('[data-role="training-feedback"]').dataset.tone = 'bad';
        });
      } finally {
        modelTrainingRequestRunning = false;
        scheduleModelTrainingPoll();
      }
    }

    async function changeModelTraining(action, model, jobId = null) {
      if (modelTrainingBusy.size) return;
      modelTrainingBusy.add(model);
      const current = modelTrainingPayload?.models?.[model] || { model, state: 'idle' };
      updateModelTrainingCard(model, current);
      const card = ensureModelHealthCard(model);
      setCardText(card, 'training-feedback', action === 'start' ? 'Запускаем безопасное обучение в staging…' : 'Отправляем запрос на отмену…');
      card.querySelector('[data-role="training-feedback"]').dataset.tone = 'warn';
      try {
        const body = { action, model };
        if (action === 'cancel') body.job_id = jobId;
        const response = await fetch('/api/model-training', {
          method: 'POST',
          cache: 'no-store',
          headers: {
            Accept: 'application/json',
            'Content-Type': 'application/json',
            'X-CS2-Action': 'model-training',
          },
          body: JSON.stringify(body),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        modelTrainingBusy.delete(model);
        renderModelTraining(data);
      } catch (error) {
        modelTrainingBusy.delete(model);
        updateModelTrainingCard(model, current);
        setCardText(card, 'training-feedback', `Действие не выполнено: ${error.message}`);
        card.querySelector('[data-role="training-feedback"]').dataset.tone = 'bad';
      }
      scheduleModelTrainingPoll(1000);
    }

    function render(data) {
      const status = data.status || {};
      const progress = data.progress || {};
      const overview = data.overview || {};
      const total = status.total_skins ?? progress.total_skins ?? 0;
      const index = status.skin_index ?? progress.skin_index ?? 0;
      const percent = total ? Math.min(100, Math.max(0, index * 100 / total)) : 0;

      const staticState = data.static_refreshing ? ' | stats refreshing' : '';
      text('lastUpdate', `updated ${msk(data.generated_at)}${staticState}`);
      text('dbName', data.db_path || 'csfloat_history.db');
      text('progressValue', `${fmt.format(index)} / ${fmt.format(total)}`);
      text('progressHint', status.current_skin || progress.market_hash_name || 'no active skin');
      document.getElementById('progressBar').style.width = `${percent}%`;

      text('salesCount', fmt.format(overview.sales_count || 0));
      text('latestFetched', `latest ${msk(overview.latest_fetched_at)}`);
      text('skinCount', fmt.format(overview.skin_variant_count || 0));
      text('stickerCount', fmt.format(overview.sticker_rows || 0));
      text('stickeredListings', `${fmt.format(overview.listings_with_stickers || 0)} listings with stickers`);
      renderModelFreshness(data);
      renderSystemHealth(data);
      text('cycleRequests', fmt.format(status.request_count_cycle || 0));
      const lastNew = status.last_new_sales_count ?? 0;
      const lastReturned = status.last_sales_count ?? 0;
      const nextFetch = status.last_next_fetch_at ? ` | next ${msk(status.last_next_fetch_at)}` : '';
      text(
        'cycleSales',
        `${fmt.format(status.sales_count_cycle || 0)} total | last ${fmt.format(lastReturned)} / new ${fmt.format(lastNew)}${nextFetch}`
      );

      if (status.sleeping_until) {
        text('sleepState', 'yes');
        text('sleepHint', `${status.sleep_reason || 'sleep'} until ${msk(status.sleeping_until)}`);
      } else if (secondsSince(status.updated_at) > 120) {
        text('sleepState', 'stale');
        text('sleepHint', `no parser updates for ${seconds(secondsSince(status.updated_at))}`);
      } else {
        text('sleepState', 'no');
        text('sleepHint', status.sleep_reason || 'parser active');
      }

      rows('keysBody', status.keys || [], (key) => {
        const [dot, label] = keyStatus(key);
        return `<tr>
          <td><span class="status"><span class="dot ${dot}"></span>${label}</span></td>
          <td class="mono">${escapeHtml(key.path || '')}</td>
          <td class="mono">${escapeHtml(key.proxy || 'direct')}</td>
          <td>${escapeHtml(key.rate_limit ?? 'unknown')}</td>
          <td>${escapeHtml(key.rate_remaining ?? 'unknown')}</td>
          <td>${msk(key.rate_reset)}</td>
          <td>${keyCooldown(key)}</td>
          <td>${key.last_http_status ?? 'none'}</td>
        </tr>`;
      });

      const schedule = data.schedule_overview || {};
      text('scheduleDueNow', fmt.format(schedule.due_now || 0));
      text('scheduleNeverFetched', fmt.format(schedule.never_fetched || 0));
      text('scheduleMedianInterval', seconds(schedule.median_interval_seconds));
      text(
        'scheduleIntervalRange',
        `${seconds(schedule.min_interval_seconds)} / ${seconds(schedule.max_interval_seconds)}`
      );
      text('scheduleNextRequest', schedule.next_fetch_at_iso ? msk(schedule.next_fetch_at_iso) : 'unknown');

      const requestLog = data.request_log || {};
      text('requestLogNew100', fmt.format(requestLog.new_sales_sum || 0));
      text('requestLogAvgNew100', num(requestLog.avg_new_sales || 0, 2));

      rows('requestLogBody', requestLog.rows || [], (row, i) => `<tr>
        <td>${i + 1}</td>
        <td>${skinButton(row.market_hash_name)}</td>
        <td>${msk(row.requested_at_iso)}</td>
        <td>${fmt.format(row.sales_count || 0)}</td>
        <td>${fmt.format(row.new_sales_count || 0)}</td>
        <td>${signedSeconds(row.next_fetch_seconds_left)}</td>
        <td>${seconds(row.interval_seconds)}</td>
        <td>${escapeHtml(row.interval_reason || 'unknown')}</td>
        <td>
          ${escapeHtml(row.coverage_status || row.outcome || 'legacy')}
          <div class="sticker-meta">overlap ${fmt.format(row.overlap_count || 0)} · pages ${fmt.format(row.page_count || 0)}</div>
        </td>
      </tr>`);

      rows('stickersBody', data.top_stickers || [], (row, i) => `<tr${rarityStyle(row.rarity_color || row.name_color)}>
        <td>${i + 1}</td>
        <td>${thumb(row.icon_url, row.sticker_name)}</td>
        <td>${stickerNameWithSteam(row.steam_market_url, row.sticker_name, row.sticker_id)}</td>
        <td>${fmt.format(row.sticker_count || 0)}</td>
        <td>${fmt.format(row.listing_count || 0)}</td>
        <td>${fmt.format(row.skin_count || 0)}</td>
      </tr>`);

      rows('weaponsBody', data.top_weapons || [], (row, i) => `<tr>
        <td>${i + 1}</td>
        <td>${escapeHtml(row.weapon_name || 'Unknown')}</td>
        <td>${fmt.format(row.sticker_count || 0)}</td>
        <td>${fmt.format(row.listing_count || 0)}</td>
        <td>${fmt.format(row.skin_count || 0)}</td>
      </tr>`);

      const weapons = data.weapons || [];
      selectedWeapon = setOptions(
        'weaponSelect',
        weapons,
        'weapon_name',
        row => `${row.weapon_name || 'Unknown'} | skins ${fmt.format(row.skin_count || 0)}`,
        selectedWeapon
      );

      const tournaments = data.tournaments || [];
      selectedTournament = setOptions(
        'tournamentSelect',
        tournaments,
        'tournament',
        row => `${row.tournament || 'Unknown'} | stickers ${fmt.format(row.sticker_count || 0)} | ids ${fmt.format(row.sticker_id_count || 0)}`,
        selectedTournament
      );

      rows('recentBody', data.recent_sales || [], (row) => `<tr${rarityStyle(row.item_rarity_color || row.item_name_color)}>
        <td>${thumb(row.item_icon_url, row.name)}</td>
        <td>${skinNameWithSteam(row.item_steam_market_url, row.name)}</td>
        <td>${money(row.price_cents)}</td>
        <td>${row.float_value === null || row.float_value === undefined ? 'unknown' : Number(row.float_value).toFixed(6)}</td>
        <td>${msk(row.sold_at)}</td>
        <td>${msk(row.fetched_at)}</td>
      </tr>`);
    }

    async function refreshWeaponSkins() {
      if (!selectedWeapon) return;
      const response = await fetch(`/api/weapon-skins?token=${encodeURIComponent(dashboardToken)}&weapon=${encodeURIComponent(selectedWeapon)}`, { cache: 'no-store' });
      const data = await response.json();
      text('weaponInfo', `${fmt.format(data.count || 0)} catalog variants · observed market stats`);

      rows('weaponSkinsBody', data.skins || [], (row, i) => `<tr${rarityStyle(row.item_rarity_color || row.item_name_color)}>
        <td>${i + 1}</td>
        <td>${thumb(row.item_icon_url, row.skin_name)}</td>
        <td>${skinNameWithSteam(row.item_steam_market_url, row.skin_name)}</td>
        <td>${fmt.format(row.sales_count || 0)}</td>
        <td>${row.sticker_count === null || row.sticker_count === undefined ? '—' : fmt.format(row.sticker_count)}</td>
        <td>${row.listings_with_stickers === null || row.listings_with_stickers === undefined ? '—' : fmt.format(row.listings_with_stickers)}</td>
        <td>${money(row.median_price_cents ?? row.avg_price_cents)}</td>
        <td>${msk(row.latest_fetched_at)}</td>
      </tr>`);
    }

    async function refreshTournamentStickers() {
      if (!selectedTournament) return;
      const response = await fetch(`/api/tournament-stickers?token=${encodeURIComponent(dashboardToken)}&tournament=${encodeURIComponent(selectedTournament)}`, { cache: 'no-store' });
      const data = await response.json();
      text('tournamentInfo', `${fmt.format(data.count || 0)} stickers sorted by rows`);

      rows('tournamentStickersBody', data.stickers || [], (row, i) => `<tr${rarityStyle(row.rarity_color || row.name_color)}>
        <td>${i + 1}</td>
        <td>${thumb(row.icon_url, row.sticker_name)}</td>
        <td>${stickerNameWithSteam(row.steam_market_url, row.sticker_name, row.sticker_id)}</td>
        <td>${escapeHtml(row.sticker_type || 'Regular')}</td>
        <td>${fmt.format(row.sticker_count || 0)}</td>
        <td>${fmt.format(row.listing_count || 0)}</td>
        <td>${fmt.format(row.skin_count || 0)}</td>
        <td>${money(row.avg_sticker_price_cents)}</td>
      </tr>`);
    }

    async function refreshDetails() {
      if (detailRefreshRunning) return;
      if (loadedWeapon === selectedWeapon && loadedTournament === selectedTournament) return;
      detailRefreshRunning = true;
      try {
        await Promise.all([refreshWeaponSkins(), refreshTournamentStickers()]);
        loadedWeapon = selectedWeapon;
        loadedTournament = selectedTournament;
      } finally {
        detailRefreshRunning = false;
      }
    }

    function renderScheduleRows(data) {
      const total = data.total || 0;
      const shownFrom = total ? scheduleOffset + 1 : 0;
      const shownTo = Math.min(scheduleOffset + (data.rows || []).length, total);
      text('scheduleBrowserInfo', `${fmt.format(shownFrom)}-${fmt.format(shownTo)} / ${fmt.format(total)}`);

      document.getElementById('schedulePrevButton').disabled = scheduleOffset <= 0;
      document.getElementById('scheduleNextButton').disabled = scheduleOffset + scheduleLimit >= total;

      rows('scheduleBody', data.rows || [], (row, i) => {
        const isDue = row.state === 'due';
        const dot = isDue ? 'good' : 'warn';
        const stateText = isDue ? 'due now' : 'scheduled';
        return `<tr>
          <td>${scheduleOffset + i + 1}</td>
          <td>
            ${skinButton(row.market_hash_name)}
            <div class="sticker-meta">last fetch ${msk(row.last_fetch_at_iso)}</div>
          </td>
          <td><span class="status"><span class="dot ${dot}"></span>${stateText}</span></td>
          <td>${signedSeconds(row.next_fetch_seconds_left)}</td>
          <td>${msk(row.next_fetch_at_iso)}</td>
          <td>${seconds(row.interval_seconds)}</td>
          <td>${row.last_sales_count === null || row.last_sales_count === undefined ? 'none' : fmt.format(row.last_sales_count)}</td>
          <td>${row.last_new_sales_count === null || row.last_new_sales_count === undefined ? 'none' : fmt.format(row.last_new_sales_count)}</td>
          <td>${num(row.avg_new_sales_count, 2)}</td>
          <td>
            ${escapeHtml(row.interval_reason || 'unknown')}
            <div class="sticker-meta">${escapeHtml(row.coverage_status || 'legacy')} · streak ${fmt.format(row.saturation_streak || 0)} · ${num(row.estimated_sales_per_day, 1)}/day</div>
          </td>
        </tr>`;
      });
    }

    async function refreshScheduleBrowser() {
      const search = document.getElementById('scheduleSearch').value.trim();
      const url = `/api/schedule?token=${encodeURIComponent(dashboardToken)}&offset=${scheduleOffset}&limit=${scheduleLimit}&search=${encodeURIComponent(search)}`;
      const response = await fetch(url, { cache: 'no-store' });
      const data = await response.json();
      renderScheduleRows(data);
    }

    async function refreshDbBrowser() {
      const search = document.getElementById('dbSearch').value.trim();
      const scope = document.getElementById('dbSearchScope').value;
      const generation = ++dbBrowserGeneration;
      if (dbBrowserController) dbBrowserController.abort();
      dbBrowserController = new AbortController();
      dbBrowserLoading = true;
      document.getElementById('dbSearchButton').disabled = true;
      text('dbBrowserInfo', 'поиск…');
      let data;
      try {
        const url = `/api/listings?token=${encodeURIComponent(dashboardToken)}&offset=${dbOffset}&limit=${dbLimit}&scope=${encodeURIComponent(scope)}&search=${encodeURIComponent(search)}`;
        const response = await fetch(url, {
          cache: 'no-store',
          signal: dbBrowserController.signal,
        });
        data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        if (generation !== dbBrowserGeneration) return;
      } catch (error) {
        if (error.name === 'AbortError' || generation !== dbBrowserGeneration) return;
        // A completed error is terminal for this exact query.  Retrying it from
        // the 10-second dashboard refresh loop would only repeat validation
        // errors and can pile up work while the database is unavailable.  A
        // user search/pagination action explicitly starts the next attempt.
        dbBrowserLoaded = true;
        text('dbBrowserInfo', `ошибка: ${error.message}`);
        rows('dbBrowserBody', [], () => '');
        document.getElementById('dbPrevButton').disabled = true;
        document.getElementById('dbNextButton').disabled = true;
        return;
      } finally {
        if (generation === dbBrowserGeneration) {
          dbBrowserLoading = false;
          document.getElementById('dbSearchButton').disabled = false;
        }
      }

      dbBrowserLoaded = true;

      const total = data.total || 0;
      const shownFrom = total ? dbOffset + 1 : 0;
      const shownTo = Math.min(dbOffset + (data.rows || []).length, total);
      text('dbBrowserInfo', `${fmt.format(shownFrom)}-${fmt.format(shownTo)} / ${fmt.format(total)}`);

      document.getElementById('dbPrevButton').disabled = dbOffset <= 0;
      const maxOffset = Number(data.max_offset ?? 100000);
      document.getElementById('dbNextButton').disabled =
        dbOffset + dbLimit >= total || dbOffset + dbLimit > maxOffset;

      rows('dbBrowserBody', data.rows || [], (row) => `<tr${rarityStyle(row.item_rarity_color || row.item_name_color)}>
        <td>${thumb(row.item_icon_url, row.name)}</td>
        <td>
          ${skinNameWithSteam(row.item_steam_market_url, row.name)}
          <div class="sticker-meta mono">${escapeHtml(row.listing_id || '')}</div>
        </td>
        <td>${money(row.price_cents)}</td>
        <td>${num(row.float_value, 6)}</td>
        <td>${escapeHtml(row.paint_seed ?? 'unknown')}</td>
        <td>${escapeHtml(row.paint_index ?? 'unknown')}</td>
        <td>${stickerList(row.stickers)}</td>
        <td>${msk(row.sold_at)}</td>
        <td>${msk(row.fetched_at)}</td>
      </tr>`);
    }

    async function refreshDealCandidates() {
      const search = document.getElementById('dealCandidateSearch').value.trim();
      const url = `/api/deal-candidates?token=${encodeURIComponent(dashboardToken)}&offset=${dealCandidateOffset}&limit=${dealCandidateLimit}&search=${encodeURIComponent(search)}`;
      const response = await fetch(url, { cache: 'no-store' });
      const data = await response.json();

      const total = data.total || 0;
      const shownFrom = total ? dealCandidateOffset + 1 : 0;
      const shownTo = Math.min(dealCandidateOffset + (data.rows || []).length, total);
      text('dealCandidateInfo', `${fmt.format(shownFrom)}-${fmt.format(shownTo)} / ${fmt.format(total)}`);

      document.getElementById('dealCandidatePrevButton').disabled = dealCandidateOffset <= 0;
      document.getElementById('dealCandidateNextButton').disabled = dealCandidateOffset + dealCandidateLimit >= total;

      rows('dealCandidatesBody', data.rows || [], (row, i) => {
        const verdictClass = row.verdict === 'good' ? 'good' : (row.verdict === 'bad' ? 'bad' : 'warn');
        return `<tr>
          <td>${dealCandidateOffset + i + 1}</td>
          <td>
            ${skinButton(row.skin_name)}
            <div class="sticker-meta">float ${num(row.float_value, 6)} | seed ${escapeHtml(row.paint_seed ?? 'unknown')} | paint ${escapeHtml(row.paint_index ?? 'unknown')}</div>
          </td>
          <td>${money(row.buy_price_cents)}</td>
          <td>${money(row.predicted_price_cents)}</td>
          <td>${money(row.net_expected_cents)}</td>
          <td>${money(row.profit_cents)}</td>
          <td>${row.roi_percent === null || row.roi_percent === undefined ? 'unknown' : `${Number(row.roi_percent).toFixed(2)}%`}</td>
          <td>${num(row.liquidity_score, 0)}</td>
          <td><span class="status"><span class="dot ${verdictClass}"></span>${escapeHtml(row.verdict || 'unknown')}</span><div class="sticker-meta">${escapeHtml(row.pricing_method || '')}</div></td>
          <td>${stickerButton(row.source_sticker_name, row.source_sticker_id)}</td>
          <td>${msk(row.last_seen_at)}</td>
          <td>${plainLink(row.csfloat_url, 'CSFloat')} ${plainLink(row.inspect_link, 'Inspect')}</td>
        </tr>`;
      });
    }

    async function refresh() {
      try {
        const response = await fetch(`/api/dashboard?token=${encodeURIComponent(dashboardToken)}`, { cache: 'no-store' });
        const data = await response.json();
        render(data);
        if (activeDashboardView === 'system') refreshScheduleBrowser();
        if (activeDashboardView === 'system' && !dbBrowserLoaded && !dbBrowserLoading) {
          refreshDbBrowser();
        }
      } catch (error) {
        text('lastUpdate', `error: ${error.message}`);
      }
    }

    document.getElementById('weaponSelect').addEventListener('change', (event) => {
      selectedWeapon = event.target.value;
      loadedWeapon = '';
      refreshWeaponSkins();
    });

    document.getElementById('tournamentSelect').addEventListener('change', (event) => {
      selectedTournament = event.target.value;
      loadedTournament = '';
      refreshTournamentStickers();
    });

    document.getElementById('scheduleSearchButton').addEventListener('click', () => {
      scheduleOffset = 0;
      refreshScheduleBrowser();
    });

    document.getElementById('scheduleSearch').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        scheduleOffset = 0;
        refreshScheduleBrowser();
      }
    });

    document.getElementById('schedulePrevButton').addEventListener('click', () => {
      scheduleOffset = Math.max(0, scheduleOffset - scheduleLimit);
      refreshScheduleBrowser();
    });

    document.getElementById('scheduleNextButton').addEventListener('click', () => {
      scheduleOffset += scheduleLimit;
      refreshScheduleBrowser();
    });

    document.getElementById('dbSearchButton').addEventListener('click', () => {
      dbOffset = 0;
      dbBrowserLoaded = false;
      refreshDbBrowser();
    });

    document.getElementById('dbSearch').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        dbOffset = 0;
        dbBrowserLoaded = false;
        refreshDbBrowser();
      }
    });

    document.getElementById('dbSearchScope').addEventListener('change', (event) => {
      const placeholders = {
        skin: 'Например: AK, MAC-10 или Asiimov',
        sticker: 'Например: Crown или Katowice 2014',
        listing: 'Точный ID лота',
        all: 'Скин, наклейка или ID лота',
      };
      document.getElementById('dbSearch').placeholder = placeholders[event.target.value] || placeholders.skin;
      dbOffset = 0;
      dbBrowserLoaded = false;
      refreshDbBrowser();
    });

    document.getElementById('dbPrevButton').addEventListener('click', () => {
      dbOffset = Math.max(0, dbOffset - dbLimit);
      dbBrowserLoaded = false;
      refreshDbBrowser();
    });

    document.getElementById('dbNextButton').addEventListener('click', () => {
      dbOffset += dbLimit;
      dbBrowserLoaded = false;
      refreshDbBrowser();
    });

    document.getElementById('dealCandidateSearchButton').addEventListener('click', () => {
      dealCandidateOffset = 0;
      refreshDealCandidates();
    });

    document.getElementById('dealCandidateSearch').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        dealCandidateOffset = 0;
        refreshDealCandidates();
      }
    });

    document.getElementById('dealCandidatePrevButton').addEventListener('click', () => {
      dealCandidateOffset = Math.max(0, dealCandidateOffset - dealCandidateLimit);
      refreshDealCandidates();
    });

    document.getElementById('dealCandidateNextButton').addEventListener('click', () => {
      dealCandidateOffset += dealCandidateLimit;
      refreshDealCandidates();
    });

    document.getElementById('modelHealthCards').addEventListener('click', event => {
      const button = event.target.closest('[data-training-action]');
      if (!button || button.disabled) return;
      const action = button.dataset.trainingAction;
      const model = button.dataset.model;
      const job = modelTrainingPayload?.models?.[model] || null;
      if (action === 'start') {
        if (!window.confirm(`Запустить полное переобучение ${model}? Это может занять десятки минут. Если кандидат лучше, система безопасно переключит модель без перезапуска; при ошибке останется старая версия.`)) return;
        changeModelTraining('start', model);
      } else if (action === 'cancel' && job?.job_id) {
        if (!window.confirm(`Остановить текущее обучение ${model}?`)) return;
        changeModelTraining('cancel', model, job.job_id);
      }
    });

    document.getElementById('scannerIntervalMinutes').addEventListener('input', event => {
      event.target.dataset.dirty = 'true';
    });

    document.getElementById('scannerApplyInterval').addEventListener('click', () => {
      const input = document.getElementById('scannerIntervalMinutes');
      const minutes = Number(input.value);
      if (!Number.isFinite(minutes) || minutes < 1 || minutes > 1440) {
        setScannerFeedback('Укажи интервал от 1 до 1440 минут.', 'bad');
        input.focus();
        return;
      }
      const intervalSeconds = Math.round(minutes * 60);
      changeScannerControl(
        { interval_seconds: intervalSeconds },
        `Новый интервал сохранён: ${seconds(intervalSeconds)}.`,
      );
    });

    document.getElementById('scannerToggleButton').addEventListener('click', () => {
      const enabled = scannerControlPayload?.control?.enabled;
      if (typeof enabled !== 'boolean') {
        setScannerFeedback('Состояние ещё не загружено. Попробуй через секунду.', 'bad');
        return;
      }
      changeScannerControl(
        { enabled: !enabled },
        enabled
          ? 'Поиск выгодных скинов приостановлен. Парсер истории продолжает работать.'
          : 'Поиск выгодных скинов включён.',
      );
    });

    document.getElementById('scannerProfileGrid').addEventListener('click', event => {
      const card = event.target.closest('[data-scanner-profile]');
      if (!card || scannerTargetBusy) return;
      scannerTargetProfile = card.dataset.scannerProfile;
      scannerTargetOffset = 0;
      document.getElementById('scannerTargetSearch').value = '';
      renderScannerProfileCards();
      refreshScannerTargets();
    });

    document.getElementById('scannerTargetsRefresh').addEventListener('click', () => {
      const hasDirtyDrafts = Array.from(scannerTargetDrafts.values()).some(draft => draft.dirty);
      const confirmMessage = window.cs2T
        ? window.cs2T('Сбросить несохранённые изменения и перечитать списки?')
        : 'Сбросить несохранённые изменения и перечитать списки?';
      if (hasDirtyDrafts && !window.confirm(confirmMessage)) return;
      scannerTargetOffset = 0;
      refreshScannerTargets({ refreshControl: true });
    });

    document.getElementById('scannerTargetSearchButton').addEventListener('click', () => {
      scannerTargetOffset = 0;
      refreshScannerTargets();
    });

    document.getElementById('scannerTargetSearch').addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      scannerTargetOffset = 0;
      refreshScannerTargets();
    });

    document.getElementById('scannerTargetMode').addEventListener('change', event => {
      if (scannerTargetBusy) return;
      const draft = scannerTargetDraft();
      if (!draft) return;
      draft.selection_mode = event.target.value === 'custom' ? 'custom' : 'recommended';
      draft.dirty = true;
      setScannerTargetFeedback(
        draft.selection_mode === 'custom'
          ? 'Свой список: будут выбраны только отмеченные вручную цели.'
          : 'Рекомендованный список можно дополнить или сократить галочками.',
        'warn',
      );
      renderScannerTargetPreview();
    });

    document.getElementById('scannerTargetEnabled').addEventListener('change', event => {
      if (scannerTargetBusy) return;
      const draft = scannerTargetDraft();
      if (!draft) return;
      draft.enabled = Boolean(event.target.checked);
      draft.dirty = true;
      setScannerTargetFeedback('Есть несохранённые изменения.', 'warn');
      renderScannerTargetPreview();
    });

    document.getElementById('scannerTargetBody').addEventListener('change', event => {
      if (scannerTargetBusy) return;
      const checkbox = event.target.closest('[data-target-key]');
      if (!checkbox) return;
      updateScannerTargetSelection(checkbox.dataset.targetKey, checkbox.checked);
    });

    document.getElementById('scannerTargetSelectVisible').addEventListener('click', () => {
      updateVisibleScannerTargets(true);
    });

    document.getElementById('scannerTargetClearVisible').addEventListener('click', () => {
      updateVisibleScannerTargets(false);
    });

    document.getElementById('scannerTargetResetOverrides').addEventListener('click', () => {
      if (scannerTargetBusy) return;
      const draft = scannerTargetDraft();
      if (!draft || (!(draft.include || []).length && !(draft.exclude || []).length)) return;
      draft.include = [];
      draft.exclude = [];
      draft.dirty = true;
      setScannerTargetFeedback('Ручные правки сброшены. Сохрани профиль, чтобы применить изменение.', 'warn');
      renderScannerTargetPreview();
    });

    document.getElementById('scannerTargetPrev').addEventListener('click', () => {
      if (scannerTargetBusy) return;
      scannerTargetOffset = Math.max(0, scannerTargetOffset - scannerTargetLimit);
      refreshScannerTargets();
    });

    document.getElementById('scannerTargetNext').addEventListener('click', () => {
      if (scannerTargetBusy) return;
      const next = scannerTargetPreview?.next_offset;
      if (next === null || next === undefined) return;
      scannerTargetOffset = Number(next);
      refreshScannerTargets();
    });

    document.getElementById('scannerTargetSave').addEventListener('click', () => {
      saveScannerTargetProfile();
    });

    window.addEventListener('beforeunload', event => {
      if (!Array.from(scannerTargetDrafts.values()).some(draft => draft.dirty)) return;
      event.preventDefault();
      event.returnValue = '';
    });

    document.getElementById('skinDetailSearchButton').addEventListener('click', () => {
      openSkinDetail(document.getElementById('skinDetailSearch').value);
    });

    document.getElementById('skinDetailSearch').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        openSkinDetail(event.target.value);
      }
    });

    document.getElementById('stickerDetailSearchButton').addEventListener('click', () => {
      openStickerDetail(document.getElementById('stickerDetailSearch').value);
    });

    document.getElementById('stickerDetailSearch').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        openStickerDetail(event.target.value);
      }
    });

    document.addEventListener('click', (event) => {
      const skinButtonEl = event.target.closest('[data-skin-name]');
      if (skinButtonEl) {
        openSkinDetail(skinButtonEl.dataset.skinName);
        return;
      }

      const stickerButtonEl = event.target.closest('[data-sticker-name], [data-sticker-id]');
      if (stickerButtonEl) {
        openStickerDetail(stickerButtonEl.dataset.stickerName, stickerButtonEl.dataset.stickerId);
      }
    });

    window.addEventListener('resize', () => {
      if (selectedSkinSales.length) {
        renderSkinChart(selectedSkinSales);
      }
      if (selectedStickerSales.length) {
        renderStickerPriceChart(selectedStickerSales);
      }
      if (selectedStickerName || selectedStickerId) {
        renderStickerSkinChart(selectedStickerSales.length && selectedStickerDetail ? selectedStickerDetail.top_skins || [] : []);
      }
    });

    document.querySelectorAll('[data-dashboard-tab]').forEach(tab => {
      tab.addEventListener('click', () => setDashboardView(tab.dataset.dashboardTab));
      tab.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        const tabs = Array.from(document.querySelectorAll('[data-dashboard-tab]'));
        const direction = event.key === 'ArrowRight' ? 1 : -1;
        const next = tabs[(tabs.indexOf(tab) + direction + tabs.length) % tabs.length];
        next.focus();
        setDashboardView(next.dataset.dashboardTab);
      });
    });

    let initialDashboardView = 'overview';
    try { initialDashboardView = localStorage.getItem('dashboardView') || 'overview'; } catch (error) {}
    if (['parser', 'database'].includes(initialDashboardView)) initialDashboardView = 'system';
    if (initialDashboardView === 'market') initialDashboardView = 'overview';
    const requestedDashboardView = new URLSearchParams(window.location.search).get('view');
    if (dashboardViews.has(requestedDashboardView)) initialDashboardView = requestedDashboardView;
    else if (['parser', 'database'].includes(requestedDashboardView)) initialDashboardView = 'system';
    setDashboardView(initialDashboardView, false);

    refreshScannerControl({ quiet: false });
    refreshModelTraining();
    refresh();
    setInterval(refresh, __LIVE_REFRESH_MS__);
    setInterval(() => {
      if (!document.hidden) refreshScannerControl();
    }, 5000);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        refreshScannerControl({ quiet: false });
        scheduleModelTrainingPoll(0);
      } else {
        scheduleModelTrainingPoll(10000);
      }
    });
  </script>
</body>
</html>
"""


DEAL_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CSFloat Model Comparison</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101114;
      --panel: #181a1f;
      --panel-2: #20232a;
      --control-bg: #111318;
      --table-head: #15171c;
      --subpanel-bg: #13151a;
      --text: #eef0f4;
      --muted: #9ca3af;
      --line: #30343d;
      --good: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
      --accent: #38bdf8;
    }
    body.theme-light {
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --panel-2: #eef2f7;
      --control-bg: #ffffff;
      --table-head: #e8edf5;
      --subpanel-bg: #f7f9fc;
      --text: #111827;
      --muted: #667085;
      --line: #d6dce7;
      --good: #15803d;
      --warn: #b45309;
      --bad: #b91c1c;
      --accent: #0369a1;
    }
    body.theme-midnight {
      color-scheme: dark;
      --bg: #070b12;
      --panel: #101722;
      --panel-2: #162131;
      --control-bg: #0b111b;
      --table-head: #111a27;
      --subpanel-bg: #0e1622;
      --text: #edf6ff;
      --muted: #93a4b8;
      --line: #253447;
      --good: #2dd4bf;
      --warn: #fbbf24;
      --bad: #fb7185;
      --accent: #60a5fa;
    }
    body.theme-graphite {
      color-scheme: dark;
      --bg: #151515;
      --panel: #202020;
      --panel-2: #2a2a2a;
      --control-bg: #181818;
      --table-head: #242424;
      --subpanel-bg: #1b1b1b;
      --text: #f2f2f2;
      --muted: #a8a8a8;
      --line: #3a3a3a;
      --good: #84cc16;
      --warn: #f97316;
      --bad: #ef4444;
      --accent: #eab308;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 2;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--bg) 94%, transparent);
      backdrop-filter: blur(10px);
    }
    .wrap {
      width: min(1280px, calc(100vw - 32px));
      margin: 0 auto;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      min-height: 64px;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 760; }
    .sub { color: var(--muted); font-size: 13px; margin-top: 2px; }
    main { padding: 18px 0 28px; }
    .grid { display: grid; gap: 12px; }
    .two { grid-template-columns: minmax(0, 1.1fr) minmax(360px, 0.9fr); }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-2);
    }
    h2 { margin: 0; font-size: 14px; font-weight: 720; }
    .body { padding: 14px; }
    .form-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; }
    input, textarea, select {
      width: 100%;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--control-bg);
      color: var(--text);
      padding: 8px 9px;
      font: inherit;
      outline: none;
    }
    .control {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 34px;
    }
    .control-label {
      color: var(--muted);
      font-size: 12px;
      line-height: 1;
      white-space: nowrap;
    }
    textarea { min-height: 114px; resize: vertical; }
    .wide { grid-column: 1 / -1; }
    .button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--control-bg);
      color: var(--text);
      padding: 0 11px;
      text-decoration: none;
      font: inherit;
      white-space: nowrap;
      cursor: pointer;
    }
    .button:hover { border-color: var(--accent); }
    .actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td {
      padding: 8px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    th { color: var(--muted); font-size: 12px; font-weight: 650; background: var(--table-head); }
    .metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .metric {
      min-height: 90px;
      padding: 14px;
      background: var(--subpanel-bg);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .metric .label { color: var(--muted); font-size: 12px; }
    .metric .value { margin-top: 7px; font-size: 24px; font-weight: 760; white-space: nowrap; }
    .metric .hint { margin-top: 4px; color: var(--muted); font-size: 12px; }
    .verdict {
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      border-radius: 999px;
      padding: 0 12px;
      border: 1px solid var(--line);
      background: var(--control-bg);
      font-weight: 720;
    }
    .verdict.good { color: var(--good); border-color: color-mix(in srgb, var(--good) 55%, var(--line)); }
    .verdict.watch, .verdict.thin { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 55%, var(--line)); }
    .verdict.bad { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 55%, var(--line)); }
    .muted { color: var(--muted); }
    .mono { font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; }
    .small { font-size: 12px; }
    @media (max-width: 980px) {
      .two, .form-grid, .metrics { grid-template-columns: 1fr; }
      .topbar { align-items: flex-start; flex-direction: column; }
      .actions, .control { width: 100%; }
      .actions .button { flex: 1 1 130px; }
      .control select { flex: 1 1 auto; }
    }
  </style>
</head>
<body class="page-deal">
  <header>
    <div class="wrap topbar">
      <a class="site-brand" href="/" aria-label="CS2 Intelligence home">
        <span class="brand-mark" aria-hidden="true">C2</span>
        <span><strong>CS2 Intelligence</strong><small id="modelStatus">three-model comparison</small></span>
      </a>
      <nav class="site-nav" aria-label="Main navigation">
        <a class="nav-link" data-nav-path="/" href="/?token=__DASHBOARD_TOKEN__">Overview</a>
        <a class="nav-link" data-nav-path="/deals" href="/deals?token=__DASHBOARD_TOKEN__">Deals</a>
        <a class="nav-link" data-nav-path="/strategy" href="/strategy?token=__DASHBOARD_TOKEN__">Strategy</a>
        <a class="nav-link" data-nav-path="/deal" href="/deal?token=__DASHBOARD_TOKEN__">Сравнение</a>
        <a class="nav-link" data-nav-path="/account" href="/account">Инвентарь</a>
      </nav>
      <div class="actions header-tools" style="margin-top:0;">
        <label class="control">
          <span class="control-label">Theme</span>
          <select id="themeSelect" title="Theme">
            <option value="dark">Dark</option>
            <option value="light">Light</option>
            <option value="midnight">Midnight</option>
            <option value="graphite">Graphite</option>
          </select>
        </label>
      </div>
    </div>
  </header>
  <main class="wrap">
    <section class="tool-hero">
      <div>
        <div class="eyebrow">MODEL COMPARISON</div>
        <h1>Compare three estimates before you decide.</h1>
        <p>Almaz, Igor and Nova stay visible side by side. The headline result uses the lowest available estimate instead of silently choosing the most optimistic ROI.</p>
      </div>
      <ol class="workflow-steps" aria-label="Prediction workflow">
        <li><span>1</span>Paste listing</li>
        <li><span>2</span>Review data</li>
        <li><span>3</span>Compare</li>
      </ol>
    </section>

    <div class="grid two workbench-grid">
    <section class="panel">
      <div class="section-head">
        <div><span class="eyebrow">INPUT</span><h2>Listing details</h2></div>
        <span class="pill">required fields first</span>
      </div>
      <div class="body">
        <div class="form-grid">
          <label class="wide">CSFloat link or text
            <input id="urlText" placeholder="Paste URL or exact market_hash_name">
          </label>
          <label class="wide">Skin name
            <input id="skinName" placeholder="AK-47 | Slate (Field-Tested)">
          </label>
          <label>Buy price, $
            <input id="buyPrice" inputmode="decimal" placeholder="12.34">
          </label>
          <label>Float
            <input id="floatValue" inputmode="decimal" placeholder="0.123456">
          </label>
          <details class="disclosure advanced-fields wide">
            <summary><span>Advanced item data</span><small>fee, pattern, paint index and raw sticker JSON</small></summary>
            <div class="form-grid disclosure-body">
              <label>Fee, %
                <input id="feePercent" inputmode="decimal" value="4">
              </label>
              <label>Pattern seed
                <input id="paintSeed" inputmode="numeric" placeholder="123">
              </label>
              <label>Paint index
                <input id="paintIndex" inputmode="numeric" placeholder="344">
              </label>
              <label>Reference base price, cents
                <input id="referenceBasePrice" inputmode="numeric" placeholder="optional">
              </label>
              <label class="wide">Stickers JSON
                <textarea id="stickersJson" placeholder='Optional: [{"sticker_id":59,"name":"Sticker | iBUYPOWER | Katowice 2014","price_usd":1200,"wear":0,"slot":0}]'></textarea>
              </label>
            </div>
          </details>
        </div>
        <div class="actions">
          <button class="button" id="parseTextButton" type="button">Fill From Text</button>
          <button class="button" id="fetchCsfloatButton" type="button">Fetch CSFloat</button>
          <button class="button button-primary" id="predictButton" type="button">Compare models</button>
          <span class="muted small" id="inputHint">empty sticker price will be estimated from DB</span>
        </div>
      </div>
      <details class="disclosure sticker-editor">
        <summary><span>Manual sticker editor</span><small>up to five applied stickers</small></summary>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th style="width: 70px;">Slot</th>
                <th style="width: 110px;">ID</th>
                <th>Name</th>
                <th style="width: 110px;">Price $</th>
                <th style="width: 100px;">Wear</th>
              </tr>
            </thead>
            <tbody id="stickerRows"></tbody>
          </table>
        </div>
      </details>
    </section>

    <section class="panel">
      <div class="section-head">
        <div><span class="eyebrow">RESULT</span><h2>Conservative outcome</h2></div>
        <span class="verdict" id="verdict">waiting</span>
      </div>
      <div class="body grid">
        <div class="comparison-callout" id="comparisonCallout" data-tone="neutral">
          <div>
            <span class="eyebrow">AGREEMENT</span>
            <strong id="comparisonTitle">Run a comparison</strong>
            <p id="comparisonExplanation">The safest available estimate will lead, while every model remains visible below.</p>
          </div>
          <span class="comparison-spread" id="comparisonSpread">spread unknown</span>
        </div>
        <div class="model-comparison-grid" id="modelComparisonGrid" aria-label="Model predictions">
          <article class="model-prediction-card"><span>Almaz</span><strong>waiting</strong></article>
          <article class="model-prediction-card"><span>Igor</span><strong>waiting</strong></article>
          <article class="model-prediction-card"><span>Nova</span><strong>waiting</strong></article>
        </div>
        <div class="grid metrics">
          <div class="metric">
            <div class="label">Conservative Estimate</div>
            <div class="value" id="predictedPrice">$0.00</div>
            <div class="hint" id="priceRange">range unknown</div>
          </div>
          <div class="metric">
            <div class="label">Net After Fee</div>
            <div class="value" id="netPrice">$0.00</div>
            <div class="hint" id="feeHint">fee 4%</div>
          </div>
          <div class="metric">
            <div class="label">Profit / ROI</div>
            <div class="value" id="profitValue">$0.00</div>
            <div class="hint" id="roiValue">enter buy price</div>
          </div>
          <div class="metric">
            <div class="label">Liquidity</div>
            <div class="value" id="liquidityValue">0</div>
            <div class="hint" id="liquidityHint">historical sales</div>
          </div>
          <div class="metric">
            <div class="label">Sticker Value</div>
            <div class="value" id="stickerValue">$0.00</div>
            <div class="hint" id="stickerCount">0 stickers</div>
          </div>
          <div class="metric">
            <div class="label">Selected Model</div>
            <div class="value" id="modelVersion">unknown</div>
            <div class="hint" id="trainedAt">not loaded</div>
          </div>
        </div>
        <details class="disclosure result-details">
          <summary><span>Technical model output</span><small id="predictedSkin">no skin</small></summary>
          <div class="body"><pre class="mono small" id="rawResult">Run comparison.</pre></div>
        </details>
      </div>
    </section>
    </div>
  </main>

  <script>
    const dashboardToken = new URLSearchParams(window.location.search).get('token') || '__DASHBOARD_TOKEN__';
    const moneyFmt = new Intl.NumberFormat('__CS2_INTL_LOCALE__', { style: 'currency', currency: 'USD' });
    const fmt = new Intl.NumberFormat('__CS2_INTL_LOCALE__');
    let fetchedDealPayload = {};

    function applyTheme() {
      const select = document.getElementById('themeSelect');
      if (!select) return;
      const value = select.value;
      document.body.classList.remove('theme-light', 'theme-midnight', 'theme-graphite');
      if (value !== 'dark') document.body.classList.add(`theme-${value}`);
      try {
        localStorage.setItem('siteTheme', value);
      } catch (error) {}
    }

    try {
      const savedTheme = localStorage.getItem('siteTheme') || localStorage.getItem('bestDealsTheme');
      if (savedTheme && ['dark', 'light', 'midnight', 'graphite'].includes(savedTheme)) {
        document.getElementById('themeSelect').value = savedTheme;
      }
    } catch (error) {}
    applyTheme();
    document.getElementById('themeSelect').addEventListener('change', applyTheme);

    function money(cents) {
      return moneyFmt.format((Number(cents) || 0) / 100);
    }

    function val(id) {
      return document.getElementById(id).value.trim();
    }

    function setText(id, value) {
      document.getElementById(id).textContent = value;
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }

    function optionalMoney(cents) {
      return cents === null || cents === undefined || cents === ''
        ? 'unknown'
        : money(cents);
    }

    function modelLabel(name) {
      return ({ almaz: 'Almaz', igor: 'Igor', nova: 'Nova' })[name] || String(name || 'Model');
    }

    function parseName(text) {
      text = String(text || '').trim();
      if (text.includes(' | ') && !text.startsWith('http')) return text;
      try {
        const url = new URL(text);
        for (const key of ['market_hash_name', 'name', 'item_name', 'hash_name']) {
          const value = url.searchParams.get(key);
          if (value) return decodeURIComponent(value);
        }
        const parts = url.pathname.split('/').filter(Boolean).map(decodeURIComponent);
        const historyIndex = parts.indexOf('history');
        if (historyIndex >= 0 && parts[historyIndex + 1]) return parts[historyIndex + 1];
        return parts.find(part => part.includes(' | ')) || '';
      } catch {
        return text.includes(' | ') ? text : '';
      }
    }

    function renderStickerRows() {
      const body = document.getElementById('stickerRows');
      body.innerHTML = '';
      for (let i = 0; i < 5; i += 1) {
        body.insertAdjacentHTML('beforeend', `
          <tr>
            <td><input data-sticker-slot="${i}" value="${i}"></td>
            <td><input data-sticker-id="${i}" placeholder="id"></td>
            <td><input data-sticker-name="${i}" placeholder="Sticker name"></td>
            <td><input data-sticker-price="${i}" inputmode="decimal" placeholder="$"></td>
            <td><input data-sticker-wear="${i}" inputmode="decimal" placeholder="0"></td>
          </tr>
        `);
      }
    }

    function stickersFromManualRows() {
      const stickers = [];
      for (let i = 0; i < 5; i += 1) {
        const name = document.querySelector(`[data-sticker-name="${i}"]`).value.trim();
        const stickerId = document.querySelector(`[data-sticker-id="${i}"]`).value.trim();
        const priceUsd = document.querySelector(`[data-sticker-price="${i}"]`).value.trim();
        const wear = document.querySelector(`[data-sticker-wear="${i}"]`).value.trim();
        const slot = document.querySelector(`[data-sticker-slot="${i}"]`).value.trim();
        if (!name && !stickerId) continue;
        stickers.push({
          sticker_id: stickerId || undefined,
          name,
          price_usd: priceUsd || undefined,
          wear: wear || undefined,
          slot: slot || i,
        });
      }
      return stickers;
    }

    function stickersFromJson() {
      const raw = val('stickersJson');
      if (!raw) return [];
      return JSON.parse(raw);
    }

    function buildPayload() {
      let stickers = stickersFromManualRows();
      const jsonStickers = stickersFromJson();
      if (Array.isArray(jsonStickers) && jsonStickers.length) {
        stickers = jsonStickers;
      } else if (jsonStickers && typeof jsonStickers === 'object') {
        stickers = jsonStickers;
      }

      const payload = { ...fetchedDealPayload };
      const overrides = {
        url_text: val('urlText'),
        name: val('skinName') || parseName(val('urlText')),
        buy_price_usd: val('buyPrice'),
        fee_percent: val('feePercent') || '4',
        float_value: val('floatValue'),
        paint_seed: val('paintSeed'),
        paint_index: val('paintIndex'),
        reference_base_price: val('referenceBasePrice'),
      };
      for (const [key, value] of Object.entries(overrides)) {
        if (value !== '') payload[key] = value;
      }
      if (Array.isArray(stickers) ? stickers.length : stickers && typeof stickers === 'object') {
        payload.stickers = stickers;
      }
      return payload;
    }

    function fillDealForm(payload) {
      if (!payload) return;
      if (payload.url_text) document.getElementById('urlText').value = payload.url_text;
      if (payload.name) document.getElementById('skinName').value = payload.name;
      if (payload.buy_price_cents) document.getElementById('buyPrice').value = (Number(payload.buy_price_cents) / 100).toFixed(2);
      if (payload.float_value !== null && payload.float_value !== undefined) document.getElementById('floatValue').value = payload.float_value;
      if (payload.paint_seed !== null && payload.paint_seed !== undefined) document.getElementById('paintSeed').value = payload.paint_seed;
      if (payload.paint_index !== null && payload.paint_index !== undefined) document.getElementById('paintIndex').value = payload.paint_index;
      if (payload.reference_base_price) document.getElementById('referenceBasePrice').value = payload.reference_base_price;
      if (payload.stickers && payload.stickers.length) {
        document.getElementById('stickersJson').value = JSON.stringify(payload.stickers, null, 2);
        for (let i = 0; i < 5; i += 1) {
          const sticker = payload.stickers[i] || {};
          document.querySelector(`[data-sticker-slot="${i}"]`).value = sticker.slot ?? i;
          document.querySelector(`[data-sticker-id="${i}"]`).value = sticker.sticker_id ?? '';
          document.querySelector(`[data-sticker-name="${i}"]`).value = sticker.name ?? '';
          document.querySelector(`[data-sticker-price="${i}"]`).value = sticker.price_cents ? (Number(sticker.price_cents) / 100).toFixed(2) : '';
          document.querySelector(`[data-sticker-wear="${i}"]`).value = sticker.wear ?? '';
        }
      }
    }

    function renderResult(payload) {
      const models = payload.models && typeof payload.models === 'object' ? payload.models : {};
      const modelNames = Object.keys(models);
      const isComparison = modelNames.length > 0;
      const selectedName = payload.selected_model || modelNames[0] || payload.model_name || 'nova';
      const data = isComparison ? (models[selectedName] || payload) : payload;
      const comparison = payload.comparison || {};
      const verdict = document.getElementById('verdict');
      const callout = document.getElementById('comparisonCallout');
      const comparisonGrid = document.getElementById('modelComparisonGrid');

      if (isComparison) {
        const states = {
          unanimous_three: ['All three agree', 'good', 'All three models report a positive signal.'],
          consensus: ['Safe consensus', 'good', 'At least two available models are positive and no active model disagrees.'],
          disagreement: ['Models disagree', 'bad', 'At least one active model disagrees. Treat the listing as high risk.'],
          single_signal: ['Single positive signal', 'watch', 'Only one model reports a positive signal. There is not enough agreement.'],
          no_positive: ['No positive signal', 'bad', 'No available model currently supports the purchase.'],
        };
        const [title, tone, explanation] = states[comparison.confidence] || ['Comparison ready', 'watch', 'Review every estimate before deciding.'];
        const unavailable = comparison.unavailable_models || [];
        const selectedLabel = modelLabel(comparison.conservative_model || selectedName);
        callout.hidden = false;
        callout.dataset.tone = tone;
        comparisonGrid.hidden = false;
        setText('comparisonTitle', title);
        setText(
          'comparisonExplanation',
          `${explanation} Headline figures use ${selectedLabel}, the lowest available estimate.${unavailable.length ? ` Unavailable: ${unavailable.map(modelLabel).join(', ')}.` : ''}`,
        );
        setText(
          'comparisonSpread',
          comparison.prediction_spread_cents === null || comparison.prediction_spread_cents === undefined
            ? 'spread unknown'
            : `model spread ${money(comparison.prediction_spread_cents)}`,
        );
        verdict.className = `verdict ${tone}`;
        verdict.textContent = title;

        const errors = payload.model_errors || {};
        comparisonGrid.innerHTML = ['almaz', 'igor', 'nova'].map(name => {
          const model = models[name];
          if (!model) {
            return `<article class="model-prediction-card unavailable"><div class="model-prediction-head"><span>${modelLabel(name)}</span><span class="model-state bad">unavailable</span></div><strong>—</strong><small>${escapeHtml(errors[name] || 'no prediction')}</small></article>`;
          }
          const selected = name === selectedName ? ' selected' : '';
          const verdictName = String(model.verdict || 'unknown');
          const verdictTone = ['good'].includes(verdictName) ? 'good' : (['watch', 'thin'].includes(verdictName) ? 'watch' : 'bad');
          const roi = model.roi_percent === null || model.roi_percent === undefined
            ? 'ROI unknown'
            : `${Number(model.roi_percent).toFixed(2)}% ROI`;
          const profit = model.profit_cents === null || model.profit_cents === undefined
            ? 'profit unknown'
            : `${money(model.profit_cents)} profit`;
          return `<article class="model-prediction-card${selected}">
            <div class="model-prediction-head"><span>${modelLabel(name)}</span><span class="model-state ${verdictTone}">${escapeHtml(verdictName)}</span></div>
            <strong>${optionalMoney(model.predicted_price_cents)}</strong>
            <small>${optionalMoney(model.low_price_cents)} – ${optionalMoney(model.high_price_cents)}</small>
            <div class="model-prediction-foot">${escapeHtml(roi)} · ${escapeHtml(profit)}</div>
          </article>`;
        }).join('');
      } else {
        callout.hidden = true;
        comparisonGrid.hidden = true;
        verdict.className = `verdict ${data.verdict || ''}`;
        verdict.textContent = data.verdict || 'unknown';
      }

      setText('predictedPrice', money(data.predicted_price_cents));
      setText('priceRange', `${money(data.low_price_cents)} - ${money(data.high_price_cents)}`);
      setText('netPrice', money(data.net_expected_cents));
      setText('feeHint', `fee ${data.fee_percent}%`);
      setText('profitValue', data.profit_cents === null || data.profit_cents === undefined ? 'no buy price' : money(data.profit_cents));
      setText('roiValue', data.roi_percent === null || data.roi_percent === undefined ? 'enter buy price' : `${Number(data.roi_percent).toFixed(2)}% ROI`);
      setText('liquidityValue', Number(data.liquidity_score || 0).toFixed(0));
      setText('liquidityHint', `${fmt.format(data.historical_sales_count || 0)} sales | ${Number(data.sales_per_day || 0).toFixed(2)} per day`);
      setText('stickerValue', money(data.sticker_value_cents));
      setText('stickerCount', `${fmt.format(data.sticker_count || 0)} stickers`);
      setText('modelVersion', `${modelLabel(selectedName)} · v${data.artifact_version || '?'}`);
      setText('trainedAt', `${data.pricing_method || 'model'} | ${data.trained_at || 'unknown'}`);
      setText('predictedSkin', data.name || 'unknown');
      setText('rawResult', JSON.stringify(payload, null, 2));
    }

    document.getElementById('parseTextButton').addEventListener('click', () => {
      const name = parseName(val('urlText'));
      if (name) {
        document.getElementById('skinName').value = name;
        setText('inputHint', `filled: ${name}`);
      } else {
        setText('inputHint', 'could not parse skin name from text');
      }
    });

    async function runPrediction() {
      try {
        setText('modelStatus', 'predicting');
        const response = await fetch(`/api/predict-deal?token=${encodeURIComponent(dashboardToken)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(buildPayload()),
        });
        const data = await response.json();
        if (!response.ok || data.error) {
          throw new Error(data.error || `HTTP ${response.status}`);
        }
        renderResult(data);
        setText('modelStatus', 'ready');
      } catch (error) {
        setText('modelStatus', `error: ${error.message}`);
        setText('rawResult', error.stack || error.message);
      }
    }

    document.getElementById('predictButton').addEventListener('click', runPrediction);

    document.getElementById('fetchCsfloatButton').addEventListener('click', async () => {
      try {
        const text = val('urlText') || val('skinName');
        if (!text) {
          throw new Error('paste CSFloat link first');
        }
        setText('modelStatus', 'fetching CSFloat');
        const response = await fetch(`/api/fetch-csfloat-listing?token=${encodeURIComponent(dashboardToken)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url_text: text }),
        });
        const data = await response.json();
        if (!response.ok || data.error) {
          throw new Error(data.error || `HTTP ${response.status}`);
        }
        fetchedDealPayload = data.deal_payload || {};
        fillDealForm(fetchedDealPayload);
        setText('inputHint', `loaded via ${data.key_file} / ${data.proxy} | remaining ${data.rate_remaining ?? 'unknown'}`);
        setText('modelStatus', 'loaded from CSFloat');
        await runPrediction();
      } catch (error) {
        setText('modelStatus', `error: ${error.message}`);
        setText('rawResult', error.stack || error.message);
      }
    });

    if (window.location.pathname === '/neural') {
      document.getElementById('comparisonCallout').hidden = true;
      document.getElementById('modelComparisonGrid').hidden = true;
    }
    renderStickerRows();
  </script>
</body>
</html>
"""


DEALS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CSFloat Best Deals</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101114;
      --panel: #181a1f;
      --panel-2: #20232a;
      --control-bg: #111318;
      --table-head: #15171c;
      --image-bg: #0d0f13;
      --modal-bg: #0d0f13;
      --modal-backdrop: rgba(0, 0, 0, 0.78);
      --text: #eef0f4;
      --muted: #9ca3af;
      --line: #30343d;
      --good: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
      --accent: #38bdf8;
    }
    body.theme-light {
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --panel-2: #eef2f7;
      --control-bg: #ffffff;
      --table-head: #e8edf5;
      --image-bg: #f7f9fc;
      --modal-bg: #ffffff;
      --modal-backdrop: rgba(15, 23, 42, 0.56);
      --text: #111827;
      --muted: #667085;
      --line: #d6dce7;
      --good: #15803d;
      --warn: #b45309;
      --bad: #b91c1c;
      --accent: #0369a1;
    }
    body.theme-midnight {
      color-scheme: dark;
      --bg: #070b12;
      --panel: #101722;
      --panel-2: #162131;
      --control-bg: #0b111b;
      --table-head: #111a27;
      --image-bg: #080d15;
      --modal-bg: #080d15;
      --modal-backdrop: rgba(0, 0, 0, 0.84);
      --text: #edf6ff;
      --muted: #93a4b8;
      --line: #253447;
      --good: #2dd4bf;
      --warn: #fbbf24;
      --bad: #fb7185;
      --accent: #60a5fa;
    }
    body.theme-graphite {
      color-scheme: dark;
      --bg: #151515;
      --panel: #202020;
      --panel-2: #2a2a2a;
      --control-bg: #181818;
      --table-head: #242424;
      --image-bg: #101010;
      --modal-bg: #111111;
      --modal-backdrop: rgba(0, 0, 0, 0.8);
      --text: #f2f2f2;
      --muted: #a8a8a8;
      --line: #3a3a3a;
      --good: #84cc16;
      --warn: #f97316;
      --bad: #ef4444;
      --accent: #eab308;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 2;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--bg) 94%, transparent);
      backdrop-filter: blur(10px);
    }
    .wrap {
      width: min(1500px, calc(100vw - 28px));
      margin: 0 auto;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      min-height: 64px;
    }
    .top-actions, .section-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 760; }
    h2 { margin: 0; font-size: 15px; font-weight: 720; }
    .sub { color: var(--muted); font-size: 13px; margin-top: 2px; }
    main { padding: 18px 0 28px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-2);
    }
    .button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--control-bg);
      color: var(--text);
      padding: 0 11px;
      text-decoration: none;
      font: inherit;
      white-space: nowrap;
      cursor: pointer;
    }
    .button:hover { border-color: var(--accent); }
    .button:disabled { opacity: 0.45; cursor: default; }
    input, select {
      width: min(360px, 52vw);
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--control-bg);
      color: var(--text);
      padding: 8px 9px;
      font: inherit;
      outline: none;
    }
    select { width: auto; }
    .control {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 34px;
    }
    .control-label {
      color: var(--muted);
      font-size: 12px;
      line-height: 1;
      white-space: nowrap;
    }
    .price-input {
      width: 92px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: var(--control-bg);
      white-space: nowrap;
    }
    .scroll { overflow-y: auto; overflow-x: hidden; max-height: calc(100vh - 154px); }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td {
      padding: 9px 8px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 1;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      background: var(--table-head);
      white-space: nowrap;
    }
    td { white-space: normal; }
    .num-cell {
      text-align: right;
      white-space: nowrap;
    }
    .skin-cell { white-space: normal; }
    .skin-name { color: var(--text); font-weight: 680; }
    .meta { margin-top: 3px; color: var(--muted); font-size: 12px; }
    .money-good { color: var(--good); font-weight: 720; }
    .money-bad { color: var(--bad); font-weight: 720; }
    .verdict {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 26px;
      border-radius: 999px;
      padding: 0 9px;
      border: 1px solid var(--line);
      background: var(--control-bg);
      font-weight: 720;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
    .good .dot { background: var(--good); }
    .watch .dot, .thin .dot { background: var(--warn); }
    .bad .dot { background: var(--bad); }
    .good { color: var(--good); }
    .watch, .thin { color: var(--warn); }
    .bad { color: var(--bad); }
    .link {
      color: var(--accent);
      text-decoration: none;
      font-weight: 650;
      margin-right: 8px;
    }
    .link:hover { text-decoration: underline; }
    .empty {
      padding: 22px 14px;
      color: var(--muted);
    }
    .preview {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      min-width: 0;
    }
    .item-img {
      width: 240px;
      height: 180px;
      object-fit: contain;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--image-bg);
      flex: 0 0 auto;
    }
    .zoomable {
      cursor: zoom-in;
      transition: border-color 0.15s ease, transform 0.15s ease;
    }
    .zoomable:hover {
      border-color: var(--accent);
      transform: scale(1.02);
    }
    .sticker-list {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 6px;
    }
    .sticker-pill {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      max-width: 260px;
      padding: 3px 6px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--control-bg);
      color: var(--muted);
      font-size: 12px;
    }
    .sticker-img {
      width: 32px;
      height: 32px;
      object-fit: contain;
      flex: 0 0 auto;
    }
    .image-modal {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: var(--modal-backdrop);
    }
    .image-modal.open { display: flex; }
    .image-modal-inner {
      width: min(1500px, calc(100vw - 24px));
      max-height: calc(100vh - 36px);
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--modal-bg);
      overflow: hidden;
      box-shadow: 0 18px 60px rgba(0, 0, 0, 0.5);
    }
    .image-modal-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-2);
    }
    .image-modal-actions {
      display: flex;
      align-items: center;
      gap: 7px;
      flex: 0 0 auto;
    }
    .image-modal-title {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 720;
    }
    .image-modal-body {
      position: relative;
      height: calc(100vh - 112px);
      min-height: 520px;
      overflow: hidden;
      cursor: grab;
      background:
        linear-gradient(45deg, rgba(255,255,255,0.035) 25%, transparent 25%),
        linear-gradient(-45deg, rgba(255,255,255,0.035) 25%, transparent 25%),
        linear-gradient(45deg, transparent 75%, rgba(255,255,255,0.035) 75%),
        linear-gradient(-45deg, transparent 75%, rgba(255,255,255,0.035) 75%);
      background-size: 22px 22px;
      background-position: 0 0, 0 11px, 11px -11px, -11px 0;
    }
    .image-modal-body.dragging {
      cursor: grabbing;
    }
    .image-modal img {
      position: absolute;
      left: 50%;
      top: 50%;
      max-width: none;
      max-height: none;
      transform-origin: center center;
      object-fit: contain;
      user-select: none;
      -webkit-user-drag: none;
      will-change: transform;
    }
    .model-box {
      display: grid;
      gap: 3px;
      white-space: normal;
    }
    .model-stack {
      display: grid;
      gap: 8px;
    }
    .model-title {
      font-weight: 760;
      color: var(--text);
    }
    .detail-stack {
      display: grid;
      gap: 6px;
    }
    .detail-line {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
      padding-bottom: 4px;
    }
    .detail-line:last-child { border-bottom: 0; padding-bottom: 0; }
    .detail-label {
      color: var(--muted);
      flex: 0 0 auto;
    }
    .link-stack {
      display: grid;
      gap: 6px;
      justify-items: start;
    }
    .category {
      display: inline-flex;
      align-items: center;
      min-height: 26px;
      border-radius: 999px;
      padding: 0 9px;
      border: 1px solid var(--line);
      color: var(--text);
      background: var(--control-bg);
      font-weight: 720;
    }
    .confidence-safe { color: var(--good); border-color: color-mix(in srgb, var(--good) 55%, var(--line)); }
    .confidence-warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 55%, var(--line)); }
    .confidence-bad { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 55%, var(--line)); }
    .risk-note {
      padding: 9px 14px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      background: var(--control-bg);
      font-size: 12px;
    }
    .mobile-deals {
      display: none;
    }
    body.force-cards .scroll {
      display: none;
    }
    body.force-cards .mobile-deals {
      display: grid;
    }
    body.force-table .scroll {
      display: block;
    }
    body.force-table .mobile-deals {
      display: none;
    }
    .deal-card {
      border-bottom: 1px solid var(--line);
      padding: 12px;
      background: var(--panel);
    }
    .deal-card:last-child { border-bottom: 0; }
    .deal-card-head {
      display: grid;
      grid-template-columns: 156px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
    }
    .deal-card .item-img {
      width: 156px;
      height: 118px;
    }
    .deal-card-title {
      font-size: 15px;
      font-weight: 760;
      line-height: 1.25;
    }
    .deal-price {
      margin-top: 8px;
      font-size: 22px;
      font-weight: 820;
      color: var(--accent);
    }
    .deal-card-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 10px;
    }
    .deal-mini {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      background: var(--control-bg);
      min-width: 0;
    }
    .deal-mini-label {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }
    .deal-mini-value {
      margin-top: 2px;
      font-weight: 720;
      overflow-wrap: anywhere;
    }
    .deal-card-section {
      margin-top: 10px;
    }
    .deal-card-section .model-stack {
      gap: 6px;
    }
    @media (max-width: 760px) {
      .wrap { width: min(100vw - 18px, 1500px); }
      .topbar, .section-head { align-items: flex-start; flex-direction: column; }
      .top-actions, .section-actions { width: 100%; }
      .top-actions .button { flex: 1 1 130px; }
      .control { width: 100%; }
      input, select { width: 100%; }
      .price-input { width: calc(50% - 4px); }
      .scroll { display: none; }
      .mobile-deals {
        display: grid;
      }
      .pill {
        width: 100%;
        justify-content: center;
        text-align: center;
        white-space: normal;
      }
      .image-modal {
        padding: 8px;
      }
      .image-modal-inner {
        width: calc(100vw - 16px);
        max-height: calc(100vh - 16px);
      }
      .image-modal-head {
        align-items: stretch;
        flex-direction: column;
      }
      .image-modal-actions {
        width: 100%;
        flex-wrap: wrap;
      }
      .image-modal-actions .button {
        flex: 1 1 64px;
      }
      .image-modal-body {
        min-height: 360px;
        height: calc(100vh - 150px);
      }
    }
    @media (max-width: 440px) {
      .deal-card-head {
        grid-template-columns: 1fr;
      }
      .deal-card .item-img {
        width: 100%;
        height: 220px;
      }
      .deal-card-grid {
        grid-template-columns: 1fr;
      }
      .sticker-pill {
        max-width: 100%;
        width: 100%;
      }
    }
  </style>
</head>
<body class="page-deals">
  <header>
    <div class="wrap topbar">
      <a class="site-brand" href="/" aria-label="CS2 Intelligence home">
        <span class="brand-mark" aria-hidden="true">C2</span>
        <span><strong>CS2 Intelligence</strong><small id="lastUpdate">model_deal_candidates</small></span>
      </a>
      <nav class="site-nav" aria-label="Main navigation">
        <a class="nav-link" data-nav-path="/" href="/?token=__DASHBOARD_TOKEN__">Overview</a>
        <a class="nav-link" data-nav-path="/deals" href="/deals?token=__DASHBOARD_TOKEN__">Deals</a>
        <a class="nav-link" data-nav-path="/strategy" href="/strategy?token=__DASHBOARD_TOKEN__">Strategy</a>
        <a class="nav-link" data-nav-path="/deal" href="/deal?token=__DASHBOARD_TOKEN__">Сравнение</a>
        <a class="nav-link" data-nav-path="/account" href="/account">Инвентарь</a>
      </nav>
      <div class="top-actions header-tools">
        <label class="control">
          <span class="control-label">Theme</span>
          <select id="themeSelect" title="Theme">
            <option value="dark">Dark</option>
            <option value="light">Light</option>
            <option value="midnight">Midnight</option>
            <option value="graphite">Graphite</option>
          </select>
        </label>
      </div>
    </div>
  </header>

  <main class="wrap">
    <section class="tool-hero deals-hero">
      <div>
        <div class="eyebrow">LIVE OPPORTUNITIES</div>
        <h1>Best deals, ranked by evidence.</h1>
        <p>Start with safe consensus, then narrow by price, freshness and model agreement when you need more control.</p>
      </div>
      <a class="button button-primary" href="/deal?token=__DASHBOARD_TOKEN__">Check another listing</a>
    </section>

    <section class="panel">
      <div class="section-head">
        <div><span class="eyebrow">WATCHLIST</span><h2>Saved profitable listings</h2></div>
        <span class="pill" id="pageInfo">loading</span>
      </div>
      <div class="filter-shell">
        <div class="filter-primary">
          <label class="search-field"><span class="sr-only">Search deals</span><input id="search" placeholder="Search skin, sticker or listing ID"></label>
          <label class="filter-field"><span>Sort</span><select id="sortSelect" title="Sort deals">
            <option value="seen_new">Newly scanned</option>
            <option value="created_new">New listings</option>
            <option value="best_roi">Best ROI</option>
            <option value="conservative_roi">Best conservative ROI</option>
            <option value="best_profit">Best profit</option>
            <option value="price_low">Cheapest first</option>
            <option value="price_high">Most expensive</option>
            <option value="new_price_low">New + cheap</option>
            <option value="liquidity_high">Most liquid</option>
            <option value="sticker_value_high">Sticker value</option>
            <option value="float_low">Lowest float</option>
          </select></label>
          <button class="button button-primary" id="searchButton" type="button">Apply</button>
        </div>
        <details class="disclosure filter-disclosure">
          <summary><span>More filters</span><small>модели, цена, безопасный ROI/прибыль, ликвидность, float и стикеры</small></summary>
          <div class="filter-grid disclosure-body">
            <label class="filter-field"><span>View</span><select id="viewSelect" title="View mode">
              <option value="auto">Auto view</option>
              <option value="cards">Cards</option>
              <option value="table">Table</option>
            </select></label>
            <label class="filter-field"><span>Source model</span><select id="categorySelect" title="Model match filter">
              <option value="all">All models</option>
              <option value="almaz">All Almaz</option>
              <option value="igor">All Igor</option>
              <option value="nova">All Nova</option>
              <option value="all_three">All three models</option>
              <option value="both">Both models</option>
              <option value="only_almaz">Only Almaz</option>
              <option value="only_igor">Only Igor</option>
              <option value="only_nova">Only Nova</option>
            </select></label>
            <label class="filter-field"><span>Agreement</span><select id="confidenceSelect" title="Agreement and risk filter">
              <option value="all">Any agreement</option>
              <option value="consensus">Safe consensus (2+)</option>
              <option value="unanimous_three">All three agree</option>
              <option value="disagreement">Has disagreement</option>
              <option value="single_signal">Single positive signal</option>
            </select></label>
            <label class="filter-field"><span>Sale type</span><select id="listingTypeSelect" title="Listing type">
              <option value="all">All sale types</option>
              <option value="buy_now">Buy now</option>
              <option value="auction">Auction</option>
              <option value="unknown">Unknown type</option>
            </select></label>
            <label class="filter-field"><span>Minimum price</span><input class="price-input" id="minPrice" type="number" min="0" step="0.01" placeholder="$0"></label>
            <label class="filter-field"><span>Maximum price</span><input class="price-input" id="maxPrice" type="number" min="0" step="0.01" placeholder="No limit"></label>
            <label class="filter-field"><span>Мин. безопасный ROI, %</span><input class="price-input" id="minRoi" type="number" step="0.1" placeholder="например 8"></label>
            <label class="filter-field"><span>Мин. безопасная прибыль, $</span><input class="price-input" id="minProfit" type="number" min="0" step="0.01" placeholder="например 1"></label>
            <label class="filter-field"><span>Мин. ликвидность, 0–100</span><input class="price-input" id="minLiquidity" type="number" min="0" max="100" step="1" placeholder="например 40"></label>
            <label class="filter-field"><span>Максимальный float</span><input class="price-input" id="maxFloat" type="number" min="0" max="1" step="0.000001" placeholder="например 0.15"></label>
            <label class="filter-field"><span>Мин. стоимость стикеров, $</span><input class="price-input" id="minStickerValue" type="number" min="0" step="0.01" placeholder="например 5"></label>
            <label class="filter-field"><span>Last scanned</span><select id="seenSelect" title="Only deals scanned recently">
              <option value="0">Any seen time</option>
              <option value="1">Seen 1h</option>
              <option value="6">Seen 6h</option>
              <option value="24" selected>Seen 24h</option>
              <option value="72">Seen 3d</option>
              <option value="168">Seen 7d</option>
            </select></label>
            <label class="filter-field"><span>Listing created</span><select id="createdSelect" title="Only newly listed deals">
              <option value="0">Any created time</option>
              <option value="1">Created 1h</option>
              <option value="6">Created 6h</option>
              <option value="24">Created 24h</option>
              <option value="72">Created 3d</option>
              <option value="168">Created 7d</option>
            </select></label>
            <button class="button" id="resetFiltersButton" type="button">Reset filters</button>
          </div>
        </details>
        <div class="pagination-bar">
          <span class="muted">100 opportunities per page</span>
          <div><button class="button" id="prevButton" type="button">Previous</button><button class="button" id="nextButton" type="button">Next</button></div>
        </div>
      </div>
      <div class="risk-note">A safe consensus means at least two models rate the listing positively and no active model disagrees. Conservative ROI uses the lowest estimate, not the most optimistic one.</div>
      <div class="deal-summary-grid" aria-label="Deal summary">
        <div class="summary-card"><span>Matching deals</span><strong id="dealSummaryTotal">0</strong></div>
        <div class="summary-card"><span>Safe consensus</span><strong id="dealSummarySafe">0</strong></div>
        <div class="summary-card"><span>All three agree</span><strong id="dealSummaryAllThree">0</strong></div>
        <div class="summary-card"><span>Model disagreement</span><strong id="dealSummaryDisagree">0</strong></div>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th style="width: 3%;">#</th>
              <th style="width: 38%;">Listing</th>
              <th style="width: 8%;">Buy</th>
              <th style="width: 22%;">Models</th>
              <th style="width: 14%;">Details</th>
              <th style="width: 11%;">Time</th>
              <th style="width: 4%;">Links</th>
            </tr>
          </thead>
          <tbody id="rowsBody"></tbody>
        </table>
        <div class="empty" id="emptyState" hidden>No saved deals yet. Start `deal_scanner.py` and wait until it finds listings that pass the filters.</div>
      </div>
      <div class="mobile-deals" id="mobileDeals"></div>
    </section>
  </main>

  <div class="image-modal" id="imageModal" aria-hidden="true">
    <div class="image-modal-inner">
      <div class="image-modal-head">
        <div class="image-modal-title" id="imageModalTitle"></div>
        <div class="image-modal-actions">
          <button class="button" id="imageZoomOut" type="button">-</button>
          <button class="button" id="imageZoomReset" type="button">100%</button>
          <button class="button" id="imageZoomFit" type="button">Fit</button>
          <button class="button" id="imageZoomIn" type="button">+</button>
          <button class="button" id="imageModalClose" type="button">Close</button>
        </div>
      </div>
      <div class="image-modal-body">
        <img id="imageModalImg" src="" alt="" referrerpolicy="no-referrer">
      </div>
    </div>
  </div>

  <script>
    const fmt = new Intl.NumberFormat('__CS2_INTL_LOCALE__');
    const moneyFmt = new Intl.NumberFormat('__CS2_INTL_LOCALE__', { style: 'currency', currency: 'USD' });
    const dashboardToken = new URLSearchParams(window.location.search).get('token') || '__DASHBOARD_TOKEN__';
    let offset = 0;
    const limit = 100;
    let latestRefresh = 0;
    const filterStateFields = {
      search: 'search',
      category: 'categorySelect',
      confidence: 'confidenceSelect',
      listing_type: 'listingTypeSelect',
      sort: 'sortSelect',
      seen_hours: 'seenSelect',
      created_hours: 'createdSelect',
      min_price: 'minPrice',
      max_price: 'maxPrice',
      min_roi: 'minRoi',
      min_profit: 'minProfit',
      min_liquidity: 'minLiquidity',
      max_float: 'maxFloat',
      min_sticker_value: 'minStickerValue',
    };
    const zoomState = {
      scale: 1,
      x: 0,
      y: 0,
      dragging: false,
      startX: 0,
      startY: 0,
      startOffsetX: 0,
      startOffsetY: 0,
    };

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }

    function safeExternalUrl(value, allowSteam = false) {
      if (!value) return '';
      try {
        const parsed = new URL(String(value), window.location.origin);
        const allowed = parsed.protocol === 'https:' || parsed.protocol === 'http:' ||
          (allowSteam && parsed.protocol === 'steam:');
        return allowed ? parsed.href : '';
      } catch (error) {
        return '';
      }
    }

    function money(cents) {
      return moneyFmt.format((Number(cents) || 0) / 100);
    }

    function optionalMoney(cents) {
      return cents === null || cents === undefined || cents === '' ? 'unknown' : money(cents);
    }

    function number(value, digits = 2) {
      if (value === null || value === undefined || value === '') return 'unknown';
      const parsed = Number(value);
      if (Number.isNaN(parsed)) return escapeHtml(value);
      return parsed.toFixed(digits);
    }

    function percent(value) {
      if (value === null || value === undefined || value === '') return 'unknown';
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return 'unknown';
      return `${parsed.toFixed(2)}%`;
    }

    function safePageOffset(value) {
      const raw = String(value ?? '').trim();
      if (!/^\d+$/.test(raw)) return 0;
      const parsed = Number(raw);
      if (!Number.isSafeInteger(parsed) || parsed < 0) return 0;
      return parsed - (parsed % limit);
    }

    function restoreFilterState() {
      const params = new URLSearchParams(window.location.search);
      for (const [param, id] of Object.entries(filterStateFields)) {
        if (!params.has(param)) continue;
        const element = document.getElementById(id);
        const value = params.get(param) || '';
        if (element.tagName === 'SELECT' && !Array.from(element.options).some(option => option.value === value)) {
          continue;
        }
        element.value = value;
      }
      offset = safePageOffset(params.get('offset') || '0');
    }

    function syncFilterState() {
      const params = new URLSearchParams(window.location.search);
      params.delete('token');
      for (const [param, id] of Object.entries(filterStateFields)) {
        const value = document.getElementById(id).value.trim();
        if (value) params.set(param, value);
        else params.delete(param);
      }
      if (offset > 0) params.set('offset', String(offset));
      else params.delete('offset');
      const query = params.toString();
      history.replaceState(null, '', `${window.location.pathname}${query ? `?${query}` : ''}`);
    }

    function priceInputFromCents(value) {
      if (value === null || value === undefined || value === '') return '';
      const cents = Number(value);
      if (!Number.isSafeInteger(cents) || cents < 0) return '';
      return (cents / 100).toFixed(2);
    }

    function applyNormalizedFilterState(data, requestedMinPrice, requestedMaxPrice, requestedThresholds = {}) {
      offset = safePageOffset(data.offset ?? offset);
      const minPriceInput = document.getElementById('minPrice');
      const maxPriceInput = document.getElementById('maxPrice');
      if (
        minPriceInput.value.trim() !== requestedMinPrice
        || maxPriceInput.value.trim() !== requestedMaxPrice
      ) return;
      minPriceInput.value = priceInputFromCents(data.min_price_cents);
      maxPriceInput.value = priceInputFromCents(data.max_price_cents);
      const normalized = {
        minRoi: data.min_roi_percent,
        minProfit: data.min_profit_cents === null || data.min_profit_cents === undefined ? null : Number(data.min_profit_cents) / 100,
        minLiquidity: data.min_liquidity_score,
        maxFloat: data.max_float_value,
        minStickerValue: data.min_sticker_value_cents === null || data.min_sticker_value_cents === undefined ? null : Number(data.min_sticker_value_cents) / 100,
      };
      for (const [id, value] of Object.entries(normalized)) {
        const input = document.getElementById(id);
        if (input.value.trim() !== String(requestedThresholds[id] ?? '').trim()) continue;
        input.value = value === null || value === undefined ? '' : String(value);
      }
      syncFilterState();
    }

    function msk(value) {
      if (!value) return 'unknown';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return escapeHtml(value);
      return date.toLocaleString('__CS2_INTL_LOCALE__', { timeZone: 'Europe/Moscow' });
    }

    function link(url, label) {
      const normalizedUrl = safeExternalUrl(url, true);
      if (!normalizedUrl) return '<span class="meta">none</span>';
      return `<a class="link" href="${escapeHtml(normalizedUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
    }

    function image(url, title, className = 'item-img', fallbackUrl = '') {
      const normalizedUrl = safeExternalUrl(url);
      const normalizedFallback = safeExternalUrl(fallbackUrl);
      if (!normalizedUrl) return `<div class="${className}"></div>`;
      const fallbackAttr = normalizedFallback && normalizedFallback !== normalizedUrl
        ? ` data-fallback-src="${escapeHtml(normalizedFallback)}" onerror="this.onerror=null;this.src=this.dataset.fallbackSrc;this.dataset.fullSrc=this.dataset.fallbackSrc"`
        : '';
      return `<img class="${className} zoomable" src="${escapeHtml(normalizedUrl)}" data-full-src="${escapeHtml(normalizedUrl)}" data-title="${escapeHtml(title || '')}" alt="" title="${escapeHtml(title || '')}" loading="lazy" referrerpolicy="no-referrer"${fallbackAttr}>`;
    }

    function openImageModal(url, title, fallbackUrl = '') {
      url = safeExternalUrl(url);
      fallbackUrl = safeExternalUrl(fallbackUrl);
      if (!url) return;
      const modal = document.getElementById('imageModal');
      const img = document.getElementById('imageModalImg');
      img.onerror = fallbackUrl && fallbackUrl !== url
        ? () => { img.onerror = null; img.src = fallbackUrl; }
        : null;
      resetImageZoom();
      img.src = url;
      document.getElementById('imageModalTitle').textContent = title || '';
      modal.classList.add('open');
      modal.setAttribute('aria-hidden', 'false');
    }

    function closeImageModal() {
      const modal = document.getElementById('imageModal');
      const img = document.getElementById('imageModalImg');
      modal.classList.remove('open');
      modal.setAttribute('aria-hidden', 'true');
      img.removeAttribute('src');
      img.onerror = null;
      resetImageZoom();
    }

    function applyImageZoom() {
      const img = document.getElementById('imageModalImg');
      img.style.transform = `translate(calc(-50% + ${zoomState.x}px), calc(-50% + ${zoomState.y}px)) scale(${zoomState.scale})`;
    }

    function resetImageZoom() {
      zoomState.scale = 1;
      zoomState.x = 0;
      zoomState.y = 0;
      zoomState.dragging = false;
      const img = document.getElementById('imageModalImg');
      if (img) img.style.transform = '';
      const body = document.querySelector('.image-modal-body');
      if (body) body.classList.remove('dragging');
    }

    function fitImageZoom() {
      const body = document.querySelector('.image-modal-body');
      const img = document.getElementById('imageModalImg');
      if (!body || !img || !img.naturalWidth || !img.naturalHeight) {
        resetImageZoom();
        return;
      }
      const scaleX = (body.clientWidth - 28) / img.naturalWidth;
      const scaleY = (body.clientHeight - 28) / img.naturalHeight;
      zoomState.scale = Math.min(1, Math.max(0.1, Math.min(scaleX, scaleY)));
      zoomState.x = 0;
      zoomState.y = 0;
      applyImageZoom();
    }

    function changeImageZoom(multiplier, originEvent = null) {
      const oldScale = zoomState.scale;
      const newScale = Math.max(0.25, Math.min(12, oldScale * multiplier));
      if (newScale === oldScale) return;
      if (originEvent) {
        const body = document.querySelector('.image-modal-body');
        if (body) {
          const rect = body.getBoundingClientRect();
          const relX = originEvent.clientX - rect.left - rect.width / 2 - zoomState.x;
          const relY = originEvent.clientY - rect.top - rect.height / 2 - zoomState.y;
          const ratio = newScale / oldScale;
          zoomState.x -= relX * (ratio - 1);
          zoomState.y -= relY * (ratio - 1);
        }
      }
      zoomState.scale = newScale;
      applyImageZoom();
    }

    function stickerList(stickers) {
      if (!stickers || !stickers.length) return '<div class="meta">no stickers</div>';
      return `<div class="sticker-list">${stickers.map(sticker => `
        <span class="sticker-pill" title="${escapeHtml(sticker.name || '')}">
          ${image(sticker.icon_url, sticker.name, 'sticker-img')}
          <span>${escapeHtml(sticker.name || 'unknown')}</span>
          <span>${money(sticker.price_cents || sticker.raw_price_cents || 0)}</span>
          <span>wear ${number(sticker.wear, 3)}</span>
        </span>
      `).join('')}</div>`;
    }

    function modelCell(model, label) {
      if (!model) {
        return `<div class="model-box"><div class="model-title">${escapeHtml(label)}</div><div class="meta">not found</div></div>`;
      }
      const verdict = model.verdict || 'unknown';
      const verdictClass = verdict === 'good' ? 'good' : (verdict === 'bad' ? 'bad' : 'watch');
      const profitClass = Number(model.profit_cents || 0) >= 0 ? 'money-good' : 'money-bad';
      const roiText = model.roi_percent === null || model.roi_percent === undefined ? 'unknown' : `${Number(model.roi_percent).toFixed(2)}%`;
      return `<div class="model-box">
        <div class="model-title">${escapeHtml(label)}: ${money(model.predicted_price_cents)}</div>
        <div>net ${money(model.net_expected_cents)} | <span class="${profitClass}">profit ${money(model.profit_cents)}</span></div>
        <div>ROI ${roiText} | liquid ${number(model.liquidity_score, 0)}</div>
        <div><span class="verdict ${verdictClass}"><span class="dot"></span>${escapeHtml(verdict)}</span></div>
        <div class="meta">${escapeHtml(model.pricing_method || '')}</div>
      </div>`;
    }

    function detailLine(label, value) {
      return `<div class="detail-line"><span class="detail-label">${escapeHtml(label)}</span><span>${value}</span></div>`;
    }

    function listingTypeLabel(value) {
      if (value === 'auction') return 'Auction';
      if (value === 'buy_now') return 'Buy now';
      return 'Unknown';
    }

    function confidenceBadge(row) {
      const confidence = row.confidence || 'single_signal';
      const labels = {
        unanimous_three: 'all 3 agree',
        consensus: 'safe consensus',
        disagreement: 'model disagreement',
        single_signal: 'single signal',
      };
      const className = confidence === 'unanimous_three' || confidence === 'consensus'
        ? 'confidence-safe'
        : (confidence === 'disagreement' ? 'confidence-bad' : 'confidence-warn');
      return `<span class="category ${className}">${escapeHtml(labels[confidence] || confidence)}</span>`;
    }

    function riskBadge(intelligence) {
      if (!intelligence || !intelligence.risk) {
        return '<span class="category confidence-warn">awaiting score</span>';
      }
      const risk = intelligence.risk;
      const score = Number(risk.score);
      const level = String(risk.level || 'unknown');
      const className = level === 'low'
        ? 'confidence-safe'
        : (level === 'high' ? 'confidence-bad' : 'confidence-warn');
      return `<span class="category ${className}">${escapeHtml(level)} ${Number.isFinite(score) ? score.toFixed(0) : '—'}/100</span>`;
    }

    function ensembleWeights(intelligence) {
      const weights = intelligence?.ensemble_prediction?.weights || {};
      const values = Object.entries(weights).map(([model, weight]) => {
        const number = Number(weight);
        return `${escapeHtml(model)} ${Number.isFinite(number) ? `${(number * 100).toFixed(0)}%` : '—'}`;
      });
      return values.length ? values.join(' · ') : 'not enough history';
    }

    function intelligenceDetailLines(row) {
      const intelligence = row.intelligence;
      if (!intelligence) return detailLine('risk', riskBadge(null));
      const ensemble = intelligence.ensemble_prediction || {};
      const sale = intelligence.sale_estimate || {};
      const probability = Number(sale.within_30d);
      const eta = Number(sale.estimated_days);
      const rank = Number(intelligence.ranking_score);
      return [
        detailLine('risk', riskBadge(intelligence)),
        detailLine('sale 30d / ETA', `${Number.isFinite(probability) ? `${(probability * 100).toFixed(0)}%` : '—'} / ${Number.isFinite(eta) ? `${eta.toFixed(eta < 10 ? 1 : 0)}d` : '—'}`),
        detailLine('ensemble safe', optionalMoney(ensemble.conservative_profit_cents)),
        detailLine('ranking', Number.isFinite(rank) ? rank.toFixed(1) : '—'),
        detailLine('weights', `<span class="meta">${ensembleWeights(intelligence)}</span>`),
      ].join('');
    }

    function discoverySourceLabel(row) {
      const sources = Array.isArray(row.discovery_sources) && row.discovery_sources.length
        ? row.discovery_sources
        : [{ source_type: row.source_type, source_name: row.source_name || row.source_sticker_name }];
      const labels = sources.map(source => source.source_type === 'liquid_skin'
        ? 'ликвидный ≤ $10'
        : source.source_type === 'sticker'
          ? `стикер: ${source.source_name || 'target'}`
          : (source.source_name || source.source_type || 'scanner'));
      return [...new Set(labels)].map(label => `<span class="category">${escapeHtml(label)}</span>`).join(' ');
    }

    function detailsCell(row) {
      return `<div class="detail-stack">
        ${intelligenceDetailLines(row)}
        ${detailLine('signal', confidenceBadge(row))}
        ${detailLine('models', `<span class="category">${escapeHtml(row.category || 'all')}</span>`)}
        ${detailLine('source', discoverySourceLabel(row))}
        ${detailLine('safe ROI', percent(row.conservative_roi_percent))}
        ${detailLine('safe profit', optionalMoney(row.conservative_profit_cents))}
        ${detailLine('model spread', percent(row.prediction_spread_percent))}
        ${detailLine('type', `<span class="category">${escapeHtml(listingTypeLabel(row.listing_type))}</span>`)}
        ${row.source_sticker_name ? detailLine('sticker', `${escapeHtml(row.source_sticker_name)}<div class="meta">id ${escapeHtml(row.source_sticker_id || '')}</div>`) : ''}
        ${detailLine('float', number(row.float_value, 6))}
        ${detailLine('seed', escapeHtml(row.paint_seed ?? 'unknown'))}
        ${detailLine('paint', escapeHtml(row.paint_index ?? 'unknown'))}
      </div>`;
    }

    function timeCell(row) {
      return `<div class="detail-stack">
        ${detailLine('created', msk(row.listing_created_at))}
        ${detailLine('seen', msk(row.last_seen_at))}
      </div>`;
    }

    function linksCell(row) {
      return `<div class="link-stack">${link(row.csfloat_url, 'CSFloat')}${link(row.inspect_link, 'Inspect')}</div>`;
    }

    function applyViewMode() {
      const value = document.getElementById('viewSelect').value;
      document.body.classList.toggle('force-cards', value === 'cards');
      document.body.classList.toggle('force-table', value === 'table');
      try {
        localStorage.setItem('bestDealsViewMode', value);
      } catch (error) {}
    }

    function applyTheme() {
      const value = document.getElementById('themeSelect').value;
      document.body.classList.remove('theme-light', 'theme-midnight', 'theme-graphite');
      if (value !== 'dark') {
        document.body.classList.add(`theme-${value}`);
      }
      try {
        localStorage.setItem('siteTheme', value);
        localStorage.setItem('bestDealsTheme', value);
      } catch (error) {}
    }

    function mini(label, value) {
      return `<div class="deal-mini"><div class="deal-mini-label">${escapeHtml(label)}</div><div class="deal-mini-value">${value}</div></div>`;
    }

    function mobileDealCard(row, index) {
      const models = row.models || {};
      const intelligence = row.intelligence;
      const ensemble = intelligence?.ensemble_prediction || {};
      const sale = intelligence?.sale_estimate || {};
      const saleProbability = Number(sale.within_30d);
      const saleEta = Number(sale.estimated_days);
      const ranking = Number(intelligence?.ranking_score);
      const previewUrl = row.screenshot_url || row.item_icon_url;
      return `<article class="deal-card">
        <div class="deal-card-head">
          ${image(previewUrl, row.skin_name, 'item-img', row.item_icon_url)}
          <div>
            <div class="deal-card-title">${escapeHtml(row.skin_name || 'unknown')}</div>
            <div class="meta">#${offset + index + 1} · id ${escapeHtml(row.listing_id || '')}</div>
            <div class="deal-price">${money(row.buy_price_cents)}</div>
            <div class="meta">${escapeHtml(listingTypeLabel(row.listing_type))}</div>
            <div class="meta">created ${msk(row.listing_created_at)}</div>
            <div class="meta">seen ${msk(row.last_seen_at)}</div>
          </div>
        </div>
        <div class="deal-card-section">
          <div class="model-stack">${modelCell(models.almaz, 'Almaz')}${modelCell(models.igor, 'Igor')}${modelCell(models.nova, 'Nova')}</div>
        </div>
        <div class="deal-card-grid">
          ${mini('risk', riskBadge(intelligence))}
          ${mini('sale 30d / ETA', `${Number.isFinite(saleProbability) ? `${(saleProbability * 100).toFixed(0)}%` : '—'} / ${Number.isFinite(saleEta) ? `${saleEta.toFixed(saleEta < 10 ? 1 : 0)}d` : '—'}`)}
          ${mini('ensemble safe profit', optionalMoney(ensemble.conservative_profit_cents))}
          ${mini('risk ranking', Number.isFinite(ranking) ? ranking.toFixed(1) : '—')}
          ${mini('dynamic weights', `<span class="meta">${ensembleWeights(intelligence)}</span>`)}
          ${mini('signal', confidenceBadge(row))}
          ${mini('models', `<span class="category">${escapeHtml(row.category || 'all')}</span>`)}
          ${mini('source', discoverySourceLabel(row))}
          ${mini('conservative ROI', percent(row.conservative_roi_percent))}
          ${mini('conservative profit', optionalMoney(row.conservative_profit_cents))}
          ${mini('model spread', percent(row.prediction_spread_percent))}
          ${mini('type', `<span class="category">${escapeHtml(listingTypeLabel(row.listing_type))}</span>`)}
          ${mini('float', number(row.float_value, 6))}
          ${mini('seed', escapeHtml(row.paint_seed ?? 'unknown'))}
          ${mini('paint', escapeHtml(row.paint_index ?? 'unknown'))}
          ${row.source_sticker_name ? mini('source sticker', `${escapeHtml(row.source_sticker_name)}<div class="meta">id ${escapeHtml(row.source_sticker_id || '')}</div>`) : ''}
          ${mini('links', linksCell(row))}
        </div>
        <div class="deal-card-section">
          ${stickerList(row.stickers || [])}
        </div>
      </article>`;
    }

    function render(data) {
      const total = data.total || 0;
      const items = data.rows || [];
      const shownFrom = total ? offset + 1 : 0;
      const shownTo = Math.min(offset + items.length, total);
      const summary = data.summary || {};
      document.getElementById('pageInfo').textContent =
        `${fmt.format(shownFrom)}-${fmt.format(shownTo)} / ${fmt.format(total)} | safe ${fmt.format((summary.consensus || 0) + (summary.unanimous_three || 0))} | disagree ${fmt.format(summary.disagreement || 0)} | all 3 ${fmt.format(summary.all_three || 0)}`;
      document.getElementById('dealSummaryTotal').textContent = fmt.format(total);
      document.getElementById('dealSummarySafe').textContent = fmt.format((summary.consensus || 0) + (summary.unanimous_three || 0));
      document.getElementById('dealSummaryAllThree').textContent = fmt.format(summary.all_three || 0);
      document.getElementById('dealSummaryDisagree').textContent = fmt.format(summary.disagreement || 0);
      document.getElementById('prevButton').disabled = offset <= 0;
      document.getElementById('nextButton').disabled = offset + limit >= total;
      document.getElementById('emptyState').hidden = items.length > 0;
      document.getElementById('lastUpdate').textContent = `updated ${new Date().toLocaleTimeString('__CS2_INTL_LOCALE__', { timeZone: 'Europe/Moscow' })} MSK`;

      document.getElementById('rowsBody').innerHTML = items.map((row, index) => {
        const models = row.models || {};
        const previewUrl = row.screenshot_url || row.item_icon_url;
        return `<tr>
          <td>${offset + index + 1}</td>
          <td class="skin-cell">
            <div class="preview">
              ${image(previewUrl, row.skin_name, 'item-img', row.item_icon_url)}
              <div>
                <div class="skin-name">${escapeHtml(row.skin_name || 'unknown')}</div>
                <div class="meta">id ${escapeHtml(row.listing_id || '')}</div>
                ${stickerList(row.stickers || [])}
              </div>
            </div>
          </td>
          <td class="num-cell">${money(row.buy_price_cents)}</td>
          <td><div class="model-stack">${modelCell(models.almaz, 'Almaz')}${modelCell(models.igor, 'Igor')}${modelCell(models.nova, 'Nova')}</div></td>
          <td>${detailsCell(row)}</td>
          <td>${timeCell(row)}</td>
          <td>${linksCell(row)}</td>
        </tr>`;
      }).join('');
      document.getElementById('mobileDeals').innerHTML = items.length
        ? items.map((row, index) => mobileDealCard(row, index)).join('')
        : '<div class="empty">No saved deals yet. Start `deal_scanner.py` and wait until it finds listings that pass the filters.</div>';
    }

    async function refresh() {
      const refreshId = ++latestRefresh;
      syncFilterState();
      const search = document.getElementById('search').value.trim();
      const category = document.getElementById('categorySelect').value;
      const confidence = document.getElementById('confidenceSelect').value;
      const listingType = document.getElementById('listingTypeSelect').value;
      const sort = document.getElementById('sortSelect').value;
      const seenHours = document.getElementById('seenSelect').value;
      const createdHours = document.getElementById('createdSelect').value;
      const minPrice = document.getElementById('minPrice').value.trim();
      const maxPrice = document.getElementById('maxPrice').value.trim();
      const thresholds = {
        minRoi: document.getElementById('minRoi').value.trim(),
        minProfit: document.getElementById('minProfit').value.trim(),
        minLiquidity: document.getElementById('minLiquidity').value.trim(),
        maxFloat: document.getElementById('maxFloat').value.trim(),
        minStickerValue: document.getElementById('minStickerValue').value.trim(),
      };
      const url = `/api/model-deal-comparison?token=${encodeURIComponent(dashboardToken)}&offset=${offset}&limit=${limit}&category=${encodeURIComponent(category)}&confidence=${encodeURIComponent(confidence)}&listing_type=${encodeURIComponent(listingType)}&sort=${encodeURIComponent(sort)}&seen_hours=${encodeURIComponent(seenHours)}&created_hours=${encodeURIComponent(createdHours)}&min_price=${encodeURIComponent(minPrice)}&max_price=${encodeURIComponent(maxPrice)}&min_roi=${encodeURIComponent(thresholds.minRoi)}&min_profit=${encodeURIComponent(thresholds.minProfit)}&min_liquidity=${encodeURIComponent(thresholds.minLiquidity)}&max_float=${encodeURIComponent(thresholds.maxFloat)}&min_sticker_value=${encodeURIComponent(thresholds.minStickerValue)}&search=${encodeURIComponent(search)}`;
      const response = await fetch(url, { cache: 'no-store' });
      const data = await response.json();
      if (refreshId !== latestRefresh) return;
      if (!response.ok) {
        throw new Error(data.error || response.statusText);
      }
      applyNormalizedFilterState(data, minPrice, maxPrice, thresholds);
      render(data);
    }

    document.getElementById('searchButton').addEventListener('click', () => {
      offset = 0;
      refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    });

    document.getElementById('search').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        offset = 0;
        refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
      }
    });

    document.getElementById('minPrice').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        offset = 0;
        refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
      }
    });

    document.getElementById('maxPrice').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        offset = 0;
        refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
      }
    });

    document.getElementById('categorySelect').addEventListener('change', () => {
      offset = 0;
      refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    });

    document.getElementById('confidenceSelect').addEventListener('change', () => {
      offset = 0;
      refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    });

    document.getElementById('listingTypeSelect').addEventListener('change', () => {
      offset = 0;
      refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    });

    document.getElementById('resetFiltersButton').addEventListener('click', () => {
      document.getElementById('search').value = '';
      document.getElementById('categorySelect').value = 'all';
      document.getElementById('confidenceSelect').value = 'all';
      document.getElementById('listingTypeSelect').value = 'all';
      document.getElementById('sortSelect').value = 'seen_new';
      document.getElementById('seenSelect').value = '24';
      document.getElementById('createdSelect').value = '0';
      document.getElementById('minPrice').value = '';
      document.getElementById('maxPrice').value = '';
      document.getElementById('minRoi').value = '';
      document.getElementById('minProfit').value = '';
      document.getElementById('minLiquidity').value = '';
      document.getElementById('maxFloat').value = '';
      document.getElementById('minStickerValue').value = '';
      offset = 0;
      refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    });

    document.getElementById('viewSelect').addEventListener('change', applyViewMode);
    document.getElementById('themeSelect').addEventListener('change', applyTheme);

    document.getElementById('sortSelect').addEventListener('change', () => {
      offset = 0;
      refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    });

    document.getElementById('seenSelect').addEventListener('change', () => {
      offset = 0;
      refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    });

    document.getElementById('createdSelect').addEventListener('change', () => {
      offset = 0;
      refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    });

    document.getElementById('minPrice').addEventListener('change', () => {
      offset = 0;
      refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    });

    document.getElementById('maxPrice').addEventListener('change', () => {
      offset = 0;
      refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    });

    ['minRoi', 'minProfit', 'minLiquidity', 'maxFloat', 'minStickerValue'].forEach(id => {
      document.getElementById(id).addEventListener('change', () => {
        offset = 0;
        refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
      });
      document.getElementById(id).addEventListener('keydown', event => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        offset = 0;
        refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
      });
    });

    document.addEventListener('click', event => {
      const target = event.target.closest('.zoomable');
      if (!target) return;
      openImageModal(target.dataset.fullSrc || target.currentSrc || target.src, target.dataset.title || target.title || '', target.dataset.fallbackSrc || '');
    });

    document.getElementById('imageModalClose').addEventListener('click', closeImageModal);
    document.getElementById('imageZoomIn').addEventListener('click', () => changeImageZoom(1.35));
    document.getElementById('imageZoomOut').addEventListener('click', () => changeImageZoom(1 / 1.35));
    document.getElementById('imageZoomReset').addEventListener('click', resetImageZoom);
    document.getElementById('imageZoomFit').addEventListener('click', fitImageZoom);
    document.getElementById('imageModalImg').addEventListener('load', fitImageZoom);
    document.getElementById('imageModal').addEventListener('click', event => {
      if (event.target.id === 'imageModal') closeImageModal();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') closeImageModal();
    });
    document.querySelector('.image-modal-body').addEventListener('wheel', event => {
      event.preventDefault();
      changeImageZoom(event.deltaY < 0 ? 1.18 : 1 / 1.18, event);
    }, { passive: false });
    document.querySelector('.image-modal-body').addEventListener('dblclick', resetImageZoom);
    document.querySelector('.image-modal-body').addEventListener('pointerdown', event => {
      if (!document.getElementById('imageModal').classList.contains('open')) return;
      zoomState.dragging = true;
      zoomState.startX = event.clientX;
      zoomState.startY = event.clientY;
      zoomState.startOffsetX = zoomState.x;
      zoomState.startOffsetY = zoomState.y;
      event.currentTarget.classList.add('dragging');
      event.currentTarget.setPointerCapture(event.pointerId);
    });
    document.querySelector('.image-modal-body').addEventListener('pointermove', event => {
      if (!zoomState.dragging) return;
      zoomState.x = zoomState.startOffsetX + event.clientX - zoomState.startX;
      zoomState.y = zoomState.startOffsetY + event.clientY - zoomState.startY;
      applyImageZoom();
    });
    document.querySelector('.image-modal-body').addEventListener('pointerup', event => {
      zoomState.dragging = false;
      event.currentTarget.classList.remove('dragging');
      try {
        event.currentTarget.releasePointerCapture(event.pointerId);
      } catch (error) {}
    });
    document.querySelector('.image-modal-body').addEventListener('pointercancel', event => {
      zoomState.dragging = false;
      event.currentTarget.classList.remove('dragging');
    });

    document.getElementById('prevButton').addEventListener('click', () => {
      offset = Math.max(0, offset - limit);
      refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    });

    document.getElementById('nextButton').addEventListener('click', () => {
      offset += limit;
      refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    });

    try {
      const savedView = localStorage.getItem('bestDealsViewMode');
      if (savedView && ['auto', 'cards', 'table'].includes(savedView)) {
        document.getElementById('viewSelect').value = savedView;
      }
    } catch (error) {}
    applyViewMode();

    try {
      const savedTheme = localStorage.getItem('siteTheme') || localStorage.getItem('bestDealsTheme');
      if (savedTheme && ['dark', 'light', 'midnight', 'graphite'].includes(savedTheme)) {
        document.getElementById('themeSelect').value = savedTheme;
      }
    } catch (error) {}
    applyTheme();

    restoreFilterState();
    refresh().catch(error => { document.getElementById('lastUpdate').textContent = `error: ${error.message}`; });
    setInterval(() => {
      if (!document.hidden) refresh().catch(() => {});
    }, 15000);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refresh().catch(() => {});
    });
  </script>
</body>
</html>
"""


STRATEGY_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CS2 Strategy Lab</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0a0f18;
      --panel: #111a27;
      --panel-2: #172335;
      --control-bg: #0d1521;
      --text: #edf5ff;
      --muted: #94a3b8;
      --line: #26364b;
      --accent: #5eead4;
      --accent-2: #60a5fa;
      --good: #2dd4bf;
      --warn: #fbbf24;
      --bad: #fb7185;
      --chart-grid: #223249;
      --chart-fan: rgba(96, 165, 250, .18);
    }
    body.theme-light {
      color-scheme: light;
      --bg: #f2f6fb;
      --panel: #ffffff;
      --panel-2: #edf3f9;
      --control-bg: #ffffff;
      --text: #142033;
      --muted: #607089;
      --line: #d4deea;
      --accent: #0f766e;
      --accent-2: #2563eb;
      --good: #0f766e;
      --warn: #b45309;
      --bad: #be123c;
      --chart-grid: #d7e1ec;
      --chart-fan: rgba(37, 99, 235, .13);
    }
    body.theme-midnight {
      --bg: #050912;
      --panel: #0d1522;
      --panel-2: #121f31;
      --control-bg: #080f1a;
      --line: #21344d;
      --accent: #67e8f9;
      --accent-2: #818cf8;
    }
    body.theme-graphite {
      --bg: #111312;
      --panel: #1b1e1c;
      --panel-2: #242824;
      --control-bg: #151816;
      --line: #343b36;
      --accent: #bef264;
      --accent-2: #facc15;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at 12% -10%, color-mix(in srgb, var(--accent-2) 17%, transparent), transparent 31rem),
        var(--bg);
      color: var(--text);
      font: 14px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 20;
      border-bottom: 1px solid color-mix(in srgb, var(--line) 86%, transparent);
      background: color-mix(in srgb, var(--bg) 90%, transparent);
      backdrop-filter: blur(16px);
    }
    .wrap { width: min(1420px, calc(100vw - 32px)); margin: 0 auto; }
    .topbar { min-height: 68px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    .top-actions { display: flex; align-items: center; gap: 9px; }
    main { padding: 22px 0 48px; }
    .strategy-hero {
      position: relative;
      overflow: hidden;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 28px;
      align-items: end;
      min-height: 230px;
      padding: 36px;
      border: 1px solid var(--line);
      border-radius: 24px;
      background:
        linear-gradient(130deg, color-mix(in srgb, var(--panel) 96%, var(--accent) 4%), var(--panel)),
        var(--panel);
      box-shadow: 0 28px 80px rgba(0, 0, 0, .24);
    }
    .strategy-hero::after {
      content: "";
      position: absolute;
      right: -80px;
      top: -100px;
      width: 360px;
      height: 360px;
      border-radius: 50%;
      background: radial-gradient(circle, color-mix(in srgb, var(--accent) 22%, transparent), transparent 68%);
      pointer-events: none;
    }
    .strategy-hero > * { position: relative; z-index: 1; min-width: 0; }
    .eyebrow { color: var(--accent); font-size: 11px; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; }
    h1 { margin: 9px 0 12px; max-width: 820px; font-size: clamp(34px, 5vw, 62px); line-height: 1.02; letter-spacing: -.045em; overflow-wrap: anywhere; }
    h2 { margin: 4px 0 0; font-size: 19px; letter-spacing: -.015em; }
    h3 { margin: 0; font-size: 15px; }
    p { margin: 0; }
    .hero-copy { max-width: 760px; color: var(--muted); font-size: 16px; }
    .hero-status { display: grid; gap: 8px; min-width: 220px; max-width: 100%; }
    .status-chip {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: color-mix(in srgb, var(--control-bg) 82%, transparent);
      color: var(--muted);
    }
    .status-chip strong { color: var(--text); font-size: 12px; }
    .status-chip > * { min-width: 0; overflow-wrap: anywhere; }
    .status-chip strong { text-align: right; }
    .notice {
      margin-top: 14px;
      min-height: 0;
      border: 1px solid transparent;
      border-radius: 12px;
      color: var(--muted);
    }
    .notice:not(:empty) { padding: 10px 13px; border-color: var(--line); background: var(--panel); }
    .notice.error { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 48%, var(--line)); }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 16px;
    }
    .metric-card {
      min-width: 0;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: color-mix(in srgb, var(--panel) 96%, var(--accent-2) 4%);
    }
    .metric-card span { display: block; color: var(--muted); font-size: 12px; }
    .metric-card strong { display: block; margin-top: 5px; font-size: clamp(22px, 3vw, 32px); line-height: 1.1; letter-spacing: -.035em; }
    .metric-card small { display: block; margin-top: 6px; color: var(--muted); }
    .strategy-layout { display: grid; gap: 16px; margin-top: 16px; }
    .panel {
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--panel);
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 17px 19px;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--panel-2) 74%, var(--panel));
    }
    .section-copy { color: var(--muted); font-size: 12px; margin-top: 3px; }
    .section-actions, .form-row { display: flex; align-items: end; gap: 9px; flex-wrap: wrap; }
    .field { display: grid; gap: 5px; color: var(--muted); font-size: 11px; font-weight: 700; }
    input, select {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--control-bg);
      color: var(--text);
      padding: 8px 10px;
      font: inherit;
      outline: none;
    }
    input:focus, select:focus, button:focus-visible, a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    .wide-input { width: min(390px, 48vw); }
    .button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--control-bg);
      color: var(--text);
      padding: 0 13px;
      font: inherit;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
    }
    .button:hover { border-color: var(--accent); }
    .button-primary { border-color: color-mix(in srgb, var(--accent) 58%, var(--line)); background: color-mix(in srgb, var(--accent) 17%, var(--control-bg)); }
    .button:disabled { opacity: .48; cursor: wait; }
    .trend-workspace { display: grid; grid-template-columns: 290px minmax(0, 1fr); min-height: 430px; }
    .trend-list { padding: 10px; border-right: 1px solid var(--line); max-height: 610px; overflow-y: auto; }
    .trend-list-empty, .empty { padding: 22px; color: var(--muted); text-align: center; }
    .trend-choice {
      width: 100%;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 5px 10px;
      padding: 11px;
      border: 1px solid transparent;
      border-radius: 10px;
      background: transparent;
      color: var(--text);
      text-align: left;
      cursor: pointer;
    }
    .trend-choice:hover, .trend-choice.active { border-color: var(--line); background: var(--panel-2); }
    .trend-choice strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .trend-choice span { color: var(--good); font-weight: 750; }
    .trend-choice small { grid-column: 1 / -1; color: var(--muted); }
    .trend-detail { padding: 20px; min-width: 0; }
    .trend-title { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 12px; }
    .trend-title strong { font-size: 21px; }
    .chart-shell { overflow: hidden; border: 1px solid var(--line); border-radius: 14px; background: color-mix(in srgb, var(--control-bg) 82%, transparent); }
    .fan-chart { display: block; width: 100%; height: auto; min-height: 250px; }
    .forecast-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; margin-top: 11px; }
    .forecast-card { padding: 12px; border: 1px solid var(--line); border-radius: 11px; background: var(--control-bg); }
    .forecast-card span, .forecast-card small { display: block; color: var(--muted); font-size: 11px; }
    .forecast-card strong { display: block; margin: 4px 0; font-size: 18px; }
    .good-text { color: var(--good) !important; }
    .bad-text { color: var(--bad) !important; }
    .two-column { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(360px, .85fr); gap: 16px; }
    .panel-body { padding: 18px; }
    .deal-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .risk-card { position: relative; padding: 14px; border: 1px solid var(--line); border-radius: 13px; background: var(--control-bg); min-width: 0; }
    .risk-card-head { display: flex; justify-content: space-between; gap: 10px; }
    .risk-card h3 { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .risk-badge { flex: 0 0 auto; display: inline-flex; align-items: center; min-height: 25px; border: 1px solid var(--line); border-radius: 999px; padding: 0 8px; font-size: 11px; font-weight: 800; }
    .risk-low { color: var(--good); border-color: color-mix(in srgb, var(--good) 46%, var(--line)); }
    .risk-medium { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 46%, var(--line)); }
    .risk-high { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 46%, var(--line)); }
    .risk-stats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; margin-top: 12px; }
    .risk-stat { padding: 8px; border-radius: 8px; background: var(--panel-2); }
    .risk-stat span { display: block; color: var(--muted); font-size: 10px; }
    .risk-stat strong { display: block; margin-top: 2px; }
    .weight-row { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 10px; }
    .weight-chip { padding: 3px 7px; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); font-size: 10px; }
    .rank-line { display: flex; justify-content: space-between; gap: 10px; margin-top: 10px; padding-top: 9px; border-top: 1px solid var(--line); color: var(--muted); font-size: 11px; }
    .rank-line a { color: var(--accent-2); font-weight: 750; text-decoration: none; }
    .control-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .control-grid .field input, .control-grid .field select { width: 100%; }
    .form-submit { width: 100%; margin-top: 11px; }
    .result-shell { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 14px; }
    .portfolio-total { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; }
    .portfolio-total div { padding: 9px; border-radius: 9px; background: var(--panel-2); }
    .portfolio-total span { display: block; color: var(--muted); font-size: 10px; }
    .portfolio-items { display: grid; gap: 6px; margin-top: 9px; }
    .portfolio-item { display: flex; justify-content: space-between; gap: 10px; padding: 9px; border: 1px solid var(--line); border-radius: 9px; }
    .outcome-table { width: 100%; border-collapse: collapse; }
    .outcome-table th, .outcome-table td { padding: 9px 7px; border-bottom: 1px solid var(--line); text-align: left; }
    .outcome-table th { color: var(--muted); font-size: 11px; }
    .backtest-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 13px; }
    .backtest-metric { padding: 10px; border: 1px solid var(--line); border-radius: 9px; }
    .backtest-metric span { display: block; color: var(--muted); font-size: 10px; }
    .backtest-metric strong { display: block; margin-top: 3px; font-size: 16px; }
    .footnote { margin-top: 10px; color: var(--muted); font-size: 11px; }
    .lab-tabs {
      display: flex;
      gap: 8px;
      margin-top: 16px;
      padding: 6px;
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--panel);
    }
    .lab-tab {
      flex: 1 0 auto;
      min-height: 42px;
      border: 1px solid transparent;
      border-radius: 9px;
      background: transparent;
      color: var(--muted);
      padding: 0 15px;
      font: inherit;
      font-weight: 780;
      cursor: pointer;
    }
    .lab-tab:hover { color: var(--text); background: var(--panel-2); }
    .lab-tab.active { color: var(--text); border-color: var(--accent); background: color-mix(in srgb, var(--accent) 13%, var(--control-bg)); }
    [data-lab-panel][hidden] { display: none !important; }
    .market-toolbar { display: grid; grid-template-columns: minmax(210px, 1.5fr) repeat(4, minmax(120px, .7fr)) auto; gap: 9px; align-items: end; }
    .market-toolbar .field input, .market-toolbar .field select { width: 100%; }
    .market-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; margin-bottom: 14px; }
    .market-summary.collection-summary { grid-template-columns: repeat(5, minmax(0, 1fr)); }
    .market-summary > div { padding: 11px; border-radius: 10px; background: var(--panel-2); }
    .market-summary span { display: block; color: var(--muted); font-size: 10px; }
    .market-summary strong { display: block; margin-top: 3px; font-size: 18px; }
    .market-table-shell { overflow-x: auto; border: 1px solid var(--line); border-radius: 12px; }
    .market-table { width: 100%; border-collapse: collapse; min-width: 920px; }
    .market-table th, .market-table td { padding: 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    .market-table th { color: var(--muted); background: var(--control-bg); font-size: 10px; text-transform: uppercase; letter-spacing: .05em; }
    .market-table tbody tr:hover { background: color-mix(in srgb, var(--panel-2) 72%, transparent); }
    .market-name { max-width: 340px; font-weight: 760; }
    .market-name small { display: block; color: var(--muted); font-weight: 500; margin-top: 3px; }
    .live-strip { margin-top: 16px; }
    .opportunity-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .opportunity-card { padding: 14px; border: 1px solid var(--line); border-radius: 12px; background: var(--control-bg); min-width: 0; }
    .opportunity-card h3 { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .opportunity-kind { display: inline-flex; margin-top: 8px; padding: 3px 7px; border: 1px solid var(--line); border-radius: 999px; color: var(--accent); font-size: 10px; font-weight: 800; }
    .opportunity-reason { min-height: 38px; margin-top: 8px; color: var(--muted); font-size: 11px; }
    .collection-workspace { display: grid; grid-template-columns: 310px minmax(0, 1fr); min-height: 620px; }
    .collection-list { max-height: 760px; overflow-y: auto; padding: 9px; border-right: 1px solid var(--line); }
    .collection-choice { width: 100%; display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 4px 8px; padding: 10px; border: 1px solid transparent; border-radius: 9px; background: transparent; color: var(--text); text-align: left; cursor: pointer; }
    .collection-choice:hover, .collection-choice.active { border-color: var(--line); background: var(--panel-2); }
    .collection-choice strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .collection-choice small { grid-column: 1 / -1; color: var(--muted); }
    .collection-detail { min-width: 0; padding: 18px; }
    .chart-grid-two { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 11px; }
    .mini-chart-shell { overflow: hidden; min-width: 0; border: 1px solid var(--line); border-radius: 11px; background: var(--control-bg); padding: 9px; }
    .mini-chart-shell h3 { margin-bottom: 6px; }
    .market-chart { display: block; width: 100%; min-height: 210px; }
    .member-grid { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(300px, .85fr); gap: 10px; margin-top: 11px; }
    .member-list { max-height: 420px; overflow: auto; border: 1px solid var(--line); border-radius: 11px; }
    .member-button { width: 100%; display: flex; justify-content: space-between; gap: 10px; padding: 9px 10px; border: 0; border-bottom: 1px solid var(--line); background: transparent; color: var(--text); text-align: left; cursor: pointer; }
    .member-button:hover, .member-button.active { background: var(--panel-2); }
    .advisor-layout { display: grid; grid-template-columns: minmax(300px, .65fr) minmax(0, 1.35fr); gap: 16px; }
    .advisor-layout .control-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .advisor-explain { display: grid; gap: 8px; margin-top: 12px; }
    .advisor-explain div { padding: 9px; border: 1px solid var(--line); border-radius: 9px; color: var(--muted); font-size: 11px; }
    .advisor-items { display: grid; gap: 8px; }
    .advisor-item { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 5px 12px; padding: 12px; border: 1px solid var(--line); border-radius: 10px; background: var(--control-bg); }
    .advisor-item p { grid-column: 1 / -1; color: var(--muted); font-size: 11px; }
    .data-warning { padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--warn) 45%, var(--line)); border-radius: 10px; color: var(--muted); background: color-mix(in srgb, var(--warn) 7%, var(--control-bg)); font-size: 11px; }
    .skeleton { min-height: 84px; background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--line) 45%, transparent), transparent); background-size: 220% 100%; animation: shimmer 1.4s infinite; }
    @keyframes shimmer { from { background-position: 120% 0; } to { background-position: -120% 0; } }
    @media (max-width: 1050px) {
      .deal-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .opportunity-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .two-column { grid-template-columns: 1fr; }
      .market-toolbar { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .advisor-layout { grid-template-columns: 1fr; }
      .market-summary.collection-summary { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    @media (max-width: 780px) {
      .wrap { width: min(100vw - 18px, 1420px); }
      .topbar, .section-head { align-items: stretch; flex-direction: column; }
      .strategy-hero { grid-template-columns: 1fr; padding: 25px 20px; }
      .hero-status { min-width: 0; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .trend-workspace { grid-template-columns: 1fr; }
      .trend-list { border-right: 0; border-bottom: 1px solid var(--line); max-height: 230px; }
      .wide-input { width: 100%; }
      .section-actions, .form-row { width: 100%; }
      .field { flex: 1 1 150px; }
      .deal-grid { grid-template-columns: 1fr; }
      .opportunity-grid { grid-template-columns: 1fr; }
      .forecast-grid { grid-template-columns: 1fr; }
      .control-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .collection-workspace { grid-template-columns: 1fr; }
      .collection-list { border-right: 0; border-bottom: 1px solid var(--line); max-height: 260px; }
      .chart-grid-two, .member-grid { grid-template-columns: 1fr; }
      .market-toolbar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      .metrics, .control-grid, .portfolio-total, .backtest-metrics, .market-summary, .market-toolbar { grid-template-columns: 1fr; }
      .market-summary.collection-summary { grid-template-columns: 1fr; }
      .advisor-layout .control-grid { grid-template-columns: 1fr; }
      .strategy-hero h1 { font-size: 35px; }
      .trend-detail, .panel-body { padding: 14px; }
    }
    @media (prefers-reduced-motion: reduce) { .skeleton { animation: none; } }
  </style>
</head>
<body class="page-strategy">
  <header>
    <div class="wrap topbar">
      <a class="site-brand" href="/" aria-label="CS2 Intelligence home">
        <span class="brand-mark" aria-hidden="true">C2</span>
        <span><strong>CS2 Intelligence</strong><small id="strategyUpdated">центр стратегий</small></span>
      </a>
      <nav class="site-nav" aria-label="Main navigation">
        <a class="nav-link" data-nav-path="/" href="/?token=__DASHBOARD_TOKEN__">Overview</a>
        <a class="nav-link" data-nav-path="/deals" href="/deals?token=__DASHBOARD_TOKEN__">Deals</a>
        <a class="nav-link" data-nav-path="/strategy" href="/strategy?token=__DASHBOARD_TOKEN__">Strategy</a>
        <a class="nav-link" data-nav-path="/deal" href="/deal?token=__DASHBOARD_TOKEN__">Сравнение</a>
        <a class="nav-link" data-nav-path="/account" href="/account">Инвентарь</a>
      </nav>
      <div class="top-actions header-tools">
        <label class="field"><span>Тема</span><select id="themeSelect" aria-label="Тема">
          <option value="dark">Тёмная</option><option value="light">Светлая</option><option value="midnight">Полночь</option><option value="graphite">Графит</option>
        </select></label>
      </div>
    </div>
  </header>

  <main class="wrap">
    <section class="strategy-hero" aria-labelledby="strategyTitle">
      <div>
        <div class="eyebrow">STRATEGY LAB</div>
        <h1 id="strategyTitle">От выгодного лота к взвешенному решению.</h1>
        <p class="hero-copy">Прогноз Trend, вероятность продажи, ранжирование по риску, реальные исходы и лимиты портфеля в одном месте.</p>
      </div>
      <div class="hero-status" aria-label="Strategy readiness">
        <div class="status-chip"><span>Trend-процесс</span><strong id="trendWorkerState">проверка</strong></div>
        <div class="status-chip"><span>Свежесть данных</span><strong id="trendFreshness">проверка</strong></div>
        <div class="status-chip"><span>Дата прогноза</span><strong id="trendAsOf">—</strong></div>
        <div class="status-chip"><span>История исходов</span><strong id="outcomeReadiness">проверка</strong></div>
      </div>
    </section>
    <div class="notice" id="strategyNotice" role="status" aria-live="polite"></div>

    <nav class="lab-tabs" role="tablist" aria-label="Разделы Strategy Lab">
      <button class="lab-tab active" type="button" role="tab" aria-selected="true" data-lab-tab="overview">Обзор и риск</button>
      <button class="lab-tab" type="button" role="tab" aria-selected="false" data-lab-tab="liquid">Ликвидные до $10</button>
      <button class="lab-tab" type="button" role="tab" aria-selected="false" data-lab-tab="collections">Все коллекции</button>
      <button class="lab-tab" type="button" role="tab" aria-selected="false" data-lab-tab="advisor">Подбор по банку</button>
    </nav>

    <section class="metrics" aria-label="Strategy overview" data-lab-panel="overview">
      <article class="metric-card"><span>Закрытые рекомендации</span><strong id="closedMetric">—</strong><small id="closedMeta">загрузка истории</small></article>
      <article class="metric-card"><span>Продажа за 30 дней</span><strong id="sellMetric">—</strong><small>по подтверждённым исходам</small></article>
      <article class="metric-card"><span>Медиана времени продажи</span><strong id="daysMetric">—</strong><small>только проданные лоты</small></article>
      <article class="metric-card"><span>Реализованная прибыль</span><strong id="profitMetric">—</strong><small>после комиссии площадки</small></article>
    </section>

    <div class="strategy-layout" data-lab-panel="overview">
      <section class="panel" aria-labelledby="trendTitle">
        <div class="section-head">
          <div><div class="eyebrow">НАПРАВЛЕНИЕ РЫНКА</div><h2 id="trendTitle">Trend q15 / q50 / q85</h2><p class="section-copy">Готовые диапазоны на 7, 14 и 30 дней. На этой странице модель не обучается.</p></div>
          <div class="section-actions">
            <label class="field"><span>Название скина</span><input class="wide-input" id="trendSearch" placeholder="Точное market hash name" autocomplete="off"></label>
            <label class="field"><span>Ранжировать по</span><select id="trendHorizon"><option value="7">7 дням</option><option value="14">14 дням</option><option value="30">30 дням</option></select></label>
            <button class="button button-primary" id="trendSearchButton" type="button">Показать прогноз</button>
          </div>
        </div>
        <div class="trend-workspace">
          <div class="trend-list" id="trendLeaderboard" aria-label="Trend leaderboard"><div class="skeleton"></div></div>
          <div class="trend-detail" id="trendDetail"><div class="empty">Выбери скин, чтобы увидеть диапазон прогноза.</div></div>
        </div>
      </section>

      <section class="panel" aria-labelledby="riskDealsTitle">
        <div class="section-head">
          <div><div class="eyebrow">ВЫГОДА С УЧЁТОМ РИСКА</div><h2 id="riskDealsTitle">Текущие сделки по консервативной оценке</h2><p class="section-copy">Веса моделей меняются только когда накоплено достаточно проверенных исходов.</p></div>
          <div class="section-actions">
            <label class="field"><span>Свежесть</span><select id="riskAge"><option value="6">6 часов</option><option value="24" selected>24 часа</option><option value="72">3 дня</option><option value="168">7 дней</option></select></label>
            <button class="button" id="riskRefreshButton" type="button">Обновить</button>
          </div>
        </div>
        <div class="panel-body"><div class="deal-grid" id="riskDeals"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div></div>
      </section>

      <div class="two-column legacy-strategy-block">
        <section class="panel" aria-labelledby="portfolioTitle">
          <div class="section-head"><div><div class="eyebrow">ПОРТФЕЛЬ</div><h2 id="portfolioTitle">Распредели бюджет с ограничениями</h2><p class="section-copy">Детерминированное предложение из сохранённых кандидатов — это не заказ и не сделка.</p></div></div>
          <div class="panel-body">
            <form id="portfolioForm">
              <div class="control-grid">
                <label class="field"><span>Бюджет, $</span><input id="portfolioBudget" type="number" min="0.01" max="20000000" step="0.01" value="1000" required></label>
                <label class="field"><span>Профиль риска</span><select id="portfolioRisk"><option value="conservative">Консервативный</option><option value="balanced" selected>Сбалансированный</option><option value="aggressive">Агрессивный</option></select></label>
                <label class="field"><span>Максимум предметов</span><input id="portfolioMaxItems" type="number" min="1" max="50" value="10" required></label>
                <label class="field"><span>На один скин</span><input id="portfolioPerSkin" type="number" min="1" max="50" value="1" required></label>
                <label class="field"><span>На одну коллекцию</span><input id="portfolioPerCollection" type="number" min="1" max="50" value="3" required></label>
                <label class="field"><span>Возраст кандидатов</span><select id="portfolioAge"><option value="6">6 часов</option><option value="24" selected>24 часа</option><option value="72">3 дня</option><option value="168">7 дней</option></select></label>
              </div>
              <button class="button button-primary form-submit" id="portfolioButton" type="submit">Собрать портфель</button>
            </form>
            <div class="result-shell" id="portfolioResult" aria-live="polite"><div class="empty">Задай лимиты и собери безопасное предложение.</div></div>
          </div>
        </section>

        <section class="panel" aria-labelledby="backtestTitle">
          <div class="section-head"><div><div class="eyebrow">WALK-FORWARD ТЕСТ</div><h2 id="backtestTitle">Экономический backtest</h2><p class="section-copy">Загружает готовый сохранённый отчёт; во время запроса ничего не пересчитывает.</p></div></div>
          <div class="panel-body">
            <form id="backtestForm">
              <div class="control-grid">
                <label class="field"><span>Срок удержания</span><select id="backtestHolding"><option value="7" selected>7 дней</option></select></label>
                <label class="field"><span>Top K сигналов</span><input id="backtestTopK" type="number" min="1" max="500" value="20" required></label>
                <label class="field"><span>Начальный капитал, $</span><input id="backtestCapital" type="number" min="0.01" max="20000000" step="0.01" value="1000" required></label>
              </div>
              <button class="button form-submit" id="backtestButton" type="submit">Показать готовый отчёт</button>
            </form>
            <div class="result-shell" id="backtestResult" aria-live="polite"><div class="empty">Загружаю последний сохранённый отчёт…</div></div>
          </div>
        </section>
      </div>

      <section class="panel legacy-strategy-block" aria-labelledby="outcomesTitle">
        <div class="section-head"><div><div class="eyebrow">ПРОВЕРКА РЕЗУЛЬТАТОВ</div><h2 id="outcomesTitle">Чем закончились прошлые рекомендации</h2><p class="section-copy">Только подтверждённые продажи и закрытые 30-дневные окна; открытые рекомендации в метрики не входят.</p></div></div>
        <div class="panel-body" id="outcomeModels"><div class="skeleton"></div></div>
      </section>
    </div>

    <div class="strategy-layout" data-lab-panel="liquid" hidden>
      <section class="panel" aria-labelledby="liquidTitle">
        <div class="section-head">
          <div><div class="eyebrow">БЫСТРЫЙ ОБОРОТ</div><h2 id="liquidTitle">Ликвидные скины и свежие лоты до $10</h2><p class="section-copy">Историческая ликвидность показана отдельно от реально свежих предложений сканера.</p></div>
        </div>
        <div class="panel-body">
          <form class="market-toolbar" id="liquidForm">
            <label class="field"><span>Поиск скина</span><input id="liquidSearch" placeholder="AK-47, Glock, название" autocomplete="off"></label>
            <label class="field"><span>Порог цены, $</span><input id="liquidMaxPrice" type="number" min="0.01" max="10" step="0.01" value="10" required></label>
            <label class="field"><span>Продаж в день от</span><input id="liquidMinSales" type="number" min="0" step="0.1" value="1"></label>
            <label class="field"><span>Ликвидность от</span><input id="liquidMinScore" type="number" min="0" max="100" step="1" value="25"></label>
            <label class="field"><span>Сортировка</span><select id="liquidSort"><option value="liquidity">Ликвидность</option><option value="return_7d">Рост за 7 дней</option><option value="return_30d">Рост за 30 дней</option><option value="price">Сначала дешёвые</option><option value="supply">Больше предметов в снимке</option></select></label>
            <button class="button button-primary" id="liquidButton" type="submit">Найти</button>
          </form>
          <div class="market-summary" id="liquidSummary" style="margin-top:14px"><div><span>Подходящих скинов</span><strong>—</strong></div><div><span>Свежих лотов</span><strong>—</strong></div><div><span>Состояние сканера</span><strong>—</strong></div><div><span>Данные на дату</span><strong>—</strong></div></div>
          <div class="data-warning" id="liquidWarning">Количество — снимок листингов из ALL_SKINS и число наблюдавшихся продаж. Это не мировой инвентарь и не гарантия доходности.</div>
          <div class="live-strip"><h3>Свежие предложения: float / стикеры / недооценка</h3><p class="section-copy" id="liveDealsMeta">Загружаю только лоты, которые сканер видел недавно.</p><div class="opportunity-grid" id="liquidLiveDeals"><div class="skeleton"></div></div></div>
          <div class="live-strip"><h3>Самые ликвидные виды скинов</h3><p class="section-copy">Это цели для мониторинга, а не утверждение, что конкретный лот сейчас продаётся.</p><div id="liquidTable"><div class="skeleton"></div></div></div>
        </div>
      </section>
    </div>

    <div class="strategy-layout" data-lab-panel="collections" hidden>
      <section class="panel" aria-labelledby="collectionsTitle">
        <div class="section-head">
          <div><div class="eyebrow">КАРТА РЫНКА</div><h2 id="collectionsTitle">Все коллекции: индекс, объём и состав</h2><p class="section-copy">Индекс начинается со 100 и сравнивает нормализованное движение составляющих скинов, не смешивая их абсолютные цены.</p></div>
          <div class="section-actions">
            <label class="field"><span>Поиск коллекции</span><input class="wide-input" id="collectionSearch" placeholder="Dust, Mirage, Train"></label>
            <label class="field"><span>Период</span><select id="collectionDays"><option value="30">30 дней</option><option value="90" selected>90 дней</option><option value="180">180 дней</option><option value="365">1 год</option><option value="730">2 года</option></select></label>
            <label class="field"><span>Сортировка</span><select id="collectionSort"><option value="return_30d">Рост 30д</option><option value="sales_30d">Объём 30д</option><option value="name">Название</option></select></label>
            <button class="button button-primary" id="collectionRefreshButton" type="button">Показать</button>
          </div>
        </div>
        <div class="collection-workspace">
          <div class="collection-list" id="collectionList"><div class="skeleton"></div></div>
          <div class="collection-detail" id="collectionDetail"><div class="empty">Выбери коллекцию, чтобы увидеть её движение и отдельные скины.</div></div>
        </div>
      </section>
    </div>

    <div class="strategy-layout" data-lab-panel="advisor" hidden>
      <section class="panel" aria-labelledby="advisorTitle">
        <div class="section-head"><div><div class="eyebrow">УМНЫЙ БАНК</div><h2 id="advisorTitle">Что рассмотреть на твой бюджет</h2><p class="section-copy">Цена одного предмета автоматически ограничивается размером банка; режим меняет источник и ранжирование кандидатов.</p></div></div>
        <div class="panel-body advisor-layout">
          <div>
            <form id="advisorForm">
              <div class="control-grid">
                <label class="field"><span>Банк, $</span><input id="advisorBudget" type="number" min="0.01" max="20000000" step="0.01" value="100" required></label>
                <label class="field"><span>Цель</span><select id="advisorMode"><option value="balanced">Смешанный подбор</option><option value="investment">Инвестиция в рост</option><option value="flip_float">Заработок на float</option><option value="flip_stickers">Заработок на стикерах</option></select></label>
                <label class="field"><span>Риск</span><select id="advisorRisk"><option value="conservative">Консервативный</option><option value="balanced" selected>Сбалансированный</option><option value="aggressive">Агрессивный</option></select></label>
                <label class="field"><span>Максимум вариантов</span><input id="advisorMaxItems" type="number" min="1" max="30" value="8" required></label>
                <label class="field"><span>Свежесть активных лотов</span><select id="advisorAge"><option value="6">6 часов</option><option value="24" selected>24 часа</option><option value="72">3 дня</option></select></label>
              </div>
              <button class="button button-primary form-submit" id="advisorButton" type="submit">Подобрать варианты</button>
            </form>
            <div class="advisor-explain"><div><strong>Инвестиция</strong><br>Trend, риск снижения, вероятность роста и ликвидность; это цель рынка, а не конкретный активный лот.</div><div><strong>Float / стикеры</strong><br>Только свежие лоты сканера с подтверждаемым преимуществом по float или стоимости стикеров.</div><div><strong>Важно</strong><br>Комиссия и риск уже учитываются в моделях, но прибыль не гарантируется и покупка не совершается автоматически.</div></div>
          </div>
          <div id="advisorResult" aria-live="polite"><div class="empty">Укажи банк и цель — здесь появятся несколько объяснимых вариантов.</div></div>
        </div>
      </section>
    </div>
  </main>

  <script>
    const format = new Intl.NumberFormat('__CS2_INTL_LOCALE__');
    let trendItems = [];
    let selectedTrendName = '';

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>'"]/g, character => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
      })[character]);
    }

    function finite(value) {
      if (value === null || value === undefined || value === '') return null;
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }

    function money(cents) {
      const value = finite(cents);
      return value === null ? '—' : `$${(value / 100).toFixed(2)}`;
    }

    function percent(value, fraction = false) {
      const number = finite(value);
      if (number === null) return '—';
      return `${(fraction ? number * 100 : number).toFixed(1)}%`;
    }

    function days(value) {
      const number = finite(value);
      return number === null ? '—' : `${number.toFixed(number < 10 ? 1 : 0)}d`;
    }

    function safeExternalUrl(value) {
      if (!value) return '';
      try {
        const parsed = new URL(String(value));
        return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '';
      } catch (error) { return ''; }
    }

    async function requestJson(url, options = {}) {
      const response = await fetch(url, { cache: 'no-store', ...options });
      let payload;
      try { payload = await response.json(); } catch (error) { payload = {}; }
      if (!response.ok) throw new Error(payload.error || response.statusText || 'Request failed');
      return payload;
    }

    function setNotice(message = '', isError = false) {
      const notice = document.getElementById('strategyNotice');
      notice.textContent = message;
      notice.classList.toggle('error', isError);
    }

    function localizedReason(reason, fallback) {
      const values = {
        'trend store is not available yet': 'Хранилище Trend пока не создано.',
        'trend forecasts have not been precomputed': 'Trend-прогнозы ещё не рассчитаны.',
        'precomputed backtest is not available yet': 'Готовый backtest пока не создан.',
        'matching backtest has not been precomputed': 'Отчёт с такими параметрами пока не рассчитан.',
      };
      return values[String(reason || '')] || reason || fallback;
    }

    const loadedLabPanels = { overview: true, liquid: false, collections: false, advisor: true };

    function setLabTab(name, { updateUrl = true } = {}) {
      const allowed = ['overview', 'liquid', 'collections', 'advisor'];
      const selected = allowed.includes(name) ? name : 'overview';
      document.querySelectorAll('[data-lab-panel]').forEach(panel => {
        panel.hidden = panel.dataset.labPanel !== selected;
      });
      document.querySelectorAll('[data-lab-tab]').forEach(button => {
        const active = button.dataset.labTab === selected;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', String(active));
      });
      if (updateUrl) {
        const params = new URLSearchParams(window.location.search);
        params.delete('token');
        if (selected === 'overview') params.delete('tab');
        else params.set('tab', selected);
        const query = params.toString();
        history.replaceState(null, '', `${window.location.pathname}${query ? `?${query}` : ''}`);
      }
      if (selected === 'liquid' && !loadedLabPanels.liquid) {
        loadedLabPanels.liquid = true;
        loadLiquidMarket().catch(error => { loadedLabPanels.liquid = false; setNotice(error.message, true); });
      }
      if (selected === 'collections' && !loadedLabPanels.collections) {
        loadedLabPanels.collections = true;
        loadCollections().catch(error => { loadedLabPanels.collections = false; setNotice(error.message, true); });
      }
    }

    function externalLink(url, label) {
      const safe = safeExternalUrl(url);
      return safe ? `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>` : '';
    }

    function renderLiveOpportunities(items = []) {
      const target = document.getElementById('liquidLiveDeals');
      if (!items.length) {
        target.innerHTML = '<div class="empty">Свежих лотов под этим порогом сейчас нет. Включи расширенный Deal Scanner — исторические продажи не выдаются за активные предложения.</div>';
        return;
      }
      target.innerHTML = items.map(item => {
        const ensemble = item.ensemble_prediction || {};
        const sale = item.sale_estimate || {};
        const kind = { float: 'хороший float', stickers: 'стикеры', misprice: 'недооценка' }[item.opportunity_type] || 'возможность';
        return `<article class="opportunity-card"><div class="risk-card-head"><div><h3 title="${escapeHtml(item.skin_name || '')}">${escapeHtml(item.skin_name || 'Неизвестный скин')}</h3><p class="section-copy">покупка ${money(item.buy_price_cents)} · float ${finite(item.float_value) === null ? '—' : Number(item.float_value).toFixed(6)}</p></div><strong>${money(ensemble.conservative_profit_cents)}</strong></div><span class="opportunity-kind">${escapeHtml(kind)}</span><p class="opportunity-reason">${escapeHtml(item.reason || '')}</p><div class="rank-line"><span>продажа 30д ${percent(sale.within_30d, true)} · ликвидность ${finite(item.liquidity_score) === null ? '—' : Number(item.liquidity_score).toFixed(0)}</span>${externalLink(item.csfloat_url, 'Открыть лот')}</div></article>`;
      }).join('');
    }

    function renderLiquidMarket(payload = {}) {
      const summary = payload.summary || {};
      const scanner = payload.scanner || {};
      const scannerControl = scanner.control || {};
      const scannerRuntime = scanner.runtime || {};
      const scannerLabel = scannerControl.enabled
        ? (scannerRuntime.online ? 'работает' : 'включён, не в сети')
        : 'на паузе';
      document.getElementById('liquidSummary').innerHTML = `<div><span>Подходящих скинов</span><strong>${format.format(summary.matching_skins || 0)}</strong></div><div><span>Свежих лотов</span><strong>${format.format(summary.fresh_live_deals || 0)}</strong></div><div><span>Состояние сканера</span><strong>${escapeHtml(scannerLabel)}</strong></div><div><span>Данные на дату</span><strong>${escapeHtml(payload.as_of_day || '—')}</strong></div>`;
      const supply = payload.supply || {};
      document.getElementById('liquidWarning').textContent = `Количество показано тремя отдельными метриками: снимок ${supply.source || 'ALL_SKINS'}${supply.snapshot_at ? ` от ${new Date(supply.snapshot_at).toLocaleDateString('__CS2_INTL_LOCALE__')}` : ''}, последнее справочное количество CSFloat и наблюдавшиеся строки продаж в БД. Ни одна из них не является мировым инвентарём. Историческая ликвидность не означает, что конкретный лот активен.`;
      const live = Array.isArray(payload.live_deals) ? payload.live_deals : [];
      document.getElementById('liveDealsMeta').textContent = live.length
        ? `${format.format(live.length)} свежих предложений, каждое со ссылкой на конкретный лот.`
        : `Свежих предложений нет; сканер ${scannerLabel}.`;
      renderLiveOpportunities(live);
      const items = Array.isArray(payload.items) ? payload.items : [];
      document.getElementById('liquidTable').innerHTML = items.length ? `<div class="market-table-shell"><table class="market-table"><thead><tr><th>Скин</th><th>Цена</th><th>Продажи 7/30д</th><th>В день</th><th>Ликвидность</th><th>Движение 7/30д</th><th>Прогноз 30д</th><th>Количество (разные источники)</th><th>Ссылки</th></tr></thead><tbody>${items.map(item => {
        const forecast = item.forecast_30d || {};
        const movement7 = finite(item.return_7d_percent);
        const movement30 = finite(item.return_30d_percent);
        return `<tr><td class="market-name">${escapeHtml(item.market_hash_name)}<small>${escapeHtml(item.collection_name || item.weapon_name || '')}</small></td><td>${money(item.current_price_cents)}</td><td>${format.format(item.sales_7d || 0)} / ${format.format(item.sales_30d || 0)}</td><td>${Number(item.sales_per_day || 0).toFixed(1)}</td><td>${Number(item.liquidity_score || 0).toFixed(0)}/100</td><td class="${movement7 !== null && movement7 >= 0 ? 'good-text' : 'bad-text'}">${percent(movement7)} / ${percent(movement30)}</td><td class="${Number(forecast.expected_return_percent || 0) >= 0 ? 'good-text' : 'bad-text'}">${percent(forecast.expected_return_percent)}<small style="display:block;color:var(--muted)">рост ${percent(forecast.probability_up, true)}</small></td><td>снимок ${finite(item.quantity_on_market_estimate) === null ? '—' : `≈${format.format(item.quantity_on_market_estimate)}`}<small style="display:block;color:var(--muted)" title="${escapeHtml(item.latest_reference_at || '')}">справка CSFloat ${finite(item.latest_reference_quantity) === null ? '—' : `≈${format.format(item.latest_reference_quantity)}`} · БД ${format.format(item.observed_sales_count || item.db_sale_rows || 0)}</small></td><td>${externalLink(item.csfloat_url, 'CSFloat')} ${externalLink(`https://steamcommunity.com/market/listings/730/${encodeURIComponent(item.market_hash_name)}`, 'Steam')}</td></tr>`;
      }).join('')}</tbody></table></div>` : '<div class="empty">Нет скинов, проходящих пороги. Уменьши продажи/ликвидность или измени поиск.</div>';
    }

    async function loadLiquidMarket() {
      const button = document.getElementById('liquidButton');
      button.disabled = true;
      try {
        const params = new URLSearchParams({
          max_price: document.getElementById('liquidMaxPrice').value || '10',
          min_sales_per_day: document.getElementById('liquidMinSales').value || '0',
          min_liquidity: document.getElementById('liquidMinScore').value || '0',
          search: document.getElementById('liquidSearch').value.trim(),
          sort: document.getElementById('liquidSort').value,
          limit: '100',
          live_age_hours: '24',
        });
        renderLiquidMarket(await requestJson(`/api/market/liquid?${params}`));
      } finally { button.disabled = false; }
    }

    function renderAdvisor(payload = {}) {
      const target = document.getElementById('advisorResult');
      const items = Array.isArray(payload.items) ? payload.items : [];
      const cap = money(payload.adaptive_item_cap_cents);
      target.innerHTML = `<div class="market-summary"><div><span>Распределено</span><strong>${money(payload.spent_cents)}</strong></div><div><span>Осталось</span><strong>${money(payload.cash_remaining_cents)}</strong></div><div><span>Лимит одного предмета</span><strong>${cap}</strong></div><div><span>Вариантов</span><strong>${format.format(payload.item_count || 0)}</strong></div></div><div class="advisor-items">${items.map(item => {
        const live = item.candidate_type === 'live_listing';
        const name = item.skin_name || item.market_hash_name || 'Неизвестно';
        const url = live ? item.csfloat_url : (item.csfloat_url || item.steam_market_url);
        return `<article class="advisor-item"><div><strong>${escapeHtml(name)}</strong><small style="display:block;color:var(--muted)">${live ? 'свежий конкретный лот' : 'инвестиционная цель рынка'}</small></div><strong>${money(item.price_cents)}</strong><p>${escapeHtml(item.reason || '')} ${url ? `· ${externalLink(url, live ? 'Открыть лот' : 'Открыть рынок')}` : ''}</p></article>`;
      }).join('') || `<div class="empty">${payload.live_candidate_count ? 'Ни один кандидат не прошёл выбранный риск и лимиты.' : 'Для режима float/стикеров сейчас нет свежих лотов. Включи сканер или выбери «Инвестиция», где используются готовые рыночные прогнозы.'}</div>`}</div><p class="footnote">Найдено инвестиционных целей: ${format.format(payload.investment_candidate_count || 0)}; свежих кандидатов: ${format.format(payload.live_candidate_count || 0)}. Никаких заявок не создано.</p>`;
    }

    let collectionItems = [];
    let selectedCollectionName = '';
    let selectedCollectionMember = '';

    function chartValue(value, kind) {
      if (kind === 'money') return money(Number(value));
      if (kind === 'percent') return `${Number(value).toFixed(0)}%`;
      if (kind === 'fraction') return `${(Number(value) * 100).toFixed(0)}%`;
      return Number(value).toFixed(1);
    }

    function lineChart(rows, key, { kind = 'number', label = '', color = 'var(--accent)' } = {}) {
      const points = (Array.isArray(rows) ? rows : []).map(row => ({ day: row.day, value: finite(row[key]) })).filter(point => point.value !== null);
      if (!points.length) return '<div class="empty">Для выбранного периода данных нет.</div>';
      const width = 760, height = 240, pad = { left: 64, right: 18, top: 20, bottom: 35 };
      let minimum = Math.min(...points.map(point => point.value));
      let maximum = Math.max(...points.map(point => point.value));
      const spread = Math.max(maximum - minimum, Math.abs(maximum) * .08, 1);
      minimum -= spread * .12;
      maximum += spread * .12;
      const x = index => pad.left + (points.length === 1 ? (width - pad.left - pad.right) / 2 : index * (width - pad.left - pad.right) / (points.length - 1));
      const y = value => pad.top + (maximum - value) / (maximum - minimum) * (height - pad.top - pad.bottom);
      const grid = [0, .25, .5, .75, 1].map(step => {
        const value = maximum - (maximum - minimum) * step;
        const py = y(value);
        return `<line x1="${pad.left}" y1="${py}" x2="${width - pad.right}" y2="${py}" stroke="var(--chart-grid)"/><text x="${pad.left - 8}" y="${py + 4}" fill="var(--muted)" font-size="10" text-anchor="end">${escapeHtml(chartValue(value, kind))}</text>`;
      }).join('');
      const polyline = points.map((point, index) => `${x(index).toFixed(1)},${y(point.value).toFixed(1)}`).join(' ');
      const every = Math.max(1, Math.ceil(points.length / 5));
      const labels = points.map((point, index) => index % every === 0 || index === points.length - 1 ? `<text x="${x(index)}" y="${height - 11}" fill="var(--muted)" font-size="10" text-anchor="middle">${escapeHtml(String(point.day || '').slice(5))}</text>` : '').join('');
      return `<svg class="market-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(label)}"><title>${escapeHtml(label)}</title>${grid}<polyline points="${polyline}" fill="none" stroke="${color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>${labels}</svg>`;
    }

    function barChart(rows, key, { label = '' } = {}) {
      const values = (Array.isArray(rows) ? rows : []).map(row => ({ day: row.day, value: Math.max(0, Number(row[key] || 0)) }));
      if (!values.length) return '<div class="empty">Для выбранного периода данных нет.</div>';
      const width = 760, height = 240, pad = { left: 54, right: 14, top: 20, bottom: 35 };
      const maximum = Math.max(...values.map(point => point.value), 1);
      const step = (width - pad.left - pad.right) / values.length;
      const bars = values.map((point, index) => {
        const barHeight = point.value / maximum * (height - pad.top - pad.bottom);
        return `<rect x="${pad.left + index * step}" y="${height - pad.bottom - barHeight}" width="${Math.max(1, step * .78)}" height="${barHeight}" fill="var(--accent-2)" opacity=".72"><title>${escapeHtml(point.day)} · ${format.format(point.value)}</title></rect>`;
      }).join('');
      return `<svg class="market-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(label)}"><title>${escapeHtml(label)}</title><line x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}" stroke="var(--chart-grid)"/>${bars}<text x="${pad.left - 7}" y="${pad.top + 4}" fill="var(--muted)" font-size="10" text-anchor="end">${format.format(maximum)}</text></svg>`;
    }

    function collectionReturn(value) {
      const number = finite(value);
      const tone = number === null ? '' : (number >= 0 ? 'good-text' : 'bad-text');
      return `<span class="${tone}">${percent(number)}</span>`;
    }

    function renderCollectionList(payload = {}) {
      collectionItems = Array.isArray(payload.items) ? payload.items : [];
      const list = document.getElementById('collectionList');
      if (!collectionItems.length) {
        list.innerHTML = `<div class="empty">${escapeHtml(localizedReason(payload.reason, 'Коллекции не найдены. После первого обновления Trend здесь появится материализованный индекс.'))}</div>`;
        document.getElementById('collectionDetail').innerHTML = '<div class="empty">Нет готовых данных коллекций.</div>';
        return;
      }
      list.innerHTML = collectionItems.map(item => `<button class="collection-choice${item.collection_name === selectedCollectionName ? ' active' : ''}" type="button" data-collection-name="${escapeHtml(item.collection_name)}"><strong>${escapeHtml(item.collection_name)}</strong>${collectionReturn(item.return_30d)}<small>${format.format(item.member_count || 0)} видов · ${format.format(item.sales_30d || 0)} продаж/30д · ширина ${percent(item.breadth_up)} · покрытие ${percent(item.member_coverage, true)}</small></button>`).join('');
      list.querySelectorAll('[data-collection-name]').forEach(button => button.addEventListener('click', () => loadCollectionDetail(button.dataset.collectionName).catch(error => setNotice(error.message, true))));
      const preferred = collectionItems.find(item => item.collection_name === selectedCollectionName) || collectionItems[0];
      loadCollectionDetail(preferred.collection_name).catch(error => setNotice(error.message, true));
    }

    async function loadCollections() {
      const button = document.getElementById('collectionRefreshButton');
      button.disabled = true;
      try {
        const params = new URLSearchParams({
          search: document.getElementById('collectionSearch').value.trim(),
          sort: document.getElementById('collectionSort').value,
          limit: '200',
          include_unknown: '1',
        });
        renderCollectionList(await requestJson(`/api/market/collections?${params}`));
      } finally { button.disabled = false; }
    }

    function renderCollectionDetail(payload = {}) {
      const target = document.getElementById('collectionDetail');
      if (payload.status !== 'ready') {
        target.innerHTML = `<div class="empty">${escapeHtml(payload.reason || 'Коллекция не найдена.')}</div>`;
        return;
      }
      const summary = payload.summary || {};
      const series = Array.isArray(payload.series) ? payload.series : [];
      const members = Array.isArray(payload.members) ? payload.members : [];
      const supply = payload.supply || {};
      const minimumCoverage = finite(summary.minimum_member_coverage) ?? 0.20;
      const coveredIndexSeries = series.filter(row =>
        (finite(row.member_coverage) ?? 0) >= minimumCoverage
      );
      const directionLabel = {
        up: 'вверх', down: 'вниз', flat: 'без явного движения',
        insufficient_coverage: 'недостаточно покрытия',
      }[summary.direction] || 'без явного движения';
      const currentIndexLabel = summary.direction === 'insufficient_coverage'
        ? '—'
        : Number(summary.current_index || 0).toFixed(1);
      target.innerHTML = `<div class="trend-title"><div><strong>${escapeHtml(payload.collection_name)}</strong><p class="section-copy">индекс ${currentIndexLabel} · направление ${escapeHtml(directionLabel)} · ${format.format(summary.member_count || 0)} вариантов</p></div><span class="risk-badge ${summary.direction === 'up' ? 'risk-low' : summary.direction === 'down' ? 'risk-high' : 'risk-medium'}">30д ${percent(summary.return_30d)}</span></div><div class="market-summary collection-summary"><div><span>Наблюдалось продаж</span><strong>${format.format(summary.observed_sales_count || 0)}</strong></div><div><span>Уникальных asset ID</span><strong>${format.format(summary.unique_asset_count || 0)}</strong></div><div><span>Листингов в снимке</span><strong>${summary.quantity_snapshot_member_count ? `≈${format.format(summary.quantity_on_market_estimate || 0)}` : '—'}</strong></div><div><span>Справка CSFloat</span><strong>${summary.quantity_member_count ? `≈${format.format(summary.latest_reference_quantity || 0)}` : '—'}</strong></div><div><span>Растущих составляющих</span><strong>${percent(summary.breadth_up)}</strong></div></div><div class="data-warning">Снимок количества: ${escapeHtml(supply.source || 'нет')}${supply.snapshot_at ? ` от ${escapeHtml(new Date(supply.snapshot_at).toLocaleDateString('__CS2_INTL_LOCALE__'))}` : ''}. Справка CSFloat: ${format.format(summary.quantity_member_count || 0)}/${format.format(summary.member_count || 0)} видов${summary.latest_reference_at ? `, последнее от ${escapeHtml(new Date(summary.latest_reference_at).toLocaleString('__CS2_INTL_LOCALE__'))}` : ''}. Это разные приблизительные метрики, не число предметов в мире. Индекс и доходность показываются только при покрытии ≥${percent(minimumCoverage, true)}; текущее покрытие ${percent(summary.member_coverage, true)}.</div><div class="chart-grid-two"><div class="mini-chart-shell"><h3>Индекс коллекции (база 100)</h3><p class="section-copy">Только дни с покрытием ≥${percent(minimumCoverage, true)}.</p>${lineChart(coveredIndexSeries, 'index_value', { label: `Индекс ${payload.collection_name}` })}</div><div class="mini-chart-shell"><h3>Объём продаж</h3>${barChart(series, 'sales_count', { label: `Объём ${payload.collection_name}` })}</div><div class="mini-chart-shell"><h3>Ширина роста</h3>${lineChart(series, 'breadth_up', { kind: 'percent', label: `Доля растущих скинов ${payload.collection_name}`, color: 'var(--good)' })}</div><div class="mini-chart-shell"><h3>Охват состава</h3>${lineChart(series, 'member_coverage', { kind: 'fraction', label: `Охват состава ${payload.collection_name}`, color: 'var(--warn)' })}</div></div><div class="member-grid"><div><h3>Скины коллекции</h3><p class="section-copy">Нажми на вид, чтобы открыть его отдельный график.</p><div class="member-list" id="collectionMembers">${members.map(member => `<button class="member-button" type="button" data-member-name="${escapeHtml(member.market_hash_name)}"><span>${escapeHtml(member.market_hash_name)}<small style="display:block;color:var(--muted)">${format.format(member.sales_30d || 0)} продаж/30д · БД ${format.format(member.observed_sales_count || 0)} · снимок ${finite(member.quantity_on_market_estimate) === null ? '—' : `≈${format.format(member.quantity_on_market_estimate)}`} · справка CSFloat ${finite(member.latest_reference_quantity) === null ? '—' : `≈${format.format(member.latest_reference_quantity)}`}</small></span><span>${money(member.current_price_cents || member.current_median_price_cents)}<small style="display:block">${percent(member.return_30d)}</small></span></button>`).join('') || '<div class="empty">Состав пока не материализован.</div>'}</div></div><div class="mini-chart-shell" id="skinSeriesPanel"><h3>Отдельный скин</h3><div class="empty">Выбери скин слева.</div></div></div>`;
      document.querySelectorAll('.collection-choice').forEach(button => button.classList.toggle('active', button.dataset.collectionName === payload.collection_name));
      const memberButtons = target.querySelectorAll('[data-member-name]');
      memberButtons.forEach(button => button.addEventListener('click', () => loadSkinSeries(button.dataset.memberName).catch(error => setNotice(error.message, true))));
      if (members.length) {
        const preferred = members.find(member => member.market_hash_name === selectedCollectionMember) || members[0];
        loadSkinSeries(preferred.market_hash_name).catch(error => setNotice(error.message, true));
      }
    }

    async function loadCollectionDetail(name) {
      selectedCollectionName = String(name || '');
      selectedCollectionMember = '';
      document.querySelectorAll('.collection-choice').forEach(button => button.classList.toggle('active', button.dataset.collectionName === selectedCollectionName));
      document.getElementById('collectionDetail').innerHTML = '<div class="skeleton"></div>';
      const params = new URLSearchParams({ name: selectedCollectionName, days: document.getElementById('collectionDays').value });
      renderCollectionDetail(await requestJson(`/api/market/collection?${params}`));
    }

    async function loadSkinSeries(name) {
      selectedCollectionMember = String(name || '');
      document.querySelectorAll('[data-member-name]').forEach(button => button.classList.toggle('active', button.dataset.memberName === selectedCollectionMember));
      const panel = document.getElementById('skinSeriesPanel');
      if (!panel) return;
      panel.innerHTML = '<div class="skeleton"></div>';
      const params = new URLSearchParams({ name: selectedCollectionMember, days: document.getElementById('collectionDays').value });
      const payload = await requestJson(`/api/market/skin-series?${params}`);
      const catalog = payload.catalog || {};
      const series = Array.isArray(payload.series) ? payload.series : [];
      panel.innerHTML = `<h3>${escapeHtml(payload.market_hash_name || selectedCollectionMember)}</h3><p class="section-copy">БД ${format.format(catalog.observed_sales_count || 0)} · asset ID ${format.format(catalog.unique_asset_count || 0)} · float ${finite(catalog.min_float) === null ? '—' : `${Number(catalog.min_float).toFixed(5)}…${Number(catalog.max_float).toFixed(5)}`} · снимок ${finite(catalog.quantity_on_market_estimate) === null ? '—' : `≈${format.format(catalog.quantity_on_market_estimate)}`} · справка CSFloat ${finite(catalog.latest_reference_quantity) === null ? '—' : `≈${format.format(catalog.latest_reference_quantity)}`}${catalog.latest_reference_at ? ` (${escapeHtml(new Date(catalog.latest_reference_at).toLocaleDateString('__CS2_INTL_LOCALE__'))})` : ''}</p>${lineChart(series, 'median_price_cents', { kind: 'money', label: `Цена ${payload.market_hash_name || selectedCollectionMember}`, color: 'var(--accent-2)' })}<div class="rank-line"><span>${format.format(series.reduce((sum, row) => sum + Number(row.sales_count || 0), 0))} продаж на графике · количества не являются мировым предложением</span><span>${externalLink(catalog.csfloat_url, 'CSFloat')} ${externalLink(catalog.steam_market_url, 'Steam')}</span></div>`;
    }

    function renderOutcomes(outcomes = {}) {
      document.getElementById('closedMetric').textContent = format.format(outcomes.closed_recommendations || 0);
      document.getElementById('sellMetric').textContent = percent(outcomes.sell_through_rate, true);
      document.getElementById('daysMetric').textContent = days(outcomes.median_days_to_sell);
      document.getElementById('profitMetric').textContent = money(outcomes.realized_profit_cents || 0);
      document.getElementById('closedMeta').textContent = `${format.format(outcomes.unique_listings || 0)} уникальных лотов`;
      document.getElementById('outcomeReadiness').textContent = outcomes.history_ready ? 'данных достаточно' : 'история копится';
      const models = outcomes.models || {};
      const rows = Object.entries(models).map(([name, value]) => `<tr>
        <td><strong>${escapeHtml(name)}</strong></td>
        <td>${format.format(value.closed_recommendations || 0)}</td>
        <td>${percent(value.sell_through_rate, true)}</td>
        <td>${days(value.median_days_to_sell)}</td>
        <td>${percent(value.median_absolute_percentage_error)}</td>
        <td class="${Number(value.realized_profit_cents || 0) >= 0 ? 'good-text' : 'bad-text'}">${money(value.realized_profit_cents)}</td>
      </tr>`).join('');
      document.getElementById('outcomeModels').innerHTML = rows
        ? `<div style="overflow-x:auto"><table class="outcome-table"><thead><tr><th>Модель</th><th>Закрыто</th><th>Продано</th><th>Медиана срока</th><th>Медиана ошибки</th><th>Прибыль</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : '<div class="empty">Исходы появятся, когда пройдёт достаточно времени или будут найдены подтверждённые продажи.</div>';
    }

    function metric(label, value, tone = '') {
      return `<div class="backtest-metric"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></div>`;
    }

    function renderBacktest(report = {}) {
      const target = document.getElementById('backtestResult');
      if (report.status !== 'ready') {
        target.innerHTML = `<div class="empty">${escapeHtml(localizedReason(report.reason, 'Подходящий готовый отчёт пока не создан.'))}</div>`;
        return;
      }
      const value = report.metrics || {};
      target.innerHTML = `<div class="backtest-metrics">
        ${metric('Чистая доходность', percent(value.net_return_percent), Number(value.net_return_percent || 0) >= 0 ? 'good-text' : 'bad-text')}
        ${metric('Чистая прибыль', money(value.net_profit_cents), Number(value.net_profit_cents || 0) >= 0 ? 'good-text' : 'bad-text')}
        ${metric('Точность @ K', percent(value.precision_at_k, true))}
        ${metric('Завершённые сделки', format.format(value.completed_trades || 0))}
        ${metric('Макс. просадка', percent(value.max_drawdown_percent), 'bad-text')}
        ${metric('Sharpe', finite(value.sharpe_annualized) === null ? '—' : Number(value.sharpe_annualized).toFixed(2))}
        ${metric('Sortino', finite(value.sortino_annualized) === null ? '—' : Number(value.sortino_annualized).toFixed(2))}
        ${metric('Комиссии', money(value.fees_paid_cents))}
        ${metric('Проскальзывание', money(value.estimated_slippage_cents))}
      </div><p class="footnote">Отчёт ${escapeHtml(report.report_id || 'сохранён')} · ${escapeHtml(report.generated_at || '')}. Walk-forward сигналы учитывают комиссии, вероятность исполнения и проскальзывание.</p>`;
    }

    function forecastAt(item, horizon) {
      const forecasts = item && item.forecasts ? item.forecasts : {};
      return forecasts[String(horizon)] || null;
    }

    function renderTrendChart(item) {
      const horizons = [7, 14, 30];
      const forecasts = horizons.map(horizon => forecastAt(item, horizon));
      if (forecasts.some(value => !value)) return '<div class="empty">Для этого скина пока нет всех трёх горизонтов прогноза.</div>';
      const width = 820;
      const height = 300;
      const pad = { left: 74, right: 24, top: 28, bottom: 44 };
      const current = Number(item.current_price_cents || 0);
      const lows = [current, ...forecasts.map(value => Number(value.low_price_cents || 0))];
      const mids = [current, ...forecasts.map(value => Number(value.predicted_price_cents || 0))];
      const highs = [current, ...forecasts.map(value => Number(value.high_price_cents || 0))];
      const all = [...lows, ...mids, ...highs].filter(Number.isFinite);
      let minimum = Math.min(...all);
      let maximum = Math.max(...all);
      const spread = Math.max(maximum - minimum, maximum * .08, 1);
      minimum = Math.max(0, minimum - spread * .14);
      maximum += spread * .14;
      const x = index => pad.left + index * ((width - pad.left - pad.right) / 3);
      const y = value => pad.top + (maximum - value) / (maximum - minimum) * (height - pad.top - pad.bottom);
      const pointList = values => values.map((value, index) => `${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(' ');
      const fan = [...lows.map((value, index) => [x(index), y(value)]), ...highs.map((value, index) => [x(index), y(value)]).reverse()].map(point => `${point[0].toFixed(1)},${point[1].toFixed(1)}`).join(' ');
      const grid = [0, .25, .5, .75, 1].map(step => {
        const value = maximum - (maximum - minimum) * step;
        const py = y(value);
        return `<line x1="${pad.left}" y1="${py}" x2="${width - pad.right}" y2="${py}" stroke="var(--chart-grid)"/><text x="${pad.left - 10}" y="${py + 4}" fill="var(--muted)" font-size="11" text-anchor="end">${escapeHtml(money(value))}</text>`;
      }).join('');
      const labels = ['Сейчас', '7д', '14д', '30д'].map((label, index) => `<text x="${x(index)}" y="${height - 16}" fill="var(--muted)" font-size="12" text-anchor="middle">${label}</text>`).join('');
      const dots = mids.map((value, index) => `<circle cx="${x(index)}" cy="${y(value)}" r="4" fill="var(--accent)" stroke="var(--panel)" stroke-width="2"/>`).join('');
      return `<div class="chart-shell"><svg class="fan-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="q15 q50 q85 forecast for ${escapeHtml(item.market_hash_name)}"><title>Price forecast range for ${escapeHtml(item.market_hash_name)}</title>${grid}<polygon points="${fan}" fill="var(--chart-fan)" stroke="var(--accent-2)" stroke-opacity=".45"/><polyline points="${pointList(mids)}" fill="none" stroke="var(--accent)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>${dots}${labels}</svg></div>`;
    }

    function renderTrendItem(item) {
      selectedTrendName = item ? String(item.market_hash_name || '') : '';
      document.querySelectorAll('.trend-choice').forEach(button => button.classList.toggle('active', button.dataset.name === selectedTrendName));
      if (!item) {
        document.getElementById('trendDetail').innerHTML = '<div class="empty">Trend-прогноз пока не готов.</div>';
        return;
      }
      const cards = [7, 14, 30].map(horizon => {
        const value = forecastAt(item, horizon) || {};
        const expected = Number(value.expected_return_percent || 0);
        return `<article class="forecast-card"><span>q50 через ${horizon} дней</span><strong>${money(value.predicted_price_cents)}</strong><small>q15 ${money(value.low_price_cents)} · q85 ${money(value.high_price_cents)}</small><small class="${expected >= 0 ? 'good-text' : 'bad-text'}">доходность ${percent(value.expected_return_percent)} · рост ${percent(value.probability_up, true)}</small></article>`;
      }).join('');
      document.getElementById('trendDetail').innerHTML = `<div class="trend-title"><div><strong>${escapeHtml(item.market_hash_name)}</strong><p class="section-copy">Сейчас ${money(item.current_price_cents)} · ${format.format(item.history_rows || 0)} дневных наблюдений</p></div><span class="risk-badge">на ${escapeHtml(item.last_observation_day || '—')}</span></div>${renderTrendChart(item)}<div class="forecast-grid">${cards}</div>`;
    }

    function renderTrendLeaderboard(payload) {
      trendItems = Array.isArray(payload.items) ? payload.items : [];
      document.getElementById('trendAsOf').textContent = payload.as_of_day || 'не готов';
      const horizon = Number(document.getElementById('trendHorizon').value || 7);
      const list = document.getElementById('trendLeaderboard');
      if (!trendItems.length) {
        list.innerHTML = `<div class="trend-list-empty">${escapeHtml(localizedReason(payload.reason, 'Готовых прогнозов пока нет.'))}</div>`;
        renderTrendItem(null);
        return;
      }
      list.innerHTML = trendItems.map(item => {
        const forecast = forecastAt(item, horizon) || {};
        return `<button class="trend-choice" type="button" data-name="${escapeHtml(item.market_hash_name)}"><strong>${escapeHtml(item.market_hash_name)}</strong><span>${percent(forecast.expected_return_percent)}</span><small>сейчас ${money(item.current_price_cents)} · вероятность роста ${percent(forecast.probability_up, true)}</small></button>`;
      }).join('');
      list.querySelectorAll('.trend-choice').forEach(button => button.addEventListener('click', () => {
        const item = trendItems.find(value => value.market_hash_name === button.dataset.name);
        if (item) renderTrendItem(item);
      }));
      const requested = payload.requested_name ? trendItems[0] : trendItems.find(item => item.market_hash_name === selectedTrendName);
      renderTrendItem(requested || trendItems[0]);
    }

    async function loadTrend(name = '') {
      const button = document.getElementById('trendSearchButton');
      button.disabled = true;
      try {
        const params = new URLSearchParams({ horizon: document.getElementById('trendHorizon').value, limit: '24' });
        if (name.trim()) params.set('name', name.trim());
        const payload = await requestJson(`/api/strategy/trend?${params}`);
        renderTrendLeaderboard(payload);
      } finally { button.disabled = false; }
    }

    function riskClass(level) {
      return ['low', 'medium', 'high'].includes(level) ? `risk-${level}` : '';
    }

    function renderRiskDeals(payload) {
      const items = Array.isArray(payload.items) ? payload.items : [];
      const target = document.getElementById('riskDeals');
      if (!items.length) {
        target.innerHTML = '<div class="empty">Нет свежих оценённых возможностей. Сканер заполнит список.</div>';
        return;
      }
      target.innerHTML = items.map(item => {
        const ensemble = item.ensemble_prediction || {};
        const sale = item.sale_estimate || {};
        const risk = item.risk || {};
        const weights = ensemble.weights || {};
        const url = safeExternalUrl(item.csfloat_url);
        return `<article class="risk-card">
          <div class="risk-card-head"><div><h3 title="${escapeHtml(item.skin_name || '')}">${escapeHtml(item.skin_name || 'Неизвестный скин')}</h3><p class="section-copy">покупка ${money(item.buy_price_cents)} · ${escapeHtml((item.segment || {}).price_band || 'без сегмента')}</p></div><span class="risk-badge ${riskClass(risk.level)}">риск ${finite(risk.score) === null ? '—' : Number(risk.score).toFixed(0)}</span></div>
          <div class="risk-stats"><div class="risk-stat"><span>Консервативная прибыль</span><strong class="${Number(ensemble.conservative_profit_cents || 0) >= 0 ? 'good-text' : 'bad-text'}">${money(ensemble.conservative_profit_cents)}</strong></div><div class="risk-stat"><span>Продажа за 30 дней</span><strong>${percent(sale.within_30d, true)}</strong></div><div class="risk-stat"><span>Оценка срока</span><strong>${days(sale.estimated_days)}</strong></div><div class="risk-stat"><span>Ожидаемая прибыль</span><strong>${money(ensemble.expected_profit_cents)}</strong></div></div>
          <div class="weight-row" aria-label="Динамические веса моделей">${Object.entries(weights).map(([model, weight]) => `<span class="weight-chip">${escapeHtml(model)} ${percent(weight, true)}</span>`).join('') || '<span class="weight-chip">веса недоступны</span>'}</div>
          <div class="rank-line"><span>ранг ${finite(item.ranking_score) === null ? '—' : Number(item.ranking_score).toFixed(1)} · веса: ${escapeHtml(ensemble.weight_source || 'равные')}</span>${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">Открыть</a>` : ''}</div>
        </article>`;
      }).join('');
    }

    async function loadRiskDeals() {
      const button = document.getElementById('riskRefreshButton');
      button.disabled = true;
      try {
        const age = document.getElementById('riskAge').value;
        renderRiskDeals(await requestJson(`/api/strategy/deals?limit=30&max_age_hours=${encodeURIComponent(age)}`));
      } finally { button.disabled = false; }
    }

    function renderPortfolio(value) {
      const target = document.getElementById('portfolioResult');
      const items = Array.isArray(value.items) ? value.items : [];
      target.innerHTML = `<div class="portfolio-total"><div><span>Распределено</span><strong>${money(value.spent_cents)}</strong></div><div><span>Консервативная прибыль</span><strong class="${Number(value.total_conservative_profit_cents || 0) >= 0 ? 'good-text' : 'bad-text'}">${money(value.total_conservative_profit_cents)}</strong></div><div><span>Взвешенный риск</span><strong>${finite(value.weighted_risk_score) === null ? '—' : Number(value.weighted_risk_score).toFixed(1)}</strong></div></div><div class="portfolio-items">${items.map(item => `<div class="portfolio-item"><span>${escapeHtml(item.skin_name || item.listing_id || 'Неизвестно')}</span><strong>${money(item.buy_price_cents)}</strong></div>`).join('') || '<div class="empty">Ни один кандидат не прошёл эти лимиты бюджета и риска.</div>'}</div><p class="footnote">${format.format(value.item_count || 0)} предметов · осталось ${money(value.cash_remaining_cents)} · это только предложение, заказы не создавались.</p>`;
    }

    async function postStrategy(url, body) {
      return requestJson(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CS2-Action': 'strategy-read' },
        body: JSON.stringify(body),
      });
    }

    async function loadOverview() {
      const payload = await requestJson('/api/strategy/overview');
      renderOutcomes(payload.outcomes || {});
      renderBacktest(payload.backtest || {});
      const pipeline = payload.trend_pipeline || {};
      const workerState = String(pipeline.state || '').replaceAll('_', ' ');
      document.getElementById('trendWorkerState').textContent = pipeline.process_alive
        ? `работает${workerState ? ` · ${workerState}` : ''}`
        : (workerState ? `не запущен · ${workerState}` : 'не запущен');
      document.getElementById('trendFreshness').textContent = pipeline.fresh ? 'данные свежие' : 'нужно обновление';
      document.getElementById('strategyUpdated').textContent = `обновлено ${new Date(payload.generated_at).toLocaleTimeString('__CS2_INTL_LOCALE__', { timeZone: 'Europe/Moscow' })} MSK`;
    }

    document.getElementById('trendSearchButton').addEventListener('click', () => loadTrend(document.getElementById('trendSearch').value).catch(error => setNotice(error.message, true)));
    document.getElementById('trendSearch').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); document.getElementById('trendSearchButton').click(); } });
    document.getElementById('trendHorizon').addEventListener('change', () => loadTrend().catch(error => setNotice(error.message, true)));
    document.getElementById('riskRefreshButton').addEventListener('click', () => loadRiskDeals().catch(error => setNotice(error.message, true)));
    document.getElementById('riskAge').addEventListener('change', () => loadRiskDeals().catch(error => setNotice(error.message, true)));
    document.querySelectorAll('[data-lab-tab]').forEach(button => button.addEventListener('click', () => setLabTab(button.dataset.labTab)));
    document.getElementById('liquidForm').addEventListener('submit', event => {
      event.preventDefault();
      loadLiquidMarket().catch(error => setNotice(error.message, true));
    });
    document.getElementById('collectionRefreshButton').addEventListener('click', () => loadCollections().catch(error => setNotice(error.message, true)));
    document.getElementById('collectionSearch').addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      loadCollections().catch(error => setNotice(error.message, true));
    });
    document.getElementById('collectionSort').addEventListener('change', () => loadCollections().catch(error => setNotice(error.message, true)));
    document.getElementById('collectionDays').addEventListener('change', () => {
      if (selectedCollectionName) loadCollectionDetail(selectedCollectionName).catch(error => setNotice(error.message, true));
    });

    document.getElementById('advisorForm').addEventListener('submit', async event => {
      event.preventDefault();
      const button = document.getElementById('advisorButton');
      button.disabled = true;
      setNotice('');
      try {
        const payload = await postStrategy('/api/strategy/advisor', {
          action: 'build_budget_advice',
          budget_cents: Math.round(Number(document.getElementById('advisorBudget').value) * 100),
          mode: document.getElementById('advisorMode').value,
          risk: document.getElementById('advisorRisk').value,
          max_items: Number(document.getElementById('advisorMaxItems').value),
          live_age_hours: Number(document.getElementById('advisorAge').value),
        });
        renderAdvisor(payload);
      } catch (error) { setNotice(error.message, true); } finally { button.disabled = false; }
    });

    document.getElementById('portfolioForm').addEventListener('submit', async event => {
      event.preventDefault();
      const button = document.getElementById('portfolioButton');
      button.disabled = true;
      setNotice('');
      try {
        const budget = Math.round(Number(document.getElementById('portfolioBudget').value) * 100);
        const payload = await postStrategy('/api/strategy/portfolio', {
          action: 'build_portfolio', budget_cents: budget,
          risk: document.getElementById('portfolioRisk').value,
          max_items: Number(document.getElementById('portfolioMaxItems').value),
          max_per_skin: Number(document.getElementById('portfolioPerSkin').value),
          max_per_collection: Number(document.getElementById('portfolioPerCollection').value),
          max_candidates: 200, max_age_hours: Number(document.getElementById('portfolioAge').value),
        });
        renderPortfolio(payload);
      } catch (error) { setNotice(error.message, true); } finally { button.disabled = false; }
    });

    document.getElementById('backtestForm').addEventListener('submit', async event => {
      event.preventDefault();
      const button = document.getElementById('backtestButton');
      button.disabled = true;
      setNotice('');
      try {
        const holding = Number(document.getElementById('backtestHolding').value);
        const payload = await postStrategy('/api/strategy/backtest', {
          action: 'read_precomputed_backtest',
          config: {
            holding_days: holding,
            top_k: Number(document.getElementById('backtestTopK').value),
            rebalance_every_days: holding + 2,
            initial_capital_cents: Math.round(Number(document.getElementById('backtestCapital').value) * 100),
          },
        });
        renderBacktest(payload);
      } catch (error) { setNotice(error.message, true); } finally { button.disabled = false; }
    });

    function applyTheme() {
      const value = document.getElementById('themeSelect').value;
      document.body.classList.remove('theme-light', 'theme-midnight', 'theme-graphite');
      if (value !== 'dark') document.body.classList.add(`theme-${value}`);
      try { localStorage.setItem('siteTheme', value); } catch (error) {}
    }
    document.getElementById('themeSelect').addEventListener('change', applyTheme);
    try {
      const saved = localStorage.getItem('siteTheme');
      if (['dark', 'light', 'midnight', 'graphite'].includes(saved)) document.getElementById('themeSelect').value = saved;
    } catch (error) {}
    applyTheme();

    const initialLabTab = new URLSearchParams(window.location.search).get('tab') || 'overview';
    setLabTab(initialLabTab, { updateUrl: false });

    Promise.allSettled([loadOverview(), loadTrend(), loadRiskDeals()]).then(results => {
      const failures = results.filter(result => result.status === 'rejected');
      if (failures.length) setNotice(failures.map(result => result.reason.message).join(' · '), true);
    });
  </script>
</body>
</html>
"""


PRIVATE_DESIGN_CSS = r"""
    :root {
      --radius-xs: 8px;
      --radius-sm: 11px;
      --radius-md: 16px;
      --radius-lg: 22px;
      --accent-soft: color-mix(in srgb, var(--accent) 14%, transparent);
      --accent-line: color-mix(in srgb, var(--accent) 42%, var(--line));
      --panel-glow: color-mix(in srgb, var(--panel) 94%, var(--accent) 6%);
      --shadow-sm: 0 8px 24px rgba(0, 0, 0, 0.12);
      --shadow-lg: 0 24px 70px rgba(0, 0, 0, 0.22);
    }

    html { scroll-behavior: smooth; }
    body {
      min-height: 100vh;
      background:
        radial-gradient(900px 480px at 7% -8%, color-mix(in srgb, var(--accent) 14%, transparent), transparent 68%),
        radial-gradient(700px 400px at 96% 10%, color-mix(in srgb, var(--good) 8%, transparent), transparent 72%),
        var(--bg);
      font-size: 14px;
      line-height: 1.5;
      letter-spacing: -0.005em;
    }
    body.theme-light {
      --shadow-sm: 0 8px 24px rgba(42, 55, 86, 0.08);
      --shadow-lg: 0 24px 70px rgba(42, 55, 86, 0.13);
    }
    [hidden] { display: none !important; }
    button, input, select, textarea { font: inherit; }
    button, a { -webkit-tap-highlight-color: transparent; }
    :focus-visible {
      outline: 3px solid color-mix(in srgb, var(--accent) 40%, transparent);
      outline-offset: 2px;
    }

    header {
      z-index: 10;
      background: color-mix(in srgb, var(--bg) 82%, transparent);
      box-shadow: 0 1px 0 color-mix(in srgb, var(--line) 75%, transparent);
      backdrop-filter: blur(18px) saturate(135%);
      -webkit-backdrop-filter: blur(18px) saturate(135%);
    }
    .topbar {
      display: grid;
      grid-template-columns: auto minmax(280px, 1fr) auto;
      min-height: 76px;
      gap: 22px;
    }
    .site-brand {
      display: inline-flex;
      align-items: center;
      gap: 11px;
      min-width: 0;
      color: var(--text);
      text-decoration: none;
    }
    .site-brand > span:last-child { display: grid; min-width: 0; }
    .site-brand strong { font-size: 15px; line-height: 1.2; letter-spacing: -0.02em; }
    .site-brand small {
      max-width: 230px;
      margin-top: 3px;
      overflow: hidden;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.2;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .brand-mark {
      display: inline-grid;
      place-items: center;
      width: 38px;
      height: 38px;
      flex: 0 0 auto;
      border: 1px solid var(--accent-line);
      border-radius: 12px;
      background: linear-gradient(145deg, color-mix(in srgb, var(--accent) 30%, var(--panel)), var(--panel));
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.12), 0 8px 24px color-mix(in srgb, var(--accent) 18%, transparent);
      color: var(--text);
      font-size: 12px;
      font-weight: 850;
      letter-spacing: -0.04em;
    }
    .site-nav {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 3px;
      width: max-content;
      max-width: 100%;
      margin: 0 auto;
      padding: 4px;
      border: 1px solid color-mix(in srgb, var(--line) 82%, transparent);
      border-radius: 13px;
      background: color-mix(in srgb, var(--control-bg) 70%, transparent);
    }
    .nav-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      padding: 0 13px;
      border-radius: 9px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 650;
      text-decoration: none;
      transition: color 140ms ease, background 140ms ease, box-shadow 140ms ease;
    }
    .nav-link:hover { color: var(--text); background: var(--panel-2); }
    .nav-link.active, .nav-link[aria-current="page"] {
      color: var(--text);
      background: var(--panel);
      box-shadow: 0 3px 10px rgba(0, 0, 0, 0.14);
    }
    .header-tools { justify-self: end; flex-wrap: nowrap; margin: 0; }
    .header-tools select {
      width: auto;
      min-width: 112px;
      max-width: 150px;
    }
    .header-tools .pill {
      max-width: 160px;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    main { padding-top: 26px; padding-bottom: 44px; }
    .dashboard-hero, .tool-hero {
      position: relative;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 32px;
      min-height: 174px;
      margin-bottom: 16px;
      padding: clamp(24px, 3.2vw, 42px);
      overflow: hidden;
      border: 1px solid var(--accent-line);
      border-radius: var(--radius-lg);
      background:
        linear-gradient(120deg, color-mix(in srgb, var(--accent) 14%, var(--panel)) 0%, var(--panel) 56%, color-mix(in srgb, var(--good) 6%, var(--panel)) 100%);
      box-shadow: var(--shadow-lg);
    }
    .dashboard-hero::after, .tool-hero::after {
      position: absolute;
      right: -75px;
      top: -100px;
      width: 290px;
      height: 290px;
      border: 48px solid color-mix(in srgb, var(--accent) 7%, transparent);
      border-radius: 50%;
      content: "";
      pointer-events: none;
    }
    .dashboard-hero > *, .tool-hero > * { position: relative; z-index: 1; }
    .dashboard-hero > div:first-child, .tool-hero > div:first-child { min-width: 0; }
    .dashboard-hero h1, .tool-hero h1 {
      max-width: 760px;
      margin: 5px 0 0;
      font-size: clamp(26px, 3vw, 43px);
      font-weight: 790;
      line-height: 1.08;
      letter-spacing: -0.045em;
    }
    .dashboard-hero p, .tool-hero p {
      max-width: 680px;
      margin: 13px 0 0;
      color: var(--muted);
      font-size: 15px;
    }
    .eyebrow {
      color: var(--accent);
      font-size: 10px;
      font-weight: 820;
      line-height: 1.2;
      letter-spacing: 0.14em;
    }
    .hero-actions { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }

    .button {
      min-height: 38px;
      padding: 0 14px;
      border-radius: 10px;
      font-weight: 650;
      transition: border-color 140ms ease, background 140ms ease, color 140ms ease, transform 140ms ease, box-shadow 140ms ease;
    }
    .button:hover { transform: translateY(-1px); box-shadow: var(--shadow-sm); }
    .button:active { transform: translateY(0); }
    .button-primary {
      border-color: color-mix(in srgb, var(--accent) 72%, var(--line));
      background: var(--accent);
      color: var(--bg);
      box-shadow: 0 8px 22px color-mix(in srgb, var(--accent) 22%, transparent);
    }
    .button-primary:hover {
      border-color: var(--accent);
      background: color-mix(in srgb, var(--accent) 86%, white 14%);
    }
    .button-quiet { color: var(--muted); }
    input, select, textarea {
      min-height: 39px;
      border-radius: 10px;
      transition: border-color 140ms ease, box-shadow 140ms ease, background 140ms ease;
    }
    input:hover, select:hover, textarea:hover { border-color: color-mix(in srgb, var(--accent) 38%, var(--line)); }
    input:focus, select:focus, textarea:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-soft);
    }
    .pill { border-radius: 999px; background: color-mix(in srgb, var(--control-bg) 82%, transparent); }
    .pill[data-tone="good"] { border-color: color-mix(in srgb, var(--good) 45%, var(--line)); color: var(--good); }
    .pill[data-tone="warn"] { border-color: color-mix(in srgb, var(--warn) 45%, var(--line)); color: var(--warn); }
    .pill[data-tone="bad"] { border-color: color-mix(in srgb, var(--bad) 45%, var(--line)); color: var(--bad); }

    .panel {
      border-color: color-mix(in srgb, var(--line) 86%, transparent);
      border-radius: var(--radius-md);
      background: color-mix(in srgb, var(--panel) 97%, transparent);
      box-shadow: var(--shadow-sm);
    }
    .full { margin-top: 16px; }
    .section-head {
      min-height: 62px;
      padding: 14px 17px;
      background: color-mix(in srgb, var(--panel-2) 76%, transparent);
    }
    .section-head h2 { margin-top: 3px; font-size: 15px; letter-spacing: -0.015em; }
    .scroll { overflow: auto; scrollbar-width: thin; scrollbar-color: var(--line) transparent; }
    th, td { padding: 11px 12px; }
    th { text-transform: uppercase; letter-spacing: 0.04em; }
    tbody tr { transition: background 120ms ease; }
    tbody tr:hover { background: color-mix(in srgb, var(--accent) 5%, transparent); }
    .thumb, .no-thumb { border-radius: 10px; background: var(--image-bg); }

    .health-banner {
      display: grid;
      grid-template-columns: 12px minmax(0, 1fr);
      align-items: center;
      gap: 12px;
      margin: 16px 0;
      padding: 14px 17px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--panel);
    }
    .health-banner[data-tone="good"] { border-color: color-mix(in srgb, var(--good) 40%, var(--line)); background: color-mix(in srgb, var(--good) 6%, var(--panel)); }
    .health-banner[data-tone="warn"] { border-color: color-mix(in srgb, var(--warn) 48%, var(--line)); background: color-mix(in srgb, var(--warn) 7%, var(--panel)); }
    .health-banner[data-tone="bad"] { border-color: color-mix(in srgb, var(--bad) 48%, var(--line)); background: color-mix(in srgb, var(--bad) 7%, var(--panel)); }
    .health-icon { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); box-shadow: 0 0 0 5px color-mix(in srgb, var(--muted) 12%, transparent); }
    [data-tone="good"] .health-icon { background: var(--good); box-shadow: 0 0 0 5px color-mix(in srgb, var(--good) 13%, transparent); }
    [data-tone="warn"] .health-icon { background: var(--warn); box-shadow: 0 0 0 5px color-mix(in srgb, var(--warn) 13%, transparent); }
    [data-tone="bad"] .health-icon { background: var(--bad); box-shadow: 0 0 0 5px color-mix(in srgb, var(--bad) 13%, transparent); }
    .health-banner > div { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
    .health-banner strong { font-size: 13px; }
    .health-banner span:last-child { color: var(--muted); font-size: 12px; }

    .workspace-tabs {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 7px;
      margin: 18px 0 28px;
      padding: 6px;
      border: 1px solid var(--line);
      border-radius: 15px;
      background: color-mix(in srgb, var(--panel) 84%, transparent);
      box-shadow: var(--shadow-sm);
    }
    .workspace-tab {
      display: grid;
      gap: 2px;
      min-height: 52px;
      padding: 8px 14px;
      border: 1px solid transparent;
      border-radius: 10px;
      background: transparent;
      color: var(--muted);
      text-align: left;
      cursor: pointer;
    }
    .workspace-tab span { color: inherit; font-size: 13px; font-weight: 720; }
    .workspace-tab small { overflow: hidden; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
    .workspace-tab:hover { background: var(--panel-2); color: var(--text); }
    .workspace-tab[aria-selected="true"] {
      border-color: var(--accent-line);
      background: linear-gradient(135deg, var(--accent-soft), var(--panel));
      color: var(--text);
      box-shadow: 0 5px 18px rgba(0, 0, 0, 0.12);
    }
    .view-heading {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 20px;
      margin: 4px 2px 14px;
    }
    .view-heading h2 { margin: 4px 0 0; font-size: 22px; letter-spacing: -0.03em; }
    .view-description { max-width: 560px; color: var(--muted); font-size: 13px; text-align: right; }
    .metrics { grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
    .metric { min-height: 105px; padding: 16px; }
    .metric .value { margin-top: 10px; font-size: 25px; letter-spacing: -0.035em; }
    .model-health-card { border-radius: var(--radius-sm); }
    .schedule-summary, .skin-detail-summary { gap: 0; padding: 0; background: transparent; }
    .schedule-summary .mini-stat, .skin-detail-summary .mini-stat { padding: 14px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
    .schedule-summary .mini-stat:last-child, .skin-detail-summary .mini-stat:last-child { border-right: 0; }
    .service-control-body {
      display: grid;
      align-items: stretch;
      gap: 20px;
      min-height: 92px;
      padding: 17px;
    }
    .service-control-body strong { font-size: 15px; }
    .service-control-body p { max-width: 720px; margin: 5px 0 0; color: var(--muted); font-size: 12px; }
    .scanner-control-layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(310px, auto);
      align-items: center;
      gap: 28px;
      width: 100%;
    }
    .scanner-control-copy { min-width: 0; }
    .scanner-runtime-summary { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 15px; }
    .scanner-runtime-summary > span {
      display: grid;
      gap: 2px;
      min-width: 126px;
      padding: 9px 11px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: color-mix(in srgb, var(--control-bg) 74%, transparent);
    }
    .scanner-runtime-summary small { color: var(--muted); font-size: 10px; }
    .scanner-runtime-summary b { overflow: hidden; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
    .scanner-control-actions { display: grid; gap: 9px; min-width: 310px; }
    .scanner-control-actions > label { display: grid; gap: 5px; color: var(--muted); font-size: 11px; }
    .scanner-interval-input { display: grid; grid-template-columns: minmax(90px, 1fr) auto; align-items: center; gap: 8px; }
    .scanner-interval-input input { width: 100%; }
    .scanner-action-row { display: flex; gap: 8px; }
    .scanner-action-row .button { flex: 1 1 auto; }
    .scanner-feedback { min-height: 17px; color: var(--muted); font-size: 11px; }
    .scanner-feedback[data-tone="good"] { color: var(--good); }
    .scanner-feedback[data-tone="warn"] { color: var(--warn); }
    .scanner-feedback[data-tone="bad"] { color: var(--bad); }
    .scanner-target-shell { display: grid; gap: 14px; padding-top: 18px; border-top: 1px solid var(--line); }
    .scanner-target-head, .scanner-target-editor-head, .scanner-target-bulk, .scanner-target-pages {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .scanner-target-head p, .scanner-target-editor-head p { margin-top: 4px; }
    .scanner-profile-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .scanner-profile-card {
      display: grid;
      gap: 9px;
      min-width: 0;
      padding: 13px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--subpanel-bg);
      color: var(--text);
      cursor: pointer;
      text-align: left;
    }
    .scanner-profile-card:hover, .scanner-profile-card.active { border-color: var(--accent-line); background: var(--accent-soft); }
    .scanner-profile-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .scanner-profile-card strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .scanner-profile-card p { min-height: 34px; margin: 0; line-height: 1.4; }
    .scanner-profile-card-foot { display: flex; flex-wrap: wrap; gap: 5px; color: var(--muted); font-size: 10px; }
    .scanner-target-editor { display: grid; gap: 12px; padding: 15px; border: 1px solid var(--line); border-radius: 13px; background: var(--subpanel-bg); }
    .scanner-target-editor h3 { margin: 4px 0 0; font-size: 17px; }
    .scanner-target-toolbar { display: grid; grid-template-columns: 180px 160px minmax(220px, 1fr) auto; align-items: end; gap: 9px; }
    .scanner-target-toolbar label:not(.scanner-target-enabled) { display: grid; gap: 5px; color: var(--muted); font-size: 10px; }
    .scanner-target-toolbar select, .scanner-target-toolbar input { width: 100%; max-width: none; }
    .scanner-target-enabled { display: flex; align-items: center; gap: 8px; min-height: 38px; color: var(--text); font-size: 11px; }
    .scanner-target-enabled input, .scanner-target-row-check { accent-color: var(--accent); }
    .scanner-target-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 7px; }
    .scanner-target-summary > span { display: grid; gap: 2px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 9px; }
    .scanner-target-summary small { color: var(--muted); font-size: 9px; }
    .scanner-target-summary b { font-size: 11px; }
    .scanner-target-bulk { justify-content: flex-start; flex-wrap: wrap; }
    .scanner-target-bulk .scanner-feedback { margin-right: auto; }
    .scanner-target-scroll { max-height: 440px; border: 1px solid var(--line); border-radius: 10px; }
    .scanner-target-scroll table { margin: 0; }
    .scanner-target-name { display: grid; gap: 3px; }
    .scanner-target-name small { color: var(--muted); overflow-wrap: anywhere; }
    .scanner-target-pages { justify-content: flex-end; }
    .scanner-target-pages .pill { min-width: 130px; text-align: center; }

    .workflow-steps {
      display: flex;
      align-items: center;
      gap: 5px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .workflow-steps li { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 12px; white-space: nowrap; }
    .workflow-steps li:not(:last-child)::after { width: 24px; height: 1px; margin: 0 4px; background: var(--line); content: ""; }
    .workflow-steps span { display: grid; place-items: center; width: 25px; height: 25px; border: 1px solid var(--accent-line); border-radius: 50%; background: var(--accent-soft); color: var(--text); font-weight: 760; }
    .workbench-grid { align-items: start; }
    .page-deal .two { grid-template-columns: minmax(0, 1.08fr) minmax(420px, .92fr); }
    .page-deal .form-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .page-deal .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .page-deal .panel > .body { padding: 18px; }
    .page-deal pre { max-height: 410px; margin: 0; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
    .comparison-callout {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 14px 16px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: color-mix(in srgb, var(--control-bg) 78%, transparent);
    }
    .comparison-callout[data-tone="good"] { border-color: color-mix(in srgb, var(--good) 48%, var(--line)); background: color-mix(in srgb, var(--good) 7%, var(--panel)); }
    .comparison-callout[data-tone="watch"] { border-color: color-mix(in srgb, var(--warn) 48%, var(--line)); background: color-mix(in srgb, var(--warn) 7%, var(--panel)); }
    .comparison-callout[data-tone="bad"] { border-color: color-mix(in srgb, var(--bad) 48%, var(--line)); background: color-mix(in srgb, var(--bad) 7%, var(--panel)); }
    .comparison-callout strong { display: block; margin-top: 4px; font-size: 16px; }
    .comparison-callout p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
    .comparison-spread { flex: 0 0 auto; color: var(--muted); font-size: 12px; white-space: nowrap; }
    .model-comparison-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .model-prediction-card {
      min-width: 0;
      padding: 13px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--subpanel-bg);
    }
    .model-prediction-card.selected { border-color: var(--accent-line); box-shadow: inset 0 0 0 1px var(--accent-soft); }
    .model-prediction-card.unavailable { opacity: .72; }
    .model-prediction-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: var(--muted); font-size: 11px; }
    .model-prediction-head > span:first-child { color: var(--text); font-size: 13px; font-weight: 760; }
    .model-state { font-weight: 680; }
    .model-state.good { color: var(--good); }
    .model-state.watch { color: var(--warn); }
    .model-state.bad { color: var(--bad); }
    .model-prediction-card > strong { display: block; margin-top: 10px; font-size: 22px; letter-spacing: -0.03em; }
    .model-prediction-card > small { display: block; margin-top: 2px; overflow-wrap: anywhere; color: var(--muted); }
    .model-prediction-foot { margin-top: 9px; overflow-wrap: anywhere; color: var(--muted); font-size: 11px; }

    .disclosure {
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: color-mix(in srgb, var(--subpanel-bg, var(--panel-2)) 84%, transparent);
      overflow: hidden;
    }
    .disclosure > summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      min-height: 50px;
      padding: 11px 14px;
      cursor: pointer;
      list-style: none;
      font-weight: 680;
    }
    .disclosure > summary::-webkit-details-marker { display: none; }
    .disclosure > summary::after { flex: 0 0 auto; color: var(--muted); content: "+"; font-size: 20px; font-weight: 400; }
    .disclosure[open] > summary::after { content: "−"; }
    .disclosure > summary:hover { background: var(--accent-soft); }
    .disclosure > summary small { margin-left: auto; color: var(--muted); font-size: 11px; font-weight: 450; }
    .disclosure-body { padding: 14px; border-top: 1px solid var(--line); }
    .advanced-fields { margin-top: 4px; }
    .sticker-editor, .result-details { margin: 14px; }
    .table-scroll { overflow-x: auto; border-top: 1px solid var(--line); }

    .filter-shell { display: grid; gap: 12px; padding: 16px; border-bottom: 1px solid var(--line); }
    .filter-primary { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(190px, auto) auto; gap: 10px; align-items: end; }
    .search-field input { width: 100%; max-width: none; }
    .filter-field { display: grid; gap: 5px; color: var(--muted); font-size: 11px; }
    .filter-field select, .filter-field input { width: 100%; max-width: none; }
    .filter-grid { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 12px; align-items: end; }
    .filter-grid .button { align-self: end; }
    .pagination-bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .pagination-bar > div { display: flex; gap: 7px; }
    .deal-summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-bottom: 1px solid var(--line); }
    .summary-card { display: grid; gap: 4px; padding: 14px 17px; border-right: 1px solid var(--line); }
    .summary-card:last-child { border-right: 0; }
    .summary-card span { color: var(--muted); font-size: 11px; }
    .summary-card strong { font-size: 21px; letter-spacing: -0.03em; }
    .page-deals .item-img { width: 174px; height: 130px; border-radius: 11px; }
    .page-deals .scroll { max-height: calc(100vh - 210px); overflow: auto; }
    .page-deals .risk-note { padding: 11px 16px; }
    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }

    .back-to-top {
      position: fixed;
      right: 20px;
      bottom: 20px;
      z-index: 8;
      display: grid;
      place-items: center;
      width: 42px;
      height: 42px;
      padding: 0;
      border: 1px solid var(--accent-line);
      border-radius: 13px;
      background: color-mix(in srgb, var(--panel) 90%, transparent);
      color: var(--text);
      box-shadow: var(--shadow-lg);
      opacity: 0;
      pointer-events: none;
      transform: translateY(8px);
      transition: opacity 150ms ease, transform 150ms ease;
      cursor: pointer;
      backdrop-filter: blur(12px);
    }
    .back-to-top.visible { opacity: 1; pointer-events: auto; transform: translateY(0); }

    @media (max-width: 1180px) {
      .topbar { grid-template-columns: auto 1fr; gap: 12px 18px; padding: 10px 0; }
      .site-nav { grid-column: 1 / -1; grid-row: 2; width: 100%; order: 3; }
      .header-tools { grid-column: 2; grid-row: 1; }
      .filter-grid { grid-template-columns: repeat(3, minmax(150px, 1fr)); }
      .page-deal .two { grid-template-columns: 1fr; }
      .page-deal .metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .scanner-profile-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 820px) {
      .wrap { width: min(100vw - 22px, 1500px); }
      main { padding-top: 16px; }
      .dashboard-hero, .tool-hero { min-height: 0; padding: 25px; align-items: flex-start; flex-direction: column; }
      .dashboard-hero > div:first-child, .tool-hero > div:first-child { width: 100%; }
      .dashboard-hero h1, .tool-hero h1 { font-size: clamp(27px, 8vw, 38px); }
      .hero-actions, .tool-hero > .button { width: 100%; }
      .hero-actions .button, .tool-hero > .button { flex: 1 1 180px; }
      .workspace-tabs { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .view-heading { align-items: flex-start; flex-direction: column; gap: 5px; }
      .view-description { text-align: left; }
      .filter-grid { grid-template-columns: repeat(2, minmax(140px, 1fr)); }
      .deal-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .summary-card:nth-child(2) { border-right: 0; }
      .summary-card:nth-child(-n + 2) { border-bottom: 1px solid var(--line); }
      .health-banner > div { align-items: flex-start; flex-direction: column; gap: 2px; }
      .service-control-body { align-items: flex-start; flex-direction: column; gap: 12px; }
      .scanner-control-layout { grid-template-columns: 1fr; gap: 20px; }
      .scanner-control-actions { width: 100%; min-width: 0; }
      .scanner-target-toolbar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .model-health-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 620px) {
      .wrap { width: min(100vw - 16px, 1500px); }
      .topbar { display: flex; align-items: center; flex-direction: row; flex-wrap: wrap; min-height: 0; padding: 9px 0; }
      .site-brand { flex: 1 1 180px; }
      .site-brand small { max-width: 170px; }
      .header-tools { width: auto; margin-left: auto; }
      .header-tools .control-label, .header-tools .pill, .header-tools .button { display: none; }
      .header-tools select { width: 94px; min-width: 0; }
      .site-nav { order: 3; flex: 1 0 100%; justify-content: stretch; overflow-x: auto; }
      .nav-link { flex: 1 0 auto; padding: 0 10px; }
      .dashboard-hero, .tool-hero { border-radius: var(--radius-md); padding: 22px 18px; }
      .workflow-steps { width: 100%; justify-content: space-between; }
      .workflow-steps li:not(:last-child)::after { width: 10px; }
      .workspace-tabs { gap: 5px; margin-bottom: 22px; padding: 5px; }
      .workspace-tab { min-height: 48px; padding: 7px 9px; }
      .workspace-tab small { display: none; }
      .metrics, .page-deal .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .metric { min-height: 96px; padding: 13px; }
      .metric .value { font-size: 21px; }
      .section-head { align-items: stretch; flex-direction: column; min-height: 0; }
      .section-actions { width: 100%; }
      .section-actions > input, .section-actions > select, .section-actions > .control { width: 100%; max-width: none; }
      .section-actions .button { flex: 1 1 90px; }
      .page-deal .form-grid { grid-template-columns: 1fr; }
      .page-deal .wide { grid-column: auto; }
      .comparison-callout { align-items: flex-start; flex-direction: column; gap: 8px; }
      .comparison-spread { white-space: normal; }
      .model-comparison-grid { grid-template-columns: 1fr; }
      .disclosure > summary { align-items: flex-start; }
      .disclosure > summary small { display: none; }
      .sticker-editor, .result-details { margin: 10px; }
      .filter-primary { grid-template-columns: 1fr; }
      .filter-grid { grid-template-columns: 1fr; }
      .scanner-runtime-summary { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .scanner-runtime-summary > span { min-width: 0; }
      .scanner-runtime-summary b { overflow: visible; text-overflow: clip; white-space: normal; }
      .scanner-action-row { flex-direction: column; }
      .scanner-profile-grid, .scanner-target-summary, .scanner-target-toolbar { grid-template-columns: 1fr; }
      .scanner-target-head, .scanner-target-editor-head { align-items: flex-start; flex-direction: column; }
      .pagination-bar { align-items: stretch; flex-direction: column; }
      .pagination-bar > div { display: grid; grid-template-columns: 1fr 1fr; }
      .page-deals .section-head .pill { width: auto; align-self: flex-start; }
      .page-deals .item-img { width: 145px; height: 108px; }
      .back-to-top { right: 12px; bottom: 12px; }
    }
    @media (max-width: 410px) {
      .workflow-steps { display: grid; gap: 7px; }
      .workflow-steps li:not(:last-child)::after { display: none; }
      .deal-summary-grid { grid-template-columns: 1fr; }
      .summary-card, .summary-card:nth-child(2) { border-right: 0; border-bottom: 1px solid var(--line); }
      .summary-card:last-child { border-bottom: 0; }
    }
    @media (prefers-reduced-motion: reduce) {
      html { scroll-behavior: auto; }
      *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
    }
"""


PRIVATE_ENHANCEMENT_SCRIPT = r"""
  <script>
    (() => {
      const main = document.querySelector('main');
      if (main && !main.id) main.id = 'main-content';

      const currentPath = window.location.pathname === '/dashboard' ? '/' : window.location.pathname;
      document.querySelectorAll('[data-nav-path]').forEach(link => {
        const active = link.dataset.navPath === currentPath;
        link.classList.toggle('active', active);
        if (active) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      });

      const backToTop = document.createElement('button');
      backToTop.className = 'back-to-top';
      backToTop.type = 'button';
      backToTop.setAttribute('aria-label', 'Back to top');
      backToTop.textContent = '↑';
      backToTop.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
      document.body.appendChild(backToTop);
      const updateBackToTop = () => backToTop.classList.toggle('visible', window.scrollY > 560);
      window.addEventListener('scroll', updateBackToTop, { passive: true });
      updateBackToTop();

      document.addEventListener('keydown', event => {
        if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) return;
        const active = document.activeElement;
        if (active && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName)) return;
        const search = Array.from(document.querySelectorAll('input')).find(input =>
          !input.disabled && input.offsetParent !== null && /search|link|skin/i.test(input.placeholder || '')
        );
        if (!search) return;
        event.preventDefault();
        search.focus();
      });

      if (currentPath === '/deals') {
        const params = new URLSearchParams(window.location.search);
        const disclosure = document.querySelector('.filter-disclosure');
        const advancedActive =
          (params.get('category') && params.get('category') !== 'all')
          || (params.get('confidence') && params.get('confidence') !== 'all')
          || (params.get('listing_type') && params.get('listing_type') !== 'all')
          || (params.get('seen_hours') && params.get('seen_hours') !== '24')
          || (params.get('created_hours') && params.get('created_hours') !== '0')
          || params.has('min_price')
          || params.has('max_price');
        if (disclosure && advancedActive) disclosure.open = true;
      }
    })();
  </script>
"""


LEAN_INTERFACE_CSS = r"""
  <style id="cs2-lean-interface">
    /* One calm application shell for legacy and standalone workspaces. */
    html, body { max-width: 100%; overflow-x: hidden; }
    body { --accent: #8068ff !important; --accent-2: #9d8cff !important; }
    body main, body header, body footer { min-width: 0; }
    body main > *, header .topbar, header .top, header .wrap { min-width: 0; }
    input, select, textarea, button { max-width: 100%; }
    header { box-shadow: none !important; }
    .lean-nav {
      display: flex !important;
      align-items: center;
      justify-content: center;
      gap: 4px !important;
      min-width: 0;
      overflow: visible !important;
    }
    .lean-nav-link, .lean-more > summary {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      padding: 0 11px;
      border: 1px solid transparent;
      border-radius: 10px;
      color: var(--muted, #98a3b7) !important;
      background: transparent;
      text-decoration: none;
      font-size: 12px;
      font-weight: 760;
      line-height: 1;
      white-space: nowrap;
      cursor: pointer;
      list-style: none;
    }
    .lean-more > summary::-webkit-details-marker { display: none; }
    .lean-nav-link:hover, .lean-more > summary:hover,
    .lean-nav-link.active, .lean-nav-link[aria-current="page"],
    .lean-more[data-active="true"] > summary {
      border-color: color-mix(in srgb, var(--accent, #8068ff) 36%, var(--line, #293549));
      background: color-mix(in srgb, var(--accent, #8068ff) 12%, transparent);
      color: var(--text, #f4f7fc) !important;
    }
    .lean-more { position: relative; flex: 0 0 auto; }
    .lean-more-menu {
      position: absolute;
      top: calc(100% + 8px);
      right: 0;
      z-index: 30;
      display: grid;
      min-width: 205px;
      gap: 3px;
      padding: 7px;
      border: 1px solid var(--line, #293549);
      border-radius: 13px;
      background: color-mix(in srgb, var(--panel, #111722) 96%, transparent);
      box-shadow: 0 18px 48px rgba(0, 0, 0, .34);
      backdrop-filter: blur(18px);
    }
    .lean-more-menu .lean-nav-link { justify-content: flex-start; width: 100%; }
    body main { padding-top: 20px; }
    .dashboard-hero, .tool-hero, .strategy-hero,
    .page-account main > .hero, .page-catalog main > .hero,
    .page-market-history main > .hero, .page-knives .hero-copy {
      min-height: 0 !important;
      padding: 21px !important;
      border-radius: 16px !important;
      box-shadow: none !important;
    }
    .dashboard-hero h1, .tool-hero h1, .strategy-hero h1,
    .page-account main > .hero h1, .page-catalog main > .hero h1,
    .page-market-history main > .hero h1, .page-knives .hero h1 {
      font-size: clamp(28px, 4vw, 43px) !important;
      line-height: 1.06 !important;
      letter-spacing: -.035em !important;
    }
    .dashboard-hero p, .tool-hero p, .strategy-hero p,
    .page-catalog main > .hero p, .page-market-history main > .hero p,
    .page-knives .hero p { max-width: 780px; line-height: 1.55 !important; }
    .panel, .metric, .metric-card, .source-card { box-shadow: none !important; }
    .section-head, .panel-head { min-height: 0 !important; }
    .page-dashboard .metric-technical,
    .page-dashboard .legacy-dashboard-duplicate,
    .page-strategy .legacy-strategy-block { display: none !important; }
    .page-dashboard #dbName { display: none !important; }
    .hero-actions .button, .hero-actions button,
    .actions .button, .actions button {
      flex: 0 0 auto !important;
      min-height: 42px !important;
      height: auto !important;
    }
    .page-knives .hero { grid-template-columns: minmax(0, 1fr) auto !important; gap: 10px !important; }
    .page-knives .model-note { display: flex; align-items: center; padding: 14px !important; border-radius: 16px !important; box-shadow: none !important; }
    .page-knives .model-note h2, .page-knives .model-note p,
    .page-knives .model-note .truth-row, .page-knives .model-note .status-head > .eyebrow { display: none !important; }
    .page-knives .metrics { grid-template-columns: repeat(4, minmax(0, 1fr)) !important; }
    .page-knives .metrics .metric:nth-child(2) { display: none; }
    .page-catalog .tabs { margin: 13px 0 !important; }
    @media (max-width: 900px) {
      .lean-nav { order: 3; width: 100%; flex: 1 0 100%; justify-content: flex-start; overflow-x: auto !important; scrollbar-width: none; }
      .lean-nav::-webkit-scrollbar { display: none; }
      .lean-more { display: none; }
      .page-knives .hero { grid-template-columns: 1fr !important; }
      .page-knives .model-note { justify-content: flex-start; }
      .page-knives .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
    }
    @media (max-width: 620px) {
      .lean-nav-link { min-height: 36px; padding: 0 10px; font-size: 11px; }
      .dashboard-hero, .tool-hero, .strategy-hero,
      .page-account main > .hero, .page-catalog main > .hero,
      .page-market-history main > .hero, .page-knives .hero-copy { padding: 17px !important; }
    }
  </style>
"""


LEAN_INTERFACE_SCRIPT = r"""
  <script id="cs2-lean-interface-script">
    (() => {
      const copy = {
        ru: { deals: 'Сделки', evaluate: 'Проверить лот', catalog: 'Рынок', strategy: 'Стратегия', inventory: 'Инвентарь', more: 'Ещё', knives: 'Ножи', history: 'История цен', system: 'Система' },
        en: { deals: 'Deals', evaluate: 'Check listing', catalog: 'Market', strategy: 'Strategy', inventory: 'Inventory', more: 'More', knives: 'Knives', history: 'Price history', system: 'System' },
      };
      const nav = document.querySelector('.lean-nav');
      if (!nav) return;
      const locale = () => document.documentElement.lang.toLowerCase().startsWith('en') ? 'en' : 'ru';
      const updateLabels = () => {
        const labels = copy[locale()];
        nav.querySelectorAll('[data-lean-label]').forEach(node => {
          const value = labels[node.dataset.leanLabel];
          if (value) node.textContent = value;
        });
        document.querySelectorAll('.site-brand strong, .brand-copy strong').forEach(node => { node.textContent = 'CS2 Market'; });
        document.querySelectorAll('header .brand').forEach(node => { if (!node.querySelector('*')) node.textContent = 'CS2 Market'; });
      };
      const current = location.pathname === '/neural' ? '/deal' : location.pathname;
      const catalogPaths = new Set(['/catalog', '/knives', '/market-history']);
      nav.querySelectorAll('[data-nav-path]').forEach(link => {
        const path = link.dataset.navPath;
        const active = path === current || (path === '/catalog' && catalogPaths.has(current));
        link.classList.toggle('active', active);
        if (active) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      });
      const more = nav.querySelector('.lean-more');
      if (more) {
        more.dataset.active = String(Array.from(more.querySelectorAll('[data-nav-path]'))
          .some(link => link.dataset.navPath === current));
        more.querySelectorAll('a').forEach(link => link.addEventListener('click', () => { more.open = false; }));
        document.addEventListener('click', event => { if (more.open && !more.contains(event.target)) more.open = false; });
        document.addEventListener('keydown', event => { if (event.key === 'Escape') more.open = false; });
      }
      updateLabels();
      new MutationObserver(updateLabels).observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] });
    })();
  </script>
"""


def json_response(handler, status, payload, *, extra_headers=None):
    body = json.dumps(payload, ensure_ascii=False, default=json_default).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    send_security_headers(handler)
    for name, value in extra_headers or ():
        handler.send_header(str(name), str(value))
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        pass


def json_default(value):
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return str(value)


def send_security_headers(handler):
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'self'; connect-src 'self'; img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    )


def get_or_create_token():
    path = Path(TOKEN_PATH)
    configured = app_config.read_secret("DASHBOARD_TOKEN", path)
    if configured:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", configured):
            raise ValueError(
                "DASHBOARD_TOKEN must contain 16-256 URL-safe characters: letters, digits, '_' or '-'."
            )
        return configured

    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


DASHBOARD_TOKEN = get_or_create_token()


def get_account_service():
    """Return the thread-safe account service, initialised lazily for tests."""

    global ACCOUNT_SERVICE
    if ACCOUNT_SERVICE is None:
        with ACCOUNT_SERVICE_LOCK:
            if ACCOUNT_SERVICE is None:
                ACCOUNT_SERVICE = account_backend.AccountService(
                    ACCOUNT_DB_PATH,
                    password_reset_sender=account_backend.password_reset_sender_from_env(),
                    reset_base_url=ACCOUNT_RESET_URL,
                )
    return ACCOUNT_SERVICE


def request_cookie_value(handler, name):
    raw_cookie = handler.headers.get("Cookie", "")
    if not raw_cookie:
        return ""
    try:
        cookie = SimpleCookie()
        cookie.load(raw_cookie)
        morsel = cookie.get(name)
        return morsel.value if morsel is not None else ""
    except (CookieError, KeyError, ValueError):
        return ""


def request_cookie_token(handler):
    return request_cookie_value(handler, "cs2_dashboard")


_LOCAL_ADMIN_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_PROXY_REQUEST_HEADERS = (
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Port",
    "X-Forwarded-Proto",
    "X-Real-IP",
    "Via",
)


def is_direct_local_request(handler):
    """Distinguish a browser on this Mac from traffic crossing the SSH proxy.

    The reverse tunnel connects to the dashboard from a loopback socket too,
    so ``client_address`` alone is not a security boundary.  Public nginx
    always supplies a public Host plus forwarding headers; the local console
    requires the actual loopback Host/port and rejects every proxy marker.
    """

    peer = getattr(handler, "client_address", ("",))[0]
    try:
        if not ipaddress.ip_address(str(peer)).is_loopback:
            return False
    except ValueError:
        return False
    if any(handler.headers.get_all(name) is not None for name in _PROXY_REQUEST_HEADERS):
        return False

    host_headers = handler.headers.get_all("Host") or []
    if len(host_headers) != 1:
        return False
    raw_host = str(host_headers[0]).strip()
    if not raw_host or any(character in raw_host for character in "/?#@"):
        return False
    try:
        parsed_host = urlparse(f"//{raw_host}")
        hostname = (parsed_host.hostname or "").rstrip(".").casefold()
        request_port = parsed_host.port
    except ValueError:
        return False
    if hostname not in _LOCAL_ADMIN_HOSTS:
        return False

    try:
        server_port = int(handler.server.server_address[1])
    except (AttributeError, IndexError, TypeError, ValueError):
        server_port = PORT
    if request_port is None:
        return server_port in {80, 443}
    return request_port == server_port


def account_request_metadata(handler):
    """Return bounded session metadata; forwarded hints are never auth input."""

    peer = getattr(handler, "client_address", ("",))[0]
    try:
        peer_address = ipaddress.ip_address(str(peer))
        ip_address = peer_address.compressed
    except ValueError:
        peer_address = None
        ip_address = None

    forwarded_request = False
    if peer_address is not None and peer_address.is_loopback:
        # nginx overwrites X-Real-IP with its direct client.  Do not trust the
        # first X-Forwarded-For element: clients can prepend a spoofed value.
        for candidate in [handler.headers.get("X-Real-IP", "")]:
            try:
                candidate_address = ipaddress.ip_address(str(candidate).strip())
            except ValueError:
                continue
            ip_address = candidate_address.compressed
            forwarded_request = True
            break

    country_code = None
    if forwarded_request and ACCOUNT_TRUST_CF_COUNTRY:
        raw_country = str(handler.headers.get("CF-IPCountry", "")).strip().upper()
        if re.fullmatch(r"[A-Z]{2}", raw_country) and raw_country not in {"XX"}:
            country_code = raw_country
    return {
        "ip_address": ip_address,
        "country_code": country_code,
        "user_agent": str(handler.headers.get("User-Agent", ""))[:512],
    }


def current_account_session(handler, *, require_csrf=False):
    session_token = request_cookie_value(handler, ACCOUNT_SESSION_COOKIE)
    csrf_token = handler.headers.get("X-CSRF-Token", "") if require_csrf else None
    cache_key = (session_token, csrf_token if require_csrf else None, require_csrf)
    cached = getattr(handler, "_account_session_cache", None)
    if cached and cached[0] == cache_key:
        return cached[1]
    if not session_token:
        result = None
    else:
        try:
            result = get_account_service().validate_session(
                session_token,
                csrf_token=csrf_token,
                require_csrf=require_csrf,
                **account_request_metadata(handler),
            )
        except (account_backend.AuthenticationError, account_backend.AuthorizationError):
            result = None
    try:
        handler._account_session_cache = (cache_key, result)
    except AttributeError:
        pass
    return result


def is_legacy_dashboard_authorized(handler, query):
    token = query.get("token", [""])[0]
    header_token = handler.headers.get("X-Dashboard-Token", "")
    cookie_token = request_cookie_token(handler)
    return bool(DASHBOARD_TOKEN) and (
        secrets.compare_digest(str(token), DASHBOARD_TOKEN)
        or secrets.compare_digest(str(header_token), DASHBOARD_TOKEN)
        or secrets.compare_digest(str(cookie_token), DASHBOARD_TOKEN)
    )


def is_authorized(handler, query):
    return is_legacy_dashboard_authorized(handler, query) or bool(
        current_account_session(handler)
    )


def is_admin_authorized(handler, query):
    if is_legacy_dashboard_authorized(handler, query):
        return True
    principal = current_account_session(handler)
    return bool(principal and principal["user"].get("role") == "admin")


def account_cookie_headers(session=None, *, clear=False):
    secure = "; Secure" if ACCOUNT_COOKIE_SECURE else ""
    if clear:
        return [
            (
                "Set-Cookie",
                (
                    f"{ACCOUNT_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; "
                    f"SameSite=Strict{secure}"
                ),
            ),
            (
                "Set-Cookie",
                f"{ACCOUNT_CSRF_COOKIE}=; Path=/; Max-Age=0; SameSite=Strict{secure}",
            ),
        ]
    if not session:
        raise ValueError("session is required")
    max_age = 30 * 24 * 60 * 60
    expires_at = parse_iso_timestamp(session.get("expires_at"))
    if expires_at is not None:
        max_age = max(60, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    return [
        (
            "Set-Cookie",
            (
                f"{ACCOUNT_SESSION_COOKIE}={session['session_token']}; Path=/; "
                f"Max-Age={max_age}; HttpOnly; SameSite=Strict{secure}"
            ),
        ),
        (
            "Set-Cookie",
            (
                f"{ACCOUNT_CSRF_COOKIE}={session['csrf_token']}; Path=/; "
                f"Max-Age={max_age}; SameSite=Strict{secure}"
            ),
        ),
    ]


def account_request_violation(handler, expected_action, *, require_csrf=False):
    """Apply one consistent browser-origin/JSON/CSRF policy to account writes."""

    content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return 415, "Content-Type must be application/json"
    if handler.headers.get("Transfer-Encoding"):
        return 400, "chunked request bodies are not accepted"
    if not secrets.compare_digest(
        handler.headers.get("X-CS2-Action", ""), expected_action
    ):
        return 403, "missing action confirmation"
    fetch_site = handler.headers.get("Sec-Fetch-Site", "").strip().lower()
    if fetch_site not in {"", "none", "same-origin"}:
        return 403, "cross-site account requests are not allowed"

    origin = handler.headers.get("Origin", "").strip()
    if origin:
        parsed_origin = urlparse(origin)
        configured = urlparse(ACCOUNT_PUBLIC_URL)
        request_host = handler.headers.get("Host", "").strip().casefold()
        allowed_origins = set()
        if configured.scheme in {"http", "https"} and configured.netloc:
            allowed_origins.add(
                (configured.scheme, configured.netloc.casefold())
            )
        request_hostname = urlparse(f"//{request_host}").hostname if request_host else None
        if request_hostname in {"localhost", "127.0.0.1", "::1"}:
            allowed_origins.add(("http", request_host))
            allowed_origins.add(("https", request_host))
        if (
            parsed_origin.scheme not in {"http", "https"}
            or (
                parsed_origin.scheme,
                parsed_origin.netloc.casefold(),
            )
            not in allowed_origins
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.params
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            return 403, "request Origin does not match this site"
    if require_csrf and current_account_session(handler, require_csrf=True) is None:
        return 403, "invalid CSRF token"
    return None


def admin_users_request_violation(handler):
    """Strict mutation policy for the localhost-only user console."""

    if not is_direct_local_request(handler):
        return 404, "not found"
    if handler.headers.get("Transfer-Encoding"):
        return 400, "streamed request bodies are not accepted"
    content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip()
    if content_type.casefold() != "application/json":
        return 415, "Content-Type must be application/json"
    if handler.headers.get("X-CS2-Action", "") != "admin-user-management":
        return 403, "missing administrative action confirmation"
    fetch_site = handler.headers.get("Sec-Fetch-Site", "").strip().casefold()
    if fetch_site not in {"", "none", "same-origin"}:
        return 403, "cross-site administrative request rejected"

    origin = handler.headers.get("Origin", "").strip()
    raw_host = handler.headers.get("Host", "").strip().casefold()
    try:
        parsed_origin = urlparse(origin)
    except ValueError:
        parsed_origin = None
    if (
        parsed_origin is None
        or parsed_origin.scheme not in {"http", "https"}
        or parsed_origin.netloc.casefold() != raw_host
        or parsed_origin.username is not None
        or parsed_origin.password is not None
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.params
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        return 403, "request Origin does not match localhost"

    # This is an owner console, not a normal account-admin surface.  Requiring
    # the master dashboard secret keeps an accidentally promoted web account
    # from managing users, even when it visits localhost on this Mac.
    if not is_legacy_dashboard_authorized(handler, {}):
        return 403, "local owner authorization is required"
    return None


def read_json_request(handler, *, limit=ACCOUNT_BODY_LIMIT):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise ValueError("invalid Content-Length") from exc
    if length <= 0 or length > limit:
        raise ValueError("invalid request body")
    try:
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("body must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    return payload


def auth_rate_limited(handler, bucket, identity, *, limit, window_seconds):
    peer = getattr(handler, "client_address", ("unknown",))[0]
    try:
        peer_address = ipaddress.ip_address(str(peer))
    except ValueError:
        peer_address = None
    if peer_address is not None and peer_address.is_loopback:
        forwarded = handler.headers.get("X-Real-IP", "").strip()
        try:
            if forwarded:
                peer = str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    safe_identity = str(identity or "").strip().casefold()[:254]
    identity_key = (str(bucket), str(peer), "identity", safe_identity)
    ip_key = (str(bucket), str(peer), "ip", "")
    now = time.monotonic()
    cutoff = now - float(window_seconds)
    with AUTH_RATE_LOCK:
        attempts = [
            stamp for stamp in AUTH_RATE_BUCKETS[identity_key] if stamp >= cutoff
        ]
        ip_attempts = [stamp for stamp in AUTH_RATE_BUCKETS[ip_key] if stamp >= cutoff]
        limited = len(attempts) >= int(limit) or len(ip_attempts) >= max(
            int(limit) * 10, 20
        )
        if not limited:
            attempts.append(now)
            ip_attempts.append(now)
        AUTH_RATE_BUCKETS[identity_key] = attempts
        AUTH_RATE_BUCKETS[ip_key] = ip_attempts
        if len(AUTH_RATE_BUCKETS) > 2_048:
            for old_key in list(AUTH_RATE_BUCKETS)[:512]:
                if not AUTH_RATE_BUCKETS[old_key] or AUTH_RATE_BUCKETS[old_key][-1] < cutoff:
                    AUTH_RATE_BUCKETS.pop(old_key, None)
            while len(AUTH_RATE_BUCKETS) > 2_048:
                AUTH_RATE_BUCKETS.pop(next(iter(AUTH_RATE_BUCKETS)))
    return limited


def account_error_response(handler, exc):
    payload = {
        "error": str(exc),
        "error_code": getattr(exc, "code", "account_error"),
    }
    if isinstance(exc, (account_backend.ValidationError, ValueError)):
        json_response(handler, 400, payload)
    elif isinstance(exc, account_backend.ConflictError):
        json_response(handler, 409, payload)
    elif isinstance(exc, account_backend.AccountAccessError):
        json_response(handler, 403, payload)
    elif isinstance(exc, account_backend.AuthenticationError):
        json_response(handler, 401, payload)
    elif isinstance(exc, account_backend.AuthorizationError):
        json_response(handler, 403, payload)
    elif isinstance(exc, account_backend.NotFoundError):
        json_response(handler, 404, payload)
    else:
        json_response(
            handler,
            503,
            {
                "error": "account service is unavailable",
                "error_code": "service_unavailable",
            },
        )


def _admin_session_payload(session):
    active = bool(session.get("active", session.get("is_active", False)))
    ip_value = session.get("ip_address") or session.get("ip")
    country_value = session.get("country_code") or session.get("country")
    return {
        "id": session.get("id"),
        "created_at": session.get("created_at"),
        "expires_at": session.get("expires_at"),
        "last_seen_at": session.get("last_seen_at"),
        "revoked_at": session.get("revoked_at"),
        "active": active,
        "is_active": active,
        "ip_address": ip_value,
        "ip": ip_value,
        "country_code": country_value,
        "country": country_value,
        "user_agent": session.get("user_agent"),
    }


def _admin_user_payload(user):
    sessions = [
        _admin_session_payload(session)
        for session in (user.get("sessions") or user.get("recent_sessions") or [])[:8]
        if isinstance(session, dict)
    ]
    last_ip = user.get("last_ip_address") or user.get("last_ip")
    last_country = user.get("last_country_code") or user.get("last_country")
    last_agent = user.get("last_user_agent") or user.get("user_agent")
    current_ip = user.get("current_ip_address") or user.get("current_ip")
    current_country = user.get("current_country_code") or user.get("current_country")
    current_agent = user.get("current_user_agent")
    online = bool(user.get("online", user.get("is_online", False)))
    return {
        "id": user.get("id"),
        "email": user.get("email"),
        "display_name": user.get("display_name"),
        "role": user.get("role"),
        "access_state": user.get("access_state"),
        "access_reason": user.get("access_reason"),
        "access_changed_at": user.get("access_changed_at"),
        "approved_at": user.get("approved_at"),
        "blocked_at": user.get("blocked_at"),
        "deleted_at": user.get("deleted_at"),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        "last_login_at": user.get("last_login_at"),
        "last_seen_at": user.get("last_seen_at"),
        "revision": user.get("revision"),
        "online": online,
        "is_online": online,
        "active_session_count": user.get("active_session_count", 0),
        "active_sessions": user.get("active_session_count", 0),
        "total_session_count": user.get(
            "total_session_count", user.get("session_count", len(sessions))
        ),
        "last_ip_address": last_ip,
        "last_ip": last_ip,
        "current_ip": current_ip if online else None,
        "last_country_code": last_country,
        "last_country": last_country,
        "current_country": current_country if online else None,
        "last_user_agent": last_agent,
        "current_user_agent": current_agent if online else None,
        "registration_ip_address": user.get("registration_ip_address"),
        "registration_ip": user.get("registration_ip_address"),
        "registration_country_code": user.get("registration_country_code"),
        "registration_country": user.get("registration_country_code"),
        "registration_user_agent": user.get("registration_user_agent"),
        "inventory_positions": user.get("inventory_positions", 0),
        "active_inventory_items": user.get("active_inventory_items", 0),
        "password_changed_at": user.get("password_changed_at"),
        "password_reset_sent_at": user.get("password_reset_sent_at"),
        "sessions": sessions,
        "recent_sessions": sessions,
        "can_manage": user.get("access_state") != "deleted",
        "is_self": False,
    }


def build_admin_users_payload(query):
    allowed = {"q", "status", "role", "online", "limit", "offset"}
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise account_backend.ValidationError(
            f"unexpected query fields: {', '.join(unknown)}"
        )
    try:
        limit = min(max(int(query.get("limit", ["200"])[0]), 1), 200)
        offset = max(int(query.get("offset", ["0"])[0]), 0)
    except ValueError as exc:
        raise account_backend.ValidationError(
            "limit and offset must be integers"
        ) from exc
    state = str(query.get("status", [""])[0]).strip().lower() or None
    if state == "all":
        state = None
    role = str(query.get("role", [""])[0]).strip().lower() or None
    online = str(query.get("online", ["0"])[0]).strip().lower() in {
        "1",
        "true",
        "yes",
    }
    service = get_account_service()
    result = service.admin_list_users(
        trusted_local_admin=True,
        query=query.get("q", [""])[0],
        access_state=state,
        role=role,
        online_only=online,
        limit=limit,
        offset=offset,
    )
    users = [_admin_user_payload(user) for user in result.get("users", [])]
    summary = result.get("summary") or {
        "total": result.get("total", len(users)),
        "online": result.get("online_count", sum(user["online"] for user in users)),
    }
    return {
        "users": users,
        "summary": summary,
        "total": result.get("total", len(users)),
        "limit": result.get("limit", limit),
        "offset": result.get("offset", offset),
        "privacy": {
            "online_window_seconds": 300,
            "country_source": (
                "trusted_cf_header" if ACCOUNT_TRUST_CF_COUNTRY else "unavailable"
            ),
            "note": "No passwords, session tokens, paths or referrers are collected.",
        },
    }


def update_admin_user_from_payload(payload):
    allowed = {"action", "user_id", "expected_revision", "confirmation_email"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise account_backend.ValidationError(
            f"unexpected fields: {', '.join(unknown)}"
        )
    action = str(payload.get("action", "")).strip().lower()
    if action not in admin_users_ui.ADMIN_USERS_API_CONTRACT["actions"]:
        raise account_backend.ValidationError("unknown user administration action")
    user_id = payload.get("user_id")
    expected_revision = payload.get("expected_revision")
    confirmation_email = str(payload.get("confirmation_email", "")).strip()
    if user_id in (None, "") or expected_revision in (None, ""):
        raise account_backend.ValidationError(
            "user_id and expected_revision are required"
        )

    service = get_account_service()
    if action == "delete":
        current_user = service.admin_get_user(user_id, trusted_local_admin=True)
        if confirmation_email.casefold() != str(current_user.get("email", "")).casefold():
            raise account_backend.ValidationError(
                "confirmation_email must exactly match the current account email"
            )
    elif confirmation_email:
        raise account_backend.ValidationError(
            "confirmation_email is accepted only for delete"
        )
    common = {
        "trusted_local_admin": True,
        "expected_revision": expected_revision,
    }
    extra = {}
    if action == "approve":
        service.admin_approve_user(user_id, **common)
    elif action == "block":
        service.admin_block_user(user_id, **common)
    elif action in {"grant_admin", "revoke_admin"}:
        changed = service.set_user_role(
            target_user_id=user_id,
            role="admin" if action == "grant_admin" else "member",
            **common,
        )
        # The backend changes the role and revokes every bearer session in one
        # transaction so no stale principal can retain or gain privileges.
        extra = {"revoked_sessions": int(changed.get("revoked_sessions", 0))}
    elif action == "revoke_sessions":
        extra = service.admin_revoke_sessions(user_id, **common)
    elif action == "send_password_reset":
        extra = service.admin_issue_password_reset(user_id, deliver=True, **common)
        if extra.get("delivery_state") == "sent":
            # A delivered reset secret does not need to be exposed a second
            # time to browser JavaScript.  The local fallback link remains
            # available only when mail was not configured or delivery failed.
            extra.pop("reset_url", None)
    else:
        service.admin_soft_delete_user(user_id, **common)
    user = service.admin_get_user(user_id, trusted_local_admin=True)
    return {"ok": True, "action": action, "user": _admin_user_payload(user), **extra}


def redirect_response(handler, location, *, extra_headers=None):
    handler.send_response(303)
    handler.send_header("Location", location)
    handler.send_header("Cache-Control", "no-store")
    send_security_headers(handler)
    for name, value in extra_headers or ():
        handler.send_header(name, value)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def begin_browser_session(handler, parsed):
    """Exchange a tokenized browser URL for a private HttpOnly cookie."""

    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    cookie = (
        f"cs2_dashboard={DASHBOARD_TOKEN}; Path=/; Max-Age=2592000; "
        "HttpOnly; SameSite=Strict"
    )
    if forwarded_proto == "https":
        cookie += "; Secure"

    clean_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "token"
    ]
    location = parsed.path or "/"
    if clean_query:
        location += "?" + urlencode(clean_query)

    handler.send_response(303)
    handler.send_header("Location", location)
    handler.send_header("Set-Cookie", cookie)
    handler.send_header("Cache-Control", "no-store")
    send_security_headers(handler)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def render_private_html(template, locale=web_i18n.DEFAULT_LOCALE):
    # Browser API calls authenticate through the HttpOnly cookie.  Keeping the
    # token out of page source also keeps it out of copied links and JS errors.
    rendered = (
        template
        .replace("?token=__DASHBOARD_TOKEN__", "")
        .replace("&token=__DASHBOARD_TOKEN__", "")
        .replace("__DASHBOARD_TOKEN__", "")
    )
    if "</style>" in rendered:
        rendered = rendered.replace("</style>", f"{PRIVATE_DESIGN_CSS}</style>", 1)
    if "</body>" in rendered:
        rendered = rendered.replace("</body>", f"{PRIVATE_ENHANCEMENT_SCRIPT}</body>", 1)
    rendered = simplify_private_interface(
        rendered,
        locale,
        page="dashboard" if 'class="page-dashboard"' in rendered else None,
    )
    return web_i18n.render_document(rendered, locale)


def _lean_navigation(locale, *, include_system=False):
    selected = web_i18n.normalize_locale(locale)
    labels = {
        "ru": {
            "deals": "Сделки",
            "evaluate": "Проверить лот",
            "strategy": "Стратегия",
            "catalog": "Рынок",
            "inventory": "Инвентарь",
            "more": "Ещё",
            "knives": "Ножи",
            "history": "История цен",
            "system": "Система",
        },
        "en": {
            "deals": "Deals",
            "evaluate": "Check listing",
            "strategy": "Strategy",
            "catalog": "Market",
            "inventory": "Inventory",
            "more": "More",
            "knives": "Knives",
            "history": "Price history",
            "system": "System",
        },
    }[selected]
    primary = (
        ("/deals", "deals"),
        ("/deal", "evaluate"),
        ("/strategy", "strategy"),
        ("/catalog", "catalog"),
        ("/account", "inventory"),
    )
    links = "".join(
        '<a class="nav-link lean-nav-link" '
        f'data-nav-path="{path}" data-lean-label="{key}" href="{path}">'
        f"{html.escape(labels[key])}</a>"
        for path, key in primary
    )
    # Knives and price sources live inside Market.  Keeping them out of the
    # primary shell avoids a second navigation layer that repeats the same
    # workspace under three different names.
    secondary = []
    if include_system:
        secondary.append(("/dashboard", "system"))
    secondary_links = "".join(
        '<a class="nav-link lean-nav-link" '
        f'data-nav-path="{path}" data-lean-label="{key}" href="{path}">'
        f"{html.escape(labels[key])}</a>"
        for path, key in secondary
    )
    more = (
        '<details class="lean-more">'
        f'<summary data-lean-label="more">{html.escape(labels["more"])}</summary>'
        f'<div class="lean-more-menu">{secondary_links}</div>'
        "</details>"
        if secondary_links
        else ""
    )
    return (
        '<nav class="site-nav lean-nav" aria-label="Primary navigation">'
        f"{links}"
        f"{more}</nav>"
    )


def simplify_private_interface(document, locale=web_i18n.DEFAULT_LOCALE, *, page=None):
    """Apply the same compact shell without deleting any backend capability."""

    rendered = str(document)
    for old_brand in ("CS2 Intelligence", "CS2 Market Lab", "CSSkinsPrediction"):
        rendered = rendered.replace(old_brand, "CS2 Market")
    include_system = page == "dashboard" or 'class="page-dashboard"' in rendered
    header_nav = re.compile(
        r"(?is)(<header\b[^>]*>.*?)(<nav\b[^>]*>.*?</nav>)"
    )
    rendered, count = header_nav.subn(
        lambda match: f"{match.group(1)}{_lean_navigation(locale, include_system=include_system)}",
        rendered,
        count=1,
    )
    if count == 0:
        return rendered
    if page and f"page-{page}" not in rendered:
        rendered = re.sub(
            r"<body(?P<attrs>[^>]*)>",
            lambda match: (
                f'<body{match.group("attrs")} class="page-{html.escape(page)}">'
                if "class=" not in match.group("attrs")
                else re.sub(
                    r'class=(["\'])(.*?)\1',
                    lambda class_match: (
                        f'class={class_match.group(1)}{class_match.group(2)} '
                        f'page-{html.escape(page)}{class_match.group(1)}'
                    ),
                    match.group(0),
                    count=1,
                )
            ),
            rendered,
            count=1,
            flags=re.IGNORECASE,
        )
    if 'id="cs2-lean-interface"' not in rendered and "</head>" in rendered:
        rendered = rendered.replace("</head>", f"{LEAN_INTERFACE_CSS}</head>", 1)
    if 'id="cs2-lean-interface-script"' not in rendered and "</body>" in rendered:
        rendered = rendered.replace("</body>", f"{LEAN_INTERFACE_SCRIPT}</body>", 1)
    return rendered


def inject_knives_navigation(document, locale=web_i18n.DEFAULT_LOCALE):
    """Add the read-only knives workspace without rewriting legacy templates."""

    rendered = str(document)
    if 'href="/knives"' in rendered:
        return rendered
    label = "Knives" if web_i18n.normalize_locale(locale) == "en" else "Ножи"
    private_account_link = (
        '<a class="nav-link" data-nav-path="/account" '
        'href="/account">Инвентарь</a>'
    )
    if private_account_link in rendered:
        knives_link = (
            '<a class="nav-link" data-nav-path="/knives" data-i18n="navKnives" '
            f'href="/knives">{label}</a>'
        )
        return rendered.replace(
            private_account_link, f"{knives_link}\n        {private_account_link}"
        )
    account_link = (
        '<a class="active" href="/account" '
        'data-i18n="navInventory">Мой инвентарь</a>'
    )
    if account_link in rendered:
        return rendered.replace(
            account_link,
            f'<a data-i18n="navKnives" href="/knives">{label}</a>{account_link}',
        )
    return rendered


def inject_catalog_navigation(document, locale=web_i18n.DEFAULT_LOCALE):
    """Expose the fast all-item catalogue across existing private pages."""

    rendered = str(document)
    if 'href="/catalog"' in rendered:
        return rendered
    label = "Catalogue" if web_i18n.normalize_locale(locale) == "en" else "Каталог"
    private_knives_link = re.search(
        r'<a\b(?=[^>]*\bdata-nav-path="/knives")[^>]*>.*?</a>',
        rendered,
        flags=re.DOTALL,
    )
    if private_knives_link:
        link_class = (
            ' class="nav-link"'
            if 'class="nav-link"' in private_knives_link.group(0)
            else ""
        )
        catalog_link = (
            f'<a{link_class} data-nav-path="/catalog" data-i18n="navCatalog" '
            f'href="/catalog">{label}</a>'
        )
        return rendered.replace(
            private_knives_link.group(0),
            f"{catalog_link}\n        {private_knives_link.group(0)}",
            1,
        )
    plain_knives_link = re.search(r'<a href="/knives">.*?</a>', rendered)
    if plain_knives_link:
        return rendered.replace(
            plain_knives_link.group(0),
            f'<a data-i18n="navCatalog" href="/catalog">{label}</a>'
            f'{plain_knives_link.group(0)}',
            1,
        )
    return rendered


def inject_local_users_navigation(document, locale=web_i18n.DEFAULT_LOCALE):
    """Add the owner console link only to pages rendered on direct localhost."""

    rendered = str(document)
    if 'href="/admin/users"' in rendered:
        return rendered
    label = (
        "Users"
        if web_i18n.normalize_locale(locale) == "en"
        else "Пользователи"
    )
    link = (
        '<a class="nav-link lean-nav-link" data-nav-path="/admin/users" '
        f'href="/admin/users">{html.escape(label)}</a>'
    )
    menu_marker = '<div class="lean-more-menu">'
    if menu_marker in rendered:
        return rendered.replace(menu_marker, f"{menu_marker}{link}", 1)
    nav_close = rendered.find("</nav>")
    if nav_close >= 0:
        return f"{rendered[:nav_close]}{link}{rendered[nav_close:]}"
    return rendered


def inject_market_history_navigation(document, locale=web_i18n.DEFAULT_LOCALE):
    """Expose provenance-aware multi-market data across private workspaces."""

    rendered = str(document)
    if 'href="/market-history"' in rendered:
        return rendered
    label = (
        "Market history"
        if web_i18n.normalize_locale(locale) == "en"
        else "История рынков"
    )
    catalog_link = re.search(
        r'<a\b(?=[^>]*\bhref="/catalog")[^>]*>.*?</a>',
        rendered,
        flags=re.DOTALL,
    )
    if catalog_link:
        link_class = (
            ' class="nav-link"'
            if 'class="nav-link"' in catalog_link.group(0)
            else ""
        )
        history_link = (
            f'<a{link_class} data-nav-path="/market-history" '
            f'data-i18n="navHistory" href="/market-history">{label}</a>'
        )
        return rendered.replace(
            catalog_link.group(0),
            f"{catalog_link.group(0)}\n        {history_link}",
            1,
        )
    return rendered


def standalone_html_response(handler, document, *, status=200):
    body = document.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    send_security_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        pass


def local_owner_document(handler, document, locale):
    """Expose the user-console tab throughout the local owner workspace."""

    if is_direct_local_request(handler) and is_legacy_dashboard_authorized(handler, {}):
        return inject_local_users_navigation(document, locale)
    return document


def auth_html_response(handler, mode):
    locale = web_i18n.request_locale(handler)
    document = account_ui.render_auth_page(mode, locale).replace(
        "CSSkinsPrediction", "CS2 Market"
    )
    standalone_html_response(handler, document)


def account_html_response(handler):
    locale = web_i18n.request_locale(handler)
    document = simplify_private_interface(
        account_ui.render_account_page(locale), locale, page="account"
    )
    document = local_owner_document(handler, document, locale)
    standalone_html_response(handler, document)


def admin_users_html_response(handler):
    locale = web_i18n.request_locale(handler)
    standalone_html_response(
        handler,
        admin_users_ui.render_admin_users_page(locale),
    )


def knives_html_response(handler):
    locale = web_i18n.request_locale(handler)
    document = simplify_private_interface(
        knives_dashboard.render_knives_page(locale), locale, page="knives"
    )
    document = local_owner_document(handler, document, locale)
    standalone_html_response(handler, document)


def catalog_html_response(handler):
    locale = web_i18n.request_locale(handler)
    document = simplify_private_interface(
        catalog_browser.render_catalog_page(locale), locale, page="catalog"
    )
    document = local_owner_document(handler, document, locale)
    standalone_html_response(handler, document)


def market_history_html_response(handler):
    locale = web_i18n.request_locale(handler)
    document = simplify_private_interface(
        market_history_dashboard.render_market_history_page(locale),
        locale,
        page="market-history",
    )
    document = local_owner_document(handler, document, locale)
    standalone_html_response(handler, document)


def html_response(handler, token=None):
    locale = web_i18n.request_locale(handler)
    html = (
        render_private_html(HTML, locale)
        .replace("__LIVE_REFRESH_MS__", str(LIVE_REFRESH_MS))
    )
    html = local_owner_document(handler, html, locale)
    body = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    send_security_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        pass


def deal_html_response(handler, token=None):
    locale = web_i18n.request_locale(handler)
    html = local_owner_document(
        handler, render_private_html(DEAL_HTML, locale), locale
    )
    body = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    send_security_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        pass


def build_neural_template():
    """Build the Nova page from canonical comparison copy before localisation."""

    return (
        DEAL_HTML
        .replace("CSFloat Model Comparison", "Nova Neural AI")
        .replace("three-model comparison", "neural price model · q15 / q50 / q85")
        .replace("MODEL COMPARISON", "NOVA NEURAL")
        .replace("Compare three estimates before you decide.", "Ask Nova for a focused neural estimate.")
        .replace(
            "Almaz, Igor and Nova stay visible side by side. The headline result uses the lowest available estimate instead of silently choosing the most optimistic ROI.",
            "Nova returns an ordered q15 / q50 / q85 range so uncertainty stays visible next to the expected sale value.",
        )
        .replace("<li><span>3</span>Compare</li>", "<li><span>3</span>Predict</li>")
        .replace("Compare models", "Predict with Nova")
        .replace("Conservative outcome", "Nova prediction")
        .replace("Conservative Estimate", "Expected Sale")
        .replace("/api/predict-deal?", "/api/predict-neural?")
    )


def neural_html_response(handler, token=None):
    locale = web_i18n.request_locale(handler)
    # Localisation is deliberately the final step, so translated copy can
    # never make structural Nova replacements stop matching.
    html = render_private_html(build_neural_template(), locale)
    body = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    send_security_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        pass


def deals_html_response(handler, token=None):
    locale = web_i18n.request_locale(handler)
    html = local_owner_document(
        handler, render_private_html(DEALS_HTML, locale), locale
    )
    body = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    send_security_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        pass


def strategy_html_response(handler, token=None):
    locale = web_i18n.request_locale(handler)
    body = local_owner_document(
        handler, render_private_html(STRATEGY_HTML, locale), locale
    ).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    send_security_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        pass


def escape_html(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def public_int(value):
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def public_msk(value, locale=web_i18n.DEFAULT_LOCALE):
    parsed = parse_iso_timestamp(value)
    if not parsed:
        return web_i18n.translate("waiting for data", locale)
    return parsed.astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M MSK")


def public_row(title, subtitle, value):
    return (
        '<div class="row">'
        f'<div><div class="name">{escape_html(title)}</div><div class="sub">{escape_html(subtitle)}</div></div>'
        f'<div class="value">{escape_html(value)}</div>'
        '</div>'
    )


def public_tile(row, locale=web_i18n.DEFAULT_LOCALE):
    name = row.get("name") or web_i18n.translate("CS2 skin", locale)
    image_url = row.get("item_icon_url") or ""
    price_cents = row.get("price_cents")
    img = (
        f'<img src="{escape_html(image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">'
        if image_url
        else '<div style="height:132px"></div>'
    )
    return (
        '<div class="skin-tile">'
        f'{img}'
        f'<div class="tile-title">{escape_html(name)} · {escape_html(public_money(price_cents))}</div>'
        '</div>'
    )


def public_money(cents, locale=web_i18n.DEFAULT_LOCALE):
    try:
        return f"${int(cents) / 100:.2f}"
    except (TypeError, ValueError):
        return web_i18n.translate("unknown", locale)


def build_public_payload():
    cached = cache_get("public_payload", 60)
    if cached is not None:
        return cached

    payload = {
        "overview": {
            "sales_count": 0,
            "skin_variant_count": 0,
            "latest_fetched_at": None,
            "sticker_rows": 0,
        },
        "deal_count": 0,
        "top_weapons": [],
        "top_stickers": [],
        "recent_sales": [],
    }

    if not Path(DB_PATH).exists():
        return cache_set("public_payload", payload)

    static_payload = get_dashboard_static_payload()
    payload["top_weapons"] = (static_payload.get("top_weapons") or [])[:6]
    payload["top_stickers"] = (static_payload.get("top_stickers") or [])[:6]

    try:
        with connect_db() as conn:
            payload["overview"] = fetch_overview(conn)
            payload["recent_sales"] = fetch_recent_sales(conn, 4)
            if table_exists(conn, "model_deal_predictions"):
                payload["deal_count"] = conn.execute("""
                    SELECT COUNT(DISTINCT listing_id)
                    FROM model_deal_predictions
                    WHERE verdict IN ('good', 'watch')
                """).fetchone()[0]
    except sqlite3.Error:
        pass

    return cache_set("public_payload", payload)


def public_html_response(handler):
    locale = web_i18n.request_locale(handler)
    payload = build_public_payload()
    overview = payload["overview"]

    recent_rows = []
    recent_names = set()
    for row in payload["recent_sales"]:
        name = str(row.get("name") or "").strip().casefold()
        if name and name in recent_names:
            continue
        if name:
            recent_names.add(name)
        recent_rows.append(row)
        if len(recent_rows) >= 4:
            break
    recent_tiles = "".join(
        public_tile(row, locale) for row in recent_rows
    )
    if not recent_tiles:
        waiting = escape_html(web_i18n.translate("Waiting for data", locale))
        recent_tiles = (
            '<div class="skin-tile wide"><div style="height:132px"></div>'
            f'<div class="tile-title">{waiting}</div></div>'
        )

    listings_label = web_i18n.translate("listings", locale)
    skins_label = web_i18n.translate("skins", locale)
    top_weapons = "".join(
        public_row(
            row.get("weapon_name") or "Unknown",
            f"{public_int(row.get('listing_count'))} {listings_label} · {public_int(row.get('skin_count'))} {skins_label}",
            public_int(row.get("sticker_count")),
        )
        for row in payload["top_weapons"]
    ) or public_row(
        web_i18n.translate("Waiting for data", locale),
        web_i18n.translate("parser has not filled weapon stats yet", locale),
        "0",
    )

    top_stickers = "".join(
        public_row(
            row.get("sticker_name") or "Unknown",
            f"{public_int(row.get('listing_count'))} {listings_label} · {public_int(row.get('skin_count'))} {skins_label}",
            public_int(row.get("sticker_count")),
        )
        for row in payload["top_stickers"]
    ) or public_row(
        web_i18n.translate("Waiting for data", locale),
        web_i18n.translate("parser has not filled sticker stats yet", locale),
        "0",
    )

    html_body = (
        PUBLIC_HTML
        .replace(
            "__LATEST_FETCHED__",
            escape_html(public_msk(overview.get("latest_fetched_at"), locale)),
        )
        .replace("__RECENT_TILES__", recent_tiles)
        .replace("__SALES_COUNT__", escape_html(public_int(overview.get("sales_count"))))
        .replace("__SKIN_VARIANT_COUNT__", escape_html(public_int(overview.get("skin_variant_count"))))
        .replace("__STICKER_ROWS__", escape_html(public_int(overview.get("sticker_rows"))))
        .replace("__DEAL_COUNT__", escape_html(public_int(payload.get("deal_count"))))
        .replace("__TOP_WEAPONS__", top_weapons)
        .replace("__TOP_STICKERS__", top_stickers)
        .replace("CSSkinsPrediction", "CS2 Market")
    )
    html_body = web_i18n.render_document(html_body, locale)

    body = html_body.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    send_security_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        pass


def create_db_snapshot():
    db_path = Path(DB_PATH)
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {DB_PATH}")

    temp_dir = Path(tempfile.gettempdir())
    free_bytes = shutil.disk_usage(temp_dir).free
    required_bytes = db_path.stat().st_size + 512 * 1024 * 1024
    if free_bytes < required_bytes:
        raise OSError(
            f"not enough free disk space for a database snapshot: "
            f"need {required_bytes:,} bytes, have {free_bytes:,}"
        )

    temp_file = tempfile.NamedTemporaryFile(
        prefix="csfloat_history_snapshot_",
        suffix=".db",
        delete=False,
    )
    temp_file.close()

    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30) as source:
            with sqlite3.connect(temp_file.name, timeout=30) as destination:
                source.backup(destination)
    except Exception:
        try:
            os.remove(temp_file.name)
        except OSError:
            pass
        raise

    return temp_file.name


def file_response(handler, path, download_name, content_type):
    file_path = Path(path)
    size = file_path.stat().st_size

    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
    handler.send_header("Cache-Control", "no-store")
    send_security_headers(handler)
    handler.send_header("Content-Length", str(size))
    handler.end_headers()

    try:
        with file_path.open("rb") as file:
            while True:
                chunk = file.read(1024 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        pass


def read_json_file(path, default):
    file_path = Path(path)
    if not file_path.exists():
        return default

    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_total_skins():
    data = read_json_file(SKINS_PATH, [])
    if not isinstance(data, list):
        return 0

    seen = set()
    for skin in data:
        if isinstance(skin, str):
            market_hash_name = skin
        elif isinstance(skin, dict):
            market_hash_name = skin.get("market_hash_name")
        else:
            market_hash_name = None

        if market_hash_name:
            seen.add(market_hash_name)

    return len(seen)


def connect_db():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.create_function("extract_tournament", 1, extract_tournament)
    conn.create_function("extract_sticker_type", 1, extract_sticker_type)
    return conn


def build_health_payload():
    model_files = {
        "almaz": (app_config.PROJECT_DIR / "models", "csfloat_price_model.joblib"),
        "igor": (
            app_config.PROJECT_DIR / "IGORMODEL" / "artifacts",
            "price_model_point.cbm",
        ),
        "nova": (
            app_config.PROJECT_DIR / "NEURALMODEL" / "artifacts",
            "nova_price_model.pt",
        ),
        "knives": (
            app_config.PROJECT_DIR / "KNIFEMODEL" / "artifacts",
            "knife_price_model.joblib",
        ),
    }
    model_available = {}
    model_generation_status = {}
    for model, (legacy_dir, filename) in model_files.items():
        try:
            bundle_dir = model_generations.active_or_legacy_bundle_dir(
                model,
                legacy_dir,
                project_dir=app_config.PROJECT_DIR,
                verify_files=False,
            )
            pointer = model_generations.read_active_pointer(
                model, project_dir=app_config.PROJECT_DIR
            )
            model_available[model] = (bundle_dir / filename).is_file()
            model_generation_status[model] = {
                "active_bundle_version": (
                    pointer.get("generation_id") if pointer else None
                ),
                "source": "generation" if pointer else "legacy",
                "error": None,
            }
        except model_generations.GenerationError as exc:
            model_available[model] = False
            model_generation_status[model] = {
                "active_bundle_version": None,
                "source": "unsafe",
                "error": str(exc),
            }
    payload = {
        "ok": False,
        "service": "cs2-dashboard",
        "database": {"path": Path(DB_PATH).name, "available": False},
        "market_history": build_market_history_dashboard_status(),
        "models": model_available,
        "model_generations": model_generation_status,
    }
    if not Path(DB_PATH).is_file():
        payload["error"] = "database file is missing"
        return payload
    try:
        with connect_db() as conn:
            conn.execute("SELECT 1").fetchone()
        payload["database"]["available"] = True
        payload["ok"] = True
    except sqlite3.Error as exc:
        payload["error"] = str(exc)
    return payload


def ensure_dashboard_indexes():
    if not Path(DB_PATH).exists():
        return

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_name ON sales(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_name_sold_at ON sales(name, sold_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_base_name ON sales(base_skin_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_fetched_at ON sales(fetched_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sale_stickers_listing_id ON sale_stickers(listing_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sale_stickers_name ON sale_stickers(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sale_stickers_sticker_id ON sale_stickers(sticker_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sale_stickers_sticker_id_name ON sale_stickers(sticker_id, name)")

        if table_exists(conn, "steam_market_assets"):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_steam_market_assets_name ON steam_market_assets(market_hash_name)")

        conn.commit()


def row_to_dict(row):
    return dict(row) if row else {}


def table_columns(conn, table_name):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def table_exists(conn, table_name):
    row = conn.execute("""
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
    """, (table_name,)).fetchone()
    return row is not None


def optional_column_expr(columns, column_name, alias):
    if column_name in columns:
        return f"{column_name} AS {alias}"

    return f"NULL AS {alias}"


def optional_qualified_column(columns, table_alias, column_name):
    if column_name in columns:
        return f"{table_alias}.{column_name}"

    return "NULL"


def optional_qualified_column_expr(columns, table_alias, column_name, alias):
    return f"{optional_qualified_column(columns, table_alias, column_name)} AS {alias}"


def optional_collection_name_expr(columns, table_alias="s"):
    candidates = [
        f"NULLIF(TRIM({table_alias}.{column_name}), '')"
        for column_name in ("item_collection_name", "item_collection")
        if column_name in columns
    ]
    if len(candidates) > 1:
        value_expr = f"COALESCE({', '.join(candidates)})"
    elif candidates:
        value_expr = candidates[0]
    else:
        value_expr = "NULL"
    return f"{value_expr} AS item_collection_name"


def steam_asset_join(conn, name_expression):
    if not table_exists(conn, "steam_market_assets"):
        return "", "NULL", "NULL"

    columns = table_columns(conn, "steam_market_assets")
    rarity_expr = "sma.rarity_color" if "rarity_color" in columns else "NULL"
    name_color_expr = "sma.name_color" if "name_color" in columns else "NULL"
    return (
        f"LEFT JOIN steam_market_assets sma ON sma.market_hash_name = {name_expression}",
        rarity_expr,
        name_color_expr,
    )


def steam_market_url(market_hash_name):
    if not market_hash_name:
        return None

    from urllib.parse import quote
    return STEAM_MARKET_BASE_URL + quote(market_hash_name, safe="")


def add_market_urls(rows, name_key, url_key):
    for row in rows:
        if not row.get(url_key):
            row[url_key] = steam_market_url(row.get(name_key))

    return rows


def cache_get(key, ttl_seconds):
    with CACHE_LOCK:
        cached = PAYLOAD_CACHE.get(key)
        if not cached:
            return None

        created_at, payload = cached
        if time.time() - created_at > ttl_seconds:
            PAYLOAD_CACHE.pop(key, None)
            return None

        return payload


def cache_set(key, payload):
    with CACHE_LOCK:
        if key not in PAYLOAD_CACHE and len(PAYLOAD_CACHE) >= 512:
            oldest_keys = sorted(PAYLOAD_CACHE, key=lambda item: PAYLOAD_CACHE[item][0])[:128]
            for oldest_key in oldest_keys:
                PAYLOAD_CACHE.pop(oldest_key, None)
        PAYLOAD_CACHE[key] = (time.time(), payload)
    return payload


def cache_get_with_age(key):
    with CACHE_LOCK:
        cached = PAYLOAD_CACHE.get(key)
        if not cached:
            return None, None

        created_at, payload = cached
        return payload, time.time() - created_at


def start_background_refresh(key, refresh_fn):
    with CACHE_LOCK:
        if key in BACKGROUND_REFRESHING_KEYS:
            return
        BACKGROUND_REFRESHING_KEYS.add(key)

    def runner():
        try:
            payload = refresh_fn()
            cache_set(key, payload)
        finally:
            with CACHE_LOCK:
                BACKGROUND_REFRESHING_KEYS.discard(key)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()


def extract_tournament(sticker_name):
    if not sticker_name:
        return "Unknown"

    parts = [part.strip() for part in sticker_name.split(" | ")]
    if len(parts) >= 3 and parts[-1]:
        return parts[-1]

    return "No Tournament"


def extract_sticker_type(sticker_name):
    if not sticker_name:
        return "Unknown"

    lowered = sticker_name.lower()
    for sticker_type in ("Holo", "Foil", "Gold", "Glitter", "Lenticular", "Paper"):
        if f"({sticker_type.lower()})" in lowered:
            return sticker_type

    return "Regular"


def fetch_progress(conn):
    row = conn.execute("""
        SELECT skin_index, market_hash_name, updated_at
        FROM progress
        WHERE id = ?
    """, (PROGRESS_ID,)).fetchone()

    result = row_to_dict(row)
    result["total_skins"] = load_total_skins()
    return result


def fetch_overview(conn):
    latest_row = conn.execute("""
        SELECT fetched_at
        FROM sales
        WHERE fetched_at IS NOT NULL
        ORDER BY fetched_at DESC
        LIMIT 1
    """).fetchone()

    return {
        "sales_count": conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0],
        "skin_variant_count": conn.execute("""
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM sales
                WHERE name IS NOT NULL
                GROUP BY name
            )
        """).fetchone()[0],
        "latest_fetched_at": latest_row["fetched_at"] if latest_row else None,
        "sticker_rows": conn.execute("SELECT COUNT(*) FROM sale_stickers").fetchone()[0],
        "listings_with_stickers": conn.execute("""
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM sale_stickers
                GROUP BY listing_id
            )
        """).fetchone()[0],
    }


def _model_quality_metrics(metrics):
    test_metrics = metrics.get("test")
    if not isinstance(test_metrics, dict):
        test_metrics = metrics.get("test_metrics")
    if not isinstance(test_metrics, dict):
        test_metrics = {}

    def finite_number(*values):
        for value in values:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                return number
        return None

    median_ape = finite_number(
        test_metrics.get("median_ape_percent"),
        test_metrics.get("median_ape_%"),
        metrics.get("median_ape_percent"),
    )
    if median_ape is None:
        ratio = finite_number(test_metrics.get("median_absolute_percentage_error"))
        median_ape = ratio * 100 if ratio is not None else None

    p90_ape = finite_number(
        test_metrics.get("p90_ape_percent"),
        test_metrics.get("p90_ape_%"),
        metrics.get("p90_ape_percent"),
    )
    if p90_ape is None:
        ratio = finite_number(test_metrics.get("p90_absolute_percentage_error"))
        p90_ape = ratio * 100 if ratio is not None else None

    return {
        "mae_usd": finite_number(test_metrics.get("mae_usd"), metrics.get("test_mae_usd")),
        "median_ape_percent": median_ape,
        "p90_ape_percent": p90_ape,
    }


def _current_model_rows_by_item_kind(conn, current_sales_count):
    """Partition model rows without scanning every wide sale through catalog SQL.

    Every catalogued knife and glove market name starts with ``★``.  The
    ``sales.name`` index can therefore isolate the small special-item range;
    ordinary weapon-skin rows are the remainder of the already-computed total.
    """

    total_rows = max(int(current_sales_count or 0), 0)
    fallback = {
        ITEM_KIND_WEAPON_SKIN: total_rows,
        ITEM_KIND_KNIFE: 0,
        ITEM_KIND_GLOVE: 0,
    }
    try:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(sales)").fetchall()
        }
        if not {"name", "state", "price_cents"}.issubset(columns):
            return fallback, "all_sales_fallback"
        expression, parameters = sql_item_kind_expression(
            conn,
            name_expression="sales.name",
            unknown_as_weapon_skin=False,
        )
        rows = conn.execute(
            f"""
            SELECT item_kind, COUNT(*)
            FROM (
                SELECT {expression} AS item_kind
                FROM sales
                WHERE name GLOB '★*'
                  AND state = 'sold'
                  AND price_cents IS NOT NULL AND price_cents > 0
                  AND name IS NOT NULL
            )
            GROUP BY item_kind
            """,
            parameters,
        ).fetchall()
    except sqlite3.Error:
        return fallback, "all_sales_fallback"

    counts = {
        ITEM_KIND_WEAPON_SKIN: total_rows,
        ITEM_KIND_KNIFE: 0,
        ITEM_KIND_GLOVE: 0,
    }
    for item_kind, count in rows:
        if item_kind in {ITEM_KIND_KNIFE, ITEM_KIND_GLOVE}:
            counts[str(item_kind)] = max(int(count or 0), 0)
    counts[ITEM_KIND_WEAPON_SKIN] = max(
        total_rows - counts[ITEM_KIND_KNIFE] - counts[ITEM_KIND_GLOVE],
        0,
    )
    return counts, "indexed_starred_item_partition"


def build_model_freshness(conn, current_sales_count, project_dir=None):
    project_dir = Path(project_dir or app_config.PROJECT_DIR)
    legacy_dirs = {
        "almaz": project_dir / "models",
        "igor": project_dir / "IGORMODEL" / "artifacts",
        "nova": project_dir / "NEURALMODEL" / "artifacts",
        "knives": project_dir / "KNIFEMODEL" / "artifacts",
        "gloves": project_dir / "GLOVEMODEL" / "artifacts",
    }
    resolved_dirs = {}
    generation_errors = {}
    active_bundle_versions = {}
    for model_name, legacy_dir in legacy_dirs.items():
        try:
            resolved_dirs[model_name] = model_generations.active_or_legacy_bundle_dir(
                model_name,
                legacy_dir,
                project_dir=project_dir,
                verify_files=False,
            )
            pointer = model_generations.read_active_pointer(
                model_name, project_dir=project_dir
            )
            active_bundle_versions[model_name] = (
                pointer.get("generation_id") if pointer else None
            )
        except model_generations.GenerationError as exc:
            resolved_dirs[model_name] = None
            active_bundle_versions[model_name] = None
            generation_errors[model_name] = str(exc)
    specs = {
        "almaz": {
            "artifact_name": "csfloat_price_model.joblib",
            "metrics_names": ["csfloat_price_model_metrics.json"],
        },
        "igor": {
            "artifact_name": "price_model_point.cbm",
            "metrics_names": [
                "igor_metrics.json",
                "igor_model_metrics.json",
                "price_model_metrics.json",
                "training_metrics.json",
                "metrics.json",
            ],
        },
        "nova": {
            "artifact_name": "nova_price_model.pt",
            "metrics_names": ["nova_metrics.json"],
        },
        "knives": {
            "artifact_name": "knife_price_model.joblib",
            "metrics_names": ["knife_price_model_metrics.json"],
        },
        "gloves": {
            "artifact_name": "glove_price_model.joblib",
            "metrics_names": ["glove_price_model_metrics.json"],
        },
    }

    current_sales_count = max(int(current_sales_count or 0), 0)
    current_rows_by_item_kind, row_count_scope = _current_model_rows_by_item_kind(
        conn, current_sales_count
    )
    models = []
    for model_name, spec in specs.items():
        item_kind = {
            "knives": ITEM_KIND_KNIFE,
            "gloves": ITEM_KIND_GLOVE,
        }.get(model_name, ITEM_KIND_WEAPON_SKIN)
        current_model_rows = current_rows_by_item_kind[item_kind]
        bundle_dir = resolved_dirs[model_name]
        artifact_path = bundle_dir / spec["artifact_name"] if bundle_dir else None
        metrics_paths = (
            [bundle_dir / name for name in spec["metrics_names"]]
            if bundle_dir
            else []
        )
        metrics_path = next((path for path in metrics_paths if path.is_file()), None)
        metrics = read_json_file(metrics_path, {}) if metrics_path else {}
        if not isinstance(metrics, dict):
            metrics = {}

        artifact_available = bool(artifact_path and artifact_path.is_file())
        trained_at = metrics.get("trained_at")
        if not trained_at and artifact_available:
            trained_at = datetime.fromtimestamp(
                artifact_path.stat().st_mtime,
                tz=timezone.utc,
            ).isoformat()

        training_rows = metrics.get("rows", metrics.get("total_rows", metrics.get("training_rows")))
        if training_rows is None:
            source_database = metrics.get("source_database")
            if isinstance(source_database, dict):
                training_rows = source_database.get(
                    "valid_price_rows",
                    source_database.get("sales_rows"),
                )
        if training_rows is None:
            split_rows = [
                metrics.get("train_rows"),
                metrics.get("validation_rows"),
                metrics.get("test_rows"),
            ]
            numeric_split_rows = []
            for value in split_rows:
                if value is None or not str(value).strip():
                    continue
                try:
                    numeric_split_rows.append(int(value))
                except (TypeError, ValueError):
                    continue
            if numeric_split_rows:
                training_rows = sum(numeric_split_rows)

        rows_estimated = False
        if training_rows is None and artifact_available and trained_at:
            # Older Igor artifacts did not save metrics.  The indexed fetched_at
            # count is a useful approximation without loading its 650 MB pickle.
            try:
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(sales)").fetchall()
                }
                if {"name", "state", "price_cents"}.issubset(columns):
                    expression, parameters = sql_item_kind_expression(
                        conn,
                        name_expression="sales.name",
                        unknown_as_weapon_skin=False,
                    )
                    training_rows = conn.execute(
                        f"""
                        SELECT COUNT(*) FROM sales
                        WHERE fetched_at IS NOT NULL AND fetched_at <= ?
                          AND state = 'sold'
                          AND price_cents IS NOT NULL AND price_cents > 0
                          AND name IS NOT NULL
                          AND ({expression}) = ?
                        """,
                        (trained_at, *parameters, item_kind),
                    ).fetchone()[0]
                else:
                    training_rows = conn.execute(
                        "SELECT COUNT(*) FROM sales "
                        "WHERE fetched_at IS NOT NULL AND fetched_at <= ?",
                        (trained_at,),
                    ).fetchone()[0]
                rows_estimated = True
            except sqlite3.Error:
                training_rows = None

        try:
            training_rows = int(training_rows) if training_rows is not None else None
        except (TypeError, ValueError):
            training_rows = None

        new_rows = None
        growth_percent = None
        coverage_percent = None
        row_count_mismatch = bool(
            training_rows is not None and training_rows > current_model_rows
        )
        if training_rows is not None and training_rows > 0 and not row_count_mismatch:
            new_rows = current_model_rows - training_rows
            growth_percent = new_rows / training_rows * 100
            coverage_percent = (
                min(training_rows / current_model_rows * 100, 100)
                if current_model_rows
                else 0
            )

        if not artifact_available:
            freshness = "missing"
        elif row_count_mismatch:
            freshness = "data_mismatch"
        elif rows_estimated and training_rows:
            freshness = "estimated"
        elif growth_percent is None:
            freshness = "unknown"
        elif growth_percent >= 20:
            freshness = "retrain_recommended"
        elif growth_percent >= 5:
            freshness = "update_soon"
        else:
            freshness = "fresh"

        models.append({
            "name": model_name,
            "item_kind": item_kind,
            "artifact_available": artifact_available,
            "artifact_version": metrics.get("artifact_version"),
            "trained_at": trained_at,
            "training_rows": training_rows,
            "training_rows_estimated": rows_estimated,
            "current_rows": current_model_rows,
            "new_rows_since_training": new_rows,
            "data_growth_percent": growth_percent,
            "training_coverage_percent": coverage_percent,
            "row_count_mismatch": row_count_mismatch,
            "freshness": freshness,
            "quality": _model_quality_metrics(metrics),
            "active_bundle_version": active_bundle_versions[model_name],
            "generation_error": generation_errors.get(model_name),
        })

    return {
        "current_rows": current_sales_count,
        "current_rows_by_item_kind": current_rows_by_item_kind,
        "current_rows_scope": row_count_scope,
        "models": models,
    }


def unix_seconds_to_iso(value):
    if value is None:
        return None

    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def fetch_schedule_overview(conn):
    if not table_exists(conn, "skin_schedule"):
        return {
            "target_new_sales": TARGET_NEW_SALES_PER_REQUEST,
            "total_scheduled": 0,
            "due_now": 0,
            "never_fetched": 0,
            "min_interval_seconds": None,
            "median_interval_seconds": None,
            "max_interval_seconds": None,
            "next_fetch_at": None,
        }

    now = int(time.time())
    row = conn.execute("""
        SELECT
            COUNT(*) AS total_scheduled,
            SUM(CASE WHEN COALESCE(next_fetch_at, 0) <= ? THEN 1 ELSE 0 END) AS due_now,
            SUM(CASE WHEN last_fetch_at IS NULL THEN 1 ELSE 0 END) AS never_fetched,
            MIN(interval_seconds) AS min_interval_seconds,
            MAX(interval_seconds) AS max_interval_seconds,
            MIN(next_fetch_at) AS next_fetch_at
        FROM skin_schedule
    """, (now,)).fetchone()

    median_row = conn.execute("""
        SELECT interval_seconds
        FROM skin_schedule
        WHERE interval_seconds IS NOT NULL
        ORDER BY interval_seconds
        LIMIT 1 OFFSET (
            SELECT COUNT(*)
            FROM skin_schedule
            WHERE interval_seconds IS NOT NULL
        ) / 2
    """).fetchone()

    result = row_to_dict(row)
    result["target_new_sales"] = TARGET_NEW_SALES_PER_REQUEST
    result["due_now"] = result.get("due_now") or 0
    result["never_fetched"] = result.get("never_fetched") or 0
    result["median_interval_seconds"] = median_row["interval_seconds"] if median_row else None
    result["next_fetch_at_iso"] = unix_seconds_to_iso(result.get("next_fetch_at"))
    result["next_fetch_seconds_left"] = (
        int(result["next_fetch_at"]) - now
        if result.get("next_fetch_at") is not None
        else None
    )
    return result


def fetch_request_log_summary(conn):
    if not table_exists(conn, "parser_request_log"):
        return {
            "request_count": 0,
            "new_sales_sum": 0,
            "sales_sum": 0,
            "avg_new_sales": 0,
            "rows": [],
        }

    columns = table_columns(conn, "parser_request_log")
    outcome_expr = optional_column_expr(columns, "outcome", "outcome")
    overlap_expr = optional_column_expr(columns, "overlap_count", "overlap_count")
    page_count_expr = optional_column_expr(columns, "page_count", "page_count")
    coverage_expr = optional_column_expr(columns, "coverage_status", "coverage_status")
    now = int(time.time())
    rows = [
        dict(row)
        for row in conn.execute(f"""
            SELECT
                requested_at,
                market_hash_name,
                sales_count,
                new_sales_count,
                interval_seconds,
                interval_reason,
                next_fetch_at,
                {outcome_expr},
                {overlap_expr},
                {page_count_expr},
                {coverage_expr}
            FROM parser_request_log
            ORDER BY requested_at DESC, id DESC
            LIMIT 100
        """)
    ]

    for row in rows:
        row["requested_at_iso"] = unix_seconds_to_iso(row.get("requested_at"))
        row["next_fetch_at_iso"] = unix_seconds_to_iso(row.get("next_fetch_at"))
        row["next_fetch_seconds_left"] = (
            int(row["next_fetch_at"]) - now
            if row.get("next_fetch_at") is not None
            else None
        )

    request_count = len(rows)
    new_sales_sum = sum(int(row.get("new_sales_count") or 0) for row in rows)
    sales_sum = sum(int(row.get("sales_count") or 0) for row in rows)

    return {
        "request_count": request_count,
        "new_sales_sum": new_sales_sum,
        "sales_sum": sales_sum,
        "avg_new_sales": new_sales_sum / request_count if request_count else 0,
        "rows": rows[:20],
    }


def decorate_schedule_rows(rows):
    now = int(time.time())
    for row in rows:
        next_fetch_at = row.get("next_fetch_at")
        last_fetch_at = row.get("last_fetch_at")
        row["next_fetch_at_iso"] = unix_seconds_to_iso(next_fetch_at)
        row["last_fetch_at_iso"] = unix_seconds_to_iso(last_fetch_at)
        row["next_fetch_seconds_left"] = int(next_fetch_at) - now if next_fetch_at is not None else None
        row["state"] = "due" if next_fetch_at is not None and int(next_fetch_at) <= now else "scheduled"

    return rows


def schedule_select_sql(where_sql, columns):
    rate_expr = optional_column_expr(
        columns,
        "estimated_sales_per_day",
        "estimated_sales_per_day",
    )
    streak_expr = optional_column_expr(columns, "saturation_streak", "saturation_streak")
    coverage_expr = optional_column_expr(columns, "coverage_status", "coverage_status")
    return f"""
        SELECT
            market_hash_name,
            skin_order,
            request_count,
            total_sales_returned,
            total_new_sales,
            last_sales_count,
            last_new_sales_count,
            avg_sales_count,
            avg_new_sales_count,
            interval_seconds,
            interval_reason,
            sales_span_seconds,
            new_sales_span_seconds,
            last_fetch_at,
            next_fetch_at,
            last_sold_at,
            {rate_expr},
            {streak_expr},
            {coverage_expr}
        FROM skin_schedule
        {where_sql}
        ORDER BY
            COALESCE(next_fetch_at, 0) ASC,
            COALESCE(avg_new_sales_count, 0) DESC,
            COALESCE(avg_sales_count, 0) DESC,
            skin_order ASC
        LIMIT ?
        OFFSET ?
    """


def fetch_schedule_rows(conn, limit):
    if not table_exists(conn, "skin_schedule"):
        return []

    columns = table_columns(conn, "skin_schedule")
    rows = [
        dict(row)
        for row in conn.execute(schedule_select_sql("", columns), (limit, 0))
    ]
    return decorate_schedule_rows(rows)


def fetch_schedule_page(conn, offset, limit, search):
    limit = min(max(int(limit), 1), 250)
    offset = max(int(offset), 0)
    search = (search or "").strip()

    if not table_exists(conn, "skin_schedule"):
        return {
            "offset": offset,
            "limit": limit,
            "total": 0,
            "rows": [],
        }

    where_sql = ""
    params = []

    if search:
        where_sql = "WHERE market_hash_name LIKE ?"
        params.append(f"%{search}%")

    total = conn.execute(f"""
        SELECT COUNT(*) AS total
        FROM skin_schedule
        {where_sql}
    """, params).fetchone()["total"]

    columns = table_columns(conn, "skin_schedule")
    rows = [
        dict(row)
        for row in conn.execute(
            schedule_select_sql(where_sql, columns),
            [*params, limit, offset],
        )
    ]

    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "rows": decorate_schedule_rows(rows),
    }


def fetch_top_stickers(conn, limit):
    sticker_columns = table_columns(conn, "sale_stickers")
    steam_url_expr = optional_qualified_column(sticker_columns, "st", "steam_market_url")
    steam_join, rarity_color_expr, name_color_expr = steam_asset_join(conn, "st.name")
    rows = [
        dict(row)
        for row in conn.execute(f"""
            SELECT
                st.sticker_id,
                st.name AS sticker_name,
                MAX(st.icon_url) AS icon_url,
                MAX({steam_url_expr}) AS steam_market_url,
                MAX({rarity_color_expr}) AS rarity_color,
                MAX({name_color_expr}) AS name_color,
                COUNT(*) AS sticker_count,
                COUNT(DISTINCT st.listing_id) AS listing_count,
                COUNT(DISTINCT s.name) AS skin_count
            FROM sale_stickers st
            JOIN sales s ON s.listing_id = st.listing_id
            {steam_join}
            WHERE st.name IS NOT NULL
            GROUP BY st.sticker_id, st.name
            ORDER BY sticker_count DESC, listing_count DESC
            LIMIT ?
        """, (limit,))
    ]
    return add_market_urls(rows, "sticker_name", "steam_market_url")


def fetch_top_weapons(conn, limit):
    return [
        dict(row)
        for row in conn.execute("""
            WITH weapon_rows AS (
                SELECT
                    CASE
                        WHEN INSTR(COALESCE(s.base_skin_name, s.name), ' | ') > 0
                        THEN SUBSTR(COALESCE(s.base_skin_name, s.name), 1, INSTR(COALESCE(s.base_skin_name, s.name), ' | ') - 1)
                        ELSE COALESCE(s.base_skin_name, s.name, 'Unknown')
                    END AS weapon_name,
                    st.sticker_id,
                    st.listing_id,
                    s.name
                FROM sale_stickers st
                JOIN sales s ON s.listing_id = st.listing_id
            )
            SELECT
                weapon_name,
                COUNT(sticker_id) AS sticker_count,
                COUNT(DISTINCT listing_id) AS listing_count,
                COUNT(DISTINCT name) AS skin_count
            FROM weapon_rows
            GROUP BY weapon_name
            ORDER BY sticker_count DESC, listing_count DESC
            LIMIT ?
        """, (limit,))
    ]


def fetch_weapons(conn):
    catalog_columns = table_columns(conn, "cs2_item_catalog")
    if {"market_hash_name", "item_kind", "weapon_name"}.issubset(catalog_columns):
        return [
            dict(row)
            for row in conn.execute("""
                SELECT
                    weapon_name,
                    COUNT(*) AS skin_count,
                    NULL AS listing_count,
                    NULL AS sticker_count
                FROM cs2_item_catalog
                WHERE item_kind = 'weapon_skin'
                    AND TRIM(COALESCE(weapon_name, '')) != ''
                GROUP BY weapon_name
                ORDER BY weapon_name COLLATE NOCASE
            """)
        ]
    return [
        dict(row)
        for row in conn.execute("""
            WITH weapon_rows AS (
                SELECT
                    CASE
                        WHEN INSTR(COALESCE(s.base_skin_name, s.name), ' | ') > 0
                        THEN SUBSTR(COALESCE(s.base_skin_name, s.name), 1, INSTR(COALESCE(s.base_skin_name, s.name), ' | ') - 1)
                        ELSE COALESCE(s.base_skin_name, s.name, 'Unknown')
                    END AS weapon_name,
                    s.listing_id,
                    s.name AS skin_name,
                    st.sticker_id
                FROM sales s
                LEFT JOIN sale_stickers st ON st.listing_id = s.listing_id
            )
            SELECT
                weapon_name,
                COUNT(DISTINCT skin_name) AS skin_count,
                COUNT(DISTINCT listing_id) AS listing_count,
                COUNT(sticker_id) AS sticker_count
            FROM weapon_rows
            GROUP BY weapon_name
            ORDER BY sticker_count DESC, listing_count DESC, weapon_name ASC
        """)
    ]


def _fetch_weapon_skins_from_sales(conn, weapon_name):
    sales_columns = table_columns(conn, "sales")
    steam_url_expr = optional_qualified_column_expr(sales_columns, "s", "item_steam_market_url", "item_steam_market_url")
    rarity_color_expr = optional_qualified_column_expr(sales_columns, "s", "item_rarity_color", "item_rarity_color")
    name_color_expr = optional_qualified_column_expr(sales_columns, "s", "item_name_color", "item_name_color")
    rows = [
        dict(row)
        for row in conn.execute(f"""
            WITH skin_rows AS (
                SELECT
                    CASE
                        WHEN INSTR(COALESCE(s.base_skin_name, s.name), ' | ') > 0
                        THEN SUBSTR(COALESCE(s.base_skin_name, s.name), 1, INSTR(COALESCE(s.base_skin_name, s.name), ' | ') - 1)
                        ELSE COALESCE(s.base_skin_name, s.name, 'Unknown')
                    END AS weapon_name,
                    s.name AS skin_name,
                    s.item_icon_url,
                    {steam_url_expr},
                    {rarity_color_expr},
                    {name_color_expr},
                    s.listing_id,
                    s.price_cents,
                    s.fetched_at,
                    st.sticker_id
                FROM sales s
                LEFT JOIN sale_stickers st ON st.listing_id = s.listing_id
            )
            SELECT
                skin_name,
                MAX(item_icon_url) AS item_icon_url,
                MAX(item_steam_market_url) AS item_steam_market_url,
                MAX(item_rarity_color) AS item_rarity_color,
                MAX(item_name_color) AS item_name_color,
                COUNT(DISTINCT listing_id) AS sales_count,
                COUNT(sticker_id) AS sticker_count,
                COUNT(DISTINCT CASE WHEN sticker_id IS NOT NULL THEN listing_id END) AS listings_with_stickers,
                AVG(price_cents) AS avg_price_cents,
                MAX(fetched_at) AS latest_fetched_at
            FROM skin_rows
            WHERE weapon_name = ?
            GROUP BY skin_name
            ORDER BY sticker_count DESC, sales_count DESC, skin_name ASC
        """, (weapon_name,))
    ]
    return add_market_urls(rows, "skin_name", "item_steam_market_url")


def fetch_weapon_skins(conn, weapon_name, trend_db_path=None):
    """Return the complete local catalog without aggregating raw sales per request.

    The legacy query joined every sale to every applied sticker before it knew
    which weapon was requested.  On the production database that made a single
    AK/MAC-10 click take tens of seconds and also multiplied prices for
    stickered listings.  The synchronized item catalog is authoritative for
    membership; the compact trend store supplies already-materialized observed
    counts and the latest daily market price.
    """

    catalog_columns = table_columns(conn, "cs2_item_catalog")
    required = {
        "market_hash_name",
        "item_kind",
        "weapon_name",
        "image_url",
        "collection_names_json",
        "crate_names_json",
        "wear_name",
        "paint_name",
        "stattrak",
        "souvenir",
    }
    if not required.issubset(catalog_columns):
        return _fetch_weapon_skins_from_sales(conn, weapon_name)

    catalog_rows = [
        dict(row)
        for row in conn.execute("""
            SELECT
                market_hash_name AS skin_name,
                image_url AS item_icon_url,
                collection_names_json,
                crate_names_json,
                wear_name,
                paint_name,
                stattrak,
                souvenir
            FROM cs2_item_catalog
            WHERE item_kind = 'weapon_skin' AND weapon_name = ?
            ORDER BY paint_name COLLATE NOCASE,
                     wear_name COLLATE NOCASE,
                     market_hash_name COLLATE NOCASE
        """, (weapon_name,))
    ]
    if not catalog_rows:
        return []

    stats_by_name = {}
    effective_trend_path = str(trend_db_path or TREND_DB_PATH)
    if Path(effective_trend_path).is_file():
        names = [row["skin_name"] for row in catalog_rows]
        placeholders = ",".join("?" for _ in names)
        try:
            with sqlite3.connect(
                f"file:{effective_trend_path}?mode=ro", uri=True, timeout=5
            ) as trend_conn:
                trend_conn.row_factory = sqlite3.Row
                trend_conn.execute("PRAGMA busy_timeout=5000")
                stats_by_name = {
                    row["market_hash_name"]: dict(row)
                    for row in trend_conn.execute(f"""
                        SELECT
                            c.market_hash_name,
                            c.observed_sales_count,
                            c.icon_url,
                            d.mean_price_cents,
                            d.median_price_cents,
                            d.day AS price_day,
                            d.source_max_fetched_at
                        FROM trend_skin_catalog c
                        LEFT JOIN trend_daily_market d
                          ON d.market_hash_name = c.market_hash_name
                         AND d.day = (
                            SELECT MAX(d2.day)
                            FROM trend_daily_market d2
                            WHERE d2.market_hash_name = c.market_hash_name
                         )
                        WHERE c.market_hash_name IN ({placeholders})
                    """, names)
                }
        except sqlite3.Error:
            stats_by_name = {}

    rows = []
    for catalog_row in catalog_rows:
        name = catalog_row["skin_name"]
        stats = stats_by_name.get(name, {})
        rows.append({
            **catalog_row,
            "item_icon_url": catalog_row.get("item_icon_url") or stats.get("icon_url"),
            "item_steam_market_url": None,
            "item_rarity_color": None,
            "item_name_color": None,
            "sales_count": int(stats.get("observed_sales_count") or 0),
            # Sticker totals are intentionally not recomputed from the 7+ GB
            # raw database here.  Exact sticker history remains in Skin Detail.
            "sticker_count": None,
            "listings_with_stickers": None,
            "avg_price_cents": stats.get("mean_price_cents"),
            "median_price_cents": stats.get("median_price_cents"),
            "latest_fetched_at": stats.get("source_max_fetched_at"),
            "price_as_of": stats.get("price_day"),
            "stats_source": "trend_daily_market" if stats else None,
        })

    rows.sort(
        key=lambda row: (
            -int(row.get("sales_count") or 0),
            str(row.get("skin_name") or "").casefold(),
        )
    )
    return add_market_urls(rows, "skin_name", "item_steam_market_url")


def fetch_tournaments(conn):
    return [
        dict(row)
        for row in conn.execute("""
            SELECT
                extract_tournament(st.name) AS tournament,
                COUNT(*) AS sticker_count,
                COUNT(DISTINCT st.sticker_id) AS sticker_id_count,
                COUNT(DISTINCT st.listing_id) AS listing_count,
                COUNT(DISTINCT s.name) AS skin_count
            FROM sale_stickers st
            JOIN sales s ON s.listing_id = st.listing_id
            WHERE st.name IS NOT NULL
            GROUP BY tournament
            ORDER BY sticker_count DESC, listing_count DESC, tournament ASC
        """)
    ]


def fetch_tournament_stickers(conn, tournament):
    sticker_columns = table_columns(conn, "sale_stickers")
    steam_url_expr = optional_qualified_column(sticker_columns, "st", "steam_market_url")
    steam_join, rarity_color_expr, name_color_expr = steam_asset_join(conn, "st.name")
    rows = [
        dict(row)
        for row in conn.execute(f"""
            SELECT
                st.sticker_id,
                st.name AS sticker_name,
                MAX(st.icon_url) AS icon_url,
                MAX({steam_url_expr}) AS steam_market_url,
                MAX({rarity_color_expr}) AS rarity_color,
                MAX({name_color_expr}) AS name_color,
                extract_sticker_type(st.name) AS sticker_type,
                COUNT(*) AS sticker_count,
                COUNT(DISTINCT st.listing_id) AS listing_count,
                COUNT(DISTINCT s.name) AS skin_count,
                AVG(st.price_cents) AS avg_sticker_price_cents
            FROM sale_stickers st
            JOIN sales s ON s.listing_id = st.listing_id
            {steam_join}
            WHERE st.name IS NOT NULL
                AND extract_tournament(st.name) = ?
            GROUP BY st.sticker_id, st.name
            ORDER BY sticker_count DESC, listing_count DESC, sticker_name ASC
        """, (tournament,))
    ]
    return add_market_urls(rows, "sticker_name", "steam_market_url")


def fetch_recent_sales(conn, limit):
    sales_columns = table_columns(conn, "sales")
    steam_url_expr = optional_qualified_column_expr(sales_columns, "sales", "item_steam_market_url", "item_steam_market_url")
    rarity_color_expr = optional_qualified_column_expr(sales_columns, "sales", "item_rarity_color", "item_rarity_color")
    name_color_expr = optional_qualified_column_expr(sales_columns, "sales", "item_name_color", "item_name_color")
    rows = [
        dict(row)
        for row in conn.execute(f"""
            SELECT
                name,
                item_icon_url,
                {steam_url_expr},
                {rarity_color_expr},
                {name_color_expr},
                price_cents,
                float_value,
                sold_at,
                fetched_at
            FROM sales
            ORDER BY fetched_at DESC, sold_at DESC
            LIMIT ?
        """, (limit,))
    ]
    return add_market_urls(rows, "name", "item_steam_market_url")


def fetch_listings_page(conn, offset, limit, search):
    limit = min(max(int(limit), 1), 100)
    offset = max(int(offset), 0)
    search = (search or "").strip()

    sales_columns = table_columns(conn, "sales")
    sticker_columns = table_columns(conn, "sale_stickers")

    sales_steam_url_expr = optional_qualified_column_expr(sales_columns, "s", "item_steam_market_url", "item_steam_market_url")
    sales_rarity_color_expr = optional_qualified_column_expr(sales_columns, "s", "item_rarity_color", "item_rarity_color")
    sales_name_color_expr = optional_qualified_column_expr(sales_columns, "s", "item_name_color", "item_name_color")
    sticker_steam_url_expr = optional_qualified_column_expr(sticker_columns, "st", "steam_market_url", "steam_market_url")

    where_sql = ""
    params = []

    if search:
        pattern = f"%{search}%"
        where_sql = """
            WHERE
                s.name LIKE ?
                OR s.listing_id LIKE ?
                OR EXISTS (
                    SELECT 1
                    FROM sale_stickers stx
                    WHERE stx.listing_id = s.listing_id
                        AND stx.name LIKE ?
                )
        """
        params.extend([pattern, pattern, pattern])

    total = conn.execute(f"""
        SELECT COUNT(*) AS total
        FROM sales s
        {where_sql}
    """, params).fetchone()["total"]

    rows = [
        dict(row)
        for row in conn.execute(f"""
            SELECT
                s.listing_id,
                s.name,
                s.item_icon_url,
                {sales_steam_url_expr},
                {sales_rarity_color_expr},
                {sales_name_color_expr},
                s.price_cents,
                s.float_value,
                s.paint_seed,
                s.paint_index,
                s.inspect_link,
                s.serialized_inspect,
                s.sold_at,
                s.fetched_at
            FROM sales s
            {where_sql}
            ORDER BY s.fetched_at DESC, s.sold_at DESC, s.listing_id DESC
            LIMIT ?
            OFFSET ?
        """, [*params, limit, offset])
    ]

    add_market_urls(rows, "name", "item_steam_market_url")

    listing_ids = [row["listing_id"] for row in rows]
    stickers_by_listing = {listing_id: [] for listing_id in listing_ids}

    if listing_ids:
        placeholders = ", ".join("?" for _ in listing_ids)
        sticker_rows = [
            dict(row)
            for row in conn.execute(f"""
                SELECT
                    st.listing_id,
                    st.sticker_index,
                    st.sticker_id,
                    st.slot,
                    st.name,
                    st.icon_url,
                    {sticker_steam_url_expr},
                    st.wear,
                    st.price_cents
                FROM sale_stickers st
                WHERE st.listing_id IN ({placeholders})
                ORDER BY st.listing_id, st.sticker_index
            """, listing_ids)
        ]

        for sticker in sticker_rows:
            if not sticker.get("steam_market_url"):
                sticker["steam_market_url"] = steam_market_url(sticker.get("name"))
            stickers_by_listing.setdefault(sticker["listing_id"], []).append(sticker)

    for row in rows:
        row["stickers"] = stickers_by_listing.get(row["listing_id"], [])

    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "rows": rows,
    }


def fetch_listings_page_fast(conn, offset, limit, search, scope="skin"):
    """Index-aware listing browser with market-link decoration."""

    payload = listing_search.fetch_listings_page(
        conn,
        offset,
        limit,
        search,
        scope,
    )
    payload.setdefault("max_offset", listing_search.MAX_OFFSET)
    rows = payload.get("rows") or []
    add_market_urls(rows, "name", "item_steam_market_url")
    for row in rows:
        for sticker in row.get("stickers") or []:
            if not sticker.get("steam_market_url"):
                sticker["steam_market_url"] = steam_market_url(sticker.get("name"))
    return payload


def fetch_deal_candidates_page(conn, offset, limit, search, verdicts=None):
    limit = min(max(int(limit), 1), 100)
    offset = max(int(offset), 0)
    search = (search or "").strip()
    verdicts = [str(item).strip() for item in (verdicts or []) if str(item).strip()]

    if not table_exists(conn, "model_deal_candidates"):
        return {
            "offset": offset,
            "limit": limit,
            "total": 0,
            "rows": [],
        }

    where_parts = []
    params = []

    if verdicts:
        placeholders = ", ".join("?" for _ in verdicts)
        where_parts.append(f"verdict IN ({placeholders})")
        params.extend(verdicts)

    if search:
        where_parts.append("""
            (skin_name LIKE ?
                OR source_sticker_name LIKE ?
                OR listing_id LIKE ?)
        """)
        pattern = f"%{search}%"
        params.extend([pattern, pattern, pattern])

    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total = conn.execute(f"""
        SELECT COUNT(*) AS total
        FROM model_deal_candidates
        {where_sql}
    """, params).fetchone()["total"]

    rows = [
        dict(row)
        for row in conn.execute(f"""
            SELECT
                listing_id,
                first_seen_at,
                last_seen_at,
                scan_count,
                source_sticker_id,
                source_sticker_name,
                skin_name,
                buy_price_cents,
                predicted_price_cents,
                low_price_cents,
                high_price_cents,
                net_expected_cents,
                profit_cents,
                roi_percent,
                liquidity_score,
                historical_sales_count,
                sales_per_day,
                verdict,
                pricing_method,
                sticker_count,
                sticker_value_cents,
                float_value,
                paint_seed,
                paint_index,
                csfloat_url,
                inspect_link,
                key_file,
                proxy,
                rate_remaining,
                deal_payload_json
            FROM model_deal_candidates
            {where_sql}
            ORDER BY roi_percent DESC, profit_cents DESC, last_seen_at DESC
            LIMIT ?
            OFFSET ?
        """, [*params, limit, offset])
    ]

    for row in rows:
        row["listing_created_at"] = None
        raw_payload = row.pop("deal_payload_json", None)
        if raw_payload:
            try:
                payload = json.loads(raw_payload)
            except (TypeError, json.JSONDecodeError):
                payload = {}
            row["listing_created_at"] = (
                payload.get("created_at")
                or payload.get("createdAt")
                or payload.get("listed_at")
                or payload.get("listedAt")
            )

    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "rows": rows,
    }


def deal_opinion_stale_hours():
    """Use the scanner's actionable lifetime without importing model runtimes."""

    config = read_json_file(app_config.PROJECT_DIR / "deal_scanner_config.json", {})
    value = config.get("stale_prediction_hours", DEFAULT_DEAL_OPINION_STALE_HOURS)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DEAL_OPINION_STALE_HOURS
    if not 1 <= value <= 720:
        return DEFAULT_DEAL_OPINION_STALE_HOURS
    return value


def deal_scanner_default_interval_seconds():
    """Read the legacy default without importing the model-heavy scanner."""

    config = read_json_file(app_config.PROJECT_DIR / "deal_scanner_config.json", {})
    value = config.get("scan_interval_seconds", scanner_control.DEFAULT_INTERVAL_SECONDS)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return scanner_control.DEFAULT_INTERVAL_SECONDS
    if not scanner_control.MIN_INTERVAL_SECONDS <= value <= scanner_control.MAX_INTERVAL_SECONDS:
        return scanner_control.DEFAULT_INTERVAL_SECONDS
    return value


def process_is_alive(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def build_scanner_control_payload(*, now=None):
    """Return desired controls together with trustworthy runtime freshness."""

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    snapshot = scanner_control.get_scanner_snapshot(
        default_interval_seconds=deal_scanner_default_interval_seconds(),
    )
    control = dict(snapshot.get("control") or {})
    runtime = dict(snapshot.get("runtime") or {})
    heartbeat = parse_iso_timestamp(runtime.get("heartbeat_at"))
    heartbeat_age = None
    heartbeat_in_future = False
    if heartbeat is not None:
        raw_heartbeat_age = (now - heartbeat).total_seconds()
        heartbeat_in_future = raw_heartbeat_age < -SCANNER_HEARTBEAT_FUTURE_TOLERANCE_SECONDS
        heartbeat_age = max(0, int(raw_heartbeat_age))
    stored_state = str(runtime.get("process_state") or "stopped")
    pid_alive = stored_state != "stopped" and process_is_alive(runtime.get("pid"))
    online = bool(
        stored_state != "stopped"
        and heartbeat_age is not None
        and heartbeat_age <= SCANNER_HEARTBEAT_STALE_SECONDS
        and not heartbeat_in_future
        and pid_alive
    )
    next_scan = parse_iso_timestamp(runtime.get("next_scan_at"))
    runtime["heartbeat_age_seconds"] = heartbeat_age
    runtime["heartbeat_in_future"] = heartbeat_in_future
    runtime["pid_alive"] = pid_alive
    runtime["online"] = online
    runtime["effective_state"] = (
        stored_state if online else ("unresponsive" if pid_alive else "offline")
    )
    runtime["next_scan_in_seconds"] = (
        max(0, int((next_scan - now).total_seconds())) if next_scan is not None else None
    )
    runtime.pop("pid", None)

    if control.get("error"):
        health = "control_error"
    elif not control.get("enabled"):
        health = "paused"
    elif not pid_alive:
        health = "offline"
    elif not online:
        health = "unresponsive"
    elif stored_state == "scanning":
        health = "scanning"
    else:
        health = "ready"

    return {
        "control": control,
        "runtime": runtime,
        "health": health,
        "limits": {
            "min_interval_seconds": scanner_control.MIN_INTERVAL_SECONDS,
            "max_interval_seconds": scanner_control.MAX_INTERVAL_SECONDS,
            "default_interval_seconds": deal_scanner_default_interval_seconds(),
        },
        "server_time": now.isoformat(),
    }


def scanner_control_update_fallback(control, *, now=None):
    """A control commit succeeded even if the follow-up status read failed."""

    now = now or datetime.now(timezone.utc)
    return {
        "control": dict(control),
        "runtime": {
            "process_state": "stopped",
            "heartbeat_at": None,
            "cycle_started_at": None,
            "cycle_completed_at": None,
            "next_scan_at": None,
            "last_error": "status_unavailable",
            "cycle_number": 0,
            "cycles_completed": 0,
            "cycle_targets_total": 0,
            "cycle_targets_scanned": 0,
            "cycle_listings_returned": 0,
            "cycle_candidates_saved": 0,
            "heartbeat_age_seconds": None,
            "heartbeat_in_future": False,
            "pid_alive": False,
            "online": False,
            "effective_state": "offline",
            "next_scan_in_seconds": None,
        },
        "health": "status_unavailable",
        "limits": {
            "min_interval_seconds": scanner_control.MIN_INTERVAL_SECONDS,
            "max_interval_seconds": scanner_control.MAX_INTERVAL_SECONDS,
            "default_interval_seconds": deal_scanner_default_interval_seconds(),
        },
        "server_time": now.isoformat(),
    }


def update_scanner_control_from_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    allowed = {"enabled", "interval_seconds"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"unexpected fields: {', '.join(unknown)}")
    if not payload:
        raise ValueError("enabled or interval_seconds is required")

    changes = {}
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise ValueError("enabled must be true or false")
        changes["enabled"] = payload["enabled"]
    if "interval_seconds" in payload:
        interval = payload["interval_seconds"]
        if isinstance(interval, bool) or not isinstance(interval, int):
            raise ValueError("interval_seconds must be an integer")
        if not scanner_control.MIN_INTERVAL_SECONDS <= interval <= scanner_control.MAX_INTERVAL_SECONDS:
            raise ValueError(
                "interval_seconds must be between "
                f"{scanner_control.MIN_INTERVAL_SECONDS} and "
                f"{scanner_control.MAX_INTERVAL_SECONDS}"
            )
        changes["interval_seconds"] = interval

    updated_control = scanner_control.update_scanner_control(
        default_interval_seconds=deal_scanner_default_interval_seconds(),
        **changes,
    )
    try:
        return build_scanner_control_payload()
    except (OSError, scanner_control.ScannerControlError):
        return scanner_control_update_fallback(updated_control)


def build_scanner_targets_payload():
    """Return durable profile settings without loading any ML runtime."""

    return scanner_target_catalog.build_target_control_payload()


def build_scanner_target_preview_payload(
    profile,
    *,
    search="",
    limit=50,
    offset=0,
):
    readiness = None
    if str(profile or "").strip() == "knives":
        status = _knife_model_dashboard_status("collecting_data")
        available = bool(status.get("dedicated_model_available"))
        readiness = {
            "scan_ready": available,
            "blocked_reason": (
                None if available else "dedicated_knife_model_unavailable"
            ),
        }
    elif str(profile or "").strip() == "gloves":
        status = _verified_active_glove_generation()
        available = bool(status.get("dedicated_model_available"))
        readiness = {
            "scan_ready": available,
            "blocked_reason": (
                None if available else "dedicated_glove_model_unavailable"
            ),
        }
    return scanner_target_catalog.build_scanner_target_preview(
        DB_PATH,
        profile,
        search=search,
        limit=limit,
        offset=offset,
        readiness=readiness,
    )


def update_scanner_targets_from_payload(payload):
    return scanner_target_catalog.update_target_control_from_payload(payload)


def scanner_mutation_request_violation(handler, expected_action):
    """Apply one same-origin JSON policy to every scanner mutation."""

    content_type = (
        handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "application/json":
        return 415, "Content-Type must be application/json"
    if handler.headers.get("Transfer-Encoding"):
        return 400, "chunked request bodies are not accepted"
    if not secrets.compare_digest(
        handler.headers.get("X-CS2-Action", ""), expected_action
    ):
        return 403, "missing scanner action confirmation"
    fetch_site = handler.headers.get("Sec-Fetch-Site", "").strip().lower()
    if fetch_site not in {"", "none", "same-origin"}:
        return 403, "cross-site scanner changes are not allowed"
    origin = handler.headers.get("Origin", "").strip()
    if origin:
        parsed_origin = urlparse(origin)
        request_host = handler.headers.get("Host", "").strip().casefold()
        if (
            parsed_origin.scheme not in {"http", "https"}
            or not request_host
            or parsed_origin.netloc.casefold() != request_host
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.params
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            return 403, "request Origin does not match this dashboard"
    return None


def _pending_model_training_eligibility():
    return {
        model: {
            "model": model,
            "item_kind": (
                ITEM_KIND_KNIFE
                if model == "knives"
                else ITEM_KIND_WEAPON_SKIN
            ),
            "bootstrap": model == "knives",
            "eligible": False,
            "reasons": ["eligibility_refreshing"],
            "trained_rows": None,
            "current_rows": 0,
            "new_rows": 0,
            "growth_percent": 0.0,
            "production_boundary": None,
            "runtime_valid_new_rows": 0,
            "trainer_available": True,
            "thresholds": {
                "min_new_rows": (
                    model_training_jobs.KNIFE_BOOTSTRAP_MIN_ROWS
                    if model == "knives"
                    else model_training_jobs.MIN_NEW_ROWS
                ),
                "min_growth_percent": model_training_jobs.MIN_DATA_GROWTH_PERCENT,
                "min_runtime_valid_new_rows": (
                    model_training_jobs.KNIFE_BOOTSTRAP_MIN_RUNTIME_VALID_ROWS
                    if model == "knives"
                    else model_training_jobs.MIN_RUNTIME_VALID_NEW_ROWS
                ),
            },
        }
        for model in model_training_jobs.MODEL_NAMES
    }


def _refresh_model_training_eligibility_cache():
    try:
        value = model_training_jobs.get_all_training_eligibility()
    except Exception:
        # A background refresh must never leave the UI permanently stuck in
        # `eligibility_refreshing`; the next idle poll can retry safely.
        value = None
    with MODEL_TRAINING_CACHE_LOCK:
        if isinstance(value, dict):
            MODEL_TRAINING_ELIGIBILITY_CACHE.update(
                {"loaded_monotonic": time.monotonic(), "value": value}
            )
        MODEL_TRAINING_ELIGIBILITY_CACHE["refreshing"] = False


def _cached_model_training_eligibility(*, force=False, allow_refresh=True):
    """Return cached gates immediately; refresh costly SQLite counts off-thread."""

    if force:
        value = model_training_jobs.get_all_training_eligibility()
        with MODEL_TRAINING_CACHE_LOCK:
            MODEL_TRAINING_ELIGIBILITY_CACHE.update(
                {
                    "loaded_monotonic": time.monotonic(),
                    "value": value,
                    "refreshing": False,
                }
            )
        return value

    now = time.monotonic()
    start_refresh = False
    with MODEL_TRAINING_CACHE_LOCK:
        loaded = float(MODEL_TRAINING_ELIGIBILITY_CACHE.get("loaded_monotonic") or 0)
        value = MODEL_TRAINING_ELIGIBILITY_CACHE.get("value")
        fresh = (
            isinstance(value, dict)
            and now - loaded < MODEL_TRAINING_ELIGIBILITY_CACHE_SECONDS
        )
        if fresh:
            return value
        if allow_refresh and not MODEL_TRAINING_ELIGIBILITY_CACHE.get("refreshing"):
            MODEL_TRAINING_ELIGIBILITY_CACHE["refreshing"] = True
            start_refresh = True
        result = value if isinstance(value, dict) else _pending_model_training_eligibility()
    if start_refresh:
        try:
            threading.Thread(
                target=_refresh_model_training_eligibility_cache,
                name="model-training-eligibility",
                daemon=True,
            ).start()
        except RuntimeError:
            with MODEL_TRAINING_CACHE_LOCK:
                MODEL_TRAINING_ELIGIBILITY_CACHE["refreshing"] = False
    return result


def build_model_training_payload(*, force_eligibility=False):
    """Return public job progress plus the server-side start eligibility gate."""

    overview = model_training_jobs.get_training_overview()
    training_active = bool(overview.get("training_active"))
    eligibility = _cached_model_training_eligibility(
        force=force_eligibility,
        allow_refresh=not training_active,
    )
    jobs_by_model = overview.get("models")
    if not isinstance(jobs_by_model, dict):
        raise model_training_jobs.TrainingJobError(
            "training overview has no model states"
        )
    models = {}
    for model in model_training_jobs.MODEL_NAMES:
        job = jobs_by_model.get(model)
        gate = eligibility.get(model)
        if not isinstance(job, dict) or not isinstance(gate, dict):
            raise model_training_jobs.TrainingJobError(
                f"training state for {model} is unavailable"
            )
        models[model] = {**job, "eligibility": gate}
    return {
        "schema_version": overview.get("schema_version"),
        "updated_at": overview.get("updated_at"),
        "server_time": datetime.now(timezone.utc).isoformat(),
        "active_job_id": overview.get("active_job_id"),
        "training_active": training_active,
        "eligibility_refreshing": bool(
            MODEL_TRAINING_ELIGIBILITY_CACHE.get("refreshing")
        ),
        "models": models,
        "promotion_policy": overview.get(
            "promotion_policy", "quality_gate_then_hot_activation"
        ),
        "activation_available": True,
    }


def update_model_training_from_payload(payload):
    """Dispatch one strictly whitelisted dashboard training action."""

    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    unknown = sorted(set(payload) - {"action", "model", "job_id"})
    if unknown:
        raise ValueError(f"unexpected fields: {', '.join(unknown)}")
    action = payload.get("action")
    model = payload.get("model")
    if action not in {"start", "cancel"}:
        raise ValueError("action must be start or cancel")
    if model not in model_training_jobs.MODEL_NAMES:
        allowed_models = ", ".join(model_training_jobs.MODEL_NAMES)
        raise ValueError(f"model must be one of: {allowed_models}")

    if action == "start":
        if set(payload) != {"action", "model"}:
            raise ValueError("start accepts only action and model")
        model_training_jobs.start_model_training(model)
    else:
        if set(payload) != {"action", "model", "job_id"}:
            raise ValueError("cancel requires action, model and job_id")
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("job_id must be a lowercase 32-character identifier")
        model_training_jobs.cancel_model_training(model, job_id=job_id)
    return build_model_training_payload()


def model_training_request_violation(handler):
    """Return an HTTP error for an unsafe mutating request, otherwise None."""

    content_type = (
        handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "application/json":
        return 415, "Content-Type must be application/json"
    if not secrets.compare_digest(
        handler.headers.get("X-CS2-Action", ""), "model-training"
    ):
        return 403, "missing model training action confirmation"
    if handler.headers.get("Transfer-Encoding"):
        return 400, "chunked request bodies are not accepted"

    fetch_site = handler.headers.get("Sec-Fetch-Site", "").strip().lower()
    if fetch_site not in {"", "none", "same-origin"}:
        return 403, "cross-site model training actions are not allowed"
    origin = handler.headers.get("Origin", "").strip()
    if origin:
        parsed_origin = urlparse(origin)
        request_host = handler.headers.get("Host", "").strip().casefold()
        if (
            parsed_origin.scheme not in {"http", "https"}
            or not request_host
            or parsed_origin.netloc.casefold() != request_host
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.params
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            return 403, "request Origin does not match this dashboard"
    return None


def fetch_model_deal_comparison_page(
    conn,
    offset,
    limit,
    search,
    category,
    listing_type="all",
    sort_mode="seen_new",
    seen_hours=0,
    created_hours=0,
    min_price=None,
    max_price=None,
    confidence="all",
    min_roi=None,
    min_profit=None,
    min_liquidity=None,
    max_float=None,
    min_sticker_value=None,
    opinion_stale_hours=None,
):
    limit = min(max(int(limit), 1), 100)
    offset = min(max(int(offset), 0), MAX_SAFE_PAGE_OFFSET)
    offset -= offset % limit
    search = (search or "").strip()
    category = (category or "all").strip()
    listing_type = (listing_type or "all").strip()
    sort_mode = (sort_mode or "seen_new").strip()
    confidence = (confidence or "all").strip()
    if confidence not in DEAL_CONFIDENCE_FILTERS:
        confidence = "all"
    if opinion_stale_hours is None:
        opinion_stale_hours = deal_opinion_stale_hours()
    try:
        opinion_stale_hours = int(opinion_stale_hours)
    except (TypeError, ValueError):
        opinion_stale_hours = DEFAULT_DEAL_OPINION_STALE_HOURS
    if not 1 <= opinion_stale_hours <= 720:
        opinion_stale_hours = DEFAULT_DEAL_OPINION_STALE_HOURS
    try:
        seen_hours = max(float(seen_hours or 0), 0)
    except (TypeError, ValueError):
        seen_hours = 0
    if not math.isfinite(seen_hours):
        seen_hours = 0
    seen_hours = min(seen_hours, 24 * 365)
    try:
        created_hours = max(float(created_hours or 0), 0)
    except (TypeError, ValueError):
        created_hours = 0
    if not math.isfinite(created_hours):
        created_hours = 0
    created_hours = min(created_hours, 24 * 365)
    min_price_cents = parse_price_filter_cents(min_price)
    max_price_cents = parse_price_filter_cents(max_price)
    min_roi_percent = parse_number_filter(
        min_roi, minimum=-1_000, maximum=100_000
    )
    min_profit_cents = parse_price_filter_cents(min_profit)
    min_liquidity_score = parse_number_filter(
        min_liquidity, minimum=0, maximum=100
    )
    max_float_value = parse_number_filter(max_float, minimum=0, maximum=1)
    min_sticker_value_cents = parse_price_filter_cents(min_sticker_value)
    if (
        min_price_cents is not None
        and max_price_cents is not None
        and min_price_cents > max_price_cents
    ):
        min_price_cents, max_price_cents = max_price_cents, min_price_cents
    now = datetime.now(timezone.utc)
    opinion_cutoff = now - timedelta(hours=opinion_stale_hours)

    if not table_exists(conn, "model_deal_predictions"):
        return {
            "offset": offset,
            "limit": limit,
            "total": 0,
            "rows": [],
            "summary": {
                "all": 0, "almaz": 0, "igor": 0, "nova": 0, "both": 0,
                "all_three": 0, "only_almaz": 0, "only_igor": 0, "only_nova": 0,
                "consensus": 0, "unanimous_three": 0,
                "disagreement": 0, "single_signal": 0,
            },
            "confidence": confidence,
            "min_roi_percent": min_roi_percent,
            "min_profit_cents": min_profit_cents,
            "min_liquidity_score": min_liquidity_score,
            "max_float_value": max_float_value,
            "min_sticker_value_cents": min_sticker_value_cents,
            "opinion_stale_hours": opinion_stale_hours,
        }

    # Keep every current opinion for a listing in the comparison.  Filtering
    # bad/thin rows here used to make a dissenting model look as if it had not
    # produced a prediction at all, which overstated model agreement in the UI.
    prediction_columns = table_columns(conn, "model_deal_predictions")
    where_parts = ["verdict != 'expired'", "last_seen_at >= ?"]
    params = [opinion_cutoff.isoformat()]
    if min_price_cents is not None:
        where_parts.append("buy_price_cents >= ?")
        params.append(min_price_cents)
    if max_price_cents is not None:
        where_parts.append("buy_price_cents <= ?")
        params.append(max_price_cents)
    if search:
        pattern = f"%{search}%"
        search_parts = ["skin_name LIKE ?", "source_sticker_name LIKE ?", "listing_id LIKE ?"]
        params.extend([pattern, pattern, pattern])
        if "source_name" in prediction_columns:
            search_parts.append("source_name LIKE ?")
            params.append(pattern)
        where_parts.append(f"({' OR '.join(search_parts)})")

    where_sql = f"WHERE {' AND '.join(where_parts)}"
    prediction_rows = [
        dict(row)
        for row in conn.execute(f"""
            SELECT *
            FROM model_deal_predictions
            {where_sql}
            ORDER BY last_seen_at DESC, roi_percent DESC, profit_cents DESC
        """, params)
    ]

    def finite_float_value(value):
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def finite_int_value(value):
        number = finite_float_value(value)
        return int(number) if number is not None else None

    def model_score(model):
        roi = finite_float_value(model.get("roi_percent"))
        profit = finite_int_value(model.get("profit_cents"))
        return (
            roi if roi is not None else -math.inf,
            profit if profit is not None else -math.inf,
        )

    def max_model_value(models, field, converter):
        values = [converter(model.get(field)) for model in models]
        finite_values = [value for value in values if value is not None]
        return max(finite_values) if finite_values else None

    grouped = {}
    for prediction_row in prediction_rows:
        listing_id = prediction_row["listing_id"]
        item = grouped.setdefault(listing_id, {
            "listing_id": listing_id,
            "models": {},
            "best": None,
            "payload": {},
            "stickers": [],
        })

        raw_payload = prediction_row.pop("deal_payload_json", None)
        raw_prediction = prediction_row.pop("prediction_json", None)
        payload = {}
        prediction_payload = {}
        if raw_payload:
            try:
                payload = json.loads(raw_payload)
            except (TypeError, json.JSONDecodeError):
                payload = {}
        if raw_prediction:
            try:
                prediction_payload = json.loads(raw_prediction)
            except (TypeError, json.JSONDecodeError):
                prediction_payload = {}

        model_name = prediction_row["model_name"]
        item["models"][model_name] = prediction_row

        if payload and not item["payload"]:
            item["payload"] = payload
            item["stickers"] = (
                prediction_payload.get("stickers")
                or payload.get("stickers")
                or []
            )

        if prediction_row.get("verdict") in DEAL_POSITIVE_VERDICTS:
            best = item["best"]
            candidate_score = model_score(prediction_row)
            if best is None or candidate_score > model_score(best):
                item["best"] = prediction_row

    seen_cutoff = now - timedelta(hours=seen_hours) if seen_hours else None
    created_cutoff = now - timedelta(hours=created_hours) if created_hours else None
    rows = []
    summary = {
        "all": 0, "almaz": 0, "igor": 0, "nova": 0, "both": 0,
        "all_three": 0, "only_almaz": 0, "only_igor": 0, "only_nova": 0,
        "consensus": 0, "unanimous_three": 0,
        "disagreement": 0, "single_signal": 0,
    }
    for item in grouped.values():
        model_names = set(item["models"])
        positive_model_names = {
            model_name
            for model_name, model in item["models"].items()
            if model.get("verdict") in DEAL_POSITIVE_VERDICTS
        }
        if not positive_model_names:
            continue

        dissenting_model_names = model_names - positive_model_names
        if {"almaz", "igor", "nova"}.issubset(positive_model_names):
            item_category = "all_three"
        elif {"almaz", "igor"}.issubset(positive_model_names):
            item_category = "both"
        elif positive_model_names == {"almaz"}:
            item_category = "only_almaz"
        elif positive_model_names == {"igor"}:
            item_category = "only_igor"
        elif positive_model_names == {"nova"}:
            item_category = "only_nova"
        else:
            item_category = "+".join(sorted(positive_model_names)) or "all"

        if positive_model_names == DEAL_MODEL_NAMES and not dissenting_model_names:
            confidence_level = "unanimous_three"
        elif len(positive_model_names) >= 2 and not dissenting_model_names:
            confidence_level = "consensus"
        elif dissenting_model_names:
            confidence_level = "disagreement"
        else:
            confidence_level = "single_signal"

        best = item["best"] or {}
        payload = item["payload"] or {}
        model_rows = list(item["models"].values())
        roi_values = [finite_float_value(model.get("roi_percent")) for model in model_rows]
        profit_values = [finite_int_value(model.get("profit_cents")) for model in model_rows]
        prediction_values = [
            finite_int_value(model.get("predicted_price_cents")) for model in model_rows
        ]
        complete_roi_values = all(value is not None for value in roi_values)
        complete_profit_values = all(value is not None for value in profit_values)
        liquidity_values = [
            finite_float_value(model.get("liquidity_score"))
            for model in model_rows
        ]
        complete_liquidity_values = all(
            value is not None for value in liquidity_values
        )
        complete_prediction_values = all(value is not None for value in prediction_values)
        buy_price_cents = finite_int_value(best.get("buy_price_cents"))
        prediction_spread_percent = None
        if (
            complete_prediction_values
            and len(prediction_values) >= 2
            and buy_price_cents is not None
            and buy_price_cents > 0
        ):
            prediction_spread_percent = (
                (max(prediction_values) - min(prediction_values))
                / buy_price_cents
                * 100
            )
        screenshot_id = (
            best.get("item_cs2_screenshot_id")
            or payload.get("item_cs2_screenshot_id")
            or payload.get("cs2_screenshot_id")
            or payload.get("screenshot_id")
        )
        item.update({
            "category": item_category,
            "confidence": confidence_level,
            "positive_model_count": len(positive_model_names),
            "active_model_count": len(model_names),
            "dissenting_models": sorted(dissenting_model_names),
            "skin_name": best.get("skin_name") or payload.get("name"),
            "source_sticker_id": best.get("source_sticker_id"),
            "source_sticker_name": best.get("source_sticker_name"),
            "source_type": best.get("source_type") or "sticker",
            "source_name": best.get("source_name") or best.get("source_sticker_name"),
            "target_key": best.get("target_key"),
            "buy_price_cents": best.get("buy_price_cents"),
            "float_value": best.get("float_value") if best.get("float_value") is not None else payload.get("float_value"),
            "paint_seed": best.get("paint_seed") if best.get("paint_seed") is not None else payload.get("paint_seed"),
            "paint_index": best.get("paint_index") if best.get("paint_index") is not None else payload.get("paint_index"),
            "listing_created_at": (
                best.get("listing_created_at")
                or payload.get("created_at")
                or payload.get("createdAt")
                or payload.get("listed_at")
                or payload.get("listedAt")
            ),
            "listing_type": normalize_listing_type(payload),
            "last_seen_at": best.get("last_seen_at"),
            "csfloat_url": best.get("csfloat_url"),
            "inspect_link": best.get("inspect_link") or payload.get("inspect_link"),
            "item_icon_url": best.get("item_icon_url") or payload.get("item_icon_url"),
            "screenshot_url": (
                best.get("screenshot_url")
                or payload.get("screenshot_url")
                or build_csfloat_screenshot_url(screenshot_id)
            ),
            "max_roi_percent": max_model_value(model_rows, "roi_percent", finite_float_value),
            "max_profit_cents": max_model_value(model_rows, "profit_cents", finite_int_value),
            "max_liquidity_score": max_model_value(
                model_rows, "liquidity_score", finite_float_value
            ),
            "max_sticker_value_cents": max_model_value(
                model_rows, "sticker_value_cents", finite_int_value
            ),
            "conservative_roi_percent": (
                min(roi_values) if model_rows and complete_roi_values else None
            ),
            "conservative_profit_cents": (
                min(profit_values) if model_rows and complete_profit_values else None
            ),
            "conservative_liquidity_score": (
                min(liquidity_values)
                if model_rows and complete_liquidity_values
                else None
            ),
            "prediction_spread_percent": prediction_spread_percent,
        })

        if (
            min_roi_percent is not None
            and (
                item["conservative_roi_percent"] is None
                or item["conservative_roi_percent"] < min_roi_percent
            )
        ):
            continue
        if (
            min_profit_cents is not None
            and (
                item["conservative_profit_cents"] is None
                or item["conservative_profit_cents"] < min_profit_cents
            )
        ):
            continue
        if (
            min_liquidity_score is not None
            and (
                item["conservative_liquidity_score"] is None
                or item["conservative_liquidity_score"] < min_liquidity_score
            )
        ):
            continue
        if (
            max_float_value is not None
            and (
                finite_float_value(item.get("float_value")) is None
                or finite_float_value(item.get("float_value")) > max_float_value
            )
        ):
            continue
        if (
            min_sticker_value_cents is not None
            and (
                item["max_sticker_value_cents"] is None
                or item["max_sticker_value_cents"] < min_sticker_value_cents
            )
        ):
            continue

        if seen_cutoff:
            seen_at = parse_iso_timestamp(item.get("last_seen_at"))
            if not seen_at or seen_at < seen_cutoff:
                continue
        if created_cutoff:
            created_at = parse_iso_timestamp(item.get("listing_created_at"))
            if not created_at or created_at < created_cutoff:
                continue
        if listing_type in {"buy_now", "auction", "unknown"} and item.get("listing_type") != listing_type:
            continue

        summary["all"] += 1
        if "almaz" in positive_model_names:
            summary["almaz"] += 1
        if "igor" in positive_model_names:
            summary["igor"] += 1
        if "nova" in positive_model_names:
            summary["nova"] += 1
        if {"almaz", "igor"}.issubset(positive_model_names):
            summary["both"] += 1
        if {"almaz", "igor", "nova"}.issubset(positive_model_names):
            summary["all_three"] += 1
        if item_category in {"only_almaz", "only_igor", "only_nova"}:
            summary[item_category] += 1
        summary[confidence_level] += 1

        if category == "almaz" and "almaz" not in positive_model_names:
            continue
        if category == "igor" and "igor" not in positive_model_names:
            continue
        if category == "nova" and "nova" not in positive_model_names:
            continue
        if category == "both" and not {"almaz", "igor"}.issubset(positive_model_names):
            continue
        if category == "all_three" and not {"almaz", "igor", "nova"}.issubset(positive_model_names):
            continue
        if category in {"only_almaz", "only_igor", "only_nova"} and item_category != category:
            continue
        if confidence == "consensus" and confidence_level not in {"consensus", "unanimous_three"}:
            continue
        if confidence != "all" and confidence != "consensus" and confidence_level != confidence:
            continue

        rows.append(item)

    def timestamp_score(value):
        parsed = parse_iso_timestamp(value)
        return parsed.timestamp() if parsed else -1

    def number_score(value, missing=-10**18):
        if value is None:
            return missing
        try:
            number = float(value)
        except (TypeError, ValueError):
            return missing
        return number if math.isfinite(number) else missing

    if sort_mode == "conservative_roi":
        rows.sort(key=lambda row: (
            number_score(row.get("conservative_roi_percent")),
            number_score(row.get("conservative_profit_cents")),
            -number_score(row.get("prediction_spread_percent"), 10**18),
            timestamp_score(row.get("last_seen_at")),
        ), reverse=True)
    elif sort_mode == "created_new":
        rows.sort(key=lambda row: (
            timestamp_score(row.get("listing_created_at")),
            timestamp_score(row.get("last_seen_at")),
            number_score(row.get("max_roi_percent")),
        ), reverse=True)
    elif sort_mode == "best_profit":
        rows.sort(key=lambda row: (
            number_score(row.get("max_profit_cents")),
            number_score(row.get("max_roi_percent")),
            timestamp_score(row.get("last_seen_at")),
        ), reverse=True)
    elif sort_mode == "price_low":
        rows.sort(key=lambda row: (
            number_score(row.get("buy_price_cents"), 10**18),
            -number_score(row.get("max_roi_percent")),
            -timestamp_score(row.get("last_seen_at")),
        ))
    elif sort_mode == "price_high":
        rows.sort(key=lambda row: (
            number_score(row.get("buy_price_cents")),
            number_score(row.get("max_profit_cents")),
            timestamp_score(row.get("last_seen_at")),
        ), reverse=True)
    elif sort_mode == "new_price_low":
        rows.sort(key=lambda row: (
            -timestamp_score(row.get("listing_created_at")),
            number_score(row.get("buy_price_cents"), 10**18),
            -number_score(row.get("max_roi_percent")),
        ))
    elif sort_mode == "liquidity_high":
        rows.sort(key=lambda row: (
            number_score(row.get("max_liquidity_score")),
            number_score(row.get("max_roi_percent")),
            timestamp_score(row.get("last_seen_at")),
        ), reverse=True)
    elif sort_mode == "sticker_value_high":
        rows.sort(key=lambda row: (
            number_score(row.get("max_sticker_value_cents")),
            number_score(row.get("max_roi_percent")),
            timestamp_score(row.get("last_seen_at")),
        ), reverse=True)
    elif sort_mode == "float_low":
        rows.sort(key=lambda row: (
            number_score(row.get("float_value"), 10**18),
            -number_score(row.get("max_roi_percent")),
            -timestamp_score(row.get("last_seen_at")),
        ))
    elif sort_mode == "best_roi":
        rows.sort(key=lambda row: (
            number_score(row.get("max_roi_percent")),
            number_score(row.get("max_profit_cents")),
            timestamp_score(row.get("last_seen_at")),
        ), reverse=True)
    else:
        rows.sort(key=lambda row: (
            timestamp_score(row.get("last_seen_at")),
            timestamp_score(row.get("listing_created_at")),
            number_score(row.get("max_roi_percent")),
        ), reverse=True)

    total = len(rows)
    if total:
        offset = min(offset, ((total - 1) // limit) * limit)
    else:
        offset = 0
    page_rows = rows[offset:offset + limit]
    page_listing_ids = [
        row.get("listing_id") for row in page_rows if row.get("listing_id")
    ]
    if page_listing_ids and table_exists(conn, "model_deal_listing_sources"):
        placeholders = ",".join("?" for _ in page_listing_ids)
        sources_by_listing = defaultdict(list)
        for source_row in conn.execute(
            f"""
            SELECT listing_id, source_type, source_name, target_key
            FROM model_deal_listing_sources
            WHERE listing_id IN ({placeholders})
            ORDER BY listing_id, last_seen_at DESC, source_type, target_key
            """,
            page_listing_ids,
        ):
            sources_by_listing[source_row["listing_id"]].append(dict(source_row))
        for row in page_rows:
            row["discovery_sources"] = sources_by_listing.get(row.get("listing_id"), [])
    add_deal_visual_fallbacks(conn, page_rows)
    listing_ids = page_listing_ids
    annotations = deal_intelligence.get_risk_annotations(
        conn,
        listing_ids,
        limit=max(1, min(len(listing_ids), deal_intelligence.MAX_ANNOTATION_ROWS)),
    )
    page_rows = deal_intelligence.attach_risk_annotations(page_rows, annotations)
    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "rows": page_rows,
        "summary": summary,
        "listing_type": listing_type,
        "sort": sort_mode,
        "seen_hours": seen_hours,
        "created_hours": created_hours,
        "min_price_cents": min_price_cents,
        "max_price_cents": max_price_cents,
        "min_roi_percent": min_roi_percent,
        "min_profit_cents": min_profit_cents,
        "min_liquidity_score": min_liquidity_score,
        "max_float_value": max_float_value,
        "min_sticker_value_cents": min_sticker_value_cents,
        "confidence": confidence,
        "opinion_stale_hours": opinion_stale_hours,
    }


def add_deal_visual_fallbacks(conn, rows):
    icon_cache = {}
    screenshot_cache = {}
    sticker_icon_cache = {}

    for row in rows:
        listing_id = row.get("listing_id")
        if listing_id and (not row.get("item_icon_url") or not row.get("screenshot_url")):
            if listing_id not in screenshot_cache:
                sale_row = conn.execute("""
                    SELECT item_icon_url, item_cs2_screenshot_id
                    FROM sales
                    WHERE listing_id = ?
                    LIMIT 1
                """, (listing_id,)).fetchone()
                screenshot_cache[listing_id] = dict(sale_row) if sale_row else {}
            sale_visuals = screenshot_cache.get(listing_id) or {}
            if not row.get("item_icon_url"):
                row["item_icon_url"] = sale_visuals.get("item_icon_url")
            if not row.get("screenshot_url"):
                row["screenshot_url"] = build_csfloat_screenshot_url(sale_visuals.get("item_cs2_screenshot_id"))

        skin_name = row.get("skin_name")
        if skin_name and not row.get("item_icon_url"):
            if skin_name not in icon_cache:
                icon_row = conn.execute("""
                    SELECT item_icon_url
                    FROM sales
                    WHERE name = ?
                        AND item_icon_url IS NOT NULL
                        AND item_icon_url != ''
                    ORDER BY fetched_at DESC
                    LIMIT 1
                """, (skin_name,)).fetchone()
                icon_cache[skin_name] = icon_row["item_icon_url"] if icon_row else None
            row["item_icon_url"] = icon_cache.get(skin_name)

        for sticker in row.get("stickers") or []:
            if not isinstance(sticker, dict) or sticker.get("icon_url"):
                continue
            key = sticker.get("sticker_id") or sticker.get("name")
            if not key:
                continue
            if key not in sticker_icon_cache:
                params = []
                if sticker.get("sticker_id") not in (None, ""):
                    where = "sticker_id = ?"
                    params.append(sticker.get("sticker_id"))
                else:
                    where = "name = ?"
                    params.append(sticker.get("name"))
                icon_row = conn.execute(f"""
                    SELECT icon_url
                    FROM sale_stickers
                    WHERE {where}
                        AND icon_url IS NOT NULL
                        AND icon_url != ''
                    ORDER BY rowid DESC
                    LIMIT 1
                """, params).fetchone()
                sticker_icon_cache[key] = icon_row["icon_url"] if icon_row else None
            sticker["icon_url"] = sticker_icon_cache.get(key)


def fetch_stickers_for_listings(conn, listing_ids):
    if not listing_ids:
        return {}

    sticker_columns = table_columns(conn, "sale_stickers")
    sticker_steam_url_expr = optional_qualified_column_expr(sticker_columns, "st", "steam_market_url", "steam_market_url")
    reference_price_expr = optional_qualified_column_expr(sticker_columns, "st", "reference_price_cents", "reference_price_cents")
    reference_quantity_expr = optional_qualified_column_expr(sticker_columns, "st", "reference_quantity", "reference_quantity")
    offset_x_expr = optional_qualified_column_expr(sticker_columns, "st", "offset_x", "offset_x")
    offset_y_expr = optional_qualified_column_expr(sticker_columns, "st", "offset_y", "offset_y")

    placeholders = ", ".join("?" for _ in listing_ids)
    stickers_by_listing = {listing_id: [] for listing_id in listing_ids}
    sticker_rows = [
        dict(row)
        for row in conn.execute(f"""
            SELECT
                st.listing_id,
                st.sticker_index,
                st.sticker_id,
                st.slot,
                st.name,
                st.icon_url,
                {sticker_steam_url_expr},
                st.wear,
                st.price_cents,
                {reference_price_expr},
                {reference_quantity_expr},
                {offset_x_expr},
                {offset_y_expr}
            FROM sale_stickers st
            WHERE st.listing_id IN ({placeholders})
            ORDER BY st.listing_id, st.sticker_index
        """, listing_ids)
    ]

    for sticker in sticker_rows:
        if not sticker.get("steam_market_url"):
            sticker["steam_market_url"] = steam_market_url(sticker.get("name"))
        stickers_by_listing.setdefault(sticker["listing_id"], []).append(sticker)

    return stickers_by_listing


def median_value(values):
    values = [value for value in values if value is not None]
    if not values:
        return None

    values.sort()
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]

    return (values[middle - 1] + values[middle]) / 2


def fetch_skin_detail(conn, market_hash_name, sales_limit=1000):
    market_hash_name = (market_hash_name or "").strip()
    sales_limit = min(max(int(sales_limit), 1), 2000)

    if not market_hash_name:
        return {
            "name": "",
            "stats": {},
            "schedule": {},
            "daily": [],
            "request_log": [],
            "sales": [],
        }

    sales_columns = table_columns(conn, "sales")
    steam_url_expr = optional_qualified_column_expr(sales_columns, "s", "item_steam_market_url", "item_steam_market_url")
    rarity_color_expr = optional_qualified_column_expr(sales_columns, "s", "item_rarity_color", "item_rarity_color")
    name_color_expr = optional_qualified_column_expr(sales_columns, "s", "item_name_color", "item_name_color")
    background_color_expr = optional_qualified_column_expr(sales_columns, "s", "item_background_color", "item_background_color")
    exterior_expr = optional_qualified_column_expr(sales_columns, "s", "item_exterior_name", "item_exterior_name")
    collection_expr = optional_collection_name_expr(sales_columns)

    stats = row_to_dict(conn.execute("""
        SELECT
            COUNT(*) AS sales_count,
            MIN(price_cents) AS min_price_cents,
            MAX(price_cents) AS max_price_cents,
            AVG(price_cents) AS avg_price_cents,
            MIN(float_value) AS min_float_value,
            MAX(float_value) AS max_float_value,
            AVG(float_value) AS avg_float_value,
            MAX(sold_at) AS latest_sold_at,
            MAX(fetched_at) AS latest_fetched_at,
            MIN(sold_at) AS oldest_sold_at
        FROM sales
        WHERE name = ?
    """, (market_hash_name,)).fetchone())

    price_values = [
        row["price_cents"]
        for row in conn.execute("""
            SELECT price_cents
            FROM sales
            WHERE name = ? AND price_cents IS NOT NULL
            ORDER BY price_cents
        """, (market_hash_name,))
    ]
    stats["median_price_cents"] = median_value(price_values)

    sticker_stats = row_to_dict(conn.execute("""
        WITH per_listing AS (
            SELECT
                s.listing_id,
                COUNT(st.sticker_index) AS sticker_count,
                SUM(COALESCE(st.price_cents, st.reference_price_cents, 0)) AS sticker_value_cents
            FROM sales s
            JOIN sale_stickers st ON st.listing_id = s.listing_id
            WHERE s.name = ?
            GROUP BY s.listing_id
        )
        SELECT
            COUNT(*) AS stickered_sales_count,
            SUM(sticker_count) AS sticker_rows,
            AVG(sticker_value_cents) AS avg_sticker_value_cents,
            MAX(sticker_value_cents) AS max_sticker_value_cents
        FROM per_listing
    """, (market_hash_name,)).fetchone())
    stats.update(sticker_stats)

    daily = [
        dict(row)
        for row in conn.execute("""
            SELECT
                SUBSTR(COALESCE(sold_at, fetched_at), 1, 10) AS day,
                COUNT(*) AS sales_count,
                AVG(price_cents) AS avg_price_cents,
                MIN(price_cents) AS min_price_cents,
                MAX(price_cents) AS max_price_cents,
                AVG(float_value) AS avg_float_value
            FROM sales
            WHERE name = ?
                AND COALESCE(sold_at, fetched_at) IS NOT NULL
            GROUP BY day
            ORDER BY day ASC
        """, (market_hash_name,))
    ]

    schedule = {}
    if table_exists(conn, "skin_schedule"):
        schedule = row_to_dict(conn.execute("""
            SELECT
                market_hash_name,
                request_count,
                total_sales_returned,
                total_new_sales,
                last_sales_count,
                last_new_sales_count,
                avg_sales_count,
                avg_new_sales_count,
                interval_seconds,
                interval_reason,
                last_fetch_at,
                next_fetch_at,
                last_sold_at
            FROM skin_schedule
            WHERE market_hash_name = ?
        """, (market_hash_name,)).fetchone())
        if schedule:
            decorate_schedule_rows([schedule])

    request_log = []
    if table_exists(conn, "parser_request_log"):
        request_log = [
            dict(row)
            for row in conn.execute("""
                SELECT
                    requested_at,
                    sales_count,
                    new_sales_count,
                    interval_seconds,
                    interval_reason,
                    next_fetch_at
                FROM parser_request_log
                WHERE market_hash_name = ?
                ORDER BY requested_at DESC, id DESC
                LIMIT 20
            """, (market_hash_name,))
        ]
        now = int(time.time())
        for row in request_log:
            row["requested_at_iso"] = unix_seconds_to_iso(row.get("requested_at"))
            row["next_fetch_at_iso"] = unix_seconds_to_iso(row.get("next_fetch_at"))
            row["next_fetch_seconds_left"] = (
                int(row["next_fetch_at"]) - now
                if row.get("next_fetch_at") is not None
                else None
            )

    sales_rows = [
        dict(row)
        for row in conn.execute(f"""
            SELECT
                s.listing_id,
                s.name,
                s.item_icon_url,
                {steam_url_expr},
                {rarity_color_expr},
                {name_color_expr},
                {background_color_expr},
                {exterior_expr},
                {collection_expr},
                s.price_cents,
                s.reference_base_price,
                s.reference_predicted_price,
                s.reference_float_factor,
                s.reference_quantity,
                s.float_value,
                s.paint_seed,
                s.paint_index,
                s.inspect_link,
                s.serialized_inspect,
                s.state,
                s.created_at,
                s.sold_at,
                s.fetched_at,
                s.sale_type,
                s.watchers,
                s.item_asset_id,
                s.item_quality,
                s.item_rarity,
                s.item_rarity_name,
                s.item_wear_name,
                s.item_is_souvenir,
                s.item_is_stattrak
            FROM sales s
            WHERE s.name = ?
            ORDER BY COALESCE(s.sold_at, s.fetched_at) DESC, s.listing_id DESC
            LIMIT ?
        """, (market_hash_name, sales_limit))
    ]
    sales_rows.reverse()
    add_market_urls(sales_rows, "name", "item_steam_market_url")

    listing_ids = [row["listing_id"] for row in sales_rows]
    stickers_by_listing = fetch_stickers_for_listings(conn, listing_ids)
    for row in sales_rows:
        stickers = stickers_by_listing.get(row["listing_id"], [])
        row["stickers"] = stickers
        row["stickers_count"] = len(stickers)
        row["stickers_total_cents"] = sum(
            int(sticker.get("price_cents") or sticker.get("reference_price_cents") or 0)
            for sticker in stickers
        )

    return {
        "name": market_hash_name,
        "stats": stats,
        "schedule": schedule,
        "daily": daily,
        "request_log": request_log,
        "sales": sales_rows,
    }


def sticker_filter_sql(sticker_id, sticker_name):
    try:
        parsed_id = int(sticker_id) if sticker_id not in (None, "") else None
    except (TypeError, ValueError):
        parsed_id = None

    if parsed_id is not None:
        return "st.sticker_id = ?", [parsed_id], parsed_id

    return "st.name = ?", [(sticker_name or "").strip()], None


def fetch_sticker_detail(conn, sticker_name="", sticker_id="", sales_limit=1000):
    sticker_name = (sticker_name or "").strip()
    where_sql, params, parsed_id = sticker_filter_sql(sticker_id, sticker_name)
    sales_limit = min(max(int(sales_limit), 1), 2000)

    if not sticker_name and parsed_id is None:
        return {
            "name": "",
            "sticker_id": None,
            "stats": {},
            "top_skins": [],
            "daily": [],
            "sales": [],
        }

    matched_row = conn.execute(f"""
        SELECT
            st.sticker_id,
            st.name,
            MAX(st.icon_url) AS icon_url,
            MAX(st.steam_market_url) AS steam_market_url
        FROM sale_stickers st
        WHERE {where_sql}
        GROUP BY st.sticker_id, st.name
        ORDER BY COUNT(*) DESC
        LIMIT 1
    """, params).fetchone()

    resolved_name = matched_row["name"] if matched_row else sticker_name
    resolved_id = matched_row["sticker_id"] if matched_row else parsed_id
    resolved_icon_url = matched_row["icon_url"] if matched_row else None
    resolved_steam_url = matched_row["steam_market_url"] if matched_row else steam_market_url(resolved_name)

    stats = row_to_dict(conn.execute(f"""
        WITH matched_listing AS (
            SELECT
                s.listing_id,
                s.name,
                s.price_cents,
                s.float_value,
                s.sold_at,
                s.fetched_at,
                COUNT(st.sticker_index) AS matching_stickers_count,
                SUM(COALESCE(st.price_cents, st.reference_price_cents, 0)) AS matching_stickers_total_cents
            FROM sale_stickers st
            JOIN sales s ON s.listing_id = st.listing_id
            WHERE {where_sql}
            GROUP BY s.listing_id
        )
        SELECT
            SUM(matching_stickers_count) AS sticker_rows,
            COUNT(*) AS listing_count,
            COUNT(DISTINCT name) AS skin_count,
            AVG(price_cents) AS avg_skin_price_cents,
            MIN(price_cents) AS min_skin_price_cents,
            MAX(price_cents) AS max_skin_price_cents,
            AVG(float_value) AS avg_float_value,
            AVG(matching_stickers_total_cents) AS avg_matching_sticker_value_cents,
            MAX(matching_stickers_total_cents) AS max_matching_sticker_value_cents,
            MAX(sold_at) AS latest_sold_at,
            MIN(sold_at) AS oldest_sold_at,
            MAX(fetched_at) AS latest_fetched_at
        FROM matched_listing
    """, params).fetchone())

    median_prices = [
        row["price_cents"]
        for row in conn.execute(f"""
            WITH matched_listing AS (
                SELECT
                    s.listing_id,
                    s.price_cents
                FROM sale_stickers st
                JOIN sales s ON s.listing_id = st.listing_id
                WHERE {where_sql}
                GROUP BY s.listing_id
            )
            SELECT price_cents
            FROM matched_listing
            WHERE price_cents IS NOT NULL
            ORDER BY price_cents
        """, params)
    ]
    stats["median_skin_price_cents"] = median_value(median_prices)

    avg_sticker_price_row = conn.execute(f"""
        SELECT AVG(COALESCE(st.price_cents, st.reference_price_cents)) AS avg_sticker_price_cents
        FROM sale_stickers st
        WHERE {where_sql}
            AND COALESCE(st.price_cents, st.reference_price_cents) IS NOT NULL
    """, params).fetchone()
    stats["avg_sticker_price_cents"] = avg_sticker_price_row["avg_sticker_price_cents"] if avg_sticker_price_row else None

    sales_columns = table_columns(conn, "sales")
    steam_url_expr = optional_qualified_column_expr(sales_columns, "s", "item_steam_market_url", "item_steam_market_url")
    rarity_color_expr = optional_qualified_column_expr(sales_columns, "s", "item_rarity_color", "item_rarity_color")
    name_color_expr = optional_qualified_column_expr(sales_columns, "s", "item_name_color", "item_name_color")

    top_skins = [
        dict(row)
        for row in conn.execute(f"""
            WITH matched_listing AS (
                SELECT
                    s.listing_id,
                    s.name,
                    s.item_icon_url,
                    {steam_url_expr},
                    {rarity_color_expr},
                    {name_color_expr},
                    s.price_cents,
                    s.sold_at,
                    COUNT(st.sticker_index) AS matching_stickers_count
                FROM sale_stickers st
                JOIN sales s ON s.listing_id = st.listing_id
                WHERE {where_sql}
                GROUP BY s.listing_id
            )
            SELECT
                name AS skin_name,
                MAX(item_icon_url) AS item_icon_url,
                MAX(item_steam_market_url) AS item_steam_market_url,
                MAX(item_rarity_color) AS item_rarity_color,
                MAX(item_name_color) AS item_name_color,
                COUNT(*) AS listing_count,
                SUM(matching_stickers_count) AS sticker_rows,
                AVG(price_cents) AS avg_price_cents,
                MAX(sold_at) AS latest_sold_at
            FROM matched_listing
            GROUP BY name
            ORDER BY listing_count DESC, sticker_rows DESC, avg_price_cents DESC, skin_name ASC
            LIMIT 200
        """, params)
    ]
    add_market_urls(top_skins, "skin_name", "item_steam_market_url")

    daily = [
        dict(row)
        for row in conn.execute(f"""
            WITH matched_listing AS (
                SELECT
                    s.listing_id,
                    s.price_cents,
                    s.sold_at,
                    s.fetched_at,
                    COUNT(st.sticker_index) AS matching_stickers_count
                FROM sale_stickers st
                JOIN sales s ON s.listing_id = st.listing_id
                WHERE {where_sql}
                GROUP BY s.listing_id
            )
            SELECT
                SUBSTR(COALESCE(sold_at, fetched_at), 1, 10) AS day,
                COUNT(*) AS listing_count,
                SUM(matching_stickers_count) AS sticker_rows,
                AVG(price_cents) AS avg_price_cents,
                MIN(price_cents) AS min_price_cents,
                MAX(price_cents) AS max_price_cents
            FROM matched_listing
            WHERE COALESCE(sold_at, fetched_at) IS NOT NULL
            GROUP BY day
            ORDER BY day ASC
        """, params)
    ]

    sales_rows = [
        dict(row)
        for row in conn.execute(f"""
            WITH matched_listing AS (
                SELECT
                    s.listing_id,
                    COUNT(st.sticker_index) AS matching_stickers_count,
                    SUM(COALESCE(st.price_cents, st.reference_price_cents, 0)) AS matching_stickers_total_cents
                FROM sale_stickers st
                JOIN sales s ON s.listing_id = st.listing_id
                WHERE {where_sql}
                GROUP BY s.listing_id
            )
            SELECT
                s.listing_id,
                s.name,
                s.item_icon_url,
                {steam_url_expr},
                {rarity_color_expr},
                {name_color_expr},
                s.price_cents,
                s.reference_base_price,
                s.reference_predicted_price,
                s.reference_float_factor,
                s.reference_quantity,
                s.float_value,
                s.paint_seed,
                s.paint_index,
                s.inspect_link,
                s.serialized_inspect,
                s.state,
                s.created_at,
                s.sold_at,
                s.fetched_at,
                s.sale_type,
                s.watchers,
                s.item_asset_id,
                s.item_quality,
                s.item_rarity,
                s.item_rarity_name,
                s.item_wear_name,
                s.item_is_souvenir,
                s.item_is_stattrak,
                ml.matching_stickers_count,
                ml.matching_stickers_total_cents
            FROM matched_listing ml
            JOIN sales s ON s.listing_id = ml.listing_id
            ORDER BY COALESCE(s.sold_at, s.fetched_at) DESC, s.listing_id DESC
            LIMIT ?
        """, [*params, sales_limit])
    ]
    sales_rows.reverse()
    add_market_urls(sales_rows, "name", "item_steam_market_url")

    listing_ids = [row["listing_id"] for row in sales_rows]
    stickers_by_listing = fetch_stickers_for_listings(conn, listing_ids)
    for row in sales_rows:
        stickers = stickers_by_listing.get(row["listing_id"], [])
        row["stickers"] = stickers
        row["stickers_count"] = len(stickers)
        row["stickers_total_cents"] = sum(
            int(sticker.get("price_cents") or sticker.get("reference_price_cents") or 0)
            for sticker in stickers
        )

    return {
        "name": resolved_name,
        "sticker_id": resolved_id,
        "icon_url": resolved_icon_url,
        "steam_market_url": resolved_steam_url,
        "stats": stats,
        "top_skins": top_skins,
        "daily": daily,
        "sales": sales_rows,
    }


def empty_dashboard_static_payload():
    return {
        "top_stickers": [],
        "top_weapons": [],
        "weapons": [],
        "tournaments": [],
        "static_generated_at": None,
        "static_refreshing": True,
    }


def build_dashboard_static_payload():
    with connect_db() as conn:
        payload = {
            "top_stickers": fetch_top_stickers(conn, 30),
            "top_weapons": fetch_top_weapons(conn, 30),
            "weapons": fetch_weapons(conn),
            "tournaments": fetch_tournaments(conn),
            "static_generated_at": datetime.now(timezone.utc).isoformat(),
            "static_refreshing": False,
        }

    return payload


def get_dashboard_static_payload():
    cache_key = "dashboard_static"
    cached, age = cache_get_with_age(cache_key)
    stale = cached is None or age is None or age > DASHBOARD_STATIC_CACHE_TTL_SECONDS

    if stale:
        start_background_refresh(cache_key, build_dashboard_static_payload)

    if cached:
        payload = dict(cached)
        payload["static_refreshing"] = stale
        return payload

    return empty_dashboard_static_payload()


def build_dashboard_payload():
    status = read_json_file(STATUS_PATH, {})

    payload = {
        "db_path": DB_PATH,
        "generated_at": status.get("updated_at"),
        "status": status,
        "progress": {"total_skins": load_total_skins()},
        "overview": {},
        "recent_sales": [],
        "schedule_overview": {},
        "schedule_rows": [],
        "request_log": {},
        "model_freshness": {"current_rows": 0, "models": []},
    }

    if not payload["generated_at"]:
        from datetime import datetime, timezone
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()

    if not Path(DB_PATH).exists():
        payload["error"] = f"DB not found: {DB_PATH}"
        payload.update(empty_dashboard_static_payload())
        return payload

    payload.update(get_dashboard_static_payload())

    try:
        with connect_db() as conn:
            payload["progress"] = fetch_progress(conn)
            payload["overview"] = fetch_overview(conn)
            payload["model_freshness"] = build_model_freshness(
                conn,
                payload["overview"].get("sales_count", 0),
            )
            payload["recent_sales"] = fetch_recent_sales(conn, 20)
            payload["schedule_overview"] = fetch_schedule_overview(conn)
            payload["schedule_rows"] = fetch_schedule_rows(conn, 40)
            payload["request_log"] = fetch_request_log_summary(conn)
    except sqlite3.Error as exc:
        payload["error"] = str(exc)

    return payload


def build_weapon_skins_payload(weapon_name):
    cache_key = ("weapon_skins", weapon_name)
    cached = cache_get(cache_key, DETAIL_CACHE_TTL_SECONDS)
    if cached:
        return cached

    payload = {
        "weapon": weapon_name,
        "count": 0,
        "skins": [],
    }

    if not weapon_name:
        return payload

    if not Path(DB_PATH).exists():
        payload["error"] = f"DB not found: {DB_PATH}"
        return payload

    try:
        with connect_db() as conn:
            payload["skins"] = fetch_weapon_skins(conn, weapon_name)
            payload["count"] = len(payload["skins"])
    except sqlite3.Error as exc:
        payload["error"] = str(exc)

    return cache_set(cache_key, payload)


def build_tournament_stickers_payload(tournament):
    cache_key = ("tournament_stickers", tournament)
    cached = cache_get(cache_key, DETAIL_CACHE_TTL_SECONDS)
    if cached:
        return cached

    payload = {
        "tournament": tournament,
        "count": 0,
        "stickers": [],
    }

    if not tournament:
        return payload

    if not Path(DB_PATH).exists():
        payload["error"] = f"DB not found: {DB_PATH}"
        return payload

    try:
        with connect_db() as conn:
            payload["stickers"] = fetch_tournament_stickers(conn, tournament)
            payload["count"] = len(payload["stickers"])
    except sqlite3.Error as exc:
        payload["error"] = str(exc)

    return cache_set(cache_key, payload)


def build_skin_detail_payload(market_hash_name):
    market_hash_name = (market_hash_name or "").strip()
    cache_key = ("skin_detail", market_hash_name)
    cached = cache_get(cache_key, DETAIL_CACHE_TTL_SECONDS)
    if cached:
        return cached

    if not market_hash_name:
        return {
            "name": "",
            "stats": {},
            "schedule": {},
            "daily": [],
            "request_log": [],
            "sales": [],
        }

    if not Path(DB_PATH).exists():
        return {"name": market_hash_name, "error": f"DB not found: {DB_PATH}"}

    try:
        with connect_db() as conn:
            payload = fetch_skin_detail(conn, market_hash_name)
    except sqlite3.Error as exc:
        payload = {"name": market_hash_name, "error": str(exc)}

    return cache_set(cache_key, payload)


def build_sticker_detail_payload(sticker_name, sticker_id):
    sticker_name = (sticker_name or "").strip()
    sticker_id = (sticker_id or "").strip()
    cache_key = ("sticker_detail", sticker_id, sticker_name)
    cached = cache_get(cache_key, DETAIL_CACHE_TTL_SECONDS)
    if cached:
        return cached

    if not sticker_name and not sticker_id:
        return {
            "name": "",
            "sticker_id": None,
            "stats": {},
            "top_skins": [],
            "daily": [],
            "sales": [],
        }

    if not Path(DB_PATH).exists():
        return {"name": sticker_name, "sticker_id": sticker_id or None, "error": f"DB not found: {DB_PATH}"}

    try:
        with connect_db() as conn:
            payload = fetch_sticker_detail(conn, sticker_name, sticker_id)
    except sqlite3.Error as exc:
        payload = {"name": sticker_name, "sticker_id": sticker_id or None, "error": str(exc)}

    return cache_set(cache_key, payload)


def _bounded_strategy_int(value, *, default, minimum, maximum, name):
    if value in (None, ""):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _strategy_candidate_summary(candidates):
    risk_counts = {"low": 0, "medium": 0, "high": 0, "unknown": 0}
    risk_values = []
    sale_probabilities = []
    conservative_profit_cents = 0
    for item in candidates:
        risk = item.get("risk") if isinstance(item.get("risk"), dict) else {}
        ensemble = (
            item.get("ensemble_prediction")
            if isinstance(item.get("ensemble_prediction"), dict)
            else {}
        )
        sale = (
            item.get("sale_estimate")
            if isinstance(item.get("sale_estimate"), dict)
            else {}
        )
        level = str(risk.get("level") or "unknown").lower()
        risk_counts[level if level in risk_counts else "unknown"] += 1
        try:
            risk_score = float(risk.get("score"))
            if math.isfinite(risk_score):
                risk_values.append(risk_score)
        except (TypeError, ValueError):
            pass
        try:
            probability = float(sale.get("within_30d"))
            if math.isfinite(probability):
                sale_probabilities.append(probability)
        except (TypeError, ValueError):
            pass
        try:
            conservative_profit_cents += int(
                ensemble.get("conservative_profit_cents") or 0
            )
        except (TypeError, ValueError):
            pass
    return {
        "candidate_count": len(candidates),
        "risk_levels": risk_counts,
        "average_risk_score": (
            sum(risk_values) / len(risk_values) if risk_values else None
        ),
        "average_sell_probability_30d": (
            sum(sale_probabilities) / len(sale_probabilities)
            if sale_probabilities
            else None
        ),
        "total_conservative_profit_cents": conservative_profit_cents,
    }


def _unavailable_trend_payload(*, requested_name=None, reason=None):
    return {
        "status": "unavailable",
        "reason": reason or "trend store is not available yet",
        "schema_version": None,
        "model_version": None,
        "as_of_day": None,
        "generated_at": None,
        "requested_name": requested_name,
        "items": [],
    }


def _unavailable_backtest_payload(reason=None):
    return {
        "status": "unavailable",
        "reason": reason or "precomputed backtest is not available yet",
        "report_id": None,
        "config": None,
        "metrics": {},
    }


def build_liquid_market_dashboard_payload(
    *,
    max_price_cents=1_000,
    min_sales_per_day=1.0,
    min_liquidity_score=0.0,
    search="",
    sort="liquidity",
    limit=50,
    live_age_hours=24,
):
    cache_key = (
        "liquid_market",
        str(max_price_cents),
        str(min_sales_per_day),
        str(min_liquidity_score),
        str(search or "").strip().casefold(),
        str(sort or ""),
        str(limit),
        str(live_age_hours),
    )
    cached = cache_get(cache_key, MARKET_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    payload = market_analytics.build_liquid_market_payload(
        DB_PATH,
        TREND_DB_PATH,
        SKINS_PATH,
        max_price_cents=max_price_cents,
        min_sales_per_day=min_sales_per_day,
        min_liquidity_score=min_liquidity_score,
        search=search,
        sort=sort,
        limit=limit,
        live_age_hours=live_age_hours,
    )
    try:
        payload["scanner"] = build_scanner_control_payload()
    except (OSError, ValueError, scanner_control.ScannerControlError):
        payload["scanner"] = {"health": "unavailable"}
    return cache_set(cache_key, payload)


def _verified_active_knife_generation(project_dir=None):
    """Describe a knife generation only after its immutable files verify."""

    project = Path(project_dir or app_config.PROJECT_DIR)
    result = {
        "dedicated_model_available": False,
        "activation_state": "not_activated",
        "active_bundle_version": None,
        "activated_at": None,
        "generation_error": None,
    }
    try:
        pointer = model_generations.read_active_pointer(
            "knives", project_dir=project
        )
        if pointer is None:
            return result
        bundle_dir = model_generations.active_or_legacy_bundle_dir(
            "knives",
            project / "KNIFEMODEL" / "artifacts",
            project_dir=project,
            verify_files=True,
        )
        metrics = read_json_file(
            bundle_dir / "knife_price_model_metrics.json", {}
        )
        if (
            not isinstance(metrics, dict)
            or metrics.get("model_name") != "knives"
            or metrics.get("item_kind") != ITEM_KIND_KNIFE
        ):
            raise model_generations.GenerationError(
                "active knife generation has the wrong metrics identity"
            )
        artifact = bundle_dir / "knife_price_model.joblib"
        if not artifact.is_file() or artifact.is_symlink():
            raise model_generations.GenerationError(
                "active knife generation artifact is unavailable"
            )
    except (OSError, model_generations.GenerationError) as exc:
        result["activation_state"] = "verification_failed"
        result["generation_error"] = str(exc)
        return result

    result.update(
        {
            "dedicated_model_available": True,
            "activation_state": "activated",
            "active_bundle_version": pointer["generation_id"],
            "activated_at": pointer.get("activated_at"),
        }
    )
    return result


def _verified_active_glove_generation(project_dir=None):
    """Describe a glove generation only after its immutable files verify."""

    project = Path(project_dir or app_config.PROJECT_DIR)
    result = {
        "dedicated_model_available": False,
        "activation_state": "not_activated",
        "active_bundle_version": None,
        "activated_at": None,
        "generation_error": None,
    }
    try:
        pointer = model_generations.read_active_pointer(
            "gloves", project_dir=project
        )
        if pointer is None:
            return result
        bundle_dir = model_generations.active_or_legacy_bundle_dir(
            "gloves",
            project / "GLOVEMODEL" / "artifacts",
            project_dir=project,
            verify_files=True,
        )
        metrics = read_json_file(
            bundle_dir / "glove_price_model_metrics.json", {}
        )
        if (
            not isinstance(metrics, dict)
            or metrics.get("model_name") != "gloves"
            or metrics.get("item_kind") != ITEM_KIND_GLOVE
        ):
            raise model_generations.GenerationError(
                "active glove generation has the wrong metrics identity"
            )
        artifact = bundle_dir / "glove_price_model.joblib"
        if not artifact.is_file() or artifact.is_symlink():
            raise model_generations.GenerationError(
                "active glove generation artifact is unavailable"
            )
    except (OSError, model_generations.GenerationError) as exc:
        result["activation_state"] = "verification_failed"
        result["generation_error"] = str(exc)
        return result

    result.update(
        {
            "dedicated_model_available": True,
            "activation_state": "activated",
            "active_bundle_version": pointer["generation_id"],
            "activated_at": pointer.get("activated_at"),
        }
    )
    return result


def _knife_model_dashboard_status(base_state):
    generation = _verified_active_knife_generation()
    overview_error = None
    try:
        overview = model_training_jobs.get_training_overview(recover=False)
    except (OSError, ValueError, model_training_jobs.TrainingJobError) as exc:
        overview = {"models": {}, "training_active": False}
        overview_error = type(exc).__name__

    jobs = overview.get("models")
    if not isinstance(jobs, dict):
        jobs = {}
    job = jobs.get("knives")
    if not isinstance(job, dict):
        job = {}
    training_active = bool(overview.get("training_active"))
    try:
        eligibility_by_model = _cached_model_training_eligibility(
            allow_refresh=not training_active
        )
    except (OSError, ValueError, model_training_jobs.TrainingJobError):
        eligibility_by_model = {}
    eligibility = eligibility_by_model.get("knives")
    if not isinstance(eligibility, dict):
        eligibility = _pending_model_training_eligibility()["knives"]

    job_state = str(job.get("state") or "unavailable")
    job_active = job_state in model_training_jobs.ACTIVE_STATES
    candidate_ready = bool(job.get("candidate_ready_for_review"))
    training_action_available = bool(
        eligibility.get("eligible")
        and not training_active
        and not job_active
        and not candidate_ready
        and overview_error is None
    )
    if job_active:
        state = (
            "retraining"
            if generation["dedicated_model_available"]
            else "training"
        )
    elif generation["dedicated_model_available"]:
        state = "active"
    elif candidate_ready:
        state = "candidate_ready"
    elif eligibility.get("eligible"):
        state = "ready_to_train"
    elif generation["activation_state"] == "verification_failed":
        state = "unavailable"
    else:
        state = (
            base_state
            if base_state in {"collecting_data", "unavailable"}
            else "collecting_data"
        )

    public_job = {
        key: job.get(key)
        for key in (
            "state",
            "phase",
            "progress_percent",
            "eta_seconds",
            "can_cancel",
            "candidate_ready_for_review",
            "activation_state",
            "rejection_kind",
        )
        if key in job
    }
    public_eligibility = {
        key: eligibility.get(key)
        for key in (
            "bootstrap",
            "eligible",
            "reasons",
            "trained_rows",
            "current_rows",
            "new_rows",
            "growth_percent",
            "runtime_valid_new_rows",
            "trainer_available",
            "thresholds",
        )
        if key in eligibility
    }
    return {
        "model_id": "knives",
        "item_kind": ITEM_KIND_KNIFE,
        "state": state,
        **generation,
        "training_action_available": training_action_available,
        "training_active": job_active,
        "training_blocked_by_other_model": bool(training_active and not job_active),
        "training_job": public_job,
        "eligibility": public_eligibility,
        "eligibility_refreshing": "eligibility_refreshing"
        in (eligibility.get("reasons") or []),
        "training_status_error": overview_error,
        # Knife cards remain observed history after activation. Predictions are
        # produced only through the explicit knife-model inference endpoint.
        "forecast_used": False,
        "fallback": "observed_market_history",
        "observed_horizons_days": [7, 30],
    }


def build_knives_dashboard_payload(
    *,
    search="",
    knife_type="",
    finish="",
    phase="",
    wear="",
    collection="",
    min_price=None,
    max_price=None,
    min_liquidity=0,
    direction="all",
    sort="liquidity",
    limit=100,
    offset=0,
):
    cache_key = (
        "knives_market_v1",
        str(search or "").strip().casefold(),
        str(knife_type or "").strip(),
        str(finish or "").strip(),
        str(phase or "").strip(),
        str(wear or "").strip(),
        str(collection or "").strip(),
        str(min_price or ""),
        str(max_price or ""),
        str(min_liquidity or "0"),
        str(direction or "all"),
        str(sort or "liquidity"),
        str(limit or "100"),
        str(offset or "0"),
    )
    cached = cache_get(cache_key, MARKET_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    payload = knives_dashboard.build_knives_payload(
        DB_PATH,
        TREND_DB_PATH,
        search=search,
        knife_type=knife_type,
        finish=finish,
        phase=phase,
        wear=wear,
        collection=collection,
        min_price=min_price,
        max_price=max_price,
        min_liquidity=min_liquidity,
        direction=direction,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    base_model_status = payload.get("model_status")
    base_state = (
        base_model_status.get("state")
        if isinstance(base_model_status, dict)
        else "collecting_data"
    )
    payload["model_status"] = _knife_model_dashboard_status(base_state)
    return cache_set(cache_key, payload)


def build_catalog_dashboard_payload(
    *,
    tab="skins",
    search="",
    weapon="",
    wear="",
    collection="",
    case="",
    sticker_type="",
    limit=48,
    offset=0,
):
    return catalog_browser.build_catalog_payload(
        DB_PATH,
        TREND_DB_PATH,
        tab=tab,
        search=search,
        weapon=weapon,
        wear=wear,
        collection=collection,
        case=case,
        sticker_type=sticker_type,
        limit=limit,
        offset=offset,
    )


def build_market_history_dashboard_status():
    return market_history_dashboard.build_market_history_status(
        MARKET_HISTORY_DB_PATH
    )


def build_market_history_dashboard_payload(
    *,
    kind="confirmed_sale",
    search="",
    marketplace="",
    provider="",
    days=90,
    limit=100,
    offset=0,
):
    return market_history_dashboard.build_market_history_payload(
        MARKET_HISTORY_DB_PATH,
        kind=kind,
        search=search,
        marketplace=marketplace,
        provider=provider,
        days=days,
        limit=limit,
        offset=offset,
    )


def build_budget_advisor_payload(payload):
    if not isinstance(payload, dict):
        raise TypeError("body must be a JSON object")
    allowed = {
        "action",
        "budget_cents",
        "mode",
        "risk",
        "max_items",
        "live_age_hours",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"unexpected fields: {', '.join(unknown)}")
    if payload.get("action") != "build_budget_advice":
        raise ValueError("action must be build_budget_advice")
    return market_analytics.build_budget_advice(
        DB_PATH,
        TREND_DB_PATH,
        SKINS_PATH,
        budget_cents=payload.get("budget_cents"),
        mode=payload.get("mode", "balanced"),
        risk=payload.get("risk", "balanced"),
        max_items=payload.get("max_items", 10),
        live_age_hours=payload.get("live_age_hours", 24),
    )


def build_collections_dashboard_payload(
    *, search="", sort="return_30d", limit=200, include_unknown=True
):
    limit_value = _bounded_strategy_int(
        limit, default=200, minimum=1, maximum=500, name="limit"
    )
    sort_name = str(sort or "return_30d").strip().lower()
    if sort_name not in {"return_30d", "sales_30d", "name"}:
        raise ValueError("sort must be return_30d, sales_30d, or name")
    cache_key = (
        "collections",
        str(search or "").strip().casefold(),
        sort_name,
        limit_value,
        bool(include_unknown),
    )
    cached = cache_get(cache_key, DETAIL_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    payload = list_collections(
        TREND_DB_PATH,
        search=search,
        include_unknown=bool(include_unknown),
        limit=500,
    )
    items = list(payload.get("items") or [])
    if sort_name == "name":
        items.sort(key=lambda item: str(item.get("collection_name") or "").casefold())
    elif sort_name == "sales_30d":
        items.sort(
            key=lambda item: (
                int(item.get("sales_30d") or 0),
                str(item.get("collection_name") or ""),
            ),
            reverse=True,
        )
    else:
        items.sort(
            key=lambda item: (
                float(item.get("member_coverage") or 0) >= 0.20,
                item.get("return_30d") is not None,
                float(item.get("return_30d") or -10**18),
                int(item.get("sales_30d") or 0),
                str(item.get("collection_name") or ""),
            ),
            reverse=True,
        )
    payload["items"] = items[:limit_value]
    payload["shown_count"] = len(payload["items"])
    payload["sort"] = sort_name
    return cache_set(cache_key, payload)


def build_collection_detail_dashboard_payload(collection_name, *, days=90):
    days_value = _bounded_strategy_int(
        days, default=90, minimum=1, maximum=730, name="days"
    )
    requested = str(collection_name or "").strip()
    cache_key = ("collection_detail_v2", requested, days_value)
    cached = cache_get(cache_key, DETAIL_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    payload = get_collection_detail(
        TREND_DB_PATH,
        requested,
        days=days_value,
        member_limit=500,
    )
    snapshot = market_analytics.load_skin_snapshot(SKINS_PATH)
    supply_total = 0
    supply_members = 0
    for member in payload.get("members") or []:
        supply = snapshot["items"].get(member.get("market_hash_name"), {})
        quantity = supply.get("quantity_on_market_estimate")
        if quantity is not None:
            member["quantity_on_market_estimate"] = quantity
            member["quantity_snapshot_at"] = snapshot["snapshot_at"]
            member["csfloat_url"] = supply.get("csfloat_url")
            supply_total += int(quantity)
            supply_members += 1
        member["steam_market_url"] = steam_market_url(member.get("market_hash_name"))
    if isinstance(payload.get("summary"), dict):
        payload["summary"]["quantity_on_market_estimate"] = supply_total
        payload["summary"]["quantity_snapshot_member_count"] = supply_members
    payload["supply"] = {
        "source": snapshot["source"],
        "snapshot_at": snapshot["snapshot_at"],
        "is_world_inventory": False,
    }
    return cache_set(cache_key, payload)


def build_skin_series_dashboard_payload(market_hash_name, *, days=90):
    days_value = _bounded_strategy_int(
        days, default=90, minimum=1, maximum=730, name="days"
    )
    requested = str(market_hash_name or "").strip()
    cache_key = ("skin_daily_v2", requested, days_value)
    cached = cache_get(cache_key, DETAIL_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    payload = get_skin_daily_series(
        TREND_DB_PATH,
        requested,
        days=days_value,
    )
    supply = market_analytics.load_skin_snapshot(SKINS_PATH)
    snapshot = supply["items"].get(requested, {})
    catalog = payload.get("catalog")
    if isinstance(catalog, dict):
        catalog["quantity_on_market_estimate"] = snapshot.get(
            "quantity_on_market_estimate"
        )
        catalog["quantity_snapshot_at"] = supply["snapshot_at"]
        catalog["csfloat_url"] = snapshot.get("csfloat_url")
        catalog["steam_market_url"] = steam_market_url(requested)
    return cache_set(cache_key, payload)


def build_strategy_overview_payload():
    """Return bounded outcome aggregates plus precomputed strategy readiness."""

    outcomes = {
        "history_ready": False,
        "loaded_rows": 0,
        "closed_recommendations": 0,
        "sold_recommendations": 0,
        "not_sold_recommendations": 0,
        "models": {},
    }
    candidates = []
    if Path(DB_PATH).is_file():
        with connect_db() as conn:
            outcomes = deal_intelligence.get_outcome_summary(
                conn, max_rows=STRATEGY_MAX_OUTCOME_ROWS
            )
            candidates = deal_intelligence.list_portfolio_candidates(
                conn,
                max_candidates=STRATEGY_MAX_DEAL_ITEMS,
                max_age_hours=24,
            )

    if Path(TREND_DB_PATH).is_file():
        backtest = get_backtest_report(TREND_DB_PATH)
    else:
        backtest = _unavailable_backtest_payload()
    return {
        "strategy_schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "outcomes": outcomes,
        "intelligence": _strategy_candidate_summary(candidates),
        "trend_pipeline": get_trend_pipeline_status(TREND_STATUS_PATH),
        "backtest": backtest,
        "safety": {
            "precomputed_only": True,
            "network_actions": False,
            "trading_actions": False,
        },
    }


def build_strategy_trend_payload(name=None, *, horizon=7, limit=24):
    """Read a bounded precomputed trend leaderboard or one exact skin."""

    horizon = _bounded_strategy_int(
        horizon, default=7, minimum=7, maximum=30, name="horizon"
    )
    if horizon not in {7, 14, 30}:
        raise ValueError("horizon must be 7, 14, or 30")
    limit = _bounded_strategy_int(
        limit,
        default=24,
        minimum=1,
        maximum=STRATEGY_MAX_TREND_ITEMS,
        name="limit",
    )
    requested_name = str(name or "").strip() or None
    if requested_name and len(requested_name) > 240:
        raise ValueError("name must not exceed 240 characters")
    if not Path(TREND_DB_PATH).is_file():
        return _unavailable_trend_payload(requested_name=requested_name)
    return get_trend_summary(
        TREND_DB_PATH,
        requested_name,
        ranking_horizon_days=horizon,
        limit=limit,
    )


def build_strategy_deals_payload(*, limit=30, max_age_hours=24):
    """Read bounded, already-computed risk annotations for current deals."""

    limit = _bounded_strategy_int(
        limit,
        default=30,
        minimum=1,
        maximum=STRATEGY_MAX_DEAL_ITEMS,
        name="limit",
    )
    max_age_hours = _bounded_strategy_int(
        max_age_hours,
        default=24,
        minimum=1,
        maximum=720,
        name="max_age_hours",
    )
    if not Path(DB_PATH).is_file():
        candidates = []
    else:
        with connect_db() as conn:
            candidates = deal_intelligence.list_portfolio_candidates(
                conn,
                max_candidates=limit,
                max_age_hours=max_age_hours,
            )
    return {
        "strategy_schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": limit,
        "max_age_hours": max_age_hours,
        "summary": _strategy_candidate_summary(candidates),
        "items": candidates,
    }


def build_strategy_portfolio_payload(payload):
    if not isinstance(payload, dict):
        raise TypeError("body must be a JSON object")
    allowed = {
        "action",
        "budget_cents",
        "risk",
        "max_items",
        "max_per_skin",
        "max_per_collection",
        "max_candidates",
        "max_age_hours",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"unexpected fields: {', '.join(unknown)}")
    if payload.get("action") != "build_portfolio":
        raise ValueError("action must be build_portfolio")
    budget_cents = _bounded_strategy_int(
        payload.get("budget_cents"),
        default=100_000,
        minimum=1,
        maximum=2_000_000_000,
        name="budget_cents",
    )
    max_items = _bounded_strategy_int(
        payload.get("max_items"),
        default=10,
        minimum=1,
        maximum=50,
        name="max_items",
    )
    max_per_skin = _bounded_strategy_int(
        payload.get("max_per_skin"),
        default=1,
        minimum=1,
        maximum=50,
        name="max_per_skin",
    )
    max_per_collection = _bounded_strategy_int(
        payload.get("max_per_collection"),
        default=3,
        minimum=1,
        maximum=50,
        name="max_per_collection",
    )
    max_candidates = _bounded_strategy_int(
        payload.get("max_candidates"),
        default=200,
        minimum=1,
        maximum=deal_intelligence.MAX_PORTFOLIO_CANDIDATES,
        name="max_candidates",
    )
    max_age_hours = _bounded_strategy_int(
        payload.get("max_age_hours"),
        default=24,
        minimum=1,
        maximum=720,
        name="max_age_hours",
    )
    risk = str(payload.get("risk") or "balanced").strip().lower()
    if risk not in deal_intelligence.RISK_PROFILES:
        raise ValueError("risk must be conservative, balanced, or aggressive")
    if not Path(DB_PATH).is_file():
        raise ValueError("deal database is not available")
    with connect_db() as conn:
        result = deal_intelligence.build_portfolio(
            conn,
            budget_cents=budget_cents,
            risk=risk,
            max_items=max_items,
            max_per_skin=max_per_skin,
            max_per_collection=max_per_collection,
            max_candidates=max_candidates,
            max_age_hours=max_age_hours,
        )
    result["precomputed_candidates_only"] = True
    result["trading_actions"] = False
    return result


def _backtest_config_from_payload(payload):
    config = payload.get("config")
    if config is None:
        return None
    if not isinstance(config, dict):
        raise TypeError("config must be a JSON object")
    allowed = set(BacktestConfig.__dataclass_fields__)
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ValueError(f"unexpected config fields: {', '.join(unknown)}")
    normalized = dict(config)
    if "fill_seed" in normalized and len(str(normalized["fill_seed"])) > 128:
        raise ValueError("fill_seed must not exceed 128 characters")
    if "initial_capital_cents" in normalized:
        normalized["initial_capital_cents"] = _bounded_strategy_int(
            normalized["initial_capital_cents"],
            default=100_000,
            minimum=1,
            maximum=2_000_000_000,
            name="initial_capital_cents",
        )
    return BacktestConfig(**normalized)


def build_strategy_backtest_payload(payload):
    if not isinstance(payload, dict):
        raise TypeError("body must be a JSON object")
    unknown = sorted(set(payload) - {"action", "config"})
    if unknown:
        raise ValueError(f"unexpected fields: {', '.join(unknown)}")
    if payload.get("action") != "read_precomputed_backtest":
        raise ValueError("action must be read_precomputed_backtest")
    config = _backtest_config_from_payload(payload)
    if not Path(TREND_DB_PATH).is_file():
        result = _unavailable_backtest_payload()
        result["config"] = config.to_dict() if config else None
        return result
    result = get_backtest_report(TREND_DB_PATH, config)
    result["precomputed_only"] = True
    result["network_actions"] = False
    result["trading_actions"] = False
    return result


def strategy_request_violation(handler):
    """Enforce JSON, explicit intent, and same-origin strategy POSTs."""

    content_type = (
        handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "application/json":
        return 415, "Content-Type must be application/json"
    if not secrets.compare_digest(
        handler.headers.get("X-CS2-Action", ""), "strategy-read"
    ):
        return 403, "missing strategy action confirmation"
    if handler.headers.get("Transfer-Encoding"):
        return 400, "chunked request bodies are not accepted"
    fetch_site = handler.headers.get("Sec-Fetch-Site", "").strip().lower()
    if fetch_site not in {"", "none", "same-origin"}:
        return 403, "cross-site strategy requests are not allowed"
    origin = handler.headers.get("Origin", "").strip()
    if origin:
        parsed_origin = urlparse(origin)
        request_host = handler.headers.get("Host", "").strip().casefold()
        if (
            parsed_origin.scheme not in {"http", "https"}
            or not request_host
            or parsed_origin.netloc.casefold() != request_host
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.params
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            return 403, "request Origin does not match this dashboard"
    return None


def build_predict_deal_payload(payload):
    try:
        import glove_model_runtime
        import igor_model_runtime
        import knife_model_runtime
        import neural_model_runtime
        import price_model_runtime

        models = {}
        model_errors = {}
        with connect_db() as conn:
            name = str(
                payload.get("name")
                or price_model_runtime.parse_market_hash_name(
                    payload.get("url") or payload.get("text") or ""
                )
                or ""
            ).strip()
            item_kind = classify_market_hash_name(name, conn=conn)
            if item_kind == ITEM_KIND_KNIFE:
                runners = {"knives": knife_model_runtime.predict_deal}
            elif item_kind == ITEM_KIND_GLOVE:
                runners = {"gloves": glove_model_runtime.predict_deal}
            elif item_kind == ITEM_KIND_WEAPON_SKIN:
                runners = {
                    "almaz": price_model_runtime.predict_deal,
                    "igor": igor_model_runtime.predict_deal,
                    "nova": neural_model_runtime.predict_deal,
                }
            else:
                return {
                    "error": "item type is unknown; choose an exact skin, knife, or glove name"
                }
            for model_name, runner in runners.items():
                try:
                    prediction = runner(payload, conn)
                    if not isinstance(prediction, dict):
                        raise TypeError("model returned a non-object prediction")
                    models[model_name] = prediction
                except Exception as exc:  # noqa: BLE001 - keep independent models useful
                    model_errors[model_name] = type(exc).__name__

        if not models:
            failed = ", ".join(sorted(model_errors)) or "all models"
            return {"error": f"prediction unavailable for {failed}"}

        def finite_number(value):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return number if math.isfinite(number) else None

        # The manual comparison must never turn the most optimistic model into
        # an unexplained recommendation.  The primary figures use the lowest
        # available price estimate; every individual estimate remains visible.
        selected_name, selected = min(
            models.items(),
            key=lambda item: (
                finite_number(item[1].get("predicted_price_cents"))
                if finite_number(item[1].get("predicted_price_cents")) is not None
                else math.inf,
                item[0],
            ),
        )

        active_models = set(models)
        positive_models = {
            name
            for name, prediction in models.items()
            if prediction.get("verdict") in DEAL_POSITIVE_VERDICTS
        }
        dissenting_models = active_models - positive_models
        if positive_models == DEAL_MODEL_NAMES and not dissenting_models:
            confidence = "unanimous_three"
        elif len(positive_models) >= 2 and not dissenting_models:
            confidence = "consensus"
        elif positive_models and dissenting_models:
            confidence = "disagreement"
        elif len(positive_models) == 1:
            confidence = "single_signal"
        else:
            confidence = "no_positive"

        predicted_values = [
            finite_number(model.get("predicted_price_cents"))
            for model in models.values()
        ]
        predicted_values = [value for value in predicted_values if value is not None]
        result = dict(selected)
        result["selected_model"] = selected_name
        result["selection_method"] = "minimum_available_predicted_price"
        result["models"] = models
        result["model_errors"] = model_errors
        result["comparison"] = {
            "confidence": confidence,
            "active_models": sorted(active_models),
            "positive_models": sorted(positive_models),
            "dissenting_models": sorted(dissenting_models),
            "unavailable_models": sorted(model_errors),
            "conservative_model": selected_name,
            "conservative_predicted_price_cents": selected.get(
                "predicted_price_cents"
            ),
            "conservative_profit_cents": selected.get("profit_cents"),
            "conservative_roi_percent": selected.get("roi_percent"),
            "prediction_spread_cents": (
                max(predicted_values) - min(predicted_values)
                if len(predicted_values) >= 2
                else None
            ),
        }
        return result
    except Exception as exc:
        return {"error": str(exc)}


def build_predict_neural_payload(payload):
    try:
        import neural_model_runtime

        with connect_db() as conn:
            return neural_model_runtime.predict_deal(payload, conn)
    except Exception as exc:
        return {"error": str(exc)}


def build_fetch_csfloat_listing_payload(payload):
    try:
        import csfloat_listing_fetcher
        text = payload.get("url_text") or payload.get("url") or payload.get("link") or payload.get("name") or ""
        return csfloat_listing_fetcher.fetch_listing_from_text(text)
    except Exception as exc:
        return {"error": str(exc)}


class AccountCsfloatImportInputError(ValueError):
    pass


class AccountCsfloatImportResponseError(RuntimeError):
    pass


class AccountCsfloatListingNotFoundError(RuntimeError):
    pass


def _account_csfloat_input(value, fetcher):
    if not isinstance(value, str):
        raise AccountCsfloatImportInputError("url must be a string")
    text = value.strip()
    if not text or len(text) > 2_048:
        raise AccountCsfloatImportInputError("invalid CSFloat URL length")

    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", text):
        parsed = urlparse(text)
        try:
            port = parsed.port
        except ValueError as exc:
            raise AccountCsfloatImportInputError("invalid CSFloat URL") from exc
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold() not in {"csfloat.com", "www.csfloat.com"}
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
        ):
            raise AccountCsfloatImportInputError("invalid CSFloat URL")

    # The fetcher's parser converts every accepted public URL/id to the fixed
    # CSFloat API origin.  We keep its result only long enough to extract the
    # listing id; no user-controlled host is ever requested.
    api_url = fetcher.parse_listing_url_or_id(text)
    listing_id = urlparse(api_url).path.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", listing_id):
        raise AccountCsfloatImportInputError("invalid CSFloat listing id")
    return text, listing_id


def _account_csfloat_int(value, *, minimum=0, maximum=100_000_000_00):
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if result < minimum or result > maximum:
        return None
    return result


def _account_csfloat_float(value, *, minimum=0.0, maximum=1.0):
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or not minimum <= result <= maximum:
        return None
    return result


def _account_csfloat_url(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 2_048:
        return None
    parsed = urlparse(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return text


def _account_csfloat_accessories(value, *, keychain=False):
    if not isinstance(value, list):
        return []
    result = []
    for index, raw in enumerate(value[:20]):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()[:256]
        if not name:
            continue
        item = {"name": name}
        identifier_name = "keychain_id" if keychain else "sticker_id"
        identifier = raw.get(identifier_name)
        if identifier not in (None, ""):
            item[identifier_name] = str(identifier)[:128]
        slot = _account_csfloat_int(raw.get("slot"), minimum=0, maximum=20)
        if slot is not None:
            item["slot"] = slot
        price = _account_csfloat_int(raw.get("price_cents"))
        if price is not None:
            item["price_cents"] = price
        if keychain:
            pattern = _account_csfloat_int(raw.get("pattern"), minimum=0, maximum=1_000_000)
            if pattern is not None:
                item["pattern"] = pattern
        else:
            wear = _account_csfloat_float(raw.get("wear"), minimum=-1.0, maximum=1.0)
            if wear is not None:
                item["wear"] = wear
        for field in ("icon_url", "steam_market_url"):
            safe_url = _account_csfloat_url(raw.get(field))
            if safe_url:
                item[field] = safe_url
        result.append(item)
    return result


def _account_csfloat_history_deal(listing_id):
    """Return one indexed local observation for a listing absent from live API."""

    if not Path(DB_PATH).is_file():
        return None
    try:
        with connect_db() as conn:
            if not table_exists(conn, "sales"):
                return None
            columns = table_columns(conn, "sales")
            select_fields = ["listing_id"]
            for field in (
                "name",
                "price_cents",
                "float_value",
                "paint_seed",
                "paint_index",
                "item_collection_name",
                "item_icon_url",
                "item_cs2_screenshot_id",
                "sale_type",
                "sold_at",
                "fetched_at",
            ):
                select_fields.append(optional_column_expr(columns, field, field))
            row = conn.execute(
                f"SELECT {', '.join(select_fields)} FROM sales WHERE listing_id = ? LIMIT 1",
                (listing_id,),
            ).fetchone()
            if row is None:
                return None
            row = dict(row)
            stickers = (
                fetch_stickers_for_listings(conn, [listing_id]).get(listing_id, [])
                if table_exists(conn, "sale_stickers")
                else []
            )
            keychains = []
            if table_exists(conn, "sale_keychains"):
                keychain_columns = table_columns(conn, "sale_keychains")
                keychain_fields = []
                for field in (
                    "keychain_id",
                    "slot",
                    "name",
                    "pattern",
                    "price_cents",
                    "icon_url",
                    "steam_market_url",
                ):
                    keychain_fields.append(
                        optional_column_expr(keychain_columns, field, field)
                    )
                keychains = [
                    dict(item)
                    for item in conn.execute(
                        f"SELECT {', '.join(keychain_fields)} FROM sale_keychains "
                        "WHERE listing_id = ? ORDER BY keychain_index LIMIT 20",
                        (listing_id,),
                    )
                ]
    except (OSError, sqlite3.Error):
        return None

    return {
        "listing_id": listing_id,
        "name": row.get("name"),
        "buy_price_cents": row.get("price_cents"),
        "float_value": row.get("float_value"),
        "paint_seed": row.get("paint_seed"),
        "paint_index": row.get("paint_index"),
        "item_collection_name": row.get("item_collection_name"),
        "item_icon_url": row.get("item_icon_url"),
        "screenshot_url": build_csfloat_screenshot_url(
            row.get("item_cs2_screenshot_id")
        ),
        "listing_type": row.get("sale_type"),
        "sold_at": row.get("sold_at"),
        "observed_at": row.get("fetched_at"),
        "stickers": stickers,
        "keychains": keychains,
    }


def _normalize_account_csfloat_listing(deal, listing_id, *, source):
    if not isinstance(deal, dict):
        raise AccountCsfloatImportResponseError("unexpected CSFloat response")
    skin_name = str(deal.get("name") or "").strip()
    if not skin_name or len(skin_name) > 512:
        raise AccountCsfloatImportResponseError("CSFloat response has no skin name")

    price = _account_csfloat_int(deal.get("buy_price_cents"), minimum=1)
    listing_type = normalize_listing_type(
        {"listing_type": deal.get("listing_type")}
    )
    warning_codes = ["verify_purchase_price"]
    price_kind = "current_listing_ask"
    if source == "local_history":
        price_kind = "historical_listing_ask"
        warning_codes.append("historical_price")
    if price is None:
        warning_codes.append("price_missing")
    if listing_type == "auction":
        warning_codes.append("auction_price")

    return {
        "listing_id": listing_id,
        "source_url": f"https://csfloat.com/item/{listing_id}",
        "skin_name": skin_name,
        "buy_price_cents": price,
        "listing_price_cents": price,
        "price_kind": price_kind,
        "price_is_purchase_price": False,
        "source": source,
        "listing_type": listing_type,
        "float_value": _account_csfloat_float(deal.get("float_value")),
        "paint_seed": _account_csfloat_int(
            deal.get("paint_seed"), minimum=0, maximum=1_000_000
        ),
        "paint_index": _account_csfloat_int(
            deal.get("paint_index"), minimum=0, maximum=1_000_000
        ),
        "collection_name": str(deal.get("item_collection_name") or "").strip()[:256]
        or None,
        "icon_url": _account_csfloat_url(deal.get("item_icon_url")),
        "screenshot_url": _account_csfloat_url(deal.get("screenshot_url")),
        "stickers": _account_csfloat_accessories(deal.get("stickers")),
        "keychains": _account_csfloat_accessories(
            deal.get("keychains"), keychain=True
        ),
        "sold_at": deal.get("sold_at") if source == "local_history" else None,
        "observed_at": deal.get("observed_at") if source == "local_history" else None,
        "warning_codes": warning_codes,
    }


def build_account_csfloat_import_response(payload):
    """Fetch one listing without exposing route, proxy, key, or raw API metadata."""

    import csfloat_listing_fetcher as fetcher

    if set(payload) != {"url"}:
        return 400, {
            "error": "Request must contain only the url field.",
            "error_code": "invalid_request",
            "retryable": False,
        }, ()
    try:
        text, listing_id = _account_csfloat_input(payload.get("url"), fetcher)
        try:
            fetched = fetcher.fetch_listing_from_text(text)
        except fetcher.CsfloatNotFoundError:
            deal = _account_csfloat_history_deal(listing_id)
            if deal is None:
                raise AccountCsfloatListingNotFoundError from None
            listing = _normalize_account_csfloat_listing(
                deal, listing_id, source="local_history"
            )
        else:
            deal = fetched.get("deal_payload") if isinstance(fetched, dict) else None
            try:
                listing = _normalize_account_csfloat_listing(
                    deal, listing_id, source="csfloat_live"
                )
            except AccountCsfloatImportResponseError:
                history_deal = _account_csfloat_history_deal(listing_id)
                if history_deal is None:
                    raise
                listing = _normalize_account_csfloat_listing(
                    history_deal, listing_id, source="local_history"
                )
        return 200, {"listing": listing}, ()
    except AccountCsfloatImportInputError:
        return 400, {
            "error": "Paste a valid HTTPS CSFloat listing URL or listing id.",
            "error_code": "invalid_csfloat_url",
            "retryable": False,
        }, ()
    except AccountCsfloatListingNotFoundError:
        return 404, {
            "error": "The listing is unavailable on CSFloat and was not found in local history.",
            "error_code": "csfloat_listing_not_found",
            "retryable": False,
        }, ()
    except fetcher.CsfloatRateLimitError:
        return 429, {
            "error": "CSFloat is rate limited. Try again shortly.",
            "error_code": "csfloat_rate_limited",
            "retryable": True,
        }, (("Retry-After", "60"),)
    except (fetcher.CsfloatRouteConfigError, FileNotFoundError):
        return 503, {
            "error": "CSFloat access is not configured on this server.",
            "error_code": "csfloat_not_configured",
            "retryable": False,
        }, ()
    except AccountCsfloatImportResponseError:
        return 502, {
            "error": "CSFloat returned a listing without usable item data.",
            "error_code": "csfloat_invalid_response",
            "retryable": True,
        }, ()
    except ValueError:
        return 400, {
            "error": "Paste a valid HTTPS CSFloat listing URL or listing id.",
            "error_code": "invalid_csfloat_url",
            "retryable": False,
        }, ()
    except fetcher.CsfloatFetchError:
        return 502, {
            "error": "CSFloat is temporarily unavailable.",
            "error_code": "csfloat_unavailable",
            "retryable": True,
        }, ()
    except Exception:  # noqa: BLE001 - public boundary must hide internal/API details
        return 502, {
            "error": "CSFloat is temporarily unavailable.",
            "error_code": "csfloat_unavailable",
            "retryable": True,
        }, ()


def warm_price_model():
    try:
        import neural_model_runtime
        import price_model_runtime

        price_model_runtime.load_model()
        neural_model_runtime.load_model()
    except Exception as exc:
        print(f"Price model warm-up skipped: {exc}", flush=True)


def handle_admin_users_post(handler, parsed):
    if parsed.path != "/api/admin/users":
        return False
    if not is_direct_local_request(handler):
        json_response(handler, 404, {"error": "not found", "error_code": "local_only"})
        return True
    violation = admin_users_request_violation(handler)
    if violation is not None:
        status, message = violation
        json_response(
            handler,
            status,
            {"error": message, "error_code": "owner_authorization_required"},
        )
        return True
    try:
        payload = read_json_request(handler, limit=ADMIN_USERS_BODY_LIMIT)
        result = update_admin_user_from_payload(payload)
    except (ValueError, account_backend.AccountError) as exc:
        account_error_response(handler, exc)
        return True
    except (OSError, sqlite3.Error):
        json_response(
            handler,
            503,
            {
                "error": "account administration is unavailable",
                "error_code": "service_unavailable",
            },
        )
        return True
    json_response(handler, 200, result)
    return True


def handle_public_auth_post(handler, parsed, query):
    action_by_path = {
        "/api/auth/register": "auth-register",
        "/api/auth/login": "auth-login",
        "/api/auth/forgot-password": "auth-forgot",
        "/api/auth/reset-password": "auth-reset",
    }
    expected_action = action_by_path.get(parsed.path)
    if expected_action is None:
        return False
    violation = account_request_violation(handler, expected_action)
    if violation is not None:
        status, message = violation
        json_response(handler, status, {"error": message})
        return True
    try:
        payload = read_json_request(handler)
        service = get_account_service()
        if parsed.path == "/api/auth/register":
            if not ACCOUNT_REGISTRATION_ENABLED:
                json_response(handler, 403, {"error": "registration is disabled"})
                return True
            unknown = sorted(set(payload) - {"email", "password", "display_name"})
            if unknown:
                raise account_backend.ValidationError(
                    f"unexpected fields: {', '.join(unknown)}"
                )
            email = payload.get("email", "")
            if auth_rate_limited(
                handler, "register", email, limit=5, window_seconds=3600
            ):
                json_response(handler, 429, {"error": "too many registration attempts"})
                return True
            user = service.create_user(
                email,
                payload.get("password", ""),
                display_name=payload.get("display_name", ""),
                role="member",
                **account_request_metadata(handler),
            )
            json_response(
                handler,
                201,
                {
                    "message": "Аккаунт создан и ожидает одобрения владельца.",
                    "user": user,
                    "approval_required": True,
                    "access_state": "pending",
                },
            )
            return True

        if parsed.path == "/api/auth/login":
            unknown = sorted(set(payload) - {"email", "password"})
            if unknown:
                raise account_backend.ValidationError(
                    f"unexpected fields: {', '.join(unknown)}"
                )
            email = payload.get("email", "")
            if auth_rate_limited(
                handler, "login", email, limit=8, window_seconds=600
            ):
                json_response(handler, 429, {"error": "too many login attempts"})
                return True
            previous_session_token = request_cookie_value(
                handler, ACCOUNT_SESSION_COOKIE
            )
            session = service.authenticate(
                email,
                payload.get("password", ""),
                **account_request_metadata(handler),
            )
            if previous_session_token:
                service.revoke_session(previous_session_token)
            json_response(
                handler,
                200,
                {"message": "Вход выполнен.", "user": session["user"]},
                extra_headers=account_cookie_headers(session),
            )
            return True

        if parsed.path == "/api/auth/forgot-password":
            unknown = sorted(set(payload) - {"email"})
            if unknown:
                raise account_backend.ValidationError(
                    f"unexpected fields: {', '.join(unknown)}"
                )
            email = str(payload.get("email", ""))
            limited = auth_rate_limited(
                handler, "forgot", email, limit=5, window_seconds=3600
            )
            if not limited:
                threading.Thread(
                    target=service.request_password_reset,
                    args=(email,),
                    name="account-password-reset",
                    daemon=True,
                ).start()
            delivery_configured = not isinstance(
                service.password_reset_sender,
                account_backend.DisabledPasswordResetSender,
            )
            json_response(
                handler,
                202,
                {
                    "message": "Если аккаунт существует, ссылка отправлена на почту.",
                    "delivery_configured": delivery_configured,
                },
            )
            return True

        unknown = sorted(set(payload) - {"token", "password"})
        if unknown:
            raise account_backend.ValidationError(
                f"unexpected fields: {', '.join(unknown)}"
            )
        if auth_rate_limited(
            handler, "reset", payload.get("token", ""), limit=10, window_seconds=3600
        ):
            json_response(handler, 429, {"error": "too many reset attempts"})
            return True
        service.reset_password(payload.get("token", ""), payload.get("password", ""))
        json_response(
            handler,
            200,
            {"message": "Пароль изменён. Теперь войдите с новым паролем."},
            extra_headers=account_cookie_headers(clear=True),
        )
        return True
    except ValueError as exc:
        json_response(handler, 400, {"error": str(exc)})
    except account_backend.AccountError as exc:
        account_error_response(handler, exc)
    except (OSError, sqlite3.Error):
        json_response(handler, 503, {"error": "account service is unavailable"})
    return True


ACCOUNT_LIVE_ASK_NOTE = (
    "Текущее минимальное активное предложение buy-now на CSFloat. "
    "Это цена продавца, а не завершённая продажа и не заявка автобая."
)
_ACCOUNT_PRICE_SNAPSHOT_LOCK = threading.Lock()
_ACCOUNT_PRICE_SNAPSHOT_CACHE = {"expires_at": 0.0, "payload": None}


def _account_csfloat_price_snapshot():
    """Return a short-lived safe copy of the aggregate CSFloat ask snapshot."""

    if not ACCOUNT_LIVE_PRICING_ENABLED:
        return None
    now = time.monotonic()
    with _ACCOUNT_PRICE_SNAPSHOT_LOCK:
        cached = _ACCOUNT_PRICE_SNAPSHOT_CACHE.get("payload")
        if cached is not None and _ACCOUNT_PRICE_SNAPSHOT_CACHE["expires_at"] > now:
            return cached
        try:
            payload = csfloat_listing_fetcher.fetch_csfloat_price_list()
        except (OSError, ValueError, csfloat_listing_fetcher.CsfloatFetchError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), dict):
            return None
        _ACCOUNT_PRICE_SNAPSHOT_CACHE.update(
            {"expires_at": time.monotonic() + 30, "payload": payload}
        )
        return payload


def _account_catalog_lookup_many(names):
    canonical_names = []
    seen = set()
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        canonical_names.append(name)
        if len(canonical_names) >= 200:
            break
    batch_lookup = getattr(inventory_item_catalog, "lookup_items", None)
    if callable(batch_lookup):
        try:
            result = batch_lookup(DB_PATH, canonical_names, limit=200)
        except (OSError, TypeError, ValueError):
            result = {}
        if isinstance(result, dict):
            return result
    result = {}
    for name in canonical_names:
        try:
            item = inventory_item_catalog.lookup_item(DB_PATH, name)
        except (OSError, TypeError, ValueError):
            item = None
        if item:
            result[name] = item
    return result


def _account_item_match_rank(name, query):
    folded_name = str(name).casefold()
    folded_query = str(query).casefold()
    if folded_name == folded_query:
        return 0
    if folded_name.startswith(folded_query):
        return 1
    if folded_query in folded_name:
        return 2
    return None


def _account_public_market_item(
    name,
    *,
    metadata=None,
    live_quote=None,
    historical=None,
):
    metadata = metadata if isinstance(metadata, dict) else {}
    live_quote = live_quote if isinstance(live_quote, dict) else {}
    historical = historical if isinstance(historical, dict) else {}
    live_price = live_quote.get("current_price_cents")
    historical_price = historical.get("market_median_cents")
    if live_price is not None:
        current_price = int(live_price)
        price_source = live_quote.get("price_source") or "csfloat_price_list"
        price_as_of = live_quote.get("price_as_of")
        active_quantity = live_quote.get("active_quantity")
    else:
        current_price = (
            int(historical_price) if historical_price is not None else None
        )
        price_source = "historical_market" if current_price is not None else None
        price_as_of = historical.get("market_as_of")
        active_quantity = None
    item_kind = str(metadata.get("item_kind") or "unknown")
    return {
        "market_hash_name": str(
            metadata.get("market_hash_name") or name
        ),
        "item_kind": item_kind,
        "item_type": item_kind,
        "icon_url": metadata.get("icon_url") or historical.get("icon_url"),
        "current_price_cents": current_price,
        "active_quantity": active_quantity,
        "price_source": price_source,
        "price_as_of": price_as_of,
        "price_is_live_ask": bool(live_price is not None),
    }


def build_account_item_search_payload(query):
    raw_query = query.get("q", [""])[0]
    if not isinstance(raw_query, str):
        raise account_backend.ValidationError("search query must be a string")
    search = raw_query.strip()
    if len(search) > inventory_item_catalog.MAX_QUERY_LENGTH:
        raise account_backend.ValidationError("search query is too long")
    try:
        local_items = inventory_item_catalog.search_items(
            DB_PATH,
            search,
            limit=ACCOUNT_ITEM_SEARCH_LIMIT,
        )
    except (OSError, TypeError, ValueError):
        local_items = []

    snapshot = _account_csfloat_price_snapshot()
    price_items = snapshot.get("items", {}) if snapshot else {}
    candidates = {}
    for item in local_items:
        name = str(item.get("market_hash_name") or "").strip()
        if name:
            candidates[name.casefold()] = {"name": name, "metadata": item}
    if search and price_items:
        for name in price_items:
            rank = _account_item_match_rank(name, search)
            if rank is None:
                continue
            candidates.setdefault(name.casefold(), {"name": name, "metadata": None})

    ranked = sorted(
        candidates.values(),
        key=lambda item: (
            _account_item_match_rank(item["name"], search) or 0,
            item["name"].casefold(),
            item["name"],
        ),
    )[:ACCOUNT_ITEM_SEARCH_LIMIT]
    names = [item["name"] for item in ranked]
    missing_names = [item["name"] for item in ranked if not item["metadata"]]
    extra_metadata = _account_catalog_lookup_many(missing_names)
    historical = inventory_market.get_market_snapshots(TREND_DB_PATH, names)
    price_as_of = snapshot.get("price_as_of") if snapshot else None
    price_source = snapshot.get("price_source") if snapshot else None
    items = []
    for candidate in ranked:
        name = candidate["name"]
        live_values = price_items.get(name)
        live_quote = (
            {
                **live_values,
                "price_as_of": price_as_of,
                "price_source": price_source,
            }
            if isinstance(live_values, dict)
            else None
        )
        items.append(
            _account_public_market_item(
                name,
                metadata=candidate["metadata"] or extra_metadata.get(name),
                live_quote=live_quote,
                historical=historical.get(name),
            )
        )
    return {"items": items}


def build_account_item_quote_payload(query):
    raw_name = query.get("market_hash_name", query.get("name", [""]))[0]
    if not isinstance(raw_name, str):
        raise account_backend.ValidationError("market_hash_name must be a string")
    requested = raw_name.strip()
    if not requested or len(requested) > 255 or any(ord(ch) < 32 for ch in requested):
        raise account_backend.ValidationError("invalid market_hash_name")
    try:
        metadata = inventory_item_catalog.lookup_item(DB_PATH, requested)
    except (OSError, TypeError, ValueError):
        metadata = None
    canonical = str((metadata or {}).get("market_hash_name") or requested)
    quote = None
    if ACCOUNT_LIVE_PRICING_ENABLED:
        try:
            quote = csfloat_listing_fetcher.fetch_csfloat_floor(canonical)
        except (OSError, ValueError, csfloat_listing_fetcher.CsfloatFetchError):
            quote = None
    historical = inventory_market.get_market_snapshots(
        TREND_DB_PATH,
        [canonical],
    ).get(canonical)
    item = _account_public_market_item(
        canonical,
        metadata=metadata,
        live_quote=quote,
        historical=historical,
    )
    return {
        "item": item,
        "quote_available": bool(quote and quote.get("current_price_cents")),
    }


def warm_account_inventory_market_data():
    """Refresh optional local metadata and prime the live floor cache."""

    try:
        cache_missing = not ACCOUNT_CONTAINER_CACHE_PATH.is_file()
        cache_stale = (
            not cache_missing
            and time.time() - ACCOUNT_CONTAINER_CACHE_PATH.stat().st_mtime
            > ACCOUNT_CONTAINER_CACHE_MAX_AGE_SECONDS
        )
        if cache_missing or cache_stale:
            inventory_item_catalog.refresh_container_cache(
                ACCOUNT_CONTAINER_CACHE_PATH
            )
    except (OSError, ValueError, inventory_item_catalog.ContainerCacheError):
        pass
    _account_csfloat_price_snapshot()


ACCOUNT_STRATEGY_LABELS = {
    "flip": "Быстрая перепродажа",
    "investment": "Инвестиция",
    "float": "Флот",
    "stickers": "Стикеры",
    "collection": "Коллекция",
    "balanced": "Сбалансированно",
    "manual": "Вручную",
}


def build_account_inventory_payload(user, query):
    service = get_account_service()
    status = str(query.get("status", ["active"])[0] or "active")
    search = str(query.get("search", [""])[0] or "")
    if status not in account_backend.INVENTORY_STATUSES | {"active", "all"}:
        raise account_backend.ValidationError("unknown inventory status filter")
    if len(search) > 200:
        raise account_backend.ValidationError("search is too long")
    sort_mode = str(query.get("sort", ["newest"])[0] or "newest")
    if sort_mode not in {"newest", "stale", "profit_high", "value_high"}:
        raise account_backend.ValidationError("unknown inventory sort")
    try:
        limit = int(query.get("limit", ["200"])[0])
    except (TypeError, ValueError) as exc:
        raise account_backend.ValidationError("limit must be an integer") from exc
    limit = min(max(limit, 1), 200)
    all_items = service.list_inventory(
        user["id"],
        status="all",
        search="",
        limit=200,
        include_analysis=True,
    )
    search_folded = search.strip().casefold()
    items = [
        item
        for item in all_items
        if (
            status == "all"
            or (status == "active" and item.get("status") in {"owned", "listed"})
            or item.get("status") == status
        )
        and (
            not search_folded
            or search_folded in str(item.get("skin_name") or "").casefold()
        )
    ]
    inventory_names = [item["skin_name"] for item in all_items]
    snapshots = inventory_market.get_market_snapshots(TREND_DB_PATH, inventory_names)
    catalog_items = _account_catalog_lookup_many(inventory_names)
    live_snapshot = _account_csfloat_price_snapshot()
    live_prices = live_snapshot.get("items", {}) if live_snapshot else {}
    live_price_as_of = live_snapshot.get("price_as_of") if live_snapshot else None
    live_price_source = live_snapshot.get("price_source") if live_snapshot else None

    invested = 0
    market_value = 0
    market_value_positions = 0
    unrealized = 0
    unrealized_positions = 0
    realized = 0
    active_count = 0
    sold_count = 0
    stale_count = 0
    for item in all_items:
        snapshot = snapshots.get(item["skin_name"], {})
        live_values = live_prices.get(item["skin_name"])
        live_price = (
            live_values.get("current_price_cents")
            if isinstance(live_values, dict)
            else None
        )
        analysis = item.get("analysis") or {}
        quantity = int(item.get("quantity") or 1)
        analysis_total_cost = analysis.get("total_cost_cents")
        total_cost_cents = int(
            analysis_total_cost
            if analysis_total_cost is not None
            else int(item["buy_price_cents"]) * quantity
        )
        pricing_buy_price_cents = max(
            1, math.ceil(total_cost_cents / max(1, quantity))
        )
        special_float = item.get("strategy") == "float"
        has_special_stickers = bool(item.get("stickers"))
        pricing = inventory_market.recommend_listing_price(
            snapshot,
            buy_price_cents=pricing_buy_price_cents,
            fee_percent=item["fee_percent"],
            listed_price_cents=item.get("listed_price_cents"),
            listed_at=item.get("listed_at"),
            strategy=item.get("strategy") or "balanced",
            stale_days=user.get("stale_after_days", 7),
            special_float=special_float,
            has_special_stickers=has_special_stickers,
        )
        pricing["recommended_price_cents"] = pricing.get("suggested_price_cents")
        historical_median = snapshot.get("market_median_cents")
        manual_valuation = item.get("market_price_cents")
        if live_price is not None:
            effective_median = int(live_price)
            valuation_source = live_price_source or "csfloat_price_list"
            market_as_of = live_price_as_of
            valuation_note = ACCOUNT_LIVE_ASK_NOTE
        elif historical_median is not None:
            effective_median = int(historical_median)
            valuation_source = "historical_market"
            market_as_of = snapshot.get("market_as_of")
            valuation_note = snapshot.get("benchmark_note")
        elif manual_valuation is not None:
            effective_median = int(manual_valuation)
            valuation_source = "saved_valuation"
            market_as_of = item.get("market_price_updated_at")
            valuation_note = (
                "Сохранённая оценка на момент добавления предмета; актуальное "
                "предложение CSFloat и история рынка сейчас недоступны."
            )
        else:
            effective_median = None
            valuation_source = "unavailable"
            market_as_of = None
            valuation_note = snapshot.get("benchmark_note")
        pricing["market_median_cents"] = effective_median
        pricing["valuation_source"] = valuation_source
        pricing["valuation_is_manual"] = valuation_source == "saved_valuation"
        pricing["valuation_is_live_ask"] = live_price is not None
        pricing["market_as_of"] = market_as_of
        pricing["valuation_note"] = valuation_note
        pricing["active_quantity"] = (
            live_values.get("active_quantity")
            if isinstance(live_values, dict)
            else None
        )
        if effective_median is not None:
            fee_fraction = 1 - float(item["fee_percent"]) / 100
            net_total = math.floor(int(effective_median) * fee_fraction) * quantity
            exact_unrealized = net_total - total_cost_cents
            pricing["unrealized_profit_cents"] = exact_unrealized
            pricing["unrealized_roi_percent"] = (
                round(exact_unrealized / total_cost_cents * 100, 2)
                if total_cost_cents > 0
                else None
            )
            pricing["unrealized_profit_basis"] = (
                "manual_valuation_after_fee"
                if pricing["valuation_is_manual"]
                else "historical_median_after_fee"
            )
        if item.get("status") != "listed":
            pricing["is_stale"] = False
        unit_unrealized = pricing.get("unrealized_profit_cents")
        if unit_unrealized is not None and effective_median is None:
            pricing["unrealized_profit_cents"] = int(unit_unrealized) * quantity
        pricing["benchmark_note"] = valuation_note
        item["pricing"] = pricing
        catalog_item = catalog_items.get(item["skin_name"], {})
        item["icon_url"] = snapshot.get("icon_url") or catalog_item.get("icon_url")
        item["item_kind"] = catalog_item.get("item_kind") or "unknown"
        item["collection_name"] = snapshot.get("collection_name")
        item["holding_days"] = int(float(analysis.get("days_held") or 0))
        item["listed_days"] = analysis.get("days_listed")
        item["strategy_label"] = ACCOUNT_STRATEGY_LABELS.get(
            item.get("strategy"), item.get("strategy") or ""
        )
        item["realized_profit_cents"] = (
            analysis.get("profit_cents") if item.get("status") == "sold" else None
        )
        item["realized_roi_percent"] = (
            analysis.get("roi_percent") if item.get("status") == "sold" else None
        )

        if item.get("status") in {"owned", "listed"}:
            active_count += quantity
            invested += total_cost_cents
            median = effective_median
            if median is not None:
                market_value += int(median) * quantity
                market_value_positions += 1
            if pricing.get("unrealized_profit_cents") is not None:
                unrealized += int(pricing["unrealized_profit_cents"])
                unrealized_positions += 1
            if pricing.get("is_stale"):
                stale_count += 1
        elif item.get("status") == "sold":
            sold_count += quantity
            realized += int(analysis.get("profit_cents") or 0)

    if sort_mode == "stale":
        items.sort(
            key=lambda item: (
                bool(item.get("pricing", {}).get("is_stale")),
                item.get("pricing", {}).get("listing_age_days") or -1,
            ),
            reverse=True,
        )
    elif sort_mode == "profit_high":
        items.sort(
            key=lambda item: (
                item.get("realized_profit_cents")
                if item.get("status") == "sold"
                else item.get("pricing", {}).get("unrealized_profit_cents")
            )
            if (
                item.get("realized_profit_cents") is not None
                or item.get("pricing", {}).get("unrealized_profit_cents") is not None
            )
            else -10**30,
            reverse=True,
        )
    elif sort_mode == "value_high":
        items.sort(
            key=lambda item: (
                item.get("pricing", {}).get("market_median_cents") or -1
            )
            * int(item.get("quantity") or 1),
            reverse=True,
        )
    items = items[:limit]

    return {
        "items": items,
        "summary": {
            "active_count": active_count,
            "sold_count": sold_count,
            "invested_cents": invested,
            "market_value_cents": market_value if market_value_positions else None,
            "market_value_positions": market_value_positions,
            "unrealized_profit_cents": unrealized if unrealized_positions else None,
            "unrealized_positions": unrealized_positions,
            "realized_profit_cents": realized,
            "stale_count": stale_count,
        },
        "pricing_note": (
            "Текущая оценка использует минимальный активный buy-now ask CSFloat; "
            "если он недоступен, применяется история продаж, затем сохранённая "
            "оценка предмета."
        ),
    }


def handle_account_post(handler, parsed):
    if parsed.path not in {
        "/api/auth/logout",
        "/api/account/import-csfloat",
        "/api/account/inventory",
        "/api/account/trades",
        "/api/account/profile",
    }:
        return False

    principal = current_account_session(handler)
    if parsed.path == "/api/auth/logout" and principal is None:
        violation = account_request_violation(handler, "account")
        if violation is not None:
            status, message = violation
            json_response(handler, status, {"error": message})
        else:
            json_response(
                handler,
                200,
                {"message": "Сессия завершена."},
                extra_headers=account_cookie_headers(clear=True),
            )
        return True
    if principal is None:
        json_response(handler, 401, {"error": "unauthorized"})
        return True
    violation = account_request_violation(handler, "account", require_csrf=True)
    if violation is not None:
        status, message = violation
        json_response(handler, status, {"error": message})
        return True
    try:
        payload = read_json_request(handler)
        service = get_account_service()
        session_token = request_cookie_value(handler, ACCOUNT_SESSION_COOKIE)
        user_id = principal["user"]["id"]
        if parsed.path == "/api/account/import-csfloat":
            if auth_rate_limited(
                handler,
                "account-csfloat-import",
                user_id,
                limit=30,
                window_seconds=600,
            ):
                json_response(
                    handler,
                    429,
                    {
                        "error": "Too many CSFloat imports. Try again shortly.",
                        "error_code": "import_rate_limited",
                        "retryable": True,
                    },
                    extra_headers=(("Retry-After", "60"),),
                )
                return True
            status, result, extra_headers = build_account_csfloat_import_response(
                payload
            )
            json_response(
                handler, status, result, extra_headers=extra_headers
            )
            return True
        if parsed.path == "/api/auth/logout":
            service.revoke_session(session_token)
            json_response(
                handler,
                200,
                {"message": "Сессия завершена."},
                extra_headers=account_cookie_headers(clear=True),
            )
            return True

        if parsed.path == "/api/account/profile":
            action = payload.pop("action", "")
            if action == "update_profile":
                unknown = sorted(
                    set(payload)
                    - {"display_name", "stale_after_days", "default_fee_percent"}
                )
                if unknown:
                    raise account_backend.ValidationError(
                        f"unexpected fields: {', '.join(unknown)}"
                    )
                user = service.update_profile(user_id, **payload)
                json_response(handler, 200, {"user": user})
                return True
            if action == "change_password":
                unknown = sorted(set(payload) - {"current_password", "new_password"})
                if unknown:
                    raise account_backend.ValidationError(
                        f"unexpected fields: {', '.join(unknown)}"
                    )
                service.update_password(
                    user_id,
                    payload.get("current_password", ""),
                    payload.get("new_password", ""),
                )
                json_response(
                    handler,
                    200,
                    {
                        "message": "Пароль изменён. Войдите снова.",
                        "reauthentication_required": True,
                    },
                    extra_headers=account_cookie_headers(clear=True),
                )
                return True
            raise account_backend.ValidationError("unknown profile action")

        if parsed.path == "/api/account/trades":
            action = str(payload.pop("action", "")).strip().lower()
            trade_id = payload.pop("trade_id", None)
            expected_revision = payload.pop("expected_revision", None)
            if action == "create":
                if trade_id is not None or expected_revision is not None:
                    raise account_backend.ValidationError(
                        "create trade does not accept trade_id"
                    )
                allowed = {"traded_at", "notes", "outgoing", "incoming"}
                unknown = sorted(set(payload) - allowed)
                if unknown:
                    raise account_backend.ValidationError(
                        f"unexpected fields: {', '.join(unknown)}"
                    )
                trade = service.create_inventory_trade(user_id, **payload)
            elif action == "update":
                if trade_id is None:
                    raise account_backend.ValidationError("trade_id is required")
                allowed = {"traded_at", "notes", "outgoing", "incoming"}
                unknown = sorted(set(payload) - allowed)
                if unknown:
                    raise account_backend.ValidationError(
                        f"unexpected fields: {', '.join(unknown)}"
                    )
                trade = service.update_inventory_trade(
                    user_id,
                    trade_id,
                    expected_revision=expected_revision,
                    **payload,
                )
            elif action in {"void", "delete"}:
                if trade_id is None:
                    raise account_backend.ValidationError("trade_id is required")
                if payload:
                    raise account_backend.ValidationError(
                        "void trade accepts no additional fields"
                    )
                trade = service.void_inventory_trade(
                    user_id,
                    trade_id,
                    expected_revision=expected_revision,
                )
            else:
                raise account_backend.ValidationError("unknown trade action")
            json_response(handler, 200, {"trade": trade})
            return True

        action = str(payload.pop("action", "")).strip().lower()
        item_id = payload.pop("item_id", None)
        # A blank hidden input is part of the browser's FormData for a new item.
        # Treat only that blank sentinel as absent so older cached account pages
        # can create purchases, while non-empty IDs remain forbidden on create.
        if item_id == "":
            item_id = None
        expected_revision = payload.pop("expected_revision", None)
        if action == "create":
            if item_id is not None or expected_revision is not None:
                raise account_backend.ValidationError("create does not accept item_id")
            allowed = {
                "skin_name",
                "buy_price_cents",
                "quantity",
                "bought_at",
                "float_value",
                "stickers",
                "source_url",
                "strategy",
                "listed_price_cents",
                "listed_at",
                "fee_percent",
                "notes",
            }
            unknown = sorted(set(payload) - allowed)
            if unknown:
                raise account_backend.ValidationError(
                    f"unexpected fields: {', '.join(unknown)}"
                )
            item = service.create_inventory_item(user_id, **payload)
        elif action == "update":
            if item_id is None:
                raise account_backend.ValidationError("item_id is required")
            allowed = {
                "skin_name",
                "buy_price_cents",
                "quantity",
                "bought_at",
                "float_value",
                "stickers",
                "source_url",
                "strategy",
                "listed_price_cents",
                "listed_at",
                "fee_percent",
                "notes",
            }
            unknown = sorted(set(payload) - allowed)
            if unknown:
                raise account_backend.ValidationError(
                    f"unexpected fields: {', '.join(unknown)}"
                )
            item = service.update_inventory_item(
                user_id,
                item_id,
                expected_revision=expected_revision,
                **payload,
            )
        elif action == "mark_sold":
            unknown = sorted(
                set(payload) - {"sold_price_cents", "sold_at", "fee_percent"}
            )
            if unknown:
                raise account_backend.ValidationError(
                    f"unexpected fields: {', '.join(unknown)}"
                )
            item = service.mark_inventory_sold(
                user_id,
                item_id,
                payload.get("sold_price_cents"),
                sold_at=payload.get("sold_at"),
                fee_percent=payload.get("fee_percent"),
                expected_revision=expected_revision,
            )
        elif action == "update_sale":
            unknown = sorted(
                set(payload) - {"sold_price_cents", "sold_at", "fee_percent"}
            )
            if unknown:
                raise account_backend.ValidationError(
                    f"unexpected fields: {', '.join(unknown)}"
                )
            item = service.update_inventory_sale(
                user_id,
                item_id,
                payload.get("sold_price_cents"),
                sold_at=payload.get("sold_at"),
                fee_percent=payload.get("fee_percent"),
                expected_revision=expected_revision,
            )
        elif action == "undo_sale":
            if payload:
                raise account_backend.ValidationError("undo_sale accepts no fields")
            item = service.undo_inventory_sale(
                user_id, item_id, expected_revision=expected_revision
            )
        elif action == "archive":
            if payload:
                raise account_backend.ValidationError("archive accepts no fields")
            item = service.archive_inventory_item(
                user_id, item_id, expected_revision=expected_revision
            )
        elif action == "restore":
            if payload:
                raise account_backend.ValidationError("restore accepts no fields")
            item = service.restore_inventory_item(
                user_id, item_id, expected_revision=expected_revision
            )
        else:
            raise account_backend.ValidationError("unknown inventory action")
        json_response(handler, 200, {"item": item})
        return True
    except ValueError as exc:
        json_response(handler, 400, {"error": str(exc)})
    except account_backend.AccountError as exc:
        account_error_response(handler, exc)
    except (OSError, sqlite3.Error):
        json_response(handler, 503, {"error": "account service is unavailable"})
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        self._account_session_cache = None
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/health":
            payload = build_health_payload()
            json_response(self, 200 if payload["ok"] else 503, payload)
            return

        if parsed.path in {"/admin/users", "/api/admin/users"}:
            if not is_direct_local_request(self):
                if parsed.path.startswith("/api/"):
                    json_response(
                        self,
                        404,
                        {"error": "not found", "error_code": "local_only"},
                    )
                else:
                    json_response(self, 404, {"error": "not found"})
                return
            query_token = query.get("token", [""])[0]
            if (
                parsed.path == "/admin/users"
                and query_token
                and secrets.compare_digest(str(query_token), DASHBOARD_TOKEN)
            ):
                begin_browser_session(self, parsed)
                return
            if not is_legacy_dashboard_authorized(self, query):
                json_response(
                    self,
                    401,
                    {
                        "error": "local owner authorization is required",
                        "error_code": "owner_authorization_required",
                    },
                )
                return
            if parsed.path == "/admin/users":
                admin_users_html_response(self)
                return
            try:
                payload = build_admin_users_payload(query)
            except (ValueError, account_backend.AccountError) as exc:
                account_error_response(self, exc)
            except (OSError, sqlite3.Error):
                json_response(
                    self,
                    503,
                    {
                        "error": "account administration is unavailable",
                        "error_code": "service_unavailable",
                    },
                )
            else:
                json_response(self, 200, payload)
            return

        auth_pages = {
            "/login": "login",
            "/register": "register",
            "/forgot-password": "forgot",
            "/reset-password": "reset",
        }
        if parsed.path in auth_pages:
            query_token = query.get("token", [""])[0]
            if query_token and secrets.compare_digest(str(query_token), DASHBOARD_TOKEN):
                begin_browser_session(self, parsed)
                return
            reauthenticate = query.get("reauth", ["0"])[0] == "1"
            if (
                current_account_session(self)
                and parsed.path in {"/login", "/register"}
                and not reauthenticate
            ):
                redirect_response(self, "/account")
                return
            auth_html_response(self, auth_pages[parsed.path])
            return

        if parsed.path == "/account":
            if current_account_session(self) is None:
                redirect_response(self, "/login?next=%2Faccount")
                return
            account_html_response(self)
            return

        if parsed.path == "/api/auth/me":
            principal = current_account_session(self)
            if principal is None:
                json_response(self, 401, {"error": "unauthorized"})
                return
            service = get_account_service()
            json_response(
                self,
                200,
                {
                    "user": principal["user"],
                    "session": principal["session"],
                    "password_reset_email_configured": not isinstance(
                        service.password_reset_sender,
                        account_backend.DisabledPasswordResetSender,
                    ),
                },
            )
            return

        if parsed.path in {
            "/api/account/inventory",
            "/api/account/trades",
            "/api/account/item-search",
            "/api/account/item-quote",
            "/api/account/skin-search",
            "/api/account/activity",
        }:
            principal = current_account_session(self)
            if principal is None:
                json_response(self, 401, {"error": "unauthorized"})
                return
            try:
                if parsed.path == "/api/account/inventory":
                    payload = build_account_inventory_payload(principal["user"], query)
                elif parsed.path == "/api/account/trades":
                    try:
                        limit = int(query.get("limit", ["100"])[0])
                    except ValueError as exc:
                        raise account_backend.ValidationError(
                            "limit must be an integer"
                        ) from exc
                    payload = {
                        "items": get_account_service().list_inventory_trades(
                            principal["user"]["id"],
                            limit=min(max(limit, 1), 200),
                        )
                    }
                elif parsed.path == "/api/account/item-search":
                    payload = build_account_item_search_payload(query)
                elif parsed.path == "/api/account/item-quote":
                    payload = build_account_item_quote_payload(query)
                elif parsed.path == "/api/account/skin-search":
                    names = inventory_market.search_skin_names(
                        TREND_DB_PATH,
                        query.get("q", [""])[0],
                        limit=20,
                    )
                    snapshots = inventory_market.get_market_snapshots(
                        TREND_DB_PATH, names
                    )
                    payload = {
                        "items": [
                            {
                                "market_hash_name": name,
                                "current_price_cents": snapshots[name].get(
                                    "market_median_cents"
                                ),
                                "market_as_of": snapshots[name].get("market_as_of"),
                                "icon_url": snapshots[name].get("icon_url"),
                            }
                            for name in names
                        ]
                    }
                else:
                    try:
                        limit = int(query.get("limit", ["100"])[0])
                    except ValueError as exc:
                        raise account_backend.ValidationError(
                            "limit must be an integer"
                        ) from exc
                    payload = {
                        "items": get_account_service().list_activity(
                            principal["user"]["id"], limit=min(max(limit, 1), 200)
                        )
                    }
                json_response(self, 200, payload)
            except (ValueError, account_backend.AccountError) as exc:
                account_error_response(self, exc)
            except (OSError, sqlite3.Error):
                json_response(self, 503, {"error": "account data is unavailable"})
            return

        if parsed.path in {
            "/",
            "/dashboard",
            "/deal",
            "/neural",
            "/deals",
            "/strategy",
            "/catalog",
            "/knives",
            "/market-history",
        }:
            query_token = query.get("token", [""])[0]
            if query_token and secrets.compare_digest(str(query_token), DASHBOARD_TOKEN):
                begin_browser_session(self, parsed)
                return

        if parsed.path == "/":
            if is_admin_authorized(self, query):
                html_response(self)
                return

            if current_account_session(self) is not None:
                redirect_response(self, "/deals")
                return

            public_html_response(self)
            return

        if parsed.path == "/dashboard":
            if not is_admin_authorized(self, query):
                json_response(self, 401, {"error": "unauthorized", "hint": "open /dashboard?token=YOUR_TOKEN"})
                return

            html_response(self)
            return

        if parsed.path == "/deal":
            if not is_authorized(self, query):
                json_response(self, 401, {"error": "unauthorized", "hint": "open /deal?token=YOUR_TOKEN"})
                return

            deal_html_response(self)
            return

        if parsed.path == "/neural":
            if not is_authorized(self, query):
                json_response(self, 401, {"error": "unauthorized", "hint": "open /neural?token=YOUR_TOKEN"})
                return

            # The dedicated Nova page duplicated the all-model comparison.
            # Keep the legacy URL and Nova API compatible without keeping two UIs.
            redirect_response(self, "/deal")
            return

        if parsed.path == "/deals":
            if not is_authorized(self, query):
                json_response(self, 401, {"error": "unauthorized", "hint": "open /deals?token=YOUR_TOKEN"})
                return

            deals_html_response(self)
            return

        if parsed.path == "/strategy":
            if not is_authorized(self, query):
                json_response(
                    self,
                    401,
                    {
                        "error": "unauthorized",
                        "hint": "open /strategy?token=YOUR_TOKEN",
                    },
                )
                return

            strategy_html_response(self)
            return

        if parsed.path == "/knives":
            if not is_authorized(self, query):
                json_response(
                    self,
                    401,
                    {
                        "error": "unauthorized",
                        "hint": "open /knives?token=YOUR_TOKEN",
                    },
                )
                return

            knives_html_response(self)
            return

        if parsed.path == "/catalog":
            if not is_authorized(self, query):
                json_response(
                    self,
                    401,
                    {
                        "error": "unauthorized",
                        "hint": "open /catalog?token=YOUR_TOKEN",
                    },
                )
                return

            catalog_html_response(self)
            return

        if parsed.path == "/market-history":
            if not is_authorized(self, query):
                json_response(
                    self,
                    401,
                    {
                        "error": "unauthorized",
                        "hint": "open /market-history?token=YOUR_TOKEN",
                    },
                )
                return

            market_history_html_response(self)
            return

        if not is_authorized(self, query):
            json_response(self, 401, {"error": "unauthorized"})
            return

        if parsed.path in {
            "/api/dashboard",
            "/api/scanner-control",
            "/api/scanner-targets",
            "/api/model-training",
            "/api/schedule",
            "/api/listings",
            "/api/deal-candidates",
            "/download/db",
        } and not is_admin_authorized(self, query):
            json_response(self, 403, {"error": "admin role is required"})
            return

        if parsed.path == "/api/dashboard":
            json_response(self, 200, build_dashboard_payload())
            return

        if parsed.path == "/api/strategy/overview":
            try:
                json_response(self, 200, build_strategy_overview_payload())
            except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
                json_response(self, 503, {"error": "strategy overview is unavailable"})
            return

        if parsed.path == "/api/strategy/trend":
            name = query.get("name", [""])[0]
            horizon = query.get("horizon", ["7"])[0]
            limit = query.get("limit", ["24"])[0]
            try:
                payload = build_strategy_trend_payload(
                    name,
                    horizon=horizon,
                    limit=limit,
                )
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error, json.JSONDecodeError):
                json_response(self, 503, {"error": "trend data is unavailable"})
                return
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/strategy/deals":
            limit = query.get("limit", ["30"])[0]
            max_age_hours = query.get("max_age_hours", ["24"])[0]
            try:
                payload = build_strategy_deals_payload(
                    limit=limit,
                    max_age_hours=max_age_hours,
                )
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error, json.JSONDecodeError):
                json_response(self, 503, {"error": "risk data is unavailable"})
                return
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/market/liquid":
            max_price = query.get("max_price", ["10"])[0]
            max_price_cents = parse_price_filter_cents(max_price)
            if max_price_cents is None or max_price_cents <= 0:
                json_response(self, 400, {"error": "max_price must be a positive dollar amount"})
                return
            try:
                payload = build_liquid_market_dashboard_payload(
                    max_price_cents=max_price_cents,
                    min_sales_per_day=query.get("min_sales_per_day", ["1"])[0],
                    min_liquidity_score=query.get("min_liquidity", ["0"])[0],
                    search=query.get("search", [""])[0],
                    sort=query.get("sort", ["liquidity"])[0],
                    limit=query.get("limit", ["50"])[0],
                    live_age_hours=query.get("live_age_hours", ["24"])[0],
                )
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error, json.JSONDecodeError):
                json_response(self, 503, {"error": "liquid market data is unavailable"})
                return
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/catalog":
            try:
                payload = build_catalog_dashboard_payload(
                    tab=query.get("tab", ["skins"])[0],
                    search=query.get("search", [""])[0],
                    weapon=query.get("weapon", [""])[0],
                    wear=query.get("wear", [""])[0],
                    collection=query.get("collection", [""])[0],
                    case=query.get("case", [""])[0],
                    sticker_type=query.get("sticker_type", [""])[0],
                    limit=query.get("limit", ["48"])[0],
                    offset=query.get("offset", ["0"])[0],
                )
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error, json.JSONDecodeError):
                json_response(self, 503, {"error": "catalog data is unavailable"})
                return
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/market-history/status":
            json_response(self, 200, build_market_history_dashboard_status())
            return

        if parsed.path == "/api/market-history":
            try:
                payload = build_market_history_dashboard_payload(
                    kind=query.get("kind", ["confirmed_sale"])[0],
                    search=query.get("search", [""])[0],
                    marketplace=query.get("marketplace", [""])[0],
                    provider=query.get("provider", [""])[0],
                    days=query.get("days", ["90"])[0],
                    limit=query.get("limit", ["100"])[0],
                    offset=query.get("offset", ["0"])[0],
                )
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error):
                json_response(
                    self, 503, {"error": "market history is unavailable"}
                )
                return
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/market/knives":
            try:
                payload = build_knives_dashboard_payload(
                    search=query.get("search", [""])[0],
                    knife_type=query.get("knife_type", [""])[0],
                    finish=query.get("finish", [""])[0],
                    phase=query.get("phase", [""])[0],
                    wear=query.get("wear", [""])[0],
                    collection=query.get("collection", [""])[0],
                    min_price=query.get("min_price", [None])[0],
                    max_price=query.get("max_price", [None])[0],
                    min_liquidity=query.get("min_liquidity", ["0"])[0],
                    direction=query.get("direction", ["all"])[0],
                    sort=query.get("sort", ["liquidity"])[0],
                    limit=query.get("limit", ["100"])[0],
                    offset=query.get("offset", ["0"])[0],
                )
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error, json.JSONDecodeError):
                json_response(self, 503, {"error": "knife market data is unavailable"})
                return
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/market/collections":
            try:
                payload = build_collections_dashboard_payload(
                    search=query.get("search", [""])[0],
                    sort=query.get("sort", ["return_30d"])[0],
                    limit=query.get("limit", ["200"])[0],
                    include_unknown=query.get("include_unknown", ["1"])[0] != "0",
                )
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error, json.JSONDecodeError):
                json_response(self, 503, {"error": "collection data is unavailable"})
                return
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/market/collection":
            try:
                payload = build_collection_detail_dashboard_payload(
                    query.get("name", [""])[0],
                    days=query.get("days", ["90"])[0],
                )
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error, json.JSONDecodeError):
                json_response(self, 503, {"error": "collection detail is unavailable"})
                return
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/market/skin-series":
            try:
                payload = build_skin_series_dashboard_payload(
                    query.get("name", [""])[0],
                    days=query.get("days", ["90"])[0],
                )
            except ValueError as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error, json.JSONDecodeError):
                json_response(self, 503, {"error": "skin series is unavailable"})
                return
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/scanner-control":
            try:
                json_response(self, 200, build_scanner_control_payload())
            except scanner_control.ScannerControlError:
                json_response(self, 503, {"error": "scanner control state is unavailable"})
            return

        if parsed.path == "/api/scanner-targets":
            profile = query.get("profile", [""])[0].strip()
            try:
                if profile:
                    payload = build_scanner_target_preview_payload(
                        profile,
                        search=query.get("search", [""])[0],
                        limit=query.get("limit", ["50"])[0],
                        offset=query.get("offset", ["0"])[0],
                    )
                else:
                    payload = build_scanner_targets_payload()
            except (TypeError, ValueError) as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except scanner_control.ScannerControlError:
                json_response(
                    self,
                    503,
                    {"error": "scanner target controls are unavailable"},
                )
                return
            except (OSError, sqlite3.Error):
                json_response(
                    self,
                    503,
                    {"error": "scanner target preview is unavailable"},
                )
                return
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/model-training":
            try:
                json_response(self, 200, build_model_training_payload())
            except model_training_jobs.TrainingJobError:
                json_response(
                    self, 503, {"error": "model training state is unavailable"}
                )
            except (OSError, ValueError):
                json_response(
                    self, 503, {"error": "model training status could not be read"}
                )
            return

        if parsed.path == "/api/weapon-skins":
            weapon_name = query.get("weapon", [""])[0]
            json_response(self, 200, build_weapon_skins_payload(weapon_name))
            return

        if parsed.path == "/api/tournament-stickers":
            tournament = query.get("tournament", [""])[0]
            json_response(self, 200, build_tournament_stickers_payload(tournament))
            return

        if parsed.path == "/api/skin-detail":
            market_hash_name = query.get("name", [""])[0]
            json_response(self, 200, build_skin_detail_payload(market_hash_name))
            return

        if parsed.path == "/api/sticker-detail":
            sticker_name = query.get("name", [""])[0]
            sticker_id = query.get("sticker_id", [""])[0]
            json_response(self, 200, build_sticker_detail_payload(sticker_name, sticker_id))
            return

        if parsed.path == "/api/schedule":
            try:
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                json_response(self, 400, {"error": "offset and limit must be integers"})
                return

            search = query.get("search", [""])[0]

            try:
                with connect_db() as conn:
                    json_response(self, 200, fetch_schedule_page(conn, offset, limit, search))
            except sqlite3.Error:
                json_response(self, 503, {"error": "schedule database is unavailable"})
            return

        if parsed.path == "/api/listings":
            try:
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                json_response(self, 400, {"error": "offset and limit must be integers"})
                return

            search = query.get("search", [""])[0]
            scope = query.get("scope", ["skin"])[0]

            try:
                with connect_db() as conn:
                    json_response(
                        self,
                        200,
                        fetch_listings_page_fast(
                            conn, offset, limit, search, scope
                        ),
                    )
            except listing_search.ListingSearchError as exc:
                json_response(self, 400, {"error": str(exc)})
            except sqlite3.Error:
                json_response(self, 503, {"error": "listing database is unavailable"})
            return

        if parsed.path == "/api/deal-candidates":
            try:
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                json_response(self, 400, {"error": "offset and limit must be integers"})
                return

            search = query.get("search", [""])[0]
            verdicts = [
                item.strip()
                for item in query.get("verdicts", [""])[0].split(",")
                if item.strip()
            ]

            try:
                with connect_db() as conn:
                    json_response(self, 200, fetch_deal_candidates_page(conn, offset, limit, search, verdicts))
            except sqlite3.Error as exc:
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/model-deal-comparison":
            try:
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                json_response(self, 400, {"error": "offset and limit must be integers"})
                return

            search = query.get("search", [""])[0]
            category = query.get("category", ["all"])[0]
            listing_type = query.get("listing_type", ["all"])[0]
            sort_mode = query.get("sort", ["seen_new"])[0]
            seen_hours = query.get("seen_hours", ["0"])[0]
            created_hours = query.get("created_hours", ["0"])[0]
            min_price = query.get("min_price", [""])[0]
            max_price = query.get("max_price", [""])[0]
            confidence = query.get("confidence", ["all"])[0]
            min_roi = query.get("min_roi", [""])[0]
            min_profit = query.get("min_profit", [""])[0]
            min_liquidity = query.get("min_liquidity", [""])[0]
            max_float = query.get("max_float", [""])[0]
            min_sticker_value = query.get("min_sticker_value", [""])[0]

            try:
                with connect_db() as conn:
                    json_response(
                        self,
                        200,
                        fetch_model_deal_comparison_page(
                            conn,
                            offset,
                            limit,
                            search,
                            category,
                            listing_type,
                            sort_mode,
                            seen_hours,
                            created_hours,
                            min_price,
                            max_price,
                            confidence,
                            min_roi,
                            min_profit,
                            min_liquidity,
                            max_float,
                            min_sticker_value,
                        ),
                    )
            except sqlite3.Error as exc:
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/neural-status":
            import neural_model_runtime

            json_response(self, 200, neural_model_runtime.model_status())
            return

        if parsed.path == "/download/db":
            if not DB_SNAPSHOT_LOCK.acquire(blocking=False):
                json_response(self, 429, {"error": "a database snapshot is already being created"})
                return
            snapshot_path = None
            try:
                snapshot_path = create_db_snapshot()
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                file_response(
                    self,
                    snapshot_path,
                    f"csfloat_history_{stamp}.db",
                    "application/vnd.sqlite3",
                )
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
            finally:
                if snapshot_path:
                    try:
                        os.remove(snapshot_path)
                    except OSError:
                        pass
                DB_SNAPSHOT_LOCK.release()
            return

        json_response(self, 404, {"error": "not found"})

    def do_POST(self):
        self._account_session_cache = None
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if handle_admin_users_post(self, parsed):
            return
        if handle_public_auth_post(self, parsed, query):
            return
        if handle_account_post(self, parsed):
            return

        if not is_authorized(self, query):
            json_response(self, 401, {"error": "unauthorized"})
            return

        if parsed.path in {
            "/api/model-training",
            "/api/scanner-control",
            "/api/scanner-targets",
        } and not is_admin_authorized(self, query):
            json_response(self, 403, {"error": "admin role is required"})
            return

        if parsed.path in {
            "/api/strategy/portfolio",
            "/api/strategy/backtest",
            "/api/strategy/advisor",
        }:
            violation = strategy_request_violation(self)
            if violation is not None:
                status, message = violation
                json_response(self, status, {"error": message})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > STRATEGY_BODY_LIMIT:
                json_response(self, 400, {"error": "invalid request body"})
                return
            try:
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
                if parsed.path == "/api/strategy/portfolio":
                    result = build_strategy_portfolio_payload(payload)
                elif parsed.path == "/api/strategy/advisor":
                    result = build_budget_advisor_payload(payload)
                else:
                    result = build_strategy_backtest_payload(payload)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except (OSError, sqlite3.Error):
                json_response(self, 503, {"error": "strategy data is unavailable"})
                return
            json_response(self, 200, result)
            return

        if parsed.path == "/api/model-training":
            violation = model_training_request_violation(self)
            if violation is not None:
                status, message = violation
                json_response(self, status, {"error": message})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MODEL_TRAINING_BODY_LIMIT:
                json_response(self, 400, {"error": "invalid request body"})
                return
            try:
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
                result = update_model_training_from_payload(payload)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except model_training_jobs.TrainingConflictError as exc:
                json_response(self, 409, {"error": str(exc)})
                return
            except model_training_jobs.TrainingJobError as exc:
                json_response(self, 409, {"error": str(exc)})
                return
            except OSError:
                json_response(self, 503, {"error": "model training action failed"})
                return
            json_response(self, 200, result)
            return

        if parsed.path == "/api/scanner-control":
            violation = scanner_mutation_request_violation(
                self, "scanner-control"
            )
            if violation is not None:
                status, message = violation
                json_response(self, status, {"error": message})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > SCANNER_CONTROL_BODY_LIMIT:
                json_response(self, 400, {"error": "invalid request body"})
                return
            try:
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
                result = update_scanner_control_from_payload(payload)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except scanner_control.ScannerControlError:
                json_response(self, 409, {"error": "scanner control state is invalid or unavailable"})
                return
            json_response(self, 200, result)
            return

        if parsed.path == "/api/scanner-targets":
            violation = scanner_mutation_request_violation(
                self, "scanner-targets"
            )
            if violation is not None:
                status, message = violation
                json_response(self, status, {"error": message})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > SCANNER_TARGET_BODY_LIMIT:
                json_response(self, 400, {"error": "invalid request body"})
                return
            try:
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
                result = update_scanner_targets_from_payload(payload)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
                json_response(self, 400, {"error": str(exc)})
                return
            except scanner_control.ScannerControlConflict as exc:
                json_response(self, 409, {"error": str(exc)})
                return
            except scanner_control.ScannerControlError:
                json_response(
                    self,
                    409,
                    {"error": "scanner target state is invalid or unavailable"},
                )
                return
            except OSError:
                json_response(self, 503, {"error": "scanner target update failed"})
                return
            json_response(self, 200, result)
            return

        if parsed.path in ("/api/predict-deal", "/api/predict-neural", "/api/fetch-csfloat-listing"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0

            if length <= 0 or length > 1024 * 1024:
                json_response(self, 400, {"error": "invalid request body"})
                return

            try:
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                json_response(self, 400, {"error": str(exc)})
                return

            if parsed.path == "/api/predict-deal":
                result = build_predict_deal_payload(payload)
            elif parsed.path == "/api/predict-neural":
                result = build_predict_neural_payload(payload)
            else:
                result = build_fetch_csfloat_listing_payload(payload)
            status = 500 if result.get("error") else 200
            json_response(self, status, result)
            return

        json_response(self, 404, {"error": "not found"})


def dashboard_startup_messages(*, reset_email_configured):
    """Return startup notices that are safe to persist in service logs."""

    reset_email_state = "configured" if reset_email_configured else "not configured"
    return (
        f"Dashboard running: http://{HOST}:{PORT}/",
        f"Account password-reset email: {reset_email_state}",
        f"Admin token stored in: {TOKEN_PATH}",
        "Start parser in another terminal: python parser_final.py",
    )


def main():
    try:
        with model_lifecycle.service_lifetime_lock("dashboard"):
            validate_account_runtime_config()
            account_service = get_account_service()
            ensure_dashboard_indexes()
            if Path(DB_PATH).exists():
                start_background_refresh("dashboard_static", build_dashboard_static_payload)
            threading.Thread(target=warm_price_model, daemon=True).start()
            threading.Thread(
                target=warm_account_inventory_market_data,
                name="account-inventory-market-warmup",
                daemon=True,
            ).start()
            server = ThreadingHTTPServer((HOST, PORT), Handler)
            reset_email_configured = not isinstance(
                account_service.password_reset_sender,
                account_backend.DisabledPasswordResetSender,
            )
            for message in dashboard_startup_messages(
                reset_email_configured=reset_email_configured
            ):
                print(message, flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("Dashboard stopped.")
            finally:
                server.server_close()
    except model_lifecycle.ModelLifecycleError as exc:
        raise SystemExit(f"Dashboard startup blocked: {exc}") from exc


if __name__ == "__main__":
    main()
