"""An administrator taking over another user's tunnel (decisions D5b and D66,
docs/architecture.md §5.4 and "Linux").

On Windows the UI runs with the user's filtered token, which the service does not let take
over. Once the user confirmed it, the UI starts the service's own executable as the take-over
helper through UAC (``ShellExecuteEx`` with the ``runas`` verb); the service accepts that
executable on its pipe only with an administrator's rights. Starting it blocks until UAC's
prompt is answered and the helper is done, or HELPER_WAIT_S passed: run it on a daemon thread
(host/workers.py), since nothing bounds how long the prompt waits for the user.

On Linux the UI runs the helper through pkexec, whose polkit action asks for an
administrator's password (auth_admin). The helper connects as root, which the service accepts
from its own executable only; the service lets go of the other user and restarts with no
owner, and this UI then claims it. pkexec blocks until the dialog is answered and the helper
is done, so it runs on a daemon thread too, for at most PKEXEC_WAIT_S.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from ctypes import wintypes
from pathlib import Path

from openweir import product
from openweir.host.base import HostError
from openweir.update.linux import DISMISSED, NOT_AUTHORIZED, PKEXEC

SERVICE_EXECUTABLE = product.SERVICE_EXECUTABLE
TAKE_OVER_ARG = "-take-over"
# The groups whose members polkit's auth_admin asks for their own password: Debian's and
# Ubuntu's sudo, Fedora's and openSUSE's wheel, and admin on older Ubuntu releases. Anyone
# else would be asked for another account's password, which is not a take-over by them.
ADMIN_GROUPS = frozenset({"sudo", "wheel", "admin"})
# How long the UI waits for pkexec: the user types a password into polkit's dialog, then the
# helper waits up to a minute for the service (service_linux.go's takeOverWait).
PKEXEC_WAIT_S = 300.0

# Runs a command; returns its exit code and what it wrote to standard error.
Runner = Callable[[Sequence[str]], tuple[int, str]]

_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION_TYPE = 18  # TOKEN_INFORMATION_CLASS
_ELEVATION_TYPE_FULL, _ELEVATION_TYPE_LIMITED = 2, 3
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SEE_MASK_NOASYNC = 0x00000100
_SW_HIDE = 0
_WAIT_OBJECT_0, _WAIT_TIMEOUT = 0, 0x102
# The helper dials the pipe for up to 10 s and the service answers its claim within the
# claim's 30 s; past this the UI stops waiting for it.
HELPER_WAIT_S = 60.0
_COINIT_APARTMENTTHREADED = 0x2
_COINIT_DISABLE_OLE1DDE = 0x4
_ERROR_CANCELLED = 1223  # the user declined UAC's prompt


class _ShellExecuteInfo(ctypes.Structure):  # SHELLEXECUTEINFOW
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    )


def service_executable() -> Path:
    """The service's executable: on Windows beside the installed UI (docs/architecture.md
    §5.1), on Linux where the package installs it, the path its polkit action names."""
    if sys.platform == "linux":
        return Path(product.LINUX_SERVICE_PATH)
    return Path(sys.executable).resolve().parent / SERVICE_EXECUTABLE


def helper_available() -> bool:
    """Whether the take-over helper can run: the service's executable is installed, and on
    Linux pkexec too."""
    if sys.platform == "linux" and not Path(PKEXEC).is_file():
        return False
    return service_executable().is_file()


def user_groups() -> list[str]:
    """The names of the current user's groups as the user database lists them, as polkit
    reads them, rather than the ones this process started with."""
    if sys.platform == "win32":
        return []
    import grp
    import pwd

    user = pwd.getpwuid(os.getuid())
    names = []
    for gid in os.getgrouplist(user.pw_name, user.pw_gid):
        try:
            names.append(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue  # a group without a name
    return names


def is_admin_group_member(groups: Callable[[], Iterable[str]] = user_groups) -> bool:
    """Linux: whether polkit asks the user for their own password as an administrator."""
    try:
        return not ADMIN_GROUPS.isdisjoint(groups())
    except OSError, KeyError:
        return False


def is_administrator() -> bool:
    """Whether the user can confirm an elevation themselves. On Windows in UAC's consent
    prompt: an administrator, whose UI runs with the filtered token, or with the full one
    when UAC is off. On Linux in polkit's dialog: a member of ADMIN_GROUPS. Others would be
    asked for another account's password, which is not a take-over by them."""
    if sys.platform == "linux":
        return is_admin_group_member()
    if sys.platform != "win32":
        return False
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        return False
    try:
        kind = ctypes.c_int()
        size = wintypes.DWORD()
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_ELEVATION_TYPE,
            ctypes.byref(kind),
            ctypes.sizeof(kind),
            ctypes.byref(size),
        ):
            return False
    finally:
        kernel32.CloseHandle(token)
    if kind.value in (_ELEVATION_TYPE_FULL, _ELEVATION_TYPE_LIMITED):
        return True
    # No split token: UAC is off, or this is a standard user.
    return bool(shell32.IsUserAnAdmin())


