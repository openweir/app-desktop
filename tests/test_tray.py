"""Whether there is a tray on Linux, and how light its panel is (desktop/tray.py, D65), asked
of a stand-in for the session bus that answers with jeepney's own messages. jeepney comes with
keyring on Linux only."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from openweir.desktop import tray
from openweir.desktop.tray import (
    PORTAL,
    WATCHER,
    panel_is_light,
    session_bus_address,
    status_notifier_host,
    system_prefers_light,
    taskbar_is_light,
    tray_supported,
)

UNKNOWN = "org.freedesktop.DBus.Error.ServiceUnknown"


@pytest.fixture
def jeepney() -> None:
    pytest.importorskip("jeepney")


class FakeBus:
    """Answers ``replies[(destination, member)]``: a (signature, body) pair, or an error
    name. What it was asked is in ``asked``."""

    def __init__(self, replies: dict[tuple[str, str], tuple[str, tuple[Any, ...]] | str]) -> None:
        self.replies = replies
        self.asked: list[tuple[str, str, tuple[Any, ...]]] = []

    def send_and_get_reply(self, message: Any, *, timeout: float | None = None) -> Any:
        from jeepney import HeaderFields, new_error, new_method_return  # pyright: ignore[reportMissingImports]  # Linux only

        assert timeout is not None, "a bus that does not answer must not hold up the UI"
        fields = message.header.fields
        key = (fields[HeaderFields.destination], fields[HeaderFields.member])
        self.asked.append((*key, message.body))
        reply = self.replies.get(key, UNKNOWN)
        if isinstance(reply, str):
            return new_error(message, reply)
        return new_method_return(message, *reply)


def opener(bus: FakeBus) -> Any:
    @contextmanager
    def open_bus() -> Iterator[FakeBus]:
        yield bus

    return open_bus


def watcher(*, running: bool, host: bool) -> FakeBus:
    return FakeBus(
        {
            ("org.freedesktop.DBus", "NameHasOwner"): ("b", (running,)),
            (WATCHER, "Get"): ("v", (("b", host),)),
        }
    )


@pytest.mark.usefixtures("jeepney")
def test_a_panel_that_hosts_status_notifier_items_is_a_tray() -> None:
    bus = watcher(running=True, host=True)
    assert status_notifier_host(opener(bus))
    assert bus.asked == [
        ("org.freedesktop.DBus", "NameHasOwner", (WATCHER,)),
        (WATCHER, "Get", (WATCHER, "IsStatusNotifierHostRegistered")),
    ]


@pytest.mark.usefixtures("jeepney")
def test_gnome_without_the_appindicator_extension_has_no_tray() -> None:
    # No watcher: it is not started just to be asked.
    bus = watcher(running=False, host=True)
    assert not status_notifier_host(opener(bus))
    assert [member for _, member, _ in bus.asked] == ["NameHasOwner"]
    # A watcher whose host has gone, as when the extension is switched off.
    assert not status_notifier_host(opener(watcher(running=True, host=False)))
    # A watcher that does not answer.
    refused = watcher(running=True, host=True)
    refused.replies[(WATCHER, "Get")] = "org.freedesktop.DBus.Error.NoReply"
    assert not status_notifier_host(opener(refused))


def test_no_session_bus_is_no_tray(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_bus() -> Any:
        raise OSError("connection refused")

    assert not status_notifier_host(no_bus)
    monkeypatch.setitem(sys.modules, "jeepney", None)  # a build without it
    assert not status_notifier_host(opener(watcher(running=True, host=True)))


def test_only_linux_asks_the_bus() -> None:
    asked: list[bool] = []

    def host(answer: bool) -> Any:
        def ask() -> bool:
            asked.append(answer)
            return answer

        return ask

    assert tray_supported("cocoa", host(False))
    assert tray_supported("windows", host(False))
    assert not tray_supported("offscreen", host(True))
    assert asked == []
    assert tray_supported("xcb", host(True))
    assert tray_supported("wayland", host(True))
    assert not tray_supported("wayland", host(False))
    assert asked == [True, True, False]


def test_the_session_bus_is_where_libdbus_looks(tmp_path: Path) -> None:
    assert session_bus_address({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}) == "unix:path=/x"
    assert session_bus_address({"XDG_RUNTIME_DIR": str(tmp_path)}) is None
    (tmp_path / "bus").touch()
    found = session_bus_address({"XDG_RUNTIME_DIR": str(tmp_path)})
    assert found == f"unix:path={tmp_path / 'bus'}"
    assert session_bus_address({}) is None


@pytest.mark.usefixtures("jeepney")
@pytest.mark.parametrize(("scheme", "light"), [(2, True), (1, False), (0, False)])
def test_the_portal_says_whether_the_system_is_light(scheme: int, light: bool) -> None:
    # The portal's Read answers with a variant in a variant.
    bus = FakeBus({(PORTAL, "Read"): ("v", (("v", ("u", scheme)),))})
    assert system_prefers_light(opener(bus)) is light
    assert bus.asked == [(PORTAL, "Read", ("org.freedesktop.appearance", "color-scheme"))]
    assert not system_prefers_light(opener(FakeBus({})))  # no portal


def test_gnomes_top_bar_is_dark_whatever_the_style() -> None:
    def refuse() -> bool:
        raise AssertionError("the portal was asked")

    assert not panel_is_light({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, refuse)
    assert panel_is_light({"XDG_CURRENT_DESKTOP": "KDE"}, lambda: True)
    assert not panel_is_light({"XDG_CURRENT_DESKTOP": "KDE"}, lambda: False)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux's panel")
def test_off_the_desktop_platforms_the_panel_is_not_asked(
    qapp: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> bool:
        raise AssertionError("the portal was asked")

    monkeypatch.setattr(tray, "system_prefers_light", refuse)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert not taskbar_is_light()  # the offscreen platform
