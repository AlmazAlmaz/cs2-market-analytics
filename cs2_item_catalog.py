"""Safe, local CS2 skin catalog synchronization.

The module deliberately performs no work at import time.  Network refreshes are
called by ``parser_final`` only; dashboard request handlers read the published
SQLite tables and therefore never depend on an upstream service.

Primary data is the MIT-licensed ByMykel/CSGO-API ``skins_not_grouped`` feed.
Collection and case metadata can be filled from that project's grouped
``skins`` feed by the stable ``skin_id``/``id`` relation; grouped rows never
create or redefine an exact market variant.
The MIT-licensed somespecialone/cs2-items-schema bundle can optionally be used
as a fallback and cross-check.  Source/license attribution is persisted beside
every successful catalog build.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import portable_os

from item_classification import (  # stable public re-export
    ITEM_KIND_GLOVE,
    ITEM_KIND_KNIFE,
    ITEM_KIND_UNKNOWN,
    ITEM_KIND_WEAPON_SKIN,
    classify_market_hash_name,
    classify_name_fallback,
)

CATALOG_SCHEMA_VERSION = 1
CATALOG_TABLE = "cs2_item_catalog"
CATALOG_VARIANTS_TABLE = "cs2_item_catalog_variants"
CATALOG_META_TABLE = "cs2_item_catalog_meta"

PRIMARY_SOURCE_ID = "bymykel_csgo_api"
PRIMARY_SOURCE_URL = (
    "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/"
    "public/api/en/skins_not_grouped.json"
)
PRIMARY_ATTRIBUTION = {
    "source_id": PRIMARY_SOURCE_ID,
    "project": "ByMykel/CSGO-API",
    "repository_url": "https://github.com/ByMykel/CSGO-API",
    "dataset_url": PRIMARY_SOURCE_URL,
    "license": "MIT",
    "license_url": "https://github.com/ByMykel/CSGO-API/blob/main/LICENSE",
}

ENRICHMENT_SOURCE_ID = "bymykel_csgo_api_grouped"
ENRICHMENT_SOURCE_URL = (
    "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/"
    "public/api/en/skins.json"
)
ENRICHMENT_ATTRIBUTION = {
    "source_id": ENRICHMENT_SOURCE_ID,
    "project": "ByMykel/CSGO-API",
    "repository_url": "https://github.com/ByMykel/CSGO-API",
    "dataset_url": ENRICHMENT_SOURCE_URL,
    "license": "MIT",
    "license_url": "https://github.com/ByMykel/CSGO-API/blob/main/LICENSE",
    "purpose": "collection and case metadata joined by skins_not_grouped.skin_id=skins.id",
}
ENRICHMENT_JOIN_VERSION = 1

FALLBACK_SOURCE_ID = "somespecialone_cs2_items_schema"
FALLBACK_REPOSITORY_URL = "https://github.com/somespecialone/cs2-items-schema"
FALLBACK_BASE_URL = (
    "https://raw.githubusercontent.com/somespecialone/"
    "cs2-items-schema/master/schemas"
)
FALLBACK_SOURCE_URLS = {
    name: f"{FALLBACK_BASE_URL}/{name}.json"
    for name in (
        "items",
        "definitions",
        "paints",
        "phases",
        "wears",
        "containers",
        "types",
    )
}
FALLBACK_ATTRIBUTION = {
    "source_id": FALLBACK_SOURCE_ID,
    "project": "somespecialone/cs2-items-schema",
    "repository_url": FALLBACK_REPOSITORY_URL,
    "dataset_urls": FALLBACK_SOURCE_URLS,
    "license": "MIT",
    "license_url": (
        "https://github.com/somespecialone/cs2-items-schema/blob/master/LICENSE"
    ),
}

DEFAULT_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_TOTAL_TIMEOUT_SECONDS = 45
DEFAULT_MAX_SOURCE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_ENRICHMENT_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_FALLBACK_FILE_BYTES = 8 * 1024 * 1024
DEFAULT_MIN_PRIMARY_ROWS = 1_000
DEFAULT_MIN_ENRICHMENT_ROWS = 1_000
DEFAULT_MAX_CATALOG_ROWS = 100_000
DEFAULT_MAX_SOURCE_VARIANTS = 250_000
DEFAULT_MAX_ENRICHMENT_ROWS = 100_000
DEFAULT_MIN_ENRICHMENT_MATCH_RATIO = 0.90
DEFAULT_MIN_ENRICHMENT_RELATION_RATIO = 0.90
DEFAULT_MIN_RETAINED_TOTAL_RATIO = 0.80
DEFAULT_MIN_RETAINED_KIND_RATIO = 0.80
MAX_CACHE_METADATA_BYTES = 64 * 1024
MAX_MARKET_HASH_NAME_LENGTH = 512
DOWNLOAD_CHUNK_BYTES = 128 * 1024
_PUBLIC_ATTRIBUTION_KEYS = frozenset(
    {
        "source_id",
        "project",
        "repository_url",
        "dataset_url",
        "dataset_urls",
        "license",
        "license_url",
        "purpose",
    }
)

_PHASE_RE = re.compile(
    r"\b(phase\s*[1-4]|ruby|sapphire|black\s+pearl|emerald)\b",
    re.IGNORECASE,
)
_WEAR_SUFFIX_RE = re.compile(r"\s+\(([^()]*)\)$")
_GLOVE_MARKERS = (
    "bloodhound gloves",
    "broken fang gloves",
    "driver gloves",
    "hand wraps",
    "hydra gloves",
    "moto gloves",
    "specialist gloves",
    "sport gloves",
)
_STANDARD_WEARS = (
    ("SFUI_InvTooltip_Wear_Amount_0", "Factory New", 0.00, 0.07),
    ("SFUI_InvTooltip_Wear_Amount_1", "Minimal Wear", 0.07, 0.15),
    ("SFUI_InvTooltip_Wear_Amount_2", "Field-Tested", 0.15, 0.38),
    ("SFUI_InvTooltip_Wear_Amount_3", "Well-Worn", 0.38, 0.45),
    ("SFUI_InvTooltip_Wear_Amount_4", "Battle-Scarred", 0.45, 1.00),
)


class CatalogSyncError(RuntimeError):
    """Base error for a bounded, recoverable catalog refresh failure."""


class CatalogDownloadError(CatalogSyncError):
    """An upstream response violated transport or size constraints."""


class CatalogValidationError(CatalogSyncError):
    """Downloaded JSON did not describe a safe, usable catalog."""


class CatalogEnrichmentValidationError(CatalogValidationError):
    """Grouped metadata could not be joined safely to exact variants."""


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    url: str
    cache_name: str
    attribution: Mapping[str, Any]


@dataclass
class FetchedPayload:
    spec: SourceSpec
    payload: Any
    raw: bytes
    sha256: str
    etag: str | None
    last_modified: str | None
    fetched_at: str
    not_modified: bool = False


@dataclass
class CatalogBuild:
    rows: list[dict[str, Any]]
    variants: list[dict[str, Any]]
    crosscheck: dict[str, Any]


def _utc_now_iso(now: float | None = None) -> str:
    timestamp = time.time() if now is None else float(now)
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _clean_text(value: Any, *, maximum: int = 2_048) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        return None
    if len(result) > maximum or any(ord(char) < 32 for char in result):
        return None
    return result


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0.0 <= result <= 1.0:
        return None
    return result


def _object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _id_name(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, Mapping):
        return _clean_text(value.get("id")), _clean_text(value.get("name"))
    text = _clean_text(value)
    return None, text


def _safe_url(value: Any) -> str | None:
    text = _clean_text(value, maximum=8_192)
    if not text:
        return None
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def _base_name(market_hash_name: str) -> str:
    return _WEAR_SUFFIX_RE.sub("", market_hash_name).strip()


def _phase_fields(entry: Mapping[str, Any], paint_name: str | None) -> tuple[str | None, str | None]:
    phase = entry.get("phase")
    if phase is None:
        phase = entry.get("phase_name")
    phase_id, phase_name = _id_name(phase)
    if phase_name is None and phase_id is not None:
        phase_name = phase_id
    if phase_name is None:
        candidates = (
            paint_name,
            _clean_text(entry.get("name")),
            _clean_text(entry.get("market_hash_name")),
        )
        for candidate in candidates:
            match = _PHASE_RE.search(candidate or "")
            if match:
                phase_name = " ".join(part.capitalize() for part in match.group(1).split())
                break
    return phase_id, phase_name


def _structured_item_kind(entry: Mapping[str, Any], market_hash_name: str) -> str:
    category = _object(entry.get("category"))
    weapon = _object(entry.get("weapon"))
    category_text = " ".join(
        filter(
            None,
            (
                _clean_text(category.get("id")),
                _clean_text(category.get("name")),
                _clean_text(entry.get("item_kind")),
                _clean_text(entry.get("type")),
            ),
        )
    ).casefold()
    weapon_text = " ".join(
        filter(
            None,
            (
                _clean_text(weapon.get("id")),
                _clean_text(weapon.get("name")),
                _clean_text(entry.get("weapon_name")),
            ),
        )
    ).casefold()
    all_text = f"{category_text} {weapon_text} {market_hash_name.casefold()}"

    if "glove" in category_text or any(marker in all_text for marker in _GLOVE_MARKERS):
        return ITEM_KIND_GLOVE
    if (
        "knife" in category_text
        or "knives" in category_text
        or "melee" in category_text
        or "knife" in weapon_text
        or "bayonet" in weapon_text
    ):
        return ITEM_KIND_KNIFE

    fallback = classify_name_fallback(market_hash_name)
    if fallback != ITEM_KIND_UNKNOWN:
        return fallback
    if weapon and (_object(entry.get("pattern")) or entry.get("paint_index") is not None):
        return ITEM_KIND_WEAPON_SKIN
    return ITEM_KIND_UNKNOWN


def _related_values(value: Any) -> tuple[list[str], list[str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return [], []
    ids: list[str] = []
    names: list[str] = []
    for item in value:
        item_id, item_name = _id_name(item)
        if item_id and item_id not in ids:
            ids.append(item_id)
        if item_name and item_name not in names:
            names.append(item_name)
    return ids, names


def _normalize_primary_variant(
    entry: Mapping[str, Any],
    *,
    source_id: str,
) -> dict[str, Any] | None:
    if entry.get("marketable") is False:
        return None
    market_hash_name = _clean_text(
        entry.get("market_hash_name") or entry.get("name"),
        maximum=MAX_MARKET_HASH_NAME_LENGTH,
    )
    if not market_hash_name:
        return None

    item_kind = _structured_item_kind(entry, market_hash_name)
    if item_kind == ITEM_KIND_UNKNOWN:
        return None

    weapon = _object(entry.get("weapon"))
    pattern = _object(entry.get("pattern") or entry.get("paint"))
    wear = _object(entry.get("wear"))
    collections_ids, collection_names = _related_values(entry.get("collections"))
    crate_ids, crate_names = _related_values(entry.get("crates") or entry.get("containers"))
    paint_name = _clean_text(pattern.get("name") or entry.get("paint_name"))
    phase_id, phase_name = _phase_fields(entry, paint_name)
    min_float = _safe_float(entry.get("min_float"))
    max_float = _safe_float(entry.get("max_float"))
    if min_float is not None and max_float is not None and min_float > max_float:
        raise CatalogValidationError(f"invalid float range for {market_hash_name!r}")

    source_variant_id = _clean_text(entry.get("id") or entry.get("skin_id"))
    if not source_variant_id:
        identity = "|".join(
            str(value or "")
            for value in (
                market_hash_name,
                entry.get("paint_index"),
                phase_id,
                phase_name,
            )
        )
        source_variant_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    return {
        "market_hash_name": market_hash_name,
        "item_kind": item_kind,
        "base_name": _base_name(market_hash_name),
        "weapon_id": _clean_text(weapon.get("id") or entry.get("weapon_id")),
        "weapon_name": _clean_text(weapon.get("name") or entry.get("weapon_name")),
        "weapon_def_index": _safe_int(
            weapon.get("weapon_id")
            or weapon.get("def_index")
            or entry.get("weapon_def_index")
            or entry.get("def_index")
        ),
        "paint_id": _clean_text(pattern.get("id") or entry.get("paint_id")),
        "paint_index": _safe_int(entry.get("paint_index") or pattern.get("paint_index")),
        "paint_name": paint_name,
        "phase_id": phase_id,
        "phase_name": phase_name,
        "wear_id": _clean_text(wear.get("id") or entry.get("wear_id")),
        "wear_name": _clean_text(wear.get("name") or entry.get("wear_name")),
        "min_float": min_float,
        "max_float": max_float,
        "stattrak": int(bool(entry.get("stattrak")) or "stattrak" in market_hash_name.casefold()),
        "souvenir": int(bool(entry.get("souvenir")) or market_hash_name.casefold().startswith("souvenir ")),
        "collection_ids": collections_ids,
        "collection_names": collection_names,
        "crate_ids": crate_ids,
        "crate_names": crate_names,
        "image_url": _safe_url(entry.get("image") or entry.get("image_url") or entry.get("icon_url")),
        "source_id": source_id,
        "source_variant_id": source_variant_id,
    }


def _iter_payload_entries(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        yield from (entry for entry in payload if isinstance(entry, Mapping))
        return
    if not isinstance(payload, Mapping):
        return
    for key in ("catalog", "skins", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            yield from (entry for entry in value if isinstance(entry, Mapping))
            return
    if payload and all(isinstance(value, Mapping) for value in payload.values()):
        yield from payload.values()


def _normalize_direct_payload(payload: Any, *, source_id: str) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    for entry in _iter_payload_entries(payload):
        variant = _normalize_primary_variant(entry, source_id=source_id)
        if variant is not None:
            variants.append(variant)
    return variants


def _grouped_enrichment_index(
    payload: Any,
    *,
    min_rows: int,
    max_rows: int,
    min_relation_ratio: float,
) -> tuple[dict[str, dict[str, list[str]]], int, int]:
    """Validate grouped metadata and index it without deriving market variants."""

    index: dict[str, dict[str, list[str]]] = {}
    entry_count = 0
    relationship_rows = 0
    for entry in _iter_payload_entries(payload):
        entry_count += 1
        if entry_count > int(max_rows):
            raise CatalogEnrichmentValidationError(
                f"grouped metadata has more than {max_rows} rows"
            )
        grouped_id = _clean_text(entry.get("id"), maximum=512)
        if not grouped_id:
            continue
        if grouped_id in index:
            raise CatalogEnrichmentValidationError(
                f"grouped metadata contains duplicate id {grouped_id!r}"
            )
        collection_ids, collection_names = _related_values(entry.get("collections"))
        crate_ids, crate_names = _related_values(
            entry.get("crates") or entry.get("containers")
        )
        if collection_ids or collection_names or crate_ids or crate_names:
            relationship_rows += 1
        index[grouped_id] = {
            "collection_ids": collection_ids,
            "collection_names": collection_names,
            "crate_ids": crate_ids,
            "crate_names": crate_names,
        }

    if entry_count < int(min_rows) or len(index) < int(min_rows):
        raise CatalogEnrichmentValidationError(
            "grouped metadata has "
            f"{entry_count} rows and {len(index)} usable ids; expected at least {min_rows}"
        )
    if not 0.0 <= min_relation_ratio <= 1.0:
        raise CatalogEnrichmentValidationError(
            "grouped metadata relation ratio must be between 0 and 1"
        )
    relation_ratio = relationship_rows / len(index) if index else 0.0
    if relation_ratio < min_relation_ratio:
        raise CatalogEnrichmentValidationError(
            "grouped metadata relationship coverage is "
            f"{relation_ratio:.3f}; expected at least {min_relation_ratio:.3f}"
        )
    return index, entry_count, relationship_rows


def _normalize_primary_with_enrichment(
    primary_payload: Any,
    enrichment_payload: Any | None,
    *,
    min_enrichment_rows: int,
    max_enrichment_rows: int,
    min_enrichment_match_ratio: float,
    min_enrichment_relation_ratio: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize exact variants and fill only absent relationship metadata."""

    enrichment_index: dict[str, dict[str, list[str]]] = {}
    enrichment_rows = 0
    enrichment_relationship_rows = 0
    if enrichment_payload is not None:
        (
            enrichment_index,
            enrichment_rows,
            enrichment_relationship_rows,
        ) = _grouped_enrichment_index(
            enrichment_payload,
            min_rows=min_enrichment_rows,
            max_rows=max_enrichment_rows,
            min_relation_ratio=min_enrichment_relation_ratio,
        )

    variants: list[dict[str, Any]] = []
    matched = 0
    collection_fills = 0
    crate_fills = 0
    for entry in _iter_payload_entries(primary_payload):
        variant = _normalize_primary_variant(entry, source_id=PRIMARY_SOURCE_ID)
        if variant is None:
            continue
        join_id = _clean_text(entry.get("skin_id") or entry.get("id"), maximum=512)
        related = enrichment_index.get(join_id or "")
        if related is not None:
            matched += 1
            if (
                not variant["collection_ids"]
                and not variant["collection_names"]
                and (related["collection_ids"] or related["collection_names"])
            ):
                variant["collection_ids"] = list(related["collection_ids"])
                variant["collection_names"] = list(related["collection_names"])
                collection_fills += 1
            if (
                not variant["crate_ids"]
                and not variant["crate_names"]
                and (related["crate_ids"] or related["crate_names"])
            ):
                variant["crate_ids"] = list(related["crate_ids"])
                variant["crate_names"] = list(related["crate_names"])
                crate_fills += 1
        variants.append(variant)

    if not 0.0 <= min_enrichment_match_ratio <= 1.0:
        raise CatalogEnrichmentValidationError(
            "grouped metadata match ratio must be between 0 and 1"
        )
    match_ratio = matched / len(variants) if enrichment_index and variants else 0.0
    if enrichment_index and match_ratio < min_enrichment_match_ratio:
        raise CatalogEnrichmentValidationError(
            "grouped metadata exact-variant match coverage is "
            f"{match_ratio:.3f}; expected at least {min_enrichment_match_ratio:.3f}"
        )

    return variants, {
        "enrichment_rows": enrichment_rows,
        "enrichment_ids": len(enrichment_index),
        "enrichment_relationship_rows": enrichment_relationship_rows,
        "enrichment_matched_variants": matched,
        "enrichment_unmatched_variants": len(variants) - matched if enrichment_index else 0,
        "enrichment_match_ratio": match_ratio,
        "enrichment_collection_fills": collection_fills,
        "enrichment_crate_fills": crate_fills,
    }


