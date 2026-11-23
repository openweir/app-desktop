"""The provider's user center pages the app opens in the browser (docs/accounts.md §6.2, D58):
buying, renewing, paying, topping up, passwords and two-factor all happen there. Each URL is
the provider's origin plus a fixed path; the only values in it, a subscription id and a plan
and period from billing, are checked or quoted so they cannot change where it leads."""

from __future__ import annotations

from urllib.parse import quote

from openweir.models.provider.api import origin as _origin
from openweir.models.provider.types import PROVIDER_INVALID_ADDRESS, ProviderError


def _base(origin: str) -> str:
    if _origin(origin) != origin:
        raise ProviderError(PROVIDER_INVALID_ADDRESS, "not an origin")
    return origin


def home(origin: str) -> str:
    """在用户中心打开."""
    return f"{_base(origin)}/"


def renew(origin: str, remote_id: int) -> str:
    """续费: the subscription's page (RemoteSubscription.id, a profile's remote_id)."""
    if isinstance(remote_id, bool) or not isinstance(remote_id, int) or remote_id <= 0:
        raise ValueError("a subscription id is a positive integer")
    return f"{_base(origin)}/subscription?id={remote_id}"


def plans(origin: str) -> str:
    """更换套餐, 选购套餐: the plans on sale."""
    return f"{_base(origin)}/plans"


def checkout(origin: str, plan_id: str, period: str | None = None) -> str:
    """选购 one plan: its checkout opens on the plans page (``<planId>:<period>``; the user
    center ignores a period it does not sell, such as one_time)."""
    if not plan_id:
        raise ValueError("a plan id is required")
    value = f"{plan_id}:{period}" if period else plan_id
    return f"{_base(origin)}/plans?checkout={quote(value, safe=':')}"


def wallet(origin: str) -> str:
    """充值, 明细."""
    return f"{_base(origin)}/wallet"


def orders(origin: str) -> str:
    """订单, 去支付."""
    return f"{_base(origin)}/orders"


def security(origin: str) -> str:
    """密码与两步验证."""
    return f"{_base(origin)}/settings/security"


def devices(origin: str) -> str:
    """登录设备."""
    return f"{_base(origin)}/settings/devices"
