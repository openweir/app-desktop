"""Billing into profiles (docs/accounts.md §5): the part of a sync that needs no request, no
storage and no lock, so that it can be read and tested on its own. models/library/accounts.py
makes the requests and applies what ``reconcile`` returns under the library's lock.

    checked       what a billing list must be: the signed-in user's, ids unique, not too many
    names         the profiles' names: the plan's name, with " · #<id>" when two share one
    blocked       why billing says a config cannot be downloaded now (a KEEPS_CONFIG code)
    reconcile     the profile list after a sync: added, renamed, usage copied, missing marked
    next_reset    when the traffic starts over, as the user center works it out
    most_left     the usable subscription with the most traffic left, for a default choice

What the app shows of a subscription (有效, 即将到期, 流量将尽, 已到期, 流量已用完, 已停用,
未开始, with 试用 beside it) is ``RemoteSubscription.condition`` in types.py, which the view
models share.
"""

from __future__ import annotations

import calendar
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, tzinfo

from openweir.models.profiles import KIND_PROVIDER, Profile, ProfileList
from openweir.models.provider.types import (
    MAX_SUBSCRIPTIONS,
    PROVIDER_BAD_RESPONSE,
    PROVIDER_SUBSCRIPTION_DISABLED,
    PROVIDER_SUBSCRIPTION_EXPIRED,
    PROVIDER_SUBSCRIPTION_UNUSABLE,
    RESET_MONTH_START,
    RESET_PURCHASE_DAY,
    STATUS_DISABLED,
    STATUS_EXPIRED,
    ProviderError,
    RemoteSubscription,
)

# What a profile's name adds to the plan's name when two subscriptions of the account share it.
DUPLICATE_SUFFIX = " · #{id}"


def checked(
    subscriptions: Sequence[RemoteSubscription], user_id: str
) -> tuple[RemoteSubscription, ...]:
    """The billing list ordered by id, once it is the signed-in user's own: every ``userId``
    is ``user_id``, no id comes twice, and there are at most MAX_SUBSCRIPTIONS. Anything else
    is PROVIDER_BAD_RESPONSE, as profiles must never be made from another account's list."""
    if len(subscriptions) > MAX_SUBSCRIPTIONS:
        raise ProviderError(PROVIDER_BAD_RESPONSE, f"more than {MAX_SUBSCRIPTIONS} subscriptions")
    if any(s.user_id != user_id for s in subscriptions):
        raise ProviderError(PROVIDER_BAD_RESPONSE, "a subscription of another user")
    if len({s.id for s in subscriptions}) != len(subscriptions):
        raise ProviderError(PROVIDER_BAD_RESPONSE, "subscriptions: duplicate ids")
    return tuple(sorted(subscriptions, key=lambda s: s.id))


def names(subscriptions: Sequence[RemoteSubscription]) -> dict[int, str]:
    """Each subscription's profile name by id: its plan's name in the request's language
    (``planId`` without one). Two or more with the same name each get " · #<id>", so the rows
    can be told apart and keep their names when one of them goes."""
    plain = {s.id: s.plan_name.strip() or s.plan_id.strip() or f"#{s.id}" for s in subscriptions}
    count = Counter(plain.values())
    return {
        remote_id: name + DUPLICATE_SUFFIX.format(id=remote_id) if count[name] > 1 else name
        for remote_id, name in plain.items()
    }


def blocked(subscription: RemoteSubscription, now: float) -> str:
    """Why billing says the config cannot be downloaded now (docs/accounts.md §5.4), as one of
    KEEPS_CONFIG's codes; "" when it can. The server's ``status`` and ``usable`` decide; the
    clock only tells an expired subscription from one that cannot be used for another reason
    (traffic used up, not started)."""
    if subscription.status == STATUS_DISABLED:
        return PROVIDER_SUBSCRIPTION_DISABLED
    expired = subscription.expire_at is not None and subscription.expire_at <= now
    if subscription.status == STATUS_EXPIRED or (expired and not subscription.usable):
        return PROVIDER_SUBSCRIPTION_EXPIRED
    if not subscription.usable:
        return PROVIDER_SUBSCRIPTION_UNUSABLE
    return ""


def key(subscription: RemoteSubscription | None) -> tuple[str, str, bool, float, str] | None:
    """What a sync compares with the stored copy: when one of these changed, the config may
    have too (another plan, renewed, used up, unblocked), or the way to download it (the
    provider switched between links and the app), so the profile is updated."""
    if subscription is None:
        return None
    return (
        subscription.plan_id,
        subscription.status,
        subscription.usable,
        subscription.updated_at,
        subscription.access,
    )


def has_config(profile: Profile) -> bool:
    """A config was downloaded for it: a profile a sync just added, or one restored from a
    backup, has none yet, and is dated only once it has."""
    return profile.updated_at > 0


@dataclass(frozen=True)
class Reconciliation:
    """A provider's profiles brought in line with its billing list. Ids are profile ids."""

    profiles: ProfileList  # the whole list: new profiles in place, names and usage up to date
    # What billing says of each profile still in the account, to store (without links).
    stored: Mapping[str, RemoteSubscription]
    added: tuple[str, ...]
    renamed: tuple[str, ...]
    # Their (plan_id, status, usable, updated_at) changed since the stored copy, or there was
    # none: whatever kept their last config may be over.
    changed: tuple[str, ...]
    # No longer in the account (已不在账户中): they keep their config and name.
    missing: tuple[str, ...]
    # To update now: usable, and changed or without a config yet.
    refresh: tuple[str, ...]


