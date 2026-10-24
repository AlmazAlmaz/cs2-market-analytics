# Nova Neural

Nova — третья независимая модель цены CS2-предметов. Она не заменяет Almaz или
Igor: сайт показывает все три прогноза рядом, а scanner сохраняет результаты
каждой модели отдельно.

## Архитектура

- embeddings для weapon/wear/pattern/rarity и до пяти sticker ID;
- числовые market, float, liquidity, reference и craft-признаки;
- residual MLP: width 512, пять residual blocks, около 8.15 млн параметров;
- один ordered-quantile head для q15, q50 и q85;
- calibration и shrinkage подбираются только на validation;
- train, validation и test разделены строго по времени;
- test не участвует ни в early stopping, ни в выборе calibration.

Артефакт весов занимает около 31 MB — заметно меньше трёх CatBoost-файлов Igor.

## Результат сохранённой модели

Данные:

- train: 513 640 продаж;
- validation: 57 071;
- полностью отложенный test: 142 678;
- устройство обучения: Apple MPS.

Test:

| Метрика | Nova | CSFloat reference baseline |
|---|---:|---:|
| MAE | $1.53 | $2.27 |
| Median APE | 5.15% | 5.90% |
| Mean APE | 12.83% | 15.20% |
| P90 APE | 29.17% | 40.94% |
| Ошибка ≤ 10% | 66.79% | 62.35% |
| Ошибка ≤ 20% | 82.10% | 76.62% |

Покрытие интервала q15–q85 на test: 67.70%, что близко к целевым 70%.

Это честное улучшение reference baseline и типичной ошибки Almaz. Сохранённый
Igor пока немного сильнее по median APE/MAE, поэтому Nova не названа
«безусловно лучшей»: ансамбль трёх независимых прогнозов безопаснее одного числа.

## Обучение

```bash
cd cs2-market-analytics
./.venv/bin/python train_neural_model.py
```

Короткая smoke-тренировка:

```bash
./.venv/bin/python train_neural_model.py --max-train-rows 100000 --epochs 2
```

Результаты появляются в `NEURALMODEL/artifacts/`:

- `nova_price_model.pt` — веса;
- `nova_metadata.json` — vocabularies/scaler/calibration/runtime contract;
- `nova_metrics.json` — история и untouched-test метрики.

Файл `.pt` исключён из Git, поэтому при переносе на VPS его надо копировать
отдельно вместе с metadata.

## Известное ограничение

Текущий исторический feature-cache Igor не содержит keychain/charm полей. Nova
не выдумывает их стоимость и пока не должна быть единственным основанием для
покупки charm-предметов. Следующая полноценная итерация должна перестроить общий
датасет с keychain token и историческими (не будущими) sticker reference prices.

