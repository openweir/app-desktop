"""Provider accounts in the profile library (models/library/accounts.py, docs/accounts.md §2 to
§6) against a stand-in for api-edge on 127.0.0.1: signing in and again, syncing billing into
profiles, fetching their configs by what billing says, what a refused session or a rate limit
does, signing out, and that sessions and links reach neither the database nor a log."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest

from openweir.models.database import DATABASE_NAME
from openweir.models.library import Accounts, ProfileLibrary
from openweir.models.library.accounts import (
    BACKOFF_S,
    SYNC_INTERVAL_S,
    WATCH_S,
    provider_profile_id,
)
from openweir.models.library.profiles import ACCOUNT_PROFILE
from openweir.models.profiles import KIND_PROVIDER, Profile, ProfileError, ProfileList
from openweir.models.provider import Session, SignedIn, TwoFactorChallenge, types
from openweir.models.provider.api import HttpProviderApi
from openweir.models.provider.store import ProviderStore
from openweir.models.provider.types import ProviderError, ProviderState
from openweir.models.routing import MODE_PROFILE
from openweir.models.store import ProfileStore
from tests.support.credentials import MemoryProviderTokens
from tests.support.provider_server import (
    DAY,
    EMAIL,
    GIB,
    NOW,
    PASSWORD,
    TOTP,
    USERNAME,
    FakeProvider,
    iso,
)
from tests.support.simulated import SAMPLE_CONFIG, SimulatedFetcher, SimulatedHost, seed_profiles


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def failure(call: Callable[[], Coroutine[Any, Any, object]]) -> ProfileError:
    with pytest.raises(ProfileError) as caught:
        run(call)
    return caught.value


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    # Reach the loopback server directly even if the machine has a proxy configured.
    monkeypatch.setenv("no_proxy", "*")
    fake = FakeProvider()
    fake.config = SAMPLE_CONFIG.encode()  # rules of its own, so it can be used
    yield fake
    fake.close()


class Scene:
    def __init__(self, directory: Path, provider: FakeProvider, *, seeded: bool = False) -> None:
        self.now = NOW
        self.directory = directory
        self.provider = provider
        self.store = ProfileStore(directory)
        if seeded:
            seed_profiles(self.store, NOW)
        self.host = SimulatedHost(clock=lambda: self.now)
        self.tokens = MemoryProviderTokens()
        self.api = HttpProviderApi(language=lambda: "zh", clock=lambda: self.now)
        self.library = self.open()

    def open(self) -> ProfileLibrary:
        """The library as a new run of the app finds it."""
        return ProfileLibrary(
            self.store,
            self.host,
            SimulatedFetcher(),
            clock=lambda: self.now,
            provider_api=self.api,
            provider_tokens=self.tokens,
        )

    @property
    def accounts(self) -> Accounts:
        return self.library.accounts

    async def signing_in(self, identifier: str = EMAIL, *, replace: bool = False) -> SignedIn:
        probe = await self.accounts.probe(self.provider.base)
        session = await self.accounts.sign_in_password(probe, identifier, PASSWORD)
        assert isinstance(session, Session)
        return await self.accounts.complete(probe, session, replace=replace)

    def sign_in(self, identifier: str = EMAIL) -> SignedIn:
        return run(lambda: self.signing_in(identifier))

    def own(self, remote_id: int) -> Profile:
        return next(p for p in self.library.snapshot().profiles if p.remote_id == remote_id)

    def provider_profiles(self) -> list[Profile]:
        return [p for p in self.library.snapshot().profiles if p.is_provider]

    def token(self, provider_id: str) -> str | None:
        stored = self.tokens.credentials.secrets.get(f"provider:{provider_id}")
        return None if stored is None else json.loads(stored)["token"]

    def requests(self, route: str) -> int:
        return len(self.provider.requests_for(route))


@pytest.fixture
def scene(tmp_path: Path, provider: FakeProvider) -> Scene:
    return Scene(tmp_path, provider)


# -- signing in -----------------------------------------------------------------------------------
def test_probing_reads_the_site_and_how_to_sign_in(scene: Scene) -> None:
    probe = run(lambda: scene.accounts.probe(f" {scene.provider.base}/login?next=1 "))
    assert probe.origin == scene.provider.base
    assert probe.site.name == "示例服务商"
    assert (probe.options.password, probe.options.email_code, probe.options.username) == (
        True,
        True,
        True,
    )
    assert probe.provider is None
    # Nothing was signed in to.
    assert scene.provider.requests_for("sign_in_email") == []

    signed = scene.sign_in()
    assert run(lambda: scene.accounts.probe(scene.provider.base)).provider == signed.provider


@pytest.mark.parametrize(
    ("identifier", "route"), [(EMAIL, "sign_in_email"), (USERNAME, "sign_in_username")]
)
def test_signing_in_brings_the_subscriptions_in_as_profiles(
    scene: Scene, identifier: str, route: str
) -> None:
    signed = scene.sign_in(identifier)

    record = signed.provider
    assert scene.requests(route) == 1
    assert (record.origin, record.name, record.account, record.user_id) == (
        scene.provider.base,
        "示例服务商",
        EMAIL,
        "user-1",
    )
    assert [s.id for s in signed.subscriptions] == [7, 9]
    assert [(p.name, p.provider_id, p.remote_id, p.url) for p in scene.provider_profiles()] == [
        ("专业版", record.id, 7, ""),
        ("基础版", record.id, 9, ""),
    ]
    # Every device that shares the provider's record names them alike.
    assert [p.id for p in scene.provider_profiles()] == [
        provider_profile_id(record.id, 7),
        provider_profile_id(record.id, 9),
    ]
    # Billing only: the dialog's use and the sync after it download configs.
    assert scene.requests("config") == scene.requests("link") == 0
    assert all(p.updated_at == 0 for p in scene.provider_profiles())
    assert scene.own(7).usage is not None
    assert scene.token(record.id)
    assert ProviderStore(scene.store.database).providers() == (record,)

    summary = run(lambda: scene.accounts.summary(record.id))
    assert summary.state == ProviderState.SIGNED_IN
    assert [s.id for s in summary.subscriptions] == [7, 9]
    assert all(s.url is None and s.urls == () for s in summary.subscriptions)
    assert summary.site is not None
    assert summary.site.name == "示例服务商"
    assert summary.notices == {}

    # A new run of the app reads the stored session and billing.
    again = scene.open()
    (later,) = run(again.accounts.summaries)
    assert later.state == ProviderState.SIGNED_IN
    assert [s.id for s in later.subscriptions] == [7, 9]
    assert later.synced_at is None


def test_signing_in_with_an_email_code(scene: Scene) -> None:
    async def scenario() -> SignedIn:
        probe = await scene.accounts.probe(scene.provider.base)
        await scene.accounts.send_email_code(probe, EMAIL)
        session = await scene.accounts.sign_in_email_code(probe, EMAIL, scene.provider.codes[EMAIL])
        assert isinstance(session, Session)
        return await scene.accounts.complete(probe, session)

    assert run(scenario).provider.user_id == "user-1"


def test_signing_in_with_two_factor(scene: Scene) -> None:
    scene.provider.users["user-1"].totp = TOTP

    async def challenge() -> tuple[Any, TwoFactorChallenge]:
        probe = await scene.accounts.probe(scene.provider.base)
        step = await scene.accounts.sign_in_password(probe, EMAIL, PASSWORD)
        assert isinstance(step, TwoFactorChallenge)
        return probe, step

    probe, step = run(challenge)
    assert step.methods == (types.TWO_FACTOR_TOTP,)
    wrong = failure(lambda: scene.accounts.verify_two_factor(step, "000000"))
    assert wrong.code == types.PROVIDER_TWO_FACTOR_INVALID
    session = run(lambda: scene.accounts.verify_two_factor(step, TOTP))
    signed = run(lambda: scene.accounts.complete(probe, session))
    assert signed.provider.account == EMAIL
    assert len(scene.provider_profiles()) == 2


def test_methods_the_app_cannot_use_are_refused_before_any_request(scene: Scene) -> None:
    scene.provider.scenes = [types.CAPTCHA_SIGN_IN]
    probe = run(lambda: scene.accounts.probe(scene.provider.base))
    refused = failure(lambda: scene.accounts.sign_in_password(probe, EMAIL, PASSWORD))
    assert refused.code == types.PROVIDER_CAPTCHA_REQUIRED
    run(lambda: scene.accounts.send_email_code(probe, EMAIL))  # codes are not guarded

    scene.provider.scenes = [types.CAPTCHA_SIGN_IN, types.CAPTCHA_SEND_CODE]
    probe = run(lambda: scene.accounts.probe(scene.provider.base))
    refused = failure(lambda: scene.accounts.send_email_code(probe, EMAIL))
    assert refused.code == types.PROVIDER_CAPTCHA_REQUIRED

    scene.provider.scenes = []
    scene.provider.methods = [{"id": "pk", "type": "passkey", "name": "通行密钥"}]
    probe = run(lambda: scene.accounts.probe(scene.provider.base))
    refused = failure(lambda: scene.accounts.sign_in_password(probe, EMAIL, PASSWORD))
    assert refused.code == types.PROVIDER_SIGN_IN_UNSUPPORTED

    # A user name where the provider has none.
    scene.provider.methods = [{"id": "email", "type": "email_password", "name": "邮箱"}]
    probe = run(lambda: scene.accounts.probe(scene.provider.base))
    refused = failure(lambda: scene.accounts.sign_in_password(probe, USERNAME, PASSWORD))
    assert refused.code == types.PROVIDER_SIGN_IN_FAILED
    assert scene.requests("sign_in_email") == scene.requests("sign_in_username") == 0
    assert scene.requests("send_code") == 1


def test_a_sign_in_whose_billing_fails_stores_nothing(scene: Scene) -> None:
    scene.provider.fail("subscriptions", 502)
    error = failure(scene.signing_in)
    assert error.code == types.PROVIDER_UNREACHABLE
    assert run(scene.accounts.summaries) == ()
    assert scene.tokens.credentials.secrets == {}
    assert ProviderStore(scene.store.database).providers() == ()
    assert scene.provider_profiles() == []


def test_signing_in_again_as_the_same_user_keeps_the_record_and_profiles(scene: Scene) -> None:
    first = scene.sign_in()
    pid = first.provider.id
    run(lambda: scene.accounts.sync(pid))
    before = [p.id for p in scene.provider_profiles()]
    old_token = scene.token(pid)

    scene.provider.revoke()
    assert failure(lambda: scene.accounts.sync(pid)).code == types.PROVIDER_SIGN_IN_REQUIRED
    assert scene.token(pid) is None
    assert run(lambda: scene.accounts.summary(pid)).state == ProviderState.SIGN_IN_REQUIRED

    second = scene.sign_in()
    assert second.provider.id == pid
    assert [p.id for p in scene.provider_profiles()] == before
    assert scene.token(pid) not in (None, old_token)
    summary = run(lambda: scene.accounts.summary(pid))
    assert (summary.state, summary.error) == (ProviderState.SIGNED_IN, "")


def test_another_user_of_the_same_provider_replaces_the_account_when_asked(
    scene: Scene,
) -> None:
    first = scene.sign_in()
    run(lambda: scene.accounts.sync(first.provider.id))

    conflict = failure(lambda: scene.signing_in("other@example.com"))
    assert conflict.code == types.PROVIDER_ACCOUNT_CONFLICT
    assert conflict.message == EMAIL  # the dialog names the account it would replace
    assert [p.remote_id for p in scene.provider_profiles()] == [7, 9]
    assert scene.token(first.provider.id)

    replaced = run(lambda: scene.signing_in("other@example.com", replace=True))
    assert replaced.provider.id != first.provider.id
    assert replaced.provider.user_id == "user-2"
    assert [p.remote_id for p in scene.provider_profiles()] == [11]
    assert scene.token(first.provider.id) is None
    assert ProviderStore(scene.store.database).providers() == (replaced.provider,)
    assert scene.provider.requests_for("sign_out")


# -- syncing --------------------------------------------------------------------------------------
def test_a_sync_downloads_usable_subscriptions_without_a_config(scene: Scene) -> None:
    pid = scene.sign_in().provider.id

    update = run(lambda: scene.accounts.sync(pid))

    assert update.changed
    seven, nine = scene.own(7), scene.own(9)
    assert seven.updated_at == NOW
    assert scene.store.read_config(seven.id) == SAMPLE_CONFIG
    # The first config's Profile-Update-Interval sets the interval.
    assert seven.auto_update_hours == 24
    assert nine.updated_at == 0  # expired: nothing to download
    assert scene.requests("config") == 1
    summary = run(lambda: scene.accounts.summary(pid))
    assert (summary.synced_at, summary.error) == (NOW, "")

    # Nothing changed: nothing is downloaded.
    run(lambda: scene.accounts.sync(pid))
    assert scene.requests("config") == 1
    # The subscription changed (renewed, another plan): its config may have.
    scene.provider.update_subscription(7, updatedAt=iso(NOW))
    run(lambda: scene.accounts.sync(pid))
    assert scene.requests("config") == 2


def test_a_sync_adds_renames_and_marks_missing(scene: Scene) -> None:
    pid = scene.sign_in().provider.id
    run(lambda: scene.accounts.sync(pid))
    local = run(lambda: scene.library.profiles.import_url("https://sub.example.com/x", "本地"))
    assert local.profiles[-1].name == "本地"

    scene.provider.add_subscription("user-1", 12, planName="专业版")
    del scene.provider.subscriptions[9]
    run(lambda: scene.accounts.sync(pid))

    names = [(p.remote_id, p.name) for p in scene.library.snapshot().profiles]
    # The new one goes after the provider's others, before the link imported later.
    assert names == [(7, "专业版 · #7"), (9, "基础版"), (12, "专业版 · #12"), (None, "本地")]
    assert scene.own(12).updated_at == NOW  # usable and new: downloaded
    summary = run(lambda: scene.accounts.summary(pid))
    assert [s.id for s in summary.subscriptions] == [7, 12]
    assert summary.notices == {9: types.PROVIDER_SUBSCRIPTION_MISSING}

    # Only one the account no longer has can be removed on its own.
    refused = failure(lambda: scene.library.profiles.remove(scene.own(7).id))
    assert refused.code == ACCOUNT_PROFILE
    run(lambda: scene.library.profiles.remove(scene.own(9).id))
    assert [p.remote_id for p in scene.provider_profiles()] == [7, 12]


def test_the_language_follows_into_names(scene: Scene) -> None:
    pid = scene.sign_in().provider.id
    scene.provider.update_subscription(7, planName="Pro")
    # A language switch syncs every provider whatever its last sync.
    run(lambda: scene.accounts.sync_all(max_age=0))
    assert scene.own(7).name == "Pro"
    assert run(lambda: scene.accounts.summary(pid)).subscriptions[0].plan_name == "Pro"


@pytest.mark.parametrize(
    ("access", "route"), [("both", "config"), ("app", "config"), ("link", "link")]
)
def test_a_config_comes_the_way_billing_says(scene: Scene, access: str, route: str) -> None:
    scene.provider.access = access
    pid = scene.sign_in().provider.id

    run(lambda: scene.accounts.sync(pid))

    assert scene.store.read_config(scene.own(7).id) == SAMPLE_CONFIG
    assert scene.requests(route) == 1
    assert scene.requests("config" if route == "link" else "link") == 0
    # Billing is read first each time (D57).
    assert scene.requests("subscription") == 1
    for request in scene.provider.requests_for("link"):
        assert request.bearer is None
        assert request.header("cookie") is None


def test_the_link_serves_when_the_app_may_not(scene: Scene) -> None:
    pid = scene.sign_in().provider.id
    scene.provider.fail("config", 403, "SUBSCRIPTION_APP_DISABLED")
    run(lambda: scene.accounts.sync(pid))
    assert scene.requests("config") == scene.requests("link") == 1
    assert scene.store.read_config(scene.own(7).id) == SAMPLE_CONFIG


@pytest.fixture
def mirror(provider: FakeProvider) -> Iterator[FakeProvider]:
    """A second subscription domain serving the provider's links."""
    fake = FakeProvider(seed=False)
    fake.subscriptions, fake.link_tokens = provider.subscriptions, provider.link_tokens
    fake.config = provider.config
    provider.mirror = fake.base
    yield fake
    fake.close()


