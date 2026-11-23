"""The subscriptions page and the overview with provider accounts, in the window (DESIGN.md
"添加菜单", "服务商的订阅行"; docs/accounts.md §7.2, §7.5): the 添加 menu by mouse, keyboard
and screen reader, a provider row's menu, signing out from it, and the overview's notice. A
stand-in for api-edge on 127.0.0.1 answers; nothing reaches a real provider or browser."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, Qt, QUrl
from PySide6.QtGui import QAccessible
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from pytestqt.qtbot import QtBot

from openweir.app import load_ui
from openweir.models.provider import ProviderError, Session
from openweir.models.store import ProfileStore
from openweir.viewmodels import profiles as profiles_module
from openweir.viewmodels.composition import ViewModels, compose
from tests.support.i18n import ui_language
from tests.support.provider_server import DAY, EMAIL, GIB, PASSWORD, FakeProvider, iso
from tests.support.releases import offline_updater
from tests.support.simulated import SAMPLE_CONFIG, SimulatedFetcher, SimulatedHost
from tests.support.window import Loaded, accessible, find, kind

Role = QAccessible.Role


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    # Reach the loopback server directly even if the machine has a proxy configured.
    monkeypatch.setenv("no_proxy", "*")
    fake = FakeProvider()
    fake.config = SAMPLE_CONFIG.encode()  # rules of its own, so it can be used
    # The window dates billing by the real clock.
    now = time.time()
    for remote_id in fake.subscriptions:
        fake.update_subscription(
            remote_id, startAt=iso(now - 30 * DAY), periodStart=iso(now - 10 * DAY)
        )
    fake.update_subscription(7, expireAt=iso(now + 30 * DAY))
    fake.update_subscription(9, expireAt=iso(now - 2 * DAY))
    yield fake
    fake.close()


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The pages opened in the browser, which none of these tests reaches."""
    pages: list[str] = []

    def open_url(url: QUrl) -> bool:
        pages.append(url.toString())
        return True

    monkeypatch.setattr(profiles_module.QDesktopServices, "openUrl", open_url)
    return pages


def sign_in(qtbot: QtBot, view_models: ViewModels, provider: FakeProvider) -> str:
    """Sign in and sync as the sign-in dialog would, then let the page read it all again."""
    accounts = view_models.library.accounts

    async def signing_in() -> str:
        probe = await accounts.probe(provider.base)
        session = await accounts.sign_in_password(probe, EMAIL, PASSWORD)
        assert isinstance(session, Session)
        provider_id = (await accounts.complete(probe, session)).provider.id
        await accounts.sync(provider_id)
        return provider_id

    provider_id = view_models.runner.submit(signing_in()).result(timeout=20)
    sync(qtbot, view_models)
    return provider_id


def sync(qtbot: QtBot, view_models: ViewModels, provider_id: str = "") -> None:
    if provider_id:
        view_models.runner.submit(view_models.library.accounts.sync(provider_id)).result(20)
    profiles = view_models.profiles
    before = profiles._accounts
    profiles.reload()
    qtbot.waitUntil(lambda: profiles._accounts is not before)


def show_profiles(qtbot: QtBot, loaded: Loaded) -> QQuickItem:
    loaded.view_models.shell.setPage("profiles")
    qtbot.wait(100)
    return loaded.item(lambda i: kind(i) == "ProfilesPage")


def add_button(loaded: Loaded) -> QQuickItem:
    return loaded.item(lambda i: kind(i) == "MenuButton" and i.property("text") == "添加")


def more_button(loaded: Loaded, name: str) -> QQuickItem:
    page = loaded.item(lambda i: kind(i) == "ProfilesPage")
    row = find(page, lambda i: kind(i) == "ProfileRow" and i.property("name") == name)
    assert row is not None
    more = find(row, lambda i: kind(i) == "IconButton")
    assert more is not None
    return more


def menu_items(loaded: Loaded) -> list[QQuickItem]:
    return loaded.overlay_items(lambda i: kind(i) == "AppMenuItem")


def menu(loaded: Loaded) -> list[tuple[str, bool]]:
    """The open menu's items: their text, and whether a separator comes before them."""
    return [(i.property("text"), i.property("separatorBefore")) for i in menu_items(loaded)]


