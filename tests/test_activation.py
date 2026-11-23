"""A later launch's token reaches the UI already running, which shows its window with it
(desktop/activation.py, docs/ui.md §8 "单实例"). The X11 calls go to a stand-in; the real ones
were checked against Xvfb, and only a Linux desktop shows what the window manager does."""

import ctypes
import os
import subprocess
import sys
from functools import partial
from pathlib import Path

import pytest
from PySide6.QtGui import QWindow
from pytestqt.qtbot import QtBot

from openweir.desktop import activation
from openweir.desktop.activation import (
    DESKTOP_STARTUP_ID,
    STARTUP_ID,
    STARTUP_INFO,
    STARTUP_INFO_BEGIN,
    XDG_ACTIVATION_TOKEN,
    complete_launch,
    launch_tokens,
    launched,
)
from openweir.desktop.instance import ACTIVATE, SingleInstance, message

TOKEN = "gnome-shell/OpenWeir/2201-4-host_TIME7731042"
LINK = "openweir://install-config?url=https%3A%2F%2Fsub.example.com%2Fa"


def test_a_later_ui_passes_on_the_tokens_it_was_launched_with() -> None:
    environ = {XDG_ACTIVATION_TOKEN: TOKEN, DESKTOP_STARTUP_ID: TOKEN, "HOME": "/home/ember"}
    assert launch_tokens(environ) == {XDG_ACTIVATION_TOKEN: TOKEN, DESKTOP_STARTUP_ID: TOKEN}
    # What no launcher gives, or what would need quoting, stays behind.
    for bad in ("", "a b", 'a"b', "a\\b", "a\tb", "a\nb", "é", "x" * 1025):
        assert launch_tokens({XDG_ACTIVATION_TOKEN: bad}) == {}


def test_the_token_rides_on_the_line() -> None:
    tokens = {XDG_ACTIVATION_TOKEN: TOKEN, DESKTOP_STARTUP_ID: "kde;1;2_TIME3"}
    assert message(None, tokens) == (
        f"activate\tXDG_ACTIVATION_TOKEN={TOKEN}\tDESKTOP_STARTUP_ID=kde;1;2_TIME3\n".encode()
    )
    assert message(LINK, {DESKTOP_STARTUP_ID: TOKEN}) == (
        f"open {LINK}\tDESKTOP_STARTUP_ID={TOKEN}\n".encode()
    )
    # macOS and Windows have none: the lines are as they always were.
    assert message(None, {}) == ACTIVATE
    assert message(LINK) == f"open {LINK}\n".encode()
    # Nothing in a link or a token can start a field or a line of its own.
    assert message("openweir://a\tb\r\nquit", {"HOME": "x", XDG_ACTIVATION_TOKEN: "a\tb"}) == (
        b"open openweir://abquit\n"
    )


def c_environment(name: str) -> bytes | None:
    """The variable as Qt reads it, from the process's own environment."""
    libc = ctypes.CDLL(None)
    libc.getenv.restype = ctypes.c_char_p
    libc.getenv.argtypes = (ctypes.c_char_p,)
    return libc.getenv(name.encode())


