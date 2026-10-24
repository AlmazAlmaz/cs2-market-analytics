from __future__ import annotations

import re
import shutil
import subprocess

import pytest

import admin_users_ui


def test_api_contract_is_explicit_and_stable():
    contract = admin_users_ui.ADMIN_USERS_API_CONTRACT

    assert contract["endpoint"] == "/api/admin/users"
    assert contract["required_post_fields"] == (
        "action",
        "user_id",
        "expected_revision",
    )
    assert contract["access_states"] == (
        "pending",
        "approved",
        "blocked",
        "deleted",
    )
    assert contract["actions"] == (
        "approve",
        "block",
        "grant_admin",
        "revoke_admin",
        "revoke_sessions",
        "send_password_reset",
        "delete",
    )


@pytest.mark.parametrize(
    ("locale", "title", "heading", "default_access_copy"),
    [
        (
            "ru",
            "Пользователи · CS2 Market",
            "Доступ пользователей",
            "По умолчанию у каждого нового аккаунта отказ в доступе",
        ),
        (
            "en",
            "Users · CS2 Market",
            "User access",
            "Every new account is denied by default",
        ),
    ],
)
def test_page_is_bilingual_and_explains_deny_by_default(
    locale, title, heading, default_access_copy
):
    page = admin_users_ui.render_admin_users_page(locale)

    assert f'<html lang="{locale}">' in page
    assert f"<title>{title}</title>" in page
    assert heading in page
    assert default_access_copy in page
    assert 'id="localeSelect"' in page
    assert "cs2_locale" in page
    assert "localStorage.setItem('cs2_locale',value)" in page


def test_unknown_locale_falls_back_to_russian():
    page = admin_users_ui.render_admin_users_page("de")

    assert '<html lang="ru">' in page
    assert "<title>Пользователи · CS2 Market</title>" in page


def test_page_covers_search_filters_summary_and_live_activity():
    page = admin_users_ui.render_admin_users_page()

    for element_id in (
        "searchInput",
        "activityFilter",
        "sortSelect",
        "metricTotal",
        "metricPending",
        "metricApproved",
        "metricBlocked",
        "metricOnline",
        "metricDownloading",
        "usersList",
    ):
        assert f'id="{element_id}"' in page

    for status in ("all", "pending", "approved", "blocked", "deleted"):
        assert f'data-status="{status}"' in page

    assert "const ONLINE_WINDOW_SECONDS=300" in page
    assert "window.setInterval" in page
    assert "window.setInterval(refreshVisibleUsers,2000)" in page
    assert "last_seen_at" in page
    assert "last_login_at" in page
    assert "active_sessions" in page
    assert "recent_sessions" in page


def test_page_displays_database_downloads_and_separate_access_key_history():
    page = admin_users_ui.render_admin_users_page()

    for field in (
        "database_downloads", "unattributed_downloads", "active_count",
        "total_bytes", "completed_count", "bytes_sent", "filename",
        "started_at", "updated_at", "finished_at", "ip_address",
    ):
        assert field in page
    assert 'id="keyDownloadsPanel"' in page
    assert 'value="downloading" data-i18n="downloadingOnly"' in page
    assert "Скачивания по ключу доступа" in page
    assert "Downloads using an access key" in page
    assert "Скачивания до включения учёта недоступны" in page
    assert "including partial downloads" in page
    assert "may differ from the file saved on the device" in page
    assert "document.addEventListener('visibilitychange',refreshVisibleUsers)" in page
    assert "if(loadingPromise)return loadingPromise" in page
    assert "rememberDetails(target)" in page


def test_page_displays_sanitized_network_security_and_account_metadata():
    page = admin_users_ui.render_admin_users_page("en")

    for field in (
        "current_ip",
        "current_ip_address",
        "last_ip",
        "last_ip_address",
        "registration_ip",
        "registration_ip_address",
        "current_country",
        "country_code",
        "last_user_agent",
        "password_changed_at",
        "password_reset_sent_at",
        "created_at",
        "approved_at",
        "blocked_at",
        "revision",
    ):
        assert field in page

    assert "Country is an estimate derived from a trusted proxy header or IP" in page
    assert "password_hash" not in page
    assert "session_token" not in page
    assert "dashboard_token" not in page


