"""Pure helpers for selecting and previewing bounded Deal Scanner targets.

This module performs no network requests and imports no model runtime.  Both
the long-running scanner and the dashboard can therefore apply exactly the
same include/exclude semantics without loading an ML artifact.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import scanner_control

PROFILE_STICKER_GUNS = "sticker_guns"
PROFILE_LIQUID_UNDER_10 = "liquid_under_10"
PROFILE_KNIVES = "knives"
PROFILE_GLOVES = "gloves"

PROFILE_ORDER = (
    PROFILE_STICKER_GUNS,
    PROFILE_LIQUID_UNDER_10,
    PROFILE_KNIVES,
    PROFILE_GLOVES,
)

PROFILE_DEFINITIONS: dict[str, dict[str, Any]] = {
    PROFILE_STICKER_GUNS: {
        "source_type": "sticker",
        "target_entity": "sticker",
        "model_policy": "weapon_skin_models",
        "exact_market_name": False,
    },
    PROFILE_LIQUID_UNDER_10: {
        "source_type": "liquid_skin",
        "target_entity": "weapon_skin",
        "model_policy": "weapon_skin_models",
        "exact_market_name": True,
    },
    PROFILE_KNIVES: {
        "source_type": "knife",
        "target_entity": "knife",
        "model_policy": "dedicated_knife_model",
        "exact_market_name": True,
    },
    PROFILE_GLOVES: {
        "source_type": "glove",
        "target_entity": "glove",
        "model_policy": "dedicated_glove_model",
        "exact_market_name": True,
    },
}

MAX_PREVIEW_LIMIT = 200
MAX_PREVIEW_OFFSET = 1_000_000
MAX_SEARCH_LENGTH = 160


def profile_settings(
    target_control: Mapping[str, Any], profile: str
) -> dict[str, Any]:
    """Return a defensive copy of one already-validated profile."""

    if profile not in PROFILE_DEFINITIONS:
        raise ValueError(f"unknown scanner target profile: {profile}")
    profiles = target_control.get("profiles")
    if not isinstance(profiles, Mapping) or not isinstance(profiles.get(profile), Mapping):
        raise TypeError("scanner target control profiles are unavailable")
    raw = profiles[profile]
    return {
        "enabled": raw.get("enabled") is True,
        "selection_mode": str(raw.get("selection_mode") or ""),
        "include": list(raw.get("include") or []),
        "exclude": list(raw.get("exclude") or []),
        "targets_per_cycle": raw.get("targets_per_cycle"),
        "listings_per_target": raw.get("listings_per_target"),
        "include_groups": list(raw.get("include_groups") or []),
    }


def _normalized_candidates(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.get("target_key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(dict(candidate))
    return result


def apply_profile_selection(
    profile: str,
    candidates: Iterable[Mapping[str, Any]],
    target_control: Mapping[str, Any],
    *,
    recommended_limit: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select manual targets and groups before capped recommendations.

    ``recommended_limit`` caps only additional algorithmic recommendations;
    manual targets and included groups remain selected above that cap.
    Per-cycle request/listing/page budgets remain a separate scanner concern.
    ``None`` is used for catalog rotations (knives/gloves), whose durable
    windows provide the local cap while eventually covering the whole catalog.
    """

    settings = profile_settings(target_control, profile)
    rows = _normalized_candidates(candidates)
    by_key = {str(row["target_key"]): row for row in rows}
    include = list(dict.fromkeys(settings["include"]))
    excluded = set(settings["exclude"])
    unresolved = [key for key in include if key not in by_key]

    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()

    def append_key(key: str) -> None:
        if key in excluded or key in selected_keys or key not in by_key:
            return
        selected_keys.add(key)
        selected.append(by_key[key])

    automatic_candidates = 0
    automatic_truncated = False
    if settings["enabled"]:
        for key in include:
            append_key(key)
        for row in rows:
            if row.get("group_included") is True:
                append_key(str(row["target_key"]))
        if settings["selection_mode"] == "recommended":
            for row in rows:
                if row.get("recommended_candidate", True) is not True:
                    continue
                key = str(row["target_key"])
                if key in selected_keys or key in excluded or key not in by_key:
                    continue
                if (
                    recommended_limit is not None
                    and automatic_candidates >= int(recommended_limit)
                ):
                    automatic_truncated = True
                    continue
                append_key(key)
                automatic_candidates += 1

    configured_cap = None
    if recommended_limit is not None:
        if isinstance(recommended_limit, bool) or int(recommended_limit) < 1:
            raise ValueError("recommended_limit must be a positive integer or null")
        configured_cap = int(recommended_limit)

    return selected, {
        "enabled": settings["enabled"],
        "selection_mode": settings["selection_mode"],
        "eligible_count": len(rows),
        "recommended_count": sum(
            row.get("recommended_candidate", True) is True for row in rows
        ),
        "selected_count": len(selected),
        "configured_pool_cap": configured_cap,
        "selection_truncated": automatic_truncated,
        "manual_include_count": len(include),
        "manual_exclude_count": len(excluded),
        "included_group_count": len(settings.get("include_groups") or []),
        "unresolved_include": unresolved,
        "selected_target_keys": selected_keys,
    }