def run_elevated(executable: Path, parameters: str) -> int:
    """Run ``executable`` with an administrator's rights once UAC confirmed it, wait for it
    and return its exit status. Raises HostError("cancelled") when the user declines, and
    HostError("take_over_failed") when it does not start or not finish within
    HELPER_WAIT_S."""
    if sys.platform != "win32":
        raise HostError("unsupported_method", "elevation is only on Windows")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole32.CoInitializeEx.restype = ctypes.c_long
    shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(_ShellExecuteInfo),)
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(_ShellExecuteInfo)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = str(executable)
    info.lpParameters = parameters
    info.nShow = _SW_HIDE
    # The shell may hand the verb to COM, which a worker thread has not set up.
    initialized = (
        ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED | _COINIT_DISABLE_OLE1DDE) >= 0
    )
    try:
        started = shell32.ShellExecuteExW(ctypes.byref(info))
        error = ctypes.get_last_error()
    finally:
        if initialized:
            ole32.CoUninitialize()
    if not started:
        if error == _ERROR_CANCELLED:
            raise HostError("cancelled", "the elevation was declined")
        raise HostError("take_over_failed", ctypes.FormatError(error).strip())
    if not info.hProcess:
        raise HostError("take_over_failed", "the helper did not start")
    try:
        waited = kernel32.WaitForSingleObject(info.hProcess, int(HELPER_WAIT_S * 1000))
        if waited == _WAIT_TIMEOUT:
            message = f"the helper did not finish within {HELPER_WAIT_S:g} s"
            raise HostError("take_over_failed", message)
        if waited != _WAIT_OBJECT_0:
            raise HostError("take_over_failed", ctypes.FormatError(ctypes.get_last_error()))
        status = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(status)):
            raise HostError("take_over_failed", ctypes.FormatError(ctypes.get_last_error()))
        return status.value
    finally:
        kernel32.CloseHandle(info.hProcess)


def run_command(command: Sequence[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            check=False,
            timeout=PKEXEC_WAIT_S,
        )
    except subprocess.TimeoutExpired as error:
        message = f"the take-over did not finish within {PKEXEC_WAIT_S:g} s"
        raise HostError("take_over_failed", message) from error
    except OSError as error:
        raise HostError("take_over_failed", f"pkexec did not start: {error}") from error
    return done.returncode, done.stderr


def run_pkexec(executable: Path, argument: str, run: Runner = run_command) -> None:
    """Run ``executable argument`` as root through pkexec once polkit's dialog confirmed it,
    and wait for it. Raises HostError("cancelled") when the user dismissed the dialog,
    HostError("not_authorized") when polkit did not authorize it, and
    HostError("take_over_failed") when the helper failed, with the last line it wrote.
    With the first two the helper never ran, and nothing changed."""
    status, output = run([PKEXEC, str(executable), argument])
    if status == DISMISSED:
        raise HostError("cancelled", "the authentication dialog was dismissed")
    lines = output.strip().splitlines()
    reason = lines[-1] if lines else ""
    if status == NOT_AUTHORIZED:
        raise HostError("not_authorized", reason or "pkexec: not authorized")
    if status != 0:
        message = reason or f"the take-over helper exited with status {status}"
        raise HostError("take_over_failed", message)


def take_over() -> None:
    """Have the service's take-over helper stop another user's tunnel, after UAC or polkit
    asked. On Windows it makes this user the owner, and the service restarts for them; on
    Linux the service restarts with no owner, for this user's UI to claim (ServiceHost
    claims after the helper either way)."""
    if sys.platform == "linux":
        run_pkexec(service_executable(), TAKE_OVER_ARG)
        return
    status = run_elevated(service_executable(), TAKE_OVER_ARG)
    if status != 0:
        raise HostError("take_over_failed", f"the take-over helper exited with status {status}")
