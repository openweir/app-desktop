"""Provider accounts in the local database (docs/accounts.md §5.1, docs/storage.md §2).

    providers                one row per provider signed in to here, in order; signing out
                             marks it deleted, for a backup to merge
    provider_subscriptions   a provider profile's subscription as billing last described it
                             (RemoteSubscription.record() as JSON); a cache of this machine

The profiles themselves, of kind ``provider`` with their ``provider_id`` and ``remote_id``,
are the profile store's (models/store.py), which also drops a profile's cache row with its
config. The session token is in the credential store (credentials.ProviderTokens) and a
subscription's link in memory only: neither ever reaches these tables.

Each method is one transaction on the shared connection (models/database.py). A failing
statement raises ProfileError ``storage_failed``, as the profile store does.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from openweir.models.database import Database
from openweir.models.profiles import KIND_PROVIDER, ProfileError
from openweir.models.provider.types import (
    PROVIDER_ACCOUNT_CONFLICT,
    PROVIDER_UNKNOWN,
    ProviderError,
    ProviderRecord,
    RemoteSubscription,
)

_COLUMNS = "id, origin, name, account, user_id, position, created_at, updated_at"


def new_provider_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class StoredSubscription:
    """A provider profile's row of ``provider_subscriptions``."""

    profile_id: str
    subscription: RemoteSubscription  # without links: they are never stored
    synced_at: float  # unix seconds of the billing answer it came from


class ProviderStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    # -- providers ---------------------------------------------------------------------------
    def providers(self) -> tuple[ProviderRecord, ...]:
        """The providers signed in to here, in their order, without the deleted ones."""
        with _stored(), self._database.reading() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM providers WHERE deleted_at IS NULL "
                "ORDER BY position, created_at, id"
            ).fetchall()
        return tuple(_record(row) for row in rows)

    def provider(self, provider_id: str) -> ProviderRecord:
        """PROVIDER_UNKNOWN when there is none, or it was signed out of."""
        with _stored(), self._database.reading() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM providers WHERE id = ? AND deleted_at IS NULL",
                (provider_id,),
            ).fetchone()
        if row is None:
            raise ProviderError(PROVIDER_UNKNOWN, f"no provider {provider_id}")
        return _record(row)

    def by_origin(self, origin: str) -> ProviderRecord | None:
        """The provider at ``origin`` signed in to here, if any: there is one at most (D55)."""
        with _stored(), self._database.reading() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM providers WHERE origin = ? AND deleted_at IS NULL "
                "ORDER BY position, created_at, id LIMIT 1",
                (origin,),
            ).fetchone()
        return None if row is None else _record(row)

    def add(self, origin: str, name: str, account: str, user_id: str) -> ProviderRecord:
        """A new provider with an id of its own, after the others. PROVIDER_ACCOUNT_CONFLICT
        while one at ``origin`` is signed in here already: one account per origin (D55), so
        signing in to another replaces that one, which signs it out first."""
        record = ProviderRecord(new_provider_id(), origin, name, account, user_id)
        with _stored(), self._database.transaction() as connection:
            taken = connection.execute(
                "SELECT 1 FROM providers WHERE origin = ? AND deleted_at IS NULL", (origin,)
            ).fetchone()
            if taken is not None:
                raise ProviderError(
                    PROVIDER_ACCOUNT_CONFLICT, "a provider at this origin is signed in here"
                )
            now = self._database.clock()
            (position,) = connection.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) FROM providers"
            ).fetchone()
            connection.execute(
                f"INSERT INTO providers ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (record.id, origin, name, account, user_id, position, now, now),
            )
        return ProviderRecord(record.id, origin, name, account, user_id, position, now, now)

    def update(self, record: ProviderRecord) -> ProviderRecord:
        """Store the record's name, account, user id and position; ``updated_at`` changes only
        when one of them does. The id and origin are the provider's for good, and its times
        the store's. PROVIDER_UNKNOWN for a provider signed out of meanwhile, which stays so."""
        values = (record.name, record.account, record.user_id, record.position)
        with _stored(), self._database.transaction() as connection:
            row = connection.execute(
                "SELECT name, account, user_id, position FROM providers "
                "WHERE id = ? AND deleted_at IS NULL",
                (record.id,),
            ).fetchone()
            if row is None:
                raise ProviderError(PROVIDER_UNKNOWN, f"no provider {record.id}")
            if tuple(row) != values:
                connection.execute(
                    "UPDATE providers SET name = ?, account = ?, user_id = ?, position = ?, "
                    "updated_at = ? WHERE id = ?",
                    (*values, self._database.clock(), record.id),
                )
            stored = connection.execute(
                f"SELECT {_COLUMNS} FROM providers WHERE id = ?", (record.id,)
            ).fetchone()
        return _record(stored)

    def delete(self, provider_id: str) -> None:
        """Mark the provider deleted, for backups, and drop what billing said of its
        profiles. Its profiles are the library's to remove, before this."""
        with _stored(), self._database.transaction() as connection:
            now = self._database.clock()
            connection.execute(
                "UPDATE providers SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (now, now, provider_id),
            )
            connection.execute(
                "DELETE FROM provider_subscriptions WHERE profile_id IN "
                "(SELECT id FROM profiles WHERE provider_id = ?)",
                (provider_id,),
            )

    # -- what billing said of each profile ----------------------------------------------------
    def subscriptions(self, provider_id: str) -> tuple[StoredSubscription, ...]:
        """What the last sync stored for the provider's profiles, by remote id. A row this
        version cannot read is left out: the next sync writes it again."""
        with _stored(), self._database.reading() as connection:
            rows = connection.execute(
                "SELECT s.profile_id, s.data, s.synced_at FROM provider_subscriptions AS s "
                "JOIN profiles AS p ON p.id = s.profile_id "
                "WHERE p.provider_id = ? AND p.kind = ? AND p.deleted_at IS NULL "
                "ORDER BY p.remote_id",
                (provider_id, KIND_PROVIDER),
            ).fetchall()
        stored = []
        for row in rows:
            try:
                data = json.loads(row["data"])
                if not isinstance(data, dict):
                    continue
                subscription = RemoteSubscription.from_record(data)
            except ValueError, TypeError:
                continue
            stored.append(StoredSubscription(row["profile_id"], subscription, row["synced_at"]))
        return tuple(stored)

    def save_subscriptions(self, subscriptions: Mapping[str, RemoteSubscription]) -> int:
        """Store each subscription, without its links, for the profile id it is under, dated
        now. Only a provider profile that is not deleted and has the subscription's id as its
        remote id takes one, so save the profiles first; the others (such as one removed
        meanwhile by signing out) are skipped. Returns how many were stored."""
        with _stored(), self._database.transaction() as connection:
            now = self._database.clock()
            written = 0
            for profile_id, subscription in subscriptions.items():
                data = json.dumps(subscription.record(), ensure_ascii=False, separators=(",", ":"))
                written += connection.execute(
                    "INSERT INTO provider_subscriptions (profile_id, data, synced_at) "
                    "SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM profiles WHERE id = ? "
                    "AND kind = ? AND remote_id = ? AND deleted_at IS NULL) "
                    "ON CONFLICT (profile_id) DO UPDATE SET data = excluded.data, "
                    "synced_at = excluded.synced_at",
                    (profile_id, data, now, profile_id, KIND_PROVIDER, subscription.id),
                ).rowcount
        return written


@contextmanager
def _stored() -> Iterator[None]:
    try:
        yield
    except sqlite3.Error as error:
        raise ProfileError("storage_failed", str(error)) from error


def _record(row: sqlite3.Row) -> ProviderRecord:
    return ProviderRecord(
        id=row["id"],
        origin=row["origin"],
        name=row["name"],
        account=row["account"],
        user_id=row["user_id"],
        position=row["position"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