def pick(qtbot: QtBot, loaded: Loaded, text: str) -> None:
    """Reach an item of the open menu with the arrows, and run it."""
    for _ in range(16):
        loaded.key(qtbot, Qt.Key.Key_Down, wait=10)
        if loaded.focused_text() == text:
            loaded.key(qtbot, Qt.Key.Key_Return)
            return
    raise AssertionError(f"the keyboard could not reach {text!r}")


def open_menu(qtbot: QtBot, loaded: Loaded, button: QQuickItem) -> None:
    button.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    loaded.key(qtbot, Qt.Key.Key_Space)
    qtbot.waitUntil(lambda: menu_items(loaded) != [])


# -- 添加 ------------------------------------------------------------------------------------------
@pytest.mark.usefixtures("screen_reader")
def test_the_add_menu_holds_every_way_to_add(qtbot: QtBot, loaded: Loaded) -> None:
    profiles = loaded.view_models.profiles
    show_profiles(qtbot, loaded)
    add = add_button(loaded)
    # One primary button, which screen readers hear as a button with a menu.
    assert add.property("variant") == "primary"
    assert (accessible(add).role, accessible(add).name) == (Role.ButtonMenu, "添加")
    assert not loaded.item(lambda i: kind(i) == "ProfilesPage").property("importOpen")

    # Space, Enter and ↓ open it, under the button, right edges aligned; Esc gives the focus
    # back to it.
    for key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Down):
        add.forceActiveFocus(Qt.FocusReason.TabFocusReason)
        loaded.key(qtbot, key)
        assert menu(loaded) == [
            ("从链接导入…", False),
            ("导入文件…", False),
            ("导入节点…", False),
            ("登录服务商…", True),
        ], key
        assert all(accessible(i).role == Role.MenuItem for i in menu_items(loaded))
        first = menu_items(loaded)[0]
        right = first.mapToScene(first.boundingRect().topRight()).x()
        assert right <= add.mapToScene(add.boundingRect().topRight()).x()
        loaded.key(qtbot, Qt.Key.Key_Escape)
        qtbot.waitUntil(lambda: loaded.focused() is add)
        assert add.property("visualFocus")

    # 登录服务商… asks the window for the sign-in dialog, which gives the focus back to 添加.
    open_menu(qtbot, loaded, add)
    with qtbot.waitSignal(profiles.providerSignInRequested) as asked:
        pick(qtbot, loaded, "登录服务商…")
    assert asked.args == [""]
    page = loaded.item(lambda i: kind(i) == "ProfilesPage")
    assert page.property("signInTrigger") is add
    sign_in_vm = loaded.view_models.provider_sign_in
    qtbot.waitUntil(lambda: sign_in_vm.property("active"))
    assert sign_in_vm.property("address") == ""
    loaded.key(qtbot, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not sign_in_vm.property("active"))
    qtbot.waitUntil(lambda: loaded.focused() is add)

    # While an import runs, the three imports wait.
    profiles._importing = True
    profiles.changed.emit()
    open_menu(qtbot, loaded, add)
    assert [i.isEnabled() for i in menu_items(loaded)] == [False, False, False, True]
    loaded.key(qtbot, Qt.Key.Key_Escape)
    profiles._importing = False
    profiles.changed.emit()
    assert loaded.warnings == []


def test_importing_from_a_link_opens_the_form_from_the_menu(qtbot: QtBot, loaded: Loaded) -> None:
    page = show_profiles(qtbot, loaded)
    add = add_button(loaded)
    open_menu(qtbot, loaded, add)
    pick(qtbot, loaded, "从链接导入…")
    assert page.property("importOpen")
    assert kind(loaded.focused()) == "AppTextField"
    # The form's 导入 is the one primary button now; the menu still works.
    assert add.property("variant") == "secondary"
    assert add.isEnabled()

    # Chosen again, it only takes the focus back to the link.
    loaded.type(qtbot, "https://new.example.com/sub")
    open_menu(qtbot, loaded, add)
    pick(qtbot, loaded, "从链接导入…")
    assert kind(loaded.focused()) == "AppTextField"
    assert loaded.focused_text() == "https://new.example.com/sub"

    # Closing the form gives the focus back to 添加.
    loaded.key(qtbot, Qt.Key.Key_Escape)
    assert not page.property("importOpen")
    qtbot.waitUntil(lambda: loaded.focused() is add)
    assert add.property("variant") == "primary"
    assert loaded.warnings == []


