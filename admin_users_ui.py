"""Local-administrator user access console.

The page deliberately contains no authorization shortcuts or administrator
secrets.  Its server-side route must require both a local request and an
authenticated administrator before calling :func:`render_admin_users_page`.

API contract used by the browser:

* ``GET /api/admin/users`` returns ``{"users": [...], "summary": {...}}``.
* ``POST /api/admin/users`` accepts exactly ``action``, ``user_id`` and
  ``expected_revision``. Supported actions are ``approve``, ``block``,
  ``grant_admin``, ``revoke_admin``, ``revoke_sessions``,
  ``send_password_reset`` and ``delete``.
* User objects expose only sanitized account/activity metadata. Session
  cookies and password hashes must never be returned. The reset action may
  return a one-time ``reset_url``; the page keeps it only in a transient local
  dialog, never in the user list or browser storage.

The exported ``ADMIN_USERS_API_CONTRACT`` is intentionally small and stable so
the HTTP integration and its tests can share the same names without parsing
the HTML template.
"""

from __future__ import annotations

import json

ADMIN_USERS_API_CONTRACT = {
    "endpoint": "/api/admin/users",
    "actions": (
        "approve",
        "block",
        "grant_admin",
        "revoke_admin",
        "revoke_sessions",
        "send_password_reset",
        "delete",
    ),
    "required_post_fields": ("action", "user_id", "expected_revision"),
    "delete_confirmation_field": "confirmation_email",
    "access_states": ("pending", "approved", "blocked", "deleted"),
}


