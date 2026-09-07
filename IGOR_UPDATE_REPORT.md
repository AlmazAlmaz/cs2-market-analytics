# Обновление Igor v2 — 2026-08-10

## Итог

Igor v2 прошла независимое сравнение со старой production-моделью на одном
заранее зафиксированном truly-new holdout и была продвинута в production.
Promotion выполнен 2026-08-10 в `11:32:26 UTC` как замена целого bundle, после
проверки контракта и при остановленных сервисах.

Предыдущая Igor v1 сохранена целиком в:

```text
IGORMODEL/backups/igor_20260810T113224.802526Z
```

Результат означает преимущество на этом временном holdout, а не гарантию такой
же точности на каждом будущем скине или редком сегменте.

## Независимый gate: старая и новая Igor

Обе модели пересчитаны независимым evaluator на одинаковых 1 105 продажах в
целых runtime-центах. Boundary был выбран до обучения:
`2026-08-09T22:34:09.594496+00:00`. История старой модели была заморожена на
данных до boundary; значения target из holdout не использовались для fit,
early stopping, calibration или исторических target-признаков.

| Метрика | Igor v1 | Igor v2 | Изменение |
|---|---:|---:|---:|
| MAE | $1.390 | $0.956 | −$0.434 |
| Median APE | 11.126% | 4.923% | −6.203 п.п. |
| Mean APE | 14.760% | 9.776% | −4.984 п.п. |
| P90 APE | 33.333% | 25.000% | −8.333 п.п. |
| Median bias | −7.063% | 0.000% | ближе к нулю на 7.063 п.п. |

Igor v2 дала меньшую абсолютную ошибку на 65.07% отдельных строк. Paired
bootstrap-интервал изменения Median APE равен от −7.182 до −5.328 процентного
пункта; во всех значениях этого рассчитанного интервала изменение остаётся
отрицательным.

В `igor_metrics.json` сохранены близкие, но не идентичные trainer-метрики на тех
же 1 105 строках: Median APE 4.816%, Mean APE 9.803% и P90 APE 24.616%.
Таблица выше является авторитетной для promotion gate, потому что независимая
проверка прошла через production reconstruction и целые runtime-центы; JSON
trainer оценивает непрерывные значения до этого шага.

Promotion gate пройден, потому что улучшились все заранее выбранные общие
point-метрики, bundle/runtime проверки завершились без ошибок, а основные
сегментные point-оценки не показали регрессии. Интервальная калибровка и малые
сегменты учитывались отдельно и не замалчиваются ниже.

## Сегменты

| Сегмент | Строк | Median APE v1 | Median APE v2 | Вывод |
|---|---:|---:|---:|---|
| С наклейками | 186 | 7.905% | 4.334% | paired bootstrap-интервал изменения целиком ниже нуля |
| С брелоками/keychains | 62 | 8.687% | 4.567% | paired bootstrap-интервал изменения целиком ниже нуля |
| Дорогие, цена ≥ 5 958 центов | 66 | 5.583% | 2.231% | все point-метрики лучше, но интервал ещё не устойчив |

Порог дорогого сегмента — pre-boundary P90, поэтому он не подбирался по test.
В дорогом сегменте верхняя граница paired bootstrap-интервала изменения Median
APE равна **+0.125 п.п.**: несмотря на лучший point estimate, выборка из 66
строк слишком мала для уверенного заявления о стабильном преимуществе. То же
ограничение размера выборки нужно помнить для 62 keychain-сделок; результаты
сегментов следует перепроверять после накопления новых продаж.

## Интервал q15–q85: важное ограничение

На непрерывных предсказаниях CatBoost raw q15–q85 coverage улучшился с 51.946%
до **66.335%**, но остаётся ниже номинальных 70% на 3.665 процентного пункта.
Quantile crossing у Igor v2 равен 0%, point оказался вне raw-интервала в 0.362%
случаев.

Отдельная проверка фактического live-runtime пути — восстановление цены,
округление до целых центов и deployed envelope, содержащий point/q15/q85, —
дала coverage **71.403%**. Это не raw calibration: число нельзя использовать,
чтобы утверждать, что исходные q15/q85 уже идеально соответствуют 70%.
Интервал на сайте остаётся полезным диапазоном неопределённости, но не является
формальным гарантированным доверительным интервалом.

