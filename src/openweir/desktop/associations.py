"""Remove the user's own link registrations that open a program no longer there
(docs/release.md §6, D45).

Windows: the installer registers the install links' schemes (links.SCHEMES) in HKLM, but a key
under the user's ``HKCU\\Software\\Classes`` wins over it. A client removed without cleaning up
leaves one whose command names a program that is gone, and Windows then reports that it cannot
find the application instead of opening OpenWeir. As the UI starts, running as that user, it
reads each scheme's ``shell\\open\\command`` there and deletes the scheme's whole key only when
the program it names does not exist, so that OpenWeir's registration in HKLM takes effect. A
key that opens a program still installed is the user's choice and stays; so does one whose
command cannot be read with certainty (a program found on the PATH, a network path, a drive
that is not there, a variable that is not set).

Linux: the package's .desktop file declares ``x-scheme-handler/<scheme>`` for each scheme, and
the system's MIME cache lists it. A default in the user's ``~/.config/mimeapps.list`` wins over
it: one that another client wrote, or the user chose, before that client went. As the UI of
the installed package starts, it removes the defaults of its schemes there whose desktop
entries are all gone, or open a program by a full path that does not exist. A default that
opens a program still there stays, and so does one that cannot be told with certainty: a
program found on the PATH, started through another (``flatpak run``, ``env``) or over D-Bus.
Nothing else in the file changes.

Only Windows' own Qt platform, and Linux's xcb and Wayland ones, do this; other platforms, and
the tests' offscreen one, never touch the registry or the file. The tests use a fake
``UserClasses`` and scratch directories.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import stat
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path, PureWindowsPath
from typing import Protocol

from openweir.desktop.links import SCHEMES
from openweir.update.releases import installed_package_format

_log = logging.getLogger(__name__)

CLASSES = r"Software\Classes"
COMMAND = r"shell\open\command"
# ExpandEnvironmentStrings' syntax: %NAME%, the name without % or "=".
_VARIABLE = re.compile(r"%([^%=]+)%")
# Characters no file name has; ":" only after the drive.
_NOT_IN_NAMES = frozenset('"<>|*?:')
# Windows' answer to a name no file can have: ERROR_INVALID_NAME, ERROR_BAD_PATHNAME.
_INVALID_NAME = (123, 161)


class UserClasses(Protocol):
    """The current user's ``HKCU\\Software\\Classes``. Both raise OSError on failure."""

    def command(self, scheme: str) -> str | None:
        """The default value of ``<scheme>\\shell\\open\\command``; None without one."""
        ...

    def delete(self, scheme: str) -> None:
        """Delete ``<scheme>`` with everything under it."""
        ...


class WindowsUserClasses:
    """The real registry, through winreg."""

    def command(self, scheme: str) -> str | None:
        if sys.platform != "win32":
            raise OSError("the registry is Windows'")
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{CLASSES}\{scheme}\{COMMAND}") as key:
                value, kind = winreg.QueryValueEx(key, "")
        except FileNotFoundError:
            return None
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            return ""  # no command Windows runs; "" is one that cannot be read
        return str(value)

    def delete(self, scheme: str) -> None:
        if sys.platform != "win32":
            raise OSError("the registry is Windows'")
        import winreg

        def delete_tree(path: str) -> None:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                children = []
                while True:
                    try:
                        children.append(winreg.EnumKey(key, len(children)))
                    except OSError:  # ERROR_NO_MORE_ITEMS
                        break
            for child in children:
                delete_tree(rf"{path}\{child}")
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)

        delete_tree(rf"{CLASSES}\{scheme}")


def expand(text: str, environ: Mapping[str, str]) -> str:
    """``%NAME%`` replaced as Windows does, ignoring case; an unset one stays as it is."""
    variables = {name.upper(): value for name, value in environ.items()}
    return _VARIABLE.sub(lambda match: variables.get(match[1].upper(), match[0]), text)


def candidates(command: str, environ: Mapping[str, str]) -> list[str] | None:
    """The paths the program in ``command`` may be at, as CreateProcess looks for it; None
    when that cannot be told with certainty.

    A quoted program ends at the closing quote. Without quotes CreateProcess tries each
    stretch up to a space in turn, so ``C:\\Program Files\\A B\\a.exe %1`` may be
    ``C:\\Program``, ``C:\\Program Files\\A``, ... ; a name without an extension gets ``.exe``.
    Environment variables are expanded first, for REG_EXPAND_SZ and REG_SZ alike: of the two
    readings, that one keeps more registrations."""
    text = expand(command, environ).strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        if end < 0:
            return None
        stretches = [text[1:end]]
    else:
        words = text.split(" ")
        stretches = [" ".join(words[: count + 1]) for count in range(len(words))]
    # Only a full local path: a bare name is looked up on the PATH, a UNC path may only be
    # out of reach, and an unset variable names no file.
    if not _local_path(stretches[0]) or "%" in stretches[0]:
        return None
    paths: list[str] = []
    for stretch in stretches:
        if not _local_path(stretch):
            break  # the longer ones have the same character
        paths.append(stretch)
        if not PureWindowsPath(stretch).suffix:
            paths.append(f"{stretch}.exe")
    return paths


