"""Removing the user's link registrations that open a program no longer there
(docs/release.md §6, D45). On Windows the rules run on a fake registry and a fake file system;
only the Windows-only test at the end touches the real registry, with a scratch key of its
own. On Linux they run on a mimeapps.list and desktop entries in a scratch directory."""

from __future__ import annotations

import contextlib
import logging
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from openweir.desktop import associations
from openweir.desktop.associations import (
    WindowsUserClasses,
    application_dirs,
    candidates,
    clear_stale,
    clear_stale_defaults,
    clear_stale_in_background,
    default_gone,
    is_stale,
    mimeapps_path,
    program_gone,
)
from openweir.desktop.links import SCHEMES

ENVIRON = {
    "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
    "SystemRoot": r"C:\Windows",
    "ProgramFiles": r"C:\Program Files",
}
FILES = {
    "C:\\",
    "D:\\",
    r"C:\Program Files\Clash Verge\clash-verge.exe",
    r"C:\Users\me\AppData\Local\Programs\FlClash\FlClash.exe",
    r"C:\Tools\clash.exe",
    r"D:\Portable\Meta.exe",
}
GONE = r'"C:\Users\me\AppData\Local\Programs\Clash Party\Clash Party.exe" "%1"'
VALID = r'"C:\Program Files\Clash Verge\clash-verge.exe" "%1"'


def exists(path: str) -> bool:
    """The fake file system: Windows' paths ignore case."""
    return path.lower() in {file.lower() for file in FILES}


class FakeClasses:
    """``HKCU\\Software\\Classes``: each scheme's command, or an error for it."""

    def __init__(self, commands: dict[str, str | OSError]) -> None:
        self.commands = dict(commands)
        self.asked: list[str] = []
        self.deleted: list[str] = []
        self.refuse_delete: set[str] = set()

    def command(self, scheme: str) -> str | None:
        self.asked.append(scheme)
        command = self.commands.get(scheme)
        if isinstance(command, OSError):
            raise command
        return command

    def delete(self, scheme: str) -> None:
        if scheme in self.refuse_delete:
            raise PermissionError(13, "Access is denied")
        self.deleted.append(scheme)
        del self.commands[scheme]


@pytest.mark.parametrize(
    ("command", "paths"),
    [
        # Quoted, with the link after it: the program ends at the quote.
        (GONE, [r"C:\Users\me\AppData\Local\Programs\Clash Party\Clash Party.exe"]),
        (r'"C:\Tools\clash.exe"%1', [r"C:\Tools\clash.exe"]),
        # Without quotes: each stretch up to a space, as CreateProcess tries them.
        (r"C:\Tools\clash.exe %1", [r"C:\Tools\clash.exe", r"C:\Tools\clash.exe %1"]),
        (
            r'C:\Program Files\Old Client\old.exe "%1"',
            [
                r"C:\Program",
                r"C:\Program.exe",
                r"C:\Program Files\Old",
                r"C:\Program Files\Old.exe",
                r"C:\Program Files\Old Client\old.exe",
            ],
        ),
        # A name without an extension: CreateProcess adds .exe.
        (r'"C:\Tools\clash" "%1"', [r"C:\Tools\clash", r"C:\Tools\clash.exe"]),
        # REG_EXPAND_SZ's variables, whatever their case.
        (
            r'"%LocalAppData%\Programs\FlClash\FlClash.exe" "%1"',
            [r"C:\Users\me\AppData\Local\Programs\FlClash\FlClash.exe"],
        ),
        (
            r"%SYSTEMROOT%\clash.exe %1",
            [r"C:\Windows\clash.exe", r"C:\Windows\clash.exe %1"],
        ),
        (r'  "C:\Tools\clash.exe" --url "%1"  ', [r"C:\Tools\clash.exe"]),
    ],
)
def test_where_the_program_may_be(command: str, paths: list[str]) -> None:
    assert candidates(command, ENVIRON) == paths


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        'clash.exe "%1"',  # found on the PATH
        "rundll32.exe url.dll,FileProtocolHandler %1",
        r'"C:\Tools\clash.exe %1',  # no closing quote
        r'"\\server\share\clash.exe" "%1"',  # perhaps only out of reach
        r'"%UNSET%\clash.exe" "%1"',
        r'"%LOCALAPPDATA" "%1"',
        r"C:clash.exe %1",  # relative to the drive's current folder
        r"\Tools\clash.exe %1",  # relative to the current drive
        r'"C:\Tools\cla*.exe" "%1"',
        r'"C:\Tools\a:b.exe" "%1"',
    ],
)
def test_commands_that_name_no_certain_program(command: str) -> None:
    assert candidates(command, ENVIRON) is None
    assert is_stale(command, ENVIRON, exists) is None


