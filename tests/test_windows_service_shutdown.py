import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import windows_service_control

PROJECT = Path(__file__).resolve().parents[1]


def wait_for_path(path, process, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"service exited early: {stdout!r} {stderr!r}")
        time.sleep(0.02)
    pytest.fail(f"timed out waiting for {path.name}")


def start_fixture_service(tmp_path, code):
    script = tmp_path / "fixture.py"
    script.write_text(code, encoding="utf-8")
    stop_file = tmp_path / "service.stop"
    token = uuid.uuid4().hex
    process = subprocess.Popen(
        [sys.executable, str(PROJECT / "windows_service_runner.py"),
         "--stop-file", str(stop_file), "--stop-token", token, str(script)],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(PROJECT)},
    )
    return process, stop_file, token


def cleanup_fixture(process):
    if process.poll() is None:
        process.terminate()
    process.communicate(timeout=10)


def test_request_checks_token_and_unwinds_sqlite_transaction(tmp_path):
    process, stop_file, token = start_fixture_service(tmp_path, """
import sqlite3
import time
from pathlib import Path
connection = sqlite3.connect('fixture.db')
connection.execute('CREATE TABLE items (value INTEGER)')
connection.execute('INSERT INTO items VALUES (1)')
connection.commit()
try:
    connection.execute('INSERT INTO items VALUES (2)')
    Path('ready').touch()
    while True:
        time.sleep(0.02)
finally:
    connection.rollback()
    connection.close()
    Path('closed').touch()
""")
    try:
        wait_for_path(tmp_path / "ready", process)
        stop_file.write_text(uuid.uuid4().hex, encoding="utf-8")
        time.sleep(0.3)
        assert process.poll() is None
        stop_file.write_text(token, encoding="utf-8")
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        assert (tmp_path / "closed").exists()
        with sqlite3.connect(tmp_path / "fixture.db") as connection:
            assert connection.execute("SELECT value FROM items").fetchall() == [(1,)]
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        cleanup_fixture(process)


def test_stop_callback_wakes_a_long_windows_event_wait(tmp_path):
    process, stop_file, token = start_fixture_service(tmp_path, """
import threading
from pathlib import Path
import windows_service_control
stop = threading.Event()
windows_service_control.register_stop_callback(stop.set)
Path('ready').touch()
stop.wait(600)
Path('closed').touch()
""")
    try:
        wait_for_path(tmp_path / "ready", process)
        stop_file.write_text(token, encoding="utf-8")
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, (stdout, stderr)
        assert (tmp_path / "closed").exists()
    finally:
        cleanup_fixture(process)


def test_runner_waits_for_owned_training_child_cleanup(tmp_path):
    (tmp_path / "child.py").write_text("""
import time
from pathlib import Path
import windows_service_control
Path('child-ready').touch()
while not windows_service_control.stop_requested():
    time.sleep(0.02)
time.sleep(0.3)
Path('child-closed').touch()
""", encoding="utf-8")
    process, stop_file, token = start_fixture_service(tmp_path, """
import subprocess
import sys
import threading
from pathlib import Path
import windows_service_control
stop = threading.Event()
windows_service_control.register_stop_callback(stop.set)
child = subprocess.Popen([sys.executable, 'child.py'])
windows_service_control.register_child(child)
Path('ready').touch()
stop.wait(600)
""")
    try:
        wait_for_path(tmp_path / "child-ready", process)
        stop_file.write_text(token, encoding="utf-8")
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        assert (tmp_path / "child-closed").exists()
    finally:
        # The token is also read by the synthetic child if an assertion fails.
        stop_file.write_text(token, encoding="utf-8")
        cleanup_fixture(process)


def test_training_stop_request_is_scoped_to_inherited_owner(tmp_path, monkeypatch):
    import model_training_jobs

    token = uuid.uuid4().hex
    marker = tmp_path / "owned.stop"
    marker.write_text(token, encoding="utf-8")
    monkeypatch.setenv("CS2_SERVICE_STOP_FILE", str(marker))
    monkeypatch.setenv("CS2_SERVICE_STOP_TOKEN", token)
    assert model_training_jobs._job_cancel_requested(None, "nova", "unused")
    with pytest.raises(model_training_jobs.TrainingJobError, match="shutdown"):
        model_training_jobs.start_model_training("nova", project_dir=tmp_path)
    monkeypatch.setenv("CS2_SERVICE_STOP_TOKEN", uuid.uuid4().hex)
    assert not windows_service_control.stop_requested()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell supervisor")
