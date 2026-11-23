"""Render the documentation's screenshots, docs/images/*-light.png and *-dark.png.

    uv run python scripts/gen_screenshots.py                # every shot
    uv run python scripts/gen_screenshots.py overview proxies  # only these

The real QML window, offscreen at twice the pixels, against the simulated host and the
sample profiles that scripts/preview.py runs with: nothing connects, takes over the system
or reaches the network. The window has a fixed size and Chinese text; the title band and the
settings follow macOS, as on the platform this runs on, so run it on a Mac. The tunnel is
connected and routes with the first sample profile's rules; the routing page's shots show my
plan, the default template. The account page's shot is signed in to a stand-in provider on a
loopback port, as scripts/preview.py --provider is, in a window of its own: the other shots
show OpenWeir without an account. Run this after a change to a page that a screenshot shows,
look at the result, and commit the images (docs/development.md).
Source-tree only, like the preview: the simulated host never ships in the package.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Run as a script, Python puts scripts/ on the path rather than the root.
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPen  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuick import QQuickItem, QQuickWindow  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from openweir.app import load_ui  # noqa: E402
from openweir.desktop.login import LoginItemStatus  # noqa: E402
from openweir.host.base import TAKEOVER_TUN  # noqa: E402
from openweir.i18n import Translations  # noqa: E402
from openweir.models.backup_icloud import ICloudProvider  # noqa: E402
from openweir.models.plans import default_plan  # noqa: E402
from openweir.models.routing import MODE_PLAN, MODE_PROFILE, RoutingStore  # noqa: E402
from openweir.models.store import ProfileStore  # noqa: E402
from openweir.product import DISPLAY_NAME  # noqa: E402
from openweir.viewmodels.appearance import Appearance  # noqa: E402
from openweir.viewmodels.composition import ViewModels, compose  # noqa: E402
from tests.support.appearance import SystemColorScheme  # noqa: E402
from tests.support.credentials import MemoryCredentials  # noqa: E402
from tests.support.icloud import FakeICloud  # noqa: E402
from tests.support.provider_server import FakeProvider  # noqa: E402
from tests.support.releases import offline_updater  # noqa: E402
from tests.support.simulated import (  # noqa: E402
    SAMPLE_CONFIG,
    SimulatedFetcher,
    SimulatedHost,
    seed_profiles,
    seed_provider,
)
from tests.support.window import find, kind  # noqa: E402

OUTPUT = ROOT / "docs" / "images"
SIZE = (1280, 800)  # the window, in points: its default size (DESIGN.md)
SCALE = 2  # pixels per point
APPEARANCES = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}
RADIUS = 10.0  # the frame's corners, in points
# A hairline around the window, as the system draws one, in each appearance.
EDGE = {"light": QColor(0, 0, 0, 40), "dark": QColor(255, 255, 255, 40)}
SETTLE_MS = 2500  # a page's transition, and the two polls that a rate takes
WARM_UP_POLLS = 5  # of ten seconds' traffic each: the chart shows forty
TIMEOUT_S = 20.0


@dataclass(frozen=True)
class Shot:
    page: str  # the sidebar's page
    # Brings the page to the state the shot shows, given its item.
    prepare: Callable[[QQuickItem], None] | None = None
    # What routes: the profile's own rules, whose groups the simulated core lists, or my plan.
    mode: str = MODE_PROFILE
    # Signed in to a stand-in provider (tests/support/provider_server.py).
    provider: bool = False
    # Whether the page has what the shot shows, which may come from the network.
    ready: Callable[[ViewModels], bool] | None = None


def _tab(name: str) -> Callable[[QQuickItem], None]:
    """Show the routing page's tab ``name``."""

    def prepare(page: QQuickItem) -> None:
        page.setProperty("tab", name)

    return prepare


def _account_read(view_models: ViewModels) -> bool:
    """The account synced, and its wallet, plans and orders read."""
    account = view_models.account
    return (
        account.property("subscriptionCount") > 0
        and bool(account.property("walletBalance"))
        and account.property("plansNote") == ""
        and account.property("ordersNote") == ""
    )


