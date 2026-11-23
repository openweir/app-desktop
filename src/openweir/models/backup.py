"""Backup snapshots (docs/storage.md §3): the user's data in the database as one JSON
document, to keep in a file or upload to a backup target, and its restore.

A snapshot holds what the user made, nothing of this machine:

    profiles   subscriptions with their link and update setting, not what was downloaded;
               local files with the config as imported (``config``); a provider account's
               subscriptions with their provider and their id there, no link and no config
    providers  the provider accounts signed in to (version 2, D59), without their sessions
    overrides  the global override and each profile's own
    routing    the routing mode, the profile and the plan in use, the info pattern, and
               every plan with its groups and rules
    mesh       the mesh networks added in the app, without their secrets
    settings   the settings whose scope is ``synced``

Usage, cached downloads, what billing last said of a provider's subscriptions, local
settings, tokens (the providers' sessions among them), the WARP account and the mesh
networks' secrets stay out. Records keep their times. A deleted record is a tombstone, its
key and times without what it held, so a removed subscription's link does not live on in
the backups.

A restore either replaces the local data, as if the user had just made it so, or merges
it record by record: whichever side changed a record later wins, tombstones included. It
never leaves two providers at one origin, nor two profiles for one provider subscription
(``_left_out``), nor a provider profile without its provider (``_settle_providers``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import platform
import sqlite3
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from openweir import __version__
from openweir.host.base import MESH_KINDS
from openweir.models.database import Database
from openweir.models.profiles import KIND_FILE, KIND_PROVIDER, KIND_SUBSCRIPTION, ProfileError
from openweir.models.routing import GROUP_KINDS, MODES, RULE_KINDS
from openweir.models.settings import SYNCED, Settings, accepted, scope

FORMAT = "openweir-backup"
# A change of the document raises it; decode keeps reading every earlier version, and an
# app of an earlier version refuses it as newer_backup. 2 added the provider accounts.
VERSION = 2
ACCOUNTS_VERSION = 2  # the first version that holds provider accounts
REPLACE = "replace"  # the local data becomes the snapshot's
MERGE = "merge"  # record by record, the later change wins
RESTORE_MODES = (MERGE, REPLACE)
# A file holds a few configs of at most 15 MiB each (subscription.MAX_CONFIG_BYTES).
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
# Written as the snapshot's header, so they are no change of its content.
_HEADER = ("format", "version", "created_at", "device", "app_version")


class BackupError(ProfileError):
    """A file or a download is no snapshot this version can restore: ``not_a_backup``,
    ``newer_backup`` or ``too_large``; or a provider failed: ``not_signed_in``,
    ``backup_missing``, ``cloud_full``, ``cloud_unreachable``, ``cloud_failed``."""


@dataclass(frozen=True)
class Counts:
    profiles: int
    plans: int


@dataclass(frozen=True)
class SnapshotInfo:
    """What a list of snapshots shows of each."""

    created_at: float
    device: str
    app_version: str
    counts: Counts


@dataclass(frozen=True)
class Snapshot:
    document: dict[str, Any]

    @property
    def info(self) -> SnapshotInfo:
        document = self.document
        profiles = sum(not _deleted(p) for p in document["profiles"])
        plans = sum(not _deleted(p) for p in document["routing"]["plans"])
        return SnapshotInfo(
            document["created_at"],
            document["device"],
            document["app_version"],
            Counts(profiles, plans),
        )

    @property
    def empty(self) -> bool:
        """Whether it holds nothing the user made, as from a database just created."""
        document = self.document
        routing = document["routing"]
        records = (
            document["profiles"],
            _providers(document),
            document["overrides"],
            routing["plans"],
        )
        return not any(records) and not document["settings"] and routing["updated_at"] == 0

    @property
    def digest(self) -> str:
        """The hash of the content, without the header: an unchanged database gives the
        same digest whenever and wherever it is taken."""
        content = {k: v for k, v in self.document.items() if k not in _HEADER}
        return hashlib.sha256(_canonical(content).encode()).hexdigest()

    def encode(self) -> bytes:
        text = json.dumps(self.document, ensure_ascii=False, indent=2)
        return (text + "\n").encode()

    @staticmethod
    def decode(data: bytes) -> Snapshot:
        """The snapshot in ``data``; raises BackupError for anything else."""
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise BackupError("too_large", f"more than {MAX_SNAPSHOT_BYTES} bytes")
        try:
            document = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as error:
            raise BackupError("not_a_backup", str(error)) from error
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            raise BackupError("not_a_backup", f"format is not {FORMAT}")
        version = document.get("version")
        if type(version) is not int or version < 1:
            raise BackupError("not_a_backup", f"version {version!r}")
        if version > VERSION:
            raise BackupError("newer_backup", f"version {version} is newer than {VERSION}")
        _check_document(document)
        return Snapshot(document)


def device_name() -> str:
    """The name this machine goes by, as snapshots name where they come from."""
    return platform.node().removesuffix(".local") or "?"


class Backups:
    """Snapshots of a database and their restore; every call is one read or transaction."""

    def __init__(
        self, database: Database, *, device: str | None = None, app_version: str = __version__
    ) -> None:
        self._database = database
        self._device = device_name() if device is None else device
        self._app_version = app_version

    def snapshot(self) -> Snapshot:
        with _stored(), self._database.reading() as connection:
            return export(connection, self._database.clock(), self._device, self._app_version)

    def counts(self) -> Counts:
        """The profiles and plans the database holds now."""
        with _stored(), self._database.reading() as connection:
            return counts(connection)

    def restore(self, snapshot: Snapshot, mode: str) -> None:
        """Write the snapshot's data with ``mode``; the profiles, the routing and the
        settings read from the database are stale afterwards."""
        with _stored(), self._database.transaction() as connection:
            restore(connection, snapshot, mode, self._database.clock())


@contextmanager
def _stored() -> Iterator[None]:
    """Database failures as the store reports them (``storage_failed``)."""
    try:
        yield
    except sqlite3.Error as error:
        raise ProfileError("storage_failed", str(error)) from error


def write_file(path: Path, snapshot: Snapshot) -> None:
    """Write the snapshot to ``path`` whole or not at all."""
    partial = path.with_name(path.name + ".part")
    try:
        partial.write_bytes(snapshot.encode())
        partial.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()


def read_file(path: Path) -> Snapshot:
    """The snapshot in ``path``; OSError when it cannot be read, BackupError when it is no
    snapshot."""
    with path.open("rb") as file:
        data = file.read(MAX_SNAPSHOT_BYTES + 1)
    return Snapshot.decode(data)


# -- export ------------------------------------------------------------------------------------
def counts(connection: sqlite3.Connection) -> Counts:
    (profiles,) = connection.execute(
        "SELECT COUNT(*) FROM profiles WHERE deleted_at IS NULL"
    ).fetchone()
    (plans,) = connection.execute("SELECT COUNT(*) FROM plans WHERE deleted_at IS NULL").fetchone()
    return Counts(profiles, plans)


def export(connection: sqlite3.Connection, now: float, device: str, app_version: str) -> Snapshot:
    """The snapshot of the database as it is; records in a fixed order, so the same data
    always gives the same document."""
    state = connection.execute(
        "SELECT mode, profile_id, plan_id, info_pattern, updated_at FROM routing_state"
    ).fetchone()
    document = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": now,
        "device": device,
        "app_version": app_version,
        "profiles": _export_profiles(connection),
        # Without the session, which the credential store keeps (D56): a provider restored
        # elsewhere needs signing in again. ``user_id`` is what tells, at that sign-in,
        # whether it is the same account, whose profiles it takes up again by remote id,
        # or another one, which replaces them only once the user agrees (docs/accounts.md
        # §2); it is the provider's id of the user and says no more than ``account``.
        "providers": [
            _record(row, ("id", "origin", "name", "account", "user_id", "position", "created_at"))
            for row in connection.execute(
                "SELECT * FROM providers ORDER BY deleted_at IS NOT NULL, position, created_at, id"
            )
        ],
        "overrides": [
            _record(row, ("scope", "yaml"))
            for row in connection.execute("SELECT * FROM overrides ORDER BY scope")
        ],
        "routing": {**dict(state), "plans": _export_plans(connection)},
        "mesh": [
            _record(
                row,
                ("id", "kind", "name", "via", "position", "created_at"),
                settings=json.loads(row["settings"]),
            )
            for row in connection.execute(
                "SELECT * FROM mesh_networks "
                "ORDER BY deleted_at IS NOT NULL, position, created_at, id"
            )
        ],
        "settings": [
            {"key": key, "value": json.loads(value), "updated_at": updated_at}
            for key, value, updated_at in connection.execute(
                "SELECT key, value, updated_at FROM settings ORDER BY key"
            )
            if key in _SYNCED_SETTINGS
        ],
    }
    return Snapshot(document)


def _record(row: sqlite3.Row, columns: Sequence[str], **values: Any) -> dict[str, Any]:
    """A live row's columns and ``updated_at``, or the tombstone of a deleted one."""
    key = columns[0]
    if row["deleted_at"] is not None:
        return {key: row[key], "updated_at": row["updated_at"], "deleted_at": row["deleted_at"]}
    return {**{c: row[c] for c in columns}, **values, "updated_at": row["updated_at"]}


