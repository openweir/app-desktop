"""Notarization in the release workflow (scripts/notarize.py) against a stand-in notarytool and
stapler: a submission without a verdict is waited for or submitted again, a verdict is final,
and stapling is tried again."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import notarize


class Xcrun:
    """subprocess.run for xcrun: notarytool answers in turn from ``answers`` (a dict is its
    JSON, None no answer), stapler fails ``staple_failures`` times."""

    def __init__(self, answers: list[dict[str, Any] | None], staple_failures: int = 0) -> None:
        self.answers = answers
        self.staple_failures = staple_failures
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if command[1] == "stapler":
            failed = sum(call[1] == "stapler" for call in self.calls) <= self.staple_failures
            return subprocess.CompletedProcess(command, 65 if failed else 0)
        if command[2] == "log":
            return subprocess.CompletedProcess(command, 0, json.dumps({"issues": ["x"]}), "")
        answer = self.answers.pop(0)
        if answer is None:
            return subprocess.CompletedProcess(command, 1, "", "HTTP 503")
        return subprocess.CompletedProcess(command, 0, json.dumps(answer), "")

    def notarytool(self) -> list[list[str]]:
        return [call[2:4] for call in self.calls if call[1] == "notarytool"]


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[float]:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("SIGNING", str(tmp_path))
    monkeypatch.setenv("APP_STORE_CONNECT_KEY_ID", "KEY")
    monkeypatch.setenv("APP_STORE_CONNECT_ISSUER_ID", "ISSUER")
    slept: list[float] = []
    monkeypatch.setattr(notarize.time, "sleep", slept.append)
    return slept


def test_an_accepted_file_is_stapled(monkeypatch: pytest.MonkeyPatch, waits: list[float]) -> None:
    xcrun = Xcrun([{"id": "1", "status": "Accepted"}])
    monkeypatch.setattr(notarize.subprocess, "run", xcrun)
    assert notarize.main(["OpenWeir.zip", "--staple", "OpenWeir.app"]) == 0
    assert xcrun.notarytool() == [["submit", "OpenWeir.zip"]]
    assert xcrun.calls[-1] == ["xcrun", "stapler", "staple", "OpenWeir.app"]
    assert waits == []


def test_a_submission_without_a_verdict_is_waited_for_again(
    monkeypatch: pytest.MonkeyPatch, waits: list[float]
) -> None:
    xcrun = Xcrun([None, {"id": "7", "status": "In Progress"}, {"id": "7", "status": "Accepted"}])
    monkeypatch.setattr(notarize.subprocess, "run", xcrun)
    assert notarize.main(["OpenWeir.dmg"]) == 0
    # No id from the first: submitted again; the second has one: waited for, not resubmitted.
    assert xcrun.notarytool() == [
        ["submit", "OpenWeir.dmg"],
        ["submit", "OpenWeir.dmg"],
        ["wait", "7"],
    ]
    assert waits == [notarize.BACKOFF_S, 2 * notarize.BACKOFF_S]
    assert not [call for call in xcrun.calls if call[1] == "stapler"]


def test_a_verdict_is_final_and_its_log_printed(
    monkeypatch: pytest.MonkeyPatch, waits: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    xcrun = Xcrun([{"id": "9", "status": "Invalid"}])
    monkeypatch.setattr(notarize.subprocess, "run", xcrun)
    assert notarize.main(["OpenWeir.dmg", "--staple", "OpenWeir.dmg"]) == 1
    assert xcrun.notarytool() == [["submit", "OpenWeir.dmg"], ["log", "9"]]
    assert '"issues"' in capsys.readouterr().out
    assert waits == []


def test_no_verdict_after_every_attempt_fails(
    monkeypatch: pytest.MonkeyPatch, waits: list[float]
) -> None:
    xcrun = Xcrun([None] * notarize.ATTEMPTS)
    monkeypatch.setattr(notarize.subprocess, "run", xcrun)
    assert notarize.main(["OpenWeir.dmg"]) == 1
    assert len(xcrun.notarytool()) == notarize.ATTEMPTS
    assert len(waits) == notarize.ATTEMPTS - 1


def test_stapling_is_tried_again(monkeypatch: pytest.MonkeyPatch, waits: list[float]) -> None:
    xcrun = Xcrun([{"id": "1", "status": "Accepted"}], staple_failures=2)
    monkeypatch.setattr(notarize.subprocess, "run", xcrun)
    assert notarize.main(["OpenWeir.dmg", "--staple", "OpenWeir.dmg"]) == 0
    assert waits == [notarize.BACKOFF_S, 2 * notarize.BACKOFF_S]
    xcrun = Xcrun([{"id": "1", "status": "Accepted"}], staple_failures=notarize.ATTEMPTS)
    monkeypatch.setattr(notarize.subprocess, "run", xcrun)
    with pytest.raises(subprocess.CalledProcessError):
        notarize.main(["OpenWeir.dmg", "--staple", "OpenWeir.dmg"])


def test_it_runs_only_in_the_release_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit, match="release workflow only"):
        notarize.main(["OpenWeir.dmg"])
