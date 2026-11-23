import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from openweir.models.profiles import (
    KIND_FILE,
    KIND_PROVIDER,
    KIND_SUBSCRIPTION,
    Profile,
    ProfileError,
    ProfileList,
    Usage,
)
from openweir.models.provider.store import ProviderStore, StoredSubscription
from openweir.models.provider.types import (
    PROVIDER_ACCOUNT_CONFLICT,
    PROVIDER_UNKNOWN,
    ProviderError,
    ProviderRecord,
    RemoteSubscription,
)
from openweir.models.settings import DatabaseSettingsStore
from openweir.models.store import ProfileStore, default_data_dir
from tests.support.storage import refuse_writes, stored

SUBSCRIPTION = Profile(
    id="a1",
    name="示例订阅 A",
    kind=KIND_SUBSCRIPTION,
    url="https://sub.example.com/s?token=x",
    auto_update_hours=12,
    updated_at=1_790_000_000.0,
    usage=Usage(1, 2, 300, 1_800_000_000),
    proxy_count=48,
)
LOCAL = Profile(id="b2", name="config", kind=KIND_FILE, path="/tmp/config.yaml", in_pool=False)
PLAN = Profile(
    id="c3",
    name="标准套餐",
    kind=KIND_PROVIDER,
    auto_update_hours=24,
    provider_id="prov1",
    remote_id=7,
    usage=Usage(1, 2, 300, 0),
    updated_at=1_790_000_000.0,
)
ORIGIN = "https://example.com"
LINK = "https://example.com/api/v1/app/proxy/s/secret-token"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def rows(directory: Path, table: str, columns: str) -> dict[str, tuple[object, ...]]:
    return {row[0]: row[1:] for row in stored(directory, f"SELECT {columns} FROM {table}")}


