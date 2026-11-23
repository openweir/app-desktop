"""Build OpenWeir's Linux packages (docs/decisions.md D60-D67, docs/architecture.md "Linux (in
progress)"): the UI by Nuitka in standalone mode, the service by go build, Cronet and the GEO
data beside it, together the tree that installs as /opt/openweir, then a .deb and a .rpm around
it by nfpm.

    uv run --group build python scripts/build_linux.py                   # tree and packages
    uv run --group build python scripts/build_linux.py --skip-packages   # the tree
    uv run python scripts/build_linux.py --packages-only                 # packages of the tree
    ... --format deb                                                     # one of the two

Everything lands in build/linux-<arch>, for the machine's architecture (x64 or arm64; Python
extension modules are never cross-compiled):

    openweir/                                 /opt/openweir as the packages install it
    symbols/                                  the DWARF split off openweir and openweir-service
    OpenWeir-<version>-linux-<arch>.deb       the release's assets (docs/release.md §1)
    OpenWeir-<version>-linux-<arch>.rpm

The packages run on the distributions the build machine's glibc and libstdc++ allow: x64 is
built in ubuntu:22.04 (glibc 2.35) and arm64 on Ubuntu 24.04 (glibc 2.39, PySide6's aarch64
floor). check() holds every ELF file in the tree to those versions and to the architecture.
The libraries the tree does not carry come from the distribution; package/linux/
system-libraries.toml names them, and an ELF file that needs any other stops the build. The
packages depend on what is used: the .deb on Debian's package names, the .rpm on the sonames.

The service links OPENWEIR_LINUX_UPDATE_KEY, the release's update key (D62), and the UI bundles
it in product.json; without it the build has no installer for updates. The Google OAuth client
comes from the environment as for scripts/build_app.py. nfpm runs as go run at NFPM_VERSION;
Nuitka needs a C compiler and patchelf. Nothing here installs or runs the packages: CI does
that on disposable machines.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import importlib.metadata
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Run as a script, Python puts scripts/ on the path rather than the root.
sys.path.insert(0, str(ROOT))

from openweir import product  # noqa: E402
from scripts import (  # noqa: E402
    build_app,
    build_apple,
    build_windows,
    cronet,
    fetch_geodata,
    gomod,
    symbols,
)
from scripts.build_apple import BuildError  # noqa: E402

PACKAGE = ROOT / "package" / "linux"
ENTRY = ROOT / "package" / "entry.py"
CORE = ROOT / "src" / "core"
NFPM_TEMPLATE = PACKAGE / "nfpm.yaml"
SCRIPTS = PACKAGE / "scripts"
FUNCTIONS = SCRIPTS / "functions.sh"
DESKTOP_TEMPLATE = PACKAGE / "org.openweir.app.desktop.in"
SYSTEM_LIBRARIES = PACKAGE / "system-libraries.toml"
NFPM_MODULE = "github.com/goreleaser/nfpm/v2/cmd/nfpm"
NFPM_VERSION = "v2.48.0"
PRODUCT_PACKAGE = build_windows.PRODUCT_PACKAGE
UPDATE_KEY_VARIABLE = f"{PRODUCT_PACKAGE}.LinuxUpdateKey"

# product.json, with the release environment's update key (OPENWEIR_LINUX_UPDATE_KEY).
PRODUCT = product.load(product.SOURCE, os.environ)
APP_NAME: str = PRODUCT["displayName"]
PACKAGE_NAME: str = PRODUCT["productId"]  # the update helper checks it
INSTALL_DIR: str = PRODUCT["linux"]["installDir"]
CLIENT: str = PRODUCT["linux"]["uiExecutable"]
SERVICE: str = PRODUCT["linux"]["serviceExecutable"]
DESKTOP_ID: str = PRODUCT["linux"]["desktopId"]
CRONET = "libcronet.so"  # beside the service, which loads it by its path
GEODATA = build_windows.GEODATA
GO_TAGS = build_windows.GO_TAGS
FORMATS = ("deb", "rpm")
# The files an installed OpenWeir cannot do without; check() looks for each.
REQUIRED = (
    CLIENT,
    SERVICE,
    CRONET,
    "LICENSE",
    "openweir/licenses/THIRD_PARTY_NOTICES.txt",
    *(f"{GEODATA}/{name}" for name in build_apple.GEO_FILES),
)
# The Qt plugins the window cannot do without, by their family and file, wherever Nuitka puts
# them: both display servers, the xdg-shell windows of Wayland and the decorations D64 asks for
# there (adwaita, which needs QtSvg and QtDBus, and the plain bradient).
REQUIRED_PLUGINS = (
    "platforms/libqwayland.so",
    "platforms/libqxcb.so",
    "wayland-shell-integration/libxdg-shell.so",
    "wayland-decoration-client/libadwaita.so",
    "wayland-decoration-client/libbradient.so",
)

ELF_MAGIC = b"\x7fELF"
EM_X86_64 = 62
EM_AARCH64 = 183


@dataclass(frozen=True)
class Target:
    goarch: str
    machine: int  # the ELF header's e_machine
    # The newest symbol versions of glibc and libstdc++ that the files may need: those of the
    # image the architecture is built in, the oldest distributions the packages support.
    newest: Mapping[str, tuple[int, ...]]


TARGETS = {
    "x64": Target(
        "amd64", EM_X86_64, {"GLIBC_": (2, 35), "GLIBCXX_": (3, 4, 30), "CXXABI_": (1, 3, 13)}
    ),
    "arm64": Target(
        "arm64", EM_AARCH64, {"GLIBC_": (2, 39), "GLIBCXX_": (3, 4, 33), "CXXABI_": (1, 3, 15)}
    ),
}
# platform.machine() of each.
MACHINES = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}


def target(arch: str) -> Target:
    try:
        return TARGETS[arch]
    except KeyError:
        raise BuildError(f"unsupported Linux architecture: {arch}") from None


def native_arch() -> str:
    machine = platform.machine().lower()
    if machine not in MACHINES:
        raise BuildError(f"the Linux build needs an x86_64 or aarch64 machine, not {machine}")
    return MACHINES[machine]


def build_directory(arch: str) -> Path:
    target(arch)
    return ROOT / "build" / f"linux-{arch}"


def package_name(version: str, arch: str, kind: str) -> str:
    """As docs/release.md §1 names the assets, which the updater and the update helper find."""
    target(arch)
    if kind not in FORMATS:
        raise BuildError(f"unknown package format: {kind}")
    return f"{APP_NAME}-{version}-linux-{arch}.{kind}"


# -- the system libraries ---------------------------------------------------------------------


@dataclass(frozen=True)
class Library:
    deb: str
    sonames: tuple[str, ...]
    # The symbol version prefix whose newest need goes into the .deb dependency, as "GLIBC_".
    versions: str | None = None
    # Loaded by its soname at run time (ctypes), which readelf does not see: always depended on.
    loaded_by_name: bool = False


@dataclass(frozen=True)
class SystemLibraries:
    libraries: tuple[Library, ...]
    requires_deb: tuple[str, ...]
    requires_rpm: tuple[str, ...]

    def by_soname(self) -> dict[str, Library]:
        return {soname: library for library in self.libraries for soname in library.sonames}


def load_system_libraries(path: Path = SYSTEM_LIBRARIES) -> SystemLibraries:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    libraries = tuple(
        Library(
            item["deb"],
            tuple(item["sonames"]),
            item.get("versions"),
            item.get("loaded_by_name", False),
        )
        for item in data["library"]
    )
    seen: set[str] = set()
    for library in libraries:
        repeated = seen.intersection(library.sonames)
        if repeated:
            raise BuildError(f"{path.name} lists {', '.join(sorted(repeated))} twice")
        seen.update(library.sonames)
    requires = data["requires"]
    return SystemLibraries(libraries, tuple(requires["deb"]), tuple(requires["rpm"]))


# -- ELF files --------------------------------------------------------------------------------


def elf_machine(path: Path) -> int | None:
    """The e_machine of an ELF file's header; None when it is not an ELF file."""
    with path.open("rb") as file:
        header = file.read(20)
    if len(header) < 20 or header[:4] != ELF_MAGIC:
        return None
    order = "little" if header[5] == 1 else "big"
    return int.from_bytes(header[18:20], order)


