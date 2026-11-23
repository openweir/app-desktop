"""ProviderApi over HTTP (docs/accounts.md §3.2, §10): the requests an OpenWeir provider's
api-edge answers, and ``origin(address)``, which turns what the user typed into the origin
every call takes.

Requests use urllib, as subscription downloads and the Google Drive backup do: it follows
the system proxy, and each request runs on one of the process's daemon worker threads
(host/workers.py), so a request in flight neither blocks the asyncio loop nor holds up the
exit. Each request builds its own opener without a cookie processor and without following
redirects: a 3xx is the answer, and is PROVIDER_BAD_RESPONSE. So the bearer token and a
two-factor step's cookies go to the URL they were made for, which is always the origin plus
a fixed path, and nowhere else; a link download carries neither.

Secrets stay out of errors. A ProviderError's message is the status and the API's error
``code`` only, or the reason a connection failed, scrubbed of the URL, its path and query,
the token and cookies. urllib's HTTPError carries the URL (``url``, ``filename``) and its
reading errors may quote it, so no exception of a request is chained to the ProviderError
raised for it: the error is raised outside the handler, with no ``__cause__`` or
``__context__``.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from functools import partial
from http.client import HTTPException
from typing import IO, Any, Never
from urllib.parse import urlencode, urlsplit

from openweir import __version__
from openweir.host.workers import run_blocking
from openweir.i18n import current_language
from openweir.models.provider.types import (
    MAX_JSON_BYTES,
    MAX_SUBSCRIPTIONS,
    METHOD_EMAIL_CODE,
    ORDER_WITHDRAWAL,
    PROVIDER_ACCOUNT_DISABLED,
    PROVIDER_APP_DISABLED,
    PROVIDER_BAD_RESPONSE,
    PROVIDER_CAPTCHA_REQUIRED,
    PROVIDER_CODE_INVALID,
    PROVIDER_EMAIL_UNVERIFIED,
    PROVIDER_INVALID_ADDRESS,
    PROVIDER_LINK_UNAVAILABLE,
    PROVIDER_NOT_OPENWEIR,
    PROVIDER_RATE_LIMITED,
    PROVIDER_SIGN_IN_FAILED,
    PROVIDER_SIGN_IN_REQUIRED,
    PROVIDER_SIGN_IN_UNSUPPORTED,
    PROVIDER_SUBSCRIPTION_MISSING,
    PROVIDER_TWO_FACTOR_EXPIRED,
    PROVIDER_TWO_FACTOR_INVALID,
    PROVIDER_UNREACHABLE,
    RESET_PURCHASE_DAY,
    TWO_FACTOR_TOTP,
    TWO_FACTOR_TTL_S,
    Account,
    Currency,
    Level,
    OrderSummary,
    Price,
    ProviderError,
    PublicPlan,
    RemoteSubscription,
    Session,
    SignInOptions,
    Site,
    TwoFactorChallenge,
    Wallet,
)
from openweir.models.subscription import MAX_CONFIG_BYTES, TIMEOUT_S, Download, read_download
from openweir.models.subscription import USER_AGENT as CONFIG_USER_AGENT
from openweir.product import DISPLAY_NAME

_PLATFORM = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux")
# API calls; configs and links send CONFIG_USER_AGENT (D48), so the provider serves its
# OpenWeir template.
USER_AGENT = f"{DISPLAY_NAME}/{__version__} ({_PLATFORM})"
# The UI language (i18n.current_language) as the API's locales.
ACCEPT_LANGUAGE = {"zh": "zh-CN", "en": "en-US"}
# Hosts an origin may reach over plain http, for development and tests.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# A Retry-After longer than this counts as this long.
RETRY_AFTER_MAX_S = 86400.0
# GET /api/v1/app/billing/orders takes pages of at most this many.
MAX_ORDERS = 100

# The api-edge paths (its src/core/paths.ts and the features' routes).
SITE = "/api/v1/app/site"
AUTH_PROVIDERS = "/api/v1/app/auth-settings/providers"
CAPTCHA = "/api/v1/app/captcha"
SIGN_IN_EMAIL = "/api/auth/sign-in/email"
SIGN_IN_USERNAME = "/api/auth/sign-in/username"
SEND_EMAIL_CODE = "/api/auth/email-otp/send-verification-otp"
SIGN_IN_EMAIL_CODE = "/api/auth/sign-in/email-otp"
VERIFY_TOTP = "/api/auth/two-factor/verify-totp"
SIGN_OUT = "/api/auth/sign-out"
ME = "/api/v1/app/me"
SUBSCRIPTIONS = "/api/v1/app/billing/subscriptions"
WALLET = "/api/v1/app/billing/wallet"
PLANS = "/api/v1/app/billing/plans"
ORDERS = "/api/v1/app/billing/orders"
CONFIG = "/api/v1/app/proxy/subscriptions/{id}/config"
_AUTH = "/api/auth/"

# Error bodies are read this far, for their ``code``.
_ERROR_BYTES = 64 * 1024
_MAX_TOKEN = 8192
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
_API_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_TOKEN = re.compile(rf"[\x21-\x7e]{{1,{_MAX_TOKEN}}}")
_COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,256}")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{0,4096}")
_DECIMAL = re.compile(r"[0-9]{1,10}")

# How a sign-in endpoint's refusal reads (Better Auth's codes, its captcha plugin's, and the
# username and email-otp plugins'). TOO_MANY_ATTEMPTS is email-otp's; the two-factor plugin's
# is TOO_MANY_ATTEMPTS_REQUEST_NEW_CODE.
_SIGN_IN_CODES = {
    "INVALID_EMAIL_OR_PASSWORD": PROVIDER_SIGN_IN_FAILED,
    "INVALID_USERNAME_OR_PASSWORD": PROVIDER_SIGN_IN_FAILED,
    "INVALID_EMAIL": PROVIDER_SIGN_IN_FAILED,
    "INVALID_USERNAME": PROVIDER_SIGN_IN_FAILED,
    "USERNAME_TOO_SHORT": PROVIDER_SIGN_IN_FAILED,
    "USERNAME_TOO_LONG": PROVIDER_SIGN_IN_FAILED,
    "PASSWORD_TOO_LONG": PROVIDER_SIGN_IN_FAILED,
    "USER_NOT_FOUND": PROVIDER_SIGN_IN_FAILED,
    "EMAIL_NOT_VERIFIED": PROVIDER_EMAIL_UNVERIFIED,
    "INVALID_OTP": PROVIDER_CODE_INVALID,
    "OTP_EXPIRED": PROVIDER_CODE_INVALID,
    "TOO_MANY_ATTEMPTS": PROVIDER_CODE_INVALID,
    "INVALID_CODE": PROVIDER_TWO_FACTOR_INVALID,
    "TOO_MANY_ATTEMPTS_REQUEST_NEW_CODE": PROVIDER_TWO_FACTOR_INVALID,
    "INVALID_TWO_FACTOR_COOKIE": PROVIDER_TWO_FACTOR_EXPIRED,
    "MISSING_RESPONSE": PROVIDER_CAPTCHA_REQUIRED,
    "VERIFICATION_FAILED": PROVIDER_CAPTCHA_REQUIRED,
    "EMAIL_PASSWORD_DISABLED": PROVIDER_SIGN_IN_UNSUPPORTED,
}
# 403 codes that mean the account itself is refused: api-edge's FORBIDDEN, and Better Auth's
# "This account is disabled", which has no code.
_DISABLED_CODES = frozenset({"", "FORBIDDEN"})


# -- the address ----------------------------------------------------------------------------------
def origin(address: str) -> str:
    """``https://host[:port]`` from what the user typed: blanks trimmed, ``https://`` added
    without a scheme, the path, query and fragment dropped, the host lowercased (and in IDNA
    form), the default port left out. Only https, and http for LOOPBACK_HOSTS; an address
    with a user name or password, without a host or with a bad port raises
    PROVIDER_INVALID_ADDRESS. The message never repeats the address."""
    value = address.strip()
    if not value or any(c.isspace() or c == "\\" or ord(c) < 32 or ord(c) == 127 for c in value):
        _invalid("not an address")
    if not _SCHEME.match(value):
        value = f"https://{value}"
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if port == -1 or port == 0:
        _invalid("a bad port")
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if "@" in parts.netloc:
        _invalid("a user name in the address")
    if not host or "%" in host:
        _invalid("no host")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            host = ""
        if not host:
            _invalid("a host that is no domain name")
    if scheme != "https" and not (scheme == "http" and host in LOOPBACK_HOSTS):
        _invalid("not https")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if scheme == "https" else 80):
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}"


def _invalid(reason: str) -> Never:
    raise ProviderError(PROVIDER_INVALID_ADDRESS, reason)


def _checked_origin(value: str) -> str:
    """``value`` when it is an origin as ``origin`` makes them, so that a call's URL is always
    the origin plus a fixed path."""
    if origin(value) != value:
        _invalid("not an origin")
    return value


def _link_url(url: str) -> str:
    """A subscription link as the provider issued it: http or https, a host, no user name and
    no blanks. Anything else is PROVIDER_LINK_UNAVAILABLE (never quoting the link)."""
    url = url.strip()
    valid = bool(url) and not any(
        c.isspace() or c == "\\" or ord(c) < 32 or ord(c) == 127 for c in url
    )
    if valid:
        parts = urlsplit(url)
        try:
            _ = parts.port
        except ValueError:
            valid = False
        else:
            valid = (
                parts.scheme in ("http", "https")
                and bool(parts.hostname)
                and "@" not in parts.netloc
            )
    if not valid:
        raise ProviderError(PROVIDER_LINK_UNAVAILABLE, "the link is no http or https URL")
    return url


# -- requests -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Call:
    """One request, made on a worker thread. ``private`` headers (Authorization, Cookie) are
    added as unredirected headers; ``secrets`` are scrubbed from any failure's message."""

    method: str
    url: str = field(repr=False)
    headers: tuple[tuple[str, str], ...]
    private: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    data: bytes | None = field(default=None, repr=False)
    limit: int = MAX_JSON_BYTES
    timeout_s: float = TIMEOUT_S
    secrets: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class _Response:
    status: int
    headers: Message = field(repr=False)
    body: bytes = field(repr=False)


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Hands every 3xx back as the answer (urllib then raises it as an HTTPError)."""

    def http_error_302(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
    ) -> None:
        return None

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302  # type: ignore[assignment]


def _send(call: _Call) -> _Response:
    """Make ``call``; any answer, 3xx to 5xx included, is a _Response. A failure to get one
    is PROVIDER_UNREACHABLE with the reason, scrubbed, and chained to nothing."""
    reason: str
    try:
        request = urllib.request.Request(
            call.url, data=call.data, method=call.method, headers=dict(call.headers)
        )
        for name, value in call.private:
            request.add_unredirected_header(name, value)
        # No cookie processor: nothing is kept between requests.
        opener = urllib.request.build_opener(_RefuseRedirects())
        with opener.open(request, timeout=call.timeout_s) as response:
            length = getattr(response, "length", None)
            body = response.read(call.limit + 1)
            if length is not None and len(body) < min(length, call.limit + 1):
                reason = "the answer broke off"
            else:
                return _Response(response.status, response.headers, body)
    except urllib.error.HTTPError as error:
        try:
            body = error.read(_ERROR_BYTES)
        except OSError, HTTPException:
            body = b""
        finally:
            error.close()
        return _Response(error.code, error.headers, body)
    except urllib.error.URLError as error:
        reason = _reason(error.reason)
    except TimeoutError:
        reason = "timed out"
    except (OSError, HTTPException, ValueError) as error:
        reason = _reason(error)
    raise ProviderError(PROVIDER_UNREACHABLE, _scrub(reason, call.secrets))


def _reason(reason: object) -> str:
    if isinstance(reason, TimeoutError):
        return "timed out"
    if isinstance(reason, BaseException):
        return str(reason) or type(reason).__name__
    return str(reason)


def _scrub(text: str, secrets: Iterable[str]) -> str:
    """``text`` without any of ``secrets`` (the URL, its path and query, the token, cookies)
    and without anything that looks like a URL."""
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, "<hidden>")
    text = _URL.sub("<url>", text).strip()
    return text[:200] or "unknown error"


def _secrets(url: str, *values: str | None) -> tuple[str, ...]:
    parts = urlsplit(url)
    return tuple(v for v in (url, parts.path, parts.query, *values) if v)


# -- error answers --------------------------------------------------------------------------------
def _api_code(body: bytes) -> str:
    """The ``code`` of an API error ``{"code", "message"}``; "" without a well-formed one. The
    message is never used: it is the server's text, and may echo input."""
    try:
        payload = json.loads(body)
    except ValueError, RecursionError:
        return ""
    code = payload.get("code") if isinstance(payload, dict) else None
    return code if isinstance(code, str) and _API_CODE.fullmatch(code) else ""


