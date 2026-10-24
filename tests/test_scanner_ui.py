"""Browser logic regressions, executed against the dashboard's JavaScript."""

import json
import re
import shutil
import subprocess

import pytest

import dashboard_server


def _run_javascript(tmp_path, source, names, scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    functions = []
    for name in names:
        match = re.search(
            rf"(?ms)^    (?:async )?function {re.escape(name)}\([^\n]*\).*?^    }}",
            source,
        )
        assert match is not None, f"missing JavaScript function {name}"
        functions.append(match.group())
    program = "(async () => {\n" + "\n".join(functions) + scenario + "\n})()"
    script = tmp_path / "scanner-ui.js"
    script.write_text(
        "const vm = require('node:vm');\n"
        "const assert = require('node:assert/strict');\n"
        f"vm.runInNewContext({json.dumps(program)}, "
        "{assert, AbortController, URL, clearTimeout})"
        ".catch(error => { console.error(error); process.exitCode = 1; });\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=15, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_deal_cards_display_asset_models_and_vega(tmp_path):
    _run_javascript(
        tmp_path,
        dashboard_server.DEALS_HTML,
        (
            "modelStack", "modelCell", "verdictLabel", "escapeHtml", "dealUiText",
            "optionalMoney", "money", "number",
        ),
        r"""
const window = {};
const moneyFmt = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD'});
const good = {verdict: 'good', predicted_price_cents: 12500, profit_cents: 2500};
for (const [kind, model, label] of [
  ['knife', 'knives', 'Knife model'], ['glove', 'gloves', 'Glove model'],
]) {
  const html = modelStack({item_kind: kind, models: {[model]: good}});
  assert.deepEqual([...html.matchAll(/data-model="([^"]+)"/g)].map(match => match[1]), [model]);
  assert.ok(html.includes(label));
  assert.ok(html.includes('$125.00'));
  assert.ok(!html.includes('not found'), 'dedicated assets should not show missing gun models');
}
const weapon = modelStack({item_kind: 'weapon', models: {vega: good}});
assert.deepEqual([...weapon.matchAll(/data-model="([^"]+)"/g)].map(match => match[1]),
  ['almaz', 'igor', 'nova', 'vega']);
assert.match(weapon, /data-model="vega" data-verdict="good"/);
assert.ok(weapon.includes('Vega'));
// Verdict codes are shown as readable labels, not raw API values.
assert.ok(weapon.includes('Profitable'));
assert.ok(!weapon.includes('</span>good</span>'));
// New model results must remain visible without changing the expected-model list.
assert.ok(modelStack({item_kind: 'knife', models: {knives: good, future: good}})
  .includes('data-model="future"'));
""",
    )


@pytest.mark.parametrize("stale_completion", ["early", "late", "abort"])
def test_deal_refresh_keeps_newest_results_and_loading_state(tmp_path, stale_completion):
    _run_javascript(
        tmp_path,
        dashboard_server.DEALS_HTML,
        ("refresh",),
        "const staleCompletion = " + json.dumps(stale_completion) + ";\n" + r"""
const nodes = new Map();
const document = {getElementById(id) {
  if (!nodes.has(id)) nodes.set(id, {
    value: '', textContent: '', dataset: {}, attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
  });
  return nodes.get(id);
}};
let dealSearchTimer;
let dealRefreshController;
let latestRefresh = 0;
const dashboardToken = '';
let offset = 0;
const limit = 50;
const requests = [];
const renders = [];
const normalized = [];
const errors = [];
function syncFilterState() {}
function renderDealFilterChips() {}
function dealUiText(value) { return value; }
function applyNormalizedFilterState(data) { normalized.push(data.id); }
function render(data) { renders.push(data.id); }
function showDealRefreshError(error) { errors.push(error.message); }
function fetch(url, options) {
  return new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));
}
function succeed(index, id) {
  requests[index].resolve({ok: true, json: async () => ({id})});
}
document.getElementById('search').value = 'old search';
const oldRefresh = refresh();
document.getElementById('search').value = '  new search & skin  ';
document.getElementById('categorySelect').value = 'vega';
document.getElementById('assetTypeSelect').value = 'knife';
document.getElementById('discoverySourceSelect').value = 'knife';
const newRefresh = refresh();
assert.equal(requests.length, 2);
assert.equal(requests[0].options.signal.aborted, true);
assert.equal(requests[1].options.signal.aborted, false);
const params = new URL(requests[1].url, 'http://localhost').searchParams;
assert.equal(params.get('search'), 'new search & skin');
assert.equal(params.get('category'), 'vega');
assert.equal(params.get('asset_type'), 'knife');
assert.equal(params.get('discovery_source'), 'knife');
if (staleCompletion !== 'late') {
  if (staleCompletion === 'abort') {
    const aborted = new Error('superseded request');
    aborted.name = 'AbortError';
    requests[0].reject(aborted);
  } else {
    succeed(0, 'old');
  }
  await oldRefresh;
  assert.equal(document.getElementById('rowsBody').attributes['aria-busy'], 'true');
  assert.deepEqual(renders, []);
  assert.deepEqual(normalized, []);
}
succeed(1, 'new');
await newRefresh;
assert.equal(document.getElementById('rowsBody').attributes['aria-busy'], 'false');
if (staleCompletion === 'late') {
  succeed(0, 'old');
  await oldRefresh;
}
assert.deepEqual(renders, ['new']);
assert.deepEqual(normalized, ['new']);
assert.deepEqual(errors, []);
""",
    )


def test_deal_background_refresh_waits_for_requests_and_search_debounce(tmp_path):
    background_events = re.search(
        r"(?ms)^    setInterval\(\(\) => \{\n"
        r"      if \(!document.hidden\).*?^    \}\);",
        dashboard_server.DEALS_HTML,
    )
    assert background_events is not None
    _run_javascript(
        tmp_path,
        dashboard_server.DEALS_HTML,
        ("refresh",),
        r"""
const callbacks = {};
const nodes = new Map();
const document = {
  hidden: false,
  addEventListener(name, callback) { callbacks[name] = callback; },
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', textContent: '', dataset: {}, attributes: {},
      setAttribute(name, value) { this.attributes[name] = value; },
    });
    return nodes.get(id);
  },
};
function setInterval(callback, delay) {
  assert.equal(delay, 15000);
  callbacks.interval = callback;
}
let dealSearchTimer = null;
let dealRefreshController = null;
let latestRefresh = 0;
const dashboardToken = '';
let offset = 0;
const limit = 100;
const requests = [];
const renders = [];
function syncFilterState() {}
function renderDealFilterChips() {}
function dealUiText(value) { return value; }
function applyNormalizedFilterState() {}
function render(data) { renders.push(data.id); }
function showDealRefreshError(error) { throw error; }
function fetch(url, options) {
  return new Promise(resolve => requests.push({options, resolve}));
}
async function settle(index) {
  requests[index].resolve({ok: true, json: async () => ({id: index})});
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}
"""
        + background_events.group()
        + r"""
const pending = refresh();
// A query lasting several polling intervals must still get a chance to render.
for (let tick = 0; tick < 3; tick++) callbacks.interval();
callbacks.visibilitychange();
assert.equal(requests.length, 1, 'background refresh must not duplicate a pending query');
assert.equal(requests[0].options.signal.aborted, false);
await settle(0);
await pending;
assert.deepEqual(renders, [0]);
assert.equal(dealRefreshController, null, 'finished queries should allow future polling');
// Typing has scheduled a search; polling must preserve its debounce.
dealSearchTimer = 123;
callbacks.interval();
callbacks.visibilitychange();
assert.equal(requests.length, 1);
assert.equal(dealSearchTimer, 123);
// Submitting the pending search clears its timer; polling resumes afterwards.
const submitted = refresh();
assert.equal(dealSearchTimer, null);
await settle(1);
await submitted;
callbacks.interval();
assert.equal(requests.length, 3);
await settle(2);
document.hidden = true;
callbacks.interval();
callbacks.visibilitychange();
assert.equal(requests.length, 3, 'hidden pages should stay idle');
assert.deepEqual(renders, [0, 1, 2]);
""",
    )


def test_target_group_exclusions_and_visible_bulk_actions(tmp_path):
    _run_javascript(
        tmp_path,
        dashboard_server.HTML,
        (
            "scannerTargetSelectedWithOverrides", "scannerTargetWithDraftGroup",
            "scannerTargetSelected", "applyScannerTargetChoice",
            "visibleScannerTargetItems", "updateVisibleScannerTargets",
            "updateScannerStickerGroups", "updateScannerTargetSelection",
        ),
        r"""
const draft = {
  dirty: true, selection_mode: 'custom', include_groups: [], include: ['other'],
  exclude: ['excluded'],
};
function scannerTargetDraft() { return draft; }
const scannerTargetsPayload = {limits: {}};
const scannerTargetBusy = false;
const scannerTargetProfile = 'sticker_guns';
let scannerTargetView = 'all';
const scannerTargetPreview = {items: [
  {target_key: 'group', name: 'Sticker | Team A | Katowice 2014'},
  {target_key: 'excluded', name: 'Sticker | Team B | Katowice 2014'},
  {target_key: 'other', name: 'Sticker | Team C | Cologne 2014'},
  {target_key: 'untouched', name: 'Sticker | Team D | Cologne 2014'},
]};
function setScannerTargetFeedback() {}
function renderScannerTargetPreview() {}
function visibleKeys() { return visibleScannerTargetItems().map(item => item.target_key); }
updateScannerStickerGroups(['Katowice 2014'], true);
assert.deepEqual(visibleKeys(), ['group', 'excluded', 'other', 'untouched']);
scannerTargetView = 'selected';
assert.deepEqual(visibleKeys(), ['group', 'other'], 'individual exclusions override whole groups');
scannerTargetView = 'unselected';
assert.deepEqual(visibleKeys(), ['excluded', 'untouched']);
scannerTargetView = 'selected';
updateVisibleScannerTargets(false);
assert.deepEqual(visibleKeys(), []);
assert.deepEqual([...draft.exclude].sort(), ['excluded', 'group']);
assert.deepEqual(draft.include, []);
assert.ok(!draft.exclude.includes('untouched'), 'bulk changes must leave hidden targets alone');
updateScannerTargetSelection('group', true);
assert.deepEqual(visibleKeys(), ['group']);
assert.ok(!draft.exclude.includes('group'), 'rechecking removes the individual exclusion');
assert.ok(draft.exclude.includes('excluded'), 'other individual exclusions are preserved');
updateScannerStickerGroups(['Katowice 2014'], false);
assert.deepEqual(visibleKeys(), ['group'], 'explicit selection survives removing the group');
""",
    )
