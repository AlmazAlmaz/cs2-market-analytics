import os
import subprocess
import sys
from pathlib import Path

import model_training_jobs
import portable_fcntl
import portable_os
from windows_ssh_askpass import _read_password

PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_windows_launcher_forces_utf8_for_python_service_logs():
    launcher = (PROJECT_DIR / "start_all.ps1").read_text(encoding="utf-8")

    utf8_mode = '$env:PYTHONUTF8 = "1"'
    io_encoding = '$env:PYTHONIOENCODING = "utf-8"'
    first_process_start = launcher.index("Start-Process")
    assert utf8_mode in launcher
    assert io_encoding in launcher
    assert launcher.index(utf8_mode) < first_process_start
    assert launcher.index(io_encoding) < first_process_start


def test_windows_password_tunnel_uses_askpass_without_secret_arguments():
    launcher = (PROJECT_DIR / "start_all.ps1").read_text(encoding="utf-8")
    wrapper = (PROJECT_DIR / "windows_ssh_askpass.cmd").read_text(
        encoding="utf-8"
    )

    assert '"SSH_ASKPASS_REQUIRE", "force"' in launcher
    assert '"CS2_SSH_ASKPASS_CONFIG"' in launcher
    assert '"BatchMode=no"' in launcher
    assert '"NumberOfPasswordPrompts=1"' in launcher
    assert '"VPS_PASSWORD"' not in wrapper
    assert "%~dp0.venv\\Scripts\\python.exe" in wrapper


def test_windows_askpass_reads_only_password_from_private_config(tmp_path):
    config = tmp_path / "hosting.local.env"
    config.write_text(
        "VPS_HOST=example.invalid\n"
        "VPS_USER=root\n"
        "VPS_PASSWORD='value with spaces & symbols'\n",
        encoding="utf-8",
    )

    assert _read_password(config) == "value with spaces & symbols"


def test_native_venv_python_path(tmp_path):
    expected = (
        tmp_path / ".venv" / "Scripts" / "python.exe"
        if os.name == "nt"
        else tmp_path / ".venv" / "bin" / "python"
    )
    assert portable_os.venv_python(tmp_path) == expected


def test_portable_lock_blocks_another_process(tmp_path):
    lock_path = tmp_path / "portable.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    portable_fcntl.flock(
        descriptor, portable_fcntl.LOCK_EX | portable_fcntl.LOCK_NB
    )
    code = """
import os
import sys
import portable_fcntl

descriptor = os.open(sys.argv[1], os.O_RDWR)
try:
    portable_fcntl.flock(
        descriptor, portable_fcntl.LOCK_EX | portable_fcntl.LOCK_NB
    )
except BlockingIOError:
    print("blocked")
else:
    print("acquired")
finally:
    os.close(descriptor)
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code, str(lock_path)],
            cwd=str(Path(__file__).resolve().parents[1]),
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        portable_fcntl.flock(descriptor, portable_fcntl.LOCK_UN)
        os.close(descriptor)
    assert completed.stdout.strip() == "blocked"


def test_shared_locks_coexist_but_still_block_exclusive_lock(tmp_path):
    lock_path = tmp_path / "portable-shared.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    portable_fcntl.flock(
        descriptor, portable_fcntl.LOCK_SH | portable_fcntl.LOCK_NB
    )
    code = """
import os
import sys
import portable_fcntl

descriptor = os.open(sys.argv[1], os.O_RDWR)
operation = portable_fcntl.LOCK_SH if sys.argv[2] == "shared" else portable_fcntl.LOCK_EX
try:
    portable_fcntl.flock(descriptor, operation | portable_fcntl.LOCK_NB)
except BlockingIOError:
    print("blocked")
else:
    print("acquired")
    portable_fcntl.flock(descriptor, portable_fcntl.LOCK_UN)
finally:
    os.close(descriptor)
"""

    def probe(mode):
        return subprocess.run(
            [sys.executable, "-c", code, str(lock_path), mode],
            cwd=str(Path(__file__).resolve().parents[1]),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    try:
        assert probe("shared") == "acquired"
        assert probe("exclusive") == "blocked"
    finally:
        portable_fcntl.flock(descriptor, portable_fcntl.LOCK_UN)
        os.close(descriptor)


def test_process_birth_token_is_stable_for_current_process():
    first = model_training_jobs._process_start_token(os.getpid())
    second = model_training_jobs._process_start_token(os.getpid())
    assert first
    assert first == second


def test_process_alive_probe_never_terminates_process():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        assert portable_os.process_alive(child.pid) is True
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)

    assert portable_os.process_alive(child.pid) is False


def test_process_alive_does_not_confuse_exit_code_259_with_running_process():
    child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(259)"])
    child.wait(timeout=10)

    assert portable_os.process_alive(child.pid) is False
