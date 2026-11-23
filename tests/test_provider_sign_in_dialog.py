"""The provider sign-in dialog (ProviderSignInDialog.qml, DESIGN.md "服务商登录对话框") in the
main window, against a stand-in for api-edge on 127.0.0.1: its widths, where the focus goes on
each step, the keyboard alone, what screen readers are told, and where 从链接导入 and an
openweir://provider link lead. The steps' logic is tested in test_provider_sign_in.py."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from PySide6.QtCore import QObject, Qt, QUrl
from PySide6.QtGui import QAccessible
from PySide6.QtQuick import QQuickItem
from pytestqt.qtbot import QtBot

from openweir.viewmodels import provider_sign_in
from tests.support.provider_server import EMAIL, GIB, PASSWORD, FakeProvider
from tests.support.simulated import SAMPLE_CONFIG
from tests.support.window import Loaded, accessible, find_all, kind

Role = QAccessible.Role
COMMA = "，"  # noqa: RUF001  # between the parts of what a screen reader is told


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    """A provider whose account has 专业版 (7), 畅享版 (8), the expired 基础版 (9) and
    加速版 (12). Request it before ``loaded``."""
    monkeypatch.setenv("no_proxy", "*")
    fake = FakeProvider()
    fake.config = SAMPLE_CONFIG.encode()
    fake.add_subscription("user-1", 8, planName="畅享版", upload=0, download=1 * GIB)
    fake.add_subscription("user-1", 12, planName="加速版", upload=0, download=50 * GIB)
    yield fake
    fake.close()


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []

    def open_url(url: QUrl) -> bool:
        opened.append(url.toString())
        return True

    monkeypatch.setattr(provider_sign_in.QDesktopServices, "openUrl", open_url)
    return opened


def dialog(loaded: Loaded) -> QObject:
    return next(
        child
        for child in loaded.window.findChildren(QObject)
        if child.metaObject().className().startswith("ProviderSignInDialog_")
    )


def shown(loaded: Loaded, test: Any) -> list[QQuickItem]:
    """What the dialog shows that passes ``test``."""
    return find_all(dialog(loaded).property("contentItem"), test)


def button(loaded: Loaded, text: str) -> QQuickItem:
    (found,) = shown(
        loaded, lambda i: kind(i) in ("AppButton", "ExternalButton") and i.property("text") == text
    )
    return found


def opened(qtbot: QtBot, loaded: Loaded) -> QObject:
    box = dialog(loaded)
    qtbot.waitUntil(lambda: box.property("opened"))
    return box


def drain(qtbot: QtBot, loaded: Loaded) -> None:
    """Wait until the answers the runner had on its way reached the Qt thread: one submitted
    later answers after them."""
    done: list[bool] = []

    async def nothing() -> None:
        return None

    loaded.view_models.runner.submit(nothing(), lambda _: done.append(True))
    qtbot.waitUntil(lambda: bool(done), timeout=10_000)


def page_button(qtbot: QtBot, loaded: Loaded) -> QQuickItem:
    """The subscriptions page's 从链接导入, focused as the keyboard would."""
    loaded.view_models.shell.setPage("profiles")
    qtbot.wait(100)
    found = loaded.item(lambda i: kind(i) == "AppButton" and i.property("text") == "从链接导入")
    found.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    return found


def focused_field(loaded: Loaded) -> str:
    """The accessible name of the text field with the focus, "" for anything else. Qt tells
    screen readers no name for a password field, only that it is one: "password" here."""
    item = loaded.focused()
    if kind(item) != "AppTextField":
        return ""
    interface = QAccessible.queryAccessibleInterface(item)
    if interface is not None and interface.state().passwordEdit:
        return "password"
    return accessible(item).name


