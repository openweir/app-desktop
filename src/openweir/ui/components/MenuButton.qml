import QtQuick
import QtQuick.Layouts

// A menu button (DESIGN.md "按钮 → 菜单按钮"), such as the subscriptions page's 添加: a primary
// or secondary button whose text a 14px chevron follows, 8px apart. A click, Space, Enter or
// ↓ opens `menu` under it, right edges aligned as a more menu's, and the focus comes back to
// it as the menu closes. Screen readers hear a button with a menu, named by its text.
AppButton {
    id: control

    required property AppMenu menu

    function open() {
        menu.openBelow(control)
    }

    Accessible.role: Accessible.ButtonMenu
    Accessible.name: text
    onClicked: open()
    Keys.onReturnPressed: open()
    Keys.onEnterPressed: open()
    Keys.onDownPressed: open()

    contentItem: RowLayout {
        spacing: Theme.space2

        Text {
            Layout.fillWidth: true
            text: control.text
            font: control.font
            color: control.primary ? Theme.textOnPrimary : Theme.text
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }

        Icon {
            name: "chevronDown"
            size: 14
            color: control.primary ? Theme.textOnPrimary : Theme.text
        }
    }
}
