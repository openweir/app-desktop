"""The release version from git (scripts/next_version.py): the bump rules, the tag it starts
from, and writing it into pyproject.toml."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from scripts import next_version as nv

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        ([], "1.2.3"),
        (["fix: crash"], "1.2.4"),
        (["docs: typo", "chore(ci): cache"], "1.2.4"),
        (["not conventional at all"], "1.2.4"),
        (["fix: a", "feat(ui): b"], "1.3.0"),
        (["feat!: drop old config"], "2.0.0"),
        (["refactor(core)!: rename"], "2.0.0"),
        (["fix: a\n\nBREAKING CHANGE: config moved"], "2.0.0"),
        (["fix: a\n\nBREAKING-CHANGE: config moved"], "2.0.0"),
        (["feat!: x", "fix: y"], "2.0.0"),
        (["Feat: capitalised is not a feat"], "1.2.4"),
    ],
)
def test_bump(messages: list[str], expected: str) -> None:
    assert nv.bump("1.2.3", messages) == expected


@pytest.mark.parametrize("bad", ["v1.2.3", "1.2", "01.2.3", "1.2.3-preview.1"])
def test_bump_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid release version"):
        nv.bump(bad, [])


def _repo(tmp_path: Path) -> Callable[..., None]:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    git("config", "tag.gpgsign", "false")
    return git


def test_next_version_from_tags(tmp_path: Path) -> None:
    git = _repo(tmp_path)
    git("commit", "-q", "--allow-empty", "-m", "feat: first")
    git("tag", "v1.4.0")
    assert nv.next_version(tmp_path) == "1.4.0"  # HEAD is the tag
    git("commit", "-q", "--allow-empty", "-m", "fix: one")
    git("tag", "v1.4.1-preview.1")  # previews are not a base
    git("tag", "v1.10.0-rc")  # nor anything else that is not vX.Y.Z
    assert nv.next_version(tmp_path) == "1.4.1"
    git("commit", "-q", "--allow-empty", "-m", "feat(ui): two")
    assert nv.next_version(tmp_path) == "1.5.0"


def test_before_the_first_release_the_version_is_pyprojects(tmp_path: Path) -> None:
    git = _repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "openweir"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    git("add", "pyproject.toml")
    git("commit", "-q", "-m", "feat: first")
    git("tag", "v0.1.0-preview.3")  # a preview is no release
    git("commit", "-q", "--allow-empty", "-m", "feat!: second")
    assert nv.next_version(tmp_path) == "0.1.0"
    # Once the first release is tagged, the commits after it count.
    git("tag", "v0.1.0")
    git("commit", "-q", "--allow-empty", "-m", "fix: third")
    assert nv.next_version(tmp_path) == "0.1.1"


def test_a_shallow_clone_without_tags_is_an_error(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    git = _repo(origin)
    (origin / "pyproject.toml").write_text(
        '[project]\nname = "openweir"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    git("add", "pyproject.toml")
    git("commit", "-q", "-m", "feat: first")
    git("tag", "v1.4.0")
    git("commit", "-q", "--allow-empty", "-m", "fix: second")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--no-tags", origin.as_uri(), str(clone)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(RuntimeError, match="shallow clone"):
        nv.next_version(clone)


@pytest.mark.parametrize("bad", ["1.0", "v0.1.0", "0.1.0.dev1"])
def test_pyproject_must_name_a_release_version(tmp_path: Path, bad: str) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f'[project]\nversion = "{bad}"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid release version"):
        nv.project_version(pyproject)


def test_write_version(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "openweir"\nversion = "0.1.0"\n\n[tool.x]\nversion = "9"\n',
        encoding="utf-8",
    )
    nv.write_version("1.5.0", pyproject)
    text = pyproject.read_text(encoding="utf-8")
    assert 'version = "1.5.0"' in text
    assert 'version = "9"' in text


@pytest.mark.parametrize(
    ("workflow", "jobs"),
    [
        ("prerelease.yml", ["publish"]),
        ("release.yml", ["macos", "windows", "release"]),
        ("ci.yml", ["apple", "windows-installer"]),
    ],
)
def test_workflows_compute_the_version(workflow: str, jobs: list[str]) -> None:
    """Jobs that name or build a version check out full history and write it first."""
    doc = yaml.load(
        (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    for job in jobs:
        steps = doc["jobs"][job]["steps"]
        checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))
        assert checkout["with"]["fetch-depth"] == "0", (workflow, job)
        assert any("scripts/next_version.py --write" in s.get("run", "") for s in steps), (
            workflow,
            job,
        )


def _load(name: str) -> dict:
    return yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def test_ci_publishes_as_its_last_jobs() -> None:
    """Previews and releases are called from CI after every check, never by workflow_run or
    a tag push, so they run this commit's copy of the workflow."""
    jobs = _load("ci.yml")["jobs"]
    for job, workflow, branch in [
        ("preview", "prerelease.yml", "develop"),
        ("release", "release.yml", "main"),
        ("remove-previews", "remove-previews.yml", "main"),
    ]:
        assert jobs[job]["uses"] == f"./.github/workflows/{workflow}"
        assert jobs[job]["needs"] == ["result"]
        assert f"refs/heads/{branch}" in jobs[job]["if"]
        on = _load(workflow)["on"]
        assert "workflow_call" in on
        assert "workflow_run" not in on
        assert "push" not in on
    assert jobs["release"]["secrets"] == "inherit"


def test_release_builds_wait_for_qualify() -> None:
    jobs = _load("release.yml")["jobs"]
    assert "--write" not in jobs["qualify"]["steps"][-1]["run"]
    for job in ("macos", "windows"):
        assert jobs[job]["needs"] == "qualify"
        assert jobs[job]["environment"] == "release"
        assert "publish" in jobs[job]["if"]
    assert jobs["release"]["needs"] == ["qualify", "macos", "windows"]
