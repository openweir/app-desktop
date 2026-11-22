"""Go modules for the builds (scripts/build_apple.py, build_app.py, build_windows.py, cronet.py,
gen_licenses.py): the proxy they come from, and fetching them with retries before a build.

On a developer's machine whose environment sets no GOPROXY, the go commands use goproxy.cn,
which answers from China where proxy.golang.org does not. In CI (CI is set, as GitHub Actions
sets it) or with a GOPROXY of one's own, the go command's own setting stands: GitHub's runners
reach proxy.golang.org directly, and goproxy.cn's 5xx and timeouts from there failed builds.

A proxy's passing failure should not cost a build of an hour either, so before a build
download() loads the packages it compiles (go list -deps), which puts exactly the modules it
needs in the module cache, trying again with backoff. The build then finds them there. A plain
`go mod download` would fetch every module go.mod lists, Cronet's native libraries for Android,
iOS and Linux among them.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "src" / "core"
GOPROXY_CN = "https://goproxy.cn,direct"
ATTEMPTS = 3
BACKOFF_S = 15.0


def _in_ci(environ: Mapping[str, str]) -> bool:
    return environ.get("CI", "").strip().lower() not in ("", "0", "false")


def environment(**overrides: str) -> dict[str, str]:
    """The process environment for a go command, with GOPROXY set to goproxy.cn outside CI
    when it names none, then ``overrides``."""
    env = dict(os.environ)
    if not env.get("GOPROXY") and not _in_ci(env):
        env["GOPROXY"] = GOPROXY_CN
    env.update(overrides)
    return env


def go(
    *arguments: str,
    env: Mapping[str, str] | None = None,
    attempts: int = ATTEMPTS,
    backoff: float = BACKOFF_S,
) -> str:
    """The output of ``go -C src/core <arguments>``, run up to ``attempts`` times with a
    doubling wait between them. Only for commands that download or read modules, where a
    failure may pass: a build's errors are its own."""
    command = ["go", "-C", str(CORE), *arguments]
    environ = dict(env) if env is not None else environment()
    for attempt in range(1, attempts + 1):
        done = subprocess.run(command, env=environ, capture_output=True, text=True, check=False)
        if done.returncode == 0:
            return done.stdout
        print(done.stderr.strip(), file=sys.stderr, flush=True)
        if attempt == attempts:
            raise subprocess.CalledProcessError(done.returncode, command, done.stdout, done.stderr)
        wait = backoff * 2 ** (attempt - 1)
        print(
            f"go {' '.join(arguments)} failed (attempt {attempt} of {attempts}); "
            f"trying again in {wait:.0f} s",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(wait)
    raise AssertionError("unreachable")


def download(package: str, tags: str, env: Mapping[str, str] | None = None) -> None:
    """Put the modules that ``package`` needs, built with ``tags`` for the GOOS and GOARCH in
    ``env``, in the module cache: go list -deps loads each package, so the go command fetches
    its module."""
    go("list", "-deps", "-tags", tags, package, env=env)
