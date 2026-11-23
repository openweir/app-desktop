import QtQuick
import QtQuick.Layouts

// The external-link button (DESIGN.md "外链按钮"): it opens a page of the provider's user
// center in the browser. Its text is followed, 6px on, by the 14px `external` glyph in the
// text's colour. Screen readers hear `accessibleName` (the text, with the object when the
// text is a bare verb) and that it opens in the browser; the glyph itself is not read.
AppButton {
    id: control

    property string accessibleName: text

    Accessible.name: qsTr("%1（在浏览器中打开）").arg(accessibleName)

    contentItem: RowLayout {
        spacing: 6

        Text {
            text: control.text
            font: control.font
            color: Theme.text
            verticalAlignment: Text.AlignVCenter
            Accessible.ignored: true
        }

        Icon {
            name: "external"
            size: 14
            color: Theme.text
        }
    }
}