SHOTS = {
    "overview": Shot("overview"),
    "proxies": Shot("proxies"),
    "profiles": Shot("profiles"),
    "connections": Shot("connections"),
    "settings": Shot("settings"),
    "routing": Shot("routing", _tab("rules"), MODE_PLAN),
    "routing-groups": Shot("routing", _tab("groups"), MODE_PLAN),
    "account": Shot("account", provider=True, ready=_account_read),
}
# The pages' items in Main.qml's StackLayout, by the sidebar's page.
PAGE_TYPES = {
    "overview": "OverviewPage",
    "proxies": "ProxiesPage",
    "profiles": "ProfilesPage",
    "routing": "RoutingPage",
    "connections": "ConnectionsPage",
    "logs": "LogsPage",
    "account": "AccountPage",
    "settings": "SettingsPage",
}


class WarpClock:
    """The time, after the jumps ``skip`` makes: the simulated host polls on it, so its
    traffic chart fills in a few seconds rather than in its forty."""

    def __init__(self) -> None:
        self._offset = 0.0

    def __call__(self) -> float:
        return time.time() + self._offset

    def skip(self, seconds: float) -> None:
        self._offset += seconds


class InstalledLoginItem:
    """Opening at login as the installed app offers it, switched off; nothing registers."""

    available = True

    def status(self) -> LoginItemStatus:
        return LoginItemStatus(enabled=False)

    def set_enabled(self, enabled: bool) -> LoginItemStatus:
        return LoginItemStatus(enabled=False)

    def open_settings(self) -> None:
        pass


