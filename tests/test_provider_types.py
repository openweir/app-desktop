"""The provider accounts' contract (docs/accounts.md, models/provider/types.py): values are
immutable, secrets stay out of repr and of what is stored, and the interfaces have the
methods the parts are built against."""

import dataclasses
import json
from typing import get_protocol_members

import pytest

from openweir.models.profiles import ProfileError, Usage
from openweir.models.provider import (
    Account,
    AccountsService,
    AccountSummary,
    Currency,
    Level,
    OrderSummary,
    Price,
    Probe,
    ProviderApi,
    ProviderError,
    ProviderRecord,
    ProviderState,
    PublicPlan,
    RemoteSubscription,
    Session,
    SignInOptions,
    Site,
    TwoFactorChallenge,
    Wallet,
    types,
)

ORIGIN = "https://example.com"
TOKEN = "secret-session-token"
LINK = "https://sub.example.com/api/v1/app/proxy/subscriptions/0123456789abcdef"
NOW = 1_800_000_000.0
DAY = 86400.0
GIB = 1024**3


def subscription(**changes: object) -> RemoteSubscription:
    base = RemoteSubscription(
        id=7,
        plan_id="pro",
        status=types.STATUS_ACTIVE,
        usable=True,
        plan_name="专业版",
        user_id="user-1",
        start_at=NOW - 30 * DAY,
        expire_at=NOW + 30 * DAY,
        period_start=NOW - 10 * DAY,
        traffic_limit=100 * GIB,
        extra_bytes=10 * GIB,
        upload=1 * GIB,
        download=9 * GIB,
        total=110 * GIB,
        device_limit=3,
        online_devices=2,
        access=types.ACCESS_BOTH,
        updated_at=NOW - DAY,
        url=LINK,
        urls=(LINK, LINK.replace("sub.", "mirror.")),
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def test_values_are_frozen() -> None:
    values = [
        Currency("CNY", "¥", 2),
        Site("示例服务商", Currency("CNY", "¥", 2)),
        SignInOptions((types.METHOD_EMAIL_PASSWORD,)),
        Account("user-1", "user@example.com"),
        Session(ORIGIN, TOKEN, Account("user-1", "user@example.com")),
        TwoFactorChallenge(ORIGIN, "user@example.com", (types.TWO_FACTOR_TOTP,), NOW),
        ProviderRecord("p1", ORIGIN, "示例服务商", "user@example.com", "user-1"),
        subscription(),
        Wallet("CNY", balance=1500, level=Level("gold", "黄金", discount_percent=5)),
        PublicPlan("pro", types.PLAN_SUBSCRIPTION, "专业版", "CNY", (Price("month", 1500),)),
        OrderSummary("2026112300001", types.ORDER_NEW, types.ORDER_PENDING, "CNY", 1500, NOW),
    ]
    for value in values:
        first = dataclasses.fields(value)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, first, None)


def test_secrets_stay_out_of_repr() -> None:
    session = Session(ORIGIN, TOKEN, Account("user-1", "user@example.com"))
    assert TOKEN not in repr(session)
    assert "user@example.com" in repr(session)
    assert session.user_id == "user-1"

    challenge = TwoFactorChallenge(
        ORIGIN, "user@example.com", ("totp",), NOW, (("better-auth.two_factor", "signed"),)
    )
    assert "signed" not in repr(challenge)
    assert "two_factor" not in repr(challenge)

    remote = subscription()
    assert LINK not in repr(remote)
    assert "mirror." not in repr(remote)
    # Stored copies have no links, and still equal what was read.
    assert remote == dataclasses.replace(remote, url=None, urls=())


def test_the_stored_record_has_no_links_and_round_trips() -> None:
    remote = subscription()
    record = remote.record()
    assert "url" not in record
    assert "urls" not in record
    text = json.dumps(record, ensure_ascii=False)
    assert "sub.example.com" not in text
    restored = RemoteSubscription.from_record(json.loads(text))
    assert restored == remote
    assert restored.url is None
    assert restored.urls == ()
    # Keys of later versions are left out; a missing field it needs is an error.
    assert RemoteSubscription.from_record({**record, "later": 1, "url": LINK}).url is None
    with pytest.raises(TypeError):
        RemoteSubscription.from_record({"id": 7})


def test_billing_usage_reads_like_the_userinfo_header() -> None:
    assert subscription().usage == Usage(1 * GIB, 9 * GIB, 110 * GIB, int(NOW + 30 * DAY))
    assert subscription(total=None, expire_at=None).usage == Usage(1 * GIB, 9 * GIB, 0, 0)


@pytest.mark.parametrize(
    ("changes", "condition"),
    [
        ({}, types.CONDITION_ACTIVE),
        ({"expire_at": NOW + 2 * DAY}, types.CONDITION_EXPIRING),
        ({"download": 99 * GIB}, types.CONDITION_TRAFFIC_LOW),
        ({"status": types.STATUS_DISABLED, "usable": False}, types.CONDITION_DISABLED),
        ({"status": types.STATUS_EXPIRED, "usable": False}, types.CONDITION_EXPIRED),
        ({"expire_at": NOW - DAY, "usable": False}, types.CONDITION_EXPIRED),
        ({"download": 109 * GIB, "usable": False}, types.CONDITION_TRAFFIC_USED_UP),
        ({"start_at": NOW + DAY, "usable": False}, types.CONDITION_NOT_STARTED),
        ({"usable": False}, types.CONDITION_UNUSABLE),
        # The server's word wins over this clock.
        ({"expire_at": NOW - DAY}, types.CONDITION_EXPIRING),
        ({"total": None, "expire_at": None}, types.CONDITION_ACTIVE),
    ],
)
def test_the_condition_shown(changes: dict[str, object], condition: str) -> None:
    assert subscription(**changes).condition(NOW) == condition


