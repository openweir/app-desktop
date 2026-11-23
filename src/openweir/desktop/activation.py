"""Bringing the window to the front for a later launch on Linux (docs/ui.md §8, "单实例").

Wayland and X11 let a program take the focus only with proof that the user just asked for it:
the launcher (the dock, the app grid, a file manager) hands it to the process it starts, as
XDG_ACTIVATION_TOKEN on Wayland or DESKTOP_STARTUP_ID, X11's startup notification. A later UI
exits at once, so it passes its own to the UI already running (desktop/instance.py), which
shows its window with it:

- Wayland: Qt activates a window with the token in XDG_ACTIVATION_TOKEN as it next shows or
  activates it, and removes it. Launchers built on an older GLib pass the token as
  DESKTOP_STARTUP_ID alone.
- X11: the window manager activates a window whose _NET_STARTUP_ID names a launch it is
  waiting for, with that launch's time, so focus stealing prevention allows it (Mutter and KWin
  do this for a window already shown, too). The launch's "remove" message then ends its busy
  cursor, which the later UI, gone without a window, would leave spinning.

Without a token, and on macOS and Windows, the window shows as before.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QNativeInterface

XDG_ACTIVATION_TOKEN = "XDG_ACTIVATION_TOKEN"
DESKTOP_STARTUP_ID = "DESKTOP_STARTUP_ID"
TOKEN_NAMES = (XDG_ACTIVATION_TOKEN, DESKTOP_STARTUP_ID)
# Printable ASCII without spaces, quotes or backslashes: what launchers use, safe in the
# single instance's message line, and needing no quoting in a startup notification message.
_TOKEN = re.compile(r"[!#-\[\]-~]{1,1024}")
# Startup notification messages go to the root window in pieces of 20 bytes.
STARTUP_INFO_BEGIN = b"_NET_STARTUP_INFO_BEGIN"
STARTUP_INFO = b"_NET_STARTUP_INFO"
STARTUP_ID = b"_NET_STARTUP_ID"
PIECE = 20

_log = logging.getLogger(__name__)


def valid(token: str) -> bool:
    return _TOKEN.fullmatch(token) is not None


def launch_tokens(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The tokens this process was launched with. ``os.environ`` is read as the interpreter
    started: Qt has removed DESKTOP_STARTUP_ID from the process by now, but not from it."""
    return {name: environ[name] for name in TOKEN_NAMES if valid(environ.get(name, ""))}


class X11(Protocol):
    """The X11 calls ``complete_launch`` makes; tests pass a stand-in."""

    def atom(self, name: bytes) -> int: ...
    def root(self, window: int) -> int: ...
    def set_property(self, window: int, name: int, value: bytes) -> bool: ...
    def send_to_root(self, root: int, window: int, kind: int, piece: bytes) -> None: ...
    def flush(self) -> None: ...


def complete_launch(x11: X11, windows: Sequence[int], startup_id: str) -> None:
    """X11: let the window manager activate ``windows`` (their X ids) for the launch
    ``startup_id``, then end the launch. The property comes first: the window manager only
    knows the launch until it ends."""
    if not windows:
        return
    value = startup_id.encode("ascii")
    name = x11.atom(STARTUP_ID)
    for window in windows:
        if not x11.set_property(window, name, value):
            _log.warning("could not mark window %#x with its launch", window)
    message = b"remove: ID=" + value + b"\0"
    begin, more = x11.atom(STARTUP_INFO_BEGIN), x11.atom(STARTUP_INFO)
    root = x11.root(windows[0])
    for start in range(0, len(message), PIECE):
        piece = message[start : start + PIECE].ljust(PIECE, b"\0")
        x11.send_to_root(root, windows[0], begin if start == 0 else more, piece)
    x11.flush()


@contextmanager
def launched(
    tokens: Mapping[str, str],
    platform: str | None = None,
    x11: Callable[[], X11 | None] | None = None,
) -> Iterator[None]:
    """Around showing the window for a later launch that passed ``tokens``: hand the platform
    the launch's token (``platform`` is QGuiApplication.platformName()). ``x11`` gives the X11
    calls, by default through Qt's own connection."""
    platform = QGuiApplication.platformName() if platform is None else platform
    if platform.startswith("wayland"):
        token = tokens.get(XDG_ACTIVATION_TOKEN) or tokens.get(DESKTOP_STARTUP_ID)
        if token is None:
            yield
            return
        # The process's own environment, where Qt reads it; os.environ is left as it was.
        os.putenv(XDG_ACTIVATION_TOKEN, token)
        try:
            yield
        finally:
            # Qt took it if the window showed; one left over would be stale next time.
            os.unsetenv(XDG_ACTIVATION_TOKEN)
        return
    yield
    startup_id = tokens.get(DESKTOP_STARTUP_ID)
    if platform == "xcb" and startup_id is not None:
        connection = (x11 or qt_x11)()
        if connection is not None:
            complete_launch(connection, shown_windows(), startup_id)


def shown_windows() -> list[int]:
    """The X ids of the application's shown top-level windows: the main window."""
    return [
        int(window.winId())
        for window in QGuiApplication.topLevelWindows()
        if window.isVisible() and window.type() == Qt.WindowType.Window
    ]