@dataclass
class ElfInfo:
    path: str  # relative to the tree
    soname: str | None
    needed: tuple[str, ...]
    # The symbol versions needed of each library, by its soname (readelf -V's version needs).
    versions: dict[str, set[str]] = field(default_factory=dict)


def parse_dynamic(text: str) -> tuple[str | None, tuple[str, ...]]:
    """The SONAME and the NEEDED entries in ``readelf -d`` output."""
    soname = re.search(r"\(SONAME\)\s+Library soname: \[([^\]]+)\]", text)
    needed = re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", text)
    return (soname.group(1) if soname else None), tuple(needed)


def parse_version_needs(text: str) -> dict[str, set[str]]:
    """The symbol versions needed of each library, from ``readelf -V`` output's version needs
    section; the other sections name versions a file defines or numbers its symbols."""
    needs: dict[str, set[str]] = {}
    current: set[str] | None = None
    inside = False
    for line in text.splitlines():
        if line.startswith("Version "):
            inside = line.startswith("Version needs section")
            current = None
            continue
        if not inside:
            continue
        if found := re.search(r"\bFile: (\S+)", line):
            current = needs.setdefault(found.group(1), set())
        elif (found := re.search(r"\bName: (\S+)", line)) and current is not None:
            current.add(found.group(1))
    return needs