@pytest.mark.parametrize(
    ("command", "stale"),
    [
        (GONE, True),
        (VALID, False),
        (VALID.upper(), False),
        (r"C:\Tools\clash.exe %1", False),
        (r"C:\Tools\gone.exe %1", True),
        (r'"C:\Tools\clash" "%1"', False),
        (r'"%LOCALAPPDATA%\Programs\FlClash\FlClash.exe" "%1"', False),
        (r'"%LOCALAPPDATA%\Programs\Gone\Gone.exe" "%1"', True),
        (r'C:\Program Files\Clash Verge\clash-verge.exe "%1"', False),
        (r'C:\Program Files\Gone Client\gone.exe "%1"', True),
        (r'"D:\Portable\Meta.exe" "%1"', False),
        (r'"D:\Portable\Gone.exe" "%1"', True),
        # The drive is not there (unplugged, not mapped): the program may come back with it.
        (r'"E:\Portable\Meta.exe" "%1"', None),
    ],
)
def test_whether_the_program_is_gone(command: str, stale: bool | None) -> None:
    assert is_stale(command, ENVIRON, exists) is stale


def test_only_registrations_of_a_missing_program_go(caplog: pytest.LogCaptureFixture) -> None:
    classes = FakeClasses(
        {"openweir": r'"C:\Old\OpenWeir.exe" "%1"', "clash": GONE, "mihomo": VALID}
    )
    with caplog.at_level(logging.INFO, logger=associations.__name__):
        cleared = clear_stale(classes, ENVIRON, exists)
    assert cleared == classes.deleted == ["openweir", "clash"]
    assert classes.asked == list(SCHEMES)
    assert classes.commands == {"mihomo": VALID}  # another client, still installed
    # What went and where it pointed, for the diagnosis.
    removed = [record.getMessage() for record in caplog.records]
    assert len(removed) == 2
    assert "clash:" in removed[1]
    assert repr(GONE) in removed[1]
    assert all(record.levelno == logging.WARNING for record in caplog.records)


def test_no_registration_of_its_own_is_nothing_to_do(caplog: pytest.LogCaptureFixture) -> None:
    classes = FakeClasses({})
    with caplog.at_level(logging.INFO, logger=associations.__name__):
        assert clear_stale(classes, ENVIRON, exists) == []
    assert classes.asked == list(SCHEMES)
    assert caplog.records == []


def test_a_command_it_cannot_read_stays(caplog: pytest.LogCaptureFixture) -> None:
    classes = FakeClasses({"clash": "rundll32.exe url.dll,FileProtocolHandler %1"})
    with caplog.at_level(logging.INFO, logger=associations.__name__):
        assert clear_stale(classes, ENVIRON, exists) == []
    assert classes.deleted == []
    (record,) = caplog.records
    assert "clash:" in record.getMessage()
    assert "rundll32.exe" in record.getMessage()


def test_failures_leave_that_scheme_and_go_on(caplog: pytest.LogCaptureFixture) -> None:
    classes = FakeClasses(
        {
            "openweir": PermissionError(13, "Access is denied"),
            "clash": GONE,
            "mihomo": r'"C:\Locked\Gone.exe" "%1"',
            "clashmeta": r'"C:\Tools\gone.exe" "%1"',
        }
    )
    classes.refuse_delete.add("clash")

    def locked(path: str) -> bool:
        if path.startswith("C:\\Locked\\"):
            raise PermissionError(13, "Access is denied")
        return exists(path)

    with caplog.at_level(logging.INFO, logger=associations.__name__):
        assert clear_stale(classes, ENVIRON, locked) == ["clashmeta"]
    assert set(classes.commands) == {"openweir", "clash", "mihomo"}
    # One line each: three could not be checked, one went.
    messages = [record.getMessage() for record in caplog.records]
    named = [scheme for message in messages for scheme in SCHEMES if f" {scheme}: " in message]
    assert named == list(SCHEMES)
    assert messages[-1].startswith("removed")


