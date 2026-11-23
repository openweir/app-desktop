"""A zip of what a bug report needs (docs/release.md §4): versions, the platform, the host's
GetInfo and GetStatus, the core's recent warnings and errors and the settings without secrets.
On Linux, also the distribution and its version, the desktop, the session type and the
package format, by allow list.

What leaves the machine is chosen by allow lists, never by removing what looks secret: a
profile contributes counts and flags, not its name, link, path or text; an override only
whether it exists. Every setting is listed as exported or withheld, and a test fails when
a new one is neither, so adding a setting means deciding. A provider account contributes
whether it is signed in, and its profiles their access mode and status from billing
(docs/accounts.md §8), never the provider's address or name, the account, the user id, the
token or a link.

The core's log is the exception, free text that cannot be listed field by field, so it is
cut down instead: no info or debug lines, which name every site visited, and no URLs. A
provider's failed fetch prints the subscription's URL with its token (Go's url.Error), and
share links carry a node's credentials; every URL, whatever its scheme, is written as
``scheme://<redacted>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import re
import sys
import zipfile
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import PySide6
from PySide6.QtCore import qVersion

from openweir import __version__
from openweir.desktop.login import PACKAGED
from openweir.host.base import HostError, HostInfo, HostStatus, LogEntry, TunnelHost
from openweir.models.credentials import CredentialError, ProviderTokens
from openweir.models.library import Routing
from openweir.models.logs import export_text, rank
from openweir.models.profiles import Profile, ProfileError, ProfileList
from openweir.models.provider.store import ProviderStore
from openweir.models.provider.types import (
    ACCESS_APP,
    ACCESS_BOTH,
    ACCESS_LINK,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_EXPIRED,
    ProviderState,
    RemoteSubscription,
)
from openweir.models.settings import Settings
from openweir.update.releases import PACKAGE_FORMATS, installed_package_format

# Settings written as they are, and settings only said to be set or not.
SETTINGS_EXPORTED = frozenset(
    {
        "language",
        "appearance",
        "close_action",
        "launch_at_login",
        "tun",
        "system_proxy",
        "mixed_port",
        "allow_lan",
        "tun_stack",
        "include_all_networks",
        "warp_protocol",
        "delay_test",
        "update_check",
        "update_check_hours",
    }
)
SETTINGS_WITHHELD: frozenset[str] = frozenset()

HOST_TIMEOUT_S = 5.0

LOG_LEVEL = "warning"  # the lowest level of the core's lines in mihomo.log
# A scheme, then everything up to a space, a quote or an angle bracket, less the punctuation
# that ends a sentence or a Go error ("Get \"https://…\": EOF").
_URL = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*)://[^\s\"'<>]*[^\s\"'<>.,:;!?)\]}]")
LOG_NOTE = "# Warnings and errors only; every URL is written as scheme://<redacted>.\n"
# The values of billing's access mode and status written as they are; billing's text is the
# provider's, so any other is written as "other".
ACCESS_MODES = frozenset({ACCESS_LINK, ACCESS_BOTH, ACCESS_APP})
STATUSES = frozenset({STATUS_ACTIVE, STATUS_DISABLED, STATUS_EXPIRED})


# Linux desktops by the names XDG_CURRENT_DESKTOP gives them: the menu specification's
# registered environments and others common there. Any other name is written as "other".
DESKTOPS = frozenset(
    {
        "Budgie",
        "Cinnamon",
        "COSMIC",
        "DDE",
        "Deepin",
        "EDE",
        "Endless",
        "Enlightenment",
        "GNOME",
        "GNOME-Classic",
        "GNOME-Flashback",
        "Hyprland",
        "i3",
        "KDE",
        "LXDE",
        "LXQt",
        "MATE",
        "niri",
        "Pantheon",
        "pop",
        "Razor",
        "ROX",
        "sway",
        "TDE",
        "UKUI",
        "ubuntu",
        "Unity",
        "wlroots",
        "X-Cinnamon",
        "XFCE",
    }
)
SESSION_TYPES = frozenset({"x11", "wayland", "tty", "mir", "unspecified"})
OS_RELEASE = (Path("/etc/os-release"), Path("/usr/lib/os-release"))  # the first one there
# os-release's ID and VERSION_ID hold lower-case letters, digits, ".", "_" and "-"; anything
# else is no value os-release gives, and is written as "other".
_OS_RELEASE_VALUE = re.compile(r"[a-z0-9._-]{1,32}")


@dataclass(frozen=True)
class Accounts:
    """The provider accounts as the archive tells of them (read_accounts)."""

    states: tuple[ProviderState, ...] = ()  # each provider's, in their order
    # What billing last said of each provider profile, by profile id. Only its access mode,
    # status and whether it is usable are written.
    billing: Mapping[str, RemoteSubscription] = field(default_factory=dict)
    error: str = ""  # the code of a failure to read the database; then nothing else is known


@dataclass(frozen=True)
class Snapshot:
    """What the archive is written from, gathered on the asyncio loop."""

    info: HostInfo | HostError
    status: HostStatus | HostError
    settings: Settings
    profiles: ProfileList
    routing: Routing
    logs: Sequence[LogEntry]
    language: str  # the UI's, "zh" or "en"
    crashes: dict[str, Any] = field(default_factory=dict)  # metadata only, never raw reports
    accounts: Accounts | None = None  # None when they were not read


def read_accounts(store: ProviderStore, tokens: ProviderTokens) -> Accounts:
    """Each provider's state, from whether the credential store holds a session for it, and
    what billing last said of its profiles. Blocking: call it on a worker thread. The token
    read goes no further than telling it is there; a failure to read the credential store
    is the state it gives (credentials_unavailable), and of the database only its code."""
    try:
        providers = store.providers()
        states = tuple(_state(tokens, provider.id) for provider in providers)
        billing = {
            stored.profile_id: stored.subscription
            for provider in providers
            for stored in store.subscriptions(provider.id)
        }
    except ProfileError as error:
        return Accounts(error=error.code)
    return Accounts(states, billing)


def _state(tokens: ProviderTokens, provider_id: str) -> ProviderState:
    try:
        token = tokens.load(provider_id)
    except CredentialError:
        return ProviderState.CREDENTIALS_UNAVAILABLE
    return ProviderState.SIGN_IN_REQUIRED if token is None else ProviderState.SIGNED_IN


def archive_name(now: float) -> str:
    stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    return f"openweir-diagnostics-{stamp}.zip"


async def _ask[T](call: Awaitable[T]) -> T | HostError:
    try:
        return await asyncio.wait_for(call, HOST_TIMEOUT_S)
    except HostError as error:
        return error
    except TimeoutError:
        return HostError("timeout", f"no answer in {HOST_TIMEOUT_S:g} s")


async def ask_host(host: TunnelHost) -> tuple[HostInfo | HostError, HostStatus | HostError]:
    """GetInfo and GetStatus, or why the host did not answer."""
    return await _ask(host.get_info()), await _ask(host.get_status())


def about(language: str, now: float) -> dict[str, Any]:
    written: dict[str, Any] = {
        "version": __version__,
        "packaged": PACKAGED,
        "python": platform.python_version(),
        "pyside6": PySide6.__version__,
        "qt": qVersion(),
        "system": sys.platform,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "language": language,
        "exported_at": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"),
    }
    if sys.platform == "linux":
        written["linux"] = linux_about()
    return written


def linux_about(
    environ: Mapping[str, str] = os.environ,
    os_release_paths: Sequence[Path] = OS_RELEASE,
    package_format: str | None = None,
) -> dict[str, Any]:
    """The distribution, the desktop and how OpenWeir came onto it: what differs between
    Linux machines where a bug shows. ``package_format`` is read from the marker by
    default."""
    desktops = environ.get("XDG_CURRENT_DESKTOP", "").split(":")
    if package_format is None:
        package_format = installed_package_format()
    return {
        "os_release": os_release(os_release_paths),
        "desktop": [_known(name, DESKTOPS) for name in desktops if name],
        "session_type": _known(environ.get("XDG_SESSION_TYPE", ""), SESSION_TYPES),
        "package_format": _known(package_format or "", frozenset(PACKAGE_FORMATS)),
    }


def os_release(paths: Sequence[Path] = OS_RELEASE) -> dict[str, str | None]:
    """os-release's ID and VERSION_ID, such as ``ubuntu`` and ``24.04``; nothing else of it,
    such as a PRETTY_NAME or URLs that a vendor wrote."""
    for path in paths:
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            continue
        values: dict[str, str] = {}
        for line in text.splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key in ("ID", "VERSION_ID"):
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
        return {
            "id": _release_value(values.get("ID", "")),
            "version_id": _release_value(values.get("VERSION_ID", "")),
        }
    return {"id": None, "version_id": None}


def _release_value(value: str) -> str | None:
    if not value:
        return None
    return value if _OS_RELEASE_VALUE.fullmatch(value) else "other"


def _answer(answer: HostInfo | HostStatus | HostError) -> dict[str, Any]:
    if isinstance(answer, HostError):
        return {"error": answer.code, "message": answer.message}
    return asdict(answer)


def host(snapshot: Snapshot) -> dict[str, Any]:
    return {"info": _answer(snapshot.info), "status": _answer(snapshot.status)}


def settings(snapshot: Snapshot) -> dict[str, Any]:
    values = {}
    for item in fields(Settings):
        value = getattr(snapshot.settings, item.name)
        if item.name in SETTINGS_EXPORTED:
            values[item.name] = value
        elif item.name in SETTINGS_WITHHELD:
            values[item.name] = "set" if value != item.default else "default"
    profiles = snapshot.profiles
    return {
        "settings": values,
        "profiles": [
            _profile(snapshot, position, profile)
            for position, profile in enumerate(profiles.profiles, start=1)
        ],
        "global_override": profiles.has_global_override,
        "routing": _routing(snapshot.routing),
        "accounts": _accounts(snapshot.accounts),
    }


def _profile(snapshot: Snapshot, position: int, profile: Profile) -> dict[str, Any]:
    profiles = snapshot.profiles
    written: dict[str, Any] = {
        "position": position,
        "kind": profile.kind,
        "active": profile.id == profiles.active,
        "in_pool": profile.in_pool,
        "auto_update_hours": profile.auto_update_hours,
        "proxy_count": profile.proxy_count,
        "has_usage": profile.usage is not None,
        "has_override": profile.has_override,
        "updated_at": int(profile.updated_at),
        "update_failed": profile.id in profiles.update_failed,
    }
    if profile.is_provider:
        accounts = snapshot.accounts
        billing = None if accounts is None else accounts.billing.get(profile.id)
        written["billing"] = None if billing is None else _billing(billing)
    return written


def _billing(subscription: RemoteSubscription) -> dict[str, Any]:
    return {
        "access": _known(subscription.access, ACCESS_MODES),
        "status": _known(subscription.status, STATUSES),
        "usable": subscription.usable,
    }


def _known(value: str, known: frozenset[str]) -> str | None:
    """``value`` when it is one of ``known``; None when billing gave none, else "other"."""
    if not value:
        return None
    return value if value in known else "other"


def _accounts(accounts: Accounts | None) -> dict[str, Any] | None:
    if accounts is None:
        return None
    if accounts.error:
        return {"error": accounts.error}
    return {"providers": len(accounts.states), "states": [s.value for s in accounts.states]}


def _routing(routing: Routing) -> dict[str, Any]:
    plan = routing.plan()
    return {
        "mode": routing.mode,
        "plans": len(routing.plans),
        "plan_in_use": None
        if plan is None
        else {
            "groups": len(plan.groups),
            "rules": len(plan.rules),
            "rules_enabled": sum(rule.enabled for rule in plan.rules),
        },
        "warnings": None if routing.report is None else len(routing.report.warnings),
    }


def redact_urls(text: str) -> str:
    return _URL.sub(r"\1://<redacted>", text)


def log(entries: Sequence[LogEntry]) -> str:
    """mihomo.log: the warnings and errors among ``entries``, their URLs redacted."""
    kept = [
        LogEntry(entry.level, redact_urls(entry.message), entry.time_unix_ms)
        for entry in entries
        if rank(entry.level) >= rank(LOG_LEVEL)
    ]
    return LOG_NOTE + export_text(kept)


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_archive(path: Path, snapshot: Snapshot, now: float) -> None:
    """Write the archive to ``path``, replacing a file there; OSError when it cannot."""
    partial = path.with_name(path.name + ".part")
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("about.json", _json(about(snapshot.language, now)))
            archive.writestr("host.json", _json(host(snapshot)))
            archive.writestr("settings.json", _json(settings(snapshot)))
            archive.writestr("mihomo.log", log(snapshot.logs))
            archive.writestr("crashes.json", _json(snapshot.crashes))
        partial.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        raise
