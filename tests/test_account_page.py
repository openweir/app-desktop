"""The 账户 page (DESIGN.md "账户页"; docs/accounts.md §6, §7.1, §7.4): one provider account's
subscriptions, wallet, plans and orders, read-only but for auto-renew, with the user center's
pages behind external-link buttons; its states; the sidebar that lists it while there is an
account; and signing out from it. A stand-in for api-edge on 127.0.0.1 answers, and the
browser is a list of the pages it was asked to open: nothing reaches a real provider."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QObject, QRectF, Qt, QUrl
from PySide6.QtGui import QAccessible
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from pytestqt.qtbot import QtBot

from openweir.app import load_ui
from openweir.models import format as fmt
from openweir.models.credentials import KeyringCredentials
from openweir.models.provider import Currency, Session
from openweir.models.store import ProfileStore
from openweir.viewmodels import account as account_module
from openweir.viewmodels.composition import ViewModels, compose
from tests.support.i18n import ui_language
from tests.support.provider_server import EMAIL, PASSWORD, FakeProvider
from tests.support.releases import offline_updater
from tests.support.simulated import (
    SAMPLE_CONFIG,
    SimulatedFetcher,
    SimulatedHost,
    seed_profiles,
    seed_provider,
)
from tests.support.window import Loaded, accessible, find, find_all, kind

Role = QAccessible.Role
# How long to wait for the stand-in provider and the library, which a full test run slows.
WAIT = 20_000
# What an external-link button's name ends in (DESIGN.md "外链按钮").
BROWSER = "（在浏览器中打开）"  # noqa: RUF001


def prop(view_model: QObject, name: str) -> Any:
    return view_model.property(name)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    # Reach the loopback server directly even if the machine has a proxy configured.
    monkeypatch.setenv("no_proxy", "*")
    fake = FakeProvider()
    fake.config = SAMPLE_CONFIG.encode()  # rules of its own, so it can be used
    yield fake
    fake.close()


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The pages opened in the browser, which none of these tests reaches."""
    pages: list[str] = []

    def open_url(url: QUrl) -> bool:
        pages.append(url.toString())
        return True

    monkeypatch.setattr(account_module.QDesktopServices, "openUrl", open_url)
    return pages


@pytest.fixture
def window(
    request: pytest.FixtureRequest, qtbot: QtBot, tmp_path: Path, provider: FakeProvider
) -> Iterator[Loaded]:
    """The main window, signed in to ``provider`` as the sign-in dialog would have left it
    (scripts/preview.py --provider), in Chinese or the language given by indirect
    parametrization. The window runs by the real clock, the provider's data around it."""
    store = ProfileStore(tmp_path)
    now = time.time()
    seed_profiles(store, now)
    seed_provider(store, provider, KeyringCredentials(), now)
    with ui_language(getattr(request, "param", "zh")):
        view_models = compose(
            SimulatedHost(), store, SimulatedFetcher(), updater=offline_updater(tmp_path)
        )
        engine = QQmlApplicationEngine()
        warnings: list[str] = []
        engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
        try:
            assert load_ui(engine, view_models)
            (root,) = engine.rootObjects()
            assert isinstance(root, QQuickWindow)
            view_models.start()
            yield Loaded(view_models, engine, root, warnings)
        finally:
            view_models.shutdown()
            for item in engine.rootObjects():
                item.deleteLater()
            del engine


def listed(view_models: ViewModels) -> list[str]:
    return [row.page for row in prop(view_models.shell, "pages").rows]


def ready(view_models: ViewModels) -> bool:
    """The account synced, its site read, and the wallet, plans and orders in."""
    account = view_models.account
    return (
        prop(account, "subscriptionCount") == 2
        and prop(account, "walletBalance").startswith("¥")
        and prop(account, "plansNote") == ""
        and prop(account, "ordersNote") == ""
    )


def show(qtbot: QtBot, loaded: Loaded) -> QQuickItem:
    """Open the account page from the sidebar once it lists it, and wait for its data."""
    view_models = loaded.view_models
    qtbot.waitUntil(lambda: "account" in listed(view_models), timeout=WAIT)
    view_models.shell.setPage("account")
    qtbot.waitUntil(lambda: ready(view_models), timeout=WAIT)
    qtbot.wait(50)
    return loaded.item(lambda i: kind(i) == "AccountPage")


