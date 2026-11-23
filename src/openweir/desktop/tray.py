"""Whether there is a tray, and the tray icon's image (docs/ui.md §8, DESIGN.md "托盘图标").

macOS draws a template image in the menu bar's own colours. Windows shows the image as it
is, so the notification area gets a dark glyph on a light taskbar and a white one on a
dark taskbar; the taskbar follows the system theme, not the apps' theme that Qt reports.
Linux shows it as it is too, on a panel whose colour nothing reports: GNOME's top bar is dark
in both styles, and other desktops' panels follow the system's light or dark style, which the
desktop portal reports. A disconnected tunnel dims the glyph.

On Linux Qt shows the icon as a StatusNotifierItem over D-Bus, which only works while a panel
hosts such items (D65): KDE's does, GNOME's only with the AppIndicator extension. Qt asks the
StatusNotifierWatcher for a host as it makes the icon; ``status_notifier_host`` asks the same
first, so that without one closing the window quits instead of hiding it in no tray.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtGui import QGuiApplication

TRAY_DIR = Path(__file__).resolve().parent.parent / "ui" / "tray"
# Platforms whose Qt plugin has a native tray. Elsewhere Qt.labs.platform would need Qt
# Widgets, which the UI does not load.
TRAY_PLATFORMS = ("cocoa", "windows")
# Linux's desktop platforms: Qt's tray there is a StatusNotifierItem, with no Qt Widgets.
STATUS_NOTIFIER_PLATFORMS = ("xcb", "wayland", "wayland-egl")
_PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"

WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_SETTINGS = "org.freedesktop.portal.Settings"
# org.freedesktop.appearance color-scheme: 0 no preference, 1 prefers dark, 2 prefers light.
PREFERS_LIGHT = 2
# The session bus answers at once; one that does not must not hold up the UI.
DBUS_TIMEOUT_S = 1.0
# What a missing bus, a refused connection or a reply jeepney cannot read raise.
_BUS_ERRORS = (OSError, ValueError, KeyError, RuntimeError)


class Bus(Protocol):
    """The part of a jeepney connection used here; tests pass a stand-in."""

    def send_and_get_reply(self, message: Any, *, timeout: float | None = None) -> Any: ...


OpenBus = Callable[[], AbstractContextManager[Bus]]


def tray_supported(platform_name: str, host: Callable[[], bool] | None = None) -> bool:
    """``platform_name`` is QGuiApplication.platformName(). On Linux ``host`` says whether a
    panel hosts StatusNotifier items; by default ``status_notifier_host`` asks the bus."""
    if platform_name in TRAY_PLATFORMS:
        return True
    if platform_name in STATUS_NOTIFIER_PLATFORMS:
        return (host or status_notifier_host)()
    return False


def session_bus_address(environ: Mapping[str, str] = os.environ) -> str | None:
    """The session bus as libdbus finds it for Qt: DBUS_SESSION_BUS_ADDRESS, or the user's
    bus socket in XDG_RUNTIME_DIR."""
    address = environ.get("DBUS_SESSION_BUS_ADDRESS")
    if address:
        return address
    runtime = environ.get("XDG_RUNTIME_DIR")
    if runtime and (Path(runtime) / "bus").exists():
        return f"unix:path={Path(runtime) / 'bus'}"
    return None


def _open_session_bus() -> AbstractContextManager[Bus]:
    from jeepney.io.blocking import open_dbus_connection  # pyright: ignore[reportMissingImports]  # Linux only

    address = session_bus_address()
    if address is None:
        raise OSError("no session bus")
    return open_dbus_connection(address, auth_timeout=DBUS_TIMEOUT_S)


def _call(bus: Bus, message: Any) -> tuple[Any, ...]:
    """The reply's body; an error reply raises OSError."""
    from jeepney import MessageType  # pyright: ignore[reportMissingImports]  # Linux only

    reply = bus.send_and_get_reply(message, timeout=DBUS_TIMEOUT_S)
    if reply.header.message_type == MessageType.error:
        raise OSError(f"D-Bus error {reply.body!r}")
    return reply.body


def _value(variant: Any) -> Any:
    """What a D-Bus variant holds: jeepney gives one as (signature, value), and the portal's
    Read wraps its answer in a second one."""
    while isinstance(variant, tuple) and len(variant) == 2 and isinstance(variant[0], str):
        variant = variant[1]
    return variant


def status_notifier_host(open_bus: OpenBus | None = None) -> bool:
    """Whether a StatusNotifierWatcher runs on the session bus and a host has registered with
    it, as Qt's D-Bus tray asks. Asking first whether it runs keeps the bus from starting one
    for the question. No bus, no jeepney or no answer counts as no host."""
    try:
        from jeepney import DBusAddress, Properties  # pyright: ignore[reportMissingImports]  # Linux only
        from jeepney.bus_messages import message_bus  # pyright: ignore[reportMissingImports]  # Linux only
    except ImportError:
        return False
    watcher = DBusAddress(WATCHER_PATH, bus_name=WATCHER, interface=WATCHER)
    try:
        with (open_bus or _open_session_bus)() as bus:
            (running,) = _call(bus, message_bus.NameHasOwner(WATCHER))
            if not running:
                return False
            (host,) = _call(bus, Properties(watcher).get("IsStatusNotifierHostRegistered"))
    except _BUS_ERRORS:
        return False
    return _value(host) is True


def system_prefers_light(open_bus: OpenBus | None = None) -> bool:
    """Whether the desktop portal says the system prefers a light style; not when it has no
    preference or does not answer."""
    try:
        from jeepney import DBusAddress, new_method_call  # pyright: ignore[reportMissingImports]  # Linux only
    except ImportError:
        return False
    settings = DBusAddress(PORTAL_PATH, bus_name=PORTAL, interface=PORTAL_SETTINGS)
    read = new_method_call(settings, "Read", "ss", ("org.freedesktop.appearance", "color-scheme"))
    try:
        with (open_bus or _open_session_bus)() as bus:
            (scheme,) = _call(bus, read)
    except _BUS_ERRORS:
        return False
    return _value(scheme) == PREFERS_LIGHT


def panel_is_light(environ: Mapping[str, str], prefers_light: Callable[[], bool]) -> bool:
    """Linux: whether the panel the tray icon is on is light. GNOME's top bar is dark in both
    styles; other desktops' panels follow the system's style."""
    if "GNOME" in environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":"):
        return False
    return prefers_light()


def taskbar_is_light() -> bool:
    """Windows: whether the taskbar uses the light theme ("Choose your default Windows
    mode"). Windows 10 has no such value until the user sets it, and its taskbar is dark.
    Linux: whether the panel is light (``panel_is_light``), asked on a desktop platform only."""
    if sys.platform == "linux":
        if QGuiApplication.platformName() not in STATUS_NOTIFIER_PLATFORMS:
            return False
        return panel_is_light(os.environ, system_prefers_light)
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
    except OSError:
        return False
    return bool(value)


def tray_icon(*, template: bool, light_taskbar: bool, connected: bool) -> Path:
    variant = "template" if template else "light-taskbar" if light_taskbar else "dark-taskbar"
    return TRAY_DIR / f"tray-{variant}-{'on' if connected else 'off'}.svg"
