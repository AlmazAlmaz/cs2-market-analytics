"""Shared Igor train/runtime feature contract primitives.

Keep scalar normalization here so the training loader and live adapter cannot
silently drift.  This module intentionally has no project imports and is safe
to import both as ``IGORMODEL.feature_contract`` and from direct scripts.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import numpy as np
import pandas as pd

FEATURE_CONTRACT_VERSION = 2
MISSING_WEAR = -1.0
MAX_STICKER_SLOTS = 5
MAX_KEYCHAIN_SLOTS = 2


def normalize_sticker_wear(value: Any) -> float:
    """Normalize CSFloat wear once; preserve the -1 missing sentinel."""

    if value is None or value == "":
        return MISSING_WEAR
    try:
        wear = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return MISSING_WEAR
    if not math.isfinite(wear) or wear < 0:
        return MISSING_WEAR
    if wear > 1:
        wear /= 100.0
    return min(1.0, max(0.0, wear))


def normalize_sticker_wear_series(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    numeric = numeric.where(numeric >= 0)
    numeric = numeric.where(numeric <= 1, numeric / 100.0)
    return numeric.clip(lower=0.0, upper=1.0)


def canonical_integer_category_series(
    values: pd.Series, *, missing: int = -1
) -> pd.Series:
    """Canonicalize integer-like categories identically after mixed-row merges.

    Pandas promotes nullable integer columns to floats after a merge, which used
    to turn keychain ``501`` into the training category ``"501.0"`` while live
    inference produced ``"501"``.  Invalid or fractional values use the same
    explicit missing category instead of being rounded into a real ID.
    """

    numeric = pd.to_numeric(values, errors="coerce")
    finite = numeric.notna() & np.isfinite(numeric)
    integral = finite & np.isclose(numeric, np.rint(numeric))
    canonical = numeric.where(integral, missing).fillna(missing).astype("int64")
    return canonical.astype(str)


def canonical_text_category_series(
    values: pd.Series, *, missing: str = "unknown"
) -> pd.Series:
    """Use one case-sensitive missing token across pandas and live payloads."""

    result = values.astype("string").str.strip()
    absent = result.isna() | result.eq("") | result.str.casefold().eq("unknown")
    return result.where(~absent, missing).astype(str)


def coherent_keychain_component_series(
    reference: pd.Series, component: pd.Series
) -> pd.Series:
    """Keep only a positive listing component strictly below its reference."""

    reference_values = pd.to_numeric(reference, errors="coerce")
    component_values = (
        pd.to_numeric(component, errors="coerce").fillna(0).clip(lower=0)
    )
    coherent = (
        reference_values.notna()
        & (reference_values > 0)
        & (component_values > 0)
        & (component_values < reference_values)
    )
    return component_values.where(coherent, 0.0)


def reference_asof_flags(updated_at: pd.Series, sold_at: pd.Series) -> pd.DataFrame:
    """Classify whether a reference value was demonstrably known at sale time."""

    updated = pd.to_datetime(updated_at, errors="coerce", utc=True)
    sold = pd.to_datetime(sold_at, errors="coerce", utc=True)
    known = updated.notna() & sold.notna()
    safe = known & (updated <= sold)
    future = known & (updated > sold)
    return pd.DataFrame(
        {
            "safe": safe.astype(np.int8),
            "future": future.astype(np.int8),
            "unknown": (~known).astype(np.int8),
        },
        index=updated_at.index,
    )


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def order_prediction_interval(point, low, high):
    """Return an interval containing point and both raw quantile predictions."""

    point_values = np.asarray(point)
    low_values = np.asarray(low)
    high_values = np.asarray(high)
    ordered_low = np.minimum(np.minimum(low_values, high_values), point_values)
    ordered_high = np.maximum(np.maximum(low_values, high_values), point_values)
    return ordered_low, ordered_high
