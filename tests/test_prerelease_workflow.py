"""The preview's naming step (.github/workflows/prerelease.yml, docs/release.md §1.2), run as
GitHub runs it with a stand-in gh: it publishes only one of each installer, every platform's,
after checking the Linux packages against the checksums their build wrote; a run that built
none, was superseded or is already released publishes nothing without failing."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "prerelease.yml"
HEAD = "a" * 40

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not (shutil.which("bash") and shutil.which("sha256sum")),
    reason="runs the step under bash with sha256sum, as the Linux runner does",
)

# Answers what the step asks gh: develop's tip, whether the version is released, the previews.
GH = """#!/usr/bin/env bash
case "$1 $2" in
  "api repos/openweir/app-desktop/git/ref/heads/develop") echo "$FAKE_DEVELOP" ;;
  "release view") [ -n "$FAKE_RELEASED" ] && echo true || exit 1 ;;
  "release list") echo "$FAKE_LAST_PREVIEW" ;;
  *) echo "unexpected gh $*" >&2; exit 2 ;;
esac
"""


def run_step(
    tmp_path: Path,
    installers: list[str] | None,
    *,
    develop: str = HEAD,
    released: bool = False,
    corrupt: str | None = None,
    check: bool = True,
) -> tuple[dict[str, str], str]:
    """The step's outputs and summary, with ``installers`` downloaded into dist (None: no
    artifact matched, so there is no dist), each Linux package with the .sha256 CI wrote for
    it; ``corrupt`` names one whose download no longer matches."""
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["publish"]["steps"]
    (step,) = [s for s in steps if s.get("id") == "files"]
    work = tmp_path / "work"
    work.mkdir(parents=True)
    if installers is not None:
        (work / "dist").mkdir()
        for name in installers:
            (work / "dist" / name).write_bytes(name.encode())
            if name.endswith((".deb", ".rpm")):
                digest = hashlib.sha256(name.encode()).hexdigest()
                (work / "dist" / f"{name}.sha256").write_text(f"{digest}  {name}\n")
                if name == corrupt:
                    (work / "dist" / name).write_bytes(b"damaged")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(GH, encoding="utf-8")
    gh.chmod(0o755)
    # The PATH below keeps the machine's own tools out, but the step needs sha256sum, which
    # macOS has only from Homebrew's coreutils.
    sha256sum = shutil.which("sha256sum")
    assert sha256sum is not None
    (bin_dir / "sha256sum").symlink_to(sha256sum)
    output, summary = tmp_path / "output", tmp_path / "summary"
    output.touch()
    summary.touch()
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-c", step["run"]],
        cwd=work,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_REPOSITORY": "openweir/app-desktop",
            "HEAD_SHA": HEAD,
            "VERSION": "0.1.0",
            "FAKE_DEVELOP": develop,
            "FAKE_RELEASED": "1" if released else "",
            "FAKE_LAST_PREVIEW": "3",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0
        return {"stdout": result.stdout}, summary.read_text()
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return values, summary.read_text()


MACOS_WINDOWS = [
    "OpenWeir-universal.dmg",
    "OpenWeir-0.1.0-windows-x64-setup.exe",
    "OpenWeir-0.1.0-windows-arm64-setup.exe",
]
LINUX = [
    "OpenWeir-0.1.0-linux-x64.deb",
    "OpenWeir-0.1.0-linux-x64.rpm",
    "OpenWeir-0.1.0-linux-arm64.deb",
    "OpenWeir-0.1.0-linux-arm64.rpm",
]
INSTALLERS = MACOS_WINDOWS + LINUX


def test_one_of_each_installer_makes_the_next_preview(tmp_path: Path) -> None:
    values, _ = run_step(tmp_path, INSTALLERS)
    assert values == {"tag": "v0.1.0-preview.4", "version": "0.1.0-preview.4", "publish": "true"}
    dist = tmp_path / "work" / "dist"
    names = [
        "OpenWeir-0.1.0-preview.4-linux-arm64-unsigned.deb",
        "OpenWeir-0.1.0-preview.4-linux-arm64-unsigned.rpm",
        "OpenWeir-0.1.0-preview.4-linux-x64-unsigned.deb",
        "OpenWeir-0.1.0-preview.4-linux-x64-unsigned.rpm",
        "OpenWeir-0.1.0-preview.4-macos-universal-unsigned.dmg",
        "OpenWeir-0.1.0-preview.4-windows-arm64-unsigned-setup.exe",
        "OpenWeir-0.1.0-preview.4-windows-x64-unsigned-setup.exe",
    ]
    # The packages' .sha256 files are not published; SHA256SUMS lists every file.
    assert sorted(path.name for path in dist.iterdir()) == [*names, "SHA256SUMS"]
    sums = (dist / "SHA256SUMS").read_text().splitlines()
    assert sorted(line.split("  ")[1] for line in sums) == names
    # Each renamed file is still what was downloaded.
    linux = dist / "OpenWeir-0.1.0-preview.4-linux-arm64-unsigned.rpm"
    assert linux.read_bytes() == b"OpenWeir-0.1.0-linux-arm64.rpm"


def test_a_linux_package_that_changed_on_the_way_fails_the_job(tmp_path: Path) -> None:
    values, _ = run_step(tmp_path, INSTALLERS, corrupt=LINUX[3], check=False)
    assert (
        "::error::OpenWeir-0.1.0-linux-arm64.rpm does not match the checksum its build wrote"
        in values["stdout"]
    )
    assert not (tmp_path / "output").read_text()


@pytest.mark.parametrize(
    ("installers", "reason"),
    [
        (None, "built no installer"),  # documentation only: download-artifact made no dist
        ([], "built no installer"),
        (INSTALLERS[:1], "Not one of each installer"),
        (INSTALLERS[1:], "Not one of each installer"),
        # Every platform's, Linux's too, or none: the preview replaces the previous one.
        (MACOS_WINDOWS, "Not one of each installer"),
        (LINUX, "Not one of each installer"),
        (INSTALLERS[:-1], "Not one of each installer"),
        ([*INSTALLERS, "OpenWeir-0.1.1-linux-x64.deb"], "Not one of each installer"),
    ],
)
def test_an_incomplete_run_publishes_nothing(
    tmp_path: Path, installers: list[str] | None, reason: str
) -> None:
    values, summary = run_step(tmp_path, installers)
    assert values == {"publish": "false"}
    assert reason in summary


def test_a_superseded_or_released_commit_publishes_nothing(tmp_path: Path) -> None:
    values, summary = run_step(tmp_path / "superseded", None, develop="b" * 40)
    assert values == {"publish": "false"}
    assert "superseded" in summary
    values, summary = run_step(tmp_path / "released", INSTALLERS, released=True)
    assert values == {"publish": "false"}
    assert "already released" in summary


def test_the_notes_say_how_to_install_on_linux() -> None:
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["publish"]["steps"]
    (publish,) = [s for s in steps if s.get("name") == "Publish the pre-release"]
    notes = publish["run"]
    for command in (
        "sudo apt install ./OpenWeir-$VERSION-linux-x64-unsigned.deb",
        "sudo dnf install ./OpenWeir-$VERSION-linux-x64-unsigned.rpm",
    ):
        # Once in English, once in Chinese.
        assert notes.count(command) == 2, command
    assert "**Previews are unsigned**" in notes
    assert "**预览版不签名**" in notes
    assert "--reinstall" in notes
