"""The steps of an update, in order (docs/release.md §2, §3): check the latest release, then
download this machine's installer, check it against the release's checksums and its
signature (signature.py), and check it once more right before the user opens it. A Linux
release signs its checksums instead, which are checked first, and the package is kept beside
them for the service's install helper (linux.py).

Every method blocks: the ViewModel runs them on a worker thread.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openweir import __version__
from openweir.platforms import native_machine
from openweir.update import download
from openweir.update.download import Progress
from openweir.update.releases import (
    CHECKSUMS_NAME,
    SIGNATURE_NAME,
    Asset,
    Release,
    ReleaseSource,
    UpdateError,
    installer_name,
    is_newer,
)
from openweir.update.signature import ChecksumsVerifier, Verifier, default_verifier

DIRECTORY_NAME = "updates"  # in the user data directory


@dataclass(frozen=True)
class Check:
    release: Release | None  # None while nothing has been released
    newer: bool  # it is a newer version than this one
    installer: str | None  # the asset this machine would install; None when there is none
    # The release has this machine's package, but this build cannot check it, being a Linux
    # build without the update key: the user downloads it from the release's page.
    manual: bool = False


@dataclass(frozen=True)
class Downloaded:
    release: Release
    path: Path
    sha256: str
    signed: bool  # False when only the release's checksum vouches for it
    # A Linux package's SHA256SUMS and SHA256SUMS.sig, beside it, which the service's install
    # helper checks again as root; None for other installers.
    checksums: Path | None = None
    signature: Path | None = None


class Updater:
    def __init__(
        self,
        data_dir: Path,
        source: ReleaseSource | None = None,
        verifier: Verifier | None = None,
        *,
        current: str = __version__,
        system: str = sys.platform,
        machine: str | None = None,
        package_format: str | None = None,
    ) -> None:
        """``verifier`` defaults to the platform's; without one nothing can be installed.
        ``package_format`` is the Linux package's, deb or rpm, read from the package's marker by
        default (releases.installer_name)."""
        self.directory = data_dir / DIRECTORY_NAME
        self._source = source or ReleaseSource()
        self._verifier = verifier if verifier is not None else default_verifier(system)
        self.current = current
        self._system = system
        self._machine = native_machine(system) if machine is None else machine
        self._package_format = package_format

    def check(self) -> Check:
        release = self._source.latest()
        if release is None:
            return Check(None, False, None)
        newer = is_newer(release.version, self.current)
        installer = installer_name(
            release.version, self._system, self._machine, self._package_format
        )
        if installer is None or release.asset(installer) is None:
            return Check(release, newer, None)
        if self._verifier is None:
            return Check(release, newer, None, manual=True)
        return Check(release, newer, installer)

    def download(
        self,
        check: Check,
        progress: Progress | None = None,
        cancelled: threading.Event | None = None,
        verifying: Callable[[], None] | None = None,
    ) -> Downloaded:
        """The installer, downloaded and verified; ``verifying`` is called between the
        download and the signature check, which can take a while, when there is one."""
        release = check.release
        asset = release.asset(check.installer) if release and check.installer else None
        if release is None or asset is None or self._verifier is None:
            raise UpdateError("no_installer", release.version if release else "")
        sums_asset = release.asset(CHECKSUMS_NAME)
        if sums_asset is None:
            raise UpdateError("checksum_missing", CHECKSUMS_NAME)
        body = _fetch(sums_asset, download.MAX_CHECKSUMS_BYTES)
        # A Linux release's signature covers SHA256SUMS, which counts for nothing until it is
        # checked.
        signature = None
        if isinstance(self._verifier, ChecksumsVerifier):
            signature_asset = release.asset(SIGNATURE_NAME)
            if signature_asset is None:
                raise UpdateError("signature_missing", SIGNATURE_NAME)
            signature = _fetch(signature_asset, download.MAX_SIGNATURE_BYTES)
            self._verifier.verify_checksums(body, signature)
        sha256 = download.expected_sha256(download.decode_checksums(body), asset.name)
        download.clear(self.directory)
        path = download.download(
            asset, sha256, self.directory, progress=progress, cancelled=cancelled
        )
        signed = self._verifier.signed
        if verifying is not None and signed and signature is None:
            verifying()
        try:
            self._verifier.verify(path)
            if signature is None:
                return Downloaded(release, path, sha256, signed)
            checksums = _keep(self.directory, CHECKSUMS_NAME, body)
            signature_path = _keep(self.directory, SIGNATURE_NAME, signature)
        except BaseException:
            download.clear(self.directory)
            raise
        return Downloaded(release, path, sha256, signed, checksums, signature_path)

    def ready(self, downloaded: Downloaded) -> Path:
        """The installer to open, after checking that it is still the file verified: it sits
        in a directory the user can write to."""
        try:
            matches = download.sha256_of(downloaded.path) == downloaded.sha256
        except OSError as error:
            raise UpdateError("download_failed", error.strerror or str(error)) from error
        if not matches:
            raise UpdateError("checksum_mismatch", downloaded.path.name)
        return downloaded.path

    def clear(self) -> None:
        download.clear(self.directory)


def _keep(directory: Path, name: str, data: bytes) -> Path:
    """A file that the install helper reads beside the package."""
    path = directory / name
    try:
        path.write_bytes(data)
    except OSError as error:
        raise UpdateError("download_failed", error.strerror or str(error)) from error
    return path


def _fetch(asset: Asset, limit: int) -> bytes:
    """A small asset; failing to fetch it is the download's failure."""
    try:
        return download.fetch_small(asset, limit)
    except UpdateError as error:
        if error.code == "check_failed":
            raise UpdateError("download_failed", error.message) from error
        raise
