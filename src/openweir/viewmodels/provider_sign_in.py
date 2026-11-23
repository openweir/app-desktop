"""Signing in to an OpenWeir provider (docs/accounts.md §2, §3 and §7.3, D55-D58): the steps of
ProviderSignInDialog.qml, one screen each (DESIGN.md "服务商登录对话框").

    address      服务商地址: probe the address, which signs in to nothing
    sign_in      密码 or 邮箱验证码 (a segmented control when the provider offers both); a
                 provider that signs in on the web only (a captcha, or methods v1 lacks) says
                 so and points to 从链接导入 instead
    two_factor   the authenticator's code
    replace      another account of this origin is signed in here: replace it?
    pick         选择要使用的订阅: 使用 or 稍后
    empty        这个账户还没有订阅: 选购套餐 in the browser, or 完成

The account is stored as the sign-in completes, before the picker shows (AccountsService.
complete): from then on, however the dialog closes, the provider is synced once it has, so the
profiles get their configs. Exactly one usable subscription and no profile in use yet skips
the picker and uses that one.

Every request belongs to the flow that made it: cancelling (Esc, 取消) ends the flow, and a
reply to an ended flow is dropped. A sign-in cancelled before its account is stored stops
there; one that was storing it already completes, uses nothing, and is synced as 稍后 would."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtQml import QmlElement, QmlUncreatable

from openweir.models import format as fmt
from openweir.models.library import ProfileLibrary, Updated
from openweir.models.provider import web
from openweir.models.provider.api import origin as make_origin
from openweir.models.provider.sync import blocked, most_left, names
from openweir.models.provider.types import (
    CONDITION_ACTIVE,
    CONDITION_DISABLED,
    CONDITION_EXPIRED,
    CONDITION_EXPIRING,
    CONDITION_NOT_STARTED,
    CONDITION_TRAFFIC_LOW,
    CONDITION_TRAFFIC_USED_UP,
    PROVIDER_ACCOUNT_CONFLICT,
    PROVIDER_CAPTCHA_REQUIRED,
    PROVIDER_CODE_INVALID,
    PROVIDER_EMAIL_UNVERIFIED,
    PROVIDER_INVALID_ADDRESS,
    PROVIDER_NOT_OPENWEIR,
    PROVIDER_SIGN_IN_FAILED,
    PROVIDER_SIGN_IN_UNSUPPORTED,
    PROVIDER_TWO_FACTOR_EXPIRED,
    PROVIDER_TWO_FACTOR_INVALID,
    TWO_FACTOR_TOTP,
    Probe,
    ProviderError,
    ProviderRemoval,
    RemoteSubscription,
    Session,
    SignedIn,
    TwoFactorChallenge,
)
from openweir.viewmodels.common.async_runner import AsyncRunner
from openweir.viewmodels.feedback import failure_text

QML_IMPORT_NAME = "OpenWeir"
QML_IMPORT_MAJOR_VERSION = 1

STEP_ADDRESS = "address"
STEP_SIGN_IN = "sign_in"
STEP_TWO_FACTOR = "two_factor"
STEP_REPLACE = "replace"
STEP_PICK = "pick"
STEP_EMPTY = "empty"

METHOD_PASSWORD = "password"
METHOD_EMAIL_CODE = "email_code"

# The fields an error is written under (DESIGN.md: errors go under the field they belong to);
# FIELD_NONE is the line above the buttons, for what no field caused, such as no connection.
FIELD_NONE = ""
FIELD_ADDRESS = "address"
FIELD_IDENTIFIER = "identifier"
FIELD_PASSWORD = "password"
FIELD_EMAIL = "email"
FIELD_CODE = "code"
FIELD_TWO_FACTOR = "twoFactor"

# 发送验证码 waits this long before it may send again (DESIGN.md "服务商登录对话框").
RESEND_S = 60
TICK_MS = 1000
DAY_S = 86400.0

# What a subscription's condition looks like in the picker: "" is the default colour.
TONE_WARNING = "warning"
TONE_DANGER = "danger"
_TONES = {
    CONDITION_ACTIVE: "",
    CONDITION_DISABLED: TONE_DANGER,
}

# Which field a refused sign-in names, by the method used.
_PASSWORD_FIELDS = {
    PROVIDER_SIGN_IN_FAILED: FIELD_PASSWORD,
    PROVIDER_EMAIL_UNVERIFIED: FIELD_IDENTIFIER,
}
_EMAIL_CODE_FIELDS = {
    PROVIDER_SIGN_IN_FAILED: FIELD_EMAIL,
    PROVIDER_EMAIL_UNVERIFIED: FIELD_EMAIL,
    PROVIDER_CODE_INVALID: FIELD_CODE,
}
# The provider signs in on its web pages only: 从链接导入 is the way in.
_WEB_ONLY = frozenset({PROVIDER_CAPTCHA_REQUIRED, PROVIDER_SIGN_IN_UNSUPPORTED})


@dataclass(frozen=True)
class _Outcome:
    """What a sign-in attempt came to, worked out on the asyncio thread."""

    challenge: TwoFactorChallenge | None = None  # a second step is needed
    session: Session | None = None  # kept for ``replace``
    removal: ProviderRemoval | None = None  # another account of the origin: replace it?
    signed: SignedIn | None = None  # stored
    active: int | None = None  # the remote id in use, signing in again
    used: bool = False  # the one usable subscription is in use now
    use_error: BaseException | None = None  # using it failed: the picker says why


@QmlElement
@QmlUncreatable("ProviderSignInViewModel is created in Python and injected into the view.")  # pyright: ignore[reportCallIssue]
class ProviderSignInViewModel(QObject):
    changed = Signal()
    openRequested = Signal()  # the view shows the dialog, or fills in the address it shows
    closeRequested = Signal()  # the flow ended; the view closes the dialog
    # 从链接导入: once the dialog has closed, the subscriptions page opens its link form.
    linkImportRequested = Signal()
    signedIn = Signal(str)  # a provider's account was stored (its id)
    profilesChanged = Signal()  # profiles were added, renamed or synced; pages read them again
    applied = Signal()  # the tunnel may run another config now

    def __init__(
        self,
        library: ProfileLibrary,
        runner: AsyncRunner,
        clock: Callable[[], float] = time.time,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._library = library
        self._accounts = library.accounts
        self._runner = runner
        self._clock = clock
        self._flow = 0  # replies to an older flow are dropped
        # Set as the flow ends, for the requests of it that run on: read on the asyncio thread.
        self._stopped = threading.Event()
        self._active = False
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._reset()

    def _reset(self) -> None:
        self._step = STEP_ADDRESS
        self._address = ""
        self._busy = False
        self._sending = False
        self._errors: dict[str, str] = {}
        self._probe: Probe | None = None
        self._method = METHOD_PASSWORD
        self._unavailable = ""
        self._code_sent = False
        self._sent_at = 0.0
        self._resend = 0
        self._challenge: TwoFactorChallenge | None = None
        self._session: Session | None = None
        self._removal: ProviderRemoval | None = None
        self._provider_id = ""  # stored during this flow: synced as it ends
        self._subscriptions: tuple[RemoteSubscription, ...] = ()
        self._selected = 0
        self._timer.stop()

    def retranslate(self) -> None:
        self.changed.emit()

    def stop(self) -> None:
        self._timer.stop()

    # -- the flow ----------------------------------------------------------------------------
    @Slot(str)
    def open(self, address: str) -> None:
        """Start signing in, with ``address`` filled in when it is not empty (an
        openweir://provider link, or 重新登录 of a provider): the dialog stops at the address
        and signs in to nothing by itself. While it is open already, the address it shows
        changes on that first step, and later steps ignore this."""
        address = address.strip()
        if self._active:
            if self._step == STEP_ADDRESS and not self._busy and address:
                self._address = address
                self._errors = {}
                self.changed.emit()
                self.openRequested.emit()
            return
        self._flow += 1
        self._stopped = threading.Event()
        self._reset()
        self._active = True
        self._address = address
        self.changed.emit()
        self.openRequested.emit()

    @Slot()
    def cancel(self) -> None:
        """取消, Esc, or the dialog closed: replies on their way are dropped."""
        if self._active:
            self._finish()

    @Slot()
    def later(self) -> None:
        """稍后 on the picker, 完成 without subscriptions."""
        if self._active and not self._busy:
            self._finish()

    def _finish(self) -> None:
        """End the flow and close the dialog; sync the account it signed in to, if any, which
        downloads the configs of its usable profiles (docs/accounts.md §5.3)."""
        provider_id = self._provider_id
        self._flow += 1
        self._stopped.set()
        self._active = False
        self._reset()
        self.changed.emit()
        self.closeRequested.emit()
        if provider_id:
            self._sync(provider_id)

    def _sync(self, provider_id: str) -> None:
        def synced(update: Updated) -> None:
            self.profilesChanged.emit()
            if update.applied:
                self.applied.emit()

        # A failure is the account's to show (its summary keeps it); the profiles may have
        # changed all the same.
        self._runner.submit(
            self._accounts.sync(provider_id), synced, lambda _error: self.profilesChanged.emit()
        )

    def _request(
        self,
        action: Coroutine[Any, Any, Any],
        done: Callable[[Any], None],
        *,
        sending: bool = False,
        stale: Callable[[Any], None] | None = None,
    ) -> None:
        """Run a request of this flow: the dialog is busy until it answers, and errors go
        under the field they name. ``stale`` takes a result that came after the flow ended."""
        flow = self._flow
        self._busy, self._sending = True, sending
        self._errors = {}
        self.changed.emit()

        def answered(result: Any) -> None:
            if flow != self._flow:
                if stale is not None:
                    stale(result)
                return
            self._busy = self._sending = False
            done(result)
            self.changed.emit()

        def failed(error: BaseException) -> None:
            if flow != self._flow:
                return
            self._busy = self._sending = False
            self._failed(error)
            self.changed.emit()

        self._runner.submit(action, answered, failed)

    def _failed(self, error: BaseException) -> None:
        code = getattr(error, "code", "")
        if code in _WEB_ONLY and self._step in (STEP_SIGN_IN, STEP_TWO_FACTOR):
            # A captcha that only shows on the request: the web is the way in after all.
            self._step = STEP_SIGN_IN
            self._challenge = None
            self._unavailable = failure_text(error)
            return
        if code == PROVIDER_TWO_FACTOR_EXPIRED:
            # The challenge is over: sign in again from the first step's fields.
            self._step = STEP_SIGN_IN
            self._challenge = None
        self._errors = {self._field(code): failure_text(error)}

    def _field(self, code: str) -> str:
        if self._step == STEP_ADDRESS:
            if code in (PROVIDER_INVALID_ADDRESS, PROVIDER_NOT_OPENWEIR):
                return FIELD_ADDRESS
            return FIELD_NONE
        if self._step == STEP_SIGN_IN:
            fields = _PASSWORD_FIELDS if self._method == METHOD_PASSWORD else _EMAIL_CODE_FIELDS
            return fields.get(code, FIELD_NONE)
        if self._step == STEP_TWO_FACTOR and code == PROVIDER_TWO_FACTOR_INVALID:
            return FIELD_TWO_FACTOR
        return FIELD_NONE

    # -- 1 the address -----------------------------------------------------------------------
    @Slot(str)
    def submitAddress(self, address: str) -> None:
        """继续: the address made an origin (https only), then what the provider is and how
        it signs in."""
        if not self._active or self._busy or self._step != STEP_ADDRESS:
            return
        try:
            origin = make_origin(address)
        except ProviderError as error:
            self._errors = {FIELD_ADDRESS: failure_text(error)}
            self.changed.emit()
            return
        self._address = origin
        self._request(self._accounts.probe(origin), self._probed)

    def _probed(self, probe: Probe) -> None:
        options = probe.options
        self._probe = probe
        self._address = probe.origin
        self._method = METHOD_PASSWORD if options.password else METHOD_EMAIL_CODE
        self._unavailable = ""
        if options.unavailable:
            error = ProviderError(options.unavailable, "the provider signs in on the web only")
            self._unavailable = failure_text(error)
        self._code_sent = False
        self._step = STEP_SIGN_IN

    # -- 2 signing in ------------------------------------------------------------------------
    @Slot(str)
    def setMethod(self, method: str) -> None:
        if self._busy or method == self._method or method not in self._methods():
            return
        self._method = method
        self._errors = {}
        self.changed.emit()

    def _methods(self) -> list[str]:
        options = self._probe.options if self._probe is not None else None
        if options is None:
            return []
        return [
            method
            for method, works in (
                (METHOD_PASSWORD, options.password),
                (METHOD_EMAIL_CODE, options.email_code),
            )
            if works
        ]

    @Slot(str, str)
    def signInPassword(self, identifier: str, password: str) -> None:
        probe = self._probe
        if not self._can_sign_in(METHOD_PASSWORD) or probe is None:
            return
        if not identifier.strip() or not password:
            return
        attempt = self._accounts.sign_in_password(probe, identifier, password)
        self._request(
            self._signing_in(probe, attempt, self._stopped), self._answered, stale=self._late
        )

    @Slot(str)
    def sendCode(self, email: str) -> None:
        """发送验证码, 重新发送."""
        probe = self._probe
        if not self._can_sign_in(METHOD_EMAIL_CODE) or probe is None or self._resend > 0:
            return
        if not email.strip():
            return
        self._request(
            self._accounts.send_email_code(probe, email.strip()), self._code_went, sending=True
        )

    def _code_went(self, _: None) -> None:
        self._code_sent = True
        self._sent_at = self._clock()
        self._resend = RESEND_S
        self._timer.start()

    def _tick(self) -> None:
        left = max(0, math.ceil(self._sent_at + RESEND_S - self._clock()))
        if left != self._resend:
            self._resend = left
            self.changed.emit()
        if left == 0:
            self._timer.stop()

    @Slot(str, str)
    def signInEmailCode(self, email: str, code: str) -> None:
        probe = self._probe
        if not self._can_sign_in(METHOD_EMAIL_CODE) or probe is None or not self._code_sent:
            return
        if not email.strip() or not code.strip():
            return
        attempt = self._accounts.sign_in_email_code(probe, email.strip(), code.strip())
        self._request(
            self._signing_in(probe, attempt, self._stopped), self._answered, stale=self._late
        )

    def _can_sign_in(self, method: str) -> bool:
        return (
            self._active
            and not self._busy
            and self._step == STEP_SIGN_IN
            and not self._unavailable
            and self._method == method
        )

    async def _signing_in(
        self,
        probe: Probe,
        attempt: Coroutine[Any, Any, Session | TwoFactorChallenge],
        stopped: threading.Event,
    ) -> _Outcome:
        answer = await attempt
        if isinstance(answer, TwoFactorChallenge):
            return _Outcome(challenge=answer)
        return await self._completing(probe, answer, stopped, replace=False)

    async def _completing(
        self, probe: Probe, session: Session, stopped: threading.Event, *, replace: bool
    ) -> _Outcome:
        """Store the account and bring its subscriptions in (on the asyncio thread), then
        the picker's first rule: exactly one usable and nothing in use is used at once. A
        flow that ended stores nothing, or, ending while it stored, uses nothing."""
        accounts = self._accounts
        if stopped.is_set():
            return _Outcome()
        try:
            signed = await accounts.complete(probe, session, replace=replace)
        except ProviderError as error:
            if error.code != PROVIDER_ACCOUNT_CONFLICT or replace:
                raise
            summaries = await accounts.summaries()
            existing = next(
                (s.provider for s in summaries if s.provider.origin == probe.origin), None
            )
            if existing is None:
                raise
            return _Outcome(session=session, removal=await accounts.sign_out_removal(existing.id))
        provider_id = signed.provider.id
        active = (await accounts.summary(provider_id)).active
        now = self._clock()
        usable = [s for s in signed.subscriptions if not blocked(s, now)]
        if stopped.is_set():
            return _Outcome(signed=signed, active=active)
        if len(usable) == 1 and signed.profiles.active is None:
            try:
                await accounts.use(provider_id, usable[0].id)
            except Exception as error:  # shown on the picker, which offers it again
                return _Outcome(signed=signed, active=active, use_error=error)
            return _Outcome(signed=signed, used=True)
        return _Outcome(signed=signed, active=active)

    def _answered(self, outcome: _Outcome) -> None:
        if outcome.challenge is not None:
            if TWO_FACTOR_TOTP not in outcome.challenge.methods:
                # v1 verifies authenticator codes only (D56): this account signs in on the web.
                error = ProviderError(PROVIDER_SIGN_IN_UNSUPPORTED, "no authenticator two-factor")
                self._unavailable = failure_text(error)
                return
            self._challenge = outcome.challenge
            self._step = STEP_TWO_FACTOR
        elif outcome.removal is not None:
            self._session = outcome.session
            self._removal = outcome.removal
            self._step = STEP_REPLACE
        elif outcome.signed is not None:
            self._stored(outcome)

    def _stored(self, outcome: _Outcome) -> None:
        signed = outcome.signed
        assert signed is not None
        self._provider_id = signed.provider.id
        self._session = None
        self.signedIn.emit(signed.provider.id)
        self.profilesChanged.emit()
        if outcome.used:
            self.applied.emit()
            self._finish()
            return
        self._subscriptions = signed.subscriptions
        if not signed.subscriptions:
            self._step = STEP_EMPTY
            return
        self._step = STEP_PICK
        self._selected = self._preselected(outcome.active)
        if outcome.use_error is not None:
            self._errors = {FIELD_NONE: failure_text(outcome.use_error)}

    def _late(self, outcome: _Outcome) -> None:
        """A sign-in that completed after the dialog closed: the account is stored, so it is
        synced as if 稍后 had been pressed."""
        if outcome.signed is None:
            return
        self.signedIn.emit(outcome.signed.provider.id)
        self.profilesChanged.emit()
        if outcome.used:
            self.applied.emit()
        self._sync(outcome.signed.provider.id)

    # -- 3 two-factor ------------------------------------------------------------------------
    @Slot(str)
    def verify(self, code: str) -> None:
        probe, challenge = self._probe, self._challenge
        if not self._active or self._busy or self._step != STEP_TWO_FACTOR:
            return
        if probe is None or challenge is None or not code.strip():
            return
        self._request(
            self._verifying(probe, challenge, code.strip(), self._stopped),
            self._answered,
            stale=self._late,
        )

    async def _verifying(
        self, probe: Probe, challenge: TwoFactorChallenge, code: str, stopped: threading.Event
    ) -> _Outcome:
        session = await self._accounts.verify_two_factor(challenge, code)
        return await self._completing(probe, session, stopped, replace=False)

    @Slot()
    def back(self) -> None:
        """上一步: the address from signing in, signing in from two-factor."""
        if not self._active or self._busy:
            return
        if self._step == STEP_SIGN_IN:
            self._probe = None
            self._unavailable = ""
            self._code_sent = False
            self._resend = 0
            self._timer.stop()
            self._step = STEP_ADDRESS
        elif self._step == STEP_TWO_FACTOR:
            self._challenge = None
            self._step = STEP_SIGN_IN
        else:
            return
        self._errors = {}
        self.changed.emit()

    # -- replacing another account ----------------------------------------------------------
    @Slot()
    def confirmReplace(self) -> None:
        """替换: sign out of the other account (its profiles go), then complete this one."""
        probe, session = self._probe, self._session
        if not self._active or self._busy or self._step != STEP_REPLACE:
            return
        if probe is None or session is None:
            return
        self._request(
            self._completing(probe, session, self._stopped, replace=True),
            self._answered,
            stale=self._late,
        )

    # -- 5 the picker, 6 no subscriptions ----------------------------------------------------
    def _preselected(self, active: int | None) -> int:
        """The one in use when signing in again, else the usable one with the most traffic
        left; 0 for none (docs/accounts.md §5.5)."""
        now = self._clock()
        usable = {s.id for s in self._subscriptions if not blocked(s, now)}
        if active in usable:
            assert active is not None
            return active
        best = most_left(self._subscriptions, now)
        return best.id if best is not None else 0

    @Slot(int)
    def select(self, remote_id: int) -> None:
        now = self._clock()
        if self._busy or self._step != STEP_PICK or remote_id == self._selected:
            return
        if any(s.id == remote_id and not blocked(s, now) for s in self._subscriptions):
            self._selected = remote_id
            self.changed.emit()

    @Slot()
    def use(self) -> None:
        """使用: download the subscription's config when it has none, make it the profile in
        use, and reload a running tunnel."""
        if not self._active or self._busy or self._step != STEP_PICK or not self._selected:
            return
        provider_id = self._provider_id

        def done(_: object) -> None:
            self.profilesChanged.emit()
            self.applied.emit()
            self._finish()

        def late(_: object) -> None:
            self.profilesChanged.emit()
            self.applied.emit()

        self._request(self._accounts.use(provider_id, self._selected), done, stale=late)

    @Slot(int)
    def renew(self, remote_id: int) -> None:
        """续费 in the user center (D58); the account is synced on return."""
        if self._probe is not None and self._provider_id:
            self._browse(web.renew(self._probe.origin, remote_id))

    @Slot()
    def browsePlans(self) -> None:
        """选购套餐 in the user center (D58)."""
        if self._probe is not None and self._provider_id:
            self._browse(web.plans(self._probe.origin))

    def _browse(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))
        # What is bought there shows once the window is back (docs/accounts.md §5.3).
        self._runner.submit(self._accounts.watch(self._provider_id), None, lambda _error: None)

    @Slot()
    def importFromLink(self) -> None:
        """从链接导入, for a provider that signs in on the web only: the dialog closes and the
        subscriptions page opens its link form, for the link copied from the user center."""
        if not self._active:
            return
        self.linkImportRequested.emit()
        self._finish()

    # -- what the dialog shows ---------------------------------------------------------------
    @Property(bool, notify=changed)
    def active(self) -> bool:
        """The dialog is open on a flow."""
        return self._active

    @Property(str, notify=changed)
    def step(self) -> str:
        return self._step

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        """A request is on its way: fields and buttons wait, Esc still cancels."""
        return self._busy

    @Property(bool, notify=changed)
    def sending(self) -> bool:
        """The request on its way sends an email code."""
        return self._sending

    @Property(str, notify=changed)
    def title(self) -> str:
        if self._step == STEP_SIGN_IN:
            return self.tr("登录「{site}」").format(site=self._site_name())
        if self._step == STEP_TWO_FACTOR:
            return self.tr("两步验证")
        if self._step == STEP_REPLACE:
            account = self._removal.provider.account if self._removal is not None else ""
            return self.tr("替换「{account}」？").format(account=account)
        if self._step == STEP_PICK:
            return self.tr("选择要使用的订阅")
        if self._step == STEP_EMPTY:
            return self.tr("这个账户还没有订阅")
        return self.tr("登录服务商")

    @Property(str, notify=changed)
    def message(self) -> str:
        """The step's body text, where it has one."""
        if self._step == STEP_TWO_FACTOR:
            return self.tr("输入身份验证器中的验证码。")
        if self._step == STEP_REPLACE and self._removal is not None:
            return self._removal_text(self._removal)
        if self._step == STEP_EMPTY:
            return self.tr("在服务商的网页上选购套餐后，订阅会出现在订阅页。")
        return ""

    def _removal_text(self, removal: ProviderRemoval) -> str:
        """What replacing the account removes, as signing out of it would (docs/accounts.md
        §4.4)."""
        count = len(removal.profiles)
        if removal.active and removal.successor is not None:
            text = self.tr("将移除它的 %n 份订阅，改用「{successor}」的规则。", "", count)
            return text.format(successor=removal.successor.name)
        if removal.active:
            return self.tr("将移除它的 %n 份订阅，改用我的方案。", "", count)
        return self.tr("将移除它的 %n 份订阅。", "", count)

    @Property(str, notify=changed)
    def address(self) -> str:
        """The address to fill in: given to ``open``, or the origin it was made."""
        return self._address

    def _host(self) -> str:
        return urlsplit(self._probe.origin).netloc if self._probe is not None else ""

    def _site_name(self) -> str:
        if self._probe is None:
            return ""
        return self._probe.site.name or self._host()

    @Property(str, notify=changed)
    def siteName(self) -> str:
        return self._site_name()

    @Property(str, notify=changed)
    def host(self) -> str:
        """The provider's host (and port), under the sign-in step's title."""
        return self._host()

    @Property("QVariantList", notify=changed)  # pyright: ignore[reportArgumentType]
    def methodOptions(self) -> list[dict[str, str]]:
        """密码 / 邮箱验证码 when the provider takes both in the app; empty otherwise."""
        methods = self._methods()
        if len(methods) < 2 or self._unavailable:
            return []
        labels = {METHOD_PASSWORD: self.tr("密码"), METHOD_EMAIL_CODE: self.tr("邮箱验证码")}
        return [{"value": method, "label": labels[method]} for method in methods]

    @Property(str, notify=changed)
    def method(self) -> str:
        return self._method

    @Property(str, notify=changed)
    def identifierLabel(self) -> str:
        options = self._probe.options if self._probe is not None else None
        return (
            self.tr("邮箱或用户名") if options is not None and options.username else self.tr("邮箱")
        )

    @Property(int, notify=changed)
    def codeLength(self) -> int:
        return self._probe.options.code_length if self._probe is not None else 6

    @Property(str, notify=changed)
    def unavailable(self) -> str:
        """Why the provider signs in on the web only (a captcha, or methods v1 lacks), with
        the way to 从链接导入; empty while the app can sign in."""
        return self._unavailable

    @Property(bool, notify=changed)
    def codeSent(self) -> bool:
        return self._code_sent

    @Property(int, notify=changed)
    def resendSeconds(self) -> int:
        """Seconds until 重新发送 may send again."""
        return self._resend

    @Property(str, notify=changed)
    def sendLabel(self) -> str:
        if self._sending:
            return self.tr("正在发送…")
        if self._resend > 0:
            return self.tr("重新发送（{seconds}）").format(seconds=self._resend)
        return self.tr("重新发送") if self._code_sent else self.tr("发送验证码")

    @Property(str, notify=changed)
    def addressError(self) -> str:
        return self._errors.get(FIELD_ADDRESS, "")

    @Property(str, notify=changed)
    def identifierError(self) -> str:
        return self._errors.get(FIELD_IDENTIFIER, "")

    @Property(str, notify=changed)
    def passwordError(self) -> str:
        return self._errors.get(FIELD_PASSWORD, "")

    @Property(str, notify=changed)
    def emailError(self) -> str:
        return self._errors.get(FIELD_EMAIL, "")

    @Property(str, notify=changed)
    def codeError(self) -> str:
        """Under the emailed code."""
        return self._errors.get(FIELD_CODE, "")

    @Property(str, notify=changed)
    def twoFactorError(self) -> str:
        return self._errors.get(FIELD_TWO_FACTOR, "")

    @Property(str, notify=changed)
    def error(self) -> str:
        """What no field caused, such as no connection: above the buttons."""
        return self._errors.get(FIELD_NONE, "")

    @Property("QVariantList", notify=changed)  # pyright: ignore[reportArgumentType]
    def subscriptions(self) -> list[dict[str, Any]]:
        """The picker's rows, by id: remoteId, name, status (its colour in ``tone``: "",
        warning or danger), details (usage and expiry) and whether it can be used; one that
        cannot offers 续费 instead."""
        now = self._clock()
        titles = names(self._subscriptions)
        rows = []
        for subscription in self._subscriptions:
            usable = not blocked(subscription, now)
            condition = subscription.condition(now)
            status = self._condition(subscription, condition, now)
            if subscription.trial:
                status = self.tr("试用 · {status}").format(status=status)
            rows.append(
                {
                    "remoteId": subscription.id,
                    "name": titles[subscription.id],
                    "status": status,
                    "tone": _TONES.get(condition, TONE_WARNING),
                    "details": self._details(subscription, now),
                    "usable": usable,
                }
            )
        return rows

    def _condition(self, subscription: RemoteSubscription, condition: str, now: float) -> str:
        if condition == CONDITION_EXPIRING:
            days = max(1, math.ceil(((subscription.expire_at or now) - now) / DAY_S))
            return self.tr("%n 天后到期", "", days)
        if condition == CONDITION_TRAFFIC_LOW:
            percent = math.floor(subscription.used * 100 / (subscription.total or 1))
            return self.tr("流量已用 {percent}%").format(percent=percent)
        texts = {
            CONDITION_ACTIVE: self.tr("有效"),
            CONDITION_EXPIRED: self.tr("已到期"),
            CONDITION_TRAFFIC_USED_UP: self.tr("流量已用完"),
            CONDITION_NOT_STARTED: self.tr("未开始"),
            CONDITION_DISABLED: self.tr("已停用"),
        }
        return texts.get(condition, self.tr("暂不可用"))

    def _details(self, subscription: RemoteSubscription, now: float) -> str:
        if subscription.total:
            used = fmt.usage(subscription.used, subscription.total)
        else:
            used = fmt.size(subscription.used)
        parts = [self.tr("已用 {usage}").format(usage=used)]
        if subscription.expire_at is not None and subscription.expire_at > now:
            parts.append(self.tr("{date} 到期").format(date=fmt.date(subscription.expire_at)))
        return " · ".join(parts)

    @Property(int, notify=changed)
    def selected(self) -> int:
        """The remote id the picker has selected; 0 for none."""
        return self._selected

    @Property(bool, notify=changed)
    def canUse(self) -> bool:
        return self._step == STEP_PICK and bool(self._selected) and not self._busy
