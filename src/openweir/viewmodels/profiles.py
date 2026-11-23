"""Profiles ViewModel: the subscription list (docs/routing.md §4.2), imports, updates, the node
pool, and the profile in use that the overview shows. Subscriptions of provider accounts are
rows like the others, with what the account says of them (docs/accounts.md §7.2, §7.5)."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import Property, QCoreApplication, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtQml import QmlElement, QmlUncreatable

from openweir.models import format as fmt
from openweir.models.library import (
    DEFAULT_AUTO_UPDATE_HOURS,
    ProfileLibrary,
    Removal,
    Routing,
    Updated,
)
from openweir.models.profiles import Profile, ProfileList, Usage
from openweir.models.provider import web
from openweir.models.provider.sync import blocked, has_config, most_left
from openweir.models.provider.types import (
    CONDITION_DISABLED,
    CONDITION_EXPIRED,
    CONDITION_EXPIRING,
    CONDITION_NOT_STARTED,
    CONDITION_TRAFFIC_LOW,
    CONDITION_TRAFFIC_USED_UP,
    CONDITION_UNUSABLE,
    KEEPS_CONFIG,
    PROVIDER_LINK_UNAVAILABLE,
    PROVIDER_SUBSCRIPTION_DISABLED,
    PROVIDER_SUBSCRIPTION_EXPIRED,
    PROVIDER_SUBSCRIPTION_MISSING,
    PROVIDER_SUBSCRIPTION_UNUSABLE,
    AccountSummary,
    ProviderError,
    ProviderRemoval,
    ProviderState,
    RemoteSubscription,
)
from openweir.models.routing import MODE_PLAN, MODE_PROFILE
from openweir.models.subscription import web_link
from openweir.viewmodels.common.async_runner import AsyncRunner
from openweir.viewmodels.common.keyed_list import KeyedListModel
from openweir.viewmodels.feedback import failure_text

QML_IMPORT_NAME = "OpenWeir"
QML_IMPORT_MAJOR_VERSION = 1

# Checks which subscriptions are due and keeps "2 小时前" current.
TICK_MS = 60_000
# The first check waits this long after start, so the window shows and a connection at login
# starts before the subscriptions and the provider accounts reach the network
# (docs/accounts.md §5.3).
FIRST_CHECK_MS = 5_000
# The account states in which nothing can be done for it until it signs in again.
_SIGNED_OUT = (ProviderState.SIGN_IN_REQUIRED, ProviderState.ACCOUNT_DISABLED)


@dataclass(frozen=True)
class ProfileRow:
    """One row of the profile table, as text."""

    id: str
    name: str
    source: str
    active: bool
    usage: str
    hasQuota: bool
    usedFraction: float
    expiry: str
    expired: bool
    updated: str
    updateFailed: bool
    updating: bool
    refreshLabel: str
    isSubscription: bool
    canUpdate: bool
    autoUpdate: bool
    autoUpdateLabel: str
    hasOverride: bool
    note: str  # 规则使用中 or 不加入节点池, after the source in the caption
    warning: str  # 更新失败, in the warning colour (DESIGN.md "分组与行"); empty otherwise
    # What the core left out reading it (D54), after the note in the warning colour.
    leftOut: str
    inPool: bool
    hasRules: bool  # it brings rules, or has not been read yet
    canAutoUpdate: bool
    # A provider account's subscription (docs/accounts.md §7.2); the rest are for those.
    isProvider: bool = False
    # What the account says of it, at the end of the caption: warning, or danger when the
    # provider stopped it (DESIGN.md "服务商的订阅行"). It takes the place of 更新失败.
    status: str = ""
    statusDanger: bool = False
    canRemove: bool = True  # 删除: any but a provider's, unless the account no longer has it
    canRenew: bool = False  # 在网页上续费
    needsSignIn: bool = False  # 重新登录…
    providerName: str = ""  # the site, for 退出「示例服务商」

    @property
    def profileId(self) -> str:  # QML reserves ``id``
        return self.id


ROLES = (*(f.name for f in fields(ProfileRow) if f.name != "id"), "profileId")


def _origin(profile: Profile) -> str:
    """Where the profile comes from: the subscription's host or the file's name."""
    if profile.is_subscription:
        return urlsplit(profile.url).hostname or profile.url
    return Path(profile.path).name if profile.path else ""


# Module-level text is translated in the ViewModel's context, as self.tr() would.
def _source(profile: Profile) -> str:
    if profile.is_subscription:
        kind = QCoreApplication.translate("ProfilesViewModel", "订阅")
    elif profile.path:
        kind = QCoreApplication.translate("ProfilesViewModel", "本地文件")
    else:
        kind = QCoreApplication.translate("ProfilesViewModel", "节点链接")
    parts = [kind, _origin(profile)]
    if profile.proxy_count:
        count = profile.proxy_count
        parts.append(QCoreApplication.translate("ProfilesViewModel", "%n 个节点", None, count))
    if profile.has_override:
        parts.append(QCoreApplication.translate("ProfilesViewModel", "已覆写"))
    return " · ".join(part for part in parts if part)


def _usage(usage: Usage | None) -> tuple[str, float | None]:
    """The usage text, and the used fraction when the subscription has a quota."""
    if usage is None:
        return "—", None
    if not usage.total:
        return fmt.size(usage.used), None
    return fmt.usage(usage.used, usage.total), min(usage.used / usage.total, 1.0)


@dataclass(frozen=True)
class ProviderStatus:
    """What a provider subscription's row says of it after its caption (DESIGN.md "服务商的
    订阅行"), and what the overview offers with it."""

    text: str = ""
    danger: bool = False  # the provider stopped it: danger, not warning
    renew: bool = False  # it runs out, or ran out: the overview offers 续费