def _local_path(text: str) -> bool:
    """Whether ``text`` is a full path on a drive that a file can have."""
    path = PureWindowsPath(text)
    return (
        bool(path.drive and path.root)
        and not text.startswith("\\\\")
        and not _NOT_IN_NAMES & set(text[len(path.drive) :])
    )


def exists(path: str) -> bool:
    """False only when the file is known not to be there; other failures raise OSError."""
    try:
        os.stat(path)
    except FileNotFoundError, NotADirectoryError:
        return False
    except OSError as error:
        if getattr(error, "winerror", None) in _INVALID_NAME:
            return False
        raise
    return True


def is_stale(
    command: str, environ: Mapping[str, str], exists: Callable[[str], bool]
) -> bool | None:
    """Whether ``command`` names a program that is not there; None when that is unknown."""
    paths = candidates(command, environ)
    if paths is None:
        return None
    # The drive itself missing (a removable or mapped one) says nothing about the program.
    if not exists(PureWindowsPath(paths[0]).anchor):
        return None
    return not any(exists(path) for path in paths)


def clear_stale(
    classes: UserClasses,
    environ: Mapping[str, str],
    exists: Callable[[str], bool] = exists,
    schemes: Sequence[str] = SCHEMES,
) -> list[str]:
    """Deletes the schemes whose command names a missing program; returns them."""
    cleared: list[str] = []
    for scheme in schemes:
        try:
            command = classes.command(scheme)
            if command is None:
                continue
            stale = is_stale(command, environ, exists)
            if stale is None:
                _log.warning(
                    "left the user's %s: link registration alone: cannot tell which program "
                    "it opens (%r)",
                    scheme,
                    command,
                )
                continue
            if not stale:
                continue
            classes.delete(scheme)
        except OSError as error:
            _log.warning("could not check the user's %s: link registration: %s", scheme, error)
            continue
        _log.warning(
            "removed the user's %s: link registration, which opened a program that no longer "
            "exists (%r); OpenWeir's registration applies now",
            scheme,
            command,
        )
        cleared.append(scheme)
    return cleared


# -- Linux: the user's mimeapps.list ---------------------------------------------------------

DEFAULTS_GROUP = "Default Applications"
SCHEME_HANDLER = "x-scheme-handler/"
# Where Flatpak and Snap put their applications' desktop entries, in case the session that
# started the UI did not add them to XDG_DATA_DIRS: an entry there is not gone.
EXPORTED_DATA_DIRS = (
    "~/.local/share/flatpak/exports/share",
    "/var/lib/flatpak/exports/share",
    "/var/lib/snapd/desktop",
)
# Variants of a desktop file ID to try: a "-" in it may stand for a "/" of a subdirectory.
_MAX_ID_DASHES = 6


def mimeapps_path(environ: Mapping[str, str]) -> Path:
    """``$XDG_CONFIG_HOME/mimeapps.list``; a relative XDG_CONFIG_HOME is to be ignored."""
    config = environ.get("XDG_CONFIG_HOME", "")
    return (Path(config) if os.path.isabs(config) else Path.home() / ".config") / "mimeapps.list"


def application_dirs(environ: Mapping[str, str]) -> list[Path]:
    """The ``applications`` directories that desktop entries are looked up in, the user's
    first (XDG Base Directory Specification)."""
    home = environ.get("XDG_DATA_HOME", "")
    bases = [home if os.path.isabs(home) else str(Path.home() / ".local/share")]
    dirs = environ.get("XDG_DATA_DIRS", "") or "/usr/local/share:/usr/share"
    bases += [base for base in dirs.split(":") if os.path.isabs(base)]
    bases += [os.path.expanduser(base) for base in EXPORTED_DATA_DIRS]
    return list(dict.fromkeys(Path(base) / "applications" for base in bases))


def _id_paths(desktop_id: str) -> Iterator[str]:
    """The paths, relative to an ``applications`` directory, that ``desktop_id`` may name:
    ``a-b.desktop`` is ``a-b.desktop`` or ``a/b.desktop``."""
    parts = desktop_id.split("-")
    if len(parts) - 1 > _MAX_ID_DASHES:
        yield desktop_id
        return
    for mask in range(2 ** (len(parts) - 1)):
        path = parts[0]
        for index, part in enumerate(parts[1:]):
            path += ("/" if mask >> index & 1 else "-") + part
        yield path


def find_desktop_file(desktop_id: str, dirs: Sequence[Path]) -> Path | None:
    for directory in dirs:
        for relative in _id_paths(desktop_id):
            path = directory / relative
            if path.is_file():
                return path
    return None