# A later UI launched with a token, in a process of its own: see test_desktop.py.
SECOND_UI = """
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from openweir.desktop.instance import SingleInstance
app = QCoreApplication(sys.argv)
link = sys.argv[2] if len(sys.argv) > 2 else None
sys.exit(1 if SingleInstance(Path(sys.argv[1])).claim(link) else 0)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="reads the C library's environment")
@pytest.mark.parametrize("link", [None, LINK])
def test_on_wayland_the_window_shows_with_the_later_launchs_token(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link: str | None
) -> None:
    monkeypatch.setattr(activation, "launched", partial(launched, platform="wayland"))
    first = SingleInstance(tmp_path)
    assert first.claim()
    seen: list[bytes | None] = []
    shown = first.received if link else first.activated
    shown.connect(lambda *_: seen.append(c_environment(XDG_ACTIVATION_TOKEN)))
    environ = {**os.environ, XDG_ACTIVATION_TOKEN: TOKEN}
    environ.pop(DESKTOP_STARTUP_ID, None)
    command = [sys.executable, "-c", SECOND_UI, str(tmp_path), *([link] if link else [])]
    second = subprocess.Popen(command, env=environ)
    try:
        with qtbot.waitSignal(shown, timeout=8000):
            pass
        assert second.wait(timeout=10) == 0
    finally:
        second.kill()
        second.wait()
        first.release()
    assert seen == [TOKEN.encode()]  # there as the window showed
    assert c_environment(XDG_ACTIVATION_TOKEN) is None  # and gone after: it is used once


@pytest.mark.skipif(sys.platform == "win32", reason="reads the C library's environment")
def test_wayland_takes_the_startup_id_when_that_is_all_there_is() -> None:
    seen: list[bytes | None] = []
    with launched({DESKTOP_STARTUP_ID: TOKEN}, platform="wayland"):
        seen.append(c_environment(XDG_ACTIVATION_TOKEN))
    with launched({}, platform="wayland"):
        seen.append(c_environment(XDG_ACTIVATION_TOKEN))
    assert seen == [TOKEN.encode(), None]


class FakeX11:
    """Records what is set and sent, and the order of the calls; atoms are numbered as they
    are asked for."""

    def __init__(self) -> None:
        self.atoms: dict[bytes, int] = {}
        self.properties: list[tuple[int, int, bytes]] = []  # window, name, value
        self.sent: list[tuple[int, int, int, bytes]] = []  # root, window, kind, piece
        self.order: list[str] = []

    def atom(self, name: bytes) -> int:
        return self.atoms.setdefault(name, 100 + len(self.atoms))

    def root(self, window: int) -> int:
        return 0x1F

    def set_property(self, window: int, name: int, value: bytes) -> bool:
        self.properties.append((window, name, value))
        self.order.append("property")
        return True

    def send_to_root(self, root: int, window: int, kind: int, piece: bytes) -> None:
        self.sent.append((root, window, kind, piece))
        self.order.append("send")

    def flush(self) -> None:
        self.order.append("flush")


def test_on_x11_the_window_names_the_launch_before_it_ends() -> None:
    """The window manager activates the window for a launch it still waits for; the remove
    message ends the launch, in pieces of 20 bytes to the root window."""
    x11 = FakeX11()
    window = 0x3C00007
    complete_launch(x11, [window], TOKEN)
    startup_id, begin, more = (x11.atoms[n] for n in (STARTUP_ID, STARTUP_INFO_BEGIN, STARTUP_INFO))
    assert x11.properties == [(window, startup_id, TOKEN.encode())]
    assert x11.order == ["property"] + ["send"] * len(x11.sent) + ["flush"]
    kinds = [begin] + [more] * (len(x11.sent) - 1)
    assert [(root, to, kind) for root, to, kind, _ in x11.sent] == [
        (0x1F, window, kind) for kind in kinds
    ]
    pieces = [piece for *_, piece in x11.sent]
    assert {len(piece) for piece in pieces} == {20}
    assert b"".join(pieces).rstrip(b"\0") == f"remove: ID={TOKEN}".encode()

    no_window = FakeX11()
    complete_launch(no_window, [], TOKEN)
    assert no_window.order == []


def test_on_x11_the_shown_window_takes_the_launch(qtbot: QtBot) -> None:
    window = QWindow()
    window.show()
    qtbot.waitExposed(window)
    x11 = FakeX11()
    tokens = {XDG_ACTIVATION_TOKEN: "other", DESKTOP_STARTUP_ID: TOKEN}
    try:
        with launched(tokens, "xcb", lambda: x11):
            pass
        assert int(window.winId()) in [marked for marked, _, _ in x11.properties]
        assert {value for _, _, value in x11.properties} == {TOKEN.encode()}

        # Without a startup id, X11 has nothing to go by.
        idle = FakeX11()
        with launched({XDG_ACTIVATION_TOKEN: TOKEN}, "xcb", lambda: idle):
            pass
        assert idle.order == []
    finally:
        window.close()


@pytest.mark.parametrize("platform", ["cocoa", "windows", "offscreen"])
def test_elsewhere_a_token_changes_nothing(platform: str) -> None:
    def refuse() -> FakeX11:
        raise AssertionError("X11 was asked")

    before = dict(os.environ)
    own = None if sys.platform == "win32" else c_environment(XDG_ACTIVATION_TOKEN)
    with launched({XDG_ACTIVATION_TOKEN: TOKEN, DESKTOP_STARTUP_ID: TOKEN}, platform, refuse):
        assert dict(os.environ) == before
        if sys.platform != "win32":
            assert c_environment(XDG_ACTIVATION_TOKEN) == own
