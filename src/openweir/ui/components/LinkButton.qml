import QtQuick
import QtQuick.Controls.Basic

// link: a text link to another page, e.g. a section's "管理", in body-sm.
AbstractButton {
    id: control

    padding: 0
    Accessible.role: Accessible.Link
    Accessible.name: text

    contentItem: UiText {
        text: control.text
        role: Theme.type.bodySm
        color: control.hovered ? Theme.linkHover : Theme.link
    }

    background: Item {
        FocusRing {
            targetRadius: 2
            visible: control.visualFocus
        }
    }
}
