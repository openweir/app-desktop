"""Installing a Linux package through the service's install helper (D62).

Once the user confirmed, the UI starts a shell of its own, detached, and quits. The shell runs
``pkexec <service> -install-update <package> <SHA256SUMS> <SHA256SUMS.sig>``: polkit asks for
an administrator, and the helper copies the files where only root writes, checks them again
and installs the package with the system's package manager
(src/core/service/update_linux.go). Then the shell starts the UI again with
``--update-result=<exit code>`` (app.py), which says how it went.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from openweir import product

PKEXEC = "/usr/bin/pkexec"
INSTALL_UPDATE_ARG = "-install-update"
UPDATE_RESULT_ARG = "--update-result"

# The exit codes the shell passes on. 0 is installed; the helper's own codes are
# update_linux.go's exitRefused and exitInstallFailed, and 1 is a helper that could not start on
# its work. Only INSTALL_FAILED comes after the package manager started: with every other code,
# nothing was installed.
INSTALLED = 0
REFUSED = 3  # the files did not check out
INSTALL_FAILED = 4  # apt-get, dnf or zypper failed
DISMISSED = 126  # pkexec: the user dismissed the authentication dialog
NOT_AUTHORIZED = 127  # pkexec: not authorized, or no authentication agent to ask

# How long the shell waits for this UI to be gone before it starts the next, in seconds:
# pkexec can fail at once, as without an authentication agent, and the next UI would only hand
# its result to the one leaving (desktop/instance.py) and go.
QUIT_WAIT_S = 15

# $1 to $3 are the files, $4 this UI's process ID.
_SCRIPT = (
    '{pkexec} {service} {mode} "$1" "$2" "$3"; code=$?; '
    'i=0; while [ "$i" -lt {wait} ] && kill -0 "$4" 2>/dev/null; do sleep 1; i=$((i + 1)); done; '
    'exec {ui} {result}="$code"'
)


def install_command(
    package: Path,
    checksums: Path,
    signature: Path,
    pid: int,
    *,
    pkexec: str = PKEXEC,
    service: str = product.LINUX_SERVICE_PATH,
    ui: str = product.LINUX_UI_PATH,
) -> list[str]:
    """The shell that installs the package and then starts the UI again; tests give it stand-ins
    for pkexec, the service and the UI."""
    script = _SCRIPT.format(
        pkexec=shlex.quote(pkexec),
        service=shlex.quote(service),
        mode=INSTALL_UPDATE_ARG,
        wait=QUIT_WAIT_S,
        ui=shlex.quote(ui),
        result=UPDATE_RESULT_ARG,
    )
    files = (str(package), str(checksums), str(signature))
    return ["/bin/sh", "-c", script, "sh", *files, str(pid)]


# Starts a program, detached; returns its process ID.
type Spawn = Callable[[Sequence[str]], int]


# The shells started, which this process never waits for: they outlive it.
_detached: list[subprocess.Popen[bytes]] = []


def spawn_detached(command: Sequence[str]) -> int:
    """Start ``command`` in a session of its own, which outlives this process, with standard
    input from /dev/null and the output where this process's goes (the journal, for a UI the
    desktop started)."""
    process = subprocess.Popen(list(command), stdin=subprocess.DEVNULL, start_new_session=True)
    _detached.append(process)
    return process.pid


def start_install(
    package: Path, checksums: Path, signature: Path, spawn: Spawn = spawn_detached
) -> bool:
    """Start the install; False when the shell could not start. The UI quits next."""
    try:
        spawn(install_command(package, checksums, signature, os.getpid()))
    except OSError:
        return False
    return True
