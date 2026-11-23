"""Provider accounts (docs/accounts.md, D55-D58): AccountsService, as ProfileLibrary.accounts.
It signs in to providers, keeps their records and sessions, and turns each account's
subscriptions into profiles of kind ``provider`` that update through ``fetch``.

What it holds:

    providers         the ``providers`` rows (ProviderStore), mirrored here in their order
    sessions          each provider's token, in the credential store (ProviderTokens, D56) and
                      here once read; never in the database, a log or an error
    billing           what the last sync or fetch said of each subscription, stored without
                      links (``provider_subscriptions``) and mirrored here; a provider profile
                      without a row is one the last sync did not find
    this run only     each provider's ProviderState, Site, last sync and its error, the backoff
                      of ``sync_all``, a Retry-After, the watch after the user center was
                      opened; and each profile's held code: why its last update kept its config
                      (KEEPS_CONFIG), until a sync sees its subscription change

A signed-in call that answers 401 deletes the token and leaves the provider SIGN_IN_REQUIRED;
403 for the account, ACCOUNT_DISABLED. Either way the profiles keep their configs, the tunnel
keeps running, and their automatic updates wait (``paused``) until the provider is signed in
again. A credential store that cannot be read is CREDENTIALS_UNAVAILABLE, read again at the
next sync. Nothing removes a provider's profiles but ``sign_out``, apart from one the account
no longer has, which the user may remove (``removable``).

Locking follows core.py: changes to the profiles go through the library's lock, requests run
outside it. Everything runs on the asyncio thread, so the state here needs no lock of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from openweir.models.credentials import CredentialError, ProviderToken, ProviderTokens
from openweir.models.library.profiles import DEFAULT_AUTO_UPDATE_HOURS, Profiles, Updated
from openweir.models.profiles import Profile, ProfileError, ProfileList
from openweir.models.provider.api import origin as make_origin
from openweir.models.provider.store import ProviderStore
from openweir.models.provider.sync import (
    Reconciliation,
    blocked,
    checked,
    has_config,
    reconcile,
)
from openweir.models.provider.types import (
    ACCESS_LINK,
    CAPTCHA_SEND_CODE,
    CAPTCHA_SIGN_IN,
    KEEPS_CONFIG,
    METHOD_EMAIL_CODE,
    METHOD_EMAIL_PASSWORD,
    METHOD_USERNAME,
    PROVIDER_ACCOUNT_CONFLICT,
    PROVIDER_ACCOUNT_DISABLED,
    PROVIDER_APP_DISABLED,
    PROVIDER_BAD_RESPONSE,
    PROVIDER_CAPTCHA_REQUIRED,
    PROVIDER_LINK_UNAVAILABLE,
    PROVIDER_RATE_LIMITED,
    PROVIDER_SIGN_IN_FAILED,
    PROVIDER_SIGN_IN_REQUIRED,
    PROVIDER_SIGN_IN_UNSUPPORTED,
    PROVIDER_SUBSCRIPTION_MISSING,
    PROVIDER_UNKNOWN,
    PROVIDER_UNREACHABLE,
    TWO_FACTOR_TOTP,
    AccountSummary,
    OrderSummary,
    Probe,
    ProviderApi,
    ProviderError,
    ProviderRecord,
    ProviderRemoval,
    ProviderState,
    PublicPlan,
    RemoteSubscription,
    Session,
    SignedIn,
    SignInOptions,
    Site,
    TwoFactorChallenge,
    Wallet,
)
from openweir.models.routing import MODE_PLAN, MODE_PROFILE

if TYPE_CHECKING:
    from openweir.models.library.core import LibraryCore
    from openweir.models.subscription import Download

# sync_all: a provider synced this long ago is due again; the first failure waits the first
# backoff, later ones the second. A Retry-After waits at least as long as it asks.
SYNC_INTERVAL_S = 30 * 60.0
BACKOFF_S = (5 * 60.0, 30 * 60.0)
# After the user center was opened, sync this long, at most this often (D58).
WATCH_S = 10 * 60.0
WATCH_EVERY_S = 60.0
# A profile's interval until its first config says another (Profile-Update-Interval).
PROVIDER_UPDATE_HOURS = DEFAULT_AUTO_UPDATE_HOURS

# The states in which no request can be made for the account until it signs in again.
_SIGNED_OUT: dict[ProviderState | None, str] = {
    ProviderState.SIGN_IN_REQUIRED: PROVIDER_SIGN_IN_REQUIRED,
    ProviderState.ACCOUNT_DISABLED: PROVIDER_ACCOUNT_DISABLED,
}
# What a refused signed-in call leaves the provider in; its token goes.
_REFUSED = {
    PROVIDER_SIGN_IN_REQUIRED: ProviderState.SIGN_IN_REQUIRED,
    PROVIDER_ACCOUNT_DISABLED: ProviderState.ACCOUNT_DISABLED,
}
# Codes a profile is held for: KEEPS_CONFIG without the provider-wide ones, which the state is.
_HELD = KEEPS_CONFIG - set(_REFUSED)

_log = logging.getLogger(__name__)


def new_profile_id() -> str:
    return uuid.uuid4().hex


@dataclass
class _Run:
    """A provider in this run of the app."""

    state: ProviderState | None = None  # None until the token was read
    token: ProviderToken | None = field(default=None, repr=False)
    site: Site | None = None  # from the last probe or sync
    synced_at: float | None = None  # the last sync that succeeded
    tried_at: float | None = None  # the last sync started
    error: str = ""  # the code of the last sync's failure
    failures: int = 0  # in a row
    retry_at: float = 0.0  # the backoff after them
    limited_until: float = 0.0  # a Retry-After
    watch_until: float = 0.0  # the user center was opened
    syncing: bool = False


class Accounts:
    """AccountsService over HTTP (``api``), the credential store (``tokens``) and the library:
    see the module's docstring. ProfileLibrary makes it and hands it to its profiles."""

    def __init__(
        self,
        core: LibraryCore,
        profiles: Profiles,
        api: ProviderApi,
        tokens: ProviderTokens,
    ) -> None:
        self._core = core
        self._profiles = profiles
        self._api = api
        self._tokens = tokens
        self._store = ProviderStore(core.store.database)
        self._records: dict[str, ProviderRecord] = {}
        self._runs: dict[str, _Run] = {}
        self._billing: dict[str, dict[int, RemoteSubscription]] = {}  # provider -> remote id
        self._held: dict[str, str] = {}  # profile id -> code
        self._take(self._read())

    # -- the records ------------------------------------------------------------------------
    def _read(
        self,
    ) -> tuple[tuple[ProviderRecord, ...], dict[str, dict[int, RemoteSubscription]]]:
        records = self._store.providers()
        billing = {
            r.id: {s.subscription.id: s.subscription for s in self._store.subscriptions(r.id)}
            for r in records
        }
        return records, billing

    def _take(
        self, read: tuple[tuple[ProviderRecord, ...], dict[str, dict[int, RemoteSubscription]]]
    ) -> None:
        records, billing = read
        runs: dict[str, _Run] = {}
        for record in records:
            run = self._runs.get(record.id)
            known = self._records.get(record.id)
            # The same account keeps its session; another one starts over.
            same = run is not None and known is not None and known.user_id == record.user_id
            runs[record.id] = run if same and run is not None else _Run()
        self._records = {r.id: r for r in records}
        self._runs = runs
        self._billing = billing

    async def reload(self) -> None:
        """Take up what a restore wrote (Profiles.restore, under the library's lock): the
        providers and their billing as stored now. A provider that is the same account keeps
        its session; the profiles' held codes start over, as their configs are read again."""
        self._take(await asyncio.to_thread(self._read))
        self._held.clear()

    def _ordered(self) -> list[ProviderRecord]:
        return sorted(self._records.values(), key=lambda r: (r.position, r.created_at, r.id))

    def _record(self, provider_id: str) -> ProviderRecord:
        record = self._records.get(provider_id)
        if record is None:
            raise ProviderError(PROVIDER_UNKNOWN, f"no provider {provider_id}")
        return record

    def _by_origin(self, origin: str) -> ProviderRecord | None:
        return next((r for r in self._ordered() if r.origin == origin), None)

    def _own(self, provider_id: str) -> tuple[Profile, ...]:
        return tuple(
            p for p in self._core.list.profiles if p.is_provider and p.provider_id == provider_id
        )

    def _profile(self, provider_id: str, remote_id: int) -> Profile:
        for profile in self._own(provider_id):
            if profile.remote_id == remote_id:
                return profile
        raise ProviderError(PROVIDER_SUBSCRIPTION_MISSING, "no profile for that subscription")

    # -- what the profiles ask --------------------------------------------------------------
    def notice(self, profile: Profile) -> str:
        """Why the provider profile keeps its last config (AccountSummary.notices): its held
        code, or PROVIDER_SUBSCRIPTION_MISSING when the last sync did not find it; "" for
        neither. While the account itself needs signing in, only the state says so."""
        held = self._held.get(profile.id)
        if held:
            return held
        run = self._runs.get(profile.provider_id or "")
        if run is None:
            return PROVIDER_SUBSCRIPTION_MISSING  # signed out meanwhile
        if run.state in _SIGNED_OUT:
            return ""
        billing = self._billing.get(profile.provider_id or "", {})
        return PROVIDER_SUBSCRIPTION_MISSING if profile.remote_id not in billing else ""

    def removable(self, profile: Profile) -> bool:
        """The user may remove this provider profile on its own: the account no longer has
        it (docs/accounts.md §5.1). The others go only with ``sign_out``."""
        return self.notice(profile) == PROVIDER_SUBSCRIPTION_MISSING

    def paused(self, profile: Profile, now: float) -> bool:
        """Whether the provider profile's automatic update waits (Profiles._due): its account
        cannot be used now, its last update kept the config for a reason only a sync can
        clear, or billing says no config can be downloaded. It waits for a sync that sees its
        subscription change, which updates it then."""
        run = self._runs.get(profile.provider_id or "")
        if run is None or run.state not in (None, ProviderState.SIGNED_IN):
            return True
        if profile.id in self._held:
            return True
        subscription = self._billing.get(profile.provider_id or "", {}).get(profile.remote_id or 0)
        return subscription is None or bool(blocked(subscription, now))

    # -- sessions ---------------------------------------------------------------------------
    async def _token(self, record: ProviderRecord) -> ProviderToken:
        """The provider's session, read from the credential store the first time."""
        run = self._runs.get(record.id)
        if run is None:  # signed out while a call was on its way
            raise ProviderError(PROVIDER_UNKNOWN, f"no provider {record.id}")
        if run.token is not None:
            return run.token
        if (code := _SIGNED_OUT.get(run.state)) is not None:
            raise ProviderError(code, "the account needs signing in again")
        try:
            token = await asyncio.to_thread(self._tokens.load, record.id)
        except CredentialError:
            run.state = ProviderState.CREDENTIALS_UNAVAILABLE
            raise
        if token is None or token.user_id != record.user_id:
            run.state = ProviderState.SIGN_IN_REQUIRED
            raise ProviderError(PROVIDER_SIGN_IN_REQUIRED, "no session is stored")
        run.token, run.state = token, ProviderState.SIGNED_IN
        return token

    async def _known(self, record: ProviderRecord) -> ProviderState:
        """The provider's state, reading the credential store when nothing has yet."""
        run = self._runs.get(record.id)
        if run is None:
            return ProviderState.SIGN_IN_REQUIRED
        # A store that could not be read is tried again at the next sync, not on every look:
        # a locked keychain may ask the user each time.
        if run.state is None:
            with contextlib.suppress(ProfileError):  # the state says why
                await self._token(record)
        return run.state or ProviderState.CREDENTIALS_UNAVAILABLE

    async def _refused(
        self, record: ProviderRecord, token: ProviderToken, error: Exception
    ) -> None:
        """After a signed-in call failed: a 401 or a refused account ends the session here.
        A call made with a token signed in again since leaves the new one alone."""
        state = _REFUSED.get(getattr(error, "code", ""))
        run = self._runs.get(record.id)
        if state is None or run is None or run.token is not token:
            return
        run.token, run.state = None, state
        try:
            await asyncio.to_thread(self._tokens.delete, record.id)
        except CredentialError:
            _log.warning("could not delete a provider's refused session")

    async def _call[T](self, record: ProviderRecord, call: Callable[[str], Awaitable[T]]) -> T:
        """A signed-in call with the provider's token."""
        token = await self._token(record)
        try:
            return await call(token.token)
        except ProviderError as error:
            await self._refused(record, token, error)
            raise

    # -- summaries --------------------------------------------------------------------------
    async def summaries(self) -> tuple[AccountSummary, ...]:
        records = self._ordered()
        for record in records:
            await self._known(record)
        return tuple(self._summary(r) for r in records if r.id in self._records)

    async def summary(self, provider_id: str) -> AccountSummary:
        record = self._record(provider_id)
        await self._known(record)
        return self._summary(self._record(provider_id))

    def _summary(self, record: ProviderRecord) -> AccountSummary:
        run = self._runs[record.id]
        core = self._core
        current = core.list.active_profile()
        active = None
        if (
            current is not None
            and current.provider_id == record.id
            and core.state.mode == MODE_PROFILE
        ):
            active = current.remote_id
        notices = {}
        for profile in self._own(record.id):
            if (code := self.notice(profile)) and profile.remote_id is not None:
                notices[profile.remote_id] = code
        billing = self._billing.get(record.id, {})
        return AccountSummary(
            provider=record,
            state=run.state or ProviderState.CREDENTIALS_UNAVAILABLE,
            subscriptions=tuple(billing[i] for i in sorted(billing)),
            site=run.site,
            synced_at=run.synced_at,
            error=run.error,
            active=active,
            notices=notices,
        )

    # -- signing in -------------------------------------------------------------------------
    async def probe(self, address: str) -> Probe:
        origin = make_origin(address)
        site = await self._api.site(origin)
        options = await self._api.sign_in_options(origin)
        record = self._by_origin(origin)
        if record is not None:
            self._runs[record.id].site = site
        return Probe(origin, site, options, record)

    async def sign_in_password(
        self, probe: Probe, identifier: str, password: str
    ) -> Session | TwoFactorChallenge:
        options = probe.options
        if not options.password:
            raise ProviderError(
                _unavailable(options, CAPTCHA_SIGN_IN, METHOD_EMAIL_PASSWORD, METHOD_USERNAME),
                "the provider takes no password sign-in from the app",
            )
        identifier = identifier.strip()
        if "@" not in identifier and not options.username:
            # The provider has no user names: it could only answer that the email is wrong.
            raise ProviderError(PROVIDER_SIGN_IN_FAILED, "an email is required")
        return await self._api.sign_in_password(probe.origin, identifier, password)

    async def send_email_code(self, probe: Probe, email: str) -> None:
        self._check_email_code(probe.options)
        await self._api.send_email_code(probe.origin, email)

    async def sign_in_email_code(
        self, probe: Probe, email: str, code: str
    ) -> Session | TwoFactorChallenge:
        self._check_email_code(probe.options)
        return await self._api.sign_in_email_code(probe.origin, email, code)

    @staticmethod
    def _check_email_code(options: SignInOptions) -> None:
        if not options.email_code:
            raise ProviderError(
                _unavailable(options, CAPTCHA_SEND_CODE, METHOD_EMAIL_CODE),
                "the provider takes no email code sign-in from the app",
            )

    async def verify_two_factor(self, challenge: TwoFactorChallenge, code: str) -> Session:
        if TWO_FACTOR_TOTP not in challenge.methods:
            # v1 verifies authenticator codes only (D56).
            raise ProviderError(PROVIDER_SIGN_IN_UNSUPPORTED, "no authenticator two-factor")
        return await self._api.verify_totp(challenge, code)

    async def complete(self, probe: Probe, session: Session, *, replace: bool = False) -> SignedIn:
        if session.origin != probe.origin:
            raise ValueError("the session is another provider's")
        # Billing first, with the new session: nothing is stored until it answers well.
        subscriptions = checked(
            await self._api.subscriptions(probe.origin, session.token), session.user_id
        )
        existing = self._by_origin(probe.origin)
        if existing is not None and existing.user_id != session.user_id:
            if not replace:
                # The dialog asks whether to replace that account, which it names.
                raise ProviderError(PROVIDER_ACCOUNT_CONFLICT, existing.account)
            await self.sign_out(existing.id)
            existing = None
        account = session.account.display
        if existing is None:
            record = await asyncio.to_thread(
                self._store.add, probe.origin, probe.site.name, account, session.user_id
            )
            self._billing[record.id] = {}
        else:
            # Signing in again, or to a provider restored from a backup: its profiles relink
            # by remote id.
            record = await asyncio.to_thread(
                self._store.update,
                _with(existing, name=probe.site.name, account=account),
            )
        token = ProviderToken(session.token, session.user_id)
        self._records[record.id] = record
        run = self._runs.setdefault(record.id, _Run())
        now = self._core.clock()
        run.token, run.state, run.site = token, ProviderState.SIGNED_IN, probe.site
        run.tried_at, run.error, run.failures, run.retry_at = now, "", 0, 0.0
        try:
            await asyncio.to_thread(self._tokens.save, record.id, token)
        except CredentialError:
            # It lasts until the app quits; after that the provider needs signing in again.
            _log.warning("could not store a provider's session")
        await self._apply(record, subscriptions)
        run.synced_at = now
        return SignedIn(self._records[record.id], subscriptions, self._core.list)

    # -- using a subscription ---------------------------------------------------------------
    async def use(self, provider_id: str, remote_id: int) -> ProfileList:
        self._record(provider_id)
        profile = self._profile(provider_id, remote_id)
        subscription = self._billing.get(provider_id, {}).get(remote_id)
        code = (
            blocked(subscription, self._core.clock())
            if subscription is not None
            else PROVIDER_SUBSCRIPTION_MISSING
        )
        if code:
            raise ProviderError(code, "billing says it cannot be used")
        if not has_config(profile) or profile.id in self._held:
            await self._profiles.update(profile.id)
        return await self._profiles.activate(profile.id)

    # -- syncing ----------------------------------------------------------------------------
    async def sync(self, provider_id: str) -> Updated:
        record = self._record(provider_id)
        run = self._runs[provider_id]
        now = self._core.clock()
        if now < run.limited_until:
            raise ProviderError(
                PROVIDER_RATE_LIMITED,
                "waiting as the provider asked",
                retry_after=run.limited_until - now,
            )
        return await self._sync(record)

    async def sync_all(self, *, max_age: float = SYNC_INTERVAL_S) -> Updated:
        now = self._core.clock()
        changed = applied = False
        for record in self._ordered():
            run = self._runs.get(record.id)
            if run is None or not _due(run, now, max_age):
                continue
            try:
                result = await self._sync(record)
            except Exception:  # recorded by _sync
                continue
            changed |= result.changed
            applied |= result.applied
        return Updated(self._core.list, changed, applied)

    async def watch(self, provider_id: str) -> None:
        self._record(provider_id)
        self._runs[provider_id].watch_until = self._core.clock() + WATCH_S

    async def _sync(self, record: ProviderRecord) -> Updated:
        """Read the account's subscriptions, bring the profiles in line, then update the
        configs a change calls for (docs/accounts.md §5.2). Records the outcome."""
        run = self._runs[record.id]
        run.tried_at = self._core.clock()
        run.syncing = True
        try:
            subscriptions = await self._call(
                record, lambda token: self._api.subscriptions(record.origin, token)
            )
            subscriptions = checked(subscriptions, record.user_id)
            site = await self._site(record)
            refresh = (await self._apply(record, subscriptions, site)).refresh
        except Exception as error:
            self._failed(record.id, error)
            raise
        finally:
            run.syncing = False
        run.synced_at, run.error, run.failures, run.retry_at = self._core.clock(), "", 0, 0.0
        changed = applied = False
        for profile_id in refresh:
            try:
                result = await self._profiles.update(profile_id)
            except Exception:  # Profiles.update marked it, or it is held
                continue
            changed |= result.changed
            applied |= result.applied
        return Updated(self._core.list, changed, applied)

    async def _site(self, record: ProviderRecord) -> Site | None:
        """The provider's site now, in the UI's language; None when it cannot be read, which
        is no reason to fail a sync."""
        try:
            site = await self._api.site(record.origin)
        except ProviderError:
            return None
        if (run := self._runs.get(record.id)) is not None:
            run.site = site
        return site

    def _failed(self, provider_id: str, error: Exception) -> None:
        run = self._runs.get(provider_id)
        if run is None:
            return
        now = self._core.clock()
        run.error = getattr(error, "code", "") or type(error).__name__
        run.failures += 1
        run.retry_at = now + BACKOFF_S[min(run.failures, len(BACKOFF_S)) - 1]
        retry_after = getattr(error, "retry_after", None)
        if run.error == PROVIDER_RATE_LIMITED and retry_after is not None:
            run.limited_until = now + retry_after
            run.retry_at = max(run.retry_at, run.limited_until)
        # The code only: messages are scrubbed already, but a log needs no more.
        _log.info("syncing a provider failed: %s", run.error)

    async def _apply(
        self,
        record: ProviderRecord,
        subscriptions: tuple[RemoteSubscription, ...],
        site: Site | None = None,
    ) -> Reconciliation:
        """Bring the profiles in line with ``subscriptions`` under the lock: the profiles are
        saved before what billing says of them, which is stored for saved profiles only."""
        core = self._core
        async with core.lock:
            record = self._record(record.id)  # signed out meanwhile: PROVIDER_UNKNOWN
            result = reconcile(
                core.list,
                record.id,
                subscriptions,
                self._billing.get(record.id, {}),
                core.clock(),
                new_id=new_profile_id,
                auto_update_hours=PROVIDER_UPDATE_HOURS,
            )
            # Names are the pool's group names in a plan, so a rename recomposes it.
            recompose = core.state.mode == MODE_PLAN and any(
                core.list.get(i).in_pool for i in result.renamed
            )
            report = core.report
            if recompose:
                report = await core.validate_plan(core.current_plan(), result.profiles)
            if result.profiles.profiles != core.list.profiles:
                await core.save(result.profiles)
            core.report = report
            await asyncio.to_thread(self._store.save_subscriptions, result.stored)
            if result.missing:
                await asyncio.to_thread(self._store.forget_subscriptions, result.missing)
            self._billing[record.id] = {s.id: _unlinked(s) for s in subscriptions}
            for profile_id in result.changed:
                self._held.pop(profile_id, None)
            if site is not None and site.name and site.name != record.name:
                record = await asyncio.to_thread(self._store.update, _with(record, name=site.name))
                self._records[record.id] = record
            if recompose:
                await core.reload_if_running()
        return result

    # -- signing out ------------------------------------------------------------------------
    async def sign_out_removal(self, provider_id: str) -> ProviderRemoval:
        record = self._record(provider_id)
        core = self._core
        await core.inspect_missing()
        own = self._own(provider_id)
        ids = {p.id for p in own}
        active = core.list.active in ids and core.state.mode == MODE_PROFILE
        remaining = replace(
            core.list, profiles=tuple(p for p in core.list.profiles if p.id not in ids)
        )
        successor = core.first_with_rules(remaining) if active else None
        return ProviderRemoval(record, own, active, successor)

    async def sign_out(self, provider_id: str) -> ProfileList:
        record = self._record(provider_id)
        run = self._runs[provider_id]
        token = run.token
        if token is None:
            try:
                token = await asyncio.to_thread(self._tokens.load, provider_id)
            except CredentialError:
                token = None
        if token is not None:
            try:
                await self._api.sign_out(record.origin, token.token)
            except ProviderError as error:
                # Offline, or the session is over already: it ends by itself in 7 days.
                _log.info("the provider did not end the session: %s", error.code)
        run.token, run.state = None, ProviderState.SIGN_IN_REQUIRED
        try:
            await asyncio.to_thread(self._tokens.delete, provider_id)
        except CredentialError:
            _log.warning("could not delete a provider's session")
        core = self._core
        async with core.lock:
            own = {p.id for p in self._own(provider_id)}
            await self._profiles.discard(own)
            await asyncio.to_thread(self._store.delete, provider_id)
            self._records.pop(provider_id, None)
            self._runs.pop(provider_id, None)
            self._billing.pop(provider_id, None)
            for profile_id in own:
                self._held.pop(profile_id, None)
            return core.list

    # -- configs ----------------------------------------------------------------------------
    async def fetch(self, profile: Profile) -> Download:
        if not profile.is_provider or profile.provider_id is None or profile.remote_id is None:
            raise ValueError("not a provider profile")
        record = self._record(profile.provider_id)
        try:
            download = await self._fetch(record, profile.id, profile.remote_id)
        except ProviderError as error:
            if error.code in _HELD:
                self._held[profile.id] = error.code
            raise
        self._held.pop(profile.id, None)
        return download

    async def _fetch(self, record: ProviderRecord, profile_id: str, remote_id: int) -> Download:
        """Billing first, every time (D57): access and status may have changed since."""
        subscription = await self._call(
            record, lambda token: self._api.subscription(record.origin, token, remote_id)
        )
        if subscription.user_id != record.user_id:
            raise ProviderError(PROVIDER_BAD_RESPONSE, "a subscription of another user")
        await self._remember(record.id, profile_id, subscription)
        if code := blocked(subscription, self._core.clock()):
            raise ProviderError(code, "billing says it cannot be used")
        if subscription.access != ACCESS_LINK:
            try:
                return await self._call(
                    record, lambda token: self._api.config(record.origin, token, remote_id)
                )
            except ProviderError as error:
                if error.code != PROVIDER_APP_DISABLED:
                    raise
        # The links live as long as this call: never stored, never in an error.
        links = list(dict.fromkeys(u for u in (subscription.url, *subscription.urls) if u))
        if not links:
            raise ProviderError(PROVIDER_LINK_UNAVAILABLE, "billing gives no link")
        failure: ProviderError | None = None
        for link in links:
            try:
                return await self._api.link(link)
            except ProviderError as error:
                if error.code != PROVIDER_UNREACHABLE:
                    raise
                failure = error
        assert failure is not None
        raise failure

    async def _remember(
        self, provider_id: str, profile_id: str, subscription: RemoteSubscription
    ) -> None:
        """Store what billing just said of one subscription, as a sync would."""
        billing = self._billing.get(provider_id)
        if billing is None:
            return  # signed out meanwhile
        billing[subscription.id] = _unlinked(subscription)
        await asyncio.to_thread(self._store.save_subscriptions, {profile_id: subscription})

    # -- billing ----------------------------------------------------------------------------
    async def wallet(self, provider_id: str) -> Wallet:
        record = self._record(provider_id)
        wallet = await self._call(record, lambda token: self._api.wallet(record.origin, token))
        if wallet.user_id and wallet.user_id != record.user_id:
            raise ProviderError(PROVIDER_BAD_RESPONSE, "the wallet of another user")
        return wallet

    async def plans(self, provider_id: str) -> tuple[PublicPlan, ...]:
        record = self._record(provider_id)
        try:
            token = await self._token(record)
        except ProfileError:
            # Plans are public: without a session they are the plans everyone sees.
            return await self._api.plans(record.origin, None)
        try:
            return await self._api.plans(record.origin, token.token)
        except ProviderError as error:
            await self._refused(record, token, error)
            raise

    async def orders(self, provider_id: str, limit: int = 5) -> tuple[OrderSummary, ...]:
        record = self._record(provider_id)
        return await self._call(record, lambda token: self._api.orders(record.origin, token, limit))

    async def set_auto_renew(
        self, provider_id: str, remote_id: int, enabled: bool
    ) -> RemoteSubscription:
        record = self._record(provider_id)
        answer = await self._call(
            record,
            lambda token: self._api.set_auto_renew(record.origin, token, remote_id, enabled),
        )
        if answer.user_id and answer.user_id != record.user_id:
            raise ProviderError(PROVIDER_BAD_RESPONSE, "a subscription of another user")
        stored = self._billing.get(provider_id, {}).get(remote_id)
        # The answer has no subscriptionAccess: the stored one stays.
        updated = replace(_unlinked(answer), access=stored.access if stored else answer.access)
        profile = next((p for p in self._own(provider_id) if p.remote_id == remote_id), None)
        if profile is not None:
            await self._remember(provider_id, profile.id, updated)
        return updated


def _due(run: _Run, now: float, max_age: float) -> bool:
    """Whether sync_all syncs a provider now (docs/accounts.md §5.3)."""
    if run.syncing or run.state in _SIGNED_OUT or now < run.limited_until:
        return False
    if run.tried_at is None:
        return True
    if now < run.watch_until and now - run.tried_at >= WATCH_EVERY_S:
        return True
    if run.failures:
        return now >= run.retry_at
    return now - run.tried_at >= max_age


def _unavailable(options: SignInOptions, scene: str, *methods: str) -> str:
    """Why a sign-in method cannot be used in the app: a captcha guards it, or the provider
    does not offer it."""
    if scene in options.captcha and set(methods).intersection(options.methods):
        return PROVIDER_CAPTCHA_REQUIRED
    return PROVIDER_SIGN_IN_UNSUPPORTED


def _unlinked(subscription: RemoteSubscription) -> RemoteSubscription:
    """Without its links, which outlive no call."""
    return replace(subscription, url=None, urls=())


def _with(record: ProviderRecord, **changes: str) -> ProviderRecord:
    return replace(record, **changes)