def button(page: QQuickItem, text: str, index: int = 0) -> QQuickItem:
    found = find_all(
        page,
        lambda i: kind(i) in ("AppButton", "ExternalButton") and i.property("text") == text,
    )
    assert len(found) > index, text
    return found[index]


def more_button(page: QQuickItem) -> QQuickItem:
    found = find(page, lambda i: i.property("accessibleName") == "账户的更多操作")
    assert found is not None
    return found


def press(qtbot: QtBot, loaded: Loaded, control: QQuickItem) -> None:
    """Tab to ``control``, which scrolls it into view, and press Space."""
    control.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    qtbot.wait(10)
    loaded.key(qtbot, Qt.Key.Key_Space)


def pick(qtbot: QtBot, loaded: Loaded, text: str) -> None:
    """Reach an item of the open menu with the arrows, and run it."""
    qtbot.waitUntil(
        lambda: any(
            i.isVisible() for i in loaded.overlay_items(lambda i: kind(i) == "AppMenuItem")
        ),
        timeout=WAIT,
    )
    for _ in range(8):
        loaded.key(qtbot, Qt.Key.Key_Down, wait=10)
        if loaded.focused_text() == text:
            loaded.key(qtbot, Qt.Key.Key_Return)
            return
    raise AssertionError(f"the keyboard could not reach {text!r}")


def seconds(value: str) -> float:
    """The fake provider's time, as unix seconds."""
    return datetime.fromisoformat(value).timestamp()


def call(view_models: ViewModels, coroutine: Any) -> Any:
    return view_models.runner.submit(coroutine).result(timeout=20)


# -- money -----------------------------------------------------------------------------------
def test_money_takes_the_sites_currency() -> None:
    cny = Currency("CNY", "¥", 2)
    assert fmt.money(1500, "CNY", cny) == "¥15.00"
    assert fmt.money(150_000_00, "CNY", cny) == "¥150,000.00"  # grouped by thousands
    assert fmt.money(-5, "CNY", cny) == "-¥0.05"
    # Another currency than the provider's, or one the site has not told yet: by its code.
    assert fmt.money(1500, "USD", cny) == "USD 15.00"
    assert fmt.money(1500, "CNY", None) == "CNY 15.00"
    # The site's decimal places, whatever they are.
    assert fmt.money(1500, "JPY", Currency("JPY", "¥", 0)) == "¥1,500"
    assert fmt.money(1500, "KWD", Currency("KWD", "KD", 3)) == "KD1.500"


