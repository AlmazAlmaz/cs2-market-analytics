"""Feature contract shared by Vega training and live inference.

Vega deliberately consumes the same audited, time-safe scalar contract as
Nova and Igor.  Keeping this module as a narrow re-export gives Vega an
independent import surface without inventing a second interpretation of live
listing fields.
"""

from NEURALMODEL.features import (
    CAT_FEATURES,
    FEATURE_CONTRACT_SHA256,
    FEATURE_CONTRACT_VERSION,
    NUMERIC_FEATURES,
)

CLS_TOKEN_COUNT = 1
FEATURE_TOKEN_COUNT = CLS_TOKEN_COUNT + len(CAT_FEATURES) + len(NUMERIC_FEATURES)

__all__ = [
    "CAT_FEATURES",
    "CLS_TOKEN_COUNT",
    "FEATURE_CONTRACT_SHA256",
    "FEATURE_CONTRACT_VERSION",
    "FEATURE_TOKEN_COUNT",
    "NUMERIC_FEATURES",
]