def _readelf(*arguments: str | Path) -> str:
    return subprocess.run(
        ["readelf", "-W", *map(str, arguments)], check=True, capture_output=True, text=True
    ).stdout


def elf_files(tree: Path) -> list[Path]:
    """Every regular ELF file in ``tree``; links are left to the files they name."""
    return [
        path
        for path in sorted(tree.rglob("*"))
        if not path.is_symlink() and path.is_file() and elf_machine(path) is not None
    ]


def read_elf(tree: Path, path: Path, readelf: Callable[..., str] = _readelf) -> ElfInfo:
    soname, needed = parse_dynamic(readelf("-d", path))
    versions = parse_version_needs(readelf("-V", path))
    return ElfInfo(path.relative_to(tree).as_posix(), soname, needed, versions)


def read_tree(tree: Path, readelf: Callable[..., str] = _readelf) -> list[ElfInfo]:
    return [read_elf(tree, path, readelf) for path in elf_files(tree)]


def _version_key(name: str, prefix: str) -> tuple[int, ...] | None:
    rest = name.removeprefix(prefix)
    if rest == name or not re.fullmatch(r"\d+(\.\d+)*", rest):
        return None
    return tuple(map(int, rest.split(".")))


def _shown(version: tuple[int, ...]) -> str:
    return ".".join(map(str, version))


def newest_versions(infos: Iterable[ElfInfo], prefix: str) -> dict[str, tuple[int, ...]]:
    """The newest ``prefix`` version (GLIBC_ and so on) the files need of each library."""
    newest: dict[str, tuple[int, ...]] = {}
    for info in infos:
        for soname, names in info.versions.items():
            for name in names:
                version = _version_key(name, prefix)
                if version is not None and version > newest.get(soname, ()):
                    newest[soname] = version
    return newest


def check_symbol_versions(infos: list[ElfInfo], arch: str) -> dict[str, str]:
    """No file needs a newer glibc or libstdc++ than the oldest supported distributions have.
    Returns the newest version needed of each prefix, for the build's summary."""
    problems = []
    summary = {}
    for prefix, limit in target(arch).newest.items():
        newest = ()
        for info in infos:
            for names in info.versions.values():
                for name in names:
                    version = _version_key(name, prefix)
                    if version is None:
                        continue
                    newest = max(newest, version)
                    if version > limit:
                        problems.append(f"{info.path} needs {name}, above {prefix}{_shown(limit)}")
        if newest:
            summary[prefix] = _shown(newest)
    if problems:
        raise BuildError(
            "newer than the oldest supported distributions:\n  " + "\n  ".join(problems)
        )
    return summary


@dataclass(frozen=True)
class Dependencies:
    deb: tuple[str, ...]
    rpm: tuple[str, ...]


class UnknownLibraryError(BuildError):
    """Sonames that files need and that neither the tree nor system-libraries.toml has, each
    with the files that need it."""

    def __init__(self, unknown: Mapping[str, list[str]]) -> None:
        self.unknown = dict(sorted(unknown.items()))
        lines = [
            f"{soname} (needed by {', '.join(users[:3])}{' and more' if len(users) > 3 else ''})"
            for soname, users in self.unknown.items()
        ]
        super().__init__(
            "neither carried nor in package/linux/system-libraries.toml:\n  " + "\n  ".join(lines)
        )


