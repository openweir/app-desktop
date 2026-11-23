"""Signing in to a provider (viewmodels/provider_sign_in.py, docs/accounts.md §7.3) against a
stand-in for api-edge on 127.0.0.1: each step of the dialog, two-factor, email codes, providers
that sign in on the web only, replacing another account, the picker's rules for zero, one and
several subscriptions, errors under their fields, and cancelling while a request runs. The
dialog itself is tested in test_provider_sign_in_dialog.py."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QUrl
from pytestqt.qtbot import QtBot

from openweir.models.library import ProfileLibrary
from openweir.models.provider import types
from openweir.models.provider.api import HttpProviderApi
from openweir.models.provider.types import ProviderState
from openweir.models.store import ProfileStore
from openweir.viewmodels import provider_sign_in
from openweir.viewmodels.common.async_runner import AsyncRunner
from openweir.viewmodels.feedback import failure_text
from openweir.viewmodels.provider_sign_in import ProviderSignInViewModel
from tests.support.credentials import MemoryProviderTokens
from tests.support.i18n import ui_language
from tests.support.provider_server import (
    DAY,
    EMAIL,
    GIB,
    NOW,
    PASSWORD,
    TOTP,
    FakeProvider,
    iso,
)
from tests.support.simulated import SAMPLE_CONFIG, SimulatedFetcher, SimulatedHost, seed_profiles

SIGNALS = (
    "openRequested",
    "closeRequested",
    "linkImportRequested",
    "profilesChanged",
    "applied",
)


def text(code: str) -> str:
    """What the dialog writes for an error with ``code``: see ``codes``."""
    return f"[{code}]"


@pytest.fixture(autouse=True)
def codes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dialog writes an error's code in place of its text, so each assertion names the
    code: the texts themselves are viewmodels/feedback.py's (failure_text)."""
    seen = provider_sign_in.failure_text
    assert seen is failure_text

    def code_of(error: BaseException) -> str:
        return text(getattr(error, "code", type(error).__name__))

    monkeypatch.setattr(provider_sign_in, "failure_text", code_of)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    # Reach the loopback server directly even if the machine has a proxy configured.
    monkeypatch.setenv("no_proxy", "*")
    fake = FakeProvider()
    fake.config = SAMPLE_CONFIG.encode()  # rules of its own, so it can be used
    yield fake
    fake.close()


class Hold:
    """Holds a request until released, to cancel the dialog while it runs."""

    def __init__(self) -> None:
        self.entered = threading.Event()  # the request is held
        self.release = threading.Event()
        self.finished = threading.Event()  # the request answered, after the release

    def around[T](
        self, call: Callable[..., Coroutine[Any, Any, T]], *, times: int = 1
    ) -> Callable[..., Coroutine[Any, Any, T]]:
        left = [times]

        async def held(*args: Any, **kwargs: Any) -> T:
            if left[0] <= 0:
                return await call(*args, **kwargs)
            left[0] -= 1
            self.entered.set()
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            try:
                return await call(*args, **kwargs)
            finally:
                self.finished.set()

        return held


