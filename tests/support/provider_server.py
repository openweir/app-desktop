"""A stand-in for an OpenWeir provider (api-edge behind its user center) on 127.0.0.1, answering
what models/provider/api.py asks the way api-edge and Better Auth do: the public site, sign-in
methods and captcha; sign-in by email or user name and password, the two-factor step with its
cookie and Origin check, email codes, sign-out; me; billing (subscriptions, auto-renew,
devices, wallet, plans, orders); the signed-in config and the token link.

Switches change what it answers: ``access`` (link, both, app), a subscription's fields,
``scenes`` (captcha), ``revoke`` (401 for every token), ``fail`` and ``redirect`` (any status,
code, Retry-After or Location, once or more). It records every request with its headers, so
tests can check the User-Agent, Accept-Language and where the bearer token went. The tests
never reach a real provider."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

NOW = 1_800_000_000.0
DAY = 86400.0
GIB = 1024**3
EMAIL = "user@example.com"
PASSWORD = "correct horse battery"
USERNAME = "user1"
TOTP = "246810"
CONFIG = b"mixed-port: 7890\nproxies: []\nproxy-groups: []\nrules: []\n"
SESSION_COOKIE = "better-auth.session_token"
TWO_FACTOR_COOKIE = "better-auth.two_factor"
_SECRET = b"fake-provider-secret"
_CONFIG = re.compile(r"/api/v1/app/proxy/subscriptions/([0-9]+)/config")
_LINK = re.compile(r"/api/v1/app/proxy/subscriptions/([A-Za-z0-9_-]+)")
_SUBSCRIPTION = re.compile(r"/api/v1/app/billing/subscriptions/([0-9]+)(/devices)?")


def iso(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


@dataclass
class FakeUser:
    id: str
    email: str
    password: str
    name: str = "User"
    username: str = ""
    totp: str = ""  # the code the authenticator shows now; "" without two-factor
    banned: bool = False
    email_verified: bool = True

    def json(self) -> dict[str, Any]:
        user: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "emailVerified": self.email_verified,
            "image": None,
            "twoFactorEnabled": bool(self.totp),
        }
        if self.username:
            user["username"] = self.username.lower()
            user["displayUsername"] = self.username
        return user


@dataclass(frozen=True)
class Recorded:
    """A request as the server saw it; header names are lowercase."""

    route: str
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def bearer(self) -> str | None:
        value = self.header("authorization") or ""
        return value[7:] if value.startswith("Bearer ") else None

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class _Answer:
    status: int
    payload: Any  # bytes as they are, anything else as JSON
    headers: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _Challenge:
    user_id: str
    expires_at: float


class FakeProvider:
    def __init__(self, *, seed: bool = True) -> None:
        self.now = NOW  # the server's clock (two-factor cookies), unix seconds
        self.site: dict[str, Any] = {
            "name": "示例服务商",
            "description": "测试用的服务商",
            "defaultLocale": "zh-CN",
            "images": {"logo": False, "share": False},
            "currency": {"code": "CNY", "symbol": "¥", "digits": 2},
        }
        self.methods: list[dict[str, Any]] = [
            {
                "id": "email",
                "type": "email_password",
                "name": "邮箱",
                "category": "basic",
                "options": {
                    "allowSignUp": True,
                    "requireEmailVerification": False,
                    "minPasswordLength": 8,
                },
            },
            {"id": "username", "type": "username", "name": "用户名", "category": "basic"},
            {
                "id": "otp",
                "type": "email_otp",
                "name": "邮箱验证码",
                "category": "basic",
                "options": {"otpLength": 6},
            },
            {"id": "2fa", "type": "two_factor", "name": "两步验证", "category": "security"},
        ]
        self.scenes: list[str] = []  # captcha scenes: sign_in, send_code
        self.access = "both"  # proxy.subscriptionAccess: link, both, app
        self.require_email_verification = False
        self.header_token = True  # answer set-auth-token (else only the body's token)
        self.user_after_two_factor = True  # verify-totp answers the user
        self.mirror: str | None = None  # a second subscription domain for ``urls``
        self.config = CONFIG
        self.users: dict[str, FakeUser] = {}
        self.subscriptions: dict[int, dict[str, Any]] = {}
        self.wallets: dict[str, dict[str, Any]] = {}
        self.plans: list[dict[str, Any]] = []
        self.orders: dict[str, list[dict[str, Any]]] = {}
        self.sessions: dict[str, str] = {}  # raw token: user id
        self.codes: dict[str, str] = {}  # email: the code sent
        self.link_tokens: dict[str, int] = {}
        self.requests: list[Recorded] = []
        self._challenges: dict[str, _Challenge] = {}
        self._answers: dict[str, list[_Answer]] = {}
        self._holds: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        if seed:
            self._seed()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fake._serve(self, "GET")

            def do_POST(self) -> None:
                fake._serve(self, "POST")

            def do_PATCH(self) -> None:
                fake._serve(self, "PATCH")

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        # A short poll keeps close() (shutdown) quick.
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()

    def close(self) -> None:
        with self._lock:
            for gate in self._holds.values():
                gate.set()
        self._server.shutdown()
        self._server.server_close()

    # -- data --------------------------------------------------------------------------------
    def _seed(self) -> None:
        user = self.add_user(
            FakeUser("user-1", EMAIL, PASSWORD, name="Example User", username=USERNAME)
        )
        self.add_subscription(user.id, 7, planId="pro", planName="专业版")
        self.add_subscription(
            user.id,
            9,
            planId="basic",
            planName="基础版",
            status="expired",
            usable=False,
            expireAt=iso(NOW - 2 * DAY),
        )
        self.add_user(FakeUser("user-2", "other@example.com", PASSWORD))
        self.add_subscription("user-2", 11, planId="pro", planName="专业版")
        self.wallets[user.id] = {
            "userId": user.id,
            "currency": "CNY",
            "balance": 1500,
            "points": 120,
            "commission": 0,
            "totalSpent": 9900,
            "paidDays": 90,
            "level": {
                "id": "silver",
                "name": "白银",
                "minSpent": 5000,
                "minDays": None,
                "discountPercent": 5,
            },
            "levelOverride": None,
            "nextLevel": {
                "id": "gold",
                "name": "黄金",
                "minSpent": 20000,
                "minDays": 365,
                "discountPercent": 10,
            },
        }
        self.plans = [
            {
                "id": "pro",
                "kind": "subscription",
                "name": "专业版",
                "description": "100 GiB 每月",
                "prices": [
                    {"period": "month", "amount": 1500},
                    {"period": "year", "amount": 15000},
                ],
                "trafficBytes": 100 * GIB,
                "speedLimit": 0,
                "deviceLimit": 3,
                "resetMode": "purchase_day",
                "tags": ["推荐"],
                "soldOut": False,
            },
            {
                "id": "pack-50",
                "kind": "traffic_pack",
                "name": "流量包 50 GiB",
                "description": None,
                "prices": [{"period": "one_time", "amount": 500}],
                "trafficBytes": 50 * GIB,
                "speedLimit": 0,
                "deviceLimit": 0,
                "resetMode": "none",
                "tags": [],
                "soldOut": True,
            },
        ]
        self.orders[user.id] = [
            self.order("o-3", "renew", "pending", amount=1500, planId="pro", period="month"),
            self.order("o-2", "withdrawal", "completed", amount=0),
            self.order("o-1", "new", "completed", amount=1500, planId="pro", period="month"),
        ]

    def add_user(self, user: FakeUser) -> FakeUser:
        with self._lock:
            self.users[user.id] = user
        return user

    def add_subscription(self, user_id: str, remote_id: int, **fields: Any) -> dict[str, Any]:
        """A subscription as api-edge stores it (its JSON names); ``fields`` override."""
        subscription: dict[str, Any] = {
            "id": remote_id,
            "userId": user_id,
            "planId": "pro",
            "planName": "专业版",
            "status": "active",
            "usable": True,
            "trial": False,
            "startAt": iso(NOW - 30 * DAY),
            "expireAt": iso(NOW + 30 * DAY),
            "periodStart": iso(NOW - 10 * DAY),
            "trafficLimit": 100 * GIB,
            "extraBytes": 0,
            "upload": 1 * GIB,
            "download": 9 * GIB,
            "total": 100 * GIB,
            "speedLimit": 0,
            "deviceLimit": 3,
            "resetMode": "purchase_day",
            "autoRenew": False,
            "onlineDevices": 2,
            "createdAt": iso(NOW - 30 * DAY),
            "updatedAt": iso(NOW - DAY),
            **fields,
        }
        with self._lock:
            self.subscriptions[remote_id] = subscription
            self.link_tokens[f"tok{secrets.token_hex(12)}"] = remote_id
        return subscription

    def move_to(self, now: float) -> None:
        """Date everything around ``now`` rather than NOW, for a window that runs by the real
        clock (scripts/preview.py, the account page's tests): subscriptions start, reset and
        expire, and orders were placed, as long before or after it."""
        shift = now - self.now
        with self._lock:
            items = [*self.subscriptions.values(), *(o for x in self.orders.values() for o in x)]
            for item in items:
                for key, value in item.items():
                    if isinstance(value, str) and (key.endswith("At") or key == "periodStart"):
                        moved = datetime.fromisoformat(value).timestamp() + shift
                        item[key] = iso(moved)
            self.now = now

    def session(self, user_id: str = "user-1") -> str:
        """A session of ``user_id``, as signing in opens one: its bearer token."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self.sessions[token] = user_id
        return _signed(token)

    def update_subscription(self, remote_id: int, **fields: Any) -> None:
        with self._lock:
            self.subscriptions[remote_id].update(fields)

    @staticmethod
    def order(order_id: str, kind: str, status: str, **fields: Any) -> dict[str, Any]:
        order: dict[str, Any] = {
            "id": order_id,
            "currency": "CNY",
            "kind": kind,
            "planId": None,
            "period": None,
            "subscriptionId": None,
            "price": fields.get("amount", 0),
            "discount": 0,
            "credit": 0,
            "pointsUsed": 0,
            "pointsAmount": 0,
            "balanceUsed": 0,
            "amount": 0,
            "status": status,
            "planName": "专业版" if fields.get("planId") else None,
            "paidAmount": 0,
            "pointsEarned": 0,
            "paidDays": 0,
            "channelId": None,
            "expiresAt": iso(NOW + DAY),
            "paidAt": iso(NOW - DAY) if status == "completed" else None,
            "delivery": None,
            "deliveryReason": None,
            "createdAt": iso(NOW - DAY),
            "updatedAt": iso(NOW - DAY),
        }
        order.update(fields)
        return order

    def link(self, remote_id: int, base: str | None = None) -> str:
        """The token link of a subscription, on this server or ``base``."""
        with self._lock:
            token = next(t for t, i in self.link_tokens.items() if i == remote_id)
        return f"{base or self.base}/api/v1/app/proxy/subscriptions/{token}"

    # -- switches ----------------------------------------------------------------------------
    def answer(
        self,
        route: str,
        status: int,
        payload: Any,
        *,
        headers: list[tuple[str, str]] | None = None,
        times: int = 1,
    ) -> None:
        """The next ``times`` requests to ``route`` get this answer instead."""
        with self._lock:
            queue = self._answers.setdefault(route, [])
            queue.extend(_Answer(status, payload, list(headers or [])) for _ in range(times))

    def fail(
        self,
        route: str,
        status: int,
        code: str | None = None,
        *,
        headers: list[tuple[str, str]] | None = None,
        times: int = 1,
    ) -> None:
        """An API error ``{"code", "message"}`` (``{"message"}`` without a code)."""
        payload: dict[str, Any] = {"message": f"failed with {status}"}
        if code is not None:
            payload["code"] = code
        self.answer(route, status, payload, headers=headers, times=times)

    def redirect(self, route: str, location: str, status: int = 302, *, times: int = 1) -> None:
        self.answer(route, status, b"", headers=[("Location", location)], times=times)

    def revoke(self) -> None:
        """Every session ends, as seven days later: signed-in calls answer 401."""
        with self._lock:
            self.sessions.clear()

    def hold(self, route: str) -> threading.Event:
        """Requests to ``route`` are answered once the returned event is set."""
        gate = threading.Event()
        with self._lock:
            self._holds[route] = gate
        return gate

    def requests_for(self, route: str) -> list[Recorded]:
        with self._lock:
            return [r for r in self.requests if r.route == route]

    # -- the server --------------------------------------------------------------------------
    def _serve(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parts = urlsplit(handler.path)
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        headers = {name.lower(): value for name, value in handler.headers.items()}
        route, respond = self._route(method, parts.path)
        recorded = Recorded(route, method, parts.path, parts.query, headers, body)
        with self._lock:
            self.requests.append(recorded)
            queue = self._answers.get(route)
            override = queue.pop(0) if queue else None
            gate = self._holds.get(route)
        if gate is not None:
            gate.wait(timeout=60)
        if override is not None:
            answer = override
        else:
            try:
                answer = respond(recorded)
            except _RefusalError as refusal:
                answer = refusal.answer
        if isinstance(answer.payload, bytes):
            data = answer.payload
            content_type = None
        else:
            data = json.dumps(answer.payload, ensure_ascii=False).encode()
            content_type = "application/json"
        handler.send_response(answer.status)
        names = {name.lower() for name, _ in answer.headers}
        if content_type and "content-type" not in names:
            handler.send_header("Content-Type", content_type)
        for name, value in answer.headers:
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _route(self, method: str, path: str) -> tuple[str, Callable[[Recorded], _Answer]]:
        """The name of the route a request takes, and how it is answered."""
        fixed: dict[tuple[str, str], tuple[str, Callable[[Recorded], _Answer]]] = {
            ("GET", "/api/v1/app/site"): ("site", self._site),
            ("GET", "/api/v1/app/auth-settings/providers"): ("providers", self._providers),
            ("GET", "/api/v1/app/captcha"): ("captcha", self._captcha),
            ("POST", "/api/auth/sign-in/email"): ("sign_in_email", self._sign_in_email),
            ("POST", "/api/auth/sign-in/username"): ("sign_in_username", self._sign_in_username),
            ("POST", "/api/auth/email-otp/send-verification-otp"): ("send_code", self._send_code),
            ("POST", "/api/auth/sign-in/email-otp"): ("sign_in_code", self._sign_in_code),
            ("POST", "/api/auth/two-factor/verify-totp"): ("verify_totp", self._verify_totp),
            ("POST", "/api/auth/sign-out"): ("sign_out", self._sign_out),
            ("GET", "/api/v1/app/me"): ("me", self._me),
            ("GET", "/api/v1/app/billing/subscriptions"): ("subscriptions", self._subscriptions),
            ("GET", "/api/v1/app/billing/wallet"): ("wallet", self._wallet),
            ("GET", "/api/v1/app/billing/plans"): ("plans", self._plans),
            ("GET", "/api/v1/app/billing/orders"): ("orders", self._orders),
        }
        if (method, path) in fixed:
            return fixed[method, path]
        if match := _SUBSCRIPTION.fullmatch(path):
            remote_id = int(match.group(1))
            if match.group(2) and method == "GET":
                return "devices", lambda r: self._devices(r, remote_id)
            if method == "GET":
                return "subscription", lambda r: self._subscription(r, remote_id)
            if method == "PATCH":
                return "auto_renew", lambda r: self._auto_renew(r, remote_id)
        if method == "GET" and (match := _CONFIG.fullmatch(path)):
            remote_id = int(match.group(1))
            return "config", lambda r: self._config(r, remote_id)
        if method == "GET" and (match := _LINK.fullmatch(path)):
            token = match.group(1)
            return "link", lambda r: self._link(token)
        return "unknown", lambda r: _error(404, "NOT_FOUND", "Not found")

    # -- public ------------------------------------------------------------------------------
    def _site(self, request: Recorded) -> _Answer:
        return _Answer(200, self.site, [("Vary", "Accept-Language")])

    def _providers(self, request: Recorded) -> _Answer:
        return _Answer(200, self.methods)

    def _captcha(self, request: Recorded) -> _Answer:
        enabled = bool(self.scenes)
        return _Answer(
            200,
            {
                "type": "turnstile" if enabled else None,
                "siteKey": "site-key" if enabled else None,
                "scenes": list(self.scenes),
                "recaptcha": None,
            },
        )

    # -- Better Auth -------------------------------------------------------------------------
    def _guard(self, request: Recorded, scene: str) -> None:
        """Better Auth's captcha plugin, then its origin check of cookie-bearing requests."""
        if scene in self.scenes and not request.header("x-captcha-response"):
            raise _RefusalError(_error(400, "MISSING_RESPONSE", "Missing CAPTCHA response"))
        if request.header("cookie") and request.header("origin") != self.base:
            raise _RefusalError(_error(403, "INVALID_ORIGIN", "Invalid origin"))

    def _sign_in_email(self, request: Recorded) -> _Answer:
        self._guard(request, "sign_in")
        body = request.json()
        email = str(body.get("email", ""))
        if "@" not in email:
            return _error(400, "INVALID_EMAIL", "Invalid email")
        user = self._find(lambda u: u.email.lower() == email.lower())
        return self._password(user, str(body.get("password", "")), "INVALID_EMAIL_OR_PASSWORD")

    def _sign_in_username(self, request: Recorded) -> _Answer:
        self._guard(request, "sign_in")
        body = request.json()
        name = str(body.get("username", "")).lower()
        user = self._find(lambda u: bool(u.username) and u.username.lower() == name)
        return self._password(user, str(body.get("password", "")), "INVALID_USERNAME_OR_PASSWORD")

    def _password(self, user: FakeUser | None, password: str, refusal: str) -> _Answer:
        if user is None or not hmac.compare_digest(user.password, password):
            return _error(401, refusal, "Invalid email or password")
        if self.require_email_verification and not user.email_verified:
            return _error(403, "EMAIL_NOT_VERIFIED", "Email not verified")
        if user.banned:  # api-edge's session hook: a FORBIDDEN without a code
            return _Answer(403, {"message": "This account is disabled"})
        if user.totp:
            cookie = f"2fa-{secrets.token_hex(10)}"
            with self._lock:
                self._challenges[cookie] = _Challenge(user.id, self.now + 600)
            return _Answer(
                200,
                {"twoFactorRedirect": True, "twoFactorMethods": ["totp"]},
                [
                    ("Set-Cookie", f"{SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"),
                    (
                        "Set-Cookie",
                        f"{TWO_FACTOR_COOKIE}={_signed(cookie)}; Max-Age=600; Path=/; HttpOnly; "
                        "SameSite=Lax",
                    ),
                ],
            )
        return self._session(user)

    def _session(self, user: FakeUser, *, with_user: bool = True) -> _Answer:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self.sessions[token] = user.id
        payload: dict[str, Any] = {"redirect": False, "token": token, "url": None}
        if with_user:
            payload["user"] = user.json()
        headers = [("Set-Cookie", f"{SESSION_COOKIE}={_signed(token)}; Path=/; HttpOnly")]
        if self.header_token:
            headers.append(("set-auth-token", _signed(token)))
            headers.append(("Access-Control-Expose-Headers", "set-auth-token"))
        return _Answer(200, payload, headers)

    def _send_code(self, request: Recorded) -> _Answer:
        self._guard(request, "send_code")
        body = request.json()
        email = str(body.get("email", "")).lower()
        if "@" not in email:
            return _error(400, "INVALID_EMAIL", "Invalid email")
        if body.get("type") != "sign-in":
            return _error(400, "VALIDATION_ERROR", "Invalid OTP type")
        with self._lock:
            self.codes[email] = f"{secrets.randbelow(10**6):06d}"
        return _Answer(200, {"success": True})

    def _sign_in_code(self, request: Recorded) -> _Answer:
        body = request.json()
        email = str(body.get("email", "")).lower()
        if "@" not in email:
            return _error(400, "INVALID_EMAIL", "Invalid email")
        with self._lock:
            expected = self.codes.get(email)
        if expected is None:
            return _error(400, "OTP_EXPIRED", "OTP expired")
        if not hmac.compare_digest(expected, str(body.get("otp", ""))):
            return _error(400, "INVALID_OTP", "Invalid OTP")
        with self._lock:
            del self.codes[email]
        user = self._find(lambda u: u.email.lower() == email)
        if user is None:
            return _error(400, "INVALID_OTP", "Invalid OTP")
        if user.banned:
            return _Answer(403, {"message": "This account is disabled"})
        return self._session(user)

    def _verify_totp(self, request: Recorded) -> _Answer:
        self._guard(request, "")
        cookies = _cookies(request.header("cookie") or "")
        signed = cookies.get(TWO_FACTOR_COOKIE, "")
        cookie, _, signature = signed.partition(".")
        with self._lock:
            challenge = self._challenges.get(cookie)
        if (
            challenge is None
            or not hmac.compare_digest(_signed(cookie), signed)
            or not signature
            or challenge.expires_at <= self.now
        ):
            return _error(401, "INVALID_TWO_FACTOR_COOKIE", "Invalid two factor cookie")
        user = self.users[challenge.user_id]
        if not hmac.compare_digest(user.totp, str(request.json().get("code", ""))):
            return _error(401, "INVALID_CODE", "Invalid code")
        with self._lock:
            del self._challenges[cookie]
        answer = self._session(user, with_user=self.user_after_two_factor)
        answer.headers.append(("Set-Cookie", f"{TWO_FACTOR_COOKIE}=; Max-Age=0; Path=/"))
        answer.payload.pop("redirect", None)
        answer.payload.pop("url", None)
        return answer

    def _sign_out(self, request: Recorded) -> _Answer:
        token = _raw(request.bearer)
        with self._lock:
            known = self.sessions.pop(token, None) if token else None
        if known is None:
            return _error(401, "UNAUTHORIZED", "Sign in required")
        return _Answer(200, {"success": True})

    # -- signed in ---------------------------------------------------------------------------
    def _user(self, request: Recorded) -> FakeUser:
        """api-edge's requireUser: a session of a user who is not banned, else 401."""
        token = _raw(request.bearer)
        with self._lock:
            user_id = self.sessions.get(token) if token else None
        user = self.users.get(user_id) if user_id else None
        if user is None or user.banned:
            raise _RefusalError(_error(401, "UNAUTHORIZED", "Sign in required"))
        return user

    def _me(self, request: Recorded) -> _Answer:
        user = self._user(request)
        payload = user.json()
        for name in ("username", "displayUsername", "twoFactorEnabled"):
            payload.pop(name, None)
        return _Answer(200, payload)

    def _view(self, subscription: dict[str, Any], *, mine: bool = True) -> dict[str, Any]:
        view = dict(subscription)
        view["user"] = None
        if self.access == "app":
            view["url"] = view["urls"] = None
        else:
            url = self.link(subscription["id"])
            view["url"] = url
            view["urls"] = [url] + (
                [self.link(subscription["id"], self.mirror)] if self.mirror else []
            )
        if mine:
            view["subscriptionAccess"] = self.access
        return view

    def _own(self, request: Recorded, remote_id: int) -> dict[str, Any]:
        user = self._user(request)
        with self._lock:
            subscription = self.subscriptions.get(remote_id)
        if subscription is None or subscription["userId"] != user.id:
            raise _RefusalError(_error(404, "NOT_FOUND", "Subscription not found"))
        return subscription

    def _subscriptions(self, request: Recorded) -> _Answer:
        user = self._user(request)
        with self._lock:
            mine = [s for s in self.subscriptions.values() if s["userId"] == user.id]
        mine.sort(key=lambda s: -s["id"])  # api-edge lists newest first
        return _Answer(200, [self._view(s) for s in mine])

    def _subscription(self, request: Recorded, remote_id: int) -> _Answer:
        return _Answer(200, self._view(self._own(request, remote_id)))

    def _auto_renew(self, request: Recorded, remote_id: int) -> _Answer:
        subscription = self._own(request, remote_id)
        auto_renew = request.json().get("autoRenew")
        if not isinstance(auto_renew, bool):
            return _error(400, "VALIDATION_ERROR", "autoRenew must be a boolean")
        with self._lock:
            subscription["autoRenew"] = auto_renew
            subscription["updatedAt"] = iso(self.now)
        return _Answer(200, self._view(subscription, mine=False))

    def _devices(self, request: Recorded, remote_id: int) -> _Answer:
        subscription = self._own(request, remote_id)
        return _Answer(
            200,
            {
                "limit": subscription["deviceLimit"],
                "items": [
                    {"ip": "203.0.113.7", "country": "CN", "lastSeenAt": iso(self.now)}
                    for _ in range(subscription["onlineDevices"])
                ],
            },
        )

    def _wallet(self, request: Recorded) -> _Answer:
        user = self._user(request)
        wallet = self.wallets.get(user.id) or {
            "userId": user.id,
            "currency": "CNY",
            "balance": 0,
            "points": 0,
            "commission": 0,
            "totalSpent": 0,
            "paidDays": 0,
            "level": None,
            "levelOverride": None,
            "nextLevel": None,
        }
        return _Answer(200, wallet)

    def _plans(self, request: Recorded) -> _Answer:
        # Public: a bad token is no error, the user just sees the public plans.
        return _Answer(200, {"currency": "CNY", "plans": self.plans})

    def _orders(self, request: Recorded) -> _Answer:
        user = self._user(request)
        query = {k: v[0] for k, v in parse_qs(request.query).items()}
        page, size = int(query.get("page", "1")), int(query.get("pageSize", "20"))
        if not (page >= 1 and 1 <= size <= 100):
            return _error(400, "VALIDATION_ERROR", "Invalid page")
        mine = [o for o in self.orders.get(user.id, []) if o["kind"] != "withdrawal"]
        start = (page - 1) * size
        return _Answer(200, {"items": mine[start : start + size], "total": len(mine)})

    # -- configs -----------------------------------------------------------------------------
    def _rendered(self, subscription: dict[str, Any]) -> _Answer:
        name = f"{self.site['name']}.yaml"
        return _Answer(
            200,
            self.config,
            [
                ("Content-Type", "text/yaml; charset=utf-8"),
                ("Profile-Update-Interval", "24"),
                (
                    "Subscription-Userinfo",
                    f"upload={subscription['upload']}; download={subscription['download']}; "
                    f"total={subscription['total'] or 0}; expire=1802592000",
                ),
                ("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}"),
                ("Cache-Control", "no-store"),
            ],
        )

    def _config(self, request: Recorded, remote_id: int) -> _Answer:
        user = self._user(request)
        if self.access == "link":
            return _error(403, "SUBSCRIPTION_APP_DISABLED", "Pulled through links for now")
        with self._lock:
            subscription = self.subscriptions.get(remote_id)
        if (
            subscription is None
            or subscription["userId"] != user.id
            or subscription["status"] == "disabled"
        ):
            return _error(404, "NOT_FOUND", "Subscription not found")
        return self._rendered(subscription)

    def _link(self, token: str) -> _Answer:
        if self.access == "app":
            return _error(403, "SUBSCRIPTION_LINK_DISABLED", "Subscription links are turned off")
        with self._lock:
            remote_id = self.link_tokens.get(token)
            subscription = self.subscriptions.get(remote_id) if remote_id else None
        if subscription is None or subscription["status"] == "disabled":
            return _error(404, "NOT_FOUND", "Subscription not found")
        return self._rendered(subscription)

    def _find(self, matches: Callable[[FakeUser], bool]) -> FakeUser | None:
        with self._lock:
            return next((u for u in self.users.values() if matches(u)), None)


class _RefusalError(Exception):
    def __init__(self, answer: _Answer) -> None:
        super().__init__(answer.status)
        self.answer = answer


def _error(status: int, code: str, message: str) -> _Answer:
    return _Answer(status, {"code": code, "message": message})


def _signed(value: str) -> str:
    """A cookie value signed as Better Auth signs them: ``value.signature``."""
    signature = hmac.new(_SECRET, value.encode(), hashlib.sha256).hexdigest()[:43]
    return f"{value}.{signature}"


def _raw(token: str | None) -> str | None:
    """The session token in a bearer value, which may be signed (set-auth-token) or not
    (the body's ``token``)."""
    if not token:
        return None
    raw, _, signature = token.partition(".")
    if signature and _signed(raw) != token:
        return None
    return raw


def _cookies(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator:
            cookies[name] = value
    return cookies
