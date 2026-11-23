"""Provider accounts' subscriptions on the subscriptions page and the overview (docs/accounts.md
§7.2, §7.5; DESIGN.md "服务商的订阅行"): their rows, statuses and menus, signing out, renewing
in the browser, the overview's notice and suggestion, when the accounts sync, and the texts of
the accounts' errors. A stand-in for api-edge on 127.0.0.1 answers; nothing reaches a real
provider or browser."""

from __future__ import annotations

import re
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QUrl
from pytestqt.qtbot import QtBot

from openweir.models.library import ProfileLibrary
from openweir.models.library.accounts import WATCH_EVERY_S
from openweir.models.library.profiles import ACCOUNT_PROFILE
from openweir.models.profiles import KIND_PROVIDER, Profile, ProfileError
from openweir.models.provider import AccountSummary, ProviderRecord, RemoteSubscription, Session
from openweir.models.provider.api import HttpProviderApi
from openweir.models.provider.types import (
    PROVIDER_CAPTCHA_REQUIRED,
    PROVIDER_CODES,
    PROVIDER_LINK_UNAVAILABLE,
    PROVIDER_SIGN_IN_UNSUPPORTED,
    PROVIDER_SUBSCRIPTION_DISABLED,
    PROVIDER_SUBSCRIPTION_EXPIRED,
    PROVIDER_SUBSCRIPTION_MISSING,
    PROVIDER_SUBSCRIPTION_UNUSABLE,
    PROVIDER_UNREACHABLE,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_EXPIRED,
    ProviderError,
    ProviderState,
)
from openweir.models.routing import MODE_PLAN
from openweir.models.store import ProfileStore
from openweir.viewmodels import profiles as profiles_module
from openweir.viewmodels.common.async_runner import AsyncRunner
from openweir.viewmodels.feedback import failure_text
from openweir.viewmodels.profiles import ProfilesViewModel, provider_status
from openweir.viewmodels.provider_text import ProviderStatus, listed
from tests.support.credentials import MemoryProviderTokens
from tests.support.i18n import ui_language
from tests.support.provider_server import (
    DAY,
    EMAIL,
    GIB,
    NOW,
    PASSWORD,
    FakeProvider,
    iso,
)
from tests.support.simulated import SAMPLE_CONFIG, SimulatedFetcher, SimulatedHost, seed_profiles

HOUR = 3600.0


# -- what a row says -------------------------------------------------------------------------------
def billing(remote_id: int = 7, **fields: Any) -> RemoteSubscription:
    values: dict[str, Any] = {
        "id": remote_id,
        "plan_id": "pro",
        "status": STATUS_ACTIVE,
        "usable": True,
        "plan_name": "专业版",
        "user_id": "user-1",
        "start_at": NOW - 30 * DAY,
        "expire_at": NOW + 30 * DAY,
        "period_start": NOW - 10 * DAY,
        "upload": 1 * GIB,
        "download": 9 * GIB,
        "total": 100 * GIB,
    }
    values.update(fields)
    return RemoteSubscription(**values)


def account(
    *subscriptions: RemoteSubscription,
    state: ProviderState = ProviderState.SIGNED_IN,
    notices: dict[int, str] | None = None,
) -> AccountSummary:
    record = ProviderRecord("p-1", "https://example.com", "示例服务商", EMAIL, "user-1")
    return AccountSummary(record, state, subscriptions, notices=notices or {})


def own(remote_id: int = 7) -> Profile:
    return Profile(f"profile-{remote_id}", "专业版", KIND_PROVIDER, provider_id="p-1", remote_id=7)


@pytest.mark.parametrize(
    ("subscription", "expected"),
    [
        (billing(), ProviderStatus()),
        (billing(expire_at=NOW + 2.5 * DAY), ProviderStatus("3 天后到期", renew=True)),
        (billing(expire_at=NOW + 5 * HOUR), ProviderStatus("5 小时后到期", renew=True)),
        (billing(expire_at=NOW + 60), ProviderStatus("1 小时后到期", renew=True)),
        (billing(download=int(91 * GIB)), ProviderStatus("流量已用 92%")),
        (billing(status=STATUS_EXPIRED, usable=False), ProviderStatus("已到期", renew=True)),
        (billing(usable=False, expire_at=NOW - DAY), ProviderStatus("已到期", renew=True)),
        (billing(usable=False, download=int(99 * GIB)), ProviderStatus("流量已用完")),
        (billing(usable=False, start_at=NOW + DAY), ProviderStatus("未开始")),
        (billing(status=STATUS_DISABLED), ProviderStatus("已停用", danger=True)),
        (billing(usable=False), ProviderStatus("暂不可用")),
    ],
    ids=[
        "active",
        "expiring",
        "last day",
        "last hour",
        "traffic low",
        "expired",
        "past expiry",
        "used up",
        "not started",
        "disabled",
        "unusable",
    ],
)
def test_billing_says_one_status(
    qapp: QCoreApplication, subscription: RemoteSubscription, expected: ProviderStatus
) -> None:
    assert provider_status(own(), account(subscription), NOW) == expected


