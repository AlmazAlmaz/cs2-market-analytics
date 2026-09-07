"""Lightweight local target catalog for Deal Scanner dashboard APIs.

Unlike :mod:`deal_scanner`, this module imports no model runtime and performs
no network requests.  It can safely be imported by ``dashboard_server`` to
preview exact stickers/skins/knives/gloves and apply the same durable target
selection contract used by the scanner process.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import app_config
import item_classification
import scanner_control
import scanner_target_profiles

DEFAULT_CONFIG_PATH = app_config.PROJECT_DIR / "deal_scanner_config.json"
DEFAULT_SKIN_CATALOG_PATH = app_config.path("CS2_SKINS_PATH", "ALL_SKINS.json")
MAX_LOCAL_CANDIDATES = 25_000
MAX_STICKER_SEARCH_CANDIDATES = 2_000

_PROFILE_BUDGETS = {
    scanner_target_profiles.PROFILE_STICKER_GUNS: (
        "stickers_per_cycle",
        "listings_per_sticker",
        "listing_pages_per_sticker",
        "max_sticker_targets",
        (1, 1000),
        (1, 50),
        (1, 10),
        (1, 5000),
    ),
    scanner_target_profiles.PROFILE_LIQUID_UNDER_10: (
        "liquid_skins_per_cycle",
        "listings_per_liquid_skin",
        "listing_pages_per_liquid_skin",
        "max_liquid_skin_targets",
        (1, 5),
        (1, 25),
        (1, 2),
        (1, 500),
    ),
    scanner_target_profiles.PROFILE_KNIVES: (
        "knives_per_cycle",
        "listings_per_knife",
        "listing_pages_per_knife",
        "max_knife_targets",
        (1, 2),
        (1, 20),
        (1, 2),
        (1, 100),
    ),
    scanner_target_profiles.PROFILE_GLOVES: (
        "gloves_per_cycle",
        "listings_per_glove",
        "listing_pages_per_glove",
        "max_glove_targets",
        (1, 2),
        (1, 20),
        (1, 2),
        (1, 100),
    ),
}

_CONFIG_DEFAULTS = {
    "stickers_per_cycle": 5,
    "listings_per_sticker": 50,
    "listing_pages_per_sticker": 1,
    "max_sticker_targets": 120,
    "liquid_skins_enabled": True,
    "liquid_skins_per_cycle": 2,
    "listings_per_liquid_skin": 20,
    "listing_pages_per_liquid_skin": 1,
    "max_liquid_skin_targets": 80,
    "liquid_skin_max_price_cents": 1000,
    "liquid_skin_min_estimated_sales_per_day": 0.5,
    "liquid_skin_min_quantity": 250,
    "liquid_skin_catalog_path": "ALL_SKINS.json",
    "knives_enabled": False,
    "knives_per_cycle": 1,
    "listings_per_knife": 10,
    "listing_pages_per_knife": 1,
    "max_knife_targets": 24,
    "gloves_enabled": False,
    "gloves_per_cycle": 1,
    "listings_per_glove": 10,
    "listing_pages_per_glove": 1,
    "max_glove_targets": 24,
    "tournament_patterns": ["Katowice 2014", "Katowice 2015"],
    "extra_sticker_names": ["Sticker | Crown (Foil)"],
    "enabled_models": ["almaz", "igor", "nova"],
}


def load_preview_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path or DEFAULT_CONFIG_PATH)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("deal scanner config is unreadable") from exc
    if not isinstance(raw, dict):
        raise TypeError("deal scanner config must contain a JSON object")
    return {**_CONFIG_DEFAULTS, **raw}


def profile_defaults(config: Mapping[str, Any]) -> dict[str, bool]:
    return {
        scanner_target_profiles.PROFILE_STICKER_GUNS: True,
        scanner_target_profiles.PROFILE_LIQUID_UNDER_10: bool(
            config.get("liquid_skins_enabled")
        ),
        scanner_target_profiles.PROFILE_KNIVES: bool(config.get("knives_enabled")),
        scanner_target_profiles.PROFILE_GLOVES: bool(config.get("gloves_enabled")),
    }


def build_target_control_payload(
    *,
    config: Mapping[str, Any] | None = None,
    target_control: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return controls plus stable UI metadata without opening the database."""

    config = dict(config or load_preview_config())
    control = dict(
        target_control
        or scanner_control.get_scanner_target_control(
            profile_defaults=profile_defaults(config),
            create_if_missing=True,
        )
    )
    control = scanner_control.validate_scanner_target_control(control)
    return {
        "control": control,
        "profile_order": list(scanner_target_profiles.PROFILE_ORDER),
        "profile_definitions": scanner_target_profiles.PROFILE_DEFINITIONS,
        "limits": {
            "selection_modes": sorted(scanner_control.TARGET_SELECTION_MODES),
            "max_included_targets": scanner_control.MAX_INCLUDED_TARGETS,
            "max_excluded_targets": scanner_control.MAX_EXCLUDED_TARGETS,
            "max_target_key_length": scanner_control.MAX_TARGET_KEY_LENGTH,
            "max_request_body_bytes": scanner_control.TARGET_CONTROL_BODY_LIMIT,
        },
    }


