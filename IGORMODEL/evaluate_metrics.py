"""Метрики оценки — те же определения, что использовал ChatGPT в своём отчёте,
чтобы сравнение было честным яблоки-с-яблоками."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


def compute_metrics(true_cents: np.ndarray, pred_cents: np.ndarray) -> dict:
    true_cents = np.asarray(true_cents, dtype=float)
    pred_cents = np.maximum(np.asarray(pred_cents, dtype=float), 1.0)

    y_true_log = np.log1p(true_cents)
    y_pred_log = np.log1p(pred_cents)

    ape = np.abs(pred_cents - true_cents) / np.maximum(true_cents, 1.0)

    return {
        "n": int(len(true_cents)),
        "mae_cents": float(mean_absolute_error(true_cents, pred_cents)),
        "mae_usd": float(mean_absolute_error(true_cents, pred_cents) / 100),
        "median_ape_%": float(np.median(ape) * 100),
        "mean_ape_%": float(np.mean(ape) * 100),
        "p75_ape_%": float(np.percentile(ape, 75) * 100),
        "p90_ape_%": float(np.percentile(ape, 90) * 100),
        "rmse_log_price": float(np.sqrt(mean_squared_error(y_true_log, y_pred_log))),
        "r2_log_price": float(r2_score(y_true_log, y_pred_log)),
    }


def print_metrics(label: str, m: dict) -> None:
    print(f"--- {label} (n={m['n']}) ---")
    print(f"  MAE: {m['mae_cents']:.2f} cents = ${m['mae_usd']:.2f}")
    print(f"  median APE: {m['median_ape_%']:.2f}%")
    print(f"  mean APE:   {m['mean_ape_%']:.2f}%")
    print(f"  p75 APE:    {m['p75_ape_%']:.2f}%")
    print(f"  p90 APE:    {m['p90_ape_%']:.2f}%")
    print(f"  RMSE log:   {m['rmse_log_price']:.4f}")
    print(f"  R2 log:     {m['r2_log_price']:.4f}")


def predicted_actual_ratio_summary(true_cents: np.ndarray, pred_cents: np.ndarray) -> dict:
    true_cents = np.asarray(true_cents, dtype=float)
    pred_cents = np.maximum(np.asarray(pred_cents, dtype=float), 1.0)
    ratio = pred_cents / np.maximum(true_cents, 1.0)
    return {
        "n": int(len(ratio)),
        "median_pred_over_actual": float(np.median(ratio)),
        "mean_pred_over_actual": float(np.mean(ratio)),
    }


def segment_report(df: pd.DataFrame, true_col: str, pred_col: str, segments: dict[str, pd.Series]) -> pd.DataFrame:
    rows = []
    for seg_name, mask in segments.items():
        sub = df[mask]
        if len(sub) == 0:
            continue
        m = compute_metrics(sub[true_col].values, sub[pred_col].values)
        ratio = predicted_actual_ratio_summary(sub[true_col].values, sub[pred_col].values)
        rows.append({
            "segment": seg_name,
            "n": m["n"],
            "median_ape_%": round(m["median_ape_%"], 2),
            "mean_ape_%": round(m["mean_ape_%"], 2),
            "median_pred/actual": round(ratio["median_pred_over_actual"], 3),
        })
    return pd.DataFrame(rows)