@pytest.mark.parametrize(
    ("state", "notices", "expected"),
    [
        (ProviderState.SIGN_IN_REQUIRED, {}, ProviderStatus("需要重新登录")),
        (ProviderState.ACCOUNT_DISABLED, {}, ProviderStatus("账户已停用", danger=True)),
        # The account's state comes before what its subscriptions say.
        (
            ProviderState.SIGN_IN_REQUIRED,
            {7: PROVIDER_SUBSCRIPTION_EXPIRED},
            ProviderStatus("需要重新登录"),
        ),
        (
            ProviderState.SIGNED_IN,
            {7: PROVIDER_SUBSCRIPTION_MISSING},
            ProviderStatus("已不在账户中"),
        ),
        (ProviderState.SIGNED_IN, {7: PROVIDER_LINK_UNAVAILABLE}, ProviderStatus("没有订阅链接")),
        # Billing changed since the update that kept the config: the code says why.
        (
            ProviderState.SIGNED_IN,
            {7: PROVIDER_SUBSCRIPTION_EXPIRED},
            ProviderStatus("已到期", renew=True),
        ),
        (
            ProviderState.SIGNED_IN,
            {7: PROVIDER_SUBSCRIPTION_DISABLED},
            ProviderStatus("已停用", danger=True),
        ),
        (ProviderState.SIGNED_IN, {7: PROVIDER_SUBSCRIPTION_UNUSABLE}, ProviderStatus("暂不可用")),
        # A locked keychain is unknown for now: billing as last synced speaks.
        (ProviderState.CREDENTIALS_UNAVAILABLE, {}, ProviderStatus()),
    ],
    ids=[
        "sign in",
        "account disabled",
        "account first",
        "missing",
        "no link",
        "held expired",
        "held disabled",
        "held unusable",
        "keychain locked",
    ],
)
def test_the_account_comes_before_its_subscriptions(
    qapp: QCoreApplication,
    state: ProviderState,
    notices: dict[int, str],
    expected: ProviderStatus,
) -> None:
    assert provider_status(own(), account(billing(), state=state, notices=notices), NOW) == expected


def test_one_wording_for_every_list(qapp: QCoreApplication) -> None:
    """The sign-in dialog's picker and the account page write a status as the subscriptions
    page does, in one translation context: 有效 for none, after 试用 for a trial, and its
    colour by what it says."""
    assert (listed(ProviderStatus(), trial=False), ProviderStatus().tone) == ("有效", "")
    expiring = provider_status(own(), account(billing(expire_at=NOW + 2.5 * DAY)), NOW)
    assert (listed(expiring, trial=True), expiring.tone) == ("试用 · 3 天后到期", "warning")
    stopped = provider_status(own(), account(billing(status=STATUS_DISABLED)), NOW)
    assert (listed(stopped, trial=False), stopped.tone) == ("已停用", "danger")
    with ui_language("en"):
        assert listed(ProviderStatus(), trial=True) == "Trial · Active"
        signed_out = account(billing(), state=ProviderState.SIGN_IN_REQUIRED)
        assert provider_status(own(), signed_out, NOW).text == "Sign-in required"


# -- the subscriptions page ------------------------------------------------------------------------
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

    monkeypatch.setattr(profiles_module.QDesktopServices, "openUrl", open_url)
    return pages


