"""Validate and atomically promote a complete Igor v2 candidate bundle.

``--check`` is deliberately read-only.  ``--promote`` is deliberately noisy:
it needs an explicit offline acknowledgement, takes an inter-process lock,
builds the complete replacement beside production, and keeps the previous
production directory as one recoverable timestamped backup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catboost import CatBoostRegressor

import app_config
import model_generations
import model_lifecycle
import portable_os
from item_classification import ITEM_KIND_WEAPON_SKIN, STRICT_ITEM_KIND_POLICY

try:
    from . import feature_contract, features
except ImportError:  # Direct execution from the project root or IGORMODEL/.
    import feature_contract
    import features

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = app_config.PROJECT_DIR
DEFAULT_CANDIDATE_DIR = SCRIPT_DIR / "staging" / "igor_candidate"
DEFAULT_PRODUCTION_DIR = SCRIPT_DIR / "artifacts"
DEFAULT_BACKUP_ROOT = SCRIPT_DIR / "backups"

EXPECTED_ARTIFACT_VERSION = 2
MIN_QUALITY_HOLDOUT_ROWS = 1_000
MIN_RAW_INTERVAL_COVERAGE = 0.60
MAX_QUANTILE_CROSSING_RATE = 0.01
MAX_POINT_OUTSIDE_RATE = 0.01
MANIFEST_SCHEMA_VERSION = 1
BUNDLE_FILES = (
    "price_model_point.cbm",
    "price_model_q15.cbm",
    "price_model_q85.cbm",
    "feature_metadata.pkl",
    "igor_metrics.json",
)
MODEL_FILES = {
    "point": "price_model_point.cbm",
    "q15": "price_model_q15.cbm",
    "q85": "price_model_q85.cbm",
}
EXPECTED_LOSSES = {
    "point": ("mae",),
    "q15": ("quantile", "alpha=0.15"),
    "q85": ("quantile", "alpha=0.85"),
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROMOTION_THREAD_LOCK = threading.Lock()


class PromotionError(RuntimeError):
    """Candidate validation or safe promotion failed."""

    classification = "integrity"


class CandidateQualityError(PromotionError):
    """A coherent candidate failed a numerical quality threshold."""

    classification = "quality"


class _PrimitiveOnlyUnpickler(pickle.Unpickler):
    """The v2 metadata contract contains primitives only.

    Refusing global/class reconstruction prevents a staging pickle from
    executing code merely because an operator ran the read-only checker.
    """

    def find_class(self, module: str, name: str):  # pragma: no cover - failure path
        raise pickle.UnpicklingError(
            f"global objects are forbidden in Igor v2 metadata: {module}.{name}"
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_without_resolving_symlinks(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        info = path.stat()
    except OSError as exc:
        raise PromotionError(f"cannot stat bundle file: {path}") from exc
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _require_regular_file(path: Path) -> None:
    if path.is_symlink():
        raise PromotionError(f"bundle file must not be a symlink: {path}")
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise PromotionError(f"candidate bundle is incomplete; missing {path.name}") from exc
    except OSError as exc:
        raise PromotionError(f"cannot inspect bundle file: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise PromotionError(f"bundle file must be a non-empty regular file: {path}")


def _read_metadata(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = _PrimitiveOnlyUnpickler(handle).load()
    except (OSError, EOFError, pickle.PickleError, ValueError, TypeError) as exc:
        raise PromotionError("feature_metadata.pkl is not safe valid v2 metadata") from exc
    if not isinstance(value, dict):
        raise PromotionError("feature_metadata.pkl must contain a dictionary")
    return value


def _read_metrics(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PromotionError("igor_metrics.json is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PromotionError("igor_metrics.json must contain an object")
    return value


def _require_int(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PromotionError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise PromotionError(f"{label} must be at least {minimum}")
    return value


def _require_number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PromotionError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise PromotionError(f"{label} must be a finite number")
    if minimum is not None and result < minimum:
        raise PromotionError(f"{label} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise PromotionError(f"{label} must be at most {maximum}")
    return result


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PromotionError(f"{label} must be a lowercase SHA-256 value")
    return value


def _require_aware_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PromotionError(f"{label} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PromotionError(f"{label} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise PromotionError(f"{label} must include a timezone")
    return value


def _require_matching(
    metadata: dict[str, Any], metrics: dict[str, Any], key: str
) -> Any:
    if key not in metadata or key not in metrics:
        raise PromotionError(f"metadata and metrics must both contain {key}")
    if _canonical_sha256(metadata[key]) != _canonical_sha256(metrics[key]):
        raise PromotionError(f"mixed candidate bundle: {key} does not match")
    return metadata[key]


def _validate_documents(
    metadata: dict[str, Any],
    metrics: dict[str, Any],
    *,
    expected_stage: str,
    expected_promoted: bool,
) -> dict[str, Any]:
    for document_name, document in (("metadata", metadata), ("metrics", metrics)):
        artifact_version = _require_int(
            document.get("artifact_version"),
            f"{document_name}.artifact_version",
        )
        if artifact_version != EXPECTED_ARTIFACT_VERSION:
            raise PromotionError(
                f"unsupported {document_name} artifact_version {artifact_version}; "
                f"expected {EXPECTED_ARTIFACT_VERSION}"
            )
        if document.get("model_name") != "igor":
            raise PromotionError(f"{document_name}.model_name must be 'igor'")
        declared_kind = document.get("item_kind")
        if declared_kind is not None and declared_kind != ITEM_KIND_WEAPON_SKIN:
            raise PromotionError(
                f"{document_name}.item_kind must be {ITEM_KIND_WEAPON_SKIN!r}"
            )
        if expected_stage == "staging" and declared_kind != ITEM_KIND_WEAPON_SKIN:
            raise PromotionError(
                f"new {document_name} must declare item_kind={ITEM_KIND_WEAPON_SKIN!r}"
            )
        if document.get("stage") != expected_stage:
            raise PromotionError(
                f"{document_name}.stage must be {expected_stage!r}"
            )

    promoted_values = (
        metadata.get("production_promoted", False),
        metrics.get("production_promoted"),
    )
    if any(not isinstance(value, bool) for value in promoted_values):
        raise PromotionError("production_promoted must be boolean in metadata and metrics")
    if promoted_values != (expected_promoted, expected_promoted):
        raise PromotionError("metadata and metrics production_promoted state does not match")

    metadata_contract_version = _require_int(
        metadata.get("feature_contract_version"),
        "metadata.feature_contract_version",
    )
    if metadata_contract_version != feature_contract.FEATURE_CONTRACT_VERSION:
        raise PromotionError(
            "candidate feature contract version does not match runtime code"
        )
    expected_contract_hash = features.FEATURE_CONTRACT_SHA256
    actual_contract_hash = _require_matching(
        metadata, metrics, "feature_contract_sha256"
    )
    _require_sha256(actual_contract_hash, "feature_contract_sha256")
    if actual_contract_hash != expected_contract_hash:
        raise PromotionError("candidate feature contract hash does not match runtime code")
    if metadata.get("feature_contract") != features.feature_contract_definition():
        raise PromotionError("embedded feature contract does not match runtime code")

    feature_cols = metadata.get("feature_cols")
    cat_cols = metadata.get("cat_features")
    if feature_cols != list(features.ALL_FEATURES):
        raise PromotionError("candidate feature columns do not match runtime code")
    if cat_cols != list(features.CAT_FEATURES):
        raise PromotionError("candidate categorical columns do not match runtime code")
    if (
        not feature_cols
        or len(feature_cols) != len(set(feature_cols))
        or not all(isinstance(column, str) and column for column in feature_cols)
    ):
        raise PromotionError("candidate feature columns must be unique non-empty strings")
    if not set(cat_cols).issubset(feature_cols):
        raise PromotionError("categorical columns must be a subset of feature columns")

    trained_at = _require_matching(metadata, metrics, "trained_at")
    _require_aware_timestamp(trained_at, "trained_at")
    cache_sha256 = _require_matching(metadata, metrics, "cache_sha256")
    _require_sha256(cache_sha256, "cache_sha256")
    source = _require_matching(metadata, metrics, "source_database")
    if not isinstance(source, dict) or not source:
        raise PromotionError("source_database provenance must be a non-empty object")
    declared_source_kind = source.get("item_kind")
    if declared_source_kind is not None and declared_source_kind != ITEM_KIND_WEAPON_SKIN:
        raise PromotionError("source_database belongs to another item kind")
    if expected_stage == "staging" and declared_source_kind != ITEM_KIND_WEAPON_SKIN:
        raise PromotionError("new Igor source_database must declare weapon_skin")
    declared_policy = source.get("item_kind_policy")
    if declared_policy is not None and declared_policy != STRICT_ITEM_KIND_POLICY:
        raise PromotionError("source_database has an unsupported item-kind policy")
    if expected_stage == "staging" and declared_policy != STRICT_ITEM_KIND_POLICY:
        raise PromotionError("new Igor source_database is not strictly item-kind isolated")
    _require_sha256(source.get("logical_sha256"), "source_database.logical_sha256")
    split = _require_matching(metadata, metrics, "split")
    if not isinstance(split, dict) or not split:
        raise PromotionError("split provenance must be a non-empty object")

    for key in ("rows", "fit_train_rows", "row_exclusions"):
        _require_matching(metadata, metrics, key)
    _require_int(metadata["rows"], "rows", minimum=1)
    _require_int(metadata["fit_train_rows"], "fit_train_rows", minimum=1)

    iterations = metadata.get("model_iterations")
    if not isinstance(iterations, dict) or set(iterations) != set(MODEL_FILES):
        raise PromotionError("metadata.model_iterations must contain point/q15/q85")
    for role in MODEL_FILES:
        _require_int(iterations[role], f"model_iterations.{role}", minimum=1)

    return {
        "artifact_version": EXPECTED_ARTIFACT_VERSION,
        "item_kind": ITEM_KIND_WEAPON_SKIN,
        "feature_contract_version": metadata_contract_version,
        "feature_contract_sha256": actual_contract_hash,
        "trained_at": trained_at,
        "cache_sha256": cache_sha256,
        "source_database_sha256": source["logical_sha256"],
        "split_sha256": _canonical_sha256(split),
        "feature_count": len(feature_cols),
        "categorical_feature_count": len(cat_cols),
    }


def _read_production_metrics(production_dir: str | Path) -> dict[str, Any]:
    path = _path_without_resolving_symlinks(production_dir) / "igor_metrics.json"
    metrics = _read_metrics(path)
    if (
        metrics.get("artifact_version") != EXPECTED_ARTIFACT_VERSION
        or metrics.get("model_name") != "igor"
        or metrics.get("stage") != "production"
        or metrics.get("production_promoted") is not True
    ):
        raise PromotionError("installed Igor production metrics are not a valid v2 baseline")
    _require_aware_timestamp(metrics.get("trained_at"), "production trained_at")
    return metrics


def _validate_legacy_production_item_kind(
    metrics: dict[str, Any],
) -> dict[str, Any] | None:
    """Bridge only absent legacy provenance; reject every explicit conflict."""

    compatibility_fields: list[str] = []
    declared_kind = metrics.get("item_kind")
    if declared_kind is None:
        compatibility_fields.append("production metrics.item_kind")
    elif declared_kind != ITEM_KIND_WEAPON_SKIN:
        raise PromotionError(
            "production metrics.item_kind must be "
            f"{ITEM_KIND_WEAPON_SKIN!r} when declared"
        )

    source = metrics.get("source_database")
    if source is None:
        compatibility_fields.extend(
            (
                "production metrics.source_database.item_kind",
                "production metrics.source_database.item_kind_policy",
            )
        )
    elif not isinstance(source, dict):
        raise PromotionError(
            "production metrics.source_database must be an object when declared"
        )
    else:
        declared_source_kind = source.get("item_kind")
        if declared_source_kind is None:
            compatibility_fields.append(
                "production metrics.source_database.item_kind"
            )
        elif declared_source_kind != ITEM_KIND_WEAPON_SKIN:
            raise PromotionError(
                "production metrics.source_database.item_kind must be "
                f"{ITEM_KIND_WEAPON_SKIN!r} when declared"
            )

        declared_policy = source.get("item_kind_policy")
        if declared_policy is None:
            compatibility_fields.append(
                "production metrics.source_database.item_kind_policy"
            )
        elif declared_policy != STRICT_ITEM_KIND_POLICY:
            raise PromotionError(
                "production metrics.source_database.item_kind_policy must be "
                f"{STRICT_ITEM_KIND_POLICY!r} when declared"
            )

    if not compatibility_fields:
        return None
    fields = sorted(set(compatibility_fields))
    warnings.warn(
        "legacy Igor production item-kind provenance inferred from fixed model "
        f"id for: {', '.join(fields)}",
        RuntimeWarning,
        stacklevel=2,
    )
    return {
        "mode": "inferred_from_fixed_model_id",
        "item_kind": ITEM_KIND_WEAPON_SKIN,
        "fields": fields,
    }


def _read_verified_hot_generation_metrics(
    production_dir: Path,
    pointer: dict[str, Any],
) -> dict[str, Any]:
    """Accept staging flags only when immutable bytes are bound to active.json."""

    try:
        manifest = model_generations.verify_generation(
            "igor",
            production_dir,
            pointer=pointer,
            project_dir=PROJECT_DIR,
        )
    except model_generations.GenerationError as exc:
        raise PromotionError(f"active Igor generation is not trustworthy: {exc}") from exc
    metrics = _read_metrics(production_dir / "igor_metrics.json")
    if (
        metrics.get("artifact_version") != EXPECTED_ARTIFACT_VERSION
        or metrics.get("model_name") != "igor"
        or metrics.get("stage") != "staging"
        or metrics.get("production_promoted") is not False
        or manifest.get("generation_id") != pointer.get("generation_id")
        or manifest.get("job_id") != pointer.get("job_id")
    ):
        raise PromotionError(
            "active Igor generation metrics are not a valid immutable staging baseline"
        )
    _require_aware_timestamp(metrics.get("trained_at"), "active generation trained_at")
    return metrics


def _validate_quality_gate(
    metrics: dict[str, Any], production_metrics: dict[str, Any]
) -> dict[str, Any]:
    comparison = metrics.get("shared_holdout_comparison")
    if not isinstance(comparison, dict):
        raise PromotionError(
            "Igor candidate has no shared_holdout_comparison; refusing unproven promotion"
        )
    if comparison.get("staging_proves_primary_improvement") is not True:
        raise CandidateQualityError(
            "shared holdout does not prove primary improvement"
        )
    holdout_rows = _require_int(
        comparison.get("holdout_rows"),
        "shared_holdout_comparison.holdout_rows",
        minimum=MIN_QUALITY_HOLDOUT_ROWS,
    )
    if holdout_rows != _require_int(
        metrics.get("test_rows"), "metrics.test_rows", minimum=MIN_QUALITY_HOLDOUT_ROWS
    ):
        raise PromotionError("shared holdout row count differs from metrics.test_rows")

    production = comparison.get("production")
    staging = comparison.get("staging")
    deltas = comparison.get("staging_minus_production")
    test = metrics.get("test")
    if not all(isinstance(value, dict) for value in (production, staging, deltas, test)):
        raise PromotionError("shared holdout metrics must be JSON objects")
    primary = ("median_ape_%", "mean_ape_%", "p90_ape_%")
    for name in primary:
        old = _require_number(production.get(name), f"shared production.{name}", minimum=0)
        new = _require_number(staging.get(name), f"shared staging.{name}", minimum=0)
        delta = _require_number(deltas.get(name), f"shared delta.{name}")
        test_value = _require_number(test.get(name), f"metrics.test.{name}", minimum=0)
        if not new < old:
            raise CandidateQualityError(
                f"shared holdout {name} is not strictly better"
            )
        if not math.isclose(delta, new - old, rel_tol=1e-9, abs_tol=1e-12):
            raise PromotionError(f"shared holdout delta for {name} is inconsistent")
        if not math.isclose(test_value, new, rel_tol=1e-9, abs_tol=1e-12):
            raise PromotionError(f"metrics.test.{name} differs from shared staging")

    current_trained_at = production_metrics.get("trained_at")
    if comparison.get("production_trained_at") != current_trained_at:
        raise PromotionError(
            "candidate was not compared with the currently installed Igor model"
        )
    candidate_time = _require_aware_timestamp(metrics.get("trained_at"), "trained_at")
    production_time = _require_aware_timestamp(current_trained_at, "production trained_at")
    if datetime.fromisoformat(candidate_time.replace("Z", "+00:00")) <= datetime.fromisoformat(
        production_time.replace("Z", "+00:00")
    ):
        raise PromotionError("candidate must be newer than the installed Igor model")

    raw_coverage = _require_number(
        metrics.get("test_raw_interval_coverage_15_85"),
        "metrics.test_raw_interval_coverage_15_85",
        minimum=0,
        maximum=1,
    )
    crossing = _require_number(
        metrics.get("test_raw_quantile_crossing_rate"),
        "metrics.test_raw_quantile_crossing_rate",
        minimum=0,
        maximum=1,
    )
    point_outside = _require_number(
        metrics.get("test_point_outside_raw_interval_rate"),
        "metrics.test_point_outside_raw_interval_rate",
        minimum=0,
        maximum=1,
    )
    if raw_coverage < MIN_RAW_INTERVAL_COVERAGE:
        raise CandidateQualityError(
            "metrics.test_raw_interval_coverage_15_85 must be at least "
            f"{MIN_RAW_INTERVAL_COVERAGE}"
        )
    if crossing > MAX_QUANTILE_CROSSING_RATE:
        raise CandidateQualityError(
            "metrics.test_raw_quantile_crossing_rate must be at most "
            f"{MAX_QUANTILE_CROSSING_RATE}"
        )
    if point_outside > MAX_POINT_OUTSIDE_RATE:
        raise CandidateQualityError(
            "metrics.test_point_outside_raw_interval_rate must be at most "
            f"{MAX_POINT_OUTSIDE_RATE}"
        )
    comparison_coverage = _require_number(
        comparison.get("staging_interval_coverage_15_85"),
        "shared_holdout_comparison.staging_interval_coverage_15_85",
        minimum=0,
        maximum=1,
    )
    if not math.isclose(comparison_coverage, raw_coverage, rel_tol=0, abs_tol=1e-12):
        raise PromotionError("shared-holdout coverage differs from raw test coverage")
    return {
        "holdout_rows": holdout_rows,
        "strictly_improved_metrics": list(primary),
        "raw_interval_coverage": raw_coverage,
        "quantile_crossing_rate": crossing,
        "point_outside_raw_interval_rate": point_outside,
        "production_trained_at": current_trained_at,
    }


def _load_catboost_model(path: Path):
    model = CatBoostRegressor()
    model.load_model(str(path))
    return model


def _normalized_loss(model: Any) -> str:
    getter = getattr(model, "get_all_params", None)
    if not callable(getter):
        raise PromotionError("loaded CatBoost model cannot report its parameters")
    try:
        params = getter()
    except Exception as exc:  # CatBoost exposes several backend exception types.
        raise PromotionError("cannot inspect CatBoost model parameters") from exc
    if not isinstance(params, dict):
        raise PromotionError("CatBoost model parameters are invalid")
    value = str(params.get("loss_function") or "")
    return re.sub(r"\s+", "", value).casefold()


def _validate_models(
    candidate_dir: Path,
    metadata: dict[str, Any],
    *,
    model_loader: Callable[[Path], Any],
) -> None:
    expected_features = list(metadata["feature_cols"])
    expected_cat_indices = [
        index
        for index, column in enumerate(expected_features)
        if column in set(metadata["cat_features"])
    ]
    for role, filename in MODEL_FILES.items():
        try:
            model = model_loader(candidate_dir / filename)
        except PromotionError:
            raise
        except Exception as exc:
            raise PromotionError(f"cannot load CatBoost {role} model") from exc

        names = list(getattr(model, "feature_names_", []) or [])
        if names != expected_features:
            raise PromotionError(f"{role} CatBoost feature names do not match metadata")
        tree_count = getattr(model, "tree_count_", None)
        if isinstance(tree_count, bool):
            tree_count = None
        try:
            tree_count = int(tree_count)
        except (TypeError, ValueError) as exc:
            raise PromotionError(f"{role} CatBoost tree count is invalid") from exc
        if tree_count != metadata["model_iterations"][role]:
            raise PromotionError(f"{role} CatBoost tree count does not match metadata")

        cat_index_getter = getattr(model, "get_cat_feature_indices", None)
        if not callable(cat_index_getter):
            raise PromotionError(
                f"{role} CatBoost model cannot report categorical feature indices"
            )
        try:
            actual_cat_indices = list(cat_index_getter())
        except Exception as exc:
            raise PromotionError(
                f"cannot inspect {role} CatBoost categorical features"
            ) from exc
        if actual_cat_indices != expected_cat_indices:
            raise PromotionError(
                f"{role} CatBoost categorical feature indices do not match metadata"
            )

        loss = _normalized_loss(model)
        if not all(marker in loss for marker in EXPECTED_LOSSES[role]):
            raise PromotionError(f"{role} CatBoost loss does not match its bundle role")


def _build_manifest(
    bundle_dir: Path,
    identity: dict[str, Any],
    *,
    stage: str,
    generated_at: str | None = None,
    file_records: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    files = file_records or {}
    if not files:
        for filename in BUNDLE_FILES:
            path = bundle_dir / filename
            _require_regular_file(path)
            files[filename] = {
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "model_name": "igor",
        "stage": stage,
        "generated_at": generated_at or _utc_now(),
        **identity,
        "files": files,
    }
    manifest["bundle_sha256"] = _canonical_sha256(files)
    return manifest


def _validate_bundle(
    candidate_dir: str | Path,
    *,
    expected_stage: str,
    expected_promoted: bool,
    model_loader: Callable[[Path], Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    directory = _path_without_resolving_symlinks(candidate_dir)
    if directory.is_symlink() or not directory.is_dir():
        raise PromotionError(f"candidate directory does not exist: {directory}")
    file_identities = {}
    file_records = {}
    for filename in BUNDLE_FILES:
        path = directory / filename
        _require_regular_file(path)
        file_identities[filename] = _file_identity(path)
        file_records[filename] = {
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
        }

    metadata = _read_metadata(directory / "feature_metadata.pkl")
    metrics = _read_metrics(directory / "igor_metrics.json")
    identity = _validate_documents(
        metadata,
        metrics,
        expected_stage=expected_stage,
        expected_promoted=expected_promoted,
    )
    _validate_models(
        directory,
        metadata,
        model_loader=model_loader or _load_catboost_model,
    )
    for filename, expected_identity in file_identities.items():
        if _file_identity(directory / filename) != expected_identity:
            raise PromotionError(
                f"candidate changed while it was being validated: {filename}"
            )
    manifest = _build_manifest(
        directory,
        identity,
        stage=expected_stage,
        file_records=file_records,
    )
    return metadata, metrics, manifest


def validate_candidate(
    candidate_dir: str | Path = DEFAULT_CANDIDATE_DIR,
    *,
    model_loader: Callable[[Path], Any] | None = None,
    production_dir: str | Path = DEFAULT_PRODUCTION_DIR,
) -> dict[str, Any]:
    """Read and validate one complete staging bundle; never writes to disk."""

    _metadata, metrics, manifest = _validate_bundle(
        candidate_dir,
        expected_stage="staging",
        expected_promoted=False,
        model_loader=model_loader,
    )
    baseline = _path_without_resolving_symlinks(production_dir)
    if baseline == _path_without_resolving_symlinks(DEFAULT_PRODUCTION_DIR):
        baseline = PROJECT_DIR / "IGORMODEL" / "artifacts"
    try:
        baseline = model_generations.active_or_legacy_bundle_dir(
            "igor",
            baseline,
            project_dir=PROJECT_DIR,
            # The hot-generation baseline helper below performs the one full
            # SHA verification tied to this pointer.
            verify_files=False,
        )
        pointer = model_generations.read_active_pointer(
            "igor", project_dir=PROJECT_DIR
        )
    except model_generations.GenerationError as exc:
        raise PromotionError(f"active Igor production generation is unsafe: {exc}") from exc
    production_metrics = (
        _read_verified_hot_generation_metrics(baseline, pointer)
        if pointer is not None
        else _read_production_metrics(baseline)
    )
    production_item_kind_compatibility = _validate_legacy_production_item_kind(
        production_metrics
    )
    manifest["quality_gate"] = _validate_quality_gate(
        metrics, production_metrics
    )
    if production_item_kind_compatibility is not None:
        manifest["production_item_kind_compatibility"] = (
            production_item_kind_compatibility
        )
    try:
        current_pointer = model_generations.read_active_pointer(
            "igor", project_dir=PROJECT_DIR
        )
    except model_generations.GenerationError as exc:
        raise PromotionError(f"active Igor generation became unsafe: {exc}") from exc
    if (current_pointer or {}).get("generation_id") != (pointer or {}).get(
        "generation_id"
    ):
        raise PromotionError("active Igor generation changed during validation")
    return manifest


def find_live_service_processes() -> list[dict[str, Any]]:
    """Return dashboard/deal-scanner Python processes visible to this user."""

    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PromotionError(
            "cannot prove dashboard/deal_scanner are offline; process scan failed"
        ) from exc

    matches: list[dict[str, Any]] = []
    targets = {"dashboard_server.py", "deal_scanner.py"}
    module_targets = {"dashboard_server", "deal_scanner"}
    for raw_line in completed.stdout.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        pid_text, separator, command = stripped.partition(" ")
        if not separator or not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid == os.getpid():
            continue
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        if not tokens:
            continue
        is_python = any(Path(token).name.casefold().startswith("python") for token in tokens[:2])
        script_services = {
            Path(token).stem
            for token in tokens
            if Path(token).name in targets
        }
        module_services = {
            tokens[index + 1]
            for index in range(len(tokens) - 1)
            if tokens[index] == "-m" and tokens[index + 1] in module_targets
        }
        services = script_services | module_services
        if is_python and services:
            matches.append(
                {
                    "pid": pid,
                    "service": min(services),
                    "command": command,
                }
            )
    return matches


def _assert_services_offline() -> None:
    live = find_live_service_processes()
    if live:
        summary = ", ".join(
            f"PID {item['pid']} ({item.get('service', 'target service')})"
            for item in live
        )
        raise PromotionError(
            "refusing Igor promotion while dashboard/deal_scanner are live: " + summary
        )


@contextmanager
def _promotion_lock(lock_path: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError:  # Windows
        import portable_fcntl as fcntl

    if not _PROMOTION_THREAD_LOCK.acquire(blocking=False):
        raise PromotionError("another Igor promotion is already running in this process")
    descriptor: int | None = None
    try:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            if lock_path.is_symlink():
                raise PromotionError(f"refusing symlink promotion lock: {lock_path}")
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(lock_path, flags, 0o600)
            portable_os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PromotionError(
                    "another Igor promotion process holds the lock"
                ) from exc
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
        except PromotionError:
            raise
        except OSError as exc:
            raise PromotionError(
                f"cannot acquire Igor promotion lock: {lock_path}"
            ) from exc
        yield
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        _PROMOTION_THREAD_LOCK.release()


def _canonical_promotion_lock_path() -> Path:
    """Return the only inter-process lock accepted for Igor promotion."""

    return _path_without_resolving_symlinks(
        PROJECT_DIR / ".run" / "igor_model_promotion.lock"
    )


def _fsync_directory(path: Path) -> None:
    portable_os.fsync_directory(path)


def _atomic_pickle(value: dict[str, Any], path: Path) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_json(value: dict[str, Any], path: Path) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    # Windows CRT descriptors default to text mode unless O_BINARY is explicit.
    # Keep every byte (notably CRLF and 0x1A) unchanged so the digest verified
    # here is the same digest produced by the binary candidate validator.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    source_fd = os.open(source, flags)
    try:
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_size <= 0:
            raise PromotionError(f"candidate file changed or is invalid: {source}")
        digest = hashlib.sha256()
        with os.fdopen(os.dup(source_fd), "rb") as input_handle, destination.open(
            "xb"
        ) as output_handle:
            while True:
                chunk = input_handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output_handle.write(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        if digest.hexdigest() != expected_sha256:
            raise PromotionError(
                f"candidate changed after validation; refusing {source.name}"
            )
    finally:
        os.close(source_fd)


def _unique_backup_path(backup_root: Path) -> Path:
    base = backup_root / f"igor_{_timestamp_slug()}"
    if not base.exists():
        return base
    for suffix in range(1, 10_000):
        candidate = backup_root / f"{base.name}_{suffix:03d}"
        if not candidate.exists():
            return candidate
    raise PromotionError("cannot allocate a unique timestamped Igor backup path")


def _same_filesystem(first: Path, second: Path) -> bool:
    return first.stat().st_dev == second.stat().st_dev


def _activate_next_bundle(next_dir: Path, production_dir: Path, backup_dir: Path) -> None:
    """Swap directories and put the original back if activation fails."""

    backup_moved = False
    installed = False
    try:
        os.replace(production_dir, backup_dir)
        backup_moved = True
        _fsync_directory(backup_dir.parent)
        os.replace(next_dir, production_dir)
        installed = True
        _fsync_directory(production_dir.parent)
    except BaseException as activation_error:
        rollback_errors = []
        if installed and production_dir.exists():
            try:
                os.replace(production_dir, next_dir)
            except BaseException as exc:  # noqa: BLE001 - rollback on interrupts too.
                rollback_errors.append(f"cannot move failed new bundle aside: {exc}")
        if backup_moved and backup_dir.exists() and not production_dir.exists():
            try:
                os.replace(backup_dir, production_dir)
                _fsync_directory(production_dir.parent)
            except BaseException as exc:  # noqa: BLE001 - rollback on interrupts too.
                rollback_errors.append(f"cannot restore production backup: {exc}")
        detail = "; ".join(rollback_errors)
        message = f"Igor activation failed and promotion was rolled back: {activation_error}"
        if detail:
            message += f"; manual recovery may be required: {detail}"
        raise PromotionError(message) from activation_error


def _directory_identity(path: Path, label: str) -> dict[str, int]:
    if path.is_symlink():
        raise PromotionError(f"{label} must not be a symlink: {path}")
    try:
        info = path.stat()
    except OSError as exc:
        raise PromotionError(f"cannot inspect {label}: {path}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise PromotionError(f"{label} is not a directory: {path}")
    return {"device": int(info.st_dev), "inode": int(info.st_ino)}


def _require_project_path(
    path: Path, label: str, *, final_may_be_missing: bool = False
) -> Path:
    project = PROJECT_DIR.expanduser().resolve()
    raw = _path_without_resolving_symlinks(path)
    try:
        relative = raw.relative_to(project)
    except ValueError as exc:
        raise PromotionError(f"{label} escapes the configured project") from exc
    cursor = project
    for index, part in enumerate(relative.parts):
        cursor = cursor / part
        if cursor.is_symlink():
            raise PromotionError(f"{label} contains a symlink component: {cursor}")
        is_final = index == len(relative.parts) - 1
        if not cursor.exists():
            if is_final and final_may_be_missing:
                return raw
            raise PromotionError(f"{label} is missing: {cursor}")
        if not cursor.is_dir():
            raise PromotionError(f"{label} component is not a directory: {cursor}")
    return raw


def _validate_igor_journal(
    record: dict[str, Any],
) -> tuple[Path, Path, Path, Path, Path, dict[str, int]]:
    if record.get("kind") != "igor_directory_v1" or record.get("model") != "igor":
        raise PromotionError("promotion journal belongs to another promoter")
    production = _require_project_path(
        _path_without_resolving_symlinks(str(record.get("production_directory") or "")),
        "journal production directory",
        final_may_be_missing=True,
    )
    backup_root = _require_project_path(
        _path_without_resolving_symlinks(str(record.get("backup_root") or "")),
        "journal backup root",
    )
    backup = _require_project_path(
        _path_without_resolving_symlinks(str(record.get("backup_directory") or "")),
        "journal backup directory",
        final_may_be_missing=True,
    )
    next_dir = _require_project_path(
        _path_without_resolving_symlinks(str(record.get("next_directory") or "")),
        "journal prepared directory",
        final_may_be_missing=True,
    )
    recovery_dir = _require_project_path(
        _path_without_resolving_symlinks(str(record.get("recovery_directory") or "")),
        "journal recovery directory",
        final_may_be_missing=True,
    )
    try:
        backup_relative = backup.relative_to(backup_root)
    except ValueError as exc:
        raise PromotionError("journal backup escapes its declared root") from exc
    if len(backup_relative.parts) != 1 or not backup.name.startswith("igor_"):
        raise PromotionError("journal backup path is not canonical")
    if next_dir.parent != production.parent or not next_dir.name.startswith(
        ".igor-next-"
    ):
        raise PromotionError("journal prepared path is not canonical")
    if recovery_dir.parent != production.parent or not recovery_dir.name.startswith(
        ".igor-recovery-"
    ):
        raise PromotionError("journal recovery path is not canonical")
    _require_sha256(
        record.get("candidate_bundle_sha256"),
        "promotion journal candidate_bundle_sha256",
    )
    _require_aware_timestamp(record.get("created_at"), "promotion journal created_at")
    old_identity = record.get("old_directory_identity")
    if (
        not isinstance(old_identity, dict)
        or isinstance(old_identity.get("device"), bool)
        or not isinstance(old_identity.get("device"), int)
        or isinstance(old_identity.get("inode"), bool)
        or not isinstance(old_identity.get("inode"), int)
    ):
        raise PromotionError("promotion journal old directory identity is invalid")
    return production, backup_root, backup, next_dir, recovery_dir, old_identity


def _remove_recovery_directory(path: Path, label: str) -> None:
    if not path.exists():
        if path.is_symlink():
            raise PromotionError(f"refusing dangling {label} symlink: {path}")
        return
    if path.is_symlink() or not path.is_dir():
        raise PromotionError(f"refusing unsafe {label}: {path}")
    shutil.rmtree(path)
    _fsync_directory(path.parent)


def _recover_igor_journal_locked(record: dict[str, Any]) -> dict[str, Any]:
    (
        production,
        _backup_root,
        backup,
        next_dir,
        recovery_dir,
        old_identity,
    ) = _validate_igor_journal(record)

    backup_exists = backup.exists() or backup.is_symlink()
    production_exists = production.exists() or production.is_symlink()
    if backup_exists:
        if _directory_identity(backup, "journal backup directory") != old_identity:
            raise PromotionError("journal backup is not the original production inode")
        if production_exists:
            if recovery_dir.exists() or recovery_dir.is_symlink():
                raise PromotionError("journal recovery directory already exists")
            os.replace(production, recovery_dir)
            _fsync_directory(production.parent)
        os.replace(backup, production)
        _fsync_directory(production.parent)
    elif not production_exists:
        raise PromotionError("journal has neither production nor its backup")

    if _directory_identity(production, "recovered Igor production") != old_identity:
        raise PromotionError("recovered Igor production is not the original inode")
    _remove_recovery_directory(recovery_dir, "failed Igor bundle")
    if next_dir != production:
        _remove_recovery_directory(next_dir, "prepared Igor bundle")
    try:
        model_lifecycle.clear_promotion_journal(
            expected_model="igor",
            expected_backup=str(backup),
            project_dir=PROJECT_DIR,
        )
    except model_lifecycle.ModelLifecycleError as exc:
        raise PromotionError("cannot clear recovered Igor promotion journal") from exc
    return {
        "status": "recovered",
        "model_name": "igor",
        "backup_directory": str(backup),
    }


def recover_interrupted_promotion() -> dict[str, Any] | None:
    """Restore an Igor directory swap interrupted by a process or power loss."""

    try:
        with (
            model_lifecycle.exclusive_model_update_lock(project_dir=PROJECT_DIR),
            _promotion_lock(_canonical_promotion_lock_path()),
        ):
            record = model_lifecycle.read_promotion_journal(
                project_dir=PROJECT_DIR
            )
            if record is None:
                return None
            return _recover_igor_journal_locked(record)
    except model_lifecycle.ModelLifecycleError as exc:
        raise PromotionError(str(exc)) from exc


def _promote_candidate_locked(
    candidate_dir: str | Path = DEFAULT_CANDIDATE_DIR,
    production_dir: str | Path = DEFAULT_PRODUCTION_DIR,
    backup_root: str | Path = DEFAULT_BACKUP_ROOT,
    *,
    offline_confirm: bool = False,
    model_loader: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    """Promote one verified candidate while retaining the complete old bundle."""

    if offline_confirm is not True:
        raise PromotionError(
            "promotion requires --offline-confirm after stopping the site and scanner"
        )

    candidate = _path_without_resolving_symlinks(candidate_dir)
    production = _path_without_resolving_symlinks(production_dir)
    backups = _path_without_resolving_symlinks(backup_root)
    _require_project_path(candidate, "candidate directory")
    _require_project_path(production, "production directory")
    _require_project_path(backups.parent, "backup root parent")
    if candidate.is_symlink():
        raise PromotionError(f"candidate directory must not be a symlink: {candidate}")
    if backups.is_symlink():
        raise PromotionError(f"backup root must not be a symlink: {backups}")
    candidate_real = candidate.resolve()
    production_real = production.resolve()
    backups_real = backups.resolve()
    if (
        candidate_real == production_real
        or production_real in candidate_real.parents
        or candidate_real in production_real.parents
    ):
        raise PromotionError("candidate and production directories must be separate")
    if production.is_symlink() or not production.is_dir():
        raise PromotionError(f"production directory does not exist: {production}")
    if backups_real == production_real or production_real in backups_real.parents:
        raise PromotionError("backup root must not be inside production")

    with _promotion_lock(_canonical_promotion_lock_path()):
        unresolved = model_lifecycle.read_promotion_journal(
            project_dir=PROJECT_DIR
        )
        if unresolved is not None:
            _recover_igor_journal_locked(unresolved)
        _assert_services_offline()
        metadata, metrics, candidate_manifest = _validate_bundle(
            candidate,
            expected_stage="staging",
            expected_promoted=False,
            model_loader=model_loader,
        )
        production_metrics = _read_production_metrics(production)
        production_item_kind_compatibility = (
            _validate_legacy_production_item_kind(production_metrics)
        )
        candidate_manifest["quality_gate"] = _validate_quality_gate(
            metrics, production_metrics
        )
        if production_item_kind_compatibility is not None:
            candidate_manifest["production_item_kind_compatibility"] = (
                production_item_kind_compatibility
            )

        backup_root_created = not backups.exists()
        backups.mkdir(exist_ok=True)
        _require_project_path(backups, "backup root")
        if backup_root_created:
            _fsync_directory(backups.parent)
        if not _same_filesystem(production.parent, backups):
            raise PromotionError(
                "backup root and production must be on the same filesystem"
            )
        backup = _unique_backup_path(backups)
        next_path = Path(
            tempfile.mkdtemp(prefix=".igor-next-", dir=production.parent)
        )
        activated = False
        journal_written = False
        journal_record: dict[str, Any] | None = None
        try:
            for filename in BUNDLE_FILES:
                _copy_verified(
                    candidate / filename,
                    next_path / filename,
                    candidate_manifest["files"][filename]["sha256"],
                )

            promoted_at = _utc_now()
            promotion_record = {
                "promoted_at": promoted_at,
                "backup_directory": str(backup),
                "candidate_bundle_sha256": candidate_manifest["bundle_sha256"],
            }
            promoted_metadata = dict(metadata)
            promoted_metadata.update(
                {
                    "stage": "production",
                    "production_promoted": True,
                    "promotion": promotion_record,
                }
            )
            promoted_metrics = dict(metrics)
            promoted_metrics.update(
                {
                    "stage": "production",
                    "production_promoted": True,
                    "promotion": promotion_record,
                }
            )
            _atomic_pickle(promoted_metadata, next_path / "feature_metadata.pkl")
            _atomic_json(promoted_metrics, next_path / "igor_metrics.json")

            identity = _validate_documents(
                promoted_metadata,
                promoted_metrics,
                expected_stage="production",
                expected_promoted=True,
            )
            production_manifest = _build_manifest(
                next_path,
                identity,
                stage="production",
                generated_at=promoted_at,
            )
            production_manifest["production_promoted"] = True
            production_manifest["promotion"] = promotion_record
            production_manifest["quality_gate"] = candidate_manifest["quality_gate"]
            if production_item_kind_compatibility is not None:
                production_manifest["production_item_kind_compatibility"] = (
                    production_item_kind_compatibility
                )
            # The manifest is the commit marker and is intentionally written last.
            _atomic_json(production_manifest, next_path / "artifact_manifest.json")
            _fsync_directory(next_path)

            # Close the race between initial offline validation and directory swap.
            _assert_services_offline()
            old_identity = _directory_identity(
                production, "Igor production directory"
            )
            recovery_path = production.parent / (
                f".igor-recovery-{candidate_manifest['bundle_sha256'][:16]}-"
                f"{_timestamp_slug()}"
            )
            if recovery_path.exists() or recovery_path.is_symlink():
                raise PromotionError("cannot allocate Igor recovery directory")
            try:
                journal_record = model_lifecycle.write_promotion_journal(
                    {
                        "kind": "igor_directory_v1",
                        "model": "igor",
                        "production_directory": str(production),
                        "backup_root": str(backups),
                        "backup_directory": str(backup),
                        "next_directory": str(next_path),
                        "recovery_directory": str(recovery_path),
                        "old_directory_identity": old_identity,
                        "candidate_bundle_sha256": candidate_manifest[
                            "bundle_sha256"
                        ],
                        "created_at": _utc_now(),
                    },
                    project_dir=PROJECT_DIR,
                )
            except model_lifecycle.ModelLifecycleError as exc:
                raise PromotionError(
                    "cannot create durable Igor promotion journal"
                ) from exc
            journal_written = True
            try:
                _activate_next_bundle(next_path, production, backup)
                installed_sha = _installed_candidate_bundle_sha256(
                    production, model_loader=model_loader
                )
                if installed_sha != candidate_manifest["bundle_sha256"]:
                    raise PromotionError(
                        "installed Igor receipt differs from the candidate"
                    )
                model_lifecycle.clear_promotion_journal(
                    expected_model="igor",
                    expected_backup=str(backup),
                    project_dir=PROJECT_DIR,
                )
                journal_written = False
                activated = True
            except BaseException as promotion_error:
                if journal_written:
                    try:
                        recovery_record = model_lifecycle.read_promotion_journal(
                            project_dir=PROJECT_DIR
                        )
                        if recovery_record is None:
                            if journal_record is None:
                                raise PromotionError(
                                    "in-memory Igor promotion journal is unavailable"
                                )
                            recovery_record = model_lifecycle.write_promotion_journal(
                                {
                                    key: value
                                    for key, value in journal_record.items()
                                    if key != "schema_version"
                                },
                                project_dir=PROJECT_DIR,
                            )
                        _recover_igor_journal_locked(recovery_record)
                        journal_written = False
                    except BaseException as recovery_error:  # noqa: BLE001 - recover on interrupts too.
                        raise PromotionError(
                            "Igor promotion failed and durable recovery could not "
                            f"restore production: {recovery_error}"
                        ) from promotion_error
                raise
        finally:
            if not activated and next_path.exists():
                shutil.rmtree(next_path, ignore_errors=True)

        result = {
            "status": "promoted",
            "production_directory": str(production),
            "backup_directory": str(backup),
            "candidate_bundle_sha256": candidate_manifest["bundle_sha256"],
            "production_bundle_sha256": production_manifest["bundle_sha256"],
            "manifest_path": str(production / "artifact_manifest.json"),
        }
        if production_item_kind_compatibility is not None:
            result["production_item_kind_compatibility"] = (
                production_item_kind_compatibility
            )
        return result


def _installed_candidate_bundle_sha256(
    production_dir: str | Path = DEFAULT_PRODUCTION_DIR,
    *,
    model_loader: Callable[[Path], Any] | None = None,
) -> str | None:
    """Return a verified candidate receipt from the installed Igor bundle."""

    production = _path_without_resolving_symlinks(production_dir)
    manifest_path = production / "artifact_manifest.json"
    if not manifest_path.exists():
        if manifest_path.is_symlink():
            raise PromotionError("installed Igor manifest is a dangling symlink")
        return None
    _require_regular_file(manifest_path)
    manifest_identity = _file_identity(manifest_path)
    metadata, metrics, actual = _validate_bundle(
        production,
        expected_stage="production",
        expected_promoted=True,
        model_loader=model_loader,
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PromotionError("installed Igor manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise PromotionError("installed Igor manifest must contain an object")
    declared_manifest_kind = manifest.get("item_kind")
    if (
        declared_manifest_kind is not None
        and declared_manifest_kind != ITEM_KIND_WEAPON_SKIN
    ):
        raise PromotionError("installed Igor manifest has the wrong item_kind")
    for key in (
        "schema_version",
        "model_name",
        "artifact_version",
        "trained_at",
        "feature_contract_sha256",
        "stage",
        "files",
        "bundle_sha256",
    ):
        if manifest.get(key) != actual.get(key):
            raise PromotionError(f"installed Igor manifest has incoherent {key}")
    if manifest.get("production_promoted") is not True:
        raise PromotionError("installed Igor manifest is not marked promoted")
    promotion = manifest.get("promotion")
    if not isinstance(promotion, dict):
        raise PromotionError("installed Igor manifest has no promotion receipt")
    if promotion != metrics.get("promotion") or promotion != metadata.get("promotion"):
        raise PromotionError(
            "installed Igor bundle promotion receipts are incoherent"
        )
    if manifest_path.is_symlink() or _file_identity(manifest_path) != manifest_identity:
        raise PromotionError("installed Igor manifest changed while inspected")
    return _require_sha256(
        promotion.get("candidate_bundle_sha256"),
        "installed promotion candidate_bundle_sha256",
    )


def installed_candidate_bundle_sha256(
    production_dir: str | Path = DEFAULT_PRODUCTION_DIR,
    *,
    model_loader: Callable[[Path], Any] | None = None,
) -> str | None:
    """Read one coherent Igor receipt under the runtime-wide read barrier."""

    try:
        with model_lifecycle.production_model_read_lock(project_dir=PROJECT_DIR):
            return _installed_candidate_bundle_sha256(
                production_dir, model_loader=model_loader
            )
    except model_lifecycle.ModelLifecycleError as exc:
        raise PromotionError(str(exc)) from exc


def promote_candidate(
    candidate_dir: str | Path = DEFAULT_CANDIDATE_DIR,
    production_dir: str | Path = DEFAULT_PRODUCTION_DIR,
    backup_root: str | Path = DEFAULT_BACKUP_ROOT,
    *,
    offline_confirm: bool = False,
    model_loader: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    """Promote Igor while holding the canonical runtime-wide update barrier."""

    try:
        with model_lifecycle.exclusive_model_update_lock(project_dir=PROJECT_DIR):
            if model_generations.read_active_pointer(
                "igor", project_dir=PROJECT_DIR
            ) is not None:
                raise PromotionError(
                    "offline legacy promotion is disabled while an active hot "
                    "generation exists"
                )
            return _promote_candidate_locked(
                candidate_dir,
                production_dir,
                backup_root,
                offline_confirm=offline_confirm,
                model_loader=model_loader,
            )
    except (model_lifecycle.ModelLifecycleError, model_generations.GenerationError) as exc:
        raise PromotionError(str(exc)) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate or safely promote one complete Igor v2 bundle"
    )
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument(
        "--check", action="store_true", help="read-only candidate validation"
    )
    operation.add_argument(
        "--promote", action="store_true", help="promote the validated candidate"
    )
    parser.add_argument("--candidate-dir", type=Path, default=DEFAULT_CANDIDATE_DIR)
    parser.add_argument("--production-dir", type=Path, default=DEFAULT_PRODUCTION_DIR)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument(
        "--offline-confirm",
        action="store_true",
        help="confirm the site and deal scanner were stopped before promotion",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.check:
            result = validate_candidate(args.candidate_dir)
        else:
            result = promote_candidate(
                args.candidate_dir,
                args.production_dir,
                args.backup_root,
                offline_confirm=args.offline_confirm,
            )
    except PromotionError as exc:
        raise SystemExit(f"Igor bundle error: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