def _coerce_preview_integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return number


def build_profile_preview(
    profile: str,
    candidates: Iterable[Mapping[str, Any]],
    target_control: Mapping[str, Any],
    *,
    recommended_limit: int | None,
    budget: Mapping[str, Any],
    search: Any = "",
    limit: Any = 50,
    offset: Any = 0,
    scan_ready: bool | None = None,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-ready, paginated preview of exact local targets."""

    if profile not in PROFILE_DEFINITIONS:
        raise ValueError(f"unknown scanner target profile: {profile}")
    limit_value = _coerce_preview_integer(limit, "limit", 1, MAX_PREVIEW_LIMIT)
    offset_value = _coerce_preview_integer(offset, "offset", 0, MAX_PREVIEW_OFFSET)
    search_value = str(search or "").strip()
    if len(search_value) > MAX_SEARCH_LENGTH:
        raise ValueError(f"search must contain at most {MAX_SEARCH_LENGTH} characters")

    rows = _normalized_candidates(candidates)
    _selected, selection = apply_profile_selection(
        profile,
        rows,
        target_control,
        recommended_limit=recommended_limit,
    )
    selected_keys = selection.pop("selected_target_keys")
    settings = profile_settings(target_control, profile)
    include = set(settings["include"])
    exclude = set(settings["exclude"])
    ranked_recommended = [
        row for row in rows if row.get("recommended_candidate", True) is True
    ]
    recommended_keys = {
        str(row["target_key"])
        for row in (
            ranked_recommended
            if recommended_limit is None
            else ranked_recommended[: int(recommended_limit)]
        )
    }

    folded_search = search_value.casefold()
    visible: list[dict[str, Any]] = []
    for row in rows:
        key = str(row["target_key"])
        name = str(row.get("name") or row.get("market_hash_name") or "")
        if folded_search and folded_search not in f"{name} {key}".casefold():
            continue
        item = dict(row)
        item.update(
            {
                "name": name,
                "selected": key in selected_keys,
                "recommended": key in recommended_keys,
                "manually_included": key in include,
                "excluded": key in exclude,
            }
        )
        visible.append(item)

    total_matching = len(visible)
    page = visible[offset_value : offset_value + limit_value]
    requests_per_cycle = int(budget.get("targets_per_cycle") or 0) * int(
        budget.get("pages_per_target") or 0
    )
    normalized_budget = {
        "targets_per_cycle": int(budget.get("targets_per_cycle") or 0),
        "listings_per_target": int(budget.get("listings_per_target") or 0),
        "pages_per_target": int(budget.get("pages_per_target") or 0),
        "max_requests_per_cycle": requests_per_cycle,
    }

    if not settings["enabled"]:
        effective_ready = False
        effective_blocked_reason = "profile_disabled"
    else:
        effective_ready = scan_ready
        effective_blocked_reason = blocked_reason

    return {
        "schema_version": scanner_control.TARGET_CONTROL_SCHEMA_VERSION,
        "control_revision": target_control.get("revision"),
        "profile": profile,
        **PROFILE_DEFINITIONS[profile],
        "scan_ready": effective_ready,
        "blocked_reason": effective_blocked_reason,
        "selection": selection,
        "budget": normalized_budget,
        "search": search_value,
        "offset": offset_value,
        "limit": limit_value,
        "total_matching": total_matching,
        "next_offset": (
            offset_value + len(page)
            if offset_value + len(page) < total_matching
            else None
        ),
        "items": page,
    }
