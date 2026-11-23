import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Templates as T

// menu (DESIGN.md "Components → 菜单"): AppMenuItems on a white overlay with the overlay
// shadow. openBelow() places it under its trigger, right edges aligned, as a "more" menu;
// openDropdown() left-aligns it, as a dropdown's list. Either goes above the trigger when
// the window has no room below; when neither side has room it takes the roomier one and
// stays inside the window. As a context menu (DESIGN.md "右键菜单") it opens at the pointer
// instead: openContext() for a row whose more button opens it, openAtPointer() otherwise.
Menu {
    id: menu

    property Item trigger
    property int triggerFocusReason: Qt.OtherFocusReason
    // The control the menu opened from, which the window gives focus back to as the menu
    // closes; the trigger too, unless retarget() moved it.
    property Item openedFrom
    property real minimumWidth: 0  // a dropdown's list is at least as wide as the dropdown
    // Where a right-click asked for the menu, in the overlay, while openContext() runs.
    property point pointer
    property bool atPointer
    // While the menu shows, what it is placed by: it goes below `placedBottom` in
    // `placedAnchor`, or above `placedTop`. A menu that opens for the first time lays its
    // items out only as it shows, and its height decides the side, so it is placed again
    // once it has its height.
    property Item placedAnchor
    property real placedTop
    property real placedBottom
    // How much of the list shows: a menu of more than ten items shows the first ten, with
    // the separators among them, and scrolls the rest inside it (DESIGN.md "菜单").
    readonly property real shownHeight: {
        let height = 0
        let items = 0
        for (let i = 0; i < count; i++) {
            const item = itemAt(i)
            if (!item?.visible)
                continue
            if (item instanceof T.MenuItem && ++items > 10)
                return height
            height += item.height
        }
        return Infinity
    }

    function openBelow(anchor: Item) {
        openAt(anchor, anchor.width - width)
    }

    function openDropdown(anchor: Item) {
        openAt(anchor, 0)
    }

    // A right-click on a row opens the menu its more button opens, the same way, so it acts
    // on the same row with the same items enabled: the button is clicked, and the menu goes
    // to the pointer (`position`, in `anchor`) rather than under the button. The button stays
    // the trigger, which dialogs the items open give focus back to. A hidden or disabled
    // button opens nothing.
    function openContext(button: T.AbstractButton, anchor: Item, position: point) {
        if (!button.visible || !button.enabled)
            return
        pointer = anchor.mapToItem(anchor.Overlay.overlay, position.x, position.y)
        atPointer = true
        button.click()
        atPointer = false
    }

    // popup() makes the trigger the menu's parent, so the menu's own window goes with a
    // trigger that is destroyed, such as a removed row's button; the new trigger's counts.
    function openAt(anchor: Item, x: real) {
        trigger = anchor
        openedFrom = anchor
        triggerFocusReason = Focus.reasonAfter(anchor)
        if (atPointer)
            popupAtPointer(anchor, anchor.mapFromItem(anchor.Overlay.overlay, pointer.x, pointer.y))
        else
            place(anchor, x, -Theme.overlayGap, anchor.height + Theme.overlayGap)
    }

    // A context menu of `anchor` itself, such as a text field's edit menu, at `position` in
    // it. Focus comes back to where it was when the menu closes.
    function openAtPointer(anchor: Item, position: point) {
        trigger = anchor
        openedFrom = anchor
        triggerFocusReason = Qt.OtherFocusReason
        popupAtPointer(anchor, position)
    }

    // The menu's top-left corner at `position` in `anchor`; its right edge at the pointer
    // instead when the window has no room on the right, and its bottom edge when there is
    // none below, unless above has less room still.
    function popupAtPointer(anchor: Item, position: point) {
        const overlay = anchor.Overlay.overlay
        const right = anchor.mapToItem(overlay, position.x, 0).x + implicitWidth <= overlay.width - Theme.overlayMargin
        place(anchor, right ? position.x : position.x - implicitWidth, position.y, position.y)
    }

    function place(anchor: Item, x: real, top: real, bottom: real) {
        placedAnchor = anchor
        placedTop = top
        placedBottom = bottom
        popup(anchor, x, placedY())
    }

    function placedY(): real {
        const overlay = placedAnchor.Overlay.overlay
        const origin = placedAnchor.mapToItem(overlay, 0, 0).y
        const roomBelow = overlay.height - Theme.overlayMargin - (origin + placedBottom)
        const roomAbove = origin + placedTop - Theme.overlayMargin
        const below = implicitHeight <= roomBelow || (implicitHeight > roomAbove && roomBelow >= roomAbove)
        return below ? placedBottom : placedTop - implicitHeight
    }

    // At least menu-min-width, and as wide as the longest item up to menu-max-width, where a
    // long name ends in an ellipsis: the ListView that holds the items reports no width of
    // its own.
    implicitWidth: {
        let widest = Math.max(implicitBackgroundWidth + leftInset + rightInset, minimumWidth)
        for (let i = 0; i < count; i++) {
            const item = itemAt(i)
            if (item)
                widest = Math.max(widest, item.implicitWidth + leftPadding + rightPadding)
        }
        return Math.min(widest, Math.max(Theme.menuMaxWidth, minimumWidth))
    }
    implicitHeight: Math.max(implicitBackgroundHeight + topInset + bottomInset, Math.min(implicitContentHeight, shownHeight) + topPadding + bottomPadding)
    margins: Theme.overlayMargin
    padding: Theme.space1

    // An item moved the row the menu opened from, as 上移 moves a rule: the row's control
    // now shows its neighbour, so focus goes back to `item`, where the row is now, instead
    // (DESIGN.md "可排序列表"). Dialogs that the items open still return to `item`.
    function retarget(item: Item) {
        trigger = item
    }

    // A focused menu hands focus back to the window as it closes, which returns it to the
    // control it opened from that had it, but without the reason that shows the focus ring;
    // a retargeted trigger takes it from there. A dialog that an item opened keeps its focus.
    onClosed: {
        placedAnchor = null
        const from = openedFrom
        openedFrom = null
        if (!trigger || !from?.activeFocus)
            return
        if (trigger !== from) {
            trigger.forceActiveFocus(triggerFocusReason)
        } else if (triggerFocusReason !== Qt.OtherFocusReason) {
            trigger.focus = false
            trigger.forceActiveFocus(triggerFocusReason)
        }
    }
    onImplicitHeightChanged: {
        if (visible && placedAnchor)
            y = placedY()
    }
    // The item the keyboard highlights scrolls into sight in a menu that scrolls.
    onCurrentIndexChanged: {
        if (currentIndex >= 0)
            (contentItem as ListView)?.positionViewAtIndex(currentIndex, ListView.Contain)
    }

    enter: Transition {
        NumberAnimation {
            property: "opacity"
            from: 0
            to: 1
            duration: Motion.popupIn
            easing.type: Easing.OutCubic
        }
    }

    exit: Transition {
        NumberAnimation {
            property: "opacity"
            from: 1
            to: 0
            duration: Motion.popupOut
            easing.type: Easing.InCubic
        }
    }

    background: Item {
        implicitWidth: Theme.menuMinWidth

        OverlayShadow {
            anchors.fill: parent
        }

        Rectangle {
            anchors.fill: parent
            radius: Theme.radiusMd
            color: Theme.surface
            border.width: 1
            border.color: Theme.border
        }
    }
}