def test_sign_in_options() -> None:
    both = SignInOptions((types.METHOD_EMAIL_PASSWORD, types.METHOD_EMAIL_CODE))
    assert both.password
    assert both.email_code
    assert not both.username
    assert both.unavailable == ""

    username = SignInOptions((types.METHOD_USERNAME,))
    assert username.password
    assert username.username

    guarded = SignInOptions(
        (types.METHOD_EMAIL_PASSWORD, types.METHOD_EMAIL_CODE),
        (types.CAPTCHA_SIGN_IN,),
    )
    assert not guarded.password
    assert guarded.email_code
    assert guarded.unavailable == ""

    captcha = dataclasses.replace(guarded, captcha=(types.CAPTCHA_SIGN_IN, types.CAPTCHA_SEND_CODE))
    assert captcha.unavailable == types.PROVIDER_CAPTCHA_REQUIRED

    social = SignInOptions(("github", "passkey", types.METHOD_TWO_FACTOR))
    assert social.unavailable == types.PROVIDER_SIGN_IN_UNSUPPORTED


def test_the_account_shown() -> None:
    assert Account("u", "user@example.com", "User", "user").display == "user@example.com"
    phone = Account("u", "8613800000000@phone.invalid", "Phone", "")
    assert phone.display == "Phone"
    assert Account("u", "x@phone.invalid", "", "handle").display == "handle"


def test_provider_errors_are_profile_errors() -> None:
    error = ProviderError(types.PROVIDER_RATE_LIMITED, "HTTP 429", retry_after=30.0)
    assert isinstance(error, ProfileError)
    assert error.code == "provider_rate_limited"
    assert error.message == "HTTP 429"
    assert error.retry_after == 30.0
    assert str(error) == "provider_rate_limited: HTTP 429"
    assert ProviderError(types.PROVIDER_UNREACHABLE, "timed out").retry_after is None


def test_the_codes() -> None:
    planned = {
        "provider_invalid_address",
        "provider_not_openweir",
        "provider_unreachable",
        "provider_rate_limited",
        "provider_bad_response",
        "provider_sign_in_failed",
        "provider_email_unverified",
        "provider_code_invalid",
        "provider_two_factor_invalid",
        "provider_captcha_required",
        "provider_account_disabled",
        "provider_sign_in_required",
        "provider_subscription_expired",
        "provider_subscription_unusable",
        "provider_subscription_disabled",
        "provider_subscription_missing",
        "provider_link_unavailable",
    }
    assert planned <= types.PROVIDER_CODES
    constants = {
        v for k, v in vars(types).items() if k.startswith("PROVIDER_") and k != "PROVIDER_CODES"
    }
    assert constants == types.PROVIDER_CODES
    assert all(code.startswith("provider_") for code in types.PROVIDER_CODES)
    # The host's code for mihomo's proxy providers is another thing.
    assert "provider_update_failed" not in types.PROVIDER_CODES
    assert types.KEEPS_CONFIG <= types.PROVIDER_CODES


def test_states() -> None:
    assert [state.value for state in ProviderState] == [
        "signed_in",
        "sign_in_required",
        "account_disabled",
        "credentials_unavailable",
    ]
    record = ProviderRecord("p1", ORIGIN, "示例服务商", "user@example.com", "user-1")
    summary = AccountSummary(record, ProviderState.SIGN_IN_REQUIRED)
    assert summary.subscriptions == ()
    assert summary.error == ""
    assert (
        Probe(ORIGIN, Site("示例服务商", Currency("CNY", "¥", 2)), SignInOptions()).provider is None
    )


def test_order_pending() -> None:
    order = OrderSummary("1", types.ORDER_RENEW, types.ORDER_PENDING, "CNY", 1500, NOW)
    assert order.pending
    assert not dataclasses.replace(order, status=types.ORDER_COMPLETED).pending


def test_the_interfaces() -> None:
    assert get_protocol_members(ProviderApi) == {
        "site",
        "sign_in_options",
        "sign_in_password",
        "send_email_code",
        "sign_in_email_code",
        "verify_totp",
        "account",
        "sign_out",
        "subscriptions",
        "subscription",
        "set_auto_renew",
        "wallet",
        "plans",
        "orders",
        "config",
        "link",
    }
    assert get_protocol_members(AccountsService) == {
        "summaries",
        "summary",
        "probe",
        "sign_in_password",
        "send_email_code",
        "sign_in_email_code",
        "verify_two_factor",
        "complete",
        "use",
        "sync",
        "sync_all",
        "sign_out_removal",
        "sign_out",
        "fetch",
        "wallet",
        "plans",
        "orders",
        "set_auto_renew",
    }
