"""Small server-first RU/EN localisation layer for the embedded web UI.

The project intentionally has no web framework or template engine: pages are
large HTML strings in :mod:`dashboard_server`.  This module keeps locale
selection, the shared browser bootstrap and the translation catalogue out of
those templates.  The server selects a locale before sending HTML; the browser
then translates both the initial DOM and nodes produced later by page scripts.
"""

from __future__ import annotations

import html
import json
import re
from http.cookies import CookieError, SimpleCookie
from typing import Any

try:
    from web_i18n_catalog import TRANSLATION_PAIRS
except ImportError:  # Lets this module remain usable while a catalogue is built.
    TRANSLATION_PAIRS: tuple[tuple[str, str], ...] = ()

try:
    from web_i18n_catalog import PATTERN_TRANSLATIONS
except ImportError:
    PATTERN_TRANSLATIONS: tuple[tuple[str, str], ...] = ()


SUPPORTED_LOCALES = ("ru", "en")
DEFAULT_LOCALE = "ru"
LOCALE_COOKIE = "cs2_locale"
LOCALE_MAX_AGE = 365 * 24 * 60 * 60
INTL_LOCALES = {"ru": "ru-RU", "en": "en-US"}

# The language control and navigation must work even if an optional extended
# catalogue is temporarily unavailable.
CORE_TRANSLATION_PAIRS: tuple[tuple[str, str], ...] = (
    ("Language", "Язык"),
    ("Russian", "Русский"),
    ("English", "English"),
    ("Overview", "Обзор"),
    ("Deals", "Сделки"),
    ("Strategy", "Стратегия"),
    ("Comparison", "Сравнение"),
    ("Inventory", "Инвентарь"),
    ("Market history", "История рынков"),
    ("Theme", "Тема"),
    ("Dark", "Тёмная"),
    ("Light", "Светлая"),
    ("Midnight", "Полночь"),
    ("Graphite", "Графит"),
    ("Back to top", "Наверх"),
    ("Login", "Войти"),
    ("Register", "Регистрация"),
    ("Loading", "Загрузка"),
    ("loading", "загрузка"),
    ("Unknown", "Неизвестно"),
    ("unknown", "неизвестно"),
    ("Previous", "Назад"),
    ("Next", "Далее"),
    ("Search", "Поиск"),
    ("Apply", "Применить"),
    ("Close", "Закрыть"),
    ("Waiting for data", "Ожидание данных"),
    ("waiting for data", "ожидание данных"),
    ("listings", "листингов"),
    ("skins", "скинов"),
    ("stickers", "стикеров"),
    ("sales", "продаж"),
)