def test_mutations_use_one_endpoint_revision_and_allowlisted_action_names():
    page = admin_users_ui.render_admin_users_page()

    assert "const API_ENDPOINT='/api/admin/users'" in page
    assert "const ACTIONS=new Set([" in page
    assert "method:'POST'" in page
    assert "credentials:'same-origin'" in page
    assert "cookieValue('cs2_csrf')" in page
    assert "headers['X-CSRF-Token']=csrf" in page
    assert "headers:mutationHeaders()" in page
    assert (
        "const requestBody={action,user_id:user.id,"
        "expected_revision:safeNumber(user.revision)}" in page
    )
    assert "requestBody.confirmation_email=" in page
    assert "body:JSON.stringify(requestBody)" in page
    assert "admin-user-management" in page
    assert "payload.error" not in page
    for action in admin_users_ui.ADMIN_USERS_API_CONTRACT["actions"]:
        assert f'"{action}"' in page

    assert "user.role==='admin'" in page
    assert "grantAdminConfirm" in page
    assert "revokeAdminConfirm" in page
    assert "system dashboard and database download" in page


def test_delete_requires_typing_the_exact_email_and_self_destruction_is_disabled():
    page = admin_users_ui.render_admin_users_page()

    assert 'id="confirmField" hidden' in page
    assert "pendingAction.user.email" in page
    assert "event.target.value" in page
    assert "pendingAction?.action!=='delete'" in page
    assert "user.is_self===true&&(action==='block'||action==='delete')" in page
    assert "byId('submitConfirm').disabled=isDelete" in page


def test_password_reset_link_is_transient_copyable_and_never_html_injected():
    page = admin_users_ui.render_admin_users_page("en")

    assert 'id="resetLinkDialog"' in page
    assert 'id="resetLinkValue" type="text" readonly' in page
    assert "showResetLink(payload.reset_url)" in page
    assert "navigator.clipboard.writeText(value)" in page
    assert "byId('resetLinkValue').value=''" in page
    assert "new URL(raw,location.origin)" in page
    assert "parsed.protocol==='https:'||parsed.protocol==='http:'" in page
    assert "reset_url" not in re.sub(
        r"showResetLink\(payload\.reset_url\)", "", page
    ).split("<script>", 1)[0]
    assert ".innerHTML" not in page
    assert "insertAdjacentHTML" not in page
    assert "document.write" not in page
    assert ".outerHTML" not in page
    assert ".textContent" in page
    assert "replaceChildren()" in page


def test_local_admin_boundary_is_prominent_but_not_mistaken_for_enforcement():
    page = admin_users_ui.render_admin_users_page()

    assert "Только localhost · администратор" in page
    assert "local_required" in page
    assert "localhost + administrator access before rendering" not in page
    assert "Authorization is intentionally not accepted" not in page


def test_page_has_responsive_layout_and_no_horizontal_overflow_defaults():
    page = admin_users_ui.render_admin_users_page()

    assert "html,body{max-width:100%;overflow-x:hidden}" in page
    assert "@media(max-width:760px)" in page
    assert "@media(max-width:480px)" in page
    assert "minmax(0,1fr)" in page
    assert "overflow-wrap:anywhere" in page