class Flow:
    """The view model over a library that reaches the fake provider, on the app's runner."""

    def __init__(
        self, qtbot: QtBot, directory: Path, provider: FakeProvider, *, seeded: bool
    ) -> None:
        self.qtbot = qtbot
        self.now = NOW
        self.provider = provider
        self.store = ProfileStore(directory)
        if seeded:
            seed_profiles(self.store, NOW)
        self.host = SimulatedHost(clock=lambda: self.now)
        self.tokens = MemoryProviderTokens()
        self.api = HttpProviderApi(language=lambda: "zh", clock=lambda: self.now)
        self.library = ProfileLibrary(
            self.store,
            self.host,
            SimulatedFetcher(),
            clock=lambda: self.now,
            provider_api=self.api,
            provider_tokens=self.tokens,
        )
        self.runner = AsyncRunner()
        self.runner.start()
        self.vm = ProviderSignInViewModel(self.library, self.runner, lambda: self.now)
        self.heard: dict[str, int] = dict.fromkeys(SIGNALS, 0)
        for name in SIGNALS:
            getattr(self.vm, name).connect(lambda *_, name=name: self._heard(name))
        self.signed_in: list[str] = []
        self.vm.signedIn.connect(self.signed_in.append)

    def _heard(self, name: str) -> None:
        self.heard[name] += 1

    def close(self) -> None:
        self.vm.stop()
        self.runner.shutdown(finalize=self.host.close)

    def get(self, name: str) -> Any:
        return self.vm.property(name)

    def settle(self) -> None:
        self.qtbot.waitUntil(lambda: not self.get("busy"), timeout=10_000)

    def drain(self) -> None:
        """Wait until every answer the runner had on its way reached the Qt thread: one
        submitted later answers after them."""
        done: list[bool] = []

        async def nothing() -> None:
            return None

        self.runner.submit(nothing(), lambda _: done.append(True))
        self.qtbot.waitUntil(lambda: bool(done), timeout=10_000)

    def run[T](self, action: Coroutine[Any, Any, T]) -> T:
        """A library call on the runner's loop, as the app makes it."""
        results: list[T] = []
        errors: list[BaseException] = []
        self.runner.submit(action, results.append, errors.append)
        self.qtbot.waitUntil(lambda: bool(results or errors), timeout=10_000)
        if errors:
            raise errors[0]
        return results[0]

    def requests(self, route: str) -> int:
        return len(self.provider.requests_for(route))

    def start(self, address: str = "") -> None:
        self.vm.open(address)

    def to_sign_in(self) -> None:
        self.start()
        self.vm.submitAddress(self.provider.base)
        self.settle()
        assert self.get("step") == "sign_in", self.get("error") or self.get("addressError")

    def sign_in(self, identifier: str = EMAIL, password: str = PASSWORD) -> None:
        self.to_sign_in()
        self.vm.signInPassword(identifier, password)
        self.settle()

    def rows(self) -> list[dict[str, Any]]:
        return self.get("subscriptions")

    def provider_profiles(self) -> list[Any]:
        return [p for p in self.library.snapshot().profiles if p.is_provider]


@pytest.fixture
def flow(qtbot: QtBot, tmp_path: Path, provider: FakeProvider) -> Iterator[Flow]:
    """A library with profiles already, one of them in use."""
    made = Flow(qtbot, tmp_path, provider, seeded=True)
    yield made
    made.close()


