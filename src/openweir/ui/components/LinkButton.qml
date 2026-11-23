import QtQuick
import QtQuick.Controls.Basic

// link: a text link to another page, e.g. a section's "管理", in body-sm, or in the type of
// the line it ends, such as a caption. One that names something may give way to the rest of
// its row with an ellipsis (DESIGN.md "长名称").
AbstractButton {
    id: control

    property var role: Theme.type.bodySm
    property int elide: Text.ElideNone

    padding: 0
    Accessible.role: Accessible.Link
    Accessible.name: text

    contentItem: UiText {
        text: control.text
        role: control.role
        color: control.hovered ? Theme.linkHover : Theme.link
        elide: control.elide
    }

    background: Item {
        FocusRing {
            targetRadius: 2
            visible: control.visualFocus
        }
    }
}
