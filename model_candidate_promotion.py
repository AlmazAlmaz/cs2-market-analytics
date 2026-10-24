"""Fail-closed validation and promotion for isolated model candidates.

The production locations and bundle filenames are deliberately fixed.  A
caller may select another *candidate directory* (useful for an isolated
training run), but cannot redirect production from the CLI.  ``--check`` is
read-only.  ``--promote`` requires the dashboard and deal scanner to be
offline, creates a complete timestamped backup, prepares every replacement
beside production, and activates metadata files last with rollback on error.

Joblib is a pickle-based format.  Almaz candidates must therefore come only
from this project's trusted trainer; merely checking an untrusted joblib file
can execute Python code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import warnings
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib

import app_config
import model_generations
import model_lifecycle
import portable_os
from item_classification import STRICT_ITEM_KIND_POLICY
from NEURALMODEL.features import CAT_FEATURES as NOVA_CAT_FEATURES
from NEURALMODEL.features import FEATURE_CONTRACT_SHA256 as NOVA_FEATURE_CONTRACT_SHA256
from NEURALMODEL.features import (
    FEATURE_CONTRACT_VERSION as NOVA_FEATURE_CONTRACT_VERSION,
)
from NEURALMODEL.features import NUMERIC_FEATURES as NOVA_NUMERIC_FEATURES
from NEURALMODEL.model import NovaPriceNet

PROJECT_DIR = app_config.PROJECT_DIR
MANIFEST_FILENAME = "artifact_manifest.json"
BACKUP_MANIFEST_FILENAME = "backup_manifest.json"
MANIFEST_SCHEMA_VERSION = 1
MIN_INTERVAL_COVERAGE = 0.60
MAX_ALMAZ_QUANTILE_CROSSING_RATE = 0.01
ALMAZ_LIKE_MODELS = frozenset({"almaz", "knives", "gloves"})
ISOLATED_ALMAZ_LIKE_MODELS = frozenset({"knives", "gloves"})
ALMAZ_LIKE_ITEM_KINDS = {
    "almaz": "weapon_skin",
    "knives": "knife",
    "gloves": "glove",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROMOTION_THREAD_LOCK = threading.Lock()


class PromotionError(RuntimeError):
    """The candidate or promotion transaction failed a safety check."""


class CandidateValidationError(PromotionError):
    """A candidate was rejected before publication."""

    classification = "integrity"


class CandidateIntegrityError(CandidateValidationError):
    """Candidate or baseline evidence is missing, stale, or incoherent."""


class CandidateQualityError(CandidateValidationError):
    """Coherent evidence shows that a candidate misses a quality threshold."""

    classification = "quality"


@dataclass(frozen=True)
class ModelProfile:
    name: str
    artifact_version: int
    candidate_dir: Path
    production_dir: Path
    backup_root: Path
    bundle_files: tuple[str, ...]
    model_filename: str
    metadata_filenames: tuple[str, ...]
    metrics_filename: str

    @property
    def activation_order(self) -> tuple[str, ...]:
        return (
            self.model_filename,
            *self.metadata_filenames,
            self.metrics_filename,
            MANIFEST_FILENAME,
        )


MODEL_PROFILES: dict[str, ModelProfile] = {
    "almaz": ModelProfile(
        name="almaz",
        artifact_version=3,
        candidate_dir=PROJECT_DIR / "models" / "staging" / "almaz",
        production_dir=PROJECT_DIR / "models",
        backup_root=PROJECT_DIR / "models" / "backups",
        bundle_files=(
            "csfloat_price_model.joblib",
            "csfloat_price_model_metrics.json",
        ),
        model_filename="csfloat_price_model.joblib",
        metadata_filenames=(),
        metrics_filename="csfloat_price_model_metrics.json",
    ),
    "nova": ModelProfile(
        name="nova",
        artifact_version=2,
        candidate_dir=PROJECT_DIR / "NEURALMODEL" / "staging" / "nova_candidate",
        production_dir=PROJECT_DIR / "NEURALMODEL" / "artifacts",
        backup_root=PROJECT_DIR / "NEURALMODEL" / "backups",
        bundle_files=(
            "nova_price_model.pt",
            "nova_metadata.json",
            "nova_metrics.json",
        ),
        model_filename="nova_price_model.pt",
        metadata_filenames=("nova_metadata.json",),
        metrics_filename="nova_metrics.json",
    ),
    "knives": ModelProfile(
        name="knives",
        artifact_version=3,
        candidate_dir=PROJECT_DIR / "KNIFEMODEL" / "staging" / "knives",
        production_dir=PROJECT_DIR / "KNIFEMODEL" / "artifacts",
        backup_root=PROJECT_DIR / "KNIFEMODEL" / "backups",
        bundle_files=(
            "knife_price_model.joblib",
            "knife_price_model_metrics.json",
        ),
        model_filename="knife_price_model.joblib",
        metadata_filenames=(),
        metrics_filename="knife_price_model_metrics.json",
    ),
    "gloves": ModelProfile(
        name="gloves",
        artifact_version=3,
        candidate_dir=PROJECT_DIR / "GLOVEMODEL" / "staging" / "gloves",
        production_dir=PROJECT_DIR / "GLOVEMODEL" / "artifacts",
        backup_root=PROJECT_DIR / "GLOVEMODEL" / "backups",
        bundle_files=(
            "glove_price_model.joblib",
            "glove_price_model_metrics.json",
        ),
        model_filename="glove_price_model.joblib",
        metadata_filenames=(),
        metrics_filename="glove_price_model_metrics.json",
    ),
}


@dataclass
class _ValidatedCandidate:
    profile: ModelProfile
    candidate_dir: Path
    documents: dict[str, dict[str, Any]]
    model_object: Any
    manifest: dict[str, Any]
    file_identities: dict[str, tuple[int, int, int, int, int]]
    production_identities: dict[str, tuple[int, int, int, int, int]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _profile(model: str) -> ModelProfile:
    try:
        return MODEL_PROFILES[str(model).strip().casefold()]
    except KeyError as exc:
        allowed = ", ".join(repr(name) for name in MODEL_PROFILES)
        raise PromotionError(f"model must be exactly one of {allowed}") from exc


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    # Raw ``os.read`` follows the CRT descriptor mode on Windows.  Without
    # O_BINARY, CRLF and 0x1A bytes in PyTorch/joblib artifacts are translated
    # and the digest no longer represents the immutable on-disk bytes.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PromotionError(
            f"cannot open regular file without following links: {path}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise PromotionError(
                f"bundle file must be a non-empty regular file: {path}"
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        info = path.stat()
    except OSError as exc:
        raise PromotionError(f"cannot stat bundle file: {path}") from exc
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _require_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise PromotionError(f"{label} must not be a symlink: {path}")
    if not path.is_dir():
        raise PromotionError(f"{label} does not exist: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PromotionError(f"cannot resolve {label}: {path}") from exc
    if resolved != path:
        raise PromotionError(
            f"{label} must not contain symlink path components: {path}"
        )


def _require_regular_file(path: Path, *, label: str = "bundle file") -> None:
    if path.is_symlink():
        raise PromotionError(f"{label} must not be a symlink: {path}")
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise PromotionError(
            f"candidate bundle is incomplete; missing {path.name}"
        ) from exc
    except OSError as exc:
        raise PromotionError(f"cannot inspect {label}: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise PromotionError(f"{label} must be a non-empty regular file: {path}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    _require_regular_file(path, label=label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PromotionError(f"{label} must contain a JSON object")
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


def _require_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PromotionError(f"{label} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PromotionError(f"{label} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise PromotionError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PromotionError(f"{label} must be a lowercase SHA-256 value")
    return value


def _same(left: Any, right: Any) -> bool:
    return _canonical_sha256(left) == _canonical_sha256(right)


def _require_same(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    key: str,
    left_label: str,
    right_label: str,
) -> Any:
    if key not in left or key not in right or not _same(left[key], right[key]):
        raise PromotionError(
            f"mixed candidate bundle: {left_label}.{key} and {right_label}.{key} differ"
        )
    return left[key]


def _require_document_identity(
    document: Mapping[str, Any],
    label: str,
    profile: ModelProfile,
    *,
    stage: str,
    promoted: bool,
) -> None:
    version = _require_int(
        document.get("artifact_version"), f"{label}.artifact_version"
    )
    if version != profile.artifact_version:
        raise PromotionError(
            f"{label}.artifact_version must be {profile.artifact_version} for {profile.name}"
        )
    if document.get("model_name") != profile.name:
        raise PromotionError(f"{label}.model_name must be {profile.name!r}")
    if document.get("stage") != stage:
        raise PromotionError(f"{label}.stage must be {stage!r}")
    if document.get("production_promoted") is not promoted:
        raise PromotionError(f"{label}.production_promoted must be {promoted!r}")
    _require_timestamp(document.get("trained_at"), f"{label}.trained_at")


def _require_new_item_kind(
    document: Mapping[str, Any],
    label: str,
    expected: str,
    *,
    source_keys: tuple[str, ...] = (),
) -> None:
    """Require explicit item identity in every newly trained document."""

    if document.get("item_kind") != expected:
        raise CandidateIntegrityError(f"{label}.item_kind must be {expected!r}")
    for key in source_keys:
        source = document.get(key)
        if not isinstance(source, Mapping) or source.get("item_kind") != expected:
            raise CandidateIntegrityError(
                f"{label}.{key}.item_kind must be {expected!r}"
            )
        if source.get("item_kind_policy") != STRICT_ITEM_KIND_POLICY:
            raise CandidateIntegrityError(
                f"{label}.{key}.item_kind_policy must be "
                f"{STRICT_ITEM_KIND_POLICY!r}"
            )


def _validate_legacy_production_item_kind(
    document: Mapping[str, Any],
    label: str,
    expected: str,
    compatibility_fields: list[str],
    *,
    source_keys: tuple[str, ...] = (),
) -> None:
    """Accept only *missing* legacy kind fields; explicit conflicts fail closed."""

    declared = document.get("item_kind")
    if declared is None:
        compatibility_fields.append(f"{label}.item_kind")
    elif declared != expected:
        raise CandidateIntegrityError(
            f"{label}.item_kind must be {expected!r} when declared"
        )
    for key in source_keys:
        source = document.get(key)
        if not isinstance(source, Mapping):
            continue
        declared_source = source.get("item_kind")
        if declared_source is None:
            compatibility_fields.append(f"{label}.{key}.item_kind")
        elif declared_source != expected:
            raise CandidateIntegrityError(
                f"{label}.{key}.item_kind must be {expected!r} when declared"
            )
        declared_policy = source.get("item_kind_policy")
        if declared_policy is None:
            compatibility_fields.append(f"{label}.{key}.item_kind_policy")
        elif declared_policy != STRICT_ITEM_KIND_POLICY:
            raise CandidateIntegrityError(
                f"{label}.{key}.item_kind_policy must be "
                f"{STRICT_ITEM_KIND_POLICY!r} when declared"
            )


def _require_string_list(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) != len(set(value))
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise PromotionError(f"{label} must be unique non-empty strings")
    return value


def _pipeline_estimator(pipeline: Any) -> Any:
    named_steps = getattr(pipeline, "named_steps", None)
    if isinstance(named_steps, Mapping) and named_steps:
        return named_steps.get("model") or list(named_steps.values())[-1]
    steps = getattr(pipeline, "steps", None)
    if isinstance(steps, list) and steps:
        return steps[-1][1]
    return None


def _validate_almaz_pipeline(
    pipeline: Any,
    role: str,
    feature_columns: list[str],
) -> None:
    if pipeline is None or not callable(getattr(pipeline, "predict", None)):
        raise PromotionError(f"Almaz {role} pipeline is missing or cannot predict")
    raw_names = getattr(pipeline, "feature_names_in_", None)
    names = [] if raw_names is None else list(raw_names)
    if names != feature_columns:
        raise PromotionError(
            f"Almaz {role} pipeline feature names differ from metadata"
        )
    estimator = _pipeline_estimator(pipeline)
    if estimator is None:
        raise PromotionError(f"Almaz {role} pipeline has no final estimator")
    loss = str(getattr(estimator, "loss", "")).casefold()
    quantile = getattr(estimator, "quantile", None)
    if role == "point":
        if loss not in {"squared_error", "absolute_error"}:
            raise PromotionError("Almaz point pipeline has an unexpected loss")
    else:
        expected = 0.15 if role == "q15" else 0.85
        if loss != "quantile" or not math.isclose(
            _require_number(quantile, f"Almaz {role} quantile"),
            expected,
            rel_tol=0,
            abs_tol=1e-12,
        ):
            raise PromotionError(f"Almaz {role} pipeline has the wrong quantile")


def _load_almaz_artifact(path: Path) -> dict[str, Any]:
    try:
        value = joblib.load(path)
    except Exception as exc:  # joblib/sklearn expose many format-specific errors.
        raise PromotionError("cannot load the Almaz joblib candidate") from exc
    if not isinstance(value, dict):
        raise PromotionError("Almaz joblib candidate must contain a dictionary")
    return value


def _inspect_nova_model(path: Path, metadata: Mapping[str, Any]) -> dict[str, Any]:
    try:
        import torch

        state = torch.load(path, map_location="cpu", weights_only=True)
        if not isinstance(state, Mapping) or not state:
            raise PromotionError(
                "Nova .pt candidate must contain a non-empty state dict"
            )
        if not all(
            isinstance(key, str) and torch.is_tensor(value)
            for key, value in state.items()
        ):
            raise PromotionError("Nova .pt candidate contains non-tensor state entries")
        model = NovaPriceNet(**metadata["model_config"])
        model.load_state_dict(state, strict=True)
        model.eval()
    except PromotionError:
        raise
    except (
        Exception
    ) as exc:  # torch raises RuntimeError, ValueError and backend errors.
        raise PromotionError(
            "Nova state dict and metadata model_config are incoherent"
        ) from exc
    return {
        "parameters": sum(parameter.numel() for parameter in model.parameters()),
        "state_keys": sorted(state),
    }


def _validate_quality_gate(
    profile: ModelProfile,
    metrics: Mapping[str, Any],
    production_metrics: Mapping[str, Any],
) -> dict[str, Any]:
    comparison = metrics.get("shared_holdout_comparison")
    if not isinstance(comparison, dict):
        raise CandidateIntegrityError(
            f"{profile.name} candidate has no shared_holdout_comparison; refusing unproven promotion"
        )
    improvement_proof = comparison.get("staging_proves_primary_improvement")
    if improvement_proof is False:
        raise CandidateQualityError(
            "shared holdout does not explicitly prove primary improvement"
        )
    if improvement_proof is not True:
        raise CandidateIntegrityError(
            "shared holdout primary-improvement proof is missing or invalid"
        )

    holdout_rows = _require_int(
        comparison.get("holdout_rows"),
        "shared_holdout_comparison.holdout_rows",
        minimum=1,
    )
    test_rows = _require_int(metrics.get("test_rows"), "metrics.test_rows", minimum=1)
    if holdout_rows != test_rows:
        raise CandidateIntegrityError(
            "shared holdout row count differs from metrics.test_rows"
        )
    if holdout_rows < 1_000:
        raise CandidateQualityError("shared holdout must contain at least 1000 rows")

    production = comparison.get("production")
    staging = comparison.get("staging")
    deltas = comparison.get("staging_minus_production")
    test = metrics.get("test")
    if not all(
        isinstance(value, dict) for value in (production, staging, deltas, test)
    ):
        raise CandidateIntegrityError(
            "shared holdout production/staging/delta/test metrics must be objects"
        )

    metric_names = (
        (
            "median_absolute_percentage_error",
            "mean_absolute_percentage_error",
            "p90_absolute_percentage_error",
        )
        if profile.name in ALMAZ_LIKE_MODELS
        else ("median_ape_percent", "mean_ape_percent", "p90_ape_percent")
    )
    for key in metric_names:
        old = _require_number(
            production.get(key), f"shared production.{key}", minimum=0
        )
        new = _require_number(staging.get(key), f"shared staging.{key}", minimum=0)
        delta = _require_number(deltas.get(key), f"shared delta.{key}")
        test_value = _require_number(test.get(key), f"metrics.test.{key}", minimum=0)
        if not new < old:
            raise CandidateQualityError(
                f"shared holdout {key} is not strictly better"
            )
        if not math.isclose(delta, new - old, rel_tol=1e-9, abs_tol=1e-12):
            raise CandidateIntegrityError(
                f"shared holdout delta for {key} is inconsistent"
            )
        if not math.isclose(test_value, new, rel_tol=1e-9, abs_tol=1e-12):
            raise CandidateIntegrityError(
                f"metrics.test.{key} differs from shared staging result"
            )

    production_trained_at = comparison.get("production_trained_at")
    current_trained_at = production_metrics.get("trained_at")
    if production_trained_at != current_trained_at:
        raise CandidateIntegrityError(
            "candidate was not compared with the currently installed production model"
        )
    candidate_time = _require_timestamp(metrics.get("trained_at"), "metrics.trained_at")
    production_time = _require_timestamp(
        current_trained_at, "production metrics.trained_at"
    )
    if candidate_time <= production_time:
        raise CandidateIntegrityError(
            "candidate must be newer than the current production model"
        )

    if profile.name in ALMAZ_LIKE_MODELS:
        coverage = _require_number(
            metrics.get("test_interval_coverage_15_85_log"),
            "metrics.test_interval_coverage_15_85_log",
            minimum=0,
            maximum=1,
        )
        crossing = _require_number(
            metrics.get("test_raw_quantile_crossing_rate"),
            "metrics.test_raw_quantile_crossing_rate",
            minimum=0,
            maximum=1,
        )
    else:
        coverage = _require_number(
            metrics.get("test_interval_coverage_15_85"),
            "metrics.test_interval_coverage_15_85",
            minimum=0,
            maximum=1,
        )
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
        comparison_raw_coverage = _require_number(
            comparison.get("staging_interval_coverage_15_85"),
            "shared_holdout_comparison.staging_interval_coverage_15_85",
            minimum=0,
            maximum=1,
        )
        _require_number(
            comparison.get("production_interval_coverage_15_85"),
            "shared_holdout_comparison.production_interval_coverage_15_85",
            minimum=0,
            maximum=1,
        )
        if not math.isclose(
            comparison_raw_coverage, raw_coverage, rel_tol=0, abs_tol=1e-12
        ):
            raise CandidateIntegrityError(
                "Nova shared-holdout interval coverage differs from raw test coverage"
            )

    if coverage < MIN_INTERVAL_COVERAGE:
        raise CandidateQualityError(
            f"candidate interval coverage must be at least {MIN_INTERVAL_COVERAGE}"
        )
    if profile.name == "nova" and raw_coverage < MIN_INTERVAL_COVERAGE:
        raise CandidateQualityError(
            f"candidate raw interval coverage must be at least {MIN_INTERVAL_COVERAGE}"
        )
    if crossing > MAX_ALMAZ_QUANTILE_CROSSING_RATE:
        raise CandidateQualityError(
            "candidate quantile crossing rate exceeds the allowed maximum"
        )
    if (
        profile.name == "nova"
        and point_outside > MAX_ALMAZ_QUANTILE_CROSSING_RATE
    ):
        raise CandidateQualityError(
            "candidate point-outside-interval rate exceeds the allowed maximum"
        )

    result = {
        "holdout_rows": holdout_rows,
        "strictly_improved_metrics": list(metric_names),
        "interval_coverage": coverage,
        "quantile_crossing_rate": crossing,
        "production_trained_at": current_trained_at,
    }
    if profile.name == "nova":
        result.update(
            {
                "raw_interval_coverage": raw_coverage,
                "point_outside_raw_interval_rate": point_outside,
            }
        )
    return result


def _validate_isolated_bootstrap_quality(
    profile: ModelProfile,
    metrics: Mapping[str, Any],
) -> dict[str, Any]:
    """Absolute gate used only for a first knife/glove generation."""

    item_label = ALMAZ_LIKE_ITEM_KINDS[profile.name]

    train_rows = _require_int(metrics.get("train_rows"), "metrics.train_rows", minimum=1)
    test_rows = _require_int(metrics.get("test_rows"), "metrics.test_rows", minimum=1)
    if train_rows < 5_000:
        raise CandidateQualityError(
            f"{item_label} bootstrap requires at least 5000 train rows"
        )
    if test_rows < 1_000:
        raise CandidateQualityError(
            f"{item_label} bootstrap requires at least 1000 test rows"
        )
    test = metrics.get("test")
    if not isinstance(test, dict):
        raise CandidateIntegrityError(
            f"{item_label} bootstrap test metrics must be an object"
        )
    thresholds = {
        "median_absolute_percentage_error": 0.15,
        "mean_absolute_percentage_error": 0.25,
        "p90_absolute_percentage_error": 0.50,
    }
    observed = {}
    for key, maximum in thresholds.items():
        value = _require_number(test.get(key), f"metrics.test.{key}", minimum=0)
        if value > maximum:
            raise CandidateQualityError(
                f"{item_label} bootstrap {key} exceeds the allowed maximum {maximum}"
            )
        observed[key] = value
    coverage = _require_number(
        metrics.get("test_interval_coverage_15_85_log"),
        "metrics.test_interval_coverage_15_85_log",
        minimum=0,
        maximum=1,
    )
    crossing = _require_number(
        metrics.get("test_raw_quantile_crossing_rate"),
        "metrics.test_raw_quantile_crossing_rate",
        minimum=0,
        maximum=1,
    )
    if coverage < MIN_INTERVAL_COVERAGE:
        raise CandidateQualityError(
            f"{item_label} bootstrap interval coverage must be at least "
            f"{MIN_INTERVAL_COVERAGE}"
        )
    if crossing > MAX_ALMAZ_QUANTILE_CROSSING_RATE:
        raise CandidateQualityError(
            f"{item_label} bootstrap quantile crossing rate exceeds the allowed maximum"
        )
    return {
        "bootstrap": True,
        "holdout_rows": test_rows,
        "train_rows": train_rows,
        "absolute_metric_maxima": thresholds,
        "observed_metrics": observed,
        "interval_coverage": coverage,
        "quantile_crossing_rate": crossing,
        "production_trained_at": None,
    }


def _validate_almaz(
    profile: ModelProfile,
    candidate: Path,
    metrics: dict[str, Any],
    production_metrics: dict[str, Any],
    model_sha256: str,
    *,
    bootstrap: bool = False,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    artifact = _load_almaz_artifact(candidate / profile.model_filename)
    _require_document_identity(
        artifact, "artifact", profile, stage="staging", promoted=False
    )
    _require_document_identity(
        metrics, "metrics", profile, stage="staging", promoted=False
    )
    expected_item_kind = ALMAZ_LIKE_ITEM_KINDS[profile.name]
    _require_new_item_kind(
        artifact,
        "artifact",
        expected_item_kind,
        source_keys=("source_database",),
    )
    _require_new_item_kind(
        metrics,
        "metrics",
        expected_item_kind,
        source_keys=("source_database_before", "source_database_after"),
    )
    trained_at = _require_same(artifact, metrics, "trained_at", "artifact", "metrics")
    _require_same(artifact, metrics, "prediction_calibration", "artifact", "metrics")
    if "source_database" in metrics:
        _require_same(artifact, metrics, "source_database", "artifact", "metrics")
    if not _same(
        artifact.get("source_database"), metrics.get("source_database_before")
    ):
        raise PromotionError(
            "mixed candidate bundle: artifact source_database differs from metrics source_database_before"
        )
    if not _same(artifact.get("split_contract"), metrics.get("split")):
        raise PromotionError(
            "mixed candidate bundle: artifact split_contract differs from metrics.split"
        )
    if metrics.get("database_changed_during_training") is not False:
        raise PromotionError("Almaz source database changed during training")
    if not _same(
        metrics.get("source_database_before"), metrics.get("source_database_after")
    ):
        raise PromotionError("Almaz source database fingerprints before/after differ")
    source = artifact.get("source_database")
    if not isinstance(source, dict):
        raise PromotionError("Almaz source_database must be an object")
    _require_sha(
        source.get("logical_sha256"), "artifact.source_database.logical_sha256"
    )
    rows = _require_int(metrics.get("rows"), "metrics.rows", minimum=1)
    if source.get("valid_price_rows") != rows:
        raise PromotionError(
            "Almaz metrics.rows differs from source database valid_price_rows"
        )
    split = artifact.get("split_contract")
    if not isinstance(split, dict) or not split:
        raise PromotionError("Almaz split contract must be a non-empty object")
    if artifact.get("evaluation_market_stats_cutoff") not in (
        None,
        split.get("holdout_after"),
    ):
        raise PromotionError("Almaz evaluation cutoff differs from holdout boundary")
    if metrics.get("point_only") is not False:
        raise PromotionError("Almaz promotion requires point, q15, and q85 models")

    feature_columns = _require_string_list(
        artifact.get("feature_columns"), "artifact.feature_columns"
    )
    categorical = _require_string_list(
        artifact.get("categorical_columns"), "artifact.categorical_columns"
    )
    numeric = _require_string_list(
        artifact.get("numeric_columns"), "artifact.numeric_columns"
    )
    if categorical + numeric != feature_columns or set(categorical).intersection(
        numeric
    ):
        raise PromotionError("Almaz categorical/numeric feature contract is incoherent")
    if artifact.get("target_mode") != "log_premium":
        raise PromotionError("Almaz target_mode must be 'log_premium'")
    _validate_almaz_pipeline(artifact.get("pipeline"), "point", feature_columns)
    _validate_almaz_pipeline(artifact.get("low_pipeline"), "q15", feature_columns)
    _validate_almaz_pipeline(artifact.get("high_pipeline"), "q85", feature_columns)

    quality = (
        _validate_isolated_bootstrap_quality(profile, metrics)
        if bootstrap
        else _validate_quality_gate(profile, metrics, production_metrics)
    )
    declared_sha = _require_sha(
        metrics.get("model_sha256"), "metrics.model_sha256"
    )
    if declared_sha != model_sha256:
        raise CandidateIntegrityError("Almaz model SHA-256 differs from metrics")
    identity = {
        "artifact_version": profile.artifact_version,
        "model_name": profile.name,
        "item_kind": expected_item_kind,
        "trained_at": trained_at,
        "feature_contract_sha256": _canonical_sha256(
            {
                "feature_columns": feature_columns,
                "categorical_columns": categorical,
                "numeric_columns": numeric,
            }
        ),
        "quality_gate": quality,
    }
    return artifact, {"metrics": metrics}, identity


def _validate_nova_metadata(metadata: Mapping[str, Any]) -> None:
    cats = _require_string_list(metadata.get("cat_features"), "metadata.cat_features")
    nums = _require_string_list(
        metadata.get("numeric_features"), "metadata.numeric_features"
    )
    if cats != list(NOVA_CAT_FEATURES) or nums != list(NOVA_NUMERIC_FEATURES):
        raise PromotionError("Nova feature columns differ from runtime code")
    config = metadata.get("model_config")
    if not isinstance(config, dict):
        raise PromotionError("metadata.model_config must be an object")
    cardinalities = config.get("category_cardinalities")
    dimensions = config.get("embedding_dims")
    if (
        not isinstance(cardinalities, list)
        or len(cardinalities) != len(cats)
        or not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 2
            for value in cardinalities
        )
        or not isinstance(dimensions, list)
        or len(dimensions) != len(cats)
        or not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 1
            for value in dimensions
        )
    ):
        raise PromotionError("Nova embedding configuration is invalid")
    if config.get("numeric_count") != len(nums):
        raise PromotionError("Nova numeric_count differs from numeric feature contract")
    _require_int(config.get("width"), "metadata.model_config.width", minimum=4)
    _require_int(
        config.get("block_count"), "metadata.model_config.block_count", minimum=1
    )
    _require_number(
        config.get("dropout"),
        "metadata.model_config.dropout",
        minimum=0,
        maximum=0.99,
    )

    vocabularies = metadata.get("vocabularies")
    medians = metadata.get("numeric_medians")
    scales = metadata.get("numeric_scales")
    if not isinstance(vocabularies, dict) or set(vocabularies) != set(cats):
        raise PromotionError(
            "Nova vocabularies differ from categorical feature contract"
        )
    if not isinstance(medians, dict) or set(medians) != set(nums):
        raise PromotionError(
            "Nova numeric medians differ from numeric feature contract"
        )
    if not isinstance(scales, dict) or set(scales) != set(nums):
        raise PromotionError("Nova numeric scales differ from numeric feature contract")
    for index, column in enumerate(cats):
        vocabulary = vocabularies[column]
        if not isinstance(vocabulary, dict):
            raise PromotionError(f"Nova vocabulary {column} must be an object")
        values = list(vocabulary.values())
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in values
        ):
            raise PromotionError(f"Nova vocabulary {column} contains invalid indices")
        if values and max(values) >= cardinalities[index]:
            raise PromotionError(
                f"Nova vocabulary {column} exceeds configured cardinality"
            )
    for column in nums:
        _require_number(medians[column], f"metadata.numeric_medians.{column}")
        _require_number(
            metadata["numeric_scales"][column],
            f"metadata.numeric_scales.{column}",
            minimum=1e-12,
        )
    calibration = metadata.get("calibration")
    if not isinstance(calibration, dict) or set(calibration) != {
        "low",
        "center",
        "high",
    }:
        raise PromotionError("Nova calibration must contain exactly low/center/high")
    for role in ("low", "center", "high"):
        _require_number(calibration[role], f"metadata.calibration.{role}")
    _require_number(
        metadata.get("premium_scale"),
        "metadata.premium_scale",
        minimum=1e-12,
    )


def _validate_nova(
    profile: ModelProfile,
    candidate: Path,
    metrics: dict[str, Any],
    production_metrics: dict[str, Any],
    model_sha256: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    metadata = _read_json(candidate / "nova_metadata.json", "nova_metadata.json")
    _require_document_identity(
        metadata, "metadata", profile, stage="staging", promoted=False
    )
    _require_document_identity(
        metrics, "metrics", profile, stage="staging", promoted=False
    )
    expected_item_kind = model_generations.MODEL_ITEM_KINDS[profile.name]
    _require_new_item_kind(
        metadata,
        "metadata",
        expected_item_kind,
        source_keys=("source_database",),
    )
    _require_new_item_kind(
        metrics,
        "metrics",
        expected_item_kind,
        source_keys=("source_database",),
    )
    trained_at = _require_same(metadata, metrics, "trained_at", "metadata", "metrics")
    for key in (
        "feature_contract_version",
        "feature_contract_sha256",
        "source_database",
        "cache_sha256",
        "cache_schema_version",
        "split",
        "row_exclusions",
        "rows",
        "fit_train_rows",
        "calibration",
        "premium_scale",
        "test_interval_coverage_15_85",
    ):
        _require_same(metadata, metrics, key, "metadata", "metrics")
    if not _same(metadata.get("test_metrics"), metrics.get("test")):
        raise PromotionError(
            "mixed candidate bundle: metadata.test_metrics differs from metrics.test"
        )
    _require_int(metrics.get("rows"), "metrics.rows", minimum=1)
    _require_int(metrics.get("fit_train_rows"), "metrics.fit_train_rows", minimum=1)
    if metadata.get("feature_contract_version") != NOVA_FEATURE_CONTRACT_VERSION:
        raise PromotionError("Nova feature contract version differs from runtime code")
    if metadata.get("feature_contract_sha256") != NOVA_FEATURE_CONTRACT_SHA256:
        raise PromotionError("Nova feature contract SHA-256 differs from runtime code")
    _require_sha(
        metadata.get("feature_contract_sha256"), "metadata.feature_contract_sha256"
    )
    _require_sha(metadata.get("cache_sha256"), "metadata.cache_sha256")
    source = metadata.get("source_database")
    if not isinstance(source, dict) or not source:
        raise PromotionError(
            "Nova source_database provenance must be a non-empty object"
        )
    _require_sha(
        source.get("logical_sha256"), "metadata.source_database.logical_sha256"
    )
    if source.get("valid_price_rows") != metrics.get("rows"):
        raise PromotionError("Nova metrics.rows differs from source valid_price_rows")
    if not isinstance(metadata.get("split"), dict) or not metadata["split"]:
        raise PromotionError("Nova split provenance must be a non-empty object")
    if metadata["split"].get("test_rows") != metrics.get("test_rows"):
        raise PromotionError("Nova split test_rows differs from metrics.test_rows")
    _require_int(
        metadata.get("cache_schema_version"),
        "metadata.cache_schema_version",
        minimum=1,
    )
    if not isinstance(metadata.get("row_exclusions"), dict):
        raise PromotionError("Nova row_exclusions must be an object")
    if metadata.get("feature_cols") != list(NOVA_CAT_FEATURES) + list(
        NOVA_NUMERIC_FEATURES
    ):
        raise PromotionError("Nova combined feature columns differ from runtime code")
    if (
        metrics.get("architecture")
        != "categorical_embeddings_residual_mlp_ordered_quantiles"
    ):
        raise PromotionError(
            "Nova architecture does not guarantee ordered q15/q50/q85 outputs"
        )
    _validate_nova_metadata(metadata)
    inspection = _inspect_nova_model(candidate / profile.model_filename, metadata)
    parameters = _require_int(
        metrics.get("parameters"), "metrics.parameters", minimum=1
    )
    if inspection.get("parameters") != parameters:
        raise PromotionError("Nova state dict parameter count differs from metrics")
    declared_sha_values = [metadata.get("model_sha256"), metrics.get("model_sha256")]
    for value in declared_sha_values:
        if _require_sha(value, "Nova model_sha256") != model_sha256:
            raise PromotionError("Nova model SHA-256 differs from metadata/metrics")
    if declared_sha_values[0] != declared_sha_values[1]:
        raise PromotionError("Nova metadata and metrics model SHA-256 differ")

    quality = _validate_quality_gate(profile, metrics, production_metrics)
    metadata_coverage = _require_number(
        metadata.get("test_interval_coverage_15_85"),
        "metadata.test_interval_coverage_15_85",
        minimum=MIN_INTERVAL_COVERAGE,
        maximum=1,
    )
    if not math.isclose(
        metadata_coverage, quality["interval_coverage"], rel_tol=0, abs_tol=1e-12
    ):
        raise PromotionError(
            "Nova metadata interval coverage differs from quality gate"
        )
    identity = {
        "artifact_version": profile.artifact_version,
        "model_name": profile.name,
        "item_kind": expected_item_kind,
        "trained_at": trained_at,
        "feature_contract_sha256": _canonical_sha256(
            {
                "cat_features": metadata["cat_features"],
                "numeric_features": metadata["numeric_features"],
                "model_config": metadata["model_config"],
            }
        ),
        "quality_gate": quality,
        "parameter_count": parameters,
    }
    return metadata, {"metadata": metadata, "metrics": metrics}, identity


def _snapshot_bundle(
    directory: Path, filenames: tuple[str, ...]
) -> tuple[dict[str, tuple[int, int, int, int, int]], dict[str, dict[str, Any]]]:
    identities: dict[str, tuple[int, int, int, int, int]] = {}
    records: dict[str, dict[str, Any]] = {}
    for filename in filenames:
        path = directory / filename
        _require_regular_file(path)
        identities[filename] = _file_identity(path)
        records[filename] = {
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
    return identities, records


def _assert_identities(
    directory: Path,
    identities: Mapping[str, tuple[int, int, int, int, int]],
    label: str,
) -> None:
    for filename, expected in identities.items():
        path = directory / filename
        if path.is_symlink() or _file_identity(path) != expected:
            raise PromotionError(
                f"{label} changed while it was being validated: {filename}"
            )


def _validate_production_baseline(
    profile: ModelProfile,
    production: Path,
    metrics: Mapping[str, Any],
    file_records: Mapping[str, Mapping[str, Any]],
    *,
    trusted_generation_model_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Tie the comparison baseline to the bytes currently loaded in production."""

    _require_timestamp(metrics.get("trained_at"), "production metrics.trained_at")
    actual_model_sha = file_records[profile.model_filename]["sha256"]
    declared_model_sha = metrics.get("model_sha256")
    compatibility_fields: list[str] = []
    if profile.name in {"almaz", "nova"}:
        expected_item_kind = model_generations.MODEL_ITEM_KINDS[profile.name]
        _validate_legacy_production_item_kind(
            metrics,
            "production metrics",
            expected_item_kind,
            compatibility_fields,
            source_keys=(
                ("source_database_before", "source_database_after")
                if profile.name == "almaz"
                else ("source_database",)
            ),
        )
    if profile.name in ISOLATED_ALMAZ_LIKE_MODELS:
        expected_item_kind = ALMAZ_LIKE_ITEM_KINDS[profile.name]
        if metrics.get("model_name") != profile.name:
            raise CandidateIntegrityError(
                f"production metrics.model_name must be {profile.name!r}"
            )
        if metrics.get("item_kind") != expected_item_kind:
            raise CandidateIntegrityError(
                "production metrics.item_kind must be "
                f"{expected_item_kind!r}"
            )
        artifact = _load_almaz_artifact(production / profile.model_filename)
        if artifact.get("model_name") != profile.name:
            raise CandidateIntegrityError(
                f"production artifact.model_name must be {profile.name!r}"
            )
        if artifact.get("item_kind") != expected_item_kind:
            raise CandidateIntegrityError(
                "production artifact.item_kind must be "
                f"{expected_item_kind!r}"
            )
        if artifact.get("trained_at") != metrics.get("trained_at"):
            raise CandidateIntegrityError(
                "production artifact and metrics trained_at values differ"
            )
        if declared_model_sha is None:
            raise CandidateIntegrityError(
                "production metrics do not identify the model SHA-256"
            )
    if profile.name == "almaz" and declared_model_sha is None:
        trusted_sha = (
            _require_sha(
                trusted_generation_model_sha256,
                "active generation model SHA-256",
            )
            if trusted_generation_model_sha256 is not None
            else None
        )
        if trusted_sha != actual_model_sha:
            raise CandidateIntegrityError(
                "Almaz production metrics do not identify the model SHA-256"
            )
    if declared_model_sha is not None and (
        _require_sha(declared_model_sha, "production metrics.model_sha256")
        != actual_model_sha
    ):
        raise CandidateIntegrityError(
            "production model bytes differ from production metrics SHA-256"
        )

    if profile.name == "nova":
        metadata = _read_json(
            production / "nova_metadata.json", "production nova_metadata.json"
        )
        if metadata.get("model_name") != "nova":
            raise PromotionError("production Nova metadata has the wrong model_name")
        if metadata.get("trained_at") != metrics.get("trained_at"):
            raise PromotionError(
                "production Nova metadata and metrics trained_at values differ"
            )
        _validate_legacy_production_item_kind(
            metadata,
            "production Nova metadata",
            model_generations.MODEL_ITEM_KINDS[profile.name],
            compatibility_fields,
            source_keys=("source_database",),
        )
        metadata_model_sha = metadata.get("model_sha256")
        if metadata_model_sha is not None and (
            _require_sha(metadata_model_sha, "production metadata.model_sha256")
            != actual_model_sha
        ):
            raise PromotionError(
                "production Nova model bytes differ from metadata SHA-256"
            )
    if compatibility_fields:
        fields = sorted(set(compatibility_fields))
        warnings.warn(
            f"legacy {profile.name} production item_kind inferred from fixed "
            f"model id for: {', '.join(fields)}",
            RuntimeWarning,
            stacklevel=2,
        )
        return {
            "mode": "inferred_from_fixed_model_id",
            "item_kind": model_generations.MODEL_ITEM_KINDS[profile.name],
            "fields": fields,
        }
    return None


