"""Provider accounts' contract (docs/accounts.md, decisions D55-D59): the values an OpenWeir
provider's API answers with, the errors, and the two interfaces the rest of the app builds on.

A provider (服务商) is one deployment of the OpenWeir panel (api-edge behind its user center,
which proxies ``/api/*``), known by its origin, such as ``https://example.com``. The user may
sign in to several providers, one account each. Every subscription of the account becomes a
profile of kind ``provider``; buying anything happens in the provider's user center in the
browser (D58), so the app only reads billing, apart from the auto-renew switch.

Two interfaces split the work:

    ProviderApi      the HTTP client (models/provider/api.py): one method per endpoint, no state
                     beyond a sign-in flow's cookies; it never stores anything.
    AccountsService  what the view models call (models/library/accounts.py): sign-in, the
                     provider records and tokens, syncing subscriptions into profiles, fetching
                     a provider profile's config, and billing for the account page.

Names in the docstrings in ``backquotes`` are the API's JSON names. Times arrive as ISO 8601
strings and are unix seconds here; JSON ``null`` becomes None, or "" for text that is only
shown. Money is an integer of the currency's minor units (``Currency.digits`` places).
Traffic is bytes.

Secrets never leave memory except into the credential store: a session's token, a two-factor
challenge's cookies and a subscription's link stay out of ``repr``, of ``RemoteSubscription.
record`` and so of the database, backups, diagnostics, logs and crash records.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Protocol

from openweir.models.profiles import ProfileError, Usage

if TYPE_CHECKING:
    from openweir.models.library.profiles import Updated
    from openweir.models.profiles import Profile, ProfileList
    from openweir.models.subscription import Download

# -- limits ---------------------------------------------------------------------------------------
# A JSON answer larger than this is provider_bad_response; configs keep
# subscription.MAX_CONFIG_BYTES.
MAX_JSON_BYTES = 1024 * 1024
# More subscriptions than this in one account is provider_bad_response.
MAX_SUBSCRIPTIONS = 128
# Better Auth's two_factor cookie lives this long; the challenge expires with it.
TWO_FACTOR_TTL_S = 600
# RemoteSubscription.condition: "expiring" within this of its expiry, "traffic_low" from this
# share of its traffic used.
EXPIRING_S = 3 * 86400
TRAFFIC_LOW = 0.9

# -- sign-in methods: AuthProvider ``type`` in GET /api/v1/app/auth-settings/providers ------------
METHOD_EMAIL_PASSWORD = "email_password"  # POST /api/auth/sign-in/email
METHOD_USERNAME = "username"  # POST /api/auth/sign-in/username
METHOD_EMAIL_CODE = "email_otp"  # /api/auth/email-otp/send-verification-otp, /sign-in/email-otp
METHOD_TWO_FACTOR = "two_factor"  # a second step after the others, never a method of its own
# The provider may list others (phone_otp, magic_link, passkey, social types, oidc): v1 cannot
# use them, and a provider with only those gets provider_sign_in_unsupported.

# -- captcha scenes: ``scenes`` in GET /api/v1/app/captcha (empty while no captcha is enabled) -----
CAPTCHA_SIGN_IN = "sign_in"  # guards /sign-in/email and /sign-in/username
CAPTCHA_SEND_CODE = "send_code"  # guards /email-otp/send-verification-otp

# -- two-factor methods: ``twoFactorMethods`` of a sign-in answer with ``twoFactorRedirect`` ------
TWO_FACTOR_TOTP = "totp"  # POST /api/auth/two-factor/verify-totp; the only one v1 verifies
TWO_FACTOR_OTP = "otp"

# -- RemoteSubscription.status: ``status`` ---------------------------------------------------------
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"  # stopped by the provider's admin
STATUS_EXPIRED = "expired"

# -- RemoteSubscription.access: ``subscriptionAccess``, one setting for all of a provider's users -
ACCESS_LINK = "link"  # only the token link (``url``) serves the config
ACCESS_BOTH = "both"  # either
ACCESS_APP = "app"  # only GET /api/v1/app/proxy/subscriptions/{id}/config, signed in

# -- reset modes: ``resetMode`` ------------------------------------------------------------------
RESET_PURCHASE_DAY = "purchase_day"  # traffic starts over monthly from periodStart's day
RESET_MONTH_START = "month_start"  # on the 1st, in the provider's time zone
RESET_NONE = "none"

# -- plan kinds and periods: PublicPlan ``kind``, Price ``period`` ---------------------------------
PLAN_SUBSCRIPTION = "subscription"
PLAN_TRAFFIC_PACK = "traffic_pack"  # adds traffic to a subscription; priced one_time
PERIOD_MONTH = "month"  # 月付
PERIOD_QUARTER = "quarter"  # 季付
PERIOD_HALF_YEAR = "half_year"  # 半年付
PERIOD_YEAR = "year"  # 年付
PERIOD_ONE_TIME = "one_time"  # 一次性

# -- orders: OrderSummary ``kind`` and ``status`` --------------------------------------------------
ORDER_NEW = "new"
ORDER_RENEW = "renew"
ORDER_CHANGE = "change"
ORDER_TRAFFIC_PACK = "traffic_pack"
ORDER_TOPUP = "topup"
ORDER_WITHDRAWAL = "withdrawal"
ORDER_PENDING = "pending"  # waiting for payment: 去支付 opens /orders
ORDER_COMPLETED = "completed"
ORDER_CANCELLED = "cancelled"
ORDER_REFUNDED = "refunded"

# -- RemoteSubscription.condition, what the app shows of a subscription ---------------------------
CONDITION_ACTIVE = "active"  # 有效
CONDITION_EXPIRING = "expiring"  # 即将到期: expires within EXPIRING_S
CONDITION_TRAFFIC_LOW = "traffic_low"  # 流量将尽: TRAFFIC_LOW of its traffic used
CONDITION_EXPIRED = "expired"  # 已到期
CONDITION_TRAFFIC_USED_UP = "traffic_used_up"  # 流量已用完
CONDITION_NOT_STARTED = "not_started"  # 未开始
CONDITION_DISABLED = "disabled"  # 已停用
CONDITION_UNUSABLE = "unusable"  # 暂不可用: the provider says unusable and gives no reason


# -- errors ---------------------------------------------------------------------------------------
# The address is no https origin (http only for loopback hosts), or carries a user name.
PROVIDER_INVALID_ADDRESS = "provider_invalid_address"
# The address answers, but not as an OpenWeir panel: GET /api/v1/app/site is no Site.
PROVIDER_NOT_OPENWEIR = "provider_not_openweir"
# No connection, a timeout or a 5xx; the message is the reason (no URL).
PROVIDER_UNREACHABLE = "provider_unreachable"
# 429 (RATE_LIMITED); ProviderError.retry_after carries Retry-After when the answer has one.
PROVIDER_RATE_LIMITED = "provider_rate_limited"
# A redirect, an answer over MAX_JSON_BYTES or not JSON, a missing or mistyped field, a
# subscription of another user, duplicate ids, more than MAX_SUBSCRIPTIONS.
PROVIDER_BAD_RESPONSE = "provider_bad_response"
# Wrong email, user name or password (401 INVALID_EMAIL_OR_PASSWORD, INVALID_USERNAME_OR_PASSWORD,
# or a 400 the server gives for the identifier).
PROVIDER_SIGN_IN_FAILED = "provider_sign_in_failed"
# 403 EMAIL_NOT_VERIFIED: the provider wants the address verified in the user center first.
PROVIDER_EMAIL_UNVERIFIED = "provider_email_unverified"
# The email code is wrong, expired or tried too often (INVALID_OTP, OTP_EXPIRED, TOO_MANY_ATTEMPTS).
PROVIDER_CODE_INVALID = "provider_code_invalid"
# The two-factor code is wrong (401 INVALID_CODE, TOO_MANY_ATTEMPTS_REQUEST_NEW_CODE).
PROVIDER_TWO_FACTOR_INVALID = "provider_two_factor_invalid"
# The two-factor step outlived TWO_FACTOR_TTL_S (401 INVALID_TWO_FACTOR_COOKIE): sign in again.
PROVIDER_TWO_FACTOR_EXPIRED = "provider_two_factor_expired"
# The provider asks for a captcha on this sign-in (CAPTCHA_SIGN_IN, CAPTCHA_SEND_CODE, or 400
# MISSING_RESPONSE / 403 VERIFICATION_FAILED): the text points to 从链接导入.
PROVIDER_CAPTCHA_REQUIRED = "provider_captcha_required"
# The provider offers no method v1 can use (only social, passkey, magic link or phone sign-in):
# the text points to 从链接导入.
PROVIDER_SIGN_IN_UNSUPPORTED = "provider_sign_in_unsupported"
# 403 FORBIDDEN on sign-in ("This account is disabled") or on a signed-in billing call.
PROVIDER_ACCOUNT_DISABLED = "provider_account_disabled"
# Another account of this origin is signed in here; AccountsService.complete(replace=True) swaps.
PROVIDER_ACCOUNT_CONFLICT = "provider_account_conflict"
# 401 on a signed-in call, or no token stored: 需要重新登录.
PROVIDER_SIGN_IN_REQUIRED = "provider_sign_in_required"
# Billing says the subscription is expired (status expired, or unusable past expireAt).
PROVIDER_SUBSCRIPTION_EXPIRED = "provider_subscription_expired"
# Billing says unusable for another reason: traffic used up, not started yet.
PROVIDER_SUBSCRIPTION_UNUSABLE = "provider_subscription_unusable"
# Billing says disabled by the provider.
PROVIDER_SUBSCRIPTION_DISABLED = "provider_subscription_disabled"
# The subscription is no longer in the account (absent from the list, or 404).
PROVIDER_SUBSCRIPTION_MISSING = "provider_subscription_missing"
# Link-only access and no link given (``url`` null), or the link answers 403
# SUBSCRIPTION_LINK_DISABLED.
PROVIDER_LINK_UNAVAILABLE = "provider_link_unavailable"
# 403 SUBSCRIPTION_APP_DISABLED from ProviderApi.config: the provider serves this subscription by
# link only right now. AccountsService.fetch falls back to the link; the UI never shows it.
PROVIDER_APP_DISABLED = "provider_app_disabled"
# No provider with this id here (signed out meanwhile).
PROVIDER_UNKNOWN = "provider_unknown"

# Every code above. ``provider_update_failed`` is no member: it is the host's code for mihomo's
# proxy providers (viewmodels/feedback.py), unrelated to accounts.
PROVIDER_CODES = frozenset(
    {
        PROVIDER_INVALID_ADDRESS,
        PROVIDER_NOT_OPENWEIR,
        PROVIDER_UNREACHABLE,
        PROVIDER_RATE_LIMITED,
        PROVIDER_BAD_RESPONSE,
        PROVIDER_SIGN_IN_FAILED,
        PROVIDER_EMAIL_UNVERIFIED,
        PROVIDER_CODE_INVALID,
        PROVIDER_TWO_FACTOR_INVALID,
        PROVIDER_TWO_FACTOR_EXPIRED,
        PROVIDER_CAPTCHA_REQUIRED,
        PROVIDER_SIGN_IN_UNSUPPORTED,
        PROVIDER_ACCOUNT_DISABLED,
        PROVIDER_ACCOUNT_CONFLICT,
        PROVIDER_SIGN_IN_REQUIRED,
        PROVIDER_SUBSCRIPTION_EXPIRED,
        PROVIDER_SUBSCRIPTION_UNUSABLE,
        PROVIDER_SUBSCRIPTION_DISABLED,
        PROVIDER_SUBSCRIPTION_MISSING,
        PROVIDER_LINK_UNAVAILABLE,
        PROVIDER_APP_DISABLED,
        PROVIDER_UNKNOWN,
    }
)
# What AccountsService.fetch raises when the profile keeps its last config by design (D57): the
# row shows the code's text instead of 更新失败, and automatic updates wait for a sync that sees
# the subscription change instead of retrying every 30 minutes.
KEEPS_CONFIG = frozenset(
    {
        PROVIDER_SIGN_IN_REQUIRED,
        PROVIDER_ACCOUNT_DISABLED,
        PROVIDER_SUBSCRIPTION_EXPIRED,
        PROVIDER_SUBSCRIPTION_UNUSABLE,
        PROVIDER_SUBSCRIPTION_DISABLED,
        PROVIDER_SUBSCRIPTION_MISSING,
        PROVIDER_LINK_UNAVAILABLE,
    }
)


class ProviderError(ProfileError):
    """A provider's API failed or refused; ``code`` is one of PROVIDER_CODES. The UI turns it
    into text like every ProfileError (viewmodels/feedback.py).

    The message is for diagnosis and never holds a URL, a token, a password or a code the
    user typed: urllib's HTTPError carries the URL (``url``, ``filename``), so the client
    builds the message from the status and the API's error ``code`` alone. ``retry_after``
    is the seconds a 429 asked to wait, None without Retry-After."""

    def __init__(self, code: str, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(code, message)
        self.retry_after = retry_after


class ProviderState(enum.StrEnum):
    """Whether the app can act for the account. Diagnostics report it as is (docs/accounts.md
    §8); nothing stores it."""

    # A token is stored, and no call has refused it since.
    SIGNED_IN = "signed_in"
    # No token: a call answered 401, the provider was restored from a backup, or the token was
    # never stored. Profiles keep their configs; their automatic updates wait. 需要重新登录.
    SIGN_IN_REQUIRED = "sign_in_required"
    # The provider refused the account (403): like SIGN_IN_REQUIRED, the text in danger.
    ACCOUNT_DISABLED = "account_disabled"
    # The credential store could not be read (a locked keychain, CredentialError
    # credentials_unavailable): unknown for now, read again at the next sync.
    CREDENTIALS_UNAVAILABLE = "credentials_unavailable"


# -- what GET /api/v1/app/site answers -----------------------------------------------------------
@dataclass(frozen=True)
class Currency:
    """``currency`` of the site: the one currency of the provider's prices and balances."""

    code: str  # ``code``, ISO 4217 such as "CNY"
    symbol: str  # ``symbol``, in the request's language, such as "¥"
    digits: int  # ``digits``, decimal places of the minor unit: 1500 with 2 is 15.00


