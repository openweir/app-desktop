"""Install links: a provider's 一键导入 button (docs/release.md §7, decision D6), and an
OpenWeir provider's sign-in link (docs/accounts.md §2, D55).

``openweir://install-config?url=<subscription>&name=<name>``, or the same with the schemes
other clients take: the Clash clients' common ``clash:``, and (D45) ``mihomo:`` (Clash Party)
and ``clashmeta:`` (Clash Meta for Android, FlClash, Remnawave's subscription page), fills in
the import form; the user imports from there. Any web page can open such a link, so it never
imports by itself.

``openweir://provider?url=<origin>``, from an OpenWeir provider's user center, opens the sign-in
dialog with the provider's address filled in. It takes OpenWeir's own scheme only, and an https
origin only (no path, query or user name); like an install link it signs in to nothing itself.

A link arrives as a command-line argument on Windows (the installer registers the scheme),
as an Apple Event on macOS, which Qt turns into a QFileOpenEvent on the application, or
from a second UI that hands it to the first (instance.py).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtGui import QFileOpenEvent

from openweir.models.profiles import ProfileError
from openweir.models.provider.api import origin as provider_origin
from openweir.models.subscription import check_url
from openweir.product import URL_SCHEMES

SCHEMES = URL_SCHEMES  # product.json; registered by the packaging
# OpenWeir's own scheme: the other clients' schemes carry install links only.
OWN_SCHEME = SCHEMES[0]
ACTION = "install-config"
PROVIDER_ACTION = "provider"
MAX_NAME = 100
MAX_LINK = 8192  # longer is no subscription link


@dataclass(frozen=True)
class InstallLink:
    url: str  # the subscription, http or https
    name: str  # may be empty


@dataclass(frozen=True)
class ProviderLink:
    origin: str  # https://host[:port], as api.origin writes it


def is_link(text: str) -> bool:
    """Whether ``text`` uses one of the schemes; whether it is a valid one is ``parse``'s."""
    scheme, separator, _ = text.partition(":")
    return bool(separator) and scheme.lower() in SCHEMES


def parse(text: str) -> InstallLink | ProviderLink | None:
    """The subscription an install link names, or the provider a sign-in link names; None for
    anything else."""
    if len(text) > MAX_LINK or not is_link(text):
        return None
    parts = urlsplit(text.strip())
    if parts.path not in ("", "/"):
        return None
    action = parts.netloc.lower()
    if action == PROVIDER_ACTION and parts.scheme.lower() == OWN_SCHEME:
        return _provider(parts.query)
    if action != ACTION:
        return None
    query = parse_qs(parts.query)
    urls = query.get("url") or []
    if len(urls) != 1:
        return None
    try:
        url = check_url(urls[0])
    except ProfileError:
        return None
    name = "".join(c for c in (query.get("name") or [""])[0] if unicodedata.category(c)[0] != "C")
    return InstallLink(url, name.strip()[:MAX_NAME].strip())


def _provider(query: str) -> ProviderLink | None:
    """The provider of ``openweir://provider?url=<origin>``: an https origin and nothing more,
    so the link cannot lead the dialog anywhere but the provider's own address."""
    urls = parse_qs(query).get("url") or []
    if len(urls) != 1:
        return None
    url = urls[0].strip()
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or parts.path not in ("", "/"):
        return None
    if parts.query or parts.fragment or "?" in url or "#" in url:
        return None
    try:
        origin = provider_origin(url)
    except ProfileError:  # ProviderError: no host, a user name, a bad port
        return None
    return ProviderLink(origin)


def from_arguments(arguments: Sequence[str]) -> str | None:
    """The first command-line argument that is a link, as Windows passes one."""
    return next((a for a in arguments if is_link(a) and len(a) <= MAX_LINK), None)


class LinkReceiver(QObject):
    """Takes links from the system (QFileOpenEvent) and from wherever else they arrive, and
    hands them on once the UI is ready: a link that opened the app comes before its window."""

    received = Signal(str)

    def __init__(self, app: QCoreApplication, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pending: list[str] = []
        self._ready = False
        app.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt's name
        if event.type() == QEvent.Type.FileOpen and isinstance(event, QFileOpenEvent):
            link = bytes(event.url().toEncoded().data()).decode("utf-8", "replace")
            if is_link(link):
                self.deliver(link)
                return True
        return super().eventFilter(watched, event)

    def deliver(self, link: str) -> None:
        if self._ready:
            self.received.emit(link)
        else:
            self._pending.append(link)

    def ready(self) -> None:
        """The UI can take links now; the ones that came early go first."""
        self._ready = True
        pending, self._pending = self._pending, []
        for link in pending:
            self.received.emit(link)
