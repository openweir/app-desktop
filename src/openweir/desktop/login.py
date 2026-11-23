"""Open the UI when the user signs in (docs/ui.md §8).

Windows registers a scheduled task with a logon trigger for the current user; macOS
registers the app as a login item with ``SMAppService.mainAppService``; Linux writes an XDG
autostart entry into the user's ``~/.config/autostart``, which GNOME, KDE and the other
desktops start at sign-in. All only exist for the packaged app: run from source there is no
app to register, and registering the Python interpreter instead would leave a login item on
the developer's machine. So a source run gets ``UnavailableLoginItem`` and tests use doubles
or a scratch directory; the real registration is verified on the test machines with the
packaged builds.

The methods block (``schtasks`` runs as a child process): call them off the Qt thread.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from xml.sax.saxutils import escape

from openweir.desktop.objc import ObjC
from openweir.models.profiles import ProfileError
from openweir.product import DISPLAY_NAME, LINUX_DESKTOP_ID, LINUX_UI_PATH
from openweir.update.releases import installed_package_format

# Started at login: open in the tray instead of showing the window.
BACKGROUND_ARG = "--background"
# Nuitka defines __compiled__ in every module it compiles.
PACKAGED = "__compiled__" in globals()


class LoginItemError(ProfileError):
    """Registering or removing the login item failed (code ``login_item_failed``)."""

    def __init__(self, message: str) -> None:
        super().__init__("login_item_failed", message)


@dataclass(frozen=True)
class LoginItemStatus:
    enabled: bool
    # macOS: registered, but the user has to allow it under Login Items in System Settings.
    needs_approval: bool = False


class LoginItem(Protocol):
    @property
    def available(self) -> bool:
        """False when there is nothing to register, e.g. when running from source."""
        ...

    def status(self) -> LoginItemStatus: ...

    def set_enabled(self, enabled: bool) -> LoginItemStatus:
        """Register or remove the login item; returns the status afterwards."""
        ...

    def open_settings(self) -> None:
        """Show the system's list of login items (macOS); nothing elsewhere."""
        ...


class UnavailableLoginItem:
    available = False

    def status(self) -> LoginItemStatus:
        return LoginItemStatus(enabled=False)

    def set_enabled(self, enabled: bool) -> LoginItemStatus:
        raise LoginItemError("the app runs from source; only the packaged app opens at login")

    def open_settings(self) -> None:
        pass


# -- Windows: a scheduled task ---------------------------------------------------------------

TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
# Characters that a task name cannot hold.
_TASK_NAME_RESERVED = re.compile(r'[\\/:*?"<>|]')

# Runs schtasks with these arguments; returns its exit code and output.
Runner = Callable[[Sequence[str]], tuple[int, str]]
# schtasks answers in a moment; one that hangs must not keep its worker thread, which the
# interpreter waits for as it exits, and with it the quit.
SCHTASKS_TIMEOUT_S = 30.0


def run_schtasks(args: Sequence[str]) -> tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            ["schtasks", *args],
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=flags,
            check=False,
            timeout=SCHTASKS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as error:
        raise LoginItemError(f"schtasks did not finish within {SCHTASKS_TIMEOUT_S:g} s") from error
    return done.returncode, (done.stdout + done.stderr).strip()