@dataclass(frozen=True)
class Site:
    """GET /api/v1/app/site, public: it is how the app tells an OpenWeir panel at an address."""

    name: str  # ``name``, the provider's name, shown as the account's site
    currency: Currency  # ``currency``
    description: str = ""  # ``description``, in the request's language; null is ""
    default_locale: str = ""  # ``defaultLocale``: "zh-CN" or "en-US"


@dataclass(frozen=True)
class SignInOptions:
    """What sign-in the provider allows, from two public endpoints:

    ``methods``      the ``type`` of each AuthProvider in GET /api/v1/app/auth-settings/providers
                     (enabled ones, in display order), such as METHOD_EMAIL_PASSWORD
    ``captcha``      ``scenes`` of GET /api/v1/app/captcha, such as CAPTCHA_SIGN_IN
    ``code_length``  ``options.otpLength`` of the email_otp provider (6 without one)

    v1 signs in natively only (D56): no captcha, no browser."""

    methods: tuple[str, ...] = ()
    captcha: tuple[str, ...] = ()
    code_length: int = 6

    @property
    def username(self) -> bool:
        """The identifier may be a user name (it has no "@"), not only an email."""
        return METHOD_USERNAME in self.methods

    @property
    def password(self) -> bool:
        """Email or user name with a password works natively."""
        return (
            METHOD_EMAIL_PASSWORD in self.methods or METHOD_USERNAME in self.methods
        ) and CAPTCHA_SIGN_IN not in self.captcha

    @property
    def email_code(self) -> bool:
        """Sign-in with a code sent by email works natively."""
        return METHOD_EMAIL_CODE in self.methods and CAPTCHA_SEND_CODE not in self.captcha

    @property
    def unavailable(self) -> str:
        """Why no method works natively, as an error code: PROVIDER_CAPTCHA_REQUIRED when a
        captcha guards the methods v1 knows, PROVIDER_SIGN_IN_UNSUPPORTED when the provider
        has none of them; empty when one works. Both texts point to 从链接导入."""
        if self.password or self.email_code:
            return ""
        known = {METHOD_EMAIL_PASSWORD, METHOD_USERNAME, METHOD_EMAIL_CODE}
        if known.intersection(self.methods):
            return PROVIDER_CAPTCHA_REQUIRED
        return PROVIDER_SIGN_IN_UNSUPPORTED


