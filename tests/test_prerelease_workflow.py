"""The preview's naming step (.github/workflows/prerelease.yml, docs/release.md §1.2), run as
GitHub runs it with a stand-in gh: it publishes only one of each installer, and a run that
built none, was superseded or is already released publishes nothing without failing."""

from __future__ import annotations

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
) -> tuple[dict[str, str], str]:
    """The step's outputs and summary, with ``installers`` downloaded into dist (None: no
    artifact matched, so there is no dist)."""
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["publish"]["steps"]
    (step,) = [s for s in steps if s.get("id") == "files"]
    work = tmp_path / "work"
    work.mkdir(parents=True)
    if installers is not None:
        (work / "dist").mkdir()
        for name in installers:
            (work / "dist" / name).write_bytes(name.encode())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(GH, encoding="utf-8")
    gh.chmod(0o755)
    output, summary = tmp_path / "output", tmp_path / "summary"
    output.touch()
    summary.touch()
    subprocess.run(
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
        check=True,
        capture_output=True,
        text=True,
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return values, summary.read_text()


INSTALLERS = [
    "OpenWeir-universal.dmg",
    "OpenWeir-0.1.0-windows-x64-setup.exe",
    "OpenWeir-0.1.0-windows-arm64-setup.exe",
]


def test_one_of_each_installer_makes_the_next_preview(tmp_path: Path) -> None:
    values, _ = run_step(tmp_path, INSTALLERS)
    assert values == {"tag": "v0.1.0-preview.4", "version": "0.1.0-preview.4", "publish": "true"}
    dist = tmp_path / "work" / "dist"
    names = [
        "OpenWeir-0.1.0-preview.4-macos-universal-unsigned.dmg",
        "OpenWeir-0.1.0-preview.4-windows-arm64-unsigned-setup.exe",
        "OpenWeir-0.1.0-preview.4-windows-x64-unsigned-setup.exe",
    ]
    assert sorted(path.name for path in dist.iterdir()) == [*names, "SHA256SUMS"]
    sums = (dist / "SHA256SUMS").read_text().splitlines()
    assert sorted(line.split("  ")[1] for line in sums) == names


@pytest.mark.parametrize(
    ("installers", "reason"),
    [
        (None, "built no installer"),  # documentation only: download-artifact made no dist
        ([], "built no installer"),
        (INSTALLERS[:1], "Not one of each installer"),
        (INSTALLERS[1:], "Not one of each installer"),
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
