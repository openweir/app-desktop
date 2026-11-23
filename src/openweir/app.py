"""Composition root: builds models, wraps them in ViewModels and loads the QML view."""

from __future__ import annotations

import os
import signal
import socket
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, QSocketNotifier, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow

from openweir import __version__
from openweir.desktop.associations import clear_stale_in_background
from openweir.desktop.dock import open_in_menu_bar
from openweir.desktop.instance import SingleInstance, ask_to_quit
from openweir.desktop.links import LinkReceiver, from_arguments
from openweir.desktop.login import BACKGROUND_ARG, LoginItem, default_login_item
from openweir.desktop.session import session_ending
from openweir.desktop.tray import tray_supported
from openweir.fonts import register_fonts
from openweir.host.base import TAKEOVER_NONE, TAKEOVER_SETTINGS, HostError, TunnelHost
from openweir.host.service import ServiceHost
from openweir.host.transport import Opener, Streams
from openweir.i18n import Translations, resolve_language
from openweir.models.backup_google import GoogleDriveProvider, configured_provider
from openweir.models.backup_icloud import bundled_provider
from openweir.models.credentials import KeyringCredentials
from openweir.models.database import DATABASE_NAME
from openweir.models.settings import DatabaseSettingsStore, SettingsStore, seed_new_install
from openweir.models.store import ProfileStore, default_data_dir
from openweir.models.subscription import Fetcher, UrlFetcher
from openweir.product import DISPLAY_NAME, LINUX_DESKTOP_ID, LINUX_SOCKET, LINUX_UNIT, PIPE_NAME
from openweir.update.linux import UPDATE_RESULT_ARG
from openweir.update.releases import installed_package_format
from openweir.update.updater import Updater
from openweir.viewmodels.appearance import Appearance, resolve_mode
from openweir.viewmodels.composition import ViewModels, compose
from openweir.viewmodels.text_editing import ClickAway

# Import registers the presentation helper for QML, including standalone component tests.
from openweir.viewmodels.text_editing import TextEditing as TextEditing

MAIN_QML = Path(__file__).resolve().parent / "ui" / "Main.qml"
DEV_SOCKET_ENV = "OPENWEIR_DEV_SOCKET"
# The command line, which the installers and the system use (none of it is for people):
# - BACKGROUND_ARG (desktop/login.py): started at login; opens in the tray where there is one,
#   or shows the window. Windows' scheduled task and Linux's autostart entry pass it.
# - QUIT_ARG: ask the UI already running to quit and wait for it, as the Windows installer does
#   before it replaces the files (docs/ui.md §8) and the Linux package is to before an upgrade
#   (D61); exits 0 once no UI runs. The tunnel stays as it is.
# - UPDATE_RESULT_ARG: --update-result=<code>, the exit code of the Linux update helper that
#   the UI ran through pkexec, from the shell that starts the UI again after it (D62,
#   update/linux.py); the settings page's version row says what it means.
QUIT_ARG = "--quit"
# The signals that end the UI from outside: SIGTERM as the session ends or systemd stops it,
# SIGINT from a terminal. They leave as the system's quit does (Application.leave_on_signals).
EXIT_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def update_result(arguments: Sequence[str]) -> int | None:
    """The exit code that ``--update-result=<code>`` passes; None without one, or for one that
    is not a number from 0 to 255."""
    prefix = f"{UPDATE_RESULT_ARG}="
    for argument in arguments:
        if argument.startswith(prefix):
            code = argument.removeprefix(prefix)
            if code.isascii() and code.isdigit() and int(code) <= 255:
                return int(code)
            return None
    return None


class Application(QGuiApplication):
    """Qt reports ⌘Q, the Dock's Quit and the system's quit at the end of the session as a
    Quit event, and would first close the windows and give up when one refuses, as the main
    window does while it hides in the tray (Main.qml). The user's quit goes to ``user_quit``
    instead, which disconnects first (D41); the system's leaves the event loop at once and
    leaves the tunnel to the service (D5)."""

    def __init__(self, argv: list[str]) -> None:
        super().__init__(argv)
        self.user_quit: Callable[[], None] | None = None
        self._wakeup: tuple[socket.socket, socket.socket, QSocketNotifier] | None = None

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Quit:
            if self.user_quit is not None and not session_ending():
                self.user_quit()
            else:
                self.exit(0)
            return True
        return super().event(event)

    def leave_on_signals(self, signals: Sequence[signal.Signals] = EXIT_SIGNALS) -> None:
        """Leave the event loop on ``signals`` as on the system's quit: at once, the tunnel
        left to the service (D5, D41). Linux's session manager and systemd end a session's
        programs with SIGTERM rather than a Quit event. Python runs a handler only between
        its own instructions, which Qt's event loop does not reach while it waits; the
        signal's number written to the wakeup socket wakes the loop instead. Windows sends
        a GUI process no such signals."""
        if sys.platform == "win32" or self._wakeup is not None:
            return
        reader, writer = socket.socketpair()
        reader.setblocking(False)
        writer.setblocking(False)
        signal.set_wakeup_fd(writer.fileno(), warn_on_full_buffer=False)
        for number in signals:
            signal.signal(number, lambda *_: None)  # the wakeup socket does the work
        notifier = QSocketNotifier(reader.fileno(), QSocketNotifier.Type.Read, self)

        def woken() -> None:
            try:
                received = reader.recv(64)
            except OSError:
                return
            if any(number in received for number in signals):
                self.exit(0)

        notifier.activated.connect(woken)
        self._wakeup = reader, writer, notifier


