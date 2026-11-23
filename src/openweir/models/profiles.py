"""Profiles: configs the user imported from a subscription link or a local file, or that a
provider account brought in (docs/accounts.md §5)."""

from __future__ import annotations

from dataclasses import dataclass, replace

KIND_SUBSCRIPTION = "subscription"
KIND_FILE = "file"
# A subscription of a provider account (docs/accounts.md §5.1): it names the provider and the
# subscription there, and has no link of its own. It updates through the account, and goes
# when the account signs out.
KIND_PROVIDER = "provider"


class ProfileError(Exception):
    """A profile could not be downloaded, read or stored. Codes share the HostError
    namespace so the UI turns both into text the same way."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Usage:
    """The ``Subscription-Userinfo`` response header."""

    upload: int
    download: int
    total: int  # 0 when the subscription has no quota
    expire: int  # unix seconds; 0 when it does not expire

    @property
    def used(self) -> int:
        return self.upload + self.download


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    kind: str  # KIND_SUBSCRIPTION, KIND_FILE (including pasted node links) or KIND_PROVIDER
    # The subscription link. Empty for a provider's subscription: its link, if it has one, stays
    # in memory (D57).
    url: str = ""
    path: str = ""  # the original local file; empty for pasted node links
    # Subscriptions and provider profiles only (can_auto_update): n > 0 updates every n hours;
    # 0 is off; -n is off, keeping the n hours it updates at once turned on again. Versions
    # before -n read any value <= 0 as off.
    auto_update_hours: int = 0
    updated_at: float = 0.0  # unix seconds of the last download or import
    usage: Usage | None = None
    # The nodes it brings to the node pool (SourceInspection.node_count); None until read.
    proxy_count: int | None = None
    has_override: bool = False  # it has an override of its own; read from the overrides
    in_pool: bool = True  # its nodes join the node pool of the user's plans (routing.md §5.2)
    # Whether the stored config brings rules of its own; None until a core read it.
    has_rules: bool | None = None
    # Stored while no core could read it (macOS, tunnel down; D27): the core reads it at the
    # next connect. Until then it has no node count and may or may not bring rules.
    unread: bool = False
    website: str = ""
    support: str = ""
    # Provider profiles only, None for the others: the ProviderRecord's id, and the
    # subscription's id in that account (RemoteSubscription.id).
    provider_id: str | None = None
    remote_id: int | None = None

    def __post_init__(self) -> None:
        if self.kind == KIND_PROVIDER:
            if not self.provider_id or type(self.remote_id) is not int:
                raise ValueError("a provider profile names its provider and subscription")
            if self.url or self.path:
                raise ValueError("a provider profile has no link or file of its own")
        elif self.provider_id is not None or self.remote_id is not None:
            raise ValueError("only a provider profile names a provider")

    @property
    def is_subscription(self) -> bool:
        """It downloads from its link; a provider's subscription is no such subscription."""
        return self.kind == KIND_SUBSCRIPTION

    @property
    def is_provider(self) -> bool:
        return self.kind == KIND_PROVIDER

    @property
    def auto_update_interval(self) -> int:
        """The hours it updates at, or would once automatic updates are on again; 0 when it
        keeps none, and the default applies."""
        return abs(self.auto_update_hours)

    @property
    def is_node_links(self) -> bool:
        return self.kind == KIND_FILE and not self.path

    @property
    def can_update(self) -> bool:
        """It has a source to update from: its link, its provider account or its file. Pasted
        node links have none."""
        return self.is_subscription or self.is_provider or bool(self.path)

    @property
    def can_auto_update(self) -> bool:
        """It may update by itself at an interval: what it updates from is on the network."""
        return self.is_subscription or self.is_provider


@dataclass(frozen=True)
class ProfileList:
    profiles: tuple[Profile, ...] = ()
    active: str | None = None  # the id of the profile the tunnel uses
    update_failed: frozenset[str] = frozenset()  # ids whose last update failed; not stored
    has_global_override: bool = False  # there is a global override; read from the overrides

    def get(self, profile_id: str) -> Profile:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        raise ProfileError("unknown_profile", f"no profile {profile_id}")

    def active_profile(self) -> Profile | None:
        return next((p for p in self.profiles if p.id == self.active), None)

    def put(self, profile: Profile) -> ProfileList:
        """Replace the profile with the same id, or append it."""
        if any(p.id == profile.id for p in self.profiles):
            profiles = tuple(profile if p.id == profile.id else p for p in self.profiles)
        else:
            profiles = (*self.profiles, profile)
        return replace(self, profiles=profiles)

    def without(self, profile_id: str) -> ProfileList:
        profiles = tuple(p for p in self.profiles if p.id != profile_id)
        active = self.active
        if active == profile_id:
            active = profiles[0].id if profiles else None
        return replace(
            self, profiles=profiles, active=active, update_failed=self.update_failed - {profile_id}
        )
