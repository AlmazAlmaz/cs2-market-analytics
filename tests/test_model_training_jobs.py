import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
from copy import deepcopy
from pathlib import Path

import pytest

import model_generations
import model_training_jobs as jobs


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def _make_project(tmp_path):
    for relative in (
        "models/staging",
        "IGORMODEL/staging",
        "NEURALMODEL/staging",
        "KNIFEMODEL/staging",
        "GLOVEMODEL/staging",
    ):
        (tmp_path / relative).mkdir(parents=True)
    (tmp_path / "train_price_model.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "IGORMODEL" / "train.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "train_neural_model.py").write_text(
        'NOVA_TRAINING_CONTRACT_VERSION = 2\nPARSER = "--test-rows"\n',
        encoding="utf-8",
    )
    (tmp_path / "train_knife_price_model.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "train_glove_price_model.py").write_text("pass\n", encoding="utf-8")
    return tmp_path


def _install_job(project, model="almaz", *, state="running", pid=None):
    paths = jobs._paths(project)
    job_id = "a" * 32
    spec = jobs._model_spec(
        model,
        paths,
        sys.executable,
        paths.snapshot_dir / f"{job_id}.db",
    )

    def install(payload):
        job = jobs._new_job(model, job_id, spec, project)
        job.update(
            {
                "state": state,
                "worker_pid": pid,
                "worker_start_token": "fake-start" if pid else None,
            }
        )
        payload["models"][model] = job
        payload["active_job_id"] = job_id if state in jobs.ACTIVE_STATES else None

    jobs._mutate_state(paths, install)
    return paths, job_id


def test_persistent_status_is_private_and_hides_process_details(tmp_path):
    project = _make_project(tmp_path)

    overview = jobs.get_training_overview(project_dir=project)

    paths = jobs._paths(project)
    assert overview["training_active"] is False
    assert set(overview["models"]) == {
        "almaz",
        "igor",
        "nova",
        "knives",
        "gloves",
    }
    assert _mode(paths.state) == 0o600
    assert _mode(paths.state_lock) == 0o600
    assert _mode(paths.run_dir) == 0o700
    assert "worker_pid" not in overview["models"]["almaz"]
    assert "trainer_start_token" not in overview["models"]["almaz"]
    assert overview["models"]["almaz"]["promotion_allowed"] is False


def test_unknown_model_never_defaults_to_weapon_kind():
    with pytest.raises(ValueError, match="unknown model"):
        jobs._model_item_kind("unexpected")


def test_legacy_schema_v1_state_adds_knives_without_losing_terminal_jobs(
    tmp_path, monkeypatch
):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    paths.run_dir.mkdir(parents=True)
    legacy = jobs._default_state()
    legacy["updated_at"] = "2026-08-20T12:34:56+00:00"
    legacy["models"].pop("knives")
    legacy["models"].pop("gloves")
    legacy["models"]["almaz"].update(
        {
            "job_id": "a" * 32,
            "state": "succeeded",
            "phase": "active",
            "progress_percent": 100.0,
            "finished_at": "2026-08-19T11:00:00+00:00",
            "candidate_bundle_sha256": "b" * 64,
            "quality_gate": {
                "strictly_improved_metrics": ["median_ape"],
                "holdout_rows": 5_000,
            },
            "activation_state": "activated",
            "active_bundle_version": "b" * 64,
        }
    )
    legacy["models"]["igor"].update(
        {
            "job_id": "c" * 32,
            "state": "rejected",
            "phase": "quality_gate",
            "progress_percent": 96.0,
            "finished_at": "2026-08-20T12:00:00+00:00",
            "error": "candidate was worse on the shared holdout",
            "rejection_kind": "quality",
            "candidate_dir": "IGORMODEL/staging/training_jobs/" + "c" * 32,
            "snapshot_rows": 1_234_567,
            "retention_error": "kept for review",
        }
    )
    legacy["models"]["nova"].update(
        {
            "job_id": "d" * 32,
            "state": "failed",
            "phase": "trainer_failed",
            "progress_percent": 55.5,
            "exit_code": 1,
            "error": "trainer exited with code 1",
        }
    )
    original_jobs = deepcopy(legacy["models"])
    paths.state.write_text(json.dumps(legacy, indent=2), encoding="utf-8")

    atomic_writes = []
    real_atomic_json = jobs._atomic_json

    def capture_atomic_write(path, payload):
        atomic_writes.append((path, deepcopy(payload)))
        real_atomic_json(path, payload)

    monkeypatch.setattr(jobs, "_atomic_json", capture_atomic_write)
    with jobs._locked_state(paths):
        migrated = jobs._read_state_unlocked(paths)

    persisted = json.loads(paths.state.read_text(encoding="utf-8"))
    assert migrated == persisted
    assert migrated["schema_version"] == jobs.SCHEMA_VERSION
    assert migrated["updated_at"] == legacy["updated_at"]
    assert {name: migrated["models"][name] for name in original_jobs} == original_jobs
    assert migrated["models"]["knives"] == jobs._idle_job("knives")
    assert migrated["models"]["gloves"] == jobs._idle_job("gloves")
    assert atomic_writes == [(paths.state, migrated)]
    assert _mode(paths.state) == 0o600
    assert not list(paths.run_dir.glob(f".{paths.state.name}.*.tmp"))


def test_legacy_almaz_sha_error_is_reclassified_as_integrity_failure(
    tmp_path, monkeypatch
):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    paths.run_dir.mkdir(parents=True)
    job_id = "2e37a2ab28144c039648a6ecb2417539"
    candidate = project / "models" / "staging" / "training_jobs" / job_id
    candidate.mkdir(parents=True)
    sentinel = candidate / "candidate.keep"
    sentinel.write_text("must not be activated or deleted", encoding="utf-8")
    legacy = jobs._default_state()
    legacy["models"].pop("knives")
    legacy["models"].pop("gloves")
    legacy["models"]["almaz"].update(
        {
            "job_id": job_id,
            "state": "rejected",
            "phase": "quality_rejected",
            "progress_percent": 100.0,
            "error": jobs.LEGACY_ALMAZ_SHA_ERROR,
            "rejection_kind": None,
            "candidate_dir": str(candidate.relative_to(project)),
            "quality_gate": None,
            "candidate_bundle_sha256": None,
            "activation_state": "none",
        }
    )
    paths.state.write_text(json.dumps(legacy), encoding="utf-8")

    with jobs._locked_state(paths):
        migrated = jobs._read_state_unlocked(paths)

    almaz = migrated["models"]["almaz"]
    assert almaz["state"] == "rejected"
    assert almaz["phase"] == "validation_failed"
    assert almaz["rejection_kind"] == "integrity"
    assert almaz["error"] == jobs.CORRECTED_ALMAZ_SHA_ERROR
    assert almaz["legacy_error"] == jobs.LEGACY_ALMAZ_SHA_ERROR
    assert almaz["job_id"] == job_id
    assert almaz["candidate_dir"] == str(candidate.relative_to(project))
    assert sentinel.read_text(encoding="utf-8") == "must not be activated or deleted"

    public = jobs._public_job(paths, almaz, {})
    assert public["phase"] == "validation_failed"
    assert public["rejection_kind"] == "integrity"
    assert public["error"] == jobs.CORRECTED_ALMAZ_SHA_ERROR
    assert "legacy_error" not in public

    monkeypatch.setattr(
        jobs,
        "_atomic_json",
        lambda *_args, **_kwargs: pytest.fail("completed migration must be idempotent"),
    )
    with jobs._locked_state(paths):
        reread = jobs._read_state_unlocked(paths)
    assert reread == migrated


def test_legacy_state_with_unknown_model_is_rejected_without_rewrite(
    tmp_path, monkeypatch
):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    paths.run_dir.mkdir(parents=True)
    invalid = jobs._default_state()
    invalid["models"].pop("knives")
    invalid["models"]["unexpected"] = {
        **jobs._idle_job("knives"),
        "model": "unexpected",
    }
    original_payload = deepcopy(invalid)
    original_bytes = json.dumps(invalid, indent=2).encode("utf-8")
    paths.state.write_bytes(original_bytes)

    monkeypatch.setattr(
        jobs,
        "_atomic_json",
        lambda *_args, **_kwargs: pytest.fail("invalid state must not be rewritten"),
    )
    with (
        jobs._locked_state(paths),
        pytest.raises(jobs.TrainingJobError, match="invalid model set"),
    ):
        jobs._read_state_unlocked(paths)

    assert invalid == original_payload
    assert paths.state.read_bytes() == original_bytes


def test_progress_parsers_are_monotonic_and_produce_eta(monkeypatch):
    clock = iter((100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0))
    monkeypatch.setattr(jobs.time, "monotonic", lambda: next(clock))

    igor = jobs.ProgressTracker("igor", started_monotonic=90.0)
    assert igor.feed("[split] train=100, validation=20, untouched test=5000")[1] == 22
    phase, percent, eta = igor.feed(
        "250:\tlearn: 0.1\ttest: 0.1\ttotal: 1m\tremaining: 9m"
    )
    assert phase == "training_point"
    assert 24 < percent < 26
    assert eta > 540
    assert igor.feed("[point] 1000.0s best=2400")[1] == 51

    almaz = jobs.ProgressTracker("almaz", started_monotonic=90.0)
    almaz.feed("Training point model (700 iterations, target=log_premium)...")
    assert 36 < almaz.feed("[350/700] 1 tree, 31 leaves")[1] < 38

    nova = jobs.ProgressTracker("nova", started_monotonic=90.0)
    nova.feed("parameters: 8,000,000")
    assert nova.feed("epoch 09 train=0.1 val=0.1")[1] == 59
    assert jobs._duration_seconds("500ms") == pytest.approx(0.5)
    assert jobs._duration_seconds("1m 2s") == pytest.approx(62)
    assert jobs._duration_seconds("250us") == pytest.approx(0.00025)


def test_global_resource_lock_rejects_second_trainer(tmp_path):
    paths = jobs._paths(_make_project(tmp_path))
    first = jobs._acquire_resource_lock(paths)
    try:
        with pytest.raises(jobs.TrainingConflictError, match="already running"):
            jobs._acquire_resource_lock(paths)
    finally:
        jobs._release_resource_lock(first)


def test_managed_state_rejects_intermediate_symlink(tmp_path):
    project = _make_project(tmp_path / "project")
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (project / ".run").symlink_to(redirected)

    with pytest.raises(jobs.TrainingJobError, match="symlink directory"):
        jobs.get_training_overview(project_dir=project)


def test_crash_recovery_marks_dead_job_interrupted(tmp_path, monkeypatch):
    paths, _job_id = _install_job(_make_project(tmp_path), pid=999_999_999)
    monkeypatch.setattr(jobs, "_process_alive", lambda _pid: False)

    overview = jobs.recover_training_jobs(project_dir=paths.project)

    status = overview["models"]["almaz"]
    assert status["state"] == "interrupted"
    assert status["can_cancel"] is False
    assert overview["active_job_id"] is None
    assert "not promoted" in status["error"]


def test_crash_recovery_trusts_live_resource_lock_over_stale_ps(
    tmp_path, monkeypatch
):
    paths, _job_id = _install_job(
        _make_project(tmp_path), pid=999_999_999
    )
    resource_fd = jobs._acquire_resource_lock(paths)
    monkeypatch.setattr(jobs, "_process_alive", lambda _pid: False)
    try:
        overview = jobs.recover_training_jobs(project_dir=paths.project)
    finally:
        jobs._release_resource_lock(resource_fd)

    assert overview["models"]["almaz"]["state"] == "running"
    assert overview["active_job_id"] is not None


def test_recovery_does_not_race_fresh_start_before_pid_is_recorded(tmp_path):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    job_id = "9" * 32
    spec = jobs._model_spec(
        "almaz",
        paths,
        sys.executable,
        paths.snapshot_dir / f"{job_id}.db",
    )

    def install(payload):
        payload["models"]["almaz"] = jobs._new_job(
            "almaz", job_id, spec, project
        )
        payload["active_job_id"] = job_id

    jobs._mutate_state(paths, install)
    overview = jobs.get_training_overview(project_dir=project)

    assert overview["active_job_id"] == job_id
    assert overview["models"]["almaz"]["state"] == "starting"


def test_cancel_refuses_unverified_or_reused_pid(tmp_path, monkeypatch):
    paths, job_id = _install_job(
        _make_project(tmp_path), pid=os.getpid()
    )
    monkeypatch.setattr(jobs, "_pid_matches", lambda _job, _kind: False)
    signalled = []
    monkeypatch.setattr(jobs.os, "kill", lambda *args: signalled.append(args))

    with pytest.raises(jobs.TrainingJobError, match="unverified or reused"):
        jobs.cancel_model_training(
            "almaz", job_id=job_id, project_dir=paths.project
        )

    assert signalled == []


def test_cancel_removes_matching_pending_activation(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    paths, job_id = _install_job(project, pid=os.getpid())
    candidate = project / "models" / "staging" / "training_jobs" / job_id
    candidate.mkdir(parents=True)
    import model_training_activation

    manifest = {
        "bundle_sha256": "b" * 64,
        "quality_gate": {"holdout_rows": 5_000},
    }
    monkeypatch.setattr(
        model_training_activation,
        "_validate_manifest",
        lambda *_args: manifest,
    )
    model_training_activation.schedule_pending_activation(
        "almaz", job_id, candidate, project_dir=project
    )
    monkeypatch.setattr(jobs, "_pid_matches", lambda _job, _kind: True)
    signalled = []
    monkeypatch.setattr(jobs.os, "kill", lambda *args: signalled.append(args))

    status = jobs.cancel_model_training(
        "almaz", job_id=job_id, project_dir=paths.project
    )

    assert status["state"] == "cancelling"
    assert signalled == [(os.getpid(), jobs.signal.SIGTERM)]
    assert not (
        project / ".run" / "model_training" / "pending" / "almaz.json"
    ).exists()


def test_log_tail_redacts_credentials_and_is_bounded(tmp_path):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    job_id = "b" * 32
    log_path = jobs._log_path(paths, "igor", job_id)
    log_path.write_text(
        "token=top-secret\n"
        "proxy=http://user:password@example.test\n"
        "https://example.test/?api_key=also-secret\n"
        "CSFLOAT_API_KEY=env-secret\n"
        "DASHBOARD_TOKEN: dashboard-secret\n"
        "Authorization: Bearer auth-secret\n"
        "request failed Bearer standalone-secret\n",
        encoding="utf-8",
    )

    lines = jobs._read_log_tail(paths, "igor", job_id)

    joined = "\n".join(lines)
    assert "top-secret" not in joined
    assert "password" not in joined
    assert "also-secret" not in joined
    assert "env-secret" not in joined
    assert "dashboard-secret" not in joined
    assert "auth-secret" not in joined
    assert "standalone-secret" not in joined
    assert "***" in joined


def test_model_commands_are_fixed_staging_only_and_use_reserved_holdout(tmp_path):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    snapshot = paths.snapshot_dir / ("c" * 32 + ".db")

    almaz = jobs._model_spec("almaz", paths, sys.executable, snapshot)
    igor = jobs._model_spec("igor", paths, sys.executable, snapshot)
    nova = jobs._model_spec("nova", paths, sys.executable, snapshot)
    knives = jobs._model_spec("knives", paths, sys.executable, snapshot)
    gloves = jobs._model_spec("gloves", paths, sys.executable, snapshot)

    for spec in (almaz, igor, nova, knives, gloves):
        assert spec.candidate_dir.is_relative_to(project)
        assert "artifacts" not in spec.candidate_dir.parts
        assert "--test-rows" in spec.command
        index = spec.command.index("--test-rows")
        assert spec.command[index + 1] == "5000"
    assert "--progress" in almaz.command
    assert "--progress" in knives.command
    assert knives.required_files == (
        "knife_price_model.joblib",
        "knife_price_model_metrics.json",
    )
    assert gloves.required_files == (
        "glove_price_model.joblib",
        "glove_price_model_metrics.json",
    )
    assert gloves.command[1].endswith("train_glove_price_model.py")
    assert igor.command[-2:] == ("--verbose-period", "25")
    assert "--rebuild-cache" in nova.command
    assert almaz.candidate_dir != jobs._model_spec(
        "almaz",
        paths,
        sys.executable,
        paths.snapshot_dir / ("f" * 32 + ".db"),
    ).candidate_dir


def test_model_command_preserves_virtualenv_launcher_symlink(tmp_path):
    project = _make_project(tmp_path)
    launcher = project / "venv-python"
    launcher.symlink_to(sys.executable)
    paths = jobs._paths(project)

    spec = jobs._model_spec(
        "almaz",
        paths,
        launcher,
        paths.snapshot_dir / ("e" * 32 + ".db"),
    )

    assert spec.command[0] == str(launcher)


def test_eligibility_requires_all_three_data_thresholds(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    metrics_dir = project / "models"
    metrics_dir.mkdir(exist_ok=True)
    metrics_dir.joinpath("csfloat_price_model_metrics.json").write_text(
        json.dumps(
            {
                "item_kind": jobs.ITEM_KIND_WEAPON_SKIN,
                "rows": 10,
                "trained_at": "2026-01-01T00:00:00+00:00",
                "source_database": {
                    "item_kind": jobs.ITEM_KIND_WEAPON_SKIN,
                    "item_kind_policy": jobs.STRICT_ITEM_KIND_POLICY,
                    "valid_price_rows": 10,
                    "max_sold_at": "2026-01-01T00:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    database = project / "history.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE sales (
                state TEXT, price_cents INTEGER, name TEXT, sold_at TEXT,
                reference_last_updated TEXT, reference_predicted_price INTEGER,
                reference_base_price INTEGER
            )
            """
        )
        old = (
            "sold",
            100,
            "AK-47 | Redline (Field-Tested)",
            "2025-12-01T00:00:00+00:00",
            "2025-11-01T00:00:00+00:00",
            100,
            100,
        )
        connection.executemany(
            "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?, ?)", [old] * 10
        )
        fresh = (
            "sold",
            100,
            "M4A1-S | Printstream (Field-Tested)",
            "2026-02-01T00:00:00+00:00",
            "2026-01-15T00:00:00+00:00",
            100,
            100,
        )
        connection.executemany(
            "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?, ?)",
            [fresh] * jobs.MIN_RUNTIME_VALID_NEW_ROWS,
        )
        invalid = (
            "pending",
            100,
            "AWP | Asiimov (Field-Tested)",
            "2026-02-01T00:00:00+00:00",
            "2026-01-15T00:00:00+00:00",
            100,
            100,
        )
        connection.executemany(
            "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?, ?)",
            [invalid] * (jobs.MIN_NEW_ROWS - jobs.MIN_RUNTIME_VALID_NEW_ROWS),
        )
    real_path = jobs.app_config.path
    monkeypatch.setattr(
        jobs.app_config,
        "path",
        lambda name, default: database if name == "CS2_DB_PATH" else real_path(name, default),
    )

    blocked_by_valid_count = jobs.get_model_training_eligibility(
        "almaz", project_dir=project
    )

    assert blocked_by_valid_count["eligible"] is False
    assert blocked_by_valid_count["new_rows"] == jobs.MIN_RUNTIME_VALID_NEW_ROWS
    assert "not_enough_new_rows" in blocked_by_valid_count["reasons"]

    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?, ?)",
            [fresh] * (jobs.MIN_NEW_ROWS - jobs.MIN_RUNTIME_VALID_NEW_ROWS),
        )
    eligible = jobs.get_model_training_eligibility("almaz", project_dir=project)

    assert eligible["eligible"] is True
    assert eligible["new_rows"] == jobs.MIN_NEW_ROWS
    assert eligible["runtime_valid_new_rows"] == jobs.MIN_NEW_ROWS
    assert eligible["growth_percent"] >= jobs.MIN_DATA_GROWTH_PERCENT

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE sales SET reference_last_updated = '2026-03-01T00:00:00+00:00' "
            "WHERE name = 'M4A1-S | Printstream (Field-Tested)'"
        )
    blocked = jobs.get_model_training_eligibility("almaz", project_dir=project)
    assert blocked["eligible"] is False
    assert "not_enough_runtime_valid_rows" in blocked["reasons"]


def test_knives_bootstrap_eligibility_uses_isolated_absolute_thresholds(tmp_path):
    project = _make_project(tmp_path)

    blocked_below_split_floor = jobs.get_model_training_eligibility(
        "knives",
        project_dir=project,
        _database_counts=(
            jobs.KNIFE_BOOTSTRAP_MIN_ROWS - 1,
            jobs.KNIFE_BOOTSTRAP_MIN_RUNTIME_VALID_ROWS - 1,
        ),
    )
    blocked_by_runtime_valid = jobs.get_model_training_eligibility(
        "knives",
        project_dir=project,
        _database_counts=(
            jobs.KNIFE_BOOTSTRAP_MIN_ROWS,
            jobs.KNIFE_BOOTSTRAP_MIN_RUNTIME_VALID_ROWS - 1,
        ),
    )
    ready = jobs.get_model_training_eligibility(
        "knives",
        project_dir=project,
        _database_counts=(
            jobs.KNIFE_BOOTSTRAP_MIN_ROWS,
            jobs.KNIFE_BOOTSTRAP_MIN_RUNTIME_VALID_ROWS,
        ),
    )

    assert jobs.KNIFE_BOOTSTRAP_MIN_ROWS == 16_000
    assert jobs.KNIFE_BOOTSTRAP_MIN_RUNTIME_VALID_ROWS == 16_000
    assert blocked_below_split_floor["bootstrap"] is True
    assert blocked_below_split_floor["item_kind"] == "knife"
    assert "not_enough_new_rows" in blocked_below_split_floor["reasons"]
    assert "production_metrics_missing" not in blocked_below_split_floor["reasons"]
    assert "not_enough_runtime_valid_rows" in blocked_by_runtime_valid["reasons"]
    assert ready["eligible"] is True
    assert ready["new_rows"] == jobs.KNIFE_BOOTSTRAP_MIN_ROWS


def test_gloves_bootstrap_eligibility_uses_dedicated_isolated_thresholds(tmp_path):
    project = _make_project(tmp_path)

    result = jobs.get_model_training_eligibility(
        "gloves",
        project_dir=project,
        _database_counts=(
            jobs.GLOVE_BOOTSTRAP_MIN_ROWS,
            jobs.GLOVE_BOOTSTRAP_MIN_RUNTIME_VALID_ROWS,
        ),
    )

    assert result["eligible"] is True
    assert result["bootstrap"] is True
    assert result["item_kind"] == "glove"
    assert result["trainer_available"] is True
    assert result["thresholds"] == {
        "min_new_rows": 16_000,
        "min_growth_percent": jobs.MIN_DATA_GROWTH_PERCENT,
        "min_runtime_valid_new_rows": 16_000,
    }


def test_eligibility_rejects_explicit_cross_kind_or_corrupt_source_provenance(
    tmp_path, monkeypatch
):
    project = _make_project(tmp_path)
    metrics = {
        "item_kind": "weapon_skin",
        "rows": 100,
        "trained_at": "2026-01-01T00:00:00+00:00",
        "source_database": {
            "item_kind": "knife",
            "item_kind_policy": "unknown_as_weapon_v0",
            "valid_price_rows": 100,
            "max_sold_at": "2026-01-01T00:00:00+00:00",
            "logical_sha256": "not-a-sha",
        },
    }
    monkeypatch.setattr(
        jobs,
        "_production_metrics",
        lambda _model, _paths: (project / "metrics.json", metrics),
    )

    result = jobs.get_model_training_eligibility(
        "almaz", project_dir=project, _database_counts=(50_000, 10_000)
    )

    assert result["eligible"] is False
    assert "production_item_kind_mismatch" in result["reasons"]
    assert "production_item_kind_policy_mismatch" in result["reasons"]
    assert "production_source_fingerprint_invalid" in result["reasons"]
    assert result["source_item_kind_verified"] is False
    assert result["contract_migration"] is False
    assert result["retrain_required"] is True


def test_legacy_missing_item_kind_warns_and_requires_retrain_without_blocking(
    tmp_path, monkeypatch
):
    project = _make_project(tmp_path)
    metrics = {
        # This old permissive count can be larger than today's strict segment.
        # It must never become a permanent current_rows < trained_rows block.
        "rows": 1_000_000,
        "trained_at": "2026-01-01T00:00:00+00:00",
        "source_database": {
            "item_kind": jobs.ITEM_KIND_WEAPON_SKIN,
            "valid_price_rows": 1_000_000,
            "max_sold_at": "2026-01-01T00:00:00+00:00",
            "logical_sha256": "a" * 64,
        },
    }
    monkeypatch.setattr(
        jobs,
        "_production_metrics",
        lambda _model, _paths: (project / "metrics.json", metrics),
    )

    result = jobs.get_model_training_eligibility(
        "igor",
        project_dir=project,
        _database_counts=(jobs.MIN_NEW_ROWS, jobs.MIN_RUNTIME_VALID_NEW_ROWS),
    )

    assert result["eligible"] is True
    assert "production_item_kind_mismatch" not in result["reasons"]
    assert "database_row_count_mismatch" not in result["reasons"]
    assert result["new_rows"] == jobs.MIN_NEW_ROWS
    assert result["growth_percent"] == 100.0
    assert result["threshold_baseline_rows"] is None
    assert result["contract_migration"] is True
    assert result["warnings"] == [
        "production_item_kind_unverified_legacy",
        "production_item_kind_policy_unverified_legacy",
    ]
    assert result["retrain_required"] is True
    assert result["source_item_kind_verified"] is False


def test_strict_eligibility_never_counts_unknown_as_weapon_skin(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    database = project / "history.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE sales (
                state TEXT, price_cents INTEGER, name TEXT, sold_at TEXT,
                reference_last_updated TEXT, reference_predicted_price INTEGER,
                reference_base_price INTEGER
            )
            """
        )
        connection.executemany(
            "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "sold", 100, "AK-47 | Redline (Field-Tested)",
                    "2026-02-01", "2026-01-15", 100, 100,
                ),
                (
                    "sold", 100, "totally unknown commodity",
                    "2026-02-01", "2026-01-15", 100, 100,
                ),
                (
                    "sold", 100, "★ Karambit | Fade (Factory New)",
                    "2026-02-01", "2026-01-15", 100, 100,
                ),
                (
                    "sold", 100, "★ Sport Gloves | Vice (Field-Tested)",
                    "2026-02-01", "2026-01-15", 100, 100,
                ),
            ],
        )
    metrics_by_model = {
        model: {
            "item_kind": jobs._model_item_kind(model),
            "rows": 1,
            "trained_at": "2026-01-01T00:00:00+00:00",
            "source_database": {
                "item_kind": jobs._model_item_kind(model),
                "item_kind_policy": jobs.STRICT_ITEM_KIND_POLICY,
                "valid_price_rows": 1,
                "max_sold_at": "2026-01-01T00:00:00+00:00",
                "logical_sha256": "a" * 64,
            },
        }
        for model in jobs.MODEL_NAMES
    }
    monkeypatch.setattr(
        jobs,
        "_production_metrics",
        lambda model, _paths: (project / f"{model}.json", metrics_by_model[model]),
    )
    real_path = jobs.app_config.path
    monkeypatch.setattr(
        jobs.app_config,
        "path",
        lambda name, default: database if name == "CS2_DB_PATH" else real_path(name, default),
    )

    result = jobs.get_all_training_eligibility(project_dir=project)

    assert result["almaz"]["current_rows"] == 1
    assert result["igor"]["current_rows"] == 1
    assert result["nova"]["current_rows"] == 1
    assert result["knives"]["current_rows"] == 1
    assert result["gloves"]["current_rows"] == 1


def test_all_model_eligibility_shares_one_consistent_database_snapshot(
    tmp_path, monkeypatch
):
    project = _make_project(tmp_path)
    database = project / "history.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE sales (
                state TEXT, price_cents INTEGER, name TEXT, sold_at TEXT,
                reference_last_updated TEXT, reference_predicted_price INTEGER,
                reference_base_price INTEGER
            )
            """
        )
        connection.executemany(
            "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "sold", 100, "AK-47 | Redline (Field-Tested)",
                    "2025-12-01", "2025-11-01", 100, 100,
                ),
                (
                    "sold", 100, "M4A1-S | Printstream (Field-Tested)",
                    "2026-02-01", "2026-01-15", 100, 100,
                ),
                (
                    "sold", 100, "AWP | Asiimov (Field-Tested)",
                    "2026-03-01", "2026-02-15", None, 100,
                ),
                (
                    "pending", 100, "Glock-18 | Fade (Factory New)",
                    "2026-03-01", "2026-02-15", 100, 100,
                ),
            ],
        )
    metrics_by_model = {
        model: {
            "item_kind": jobs._model_item_kind(model),
            "rows": 1,
            "trained_at": "2026-01-01T00:00:00+00:00",
            "source_database": {
                "item_kind": jobs._model_item_kind(model),
                "item_kind_policy": jobs.STRICT_ITEM_KIND_POLICY,
                "valid_price_rows": 1,
                "max_sold_at": "2026-01-01T00:00:00+00:00",
            },
        }
        for model in jobs.MODEL_NAMES
    }
    monkeypatch.setattr(
        jobs,
        "_production_metrics",
        lambda model, paths: (project / f"{model}.json", metrics_by_model[model]),
    )
    real_path = jobs.app_config.path
    monkeypatch.setattr(
        jobs.app_config,
        "path",
        lambda name, default: database if name == "CS2_DB_PATH" else real_path(name, default),
    )

    result = jobs.get_all_training_eligibility(project_dir=project)

    for model in ("almaz", "igor", "nova"):
        assert result[model]["current_rows"] == 3
        assert result[model]["new_rows"] == 2
        assert result[model]["runtime_valid_new_rows"] == 2
        assert "database_eligibility_check_failed" not in result[model]["reasons"]
    assert result["knives"]["current_rows"] == 0
    assert result["knives"]["new_rows"] == 0
    assert result["knives"]["runtime_valid_new_rows"] == 0
    assert "database_eligibility_check_failed" not in result["knives"]["reasons"]
    assert result["gloves"]["current_rows"] == 0
    assert result["gloves"]["runtime_valid_new_rows"] == 0
    assert "database_eligibility_check_failed" not in result["gloves"]["reasons"]


def test_trainer_gate_exits_on_worker_pipe_eof_without_exec(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    lock_path = tmp_path / "resource.lock"
    resource_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    gate_read, gate_write = os.pipe()
    os.close(gate_write)
    monkeypatch.setenv("CS2_TRAINING_INTERNAL_WORKER", "1")
    try:
        result = jobs._run_trainer_gate(
            "almaz", "7" * 32, resource_fd, project, gate_read
        )
    finally:
        os.close(resource_fd)

    assert result == 125


def test_stop_and_reap_escalates_to_kill_before_reporting_terminal(monkeypatch):
    class StubbornProcess:
        pid = 12345
        stopped = False
        waited = False

        def poll(self):
            return -9 if self.stopped else None

        def wait(self, timeout):
            self.waited = True
            return self.poll()

    process = StubbornProcess()
    signals = []

    def terminate(_pid, *, force=False):
        signals.append(force)
        if force:
            process.stopped = True

    monkeypatch.setattr(jobs, "_terminate_group", terminate)

    assert jobs._stop_and_reap(process, grace_seconds=0) is True
    assert signals == [False, True]
    assert process.waited is True


def test_sqlite_snapshot_is_private_immutable_and_point_in_time(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    database = project / "source.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE sales (state TEXT, price_cents INTEGER, name TEXT)"
        )
        connection.execute("INSERT INTO sales VALUES ('sold', 100, 'one')")
    monkeypatch.setattr(
        jobs.app_config,
        "path",
        lambda name, default: database if name == "CS2_DB_PATH" else Path(default),
    )
    monkeypatch.setattr(jobs, "_update_worker_job", lambda *args, **kwargs: True)
    paths = jobs._paths(project)
    job_id = "d" * 32

    snapshot, rows = jobs._create_database_snapshot(paths, "almaz", job_id)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO sales VALUES ('sold', 200, 'two')")

    assert rows == 1
    assert _mode(snapshot) == 0o400
    with sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1
    jobs._delete_snapshot(paths, job_id)
    assert not snapshot.exists()


def test_snapshot_requires_model_specific_free_disk_reserve(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    database = project / "source.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE sales (state TEXT, price_cents INTEGER, name TEXT)"
        )
    monkeypatch.setattr(
        jobs.app_config,
        "path",
        lambda name, default: database if name == "CS2_DB_PATH" else Path(default),
    )
    disk_usage = shutil.disk_usage(project)
    monkeypatch.setattr(
        jobs.shutil,
        "disk_usage",
        lambda _path: disk_usage._replace(free=jobs.MODEL_MIN_FREE_DISK_BYTES["igor"] - 1),
    )

    with pytest.raises(jobs.TrainingJobError, match="not enough free space"):
        jobs._create_database_snapshot(
            jobs._paths(project), "igor", "d" * 32
        )


def test_start_uses_list_argv_no_shell_and_inherits_lock(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    captured = {}

    class FakeProcess:
        pid = os.getpid()

        def poll(self):
            return None

        def terminate(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(
        jobs,
        "get_model_training_eligibility",
        lambda *args, **kwargs: {"eligible": True, "reasons": []},
    )
    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(jobs, "_process_start_token", lambda _pid: "birth-token")
    monkeypatch.setattr(jobs, "_pid_matches", lambda _job, _kind: True)

    result = jobs.start_model_training(
        "almaz", project_dir=project, python_bin=sys.executable
    )

    assert isinstance(captured["command"], list)
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["pass_fds"]
    assert result["state"] == "starting"
    assert result["promotion_allowed"] is False


def test_start_refuses_to_replace_same_model_pending_activation(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    monkeypatch.setattr(
        jobs,
        "get_model_training_eligibility",
        lambda *args, **kwargs: {"eligible": True, "reasons": []},
    )
    import model_training_activation

    monkeypatch.setattr(
        model_training_activation,
        "get_pending_activations",
        lambda **_kwargs: {"almaz": {"job_id": "a" * 32}},
    )

    with pytest.raises(jobs.TrainingConflictError, match="waiting for restart"):
        jobs.start_model_training(
            "almaz", project_dir=project, python_bin=sys.executable
        )


def test_strict_candidate_gate_returns_manifest_and_rejects_validator_error(
    tmp_path, monkeypatch
):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    snapshot = paths.snapshot_dir / ("8" * 32 + ".db")
    paths.snapshot_dir.mkdir(parents=True, exist_ok=True)
    spec = jobs._model_spec("almaz", paths, sys.executable, snapshot)
    spec.candidate_dir.mkdir(parents=True)
    for filename in spec.required_files:
        (spec.candidate_dir / filename).write_bytes(b"candidate")
    (spec.candidate_dir / spec.metrics_file).write_text(
        json.dumps(
            {
                "stage": "staging",
                "production_promoted": False,
                "model_name": "almaz",
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "bundle_sha256": "9" * 64,
        "quality_gate": {"holdout_rows": 5_000},
    }
    import model_candidate_promotion

    monkeypatch.setattr(
        model_candidate_promotion,
        "validate_candidate",
        lambda model, candidate: manifest,
    )

    checked = jobs._validate_candidate(spec, "2020-01-01T00:00:00+00:00")

    assert checked == manifest
    monkeypatch.setattr(
        model_candidate_promotion,
        "validate_candidate",
        lambda *_args: (_ for _ in ()).throw(
            model_candidate_promotion.CandidateQualityError("worse on holdout")
        ),
    )
    with pytest.raises(
        jobs.CandidateQualityRejectedError, match="quality gate rejected"
    ):
        jobs._validate_candidate(spec, "2020-01-01T00:00:00+00:00")

    monkeypatch.setattr(
        model_candidate_promotion,
        "validate_candidate",
        lambda *_args: (_ for _ in ()).throw(
            model_candidate_promotion.CandidateIntegrityError("bad SHA")
        ),
    )
    with pytest.raises(
        jobs.CandidateIntegrityRejectedError, match="integrity check rejected"
    ):
        jobs._validate_candidate(spec, "2020-01-01T00:00:00+00:00")


@pytest.mark.parametrize("model", ("almaz", "knives", "gloves"))
def test_quality_comparison_normalizes_almaz_like_fraction_metrics(model):
    metrics = {
        "trained_at": "2026-08-24T08:00:00+00:00",
        "test_rows": 5_000,
        "test_interval_coverage_15_85_log": 0.72,
        "shared_holdout_comparison": {
            "holdout_rows": 5_000,
            "production_trained_at": "2026-08-20T08:00:00+00:00",
            "staging_proves_primary_improvement": True,
            "production": {
                "median_absolute_percentage_error": 0.06,
                "mean_absolute_percentage_error": 0.12,
                "p90_absolute_percentage_error": 0.30,
                "mae_cents": 250,
                "rmse_log_price": 0.25,
                "r2_log_price": 0.98,
            },
            "staging": {
                "median_absolute_percentage_error": 0.05,
                "mean_absolute_percentage_error": 0.10,
                "p90_absolute_percentage_error": 0.25,
                "mae_cents": 200,
                "rmse_log_price": 0.20,
                "r2_log_price": 0.99,
            },
        },
    }

    result = jobs._normalized_quality_comparison(model, metrics)

    assert result["item_kind"] == jobs._model_item_kind(model)
    assert result["holdout_rows"] == 5_000
    assert result["primary_improvement_proven"] is True
    assert result["outcome"] == "improved"
    assert set(result["metrics"]) == {
        "median_ape",
        "mean_ape",
        "p90_ape",
        "mae_usd",
        "rmse_log",
        "r2",
        "interval_coverage",
        "quantile_crossing_rate",
        "candidate_row_win_percent",
    }
    assert result["metrics"]["median_ape"] == {
        "production": pytest.approx(6.0),
        "candidate": pytest.approx(5.0),
        "delta": pytest.approx(-1.0),
        "lower_is_better": True,
        "unit": "percent",
    }
    assert result["metrics"]["mae_usd"]["production"] == pytest.approx(2.5)
    assert result["metrics"]["mae_usd"]["candidate"] == pytest.approx(2.0)
    assert result["metrics"]["r2"]["lower_is_better"] is False
    assert result["metrics"]["interval_coverage"]["candidate"] == pytest.approx(72.0)
    assert result["metrics"]["interval_coverage"]["production"] is None
    assert result["metrics"]["quantile_crossing_rate"]["candidate"] is None
    assert result["metrics"]["candidate_row_win_percent"]["candidate"] is None


@pytest.mark.parametrize(
    ("model", "median_key"),
    (("igor", "median_ape_%"), ("nova", "median_ape_percent")),
)
def test_quality_comparison_normalizes_igor_and_nova_percent_metrics(
    model, median_key
):
    metrics = {
        "trained_at": "2026-08-24T08:00:00+00:00",
        "test_rows": 5_001,
        "test_raw_interval_coverage_15_85": 0.73,
        "shared_holdout_comparison": {
            "holdout_rows": 5_001,
            "production_trained_at": "2026-08-20T08:00:00+00:00",
            "staging_proves_primary_improvement": True,
            "production_interval_coverage_15_85": 0.70,
            "staging_interval_coverage_15_85": 0.73,
            "candidate_row_win_percent": 61.5,
            "production": {
                median_key: 5.5,
                ("mean_ape_%" if model == "igor" else "mean_ape_percent"): 11.0,
                ("p90_ape_%" if model == "igor" else "p90_ape_percent"): 27.0,
                "mae_usd": 1.8,
            },
            "staging": {
                median_key: 5.0,
                ("mean_ape_%" if model == "igor" else "mean_ape_percent"): 10.0,
                ("p90_ape_%" if model == "igor" else "p90_ape_percent"): 25.0,
                "mae_usd": 1.5,
            },
        },
        "test_raw_quantile_crossing_rate": 0.002,
    }

    result = jobs._normalized_quality_comparison(model, metrics)

    assert result["metrics"]["median_ape"]["production"] == pytest.approx(5.5)
    assert result["metrics"]["median_ape"]["candidate"] == pytest.approx(5.0)
    assert result["metrics"]["interval_coverage"]["production"] == pytest.approx(70.0)
    assert result["metrics"]["interval_coverage"]["candidate"] == pytest.approx(73.0)
    assert result["metrics"]["rmse_log"]["candidate"] is None
    assert result["metrics"]["quantile_crossing_rate"]["candidate"] == pytest.approx(
        0.2
    )
    assert result["metrics"]["quantile_crossing_rate"]["production"] is None
    assert result["metrics"]["candidate_row_win_percent"] == {
        "production": 50.0,
        "candidate": 61.5,
        "delta": 11.5,
        "lower_is_better": False,
        "unit": "percent",
    }


def test_quality_comparison_distinguishes_rejected_and_bootstrap_outcomes():
    rejected = jobs._normalized_quality_comparison(
        "nova",
        {
            "trained_at": "2026-08-24T08:00:00+00:00",
            "shared_holdout_comparison": {
                "holdout_rows": 5_000,
                "production_trained_at": "2026-08-20T08:00:00+00:00",
                "staging_proves_primary_improvement": False,
                "production": {"median_ape_percent": 5.0},
                "staging": {"median_ape_percent": 5.5},
            },
        },
    )
    bootstrap = jobs._normalized_quality_comparison(
        "gloves",
        {
            "trained_at": "2026-08-24T08:00:00+00:00",
            "test_rows": 5_000,
            "test": {"median_absolute_percentage_error": 0.05},
        },
    )

    assert rejected["outcome"] == "not_improved"
    assert rejected["primary_improvement_proven"] is False
    assert bootstrap["outcome"] == "bootstrap"
    assert bootstrap["production_trained_at"] is None


def test_quality_comparison_discards_non_finite_optional_evidence():
    result = jobs._normalized_quality_comparison(
        "igor",
        {
            "test_raw_quantile_crossing_rate": float("nan"),
            "shared_holdout_comparison": {
                "holdout_rows": 5_000,
                "candidate_row_win_percent": float("inf"),
                "production": {"median_ape_%": 5.0},
                "staging": {"median_ape_%": 4.9},
            },
        },
    )

    assert result["metrics"]["quantile_crossing_rate"]["candidate"] is None
    assert result["metrics"]["candidate_row_win_percent"]["candidate"] is None


def test_public_job_always_exposes_item_kind_and_quality_comparison(tmp_path):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    job = jobs._idle_job("gloves")
    comparison = {
        "item_kind": "glove",
        "holdout_rows": 5_000,
        "metrics": {},
    }
    job["quality_comparison"] = comparison

    public = jobs._public_job(paths, job, {})

    assert public["item_kind"] == "glove"
    assert public["quality_comparison"] == comparison
    assert public["warnings"] == []
    assert public["retrain_required"] is False


def test_public_job_marks_legacy_production_kind_as_unverified(
    tmp_path, monkeypatch
):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    job = jobs._idle_job("nova")
    metrics = {
        "rows": 10_000,
        "trained_at": "2026-08-20T08:00:00+00:00",
        "source_database": {
            "valid_price_rows": 10_000,
            "logical_sha256": "a" * 64,
        },
    }
    monkeypatch.setattr(
        jobs,
        "_production_metrics",
        lambda _model, _paths: (project / "metrics.json", metrics),
    )

    public = jobs._public_job(paths, job, {})

    assert public["warnings"] == [
        "production_item_kind_unverified_legacy",
        "production_item_kind_policy_unverified_legacy",
    ]
    assert public["contract_migration"] is True
    assert public["retrain_required"] is True


def test_public_status_marks_only_matching_candidate_pending_for_restart(tmp_path):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    job = jobs._idle_job("almaz")
    job.update(
        {
            "job_id": "a" * 32,
            "state": "succeeded",
            "candidate_bundle_sha256": "b" * 64,
        }
    )
    pending = {
        "almaz": {
            "job_id": "a" * 32,
            "candidate_bundle_sha256": "b" * 64,
        }
    }

    public = jobs._public_job(paths, job, pending)

    assert public["candidate_ready_for_review"] is True
    assert public["pending_activation"] is True
    assert public["activation_state"] == "pending_restart"


def test_public_status_exposes_hot_activation_generation_and_errors(tmp_path):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    job = jobs._idle_job("nova")
    job.update(
        {
            "job_id": "c" * 32,
            "state": "succeeded",
            "phase": "active",
            "activation_state": "activated",
            "active_bundle_version": "d" * 64,
            "activation_error": None,
            "rollback_error": None,
        }
    )

    public = jobs._public_job(
        paths,
        job,
        {},
        {
            "nova": {
                "active_bundle_version": "d" * 64,
                "activated_at": "2026-08-11T10:00:00+00:00",
            }
        },
    )

    assert public["phase"] == "active"
    assert public["activation_state"] == "activated"
    assert public["active_bundle_version"] == "d" * 64
    assert public["active_generation_activated_at"] == "2026-08-11T10:00:00+00:00"
    assert public["rollback_error"] is None


def test_public_status_reconciles_runtime_rollback_receipt(tmp_path):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    failed_version = "e" * 64
    job = jobs._idle_job("nova")
    job.update(
        {
            "job_id": "f" * 32,
            "state": "succeeded",
            "phase": "active",
            "activation_state": "activated",
            "candidate_bundle_sha256": failed_version,
            "active_bundle_version": failed_version,
        }
    )
    public = jobs._public_job(
        paths,
        job,
        {},
        {
            "nova": {
                "active_bundle_version": None,
                "activated_at": None,
                "last_activation_failure": {
                    "failed_generation_id": failed_version,
                    "error": "runtime smoke failed",
                    "rolled_back_to": "legacy",
                    "rollback_error": None,
                },
            }
        },
    )

    assert public["activation_state"] == "rolled_back"
    assert public["active_bundle_version"] is None
    assert public["activation_error"] == "runtime smoke failed"


def test_crash_recovery_recognizes_already_hot_activated_job(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    paths, job_id = _install_job(project, model="almaz", pid=999_999_999)
    candidate = project / model_generations.STAGING_ROOTS["almaz"] / job_id
    candidate.mkdir(parents=True)
    records = {}
    for index, filename in enumerate(model_generations.MODEL_FILES["almaz"]):
        path = candidate / filename
        path.write_bytes(f"candidate-{index}".encode())
        records[filename] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }
    manifest = {
        "item_kind": jobs.ITEM_KIND_WEAPON_SKIN,
        "files": records,
        "bundle_sha256": model_generations._bundle_sha256(records),
        "quality_gate": {"strictly_improved_metrics": ["primary"]},
    }

    def mark_validated(state):
        state["models"]["almaz"].update(
            {
                "candidate_bundle_sha256": manifest["bundle_sha256"],
                "quality_gate": manifest["quality_gate"],
                "phase": "hot_activation",
                "activation_state": "loading",
            }
        )

    jobs._mutate_state(paths, mark_validated)
    model_generations._publish_candidate(
        "almaz",
        candidate,
        manifest=manifest,
        job_id=job_id,
        project_dir=project,
        validator=lambda *_args: manifest,
        load_and_smoke=lambda *_args: None,
    )
    monkeypatch.setattr(jobs, "_resource_lock_is_held", lambda _paths: False)
    monkeypatch.setattr(jobs, "_pid_matches", lambda *_args: False)

    overview = jobs.recover_training_jobs(project_dir=project)

    status = overview["models"]["almaz"]
    assert status["state"] == "succeeded"
    assert status["phase"] == "active"
    assert status["activation_state"] == "activated"
    assert status["active_bundle_version"] == manifest["bundle_sha256"]
    assert overview["promotion_policy"] == "quality_gate_then_hot_activation"


def test_candidate_retention_keeps_active_and_latest_failure_only(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    roots = jobs._candidate_roots(paths)
    ids = {
        "latest_failed": "1" * 32,
        "old_almaz": "2" * 32,
        "succeeded_igor": "3" * 32,
        "active_nova": "4" * 32,
        "pending_nova": "5" * 32,
    }
    for model, job_ids in {
        "almaz": (ids["latest_failed"], ids["old_almaz"]),
        "igor": (ids["succeeded_igor"],),
        "nova": (ids["active_nova"], ids["pending_nova"]),
    }.items():
        for job_id in job_ids:
            directory = roots[model] / job_id
            directory.mkdir(parents=True)
            (directory / "artifact.bin").write_bytes(b"large-candidate")

    def install(state):
        state["models"]["almaz"].update(
            {
                "job_id": ids["latest_failed"],
                "state": "rejected",
                "candidate_dir": str(
                    (roots["almaz"] / ids["latest_failed"]).relative_to(project)
                ),
            }
        )
        state["models"]["igor"].update(
            {
                "job_id": ids["succeeded_igor"],
                "state": "succeeded",
                "candidate_dir": str(
                    (roots["igor"] / ids["succeeded_igor"]).relative_to(project)
                ),
            }
        )
        state["models"]["nova"].update(
            {
                "job_id": ids["active_nova"],
                "state": "running",
                "candidate_dir": str(
                    (roots["nova"] / ids["active_nova"]).relative_to(project)
                ),
            }
        )
        state["active_job_id"] = ids["active_nova"]

    jobs._mutate_state(paths, install)
    import model_training_activation

    monkeypatch.setattr(
        model_training_activation,
        "get_pending_activations",
        lambda **_kwargs: {
            "nova": {"job_id": ids["pending_nova"]}
        },
    )

    removed = jobs.cleanup_training_candidates(project_dir=project)

    assert removed["almaz"] == [ids["old_almaz"]]
    assert removed["igor"] == [ids["succeeded_igor"]]
    assert (roots["almaz"] / ids["latest_failed"]).is_dir()
    assert (roots["nova"] / ids["active_nova"]).is_dir()
    assert (roots["nova"] / ids["pending_nova"]).is_dir()


def test_candidate_retention_refuses_nested_symlink(tmp_path):
    project = _make_project(tmp_path)
    paths = jobs._paths(project)
    job_id = "6" * 32
    candidate = jobs._candidate_roots(paths)["almaz"] / job_id
    candidate.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (candidate / "escape").symlink_to(outside)

    with pytest.raises(jobs.TrainingJobError, match="unsafe entry"):
        jobs.cleanup_training_candidates(project_dir=project)

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