def _retry_after(headers: Message, now: float) -> float | None:
    """Retry-After in seconds from ``now`` (delay or HTTP date), at most RETRY_AFTER_MAX_S."""
    value = (headers.get("Retry-After") or "").strip()
    if not value:
        return None
    if _DECIMAL.fullmatch(value):
        seconds = float(value)
    else:
        try:
            when = parsedate_to_datetime(value)
        except TypeError, ValueError, IndexError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = when.timestamp() - now
    return min(max(seconds, 0.0), RETRY_AFTER_MAX_S)


def _detail(response: _Response, code: str) -> str:
    return f"HTTP {response.status} {code}" if code else f"HTTP {response.status}"


def _transport(response: _Response, code: str, now: float) -> ProviderError | None:
    """What every endpoint's 3xx, 429 and 5xx mean."""
    status = response.status
    if 300 <= status < 400:
        return ProviderError(PROVIDER_BAD_RESPONSE, f"redirected ({_detail(response, code)})")
    if status == 429:
        return ProviderError(
            PROVIDER_RATE_LIMITED,
            _detail(response, code),
            retry_after=_retry_after(response.headers, now),
        )
    if status >= 500 or status == 408:
        return ProviderError(PROVIDER_UNREACHABLE, _detail(response, code))
    return None


