"""What the diagnostics archive tells of provider accounts (docs/accounts.md §8): how many
there are, whether each is signed in, and its profiles' access mode and status, by allow
list, never anything that identifies the provider, the account or the user."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from openweir import diagnostics
from openweir.host.base import HostError
from openweir.models.credentials import (
    PROVIDER_TOKEN,
    CredentialError,
    ProviderToken,
    ProviderTokens,
)
from openweir.models.library import Routing
from openweir.models.profiles import KIND_PROVIDER, KIND_SUBSCRIPTION, Profile, ProfileList
from openweir.models.provider.store import ProviderStore
from openweir.models.provider.types import ProviderState, RemoteSubscription
from openweir.models.routing import RoutingState
from openweir.models.settings import Settings
from openweir.models.store import ProfileStore
from tests.support.credentials import MemoryCredentials, MemoryProviderTokens
from tests.support.storage import stored

NOW = 1_800_000_000.0
TOKEN = "session-token-secret"
LINK = "https://link.example.org/s/link-secret"
# What identifies a provider, its account or its user, and must stay out of the archive.
SECRETS = (
    "panel-one.example.com",
    "panel-two.example.com",
    "panel-three.example.com",
    "服务商甲",
    "Provider B",
    "Provider C",
    "someone@example.com",
    "other-user",
    "user-id-one",
    "user-id-two",
    "user-id-secret",
    TOKEN,
    "link-secret",
    "link.example.org",
    "套餐名称",
    "plan-id-secret",
)


class LockedFor(MemoryCredentials):
    """A credential store that cannot read one key, as a keychain locked for one item, and
    says what it could not read in its error."""

    def __init__(self, locked: str) -> None:
        super().__init__()
        self.locked = locked

    def read(self, key: str) -> str | None:
        if key == self.locked:
            raise CredentialError(
                "credentials_unavailable",
                f"locked: {key} {TOKEN} https://panel-three.example.com someone@example.com",
            )
        return super().read(key)


def billing(remote_id: int, *, access: str, status: str = "active") -> RemoteSubscription:
    return RemoteSubscription(
        id=remote_id,
        plan_id="plan-id-secret",
        status=status,
        usable=status == "active",
        plan_name="套餐名称",
        user_id="user-id-secret",
        access=access,
        url=LINK,
        urls=(LINK,),
    )


def accounts_in(tmp_path: Path) -> tuple[ProfileStore, diagnostics.Accounts]:
    """Three providers: signed in, signed out by a 401, and one the keychain cannot read.
    Their profiles come after a subscription; billing describes all but the last."""
    store = ProfileStore(tmp_path)
    providers = ProviderStore(store.database)
    one = providers.add(
        "https://panel-one.example.com", "服务商甲", "someone@example.com", "user-id-one"
    )
    two = providers.add("https://panel-two.example.com", "Provider B", "other-user", "user-id-two")
    three = providers.add(
        "https://panel-three.example.com", "Provider C", "someone@example.com", "user-id-secret"
    )
    subscription = Profile(id="s", name="订阅", kind=KIND_SUBSCRIPTION, url=LINK)

    def plan(profile_id: str, provider_id: str, remote_id: int) -> Profile:
        return Profile(
            id=profile_id,
            name="套餐名称",
            kind=KIND_PROVIDER,
            provider_id=provider_id,
            remote_id=remote_id,
        )

    profiles = (
        subscription,
        plan("p1", one.id, 7),
        plan("p2", two.id, 8),
        plan("p3", two.id, 9),
        plan("p4", three.id, 10),
    )
    store.save(ProfileList(profiles, "p1"))
    providers.save_subscriptions(
        {
            "p1": billing(7, access="app"),
            "p2": billing(8, access="link", status="expired"),
            # The provider's words: only those the app knows are written.
            "p3": billing(9, access="someone@example.com", status="https://panel-two.example.com"),
        }
    )
    tokens = ProviderTokens(LockedFor(PROVIDER_TOKEN.format(id=three.id)))
    tokens.save(one.id, ProviderToken(TOKEN, "user-id-one"))
    return store, diagnostics.read_accounts(providers, tokens)


def write(tmp_path: Path, profiles: ProfileList, accounts: diagnostics.Accounts | None) -> str:
    snapshot = diagnostics.Snapshot(
        info=HostError("unreachable", "no service"),
        status=HostError("unreachable", "no service"),
        settings=Settings(),
        profiles=profiles,
        routing=Routing(RoutingState(), (), None, {}),
        logs=(),
        language="zh",
        accounts=accounts,
    )
    path = tmp_path / "diagnostics.zip"
    diagnostics.write_archive(path, snapshot, NOW)
    with zipfile.ZipFile(path) as archive:
        return "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())


def settings_of(tmp_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(tmp_path / "diagnostics.zip") as archive:
        return json.loads(archive.read("settings.json"))


def test_accounts_read_from_the_database_and_the_credential_store(tmp_path: Path) -> None:
    _, accounts = accounts_in(tmp_path / "data")
    assert accounts.states == (
        ProviderState.SIGNED_IN,
        ProviderState.SIGN_IN_REQUIRED,
        ProviderState.CREDENTIALS_UNAVAILABLE,
    )
    assert set(accounts.billing) == {"p1", "p2", "p3"}
    assert not accounts.error


def test_the_archive_tells_of_accounts_by_allow_list(tmp_path: Path) -> None:
    store, accounts = accounts_in(tmp_path / "data")
    everything = write(tmp_path, store.load(), accounts)
    written = settings_of(tmp_path)

    assert written["accounts"] == {
        "providers": 3,
        "states": ["signed_in", "sign_in_required", "credentials_unavailable"],
    }
    profiles = written["profiles"]
    assert [p["kind"] for p in profiles] == ["subscription", *["provider"] * 4]
    assert "billing" not in profiles[0]
    assert [p["billing"] for p in profiles[1:]] == [
        {"access": "app", "status": "active", "usable": True},
        {"access": "link", "status": "expired", "usable": False},
        {"access": "other", "status": "other", "usable": False},
        None,  # not synced yet
    ]
    assert profiles[1]["active"]
    for secret in SECRETS:
        assert secret not in everything, secret
    assert "locked" not in everything  # the credential store's error stays out


def test_accounts_unread_or_unreadable(tmp_path: Path) -> None:
    store, _ = accounts_in(tmp_path / "data")
    profiles = store.load()

    write(tmp_path, profiles, None)
    written = settings_of(tmp_path)
    assert written["accounts"] is None
    assert [p.get("billing") for p in written["profiles"][1:]] == [None] * 4

    stored(tmp_path / "data", "DROP TABLE provider_subscriptions")
    accounts = diagnostics.read_accounts(ProviderStore(store.database), MemoryProviderTokens())
    assert accounts == diagnostics.Accounts(error="storage_failed")
    everything = write(tmp_path, profiles, accounts)
    assert settings_of(tmp_path)["accounts"] == {"error": "storage_failed"}
    assert "provider_subscriptions" not in everything  # sqlite's message stays out