def task_xml(command: Path, user: str) -> str:
    """A task that starts ``command`` in the tray when ``user`` signs in, as that user and
    without elevation, at normal priority and without a time limit."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{TASK_NAMESPACE}">
  <RegistrationInfo>
    <Description>{escape(f"Opens {DISPLAY_NAME} when you sign in.")}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(command))}</Command>
      <Arguments>{BACKGROUND_ARG}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def task_enabled(xml: str) -> bool:
    """Whether a task, as ``schtasks /Query /XML`` prints it, is enabled; the user can turn
    it off in Task Scheduler without deleting it."""
    root = ET.fromstring(xml)
    enabled = root.find(f"{{{TASK_NAMESPACE}}}Settings/{{{TASK_NAMESPACE}}}Enabled")
    return enabled is None or (enabled.text or "").strip().lower() != "false"


class ScheduledTask:
    available = True

    def __init__(self, command: Path, user: str, run: Runner = run_schtasks) -> None:
        """``user`` is ``DOMAIN\\name``; the task is named after it, so each user has one."""
        self._command = command
        self._user = user
        self._run = run
        self.name = _TASK_NAME_RESERVED.sub("_", f"{DISPLAY_NAME} ({user})")

    def status(self) -> LoginItemStatus:
        code, output = self._run(["/Query", "/TN", self.name, "/XML"])
        if code != 0:  # no such task
            return LoginItemStatus(enabled=False)
        try:
            return LoginItemStatus(enabled=task_enabled(output))
        except ET.ParseError as error:
            raise LoginItemError(f"unexpected schtasks output: {error}") from error

    def set_enabled(self, enabled: bool) -> LoginItemStatus:
        if enabled:
            self._create()
        elif self._exists():
            code, output = self._run(["/Delete", "/TN", self.name, "/F"])
            if code != 0:
                raise LoginItemError(output or f"schtasks exited with {code}")
        return self.status()

    def _exists(self) -> bool:
        return self._run(["/Query", "/TN", self.name])[0] == 0

    def _create(self) -> None:
        # schtasks reads the definition from a UTF-16 file.
        fd, path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(task_xml(self._command, self._user).encode("utf-16"))
            code, output = self._run(["/Create", "/TN", self.name, "/XML", path, "/F"])
        finally:
            os.unlink(path)
        if code != 0:
            raise LoginItemError(output or f"schtasks exited with {code}")

    def open_settings(self) -> None:
        pass


def windows_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    name = os.environ.get("USERNAME", "")
    return f"{domain}\\{name}" if domain else name


# -- macOS: SMAppService ---------------------------------------------------------------------

# SMAppServiceStatus
NOT_REGISTERED, ENABLED, REQUIRES_APPROVAL, NOT_FOUND = 0, 1, 2, 3


class AppService(Protocol):
    """The calls on ``SMAppService.mainAppService`` that the login item makes."""

    def status(self) -> int: ...

    def register(self) -> None: ...

    def unregister(self) -> None: ...

    def open_settings(self) -> None: ...


class MainAppLoginItem:
    available = True

    def __init__(self, service: AppService) -> None:
        self._service = service

    def status(self) -> LoginItemStatus:
        status = self._service.status()
        if status == REQUIRES_APPROVAL:
            return LoginItemStatus(enabled=True, needs_approval=True)
        return LoginItemStatus(enabled=status == ENABLED)

    def set_enabled(self, enabled: bool) -> LoginItemStatus:
        if enabled:
            self._service.register()
        elif self._service.status() in (ENABLED, REQUIRES_APPROVAL):
            self._service.unregister()
        return self.status()

    def open_settings(self) -> None:
        self._service.open_settings()


class ObjCMainAppService:
    """``SMAppService.mainAppService`` through the Objective-C runtime (macOS 13 and later),
    so the UI needs no PyObjC."""

    def __init__(self) -> None:
        self._objc = ObjC()
        ctypes.CDLL("/System/Library/Frameworks/ServiceManagement.framework/ServiceManagement")
        self._class = self._objc.cls(b"SMAppService")
        if not self._class:
            raise LoginItemError("SMAppService needs macOS 13 or later")

    @contextmanager
    def _service(self) -> Iterator[int]:
        """``mainAppService``, within an autorelease pool."""
        with self._objc.pool():
            yield self._objc.send(ctypes.c_void_p, self._class, b"mainAppService")

    def status(self) -> int:
        with self._service() as service:
            return int(self._objc.send(ctypes.c_long, service, b"status"))

    def register(self) -> None:
        self._call(b"registerAndReturnError:")

    def unregister(self) -> None:
        self._call(b"unregisterAndReturnError:")

    def open_settings(self) -> None:
        with self._objc.pool():
            self._objc.send(None, self._class, b"openSystemSettingsLoginItems")

    def _call(self, selector: bytes) -> None:
        with self._service() as service:
            error = ctypes.c_void_p()
            pointer = ctypes.POINTER(ctypes.c_void_p)
            if not self._objc.send(
                ctypes.c_bool, service, selector, ctypes.byref(error), argtypes=(pointer,)
            ):
                raise LoginItemError(self._describe(error.value))

    def _describe(self, error: int | None) -> str:
        if not error:
            return "SMAppService failed"
        text = self._objc.send(ctypes.c_void_p, error, b"localizedDescription")
        raw = self._objc.send(ctypes.c_char_p, text, b"UTF8String") if text else None
        return raw.decode("utf-8", "replace") if raw else "SMAppService failed"


# -- Linux: an XDG autostart entry ----------------------------------------------------------

AUTOSTART_NAME = f"{LINUX_DESKTOP_ID}.desktop"


def autostart_directory(environ: Mapping[str, str] = os.environ) -> Path:
    """``$XDG_CONFIG_HOME/autostart``; a relative XDG_CONFIG_HOME is to be ignored, as the
    base directory specification says."""
    config = environ.get("XDG_CONFIG_HOME", "")
    base = Path(config) if os.path.isabs(config) else Path.home() / ".config"
    return base / "autostart"


def autostart_entry(executable: str) -> str:
    """An entry that starts ``executable`` in the tray when the user signs in. TryExec makes
    desktops skip it once the package is removed; GNOME turns an entry off with
    X-GNOME-Autostart-enabled=false, KDE and the specification with Hidden=true."""
    return f"""[Desktop Entry]
