# Igor v2 — обучение и безопасное обновление

Igor — CatBoost-модель цены продажи скина CS2 на CSFloat. Она обучает три
residual-модели: точечный прогноз и квантили q15/q85. Цель — логарифмическая
премия относительно доступной на момент продажи reference-цены без компонента
брелока; итоговая цена восстанавливается в центах.

Этот pipeline отделяет обучение от production. `train.py` всегда пишет
кандидата в staging и намеренно отказывается писать прямо в
`IGORMODEL/artifacts/`.

## Данные

Канонический путь к базе из корня проекта:

```text
csfloat_history.db
```

Запускай команды из корня проекта. Для совместимости loader умеет использовать
`IGORMODEL/csfloat_history.db`, только если корневого файла нет; если существуют
оба файла, всегда выбирается корневой. Не держи две разные копии базы — это
легко приводит к обучению не на тех данных.

Pipeline читает только продажи с `sales.state = 'sold'`, непустым `name` и
положительным `price_cents`. Для текущей схемы нужны три таблицы:

- `sales`: `listing_id`, `name`, `base_skin_name`, `wear_name`, `price_cents`,
  `reference_base_price`, `reference_predicted_price`,
  `reference_float_factor`, `reference_quantity`, `reference_keychain_price`,
  `reference_last_updated`, `float_value`, `paint_seed`, `paint_index`, `state`,
  `created_at`, `sold_at`, `fetched_at`, `sale_type`, `is_seller`,
  `is_watchlisted`, `watchers`, `item_def_index`, `item_rarity_name`,
  `item_collection_name`, `item_is_stattrak`, `item_is_souvenir`;
- `sale_stickers`: `listing_id`, `sticker_index`, `sticker_id`, `slot`, `name`,
  `wear`, `price_cents`, `reference_price_cents`, `reference_updated_at`;
- `sale_keychains`: `listing_id`, `keychain_index`, `keychain_id`, `slot`,
  `name`, `pattern`, `price_cents`, `reference_quantity`,
  `reference_updated_at`.

Timestamp-поля reference обязательны для time-safe контракта. Main reference
допускается в обучение только при `reference_last_updated <= sold_at`.
Reference-цены наклеек и брелоков с будущим или неизвестным временем не
подставляются в признаки: их значения маскируются, а отдельные счётчики
сохраняют причину маскирования. Строки без безопасного main reference anchor
исключаются из fit/evaluation.

## Immutable snapshot и cache schema 4

`data_loading.py` открывает SQLite в read-only режиме и держит одну read
transaction на всём чтении `sales`, `sale_stickers` и `sale_keychains`. Поэтому
все три таблицы относятся к одному WAL-aware snapshot, даже если парсер в это
время дописывает новые строки.

Для provenance pipeline потоково, с ограниченным расходом памяти, вычисляет
SHA-256 точных исходных значений в стабильном порядке. Отдельно записываются
логический fingerprint базы, диапазоны дат, число строк таблиц и версия SQLite.

Результат feature engineering сохраняется как cache schema 4:

- `IGORMODEL/cache/features_full.pkl` — полный time-safe dataframe;
- `IGORMODEL/cache/features_full.metadata.json` — source/content fingerprint,
  SHA-256 cache-файла, версия схемы и SHA-256 feature contract.

Запись обоих файлов выполняется через временный файл и `os.replace`. Перед
обучением `train.py` сверяет schema version, feature-contract hash и фактический
SHA-256 cache. Устаревший или повреждённый default cache не используется
молча: запуск завершится ошибкой и попросит `--rebuild-cache`.

## Установка

Из корня проекта:

```bash
./.venv/bin/python -m pip install -r IGORMODEL/requirements.txt
```

Текущие зафиксированные зависимости находятся в
`IGORMODEL/requirements.txt`. Используй проектную `.venv`, чтобы обучение и
runtime не разошлись по версиям Python-библиотек.

## Рекомендуемый запуск

Одна команда перестраивает cache из одного snapshot, проверяет его и обучает
кандидата в `IGORMODEL/staging/igor_candidate/`:

```bash
./.venv/bin/python IGORMODEL/train.py --rebuild-cache \
  --holdout-after '2026-08-09T22:34:09.594496Z'
```

Этот boundary зафиксирован для текущего честного сравнения со старой Igor.
Менять его после просмотра результатов кандидата нельзя. Для будущего цикла
обновления возьми новый заранее выбранный boundary и запиши его в отчёт до
обучения.

Если cache уже собран из нужного immutable snapshot и база с тех пор не
изменилась:

```bash
./.venv/bin/python IGORMODEL/train.py \
  --holdout-after '2026-08-09T22:34:09.594496Z'
```

Отдельно только пересобрать cache:

```bash
./.venv/bin/python IGORMODEL/build_cache.py
```

Полезные параметры `train.py`:

- `--thread-count N` — число CPU-потоков CatBoost, по умолчанию 8;
- `--point-iterations N` и `--quantile-iterations N` — верхние пределы числа
  деревьев; early stopping может остановить обучение раньше;
- `--max-train-rows N` — только для диагностического прогона, не для кандидата
  на production;
- `--artifact-dir PATH` — отдельная staging-папка; production-путь запрещён;
- `--cache-path` вместе с `--cache-metadata` — явно заданная неизменяемая пара
  cache/manifest для воспроизводимого запуска.

`--rebuild-cache` разрешён только для default cache. Не используй старые
`fast_iterate.py`/`evaluate_segments.py` как promotion gate: это legacy
экспериментальные утилиты с другим split-контрактом.

## Train, validation и untouched test

При указанном `--holdout-after` разбиение строго хронологическое:

1. строки с `sold_at <= boundary` образуют development-набор;
2. строки с `sold_at > boundary` образуют фиксированный untouched test;
3. последние 10% development по времени образуют validation, причём одинаковый
   `sold_at` целиком остаётся на одной стороне validation boundary;
4. всё более раннее — train.

Test должен содержать не меньше 500 runtime-valid строк. Seed popularity и
исторические premium/liquidity признаки обучаются только на train. Для
validation и test используется одна frozen history на границе train: их
собственные target-значения не попадают в признаки. В train временные признаки
учитывают только продажи со строго более ранним timestamp, а не соседние строки
с тем же `sold_at`.

Validation используется для early stopping и calibration point/q15/q85. Test
впервые оценивается только после завершения fit и calibration. Дополнительно
считается runtime-like часть test с `0 <= fetched_at - sold_at <= 24 часа`; в
ней тоже требуется минимум 500 строк. Отчёт отдельно хранит покрытие исходного
q15–q85 и покрытие production-envelope, который гарантированно содержит point,
q15 и q85.

Без `--holdout-after` существует fallback с последними 20% строк как test. Для
сравнения production-моделей его не используй: фиксированный boundary делает
старую и новую модели сопоставимыми и не позволяет выбирать test задним числом.

## Артефакты кандидата

Успешный v2 run атомарно записывает в staging:

- `price_model_point.cbm` — точечная MAE-модель;
- `price_model_q15.cbm` — нижний квантиль;
- `price_model_q85.cbm` — верхний квантиль;
- `feature_metadata.pkl` — feature contract, calibration, seed encoder, frozen
  runtime history, split и source/cache provenance;
- `igor_metrics.json` — validation/test/runtime-like метрики, интервалы,
  сегменты и флаг `production_promoted: false`.

`train_test_split_cache.pkl`, который лежит в старом production bundle, —
legacy-файл. Pipeline v2 его не создаёт и для runtime он не нужен.

Сайт продолжает использовать `IGORMODEL/artifacts/`, пока кандидат не прошёл
gate и не был перенесён туда как единый согласованный bundle.

## Promotion gate и откат

Сам `train.py` ничего не продвигает в production. Перед promotion обязательно:

1. сравнить старую и новую Igor на одном зафиксированном untouched holdout;
2. проверить общие MAE/Median APE/Mean APE/P90 APE и сегменты с достаточным
   числом строк, особенно сделки с наклейками и брелоками;
3. отдельно проверить raw q15–q85 coverage, crossing rate и долю point вне raw
   интервала — production-envelope не должен скрывать плохую калибровку;
4. выполнить runtime smoke-test через `igor_model_runtime.py`;
5. проверить соответствие artifact/contract/cache SHA и отсутствие
   предварительного чтения test при подборе модели.

После gate сначала выполни полностью read-only проверку кандидата:

```bash
./.venv/bin/python IGORMODEL/promote_candidate.py --check
```

Команда проверяет ровно пять файлов bundle, общий provenance метаданных и
метрик, feature contract, порядок признаков, категориальные индексы, loss и
число деревьев всех трёх CatBoost-моделей. В stdout выводится SHA-256 manifest;
в staging при этом ничего не записывается.

Для фактического promotion сначала останови `./start_all.sh` через `Ctrl+C` и
убедись, что сайт и Deal Scanner завершились. Затем отдельно запусти:

```bash
./.venv/bin/python IGORMODEL/promote_candidate.py \
  --promote --offline-confirm
```

`--promote` без `--offline-confirm` запрещён. Утилита также сама проверяет
процессы перед копированием и непосредственно перед заменой. Старый production
целиком переезжает в timestamped `IGORMODEL/backups/igor_<UTC>/`, новый bundle
собирается рядом с `artifacts/`, fsync-ится и активируется каталогом; при ошибке
старый каталог автоматически возвращается на место. Legacy-файлы старой v1
остаются только в backup, а production получает пять согласованных v2-файлов и
записанный последним `artifact_manifest.json`.

Не переноси отдельные CBM или metadata вручную: это может смешать разные run.
После успешной команды снова запусти `./start_all.sh` и повтори runtime
smoke-test уже на production bundle.

Для отката восстанови весь предыдущий bundle из одной backup-папки, а не
отдельную модель. Runtime сохраняет совместимость со старым v1 metadata, если
вместе возвращены его собственные CBM-файлы.

## Ресурсы

На snapshot этого обновления корневая SQLite-база занимает около 4.1 GB, cache —
около 1.0 GB, активный v2 production bundle — около 2.3 GB, а сохранённый v1
bundle вместе с legacy split cache — около 2.2 GB. Для безопасного цикла нужны
место одновременно под базу, cache, staging-кандидата и полный backup
production; практический запас — не меньше 10 GB свободного диска.

Feature engineering загружает большой pandas dataframe, а CatBoost строит три
модели последовательно. На машине с 8 GB RAM возможны сильный swap или ошибка;
16 GB и больше — разумный минимум для полного snapshot. Обучение CPU-heavy и в
зависимости от Mac может занять от десятков минут до нескольких часов. При
нехватке ресурсов уменьши `--thread-count`; `--max-train-rows` используй лишь
для smoke/диагностики, не для окончательного promotion.

## Метрики текущего обновления

Igor v2 прошла независимый gate и продвинута в production. Авторитетный итог,
сравнение со старой Igor, ограничения интервала, hashes и точный rollback смотри
в [IGOR_UPDATE_REPORT.md](../IGOR_UPDATE_REPORT.md). Машиночитаемые production-
метрики находятся в `IGORMODEL/artifacts/igor_metrics.json`.