# -- the sidebar ---------------------------------------------------------------------------------
def test_the_sidebar_lists_the_page_while_there_is_an_account(qtbot: QtBot, tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    seed_profiles(store, time.time())
    view_models = compose(SimulatedHost(), store, SimulatedFetcher())
    shell = view_models.shell
    try:
        view_models.start()
        qtbot.wait(200)
        assert "account" not in listed(view_models)
        shell.setPage("account")  # a stale link
        assert prop(shell, "currentPage") == "overview"

        # Before 设置, while an account is here.
        shell.has_accounts(True)
        assert listed(view_models)[-2:] == ["account", "settings"]
        shell.setPage("account")
        assert prop(shell, "currentPage") == "account"
        assert prop(shell, "pageIndex") == 6

        # The last one goes under the page: the subscriptions page, whose 添加 signs in.
        with qtbot.waitSignal(shell.leavingPage) as leaving:
            shell.has_accounts(False)
        assert leaving.args == ["profiles"]
        assert prop(shell, "currentPage") == "profiles"
        assert "account" not in listed(view_models)

        # Elsewhere, the page stays where it is.
        shell.has_accounts(True)
        shell.setPage("logs")
        shell.has_accounts(False)
        assert prop(shell, "currentPage") == "logs"
    finally:
        view_models.shutdown()


# -- what the page shows -----------------------------------------------------------------------
def test_the_page_shows_the_account(qtbot: QtBot, window: Loaded, provider: FakeProvider) -> None:
    view_models = window.view_models
    account = view_models.account
    page = show(qtbot, window)
    labels = [row.label for row in prop(view_models.shell, "pages").rows]
    assert labels[-2:] == ["账户", "设置"]
    assert (prop(account, "status"), prop(account, "statusTone")) == (
        "user@example.com · 示例服务商",
        "",
    )
    assert not prop(account, "multiple")

    # 当前订阅: nothing of the account is in use, so the one with the most left.
    assert prop(account, "currentName") == "专业版"
    assert (prop(account, "currentStatus"), prop(account, "currentTone")) == ("有效", "")
    assert prop(account, "currentUsage") == "10.0 / 100 GB"
    assert prop(account, "currentHasQuota")
    assert prop(account, "currentUsedFraction") == pytest.approx(0.1)
    assert prop(account, "currentDevices") == "2 / 3 台在线"
    assert prop(account, "currentExpiry") == fmt.date(
        seconds(provider.subscriptions[7]["expireAt"])
    )
    assert prop(account, "currentReset") not in ("", "—")
    assert not prop(account, "autoRenew")
    assert prop(account, "canSetAutoRenew")

    # 此账户的订阅: the expired one is renewed in the browser, the other can be used.
    rows = [
        (r.name, r.status, r.tone, r.inUse, r.action) for r in prop(account, "subscriptions").rows
    ]
    assert rows == [
        ("专业版", "有效", "", False, "use"),
        ("基础版", "已到期", "warning", False, "renew"),
    ]
    title = "此账户的订阅（2）"  # noqa: RUF001
    assert find(page, lambda i: i.property("text") == title) is not None

    # 钱包, in the site's currency, money in monospace.
    assert prop(account, "walletBalance") == "¥15.00"
    assert prop(account, "walletPoints") == "120"
    assert prop(account, "walletLevel") == "白银 · 优惠 5%"
    balance = find(page, lambda i: i.property("text") == "¥15.00" and kind(i) == "AccessibleText")
    label = find(page, lambda i: i.property("text") == "余额")
    assert balance is not None
    assert label is not None
    assert balance.property("font").family() != label.property("font").family()

    # 套餐: the prices per period, and nothing to buy when sold out.
    plans = [(r.name, r.detail, r.prices, r.soldOut) for r in prop(account, "plans").rows]
    assert plans == [
        (
            "专业版",
            "每月 100 GB · 3 台设备",
            [{"amount": "¥15.00", "per": " / 月"}, {"amount": "¥150.00", "per": " / 年"}],
            False,
        ),
        ("流量包 50 GiB", "流量包 · 50.0 GB", [{"amount": "¥5.00", "per": ""}], True),
    ]
    assert find(page, lambda i: i.property("text") == "已售罄") is not None

    # 订单: the latest, withdrawals left out; the one waiting for payment has 去支付.
    orders = [
        (r.title, r.detail, r.amount, r.status, r.pending) for r in prop(account, "orders").rows
    ]
    placed = fmt.date(seconds(provider.orders["user-1"][0]["createdAt"]))
    assert orders == [
        ("专业版", f"续费 · 月付 · {placed}", "¥15.00", "待支付", True),
        ("专业版", f"新购 · 月付 · {placed}", "¥15.00", "已完成", False),
    ]
    pay = find_all(page, lambda i: kind(i) == "ExternalButton" and i.property("text") == "去支付")
    assert len(pay) == 1
    request = provider.requests_for("orders")[-1]
    assert "pageSize=5" in request.query
    assert window.warnings == []


def test_an_order_paid_in_part_shows_what_it_cost(
    qtbot: QtBot, window: Loaded, provider: FakeProvider
) -> None:
    # Paid with the balance after a discount: the order's total, not what a channel charged.
    orders = provider.orders["user-1"]
    orders[2].update(price=3000, discount=300, amount=0, paidAmount=0, currency="USD")
    show(qtbot, window)
    account = window.view_models.account
    assert [r.amount for r in prop(account, "orders").rows] == ["¥15.00", "USD 27.00"]


# -- auto-renew -----------------------------------------------------------------------------------
def test_auto_renew_is_the_one_change_and_goes_back_when_refused(
    qtbot: QtBot, window: Loaded, provider: FakeProvider
) -> None:
    account = window.view_models.account
    page = show(qtbot, window)
    switch = find(page, lambda i: kind(i) == "AppSwitch")
    assert switch is not None
    press(qtbot, window, switch)
    # At once, and the switch waits for the provider before it takes another change.
    assert prop(account, "autoRenew")
    qtbot.waitUntil(lambda: prop(account, "canSetAutoRenew"), timeout=WAIT)
    assert prop(account, "autoRenew")
    (patch,) = provider.requests_for("auto_renew")
    assert patch.path.endswith("/subscriptions/7")
    assert patch.json() == {"autoRenew": True}
    assert provider.subscriptions[7]["autoRenew"] is True

    # Refused: back where it was, the reason under it in danger.
    provider.fail("auto_renew", 500)
    press(qtbot, window, switch)
    qtbot.waitUntil(lambda: prop(account, "autoRenewError") != "", timeout=WAIT)
    qtbot.waitUntil(lambda: prop(account, "canSetAutoRenew"), timeout=WAIT)
    assert prop(account, "autoRenew")
    assert switch.property("checked")
    assert "无法连接服务商" in prop(account, "autoRenewError")
    error = find(page, lambda i: i.property("text") == prop(account, "autoRenewError"))
    assert error is not None
    assert prop(account, "status") == "user@example.com · 示例服务商"  # no header error
    assert window.warnings == []


# -- the browser ---------------------------------------------------------------------------------
def test_every_purchase_opens_the_user_center_and_is_read_again_on_return(
    qtbot: QtBot,
    window: Loaded,
    provider: FakeProvider,
    opened: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view_models = window.view_models
    accounts = view_models.library.accounts
    watched: list[str] = []
    watch = accounts.watch

    async def watching(provider_id: str) -> None:
        watched.append(provider_id)
        await watch(provider_id)

    monkeypatch.setattr(accounts, "watch", watching)
    page = show(qtbot, window)
    base = provider.base
    for text, index, address in (
        ("续费", 0, "/subscription?id=7"),  # 当前订阅
        ("更换套餐", 0, "/plans"),
        ("续费", 1, "/subscription?id=9"),  # 基础版, which ran out
        ("充值", 0, "/wallet"),
        ("明细", 0, "/wallet"),
        ("选购", 0, "/plans?checkout=pro:month"),
        ("去支付", 0, "/orders"),
        ("密码与两步验证", 0, "/settings/security"),
        ("登录设备", 0, "/settings/devices"),
    ):
        target = button(page, text, index)
        assert kind(target) == "ExternalButton", text
        count = len(opened)
        press(qtbot, window, target)
        assert opened[count:] == [base + address], text
    pid = prop(view_models.account, "providerId")
    qtbot.waitUntil(lambda: len(watched) == 9, timeout=WAIT)
    assert set(watched) == {pid}

    # The more menu's 在用户中心打开.
    more = more_button(page)
    more.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    window.key(qtbot, Qt.Key.Key_Space)
    items = window.overlay_items(lambda i: kind(i) == "AppMenuItem")
    assert [(i.property("text"), i.property("separatorBefore")) for i in items] == [
        ("刷新", False),
        ("在用户中心打开", False),
        ("退出登录", True),
    ]
    assert items[2].property("destructive")
    pick(qtbot, window, "在用户中心打开")
    assert opened[-1] == base + "/"

    # Back in the window, what was bought or paid for is read again.
    wallets = len(provider.requests_for("wallet"))
    view_models.desktop.activated.emit()
    qtbot.waitUntil(lambda: len(provider.requests_for("wallet")) > wallets, timeout=WAIT)
    assert window.warnings == []


# -- states --------------------------------------------------------------------------------------
def test_an_account_to_sign_in_again_keeps_its_data_and_offers_the_way_back(
    qtbot: QtBot, window: Loaded, provider: FakeProvider
) -> None:
    view_models = window.view_models
    account = view_models.account
    page = show(qtbot, window)
    provider.revoke()
    account.refresh()
    qtbot.waitUntil(lambda: prop(account, "needsSignIn"), timeout=WAIT)
    assert (prop(account, "status"), prop(account, "statusTone")) == (
        "需要重新登录 · 示例服务商",
        "warning",
    )
    # The last synced subscriptions stay; the wallet, plans and orders wait for a sign-in.
    assert prop(account, "currentName") == "专业版"
    assert prop(account, "signInText") == "需要重新登录"
    assert not prop(account, "canSetAutoRenew")
    for note in ("walletNote", "plansNote", "ordersNote"):
        assert prop(account, note) == "重新登录后显示"
    assert prop(account, "orders").rows == []
    wallets = len(provider.requests_for("wallet"))
    view_models.shell.setPage("overview")
    view_models.shell.setPage("account")
    qtbot.wait(200)
    assert len(provider.requests_for("wallet")) == wallets  # not asked for

    # 重新登录…: the sign-in dialog on the provider's address, and back to the button.
    again = button(page, "重新登录…")
    again.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    sign_in = view_models.provider_sign_in
    window.key(qtbot, Qt.Key.Key_Space)
    qtbot.waitUntil(lambda: prop(sign_in, "active"), timeout=WAIT)
    assert prop(sign_in, "address") == provider.base
    window.key(qtbot, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: window.focused() is again, timeout=WAIT)

    # Signed in again: the account is read again, and so are its wallet, plans and orders.
    accounts = view_models.library.accounts

    async def signing_in() -> str:
        probe = await accounts.probe(provider.base)
        session = await accounts.sign_in_password(probe, EMAIL, PASSWORD)
        assert isinstance(session, Session)
        return (await accounts.complete(probe, session)).provider.id

    pid = call(view_models, signing_in())
    sign_in.signedIn.emit(pid)
    qtbot.waitUntil(lambda: not prop(account, "needsSignIn"), timeout=WAIT)
    qtbot.waitUntil(lambda: ready(view_models), timeout=WAIT)
    assert prop(account, "status") == "user@example.com · 示例服务商"
    assert window.warnings == []


def test_offline_the_page_says_when_it_last_synced(
    qtbot: QtBot, window: Loaded, provider: FakeProvider
) -> None:
    view_models = window.view_models
    account = view_models.account
    show(qtbot, window)
    provider.fail("subscriptions", 503, times=3)
    call(view_models, view_models.library.accounts.sync_all(max_age=0))  # recorded, not raised
    account.reload()
    qtbot.waitUntil(lambda: prop(account, "status") == "上次同步 刚刚", timeout=WAIT)
    assert prop(account, "statusTone") == ""
    assert prop(account, "currentName") == "专业版"  # what it said then

    # 刷新 that fails says why, in danger.
    account.refresh()
    qtbot.waitUntil(lambda: prop(account, "statusTone") == "danger", timeout=WAIT)
    assert prop(account, "status").startswith("无法连接服务商")
    assert not prop(account, "busy")


def test_a_group_that_cannot_be_read_says_why_and_the_others_show(
    qtbot: QtBot, window: Loaded, provider: FakeProvider
) -> None:
    view_models = window.view_models
    account = view_models.account
    provider.fail("wallet", 500, times=5)
    qtbot.waitUntil(lambda: "account" in listed(view_models), timeout=WAIT)
    view_models.shell.setPage("account")
    qtbot.waitUntil(lambda: prop(account, "walletNoteDanger"), timeout=WAIT)
    assert prop(account, "walletNote").startswith("无法连接服务商")
    assert prop(account, "walletBalance") == ""
    qtbot.waitUntil(
        lambda: prop(account, "ordersNote") == "" and prop(account, "plansNote") == "", timeout=WAIT
    )
    page = window.item(lambda i: kind(i) == "AccountPage")
    assert button(page, "充值").isVisible()  # the user center still works
    assert window.warnings == []


# -- using and signing out -----------------------------------------------------------------------
def test_using_a_subscription_of_the_account(
    qtbot: QtBot, window: Loaded, provider: FakeProvider
) -> None:
    view_models = window.view_models
    account, profiles = view_models.account, view_models.profiles
    page = show(qtbot, window)
    use = button(page, "使用")
    with qtbot.waitSignal(account.applied, timeout=WAIT):
        press(qtbot, window, use)
    qtbot.waitUntil(lambda: prop(account, "subscriptions").rows[0].inUse, timeout=WAIT)
    qtbot.waitUntil(lambda: prop(profiles, "activeName") == "专业版", timeout=WAIT)
    assert find(page, lambda i: i.property("text") == "使用中") is not None
    assert provider.requests_for("config")  # its config, downloaded first
    assert window.warnings == []


def test_signing_out_of_the_last_account_goes_to_the_subscriptions_page(
    qtbot: QtBot, window: Loaded, provider: FakeProvider
) -> None:
    view_models = window.view_models
    account, shell, profiles = view_models.account, view_models.shell, view_models.profiles
    show(qtbot, window)
    pid = prop(account, "providerId")
    call(view_models, view_models.library.accounts.use(pid, 7))
    view_models.profiles.reload()
    qtbot.waitUntil(lambda: prop(account, "subscriptions").rows[0].inUse, timeout=WAIT)
    page = window.item(lambda i: kind(i) == "AccountPage")

    # From 账户与安全, by the keyboard: a destructive confirmation that names what goes and
    # whose rules come next.
    sign_out = button(page, "退出登录")
    sign_out.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    window.key(qtbot, Qt.Key.Key_Space)
    qtbot.waitUntil(lambda: window.focused_text() == "取消", timeout=WAIT)
    assert prop(account, "signOutTitle") == "退出「示例服务商」？"  # noqa: RUF001
    text = prop(account, "signOutText")
    assert "「专业版」、「基础版」会从本机删除。" in text
    assert "之后改用「示例订阅 A」的规则" in text
    window.key(qtbot, Qt.Key.Key_Tab)
    assert window.focused_text() == "退出登录"
    window.key(qtbot, Qt.Key.Key_Return)

    # The page and its tab go; 添加 on the subscriptions page has the focus, with its ring.
    qtbot.waitUntil(lambda: "account" not in listed(view_models), timeout=WAIT)
    assert prop(shell, "currentPage") == "profiles"
    add = window.item(lambda i: kind(i) == "MenuButton" and i.property("text") == "添加")
    qtbot.waitUntil(lambda: window.focused() is add, timeout=WAIT)
    # The ring shows once the window has the focus back from the dialog, a moment later on
    # macOS.
    qtbot.waitUntil(lambda: add.property("visualFocus"), timeout=WAIT)
    qtbot.waitUntil(lambda: len(prop(profiles, "model").rows) == 3, timeout=WAIT)
    assert prop(profiles, "activeName") == "示例订阅 A"
    assert provider.requests_for("sign_out")
    assert window.warnings == []


def test_signing_out_on_the_subscriptions_page_takes_the_page_away(
    qtbot: QtBot, window: Loaded
) -> None:
    view_models = window.view_models
    shell, profiles = view_models.shell, view_models.profiles
    show(qtbot, window)
    shell.setPage("profiles")
    qtbot.waitUntil(lambda: len(prop(profiles, "model").rows) == 5, timeout=WAIT)
    row = next(r for r in prop(profiles, "model").rows if r.isProvider)
    with qtbot.waitSignal(profiles.signOutRequested, timeout=WAIT):
        profiles.requestSignOut(row.profileId)
    profiles.confirmSignOut()
    qtbot.waitUntil(lambda: "account" not in listed(view_models), timeout=WAIT)
    assert prop(shell, "currentPage") == "profiles"
    assert not prop(view_models.account, "hasAccounts")
    assert window.warnings == []


def test_several_providers_are_chosen_in_the_header(
    qtbot: QtBot, window: Loaded, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    view_models = window.view_models
    account, shell = view_models.account, view_models.shell
    page = show(qtbot, window)
    first = prop(account, "providerId")
    other = FakeProvider()
    try:
        other.site["name"] = "另一个服务商"
        other.move_to(time.time())
        accounts = view_models.library.accounts

        async def signing_in() -> str:
            probe = await accounts.probe(other.base)
            session = await accounts.sign_in_password(probe, EMAIL, PASSWORD)
            assert isinstance(session, Session)
            pid = (await accounts.complete(probe, session)).provider.id
            await accounts.sync(pid)
            return pid

        second = call(view_models, signing_in())
        # The sign-in dialog says so as it completes: the page shows that account.
        view_models.provider_sign_in.signedIn.emit(second)
        qtbot.waitUntil(lambda: prop(account, "multiple"), timeout=WAIT)
        qtbot.waitUntil(
            lambda: prop(account, "status") == "user@example.com · 另一个服务商", timeout=WAIT
        )
        assert prop(account, "providerId") == second
        assert [(o["label"], o["note"]) for o in prop(account, "providerOptions")] == [
            ("示例服务商", "user@example.com"),
            ("另一个服务商", "user@example.com"),
        ]
        select = find(page, lambda i: kind(i) == "AppSelect")
        assert select is not None
        assert select.property("current") == second
        qtbot.waitUntil(lambda: other.requests_for("wallet") != [], timeout=WAIT)

        account.selectProvider(first)
        assert prop(account, "status") == "user@example.com · 示例服务商"
        wallets = len(provider.requests_for("wallet"))
        qtbot.waitUntil(lambda: len(provider.requests_for("wallet")) > wallets, timeout=WAIT)

        # Signing out of one from the more menu leaves the page on the other.
        more = more_button(page)
        press(qtbot, window, more)
        pick(qtbot, window, "退出登录")
        qtbot.waitUntil(lambda: window.focused_text() == "取消", timeout=WAIT)
        window.key(qtbot, Qt.Key.Key_Tab)
        window.key(qtbot, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: not prop(account, "multiple"), timeout=WAIT)
        assert prop(account, "providerId") == second
        assert prop(shell, "currentPage") == "account"
        qtbot.waitUntil(lambda: window.focused() is more, timeout=WAIT)
        assert window.warnings == []
    finally:
        other.close()


# -- the window ------------------------------------------------------------------------------------
def tab_cycle(qtbot: QtBot, loaded: Loaded, start: QQuickItem) -> list[QQuickItem]:
    """The controls Tab visits from ``start`` until it comes back to it."""
    start.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    stops = [start]
    for _ in range(200):
        loaded.key(qtbot, Qt.Key.Key_Tab, wait=10)
        focused = loaded.focused()
        if focused is start:
            return stops
        assert focused not in stops, "Tab went round without coming back to the start"
        stops.append(focused)
    raise AssertionError("Tab never came back to the start")


@pytest.mark.usefixtures("screen_reader")
def test_the_smallest_window_reaches_every_control_in_order_with_its_name(
    qtbot: QtBot, window: Loaded
) -> None:
    root = window.window
    root.setWidth(root.minimumWidth())
    root.setHeight(root.minimumHeight())
    page = show(qtbot, window)
    qtbot.wait(100)

    # One column, nothing wider than the page.
    grid = find(page, lambda i: kind(i) == "QQuickGridLayout")
    assert grid is not None
    assert grid.property("columns") == 1
    right = page.mapRectToScene(QRectF(0, 0, page.width(), page.height())).right()
    for group in find_all(page, lambda i: kind(i) == "Group"):
        assert group.mapRectToScene(QRectF(0, 0, group.width(), group.height())).right() <= right

    tab = window.item(lambda i: kind(i) == "NavItem" and i.property("page") == "account")
    stops = tab_cycle(qtbot, window, tab)
    controls = find_all(page, lambda i: i.activeFocusOnTab() and i.isEnabled())
    assert [c for c in controls if c not in stops] == []
    for stop in stops:
        stop.forceActiveFocus(Qt.FocusReason.TabFocusReason)
        qtbot.wait(10)
        info = accessible(stop)
        assert info.name.strip(), f"{kind(stop)} has no accessible name"
        assert info.role not in (Role.NoRole, Role.Client, Role.Pane), kind(stop)
        assert window.focus_ring(stop) is not None, f"{info.name} shows no focus ring"
        assert window.on_screen(stop), f"{info.name} is scrolled out of view"

    # The header, then the left column top to bottom, then the right one.
    names = [accessible(stop).name for stop in stops]
    first = names.index("账户的更多操作")
    assert names[first : first + 13] == [
        "账户的更多操作",
        "自动续费",
        "续费" + BROWSER,
        "更换套餐" + BROWSER,
        "使用 专业版",
        "续费 基础版" + BROWSER,
        "充值" + BROWSER,
        "钱包明细" + BROWSER,
        "选购 专业版" + BROWSER,
        "去支付 专业版" + BROWSER,
        "密码与两步验证" + BROWSER,
        "登录设备" + BROWSER,
        "退出登录",
    ]
    assert window.warnings == []


@pytest.mark.usefixtures("screen_reader")
@pytest.mark.parametrize("window", ["en"], indirect=True)
def test_in_english_the_links_say_they_open_the_browser(qtbot: QtBot, window: Loaded) -> None:
    view_models = window.view_models
    qtbot.waitUntil(lambda: "account" in listed(view_models), timeout=WAIT)
    view_models.shell.setPage("account")
    account = view_models.account
    # The wallet can come before the sync that brings the subscriptions and their 续费.
    qtbot.waitUntil(lambda: ready(view_models), timeout=WAIT)
    page = window.item(lambda i: kind(i) == "AccountPage")
    renew = button(page, "Renew")
    assert accessible(renew).name == "Renew (opens in browser)"
    assert prop(account, "walletLevel") == "白银 · 5% off"
    assert prop(account, "currentDevices") == "2 / 3 online"
    tab = window.item(lambda i: kind(i) == "NavItem" and i.property("page") == "account")
    assert tab.property("text") == "Account"
    assert window.warnings == []
