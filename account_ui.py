"""HTML views for user authentication and the personal inventory workspace."""


AUTH_HTML = r"""<!doctype html>
<html lang="__LOCALE__">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>CSSkinsPrediction · Аккаунт</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #080b12;
      --panel: rgba(18, 23, 35, .94);
      --line: rgba(255,255,255,.1);
      --text: #f5f7fb;
      --muted: #98a3b7;
      --accent: #a6ff6f;
      --accent-2: #63d7ff;
      --bad: #ff7b88;
      --shadow: 0 28px 80px rgba(0,0,0,.38);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font: 15px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at 12% 12%, rgba(99,215,255,.14), transparent 34%),
        radial-gradient(circle at 88% 84%, rgba(166,255,111,.12), transparent 32%),
        var(--bg);
    }
    a { color: inherit; }
    .shell { width: min(1120px, calc(100% - 32px)); margin: 0 auto; }
    header { padding: 22px 0; }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    .brand { display: flex; align-items: center; gap: 11px; font-weight: 850; letter-spacing: -.02em; text-decoration: none; }
    .brand-mark { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 11px; color: #08100a; background: linear-gradient(135deg, var(--accent), #5df0a8); box-shadow: 0 0 28px rgba(166,255,111,.22); }
    .header-actions { display: flex; align-items: center; gap: 12px; }
    .back { color: var(--muted); text-decoration: none; }
    .back:hover { color: var(--text); }
    .locale-control { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 12px; font-weight: 700; }
    .locale-control select { width: auto; min-width: 64px; padding: 7px 9px; border: 1px solid var(--line); border-radius: 10px; color: var(--text); background: rgba(4,7,13,.82); font: inherit; cursor: pointer; }
    main { min-height: calc(100vh - 86px); display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(340px, .72fr); align-items: center; gap: clamp(36px, 8vw, 110px); padding: 34px 0 90px; }
    .eyebrow { color: var(--accent); font-size: 12px; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; }
    h1 { margin: 14px 0 18px; max-width: 720px; font-size: clamp(42px, 6vw, 76px); line-height: .98; letter-spacing: -.055em; }
    .lead { max-width: 620px; color: var(--muted); font-size: clamp(16px, 2vw, 19px); }
    .benefits { display: grid; gap: 12px; margin-top: 30px; padding: 0; list-style: none; }
    .benefits li { display: flex; align-items: center; gap: 11px; }
    .check { width: 25px; height: 25px; display: grid; place-items: center; flex: 0 0 auto; border: 1px solid rgba(166,255,111,.35); border-radius: 50%; color: var(--accent); background: rgba(166,255,111,.07); }
    .card { padding: clamp(24px, 4vw, 38px); border: 1px solid var(--line); border-radius: 26px; background: var(--panel); box-shadow: var(--shadow); backdrop-filter: blur(18px); }
    .card h2 { margin: 0 0 7px; font-size: 29px; letter-spacing: -.035em; }
    .sub { margin: 0 0 25px; color: var(--muted); }
    form { display: none; gap: 15px; }
    body[data-mode="login"] form[data-form="login"],
    body[data-mode="register"] form[data-form="register"],
    body[data-mode="forgot"] form[data-form="forgot"],
    body[data-mode="reset"] form[data-form="reset"] { display: grid; }
    label { display: grid; gap: 7px; color: #cbd3e2; font-size: 13px; font-weight: 700; }
    input {
      width: 100%; padding: 13px 14px; border: 1px solid var(--line); border-radius: 13px;
      outline: none; color: var(--text); background: rgba(4,7,13,.7); font: inherit;
    }
    input:focus { border-color: rgba(99,215,255,.65); box-shadow: 0 0 0 3px rgba(99,215,255,.1); }
    .password-row { display: flex; justify-content: space-between; gap: 12px; }
    .password-row a { color: var(--accent-2); font-weight: 600; text-decoration: none; }
    button {
      min-height: 47px; border: 0; border-radius: 13px; cursor: pointer; color: #07100a;
      background: linear-gradient(135deg, var(--accent), #67eba5); font: 800 15px/1 inherit;
      box-shadow: 0 12px 28px rgba(119,245,141,.15);
    }
    button:disabled { opacity: .62; cursor: wait; }
    .feedback { min-height: 21px; margin: 0; color: var(--muted); font-size: 13px; }
    .feedback[data-tone="bad"] { color: var(--bad); }
    .feedback[data-tone="good"] { color: var(--accent); }
    .feedback[data-tone="warn"] { color: #ffc768; }
    .switch { margin: 22px 0 0; color: var(--muted); text-align: center; }
    .switch a { color: var(--text); font-weight: 750; text-decoration: none; }
    .mode-only { display: none; }
    body[data-mode="login"] .login-only,
    body[data-mode="register"] .register-only,
    body[data-mode="forgot"] .forgot-only,
    body[data-mode="reset"] .reset-only { display: block; }
    .security-note { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }
    @media (max-width: 820px) {
      main { grid-template-columns: 1fr; padding-top: 20px; }
      .intro { text-align: center; }
      .benefits { width: max-content; max-width: 100%; margin-inline: auto; text-align: left; }
      h1 { margin-inline: auto; }
      .lead { margin-inline: auto; }
    }
    @media (max-width: 480px) { .shell { width: min(100% - 20px, 1120px); } .header-actions { gap: 8px; } .locale-control span { display: none; } .back { font-size: 13px; } .card { border-radius: 20px; } }
  </style>
</head>
<body data-mode="__AUTH_MODE__">
  <header>
    <div class="shell topbar">
      <a class="brand" href="/"><span class="brand-mark">CS</span><span>CSSkinsPrediction</span></a>
      <div class="header-actions">
        <label class="locale-control"><span data-i18n="language">Язык</span><select id="localeSelect" data-i18n-aria-label="language"><option value="ru">RU</option><option value="en">EN</option></select></label>
        <a class="back" href="/" data-i18n="backToSite">← Вернуться на сайт</a>
      </div>
    </div>
  </header>
  <main class="shell">
    <section class="intro">
      <span class="eyebrow" data-i18n="heroEyebrow">Личный кабинет трейдера</span>
      <h1 data-i18n="heroTitle">Все покупки и цены под контролем.</h1>
      <p class="lead" data-i18n="heroLead">Записывай, что и за сколько купил, следи за прибылью после комиссии и вовремя меняй цену зависшего лота.</p>
      <ul class="benefits">
        <li><span class="check">✓</span><span data-i18n="benefitInventory">Инвентарь, покупки, выставленные и проданные предметы</span></li>
        <li><span class="check">✓</span><span data-i18n="benefitPricing">Историческая цена, прогноз и честный безубыток</span></li>
        <li><span class="check">✓</span><span data-i18n="benefitRecovery">Восстановление доступа через одноразовую ссылку</span></li>
      </ul>
    </section>
    <section class="card">
      <div class="mode-only login-only"><h2 data-i18n="loginTitle">С возвращением</h2><p class="sub" data-i18n="loginSubtitle">Войди, чтобы открыть свой инвентарь.</p></div>
      <div class="mode-only register-only"><h2 data-i18n="registerTitle">Создать аккаунт</h2><p class="sub" data-i18n="registerSubtitle">После регистрации владелец сайта должен одобрить доступ.</p></div>
      <div class="mode-only forgot-only"><h2 data-i18n="forgotTitle">Восстановить пароль</h2><p class="sub" data-i18n="forgotSubtitle">Пришлём одноразовую ссылку, если аккаунт существует.</p></div>
      <div class="mode-only reset-only"><h2 data-i18n="resetTitle">Новый пароль</h2><p class="sub" data-i18n="resetSubtitle">Ссылка одноразовая и действует ограниченное время.</p></div>

      <form data-form="login" autocomplete="on">
        <label><span data-i18n="email">Электронная почта</span><input name="email" type="email" maxlength="254" autocomplete="email" required></label>
        <label><span class="password-row"><span data-i18n="password">Пароль</span><a href="/forgot-password" data-i18n="forgotLink">Забыли?</a></span><input name="password" type="password" maxlength="256" autocomplete="current-password" required></label>
        <button type="submit" data-i18n="loginButton">Войти</button><p class="feedback" role="status" aria-live="polite"></p>
      </form>
      <form data-form="register" autocomplete="on">
        <label><span data-i18n="displayName">Как вас называть</span><input name="display_name" maxlength="60" autocomplete="name" data-i18n-placeholder="displayNamePlaceholder" placeholder="Например, Almaz" required></label>
        <label><span data-i18n="email">Электронная почта</span><input name="email" type="email" maxlength="254" autocomplete="email" required></label>
        <label><span data-i18n="password">Пароль</span><input name="password" type="password" minlength="10" maxlength="256" autocomplete="new-password" required></label>
        <label><span data-i18n="passwordConfirm">Повторите пароль</span><input name="password_confirm" type="password" minlength="10" maxlength="256" autocomplete="new-password" required></label>
        <button type="submit" data-i18n="registerButton">Создать аккаунт</button><p class="feedback" role="status" aria-live="polite"></p>
      </form>
      <form data-form="forgot" autocomplete="on">
        <label><span data-i18n="accountEmail">Электронная почта аккаунта</span><input name="email" type="email" maxlength="254" autocomplete="email" required></label>
        <button type="submit" data-i18n="forgotButton">Отправить ссылку</button><p class="feedback" role="status" aria-live="polite"></p>
      </form>
      <form data-form="reset" autocomplete="on">
        <label><span data-i18n="newPassword">Новый пароль</span><input name="password" type="password" minlength="10" maxlength="256" autocomplete="new-password" required></label>
        <label><span data-i18n="passwordConfirm">Повторите пароль</span><input name="password_confirm" type="password" minlength="10" maxlength="256" autocomplete="new-password" required></label>
        <button type="submit" data-i18n="resetButton">Сменить пароль</button><p class="feedback" role="status" aria-live="polite"></p>
      </form>

      <p class="switch mode-only login-only"><span data-i18n="noAccount">Нет аккаунта?</span> <a href="/register" data-i18n="registerLink">Зарегистрироваться</a></p>
      <p class="switch mode-only register-only"><span data-i18n="hasAccount">Уже есть аккаунт?</span> <a href="/login" data-i18n="loginLink">Войти</a></p>
      <p class="switch mode-only forgot-only reset-only"><a href="/login" data-i18n="backToLogin">Вернуться ко входу</a></p>
      <p class="security-note" data-i18n="securityNote">Пароль не сохраняется в открытом виде. После сброса все прежние сессии будут завершены.</p>
    </section>
  </main>
  <script>
    const I18N = {
      ru: {
        documentTitle:'CSSkinsPrediction · Аккаунт', language:'Язык', backToSite:'← Вернуться на сайт', heroEyebrow:'Личный кабинет трейдера',
        heroTitle:'Все покупки и цены под контролем.', heroLead:'Записывай, что и за сколько купил, следи за прибылью после комиссии и вовремя меняй цену зависшего лота.',
        benefitInventory:'Инвентарь, покупки, выставленные и проданные предметы', benefitPricing:'Историческая цена, прогноз и честный безубыток', benefitRecovery:'Восстановление доступа через одноразовую ссылку',
        loginTitle:'С возвращением', loginSubtitle:'Войди, чтобы открыть свой инвентарь.', registerTitle:'Создать аккаунт', registerSubtitle:'После регистрации владелец сайта должен одобрить доступ.',
        forgotTitle:'Восстановить пароль', forgotSubtitle:'Пришлём одноразовую ссылку, если аккаунт существует.', resetTitle:'Новый пароль', resetSubtitle:'Ссылка одноразовая и действует ограниченное время.',
        email:'Электронная почта', accountEmail:'Электронная почта аккаунта', password:'Пароль', newPassword:'Новый пароль', passwordConfirm:'Повторите пароль', forgotLink:'Забыли?',
        displayName:'Как вас называть', displayNamePlaceholder:'Например, Almaz', loginButton:'Войти', registerButton:'Создать аккаунт', forgotButton:'Отправить ссылку', resetButton:'Сменить пароль',
        noAccount:'Нет аккаунта?', registerLink:'Зарегистрироваться', hasAccount:'Уже есть аккаунт?', loginLink:'Войти', backToLogin:'Вернуться ко входу',
        securityNote:'Пароль не сохраняется в открытом виде. После сброса все прежние сессии будут завершены.',
        loginPending:'Входим…', registerPending:'Создаём…', forgotPending:'Отправляем…', resetPending:'Сохраняем…', passwordsMismatch:'Пароли не совпадают.',
        requestFailed:'Не удалось выполнить запрос.', done:'Готово.', resetSuccess:'Пароль сброшен. Войдите с новым паролем.', passwordChanged:'Пароль изменён. Войдите снова.',
        forgotAccepted:'Если аккаунт существует, ссылка отправлена на почту.', smtpMissing:'Запрос принят, но владелец сайта ещё не настроил отправку почты.',
        invalidCredentials:'Неверная почта или пароль.', invalidEmail:'Введите корректный адрес электронной почты.', passwordLength:'Пароль должен содержать от 10 до 256 символов.',
        duplicateEmail:'Аккаунт с этой почтой уже существует.', invalidResetToken:'Ссылка для сброса недействительна или истекла.', tooManyAttempts:'Слишком много попыток. Попробуйте позже.', validationError:'Проверьте введённые данные.', accessDenied:'Запрос отклонён. Обновите страницу и повторите попытку.', registrationDisabled:'Регистрация отключена.', serviceUnavailable:'Сервис аккаунтов временно недоступен.',
        registrationPendingApproval:'Аккаунт создан. Доступ откроется только после одобрения владельца сайта.', accessPending:'Аккаунт ожидает одобрения владельца сайта.', accessBlocked:'Владелец сайта закрыл доступ для этого аккаунта.', accountDeleted:'Эта учётная запись удалена.'
      },
      en: {
        documentTitle:'CSSkinsPrediction · Account', language:'Language', backToSite:'← Back to site', heroEyebrow:'Trader account',
        heroTitle:'Every purchase and price under control.', heroLead:'Record what you bought and for how much, track profit after fees, and reprice stale listings in time.',
        benefitInventory:'Inventory, purchases, listed and sold items', benefitPricing:'Historical prices, forecasts, and true break-even', benefitRecovery:'Account recovery through a one-time link',
        loginTitle:'Welcome back', loginSubtitle:'Sign in to open your inventory.', registerTitle:'Create an account', registerSubtitle:'After registration, the site owner must approve access.',
        forgotTitle:'Reset your password', forgotSubtitle:'We will send a one-time link if the account exists.', resetTitle:'New password', resetSubtitle:'The link is single-use and expires after a limited time.',
        email:'Email', accountEmail:'Account email', password:'Password', newPassword:'New password', passwordConfirm:'Confirm password', forgotLink:'Forgot?',
        displayName:'Display name', displayNamePlaceholder:'For example, Almaz', loginButton:'Sign in', registerButton:'Create account', forgotButton:'Send reset link', resetButton:'Change password',
        noAccount:'No account?', registerLink:'Register', hasAccount:'Already have an account?', loginLink:'Sign in', backToLogin:'Back to sign in',
        securityNote:'Your password is never stored in plain text. A reset signs out all previous sessions.',
        loginPending:'Signing in…', registerPending:'Creating…', forgotPending:'Sending…', resetPending:'Saving…', passwordsMismatch:'Passwords do not match.',
        requestFailed:'The request could not be completed.', done:'Done.', resetSuccess:'Password reset. Sign in with your new password.', passwordChanged:'Password changed. Sign in again.',
        forgotAccepted:'If the account exists, a reset link has been sent.', smtpMissing:'The request was accepted, but email delivery has not been configured by the site owner.',
        invalidCredentials:'Invalid email or password.', invalidEmail:'Enter a valid email address.', passwordLength:'Password must contain between 10 and 256 characters.',
        duplicateEmail:'An account with this email already exists.', invalidResetToken:'The reset link is invalid or has expired.', tooManyAttempts:'Too many attempts. Try again later.', validationError:'Check the entered data.', accessDenied:'The request was rejected. Refresh the page and try again.', registrationDisabled:'Registration is disabled.', serviceUnavailable:'The account service is temporarily unavailable.',
        registrationPendingApproval:'Account created. Access opens only after approval by the site owner.', accessPending:'This account is waiting for approval by the site owner.', accessBlocked:'The site owner has blocked access for this account.', accountDeleted:'This account has been deleted.'
      }
    };
    const VALID_LOCALES = new Set(Object.keys(I18N));
    const readCookie = name => document.cookie.split(';').map(value=>value.trim()).find(value=>value.startsWith(`${name}=`))?.slice(name.length+1) || '';
    function storedLocale(){
      let cookie='';try{cookie=decodeURIComponent(readCookie('cs2_locale'));}catch(_){}
      let local=''; try{local=localStorage.getItem('cs2_locale')||'';}catch(_){}
      const server=document.documentElement.lang;
      return VALID_LOCALES.has(cookie)?cookie:(VALID_LOCALES.has(server)?server:(VALID_LOCALES.has(local)?local:'ru'));
    }
    let locale=storedLocale();
    const tr = (key, values={}) => (I18N[locale]?.[key] ?? I18N.ru[key] ?? key).replace(/\{(\w+)\}/g,(_,name)=>String(values[name]??''));
    function persistLocale(value){
      document.cookie=`cs2_locale=${encodeURIComponent(value)}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol==='https:'?'; Secure':''}`;
      try{localStorage.setItem('cs2_locale',value);}catch(_){}
    }
    function applyLocale(value,{persist=true}={}){
      locale=VALID_LOCALES.has(value)?value:'ru'; document.documentElement.lang=locale; document.title=tr('documentTitle');
      document.querySelectorAll('[data-i18n]').forEach(node=>{node.textContent=tr(node.dataset.i18n);});
      document.querySelectorAll('[data-i18n-placeholder]').forEach(node=>{node.placeholder=tr(node.dataset.i18nPlaceholder);});
      document.querySelectorAll('[data-i18n-aria-label]').forEach(node=>{node.setAttribute('aria-label',tr(node.dataset.i18nAriaLabel));});
      document.querySelector('#localeSelect').value=locale; if(persist)persistLocale(locale);
      document.querySelectorAll('.feedback').forEach(node=>{node.textContent='';delete node.dataset.tone;});renderPageNotice();
    }
    const SERVER_MESSAGE_KEYS = {
      'invalid email or password':'invalidCredentials', 'email is invalid':'invalidEmail', 'password must be a string':'passwordLength',
      'password must contain between 10 and 256 characters':'passwordLength', 'password is too long':'passwordLength',
      'an account with this email already exists':'duplicateEmail', 'invalid or expired reset token':'invalidResetToken',
      'too many registration attempts':'tooManyAttempts', 'too many login attempts':'tooManyAttempts', 'too many reset attempts':'tooManyAttempts',
      'registration is disabled':'registrationDisabled', 'account service is unavailable':'serviceUnavailable',
      'password-reset email delivery is not configured':'smtpMissing', 'email delivery is not configured':'smtpMissing'
    };
    const ERROR_CODE_KEYS = {access_pending:'accessPending',access_blocked:'accessBlocked',account_deleted:'accountDeleted',invalid_credentials:'invalidCredentials'};
    const safeServerMessage = (value,status=0,code='') => {
      const key=ERROR_CODE_KEYS[String(code||'').trim().toLowerCase()]||SERVER_MESSAGE_KEYS[String(value||'').trim()]||(status===429?'tooManyAttempts':status===503?'serviceUnavailable':status===403?'accessDenied':status===400?'validationError':'requestFailed');
      return tr(key);
    };
    const mode = document.body.dataset.mode;
    const form = document.querySelector(`form[data-form="${mode}"]`);
    const endpoints = {login: '/api/auth/login', register: '/api/auth/register', forgot: '/api/auth/forgot-password', reset: '/api/auth/reset-password'};
    const pendingKeys = {login:'loginPending', register:'registerPending', forgot:'forgotPending', reset:'resetPending'};
    const pageParams = new URLSearchParams(location.search);
    function renderPageNotice(){
      if (mode !== 'login' || !form || (pageParams.get('password_changed') !== '1' && pageParams.get('reset') !== '1')) return;
      const feedback = form.querySelector('.feedback');
      feedback.textContent=tr(pageParams.get('reset')==='1'?'resetSuccess':'passwordChanged');
      feedback.dataset.tone = 'good';
    }
    applyLocale(locale);
    document.querySelector('#localeSelect').addEventListener('change',event=>applyLocale(event.target.value));

    function safeNext() {
      const raw = new URLSearchParams(location.search).get('next') || '/account';
      try {
        const target = new URL(raw, location.origin);
        return target.origin === location.origin && target.pathname.startsWith('/') ? `${target.pathname}${target.search}${target.hash}` : '/account';
      } catch (_) { return '/account'; }
    }

    if (form) form.addEventListener('submit', async event => {
      event.preventDefault();
      const feedback = form.querySelector('.feedback');
      const button = form.querySelector('button');
      const data = Object.fromEntries(new FormData(form).entries());
      if (mode === 'reset') data.token = new URLSearchParams(location.search).get('token') || '';
      if ((mode === 'register' || mode === 'reset') && data.password !== data.password_confirm) {
        feedback.textContent = tr('passwordsMismatch'); feedback.dataset.tone = 'bad'; return;
      }
      delete data.password_confirm;
      const buttonKey=button.dataset.i18n; button.disabled = true; button.textContent = tr(pendingKeys[mode]); feedback.textContent = ''; delete feedback.dataset.tone;
      try {
        const response = await fetch(endpoints[mode], {
          method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json', 'X-CS2-Action': `auth-${mode}`},
          body: JSON.stringify(data)
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw Object.assign(new Error(safeServerMessage(payload.error,response.status,payload.error_code)),{localized:true});
        if (mode === 'register' && payload.approval_required === true) {
          feedback.textContent = tr('registrationPendingApproval'); feedback.dataset.tone = 'warn'; form.reset(); return;
        }
        feedback.textContent = mode === 'forgot' && payload.delivery_configured === false
          ? tr('smtpMissing')
          : tr(mode === 'forgot' ? 'forgotAccepted' : 'done');
        feedback.dataset.tone = mode === 'forgot' && payload.delivery_configured === false ? 'bad' : 'good';
        if (mode === 'login' || mode === 'register') location.assign(safeNext());
        if (mode === 'reset') setTimeout(() => location.assign('/login?reset=1'), 900);
        if (mode === 'forgot') form.reset();
      } catch (error) {
        feedback.textContent = error?.localized ? error.message : tr('requestFailed'); feedback.dataset.tone = 'bad';
      } finally { button.disabled = false; button.textContent = tr(buttonKey); }
    });
  </script>
</body>
</html>
"""