def test_a_link_that_cannot_be_reached_falls_back_to_its_mirrors(
    scene: Scene, mirror: FakeProvider
) -> None:
    scene.provider.access = "link"
    pid = scene.sign_in().provider.id
    scene.provider.fail("link", 502)

    run(lambda: scene.accounts.sync(pid))

    assert scene.requests("link") == 1
    (served,) = mirror.requests_for("link")
    assert served.bearer is None
    assert scene.store.read_config(scene.own(7).id) == SAMPLE_CONFIG


def no_link(provider: FakeProvider) -> None:
    """The app may not have the config, and the link is turned off too."""
    provider.fail("config", 403, "SUBSCRIPTION_APP_DISABLED", times=9)
    provider.fail("link", 403, "SUBSCRIPTION_LINK_DISABLED", times=9)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            lambda p: p.update_subscription(
                7, status="expired", usable=False, expireAt=iso(NOW - DAY)
            ),
            types.PROVIDER_SUBSCRIPTION_EXPIRED,
        ),
        (
            lambda p: p.update_subscription(7, status="disabled", usable=False),
            types.PROVIDER_SUBSCRIPTION_DISABLED,
        ),
        (
            lambda p: p.update_subscription(7, usable=False, download=100 * GIB),
            types.PROVIDER_SUBSCRIPTION_UNUSABLE,
        ),
        (lambda p: p.subscriptions.pop(7), types.PROVIDER_SUBSCRIPTION_MISSING),
        (no_link, types.PROVIDER_LINK_UNAVAILABLE),
    ],
    ids=["expired", "disabled", "unusable", "missing", "no link"],
)
def test_billing_that_refuses_a_download_keeps_the_last_config(
    scene: Scene, change: Callable[[FakeProvider], object], code: str
) -> None:
    pid = scene.sign_in().provider.id
    run(lambda: scene.accounts.sync(pid))
    seven = scene.own(7)
    change(scene.provider)

    refused = failure(lambda: scene.library.profiles.update(seven.id))

    assert refused.code == code
    assert code in types.KEEPS_CONFIG
    assert scene.store.read_config(seven.id) == SAMPLE_CONFIG
    assert scene.own(7).updated_at == NOW
    # Not a failed update: the row shows the code's text.
    assert seven.id not in scene.library.snapshot().update_failed
    assert run(lambda: scene.accounts.summary(pid)).notices == {7: code}

    # Its interval passes: it is not tried again until a sync sees it change.
    details = scene.requests("subscription")
    downloads = scene.requests("config") + scene.requests("link")
    scene.now += 25 * 3600
    run(scene.library.profiles.update_due)
    assert scene.requests("subscription") == details
    assert scene.requests("config") + scene.requests("link") == downloads
    assert scene.store.read_config(seven.id) == SAMPLE_CONFIG


