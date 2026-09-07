#!/usr/bin/env python3
"""
Диагностика по проблемным сегментам, которые ChatGPT-модель предсказывала
плохо: дорогие стикер-крафты, редкие стикеры, Katowice 2014, низколиквидные
скины, 3-5 стикеров, дубликат-крафты.

diagnostic_report() переиспользуется и из train.py/fast_iterate.py (в памяти),
и из main() ниже (грузит с диска артефакты после полного прогона).
"""
from __future__ import annotations

import pickle
from pathlib import Path

import evaluate_metrics as em
import features as F
import pandas as pd
import train as T
from catboost import CatBoostRegressor

LEGACY_ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"

BASELINE_TEST_METRICS = T.BASELINE_TEST_METRICS
BASELINE_TOP100_STICKER_RATIO = T.BASELINE_TOP100_STICKER_RATIO


def load_artifacts():
    artifact_dir = T.model_generations.active_or_legacy_bundle_dir(
        "igor",
        LEGACY_ARTIFACT_DIR,
        project_dir=Path(__file__).resolve().parent.parent,
    )
    with open(LEGACY_ARTIFACT_DIR / "train_test_split_cache.pkl", "rb") as f:
        cache = pickle.load(f)
    model = CatBoostRegressor()
    model.load_model(str(artifact_dir / "price_model_point.cbm"))
    model_low, model_high = None, None
    if (artifact_dir / "price_model_q15.cbm").exists():
        model_low = CatBoostRegressor()
        model_low.load_model(str(artifact_dir / "price_model_q15.cbm"))
    if (artifact_dir / "price_model_q85.cbm").exists():
        model_high = CatBoostRegressor()
        model_high.load_model(str(artifact_dir / "price_model_q85.cbm"))
    return cache, model, model_low, model_high


def add_predictions(df, model, feature_cols, cat_cols):
    df = T.clean_cat_features(df, cat_cols)
    log_prem_pred = model.predict(df[feature_cols])
    df["pred_price_cents"] = T.reconstruct_price(log_prem_pred, df["log_reference_predicted_price"].values)
    return df


def rare_sticker_mask(train_df: pd.DataFrame, df: pd.DataFrame, max_count: int = 5) -> pd.Series:
    """Строки, где хотя бы один стикер встречался в train <= max_count раз. Векторизовано."""
    id_cols = [f"sticker_{i}_id" for i in range(F.MAX_STICKER_SLOTS)]
    all_ids = pd.concat([train_df[c] for c in id_cols])
    counts = all_ids[all_ids != -1].value_counts()

    mask = pd.Series(False, index=df.index)
    for c in id_cols:
        col = df[c]
        col_counts = col.map(counts).fillna(0)
        mask = mask | ((col != -1) & (col_counts <= max_count))
    return mask


def diagnostic_report(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    model,
    feature_cols: list[str],
    cat_cols: list[str],
    model_low=None,
    model_high=None,
    label: str = "",
) -> dict:
    """Возвращает dict с test_m (метрики) и top100_ratio, попутно печатая полный отчёт."""
    test_df = add_predictions(test_df, model, feature_cols, cat_cols)
    train_df_pred = add_predictions(train_df, model, feature_cols, cat_cols)

    print("=" * 80)
    print(f"ОБЩИЕ МЕТРИКИ {label}")
    print("=" * 80)
    test_m = em.compute_metrics(test_df["price_cents"].values, test_df["pred_price_cents"].values)
    train_m = em.compute_metrics(train_df_pred["price_cents"].values, train_df_pred["pred_price_cents"].values)
    em.print_metrics("train", train_m)
    em.print_metrics("test", test_m)

    print("\n--- vs Baseline (ChatGPT, тот же test-период) ---")
    for k, v in BASELINE_TEST_METRICS.items():
        new_v = test_m.get(k)
        better = (new_v < v) if "r2" not in k else (new_v > v)
        arrow = "лучше" if better else "хуже"
        print(f"  {k}: baseline={v}  new={new_v:.3f}  ({arrow})")

    print("\n" + "=" * 80)
    print("СЕГМЕНТ: топ-100 крафтов по sticker_value (test) — главная проблема старой модели")
    print("=" * 80)
    top100 = test_df.nlargest(100, "sum_ref_sticker_price")
    ratio = em.predicted_actual_ratio_summary(top100["price_cents"].values, top100["pred_price_cents"].values)
    print(f"n={ratio['n']}, median predicted/actual = {ratio['median_pred_over_actual']:.3f}")
    print(f"(baseline RAW ml до калибровки: {BASELINE_TOP100_STICKER_RATIO}, baseline ПОСЛЕ runtime-калибровки: ~1.00)")

    print("\n" + "=" * 80)
    print("СЕГМЕНТЫ (test)")
    print("=" * 80)
    high_value_thr = train_df["sum_ref_sticker_price"].quantile(0.99)
    liquidity_thr = train_df["skin_liquidity_cumcount"].quantile(0.25)
    rare_mask = rare_sticker_mask(train_df, test_df, max_count=5)

    segments = {
        "все стикер-крафты (>=1 наклейка)": test_df["sticker_count"] > 0,
        "без наклеек": test_df["sticker_count"] == 0,
        "1-2 наклейки": test_df["sticker_count"].between(1, 2),
        "3-5 наклеек": test_df["sticker_count"].between(3, 5),
        "Katowice 2014": test_df["has_katowice_2014"] == 1,
        "редкие стикеры (<=5 раз в train)": rare_mask,
        "дорогие крафты (sum_ref>p99 train)": test_df["sum_ref_sticker_price"] > high_value_thr,
        "низколиквидные скины (низ. 25% train)": test_df["skin_liquidity_cumcount"] < liquidity_thr,
        "дубликат-крафты (все наклейки одинаковые)": test_df["is_uniform_craft"] == 1,
        "quad-крафт (4 одинаковых)": test_df["is_quad_craft"] == 1,
    }
    report = em.segment_report(test_df, "price_cents", "pred_price_cents", segments)
    print(report.to_string(index=False))

    print("\nТоп-15 важности признаков")
    imp = pd.Series(model.get_feature_importance(), index=feature_cols).sort_values(ascending=False)
    print(imp.head(15).to_string())

    if model_low is not None and model_high is not None:
        print("\nПокрытие квантильного интервала [q15, q85] на test")
        low_pred = T.reconstruct_price(model_low.predict(test_df[feature_cols]), test_df["log_reference_predicted_price"].values)
        high_pred = T.reconstruct_price(model_high.predict(test_df[feature_cols]), test_df["log_reference_predicted_price"].values)
        inside = (test_df["price_cents"].values >= low_pred) & (test_df["price_cents"].values <= high_pred)
        print(f"Доля реальных цен внутри [q15,q85]: {inside.mean()*100:.1f}% (цель: ~70%)")

    return {"test_m": test_m, "train_m": train_m, "top100_ratio": ratio, "segment_report": report, "test_df": test_df}


def main():
    cache, model, model_low, model_high = load_artifacts()
    result = diagnostic_report(
        cache["train_df"], cache["test_df"], model, cache["feature_cols"], cache["cat_cols"],
        model_low=model_low, model_high=model_high, label="(финальная модель)",
    )
    result["test_df"].to_pickle(LEGACY_ARTIFACT_DIR / "test_with_predictions.pkl")
    print(
        "\ntest_df с предсказаниями сохранён в "
        f"{LEGACY_ARTIFACT_DIR / 'test_with_predictions.pkl'}"
    )


if __name__ == "__main__":
    main()
