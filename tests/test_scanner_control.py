import json
import stat
import threading

import pytest

import scanner_control


def file_mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_control_persists_schema_defaults_and_private_mode(tmp_path):
    path = tmp_path / "control.json"

    state = scanner_control.get_scanner_control(
        default_interval_seconds=600,
        path=path,
    )

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert state == {**stored, "status": "enabled"}
    assert stored["schema_version"] == scanner_control.CONTROL_SCHEMA_VERSION
    assert stored["enabled"] is True
    assert stored["interval_seconds"] == 600
    assert file_mode(path) == 0o600
    assert file_mode(tmp_path / ".control.json.lock") == 0o600


def test_relative_control_path_is_resolved_from_project(monkeypatch, tmp_path):
    monkeypatch.setattr(scanner_control.app_config, "PROJECT_DIR", tmp_path)

    scanner_control.get_scanner_control(path="runtime/control.json")

    assert (tmp_path / "runtime" / "control.json").is_file()


def test_partial_updates_are_validated_and_preserve_other_fields(tmp_path):
    path = tmp_path / "control.json"
    scanner_control.get_scanner_control(path=path)

    paused = scanner_control.update_scanner_control(enabled=False, path=path)
    changed_interval = scanner_control.update_scanner_control(
        interval_seconds="300",
        path=path,
    )

    assert paused["status"] == "paused"
    assert changed_interval["enabled"] is False
    assert changed_interval["interval_seconds"] == 300
    assert changed_interval["status"] == "paused"
    assert file_mode(path) == 0o600


@pytest.mark.parametrize("value", [True, 59, 86401, 1.5, "not-a-number"])
def test_interval_validation_rejects_unsafe_values(tmp_path, value):
    with pytest.raises((TypeError, ValueError), match="interval_seconds"):
        scanner_control.update_scanner_control(
            interval_seconds=value,
            path=tmp_path / "control.json",
        )


def test_corrupt_control_fails_closed_without_destroying_evidence(tmp_path):
    path = tmp_path / "control.json"
    path.write_text('{"enabled": true', encoding="utf-8")
    original = path.read_bytes()

    state = scanner_control.get_scanner_control(path=path)

    assert state["enabled"] is False
    assert state["status"] == "paused"
    assert state["error"] == "invalid_control_state"
    assert path.read_bytes() == original
    with pytest.raises(scanner_control.ScannerControlError, match="invalid"):
        scanner_control.update_scanner_control(enabled=True, path=path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("bad_interval", [True, 1.5, "not-a-number", None])
def test_invalid_control_interval_types_also_fail_closed(tmp_path, bad_interval):
    path = tmp_path / "control.json"
    payload = {
        "schema_version": scanner_control.CONTROL_SCHEMA_VERSION,
        "enabled": True,
        "interval_seconds": bad_interval,
        "updated_at": "2026-08-10T12:00:00+00:00",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    state = scanner_control.get_scanner_control(path=path)

    assert state["enabled"] is False
    assert state["error"] == "invalid_control_state"


def test_atomic_replace_failure_preserves_previous_state(monkeypatch, tmp_path):
    path = tmp_path / "control.json"
    scanner_control.get_scanner_control(path=path)
    original = path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(scanner_control.os, "replace", fail_replace)
    with pytest.raises(scanner_control.ScannerControlError, match="atomically write"):
        scanner_control.update_scanner_control(enabled=False, path=path)

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".control.json.*.tmp"))