def reconcile(
    profiles: ProfileList,
    provider_id: str,
    subscriptions: Sequence[RemoteSubscription],
    stored: Mapping[int, RemoteSubscription],
    now: float,
    *,
    new_id: Callable[[int], str],
    auto_update_hours: int,
) -> Reconciliation:
    """Bring the provider's profiles in line with ``subscriptions`` (``checked``), given what
    was ``stored`` of them by remote id (docs/accounts.md §5.2):

      - a subscription without a profile gets one, with the id ``new_id`` gives its remote
        id, named by ``names``, updating every ``auto_update_hours``; new ones go after the
        provider's last profile, or at the end of the list when it has none, in the order of
        their ids;
      - a profile whose subscription is listed takes its current name and billing's usage
        (Profile.usage, as Subscription-Userinfo would give it);
      - a profile whose subscription is not listed is missing: it stays as it is.

    A remote id with two profiles (which nothing makes) is the first one's."""
    titles = names(subscriptions)
    own: dict[int, Profile] = {}
    for profile in profiles.profiles:
        if profile.kind == KIND_PROVIDER and profile.provider_id == provider_id:
            assert profile.remote_id is not None
            own.setdefault(profile.remote_id, profile)
    listed = {s.id for s in subscriptions}
    edited: dict[str, Profile] = {}
    added: list[Profile] = []
    rows: dict[str, RemoteSubscription] = {}
    renamed: list[str] = []
    changed: list[str] = []
    refresh: list[str] = []
    for subscription in subscriptions:
        profile = own.get(subscription.id)
        if profile is None:
            profile = Profile(
                id=new_id(subscription.id),
                name=titles[subscription.id],
                kind=KIND_PROVIDER,
                auto_update_hours=auto_update_hours,
                usage=subscription.usage,
                provider_id=provider_id,
                remote_id=subscription.id,
            )
            added.append(profile)
        else:
            current = replace(profile, name=titles[subscription.id], usage=subscription.usage)
            if current.name != profile.name:
                renamed.append(profile.id)
            if current != profile:
                edited[profile.id] = current
            profile = current
        rows[profile.id] = subscription
        if key(subscription) != key(stored.get(subscription.id)):
            changed.append(profile.id)
        if not blocked(subscription, now) and (profile.id in changed or not has_config(profile)):
            refresh.append(profile.id)
    kept = [edited.get(p.id, p) for p in profiles.profiles]
    last = max(
        (i for i, p in enumerate(kept) if p.kind == KIND_PROVIDER and p.provider_id == provider_id),
        default=len(kept) - 1,
    )
    ordered = (*kept[: last + 1], *added, *kept[last + 1 :])
    return Reconciliation(
        profiles=replace(profiles, profiles=ordered),
        stored=rows,
        added=tuple(p.id for p in added),
        renamed=tuple(renamed),
        changed=tuple(changed),
        missing=tuple(p.id for remote_id, p in own.items() if remote_id not in listed),
        refresh=tuple(refresh),
    )


# -- the next reset -------------------------------------------------------------------------------
def _add_months(when: datetime, months: int) -> datetime:
    """``when`` that many calendar months on, on the same day or the month's last when it has
    fewer days: api-edge's addMonths, which counts from the purchase so the day never drifts
    (Jan 31, Feb 28, Mar 31)."""
    index = when.month - 1 + months
    year, month = when.year + index // 12, index % 12 + 1
    return when.replace(
        year=year, month=month, day=min(when.day, calendar.monthrange(year, month)[1])
    )


def next_reset(subscription: RemoteSubscription, zone: tzinfo | None = None) -> float | None:
    """When the subscription's traffic starts over next, in unix seconds; None when it never
    does (``none``) or billing lacks the dates.

    ``purchase_day`` follows the user center (user-web's nextReset) and api-edge's daily job:
    periods are whole months counted from ``startAt``, so the next one starts at the first
    such boundary after ``periodStart``. ``month_start`` starts over on the 1st, which api-edge
    reckons in the provider's time zone; billing does not say which, so it is ``zone``, the
    machine's own by default: midnight of the 1st after ``periodStart``."""
    if subscription.reset_mode == RESET_PURCHASE_DAY:
        if subscription.start_at <= 0 or subscription.period_start <= 0:
            return None
        start = datetime.fromtimestamp(subscription.start_at, UTC)
        period = datetime.fromtimestamp(subscription.period_start, UTC)
        months = (period.year - start.year) * 12 + period.month - start.month
        candidate = _add_months(start, max(1, months))
        if candidate <= period:
            candidate = _add_months(start, months + 1)
        return candidate.timestamp()
    if subscription.reset_mode == RESET_MONTH_START:
        if subscription.period_start <= 0:
            return None
        # Naive local times read as the machine's zone, daylight saving included.
        period = datetime.fromtimestamp(subscription.period_start, zone)
        year, month = (
            (period.year + 1, 1) if period.month == 12 else (period.year, period.month + 1)
        )
        return datetime(year, month, 1, tzinfo=period.tzinfo).timestamp()
    return None


def traffic_left(subscription: RemoteSubscription) -> float:
    """Bytes left this period; infinity without a quota."""
    if subscription.total is None:
        return math.inf
    return max(0, subscription.total - subscription.used)


def most_left(subscriptions: Sequence[RemoteSubscription], now: float) -> RemoteSubscription | None:
    """The one to suggest when nothing else decides (the sign-in dialog's picker, the account
    page's 当前订阅): of those billing lets download a config, the one with the most traffic
    left, then the one that runs longest, then the oldest. None when none can be used."""
    usable = [s for s in subscriptions if not blocked(s, now)]
    if not usable:
        return None
    return max(
        usable,
        key=lambda s: (
            traffic_left(s),
            math.inf if s.expire_at is None else s.expire_at,
            -s.id,
        ),
    )
