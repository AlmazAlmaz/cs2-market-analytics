# Установка и запуск проекта на macOS

Ниже — команды именно для этого проекта. Виртуальное окружение Windows на Mac
использовать нельзя: `.venv` всегда создаётся заново для конкретной ОС.

## 1. Первый запуск

Открой Terminal и выполни:

```bash
cd cs2-market-analytics
```

Если Python 3.12 уже установлен и `.venv` не скопирован с Windows, весь раздел
установки можно выполнить одной командой:

```bash
./setup_mac.sh
```

Если `.venv` был скопирован с Windows или повреждён, не удаляй его сразу, а
переименуй:

```bash
mv .venv .venv.from-windows
```

Установи Python 3.12 и создай окружение:

```bash
brew install python@3.12
python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m pip install -r requirements-dev.txt
```

Если Homebrew ещё не установлен, используй инструкцию с
[официального сайта Homebrew](https://brew.sh/), затем повтори команды выше.

Подготовь приватные конфиги, только если их ещё нет:

```bash
test -f config.local.env || cp config.local.env.example config.local.env
test -f hosting.local.env || cp hosting.local.env.example hosting.local.env
chmod 600 config.local.env hosting.local.env
chmod 600 key key2 key3 key4 api_keys_proxies.txt dashboard_token.txt 2>/dev/null || true
```

Проверь окружение:

```bash
./.venv/bin/python -m pip check
./.venv/bin/python -m pytest
```

## 2. Безопасный пробный запуск

Сначала запусти только сайт, без расходования CSFloat API-запросов:

```bash
START_PARSER=0 START_SCANNER=0 START_TREND=0 START_REVERSE_TUNNEL=0 START_API=0 START_TELEGRAM=0 PUBLIC_TUNNEL=0 ./start_all.sh
```

Скрипт дождётся настоящего ответа `/health`, выведет адрес и откроет браузер.
Ссылка с токеном используется один раз: сервер записывает `HttpOnly` cookie и
убирает секрет из адресной строки.

Проверка из другого Terminal:

```bash
curl -fsS http://127.0.0.1:8010/health
tail -f logs/dashboard.log
```

Остановить сайт: вернись в первый Terminal и нажми `Ctrl+C`.

## 3. Обычный запуск

Весь заполненный проект вместе:

```bash
cd cs2-market-analytics
./start_all.sh
```

Одна команда выполняет preflight и запускает dashboard, постоянный reverse SSH
до VPS, parser, deal scanner и фоновый Trend worker. Она проверяет локальный и публичный `/health`,
открывает правильную авторизованную ссылку и автоматически восстанавливает SSH
после разрыва. Если домен или TLS временно не прошли проверку, откроется рабочий
локальный адрес, а причина останется в Terminal и логах. Telegram и read-only
export API запускаются в режиме `auto`: только если для них заполнены токен и
безопасные настройки доступа.

Полезные варианты:

```bash
# сайт + scanner, без основного parser
START_PARSER=0 ./start_all.sh

# все сервисы, но не открывать браузер
OPEN_BROWSER=0 ./start_all.sh

# только локальный проект, без VPS
START_REVERSE_TUNNEL=0 ./start_all.sh

# принудительно не запускать необязательные сервисы
START_API=0 START_TELEGRAM=0 ./start_all.sh

# временный случайный trycloudflare URL только для теста
START_REVERSE_TUNNEL=0 PUBLIC_TUNNEL=1 ./start_all.sh
```

`PUBLIC_TUNNEL` специально выключен по умолчанию. Quick Tunnel даёт случайный
адрес, предназначен для тестирования и не связан с твоим доменом.

После запуска интервал поиска выгодных лотов можно менять прямо на сайте:
`Система` → управление scanner. Этот раздел доступен только администратору.
Кнопка паузы относится только к поиску активных сделок,
а не к основному сбору истории. Допустимый интервал — 1–1440 минут, исходное
значение — 10 минут; перезапуск процессов после изменения не нужен.

Повторный ручной запуск `deal_scanner.py` безопасно отклоняется lock-файлом,
поэтому он не удвоит расход CSFloat API и не будет спорить за одну SQLite-базу.
Ручная оценка находится в меню `Проверить лот`: сайт показывает Almaz, Igor и Nova
одновременно, а основной итог строит по самой осторожной доступной оценке.

Основное меню после входа состоит из пяти разделов: `Сделки`, `Проверить лот`,
`Стратегия`, `Рынок` и `Инвентарь` (`Deals`, `Check listing`, `Strategy`,
`Market`, `Inventory` в английском интерфейсе). У администратора дополнительно
доступна `Система` / `System` со scanner, parser, моделями и диагностикой.
В блоке переобучения Almaz, Igor и Nova используют только оружейные скины.
Knives и Gloves имеют отдельные модели, staging и quality gate; если данных для
перчаток ещё недостаточно, сайт покажет недостающий объём и не подменит их
моделью обычных скинов. После обучения таблица сравнения показывает, где
кандидат лучше или хуже рабочей версии по каждой основной метрике.

Отдельные команды, если нужно отлаживать один компонент:

```bash
./.venv/bin/python dashboard_server.py
./.venv/bin/python parser_final.py
./.venv/bin/python deal_scanner.py
./.venv/bin/python tg_deals_bot.py
./.venv/bin/python api_server.py
```

Для `api_server.py` сначала задай длинный случайный `API_TOKEN` в
`config.local.env`. Он принимает токен в заголовке `X-API-Token`, а не в URL.

## 4. Где настраивать ключи

`config.local.env` читает только Python-приложение:

```dotenv
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8010
DASHBOARD_TOKEN=
API_TOKEN=
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_IDS=123456789
TELEGRAM_ALLOW_ALL=false
STEAMWEBAPI_KEY=
```

Пустой `DASHBOARD_TOKEN` допустим: при первом запуске будет создан безопасный
`dashboard_token.txt`. Старые файлы `key`, `key2`, `key3`, `key4` и остальные
token-файлы сохранены для совместимости.

IP и пароль VPS не клади в этот файл. Для них создан отдельный
`hosting.local.env`, который читает только `start_reverse_tunnel.sh`.
В нём также можно указать `PUBLIC_SITE_URL=https://csskinsprediction.online`.
Проверка без подключения к серверу:

```bash
./start_reverse_tunnel.sh --check
```

## 5. Nova Neural

Nova уже обучена, её веса лежат в
`NEURALMODEL/artifacts/nova_price_model.pt`. Отдельной вкладки Nova больше нет:
её прогноз показывается вместе с Almaz и Igor на странице `/deal`
(`Проверить лот`). Старый адрес `/neural` сохранён и перенаправляет на `/deal`.
API модели `/api/predict-neural` и `/api/neural-status` продолжают работать.

Проверить модель без браузера:

```bash
./.venv/bin/python -c "import neural_model_runtime as n; print(n.model_status())"
```

Полное переобучение на Apple Silicon:

```bash
./.venv/bin/python train_neural_model.py
```

## 6. Почему раньше не запускалось

Последний запуск падал не из-за Python. Активный VPN оставил macOS без рабочего
глобального DNS; `cloudflared` пытался обратиться к DNS на `[::1]:53` и
завершался. Старый `start_all.sh` видел строку со случайным URL и ошибочно
сообщал, что tunnel готов.

Теперь основной сайт не зависит от внешнего tunnel. Если Quick Tunnel всё же
нужен, сначала отключи конфликтующий VPN и проверь DNS:

```bash
dig SRV _v2-origintunneld._tcp.argotunnel.com
```

Для постоянного домена используй reverse SSH по следующей инструкции либо
именованный Cloudflare Tunnel, а не Quick Tunnel.

## 7. Диагностика

```bash
tail -n 100 logs/dashboard.log
tail -n 100 logs/parser_final.log
tail -n 100 logs/deal_scanner.log
tail -n 100 logs/reverse_tunnel.log
lsof -nP -iTCP:8010 -sTCP:LISTEN
./.venv/bin/python -m pip check
```

`start_all.sh` по умолчанию сам запускает `caffeinate`, поэтому Mac не уснёт,
пока открыта команда. Если это поведение не нужно:

```bash
KEEP_AWAKE=0 ./start_all.sh
```

Для круглосуточной работы надёжнее перенести все процессы и локальную SQLite на
VPS с постоянным SSD. Не размещай `csfloat_history.db` на сетевой папке.