## Данные, split и provenance

Все три исходные таблицы прочитаны в одной read-only SQLite transaction. Cache
schema 4 связан с точными значениями snapshot потоковым content SHA-256.

- продаж с корректной ценой в snapshot: 806 677;
- строк с безопасным runtime reference anchor: 719 997;
- train: 646 996 строк, до `2026-08-05T17:36:04.971126+00:00`;
- validation: 71 896 строк, с `2026-08-05T17:36:11.710036+00:00` до boundary;
- untouched test: 1 105 строк, с `2026-08-09T22:34:33.437638+00:00` до
  `2026-08-10T09:05:57.613600+00:00`;
- строк наклеек: 419 036 для 143 805 listings;
- строк keychains: 27 582 для 27 582 listings.

Из fit/evaluation исключено 86 680 продаж без доказуемо безопасного main
reference anchor. Среди них 295 main reference были из будущего, а 86 383 не
имели известного reference timestamp. Дополнительно замаскированы, но учтены
счётчиками, 291 820 будущих и 40 460 неизвестных sticker reference-значений,
19 155 будущих и 2 неизвестных keychain reference-значения. Это консервативная
защита от известного temporal leakage; она не превращает неполную историческую
reference-базу в полноценную time-series.

Контрольные значения provenance:

- source content SHA-256:
  `1dbe09a797d6cb08deb1c002116a68993376a404a3aa4587a4fd1a819cd786a0`;
- source logical SHA-256:
  `cded2ff76017e0c7b3de2fda8172cd22b707c56f00b4281659524fce51387010`;
- cache SHA-256:
  `d55d657ad243b23972067b19118a2420da406560264eeb8913c654113b282f42`;
- feature-contract SHA-256:
  `30afe5f5af6e76689d9539ad588d34c847d361b5a6798348ecb6f61cf7e58127`;
- split SHA-256:
  `15a3c022586876dad525b3e894fe180cbddd9ec94a4d12e77fc7d03cd2c72041`.

Полные machine-readable данные находятся в
[production metrics](IGORMODEL/artifacts/igor_metrics.json) и
[artifact manifest](IGORMODEL/artifacts/artifact_manifest.json).

## Что исправлено в Igor v2

- Training/runtime используют один versioned feature contract: 68 признаков,
  из них 14 категориальных, с проверкой порядка и SHA-256.
- Корневая `csfloat_history.db`, наклейки и keychains читаются из одного
  immutable snapshot; cache проверяется по схеме, contract hash и SHA-256.
- Main и дочерние reference-значения проверяются по времени. Будущие/неизвестные
  значения исключаются или маскируются, а не выдаются модели как исторически
  доступные.
- Исправлена единая нормализация sticker wear, missing sentinel, paint seed,
  paint index, keychain id/pattern и текстовых категорий между train/runtime.
- Keychains получили собственные slot/aggregate признаки. Listing-level
  keychain component отделяется от reference anchor только когда он явно и
  согласованно доступен; цена дочернего keychain не подменяет этот компонент.
- Rolling premium и liquidity для train используют только строго более ранний
  timestamp. Продажи с одинаковым `sold_at` не видят targets друг друга.
- Validation/test получают frozen seed/history metadata только из train.
  Validation используется для early stopping и calibration; test оценивается
  после фиксации моделей и calibration.
- Point, q15 и q85 обучаются и сохраняются как согласованный staging bundle.
  Raw quantile coverage и deployed envelope coverage теперь отчётливо разделены.
- Production trainer не может писать напрямую в `artifacts/`. Отдельная
  promotion-утилита проверяет bundle, требует offline confirmation, сохраняет
  предыдущую версию и активирует весь каталог с автоматическим rollback при
  ошибке замены.

Эти изменения закрывают обнаруженные рассинхронизации и известные источники
leakage, но не отменяют рыночный drift, редкость дорогих крафтов и неполноту
исторических reference timestamps.

## Production bundle и hashes

Candidate обучен в `2026-08-10T11:27:12.267844+00:00`.

- staging candidate bundle SHA-256:
  `64989d50ddeddfb73f55766dba68db11cc67570d1577ca4ae2ad861bdb863b9c`;
- активный production bundle SHA-256:
  `3fb4a6672dd194316a6284e4bbd085de761c38b278cd327a5b49444426cdd2bf`.