def unavailable_opener(message: str) -> Opener:
    """No service to connect to; every connection fails with ``message``."""

    async def unavailable() -> Streams:
        raise HostError("service_unavailable", message)

    return unavailable


def missing_socket_opener(connect: Opener, path: str) -> Opener:
    """``connect`` to the Linux release service's socket (D60), which systemd's
    ``openweir.socket`` holds open even while the service is stopped. When that socket is not
    there, the error names the unit that should be listening, rather than only saying that a
    file is missing."""

    async def open_release() -> Streams:
        try:
            return await connect()
        except HostError as error:
            if not isinstance(error.__cause__, FileNotFoundError):
                raise
            message = (
                f"{path} does not exist: the {LINUX_UNIT}.socket unit is not running "
                f"(systemctl enable --now {LINUX_UNIT}.socket)"
            )
            raise HostError("service_unavailable", message, retryable=True) from error

    return open_release


def default_host(
    environ: Mapping[str, str] = os.environ, executable: str = sys.executable
) -> TunnelHost:
    """The installed service on Windows. With a development socket set elsewhere, the
    development service, which takes over nothing (docs/architecture.md §11). Otherwise on
    macOS the system extension of a packaged OpenWeir.app (§6), and on Linux the release
    service of an installed package (D60, D61), which a package-format marker beside the
    executable tells from a run from source. Anything else has no service to talk to."""
    if sys.platform == "win32":
        from openweir.host.transport import pipe_opener

        return ServiceHost(pipe_opener(PIPE_NAME), takeover=TAKEOVER_SETTINGS)
    from openweir.host.transport import unix_opener

    development = environ.get(DEV_SOCKET_ENV)
    if development:
        return ServiceHost(unix_opener(development), takeover=TAKEOVER_NONE)
    if sys.platform == "darwin":
        from openweir.host.extension import bundled_host

        host = bundled_host()
        if host is not None:
            return host
    elif sys.platform == "linux" and installed_package_format(executable) is not None:
        opener = missing_socket_opener(unix_opener(LINUX_SOCKET), LINUX_SOCKET)
        return ServiceHost(opener, takeover=TAKEOVER_SETTINGS)
    message = f"no installed service; set {DEV_SOCKET_ENV} to a development service's socket"
    return ServiceHost(unavailable_opener(message), takeover=TAKEOVER_NONE)


def default_take_over(host: TunnelHost) -> Callable[[], Coroutine[Any, Any, None]] | None:
    """An administrator's take-over of another user's tunnel (D5b, D66): only with the
    installed service, whose executable runs as the take-over helper, through UAC on Windows
    and pkexec on Linux; and only for a user the system asks as an administrator."""
    if sys.platform not in ("win32", "linux") or not isinstance(host, ServiceHost):
        return None
    if host.takeover != TAKEOVER_SETTINGS:  # the development service
        return None
    from openweir.desktop import elevation

    if not elevation.is_administrator() or not elevation.helper_available():
        return None
    return partial(host.take_over, elevation.take_over)


def load_ui(engine: QQmlApplicationEngine, view_models: ViewModels) -> bool:
    register_fonts()
    engine.setInitialProperties(view_models.initial_properties())
    engine.load(QUrl.fromLocalFile(str(MAIN_QML)))
    for window in engine.rootObjects():
        if isinstance(window, QQuickWindow):
            ClickAway(window, engine)
    return bool(engine.rootObjects())


def switch_language(
    translations: Translations, engine: QQmlApplicationEngine, view_models: ViewModels
) -> None:
    """Show the running UI in the language the settings name (docs/ui.md §5)."""
    translations.apply(resolve_language(view_models.settings_store.current.language))
    engine.retranslate()
    view_models.retranslate()


def stored_appearance(settings: SettingsStore) -> str:
    """The saved appearance; empty for ``system``, so OPENWEIR_APPEARANCE still applies."""
    mode = settings.current.appearance
    return "" if mode == "system" else mode