class Scene:
    def __init__(self, qtbot: QtBot, directory: Path, provider: FakeProvider) -> None:
        self.qtbot = qtbot
        self.now = NOW
        self.provider = provider
        store = ProfileStore(directory)
        seed_profiles(store, NOW)
        self.tokens = MemoryProviderTokens()
        self.library = ProfileLibrary(
            store,
            SimulatedHost(clock=lambda: self.now),
            SimulatedFetcher(),
            clock=lambda: self.now,
            provider_api=HttpProviderApi(language=lambda: "zh", clock=lambda: self.now),
            provider_tokens=self.tokens,
        )
        self.runner = AsyncRunner()
        self.runner.start()
        # The timers stay off: each test says when the page checks and syncs.
        self.profiles = ProfilesViewModel(self.library, self.runner, lambda: self.now)
        self.provider_id = ""

    def call[T](self, action: Coroutine[Any, Any, T]) -> T:
        """Run a library call to its end, as a dialog would before the page reads again."""
        return self.runner.submit(action).result(timeout=20)

    def sign_in(self, *, sync: bool = True) -> str:
        """Sign in as the dialog does; the sync after it downloads the configs."""
        accounts = self.library.accounts

        async def signing_in() -> str:
            probe = await accounts.probe(self.provider.base)
            session = await accounts.sign_in_password(probe, EMAIL, PASSWORD)
            assert isinstance(session, Session)
            return (await accounts.complete(probe, session)).provider.id

        self.provider_id = self.call(signing_in())
        if sync:
            self.sync()
        else:
            self.reload()
        return self.provider_id

    def sync(self) -> None:
        """Sync the account, then let the page read it all again."""
        self.call(self.library.accounts.sync(self.provider_id))
        self.reload()

    def reload(self) -> None:
        """Read the list, the routing and the accounts again, as after a dialog."""
        before = self.profiles._accounts
        self.profiles.reload()
        self.qtbot.waitUntil(lambda: self.profiles._accounts is not before)

    def rows(self) -> list[Any]:
        return list(self.profiles.property("model").rows)

    def row(self, name: str) -> Any:
        return next(r for r in self.rows() if r.name == name)

    def wait_row(self, name: str, **expected: Any) -> Any:
        def matches() -> bool:
            row = next((r for r in self.rows() if r.name == name), None)
            return row is not None and all(getattr(row, k) == v for k, v in expected.items())

        self.qtbot.waitUntil(matches, timeout=10000)
        return self.row(name)

    def profile(self, remote_id: int) -> Profile:
        return next(p for p in self.library.snapshot().profiles if p.remote_id == remote_id)

    def close(self) -> None:
        self.runner.shutdown()


@pytest.fixture
def scene(qtbot: QtBot, tmp_path: Path, provider: FakeProvider) -> Iterator[Scene]:
    built = Scene(qtbot, tmp_path, provider)
    yield built
    built.close()


def test_a_provider_row_names_the_site_and_the_account(scene: Scene) -> None:
    scene.sign_in()

    pro = scene.wait_row("专业版", source="示例服务商 · user@example.com · 9 个节点")
    assert pro.isProvider
    # Billing's usage and expiry; the config downloaded as the sync found it new.
    assert (pro.usage, pro.hasQuota) == ("10.0 / 100 GB", True)
    assert pro.expiry == "2027-02-14"
    assert pro.updated == "刚刚"
    assert (pro.status, pro.statusDanger) == ("", False)
    assert (pro.canRemove, pro.canRenew, pro.needsSignIn) == (False, True, False)
    assert (pro.providerName, pro.refreshLabel) == ("示例服务商", "更新")
    assert pro.canAutoUpdate
    # The expired one keeps waiting for a config it cannot have: no update time, no failure.
    basic = scene.row("基础版")
    assert (basic.status, basic.updated, basic.updateFailed) == ("已到期", "—", False)
    assert basic.source == "示例服务商 · user@example.com"
    # The others are as they were.
    sample = scene.row("示例订阅 A")
    assert not sample.isProvider
    assert sample.canRemove
    assert (sample.status, sample.canRenew, sample.needsSignIn) == ("", False, False)


def test_a_trial_says_so_before_the_source(scene: Scene) -> None:
    scene.provider.update_subscription(7, trial=True)
    scene.sign_in()
    assert scene.wait_row("专业版").source.startswith("试用 · 示例服务商 · user@example.com")


def test_a_status_takes_the_place_of_a_failed_update(scene: Scene) -> None:
    scene.sign_in()
    scene.wait_row("专业版", status="")
    scene.provider.update_subscription(
        7, status="expired", usable=False, expireAt=iso(NOW - DAY), updatedAt=iso(NOW)
    )
    with scene.qtbot.waitSignal(scene.profiles.changed):
        scene.profiles.updateProfile(scene.profile(7).id)

    pro = scene.wait_row("专业版", status="已到期", updating=False)
    assert not pro.updateFailed
    assert pro.updated == "刚刚"  # the last config stays
    # Keeping it is meant to happen: the header shows no failure.
    assert not scene.profiles.property("hasError")
    assert scene.profiles.property("summary") == "5 份订阅"