@pytest.mark.usefixtures("screen_reader")
def test_signing_in_and_picking_by_keyboard_alone(
    qtbot: QtBot, provider: FakeProvider, loaded: Loaded
) -> None:
    vm = loaded.view_models.provider_sign_in
    start = page_button(qtbot, loaded)
    vm.open("")
    box = opened(qtbot, loaded)
    assert box.property("width") == 400
    assert box.property("title") == "登录服务商"
    assert focused_field(loaded) == "服务商地址"
    (heading,) = shown(loaded, lambda i: kind(i) == "UiText" and i.property("text") == "登录服务商")
    assert accessible(heading).role == Role.Heading

    loaded.type(qtbot, provider.base)
    loaded.key(qtbot, Qt.Key.Key_Return)  # 继续
    qtbot.waitUntil(lambda: vm.property("step") == "sign_in")
    qtbot.waitUntil(lambda: focused_field(loaded) == "邮箱或用户名")
    assert box.property("title") == "登录「示例服务商」"
    assert box.property("width") == 400
    methods = shown(loaded, lambda i: kind(i) == "SegmentedControl")
    assert [accessible(m).name for m in methods] == ["登录方式"]
    back = button(loaded, "上一步")
    cancel = button(loaded, "取消")
    sign_in = button(loaded, "登录")
    # 上一步 sits apart at the left end of the button row.
    assert (
        back.mapToScene(back.boundingRect().topLeft()).x()
        < cancel.mapToScene(cancel.boundingRect().topLeft()).x()
    )
    assert not sign_in.isEnabled()

    loaded.type(qtbot, EMAIL)
    loaded.key(qtbot, Qt.Key.Key_Tab)
    assert focused_field(loaded) == "password"
    loaded.type(qtbot, "wrong password")
    loaded.key(qtbot, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: vm.property("passwordError") != "")
    # The error is under the field and in its description; the field has the focus back,
    # its text selected to be typed over.
    qtbot.waitUntil(lambda: focused_field(loaded) == "password")
    error = vm.property("passwordError")
    assert accessible(loaded.focused()).description == error
    assert shown(loaded, lambda i: kind(i) == "AccessibleText" and i.property("text") == error)
    loaded.type(qtbot, PASSWORD)
    loaded.key(qtbot, Qt.Key.Key_Return)

    # Profiles are in use already: the subscriptions to pick from, wide.
    qtbot.waitUntil(lambda: vm.property("step") == "pick", timeout=10_000)
    qtbot.waitUntil(lambda: box.property("width") == 640)
    assert box.property("title") == "选择要使用的订阅"
    picker = loaded.focused()
    assert (accessible(picker).role, accessible(picker).name) == (Role.List, "订阅")
    assert loaded.focus_ring(picker) is not None
    assert vm.property("selected") == 8  # the most left
    rows = {
        accessible(r).name: r
        for r in shown(loaded, lambda i: accessible(i).role == Role.RadioButton)
    }
    assert sorted(rows) == ["专业版", "加速版", "基础版", "畅享版"]
    assert accessible(rows["畅享版"]).checked
    assert not rows["基础版"].isEnabled()
    assert accessible(rows["专业版"]).description.startswith("有效" + COMMA + "已用 10.0 / 100 GB")

    heard = loaded.announcements()
    loaded.key(qtbot, Qt.Key.Key_Up)
    assert vm.property("selected") == 7
    loaded.key(qtbot, Qt.Key.Key_Up)  # the first: it stays
    assert vm.property("selected") == 7
    loaded.key(qtbot, Qt.Key.Key_Down)
    loaded.key(qtbot, Qt.Key.Key_Down)  # past 基础版, which cannot be used
    assert vm.property("selected") == 12
    assert loaded.focused() is picker
    assert [h.split(COMMA)[0] for h in heard] == ["专业版", "畅享版", "加速版"]

    # The list, then each 续费 of what cannot be used, then the buttons.
    loaded.key(qtbot, Qt.Key.Key_Tab)
    renew = loaded.focused()
    assert kind(renew) == "ExternalButton"
    assert accessible(renew).name == "续费 基础版（在浏览器中打开）"  # noqa: RUF001
    assert loaded.focus_ring(renew) is not None
    loaded.key(qtbot, Qt.Key.Key_Tab)
    assert loaded.focused().property("text") == "稍后"
    loaded.key(qtbot, Qt.Key.Key_Tab)
    assert loaded.focused().property("text") == "使用"
    loaded.key(qtbot, Qt.Key.Key_Tab)
    assert loaded.focused() is picker

    loaded.key(qtbot, Qt.Key.Key_Return)  # 使用
    qtbot.waitUntil(lambda: not box.property("visible"), timeout=10_000)
    twelve = next(p for p in loaded.view_models.library.snapshot().profiles if p.remote_id == 12)
    assert loaded.view_models.library.snapshot().active == twelve.id
    assert loaded.focused() is start
    assert loaded.warnings == []


@pytest.mark.usefixtures("screen_reader")
def test_esc_cancels_even_while_waiting(
    qtbot: QtBot, provider: FakeProvider, loaded: Loaded
) -> None:
    vm = loaded.view_models.provider_sign_in
    accounts = loaded.view_models.library.accounts
    release, finished = threading.Event(), threading.Event()
    real = accounts.probe

    async def held(address: str) -> Any:
        while not release.is_set():
            await asyncio.sleep(0.01)
        try:
            return await real(address)
        finally:
            finished.set()

    accounts.probe = held  # type: ignore[method-assign]
    start = page_button(qtbot, loaded)
    vm.open(provider.base)
    box = opened(qtbot, loaded)
    loaded.key(qtbot, Qt.Key.Key_Return)  # 继续
    assert vm.property("busy")
    assert button(loaded, "正在检查…").isEnabled() is False
    field = next(i for i in shown(loaded, lambda i: kind(i) == "AppTextField"))
    assert not field.isEnabled()

    loaded.key(qtbot, Qt.Key.Key_Escape)
    assert not box.property("visible")
    assert not vm.property("active")
    assert loaded.focused() is start
    release.set()
    qtbot.waitUntil(finished.is_set, timeout=10_000)
    drain(qtbot, loaded)
    # The probe's answer was dropped: nothing opened again.
    assert not vm.property("active")
    assert vm.property("step") == "address"
    assert not box.property("visible")
    assert loaded.warnings == []