def _supported_locale(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace("_", "-")
    if not text:
        return None
    primary = text.split("-", 1)[0]
    return primary if primary in SUPPORTED_LOCALES else None


def normalize_locale(value: Any, default: str = DEFAULT_LOCALE) -> str:
    """Return ``ru`` or ``en``; malformed and unsupported values use default."""

    supported = _supported_locale(value)
    if supported is not None:
        return supported
    return _supported_locale(default) or DEFAULT_LOCALE


def parse_accept_language(value: Any) -> str | None:
    """Select the best supported locale from a standard Accept-Language value."""

    ranked: list[tuple[float, int, str]] = []
    for index, part in enumerate(str(value or "").split(",")):
        bits = [piece.strip() for piece in part.split(";")]
        locale = _supported_locale(bits[0])
        if locale is None:
            continue
        quality = 1.0
        for parameter in bits[1:]:
            if parameter.lower().startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        if quality > 0:
            ranked.append((min(quality, 1.0), -index, locale))
    return max(ranked)[2] if ranked else None


def request_locale(handler: Any) -> str:
    """Resolve locale from a validated cookie, then Accept-Language, then RU."""

    headers = getattr(handler, "headers", {})
    raw_cookie = headers.get("Cookie", "") if headers is not None else ""
    if raw_cookie:
        try:
            cookie = SimpleCookie()
            cookie.load(raw_cookie)
            morsel = cookie.get(LOCALE_COOKIE)
            locale = _supported_locale(morsel.value if morsel is not None else None)
            if locale is not None:
                return locale
        except (CookieError, KeyError, ValueError):
            pass
    accepted = parse_accept_language(
        headers.get("Accept-Language", "") if headers is not None else ""
    )
    return accepted or DEFAULT_LOCALE


def locale_cookie_header(locale: Any, *, secure: bool = False) -> str:
    """Build the non-sensitive preference cookie used by the language switch."""

    value = normalize_locale(locale)
    suffix = "; Secure" if secure else ""
    return (
        f"{LOCALE_COOKIE}={value}; Path=/; Max-Age={LOCALE_MAX_AGE}; "
        f"SameSite=Lax{suffix}"
    )


def _pairs() -> tuple[tuple[str, str], ...]:
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for english, russian in (*CORE_TRANSLATION_PAIRS, *TRANSLATION_PAIRS):
        pair = (str(english).strip(), str(russian).strip())
        if not pair[0] or not pair[1] or pair in seen:
            continue
        seen.add(pair)
        result.append(pair)
    return tuple(result)


def translations_for(locale: Any) -> dict[str, str]:
    """Return exact-text aliases in both source languages for ``locale``."""

    selected = normalize_locale(locale)
    translations: dict[str, str] = {}
    for english, russian in _pairs():
        target = russian if selected == "ru" else english
        translations[english] = target
        translations[russian] = target
    return translations


def translate(text: Any, locale: Any) -> str:
    """Translate one exact catalogue phrase, returning unknown text unchanged."""

    value = str(text if text is not None else "")
    return translations_for(locale).get(value, value)


def pattern_translations_for(locale: Any) -> tuple[tuple[str, str], ...]:
    """Return dynamic source/target templates in the selected direction."""

    selected = normalize_locale(locale)
    if selected == "ru":
        directed = tuple((english, russian) for english, russian in PATTERN_TRANSLATIONS)
    else:
        directed = tuple((russian, english) for english, russian in PATTERN_TRANSLATIONS)

    # A placeholder is intentionally allowed to contain punctuation and words.
    # Evaluate the most literal template first so ``updated {time}`` cannot
    # swallow ``updated {time} | stats refreshing``.
    def specificity(pair: tuple[str, str]) -> tuple[int, int, int]:
        source = pair[0]
        placeholder_count = len(_PLACEHOLDER_RE.findall(source))
        literal_length = len(_PLACEHOLDER_RE.sub("", source))
        return literal_length, len(source), -placeholder_count

    return tuple(sorted(directed, key=specificity, reverse=True))


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def translate_dynamic(text: Any, locale: Any, *, _depth: int = 0) -> str:
    """Translate exact copy or a catalogued ``{placeholder}`` text pattern."""

    value = str(text if text is not None else "")
    direct = translate(value, locale)
    if direct != value:
        return direct
    for source, target in pattern_translations_for(locale):
        cursor = 0
        names: list[str] = []
        expression: list[str] = []
        for placeholder in _PLACEHOLDER_RE.finditer(source):
            expression.append(re.escape(source[cursor : placeholder.start()]))
            expression.append("(.+?)")
            names.append(placeholder.group(1))
            cursor = placeholder.end()
        expression.append(re.escape(source[cursor:]))
        matched = re.fullmatch("".join(expression), value, flags=re.IGNORECASE)
        if matched is None:
            continue
        values: dict[str, str] = {}
        for index, name in enumerate(names, start=1):
            captured = matched.group(index)
            if _depth < 2:
                captured = translate_dynamic(captured, locale, _depth=_depth + 1)
            values.setdefault(name, captured)
        return _PLACEHOLDER_RE.sub(
            lambda match, replacements=values: replacements.get(match.group(1), ""),
            target,
        )
    return value


def catalog(locale: Any) -> dict[str, Any]:
    """Return the serialisable browser catalogue (also reusable by account UI)."""

    selected = normalize_locale(locale)
    return {
        "locale": selected,
        "intl_locale": INTL_LOCALES[selected],
        "translations": translations_for(selected),
        "patterns": pattern_translations_for(selected),
        "cookie_name": LOCALE_COOKIE,
        "cookie_max_age": LOCALE_MAX_AGE,
    }


def catalog_json(locale: Any) -> str:
    """Encode a catalogue safely for an inline script element."""

    raw = json.dumps(catalog(locale), ensure_ascii=False, separators=(",", ":"))
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


_LANG_RE = re.compile(r"(<html\b[^>]*\blang=)([\"'])[^\"']*\2", re.IGNORECASE)
_PROTECTED_BLOCK_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_VISIBLE_TEXT_RE = re.compile(r"(?s)(>)([^<>]+)(<)")
_TRANSLATABLE_ATTRIBUTE_RE = re.compile(
    r"(?is)(\b(?:placeholder|title|aria-label)\s*=\s*)([\"'])(.*?)\2"
)


def _translate_exact_visible(value: str, translations: dict[str, str]) -> str:
    """Translate a text/attribute value while preserving surrounding spacing."""

    match = re.fullmatch(r"(\s*)(.*?)(\s*)", value, flags=re.DOTALL)
    if match is None or not match.group(2):
        return value
    leading, source, trailing = match.groups()
    decoded = html.unescape(source)
    canonical = " ".join(decoded.split())
    target = translations.get(decoded) or translations.get(canonical)
    if not target or target == decoded:
        return value
    return f"{leading}{html.escape(target, quote=False)}{trailing}"


def _translate_markup_segment(segment: str, translations: dict[str, str]) -> str:
    def replace_attribute(match: re.Match[str]) -> str:
        prefix, quote, value = match.groups()
        translated = _translate_exact_visible(value, translations)
        if translated == value:
            return match.group(0)
        # Attribute values need quote escaping in addition to HTML text escaping.
        decoded = html.unescape(translated)
        escaped = html.escape(decoded, quote=True)
        if quote == "'":
            escaped = escaped.replace("&#x27;", "&#39;")
        return f"{prefix}{quote}{escaped}{quote}"

    segment = _TRANSLATABLE_ATTRIBUTE_RE.sub(replace_attribute, segment)
    return _VISIBLE_TEXT_RE.sub(
        lambda match: (
            f"{match.group(1)}"
            f"{_translate_exact_visible(match.group(2), translations)}"
            f"{match.group(3)}"
        ),
        segment,
    )


def translate_static_document(document: str, locale: Any) -> str:
    """Translate server-visible markup without touching JavaScript or CSS."""

    translations = translations_for(locale)
    parts: list[str] = []
    cursor = 0
    for match in _PROTECTED_BLOCK_RE.finditer(document):
        parts.append(_translate_markup_segment(document[cursor : match.start()], translations))
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(_translate_markup_segment(document[cursor:], translations))
    return "".join(parts)


def _bootstrap(locale: str) -> str:
    payload = catalog_json(locale)
    # The observer is deliberately exact-text based: it cannot rewrite IDs,
    # form values, URLs or user data, and it also covers nodes rendered later
    # through innerHTML/textContent by the existing page scripts.
    return rf"""
  <script id="cs2-i18n-bootstrap">
    (() => {{
      const config = {payload};
      const translations = config.translations || {{}};
      const patternTemplates = config.patterns || [];
      const locale = config.locale;
      const intlLocale = config.intl_locale;
      const ignoredParents = new Set(['SCRIPT', 'STYLE', 'CODE', 'PRE', 'TEXTAREA']);

      function compileTemplate(source, target) {{
        const names = [];
        const escaped = String(source).split(/(\{{[A-Za-z_][A-Za-z0-9_]*\}})/g).map(part => {{
          const placeholder = /^\{{([A-Za-z_][A-Za-z0-9_]*)\}}$/.exec(part);
          if (placeholder) {{ names.push(placeholder[1]); return '(.+?)'; }}
          return part.replace(/[.*+?^${{}}()|[\]\\]/g, '\\$&');
        }}).join('');
        try {{ return {{ expression: new RegExp(`^${{escaped}}$`, 'i'), names, target }}; }}
        catch (error) {{ return null; }}
      }}
      const compiledPatterns = patternTemplates.map(pair => compileTemplate(pair[0], pair[1])).filter(Boolean);

      // Pages re-render hundreds of rows; most text nodes repeat or have no
      // translation.  Memoising the lookup avoids running every compiled
      // pattern again for each identical miss.
      const memo = new Map();
      const MEMO_LIMIT = 6000;
      function translated(value, depth = 0) {{
        const source = String(value ?? '');
        const trimmed = source.trim();
        if (!trimmed) return source;
        if (depth === 0 && memo.has(trimmed)) {{
          const cached = memo.get(trimmed);
          if (!cached) return source;
          const leading = source.slice(0, source.indexOf(trimmed));
          return `${{leading}}${{cached}}${{source.slice(leading.length + trimmed.length)}}`;
        }}
        const result = translatedUncached(source, trimmed, depth);
        if (depth === 0) {{
          if (memo.size >= MEMO_LIMIT) memo.clear();
          const resultTrimmed = result.trim();
          memo.set(trimmed, resultTrimmed !== trimmed ? resultTrimmed : null);
        }}
        return result;
      }}

      function translatedUncached(source, trimmed, depth) {{
        let target = translations[trimmed];
        if (!target) {{
          for (const pattern of compiledPatterns) {{
            const match = pattern.expression.exec(trimmed);
            if (!match) continue;
            const values = {{}};
            pattern.names.forEach((name, index) => {{
              const captured = depth < 2 ? translated(match[index + 1], depth + 1).trim() : match[index + 1];
              if (!(name in values)) values[name] = captured;
            }});
            target = pattern.target.replace(/\{{([A-Za-z_][A-Za-z0-9_]*)\}}/g, (_, name) => values[name] ?? '');
            break;
          }}
        }}
        if (!target) {{
          const patterns = locale === 'ru' ? [
            [/^Updated:\\s*(.+)$/i, 'Обновлено: $1'],
            [/^updated (.+) MSK$/i, 'обновлено $1 MSK'],
            [/^error:\\s*(.+)$/i, 'ошибка: $1'],
            [/^(\\d+) sales$/i, '$1 продаж'],
            [/^(\\d+) listings$/i, '$1 листингов'],
            [/^(\\d+) skins$/i, '$1 скинов'],
            [/^(\\d+) stickers$/i, '$1 стикеров'],
            [/^(\\d+) rows$/i, '$1 строк'],
          ] : [
            [/^Обновлено:\\s*(.+)$/i, 'Updated: $1'],
            [/^обновлено (.+) MSK$/i, 'updated $1 MSK'],
            [/^ошибка:\\s*(.+)$/i, 'error: $1'],
            [/^(\\d+) продаж(?:а|и)?$/i, '$1 sales'],
            [/^(\\d+) листинг(?:а|ов)?$/i, '$1 listings'],
            [/^(\\d+) скин(?:а|ов)?$/i, '$1 skins'],
            [/^(\\d+) стикер(?:а|ов)?$/i, '$1 stickers'],
            [/^(\\d+) строк(?:а|и)?$/i, '$1 rows'],
          ];
          for (const [pattern, replacement] of patterns) {{
            if (pattern.test(trimmed)) {{ target = trimmed.replace(pattern, replacement); break; }}
          }}
        }}
        if (!target || target === trimmed) return source;
        const leading = source.slice(0, source.indexOf(trimmed));
        return `${{leading}}${{target}}${{source.slice(leading.length + trimmed.length)}}`;
      }}

      function t(key, params = {{}}) {{
        let result = translated(key).trim();
        for (const [name, value] of Object.entries(params)) {{
          result = result.replaceAll(`{{${{name}}}}`, String(value));
        }}
        return result;
      }}

      function translateElement(element) {{
        for (const attribute of ['placeholder', 'title', 'aria-label']) {{
          if (!element.hasAttribute?.(attribute)) continue;
          const before = element.getAttribute(attribute);
          const after = translated(before);
          if (after !== before) element.setAttribute(attribute, after);
        }}
      }}

      function apply(root = document.documentElement) {{
        if (!root) return;
        if (root.nodeType === Node.TEXT_NODE) {{
          if (!root.parentElement || ignoredParents.has(root.parentElement.tagName)) return;
          const after = translated(root.data);
          if (after !== root.data) root.data = after;
          return;
        }}
        if (root.nodeType === Node.ELEMENT_NODE) translateElement(root);
        const walker = document.createTreeWalker(
          root,
          NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
        );
        let node;
        while ((node = walker.nextNode())) {{
          if (node.nodeType === Node.ELEMENT_NODE) {{ translateElement(node); continue; }}
          if (!node.parentElement || ignoredParents.has(node.parentElement.tagName)) continue;
          const after = translated(node.data);
          if (after !== node.data) node.data = after;
        }}
      }}

      window.CS2_LOCALE = locale;
      window.CS2_INTL_LOCALE = intlLocale;
      window.cs2T = t;
      window.CS2_I18N = Object.freeze({{ locale, intlLocale, t, apply }});
      document.documentElement.lang = locale;

      const cookieLocale = document.cookie.split(';').map(value => value.trim())
        .find(value => value.startsWith(`${{config.cookie_name}}=`))
        ?.slice(config.cookie_name.length + 1);
      let storedLocale = '';
      try {{ storedLocale = localStorage.getItem('cs2_locale') || ''; }} catch (error) {{}}
      if (!cookieLocale && ['ru', 'en'].includes(storedLocale) && storedLocale !== locale) {{
        const secure = location.protocol === 'https:' ? '; Secure' : '';
        document.cookie = `${{config.cookie_name}}=${{storedLocale}}; Path=/; Max-Age=${{config.cookie_max_age}}; SameSite=Lax${{secure}}`;
        location.reload();
        return;
      }}

      const control = document.createElement('label');
      control.className = 'cs2-language-control';
      control.innerHTML = `<span>${{t('Language')}}</span><select aria-label="${{t('Language')}}"><option value="ru">Русский</option><option value="en">English</option></select>`;
      const select = control.querySelector('select');
      select.value = locale;
      select.addEventListener('change', () => {{
        const next = select.value === 'en' ? 'en' : 'ru';
        try {{ localStorage.setItem('cs2_locale', next); }} catch (error) {{}}
        const secure = location.protocol === 'https:' ? '; Secure' : '';
        document.cookie = `${{config.cookie_name}}=${{next}}; Path=/; Max-Age=${{config.cookie_max_age}}; SameSite=Lax${{secure}}`;
        location.reload();
      }});
      // querySelector on a comma-separated list returns the earliest DOM node,
      // not the first selector by preference.  Resolve hosts explicitly so the
      // compact language switch stays in the header actions instead of becoming
      // a separate full-width row above the application shell.
      const host = ['.header-tools', '.top-actions', 'header .nav', 'header .topbar', 'header']
        .map(selector => document.querySelector(selector))
        .find(Boolean);
      if (host && !document.querySelector('.cs2-language-control')) host.prepend(control);
      try {{ localStorage.setItem('cs2_locale', locale); }} catch (error) {{}}

      apply(document.documentElement);
      const observer = new MutationObserver(records => {{
        for (const record of records) {{
          if (record.type === 'characterData') apply(record.target.parentElement);
          else if (record.type === 'attributes') translateElement(record.target);
          else for (const node of record.addedNodes) apply(node);
        }}
      }});
      observer.observe(document.documentElement, {{
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true,
        attributeFilter: ['placeholder', 'title', 'aria-label'],
      }});
    }})();
  </script>
"""


_CONTROL_CSS = """
  <style id="cs2-i18n-style">
    .cs2-language-control { display:inline-flex; align-items:center; gap:7px; color:var(--muted,#98a3b7); font-size:12px; font-weight:700; white-space:nowrap; }
    .cs2-language-control select { min-height:34px; padding:6px 28px 6px 9px; border:1px solid var(--line,rgba(255,255,255,.12)); border-radius:9px; color:var(--text,#f5f7fb); background:var(--panel,#111724); font:inherit; cursor:pointer; }
    @media (max-width:720px) { .cs2-language-control span { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; } }
  </style>
"""


def render_document(document: str, locale: Any) -> str:
    """Set server-selected language and inject the shared translator once."""

    selected = normalize_locale(locale)
    rendered, count = _LANG_RE.subn(rf"\g<1>\g<2>{selected}\g<2>", document, count=1)
    if count == 0:
        rendered = rendered.replace("<html", f'<html lang="{html.escape(selected)}"', 1)
    rendered = rendered.replace("__CS2_LOCALE__", selected)
    rendered = rendered.replace("__CS2_INTL_LOCALE__", INTL_LOCALES[selected])
    if 'id="cs2-i18n-bootstrap"' in rendered:
        return rendered
    rendered = translate_static_document(rendered, selected)
    if "</head>" in rendered:
        rendered = rendered.replace("</head>", f"{_CONTROL_CSS}</head>", 1)
    if "</body>" in rendered:
        rendered = rendered.replace("</body>", f"{_bootstrap(selected)}</body>", 1)
    return rendered