def dependencies(
    infos: list[ElfInfo], libraries: SystemLibraries, carried: Iterable[str] = ()
) -> Dependencies:
    """What the packages depend on: the system libraries the files need, and the rest that
    system-libraries.toml requires. ``carried`` are the names the tree's own libraries answer
    to (their files and sonames). A soname that neither the tree nor the list has fails, as
    does a library the list says comes from the system but the tree carries too."""
    known = libraries.by_soname()
    provided = set(carried)
    for info in infos:
        provided.add(Path(info.path).name)
        if info.soname:
            provided.add(info.soname)
    shadowing = sorted(provided & known.keys())
    if shadowing:
        raise BuildError(
            f"the tree carries {', '.join(shadowing)}, which system-libraries.toml takes from "
            "the system; leave it out of the build"
        )
    unknown: dict[str, list[str]] = {}
    used = {
        soname
        for library in libraries.libraries
        if library.loaded_by_name
        for soname in library.sonames
    }
    for info in infos:
        for soname in info.needed:
            if soname in provided:
                continue
            if soname in known:
                used.add(soname)
            else:
                unknown.setdefault(soname, []).append(info.path)
    if unknown:
        raise UnknownLibraryError(unknown)
    deb: list[str] = []
    for library in libraries.libraries:
        if not used.intersection(library.sonames):
            continue
        entry = library.deb
        if library.versions:
            newest = newest_versions(infos, library.versions)
            version = max((newest[s] for s in library.sonames if s in newest), default=None)
            if version is not None:
                entry = f"{entry} (>= {_shown(version)})"
        if entry not in deb:
            deb.append(entry)
    rpm = [f"{soname}()(64bit)" for soname in sorted(used)]
    for prefix in ("GLIBC_", "GLIBCXX_", "CXXABI_"):
        for soname, version in sorted(newest_versions(infos, prefix).items()):
            if soname in used:
                rpm.append(f"{soname}({prefix}{_shown(version)})(64bit)")
    return Dependencies((*deb, *libraries.requires_deb), (*rpm, *libraries.requires_rpm))


def prune_unneeded(tree: Path, infos: list[ElfInfo], keep: Iterable[str] = (CRONET,)) -> list[str]:
    """Removes the shared libraries no file in the tree needs, until there are none, and returns
    their paths. Nuitka carries a library's dependencies with it, so leaving a system library
    out leaves its own dependencies behind (libsystemd for libdbus, say); the distribution
    provides those with the library. Only lib*.so* files with a soname go: Qt's plugins have
    none, and Python's extension modules, which it loads by their paths, neither have one nor
    start with lib. Cronet, which the service loads by its path, stays too."""
    kept = set(keep)
    removed: list[str] = []
    while True:
        needed = {soname for info in infos for soname in info.needed}
        unneeded = [
            info
            for info in infos
            if info.soname is not None
            and Path(info.path).name.startswith("lib")
            and Path(info.path).name not in kept
            and info.soname not in needed
            and Path(info.path).name not in needed
        ]
        if not unneeded:
            return removed
        for info in unneeded:
            (tree / info.path).unlink()
            removed.append(info.path)
            infos.remove(info)
        # Links to them would dangle.
        for path in sorted(tree.rglob("*")):
            if path.is_symlink() and not path.exists():
                path.unlink()


# -- the tree ---------------------------------------------------------------------------------


def normalize_modes(tree: Path) -> None:
    """Directories 0755, programs and libraries 0755, the rest 0644, whatever the build
    machine's umask made of them: nfpm copies the modes, and the service accepts a UI only from
    a tree no one but root may write to."""
    tree.chmod(0o755)
    for path in tree.rglob("*"):
        if path.is_symlink():
            continue
        executable = path.stat().st_mode & 0o111 or elf_machine(path) is not None
        path.chmod(0o755 if path.is_dir() or executable else 0o644)


def check_tree(tree: Path, arch: str) -> int:
    """Every required file is there; only directories, regular files and links that stay inside
    the tree; nothing group or other may write to, nothing setuid or setgid; and every ELF file
    is for the target. Returns how many ELF files there are."""
    expected = target(arch).machine
    for name in REQUIRED:
        path = tree / name
        if path.is_symlink() or not path.is_file():
            raise BuildError(f"{tree.name} has no {name}")
    files = {path.relative_to(tree).as_posix() for path in tree.rglob("*") if path.is_file()}
    for plugin in REQUIRED_PLUGINS:
        if not any(name == plugin or name.endswith(f"/{plugin}") for name in files):
            raise BuildError(f"{tree.name} has no Qt plugin {plugin}")
    resolved_tree = tree.resolve()
    count = 0
    for path in [tree, *sorted(tree.rglob("*"))]:
        name = path.relative_to(tree).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            link = os.readlink(path)
            target_path = (path.parent / link).resolve()
            if os.path.isabs(link) or not target_path.is_relative_to(resolved_tree):
                raise BuildError(f"{name} links to {link}, outside the tree")
            if not target_path.exists():
                raise BuildError(f"{name} links to {link}, which is missing")
            continue
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise BuildError(f"{name} is neither a file, a directory nor a link")
        if mode & 0o022:
            raise BuildError(f"{name} is writable by others than its owner ({mode & 0o777:o})")
        if mode & (stat.S_ISUID | stat.S_ISGID):
            raise BuildError(f"{name} is setuid or setgid")
        if not stat.S_ISREG(mode):
            continue
        machine = elf_machine(path)
        if machine is None:
            continue
        if machine != expected:
            raise BuildError(f"{name} is for machine {machine}, not {arch}")
        count += 1
    return count


