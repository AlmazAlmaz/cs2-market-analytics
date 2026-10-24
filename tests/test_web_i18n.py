import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

import dashboard_server
import web_i18n
import web_i18n_catalog


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self._protected_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._protected_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._protected_depth -= 1

    def handle_data(self, data):
        if not self._protected_depth and data.strip():
            self.parts.append(" ".join(data.split()))


class _Request:
    def __init__(self, *, cookie="", accept_language=""):
        self.headers = {
            "Cookie": cookie,
            "Accept-Language": accept_language,
        }


def _visible(document):
    parser = _VisibleText()
    parser.feed(document)
    return "\n".join(parser.parts)


def _visible_parts(document):
    parser = _VisibleText()
    parser.feed(document)
    return parser.parts


def test_locale_normalisation_accept_language_and_cookie_precedence():
    assert web_i18n.normalize_locale("EN_us") == "en"
    assert web_i18n.normalize_locale("ru-RU") == "ru"
    assert web_i18n.normalize_locale("de") == "ru"
    assert web_i18n.parse_accept_language("de, en;q=0.8, ru;q=0.5") == "en"
    assert web_i18n.parse_accept_language("en;q=0.2, ru;q=0.9") == "ru"

    request = _Request(
        cookie="other=1; cs2_locale=en-US",
        accept_language="ru-RU,ru;q=0.9",
    )
    assert web_i18n.request_locale(request) == "en"
    assert web_i18n.request_locale(
        _Request(cookie="cs2_locale=de", accept_language="en-US")
    ) == "en"
    assert web_i18n.request_locale(_Request()) == "ru"


def test_locale_cookie_is_scoped_persistent_and_secure_when_requested():
    plain = web_i18n.locale_cookie_header("en")
    assert plain.startswith("cs2_locale=en;")
    assert "Path=/" in plain
    assert "Max-Age=31536000" in plain
    assert "SameSite=Lax" in plain
    assert "Secure" not in plain
    assert web_i18n.locale_cookie_header("ru", secure=True).endswith("; Secure")


def test_language_switch_prefers_compact_header_actions_over_earlier_header():
    rendered = web_i18n.render_document(
        """<!doctype html><html lang="ru"><head></head><body>
        <header><div class="top-actions"></div></header></body></html>""",
        "ru",
    )

    assert "const host = ['.header-tools', '.top-actions'" in rendered
    assert ".map(selector => document.querySelector(selector))" in rendered
    assert ".find(Boolean)" in rendered
    assert "document.querySelector('.header-tools, .top-actions" not in rendered


def test_extended_catalog_is_valid_and_available_in_both_directions():
    web_i18n_catalog.validate_catalog()
    web_i18n_catalog.validate_pattern_catalog()
    russian = web_i18n.translations_for("ru")
    english = web_i18n.translations_for("en")
    assert len(web_i18n_catalog.TRANSLATION_PAIRS) >= 650
    for source, target in web_i18n_catalog.TRANSLATION_PAIRS:
        assert russian[source] == target
        assert russian[target] == target
        assert english[source] == source
        assert target in english


@pytest.mark.parametrize(
    ("source", "locale", "expected"),
    [
        (
            "3/10 targets · 8 listings · 2 saved",
            "ru",
            "3/10 целей · 8 лотов · 2 сохранено",
        ),
        (
            "3/10 целей · 8 лотов · 2 сохранено",
            "en",
            "3/10 targets · 8 listings · 2 saved",
        ),
        ("14 sales | 1.25 per day", "ru", "14 продаж | 1.25 в день"),
        (
            "Сейчас $10.00 · 20 дневных наблюдений",
            "en",
            "Now $10.00 · 20 daily observations",
        ),
        ("Прогресс обучения nova", "en", "Training progress nova"),
        (
            "index 100 · direction up · 3 options",
            "ru",
            "индекс 100 · направление вверх · 3 вариантов",
        ),
        (
            "5 предметов · осталось $2.00 · это только предложение, заказы не создавались.",
            "en",
            "5 items · $2.00 left · suggestion only, no orders were created.",
        ),
    ],
)
def test_dynamic_pattern_catalog_formats_both_languages(source, locale, expected):
    assert web_i18n.translate_dynamic(source, locale) == expected