def _sign_in_failure(response: _Response, now: float, rejected: str) -> ProviderError:
    """A refused POST to /api/auth/*. ``rejected`` is what a 401 without a known code, or a 400
    VALIDATION_ERROR of the input, means for this step."""
    code = _api_code(response.body)
    if failure := _transport(response, code, now):
        return failure
    detail = _detail(response, code)
    if code in _SIGN_IN_CODES:
        return ProviderError(_SIGN_IN_CODES[code], detail)
    if response.status == 401 or code == "VALIDATION_ERROR":
        return ProviderError(rejected, detail)
    if response.status == 403 and code in _DISABLED_CODES:
        return ProviderError(PROVIDER_ACCOUNT_DISABLED, detail)
    if response.status == 404:  # the provider has not enabled this method
        return ProviderError(PROVIDER_SIGN_IN_UNSUPPORTED, detail)
    return ProviderError(PROVIDER_BAD_RESPONSE, detail)


def _app_failure(
    response: _Response,
    now: float,
    *,
    signed_in: bool,
    missing: str = PROVIDER_BAD_RESPONSE,
    app_disabled: bool = False,
) -> ProviderError:
    """A refused /api/v1/app/* call. ``missing`` is what a 404 means here."""
    code = _api_code(response.body)
    if failure := _transport(response, code, now):
        return failure
    detail = _detail(response, code)
    status = response.status
    if status == 401 and signed_in:
        return ProviderError(PROVIDER_SIGN_IN_REQUIRED, detail)
    if status == 403 and app_disabled and code == "SUBSCRIPTION_APP_DISABLED":
        return ProviderError(PROVIDER_APP_DISABLED, detail)
    if status == 403 and signed_in and code in _DISABLED_CODES:
        return ProviderError(PROVIDER_ACCOUNT_DISABLED, detail)
    if status == 404:
        return ProviderError(missing, detail)
    return ProviderError(PROVIDER_BAD_RESPONSE, detail)


