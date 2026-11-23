"""Updates on Linux (D62): the package of the installed format, SHA256SUMS checked against its
Ed25519 signature before it is trusted, and the shell that runs the service's install helper
through pkexec and starts the UI again with its exit code."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from openweir import product
from openweir.update import linux
from openweir.update.releases import (
    CHECKSUMS_NAME,
    SIGNATURE_NAME,
    ReleaseSource,
    UpdateError,
    installed_package_format,
    installer_name,
)
from openweir.update.signature import LinuxVerifier, default_verifier, linux_verifier
from openweir.update.updater import Updater
from tests.support.releases import FakeGitHub, fake_github

ROOT = Path(__file__).resolve().parents[1]
DEB = "OpenWeir-1.1.0-linux-x64.deb"
RPM = "OpenWeir-1.1.0-linux-arm64.rpm"
BODY = b"package " * 100_000
CRYPTOGRAPHY = "cryptography.hazmat.primitives.asymmetric.ed25519"


@pytest.fixture
def signing_key() -> Any:
    """A key pair of the test's own. cryptography comes with keyring's Secret Service backend,
    which only Linux installs, so elsewhere these tests skip."""
    ed25519 = pytest.importorskip(CRYPTOGRAPHY)
    return ed25519.Ed25519PrivateKey.generate()


def public(private: Any) -> str:
    """The key as OPENWEIR_LINUX_UPDATE_KEY holds it."""
    return base64.b64encode(private.public_key().public_bytes_raw()).decode()


@pytest.fixture
def github() -> Iterator[FakeGitHub]:
    with fake_github() as server:
        server.add("OpenWeir-1.1.0-macos-universal.dmg", b"disk image")
        server.add(DEB, b"!<arch>\n" + BODY)
        server.add(RPM, b"\xed\xab\xee\xdb" + BODY)
        server.add_checksums("OpenWeir-1.1.0-macos-universal.dmg", DEB, RPM)
        yield server


def sign(github: FakeGitHub, private: Any) -> None:
    github.add(SIGNATURE_NAME, private.sign(github.files[CHECKSUMS_NAME]))


def updater(
    github: FakeGitHub,
    tmp_path: Path,
    key: str,
    *,
    package_format: str = "deb",
    machine: str = "x86_64",
) -> Updater:
    return Updater(
        tmp_path,
        ReleaseSource(github.latest_url),
        default_verifier("linux", linux_key=key),
        current="1.0.0",
        system="linux",
        machine=machine,
        package_format=package_format,
    )


@pytest.mark.parametrize(
    ("machine", "package_format", "name"),
    [
        ("x86_64", "deb", "OpenWeir-1.1.0-linux-x64.deb"),
        ("aarch64", "deb", "OpenWeir-1.1.0-linux-arm64.deb"),
        ("amd64", "rpm", "OpenWeir-1.1.0-linux-x64.rpm"),
        ("arm64", "rpm", "OpenWeir-1.1.0-linux-arm64.rpm"),
        ("x86_64", "", None),  # from source: no marker
        ("x86_64", "tar", None),
        ("riscv64", "deb", None),
    ],
)
def test_the_package_is_the_installed_format_for_this_machine(
    machine: str, package_format: str, name: str | None
) -> None:
    assert installer_name("1.1.0", "linux", machine, package_format) == name


def test_the_format_is_the_marker_beside_the_executable(tmp_path: Path) -> None:
    install = tmp_path / "opt" / "openweir"
    install.mkdir(parents=True)
    executable = install / "openweir"
    executable.write_bytes(b"")
    assert installed_package_format(str(executable)) is None
    for text, found in (("deb\n", "deb"), ("rpm", "rpm"), ("tar\n", None), ("", None)):
        (install / "package-format").write_text(text)
        assert installed_package_format(str(executable)) == found
    # /usr/bin/openweir links to the executable in /opt/openweir.
    (install / "package-format").write_text("rpm\n")
    link = tmp_path / "usr" / "bin" / "openweir"
    link.parent.mkdir(parents=True)
    link.symlink_to(executable)
    assert installed_package_format(str(link)) == "rpm"
    # The package's marker is the one beside its executable.
    assert Path(product.LINUX_PACKAGE_FORMAT).parent == Path(product.LINUX_UI_PATH).parent


def test_the_package_and_its_signed_checksums_are_downloaded(
    github: FakeGitHub, tmp_path: Path, signing_key: Any
) -> None:
    sign(github, signing_key)
    update = updater(github, tmp_path, public(signing_key))
    check = update.check()
    assert (check.installer, check.manual) == (DEB, False)
    order: list[str] = []
    downloaded = update.download(check, verifying=lambda: order.append("verifying"))
    assert downloaded.path == tmp_path / "updates" / DEB
    assert downloaded.path.read_bytes() == b"!<arch>\n" + BODY
    assert downloaded.signed
    assert order == []  # the signature was checked before the download, on SHA256SUMS
    # The install helper checks the three files again as root.
    checksums, signature = (
        tmp_path / "updates" / CHECKSUMS_NAME,
        tmp_path / "updates" / SIGNATURE_NAME,
    )
    assert (downloaded.checksums, downloaded.signature) == (checksums, signature)
    assert checksums.read_bytes() == github.files[CHECKSUMS_NAME]
    assert signature.read_bytes() == github.files[SIGNATURE_NAME]
    assert update.ready(downloaded) == downloaded.path


def test_an_rpm_machine_downloads_the_rpm(
    github: FakeGitHub, tmp_path: Path, signing_key: Any
) -> None:
    sign(github, signing_key)
    update = updater(github, tmp_path, public(signing_key), package_format="rpm", machine="aarch64")
    assert update.download(update.check()).path.name == RPM


def _another_key(github: FakeGitHub, private: Any) -> None:
    ed25519 = pytest.importorskip(CRYPTOGRAPHY)
    sign(github, ed25519.Ed25519PrivateKey.generate())


def _changed_checksums(github: FakeGitHub, private: Any) -> None:
    sign(github, private)
    github.add_checksums(DEB, wrong=DEB)  # SHA256SUMS changed after it was signed


def _short(github: FakeGitHub, private: Any) -> None:
    github.add(SIGNATURE_NAME, private.sign(github.files[CHECKSUMS_NAME])[:63])


def _armored(github: FakeGitHub, private: Any) -> None:
    signature = private.sign(github.files[CHECKSUMS_NAME])
    github.add(SIGNATURE_NAME, base64.b64encode(signature) + b"\n")


@pytest.mark.parametrize("tamper", [_another_key, _changed_checksums, _short, _armored])
def test_a_bad_signature_is_refused_before_the_download(
    github: FakeGitHub, tmp_path: Path, signing_key: Any, tamper: Any
) -> None:
    tamper(github, signing_key)
    update = updater(github, tmp_path, public(signing_key))
    with pytest.raises(UpdateError) as caught:
        update.download(update.check())
    assert caught.value.code == "signature_invalid"
    assert not update.directory.exists()  # the package was never fetched
    assert not any(path.startswith("/download/OpenWeir") for path, _ in github.requests)


def test_a_release_without_the_signature_is_refused(
    github: FakeGitHub, tmp_path: Path, signing_key: Any
) -> None:
    update = updater(github, tmp_path, public(signing_key))
    with pytest.raises(UpdateError) as caught:
        update.download(update.check())
    assert (caught.value.code, caught.value.message) == ("signature_missing", SIGNATURE_NAME)


def test_a_package_other_than_the_signed_one_is_refused(
    github: FakeGitHub, tmp_path: Path, signing_key: Any
) -> None:
    sign(github, signing_key)
    github.add(DEB, b"!<arch>\n" + BODY[:-1] + b"!")  # after SHA256SUMS was signed
    update = updater(github, tmp_path, public(signing_key))
    with pytest.raises(UpdateError) as caught:
        update.download(update.check())
    assert caught.value.code == "checksum_mismatch"
    assert list(update.directory.iterdir()) == []


def test_a_build_without_the_key_points_to_the_release_page(
    github: FakeGitHub, tmp_path: Path
) -> None:
    update = updater(github, tmp_path, "")
    check = update.check()
    assert check.newer
    assert (check.installer, check.manual) == (None, True)
    with pytest.raises(UpdateError) as caught:
        update.download(check)
    assert caught.value.code == "no_installer"
    # No package for this machine at all: nothing to download by hand either.
    del github.files[DEB]
    assert not update.check().manual


def test_the_verifier_needs_a_key_and_cryptography(
    monkeypatch: pytest.MonkeyPatch, signing_key: Any
) -> None:
    key = public(signing_key)
    assert isinstance(linux_verifier(key), LinuxVerifier)
    assert isinstance(default_verifier("linux", linux_key=key), LinuxVerifier)
    # product.json leaves the key empty; a release build writes it (D62).
    assert default_verifier("linux", linux_key="") is None
    for bad in ("not base64!", base64.b64encode(b"short").decode(), key.rstrip("=")):
        assert linux_verifier(bad) is None
    monkeypatch.setitem(sys.modules, CRYPTOGRAPHY, None)  # as where it is not installed
    assert linux_verifier(key) is None


def test_the_signature_is_over_the_bytes_of_sha256sums(signing_key: Any) -> None:
    verifier = linux_verifier(public(signing_key))
    assert verifier is not None
    sums = f"{'a' * 64}  {DEB}\n".encode()
    verifier.verify_checksums(sums, signing_key.sign(sums))
    for changed in (sums.replace(b"\n", b"\r\n"), sums + b"\n", sums.upper()):
        with pytest.raises(UpdateError) as caught:
            verifier.verify_checksums(changed, signing_key.sign(sums))
        assert caught.value.code == "signature_invalid"


# -- the install helper ------------------------------------------------------------------------


def test_the_install_runs_the_helper_through_pkexec_then_the_ui() -> None:
    package = Path("/home/u/.local/share/OpenWeir/updates") / DEB
    command = linux.install_command(
        package, package.with_name(CHECKSUMS_NAME), package.with_name(SIGNATURE_NAME), 4242
    )
    assert command[:2] == ["/bin/sh", "-c"]
    assert command[3:] == [
        "sh",
        str(package),
        str(package.with_name(CHECKSUMS_NAME)),
        str(package.with_name(SIGNATURE_NAME)),
        "4242",
    ]
    script = command[2]
    assert script.startswith(
        '/usr/bin/pkexec /opt/openweir/openweir-service -install-update "$1" "$2" "$3"; code=$?;'
    )
    assert script.endswith('exec /opt/openweir/openweir --update-result="$code"')
    assert 'kill -0 "$4"' in script
    assert product.LINUX_SERVICE_PATH == "/opt/openweir/openweir-service"
    assert product.LINUX_UI_PATH == "/opt/openweir/openweir"


def _executable(path: Path, script: str) -> str:
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return str(path)


@pytest.mark.skipif(sys.platform == "win32", reason="the shell is Linux's")
@pytest.mark.parametrize("code", [0, linux.REFUSED, linux.DISMISSED, linux.NOT_AUTHORIZED])
def test_the_shell_starts_the_ui_with_the_helpers_exit_code(tmp_path: Path, code: int) -> None:
    """pkexec, the service and the UI stand in here; the files' paths have spaces, as a home
    directory's may."""
    data = tmp_path / "data dir" / "updates"
    data.mkdir(parents=True)
    files = (data / DEB, data / CHECKSUMS_NAME, data / SIGNATURE_NAME)
    asked, started = tmp_path / "asked", tmp_path / "started"
    pkexec = _executable(tmp_path / "pkexec", f'printf "%s\\n" "$@" > "{asked}"\nexit {code}\n')
    ui = _executable(tmp_path / "ui", f'printf "%s\\n" "$@" > "{started}"\n')
    ended = subprocess.Popen(["true"])  # a UI that is gone
    ended.wait()
    command = linux.install_command(
        *files, ended.pid, pkexec=pkexec, service="/opt/openweir/openweir-service", ui=ui
    )
    subprocess.run(command, check=True, timeout=30)
    assert asked.read_text().splitlines() == [
        "/opt/openweir/openweir-service",
        "-install-update",
        *map(str, files),
    ]
    assert started.read_text().splitlines() == [f"--update-result={code}"]


