"""The smallest window with long names (DESIGN.md "长名称", "页面标题栏", "表格"): only the names
give up room; titles, buttons, links, values, notes and table columns keep theirs, and nothing
runs past its row. Checked with the Windows title band, which leaves the header the least
room, in both languages. Then the components that carry the rest of the rules: a dropdown shows
ten items and scrolls the rest, and the segmented selection slides.

A row item without Layout.fillWidth keeps its preferred width (Qt Quick Layouts give it a fixed
size policy); only the items that fill shrink, down to their Layout.minimumWidth. A row whose
fixed items need more than its width lays them past its end instead, which is what
runs_past() finds."""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt, QUrl
from PySide6.QtGui import QFontMetricsF
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from openweir.app import MAIN_QML
from openweir.desktop.titlebar import WINDOWS
from openweir.models.profiles import ProfileError
from tests.support.i18n import ui_language
from tests.support.simulated import LONG_GROUP, LONG_PROFILES, SimulatedFetcher
from tests.support.window import Loaded, find, find_all, kind

PAGES = {
    "overview": "OverviewPage",
    "proxies": "ProxiesPage",
    "profiles": "ProfilesPage",
    "routing": "RoutingPage",
    "connections": "ConnectionsPage",
    "logs": "LogsPage",
    "settings": "SettingsPage",
}
TITLE_ROOM = 280  # what the title and the status keep beside the header's controls
# The pages whose filters move under the header rule to keep TITLE_ROOM; buttons stay in the
# header whatever room they leave (DESIGN.md "页面标题栏").
FILTERED = {"proxies", "connections", "logs"}


@pytest.fixture
def long_names() -> bool:
    return True