def test_a_renewal_seen_by_a_sync_updates_a_held_profile(scene: Scene) -> None:
    pid = scene.sign_in().provider.id
    run(lambda: scene.accounts.sync(pid))
    seven = scene.own(7)
    scene.provider.update_subscription(
        7, status="expired", usable=False, expireAt=iso(NOW - DAY), updatedAt=iso(NOW)
    )
    run(lambda: scene.accounts.sync(pid))  # changed, but nothing can be downloaded
    assert scene.requests("config") == 1
    scene.now += 25 * 3600
    run(scene.library.profiles.update_due)
    assert scene.requests("subscription") == 1  # the first download only
    assert scene.own(7).updated_at == NOW

    scene.provider.update_subscription(
        7, status="active", usable=True, expireAt=iso(NOW + 60 * DAY), updatedAt=iso(NOW + DAY)
    )
    run(lambda: scene.accounts.sync(pid))
    assert scene.requests("config") == 2
    assert scene.own(7).updated_at == scene.now
    assert scene.store.read_config(seven.id) == SAMPLE_CONFIG
    assert run(lambda: scene.accounts.summary(pid)).notices == {}


def test_a_refused_session_needs_signing_in_and_keeps_the_profiles(scene: Scene) -> None:
    pid = scene.sign_in().provider.id
    run(lambda: scene.accounts.sync(pid))
    seven = scene.own(7)
    scene.provider.revoke()

    refused = failure(lambda: scene.library.profiles.update(seven.id))

    assert refused.code == types.PROVIDER_SIGN_IN_REQUIRED
    assert scene.token(pid) is None
    summary = run(lambda: scene.accounts.summary(pid))
    assert summary.state == ProviderState.SIGN_IN_REQUIRED
    assert summary.notices == {}  # the state says it for every profile
    assert scene.store.read_config(seven.id) == SAMPLE_CONFIG
    assert seven.id not in scene.library.snapshot().update_failed
    # Nothing is asked of the provider until the account signs in again.
    asked = len(scene.provider.requests)
    scene.now += 25 * 3600
    run(scene.library.profiles.update_due)
    again = failure(lambda: scene.library.profiles.update(seven.id))
    assert again.code == types.PROVIDER_SIGN_IN_REQUIRED
    assert len(scene.provider.requests) == asked
    # Nor does a new run of the app ask: the session is gone.
    assert run(scene.open().accounts.summaries)[0].state == ProviderState.SIGN_IN_REQUIRED