Hashes различаются ожидаемо: при promotion в metadata/metrics атомарно
добавлены `stage: production`, `production_promoted: true` и запись о backup.
SHA-256 активных файлов:

| Файл | SHA-256 |
|---|---|
| `price_model_point.cbm` | `edc39056bc382f3943aee8576f24adaf3c29232695b488d35e01ff2f8e2862c6` |
| `price_model_q15.cbm` | `d86f6c5dfc632fa5c7e6d6922a2bce59d1f3a558ffae9eddfdb8e1125c5b1045` |
| `price_model_q85.cbm` | `42b34e3cd826e05968ee512eb1fcdb40d72941166ed79d3cdf8ecec8b842a152` |
| `feature_metadata.pkl` | `af54db8347e1c037aea2921a4a3707d1cf4e52af09d6e624f4d80ecdda607f7d` |
| `igor_metrics.json` | `95a6f3169fdb4be2a7c972ce3c218d3e5734f1b2a35653d19adb585713118917` |

`artifact_manifest.json` — commit marker, записанный последним; его собственный
SHA-256 равен
`f1f0c52db24bb18ff768292204f1d334c8079e4bad4d4e7a0ec791ea37fbfc94`.

Igor v1 была создана до появления v2 manifest, поэтому ей не приписывается
несуществующий canonical bundle hash. Для проверки backup зафиксированы hashes
его реальных файлов:

| Файл v1 backup | SHA-256 |
|---|---|
| `price_model_point.cbm` | `2a9f0b680d0ddd7ec6e202aac855e8aa24622e3d80776d6fb8bfbe31aca8bb91` |
| `price_model_q15.cbm` | `a5ea7aaf7f954607980a901ab59a7556e3fc472e1aec6fbb9fc9d27e6f08b540` |
| `price_model_q85.cbm` | `0b4a0efecad04a7980a7546c27a48caf51f2cbb17b844fbca70c881f63b8747a` |
| `feature_metadata.pkl` | `e7a13f5deee7a75ed44cfdb8ae948e11ab368c2ab55222f38f1df143108c736e` |
| `train_test_split_cache.pkl` | `5c0495878999488f7e5f0be1be94e5297bee334e935adfa54aa650af6b09d02a` |

## Проверка и rollback

Проверить активный v2 bundle без изменения файлов:

```bash
./.venv/bin/python -c "import igor_model_runtime as model; model.load_models(); print('Igor bundle OK')"
```

Rollback нужен только при реальной runtime-регрессии. Сначала останови
`./start_all.sh` через `Ctrl+C`, затем убедись, что dashboard и Deal Scanner не
работают:

```bash
cd cs2-market-analytics
pgrep -fl 'dashboard_server.py|deal_scanner.py'
```

Команда должна завершиться без вывода. Дальше выполни следующие шаги в одном
терминале. Сначала проверь оба источника и подготовь уникальный сохраняемый путь
для текущей v2:

```bash
IGOR_FAILED_DIR="IGORMODEL/backups/igor_v2_removed_$(date -u +%Y%m%dT%H%M%SZ)"
test -d IGORMODEL/artifacts
test -d IGORMODEL/backups/igor_20260810T113224.802526Z
test ! -e "$IGOR_FAILED_DIR"
```

Затем сохрани, а не удаляй текущую v2:

```bash
mv IGORMODEL/artifacts "$IGOR_FAILED_DIR"
```

И только после успешного `mv` восстанови весь старый bundle одним каталогом:

```bash
mv IGORMODEL/backups/igor_20260810T113224.802526Z IGORMODEL/artifacts
```

Если второй `mv` завершился ошибкой, верни v2 командой
`mv "$IGOR_FAILED_DIR" IGORMODEL/artifacts` до запуска сервисов. После
успешного rollback повтори read-only проверку загрузки моделей, затем запусти:

```bash
./start_all.sh
```

Старая v1 поддерживается runtime только как цельный комплект её собственных
трёх CBM и metadata. Legacy split cache лежит в backup для воспроизводимости,
но runtime его не читает. Не смешивай файлы v1 и v2.

Подробная инструкция по следующему обучению и promotion находится в
[IGORMODEL/README.md](IGORMODEL/README.md).