def test_signing_in_again_is_offered_once_the_session_ends(scene: Scene) -> None:
    pid = scene.sign_in()
    scene.provider.revoke()
    with pytest.raises(ProviderError):
        scene.call(scene.library.accounts.sync(pid))
    scene.reload()

    pro = scene.wait_row("专业版", status="需要重新登录")
    assert (pro.statusDanger, pro.needsSignIn, pro.canRemove) == (False, True, False)
    with scene.qtbot.waitSignal(scene.profiles.providerSignInRequested) as asked:
        scene.profiles.signInAgain(pro.profileId)
    assert asked.args == [scene.provider.base]


def test_a_disabled_account_is_in_the_danger_colour(scene: Scene) -> None:
    pid = scene.sign_in()
    scene.provider.fail("subscriptions", 403, "FORBIDDEN")
    with pytest.raises(ProviderError):
        scene.call(scene.library.accounts.sync(pid))
    scene.reload()

    pro = scene.wait_row("专业版", status="账户已停用")
    assert pro.statusDanger
    assert pro.needsSignIn


def test_only_a_subscription_the_account_lost_can_be_deleted(scene: Scene) -> None:
    scene.sign_in()
    seven = scene.profile(7).id
    refused: list[str] = []
    scene.profiles.removalRequested.connect(lambda: refused.append("asked"))
    scene.profiles.requestRemove(seven)
    scene.qtbot.waitUntil(lambda: refused == ["asked"])
    scene.profiles.confirmRemove()
    scene.qtbot.waitUntil(lambda: scene.profiles.property("hasError"))
    assert scene.profiles.property("summary") == failure_text(ProfileError(ACCOUNT_PROFILE, ""))
    assert scene.profile(7).id == seven

    del scene.provider.subscriptions[9]
    scene.sync()
    basic = scene.wait_row("基础版", status="已不在账户中")
    assert (basic.canRemove, basic.canRenew) == (True, False)
    with scene.qtbot.waitSignal(scene.profiles.removalRequested):
        scene.profiles.requestRemove(basic.profileId)
    assert scene.profiles.property("removalTitle") == "删除「基础版」？"  # noqa: RUF001
    scene.profiles.confirmRemove()
    scene.qtbot.waitUntil(lambda: "基础版" not in [r.name for r in scene.rows()])


def test_renewing_opens_the_subscriptions_page_and_watches_the_account(
    scene: Scene, opened: list[str]
) -> None:
    scene.sign_in()
    pro = scene.wait_row("专业版", canRenew=True)

    scene.profiles.renew(pro.profileId)
    assert opened == [f"{scene.provider.base}/subscription?id=7"]

    # Back from the browser a minute later, the window's activation syncs the account; a
    # minute is all the watch waits between syncs (D58).
    synced = len(scene.provider.requests_for("subscriptions"))
    scene.now += WATCH_EVERY_S
    scene.provider.update_subscription(7, expireAt=iso(NOW + 60 * DAY), updatedAt=iso(NOW + 60))
    scene.profiles.windowActivated()
    scene.qtbot.waitUntil(lambda: len(scene.provider.requests_for("subscriptions")) > synced)
    scene.wait_row("专业版", expiry="2027-03-16")


def test_the_page_and_a_language_switch_sync_the_accounts(scene: Scene) -> None:
    scene.sign_in()
    synced = len(scene.provider.requests_for("subscriptions"))
    # Shown within a minute of the last sync, the page does not ask again.
    scene.now += 30
    scene.profiles.pageShown()
    scene.qtbot.wait(300)
    assert len(scene.provider.requests_for("subscriptions")) == synced
    scene.now += 31
    scene.profiles.pageShown()
    scene.qtbot.waitUntil(lambda: len(scene.provider.requests_for("subscriptions")) == synced + 1)
    scene.qtbot.waitUntil(lambda: not scene.profiles._syncing)
    # The plans' names come in the language asked for, whenever it changes.
    scene.profiles.retranslate()
    scene.qtbot.waitUntil(lambda: len(scene.provider.requests_for("subscriptions")) == synced + 2)