@pytest.mark.skipif(sys.platform == "win32", reason="the shell is Linux's")
def test_the_next_ui_waits_for_this_one_to_go(tmp_path: Path) -> None:
    """When pkexec fails at once, the UI that asked may still be on its way out; the next one
    would hand it its result and go (desktop/instance.py)."""
    gone = tmp_path / "gone"
    leaving = subprocess.Popen(["/bin/sh", "-c", f'sleep 1; touch "{gone}"'])
    pkexec = _executable(tmp_path / "pkexec", f"exit {linux.NOT_AUTHORIZED}\n")
    started = tmp_path / "started"
    ui = _executable(tmp_path / "ui", f'[ -e "{gone}" ] && echo after > "{started}"\n')
    files = (tmp_path / DEB, tmp_path / CHECKSUMS_NAME, tmp_path / SIGNATURE_NAME)
    shell = subprocess.Popen(linux.install_command(*files, leaving.pid, pkexec=pkexec, ui=ui))
    leaving.wait(timeout=30)  # reaped: until then kill -0 finds it
    assert shell.wait(timeout=30) == 0
    assert started.read_text() == "after\n"


@pytest.mark.skipif(sys.platform != "linux", reason="the install helper is Linux's")
def test_the_shell_outlives_the_ui(tmp_path: Path) -> None:
    """It runs in a session of its own, which the UI's leaving does not end, and reads nothing
    from the UI's terminal."""
    out = tmp_path / "out"
    probe = (
        "import os, sys\n"
        "stdin = os.readlink('/proc/self/fd/0')\n"
        "open(sys.argv[1], 'w').write(f'{os.getsid(0) == os.getpid()} {stdin}')\n"
    )
    pid = linux.spawn_detached([sys.executable, "-c", probe, str(out)])
    assert os.waitpid(pid, 0)[1] == 0
    assert out.read_text() == "True /dev/null"


