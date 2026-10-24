"""One visual system for every server-rendered page.

The dashboard grew page by page: each template carries its own palette,
header and component styles, and two later override layers patched them
again.  This module is the single final layer.  It maps every legacy colour
variable onto one token set (dark, light, midnight and graphite), gives all
pages the same header, brand, navigation and controls, and fixes layout
details that the per-page styles got wrong.

It only adds CSS and a small progressive-enhancement script; page scripts,
IDs and data contracts are untouched, so removing the injection restores the
previous look exactly.
"""

from __future__ import annotations

import html
import re

FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#a395ff"/><stop offset="1" stop-color="#5b4be0"/>'
    "</linearGradient></defs>"
    '<rect width="64" height="64" rx="16" fill="url(#g)"/>'
    '<path d="M14 42 L26 30 L34 37 L50 20" fill="none" stroke="#fff" '
    'stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>'
    '<circle cx="50" cy="20" r="4.5" fill="#fff"/>'
    "</svg>"
)

# URL-encoded once so CSS and <link rel="icon"> can embed it without a request.
_FAVICON_DATA_URI = "data:image/svg+xml," + (
    FAVICON_SVG.replace("%", "%25")
    .replace("#", "%23")
    .replace("<", "%3C")
    .replace(">", "%3E")
    .replace('"', "'")
)

_CHEVRON_DATA_URI = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E"
    "%3Cpath d='M3 4.5 6 7.5 9 4.5' fill='none' stroke='%238b95a9' stroke-width='1.6' "
    "stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E"
)

THEME_BOOT_SCRIPT = """
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <script id="cs2-theme-boot">
    (function () {
      var theme = 'dark';
      try {
        theme = localStorage.getItem('siteTheme') || localStorage.getItem('bestDealsTheme')
          || (localStorage.getItem('cs2_theme') === 'light' ? 'light' : 'dark');
      } catch (error) {}
      if (!/^(dark|light|midnight|graphite)$/.test(theme)) theme = 'dark';
      document.documentElement.setAttribute('data-cs-theme', theme);
    })();
  </script>
"""