def test_signing_out_names_what_goes_and_who_takes_over(scene: Scene) -> None:
    pid = scene.sign_in()
    scene.call(scene.library.accounts.use(pid, 7))
    scene.reload()
    pro = scene.wait_row("专业版", active=True)

    with scene.qtbot.waitSignal(scene.profiles.signOutRequested):
        scene.profiles.requestSignOut(pro.profileId)
    assert scene.profiles.property("signOutTitle") == "退出「示例服务商」？"  # noqa: RUF001
    assert scene.profiles.property("signOutText") == (
        "「专业版」、「基础版」会从本机删除。"
        "之后改用「示例订阅 A」的规则，服务商那边的订阅不受影响。"  # noqa: RUF001
    )
    assert scene.profiles.property("signOutProfiles") == [
        scene.profile(7).id,
        scene.profile(9).id,
    ]

    # Cancelled, nothing goes; confirmed, the account's subscriptions go.
    scene.profiles.cancelSignOut()
    scene.profiles.confirmSignOut()
    scene.qtbot.wait(200)
    assert len(scene.rows()) == 5
    with scene.qtbot.waitSignal(scene.profiles.signOutRequested):
        scene.profiles.requestSignOut(pro.profileId)
    with scene.qtbot.waitSignal(scene.profiles.applied, timeout=10000):
        scene.profiles.confirmSignOut()
    assert [r.name for r in scene.rows()] == ["示例订阅 A", "示例订阅 B", "本地配置"]
    assert scene.profiles.property("activeName") == "示例订阅 A"
    assert run_summaries(scene) == ()
    assert scene.provider.requests_for("sign_out")


def run_summaries(scene: Scene) -> tuple[AccountSummary, ...]:
    return scene.call(scene.library.accounts.summaries())


def test_many_subscriptions_are_counted_rather_than_named(scene: Scene) -> None:
    for remote_id in (21, 22, 23):
        scene.provider.add_subscription("user-1", remote_id, planName=f"套餐 {remote_id}")
    scene.sign_in()
    pro = scene.wait_row("专业版")
    with scene.qtbot.waitSignal(scene.profiles.signOutRequested):
        scene.profiles.requestSignOut(pro.profileId)
    assert scene.profiles.property("signOutText") == (
        "「专业版」、「基础版」、「套餐 21」等 5 份订阅会从本机删除。服务商那边的订阅不受影响。"
    )


def test_the_header_asks_for_the_sign_in_dialog(scene: Scene) -> None:
    with scene.qtbot.waitSignal(scene.profiles.providerSignInRequested) as asked:
        scene.profiles.requestProviderSignIn("")
    assert asked.args == [""]


def test_rules_of_a_subscription_without_a_config_download_it_first(scene: Scene) -> None:
    # Brought in by signing in, not downloaded yet: the dialog's use or the next sync would.
    scene.sign_in(sync=False)
    pro = scene.wait_row("专业版", updated="—")
    with scene.qtbot.waitSignal(scene.profiles.routingRequested):
        scene.profiles.useRules(pro.profileId)
    scene.wait_row("专业版", active=True, updated="刚刚")
    assert scene.provider.requests_for("config")


# -- the overview ----------------------------------------------------------------------------------
def test_the_overview_tells_how_the_subscription_in_use_runs_out(
    scene: Scene, opened: list[str]
) -> None:
    pid = scene.sign_in()
    profiles = scene.profiles
    assert profiles.property("activeNotice") == ""  # 示例订阅 A is no provider's
    scene.call(scene.library.accounts.use(pid, 7))
    scene.reload()
    scene.wait_row("专业版", active=True)
    assert profiles.property("activeNotice") == ""

    scene.provider.update_subscription(7, expireAt=iso(NOW + 2 * DAY))
    scene.sync()
    scene.qtbot.waitUntil(lambda: profiles.property("activeNotice") == "2 天后到期")
    assert profiles.property("canRenewActive")
    assert not profiles.property("activeNoticeDanger")
    assert profiles.property("suggestedName") == ""  # it still works
    # 续费 goes to the account page, on the subscription's provider; the browser waits.
    with scene.qtbot.waitSignal(profiles.accountRequested) as asked:
        profiles.renewActive()
    assert asked.args == [pid]
    assert opened == []

    scene.provider.update_subscription(7, expireAt=iso(NOW + 30 * DAY), download=int(91 * GIB))
    scene.sync()
    scene.qtbot.waitUntil(lambda: profiles.property("activeNotice") == "流量已用 92%")
    assert not profiles.property("canRenewActive")

    # Under my plan no one subscription is in use.
    scene.call(scene.library.plans.set_mode(MODE_PLAN))
    scene.reload()
    scene.qtbot.waitUntil(lambda: profiles.property("activeNotice") == "")