def _mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, Mapping) else {}


def _fallback_wears(bundle: Mapping[str, Any], min_float: float, max_float: float):
    result: list[tuple[str, str, float, float]] = []
    wears = _mapping(bundle, "wears")
    for wear_id, raw in wears.items():
        data = _object(raw)
        name = _clean_text(data.get("name") or data.get("value"))
        low_value = data.get("min") if data.get("min") is not None else data.get("wear_min")
        high_value = data.get("max") if data.get("max") is not None else data.get("wear_max")
        low = _safe_float(low_value)
        high = _safe_float(high_value)
        if name and low is not None and high is not None:
            result.append((str(wear_id), name, low, high))
    if not result:
        result = list(_STANDARD_WEARS)
    return [wear for wear in result if wear[2] < max_float and wear[3] > min_float]


def _fallback_item_kind(definition: Mapping[str, Any], types: Mapping[str, Any]) -> str:
    name = _clean_text(definition.get("name")) or ""
    type_value = definition.get("type")
    type_name = _clean_text(types.get(str(type_value))) or _clean_text(type_value) or ""
    folded = f"{name} {type_name}".casefold()
    if "glove" in folded or any(marker in folded for marker in _GLOVE_MARKERS):
        return ITEM_KIND_GLOVE
    if "knife" in folded or "bayonet" in folded or "melee" in folded:
        return ITEM_KIND_KNIFE
    return ITEM_KIND_WEAPON_SKIN if name else ITEM_KIND_UNKNOWN