def entry_values(text: str) -> dict[str, str]:
    """The keys of a desktop entry's ``[Desktop Entry]`` group, without localized ones."""
    values: dict[str, str] = {}
    group = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
        elif group == "Desktop Entry" and "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip())
    return values


def program_gone(text: str, exists: Callable[[str], bool]) -> bool | None:
    """Whether the desktop entry ``text`` opens a program that is not there; None when that
    cannot be told with certainty."""
    entry = entry_values(text)
    if entry.get("DBusActivatable", "").lower() == "true":
        return None  # D-Bus starts it; Exec is only its fallback
    programs: list[str] = []
    try_exec = entry.get("TryExec", "")
    if try_exec:
        programs.append(try_exec)
    try:
        words = shlex.split(entry.get("Exec", ""))
    except ValueError:
        return None
    if not words:
        return None
    programs.append(words[0])
    # A program by name is looked up on the PATH, which the session may set otherwise.
    if not all(os.path.isabs(program) for program in programs):
        return None
    return not all(exists(program) for program in programs)


def default_gone(value: str, dirs: Sequence[Path], exists: Callable[[str], bool]) -> bool | None:
    """Whether every desktop entry a default lists (``a.desktop;b.desktop;``) is missing or
    opens a missing program; None when that is unknown. The desktops use the first one that
    is there."""
    ids = [part.strip() for part in value.split(";") if part.strip()]
    if not ids:
        return None
    unknown = False
    for desktop_id in ids:
        path = find_desktop_file(desktop_id, dirs)
        if path is None:
            continue
        try:
            gone = program_gone(path.read_text("utf-8", errors="replace"), exists)
        except OSError:
            gone = None
        if gone is False:
            return False
        unknown = unknown or gone is None
    return None if unknown else True


def _handled_scheme(key: str, schemes: Sequence[str]) -> str | None:
    if not key.lower().startswith(SCHEME_HANDLER):
        return None
    scheme = key[len(SCHEME_HANDLER) :].lower()
    return scheme if scheme in schemes else None


def clear_stale_defaults(
    path: Path,
    dirs: Sequence[Path],
    exists: Callable[[str], bool] = os.path.exists,
    schemes: Sequence[str] = SCHEMES,
) -> list[str]:
    """Remove ``schemes``' defaults in the mimeapps.list at ``path`` whose programs are gone;
    returns the schemes. The rest of the file stays as it was, byte for byte. A file that is
    a link, such as one a dotfiles manager keeps, is left alone: replacing it would break the
    link."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return []
    except OSError as error:
        _log.warning("could not read %s: %s", path, error)
        return []
    if not stat.S_ISREG(info.st_mode):
        _log.warning("left %s alone: it is not a regular file", path)
        return []
    try:
        text = path.read_bytes().decode("utf-8")  # its line ends as they are
    except (OSError, UnicodeDecodeError) as error:
        _log.warning("could not read %s: %s", path, error)
        return []
    kept: list[str] = []
    cleared: list[str] = []
    group = ""
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            group = stripped[1:-1]
        elif group == DEFAULTS_GROUP and "=" in stripped and not stripped.startswith("#"):
            key, value = (part.strip() for part in stripped.split("=", 1))
            scheme = _handled_scheme(key, schemes)
            if scheme is not None:
                gone = default_gone(value, dirs, exists)
                if gone is None:
                    _log.warning(
                        "left the user's default for %s: links alone: cannot tell which "
                        "program it opens (%r)",
                        scheme,
                        value,
                    )
                elif gone:
                    _log.warning(
                        "removing the user's default for %s: links, which opened a program "
                        "that no longer exists (%r); OpenWeir's registration applies now",
                        scheme,
                        value,
                    )
                    cleared.append(scheme)
                    continue
        kept.append(line)
    if cleared:
        try:
            _replace(path, "".join(kept), stat.S_IMODE(info.st_mode))
        except OSError as error:
            _log.warning("could not write %s: %s", path, error)
            return []
    return cleared


def _replace(path: Path, text: str, mode: int) -> None:
    """Write ``text`` to ``path`` in one step, as the desktops' own tools do."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def clear_stale_in_background(platform_name: str) -> threading.Thread | None:
    """``clear_stale`` on Windows' Qt platform, for the user's registry, and
    ``clear_stale_defaults`` on Linux's, for the installed package's user, off the Qt thread:
    a path on a drive that sleeps or is out of reach can take a while to answer.
    ``platform_name`` is QGuiApplication.platformName()."""
    if sys.platform == "win32" and platform_name == "windows":
        target, args = clear_stale, (WindowsUserClasses(), os.environ)
    elif (
        sys.platform == "linux"
        and (platform_name == "xcb" or platform_name.startswith("wayland"))
        and installed_package_format() is not None
    ):
        environ = dict(os.environ)
        target, args = clear_stale_defaults, (mimeapps_path(environ), application_dirs(environ))
    else:
        return None
    thread = threading.Thread(
        target=target, args=args, name="clear-stale-link-registrations", daemon=True
    )
    thread.start()
    return thread
