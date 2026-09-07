"""Runtime adapter for the dedicated knife-only price model."""

from __future__ import annotations

import threading
from pathlib import Path

import joblib

import app_config
import model_generations
import price_model_runtime
from item_classification import ITEM_KIND_KNIFE, classify_market_hash_name

MODEL_ID = "knives"
MODEL_DIR = app_config.PROJECT_DIR / "KNIFEMODEL" / "artifacts"
MODEL_PATH = MODEL_DIR / "knife_price_model.joblib"
METRICS_PATH = MODEL_DIR / "knife_price_model_metrics.json"

_RUNTIME_SLOT = None
_RUNTIME_SLOT_LOCK = threading.Lock()


class KnifeModelUnavailable(RuntimeError):
    """A knife cannot be evaluated until its isolated model is available."""


def load_bundle_from_directory(directory: str | Path) -> dict:
    directory = Path(directory)
    path = directory / MODEL_PATH.name
    if path.is_symlink() or not path.is_file():
        raise KnifeModelUnavailable("dedicated knife model artifact is unavailable")
    try:
        artifact = joblib.load(path)
    except Exception as exc:  # joblib exposes backend-specific format errors.
        raise KnifeModelUnavailable("dedicated knife model artifact is unreadable") from exc
    if (
        not isinstance(artifact, dict)
        or artifact.get("model_name") != MODEL_ID
        or artifact.get("item_kind") != ITEM_KIND_KNIFE
    ):
        raise KnifeModelUnavailable("dedicated knife model has the wrong identity")
    artifact["_runtime_source_dir"] = str(directory)
    artifact["_runtime_generation_id"] = (
        directory.name
        if directory.parent.name == "versions"
        and model_generations.SHA256_RE.fullmatch(directory.name)
        else None
    )
    return artifact


def smoke_bundle(artifact: dict) -> None:
    if artifact.get("model_name") != MODEL_ID or artifact.get("item_kind") != ITEM_KIND_KNIFE:
        raise KnifeModelUnavailable("dedicated knife model identity is invalid")
    price_model_runtime.smoke_bundle(artifact)


def _runtime_slot():
    global _RUNTIME_SLOT
    if _RUNTIME_SLOT is None:
        with _RUNTIME_SLOT_LOCK:
            if _RUNTIME_SLOT is None:
                _RUNTIME_SLOT = model_generations.RuntimeBundleSlot(
                    MODEL_ID,
                    MODEL_DIR,
                    load_bundle=load_bundle_from_directory,
                    smoke_bundle=smoke_bundle,
                    project_dir=app_config.PROJECT_DIR,
                )
    return _RUNTIME_SLOT


def load_model() -> dict:
    try:
        return _runtime_slot().get()
    except (model_generations.GenerationError, KnifeModelUnavailable) as exc:
        raise KnifeModelUnavailable(
            "knife model is still collecting training data or has not been activated"
        ) from exc


def predict_deal(payload: dict, conn=None) -> dict:
    name = str(
        payload.get("name")
        or price_model_runtime.parse_market_hash_name(
            payload.get("url") or payload.get("text") or ""
        )
        or ""
    ).strip()
    if classify_market_hash_name(name, conn=conn) != ITEM_KIND_KNIFE:
        raise ValueError("the dedicated knife model accepts knife items only")
    result = price_model_runtime.predict_deal_with_artifact(payload, load_model(), conn)
    result["model_name"] = MODEL_ID
    result["item_kind"] = ITEM_KIND_KNIFE
    return result