def _export_profiles(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    profiles = []
    for row in connection.execute(
        "SELECT p.*, c.yaml, c.updated_at AS config_updated_at FROM profiles AS p "
        "LEFT JOIN profile_contents AS c ON c.profile_id = p.id AND p.kind = ? "
        "ORDER BY p.deleted_at IS NOT NULL, p.position, p.created_at, p.id",
        (KIND_FILE,),
    ):
        extra: dict[str, Any] = {"in_pool": bool(row["in_pool"])}
        if row["kind"] == KIND_PROVIDER:
            # It has no link of its own (the provider's stays in memory, D57), and its
            # config is downloaded again through the account, as a subscription's is.
            source: tuple[str, ...] = ("provider_id", "remote_id")
        else:
            source = ("url", "path")
            if row["yaml"] is not None:  # a subscription's download stays here
                extra["config"] = {"yaml": row["yaml"], "updated_at": row["config_updated_at"]}
        columns = ("id", "name", "kind", *source, "auto_update_hours", "position", "created_at")
        profiles.append(_record(row, columns, **extra))
    return profiles


def _export_plans(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    plans = []
    for row in connection.execute(
        "SELECT * FROM plans ORDER BY deleted_at IS NOT NULL, position, created_at, id"
    ):
        if row["deleted_at"] is not None:  # its groups and rules went with it
            plans.append(_record(row, ("id",)))
            continue
        groups = [
            _record(
                group,
                ("id", "kind", "type", "name", "position"),
                conditions=json.loads(group["conditions"]),
                options=json.loads(group["options"]),
                hidden=bool(group["hidden"]),
            )
            for group in connection.execute(
                "SELECT * FROM plan_groups WHERE plan_id = ? ORDER BY position, id", (row["id"],)
            )
        ]
        rules = [
            _record(
                rule,
                ("id", "position", "kind", "target"),
                content=json.loads(rule["content"]),
                enabled=bool(rule["enabled"]),
            )
            for rule in connection.execute(
                "SELECT * FROM plan_rules WHERE plan_id = ? ORDER BY position, id", (row["id"],)
            )
        ]
        plan = _record(
            row,
            ("id", "name", "fallback", "position", "created_at"),
            egress=json.loads(row["egress"]),
        )
        plans.append({**plan, "groups": groups, "rules": rules})
    return plans


# -- reading a snapshot: every record checked before anything is written ------------------------
Check = Callable[[Any], bool]


def _text(value: Any) -> bool:
    return isinstance(value, str)


def _integer(value: Any) -> bool:
    return type(value) is int


def _flag(value: Any) -> bool:
    return type(value) is bool


def _time(value: Any) -> bool:
    return type(value) in (int, float) and value >= 0


def _any(_: Any) -> bool:
    return True


def _one_of(*choices: Any) -> Check:
    return lambda value: value in choices


def _optional(check: Check) -> Check:
    return lambda value: value is None or check(value)


_PROFILE: dict[str, Check] = {
    "id": _text,
    "name": _text,
    "kind": _text,  # one of _KINDS, KIND_PROVIDER only from ACCOUNTS_VERSION on
    "auto_update_hours": _integer,
    "in_pool": _flag,
    "position": _integer,
    "created_at": _time,
    "updated_at": _time,
}
_KINDS = (KIND_SUBSCRIPTION, KIND_FILE, KIND_PROVIDER)
# What a profile holds besides, by kind: a provider's names its provider and its subscription
# there, like Profile does, and carries no link, path or config.
_SOURCE: dict[str, Check] = {"url": _text, "path": _text}
_PROVIDER_SOURCE: dict[str, Check] = {
    "provider_id": lambda value: isinstance(value, str) and value != "",
    "remote_id": _integer,
}
_PROVIDER: dict[str, Check] = {
    "id": _text,
    "origin": _text,
    "name": _text,
    "account": _text,
    "user_id": _text,
    "position": _integer,
    "created_at": _time,
    "updated_at": _time,
}
_CONFIG: dict[str, Check] = {"yaml": _text, "updated_at": _time}
_OVERRIDE: dict[str, Check] = {"scope": _text, "yaml": _text, "updated_at": _time}
_ROUTING: dict[str, Check] = {
    "mode": _optional(_one_of(*MODES)),
    "profile_id": _optional(_text),
    "plan_id": _optional(_text),
    "info_pattern": _optional(_text),
    "updated_at": _time,
}
_PLAN: dict[str, Check] = {
    "id": _text,
    "name": _text,
    "fallback": _text,
    "egress": _any,
    "position": _integer,
    "created_at": _time,
    "updated_at": _time,
}
_GROUP: dict[str, Check] = {
    "id": _text,
    "kind": _one_of(*GROUP_KINDS),
    "type": _text,
    "name": _text,
    "conditions": _any,
    "options": _any,
    "hidden": _flag,
    "position": _integer,
    "updated_at": _time,
}
_RULE: dict[str, Check] = {
    "id": _text,
    "position": _integer,
    "enabled": _flag,
    "kind": _one_of(*RULE_KINDS),
    "content": _any,
    "target": _text,
    "updated_at": _time,
}
_NETWORK: dict[str, Check] = {
    "id": _text,
    "kind": _one_of(*MESH_KINDS),
    "name": _text,
    "via": _text,
    "settings": lambda value: isinstance(value, dict),
    "position": _integer,
    "created_at": _time,
    "updated_at": _time,
}
_SETTING: dict[str, Check] = {"key": _text, "value": _any, "updated_at": _time}
_HEADER_FIELDS: dict[str, Check] = {"created_at": _time, "device": _text, "app_version": _text}
_SYNCED_SETTINGS = frozenset(f.name for f in fields(Settings) if scope(f.name) == SYNCED)


def _deleted(record: Mapping[str, Any]) -> bool:
    return record.get("deleted_at") is not None


def _providers(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The snapshot's providers; none before ACCOUNTS_VERSION, whatever the document says."""
    return document["providers"] if document["version"] >= ACCOUNTS_VERSION else []


def _check(record: Any, checks: Mapping[str, Check], what: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise BackupError("not_a_backup", f"{what} is not an object")
    for name, check in checks.items():
        if name not in record or not check(record[name]):
            raise BackupError("not_a_backup", f"{what}: {name} {record.get(name)!r}")
    return record


def _records(
    items: Any, checks: Mapping[str, Check], what: str, key: str = "id"
) -> Iterable[dict[str, Any]]:
    """The records of a list, each checked: a tombstone for its key and times only."""
    if not isinstance(items, list):
        raise BackupError("not_a_backup", f"{what} is not a list")
    keys: set[Any] = set()
    for record in items:
        tombstone = isinstance(record, dict) and record.get("deleted_at") is not None
        if tombstone:
            _check(record, {key: checks[key], "updated_at": _time, "deleted_at": _time}, what)
        else:
            _check(record, checks, what)
        if record[key] in keys:
            raise BackupError("not_a_backup", f"{what}: {key} {record[key]!r} twice")
        keys.add(record[key])
        yield record


def _check_document(document: dict[str, Any]) -> None:
    _check(document, _HEADER_FIELDS, "the header")
    version = document["version"]
    kinds = _KINDS if version >= ACCOUNTS_VERSION else (KIND_SUBSCRIPTION, KIND_FILE)
    for profile in _records(document.get("profiles"), _PROFILE, "profiles"):
        if _deleted(profile):
            continue
        if profile["kind"] not in kinds:
            raise BackupError("not_a_backup", f"profiles: kind {profile['kind']!r}")
        if profile["kind"] != KIND_PROVIDER:
            _check(profile, _SOURCE, "profiles")
            if "config" in profile:
                _check(profile["config"], _CONFIG, "a profile's config")
            continue
        _check(profile, _PROVIDER_SOURCE, "profiles")
        # A link here would be a secret in the backup, and the profile could not be loaded.
        if any(name in profile for name in ("url", "path", "config")):
            raise BackupError("not_a_backup", "profiles: a provider's with a link or a config")
    if version >= ACCOUNTS_VERSION:
        for _ in _records(document.get("providers"), _PROVIDER, "providers"):
            pass
    for _ in _records(document.get("overrides"), _OVERRIDE, "overrides", key="scope"):
        pass
    routing = _check(document.get("routing"), _ROUTING, "routing")
    for plan in _records(routing.get("plans"), _PLAN, "plans"):
        if not _deleted(plan):
            for _ in _records(plan.get("groups"), _GROUP, "groups"):
                pass
            for _ in _records(plan.get("rules"), _RULE, "rules"):
                pass
    for _ in _records(document.get("mesh"), _NETWORK, "mesh"):
        pass
    for _ in _records(document.get("settings"), _SETTING, "settings", key="key"):
        pass


# -- restore -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Table:
    name: str
    key: tuple[str, ...]
    columns: tuple[str, ...]  # what a record holds besides its key, as stored
    created_at: bool = False  # set when the row is inserted, kept afterwards


_PROFILES = _Table(
    "profiles",
    ("id",),
    (
        "name",
        "kind",
        "url",
        "path",
        "provider_id",
        "remote_id",
        "auto_update_hours",
        "in_pool",
        "position",
    ),
    created_at=True,
)
_PROVIDERS = _Table(
    "providers", ("id",), ("origin", "name", "account", "user_id", "position"), created_at=True
)
_OVERRIDES = _Table("overrides", ("scope",), ("yaml",))
_PLANS = _Table("plans", ("id",), ("name", "fallback", "egress", "position"), created_at=True)
_GROUPS = _Table(
    "plan_groups",
    ("plan_id", "id"),
    ("kind", "type", "name", "conditions", "options", "hidden", "position"),
)
_RULES = _Table(
    "plan_rules", ("id",), ("plan_id", "position", "enabled", "kind", "content", "target")
)
_NETWORKS = _Table(
    "mesh_networks", ("id",), ("kind", "name", "via", "settings", "position"), created_at=True
)


def _canonical(value: Any) -> str:
    # Also the spelling routing.py stores JSON in, so a restored group or rule compares
    # equal to the one the routing page would write.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def restore(connection: sqlite3.Connection, snapshot: Snapshot, mode: str, now: float) -> None:
    """Write the snapshot's data into the database with ``mode``: REPLACE or MERGE.

    A snapshot older than ACCOUNTS_VERSION knows nothing of provider accounts, so even a
    replace leaves the providers, their profiles and those profiles' overrides as they are.
    A restored provider has no session here: it needs signing in again."""
    if mode not in RESTORE_MODES:
        raise ValueError(f"invalid restore mode: {mode!r}")
    document = snapshot.document
    accounts = document["version"] >= ACCOUNTS_VERSION
    profile_scope = override_scope = None
    if accounts:
        left = _left_out(connection, document, mode)
    else:
        left = _LeftOut()
        theirs = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM profiles WHERE kind = ?", (KIND_PROVIDER,)
            )
        }
        profile_scope, override_scope = _not_in("id", theirs), _not_in("scope", theirs)
    profiles = [
        _stored_profile(profile)
        for profile in document["profiles"]
        if profile["id"] not in left.profiles
    ]
    _restore_rows(connection, _PROFILES, profiles, mode, now, scope=profile_scope)
    _restore_configs(connection, document["profiles"], mode, now)
    overrides = [o for o in document["overrides"] if o["scope"] not in left.profiles]
    _restore_rows(connection, _OVERRIDES, overrides, mode, now, scope=override_scope)
    if accounts:
        providers = [p for p in _providers(document) if p["id"] not in left.providers]
        _restore_rows(connection, _PROVIDERS, providers, mode, now)
    routing = document["routing"]
    plans = [_stored_plan(plan) for plan in routing["plans"]]
    _restore_rows(connection, _PLANS, plans, mode, now)
    for plan in routing["plans"]:
        if _deleted(plan):
            continue
        scoped = _where("plan_id", plan["id"])
        groups = [_stored_group(plan["id"], group) for group in plan["groups"]]
        _restore_rows(connection, _GROUPS, groups, mode, now, scope=scoped)
        rules = [_stored_rule(plan["id"], rule) for rule in plan["rules"]]
        _restore_rows(connection, _RULES, rules, mode, now, scope=scoped)
    in_use = left.moved.get(routing["profile_id"], routing["profile_id"])
    _restore_routing_state(connection, {**routing, "profile_id": in_use}, mode, now)
    networks = [_stored_network(network) for network in document["mesh"]]
    _restore_rows(connection, _NETWORKS, networks, mode, now)
    _restore_settings(connection, document["settings"], mode, now)
    _settle_providers(connection, now)
    # What was downloaded for a profile no longer there, or what billing said of it, is a
    # cache of nothing.
    connection.execute(
        "DELETE FROM profile_contents WHERE profile_id NOT IN "
        "(SELECT id FROM profiles WHERE deleted_at IS NULL)"
    )
    connection.execute(
        "DELETE FROM provider_subscriptions WHERE profile_id NOT IN "
        "(SELECT id FROM profiles WHERE deleted_at IS NULL AND kind = ?)",
        (KIND_PROVIDER,),
    )


def _where(column: str, value: Any) -> Callable[[sqlite3.Row], bool]:
    return lambda row: row[column] == value


def _not_in(column: str, values: Collection[Any]) -> Callable[[sqlite3.Row], bool]:
    return lambda row: row[column] not in values


def _stored_profile(record: dict[str, Any]) -> dict[str, Any]:
    if _deleted(record):
        return record
    stored = {**record, "in_pool": int(record["in_pool"])}
    if record["kind"] == KIND_PROVIDER:
        return {**stored, "url": "", "path": ""}
    return {**stored, "provider_id": None, "remote_id": None}


# -- one provider per origin, one profile per subscription --------------------------------------
@dataclass(frozen=True)
class _LeftOut:
    """What of a snapshot a restore leaves out (``_left_out``)."""

    providers: frozenset[str] = frozenset()
    profiles: frozenset[str] = frozenset()
    # A profile left out, and the one here that stands for it: the same subscription of the
    # same account. The profile in use follows.
    moved: Mapping[str, str] = field(default_factory=dict)


def _left_out(connection: sqlite3.Connection, document: Mapping[str, Any], mode: str) -> _LeftOut:
    """What of the snapshot to leave out so that once it is restored, one provider at most
    is signed in at an origin (D55) and one profile at most stands for a provider's
    subscription.

    Two devices that each signed in to an origin on their own have a provider each, with
    profiles of their own for the same subscriptions, and merging one's snapshot into the
    other would give two of each. The provider signed in here stays, and the snapshot's is
    left out with its profiles and their overrides: this device keeps its session, which the
    credential store holds by the provider's id, and nothing of either is marked deleted, so
    the other device keeps its own as well when it merges this one's snapshot in turn. The
    two go on with a provider each until one of them replaces its data or signs out. Where
    neither was signed in here before (a replace, or a snapshot that held two), the first in
    the snapshot's order stays. Two profiles for one subscription of one provider (each
    device added one when it came up) are settled the same way."""
    providers_here = _rows(connection, "providers")
    providers = _live_after(providers_here, _providers(document), mode)
    replaced: dict[str, str] = {}  # a provider left out, and the one at its origin that stays
    for ids in _clashes(providers, ("origin",)):
        stays = _staying(ids, providers, providers_here)
        replaced.update((other, stays) for other in ids if other != stays)
    of_replaced = [
        record
        for record in document["profiles"]
        if not _deleted(record)
        and record["kind"] == KIND_PROVIDER
        and record["provider_id"] in replaced
    ]
    left = {record["id"] for record in of_replaced}
    records = [record for record in document["profiles"] if record["id"] not in left]
    profiles_here = _rows(connection, "profiles")
    profiles = {
        key: values
        for key, values in _live_after(profiles_here, records, mode).items()
        if values["kind"] == KIND_PROVIDER
    }
    moved: dict[str, str] = {}
    standing: dict[tuple[str, int], str] = {}  # the profile that stays for a subscription
    for key, values in profiles.items():
        standing[(values["provider_id"], values["remote_id"])] = key
    for ids in _clashes(profiles, ("provider_id", "remote_id")):
        stays = _staying(ids, profiles, profiles_here)
        standing[(profiles[stays]["provider_id"], profiles[stays]["remote_id"])] = stays
        moved.update((other, stays) for other in ids if other != stays)
    left |= moved.keys()
    for record in of_replaced:
        stays = replaced[record["provider_id"]]
        user = providers[record["provider_id"]]["user_id"]
        if user and user == providers[stays]["user_id"]:
            here = standing.get((stays, record["remote_id"]))
            if here is not None:
                moved[record["id"]] = here
    return _LeftOut(frozenset(replaced), frozenset(left), moved)


def _rows(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {row["id"]: row for row in connection.execute(f"SELECT * FROM {table}")}


def _live_after(
    stored: Mapping[str, sqlite3.Row], records: Iterable[Mapping[str, Any]], mode: str
) -> dict[str, Any]:
    """By id, the values of each record or row that is live once ``records`` are restored
    with ``mode``, as _restore_rows decides: the record's where it wins, else the row's."""
    incoming = {record["id"]: record for record in records}
    live: dict[str, Any] = {}
    for key in stored.keys() | incoming.keys():
        row, record = stored.get(key), incoming.get(key)
        values: Any = None
        if mode == REPLACE:
            if record is not None and not _deleted(record):
                values = record
        elif record is not None and (row is None or row["updated_at"] < record["updated_at"]):
            if not _deleted(record):
                values = record
        elif row is not None and row["deleted_at"] is None:
            values = row
        if values is not None:
            live[key] = values
    return live


def _clashes(live: Mapping[str, Any], columns: tuple[str, ...]) -> list[list[str]]:
    """The ids of ``live`` that share the values of ``columns``, where more than one does."""
    groups: dict[tuple[Any, ...], list[str]] = {}
    for key, values in live.items():
        groups.setdefault(tuple(values[column] for column in columns), []).append(key)
    return [keys for keys in groups.values() if len(keys) > 1]


def _staying(ids: Iterable[str], live: Mapping[str, Any], stored: Mapping[str, sqlite3.Row]) -> str:
    """Of the ids that clash, the one live here before the restore, else the first in order."""

    def order(key: str) -> tuple[bool, int, float, str]:
        row = stored.get(key)
        values = live[key]
        here = row is not None and row["deleted_at"] is None
        return (not here, values["position"], values["created_at"], key)

    return min(ids, key=order)


def _settle_providers(connection: sqlite3.Connection, now: float) -> None:
    """Mark deleted, with their overrides, the provider profiles whose provider is not
    signed in here once restored: a merge took another device's sign-out, which is later
    than this provider's last change here, but a profile of it changed here later still.
    Signing out removes the provider's profiles (docs/accounts.md §4.4), and so does this."""
    orphans = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM profiles WHERE kind = ? AND deleted_at IS NULL AND provider_id "
            "NOT IN (SELECT id FROM providers WHERE deleted_at IS NULL)",
            (KIND_PROVIDER,),
        )
    ]
    for table, key in (("profiles", "id"), ("overrides", "scope")):
        connection.executemany(
            f"UPDATE {table} SET deleted_at = ?, updated_at = ? "
            f"WHERE {key} = ? AND deleted_at IS NULL",
            [(now, now, orphan) for orphan in orphans],
        )


def _stored_plan(record: dict[str, Any]) -> dict[str, Any]:
    if _deleted(record):
        return record
    return {**record, "egress": _canonical(record["egress"])}


def _stored_group(plan_id: str, record: dict[str, Any]) -> dict[str, Any]:
    if _deleted(record):
        return {**record, "plan_id": plan_id}
    converted = {
        "conditions": _canonical(record["conditions"]),
        "options": _canonical(record["options"]),
        "hidden": int(record["hidden"]),
    }
    return {**record, **converted, "plan_id": plan_id}


def _stored_network(record: dict[str, Any]) -> dict[str, Any]:
    if _deleted(record):
        return record
    return {**record, "settings": _canonical(record["settings"])}


def _stored_rule(plan_id: str, record: dict[str, Any]) -> dict[str, Any]:
    if _deleted(record):
        return record
    converted = {"content": _canonical(record["content"]), "enabled": int(record["enabled"])}
    return {**record, **converted, "plan_id": plan_id}


def _restore_rows(
    connection: sqlite3.Connection,
    table: _Table,
    records: Sequence[Mapping[str, Any]],
    mode: str,
    now: float,
    *,
    scope: Callable[[sqlite3.Row], bool] | None = None,
) -> None:
    """Merge or replace the rows of ``table`` with ``records``; ``scope`` limits the rows a
    replace marks deleted to those it accepts, such as one plan's groups. A record finds its
    row by key wherever it is, so a rule may move to another plan.

    A merge writes a record that is new here or changed later than the row, with its own
    times; a tombstone marks the row deleted, and is dropped when there is no row. A replace
    inserts the records new here with their times, and stamps ``now`` on the rows it
    changes or marks deleted."""
    key_match = " AND ".join(f"{column} = ?" for column in table.key)
    stored = {
        tuple(row[column] for column in table.key): row
        for row in connection.execute(f"SELECT * FROM {table.name}")
    }
    live: set[tuple[Any, ...]] = set()
    for record in records:
        key = tuple(record[column] for column in table.key)
        row = stored.get(key)
        if mode == MERGE:
            if row is not None and row["updated_at"] >= record["updated_at"]:
                continue
            if _deleted(record):
                if row is not None:
                    connection.execute(
                        f"UPDATE {table.name} SET deleted_at = ?, updated_at = ? WHERE {key_match}",
                        (record["deleted_at"], record["updated_at"], *key),
                    )
                continue
            _write_row(connection, table, record, record["updated_at"], exists=row is not None)
            continue
        if _deleted(record):
            continue  # marked deleted below, if it is here
        live.add(key)
        if row is None:
            _write_row(connection, table, record, record["updated_at"], exists=False)
        elif row["deleted_at"] is not None or any(
            row[column] != record[column] for column in table.columns
        ):
            _write_row(connection, table, record, now, exists=True)
    if mode == REPLACE:
        for key, row in stored.items():
            in_scope = scope is None or scope(row)
            if in_scope and key not in live and row["deleted_at"] is None:
                connection.execute(
                    f"UPDATE {table.name} SET deleted_at = ?, updated_at = ? WHERE {key_match}",
                    (now, now, *key),
                )


def _write_row(
    connection: sqlite3.Connection,
    table: _Table,
    record: Mapping[str, Any],
    updated_at: float,
    *,
    exists: bool,
) -> None:
    if exists:
        assigned = ", ".join(f"{column} = ?" for column in table.columns)
        key_match = " AND ".join(f"{column} = ?" for column in table.key)
        connection.execute(
            f"UPDATE {table.name} SET {assigned}, updated_at = ?, deleted_at = NULL "
            f"WHERE {key_match}",
            (
                *(record[c] for c in table.columns),
                updated_at,
                *(record[c] for c in table.key),
            ),
        )
        return
    columns = (*table.key, *table.columns, *(("created_at",) if table.created_at else ()))
    inserted = ", ".join((*columns, "updated_at"))
    placeholders = ", ".join("?" * (len(columns) + 1))
    connection.execute(
        f"INSERT INTO {table.name} ({inserted}) VALUES ({placeholders})",
        (*(record[c] for c in columns), updated_at),
    )


def _restore_configs(
    connection: sqlite3.Connection, profiles: Sequence[Mapping[str, Any]], mode: str, now: float
) -> None:
    """The configs of the local files, as their own records: a merge keeps the one read
    later. A config the database has but the snapshot lacks stays until its profile goes."""
    live_files = {
        row[0]
        for row in connection.execute(
            "SELECT id FROM profiles WHERE kind = ? AND deleted_at IS NULL", (KIND_FILE,)
        )
    }
    for profile in profiles:
        config = profile.get("config")
        if config is None or _deleted(profile) or profile["id"] not in live_files:
            continue
        row = connection.execute(
            "SELECT yaml, updated_at FROM profile_contents WHERE profile_id = ?", (profile["id"],)
        ).fetchone()
        if row is None:
            updated_at = config["updated_at"]
        elif mode == MERGE:
            if row["updated_at"] >= config["updated_at"]:
                continue
            updated_at = config["updated_at"]
        elif row["yaml"] == config["yaml"]:
            continue
        else:
            updated_at = now
        connection.execute(
            "INSERT INTO profile_contents (profile_id, yaml, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (profile_id) DO UPDATE SET yaml = excluded.yaml, "
            "updated_at = excluded.updated_at",
            (profile["id"], config["yaml"], updated_at),
        )
        if not profile["path"]:
            # Pasted node batches carry no trusted inspection in the snapshot.
            # Recheck every URI before the restored batch may join a live pool.
            connection.execute(
                "UPDATE profile_contents SET unread = 1, proxy_count = NULL, "
                "has_rules = 0, read_yaml = NULL WHERE profile_id = ?",
                (profile["id"],),
            )


def _restore_routing_state(
    connection: sqlite3.Connection, routing: Mapping[str, Any], mode: str, now: float
) -> None:
    columns = ("mode", "profile_id", "plan_id", "info_pattern")
    selected = ", ".join(columns)
    row = connection.execute(f"SELECT {selected}, updated_at FROM routing_state").fetchone()
    if mode == MERGE:
        if row["updated_at"] >= routing["updated_at"]:
            return
        updated_at = routing["updated_at"]
    elif all(row[column] == routing[column] for column in columns):
        # This local fallback belongs to the previous library, even when the selected
        # mode and IDs happen to match the replacement snapshot.
        connection.execute("UPDATE routing_state SET resume_profile = 0")
        return
    else:
        # A database that never chose its routing takes the snapshot's as it was.
        updated_at = routing["updated_at"] if row["updated_at"] == 0 else now
    assigned = ", ".join(f"{column} = ?" for column in columns)
    connection.execute(
        f"UPDATE routing_state SET {assigned}, resume_profile = 0, updated_at = ?",
        (*(routing[column] for column in columns), updated_at),
    )


def _restore_settings(
    connection: sqlite3.Connection, records: Sequence[Mapping[str, Any]], mode: str, now: float
) -> None:
    """The synced settings: a merge takes the ones changed later; a replace sets every one
    to the snapshot's value, or to its default when the snapshot has none. A value this
    version does not accept counts as none."""
    incoming = {
        r["key"]: r
        for r in records
        if r["key"] in _SYNCED_SETTINGS and accepted({r["key"]: r["value"]})
    }
    stored = {
        key: (value, updated_at)
        for key, value, updated_at in connection.execute(
            "SELECT key, value, updated_at FROM settings"
        )
    }
    defaults = Settings()
    for name in sorted(_SYNCED_SETTINGS):
        record = incoming.get(name)
        old = stored.get(name)
        if mode == MERGE:
            if record is None or (old is not None and old[1] >= record["updated_at"]):
                continue
            value, updated_at = record["value"], record["updated_at"]
        else:
            value = record["value"] if record else getattr(defaults, name)
            if old is None:
                if record is None:
                    continue  # the default already
                updated_at = record["updated_at"]
            else:
                with contextlib.suppress(ValueError):
                    if json.loads(old[0]) == value:
                        continue
                updated_at = now
        connection.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (name, json.dumps(value), updated_at),
        )