def carried_names(tree: Path) -> set[str]:
    """The file names of the tree's links, which the dynamic loader also finds."""
    return {path.name for path in tree.rglob("*") if path.is_symlink()}


# -- building ---------------------------------------------------------------------------------


def update_key() -> str:
    """The release's update key, checked as the service decodes it; empty without one."""
    key: str = PRODUCT["signing"]["linuxUpdateKey"]
    if not key:
        return ""
    try:
        raw = base64.b64decode(key, validate=True)
    except binascii.Error:
        raw = b""
    if len(raw) != 32:
        raise BuildError(
            f"{product.SIGNING_ENVIRONMENT['linuxUpdateKey']} is not an Ed25519 public key's "
            "32 bytes in base64"
        )
    return key


def build_service(version: str, arch: str) -> Path:
    """The service, static (CGO_ENABLED=0; Cronet comes through purego) and with DWARF, which
    symbols.linux splits off. -B gobuildid gives it a GNU build ID from Go's own."""
    output = build_directory(arch) / "service" / SERVICE
    output.unlink(missing_ok=True)
    environment = gomod.environment(GOOS="linux", GOARCH=target(arch).goarch, CGO_ENABLED="0")
    variables = f"-B gobuildid -X {build_apple.GO_VERSION_VARIABLE}={version}"
    if key := update_key():
        variables += f" -X {UPDATE_KEY_VARIABLE}={key}"
    else:
        print("build_linux: no OPENWEIR_LINUX_UPDATE_KEY, so no installer for updates")
    command: list[str | Path] = [
        "go",
        "-C",
        CORE,
        "build",
        "-trimpath",
        "-tags",
        GO_TAGS,
        f"-ldflags={variables}",
        "-o",
        output,
        "./service",
    ]
    gomod.download("./service", GO_TAGS, environment)
    build_apple._run(command, env=environment)
    return output


# The UI shows SVG; PNG is built in. Qt names its image format plugins libq<name>.so.
UNUSED_IMAGE_FORMATS = ("gif", "icns", "ico", "jpeg", "pdf", "tga", "tiff", "wbmp", "webp")
# Qt's platforms for devices without a desktop.
UNUSED_PLATFORMS = ("eglfs", "linuxfb", "minimalegl", "vkkhrdisplay", "vnc")
# Qt plugins beyond Nuitka's sensible set: the input methods and Wayland's decorations (the
# title band, D64).
QT_PLUGINS = (
    "sensible",
    "qml",
    "platforminputcontexts",
    "wayland-decoration-client",
    "wayland-graphics-integration-client",
    "wayland-shell-integration",
    "xcbglintegrations",
)
# Secure storage through the Secret Service (keyring's backend), the tray's check for a
# StatusNotifier host (jeepney) and the updater's signature check (cryptography's Ed25519): all
# imported only on Linux, some of it lazily.
INCLUDED_MODULES = (
    "keyring.backends.SecretService",
    "jeepney",
    "jeepney.bus_messages",
    "jeepney.io.blocking",
    "cryptography.exceptions",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
)
INCLUDED_PACKAGES = ("secretstorage",)
# As on macOS and Windows, and what only Linux wheels carry.
EXCLUDED = (
    *(f"*Qt{name}*" for name in build_app.UNUSED_QT_LIBRARIES),
    *(f"*Qt6{name}*" for name in build_app.UNUSED_QT_LIBRARIES),
    *(f"*/qml/{directory}/*" for directory in build_app.UNUSED_QML_DIRECTORIES),
    *(f"*/imageformats/libq{name}.so" for name in UNUSED_IMAGE_FORMATS),
    *(f"*/platforms/libq{name}.so" for name in UNUSED_PLATFORMS),
    "*/egldeviceintegrations/*",
    # GTK's theme would bring GTK 3 and all below it; the portal theme serves the dialogs.
    "*/platformthemes/libqgtk3.so",
    # Buffers that a compositor shares; OpenWeir is a client.
    "*/wayland-graphics-integration-client/lib*-server.so",
    "*/wayland-graphics-integration-server/*",
    "*/platforminputcontexts/libqtvirtualkeyboardplugin.so",
    "*/networkinformation/*",
    # FFmpeg, for Qt Multimedia.
    "*/libav*",
    "*/libsw*",
    "*FFmpegStub*",
    # Qt's TLS: the UI's HTTPS goes through Python's ssl, which carries its own OpenSSL.
    "*qopensslbackend*",
)


