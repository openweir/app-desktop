"""The UI's half of an administrator's take-over (D5b, D66): who may, and starting the
helper.

UAC's prompt itself only shows on a Windows VM (docs/maintenance.md §2.1); CI's Windows
runners are elevated already, so starting a program elevated asks nothing there. polkit's
dialog only shows on a Linux machine with the package installed; the tests here never run
pkexec, only a stand-in for it.
"""

import os
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from openweir import product
from openweir.desktop import elevation
from openweir.host.base import HostError
from openweir.update.linux import PKEXEC


def test_uac_needs_windows() -> None:
    if sys.platform == "win32":
        pytest.skip("off Windows only")
    with pytest.raises(HostError) as raised:
        elevation.run_elevated(Path("/bin/true"), "")
    assert raised.value.code == "unsupported_method"


def test_macos_has_no_take_over() -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS's extension has one user")
    assert not elevation.is_administrator()


def test_the_helper_is_the_service() -> None:
    if sys.platform == "linux":
        # Where the package installs it, the path its polkit action names.
        assert elevation.service_executable() == Path(product.LINUX_SERVICE_PATH)
    else:
        assert elevation.service_executable() == Path(sys.executable).resolve().parent / (
            "openweir-service.exe"
        )


@pytest.mark.parametrize(
    ("groups", "administrator"),
    [
        (["alice", "sudo"], True),  # Debian, Ubuntu
        (["alice", "wheel"], True),  # Fedora, openSUSE
        (["admin"], True),  # older Ubuntu
        (["alice", "adm", "users", "lpadmin"], False),  # reads logs, manages printers
        ([], False),
    ],
)
def test_who_polkit_asks_as_an_administrator(groups: list[str], administrator: bool) -> None:
    assert elevation.is_admin_group_member(lambda: groups) is administrator


def test_groups_that_cannot_be_read_make_no_administrator() -> None:
    def unreadable() -> list[str]:
        raise KeyError("no such user")

    assert not elevation.is_admin_group_member(unreadable)


@pytest.mark.skipif(sys.platform == "win32", reason="the user database is Unix's")
def test_the_users_own_groups() -> None:
    import grp

    assert grp.getgrgid(os.getgid()).gr_name in elevation.user_groups()


class FakePkexec:
    """Answers as pkexec would, without asking anyone anything."""

    def __init__(self, status: int, stderr: str = "") -> None:
        self.status = status
        self.stderr = stderr
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> tuple[int, str]:
        self.commands.append(list(command))
        return self.status, self.stderr


HELPER = Path("/opt/openweir/openweir-service")


def test_pkexec_runs_the_take_over_helper() -> None:
    pkexec = FakePkexec(0)
    elevation.run_pkexec(HELPER, "-take-over", pkexec)
    assert pkexec.commands == [[PKEXEC, str(HELPER), "-take-over"]]


@pytest.mark.parametrize(
    ("status", "stderr", "code", "message"),
    [
        # The user dismissed polkit's dialog: nothing changed, and nothing to say.
        (126, "", "cancelled", "the authentication dialog was dismissed"),
        # Not an administrator after all, or no agent to ask.
        (
            127,
            "Error executing command as another user: Not authorized\n",
            "not_authorized",
            "Error executing command as another user: Not authorized",
        ),
        (127, "", "not_authorized", "pkexec: not authorized"),
        # The helper ran and failed; it says why on its last line.
        (
            1,
            "starting\ndial unix /run/openweir/control.sock: connect: no such file\n",
            "take_over_failed",
            "dial unix /run/openweir/control.sock: connect: no such file",
        ),
        (1, "", "take_over_failed", "the take-over helper exited with status 1"),
    ],
)
def test_pkexec_failures(status: int, stderr: str, code: str, message: str) -> None:
    with pytest.raises(HostError) as raised:
        elevation.run_pkexec(HELPER, "-take-over", FakePkexec(status, stderr))
    assert (raised.value.code, raised.value.message) == (code, message)


@pytest.mark.skipif(sys.platform == "win32", reason="a stand-in shell script for pkexec")
def test_the_real_runner_reads_the_exit_status_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "pkexec"
    script.write_text('#!/bin/sh\necho "$1 $2" >&2\nexit 7\n')
    script.chmod(0o755)
    assert elevation.run_command([str(script), "helper", "-take-over"]) == (
        7,
        "helper -take-over\n",
    )
    monkeypatch.setattr(elevation, "PKEXEC_WAIT_S", 0.2)
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    with pytest.raises(HostError) as raised:
        elevation.run_command([str(script)])
    assert raised.value.code == "take_over_failed"
    with pytest.raises(HostError) as raised:
        elevation.run_command([str(tmp_path / "missing")])
    assert raised.value.code == "take_over_failed"


def test_an_elevated_user_is_an_administrator() -> None:
    if sys.platform != "win32":
        pytest.skip("UAC is on Windows")
    import ctypes

    if not ctypes.windll.shell32.IsUserAnAdmin():
        pytest.skip("the tests do not run elevated")
    assert elevation.is_administrator()


def test_run_elevated_stops_waiting_for_a_helper_that_does_not_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "win32":
        pytest.skip("UAC is on Windows")
    import ctypes

    if not ctypes.windll.shell32.IsUserAnAdmin():
        pytest.skip("only an elevated process starts another without UAC's prompt")
    monkeypatch.setattr(elevation, "HELPER_WAIT_S", 0.5)
    shell = Path(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"))
    with pytest.raises(HostError) as raised:
        elevation.run_elevated(shell, "/c ping -n 6 127.0.0.1 > nul")
    assert raised.value.code == "take_over_failed"
    assert "did not finish" in raised.value.message


def test_run_elevated_returns_the_exit_status() -> None:
    if sys.platform != "win32":
        pytest.skip("UAC is on Windows")
    import ctypes

    if not ctypes.windll.shell32.IsUserAnAdmin():
        pytest.skip("only an elevated process starts another without UAC's prompt")
    statuses: list[int] = []
    shell = Path(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"))
    # A daemon thread, so that a prompt nobody answers fails the test instead of hanging it.
    thread = threading.Thread(
        target=lambda: statuses.append(elevation.run_elevated(shell, "/c exit 7")), daemon=True
    )
    thread.start()
    thread.join(60)
    assert statuses == [7]
