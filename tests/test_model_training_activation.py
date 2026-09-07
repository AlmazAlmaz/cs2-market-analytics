import json
import os
import stat
from pathlib import Path

import pytest

import model_candidate_promotion
import model_training_activation as activation

JOB_ID = "a" * 32
SHA = "b" * 64


@pytest.fixture(autouse=True)
def _configured_project(tmp_path, monkeypatch):
    monkeypatch.setattr(activation.app_config, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(model_candidate_promotion, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(
        model_candidate_promotion,
        "installed_candidate_bundle_sha256",
        lambda _model: None,
    )


def _candidate(project: Path, model: str = "almaz") -> Path:
    roots = {
        "almaz": project / "models" / "staging" / "training_jobs",
        "igor": project / "IGORMODEL" / "staging" / "training_jobs",
        "nova": project / "NEURALMODEL" / "staging" / "training_jobs",
    }
    path = roots[model] / JOB_ID
    path.mkdir(parents=True)
    return path


def _manifest():
    return {
        "bundle_sha256": SHA,
        "quality_gate": {"holdout_rows": 5_000},
    }


def test_manifest_validation_preserves_quality_vs_integrity_type(
    tmp_path, monkeypatch
):
    candidate = _candidate(tmp_path)
    monkeypatch.setattr(
        model_candidate_promotion,
        "validate_candidate",
        lambda *_args: (_ for _ in ()).throw(
            model_candidate_promotion.CandidateQualityError("worse holdout")
        ),
    )
    with pytest.raises(
        activation.CandidateActivationQualityError,
        match="quality gate",
    ):
        activation._validate_manifest("almaz", candidate)

    monkeypatch.setattr(
        model_candidate_promotion,
        "validate_candidate",
        lambda *_args: (_ for _ in ()).throw(
            model_candidate_promotion.CandidateIntegrityError("bad SHA")
        ),
    )
    with pytest.raises(
        activation.CandidateActivationIntegrityError,
        match="integrity check",
    ):
        activation._validate_manifest("almaz", candidate)


def test_schedule_uses_private_atomic_record_and_public_relative_path(
    tmp_path, monkeypatch
):
    candidate = _candidate(tmp_path)
    monkeypatch.setattr(activation, "_validate_manifest", lambda *_args: _manifest())

    record = activation.schedule_pending_activation(
        "almaz", JOB_ID, candidate, project_dir=tmp_path
    )

    path = tmp_path / ".run" / "model_training" / "pending" / "almaz.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not Path(record["candidate_dir"]).is_absolute()
    assert record["candidate_bundle_sha256"] == SHA
    assert json.loads(path.read_text(encoding="utf-8")) == record


def test_schedule_rejects_path_outside_managed_job_root(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(activation, "_validate_manifest", lambda *_args: _manifest())

    with pytest.raises(activation.ActivationError, match="managed training root"):
        activation.schedule_pending_activation(
            "almaz", JOB_ID, outside, project_dir=tmp_path
        )


def test_activate_revalidates_hash_promotes_and_removes_pending(tmp_path, monkeypatch):
    candidate = _candidate(tmp_path)
    monkeypatch.setattr(activation, "_validate_manifest", lambda *_args: _manifest())
    activation.schedule_pending_activation(
        "almaz", JOB_ID, candidate, project_dir=tmp_path
    )
    calls = []
    monkeypatch.setattr(
        model_candidate_promotion,
        "find_live_service_processes",
        list,
    )
    monkeypatch.setattr(
        model_candidate_promotion,
        "promote_candidate",
        lambda model, path, offline_confirm: calls.append(
            (model, Path(path), offline_confirm)
        )
        or {"status": "promoted"},
    )

    result = activation.activate_pending(offline_confirm=True, project_dir=tmp_path)

    assert result["count"] == 1
    assert calls == [("almaz", candidate, True)]
    assert not (
        tmp_path / ".run" / "model_training" / "pending" / "almaz.json"
    ).exists()


def test_activate_refuses_changed_candidate_hash(tmp_path, monkeypatch):
    candidate = _candidate(tmp_path)
    manifests = iter([_manifest(), _manifest(), {**_manifest(), "bundle_sha256": "c" * 64}])
    monkeypatch.setattr(activation, "_validate_manifest", lambda *_args: next(manifests))
    monkeypatch.setattr(model_candidate_promotion, "find_live_service_processes", list)
    activation.schedule_pending_activation(
        "almaz", JOB_ID, candidate, project_dir=tmp_path
    )

    with pytest.raises(activation.ActivationError, match="bytes have changed"):
        activation.activate_pending(offline_confirm=True, project_dir=tmp_path)


def test_activate_requires_explicit_offline_confirmation(tmp_path):
    with pytest.raises(activation.ActivationError, match="offline-confirm"):
        activation.activate_pending(project_dir=tmp_path)


def test_activate_clears_stale_pending_after_already_installed_receipt(
    tmp_path, monkeypatch
):
    candidate = _candidate(tmp_path)
    monkeypatch.setattr(activation, "_validate_manifest", lambda *_args: _manifest())
    activation.schedule_pending_activation(
        "almaz", JOB_ID, candidate, project_dir=tmp_path
    )
    monkeypatch.setattr(
        model_candidate_promotion,
        "installed_candidate_bundle_sha256",
        lambda _model: SHA,
    )
    monkeypatch.setattr(
        model_candidate_promotion,
        "find_live_service_processes",
        list,
    )
    monkeypatch.setattr(
        model_candidate_promotion,
        "promote_candidate",
        lambda *_args, **_kwargs: pytest.fail("candidate must not be promoted twice"),
    )

    result = activation.activate_pending(
        offline_confirm=True, project_dir=tmp_path
    )

    assert result["activated"]["almaz"]["status"] == "already_promoted"
    assert result["count"] == 1
    assert not (
        tmp_path / ".run" / "model_training" / "pending" / "almaz.json"
    ).exists()


def test_activate_rejects_an_alternate_project_profile(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(activation.ActivationError, match="configured production"):
        activation.activate_pending(
            offline_confirm=True, project_dir=other
        )


def test_activate_dispatches_igor_crash_journal_recovery(tmp_path, monkeypatch):
    from IGORMODEL import promote_candidate as igor_promotion

    backup = tmp_path / "IGORMODEL" / "backups" / "igor_test"
    written = activation.model_lifecycle.write_promotion_journal(
        {
            "kind": "igor_directory_v1",
            "model": "igor",
            "backup_directory": str(backup),
        },
        project_dir=tmp_path,
    )
    calls = []

    def recover():
        calls.append("igor")
        activation.model_lifecycle.clear_promotion_journal(
            expected_model="igor",
            expected_backup=written["backup_directory"],
            project_dir=tmp_path,
        )
        return {"status": "recovered"}

    monkeypatch.setattr(igor_promotion, "recover_interrupted_promotion", recover)

    result = activation.activate_pending(
        offline_confirm=True, project_dir=tmp_path
    )

    assert calls == ["igor"]
    assert result == {"status": "complete", "activated": {}, "count": 0}


def test_schedule_rechecks_cancellation_inside_activation_lock(
    tmp_path, monkeypatch
):
    candidate = _candidate(tmp_path)
    monkeypatch.setattr(activation, "_validate_manifest", lambda *_args: _manifest())
    checks = iter((False, True))

    with pytest.raises(activation.ActivationError, match="cancelled"):
        activation.schedule_pending_activation(
            "almaz",
            JOB_ID,
            candidate,
            project_dir=tmp_path,
            cancel_check=lambda: next(checks),
        )

    assert not (
        tmp_path / ".run" / "model_training" / "pending" / "almaz.json"
    ).exists()


def test_contended_activation_locks_do_not_leak_file_descriptors(tmp_path):
    before = len(os.listdir("/dev/fd"))
    with activation._activation_lock(tmp_path):
        for _ in range(20):
            with (
                pytest.raises(activation.ActivationError, match="already running"),
                activation._activation_lock(tmp_path),
            ):
                pass
    resource = tmp_path / ".run" / "model_training" / "resource.lock"
    resource.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(resource, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        activation.fcntl.flock(descriptor, activation.fcntl.LOCK_EX)
        for _ in range(20):
            with (
                pytest.raises(activation.ActivationError, match="still running"),
                activation._training_idle_lock(tmp_path),
            ):
                pass
    finally:
        activation.fcntl.flock(descriptor, activation.fcntl.LOCK_UN)
        os.close(descriptor)
    assert len(os.listdir("/dev/fd")) <= before + 1