def test_starting_the_install_passes_this_process() -> None:
    spawned: list[Sequence[str]] = []

    def spawn(command: Sequence[str]) -> int:
        spawned.append(command)
        return 1

    files = (Path("/u") / DEB, Path("/u") / CHECKSUMS_NAME, Path("/u") / SIGNATURE_NAME)
    assert linux.start_install(*files, spawn=spawn)
    assert spawned == [linux.install_command(*files, os.getpid())]

    def failing(command: Sequence[str]) -> int:
        raise FileNotFoundError(2, "No such file or directory", command[0])

    assert not linux.start_install(*files, spawn=failing)


def test_the_exit_codes_are_the_helpers() -> None:
    """update_linux.go's codes, which the UI tells apart (viewmodels/about.py)."""
    go = (ROOT / "src/core/service/update_linux.go").read_text(encoding="utf-8")
    codes = dict(re.findall(r"^\s*(exit\w+)\s*=\s*(\d+)$", go, re.MULTILINE))
    assert codes == {
        "exitRefused": str(linux.REFUSED),
        "exitInstallFailed": str(linux.INSTALL_FAILED),
    }
    assert {linux.INSTALLED, 1, linux.DISMISSED, linux.NOT_AUTHORIZED}.isdisjoint(
        {linux.REFUSED, linux.INSTALL_FAILED}
    )
