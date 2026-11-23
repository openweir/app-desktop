pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Dialogs
import QtQuick.Layouts
import OpenWeir
import "../components"

ScrollView {
    id: page

    required property ProfilesViewModel profiles
    property bool importOpen: false
    property Item overrideTrigger  // gets focus back when the override editor closes
    property int overrideFocusReason: Qt.OtherFocusReason
    // The profiles whose removal, or whose account's sign-out, was confirmed.
    property var removingIds: []
    // What opened the provider sign-in dialog last (登录服务商…, 重新登录…), which gets focus
    // back when it closes.
    property Item signInTrigger
    property int signInFocusReason: Qt.OtherFocusReason
    // The page's one primary action (DESIGN.md "添加菜单"); the focus goes there when there is
    // nowhere else, such as after the last row went.
    readonly property alias addButton: addMenuButton

    readonly property int tableMinWidth: 680
    readonly property int usageWidth: 170
    readonly property int expiryWidth: 110
    readonly property int updatedWidth: 110

    function openImport() {
        importOpen = true
        urlField.forceActiveFocus()
    }

    // Closing the form hands its focus back to the button whose menu opened it.
    function closeImport() {
        const focused = Window.activeFocusItem
        const refocus = Focus.within(focused, importForm)
        importOpen = false
        urlField.clear()
        nameField.clear()
        autoUpdate.setInterval(true, 0)
        page.profiles.clearImportError()
        if (refocus)
            addMenuButton.forceActiveFocus(Focus.reasonAfter(focused))
    }

    // The provider sign-in dialog, which the window opens: on the provider of the
    // subscription `profileId`, or on no address for "". `trigger` gets the focus back.
    function requestSignIn(trigger: Item, reason: int, profileId: string) {
        signInTrigger = trigger
        signInFocusReason = reason
        if (profileId === "")
            page.profiles.requestProviderSignIn("")
        else
            page.profiles.signInAgain(profileId)
    }

    // The import form as the user left it, which the window keeps while it lets the page go
    // (Main.qml); null while the form is closed.
    function importDraft(): var {
        if (!importOpen)
            return null
        return { url: urlField.text, name: nameField.text, autoUpdate: autoUpdate.checked, interval: autoUpdate.intervalText }
    }

    // The form as importDraft() kept it, as the page loads again; the focus stays where it is.
    function restoreImport(draft: var) {
        if (!draft)
            return
        urlField.text = draft.url
        nameField.text = draft.name
        autoUpdate.checked = draft.autoUpdate
        autoUpdate.intervalText = draft.interval ?? ""
        importOpen = true
    }

    function rowAt(index: int): ProfileRow {
        return profileRepeater.itemAt(Math.max(0, Math.min(index, profileRepeater.count - 1))) as ProfileRow
    }

    // The row that takes the place of one at `index` that went: the first after it that
    // stays, else the last before it; null when none stays.
    function survivor(index: int): ProfileRow {
        for (let i = index; i < profileRepeater.count; i++) {
            const row = profileRepeater.itemAt(i) as ProfileRow
            if (!removingIds.includes(row.profileId))
                return row
        }
        for (let i = Math.min(index, profileRepeater.count) - 1; i >= 0; i--) {
            const row = profileRepeater.itemAt(i) as ProfileRow
            if (!removingIds.includes(row.profileId))
                return row
        }
        return null
    }

    // A removed row takes the focus with it, so the focus moves to the row that takes its
    // place, or the one above when it was the last; with no rows left, to 添加. Signing out
    // removes several rows at once: the focus skips those that go too. While the
    // confirmation is still closing, it hands its focus there instead.
    function rowRemoved(index: int, row: ProfileRow) {
        if (!removingIds.includes(row.profileId))
            return
        removingIds = removingIds.filter(id => id !== row.profileId)
        const next = survivor(index)?.moreButton ?? addMenuButton
        const dialog = removeDialog.visible ? removeDialog : signOutDialog.visible ? signOutDialog : null
        if (dialog)
            dialog.returnFocusTo = next
        else if (row.moreButton.activeFocus)
            next.forceActiveFocus(removeDialog.returnFocusReason)
    }

    function submitImport() {
        if (urlField.text.trim() !== "" && !page.profiles.importing && autoUpdate.acceptableInput)
            page.profiles.importUrl(urlField.text, nameField.text, autoUpdate.checked, autoUpdate.hours, autoUpdate.suggested)
    }

    contentWidth: availableWidth
    clip: true

    ScrollToFocus {
        flickable: page.contentItem as Flickable
    }

    ScrollToFocus {
        flickable: table
        vertical: false
    }

    Connections {
        target: page.profiles

        function onImported() {
            if (nodeLinksDialog.visible) {
                nodeLinksDialog.close()
                return
            }
            if (page.importOpen)
                page.closeImport()
        }

        function onEditOpened() {
            profileDialog.showFor(profileMenu.trigger, profileMenu.triggerFocusReason)
        }

        function onEditSaved() {
            profileDialog.close()
        }

        // An install link (docs/release.md §7): the form, filled in, for the user to check.
        function onImportPrefilled() {
            urlField.text = page.profiles.prefillUrl
            nameField.text = page.profiles.prefillName
            page.openImport()
        }

        function onRemovalRequested() {
            removeDialog.showFor(profileMenu.trigger, profileMenu.triggerFocusReason)
        }

        function onSignOutRequested() {
            signOutDialog.showFor(profileMenu.trigger, profileMenu.triggerFocusReason)
        }

        function onOverrideOpened() {
            overrideDialog.showFor(page.overrideTrigger, page.overrideFocusReason)
        }

        function onOverrideSaved() {
            overrideDialog.close()
        }
    }

    ColumnLayout {
        width: page.availableWidth
        spacing: Theme.space5

        PageHeader {
            Layout.fillWidth: true
            Layout.topMargin: Theme.titlebarPageTop
            Layout.leftMargin: Theme.pageX
            Layout.rightMargin: Theme.pageX
            title: qsTr("订阅")
            detail: page.profiles.summary
            detailColor: page.profiles.hasError ? Theme.danger : Theme.textMuted

            // Every way to add a subscription (DESIGN.md "添加菜单"). It is the page's one
            // primary action; the form's own button takes over while the form is open.
            MenuButton {
                id: addMenuButton

                text: qsTr("添加")
                variant: page.importOpen ? "secondary" : "primary"
                menu: addMenu
            }
        }

        ColumnLayout {
            id: importForm

            Layout.fillWidth: true
            Layout.leftMargin: Theme.pageX
            Layout.rightMargin: Theme.pageX
            visible: page.importOpen
            spacing: Theme.space2

            SectionLabel {
                text: qsTr("从链接导入")
            }

            Group {
                Layout.fillWidth: true

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.leftMargin: Theme.space4
                    Layout.rightMargin: Theme.space4
                    Layout.topMargin: 14
                    Layout.bottomMargin: 14
                    spacing: Theme.space3

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space3

                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.preferredWidth: 320
                            spacing: 6

                            UiText {
                                text: qsTr("订阅链接")
                                role: Theme.type.caption
                                color: Theme.textTertiary
                            }

                            AppTextField {
                                id: urlField

                                Layout.fillWidth: true
                                placeholderText: "https://sub.example.com/…"
                                inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoAutoUppercase
                                enabled: !page.profiles.importing
                                Accessible.name: qsTr("订阅链接")
                                onAccepted: page.submitImport()
                                Keys.onEscapePressed: page.closeImport()
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.preferredWidth: 160
                            Layout.maximumWidth: 240
                            spacing: 6

                            UiText {
                                text: qsTr("名称")
                                role: Theme.type.caption
                                color: Theme.textTertiary
                            }

                            AppTextField {
                                id: nameField

                                Layout.fillWidth: true
                                placeholderText: qsTr("可选")
                                enabled: !page.profiles.importing
                                Accessible.name: qsTr("名称")
                                onAccepted: page.submitImport()
                                Keys.onEscapePressed: page.closeImport()
                            }
                        }

                        AppButton {
                            Layout.alignment: Qt.AlignBottom
                            text: qsTr("取消")
                            onClicked: page.closeImport()
                        }

                        AppButton {
                            Layout.alignment: Qt.AlignBottom
                            text: page.profiles.importing ? qsTr("正在导入…") : qsTr("导入")
                            variant: "primary"
                            enabled: urlField.text.trim() !== "" && !page.profiles.importing && autoUpdate.acceptableInput
                            onClicked: page.submitImport()
                        }
                    }

                    AutoUpdateField {
                        id: autoUpdate

                        allowSuggestion: true
                        defaultHours: page.profiles.defaultAutoUpdateHours
                        checked: true
                        enabled: !page.profiles.importing
                        onAccepted: page.submitImport()
                    }

                    AccessibleText {
                        Layout.fillWidth: true
                        visible: text !== ""
                        text: page.profiles.importError
                        role: Theme.type.caption
                        color: Theme.danger
                        wrapMode: Text.Wrap
                    }
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.leftMargin: Theme.pageX
            Layout.rightMargin: Theme.pageX
            spacing: Theme.space2

            SectionLabel {
                text: qsTr("全部订阅")
            }

            Group {
                Layout.fillWidth: true

                // Columns keep their widths; a narrow window scrolls the table inside its group.
                Flickable {
                    id: table

                    Layout.fillWidth: true
                    implicitHeight: rows.implicitHeight + (horizontalBar.visible ? horizontalBar.height : 0)
                    contentWidth: Math.max(width, page.tableMinWidth)
                    contentHeight: rows.implicitHeight
                    flickableDirection: Flickable.HorizontalFlick
                    boundsBehavior: Flickable.StopAtBounds
                    clip: true
                    Accessible.role: Accessible.List
                    Accessible.name: qsTr("全部订阅")

                    ScrollBar.horizontal: ScrollBar {
                        id: horizontalBar

                        policy: table.contentWidth > table.width ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
                    }

                    ColumnLayout {
                        id: rows

                        width: table.contentWidth
                        spacing: 0

                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: Theme.rowHeader
                            topLeftRadius: Theme.radiusMd - 1
                            topRightRadius: Theme.radiusMd - 1
                            color: Theme.surfaceSubtle
                            Accessible.ignored: true

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: Theme.space4
                                anchors.rightMargin: Theme.space4
                                spacing: Theme.space3

                                UiText {
                                    Layout.fillWidth: true
                                    text: qsTr("名称")
                                    role: Theme.type.caption
                                    color: Theme.textMuted
                                }

                                UiText {
                                    Layout.preferredWidth: page.usageWidth
                                    text: qsTr("用量")
                                    role: Theme.type.caption
                                    color: Theme.textMuted
                                }

                                UiText {
                                    Layout.preferredWidth: page.expiryWidth
                                    text: qsTr("到期")
                                    role: Theme.type.caption
                                    color: Theme.textMuted
                                }

                                UiText {
                                    Layout.preferredWidth: page.updatedWidth
                                    text: qsTr("更新")
                                    role: Theme.type.caption
                                    color: Theme.textMuted
                                }

                                Item {
                                    implicitWidth: 28 - Theme.space1
                                }
                            }
                        }

                        Repeater {
                            id: profileRepeater

                            model: page.profiles.model
                            delegate: ProfileRow {}
                            onItemRemoved: (index, item) => page.rowRemoved(index, item as ProfileRow)
                        }

                        GroupRow {
                            visible: profileRepeater.count === 0
                            rowHeight: Theme.rowSingle

                            AccessibleText {
                                Layout.fillWidth: true
                                text: qsTr("尚未导入订阅")
                                role: Theme.type.bodySm
                                color: Theme.textMuted
                            }
                        }
                    }
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.leftMargin: Theme.pageX
            Layout.rightMargin: Theme.pageX
            Layout.bottomMargin: Theme.pageBottom
            spacing: Theme.space2

            SectionLabel {
                text: qsTr("覆写")
            }

            Group {
                Layout.fillWidth: true

                GroupRow {
                    divider: false

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0

                        UiText {
                            Layout.fillWidth: true
                            text: qsTr("全局覆写")
                            role: Theme.type.labelMd
                        }

                        AccessibleText {
                            Layout.fillWidth: true
                            text: page.profiles.globalOverrideDetail
                            role: Theme.type.caption
                            color: Theme.textMuted
                            elide: Text.ElideRight
                        }
                    }

                    // The global override is checked with the routing in use, so it needs
                    // something to route with.
                    AppButton {
                        id: globalOverrideButton

                        variant: "compact"
                        text: qsTr("编辑")
                        enabled: page.profiles.canEditGlobalOverride
                        Accessible.name: qsTr("编辑全局覆写")
                        onClicked: {
                            page.overrideTrigger = globalOverrideButton
                            page.overrideFocusReason = Focus.reasonAfter(globalOverrideButton)
                            page.profiles.editGlobalOverride()
                        }
                    }
                }
            }
        }
    }

    // One subscription: its name and where it comes from, with its state in the caption
    // (docs/routing.md §4.2); its menu holds what can be done with it. The menu button is the
    // row's Tab stop, and the arrows move between the rows' buttons.
    component ProfileRow: Item {
        id: profileRow

        required property int index
        required property string profileId
        required property string name
        required property string source
        required property string note
        required property string leftOut
        required property string usage
        required property bool hasQuota
        required property real usedFraction
        required property string expiry
        required property bool expired
        required property string updated
        required property bool updateFailed
        required property bool updating
        required property string refreshLabel
        required property bool isSubscription
        required property bool canUpdate
        required property bool autoUpdate
        required property string autoUpdateLabel
        required property bool inPool
        required property bool hasRules
        required property bool canAutoUpdate
        required property bool isProvider
        required property string status
        required property bool statusDanger
        required property bool canRemove
        required property bool canRenew
        required property bool needsSignIn
        required property string providerName
        readonly property alias moreButton: moreButton
        // The caption as it reads: where it comes from, then its note and status.
        readonly property string caption: [source, note, status].filter(part => part !== "").join(qsTr(" · "))

        Layout.fillWidth: true
        implicitHeight: Theme.rowDouble
        Accessible.role: Accessible.ListItem
        Accessible.name: name
        Accessible.description: qsTr("%1。用量 %2，到期 %3，更新 %4").arg(caption).arg(usage).arg(expiry).arg(updated)
        // A right-click opens its menu at the pointer (DESIGN.md "右键菜单").
        ContextMenu.onRequested: position => profileMenu.openContext(profileRow.moreButton, profileRow, position)

        Rectangle {
            anchors.fill: parent
            bottomLeftRadius: profileRow.index === profileRepeater.count - 1 ? Theme.radiusMd - 1 : 0
            bottomRightRadius: bottomLeftRadius
            color: hover.hovered ? Theme.surfaceSubtle : "transparent"

            Rectangle {
                width: parent.width
                height: 1
                color: Theme.divider
            }
        }

        HoverHandler {
            id: hover
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Theme.space4
            anchors.rightMargin: Theme.space3
            spacing: Theme.space3

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0

                UiText {
                    Layout.fillWidth: true
                    text: profileRow.name
                    role: Theme.type.labelMd
                    elide: Text.ElideRight
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 0

                    UiText {
                        Layout.preferredWidth: Math.ceil(implicitWidth)
                        Layout.maximumWidth: Math.ceil(implicitWidth)
                        Layout.fillWidth: true
                        text: profileRow.source
                        role: Theme.type.caption
                        color: Theme.textMuted
                        elide: Text.ElideRight
                    }

                    UiText {
                        visible: profileRow.note !== ""
                        text: qsTr(" · %1").arg(profileRow.note)
                        role: Theme.type.caption
                        color: Theme.textSecondary
                    }

                    // What the core left out reading it (D54), as DESIGN.md "分组与行" colours
                    // a warning; a line has one, and a provider's status comes first.
                    UiText {
                        visible: profileRow.leftOut !== "" && profileRow.status === ""
                        text: qsTr(" · %1").arg(profileRow.leftOut)
                        role: Theme.type.caption
                        color: Theme.warning
                    }

                    // What the account says of a provider's subscription (DESIGN.md "服务商的
                    // 订阅行"): danger when the provider stopped it.
                    UiText {
                        visible: profileRow.status !== ""
                        text: qsTr(" · %1").arg(profileRow.status)
                        role: Theme.type.caption
                        color: profileRow.statusDanger ? Theme.danger : Theme.warning
                    }

                    Item {
                        Layout.fillWidth: true
                        Layout.preferredWidth: 0
                    }
                }
            }

            // Nested layouts fill by default; the data columns keep fixed widths.
            ColumnLayout {
                Layout.fillWidth: false
                Layout.preferredWidth: page.usageWidth
                spacing: 5

                UiText {
                    text: profileRow.usage
                    role: Theme.type.dataSm
                    color: Theme.textSecondary
                }

                UsageBar {
                    Layout.fillWidth: true
                    visible: profileRow.hasQuota
                    fraction: profileRow.usedFraction
                }
            }

            UiText {
                Layout.preferredWidth: page.expiryWidth
                text: profileRow.expiry
                role: Theme.type.bodySm
                color: profileRow.expired ? Theme.warning : Theme.textSecondary
                font.features: ({
                        "tnum": 1
                    })
            }

            UiText {
                Layout.preferredWidth: page.updatedWidth
                text: profileRow.updated
                role: Theme.type.bodySm
                color: profileRow.updateFailed ? Theme.warning : Theme.textMuted
                elide: Text.ElideRight
            }

            IconButton {
                id: moreButton

                iconName: "more"
                accessibleName: qsTr("%1 的更多操作").arg(profileRow.name)
                onClicked: profileMenu.openFor(profileRow, moreButton)
                Keys.onUpPressed: page.rowAt(profileRow.index - 1).moreButton.forceActiveFocus(Qt.TabFocusReason)
                Keys.onDownPressed: page.rowAt(profileRow.index + 1).moreButton.forceActiveFocus(Qt.TabFocusReason)
            }
        }
    }

    AppMenu {
        id: addMenu

        AppMenuItem {
            text: qsTr("从链接导入…")
            enabled: !page.profiles.importing
            onTriggered: page.openImport()
        }

        AppMenuItem {
            text: qsTr("导入文件…")
            enabled: !page.profiles.importing
            onTriggered: fileDialog.open()
        }

        AppMenuItem {
            text: qsTr("导入节点…")
            enabled: !page.profiles.importing
            onTriggered: nodeLinksDialog.showFor(addMenu.trigger, addMenu.triggerFocusReason)
        }

        AppMenuItem {
            text: qsTr("登录服务商…")
            separatorBefore: true
            onTriggered: page.requestSignIn(addMenu.trigger, addMenu.triggerFocusReason, "")
        }
    }

    // A provider's subscription has the account's name, no link, and goes with the account,
    // so its menu has neither 编辑信息… nor 复制订阅链接 nor 删除, unless the account no
    // longer has it; it has the account's pages and sign-in instead (docs/accounts.md §7.2).
    AppMenu {
        id: profileMenu

        // What the items act on, copied when the menu opens: the row may go meanwhile.
        property string profileId
        property bool isSubscription
        property bool canUpdate
        property bool canAutoUpdate
        property bool updating
        property string refreshLabel
        property bool inPool
        property bool hasRules
        property string autoUpdateLabel
        property bool isProvider
        property bool canRemove
        property bool canRenew
        property bool needsSignIn
        property string providerName

        function openFor(row: ProfileRow, anchor: Item) {
            profileId = row.profileId
            isSubscription = row.isSubscription
            canUpdate = row.canUpdate
            canAutoUpdate = row.canAutoUpdate
            updating = row.updating
            refreshLabel = row.refreshLabel
            inPool = row.inPool
            hasRules = row.hasRules
            // Triggering toggles `checked` itself, so it is set here rather than bound.
            autoUpdateItem.checked = row.autoUpdate
            autoUpdateLabel = row.autoUpdateLabel
            isProvider = row.isProvider
            canRemove = row.canRemove
            canRenew = row.canRenew
            needsSignIn = row.needsSignIn
            providerName = row.providerName
            openBelow(anchor)
        }

        AppMenuItem {
            visible: !profileMenu.isProvider
            text: qsTr("编辑信息…")
            onTriggered: page.profiles.editProfile(profileMenu.profileId)
        }

        AppMenuItem {
            text: profileMenu.refreshLabel
            enabled: profileMenu.canUpdate && !profileMenu.updating
            onTriggered: page.profiles.updateProfile(profileMenu.profileId)
        }

        AppMenuItem {
            id: autoUpdateItem

            text: profileMenu.autoUpdateLabel
            checkable: true
            enabled: profileMenu.canAutoUpdate
            onTriggered: page.profiles.setAutoUpdate(profileMenu.profileId, checked)
        }

        AppMenuItem {
            visible: !profileMenu.isProvider
            text: qsTr("复制订阅链接")
            enabled: profileMenu.isSubscription
            onTriggered: page.profiles.copyLink(profileMenu.profileId)
        }

        AppMenuItem {
            text: qsTr("编辑覆写…")
            onTriggered: {
                page.overrideTrigger = profileMenu.trigger
                page.overrideFocusReason = profileMenu.triggerFocusReason
                page.profiles.editOverride(profileMenu.profileId)
            }
        }

        // A link item: it goes to the routing page (DESIGN.md "链接型菜单项").
        AppMenuItem {
            text: qsTr("使用它的规则")
            enabled: profileMenu.hasRules
            onTriggered: page.profiles.useRules(profileMenu.profileId)
        }

        AppMenuItem {
            text: profileMenu.inPool ? qsTr("不加入节点池") : qsTr("加入节点池")
            onTriggered: page.profiles.setInPool(profileMenu.profileId, !profileMenu.inPool)
        }

        // The user center in the browser, where renewing happens (D58); the menu says so in
        // words, as items have no icons (DESIGN.md "外链按钮").
        AppMenuItem {
            id: renewItem

            visible: profileMenu.isProvider && profileMenu.canRenew
            separatorBefore: true
            text: qsTr("在网页上续费")
            onTriggered: page.profiles.renew(profileMenu.profileId)
        }

        AppMenuItem {
            visible: profileMenu.isProvider && profileMenu.needsSignIn
            separatorBefore: !renewItem.visible
            text: qsTr("重新登录…")
            onTriggered: page.requestSignIn(profileMenu.trigger, profileMenu.triggerFocusReason, profileMenu.profileId)
        }

        AppMenuItem {
            id: removeItem

            visible: profileMenu.canRemove
            separatorBefore: true
            text: qsTr("删除")
            destructive: true
            onTriggered: page.profiles.requestRemove(profileMenu.profileId)
        }

        // It asks first, so it has no "…" (DESIGN.md "菜单"). It waits for the account to be
        // read, which names the site.
        AppMenuItem {
            visible: profileMenu.isProvider && profileMenu.providerName !== ""
            separatorBefore: !removeItem.visible
            text: qsTr("退出「%1」").arg(profileMenu.providerName)
            destructive: true
            onTriggered: page.profiles.requestSignOut(profileMenu.profileId)
        }
    }

    ConfirmDialog {
        id: removeDialog

        title: page.profiles.removalTitle
        message: page.profiles.removalText
        confirmText: qsTr("删除")
        destructive: true
        onAccepted: {
            page.removingIds = [profileMenu.profileId]
            page.profiles.confirmRemove()
        }
        onRejected: page.profiles.cancelRemove()
    }

    ConfirmDialog {
        id: signOutDialog

        title: page.profiles.signOutTitle
        message: page.profiles.signOutText
        confirmText: qsTr("退出登录")
        destructive: true
        onAccepted: {
            page.removingIds = page.profiles.signOutProfiles
            page.profiles.confirmSignOut()
        }
        onRejected: page.profiles.cancelSignOut()
    }

    OverrideDialog {
        id: overrideDialog

        profiles: page.profiles
    }

    ProfileDialog {
        id: profileDialog
        profiles: page.profiles
    }

    NodeLinksDialog {
        id: nodeLinksDialog
        profiles: page.profiles
    }

    FileDialog {
        id: fileDialog

        title: qsTr("导入订阅文件")
        nameFilters: [qsTr("订阅文件 (*.yaml *.yml *.json *.txt)"), qsTr("所有文件 (*)")]
        onAccepted: page.profiles.importFile(selectedFile.toString())
    }
}