def test_a_disabled_account_keeps_the_profiles(scene: Scene) -> None:
    pid = scene.sign_in().provider.id
    run(lambda: scene.accounts.sync(pid))
    scene.provider.fail("subscriptions", 403, "FORBIDDEN")

    refused = failure(lambda: scene.accounts.sync(pid))

    assert refused.code == types.PROVIDER_ACCOUNT_DISABLED
    assert scene.token(pid) is None
    summary = run(lambda: scene.accounts.summary(pid))
    assert (summary.state, summary.error) == (
        ProviderState.ACCOUNT_DISABLED,
        types.PROVIDER_ACCOUNT_DISABLED,
    )
    assert scene.store.read_config(scene.own(7).id) == SAMPLE_CONFIG
    refused = failure(lambda: scene.library.profiles.update(scene.own(7).id))
    assert refused.code == types.PROVIDER_ACCOUNT_DISABLED


def test_a_locked_credential_store_is_read_again_at_the_next_sync(scene: Scene) -> None:
    pid = scene.sign_in().provider.id
    run(lambda: scene.accounts.sync(pid))
    scene.tokens.credentials.fail = True
    later = scene.open()

    refused = failure(lambda: later.accounts.sync(pid))

    assert refused.code == "credentials_unavailable"
    summary = run(later.accounts.summaries)[0]
    assert summary.state == ProviderState.CREDENTIALS_UNAVAILABLE
    assert summary.error == "credentials_unavailable"
    # Its profiles keep their configs and wait.
    seven = scene.own(7)
    scene.now += 25 * 3600 - BACKOFF_S[0]
    assert later.accounts.paused(later.snapshot().get(seven.id), scene.now)

    scene.tokens.credentials.fail = False
    scene.now += BACKOFF_S[0]
    run(later.accounts.sync_all)
    assert run(later.accounts.summaries)[0].state == ProviderState.SIGNED_IN