def _normalize_schema_bundle(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Join somespecialone's indexed schema into conservative market variants."""

    items = _mapping(bundle, "items")
    definitions = _mapping(bundle, "definitions")
    paints = _mapping(bundle, "paints")
    phases = _mapping(bundle, "phases")
    containers = _mapping(bundle, "containers")
    types = _mapping(bundle, "types")
    if not items or not definitions or not paints:
        return []

    variants: list[dict[str, Any]] = []
    for item_key, raw_item in items.items():
        item = _object(raw_item)
        def_index = _clean_text(item.get("def"))
        paint_index_text = _clean_text(item.get("paint"))
        definition = _object(definitions.get(str(def_index)))
        paint = _object(paints.get(str(paint_index_text)))
        if not def_index or not paint_index_text or not definition or not paint:
            continue
        weapon_name = _clean_text(definition.get("name"))
        paint_name = _clean_text(paint.get("name"))
        if not weapon_name or not paint_name:
            continue

        item_kind = _fallback_item_kind(definition, types)
        if item_kind == ITEM_KIND_UNKNOWN:
            continue
        display_weapon = weapon_name
        if item_kind in {ITEM_KIND_KNIFE, ITEM_KIND_GLOVE} and not display_weapon.startswith("★"):
            display_weapon = f"★ {display_weapon}"

        min_float = _safe_float(paint.get("wear_min"))
        max_float = _safe_float(paint.get("wear_max"))
        min_float = 0.0 if min_float is None else min_float
        max_float = 1.0 if max_float is None else max_float
        if min_float > max_float:
            continue

        phase_value = paint.get("phase") or phases.get(str(paint_index_text))
        phase_id, phase_name = _id_name(phase_value)
        if phase_name is None and phase_id is not None:
            phase_name = phase_id
        container_ids = [str(value) for value in item.get("containers", []) if value is not None]
        container_names = [
            name
            for container_id in container_ids
            if (name := _clean_text(_object(containers.get(container_id)).get("name")))
        ]

        for wear_id, wear_name, _wear_low, _wear_high in _fallback_wears(
            bundle, min_float, max_float
        ):
            market_hash_name = f"{display_weapon} | {paint_name} ({wear_name})"
            variants.append(
                {
                    "market_hash_name": market_hash_name,
                    "item_kind": item_kind,
                    "base_name": _base_name(market_hash_name),
                    "weapon_id": None,
                    "weapon_name": weapon_name.lstrip("★ ").strip(),
                    "weapon_def_index": _safe_int(def_index),
                    "paint_id": None,
                    "paint_index": _safe_int(paint_index_text),
                    "paint_name": paint_name,
                    "phase_id": phase_id,
                    "phase_name": phase_name,
                    "wear_id": str(wear_id),
                    "wear_name": wear_name,
                    "min_float": min_float,
                    "max_float": max_float,
                    "stattrak": 0,
                    "souvenir": 0,
                    "collection_ids": [],
                    "collection_names": [],
                    "crate_ids": container_ids,
                    "crate_names": container_names,
                    "image_url": _safe_url(item.get("image")),
                    "source_id": FALLBACK_SOURCE_ID,
                    "source_variant_id": _clean_text(item_key) or f"[{paint_index_text}]{def_index}",
                }
            )
    return variants


def normalize_fallback_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping) and {
        "items",
        "definitions",
        "paints",
    }.issubset(payload):
        return _normalize_schema_bundle(payload)
    return _normalize_direct_payload(payload, source_id=FALLBACK_SOURCE_ID)


def _variant_identity(variant: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        variant.get("market_hash_name"),
        variant.get("paint_index"),
        variant.get("phase_id"),
        variant.get("phase_name"),
        variant.get("source_id"),
        variant.get("source_variant_id"),
    )