# -- sign-in -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Account:
    """Who signed in: ``user`` of a sign-in answer, or GET /api/v1/app/me."""

    id: str  # ``id``, the provider's user id; billing ``userId`` must match it
    email: str  # ``email``; a phone sign-up has a placeholder ending in ".invalid"
    name: str = ""  # ``name``
    username: str = ""  # ``displayUsername``, else ``username``; "" without the username method

    @property
    def display(self) -> str:
        """What the app shows of the account (ProviderRecord.account): the email, or the user
        name or name when the email is a placeholder."""
        if self.email and not self.email.endswith(".invalid"):
            return self.email
        return self.username or self.name or self.email


@dataclass(frozen=True)
class Session:
    """A signed-in session: Better Auth's bearer token, from the ``set-auth-token`` header of
    the answer (else its ``token``). It is sliding (7 days of disuse) and has no refresh token.
    AccountsService.complete stores ``token`` and ``account.id`` in the credential store."""

    origin: str
    token: str = field(repr=False)
    account: Account

    @property
    def user_id(self) -> str:
        return self.account.id


@dataclass(frozen=True)
class TwoFactorChallenge:
    """A password or email-code sign-in that answered ``{"twoFactorRedirect": true,
    "twoFactorMethods": [...]}`` instead of a session. The answer set Better Auth's
    ``two_factor`` cookie; ``cookies`` are the name and value of every cookie it set, which the
    verification sends back with ``Origin: <origin>``. They are secret and short-lived."""

    origin: str
    identifier: str  # what the user typed, shown on the two-factor step
    methods: tuple[str, ...]  # ``twoFactorMethods``; ("totp",) when the answer has none
    expires_at: float  # unix seconds: the answer's time plus TWO_FACTOR_TTL_S
    cookies: tuple[tuple[str, str], ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class ProviderRecord:
    """A provider signed in to on this device: a row of the ``providers`` table (T2). One per
    origin; signing out deletes it (softly, for backups)."""

    id: str  # uuid4().hex, made here; the token's key is provider:<id>
    origin: str  # https://host[:port], no path
    name: str  # Site.name at the last probe or sync
    account: str  # Account.display at sign-in
    user_id: str  # Account.id; a later sign-in as another user asks to replace (D55)
    position: int = 0  # order among providers (the account page's select)
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class Probe:
    """What the sign-in dialog learns from an address before asking for anything."""

    origin: str  # the address made an origin
    site: Site
    options: SignInOptions
    provider: ProviderRecord | None = None  # signed in to this origin here already


# -- billing -------------------------------------------------------------------------------------
_LINKS = ("url", "urls")


@dataclass(frozen=True)
class RemoteSubscription:
    """One subscription of the account: an item of GET /api/v1/app/billing/subscriptions
    (MyBillingSubscription), GET …/subscriptions/{id}, or the answer of PATCH …/{id} (which
    has no ``subscriptionAccess``: ``access`` is "" then).

    ``url`` and ``urls`` are the token link and its mirrors; null while the provider serves
    configs to the app only. They are as good as a password and live in memory only: they stay
    out of ``repr``, equality and ``record``, and never reach the database, a backup, a log or
    an error message."""

    id: int  # ``id``; a profile's remote_id
    plan_id: str  # ``planId``
    status: str  # ``status``: STATUS_ACTIVE, STATUS_DISABLED, STATUS_EXPIRED
    usable: bool  # ``usable``: active, started, not expired and traffic left, by the server
    plan_name: str = ""  # ``planName`` in the request's language; null is ""
    user_id: str = ""  # ``userId``; must equal the signed-in Account.id
    trial: bool = False  # ``trial``
    start_at: float = 0.0  # ``startAt``
    expire_at: float | None = None  # ``expireAt``; None never expires
    period_start: float = 0.0  # ``periodStart``: the traffic period's start
    traffic_limit: int | None = None  # ``trafficLimit`` per period; None is unlimited
    extra_bytes: int = 0  # ``extraBytes`` from traffic packs
    upload: int = 0  # ``upload``
    download: int = 0  # ``download``
    total: int | None = None  # ``total`` = trafficLimit + extraBytes; None is unlimited
    speed_limit: int = 0  # ``speedLimit`` in Mbps; 0 is unlimited
    device_limit: int = 0  # ``deviceLimit``, concurrent IPs; 0 is unlimited
    online_devices: int = 0  # ``onlineDevices``
    reset_mode: str = RESET_PURCHASE_DAY  # ``resetMode``
    auto_renew: bool = False  # ``autoRenew``
    access: str = ""  # ``subscriptionAccess``: ACCESS_LINK, ACCESS_BOTH, ACCESS_APP
    created_at: float = 0.0  # ``createdAt``
    updated_at: float = 0.0  # ``updatedAt``; a change of it is a reason to fetch the config
    url: str | None = field(default=None, repr=False, compare=False)  # ``url``
    urls: tuple[str, ...] = field(default=(), repr=False, compare=False)  # ``urls``

    @property
    def used(self) -> int:
        return self.upload + self.download

    @property
    def usage(self) -> Usage:
        """The usage a provider profile shows (Profile.usage), as Subscription-Userinfo
        would give it: 0 for no quota and for no expiry."""
        return Usage(self.upload, self.download, self.total or 0, int(self.expire_at or 0))

    def condition(self, now: float) -> str:
        """What the app shows of it at ``now`` (unix seconds), one of the CONDITION_*
        constants; ``trial`` is shown beside it. The server's ``status`` and ``usable`` win
        over the clock: a disabled or expired subscription is that whatever the dates say, and
        an unusable one is never active."""
        expired = self.expire_at is not None and self.expire_at <= now
        used_up = self.total is not None and self.used >= self.total
        if self.status == STATUS_DISABLED:
            return CONDITION_DISABLED
        if self.status == STATUS_EXPIRED or (expired and not self.usable):
            return CONDITION_EXPIRED
        if not self.usable:
            if used_up:
                return CONDITION_TRAFFIC_USED_UP
            if self.start_at > now:
                return CONDITION_NOT_STARTED
            return CONDITION_EXPIRED if expired else CONDITION_UNUSABLE
        if self.expire_at is not None and self.expire_at - now <= EXPIRING_S:
            return CONDITION_EXPIRING
        if self.total and self.used >= self.total * TRAFFIC_LOW:
            return CONDITION_TRAFFIC_LOW
        return CONDITION_ACTIVE

    def record(self) -> dict[str, Any]:
        """The fields to store (``provider_subscriptions.data``, T2) by their names here,
        without the links."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name not in _LINKS}

    @classmethod
    def from_record(cls, data: dict[str, Any]) -> RemoteSubscription:
        """The subscription ``record`` stored, without links; keys it does not know are left
        out. Raises TypeError when a field it needs is missing."""
        known = {f.name for f in fields(cls)} - set(_LINKS)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class Level:
    """A wallet's ``level`` or ``nextLevel``: the discount the provider gives by spending."""

    id: str  # ``id``
    name: str  # ``name`` in the request's language
    discount_percent: int = 0  # ``discountPercent``
    min_spent: int | None = None  # ``minSpent``, minor units; None: not used
    min_days: int | None = None  # ``minDays``, paid days; None: not used


@dataclass(frozen=True)
class Wallet:
    """GET /api/v1/app/billing/wallet. Never stored: read each time the account page shows."""

    currency: str  # ``currency``, ISO 4217; format with Site.currency when the codes match
    balance: int = 0  # ``balance``, minor units
    points: int = 0  # ``points``
    commission: int = 0  # ``commission``, minor units
    total_spent: int = 0  # ``totalSpent``, minor units
    paid_days: int = 0  # ``paidDays``
    level: Level | None = None  # ``level``
    next_level: Level | None = None  # ``nextLevel``; None at the top or under an admin's level
    user_id: str = ""  # ``userId``; must equal the signed-in Account.id


@dataclass(frozen=True)
class Price:
    """One of a plan's ``prices``."""

    period: str  # ``period``: PERIOD_MONTH … PERIOD_ONE_TIME
    amount: int  # ``amount``, minor units of the plan's currency


@dataclass(frozen=True)
class PublicPlan:
    """A plan on sale: an item of ``plans`` in GET /api/v1/app/billing/plans (public; signed
    in, the provider adds plans for the user's level). Never stored."""

    id: str  # ``id``; 选购 opens /plans?checkout=<id>:<period>
    kind: str  # ``kind``: PLAN_SUBSCRIPTION or PLAN_TRAFFIC_PACK
    name: str  # ``name`` in the request's language
    currency: str  # the answer's ``currency``, ISO 4217
    prices: tuple[Price, ...] = ()  # ``prices``
    description: str = ""  # ``description``; null is ""
    # ``trafficBytes`` per period, or what a traffic pack adds; None is unlimited.
    traffic_bytes: int | None = None
    speed_limit: int = 0  # ``speedLimit`` in Mbps; 0 is unlimited
    device_limit: int = 0  # ``deviceLimit``; 0 is unlimited
    reset_mode: str = RESET_PURCHASE_DAY  # ``resetMode``
    tags: tuple[str, ...] = ()  # ``tags``
    sold_out: bool = False  # ``soldOut``


@dataclass(frozen=True)
class OrderSummary:
    """An item of ``items`` in GET /api/v1/app/billing/orders (newest first). Never stored."""

    id: str  # ``id``, the order number
    kind: str  # ``kind``: ORDER_NEW … ORDER_WITHDRAWAL
    status: str  # ``status``: ORDER_PENDING … ORDER_REFUNDED
    currency: str  # ``currency``, ISO 4217
    amount: int  # ``amount``: left to pay through a payment channel, minor units
    created_at: float  # ``createdAt``
    price: int = 0  # ``price``: the list price (a top-up: the amount added)
    paid_amount: int = 0  # ``paidAmount``
    plan_id: str | None = None  # ``planId``
    plan_name: str = ""  # ``planName``; null (top-ups) is ""
    period: str | None = None  # ``period``
    subscription_id: int | None = None  # ``subscriptionId``
    expires_at: float | None = None  # ``expiresAt``: an unpaid order is cancelled then
    paid_at: float | None = None  # ``paidAt``

    @property
    def pending(self) -> bool:
        return self.status == ORDER_PENDING


# -- what AccountsService answers ----------------------------------------------------------------
@dataclass(frozen=True)
class AccountSummary:
    """A provider as the account page and the sidebar show it, from memory and the database:
    no request is made for it."""

    provider: ProviderRecord
    state: ProviderState
    # As last synced, ordered by id, without links.
    subscriptions: tuple[RemoteSubscription, ...] = ()
    site: Site | None = None  # from the last probe or sync of this run; None before one
    synced_at: float | None = None  # the last sync that succeeded, this run; None before one
    error: str = ""  # the code of the last sync's failure; "" once one succeeds
    active: int | None = None  # the remote id whose profile the tunnel uses, if one of these


@dataclass(frozen=True)
class SignedIn:
    """What AccountsService.complete did: the provider stored, its subscriptions synced into
    profiles. Configs are not downloaded yet (``use`` and ``sync`` do that)."""

    provider: ProviderRecord
    subscriptions: tuple[RemoteSubscription, ...]  # for the dialog's picker
    profiles: ProfileList


@dataclass(frozen=True)
class ProviderRemoval:
    """What signing out of a provider removes, for the confirmation shown before it."""

    provider: ProviderRecord
    profiles: tuple[Profile, ...]  # the provider's profiles, all removed
    active: bool  # one of them is in use (订阅自带)
    successor: Profile | None  # whose rules are used afterwards, when ``active``


# -- interfaces ----------------------------------------------------------------------------------
class ProviderApi(Protocol):
    """The HTTP client (T1, models/provider/api.py). Stateless apart from what each call
    takes; it stores nothing. Every method raises ProviderError.

    Every request runs in the UI process as the user on a daemon worker thread (host/workers.py),
    follows the system proxy, times out after 30 seconds and sends:

        User-Agent       OpenWeir/<version> (Windows|macOS), except config and link downloads,
                         which send subscription.USER_AGENT (D48)
        Accept-Language  zh-CN or en-US from the UI language (i18n.current_language), so names
                         and descriptions come in it
        Accept           application/json, except config and link downloads

    POSTs to /api/auth/* also send ``Origin: <origin>`` (Better Auth checks it). No request
    follows a redirect: a 3xx is PROVIDER_BAD_RESPONSE. ``Authorization: Bearer <token>`` only
    ever goes to the origin. JSON answers are read up to MAX_JSON_BYTES, configs up to
    subscription.MAX_CONFIG_BYTES. API errors are JSON ``{"code", "message"}``; the mapping to
    PROVIDER_CODES is in docs/accounts.md §10. Arguments named ``origin`` come from
    ``api.origin(address)`` (T1): https://host[:port], http only for loopback hosts."""

    async def site(self, origin: str) -> Site:
        """GET /api/v1/app/site. PROVIDER_NOT_OPENWEIR when it answers anything but a Site."""
        ...

    async def sign_in_options(self, origin: str) -> SignInOptions:
        """GET /api/v1/app/auth-settings/providers and GET /api/v1/app/captcha."""
        ...

    async def sign_in_password(
        self, origin: str, identifier: str, password: str
    ) -> Session | TwoFactorChallenge:
        """POST /api/auth/sign-in/email ``{email, password}`` when ``identifier`` has an "@",
        else /api/auth/sign-in/username ``{username, password}``. The account comes from the
        answer's ``user``."""
        ...

    async def send_email_code(self, origin: str, email: str) -> None:
        """POST /api/auth/email-otp/send-verification-otp ``{email, type: "sign-in"}``."""
        ...

    async def sign_in_email_code(
        self, origin: str, email: str, code: str
    ) -> Session | TwoFactorChallenge:
        """POST /api/auth/sign-in/email-otp ``{email, otp}``."""
        ...

    async def verify_totp(self, challenge: TwoFactorChallenge, code: str) -> Session:
        """POST /api/auth/two-factor/verify-totp ``{code}`` with the challenge's cookies.
        PROVIDER_TWO_FACTOR_EXPIRED without a request once ``expires_at`` passed. The account
        comes from the answer's ``user``, else from GET /api/v1/app/me with the new token."""
        ...

    async def account(self, origin: str, token: str) -> Account:
        """GET /api/v1/app/me."""
        ...

    async def sign_out(self, origin: str, token: str) -> None:
        """POST /api/auth/sign-out: the provider ends the session. 401 is no error."""
        ...

    async def subscriptions(self, origin: str, token: str) -> tuple[RemoteSubscription, ...]:
        """GET /api/v1/app/billing/subscriptions, ordered by id. PROVIDER_BAD_RESPONSE for
        duplicate ids or more than MAX_SUBSCRIPTIONS; the caller checks ``user_id``."""
        ...

    async def subscription(self, origin: str, token: str, remote_id: int) -> RemoteSubscription:
        """GET /api/v1/app/billing/subscriptions/{id}; 404 is PROVIDER_SUBSCRIPTION_MISSING."""
        ...

    async def set_auto_renew(
        self, origin: str, token: str, remote_id: int, enabled: bool
    ) -> RemoteSubscription:
        """PATCH /api/v1/app/billing/subscriptions/{id} ``{autoRenew}``; the answer has no
        ``subscriptionAccess`` (``access`` is "") and may have no links."""
        ...

    async def wallet(self, origin: str, token: str) -> Wallet:
        """GET /api/v1/app/billing/wallet."""
        ...

    async def plans(self, origin: str, token: str | None) -> tuple[PublicPlan, ...]:
        """GET /api/v1/app/billing/plans, with the token when there is one."""
        ...

    async def orders(self, origin: str, token: str, limit: int) -> tuple[OrderSummary, ...]:
        """GET /api/v1/app/billing/orders?page=1&pageSize=<limit>, newest first; withdrawals
        left out."""
        ...

    async def config(self, origin: str, token: str, remote_id: int) -> Download:
        """GET /api/v1/app/proxy/subscriptions/{id}/config with the token and the D48 user
        agent, read like a subscription download (Subscription-Userinfo, Profile-Update-
        Interval, Content-Disposition). 403 SUBSCRIPTION_APP_DISABLED is PROVIDER_APP_DISABLED,
        404 PROVIDER_SUBSCRIPTION_MISSING. No ETag: the provider caches renders 10 minutes."""
        ...

    async def link(self, url: str) -> Download:
        """GET a subscription's token link (RemoteSubscription.url or one of ``urls``, any
        host): no token, no cookies, no redirects, the D48 user agent. 403
        SUBSCRIPTION_LINK_DISABLED and 404 are PROVIDER_LINK_UNAVAILABLE. The URL is never
        stored nor put in an error."""
        ...


class AccountsService(Protocol):
    """What the view models call (T3, models/library/accounts.py, as ProfileLibrary.accounts).
    It holds the provider records, their state and synced subscriptions in memory, the tokens
    in the credential store (ProviderTokens, key ``provider:<id>``, value
    ``{"token", "user_id"}``), and changes profiles through the library and its lock; network
    requests run outside the lock. Methods raise ProviderError, or ProfileError from the
    library and the stores. A signed-in call that answers 401 deletes the token and leaves the
    provider SIGN_IN_REQUIRED (403 account disabled: ACCOUNT_DISABLED) before raising;
    nothing removes profiles but ``sign_out``."""

    async def summaries(self) -> tuple[AccountSummary, ...]:
        """Every provider, by position. The 账户 page shows while there is one."""
        ...

    async def summary(self, provider_id: str) -> AccountSummary:
        """PROVIDER_UNKNOWN for an id it does not hold."""
        ...

    async def probe(self, address: str) -> Probe:
        """Make the address an origin (PROVIDER_INVALID_ADDRESS), then read its Site and
        SignInOptions (PROVIDER_NOT_OPENWEIR, PROVIDER_UNREACHABLE). It signs in to nothing:
        an openweir://provider link opens the dialog with the address only."""
        ...

    async def sign_in_password(
        self, probe: Probe, identifier: str, password: str
    ) -> Session | TwoFactorChallenge:
        """Sign in with an email or user name and a password. Before any request: while
        ``probe.options.password`` is false, PROVIDER_CAPTCHA_REQUIRED when a captcha guards
        it, else PROVIDER_SIGN_IN_UNSUPPORTED. Stores nothing: ``complete`` does."""
        ...

    async def send_email_code(self, probe: Probe, email: str) -> None:
        """Ask the provider to email a sign-in code. Before any request: while
        ``probe.options.email_code`` is false, PROVIDER_CAPTCHA_REQUIRED when a captcha guards
        sending codes, else PROVIDER_SIGN_IN_UNSUPPORTED."""
        ...

    async def sign_in_email_code(
        self, probe: Probe, email: str, code: str
    ) -> Session | TwoFactorChallenge:
        """Sign in with the emailed code. Stores nothing."""
        ...

    async def verify_two_factor(self, challenge: TwoFactorChallenge, code: str) -> Session:
        """Finish a two-factor sign-in with an authenticator code. Stores nothing."""
        ...

    async def complete(self, probe: Probe, session: Session, *, replace: bool = False) -> SignedIn:
        """Keep the session and bring its subscriptions in as profiles.

        A provider at this origin with another ``user_id`` raises PROVIDER_ACCOUNT_CONFLICT
        (its account in the message) unless ``replace``, which first signs that one out like
        ``sign_out``. Same user (signing in again, or a provider restored from a backup): the
        record stays, the new token replaces the old and the profiles relink by remote id.
        Otherwise a new record. Then it syncs billing like ``sync`` but downloads no config.
        The dialog then applies the picker's rules (docs/accounts.md §7.3) through ``use``."""
        ...

    async def use(self, provider_id: str, remote_id: int) -> ProfileList:
        """Make that subscription's profile the one in use, downloading its config first when
        it has none (the profile's update), then ProfileLibrary.profiles.activate: a running
        tunnel reloads. PROVIDER_SUBSCRIPTION_* when billing says it cannot be used."""
        ...

    async def sync(self, provider_id: str) -> Updated:
        """Read the account's subscriptions and bring the profiles in line (docs/accounts.md
        §5.2): add new ones, rename, mark missing ones, copy billing usage into
        Profile.usage, then update the configs of usable profiles without one or whose
        (plan_id, status, usable, updated_at) changed. Records ``synced_at`` or ``error``.
        Raises its failure; PROVIDER_RATE_LIMITED without a request while a Retry-After runs."""
        ...

    async def sync_all(self) -> Updated:
        """``sync`` every signed-in provider that is due by the backoff (docs/accounts.md
        §5.3), for the timers. A provider's failure is recorded, not raised."""
        ...

    async def sign_out_removal(self, provider_id: str) -> ProviderRemoval:
        """What ``sign_out`` would remove now."""
        ...

    async def sign_out(self, provider_id: str) -> ProfileList:
        """End the session on the provider (best effort, offline included), delete the token,
        remove the provider's profiles as ProfileLibrary.profiles.remove does (the successor
        takes over, a running tunnel reloads), and delete the record."""
        ...

    async def fetch(self, profile: Profile) -> Download:
        """A provider profile's config, for the library's update of it (D57). Billing first:
        GET the subscription, then by what it says

            401                                 PROVIDER_SIGN_IN_REQUIRED
            404 or absent                       PROVIDER_SUBSCRIPTION_MISSING
            status disabled                     PROVIDER_SUBSCRIPTION_DISABLED
            expired, or unusable past expiry    PROVIDER_SUBSCRIPTION_EXPIRED
            otherwise unusable                  PROVIDER_SUBSCRIPTION_UNUSABLE
            usable, access app or both          ProviderApi.config; PROVIDER_APP_DISABLED falls
                                                through to the link
            usable, access link                 ProviderApi.link(url), then each of ``urls`` on
                                                PROVIDER_UNREACHABLE; PROVIDER_LINK_UNAVAILABLE
                                                without one

        The codes in KEEPS_CONFIG keep the last config."""
        ...

    async def wallet(self, provider_id: str) -> Wallet:
        """Read each time; never stored."""
        ...

    async def plans(self, provider_id: str) -> tuple[PublicPlan, ...]:
        """Read each time; never stored."""
        ...

    async def orders(self, provider_id: str, limit: int = 5) -> tuple[OrderSummary, ...]:
        """The latest orders, newest first. Read each time; never stored."""
        ...

    async def set_auto_renew(
        self, provider_id: str, remote_id: int, enabled: bool
    ) -> RemoteSubscription:
        """The one change the app makes to billing (D58); the stored copy follows, keeping its
        ``access``."""
        ...