def system_patterns(libraries: SystemLibraries) -> tuple[str, ...]:
    """Nuitka patterns for the system libraries, wherever Nuitka would put them."""
    sonames = sorted(libraries.by_soname())
    return (*sonames, *(f"*/{soname}" for soname in sonames))


def build_ui(version: str, arch: str, libraries: SystemLibraries) -> Path:
    """The Nuitka standalone directory of package/entry.py; its own options are in that file."""
    if native_arch() != arch:
        raise BuildError(f"build the {arch} UI on an {arch} machine")
    build = build_directory(arch)
    output = build / "nuitka"
    if output.exists():
        shutil.rmtree(output)
    excluded = (*EXCLUDED, *system_patterns(libraries))
    command: list[str | Path] = [
        sys.executable,
        "-m",
        "nuitka",
        # Never onefile: the service accepts the UI only from /opt/openweir.
        "--mode=standalone",
        "--unstripped",
        "--enable-plugins=pyside6",
        f"--include-qt-plugins={','.join(QT_PLUGINS)}",
        # The Nuitka plugin pulls in the QtWidgets binding for QtQuick; the UI never imports it.
        "--nofollow-import-to=PySide6.QtWidgets",
        *(f"--include-module={name}" for name in INCLUDED_MODULES),
        *(f"--include-package={name}" for name in INCLUDED_PACKAGES),
        f"--output-filename={CLIENT}",
        f"--output-dir={output}",
        "--assume-yes-for-downloads",
        *(f"--noinclude-dlls={pattern}" for pattern in excluded),
        *(f"--noinclude-data-files={pattern}" for pattern in excluded),
        *build_app.google_client(build),
        *build_app.product_file(build),
        ENTRY,
    ]
    build_apple._run(command)
    return output / f"{ENTRY.stem}.dist"


def assemble(ui: Path, service: Path, geodata: Mapping[str, Path], arch: str) -> Path:
    """The tree of /opt/openweir: the UI's standalone directory, the service and Cronet beside
    the UI, the GEO data in geodata/."""
    tree = build_directory(arch) / PACKAGE_NAME
    if tree.exists():
        shutil.rmtree(tree)
    shutil.copytree(ui, tree, symlinks=True)
    shutil.copyfile(service, tree / SERVICE)
    shutil.copyfile(cronet.library("linux", target(arch).goarch).binary, tree / CRONET)
    (tree / GEODATA).mkdir()
    for name in build_apple.GEO_FILES:
        shutil.copyfile(geodata[name], tree / GEODATA / name)
    return tree


# -- packaging --------------------------------------------------------------------------------


def render_desktop_file(
    template: Path = DESKTOP_TEMPLATE, schemes: Iterable[str] = product.URL_SCHEMES
) -> str:
    """org.openweir.app.desktop: the template without its comment lines, MimeType from the URL
    schemes."""
    mime = "".join(f"x-scheme-handler/{scheme};" for scheme in schemes)
    lines = [
        line.replace("@MIME_TYPES@", mime)
        for line in template.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    ]
    return "\n".join(lines) + "\n"


def render_script(name: str, settings: Mapping[str, str] | None = None) -> str:
    """A maintainer script from package/linux/scripts with functions.sh in it. ``settings``
    replaces the values that functions.sh sets, for the tests."""
    functions = FUNCTIONS.read_text(encoding="utf-8")
    for key, value in (settings or {}).items():
        line = f"{key}={value}"
        functions, count = re.subn(
            rf"^{re.escape(key)}=.*$", lambda _, line=line: line, functions, flags=re.M
        )
        if count != 1:
            raise BuildError(f"functions.sh sets no {key}")
    script = (SCRIPTS / f"{name}.sh").read_text(encoding="utf-8")
    if script.count("# @functions@\n") != 1:
        raise BuildError(f"{name}.sh has no # @functions@ line")
    return script.replace("# @functions@\n", functions)


SCRIPT_NAMES = ("preinstall", "postinstall", "preremove", "postremove")


def render_nfpm(template: str, values: Mapping[str, object]) -> str:
    """The nfpm configuration: each @NAME@ of the template becomes values[NAME] as JSON."""

    def replace(match: re.Match[str]) -> str:
        if match.group(1) not in values:
            raise BuildError(f"nfpm.yaml names @{match.group(1)}@, which has no value")
        return json.dumps(values[match.group(1)])

    rendered = re.sub(r"@([A-Z_]+)@", replace, template)
    unused = set(values) - set(re.findall(r"@([A-Z_]+)@", template))
    if unused:
        raise BuildError(f"nfpm.yaml has no @{', '.join(sorted(unused))}@")
    return rendered


