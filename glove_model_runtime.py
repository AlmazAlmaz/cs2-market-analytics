"""Fail-closed runtime adapter for the dedicated glove-only price model.

Gloves must never be routed through the weapon-skin or knife model.  The
adapter deliberately accepts only an explicitly identified glove artifact;
until one is trained and placed in ``GLOVEMODEL/artifacts``, scanner readiness
is false and no CSFloat request is spent on the glove profile.
"""

from __future__ import annotations

import threading
from pathlib import Path

import joblib

import app_config
import model_generations
import price_model_runtime
from item_classification import ITEM_KIND_GLOVE, classify_market_hash_name

MODEL_ID = "gloves"
MODEL_DIR = app_config.PROJECT_DIR / "GLOVEMODEL" / "artifacts"
MODEL_PATH = MODEL_DIR / "glove_price_model.joblib"
METRICS_PATH = MODEL_DIR / "glove_price_model_metrics.json"

_RUNTIME_SLOT = None
_RUNTIME_SLOT_LOCK = threading.Lock()


class GloveModelUnavailable(RuntimeError):
    """A glove cannot be evaluated until its isolated model is available."""


def load_bundle_from_directory(directory: str | Path) -> dict:
    directory = Path(directory)
    path = directory / MODEL_PATH.name
    if path.is_symlink() or not path.is_file():
        raise GloveModelUnavailable(
            "dedicated glove model artifact is unavailable"
        )
    try:
        artifact = joblib.load(path)
    except Exception as exc:  # joblib exposes backend-specific format errors.
        raise GloveModelUnavailable("dedicated glove model artifact is unreadable") from exc
    if (
        not isinstance(artifact, dict)
        or artifact.get("model_name") != MODEL_ID
        or artifact.get("item_kind") != ITEM_KIND_GLOVE
    ):
        raise GloveModelUnavailable("dedicated glove model has the wrong identity")
    artifact["_runtime_source_dir"] = str(directory)
    artifact["_runtime_generation_id"] = (
        directory.name
        if directory.parent.name == "versions"
        and model_generations.SHA256_RE.fullmatch(directory.name)
        else None
    )
    return artifact


def smoke_bundle(artifact: dict) -> None:
    if (
        artifact.get("model_name") != MODEL_ID
        or artifact.get("item_kind") != ITEM_KIND_GLOVE
    ):
        raise GloveModelUnavailable("dedicated glove model identity is invalid")
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
    except (model_generations.GenerationError, GloveModelUnavailable) as exc:
        raise GloveModelUnavailable(
            "glove model is still collecting training data or has not been activated"
        ) from exc


def predict_deal(payload: dict, conn=None) -> dict:
    name = str(
        payload.get("name")
        or price_model_runtime.parse_market_hash_name(
            payload.get("url") or payload.get("text") or ""
        )
        or ""
    ).strip()
    if classify_market_hash_name(name, conn=conn) != ITEM_KIND_GLOVE:
        raise ValueError("the dedicated glove model accepts glove items only")
    result = price_model_runtime.predict_deal_with_artifact(payload, load_model(), conn)
    result["model_name"] = MODEL_ID
    result["item_kind"] = ITEM_KIND_GLOVE
    return result
