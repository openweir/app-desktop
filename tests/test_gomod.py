"""The builds' Go module downloads (scripts/gomod.py): goproxy.cn only on a developer's machine
that names no proxy, and passing download failures tried again before they fail a build."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from scripts import gomod


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, gomod.GOPROXY_CN),  # a developer's machine
        ({"CI": "false"}, gomod.GOPROXY_CN),
        ({"CI": "true"}, None),  # GitHub Actions: the go command's own default
        ({"GOPROXY": "https://proxy.golang.org,direct"}, "https://proxy.golang.org,direct"),
        ({"CI": "true", "GOPROXY": "off"}, "off"),
    ],
)
def test_goproxy_cn_only_outside_ci_without_a_proxy_of_ones_own(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], expected: str | None
) -> None:
    for name in ("CI", "GOPROXY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    environment = gomod.environment(GOOS="windows")
    assert environment.get("GOPROXY") == expected
    assert environment["GOOS"] == "windows"


class FakeGo:
    """subprocess.run for go: fails the first ``failures`` times."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        assert options["env"]["GOOS"] == "darwin"
        if len(self.commands) <= self.failures:
            return subprocess.CompletedProcess(command, 1, "", "502 Bad Gateway")
        return subprocess.CompletedProcess(command, 0, "ok\n", "")


def test_a_passing_failure_is_tried_again_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    go = FakeGo(failures=2)
    waits: list[float] = []
    monkeypatch.setattr(gomod.subprocess, "run", go)
    monkeypatch.setattr(gomod.time, "sleep", waits.append)
    gomod.download("./apple", "with_gvisor", gomod.environment(GOOS="darwin"))
    assert waits == [gomod.BACKOFF_S, 2 * gomod.BACKOFF_S]
    assert len(go.commands) == 3
    assert go.commands[0][-5:] == ["list", "-deps", "-tags", "with_gvisor", "./apple"]
    assert go.commands[0][:3] == ["go", "-C", str(gomod.CORE)]


def test_it_gives_up_after_the_last_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    go = FakeGo(failures=gomod.ATTEMPTS)
    monkeypatch.setattr(gomod.subprocess, "run", go)
    monkeypatch.setattr(gomod.time, "sleep", lambda _: None)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        gomod.go("mod", "download", env=gomod.environment(GOOS="darwin"))
    assert raised.value.stderr == "502 Bad Gateway"
    assert len(go.commands) == gomod.ATTEMPTS
