import json
import os
import stat

import pytest

import model_lifecycle


def _symlink_or_skip(link, target, *, target_is_directory=False):
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is not enabled")
        raise


def _journal_record():
    return {
        "kind": "generic_files_v1",
        "model": "almaz",
        "production_directory": "/project/models",
        "backup_directory": "/project/models/backups/almaz_test",
        "candidate_bundle_sha256": "a" * 64,
        "backup_manifest_sha256": "b" * 64,
        "created_at": "2026-08-11T10:00:00+00:00",
    }


def test_runtime_shared_and_exclusive_locks_are_mutually_exclusive(tmp_path):
    with (
        model_lifecycle.service_lifetime_lock("dashboard", project_dir=tmp_path),
        pytest.raises(model_lifecycle.ModelLifecycleError, match="offline"),
        model_lifecycle.exclusive_model_update_lock(project_dir=tmp_path),
    ):
        pass

    with (
        model_lifecycle.exclusive_model_update_lock(project_dir=tmp_path),
        pytest.raises(model_lifecycle.ModelLifecycleError, match="cannot start"),
        model_lifecycle.service_lifetime_lock(
            "deal_scanner", project_dir=tmp_path
        ),
    ):
        pass


def test_service_start_fails_closed_while_durable_journal_exists(tmp_path):
    written = model_lifecycle.write_promotion_journal(
        _journal_record(), project_dir=tmp_path
    )
    path = (
        tmp_path
        / ".run"
        / "model_lifecycle"
        / model_lifecycle.JOURNAL_FILENAME
    )

    assert json.loads(path.read_text(encoding="utf-8")) == written
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with (
        pytest.raises(model_lifecycle.ModelLifecycleError, match="journal"),
        model_lifecycle.service_lifetime_lock("dashboard", project_dir=tmp_path),
    ):
        pass

    model_lifecycle.clear_promotion_journal(
        expected_model="almaz",
        expected_backup=written["backup_directory"],
        project_dir=tmp_path,
    )
    with model_lifecycle.service_lifetime_lock("dashboard", project_dir=tmp_path):
        pass


def test_journal_rejects_reserved_schema_and_dangling_symlink(tmp_path):
    with pytest.raises(model_lifecycle.ModelLifecycleError, match="schema_version"):
        model_lifecycle.write_promotion_journal(
            {**_journal_record(), "schema_version": 99}, project_dir=tmp_path
        )

    run_dir = tmp_path / ".run" / "model_lifecycle"
    run_dir.mkdir(parents=True, exist_ok=True)
    _symlink_or_skip(
        run_dir / model_lifecycle.JOURNAL_FILENAME,
        "missing.json",
    )
    with pytest.raises(model_lifecycle.ModelLifecycleError, match="symlink"):
        model_lifecycle.read_promotion_journal(project_dir=tmp_path)


def test_startup_singleton_rejects_a_second_start_all_owner(tmp_path):
    with (
        model_lifecycle.startup_lifetime_lock(project_dir=tmp_path),
        pytest.raises(
            model_lifecycle.ModelLifecycleError, match="another start_all"
        ),
        model_lifecycle.startup_lifetime_lock(project_dir=tmp_path),
    ):
        pass