class InvalidNameError(OSError):
    winerror = 123  # ERROR_INVALID_NAME, which Python reports as EINVAL


@pytest.mark.parametrize(
    ("error", "there"),
    [
        (None, True),
        (FileNotFoundError(2, "gone"), False),
        (NotADirectoryError(20, "a file on the way"), False),
        (InvalidNameError(22, "bad name"), False),
        (PermissionError(13, "denied"), None),  # perhaps there: an error
        (OSError(21, "the device is not ready"), None),
    ],
)
def test_the_file_system_answer(
    monkeypatch: pytest.MonkeyPatch, error: OSError | None, there: bool | None
) -> None:
    def stat(path: str) -> None:
        if error is not None:
            raise error

    # The module's os only: the tests' own calls keep the real one.
    monkeypatch.setattr(associations, "os", SimpleNamespace(stat=stat))
    if error is not None and there is None:
        with pytest.raises(type(error), match=error.strerror):
            associations.exists(r"C:\Tools\clash.exe")
    else:
        assert associations.exists(r"C:\Tools\clash.exe") is there


@pytest.mark.parametrize(
    ("platform", "qt_platform", "runs"),
    [
        ("win32", "windows", True),
        ("win32", "offscreen", False),  # the tests' platform on Windows
        ("darwin", "cocoa", False),
        ("linux", "xcb", False),
    ],
)
def test_only_windows_own_platform_looks(
    monkeypatch: pytest.MonkeyPatch, platform: str, qt_platform: str, runs: bool
) -> None:
    classes = FakeClasses({"clash": GONE})
    monkeypatch.setattr(associations, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(associations, "WindowsUserClasses", lambda: classes)
    monkeypatch.setattr(associations, "installed_package_format", lambda: None)
    thread = clear_stale_in_background(qt_platform)
    assert (thread is not None) is runs
    if thread is not None:
        assert thread.daemon
        thread.join(timeout=10)
        assert classes.asked == list(SCHEMES)
    else:
        assert classes.asked == []


# -- Linux -----------------------------------------------------------------------------------

linux_paths = pytest.mark.skipif(sys.platform == "win32", reason="Linux's paths and file modes")


class Desktop:
    """A user's mimeapps.list and the applications directories, in a scratch directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = root / "config"
        self.config.mkdir()
        self.user = root / "data" / "applications"
        self.system = root / "usr" / "share" / "applications"
        for directory in (self.user, self.system):
            directory.mkdir(parents=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.mimeapps = self.config / "mimeapps.list"

    @property
    def dirs(self) -> list[Path]:
        return [self.user, self.system]

    def program(self, name: str) -> str:
        path = self.bin / name
        path.write_text("#!/bin/sh\n")
        return str(path)

    def entry(
        self, relative: str, exec_line: str, *, directory: Path | None = None, extra: str = ""
    ) -> None:
        path = (directory or self.user) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"[Desktop Entry]\nType=Application\nName=Client\nExec={exec_line}\n{extra}"
        )


@pytest.fixture
def desktop(tmp_path: Path) -> Desktop:
    return Desktop(tmp_path)


@linux_paths
def test_a_default_whose_program_is_gone_goes(
    desktop: Desktop, caplog: pytest.LogCaptureFixture
) -> None:
    gone = str(desktop.bin / "clash-party.AppImage")
    desktop.entry("clash-party.desktop", f"{gone} %u")
    desktop.entry("clash-verge.desktop", f"{desktop.program('clash-verge')} %u")
    text = (
        "# kept as it is\r\n"
        "[Default Applications]\r\n"
        "text/html=firefox.desktop\r\n"
        "x-scheme-handler/clash=clash-party.desktop\r\n"
        "x-scheme-handler/mihomo=clash-verge.desktop;\r\n"
        "x-scheme-handler/http=clash-party.desktop\r\n"  # not one of OpenWeir's schemes
        "\r\n"
        "[Added Associations]\r\n"
        "x-scheme-handler/clash=clash-party.desktop;\r\n"
    )
    desktop.mimeapps.write_bytes(text.encode())
    desktop.mimeapps.chmod(0o600)
    with caplog.at_level(logging.WARNING):
        assert clear_stale_defaults(desktop.mimeapps, desktop.dirs) == ["clash"]
    expected = text.replace("x-scheme-handler/clash=clash-party.desktop\r\n", "", 1)
    assert desktop.mimeapps.read_bytes() == expected.encode()  # nothing else changed
    assert desktop.mimeapps.stat().st_mode & 0o777 == 0o600
    assert [path.name for path in desktop.config.iterdir()] == ["mimeapps.list"]
    assert "clash: links" in caplog.text
    assert "clash-party.desktop" in caplog.text
    # The next start finds nothing to do, and writes nothing.
    before = desktop.mimeapps.stat().st_mtime_ns
    assert clear_stale_defaults(desktop.mimeapps, desktop.dirs) == []
    assert desktop.mimeapps.stat().st_mtime_ns == before


@linux_paths
def test_a_default_whose_entry_is_gone_goes(desktop: Desktop) -> None:
    desktop.mimeapps.write_text("[Default Applications]\nx-scheme-handler/Clash=removed.desktop;\n")
    assert clear_stale_defaults(desktop.mimeapps, desktop.dirs) == ["clash"]
    assert desktop.mimeapps.read_text() == "[Default Applications]\n"


@linux_paths
def test_the_first_entry_that_is_there_opens_the_link(desktop: Desktop) -> None:
    program = desktop.program("clash-verge")
    desktop.entry("clash-verge.desktop", f"{program} %u", directory=desktop.system)
    exists = {program}.__contains__
    assert default_gone("removed.desktop;clash-verge.desktop;", desktop.dirs, exists) is False
    assert default_gone("removed.desktop;other.desktop", desktop.dirs, exists) is True
    assert default_gone(" ; ", desktop.dirs, exists) is None


@linux_paths
@pytest.mark.parametrize(
    ("entry", "gone"),
    [
        ("Exec=/opt/gone/client --minimized %u", True),
        # Unquoted, the program ends at the space, unlike on Windows.
        ("Exec=/opt/Old Client/client %u", False),
        ('Exec="/opt/Old Client/client" %u', True),
        ("Exec=/usr/bin/clash-verge %u", False),
        ("Exec=clash-verge %u", None),  # found on the PATH, which the session may set otherwise
        ("Exec=env GDK_BACKEND=x11 /opt/gone/client %u", None),  # through another program
        ("Exec=/usr/bin/flatpak run io.github.clash %u", False),
        ("Exec=/usr/bin/clash-verge %u\nDBusActivatable=true", None),
        ("TryExec=/opt/gone/client\nExec=/usr/bin/clash-verge %u", True),
        ('Exec="/opt/unclosed %u', None),
        ("Exec=", None),
        ("", None),
        # Only the [Desktop Entry] group counts, without localized keys or comments.
        ("Exec=/usr/bin/clash-verge %u\n[Desktop Action new]\nExec=/opt/gone/client", False),
    ],
)
def test_which_program_an_entry_opens(entry: str, gone: bool | None) -> None:
    files = {"/usr/bin/clash-verge", "/usr/bin/flatpak", "/opt/Old"}
    assert program_gone(f"[Desktop Entry]\nType=Application\n{entry}\n", files.__contains__) is gone


@linux_paths
def test_a_default_it_cannot_judge_stays(
    desktop: Desktop, caplog: pytest.LogCaptureFixture
) -> None:
    desktop.entry("flclash.desktop", "flclash %u")
    text = "[Default Applications]\nx-scheme-handler/clashmeta=flclash.desktop\n"
    desktop.mimeapps.write_text(text)
    with caplog.at_level(logging.WARNING):
        assert clear_stale_defaults(desktop.mimeapps, desktop.dirs) == []
    assert desktop.mimeapps.read_text() == text
    assert "cannot tell" in caplog.text


@linux_paths
def test_an_entry_in_a_subdirectory_is_there(desktop: Desktop) -> None:
    # The desktop file ID kde-clash.desktop names applications/kde/clash.desktop too.
    desktop.entry("kde/clash.desktop", f"{desktop.program('clash')} %u", directory=desktop.system)
    text = "[Default Applications]\nx-scheme-handler/clash=kde-clash.desktop\n"
    desktop.mimeapps.write_text(text)
    assert clear_stale_defaults(desktop.mimeapps, desktop.dirs) == []
    assert desktop.mimeapps.read_text() == text


@linux_paths
def test_a_linked_file_is_left_alone(desktop: Desktop, caplog: pytest.LogCaptureFixture) -> None:
    dotfiles = desktop.root / "dotfiles-mimeapps.list"
    text = "[Default Applications]\nx-scheme-handler/clash=removed.desktop\n"
    dotfiles.write_text(text)
    desktop.mimeapps.symlink_to(dotfiles)
    with caplog.at_level(logging.WARNING):
        assert clear_stale_defaults(desktop.mimeapps, desktop.dirs) == []
    assert desktop.mimeapps.is_symlink()
    assert dotfiles.read_text() == text


@linux_paths
def test_no_file_and_an_unreadable_file_are_nothing_to_do(desktop: Desktop) -> None:
    assert clear_stale_defaults(desktop.mimeapps, desktop.dirs) == []
    assert not desktop.mimeapps.exists()
    desktop.mimeapps.write_bytes(b"[Default Applications]\nx-scheme-handler/clash=\xff.desktop\n")
    assert clear_stale_defaults(desktop.mimeapps, desktop.dirs) == []


@linux_paths
def test_a_file_it_cannot_write_stays(desktop: Desktop, monkeypatch: pytest.MonkeyPatch) -> None:
    text = "[Default Applications]\nx-scheme-handler/clash=removed.desktop\n"
    desktop.mimeapps.write_text(text)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(associations.os, "replace", refuse)
    assert clear_stale_defaults(desktop.mimeapps, desktop.dirs) == []
    assert desktop.mimeapps.read_text() == text
    assert [path.name for path in desktop.config.iterdir()] == ["mimeapps.list"]


@linux_paths
def test_where_the_files_are(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert mimeapps_path({}) == tmp_path / ".config" / "mimeapps.list"
    assert mimeapps_path({"XDG_CONFIG_HOME": "/cfg"}) == Path("/cfg/mimeapps.list")
    assert mimeapps_path({"XDG_CONFIG_HOME": "relative"}) == tmp_path / ".config" / "mimeapps.list"
    dirs = application_dirs({"XDG_DATA_DIRS": "/usr/share/gnome:relative:/usr/share"})
    assert dirs[:3] == [
        tmp_path / ".local/share/applications",
        Path("/usr/share/gnome/applications"),
        Path("/usr/share/applications"),
    ]
    assert Path("/var/lib/flatpak/exports/share/applications") in dirs
    default = application_dirs({"XDG_DATA_HOME": "/data"})
    assert default[:3] == [
        Path("/data/applications"),
        Path("/usr/local/share/applications"),
        Path("/usr/share/applications"),
    ]


@linux_paths
@pytest.mark.parametrize(
    ("qt_platform", "package", "runs"),
    [
        ("xcb", "deb", True),
        ("wayland", "rpm", True),
        ("wayland-egl", "deb", True),
        ("xcb", None, False),  # run from source: no registration of its own to make way for
        ("offscreen", "deb", False),  # the tests' platform
    ],
)
def test_on_linux_only_the_installed_package_looks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    qt_platform: str,
    package: str | None,
    runs: bool,
) -> None:
    calls: list[tuple[Path, list[Path]]] = []
    monkeypatch.setattr(associations, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(associations, "installed_package_format", lambda: package)
    monkeypatch.setattr(
        associations, "clear_stale_defaults", lambda path, dirs: calls.append((path, dirs))
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    thread = clear_stale_in_background(qt_platform)
    assert (thread is not None) is runs
    if thread is not None:
        thread.join(timeout=10)
        assert [path for path, _dirs in calls] == [tmp_path / "mimeapps.list"]
    else:
        assert calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="the real registry is Windows'")
def test_the_registry_under_a_scratch_key() -> None:
    assert sys.platform == "win32"
    import winreg

    # Not a URL scheme (no "URL Protocol"), and a name no program uses.
    scheme = f"openweir-test-{uuid.uuid4().hex}"
    path = rf"{associations.CLASSES}\{scheme}"
    classes = WindowsUserClasses()
    try:
        assert classes.command(scheme) is None
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{path}\DefaultIcon") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "icon")
        assert classes.command(scheme) is None  # a key without a command
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{path}\{associations.COMMAND}") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_EXPAND_SZ, GONE)
        assert classes.command(scheme) == GONE
        classes.delete(scheme)
        with pytest.raises(FileNotFoundError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, path).Close()
    finally:
        with contextlib.suppress(FileNotFoundError):
            classes.delete(scheme)