Type=Application
Name={DISPLAY_NAME}
Comment=Opens {DISPLAY_NAME} when you sign in.
Exec={executable} {BACKGROUND_ARG}
TryExec={executable}
Icon={LINUX_DESKTOP_ID}
Terminal=false
X-GNOME-Autostart-enabled=true
"""


def entry_enabled(text: str) -> bool:
    """Whether an autostart entry starts anything: the user can turn one off in GNOME
    Tweaks or KDE's settings without deleting it."""
    group = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            group = line
            continue
        if group != "[Desktop Entry]" or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if (key, value.lower()) in (("Hidden", "true"), ("X-GNOME-Autostart-enabled", "false")):
            return False
    return True


class XdgAutostart:
    available = True

    def __init__(self, directory: Path, executable: str = LINUX_UI_PATH) -> None:
        """``directory`` is the user's autostart directory (``autostart_directory``)."""
        self._directory = directory
        self._executable = executable
        self.path = directory / AUTOSTART_NAME

    def status(self) -> LoginItemStatus:
        try:
            text = self.path.read_text("utf-8", errors="replace")
        except FileNotFoundError:
            return LoginItemStatus(enabled=False)
        except OSError as error:
            raise LoginItemError(f"cannot read {self.path}: {error.strerror}") from error
        return LoginItemStatus(enabled=entry_enabled(text))

    def set_enabled(self, enabled: bool) -> LoginItemStatus:
        try:
            if enabled:
                self._write()
            else:
                self.path.unlink(missing_ok=True)
        except OSError as error:
            raise LoginItemError(f"cannot change {self.path}: {error.strerror}") from error
        return self.status()

    def _write(self) -> None:
        """Replace the entry in one step, so that a desktop reading it at sign-in never
        reads half of it."""
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{AUTOSTART_NAME}.", dir=self._directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(autostart_entry(self._executable))
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def open_settings(self) -> None:
        pass


def default_login_item() -> LoginItem:
    """The packaged app's login item on this platform; unavailable from source."""
    if PACKAGED and sys.platform == "win32":
        return ScheduledTask(Path(sys.executable), windows_user())
    # The installed package, which the format marker beside the executable tells (D61).
    if PACKAGED and sys.platform == "linux" and installed_package_format() is not None:
        return XdgAutostart(autostart_directory())
    if PACKAGED and sys.platform == "darwin" and ".app/Contents/MacOS/" in sys.executable:
        try:
            return MainAppLoginItem(ObjCMainAppService())
        except OSError, LoginItemError:
            return UnavailableLoginItem()
    return UnavailableLoginItem()


def open_login_settings() -> None:
    """Open Login Items & Extensions without registering or changing a login item.

    Network extension approval lives on this page too. Use Apple's existing API rather
    than a private System Settings URL; the caller handles an unavailable framework.
    """
    if sys.platform != "darwin":
        raise LoginItemError("Login Items & Extensions is only available on macOS")
    ObjCMainAppService().open_settings()
