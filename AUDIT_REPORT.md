# Аудит проекта — 2026-08-10

## Что это за проект

Система собирает историю CSFloat в SQLite, строит рыночную аналитику, ищет
активные CS2-лоты со стикерами, оценивает цену моделями Almaz/Igor/Nova,
показывает сравнение на сайте и может отправлять сделки в Telegram.

## Исправлено

- потеря корректных нулевых `float_value`, `paint_seed`, `paint_index` и
  string-формы collection при нормализации CSFloat;
- повторная нормализация неизвестного sticker wear `-1` в «идеальный» wear `0`;
- train/runtime skew Igor: runtime снова передаёт сырую sticker reference price;
- неверный reference=0 в Igor и регистр `IGORMODEL` для Linux;
- Almaz listing age и выбор более честных comparable sales;
- позиционный сдвиг аргументов `/deals` в Telegram и падение `seen=2`;
- открытый для всех Telegram-бот: теперь default-deny по chat ID;
- потеря Telegram updates и длинные сообщения > лимита Telegram;
- stale Best Deals: плохие прогнозы обновляются, старые записи получают
  `expired`, а сайт по умолчанию показывает только свежие 24 часа;
- Best Deals показывает разногласия моделей, safe consensus, консервативные
  ROI/profit и не считает отсутствующую метрику безопасным сигналом;
- фильтры и страница Best Deals сохраняются в URL, а скрытая вкладка не создаёт
  лишние refresh-запросы;
- scanner отделяет решение модели от порогов публикации, инвалидирует старый
  положительный сигнал после ошибки и не подменяет время наблюдения при reprice;
- Telegram offset и watcher-состояние сохраняются атомарно, а токен удаляется
  из ошибок и логов;
- ошибка одной модели/лота больше не обрывает весь scanner;
- добавлены cursor pagination, строгая проверка scanner config и миграции схемы;
- неправильный выбор best model по условию `ROI OR profit`;
- `NaN/Inf` в web-фильтрах, неограниченный cache и параллельные 3.8-GB snapshots;
- HTTPS-only для прямого CSFloat API URL и более надёжный key/proxy failover;
- API отделён от тяжёлой основной БД, закрыт header token и запускается только
  через `main()`;
- токен dashboard обменивается на HttpOnly/SameSite cookie и удаляется из URL;
- CSP/security headers, безопасные внешние URL и nginx log без query string;
- `.venv`/Conda рассинхронизация, ложный tunnel readiness и остановка исправных
  сервисов при падении необязательного `cloudflared`;
- секреты, пути и порты вынесены в приватные конфиги; права файлов `0600`;
- Almaz v3 обучена на актуальной базе с runtime-safe признаками, residual target,
  calibration, q15/q85 и chronological train/validation/untouched test;
- Igor cache теперь проверяет fingerprint базы и не принимает устаревший или
  изменившийся во время сборки snapshot.

## Nova Neural

Добавлена отдельная residual embedding network с q15/q50/q85, отдельным runtime,
API, страницей `/neural`, scanner/Telegram integration и 31-MB артефактом.
Подробные метрики находятся в `NEURALMODEL/README.md` и
`NEURALMODEL/artifacts/nova_metrics.json`.

## Almaz v3

Almaz v3 обучена на 795 869 продажах. На общем untouched holdout из 51 802
новых продаж MAE старой Almaz снизилась с $2.3745 до $1.6276, median APE — с
7.2752% до 5.6503%. Интервал q15–q85 имеет покрытие 71.44% без quantile crossing.
Старый production-артефакт сохранён для восстановления. Полный отчёт:
`MODEL_UPDATE_REPORT.md`.

## Проверено

- SQLite `PRAGMA quick_check`: `ok`;
- база: 795 869 sales и 413 169 sticker rows;
- 41 regression test: passed;
- Python compilation, Ruff correctness checks, Bash syntax: passed;
- JavaScript всех четырёх inline-блоков: passed `node --check`;
- live dashboard health/auth/Best Deals/model freshness: passed;
- live export API health/auth/validation: passed.

## Что остаётся ограничением данных

- В старом Igor feature-cache нет keychain/charm token, хотя такие сделки есть.
- Исторические sticker reference prices часто были обновлены после даты продажи;
  без отдельной time-series цен их нельзя превратить в полностью честный backtest.
- Igor ещё не переобучен после исправления training pipeline: полный CPU-прогон
  тяжёлый по RAM, времени и размеру артефактов. Almaz уже обновлена до v3 только
  после сравнительного теста и с сохранением прежней версии.
- В датасете почти только проданные предметы. Он годится для оценки цены, но не
  для честной вероятности «продастся за 24 часа» — для этого нужны snapshots
  непроданных и истёкших listings.
- SQLite около 3.8 GB и Igor artifacts около 2.2 GB требуют локального SSD и
  запаса RAM; сетевой shared-disk для этой архитектуры не подходит.
