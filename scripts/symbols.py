"""Retain build-matched symbols as CI artifacts, separate from end-user installers."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


def apple(binary: Path, dsym: Path, destination: Path) -> None:
    def uuids(path: Path) -> set[str]:
        result = subprocess.run(
            ["xcrun", "dwarfdump", "--uuid", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return set(re.findall(r"UUID: ([A-Fa-f0-9-]+)", result.stdout))

    identifiers = uuids(binary)
    if not identifiers or identifiers != uuids(dsym):
        raise RuntimeError(f"symbols do not match {binary.name}")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / dsym.name
    if dsym.resolve() != target.resolve():
        shutil.copytree(dsym, target, dirs_exist_ok=True)
    (destination / f"{binary.name}.json").write_text(
        json.dumps({"binary": binary.name, "uuids": sorted(identifiers)}, indent=2) + "\n"
    )


def windows(ui: Path, service: Path, build: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    pdbs = list(build.rglob("*.pdb"))
    if not pdbs:
        raise RuntimeError("the unstripped Windows UI build produced no PDB symbols")
    for path in pdbs:
        target = destination / "pdb" / path.relative_to(build)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    # Keep the exact executables (Go retains its DWARF information) to match minidumps.
    hashes = {}
    for path in (ui, service):
        shutil.copyfile(path, destination / path.name)
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (destination / "binaries.json").write_text(json.dumps(hashes, indent=2) + "\n")


def build_id(path: Path) -> str | None:
    """The GNU build ID of an ELF file (readelf -n), which ties a core dump to its binary and to
    the debug information split off it; None without one."""
    notes = subprocess.run(
        ["readelf", "-n", str(path)], check=True, capture_output=True, text=True
    ).stdout
    found = re.search(r"Build ID: ([0-9a-f]+)", notes)
    return found.group(1) if found else None


def linux(binaries: list[Path], destination: Path) -> None:
    """Move each binary's debug information to <destination>/<name>.debug and leave a
    .gnu_debuglink to it: the package ships the binaries with their symbol tables but without
    DWARF. Both halves must carry the same build ID, which build-ids.json records."""
    destination.mkdir(parents=True, exist_ok=True)
    identifiers = {}
    for binary in binaries:
        identifier = build_id(binary)
        if identifier is None:
            raise RuntimeError(f"{binary.name} has no build ID")
        debug = destination / f"{binary.name}.debug"
        for command in (
            ["objcopy", "--only-keep-debug", str(binary), str(debug)],
            ["objcopy", "--strip-debug", f"--add-gnu-debuglink={debug}", str(binary)],
        ):
            subprocess.run(command, check=True)
        if build_id(binary) != identifier or build_id(debug) != identifier:
            raise RuntimeError(f"symbols do not match {binary.name}")
        identifiers[binary.name] = identifier
    (destination / "build-ids.json").write_text(json.dumps(identifiers, indent=2) + "\n")