def _validate_candidate_internal(
    model: str, candidate_dir: str | Path | None
) -> _ValidatedCandidate:
    profile = _profile(model)
    candidate = _absolute(
        candidate_dir if candidate_dir is not None else profile.candidate_dir
    )
    try:
        active_pointer = model_generations.read_active_pointer(
            profile.name, project_dir=PROJECT_DIR
        )
        production = _absolute(
            model_generations.active_or_legacy_bundle_dir(
                profile.name,
                profile.production_dir,
                project_dir=PROJECT_DIR,
                # _snapshot_bundle below hashes each production file once;
                # compare those records to the trusted generation manifest
                # instead of hashing a multi-GB bundle twice.
                verify_files=False,
            )
        )
    except model_generations.GenerationError as exc:
        raise PromotionError(f"active production generation is unsafe: {exc}") from exc
    _require_directory(candidate, "candidate directory")
    if candidate == production:
        raise PromotionError("candidate and production directories must differ")

    production_bundle_presence = [
        (production / filename).exists() or (production / filename).is_symlink()
        for filename in profile.bundle_files
    ]
    if any(production_bundle_presence) and not all(production_bundle_presence):
        raise CandidateIntegrityError("production bundle is incomplete")
    bootstrap = (
        profile.name in ISOLATED_ALMAZ_LIKE_MODELS
        and active_pointer is None
        and not any(production_bundle_presence)
    )
    if not bootstrap:
        _require_directory(production, "production directory")

    file_identities, file_records = _snapshot_bundle(candidate, profile.bundle_files)
    production_identities: dict[str, tuple[int, int, int, int, int]] = {}
    production_records: dict[str, dict[str, Any]] = {}
    if not bootstrap:
        production_identities, production_records = _snapshot_bundle(
            production, profile.bundle_files
        )
    trusted_generation_model_sha256 = None
    if active_pointer is not None:
        active_manifest = model_generations.verify_generation(
            profile.name,
            production,
            pointer=active_pointer,
            project_dir=PROJECT_DIR,
            verify_files=False,
        )
        if not _same(active_manifest.get("files"), production_records):
            raise PromotionError(
                "active generation bytes differ from its immutable manifest"
            )
        trusted_generation_model_sha256 = active_manifest["files"][
            profile.model_filename
        ]["sha256"]
    production_metrics: dict[str, Any] = {}
    production_item_kind_compatibility: dict[str, Any] | None = None
    if not bootstrap:
        production_metrics = _read_json(
            production / profile.metrics_filename,
            f"production {profile.metrics_filename}",
        )
        production_item_kind_compatibility = _validate_production_baseline(
            profile,
            production,
            production_metrics,
            production_records,
            trusted_generation_model_sha256=trusted_generation_model_sha256,
        )
    metrics = _read_json(candidate / profile.metrics_filename, profile.metrics_filename)
    model_sha256 = file_records[profile.model_filename]["sha256"]
    if profile.name in ALMAZ_LIKE_MODELS:
        model_object, documents, identity = _validate_almaz(
            profile,
            candidate,
            metrics,
            production_metrics,
            model_sha256,
            bootstrap=bootstrap,
        )
    else:
        model_object, documents, identity = _validate_nova(
            profile, candidate, metrics, production_metrics, model_sha256
        )
    if production_item_kind_compatibility is not None:
        identity["production_item_kind_compatibility"] = (
            production_item_kind_compatibility
        )
    _assert_identities(candidate, file_identities, "candidate")
    if not bootstrap:
        _assert_identities(production, production_identities, "production bundle")
    try:
        current_pointer = model_generations.read_active_pointer(
            profile.name, project_dir=PROJECT_DIR
        )
    except model_generations.GenerationError as exc:
        raise PromotionError(f"active production generation became unsafe: {exc}") from exc
    if (current_pointer or {}).get("generation_id") != (
        active_pointer or {}
    ).get("generation_id"):
        raise PromotionError("active production generation changed during validation")

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        **identity,
        "stage": "staging",
        "generated_at": _utc_now(),
        "candidate_directory": str(candidate),
        "production_directory": str(production),
        "files": file_records,
    }
    manifest["bundle_sha256"] = _canonical_sha256(file_records)
    return _ValidatedCandidate(
        profile=profile,
        candidate_dir=candidate,
        documents=documents,
        model_object=model_object,
        manifest=manifest,
        file_identities=file_identities,
        production_identities=production_identities,
    )


