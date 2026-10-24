import re
import shutil
import subprocess

import pytest

import account_ui


def _inline_scripts(document):
    return re.findall(
        r"<script(?:\s[^>]*)?>(.*?)</script>",
        document,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_account_ui_keeps_money_profit_revision_and_sticker_contracts():
    page = account_ui.render_account_page()

    # An absent amount is not silently converted to a valid-looking $0 value.
    assert "if(!text)return null" in page
    assert "Number.isFinite(n)&&cents>0" in page
    assert "setFeedback($('#formFeedback'),tr('priceInvalid'),'bad')" in page
    assert "if(!hasNumber(value)){delete node.dataset.tone;return;}" in page
    assert "hasNumber(roi)?" in page

    # Mutations use the revision returned with the item instead of overwriting
    # a change made from another browser tab.
    assert "body.expected_revision=state.editingRevision" in page
    # A new-item FormData contains a blank hidden item_id.  It must not be
    # sent as part of create, otherwise the server interprets it as an update
    # identifier and rejects every purchase.
    assert "else delete body.item_id" in page
    assert "expected_revision:current.revision" in page

    # Sold cards must use realized P&L/ROI.  The market recommendation remains
    # the source for active inventory only.
    assert (
        "const profit=item.status==='sold'?item.realized_profit_cents:"
        "p.unrealized_profit_cents" in page
    )
    assert (
        "const roi=item.status==='sold'?item.analysis?.roi_percent:"
        "p.unrealized_roi_percent" in page
    )
    assert "const stale=item.status==='listed'&&Boolean(p.is_stale)" in page
    assert "p.manual_premium_required?" in page
    assert "p.special_premium_note?" not in page
    assert "Покупка / шт." in page
    assert "Продано / шт." in page
    assert "tr('soldOn',{date:saleDate})" in page
    assert "tr('listedSince',{date:listedDate})" in page

    # The friendly text field only edits sticker names.  When it was not
    # touched, resubmit the original structured array so wear/slot/price data
    # is not destroyed by an unrelated edit.
    assert "state.editingStickerText=stickers" in page
    assert (
        "if(body.stickers===state.editingStickerText)"
        "body.stickers=state.editingStickers" in page
    )


def test_account_ui_can_edit_or_undo_a_recorded_sale():
    russian = account_ui.render_account_page("ru")
    english = account_ui.render_account_page("en")

    # A sold position exposes separate sale controls.  Editing the position's
    # purchase metadata remains a different action.
    assert 'data-action="edit_sale"' in russian
    assert 'data-action="undo_sale"' in russian
    assert "Изменить продажу" in russian
    assert "Отменить продажу" in russian
    assert "Edit sale" in english
    assert "Undo sale" in english

    # The same dialog records price, date, and the actual sale fee.  Existing
    # sold values are loaded when editing instead of using today's defaults.
    assert 'id="soldForm" data-action="mark_sold"' in english
    assert 'name="sold_price"' in english
    assert 'name="sold_at" type="date" required' in english
    assert 'name="fee_percent" inputmode="decimal" required' in english
    assert "openSaleDialog(id,'update_sale')" in english
    assert "editing?item.sold_price_cents" in english
    assert "editing?String(item.sold_at||'').slice(0,10):today()" in english
    assert "form.elements.fee_percent.value=item.fee_percent" in english

    # Save uses the dedicated backend action, includes all editable sale
    # fields, and goes through inventoryAction so expected_revision is sent.
    assert "form.dataset.action==='update_sale'?'update_sale':'mark_sold'" in english
    assert "sold_price_cents:soldPrice,sold_at:form.elements.sold_at.value,fee_percent:feePercent" in english
    assert "expected_revision:current.revision" in english

    # Undo is deliberate and also uses optimistic concurrency protection.
    assert "action==='undo_sale'&&!confirm(tr('undoSaleConfirm'))" in english
    assert "Цена продажи и реализованная прибыль будут убраны" in russian
    assert "The sale price and realized profit will be removed" in english


def test_account_ui_imports_csfloat_link_without_saving_before_confirmation():
    page = account_ui.render_account_page("en")

    assert 'id="sourceUrlInput" name="source_url"' in page
    assert 'id="importCsfloatButton" type="button"' in page
    assert "sourceUrlInput.addEventListener('paste'" in page
    assert (
        "api('/api/account/import-csfloat',{method:'POST',body:{url}})" in page
    )

    # The listing response fills the editable form; it never submits the
    # purchase automatically.  The current ask is deliberately labelled as
    # something the user must review before pressing Save.
    assert "form.elements.skin_name.value=name" in page
    assert "form.elements.buy_price.value=priceAvailable?" in page
    assert "form.elements.float_value.value=hasNumber(listing.float_value)?" in page
    assert "form.elements.source_url.value=canonicalUrl" in page
    assert "current listing ask, not a confirmed purchase amount" in page
    assert "requestSubmit(" not in page

    # Untouched imported stickers keep their structured slot/wear/price data
    # when the user explicitly saves the form.
    assert "state.importedStickers=stickers" in page
    assert (
        "Array.isArray(state.importedStickers)&&"
        "String(body.source_url||'').trim()===state.importedSourceUrl&&"
        "body.stickers===state.importedStickerText" in page
    )
    assert "body.stickers=state.importedStickers" in page


def test_account_ui_localizes_csfloat_import_states_and_server_error_codes():
    russian = account_ui.render_account_page("ru")
    english = account_ui.render_account_page("en")

    assert "Загружаем данные лота с CSFloat" in russian
    assert "текущая цена лота" in russian
    assert "Loading listing data from CSFloat" in english
    assert "csfloat_rate_limited:'csfloatRateLimited'" in english
    assert "csfloat_listing_not_found:'csfloatListingNotFound'" in english
    assert "csfloat_not_configured:'csfloatNotConfigured'" in english
    assert "csfloat_invalid_response:'csfloatInvalidResponse'" in english
    assert "csfloat_unavailable:'csfloatUnavailable'" in english
    assert "listing.price_kind==='historical_listing_ask'" in english
    assert "historical listing ask, not a confirmed purchase amount" in english


def test_account_purchase_form_keeps_only_the_quick_path_expanded():
    russian = account_ui.render_account_page("ru")
    english = account_ui.render_account_page("en")

    form_start = english.index('<form class="trade-form" id="inventoryForm">')
    form_end = english.index("</form>", form_start)
    form = english[form_start:form_end]
    details_start = form.index(
        '<details class="optional-details" id="optionalInventoryDetails">'
    )
    details_end = form.index("</details>", details_start)

    # The default path is intentionally short: import, confirm name and price,
    # then save. Existing control names stay unchanged for FormData/CRUD.
    for fragment in (
        'id="sourceUrlInput" name="source_url"',
        'name="skin_name"',
        'name="buy_price"',
    ):
        assert 0 <= form.index(fragment) < details_start
    assert form.index('id="saveButton"') > details_end

    for field_name in (
        "quantity",
        "bought_at",
        "strategy",
        "fee_percent",
        "float_value",
        "listed_price",
        "listed_at",
        "stickers",
        "notes",
    ):
        position = form.index(f'name="{field_name}"')
        assert details_start < position < details_end

    assert ">Дополнительно<" in russian
    assert "optionalDetails:'Optional details'" in english
    assert "$('#optionalInventoryDetails').open=false" in english
    assert "$('#optionalInventoryDetails').open=true" in english
    assert (
        "if($('#optionalInventoryDetails').contains(event.target))"
        "$('#optionalInventoryDetails').open=true" in english
    )


def test_account_profile_and_security_forms_are_compact_disclosures():
    page = account_ui.render_account_page("en")
    settings_start = page.index('<section class="settings" id="settingsPanel">')
    settings_end = page.index("</section>", settings_start)
    settings = page[settings_start:settings_end]

    assert '<details class="settings-disclosure">' in settings
    assert 'data-i18n="accountSettings"' in settings
    assert 'id="profileForm"' in settings
    assert 'id="passwordForm"' in settings
    assert 'data-i18n="profileSettings"' in settings
    assert 'data-i18n="passwordSecurity"' in settings
    assert "profileSettings:'Profile and calculations'" in page
    assert "passwordSecurity:'Security'" in page


def test_account_and_auth_pages_are_responsive_and_accessible():
    account = account_ui.render_account_page()
    auth = account_ui.render_auth_page("login")

    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in account
    assert "@media(max-width:620px)" in account
    assert "@media(max-width:850px)" in account
    assert "html { max-width:100%;overflow-x:hidden; }" in account
    assert "body { margin:0;max-width:100%;overflow-x:hidden" in account
    assert ".topbar>*{min-width:0}" in account
    assert "grid-template-columns:minmax(0,1fr)" in account
    assert "nav{order:3;width:100%;flex-basis:100%" in account
    assert 'aria-live="polite"' in account
    assert 'type="date" required' in account
    assert 'autocomplete="current-password"' in account

    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in auth
    assert "@media (max-width: 480px)" in auth
    assert 'role="status" aria-live="polite"' in auth


@pytest.mark.parametrize("renderer", [
    lambda locale: account_ui.render_auth_page("login", locale),
    account_ui.render_account_page,
])
def test_account_pages_render_server_locale_and_visible_persistent_switch(renderer):
    russian = renderer("ru")
    english = renderer("en")
    fallback = renderer("unsupported")

    assert '<html lang="ru">' in russian
    assert '<html lang="en">' in english
    assert '<html lang="ru">' in fallback
    assert "__LOCALE__" not in english
    assert 'id="localeSelect"' in english
    assert '<option value="ru">RU</option><option value="en">EN</option>' in english
    assert "cookieValue('cs2_locale')" in english or "readCookie('cs2_locale')" in english
    assert "localStorage.getItem('cs2_locale')" in english
    assert "localStorage.setItem('cs2_locale',value)" in english
    assert "document.cookie=`cs2_locale=" in english
    assert "document.documentElement.lang=locale" in english


@pytest.mark.parametrize(
    "document",
    [account_ui.render_auth_page("login", "en"), account_ui.render_account_page("en")],
    ids=("auth", "account"),
)
def test_every_static_translation_marker_has_ru_and_en_entries(document):
    ru_start = document.index("ru: {")
    en_start = document.index("en: {", ru_start)
    dictionary_end = document.index("\n      }\n    };", en_start)
    ru_dictionary = document[ru_start:en_start]
    en_dictionary = document[en_start:dictionary_end]
    keys = set(
        re.findall(
            r'data-i18n(?:-placeholder|-aria-label)?="([A-Za-z][A-Za-z0-9]*)"',
            document,
        )
    )

    assert keys
    for key in keys:
        assert re.search(rf"\b{re.escape(key)}:", ru_dictionary), key
        assert re.search(rf"\b{re.escape(key)}:", en_dictionary), key


def test_account_dynamic_content_and_errors_are_localized_without_raw_api_text():
    account = account_ui.render_account_page("en")
    auth = account_ui.render_auth_page("login", "en")

    assert "new Intl.NumberFormat(locale==='ru'?'ru-RU':'en-US'" in account
    assert "new Intl.PluralRules(locale)" in account
    assert "strategyLabel(item.strategy)" in account
    assert "recommendationText(p)" in account
    assert "item.strategy_label" not in account
    assert "p.recommendation_short" not in account
    assert "confirm(tr('archiveConfirm'))" in account
    assert "alert(displayError(error))" in account
    assert "responseErrorKey(payload.error,response.status)" in account
    assert "throw new Error(payload.error" not in account

    assert "safeServerMessage(payload.error,response.status,payload.error_code)" in auth
    assert "payload.approval_required === true" in auth
    assert "registrationPendingApproval" in auth
    assert "payload.message" not in auth
    assert "'invalid email or password':'invalidCredentials'" in auth
    assert "'an account with this email already exists':'duplicateEmail'" in auth
    assert "'password-reset email delivery is not configured':'smtpMissing'" in auth


def test_account_item_catalog_drives_trade_autocomplete_preview_and_floor_price():
    page = account_ui.render_account_page("en")

    assert "/api/account/item-search?q=" in page
    assert "/api/account/item-quote?market_hash_name=" in page
    assert "const itemLookupState=new WeakMap()" in page
    assert "new AbortController()" in page
    assert "incomingItemSuggestions${legKey}" in page
    assert 'class="item-preview" hidden aria-live="polite"' in page
    assert "item.icon_url?`<img" in page
    assert "itemKindText(item.item_kind)" in page

    # A live CSFloat floor fills an empty/previously auto-filled trade value,
    # while a value manually changed by the user remains authoritative.
    assert "!String(valueInput?.value||'').trim()" in page
    assert "current===Number(lookup.autoValueCents)" in page
    assert "item.price_is_live_ask" in page
    assert "lookupStateFor(row.querySelector('[name=\"skin_name\"]')).autoValueCents=null" in page

    # Purchase lookup is informational; only a pasted concrete listing may
    # fill the actual purchase-price field.
    assert 'id="purchaseItemPreview" hidden' in page
    assert "scheduleItemSearch(purchaseNameInput" in page
    assert "quoteExactItem(purchaseNameInput" in page
    lookup_start = page.index("function applyResolvedItem")
    lookup_end = page.index("async function runItemSearch", lookup_start)
    assert "buy_price" not in page[lookup_start:lookup_end]


def test_inventory_cards_label_catalog_images_trade_origin_and_price_source():
    page = account_ui.render_account_page("en")

    assert "item.acquisition_type==='trade'" in page
    assert "receivedByTrade:'Received in trade'" in page
    assert "pricing.valuation_is_live_ask" in page
    assert "priceCsfloatFloor:'CSFloat floor / item'" in page
    assert "priceMarketHistory:'Market history / item'" in page
    assert "priceSavedManual:'Saved value / item'" in page
    assert "hasNumber(p.active_quantity)?plural('activeOffers'" in page
    assert '<option value="balanced"' in page
    assert '<option value="manual"' in page


@pytest.mark.parametrize("mode", ["login", "register", "forgot", "reset"])
def test_account_inline_javascript_passes_node_syntax_check(mode, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")

    documents = [account_ui.render_account_page(), account_ui.render_auth_page(mode)]
    for index, document in enumerate(documents):
        scripts = _inline_scripts(document)
        assert scripts
        script = tmp_path / f"{mode}-{index}.js"
        script.write_text("\n".join(scripts), encoding="utf-8")
        completed = subprocess.run(
            [node, "--check", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
