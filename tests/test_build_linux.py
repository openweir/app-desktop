"""scripts/build_linux.py and package/linux: the tree's checks, the dependencies taken from
readelf, the nfpm configuration and what the packages install, and the maintainer scripts run
against a temporary root with stand-ins for systemctl, runuser and getent. The real build runs
on Linux in CI; here every ELF file and readelf answer is made up."""

from __future__ import annotations

import configparser
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest
import yaml

from openweir import product
from openweir.update import releases
from scripts import build_apple, build_linux, cronet, gen_icons, symbols
from scripts.build_apple import BuildError

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="modes and links of a Linux tree")
LINUX = pytest.mark.skipif(sys.platform != "linux", reason="GNU stat, timeout and /proc")


def _elf(machine: int = build_linux.EM_AARCH64) -> bytes:
    """An ELF header up to e_machine: 64-bit, little-endian."""
    return build_linux.ELF_MAGIC + bytes([2, 1, 1]) + bytes(9) + struct.pack("<HH", 3, machine)


def _tree(root: Path, machine: int = build_linux.EM_AARCH64) -> Path:
    tree = root / "openweir"
    for name in build_linux.REQUIRED:
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        binary = name in (build_linux.CLIENT, build_linux.SERVICE, build_linux.CRONET)
        path.write_bytes(_elf(machine) if binary else b"x")
    qml = tree / "PySide6" / "qml" / "QtQuick"
    qml.mkdir(parents=True)
    (qml / "libqtquick2plugin.so").write_bytes(_elf(machine))
    (qml / "qmldir").write_text("module QtQuick\n")
    return tree


# -- ELF files and readelf --------------------------------------------------------------------


def test_elf_machine(tmp_path: Path) -> None:
    path = tmp_path / "a.so"
    path.write_bytes(_elf(build_linux.EM_X86_64))
    assert build_linux.elf_machine(path) == build_linux.EM_X86_64
    big = build_linux.ELF_MAGIC + bytes([2, 2, 1]) + bytes(9) + struct.pack(">HH", 3, 183)
    path.write_bytes(big)
    assert build_linux.elf_machine(path) == build_linux.EM_AARCH64
    path.write_bytes(build_linux.ELF_MAGIC + bytes(4))
    assert build_linux.elf_machine(path) is None
    path.write_text("#!/bin/sh\n")
    assert build_linux.elf_machine(path) is None


DYNAMIC = """
Dynamic section at offset 0x2d8e8 contains 31 entries:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [libQt6Core.so.6]
 0x0000000000000001 (NEEDED)             Shared library: [libglib-2.0.so.0]
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
 0x000000000000000e (SONAME)             Library soname: [libQt6Gui.so.6]
 0x000000000000001d (RUNPATH)            Library runpath: [$ORIGIN]
"""

VERSIONS = """
Version symbols section '.gnu.version' contains 3 entries:
 Addr: 0x0000000000003390  Offset: 0x00003390  Link: 2 (.dynsym)
  000:   0 (*local*)       2 (GLIBC_2.99)    3 (Qt_6)

Version definition section '.gnu.version_d' contains 2 entries:
 Addr: 0x0000000000000a00  Offset: 0x00000a00  Link: 7 (.dynstr)
  000000: Rev: 1  Flags: BASE  Index: 1  Cnt: 1  Name: libQt6Gui.so.6
  0x001c: Rev: 1  Flags: none  Index: 2  Cnt: 1  Name: GLIBCXX_9.9

Version needs section '.gnu.version_r' contains 3 entries:
 Addr: 0x0000000000000d00  Offset: 0x00000d00  Link: 6 (.dynstr)
  000000: Version: 1  File: libstdc++.so.6  Cnt: 2
  0x0010:   Name: GLIBCXX_3.4.29  Flags: none  Version: 5
  0x0020:   Name: CXXABI_1.3.13  Flags: none  Version: 4
  0x0030: Version: 1  File: libc.so.6  Cnt: 2
  0x0040:   Name: GLIBC_2.34  Flags: none  Version: 3
  0x0050:   Name: GLIBC_2.17  Flags: none  Version: 2
  0x0060: Version: 1  File: libQt6Core.so.6  Cnt: 1
  0x0070:   Name: Qt_6  Flags: none  Version: 6
"""


def test_readelf_dynamic_section() -> None:
    soname, needed = build_linux.parse_dynamic(DYNAMIC)
    assert soname == "libQt6Gui.so.6"
    assert needed == ("libQt6Core.so.6", "libglib-2.0.so.0", "libc.so.6")
    assert build_linux.parse_dynamic("There is no dynamic section in this file.\n") == (None, ())


def test_readelf_version_needs_ignore_definitions_and_symbols() -> None:
    assert build_linux.parse_version_needs(VERSIONS) == {
        "libstdc++.so.6": {"GLIBCXX_3.4.29", "CXXABI_1.3.13"},
        "libc.so.6": {"GLIBC_2.34", "GLIBC_2.17"},
        "libQt6Core.so.6": {"Qt_6"},
    }
    assert build_linux.parse_version_needs("No version information found in this file.\n") == {}


