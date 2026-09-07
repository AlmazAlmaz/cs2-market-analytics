# Публикация сайта с Mac через VPS

Эта схема подходит к уже существующему домену и VPS:

```text
браузер → HTTPS/nginx на VPS → reverse SSH → 127.0.0.1:8010 на Mac
```

База, CSFloat-ключи и модели остаются на Mac. На VPS находятся только nginx,
TLS-сертификат и SSH endpoint.

## 1. Заполни отдельный приватный файл

Открой `hosting.local.env` и впиши данные сервера:

```dotenv
VPS_HOST=IP_ТВОЕГО_СЕРВЕРА
VPS_USER=root
VPS_SSH_PORT=22
VPS_IDENTITY_FILE=
VPS_PASSWORD=ПАРОЛЬ_СЕРВЕРА
REMOTE_BIND_HOST=127.0.0.1
REMOTE_PORT=8010
LOCAL_HOST=127.0.0.1
LOCAL_PORT=8010
PUBLIC_SITE_URL=https://csskinsprediction.online
```

Затем:

```bash
chmod 600 hosting.local.env
```

Этот файл исключён из Git и не загружается в процессы сайта, parser или
нейросетей. Пароль поддержан для удобства, но SSH-ключ безопаснее и надёжнее.

## 2. Рекомендуемый вход по SSH-ключу

На Mac:

```bash
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/cs2_vps
cat ~/.ssh/cs2_vps.pub | ssh -p 22 root@IP_ТВОЕГО_СЕРВЕРА \
  'umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys'
```

Пароль будет запрошен один раз. После этого укажи:

```dotenv
VPS_IDENTITY_FILE=~/.ssh/cs2_vps
VPS_PASSWORD=
```

Для автоматического переподключения:

```bash
brew install autossh
```

Если принципиально хочешь хранить пароль, `start_reverse_tunnel.sh` использует
`sshpass` либо встроенный в macOS `/usr/bin/expect`; пароль не подставляется в
командную строку. Без автоподстановки просто оставь `VPS_PASSWORD` пустым —
обычный `ssh` спросит его в Terminal.

## 3. Настрой nginx на VPS

DNS-записи домена должны указывать на IP VPS. На Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

Скопируй `deploy/nginx-cs2.conf.example` в
`/etc/nginx/sites-available/cs2-dashboard`, замени домены и включи конфиг:

```bash
sudo ln -s /etc/nginx/sites-available/cs2-dashboard \
  /etc/nginx/sites-enabled/cs2-dashboard
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d csskinsprediction.online \
  -d deals.csskinsprediction.online
```

Порт `8010` на VPS не нужно открывать в firewall: nginx и reverse tunnel
соединяются через `127.0.0.1`.

## 4. Запусти сайт и tunnel одной командой

```bash
cd cs2-market-analytics
./start_all.sh
```

Перед подключением можно безопасно проверить заполнение файла без сетевого
соединения:

```bash
./start_reverse_tunnel.sh --check
```

`start_all.sh` сначала проверит локальные файлы и SSH-конфиг, затем поднимет
dashboard и tunnel, дождётся `/health` и только после этого включит parser и
scanner. Он также перезапустит tunnel после разрыва. Оставь это одно окно
Terminal открытым; Mac автоматически удерживается от сна. Проверки:

```bash
curl -fsS http://127.0.0.1:8010/health
curl -I https://csskinsprediction.online/health
```

Чтобы выключить всё, нажми `Control+C` именно в этом окне Terminal (не
`Command+C`). Если окно потерялось или IDE не передаёт сочетание клавиш, открой
другой Terminal и выполни:

```bash
cd cs2-market-analytics
./stop_all.sh
```

Команда проверяет PID супервизора и даёт `start_all.sh` самому корректно
остановить dashboard, parser, scanner, trend worker и tunnel.

Если публичный `/health` не подтвердился, сервисы продолжат работать и браузер
откроет локальный авторизованный адрес. Так проблема DNS, TLS или nginx не выглядит
как поломка самого проекта; предупреждение останется в Terminal.