def _expires_in(seconds: float) -> str:
    """「3 天后到期」, in hours on the last day."""
    if seconds >= 86400:
        days = math.ceil(seconds / 86400)
        return QCoreApplication.translate("ProfilesViewModel", "%n 天后到期", None, days)
    hours = max(1, math.ceil(seconds / 3600))
    return QCoreApplication.translate("ProfilesViewModel", "%n 小时后到期", None, hours)


def condition_status(subscription: RemoteSubscription, now: float) -> ProviderStatus:
    """The status billing gives (docs/accounts.md §5.5); none while it is active. The
    account page words its subscriptions with it too."""
    condition = subscription.condition(now)
    if condition == CONDITION_EXPIRING:
        assert subscription.expire_at is not None
        return ProviderStatus(_expires_in(subscription.expire_at - now), renew=True)
    if condition == CONDITION_TRAFFIC_LOW:
        assert subscription.total
        percent = min(100, subscription.used * 100 // subscription.total)
        text = QCoreApplication.translate("ProfilesViewModel", "流量已用 {percent}%")
        return ProviderStatus(text.format(percent=percent))
    if condition == CONDITION_EXPIRED:
        return ProviderStatus(_expired(), renew=True)
    if condition == CONDITION_TRAFFIC_USED_UP:
        return ProviderStatus(QCoreApplication.translate("ProfilesViewModel", "流量已用完"))
    if condition == CONDITION_NOT_STARTED:
        return ProviderStatus(QCoreApplication.translate("ProfilesViewModel", "未开始"))
    if condition == CONDITION_DISABLED:
        return ProviderStatus(_stopped(), danger=True)
    if condition == CONDITION_UNUSABLE:
        return ProviderStatus(_unusable())
    return ProviderStatus()


def _expired() -> str:
    return QCoreApplication.translate("ProfilesViewModel", "已到期")


def _stopped() -> str:
    return QCoreApplication.translate("ProfilesViewModel", "已停用")


def _unusable() -> str:
    return QCoreApplication.translate("ProfilesViewModel", "暂不可用")


def provider_status(profile: Profile, summary: AccountSummary, now: float) -> ProviderStatus:
    """The one status a provider subscription's row shows: the account's comes first, then
    why its last update kept its config, then what billing says (docs/accounts.md §5.4, §5.5).
    These take the place of 更新失败: keeping the last config is what is meant to happen."""
    if summary.state == ProviderState.SIGN_IN_REQUIRED:
        return ProviderStatus(QCoreApplication.translate("ProfilesViewModel", "需要重新登录"))
    if summary.state == ProviderState.ACCOUNT_DISABLED:
        text = QCoreApplication.translate("ProfilesViewModel", "账户已停用")
        return ProviderStatus(text, danger=True)
    notice = summary.notices.get(profile.remote_id or 0, "")
    if notice == PROVIDER_SUBSCRIPTION_MISSING:
        return ProviderStatus(QCoreApplication.translate("ProfilesViewModel", "已不在账户中"))
    if notice == PROVIDER_LINK_UNAVAILABLE:
        return ProviderStatus(QCoreApplication.translate("ProfilesViewModel", "没有订阅链接"))
    subscription = _billing(profile, summary)
    status = condition_status(subscription, now) if subscription is not None else ProviderStatus()
    if status.text or not notice:
        return status
    # Billing changed since the update that kept the config; the code still says why.
    if notice == PROVIDER_SUBSCRIPTION_EXPIRED:
        return ProviderStatus(_expired(), renew=True)
    if notice == PROVIDER_SUBSCRIPTION_DISABLED:
        return ProviderStatus(_stopped(), danger=True)
    if notice == PROVIDER_SUBSCRIPTION_UNUSABLE:
        return ProviderStatus(_unusable())
    return status


def _billing(profile: Profile, summary: AccountSummary | None) -> RemoteSubscription | None:
    """What the account's last sync said of the provider subscription; None without it."""
    if summary is None:
        return None
    return next((s for s in summary.subscriptions if s.id == profile.remote_id), None)


def _removal_text(removal: Removal) -> str:
    """The consequences of a removal, stated in its confirmation (routing.md §5.1)."""
    if removal.profile.has_override:
        deleted = QCoreApplication.translate(
            "ProfilesViewModel", "订阅和它的覆写会从本机删除，无法恢复。"
        )
    else:
        deleted = QCoreApplication.translate("ProfilesViewModel", "订阅会从本机删除，无法恢复。")
    # Whole sentences, so that a translation can put a space between them.
    if removal.active and removal.successor is not None:
        text = QCoreApplication.translate(
            "ProfilesViewModel",
            "这是正在使用规则的订阅，删除后改用「{successor}」的规则。{deleted}",
        )
        return text.format(successor=removal.successor.name, deleted=deleted)
    if removal.active:
        text = QCoreApplication.translate(
            "ProfilesViewModel", "这是正在使用规则的订阅，删除后改用我的方案。{deleted}"
        )
        return text.format(deleted=deleted)
    if removal.pool_nodes:
        text = QCoreApplication.translate(
            "ProfilesViewModel", "节点池会少 %n 个节点。{deleted}", None, removal.pool_nodes
        )
        return text.format(deleted=deleted)
    return deleted


def _expiry(usage: Usage | None, now: float) -> tuple[str, bool]:
    """The expiry date, and whether it has passed."""
    if usage is None or not usage.expire:
        return "—", False
    if usage.expire <= now:
        return QCoreApplication.translate("ProfilesViewModel", "已过期"), True
    return fmt.date(usage.expire), False


def sign_out_text(removal: ProviderRemoval) -> str:
    """What signing out of a provider removes, stated in its confirmation (docs/accounts.md
    §4.4): its subscriptions by name, and whose rules come next. The subscriptions page and
    the account page ask the same."""
    names = [p.name for p in removal.profiles]
    quoted = QCoreApplication.translate("ProfilesViewModel", "「{name}」")
    separator = QCoreApplication.translate("ProfilesViewModel", "、")
    listed = separator.join(quoted.format(name=name) for name in names[:3])
    if len(names) > 3:
        text = QCoreApplication.translate(
            "ProfilesViewModel", "{names}等 %n 份订阅", None, len(names)
        )
        listed = text.format(names=listed)
    if not names:
        removed = QCoreApplication.translate("ProfilesViewModel", "这个账户在本机没有订阅。")
    elif any(p.has_override for p in removal.profiles):
        text = QCoreApplication.translate("ProfilesViewModel", "{listed}和它们的覆写会从本机删除。")
        removed = text.format(listed=listed)
    else:
        text = QCoreApplication.translate("ProfilesViewModel", "{listed}会从本机删除。")
        removed = text.format(listed=listed)
    # Whole sentences, so that a translation can put a space between them.
    if removal.active and removal.successor is not None:
        text = QCoreApplication.translate(
            "ProfilesViewModel",
            "{removed}之后改用「{successor}」的规则，服务商那边的订阅不受影响。",
        )
        return text.format(removed=removed, successor=removal.successor.name)
    if removal.active:
        text = QCoreApplication.translate(
            "ProfilesViewModel", "{removed}之后改用我的方案，服务商那边的订阅不受影响。"
        )
        return text.format(removed=removed)
    text = QCoreApplication.translate("ProfilesViewModel", "{removed}服务商那边的订阅不受影响。")
    return text.format(removed=removed)


@QmlElement
@QmlUncreatable("ProfilesViewModel is created in Python and injected into the view.")  # pyright: ignore[reportCallIssue]
class ProfilesViewModel(QObject):
    changed = Signal()
    applied = Signal()  # the tunnel may run another config now; groups need a refresh
    imported = Signal()  # an import succeeded; the view closes its form
    removalRequested = Signal()  # a removal awaits confirmation; the view asks
    overrideOpened = Signal()  # an override was read; the view opens its editor
    overrideSaved = Signal()  # the host accepted the override and it is stored
    routingChanged = Signal()  # the pool or the profile in use may have changed
    routingRequested = Signal()  # 使用它的规则: the view goes to the routing page
    importPrefilled = Signal()  # an install link filled in the import form; the view opens it
    editOpened = Signal()
    editSaved = Signal()
    # 登录服务商…, 重新登录…: the sign-in dialog opens on this address, "" for none yet
    # (docs/accounts.md §7.3).
    providerSignInRequested = Signal(str)
    signOutRequested = Signal()  # 退出「示例服务商」 awaits confirmation; the view asks
    # The overview's 续费: the account page, on this provider (docs/accounts.md §7.5).
    accountRequested = Signal(str)

    def __init__(
        self,
        library: ProfileLibrary,
        runner: AsyncRunner,
        clock: Callable[[], float] = time.time,
        parent: QObject | None = None,
        *,
        first_check_ms: int = FIRST_CHECK_MS,
    ) -> None:
        super().__init__(parent)
        self._library = library
        self._runner = runner
        self._clock = clock
        self._list: ProfileList = library.snapshot()
        self._routing: Routing = library.routing_snapshot()
        # Each provider as the accounts last said, by id; read whenever the list is.
        self._accounts: dict[str, AccountSummary] = {}
        self._syncing = False
        self._sign_out: ProviderRemoval | None = None  # the last one asked about
        self._sign_out_pending = False  # while the dialog closes
        self._updating: set[str] = set()
        self._importing = False
        self._import_error = ""
        self._prefill_url = ""
        self._prefill_name = ""
        self._edit_target = ""
        self._edit_saving = False
        self._edit_error = ""
        self._checking = False
        self._error = ""
        self._removal: Removal | None = None  # the last one asked about; its text stays
        self._removal_pending = False  # while the dialog closes
        self._override_target: str | None = None  # the profile id; None is the global one
        self._override_name = ""
        self._override_text = ""
        self._override_saving = False
        self._override_error = ""
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.checkUpdates)
        self._first_check = QTimer(self)
        self._first_check.setSingleShot(True)
        self._first_check.setInterval(first_check_ms)
        self._first_check.timeout.connect(self.checkUpdates)
        # Rows update in place, so the table keeps its delegates and the keyboard focus.
        self._model = KeyedListModel(ROLES, self)
        self.changed.connect(self._sync_rows)
        self._sync_rows()

    def start(self) -> None:
        self._read_accounts()
        self._first_check.start()
        self._timer.start()

    def stop(self) -> None:
        self._first_check.stop()
        self._timer.stop()

    def retranslate(self) -> None:
        """Re-read the text properties in the language just installed. A provider names its
        plans in the language it is asked in, so the accounts sync again (docs/accounts.md
        §5.3)."""
        self.changed.emit()
        self._sync_accounts(max_age=0)

    def _loaded(self, profiles: ProfileList) -> None:
        self._list = profiles
        self.changed.emit()
        self._read_accounts()

    def _has_providers(self) -> bool:
        return any(p.is_provider for p in self._list.profiles)

    def _read_accounts(self) -> None:
        """Read what the accounts say of the providers the rows show; it takes no request."""
        if not self._has_providers():
            if self._accounts:
                self._accounts = {}
                self.changed.emit()
            return

        def read(summaries: tuple[AccountSummary, ...]) -> None:
            self._accounts = {s.provider.id: s for s in summaries}
            self.changed.emit()

        self._runner.submit(self._library.accounts.summaries(), read)

    def _sync_accounts(self, *, max_age: float | None = None) -> None:
        """Sync the providers that are due (Accounts.sync_all): those synced ``max_age``
        seconds ago or more, or watched since their user center was opened; then show what
        changed. Nothing while no row is a provider's, or while the update check runs, which
        syncs them first."""
        if self._syncing or self._checking or not self._has_providers():
            return
        self._syncing = True
        accounts = self._library.accounts
        sync = accounts.sync_all() if max_age is None else accounts.sync_all(max_age=max_age)

        def synced(update: Updated) -> None:
            self._syncing = False
            self._updated(update)

        def failed(error: BaseException) -> None:
            self._syncing = False
            self._failed(error)

        self._runner.submit(sync, synced, failed)

    @Slot()
    def pageShown(self) -> None:
        """The page shows: sync the providers not synced in the last minute."""
        self._sync_accounts(max_age=60)

    @Slot()
    def windowActivated(self) -> None:
        """The window became active again, maybe back from the user center in the browser
        (D58): sync the providers watched since it was opened."""
        self._sync_accounts()

    def _run(
        self,
        action: Coroutine[Any, Any, ProfileList],
        done: Callable[[], None] | None = None,
        *,
        reloads: bool = False,
    ) -> None:
        """Submit a library action; its result replaces the list, its failure the status."""
        self._error = ""
        self.changed.emit()

        def succeeded(profiles: ProfileList) -> None:
            if done is not None:
                done()
            self._error = ""
            self._loaded(profiles)
            self._routing_touched()
            if reloads:
                self.applied.emit()

        def failed(error: BaseException) -> None:
            if done is not None:
                done()
            self._failed(error)

        self._runner.submit(action, succeeded, failed)

    def _failed(self, error: BaseException) -> None:
        self._error = failure_text(error)
        self.changed.emit()
        # A failed update still marks its profile; fetch the list that says so.
        self._runner.submit(self._library.current(), self._loaded)

    def _updated(self, update: Updated) -> None:
        """Show what an update brought. A new text changes what the routing page shows of
        the profile; one in use reached the tunnel, whose groups and rules the pages read
        again."""
        self._loaded(update.profiles)
        if update.changed:
            self._routing_touched()
        if update.applied:
            self.applied.emit()

    def _routing_touched(self) -> None:
        """Read the routing again after a change to the profiles, and let the routing page
        know."""
        self._runner.submit(self._library.routing(), self._routing_loaded)
        self.routingChanged.emit()

    def _routing_loaded(self, routing: Routing) -> None:
        self._routing = routing
        self.changed.emit()

    @Slot()
    def reload(self) -> None:
        """Take up what the routing page changed."""

        async def read() -> tuple[ProfileList, Routing]:
            return await self._library.current(), await self._library.routing()

        def done(result: tuple[ProfileList, Routing]) -> None:
            self._list, self._routing = result
            self.changed.emit()
            self._read_accounts()

        self._runner.submit(read(), done)

    @Slot()
    def inspect(self) -> None:
        """Read the profiles not read yet, to know which bring rules."""
        self._runner.submit(self._library.inspect_all(), self._routing_loaded)

    # -- list --------------------------------------------------------------------------------
    @Property(str, notify=changed)
    def summary(self) -> str:
        if self._error:
            return self._error
        count = self.tr("%n 份订阅", "", len(self._list.profiles))
        nodes = self._pool_nodes()
        if self._routing.mode != MODE_PLAN or nodes is None:
            return count
        pool = self.tr("节点池 %n 个节点", "", nodes)
        return self.tr("{count} · {pool}").format(count=count, pool=pool)

    def _pool_nodes(self) -> int | None:
        report = self._routing.report
        if report is not None:
            return len(report.nodes)
        total = 0
        for profile in self._list.profiles:
            if not profile.in_pool:
                continue
            inspection = self._routing.inspections.get(profile.id)
            if inspection is None:
                return None
            total += inspection.node_count
        return total

    @Property(bool, notify=changed)
    def hasError(self) -> bool:
        return bool(self._error)

    @Property(QObject, constant=True)
    def model(self) -> KeyedListModel:
        """The profiles as ProfileRow items."""
        return self._model

    def _sync_rows(self) -> None:
        now = self._clock()
        self._model.sync([self._row(profile, now) for profile in self._list.profiles])

    def _row(self, profile: Profile, now: float) -> ProfileRow:
        summary = self._accounts.get(profile.provider_id or "") if profile.is_provider else None
        billing = _billing(profile, summary)
        # A provider's subscription shows billing's usage and expiry (DESIGN.md "服务商的订阅行").
        usage_of = billing.usage if billing is not None else profile.usage
        usage, fraction = _usage(usage_of)
        expiry, expired = _expiry(usage_of, now)
        updating = profile.id in self._updating
        if updating:
            updated = self.tr("正在更新")
        elif profile.id in self._list.update_failed:
            updated = self.tr("更新失败")
        elif not has_config(profile):
            updated = "—"  # a provider's subscription waiting for its first config
        else:
            updated = fmt.ago(profile.updated_at, now)
        account = self._provider_row(profile, summary, now) if profile.is_provider else {}
        return ProfileRow(
            id=profile.id,
            name=profile.name,
            source=self._source(profile, summary, billing),
            active=profile.id == self._list.active,
            usage=usage,
            hasQuota=fraction is not None,
            usedFraction=fraction or 0.0,
            expiry=expiry,
            expired=expired,
            updated=updated,
            updateFailed=profile.id in self._list.update_failed and not updating,
            updating=updating,
            refreshLabel=self._refresh_label(profile),
            isSubscription=profile.is_subscription,
            canUpdate=profile.can_update,
            autoUpdate=profile.auto_update_hours > 0,
            autoUpdateLabel=self.tr(
                "每 %n 小时自动更新", "", profile.auto_update_interval or DEFAULT_AUTO_UPDATE_HOURS
            ),
            hasOverride=profile.has_override,
            note=self._note(profile),
            leftOut=self._left_out(profile),
            warning=self.tr("更新失败")
            if profile.id in self._list.update_failed and not updating
            else "",
            inPool=profile.in_pool,
            hasRules=self._routing.has_rules(profile) is not False,
            canAutoUpdate=profile.can_auto_update,
            **account,
        )

    def _source(
        self,
        profile: Profile,
        summary: AccountSummary | None,
        billing: RemoteSubscription | None,
    ) -> str:
        """Where it comes from: a provider's subscription names the site and the account,
        after 试用 for a trial (DESIGN.md "服务商的订阅行")."""
        if not profile.is_provider:
            return _source(profile)
        parts = []
        if billing is not None and billing.trial:
            parts.append(self.tr("试用"))
        if summary is None:
            parts.append(self.tr("服务商"))  # until the accounts are read
        else:
            parts += [summary.provider.name, summary.provider.account]
        if profile.proxy_count:
            parts.append(self.tr("%n 个节点", "", profile.proxy_count))
        if profile.has_override:
            parts.append(self.tr("已覆写"))
        return " · ".join(part for part in parts if part)

    def _provider_row(
        self, profile: Profile, summary: AccountSummary | None, now: float
    ) -> dict[str, Any]:
        """What a provider's subscription adds to its row: its status and its menu."""
        if summary is None:
            return {"isProvider": True, "canRemove": False}
        status = provider_status(profile, summary, now)
        missing = summary.notices.get(profile.remote_id or 0) == PROVIDER_SUBSCRIPTION_MISSING
        return {
            "isProvider": True,
            "status": status.text,
            "statusDanger": status.danger,
            # Accounts.removable: only one the account no longer has goes on its own.
            "canRemove": missing,
            "canRenew": not missing and _billing(profile, summary) is not None,
            "needsSignIn": summary.state in _SIGNED_OUT,
            "providerName": summary.provider.name,
        }

    def _note(self, profile: Profile) -> str:
        if profile.unread:
            return self.tr("连接后读取")
        if self._routing.mode == MODE_PROFILE and profile.id == self._list.active:
            inspection = self._routing.inspections.get(profile.id)
            if inspection is not None and inspection.template:
                # Its own rules are missing or too lossy: subconv's routing template routes.
                return self.tr("路由模板使用中")
            return self.tr("规则使用中")
        if self._routing.mode == MODE_PLAN and not profile.in_pool:
            return self.tr("不加入节点池")
        return ""

    def _left_out(self, profile: Profile) -> str:
        """The nodes, and the rules of its own, that the core could not read (D54); its rules
        only count while they route."""
        inspection = self._routing.inspections.get(profile.id)
        if inspection is None:
            return ""
        parts = []
        if inspection.nodes_left_out:
            parts.append(self.tr("略去 %n 个节点", "", inspection.nodes_left_out))
        rules_in_use = self._routing.mode == MODE_PROFILE and profile.id == self._list.active
        if rules_in_use and inspection.rules_left_out:
            parts.append(self.tr("略去 %n 条规则", "", inspection.rules_left_out))
        return " · ".join(parts)

    @Property(int, constant=True)
    def defaultAutoUpdateHours(self) -> int:
        return DEFAULT_AUTO_UPDATE_HOURS

    def _refresh_label(self, profile: Profile) -> str:
        return self.tr("更新") if profile.can_auto_update else self.tr("重新读取")

    @Slot(str)
    def useRules(self, profile_id: str) -> None:
        """使用它的规则: route with this profile's own rules, as the routing page would, and
        go there. A provider's subscription without a config yet downloads it first."""
        self.routingRequested.emit()
        if profile_id == self._list.active and self._routing.mode == MODE_PROFILE:
            return
        profile = self._profile(profile_id)
        if profile is not None and profile.is_provider and not has_config(profile):
            self._use(profile)
            return
        self._run(self._library.profiles.activate(profile_id), reloads=True)

    def _profile(self, profile_id: str) -> Profile | None:
        return next((p for p in self._list.profiles if p.id == profile_id), None)

    def _use(self, profile: Profile) -> None:
        """Use a provider's subscription (Accounts.use): its config downloads first when it
        has none, and a running tunnel reloads."""
        provider_id, remote_id = profile.provider_id, profile.remote_id
        assert provider_id is not None
        assert remote_id is not None
        self._run(self._library.accounts.use(provider_id, remote_id), reloads=True)

    @Slot(str, bool)
    def setInPool(self, profile_id: str, in_pool: bool) -> None:
        reloads = self._routing.mode == MODE_PLAN
        self._run(self._library.profiles.set_in_pool(profile_id, in_pool), reloads=reloads)

    @Slot(str)
    def updateProfile(self, profile_id: str) -> None:
        if profile_id in self._updating:
            return
        self._updating.add(profile_id)
        self._error = ""
        self.changed.emit()

        def updated(update: Updated) -> None:
            self._updating.discard(profile_id)
            self._error = ""
            self._updated(update)

        def failed(error: BaseException) -> None:
            self._updating.discard(profile_id)
            if isinstance(error, ProviderError) and error.code in KEEPS_CONFIG:
                # The account kept its last config by design; its row says why (D57).
                self._error = ""
                self._runner.submit(self._library.current(), self._loaded)
                return
            self._failed(error)

        self._runner.submit(self._library.profiles.update(profile_id), updated, failed)

    @Slot(str, bool)
    def setAutoUpdate(self, profile_id: str, enabled: bool) -> None:
        self._run(self._library.profiles.set_auto_update(profile_id, enabled))

    @Slot(str)
    def editProfile(self, profile_id: str) -> None:
        if self._edit_saving:
            return
        self._edit_target = profile_id
        self._edit_error = ""
        self.changed.emit()
        self.editOpened.emit()

    @Property("QVariantMap", notify=changed)  # pyright: ignore[reportArgumentType]
    def editDetails(self) -> dict[str, Any]:
        profile = next((p for p in self._list.profiles if p.id == self._edit_target), None)
        if profile is None:
            return {}
        return {
            "name": profile.name,
            "url": profile.url,
            "subscription": profile.is_subscription,
            "autoUpdate": profile.auto_update_hours > 0,
            "website": web_link(profile.website),
            "support": web_link(profile.support),
            "autoUpdateHours": profile.auto_update_interval,  # also kept while off
        }

    @Property(bool, notify=changed)
    def editSaving(self) -> bool:
        return self._edit_saving

    @Property(str, notify=changed)
    def editError(self) -> str:
        return self._edit_error

    @Slot(str, str, bool, int)
    def saveProfile(
        self, name: str, url: str, auto_update: bool, interval_hours: int | None = None
    ) -> None:
        if self._edit_saving:
            return
        self._edit_saving = True
        self._edit_error = ""
        self.changed.emit()

        def saved(profiles: ProfileList) -> None:
            self._edit_saving = False
            self._loaded(profiles)
            self._routing_touched()
            self.applied.emit()
            self.editSaved.emit()

        def failed(error: BaseException) -> None:
            self._edit_saving = False
            self._edit_error = failure_text(error)
            self._loaded(self._library.snapshot())

        self._runner.submit(
            self._library.profiles.edit(
                self._edit_target, name, url, auto_update, interval_hours=interval_hours
            ),
            saved,
            failed,
        )

    # -- removal -----------------------------------------------------------------------------
    @Slot(str)
    def requestRemove(self, profile_id: str) -> None:
        """Work out what removing the profile does, then ask the view to confirm it. The
        profile in use while the tunnel runs is refused without asking."""
        self._error = ""
        self.changed.emit()

        def ready(removal: Removal) -> None:
            self._removal = removal
            self._removal_pending = True
            self.changed.emit()
            self.removalRequested.emit()

        def failed(error: BaseException) -> None:
            self._error = failure_text(error)
            self.changed.emit()

        self._runner.submit(self._library.profiles.removal(profile_id), ready, failed)

    @Property(str, notify=changed)
    def removalTitle(self) -> str:
        if self._removal is None:
            return ""
        return self.tr("删除「{name}」？").format(name=self._removal.profile.name)

    @Property(str, notify=changed)
    def removalText(self) -> str:
        return _removal_text(self._removal) if self._removal else ""

    @Slot()
    def confirmRemove(self) -> None:
        if self._removal is not None and self._removal_pending:
            self._removal_pending = False
            removal = self._removal
            reloads = removal.active or bool(removal.pool_nodes)
            self._run(self._library.profiles.remove(removal.profile.id), reloads=reloads)

    @Slot()
    def cancelRemove(self) -> None:
        self._removal_pending = False

    @Slot(str)
    def copyLink(self, profile_id: str) -> None:
        profile = next((p for p in self._list.profiles if p.id == profile_id), None)
        if profile is not None and profile.url:
            QGuiApplication.clipboard().setText(profile.url)

    # -- provider accounts -------------------------------------------------------------------
    def _account(self, profile_id: str) -> tuple[Profile, AccountSummary] | None:
        """A provider's subscription and its account, while both are known."""
        profile = self._profile(profile_id)
        if profile is None or not profile.is_provider:
            return None
        summary = self._accounts.get(profile.provider_id or "")
        return None if summary is None else (profile, summary)

    @Slot(str)
    def requestProviderSignIn(self, address: str) -> None:
        """登录服务商…: open the sign-in dialog, on ``address`` when there is one."""
        self.providerSignInRequested.emit(address)

    @Slot(str)
    def signInAgain(self, profile_id: str) -> None:
        """重新登录…: the sign-in dialog, on the subscription's provider."""
        found = self._account(profile_id)
        if found is not None:
            self.providerSignInRequested.emit(found[1].provider.origin)

    @Slot(str)
    def renew(self, profile_id: str) -> None:
        """在网页上续费: the subscription's page in the user center, in the browser (D58).
        Back from it, the window's activation and the update check sync the account."""
        found = self._account(profile_id)
        if found is None:
            return
        profile, summary = found
        assert profile.remote_id is not None
        try:
            address = web.renew(summary.provider.origin, profile.remote_id)
        except (ProviderError, ValueError) as error:
            self._error = failure_text(error)
            self.changed.emit()
            return
        QDesktopServices.openUrl(QUrl(address))
        # Signed out meanwhile, there is nothing left to watch.
        watch = self._library.accounts.watch(summary.provider.id)
        self._runner.submit(watch, None, lambda _error: None)

    @Slot(str)
    def requestSignOut(self, profile_id: str) -> None:
        """退出「示例服务商」: work out what signing out removes, then ask the view to confirm
        it (docs/accounts.md §4.4)."""
        found = self._account(profile_id)
        if found is None:
            return
        self._error = ""
        self.changed.emit()

        def ready(removal: ProviderRemoval) -> None:
            self._sign_out = removal
            self._sign_out_pending = True
            self.changed.emit()
            self.signOutRequested.emit()

        def failed(error: BaseException) -> None:
            self._error = failure_text(error)
            self.changed.emit()

        removal = self._library.accounts.sign_out_removal(found[1].provider.id)
        self._runner.submit(removal, ready, failed)

    @Property(str, notify=changed)
    def signOutTitle(self) -> str:
        if self._sign_out is None:
            return ""
        return self.tr("退出「{name}」？").format(name=self._sign_out.provider.name)

    @Property(str, notify=changed)
    def signOutText(self) -> str:
        return sign_out_text(self._sign_out) if self._sign_out else ""

    @Property("QStringList", notify=changed)  # pyright: ignore[reportArgumentType]
    def signOutProfiles(self) -> list[str]:
        """The ids of the subscriptions signing out removes, for the view to move the focus
        off their rows."""
        return [p.id for p in self._sign_out.profiles] if self._sign_out else []

    @Slot()
    def confirmSignOut(self) -> None:
        if self._sign_out is not None and self._sign_out_pending:
            self._sign_out_pending = False
            provider_id = self._sign_out.provider.id
            self._run(self._library.accounts.sign_out(provider_id), reloads=True)

    @Slot()
    def cancelSignOut(self) -> None:
        self._sign_out_pending = False

    @Slot()
    def checkUpdates(self) -> None:
        """Sync the provider accounts, then update the subscriptions that are due; also
        refreshes the relative times."""
        if self._checking:
            self.changed.emit()
            return
        self._checking = True

        def checked(update: Updated) -> None:
            self._checking = False
            self._updated(update)

        def failed(error: BaseException) -> None:
            self._checking = False
            self._error = failure_text(error)
            self.changed.emit()

        self._runner.submit(self._library.profiles.update_due(), checked, failed)

    # -- overrides ---------------------------------------------------------------------------
    @Property(bool, notify=changed)
    def hasGlobalOverride(self) -> bool:
        return self._list.has_global_override

    @Property(str, notify=changed)
    def globalOverrideDetail(self) -> str:
        if self._list.has_global_override:
            return self.tr("两种分流方式都生效 · 已设置")
        return self.tr("两种分流方式都生效 · 未设置")

    @Property(bool, notify=changed)
    def canEditGlobalOverride(self) -> bool:
        """The global override is checked with the routing in use, so there must be one."""
        return self._routing.mode == MODE_PLAN or self._list.active is not None

    @Property(str, notify=changed)
    def overrideScope(self) -> str:
        """Where the override being edited applies (routing.md §5.6)."""
        if self._override_target is None:
            return self.tr("对“订阅自带”和“我的方案”都生效。")
        if self._routing.mode == MODE_PROFILE and self._override_target == self._list.active:
            return self.tr("使用这份订阅的规则时生效，当前生效。")
        return self.tr("使用这份订阅的规则时生效，当前未生效。")

    @Slot(str)
    def editOverride(self, profile_id: str) -> None:
        profile = next((p for p in self._list.profiles if p.id == profile_id), None)
        if profile is not None:
            self._open_override(profile_id, profile.name)

    @Slot()
    def editGlobalOverride(self) -> None:
        self._open_override(None, "")

    def _open_override(self, profile_id: str | None, name: str) -> None:
        """Read the stored text, then let the view open the editor on it."""
        self._error = ""

        def read(text: str) -> None:
            self._override_target, self._override_name = profile_id, name
            self._override_text = text
            self._override_error = ""
            self._override_saving = False
            self.changed.emit()
            self.overrideOpened.emit()

        def failed(error: BaseException) -> None:
            self._error = failure_text(error)
            self.changed.emit()

        self._runner.submit(self._library.profiles.read_override(profile_id), read, failed)

    @Property(str, notify=changed)
    def overrideTitle(self) -> str:
        if self._override_target is None:
            return self.tr("编辑全局覆写")
        return self.tr("编辑「{name}」的覆写").format(name=self._override_name)

    @Property(str, notify=changed)
    def overrideText(self) -> str:
        """The stored text when the editor opened."""
        return self._override_text

    @Property(bool, notify=changed)
    def overrideSaving(self) -> bool:
        return self._override_saving

    @Property(str, notify=changed)
    def overrideError(self) -> str:
        return self._override_error

    @Slot(str)
    def saveOverride(self, text: str) -> None:
        """Validate and store the editor's text. A rejection stays in the editor; success
        closes it, and refreshes the groups when the tunnel runs the profile affected."""
        if self._override_saving:
            return
        target = self._override_target
        reloads = target is None or (
            target == self._list.active and self._routing.mode == MODE_PROFILE
        )
        self._override_saving = True
        self._override_error = ""
        self.changed.emit()

        def saved(profiles: ProfileList) -> None:
            self._override_saving = False
            self._override_text = text
            self._loaded(profiles)
            self.overrideSaved.emit()
            if reloads:
                self.applied.emit()

        def failed(error: BaseException) -> None:
            self._override_saving = False
            self._override_error = failure_text(error)
            self.changed.emit()

        self._runner.submit(self._library.profiles.save_override(target, text), saved, failed)

    # -- import ------------------------------------------------------------------------------
    @Property(bool, notify=changed)
    def importing(self) -> bool:
        return self._importing

    @Property(str, notify=changed)
    def importError(self) -> str:
        return self._import_error

    @Slot()
    def clearImportError(self) -> None:
        if self._import_error:
            self._import_error = ""
            self.changed.emit()

    def _import(self, action: Coroutine[Any, Any, ProfileList], *, from_link: bool) -> None:
        """Link imports report failures in their form; file imports in the page header."""
        if self._importing:
            action.close()
            return
        self._importing = True
        self._import_error = ""
        self._error = ""
        self.changed.emit()

        def imported(profiles: ProfileList) -> None:
            self._importing = False
            self._loaded(profiles)
            self._routing_touched()
            self.applied.emit()
            self.imported.emit()

        def failed(error: BaseException) -> None:
            self._importing = False
            if from_link:
                self._import_error = failure_text(error)
            else:
                self._error = failure_text(error)
            self.changed.emit()

        self._runner.submit(action, imported, failed)

    @Property(str, notify=changed)
    def prefillUrl(self) -> str:
        return self._prefill_url

    @Property(str, notify=changed)
    def prefillName(self) -> str:
        return self._prefill_name

    @Slot(str, str)
    def prefillImport(self, url: str, name: str) -> None:
        """An install link names a subscription (docs/release.md §7): the view opens the
        import form with it filled in, and the user imports from there. Ignored while an
        import runs."""
        if self._importing:
            return
        self._prefill_url, self._prefill_name = url, name
        self._import_error = self._error = ""
        self.changed.emit()
        self.importPrefilled.emit()

    @Slot()
    def rejectLink(self) -> None:
        """An install link that names no subscription; the page header says so."""
        self._error = self.tr("无法识别的导入链接")
        self.changed.emit()

    @Slot(str, str, bool, int, bool)
    def importUrl(
        self,
        url: str,
        name: str,
        auto_update: bool,
        interval_hours: int | None = None,
        suggested: bool = False,
    ) -> None:
        self._import(
            self._library.profiles.import_url(
                url, name, auto_update, interval_hours=None if suggested else interval_hours
            ),
            from_link=True,
        )

    @Slot(str)
    def importFile(self, file_url: str) -> None:
        """Import the file a FileDialog chose; it passes a file:// URL."""
        path = QUrl(file_url).toLocalFile() or file_url
        self._import(self._library.profiles.import_file(Path(path)), from_link=False)

    @Slot(str, str)
    def importNodeLinks(self, text: str, name: str) -> None:
        self._import(
            self._library.profiles.import_links(text, name.strip() or self.tr("节点链接")),
            from_link=True,
        )

    # -- active profile ----------------------------------------------------------------------
    def _active(self) -> Profile | None:
        return self._list.active_profile()

    @Property(bool, notify=changed)
    def hasActive(self) -> bool:
        return self._active() is not None

    @Property(str, notify=changed)
    def activeName(self) -> str:
        active = self._active()
        return active.name if active else ""

    def _from(self, profile: Profile) -> str:
        """Where the profile comes from, in short: a provider's site, a subscription's host,
        a file's name."""
        if profile.is_provider:
            summary = self._accounts.get(profile.provider_id or "")
            return summary.provider.name if summary is not None else self.tr("服务商")
        return _origin(profile)

    def _usage_of(self, profile: Profile) -> Usage | None:
        """Its usage: a provider's subscription's from billing (DESIGN.md "服务商的订阅行")."""
        if not profile.is_provider:
            return profile.usage
        billing = _billing(profile, self._accounts.get(profile.provider_id or ""))
        return billing.usage if billing is not None else profile.usage

    @Property(str, notify=changed)
    def activeDetail(self) -> str:
        """Where the profile in use comes from and when it was updated; a failed update is
        ``activeWarning``, shown in the warning colour after it (DESIGN.md "分组与行")."""
        active = self._active()
        if active is None:
            return ""
        origin = self._from(active)
        if active.id in self._updating:
            return self.tr("{origin} · 正在更新").format(origin=origin)
        if active.id in self._list.update_failed or not has_config(active):
            return origin
        ago = fmt.ago(active.updated_at, self._clock())
        return self.tr("{origin} · {ago}更新").format(origin=origin, ago=ago)

    @Property(str, notify=changed)
    def activeWarning(self) -> str:
        active = self._active()
        if active is None or active.id in self._updating:
            return ""
        return self.tr("更新失败") if active.id in self._list.update_failed else ""

    @Property(bool, notify=changed)
    def activeUpdating(self) -> bool:
        active = self._active()
        return active is not None and active.id in self._updating

    @Property(str, notify=changed)
    def activeUsage(self) -> str:
        active = self._active()
        return _usage(self._usage_of(active))[0] if active else ""

    @Property(str, notify=changed)
    def activeExpiry(self) -> str:
        active = self._active()
        if active is None:
            return ""
        text, expired = _expiry(self._usage_of(active), self._clock())
        if text == "—":
            return ""
        return text if expired else self.tr("{date} 到期").format(date=text)

    # -- the provider subscription in use, as the overview shows it ----------------------------
    def _active_account(self) -> tuple[Profile, AccountSummary] | None:
        """The provider's subscription whose rules are in use (订阅自带), and its account.
        Under my plan no one subscription is in use, so there is none."""
        active = self._active()
        if active is None or self._routing.mode != MODE_PROFILE:
            return None
        return self._account(active.id)

    def _active_status(self) -> ProviderStatus:
        found = self._active_account()
        if found is None:
            return ProviderStatus()
        return provider_status(*found, self._clock())

    @Property(str, notify=changed)
    def activeNotice(self) -> str:
        """The status of the provider's subscription in use, the overview's 当前使用 second
        line in the warning colour (DESIGN.md "服务商的订阅行"); "" while there is none."""
        return self._active_status().text

    @Property(bool, notify=changed)
    def activeNoticeDanger(self) -> bool:
        """The provider stopped it: the notice is in the danger colour."""
        return self._active_status().danger

    @Property(bool, notify=changed)
    def canRenewActive(self) -> bool:
        """It runs out or ran out: 续费 follows the notice."""
        return self._active_status().renew

    @Slot()
    def renewActive(self) -> None:
        """续费 on the overview: a link to the account page, on the provider of the
        subscription in use, which shows what renewing it means and opens the user center
        from there (DESIGN.md "服务商的订阅行"; docs/accounts.md §7.5)."""
        found = self._active_account()
        if found is not None:
            self.accountRequested.emit(found[1].provider.id)

    def _suggestion(self) -> Profile | None:
        """Another subscription of the same account to use instead of the one in use, which
        billing says cannot be used now: the one with the most left. Never used without
        asking (D57)."""
        found = self._active_account()
        if found is None:
            return None
        active, summary = found
        if summary.state != ProviderState.SIGNED_IN:
            return None  # none of its subscriptions can be downloaded now
        now = self._clock()
        billing = {s.id: s for s in summary.subscriptions}
        current = billing.get(active.remote_id or 0)
        if current is not None and not blocked(current, now):
            return None
        others = {
            p.remote_id: p
            for p in self._list.profiles
            if p.is_provider
            and p.provider_id == active.provider_id
            and p.id != active.id
            and p.remote_id in billing
            and self._routing.has_rules(p) is not False
        }
        best = most_left([billing[remote_id] for remote_id in others if remote_id is not None], now)
        return None if best is None else others[best.id]

    @Property(str, notify=changed)
    def suggestedName(self) -> str:
        """The subscription the overview suggests in 改用「示例订阅 B」; "" for none."""
        suggestion = self._suggestion()
        return suggestion.name if suggestion is not None else ""

    @Slot()
    def useSuggested(self) -> None:
        """改用「示例订阅 B」: use the suggested subscription, downloading its config first
        when it has none; a running tunnel reloads (Accounts.use)."""
        suggestion = self._suggestion()
        if suggestion is not None:
            self._use(suggestion)
