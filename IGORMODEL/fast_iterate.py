"""
Быстрый цикл эксперимент -> результат (~3-5 минут вместо часа), чтобы
проверять гипотезы на подвыборке train, но оценивать честно на ПОЛНОМ test.

Использование: python fast_iterate.py
Или из кода: run_experiment(name=..., weight_col=..., train_sample_size=...)
"""
from __future__ import annotations

import time

import pandas as pd
from catboost import CatBoostRegressor

import build_cache
import features
import train as T
import evaluate_segments as ES

CACHE_PATH = build_cache.CACHE_PATH

_FULL_DF_CACHE = {}


def get_full_df() -> pd.DataFrame:
    if "df" not in _FULL_DF_CACHE:
        if not CACHE_PATH.exists():
            build_cache.build_and_cache()
        t0 = time.time()
        _FULL_DF_CACHE["df"] = pd.read_pickle(CACHE_PATH)
        print(f"[cache] загружено за {time.time()-t0:.1f}s, {len(_FULL_DF_CACHE['df'])} строк")
    return _FULL_DF_CACHE["df"].copy()


def run_experiment(
    name: str,
    train_sample_size: int = 150_000,
    weight_fn=None,
    feature_overrides: list[str] | None = None,
    iterations: int = 1200,
    depth: int = 6,
    learning_rate: float = 0.08,
    early_stopping_rounds: int = 60,
    seed: int = 42,
    show_diagnostics: bool = True,
):
    df = get_full_df()
    train_df, test_df = T.time_split(df)

    if train_sample_size and len(train_df) > train_sample_size:
        train_df = train_df.sample(n=train_sample_size, random_state=seed).sort_values(["sold_dt", "listing_id"])

    T.fit_seed_encoder(train_df, test_df)

    feature_cols = feature_overrides or [c for c in features.ALL_FEATURES if c in train_df.columns]
    cat_cols = [c for c in features.CAT_FEATURES if c in feature_cols]

    weight_col = None
    if weight_fn is not None:
        train_df = train_df.copy()
        train_df["_weight"] = weight_fn(train_df)
        weight_col = "_weight"

    train_pool, test_pool = T.make_pools(train_df, test_df, feature_cols, cat_cols, "log_premium", weight_col=weight_col)

    print(f"\n{'='*80}\nЭКСПЕРИМЕНТ: {name}\n{'='*80}")
    print(f"train (подвыборка): {len(train_df)}, test (полный): {len(test_df)}, фичей: {len(feature_cols)}")

    t0 = time.time()
    model = CatBoostRegressor(
        loss_function="MAE",
        iterations=iterations,
        learning_rate=learning_rate,
        depth=depth,
        l2_leaf_reg=3.0,
        random_seed=seed,
        early_stopping_rounds=early_stopping_rounds,
        thread_count=T.THREAD_COUNT,
        verbose=False,
    )
    model.fit(train_pool, eval_set=test_pool, use_best_model=True)
    elapsed = time.time() - t0
    print(f"обучение: {elapsed:.1f}s, best_iteration={model.get_best_iteration()}")

    if show_diagnostics:
        result = ES.diagnostic_report(train_df, test_df, model, feature_cols, cat_cols, label=f"[{name}]")
    else:
        test_pred_df = ES.add_predictions(test_df, model, feature_cols, cat_cols)
        test_m = __import__("evaluate_metrics").compute_metrics(
            test_pred_df["price_cents"].values, test_pred_df["pred_price_cents"].values
        )
        top100 = test_pred_df.nlargest(100, "sum_ref_sticker_price")
        ratio = __import__("evaluate_metrics").predicted_actual_ratio_summary(
            top100["price_cents"].values, top100["pred_price_cents"].values
        )
        print(f"test: median_ape={test_m['median_ape_%']:.2f}% mean_ape={test_m['mean_ape_%']:.2f}% "
              f"r2_log={test_m['r2_log_price']:.4f} | top100_sticker predicted/actual={ratio['median_pred_over_actual']:.3f}")
        result = {"test_m": test_m, "top100_ratio": ratio}

    result["elapsed"] = elapsed
    result["model"] = model
    return result


if __name__ == "__main__":
    run_experiment("baseline fast (150k sample, depth6, 1200 iter)")
