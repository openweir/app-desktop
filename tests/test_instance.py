"""The single instance's key and who it writes to (docs/ui.md §8, "单实例"); the messages
themselves are in test_desktop.py."""

import os
import stat
import sys
from pathlib import Path

import pytest
from PySide6.QtNetwork import QLocalSocket
from pytestqt.qtbot import QtBot

from openweir.desktop import instance, windows_identity
from openweir.desktop.instance import KEY_NAME, SingleInstance, message, read_key, server_name

LINK = "openweir://install-config?url=https%3A%2F%2Fsub.example.com%2Fapi%3Ftoken%3Dsecret"


def test_the_first_ui_makes_a_key_for_its_user_alone(tmp_path: Path) -> None:
    first = SingleInstance(tmp_path)
    assert read_key(tmp_path) is None  # nothing until it holds the lock
    assert first.claim()
    try:
        key = read_key(tmp_path)
        assert key is not None
        assert len(key) == 32
        assert first.name == server_name(tmp_path, key)
        if sys.platform != "win32":  # Windows: the profile's ACL on the data directory
            assert stat.S_IMODE((tmp_path / KEY_NAME).stat().st_mode) == 0o600
    finally:
        first.release()

    # The next UI keeps the key; one that is damaged is made again.
    second = SingleInstance(tmp_path)
    assert second.claim()
    second.release()
    assert read_key(tmp_path) == key
    (tmp_path / KEY_NAME).write_text("not a key\n")
    third = SingleInstance(tmp_path)
    assert third.claim()
    try:
        renewed = read_key(tmp_path)
        assert renewed not in (None, key)
        assert third.name == server_name(tmp_path, renewed or "")
    finally:
        third.release()
    assert not (tmp_path / (KEY_NAME + ".part")).exists()


def test_nothing_goes_to_a_server_another_user_runs(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = SingleInstance(tmp_path)
    assert first.claim()
    checked: list[QLocalSocket] = []

    def another_users(socket: QLocalSocket) -> bool:
        checked.append(socket)
        return False

    monkeypatch.setattr(instance, "_serves_this_user", another_users)
    try:
        with (
            qtbot.assertNotEmitted(first.received, wait=300),
            qtbot.assertNotEmitted(first.activated),
        ):
            assert not instance._send(tmp_path, message(LINK), 1, 1000)
        assert len(checked) == 1  # it connected, then wrote nothing
    finally:
        first.release()


def test_nothing_is_asked_before_there_is_a_key(tmp_path: Path) -> None:
    assert not instance._send(tmp_path, message(None), 1, 1000)


def test_the_socket_peer_is_this_user(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("a Unix socket's peer")
    first = SingleInstance(tmp_path)
    assert first.claim()
    socket = QLocalSocket()
    try:
        socket.connectToServer(first.name)
        assert socket.waitForConnected(1000)
        assert instance._peer_uid(socket.socketDescriptor()) == os.geteuid()
        assert instance._serves_this_user(socket)
    finally:
        socket.abort()
        first.release()


@pytest.mark.skipif(sys.platform != "win32", reason="a named pipe's server process")
def test_the_pipe_server_is_this_process(tmp_path: Path) -> None:
    first = SingleInstance(tmp_path)
    assert first.claim()
    socket = QLocalSocket()
    try:
        socket.connectToServer(first.name)
        assert socket.waitForConnected(1000)
        assert instance._pipe_server_pid(socket.socketDescriptor()) == os.getpid()
        assert windows_identity.process_sid(os.getpid()) == windows_identity.current_sid() != ""
        assert instance._serves_this_user(socket)
    finally:
        socket.abort()
        first.release()


USER = "S-1-5-21-1-1001"


@pytest.mark.parametrize(
    ("server", "user", "ours"),
    [
        (USER, USER, True),
        ("S-1-5-21-1-1002", USER, False),  # another user's
        ("", USER, False),  # a process whose token this user may not read
        ("", "", False),  # neither token read
        (None, USER, False),  # the kernel names no server process
    ],
)
def test_a_pipe_is_this_users_by_its_server_process(
    monkeypatch: pytest.MonkeyPatch, server: str | None, user: str, ours: bool
) -> None:
    """Windows' check, with the system calls answered here."""
    pid = 4242
    monkeypatch.setattr(
        instance, "_pipe_server_pid", lambda handle: None if server is None else pid
    )
    monkeypatch.setattr(windows_identity, "current_sid", lambda: user)
    monkeypatch.setattr(
        windows_identity, "process_sid", lambda asked: server if asked == pid else ""
    )
    assert instance._pipe_server_is_this_user(7) is ours