def _link_failure(response: _Response, now: float) -> ProviderError:
    code = _api_code(response.body)
    if failure := _transport(response, code, now):
        return failure
    detail = _detail(response, code)
    if response.status == 404 or (response.status == 403 and code == "SUBSCRIPTION_LINK_DISABLED"):
        return ProviderError(PROVIDER_LINK_UNAVAILABLE, detail)
    return ProviderError(PROVIDER_BAD_RESPONSE, detail)


# -- reading answers ------------------------------------------------------------------------------
_INVALID = object()


def _json(response: _Response, code: str = PROVIDER_BAD_RESPONSE) -> Any:
    if len(response.body) > MAX_JSON_BYTES:
        raise ProviderError(code, f"an answer over {MAX_JSON_BYTES} bytes")
    try:
        value = json.loads(response.body)
    except ValueError, RecursionError:
        value = _INVALID
    if value is _INVALID:
        raise ProviderError(code, "an answer that is not JSON")
    return value


def _time(value: str) -> float | None:
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


class _Fields:
    """A JSON object read field by field; a missing or mistyped field raises ``code`` naming
    it (never its value)."""

    def __init__(self, value: object, what: str, code: str = PROVIDER_BAD_RESPONSE) -> None:
        if not isinstance(value, dict):
            raise ProviderError(code, f"{what} is no object")
        self.data: dict[str, Any] = value
        self._what = what
        self._code = code

    def _bad(self, name: str) -> Never:
        raise ProviderError(self._code, f"{self._what}: {name} is missing or mistyped")

    def text(self, name: str, *, empty: bool = False) -> str:
        value = self.data.get(name)
        if not isinstance(value, str) or not (empty or value.strip()):
            self._bad(name)
        return value

    def text_or(self, name: str, default: str = "") -> str:
        value = self.data.get(name)
        if value is None:
            return default
        if not isinstance(value, str):
            self._bad(name)
        return value

    def optional_text(self, name: str) -> str | None:
        value = self.data.get(name)
        if value is not None and not isinstance(value, str):
            self._bad(name)
        return value

    def integer(self, name: str) -> int:
        value = _integer(self.data.get(name))
        if value is None:
            self._bad(name)
        return value

    def integer_or(self, name: str, default: int = 0) -> int:
        if self.data.get(name) is None:
            return default
        return self.integer(name)

    def optional_integer(self, name: str) -> int | None:
        return None if self.data.get(name) is None else self.integer(name)

    def flag(self, name: str) -> bool:
        value = self.data.get(name)
        if not isinstance(value, bool):
            self._bad(name)
        return value

    def flag_or(self, name: str, default: bool = False) -> bool:
        return default if self.data.get(name) is None else self.flag(name)

    def optional_time(self, name: str) -> float | None:
        value = self.data.get(name)
        if value is None:
            return None
        when = _time(value) if isinstance(value, str) else None
        if when is None:
            self._bad(name)
        return when

    def time_or(self, name: str, default: float = 0.0) -> float:
        when = self.optional_time(name)
        return default if when is None else when

    def texts(self, name: str) -> tuple[str, ...]:
        value = self.data.get(name)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self._bad(name)
        return tuple(value)

    def items(self, name: str) -> list[Any]:
        value = self.data.get(name)
        if not isinstance(value, list):
            self._bad(name)
        return value

    def child(self, name: str) -> _Fields:
        return _Fields(self.data.get(name), f"{self._what}.{name}", self._code)

    def optional_child(self, name: str) -> _Fields | None:
        return None if self.data.get(name) is None else self.child(name)