UNIFIED_THEME_CSS = (
    r"""
  <style id="cs2-unified-theme">
    /* ---------- tokens ---------- */
    html { background: #07090e; }
    html[data-cs-theme="light"] { background: #f4f5fa; }
    html[data-cs-theme="midnight"] { background: #050a14; }
    html[data-cs-theme="graphite"] { background: #0e0e10; }
    body {
      --cs-font: "Inter", "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
      --cs-bg: #07090e;
      --cs-panel: #0f131c;
      --cs-panel-2: #151a26;
      --cs-sunken: #0a0d14;
      --cs-line: #1d2433;
      --cs-line-strong: #2a3346;
      --cs-text: #eceff6;
      --cs-muted: #8c96aa;
      --cs-faint: #5f6a7e;
      --cs-accent: #8b7bff;
      --cs-accent-strong: #7564f5;
      --cs-accent-2: #2dd4a7;
      --cs-good: #34d399;
      --cs-warn: #f5b84a;
      --cs-bad: #f87171;
      --cs-accent-soft: rgba(139, 123, 255, .13);
      --cs-accent-line: rgba(139, 123, 255, .42);
      --cs-header: rgba(7, 9, 14, .78);
      --cs-glow-1: rgba(124, 108, 255, .17);
      --cs-glow-2: rgba(45, 212, 167, .055);
      --cs-image: #0b0f17;
      --cs-shadow: 0 1px 0 rgba(255, 255, 255, .025) inset, 0 18px 40px -24px rgba(0, 0, 0, .8);
      --cs-shadow-pop: 0 24px 60px -18px rgba(0, 0, 0, .75);
      --cs-hover: rgba(255, 255, 255, .045);

      --bg: var(--cs-bg) !important;
      --panel: var(--cs-panel) !important;
      --panel-2: var(--cs-panel-2) !important;
      --panel2: var(--cs-panel-2) !important;
      --control-bg: var(--cs-sunken) !important;
      --table-head: var(--cs-panel-2) !important;
      --image-bg: var(--cs-image) !important;
      --visual-bg: var(--cs-sunken) !important;
      --modal-bg: var(--cs-panel) !important;
      --subpanel-bg: var(--cs-panel-2) !important;
      --text: var(--cs-text) !important;
      --muted: var(--cs-muted) !important;
      --line: var(--cs-line) !important;
      --good: var(--cs-good) !important;
      --warn: var(--cs-warn) !important;
      --bad: var(--cs-bad) !important;
      --accent: var(--cs-accent) !important;
      --accent-2: var(--cs-accent-2) !important;
      --cyan: #67c8ff !important;
      --accent-soft: var(--cs-accent-soft) !important;
      --accent-line: var(--cs-accent-line) !important;
      --shadow: var(--cs-shadow) !important;
      --shadow-sm: var(--cs-shadow) !important;
      --shadow-lg: var(--cs-shadow-pop) !important;
      --chart-grid: var(--cs-line) !important;
      color-scheme: dark;
    }
    html[data-cs-theme="light"] body {
      --cs-bg: #f4f5fa;
      --cs-panel: #ffffff;
      --cs-panel-2: #f6f7fb;
      --cs-sunken: #f1f3f8;
      --cs-line: #e2e5ee;
      --cs-line-strong: #d2d7e3;
      --cs-text: #131a29;
      --cs-muted: #5b6578;
      --cs-faint: #8a93a6;
      --cs-accent: #6a56f0;
      --cs-accent-strong: #5843e0;
      --cs-accent-2: #0d9488;
      --cs-good: #0f9a69;
      --cs-warn: #b7791f;
      --cs-bad: #d43f3f;
      --cs-accent-soft: rgba(106, 86, 240, .09);
      --cs-accent-line: rgba(106, 86, 240, .34);
      --cs-header: rgba(244, 245, 250, .82);
      --cs-glow-1: rgba(106, 86, 240, .10);
      --cs-glow-2: rgba(13, 148, 136, .05);
      --cs-image: #eef0f6;
      --cs-shadow: 0 1px 2px rgba(16, 24, 40, .04), 0 10px 28px -18px rgba(16, 24, 40, .18);
      --cs-shadow-pop: 0 24px 60px -20px rgba(16, 24, 40, .28);
      --cs-hover: rgba(19, 26, 41, .04);
      --cyan: #0b84c6 !important;
      color-scheme: light;
    }
    html[data-cs-theme="midnight"] body {
      --cs-bg: #050a14;
      --cs-panel: #0b1322;
      --cs-panel-2: #101b2e;
      --cs-sunken: #08101d;
      --cs-line: #182640;
      --cs-line-strong: #22345a;
      --cs-text: #e8f1ff;
      --cs-muted: #8ca1bd;
      --cs-accent: #6f9bff;
      --cs-accent-strong: #5a86f0;
      --cs-accent-soft: rgba(111, 155, 255, .14);
      --cs-accent-line: rgba(111, 155, 255, .42);
      --cs-header: rgba(5, 10, 20, .8);
      --cs-glow-1: rgba(80, 130, 255, .16);
      --cs-image: #08101c;
    }
    html[data-cs-theme="graphite"] body {
      --cs-bg: #0e0e10;
      --cs-panel: #17171a;
      --cs-panel-2: #1e1e22;
      --cs-sunken: #121214;
      --cs-line: #29292e;
      --cs-line-strong: #36363c;
      --cs-text: #f2f2f3;
      --cs-muted: #a1a1aa;
      --cs-accent: #e5b546;
      --cs-accent-strong: #d4a234;
      --cs-accent-soft: rgba(229, 181, 70, .12);
      --cs-accent-line: rgba(229, 181, 70, .4);
      --cs-header: rgba(14, 14, 16, .82);
      --cs-glow-1: rgba(229, 181, 70, .07);
      --cs-glow-2: rgba(255, 255, 255, .02);
      --cs-image: #111113;
    }

    /* ---------- base ---------- */
    body {
      font-family: var(--cs-font) !important;
      color: var(--cs-text);
      background:
        radial-gradient(1200px 620px at 6% -14%, var(--cs-glow-1), transparent 62%),
        radial-gradient(900px 520px at 104% -10%, var(--cs-glow-2), transparent 60%),
        var(--cs-bg) !important;
      background-attachment: fixed !important;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      font-variant-numeric: tabular-nums;
      text-rendering: optimizeLegibility;
    }
    ::selection { background: color-mix(in srgb, var(--cs-accent) 34%, transparent); color: var(--cs-text); }
    * { scrollbar-width: thin; scrollbar-color: var(--cs-line-strong) transparent; }
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { border: 3px solid transparent; border-radius: 10px; background: var(--cs-line-strong); background-clip: padding-box; }
    :focus-visible { outline: 2px solid color-mix(in srgb, var(--cs-accent) 70%, transparent) !important; outline-offset: 2px; }
    a { text-underline-offset: 3px; }
    h1, h2, h3 { color: var(--cs-text); }
    .muted, .meta, .sub { color: var(--cs-muted); }

    /* ---------- header ---------- */
    body > header, body > .site-header {
      position: sticky !important;
      top: 0 !important;
      z-index: 40 !important;
      border-bottom: 1px solid var(--cs-line) !important;
      background: var(--cs-header) !important;
      box-shadow: none !important;
      backdrop-filter: saturate(170%) blur(18px) !important;
      -webkit-backdrop-filter: saturate(170%) blur(18px) !important;
      transition: box-shadow .2s ease;
    }
    body > header.cs-scrolled { box-shadow: 0 10px 30px -18px rgba(0, 0, 0, .7) !important; }
    body > header > .topbar, body > header > .top, body > header > .wrap, body > header > .shell {
      display: grid !important;
      grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) !important;
      align-items: center !important;
      gap: 18px !important;
      width: min(1440px, calc(100% - 40px)) !important;
      max-width: none !important;
      min-height: 64px !important;
      margin: 0 auto !important;
      padding: 0 !important;
    }
    body > header > * > :first-child { grid-column: 1; justify-self: start; }
    body > header > * > nav:not(.nav) { grid-column: 2; justify-self: center; }
    body > header > * > :last-child:not(nav):not(:first-child) { grid-column: 3; justify-self: end; }

    header .site-brand, header .brand {
      display: inline-flex !important;
      align-items: center !important;
      gap: 11px !important;
      min-width: 0;
      color: var(--cs-text) !important;
      font-size: 15px !important;
      font-weight: 750 !important;
      letter-spacing: -.015em !important;
      text-decoration: none !important;
      white-space: nowrap;
    }
    header .site-brand > span:last-child, header .brand .brand-copy { display: grid !important; min-width: 0; line-height: 1.2; }
    header .site-brand strong, header .brand strong { font-size: 15px !important; font-weight: 750 !important; letter-spacing: -.015em !important; }
    header .site-brand small, header .brand small {
      display: block !important;
      max-width: 230px;
      margin-top: 2px !important;
      overflow: hidden;
      color: var(--cs-muted) !important;
      font-size: 11px !important;
      font-weight: 500 !important;
      letter-spacing: 0 !important;
      text-overflow: ellipsis;
      text-transform: none !important;
      white-space: nowrap;
    }
    header .brand-mark, header .brand .mark, header .brand.cs-brand-plain::before {
      display: inline-block !important;
      flex: 0 0 auto;
      width: 34px !important;
      height: 34px !important;
      padding: 0 !important;
      border: 0 !important;
      border-radius: 10px !important;
      background: url("__FAVICON__") center / cover no-repeat !important;
      box-shadow: 0 8px 20px -8px color-mix(in srgb, var(--cs-accent) 75%, transparent) !important;
      color: transparent !important;
      font-size: 0 !important;
      line-height: 0 !important;
      overflow: hidden;
    }
    header .brand.cs-brand-plain::before { content: ""; }

    header .lean-nav, header .site-nav, body > header > * > nav:not(.nav) {
      display: flex !important;
      align-items: center !important;
      gap: 2px !important;
      width: auto !important;
      max-width: 100%;
      margin: 0 !important;
      padding: 4px !important;
      border: 1px solid var(--cs-line) !important;
      border-radius: 13px !important;
      background: color-mix(in srgb, var(--cs-panel) 72%, transparent) !important;
      box-shadow: none !important;
      overflow-x: auto !important;
      scrollbar-width: none;
    }
    header .lean-nav::-webkit-scrollbar { display: none; }
    header .lean-nav-link, header .lean-more > summary, header .site-nav a, body > header > * > nav:not(.nav) > a {
      display: inline-flex !important;
      align-items: center !important;
      justify-content: center !important;
      min-height: 34px !important;
      padding: 0 13px !important;
      border: 0 !important;
      border-radius: 9px !important;
      background: transparent !important;
      box-shadow: none !important;
      color: var(--cs-muted) !important;
      font-size: 13px !important;
      font-weight: 600 !important;
      letter-spacing: -.005em !important;
      line-height: 1 !important;
      text-decoration: none !important;
      white-space: nowrap !important;
      transition: color .15s ease, background .15s ease, box-shadow .15s ease !important;
    }
    header .lean-nav-link:hover, header .lean-more > summary:hover, body > header > * > nav:not(.nav) > a:hover {
      background: var(--cs-hover) !important;
      color: var(--cs-text) !important;
    }
    header .lean-nav-link.active, header .lean-nav-link[aria-current="page"],
    body > header > * > nav:not(.nav) > a.active, body > header > * > nav:not(.nav) > a[aria-current="page"],
    header .lean-more[data-active="true"] > summary {
      background: var(--cs-accent-soft) !important;
      box-shadow: inset 0 0 0 1px var(--cs-accent-line) !important;
      color: var(--cs-text) !important;
    }
    header .lean-more-menu {
      border: 1px solid var(--cs-line-strong) !important;
      border-radius: 13px !important;
      background: var(--cs-panel) !important;
      box-shadow: var(--cs-shadow-pop) !important;
    }
    header .lean-more { overflow: visible; }

    header .header-tools, header .top-actions, header .header-actions, header .actions.header-tools {
      display: flex !important;
      align-items: center !important;
      justify-content: flex-end !important;
      gap: 8px !important;
      flex-wrap: nowrap !important;
      width: auto !important;
      margin: 0 !important;
    }
    header .control, header label.field, header .cs2-language-control, header label.locale, header .locale-control {
      display: inline-flex !important;
      align-items: center !important;
      gap: 0 !important;
      width: auto !important;
      min-height: 0 !important;
      margin: 0 !important;
    }
    header .control-label, header label.field > span, header .cs2-language-control > span,
    header label.locale > span, header .locale-control > span {
      position: absolute !important;
      width: 1px !important;
      height: 1px !important;
      margin: -1px !important;
      padding: 0 !important;
      overflow: hidden !important;
      clip: rect(0, 0, 0, 0) !important;
      white-space: nowrap !important;
      border: 0 !important;
    }
    header select {
      width: auto !important;
      min-width: 0 !important;
      max-width: 160px !important;
      height: 36px !important;
      min-height: 36px !important;
      margin: 0 !important;
      padding: 0 30px 0 12px !important;
      border: 1px solid var(--cs-line) !important;
      border-radius: 10px !important;
      background: var(--cs-panel) url("__CHEVRON__") no-repeat right 10px center / 12px !important;
      box-shadow: none !important;
      color: var(--cs-text) !important;
      font-size: 13px !important;
      font-weight: 600 !important;
      cursor: pointer;
      -webkit-appearance: none !important;
      appearance: none !important;
    }
    header select:hover { border-color: var(--cs-line-strong) !important; }
    header .icon-button, header .cs-theme-toggle {
      display: inline-grid !important;
      place-items: center !important;
      width: 36px !important;
      height: 36px !important;
      min-height: 36px !important;
      padding: 0 !important;
      border: 1px solid var(--cs-line) !important;
      border-radius: 10px !important;
      background: var(--cs-panel) !important;
      color: var(--cs-muted) !important;
      font-size: 16px !important;
      line-height: 1 !important;
      cursor: pointer;
    }
    header .cs-theme-toggle { box-shadow: none !important; }
    header .icon-button:hover, header .cs-theme-toggle:hover { border-color: var(--cs-line-strong) !important; color: var(--cs-text) !important; }
    header .button, header a.back {
      min-height: 36px !important;
      height: 36px;
      padding: 0 13px !important;
      border-radius: 10px !important;
      font-size: 13px !important;
    }
    header a.back { display: inline-flex; align-items: center; color: var(--cs-muted) !important; text-decoration: none; }
    header .userbox { display: inline-flex !important; align-items: center !important; gap: 8px !important; margin: 0 !important; }
    header .userbox .avatar { width: 32px !important; height: 32px !important; border-radius: 10px !important; }
    header .userbox .user-name { max-width: 140px; overflow: hidden; color: var(--cs-text); font-size: 13px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
    header .quiet-button { min-height: 36px !important; padding: 0 12px !important; border: 1px solid var(--cs-line) !important; border-radius: 10px !important; background: var(--cs-panel) !important; color: var(--cs-muted) !important; font-size: 13px !important; font-weight: 600 !important; cursor: pointer; }
    header .quiet-button:hover { border-color: var(--cs-line-strong) !important; color: var(--cs-text) !important; }
    header a.back:hover { color: var(--cs-text) !important; }

    /* ---------- layout ---------- */
    main.wrap, body > main > .wrap, .wrap:not(header .wrap) {
      width: min(1440px, calc(100% - 40px)) !important;
      max-width: none !important;
    }
    body main { padding-top: 24px !important; }

    /* ---------- hero ---------- */
    .dashboard-hero, .tool-hero, .strategy-hero, main > .hero, main > section.hero, .page-knives .hero {
      position: relative !important;
      isolation: isolate;
      overflow: hidden !important;
      min-height: 0 !important;
      margin-bottom: 18px !important;
      padding: 28px 30px !important;
      border: 1px solid var(--cs-line) !important;
      border-radius: 18px !important;
      background:
        radial-gradient(640px 260px at 0% 0%, color-mix(in srgb, var(--cs-accent) 15%, transparent), transparent 72%),
        linear-gradient(180deg, color-mix(in srgb, var(--cs-panel-2) 55%, var(--cs-panel)), var(--cs-panel)) !important;
      box-shadow: var(--cs-shadow) !important;
    }
    .dashboard-hero::before, .tool-hero::before, .strategy-hero::before, main > .hero::before, .page-knives .hero::before {
      position: absolute;
      inset: 0;
      z-index: -1;
      background-image: radial-gradient(color-mix(in srgb, var(--cs-text) 10%, transparent) 1px, transparent 1.2px);
      background-size: 18px 18px;
      content: "";
      opacity: .5;
      pointer-events: none;
      -webkit-mask-image: linear-gradient(100deg, transparent 45%, #000 100%);
      mask-image: linear-gradient(100deg, transparent 45%, #000 100%);
    }
    .dashboard-hero::after, .tool-hero::after { display: none !important; }
    .dashboard-hero h1, .tool-hero h1, .strategy-hero h1, main > .hero h1, .page-knives .hero h1 {
      max-width: 860px !important;
      margin: 8px 0 0 !important;
      color: var(--cs-text) !important;
      font-size: clamp(26px, 3vw, 38px) !important;
      font-weight: 760 !important;
      line-height: 1.12 !important;
      letter-spacing: -.03em !important;
      text-wrap: balance;
    }
    .dashboard-hero p, .tool-hero p, .strategy-hero p, main > .hero p, .page-knives .hero p {
      max-width: 760px !important;
      margin: 10px 0 0 !important;
      color: var(--cs-muted) !important;
      font-size: 15px !important;
      line-height: 1.6 !important;
    }
    .eyebrow {
      color: var(--cs-accent) !important;
      font-size: 11px !important;
      font-weight: 700 !important;
      letter-spacing: .12em !important;
      text-transform: uppercase !important;
    }
    .page-knives .hero { display: block !important; }
    .page-knives .hero-copy { padding: 0 !important; border: 0 !important; background: none !important; box-shadow: none !important; }
    .page-knives .hero-copy::before { display: none !important; }
    .page-knives .model-note {
      position: absolute !important;
      top: 22px;
      right: 22px;
      display: block !important;
      min-height: 0 !important;
      padding: 0 !important;
      border: 0 !important;
      background: none !important;
      box-shadow: none !important;
    }
    .page-knives .model-note .status-head { display: block !important; }
    .strategy-hero { display: grid !important; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 24px; }
    .strategy-hero .hero-status { display: grid !important; grid-template-columns: repeat(2, minmax(150px, 1fr)); gap: 8px !important; }
    .strategy-hero .status-chip {
      display: grid !important;
      gap: 3px;
      min-height: 0 !important;
      padding: 10px 12px !important;
      border: 1px solid var(--cs-line) !important;
      border-radius: 12px !important;
      background: color-mix(in srgb, var(--cs-sunken) 70%, transparent) !important;
    }
    .strategy-hero .status-chip span { color: var(--cs-muted) !important; font-size: 11px !important; }
    .strategy-hero .status-chip strong { color: var(--cs-text) !important; font-size: 13px !important; font-weight: 650 !important; }

    /* ---------- surfaces ---------- */
    .panel {
      border: 1px solid var(--cs-line) !important;
      border-radius: 16px !important;
      background: var(--cs-panel) !important;
      box-shadow: var(--cs-shadow) !important;
    }
    .section-head, .panel-head {
      padding: 16px 18px !important;
      border-bottom: 1px solid var(--cs-line) !important;
      background: transparent !important;
    }
    .section-head h2, .panel-head h2, .panel > h2 { font-size: 15px !important; font-weight: 700 !important; letter-spacing: -.01em !important; }
    .metric, .metric-card, .summary-card-tile, .source-card, .stat-card {
      border: 1px solid var(--cs-line) !important;
      border-radius: 16px !important;
      background: linear-gradient(180deg, color-mix(in srgb, var(--cs-panel-2) 70%, var(--cs-panel)), var(--cs-panel)) !important;
      box-shadow: var(--cs-shadow) !important;
    }
    .metric > span:first-child, .metric-card > span:first-child, .metric .label { color: var(--cs-muted) !important; font-size: 12px !important; font-weight: 600 !important; }
    .metric strong, .metric .value, .metric-card strong {
      color: var(--cs-text) !important;
      font-weight: 750 !important;
      letter-spacing: -.025em !important;
    }
    .metric small, .metric-card small { color: var(--cs-muted) !important; font-size: 12px !important; }
    .disclosure, details.disclosure {
      border: 1px solid var(--cs-line) !important;
      border-radius: 12px !important;
      background: color-mix(in srgb, var(--cs-sunken) 60%, transparent) !important;
    }
    .disclosure > summary:hover { background: var(--cs-hover) !important; }

    /* ---------- controls ---------- */
    .button, a.button, button.button, .apply, button.primary, .lab-tab, .tab, .workspace-tab, .filter-chip {
      font-family: inherit !important;
    }
    .button, .apply {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      min-height: 38px !important;
      padding: 0 15px !important;
      border: 1px solid var(--cs-line-strong) !important;
      border-radius: 10px !important;
      background: var(--cs-panel-2) !important;
      box-shadow: none !important;
      color: var(--cs-text) !important;
      font-size: 13px !important;
      font-weight: 650 !important;
      text-decoration: none !important;
      cursor: pointer;
      transform: none !important;
      transition: background .15s ease, border-color .15s ease, color .15s ease, filter .15s ease !important;
    }
    .button:hover {
      border-color: color-mix(in srgb, var(--cs-accent) 55%, var(--cs-line-strong)) !important;
      background: color-mix(in srgb, var(--cs-accent) 9%, var(--cs-panel-2)) !important;
    }
    .button.button-primary, .button-primary, .button.primary, button.primary, .apply {
      border-color: transparent !important;
      background: linear-gradient(180deg, color-mix(in srgb, var(--cs-accent) 88%, #fff 12%), var(--cs-accent-strong)) !important;
      box-shadow: 0 10px 24px -12px color-mix(in srgb, var(--cs-accent) 85%, transparent), inset 0 1px 0 rgba(255, 255, 255, .2) !important;
      color: #fff !important;
    }
    .button.button-primary:hover, .button-primary:hover, .button.primary:hover, button.primary:hover, .apply:hover {
      border-color: transparent !important;
      background: linear-gradient(180deg, color-mix(in srgb, var(--cs-accent) 80%, #fff 20%), var(--cs-accent)) !important;
    }
    .button:disabled, button:disabled { opacity: .45 !important; cursor: not-allowed !important; filter: saturate(.6); }
    .button-quiet { color: var(--cs-muted) !important; }

    input:not([type="checkbox"]):not([type="radio"]):not([type="range"]), select, textarea {
      min-height: 38px;
      border: 1px solid var(--cs-line) !important;
      border-radius: 10px !important;
      background-color: var(--cs-sunken) !important;
      color: var(--cs-text) !important;
      font-family: inherit !important;
      transition: border-color .15s ease, box-shadow .15s ease, background-color .15s ease !important;
    }
    input::placeholder, textarea::placeholder { color: var(--cs-faint) !important; opacity: 1; }
    input:not([type="checkbox"]):not([type="radio"]):hover, select:hover, textarea:hover { border-color: var(--cs-line-strong) !important; }
    input:not([type="checkbox"]):not([type="radio"]):focus, select:focus, textarea:focus {
      border-color: var(--cs-accent) !important;
      box-shadow: 0 0 0 3px var(--cs-accent-soft) !important;
      outline: none !important;
    }
    main select:not([multiple]):not([size]) {
      padding-right: 32px !important;
      background-image: url("__CHEVRON__") !important;
      background-position: right 11px center !important;
      background-repeat: no-repeat !important;
      background-size: 12px !important;
      -webkit-appearance: none !important;
      appearance: none !important;
      cursor: pointer;
    }
    input[type="checkbox"], input[type="radio"] { accent-color: var(--cs-accent); }
    .field, .filter-field { min-width: 0 !important; }
    .field > input, .field > select, .filter-field > input, .filter-field > select { width: 100% !important; min-width: 0 !important; max-width: none !important; }
    .field > span, .filter-field > span { color: var(--cs-muted) !important; font-size: 11px !important; font-weight: 600 !important; letter-spacing: .01em; }

    .pill, .status-pill, .tag, .truth-chip {
      border-color: var(--cs-line-strong) !important;
      background: color-mix(in srgb, var(--cs-sunken) 70%, transparent) !important;
      color: var(--cs-muted);
    }
    .tag { border-radius: 7px !important; }

    .filter-chip, .tab, .lab-tab, .workspace-tab {
      border: 1px solid var(--cs-line) !important;
      background: var(--cs-panel) !important;
      color: var(--cs-muted) !important;
      transition: color .15s ease, background .15s ease, border-color .15s ease !important;
    }
    .filter-chip:hover, .tab:hover, .lab-tab:hover, .workspace-tab:hover { border-color: var(--cs-line-strong) !important; color: var(--cs-text) !important; }
    .filter-chip.active, .filter-chip[aria-pressed="true"], .tab.active, .lab-tab.active, .lab-tab[aria-selected="true"], .workspace-tab[aria-selected="true"] {
      border-color: var(--cs-accent-line) !important;
      background: var(--cs-accent-soft) !important;
      box-shadow: none !important;
      color: var(--cs-text) !important;
    }
    .lab-tabs, .workspace-tabs {
      padding: 5px !important;
      border: 1px solid var(--cs-line) !important;
      border-radius: 14px !important;
      background: color-mix(in srgb, var(--cs-panel) 80%, transparent) !important;
      box-shadow: none !important;
    }
    .lab-tabs .lab-tab, .workspace-tabs .workspace-tab { border-color: transparent !important; background: transparent !important; border-radius: 10px !important; }
    .lab-tabs .lab-tab.active, .lab-tabs .lab-tab[aria-selected="true"], .workspace-tabs .workspace-tab[aria-selected="true"] {
      border-color: var(--cs-accent-line) !important;
      background: var(--cs-accent-soft) !important;
    }

    /* ---------- tables ---------- */
    table { border-color: var(--cs-line); }
    th {
      background: var(--cs-panel-2) !important;
      color: var(--cs-muted) !important;
      font-size: 11px !important;
      font-weight: 650 !important;
      letter-spacing: .06em !important;
      text-transform: uppercase;
    }
    th, td { border-bottom-color: var(--cs-line) !important; }
    tbody tr:hover { background: var(--cs-hover) !important; }

    /* ---------- tones ---------- */
    .good, .money-good, .up, .positive { color: var(--cs-good); }
    .bad, .money-bad, .down, .negative { color: var(--cs-bad); }
    .watch, .thin, .warn { color: var(--cs-warn); }

    /* ---------- deals ---------- */
    .page-deals .scroll { border-top: 1px solid var(--cs-line); }
    .page-deals td { padding: 14px 12px !important; }
    .page-deals .preview { gap: 14px !important; }
    .page-deals .item-img { border-color: var(--cs-line) !important; border-radius: 12px !important; background: radial-gradient(circle at 50% 40%, color-mix(in srgb, var(--cs-accent) 10%, var(--cs-image)), var(--cs-image) 70%) !important; }
    .page-deals .skin-name { font-size: 14px; font-weight: 700 !important; line-height: 1.3; }
    .page-deals .sticker-list { gap: 6px !important; margin-top: 10px !important; }
    .page-deals .sticker-pill {
      display: flex !important;
      align-items: center !important;
      gap: 9px !important;
      max-width: 100% !important;
      padding: 5px 10px 5px 6px !important;
      border-color: var(--cs-line) !important;
      border-radius: 10px !important;
      background: var(--cs-sunken) !important;
    }
    .page-deals .sticker-copy { display: grid; min-width: 0; gap: 1px; }
    .page-deals .sticker-name { overflow: hidden; color: var(--cs-text); font-size: 12px; font-weight: 600; line-height: 1.3; text-overflow: ellipsis; white-space: nowrap; }
    .page-deals .sticker-meta { color: var(--cs-muted); font-size: 11px; white-space: nowrap; }
    .page-deals .model-box {
      gap: 4px !important;
      padding: 10px 12px !important;
      border: 1px solid var(--cs-line) !important;
      border-left-width: 3px !important;
      border-radius: 11px !important;
      background: color-mix(in srgb, var(--cs-sunken) 70%, transparent) !important;
      color: var(--cs-muted);
      font-size: 12px !important;
      line-height: 1.45;
    }
    .page-deals .model-box[data-verdict="good"] { border-left-color: var(--cs-good) !important; }
    .page-deals .model-box[data-verdict="watch"] { border-left-color: var(--cs-warn) !important; }
    .page-deals .model-box[data-verdict="bad"] { border-left-color: var(--cs-bad) !important; }
    .page-deals .model-box[data-model="vega"] { border-left-color: var(--cs-accent) !important; }
    .page-deals .model-title { color: var(--cs-text) !important; font-size: 13px !important; }
    .page-deals .model-title strong { font-size: 13px; }
    .page-deals .verdict { min-height: 22px !important; padding: 0 9px !important; border-color: var(--cs-line-strong) !important; background: transparent !important; font-size: 11px !important; font-weight: 650 !important; }
    .page-deals .detail-stack { gap: 0 !important; }
    .page-deals .detail-line {
      align-items: baseline;
      gap: 10px !important;
      padding: 6px 0 !important;
      border-bottom: 1px dashed color-mix(in srgb, var(--cs-line) 80%, transparent) !important;
      font-size: 12px;
    }
    .page-deals .detail-line:last-child { border-bottom: 0 !important; }
    .page-deals .detail-line > span:last-child { min-width: 0; color: var(--cs-text); font-weight: 600; text-align: right; overflow-wrap: anywhere; }
    .page-deals .detail-label { color: var(--cs-muted) !important; font-weight: 500; white-space: nowrap; }
    .page-deals .category {
      display: inline-block !important;
      max-width: 100%;
      min-height: 0 !important;
      padding: 3px 9px !important;
      border-color: var(--cs-line-strong) !important;
      border-radius: 8px !important;
      background: var(--cs-sunken) !important;
      font-size: 11px !important;
      font-weight: 650 !important;
      line-height: 1.35 !important;
      text-align: left;
      white-space: normal !important;
    }
    .page-deals .category + .category { margin-top: 4px; }
    .page-deals .confidence-safe { color: var(--cs-good) !important; border-color: color-mix(in srgb, var(--cs-good) 45%, var(--cs-line)) !important; }
    .page-deals .confidence-warn { color: var(--cs-warn) !important; border-color: color-mix(in srgb, var(--cs-warn) 45%, var(--cs-line)) !important; }
    .page-deals .confidence-bad { color: var(--cs-bad) !important; border-color: color-mix(in srgb, var(--cs-bad) 45%, var(--cs-line)) !important; }
    .page-deals .time-stack .detail-line { flex-direction: column; align-items: flex-start; gap: 1px !important; }
    .page-deals .time-stack .detail-line > span:last-child { text-align: left; font-weight: 600; }
    .page-deals .link-stack { display: flex !important; flex-wrap: wrap; gap: 6px !important; margin-top: 10px; }
    .page-deals .link-stack .link {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      margin: 0 !important;
      padding: 0 10px;
      border: 1px solid var(--cs-line-strong);
      border-radius: 8px;
      background: var(--cs-panel-2);
      color: var(--cs-text) !important;
      font-size: 12px;
      font-weight: 650;
      text-decoration: none !important;
    }
    .page-deals .link-stack .link:hover { border-color: var(--cs-accent-line); color: var(--cs-accent) !important; }
    .page-deals .link-stack > .meta { display: none; }
    .page-deals .num-cell { font-size: 15px; font-weight: 750; color: var(--cs-text); }
    .page-deals .summary-card strong { color: var(--cs-text); }
    .page-deals .risk-note { background: color-mix(in srgb, var(--cs-sunken) 70%, transparent) !important; }
    .page-deals .deal-card { background: transparent !important; }
    .page-deals .deal-mini { border-color: var(--cs-line) !important; border-radius: 10px !important; background: var(--cs-sunken) !important; }

    .page-deal .sticker-editor { margin: 0 18px 18px !important; }
    .page-deal .result-details { margin: 14px 0 0 !important; }
    .page-deal .model-comparison-grid { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)) !important; }
    .page-deal .model-prediction-card { border-color: var(--cs-line) !important; border-radius: 12px !important; background: color-mix(in srgb, var(--cs-sunken) 70%, var(--cs-panel)) !important; }
    .page-deal .model-prediction-card.selected { border-color: var(--cs-accent-line) !important; box-shadow: inset 0 0 0 1px var(--cs-accent-line) !important; }
    .page-deal .comparison-callout { border-radius: 14px !important; }
    .page-market-history .filters { grid-template-columns: minmax(200px, 2fr) repeat(4, minmax(150px, 1fr)) auto !important; align-items: end; }

    /* ---------- catalogue & knives cards ---------- */
    .page-catalog .card, .page-knives .knife-card, .page-knives .opportunity-card {
      border: 1px solid var(--cs-line) !important;
      border-radius: 16px !important;
      background: linear-gradient(180deg, color-mix(in srgb, var(--cs-panel-2) 55%, var(--cs-panel)), var(--cs-panel)) !important;
      box-shadow: var(--cs-shadow) !important;
      transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease !important;
    }
    .page-catalog .card:hover, .page-knives .knife-card:hover {
      border-color: var(--cs-accent-line) !important;
      box-shadow: var(--cs-shadow-pop) !important;
      transform: translateY(-2px) !important;
    }
    .page-catalog .card .art, .page-knives .knife-image, .page-knives .knife-placeholder {
      border: 1px solid var(--cs-line) !important;
      border-radius: 12px !important;
      background: radial-gradient(circle at 50% 42%, color-mix(in srgb, var(--cs-accent) 13%, var(--cs-image)), var(--cs-image) 72%) !important;
    }
    .page-catalog .card .price, .page-knives .card-price { color: var(--cs-text) !important; letter-spacing: -.02em; }
    .page-catalog .card .price.stale { color: var(--cs-muted) !important; font-size: .82em; }
    .page-catalog .card .price-asof { display: block; margin-top: 2px; color: var(--cs-faint); font-size: 11px; font-weight: 500; letter-spacing: 0; }
    .page-knives .card-stat { border: 1px solid var(--cs-line) !important; border-radius: 9px !important; background: var(--cs-sunken) !important; }
    .page-catalog .tabs { gap: 6px !important; }

    /* ---------- market history ---------- */
    .page-market-history .truth {
      border-color: color-mix(in srgb, var(--cs-warn) 40%, var(--cs-line)) !important;
      background: color-mix(in srgb, var(--cs-warn) 8%, transparent) !important;
      color: color-mix(in srgb, var(--cs-warn) 70%, var(--cs-text)) !important;
    }
    .page-market-history .truth strong { color: var(--cs-text) !important; }

    /* ---------- system dashboard ---------- */
    .page-dashboard .model-health-grid { align-items: start !important; }
    .page-dashboard .model-health-card { border-color: var(--cs-line) !important; background: color-mix(in srgb, var(--cs-sunken) 55%, var(--cs-panel)) !important; }
    .health-banner { border-radius: 14px !important; }
    .scanner-target-scroll th { letter-spacing: .02em !important; }

    /* ---------- dialogs & floating ---------- */
    dialog, .image-modal-inner { border-color: var(--cs-line-strong) !important; background: var(--cs-panel) !important; box-shadow: var(--cs-shadow-pop) !important; }
    .back-to-top { border-color: var(--cs-line-strong) !important; background: color-mix(in srgb, var(--cs-panel) 92%, transparent) !important; color: var(--cs-text) !important; }

    /* ---------- public landing ---------- */
    body > header > * > nav.nav {
      grid-column: 3 !important;
      justify-self: end !important;
      display: flex !important;
      align-items: center !important;
      gap: 8px !important;
      padding: 0 !important;
      border: 0 !important;
      background: none !important;
    }
    .page-public main > .hero { padding: 0 !important; border: 0 !important; background: none !important; box-shadow: none !important; overflow: visible !important; }
    .page-public main > .hero::before { display: none !important; }
    .page-public .hero .intro { padding: 30px !important; background: radial-gradient(640px 260px at 0% 0%, color-mix(in srgb, var(--cs-accent) 15%, transparent), transparent 72%), var(--cs-panel) !important; }
    .page-public .hero .intro h1 { font-size: clamp(32px, 4.4vw, 54px) !important; letter-spacing: -.035em !important; }
    .page-public .visual-grid { align-content: center; gap: 10px !important; padding: 12px !important; }
    .page-public .skin-tile {
      display: grid;
      grid-template-rows: minmax(0, 1fr) auto;
      min-height: 150px !important;
      border-color: var(--cs-line) !important;
      border-radius: 14px !important;
      background: var(--cs-sunken) !important;
    }
    .page-public .skin-tile img { height: 150px !important; padding: 12px; background: radial-gradient(circle at 50% 45%, color-mix(in srgb, var(--cs-accent) 12%, var(--cs-image)), var(--cs-image) 70%) !important; }
    .page-public .skin-tile .tile-title { padding: 9px 12px !important; border-top: 1px solid var(--cs-line); color: var(--cs-muted) !important; }
    .page-public .metric .metric-value { color: var(--cs-text); letter-spacing: -.025em; }
    .page-public .row .value { color: var(--cs-accent) !important; }

    /* ---------- sign-in pages ---------- */
    .page-auth main.shell { width: min(1180px, calc(100% - 40px)) !important; }
    .page-auth h1 { font-size: clamp(34px, 4.8vw, 58px) !important; line-height: 1.02 !important; letter-spacing: -.045em !important; }
    .page-auth .card { border: 1px solid var(--cs-line-strong) !important; border-radius: 20px !important; background: color-mix(in srgb, var(--cs-panel) 94%, transparent) !important; box-shadow: var(--cs-shadow-pop) !important; }
    .page-auth form button[type="submit"] {
      border: 0 !important;
      background: linear-gradient(180deg, color-mix(in srgb, var(--cs-accent) 88%, #fff 12%), var(--cs-accent-strong)) !important;
      box-shadow: 0 12px 26px -12px color-mix(in srgb, var(--cs-accent) 85%, transparent), inset 0 1px 0 rgba(255, 255, 255, .2) !important;
      color: #fff !important;
      font-weight: 700 !important;
    }
    .page-auth .check { border-color: var(--cs-accent-line) !important; background: var(--cs-accent-soft) !important; color: var(--cs-accent) !important; }
    .page-auth a:not(.back):not(.brand) { color: var(--cs-accent); }

    /* ---------- responsive ---------- */
    @media (max-width: 1100px) {
      body > header > .topbar, body > header > .top, body > header > .wrap, body > header > .shell {
        grid-template-columns: minmax(0, 1fr) auto !important;
        gap: 10px 14px !important;
        padding: 10px 0 !important;
      }
      body > header > * > nav:not(.nav) { grid-column: 1 / -1 !important; grid-row: 2; justify-self: stretch !important; justify-content: flex-start !important; width: 100% !important; }
      body > header > * > :last-child:not(nav):not(:first-child) { grid-column: 2; grid-row: 1; }
      header .lean-more { display: none !important; }
      .strategy-hero { grid-template-columns: 1fr; }
    }
    @media (max-width: 720px) {
      body > header > .topbar, body > header > .top, body > header > .wrap, body > header > .shell,
      main.wrap, body > main > .wrap, .wrap:not(header .wrap) { width: calc(100% - 24px) !important; }
      header .site-brand small, header .brand small { display: none !important; }
      header .lean-nav-link, body > header > * > nav:not(.nav) > a { padding: 0 11px !important; font-size: 12.5px !important; }
      header select { max-width: 112px !important; }
      .dashboard-hero, .tool-hero, .strategy-hero, main > .hero, .page-knives .hero { padding: 20px 18px !important; border-radius: 16px !important; }
      .dashboard-hero h1, .tool-hero h1, .strategy-hero h1, main > .hero h1, .page-knives .hero h1 { font-size: clamp(23px, 7vw, 30px) !important; }
      .dashboard-hero p, .tool-hero p, .strategy-hero p, main > .hero p, .page-knives .hero p { font-size: 14px !important; }
      .page-knives .model-note { position: static !important; margin-top: 14px; }
      .tool-hero > .button, .dashboard-hero .hero-actions .button { flex: 0 0 auto !important; width: 100%; min-height: 42px !important; }
      .page-deals .deal-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
      .page-deals .summary-card { border-right: 1px solid var(--cs-line) !important; border-bottom: 1px solid var(--cs-line) !important; }
      .page-deals .summary-card:nth-child(2n) { border-right: 0 !important; }
      .strategy-hero .hero-status { grid-template-columns: 1fr 1fr; }
      .panel { border-radius: 14px !important; }
    }
    @media (prefers-reduced-motion: reduce) {
      .page-catalog .card:hover, .page-knives .knife-card:hover { transform: none !important; }
    }
  </style>
"""
    .replace("__FAVICON__", _FAVICON_DATA_URI)
    .replace("__CHEVRON__", _CHEVRON_DATA_URI)
)

