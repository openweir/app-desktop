"""What every workflow holds to: actions pinned to a commit, with the version they are as a
comment for Dependabot (.github/dependabot.yml); CI's token read-only unless a job asks for
more, and its check jobs' checkouts keep no credentials."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
USES = re.compile(r"^\s*(?:- )?uses:\s*(\S+)(.*)$", re.MULTILINE)


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_every_action_is_pinned_to_a_commit() -> None:
    found = 0
    for path in WORKFLOWS:
        for action, rest in USES.findall(path.read_text(encoding="utf-8")):
            if action.startswith("./"):
                continue
            found += 1
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action), (path.name, action)
            assert re.fullmatch(r"\s+# v\d+\.\d+\.\d+", rest), (path.name, action, rest)
    assert found > 40


def test_dependabot_updates_the_actions_on_develop() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    (update,) = config["updates"]
    assert update["package-ecosystem"] == "github-actions"
    assert update["target-branch"] == "develop"
    assert update["schedule"]["interval"] == "monthly"
    assert update["groups"]["actions"]["patterns"] == ["*"]
    assert update["commit-message"]["prefix"] == "ci"


def test_ci_is_read_only_and_its_checks_keep_no_credentials() -> None:
    ci = _load(ROOT / ".github" / "workflows" / "ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    checkouts = 0
    for name, job in ci["jobs"].items():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/checkout@"):
                checkouts += 1
                assert step["with"]["persist-credentials"] == "false", name
    assert checkouts >= 9


def test_the_linux_test_jobs_install_qts_system_libraries_first() -> None:
    """PySide6's Qt loads libEGL, libxkbcommon, fontconfig and a few more from the system,
    which a runner image need not have: each job that runs pytest on Linux too installs them,
    the same way, before the tests, which draw offscreen."""
    jobs = _load(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]
    installs = set()
    for name in ("python", "e2e"):
        job = jobs[name]
        matrix = job["strategy"]["matrix"]
        systems = matrix.get("os") or [entry["os"] for entry in matrix["include"]]
        assert any(system.startswith("ubuntu-") for system in systems), name
        steps = job["steps"]
        names = [step.get("name") for step in steps]
        assert names.count("Qt system libraries") == 1, name
        install = names.index("Qt system libraries")
        assert steps[install]["if"] == "runner.os == 'Linux'"
        installs.add(steps[install]["run"])
        tests = [i for i, step in enumerate(steps) if "pytest" in step.get("run", "")]
        assert tests, name
        assert install < min(tests), name
        assert {steps[i]["env"]["QT_QPA_PLATFORM"] for i in tests} == {"offscreen"}, name
    assert len(installs) == 1


def test_every_job_has_a_timeout() -> None:
    for path in WORKFLOWS:
        for name, job in _load(path)["jobs"].items():
            if "uses" in job:  # a reusable workflow's jobs carry their own
                continue
            assert int(job["timeout-minutes"]) > 0, (path.name, name)
