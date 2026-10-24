import hashlib
import inspect
import os
import stat
import threading
from pathlib import Path

import pytest

import model_generations as generations


def _symlink_or_skip(link: Path, target, *, target_is_directory=False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is not enabled")
        raise


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate(tmp_path: Path, model: str, job_id: str, *, marker: str = "new"):
    project = tmp_path
    project.mkdir(parents=True, exist_ok=True)
    candidate = project / generations.STAGING_ROOTS[model] / job_id
    candidate.mkdir(parents=True)
    records = {}
    for index, filename in enumerate(generations.MODEL_FILES[model]):
        path = candidate / filename
        path.write_bytes(f"{marker}-{model}-{index}".encode())
        records[filename] = {
            "sha256": _sha(path),
            "size_bytes": path.stat().st_size,
        }
    bundle_sha = generations._bundle_sha256(records)
    manifest = {
        "bundle_sha256": bundle_sha,
        "files": records,
        "quality_gate": {"strictly_improved_metrics": ["primary"]},
    }
    manifest["item_kind"] = generations.MODEL_ITEM_KINDS[model]
    return candidate, manifest


def _legacy(project: Path, model: str, *, marker: str = "old") -> Path:
    directory = project / generations.LEGACY_DIRECTORIES[model]
    directory.mkdir(parents=True, exist_ok=True)
    for index, filename in enumerate(generations.MODEL_FILES[model]):
        (directory / filename).write_bytes(f"{marker}-{model}-{index}".encode())
    return directory


def test_public_publish_api_has_no_quality_or_smoke_bypass(monkeypatch, tmp_path):
    parameters = inspect.signature(generations.publish_candidate).parameters
    assert "validator" not in parameters
    assert "load_and_smoke" not in parameters
    model = "almaz"
    job_id = "0" * 32
    _legacy(tmp_path, model)
    candidate, manifest = _candidate(tmp_path, model, job_id)
    calls = []
    monkeypatch.setattr(
        generations,
        "_default_validate",
        lambda selected, directory: calls.append(("validate", selected, directory))
        or manifest,
    )
    monkeypatch.setattr(
        generations,
        "_default_load_and_smoke",
        lambda selected, directory: calls.append(("smoke", selected, directory)),
    )

    result = generations.publish_candidate(
        model,
        candidate,
        manifest=manifest,
        job_id=job_id,
        project_dir=tmp_path,
    )

    assert result["activation_state"] == "activated"
    assert [value[0] for value in calls] == ["validate", "smoke"]


@pytest.mark.parametrize("model", generations.MODEL_FILES)
def test_publish_creates_verified_immutable_generation_and_atomic_pointer(
    tmp_path, model
):
    job_id = "a" * 32
    legacy = _legacy(tmp_path, model)
    before = {
        filename: (legacy / filename).read_bytes()
        for filename in generations.MODEL_FILES[model]
    }
    candidate, manifest = _candidate(tmp_path, model, job_id)

    result = generations._publish_candidate(
        model,
        candidate,
        manifest=manifest,
        job_id=job_id,
        project_dir=tmp_path,
        validator=lambda _model, _candidate: manifest,
        load_and_smoke=lambda _model, directory: directory,
    )

    pointer = generations.read_active_pointer(model, project_dir=tmp_path)
    assert result["activation_state"] == "activated"
    assert result["active_bundle_version"] == manifest["bundle_sha256"]
    assert pointer["generation_id"] == manifest["bundle_sha256"]
    generation_dir = generations.active_or_legacy_bundle_dir(
        model, legacy, project_dir=tmp_path
    )
    assert generation_dir.name == manifest["bundle_sha256"]
    if os.name != "nt":
        assert stat.S_IMODE(generation_dir.stat().st_mode) == 0o500
        assert all(
            stat.S_IMODE((generation_dir / filename).stat().st_mode) == 0o400
            for filename in generations.MODEL_FILES[model]
        )
    assert before == {
        filename: (legacy / filename).read_bytes()
        for filename in generations.MODEL_FILES[model]
    }


@pytest.mark.parametrize("model", generations.MODEL_ITEM_KINDS)
def test_verify_generation_infers_only_missing_legacy_item_kind(tmp_path, model):
    job_id = "1" * 32
    _legacy(tmp_path, model)
    candidate, manifest = _candidate(tmp_path, model, job_id)
    result = generations._publish_candidate(
        model,
        candidate,
        manifest=manifest,
        job_id=job_id,
        project_dir=tmp_path,
        validator=lambda _model, _candidate: manifest,
        load_and_smoke=lambda _model, directory: directory,
    )
    generation_dir = (
        tmp_path
        / ".model_generations"
        / model
        / "versions"
        / result["active_bundle_version"]
    )
    manifest_path = generation_dir / generations.GENERATION_MANIFEST
    document = generations._read_json(manifest_path)
    document.pop("item_kind")
    generation_dir.chmod(0o700)
    manifest_path.chmod(0o600)
    generations._atomic_json(manifest_path, document)
    manifest_path.chmod(0o400)
    generation_dir.chmod(0o500)

    verified = generations.verify_generation(
        model,
        generation_dir,
        project_dir=tmp_path,
    )

    assert verified["item_kind"] == generations.MODEL_ITEM_KINDS[model]
    assert (
        verified["item_kind_compatibility"]
        == "inferred_from_fixed_model_id"
    )

    generation_dir.chmod(0o700)
    manifest_path.chmod(0o600)
    document["item_kind"] = (
        "knife" if generations.MODEL_ITEM_KINDS[model] == "weapon_skin" else "weapon_skin"
    )
    generations._atomic_json(manifest_path, document)
    manifest_path.chmod(0o400)
    generation_dir.chmod(0o500)
    with pytest.raises(generations.GenerationError, match="wrong item kind"):
        generations.verify_generation(model, generation_dir, project_dir=tmp_path)


def test_publish_revalidates_and_smokes_before_pointer_switch(tmp_path):
    model = "almaz"
    job_id = "b" * 32
    legacy = _legacy(tmp_path, model)
    candidate, manifest = _candidate(tmp_path, model, job_id)

    with pytest.raises(generations.GenerationError, match="changed"):
        generations._publish_candidate(
            model,
            candidate,
            manifest=manifest,
            job_id=job_id,
            project_dir=tmp_path,
            validator=lambda _model, _candidate: {
                **manifest,
                "bundle_sha256": "0" * 64,
            },
            load_and_smoke=lambda *_args: None,
        )
    assert generations.read_active_pointer(model, project_dir=tmp_path) is None
    assert generations.active_or_legacy_bundle_dir(
        model, legacy, project_dir=tmp_path
    ) == legacy

    with pytest.raises(generations.GenerationActivationError, match="smoke-tested"):
        generations._publish_candidate(
            model,
            candidate,
            manifest=manifest,
            job_id=job_id,
            project_dir=tmp_path,
            validator=lambda _model, _candidate: manifest,
            load_and_smoke=lambda *_args: (_ for _ in ()).throw(RuntimeError("bad")),
        )
    assert generations.read_active_pointer(model, project_dir=tmp_path) is None


def test_cancellation_after_pointer_write_rolls_back_to_legacy(tmp_path):
    model = "nova"
    job_id = "c" * 32
    _legacy(tmp_path, model)
    candidate, manifest = _candidate(tmp_path, model, job_id)
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 5

    with pytest.raises(generations.GenerationCancelled, match="rolled back"):
        generations._publish_candidate(
            model,
            candidate,
            manifest=manifest,
            job_id=job_id,
            project_dir=tmp_path,
            validator=lambda _model, _candidate: manifest,
            load_and_smoke=lambda *_args: None,
            cancel_check=cancelled,
        )

    assert generations.read_active_pointer(model, project_dir=tmp_path) is None
    failure = generations.get_activation_status(model, project_dir=tmp_path)[
        "last_activation_failure"
    ]
    assert failure["rolled_back_to"] == "legacy"


def test_runtime_slot_swaps_one_reference_and_old_request_finishes_old(tmp_path):
    model = "almaz"
    job_id = "d" * 32
    legacy = _legacy(tmp_path, model, marker="old")
    candidate, manifest = _candidate(tmp_path, model, job_id, marker="new")
    started = threading.Event()
    release = threading.Event()

    def load(directory):
        value = (directory / generations.MODEL_FILES[model][0]).read_text()
        if value.startswith("new"):
            started.set()
            assert release.wait(3)
        return {"value": value}

    slot = generations.RuntimeBundleSlot(
        model,
        legacy,
        load_bundle=load,
        smoke_bundle=lambda bundle: None,
        project_dir=tmp_path,
    )
    old_request_bundle = slot.get()
    generations._publish_candidate(
        model,
        candidate,
        manifest=manifest,
        job_id=job_id,
        project_dir=tmp_path,
        validator=lambda _model, _candidate: manifest,
        load_and_smoke=lambda *_args: None,
    )
    loaded = []
    thread = threading.Thread(target=lambda: loaded.append(slot.get()))
    thread.start()
    assert started.wait(3)
    assert old_request_bundle["value"].startswith("old")
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert loaded[0]["value"].startswith("new")
    assert old_request_bundle["value"].startswith("old")


def test_runtime_load_failure_cas_rolls_pointer_back_and_keeps_old_object(tmp_path):
    model = "almaz"
    job_id = "e" * 32
    legacy = _legacy(tmp_path, model, marker="old")
    candidate, manifest = _candidate(tmp_path, model, job_id, marker="broken")

    def load(directory):
        return {"value": (directory / generations.MODEL_FILES[model][0]).read_text()}

    def smoke(bundle):
        if bundle["value"].startswith("broken"):
            raise RuntimeError("synthetic smoke failure")

    slot = generations.RuntimeBundleSlot(
        model,
        legacy,
        load_bundle=load,
        smoke_bundle=smoke,
        project_dir=tmp_path,
    )
    old = slot.get()
    generations._publish_candidate(
        model,
        candidate,
        manifest=manifest,
        job_id=job_id,
        project_dir=tmp_path,
        validator=lambda _model, _candidate: manifest,
        load_and_smoke=lambda *_args: None,
    )

    assert slot.get() is old
    assert generations.read_active_pointer(model, project_dir=tmp_path) is None
    assert "synthetic smoke failure" in generations.get_activation_status(
        model, project_dir=tmp_path
    )["last_activation_failure"]["error"]


def test_pointer_and_candidate_symlinks_fail_closed(tmp_path):
    model = "almaz"
    job_id = "f" * 32
    _legacy(tmp_path, model)
    real_candidate, manifest = _candidate(tmp_path, model, job_id)
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    bad_job = "1" * 32
    _symlink_or_skip(
        tmp_path / generations.STAGING_ROOTS[model] / bad_job,
        redirected,
        target_is_directory=True,
    )
    with pytest.raises(generations.GenerationError, match="symlink"):
        generations._publish_candidate(
            model,
            tmp_path / generations.STAGING_ROOTS[model] / bad_job,
            manifest=manifest,
            job_id=bad_job,
            project_dir=tmp_path,
            validator=lambda *_args: manifest,
            load_and_smoke=lambda *_args: None,
        )

    generations._publish_candidate(
        model,
        real_candidate,
        manifest=manifest,
        job_id=job_id,
        project_dir=tmp_path,
        validator=lambda *_args: manifest,
        load_and_smoke=lambda *_args: None,
    )
    pointer = tmp_path / ".model_generations" / model / generations.ACTIVE_POINTER
    pointer.unlink()
    _symlink_or_skip(pointer, tmp_path / "outside.json")
    with pytest.raises(generations.GenerationError):
        generations.read_active_pointer(model, project_dir=tmp_path)


def test_generation_retention_keeps_only_active_and_rollback_predecessor(tmp_path):
    model = "almaz"
    _legacy(tmp_path, model)
    generation_ids = []
    for digit in ("1", "2", "3"):
        job_id = digit * 32
        candidate, manifest = _candidate(
            tmp_path, model, job_id, marker=f"generation-{digit}"
        )
        generations._publish_candidate(
            model,
            candidate,
            manifest=manifest,
            job_id=job_id,
            project_dir=tmp_path,
            validator=lambda _model, _candidate, value=manifest: value,
            load_and_smoke=lambda *_args: None,
        )
        generation_ids.append(manifest["bundle_sha256"])

    versions = tmp_path / ".model_generations" / model / "versions"
    retained = {entry.name for entry in versions.iterdir() if entry.is_dir()}
    pointer = generations.read_active_pointer(model, project_dir=tmp_path)

    assert generation_ids[0] not in retained
    assert retained == {generation_ids[1], generation_ids[2]}
    assert pointer["generation_id"] == generation_ids[2]
    assert pointer["previous"]["generation_id"] == generation_ids[1]