def wait_until(test: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + TIMEOUT_S
    while not test():
        if time.monotonic() > deadline:
            raise TimeoutError(f"gave up waiting for {what}")
        QTest.qWait(50)


def framed(image: QImage, ratio: float, appearance: str) -> QImage:
    """The window with rounded corners and a hairline edge, transparent outside them;
    ``ratio`` is the image's pixels per point."""
    result = QImage(image.size(), QImage.Format.Format_ARGB32)
    result.fill(Qt.GlobalColor.transparent)
    bounds = QRectF(0, 0, image.width(), image.height())
    radius = RADIUS * ratio
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(bounds, radius, radius)
    painter.setClipPath(path)
    painter.drawImage(0, 0, image)
    painter.setClipping(False)
    half = ratio / 2
    edge = QPainterPath()
    edge.addRoundedRect(bounds.adjusted(half, half, -half, -half), radius - half, radius - half)
    painter.setPen(QPen(EDGE[appearance], ratio))
    painter.drawPath(edge)
    painter.end()
    return result


def _show(window: QQuickWindow, view_models: ViewModels, shot: Shot) -> None:
    """Route as ``shot`` says, open its page and bring the page to its state."""
    routing, shell = view_models.routing, view_models.shell
    routing.setMode(shot.mode)
    wait_until(
        lambda: routing.property("mode") == shot.mode and not routing.property("busy"),
        f"routing with {shot.mode}",
    )

    def opened() -> bool:
        # The sidebar lists the account page only once the accounts are read.
        if shell.property("currentPage") != shot.page:
            shell.setPage(shot.page)
        return shell.property("currentPage") == shot.page

    wait_until(opened, f"the page {shot.page}")
    if shot.ready is not None:
        ready = shot.ready
        wait_until(lambda: ready(view_models), f"the page {shot.page}'s data")
    if shot.prepare is not None:
        page = find(window.contentItem(), lambda item: kind(item) == PAGE_TYPES[shot.page])
        assert page is not None
        shot.prepare(page)
    QTest.qWait(SETTLE_MS)


@contextlib.contextmanager
def _stand_in(wanted: bool) -> Iterator[FakeProvider | None]:
    """A stand-in provider on a loopback port when ``wanted``, reached past any proxy the
    machine has, with rules of its own so that it can be used."""
    if not wanted:
        yield None
        return
    previous = os.environ.get("no_proxy")
    os.environ["no_proxy"] = "127.0.0.1"
    provider = FakeProvider()
    provider.config = SAMPLE_CONFIG.encode()
    try:
        yield provider
    finally:
        provider.close()
        if previous is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = previous


def _load(
    store: ProfileStore, appearance: str, clock: WarpClock, provider: FakeProvider | None
) -> tuple[ViewModels, QQmlApplicationEngine]:
    seed_profiles(store, time.time())
    # Credentials stay in memory: the WARP account, and the provider's session as the sign-in
    # dialog would have left it.
    credentials = MemoryCredentials()
    if provider is not None:
        seed_provider(store, provider, credentials, time.time())
    # My plan is the default template, there to switch to.
    routing = RoutingStore(store.database)
    plan = default_plan()
    routing.put_plan(plan)
    routing.update_state(plan_id=plan.id)
    view_models = compose(
        SimulatedHost(clock=clock, takeover=TAKEOVER_TUN),
        store,
        SimulatedFetcher(),
        appearance=Appearance("system", hints=SystemColorScheme(APPEARANCES[appearance])),
        login_item=InstalledLoginItem(),
        tray_available=True,  # the menu bar's, as on a Mac
        updater=offline_updater(store.directory),
        credentials=credentials,
        icloud=ICloudProvider(FakeICloud()),
    )
    engine = QQmlApplicationEngine()
    if not load_ui(engine, view_models):
        raise RuntimeError("the window did not load")
    return view_models, engine


def capture(output: Path, appearance: str, names: Iterable[str]) -> list[Path]:
    """Save the shots ``names`` in ``appearance`` to ``output``; returns the files. The shots
    signed in to a provider are taken in a window of their own, after the others."""
    app = QCoreApplication.instance()
    assert app is not None, "needs a QGuiApplication"
    wanted = [name for name in SHOTS if name in names]
    translations = Translations(app)
    translations.apply("zh")
    saved: list[Path] = []
    try:
        for signed_in in (False, True):
            group = [name for name in wanted if SHOTS[name].provider == signed_in]
            if group:
                saved += _session(output, appearance, group, signed_in)
    finally:
        translations.remove()
    return saved


def _session(output: Path, appearance: str, names: list[str], signed_in: bool) -> list[Path]:
    """Take the shots ``names`` in one window, signed in to a stand-in provider or not."""
    clock = WarpClock()
    saved = []
    with (
        tempfile.TemporaryDirectory(prefix="openweir-screenshots-") as directory,
        _stand_in(signed_in) as provider,
    ):
        store = ProfileStore(Path(directory))
        view_models, engine = _load(store, appearance, clock, provider)
        try:
            (window,) = engine.rootObjects()
            assert isinstance(window, QQuickWindow)
            window.resize(*SIZE)
            view_models.start()
            overview = view_models.overview
            wait_until(lambda: overview.property("connected"), "the tunnel")
            for _ in range(WARM_UP_POLLS):
                clock.skip(10)
                QTest.qWait(1100)
            QTest.qWait(2000)  # the connections' rates over real seconds again
            for name in names:
                _show(window, view_models, SHOTS[name])
                path = output / f"{name}-{appearance}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                # Quality 0 is PNG's strongest compression; the pixels stay the same.
                image = framed(window.grabWindow(), window.devicePixelRatio(), appearance)
                if not image.save(str(path), quality=0):  # PNG, by the suffix
                    raise OSError(f"cannot write {path}")
                saved.append(path)
        finally:
            view_models.shutdown()
            for root in engine.rootObjects():
                root.deleteLater()
            del engine
            QTest.qWait(0)
            store.close()
    return saved


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the documentation's screenshots")
    parser.add_argument(
        "shots", nargs="*", metavar="shot", help=f"default: all of {', '.join(SHOTS)}"
    )
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if unknown := set(args.shots) - SHOTS.keys():
        parser.error(f"no such shot: {', '.join(sorted(unknown))}")
    # Before Qt starts: headless, and twice the pixels of a point.
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = str(SCALE)
    app = QGuiApplication(sys.argv[:1])
    app.setApplicationName(DISPLAY_NAME)
    for appearance in APPEARANCES:
        for path in capture(args.output, appearance, args.shots or SHOTS):
            print(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")


if __name__ == "__main__":
    main()