def _merge_missing(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    def missing(value: Any) -> bool:
        return value is None or value == "" or value == () or value == []

    for key, value in source.items():
        if key in {"source_id", "source_variant_id"}:
            continue
        if missing(target.get(key)) and not missing(value):
            target[key] = value


def _aggregate_variants(
    variants: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_identities: set[tuple[Any, ...]] = set()
    for raw in variants:
        variant = dict(raw)
        identity = _variant_identity(variant)
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        grouped.setdefault(str(variant["market_hash_name"]), []).append(variant)

    rows: list[dict[str, Any]] = []
    variant_rows: list[dict[str, Any]] = []
    for market_hash_name in sorted(grouped, key=str.casefold):
        entries = grouped[market_hash_name]
        kinds = {entry["item_kind"] for entry in entries}
        if len(kinds) != 1:
            raise CatalogValidationError(
                f"conflicting item kinds for {market_hash_name!r}: {sorted(kinds)}"
            )
        primary = dict(entries[0])
        for entry in entries[1:]:
            _merge_missing(primary, entry)

        def unique_values(values: Sequence[Mapping[str, Any]], key: str) -> list[Any]:
            result: list[Any] = []
            for entry in values:
                value = entry.get(key)
                if value is not None and value not in result:
                    result.append(value)
            return result

        def unique_list_values(
            values: Sequence[Mapping[str, Any]], key: str
        ) -> list[Any]:
            result: list[Any] = []
            for entry in values:
                for value in entry.get(key) or []:
                    if value not in result:
                        result.append(value)
            return result

        paint_ids = unique_values(entries, "paint_id")
        paint_indices = unique_values(entries, "paint_index")
        paint_names = unique_values(entries, "paint_name")
        phase_ids = unique_values(entries, "phase_id")
        phase_names = unique_values(entries, "phase_name")
        collection_ids = unique_list_values(entries, "collection_ids")
        collection_names = unique_list_values(entries, "collection_names")
        crate_ids = unique_list_values(entries, "crate_ids")
        crate_names = unique_list_values(entries, "crate_names")
        source_ids = unique_values(entries, "source_id")
        rows.append(
            {
                "market_hash_name": market_hash_name,
                "item_kind": primary["item_kind"],
                "base_name": primary.get("base_name") or _base_name(market_hash_name),
                "weapon_id": primary.get("weapon_id"),
                "weapon_name": primary.get("weapon_name"),
                "weapon_def_index": primary.get("weapon_def_index"),
                "paint_id": paint_ids[0] if len(paint_ids) == 1 else None,
                "paint_index": paint_indices[0] if len(paint_indices) == 1 else None,
                "paint_name": paint_names[0] if len(paint_names) == 1 else None,
                "paint_indices_json": _json_text(paint_indices),
                "phase_id": phase_ids[0] if len(phase_ids) == 1 else None,
                "phase_name": phase_names[0] if len(phase_names) == 1 else None,
                "phase_ids_json": _json_text(phase_ids),
                "phase_names_json": _json_text(phase_names),
                "wear_id": primary.get("wear_id"),
                "wear_name": primary.get("wear_name"),
                "min_float": primary.get("min_float"),
                "max_float": primary.get("max_float"),
                "stattrak": int(any(entry.get("stattrak") for entry in entries)),
                "souvenir": int(any(entry.get("souvenir") for entry in entries)),
                "collection_ids_json": _json_text(collection_ids),
                "collection_names_json": _json_text(collection_names),
                "crate_ids_json": _json_text(crate_ids),
                "crate_names_json": _json_text(crate_names),
                "image_url": primary.get("image_url"),
                "source_id": ",".join(source_ids),
                "variant_count": len(entries),
            }
        )
        for entry in entries:
            variant_rows.append(
                {
                    "market_hash_name": market_hash_name,
                    "source_variant_id": str(entry["source_variant_id"]),
                    "paint_id": entry.get("paint_id"),
                    "paint_index": entry.get("paint_index"),
                    "paint_name": entry.get("paint_name"),
                    "phase_id": entry.get("phase_id"),
                    "phase_name": entry.get("phase_name"),
                    "image_url": entry.get("image_url"),
                    "source_id": entry.get("source_id"),
                }
            )
    return rows, variant_rows


def build_catalog(
    primary_payload: Any | None,
    *,
    enrichment_payload: Any | None = None,
    fallback_payload: Any | None = None,
    min_primary_rows: int = 1,
    min_enrichment_rows: int = 1,
    min_enrichment_match_ratio: float = DEFAULT_MIN_ENRICHMENT_MATCH_RATIO,
    min_enrichment_relation_ratio: float = DEFAULT_MIN_ENRICHMENT_RELATION_RATIO,
    max_catalog_rows: int = DEFAULT_MAX_CATALOG_ROWS,
    max_source_variants: int = DEFAULT_MAX_SOURCE_VARIANTS,
    max_enrichment_rows: int = DEFAULT_MAX_ENRICHMENT_ROWS,
    required_kinds: Iterable[str] = (
        ITEM_KIND_KNIFE,
        ITEM_KIND_WEAPON_SKIN,
        ITEM_KIND_GLOVE,
    ),
) -> CatalogBuild:
    """Normalize, cross-check and aggregate source payloads without I/O."""

    if primary_payload is not None:
        primary_variants, enrichment_stats = _normalize_primary_with_enrichment(
            primary_payload,
            enrichment_payload,
            min_enrichment_rows=min_enrichment_rows,
            max_enrichment_rows=max_enrichment_rows,
            min_enrichment_match_ratio=min_enrichment_match_ratio,
            min_enrichment_relation_ratio=min_enrichment_relation_ratio,
        )
    else:
        primary_variants = []
        enrichment_stats = {
            "enrichment_rows": 0,
            "enrichment_ids": 0,
            "enrichment_relationship_rows": 0,
            "enrichment_matched_variants": 0,
            "enrichment_unmatched_variants": 0,
            "enrichment_match_ratio": 0.0,
            "enrichment_collection_fills": 0,
            "enrichment_crate_fills": 0,
        }
    fallback_variants = (
        normalize_fallback_payload(fallback_payload)
        if fallback_payload is not None
        else []
    )
    if primary_payload is not None and len(primary_variants) < int(min_primary_rows):
        raise CatalogValidationError(
            f"primary catalog has {len(primary_variants)} variants; expected at least {min_primary_rows}"
        )
    if not primary_variants and not fallback_variants:
        raise CatalogValidationError("no marketable skin variants were found")
    if len(primary_variants) + len(fallback_variants) > int(max_source_variants):
        raise CatalogValidationError(
            "source has too many variants: "
            f"{len(primary_variants) + len(fallback_variants)} > {max_source_variants}"
        )

    primary_by_name = {str(row["market_hash_name"]): row for row in primary_variants}
    fallback_by_name = {str(row["market_hash_name"]): row for row in fallback_variants}
    matched_names = primary_by_name.keys() & fallback_by_name.keys()
    kind_conflicts = sum(
        primary_by_name[name]["item_kind"] != fallback_by_name[name]["item_kind"]
        for name in matched_names
    )
    paint_conflicts = sum(
        primary_by_name[name].get("paint_index") is not None
        and fallback_by_name[name].get("paint_index") is not None
        and primary_by_name[name].get("paint_index") != fallback_by_name[name].get("paint_index")
        for name in matched_names
    )

    # Primary rows are authoritative.  The fallback can fill absent fields and
    # add genuinely missing market names, but never overrides primary identity.
    combined = [dict(row) for row in primary_variants]
    primary_variant_by_identity = {
        (row["market_hash_name"], row.get("paint_index"), row.get("phase_name")): row
        for row in combined
    }
    for fallback in fallback_variants:
        key = (
            fallback["market_hash_name"],
            fallback.get("paint_index"),
            fallback.get("phase_name"),
        )
        existing = primary_variant_by_identity.get(key)
        if existing is not None:
            _merge_missing(existing, fallback)
        else:
            combined.append(dict(fallback))

    rows, variants = _aggregate_variants(combined)
    if len(rows) > int(max_catalog_rows):
        raise CatalogValidationError(
            f"catalog has {len(rows)} rows; limit is {max_catalog_rows}"
        )
    counts = Counter(row["item_kind"] for row in rows)
    missing_kinds = [kind for kind in required_kinds if counts.get(kind, 0) == 0]
    if missing_kinds:
        raise CatalogValidationError(
            "catalog is missing required item kinds: " + ", ".join(missing_kinds)
        )
    return CatalogBuild(
        rows=rows,
        variants=variants,
        crosscheck={
            "primary_variants": len(primary_variants),
            "fallback_variants": len(fallback_variants),
            "matched_market_names": len(matched_names),
            "fallback_only_market_names": len(fallback_by_name.keys() - primary_by_name.keys()),
            "primary_only_market_names": len(primary_by_name.keys() - fallback_by_name.keys()),
            "item_kind_conflicts": kind_conflicts,
            "paint_index_conflicts": paint_conflicts,
            **enrichment_stats,
        },
    )


_CATALOG_COLUMNS_SQL = """
    market_hash_name TEXT PRIMARY KEY,
    item_kind TEXT NOT NULL CHECK (item_kind IN ('knife', 'weapon_skin', 'glove')),
    base_name TEXT NOT NULL,
    weapon_id TEXT,
    weapon_name TEXT,
    weapon_def_index INTEGER,
    paint_id TEXT,
    paint_index INTEGER,
    paint_name TEXT,
    paint_indices_json TEXT NOT NULL,
    phase_id TEXT,
    phase_name TEXT,
    phase_ids_json TEXT NOT NULL,
    phase_names_json TEXT NOT NULL,
    wear_id TEXT,
    wear_name TEXT,
    min_float REAL,
    max_float REAL,
    stattrak INTEGER NOT NULL CHECK (stattrak IN (0, 1)),
    souvenir INTEGER NOT NULL CHECK (souvenir IN (0, 1)),
    collection_ids_json TEXT NOT NULL,
    collection_names_json TEXT NOT NULL,
    crate_ids_json TEXT NOT NULL,
    crate_names_json TEXT NOT NULL,
    image_url TEXT,
    source_id TEXT NOT NULL,
    source_updated_at TEXT NOT NULL,
    catalog_build_id TEXT NOT NULL,
    variant_count INTEGER NOT NULL CHECK (variant_count > 0)
"""

_VARIANT_COLUMNS_SQL = """
    market_hash_name TEXT NOT NULL,
    source_variant_id TEXT NOT NULL,
    paint_id TEXT,
    paint_index INTEGER,
    paint_name TEXT,
    phase_id TEXT,
    phase_name TEXT,
    image_url TEXT,
    source_id TEXT NOT NULL,
    catalog_build_id TEXT NOT NULL,
    PRIMARY KEY (market_hash_name, source_id, source_variant_id)
"""

_CATALOG_INSERT_COLUMNS = (
    "market_hash_name",
    "item_kind",
    "base_name",
    "weapon_id",
    "weapon_name",
    "weapon_def_index",
    "paint_id",
    "paint_index",
    "paint_name",
    "paint_indices_json",
    "phase_id",
    "phase_name",
    "phase_ids_json",
    "phase_names_json",
    "wear_id",
    "wear_name",
    "min_float",
    "max_float",
    "stattrak",
    "souvenir",
    "collection_ids_json",
    "collection_names_json",
    "crate_ids_json",
    "crate_names_json",
    "image_url",
    "source_id",
    "source_updated_at",
    "catalog_build_id",
    "variant_count",
)
_VARIANT_INSERT_COLUMNS = (
    "market_hash_name",
    "source_variant_id",
    "paint_id",
    "paint_index",
    "paint_name",
    "phase_id",
    "phase_name",
    "image_url",
    "source_id",
    "catalog_build_id",
)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
    )


def ensure_catalog_schema(conn: sqlite3.Connection) -> None:
    """Create empty read-side tables without performing any network access."""

    caller_transaction = conn.in_transaction
    conn.execute(f"CREATE TABLE IF NOT EXISTS {CATALOG_TABLE} ({_CATALOG_COLUMNS_SQL})")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {CATALOG_VARIANTS_TABLE} ({_VARIANT_COLUMNS_SQL})"
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CATALOG_META_TABLE} (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    catalog_columns = {
        row[1] for row in conn.execute(f"PRAGMA table_info({CATALOG_TABLE})")
    }
    if {"item_kind", "market_hash_name"}.issubset(catalog_columns):
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_cs2_item_catalog_kind ON {CATALOG_TABLE}(item_kind, market_hash_name)"
        )
    if {"weapon_name", "paint_name", "wear_name"}.issubset(catalog_columns):
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_cs2_item_catalog_weapon ON {CATALOG_TABLE}(weapon_name, paint_name, wear_name)"
        )
    variant_columns = {
        row[1] for row in conn.execute(f"PRAGMA table_info({CATALOG_VARIANTS_TABLE})")
    }
    if {"paint_index", "phase_name"}.issubset(variant_columns):
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_cs2_item_catalog_variants_paint ON {CATALOG_VARIANTS_TABLE}(paint_index, phase_name)"
        )
    now = _utc_now_iso()
    conn.execute(
        f"""
        INSERT INTO {CATALOG_META_TABLE}(key, value, updated_at)
        VALUES ('schema_version', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (str(CATALOG_SCHEMA_VERSION), now),
    )
    if not caller_transaction:
        conn.commit()


def _catalog_meta(conn: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(conn, CATALOG_META_TABLE):
        return {}
    return {
        str(row[0]): str(row[1])
        for row in conn.execute(f"SELECT key, value FROM {CATALOG_META_TABLE}")
    }


def catalog_row_count(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, CATALOG_TABLE):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {CATALOG_TABLE}").fetchone()[0])


def _validate_last_good_size(
    conn: sqlite3.Connection,
    build: CatalogBuild,
    *,
    min_total_ratio: float,
    min_kind_ratio: float,
) -> dict[str, Any]:
    """Reject a plausible-looking upstream truncation before atomic publish."""

    if not 0.0 <= min_total_ratio <= 1.0 or not 0.0 <= min_kind_ratio <= 1.0:
        raise CatalogValidationError("catalog shrink guard ratios must be between 0 and 1")
    if not _table_exists(conn, CATALOG_TABLE):
        return {"previous_rows": 0, "candidate_rows": len(build.rows)}
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({CATALOG_TABLE})")}
    if not {"market_hash_name", "item_kind"}.issubset(columns):
        return {"previous_rows": 0, "candidate_rows": len(build.rows)}

    previous_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            f"SELECT item_kind, COUNT(*) FROM {CATALOG_TABLE} GROUP BY item_kind"
        )
    }
    candidate_counts = Counter(str(row["item_kind"]) for row in build.rows)
    previous_total = sum(previous_counts.values())
    candidate_total = len(build.rows)
    total_floor = math.ceil(previous_total * min_total_ratio)
    violations: list[str] = []
    if previous_total and candidate_total < total_floor:
        violations.append(
            f"total {candidate_total} is below retained floor {total_floor} of {previous_total}"
        )
    for kind, previous_count in sorted(previous_counts.items()):
        kind_floor = math.ceil(previous_count * min_kind_ratio)
        candidate_count = int(candidate_counts.get(kind, 0))
        if previous_count and candidate_count < kind_floor:
            violations.append(
                f"{kind} {candidate_count} is below retained floor "
                f"{kind_floor} of {previous_count}"
            )
    if violations:
        raise CatalogValidationError(
            "candidate catalog shrink guard rejected publication: " + "; ".join(violations)
        )
    return {
        "previous_rows": previous_total,
        "candidate_rows": candidate_total,
        "previous_kind_counts": previous_counts,
        "candidate_kind_counts": dict(sorted(candidate_counts.items())),
        "min_retained_total_ratio": min_total_ratio,
        "min_retained_kind_ratio": min_kind_ratio,
    }


def publish_catalog(
    conn: sqlite3.Connection,
    build: CatalogBuild,
    *,
    build_id: str,
    source_updated_at: str,
    metadata: Mapping[str, Any],
) -> None:
    """Atomically replace both derived catalog relations and their metadata."""

    if conn.in_transaction:
        raise CatalogSyncError("catalog publication requires a clean SQLite transaction")
    next_catalog = f"{CATALOG_TABLE}_next"
    old_catalog = f"{CATALOG_TABLE}_old"
    next_variants = f"{CATALOG_VARIANTS_TABLE}_next"
    old_variants = f"{CATALOG_VARIANTS_TABLE}_old"
    now = _utc_now_iso()
    kind_counts = Counter(row["item_kind"] for row in build.rows)

    conn.execute("BEGIN IMMEDIATE")
    try:
        for table_name in (next_catalog, old_catalog, next_variants, old_variants):
            conn.execute(f"DROP TABLE IF EXISTS {table_name}")
        conn.execute(f"CREATE TABLE {next_catalog} ({_CATALOG_COLUMNS_SQL})")
        conn.execute(f"CREATE TABLE {next_variants} ({_VARIANT_COLUMNS_SQL})")

        catalog_sql = (
            f"INSERT INTO {next_catalog} ({','.join(_CATALOG_INSERT_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in _CATALOG_INSERT_COLUMNS)})"
        )
        catalog_rows = []
        for raw in build.rows:
            row = dict(raw)
            row["source_updated_at"] = source_updated_at
            row["catalog_build_id"] = build_id
            catalog_rows.append(tuple(row.get(column) for column in _CATALOG_INSERT_COLUMNS))
        conn.executemany(catalog_sql, catalog_rows)

        variant_sql = (
            f"INSERT INTO {next_variants} ({','.join(_VARIANT_INSERT_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in _VARIANT_INSERT_COLUMNS)})"
        )
        variant_rows = []
        for raw in build.variants:
            row = dict(raw)
            row["catalog_build_id"] = build_id
            variant_rows.append(tuple(row.get(column) for column in _VARIANT_INSERT_COLUMNS))
        conn.executemany(variant_sql, variant_rows)

        inserted = int(conn.execute(f"SELECT COUNT(*) FROM {next_catalog}").fetchone()[0])
        orphaned = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM {next_variants} v
                LEFT JOIN {next_catalog} c USING (market_hash_name)
                WHERE c.market_hash_name IS NULL
                """
            ).fetchone()[0]
        )
        if inserted != len(build.rows) or inserted <= 0 or orphaned:
            raise CatalogValidationError(
                f"staged catalog validation failed: rows={inserted}, orphaned_variants={orphaned}"
            )

        if _table_exists(conn, CATALOG_TABLE):
            conn.execute(f"ALTER TABLE {CATALOG_TABLE} RENAME TO {old_catalog}")
        if _table_exists(conn, CATALOG_VARIANTS_TABLE):
            conn.execute(f"ALTER TABLE {CATALOG_VARIANTS_TABLE} RENAME TO {old_variants}")
        conn.execute(f"ALTER TABLE {next_catalog} RENAME TO {CATALOG_TABLE}")
        conn.execute(f"ALTER TABLE {next_variants} RENAME TO {CATALOG_VARIANTS_TABLE}")
        conn.execute(f"DROP TABLE IF EXISTS {old_variants}")
        conn.execute(f"DROP TABLE IF EXISTS {old_catalog}")
        conn.execute(
            f"CREATE INDEX idx_cs2_item_catalog_kind ON {CATALOG_TABLE}(item_kind, market_hash_name)"
        )
        conn.execute(
            f"CREATE INDEX idx_cs2_item_catalog_weapon ON {CATALOG_TABLE}(weapon_name, paint_name, wear_name)"
        )
        conn.execute(
            f"CREATE INDEX idx_cs2_item_catalog_variants_paint ON {CATALOG_VARIANTS_TABLE}(paint_index, phase_name)"
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {CATALOG_META_TABLE} (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        stable_meta = {
            "schema_version": str(CATALOG_SCHEMA_VERSION),
            "active_build_id": build_id,
            "refreshed_at": source_updated_at,
            "refreshed_at_epoch": str(int(time.time())),
            "row_count": str(inserted),
            "variant_count": str(len(build.variants)),
            "kind_counts_json": _json_text(dict(sorted(kind_counts.items()))),
            "crosscheck_json": _json_text(build.crosscheck),
            **{str(key): str(value) for key, value in metadata.items()},
        }
        conn.execute(f"DELETE FROM {CATALOG_META_TABLE}")
        conn.executemany(
            f"""
            INSERT INTO {CATALOG_META_TABLE}(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            ((key, value, now) for key, value in stable_meta.items()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def load_schedule_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return one parser target per market name from the active local catalog."""

    if not _table_exists(conn, CATALOG_TABLE):
        return []
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({CATALOG_TABLE})")}
    if not {"market_hash_name", "item_kind"}.issubset(columns):
        return []
    paint_expr = "paint_index" if "paint_index" in columns else "NULL"
    return [
        {
            "market_hash_name": str(row[0]),
            "paint_index": row[1],
            "item_kind": str(row[2]),
        }
        for row in conn.execute(
            f"""
            SELECT market_hash_name, {paint_expr}, item_kind
            FROM {CATALOG_TABLE}
            ORDER BY CASE item_kind WHEN 'weapon_skin' THEN 0 WHEN 'knife' THEN 1 ELSE 2 END,
                     market_hash_name COLLATE NOCASE
            """
        )
        if row[0]
    ]


def merge_schedule_items(
    base_items: Sequence[Mapping[str, Any]],
    catalog_items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Append catalog targets without deleting or duplicating legacy targets."""

    result: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    def merge_one(raw: Mapping[str, Any], *, catalog_authoritative: bool) -> None:
        name = _clean_text(raw.get("market_hash_name"), maximum=MAX_MARKET_HASH_NAME_LENGTH)
        if not name:
            return
        if name in index:
            target = result[index[name]]
            if catalog_authoritative:
                # A NULL catalog paint index is intentional for market hashes
                # shared by several Doppler phases.  Keeping a legacy single
                # paint index here would silently schedule only one phase.
                target["paint_index"] = raw.get("paint_index")
            elif target.get("paint_index") is None and raw.get("paint_index") is not None:
                target["paint_index"] = raw.get("paint_index")
            if catalog_authoritative or target.get("item_kind") is None:
                target["item_kind"] = raw.get("item_kind")
            return
        value = dict(raw)
        value["market_hash_name"] = name
        index[name] = len(result)
        result.append(value)
    for raw in base_items:
        merge_one(raw, catalog_authoritative=False)
    for raw in catalog_items:
        merge_one(raw, catalog_authoritative=True)
    return result


def _cache_paths(cache_dir: str | Path, spec: SourceSpec) -> tuple[Path, Path]:
    root = Path(cache_dir)
    return root / f"{spec.cache_name}.json", root / f"{spec.cache_name}.meta.json"


def _read_cache_metadata(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_CACHE_METADATA_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_verified_cache(payload_path: Path, metadata: Mapping[str, Any]) -> bytes:
    expected = _clean_text(metadata.get("sha256"), maximum=64)
    if not expected or not payload_path.is_file():
        raise CatalogDownloadError("conditional response has no verified local payload")
    raw = payload_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise CatalogDownloadError("cached payload checksum mismatch")
    return raw


def _header_value(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    try:
        return _clean_text(headers.get(name), maximum=2_048)
    except (AttributeError, TypeError):
        return None


def _response_status(response: Any) -> int:
    value = getattr(response, "status", None)
    if value is None and hasattr(response, "getcode"):
        value = response.getcode()
    return int(value or 200)


def _validate_source_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise CatalogDownloadError("catalog source must be an unauthenticated HTTPS URL")
    return parsed


def _public_source_url(url: str) -> str:
    """Return a provenance-safe URL with credentials, query and fragment absent."""

    parsed = _validate_source_url(url)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", "", "")
    )


def _public_attribution(value: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist attribution fields and sanitize every URL-valued field."""

    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if key not in _PUBLIC_ATTRIBUTION_KEYS:
            continue
        if key in {"repository_url", "dataset_url", "license_url"}:
            url = _clean_text(raw_value, maximum=8_192)
            if url:
                result[key] = _public_source_url(url)
            continue
        if key == "dataset_urls" and isinstance(raw_value, Mapping):
            urls: dict[str, str] = {}
            for raw_name, raw_url in raw_value.items():
                name = _clean_text(raw_name, maximum=128)
                url = _clean_text(raw_url, maximum=8_192)
                if name and url:
                    urls[name] = _public_source_url(url)
            result[key] = urls
            continue
        text = _clean_text(raw_value, maximum=2_048)
        if text:
            result[key] = text
    return result


def _read_bounded_response(
    response: Any,
    *,
    max_bytes: int,
    deadline: float,
) -> bytes:
    content_length = _header_value(getattr(response, "headers", None), "Content-Length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise CatalogDownloadError("invalid Content-Length") from exc
        if declared < 0 or declared > max_bytes:
            raise CatalogDownloadError(
                f"catalog response is {declared} bytes; limit is {max_bytes}"
            )
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise CatalogDownloadError("catalog response exceeded its total time limit")
        chunk = response.read(min(DOWNLOAD_CHUNK_BYTES, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise CatalogDownloadError(f"catalog response exceeded {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_json_source(
    spec: SourceSpec,
    *,
    cache_dir: str | Path,
    opener: Callable[..., Any] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    deadline: float | None = None,
) -> FetchedPayload:
    """Conditionally fetch one JSON source, without committing its cache yet."""

    requested = _validate_source_url(spec.url)
    opener = opener or urllib.request.urlopen
    payload_path, metadata_path = _cache_paths(cache_dir, spec)
    cache_metadata = _read_cache_metadata(metadata_path)
    conditional_cache_ok = False
    source_url_sha256 = hashlib.sha256(spec.url.encode("utf-8")).hexdigest()
    cache_url_matches = (
        cache_metadata.get("url_sha256") == source_url_sha256
        or (
            not cache_metadata.get("url_sha256")
            and cache_metadata.get("url") == spec.url
        )
    )
    if (
        payload_path.is_file()
        and cache_metadata.get("sha256")
        and cache_url_matches
    ):
        try:
            conditional_cache_ok = (
                hashlib.sha256(payload_path.read_bytes()).hexdigest()
                == cache_metadata.get("sha256")
            )
        except OSError:
            conditional_cache_ok = False

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "CS2-local-catalog/1.0 (+MIT attribution in SQLite metadata)",
    }
    if conditional_cache_ok:
        etag = _clean_text(cache_metadata.get("etag"), maximum=2_048)
        last_modified = _clean_text(cache_metadata.get("last_modified"), maximum=2_048)
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(spec.url, headers=headers, method="GET")
    effective_deadline = deadline or (time.monotonic() + timeout_seconds)
    remaining = effective_deadline - time.monotonic()
    if remaining <= 0:
        raise CatalogDownloadError("catalog refresh time budget is exhausted")
    request_timeout = max(1, min(int(timeout_seconds), int(remaining + 0.999)))

    try:
        response = opener(request, timeout=request_timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 304:
            raise CatalogDownloadError(f"catalog HTTP status {exc.code}") from exc
        try:
            raw = _read_verified_cache(payload_path, cache_metadata)
            try:
                payload = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError) as parse_exc:
                raise CatalogDownloadError("cached catalog JSON is invalid") from parse_exc
            return FetchedPayload(
                spec=spec,
                payload=payload,
                raw=raw,
                sha256=hashlib.sha256(raw).hexdigest(),
                etag=_clean_text(cache_metadata.get("etag"), maximum=2_048),
                last_modified=_clean_text(cache_metadata.get("last_modified"), maximum=2_048),
                fetched_at=_clean_text(cache_metadata.get("fetched_at")) or _utc_now_iso(),
                not_modified=True,
            )
        finally:
            try:
                exc.close()
            except (AttributeError, OSError):
                pass
    except (OSError, TimeoutError) as exc:
        raise CatalogDownloadError(
            f"catalog request failed ({type(exc).__name__})"
        ) from exc

    try:
        status = _response_status(response)
        if status == 304:
            raw = _read_verified_cache(payload_path, cache_metadata)
            etag = _clean_text(cache_metadata.get("etag"), maximum=2_048)
            last_modified = _clean_text(cache_metadata.get("last_modified"), maximum=2_048)
            not_modified = True
        elif status == 200:
            final_url = spec.url
            if hasattr(response, "geturl"):
                final_url = response.geturl() or spec.url
            final = _validate_source_url(final_url)
            if final.hostname.casefold() != requested.hostname.casefold():
                raise CatalogDownloadError("catalog redirect changed the trusted host")
            raw = _read_bounded_response(
                response,
                max_bytes=int(max_bytes),
                deadline=effective_deadline,
            )
            etag = _header_value(getattr(response, "headers", None), "ETag")
            last_modified = _header_value(
                getattr(response, "headers", None), "Last-Modified"
            )
            not_modified = False
        else:
            raise CatalogDownloadError(f"catalog HTTP status {status}")
    finally:
        try:
            response.close()
        except (AttributeError, OSError):
            pass
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogValidationError("catalog response is not valid JSON") from exc
    return FetchedPayload(
        spec=spec,
        payload=payload,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        etag=etag,
        last_modified=last_modified,
        fetched_at=_utc_now_iso(),
        not_modified=not_modified,
    )


def _fsync_directory(path: Path) -> None:
    portable_os.fsync_directory(path)


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        portable_os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def commit_source_cache(fetched: FetchedPayload, *, cache_dir: str | Path) -> None:
    """Commit only a payload that has already passed catalog validation."""

    payload_path, metadata_path = _cache_paths(cache_dir, fetched.spec)
    if not fetched.not_modified:
        _atomic_write(payload_path, fetched.raw)
    _atomic_write(
        metadata_path,
        _json_text(
            {
                "source_id": fetched.spec.source_id,
                "url": _public_source_url(fetched.spec.url),
                "url_sha256": hashlib.sha256(
                    fetched.spec.url.encode("utf-8")
                ).hexdigest(),
                "sha256": fetched.sha256,
                "etag": fetched.etag,
                "last_modified": fetched.last_modified,
                "fetched_at": fetched.fetched_at,
                "attribution": _public_attribution(fetched.spec.attribution),
            }
        ).encode("utf-8"),
    )


def _is_refresh_due(
    conn: sqlite3.Connection,
    *,
    now: float,
    refresh_interval_seconds: int,
) -> bool:
    if catalog_row_count(conn) <= 0:
        return True
    meta = _catalog_meta(conn)
    try:
        checked_at = int(meta.get("last_checked_at_epoch") or meta.get("refreshed_at_epoch") or 0)
    except ValueError:
        return True
    return now - checked_at >= max(60, int(refresh_interval_seconds))


def _set_checked_at(
    conn: sqlite3.Connection,
    now: float,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    updated_at = _utc_now_iso(now)
    values = [("last_checked_at_epoch", str(int(now)), updated_at)]
    values.extend(
        (str(key), str(value), updated_at) for key, value in (metadata or {}).items()
    )
    conn.executemany(
        f"""
        INSERT INTO {CATALOG_META_TABLE}(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        values,
    )
    conn.commit()


def _fetch_fallback_bundle(
    *,
    cache_dir: str | Path,
    opener: Callable[..., Any],
    timeout_seconds: int,
    max_bytes: int,
    deadline: float,
    source_urls: Mapping[str, str],
) -> tuple[dict[str, Any], list[FetchedPayload]]:
    bundle: dict[str, Any] = {}
    fetched_values: list[FetchedPayload] = []
    required = {"items", "definitions", "paints"}
    for name, url in source_urls.items():
        spec = SourceSpec(
            source_id=FALLBACK_SOURCE_ID,
            url=url,
            cache_name=f"somespecialone_{name}",
            attribution=FALLBACK_ATTRIBUTION,
        )
        try:
            fetched = fetch_json_source(
                spec,
                cache_dir=cache_dir,
                opener=opener,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
                deadline=deadline,
            )
        except CatalogSyncError:
            if name in required:
                raise
            continue
        bundle[name] = fetched.payload
        fetched_values.append(fetched)
    if not required.issubset(bundle):
        raise CatalogValidationError("fallback schema bundle is incomplete")
    return bundle, fetched_values


def refresh_catalog_if_due(
    conn: sqlite3.Connection,
    *,
    cache_dir: str | Path,
    primary_url: str = PRIMARY_SOURCE_URL,
    enrichment_enabled: bool = False,
    enrichment_url: str = ENRICHMENT_SOURCE_URL,
    fallback_enabled: bool = False,
    fallback_source_urls: Mapping[str, str] = FALLBACK_SOURCE_URLS,
    opener: Callable[..., Any] | None = None,
    now: float | None = None,
    force: bool = False,
    refresh_interval_seconds: int = DEFAULT_REFRESH_INTERVAL_SECONDS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    total_timeout_seconds: int = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    max_enrichment_bytes: int = DEFAULT_MAX_ENRICHMENT_BYTES,
    max_fallback_file_bytes: int = DEFAULT_MAX_FALLBACK_FILE_BYTES,
    min_primary_rows: int = DEFAULT_MIN_PRIMARY_ROWS,
    min_enrichment_rows: int = DEFAULT_MIN_ENRICHMENT_ROWS,
    min_enrichment_match_ratio: float = DEFAULT_MIN_ENRICHMENT_MATCH_RATIO,
    min_enrichment_relation_ratio: float = DEFAULT_MIN_ENRICHMENT_RELATION_RATIO,
    min_retained_total_ratio: float = DEFAULT_MIN_RETAINED_TOTAL_RATIO,
    min_retained_kind_ratio: float = DEFAULT_MIN_RETAINED_KIND_RATIO,
    required_kinds: Iterable[str] = (
        ITEM_KIND_KNIFE,
        ITEM_KIND_WEAPON_SKIN,
        ITEM_KIND_GLOVE,
    ),
) -> dict[str, Any]:
    """Refresh a due catalog; preserve the last good SQLite snapshot on error."""

    ensure_catalog_schema(conn)
    opener = opener or urllib.request.urlopen
    timestamp = time.time() if now is None else float(now)
    if not force and not _is_refresh_due(
        conn,
        now=timestamp,
        refresh_interval_seconds=refresh_interval_seconds,
    ):
        return {"status": "fresh", "row_count": catalog_row_count(conn), "network": False}

    deadline = time.monotonic() + max(1, int(total_timeout_seconds))
    primary_spec = SourceSpec(
        source_id=PRIMARY_SOURCE_ID,
        url=primary_url,
        cache_name="bymykel_skins_not_grouped",
        attribution={**PRIMARY_ATTRIBUTION, "dataset_url": primary_url},
    )
    fetched_primary: FetchedPayload | None = None
    fetched_enrichment: FetchedPayload | None = None
    fetched_fallback: list[FetchedPayload] = []
    fallback_payload: Any | None = None
    primary_error: CatalogSyncError | None = None
    enrichment_error: CatalogSyncError | None = None
    try:
        fetched_primary = fetch_json_source(
            primary_spec,
            cache_dir=cache_dir,
            opener=opener,
            timeout_seconds=timeout_seconds,
            max_bytes=max_source_bytes,
            deadline=deadline,
        )
    except CatalogSyncError as exc:
        primary_error = exc

    enrichment_spec = SourceSpec(
        source_id=ENRICHMENT_SOURCE_ID,
        url=enrichment_url,
        cache_name="bymykel_skins_grouped",
        attribution={**ENRICHMENT_ATTRIBUTION, "dataset_url": enrichment_url},
    )
    if enrichment_enabled and fetched_primary is not None:
        try:
            fetched_enrichment = fetch_json_source(
                enrichment_spec,
                cache_dir=cache_dir,
                opener=opener,
                timeout_seconds=timeout_seconds,
                max_bytes=max_enrichment_bytes,
                deadline=deadline,
            )
        except CatalogSyncError as exc:
            enrichment_error = exc

    existing_rows = catalog_row_count(conn)
    existing_meta = _catalog_meta(conn)
    if fetched_primary is not None and fetched_primary.not_modified and existing_rows > 0:
        enrichment_current = not enrichment_enabled or (
            fetched_enrichment is not None
            and fetched_enrichment.not_modified
            and existing_meta.get("enrichment_payload_sha256")
            == fetched_enrichment.sha256
            and existing_meta.get("enrichment_join_version")
            == str(ENRICHMENT_JOIN_VERSION)
        )
        if enrichment_current:
            public_metadata = {
                "primary_source_url": _public_source_url(primary_url),
                "primary_attribution_json": _json_text(
                    _public_attribution(primary_spec.attribution)
                ),
            }
            if fetched_enrichment is not None:
                public_metadata.update(
                    {
                        "enrichment_source_url": _public_source_url(enrichment_url),
                        "enrichment_attribution_json": _json_text(
                            _public_attribution(enrichment_spec.attribution)
                        ),
                    }
                )
            _set_checked_at(conn, timestamp, metadata=public_metadata)
            cache_warning = None
            try:
                commit_source_cache(fetched_primary, cache_dir=cache_dir)
                if fetched_enrichment is not None:
                    commit_source_cache(fetched_enrichment, cache_dir=cache_dir)
            except OSError as exc:
                cache_warning = str(exc)
            result = {
                "status": "not_modified",
                "row_count": existing_rows,
                "network": True,
                "primary_sha256": fetched_primary.sha256,
            }
            if fetched_enrichment is not None:
                result["enrichment_sha256"] = fetched_enrichment.sha256
            if cache_warning:
                result["cache_warning"] = cache_warning
            return result
        if fetched_enrichment is None:
            return {
                "status": "stale",
                "row_count": existing_rows,
                "network": True,
                "error": str(enrichment_error or "grouped metadata source unavailable"),
            }

    # Enrichment is an explicit publication contract. Do not publish a partial
    # snapshot (or erase last-good relationships) when that source is down.
    if (
        enrichment_enabled
        and fetched_primary is not None
        and fetched_enrichment is None
    ):
        return {
            "status": "stale" if existing_rows else "unavailable",
            "row_count": existing_rows,
            "network": True,
            "error": str(enrichment_error or "grouped metadata source unavailable"),
        }

    if fallback_enabled:
        try:
            fallback_payload, fetched_fallback = _fetch_fallback_bundle(
                cache_dir=cache_dir,
                opener=opener,
                timeout_seconds=timeout_seconds,
                max_bytes=max_fallback_file_bytes,
                deadline=deadline,
                source_urls=fallback_source_urls,
            )
        except CatalogSyncError:
            if fetched_primary is None:
                fallback_payload = None

    if fetched_primary is None and fallback_payload is None:
        status = "stale" if catalog_row_count(conn) else "unavailable"
        return {
            "status": status,
            "row_count": catalog_row_count(conn),
            "network": True,
            "error": str(primary_error or "catalog sources unavailable"),
        }

    try:
        used_primary = fetched_primary is not None
        try:
            build = build_catalog(
                fetched_primary.payload if fetched_primary is not None else None,
                enrichment_payload=(
                    fetched_enrichment.payload
                    if fetched_primary is not None and fetched_enrichment is not None
                    else None
                ),
                fallback_payload=fallback_payload,
                min_primary_rows=min_primary_rows if fetched_primary is not None else 0,
                min_enrichment_rows=min_enrichment_rows,
                min_enrichment_match_ratio=min_enrichment_match_ratio,
                min_enrichment_relation_ratio=min_enrichment_relation_ratio,
                required_kinds=required_kinds,
            )
        except CatalogEnrichmentValidationError:
            raise
        except CatalogValidationError as primary_validation_error:
            if fetched_primary is None or fallback_payload is None:
                raise
            # A syntactically valid but truncated/changed primary schema must
            # not block an explicitly enabled fallback snapshot.
            build = build_catalog(
                None,
                fallback_payload=fallback_payload,
                min_primary_rows=0,
                required_kinds=required_kinds,
            )
            used_primary = False
            build.crosscheck["primary_rejected_error"] = str(primary_validation_error)
        build.crosscheck["shrink_guard"] = _validate_last_good_size(
            conn,
            build,
            min_total_ratio=float(min_retained_total_ratio),
            min_kind_ratio=float(min_retained_kind_ratio),
        )
        source_hashes = [
            fetched.sha256
            for fetched in ([fetched_primary] if fetched_primary else []) + fetched_fallback
        ]
        if used_primary and fetched_enrichment is not None:
            source_hashes.append(fetched_enrichment.sha256)
        build_seed = (
            "\n".join(source_hashes)
            + f"\nenrichment_join_version={ENRICHMENT_JOIN_VERSION}\n"
            + _json_text(build.crosscheck)
        )
        build_id = hashlib.sha256(build_seed.encode("utf-8")).hexdigest()
        refreshed_at = _utc_now_iso(timestamp)
        metadata = {
            "last_checked_at_epoch": str(int(timestamp)),
            "refreshed_at_epoch": str(int(timestamp)),
            "active_source_id": (
                PRIMARY_SOURCE_ID if used_primary else FALLBACK_SOURCE_ID
            ),
            "primary_source_id": PRIMARY_SOURCE_ID,
            "primary_source_url": _public_source_url(primary_url),
            "primary_payload_sha256": fetched_primary.sha256 if fetched_primary else "",
            "primary_etag": (fetched_primary.etag or "") if fetched_primary else "",
            "primary_last_modified": (
                fetched_primary.last_modified or ""
            ) if fetched_primary else "",
            "primary_fetched_at": (
                fetched_primary.fetched_at or ""
            ) if fetched_primary else "",
            "primary_attribution_json": _json_text(
                _public_attribution(primary_spec.attribution)
            ),
        }
        if used_primary and fetched_enrichment is not None:
            metadata.update(
                {
                    "enrichment_source_id": ENRICHMENT_SOURCE_ID,
                    "enrichment_source_url": _public_source_url(enrichment_url),
                    "enrichment_payload_sha256": fetched_enrichment.sha256,
                    "enrichment_etag": fetched_enrichment.etag or "",
                    "enrichment_last_modified": fetched_enrichment.last_modified or "",
                    "enrichment_fetched_at": fetched_enrichment.fetched_at or "",
                    "enrichment_join_version": str(ENRICHMENT_JOIN_VERSION),
                    "enrichment_attribution_json": _json_text(
                        _public_attribution(enrichment_spec.attribution)
                    ),
                }
            )
        elif enrichment_error is not None:
            metadata["enrichment_error"] = str(enrichment_error)
        if fallback_payload is not None:
            metadata.update(
                {
                    "fallback_source_url": FALLBACK_REPOSITORY_URL,
                    "fallback_payload_sha256": hashlib.sha256(
                        "".join(item.sha256 for item in fetched_fallback).encode("ascii")
                    ).hexdigest(),
                    "fallback_attribution_json": _json_text(
                        _public_attribution(FALLBACK_ATTRIBUTION)
                    ),
                }
            )
        publish_catalog(
            conn,
            build,
            build_id=build_id,
            source_updated_at=refreshed_at,
            metadata=metadata,
        )
        cache_warning = None
        try:
            if fetched_primary is not None and used_primary:
                commit_source_cache(fetched_primary, cache_dir=cache_dir)
            if fetched_enrichment is not None and used_primary:
                commit_source_cache(fetched_enrichment, cache_dir=cache_dir)
            for fetched in fetched_fallback:
                commit_source_cache(fetched, cache_dir=cache_dir)
        except OSError as exc:
            # SQLite is already the active, valid snapshot.  A cache write
            # failure only means the next run downloads the payload again.
            cache_warning = str(exc)
        result = {
            "status": "updated",
            "row_count": len(build.rows),
            "variant_count": len(build.variants),
            "network": True,
            "build_id": build_id,
            "crosscheck": build.crosscheck,
        }
        if cache_warning:
            result["cache_warning"] = cache_warning
        return result
    except (CatalogSyncError, OSError, sqlite3.Error) as exc:
        status = "stale" if catalog_row_count(conn) else "invalid"
        return {
            "status": status,
            "row_count": catalog_row_count(conn),
            "network": True,
            "error": str(exc),
        }


__all__ = [
    "CATALOG_META_TABLE",
    "CATALOG_TABLE",
    "CATALOG_VARIANTS_TABLE",
    "ENRICHMENT_ATTRIBUTION",
    "ENRICHMENT_SOURCE_URL",
    "FALLBACK_ATTRIBUTION",
    "FALLBACK_SOURCE_URLS",
    "PRIMARY_ATTRIBUTION",
    "PRIMARY_SOURCE_URL",
    "CatalogBuild",
    "CatalogDownloadError",
    "CatalogEnrichmentValidationError",
    "CatalogSyncError",
    "CatalogValidationError",
    "SourceSpec",
    "build_catalog",
    "catalog_row_count",
    "classify_market_hash_name",
    "commit_source_cache",
    "ensure_catalog_schema",
    "fetch_json_source",
    "load_schedule_items",
    "merge_schedule_items",
    "normalize_fallback_payload",
    "publish_catalog",
    "refresh_catalog_if_due",
]