def validate_candidate(
    model: str, candidate_dir: str | Path | None = None
) -> dict[str, Any]:
    """Validate a staging candidate and its current baseline without writing."""

    try:
        return _validate_candidate_internal(model, candidate_dir).manifest
    except CandidateValidationError:
        raise
    except PromotionError as exc:
        raise CandidateIntegrityError(str(exc)) from exc


def find_live_service_processes() -> list[dict[str, Any]]:
    """Find Python dashboard/deal-scanner processes visible to this user."""

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
    script_targets = {"dashboard_server.py", "deal_scanner.py"}
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
        is_python = any(
            Path(token).name.casefold().startswith("python") for token in tokens[:2]
        )
        services = {
            Path(token).stem for token in tokens if Path(token).name in script_targets
        }
        services.update(
            tokens[index + 1]
            for index in range(len(tokens) - 1)
            if tokens[index] == "-m" and tokens[index + 1] in module_targets
        )
        if is_python and services:
            matches.append({"pid": pid, "service": min(services), "command": command})
    return matches


def _assert_services_offline() -> None:
    live = find_live_service_processes()
    if live:
        summary = ", ".join(f"PID {item['pid']} ({item['service']})" for item in live)
        raise PromotionError(
            "refusing model promotion while dashboard/deal_scanner are live: " + summary
        )


