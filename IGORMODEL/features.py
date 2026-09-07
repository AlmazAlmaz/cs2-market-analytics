"""
Построение признаков поверх плоского датасета из data_loading.build_dataset().

Ключевая архитектурная идея (в отличие от прошлой модели, которая училась
предсказывать log1p(price_cents) с нуля):

  reference_predicted_price — это собственная оценка CSFloat "справедливой"
  цены предмета БЕЗ учёта наклеек (только по float/wear). Для листингов без
  наклеек она уже сама по себе очень точна (медианное отношение
  price/reference == 1.00, IQR [0.94, 1.07] — см. аудит). Значит модели не
  нужно заново учить базовую цену оружия по флоату — это дорого и особенно
  плохо работает на редких комбинациях (редкие стикеры, низколиквидные
  скины), где данных мало.

  Вместо этого таргетом становится ЛОГ-ПРЕМИЯ:
      y = log1p(price_cents) - log1p(reference_predicted_price)
  То есть "во сколько раз реальная цена отличается от безстикерной базы".
  Для ~82% листингов без наклеек этот таргет близок к 0 с маленькой
  дисперсией (лёгкая часть задачи). Вся "мощность" модели уходит на то,
  чтобы объяснить отклонения от 0 — а это почти всегда наклейки (и в
  меньшей степени редкие paint_seed на pattern-чувствительных скинах).
  При инференсе цена восстанавливается как:
      price_pred = expm1(y_pred + log1p(reference_predicted_price))

  Второй важный момент, найденный при аудите: reference_price_cents у
  стикеров и reference_predicted_price у оружия — это фактически снепшот
  "на момент fetched_at", а не "на момент sold_at". Для большинства строк
  разрыв fetched_at-sold_at маленький (медиана 5 дней), но у части старых
  дозабэкфиленных продаж (в основном 2020-2024 годов, ~4% трейна) разрыв
  доходит до многих месяцев и даже лет — а именно в эту категорию чаще
  попадают дорогие раритетные стикеры типа Katowice 2014 (см. память
  аудита: Titan Katowice reference был буквально ЗАМОРОЖЕН на одном
  значении все 2022-2024 и начал "плыть" только в 2025-2026). Поэтому вместо
  того, чтобы слепо доверять reference-фичам, модели даётся явный признак
  "насколько устарел reference" (gap_days), чтобы она сама научилась делать
  им скидку на редких старых сделках.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    from . import feature_contract as contract
except ImportError:  # Direct execution from IGORMODEL/
    import feature_contract as contract

MAX_STICKER_SLOTS = contract.MAX_STICKER_SLOTS
MAX_KEYCHAIN_SLOTS = contract.MAX_KEYCHAIN_SLOTS
STATTRAK_PREFIX = "StatTrak™ "
SOUVENIR_PREFIX = "Souvenir "

ROLLING_TREND_WINDOW = 200
MIN_TREND_HISTORY = 10

CAT_FEATURES = (
    [
        "weapon_type",
        "wear_name",
        "skin_seed_key",
        "item_rarity_name",
        "paint_index_cat",
    ]
    + [f"sticker_{i}_id_cat" for i in range(MAX_STICKER_SLOTS)]
    + [f"keychain_{i}_id_cat" for i in range(MAX_KEYCHAIN_SLOTS)]
    + [f"keychain_{i}_pattern_cat" for i in range(MAX_KEYCHAIN_SLOTS)]
)

NUMERIC_FEATURES = (
    [
        "float_value",
        "paint_seed",
        "is_stattrak",
        "is_souvenir",
        "log_reference_predicted_price",
        "log_reference_base_price",
        "reference_float_factor",
        "log_reference_quantity",
        "has_reference",
        "has_reference_anchor",
        "has_reference_keychain_price",
        "log_reference_keychain_price",
        "sold_year",
        "sold_month",
        "sold_dow",
        "sold_hour",
        "seed_popularity",
        "weapon_type_recent_premium",
        "skin_liquidity_cumcount",
        # стикеры (агрегаты)
        "sticker_count",
        "n_distinct_stickers",
        "log_sum_ref_sticker_price",
        "log_max_ref_sticker_price",
        "log_mean_ref_sticker_price",
        "log_min_ref_sticker_price",
        "mean_sticker_wear",
        "has_katowice_2014",
        "has_premium_finish",
        "has_slot_ge5",
        "is_quad_craft",
        "is_uniform_craft",
        "craft_fill_ratio",
        "sticker_reference_safe_count",
        "sticker_reference_future_count",
        "sticker_reference_unknown_count",
        # charms/keychains
        "keychain_count",
        "n_distinct_keychains",
        "log_sum_ref_keychain_price",
        "log_max_ref_keychain_price",
        "keychain_reference_safe_count",
        "keychain_reference_future_count",
        "keychain_reference_unknown_count",
    ]
    + [f"sticker_{i}_wear" for i in range(MAX_STICKER_SLOTS)]
    + [f"log_sticker_{i}_ref_price" for i in range(MAX_STICKER_SLOTS)]
    + [f"log_keychain_{i}_ref_price" for i in range(MAX_KEYCHAIN_SLOTS)]
)

ALL_FEATURES = CAT_FEATURES + NUMERIC_FEATURES

TARGET_RAW = "price_cents"


def _strip_prefixes(name: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    is_stattrak = name.str.startswith(STATTRAK_PREFIX)
    is_souvenir = name.str.startswith(SOUVENIR_PREFIX)
    cleaned = name.where(~is_stattrak, name.str[len(STATTRAK_PREFIX) :])
    cleaned = cleaned.where(~is_souvenir, cleaned.str[len(SOUVENIR_PREFIX) :])
    return cleaned, is_stattrak.astype(int), is_souvenir.astype(int)


def parse_weapon_identity(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    name = df["name"].astype("string").str.strip()
    has_wear_suffix = name.str.endswith(")", na=False) & name.str.contains(
        "(", regex=False, na=False
    )
    derived_base = name.where(~has_wear_suffix, name.str.rsplit("(", n=1).str[0].str.strip())
    derived_wear = name.str.rsplit("(", n=1).str[-1].str[:-1].where(
        has_wear_suffix
    )
    base = df["base_skin_name"].astype("string").str.strip()
    base = base.where(base.notna() & base.ne(""), derived_base)
    wear = df["wear_name"].astype("string").str.strip()
    df["wear_name"] = wear.where(wear.notna() & wear.ne(""), derived_wear)
    weapon_type, is_stattrak, is_souvenir = _strip_prefixes(base)
    df["weapon_type"] = weapon_type
    df["wear_name"] = contract.canonical_text_category_series(df["wear_name"])
    df["item_rarity_name"] = contract.canonical_text_category_series(
        df["item_rarity_name"]
    )
    # item_is_stattrak из БД пустой у ~14% строк (не докачались метаданные) -
    # строковый парсинг name всегда доступен и надёжнее
    df["is_stattrak"] = is_stattrak
    df["is_souvenir"] = is_souvenir
    return df


def build_seed_keys(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    seed_str = contract.canonical_integer_category_series(df["paint_seed"])
    df["paint_seed"] = pd.to_numeric(seed_str, errors="coerce").fillna(-1)
    df["float_value"] = (
        pd.to_numeric(df["float_value"], errors="coerce").fillna(-1.0)
    )
    df["skin_seed_key"] = df["weapon_type"].astype(str) + "_" + seed_str
    df["paint_index_cat"] = contract.canonical_integer_category_series(
        df["paint_index"]
    )
    return df


class SeedPopularityEncoder:
    def __init__(self):
        self.counts_: dict[str, int] = {}

    def fit(self, df: pd.DataFrame) -> SeedPopularityEncoder:
        self.counts_ = df["skin_seed_key"].value_counts().to_dict()
        return self

    def transform(self, df: pd.DataFrame) -> pd.Series:
        return df["skin_seed_key"].map(self.counts_).fillna(0).astype(int)


def build_reference_features(
    df: pd.DataFrame,
    *,
    contract_version: int = 1,
) -> pd.DataFrame:
    df = df.copy()
    reference = pd.to_numeric(df["reference_predicted_price"], errors="coerce")
    base = pd.to_numeric(df["reference_base_price"], errors="coerce")
    valid_reference = reference.notna() & (reference > 0)

    if contract_version < 2:
        df["has_reference"] = valid_reference.astype(int)

        # Legacy fallback retained byte-for-byte for old Igor/Nova artifacts.
        ref = reference.where(valid_reference)
        ref = ref.fillna(base.where(base > 0))
        fallback_source = reference.where(valid_reference)
        fallback_group = fallback_source.groupby(
            [df["weapon_type"], df["wear_name"]]
        ).transform("median")
        ref = ref.fillna(fallback_group)
        ref = ref.fillna(fallback_source.median()).fillna(1.0).clip(lower=1.0)
        df["reference_predicted_price_filled"] = ref
        df["log_reference_predicted_price"] = np.log1p(ref)
        df["log_reference_base_price"] = np.log1p(
            df["reference_base_price"].fillna(ref).clip(lower=1.0)
        )
        df["reference_float_factor"] = df["reference_float_factor"].fillna(1.0)
        df["log_reference_quantity"] = np.log1p(
            df["reference_quantity"].fillna(0).clip(lower=0)
        )
        return df

    sold = pd.to_datetime(df["sold_at"], errors="coerce", utc=True)
    updated = pd.to_datetime(
        df.get("reference_last_updated"), errors="coerce", utc=True
    )
    runtime_observed = (
        pd.to_numeric(
            df.get("reference_is_runtime_observed", pd.Series(0, index=df.index)),
            errors="coerce",
        )
        .fillna(0)
        .astype(bool)
    )
    reference_asof_safe = runtime_observed | (
        sold.notna() & updated.notna() & (updated <= sold)
    )
    df["reference_asof_safe"] = reference_asof_safe.astype(int)
    df["reference_asof_future"] = (
        ~runtime_observed & sold.notna() & updated.notna() & (updated > sold)
    ).astype(int)
    df["reference_asof_unknown"] = (
        ~runtime_observed & (sold.isna() | updated.isna())
    ).astype(int)

    valid_predicted = reference_asof_safe & valid_reference
    valid_base = reference_asof_safe & base.notna() & (base > 0)
    anchor_available = valid_predicted | valid_base
    source_reference = reference.where(valid_predicted).fillna(base.where(valid_base))

    # CSFloat's predicted reference includes the attached charm component
    # (observed rows satisfy predicted ~= base + keychain_price).  Remove it
    # from the residual anchor and expose it separately as a charm feature.
    keychain_component = (
        pd.to_numeric(
            df.get("reference_keychain_price", pd.Series(0, index=df.index)),
            errors="coerce",
        )
        .fillna(0)
        .clip(lower=0)
    )
    keychain_component = keychain_component.where(
        reference_asof_safe & valid_predicted, 0.0
    )
    keychain_component = contract.coherent_keychain_component_series(
        source_reference, keychain_component
    )
    anchor = (source_reference - keychain_component).where(anchor_available, 1.0)
    anchor = anchor.fillna(1.0).clip(lower=1.0)

    df["has_reference"] = valid_predicted.astype(int)
    df["has_reference_anchor"] = anchor_available.astype(int)
    df["reference_contract_valid"] = anchor_available.astype(int)
    df["reference_predicted_price_filled"] = anchor
    df["reference_keychain_price_safe"] = keychain_component
    df["has_reference_keychain_price"] = (keychain_component > 0).astype(int)
    df["log_reference_keychain_price"] = np.log1p(keychain_component)
    df["log_reference_predicted_price"] = np.log1p(anchor)
    df["log_reference_base_price"] = np.log1p(
        base.where(valid_base, anchor).fillna(anchor).clip(lower=1.0)
    )
    df["reference_float_factor"] = (
        pd.to_numeric(df["reference_float_factor"], errors="coerce")
        .where(reference_asof_safe, 1.0)
        .fillna(1.0)
    )
    quantity = pd.to_numeric(df["reference_quantity"], errors="coerce")
    df["log_reference_quantity"] = np.log1p(
        quantity.where(reference_asof_safe, 0).fillna(0).clip(lower=0)
    )

    return df


def build_staleness_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    sold_dt = pd.to_datetime(df["sold_at"], errors="coerce", utc=True)
    fetched_dt = pd.to_datetime(df["fetched_at"], errors="coerce", utc=True)
    created_dt = pd.to_datetime(df["created_at"], errors="coerce", utc=True)

    gap_days = (fetched_dt - sold_dt).dt.total_seconds() / 86400.0
    df["log_gap_days"] = np.log1p(gap_days.clip(lower=0).fillna(0))

    df["sold_dt"] = sold_dt.fillna(fetched_dt).fillna(created_dt)
    df["sold_year"] = df["sold_dt"].dt.year
    df["sold_month"] = df["sold_dt"].dt.month
    df["sold_dow"] = df["sold_dt"].dt.dayofweek
    df["sold_hour"] = df["sold_dt"].dt.hour
    return df


def build_sticker_derived_features(
    df: pd.DataFrame,
    *,
    contract_version: int = 1,
) -> pd.DataFrame:
    df = df.copy()
    if contract_version < 2 and "raw_sum_ref_sticker_price" in df:
        for prefix in ("sum", "max", "mean", "min"):
            df[f"{prefix}_ref_sticker_price"] = df[f"raw_{prefix}_ref_sticker_price"]
        for idx in range(MAX_STICKER_SLOTS):
            raw_col = f"sticker_{idx}_raw_ref_price_cents"
            if raw_col in df:
                df[f"sticker_{idx}_ref_price_cents"] = df[raw_col]
    for col in [
        "sum_ref_sticker_price",
        "max_ref_sticker_price",
        "mean_ref_sticker_price",
        "min_ref_sticker_price",
    ]:
        df[f"log_{col}"] = np.log1p(df[col].clip(lower=0))
    for idx in range(MAX_STICKER_SLOTS):
        df[f"log_sticker_{idx}_ref_price"] = np.log1p(
            df[f"sticker_{idx}_ref_price_cents"].clip(lower=0)
        )
        sticker_id = contract.canonical_integer_category_series(
            df[f"sticker_{idx}_id"]
        )
        df[f"sticker_{idx}_id"] = pd.to_numeric(sticker_id).astype("int64")
        df[f"sticker_{idx}_id_cat"] = sticker_id

    first4 = [f"sticker_{i}_id" for i in range(4)]
    sub = df[first4]
    quad_filled = (sub != -1).all(axis=1)
    quad_same = sub.eq(sub.iloc[:, 0], axis=0).all(axis=1)
    df["is_quad_craft"] = (quad_filled & quad_same).astype(int)

    df["is_uniform_craft"] = (
        (df["sticker_count"] > 1) & (df["n_distinct_stickers"] == 1)
    ).astype(int)

    df["craft_fill_ratio"] = df["sticker_count"] / MAX_STICKER_SLOTS
    return df


def build_keychain_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in ("sum_ref_keychain_price", "max_ref_keychain_price"):
        df[f"log_{col}"] = np.log1p(
            pd.to_numeric(df[col], errors="coerce").fillna(0).clip(lower=0)
        )
    for idx in range(MAX_KEYCHAIN_SLOTS):
        keychain_id = contract.canonical_integer_category_series(
            df[f"keychain_{idx}_id"]
        )
        keychain_pattern = contract.canonical_integer_category_series(
            df[f"keychain_{idx}_pattern"]
        )
        df[f"keychain_{idx}_id"] = pd.to_numeric(keychain_id).astype("int64")
        df[f"keychain_{idx}_pattern"] = pd.to_numeric(keychain_pattern).astype(
            "int64"
        )
        df[f"keychain_{idx}_id_cat"] = keychain_id
        df[f"keychain_{idx}_pattern_cat"] = keychain_pattern
        df[f"log_keychain_{idx}_ref_price"] = np.log1p(
            pd.to_numeric(df[f"keychain_{idx}_ref_price_cents"], errors="coerce")
            .fillna(0)
            .clip(lower=0)
        )
    return df


def add_weapon_type_premium_trend(
    df: pd.DataFrame,
    premium_col: str,
    window: int = ROLLING_TREND_WINDOW,
    min_periods: int = MIN_TREND_HISTORY,
) -> pd.DataFrame:
    """
    Скользящая (строго по прошлым продажам, shift(1)) медиана лог-премии
    по weapon_type — сигнал "в последнее время этот скин продаётся с такой-то
    надбавкой/скидкой к reference". Помогает там, где reference систематически
    промахивается (например, pattern-чувствительные скины типа Printstream,
    где reference учитывает только float, а не paint_seed).
    """
    original = df.copy()
    ordered = original.sort_values(
        ["weapon_type", "sold_dt", "listing_id"]
    ).copy()
    group_keys = ["weapon_type", "sold_dt"]
    within_timestamp = ordered.groupby(
        group_keys, sort=False, dropna=False
    ).cumcount()
    raw_weapon_trend = ordered.groupby("weapon_type", sort=False)[
        premium_col
    ].transform(
        lambda values: values.shift(1)
        .rolling(window=window, min_periods=min_periods)
        .median()
    )
    weapon_group_id = ordered.groupby(
        group_keys, sort=False, dropna=False
    ).ngroup()
    first_in_group = within_timestamp.eq(0)
    weapon_value_by_group = pd.Series(
        raw_weapon_trend.loc[first_in_group].to_numpy(),
        index=weapon_group_id.loc[first_in_group].to_numpy(),
    )
    weapon_trend = weapon_group_id.map(weapon_value_by_group)
    weapon_trend.index = ordered.index

    chronological = original.sort_values(["sold_dt", "listing_id"]).copy()
    raw_global_trend = (
        chronological[premium_col]
        .shift(1)
        .expanding(min_periods=min_periods)
        .median()
    )
    global_group_id = chronological.groupby(
        "sold_dt", sort=False, dropna=False
    ).ngroup()
    first_global = chronological.groupby(
        "sold_dt", sort=False, dropna=False
    ).cumcount().eq(0)
    global_value_by_group = pd.Series(
        raw_global_trend.loc[first_global].to_numpy(),
        index=global_group_id.loc[first_global].to_numpy(),
    )
    global_trend = global_group_id.map(global_value_by_group)
    global_trend.index = chronological.index

    original["weapon_type_recent_premium"] = weapon_trend.reindex(original.index)
    original["weapon_type_recent_premium"] = original[
        "weapon_type_recent_premium"
    ].fillna(global_trend.reindex(original.index))
    df = original
    df["weapon_type_recent_premium"] = df["weapon_type_recent_premium"].fillna(0.0)
    return df


def add_liquidity_cumcount(df: pd.DataFrame) -> pd.DataFrame:
    """Count only sales at strictly earlier timestamps for this weapon."""

    original = df.copy()
    ordered = original.sort_values(
        ["weapon_type", "sold_dt", "listing_id"]
    ).copy()
    cumulative_position = ordered.groupby("weapon_type", sort=False).cumcount()
    within_timestamp = ordered.groupby(
        ["weapon_type", "sold_dt"], sort=False, dropna=False
    ).cumcount()
    strictly_prior_count = cumulative_position - within_timestamp
    original["skin_liquidity_cumcount"] = np.log1p(
        strictly_prior_count.reindex(original.index)
    )
    return original


def build_time_safe_features(
    df: pd.DataFrame,
    *,
    contract_version: int = contract.FEATURE_CONTRACT_VERSION,
) -> pd.DataFrame:
    df = parse_weapon_identity(df)
    df = build_seed_keys(df)
    df = build_reference_features(df, contract_version=contract_version)
    df = build_staleness_features(df)
    df = build_sticker_derived_features(df, contract_version=contract_version)
    if contract_version >= 2:
        df = build_keychain_derived_features(df)

    df["log_price"] = np.log1p(df[TARGET_RAW].astype(float))
    df["log_premium"] = df["log_price"] - df["log_reference_predicted_price"]

    df = add_weapon_type_premium_trend(df, premium_col="log_premium")
    df = add_liquidity_cumcount(df)

    df["sale_type"] = df["sale_type"].fillna("unknown")

    return df


def feature_contract_definition() -> dict:
    return {
        "version": contract.FEATURE_CONTRACT_VERSION,
        "cat_features": CAT_FEATURES,
        "numeric_features": NUMERIC_FEATURES,
        "target": "log1p(price_cents)-log1p(reference_without_keychain_cents)",
        "missing_wear": contract.MISSING_WEAR,
        "max_sticker_slots": MAX_STICKER_SLOTS,
        "max_keychain_slots": MAX_KEYCHAIN_SLOTS,
        "reference_policy": "updated_at<=sold_at_or_live_observed",
        "child_reference_policy": "mask_future_or_unknown",
        "integer_category_policy": "canonical_int_string_or_minus_one",
        "missing_seed_policy": "numeric_minus_one_and_skin_seed_suffix_minus_one",
        "missing_float_policy": "numeric_minus_one",
        "missing_text_category_policy": {
            "wear_name": "unknown",
            "item_rarity_name": "unknown",
        },
        "missing_identity_fallback_policy": "derive_base_and_wear_from_full_name",
        "runtime_keychain_component_policy": (
            "exact_live_listing_aggregate_else_zero_child_features_separate"
        ),
        "keychain_component_coherence_policy": (
            "predicted_reference_only_and_zero_unless_0_lt_component_lt_reference"
        ),
        "target_history_policy": "strictly_earlier_timestamp_group",
        "liquidity_history_policy": "strictly_earlier_timestamp_group",
        "interval_policy": "envelope_contains_point_q15_q85",
    }


FEATURE_CONTRACT_SHA256 = contract.canonical_sha256(feature_contract_definition())


if __name__ == "__main__":
    import data_loading

    raw = data_loading.build_dataset()
    feats = build_time_safe_features(raw)
    print(f"rows: {len(feats)}")
    missing = [c for c in ALL_FEATURES if c not in feats.columns]
    print("missing feature cols:", missing)
    print(
        feats[
            [
                "log_premium",
                "log_reference_predicted_price",
                "weapon_type_recent_premium",
            ]
        ].describe()
    )