def update_target_control_from_payload(
    payload: Any,
    *,
    config: Mapping[str, Any] | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate the dashboard POST shape and commit one atomic revision."""

    if not isinstance(payload, dict):
        raise TypeError("body must be a JSON object")
    if set(payload) != {"expected_revision", "profiles"}:
        raise ValueError("body must contain only expected_revision and profiles")
    expected_revision = payload["expected_revision"]
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise TypeError("expected_revision must be a non-negative integer")
    config = dict(config or load_preview_config())
    profile_changes = payload["profiles"]
    if not isinstance(profile_changes, dict) or not profile_changes:
        raise ValueError("profiles must be a non-empty JSON object")
    unknown_profiles = sorted(
        set(profile_changes) - set(scanner_control.TARGET_PROFILE_NAMES)
    )
    if unknown_profiles:
        raise ValueError(
            f"unknown scanner target profiles: {', '.join(unknown_profiles)}"
        )

    defaults = profile_defaults(config)
    current = scanner_control.get_scanner_target_control(
        path=path,
        profile_defaults=defaults,
        create_if_missing=False,
    )
    if current.get("error"):
        raise scanner_control.ScannerControlError(
            "scanner target control is invalid; preserve or remove it before updating"
        )
    candidate = {
        **current,
        "profiles": {
            name: dict(settings) for name, settings in current["profiles"].items()
        },
    }
    for name, changes in profile_changes.items():
        if not isinstance(changes, dict) or not changes:
            raise ValueError(f"profile {name} changes must be a non-empty object")
        unknown_fields = sorted(
            set(changes) - scanner_control.TARGET_PROFILE_FILE_KEYS
        )
        if unknown_fields:
            raise ValueError(
                f"unexpected {name} fields: {', '.join(unknown_fields)}"
            )
        candidate["profiles"][name].update(changes)
    try:
        scanner_control.validate_scanner_target_control(candidate)
    except scanner_control.ScannerControlError as exc:
        # The persisted document above was already validated, so an error in
        # the merged candidate is a client-payload validation failure (400),
        # not a revision/state conflict (409).
        raise ValueError(str(exc)) from exc

    updated = scanner_control.update_scanner_target_control(
        profile_changes,
        expected_revision=expected_revision,
        profile_defaults=defaults,
        path=path,
    )
    return build_target_control_payload(config=config, target_control=updated)


def _bounded_int(value: Any, field: str, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if not bounds[0] <= number <= bounds[1]:
        raise ValueError(f"{field} must be between {bounds[0]} and {bounds[1]}")
    return number


def _profile_budget(profile: str, config: Mapping[str, Any]):
    try:
        (
            targets_field,
            listings_field,
            pages_field,
            pool_field,
            target_bounds,
            listing_bounds,
            page_bounds,
            pool_bounds,
        ) = _PROFILE_BUDGETS[profile]
    except KeyError as exc:
        raise ValueError(f"unknown scanner target profile: {profile}") from exc
    budget = {
        "targets_per_cycle": _bounded_int(
            config.get(targets_field), targets_field, target_bounds
        ),
        "listings_per_target": _bounded_int(
            config.get(listings_field), listings_field, listing_bounds
        ),
        "pages_per_target": _bounded_int(
            config.get(pages_field), pages_field, page_bounds
        ),
    }
    pool_limit = _bounded_int(config.get(pool_field), pool_field, pool_bounds)
    return budget, pool_limit


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return {str(info[1]) for info in conn.execute(f"PRAGMA table_info({table})")} if row else set()


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _config_string_list(config: Mapping[str, Any], field: str) -> list[str]:
    raw_values = config.get(field)
    if not isinstance(raw_values, list):
        raise TypeError(f"{field} must be a JSON list")
    if len(raw_values) > 100:
        raise ValueError(f"{field} may contain at most 100 values")
    values = []
    for raw in raw_values:
        if not isinstance(raw, str):
            raise TypeError(f"{field} values must be strings")
        value = raw.strip()
        if (
            not value
            or len(value) > 160
            or any(character in value for character in ("\x00", "\r", "\n"))
        ):
            raise ValueError(f"{field} contains an invalid value")
        values.append(value)
    return list(dict.fromkeys(values))


def _schedule_velocity(conn: sqlite3.Connection) -> dict[str, float]:
    columns = _table_columns(conn, "skin_schedule")
    if "market_hash_name" not in columns:
        return {}
    if "estimated_sales_per_day" in columns:
        expression = "COALESCE(estimated_sales_per_day, 0)"
    elif "avg_new_sales_count" in columns:
        expression = "COALESCE(avg_new_sales_count, 0)"
    else:
        expression = "0"
    result = {}
    for row in conn.execute(
        f"SELECT market_hash_name, MAX({expression}) FROM skin_schedule "
        "WHERE market_hash_name IS NOT NULL GROUP BY market_hash_name"
    ):
        name = str(row[0] or "").strip()
        if name:
            result[name] = max(_finite_float(row[1]), 0.0)
    return result


def _sticker_candidates(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    target_control: Mapping[str, Any],
    *,
    search: Any = "",
) -> list[dict[str, Any]]:
    columns = _table_columns(conn, "sale_stickers")
    if not {"sticker_id", "name"}.issubset(columns):
        return []
    price_expression = (
        "COALESCE(price_cents, reference_price_cents, 0)"
        if {"price_cents", "reference_price_cents"}.issubset(columns)
        else ("COALESCE(price_cents, 0)" if "price_cents" in columns else "0")
    )
    tournament_patterns = _config_string_list(config, "tournament_patterns")
    extra_sticker_names = _config_string_list(config, "extra_sticker_names")
    patterns = [value.casefold() for value in tournament_patterns]
    extras = {
        value.casefold()
        for value in extra_sticker_names
    }
    settings = scanner_target_profiles.profile_settings(
        target_control,
        scanner_target_profiles.PROFILE_STICKER_GUNS,
    )
    manual = set(settings["include"])
    manual_ids = []
    for key in manual:
        try:
            identifier = int(key.split(":", 1)[1])
        except (IndexError, TypeError, ValueError):
            continue
        if identifier >= 0:
            manual_ids.append(identifier)

    where_parts = []
    params: list[Any] = []
    for value in tournament_patterns:
        where_parts.append("name LIKE ?")
        params.append(f"%{value}%")
    for value in extra_sticker_names:
        where_parts.append("name = ?")
        params.append(value)
    if manual_ids:
        placeholders = ",".join("?" for _ in manual_ids)
        where_parts.append(f"sticker_id IN ({placeholders})")
        params.extend(manual_ids)
    search_value = str(search or "").strip()
    if search_value:
        escaped_search = (
            search_value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        where_parts.append("name LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped_search}%")
    if not where_parts:
        return []

    pool_limit = _bounded_int(
        config.get("max_sticker_targets"),
        "max_sticker_targets",
        (1, 5000),
    )
    query_limit = min(
        MAX_LOCAL_CANDIDATES,
        pool_limit
        + scanner_control.MAX_INCLUDED_TARGETS
        + (MAX_STICKER_SEARCH_CANDIDATES if search_value else 0),
    )
    manual_order = ""
    order_params: list[Any] = []
    if manual_ids:
        placeholders = ",".join("?" for _ in manual_ids)
        manual_order = f"CASE WHEN sticker_id IN ({placeholders}) THEN 0 ELSE 1 END,"
        order_params.extend(manual_ids)
    rows = []
    for row in conn.execute(
        f"""
        SELECT sticker_id, name, COUNT(*) AS seen_count,
               AVG({price_expression}) AS avg_price_cents,
               MAX({price_expression}) AS max_price_cents
        FROM sale_stickers
        WHERE sticker_id IS NOT NULL AND name IS NOT NULL
          AND ({' OR '.join(where_parts)})
        GROUP BY sticker_id, name
        ORDER BY {manual_order}
                 max_price_cents DESC, avg_price_cents DESC, seen_count DESC
        LIMIT ?
        """,
        (*params, *order_params, query_limit),
    ):
        name = str(row[1] or "").strip()
        key = f"sticker:{row[0]}"
        folded = name.casefold()
        recommended = (
            key in manual
            or folded in extras
            or any(pattern in folded for pattern in patterns)
        )
        rows.append(
            {
                "target_key": key,
                "name": name,
                "sticker_id": int(row[0]),
                "seen_count": int(row[2] or 0),
                "avg_price_cents": round(_finite_float(row[3])),
                "max_price_cents": round(_finite_float(row[4])),
                "recommended_candidate": recommended,
            }
        )
    rows.sort(
        key=lambda row: (
            -int(row["target_key"] in manual),
            -int(row["recommended_candidate"]),
            -row["max_price_cents"],
            -row["seen_count"],
            row["name"].casefold(),
        )
    )
    return rows


def _skin_catalog_path(config: Mapping[str, Any]) -> Path:
    raw = str(config.get("liquid_skin_catalog_path") or "ALL_SKINS.json").strip()
    if raw == "ALL_SKINS.json":
        return Path(DEFAULT_SKIN_CATALOG_PATH)
    path = Path(raw).expanduser()
    return path if path.is_absolute() else app_config.PROJECT_DIR / path


def _liquid_candidates(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    target_control: Mapping[str, Any],
) -> list[dict[str, Any]]:
    path = _skin_catalog_path(config)
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("liquid skin catalog is unreadable") from exc
    if not isinstance(catalog, list):
        raise TypeError("liquid skin catalog must contain a JSON list")
    max_price = _bounded_int(
        config.get("liquid_skin_max_price_cents"),
        "liquid_skin_max_price_cents",
        (1, 1000),
    )
    min_quantity = _bounded_int(
        config.get("liquid_skin_min_quantity"),
        "liquid_skin_min_quantity",
        (0, 1_000_000_000),
    )
    minimum_velocity = _finite_float(
        config.get("liquid_skin_min_estimated_sales_per_day"),
        -1,
    )
    if minimum_velocity < 0:
        raise ValueError("liquid_skin_min_estimated_sales_per_day must be at least 0")
    velocity = _schedule_velocity(conn)
    settings = scanner_target_profiles.profile_settings(
        target_control,
        scanner_target_profiles.PROFILE_LIQUID_UNDER_10,
    )
    manual = set(settings["include"])
    by_name: dict[str, dict[str, Any]] = {}
    for order, raw in enumerate(catalog[:MAX_LOCAL_CANDIDATES]):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("market_hash_name") or "").strip()
        if (
            item_classification.classify_market_hash_name(name, conn=conn)
            != item_classification.ITEM_KIND_WEAPON_SKIN
        ):
            continue
        base_price = _finite_float(raw.get("base_price_usd"), -1)
        price_cents = round(base_price * 100)
        try:
            quantity = max(int(raw.get("quantity_on_market") or 0), 0)
        except (TypeError, ValueError):
            quantity = 0
        if not name or not 1 <= price_cents <= max_price:
            continue
        sales_per_day = velocity.get(name)
        key = f"liquid_skin:{name}"
        recommended = bool(
            key in manual
            or (
                quantity >= min_quantity
                and (
                    (sales_per_day is not None and sales_per_day >= minimum_velocity)
                    or (sales_per_day is None and minimum_velocity == 0)
                )
            )
        )
        candidate = {
            "target_key": key,
            "name": name,
            "market_hash_name": name,
            "base_price_cents": price_cents,
            "quantity_on_market": quantity,
            "estimated_sales_per_day": sales_per_day or 0.0,
            "has_schedule_velocity": sales_per_day is not None,
            "catalog_order": order,
            "recommended_candidate": recommended,
        }
        previous = by_name.get(name)
        if previous is None or (quantity, -price_cents) > (
            previous["quantity_on_market"],
            -previous["base_price_cents"],
        ):
            by_name[name] = candidate
    rows = list(by_name.values())
    rows.sort(
        key=lambda row: (
            -int(row["target_key"] in manual),
            -int(row["recommended_candidate"]),
            -int(row["has_schedule_velocity"]),
            -row["estimated_sales_per_day"],
            -row["quantity_on_market"],
            row["base_price_cents"],
            row["catalog_order"],
        )
    )
    return rows


def _exact_catalog_candidates(
    conn: sqlite3.Connection,
    item_kind: str,
    source_type: str,
) -> list[dict[str, Any]]:
    columns = _table_columns(conn, "cs2_item_catalog")
    if not {"market_hash_name", "item_kind"}.issubset(columns):
        return []
    optional = [
        name
        for name in ("weapon_name", "paint_name", "phase_name", "wear_name")
        if name in columns
    ]
    select = ", ".join(["market_hash_name", *optional])
    velocity = _schedule_velocity(conn)
    rows = []
    for order, row in enumerate(
        conn.execute(
            f"SELECT {select} FROM cs2_item_catalog "
            "WHERE item_kind=? AND TRIM(market_hash_name)!='' "
            "ORDER BY market_hash_name COLLATE NOCASE LIMIT ?",
            (item_kind, MAX_LOCAL_CANDIDATES),
        )
    ):
        name = str(row[0] or "").strip()
        candidate = {
            "target_key": f"{source_type}:{name}",
            "name": name,
            "market_hash_name": name,
            "item_kind": item_kind,
            "catalog_order": order,
            "estimated_sales_per_day": velocity.get(name, 0.0),
            "has_schedule_velocity": name in velocity,
            "recommended_candidate": True,
        }
        for index, field in enumerate(optional, start=1):
            candidate[field] = row[index]
        rows.append(candidate)
    rows.sort(
        key=lambda row: (
            -int(row["has_schedule_velocity"]),
            -row["estimated_sales_per_day"],
            row["catalog_order"],
        )
    )
    return rows


def load_profile_candidates(
    conn: sqlite3.Connection,
    profile: str,
    config: Mapping[str, Any],
    target_control: Mapping[str, Any],
    *,
    search: Any = "",
) -> list[dict[str, Any]]:
    if profile == scanner_target_profiles.PROFILE_STICKER_GUNS:
        return _sticker_candidates(
            conn,
            config,
            target_control,
            search=search,
        )
    if profile == scanner_target_profiles.PROFILE_LIQUID_UNDER_10:
        return _liquid_candidates(conn, config, target_control)
    if profile == scanner_target_profiles.PROFILE_KNIVES:
        return _exact_catalog_candidates(conn, "knife", "knife")
    if profile == scanner_target_profiles.PROFILE_GLOVES:
        return _exact_catalog_candidates(conn, "glove", "glove")
    raise ValueError(f"unknown scanner target profile: {profile}")


def _default_readiness(profile: str, config: Mapping[str, Any]) -> dict[str, Any]:
    if profile in {
        scanner_target_profiles.PROFILE_STICKER_GUNS,
        scanner_target_profiles.PROFILE_LIQUID_UNDER_10,
    }:
        ready = bool(config.get("enabled_models"))
        return {
            "scan_ready": ready,
            "blocked_reason": None if ready else "weapon_models_disabled",
        }
    if profile == scanner_target_profiles.PROFILE_GLOVES:
        return {
            "scan_ready": False,
            "blocked_reason": "dedicated_glove_model_unavailable",
        }
    return {
        "scan_ready": None,
        "blocked_reason": "dedicated_knife_model_readiness_not_checked",
    }


def build_preview_with_connection(
    conn: sqlite3.Connection,
    profile: str,
    *,
    config: Mapping[str, Any],
    target_control: Mapping[str, Any],
    search: Any = "",
    limit: Any = 50,
    offset: Any = 0,
    readiness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    target_control = scanner_control.validate_scanner_target_control(target_control)
    budget, pool_limit = _profile_budget(profile, config)
    candidates = load_profile_candidates(
        conn,
        profile,
        config,
        target_control,
        search=search,
    )
    model_state = {**_default_readiness(profile, config), **dict(readiness or {})}
    rotation = profile in {
        scanner_target_profiles.PROFILE_KNIVES,
        scanner_target_profiles.PROFILE_GLOVES,
    }
    payload = scanner_target_profiles.build_profile_preview(
        profile,
        candidates,
        target_control,
        recommended_limit=None if rotation else pool_limit,
        budget=budget,
        search=search,
        limit=limit,
        offset=offset,
        scan_ready=model_state.get("scan_ready"),
        blocked_reason=model_state.get("blocked_reason"),
    )
    payload["rotation"] = {
        "durable": rotation,
        "window_size": pool_limit if rotation else None,
        "eventual_catalog_coverage": rotation,
    }
    payload["local_only"] = True
    return payload


def build_scanner_target_preview(
    db_path: str | Path,
    profile: str,
    *,
    config: Mapping[str, Any] | None = None,
    target_control: Mapping[str, Any] | None = None,
    search: Any = "",
    limit: Any = 50,
    offset: Any = 0,
    readiness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Open SQLite read-only and return one JSON-ready profile preview."""

    config = dict(config or load_preview_config())
    target_control = dict(
        target_control
        or scanner_control.get_scanner_target_control(
            profile_defaults=profile_defaults(config),
            create_if_missing=False,
        )
    )
    path = Path(db_path).expanduser().resolve()
    uri = f"{path.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return build_preview_with_connection(
            conn,
            profile,
            config=config,
            target_control=target_control,
            search=search,
            limit=limit,
            offset=offset,
            readiness=readiness,
        )