def test_read_tree_asks_readelf_about_each_elf_file(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    asked: list[tuple[str, str]] = []

    def readelf(option: str, path: Path) -> str:
        asked.append((option, path.relative_to(tree).as_posix()))
        if path.name == build_linux.CLIENT:
            return DYNAMIC if option == "-d" else VERSIONS
        return ""

    infos = build_linux.read_tree(tree, readelf)
    assert [info.path for info in infos] == [
        "PySide6/qml/QtQuick/libqtquick2plugin.so",
        build_linux.CRONET,
        build_linux.SERVICE,
        build_linux.CLIENT,
    ]
    client = next(info for info in infos if info.path == build_linux.CLIENT)
    assert client.needed == ("libQt6Core.so.6", "libglib-2.0.so.0", "libc.so.6")
    assert client.versions["libc.so.6"] == {"GLIBC_2.34", "GLIBC_2.17"}
    assert ("-d", "PySide6/qml/QtQuick/qmldir") not in asked
    assert len(asked) == 2 * len(infos)


# -- glibc and libstdc++ ----------------------------------------------------------------------


def test_symbol_versions_stay_within_the_oldest_distributions() -> None:
    ui = build_linux.ElfInfo(
        "openweir-ui",
        None,
        ("libc.so.6", "libstdc++.so.6"),
        {"libc.so.6": {"GLIBC_2.17", "GLIBC_2.35"}, "libstdc++.so.6": {"GLIBCXX_3.4.30"}},
    )
    assert build_linux.check_symbol_versions([ui], "x64") == {
        "GLIBC_": "2.35",
        "GLIBCXX_": "3.4.30",
    }
    qt = build_linux.ElfInfo(
        "PySide6/Qt/lib/libQt6Core.so.6",
        "libQt6Core.so.6",
        ("libc.so.6", "libstdc++.so.6"),
        {"libc.so.6": {"GLIBC_2.38"}, "libstdc++.so.6": {"GLIBCXX_3.4.32", "CXXABI_1.3.15"}},
    )
    with pytest.raises(BuildError) as raised:
        build_linux.check_symbol_versions([ui, qt], "x64")
    message = str(raised.value)
    assert "libQt6Core.so.6 needs GLIBC_2.38, above GLIBC_2.35" in message
    assert "needs GLIBCXX_3.4.32, above GLIBCXX_3.4.30" in message
    assert "needs CXXABI_1.3.15, above CXXABI_1.3.13" in message
    # Ubuntu 24.04 has them all.
    assert build_linux.check_symbol_versions([ui, qt], "arm64")["GLIBC_"] == "2.38"
    newer = build_linux.ElfInfo("x", None, ("libc.so.6",), {"libc.so.6": {"GLIBC_2.40"}})
    with pytest.raises(BuildError, match=r"GLIBC_2\.40, above GLIBC_2\.39"):
        build_linux.check_symbol_versions([newer], "arm64")


def test_versions_compare_as_numbers() -> None:
    info = build_linux.ElfInfo(
        "a", None, (), {"libc.so.6": {"GLIBC_2.9", "GLIBC_2.17", "GLIBC_PRIVATE"}}
    )
    assert build_linux.newest_versions([info], "GLIBC_") == {"libc.so.6": (2, 17)}


# -- dependencies -----------------------------------------------------------------------------


def _libraries() -> build_linux.SystemLibraries:
    return build_linux.SystemLibraries(
        (
            build_linux.Library("libc6", ("libc.so.6", "libm.so.6"), "GLIBC_"),
            build_linux.Library("libstdc++6", ("libstdc++.so.6",)),
            build_linux.Library("libglib2.0-0t64 | libglib2.0-0", ("libglib-2.0.so.0",)),
            build_linux.Library("libx11-6", ("libX11.so.6",)),
        ),
        ("systemd", "pkexec | policykit-1"),
        ("systemd", "/usr/bin/pkexec"),
    )


def test_dependencies_name_the_system_libraries_in_use() -> None:
    infos = [
        build_linux.ElfInfo(
            "openweir-ui",
            None,
            ("libpython3.14.so.1.0", "libc.so.6", "libm.so.6"),
            {"libc.so.6": {"GLIBC_2.34"}, "libm.so.6": {"GLIBC_2.29"}},
        ),
        build_linux.ElfInfo("libpython3.14.so.1.0", "libpython3.14.so.1.0", ("libc.so.6",)),
        build_linux.ElfInfo(
            "PySide6/Qt/lib/libQt6Core.so.6",
            "libQt6Core.so.6",
            ("libglib-2.0.so.0", "libstdc++.so.6", "libc.so.6"),
            {"libstdc++.so.6": {"GLIBCXX_3.4.29", "CXXABI_1.3.13"}, "libc.so.6": {"GLIBC_2.35"}},
        ),
    ]
    found = build_linux.dependencies(infos, _libraries())
    # libX11 is listed but not needed.
    assert found.deb == (
        "libc6 (>= 2.35)",
        "libstdc++6",
        "libglib2.0-0t64 | libglib2.0-0",
        "systemd",
        "pkexec | policykit-1",
    )
    assert found.rpm == (
        "libc.so.6()(64bit)",
        "libglib-2.0.so.0()(64bit)",
        "libm.so.6()(64bit)",
        "libstdc++.so.6()(64bit)",
        "libc.so.6(GLIBC_2.35)(64bit)",
        "libm.so.6(GLIBC_2.29)(64bit)",
        "libstdc++.so.6(GLIBCXX_3.4.29)(64bit)",
        "libstdc++.so.6(CXXABI_1.3.13)(64bit)",
        "systemd",
        "/usr/bin/pkexec",
    )


def test_an_unknown_soname_stops_the_build() -> None:
    infos = [
        build_linux.ElfInfo("PySide6/Qt/plugins/platforms/libqxcb.so", None, ("libxcb-util.so.1",)),
        build_linux.ElfInfo("PySide6/Qt/lib/libQt6XcbQpa.so.6", "x", ("libxcb-util.so.1",)),
        build_linux.ElfInfo("openweir-ui", None, ("libc.so.6",)),
    ]
    with pytest.raises(BuildError) as raised:
        build_linux.dependencies(infos, _libraries())
    message = str(raised.value)
    assert "libxcb-util.so.1 (needed by PySide6/Qt/plugins/platforms/libqxcb.so, " in message
    assert "system-libraries.toml" in message
    # A link in the tree answers for it, as the loader would find it.
    assert build_linux.dependencies(infos, _libraries(), {"libxcb-util.so.1"}).deb[0] == "libc6"


def test_a_system_library_must_not_be_carried() -> None:
    infos = [
        build_linux.ElfInfo("openweir-ui", None, ("libglib-2.0.so.0",)),
        build_linux.ElfInfo("libglib-2.0.so.0", "libglib-2.0.so.0", ("libc.so.6",)),
    ]
    with pytest.raises(BuildError, match=r"carries libglib-2\.0\.so\.0"):
        build_linux.dependencies(infos, _libraries())


def test_the_system_library_list() -> None:
    libraries = build_linux.load_system_libraries()
    by_soname = libraries.by_soname()
    for soname in by_soname:
        assert ".so." in soname, soname
    for name in ("libc.so.6", "ld-linux-x86-64.so.2", "ld-linux-aarch64.so.1", "libEGL.so.1"):
        assert name in by_soname
    assert by_soname["libc.so.6"].versions == "GLIBC_"
    # Ubuntu 24.04's t64 name, with the old one for 22.04 and Debian 12.
    assert by_soname["libglib-2.0.so.0"].deb == "libglib2.0-0t64 | libglib2.0-0"
    assert {"systemd", "pkexec | policykit-1"} <= set(libraries.requires_deb)
    assert {"systemd", "/usr/bin/pkexec"} <= set(libraries.requires_rpm)
    # GTK stays out (libqgtk3.so is not built in), and with it the rest of GTK's stack.
    assert "libgtk-3.so.0" not in by_soname
    assert "*/platformthemes/libqgtk3.so" in build_linux.EXCLUDED


def test_a_soname_listed_twice_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "libraries.toml"
    path.write_text(
        textwrap.dedent(
            """\
            [requires]
            deb = []
            rpm = []
            [[library]]
            deb = "a"
            sonames = ["liba.so.1"]
            [[library]]
            deb = "b"
            sonames = ["liba.so.1"]
            """
        )
    )
    with pytest.raises(BuildError, match=r"liba\.so\.1 twice"):
        build_linux.load_system_libraries(path)


@POSIX
def test_libraries_only_system_libraries_need_are_removed(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "plugins").mkdir(parents=True)
    for name in ("libsystemd.so.0", "liblz4.so.1", "libQt6DBus.so.6", "libcronet.so"):
        (tree / name).write_bytes(_elf())
    (tree / "plugins" / "libqxcb.so").write_bytes(_elf())
    (tree / "liblz4.so").symlink_to("liblz4.so.1")
    infos = [
        build_linux.ElfInfo("plugins/libqxcb.so", None, ("libQt6DBus.so.6",)),
        build_linux.ElfInfo("libQt6DBus.so.6", "libQt6DBus.so.6", ("libdbus-1.so.3",)),
        # What Nuitka brought along for libdbus-1, which stays out.
        build_linux.ElfInfo("libsystemd.so.0", "libsystemd.so.0", ("liblz4.so.1",)),
        build_linux.ElfInfo("liblz4.so.1", "liblz4.so.1", ()),
        build_linux.ElfInfo("libcronet.so", "libcronet.so", ("libc.so.6",)),
    ]
    removed = build_linux.prune_unneeded(tree, infos)
    assert removed == ["libsystemd.so.0", "liblz4.so.1"]
    assert sorted(path.name for path in tree.rglob("*")) == [
        "libQt6DBus.so.6",
        "libcronet.so",
        "libqxcb.so",
        "plugins",
    ]
    assert [info.path for info in infos] == [
        "plugins/libqxcb.so",
        "libQt6DBus.so.6",
        "libcronet.so",
    ]


# -- the tree ---------------------------------------------------------------------------------


@POSIX
def test_modes_become_root_only_writable(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (tree / "geodata").chmod(0o700)
    (tree / "LICENSE").chmod(0o666)
    (tree / build_linux.CLIENT).chmod(0o600)  # ELF without an execute bit
    script = tree / "tool.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o775)
    build_linux.normalize_modes(tree)
    mode = lambda path: stat.S_IMODE(path.stat().st_mode)  # noqa: E731
    assert mode(tree) == 0o755
    assert mode(tree / "geodata") == 0o755
    assert mode(tree / "LICENSE") == 0o644
    assert mode(tree / build_linux.CLIENT) == 0o755
    assert mode(script) == 0o755
    assert build_linux.check_tree(tree, "arm64") == 4


@POSIX
def test_the_tree_check_refuses_what_the_service_would(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    build_linux.normalize_modes(tree)
    (tree / build_linux.CRONET).write_bytes(_elf(build_linux.EM_X86_64))
    with pytest.raises(BuildError, match=r"libcronet\.so is for machine 62, not arm64"):
        build_linux.check_tree(tree, "arm64")
    (tree / build_linux.CRONET).write_bytes(_elf())

    license_ = tree / "LICENSE"
    license_.chmod(0o664)
    with pytest.raises(BuildError, match="LICENSE is writable by others than its owner"):
        build_linux.check_tree(tree, "arm64")
    license_.chmod(0o644)

    (tree / "geodata").chmod(0o775)
    with pytest.raises(BuildError, match="geodata is writable"):
        build_linux.check_tree(tree, "arm64")
    (tree / "geodata").chmod(0o755)

    (tree / build_linux.SERVICE).chmod(0o4755)
    with pytest.raises(BuildError, match="setuid or setgid"):
        build_linux.check_tree(tree, "arm64")
    (tree / build_linux.SERVICE).chmod(0o755)

    (tree / "LICENSE").unlink()
    with pytest.raises(BuildError, match="has no LICENSE"):
        build_linux.check_tree(tree, "arm64")
    (tree / "LICENSE").symlink_to("openweir/licenses/THIRD_PARTY_NOTICES.txt")
    with pytest.raises(BuildError, match="has no LICENSE"):
        build_linux.check_tree(tree, "arm64")
    (tree / "LICENSE").unlink()
    license_.write_text("GPL")
    license_.chmod(0o644)
    assert build_linux.check_tree(tree, "arm64") == 4


@POSIX
def test_links_stay_inside_the_tree(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    build_linux.normalize_modes(tree)
    lib = tree / "PySide6" / "qml" / "QtQuick"
    (lib / "inside.so").symlink_to("libqtquick2plugin.so")
    assert build_linux.check_tree(tree, "arm64") == 4
    (tmp_path / "outside").write_text("x")
    for link in ("../../../../outside", str(tmp_path / "outside"), "/usr/lib/libc.so.6"):
        escape = lib / "escape"
        escape.symlink_to(link)
        with pytest.raises(BuildError, match="outside the tree"):
            build_linux.check_tree(tree, "arm64")
        escape.unlink()
    (lib / "dangling").symlink_to("missing.so")
    with pytest.raises(BuildError, match="which is missing"):
        build_linux.check_tree(tree, "arm64")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs")
def test_the_tree_holds_only_files_directories_and_links(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    build_linux.normalize_modes(tree)
    os.mkfifo(tree / "pipe", 0o644)
    with pytest.raises(BuildError, match="pipe is neither"):
        build_linux.check_tree(tree, "arm64")


def test_listing_of_the_deb_is_root_owned() -> None:
    listing = textwrap.dedent(
        """\
        drwxr-xr-x root/root         0 2026-11-23 04:43 ./opt/openweir/
        -rwxr-xr-x root/root  12870832 2026-11-23 04:43 ./opt/openweir/libcronet.so
        l--------- root/root         0 2026-11-23 04:44 ./usr/bin/openweir -> /opt/x
        -rw-r--r-- root/root       501 2026-11-23 04:43 ./usr/lib/systemd/system/openweir.socket
        """
    )
    build_linux.check_listing(listing)
    with pytest.raises(BuildError, match="belongs to runner/runner"):
        build_linux.check_listing(listing.replace("root/root  1287", "runner/runner  1287"))
    with pytest.raises(BuildError, match=r"libcronet\.so is -rwxrwxr-x"):
        build_linux.check_listing(listing.replace("-rwxr-xr-x", "-rwxrwxr-x"))
    with pytest.raises(BuildError, match="is -rwsr-xr-x"):
        build_linux.check_listing(listing.replace("-rwxr-xr-x", "-rwsr-xr-x"))
    with pytest.raises(BuildError, match="lists no files"):
        build_linux.check_listing("")


# -- building ---------------------------------------------------------------------------------


def test_package_names_are_what_the_updater_looks_for() -> None:
    for arch in build_linux.TARGETS:
        for kind in build_linux.FORMATS:
            name = build_linux.package_name("1.2.3", arch, kind)
            assert name == releases.installer_name("1.2.3", "linux", arch, kind)
    with pytest.raises(BuildError):
        build_linux.package_name("1.2.3", "riscv64", "deb")
    assert build_linux.PACKAGE_NAME == "openweir"  # the update helper's packageName


def test_the_tree_is_the_products_install_directory() -> None:
    assert f"{build_linux.INSTALL_DIR}/{build_linux.CLIENT}" == product.LINUX_UI_PATH
    assert f"{build_linux.INSTALL_DIR}/{build_linux.SERVICE}" == product.LINUX_SERVICE_PATH
    # Nuitka puts the openweir package's data in openweir/ beside the executable.
    assert build_linux.CLIENT != "openweir"
    assert any(name.startswith("openweir/") for name in build_linux.REQUIRED)


def test_the_ui_build_includes_secure_storage_and_the_signature_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str | Path]] = []
    monkeypatch.setattr(build_linux, "native_arch", lambda: "x64")
    monkeypatch.setattr(build_linux, "build_directory", lambda arch: tmp_path / arch)
    monkeypatch.setattr(build_linux.build_app, "google_client", lambda directory: [])
    monkeypatch.setattr(
        build_linux.build_apple, "_run", lambda command, **_: commands.append(command)
    )
    build_linux.build_ui("1.2.3", "x64", build_linux.load_system_libraries())
    command = [str(part) for part in commands[0]]
    assert "--mode=standalone" in command
    assert f"--output-filename={product.LINUX_UI_EXECUTABLE}" in command
    for module in (
        "keyring.backends.SecretService",
        "jeepney.io.blocking",
        "cryptography.hazmat.primitives.asymmetric.ed25519",
    ):
        assert f"--include-module={module}" in command
    assert "--include-package=secretstorage" in command
    assert "--noinclude-dlls=*/platformthemes/libqgtk3.so" in command
    assert "--noinclude-dlls=libglib-2.0.so.0" in command
    assert "--noinclude-dlls=*/libglib-2.0.so.0" in command
    assert "--noinclude-dlls=*QtWaylandCompositor*" in command
    plugins = next(part for part in command if part.startswith("--include-qt-plugins="))
    assert {"qml", "wayland-decoration-client", "platforminputcontexts"} <= set(
        plugins.removeprefix("--include-qt-plugins=").split(",")
    )
    with pytest.raises(BuildError, match="arm64 machine"):
        build_linux.build_ui("1.2.3", "arm64", build_linux.load_system_libraries())


def test_the_service_links_the_update_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[tuple[list[str | Path], dict[str, str]]] = []
    monkeypatch.setattr(build_linux, "build_directory", lambda arch: tmp_path / arch)
    monkeypatch.setattr(build_linux.gomod, "download", lambda *_: None)
    monkeypatch.setattr(
        build_linux.build_apple, "_run", lambda command, env: runs.append((command, env))
    )
    key = "11qYAYKxCrfVS/7TyWQHOg7hcvPapiMlrwIaaPcHURo="
    monkeypatch.setitem(build_linux.PRODUCT["signing"], "linuxUpdateKey", key)
    build_linux.build_service("1.2.3", "arm64")
    command, environment = runs[0]
    assert (environment["GOOS"], environment["GOARCH"], environment["CGO_ENABLED"]) == (
        "linux",
        "arm64",
        "0",
    )
    shown = [str(part) for part in command]
    assert shown[shown.index("-tags") + 1] == "with_gvisor,with_naive,with_purego"
    flags = next(part for part in shown if part.startswith("-ldflags="))
    assert "-B gobuildid" in flags
    assert f"-X {build_apple.GO_VERSION_VARIABLE}=1.2.3" in flags
    assert f"-X {build_linux.UPDATE_KEY_VARIABLE}={key}" in flags
    monkeypatch.setitem(build_linux.PRODUCT["signing"], "linuxUpdateKey", "bm90IGEga2V5")
    with pytest.raises(BuildError, match="OPENWEIR_LINUX_UPDATE_KEY is not an Ed25519"):
        build_linux.build_service("1.2.3", "arm64")
    monkeypatch.setitem(build_linux.PRODUCT["signing"], "linuxUpdateKey", "")
    runs.clear()
    build_linux.build_service("1.2.3", "arm64")
    assert "LinuxUpdateKey" not in str(runs[0][0])


def test_cronet_for_linux_is_the_shared_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "libcronet.so").write_bytes(_elf())
    answers = iter([json.dumps({"Version": "v0.0.0-1"}), json.dumps({"Dir": str(tmp_path)})])
    monkeypatch.setattr(cronet.gomod, "go", lambda *arguments: next(answers))
    library = cronet.library("linux", "arm64")
    assert library.module == "github.com/sagernet/cronet-go/lib/linux_arm64"
    assert library.binary == tmp_path / "libcronet.so"


@pytest.mark.skipif(
    sys.platform != "linux" or not (shutil.which("objcopy") and shutil.which("readelf")),
    reason="binutils on Linux",
)
def test_symbols_split_off_with_a_debug_link(tmp_path: Path) -> None:
    binary = tmp_path / "true"
    shutil.copyfile("/bin/true", binary)
    identifier = symbols.build_id(binary)
    if identifier is None:
        pytest.skip("/bin/true has no build ID here")
    symbols.linux([binary], tmp_path / "symbols")
    assert (tmp_path / "symbols" / "true.debug").is_file()
    sections = subprocess.run(
        ["readelf", "-S", "-W", str(binary)], check=True, capture_output=True, text=True
    ).stdout
    assert ".gnu_debuglink" in sections
    assert symbols.build_id(binary) == identifier
    assert f'"true": "{identifier}"' in (tmp_path / "symbols" / "build-ids.json").read_text()


# -- what the packages install ----------------------------------------------------------------


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # type: ignore[assignment]
    parser.read_string((build_linux.PACKAGE / name).read_text(encoding="utf-8"))
    return parser


def test_the_socket_is_the_products() -> None:
    socket = _unit("openweir.socket")
    assert socket["Socket"]["ListenStream"] == product.LINUX_SOCKET
    assert socket["Socket"]["SocketMode"] == "0666"
    assert socket["Socket"]["DirectoryMode"] == "0755"
    assert socket["Install"]["WantedBy"] == "sockets.target"
    assert product.LINUX_UNIT == "openweir"


def test_the_service_runs_the_release_modes_sandboxed() -> None:
    unit = _unit("openweir.service")
    service = unit["Service"]
    path = product.LINUX_SERVICE_PATH
    assert unit["Unit"]["Requires"] == "openweir.socket"
    assert service["Type"] == "notify"
    assert service["ExecStart"] == f"{path} -system"
    # A failed clean-up does not keep the service from starting.
    assert service["ExecStartPre"] == f"-{path} -cleanup"
    assert service["ExecStopPost"] == f"{path} -cleanup"
    assert service["Restart"] == "always"
    assert service["StateDirectory"] == Path(product.LINUX_STATE_DIR).name
    assert service["StateDirectoryMode"] == "0711"
    assert service["CacheDirectory"] == "openweir"
    capabilities = set(service["CapabilityBoundingSet"].split())
    assert {"CAP_NET_ADMIN", "CAP_SYS_PTRACE", "CAP_DAC_OVERRIDE"} <= capabilities
    assert "CAP_SYS_ADMIN" not in capabilities
    assert service["DeviceAllow"] == "/dev/net/tun rw"
    assert service["ProtectSystem"] == "strict"
    # The peer check reads other users' processes; Cronet needs writable code.
    assert "ProtectProc" not in service
    assert "MemoryDenyWriteExecute" not in service
    assert unit["Install"]["WantedBy"] == "multi-user.target"
    assert unit["Install"]["Also"] == "openweir.socket"
    source = (build_linux.CORE / "service" / "service_linux.go").read_text("utf-8")
    for mode in ("system", "cleanup"):
        assert f'flags.Bool("{mode}"' in source


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
def test_systemd_accepts_the_units(tmp_path: Path) -> None:
    # verify wants the programs to exist: /bin/true stands in for the service.
    for name in ("openweir.socket", "openweir.service"):
        text = (build_linux.PACKAGE / name).read_text("utf-8")
        (tmp_path / name).write_text(text.replace(product.LINUX_SERVICE_PATH, "/bin/true"))
    done = subprocess.run(
        ["systemd-analyze", "verify", tmp_path / "openweir.socket", tmp_path / "openweir.service"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr


def test_polkit_runs_the_service_for_two_modes_only() -> None:
    root = ElementTree.parse(build_linux.PACKAGE / f"{product.BUNDLE_ID}.policy").getroot()
    actions = {action.get("id", ""): action for action in root.iter("action")}
    assert set(actions) == {f"{product.BUNDLE_ID}.take-over", f"{product.BUNDLE_ID}.install-update"}
    source = (build_linux.CORE / "service" / "service_linux.go").read_text("utf-8")
    for action_id, action in actions.items():
        defaults = action.find("defaults")
        assert defaults is not None
        assert defaults.findtext("allow_active") == "auth_admin"
        assert defaults.findtext("allow_inactive") == "no"
        assert defaults.findtext("allow_any") == "no"
        annotations = {item.get("key"): item.text for item in action.iter("annotate")}
        assert annotations["org.freedesktop.policykit.exec.path"] == product.LINUX_SERVICE_PATH
        argument = annotations["org.freedesktop.policykit.exec.argv1"] or ""
        assert argument == "-" + action_id.removeprefix(f"{product.BUNDLE_ID}.")
        assert f'flags.Bool("{argument[1:]}"' in source
        languages = [
            message.get("{http://www.w3.org/XML/1998/namespace}lang")
            for message in action.iter("message")
        ]
        assert languages == [None, "zh_CN"]


def test_the_desktop_file_opens_the_ui_for_every_url_scheme() -> None:
    text = build_linux.render_desktop_file()
    assert "#" not in text
    assert "@" not in text
    entry = configparser.ConfigParser(interpolation=None, strict=True)
    entry.optionxform = str  # type: ignore[assignment]
    entry.read_string(text)
    desktop = entry["Desktop Entry"]
    assert desktop["Type"] == "Application"
    assert desktop["Exec"] == f"{product.LINUX_UI_PATH} %u"
    assert desktop["TryExec"] == product.LINUX_UI_PATH
    assert desktop["Icon"] == product.LINUX_DESKTOP_ID
    schemes = [
        item.removeprefix("x-scheme-handler/") for item in desktop["MimeType"].split(";") if item
    ]
    assert schemes == list(product.URL_SCHEMES)
    assert desktop["MimeType"].endswith(";")


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="no desktop-file-utils")
def test_desktop_file_validate_accepts_it(tmp_path: Path) -> None:
    path = tmp_path / f"{product.LINUX_DESKTOP_ID}.desktop"
    path.write_text(build_linux.render_desktop_file())
    done = subprocess.run(["desktop-file-validate", path], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_networkmanager_leaves_the_tun_device_alone() -> None:
    conf = configparser.ConfigParser(interpolation=None, strict=True)
    conf.optionxform = str  # type: ignore[assignment]
    conf.read_string((build_linux.PACKAGE / "90-openweir.conf").read_text("utf-8"))
    # Appended to what other files set, rather than replacing it.
    assert conf["keyfile"]["unmanaged-devices+"] == "interface-name:openweir*"
    assert product.LINUX_TUN_DEVICE.startswith("openweir")


def test_the_hicolor_icons_are_generated() -> None:
    for relative, path in gen_icons.hicolor_paths().items():
        if relative.endswith(".svg"):
            assert path.read_text().startswith("<svg")
            continue
        size = int(relative.split("x", 1)[0])
        header = path.read_bytes()[:24]
        assert header[:8] == b"\x89PNG\r\n\x1a\n", relative
        assert struct.unpack(">II", header[16:24]) == (size, size), relative


# -- the nfpm configuration -------------------------------------------------------------------


def _inputs(tmp_path: Path, version: str = "1.2.3", arch: str = "x64") -> dict:
    depends = build_linux.Dependencies(("libc6 (>= 2.35)", "systemd"), ("libc.so.6()(64bit)",))
    tree = tmp_path / "openweir"
    tree.mkdir(exist_ok=True)
    config = build_linux.write_package_inputs(tmp_path / "nfpm", tree, version, arch, depends)
    return yaml.safe_load(config.read_text())


def test_nfpm_configuration(tmp_path: Path) -> None:
    config = _inputs(tmp_path)
    assert config["name"] == build_linux.PACKAGE_NAME == product.PRODUCT_ID
    assert (config["version"], config["release"], config["arch"]) == ("1.2.3", "1", "amd64")
    assert config["overrides"]["deb"]["depends"] == ["libc6 (>= 2.35)", "systemd"]
    assert config["overrides"]["rpm"]["depends"] == ["libc.so.6()(64bit)"]
    # The UI draws emoji with the system's colour font.
    assert config["overrides"]["deb"]["recommends"] == ["fonts-noto-color-emoji"]
    assert config["overrides"]["rpm"]["recommends"] == ["google-noto-color-emoji-fonts"]
    contents = {(item["dst"], item.get("packager")): item for item in config["contents"]}
    tree = contents[(product.LINUX_INSTALL_DIR, None)]
    assert tree["type"] == "tree"
    assert tree["src"] == str(tmp_path / "openweir")
    assert tree["file_info"] == {"owner": "root", "group": "root"}
    link = contents[("/usr/bin/openweir", None)]
    assert (link["type"], link["src"]) == ("symlink", product.LINUX_UI_PATH)
    for kind in build_linux.FORMATS:
        marker = contents[(product.LINUX_PACKAGE_FORMAT, kind)]
        assert Path(marker["src"]).read_text() == f"{kind}\n"
        # The updater reads the marker beside the executable.
        beside = tmp_path / "check" / kind
        beside.mkdir(parents=True)
        shutil.copyfile(marker["src"], beside / Path(product.LINUX_PACKAGE_FORMAT).name)
        executable = beside / product.LINUX_UI_EXECUTABLE
        assert releases.installed_package_format(str(executable)) == kind
    expected = {
        "/usr/lib/systemd/system/openweir.socket": "package/linux/openweir.socket",
        "/usr/lib/systemd/system/openweir.service": "package/linux/openweir.service",
        f"/usr/share/polkit-1/actions/{product.BUNDLE_ID}.policy": (
            f"package/linux/{product.BUNDLE_ID}.policy"
        ),
        "/usr/lib/NetworkManager/conf.d/90-openweir.conf": "package/linux/90-openweir.conf",
    }
    for destination, source in expected.items():
        assert contents[(destination, None)]["src"] == source
        assert (build_linux.ROOT / source).is_file()
    desktop = contents[(f"/usr/share/applications/{product.LINUX_DESKTOP_ID}.desktop", None)]
    assert Path(desktop["src"]).read_text() == build_linux.render_desktop_file()
    icons = {
        item["dst"].removeprefix("/usr/share/icons/hicolor/"): item["src"]
        for item in config["contents"]
        if item["dst"].startswith("/usr/share/icons/")
    }
    assert {
        relative: build_linux.ROOT / source for relative, source in icons.items()
    } == gen_icons.hicolor_paths()
    for item in config["contents"]:
        if item.get("type") != "symlink" and item["dst"] != product.LINUX_INSTALL_DIR:
            assert item["file_info"]["owner"] == "root"
            assert item["file_info"]["mode"] == 0o644
    for name in build_linux.SCRIPT_NAMES:
        script = Path(config["scripts"][name])
        assert script.read_text() == build_linux.render_script(name)
        assert os.access(script, os.X_OK) or sys.platform == "win32"


def test_nfpm_placeholders_all_have_values() -> None:
    assert build_linux.render_nfpm("a: @A@\nb: @B@\n", {"A": "x y", "B": [1, "z"]}) == (
        'a: "x y"\nb: [1, "z"]\n'
    )
    with pytest.raises(BuildError, match="@C@, which has no value"):
        build_linux.render_nfpm("c: @C@\n", {})
    with pytest.raises(BuildError, match="has no @D@"):
        build_linux.render_nfpm("a: @A@\n", {"A": 1, "D": 2})


def test_packages_take_three_part_versions(tmp_path: Path) -> None:
    with pytest.raises(BuildError, match=r"must be X\.Y\.Z"):
        _inputs(tmp_path, version="1.2.3-preview.1")
    assert _inputs(tmp_path, arch="arm64")["arch"] == "arm64"


# -- the maintainer scripts -------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell")
@pytest.mark.parametrize("name", build_linux.SCRIPT_NAMES)
def test_the_scripts_are_posix_shell(name: str, tmp_path: Path) -> None:
    path = tmp_path / f"{name}.sh"
    path.write_text(build_linux.render_script(name))
    done = subprocess.run(["sh", "-n", path], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    text = path.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert "\nset -e\n" in text
    assert "# @functions@" not in text


def test_the_scripts_settings_are_the_products() -> None:
    functions = build_linux.FUNCTIONS.read_text("utf-8")
    assert f"ui={product.LINUX_UI_PATH}\n" in functions
    assert f"state_dir={product.LINUX_STATE_DIR}\n" in functions
    assert "cache_dir=/var/cache/openweir\n" in functions
    with pytest.raises(BuildError, match="sets no nothing"):
        build_linux.render_script("preinstall", {"nothing": "x"})


class Root:
    """A temporary root for the maintainer scripts: its own /proc, /run/systemd/system,
    /var/lib/openweir and /var/cache/openweir, and stand-ins for systemctl, runuser and getent
    that log their arguments."""

    def __init__(self, path: Path, systemd: bool = True, enabled: str = "enabled") -> None:
        self.path = path
        self.bin = path / "bin"
        self.bin.mkdir()
        self.log = path / "log"
        self.log.write_text("")
        self.proc = path / "proc"
        self.proc.mkdir()
        if systemd:
            (path / "run" / "systemd" / "system").mkdir(parents=True)
        self.ui = path / "opt" / "openweir" / product.LINUX_UI_EXECUTABLE
        self.env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        self.env["FAKE_ENABLED"] = enabled
        self.env["FAKE_LOG"] = str(self.log)
        self._tool(
            "systemctl",
            """\
            echo "systemctl $*" >> "$FAKE_LOG"
            case "$*" in
            "is-enabled "*) echo "$FAKE_ENABLED"; [ "$FAKE_ENABLED" = enabled ] ;;
            "--quiet is-enabled "*) [ "$FAKE_ENABLED" = enabled ] ;;
            esac
            """,
        )
        self._tool(
            "runuser",
            """\
            echo "runuser $*" >> "$FAKE_LOG"
            if [ -n "$FAKE_QUITS" ]; then kill "$FAKE_QUITS"; rm -rf "$FAKE_PROC_ENTRY"; fi
            """,
        )
        self._tool("getent", 'echo "tester:x:$2:$2::/home/tester:/bin/sh"')

    def _tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        path.chmod(0o755)

    def run(self, name: str, *arguments: str, quit_wait: int = 15) -> list[str]:
        script = self.path / f"{name}.sh"
        settings = {
            "ui": str(self.ui),
            "proc": str(self.proc),
            "systemd_dir": str(self.path / "run" / "systemd" / "system"),
            "state_dir": str(self.path / "var" / "lib" / "openweir"),
            "cache_dir": str(self.path / "var" / "cache" / "openweir"),
            "quit_wait": str(quit_wait),
        }
        script.write_text(build_linux.render_script(name, settings))
        self.log.write_text("")
        done = subprocess.run(
            ["sh", script, *arguments], env=self.env, capture_output=True, text=True, timeout=60
        )
        assert done.returncode == 0, done.stderr
        self.stderr = done.stderr
        return self.log.read_text().splitlines()

    def ui_process(self) -> subprocess.Popen[bytes]:
        """A process that /proc says runs the UI."""
        process = subprocess.Popen(["sleep", "60"])
        entry = self.proc / str(process.pid)
        entry.mkdir()
        (entry / "exe").symlink_to(self.ui)
        self.env["FAKE_PROC_ENTRY"] = str(entry)
        return process


SCRIPTS = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell scripts")

INSTALL = [
    "systemctl daemon-reload",
    "systemctl is-enabled openweir.socket",
    "systemctl is-enabled openweir.service",
    "systemctl --no-reload enable openweir.service",
    "systemctl start openweir.socket openweir.service",
]


@SCRIPTS
@pytest.mark.parametrize("arguments", [("configure", ""), ("configure",), ("1",)])
def test_an_installation_enables_and_starts_the_units(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    root = Root(tmp_path, enabled="disabled")
    assert root.run("postinstall", *arguments) == INSTALL


@SCRIPTS
def test_an_installation_without_systemd_only_enables(tmp_path: Path) -> None:
    # A container or a chroot: systemctl enables offline, and nothing starts.
    root = Root(tmp_path, systemd=False, enabled="disabled")
    log = root.run("postinstall", "1")
    assert "systemctl --no-reload enable openweir.service" in log
    assert not [line for line in log if "daemon-reload" in line or " start " in line]


@SCRIPTS
@pytest.mark.parametrize("arguments", [("configure", "1.0.0"), ("2",)])
def test_an_upgrade_restarts_a_running_service(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    root = Root(tmp_path)
    assert root.run("postinstall", *arguments) == [
        "systemctl daemon-reload",
        "systemctl --quiet is-enabled openweir.socket",
        "systemctl try-restart openweir.service",
    ]


@SCRIPTS
def test_a_reinstallation_after_a_removal_enables_again(tmp_path: Path) -> None:
    # dpkg configures it with the old version, but the removal disabled the units.
    root = Root(tmp_path, enabled="disabled")
    assert root.run("postinstall", "configure", "1.0.0") == [
        "systemctl daemon-reload",
        "systemctl --quiet is-enabled openweir.socket",
        "systemctl --quiet is-enabled openweir.service",
        *INSTALL[1:],
    ]


@SCRIPTS
def test_masked_units_stay_masked(tmp_path: Path) -> None:
    root = Root(tmp_path, enabled="masked")
    assert root.run("postinstall", "configure", "") == [
        "systemctl daemon-reload",
        "systemctl is-enabled openweir.socket",
    ]
    assert "openweir.socket is masked" in root.stderr


@SCRIPTS
@pytest.mark.parametrize("arguments", [("remove",), ("0",)])
def test_a_removal_stops_and_disables(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    root = Root(tmp_path)
    assert root.run("preremove", *arguments) == [
        "systemctl stop openweir.socket openweir.service",
        "systemctl --no-reload disable openweir.service openweir.socket",
    ]
    assert root.run("postremove", *arguments) == ["systemctl daemon-reload"]


@SCRIPTS
@pytest.mark.parametrize("arguments", [("upgrade", "1.1.0"), ("1",)])
def test_an_upgrade_leaves_the_old_units_running(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    root = Root(tmp_path)
    assert root.run("preremove", *arguments) == []
    assert root.run("postremove", *arguments) == []


@SCRIPTS
def test_purge_removes_only_the_services_state_and_cache(tmp_path: Path) -> None:
    root = Root(tmp_path)
    state = tmp_path / "var" / "lib" / "openweir" / "users" / "1000"
    cache = tmp_path / "var" / "cache" / "openweir" / "update-1"
    other = tmp_path / "var" / "lib" / "other"
    for directory in (state, cache, other):
        directory.mkdir(parents=True)
        (directory / "file").write_text("x")
    assert root.run("postremove", "remove") == ["systemctl daemon-reload"]
    assert state.is_dir()
    assert cache.is_dir()
    assert root.run("postremove", "purge") == []
    assert not (tmp_path / "var" / "lib" / "openweir").exists()
    assert not (tmp_path / "var" / "cache" / "openweir").exists()
    assert (other / "file").is_file()


@SCRIPTS
@pytest.mark.parametrize("arguments", [("install",), ("1",), ("upgrade", "1.0.0"), ("2",)])
def test_without_a_running_ui_nobody_is_asked_to_quit(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    root = Root(tmp_path)
    started = time.monotonic()
    assert root.run("preinstall", *arguments) == []
    assert time.monotonic() - started < 10


@LINUX
def test_an_upgrade_asks_the_running_ui_to_quit(tmp_path: Path) -> None:
    root = Root(tmp_path)
    process = root.ui_process()
    root.env["FAKE_QUITS"] = str(process.pid)
    try:
        log = root.run("preinstall", "upgrade", "1.0.0")
        assert log == [f"runuser -u tester -- {root.ui} --quit"]
        # It quit when asked: SIGTERM from the stand-in, not from the script.
        assert process.wait(timeout=5) == -15
    finally:
        process.kill()


@LINUX
@pytest.mark.parametrize("arguments", [("2",), ("remove",)])
def test_a_ui_that_does_not_quit_gets_sigterm(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    root = Root(tmp_path)
    process = root.ui_process()
    try:
        script = "preremove" if arguments == ("remove",) else "preinstall"
        started = time.monotonic()
        log = root.run(script, *arguments, quit_wait=1)
        assert log[0] == f"runuser -u tester -- {root.ui} --quit"
        assert process.wait(timeout=5) == -15
        assert time.monotonic() - started < 10
    finally:
        process.kill()