# -- X11 through Qt's xcb connection ---------------------------------------------------------
XCB_PROP_MODE_REPLACE = 0
XCB_ATOM_NONE = 0
XCB_CLIENT_MESSAGE = 33
XCB_EVENT_MASK_PROPERTY_CHANGE = 1 << 22


class _Cookie(ctypes.Structure):
    _fields_ = (("sequence", ctypes.c_uint),)


class _AtomReply(ctypes.Structure):
    _fields_ = (
        ("response_type", ctypes.c_uint8),
        ("pad0", ctypes.c_uint8),
        ("sequence", ctypes.c_uint16),
        ("length", ctypes.c_uint32),
        ("atom", ctypes.c_uint32),
    )


class _TreeReply(ctypes.Structure):
    _fields_ = (
        ("response_type", ctypes.c_uint8),
        ("pad0", ctypes.c_uint8),
        ("sequence", ctypes.c_uint16),
        ("length", ctypes.c_uint32),
        ("root", ctypes.c_uint32),
        ("parent", ctypes.c_uint32),
        ("children_len", ctypes.c_uint16),
        ("pad1", ctypes.c_uint8 * 14),
    )


class _ClientMessage(ctypes.Structure):
    _fields_ = (
        ("response_type", ctypes.c_uint8),
        ("format", ctypes.c_uint8),
        ("sequence", ctypes.c_uint16),
        ("window", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("data", ctypes.c_uint8 * PIECE),
    )


def _bind(library: ctypes.CDLL, name: str, restype: Any, *argtypes: Any) -> Any:
    function = getattr(library, name)
    function.restype, function.argtypes = restype, list(argtypes)
    return function


class Xcb:
    """X11 for ``complete_launch`` through libxcb on ``connection`` (an xcb_connection_t *).
    On Qt's own connection the requests follow Qt's, so the window they name exists by then;
    the checked ones report an error here rather than to Qt."""

    def __init__(self, connection: int) -> None:
        xcb = ctypes.CDLL("libxcb.so.1")
        libc = ctypes.CDLL(None)
        pointer, cookie = ctypes.c_void_p, _Cookie
        u8, u16, u32 = ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32
        self._connection = pointer(connection)
        self._free = _bind(libc, "free", None, pointer)
        self._intern = _bind(xcb, "xcb_intern_atom", cookie, pointer, u8, u16, ctypes.c_char_p)
        self._intern_reply = _bind(
            xcb, "xcb_intern_atom_reply", ctypes.POINTER(_AtomReply), pointer, cookie, pointer
        )
        self._tree = _bind(xcb, "xcb_query_tree", cookie, pointer, u32)
        self._tree_reply = _bind(
            xcb, "xcb_query_tree_reply", ctypes.POINTER(_TreeReply), pointer, cookie, pointer
        )
        self._change = _bind(
            xcb,
            "xcb_change_property_checked",
            cookie,
            pointer,
            u8,
            u32,
            u32,
            u32,
            u8,
            u32,
            ctypes.c_char_p,
        )
        self._check = _bind(xcb, "xcb_request_check", pointer, pointer, cookie)
        self._send = _bind(xcb, "xcb_send_event", cookie, pointer, u8, u32, u32, ctypes.c_char_p)
        self._flush = _bind(xcb, "xcb_flush", ctypes.c_int, pointer)

    def atom(self, name: bytes) -> int:
        reply = self._intern_reply(
            self._connection, self._intern(self._connection, 0, len(name), name), None
        )
        if not reply:
            return XCB_ATOM_NONE
        try:
            return reply.contents.atom
        finally:
            self._free(ctypes.cast(reply, ctypes.c_void_p))

    def root(self, window: int) -> int:
        reply = self._tree_reply(self._connection, self._tree(self._connection, window), None)
        if not reply:
            return 0
        try:
            return reply.contents.root
        finally:
            self._free(ctypes.cast(reply, ctypes.c_void_p))

    def set_property(self, window: int, name: int, value: bytes) -> bool:
        utf8 = self.atom(b"UTF8_STRING")
        cookie = self._change(
            self._connection, XCB_PROP_MODE_REPLACE, window, name, utf8, 8, len(value), value
        )
        error = self._check(self._connection, cookie)
        if error:
            self._free(error)
            return False
        return True

    def send_to_root(self, root: int, window: int, kind: int, piece: bytes) -> None:
        event = _ClientMessage(XCB_CLIENT_MESSAGE, 8, 0, window, kind)
        ctypes.memmove(event.data, piece, PIECE)
        raw = ctypes.string_at(ctypes.addressof(event), ctypes.sizeof(event))
        self._send(self._connection, 0, root, XCB_EVENT_MASK_PROPERTY_CHANGE, raw)

    def flush(self) -> None:
        self._flush(self._connection)


def qt_x11() -> X11 | None:
    """X11 through the xcb connection of Qt's xcb platform; None without one."""
    if sys.platform != "linux":  # also tells the type checker: PySide's X11 types are Linux's
        return None
    app = QGuiApplication.instance()
    interface = app.nativeInterface() if isinstance(app, QGuiApplication) else None
    if not isinstance(interface, QNativeInterface.QX11Application):
        return None
    connection = interface.connection()
    if not connection:
        return None
    try:
        return Xcb(connection)
    except OSError as error:
        _log.warning("cannot load libxcb: %s", error)
        return None