def test_the_overview_suggests_another_subscription_of_the_account(scene: Scene) -> None:
    pid = scene.sign_in()
    profiles = scene.profiles
    scene.call(scene.library.accounts.use(pid, 7))
    scene.provider.add_subscription("user-1", 12, planName="家庭版")
    scene.provider.update_subscription(
        7, status="expired", usable=False, expireAt=iso(NOW - DAY), updatedAt=iso(NOW)
    )
    scene.sync()

    scene.qtbot.waitUntil(lambda: profiles.property("activeNotice") == "已到期")
    assert profiles.property("canRenewActive")
    # The expired 基础版 cannot be used either: the one that can is suggested, and nothing
    # switches until the user says so.
    assert profiles.property("suggestedName") == "家庭版"
    assert profiles.property("activeName") == "专业版"
    with scene.qtbot.waitSignal(profiles.applied, timeout=10000):
        profiles.useSuggested()
    assert profiles.property("activeName") == "家庭版"
    scene.qtbot.waitUntil(lambda: profiles.property("activeNotice") == "")
    assert profiles.property("suggestedName") == ""


def test_nothing_is_suggested_while_the_account_needs_signing_in(scene: Scene) -> None:
    pid = scene.sign_in()
    profiles = scene.profiles
    scene.call(scene.library.accounts.use(pid, 7))
    scene.provider.add_subscription("user-1", 12, planName="家庭版")
    scene.provider.update_subscription(7, status="disabled", updatedAt=iso(NOW))
    scene.sync()
    scene.qtbot.waitUntil(lambda: profiles.property("activeNotice") == "已停用")
    assert profiles.property("activeNoticeDanger")
    assert profiles.property("suggestedName") == "家庭版"

    scene.provider.revoke()
    with pytest.raises(ProviderError):
        scene.call(scene.library.accounts.sync(pid))
    scene.reload()
    scene.qtbot.waitUntil(lambda: profiles.property("activeNotice") == "需要重新登录")
    assert profiles.property("suggestedName") == ""


# -- when the page checks --------------------------------------------------------------------------
def test_the_first_check_waits_after_start(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProfileStore(tmp_path)
    seed_profiles(store, NOW)
    library = ProfileLibrary(store, SimulatedHost(), SimulatedFetcher(), clock=lambda: NOW)
    runner = AsyncRunner()
    runner.start()
    checks: list[float] = []
    update_due = library.profiles.update_due

    async def counted() -> Any:
        checks.append(NOW)
        return await update_due()

    monkeypatch.setattr(library.profiles, "update_due", counted)
    profiles = ProfilesViewModel(library, runner, lambda: NOW, first_check_ms=200)
    try:
        profiles.start()
        qtbot.wait(50)
        assert checks == []
        qtbot.waitUntil(lambda: checks == [NOW])
        qtbot.wait(300)
        assert checks == [NOW]  # then every minute
    finally:
        profiles.stop()
        runner.shutdown()


# -- texts -----------------------------------------------------------------------------------------
CODES = sorted(PROVIDER_CODES | {ACCOUNT_PROFILE, "credentials_unavailable"})
CJK = re.compile(r"[一-鿿]")


@pytest.mark.parametrize("code", CODES)
def test_every_account_error_has_a_text(qapp: QCoreApplication, code: str) -> None:
    text = failure_text(ProviderError(code, "HTTP 503"))
    assert not text.startswith("操作失败"), code
    assert CJK.search(text), code
    with ui_language("en"):
        english = failure_text(ProviderError(code, "HTTP 503"))
    assert not CJK.search(english), (code, english)
    assert not english.startswith("Operation failed"), code


def test_account_errors_say_what_helps(qapp: QCoreApplication) -> None:
    # Why it could not connect helps; a URL never is in the message (ProviderError).
    assert failure_text(ProviderError(PROVIDER_UNREACHABLE, "timed out")) == (
        "无法连接服务商：timed out"  # noqa: RUF001
    )
    # Signing in only on the web: the link in the user center is the way in.
    for code in (PROVIDER_CAPTCHA_REQUIRED, PROVIDER_SIGN_IN_UNSUPPORTED):
        assert "「从链接导入」" in failure_text(ProviderError(code, ""))
        with ui_language("en"):
            assert "“Import from URL”" in failure_text(ProviderError(code, ""))