def open_store(directory: Path, platform: str = sys.platform) -> ProfileStore:
    """The profiles and settings in ``directory``. With no database there yet, this is a new
    installation, which starts with settings of its own (D46); an existing one keeps the
    defaults it had."""
    new = not (directory / DATABASE_NAME).exists()
    store = ProfileStore(directory)
    if new:
        seed_new_install(store.database, platform)
    return store


def main(
    host: TunnelHost | None = None,
    store: ProfileStore | None = None,
    fetcher: Fetcher | None = None,
    language: str = "",
    appearance: str = "",
    login_item: LoginItem | None = None,
    updater: Updater | None = None,
    google: GoogleDriveProvider | None = None,
) -> int:
    """``language`` is ``zh`` or ``en`` and wins over the settings at startup; empty follows
    the settings, then OPENWEIR_LANG, then the system. ``appearance`` is ``system``,
    ``light`` or ``dark`` and likewise wins over the settings; empty follows the settings,
    then OPENWEIR_APPEARANCE, then the system. The settings live in the profiles'
    database, and the lock that keeps one UI per user beside it; the lock is taken before
    the database is opened, so only one UI migrates it, and let go after the store is
    closed on exit. With ``--quit`` it asks the UI already running to quit instead."""
    directory = store.directory if store else default_data_dir()
    if QUIT_ARG in sys.argv[1:]:
        _core = QCoreApplication.instance() or QCoreApplication(sys.argv)
        return 0 if ask_to_quit(directory) else 1
    start_hidden = BACKGROUND_ARG in sys.argv[1:]
    if start_hidden:
        open_in_menu_bar()
    app = Application(sys.argv)
    app.leave_on_signals()
    app.setApplicationName(DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    # Names the window's .desktop file, which gives it its icon and name on Linux: Wayland's
    # app id, X11's _KDE_NET_WM_DESKTOP_FILE. The package installs the file (D61).
    app.setDesktopFileName(LINUX_DESKTOP_ID)
    # The bundle's icon covers the packaged macOS app; this one covers Windows' taskbar and
    # runs from source (scripts/gen_icons.py).
    brand = Path(__file__).resolve().parent / "ui" / "brand"
    app.setWindowIcon(
        QIcon(str(brand / ("app-icon-macos.svg" if sys.platform == "darwin" else "app-icon.svg")))
    )
    # Closing the window hides it in the tray or quits OpenWeir (DesktopViewModel).
    app.setQuitOnLastWindowClosed(False)
    # macOS sends install links as Apple Events, which can come before the window does.
    receiver = LinkReceiver(app)
    link = from_arguments(sys.argv[1:])
    instance = SingleInstance(directory)
    if not instance.claim(link):
        return 0  # the UI already running shows its window, or opens the link
    # Windows: a stale link registration in the user's own HKCU hides OpenWeir's (D45).
    clear_stale_in_background(app.platformName())
    store = store or open_store(directory)
    settings = DatabaseSettingsStore(store.database)
    translations = Translations(app)
    translations.apply(resolve_language(language or settings.current.language))
    host = host or default_host()
    view_models = compose(
        host,
        store,
        fetcher or UrlFetcher(),
        appearance=Appearance(resolve_mode(appearance or stored_appearance(settings))),
        settings=settings,
        login_item=login_item or default_login_item(),
        tray_available=tray_supported(app.platformName()),
        start_hidden=start_hidden,
        # Opened at login by Windows' scheduled task or Linux's autostart entry.
        connect_at_login=sys.platform in ("win32", "linux") and start_hidden,
        updater=updater,
        icloud=bundled_provider(),
        google=google or configured_provider(KeyringCredentials()),
        take_over=default_take_over(host),
        update_result=update_result(sys.argv[1:]),
    )
    engine = QQmlApplicationEngine()
    if not load_ui(engine, view_models):
        store.close()
        instance.release()
        return 1
    # Queued: the switch happens once the control that asked for it has handled its click.
    switch = partial(switch_language, translations, engine, view_models)
    view_models.settings.languageChanged.connect(switch, Qt.ConnectionType.QueuedConnection)
    # exit, not quit: on macOS quit goes through terminate: and comes back as a Quit event.
    view_models.desktop.quitRequested.connect(partial(app.exit, 0))
    app.user_quit = view_models.desktop.quit
    instance.activated.connect(view_models.desktop.showWindow)
    # The installer's --quit: it replaces the files, and the service keeps the tunnel.
    instance.quit_requested.connect(view_models.desktop.leave)
    instance.received.connect(receiver.deliver)
    receiver.received.connect(view_models.open_link)
    if link is not None:
        receiver.deliver(link)
    view_models.start()
    receiver.ready()
    app.aboutToQuit.connect(view_models.shutdown)
    code = app.exec()
    view_models.settings.languageChanged.disconnect(switch)
    del switch, engine  # tear down QML before the ViewModels it binds to
    store.close()
    instance.release()  # last: ask_to_quit takes the lock let go as the UI gone
    return code