def _site(value: object) -> Site:
    fields = _Fields(value, "site", PROVIDER_NOT_OPENWEIR)
    currency = fields.child("currency")
    digits = currency.integer("digits")
    if not 0 <= digits <= 8:
        raise ProviderError(PROVIDER_NOT_OPENWEIR, "site: currency.digits is out of range")
    return Site(
        name=fields.text("name").strip(),
        currency=Currency(currency.text("code"), currency.text("symbol", empty=True), digits),
        description=fields.text_or("description"),
        default_locale=fields.text_or("defaultLocale"),
    )


def _options(providers: object, scenes: tuple[str, ...]) -> SignInOptions:
    if not isinstance(providers, list):
        raise ProviderError(PROVIDER_BAD_RESPONSE, "auth providers: no list")
    methods: list[str] = []
    code_length = 6
    for item in providers:
        fields = _Fields(item, "auth provider")
        kind = fields.text("type")
        if kind not in methods:
            methods.append(kind)
        options = fields.optional_child("options")
        if kind == METHOD_EMAIL_CODE and options is not None:
            code_length = options.integer_or("otpLength", 6)
            if not 1 <= code_length <= 32:
                raise ProviderError(PROVIDER_BAD_RESPONSE, "auth provider: otpLength is odd")
    return SignInOptions(tuple(methods), scenes, code_length)


def _account(value: object) -> Account:
    fields = _Fields(value, "user")
    return Account(
        id=fields.text("id"),
        email=fields.text_or("email"),
        name=fields.text_or("name"),
        username=fields.text_or("displayUsername") or fields.text_or("username"),
    )


def _subscription(value: object) -> RemoteSubscription:
    fields = _Fields(value, "subscription")
    remote_id = fields.integer("id")
    if remote_id <= 0:
        raise ProviderError(PROVIDER_BAD_RESPONSE, "subscription: id is not positive")
    url = fields.optional_text("url")
    return RemoteSubscription(
        id=remote_id,
        plan_id=fields.text("planId"),
        status=fields.text("status"),
        usable=fields.flag("usable"),
        plan_name=fields.text_or("planName"),
        user_id=fields.text_or("userId"),
        trial=fields.flag_or("trial"),
        start_at=fields.time_or("startAt"),
        expire_at=fields.optional_time("expireAt"),
        period_start=fields.time_or("periodStart"),
        traffic_limit=fields.optional_integer("trafficLimit"),
        extra_bytes=fields.integer_or("extraBytes"),
        upload=fields.integer_or("upload"),
        download=fields.integer_or("download"),
        total=fields.optional_integer("total"),
        speed_limit=fields.integer_or("speedLimit"),
        device_limit=fields.integer_or("deviceLimit"),
        online_devices=fields.integer_or("onlineDevices"),
        reset_mode=fields.text_or("resetMode", RESET_PURCHASE_DAY),
        auto_renew=fields.flag_or("autoRenew"),
        access=fields.text_or("subscriptionAccess"),
        created_at=fields.time_or("createdAt"),
        updated_at=fields.time_or("updatedAt"),
        url=url or None,
        urls=fields.texts("urls"),
    )


def _level(fields: _Fields | None) -> Level | None:
    if fields is None:
        return None
    return Level(
        id=fields.text("id"),
        name=fields.text_or("name"),
        discount_percent=fields.integer_or("discountPercent"),
        min_spent=fields.optional_integer("minSpent"),
        min_days=fields.optional_integer("minDays"),
    )


def _wallet(value: object) -> Wallet:
    fields = _Fields(value, "wallet")
    return Wallet(
        currency=fields.text("currency"),
        balance=fields.integer_or("balance"),
        points=fields.integer_or("points"),
        commission=fields.integer_or("commission"),
        total_spent=fields.integer_or("totalSpent"),
        paid_days=fields.integer_or("paidDays"),
        level=_level(fields.optional_child("level")),
        next_level=_level(fields.optional_child("nextLevel")),
        user_id=fields.text_or("userId"),
    )


def _plans(value: object) -> tuple[PublicPlan, ...]:
    answer = _Fields(value, "plans")
    currency = answer.text("currency")
    plans: list[PublicPlan] = []
    for item in answer.items("plans"):
        fields = _Fields(item, "plan")
        prices = tuple(
            Price(price.text("period"), price.integer("amount"))
            for price in (_Fields(p, "plan price") for p in fields.items("prices"))
        )
        plans.append(
            PublicPlan(
                id=fields.text("id"),
                kind=fields.text("kind"),
                name=fields.text_or("name") or fields.text("id"),
                currency=currency,
                prices=prices,
                description=fields.text_or("description"),
                traffic_bytes=fields.optional_integer("trafficBytes"),
                speed_limit=fields.integer_or("speedLimit"),
                device_limit=fields.integer_or("deviceLimit"),
                reset_mode=fields.text_or("resetMode", RESET_PURCHASE_DAY),
                tags=fields.texts("tags"),
                sold_out=fields.flag_or("soldOut"),
            )
        )
    return tuple(plans)