@pytest.fixture
def fresh(qtbot: QtBot, tmp_path: Path, provider: FakeProvider) -> Iterator[Flow]:
    """A library without any profile."""
    made = Flow(qtbot, tmp_path, provider, seeded=False)
    yield made
    made.close()


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the dialog opens in the browser; nothing is opened."""
    opened: list[str] = []

    def open_url(url: QUrl) -> bool:
        opened.append(url.toString())
        return True

    monkeypatch.setattr(provider_sign_in.QDesktopServices, "openUrl", open_url)
    return opened


# -- 1 the address ----------------------------------------------------------------------------
def test_opening_fills_in_the_address_and_signs_in_to_nothing(flow: Flow) -> None:
    flow.start(f"  {flow.provider.base}  ")
    assert flow.get("active")
    assert flow.get("step") == "address"
    assert flow.get("address") == flow.provider.base
    assert flow.get("title") == "登录服务商"
    assert flow.heard["openRequested"] == 1
    flow.drain()
    assert flow.provider.requests == []

    # Opened again while it shows: the first step takes the new address.
    flow.start("https://other.example.com")
    assert flow.get("address") == "https://other.example.com"
    assert flow.heard["openRequested"] == 2


def test_the_address_is_made_an_https_origin(flow: Flow) -> None:
    flow.start()
    flow.vm.submitAddress(f" {flow.provider.base}/login?next=/ ")
    flow.settle()
    assert flow.get("step") == "sign_in"
    assert flow.get("address") == flow.provider.base
    assert flow.get("title") == "登录「示例服务商」"
    assert flow.get("host") == flow.provider.base.removeprefix("http://")
    assert flow.requests("site") == 1
    assert flow.requests("sign_in_email") == 0  # probing signs in to nothing

    # Opened again on a later step: ignored.
    flow.start("https://other.example.com")
    assert flow.get("step") == "sign_in"
    assert flow.heard["openRequested"] == 1


@pytest.mark.parametrize(
    "address", ["http://example.com", "ftp://example.com", "a b", "user@example.com:x"]
)
def test_an_address_that_is_no_https_origin_is_refused_under_the_field(
    flow: Flow, address: str
) -> None:
    flow.start()
    flow.vm.submitAddress(address)
    assert flow.get("addressError") == text(types.PROVIDER_INVALID_ADDRESS)
    assert flow.get("error") == ""
    assert not flow.get("busy")
    assert flow.get("step") == "address"


def test_an_address_that_is_no_openweir_provider_says_so_under_the_field(flow: Flow) -> None:
    flow.provider.answer("site", 200, {"hello": "world"})
    flow.start()
    flow.vm.submitAddress(flow.provider.base)
    flow.settle()
    assert flow.get("step") == "address"
    assert flow.get("addressError") == text(types.PROVIDER_NOT_OPENWEIR)
    assert flow.get("error") == ""


def test_a_provider_that_cannot_be_reached_says_so_above_the_buttons(flow: Flow) -> None:
    flow.provider.fail("site", 503)
    flow.start()
    flow.vm.submitAddress(flow.provider.base)
    flow.settle()
    assert flow.get("step") == "address"
    assert flow.get("addressError") == ""
    assert flow.get("error") == text(types.PROVIDER_UNREACHABLE)


# -- 2 signing in -----------------------------------------------------------------------------
def test_both_methods_offer_a_choice_and_keep_their_errors_apart(flow: Flow) -> None:
    flow.to_sign_in()
    assert [o["value"] for o in flow.get("methodOptions")] == ["password", "email_code"]
    assert [o["label"] for o in flow.get("methodOptions")] == ["密码", "邮箱验证码"]
    assert flow.get("method") == "password"
    assert flow.get("identifierLabel") == "邮箱或用户名"
    assert flow.get("unavailable") == ""

    flow.vm.signInPassword(EMAIL, "wrong password")
    flow.settle()
    assert flow.get("passwordError") == text(types.PROVIDER_SIGN_IN_FAILED)
    assert (flow.get("identifierError"), flow.get("error")) == ("", "")
    assert flow.get("step") == "sign_in"

    flow.vm.setMethod("email_code")
    assert flow.get("method") == "email_code"
    assert flow.get("passwordError") == ""  # a new method starts without errors


def test_one_method_offers_no_choice(flow: Flow) -> None:
    flow.provider.methods = [{"id": "email", "type": "email_password", "name": "邮箱"}]
    flow.to_sign_in()
    assert flow.get("methodOptions") == []
    assert flow.get("identifierLabel") == "邮箱"
    flow.vm.setMethod("email_code")
    assert flow.get("method") == "password"


def test_an_unverified_email_is_said_under_the_account(flow: Flow) -> None:
    flow.provider.require_email_verification = True
    flow.provider.users["user-1"].email_verified = False
    flow.sign_in()
    assert flow.get("identifierError") == text(types.PROVIDER_EMAIL_UNVERIFIED)
    assert flow.get("passwordError") == ""


def test_signing_in_with_an_email_code(flow: Flow) -> None:
    flow.to_sign_in()
    flow.vm.setMethod("email_code")
    assert flow.get("sendLabel") == "发送验证码"
    flow.vm.signInEmailCode(EMAIL, "123456")  # no code was sent yet
    assert not flow.get("busy")

    flow.vm.sendCode("not an email")
    assert flow.get("sending")
    assert flow.get("busy")
    assert flow.get("sendLabel") == "正在发送…"
    flow.settle()
    assert flow.get("emailError") == text(types.PROVIDER_SIGN_IN_FAILED)
    assert not flow.get("codeSent")

    flow.vm.sendCode(EMAIL)
    flow.settle()
    assert flow.get("codeSent")
    assert flow.get("resendSeconds") == 60
    assert flow.get("sendLabel") == "重新发送（60）"  # noqa: RUF001
    flow.vm.sendCode(EMAIL)  # waits out the minute
    assert not flow.get("busy")
    assert flow.requests("send_code") == 2
    flow.now += 18.5
    flow.vm._tick()
    assert flow.get("resendSeconds") == 42
    assert flow.get("sendLabel") == "重新发送（42）"  # noqa: RUF001

    flow.vm.signInEmailCode(EMAIL, "wrong")
    flow.settle()
    assert flow.get("codeError") == text(types.PROVIDER_CODE_INVALID)
    assert flow.get("emailError") == ""

    flow.now += 60
    flow.vm._tick()
    assert flow.get("resendSeconds") == 0
    assert flow.get("sendLabel") == "重新发送"
    flow.vm.sendCode(EMAIL)
    flow.settle()
    assert flow.requests("send_code") == 3
    flow.vm.signInEmailCode(EMAIL, flow.provider.codes[EMAIL])
    flow.settle()
    assert flow.get("step") == "pick"
    assert flow.signed_in


@pytest.mark.parametrize(
    ("scenes", "methods", "code"),
    [
        (["sign_in", "send_code"], None, types.PROVIDER_CAPTCHA_REQUIRED),
        (
            [],
            [{"id": "gh", "type": "github", "name": "GitHub"}],
            types.PROVIDER_SIGN_IN_UNSUPPORTED,
        ),
    ],
)
def test_a_provider_that_signs_in_on_the_web_only_points_to_link_import(
    flow: Flow, scenes: list[str], methods: list[dict[str, str]] | None, code: str
) -> None:
    flow.provider.scenes = scenes
    if methods is not None:
        flow.provider.methods = methods
    flow.to_sign_in()
    assert flow.get("unavailable") == text(code)
    assert flow.get("methodOptions") == []
    flow.vm.signInPassword(EMAIL, PASSWORD)
    assert not flow.get("busy")

    flow.vm.importFromLink()
    assert flow.heard["linkImportRequested"] == 1
    assert flow.heard["closeRequested"] == 1
    assert not flow.get("active")
    assert flow.requests("sign_in_email") == 0


def test_a_captcha_that_shows_only_on_signing_in_points_to_link_import(flow: Flow) -> None:
    flow.provider.fail("sign_in_email", 400, "MISSING_RESPONSE")
    flow.sign_in()
    assert flow.get("step") == "sign_in"
    assert flow.get("unavailable") == text(types.PROVIDER_CAPTCHA_REQUIRED)
    assert flow.get("passwordError") == flow.get("error") == ""


def test_a_failure_no_field_caused_shows_above_the_buttons(flow: Flow) -> None:
    flow.provider.fail("sign_in_email", 502)
    flow.sign_in()
    assert flow.get("error") == text(types.PROVIDER_UNREACHABLE)
    assert flow.get("passwordError") == flow.get("identifierError") == ""


# -- 3 two-factor -----------------------------------------------------------------------------
def test_two_factor(flow: Flow) -> None:
    flow.provider.users["user-1"].totp = TOTP
    flow.sign_in()
    assert flow.get("step") == "two_factor"
    assert flow.get("title") == "两步验证"
    assert flow.get("message")
    assert flow.signed_in == []

    flow.vm.verify("000000")
    flow.settle()
    assert flow.get("twoFactorError") == text(types.PROVIDER_TWO_FACTOR_INVALID)
    assert flow.get("step") == "two_factor"

    flow.vm.back()
    assert flow.get("step") == "sign_in"
    assert flow.get("twoFactorError") == ""
    flow.vm.signInPassword(EMAIL, PASSWORD)
    flow.settle()
    flow.vm.verify(TOTP)
    flow.settle()
    assert flow.get("step") == "pick"
    assert len(flow.signed_in) == 1


def test_a_two_factor_step_that_timed_out_goes_back_to_signing_in(flow: Flow) -> None:
    flow.provider.users["user-1"].totp = TOTP
    flow.sign_in()
    flow.provider.fail("verify_totp", 401, "INVALID_TWO_FACTOR_COOKIE")
    flow.vm.verify(TOTP)
    flow.settle()
    assert flow.get("step") == "sign_in"
    assert flow.get("error") == text(types.PROVIDER_TWO_FACTOR_EXPIRED)


def test_two_factor_without_an_authenticator_signs_in_on_the_web(flow: Flow) -> None:
    flow.provider.users["user-1"].totp = TOTP
    real = flow.api.sign_in_password

    async def otp_only(origin: str, identifier: str, password: str) -> Any:
        challenge = await real(origin, identifier, password)
        assert isinstance(challenge, types.TwoFactorChallenge)
        return types.TwoFactorChallenge(
            challenge.origin, challenge.identifier, ("otp",), challenge.expires_at
        )

    flow.api.sign_in_password = otp_only  # type: ignore[method-assign]
    flow.sign_in()
    assert flow.get("step") == "sign_in"
    assert flow.get("unavailable") == text(types.PROVIDER_SIGN_IN_UNSUPPORTED)


def test_going_back_to_the_address(flow: Flow) -> None:
    flow.to_sign_in()
    flow.vm.back()
    assert flow.get("step") == "address"
    assert flow.get("address") == flow.provider.base
    flow.vm.back()  # nothing before the first step
    assert flow.get("step") == "address"


# -- replacing another account ------------------------------------------------------------------
def test_another_account_of_the_provider_is_replaced_once_confirmed(flow: Flow) -> None:
    flow.sign_in()
    first = flow.signed_in[0]
    flow.vm.later()
    flow.qtbot.waitUntil(lambda: flow.requests("subscriptions") >= 2)
    assert [p.remote_id for p in flow.provider_profiles()] == [7, 9]

    flow.sign_in("other@example.com")
    assert flow.get("step") == "replace"
    assert flow.get("title") == f"替换「{EMAIL}」？"  # noqa: RUF001
    assert flow.get("message") == "将移除它的 2 份订阅。"
    assert len(flow.signed_in) == 1  # nothing was stored yet
    assert [p.remote_id for p in flow.provider_profiles()] == [7, 9]

    flow.vm.confirmReplace()
    flow.settle()
    assert flow.get("step") == "pick"
    assert flow.signed_in[1] != first
    assert [p.remote_id for p in flow.provider_profiles()] == [11]
    assert flow.requests("sign_out") == 1


def test_replacing_the_account_in_use_names_who_takes_over(flow: Flow) -> None:
    flow.sign_in()
    flow.vm.use()
    flow.qtbot.waitUntil(lambda: not flow.get("active"))
    flow.sign_in("other@example.com")
    assert flow.get("step") == "replace"
    successor = "将移除它的 2 份订阅，改用「示例订阅 A」的规则。"  # noqa: RUF001
    assert flow.get("message") == successor

    flow.vm.cancel()
    assert not flow.get("active")
    flow.drain()
    assert [p.remote_id for p in flow.provider_profiles()] == [7, 9]


# -- 5 the picker, 6 none, 7 the one to use -----------------------------------------------------
def test_several_subscriptions_are_picked_from(flow: Flow, browser: list[str]) -> None:
    flow.provider.add_subscription(
        "user-1", 8, planName="畅享版", upload=0, download=1 * GIB, total=200 * GIB
    )
    flow.provider.add_subscription(
        "user-1", 12, planName="试用", trial=True, expireAt=iso(NOW + DAY), upload=0, download=0
    )
    flow.sign_in()
    assert flow.get("step") == "pick"
    assert flow.get("title") == "选择要使用的订阅"
    rows = flow.rows()
    assert [(r["remoteId"], r["name"], r["usable"]) for r in rows] == [
        (7, "专业版", True),
        (8, "畅享版", True),
        (9, "基础版", False),
        (12, "试用", True),
    ]
    assert rows[0]["status"] == "有效"
    assert rows[0]["tone"] == ""
    assert rows[0]["details"].startswith("已用 10.0 / 100 GB · ")
    assert (rows[2]["status"], rows[2]["tone"]) == ("已到期", "warning")
    assert rows[2]["details"] == "已用 10.0 / 100 GB"
    assert (rows[3]["status"], rows[3]["tone"]) == ("试用 · 1 天后到期", "warning")
    # Nothing of this account is in use: the most traffic left.
    assert flow.get("selected") == 8
    assert flow.get("canUse")

    flow.vm.select(9)  # cannot be used
    assert flow.get("selected") == 8
    flow.vm.renew(9)
    assert browser == [f"{flow.provider.base}/subscription?id=9"]

    flow.vm.select(7)
    assert flow.get("selected") == 7
    profiles_before = flow.heard["profilesChanged"]
    flow.vm.use()
    assert flow.get("busy")
    assert not flow.get("canUse")
    flow.qtbot.waitUntil(lambda: not flow.get("active"), timeout=10_000)
    assert flow.heard["closeRequested"] == 1
    assert flow.heard["applied"] >= 1
    assert flow.heard["profilesChanged"] > profiles_before
    seven = next(p for p in flow.provider_profiles() if p.remote_id == 7)
    assert flow.library.snapshot().active == seven.id
    # The dialog closed: the account syncs, which downloads the other usable ones.
    flow.qtbot.waitUntil(
        lambda: all(p.updated_at > 0 for p in flow.provider_profiles() if p.remote_id in (8, 12)),
        timeout=10_000,
    )


def test_signing_in_again_preselects_the_one_in_use(flow: Flow) -> None:
    flow.provider.add_subscription("user-1", 8, upload=0, download=0, total=500 * GIB)
    flow.sign_in()
    flow.vm.select(7)
    flow.vm.use()
    flow.qtbot.waitUntil(lambda: not flow.get("active"), timeout=10_000)

    flow.sign_in()
    assert flow.get("step") == "pick"
    assert flow.get("selected") == 7  # not 8, which has more left


def test_later_closes_and_syncs(flow: Flow) -> None:
    flow.sign_in()
    synced = flow.requests("subscriptions")
    flow.vm.later()
    assert not flow.get("active")
    assert flow.heard["closeRequested"] == 1
    flow.qtbot.waitUntil(lambda: flow.requests("subscriptions") > synced, timeout=10_000)
    flow.qtbot.waitUntil(lambda: flow.heard["profilesChanged"] >= 2, timeout=10_000)
    # 稍后 uses nothing: the profile in use stays.
    assert not any(p.id == flow.library.snapshot().active for p in flow.provider_profiles())


def test_an_account_without_subscriptions_offers_its_plans(flow: Flow, browser: list[str]) -> None:
    flow.provider.subscriptions = {
        k: v for k, v in flow.provider.subscriptions.items() if v["userId"] != "user-1"
    }
    flow.sign_in()
    assert flow.get("step") == "empty"
    assert flow.get("title") == "这个账户还没有订阅"
    assert flow.get("message")
    flow.vm.browsePlans()
    assert browser == [f"{flow.provider.base}/plans"]
    flow.vm.later()  # 完成
    assert not flow.get("active")
    assert flow.provider_profiles() == []


def test_the_one_usable_subscription_is_used_when_nothing_is_in_use(fresh: Flow) -> None:
    fresh.sign_in()
    assert not fresh.get("active")
    assert fresh.heard["closeRequested"] == 1
    assert fresh.heard["applied"] == 1
    seven = next(p for p in fresh.provider_profiles() if p.remote_id == 7)
    assert fresh.library.snapshot().active == seven.id
    summary = fresh.run(fresh.library.accounts.summary(fresh.signed_in[0]))
    assert summary.state == ProviderState.SIGNED_IN
    assert summary.active == 7


def test_the_one_usable_subscription_is_offered_when_another_is_in_use(flow: Flow) -> None:
    active = flow.library.snapshot().active
    flow.sign_in()
    assert flow.get("step") == "pick"
    assert flow.get("selected") == 7
    assert flow.library.snapshot().active == active


def test_using_it_may_fail_and_is_offered_again(fresh: Flow) -> None:
    fresh.provider.fail("config", 502)
    fresh.sign_in()
    assert fresh.get("step") == "pick"
    assert fresh.get("selected") == 7
    assert fresh.get("error")
    assert fresh.signed_in

    fresh.vm.use()
    fresh.qtbot.waitUntil(lambda: not fresh.get("active"), timeout=10_000)
    assert fresh.library.snapshot().active is not None


# -- cancelling ---------------------------------------------------------------------------------
def test_a_cancelled_sign_in_drops_its_answer(flow: Flow) -> None:
    hold = Hold()
    flow.api.sign_in_password = hold.around(flow.api.sign_in_password)  # type: ignore[method-assign]
    flow.to_sign_in()
    flow.vm.signInPassword(EMAIL, PASSWORD)
    assert flow.get("busy")

    flow.vm.cancel()
    assert not flow.get("active")
    assert not flow.get("busy")
    assert flow.heard["closeRequested"] == 1
    hold.release.set()
    flow.qtbot.waitUntil(hold.finished.is_set, timeout=10_000)
    flow.drain()
    assert flow.get("step") == "address"
    assert not flow.get("active")
    assert flow.signed_in == []
    assert flow.provider_profiles() == []
    assert flow.requests("subscriptions") == 0  # nothing was stored

    # A new flow starts afresh.
    flow.start()
    assert flow.get("step") == "address"
    assert flow.get("error") == ""


def test_a_sign_in_that_completes_after_cancelling_is_synced(flow: Flow) -> None:
    hold = Hold()
    flow.api.subscriptions = hold.around(flow.api.subscriptions)  # type: ignore[method-assign]
    flow.to_sign_in()
    flow.vm.signInPassword(EMAIL, PASSWORD)
    flow.qtbot.waitUntil(hold.entered.is_set, timeout=10_000)  # storing the account
    flow.vm.cancel()
    hold.release.set()
    # The account was stored as the dialog closed: it is synced as after 稍后.
    flow.qtbot.waitUntil(lambda: flow.requests("subscriptions") >= 2, timeout=10_000)
    flow.qtbot.waitUntil(
        lambda: any(p.updated_at > 0 for p in flow.provider_profiles()), timeout=10_000
    )
    assert len(flow.signed_in) == 1
    assert not flow.get("active")
    assert flow.get("step") == "address"


def test_esc_while_probing_drops_the_probe(flow: Flow) -> None:
    hold = Hold()
    flow.api.site = hold.around(flow.api.site)  # type: ignore[method-assign]
    flow.start()
    flow.vm.submitAddress(flow.provider.base)
    assert flow.get("busy")
    flow.vm.cancel()
    flow.start("https://example.com")
    hold.release.set()
    flow.qtbot.waitUntil(hold.finished.is_set, timeout=10_000)
    flow.drain()
    # The new flow is still on its first step, not the old one's second.
    assert flow.get("active")
    assert flow.get("step") == "address"
    assert flow.get("address") == "https://example.com"
    assert not flow.get("busy")


def test_the_texts_follow_the_language(flow: Flow) -> None:
    flow.sign_in()
    with ui_language("en"):
        flow.vm.retranslate()
        assert flow.get("title") == "Choose a subscription to use"
        assert flow.rows()[0]["status"] == "Active"
