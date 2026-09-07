"""Shared temporal data contract for Almaz-like price models.

Keep this module dependency-free: both the trainer and the lightweight job
eligibility checks import it.
"""

from __future__ import annotations

import re

# These legacy feature names are part of already-published Almaz artifacts.
# Their values must describe the instant at which inference could first happen,
# never the later instant at which a listing was marked sold.
OBSERVATION_TIMESTAMP_KEYS = (
    "first_seen_at",
    "fetched_at",
    "listing_created_at",
    "created_at",
    "listed_at",
)


def time_safe_reference_sql(table_alias: str | None = None) -> str:
    """Return the canonical SQLite predicate for a usable reference snapshot."""

    if table_alias is not None and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", table_alias
    ):
        raise ValueError("table_alias must be a simple SQL identifier")
    prefix = f"{table_alias}." if table_alias else ""
    return f"""
        {prefix}reference_last_updated IS NOT NULL
        AND julianday({prefix}reference_last_updated) IS NOT NULL
        AND julianday({prefix}sold_at) IS NOT NULL
        AND julianday({prefix}reference_last_updated) <= julianday({prefix}sold_at)
        AND (
            ({prefix}reference_predicted_price IS NOT NULL
             AND {prefix}reference_predicted_price > 0)
            OR
            ({prefix}reference_base_price IS NOT NULL
             AND {prefix}reference_base_price > 0)
        )
    """
