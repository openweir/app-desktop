"""The next release version, from git: the newest stable vX.Y.Z tag reachable from HEAD and
the conventional commits after it (docs/release.md). A breaking change (type!: or a
BREAKING CHANGE: footer) raises the major version, a feat the minor, anything else the patch;
a tagged HEAD keeps its own version. Mirrors openwrt's internal/release/nextversion.go. Until
the first release is tagged, the version is pyproject.toml's (the first release is 0.1.0).

    uv run --no-project python scripts/next_version.py           # print it
    uv run --no-project python scripts/next_version.py --write   # also set it in pyproject.toml

CI checks out with fetch-depth: 0 so the tags and history are there.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"

VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
STABLE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
CONVENTIONAL = re.compile(r"^([a-z]+)(\([^)]*\))?(!)?:")
PROJECT_VERSION = re.compile(r'^(version\s*=\s*)"[^"]*"', re.MULTILINE)


def bump(latest: str, messages: list[str]) -> str:
    """The version after latest for these commit messages."""
    m = VERSION.match(latest)
    if m is None:
        raise ValueError(f"invalid release version {latest!r}")
    major, minor, patch = (int(g) for g in m.groups())
    level = 0
    for message in messages:
        subject, _, body = message.partition("\n")
        c = CONVENTIONAL.match(subject)
        if (c and c[3]) or "BREAKING CHANGE:" in body or "BREAKING-CHANGE:" in body:
            level = 3
        elif c and c[1] == "feat":
            level = max(level, 2)
        else:
            level = max(level, 1)
    if level == 3:
        return f"{major + 1}.0.0"
    if level == 2:
        return f"{major}.{minor + 1}.0"
    if level == 1:
        return f"{major}.{minor}.{patch + 1}"
    return latest


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def project_version(pyproject: Path = PYPROJECT) -> str:
    """[project] version in pyproject.toml."""
    with pyproject.open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    if not isinstance(version, str) or VERSION.match(version) is None:
        raise ValueError(f"invalid release version {version!r} in {pyproject}")
    return version


def next_version(cwd: Path = ROOT) -> str:
    """The next version from the repository at cwd."""
    tags = _git(cwd, "tag", "--merged", "HEAD", "--list", "v*", "--sort=-v:refname").split()
    latest = next((t for t in tags if STABLE_TAG.match(t)), None)
    if latest is None:
        # A shallow clone may just lack the tags; a full one has no release yet, so the first
        # is the version pyproject.toml names.
        if _git(cwd, "rev-parse", "--is-shallow-repository").strip() == "true":
            raise RuntimeError("no stable vX.Y.Z tag in a shallow clone; fetch full history")
        return project_version(cwd / "pyproject.toml")
    log = _git(cwd, "log", "--format=%B%x00", f"{latest}..HEAD")
    messages = [m.strip() for m in log.split("\0") if m.strip()]
    return bump(latest[1:], messages)


def write_version(version: str, pyproject: Path = PYPROJECT) -> None:
    """Set [project] version in pyproject.toml (its first version = line)."""
    text = pyproject.read_text(encoding="utf-8")
    new, count = PROJECT_VERSION.subn(rf'\g<1>"{version}"', text, count=1)
    if count != 1:
        raise RuntimeError(f"no version line in {pyproject}")
    pyproject.write_text(new, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("--write", action="store_true", help="also set it in pyproject.toml")
    args = parser.parse_args()
    version = next_version()
    if args.write:
        write_version(version)
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