UNIFIED_THEME_SCRIPT = r"""
  <script id="cs2-unified-theme-script">
    (() => {
      const root = document.documentElement;
      const body = document.body;
      const themes = ['dark', 'light', 'midnight', 'graphite'];
      const valid = new Set(themes);

      function setTheme(value, persist) {
        const theme = valid.has(value) ? value : 'dark';
        root.setAttribute('data-cs-theme', theme);
        body.classList.remove('theme-light', 'theme-midnight', 'theme-graphite');
        if (theme !== 'dark') body.classList.add(`theme-${theme}`);
        body.classList.toggle('light', theme === 'light');
        const select = document.getElementById('themeSelect');
        if (select && select.value !== theme && Array.from(select.options).some(option => option.value === theme)) {
          select.value = theme;
        }
        const toggle = document.querySelector('.cs-theme-toggle');
        if (toggle) toggle.textContent = theme === 'light' ? '☾' : '☀';
        if (persist) {
          try {
            localStorage.setItem('siteTheme', theme);
            localStorage.setItem('bestDealsTheme', theme);
            localStorage.setItem('cs2_theme', theme === 'light' ? 'light' : 'dark');
          } catch (error) {}
        }
      }

      const header = document.querySelector('body > header');
      // Pages place a varying number of controls after the navigation
      // (language, user box, buttons).  Group them so the header always stays
      // a three-cell row: brand | navigation | tools.
      const bar = header?.firstElementChild;
      const nav = bar ? Array.from(bar.children).find(node => node.tagName === 'NAV' && !node.classList.contains('nav')) : null;
      if (nav) {
        const trailing = [];
        for (let node = nav.nextElementSibling; node; node = node.nextElementSibling) trailing.push(node);
        const alreadyGrouped = trailing.length === 1 && trailing[0].matches('.header-tools, .top-actions, .header-actions');
        if (trailing.length && !alreadyGrouped) {
          const group = document.createElement('div');
          group.className = 'header-tools cs-header-tools';
          nav.after(group);
          trailing.forEach(node => group.appendChild(node));
        }
      }
      const select = document.getElementById('themeSelect');
      const knivesButton = document.getElementById('themeButton');
      if (select) {
        select.addEventListener('change', () => setTheme(select.value, true));
      } else if (knivesButton) {
        knivesButton.addEventListener('click', () => {
          setTheme(body.classList.contains('light') ? 'light' : 'dark', true);
        });
      } else if (header) {
        const tools = header.querySelector('.header-tools, .top-actions, .header-actions');
        const toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'cs-theme-toggle';
        toggle.setAttribute('aria-label', document.documentElement.lang === 'en' ? 'Toggle theme' : 'Переключить тему');
        toggle.addEventListener('click', () => {
          setTheme(root.getAttribute('data-cs-theme') === 'light' ? 'dark' : 'light', true);
        });
        if (tools) tools.appendChild(toggle);
        else if (header.firstElementChild) {
          // Keep the header at three grid cells: wrap a trailing language
          // control together with the new toggle instead of adding a fourth.
          const bar = header.firstElementChild;
          const last = bar.lastElementChild;
          const group = document.createElement('div');
          group.className = 'header-tools';
          if (last && last.tagName !== 'NAV' && last !== bar.firstElementChild) {
            last.replaceWith(group);
            group.append(last, toggle);
          } else {
            group.appendChild(toggle);
            bar.appendChild(group);
          }
        }
      }
      setTheme(root.getAttribute('data-cs-theme'), false);

      if (header) {
        const onScroll = () => header.classList.toggle('cs-scrolled', window.scrollY > 4);
        window.addEventListener('scroll', onScroll, { passive: true });
        onScroll();
      }
    })();
  </script>
"""


