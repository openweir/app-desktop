"""The provider HTTP client (models/provider/api.py, docs/accounts.md §3.2 and §10) against a
stand-in for api-edge on 127.0.0.1: what each call sends and reads, how refusals become
ProviderError codes, and that the token goes to the origin only, never follows a redirect and
never reaches an error; and the user center's links (models/provider/web.py)."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import pytest

from openweir.models.provider import Session, TwoFactorChallenge, api, types, web
from openweir.models.provider.api import HttpProviderApi, origin
from openweir.models.provider.types import ProviderError
from openweir.models.subscription import USER_AGENT as CONFIG_USER_AGENT
from tests.support.provider_server import (
    EMAIL,
    GIB,
    NOW,
    PASSWORD,
    TOTP,
    TWO_FACTOR_COOKIE,
    USERNAME,
    FakeProvider,
)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    # Reach the loopback servers directly even if the machine has a proxy configured.
    monkeypatch.setenv("no_proxy", "*")
    fake = FakeProvider()
    yield fake
    fake.close()


@pytest.fixture
def elsewhere(provider: FakeProvider) -> Iterator[FakeProvider]:
    """Another host, where redirects point: nothing may ever reach it."""
    fake = FakeProvider(seed=False)
    yield fake
    fake.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(clock: Clock) -> HttpProviderApi:
    return HttpProviderApi(language=lambda: "zh", clock=clock)


def failure(call: Callable[[], Coroutine[Any, Any, object]]) -> ProviderError:
    with pytest.raises(ProviderError) as caught:
        run(call)
    return caught.value


def assert_clean(error: ProviderError, *secrets: str) -> None:
    """The error holds no URL, token or other secret, in its text or anything chained."""
    assert error.code in types.PROVIDER_CODES
    assert error.__cause__ is None
    assert error.__context__ is None
    for text in (str(error), repr(error), error.message):
        assert "://" not in text
        assert "127.0.0.1" not in text
        for secret in secrets:
            assert secret not in text


def sign_in(client: HttpProviderApi, provider: FakeProvider) -> Session:
    session = run(lambda: client.sign_in_password(provider.base, EMAIL, PASSWORD))
    assert isinstance(session, Session)
    return session


# -- the address ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("example.com", "https://example.com"),
        ("  https://Example.COM/path/?query=1#fragment ", "https://example.com"),
        ("HTTPS://example.com:443", "https://example.com"),
        ("example.com:8443/user", "https://example.com:8443"),
        ("http://localhost:8787/", "http://localhost:8787"),
        ("http://127.0.0.1:80", "http://127.0.0.1"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        ("https://[2001:db8::1]", "https://[2001:db8::1]"),
        ("例子.测试", "https://xn--fsqu00a.xn--0zwm56d"),
    ],
)
def test_origin_keeps_scheme_host_and_port(address: str, expected: str) -> None:
    assert origin(address) == expected


@pytest.mark.parametrize(
    "address",
    [
        "",
        "   ",
        "http://example.com",
        "http://localhost.example.com",
        "ftp://example.com",
        "https://user:secret@example.com",
        "https://user@example.com",
        "https://",
        "https://example.com:99999",
        "https://example.com:0",
        "https://exa mple.com",
        "https://example.com\\@evil.example",
        "https://exam\nple.com",
        "javascript:alert(1)",
    ],
)
def test_origin_refuses_what_is_no_https_origin(address: str) -> None:
    with pytest.raises(ProviderError) as caught:
        origin(address)
    assert caught.value.code == types.PROVIDER_INVALID_ADDRESS
    assert "secret" not in str(caught.value)
    assert "example" not in str(caught.value)


def test_calls_take_origins_only(client: HttpProviderApi, provider: FakeProvider) -> None:
    error = failure(lambda: client.site(provider.base + "/api"))
    assert error.code == types.PROVIDER_INVALID_ADDRESS
    assert provider.requests == []


# -- probing --------------------------------------------------------------------------------------
def test_site_reads_the_panel_and_sends_the_app_headers(
    client: HttpProviderApi, provider: FakeProvider
) -> None:
    site = run(lambda: client.site(provider.base))
    assert site == types.Site(
        "示例服务商", types.Currency("CNY", "¥", 2), "测试用的服务商", "zh-CN"
    )
    [request] = provider.requests
    assert request.header("user-agent") == api.USER_AGENT
    assert api.USER_AGENT.startswith("OpenWeir/")
    assert not api.USER_AGENT.endswith("clash.meta")
    assert request.header("accept-language") == "zh-CN"
    assert request.header("accept") == "application/json"
    assert request.header("authorization") is None
    assert request.header("cookie") is None


def test_accept_language_follows_the_ui_language(provider: FakeProvider) -> None:
    language = ["en"]
    client = HttpProviderApi(language=lambda: language[0])
    run(lambda: client.site(provider.base))
    language[0] = "zh"
    run(lambda: client.site(provider.base))
    assert [r.header("accept-language") for r in provider.requests] == ["en-US", "zh-CN"]


@pytest.mark.parametrize(
    ("status", "payload", "code"),
    [
        (404, {"code": "NOT_FOUND", "message": "Not found"}, types.PROVIDER_NOT_OPENWEIR),
        (200, b"<!doctype html><title>Blog</title>", types.PROVIDER_NOT_OPENWEIR),
        (200, {"name": "Blog"}, types.PROVIDER_NOT_OPENWEIR),
        (200, [1, 2, 3], types.PROVIDER_NOT_OPENWEIR),
        (401, {"code": "UNAUTHORIZED"}, types.PROVIDER_NOT_OPENWEIR),
        (503, {"code": "NOT_INSTALLED"}, types.PROVIDER_UNREACHABLE),
    ],
)
def test_an_address_that_is_no_panel(
    client: HttpProviderApi, provider: FakeProvider, status: int, payload: Any, code: str
) -> None:
    provider.answer("site", status, payload)
    error = failure(lambda: client.site(provider.base))
    assert error.code == code
    assert_clean(error)


def test_sign_in_options(client: HttpProviderApi, provider: FakeProvider) -> None:
    options = run(lambda: client.sign_in_options(provider.base))
    assert options.methods == ("email_password", "username", "email_otp", "two_factor")
    assert options.captcha == ()
    assert options.code_length == 6
    assert options.password
    assert options.email_code
    assert options.username


def test_sign_in_options_with_a_captcha(client: HttpProviderApi, provider: FakeProvider) -> None:
    provider.scenes = ["sign_in", "send_code"]
    provider.methods[2]["options"] = {"otpLength": 8}
    options = run(lambda: client.sign_in_options(provider.base))
    assert options.captcha == ("sign_in", "send_code")
    assert options.code_length == 8
    assert options.unavailable == types.PROVIDER_CAPTCHA_REQUIRED


def test_sign_in_options_without_the_captcha_module(
    client: HttpProviderApi, provider: FakeProvider
) -> None:
    provider.fail("captcha", 404, "NOT_FOUND")
    provider.methods = [{"id": "gh", "type": "github", "name": "GitHub", "category": "social"}]
    options = run(lambda: client.sign_in_options(provider.base))
    assert options.captcha == ()
    assert options.unavailable == types.PROVIDER_SIGN_IN_UNSUPPORTED


# -- signing in -----------------------------------------------------------------------------------
def test_password_sign_in_by_email(client: HttpProviderApi, provider: FakeProvider) -> None:
    session = sign_in(client, provider)
    assert session.origin == provider.base
    assert session.account == types.Account("user-1", EMAIL, "Example User", USERNAME)
    [request] = provider.requests
    assert request.route == "sign_in_email"
    assert request.json() == {"email": EMAIL, "password": PASSWORD}
    assert request.header("origin") == provider.base
    assert request.header("content-type") == "application/json"
    assert request.header("authorization") is None
    assert request.header("user-agent") == api.USER_AGENT
    # The token is set-auth-token, Better Auth's signed one, and works as a bearer.
    assert "." in session.token
    assert session.token not in repr(session)
    run(lambda: client.account(provider.base, session.token))


def test_password_sign_in_by_user_name(client: HttpProviderApi, provider: FakeProvider) -> None:
    session = run(lambda: client.sign_in_password(provider.base, f" {USERNAME} ", PASSWORD))
    assert isinstance(session, Session)
    assert session.user_id == "user-1"
    [request] = provider.requests
    assert request.route == "sign_in_username"
    assert request.json() == {"username": USERNAME, "password": PASSWORD}


def test_the_token_falls_back_to_the_answers_body(
    client: HttpProviderApi, provider: FakeProvider
) -> None:
    provider.header_token = False
    session = sign_in(client, provider)
    assert "." not in session.token
    assert run(lambda: client.account(provider.base, session.token)).id == "user-1"


@pytest.mark.parametrize(
    ("identifier", "password", "code"),
    [
        (EMAIL, "wrong password", types.PROVIDER_SIGN_IN_FAILED),
        ("nobody@example.com", PASSWORD, types.PROVIDER_SIGN_IN_FAILED),
        ("nobody", PASSWORD, types.PROVIDER_SIGN_IN_FAILED),
        ("@", PASSWORD, types.PROVIDER_SIGN_IN_FAILED),
    ],
)
def test_wrong_credentials(
    client: HttpProviderApi, provider: FakeProvider, identifier: str, password: str, code: str
) -> None:
    error = failure(lambda: client.sign_in_password(provider.base, identifier, password))
    assert error.code == code
    assert_clean(error, password)


def test_a_disabled_or_unverified_account(client: HttpProviderApi, provider: FakeProvider) -> None:
    provider.users["user-1"].banned = True
    error = failure(lambda: client.sign_in_password(provider.base, EMAIL, PASSWORD))
    assert error.code == types.PROVIDER_ACCOUNT_DISABLED
    provider.users["user-1"].banned = False
    provider.users["user-1"].email_verified = False
    provider.require_email_verification = True
    error = failure(lambda: client.sign_in_password(provider.base, EMAIL, PASSWORD))
    assert error.code == types.PROVIDER_EMAIL_UNVERIFIED


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, "MISSING_RESPONSE", types.PROVIDER_CAPTCHA_REQUIRED),
        (403, "VERIFICATION_FAILED", types.PROVIDER_CAPTCHA_REQUIRED),
        (400, "EMAIL_PASSWORD_DISABLED", types.PROVIDER_SIGN_IN_UNSUPPORTED),
        (404, "NOT_FOUND", types.PROVIDER_SIGN_IN_UNSUPPORTED),
        (403, "FORBIDDEN", types.PROVIDER_ACCOUNT_DISABLED),
        (403, "INVALID_ORIGIN", types.PROVIDER_BAD_RESPONSE),
        (400, "VALIDATION_ERROR", types.PROVIDER_SIGN_IN_FAILED),
        (401, None, types.PROVIDER_SIGN_IN_FAILED),
        (429, "RATE_LIMITED", types.PROVIDER_RATE_LIMITED),
        (500, "UNKNOWN_ERROR", types.PROVIDER_UNREACHABLE),
        (418, None, types.PROVIDER_BAD_RESPONSE),
    ],
)
def test_sign_in_refusals(
    client: HttpProviderApi, provider: FakeProvider, status: int, code: str | None, expected: str
) -> None:
    provider.fail("sign_in_email", status, code)
    error = failure(lambda: client.sign_in_password(provider.base, EMAIL, PASSWORD))
    assert error.code == expected
    assert error.message == (f"HTTP {status} {code}" if code else f"HTTP {status}")


def test_a_captcha_on_sign_in(client: HttpProviderApi, provider: FakeProvider) -> None:
    provider.scenes = ["sign_in"]
    error = failure(lambda: client.sign_in_password(provider.base, EMAIL, PASSWORD))
    assert error.code == types.PROVIDER_CAPTCHA_REQUIRED


def test_two_factor_sign_in(client: HttpProviderApi, provider: FakeProvider, clock: Clock) -> None:
    provider.users["user-1"].totp = TOTP
    challenge = run(lambda: client.sign_in_password(provider.base, EMAIL, PASSWORD))
    assert isinstance(challenge, TwoFactorChallenge)
    assert challenge.origin == provider.base
    assert challenge.identifier == EMAIL
    assert challenge.methods == ("totp",)
    assert challenge.expires_at == NOW + types.TWO_FACTOR_TTL_S
    # Only the two-factor cookie: the answer deleted the session cookie.
    [(name, value)] = challenge.cookies
    assert name == TWO_FACTOR_COOKIE
    assert value not in repr(challenge)

    error = failure(lambda: client.verify_totp(challenge, "000000"))
    assert error.code == types.PROVIDER_TWO_FACTOR_INVALID
    assert_clean(error, value)

    clock.now += 60
    session = run(lambda: client.verify_totp(challenge, f" {TOTP} "))
    assert session.account.id == "user-1"
    assert session.origin == provider.base
    verify = provider.requests_for("verify_totp")
    assert len(verify) == 2
    for request in verify:
        assert request.header("cookie") == f"{name}={value}"
        assert request.header("origin") == provider.base
        assert request.header("authorization") is None
    assert verify[-1].json() == {"code": TOTP}
    run(lambda: client.subscriptions(provider.base, session.token))


def test_two_factor_without_the_user_reads_me(
    client: HttpProviderApi, provider: FakeProvider
) -> None:
    provider.users["user-1"].totp = TOTP
    provider.user_after_two_factor = False
    challenge = run(lambda: client.sign_in_password(provider.base, EMAIL, PASSWORD))
    assert isinstance(challenge, TwoFactorChallenge)
    session = run(lambda: client.verify_totp(challenge, TOTP))
    assert session.account == types.Account("user-1", EMAIL, "Example User")
    [me] = provider.requests_for("me")
    assert me.bearer == session.token


def test_two_factor_expires(client: HttpProviderApi, provider: FakeProvider, clock: Clock) -> None:
    provider.users["user-1"].totp = TOTP
    challenge = run(lambda: client.sign_in_password(provider.base, EMAIL, PASSWORD))
    assert isinstance(challenge, TwoFactorChallenge)
    # The provider's cookie ran out first.
    provider.now += types.TWO_FACTOR_TTL_S
    error = failure(lambda: client.verify_totp(challenge, TOTP))
    assert error.code == types.PROVIDER_TWO_FACTOR_EXPIRED
    # Past its expiry here, nothing is sent.
    asked = len(provider.requests)
    clock.now = challenge.expires_at
    error = failure(lambda: client.verify_totp(challenge, TOTP))
    assert error.code == types.PROVIDER_TWO_FACTOR_EXPIRED
    assert len(provider.requests) == asked


def test_a_two_factor_answer_without_a_cookie(
    client: HttpProviderApi, provider: FakeProvider
) -> None:
    provider.answer("sign_in_email", 200, {"twoFactorRedirect": True, "twoFactorMethods": []})
    error = failure(lambda: client.sign_in_password(provider.base, EMAIL, PASSWORD))
    assert error.code == types.PROVIDER_BAD_RESPONSE


def test_email_code_sign_in(client: HttpProviderApi, provider: FakeProvider) -> None:
    run(lambda: client.send_email_code(provider.base, f" {EMAIL} "))
    [send] = provider.requests
    assert send.json() == {"email": EMAIL, "type": "sign-in"}
    assert send.header("origin") == provider.base
    code = provider.codes[EMAIL]

    error = failure(lambda: client.sign_in_email_code(provider.base, EMAIL, "999999x"))
    assert error.code == types.PROVIDER_CODE_INVALID
    assert_clean(error, "999999x")

    session = run(lambda: client.sign_in_email_code(provider.base, EMAIL, code))
    assert isinstance(session, Session)
    assert session.user_id == "user-1"
    assert provider.requests_for("sign_in_code")[-1].json() == {"email": EMAIL, "otp": code}
    error = failure(lambda: client.sign_in_email_code(provider.base, EMAIL, code))
    assert error.code == types.PROVIDER_CODE_INVALID  # used up: OTP_EXPIRED


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (403, "TOO_MANY_ATTEMPTS", types.PROVIDER_CODE_INVALID),
        (400, "OTP_EXPIRED", types.PROVIDER_CODE_INVALID),
        (400, "VALIDATION_ERROR", types.PROVIDER_CODE_INVALID),
    ],
)
def test_email_code_refusals(
    client: HttpProviderApi, provider: FakeProvider, status: int, code: str, expected: str
) -> None:
    provider.fail("sign_in_code", status, code)
    error = failure(lambda: client.sign_in_email_code(provider.base, EMAIL, "123456"))
    assert error.code == expected


def test_sending_a_code_behind_a_captcha(client: HttpProviderApi, provider: FakeProvider) -> None:
    provider.scenes = ["send_code"]
    error = failure(lambda: client.send_email_code(provider.base, EMAIL))
    assert error.code == types.PROVIDER_CAPTCHA_REQUIRED
    error = failure(lambda: client.send_email_code(provider.base, "not an address"))
    assert error.code == types.PROVIDER_CAPTCHA_REQUIRED
    provider.scenes = []
    error = failure(lambda: client.send_email_code(provider.base, "not an address"))
    assert error.code == types.PROVIDER_SIGN_IN_FAILED


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("INVALID_TWO_FACTOR_COOKIE", types.PROVIDER_TWO_FACTOR_EXPIRED),
        ("TOO_MANY_ATTEMPTS_REQUEST_NEW_CODE", types.PROVIDER_TWO_FACTOR_INVALID),
        ("INVALID_CODE", types.PROVIDER_TWO_FACTOR_INVALID),
    ],
)
def test_two_factor_refusals(
    client: HttpProviderApi, provider: FakeProvider, code: str, expected: str
) -> None:
    challenge = TwoFactorChallenge(
        provider.base, EMAIL, ("totp",), NOW + 600, ((TWO_FACTOR_COOKIE, "2fa-x.y"),)
    )
    provider.fail("verify_totp", 401 if code != "TOO_MANY_ATTEMPTS_REQUEST_NEW_CODE" else 400, code)
    assert failure(lambda: client.verify_totp(challenge, TOTP)).code == expected


def test_sign_out(client: HttpProviderApi, provider: FakeProvider) -> None:
    session = sign_in(client, provider)
    run(lambda: client.sign_out(provider.base, session.token))
    [request] = provider.requests_for("sign_out")
    assert request.bearer == session.token
    assert request.header("origin") == provider.base
    assert provider.sessions == {}
    # Signed out already: 401 is no error.
    run(lambda: client.sign_out(provider.base, session.token))
    provider.fail("sign_out", 503)
    error = failure(lambda: client.sign_out(provider.base, session.token))
    assert error.code == types.PROVIDER_UNREACHABLE
    assert_clean(error, session.token)


# -- billing --------------------------------------------------------------------------------------
def test_subscriptions(client: HttpProviderApi, provider: FakeProvider) -> None:
    session = sign_in(client, provider)
    subscriptions = run(lambda: client.subscriptions(provider.base, session.token))
    assert [s.id for s in subscriptions] == [7, 9]  # by id, though listed newest first
    pro, basic = subscriptions
    assert pro.plan_id == "pro"
    assert pro.plan_name == "专业版"
    assert pro.status == types.STATUS_ACTIVE
    assert pro.usable
    assert pro.user_id == "user-1"
    assert pro.access == types.ACCESS_BOTH
    assert pro.expire_at == NOW + 30 * 86400
    assert pro.start_at == NOW - 30 * 86400
    assert pro.period_start == NOW - 10 * 86400
    assert pro.updated_at == NOW - 86400
    assert (pro.traffic_limit, pro.total, pro.upload, pro.download) == (
        100 * GIB,
        100 * GIB,
        GIB,
        9 * GIB,
    )
    assert (pro.device_limit, pro.online_devices) == (3, 2)
    assert pro.url == provider.link(7)
    assert pro.urls == (provider.link(7),)
    assert basic.condition(NOW) == types.CONDITION_EXPIRED
    [request] = provider.requests_for("subscriptions")
    assert request.bearer == session.token
    assert request.header("accept-language") == "zh-CN"
    assert request.header("user-agent") == api.USER_AGENT


def test_subscriptions_without_links_while_app_only(
    client: HttpProviderApi, provider: FakeProvider
) -> None:
    provider.access = "app"
    session = sign_in(client, provider)
    pro = run(lambda: client.subscription(provider.base, session.token, 7))
    assert (pro.access, pro.url, pro.urls) == (types.ACCESS_APP, None, ())


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"items": []}, "no list"),
        ("change", "duplicate ids"),
        ("many", "more than 128"),
        ([{"id": 1, "planId": "pro", "status": "active"}], "usable"),
        ([{"id": "1", "planId": "pro", "status": "active", "usable": True}], "id"),
        (
            [{"id": 1, "planId": "pro", "status": "active", "usable": True, "expireAt": "soon"}],
            "expireAt",
        ),
        ([{"id": 1, "planId": "pro", "status": "active", "usable": True, "total": True}], "total"),
    ],
)
def test_subscriptions_that_make_no_sense(
    client: HttpProviderApi, provider: FakeProvider, payload: Any, reason: str
) -> None:
    session = sign_in(client, provider)
    item = {"id": 1, "planId": "pro", "status": "active", "usable": True}
    if payload == "change":
        payload = [item, item]
    elif payload == "many":
        payload = [{**item, "id": n} for n in range(1, types.MAX_SUBSCRIPTIONS + 2)]
    provider.answer("subscriptions", 200, payload)
    error = failure(lambda: client.subscriptions(provider.base, session.token))
    assert error.code == types.PROVIDER_BAD_RESPONSE
    assert reason in error.message


def test_one_subscription_and_auto_renew(client: HttpProviderApi, provider: FakeProvider) -> None:
    session = sign_in(client, provider)
    error = failure(lambda: client.subscription(provider.base, session.token, 11))
    assert error.code == types.PROVIDER_SUBSCRIPTION_MISSING  # another user's
    error = failure(lambda: client.subscription(provider.base, session.token, 0))
    assert error.code == types.PROVIDER_SUBSCRIPTION_MISSING
    assert provider.requests_for("subscription")[-1].path.endswith("/11")

    renewed = run(lambda: client.set_auto_renew(provider.base, session.token, 7, True))
    assert renewed.auto_renew
    assert renewed.access == ""  # PATCH answers without subscriptionAccess
    [request] = provider.requests_for("auto_renew")
    assert request.method == "PATCH"
    assert request.json() == {"autoRenew": True}
    assert request.bearer == session.token
    assert request.header("origin") is None
    error = failure(lambda: client.set_auto_renew(provider.base, session.token, 11, False))
    assert error.code == types.PROVIDER_SUBSCRIPTION_MISSING


def test_wallet_plans_and_orders(client: HttpProviderApi, provider: FakeProvider) -> None:
    session = sign_in(client, provider)
    wallet = run(lambda: client.wallet(provider.base, session.token))
    assert wallet.currency == "CNY"
    assert (wallet.balance, wallet.points, wallet.total_spent, wallet.paid_days) == (
        1500,
        120,
        9900,
        90,
    )
    assert wallet.level == types.Level("silver", "白银", 5, 5000, None)
    assert wallet.next_level == types.Level("gold", "黄金", 10, 20000, 365)
    assert wallet.user_id == "user-1"

    plans = run(lambda: client.plans(provider.base, None))
    assert [p.id for p in plans] == ["pro", "pack-50"]
    pro, pack = plans
    assert pro.prices == (types.Price("month", 1500), types.Price("year", 15000))
    assert (pro.currency, pro.traffic_bytes, pro.device_limit, pro.tags) == (
        "CNY",
        100 * GIB,
        3,
        ("推荐",),
    )
    assert pack.kind == types.PLAN_TRAFFIC_PACK
    assert pack.sold_out
    assert pack.description == ""
    run(lambda: client.plans(provider.base, session.token))
    public, signed_in = provider.requests_for("plans")
    assert public.header("authorization") is None
    assert signed_in.bearer == session.token

    orders = run(lambda: client.orders(provider.base, session.token, 5))
    assert [o.id for o in orders] == ["o-3", "o-1"]  # no withdrawal
    assert orders[0].pending
    assert orders[0].plan_id == "pro"
    assert orders[0].paid_at is None
    assert orders[1].paid_at == NOW - 86400
    [request] = provider.requests_for("orders")
    assert request.query == "page=1&pageSize=5"
    run(lambda: client.orders(provider.base, session.token, 1000))
    assert provider.requests_for("orders")[-1].query == "page=1&pageSize=100"


def test_withdrawals_stay_out_of_orders(client: HttpProviderApi, provider: FakeProvider) -> None:
    session = sign_in(client, provider)
    withdrawal = provider.order("w-1", "withdrawal", "completed")
    provider.answer("orders", 200, {"items": [withdrawal], "total": 1})
    assert run(lambda: client.orders(provider.base, session.token, 5)) == ()


@pytest.mark.parametrize("route", ["me", "subscriptions", "wallet", "orders"])
def test_a_session_that_ended(client: HttpProviderApi, provider: FakeProvider, route: str) -> None:
    session = sign_in(client, provider)
    provider.revoke()
    calls: dict[str, Callable[[], Coroutine[Any, Any, object]]] = {
        "me": lambda: client.account(provider.base, session.token),
        "subscriptions": lambda: client.subscriptions(provider.base, session.token),
        "wallet": lambda: client.wallet(provider.base, session.token),
        "orders": lambda: client.orders(provider.base, session.token, 5),
    }
    error = failure(calls[route])
    assert error.code == types.PROVIDER_SIGN_IN_REQUIRED
    assert_clean(error, session.token)


def test_a_disabled_account_on_billing(client: HttpProviderApi, provider: FakeProvider) -> None:
    session = sign_in(client, provider)
    provider.fail("subscriptions", 403, "FORBIDDEN")
    error = failure(lambda: client.subscriptions(provider.base, session.token))
    assert error.code == types.PROVIDER_ACCOUNT_DISABLED
    provider.fail("wallet", 403, "CROSS_SITE_REQUEST")
    assert failure(lambda: client.wallet(provider.base, session.token)).code == (
        types.PROVIDER_BAD_RESPONSE
    )


def test_a_malformed_token_is_never_sent(client: HttpProviderApi, provider: FakeProvider) -> None:
    error = failure(lambda: client.subscriptions(provider.base, "token\r\nX-Evil: 1"))
    assert error.code == types.PROVIDER_SIGN_IN_REQUIRED
    assert provider.requests == []


# -- configs --------------------------------------------------------------------------------------
def test_config_signed_in(client: HttpProviderApi, provider: FakeProvider) -> None:
    session = sign_in(client, provider)
    download = run(lambda: client.config(provider.base, session.token, 7))
    assert download.body == provider.config
    assert download.interval == 24
    assert download.usage == types.Usage(GIB, 9 * GIB, 100 * GIB, 1802592000)
    assert download.filename == "示例服务商"
    assert download.etag is None
    [request] = provider.requests_for("config")
    assert request.path == "/api/v1/app/proxy/subscriptions/7/config"
    assert request.bearer == session.token
    assert request.header("user-agent") == CONFIG_USER_AGENT
    assert request.header("accept-language") == "zh-CN"
    assert request.header("accept") is None  # a config, not JSON


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"access": "link"}, types.PROVIDER_APP_DISABLED),
        ({"remote_id": 11}, types.PROVIDER_SUBSCRIPTION_MISSING),
        ({"remote_id": 404}, types.PROVIDER_SUBSCRIPTION_MISSING),
        ({"status": "disabled"}, types.PROVIDER_SUBSCRIPTION_MISSING),
        ({"revoke": True}, types.PROVIDER_SIGN_IN_REQUIRED),
    ],
)
def test_config_refusals(
    client: HttpProviderApi, provider: FakeProvider, change: dict[str, Any], code: str
) -> None:
    session = sign_in(client, provider)
    provider.access = change.get("access", provider.access)
    if "status" in change:
        provider.update_subscription(7, status=change["status"])
    if change.get("revoke"):
        provider.revoke()
    remote_id = change.get("remote_id", 7)
    error = failure(lambda: client.config(provider.base, session.token, remote_id))
    assert error.code == code
    assert_clean(error, session.token)


def test_a_config_too_large(
    client: HttpProviderApi, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = sign_in(client, provider)
    monkeypatch.setattr(api, "MAX_CONFIG_BYTES", 1024)
    provider.config = b"#" * 1025
    error = failure(lambda: client.config(provider.base, session.token, 7))
    assert error.code == types.PROVIDER_BAD_RESPONSE
    provider.config = b"#" * 1024
    assert len(run(lambda: client.config(provider.base, session.token, 7)).body) == 1024


def test_link_download_carries_no_token(client: HttpProviderApi, provider: FakeProvider) -> None:
    session = sign_in(client, provider)
    url = run(lambda: client.subscription(provider.base, session.token, 7)).url
    assert url is not None
    download = run(lambda: client.link(url))
    assert download.body == provider.config
    assert download.interval == 24
    [request] = provider.requests_for("link")
    assert request.header("authorization") is None
    assert request.header("cookie") is None
    assert request.header("origin") is None
    assert request.header("accept") is None
    assert request.header("user-agent") == CONFIG_USER_AGENT


def test_link_refusals(client: HttpProviderApi, provider: FakeProvider) -> None:
    url = provider.link(7)
    token = url.rsplit("/", 1)[1]
    provider.access = "app"
    error = failure(lambda: client.link(url))
    assert error.code == types.PROVIDER_LINK_UNAVAILABLE
    assert_clean(error, token)
    provider.access = "link"
    error = failure(lambda: client.link(url.replace(token, "tok0000")))
    assert error.code == types.PROVIDER_LINK_UNAVAILABLE
    provider.fail("link", 403, "FORBIDDEN")
    error = failure(lambda: client.link(url))
    assert error.code == types.PROVIDER_BAD_RESPONSE
    assert_clean(error, token)
    asked = len(provider.requests)
    for bad in ("ftp://example.com/sub", "https://user:pw@example.com/sub", "not a link", ""):
        error = failure(lambda bad=bad: client.link(bad))
        assert error.code == types.PROVIDER_LINK_UNAVAILABLE
        assert_clean(error, "pw@")
    assert len(provider.requests) == asked


# -- redirects, limits and failures ---------------------------------------------------------------
def test_no_request_follows_a_redirect(
    client: HttpProviderApi, provider: FakeProvider, elsewhere: FakeProvider
) -> None:
    session = sign_in(client, provider)
    target = f"{elsewhere.base}/api/v1/app/proxy/subscriptions/7/config"
    for status in (301, 302, 303, 307, 308):
        provider.redirect("config", target, status)
        error = failure(lambda: client.config(provider.base, session.token, 7))
        assert error.code == types.PROVIDER_BAD_RESPONSE
        assert error.message == f"redirected (HTTP {status})"
        assert_clean(error, session.token)
    provider.redirect("link", target)
    error = failure(lambda: client.link(provider.link(7)))
    assert error.code == types.PROVIDER_BAD_RESPONSE
    provider.redirect("sign_in_email", f"{elsewhere.base}/api/auth/sign-in/email", 307)
    error = failure(lambda: client.sign_in_password(provider.base, EMAIL, PASSWORD))
    assert error.code == types.PROVIDER_BAD_RESPONSE
    provider.redirect("subscriptions", f"{elsewhere.base}/api/v1/app/billing/subscriptions")
    error = failure(lambda: client.subscriptions(provider.base, session.token))
    assert error.code == types.PROVIDER_BAD_RESPONSE
    assert elsewhere.requests == []


def test_the_token_goes_to_the_origin_only(
    client: HttpProviderApi, provider: FakeProvider, elsewhere: FakeProvider
) -> None:
    """A whole session's worth of calls: every request with the token is to the provider, and
    links and public calls carry none."""
    provider.mirror = elsewhere.base
    session = sign_in(client, provider)
    subscriptions = run(lambda: client.subscriptions(provider.base, session.token))
    run(lambda: client.subscription(provider.base, session.token, 7))
    run(lambda: client.config(provider.base, session.token, 7))
    run(lambda: client.wallet(provider.base, session.token))
    run(lambda: client.plans(provider.base, session.token))
    run(lambda: client.orders(provider.base, session.token, 5))
    mirror = subscriptions[0].urls[1]
    assert mirror.startswith(elsewhere.base)
    failure(lambda: client.link(mirror))  # the mirror knows no such token
    run(lambda: client.link(subscriptions[0].urls[0]))
    run(lambda: client.sign_out(provider.base, session.token))
    assert [r.route for r in elsewhere.requests] == ["link"]
    assert all(r.header("authorization") is None for r in elsewhere.requests)
    bearers = {r.route for r in provider.requests if r.bearer is not None}
    assert bearers == {
        "subscriptions",
        "subscription",
        "config",
        "wallet",
        "plans",
        "orders",
        "sign_out",
    }
    for request in provider.requests:
        assert request.header("cookie") is None
        assert request.bearer in (None, session.token)


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        ("30", 30.0),
        (format_datetime(datetime.fromtimestamp(NOW + 120, UTC), usegmt=True), 120.0),
        ("999999999", api.RETRY_AFTER_MAX_S),
        ("soon", None),
        (None, None),
    ],
)
def test_rate_limits(
    client: HttpProviderApi, provider: FakeProvider, retry_after: str | None, expected: float | None
) -> None:
    session = sign_in(client, provider)
    headers = [("Retry-After", retry_after)] if retry_after else []
    provider.fail("subscriptions", 429, "RATE_LIMITED", headers=headers)
    error = failure(lambda: client.subscriptions(provider.base, session.token))
    assert error.code == types.PROVIDER_RATE_LIMITED
    assert error.retry_after == expected
    assert error.message == "HTTP 429 RATE_LIMITED"


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408])
def test_server_errors_are_unreachable(
    client: HttpProviderApi, provider: FakeProvider, status: int
) -> None:
    session = sign_in(client, provider)
    provider.answer("wallet", status, b"<html>bad gateway</html>")
    error = failure(lambda: client.wallet(provider.base, session.token))
    assert error.code == types.PROVIDER_UNREACHABLE
    assert error.message == f"HTTP {status}"


def test_an_unreachable_provider(client: HttpProviderApi) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    token = "secret-session-token"
    errors = [
        failure(lambda: client.site(base)),
        failure(lambda: client.subscriptions(base, token)),
        failure(lambda: client.link(f"{base}/api/v1/app/proxy/subscriptions/tok-secret?x=1")),
    ]
    for error in errors:
        assert error.code == types.PROVIDER_UNREACHABLE
        assert_clean(error, token, "tok-secret", str(port))


def test_answers_over_the_json_limit(client: HttpProviderApi, provider: FakeProvider) -> None:
    session = sign_in(client, provider)
    padding = b" " * types.MAX_JSON_BYTES
    provider.answer("wallet", 200, b'{"currency": "CNY"}' + padding)
    error = failure(lambda: client.wallet(provider.base, session.token))
    assert error.code == types.PROVIDER_BAD_RESPONSE
    provider.answer("site", 200, b'{"name": "x"}' + padding)
    assert failure(lambda: client.site(provider.base)).code == types.PROVIDER_NOT_OPENWEIR


def test_answers_that_are_no_json(client: HttpProviderApi, provider: FakeProvider) -> None:
    session = sign_in(client, provider)
    for body in (b"<html></html>", b"[" * 100_000, b"\xff\xfe"):
        provider.answer("wallet", 200, body)
        error = failure(lambda: client.wallet(provider.base, session.token))
        assert error.code == types.PROVIDER_BAD_RESPONSE
        assert_clean(error, session.token)


def test_the_api_message_is_never_used(client: HttpProviderApi, provider: FakeProvider) -> None:
    """Only the status and a well-formed code reach the message: the server's own text may
    echo what was sent."""
    session = sign_in(client, provider)
    provider.answer(
        "wallet",
        400,
        {"code": "bad code with https://evil.example", "message": session.token},
    )
    error = failure(lambda: client.wallet(provider.base, session.token))
    assert error.message == "HTTP 400"
    assert_clean(error, session.token, "evil")


def test_errors_never_hold_a_secret(
    client: HttpProviderApi, provider: FakeProvider, elsewhere: FakeProvider
) -> None:
    """Every refusal of a signed-in call or a link, by status: no URL, token, link token or
    query in what the error says, and nothing chained to it."""
    session = sign_in(client, provider)
    url = provider.link(7) + "?flag=secret-query"
    token = url.rsplit("/", 1)[1].split("?")[0]
    for status in (301, 400, 401, 403, 404, 409, 429, 500, 503):
        provider.fail("config", status, "SOME_CODE", headers=[("Location", url)])
        assert_clean(
            failure(lambda: client.config(provider.base, session.token, 7)),
            session.token,
            token,
            "secret-query",
        )
        provider.fail("link", status, None, headers=[("Location", url)])
        assert_clean(failure(lambda: client.link(url)), session.token, token, "secret-query")
    assert elsewhere.requests == []


# -- the user center ------------------------------------------------------------------------------
def test_user_center_links() -> None:
    base = "https://example.com"
    assert web.home(base) == "https://example.com/"
    assert web.renew(base, 7) == "https://example.com/subscription?id=7"
    assert web.plans(base) == "https://example.com/plans"
    assert web.checkout(base, "pro", "month") == "https://example.com/plans?checkout=pro:month"
    assert web.checkout(base, "pro") == "https://example.com/plans?checkout=pro"
    assert web.wallet(base) == "https://example.com/wallet"
    assert web.orders(base) == "https://example.com/orders"
    assert web.security(base) == "https://example.com/settings/security"
    assert web.devices(base) == "https://example.com/settings/devices"
    assert web.plans("http://127.0.0.1:8787") == "http://127.0.0.1:8787/plans"


def test_user_center_links_stay_on_the_origin() -> None:
    base = "https://example.com"
    link = web.checkout(base, "pro&next=https://evil.example/#", "month/../x")
    assert link.startswith("https://example.com/plans?checkout=")
    assert "&" not in link
    assert "#" not in link
    assert "/" not in link.removeprefix("https://example.com/plans")
    for bad_origin in ("https://example.com/path", "http://example.com", "example.com"):
        with pytest.raises(ProviderError):
            web.plans(bad_origin)
    for bad_id in (0, -1, True):
        with pytest.raises(ValueError, match="positive"):
            web.renew(base, bad_id)
    with pytest.raises(ValueError, match="plan id"):
        web.checkout(base, "")
