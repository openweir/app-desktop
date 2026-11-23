"""Account ViewModel: the 账户 page (docs/accounts.md §6, §7.4; DESIGN.md "账户页"). It shows one
provider account at a time: the subscription in use or the best one, the account's other
subscriptions, the wallet, the plans on sale and the latest orders. Nothing here changes the
account but auto-renew; buying, renewing, paying, topping up, passwords and two-factor open
the provider's user center in the browser (D58), and the account syncs once the window is
back.

What it reads, and when:

    the accounts      AccountsService.summaries() and the profiles' names, from memory: at
                      start, whenever the subscriptions page's list changes (its minute's
                      check syncs the accounts first), after signing in, and after what this
                      page does
    billing           sync_all(max_age=60) as the page shows; sync_all() as the window comes
                      back, which syncs the accounts whose user center was opened in the last
                      10 minutes (Accounts.watch); sync(id) on 刷新
    wallet, plans,    as the page shows, for the provider it shows, after 刷新, and as the
    orders            window comes back from the user center; never stored. Not read while
                      the account needs signing in again.

Money is formatted with the site's currency as the last sync of this run read it
(models/format.py), so the rows are made again as it arrives.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, fields
from typing import Any

from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtQml import QmlElement, QmlUncreatable

from openweir.models import format as fmt
from openweir.models.library import ProfileLibrary, Updated
from openweir.models.library.accounts import WATCH_S
from openweir.models.profiles import ProfileError, ProfileList
from openweir.models.provider import web
from openweir.models.provider.sync import blocked, most_left, names, next_reset
from openweir.models.provider.types import (
    CONDITION_EXPIRED,
    CONDITION_TRAFFIC_USED_UP,
    ORDER_CANCELLED,
    ORDER_CHANGE,
    ORDER_COMPLETED,
    ORDER_NEW,
    ORDER_PENDING,
    ORDER_REFUNDED,
    ORDER_RENEW,
    ORDER_TOPUP,
    ORDER_TRAFFIC_PACK,
    PERIOD_HALF_YEAR,
    PERIOD_MONTH,
    PERIOD_ONE_TIME,
    PERIOD_QUARTER,
    PERIOD_YEAR,
    PLAN_TRAFFIC_PACK,
    PROVIDER_ACCOUNT_DISABLED,
    PROVIDER_LINK_UNAVAILABLE,
    PROVIDER_SIGN_IN_REQUIRED,
    RESET_NONE,
    AccountSummary,
    Currency,
    OrderSummary,
    ProviderError,
    ProviderRemoval,
    ProviderState,
    PublicPlan,
    RemoteSubscription,
    Wallet,
)
from openweir.viewmodels.common.async_runner import AsyncRunner
from openweir.viewmodels.common.keyed_list import KeyedListModel
from openweir.viewmodels.feedback import failure_text
from openweir.viewmodels.profiles import condition_status, sign_out_text

QML_IMPORT_NAME = "OpenWeir"
QML_IMPORT_MAJOR_VERSION = 1

# The colour a status is written in: "" is the default one (DESIGN.md "服务商的订阅行").
TONE_WARNING = "warning"
TONE_DANGER = "danger"
# What a row of 此账户的订阅 offers after its status.
ACTION_USE = "use"  # 使用
ACTION_RENEW = "renew"  # 续费, in the browser: it cannot be used until renewed
ORDERS = 5  # the latest orders the page lists
# The account states in which nothing can be read for it until it signs in again.
_SIGNED_OUT = (ProviderState.SIGN_IN_REQUIRED, ProviderState.ACCOUNT_DISABLED)
# Failures that leave the account in one of them: the header says so by the state.
_SIGNS_OUT = frozenset({PROVIDER_SIGN_IN_REQUIRED, PROVIDER_ACCOUNT_DISABLED})


# The rows' ids are for KeyedListModel; QML reserves ``id``, so the roles name them otherwise.
@dataclass(frozen=True)
class SubscriptionRow:
    """A row of 此账户的订阅."""

    remoteId: int
    name: str
    status: str
    tone: str
    expiry: str
    inUse: bool  # 使用中
    action: str  # ACTION_USE, ACTION_RENEW, or "" for none

    @property
    def id(self) -> str:
        return str(self.remoteId)


@dataclass(frozen=True)
class PlanRow:
    """A plan on sale: its name, what it gives, and its price per period (DESIGN.md "金额":
    the amount in monospace, the period after it in the body font)."""

    planId: str
    name: str
    detail: str  # traffic and devices, before the prices
    prices: list[dict[str, str]]  # [{"amount": "¥15.00", "per": " / 月"}]
    priceText: str  # the prices in one line, for screen readers
    soldOut: bool

    @property
    def id(self) -> str:
        return self.planId


@dataclass(frozen=True)
class OrderRow:
    """One of the latest orders."""

    orderId: str
    title: str  # the plan, or 充值
    detail: str  # the kind of order, its period and its date
    amount: str
    status: str
    pending: bool  # waiting for payment: 去支付, the status in warning

    @property
    def id(self) -> str:
        return self.orderId


@dataclass
class _Read[T]:
    """What one request for the account page answered: the value, or why it failed."""

    value: T | None = None
    error: BaseException | None = None


@dataclass
class _View:
    """What the page shows, made again as anything it shows changes."""

    status: str = ""
    tone: str = ""
    sign_in: str = ""  # 需要重新登录 or 账户已停用, while the account is signed out
    sign_in_danger: bool = False
    current: RemoteSubscription | None = None
    current_name: str = ""
    current_status: str = ""
    current_tone: str = ""
    usage: str = ""
    has_quota: bool = False
    used_fraction: float = 0.0
    reset: str = ""
    expiry: str = ""
    devices: str = ""
    auto_renew: bool = False
    balance: str = ""
    points: str = ""
    level: str = ""
    wallet_note: str = ""
    wallet_danger: bool = False
    plans_note: str = ""
    plans_danger: bool = False
    orders_note: str = ""
    orders_danger: bool = False
    options: list[dict[str, str]] = field(default_factory=list)


def _roles(row: type) -> list[str]:
    return [f.name for f in fields(row)]


@QmlElement
@QmlUncreatable("AccountViewModel is created in Python and injected into the view.")  # pyright: ignore[reportCallIssue]
class AccountViewModel(QObject):
    changed = Signal()
    # Whether there is a provider account at all: the sidebar lists the page only then.
    accountsChanged = Signal(bool)
    signInRequested = Signal(str)  # 重新登录…: the sign-in dialog, on the provider's origin
    signOutRequested = Signal()  # 退出登录 awaits confirmation; the view asks
    profilesChanged = Signal()  # 使用, 退出登录 or a sync changed the profiles
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
        self._summaries: tuple[AccountSummary, ...] = ()
        self._names: dict[tuple[str, int], str] = {}  # profile names by provider and remote id
        self._loaded = False  # the accounts were read once
        self._provider_id = ""  # the one shown
        self._shown = False  # the page shows
        self._reading = False
        self._read_again = False  # asked for while a read was on its way
        self._syncing = False  # sync_all
        self._refreshing = False  # 刷新
        self._error = ""  # what the last action ran into, in the header
        self._watch_until = 0.0  # the user center was opened: read again on return
        # What the wallet, plans and orders were read for: the provider, and whether it was
        # signed in. Another provider, or one signed in again, reads them anew.
        self._extras_for: tuple[str, bool] | None = None
        self._extras = 0  # the reads of a former provider are dropped
        self._wallet = _Read[Wallet]()
        self._plans = _Read[tuple[PublicPlan, ...]]()
        self._orders = _Read[tuple[OrderSummary, ...]]()
        self._renewing: bool | None = None  # auto-renew as asked, until billing answers
        self._renew_error: BaseException | None = None
        self._sign_out: ProviderRemoval | None = None  # the last one asked about
        self._sign_out_pending = False  # while the dialog closes
        self._signing_out = False
        self._view = _View()
        self._read_timer = QTimer(self)
        self._read_timer.setSingleShot(True)
        self._read_timer.setInterval(0)  # every change in one turn of the event loop reads once
        self._read_timer.timeout.connect(self._read)
        # Rows update in place, so the lists keep their delegates and the keyboard focus.
        self._subscriptions = KeyedListModel(_roles(SubscriptionRow), self)
        self._plan_rows = KeyedListModel(_roles(PlanRow), self)
        self._order_rows = KeyedListModel(_roles(OrderRow), self)
        self.changed.connect(self._rebuild)

    def start(self) -> None:
        self.reload()

    def stop(self) -> None:
        self._read_timer.stop()

    def retranslate(self) -> None:
        """Re-read the texts in the language just installed. The provider names its plans in
        the language it is asked in: shown, the page reads them again."""
        self.changed.emit()
        if self._shown:
            self._read_extras()

    # -- reading the accounts ----------------------------------------------------------------
    @Slot()
    def reload(self) -> None:
        """Read the accounts again, once for all that asks in one turn of the event loop."""
        self._read_timer.start()

    def _read(self) -> None:
        if self._reading:
            self._read_again = True
            return
        self._reading = True
        self._read_again = False

        async def read() -> tuple[tuple[AccountSummary, ...], ProfileList]:
            return await self._accounts.summaries(), await self._library.current()

        def done(result: tuple[tuple[AccountSummary, ...], ProfileList]) -> None:
            self._reading = False
            self._take(*result)
            if self._read_again:
                self._read()

        def failed(error: BaseException) -> None:
            self._reading = False
            self._error = failure_text(error)
            self.changed.emit()
            if self._read_again:
                self._read()

        self._runner.submit(read(), done, failed)

    def _take(self, summaries: tuple[AccountSummary, ...], profiles: ProfileList) -> None:
        had = bool(self._summaries)
        self._summaries = summaries
        self._names = {
            (p.provider_id, p.remote_id): p.name
            for p in profiles.profiles
            if p.is_provider and p.provider_id is not None and p.remote_id is not None
        }
        self._loaded = True
        if self._summary() is None:
            self._provider_id = summaries[0].provider.id if summaries else ""
            self._renewing, self._renew_error = None, None
        self.changed.emit()
        if bool(summaries) != had:
            self.accountsChanged.emit(bool(summaries))
        # Another provider, or one that signed in again or out: its wallet, plans and orders.
        if self._shown and self._extras_key() != self._extras_for:
            self._read_extras()

    def _summary(self) -> AccountSummary | None:
        return next((s for s in self._summaries if s.provider.id == self._provider_id), None)

    def _signed_out(self, summary: AccountSummary | None) -> bool:
        return summary is not None and summary.state in _SIGNED_OUT

    def _extras_key(self) -> tuple[str, bool] | None:
        summary = self._summary()
        if summary is None:
            return None
        return summary.provider.id, not self._signed_out(summary)

    # -- the page ----------------------------------------------------------------------------
    def set_shown(self, shown: bool) -> None:
        """The page shows, or another does. Shown, it syncs the accounts not synced in the
        last minute (docs/accounts.md §5.3) and reads the wallet, plans and orders."""
        self._shown = shown
        if not shown:
            return
        self._sync_all(max_age=60)
        self._read_extras()

    @Slot()
    def windowActivated(self) -> None:
        """The window became active again, maybe back from the user center (D58): sync the
        accounts watched since a page of it was opened (Accounts.watch), and read what was
        bought or paid for."""
        if not self._shown or not self._summaries:
            return
        self._sync_all()
        if self._clock() < self._watch_until:
            self._read_extras()

    def _sync_all(self, *, max_age: float | None = None) -> None:
        if self._syncing or not self._summaries:
            return
        self._syncing = True
        accounts = self._accounts
        sync = accounts.sync_all() if max_age is None else accounts.sync_all(max_age=max_age)

        def synced(update: Updated) -> None:
            self._syncing = False
            self._synced(update)

        def failed(error: BaseException) -> None:
            self._syncing = False
            self._error = failure_text(error)
            self.reload()

        self._runner.submit(sync, synced, failed)

    def _synced(self, update: Updated) -> None:
        """A sync may have added, renamed or updated profiles: the pages read them again."""
        self.profilesChanged.emit()
        if update.applied:
            self.applied.emit()
        self.reload()

    @Property(bool, notify=changed)
    def hasAccounts(self) -> bool:
        return bool(self._summaries)

    @Property(bool, notify=changed)
    def loaded(self) -> bool:
        return self._loaded

    @Property(str, notify=changed)
    def providerId(self) -> str:
        return self._provider_id

    @Property("QVariantList", notify=changed)  # pyright: ignore[reportArgumentType]
    def providerOptions(self) -> list[dict[str, str]]:
        """The providers to choose from: the site's name, and the account as its note."""
        return self._view.options

    @Property(bool, notify=changed)
    def multiple(self) -> bool:
        """More than one provider: the header has a dropdown to choose."""
        return len(self._summaries) > 1

    @Slot(str)
    def selectProvider(self, provider_id: str) -> None:
        """Show ``provider_id``'s account: from the header's dropdown, or the overview's 续费
        on its subscription in use."""
        if provider_id == self._provider_id:
            return
        if not any(s.provider.id == provider_id for s in self._summaries):
            # Not read yet, such as one just signed in to: shown once it is.
            self._provider_id = provider_id
            self.reload()
            return
        self._provider_id = provider_id
        self._error = ""
        self._renewing, self._renew_error = None, None
        self.changed.emit()
        if self._shown:
            self._read_extras()

    def signed_in(self, provider_id: str) -> None:
        """A sign-in completed: show that account."""
        self._provider_id = provider_id
        self._error = ""
        self.reload()

    @Property(str, notify=changed)
    def siteName(self) -> str:
        summary = self._summary()
        return summary.provider.name if summary is not None else ""

    @Property(str, notify=changed)
    def status(self) -> str:
        """The header's status (DESIGN.md "账户页"): the account and its site; why it needs
        signing in again; when it last synced while the provider cannot be reached; or what
        the last action ran into."""
        return self._view.status

    @Property(str, notify=changed)
    def statusTone(self) -> str:
        return self._view.tone

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        """刷新 or 退出登录 runs."""
        return self._refreshing or self._signing_out

    def _status(self, summary: AccountSummary | None) -> tuple[str, str]:
        if summary is None:
            return "", ""
        site = summary.provider.name
        if self._error:
            return self._error, TONE_DANGER
        if self._refreshing:
            return self.tr("正在同步…"), ""
        if summary.state == ProviderState.SIGN_IN_REQUIRED:
            return self.tr("需要重新登录 · {site}").format(site=site), TONE_WARNING
        if summary.state == ProviderState.ACCOUNT_DISABLED:
            return self.tr("账户已停用 · {site}").format(site=site), TONE_DANGER
        if summary.state == ProviderState.CREDENTIALS_UNAVAILABLE:
            error = ProfileError("credentials_unavailable", "")
            return failure_text(error), TONE_WARNING
        if summary.error and summary.synced_at is not None:
            # Offline, or the provider failed since: what shows is what it said then.
            ago = fmt.ago(summary.synced_at, self._clock())
            return self.tr("上次同步 {ago}").format(ago=ago), ""
        if summary.error:
            return self.tr("未能同步 · {site}").format(site=site), TONE_WARNING
        account = summary.provider.account
        return self.tr("{account} · {site}").format(account=account, site=site), ""

    # -- 当前订阅 ------------------------------------------------------------------------------
    @Property(bool, notify=changed)
    def needsSignIn(self) -> bool:
        """The account needs signing in again: 当前订阅 starts with 重新登录…."""
        return self._signed_out(self._summary())

    @Property(str, notify=changed)
    def signInText(self) -> str:
        return self._view.sign_in

    @Property(bool, notify=changed)
    def signInDanger(self) -> bool:
        return self._view.sign_in_danger

    @Slot()
    def signInAgain(self) -> None:
        """重新登录…: the sign-in dialog, on the provider's address."""
        summary = self._summary()
        if summary is not None:
            self.signInRequested.emit(summary.provider.origin)

    @Property(bool, notify=changed)
    def hasSubscriptions(self) -> bool:
        summary = self._summary()
        return summary is not None and bool(summary.subscriptions)

    def _current(self, summary: AccountSummary, now: float) -> RemoteSubscription | None:
        """The subscription 当前订阅 shows: the one in use, else the one with the most left
        (docs/accounts.md §7.4), else, when none can be used, the one that runs out last,
        which is the one to renew."""
        subscriptions = summary.subscriptions
        if not subscriptions:
            return None
        found = next((s for s in subscriptions if s.id == summary.active), None)
        if found is not None:
            return found
        best = most_left(subscriptions, now)
        if best is not None:
            return best
        return max(
            subscriptions,
            key=lambda s: (math.inf if s.expire_at is None else s.expire_at, -s.id),
        )

    def _name(self, summary: AccountSummary, subscription: RemoteSubscription) -> str:
        name = self._names.get((summary.provider.id, subscription.id))
        return name or names(summary.subscriptions)[subscription.id]

    def _condition(
        self, summary: AccountSummary, subscription: RemoteSubscription, now: float
    ) -> tuple[str, str]:
        """A subscription's status and its tone: 有效, or what billing says of it
        (docs/accounts.md §5.5), or why its config is kept; 试用 before it."""
        if summary.notices.get(subscription.id) == PROVIDER_LINK_UNAVAILABLE:
            text, tone = self.tr("没有订阅链接"), TONE_WARNING
        else:
            status = condition_status(subscription, now)
            text = status.text or self.tr("有效")
            tone = TONE_DANGER if status.danger else TONE_WARNING if status.text else ""
        if subscription.trial:
            text = self.tr("试用 · {status}").format(status=text)
        return text, tone

    def _expiry(self, subscription: RemoteSubscription) -> str:
        if subscription.expire_at is None:
            return self.tr("长期有效")
        return fmt.date(subscription.expire_at)

    def _reset(self, subscription: RemoteSubscription) -> str:
        if subscription.reset_mode == RESET_NONE:
            return self.tr("不重置")
        when = next_reset(subscription)
        return fmt.date(when) if when is not None else "—"

    def _devices(self, subscription: RemoteSubscription) -> str:
        online = subscription.online_devices
        if not subscription.device_limit:
            return self.tr("%n 台在线 · 不限台数", "", online)
        return self.tr("{online} / {limit} 台在线").format(
            online=online, limit=subscription.device_limit
        )

    @Property(str, notify=changed)
    def currentName(self) -> str:
        return self._view.current_name

    @Property(str, notify=changed)
    def currentStatus(self) -> str:
        return self._view.current_status

    @Property(str, notify=changed)
    def currentTone(self) -> str:
        return self._view.current_tone

    @Property(str, notify=changed)
    def currentUsage(self) -> str:
        """本期流量: ``12.3 / 100 GB``, or what was used of an unlimited one."""
        return self._view.usage

    @Property(bool, notify=changed)
    def currentHasQuota(self) -> bool:
        return self._view.has_quota

    @Property(float, notify=changed)
    def currentUsedFraction(self) -> float:
        return self._view.used_fraction

    @Property(str, notify=changed)
    def currentReset(self) -> str:
        """下次重置: the date the traffic starts over, 不重置, or — when billing does not say."""
        return self._view.reset

    @Property(str, notify=changed)
    def currentExpiry(self) -> str:
        return self._view.expiry

    @Property(str, notify=changed)
    def currentDevices(self) -> str:
        """设备: 「2 / 3 台在线」."""
        return self._view.devices

    @Property(bool, notify=changed)
    def autoRenew(self) -> bool:
        return self._view.auto_renew

    @Property(bool, notify=changed)
    def canSetAutoRenew(self) -> bool:
        """The switch works while the account is signed in and no change of it is on its way."""
        return self._can_set_auto_renew()

    def _can_set_auto_renew(self) -> bool:
        summary = self._summary()
        return (
            self._view.current is not None
            and summary is not None
            and summary.state == ProviderState.SIGNED_IN
            and self._renewing is None
            and not self._signing_out
        )

    @Property(str, notify=changed)
    def autoRenewError(self) -> str:
        """Why the last change of auto-renew failed, under its switch in danger."""
        return failure_text(self._renew_error) if self._renew_error is not None else ""

    @Slot(bool)
    def setAutoRenew(self, enabled: bool) -> None:
        """自动续费, the one thing the app changes in billing (D58). The switch shows the
        change at once and goes back if the provider refuses it."""
        summary = self._summary()
        current = self._view.current
        if summary is None or current is None or not self._can_set_auto_renew():
            return
        provider_id = summary.provider.id
        self._renewing, self._renew_error = enabled, None
        self.changed.emit()

        def done(_: RemoteSubscription) -> None:
            self._renewing = None
            self.reload()  # the stored billing has it now

        def failed(error: BaseException) -> None:
            self._renewing, self._renew_error = None, error
            self.changed.emit()
            self.reload()  # a refused session leaves the account signed out

        change = self._accounts.set_auto_renew(provider_id, current.id, enabled)
        self._runner.submit(change, done, failed)

    # -- the browser -------------------------------------------------------------------------
    def _open(self, page: Callable[[str], str]) -> None:
        """Open a page of the user center (docs/accounts.md §6.2) in the browser; the account
        is watched, so that what was done there shows as the window comes back."""
        summary = self._summary()
        if summary is None:
            return
        try:
            address = page(summary.provider.origin)
        except (ProviderError, ValueError) as error:
            self._error = failure_text(error)
            self.changed.emit()
            return
        QDesktopServices.openUrl(QUrl(address))
        self._watch_until = self._clock() + WATCH_S
        # Signed out meanwhile, there is nothing left to watch.
        self._runner.submit(self._accounts.watch(summary.provider.id), None, lambda _error: None)

    @Slot()
    def openUserCenter(self) -> None:
        """在用户中心打开."""
        self._open(web.home)

    @Slot()
    def renew(self) -> None:
        """续费 of 当前订阅."""
        if (current := self._view.current) is not None:
            self.renewSubscription(current.id)

    @Slot(int)
    def renewSubscription(self, remote_id: int) -> None:
        """续费 of a subscription that cannot be used until it is renewed."""
        self._open(lambda origin: web.renew(origin, remote_id))

    @Slot()
    def openPlans(self) -> None:
        """更换套餐, 选购套餐."""
        self._open(web.plans)

    @Slot(str)
    def buyPlan(self, plan_id: str) -> None:
        """选购: the plan's checkout, for its first period."""
        plan = next((p for p in self._plans.value or () if p.id == plan_id), None)
        period = plan.prices[0].period if plan is not None and plan.prices else None
        self._open(lambda origin: web.checkout(origin, plan_id, period))

    @Slot()
    def openWallet(self) -> None:
        """充值, 明细."""
        self._open(web.wallet)

    @Slot()
    def openOrders(self) -> None:
        """去支付."""
        self._open(web.orders)

    @Slot()
    def openSecurity(self) -> None:
        """密码与两步验证."""
        self._open(web.security)

    @Slot()
    def openDevices(self) -> None:
        """登录设备."""
        self._open(web.devices)

    # -- actions -----------------------------------------------------------------------------
    def _act(
        self,
        action: Coroutine[Any, Any, Any],
        done: Callable[[Any], None],
        settle: Callable[[], None] = lambda: None,
    ) -> None:
        """Run an action of the page; its failure takes the header's status."""
        self._error = ""
        self.changed.emit()

        def succeeded(result: Any) -> None:
            settle()
            done(result)

        def failed(error: BaseException) -> None:
            settle()
            if getattr(error, "code", "") not in _SIGNS_OUT:
                self._error = failure_text(error)
            self.changed.emit()
            self.reload()

        self._runner.submit(action, succeeded, failed)

    @Slot()
    def refresh(self) -> None:
        """刷新: sync the account now, then read the wallet, plans and orders again."""
        summary = self._summary()
        if summary is None or self._refreshing:
            return
        self._refreshing = True

        def settle() -> None:
            self._refreshing = False

        def done(update: Updated) -> None:
            self._synced(update)
            self._read_extras()

        self._act(self._accounts.sync(summary.provider.id), done, settle)

    @Slot(int)
    def use(self, remote_id: int) -> None:
        """使用: that subscription's rules, its config downloaded first when it has none; a
        running tunnel reloads (Accounts.use)."""
        summary = self._summary()
        if summary is None:
            return

        def done(_: ProfileList) -> None:
            self.profilesChanged.emit()
            self.applied.emit()
            self.reload()

        self._act(self._accounts.use(summary.provider.id, remote_id), done)

    # -- 退出登录 ------------------------------------------------------------------------------
    @Slot()
    def requestSignOut(self) -> None:
        """退出登录: work out what it removes, then ask the view to confirm it
        (docs/accounts.md §4.4)."""
        summary = self._summary()
        if summary is None or self._signing_out:
            return

        def ready(removal: ProviderRemoval) -> None:
            self._sign_out = removal
            self._sign_out_pending = True
            self.changed.emit()
            self.signOutRequested.emit()

        self._act(self._accounts.sign_out_removal(summary.provider.id), ready)

    @Property(str, notify=changed)
    def signOutTitle(self) -> str:
        if self._sign_out is None:
            return ""
        return self.tr("退出「{name}」？").format(name=self._sign_out.provider.name)

    @Property(str, notify=changed)
    def signOutText(self) -> str:
        return sign_out_text(self._sign_out) if self._sign_out is not None else ""

    @Slot()
    def confirmSignOut(self) -> None:
        """Sign out: the provider's subscriptions go, whose rules come next takes over, and
        the page shows the next account, or goes when it was the last."""
        if self._sign_out is None or not self._sign_out_pending:
            return
        self._sign_out_pending = False
        self._signing_out = True

        def settle() -> None:
            self._signing_out = False

        def done(_: ProfileList) -> None:
            self.profilesChanged.emit()
            self.applied.emit()
            self.reload()

        self._act(self._accounts.sign_out(self._sign_out.provider.id), done, settle)

    @Slot()
    def cancelSignOut(self) -> None:
        self._sign_out_pending = False

    # -- 此账户的订阅 --------------------------------------------------------------------------
    @Property(QObject, constant=True)
    def subscriptions(self) -> KeyedListModel:
        """The account's subscriptions as SubscriptionRow items, by id."""
        return self._subscriptions

    @Property(int, notify=changed)
    def subscriptionCount(self) -> int:
        return len(self._subscriptions.rows)

    def _subscription_rows(self, summary: AccountSummary, now: float) -> list[SubscriptionRow]:
        rows = []
        for subscription in summary.subscriptions:
            status, tone = self._condition(summary, subscription, now)
            in_use = subscription.id == summary.active
            if in_use:
                action = ""
            elif not blocked(subscription, now):
                action = ACTION_USE
            elif subscription.condition(now) in (CONDITION_EXPIRED, CONDITION_TRAFFIC_USED_UP):
                action = ACTION_RENEW
            else:
                action = ""  # stopped by the provider, or not started: nothing to do here
            if subscription.expire_at is None:
                expiry = self.tr("长期有效")
            else:
                expiry = self.tr("{date} 到期").format(date=fmt.date(subscription.expire_at))
            rows.append(
                SubscriptionRow(
                    remoteId=subscription.id,
                    name=self._name(summary, subscription),
                    status=status,
                    tone=tone,
                    expiry=expiry,
                    inUse=in_use,
                    action=action,
                )
            )
        return rows

    # -- 钱包, 套餐, 订单 -----------------------------------------------------------------------
    def _read_extras(self) -> None:
        """Read the wallet, the plans and the latest orders of the account shown; a reply for
        another account, or an earlier read, is dropped. What was read for this account stays
        until the new answers come."""
        summary = self._summary()
        key = self._extras_key()
        if key != self._extras_for:
            self._wallet = _Read[Wallet]()
            self._plans = _Read[tuple[PublicPlan, ...]]()
            self._orders = _Read[tuple[OrderSummary, ...]]()
        self._extras_for = key
        self._extras += 1
        generation = self._extras
        self.changed.emit()
        if summary is None or self._signed_out(summary):
            return
        provider_id = summary.provider.id

        def current() -> bool:
            return generation == self._extras

        def reading[T](read: _Read[T], ask: Coroutine[Any, Any, T]) -> None:
            def done(value: T) -> None:
                read.value, read.error = value, None
                self.changed.emit()

            def failed(error: BaseException) -> None:
                read.error = error
                self.changed.emit()
                self.reload()  # a refused session leaves the account signed out

            self._runner.submit(ask, done, failed, is_current=current)

        reading(self._wallet, self._accounts.wallet(provider_id))
        reading(self._plans, self._accounts.plans(provider_id))
        reading(self._orders, self._accounts.orders(provider_id, ORDERS))

    def _note[T](self, read: _Read[T], signed_out: bool) -> tuple[str, bool]:
        """What a group says instead of its rows: why, in danger when reading failed."""
        if signed_out:
            return self.tr("重新登录后显示"), False
        if read.error is not None and read.value is None:
            return failure_text(read.error), True
        if read.value is None:
            return self.tr("正在读取…"), False
        return "", False

    @Property(str, notify=changed)
    def walletBalance(self) -> str:
        return self._view.balance

    @Property(str, notify=changed)
    def walletPoints(self) -> str:
        return self._view.points

    @Property(str, notify=changed)
    def walletLevel(self) -> str:
        """等级: 「白银 · 优惠 5%」, as the user center writes it."""
        return self._view.level

    @Property(str, notify=changed)
    def walletNote(self) -> str:
        return self._view.wallet_note

    @Property(bool, notify=changed)
    def walletNoteDanger(self) -> bool:
        return self._view.wallet_danger

    @Property(QObject, constant=True)
    def plans(self) -> KeyedListModel:
        """The plans on sale as PlanRow items."""
        return self._plan_rows

    @Property(str, notify=changed)
    def plansNote(self) -> str:
        return self._view.plans_note

    @Property(bool, notify=changed)
    def plansNoteDanger(self) -> bool:
        return self._view.plans_danger

    @Property(QObject, constant=True)
    def orders(self) -> KeyedListModel:
        """The latest orders as OrderRow items, newest first."""
        return self._order_rows

    @Property(str, notify=changed)
    def ordersNote(self) -> str:
        return self._view.orders_note

    @Property(bool, notify=changed)
    def ordersNoteDanger(self) -> bool:
        return self._view.orders_danger

    @Property(bool, notify=changed)
    def ordersPending(self) -> bool:
        """An order waits for payment: the amounts leave room for its 去支付, so that they
        line up (DESIGN.md "金额")."""
        return any(row.pending for row in self._order_rows.rows)

    def _level(self, wallet: Wallet) -> str:
        level = wallet.level
        if level is None:
            return "—"
        if not level.discount_percent:
            return level.name
        return self.tr("{level} · 优惠 {percent}%").format(
            level=level.name, percent=level.discount_percent
        )

    def _per(self, period: str) -> str:
        """What follows a price: its period, or nothing for a one-time price."""
        per = {
            PERIOD_MONTH: self.tr("/ 月"),
            PERIOD_QUARTER: self.tr("/ 季"),
            PERIOD_HALF_YEAR: self.tr("/ 半年"),
            PERIOD_YEAR: self.tr("/ 年"),
        }.get(period, "")
        return f" {per}" if per else ""

    def _plan_rows_of(
        self, plans: tuple[PublicPlan, ...], currency: Currency | None
    ) -> list[PlanRow]:
        rows = []
        for plan in plans:
            parts = []
            if plan.kind == PLAN_TRAFFIC_PACK:
                parts.append(self.tr("流量包"))
            if plan.traffic_bytes is None:
                parts.append(self.tr("不限流量"))
            elif plan.kind == PLAN_TRAFFIC_PACK or plan.reset_mode == RESET_NONE:
                parts.append(fmt.size(plan.traffic_bytes))
            else:
                parts.append(self.tr("每月 {size}").format(size=fmt.size(plan.traffic_bytes)))
            if plan.kind != PLAN_TRAFFIC_PACK:
                if plan.device_limit:
                    parts.append(self.tr("%n 台设备", "", plan.device_limit))
                else:
                    parts.append(self.tr("不限设备"))
            prices = [
                {"amount": fmt.money(p.amount, plan.currency, currency), "per": self._per(p.period)}
                for p in plan.prices
            ]
            text = " · ".join(price["amount"] + price["per"] for price in prices)
            rows.append(PlanRow(plan.id, plan.name, " · ".join(parts), prices, text, plan.sold_out))
        return rows

    def _kind(self, kind: str) -> str:
        return {
            ORDER_NEW: self.tr("新购"),
            ORDER_RENEW: self.tr("续费"),
            ORDER_CHANGE: self.tr("更换套餐"),
            ORDER_TRAFFIC_PACK: self.tr("流量包"),
            ORDER_TOPUP: self.tr("充值"),
        }.get(kind, "")

    def _period(self, period: str | None) -> str:
        return {
            PERIOD_MONTH: self.tr("月付"),
            PERIOD_QUARTER: self.tr("季付"),
            PERIOD_HALF_YEAR: self.tr("半年付"),
            PERIOD_YEAR: self.tr("年付"),
            PERIOD_ONE_TIME: self.tr("一次性"),
        }.get(period or "", "")

    def _order_rows_of(
        self, orders: tuple[OrderSummary, ...], currency: Currency | None, now: float
    ) -> list[OrderRow]:
        rows = []
        for order in orders:
            kind = self._kind(order.kind)
            if order.kind == ORDER_TOPUP or not order.plan_name:
                title, parts = kind or order.kind, []
            else:
                title, parts = order.plan_name, [kind]
            parts += [self._period(order.period), fmt.date(order.created_at)]
            # An unpaid order past its time is cancelled by the provider within minutes.
            timed_out = order.pending and order.expires_at is not None and order.expires_at <= now
            pending = order.pending and not timed_out
            status = {
                ORDER_PENDING: self.tr("已超时") if timed_out else self.tr("待支付"),
                ORDER_COMPLETED: self.tr("已完成"),
                ORDER_CANCELLED: self.tr("已取消"),
                ORDER_REFUNDED: self.tr("已退款"),
            }.get(order.status, order.status)
            # What is left to pay, waiting for payment; otherwise the order's total.
            amount = order.amount if pending else order.total
            rows.append(
                OrderRow(
                    orderId=order.id,
                    title=title,
                    detail=" · ".join(p for p in parts if p),
                    amount=fmt.money(amount, order.currency, currency),
                    status=status,
                    pending=pending,
                )
            )
        return rows

    # -- the view ----------------------------------------------------------------------------
    def _rebuild(self) -> None:
        """Make what the page shows from what is known now."""
        now = self._clock()
        summary = self._summary()
        view = _View()
        view.options = [
            {"value": s.provider.id, "label": s.provider.name, "note": s.provider.account}
            for s in self._summaries
        ]
        view.status, view.tone = self._status(summary)
        signed_out = self._signed_out(summary)
        if summary is not None and signed_out:
            disabled = summary.state == ProviderState.ACCOUNT_DISABLED
            view.sign_in = self.tr("账户已停用") if disabled else self.tr("需要重新登录")
            view.sign_in_danger = disabled
        current = self._current(summary, now) if summary is not None else None
        if summary is not None and current is not None:
            view.current = current
            view.current_name = self._name(summary, current)
            view.current_status, view.current_tone = self._condition(summary, current, now)
            if current.total:
                view.usage = fmt.usage(current.used, current.total)
                view.has_quota = True
                view.used_fraction = min(current.used / current.total, 1.0)
            else:
                view.usage = self.tr("{used} · 不限流量").format(used=fmt.size(current.used))
            view.reset = self._reset(current)
            view.expiry = self._expiry(current)
            view.devices = self._devices(current)
            view.auto_renew = current.auto_renew if self._renewing is None else self._renewing
        site = summary.site if summary is not None else None
        currency = site.currency if site is not None else None
        view.wallet_note, view.wallet_danger = self._note(self._wallet, signed_out)
        if (wallet := self._wallet.value) is not None and not signed_out:
            view.balance = fmt.money(wallet.balance, wallet.currency, currency)
            view.points = f"{wallet.points:,}"
            view.level = self._level(wallet)
        view.plans_note, view.plans_danger = self._note(self._plans, signed_out)
        plans = self._plans.value if not signed_out else None
        if plans is not None and not plans:
            view.plans_note = self.tr("暂无在售的套餐")
        view.orders_note, view.orders_danger = self._note(self._orders, signed_out)
        orders = self._orders.value if not signed_out else None
        if orders is not None and not orders:
            view.orders_note = self.tr("没有订单")
        self._view = view
        rows = self._subscription_rows(summary, now) if summary is not None else []
        self._subscriptions.sync(rows)
        self._plan_rows.sync(self._plan_rows_of(plans or (), currency))
        self._order_rows.sync(self._order_rows_of(orders or (), currency, now))