@pytest.mark.parametrize(
    ("english_template", "russian_template"),
    web_i18n_catalog.PATTERN_TRANSLATIONS,
)
def test_every_dynamic_pattern_round_trips_without_generic_shadowing(
    english_template, russian_template
):
    values = {
        name: f"VALUE_{name.upper()}"
        for name in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", english_template)
    }

    def materialize(template):
        return re.sub(
            r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda match: values[match.group(1)],
            template,
        )

    english = materialize(english_template)
    russian = materialize(russian_template)
    assert web_i18n.translate_dynamic(english, "ru") == russian
    assert web_i18n.translate_dynamic(russian, "en") == english


@pytest.mark.parametrize(
    ("template_name", "source"),
    [
        ("PUBLIC_HTML", "CS2 skin price intelligence"),
        ("HTML", "Everything important, one glance away."),
        ("DEAL_HTML", "Compare three estimates before you decide."),
        ("DEALS_HTML", "Best deals, ranked by evidence."),
        ("STRATEGY_HTML", "От выгодного лота к взвешенному решению."),
    ],
)
@pytest.mark.parametrize("locale", ["ru", "en"])
def test_all_dashboard_pages_are_translated_server_side(template_name, source, locale):
    template = getattr(dashboard_server, template_name)
    if template_name == "PUBLIC_HTML":
        rendered = web_i18n.render_document(template, locale)
    else:
        rendered = dashboard_server.render_private_html(template, locale)
    expected = web_i18n.translate(source, locale)
    opposite = web_i18n.translate(source, "en" if locale == "ru" else "ru")
    visible = _visible(rendered)

    assert f'<html lang="{locale}">' in rendered
    assert expected in visible
    if expected != opposite:
        assert opposite not in visible
    assert "__CS2_INTL_LOCALE__" not in rendered
    assert f'"locale":"{locale}"' in rendered
    assert "cs2-language-control" in rendered
    assert "cs2_locale" in rendered
    assert "localStorage.setItem('cs2_locale'" in rendered


def test_static_translation_does_not_rewrite_javascript_or_identifiers():
    source = """<!doctype html><html lang="ru"><head><title>Overview</title></head>
    <body><label title="Theme">Overview</label>
    <script>const Overview = 'Overview'; const url = '/api/Overview';</script></body></html>"""
    rendered = web_i18n.render_document(source, "ru")
    visible = _visible(rendered)
    assert "Обзор" in visible
    assert 'title="Тема"' in rendered
    assert "const Overview = 'Overview'" in rendered
    assert "'/api/Overview'" in rendered


def test_static_catalog_has_no_untranslated_opposite_language_copy():
    english_sources = {source for source, _ in web_i18n_catalog.TRANSLATION_PAIRS}
    russian_sources = {target for _, target in web_i18n_catalog.TRANSLATION_PAIRS}
    for template_name in (
        "PUBLIC_HTML",
        "HTML",
        "DEAL_HTML",
        "DEALS_HTML",
        "STRATEGY_HTML",
    ):
        template = getattr(dashboard_server, template_name)
        russian_page = web_i18n.render_document(template, "ru")
        english_page = web_i18n.render_document(template, "en")
        untranslated_russian = set(_visible_parts(russian_page)) & english_sources
        untranslated_english = set(_visible_parts(english_page)) & russian_sources
        assert not untranslated_russian, (template_name, untranslated_russian)
        assert not untranslated_english, (template_name, untranslated_english)
        assert not re.search(r"[А-Яа-яЁё]", _visible(english_page)), template_name


def test_dashboard_source_and_rendered_pages_have_no_mojibake_or_legacy_mixed_copy():
    source = Path(dashboard_server.__file__).read_text(encoding="utf-8")
    assert not re.search(r"(?:Ð|Ñ)[\x80-\xffА-Яа-я]", source)
    forbidden_strategy_copy = (
        "Больше в snapshot",
        "Листингов в snapshot",
        "Растущих constituent’ов",
        "Свежесть live-лотов",
        "float-edge",
        "sticker value",
        "world supply",
        "CSFloat ref",
        "CSFloat reference",
    )
    for phrase in forbidden_strategy_copy:
        assert phrase not in dashboard_server.STRATEGY_HTML
    for locale in ("ru", "en"):
        rendered = dashboard_server.render_private_html(
            dashboard_server.STRATEGY_HTML, locale
        )
        assert not re.search(r"(?:Ð|Ñ)[\x80-\xffА-Яа-я]", rendered)