def test_profiles_round_trip(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    assert store.load() == ProfileList()
    store.write_config("a1", "proxies: []\n")
    store.write_config("b2", "mixed-port: 9347\n")
    store.save(ProfileList((SUBSCRIPTION, LOCAL), "a1", frozenset({"a1"})))
    store.close()

    store = ProfileStore(tmp_path)
    # Update failures are not stored.
    assert store.load() == ProfileList((SUBSCRIPTION, LOCAL), "a1")
    assert store.read_config("a1") == "proxies: []\n"
    assert store.load().get("b2").in_pool is False
    store.save(ProfileList((LOCAL, SUBSCRIPTION), "b2"))  # reordered, another one in use
    assert store.load() == ProfileList((LOCAL, SUBSCRIPTION), "b2")
    # The profile in use is the one the routing state names (routing.md §9).
    assert stored(tmp_path, "SELECT profile_id FROM routing_state") == [("b2",)]


def test_what_came_with_a_config_is_stored_with_it(tmp_path: Path) -> None:
    """The time, usage and proxy count describe the downloaded config, a cache of this
    machine (storage.md §2), so they are kept beside it and not with the profile."""
    store = ProfileStore(tmp_path)
    store.save(ProfileList((SUBSCRIPTION,), "a1"))
    assert store.load().get("a1") == replace(
        SUBSCRIPTION, updated_at=0.0, usage=None, proxy_count=None
    )
    store.write_config("a1", "proxies: []\n")
    store.save(ProfileList((SUBSCRIPTION,), "a1"))
    assert store.load().get("a1") == SUBSCRIPTION
    columns = "profile_id, fetched_at, upload, download, total, expire, proxy_count, unread"
    assert rows(tmp_path, "profile_contents", columns) == {
        "a1": (1_790_000_000.0, 1, 2, 300, 1_800_000_000, 48, 0)
    }
    # Stored while no core could read it (D27), until one did.
    store.save(ProfileList((replace(SUBSCRIPTION, proxy_count=None, unread=True),), "a1"))
    assert store.load().get("a1").unread
    read = replace(SUBSCRIPTION, has_rules=False)
    store.save(ProfileList((read,), "a1"))
    assert store.load().get("a1") == read


def test_an_unread_update_keeps_the_text_a_core_read(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    store.write_config("a1", "proxies: []\n# read\n")
    store.save(ProfileList((replace(SUBSCRIPTION, has_rules=False),), "a1"))
    assert not store.fall_back("a1")  # nothing waits
    unread = replace(SUBSCRIPTION, unread=True)
    for text in ("# first\n", "# second\n"):
        store.write_config("a1", text, keep_read=True)
        store.save(ProfileList((unread,), "a1"))
    assert store.read_config("a1") == "# second\n"
    assert store.fall_back("a1")
    assert store.read_config("a1") == "proxies: []\n# read\n"
    # Read at last: nothing to fall back to any more.
    store.write_config("a1", "# third\n", keep_read=True)
    store.save(ProfileList((unread,), "a1"))
    store.save(ProfileList((SUBSCRIPTION,), "a1"))
    assert not store.fall_back("a1")


def test_records_change_only_when_their_values_do(tmp_path: Path) -> None:
    clock = Clock()
    store = ProfileStore(tmp_path, clock=clock)
    store.write_config("a1", "proxies: []\n")
    store.save(ProfileList((SUBSCRIPTION, LOCAL), "a1"))
    clock.now = 2_000.0
    store.save(ProfileList((SUBSCRIPTION, LOCAL), "a1"))  # nothing changed
    store.save(ProfileList((SUBSCRIPTION, replace(LOCAL, name="renamed")), "a1"))
    columns = "id, created_at, updated_at, deleted_at"
    assert rows(tmp_path, "profiles", columns) == {
        "a1": (1_000.0, 1_000.0, None),
        "b2": (1_000.0, 2_000.0, None),
    }
    # An update that only downloaded the config again leaves the profile as it was.
    clock.now = 3_000.0
    store.write_config("a1", "proxies: []\n")
    store.save(ProfileList((replace(SUBSCRIPTION, updated_at=3_000.0), LOCAL), "a1"))
    assert rows(tmp_path, "profiles", columns)["a1"] == (1_000.0, 1_000.0, None)
    assert rows(tmp_path, "profile_contents", "profile_id, updated_at")["a1"] == (3_000.0,)


def test_removed_profiles_are_marked_deleted(tmp_path: Path) -> None:
    clock = Clock()
    store = ProfileStore(tmp_path, clock=clock)
    for profile in (SUBSCRIPTION, LOCAL):
        store.write_config(profile.id, "proxies: []\n")
    store.write_override("b2", "mode: direct\n")
    store.save(ProfileList((SUBSCRIPTION, LOCAL), "a1"))

    clock.now = 2_000.0
    store.save(ProfileList((SUBSCRIPTION,), "a1"))
    store.delete_config("b2")
    assert store.load() == ProfileList((SUBSCRIPTION,), "a1")
    # The record stays, marked, for a backup to merge; the config, a cache, is gone.
    assert rows(tmp_path, "profiles", "id, updated_at, deleted_at")["b2"] == (2_000.0, 2_000.0)
    assert rows(tmp_path, "overrides", "scope, deleted_at") == {"b2": (2_000.0,)}
    with pytest.raises(ProfileError) as raised:
        store.read_config("b2")
    assert raised.value.code == "config_missing"
    assert store.read_override("b2") == ""

    clock.now = 3_000.0
    store.save(ProfileList((SUBSCRIPTION, LOCAL), "a1"))  # the same id is restored
    assert rows(tmp_path, "profiles", "id, updated_at, deleted_at")["b2"] == (3_000.0, None)


def test_overrides(tmp_path: Path) -> None:
    clock = Clock()
    store = ProfileStore(tmp_path, clock=clock)
    store.save(ProfileList((SUBSCRIPTION, LOCAL), "a1"))
    assert store.read_override(None) == ""
    store.write_override(None, "mode: global\n")
    store.write_override("b2", "prepend-rules: [MATCH,DIRECT]\n")

    loaded = ProfileStore(tmp_path).load()
    assert loaded.has_global_override
    assert [p.has_override for p in loaded.profiles] == [False, True]
    assert store.read_override(None) == "mode: global\n"
    assert store.read_override("b2") == "prepend-rules: [MATCH,DIRECT]\n"

    # Blank text deletes an override: the record is marked, and writing again restores it.
    clock.now = 2_000.0
    store.write_override(None, "\n  \n")
    loaded = store.load()
    assert not loaded.has_global_override
    assert store.read_override(None) == ""
    assert rows(tmp_path, "overrides", "scope, updated_at, deleted_at")["global"] == (
        2_000.0,
        2_000.0,
    )
    clock.now = 3_000.0
    store.write_override(None, "mode: rule\n")
    assert store.read_override(None) == "mode: rule\n"
    assert rows(tmp_path, "overrides", "scope, updated_at, deleted_at")["global"] == (
        3_000.0,
        None,
    )


def test_active_must_name_a_profile(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    store.save(ProfileList((LOCAL,), "gone"))
    assert store.load().active is None


def test_missing_config_is_a_profile_error(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    with pytest.raises(ProfileError) as raised:
        store.read_config("nope")
    assert raised.value.code == "config_missing"
    store.delete_config("nope")  # deleting twice is fine


def test_a_config_no_profile_names_goes_at_the_next_start(tmp_path: Path) -> None:
    """A crash between writing a new profile's config and saving the list leaves it behind."""
    store = ProfileStore(tmp_path)
    store.write_config("a1", "proxies: []\n")
    store.write_config("new", "proxies: []\n")
    store.save(ProfileList((SUBSCRIPTION,), "a1"))
    store.close()
    ProfileStore(tmp_path).close()
    assert stored(tmp_path, "SELECT profile_id FROM profile_contents") == [("a1",)]


def test_failed_writes_are_profile_errors(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    store.write_config("a1", "proxies: []\n")
    store.save(ProfileList((SUBSCRIPTION,), "a1"))
    refuse_writes(tmp_path, "profiles")
    with pytest.raises(ProfileError) as raised:
        store.save(ProfileList((SUBSCRIPTION, LOCAL), "b2"))
    assert raised.value.code == "storage_failed"
    assert "disk is full" in raised.value.message
    assert store.load() == ProfileList((SUBSCRIPTION,), "a1")  # all or nothing


def test_threads_share_the_store(tmp_path: Path) -> None:
    """The asyncio thread's workers and the Qt thread read and write at the same time."""
    store = ProfileStore(tmp_path)
    settings = DatabaseSettingsStore(store.database)
    profiles = [
        Profile(id=f"p{n}", name=f"配置 {n}", kind=KIND_SUBSCRIPTION, url=f"https://e/{n}")
        for n in range(8)
    ]
    errors: list[BaseException] = []
    lock = threading.Lock()  # the library writes the list under its own lock
    saved = [ProfileList()]

    def write(profile: Profile) -> None:
        try:
            for round_ in range(20):
                store.write_config(profile.id, f"# {round_}\nproxies: []\n")
                store.write_override(profile.id, f"mode: {'rule' if round_ % 2 else 'direct'}\n")
                with lock:
                    saved[0] = saved[0].put(replace(profile, auto_update_hours=round_))
                    store.save(saved[0])
                settings.update(mixed_port=10_000 + round_)
        except BaseException as error:
            errors.append(error)

    def read() -> None:
        try:
            for _ in range(100):
                listed = store.load()
                for profile in listed.profiles:
                    assert store.read_config(profile.id).endswith("proxies: []\n")
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=write, args=(p,)) for p in profiles]
    threads += [threading.Thread(target=read) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    loaded = ProfileStore(tmp_path).load()
    assert {p.id: p.auto_update_hours for p in loaded.profiles} == {p.id: 19 for p in profiles}
    assert all(store.read_override(p.id) == "mode: rule\n" for p in profiles)
    assert settings.current.mixed_port == 10_019
    assert DatabaseSettingsStore(store.database).current == settings.current


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("darwin", {}, "Library/Application Support/OpenWeir"),
        ("win32", {"LOCALAPPDATA": "{home}/Local"}, "Local/OpenWeir"),
        ("linux", {"XDG_DATA_HOME": ""}, ".local/share/OpenWeir"),
    ],
)
def test_default_data_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    env: dict[str, str],
    expected: str,
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value.format(home=tmp_path))
    assert default_data_dir() == tmp_path / expected


# -- provider accounts (docs/accounts.md §5.1) -----------------------------------------------
def remote(remote_id: int, **values: object) -> RemoteSubscription:
    return RemoteSubscription(
        **{
            "id": remote_id,
            "plan_id": "basic",
            "status": "active",
            "usable": True,
            "plan_name": "标准套餐",
            "user_id": "user-1",
            "expire_at": 1_800_000_000.0,
            "total": 300,
            "access": "both",
            "url": LINK,
            "urls": (LINK + "-mirror",),
            **values,
        }  # type: ignore[arg-type]
    )


def test_provider_profiles_round_trip(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    for profile in (SUBSCRIPTION, PLAN, LOCAL):
        store.write_config(profile.id, "proxies: []\n")
    store.save(ProfileList((SUBSCRIPTION, PLAN, LOCAL), "c3"))
    store.close()
    store = ProfileStore(tmp_path)
    assert store.load() == ProfileList((SUBSCRIPTION, PLAN, LOCAL), "c3")
    columns = "id, kind, url, path, provider_id, remote_id"
    assert rows(tmp_path, "profiles", columns) == {
        "a1": ("subscription", SUBSCRIPTION.url, "", None, None),
        "b2": ("file", "", "/tmp/config.yaml", None, None),
        "c3": ("provider", "", "", "prov1", 7),
    }


@pytest.mark.parametrize(
    "values",
    [
        {"kind": KIND_PROVIDER, "remote_id": 7},  # no provider
        {"kind": KIND_PROVIDER, "provider_id": "", "remote_id": 7},
        {"kind": KIND_PROVIDER, "provider_id": "prov1"},  # no subscription
        {"kind": KIND_PROVIDER, "provider_id": "prov1", "remote_id": "7"},
        # The link stays in memory (D57); a provider profile has no file either.
        {"kind": KIND_PROVIDER, "provider_id": "prov1", "remote_id": 7, "url": LINK},
        {"kind": KIND_PROVIDER, "provider_id": "prov1", "remote_id": 7, "path": "/tmp/x"},
        {"kind": KIND_SUBSCRIPTION, "url": LINK, "provider_id": "prov1"},
        {"kind": KIND_FILE, "path": "/tmp/x", "remote_id": 7},
    ],
)
def test_only_a_provider_profile_names_a_provider_and_it_has_no_link(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="provider"):
        Profile(id="x", name="x", **values)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no link"):
        replace(PLAN, url=LINK)


def test_a_provider_profile_updates_through_its_account() -> None:
    nodes = Profile(id="n", name="nodes", kind=KIND_FILE)
    kinds = {
        "subscription": SUBSCRIPTION,
        "provider": PLAN,
        "file": LOCAL,
        "nodes": nodes,
    }
    assert {k: p.can_update for k, p in kinds.items()} == {
        "subscription": True,
        "provider": True,
        "file": True,
        "nodes": False,
    }
    assert {k: p.can_auto_update for k, p in kinds.items()} == {
        "subscription": True,
        "provider": True,
        "file": False,
        "nodes": False,
    }
    # It has no link of its own: no subscription link, nor node links.
    assert (PLAN.is_provider, PLAN.is_subscription, PLAN.is_node_links) == (True, False, False)
    assert not any(p.is_provider for p in (SUBSCRIPTION, LOCAL, nodes))


def test_providers_in_order(tmp_path: Path) -> None:
    clock = Clock()
    database = ProfileStore(tmp_path, clock=clock).database
    providers = ProviderStore(database)
    assert providers.providers() == ()
    assert providers.by_origin(ORIGIN) is None
    first = providers.add(ORIGIN, "示例服务商", "user@example.com", "user-1")
    assert first == ProviderRecord(
        first.id, ORIGIN, "示例服务商", "user@example.com", "user-1", 0, 1_000.0, 1_000.0
    )
    assert len(first.id) == 32
    clock.now = 2_000.0
    second = providers.add("https://b.example:8443", "B", "b", "user-9")
    assert second.position == 1
    assert providers.providers() == (first, second)
    assert providers.provider(second.id) == second
    assert providers.by_origin(ORIGIN) == first

    # Moved first: the order follows the positions.
    clock.now = 3_000.0
    moved = providers.update(replace(second, position=-1))
    assert (moved.position, moved.created_at, moved.updated_at) == (-1, 2_000.0, 3_000.0)
    assert providers.providers() == (moved, first)


def test_one_provider_per_origin(tmp_path: Path) -> None:
    providers = ProviderStore(ProfileStore(tmp_path).database)
    first = providers.add(ORIGIN, "示例服务商", "user@example.com", "user-1")
    with pytest.raises(ProviderError) as raised:
        providers.add(ORIGIN, "示例服务商", "other@example.com", "user-2")
    assert raised.value.code == PROVIDER_ACCOUNT_CONFLICT
    assert providers.providers() == (first,)
    # Once it is signed out of, another account may sign in: a new provider.
    providers.delete(first.id)
    again = providers.add(ORIGIN, "示例服务商", "other@example.com", "user-2")
    assert again.id != first.id
    assert providers.providers() == (again,)


def test_a_provider_changes_only_when_its_values_do(tmp_path: Path) -> None:
    clock = Clock()
    providers = ProviderStore(ProfileStore(tmp_path, clock=clock).database)
    record = providers.add(ORIGIN, "示例服务商", "user@example.com", "user-1")
    clock.now = 2_000.0
    assert providers.update(record) == record  # nothing changed
    # The id and origin are the provider's for good; the times are the store's.
    renamed = providers.update(
        replace(record, name="Example", origin="https://elsewhere", created_at=5.0)
    )
    assert renamed == replace(record, name="Example", updated_at=2_000.0)
    columns = "id, origin, name, created_at, updated_at, deleted_at"
    assert rows(tmp_path, "providers", columns) == {
        record.id: (ORIGIN, "Example", 1_000.0, 2_000.0, None)
    }


def test_signing_out_marks_the_provider_deleted(tmp_path: Path) -> None:
    clock = Clock()
    providers = ProviderStore(ProfileStore(tmp_path, clock=clock).database)
    record = providers.add(ORIGIN, "示例服务商", "user@example.com", "user-1")
    clock.now = 2_000.0
    providers.delete(record.id)
    providers.delete(record.id)  # twice is fine
    assert providers.providers() == ()
    assert providers.by_origin(ORIGIN) is None
    for call in (lambda: providers.provider(record.id), lambda: providers.update(record)):
        with pytest.raises(ProviderError) as raised:
            call()
        assert raised.value.code == PROVIDER_UNKNOWN
    # The record stays, marked, for a backup to merge; signing out meanwhile keeps it so.
    columns = "id, updated_at, deleted_at"
    assert rows(tmp_path, "providers", columns) == {record.id: (2_000.0, 2_000.0)}


def test_what_billing_said_is_stored_without_links(tmp_path: Path) -> None:
    clock = Clock()
    store = ProfileStore(tmp_path, clock=clock)
    providers = ProviderStore(store.database)
    other = replace(PLAN, id="d4", name="Other", remote_id=3)
    store.save(ProfileList((PLAN, other, SUBSCRIPTION)))
    assert providers.subscriptions("prov1") == ()
    written = providers.save_subscriptions({"c3": remote(7), "d4": remote(3, status="expired")})
    assert written == 2
    assert providers.subscriptions("prov1") == (
        StoredSubscription("d4", remote(3, status="expired"), 1_000.0),
        StoredSubscription("c3", remote(7), 1_000.0),
    )
    data = stored(tmp_path, "SELECT data FROM provider_subscriptions")
    assert len(data) == 2
    assert all("secret-token" not in text and '"url' not in text for (text,) in data)
    assert all(s.subscription.url is None for s in providers.subscriptions("prov1"))

    clock.now = 2_000.0
    providers.save_subscriptions({"c3": remote(7, auto_renew=True)})
    (_, latest) = providers.subscriptions("prov1")
    assert (latest.subscription.auto_renew, latest.synced_at) == (True, 2_000.0)
    assert providers.subscriptions("prov2") == ()


def test_only_a_live_provider_profile_takes_its_subscription(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    providers = ProviderStore(store.database)
    store.save(ProfileList((PLAN, SUBSCRIPTION)))
    written = providers.save_subscriptions(
        {
            "a1": remote(7),  # no provider profile
            "c3": remote(8),  # another subscription than the profile's
            "missing": remote(7),  # not saved yet, or removed meanwhile
        }
    )
    assert written == 0
    assert stored(tmp_path, "SELECT * FROM provider_subscriptions") == []


def test_the_billing_cache_goes_with_its_profile(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    providers = ProviderStore(store.database)
    record = providers.add(ORIGIN, "示例服务商", "user@example.com", "user-1")
    first = replace(PLAN, provider_id=record.id)
    second = replace(first, id="d4", remote_id=3)
    third = replace(first, id="e5", remote_id=9)
    store.save(ProfileList((first, second, third)))
    providers.save_subscriptions({"c3": remote(7), "d4": remote(3), "e5": remote(9)})

    # Removed like any profile: its config, override and billing go.
    store.save(ProfileList((first, third)))
    store.delete_config("d4")
    assert [s.profile_id for s in providers.subscriptions(record.id)] == ["c3", "e5"]
    # A profile marked deleted shows nothing, and its row goes at the next start.
    store.save(ProfileList((first,)))
    assert [s.profile_id for s in providers.subscriptions(record.id)] == ["c3"]
    store.close()
    store = ProfileStore(tmp_path)
    assert stored(tmp_path, "SELECT profile_id FROM provider_subscriptions") == [("c3",)]
    # Signing out drops what is left.
    ProviderStore(store.database).delete(record.id)
    assert stored(tmp_path, "SELECT profile_id FROM provider_subscriptions") == []


def test_a_billing_row_this_version_cannot_read_is_left_out(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    providers = ProviderStore(store.database)
    store.save(ProfileList((PLAN, replace(PLAN, id="d4", remote_id=3))))
    providers.save_subscriptions({"c3": remote(7), "d4": remote(3)})
    with store.database.transaction() as connection:
        connection.execute("UPDATE provider_subscriptions SET data = '[]' WHERE profile_id = 'd4'")
    assert [s.profile_id for s in providers.subscriptions("prov1")] == ["c3"]
    with store.database.transaction() as connection:
        connection.execute("UPDATE provider_subscriptions SET data = '{\"id\": 3}'")
    assert providers.subscriptions("prov1") == ()


def test_failed_provider_writes_are_profile_errors(tmp_path: Path) -> None:
    providers = ProviderStore(ProfileStore(tmp_path).database)
    refuse_writes(tmp_path, "providers")
    with pytest.raises(ProfileError) as raised:
        providers.add(ORIGIN, "示例服务商", "user@example.com", "user-1")
    assert raised.value.code == "storage_failed"
    assert providers.providers() == ()
