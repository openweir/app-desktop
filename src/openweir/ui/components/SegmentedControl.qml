pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls.Basic

// 2–4 mutually exclusive options: segmented-track and segmented-item(-selected).
Rectangle {
    id: root

    property var options: []  // [{value, label}]
    property string current
    property bool allowReselect: false
    property string accessibleName
    signal picked(string value)

    implicitWidth: row.implicitWidth + 4
    implicitHeight: Theme.controlSm + 4
    radius: Theme.radiusSm
    color: Theme.controlTrack
    opacity: enabled ? 1 : Theme.disabledOpacity
    Accessible.role: Accessible.Grouping
    Accessible.name: accessibleName

    // The selected segment's raised block. It slides to a new choice in 120ms, at once while
    // the system reduces motion (DESIGN.md "动效", Motion); it takes its first place without
    // sliding, as the page shows.
    Item {
        id: selection

        readonly property Item segment: {
            for (let i = 0; i < segments.count; i++) {
                const segment = segments.itemAt(i) as Segment
                if (segment?.selected)
                    return segment
            }
            return null
        }
        property bool placed: false

        objectName: "selection"
        visible: segment !== null
        x: row.x + (segment?.x ?? 0)
        y: row.y
        width: segment?.width ?? 0
        height: Theme.controlSm

        Behavior on x {
            enabled: selection.placed

            NumberAnimation {
                duration: Motion.stateChange
                easing.type: Easing.OutCubic
            }
        }

        Behavior on width {
            enabled: selection.placed

            NumberAnimation {
                duration: Motion.stateChange
                easing.type: Easing.OutCubic
            }
        }

        Rectangle {
            anchors.fill: parent
            anchors.topMargin: 1
            anchors.bottomMargin: -1
            radius: Theme.radiusXs
            color: Theme.shadowSegment
        }

        Rectangle {
            anchors.fill: parent
            radius: Theme.radiusXs
            color: Theme.surface
        }
    }

    Row {
        id: row

        anchors.centerIn: parent
        onPositioningComplete: selection.placed = selection.segment !== null

        Repeater {
            id: segments

            model: root.options
            delegate: Segment {}
        }
    }

    component Segment: AbstractButton {
        id: segment

        required property var modelData
        readonly property bool selected: modelData.value === root.current

        implicitHeight: Theme.controlSm
        leftPadding: Theme.space3
        rightPadding: Theme.space3
        checked: selected  // what screen readers hear; clicking does not toggle it
        Accessible.role: Accessible.RadioButton
        Accessible.name: modelData.label
        onClicked: {
            if (!selected || root.allowReselect)
                root.picked(modelData.value)
        }

        contentItem: UiText {
            text: segment.modelData.label
            role: segment.selected ? Theme.type.labelSmStrong : Theme.type.labelSm
            color: segment.selected ? Theme.text : Theme.textTertiary
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }

        background: Item {
            FocusRing {
                targetRadius: Theme.radiusXs
                visible: segment.visualFocus
            }
        }
    }
}