def test_model_comparison_is_the_canonical_localized_nova_ui():
    template = dashboard_server.DEAL_HTML
    assert "/api/predict-deal?" in template
    assert "['almaz', 'igor', 'nova', 'vega']" in template
    assert "Vega (advisory)" in template
    assert 'id="modelComparisonGrid"' in template

    rendered = dashboard_server.render_private_html(template, "ru")
    assert "/api/predict-deal?" in rendered
    assert 'href="/neural"' not in rendered
    assert rendered.count('data-nav-path="/deal"') == 1
    assert "__CS2_INTL_LOCALE__" not in rendered
    assert '"locale":"ru"' in rendered


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_shared_i18n_bootstrap_passes_node_syntax_check(locale, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    document = web_i18n.render_document(
        '<!doctype html><html lang="ru"><head></head><body>Overview</body></html>',
        locale,
    )
    scripts = re.findall(
        r"<script(?:\s[^>]*)?>(.*?)</script>",
        document,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert scripts
    path = tmp_path / f"web-i18n-{locale}.js"
    path.write_text("\n".join(scripts), encoding="utf-8")
    completed = subprocess.run(
        [node, "--check", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("locale", "samples"),
    [
        (
            "ru",
            {
                "3/10 targets · 8 listings · 2 saved": "3/10 целей · 8 лотов · 2 сохранено",
                "14 sales | 1.25 per day": "14 продаж | 1.25 в день",
                "Training progress nova": "Прогресс обучения nova",
            },
        ),
        (
            "en",
            {
                "Сейчас $10.00 · 20 дневных наблюдений": "Now $10.00 · 20 daily observations",
                "3/10 целей · 8 лотов · 2 сохранено": "3/10 targets · 8 listings · 2 saved",
                "Прогресс обучения nova": "Training progress nova",
            },
        ),
    ],
)
def test_browser_runtime_translates_dynamic_patterns(locale, samples, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    document = web_i18n.render_document(
        '<!doctype html><html lang="ru"><head></head><body></body></html>', locale
    )
    scripts = re.findall(
        r"<script(?:\s[^>]*)?>(.*?)</script>",
        document,
        flags=re.IGNORECASE | re.DOTALL,
    )
    stub = f"""
global.window=globalThis;
global.Node={{ELEMENT_NODE:1,TEXT_NODE:3}};
global.NodeFilter={{SHOW_ELEMENT:1,SHOW_TEXT:4}};
global.location={{protocol:'http:',reload(){{throw new Error('unexpected reload')}}}};
global.localStorage={{getItem(){{return null}},setItem(){{}}}};
const select={{value:'',addEventListener(){{}}}};
const control={{nodeType:1,className:'',innerHTML:'',querySelector(){{return select}},hasAttribute(){{return false}}}};
global.document={{
  cookie:'cs2_locale={locale}',
  documentElement:{{nodeType:1,lang:'',hasAttribute(){{return false}}}},
  createElement(){{return control}},querySelector(){{return null}},
  createTreeWalker(){{return {{nextNode(){{return null}}}}}},
}};
global.MutationObserver=class{{constructor(callback){{global.observerCallback=callback}}observe(){{}}}};
"""
    assertions = [
        f"if(CS2_I18N.t({json.dumps(source)})!=={json.dumps(expected)})throw new Error({json.dumps(source)});"
        for source, expected in samples.items()
    ]
    dynamic_source, dynamic_expected = next(iter(samples.items()))
    title_source = "Training progress nova" if locale == "ru" else "Прогресс обучения nova"
    title_expected = "Прогресс обучения nova" if locale == "ru" else "Training progress nova"
    assertions.extend(
        [
            (
                "const dynamicText={nodeType:Node.TEXT_NODE,"
                f"data:{json.dumps(dynamic_source)},parentElement:{{tagName:'DIV'}}}};"
                "observerCallback([{type:'childList',addedNodes:[dynamicText]}]);"
                f"if(dynamicText.data!=={json.dumps(dynamic_expected)})throw new Error('dynamic text node');"
            ),
            (
                f"const dynamicAttribute={{value:{json.dumps(title_source)},"
                "hasAttribute(name){return name==='title'},getAttribute(){return this.value},"
                "setAttribute(name,value){this.value=value}};"
                "observerCallback([{type:'attributes',target:dynamicAttribute}]);"
                f"if(dynamicAttribute.value!=={json.dumps(title_expected)})throw new Error('dynamic attribute');"
            ),
        ]
    )
    path = tmp_path / f"web-i18n-runtime-{locale}.js"
    path.write_text(stub + "\n" + "\n".join(scripts + assertions), encoding="utf-8")
    completed = subprocess.run(
        [node, str(path)], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