def write_package_inputs(
    work: Path, tree: Path, version: str, arch: str, depends: Dependencies
) -> Path:
    """The files nfpm needs besides the tree and package/: the format markers, the .desktop
    file, the maintainer scripts and the configuration. Returns the configuration."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise BuildError(f"the packages' version must be X.Y.Z, not {version}")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    values: dict[str, object] = {
        "ARCH": target(arch).goarch,
        "VERSION": version,
        "DEB_DEPENDS": list(depends.deb),
        "RPM_DEPENDS": list(depends.rpm),
        "TREE": str(tree),
    }
    for kind in FORMATS:
        marker = work / kind / "package-format"
        marker.parent.mkdir()
        marker.write_text(f"{kind}\n", encoding="utf-8", newline="\n")
        values[f"{kind.upper()}_MARKER"] = str(marker)
    desktop = work / f"{DESKTOP_ID}.desktop"
    desktop.write_text(render_desktop_file(), encoding="utf-8", newline="\n")
    values["DESKTOP_FILE"] = str(desktop)
    for name in SCRIPT_NAMES:
        script = work / f"{name}.sh"
        script.write_text(render_script(name), encoding="utf-8", newline="\n")
        script.chmod(0o755)
        values[name.upper()] = str(script)
    config = work / "nfpm.yaml"
    template = NFPM_TEMPLATE.read_text(encoding="utf-8")
    config.write_text(render_nfpm(template, values), encoding="utf-8", newline="\n")
    return config


def nfpm_command(given: str | None) -> list[str]:
    if given:
        return [given]
    return ["go", "run", f"{NFPM_MODULE}@{NFPM_VERSION}"]


def build_package(config: Path, kind: str, output: Path, nfpm: list[str]) -> Path:
    """Runs nfpm from the repository's root, where the configuration's paths start."""
    output.unlink(missing_ok=True)
    environment = gomod.environment(GOWORK="off")
    build_apple._run(
        [*nfpm, "package", "--config", config, "--packager", kind, "--target", output],
        cwd=ROOT,
        env=environment,
    )
    if not output.is_file():
        raise BuildError(f"nfpm wrote no {output.name}")
    return output


def check_deb(path: Path, version: str, arch: str) -> None:
    """With dpkg-deb on the machine: the control fields, and that root owns every entry and only
    root may write to /opt/openweir."""
    if shutil.which("dpkg-deb") is None:
        print(f"build_linux: no dpkg-deb, {path.name} unchecked")
        return
    fields = subprocess.run(
        ["dpkg-deb", "--field", str(path), "Package", "Version", "Architecture"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    expected = (
        f"Package: {PACKAGE_NAME}\nVersion: {version}-1\nArchitecture: {target(arch).goarch}\n"
    )
    if fields != expected:
        raise BuildError(f"{path.name} says\n{fields}not\n{expected}")
    listing = subprocess.run(
        ["dpkg-deb", "--contents", str(path)], check=True, capture_output=True, text=True
    ).stdout
    check_listing(listing)


def check_listing(listing: str) -> None:
    """``dpkg-deb --contents`` or ``tar -tv`` output: root owns every entry, and nothing in the
    install directory is writable by group or others, or setuid."""
    entries = 0
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        mode, owner, name = parts[0], parts[1], parts[5]
        entries += 1
        if owner != "root/root":
            raise BuildError(f"{name} belongs to {owner}, not root")
        inside = name.lstrip(".").startswith(INSTALL_DIR) and mode[0] != "l"
        if inside and (mode[5] == "w" or mode[8] == "w" or mode[3] in "sS" or mode[6] in "sS"):
            raise BuildError(f"{name} is {mode}")
    if not entries:
        raise BuildError("the package lists no files")


# -- main -------------------------------------------------------------------------------------

# How much of an error an annotation keeps, as tests/conftest.py's: the start, where it says
# what failed.
ANNOTATION_LIMIT = 3000
# GitHub shows ten error annotations of a step; the unknown sonames take at most this many.
ANNOTATED_SONAMES = 9


def _annotation_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _annotation_property(text: str) -> str:
    return _annotation_data(text).replace(":", "%3A").replace(",", "%2C")


def _annotation(title: str, message: str) -> str:
    return (
        f"::error title={_annotation_property(title)}::"
        f"{_annotation_data(message[:ANNOTATION_LIMIT])}"
    )


def annotations(error: BaseException) -> list[str]:
    """The error annotations of a failed build, which a CI run's page shows to anyone, without
    the logs that need a signed-in account: one for each soname nothing provides, or one for
    the error."""
    if isinstance(error, UnknownLibraryError):
        found = [
            _annotation(
                f"build_linux: unknown soname {soname}",
                f"{soname} is needed by {', '.join(users)}: bundle it, or list it in "
                "package/linux/system-libraries.toml once the oldest supported distributions "
                "ship it",
            )
            for soname, users in list(error.unknown.items())[:ANNOTATED_SONAMES]
        ]
        rest = list(error.unknown)[ANNOTATED_SONAMES:]
        if rest:
            found.append(_annotation("build_linux: unknown sonames", f"also {', '.join(rest)}"))
        return found
    if isinstance(error, subprocess.CalledProcessError):
        command = error.cmd if isinstance(error.cmd, str) else shlex.join(map(str, error.cmd))
        shown = command if len(command) <= 300 else f"{command[:300]}…"
        return [_annotation("build_linux", f"{shown} exited with {error.returncode}")]
    return [_annotation("build_linux", str(error))]


def report(error: BaseException) -> None:
    """Writes ``error``'s annotations when GitHub Actions runs the build."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for line in annotations(error):
            print(line, flush=True)


