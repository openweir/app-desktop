"""Opening at login on Linux: the XDG autostart entry (desktop/login.py). The tests write in
a scratch directory, never the user's own ~/.config/autostart."""

import sys
from pathlib import Path

import pytest

from openweir.desktop import login
from openweir.desktop.login import (
    AUTOSTART_NAME,
    BACKGROUND_ARG,
    LoginItemError,
    UnavailableLoginItem,
    XdgAutostart,
    autostart_directory,
    autostart_entry,
    entry_enabled,
)
from openweir.product import LINUX_DESKTOP_ID, LINUX_UI_PATH


def read_entry(path: Path) -> dict[str, str]:
    lines = path.read_text("utf-8").splitlines()
    assert lines[0] == "[Desktop Entry]"
    return dict(line.split("=", 1) for line in lines[1:])


def test_the_entry_starts_the_installed_ui_in_the_background(tmp_path: Path) -> None:
    directory = tmp_path / "config" / "autostart"  # made on the first write
    item = XdgAutostart(directory)
    assert item.available
    assert not item.status().enabled
    assert item.set_enabled(True).enabled
    assert item.path == directory / AUTOSTART_NAME == directory / "org.openweir.app.desktop"
    entry = read_entry(item.path)
    assert entry["Type"] == "Application"
    # The installed UI by product.json's path, wherever the package puts it.
    assert entry["Exec"] == f"{LINUX_UI_PATH} {BACKGROUND_ARG}"
    assert entry["TryExec"] == LINUX_UI_PATH  # skipped once the package is gone
    assert entry["Icon"] == LINUX_DESKTOP_ID
    assert entry["X-GNOME-Autostart-enabled"] == "true"
    assert [path.name for path in directory.iterdir()] == [AUTOSTART_NAME]  # nothing left over
    assert item.set_enabled(True).enabled  # again: replaced, still one entry
    assert not item.set_enabled(False).enabled
    assert not item.path.exists()
    assert not item.set_enabled(False).enabled  # nothing to remove


@pytest.mark.parametrize(
    ("change", "enabled"),
    [
        ("", True),
        ("X-GNOME-Autostart-enabled=false\n", False),  # GNOME Tweaks turned it off
        ("Hidden=true\n", False),  # KDE and the specification's way
        ("Hidden=false\n", True),
    ],
)
def test_an_entry_the_user_turned_off(change: str, enabled: bool) -> None:
    text = autostart_entry(LINUX_UI_PATH).replace("X-GNOME-Autostart-enabled=true\n", "")
    assert entry_enabled(text + change) is enabled


def test_keys_of_other_groups_do_not_count() -> None:
    text = autostart_entry(LINUX_UI_PATH) + "[Desktop Action quit]\nHidden=true\n"
    assert entry_enabled(text)


def test_an_entry_turned_off_elsewhere_reads_as_off(tmp_path: Path) -> None:
    item = XdgAutostart(tmp_path)
    item.set_enabled(True)
    text = item.path.read_text("utf-8").replace("enabled=true", "enabled=false")
    item.path.write_text(text, "utf-8")
    assert not item.status().enabled
    assert item.set_enabled(True).enabled  # on again


@pytest.mark.skipif(sys.platform == "win32", reason="XDG paths are POSIX paths")
def test_failures_are_login_item_errors(tmp_path: Path) -> None:
    blocked = tmp_path / "autostart"
    blocked.write_text("not a directory")
    item = XdgAutostart(blocked)
    with pytest.raises(LoginItemError) as raised:
        item.set_enabled(True)
    assert raised.value.code == "login_item_failed"
    assert str(blocked) in raised.value.message
    with pytest.raises(LoginItemError):
        item.status()
    assert list(tmp_path.iterdir()) == [blocked]


@pytest.mark.skipif(sys.platform == "win32", reason="XDG paths are POSIX paths")
def test_where_the_entries_are(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert autostart_directory({}) == tmp_path / ".config" / "autostart"
    assert autostart_directory({"XDG_CONFIG_HOME": "/srv/alice"}) == Path("/srv/alice/autostart")
    # A relative path is to be ignored (XDG Base Directory Specification).
    assert autostart_directory({"XDG_CONFIG_HOME": "cfg"}) == tmp_path / ".config" / "autostart"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux's login item")
def test_only_the_installed_package_opens_at_login(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(login.default_login_item(), UnavailableLoginItem)  # from source
    monkeypatch.setattr(login, "PACKAGED", True)
    monkeypatch.setattr(login, "installed_package_format", lambda: None)  # not the package
    assert isinstance(login.default_login_item(), UnavailableLoginItem)
    monkeypatch.setattr(login, "installed_package_format", lambda: "rpm")
    assert isinstance(login.default_login_item(), XdgAutostart)