@pytest.mark.parametrize("recovery", [False, True])
def test_powershell_launch_and_stop_clean_an_isolated_python_service(tmp_path, recovery):
    for name in ("windows_service_runner.py", "windows_service_control.py",
                 "windows_service_shutdown.ps1"):
        shutil.copy2(PROJECT / name, tmp_path / name)
    (tmp_path / "fixture.py").write_text("""
import threading
from pathlib import Path
import windows_service_control
stop = threading.Event()
windows_service_control.register_stop_callback(stop.set)
Path('ready').touch()
stop.wait(600)
Path('closed').touch()
""", encoding="utf-8")
    harness = tmp_path / "harness.ps1"
    harness.write_text(r"""
param([string]$Source, [string]$StopSource, [string]$Interpreter, [switch]$Recovery)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectDir = $PSScriptRoot
$RunDir = Join-Path $ProjectDir '.run'
$LogDir = Join-Path $ProjectDir 'logs'
$StopRequestFile = Join-Path $RunDir 'stop_all.windows.request'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:InstanceId = [guid]::NewGuid().ToString('N')
$script:RecoverableSupervisorInstance = ''
$script:StartedServices = New-Object System.Collections.Generic.List[string]
$null = New-Item -Path $RunDir, $LogDir -ItemType Directory
. (Join-Path $ProjectDir 'windows_service_shutdown.ps1')
$tokens = $null
$errorsFound = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errorsFound)
if ($errorsFound.Count -gt 0) { throw 'Invalid source syntax' }
$definitions = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)
foreach ($definition in $definitions) { . ([scriptblock]::Create($definition.Extent.Text)) }
try {
    $null = Start-ServiceProcess -Name 'fixture' -FilePath $Interpreter -Arguments @('-u', 'fixture.py')
    $deadline = [datetime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath (Join-Path $ProjectDir 'ready'))) {
        if ([datetime]::UtcNow -gt $deadline) { throw 'Fixture never became ready' }
        Start-Sleep -Milliseconds 100
    }
    if ($Recovery) {
        $stopAst = [System.Management.Automation.Language.Parser]::ParseFile($StopSource, [ref]$tokens, [ref]$errorsFound)
        if ($errorsFound.Count -gt 0) { throw 'Invalid stop source syntax' }
        $stopDefinitions = $stopAst.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)
        foreach ($definition in $stopDefinitions) { . ([scriptblock]::Create($definition.Extent.Text)) }
        $ServiceNames = @('fixture')
        if (-not (Stop-OrphanedServices -OwnerInstance ([guid]::NewGuid().ToString('N')))) { throw 'Foreign owner handling failed' }
        if (Test-Path -LiteralPath (Join-Path $ProjectDir 'closed')) { throw 'Stopped another owner' }
        if (-not (Stop-OrphanedServices -OwnerInstance $script:InstanceId)) { throw 'Orphan cleanup failed' }
    }
    else { Stop-StartedServices }
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir 'closed'))) { throw 'Cleanup was skipped' }
    if (Test-Path -LiteralPath (Join-Path $RunDir 'fixture.windows.json')) { throw 'State not removed' }
}
finally { Stop-StartedServices }
""", encoding="utf-8")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-File", str(harness), "-Source", str(PROJECT / "start_all.ps1"),
         "-StopSource", str(PROJECT / "stop_all.ps1"),
         "-Interpreter", sys.executable, *(["-Recovery"] if recovery else [])],
        capture_output=True, text=True, timeout=65,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "terminating its verified process tree" not in result.stdout
    assert not list((tmp_path / ".run").glob("*.stop"))


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell supervisor")
def test_close_before_supervisor_state_prevents_startup(tmp_path):
    for name in ("start_all.ps1", "windows_service_shutdown.ps1"):
        shutil.copy2(PROJECT / name, tmp_path / name)
    run_dir = tmp_path / ".run"
    run_dir.mkdir()
    marker = run_dir / f"launcher.{uuid.uuid4().hex}.stop"
    marker.touch()
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-File", str(tmp_path / "start_all.ps1"), "-PythonBin",
         str(tmp_path / "must-not-be-executed.exe")],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "CS2_LAUNCHER_STOP_FILE": str(marker)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Stop requested" in result.stdout
    assert not (run_dir / "start_all.windows.json").exists()
    assert not (run_dir / "start_all.windows.lock").exists()
    assert marker.exists()  # Only its owning desktop controller removes it.