_COPY = {
    "ru": {
        "documentTitle": "Пользователи · CS2 Market",
        "language": "Язык",
        "dashboard": "Система",
        "deals": "Предложения",
        "inventory": "Инвентарь",
        "users": "Пользователи",
        "eyebrow": "Локальная админ-панель",
        "title": "Доступ пользователей",
        "lead": "По умолчанию у каждого нового аккаунта отказ в доступе: закрытые данные станут видны только после вашего одобрения. Здесь можно проверить активность и полностью отозвать доступ.",
        "localOnly": "Только localhost · администратор",
        "refresh": "Обновить",
        "refreshing": "Обновляем…",
        "loaded": "Данные обновлены",
        "total": "Всего аккаунтов",
        "pending": "Ждут решения",
        "approved": "Есть доступ",
        "blocked": "Доступ закрыт",
        "online": "Сейчас на сайте",
        "all": "Все",
        "deleted": "Удалены",
        "search": "Email, имя, IP, страна или ID",
        "searchLabel": "Поиск пользователей",
        "activity": "Активность",
        "allActivity": "Любая активность",
        "onlineOnly": "Только онлайн",
        "offlineOnly": "Только офлайн",
        "sort": "Сортировка",
        "recentlySeen": "Недавно активные",
        "newest": "Новые аккаунты",
        "oldest": "Старые аккаунты",
        "resultOne": "{count} пользователь",
        "resultFew": "{count} пользователя",
        "resultMany": "{count} пользователей",
        "emptyTitle": "Ничего не найдено",
        "emptyLead": "Попробуйте изменить поиск или фильтры.",
        "loadFailed": "Не удалось загрузить пользователей. Проверьте, что страница открыта на localhost под администратором.",
        "forbidden": "У этой учётной записи нет прав администратора.",
        "localRequired": "Управление пользователями доступно только через localhost.",
        "requestFailed": "Действие не выполнено. Обновите список и попробуйте ещё раз.",
        "conflict": "Данные пользователя уже изменились. Список обновлён — повторите действие.",
        "smtpMissing": "Письмо не отправлено: сначала настройте почтовый сервер.",
        "resetManualReady": "Почта недоступна: одноразовая ссылка открыта в защищённом локальном окне.",
        "statusPending": "Ожидает доступа",
        "statusApproved": "Разрешён",
        "statusBlocked": "Заблокирован",
        "statusDeleted": "Удалён",
        "admin": "Администратор",
        "member": "Пользователь",
        "currentAdmin": "Это вы",
        "onlineNow": "Активен за последние 5 минут",
        "offline": "Не в сети",
        "lastSeen": "Последняя активность",
        "lastLogin": "Последний вход",
        "neverLoggedIn": "Ещё не входил",
        "activeSessions": "Активные сессии",
        "allSessions": "Всего входов",
        "network": "Сеть и устройство",
        "currentIp": "Текущий IP",
        "lastIp": "Последний IP",
        "registrationIp": "IP регистрации",
        "country": "Страна (если доступна)",
        "countryHint": "Страна определяется приблизительно по доверенному proxy-заголовку или IP и может быть недоступна.",
        "userAgent": "Браузер / устройство",
        "unknown": "Не определено",
        "account": "Учётная запись",
        "created": "Создана",
        "approvedAt": "Доступ выдан",
        "blockedAt": "Доступ закрыт",
        "revision": "Версия",
        "security": "Безопасность",
        "failedLogins": "Неудачных входов",
        "passwordChanged": "Пароль изменён",
        "resetSent": "Сброс запрошен",
        "sessionsTitle": "Последние сессии",
        "sessionActive": "Активна",
        "sessionEnded": "Завершена",
        "sessionCreated": "Начало",
        "sessionExpires": "Истекает",
        "noSessions": "Сессий пока нет",
        "approve": "Открыть доступ",
        "block": "Закрыть доступ",
        "grantAdmin": "Выдать права администратора",
        "revokeAdmin": "Снять права администратора",
        "revokeSessions": "Завершить все сессии",
        "sendReset": "Отправить сброс пароля",
        "deleteAccount": "Удалить аккаунт",
        "actions": "Управление",
        "confirmTitle": "Подтвердите действие",
        "approveConfirm": "Пользователь сразу получит доступ к закрытым разделам сайта.",
        "blockConfirm": "Доступ будет закрыт, а все активные сессии пользователя — завершены.",
        "grantAdminConfirm": "Администратор получит доступ к системной панели и скачиванию базы данных. Все его текущие сессии будут завершены: потребуется войти заново.",
        "revokeAdminConfirm": "Доступ к системной панели и скачиванию базы данных будет снят. Все текущие сессии пользователя будут завершены.",
        "revokeConfirm": "Пользователь выйдет на всех устройствах. Доступ к аккаунту сохранится.",
        "resetConfirm": "На email пользователя будет отправлена одноразовая ссылка. Пароль и ссылка здесь не показываются.",
        "deleteConfirm": "Аккаунт будет удалён, доступ закрыт, а все сессии завершены. Для подтверждения введите email полностью:",
        "typeEmail": "Введите email пользователя",
        "cancel": "Отмена",
        "confirm": "Подтвердить",
        "working": "Выполняем…",
        "approveDone": "Доступ открыт.",
        "blockDone": "Доступ закрыт, активные сессии завершены.",
        "grant_adminDone": "Права администратора выданы. Пользователю нужно войти заново.",
        "revoke_adminDone": "Права администратора сняты, активные сессии завершены.",
        "revoke_sessionsDone": "Все сессии пользователя завершены.",
        "send_password_resetDone": "Запрос на сброс пароля отправлен.",
        "deleteDone": "Учётная запись удалена.",
        "autoRefresh": "Онлайн-статус обновляется автоматически каждые 20 секунд.",
        "noSecret": "Пароли, хеши и cookie не отображаются; одноразовая ссылка сброса показывается только сразу после запроса.",
        "resetLinkTitle": "Одноразовая ссылка сброса",
        "resetLinkLead": "Почта не доставила письмо. Передайте эту ссылку пользователю безопасным способом. После закрытия окна она исчезнет.",
        "resetLinkSensitive": "Ссылка даёт право сменить пароль — не публикуйте её и не отправляйте посторонним.",
        "copy": "Скопировать",
        "copied": "Скопировано",
        "close": "Закрыть",
    },
    "en": {
        "documentTitle": "Users · CS2 Market",
        "language": "Language",
        "dashboard": "System",
        "deals": "Deals",
        "inventory": "Inventory",
        "users": "Users",
        "eyebrow": "Local admin console",
        "title": "User access",
        "lead": "Every new account is denied by default: private data remains hidden until you approve access. Review activity and revoke access here.",
        "localOnly": "Localhost only · administrator",
        "refresh": "Refresh",
        "refreshing": "Refreshing…",
        "loaded": "Data refreshed",
        "total": "All accounts",
        "pending": "Awaiting decision",
        "approved": "Access granted",
        "blocked": "Access blocked",
        "online": "Online now",
        "all": "All",
        "deleted": "Deleted",
        "search": "Email, name, IP, country or ID",
        "searchLabel": "Search users",
        "activity": "Activity",
        "allActivity": "Any activity",
        "onlineOnly": "Online only",
        "offlineOnly": "Offline only",
        "sort": "Sort",
        "recentlySeen": "Recently active",
        "newest": "Newest accounts",
        "oldest": "Oldest accounts",
        "resultOne": "{count} user",
        "resultFew": "{count} users",
        "resultMany": "{count} users",
        "emptyTitle": "Nothing found",
        "emptyLead": "Try changing the search or filters.",
        "loadFailed": "Could not load users. Open this page on localhost while signed in as an administrator.",
        "forbidden": "This account does not have administrator permission.",
        "localRequired": "User management is available from localhost only.",
        "requestFailed": "The action failed. Refresh the list and try again.",
        "conflict": "This user changed since the page loaded. The list was refreshed; try again.",
        "smtpMissing": "Email was not sent: configure the mail server first.",
        "resetManualReady": "Email is unavailable: the one-time link is open in a protected local dialog.",
        "statusPending": "Awaiting access",
        "statusApproved": "Approved",
        "statusBlocked": "Blocked",
        "statusDeleted": "Deleted",
        "admin": "Administrator",
        "member": "Member",
        "currentAdmin": "This is you",
        "onlineNow": "Active in the last 5 minutes",
        "offline": "Offline",
        "lastSeen": "Last activity",
        "lastLogin": "Last sign-in",
        "neverLoggedIn": "Never signed in",
        "activeSessions": "Active sessions",
        "allSessions": "Total sign-ins",
        "network": "Network and device",
        "currentIp": "Current IP",
        "lastIp": "Last IP",
        "registrationIp": "Registration IP",
        "country": "Country (when available)",
        "countryHint": "Country is an estimate derived from a trusted proxy header or IP and may be unavailable.",
        "userAgent": "Browser / device",
        "unknown": "Unknown",
        "account": "Account",
        "created": "Created",
        "approvedAt": "Access granted",
        "blockedAt": "Access blocked",
        "revision": "Revision",
        "security": "Security",
        "failedLogins": "Failed sign-ins",
        "passwordChanged": "Password changed",
        "resetSent": "Reset requested",
        "sessionsTitle": "Recent sessions",
        "sessionActive": "Active",
        "sessionEnded": "Ended",
        "sessionCreated": "Started",
        "sessionExpires": "Expires",
        "noSessions": "No sessions yet",
        "approve": "Grant access",
        "block": "Block access",
        "grantAdmin": "Grant administrator",
        "revokeAdmin": "Remove administrator",
        "revokeSessions": "End all sessions",
        "sendReset": "Send password reset",
        "deleteAccount": "Delete account",
        "actions": "Management",
        "confirmTitle": "Confirm action",
        "approveConfirm": "The user will immediately gain access to private site sections.",
        "blockConfirm": "Access will be blocked and all active user sessions will be ended.",
        "grantAdminConfirm": "The administrator will gain access to the system dashboard and database download. All current sessions will be ended, so they must sign in again.",
        "revokeAdminConfirm": "System dashboard and database download access will be removed. All current user sessions will be ended.",
        "revokeConfirm": "The user will be signed out on every device. Account access remains approved.",
        "resetConfirm": "A one-time link will be sent to the user. The password and link are never shown here.",
        "deleteConfirm": "The account will be deleted, access blocked, and all sessions ended. Type the full email to confirm:",
        "typeEmail": "Type the user's email",
        "cancel": "Cancel",
        "confirm": "Confirm",
        "working": "Working…",
        "approveDone": "Access granted.",
        "blockDone": "Access blocked and active sessions ended.",
        "grant_adminDone": "Administrator permission granted. The user must sign in again.",
        "revoke_adminDone": "Administrator permission removed and active sessions ended.",
        "revoke_sessionsDone": "All user sessions ended.",
        "send_password_resetDone": "Password reset request sent.",
        "deleteDone": "Account deleted.",
        "autoRefresh": "Online status refreshes automatically every 20 seconds.",
        "noSecret": "Passwords, hashes, and cookies are hidden; a one-time reset link appears only immediately after a request.",
        "resetLinkTitle": "One-time reset link",
        "resetLinkLead": "Email delivery was unavailable. Share this link with the user through a safe channel. It disappears when this dialog closes.",
        "resetLinkSensitive": "This link grants permission to change the password. Do not publish it or share it with anyone else.",
        "copy": "Copy",
        "copied": "Copied",
        "close": "Close",
    },
}