@contextmanager
def _promotion_lock(lock_path: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError:  # Windows
        import portable_fcntl as fcntl
    if not _PROMOTION_THREAD_LOCK.acquire(blocking=False):
        raise PromotionError(
            "another model promotion is already running in this process"
        )
    descriptor: int | None = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        _require_directory(lock_path.parent, "promotion lock directory")
        if lock_path.is_symlink():
            raise PromotionError(f"promotion lock must not be a symlink: {lock_path}")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            portable_os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
        except BlockingIOError as exc:
            raise PromotionError(
                "another model promotion process holds the lock"
            ) from exc
        except OSError as exc:
            raise PromotionError(
                f"cannot acquire model promotion lock: {lock_path}"
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
    """Return the only inter-process lock accepted for production promotion."""

    return _absolute(PROJECT_DIR / ".run" / "model_candidate_promotion.lock")


def _fsync_directory(path: Path) -> None:
    portable_os.fsync_directory(path)


def _atomic_json(value: Mapping[str, Any], path: Path) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_joblib(value: Mapping[str, Any], path: Path) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        if temporary.exists():
            raise PromotionError(f"refusing existing temporary file: {temporary}")
        joblib.dump(dict(value), temporary)
        # FlushFileBuffers requires a writable handle on Windows.
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise PromotionError(f"cannot safely open source file: {source}") from exc
    try:
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise PromotionError(f"source is no longer a regular file: {source}")
        digest = hashlib.sha256()
        with (
            os.fdopen(os.dup(source_fd), "rb") as input_handle,
            destination.open("xb") as output_handle,
        ):
            while True:
                chunk = input_handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output_handle.write(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        if digest.hexdigest() != expected_sha256:
            raise PromotionError(f"source changed after validation: {source.name}")
    finally:
        os.close(source_fd)


def _unique_backup_path(profile: ModelProfile) -> Path:
    root = _absolute(profile.backup_root)
    if root.is_symlink():
        raise PromotionError(f"backup root must not be a symlink: {root}")
    _require_directory(root.parent, "backup root parent")
    root_created = not root.exists()
    root.mkdir(exist_ok=True)
    _require_directory(root, "backup root")
    if root_created:
        _fsync_directory(root.parent)
    base = root / f"{profile.name}_{_timestamp_slug()}"
    for suffix in range(10_000):
        candidate = base if suffix == 0 else root / f"{base.name}_{suffix:03d}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise PromotionError(f"cannot allocate a unique {profile.name} backup directory")


def _create_backup(validated: _ValidatedCandidate) -> tuple[Path, dict[str, Any]]:
    profile = validated.profile
    production = _absolute(profile.production_dir)
    backup = _unique_backup_path(profile)
    backup.mkdir(mode=0o700)
    file_records: dict[str, dict[str, Any]] = {}
    try:
        for filename in profile.bundle_files:
            source = production / filename
            sha = _sha256_file(source)
            _copy_verified(source, backup / filename, sha)
            file_records[filename] = {
                "sha256": sha,
                "size_bytes": (backup / filename).stat().st_size,
                "restore_target": str(source),
            }
        current_manifest = production / MANIFEST_FILENAME
        if current_manifest.exists() or current_manifest.is_symlink():
            _require_regular_file(current_manifest, label="production manifest")
            sha = _sha256_file(current_manifest)
            _copy_verified(current_manifest, backup / MANIFEST_FILENAME, sha)
            file_records[MANIFEST_FILENAME] = {
                "sha256": sha,
                "size_bytes": (backup / MANIFEST_FILENAME).stat().st_size,
                "restore_target": str(current_manifest),
            }
        _assert_identities(
            production, validated.production_identities, "production bundle"
        )
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "model_name": profile.name,
            "created_at": _utc_now(),
            "reason": "complete pre-promotion production bundle",
            "files": file_records,
        }
        manifest["bundle_sha256"] = _canonical_sha256(file_records)
        _atomic_json(manifest, backup / BACKUP_MANIFEST_FILENAME)
        _fsync_directory(backup)
        _fsync_directory(backup.parent)
        return backup, manifest
    except BaseException:
        shutil.rmtree(backup, ignore_errors=True)
        raise


def _validate_prepared_bundle(
    validated: _ValidatedCandidate,
    next_dir: Path,
    expected_manifest: Mapping[str, Any],
) -> None:
    """Read back the complete staged production bundle before activation."""

    profile = validated.profile
    _require_directory(next_dir, "prepared bundle directory")
    _identities, file_records = _snapshot_bundle(next_dir, profile.bundle_files)
    if not _same(file_records, expected_manifest.get("files")):
        raise PromotionError(
            "prepared bundle files differ from its production manifest"
        )
    if expected_manifest.get("bundle_sha256") != _canonical_sha256(file_records):
        raise PromotionError("prepared production bundle SHA-256 is inconsistent")
    expected_item_kind = model_generations.MODEL_ITEM_KINDS[profile.name]
    if expected_manifest.get("item_kind") != expected_item_kind:
        raise PromotionError("prepared production manifest has the wrong item_kind")
    manifest = _read_json(next_dir / MANIFEST_FILENAME, MANIFEST_FILENAME)
    if not _same(manifest, expected_manifest):
        raise PromotionError(
            "prepared production manifest failed read-back verification"
        )

    metrics = _read_json(
        next_dir / profile.metrics_filename, f"prepared {profile.metrics_filename}"
    )
    _require_document_identity(
        metrics, "prepared metrics", profile, stage="production", promoted=True
    )
    _require_new_item_kind(
        metrics,
        "prepared metrics",
        expected_item_kind,
        source_keys=(
            ("source_database_before", "source_database_after")
            if profile.name in ALMAZ_LIKE_MODELS
            else ("source_database",)
        ),
    )
    if not _same(
        metrics.get("shared_holdout_comparison"),
        validated.documents["metrics"].get("shared_holdout_comparison"),
    ):
        raise PromotionError("prepared metrics changed the validated quality evidence")
    actual_model_sha = file_records[profile.model_filename]["sha256"]
    if metrics.get("model_sha256") != actual_model_sha:
        raise PromotionError(
            "prepared metrics do not identify the prepared model bytes"
        )

    if profile.name in ALMAZ_LIKE_MODELS:
        artifact = _load_almaz_artifact(next_dir / profile.model_filename)
        _require_document_identity(
            artifact, "prepared artifact", profile, stage="production", promoted=True
        )
        _require_new_item_kind(
            artifact,
            "prepared artifact",
            expected_item_kind,
            source_keys=("source_database",),
        )
        _require_same(
            artifact, metrics, "trained_at", "prepared artifact", "prepared metrics"
        )
        features = _require_string_list(
            artifact.get("feature_columns"), "prepared artifact.feature_columns"
        )
        _validate_almaz_pipeline(artifact.get("pipeline"), "point", features)
        _validate_almaz_pipeline(artifact.get("low_pipeline"), "q15", features)
        _validate_almaz_pipeline(artifact.get("high_pipeline"), "q85", features)
    else:
        metadata = _read_json(
            next_dir / "nova_metadata.json", "prepared nova_metadata.json"
        )
        _require_document_identity(
            metadata, "prepared metadata", profile, stage="production", promoted=True
        )
        _require_new_item_kind(
            metadata,
            "prepared metadata",
            expected_item_kind,
            source_keys=("source_database",),
        )
        _require_same(
            metadata, metrics, "trained_at", "prepared metadata", "prepared metrics"
        )
        if metadata.get("model_sha256") != actual_model_sha:
            raise PromotionError("prepared Nova metadata has the wrong model SHA-256")
        if metrics.get("metadata_sha256") != _sha256_file(
            next_dir / "nova_metadata.json"
        ):
            raise PromotionError("prepared Nova metadata SHA-256 differs from metrics")
        _validate_nova_metadata(metadata)
        inspection = _inspect_nova_model(next_dir / profile.model_filename, metadata)
        if inspection.get("parameters") != metrics.get("parameters"):
            raise PromotionError(
                "prepared Nova model parameter count differs from metrics"
            )


def _prepare_next_bundle(
    validated: _ValidatedCandidate, backup: Path
) -> tuple[Path, dict[str, Any]]:
    profile = validated.profile
    production = _absolute(profile.production_dir)
    next_dir = Path(tempfile.mkdtemp(prefix=f".{profile.name}-next-", dir=production))
    candidate_manifest = validated.manifest
    promoted_at = _utc_now()
    promotion_record = {
        "promoted_at": promoted_at,
        "backup_directory": str(backup),
        "candidate_directory": str(validated.candidate_dir),
        "candidate_bundle_sha256": candidate_manifest["bundle_sha256"],
    }
    try:
        if profile.name in ALMAZ_LIKE_MODELS:
            promoted_artifact = dict(validated.model_object)
            promoted_artifact.update(
                {
                    "stage": "production",
                    "production_promoted": True,
                    "promoted_at": promoted_at,
                    "promotion_backup_dir": str(backup),
                    "promotion": promotion_record,
                }
            )
            _atomic_joblib(promoted_artifact, next_dir / profile.model_filename)
            promoted_metrics = dict(validated.documents["metrics"])
            promoted_metrics.update(
                {
                    "stage": "production",
                    "production_promoted": True,
                    "promoted_at": promoted_at,
                    "promotion_backup_dir": str(backup),
                    "promotion": promotion_record,
                    "model_sha256": _sha256_file(next_dir / profile.model_filename),
                }
            )
            _atomic_json(promoted_metrics, next_dir / profile.metrics_filename)
        else:
            _copy_verified(
                validated.candidate_dir / profile.model_filename,
                next_dir / profile.model_filename,
                candidate_manifest["files"][profile.model_filename]["sha256"],
            )
            model_sha = _sha256_file(next_dir / profile.model_filename)
            promoted_metadata = dict(validated.documents["metadata"])
            promoted_metadata.update(
                {
                    "stage": "production",
                    "production_promoted": True,
                    "promoted_at": promoted_at,
                    "promotion": promotion_record,
                    "model_sha256": model_sha,
                }
            )
            _atomic_json(promoted_metadata, next_dir / "nova_metadata.json")
            promoted_metrics = dict(validated.documents["metrics"])
            promoted_metrics.update(
                {
                    "stage": "production",
                    "production_promoted": True,
                    "promoted_at": promoted_at,
                    "promotion": promotion_record,
                    "model_sha256": model_sha,
                    "metadata_sha256": _sha256_file(next_dir / "nova_metadata.json"),
                }
            )
            _atomic_json(promoted_metrics, next_dir / profile.metrics_filename)

        files: dict[str, dict[str, Any]] = {}
        for filename in profile.bundle_files:
            path = next_dir / filename
            files[filename] = {
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        production_manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "artifact_version": profile.artifact_version,
            "model_name": profile.name,
            "item_kind": validated.manifest["item_kind"],
            "stage": "production",
            "production_promoted": True,
            "trained_at": validated.manifest["trained_at"],
            "generated_at": promoted_at,
            "feature_contract_sha256": validated.manifest["feature_contract_sha256"],
            "quality_gate": validated.manifest["quality_gate"],
            "promotion": promotion_record,
            "files": files,
        }
        production_manifest["bundle_sha256"] = _canonical_sha256(files)
        # Commit marker: always prepared after every bundle document.
        _atomic_json(production_manifest, next_dir / MANIFEST_FILENAME)
        _validate_prepared_bundle(validated, next_dir, production_manifest)
        _fsync_directory(next_dir)
        return next_dir, production_manifest
    except BaseException:
        shutil.rmtree(next_dir, ignore_errors=True)
        raise


def _restore_from_backup(
    profile: ModelProfile, backup: Path, installed: list[str]
) -> list[str]:
    production = _absolute(profile.production_dir)
    errors: list[str] = []
    backup_manifest = _read_json(
        backup / BACKUP_MANIFEST_FILENAME, BACKUP_MANIFEST_FILENAME
    )
    backed_up = backup_manifest.get("files")
    if not isinstance(backed_up, dict):
        return ["backup manifest has no files object"]
    for filename in reversed(installed):
        destination = production / filename
        record = backed_up.get(filename)
        try:
            if isinstance(record, dict):
                expected = _require_sha(
                    record.get("sha256"), f"backup {filename} SHA-256"
                )
                rollback_temp = production / f".{filename}.{os.getpid()}.rollback"
                rollback_temp.unlink(missing_ok=True)
                _copy_verified(backup / filename, rollback_temp, expected)
                os.replace(rollback_temp, destination)
            else:
                # The manifest is newly introduced on first promotion.
                if filename != MANIFEST_FILENAME:
                    raise PromotionError(
                        f"backup is missing required original {filename}"
                    )
                destination.unlink(missing_ok=True)
        except BaseException as exc:  # noqa: BLE001 - rollback must report every failure.
            errors.append(f"cannot restore {filename}: {exc}")
    try:
        _fsync_directory(production)
    except OSError as exc:
        errors.append(f"cannot fsync restored production directory: {exc}")
    return errors


def _installed_candidate_bundle_sha256(profile: ModelProfile) -> str | None:
    """Return a verified promotion receipt from the installed manifest."""

    production = _absolute(profile.production_dir)
    manifest_path = production / MANIFEST_FILENAME
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return None
    _require_directory(production, "production directory")
    identities, files = _snapshot_bundle(production, profile.bundle_files)
    manifest_identity = _file_identity(manifest_path)
    metrics = _read_json(
        production / profile.metrics_filename,
        f"production {profile.metrics_filename}",
    )
    _validate_production_baseline(profile, production, metrics, files)
    manifest = _read_json(manifest_path, "production artifact manifest")
    if (
        manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("model_name") != profile.name
        or manifest.get("stage") != "production"
        or manifest.get("production_promoted") is not True
        or not _same(manifest.get("files"), files)
        or manifest.get("bundle_sha256") != _canonical_sha256(files)
    ):
        raise PromotionError("installed production manifest is incoherent")
    declared_manifest_item_kind = manifest.get("item_kind")
    if (
        declared_manifest_item_kind is not None
        and declared_manifest_item_kind
        != model_generations.MODEL_ITEM_KINDS[profile.name]
    ):
        raise PromotionError("installed production manifest has the wrong item_kind")
    promotion = manifest.get("promotion")
    if not isinstance(promotion, dict):
        raise PromotionError("installed production manifest has no promotion receipt")
    candidate_sha = _require_sha(
        promotion.get("candidate_bundle_sha256"),
        "installed promotion candidate_bundle_sha256",
    )
    if not _same(promotion, metrics.get("promotion")):
        raise PromotionError(
            "installed production manifest and metrics promotion receipts differ"
        )
    if (
        manifest.get("artifact_version") != profile.artifact_version
        or manifest.get("trained_at") != metrics.get("trained_at")
    ):
        raise PromotionError("installed production manifest identity is incoherent")
    if profile.name in ALMAZ_LIKE_MODELS:
        artifact = _load_almaz_artifact(production / profile.model_filename)
        if not _same(promotion, artifact.get("promotion")):
            raise PromotionError(
                "installed Almaz artifact promotion receipt differs"
            )
    else:
        metadata = _read_json(
            production / "nova_metadata.json", "production nova_metadata.json"
        )
        if (
            not _same(promotion, metadata.get("promotion"))
            or manifest.get("feature_contract_sha256")
            != _canonical_sha256(
                {
                    "cat_features": metadata.get("cat_features"),
                    "numeric_features": metadata.get("numeric_features"),
                    "model_config": metadata.get("model_config"),
                }
            )
        ):
            raise PromotionError(
                "installed Nova metadata and manifest are incoherent"
            )
    _assert_identities(production, identities, "installed production bundle")
    if manifest_path.is_symlink() or _file_identity(manifest_path) != manifest_identity:
        raise PromotionError("installed production manifest changed while inspected")
    return candidate_sha


def installed_candidate_bundle_sha256(model: str) -> str | None:
    """Read a cryptographically verified receipt for an installed candidate."""

    try:
        with model_lifecycle.production_model_read_lock(project_dir=PROJECT_DIR):
            return _installed_candidate_bundle_sha256(_profile(model))
    except (model_lifecycle.ModelLifecycleError, model_generations.GenerationError) as exc:
        raise PromotionError(str(exc)) from exc


def _validate_generic_journal(record: Mapping[str, Any]) -> tuple[ModelProfile, Path]:
    if record.get("kind") != "generic_files_v1":
        raise PromotionError("promotion journal belongs to another promoter")
    profile = _profile(str(record.get("model") or ""))
    production = _absolute(profile.production_dir)
    _require_directory(production, "journal production directory")
    if record.get("production_directory") != str(production):
        raise PromotionError("promotion journal production path is not canonical")
    backup = _absolute(str(record.get("backup_directory") or ""))
    backup_root = _absolute(profile.backup_root)
    _require_directory(backup_root, "journal backup root")
    try:
        relative = backup.relative_to(backup_root)
    except ValueError as exc:
        raise PromotionError("promotion journal backup escapes its canonical root") from exc
    if len(relative.parts) != 1 or not relative.name.startswith(f"{profile.name}_"):
        raise PromotionError("promotion journal backup name is invalid")
    _require_directory(backup, "journal backup directory")
    _require_sha(
        record.get("candidate_bundle_sha256"),
        "promotion journal candidate_bundle_sha256",
    )
    _require_timestamp(record.get("created_at"), "promotion journal created_at")
    expected_backup_manifest_sha = _require_sha(
        record.get("backup_manifest_sha256"),
        "promotion journal backup_manifest_sha256",
    )
    backup_manifest_path = backup / BACKUP_MANIFEST_FILENAME
    if _sha256_file(backup_manifest_path) != expected_backup_manifest_sha:
        raise PromotionError("promotion journal backup manifest SHA-256 differs")
    backup_manifest = _read_json(
        backup_manifest_path, BACKUP_MANIFEST_FILENAME
    )
    backup_files = backup_manifest.get("files")
    allowed_files = {*profile.bundle_files, MANIFEST_FILENAME}
    if (
        backup_manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or backup_manifest.get("model_name") != profile.name
        or not isinstance(backup_files, dict)
        or not set(profile.bundle_files).issubset(backup_files)
        or not set(backup_files).issubset(allowed_files)
        or backup_manifest.get("bundle_sha256")
        != _canonical_sha256(backup_files)
    ):
        raise PromotionError("promotion journal backup manifest is incoherent")
    for filename, file_record in backup_files.items():
        if not isinstance(file_record, dict):
            raise PromotionError("promotion journal backup file record is invalid")
        _require_sha(
            file_record.get("sha256"), f"backup manifest {filename} SHA-256"
        )
        if file_record.get("restore_target") != str(production / filename):
            raise PromotionError(
                "promotion journal backup restore target is not canonical"
            )
    return profile, backup


def _recover_generic_journal_locked(record: Mapping[str, Any]) -> dict[str, Any]:
    profile, backup = _validate_generic_journal(record)
    errors = _restore_from_backup(profile, backup, list(profile.activation_order))
    if errors:
        raise PromotionError(
            "interrupted promotion recovery failed: " + "; ".join(errors)
        )
    production = _absolute(profile.production_dir)
    _identities, files = _snapshot_bundle(production, profile.bundle_files)
    metrics = _read_json(
        production / profile.metrics_filename,
        f"recovered production {profile.metrics_filename}",
    )
    _validate_production_baseline(profile, production, metrics, files)
    try:
        model_lifecycle.clear_promotion_journal(
            expected_model=profile.name,
            expected_backup=str(backup),
            project_dir=PROJECT_DIR,
        )
    except model_lifecycle.ModelLifecycleError as exc:
        raise PromotionError("cannot clear recovered promotion journal") from exc
    return {
        "status": "recovered",
        "model_name": profile.name,
        "backup_directory": str(backup),
    }


def recover_interrupted_promotion() -> dict[str, Any] | None:
    """Rollback one crash-interrupted Almaz/Nova transaction, if present."""

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
            return _recover_generic_journal_locked(record)
    except (model_lifecycle.ModelLifecycleError, model_generations.GenerationError) as exc:
        raise PromotionError(str(exc)) from exc


def _activate_bundle(profile: ModelProfile, next_dir: Path, backup: Path) -> None:
    production = _absolute(profile.production_dir)
    installed: list[str] = []
    try:
        for filename in profile.activation_order:
            os.replace(next_dir / filename, production / filename)
            installed.append(filename)
        _fsync_directory(production)
    except BaseException as activation_error:
        rollback_errors = _restore_from_backup(profile, backup, installed)
        message = (
            f"{profile.name} activation failed and was rolled back: {activation_error}"
        )
        if rollback_errors:
            message += "; manual recovery may be required: " + "; ".join(
                rollback_errors
            )
        raise PromotionError(message) from activation_error


def promote_candidate(
    model: str,
    candidate_dir: str | Path | None = None,
    *,
    offline_confirm: bool = False,
) -> dict[str, Any]:
    """Promote one complete verified bundle and retain a recoverable backup."""

    if offline_confirm is not True:
        raise PromotionError(
            "promotion requires --offline-confirm after stopping dashboard and deal_scanner"
        )
    profile = _profile(model)
    try:
        lifecycle = model_lifecycle.exclusive_model_update_lock(
            project_dir=PROJECT_DIR
        )
        with lifecycle, _promotion_lock(_canonical_promotion_lock_path()):
            unresolved = model_lifecycle.read_promotion_journal(
                project_dir=PROJECT_DIR
            )
            if unresolved is not None:
                _recover_generic_journal_locked(unresolved)
            if model_generations.read_active_pointer(
                profile.name, project_dir=PROJECT_DIR
            ) is not None:
                raise PromotionError(
                    "offline legacy promotion is disabled while an active hot "
                    "generation exists"
                )
            _assert_services_offline()
            validated = _validate_candidate_internal(profile.name, candidate_dir)
            _assert_services_offline()
            _assert_identities(
                validated.candidate_dir, validated.file_identities, "candidate"
            )
            backup, _backup_manifest = _create_backup(validated)
            next_dir: Path | None = None
            activated = False
            journal_written = False
            journal_record: dict[str, Any] | None = None
            try:
                next_dir, production_manifest = _prepare_next_bundle(
                    validated, backup
                )
                _assert_services_offline()
                _assert_identities(
                    validated.candidate_dir,
                    validated.file_identities,
                    "candidate",
                )
                _assert_identities(
                    _absolute(profile.production_dir),
                    validated.production_identities,
                    "production bundle",
                )
                try:
                    journal_record = model_lifecycle.write_promotion_journal(
                        {
                            "kind": "generic_files_v1",
                            "model": profile.name,
                            "production_directory": str(
                                _absolute(profile.production_dir)
                            ),
                            "backup_directory": str(backup),
                            "candidate_bundle_sha256": validated.manifest[
                                "bundle_sha256"
                            ],
                            "backup_manifest_sha256": _sha256_file(
                                backup / BACKUP_MANIFEST_FILENAME
                            ),
                            "created_at": _utc_now(),
                        },
                        project_dir=PROJECT_DIR,
                    )
                except model_lifecycle.ModelLifecycleError as exc:
                    raise PromotionError(
                        "cannot create durable promotion journal"
                    ) from exc
                journal_written = True
                try:
                    _activate_bundle(profile, next_dir, backup)
                    installed_sha = _installed_candidate_bundle_sha256(profile)
                    if installed_sha != validated.manifest["bundle_sha256"]:
                        raise PromotionError(
                            "installed production receipt differs from the candidate"
                        )
                    model_lifecycle.clear_promotion_journal(
                        expected_model=profile.name,
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
                                        "in-memory promotion journal is unavailable"
                                    )
                                recovery_record = model_lifecycle.write_promotion_journal(
                                    {
                                        key: value
                                        for key, value in journal_record.items()
                                        if key != "schema_version"
                                    },
                                    project_dir=PROJECT_DIR,
                                )
                            _recover_generic_journal_locked(recovery_record)
                            journal_written = False
                        except BaseException as recovery_error:  # noqa: BLE001 - recover on interrupts too.
                            raise PromotionError(
                                "promotion failed and durable recovery could not "
                                f"restore production: {recovery_error}"
                            ) from promotion_error
                    raise
            finally:
                if next_dir is not None and next_dir.exists():
                    shutil.rmtree(next_dir, ignore_errors=True)

            if not activated:  # pragma: no cover - exceptions leave first.
                raise PromotionError("promotion did not activate")
            return {
                "status": "promoted",
                "model_name": profile.name,
                "production_directory": str(profile.production_dir),
                "backup_directory": str(backup),
                "candidate_bundle_sha256": validated.manifest["bundle_sha256"],
                "production_bundle_sha256": production_manifest["bundle_sha256"],
                "manifest_path": str(profile.production_dir / MANIFEST_FILENAME),
            }
    except (model_lifecycle.ModelLifecycleError, model_generations.GenerationError) as exc:
        raise PromotionError(str(exc)) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate or safely promote an Almaz/Nova staging candidate"
    )
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--check", action="store_true", help="read-only validation")
    operation.add_argument(
        "--promote", action="store_true", help="activate the candidate"
    )
    parser.add_argument("--model", required=True, choices=tuple(MODEL_PROFILES))
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=None,
        help="candidate directory; filenames remain fixed for the selected model",
    )
    parser.add_argument(
        "--offline-confirm",
        action="store_true",
        help="confirm dashboard and deal_scanner were stopped",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.check:
            result = validate_candidate(args.model, args.candidate_dir)
        else:
            result = promote_candidate(
                args.model,
                args.candidate_dir,
                offline_confirm=args.offline_confirm,
            )
    except PromotionError as exc:
        raise SystemExit(f"model candidate error: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