def test_link_import_for_a_provider_that_signs_in_on_the_web(
    qtbot: QtBot, provider: FakeProvider, loaded: Loaded
) -> None:
    provider.scenes = ["sign_in", "send_code"]
    vm = loaded.view_models.provider_sign_in
    loaded.view_models.shell.setPage("overview")
    vm.open(provider.base)
    box = opened(qtbot, loaded)
    loaded.key(qtbot, Qt.Key.Key_Return)  # 继续
    qtbot.waitUntil(lambda: vm.property("step") == "sign_in")
    qtbot.waitUntil(lambda: loaded.focused().property("text") == "从链接导入")
    assert shown(loaded, lambda i: i.property("text") == vm.property("unavailable"))
    assert not shown(loaded, lambda i: kind(i) == "AppTextField")
    assert not shown(loaded, lambda i: kind(i) == "SegmentedControl")

    loaded.key(qtbot, Qt.Key.Key_Return)  # 从链接导入
    qtbot.waitUntil(lambda: not box.property("visible"))
    # The subscriptions page's link form, its link field focused.
    qtbot.waitUntil(lambda: loaded.view_models.shell.property("currentPage") == "profiles")
    qtbot.waitUntil(lambda: kind(loaded.focused()) == "AppTextField")
    qtbot.wait(250)
    focused = loaded.focused()
    assert kind(focused) == "AppTextField"
    assert focused.property("text") == ""
    assert focused.property("placeholderText")
    assert loaded.warnings == []


@pytest.mark.usefixtures("screen_reader")
def test_no_subscriptions_offers_the_plans(
    qtbot: QtBot, provider: FakeProvider, loaded: Loaded, browser: list[str]
) -> None:
    provider.subscriptions = {
        k: v for k, v in provider.subscriptions.items() if v["userId"] != "user-1"
    }
    vm = loaded.view_models.provider_sign_in
    vm.open(provider.base)
    box = opened(qtbot, loaded)
    loaded.key(qtbot, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: vm.property("step") == "sign_in")
    vm.signInPassword(EMAIL, PASSWORD)
    qtbot.waitUntil(lambda: vm.property("step") == "empty", timeout=10_000)
    qtbot.waitUntil(lambda: loaded.focused().property("text") == "完成")
    assert box.property("width") == 400
    plans = button(loaded, "选购套餐")
    assert kind(plans) == "ExternalButton"
    loaded.key(qtbot, Qt.Key.Key_Tab)  # 完成 is last: 选购套餐 before it
    assert loaded.focused() is plans
    assert accessible(plans).name == "选购套餐（在浏览器中打开）"  # noqa: RUF001
    loaded.key(qtbot, Qt.Key.Key_Space)
    assert browser == [f"{provider.base}/plans"]
    loaded.key(qtbot, Qt.Key.Key_Tab)
    assert loaded.focused().property("text") == "完成"
    loaded.key(qtbot, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: not box.property("visible"))
    assert not vm.property("active")


@pytest.mark.parametrize("loaded", ["zh", "en"], indirect=True)
def test_every_step_fits_the_smallest_window(
    qtbot: QtBot, provider: FakeProvider, loaded: Loaded
) -> None:
    provider.users["user-1"].totp = "246810"
    window = loaded.window
    window.setWidth(window.minimumWidth())
    window.setHeight(window.minimumHeight())
    vm = loaded.view_models.provider_sign_in
    vm.open(provider.base)
    box = opened(qtbot, loaded)

    def fits() -> None:
        qtbot.wait(100)
        content = box.property("contentItem")
        for item in find_all(content, lambda i: bool(i.property("text"))):
            if kind(item) == "AppTextField":
                continue
            assert loaded.on_screen(item), item.property("text")
            assert not item.property("truncated"), item.property("text")

    fits()
    vm.submitAddress(provider.base)
    qtbot.waitUntil(lambda: vm.property("step") == "sign_in")
    fits()
    vm.setMethod("email_code")
    fits()
    vm.setMethod("password")
    vm.signInPassword(EMAIL, PASSWORD)
    qtbot.waitUntil(lambda: vm.property("step") == "two_factor", timeout=10_000)
    fits()
    vm.verify("246810")
    qtbot.waitUntil(lambda: vm.property("step") == "pick", timeout=10_000)
    qtbot.waitUntil(lambda: box.property("width") == 640)
    fits()
    loaded.key(qtbot, Qt.Key.Key_Escape)
    assert loaded.warnings == []


def test_the_dialog_opens_again_while_it_fades_out(
    qtbot: QtBot, provider: FakeProvider, loaded: Loaded
) -> None:
    vm = loaded.view_models.provider_sign_in
    vm.open("")
    box = opened(qtbot, loaded)
    loaded.key(qtbot, Qt.Key.Key_Escape, wait=0)
    assert not vm.property("active")
    vm.open(provider.base)  # before the fade has ended
    qtbot.waitUntil(lambda: box.property("opened"))
    qtbot.wait(250)
    assert box.property("visible")
    assert vm.property("active")
    field = next(i for i in shown(loaded, lambda i: kind(i) == "AppTextField"))
    assert field.property("text") == provider.base
    assert loaded.warnings == []