@pytest.fixture(autouse=True)
def windows_band(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows title band, whatever system the tests run on: its caption buttons leave
    the page header 114px less."""
    monkeypatch.setattr("openweir.viewmodels.desktop.titlebar_style", lambda _: WINDOWS)


def smallest(qtbot: QtBot, loaded: Loaded) -> None:
    """The window at its minimum size, 960 by 640, connected, with the core's groups read."""
    window = loaded.window
    window.setWidth(window.minimumWidth())
    window.setHeight(window.minimumHeight())
    assert (window.width(), window.height()) == (960, 640)
    models = loaded.view_models
    qtbot.waitUntil(lambda: models.overview.property("connected"))
    models.proxies.refresh()
    qtbot.waitUntil(lambda: models.proxies.property("currentGroup") != "")


def route(qtbot: QtBot, loaded: Loaded, mode: str) -> None:
    """Route with a profile's own rules or with my plan, and read the core's groups again."""
    models = loaded.view_models
    models.routing.setMode(mode)
    qtbot.waitUntil(lambda: models.routing.property("mode") == mode)
    qtbot.wait(300)
    models.proxies.refresh()
    qtbot.wait(300)


def show(qtbot: QtBot, loaded: Loaded, page: str) -> QQuickItem:
    loaded.view_models.shell.setPage(page)
    qtbot.waitUntil(
        lambda: find(loaded.window.contentItem(), lambda i: kind(i) == PAGES[page]) is not None
    )
    qtbot.wait(200)
    return loaded.item(lambda i: kind(i) == PAGES[page])


def scene(item: QQuickItem) -> QRectF:
    return item.mapRectToScene(QRectF(0, 0, item.width(), item.height()))


def shown(item: QQuickItem) -> list[QQuickItem]:
    """The children ``item`` shows, as a layout places them."""
    return [c for c in item.childItems() if c.isVisible() and kind(c) != "QQuickRepeater"]


def label(item: QQuickItem) -> str:
    text = item.property("text")
    if not text and isinstance(content := item.property("contentItem"), QQuickItem):
        text = content.property("text")
    return f"{kind(item)} {text!r}"


def whole(item: QQuickItem) -> bool:
    """Whether ``item`` has the width it asks for, and the text in it is not cut short."""
    content = item.property("contentItem")
    text = content if isinstance(content, QQuickItem) else item
    return item.width() >= item.implicitWidth() - 0.5 and text.property("truncated") is not True


LAYOUTS = ("QQuickRowLayout", "QQuickColumnLayout", "QQuickGridLayout")


def runs_past(root: QQuickItem) -> list[str]:
    """Items that a layout under ``root`` lays past its end, or a row over the item before.
    A row that needs more than its width takes it from the layout around it, so it is that
    layout's end the row runs past."""
    found = []
    layouts = find_all(root, lambda i: kind(i) in LAYOUTS)
    # A layout places its items when the window next polishes it, before it draws. Values
    # that change every second (the overview's busiest connections) can be newer than that,
    # so lay them out as the next frame will, the outer layouts first.
    for layout in layouts:
        layout.ensurePolished()
    for layout in layouts:
        before = None
        for item in shown(layout):
            if item.x() + item.width() > layout.width() + 0.5:
                found.append(f"{label(item)} runs past its {kind(layout)}")
            if (
                kind(layout) == "QQuickRowLayout"
                and before is not None
                and item.x() < before.x() + before.width() - 0.5
            ):
                found.append(f"{label(item)} lies over {label(before)}")
            before = item
    return found


def header_faults(page: QQuickItem, *, room: bool = True) -> list[str]:
    """What the page header cuts short, where only the status may be; and with ``room``,
    whether the title and the status keep theirs."""
    header = find(page, lambda i: kind(i) == "PageHeader")
    assert header is not None
    title = find(
        header, lambda i: kind(i) == "UiText" and i.property("text") == header.property("title")
    )
    status = find(header, lambda i: kind(i) == "AccessibleText")
    assert title is not None
    assert status is not None
    controls = find_all(
        header, lambda i: kind(i) in ("AppButton", "MenuButton", "SegmentedControl")
    )
    found = [f"{label(c)} is cut short" for c in [title, *controls] if not whole(c)]
    found += [
        f"{label(text)} is cut short"
        for control in controls
        for text in find_all(control, lambda i: kind(i) == "UiText")
        if text.property("truncated")
    ]
    width = scene(status).right() - scene(title).left()
    if room and width < TITLE_ROOM - 0.5:
        found.append(f"the title and the status have {width:.0f}px")
    return found


@pytest.mark.parametrize("loaded", ["en", "zh"], indirect=True)
def test_only_names_give_way_in_the_smallest_window(qtbot: QtBot, loaded: Loaded) -> None:
    smallest(qtbot, loaded)
    faults = []
    for mode in ("profile", "plan"):  # a profile's rules, or my plan with its node pool
        route(qtbot, loaded, mode)
        for page in PAGES:
            content = show(qtbot, loaded, page)
            faults += [f"{mode}/{page}: {fault}" for fault in runs_past(content)]
            faults += [
                f"{mode}/{page}: {fault}" for fault in header_faults(content, room=page in FILTERED)
            ]
    assert faults == []

    # The checks find a row that has less room than its items need.
    loaded.window.setMinimumWidth(480)
    loaded.window.setWidth(480)
    qtbot.wait(100)
    assert runs_past(show(qtbot, loaded, "profiles")) != []


@pytest.mark.parametrize(
    ("loaded", "in_header"),
    # "Proxies", the sort order and "Test latency" would leave the title and status 229px.
    [("en", False), ("zh", True)],
    indirect=["loaded"],
)
def test_the_sort_order_moves_under_the_header_rule_without_room(
    qtbot: QtBot, loaded: Loaded, in_header: bool
) -> None:
    smallest(qtbot, loaded)
    page = show(qtbot, loaded, "proxies")
    header = loaded.item(lambda i: kind(i) == "PageHeader")
    sort = loaded.item(lambda i: kind(i) == "SegmentedControl")
    assert (find(header, lambda i: i is sort) is not None) == in_header
    assert header_faults(page) == []
    if not in_header:  # left-aligned, 20px from the rule and from the boxes below
        assert scene(sort).top() == scene(header).bottom() + 20
        assert scene(sort).left() == scene(header).left()
        groups_label = loaded.item(
            lambda i: kind(i) == "SectionLabel" and i.property("text") == "Proxy groups"
        )
        assert scene(groups_label).top() == scene(sort).bottom() + 20
    # The nodes' box still ends at the page's bottom margin.
    _, nodes = find_all(page, lambda i: kind(i) == "QQuickListView")
    bottom = page.mapToScene(QPointF(0, page.height())).y() - 40 - 1  # inside the border
    assert scene(nodes).bottom() == pytest.approx(bottom)

    # A wider window has room for it in the header.
    loaded.window.setWidth(1280)
    qtbot.wait(100)
    assert find(header, lambda i: i is sort) is not None
    assert header_faults(page) == []


def columns(row: QQuickItem) -> list[tuple[float, float]]:
    """Where a row's columns start in the window, and how wide they are."""
    return [(scene(item).left(), item.width()) for item in shown(row)]


def table(root: QQuickItem, titles: list[str]) -> tuple[QQuickItem, list[QQuickItem]]:
    """The row of column titles under ``root``, and the rows of the box it heads."""

    def titled(item: QQuickItem) -> bool:
        texts = [c.property("text") for c in shown(item)]
        return kind(item) == "QQuickRowLayout" and [t for t in texts if t] == titles

    head = find(root, titled)
    assert head is not None
    box = head.parentItem().parentItem()
    count = len(shown(head))
    rows = [
        row
        for row in find_all(box, lambda i: kind(i) == "QQuickRowLayout")
        if row is not head and len(shown(row)) == count
    ]
    assert rows
    return head, rows


@pytest.mark.parametrize("loaded", ["en"], indirect=True)
def test_table_columns_line_up_with_their_titles(qtbot: QtBot, loaded: Loaded) -> None:
    smallest(qtbot, loaded)

    # The nodes from my plan's node pool, with their sources.
    route(qtbot, loaded, "plan")
    page = show(qtbot, loaded, "proxies")
    qtbot.waitUntil(lambda: loaded.view_models.proxies.property("showSource"))
    head, rows = table(page, ["Name", "Source", "Type", "Latency"])
    assert any(
        find(row, lambda i: kind(i) == "UiText" and i.property("truncated")) for row in rows
    ), "no name long enough to try the columns"
    assert all(columns(row) == columns(head) for row in rows)
    # Their side padding is row-single's 16px, inside the box's 1px border.
    box = head.parentItem().parentItem()
    first, last = shown(head)[0], shown(head)[-1]
    assert scene(first).left() == scene(box).left() + 1 + 16
    assert scene(last).right() == scene(box).right() - 1 - 16

    # The subscriptions; English's common values fit (DESIGN.md "英文文案").
    page = show(qtbot, loaded, "profiles")
    head, rows = table(page, ["Name", "Usage", "Expires", "Updated"])
    # The last column is the more button, under a spacer as wide as its row's margin.
    assert all(columns(row)[:-1] == columns(head)[:-1] for row in rows)
    more = scene(shown(head)[-1]).right() + 4
    assert all(scene(shown(row)[-1]).right() == more for row in rows)
    expiry, updated = shown(rows[0])[2:4]
    assert (
        QFontMetricsF(expiry.property("font")).horizontalAdvance("Dec 31, 2026") <= expiry.width()
    )
    assert (
        QFontMetricsF(updated.property("font")).horizontalAdvance("45 minutes ago")
        <= updated.width()
    )

    # A profile's own rules: the targets under their title, the long group's too.
    route(qtbot, loaded, "profile")
    page = show(qtbot, loaded, "routing")
    head, rows = table(page, ["Match", "Target"])
    assert LONG_GROUP in {shown(row)[-1].property("text") for row in rows}
    assert all(columns(row) == columns(head) for row in rows)


@pytest.mark.parametrize("loaded", ["en"], indirect=True)
def test_log_lines_keep_their_time_and_level_columns(qtbot: QtBot, loaded: Loaded) -> None:
    smallest(qtbot, loaded)
    page = show(qtbot, loaded, "logs")
    qtbot.waitUntil(lambda: loaded.view_models.logs.property("count") > 0)
    qtbot.wait(200)
    lines = [
        row
        for row in find_all(page, lambda i: kind(i) == "QQuickRowLayout")
        if [kind(c) for c in row.childItems()] == ["LogText"] * 3
    ]
    assert any(row.childItems()[2].property("lineCount") > 1 for row in lines), "none wraps"
    for row in lines:
        time, level, message = row.childItems()
        assert (time.width(), level.width(), message.x()) == (64, 48, 64 + 12 + 48 + 12)


@pytest.mark.parametrize("loaded", ["en", "zh"], indirect=True)
def test_overview_rows_take_room_only_from_names(
    qtbot: QtBot, loaded: Loaded, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(self: SimulatedFetcher, url: str) -> None:
        raise ProfileError("download_failed", "HTTP 503")

    monkeypatch.setattr(SimulatedFetcher, "fetch", fail)
    smallest(qtbot, loaded)
    models = loaded.view_models
    route(qtbot, loaded, "profile")  # the link to my plan shows
    models.profiles.updateProfile("sample-a")
    qtbot.waitUntil(lambda: models.profiles.property("hasError"))
    page = show(qtbot, loaded, "overview")

    # 当前使用: the label and the link keep their widths; the button with the name shrinks.
    link = loaded.item(
        lambda i: (
            kind(i) == "LinkButton"
            and i.property("text") in ("改用我的方案", "Use my plan instead")
        )
    )
    name_label, middle, button = shown(link.parentItem())
    assert middle is link
    assert whole(name_label)
    assert whole(link)
    assert link.property("contentItem").property("font").pixelSize() == 13  # link: body-sm
    assert button.width() < button.implicitWidth()
    assert scene(button).left() == scene(link).right() + 12

    # The subscription whose update failed: the warning and the usage stay whole.
    name = loaded.item(
        lambda i: kind(i) == "AccessibleText" and i.property("text") == LONG_PROFILES[0]
    )
    row = name.parentItem()
    warning, usage = shown(row)[1], shown(row)[-1]
    assert warning.property("text").strip(" ·") in ("更新失败", "Update failed")
    assert whole(warning)
    assert whole(usage)
    assert scene(usage).right() <= scene(row).right()
    assert runs_past(page) == []


# -- the components ------------------------------------------------------------------------
COMPONENTS = b"""import QtQuick
import QtQuick.Controls.Basic
import "components"
ApplicationWindow {
    visible: true
    width: 480; height: 640
    AppSelect {
        objectName: "long"; x: 20; y: 20; width: 200; current: "o0"
        options: Array.from({length: 25}, (_, i) => ({
            value: "o" + i, label: "Option " + i, separator: i === 4
        }))
    }
    AppSelect {
        objectName: "short"; x: 240; y: 20; width: 200; current: "o0"
        options: Array.from({length: 5}, (_, i) => ({value: "o" + i, label: "Option " + i}))
    }
    SegmentedControl {
        objectName: "segments"; x: 20; y: 560; current: "b"
        options: [{value: "a", label: "Default"}, {value: "b", label: "Name"},
                  {value: "c", label: "Latency"}]
    }
}"""


@pytest.fixture
def components(qtbot: QtBot) -> Iterator[QQuickWindow]:
    with ui_language("en"):
        engine = QQmlEngine()
        component = QQmlComponent(engine)
        component.setData(COMPONENTS, QUrl.fromLocalFile(str(MAIN_QML)))
        window = component.create()
        assert isinstance(window, QQuickWindow), [e.toString() for e in component.errors()]
        try:
            window.requestActivate()
            qtbot.waitUntil(window.isActive)
            yield window
        finally:
            window.close()
            window.deleteLater()


def open_list(qtbot: QtBot, window: QQuickWindow, name: str) -> QQuickItem:
    """Open the dropdown ``name`` from the keyboard; its menu's list of items."""
    select = window.findChild(QQuickItem, name)
    assert select is not None
    select.forceActiveFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(window, Qt.Key.Key_Space)
    qtbot.wait(250)
    (items,) = find_all(window.contentItem(), lambda i: kind(i) == "QQuickListView")
    return items


def test_a_dropdown_shows_ten_items_and_scrolls_the_rest(
    qtbot: QtBot, components: QQuickWindow
) -> None:
    items = open_list(qtbot, components, "long")
    # Ten 32px items with the separator line among them (4px, 1px, 4px).
    assert items.height() == 10 * 32 + 4 + 1 + 4
    assert items.property("contentHeight") > items.height()
    assert items.property("interactive")

    # The keyboard reaches the last item, which scrolls into sight. The list holds every
    # item, and hides those out of sight.
    (last,) = [
        i for i in items.property("contentItem").childItems() if i.property("text") == "Option 24"
    ]
    for _ in range(30):
        if last.property("highlighted"):
            break
        QTest.keyClick(components, Qt.Key.Key_Down)
    qtbot.wait(250)
    assert last.property("highlighted")
    assert scene(items).contains(scene(last))
    QTest.keyClick(components, Qt.Key.Key_Escape)
    qtbot.wait(250)

    # A short one is as tall as its items.
    items = open_list(qtbot, components, "short")
    assert items.height() == 5 * 32
    assert not items.property("interactive")


def test_the_segmented_selection_slides_to_a_new_choice(
    qtbot: QtBot, components: QQuickWindow
) -> None:
    control = components.findChild(QQuickItem, "segments")
    assert control is not None
    segments = find_all(control, lambda i: i.property("modelData") is not None)
    block = find(control, lambda i: i.objectName() == "selection")
    assert block is not None

    # It takes its first place at once.
    assert scene(block) == scene(segments[1])

    control.setProperty("current", "c")
    assert scene(block).left() == scene(segments[1]).left()  # on its way
    qtbot.waitUntil(lambda: scene(block) == scene(segments[2]), timeout=1000)
