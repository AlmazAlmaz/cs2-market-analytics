"""Fail-closed validation for a staged Vega model bundle.

Vega is activated through :mod:`model_generations`; this module deliberately
keeps the quality gate read-only.  A candidate is accepted only when its three
fixed files are coherent, its PyTorch state can be loaded with ``weights_only``,
and it strictly beats the exact immutable model used as its benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import torch

import app_config
import model_generations
from item_classification import ITEM_KIND_WEAPON_SKIN, STRICT_ITEM_KIND_POLICY
from VEGAMODEL.features import (
    CAT_FEATURES,
    FEATURE_CONTRACT_SHA256,
    FEATURE_CONTRACT_VERSION,
    NUMERIC_FEATURES,
)
from VEGAMODEL.model import ARCHITECTURE_ID, VegaPriceNet

PROJECT_DIR = app_config.PROJECT_DIR
DEFAULT_CANDIDATE_DIR = PROJECT_DIR / "VEGAMODEL" / "staging" / "vega_candidate"
DEFAULT_PRODUCTION_DIR = PROJECT_DIR / "VEGAMODEL" / "artifacts"

EXPECTED_ARTIFACT_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
MODEL_FILENAME = "vega_price_model.pt"
METADATA_FILENAME = "vega_metadata.json"
METRICS_FILENAME = "vega_metrics.json"
BUNDLE_FILES = (MODEL_FILENAME, METADATA_FILENAME, METRICS_FILENAME)

MIN_QUALITY_HOLDOUT_ROWS = 25_000
MIN_INTERVAL_COVERAGE = 0.60
MAX_QUANTILE_CROSSING_RATE = 0.01
MAX_POINT_OUTSIDE_RATE = 0.01
VALIDATION_POINT_OUTSIDE_TARGET = 0.006
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_MODEL_BYTES = 1024 * 1024 * 1024
MAX_MODEL_PARAMETERS = 100_000_000
MAX_TOTAL_VOCABULARY = 500_000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PRIMARY_METRICS = (
    "median_ape_percent",
    "mean_ape_percent",
    "p90_ape_percent",
)
COMPARISON_METRICS = (*PRIMARY_METRICS, "mae_cents")
MODEL_CONFIG_KEYS = {
    "category_cardinalities",
    "numeric_count",
    "d_token",
    "block_count",
    "head_count",
    "ffn_hidden",
    "attention_dropout",
    "residual_dropout",
    "head_dropout",
}
EXPECTED_ARCHITECTURE_CONFIG = {
    "d_token": 192,
    "block_count": 4,
    "head_count": 8,
    "ffn_hidden": 768,
    "attention_dropout": 0.05,
    "residual_dropout": 0.08,
    "head_dropout": 0.05,
}
BENCHMARK_IDENTITY_KEYS = {
    "model_name",
    "active_generation_id",
    "model_sha256",
    "trained_at",
}


class PromotionError(RuntimeError):
    """The candidate failed an integrity check."""

    classification = "integrity"


class CandidateQualityError(PromotionError):
    """A coherent candidate failed a numerical quality threshold."""

    classification = "quality"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _raw_flags(base: int) -> int:
    return base | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


def _sha256_file(path: Path) -> str:
    """Hash exact on-disk bytes, including CRLF/0x1a on the Windows CRT."""

    digest = hashlib.sha256()
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _raw_flags(os.O_RDONLY))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise PromotionError(f"bundle file must be a non-empty regular file: {path}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    except PromotionError:
        raise
    except OSError as exc:
        raise PromotionError(f"cannot hash bundle file: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return digest.hexdigest()


def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    """Copy exact bytes and refuse a short, translated, or changed copy."""

    _require_sha256(expected_sha256, "copy expected_sha256")
    source_fd: int | None = None
    destination_fd: int | None = None
    digest = hashlib.sha256()
    try:
        source_fd = os.open(source, _raw_flags(os.O_RDONLY))
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_size <= 0:
            raise PromotionError(f"copy source is not a regular file: {source}")
        destination_fd = os.open(
            destination,
            _raw_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            0o600,
        )
        copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise PromotionError("short write while copying Vega artifact")
                view = view[written:]
        os.fsync(destination_fd)
        if copied != source_info.st_size or digest.hexdigest() != expected_sha256:
            raise PromotionError("copied Vega artifact SHA-256 differs from source")
    except PromotionError:
        raise
    except OSError as exc:
        raise PromotionError("cannot copy Vega artifact") from exc
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)
        if destination.exists():
            try:
                valid = _sha256_file(destination) == expected_sha256
            except PromotionError:
                valid = False
            if not valid:
                destination.unlink(missing_ok=True)


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise PromotionError(f"cannot stat bundle file: {path}") from exc
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _require_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise PromotionError(f"{label} is missing or unsafe: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PromotionError(f"cannot resolve {label}: {path}") from exc
    if resolved != path:
        raise PromotionError(f"{label} contains a symlink path component: {path}")


def _require_regular_file(path: Path, *, maximum_size: int | None = None) -> int:
    if path.is_symlink():
        raise PromotionError(f"bundle file must not be a symlink: {path}")
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise PromotionError(
            f"candidate bundle is incomplete; missing {path.name}"
        ) from exc
    except OSError as exc:
        raise PromotionError(f"cannot inspect bundle file: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise PromotionError(f"bundle file must be a non-empty regular file: {path}")
    if maximum_size is not None and info.st_size > maximum_size:
        raise PromotionError(f"bundle file exceeds its safe size limit: {path.name}")
    return int(info.st_size)


def _require_exact_bundle(directory: Path) -> None:
    try:
        names = {entry.name for entry in os.scandir(directory)}
    except OSError as exc:
        raise PromotionError("cannot enumerate Vega candidate bundle") from exc
    expected = set(BUNDLE_FILES)
    if names != expected:
        missing = sorted(expected - names)
        unexpected = sorted(names - expected)
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unexpected:
            details.append(f"unexpected {unexpected}")
        raise PromotionError(
            "Vega candidate must contain exactly its three bundle files: "
            + "; ".join(details)
        )


def _read_json(path: Path, label: str) -> dict[str, Any]:
    size = _require_regular_file(path, maximum_size=MAX_JSON_BYTES)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _raw_flags(os.O_RDONLY))
        info = os.fstat(descriptor)
        if info.st_size != size or not stat.S_ISREG(info.st_mode):
            raise PromotionError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise PromotionError(f"{label} ended unexpectedly")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise PromotionError(f"{label} changed while it was read")
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except PromotionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"{label} is not valid UTF-8 JSON") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise PromotionError(f"{label} must contain a JSON object")
    return value


def _require_int(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PromotionError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise PromotionError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise PromotionError(f"{label} must be at most {maximum}")
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


def _same(left: Any, right: Any) -> bool:
    return _canonical_sha256(left) == _canonical_sha256(right)


def _require_matching(
    metadata: Mapping[str, Any], metrics: Mapping[str, Any], key: str
) -> Any:
    if key not in metadata or key not in metrics or not _same(metadata[key], metrics[key]):
        raise PromotionError(f"mixed Vega candidate bundle: {key} does not match")
    return metadata[key]


def _validate_document_identity(document: Mapping[str, Any], label: str) -> None:
    version = _require_int(document.get("artifact_version"), f"{label}.artifact_version")
    if version != EXPECTED_ARTIFACT_VERSION:
        raise PromotionError(
            f"{label}.artifact_version must be {EXPECTED_ARTIFACT_VERSION}"
        )
    if document.get("model_name") != "vega":
        raise PromotionError(f"{label}.model_name must be 'vega'")
    if document.get("item_kind") != ITEM_KIND_WEAPON_SKIN:
        raise PromotionError(
            f"{label}.item_kind must be {ITEM_KIND_WEAPON_SKIN!r}"
        )
    if document.get("stage") != "staging":
        raise PromotionError(f"{label}.stage must be 'staging'")
    if document.get("production_promoted") is not False:
        raise PromotionError(f"{label}.production_promoted must be false")
    _require_timestamp(document.get("trained_at"), f"{label}.trained_at")


def _validate_model_config(metadata: Mapping[str, Any]) -> dict[str, Any]:
    config = metadata.get("model_config")
    if not isinstance(config, dict) or set(config) != MODEL_CONFIG_KEYS:
        raise PromotionError("metadata.model_config has unexpected or missing fields")
    cardinalities = config.get("category_cardinalities")
    if (
        not isinstance(cardinalities, list)
        or len(cardinalities) != len(CAT_FEATURES)
        or not all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and 2 <= value <= 100_000
            for value in cardinalities
        )
    ):
        raise PromotionError("Vega category cardinalities are invalid or unsafe")
    numeric_count = _require_int(
        config.get("numeric_count"), "metadata.model_config.numeric_count", minimum=1
    )
    if numeric_count != len(NUMERIC_FEATURES):
        raise PromotionError("Vega numeric_count differs from the runtime contract")
    width = _require_int(
        config.get("d_token"), "metadata.model_config.d_token", minimum=8, maximum=256
    )
    blocks = _require_int(
        config.get("block_count"),
        "metadata.model_config.block_count",
        minimum=1,
        maximum=8,
    )
    heads = _require_int(
        config.get("head_count"),
        "metadata.model_config.head_count",
        minimum=1,
        maximum=16,
    )
    if width % heads:
        raise PromotionError("Vega d_token must be divisible by head_count")
    hidden = _require_int(
        config.get("ffn_hidden"),
        "metadata.model_config.ffn_hidden",
        minimum=width,
        maximum=2048,
    )
    for key in ("attention_dropout", "residual_dropout", "head_dropout"):
        _require_number(
            config.get(key),
            f"metadata.model_config.{key}",
            minimum=0,
            maximum=0.5,
        )
    # Touch local variables so future edits cannot accidentally drop a bound.
    if blocks * width * hidden <= 0:  # pragma: no cover - guarded above
        raise PromotionError("Vega model configuration is empty")
    if any(
        config.get(key) != expected
        for key, expected in EXPECTED_ARCHITECTURE_CONFIG.items()
    ):
        raise PromotionError(
            "Vega model_config does not match the optimized production architecture"
        )
    return config


def _validate_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    cats = metadata.get("cat_features")
    numerics = metadata.get("numeric_features")
    if cats != list(CAT_FEATURES) or numerics != list(NUMERIC_FEATURES):
        raise PromotionError("Vega feature columns differ from runtime code")
    if metadata.get("feature_cols") != list(CAT_FEATURES) + list(NUMERIC_FEATURES):
        raise PromotionError("Vega combined feature order differs from runtime code")
    if metadata.get("architecture") != ARCHITECTURE_ID:
        raise PromotionError("Vega architecture does not guarantee ordered quantiles")
    config = _validate_model_config(metadata)

    vocabularies = metadata.get("vocabularies")
    medians = metadata.get("numeric_medians")
    scales = metadata.get("numeric_scales")
    if not isinstance(vocabularies, dict) or set(vocabularies) != set(CAT_FEATURES):
        raise PromotionError("Vega vocabularies differ from categorical features")
    if not isinstance(medians, dict) or set(medians) != set(NUMERIC_FEATURES):
        raise PromotionError("Vega numeric medians differ from numeric features")
    if not isinstance(scales, dict) or set(scales) != set(NUMERIC_FEATURES):
        raise PromotionError("Vega numeric scales differ from numeric features")
    total_vocabulary = 0
    for index, column in enumerate(CAT_FEATURES):
        vocabulary = vocabularies[column]
        if not isinstance(vocabulary, dict):
            raise PromotionError(f"Vega vocabulary {column} must be an object")
        if not all(isinstance(key, str) and key for key in vocabulary):
            raise PromotionError(f"Vega vocabulary {column} has an invalid key")
        values = list(vocabulary.values())
        expected_indices = set(range(2, len(values) + 2))
        if (
            any(isinstance(value, bool) or not isinstance(value, int) for value in values)
            or set(values) != expected_indices
            or config["category_cardinalities"][index] != len(values) + 2
        ):
            raise PromotionError(f"Vega vocabulary {column} is incoherent")
        total_vocabulary += len(values)
    if total_vocabulary > MAX_TOTAL_VOCABULARY:
        raise PromotionError("Vega total vocabulary exceeds the safe limit")
    for column in NUMERIC_FEATURES:
        _require_number(medians[column], f"metadata.numeric_medians.{column}")
        _require_number(
            scales[column],
            f"metadata.numeric_scales.{column}",
            minimum=1e-12,
        )

    calibration = metadata.get("calibration")
    if not isinstance(calibration, dict) or set(calibration) != {"low", "center", "high"}:
        raise PromotionError("Vega calibration must contain exactly low/center/high")
    for role in ("low", "center", "high"):
        _require_number(calibration[role], f"metadata.calibration.{role}")
    if not calibration["low"] <= calibration["center"] <= calibration["high"]:
        raise PromotionError("Vega calibration offsets must remain low <= center <= high")
    containment = metadata.get("calibration_containment")
    containment_keys = {
        "target_max_outside_rate",
        "validation_point_outside_raw_rate_before",
        "validation_point_outside_raw_rate_after",
        "low_point_containment_margin",
        "high_point_containment_margin",
    }
    if not isinstance(containment, dict) or set(containment) != containment_keys:
        raise PromotionError("Vega calibration_containment is missing or unexpected")
    target = _require_number(
        containment.get("target_max_outside_rate"),
        "metadata.calibration_containment.target_max_outside_rate",
        minimum=0,
        maximum=MAX_POINT_OUTSIDE_RATE,
    )
    if not math.isclose(
        target, VALIDATION_POINT_OUTSIDE_TARGET, rel_tol=0, abs_tol=1e-12
    ):
        raise PromotionError("Vega calibration containment target is unsupported")
    _require_number(
        containment.get("validation_point_outside_raw_rate_before"),
        "metadata.calibration_containment.validation_point_outside_raw_rate_before",
        minimum=0,
        maximum=1,
    )
    after = _require_number(
        containment.get("validation_point_outside_raw_rate_after"),
        "metadata.calibration_containment.validation_point_outside_raw_rate_after",
        minimum=0,
        maximum=1,
    )
    if after > target:
        raise PromotionError("Vega validation calibration misses its containment target")
    for role in ("low_point_containment_margin", "high_point_containment_margin"):
        _require_number(
            containment.get(role),
            f"metadata.calibration_containment.{role}",
            minimum=0,
        )
    _require_number(
        metadata.get("premium_scale"),
        "metadata.premium_scale",
        minimum=1e-12,
        maximum=100,
    )
    if metadata.get("target") != "log_premium_over_reference_without_keychain_component":
        raise PromotionError("Vega target contract is unexpected")
    if not isinstance(metadata.get("seed_encoder_counts"), dict):
        raise PromotionError("Vega seed_encoder_counts must be an object")
    if not isinstance(metadata.get("runtime_history"), dict):
        raise PromotionError("Vega runtime_history must be an object")
    return config


def _inspect_model(path: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    _require_regular_file(path, maximum_size=MAX_MODEL_BYTES)
    try:
        state = torch.load(path, map_location="cpu", weights_only=True)
    except Exception as exc:  # PyTorch exposes several serialization exceptions.
        raise PromotionError("Vega weights_only state dictionary cannot be loaded") from exc
    if not isinstance(state, Mapping) or not state:
        raise PromotionError("Vega .pt candidate must contain a non-empty state dict")
    total_elements = 0
    for key, value in state.items():
        if not isinstance(key, str) or not torch.is_tensor(value):
            raise PromotionError("Vega state dictionary contains a non-tensor entry")
        if value.layout != torch.strided or value.device.type != "cpu":
            raise PromotionError("Vega state tensor has an unsupported layout or device")
        if value.dtype != torch.float32:
            raise PromotionError("Vega production state tensors must be float32")
        total_elements += value.numel()
        if total_elements > MAX_MODEL_PARAMETERS:
            raise PromotionError("Vega state dictionary exceeds the parameter limit")
        if not bool(torch.isfinite(value).all()):
            raise PromotionError("Vega state dictionary contains non-finite values")
    try:
        model = VegaPriceNet(**dict(config))
        model.load_state_dict(state, strict=True)
        model.float().eval()
    except Exception as exc:
        raise PromotionError("Vega state dict and model_config are incoherent") from exc
    parameters = sum(parameter.numel() for parameter in model.parameters())
    if parameters <= 0 or parameters > MAX_MODEL_PARAMETERS or parameters != total_elements:
        raise PromotionError("Vega state dictionary parameter count is incoherent")
    try:
        with torch.inference_mode():
            output = model(
                torch.zeros((2, len(CAT_FEATURES)), dtype=torch.int64),
                torch.zeros((2, len(NUMERIC_FEATURES)), dtype=torch.float32),
            )
    except Exception as exc:
        raise PromotionError("Vega model cannot execute its CPU smoke prediction") from exc
    if output.shape != (2, 3) or not bool(torch.isfinite(output).all()):
        raise PromotionError("Vega smoke prediction has an invalid shape or value")
    if not bool(((output[:, 0] <= output[:, 1]) & (output[:, 1] <= output[:, 2])).all()):
        raise PromotionError("Vega architecture violated ordered q15/q50/q85 output")
    return {"parameters": parameters, "state_keys": sorted(state)}


def _active_benchmark_identity(project: Path) -> dict[str, Any]:
    """Return an identity cryptographically bound to the active generation."""

    model_name = "vega"
    try:
        vega_pointer = model_generations.read_active_pointer("vega", project_dir=project)
        if vega_pointer is not None:
            model_name = "vega"
            pointer = vega_pointer
        else:
            model_name = "nova"
            pointer = model_generations.read_active_pointer("nova", project_dir=project)
        if pointer is None:
            raise PromotionError(
                "Vega bootstrap requires an immutable active Nova generation"
            )
        legacy = project / model_generations.LEGACY_DIRECTORIES[model_name]
        directory = model_generations.active_or_legacy_bundle_dir(
            model_name,
            legacy,
            project_dir=project,
            verify_files=False,
        )
        manifest = model_generations.verify_generation(
            model_name,
            directory,
            pointer=pointer,
            project_dir=project,
            verify_files=True,
        )
    except PromotionError:
        raise
    except (OSError, model_generations.GenerationError) as exc:
        raise PromotionError(f"active {model_name} benchmark is unsafe: {exc}") from exc

    if model_name == "vega":
        model_filename = MODEL_FILENAME
        metadata_filename = METADATA_FILENAME
        metrics_filename = METRICS_FILENAME
    else:
        model_filename = "nova_price_model.pt"
        metadata_filename = "nova_metadata.json"
        metrics_filename = "nova_metrics.json"
    metadata = _read_json(directory / metadata_filename, f"active {metadata_filename}")
    metrics = _read_json(directory / metrics_filename, f"active {metrics_filename}")
    records = manifest.get("files")
    if not isinstance(records, dict) or not isinstance(records.get(model_filename), dict):
        raise PromotionError("active benchmark manifest has no model identity")
    model_sha256 = _require_sha256(
        records[model_filename].get("sha256"), "active benchmark model SHA-256"
    )
    for label, document in (("metadata", metadata), ("metrics", metrics)):
        if document.get("model_name") != model_name:
            raise PromotionError(f"active benchmark {label} has the wrong model_name")
        if document.get("item_kind") not in {None, ITEM_KIND_WEAPON_SKIN}:
            raise PromotionError(f"active benchmark {label} has the wrong item_kind")
        if document.get("trained_at") != metadata.get("trained_at"):
            raise PromotionError("active benchmark documents have mixed trained_at values")
        declared_sha = document.get("model_sha256")
        if declared_sha is not None and (
            _require_sha256(declared_sha, f"active benchmark {label}.model_sha256")
            != model_sha256
        ):
            raise PromotionError("active benchmark model SHA-256 is incoherent")
    _require_timestamp(metadata.get("trained_at"), "active benchmark trained_at")
    return {
        "model_name": model_name,
        "active_generation_id": pointer["generation_id"],
        "model_sha256": model_sha256,
        "trained_at": metadata["trained_at"],
    }


def _validate_benchmark_binding(
    metrics: Mapping[str, Any], expected: Mapping[str, Any]
) -> dict[str, Any]:
    declared = metrics.get("bootstrap_benchmark")
    if not isinstance(declared, dict) or set(declared) != BENCHMARK_IDENTITY_KEYS:
        raise PromotionError("metrics.bootstrap_benchmark has an invalid identity")
    _require_sha256(declared.get("model_sha256"), "bootstrap benchmark model_sha256")
    _require_sha256(
        declared.get("active_generation_id"),
        "bootstrap benchmark active_generation_id",
    )
    _require_timestamp(declared.get("trained_at"), "bootstrap benchmark trained_at")
    if not _same(declared, expected):
        raise PromotionError(
            "Vega candidate was not evaluated against the exact active benchmark"
        )
    comparison = metrics.get("shared_holdout_comparison")
    if not isinstance(comparison, dict):
        raise PromotionError("Vega candidate has no shared_holdout_comparison")
    comparison_identity = {
        "model_name": comparison.get("production_model_name"),
        "active_generation_id": comparison.get("production_active_generation_id"),
        "model_sha256": comparison.get("production_model_sha256"),
        "trained_at": comparison.get("production_trained_at"),
    }
    if not _same(comparison_identity, expected):
        raise PromotionError(
            "shared holdout is not bound to the exact active benchmark"
        )
    return dict(expected)


def _validate_quality(metrics: Mapping[str, Any]) -> dict[str, Any]:
    comparison = metrics.get("shared_holdout_comparison")
    if not isinstance(comparison, dict):
        raise PromotionError("Vega candidate has no shared_holdout_comparison")
    proof = comparison.get("staging_proves_primary_improvement")
    if proof is False:
        raise CandidateQualityError(
            "shared holdout does not prove all primary improvements"
        )
    if proof is not True:
        raise PromotionError("shared holdout improvement proof is missing or invalid")
    holdout_rows = _require_int(
        comparison.get("holdout_rows"),
        "shared_holdout_comparison.holdout_rows",
        minimum=1,
    )
    test_rows = _require_int(metrics.get("test_rows"), "metrics.test_rows", minimum=1)
    if holdout_rows != test_rows:
        raise PromotionError("shared holdout row count differs from metrics.test_rows")
    if holdout_rows < MIN_QUALITY_HOLDOUT_ROWS:
        raise CandidateQualityError(
            f"Vega shared holdout must contain at least {MIN_QUALITY_HOLDOUT_ROWS} rows"
        )

    production = comparison.get("production")
    staging = comparison.get("staging")
    deltas = comparison.get("staging_minus_production")
    test = metrics.get("test")
    if not all(isinstance(value, dict) for value in (production, staging, deltas, test)):
        raise PromotionError("Vega shared holdout metric objects are incomplete")
    if set(deltas) != set(COMPARISON_METRICS):
        raise PromotionError("Vega shared holdout delta metric set is unexpected")
    observed: dict[str, dict[str, float]] = {}
    for key in COMPARISON_METRICS:
        old = _require_number(production.get(key), f"shared production.{key}", minimum=0)
        new = _require_number(staging.get(key), f"shared staging.{key}", minimum=0)
        delta = _require_number(deltas.get(key), f"shared delta.{key}")
        test_value = _require_number(test.get(key), f"metrics.test.{key}", minimum=0)
        if not math.isclose(delta, new - old, rel_tol=1e-9, abs_tol=1e-12):
            raise PromotionError(f"shared holdout delta for {key} is inconsistent")
        if not math.isclose(test_value, new, rel_tol=1e-9, abs_tol=1e-12):
            raise PromotionError(f"metrics.test.{key} differs from shared staging")
        if key in PRIMARY_METRICS and not new < old:
            raise CandidateQualityError(f"shared holdout {key} is not strictly better")
        observed[key] = {"production": old, "staging": new, "delta": delta}

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
    comparison_coverage = _require_number(
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
    _require_number(
        comparison.get("candidate_row_win_percent"),
        "shared_holdout_comparison.candidate_row_win_percent",
        minimum=0,
        maximum=100,
    )
    if not math.isclose(comparison_coverage, raw_coverage, rel_tol=0, abs_tol=1e-12):
        raise PromotionError("shared-holdout coverage differs from raw test coverage")
    if coverage < MIN_INTERVAL_COVERAGE:
        raise CandidateQualityError(
            f"Vega interval coverage must be at least {MIN_INTERVAL_COVERAGE}"
        )
    if raw_coverage < MIN_INTERVAL_COVERAGE:
        raise CandidateQualityError(
            f"Vega raw interval coverage must be at least {MIN_INTERVAL_COVERAGE}"
        )
    if crossing > MAX_QUANTILE_CROSSING_RATE:
        raise CandidateQualityError("Vega quantile crossing rate exceeds 0.01")
    if point_outside > MAX_POINT_OUTSIDE_RATE:
        raise CandidateQualityError("Vega point-outside-interval rate exceeds 0.01")
    return {
        "holdout_rows": holdout_rows,
        "strictly_improved_metrics": list(PRIMARY_METRICS),
        "observed_metrics": observed,
        "interval_coverage": coverage,
        "raw_interval_coverage": raw_coverage,
        "quantile_crossing_rate": crossing,
        "point_outside_raw_interval_rate": point_outside,
    }


def _validate_documents(
    metadata: dict[str, Any],
    metrics: dict[str, Any],
    *,
    model_sha256: str,
    benchmark_identity: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    _validate_document_identity(metadata, "metadata")
    _validate_document_identity(metrics, "metrics")
    trained_at = _require_matching(metadata, metrics, "trained_at")
    for key in (
        "architecture",
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
        "calibration_containment",
        "premium_scale",
        "test_interval_coverage_15_85",
        "model_sha256",
    ):
        _require_matching(metadata, metrics, key)
    if not _same(metadata.get("test_metrics"), metrics.get("test")):
        raise PromotionError(
            "mixed Vega candidate bundle: metadata.test_metrics differs from metrics.test"
        )
    if metadata.get("architecture") != ARCHITECTURE_ID:
        raise PromotionError("Vega metadata architecture is unsupported")
    if metrics.get("architecture") != ARCHITECTURE_ID:
        raise PromotionError("Vega metrics architecture is unsupported")
    if metadata.get("feature_contract_version") != FEATURE_CONTRACT_VERSION:
        raise PromotionError("Vega feature contract version differs from runtime code")
    if metadata.get("feature_contract_sha256") != FEATURE_CONTRACT_SHA256:
        raise PromotionError("Vega feature contract SHA-256 differs from runtime code")
    _require_sha256(metadata.get("feature_contract_sha256"), "feature_contract_sha256")
    _require_sha256(metadata.get("cache_sha256"), "cache_sha256")
    declared_model_sha = _require_sha256(metadata.get("model_sha256"), "model_sha256")
    if declared_model_sha != model_sha256:
        raise PromotionError("Vega model bytes differ from metadata/metrics SHA-256")

    source = metadata.get("source_database")
    if not isinstance(source, dict) or not source:
        raise PromotionError("Vega source_database must be a non-empty object")
    if source.get("item_kind") != ITEM_KIND_WEAPON_SKIN:
        raise PromotionError("Vega source_database belongs to another item kind")
    if source.get("item_kind_policy") != STRICT_ITEM_KIND_POLICY:
        raise PromotionError("Vega source_database is not strictly item-kind isolated")
    _require_sha256(source.get("logical_sha256"), "source_database.logical_sha256")
    rows = _require_int(metadata.get("rows"), "metadata.rows", minimum=1)
    fit_rows = _require_int(metadata.get("fit_train_rows"), "metadata.fit_train_rows", minimum=1)
    if source.get("valid_price_rows") != rows:
        raise PromotionError("Vega rows differ from source_database.valid_price_rows")
    _require_int(metadata.get("cache_schema_version"), "cache_schema_version", minimum=1)
    exclusions = metadata.get("row_exclusions")
    if not isinstance(exclusions, dict):
        raise PromotionError("Vega row_exclusions must be an object")
    if exclusions.get("source_rows") not in {None, rows}:
        raise PromotionError("Vega row_exclusions.source_rows differs from rows")
    split = metadata.get("split")
    if not isinstance(split, dict) or not split:
        raise PromotionError("Vega split provenance must be a non-empty object")
    if split.get("method") != "fixed_newest_rows_with_timestamp_ties":
        raise PromotionError("Vega split method is unsupported")
    if split.get("test_untouched_until_final_evaluation") is not True:
        raise PromotionError("Vega test split was not kept untouched")
    if split.get("test_rows") != metrics.get("test_rows"):
        raise PromotionError("Vega split test_rows differs from metrics.test_rows")
    if split.get("train_rows") != fit_rows or metrics.get("train_rows") != fit_rows:
        raise PromotionError("Vega fit/train row counts differ")
    if split.get("validation_rows") != metrics.get("validation_rows"):
        raise PromotionError("Vega validation row counts differ")

    if metrics.get("device") != "cuda":
        raise PromotionError("Vega production candidate was not trained on CUDA")
    if not isinstance(metrics.get("device_name"), str) or not metrics["device_name"].strip():
        raise PromotionError("Vega CUDA device identity is missing")
    if metrics.get("mixed_precision_training") is not True:
        raise PromotionError("Vega mixed-precision training evidence is missing")
    if metrics.get("training_autocast_dtype") != "float16":
        raise PromotionError("Vega training autocast must be float16")
    if metrics.get("quality_evaluation_dtype") != "float32":
        raise PromotionError("Vega quality gate must run in float32")
    if metrics.get("quality_evaluation_tf32") is not False:
        raise PromotionError("Vega quality gate must disable TF32")

    _validate_benchmark_binding(metrics, benchmark_identity)
    if _require_timestamp(trained_at, "trained_at") <= _require_timestamp(
        benchmark_identity.get("trained_at"), "active benchmark trained_at"
    ):
        raise PromotionError("Vega candidate must be newer than its active benchmark")
    quality = _validate_quality(metrics)
    return _validate_metadata(metadata), quality


def validate_candidate(
    candidate_dir: str | Path | None = None,
    *,
    project_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Validate a complete staging candidate without writing any files."""

    project = _absolute(project_dir or PROJECT_DIR)
    candidate = _absolute(candidate_dir or DEFAULT_CANDIDATE_DIR)
    _require_directory(candidate, "candidate directory")
    _require_exact_bundle(candidate)
    identities: dict[str, tuple[int, int, int, int, int]] = {}
    files: dict[str, dict[str, Any]] = {}
    for filename in BUNDLE_FILES:
        path = candidate / filename
        maximum = MAX_MODEL_BYTES if filename == MODEL_FILENAME else MAX_JSON_BYTES
        size = _require_regular_file(path, maximum_size=maximum)
        identities[filename] = _file_identity(path)
        files[filename] = {"sha256": _sha256_file(path), "size_bytes": size}

    benchmark_identity = _active_benchmark_identity(project)
    metadata = _read_json(candidate / METADATA_FILENAME, METADATA_FILENAME)
    metrics = _read_json(candidate / METRICS_FILENAME, METRICS_FILENAME)
    config, quality = _validate_documents(
        metadata,
        metrics,
        model_sha256=files[MODEL_FILENAME]["sha256"],
        benchmark_identity=benchmark_identity,
    )
    inspection = _inspect_model(candidate / MODEL_FILENAME, config)
    parameters = _require_int(metrics.get("parameters"), "metrics.parameters", minimum=1)
    if inspection["parameters"] != parameters:
        raise PromotionError("Vega state parameter count differs from metrics")

    _require_exact_bundle(candidate)
    for filename, expected in identities.items():
        path = candidate / filename
        if path.is_symlink() or _file_identity(path) != expected:
            raise PromotionError(
                f"Vega candidate changed while it was validated: {filename}"
            )
    current_benchmark = _active_benchmark_identity(project)
    if not _same(current_benchmark, benchmark_identity):
        raise PromotionError("active benchmark changed while Vega was validated")

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifact_version": EXPECTED_ARTIFACT_VERSION,
        "model_name": "vega",
        "item_kind": ITEM_KIND_WEAPON_SKIN,
        "architecture": ARCHITECTURE_ID,
        "stage": "staging",
        "trained_at": metadata["trained_at"],
        "generated_at": _utc_now(),
        "candidate_directory": str(candidate),
        "feature_contract_sha256": FEATURE_CONTRACT_SHA256,
        "parameter_count": parameters,
        "benchmark": benchmark_identity,
        "quality_gate": quality,
        "files": files,
    }
    manifest["bundle_sha256"] = _canonical_sha256(files)
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate without writing")
    parser.add_argument("--candidate-dir", type=Path, default=DEFAULT_CANDIDATE_DIR)
    args = parser.parse_args(argv)
    if not args.check:
        parser.error("Vega activation is generation-based; use --check here")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = validate_candidate(args.candidate_dir)
    except PromotionError as exc:
        raise SystemExit(f"Vega candidate error: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