def test_a_rate_limit_waits_as_long_as_the_provider_asks(scene: Scene) -> None:
    pid = scene.sign_in().provider.id
    scene.provider.fail("subscriptions", 429, "RATE_LIMITED", headers=[("Retry-After", "600")])

    limited = failure(lambda: scene.accounts.sync(pid))

    assert limited.code == types.PROVIDER_RATE_LIMITED
    asked = scene.requests("subscriptions")
    # Asked again within the wait: refused without a request.
    again = failure(lambda: scene.accounts.sync(pid))
    assert isinstance(again, ProviderError)
    assert again.code == types.PROVIDER_RATE_LIMITED
    assert again.retry_after == 600
    scene.now += 599
    run(scene.accounts.sync_all)
    assert scene.requests("subscriptions") == asked
    scene.now += 1
    run(scene.accounts.sync_all)
    assert scene.requests("subscriptions") == asked + 1
    assert run(lambda: scene.accounts.summary(pid)).error == ""


def test_when_syncs_happen(scene: Scene) -> None:
    pid = scene.sign_in().provider.id

    def synced() -> int:
        run(scene.accounts.sync_all)
        return scene.requests("subscriptions")

    # Signing in synced: the next is 30 minutes later.
    asked = scene.requests("subscriptions")
    assert synced() == asked
    scene.now += SYNC_INTERVAL_S - 1
    assert synced() == asked
    scene.now += 1
    assert synced() == asked + 1

    # Failures back off: 5 minutes, then 30.
    scene.provider.fail("subscriptions", 502, times=3)
    scene.now += SYNC_INTERVAL_S
    assert synced() == asked + 2
    assert run(lambda: scene.accounts.summary(pid)).error == types.PROVIDER_UNREACHABLE
    scene.now += BACKOFF_S[0] - 1
    assert synced() == asked + 2
    scene.now += 1
    assert synced() == asked + 3
    scene.now += BACKOFF_S[1] - 1
    assert synced() == asked + 3
    scene.now += 1
    assert synced() == asked + 4
    scene.now += BACKOFF_S[1]
    assert synced() == asked + 5
    assert run(lambda: scene.accounts.summary(pid)).error == ""

    # After the user center was opened: every minute for ten.
    run(lambda: scene.accounts.watch(pid))
    scene.now += 59
    assert synced() == asked + 5
    scene.now += 1
    assert synced() == asked + 6
    for _ in range(int(WATCH_S // 60) - 2):
        scene.now += 60
        synced()
    watched = scene.requests("subscriptions")
    assert watched == asked + 5 + int(WATCH_S // 60) - 1
    scene.now += 60  # ten minutes on: the watch is over
    assert synced() == watched

    # A language switch asks for a sync now; a page shown for one at most a minute old.
    run(lambda: scene.accounts.sync_all(max_age=0))
    assert scene.requests("subscriptions") == watched + 1
    run(lambda: scene.accounts.sync_all(max_age=60))
    assert scene.requests("subscriptions") == watched + 1
    scene.now += 60
    run(lambda: scene.accounts.sync_all(max_age=60))
    assert scene.requests("subscriptions") == watched + 2

    # A new run of the app syncs at its first tick.
    run(scene.open().accounts.sync_all)
    assert scene.requests("subscriptions") == watched + 3


def test_the_profiles_timer_drives_the_sync(scene: Scene) -> None:
    pid = scene.sign_in().provider.id
    later = scene.open()
    scene.host.core_needs_tunnel = True
    run(scene.host.stop)
    assert not later.core_available

    run(later.profiles.update_due)

    # No core: the sync still runs, and keeps the session alive.
    assert scene.requests("subscriptions") == 2
    assert run(lambda: later.accounts.summary(pid)).synced_at == NOW


# -- using and editing ----------------------------------------------------------------------------
def test_using_a_subscription_downloads_and_activates_it(
    tmp_path: Path, provider: FakeProvider
) -> None:
    scene = Scene(tmp_path, provider, seeded=True)
    pid = scene.sign_in().provider.id

    profiles = run(lambda: scene.accounts.use(pid, 7))

    seven = scene.own(7)
    assert profiles.active == seven.id
    assert scene.store.read_config(seven.id) == SAMPLE_CONFIG
    assert run(scene.library.routing).mode == MODE_PROFILE
    assert run(lambda: scene.accounts.summary(pid)).active == 7
    expired = failure(lambda: scene.accounts.use(pid, 9))
    assert expired.code == types.PROVIDER_SUBSCRIPTION_EXPIRED
    missing = failure(lambda: scene.accounts.use(pid, 42))
    assert missing.code == types.PROVIDER_SUBSCRIPTION_MISSING


def test_a_providers_profile_keeps_its_name_and_has_no_link(scene: Scene) -> None:
    scene.sign_in()
    seven = scene.own(7)
    renamed = failure(lambda: scene.library.profiles.edit(seven.id, "我的", "", True))
    assert renamed.code == ACCOUNT_PROFILE
    linked = failure(
        lambda: scene.library.profiles.edit(seven.id, seven.name, "https://sub.example/x", True)
    )
    assert linked.code == ACCOUNT_PROFILE

    run(lambda: scene.library.profiles.edit(seven.id, seven.name, "", True, interval_hours=6))
    assert scene.own(7).auto_update_hours == 6
    run(lambda: scene.library.profiles.set_auto_update(seven.id, False))
    assert scene.own(7).auto_update_hours == -6
    run(lambda: scene.library.profiles.set_auto_update(seven.id, True))
    assert scene.own(7).auto_update_hours == 6


def test_wallet_plans_orders_and_auto_renew(scene: Scene) -> None:
    pid = scene.sign_in().provider.id

    assert run(lambda: scene.accounts.wallet(pid)).balance == 1500
    assert [p.id for p in run(lambda: scene.accounts.plans(pid))] == ["pro", "pack-50"]
    assert [o.id for o in run(lambda: scene.accounts.orders(pid))] == ["o-3", "o-1"]

    run(lambda: scene.accounts.sync(pid))
    assert scene.requests("config") == 1
    renewed = run(lambda: scene.accounts.set_auto_renew(pid, 7, True))
    assert renewed.auto_renew
    assert renewed.access == "both"  # the answer has none; the stored one stays
    stored = run(lambda: scene.accounts.summary(pid)).subscriptions[0]
    assert (stored.auto_renew, stored.access) == (True, "both")
    # The switch changed the subscription's time, which the stored copy has already: the
    # sync after it downloads nothing.
    run(lambda: scene.accounts.sync(pid))
    assert scene.requests("config") == 1

    # Plans are public: they still show once the session is refused.
    scene.provider.revoke()
    assert failure(lambda: scene.accounts.wallet(pid)).code == types.PROVIDER_SIGN_IN_REQUIRED
    assert len(run(lambda: scene.accounts.plans(pid))) == 2


# -- signing out ----------------------------------------------------------------------------------
@pytest.mark.parametrize("online", [True, False])
def test_signing_out_removes_the_providers_profiles(
    tmp_path: Path, provider: FakeProvider, online: bool
) -> None:
    scene = Scene(tmp_path, provider, seeded=True)
    pid = scene.sign_in().provider.id
    run(lambda: scene.accounts.use(pid, 7))
    run(lambda: scene.accounts.sync(pid))
    seven = scene.own(7)

    removal = run(lambda: scene.accounts.sign_out_removal(pid))
    assert removal.active
    assert removal.successor is not None
    assert removal.successor.id == "sample-a"
    assert [p.remote_id for p in removal.profiles] == [7, 9]

    if not online:
        provider.fail("sign_out", 502)
    reloads = scene.host.reloads
    profiles = run(lambda: scene.accounts.sign_out(pid))

    assert profiles.active == "sample-a"
    assert scene.provider_profiles() == []
    assert [p.id for p in scene.store.load().profiles] == ["sample-a", "sample-b", "sample-c"]
    assert scene.host.reloads == reloads + 1
    with pytest.raises(ProfileError):
        scene.store.read_config(seven.id)
    (request,) = provider.requests_for("sign_out")
    assert request.bearer
    assert scene.token(pid) is None
    store = ProviderStore(scene.store.database)
    assert store.providers() == ()
    assert store.subscriptions(pid) == ()
    assert run(scene.accounts.summaries) == ()
    assert failure(lambda: scene.accounts.sync(pid)).code == types.PROVIDER_UNKNOWN


# -- restoring ------------------------------------------------------------------------------------
def test_a_restore_takes_up_the_providers_and_reads_their_configs_again(scene: Scene) -> None:
    pid = scene.sign_in().provider.id
    run(lambda: scene.accounts.sync(pid))
    store = ProviderStore(scene.store.database)

    def write() -> None:
        # As a backup's restore: billing is a cache of this machine, and does not come back.
        store.forget_subscriptions(p.id for p in scene.provider_profiles())

    run(lambda: scene.library.profiles.restore(write))

    # The same account keeps its session; its profiles are read through it again.
    summary = run(lambda: scene.accounts.summary(pid))
    assert summary.state == ProviderState.SIGNED_IN
    assert [s.id for s in summary.subscriptions] == [7, 9]
    assert summary.notices == {9: types.PROVIDER_SUBSCRIPTION_EXPIRED}
    assert scene.requests("config") == 2
    assert scene.library.snapshot().update_failed == frozenset()


def test_a_restored_provider_needs_signing_in_and_relinks_its_profiles(scene: Scene) -> None:
    store = ProviderStore(scene.store.database)
    restored: list[str] = []

    def write() -> None:
        # As a backup from another device: the provider and its profiles, no session, no
        # config, no billing.
        record = store.add(scene.provider.base, "示例服务商", EMAIL, "user-1")
        restored.append(record.id)
        profiles = tuple(
            Profile(
                id=f"restored-{remote_id}",
                name=name,
                kind=KIND_PROVIDER,
                auto_update_hours=24,
                provider_id=record.id,
                remote_id=remote_id,
            )
            for remote_id, name in ((7, "专业版"), (9, "基础版"))
        )
        scene.store.save(ProfileList(profiles))

    run(lambda: scene.library.profiles.restore(write))

    (pid,) = restored
    summary = run(lambda: scene.accounts.summary(pid))
    assert summary.state == ProviderState.SIGN_IN_REQUIRED
    assert summary.notices == {}
    # Reading them again asked nothing of the provider, and failed nothing.
    assert scene.provider.requests == []
    assert scene.library.snapshot().update_failed == frozenset()

    # Signing in to the same account takes the restored record and its profiles.
    signed = scene.sign_in()
    assert signed.provider.id == pid
    assert scene.token(pid)
    assert [p.id for p in scene.provider_profiles()] == ["restored-7", "restored-9"]
    run(lambda: scene.accounts.sync(pid))
    assert scene.store.read_config("restored-7") == SAMPLE_CONFIG
    # Another account asks first.
    conflict = failure(lambda: scene.signing_in("other@example.com"))
    assert conflict.code == types.PROVIDER_ACCOUNT_CONFLICT


def test_a_restore_without_a_provider_deletes_its_session(scene: Scene) -> None:
    pid = scene.sign_in().provider.id
    store = ProviderStore(scene.store.database)

    def write() -> None:
        # As a sign-out merged from another device.
        scene.store.save(ProfileList())
        store.delete(pid)

    run(lambda: scene.library.profiles.restore(write))

    assert run(scene.accounts.summaries) == ()
    assert scene.token(pid) is None
    assert scene.provider_profiles() == []


# -- secrets --------------------------------------------------------------------------------------
def test_sessions_and_links_reach_neither_the_database_nor_a_log(
    scene: Scene, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    scene.provider.access = "link"
    pid = scene.sign_in().provider.id
    token = scene.token(pid)
    assert token
    run(lambda: scene.accounts.sync(pid))
    link = scene.provider.link(7)
    scene.provider.revoke()
    failure(lambda: scene.accounts.sync(pid))
    summaries = run(scene.accounts.summaries)

    secrets = [token, token.partition(".")[0], link, link.rsplit("/", 1)[1]]
    stored = b"".join(p.read_bytes() for p in scene.directory.glob(f"{DATABASE_NAME}*"))
    for secret in secrets:
        assert secret.encode() not in stored
        assert secret not in caplog.text
        assert secret not in repr(summaries)