def _orders(value: object) -> tuple[OrderSummary, ...]:
    orders: list[OrderSummary] = []
    for item in _Fields(value, "orders").items("items"):
        fields = _Fields(item, "order")
        kind = fields.text("kind")
        if kind == ORDER_WITHDRAWAL:
            continue
        orders.append(
            OrderSummary(
                id=fields.text("id"),
                kind=kind,
                status=fields.text("status"),
                currency=fields.text("currency"),
                amount=fields.integer("amount"),
                created_at=fields.time_or("createdAt"),
                price=fields.integer_or("price"),
                discount=fields.integer_or("discount"),
                credit=fields.integer_or("credit"),
                paid_amount=fields.integer_or("paidAmount"),
                plan_id=fields.optional_text("planId"),
                plan_name=fields.text_or("planName"),
                period=fields.optional_text("period"),
                subscription_id=fields.optional_integer("subscriptionId"),
                expires_at=fields.optional_time("expiresAt"),
                paid_at=fields.optional_time("paidAt"),
            )
        )
    return tuple(orders)


def _token(value: object) -> str:
    """The session token of a sign-in answer: printable ASCII without blanks, so that it is
    safe as a header value."""
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ProviderError(PROVIDER_BAD_RESPONSE, "the sign-in answer has no usable token")
    return value


def _cookies(headers: Message, now: float) -> tuple[tuple[str, str], ...]:
    """The name and value of each cookie the answer sets, in this sign-in's own jar: a cookie
    the answer deletes (empty, ``Max-Age`` 0 or an ``Expires`` past) is left out."""
    jar: dict[str, str] = {}
    for header in headers.get_all("Set-Cookie") or ():
        pair, *attributes = str(header).split(";")
        name, separator, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not _COOKIE_NAME.fullmatch(name):
            continue
        if not value or not _COOKIE_VALUE.fullmatch(value) or _expired(attributes, now):
            jar.pop(name, None)
        else:
            jar[name] = value
    return tuple(jar.items())


def _expired(attributes: list[str], now: float) -> bool:
    for attribute in attributes:
        key, _, value = attribute.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key == "max-age":
            try:
                if int(value) <= 0:
                    return True
            except ValueError:
                pass
        elif key == "expires":
            try:
                when = parsedate_to_datetime(value)
            except TypeError, ValueError, IndexError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            if when.timestamp() <= now:
                return True
    return False


def _remote_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProviderError(PROVIDER_SUBSCRIPTION_MISSING, "no such subscription id")
    return value


def _download(response: _Response) -> Download:
    if len(response.body) > MAX_CONFIG_BYTES:
        raise ProviderError(PROVIDER_BAD_RESPONSE, f"a config over {MAX_CONFIG_BYTES} bytes")
    return read_download(response.body, response.headers)


