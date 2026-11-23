pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import OpenWeir

// Signing in to an OpenWeir provider (DESIGN.md "服务商登录对话框", docs/accounts.md §7.3):
// one step a screen, in place, which ProviderSignInViewModel decides; the dialog is 400px
// wide, 640px while it lists the subscriptions to pick from. Errors go under the field they
// belong to, and into its description; what no field caused goes above the buttons. While a
// request runs, fields and buttons wait and Esc still cancels, which drops its answer.
AppDialog {
    id: dialog

    required property ProviderSignInViewModel signIn
    readonly property string step: signIn.step
    readonly property bool busy: signIn.busy
    readonly property bool emailCode: signIn.method === "email_code"
    readonly property bool webOnly: signIn.unavailable !== ""
    // Opened with the address filled in (an openweir://provider link): 继续 has the focus, so
    // the user checks the address and goes on (docs/accounts.md §7.3).
    property bool startOnContinue: false
    // 从链接导入 was chosen: once the dialog has gone, the subscriptions page opens its link
    // form (Main.qml), which takes the focus instead of the control the dialog came from.
    property bool importAfterClose: false

    // Shows the dialog for a new flow, `from` getting the focus back; while it shows already,
    // fills in the address its first step shows. A dialog still fading out opens again.
    function start(from: Item, reason: int) {
        addressField.text = signIn.address
        startOnContinue = signIn.address !== ""
        if (holdsFocus) {
            Qt.callLater(focusStep)
            return
        }
        for (const field of [identifierField, passwordField, emailField, codeField, twoFactorField])
            field.clear()
        importAfterClose = false
        showFor(from, reason)
    }

    function submit() {
        if (step === "address")
            signIn.submitAddress(addressField.text)
        else if (step === "sign_in" && emailCode)
            signIn.signInEmailCode(emailField.text, codeField.text)
        else if (step === "sign_in")
            signIn.signInPassword(identifierField.text, passwordField.text)
        else if (step === "two_factor")
            signIn.verify(twoFactorField.text)
    }

    // The control a step starts on: a field with an error first, then the one to fill in.
    function stepFocus(): Item {
        switch (step) {
        case "address":
            return startOnContinue && signIn.addressError === "" ? continueButton : addressField
        case "sign_in":
            if (webOnly)
                return linkImportButton
            if (emailCode)
                return signIn.codeSent && signIn.emailError === "" ? codeField : emailField
            if (signIn.passwordError === "" && (signIn.identifierError !== "" || identifierField.text.trim() === ""))
                return identifierField
            return passwordField
        case "two_factor":
            return twoFactorField
        case "replace":
            return cancelButton
        case "pick":
            return picker
        default:
            return doneButton
        }
    }

    // Focus goes to the step's control as the step shows, and back to it after a request:
    // fields and buttons that waited have given their focus up. A field with an error has
    // its text selected, to be typed over.
    function focusStep() {
        if (!visible)
            return
        const item = stepFocus()
        if (!item || !item.visible || !item.enabled)
            return
        item.forceActiveFocus(Qt.OtherFocusReason)
        const field = item as AppTextField
        if (field?.error)
            field.selectAll()
        if (item === picker)
            picker.reveal(picker.selectedIndex())
    }

    function pickMethod(method: string) {
        // The email typed to sign in with a password is the one to send a code to.
        if (method === "email_code" && emailField.text === "" && identifierField.text.indexOf("@") > 0)
            emailField.text = identifierField.text.trim()
        signIn.setMethod(method)
        Qt.callLater(focusStep)
    }

    title: signIn.title
    wide: step === "pick"
    initialFocus: stepFocus()
    defaultButton: step === "address" ? continueButton
        : step === "sign_in" ? (webOnly ? linkImportButton : signInButton)
        : step === "two_factor" ? verifyButton
        : step === "replace" ? cancelButton
        : step === "pick" ? useButton : doneButton

    onStepChanged: {
        if (step === "two_factor")
            twoFactorField.clear()
        if (step !== "address")
            startOnContinue = false
        Qt.callLater(focusStep)
    }
    onBusyChanged: {
        if (!busy)
            Qt.callLater(focusStep)
    }
    // Esc, or anything else that closes the dialog, cancels the flow; a flow that ended
    // closed it itself.
    onAboutToHide: {
        if (signIn.active)
            signIn.cancel()
    }
    onClosed: {
        passwordField.clear()
        codeField.clear()
        twoFactorField.clear()
    }

    Connections {
        target: dialog.signIn

        function onCloseRequested() {
            dialog.close()
        }

        function onLinkImportRequested() {
            dialog.returnFocusTo = null
            dialog.importAfterClose = true
        }
    }

    // 1 服务商地址
    Field {
        visible: dialog.step === "address"
        label: qsTr("服务商地址")
        error: dialog.signIn.addressError

        AppTextField {
            id: addressField

            Layout.fillWidth: true
            enabled: !dialog.busy
            placeholderText: "https://example.com"
            inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoAutoUppercase
            error: dialog.signIn.addressError !== ""
            Accessible.name: qsTr("服务商地址")
            Accessible.description: dialog.signIn.addressError
            onTextEdited: dialog.startOnContinue = false
        }
    }

    // 2 登录「示例服务商」
    ColumnLayout {
        Layout.fillWidth: true
        visible: dialog.step === "sign_in"
        spacing: Theme.space4

        UiText {
            Layout.fillWidth: true
            text: dialog.signIn.host
            role: Theme.type.caption
            color: Theme.textMuted
            elide: Text.ElideMiddle
        }

        SegmentedControl {
            visible: dialog.signIn.methodOptions.length > 0
            enabled: !dialog.busy
            options: dialog.signIn.methodOptions
            current: dialog.signIn.method
            accessibleName: qsTr("登录方式")
            onPicked: value => dialog.pickMethod(value)
        }

        // The provider signs in on its web pages only: why, and 从链接导入 below.
        AccessibleText {
            Layout.fillWidth: true
            visible: dialog.webOnly
            text: dialog.signIn.unavailable
            role: Theme.type.bodySm
            color: Theme.textSecondary
            wrapMode: Text.Wrap
        }

        Field {
            visible: !dialog.webOnly && !dialog.emailCode
            label: dialog.signIn.identifierLabel
            error: dialog.signIn.identifierError

            AppTextField {
                id: identifierField

                Layout.fillWidth: true
                enabled: !dialog.busy
                inputMethodHints: Qt.ImhEmailCharactersOnly | Qt.ImhNoAutoUppercase
                error: dialog.signIn.identifierError !== ""
                Accessible.name: dialog.signIn.identifierLabel
                Accessible.description: dialog.signIn.identifierError
            }
        }

        Field {
            visible: !dialog.webOnly && !dialog.emailCode
            label: qsTr("密码")
            error: dialog.signIn.passwordError

            AppTextField {
                id: passwordField

                Layout.fillWidth: true
                enabled: !dialog.busy
                echoMode: TextInput.Password
                inputMethodHints: Qt.ImhHiddenText | Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
                error: dialog.signIn.passwordError !== ""
                Accessible.name: qsTr("密码")
                Accessible.description: dialog.signIn.passwordError
            }
        }

        Field {
            visible: !dialog.webOnly && dialog.emailCode
            label: qsTr("邮箱")
            error: dialog.signIn.emailError

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.space2

                AppTextField {
                    id: emailField

                    Layout.fillWidth: true
                    enabled: !dialog.busy
                    inputMethodHints: Qt.ImhEmailCharactersOnly | Qt.ImhNoAutoUppercase
                    error: dialog.signIn.emailError !== ""
                    Accessible.name: qsTr("邮箱")
                    Accessible.description: dialog.signIn.emailError
                    // Enter sends the code until there is one to sign in with.
                    onAccepted: {
                        if (!dialog.signIn.codeSent)
                            dialog.signIn.sendCode(text)
                    }
                }

                // 发送验证码, then 重新发送（42） for a minute; the seconds keep their width.
                AppButton {
                    id: sendButton

                    variant: "compact"
                    text: dialog.signIn.sendLabel
                    font.features: ({
                            "tnum": 1
                        })
                    enabled: !dialog.busy && dialog.signIn.resendSeconds === 0 && emailField.text.trim() !== ""
                    onClicked: dialog.signIn.sendCode(emailField.text)
                }
            }
        }

        Field {
            visible: !dialog.webOnly && dialog.emailCode && dialog.signIn.codeSent
            label: qsTr("验证码")
            error: dialog.signIn.codeError

            AppTextField {
                id: codeField

                Layout.fillWidth: true
                enabled: !dialog.busy
                inputMethodHints: Qt.ImhDigitsOnly | Qt.ImhNoPredictiveText
                error: dialog.signIn.codeError !== ""
                Accessible.name: qsTr("验证码")
                Accessible.description: dialog.signIn.codeError
            }
        }
    }

    // 3 两步验证
    ColumnLayout {
        Layout.fillWidth: true
        visible: dialog.step === "two_factor"
        spacing: Theme.space4

        AccessibleText {
            Layout.fillWidth: true
            text: dialog.signIn.message
            role: Theme.type.bodySm
            color: Theme.textSecondary
            wrapMode: Text.Wrap
        }

        Field {
            label: qsTr("验证码")
            error: dialog.signIn.twoFactorError

            AppTextField {
                id: twoFactorField

                Layout.fillWidth: true
                enabled: !dialog.busy
                inputMethodHints: Qt.ImhDigitsOnly | Qt.ImhNoPredictiveText
                error: dialog.signIn.twoFactorError !== ""
                Accessible.name: qsTr("验证码")
                Accessible.description: dialog.signIn.twoFactorError
            }
        }
    }

    // 替换「a@example.com」？ and 这个账户还没有订阅: a sentence each.
    AccessibleText {
        Layout.fillWidth: true
        visible: dialog.step === "replace" || dialog.step === "empty"
        text: dialog.signIn.message
        role: Theme.type.bodySm
        color: Theme.textSecondary
        wrapMode: Text.Wrap
    }

    // 5 选择要使用的订阅: one Tab stop; ↑ ↓ move the choice past the subscriptions that
    // cannot be used, whose 续费 buttons follow the list in the Tab order. The focus stays on
    // the list, so screen readers are told the row the choice moves to. No FocusScope: Tab
    // back to the list would hand its focus on to the 续费 last focused.
    Item {
        id: picker

        readonly property var rows: dialog.signIn.subscriptions

        function selectedIndex(): int {
            return rows.findIndex(row => row.remoteId === dialog.signIn.selected)
        }

        function move(delta: int) {
            for (let i = selectedIndex() + delta; i >= 0 && i < rows.length; i += delta) {
                if (!rows[i].usable)
                    continue
                dialog.signIn.select(rows[i].remoteId)
                reveal(i)
                ScreenReader.announce(picker, ScreenReader.join([rows[i].name, rows[i].status, rows[i].details]))
                return
            }
        }

        // Scrolls the list so that row `index` shows whole.
        function reveal(index: int) {
            if (index < 0)
                return
            const top = index * Theme.rowDouble
            if (top < pickerView.contentY)
                pickerView.contentY = top
            else if (top + Theme.rowDouble > pickerView.contentY + pickerView.height)
                pickerView.contentY = top + Theme.rowDouble - pickerView.height
        }

        Layout.fillWidth: true
        // Five rows at most; more scroll inside the box.
        Layout.preferredHeight: Math.min(Math.max(rows.length, 1), 5) * Theme.rowDouble + 2
        visible: dialog.step === "pick"
        enabled: !dialog.busy
        activeFocusOnTab: true
        Accessible.role: Accessible.List
        Accessible.name: qsTr("订阅")
        // The arrows are the list's own, not its 续费 buttons'.
        Keys.onUpPressed: event => {
            event.accepted = Window.activeFocusItem === picker
            if (event.accepted)
                move(-1)
        }
        Keys.onDownPressed: event => {
            event.accepted = Window.activeFocusItem === picker
            if (event.accepted)
                move(1)
        }

        Rectangle {
            anchors.fill: parent
            radius: Theme.radiusSm
            color: Theme.surface
            border.width: 1
            border.color: Theme.border

            FocusRing {
                targetRadius: Theme.radiusSm
                visible: picker.activeFocus
            }
        }

        Flickable {
            id: pickerView

            anchors.fill: parent
            anchors.margins: 1
            clip: true
            contentHeight: pickerColumn.height
            boundsBehavior: Flickable.StopAtBounds

            ScrollBar.vertical: ScrollBar {}

            Column {
                id: pickerColumn

                width: pickerView.width

                Repeater {
                    model: picker.rows

                    delegate: Item {
                        id: row

                        required property var modelData
                        required property int index
                        readonly property bool usable: modelData.usable
                        readonly property bool chosen: modelData.remoteId === dialog.signIn.selected

                        width: pickerColumn.width
                        height: Theme.rowDouble

                        // The row itself: chosen by a click, never a Tab stop of its own.
                        AbstractButton {
                            id: choice

                            anchors.fill: parent
                            focusPolicy: Qt.NoFocus
                            enabled: row.usable
                            checkable: false
                            checked: row.chosen  // what screen readers hear
                            Accessible.role: Accessible.RadioButton
                            Accessible.name: row.modelData.name
                            Accessible.description: ScreenReader.join([row.modelData.status, row.modelData.details])
                            onClicked: {
                                dialog.signIn.select(row.modelData.remoteId)
                                picker.forceActiveFocus(Qt.MouseFocusReason)
                            }

                            background: Rectangle {
                                color: row.chosen && picker.activeFocus ? Theme.surfaceSelected
                                    : choice.hovered ? Theme.surfaceSubtle : Theme.surface

                                Rectangle {
                                    visible: row.index > 0
                                    width: parent.width
                                    height: 1
                                    color: Theme.divider
                                }
                            }
                        }

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: Theme.space4
                            anchors.rightMargin: Theme.space4
                            spacing: Theme.space3

                            // No ring for one that cannot be chosen; its place stays, so the
                            // names line up.
                            Item {
                                implicitWidth: 18
                                implicitHeight: 18

                                RadioMark {
                                    visible: row.usable
                                    checked: row.chosen
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0

                                UiText {
                                    Layout.fillWidth: true
                                    text: row.modelData.name
                                    role: row.chosen ? Theme.type.bodyMdStrong : Theme.type.bodyMd
                                    color: row.usable ? Theme.text : Theme.textMuted
                                    elide: Text.ElideRight
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 0

                                    UiText {
                                        text: row.modelData.status
                                        role: Theme.type.caption
                                        color: row.modelData.tone === "danger" ? Theme.danger
                                            : row.modelData.tone === "warning" ? Theme.warning : Theme.textMuted
                                    }

                                    UiText {
                                        Layout.fillWidth: true
                                        visible: row.modelData.details !== ""
                                        text: " · " + row.modelData.details
                                        role: Theme.type.caption
                                        color: Theme.textMuted
                                        elide: Text.ElideRight
                                    }
                                }
                            }

                            ExternalButton {
                                visible: !row.usable
                                variant: "compact"
                                text: qsTr("续费")
                                accessibleName: qsTr("续费 %1").arg(row.modelData.name)
                                onClicked: dialog.signIn.renew(row.modelData.remoteId)
                            }
                        }
                    }
                }
            }
        }

        ScrollToFocus {
            flickable: pickerView
        }
    }

    // What no field caused, such as no connection to the provider.
    AccessibleText {
        Layout.fillWidth: true
        visible: text !== ""
        text: dialog.signIn.error
        role: Theme.type.caption
        color: Theme.danger
        wrapMode: Text.Wrap
    }

    leadingButtons: [
        AppButton {
            text: qsTr("上一步")
            visible: dialog.step === "sign_in" || dialog.step === "two_factor"
            enabled: !dialog.busy
            onClicked: dialog.signIn.back()
        }
    ]

    buttons: [
        AppButton {
            id: cancelButton

            text: qsTr("取消")
            visible: ["address", "sign_in", "two_factor", "replace"].includes(dialog.step)
            // A replace is destructive: 取消 is its default (DESIGN.md "破坏性确认框").
            variant: dialog.step === "replace" ? "primary" : "secondary"
            enabled: !dialog.busy
            onClicked: dialog.signIn.cancel()
        },
        AppButton {
            text: qsTr("稍后")
            visible: dialog.step === "pick"
            enabled: !dialog.busy
            onClicked: dialog.signIn.later()
        },
        ExternalButton {
            text: qsTr("选购套餐")
            visible: dialog.step === "empty"
            onClicked: dialog.signIn.browsePlans()
        },
        AppButton {
            id: continueButton

            text: dialog.busy ? qsTr("正在检查…") : qsTr("继续")
            visible: dialog.step === "address"
            variant: "primary"
            enabled: !dialog.busy && addressField.text.trim() !== ""
            onClicked: dialog.submit()
        },
        AppButton {
            id: signInButton

            text: dialog.busy && !dialog.signIn.sending ? qsTr("正在登录…") : qsTr("登录")
            visible: dialog.step === "sign_in" && !dialog.webOnly
            variant: "primary"
            enabled: !dialog.busy && (dialog.emailCode
                ? dialog.signIn.codeSent && emailField.text.trim() !== "" && codeField.text.trim() !== ""
                : identifierField.text.trim() !== "" && passwordField.text !== "")
            onClicked: dialog.submit()
        },
        AppButton {
            id: linkImportButton

            text: qsTr("从链接导入")
            visible: dialog.step === "sign_in" && dialog.webOnly
            variant: "primary"
            enabled: !dialog.busy
            onClicked: dialog.signIn.importFromLink()
        },
        AppButton {
            id: verifyButton

            text: dialog.busy ? qsTr("正在验证…") : qsTr("验证")
            visible: dialog.step === "two_factor"
            variant: "primary"
            enabled: !dialog.busy && twoFactorField.text.trim() !== ""
            onClicked: dialog.submit()
        },
        AppButton {
            text: dialog.busy ? qsTr("正在替换…") : qsTr("替换")
            visible: dialog.step === "replace"
            variant: "destructive"
            enabled: !dialog.busy
            onClicked: dialog.signIn.confirmReplace()
        },
        AppButton {
            id: useButton

            text: dialog.busy ? qsTr("正在应用…") : qsTr("使用")
            visible: dialog.step === "pick"
            variant: "primary"
            enabled: dialog.signIn.canUse
            onClicked: dialog.signIn.use()
        },
        AppButton {
            id: doneButton

            text: qsTr("完成")
            visible: dialog.step === "empty"
            variant: "primary"
            onClicked: dialog.signIn.later()
        }
    ]

    // A labelled field (DESIGN.md "条件编辑表单"): the label 6px above the control, the error
    // under it in input-error.
    component Field: ColumnLayout {
        id: field

        property string label
        property string error
        default property alias control: holder.data

        Layout.fillWidth: true
        spacing: 6

        UiText {
            text: field.label
            role: Theme.type.caption
            color: Theme.textTertiary
        }

        ColumnLayout {
            id: holder

            Layout.fillWidth: true
            spacing: 0
        }

        AccessibleText {
            Layout.fillWidth: true
            visible: text !== ""
            text: field.error
            role: Theme.type.caption
            color: Theme.danger
            wrapMode: Text.Wrap
        }
    }
}