def test_the_node_dialog_gives_the_focus_back_to_add(qtbot: QtBot, loaded: Loaded) -> None:
    show_profiles(qtbot, loaded)
    add = add_button(loaded)
    open_menu(qtbot, loaded, add)
    pick(qtbot, loaded, "导入节点…")
    qtbot.waitUntil(lambda: loaded.overlay_items(lambda i: kind(i) == "CodeEditor") != [])
    loaded.key(qtbot, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: loaded.focused() is add)
    assert loaded.warnings == []


# -- a provider's rows ---------------------------------------------------------------------------
def test_a_provider_row_has_the_accounts_menu(
    qtbot: QtBot, loaded: Loaded, provider: FakeProvider, opened: list[str]
) -> None:
    view_models = loaded.view_models
    profiles = view_models.profiles
    provider_id = sign_in(qtbot, view_models, provider)
    page = show_profiles(qtbot, loaded)
    qtbot.waitUntil(lambda: find(page, lambda i: i.property("name") == "专业版") is not None)

    # The caption: the site and the account; 基础版 has run out, in the warning colour.
    row = find(page, lambda i: kind(i) == "ProfileRow" and i.property("name") == "基础版")
    assert row is not None
    assert find(row, lambda i: i.property("text") == " · 已到期") is not None
    assert row.property("source") == "示例服务商 · user@example.com"

    # Its menu has the account's pages and sign-out instead of 编辑信息…, the link and 删除.
    pro = more_button(loaded, "专业版")
    open_menu(qtbot, loaded, pro)
    assert menu(loaded) == [
        ("更新", False),
        ("每 24 小时自动更新", False),
        ("编辑覆写…", False),
        ("使用它的规则", False),
        ("不加入节点池", False),
        ("在网页上续费", True),
        ("退出「示例服务商」", True),
    ]
    pick(qtbot, loaded, "在网页上续费")
    assert opened == [f"{provider.base}/subscription?id=7"]

    # One the account lost can be deleted, and has nothing to renew.
    del provider.subscriptions[9]
    sync(qtbot, view_models, provider_id)
    open_menu(qtbot, loaded, more_button(loaded, "基础版"))
    assert [text for text, _ in menu(loaded)][-3:] == ["不加入节点池", "删除", "退出「示例服务商」"]
    assert menu(loaded)[-2:] == [("删除", True), ("退出「示例服务商」", False)]
    loaded.key(qtbot, Qt.Key.Key_Escape)

    # Once the session ends, the menu offers to sign in again, on the provider's address.
    provider.revoke()
    with pytest.raises(ProviderError):
        view_models.runner.submit(view_models.library.accounts.sync(provider_id)).result(20)
    sync(qtbot, view_models)
    pro = more_button(loaded, "专业版")
    open_menu(qtbot, loaded, pro)
    assert ("重新登录…", False) in menu(loaded)
    with qtbot.waitSignal(profiles.providerSignInRequested) as asked:
        pick(qtbot, loaded, "重新登录…")
    assert asked.args == [provider.base]
    assert page.property("signInTrigger") is pro
    assert loaded.warnings == []


def test_signing_out_from_a_row_moves_the_focus_off_the_rows_that_go(
    qtbot: QtBot, loaded: Loaded, provider: FakeProvider
) -> None:
    view_models = loaded.view_models
    profiles = view_models.profiles
    sign_in(qtbot, view_models, provider)
    show_profiles(qtbot, loaded)
    qtbot.waitUntil(lambda: len(profiles.property("model").rows) == 5)

    open_menu(qtbot, loaded, more_button(loaded, "专业版"))
    pick(qtbot, loaded, "退出「示例服务商」")
    # A destructive confirmation: it names what goes, and starts on 取消.
    qtbot.waitUntil(lambda: loaded.focused_text() == "取消")
    assert profiles.property("signOutTitle") == "退出「示例服务商」？"  # noqa: RUF001
    assert "「专业版」、「基础版」会从本机删除。" in profiles.property("signOutText")
    loaded.key(qtbot, Qt.Key.Key_Tab)
    assert loaded.focused_text() == "退出登录"
    loaded.key(qtbot, Qt.Key.Key_Return)

    # Both rows go; the focus goes to the row above them, not to the other that goes.
    qtbot.waitUntil(lambda: len(profiles.property("model").rows) == 3)
    qtbot.waitUntil(lambda: loaded.focused().property("accessibleName") == "本地配置 的更多操作")
    assert loaded.focused().property("visualFocus")
    assert loaded.warnings == []


