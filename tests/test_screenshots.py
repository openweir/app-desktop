"""scripts/gen_screenshots.py renders the documentation's screenshots from the real window."""

import os
from pathlib import Path

import pytest
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickWindow
from pytestqt.qtbot import QtBot

from openweir.viewmodels.composition import ViewModels
from scripts import gen_screenshots


def test_renders_a_shot_offscreen(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The routing page switches to my plan and its groups' tab, which is most of what a
    shot can ask for; the picture is the window in its frame, not an empty one."""
    monkeypatch.setattr(gen_screenshots, "WARM_UP_POLLS", 1)
    monkeypatch.setattr(gen_screenshots, "SETTLE_MS", 300)
    (path,) = gen_screenshots.capture(tmp_path, "dark", ["routing-groups"])
    assert path == tmp_path / "routing-groups-dark.png"
    image = QImage(str(path))
    width, height = gen_screenshots.SIZE
    assert image.width() >= width
    assert image.width() * height == image.height() * width
    assert image.pixelColor(0, 0).alpha() == 0  # outside the rounded corner
    middle = image.height() // 2
    row = {image.pixelColor(x, middle).name() for x in range(0, image.width(), 4)}
    assert len(row) > 8, row  # the sidebar, the page and the text on them


def test_renders_the_account_page_signed_in(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The account page's shot is taken signed in to a stand-in provider, once its account
    has synced and its wallet, plans and orders are read; the shots before it are taken in a
    window without an account, and the stand-in is gone after."""
    monkeypatch.setattr(gen_screenshots, "WARM_UP_POLLS", 1)
    monkeypatch.setattr(gen_screenshots, "SETTLE_MS", 300)
    monkeypatch.delenv("no_proxy", raising=False)
    shown: list[tuple[str, bool]] = []
    show = gen_screenshots._show

    def showing(window: QQuickWindow, view_models: ViewModels, shot: gen_screenshots.Shot) -> None:
        show(window, view_models, shot)
        shown.append((shot.page, view_models.account.property("subscriptionCount") > 0))

    monkeypatch.setattr(gen_screenshots, "_show", showing)
    paths = gen_screenshots.capture(tmp_path, "light", ["account", "settings"])
    assert paths == [tmp_path / "settings-light.png", tmp_path / "account-light.png"]
    assert shown == [("settings", False), ("account", True)]
    image = QImage(str(paths[1]))
    width, height = gen_screenshots.SIZE
    assert image.width() * height == image.height() * width
    assert "no_proxy" not in os.environ