# -- the client -----------------------------------------------------------------------------------
class HttpProviderApi:
    """ProviderApi over urllib (module docstring). It keeps nothing between calls; a
    two-factor step's cookies travel in its TwoFactorChallenge.

    ``language`` gives the UI language (``zh`` or ``en``) for Accept-Language, read on the
    calling thread for each request; ``clock`` gives unix seconds (a challenge's expiry,
    Retry-After dates)."""

    def __init__(
        self,
        *,
        language: Callable[[], str] = current_language,
        clock: Callable[[], float] = time.time,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self._language = language
        self._clock = clock
        self._timeout_s = timeout_s

    # -- requests ----------------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        url: str,
        *,
        user_agent: str = USER_AGENT,
        accept: str | None = "application/json",
        body: dict[str, Any] | None = None,
        token: str | None = None,
        cookies: tuple[tuple[str, str], ...] = (),
        origin_header: str | None = None,
        limit: int = MAX_JSON_BYTES,
    ) -> _Response:
        headers = [
            ("User-Agent", user_agent),
            ("Accept-Language", ACCEPT_LANGUAGE.get(self._language(), "zh-CN")),
        ]
        if accept:
            headers.append(("Accept", accept))
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            headers.append(("Content-Type", "application/json"))
        if origin_header:
            headers.append(("Origin", origin_header))
        private: list[tuple[str, str]] = []
        if token is not None:
            if not _TOKEN.fullmatch(token):
                raise ProviderError(PROVIDER_SIGN_IN_REQUIRED, "the stored token is malformed")
            private.append(("Authorization", f"Bearer {token}"))
        for name, value in cookies:
            if not (_COOKIE_NAME.fullmatch(name) and _COOKIE_VALUE.fullmatch(value)):
                raise ProviderError(PROVIDER_TWO_FACTOR_EXPIRED, "a malformed two-factor cookie")
        if cookies:
            private.append(("Cookie", "; ".join(f"{name}={value}" for name, value in cookies)))
        call = _Call(
            method=method,
            url=url,
            headers=tuple(headers),
            private=tuple(private),
            data=data,
            limit=limit,
            timeout_s=self._timeout_s,
            secrets=_secrets(url, token, *(value for _, value in cookies)),
        )
        return await run_blocking(partial(_send, call))

    async def _api(
        self,
        method: str,
        origin: str,
        path: str,
        **options: Any,
    ) -> _Response:
        """A call to ``origin`` + ``path``; a POST to /api/auth/* also sends Origin."""
        base = _checked_origin(origin)
        if method == "POST" and path.startswith(_AUTH):
            options["origin_header"] = base
        return await self._request(method, base + path, **options)

    # -- probing -----------------------------------------------------------------------------
    async def site(self, origin: str) -> Site:
        response = await self._api("GET", origin, SITE)
        if response.status != 200:
            code = _api_code(response.body)
            raise _transport(response, code, self._clock()) or ProviderError(
                PROVIDER_NOT_OPENWEIR, _detail(response, code)
            )
        return _site(_json(response, PROVIDER_NOT_OPENWEIR))

    async def sign_in_options(self, origin: str) -> SignInOptions:
        providers = await self._api("GET", origin, AUTH_PROVIDERS)
        if providers.status != 200:
            raise _app_failure(providers, self._clock(), signed_in=False)
        methods = _json(providers)
        captcha = await self._api("GET", origin, CAPTCHA)
        if captcha.status == 404:  # a panel without the captcha module asks for none
            scenes: tuple[str, ...] = ()
        elif captcha.status != 200:
            raise _app_failure(captcha, self._clock(), signed_in=False)
        else:
            scenes = _Fields(_json(captcha), "captcha").texts("scenes")
        return _options(methods, scenes)

    # -- signing in --------------------------------------------------------------------------
    async def sign_in_password(
        self, origin: str, identifier: str, password: str
    ) -> Session | TwoFactorChallenge:
        identifier = identifier.strip()
        if "@" in identifier:
            path, body = SIGN_IN_EMAIL, {"email": identifier, "password": password}
        else:
            path, body = SIGN_IN_USERNAME, {"username": identifier, "password": password}
        response = await self._api("POST", origin, path, body=body)
        return await self._signed_in(origin, identifier, response, PROVIDER_SIGN_IN_FAILED)

    async def send_email_code(self, origin: str, email: str) -> None:
        body = {"email": email.strip(), "type": "sign-in"}
        response = await self._api("POST", origin, SEND_EMAIL_CODE, body=body)
        if response.status != 200:
            raise _sign_in_failure(response, self._clock(), PROVIDER_SIGN_IN_FAILED)

    async def sign_in_email_code(
        self, origin: str, email: str, code: str
    ) -> Session | TwoFactorChallenge:
        email = email.strip()
        body = {"email": email, "otp": code.strip()}
        response = await self._api("POST", origin, SIGN_IN_EMAIL_CODE, body=body)
        return await self._signed_in(origin, email, response, PROVIDER_CODE_INVALID)

    async def verify_totp(self, challenge: TwoFactorChallenge, code: str) -> Session:
        if self._clock() >= challenge.expires_at:
            raise ProviderError(PROVIDER_TWO_FACTOR_EXPIRED, "the two-factor step timed out")
        response = await self._api(
            "POST",
            challenge.origin,
            VERIFY_TOTP,
            body={"code": code.strip()},
            cookies=challenge.cookies,
        )
        if response.status != 200:
            raise _sign_in_failure(response, self._clock(), PROVIDER_TWO_FACTOR_INVALID)
        return await self._session(challenge.origin, response, _Fields(_json(response), "answer"))

    async def _signed_in(
        self, origin: str, identifier: str, response: _Response, rejected: str
    ) -> Session | TwoFactorChallenge:
        now = self._clock()
        if response.status != 200:
            raise _sign_in_failure(response, now, rejected)
        fields = _Fields(_json(response), "sign-in answer")
        if fields.data.get("twoFactorRedirect") is True:
            cookies = _cookies(response.headers, now)
            if not cookies:
                raise ProviderError(PROVIDER_BAD_RESPONSE, "two-factor sign-in without a cookie")
            return TwoFactorChallenge(
                origin=origin,
                identifier=identifier,
                methods=fields.texts("twoFactorMethods") or (TWO_FACTOR_TOTP,),
                expires_at=now + TWO_FACTOR_TTL_S,
                cookies=cookies,
            )
        return await self._session(origin, response, fields)

    async def _session(self, origin: str, response: _Response, fields: _Fields) -> Session:
        """Better Auth's bearer plugin answers the session token in set-auth-token; the body's
        ``token`` serves without it."""
        token = _token(response.headers.get("set-auth-token") or fields.data.get("token"))
        user = fields.data.get("user")
        account = _account(user) if user is not None else await self.account(origin, token)
        return Session(origin, token, account)

    async def account(self, origin: str, token: str) -> Account:
        response = await self._api("GET", origin, ME, token=token)
        if response.status != 200:
            raise _app_failure(response, self._clock(), signed_in=True)
        return _account(_json(response))

    async def sign_out(self, origin: str, token: str) -> None:
        response = await self._api("POST", origin, SIGN_OUT, body={}, token=token)
        if response.status not in (200, 401):
            raise _app_failure(response, self._clock(), signed_in=True)

    # -- billing -----------------------------------------------------------------------------
    async def subscriptions(self, origin: str, token: str) -> tuple[RemoteSubscription, ...]:
        response = await self._api("GET", origin, SUBSCRIPTIONS, token=token)
        if response.status != 200:
            raise _app_failure(response, self._clock(), signed_in=True)
        items = _json(response)
        if not isinstance(items, list):
            raise ProviderError(PROVIDER_BAD_RESPONSE, "subscriptions: no list")
        if len(items) > MAX_SUBSCRIPTIONS:
            raise ProviderError(
                PROVIDER_BAD_RESPONSE, f"more than {MAX_SUBSCRIPTIONS} subscriptions"
            )
        subscriptions = sorted((_subscription(item) for item in items), key=lambda s: s.id)
        if len({s.id for s in subscriptions}) != len(subscriptions):
            raise ProviderError(PROVIDER_BAD_RESPONSE, "subscriptions: duplicate ids")
        return tuple(subscriptions)

    async def subscription(self, origin: str, token: str, remote_id: int) -> RemoteSubscription:
        path = f"{SUBSCRIPTIONS}/{_remote_id(remote_id)}"
        response = await self._api("GET", origin, path, token=token)
        if response.status != 200:
            raise _app_failure(
                response, self._clock(), signed_in=True, missing=PROVIDER_SUBSCRIPTION_MISSING
            )
        return self._same(_subscription(_json(response)), remote_id)

    async def set_auto_renew(
        self, origin: str, token: str, remote_id: int, enabled: bool
    ) -> RemoteSubscription:
        path = f"{SUBSCRIPTIONS}/{_remote_id(remote_id)}"
        response = await self._api(
            "PATCH", origin, path, token=token, body={"autoRenew": bool(enabled)}
        )
        if response.status != 200:
            raise _app_failure(
                response, self._clock(), signed_in=True, missing=PROVIDER_SUBSCRIPTION_MISSING
            )
        return self._same(_subscription(_json(response)), remote_id)

    @staticmethod
    def _same(subscription: RemoteSubscription, remote_id: int) -> RemoteSubscription:
        if subscription.id != remote_id:
            raise ProviderError(PROVIDER_BAD_RESPONSE, "the answer is another subscription")
        return subscription

    async def wallet(self, origin: str, token: str) -> Wallet:
        response = await self._api("GET", origin, WALLET, token=token)
        if response.status != 200:
            raise _app_failure(response, self._clock(), signed_in=True)
        return _wallet(_json(response))

    async def plans(self, origin: str, token: str | None) -> tuple[PublicPlan, ...]:
        response = await self._api("GET", origin, PLANS, token=token)
        if response.status != 200:
            raise _app_failure(response, self._clock(), signed_in=token is not None)
        return _plans(_json(response))

    async def orders(self, origin: str, token: str, limit: int) -> tuple[OrderSummary, ...]:
        size = min(max(int(limit), 1), MAX_ORDERS)
        path = f"{ORDERS}?{urlencode({'page': 1, 'pageSize': size})}"
        response = await self._api("GET", origin, path, token=token)
        if response.status != 200:
            raise _app_failure(response, self._clock(), signed_in=True)
        return _orders(_json(response))[:size]

    # -- configs -----------------------------------------------------------------------------
    async def config(self, origin: str, token: str, remote_id: int) -> Download:
        response = await self._api(
            "GET",
            origin,
            CONFIG.format(id=_remote_id(remote_id)),
            token=token,
            user_agent=CONFIG_USER_AGENT,
            accept=None,
            limit=MAX_CONFIG_BYTES,
        )
        if response.status != 200:
            raise _app_failure(
                response,
                self._clock(),
                signed_in=True,
                missing=PROVIDER_SUBSCRIPTION_MISSING,
                app_disabled=True,
            )
        return _download(response)

    async def link(self, url: str) -> Download:
        response = await self._request(
            "GET",
            _link_url(url),
            user_agent=CONFIG_USER_AGENT,
            accept=None,
            limit=MAX_CONFIG_BYTES,
        )
        if response.status != 200:
            raise _link_failure(response, self._clock())
        return _download(response)