# -- the overview --------------------------------------------------------------------------------
def test_the_overview_notes_the_subscription_in_use(
    qtbot: QtBot, loaded: Loaded, provider: FakeProvider, opened: list[str]
) -> None:
    view_models = loaded.view_models
    profiles = view_models.profiles
    provider_id = sign_in(qtbot, view_models, provider)
    view_models.runner.submit(view_models.library.accounts.use(provider_id, 7)).result(20)
    provider.update_subscription(7, expireAt=iso(time.time() + 2 * DAY + 600))
    sync(qtbot, view_models, provider_id)
    view_models.shell.setPage("overview")
    qtbot.waitUntil(lambda: profiles.property("activeNotice") == "3 天后到期")
    overview = loaded.item(lambda i: kind(i) == "OverviewPage")

    notice = find(overview, lambda i: i.property("text") == "3 天后到期")
    assert notice is not None
    # 续费 is a link to the account page, on the subscription's provider (docs/accounts.md
    # §7.5): the page opens the user center from there.
    renew = find(overview, lambda i: kind(i) == "LinkButton" and i.property("text") == "续费")
    assert renew is not None
    loaded.click(qtbot, renew)
    assert view_models.shell.property("currentPage") == "account"
    assert view_models.account.property("providerId") == provider_id
    assert opened == []
    view_models.shell.setPage("overview")

    # Run out, with another subscription of the account that can be used: it is suggested,
    # and used only when the user says so.
    provider.add_subscription("user-1", 12, planName="家庭版", **window_dates())
    provider.update_subscription(7, status="expired", usable=False, expireAt=iso(time.time() - DAY))
    sync(qtbot, view_models, provider_id)
    qtbot.waitUntil(lambda: profiles.property("suggestedName") == "家庭版")
    switch = find(
        overview, lambda i: kind(i) == "LinkButton" and i.property("text") == "改用「家庭版」"
    )
    assert switch is not None
    assert profiles.property("activeName") == "专业版"
    with qtbot.waitSignal(profiles.applied, timeout=10000):
        loaded.click(qtbot, switch)
    assert profiles.property("activeName") == "家庭版"
    qtbot.waitUntil(lambda: find(overview, lambda i: i.property("text") == "已到期") is None)
    assert loaded.warnings == []


def window_dates() -> dict[str, Any]:
    now = time.time()
    return {
        "startAt": iso(now - 30 * DAY),
        "periodStart": iso(now - 10 * DAY),
        "expireAt": iso(now + 30 * DAY),
        "download": 1 * GIB,
    }


@pytest.fixture
def empty(qtbot: QtBot, tmp_path: Path) -> Iterator[Loaded]:
    """The window with no subscription at all."""
    with ui_language("zh"):
        view_models = compose(
            SimulatedHost(),
            ProfileStore(tmp_path),
            SimulatedFetcher(),
            updater=offline_updater(tmp_path),
        )
        engine = QQmlApplicationEngine()
        warnings: list[str] = []
        engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
        try:
            assert load_ui(engine, view_models)
            (window,) = engine.rootObjects()
            assert isinstance(window, QQuickWindow)
            view_models.start()
            yield Loaded(view_models, engine, window, warnings)
        finally:
            view_models.shutdown()
            for root in engine.rootObjects():
                root.deleteLater()
            del engine


def test_without_subscriptions_the_overview_offers_signing_in(
    qtbot: QtBot, empty: Loaded, qapp: QCoreApplication
) -> None:
    profiles = empty.view_models.profiles
    empty.view_models.shell.setPage("overview")
    overview = empty.item(lambda i: kind(i) == "OverviewPage")
    qtbot.waitUntil(
        lambda: find(overview, lambda i: i.property("text") == "尚未导入订阅") is not None
    )
    link = find(overview, lambda i: kind(i) == "LinkButton" and i.property("text") == "登录服务商")
    assert link is not None
    with qtbot.waitSignal(profiles.providerSignInRequested) as asked:
        empty.click(qtbot, link)
    assert asked.args == [""]
    assert overview.property("signInTrigger") is link
    assert empty.warnings == []