def test_inline_browser_program_has_valid_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    page = admin_users_ui.render_admin_users_page()
    match = re.search(r"<script>(.*?)</script>", page, flags=re.DOTALL)
    assert match

    result = subprocess.run(
        [node, "--check"],
        input=match.group(1),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_download_rendering_filter_refresh_and_preserved_controls_in_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    match = re.search(
        r"<script>(.*?)</script>", admin_users_ui.render_admin_users_page("en"), re.DOTALL
    )
    assert match
    program = match.group(1).split("    locale=storedLocale();", 1)[0]
    harness = r"""
const assert=require('node:assert/strict');
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attributes={};this.className='';this.value='';this.open=false;this._text=''}
  set textContent(value){this._text=String(value);this.children=[]}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('')}
  appendChild(child){child.parentElement=this;this.children.push(child);return child}
  replaceChildren(){this.children=[];this._text=''}
  setAttribute(key,value){this.attributes[key]=String(value)}
  addEventListener(){}
  matches(selector){if(selector==='details[data-detail-key]')return this.tagName==='DETAILS'&&this.dataset.detailKey!==undefined;if(selector==='[data-action]')return this.dataset.action!==undefined;if(selector.startsWith('.'))return this.className.split(' ').includes(selector.slice(1));return this.tagName===selector.toUpperCase()}
  querySelectorAll(selector){return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)])}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null}
  closest(selector){return this.matches(selector)?this:this.parentElement?.closest(selector)||null}
  focus(){document.activeElement=this}
}
const elements=new Map();
const document={activeElement:null,visibilityState:'visible',cookie:'',documentElement:{},addEventListener(){},querySelectorAll(){return []},getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id)},createElement:tag=>new Element(tag),createTextNode:value=>{const node=new Element('#text');node.textContent=value;return node}};
const window={};
const localStorage={getItem(){return ''}};
const location={origin:'http://localhost',protocol:'http:'};
const fetchRequests=[];
const fetch=()=>new Promise(resolve=>fetchRequests.push(resolve));
"""
    assertions = r"""
(async()=>{
  byId('activityFilter').value='all';byId('sortSelect').value='seen';
  const base={id:12,status:'downloading',bytes_sent:512,total_bytes:1024,filename:'<img src=x onerror=alert(1)>.sqlite',ip_address:'192.0.2.7',started_at:'2026-09-30T10:00:00Z',updated_at:'2026-09-30T10:01:00Z'};
  const row=downloadRow(base);
  assert.match(row.textContent,/Transferred 512 B of 1 KiB · 50%/);
  assert.match(row.textContent,/192\.0\.2\.7/);
  assert.match(row.textContent,/<img src=x onerror=alert\(1\)>/);
  assert.equal(row.querySelector('img'),null);
  assert.equal(row.querySelector('progress').value,50);
  assert.match(row.querySelector('progress').attributes['aria-label'],/Database transfer/);
  const preparing=downloadRow({...base,status:'preparing',total_bytes:null,bytes_sent:0});
  assert.match(preparing.textContent,/Preparing file/);
  assert.equal(preparing.querySelector('progress').value,'');
  const empty=downloadRow({...base,status:'completed',total_bytes:0,bytes_sent:0});
  assert.match(empty.textContent,/Transferred 0 B of 0 B · 100%/);
  for(const [status,label] of [['interrupted','Interrupted'],['failed','Failed'],['completed','Completed']])assert.match(downloadRow({...base,status}).textContent,new RegExp(label));
  const first={id:'person-1',email:'first@example.test',access_status:'approved',is_online:false,database_downloads:{active_count:1,total_bytes:1536,completed_count:1,recent:[base,{...base,id:10,status:'completed'}]}};
  const second={id:'person-2',email:'second@example.test',access_status:'approved',is_online:true,database_downloads:{active_count:0,total_bytes:0,completed_count:0,recent:[]}};
  users=[first,second];unattributedDownloads={active_count:2,total_bytes:4096,completed_count:0,recent:[{...base,id:15,ip_address:'198.51.100.9'}]};
  renderUsers();renderKeyDownloads();updateSummary();
  assert.equal(byId('metricDownloading').textContent,'3');
  assert.equal(byId('keyDownloadsPanel').hidden,false);
  assert.match(byId('keyDownloadsList').textContent,/198\.51\.100\.9/);
  assert.doesNotMatch(byId('usersList').textContent,/198\.51\.100\.9/);
  assert.match(byId('usersList').textContent,/Total transferred, including partial downloads: 1.5 KiB/);
  const details=byId('usersList').querySelectorAll('details');details.forEach(detail=>detail.open=true);
  const control=byId('usersList').querySelector('[data-action]');control.focus();
  first.database_downloads.recent[0]={...base,bytes_sent:768};renderUsers();
  assert.equal(byId('usersList').querySelectorAll('details').every(detail=>detail.open),true);
  assert.equal(document.activeElement.dataset.action,control.dataset.action);
  assert.notEqual(document.activeElement,control);
  assert.match(byId('usersList').textContent,/75%/);
  byId('activityFilter').value='downloading';assert.deepEqual(filteredUsers().map(user=>user.id),['person-1']);
  byId('activityFilter').value='offline';assert.deepEqual(filteredUsers().map(user=>user.id),['person-1']);
  byId('activityFilter').value='online';assert.deepEqual(filteredUsers().map(user=>user.id),['person-2']);
  byId('activityFilter').value='all';
  applyLocale('ru');assert.match(byId('usersList').textContent,/Сейчас скачивает/);
  assert.match(byId('usersList').textContent,/Передано 768 B из 1 KiB · 75%/);
  const one=loadUsers({quiet:true}),two=loadUsers({quiet:true});
  assert.equal(one,two);assert.equal(fetchRequests.length,1);
  fetchRequests.shift()({ok:true,json:async()=>({users:[first,second],unattributed_downloads:unattributedDownloads,summary:{downloading:3}})});await one;
  assert.equal(loadingPromise,null);assert.equal(byId('refreshButton').disabled,false);
  document.visibilityState='hidden';refreshVisibleUsers();assert.equal(fetchRequests.length,0);
  document.visibilityState='visible';pendingAction={action:'block',user:first};refreshVisibleUsers();assert.equal(fetchRequests.length,0);
  pendingAction=null;byId('resetLinkDialog').open=true;refreshVisibleUsers();assert.equal(fetchRequests.length,0);
  byId('resetLinkDialog').open=false;refreshVisibleUsers();refreshVisibleUsers();assert.equal(fetchRequests.length,1);
  const pending=loadingPromise;fetchRequests.shift()({ok:true,json:async()=>({users:[],unattributed_downloads:{}})});await pending;
  assert.equal(byId('keyDownloadsPanel').hidden,true);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(
        [node], input=harness + program + assertions,
        text=True, encoding="utf-8", capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