_PLAIN_BRAND_RE = re.compile(
    r'<(?P<tag>a|div) class="brand"(?P<attrs>(?: href="[^"]*")?)>\s*(?:◆\s*)?'
    r"(?P<label>[^<]{1,40}?)\s*</(?P=tag)>"
)
_BODY_TAG_RE = re.compile(r"<body(?P<attrs>[^>]*)>", re.IGNORECASE)


def _add_body_class(document: str, class_name: str) -> str:
    def replace(match: re.Match[str]) -> str:
        attrs = match.group("attrs")
        class_match = re.search(r"""class=(["'])(.*?)\1""", attrs)
        if class_match is None:
            return f'<body{attrs} class="{class_name}">'
        if class_name in class_match.group(2).split():
            return match.group(0)
        quote = class_match.group(1)
        updated = f"class={quote}{class_match.group(2)} {class_name}{quote}"
        return f"<body{attrs[:class_match.start()]}{updated}{attrs[class_match.end():]}>"

    return _BODY_TAG_RE.sub(replace, document, count=1)


def inject_theme(document: str, *, page: str | None = None) -> str:
    """Append the unified theme once, after every page-specific stylesheet."""

    rendered = str(document)
    if 'id="cs2-unified-theme"' in rendered:
        return rendered
    if page:
        rendered = _add_body_class(rendered, f"page-{html.escape(page)}")
    # Text-only brands get the same logo mark as the rest of the site.
    rendered = _PLAIN_BRAND_RE.sub(
        lambda match: (
            f'<{match.group("tag")} class="brand cs-brand-plain"{match.group("attrs")}>'
            f'{match.group("label")}</{match.group("tag")}>'
        ),
        rendered,
        count=1,
    )
    head_close = rendered.find("</head>")
    if head_close >= 0:
        rendered = (
            rendered[:head_close]
            + THEME_BOOT_SCRIPT
            + UNIFIED_THEME_CSS
            + rendered[head_close:]
        )
    body_close = rendered.rfind("</body>")
    if body_close >= 0:
        rendered = rendered[:body_close] + UNIFIED_THEME_SCRIPT + rendered[body_close:]
    return rendered
