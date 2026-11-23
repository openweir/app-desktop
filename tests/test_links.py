"""Install links (docs/release.md §7): parsing every scheme, the ways a link arrives, and the
import form it fills in; and a provider's sign-in link (docs/accounts.md §2), which opens the
sign-in dialog with its address."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtGui import QFileOpenEvent
from pytestqt.qtbot import QtBot

from openweir.desktop.instance import ACTIVATE, SingleInstance, message
from openweir.desktop.links import (
    SCHEMES,
    InstallLink,
    LinkReceiver,
    ProviderLink,
    from_arguments,
    is_link,
    parse,
)
from openweir.models.store import ProfileStore
from openweir.viewmodels.composition import ViewModels, compose
from tests.support.releases import offline_updater
from tests.support.simulated import SimulatedFetcher, SimulatedHost, seed_profiles
from tests.support.window import Loaded, find, kind

SUBSCRIPTION = "https://sub.example.com/api/v1/client/subscribe?token=abc&flag=clash"
ENCODED = quote(SUBSCRIPTION, safe="")


@pytest.mark.parametrize(
    ("link", "parsed"),
    [
        (f"openweir://install-config?url={ENCODED}&name=%E6%9C%BA%E5%9C%BA", ("机场",)),
        (f"clash://install-config?url={ENCODED}", ("",)),
        (f"CLASH://Install-Config/?url={ENCODED}&name=Hi", ("Hi",)),
        # Clash Party's, and Clash Meta for Android's, FlClash's and Remnawave's (D45).
        (f"mihomo://install-config?url={ENCODED}&name=Party", ("Party",)),
        (f"clashmeta://install-config?url={ENCODED}&name=%E6%9C%BA%E5%9C%BA", ("机场",)),
        (f"MiHoMo://INSTALL-CONFIG/?url={ENCODED}", ("",)),
        (f"ClashMeta://install-config/?name=Meta&url={ENCODED}", ("Meta",)),
        (f"openweir://install-config?name=+%20Spaced%20+&url={ENCODED}", ("Spaced",)),
        (f"openweir://install-config?url={ENCODED}&name=a%0Ab%09c", ("abc",)),
        (f"openweir://install-config?url={ENCODED}&name={'x' * 150}", ("x" * 100,)),
    ],
)
def test_parse(link: str, parsed: tuple[str]) -> None:
    assert parse(link) == InstallLink(SUBSCRIPTION, parsed[0])


@pytest.mark.parametrize(
    "link",
    [
        f"https://install-config?url={ENCODED}",  # another scheme
        f"openweir://import?url={ENCODED}",  # another action
        f"openweir://install-config/more?url={ENCODED}",
        "openweir://install-config?name=only",  # no url
        f"openweir://install-config?url={ENCODED}&url={ENCODED}",  # two
        "openweir://install-config?url=file%3A%2F%2F%2Fetc%2Fpasswd",
        "openweir://install-config?url=javascript%3Aalert(1)",
        "clash://install-config?url=ftp%3A%2F%2Fexample.com%2Fsub",
        "mihomo://install-config?name=only",
        "clashmeta://install-config",
        "clashmeta://install-config/",
        f"mihomo://install-config/more?url={ENCODED}",
        f"clashmeta://import?url={ENCODED}",
        f"meta://install-config?url={ENCODED}",  # schemes OpenWeir does not take
        f"clash-meta://install-config?url={ENCODED}",
        f"clashmetax://install-config?url={ENCODED}",
        "clashmeta:",
        f"openweir://install-config?url={ENCODED}&name={'x' * 9000}",  # too long
        "openweir:",
        "",
    ],
)
def test_parse_refuses(link: str) -> None:
    assert parse(link) is None


@pytest.mark.parametrize(
    ("link", "origin"),
    [
        ("openweir://provider?url=https%3A%2F%2Fexample.com", "https://example.com"),
        ("openweir://provider?url=https%3A%2F%2Fexample.com%2F", "https://example.com"),
        ("openweir://provider/?url=https://example.com", "https://example.com"),
        ("OpenWeir://Provider?url=HTTPS%3A%2F%2FPanel.Example.COM", "https://panel.example.com"),
        ("openweir://provider?url=https%3A%2F%2Fexample.com%3A8443", "https://example.com:8443"),
        ("openweir://provider?url=https%3A%2F%2Fexample.com%3A443", "https://example.com"),
        ("openweir://provider?url=%20https%3A%2F%2Fexample.com%20", "https://example.com"),
        (
            "openweir://provider?url=https%3A%2F%2F%E4%BE%8B%E5%AD%90.%E6%B5%8B%E8%AF%95",
            "https://xn--fsqu00a.xn--0zwm56d",
        ),
        ("openweir://provider?url=https%3A%2F%2Fexample.com&utm_source=x", "https://example.com"),
    ],
)
def test_a_provider_link_names_an_https_origin(link: str, origin: str) -> None:
    assert is_link(link)
    assert parse(link) == ProviderLink(origin)


@pytest.mark.parametrize(
    "link",
    [
        "openweir://provider?url=http%3A%2F%2Fexample.com",  # https only
        "openweir://provider?url=http%3A%2F%2F127.0.0.1%3A8080",  # loopback too
        "openweir://provider?url=example.com",  # no scheme
        "openweir://provider?url=https%3A%2F%2Fexample.com%2Flogin",  # a path
        "openweir://provider?url=https%3A%2F%2Fexample.com%2F%3Fnext%3D1",  # a query
        "openweir://provider?url=https%3A%2F%2Fexample.com%23top",  # a fragment
        "openweir://provider?url=https%3A%2F%2Fexample.com%3F",
        "openweir://provider?url=https%3A%2F%2Fuser%40example.com",  # a user name
        "openweir://provider?url=https%3A%2F%2Fexample.com%3A0",  # a bad port
        "openweir://provider?url=https%3A%2F%2F",  # no host
        "openweir://provider?url=javascript%3Aalert(1)",
        "openweir://provider?url=https%3A%2F%2Fa.com&url=https%3A%2F%2Fb.com",  # two
        "openweir://provider",
        "openweir://provider?name=x",
        "openweir://provider/more?url=https%3A%2F%2Fexample.com",
        # The other clients' schemes carry install links only.
        "clash://provider?url=https%3A%2F%2Fexample.com",
        "mihomo://provider?url=https%3A%2F%2Fexample.com",
        "clashmeta://provider?url=https%3A%2F%2Fexample.com",
        f"openweir://provider?url=https%3A%2F%2F{'x' * 9000}.com",  # too long
    ],
)
def test_a_provider_link_refuses_anything_but_an_https_origin(link: str) -> None:
    assert parse(link) is None


def test_unencoded_ampersands_cut_the_link() -> None:
    # As in other clients: the provider must encode the subscription link.
    parsed = parse(f"clash://install-config?url={SUBSCRIPTION}")
    assert isinstance(parsed, InstallLink)
    assert parsed.url == "https://sub.example.com/api/v1/client/subscribe?token=abc"


def test_every_scheme_in_any_case_is_a_link() -> None:
    assert SCHEMES == ("openweir", "clash", "mihomo", "clashmeta")
    for scheme in SCHEMES:
        for spelling in (scheme, scheme.upper(), scheme.capitalize()):
            link = f"{spelling}://install-config?url={ENCODED}"
            assert is_link(link)
            assert parse(link) == InstallLink(SUBSCRIPTION, "")
    assert not is_link(f"install-config?url={ENCODED}")


def test_link_from_the_command_line() -> None:
    link = f"openweir://install-config?url={ENCODED}"
    assert from_arguments(["--background", link, "clash://other"]) == link
    assert from_arguments(["--background"]) is None
    assert from_arguments(["openweir" + "x" * 9000 + ":"]) is None


@pytest.mark.parametrize("scheme", ["mihomo", "clashmeta", "MIHOMO", "ClashMeta"])
def test_the_command_line_takes_the_other_clients_schemes(scheme: str) -> None:
    # As Windows starts OpenWeir for a link: the registered command's "%1".
    link = f"{scheme}://install-config?url={ENCODED}&name=Mine"
    assert from_arguments(["--background", link]) == link
    assert from_arguments([f"{scheme}x://install-config?url={ENCODED}"]) is None


def test_the_message_a_later_ui_sends() -> None:
    assert message(None) == ACTIVATE
    assert message("openweir://install-config?url=x") == b"open openweir://install-config?url=x\n"
    assert message("openweir://a\r\nactivate") == b"open openweir://aactivate\n"


# A second UI started with a link, in a process of its own: see test_desktop.py.
SECOND_UI = """
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from openweir.desktop.instance import SingleInstance
app = QCoreApplication(sys.argv)
sys.exit(1 if SingleInstance(Path(sys.argv[1])).claim(sys.argv[2]) else 0)
"""


def test_a_second_ui_hands_over_its_link(qtbot: QtBot, tmp_path: Path) -> None:
    first = SingleInstance(tmp_path)
    assert first.claim()
    link = f"openweir://install-config?url={ENCODED}&name=%E6%9C%BA%E5%9C%BA"
    second = subprocess.Popen([sys.executable, "-c", SECOND_UI, str(tmp_path), link])
    try:
        with qtbot.waitSignal(first.received, timeout=8000) as received:
            pass
        assert received.args == [link]
        assert second.wait(timeout=10) == 0
    finally:
        second.kill()
        second.wait()
        first.release()


def test_the_system_opens_a_link(qtbot: QtBot) -> None:
    app = QCoreApplication.instance()
    assert app is not None
    receiver = LinkReceiver(app)
    seen: list[str] = []
    receiver.received.connect(seen.append)
    link = f"openweir://install-config?url={ENCODED}"
    # Before the UI is ready the link waits, as on a launch that a link started.
    QCoreApplication.sendEvent(app, QFileOpenEvent(QUrl(link)))
    QCoreApplication.sendEvent(app, QFileOpenEvent(QUrl.fromLocalFile("/tmp/a.yaml")))
    assert seen == []
    receiver.ready()
    assert len(seen) == 1
    assert parse(seen[0]) == InstallLink(SUBSCRIPTION, "")
    receiver.deliver("clash://install-config?url=x")
    assert seen[1] == "clash://install-config?url=x"
    app.removeEventFilter(receiver)


@pytest.mark.parametrize("scheme", ["mihomo", "clashmeta"])
def test_the_system_opens_the_other_clients_links(qtbot: QtBot, scheme: str) -> None:
    # macOS: on a launch that the link started, and to the UI already running.
    app = QCoreApplication.instance()
    assert app is not None
    receiver = LinkReceiver(app)
    seen: list[str] = []
    receiver.received.connect(seen.append)
    link = f"{scheme}://install-config?url={ENCODED}&name=Mine"
    QCoreApplication.sendEvent(app, QFileOpenEvent(QUrl(link)))
    assert seen == []
    receiver.ready()
    QCoreApplication.sendEvent(
        app, QFileOpenEvent(QUrl(f"{scheme.upper()}://install-config?url=x"))
    )
    assert len(seen) == 2
    assert parse(seen[0]) == InstallLink(SUBSCRIPTION, "Mine")
    assert seen[1].lower() == f"{scheme}://install-config?url=x"
    app.removeEventFilter(receiver)


@pytest.fixture
def built(qtbot: QtBot, tmp_path: Path) -> Iterator[ViewModels]:
    store = ProfileStore(tmp_path)
    seed_profiles(store, time.time())
    view_models = compose(
        SimulatedHost(), store, SimulatedFetcher(), updater=offline_updater(tmp_path)
    )
    view_models.runner.start()
    yield view_models
    view_models.shutdown()


@pytest.mark.parametrize("scheme", SCHEMES)
def test_a_link_fills_in_the_import_form(qtbot: QtBot, built: ViewModels, scheme: str) -> None:
    shown: list[bool] = []
    built.desktop.showWindowRequested.connect(lambda: shown.append(True))
    with qtbot.waitSignal(built.profiles.importPrefilled):
        built.open_link(f"{scheme}://install-config?url={ENCODED}&name=%E6%9C%BA%E5%9C%BA")
    assert shown == [True]
    assert built.shell.currentPage == "profiles"
    assert built.profiles.property("prefillUrl") == SUBSCRIPTION
    assert built.profiles.property("prefillName") == "机场"
    assert not built.profiles.property("hasError")


def test_a_provider_link_opens_the_sign_in_dialog_filled_in(
    qtbot: QtBot, built: ViewModels
) -> None:
    shown: list[bool] = []
    built.desktop.showWindowRequested.connect(lambda: shown.append(True))
    sign_in = built.provider_sign_in
    page = built.shell.currentPage
    with qtbot.waitSignal(sign_in.openRequested):
        built.open_link("openweir://provider?url=https%3A%2F%2Fexample.com%2F")
    assert shown == [True]
    assert sign_in.property("active")
    assert sign_in.property("step") == "address"
    assert sign_in.property("address") == "https://example.com"
    # Over the page that shows; the import form stays as it was.
    assert built.shell.currentPage == page
    assert built.profiles.property("prefillUrl") == ""
    assert not built.profiles.property("hasError")

    # A second link while the dialog waits on its address takes that one.
    built.open_link("openweir://provider?url=https%3A%2F%2Fother.example.com")
    assert sign_in.property("address") == "https://other.example.com"
    sign_in.cancel()


def test_a_provider_link_that_is_no_https_origin_says_so(qtbot: QtBot, built: ViewModels) -> None:
    built.open_link("openweir://provider?url=http%3A%2F%2Fexample.com")
    assert not built.provider_sign_in.property("active")
    assert built.shell.currentPage == "profiles"
    assert built.profiles.property("summary") == "无法识别的导入链接"


def test_an_unknown_link_says_so(qtbot: QtBot, built: ViewModels) -> None:
    built.open_link("openweir://something-else")
    assert built.shell.currentPage == "profiles"
    assert built.profiles.property("summary") == "无法识别的导入链接"
    assert built.profiles.property("hasError")


def test_the_form_opens_filled_in(qtbot: QtBot, loaded: Loaded) -> None:
    view_models = loaded.view_models
    view_models.open_link(f"clash://install-config?url={ENCODED}&name=Mine")
    root = loaded.window.contentItem()

    def field(text: str) -> object:
        return find(root, lambda i: kind(i) == "AppTextField" and i.property("text") == text)

    qtbot.waitUntil(lambda: field(SUBSCRIPTION) is not None)
    assert field("Mine") is not None
    assert loaded.focused_text() == SUBSCRIPTION
    # Nothing was imported: the user checks the link and presses 导入.
    assert len(view_models.library.snapshot().profiles) == 3
