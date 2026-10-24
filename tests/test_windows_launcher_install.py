"""Exercise real .lnk creation without touching the user's desktop or autostart."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows shell shortcuts")


@pytest.fixture
def launcher_project():
    local_run = PROJECT_DIR / ".run"
    local_run.mkdir(exist_ok=True)
    # Spaces, an apostrophe, and '$' must remain literal in launch arguments.
    with tempfile.TemporaryDirectory(prefix="shortcut test's $ ", dir=local_run) as temp:
        project = Path(temp)
        shutil.copyfile(PROJECT_DIR / "install_windows_launcher.ps1", project / "install_windows_launcher.ps1")
        (project / "windows_launcher.ps1").write_text("throw 'This test must never start the launcher.'\n")
        yield project


def powershell(arguments, **kwargs):
    return subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        **kwargs,
    )


def install(project, *extra):
    return powershell([
        "-File", str(project / "install_windows_launcher.ps1"),
        "-DesktopDirectory", str(project / "Desktop"),
        "-StartupDirectory", str(project / "Startup"), *extra,
    ])


def shortcut_details(path):
    env = {**os.environ, "CS2_TEST_SHORTCUT_PATH": str(path)}
    result = powershell(["-Command", """
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CS2_TEST_SHORTCUT_PATH)
        [pscustomobject]@{
            TargetPath = $shortcut.TargetPath
            Arguments = $shortcut.Arguments
            WorkingDirectory = $shortcut.WorkingDirectory
            WindowStyle = $shortcut.WindowStyle
            Description = $shortcut.Description
        } | ConvertTo-Json -Compress
    """], env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_install_is_idempotent_and_removal_only_deletes_owned_links(launcher_project):
    project = launcher_project
    for _ in range(2):
        result = install(project)
        assert result.returncode == 0, result.stderr

    for folder, autostart in (("Desktop", False), ("Startup", True)):
        links = list((project / folder).glob("*.lnk"))
        assert len(links) == 1
        link = shortcut_details(links[0])
        assert Path(link["TargetPath"]).name.lower() == "powershell.exe"
        expected = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + str(project / "windows_launcher.ps1") + '"'
        if autostart:
            expected += " -AutoStart"
        assert link["Arguments"] == expected
        assert Path(link["WorkingDirectory"]) == project
        assert link["WindowStyle"] == 7
        (project / folder / "unrelated.txt").write_text("keep")

    result = install(project, "-Remove")
    assert result.returncode == 0, result.stderr
    for folder in ("Desktop", "Startup"):
        assert not list((project / folder).glob("*.lnk"))
        assert (project / folder / "unrelated.txt").read_text() == "keep"
    assert install(project, "-Remove").returncode == 0


def test_install_preserves_unowned_shortcut_name_collision(launcher_project):
    project = launcher_project
    for folder in ("Desktop", "Startup"):
        (project / folder).mkdir()
        (project / folder / "CS2 Dashboard.lnk").write_bytes(b"unrelated shortcut")

    for _ in range(2):
        result = install(project)
        assert result.returncode == 0, result.stderr
    for folder in ("Desktop", "Startup"):
        assert len(list((project / folder).glob("*.lnk"))) == 2
        assert (project / folder / "CS2 Dashboard.lnk").read_bytes() == b"unrelated shortcut"

    result = install(project, "-Remove")
    assert result.returncode == 0, result.stderr
    for folder in ("Desktop", "Startup"):
        assert len(list((project / folder).glob("*.lnk"))) == 1
        assert (project / folder / "CS2 Dashboard.lnk").read_bytes() == b"unrelated shortcut"


def test_desktop_only_does_not_create_startup_shortcut(launcher_project):
    project = launcher_project
    result = install(project, "-DesktopOnly")
    assert result.returncode == 0, result.stderr
    links = list((project / "Desktop").glob("*.lnk"))
    assert len(links) == 1
    assert "-AutoStart" not in shortcut_details(links[0])["Arguments"]
    assert not (project / "Startup").exists()


def test_desktop_only_install_and_removal_preserve_existing_startup(launcher_project):
    project = launcher_project
    result = install(project)
    assert result.returncode == 0, result.stderr
    startup_link = next((project / "Startup").glob("*.lnk"))
    startup_bytes = startup_link.read_bytes()
    (project / "Startup" / "unrelated.txt").write_text("keep")

    for extra in (("-DesktopOnly",), ("-DesktopOnly", "-Remove")):
        result = install(project, *extra)
        assert result.returncode == 0, result.stderr
        assert startup_link.read_bytes() == startup_bytes
        assert (project / "Startup" / "unrelated.txt").read_text() == "keep"
        assert len(list((project / "Startup").glob("*.lnk"))) == 1
    assert not list((project / "Desktop").glob("*.lnk"))


def test_test_directory_cannot_escape_project(launcher_project):
    project = launcher_project
    result = powershell([
        "-File", str(project / "install_windows_launcher.ps1"),
        "-DesktopDirectory", str(project / "Desktop"),
        "-StartupDirectory", str(project / ".." / "outside project"),
    ])
    assert result.returncode != 0
    assert "must be inside this project" in result.stderr
    assert not (project / "Desktop").exists()