_HTML = r"""<!doctype html>
<html lang="__LOCALE__">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root{color-scheme:dark;--bg:#080b12;--panel:#111724;--panel2:#151d2a;--line:rgba(255,255,255,.09);--text:#f4f7fb;--muted:#92a0b5;--accent:#a6ff6f;--cyan:#63d7ff;--warn:#ffc768;--bad:#ff7c88;--shadow:0 20px 55px rgba(0,0,0,.24)}
    *{box-sizing:border-box}html,body{max-width:100%;overflow-x:hidden}body{margin:0;color:var(--text);background:radial-gradient(circle at 7% -5%,rgba(99,215,255,.1),transparent 28%),var(--bg);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}a{color:inherit;text-decoration:none}button,input,select{font:inherit}.wrap{width:min(1440px,calc(100% - 34px));margin:0 auto}
    header{position:sticky;top:0;z-index:20;border-bottom:1px solid var(--line);background:rgba(8,11,18,.88);backdrop-filter:blur(18px)}.topbar{min-height:68px;display:flex;align-items:center;gap:22px}.brand{font-size:18px;font-weight:900;letter-spacing:-.035em;white-space:nowrap}nav{min-width:0;display:flex;gap:5px;flex:1;overflow:auto}nav a{padding:9px 12px;border-radius:10px;color:var(--muted);white-space:nowrap}nav a:hover,nav a.active{color:var(--text);background:rgba(255,255,255,.06)}.locale{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:11px;font-weight:750}.locale select{width:auto;padding:7px 8px;border:1px solid var(--line);border-radius:9px;color:var(--text);background:#0a0e16}
    main{padding:34px 0 76px}.hero{display:flex;align-items:flex-end;justify-content:space-between;gap:22px;margin-bottom:23px}.eyebrow{color:var(--accent);font-size:11px;font-weight:850;letter-spacing:.14em;text-transform:uppercase}h1{margin:8px 0 7px;font-size:clamp(34px,5vw,56px);line-height:1;letter-spacing:-.05em}.lead{max-width:760px;margin:0;color:var(--muted)}.hero-actions{display:flex;align-items:center;gap:9px;flex-wrap:wrap;justify-content:flex-end}.local-badge{padding:9px 11px;border:1px solid rgba(166,255,111,.22);border-radius:11px;color:var(--accent);background:rgba(166,255,111,.055);font-size:11px;font-weight:800}.button{min-height:40px;padding:0 14px;border:1px solid var(--line);border-radius:11px;color:var(--text);background:rgba(255,255,255,.04);font-weight:750;cursor:pointer}.button:hover{border-color:rgba(255,255,255,.2);background:rgba(255,255,255,.07)}.button:disabled{opacity:.48;cursor:not-allowed}.button.primary{border:0;color:#081009;background:linear-gradient(135deg,var(--accent),#64e8a1)}.button.danger{color:var(--bad);border-color:rgba(255,124,136,.25)}
    .notice{display:none;margin:0 0 16px;padding:12px 14px;border:1px solid var(--line);border-radius:12px;color:var(--muted);background:rgba(255,255,255,.025)}.notice:not(:empty){display:block}.notice[data-tone=good]{color:var(--accent);border-color:rgba(166,255,111,.24)}.notice[data-tone=bad]{color:var(--bad);border-color:rgba(255,124,136,.28)}
    .summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:11px;margin-bottom:17px}.metric{min-width:0;padding:16px;border:1px solid var(--line);border-radius:16px;background:linear-gradient(150deg,rgba(22,29,43,.97),rgba(14,19,29,.97));box-shadow:var(--shadow)}.metric span{display:block;color:var(--muted);font-size:11px}.metric strong{display:block;margin-top:7px;font-size:25px;line-height:1;letter-spacing:-.035em}.metric[data-kind=pending] strong{color:var(--warn)}.metric[data-kind=approved] strong,.metric[data-kind=online] strong{color:var(--accent)}.metric[data-kind=blocked] strong{color:var(--bad)}
    .panel{min-width:0;border:1px solid var(--line);border-radius:18px;background:linear-gradient(150deg,rgba(20,27,40,.97),rgba(13,18,28,.98));box-shadow:var(--shadow)}.toolbar{display:grid;grid-template-columns:minmax(220px,1fr) minmax(155px,190px) minmax(170px,210px);gap:9px;padding:15px;border-bottom:1px solid var(--line)}input,select{min-width:0;width:100%;padding:10px 11px;border:1px solid var(--line);border-radius:11px;outline:0;color:var(--text);background:rgba(5,8,14,.7)}input:focus,select:focus{border-color:rgba(99,215,255,.55);box-shadow:0 0 0 3px rgba(99,215,255,.08)}.tabs{display:flex;gap:6px;padding:0 15px 14px;overflow:auto;border-bottom:1px solid var(--line)}.tab{padding:7px 10px;border:1px solid var(--line);border-radius:999px;color:var(--muted);background:transparent;cursor:pointer;white-space:nowrap}.tab.active{color:#081009;border-color:transparent;background:var(--accent);font-weight:850}.list-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:13px 15px;color:var(--muted);font-size:11px}.hints{display:flex;gap:13px;flex-wrap:wrap;justify-content:flex-end}
    .users-list{display:grid;gap:9px;padding:0 15px 15px}.user-card{min-width:0;padding:15px;border:1px solid var(--line);border-radius:15px;background:rgba(5,8,14,.34)}.user-main{display:grid;grid-template-columns:minmax(220px,1.15fr) minmax(175px,.8fr) minmax(210px,1fr) minmax(210px,.95fr);gap:18px;align-items:start}.identity{min-width:0;display:grid;grid-template-columns:42px minmax(0,1fr);gap:10px}.avatar{width:42px;height:42px;display:grid;place-items:center;border-radius:12px;color:#071108;background:linear-gradient(135deg,var(--accent),#5edda2);font-weight:900}.identity h2{overflow-wrap:anywhere;margin:0 0 2px;font-size:15px;letter-spacing:-.015em}.email{overflow-wrap:anywhere;color:var(--muted);font-size:12px}.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}.chip{padding:3px 7px;border-radius:999px;color:var(--muted);background:rgba(255,255,255,.055);font-size:10px}.chip.status-pending{color:#211704;background:var(--warn);font-weight:850}.chip.status-approved,.chip.online{color:#071108;background:var(--accent);font-weight:850}.chip.status-blocked,.chip.status-deleted{color:#21070b;background:var(--bad);font-weight:850}.column{min-width:0}.column-title{display:block;margin-bottom:7px;color:var(--muted);font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.fact{min-width:0;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;padding:3px 0;font-size:11px}.fact span{color:var(--muted)}.fact strong{max-width:180px;overflow-wrap:anywhere;text-align:right;font-weight:700}.ua{display:-webkit-box;overflow:hidden;-webkit-box-orient:vertical;-webkit-line-clamp:2;margin-top:5px;color:var(--muted);font-size:10px;overflow-wrap:anywhere}.card-footer{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-top:13px;padding-top:12px;border-top:1px solid var(--line)}.activity-line{color:var(--muted);font-size:11px}.activity-line strong{color:var(--text)}.actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px}.actions .button{min-height:34px;padding:0 10px;font-size:10px}.sessions{margin-top:11px;border:1px solid var(--line);border-radius:11px;background:rgba(255,255,255,.015)}.sessions summary{padding:10px 11px;color:var(--muted);font-size:11px;font-weight:750;cursor:pointer}.session-list{display:grid;gap:0;padding:0 11px 10px}.session{display:grid;grid-template-columns:minmax(120px,.7fr) minmax(170px,1fr) minmax(180px,1.3fr);gap:10px;padding:8px 0;border-top:1px solid var(--line);font-size:10px}.session>*{min-width:0;overflow-wrap:anywhere}.session-state{font-weight:800}.session-state.active{color:var(--accent)}.empty{padding:48px 18px;text-align:center;color:var(--muted)}.empty strong{display:block;margin-bottom:4px;color:var(--text);font-size:15px}.skeleton{height:148px;border-radius:14px;background:linear-gradient(90deg,rgba(255,255,255,.025),rgba(255,255,255,.07),rgba(255,255,255,.025));background-size:220% 100%;animation:pulse 1.3s linear infinite}@keyframes pulse{to{background-position:-220% 0}}
    dialog{width:min(480px,calc(100% - 24px));padding:0;border:1px solid var(--line);border-radius:20px;color:var(--text);background:var(--panel2);box-shadow:0 30px 100px #000}dialog::backdrop{background:rgba(0,0,0,.72);backdrop-filter:blur(4px)}.dialog-body{padding:22px}.dialog-body h2{margin:0 0 8px;font-size:20px}.dialog-body p{margin:0 0 15px;color:var(--muted)}.confirm-field{display:grid;gap:6px;margin-bottom:15px;color:var(--muted);font-size:11px}.dialog-actions{display:flex;gap:8px;justify-content:flex-end}.dialog-actions .button{min-width:110px}.reset-link-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;margin:13px 0}.reset-link-row input{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.sensitive-note{padding:10px;border:1px solid rgba(255,199,104,.24);border-radius:10px;color:var(--warn)!important;background:rgba(255,199,104,.045);font-size:11px}
    @media(max-width:1120px){.summary{grid-template-columns:repeat(3,1fr)}.user-main{grid-template-columns:minmax(210px,1fr) minmax(180px,.85fr) minmax(210px,1fr)}.user-main>.column:last-child{grid-column:2/-1}.session{grid-template-columns:1fr 1fr}.session>*:last-child{grid-column:1/-1}}
    @media(max-width:760px){.wrap{width:min(100% - 20px,1440px)}.topbar{flex-wrap:wrap;gap:9px;padding:11px 0}.brand{font-size:15px}nav{order:3;width:100%;flex-basis:100%}.locale span{display:none}.hero{align-items:flex-start;flex-direction:column}.hero-actions{justify-content:flex-start}.summary{grid-template-columns:1fr 1fr}.toolbar{grid-template-columns:1fr 1fr}.toolbar input{grid-column:1/-1}.user-main{grid-template-columns:1fr 1fr}.user-main>.identity{grid-column:1/-1}.user-main>.column:last-child{grid-column:auto}.card-footer{align-items:flex-start;flex-direction:column}.actions{justify-content:flex-start}.hints{display:none}}
    @media(max-width:480px){.summary{grid-template-columns:1fr 1fr}.metric{padding:13px}.metric strong{font-size:21px}.toolbar{grid-template-columns:1fr}.toolbar input{grid-column:auto}.user-main{grid-template-columns:1fr}.user-main>.column:last-child,.user-main>.identity{grid-column:auto}.session{grid-template-columns:1fr}.session>*:last-child{grid-column:auto}.actions{width:100%}.actions .button{flex:1}.dialog-actions{flex-direction:column-reverse}.dialog-actions .button{width:100%}}
  </style>
</head>
<body>
  <header><div class="wrap topbar">
    <a class="brand" href="/">CS2 Market</a>
    <nav><a href="/dashboard" data-i18n="dashboard"></a><a href="/deals" data-i18n="deals"></a><a href="/account" data-i18n="inventory"></a><a class="active" href="/admin/users" data-i18n="users"></a></nav>
    <label class="locale"><span data-i18n="language"></span><select id="localeSelect" aria-label="Language"><option value="ru">RU</option><option value="en">EN</option></select></label>
  </div></header>
  <main class="wrap">
    <section class="hero"><div><span class="eyebrow" data-i18n="eyebrow"></span><h1 data-i18n="title"></h1><p class="lead" data-i18n="lead"></p></div><div class="hero-actions"><span class="local-badge" data-i18n="localOnly"></span><button class="button" id="refreshButton" type="button" data-i18n="refresh"></button></div></section>
    <p class="notice" id="notice" role="status" aria-live="polite"></p>
    <section class="summary" aria-label="User summary">
      <article class="metric"><span data-i18n="total"></span><strong id="metricTotal">—</strong></article>
      <article class="metric" data-kind="pending"><span data-i18n="pending"></span><strong id="metricPending">—</strong></article>
      <article class="metric" data-kind="approved"><span data-i18n="approved"></span><strong id="metricApproved">—</strong></article>
      <article class="metric" data-kind="blocked"><span data-i18n="blocked"></span><strong id="metricBlocked">—</strong></article>
      <article class="metric" data-kind="online"><span data-i18n="online"></span><strong id="metricOnline">—</strong></article>
    </section>
    <section class="panel">
      <div class="toolbar">
        <input id="searchInput" type="search" autocomplete="off" data-i18n-placeholder="search" data-i18n-aria-label="searchLabel">
        <select id="activityFilter" data-i18n-aria-label="activity"><option value="all" data-i18n="allActivity"></option><option value="online" data-i18n="onlineOnly"></option><option value="offline" data-i18n="offlineOnly"></option></select>
        <select id="sortSelect" data-i18n-aria-label="sort"><option value="seen" data-i18n="recentlySeen"></option><option value="newest" data-i18n="newest"></option><option value="oldest" data-i18n="oldest"></option></select>
      </div>
      <div class="tabs" id="statusTabs" role="tablist" aria-label="Access status">
        <button class="tab active" type="button" data-status="all" data-i18n="all"></button><button class="tab" type="button" data-status="pending" data-i18n="pending"></button><button class="tab" type="button" data-status="approved" data-i18n="approved"></button><button class="tab" type="button" data-status="blocked" data-i18n="blocked"></button><button class="tab" type="button" data-status="deleted" data-i18n="deleted"></button>
      </div>
      <div class="list-head"><strong id="resultCount">—</strong><div class="hints"><span data-i18n="autoRefresh"></span><span data-i18n="noSecret"></span></div></div>
      <div class="users-list" id="usersList" aria-live="polite"><div class="skeleton"></div><div class="skeleton"></div></div>
    </section>
  </main>
  <dialog id="confirmDialog" aria-labelledby="confirmTitle">
    <form class="dialog-body" method="dialog" id="confirmForm">
      <h2 id="confirmTitle" data-i18n="confirmTitle"></h2><p id="confirmMessage"></p>
      <label class="confirm-field" id="confirmField" hidden><span data-i18n="typeEmail"></span><input id="confirmInput" type="text" autocomplete="off" spellcheck="false"></label>
      <div class="dialog-actions"><button class="button" type="button" id="cancelConfirm" data-i18n="cancel"></button><button class="button primary" type="submit" id="submitConfirm" data-i18n="confirm"></button></div>
    </form>
  </dialog>
  <dialog id="resetLinkDialog" aria-labelledby="resetLinkTitle">
    <section class="dialog-body">
      <h2 id="resetLinkTitle" data-i18n="resetLinkTitle"></h2><p data-i18n="resetLinkLead"></p>
      <div class="reset-link-row"><input id="resetLinkValue" type="text" readonly aria-label="One-time password reset URL"><button class="button" id="copyResetLink" type="button" data-i18n="copy"></button></div>
      <p class="sensitive-note" data-i18n="resetLinkSensitive"></p>
      <div class="dialog-actions"><button class="button primary" id="closeResetLink" type="button" data-i18n="close"></button></div>
    </section>
  </dialog>
  <script>
    'use strict';
    const API_ENDPOINT='/api/admin/users';
    const COPY=__COPY__;
    const VALID_LOCALES=new Set(['ru','en']);
    const VALID_STATES=new Set(['pending','approved','blocked','deleted']);
    const ACTIONS=new Set(__ACTIONS__);
    const ONLINE_WINDOW_SECONDS=300;
    let locale='__LOCALE__';
    let users=[];
    let statusFilter='all';
    let pendingAction=null;
    let autoRefreshId=null;
    const byId=id=>document.getElementById(id);
    const tr=(key,values={})=>(COPY[locale]?.[key]??COPY.ru[key]??key).replace(/\{(\w+)\}/g,(_,name)=>String(values[name]??''));
    const safeString=(value,max=500)=>String(value??'').trim().slice(0,max);
    const safeNumber=value=>Number.isFinite(Number(value))?Number(value):0;
    function cookieValue(name){const prefix=`${name}=`;for(const part of document.cookie.split(';')){const value=part.trim();if(value.startsWith(prefix))return value.slice(prefix.length)}return ''}
    function storedLocale(){let cookie='';try{cookie=decodeURIComponent(cookieValue('cs2_locale'))}catch(_){}let local='';try{local=localStorage.getItem('cs2_locale')||''}catch(_){}const preferred=cookie||local||locale;return VALID_LOCALES.has(preferred)?preferred:'ru'}
    function persistLocale(value){document.cookie=`cs2_locale=${encodeURIComponent(value)}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol==='https:'?'; Secure':''}`;try{localStorage.setItem('cs2_locale',value)}catch(_){}}
    function applyLocale(value,persist=false){locale=VALID_LOCALES.has(value)?value:'ru';document.documentElement.lang=locale;document.title=tr('documentTitle');document.querySelectorAll('[data-i18n]').forEach(node=>{node.textContent=tr(node.dataset.i18n)});document.querySelectorAll('[data-i18n-placeholder]').forEach(node=>{node.placeholder=tr(node.dataset.i18nPlaceholder)});document.querySelectorAll('[data-i18n-aria-label]').forEach(node=>{node.setAttribute('aria-label',tr(node.dataset.i18nAriaLabel))});byId('localeSelect').value=locale;if(persist)persistLocale(locale);renderUsers()}
    function node(tag,className,textValue){const element=document.createElement(tag);if(className)element.className=className;if(textValue!==undefined)element.textContent=safeString(textValue);return element}
    function append(parent,...children){children.forEach(child=>parent.appendChild(child));return parent}
    function setNotice(message,tone=''){const target=byId('notice');target.textContent=message;target.dataset.tone=tone}
    function stateOf(user){const raw=safeString(user.access_status||user.access_state||user.status,30).toLowerCase();if(VALID_STATES.has(raw))return raw;if(user.deleted_at)return'deleted';if(user.disabled_at||user.blocked_at)return'blocked';if(user.access_granted===true)return'approved';return'pending'}
    function isOnline(user){if(user.is_online===true||user.online===true)return true;if(user.is_online===false||user.online===false)return false;const seen=dateValue(user.last_seen_at);if(!seen)return false;const age=(Date.now()-seen.getTime())/1000;return age>=-60&&age<=ONLINE_WINDOW_SECONDS}
    function dateValue(value){if(!value)return null;const date=new Date(value);return Number.isNaN(date.getTime())?null:date}
    function formatDate(value){const date=dateValue(value);return date?new Intl.DateTimeFormat(locale==='ru'?'ru-RU':'en-US',{dateStyle:'medium',timeStyle:'short'}).format(date):'—'}
    function formatCount(value){return new Intl.NumberFormat(locale==='ru'?'ru-RU':'en-US').format(Math.max(0,safeNumber(value)))}
    function resultLabel(count){if(locale==='en')return tr(count===1?'resultOne':'resultMany',{count});const mod10=count%10,mod100=count%100;const key=mod10===1&&mod100!==11?'resultOne':mod10>=2&&mod10<=4&&(mod100<12||mod100>14)?'resultFew':'resultMany';return tr(key,{count})}
    function countryOf(value){if(value&&typeof value==='object')return safeString(value.name||value.country_name||value.code||value.country_code,100)||tr('unknown');return safeString(value,100)||tr('unknown')}
    function userIp(user,kind){if(kind==='current')return safeString(user.current_ip||user.current_ip_address||user.ip_address,80);if(kind==='registration')return safeString(user.registration_ip||user.registration_ip_address,80);return safeString(user.last_ip||user.last_ip_address||user.ip_address,80)}
    function userCountry(user){return user.current_country||user.last_country||user.country||user.country_name||user.country_code}
    function fact(label,value){const row=node('div','fact');append(row,node('span','',label),node('strong','',value||'—'));return row}
    function column(title){const wrapper=node('div','column');append(wrapper,node('span','column-title',title));return wrapper}
    function statusChip(user){const state=stateOf(user);return node('span',`chip status-${state}`,tr(`status${state[0].toUpperCase()}${state.slice(1)}`))}
    function identityBlock(user){const wrapper=node('div','identity');const name=safeString(user.display_name||user.name||user.email||user.id,180)||'—';const avatar=node('span','avatar',name.slice(0,1).toUpperCase());const copy=node('div','');append(copy,node('h2','',name),node('div','email',safeString(user.email,254)||'—'));const chips=node('div','chips');append(chips,statusChip(user),node('span','chip',user.role==='admin'?tr('admin'):tr('member')));if(user.is_self===true)append(chips,node('span','chip',tr('currentAdmin')));append(copy,chips);append(wrapper,avatar,copy);return wrapper}
    function activityColumn(user){const wrapper=column(tr('activity'));const active=safeNumber(user.active_sessions??user.active_session_count);const total=safeNumber(user.total_sessions??user.session_count??user.login_count);append(wrapper,fact(isOnline(user)?tr('onlineNow'):tr('offline'),isOnline(user)?'●':'—'),fact(tr('lastSeen'),formatDate(user.last_seen_at)),fact(tr('lastLogin'),user.last_login_at?formatDate(user.last_login_at):tr('neverLoggedIn')),fact(tr('activeSessions'),formatCount(active)),fact(tr('allSessions'),formatCount(total)));return wrapper}
    function networkColumn(user){const wrapper=column(tr('network'));const currentIp=userIp(user,'current'),lastIp=userIp(user,'last');append(wrapper,fact(tr('currentIp'),currentIp||'—'),fact(tr('lastIp'),lastIp||currentIp||'—'),fact(tr('registrationIp'),userIp(user,'registration')||'—'),fact(tr('country'),countryOf(userCountry(user))));const agent=safeString(user.current_user_agent||user.last_user_agent||user.user_agent,500)||tr('unknown');const ua=node('div','ua',agent);ua.title=agent;const hint=node('div','ua',tr('countryHint'));append(wrapper,ua,hint);return wrapper}
    function accountColumn(user){const wrapper=column(tr('account'));append(wrapper,fact('ID',safeString(user.id,100)||'—'),fact(tr('created'),formatDate(user.created_at)),fact(tr('approvedAt'),formatDate(user.approved_at||user.access_granted_at)),fact(tr('blockedAt'),formatDate(user.blocked_at||user.disabled_at)),fact(tr('passwordChanged'),formatDate(user.password_changed_at)),fact(tr('resetSent'),formatDate(user.password_reset_sent_at)),fact(tr('revision'),formatCount(user.revision)));return wrapper}
    function sessionDetails(user){const sessions=Array.isArray(user.recent_sessions)?user.recent_sessions:Array.isArray(user.sessions)?user.sessions:[];const details=node('details','sessions');const summary=node('summary','',`${tr('sessionsTitle')} · ${sessions.length}`);append(details,summary);const list=node('div','session-list');if(!sessions.length){append(list,node('div','activity-line',tr('noSessions')))}else sessions.slice(0,8).forEach(session=>{const row=node('div','session');const active=session.is_active===true&&!session.revoked_at;append(row,node('span',`session-state${active?' active':''}`,tr(active?'sessionActive':'sessionEnded')),node('span','',`${safeString(session.ip||session.ip_address,80)||'—'} · ${countryOf(session.country||session.country_name||session.country_code)}`),node('span','',`${tr('sessionCreated')}: ${formatDate(session.created_at)} · ${tr('lastSeen')}: ${formatDate(session.last_seen_at)} · ${tr('sessionExpires')}: ${formatDate(session.expires_at)} · ${safeString(session.user_agent,280)||tr('unknown')}`));append(list,row)});append(details,list);return details}
    function actionButton(action,label,user,tone=''){const button=node('button',`button${tone?` ${tone}`:''}`,label);button.type='button';button.dataset.action=action;button.disabled=user.can_manage===false||(user.is_self===true&&(action==='block'||action==='delete'));button.addEventListener('click',()=>openConfirmation(action,user));return button}
    function actionsBlock(user){const wrapper=node('div','actions');const state=stateOf(user);if(state==='deleted')return wrapper;if(state!=='approved')append(wrapper,actionButton('approve',tr('approve'),user,'primary'));if(state==='approved')append(wrapper,actionButton('block',tr('block'),user,'danger'));if(user.role==='admin')append(wrapper,actionButton('revoke_admin',tr('revokeAdmin'),user,'danger'));else if(state==='approved')append(wrapper,actionButton('grant_admin',tr('grantAdmin'),user));append(wrapper,actionButton('revoke_sessions',tr('revokeSessions'),user),actionButton('send_password_reset',tr('sendReset'),user),actionButton('delete',tr('deleteAccount'),user,'danger'));return wrapper}
    function userCard(user){const card=node('article','user-card');card.dataset.userId=safeString(user.id,100);const main=node('div','user-main');append(main,identityBlock(user),activityColumn(user),networkColumn(user),accountColumn(user));const footer=node('div','card-footer');const activity=node('div','activity-line');const label=node('strong','',isOnline(user)?tr('onlineNow'):tr('lastSeen'));append(activity,label,document.createTextNode(` · ${formatDate(user.last_seen_at)}`));append(footer,activity,actionsBlock(user));append(card,main,sessionDetails(user),footer);return card}
    function searchable(user){return [user.id,user.email,user.display_name,user.name,userIp(user,'current'),userIp(user,'last'),userIp(user,'registration'),countryOf(userCountry(user)),user.current_user_agent,user.last_user_agent].map(value=>safeString(value,500).toLocaleLowerCase(locale==='ru'?'ru-RU':'en-US')).join(' ')}
    function filteredUsers(){const query=safeString(byId('searchInput').value,300).toLocaleLowerCase(locale==='ru'?'ru-RU':'en-US');const activity=byId('activityFilter').value;const sort=byId('sortSelect').value;const result=users.filter(user=>(statusFilter==='all'||stateOf(user)===statusFilter)&&(!query||searchable(user).includes(query))&&(activity==='all'||(activity==='online')===isOnline(user)));const timestamp=(user,key)=>dateValue(user[key])?.getTime()||0;result.sort((a,b)=>sort==='oldest'?timestamp(a,'created_at')-timestamp(b,'created_at'):sort==='newest'?timestamp(b,'created_at')-timestamp(a,'created_at'):timestamp(b,'last_seen_at')-timestamp(a,'last_seen_at'));return result}
    function renderUsers(){if(!byId('usersList'))return;const target=byId('usersList');target.replaceChildren();const result=filteredUsers();byId('resultCount').textContent=resultLabel(result.length);if(!result.length){const empty=node('div','empty');append(empty,node('strong','',tr('emptyTitle')),node('span','',tr('emptyLead')));append(target,empty);return}result.forEach(user=>append(target,userCard(user)))}
    function updateSummary(serverSummary={}){const counts={total:users.length,pending:0,approved:0,blocked:0,online:0};users.forEach(user=>{const state=stateOf(user);if(Object.hasOwn(counts,state))counts[state]+=1;if(isOnline(user))counts.online+=1});for(const key of Object.keys(counts)){const serverValue=serverSummary[key]??serverSummary[`${key}_count`];const value=Number.isFinite(Number(serverValue))?Number(serverValue):counts[key];const target=byId(`metric${key[0].toUpperCase()}${key.slice(1)}`);if(target)target.textContent=formatCount(value)}}
    function safeError(payload,status){const code=safeString(payload?.error_code||payload?.code,80).toLowerCase();if(status===403)return code==='local_only'||code==='local_required'?tr('localRequired'):tr('forbidden');if(status===409)return tr('conflict');if(code==='smtp_not_configured')return tr('smtpMissing');return tr('requestFailed')}
    async function loadUsers({quiet=false}={}){const refresh=byId('refreshButton');refresh.disabled=true;if(!quiet)refresh.textContent=tr('refreshing');try{const response=await fetch(API_ENDPOINT,{credentials:'same-origin',headers:{Accept:'application/json','X-CS2-Action':'admin-users-read'}});const payload=await response.json().catch(()=>({}));if(!response.ok)throw Object.assign(new Error(safeError(payload,response.status)),{safe:true,status:response.status});users=Array.isArray(payload.users)?payload.users.filter(user=>user&&typeof user==='object'):[];updateSummary(payload.summary&&typeof payload.summary==='object'?payload.summary:{});renderUsers();if(!quiet)setNotice(tr('loaded'),'good')}catch(error){setNotice(error?.safe?error.message:tr('loadFailed'),'bad');if(!users.length){users=[];updateSummary();renderUsers()}}finally{refresh.disabled=false;refresh.textContent=tr('refresh')}}
    function openConfirmation(action,user){if(!ACTIONS.has(action)||!user)return;pendingAction={action,user};const messages={approve:'approveConfirm',block:'blockConfirm',grant_admin:'grantAdminConfirm',revoke_admin:'revokeAdminConfirm',revoke_sessions:'revokeConfirm',send_password_reset:'resetConfirm',delete:'deleteConfirm'};byId('confirmMessage').textContent=tr(messages[action]);const isDelete=action==='delete';byId('confirmField').hidden=!isDelete;byId('confirmInput').value='';byId('confirmInput').placeholder=safeString(user.email,254);byId('submitConfirm').disabled=isDelete;byId('submitConfirm').className=`button ${action==='delete'||action==='block'||action==='revoke_admin'?'danger':'primary'}`;byId('confirmDialog').showModal();if(isDelete)byId('confirmInput').focus()}
    function closeConfirmation(){pendingAction=null;if(byId('confirmDialog').open)byId('confirmDialog').close()}
    function safeResetUrl(value){const raw=safeString(value,2200);if(!raw)return'';try{const parsed=new URL(raw,location.origin);return parsed.protocol==='https:'||parsed.protocol==='http:'?parsed.href:''}catch(_){return''}}
    function showResetLink(value){const resetUrl=safeResetUrl(value);if(!resetUrl)return false;byId('resetLinkValue').value=resetUrl;byId('copyResetLink').textContent=tr('copy');byId('resetLinkDialog').showModal();return true}
    function closeResetLink(){byId('resetLinkValue').value='';if(byId('resetLinkDialog').open)byId('resetLinkDialog').close()}
    async function copyResetLink(){const value=byId('resetLinkValue').value;if(!value)return;let copied=false;try{await navigator.clipboard.writeText(value);copied=true}catch(_){byId('resetLinkValue').select();copied=document.execCommand('copy')}if(copied)byId('copyResetLink').textContent=tr('copied')}
    function mutationHeaders(){const headers={'Content-Type':'application/json','Accept':'application/json','X-CS2-Action':'admin-user-management'};let csrf='';try{csrf=decodeURIComponent(cookieValue('cs2_csrf'))}catch(_){}if(csrf)headers['X-CSRF-Token']=csrf;return headers}
    async function performAction(){if(!pendingAction)return;const {action,user}=pendingAction;const button=byId('submitConfirm');button.disabled=true;button.textContent=tr('working');const requestBody={action,user_id:user.id,expected_revision:safeNumber(user.revision)};if(action==='delete')requestBody.confirmation_email=safeString(byId('confirmInput').value,254);try{const response=await fetch(API_ENDPOINT,{method:'POST',credentials:'same-origin',headers:mutationHeaders(),body:JSON.stringify(requestBody)});const payload=await response.json().catch(()=>({}));if(!response.ok)throw Object.assign(new Error(safeError(payload,response.status)),{safe:true,status:response.status});closeConfirmation();const manualReset=action==='send_password_reset'&&showResetLink(payload.reset_url);const deliveryUnavailable=payload.delivery_configured===false||payload.email_sent===false||payload.delivery_status==='unavailable';setNotice(manualReset?tr('resetManualReady'):deliveryUnavailable?tr('smtpMissing'):tr(`${action}Done`),manualReset?'good':deliveryUnavailable?'bad':'good');await loadUsers({quiet:true})}catch(error){closeConfirmation();setNotice(error?.safe?error.message:tr('requestFailed'),'bad');if(error?.status===409)await loadUsers({quiet:true})}finally{button.textContent=tr('confirm');button.disabled=false}}
    byId('localeSelect').addEventListener('change',event=>applyLocale(event.target.value,true));
    byId('refreshButton').addEventListener('click',()=>loadUsers());
    byId('searchInput').addEventListener('input',renderUsers);
    byId('activityFilter').addEventListener('change',renderUsers);
    byId('sortSelect').addEventListener('change',renderUsers);
    byId('statusTabs').addEventListener('click',event=>{const button=event.target.closest('[data-status]');if(!button)return;statusFilter=button.dataset.status;document.querySelectorAll('[data-status]').forEach(tab=>tab.classList.toggle('active',tab===button));renderUsers()});
    byId('confirmInput').addEventListener('input',event=>{if(pendingAction?.action!=='delete')return;const expected=safeString(pendingAction.user.email,254).toLocaleLowerCase();byId('submitConfirm').disabled=safeString(event.target.value,254).toLocaleLowerCase()!==expected});
    byId('cancelConfirm').addEventListener('click',closeConfirmation);
    byId('copyResetLink').addEventListener('click',copyResetLink);
    byId('closeResetLink').addEventListener('click',closeResetLink);
    byId('confirmForm').addEventListener('submit',event=>{event.preventDefault();performAction()});
    byId('confirmDialog').addEventListener('cancel',event=>{event.preventDefault();closeConfirmation()});
    byId('resetLinkDialog').addEventListener('cancel',event=>{event.preventDefault();closeResetLink()});
    locale=storedLocale();applyLocale(locale);loadUsers();autoRefreshId=window.setInterval(()=>{if(document.visibilityState==='visible'&&!pendingAction&&!byId('resetLinkDialog').open)loadUsers({quiet:true})},20000);window.addEventListener('pagehide',()=>{byId('resetLinkValue').value='';window.clearInterval(autoRefreshId)},{once:true});
  </script>
</body>
</html>
"""


def _normalise_locale(locale: str) -> str:
    """Return one of the two locales supported by the standalone page."""

    return locale if locale in _COPY else "ru"


def render_admin_users_page(locale: str = "ru") -> str:
    """Render the local-admin user access console in Russian or English.

    Authorization is intentionally not accepted as an argument: the HTTP
    handler must enforce localhost + administrator access before rendering.
    """

    selected = _normalise_locale(locale)
    return (
        _HTML.replace("__LOCALE__", selected)
        .replace("__TITLE__", _COPY[selected]["documentTitle"])
        .replace(
            "__COPY__",
            json.dumps(_COPY, ensure_ascii=False, separators=(",", ":")),
        )
        .replace(
            "__ACTIONS__",
            json.dumps(ADMIN_USERS_API_CONTRACT["actions"], separators=(",", ":")),
        )
    )


__all__ = ["ADMIN_USERS_API_CONTRACT", "render_admin_users_page"]
