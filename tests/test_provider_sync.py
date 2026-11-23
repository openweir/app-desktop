"""Billing into profiles, without requests or storage (models/provider/sync.py,
docs/accounts.md §5): the checks of a billing list, the names, what blocks a download, the
reconciliation a sync applies, the next traffic reset and the default choice."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from openweir.models.profiles import KIND_FILE, KIND_PROVIDER, Profile, ProfileList, Usage
from openweir.models.provider import sync, types
from openweir.models.provider.types import ProviderError, RemoteSubscription

NOW = 1_800_000_000.0
DAY = 86400.0
GIB = 1024**3
LINK = "https://sub.example.com/api/v1/app/proxy/subscriptions/secret-token"


def subscription(remote_id: int = 7, /, **changes: object) -> RemoteSubscription:
    fields: dict[str, object] = {
        "id": remote_id,
        "plan_id": "pro",
        "status": types.STATUS_ACTIVE,
        "usable": True,
        "plan_name": "专业版",
        "user_id": "user-1",
        "start_at": NOW - 30 * DAY,
        "expire_at": NOW + 30 * DAY,
        "period_start": NOW - 10 * DAY,
        "traffic_limit": 100 * GIB,
        "upload": 1 * GIB,
        "download": 9 * GIB,
        "total": 100 * GIB,
        "access": types.ACCESS_BOTH,
        "updated_at": NOW - DAY,
        "url": LINK,
    }
    fields.update(changes)
    return RemoteSubscription(**fields)  # type: ignore[arg-type]


def provider_profile(
    profile_id: str, remote_id: int, provider_id: str = "p1", **changes: object
) -> Profile:
    return replace(
        Profile(
            id=profile_id,
            name=f"#{remote_id}",
            kind=KIND_PROVIDER,
            auto_update_hours=12,
            updated_at=NOW - DAY,
            provider_id=provider_id,
            remote_id=remote_id,
        ),
        **changes,
    )


def new_id(remote_id: int) -> str:
    return f"new-{remote_id}"


def reconcile(
    profiles: ProfileList,
    subscriptions: list[RemoteSubscription],
    stored: dict[int, RemoteSubscription] | None = None,
) -> sync.Reconciliation:
    return sync.reconcile(
        profiles,
        "p1",
        subscriptions,
        stored or {},
        NOW,
        new_id=new_id,
        auto_update_hours=12,
    )


# -- the list ---------------------------------------------------------------------------------
def test_a_list_of_the_users_own_comes_ordered_by_id() -> None:
    listed = sync.checked([subscription(9), subscription(7)], "user-1")
    assert [s.id for s in listed] == [7, 9]
    assert sync.checked([], "user-1") == ()


@pytest.mark.parametrize(
    "subscriptions",
    [
        [subscription(7, user_id="user-2")],
        [subscription(7, user_id="")],
        [subscription(7), subscription(7)],
        [subscription(i) for i in range(1, types.MAX_SUBSCRIPTIONS + 2)],
    ],
    ids=["another user", "no user", "duplicate ids", "too many"],
)
def test_a_list_that_is_not_the_users_own_is_refused(
    subscriptions: list[RemoteSubscription],
) -> None:
    with pytest.raises(ProviderError) as caught:
        sync.checked(subscriptions, "user-1")
    assert caught.value.code == types.PROVIDER_BAD_RESPONSE
    assert "://" not in str(caught.value)


def test_names_are_the_plans_numbered_only_when_shared() -> None:
    names = sync.names(
        [
            subscription(7, plan_name="专业版"),
            subscription(9, plan_name="基础版"),
            subscription(11, plan_name="专业版"),
            subscription(12, plan_name="", plan_id="team"),
            subscription(13, plan_name="  "),
        ]
    )
    assert names == {7: "专业版 · #7", 9: "基础版", 11: "专业版 · #11", 12: "team", 13: "pro"}


# -- what blocks a download -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({}, ""),
        ({"status": types.STATUS_DISABLED, "usable": False}, types.PROVIDER_SUBSCRIPTION_DISABLED),
        ({"status": types.STATUS_EXPIRED, "usable": False}, types.PROVIDER_SUBSCRIPTION_EXPIRED),
        ({"expire_at": NOW - DAY, "usable": False}, types.PROVIDER_SUBSCRIPTION_EXPIRED),
        ({"download": 99 * GIB, "usable": False}, types.PROVIDER_SUBSCRIPTION_UNUSABLE),
        ({"start_at": NOW + DAY, "usable": False}, types.PROVIDER_SUBSCRIPTION_UNUSABLE),
        # The server says usable: the clock does not overrule it.
        ({"expire_at": NOW - DAY}, ""),
        ({"expire_at": None, "total": None}, ""),
    ],
)
def test_what_blocks_a_download(changes: dict[str, object], code: str) -> None:
    assert sync.blocked(subscription(**changes), NOW) == code
    assert not code or code in types.KEEPS_CONFIG


@pytest.mark.parametrize(
    ("changes", "condition"),
    [
        ({}, types.CONDITION_ACTIVE),
        ({"expire_at": NOW + 3 * DAY}, types.CONDITION_EXPIRING),
        ({"expire_at": NOW + 3 * DAY + 1}, types.CONDITION_ACTIVE),
        ({"download": 89 * GIB}, types.CONDITION_TRAFFIC_LOW),  # 90 % of 100 GiB used
        ({"download": 89 * GIB - 1}, types.CONDITION_ACTIVE),
        ({"status": types.STATUS_EXPIRED, "usable": False}, types.CONDITION_EXPIRED),
        ({"download": 99 * GIB, "usable": False}, types.CONDITION_TRAFFIC_USED_UP),
        ({"status": types.STATUS_DISABLED, "usable": False}, types.CONDITION_DISABLED),
        ({"start_at": NOW + DAY, "usable": False}, types.CONDITION_NOT_STARTED),
        ({"trial": True}, types.CONDITION_ACTIVE),  # 试用 is shown beside the condition
    ],
)
def test_the_statuses_shown(changes: dict[str, object], condition: str) -> None:
    shown = subscription(**changes)
    assert shown.condition(NOW) == condition
    assert shown.trial == bool(changes.get("trial"))


# -- reconciling ----------------------------------------------------------------------------------
def test_a_first_sync_adds_a_profile_for_each_subscription_at_the_end() -> None:
    local = Profile(id="local", name="本地配置", kind=KIND_FILE, path="/tmp/c.yaml")
    result = reconcile(
        ProfileList((local,), active="local"),
        [subscription(7), subscription(9, plan_name="基础版", usable=False)],
    )
    assert [p.id for p in result.profiles.profiles] == ["local", "new-7", "new-9"]
    assert result.profiles.active == "local"
    seven, nine = result.profiles.profiles[1:]
    assert (nine.name, nine.remote_id) == ("基础版", 9)
    assert (seven.name, seven.kind, seven.provider_id, seven.remote_id) == (
        "专业版",
        KIND_PROVIDER,
        "p1",
        7,
    )
    assert (seven.url, seven.path, seven.auto_update_hours, seven.updated_at) == ("", "", 12, 0)
    assert seven.usage == Usage(1 * GIB, 9 * GIB, 100 * GIB, int(NOW + 30 * DAY))
    assert result.added == ("new-7", "new-9")
    assert result.changed == ("new-7", "new-9")
    # Both are new; only the usable one is downloaded.
    assert result.refresh == ("new-7",)
    assert result.stored == {
        "new-7": subscription(7),
        "new-9": subscription(9, plan_name="基础版", usable=False),
    }
    assert result.missing == result.renamed == ()


def test_new_profiles_go_after_the_providers_last_one() -> None:
    profiles = ProfileList(
        (
            provider_profile("a", 7),
            Profile(id="mid", name="订阅", kind="subscription", url="https://s.example/x"),
            provider_profile("b", 9),
            provider_profile("other", 7, provider_id="p2"),
        )
    )
    result = reconcile(profiles, [subscription(7), subscription(9), subscription(12)])
    assert [p.id for p in result.profiles.profiles] == ["a", "mid", "b", "new-12", "other"]


def test_an_unchanged_subscription_with_a_config_is_left_alone() -> None:
    stored = subscription(7)
    profile = provider_profile("a", 7, name="专业版", usage=stored.usage)
    profiles = ProfileList((profile,), active="a")
    result = reconcile(profiles, [subscription(7, upload=2 * GIB)], {7: stored})
    # Billing's usage is copied; nothing else changes, so nothing is downloaded.
    assert result.profiles.get("a").usage == Usage(2 * GIB, 9 * GIB, 100 * GIB, stored.usage.expire)
    assert result.changed == result.refresh == result.renamed == result.added == ()


@pytest.mark.parametrize(
    "change",
    [
        {"plan_id": "max"},
        {"status": types.STATUS_EXPIRED},
        {"usable": False},
        {"updated_at": NOW},
        {"access": types.ACCESS_LINK},
    ],
)
def test_a_changed_subscription_is_updated_when_usable(change: dict[str, object]) -> None:
    profiles = ProfileList((provider_profile("a", 7, name="专业版"),))
    result = reconcile(profiles, [subscription(7, **change)], {7: subscription(7)})
    assert result.changed == ("a",)
    usable = not sync.blocked(subscription(7, **change), NOW)
    assert result.refresh == (("a",) if usable else ())


def test_a_usable_profile_without_a_config_is_downloaded() -> None:
    profiles = ProfileList((provider_profile("a", 7, name="专业版", updated_at=0.0),))
    result = reconcile(profiles, [subscription(7)], {7: subscription(7)})
    assert result.changed == ()
    assert result.refresh == ("a",)


def test_names_follow_billing() -> None:
    profiles = ProfileList((provider_profile("a", 7, name="Pro"), provider_profile("b", 9)))
    result = reconcile(
        profiles,
        [subscription(7), subscription(9)],
        {7: subscription(7), 9: subscription(9)},
    )
    assert result.renamed == ("a", "b")
    assert [p.name for p in result.profiles.profiles] == ["专业版 · #7", "专业版 · #9"]


def test_a_subscription_no_longer_listed_is_missing_and_kept() -> None:
    gone = provider_profile("b", 9, name="基础版")
    profiles = ProfileList((provider_profile("a", 7, name="专业版"), gone), active="b")
    result = reconcile(profiles, [subscription(7)], {7: subscription(7), 9: subscription(9)})
    assert result.missing == ("b",)
    assert result.profiles.get("b") == gone
    assert result.profiles.active == "b"
    assert "b" not in result.stored


def test_other_providers_and_other_kinds_are_untouched() -> None:
    other = provider_profile("x", 7, provider_id="p2", name="别家")
    local = Profile(id="local", name="本地", kind=KIND_FILE, path="/tmp/c.yaml")
    result = reconcile(ProfileList((other, local)), [])
    assert result.profiles.profiles == (other, local)
    assert result.missing == ()


# -- the next reset -------------------------------------------------------------------------------
def at(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def iso(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat()


@pytest.mark.parametrize(
    ("start", "period", "reset"),
    [
        # As user-web's tests: anchored to the purchase, clamped to the month's last day.
        ("2026-01-31T23:30:00+00:00", "2026-02-28T23:30:00+00:00", "2026-03-31T23:30:00+00:00"),
        ("2026-01-31T23:30:00+00:00", "2026-03-15T12:00:00+00:00", "2026-03-31T23:30:00+00:00"),
        ("2026-01-31T23:30:00+00:00", "2026-01-31T23:30:00+00:00", "2026-02-28T23:30:00+00:00"),
        ("2027-12-15T08:00:00+00:00", "2027-12-15T08:00:00+00:00", "2028-01-15T08:00:00+00:00"),
        ("2028-01-30T00:00:00+00:00", "2028-01-30T00:00:00+00:00", "2028-02-29T00:00:00+00:00"),
    ],
)
def test_the_next_reset_on_the_purchase_day(start: str, period: str, reset: str) -> None:
    shown = subscription(start_at=at(start), period_start=at(period))
    assert iso(sync.next_reset(shown)) == iso(at(reset))


def test_the_next_reset_at_the_start_of_a_month() -> None:
    china = timezone(timedelta(hours=8))
    shown = subscription(
        reset_mode=types.RESET_MONTH_START, period_start=at("2026-11-01T00:05:00+08:00")
    )
    assert sync.next_reset(shown, china) == at("2026-12-01T00:00:00+08:00")
    december = replace(shown, period_start=at("2026-12-01T00:05:00+08:00"))
    assert sync.next_reset(december, china) == at("2027-01-01T00:00:00+08:00")
    # The machine's own zone by default: midnight there.
    local = datetime.fromtimestamp(sync.next_reset(shown) or 0)
    assert (local.day, local.hour, local.minute) == (1, 0, 0)


def test_no_reset_without_one_or_without_dates() -> None:
    assert sync.next_reset(subscription(reset_mode=types.RESET_NONE)) is None
    assert sync.next_reset(subscription(start_at=0.0)) is None
    assert (
        sync.next_reset(subscription(reset_mode=types.RESET_MONTH_START, period_start=0.0)) is None
    )


# -- the default choice ---------------------------------------------------------------------------
def test_the_usable_one_with_most_traffic_left_is_suggested() -> None:
    small = subscription(7, total=10 * GIB, download=0, upload=0)
    large = subscription(9, total=500 * GIB)
    unlimited_short = subscription(11, total=None, expire_at=NOW + DAY)
    unlimited_long = subscription(12, total=None, expire_at=NOW + 9 * DAY)
    expired = subscription(13, status=types.STATUS_EXPIRED, usable=False, total=None)
    assert sync.most_left([small, large], NOW) == large
    assert sync.most_left([small, large, unlimited_short, unlimited_long, expired], NOW) == (
        unlimited_long
    )
    assert sync.most_left([expired], NOW) is None
    assert sync.most_left([], NOW) is None
    # Equal: the older one.
    assert sync.most_left([subscription(9), subscription(7)], NOW) == subscription(7)
