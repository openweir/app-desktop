"""What about.json in the diagnostics archive tells of a Linux machine (docs/release.md §4):
the distribution, the desktop, the session type and the package format, by allow list."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from openweir import diagnostics
from openweir.diagnostics import linux_about, os_release

UBUNTU = """PRETTY_NAME="Ubuntu 24.04.1 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
VERSION="24.04.1 LTS (Noble Numbat)"
ID=ubuntu
ID_LIKE=debian
HOME_URL="https://www.ubuntu.com/"
"""


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, "utf-8")
    return path


def test_a_linux_machine_as_the_archive_tells_of_it(tmp_path: Path) -> None:
    release = write(tmp_path, "os-release", UBUNTU)
    environ = {
        "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
        "XDG_SESSION_TYPE": "wayland",
        "HOME": "/home/someone",
        "DESKTOP_SESSION": "someone-custom",
    }
    assert linux_about(environ, [release], "deb") == {
        "os_release": {"id": "ubuntu", "version_id": "24.04"},
        "desktop": ["ubuntu", "GNOME"],
        "session_type": "wayland",
        "package_format": "deb",
    }


def test_anything_else_is_other(tmp_path: Path) -> None:
    release = write(tmp_path, "os-release", "ID='My Distro'\nVERSION_ID=1.0+someone's\n")
    environ = {"XDG_CURRENT_DESKTOP": "KDE:someone-private-wm", "XDG_SESSION_TYPE": "vnc"}
    about = linux_about(environ, [release], "tar")
    assert about == {
        "os_release": {"id": "other", "version_id": "other"},
        "desktop": ["KDE", "other"],
        "session_type": "other",
        "package_format": "other",
    }
    assert "someone" not in repr(about)


def test_what_is_not_set_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # From source: no package-format marker beside the interpreter.
    monkeypatch.setattr(diagnostics, "installed_package_format", lambda: None)
    assert linux_about({}, [tmp_path / "missing"]) == {
        "os_release": {"id": None, "version_id": None},
        "desktop": [],
        "session_type": None,
        "package_format": None,
    }


def test_the_first_os_release_there_counts(tmp_path: Path) -> None:
    usr = write(tmp_path, "usr-os-release", "ID=fedora\nVERSION_ID=41\n")
    assert os_release([tmp_path / "etc-os-release", usr]) == {"id": "fedora", "version_id": "41"}
    etc = write(tmp_path, "etc-os-release", "ID=arch\n")  # rolling: no VERSION_ID
    assert os_release([etc, usr]) == {"id": "arch", "version_id": None}


def test_about_json_has_the_linux_part_on_linux_only() -> None:
    about = diagnostics.about("zh", 1_800_000_000.0)
    assert ("linux" in about) is (sys.platform == "linux")
