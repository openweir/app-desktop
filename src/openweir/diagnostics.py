"""A zip of what a bug report needs (docs/release.md §4): versions, the platform, the host's
GetInfo and GetStatus, the core's recent warnings and errors and the settings without secrets.

What leaves the machine is chosen by allow lists, never by removing what looks secret: a
profile contributes counts and flags, not its name, link, path or text; an override only
whether it exists. Every setting is listed as exported or withheld, and a test fails when
a new one is neither, so adding a setting means deciding.

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
import platform
import re
import sys
import zipfile
from collections.abc import Awaitable, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import PySide6
from PySide6.QtCore import qVersion

from openweir import __version__
from openweir.desktop.login import PACKAGED
from openweir.host.base import HostError, HostInfo, HostStatus, LogEntry, TunnelHost
from openweir.models.library import Routing
from openweir.models.logs import export_text, rank
from openweir.models.profiles import ProfileList
from openweir.models.settings import Settings

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
    return {
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
            {
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
            for position, profile in enumerate(profiles.profiles, start=1)
        ],
        "global_override": profiles.has_global_override,
        "routing": _routing(snapshot.routing),
    }


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