Если nginx отвечает `502`, почти всегда отсутствует живой reverse tunnel либо
локальный dashboard. Проверь `logs/reverse_tunnel.log`, `logs/dashboard.log` и
на VPS:

```bash
ss -ltn | grep 8010
sudo tail -n 100 /var/log/nginx/error.log
```

## 5. Круглосуточный production

Схема выше зависит от включённого Mac. Для настоящей работы 24/7 лучше перенести
код, модели и SQLite на один VPS с локальным SSD и запустить dashboard, parser и
scanner отдельными `systemd` services.

Текущий проект вместе с базой и модельными артефактами занимает несколько GB;
для роста базы и snapshot-загрузок практический минимум — 40 GB SSD и 4 GB RAM,
рекомендуется 80+ GB SSD и 8 GB RAM. Обучение Nova лучше оставить на Mac, а на
VPS выполнять только inference.

SQLite-файл должен лежать на той же машине, где работают Python-процессы. Если
понадобятся несколько серверов или много одновременных writers, мигрируй данные
в PostgreSQL вместо общей сетевой SQLite.

## 6. Управление доступом пользователей

Каждая публичная регистрация создаёт обычного пользователя со статусом
`pending`. Регистрация не создаёт сессию: до ручного одобрения войти в закрытые
разделы и получить предложения нельзя.

Панель владельца доступна только напрямую на этом Mac:

```text
http://127.0.0.1:8010/admin/users
```

Сначала открой локальный dashboard с master-токеном из `dashboard_token.txt`,
чтобы браузер получил приватную cookie, затем выбери «Пользователи». Через
публичный домен `/admin/users` и `/api/admin/users` намеренно отвечают `404`.

В панели можно одобрить или заблокировать аккаунт, завершить все его сессии,
выдать одноразовую ссылку сброса пароля и мягко удалить учётную запись. Только
локальный владелец с master-токеном может выдать или снять роль администратора;
обычный web-администратор не может назначать роли. Сначала одобри аккаунт, затем
нажми «Выдать права администратора». Изменение роли завершает все его сессии,
поэтому пользователю нужно войти заново.

Администратор получает доступ к системному dashboard, управлению scanner и
переобучением моделей, а также к кнопке `Export DB`. Она скачивает согласованный
SQLite snapshot основной базы, а не копирует открытый файл во время записи.
Выдавай эту роль только полностью доверенным людям: snapshot может быть большим
и содержит собранную рыночную историю.

Новым аккаунтам доступ запрещён по умолчанию. Блокировка действует и на уже
открытые сессии. Удаление обезличивает логин и данные присутствия, но сохраняет
инвентарь и неизменяемый аудит операций.

Показываются только ограниченные данные безопасности: время последней
активности, IP, браузер/устройство и число сессий. Пароли, bearer-токены,
посещённые URL и referrer не сохраняются в этой телеметрии. Страна остаётся
неопределённой, пока не настроен доверенный локальный GeoIP/proxy; не включай
доверие к клиентскому заголовку страны на обычном nginx.

Если SMTP не настроен, панель покажет локальную одноразовую ссылку для ручной
передачи пользователю. Для автоматической отправки письма заполни
`CS2_ACCOUNT_SMTP_*` в `config.local.env`; секреты не помещай в Git или командную
строку.

## 7. Мультирыночная история BUFF/YouPin

Новая история рынка изолирована в `.private/market_history.db`: она не меняет
`csfloat_history.db`, не влияет на SHA-256 обучающих данных и не попадает в
существующие модели. Формат источников и ограничения площадок описаны в
[`market_history_sources.md`](market_history_sources.md).

Обычный `./start_all.sh` не требует ключа внешнего поставщика и не обращается к
нему. В `config.local.env.example` безопасные значения выглядят так:

```dotenv
CS2_MARKET_HISTORY_ENABLED=false
CS2_MARKET_HISTORY_LICENSE_CONFIRMED=false
CSPRICEAPI_KEY=
```

Даже заданный ключ сам по себе не включает загрузку. Для автоматического
импорта сначала получи у поставщика письменное разрешение на нужные способы
использования данных (долговременное хранение, показ на сайте и/или ML), затем
заполни приватный `config.local.env`:

```dotenv
CS2_MARKET_HISTORY_DB_PATH=.private/market_history.db
CS2_MARKET_HISTORY_STATUS_PATH=.private/market_history_status.json
CS2_MARKET_HISTORY_ENABLED=true
CS2_MARKET_HISTORY_LICENSE_CONFIRMED=true
CS2_MARKET_HISTORY_REFRESH_SECONDS=86400
CS2_MARKET_HISTORY_FETCH_SNAPSHOTS=true
CS2_MARKET_HISTORY_FETCH_SALES=true
CS2_MARKET_HISTORY_SALES_CONTRACT_CONFIRMED=false
CS2_MARKET_HISTORY_SALES_CONTRACT_VERSION=
CS2_MARKET_HISTORY_SALES_TIMESTAMPS_ARE_UTC=false
CSPRICEAPI_KEY=ТВОЙ_КЛЮЧ
```

Три `SALES_CONTRACT_*` значения относятся только к выгрузке завершённых
продаж. Публичное описание формата сейчас не фиксирует однозначно валюту каждой
площадки и timezone timestamp'ов. После письменного подтверждения поставщика
проверь контракт в коде (`csfloat=USD`, `youpin=CNY`, `skinport=EUR`; timestamps
без timezone означают UTC), затем поставь `CONFIRMED=true`, укажи идентификатор
версии/письма в `VERSION` и только при явном подтверждении UTC поставь
`TIMESTAMPS_ARE_UTC=true`. Пока хотя бы одно значение не подтверждено, worker
пропускает sales ZIP, но продолжает независимо сохранять BUFF/YouPin snapshots.

Не вставляй ключ в командную строку, URL или скриншот. Файл уже исключён из Git;
проверь его права:

```bash
chmod 600 config.local.env
```

Создать/обновить только схему можно заранее, без ключа и без сетевых запросов:

```bash
.venv/bin/python market_history_worker.py --migrate-only
```

После этого обычный запуск с режимом `auto` поднимет worker вместе с сайтом:

```bash
./start_all.sh
```

Явная проверка конфигурации выполняется режимом `1`: при отсутствующем ключе,
выключенном импорте или неподтверждённой лицензии старт завершится ошибкой до
сетевого запроса.

```bash
START_MARKET_HISTORY=1 ./start_all.sh
```

Чтобы гарантированно не запускать только этот worker, используй:

```bash
START_MARKET_HISTORY=0 ./start_all.sh
```

Worker корректно получает сигнал остановки от общего супервизора. Поэтому его,
как и остальные процессы, выключают `Control+C` в окне `start_all.sh` либо:

```bash
./stop_all.sh
```

Диагностика не содержит API-ключ:

```bash
tail -n 100 logs/market_history_worker.log
jq . .private/market_history_status.json
```

Разовый приватный импорт законно полученного ZIP для локального прототипа не
требует включать автоматические загрузки или задавать API-ключ. Это не даёт
дополнительных прав на публикацию или ML:

```bash
.venv/bin/python market_history_worker.py --import-zip /полный/путь/export.zip
```

Схема создаётся при первом безопасном подключении. Импортёр отклонит попытку
указать старую базу с таблицей `sales`, ограничивает ZIP/CSV и повторно не
добавляет уже обработанную выгрузку. BUFF/YouPin listing snapshots сохраняются
в CNY; примерная цена USD вычисляется только по сохранённому курсу с датой и
источником. У ручного sales ZIP без подтверждённого контракта валюта остаётся
`UNK`, timezone помечается `UTC_unverified`, а USD-поля не используются. Если
все три `SALES_CONTRACT_*` значения подтверждены, тот же ручной импорт применит
закреплённый currency/timezone контракт и сохранит его версию в provenance.

Важное различие: BUFF/YouPin current price — это снимок активного объявления,
а не подтверждённая продажа. Если выгрузка продаж недоступна по тарифу, уже
сохранённые снимки не откатываются и не переименовываются в продажи.
