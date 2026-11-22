"""Notarize a signed app archive or disk image, wait for Apple's verdict and staple the ticket;
only the release workflow (.github/workflows/release.yml) runs it, never a developer's machine.

    python scripts/notarize.py "$RUNNER_TEMP/OpenWeir.zip" --staple build/app/OpenWeir.app
    python scripts/notarize.py build/release/OpenWeir-1.0.0-macos-universal.dmg \\
        --staple build/release/OpenWeir-1.0.0-macos-universal.dmg

The App Store Connect API key is $SIGNING/AuthKey.p8, its id APP_STORE_CONNECT_KEY_ID and its
issuer APP_STORE_CONNECT_ISSUER_ID. When Apple does not accept the file, Apple's log is printed.

Apple's service and the network fail now and then, and a release should not. Up to ATTEMPTS
times, with a doubling wait in between: a submission without a verdict (notarytool failed, or
its WAIT ran out) is waited for again, or submitted again when it never got an id; stapling,
which fetches the ticket Apple has just issued, likewise. Invalid and Rejected are verdicts and
final.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ATTEMPTS = 3
BACKOFF_S = 60.0
WAIT = "30m"  # each attempt's wait for the verdict; Apple usually answers within minutes
VERDICTS = frozenset({"Accepted", "Invalid", "Rejected"})


def notarytool(*arguments: str) -> dict[str, Any]:
    """notarytool's JSON answer, or {} when it gave none; its errors go to stderr."""
    key = Path(os.environ["SIGNING"]) / "AuthKey.p8"
    credentials = [
        "--key",
        str(key),
        "--key-id",
        os.environ["APP_STORE_CONNECT_KEY_ID"],
        "--issuer",
        os.environ["APP_STORE_CONNECT_ISSUER_ID"],
    ]
    command = ["xcrun", "notarytool", *arguments, *credentials, "--output-format", "json"]
    done = subprocess.run(command, check=False, capture_output=True, text=True)
    if done.stderr.strip():
        print(done.stderr.strip(), file=sys.stderr, flush=True)
    try:
        answer = json.loads(done.stdout)
    except ValueError:
        answer = None
    return answer if isinstance(answer, dict) else {}


def _backoff(what: str, attempt: int) -> None:
    wait = BACKOFF_S * 2 ** (attempt - 1)
    print(
        f"::warning::{what} (attempt {attempt} of {ATTEMPTS}); trying again in {wait:.0f} s",
        flush=True,
    )
    time.sleep(wait)


def notarize(path: str) -> dict[str, Any]:
    """notarytool's last answer for ``path``: one with a verdict, unless no attempt got one."""
    name = Path(path).name
    submission: str | None = None
    answer: dict[str, Any] = {}
    for attempt in range(1, ATTEMPTS + 1):
        if submission is None:
            print(f"Submitting {name} for notarization", flush=True)
            answer = notarytool("submit", path, "--wait", "--timeout", WAIT)
        else:
            print(f"Waiting for notarization {submission} of {name}", flush=True)
            answer = notarytool("wait", submission, "--timeout", WAIT)
        submission = answer.get("id") or submission
        status = answer.get("status")
        print(f"Notarization {submission}: {status}", flush=True)
        if status in VERDICTS:
            return answer
        if attempt < ATTEMPTS:
            _backoff(f"No notarization verdict for {name}", attempt)
    return {**answer, "id": submission}


def staple(target: str) -> None:
    """Staple the ticket to ``target``; the ticket may take a moment to be served."""
    command = ["xcrun", "stapler", "staple", target]
    for attempt in range(1, ATTEMPTS + 1):
        done = subprocess.run(command, check=False)
        if done.returncode == 0:
            return
        if attempt == ATTEMPTS:
            raise subprocess.CalledProcessError(done.returncode, command)
        _backoff(f"Stapling {Path(target).name} failed", attempt)


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        sys.exit("notarize: notarization runs in the release workflow only")
    parser = argparse.ArgumentParser(description="Notarize a file and staple the ticket.")
    parser.add_argument("path", help="the signed zip of the app, or the signed disk image")
    parser.add_argument("--staple", metavar="TARGET", help="staple the ticket to this file")
    arguments = parser.parse_args(argv)
    answer = notarize(arguments.path)
    if answer.get("status") != "Accepted":
        if answer.get("status") in VERDICTS and answer.get("id"):
            print(json.dumps(notarytool("log", answer["id"]), indent=2))
        return 1
    if arguments.staple:
        staple(arguments.staple)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
