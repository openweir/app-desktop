"""How a provider account and its subscriptions are worded (docs/accounts.md §5.5; DESIGN.md
"服务商的订阅行"): the one set of status texts that the sign-in dialog's picker, the
subscriptions page and overview, and the account page all show, in one translation context."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication

from openweir.models.provider.types import (
    CONDITION_DISABLED,
    CONDITION_EXPIRED,
    CONDITION_EXPIRING,
    CONDITION_NOT_STARTED,
    CONDITION_TRAFFIC_LOW,
    CONDITION_TRAFFIC_USED_UP,
    CONDITION_UNUSABLE,
    PROVIDER_LINK_UNAVAILABLE,
    PROVIDER_SUBSCRIPTION_DISABLED,
    PROVIDER_SUBSCRIPTION_EXPIRED,
    PROVIDER_SUBSCRIPTION_MISSING,
    PROVIDER_SUBSCRIPTION_UNUSABLE,
    ProviderState,
    RemoteSubscription,
)

# The colour a status is written in; "" is the default one.
TONE_WARNING = "warning"
TONE_DANGER = "danger"
DAY_S = 86400
HOUR_S = 3600


@dataclass(frozen=True)
class ProviderStatus:
    """What is wrong with a provider's subscription or account, as one short text; none
    while nothing is (DESIGN.md "服务商的订阅行"), and what the overview offers with it."""

    text: str = ""
    danger: bool = False  # the provider stopped it: danger, not warning
    renew: bool = False  # it runs out, or ran out: the overview offers 续费

    @property
    def tone(self) -> str:
        """Its colour: danger, warning for anything else to say, the default for none."""
        if self.danger:
            return TONE_DANGER
        return TONE_WARNING if self.text else ""


def expires_in(seconds: float) -> str:
    """「3 天后到期」, in hours on the last day."""
    if seconds >= DAY_S:
        days = math.ceil(seconds / DAY_S)
        return QCoreApplication.translate("ProviderText", "%n 天后到期", None, days)
    hours = max(1, math.ceil(seconds / HOUR_S))
    return QCoreApplication.translate("ProviderText", "%n 小时后到期", None, hours)


def condition_status(subscription: RemoteSubscription, now: float) -> ProviderStatus:
    """What billing says of a subscription at ``now`` (RemoteSubscription.condition); none
    while it is active."""
    condition = subscription.condition(now)
    if condition == CONDITION_EXPIRING:
        assert subscription.expire_at is not None
        return ProviderStatus(expires_in(subscription.expire_at - now), renew=True)
    if condition == CONDITION_TRAFFIC_LOW:
        assert subscription.total
        percent = min(100, subscription.used * 100 // subscription.total)
        text = QCoreApplication.translate("ProviderText", "流量已用 {percent}%")
        return ProviderStatus(text.format(percent=percent))
    if condition == CONDITION_EXPIRED:
        return _expired()
    if condition == CONDITION_TRAFFIC_USED_UP:
        return ProviderStatus(QCoreApplication.translate("ProviderText", "流量已用完"))
    if condition == CONDITION_NOT_STARTED:
        return ProviderStatus(QCoreApplication.translate("ProviderText", "未开始"))
    if condition == CONDITION_DISABLED:
        return _stopped()
    if condition == CONDITION_UNUSABLE:
        return _unusable()
    return ProviderStatus()


def notice_status(code: str) -> ProviderStatus:
    """Why a subscription's last update kept its config, by the code it ran into
    (AccountSummary.notices; docs/accounts.md §5.4); none for another code."""
    if code == PROVIDER_SUBSCRIPTION_MISSING:
        return ProviderStatus(QCoreApplication.translate("ProviderText", "已不在账户中"))
    if code == PROVIDER_LINK_UNAVAILABLE:
        return ProviderStatus(QCoreApplication.translate("ProviderText", "没有订阅链接"))
    if code == PROVIDER_SUBSCRIPTION_EXPIRED:
        return _expired()
    if code == PROVIDER_SUBSCRIPTION_DISABLED:
        return _stopped()
    if code == PROVIDER_SUBSCRIPTION_UNUSABLE:
        return _unusable()
    return ProviderStatus()


def no_nodes_status() -> ProviderStatus:
    """A usable subscription whose config has no node: the provider grants none yet (a plan
    waiting for a server, or no rule for it), so the user is pointed at its support."""
    return ProviderStatus(QCoreApplication.translate("ProviderText", "暂无可用节点，请联系客服"))


def account_status(state: ProviderState) -> ProviderStatus:
    """What the account's state says of everything in it (docs/accounts.md §4.2): it needs
    signing in again, or the provider disabled it; none otherwise."""
    if state == ProviderState.SIGN_IN_REQUIRED:
        return ProviderStatus(QCoreApplication.translate("ProviderText", "需要重新登录"))
    if state == ProviderState.ACCOUNT_DISABLED:
        text = QCoreApplication.translate("ProviderText", "账户已停用")
        return ProviderStatus(text, danger=True)
    return ProviderStatus()


def listed(status: ProviderStatus, trial: bool) -> str:
    """A status as a list of an account's subscriptions writes it: 有效 while nothing is
    wrong, after 试用 for a trial."""
    text = status.text or QCoreApplication.translate("ProviderText", "有效")
    if not trial:
        return text
    return QCoreApplication.translate("ProviderText", "试用 · {status}").format(status=text)


def trial() -> str:
    """试用, before where a trial subscription comes from on the subscriptions page."""
    return QCoreApplication.translate("ProviderText", "试用")


def _expired() -> ProviderStatus:
    return ProviderStatus(QCoreApplication.translate("ProviderText", "已到期"), renew=True)


def _stopped() -> ProviderStatus:
    return ProviderStatus(QCoreApplication.translate("ProviderText", "已停用"), danger=True)


def _unusable() -> ProviderStatus:
    return ProviderStatus(QCoreApplication.translate("ProviderText", "暂不可用"))