def test_invalid_parent_path_is_reported_as_control_error(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocked", encoding="utf-8")

    with pytest.raises(scanner_control.ScannerControlError, match="prepare scanner state"):
        scanner_control.get_scanner_control(path=blocker / "control.json")


def test_atomic_write_fsyncs_file_and_parent_directory(monkeypatch, tmp_path):
    fsynced = []
    real_fsync = scanner_control.os.fsync

    def record_fsync(fd):
        fsynced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(scanner_control.os, "fsync", record_fsync)
    scanner_control.update_scanner_control(
        enabled=False,
        interval_seconds=300,
        path=tmp_path / "control.json",
    )

    assert len(fsynced) >= 2


def test_concurrent_partial_updates_do_not_lose_fields(tmp_path):
    path = tmp_path / "control.json"
    scanner_control.get_scanner_control(path=path)
    barrier = threading.Barrier(3)
    failures = []

    def update_enabled():
        try:
            barrier.wait()
            scanner_control.update_scanner_control(enabled=False, path=path)
        except (
            OSError,
            TypeError,
            ValueError,
            threading.BrokenBarrierError,
            scanner_control.ScannerControlError,
        ) as exc:  # pragma: no cover - reported by assertion
            failures.append(exc)

    def update_interval():
        try:
            barrier.wait()
            scanner_control.update_scanner_control(interval_seconds=900, path=path)
        except (
            OSError,
            TypeError,
            ValueError,
            threading.BrokenBarrierError,
            scanner_control.ScannerControlError,
        ) as exc:  # pragma: no cover - reported by assertion
            failures.append(exc)

    threads = [threading.Thread(target=update_enabled), threading.Thread(target=update_interval)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    state = scanner_control.get_scanner_control(path=path)
    assert state["enabled"] is False
    assert state["interval_seconds"] == 900


def test_runtime_status_is_separate_validated_and_redacts_secrets(tmp_path):
    path = tmp_path / "status.json"

    status = scanner_control.update_scanner_status(
        path=path,
        process_state="scanning",
        pid=123,
        heartbeat_at="2026-08-10T12:00:00+00:00",
        cycle_started_at="2026-08-10T11:59:00+00:00",
        cycle_number=4,
        cycles_completed=3,
        cycle_targets_total=5,
        cycle_targets_scanned=2,
        cycle_listings_returned=100,
        cycle_candidates_saved=7,
        last_error=(
            "proxy=http://user:password@127.0.0.1:9000 "
            "token=top-secret api_key=also-secret"
        ),
    )

    assert status["process_state"] == "scanning"
    assert status["cycle_targets_scanned"] == 2
    assert "password" not in status["last_error"]
    assert "top-secret" not in status["last_error"]
    assert "also-secret" not in status["last_error"]
    assert "***" in status["last_error"]
    assert file_mode(path) == 0o600
    assert scanner_control.get_scanner_status(path=path) == status


def test_corrupt_runtime_status_reads_stopped_and_is_repairable(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("not-json", encoding="utf-8")

    invalid = scanner_control.get_scanner_status(path=path)
    repaired = scanner_control.update_scanner_status(
        path=path,
        process_state="paused",
        pid=321,
        heartbeat_at="2026-08-10T12:00:00+00:00",
    )

    assert invalid["process_state"] == "stopped"
    assert invalid["last_error"] == "invalid_status_state"
    assert repaired["process_state"] == "paused"
    assert repaired["pid"] == 321
    assert scanner_control.get_scanner_status(path=path) == repaired


def test_target_control_defaults_are_private_and_profile_complete(tmp_path):
    path = tmp_path / "targets.json"

    state = scanner_control.get_scanner_target_control(
        path=path,
        profile_defaults={"knives": True},
    )

    assert state["revision"] == 0
    assert tuple(state["profiles"]) == scanner_control.TARGET_PROFILE_NAMES
    assert state["profiles"]["knives"]["enabled"] is True
    assert state["profiles"]["gloves"]["enabled"] is False
    assert state["profiles"]["liquid_under_10"]["selection_mode"] == "recommended"
    assert file_mode(path) == 0o600


def test_target_control_partial_patch_uses_revision_and_replaces_lists(tmp_path):
    path = tmp_path / "targets.json"
    initial = scanner_control.get_scanner_target_control(path=path)

    changed = scanner_control.update_scanner_target_control(
        {
            "liquid_under_10": {
                "selection_mode": "custom",
                "include": [
                    "liquid_skin:AK-47 | Slate (Field-Tested)",
                    "liquid_skin:AK-47 | Slate (Field-Tested)",
                ],
            },
            "knives": {"enabled": True},
        },
        expected_revision=initial["revision"],
        path=path,
    )

    assert changed["revision"] == initial["revision"] + 1
    assert changed["profiles"]["knives"]["enabled"] is True
    assert changed["profiles"]["liquid_under_10"]["include"] == [
        "liquid_skin:AK-47 | Slate (Field-Tested)"
    ]
    assert changed["profiles"]["sticker_guns"] == initial["profiles"]["sticker_guns"]
    with pytest.raises(scanner_control.ScannerControlConflict, match="refresh"):
        scanner_control.update_scanner_target_control(
            {"knives": {"enabled": False}},
            expected_revision=initial["revision"],
            path=path,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"knives": {"include": ["glove:Vice"]}},
        {"knives": {"selection_mode": "everything"}},
        {
            "knives": {
                "include": ["knife:Karambit"],
                "exclude": ["knife:Karambit"],
            }
        },
    ],
)
def test_target_control_rejects_ambiguous_or_cross_profile_keys(tmp_path, changes):
    path = tmp_path / "targets.json"
    scanner_control.get_scanner_target_control(path=path)

    with pytest.raises(scanner_control.ScannerControlError):
        scanner_control.update_scanner_target_control(changes, path=path)


def test_corrupt_target_control_fails_closed_without_overwriting_evidence(tmp_path):
    path = tmp_path / "targets.json"
    path.write_text('{"profiles":', encoding="utf-8")
    before = path.read_bytes()

    state = scanner_control.get_scanner_target_control(path=path)

    assert state["error"] == "invalid_target_control_state"
    assert all(not profile["enabled"] for profile in state["profiles"].values())
    assert path.read_bytes() == before