ACCOUNT_HTML = r"""<!doctype html>
<html lang="__LOCALE__">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Мой инвентарь · CSSkinsPrediction</title>
  <style>
    :root {
      color-scheme: dark; --bg:#080b12; --panel:#111724; --panel-2:#151c2a; --line:rgba(255,255,255,.09);
      --text:#f4f7fb; --muted:#93a0b5; --accent:#a6ff6f; --cyan:#63d7ff; --warn:#ffc768; --bad:#ff7c88;
      --shadow:0 22px 60px rgba(0,0,0,.26);
    }
    * { box-sizing:border-box; }
    html { max-width:100%;overflow-x:hidden; }
    body { margin:0;max-width:100%;overflow-x:hidden;color:var(--text);background:radial-gradient(circle at 8% 0,rgba(99,215,255,.09),transparent 30%),var(--bg);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
    a { color:inherit; text-decoration:none; } button,input,select,textarea { font:inherit; }
    .wrap { width:min(1440px,calc(100% - 34px)); margin:0 auto; }
    header { position:sticky; top:0; z-index:20; border-bottom:1px solid var(--line); background:rgba(8,11,18,.88); backdrop-filter:blur(18px); }
    .topbar { min-width:0;min-height:68px;display:flex;align-items:center;gap:24px; }
    .brand { font-weight:900; letter-spacing:-.035em; font-size:18px; }
    nav { min-width:0;max-width:100%;display:flex;align-items:center;gap:5px;flex:1;overflow-x:auto;overflow-y:hidden; }
    nav a { padding:9px 12px; color:var(--muted); border-radius:10px; white-space:nowrap; }
    nav a:hover,nav a.active { color:var(--text); background:rgba(255,255,255,.06); }
    .userbox { min-width:0;display:flex;align-items:center;gap:10px; }
    .avatar { width:34px;height:34px;display:grid;place-items:center;border-radius:50%;color:#081009;background:linear-gradient(135deg,var(--accent),#5deca5);font-weight:900; }
    .quiet-button { padding:8px 10px;border:1px solid var(--line);border-radius:10px;color:var(--muted);background:transparent;cursor:pointer; }
    .locale-control { display:flex;align-items:center;gap:6px;color:var(--muted);font-size:11px;font-weight:750;white-space:nowrap; }
    .locale-control select { width:auto;min-width:62px;padding:7px 8px;cursor:pointer; }
    main { padding:36px 0 80px; }
    .hero { display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin-bottom:24px; }
    .hero-actions { display:flex;flex-wrap:wrap;justify-content:flex-end;gap:9px; }
    .eyebrow { color:var(--accent);font-size:11px;font-weight:850;letter-spacing:.14em;text-transform:uppercase; }
    h1 { margin:8px 0 5px;font-size:clamp(34px,5vw,58px);line-height:1;letter-spacing:-.05em; }
    .muted { color:var(--muted); }
    .primary { min-height:43px;padding:0 17px;border:0;border-radius:12px;color:#081009;background:linear-gradient(135deg,var(--accent),#67e9a4);font-weight:850;cursor:pointer; }
    .metrics { display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:24px; }
    .metric,.panel { border:1px solid var(--line);border-radius:18px;background:linear-gradient(150deg,rgba(22,29,43,.98),rgba(14,19,29,.98));box-shadow:var(--shadow); }
    .metric { padding:18px;min-height:116px; }
    .metric span { display:block;color:var(--muted);font-size:12px; }
    .metric strong { display:block;margin:9px 0 3px;font-size:24px;letter-spacing:-.035em; }
    .metric small { color:var(--muted); }
    .metric[data-tone="good"] strong { color:var(--accent); }.metric[data-tone="bad"] strong { color:var(--bad); }
    .workspace { min-width:0;display:grid;grid-template-columns:minmax(310px,370px) minmax(0,1fr);gap:16px;align-items:start; }
    .panel { min-width:0;padding:20px; }
    .panel-head { display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px; }
    .panel h2 { margin:0;font-size:19px;letter-spacing:-.025em; }
    .trade-form { min-width:0;display:grid;gap:12px; }
    .field { display:grid;gap:6px;color:#cbd3df;font-size:12px;font-weight:700; }
    .import-box { display:grid;gap:7px;padding:12px;border:1px solid rgba(99,215,255,.2);border-radius:13px;background:rgba(99,215,255,.045); }
    .import-row { display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:stretch; }
    .import-row button { min-height:41px;white-space:nowrap; }
    .import-help { margin:0;color:var(--muted);font-size:11px; }
    input,select,textarea { min-width:0;max-width:100%;width:100%;padding:10px 11px;border:1px solid var(--line);border-radius:11px;outline:0;color:var(--text);background:rgba(5,8,14,.72); }
    textarea { min-height:72px;resize:vertical; }
    input:focus,select:focus,textarea:focus { border-color:rgba(99,215,255,.58);box-shadow:0 0 0 3px rgba(99,215,255,.08); }
    .two { display:grid;grid-template-columns:1fr 1fr;gap:10px; }
    details { min-width:0; }
    summary { list-style:none; }
    summary::-webkit-details-marker { display:none; }
    .disclosure-summary { min-width:0;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px;border-radius:12px;color:var(--text);cursor:pointer;user-select:none; }
    .disclosure-summary:hover { background:rgba(255,255,255,.035); }
    .disclosure-summary:focus-visible { outline:2px solid var(--cyan);outline-offset:2px; }
    .disclosure-copy { min-width:0;display:grid;gap:2px; }
    .disclosure-copy strong { font-size:13px; }
    .disclosure-copy small { color:var(--muted);font-size:11px;font-weight:500; }
    .disclosure-icon { width:25px;height:25px;display:grid;place-items:center;flex:0 0 auto;border:1px solid var(--line);border-radius:8px;color:var(--muted);transition:transform .18s ease; }
    details[open] > .disclosure-summary .disclosure-icon { transform:rotate(45deg); }
    .optional-details { border:1px solid var(--line);border-radius:13px;background:rgba(255,255,255,.018); }
    .optional-content { min-width:0;display:grid;gap:12px;padding:4px 12px 12px; }
    .form-actions { display:flex;gap:8px;margin-top:3px; }.form-actions button { flex:1; }
    .secondary { min-height:41px;padding:0 13px;border:1px solid var(--line);border-radius:11px;color:var(--text);background:rgba(255,255,255,.035);cursor:pointer; }
    .secondary[hidden] { display:none; }.feedback { min-height:18px;margin:0;color:var(--muted);font-size:12px; }.feedback[data-tone="bad"] { color:var(--bad); }.feedback[data-tone="good"] { color:var(--accent); }.feedback[data-tone="warn"] { color:var(--warn); }
    .toolbar { display:grid;grid-template-columns:minmax(150px,1fr) auto auto;gap:9px;margin-bottom:15px; }
    .status-tabs { display:flex;gap:6px;overflow:auto;margin-bottom:15px; }
    .status-tab { padding:7px 10px;border:1px solid var(--line);border-radius:999px;color:var(--muted);background:transparent;cursor:pointer;white-space:nowrap; }
    .status-tab.active { color:#091008;border-color:transparent;background:var(--accent);font-weight:800; }
    .inventory { display:grid;gap:10px; }
    .empty { padding:44px 20px;border:1px dashed rgba(255,255,255,.13);border-radius:15px;text-align:center;color:var(--muted); }
    .item { display:grid;grid-template-columns:64px minmax(190px,1.4fr) repeat(3,minmax(110px,.7fr)) minmax(150px,.85fr);gap:14px;align-items:center;padding:13px;border:1px solid var(--line);border-radius:15px;background:rgba(5,8,14,.34); }
    .skin-img { width:64px;height:52px;object-fit:contain;border-radius:10px;background:radial-gradient(circle,rgba(99,215,255,.12),transparent 68%); }
    .skin-fallback { width:64px;height:52px;display:grid;place-items:center;border-radius:10px;background:rgba(255,255,255,.04);color:var(--muted);font-weight:900; }
    .item h3 { margin:0 0 5px;font-size:14px; }.chips { display:flex;flex-wrap:wrap;gap:5px; }.chip { padding:3px 7px;border-radius:999px;color:var(--muted);background:rgba(255,255,255,.055);font-size:10px; }
    .chip.warn { color:#261b05;background:var(--warn);font-weight:850; }.chip.good { color:#071108;background:var(--accent);font-weight:850; }
    .value span { display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em; }.value strong { display:block;margin-top:4px;font-size:14px; }.value small { display:block;margin-top:2px;color:var(--muted);font-size:10px; }
    .profit.good { color:var(--accent); }.profit.bad { color:var(--bad); }
    .actions { display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px; }.actions button { padding:7px 9px;border:1px solid var(--line);border-radius:9px;color:var(--text);background:rgba(255,255,255,.04);cursor:pointer;font-size:11px; }.actions button.danger { color:var(--bad); }
    .disclaimer { margin:13px 0 0;color:var(--muted);font-size:11px; }
    .trade-ledger { margin-top:16px; }
    .ledger-head-actions { display:flex;align-items:center;gap:10px; }
    .trade-list { display:grid;gap:10px; }
    .trade-card { display:grid;grid-template-columns:minmax(150px,.55fr) minmax(0,1.5fr) minmax(180px,.65fr);gap:16px;align-items:center;padding:15px;border:1px solid var(--line);border-radius:15px;background:rgba(5,8,14,.34); }
    .trade-card h3 { margin:0 0 5px;font-size:14px; }
    .trade-card-date { color:var(--muted);font-size:11px; }
    .trade-card-sides { min-width:0;display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);gap:12px;align-items:center; }
    .trade-card-side { min-width:0;display:grid;gap:5px; }
    .trade-card-side>span { color:var(--muted);font-size:10px;font-weight:800;letter-spacing:.07em;text-transform:uppercase; }
    .trade-card-leg { overflow-wrap:anywhere;font-size:12px; }
    .trade-arrow { color:var(--cyan);font-size:21px;font-weight:900; }
    .trade-card-result { display:grid;gap:6px;text-align:right; }
    .trade-card-result strong { font-size:16px; }
    .trade-card-result small { color:var(--muted); }
    .trade-card-result .actions { margin-top:3px; }
    dialog { width:min(440px,calc(100% - 24px));padding:0;border:1px solid var(--line);border-radius:20px;color:var(--text);background:var(--panel);box-shadow:0 30px 100px #000; }
    dialog::backdrop { background:rgba(0,0,0,.7);backdrop-filter:blur(4px); }.dialog-body { padding:22px; }.dialog-body h2 { margin:0 0 16px; }.dialog-actions { display:flex;gap:8px;margin-top:16px; }.dialog-actions button { flex:1; }
    .trade-dialog { width:min(1040px,calc(100% - 24px));max-height:calc(100vh - 30px);overflow:auto; }
    .trade-editor { display:grid;gap:15px; }
    .trade-editor-head { display:flex;align-items:flex-start;justify-content:space-between;gap:16px; }
    .trade-editor-head h2 { margin:0; }
    .trade-sides { min-width:0;display:grid;grid-template-columns:1fr 1fr;gap:12px; }
    .trade-side { min-width:0;padding:13px;border:1px solid var(--line);border-radius:15px;background:rgba(5,8,14,.3); }
    .trade-side-head { display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:11px; }
    .trade-side-head h3 { margin:0 0 2px;font-size:15px; }
    .trade-side-head p { margin:0;color:var(--muted);font-size:10px; }
    .trade-legs { display:grid;gap:8px; }
    .trade-leg { min-width:0;display:grid;grid-template-columns:minmax(0,1.7fr) 78px 118px 34px;gap:7px;align-items:end;padding:9px;border:1px solid rgba(255,255,255,.07);border-radius:12px;background:rgba(255,255,255,.025); }
    .trade-leg .field { min-width:0; }
    .trade-leg .field span { overflow:hidden;text-overflow:ellipsis;white-space:nowrap; }
    .item-preview { min-width:0;display:grid;grid-template-columns:52px minmax(0,1fr);gap:10px;align-items:center;padding:9px;border:1px solid rgba(99,215,255,.2);border-radius:12px;background:rgba(99,215,255,.045); }
    .item-preview[hidden] { display:none; }
    .item-preview-media { width:52px;height:44px;display:grid;place-items:center;border-radius:9px;background:radial-gradient(circle,rgba(99,215,255,.14),transparent 70%);overflow:hidden; }
    .item-preview-media img { width:100%;height:100%;object-fit:contain; }
    .item-preview-fallback { color:var(--muted);font-size:10px;font-weight:900;letter-spacing:.04em; }
    .item-preview-copy { min-width:0;display:grid;gap:2px; }
    .item-preview-copy strong { overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px; }
    .item-preview-copy span,.item-preview-copy small { color:var(--muted);font-size:10px; }
    .item-preview-copy small[data-tone="good"] { color:var(--accent); }
    .item-preview-copy small[data-tone="warn"] { color:var(--warn); }
    .purchase-item-preview { margin-top:-5px; }
    .trade-leg .item-preview { grid-column:1/-1; }
    .leg-remove { width:34px;height:41px;padding:0;border:1px solid var(--line);border-radius:10px;color:var(--bad);background:rgba(255,255,255,.035);cursor:pointer;font-size:19px; }
    .add-leg { width:100%;margin-top:9px; }
    .trade-summary { display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px; }
    .trade-summary-card { min-width:0;padding:11px;border:1px solid var(--line);border-radius:12px;background:rgba(255,255,255,.025); }
    .trade-summary-card span { display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em; }
    .trade-summary-card strong { display:block;margin-top:4px;font-size:16px; }
    .trade-summary-note { grid-column:1/-1;margin:0;color:var(--warn);font-size:11px; }
    .trade-summary-note:empty { display:none; }
    .settings { min-width:0;margin-top:16px; }
    .settings-disclosure { border:1px solid var(--line);border-radius:18px;background:linear-gradient(150deg,rgba(22,29,43,.98),rgba(14,19,29,.98));box-shadow:var(--shadow); }
    .settings-disclosure > .disclosure-summary { padding:16px 20px; }
    .settings-disclosure > .disclosure-summary .disclosure-copy strong { font-size:17px;letter-spacing:-.02em; }
    .settings-grid { min-width:0;display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:0 20px 20px; }
    .settings form { min-width:0;display:grid;gap:11px;padding:15px;border:1px solid var(--line);border-radius:14px;background:rgba(5,8,14,.26); }
    .settings-form-title { margin:0;font-size:13px;letter-spacing:-.01em; }
    @media(max-width:1180px){.metrics{grid-template-columns:repeat(3,1fr)}.item{grid-template-columns:58px minmax(180px,1.3fr) repeat(2,minmax(100px,.7fr));}.item .value:nth-of-type(3){display:none}.actions{grid-column:2/-1;justify-content:flex-start}.trade-card{grid-template-columns:minmax(130px,.5fr) minmax(0,1.5fr)}.trade-card-result{grid-column:1/-1;text-align:left}.trade-card-result .actions{grid-column:auto}}
    @media(max-width:850px){.workspace{grid-template-columns:1fr}.trade-panel{order:2}.metrics{grid-template-columns:repeat(2,1fr)}.settings-grid{grid-template-columns:1fr}.user-name{display:none}.trade-sides{grid-template-columns:1fr}.trade-card{grid-template-columns:1fr}.trade-card-result{grid-column:auto}.trade-card-sides{grid-template-columns:minmax(0,1fr) auto minmax(0,1fr)}}
    @media(max-width:620px){.wrap{width:min(100% - 20px,1440px)}.topbar{gap:10px}.topbar>*{min-width:0}.brand{font-size:15px}nav{order:3;width:100%;flex-basis:100%;scrollbar-width:thin}.topbar{flex-wrap:wrap;padding:12px 0}.locale-control span{display:none}.userbox{margin-left:auto}.hero{align-items:flex-start;flex-direction:column}.hero-actions{width:100%;justify-content:stretch}.hero-actions button{flex:1}.metrics{grid-template-columns:1fr 1fr}.metric{min-width:0;min-height:100px;padding:14px}.toolbar{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.toolbar input{grid-column:1/-1}.item{min-width:0;grid-template-columns:52px minmax(0,1fr);padding:11px}.skin-img,.skin-fallback{width:52px;height:46px}.item .value{display:block!important}.actions{grid-column:1/-1}.two{grid-template-columns:minmax(0,1fr)}.import-row{grid-template-columns:minmax(0,1fr)}.import-row button{width:100%}.ledger-head-actions{align-items:flex-end;flex-direction:column}.trade-card-sides{grid-template-columns:1fr}.trade-arrow{transform:rotate(90deg);justify-self:center}.trade-leg{grid-template-columns:minmax(0,1fr) 72px}.trade-leg .field:nth-of-type(3){grid-column:1/2}.trade-summary{grid-template-columns:1fr}.trade-summary-note{grid-column:auto}.trade-editor-head{align-items:flex-start;flex-direction:column}.dialog-body{padding:15px}.settings-grid{padding:0 12px 12px}.settings-disclosure>.disclosure-summary{padding:14px 12px}}
  </style>
</head>
<body>
  <header><div class="wrap topbar">
    <a class="brand" href="/">CSSkinsPrediction</a>
    <nav><a href="/" data-i18n="navOverview">Обзор</a><a href="/deals" data-i18n="navDeals">Сделки</a><a href="/strategy" data-i18n="navStrategy">Стратегия</a><a class="active" href="/account" data-i18n="navInventory">Мой инвентарь</a></nav>
    <label class="locale-control"><span data-i18n="language">Язык</span><select id="localeSelect" data-i18n-aria-label="language"><option value="ru">RU</option><option value="en">EN</option></select></label>
    <div class="userbox"><span class="avatar" id="avatar">?</span><span class="user-name" id="userName" data-i18n="loading">Загрузка…</span><button class="quiet-button" id="logoutButton" type="button" data-i18n="logout">Выйти</button></div>
  </div></header>
  <main class="wrap">
    <section class="hero"><div><span class="eyebrow" data-i18n="heroEyebrow">Портфель и сделки</span><h1 data-i18n="heroTitle">Мой инвентарь</h1><p class="muted" data-i18n="heroLead">Покупки, выставление, прибыль и сигналы для переоценки в одном месте.</p></div><div class="hero-actions"><button class="secondary" id="focusTrade" type="button" data-i18n="addTrade">⇄ Записать обмен</button><button class="primary" id="focusAdd" type="button" data-i18n="addPurchase">+ Добавить покупку</button></div></section>
    <section class="metrics">
      <article class="metric"><span data-i18n="metricInvested">Вложено в активные</span><strong id="metricInvested">—</strong><small id="metricActiveCount">— предметов</small></article>
      <article class="metric"><span data-i18n="metricMarket">Текущая оценка</span><strong id="metricMarket">—</strong><small data-i18n="salesHistory">минимум CSFloat, затем история</small></article>
      <article class="metric" id="unrealizedCard"><span data-i18n="metricUnrealized">Нереализованная прибыль/убыток</span><strong id="metricUnrealized">—</strong><small data-i18n="afterFee">после комиссии</small></article>
      <article class="metric" id="realizedCard"><span data-i18n="metricRealized">Реализованная прибыль/убыток</span><strong id="metricRealized">—</strong><small id="metricSoldCount">— продаж</small></article>
      <article class="metric"><span data-i18n="metricStale">Требуют внимания</span><strong id="metricStale">—</strong><small data-i18n="staleHint">долго не продаются</small></article>
    </section>
    <section class="workspace">
      <aside class="panel trade-panel" id="tradePanel">
        <div class="panel-head"><h2 id="formTitle">Добавить покупку</h2><span class="muted">USD</span></div>
        <form class="trade-form" id="inventoryForm">
          <input type="hidden" name="item_id">
          <div class="import-box">
            <label class="field" for="sourceUrlInput"><span data-i18n="csfloatUrl">Ссылка на лот CSFloat</span></label>
            <div class="import-row"><input id="sourceUrlInput" name="source_url" type="url" maxlength="1000" data-i18n-placeholder="csfloatUrlPlaceholder" placeholder="https://csfloat.com/item/..."><button class="secondary" id="importCsfloatButton" type="button" data-i18n="importCsfloat">Загрузить лот</button></div>
            <p class="feedback" id="importFeedback" role="status" aria-live="polite"></p>
            <p class="import-help" data-i18n="importHelp">Вставьте ссылку: данные заполнят форму, но покупка не сохранится, пока вы сами не нажмёте «Сохранить».</p>
          </div>
          <label class="field"><span data-i18n="itemName">Название предмета</span><input name="skin_name" list="skinSuggestions" maxlength="180" autocomplete="off" placeholder="AK-47 | Slate (Field-Tested)" required><datalist id="skinSuggestions"></datalist></label>
          <div class="item-preview purchase-item-preview" id="purchaseItemPreview" hidden aria-live="polite"></div>
          <label class="field"><span data-i18n="buyPrice">Купил за, $</span><input name="buy_price" inputmode="decimal" placeholder="5.79" required></label>
          <details class="optional-details" id="optionalInventoryDetails">
            <summary class="disclosure-summary"><span class="disclosure-copy"><strong data-i18n="optionalDetails">Дополнительно</strong><small data-i18n="optionalDetailsHint">Дата, количество, float, стикеры и параметры выставления</small></span><span class="disclosure-icon" aria-hidden="true">+</span></summary>
            <div class="optional-content">
              <div class="two"><label class="field"><span data-i18n="quantity">Количество</span><input name="quantity" type="number" min="1" max="1000" value="1" required></label><label class="field"><span data-i18n="boughtAt">Дата покупки</span><input name="bought_at" type="date" required></label></div>
              <div class="two"><label class="field"><span data-i18n="strategy">Стратегия</span><select name="strategy"><option value="flip" data-i18n="strategyFlip">Быстрая перепродажа</option><option value="investment" data-i18n="strategyInvestment">Инвестиция</option><option value="balanced" data-i18n="strategyBalanced">Сбалансированно</option><option value="float" data-i18n="strategyFloat">Флот</option><option value="stickers" data-i18n="strategyStickers">Стикеры</option><option value="collection" data-i18n="strategyCollection">Коллекция</option><option value="manual" data-i18n="strategyManual">Вручную</option></select></label><label class="field"><span data-i18n="feePercent">Комиссия, %</span><input name="fee_percent" inputmode="decimal" value="4"></label></div>
              <label class="field"><span data-i18n="floatValue">Флот</span><input name="float_value" inputmode="decimal" data-i18n-placeholder="optional" placeholder="необязательно"></label>
              <div class="two"><label class="field"><span data-i18n="listedPrice">Выставлен за, $</span><input name="listed_price" inputmode="decimal" data-i18n-placeholder="optional" placeholder="необязательно"></label><label class="field"><span data-i18n="listedAt">Дата выставления</span><input name="listed_at" type="date"></label></div>
              <label class="field"><span data-i18n="stickers">Стикеры</span><input name="stickers" maxlength="600" data-i18n-placeholder="stickersPlaceholder" placeholder="Названия, позиции или краткое описание"></label>
              <label class="field"><span data-i18n="notes">Заметка</span><textarea name="notes" maxlength="1200" data-i18n-placeholder="notesPlaceholder" placeholder="Паттерн, редкий флот, желаемая прибыль…"></textarea></label>
            </div>
          </details>
          <div class="form-actions"><button class="primary" type="submit" id="saveButton">Сохранить</button><button class="secondary" type="button" id="cancelEdit" hidden data-i18n="cancel">Отмена</button></div>
          <p class="feedback" id="formFeedback" role="status" aria-live="polite"></p>
        </form>
      </aside>
      <section class="panel">
        <div class="panel-head"><h2 data-i18n="positions">Позиции</h2><span class="muted" id="resultCount">—</span></div>
        <div class="toolbar"><input id="searchInput" type="search" data-i18n-placeholder="searchPlaceholder" placeholder="Поиск по названию"><select id="sortSelect"><option value="newest" data-i18n="sortNewest">Сначала новые</option><option value="stale" data-i18n="sortStale">Сначала зависшие</option><option value="profit_high" data-i18n="sortProfit">Прибыль выше</option><option value="value_high" data-i18n="sortValue">Стоимость выше</option></select><button class="secondary" id="refreshButton" type="button" data-i18n="refresh">Обновить</button></div>
        <div class="status-tabs" id="statusTabs"><button class="status-tab active" data-status="active" data-i18n="tabActive">Активные</button><button class="status-tab" data-status="listed" data-i18n="tabListed">Выставлены</button><button class="status-tab" data-status="owned" data-i18n="tabOwned">Не выставлены</button><button class="status-tab" data-status="sold" data-i18n="tabSold">Проданные</button><button class="status-tab" data-status="archived" data-i18n="tabArchived">Удалённые</button><button class="status-tab" data-status="all" data-i18n="tabAll">Все</button></div>
        <div class="inventory" id="inventoryList"><div class="empty" data-i18n="inventoryLoading">Загружаем инвентарь…</div></div>
        <p class="disclaimer" data-i18n="disclaimer">Сначала используется минимальное активное buy-now предложение CSFloat, затем история продаж и сохранённая оценка. Редкий флот, паттерн и дорогие стикеры могут стоить иначе.</p>
      </section>
    </section>
    <section class="panel trade-ledger" id="tradeLedger">
      <div class="panel-head"><div><h2 data-i18n="trades">Обмены</h2><span class="muted" id="tradeResultCount">—</span></div><div class="ledger-head-actions"><button class="secondary" id="addTradeButton" type="button" data-i18n="addTrade">⇄ Записать обмен</button></div></div>
      <div class="trade-list" id="tradeList"><div class="empty" data-i18n="tradesLoading">Загружаем обмены…</div></div>
      <p class="feedback" id="tradeLedgerFeedback" role="status" aria-live="polite"></p>
      <p class="disclaimer" data-i18n="tradeDisclaimer">Оценка фиксируется на дату обмена и нужна для сравнения сторон. Текущая стоимость полученных предметов считается отдельно по доступным рыночным данным.</p>
    </section>
    <section class="settings" id="settingsPanel">
      <details class="settings-disclosure">
        <summary class="disclosure-summary"><span class="disclosure-copy"><strong data-i18n="accountSettings">Настройки аккаунта</strong><small data-i18n="securityAndPricing">Профиль, безопасность и расчёты</small></span><span class="disclosure-icon" aria-hidden="true">+</span></summary>
        <div class="settings-grid">
          <form id="profileForm"><h3 class="settings-form-title" data-i18n="profileSettings">Профиль и расчёты</h3><label class="field"><span data-i18n="name">Имя</span><input name="display_name" maxlength="60" required></label><label class="field"><span data-i18n="staleThreshold">Порог зависшего лота, дней</span><input name="stale_after_days" type="number" min="1" max="90" value="7" required></label><label class="field"><span data-i18n="defaultFee">Комиссия по умолчанию, %</span><input name="default_fee_percent" inputmode="decimal" value="4" required></label><button class="secondary" type="submit" data-i18n="saveSettings">Сохранить настройки</button><p class="feedback" role="status" aria-live="polite"></p></form>
          <form id="passwordForm"><h3 class="settings-form-title" data-i18n="passwordSecurity">Безопасность</h3><label class="field"><span data-i18n="currentPassword">Текущий пароль</span><input name="current_password" type="password" autocomplete="current-password" required></label><label class="field"><span data-i18n="newPassword">Новый пароль</span><input name="new_password" type="password" minlength="10" maxlength="256" autocomplete="new-password" required></label><button class="secondary" type="submit" data-i18n="changePassword">Сменить пароль и выйти с других устройств</button><p class="feedback" role="status" aria-live="polite"></p></form>
        </div>
      </details>
    </section>
  </main>
  <dialog id="soldDialog"><form method="dialog" class="dialog-body" id="soldForm" data-action="mark_sold"><h2 id="soldDialogTitle" data-i18n="markSold">Отметить как проданный</h2><input type="hidden" name="item_id"><div class="two"><label class="field"><span data-i18n="soldPrice">Цена продажи за штуку, $</span><input name="sold_price" inputmode="decimal" required></label><label class="field"><span data-i18n="soldAt">Дата продажи</span><input name="sold_at" type="date" required></label></div><label class="field"><span data-i18n="saleFee">Комиссия при продаже, %</span><input name="fee_percent" inputmode="decimal" required></label><p class="feedback" role="status" aria-live="polite"></p><div class="dialog-actions"><button class="secondary" value="cancel" data-i18n="cancel">Отмена</button><button class="primary" id="saveSaleButton" value="default" data-i18n="saveSale">Сохранить продажу</button></div></form></dialog>
  <dialog class="trade-dialog" id="tradeDialog">
    <form class="dialog-body trade-editor" id="tradeForm">
      <div class="trade-editor-head"><div><h2 id="tradeDialogTitle" data-i18n="createTradeTitle">Записать обмен</h2><p class="muted" data-i18n="tradeEditorHint">Добавьте все предметы с обеих сторон и укажите их оценку на момент обмена.</p></div><span class="muted">USD</span></div>
      <input type="hidden" name="trade_id">
      <input type="hidden" name="expected_revision">
      <div class="two"><label class="field"><span data-i18n="tradedAt">Дата обмена</span><input name="traded_at" type="date" required></label><label class="field"><span data-i18n="tradeNotes">Заметка</span><input name="notes" maxlength="1200" data-i18n-placeholder="tradeNotesPlaceholder" placeholder="С кем обменялся, условия или детали"></label></div>
      <div class="trade-sides">
        <section class="trade-side"><div class="trade-side-head"><div><h3 data-i18n="outgoingTitle">Отдаю</h3><p data-i18n="outgoingHint">Выберите предметы из активного инвентаря</p></div></div><div class="trade-legs" id="outgoingLegs"></div><button class="secondary add-leg" id="addOutgoingLeg" type="button" data-i18n="addOutgoing">+ Добавить мой предмет</button></section>
        <section class="trade-side"><div class="trade-side-head"><div><h3 data-i18n="incomingTitle">Получаю</h3><p data-i18n="incomingHint">Начните вводить название — картинка и минимальная цена CSFloat подставятся автоматически</p></div></div><div class="trade-legs" id="incomingLegs"></div><button class="secondary add-leg" id="addIncomingLeg" type="button" data-i18n="addIncoming">+ Добавить полученный предмет</button></section>
      </div>
      <div class="trade-summary"><div class="trade-summary-card"><span data-i18n="outgoingTotal">Отдаю всего</span><strong id="outgoingTotal">—</strong></div><div class="trade-summary-card"><span data-i18n="incomingTotal">Получаю всего</span><strong id="incomingTotal">—</strong></div><div class="trade-summary-card"><span data-i18n="tradeDifference">Разница</span><strong id="tradeDifference">—</strong></div><p class="trade-summary-note" id="tradeSummaryNote" role="status" aria-live="polite"></p></div>
      <p class="feedback" id="tradeFeedback" role="status" aria-live="polite"></p>
      <div class="dialog-actions"><button class="secondary" id="cancelTradeButton" type="button" data-i18n="cancel">Отмена</button><button class="primary" id="saveTradeButton" type="submit" data-i18n="saveTrade">Сохранить обмен</button></div>
    </form>
  </dialog>
  <script>
    const I18N = {
      ru: {
        documentTitle:'Мой инвентарь · CSSkinsPrediction', language:'Язык', navOverview:'Обзор', navDeals:'Сделки', navStrategy:'Стратегия', navCatalog:'Каталог', navHistory:'История рынков', navKnives:'Ножи', navInventory:'Мой инвентарь', loading:'Загрузка…', logout:'Выйти',
        heroEyebrow:'Портфель и сделки', heroTitle:'Мой инвентарь', heroLead:'Покупки, выставление, прибыль и сигналы для переоценки в одном месте.', addPurchase:'+ Добавить покупку', addTrade:'⇄ Записать обмен',
        metricInvested:'Вложено в активные', metricMarket:'Текущая оценка', salesHistory:'минимум CSFloat, затем история', metricUnrealized:'Нереализованная прибыль/убыток', metricRealized:'Реализованная прибыль/убыток', afterFee:'после комиссии', metricStale:'Требуют внимания', staleHint:'долго не продаются',
        csfloatUrl:'Ссылка на лот CSFloat', csfloatUrlPlaceholder:'https://csfloat.com/item/...', importCsfloat:'Загрузить лот', importHelp:'Вставьте ссылку: данные заполнят форму, но покупка не сохранится, пока вы сами не нажмёте «Сохранить».', importLoading:'Загружаем данные лота с CSFloat…',
        importLoaded:'Лот загружен. В поле цены подставлена текущая цена лота, а не подтверждённая сумма покупки. Проверьте её и нажмите «Сохранить».', importLoadedWithId:'Лот #{id} загружен. В поле цены подставлена текущая цена лота, а не подтверждённая сумма покупки. Проверьте её и нажмите «Сохранить».', importHistorical:'Лот загружен из локальной истории. В поле цены указана историческая цена объявления, а не подтверждённая сумма покупки. Введите фактическую цену и нажмите «Сохранить».', importHistoricalWithId:'Лот #{id} загружен из локальной истории. В поле цены указана историческая цена объявления, а не подтверждённая сумма покупки. Введите фактическую цену и нажмите «Сохранить».', importWithoutPrice:'Лот загружен, но его цена недоступна. Введите фактическую цену покупки и нажмите «Сохранить».', importWithoutPriceWithId:'Лот #{id} загружен, но его цена недоступна. Введите фактическую цену покупки и нажмите «Сохранить».',
        invalidCsfloatUrl:'Вставьте полную ссылку на лот с сайта csfloat.com.', importRateLimited:'Слишком много загрузок. Подождите немного и попробуйте снова.', csfloatRateLimited:'CSFloat временно ограничил запросы. Попробуйте чуть позже.', csfloatListingNotFound:'Лот не найден: возможно, его уже купили или сняли с продажи.', csfloatNotConfigured:'Загрузка с CSFloat пока не настроена на сервере.', csfloatInvalidResponse:'CSFloat вернул неполные данные этого лота.', csfloatUnavailable:'Не удалось получить лот с CSFloat. Проверьте ссылку и попробуйте позже.',
        skinName:'Название скина', itemName:'Название предмета', buyPrice:'Купил за, $', optionalDetails:'Дополнительно', optionalDetailsHint:'Дата, количество, float, стикеры и параметры выставления', quantity:'Количество', boughtAt:'Дата покупки', strategy:'Стратегия', strategyFlip:'Быстрая перепродажа', strategyInvestment:'Инвестиция', strategyFloat:'Флот', strategyStickers:'Стикеры', strategyCollection:'Коллекция', strategyBalanced:'Сбалансированно', strategyManual:'Вручную',
        floatValue:'Флот', optional:'необязательно', feePercent:'Комиссия, %', listedPrice:'Выставлен за, $', listedAt:'Дата выставления', stickers:'Стикеры', stickersPlaceholder:'Названия, позиции или краткое описание', sourceUrl:'Ссылка на покупку', notes:'Заметка', notesPlaceholder:'Паттерн, редкий флот, желаемая прибыль…', save:'Сохранить', cancel:'Отмена', editPosition:'Изменить позицию', saveChanges:'Сохранить изменения',
        positions:'Позиции', searchPlaceholder:'Поиск по названию', sortNewest:'Сначала новые', sortStale:'Сначала зависшие', sortProfit:'Прибыль выше', sortValue:'Стоимость выше', refresh:'Обновить', tabActive:'Активные', tabListed:'Выставлены', tabOwned:'Не выставлены', tabSold:'Проданные', tabArchived:'Удалённые', tabAll:'Все', inventoryLoading:'Загружаем инвентарь…', inventoryUpdating:'Обновляем цены и позиции…', inventoryEmpty:'Здесь пока пусто. Добавьте покупку слева — её всегда можно изменить или удалить.',
        disclaimer:'Сначала используется минимальное активное buy-now предложение CSFloat, затем история продаж и сохранённая оценка. Редкий флот, паттерн и дорогие стикеры могут стоить иначе.',
        trades:'Обмены', tradesLoading:'Загружаем обмены…', tradesUpdating:'Обновляем обмены…', tradesEmpty:'Обменов пока нет. Запишите первый обмен, чтобы полученные предметы появились в инвентаре.', tradesLoadFailed:'Не удалось загрузить обмены.', tradeDisclaimer:'Оценка фиксируется на дату обмена и нужна для сравнения сторон. Текущая стоимость полученных предметов считается отдельно по доступным рыночным данным.',
        createTradeTitle:'Записать обмен', editTradeTitle:'Изменить обмен', tradeEditorHint:'Добавьте все предметы с обеих сторон и укажите их оценку на момент обмена.', tradedAt:'Дата обмена', tradeNotes:'Заметка', tradeNotesPlaceholder:'С кем обменялся, условия или детали', outgoingTitle:'Отдаю', outgoingHint:'Выберите предметы из активного инвентаря', incomingTitle:'Получаю', incomingHint:'Начните вводить название — картинка и минимальная цена CSFloat подставятся автоматически', addOutgoing:'+ Добавить мой предмет', addIncoming:'+ Добавить полученный предмет',
        tradeItem:'Предмет', tradeSelectItem:'Выберите предмет', tradeItemName:'Название предмета', tradeUnitValue:'Оценка / шт., $', removeItem:'Убрать предмет', outgoingTotal:'Отдаю всего', incomingTotal:'Получаю всего', tradeDifference:'Разница', tradeTotalsIncomplete:'Заполните количество и положительную оценку у каждого предмета, чтобы посчитать итог.', saveTrade:'Сохранить обмен', saveTradeChanges:'Сохранить изменения', tradeRecorded:'Обмен сохранён.', tradeUpdated:'Обмен изменён.', tradeVoided:'Обмен удалён, инвентарь обновлён.', editTrade:'Изменить', voidTrade:'Удалить', tradeVoidConfirm:'Удалить этот обмен? Инвентарь будет возвращён к состоянию до обмена.', tradeValidation:'Добавьте хотя бы один предмет с каждой стороны и проверьте название, количество и оценку.', tradeDuplicateOutgoing:'Один предмет инвентаря нельзя добавить в обмен дважды.', tradeNoCandidates:'В активном инвентаре нет предметов для обмена.', tradeNotFound:'Обмен не найден.', tradeHasDependents:'Обмен нельзя удалить: один из полученных предметов уже изменён, продан или обменян.', tradeQuantityInvalid:'Для обмена выбрано недоступное количество предмета.',
        accountSettings:'Настройки аккаунта', securityAndPricing:'Профиль, безопасность и расчёты', profileSettings:'Профиль и расчёты', passwordSecurity:'Безопасность', name:'Имя', staleThreshold:'Порог зависшего лота, дней', defaultFee:'Комиссия по умолчанию, %', saveSettings:'Сохранить настройки', currentPassword:'Текущий пароль', newPassword:'Новый пароль', changePassword:'Сменить пароль и выйти с других устройств', markSold:'Отметить как проданный', editSaleTitle:'Изменить продажу', soldPrice:'Цена продажи за штуку, $', soldAt:'Дата продажи', saleFee:'Комиссия при продаже, %', saveSale:'Сохранить продажу', saveSaleChanges:'Сохранить изменения',
        activeItems_one:'{count} предмет', activeItems_few:'{count} предмета', activeItems_many:'{count} предметов', activeItems_other:'{count} предмета', salesCount_one:'{count} продажа', salesCount_few:'{count} продажи', salesCount_many:'{count} продаж', salesCount_other:'{count} продажи', positionsCount_one:'{count} позиция', positionsCount_few:'{count} позиции', positionsCount_many:'{count} позиций', positionsCount_other:'{count} позиции', tradesCount_one:'{count} обмен', tradesCount_few:'{count} обмена', tradesCount_many:'{count} обменов', tradesCount_other:'{count} обмена', holding:'{quantity} шт. · {days} дн.',
        statusListed:'Выставлен', statusSold:'Продан', statusArchived:'Удалён', statusOwned:'В инвентаре', priceSold:'Продано / шт.', priceListed:'Выставлено / шт.', priceMarketHistory:'Рынок (история) / шт.', priceCsfloatFloor:'CSFloat минимум / шт.', priceSavedManual:'Сохранено / шт.', soldOn:'продано {date}', saleDateMissing:'дата продажи не указана', listedSince:'с {date}', listedDateMissing:'дата выставления не указана', dataAsOf:'данные на {date}', noMarketData:'нет рыночных данных', restore:'Вернуть', edit:'Изменить', editSale:'Изменить продажу', undoSale:'Отменить продажу', delete:'Удалить', soldAction:'Продано', staleChip:'Пора проверить цену', specialPremium:'Флот / стикеры', receivedByTrade:'Получено обменом', valuationCsfloatFloor:'CSFloat · минимум', valuationSavedManual:'Сохранённая оценка', valuationSalesHistory:'История рынка', purchaseEach:'Покупка / шт.', recommendationEach:'Рекомендация / шт.', result:'Итог', pnlAfterFee:'Прибыль после комиссии',
        itemTypeSkin:'Скин', itemTypeKnife:'Нож', itemTypeGlove:'Перчатки', itemTypeSticker:'Наклейка', itemTypeCase:'Кейс', itemTypeCapsule:'Капсула', itemTypeSouvenir:'Сувенирный набор', itemTypeContainer:'Контейнер', itemTypeUnknown:'Предмет CS2', priceSourceCsfloatFloor:'минимальный активный лот CSFloat', priceSourceSavedManual:'сохранённая / ручная оценка', priceSourceSalesHistory:'история рынка', priceSourceUnknown:'источник цены не указан', catalogLowestAsk:'Самое дешёвое активное предложение: {price}', catalogPriceUnavailable:'Автоцена не найдена — укажите стоимость вручную.', catalogLookupUnavailable:'Не удалось обновить автоцену — стоимость можно указать вручную.', activeOffers_one:'{count} активное предложение', activeOffers_few:'{count} активных предложения', activeOffers_many:'{count} активных предложений', activeOffers_other:'{count} активного предложения',
        recommendationUnavailable:'Недостаточно рыночных данных', recommendationManualReview:'Проверьте премию вручную', recommendationReviewLossOrWait:'Безубыток выше рынка', recommendationList:'Можно выставлять', recommendationLower:'Снизьте цену', recommendationWaitOrLower:'Цена выше ориентира', recommendationReviewLowPrice:'Цена ниже ориентира', recommendationKeep:'Оставьте цену', recommendationFallback:'Нужна история продаж',
        sessionExpired:'Нужно войти заново.', requestFailed:'Не удалось выполнить запрос.', tooManyAttempts:'Слишком много попыток. Попробуйте позже.', serviceUnavailable:'Сервис аккаунтов временно недоступен.', accessDenied:'Доступ запрещён. Обновите страницу и повторите попытку.', conflict:'Позиция изменилась в другой вкладке. Обновите список и повторите попытку.', validationError:'Проверьте введённые данные.', notFound:'Позиция не найдена.', currentPasswordInvalid:'Неверный текущий пароль.', priceInvalid:'Введите корректную цену больше $0.', feeInvalid:'Введите комиссию от 0 до 50%.', itemUpdated:'Позиция обновлена.', purchaseAdded:'Покупка добавлена.', settingsSaved:'Настройки сохранены.', passwordUpdated:'Пароль изменён. Все сессии завершены — войдите снова.', archiveConfirm:'Удалить позицию из активного инвентаря? Её можно будет восстановить во вкладке «Удалённые».', undoSaleConfirm:'Отменить продажу? Цена продажи и реализованная прибыль будут убраны, а позиция вернётся в активный инвентарь.', logoutFailed:'Не удалось завершить сессию.', inventoryLoadFailed:'Не удалось загрузить инвентарь.'
      },
      en: {
        documentTitle:'My inventory · CSSkinsPrediction', language:'Language', navOverview:'Overview', navDeals:'Deals', navStrategy:'Strategy', navCatalog:'Catalogue', navHistory:'Market history', navKnives:'Knives', navInventory:'My inventory', loading:'Loading…', logout:'Sign out',
        heroEyebrow:'Portfolio and trades', heroTitle:'My inventory', heroLead:'Purchases, listings, profit, and repricing signals in one place.', addPurchase:'+ Add purchase', addTrade:'⇄ Record trade',
        metricInvested:'Invested in active items', metricMarket:'Current valuation', salesHistory:'CSFloat floor, then history', metricUnrealized:'Unrealized P&L', metricRealized:'Realized P&L', afterFee:'after fees', metricStale:'Needs attention', staleHint:'listed for too long',
        csfloatUrl:'CSFloat listing link', csfloatUrlPlaceholder:'https://csfloat.com/item/...', importCsfloat:'Load listing', importHelp:'Paste a link to fill the form. Nothing is saved until you review it and press Save.', importLoading:'Loading listing data from CSFloat…',
        importLoaded:'Listing loaded. The price field contains the current listing ask, not a confirmed purchase amount. Review it and press Save.', importLoadedWithId:'Listing #{id} loaded. The price field contains the current listing ask, not a confirmed purchase amount. Review it and press Save.', importHistorical:'Listing loaded from local history. The price field contains a historical listing ask, not a confirmed purchase amount. Enter the actual price and press Save.', importHistoricalWithId:'Listing #{id} loaded from local history. The price field contains a historical listing ask, not a confirmed purchase amount. Enter the actual price and press Save.', importWithoutPrice:'Listing loaded, but its price is unavailable. Enter the actual purchase price and press Save.', importWithoutPriceWithId:'Listing #{id} loaded, but its price is unavailable. Enter the actual purchase price and press Save.',
        invalidCsfloatUrl:'Paste a full listing link from csfloat.com.', importRateLimited:'Too many listing imports. Wait a moment and try again.', csfloatRateLimited:'CSFloat is temporarily rate limiting requests. Try again shortly.', csfloatListingNotFound:'Listing not found. It may have been purchased or removed.', csfloatNotConfigured:'CSFloat importing is not configured on the server yet.', csfloatInvalidResponse:'CSFloat returned incomplete data for this listing.', csfloatUnavailable:'Could not load the listing from CSFloat. Check the link and try again later.',
        skinName:'Skin name', itemName:'Item name', buyPrice:'Bought for, $', optionalDetails:'Optional details', optionalDetailsHint:'Date, quantity, float, stickers, and listing options', quantity:'Quantity', boughtAt:'Purchase date', strategy:'Strategy', strategyFlip:'Quick flip', strategyInvestment:'Investment', strategyFloat:'Float', strategyStickers:'Stickers', strategyCollection:'Collection', strategyBalanced:'Balanced', strategyManual:'Manual',
        floatValue:'Float', optional:'optional', feePercent:'Fee, %', listedPrice:'Listed for, $', listedAt:'Listing date', stickers:'Stickers', stickersPlaceholder:'Names, positions, or a short description', sourceUrl:'Purchase link', notes:'Note', notesPlaceholder:'Pattern, rare float, target profit…', save:'Save', cancel:'Cancel', editPosition:'Edit position', saveChanges:'Save changes',
        positions:'Positions', searchPlaceholder:'Search by name', sortNewest:'Newest first', sortStale:'Stale first', sortProfit:'Highest profit', sortValue:'Highest value', refresh:'Refresh', tabActive:'Active', tabListed:'Listed', tabOwned:'Not listed', tabSold:'Sold', tabArchived:'Deleted', tabAll:'All', inventoryLoading:'Loading inventory…', inventoryUpdating:'Updating prices and positions…', inventoryEmpty:'Nothing here yet. Add a purchase on the left — you can edit or delete it at any time.',
        disclaimer:'Valuation uses the lowest active CSFloat buy-now ask first, then sales history and the saved value. Rare floats, patterns, and valuable stickers may be worth a different amount.',
        trades:'Trades', tradesLoading:'Loading trades…', tradesUpdating:'Updating trades…', tradesEmpty:'No trades yet. Record the first one to add received items to your inventory.', tradesLoadFailed:'Could not load trades.', tradeDisclaimer:'The valuation is fixed on the trade date and compares both sides. Current values of received items are calculated separately from available market data.',
        createTradeTitle:'Record trade', editTradeTitle:'Edit trade', tradeEditorHint:'Add every item on both sides and enter its value at the time of the trade.', tradedAt:'Trade date', tradeNotes:'Note', tradeNotesPlaceholder:'Counterparty, terms, or other details', outgoingTitle:'I give', outgoingHint:'Choose items from the active inventory', incomingTitle:'I receive', incomingHint:'Start typing a name — its image and lowest CSFloat ask will be filled automatically', addOutgoing:'+ Add my item', addIncoming:'+ Add received item',
        tradeItem:'Item', tradeSelectItem:'Choose an item', tradeItemName:'Item name', tradeUnitValue:'Value / item, $', removeItem:'Remove item', outgoingTotal:'Total given', incomingTotal:'Total received', tradeDifference:'Difference', tradeTotalsIncomplete:'Fill in a positive quantity and value for every item to calculate totals.', saveTrade:'Save trade', saveTradeChanges:'Save changes', tradeRecorded:'Trade saved.', tradeUpdated:'Trade updated.', tradeVoided:'Trade removed and inventory refreshed.', editTrade:'Edit', voidTrade:'Delete', tradeVoidConfirm:'Delete this trade? The inventory will be rolled back to its state before the trade.', tradeValidation:'Add at least one item on each side and check every name, quantity, and value.', tradeDuplicateOutgoing:'The same inventory item cannot be added to a trade twice.', tradeNoCandidates:'There are no active inventory items available for a trade.', tradeNotFound:'Trade not found.', tradeHasDependents:'This trade cannot be removed because a received item was changed, sold, or traded again.', tradeQuantityInvalid:'The selected item quantity is no longer available.',
        accountSettings:'Account settings', securityAndPricing:'Profile, security, and calculations', profileSettings:'Profile and calculations', passwordSecurity:'Security', name:'Name', staleThreshold:'Stale listing threshold, days', defaultFee:'Default fee, %', saveSettings:'Save settings', currentPassword:'Current password', newPassword:'New password', changePassword:'Change password and sign out other devices', markSold:'Mark as sold', editSaleTitle:'Edit sale', soldPrice:'Sale price per item, $', soldAt:'Sale date', saleFee:'Sale fee, %', saveSale:'Save sale', saveSaleChanges:'Save changes',
        activeItems_one:'{count} item', activeItems_other:'{count} items', salesCount_one:'{count} sale', salesCount_other:'{count} sales', positionsCount_one:'{count} position', positionsCount_other:'{count} positions', tradesCount_one:'{count} trade', tradesCount_other:'{count} trades', holding:'{quantity} pcs · {days} days',
        statusListed:'Listed', statusSold:'Sold', statusArchived:'Deleted', statusOwned:'In inventory', priceSold:'Sold / item', priceListed:'Listed / item', priceMarketHistory:'Market history / item', priceCsfloatFloor:'CSFloat floor / item', priceSavedManual:'Saved value / item', soldOn:'sold {date}', saleDateMissing:'sale date not provided', listedSince:'since {date}', listedDateMissing:'listing date not provided', dataAsOf:'data as of {date}', noMarketData:'no market data', restore:'Restore', edit:'Edit', editSale:'Edit sale', undoSale:'Undo sale', delete:'Delete', soldAction:'Sold', staleChip:'Time to review price', specialPremium:'Float / stickers', receivedByTrade:'Received in trade', valuationCsfloatFloor:'CSFloat floor', valuationSavedManual:'Saved valuation', valuationSalesHistory:'Market history', purchaseEach:'Purchase / item', recommendationEach:'Recommendation / item', result:'Result', pnlAfterFee:'P&L after fees',
        itemTypeSkin:'Skin', itemTypeKnife:'Knife', itemTypeGlove:'Gloves', itemTypeSticker:'Sticker', itemTypeCase:'Case', itemTypeCapsule:'Capsule', itemTypeSouvenir:'Souvenir package', itemTypeContainer:'Container', itemTypeUnknown:'CS2 item', priceSourceCsfloatFloor:'lowest active CSFloat listing', priceSourceSavedManual:'saved / manual valuation', priceSourceSalesHistory:'market history', priceSourceUnknown:'price source unavailable', catalogLowestAsk:'Lowest active listing: {price}', catalogPriceUnavailable:'No automatic price found — enter a value manually.', catalogLookupUnavailable:'Could not refresh the automatic price — you can enter a value manually.', activeOffers_one:'{count} active listing', activeOffers_other:'{count} active listings',
        recommendationUnavailable:'Not enough market data', recommendationManualReview:'Review the premium manually', recommendationReviewLossOrWait:'Break-even is above market', recommendationList:'Ready to list', recommendationLower:'Lower the price', recommendationWaitOrLower:'Price is above benchmark', recommendationReviewLowPrice:'Price is below benchmark', recommendationKeep:'Keep the price', recommendationFallback:'Sales history required',
        sessionExpired:'Please sign in again.', requestFailed:'The request could not be completed.', tooManyAttempts:'Too many attempts. Try again later.', serviceUnavailable:'The account service is temporarily unavailable.', accessDenied:'Access denied. Refresh the page and try again.', conflict:'This position changed in another tab. Refresh the list and try again.', validationError:'Check the entered data.', notFound:'Position not found.', currentPasswordInvalid:'The current password is incorrect.', priceInvalid:'Enter a valid price greater than $0.', feeInvalid:'Enter a fee between 0 and 50%.', itemUpdated:'Position updated.', purchaseAdded:'Purchase added.', settingsSaved:'Settings saved.', passwordUpdated:'Password changed. All sessions ended — sign in again.', archiveConfirm:'Remove this position from active inventory? You can restore it from the Deleted tab.', undoSaleConfirm:'Undo this sale? The sale price and realized profit will be removed, and the position will return to active inventory.', logoutFailed:'Could not end the session.', inventoryLoadFailed:'Could not load the inventory.'
      }
    };
    const state = {csrf:'', user:null, items:[], summary:{}, inventoryLoaded:false, inventoryLoading:false, inventoryError:false, status:'active', search:'', sort:'newest', editing:null, editingRevision:null, editingStickers:null, editingStickerText:'', importedStickers:null, importedStickerText:'', importedSourceUrl:'', importSerial:0,trades:[],tradesLoaded:false,tradesLoading:false,tradesError:false,tradeCandidates:[],tradeCandidatesLoaded:false,tradeEditing:null,tradeEditingRevision:null,tradeLegSerial:0};
    const $ = selector => document.querySelector(selector);
    const VALID_LOCALES = new Set(Object.keys(I18N));
    const cookieValue = name => document.cookie.split(';').map(value=>value.trim()).find(value=>value.startsWith(`${name}=`))?.slice(name.length+1) || '';
    function storedLocale(){
      let cookie='';try{cookie=decodeURIComponent(cookieValue('cs2_locale'));}catch(_){}
      let local=''; try{local=localStorage.getItem('cs2_locale')||'';}catch(_){}
      const server=document.documentElement.lang;
      return VALID_LOCALES.has(cookie)?cookie:(VALID_LOCALES.has(server)?server:(VALID_LOCALES.has(local)?local:'ru'));
    }
    let locale=storedLocale();
    const tr = (key, values={}) => (I18N[locale]?.[key] ?? I18N.ru[key] ?? key).replace(/\{(\w+)\}/g,(_,name)=>String(values[name]??''));
    function plural(key,count){const form=new Intl.PluralRules(locale).select(Number(count)||0);return tr(`${key}_${form}`,{count});}
    function persistLocale(value){
      document.cookie=`cs2_locale=${encodeURIComponent(value)}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol==='https:'?'; Secure':''}`;
      try{localStorage.setItem('cs2_locale',value);}catch(_){}
    }
    function syncEditorLabels(){
      $('#formTitle').textContent=tr(state.editing?'editPosition':'addPurchase').replace(/^\+\s*/, '');
      $('#saveButton').textContent=tr(state.editing?'saveChanges':'save');
    }
    function syncSaleDialogLabels(){
      const editing=$('#soldForm').dataset.action==='update_sale';
      const title=$('#soldDialogTitle'),button=$('#saveSaleButton');
      title.dataset.i18n=editing?'editSaleTitle':'markSold';title.textContent=tr(title.dataset.i18n);
      button.dataset.i18n=editing?'saveSaleChanges':'saveSale';button.textContent=tr(button.dataset.i18n);
    }
    function syncTradeDialogLabels(){
      const title=$('#tradeDialogTitle'),button=$('#saveTradeButton'),editing=state.tradeEditing!==null;
      title.dataset.i18n=editing?'editTradeTitle':'createTradeTitle';title.textContent=tr(title.dataset.i18n);
      button.dataset.i18n=editing?'saveTradeChanges':'saveTrade';button.textContent=tr(button.dataset.i18n);
    }
    function applyLocale(value,{persist=true}={}){
      locale=VALID_LOCALES.has(value)?value:'ru'; document.documentElement.lang=locale; document.title=tr('documentTitle');
      document.querySelectorAll('[data-i18n]').forEach(node=>{node.textContent=tr(node.dataset.i18n);});
      document.querySelectorAll('[data-i18n-placeholder]').forEach(node=>{node.placeholder=tr(node.dataset.i18nPlaceholder);});
      document.querySelectorAll('[data-i18n-aria-label]').forEach(node=>{node.setAttribute('aria-label',tr(node.dataset.i18nAriaLabel));});
      $('#localeSelect').value=locale; if(persist)persistLocale(locale); syncEditorLabels(); syncSaleDialogLabels(); syncTradeDialogLabels();
      document.querySelectorAll('.feedback').forEach(node=>setFeedback(node,''));
      if(state.user)$('#userName').textContent=state.user.display_name||state.user.email;
      renderMetrics(state.summary);
      if(state.inventoryLoading)$('#inventoryList').innerHTML=`<div class="empty">${esc(tr('inventoryUpdating'))}</div>`;else if(state.inventoryError)$('#inventoryList').innerHTML=`<div class="empty">${esc(tr('inventoryLoadFailed'))}</div>`;else if(state.inventoryLoaded)renderItems();
      if(state.tradesLoading)$('#tradeList').innerHTML=`<div class="empty">${esc(tr('tradesUpdating'))}</div>`;else if(state.tradesError)$('#tradeList').innerHTML=`<div class="empty">${esc(tr('tradesLoadFailed'))}</div>`;else if(state.tradesLoaded)renderTrades();
      updateTradeTotals();
    }
    const hasNumber = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
    const money = cents => hasNumber(cents) ? new Intl.NumberFormat(locale==='ru'?'ru-RU':'en-US',{style:'currency',currency:'USD'}).format(Number(cents)/100) : '—';
    const signedMoney = cents => hasNumber(cents) ? `${Number(cents)>=0?'+':'−'}${money(Math.abs(Number(cents)))}` : '—';
    const esc = value => String(value??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
    const today = () => { const value=new Date(); value.setMinutes(value.getMinutes()-value.getTimezoneOffset()); return value.toISOString().slice(0,10); };
    const shortDate = value => {const parts=String(value??'').slice(0,10).split('-').map(Number);if(parts.length!==3||parts.some(part=>!Number.isInteger(part)))return '';return new Date(parts[0],parts[1]-1,parts[2]).toLocaleDateString(locale==='ru'?'ru-RU':'en-US');};
    const toDollars = cents => hasNumber(cents) ? (Number(cents)/100).toFixed(2) : '';
    const priceToCents = value => { const text=String(value??'').trim().replace(',','.');if(!text)return null;const n=Number(text),cents=Math.round(n*100);return Number.isFinite(n)&&cents>0?cents:null; };
    const localizedError = key => Object.assign(new Error(tr(key)),{localized:true});
    const displayError = error => error?.localized ? error.message : tr('requestFailed');
    function responseErrorKey(message,status){
      const exact={'invalid current password':'currentPasswordInvalid','inventory item was changed in another request':'conflict','inventory item not found':'notFound','trade not found':'tradeNotFound','inventory trade not found':'tradeNotFound','trade has dependent inventory changes':'tradeHasDependents','insufficient inventory quantity':'tradeQuantityInvalid','invalid CSRF token':'accessDenied','unauthorized':'sessionExpired','invalid or expired session':'sessionExpired','account service is unavailable':'serviceUnavailable'};
      const known=exact[String(message||'').trim()];if(known)return known;
      return status===409?'conflict':status===404?'notFound':status===403?'accessDenied':status===429?'tooManyAttempts':status===503?'serviceUnavailable':status===400?'validationError':'requestFailed';
    }
    function responseErrorCodeKey(code){return {invalid_csfloat_url:'invalidCsfloatUrl',import_rate_limited:'importRateLimited',csfloat_rate_limited:'csfloatRateLimited',csfloat_listing_not_found:'csfloatListingNotFound',csfloat_not_configured:'csfloatNotConfigured',csfloat_invalid_response:'csfloatInvalidResponse',csfloat_unavailable:'csfloatUnavailable',trade_not_found:'tradeNotFound',trade_has_dependents:'tradeHasDependents',trade_dependency_conflict:'tradeHasDependents',insufficient_quantity:'tradeQuantityInvalid',duplicate_outgoing_item:'tradeDuplicateOutgoing'}[String(code||'')]||'';}
    function tone(node,value){if(!hasNumber(value)){delete node.dataset.tone;return;}node.dataset.tone=Number(value)>=0?'good':'bad';}
    async function api(path, options={}) {
      const headers = {...(options.headers||{})};
      if (options.body && typeof options.body !== 'string') { headers['Content-Type']='application/json'; options.body=JSON.stringify(options.body); }
      if ((options.method||'GET')!=='GET') { headers['X-CSRF-Token']=state.csrf; headers['X-CS2-Action']='account'; }
      let response;try{response=await fetch(path,{credentials:'same-origin',cache:'no-store',...options,headers});}catch(error){if(error?.name==='AbortError')throw error;throw localizedError('requestFailed');}
      if(response.status===401){ location.assign(`/login?next=${encodeURIComponent(location.pathname+location.search)}`); throw localizedError('sessionExpired'); }
      const payload=await response.json().catch(()=>({})); if(!response.ok) throw localizedError(responseErrorCodeKey(payload.error_code)||responseErrorKey(payload.error,response.status)); return payload;
    }
    function setFeedback(node,message,toneName=''){node.textContent=message;if(toneName)node.dataset.tone=toneName;else delete node.dataset.tone;}
    function itemStatus(item){return tr(item.status==='listed'?'statusListed':item.status==='sold'?'statusSold':item.status==='archived'?'statusArchived':'statusOwned');}
    function strategyLabel(strategy){const key={flip:'strategyFlip',investment:'strategyInvestment',float:'strategyFloat',stickers:'strategyStickers',collection:'strategyCollection',balanced:'strategyBalanced',manual:'strategyManual'}[strategy]||'strategyManual';return tr(key);}
    function recommendationText(pricing){const key={unavailable:'recommendationUnavailable',manual_review:'recommendationManualReview',review_loss_or_wait:'recommendationReviewLossOrWait',list:'recommendationList',lower:'recommendationLower',wait_or_lower:'recommendationWaitOrLower',review_low_price:'recommendationReviewLowPrice',keep:'recommendationKeep'}[pricing.action]||'recommendationFallback';return tr(key);}
    function valuationPriceKey(pricing={}){if(pricing.valuation_is_live_ask||String(pricing.valuation_source||'').startsWith('csfloat_'))return 'priceCsfloatFloor';if(pricing.valuation_is_manual||pricing.valuation_source==='saved_valuation')return 'priceSavedManual';return 'priceMarketHistory';}
    function valuationChipKey(pricing={}){if(pricing.valuation_is_live_ask||String(pricing.valuation_source||'').startsWith('csfloat_'))return 'valuationCsfloatFloor';if(pricing.valuation_is_manual||pricing.valuation_source==='saved_valuation')return 'valuationSavedManual';if(pricing.valuation_source==='historical_market')return 'valuationSalesHistory';return '';}
    function renderMetrics(summary={}){
      $('#metricInvested').textContent=money(summary.invested_cents); $('#metricActiveCount').textContent=plural('activeItems',summary.active_count??0);
      $('#metricMarket').textContent=money(summary.market_value_cents); $('#metricUnrealized').textContent=signedMoney(summary.unrealized_profit_cents); tone($('#unrealizedCard'),summary.unrealized_profit_cents);
      $('#metricRealized').textContent=signedMoney(summary.realized_profit_cents); tone($('#realizedCard'),summary.realized_profit_cents); $('#metricSoldCount').textContent=plural('salesCount',summary.sold_count??0);
      $('#metricStale').textContent=String(summary.stale_count??0);
    }
    function renderItems(){
      $('#resultCount').textContent=plural('positionsCount',state.items.length);
      if(!state.items.length){$('#inventoryList').innerHTML=`<div class="empty">${esc(tr('inventoryEmpty'))}</div>`;return;}
      $('#inventoryList').innerHTML=state.items.map(item=>{
        const p=item.pricing||{}; const status=itemStatus(item); const profit=item.status==='sold'?item.realized_profit_cents:p.unrealized_profit_cents; const roi=item.status==='sold'?item.analysis?.roi_percent:p.unrealized_roi_percent; const profitTone=hasNumber(profit)?(Number(profit)>=0?'good':'bad'):''; const stale=item.status==='listed'&&Boolean(p.is_stale);
        const image=item.icon_url?`<img class="skin-img" src="${esc(item.icon_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">`:'<div class="skin-fallback">CS</div>';
        const priceLabel=tr(item.status==='sold'?'priceSold':item.status==='listed'?'priceListed':valuationPriceKey(p));
        const priceValue=item.status==='sold'?item.sold_price_cents:item.status==='listed'?item.listed_price_cents:p.market_median_cents;
        const saleDate=shortDate(item.sold_at),listedDate=shortDate(item.listed_at),marketDate=shortDate(p.market_as_of)||String(p.market_as_of||'');
        const offerMeta=hasNumber(p.active_quantity)?plural('activeOffers',p.active_quantity):'',priceMeta=item.status==='sold'?(saleDate?tr('soldOn',{date:saleDate}):tr('saleDateMissing')):item.status==='listed'?(listedDate?tr('listedSince',{date:listedDate}):tr('listedDateMissing')):([marketDate?tr('dataAsOf',{date:marketDate}):tr('noMarketData'),offerMeta].filter(Boolean).join(' · '));
        const valuationChip=valuationChipKey(p);
        const editButton=`<button data-action="edit" data-id="${item.id}">${esc(tr('edit'))}</button>`,deleteButton=`<button class="danger" data-action="archive" data-id="${item.id}">${esc(tr('delete'))}</button>`;
        const saleButtons=`<button data-action="edit_sale" data-id="${item.id}">${esc(tr('editSale'))}</button><button data-action="undo_sale" data-id="${item.id}">${esc(tr('undoSale'))}</button>`;
        const actionButtons=item.status==='archived'?`<button data-action="restore" data-id="${item.id}">${esc(tr('restore'))}</button>`:item.status==='sold'?`${editButton}${saleButtons}${deleteButton}`:`${editButton}<button data-action="sold" data-id="${item.id}">${esc(tr('soldAction'))}</button>${deleteButton}`;
        return `<article class="item" data-id="${item.id}">${image}<div><h3>${esc(item.skin_name)}</h3><div class="chips"><span class="chip">${esc(status)}</span><span class="chip">${esc(strategyLabel(item.strategy))}</span>${item.acquisition_type==='trade'?`<span class="chip good">${esc(tr('receivedByTrade'))}</span>`:''}${valuationChip?`<span class="chip">${esc(tr(valuationChip))}</span>`:''}${stale?`<span class="chip warn">${esc(tr('staleChip'))}</span>`:''}${p.manual_premium_required?`<span class="chip">${esc(tr('specialPremium'))}</span>`:''}</div></div><div class="value"><span>${esc(tr('purchaseEach'))}</span><strong>${money(item.buy_price_cents)}</strong><small>${esc(tr('holding',{quantity:item.quantity||1,days:item.holding_days??0}))}</small></div><div class="value"><span>${esc(priceLabel)}</span><strong>${money(priceValue)}</strong><small>${esc(priceMeta)}</small></div><div class="value"><span>${esc(tr('recommendationEach'))}</span><strong>${money(p.recommended_price_cents)}</strong><small>${esc(recommendationText(p))}</small></div><div><div class="value"><span>${esc(tr(item.status==='sold'?'result':'pnlAfterFee'))}</span><strong class="profit ${profitTone}">${signedMoney(profit)}</strong><small>${hasNumber(roi)?`${Number(roi).toFixed(1)}%`:'—'}</small></div><div class="actions">${actionButtons}</div></div></article>`;
      }).join('');
    }
    async function loadInventory(){
      state.inventoryLoading=true;state.inventoryError=false;$('#inventoryList').innerHTML=`<div class="empty">${esc(tr('inventoryUpdating'))}</div>`;
      try{const params=new URLSearchParams({status:state.status,search:state.search,sort:state.sort,limit:'200'});const data=await api(`/api/account/inventory?${params}`);state.items=data.items||[];state.summary=data.summary||{};state.inventoryLoaded=true;renderMetrics(state.summary);renderItems();}
      catch(error){state.inventoryError=true;$('#inventoryList').innerHTML=`<div class="empty">${esc(tr('inventoryLoadFailed'))}</div>`;throw error;}finally{state.inventoryLoading=false;}
    }
    function tradeLegsValue(legs){
      if(!Array.isArray(legs)||!legs.length)return null;let total=0;
      for(const leg of legs){const quantity=Number(leg?.quantity),unit=Number(leg?.unit_value_cents);if(!Number.isInteger(quantity)||quantity<1||!Number.isFinite(unit)||unit<=0)return null;total+=quantity*unit;}
      return total;
    }
    const tradeLegName=leg=>String(leg?.skin_name||leg?.item?.skin_name||leg?.current_item?.skin_name||'').trim();
    const tradeLegRevision=leg=>leg?.expected_revision??leg?.inventory_revision??leg?.current_item?.revision;
    function renderTradeLegs(legs){
      if(!Array.isArray(legs)||!legs.length)return '<div class="trade-card-leg">—</div>';
      return legs.map(leg=>`<div class="trade-card-leg"><strong>${esc(tradeLegName(leg)||tr('tradeItem'))}</strong> × ${Number(leg.quantity)||0} · ${money(leg.unit_value_cents)}</div>`).join('');
    }
    function renderTrades(){
      $('#tradeResultCount').textContent=plural('tradesCount',state.trades.length);
      if(!state.trades.length){$('#tradeList').innerHTML=`<div class="empty">${esc(tr('tradesEmpty'))}</div>`;return;}
      $('#tradeList').innerHTML=state.trades.map(trade=>{
        const outgoing=hasNumber(trade.outgoing_value_cents)?Number(trade.outgoing_value_cents):hasNumber(trade.totals?.outgoing_value_cents)?Number(trade.totals.outgoing_value_cents):tradeLegsValue(trade.outgoing),incoming=hasNumber(trade.incoming_value_cents)?Number(trade.incoming_value_cents):hasNumber(trade.totals?.incoming_value_cents)?Number(trade.totals.incoming_value_cents):tradeLegsValue(trade.incoming),difference=hasNumber(outgoing)&&hasNumber(incoming)?incoming-outgoing:null,date=shortDate(trade.traded_at)||String(trade.traded_at||'—'),differenceTone=hasNumber(difference)?(Number(difference)>=0?'good':'bad'):'';
        return `<article class="trade-card" data-trade-id="${esc(trade.id)}"><div><h3>${esc(date)}</h3><div class="trade-card-date">#${esc(trade.id)}${trade.notes?` · ${esc(trade.notes)}`:''}</div></div><div class="trade-card-sides"><div class="trade-card-side"><span>${esc(tr('outgoingTitle'))}</span>${renderTradeLegs(trade.outgoing)}</div><div class="trade-arrow" aria-hidden="true">→</div><div class="trade-card-side"><span>${esc(tr('incomingTitle'))}</span>${renderTradeLegs(trade.incoming)}</div></div><div class="trade-card-result"><small>${money(outgoing)} → ${money(incoming)}</small><strong class="profit ${differenceTone}">${signedMoney(difference)}</strong><div class="actions"><button data-trade-action="edit" data-id="${esc(trade.id)}">${esc(tr('editTrade'))}</button><button class="danger" data-trade-action="void" data-id="${esc(trade.id)}">${esc(tr('voidTrade'))}</button></div></div></article>`;
      }).join('');
    }
    async function loadTrades(){
      state.tradesLoading=true;state.tradesError=false;$('#tradeList').innerHTML=`<div class="empty">${esc(tr('tradesUpdating'))}</div>`;
      try{const data=await api('/api/account/trades');state.trades=Array.isArray(data.items)?data.items:[];state.tradesLoaded=true;renderTrades();}
      catch(error){state.tradesError=true;$('#tradeList').innerHTML=`<div class="empty">${esc(tr('tradesLoadFailed'))}</div>`;throw error;}finally{state.tradesLoading=false;}
    }
    async function loadTradeCandidates(){
      state.tradeCandidatesLoaded=false;
      const params=new URLSearchParams({status:'active',search:'',sort:'newest',limit:'200'}),data=await api(`/api/account/inventory?${params}`);
      state.tradeCandidates=Array.isArray(data.items)?data.items:[];state.tradeCandidatesLoaded=true;return state.tradeCandidates;
    }
    function candidateDefaultValue(item){
      for(const value of [item?.pricing?.market_median_cents,item?.market_price_cents,item?.listed_price_cents,item?.buy_price_cents,item?.unit_value_cents])if(hasNumber(value)&&Number(value)>0)return Number(value);
      return null;
    }
    function currentEditedTrade(){return state.trades.find(trade=>String(trade.id)===String(state.tradeEditing))||null;}
    function tradeCandidatePool(){
      const pool=new Map();
      state.tradeCandidates.forEach(item=>pool.set(String(item.id),{...item,id:item.id,quantity:Number(item.quantity)||1,revision:item.revision,default_value_cents:candidateDefaultValue(item)}));
      const trade=currentEditedTrade();
      (trade?.outgoing||[]).forEach(leg=>{const key=String(leg.inventory_item_id),existing=pool.get(key),quantity=(Number(existing?.quantity)||0)+(Number(leg.quantity)||1);pool.set(key,{...(existing||{}),id:leg.inventory_item_id,skin_name:tradeLegName(leg)||existing?.skin_name||tr('tradeItem'),quantity,revision:tradeLegRevision(leg)??existing?.revision,default_value_cents:candidateDefaultValue(existing)||leg.unit_value_cents});});
      return Array.from(pool.values());
    }
    function selectedOutgoingIds(){return new Set(Array.from(document.querySelectorAll('#outgoingLegs [name="inventory_item_id"]')).map(node=>String(node.value||'')).filter(Boolean));}
    function syncOutgoingRow(row,{resetValue=false}={}){
      const select=row.querySelector('[name="inventory_item_id"]'),quantity=row.querySelector('[name="quantity"]'),value=row.querySelector('[name="unit_value"]'),option=select.selectedOptions[0],available=Number(option?.dataset.quantity)||0;
      quantity.max=String(Math.max(available,1));quantity.disabled=!option?.value;value.disabled=!option?.value;
      if(resetValue){quantity.value='1';value.value=toDollars(option?.dataset.valueCents);}
      updateTradeTotals();
    }
    function addOutgoingLeg(leg=null){
      const pool=tradeCandidatePool(),used=selectedOutgoingIds(),fallback=pool.find(item=>!used.has(String(item.id)))||pool[0],selectedId=String(leg?.inventory_item_id??fallback?.id??''),selected=pool.find(item=>String(item.id)===selectedId),quantity=Number(leg?.quantity)||1,unitValue=leg?.unit_value_cents??selected?.default_value_cents,row=document.createElement('div');
      row.className='trade-leg';row.dataset.legKey=String(++state.tradeLegSerial);
      const placeholder=pool.length?tr('tradeSelectItem'):tr('tradeNoCandidates'),options=pool.map(item=>`<option value="${esc(item.id)}" data-revision="${esc(item.revision??'')}" data-quantity="${esc(item.quantity||1)}" data-value-cents="${esc(item.default_value_cents??'')}"${String(item.id)===selectedId?' selected':''}>${esc(item.skin_name)} ×${Number(item.quantity)||1}</option>`).join('');
      row.innerHTML=`<label class="field"><span data-i18n="tradeItem">${esc(tr('tradeItem'))}</span><select name="inventory_item_id" required${pool.length?'':' disabled'}><option value="">${esc(placeholder)}</option>${options}</select></label><label class="field"><span data-i18n="quantity">${esc(tr('quantity'))}</span><input name="quantity" type="number" min="1" max="${esc(selected?.quantity||1)}" value="${esc(quantity)}" required></label><label class="field"><span data-i18n="tradeUnitValue">${esc(tr('tradeUnitValue'))}</span><input name="unit_value" inputmode="decimal" value="${esc(toDollars(unitValue))}" required></label><button class="leg-remove" type="button" data-remove-leg aria-label="${esc(tr('removeItem'))}" title="${esc(tr('removeItem'))}">×</button>`;
      $('#outgoingLegs').appendChild(row);syncOutgoingRow(row);
    }
    const itemLookupState=new WeakMap();
    function lookupStateFor(input){let value=itemLookupState.get(input);if(!value){value={timer:null,controller:null,serial:0,autoValueCents:null};itemLookupState.set(input,value);}return value;}
    function itemKindText(kind){const key={weapon_skin:'itemTypeSkin',knife:'itemTypeKnife',glove:'itemTypeGlove',sticker:'itemTypeSticker',case:'itemTypeCase',capsule:'itemTypeCapsule',souvenir_package:'itemTypeSouvenir',container:'itemTypeContainer'}[String(kind||'')]||'itemTypeUnknown';return tr(key);}
    function itemPriceSourceText(item={}){const source=String(item.price_source||'');if(item.price_is_live_ask||source.startsWith('csfloat_'))return tr('priceSourceCsfloatFloor');if(source==='historical_market')return tr('priceSourceSalesHistory');if(source==='saved_valuation')return tr('priceSourceSavedManual');return tr('priceSourceUnknown');}
    function renderItemPreview(node,item,{lookupFailed=false}={}){
      if(!node)return;if(!item){node.hidden=true;node.innerHTML='';return;}
      const icon=item.icon_url?`<img src="${esc(item.icon_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">`:`<span class="item-preview-fallback">CS2</span>`,offers=hasNumber(item.active_quantity)?plural('activeOffers',item.active_quantity):'',priceMessage=hasNumber(item.current_price_cents)?tr('catalogLowestAsk',{price:money(item.current_price_cents)}):(lookupFailed?tr('catalogLookupUnavailable'):tr('catalogPriceUnavailable')),toneName=hasNumber(item.current_price_cents)?'good':'warn';
      node.innerHTML=`<span class="item-preview-media">${icon}</span><span class="item-preview-copy"><strong>${esc(item.market_hash_name||'')}</strong><span>${esc([itemKindText(item.item_kind),offers].filter(Boolean).join(' · '))}</span><small data-tone="${toneName}">${esc(priceMessage)}${hasNumber(item.current_price_cents)?` · ${esc(itemPriceSourceText(item))}`:''}</small></span>`;node.hidden=false;
    }
    function renderItemSuggestions(datalist,items){if(!datalist)return;datalist.innerHTML=(Array.isArray(items)?items:[]).map(item=>`<option value="${esc(item.market_hash_name)}">${esc(itemKindText(item.item_kind))} · ${money(item.current_price_cents)}</option>`).join('');}
    function exactSuggestedItem(items,value){const folded=String(value||'').trim().toLocaleLowerCase();return (Array.isArray(items)?items:[]).find(item=>String(item.market_hash_name||'').toLocaleLowerCase()===folded)||null;}
    function applyResolvedItem(input,preview,item,{row=null,canonicalize=false}={}){
      if(!item)return;if(canonicalize&&item.market_hash_name)input.value=item.market_hash_name;renderItemPreview(preview,item);
      if(!row||!item.price_is_live_ask||!hasNumber(item.current_price_cents))return;
      const valueInput=row.querySelector('[name="unit_value"]'),lookup=lookupStateFor(input),current=priceToCents(valueInput?.value),canReplace=!String(valueInput?.value||'').trim()||(hasNumber(lookup.autoValueCents)&&current===Number(lookup.autoValueCents));
      if(canReplace&&valueInput){valueInput.value=toDollars(item.current_price_cents);lookup.autoValueCents=Number(item.current_price_cents);updateTradeTotals();}
    }
    async function runItemSearch(input,datalist,preview,{row=null}={}){
      const query=String(input.value||'').trim(),lookup=lookupStateFor(input),serial=++lookup.serial;if(lookup.controller)lookup.controller.abort();if(query.length<2){renderItemSuggestions(datalist,[]);renderItemPreview(preview,null);return;}
      const controller=new AbortController();lookup.controller=controller;
      try{const data=await api(`/api/account/item-search?q=${encodeURIComponent(query)}`,{signal:controller.signal});if(serial!==lookup.serial)return;const items=Array.isArray(data.items)?data.items:[];renderItemSuggestions(datalist,items);const exact=exactSuggestedItem(items,input.value);if(exact)applyResolvedItem(input,preview,exact,{row});else renderItemPreview(preview,null);}
      catch(error){if(error?.name!=='AbortError'&&serial===lookup.serial)renderItemPreview(preview,{market_hash_name:query,item_kind:'unknown'},{lookupFailed:true});}
    }
    function scheduleItemSearch(input,datalist,preview,{row=null}={}){const lookup=lookupStateFor(input);clearTimeout(lookup.timer);lookup.timer=setTimeout(()=>runItemSearch(input,datalist,preview,{row}),180);}
    async function quoteExactItem(input,preview,{row=null}={}){
      const name=String(input.value||'').trim();if(!name)return;const lookup=lookupStateFor(input),serial=++lookup.serial;if(lookup.controller)lookup.controller.abort();const controller=new AbortController();lookup.controller=controller;
      try{const data=await api(`/api/account/item-quote?market_hash_name=${encodeURIComponent(name)}`,{signal:controller.signal});if(serial!==lookup.serial)return;const item=data?.item;if(item)applyResolvedItem(input,preview,item,{row,canonicalize:true});}
      catch(error){if(error?.name!=='AbortError'&&serial===lookup.serial)renderItemPreview(preview,{market_hash_name:name,item_kind:'unknown'},{lookupFailed:true});}
    }
    function addIncomingLeg(leg=null){
      const row=document.createElement('div'),legKey=String(++state.tradeLegSerial),listId=`incomingItemSuggestions${legKey}`;row.className='trade-leg';row.dataset.legKey=legKey;row.dataset.tradeSide='incoming';
      row.innerHTML=`<label class="field"><span data-i18n="tradeItemName">${esc(tr('tradeItemName'))}</span><input name="skin_name" list="${listId}" maxlength="255" autocomplete="off" value="${esc(tradeLegName(leg))}" required><datalist id="${listId}"></datalist></label><label class="field"><span data-i18n="quantity">${esc(tr('quantity'))}</span><input name="quantity" type="number" min="1" max="10000" value="${esc(Number(leg?.quantity)||1)}" required></label><label class="field"><span data-i18n="tradeUnitValue">${esc(tr('tradeUnitValue'))}</span><input name="unit_value" inputmode="decimal" value="${esc(toDollars(leg?.unit_value_cents))}" required></label><button class="leg-remove" type="button" data-remove-leg aria-label="${esc(tr('removeItem'))}" title="${esc(tr('removeItem'))}">×</button><div class="item-preview" hidden aria-live="polite"></div>`;
      $('#incomingLegs').appendChild(row);updateTradeTotals();
    }
    function tradeDomTotal(container){
      const rows=Array.from(container.querySelectorAll('.trade-leg'));if(!rows.length)return null;let total=0;
      for(const row of rows){const quantity=Number(row.querySelector('[name="quantity"]')?.value),unit=priceToCents(row.querySelector('[name="unit_value"]')?.value);if(!Number.isInteger(quantity)||quantity<1||unit===null)return null;total+=quantity*unit;}
      return total;
    }
    function updateTradeTotals(){
      const outgoing=tradeDomTotal($('#outgoingLegs')),incoming=tradeDomTotal($('#incomingLegs')),complete=hasNumber(outgoing)&&hasNumber(incoming);
      $('#outgoingTotal').textContent=money(outgoing);$('#incomingTotal').textContent=money(incoming);$('#tradeDifference').textContent=complete?signedMoney(incoming-outgoing):'—';$('#tradeSummaryNote').textContent=complete?'':tr('tradeTotalsIncomplete');
    }
    function resetTradeForm(){
      state.tradeEditing=null;state.tradeEditingRevision=null;const form=$('#tradeForm');form.reset();form.elements.trade_id.value='';form.elements.expected_revision.value='';form.elements.traded_at.value=today();$('#outgoingLegs').innerHTML='';$('#incomingLegs').innerHTML='';addOutgoingLeg();addIncomingLeg();syncTradeDialogLabels();setFeedback($('#tradeFeedback'),'');updateTradeTotals();
    }
    function fillTradeForm(trade){
      state.tradeEditing=trade.id;state.tradeEditingRevision=trade.revision;const form=$('#tradeForm');form.reset();form.elements.trade_id.value=trade.id;form.elements.expected_revision.value=trade.revision;form.elements.traded_at.value=String(trade.traded_at||'').slice(0,10)||today();form.elements.notes.value=trade.notes||'';$('#outgoingLegs').innerHTML='';$('#incomingLegs').innerHTML='';(trade.outgoing||[]).forEach(leg=>addOutgoingLeg(leg));(trade.incoming||[]).forEach(leg=>addIncomingLeg(leg));if(!$('#outgoingLegs').children.length)addOutgoingLeg();if(!$('#incomingLegs').children.length)addIncomingLeg();syncTradeDialogLabels();setFeedback($('#tradeFeedback'),'');updateTradeTotals();
    }
    async function openTradeDialog(id=null){
      try{await loadTradeCandidates();if(id===null)resetTradeForm();else{const trade=state.trades.find(item=>String(item.id)===String(id));if(!trade)throw localizedError('tradeNotFound');fillTradeForm(trade);}$('#tradeDialog').showModal();}
      catch(error){alert(displayError(error));}
    }
    function collectTradeLegs(){
      const outgoing=[],incoming=[],seen=new Set();
      for(const row of document.querySelectorAll('#outgoingLegs .trade-leg')){const select=row.querySelector('[name="inventory_item_id"]'),option=select?.selectedOptions[0],itemId=Number(select?.value),quantity=Number(row.querySelector('[name="quantity"]')?.value),available=Number(option?.dataset.quantity),revision=Number(option?.dataset.revision),unitValue=priceToCents(row.querySelector('[name="unit_value"]')?.value);if(!Number.isInteger(itemId)||itemId<1||!Number.isInteger(revision)||revision<1||!Number.isInteger(quantity)||quantity<1||unitValue===null)return {errorKey:'tradeValidation'};if(Number.isFinite(available)&&quantity>available)return {errorKey:'tradeQuantityInvalid'};if(seen.has(itemId))return {errorKey:'tradeDuplicateOutgoing'};seen.add(itemId);outgoing.push({inventory_item_id:itemId,quantity,expected_revision:revision,unit_value_cents:unitValue});}
      for(const row of document.querySelectorAll('#incomingLegs .trade-leg')){const skinName=String(row.querySelector('[name="skin_name"]')?.value||'').trim(),quantity=Number(row.querySelector('[name="quantity"]')?.value),unitValue=priceToCents(row.querySelector('[name="unit_value"]')?.value);if(!skinName||skinName.length>255||!Number.isInteger(quantity)||quantity<1||unitValue===null)return {errorKey:'tradeValidation'};incoming.push({skin_name:skinName,quantity,unit_value_cents:unitValue});}
      if(!outgoing.length||!incoming.length)return {errorKey:'tradeValidation'};return {outgoing,incoming};
    }
    async function refreshAfterTrade(){
      const results=await Promise.allSettled([loadInventory(),loadTrades(),loadTradeCandidates()]);results.filter(result=>result.status==='rejected').forEach(result=>console.error(result.reason));
    }
    async function submitTrade(event){
      event.preventDefault();const form=event.currentTarget,legs=collectTradeLegs(),feedback=$('#tradeFeedback'),button=$('#saveTradeButton');if(legs.errorKey){setFeedback(feedback,tr(legs.errorKey),'bad');return;}
      const editing=state.tradeEditing!==null,body={action:editing?'update':'create',traded_at:form.elements.traded_at.value,notes:String(form.elements.notes.value||'').trim(),outgoing:legs.outgoing,incoming:legs.incoming};if(editing){body.trade_id=state.tradeEditing;body.expected_revision=state.tradeEditingRevision;}
      button.disabled=true;try{await api('/api/account/trades',{method:'POST',body});$('#tradeDialog').close();await refreshAfterTrade();setFeedback($('#tradeLedgerFeedback'),tr(editing?'tradeUpdated':'tradeRecorded'),'good');}
      catch(error){setFeedback(feedback,displayError(error),'bad');}finally{button.disabled=false;}
    }
    async function voidTrade(id){
      const trade=state.trades.find(item=>String(item.id)===String(id));if(!trade)throw localizedError('tradeNotFound');if(!confirm(tr('tradeVoidConfirm')))return;
      await api('/api/account/trades',{method:'POST',body:{action:'void',trade_id:trade.id,expected_revision:trade.revision}});await refreshAfterTrade();setFeedback($('#tradeLedgerFeedback'),tr('tradeVoided'),'good');
    }
    function resetForm(){state.importSerial+=1;state.editing=null;state.editingRevision=null;state.editingStickers=null;state.editingStickerText='';state.importedStickers=null;state.importedStickerText='';state.importedSourceUrl='';$('#inventoryForm').reset();$('#inventoryForm [name="item_id"]').value='';$('#inventoryForm [name="quantity"]').value='1';$('#inventoryForm [name="bought_at"]').value=today();$('#inventoryForm [name="fee_percent"]').value=state.user?.default_fee_percent??4;renderItemPreview($('#purchaseItemPreview'),null);$('#skinSuggestions').innerHTML='';$('#optionalInventoryDetails').open=false;syncEditorLabels();$('#cancelEdit').hidden=true;$('#importCsfloatButton').disabled=false;$('#importCsfloatButton').textContent=tr('importCsfloat');setFeedback($('#importFeedback'),'');setFeedback($('#formFeedback'),'');}
    function editItem(id){const item=state.items.find(x=>String(x.id)===String(id));if(!item)return;state.importSerial+=1;state.importedStickers=null;state.importedStickerText='';state.importedSourceUrl='';state.editing=item.id;state.editingRevision=item.revision;state.editingStickers=item.stickers;const stickers=Array.isArray(item.stickers)?item.stickers.map(x=>typeof x==='string'?x:(x?.name||'')).filter(Boolean).join(', '):(item.stickers||'');state.editingStickerText=stickers;const form=$('#inventoryForm');for(const [name,value] of Object.entries({item_id:item.id,skin_name:item.skin_name,buy_price:toDollars(item.buy_price_cents),quantity:item.quantity,bought_at:(item.bought_at||'').slice(0,10),strategy:item.strategy,float_value:item.float_value??'',fee_percent:item.fee_percent,listed_price:toDollars(item.listed_price_cents),listed_at:(item.listed_at||'').slice(0,10),stickers,source_url:item.source_url||'',notes:item.notes||''})){const input=form.elements[name];if(input)input.value=value??'';}quoteExactItem(form.elements.skin_name,$('#purchaseItemPreview')).catch(()=>{});$('#optionalInventoryDetails').open=true;$('#importCsfloatButton').disabled=false;$('#importCsfloatButton').textContent=tr('importCsfloat');setFeedback($('#importFeedback'),'');syncEditorLabels();$('#cancelEdit').hidden=false;$('#tradePanel').scrollIntoView({behavior:'smooth',block:'start'});}
    function openSaleDialog(id,action='mark_sold'){
      const item=state.items.find(x=>String(x.id)===String(id));if(!item)return;
      const editing=action==='update_sale',form=$('#soldForm');form.dataset.action=editing?'update_sale':'mark_sold';form.elements.item_id.value=item.id;
      form.elements.sold_price.value=toDollars(editing?item.sold_price_cents:(item.listed_price_cents??item.pricing?.recommended_price_cents));
      form.elements.sold_at.value=editing?String(item.sold_at||'').slice(0,10):today();form.elements.fee_percent.value=item.fee_percent??state.user?.default_fee_percent??4;
      setFeedback(form.querySelector('.feedback'),'');syncSaleDialogLabels();$('#soldDialog').showModal();
    }
    function stickerText(stickers){return (Array.isArray(stickers)?stickers:[]).map(sticker=>typeof sticker==='string'?sticker:String(sticker?.name||'').trim()).filter(Boolean).join(', ');}
    async function importCsfloatListing(){
      const form=$('#inventoryForm'),input=form.elements.source_url,feedback=$('#importFeedback'),button=$('#importCsfloatButton'),url=String(input.value||'').trim();
      if(!url){setFeedback(feedback,tr('invalidCsfloatUrl'),'bad');return;}
      const serial=++state.importSerial;state.importedStickers=null;state.importedStickerText='';state.importedSourceUrl='';button.disabled=true;button.textContent=tr('importLoading');setFeedback(feedback,tr('importLoading'));
      try{
        const data=await api('/api/account/import-csfloat',{method:'POST',body:{url}});if(serial!==state.importSerial)return;
        const listing=data?.listing||{},name=String(listing.skin_name||'').trim();if(!name)throw localizedError('csfloatInvalidResponse');
        const canonicalUrl=String(listing.source_url||url).trim(),stickers=Array.isArray(listing.stickers)?listing.stickers:[],names=stickerText(stickers),priceAvailable=hasNumber(listing.buy_price_cents)&&Number(listing.buy_price_cents)>0;
        form.elements.skin_name.value=name;form.elements.source_url.value=canonicalUrl;form.elements.buy_price.value=priceAvailable?toDollars(listing.buy_price_cents):'';form.elements.float_value.value=hasNumber(listing.float_value)?listing.float_value:'';form.elements.stickers.value=names;quoteExactItem(form.elements.skin_name,$('#purchaseItemPreview')).catch(()=>{});
        state.importedStickers=stickers;state.importedStickerText=names;state.importedSourceUrl=canonicalUrl;
        const id=String(listing.listing_id||'').trim(),warningCodes=[...(Array.isArray(data.warning_codes)?data.warning_codes:[]),...(Array.isArray(listing.warning_codes)?listing.warning_codes:[])],historicalPrice=listing.price_kind==='historical_listing_ask'||listing.source==='local_history'||warningCodes.includes('historical_price');
        const messageKey=!priceAvailable?(id?'importWithoutPriceWithId':'importWithoutPrice'):historicalPrice?(id?'importHistoricalWithId':'importHistorical'):(id?'importLoadedWithId':'importLoaded');
        setFeedback(feedback,tr(messageKey,{id}),historicalPrice||!priceAvailable?'warn':'good');
      }catch(error){if(serial===state.importSerial)setFeedback(feedback,displayError(error),'bad');}
      finally{if(serial===state.importSerial){button.disabled=false;button.textContent=tr('importCsfloat');}}
    }
    async function submitInventory(event){event.preventDefault();const form=event.currentTarget,fd=new FormData(form);const editing=Boolean(state.editing);const body={action:editing?'update':'create'};for(const [k,v] of fd.entries())body[k]=v;if(editing)body.expected_revision=state.editingRevision;else delete body.item_id;if(Array.isArray(state.importedStickers)&&String(body.source_url||'').trim()===state.importedSourceUrl&&body.stickers===state.importedStickerText)body.stickers=state.importedStickers;else if(editing){if(body.stickers===state.editingStickerText)body.stickers=state.editingStickers;}const listedText=String(body.listed_price??'').trim();body.buy_price_cents=priceToCents(body.buy_price);body.listed_price_cents=listedText?priceToCents(listedText):null;delete body.buy_price;delete body.listed_price;if(body.buy_price_cents===null||(listedText&&body.listed_price_cents===null)){if(listedText)$('#optionalInventoryDetails').open=true;setFeedback($('#formFeedback'),tr('priceInvalid'),'bad');return;}if(!body.listed_at)body.listed_at=null;const button=$('#saveButton');button.disabled=true;try{await api('/api/account/inventory',{method:'POST',body});resetForm();setFeedback($('#formFeedback'),tr(editing?'itemUpdated':'purchaseAdded'),'good');await loadInventory();}catch(error){setFeedback($('#formFeedback'),displayError(error),'bad');}finally{button.disabled=false;}}
    async function inventoryAction(action,id,extra={}){const current=state.items.find(x=>String(x.id)===String(id));await api('/api/account/inventory',{method:'POST',body:{action,item_id:id,...(current?{expected_revision:current.revision}:{}),...extra}});await loadInventory();}
    async function bootstrap(){
      try{state.csrf=decodeURIComponent(cookieValue('cs2_csrf'));if(!state.csrf){location.assign('/login?reauth=1&next=%2Faccount');return;}const me=await api('/api/auth/me');state.user=me.user;$('#userName').removeAttribute('data-i18n');$('#userName').textContent=me.user.display_name||me.user.email;$('#avatar').textContent=(me.user.display_name||me.user.email||'?').slice(0,1).toUpperCase();$('#profileForm [name="display_name"]').value=me.user.display_name||'';$('#profileForm [name="stale_after_days"]').value=me.user.stale_after_days??7;$('#profileForm [name="default_fee_percent"]').value=me.user.default_fee_percent??4;resetForm();resetTradeForm();const results=await Promise.allSettled([loadInventory(),loadTrades(),loadTradeCandidates()]);results.filter(result=>result.status==='rejected').forEach(result=>console.error(result.reason));}catch(error){if(!location.pathname.startsWith('/login'))console.error(error);}
    }
    const purchaseNameInput=$('#inventoryForm [name="skin_name"]');purchaseNameInput.addEventListener('input',()=>scheduleItemSearch(purchaseNameInput,$('#skinSuggestions'),$('#purchaseItemPreview')));purchaseNameInput.addEventListener('change',()=>quoteExactItem(purchaseNameInput,$('#purchaseItemPreview')).catch(()=>{}));
    const sourceUrlInput=$('#sourceUrlInput');
    sourceUrlInput.addEventListener('input',event=>{const value=String(event.target.value||'').trim();if(value===state.importedSourceUrl)return;state.importSerial+=1;state.importedStickers=null;state.importedStickerText='';state.importedSourceUrl='';$('#importCsfloatButton').disabled=false;$('#importCsfloatButton').textContent=tr('importCsfloat');setFeedback($('#importFeedback'),'');});
    sourceUrlInput.addEventListener('paste',()=>setTimeout(()=>{if(String(sourceUrlInput.value||'').trim())importCsfloatListing();},0));
    $('#importCsfloatButton').addEventListener('click',importCsfloatListing);
    $('#inventoryForm').addEventListener('submit',submitInventory);$('#inventoryForm').addEventListener('invalid',event=>{if($('#optionalInventoryDetails').contains(event.target))$('#optionalInventoryDetails').open=true;},true);$('#cancelEdit').addEventListener('click',resetForm);$('#focusAdd').addEventListener('click',()=>{$('#tradePanel').scrollIntoView({behavior:'smooth'});sourceUrlInput.focus();});
    $('#statusTabs').addEventListener('click',event=>{const button=event.target.closest('[data-status]');if(!button)return;state.status=button.dataset.status;document.querySelectorAll('.status-tab').forEach(x=>x.classList.toggle('active',x===button));loadInventory().catch(console.error);});
    $('#sortSelect').addEventListener('change',event=>{state.sort=event.target.value;loadInventory().catch(console.error);});$('#refreshButton').addEventListener('click',()=>loadInventory().catch(console.error));
    let inventorySearchTimer;$('#searchInput').addEventListener('input',event=>{clearTimeout(inventorySearchTimer);inventorySearchTimer=setTimeout(()=>{state.search=event.target.value.trim();loadInventory().catch(console.error);},250);});
    $('#inventoryList').addEventListener('click',async event=>{const button=event.target.closest('[data-action]');if(!button)return;const {action,id}=button.dataset;try{if(action==='edit')return editItem(id);if(action==='sold')return openSaleDialog(id);if(action==='edit_sale')return openSaleDialog(id,'update_sale');if(action==='archive'&&!confirm(tr('archiveConfirm')))return;if(action==='undo_sale'&&!confirm(tr('undoSaleConfirm')))return;await inventoryAction(action,id);}catch(error){alert(displayError(error));}});
    $('#soldForm').addEventListener('submit',async event=>{const submitter=event.submitter;if(submitter?.value==='cancel')return;event.preventDefault();const form=event.currentTarget,feedback=form.querySelector('.feedback'),button=$('#saveSaleButton'),soldPrice=priceToCents(form.elements.sold_price.value),feePercent=Number(String(form.elements.fee_percent.value||'').trim().replace(',','.'));if(soldPrice===null){setFeedback(feedback,tr('priceInvalid'),'bad');return;}if(!Number.isFinite(feePercent)||feePercent<0||feePercent>50){setFeedback(feedback,tr('feeInvalid'),'bad');return;}button.disabled=true;try{const action=form.dataset.action==='update_sale'?'update_sale':'mark_sold';await inventoryAction(action,form.elements.item_id.value,{sold_price_cents:soldPrice,sold_at:form.elements.sold_at.value,fee_percent:feePercent});$('#soldDialog').close();setFeedback(feedback,'');}catch(error){setFeedback(feedback,displayError(error),'bad');}finally{button.disabled=false;}});
    $('#focusTrade').addEventListener('click',()=>openTradeDialog());$('#addTradeButton').addEventListener('click',()=>openTradeDialog());
    $('#addOutgoingLeg').addEventListener('click',()=>addOutgoingLeg());$('#addIncomingLeg').addEventListener('click',()=>addIncomingLeg());
    $('#tradeForm').addEventListener('input',event=>{const row=event.target.closest('.trade-leg');if(row?.dataset.tradeSide==='incoming'&&event.target.matches('[name="skin_name"]'))scheduleItemSearch(event.target,row.querySelector('datalist'),row.querySelector('.item-preview'),{row});if(row?.dataset.tradeSide==='incoming'&&event.target.matches('[name="unit_value"]'))lookupStateFor(row.querySelector('[name="skin_name"]')).autoValueCents=null;updateTradeTotals();});$('#tradeForm').addEventListener('change',event=>{const row=event.target.closest('.trade-leg');if(event.target.matches('[name="inventory_item_id"]'))syncOutgoingRow(row,{resetValue:true});else if(row?.dataset.tradeSide==='incoming'&&event.target.matches('[name="skin_name"]'))quoteExactItem(event.target,row.querySelector('.item-preview'),{row}).catch(()=>{});else updateTradeTotals();});
    $('#tradeForm').addEventListener('click',event=>{const button=event.target.closest('[data-remove-leg]');if(!button)return;const row=button.closest('.trade-leg'),input=row?.querySelector('[name="skin_name"]'),lookup=input?itemLookupState.get(input):null;if(lookup?.controller)lookup.controller.abort();row?.remove();updateTradeTotals();});
    $('#tradeForm').addEventListener('submit',submitTrade);$('#cancelTradeButton').addEventListener('click',()=>$('#tradeDialog').close());
    $('#tradeList').addEventListener('click',async event=>{const button=event.target.closest('[data-trade-action]');if(!button)return;const {tradeAction,id}=button.dataset;if(tradeAction==='edit'){await openTradeDialog(id);return;}button.disabled=true;try{if(tradeAction==='void')await voidTrade(id);}catch(error){alert(displayError(error));}finally{if(button.isConnected)button.disabled=false;}});
    $('#profileForm').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,feedback=form.querySelector('.feedback');try{const data=Object.fromEntries(new FormData(form));const result=await api('/api/account/profile',{method:'POST',body:{action:'update_profile',...data}});state.user=result.user||state.user;setFeedback(feedback,tr('settingsSaved'),'good');}catch(error){setFeedback(feedback,displayError(error),'bad');}});
    $('#passwordForm').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,feedback=form.querySelector('.feedback');try{await api('/api/account/profile',{method:'POST',body:{action:'change_password',...Object.fromEntries(new FormData(form))}});form.reset();setFeedback(feedback,tr('passwordUpdated'),'good');setTimeout(()=>location.assign('/login?password_changed=1'),700);}catch(error){setFeedback(feedback,displayError(error),'bad');}});
    $('#logoutButton').addEventListener('click',async()=>{try{await api('/api/auth/logout',{method:'POST',body:{}});location.assign('/login');}catch(_){alert(tr('logoutFailed'));}});
    $('#localeSelect').addEventListener('change',event=>applyLocale(event.target.value));
    applyLocale(locale);
    bootstrap();
  </script>
</body>
</html>
"""


def _normalise_page_locale(locale: str) -> str:
    return locale if locale in {"ru", "en"} else "ru"


def render_auth_page(mode: str, locale: str = "ru") -> str:
    """Render one of the fixed public authentication modes."""

    if mode not in {"login", "register", "forgot", "reset"}:
        raise ValueError("unknown authentication page")
    return AUTH_HTML.replace("__AUTH_MODE__", mode).replace(
        "__LOCALE__", _normalise_page_locale(locale)
    )


def render_account_page(locale: str = "ru") -> str:
    return ACCOUNT_HTML.replace("__LOCALE__", _normalise_page_locale(locale))