def _check_nuitka() -> None:
    try:
        installed = importlib.metadata.version("nuitka")
    except importlib.metadata.PackageNotFoundError:
        installed = None
    if installed != build_app.NUITKA_VERSION:
        version = build_app.NUITKA_VERSION
        raise BuildError(f"run with uv run --group build (Nuitka {installed}, not {version})")


def prepare_tree(tree: Path, arch: str, libraries: SystemLibraries) -> Dependencies:
    """Sets the modes, removes what nothing needs, checks the tree and works out the packages'
    dependencies."""
    normalize_modes(tree)
    infos = read_tree(tree)
    for path in prune_unneeded(tree, infos):
        print(f"build_linux: removed {path}, which only system libraries need")
    count = check_tree(tree, arch)
    newest = check_symbol_versions(infos, arch)
    depends = dependencies(infos, libraries, carried_names(tree))
    size = sum(path.stat().st_size for path in tree.rglob("*") if path.is_file())
    shown = ", ".join(f"{prefix}{version}" for prefix, version in newest.items())
    print(f"build_linux: {tree.relative_to(ROOT)}, {count} {arch} ELF files ({shown}), ", end="")
    print(f"{size / 2**20:.1f} MiB, {len(depends.deb)} dependencies")
    return depends


def main() -> None:
    parser = argparse.ArgumentParser(description="Build OpenWeir's Linux packages.")
    stage = parser.add_mutually_exclusive_group()
    stage.add_argument(
        "--skip-packages", action="store_true", help="stop at build/linux-<arch>/openweir"
    )
    stage.add_argument(
        "--packages-only",
        action="store_true",
        help="package an existing build/linux-<arch>/openweir",
    )
    parser.add_argument("--format", choices=FORMATS, action="append", help="default: both")
    parser.add_argument("--nfpm", help=f"an nfpm to run (default: go run {NFPM_VERSION})")
    arguments = parser.parse_args()
    if sys.platform != "linux":
        sys.exit("build_linux: the Linux packages are built on Linux")
    version = build_apple._version()
    try:
        arch = native_arch()
        build = build_directory(arch)
        tree = build / PACKAGE_NAME
        libraries = load_system_libraries()
        if not arguments.packages_only:
            _check_nuitka()
            update_key()
            geodata = fetch_geodata.fetch()
            service = build_service(version, arch)
            ui = build_ui(version, arch, libraries)
            tree = assemble(ui, service, geodata, arch)
            symbol_dir = build / "symbols"
            build_apple._remove(symbol_dir)
            symbols.linux([tree / CLIENT, tree / SERVICE], symbol_dir)
        depends = prepare_tree(tree, arch, libraries)
        if arguments.skip_packages:
            return
        config = write_package_inputs(build / "nfpm", tree, version, arch, depends)
        for kind in arguments.format or FORMATS:
            output = build_package(
                config,
                kind,
                build / package_name(version, arch, kind),
                nfpm_command(arguments.nfpm),
            )
            if kind == "deb":
                check_deb(output, version, arch)
            megabytes = output.stat().st_size / 2**20
            print(f"build_linux: {output.relative_to(ROOT)}, {megabytes:.1f} MiB")
    except (BuildError, OSError, subprocess.CalledProcessError) as error:
        report(error)
        sys.exit(f"build_linux: {error}")


if __name__ == "__main__":
    main()
