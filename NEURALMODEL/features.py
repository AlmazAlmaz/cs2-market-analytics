"""Feature contract shared by Nova training and live inference.

Nova v2 deliberately reuses Igor's audited, time-safe scalar contract.  The
legacy lists remain named for loading the already deployed v1 metadata, while
new training always uses the complete v2 lists (including keychains and
without post-listing watcher fields).
"""

from IGORMODEL import features as igor_features

LEGACY_CAT_FEATURES = [
    "weapon_type",
    "wear_name",
    "skin_seed_key",
    "item_rarity_name",
    "sale_type",
    "paint_index_cat",
    "sticker_0_id_cat",
    "sticker_1_id_cat",
    "sticker_2_id_cat",
    "sticker_3_id_cat",
    "sticker_4_id_cat",
]

LEGACY_NUMERIC_FEATURES = [
    "float_value",
    "paint_seed",
    "is_stattrak",
    "is_souvenir",
    "log_reference_predicted_price",
    "log_reference_base_price",
    "reference_float_factor",
    "log_reference_quantity",
    "has_reference",
    "log_gap_days",
    "watchers",
    "is_watchlisted",
    "sold_year",
    "sold_month",
    "sold_dow",
    "sold_hour",
    "weapon_type_recent_premium",
    "skin_liquidity_cumcount",
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
    "sticker_0_wear",
    "sticker_1_wear",
    "sticker_2_wear",
    "sticker_3_wear",
    "sticker_4_wear",
    "log_sticker_0_ref_price",
    "log_sticker_1_ref_price",
    "log_sticker_2_ref_price",
    "log_sticker_3_ref_price",
    "log_sticker_4_ref_price",
]

FEATURE_CONTRACT_VERSION = 2
FEATURE_CONTRACT_SHA256 = igor_features.FEATURE_CONTRACT_SHA256
CAT_FEATURES = list(igor_features.CAT_FEATURES)
NUMERIC_FEATURES = list(igor_features.NUMERIC_FEATURES)


def embedding_dim(cardinality: int) -> int:
    if cardinality <= 16:
        return 4
    if cardinality <= 128:
        return 8
    if cardinality <= 512:
        return 12
    if cardinality <= 2_048:
        return 16
    if cardinality <= 8_192:
        return 24
    return 32
