"""Строит фичи один раз и кэширует на диск, чтобы каждая последующая итерация
(быстрые эксперименты + финальный прогон) не пересчитывала SQL+pandas заново."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
import sys

# Добавляем папку CS2 в пути поиска
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from item_classification import ITEM_KIND_WEAPON_SKIN, STRICT_ITEM_KIND_POLICY

try:
    from IGORMODEL import data_loading, features
except ImportError:  # Direct execution: cd IGORMODEL && python build_cache.py
    import data_loading
    import features

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_PATH = CACHE_DIR / "features_full.pkl"
CACHE_METADATA_PATH = CACHE_DIR / "features_full.metadata.json"
CACHE_SCHEMA_VERSION = 5


def database_fingerprint(db_path: Path = data_loading.DB_PATH) -> dict:
    """Fingerprint one WAL-aware logical snapshot."""

    with data_loading.open_read_snapshot(db_path) as conn:
        value = data_loading.snapshot_fingerprint(conn, db_path)
        value = data_loading.with_content_fingerprint(
            value, data_loading.stream_source_content_sha256(conn)
        )
    value["source_logical_sha256"] = value.get("logical_sha256")
    value["cache_schema_version"] = CACHE_SCHEMA_VERSION
    value["feature_contract_sha256"] = features.FEATURE_CONTRACT_SHA256
    value["logical_sha256"] = features.contract.canonical_sha256(value)
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_cache_metadata() -> dict | None:
    if not CACHE_METADATA_PATH.exists():
        return None
    try:
        value = json.loads(CACHE_METADATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def is_cache_current(db_path: Path = data_loading.DB_PATH) -> bool:
    if not CACHE_PATH.exists():
        return False
    metadata = load_cache_metadata()
    if not metadata:
        return False
    if metadata.get("database_changed_during_build"):
        return False
    current = database_fingerprint(db_path)
    recorded_source = metadata.get("source_database", {})
    recorded_schema = metadata.get(
        "cache_schema_version", recorded_source.get("cache_schema_version")
    )
    recorded_contract = metadata.get(
        "feature_contract_sha256",
        recorded_source.get("feature_contract_sha256"),
    )
    recorded_source_hash = recorded_source.get(
        "source_logical_sha256", recorded_source.get("logical_sha256")
    )
    return (
        recorded_schema == CACHE_SCHEMA_VERSION
        and metadata.get("item_kind") == ITEM_KIND_WEAPON_SKIN
        and recorded_source.get("item_kind") == ITEM_KIND_WEAPON_SKIN
        and recorded_source.get("item_kind_policy") == STRICT_ITEM_KIND_POLICY
        and recorded_contract == features.FEATURE_CONTRACT_SHA256
        and recorded_source_hash == current.get("source_logical_sha256")
    )


def build_and_cache(force: bool = False):
    if not force and is_cache_current():
        print(f"Кэш уже существует: {CACHE_PATH}")
        return CACHE_PATH

    if CACHE_PATH.exists() and not force:
        print(f"Кэш устарел, перестраиваем: {CACHE_PATH}")

    CACHE_DIR.mkdir(exist_ok=True)
    t0 = time.time()
    raw, source_snapshot = data_loading.build_dataset(return_provenance=True)
    print(f"data_loading: {time.time() - t0:.1f}s, {len(raw)} строк")

    t0 = time.time()
    df = features.build_time_safe_features(raw)
    df = df.sort_values(["sold_dt", "listing_id"]).reset_index(drop=True)
    print(f"features: {time.time() - t0:.1f}s")

    temporary_cache = CACHE_PATH.with_suffix(".pkl.tmp")
    df.to_pickle(temporary_cache)
    os.replace(temporary_cache, CACHE_PATH)
    cache_sha256 = file_sha256(CACHE_PATH)
    metadata = {
        "built_at_epoch": time.time(),
        "rows": len(df),
        "item_kind": ITEM_KIND_WEAPON_SKIN,
        "source_database": source_snapshot,
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "feature_contract": features.feature_contract_definition(),
        "feature_contract_sha256": features.FEATURE_CONTRACT_SHA256,
        "cache_sha256": cache_sha256,
        "snapshot_consistency": "single_sqlite_read_transaction",
        "database_changed_during_build": False,
    }
    temporary_metadata = CACHE_METADATA_PATH.with_suffix(".json.tmp")
    temporary_metadata.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary_metadata, CACHE_METADATA_PATH)
    print(f"Кэш сохранён: {CACHE_PATH} ({CACHE_PATH.stat().st_size / 1e6:.1f} MB)")
    return CACHE_PATH


if __name__ == "__main__":
    build_and_cache(force=True)
