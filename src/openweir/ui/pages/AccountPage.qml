pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import OpenWeir
import "../components"

// The 账户 page (DESIGN.md "账户页", docs/accounts.md §7.4): one provider account, read-only but
// for 自动续费. What buys or changes the account is an external-link button: it opens the
// provider's user center in the browser, and the account syncs as the window comes back.
ScrollView {
    id: page

    required property AccountViewModel account
    // What asked to sign out last (the more menu's trigger, or 退出登录 in 账户与安全), which
    // gets the focus back when the confirmation closes.
    property Item signOutTrigger
    property int signOutFocusReason: Qt.OtherFocusReason

    // The last account signed out under the page, which goes (Main.qml): a confirmation still
    // closing gives its focus to `item` rather than to a control of this page, with its ring
    // if it would have shown one. False when none is closing.
    function handFocus(item: Item): bool {
        if (!signOutDialog.visible)
            return false
        signOutDialog.returnFocusTo = item
        return true
    }

    function toneColor(tone: string): color {
        return tone === "danger" ? Theme.danger : tone === "warning" ? Theme.warning : Theme.textMuted
    }

    function requestSignOut(trigger: Item, reason: int) {
        signOutTrigger = trigger
        signOutFocusReason = reason
        page.account.requestSignOut()
    }

    contentWidth: availableWidth
    clip: true

    ScrollToFocus {
        flickable: page.contentItem as Flickable
    }

    Connections {
        target: page.account

        function onSignOutRequested() {
            signOutDialog.showFor(page.signOutTrigger, page.signOutFocusReason)
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
            title: qsTr("账户")
            detail: page.account.status
            detailColor: page.toneColor(page.account.statusTone)

            // Several providers signed in to: which one the page shows.
            AppSelect {
                Layout.preferredWidth: 180
                visible: page.account.multiple
                options: page.account.providerOptions
                current: page.account.providerId
                accessibleName: qsTr("服务商")
                onPicked: value => page.account.selectProvider(value)
            }

            IconButton {
                id: moreButton

                iconName: "more"
                accessibleName: qsTr("账户的更多操作")
                onClicked: moreMenu.openBelow(moreButton)
            }
        }

        GridLayout {
            id: grid

            readonly property real columnWidth: (width - (columns - 1) * columnSpacing) / columns

            Layout.fillWidth: true
            Layout.leftMargin: Theme.pageX
            Layout.rightMargin: Theme.pageX
            Layout.bottomMargin: Theme.pageBottom
            columns: width >= 2 * Theme.groupColumnMin + Theme.space6 ? 2 : 1
            columnSpacing: Theme.space6
            rowSpacing: Theme.space5

            ColumnLayout {
                Layout.preferredWidth: grid.columnWidth
                Layout.alignment: Qt.AlignTop
                spacing: Theme.space5

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.space2

                    SectionLabel {
                        Layout.fillWidth: true
                        text: qsTr("当前订阅")
                    }

                    Group {
                        Layout.fillWidth: true

                        GroupRow {
                            visible: !page.account.loaded
                            divider: false

                            AccessibleText {
                                Layout.fillWidth: true
                                text: qsTr("正在读取…")
                                role: Theme.type.bodySm
                                color: Theme.textMuted
                            }
                        }

                        // Signed out: the last synced data stays, and the way back in comes
                        // first (DESIGN.md "账户页").
                        GroupRow {
                            visible: page.account.needsSignIn
                            divider: false

                            AccessibleText {
                                Layout.fillWidth: true
                                text: page.account.signInText
                                role: Theme.type.bodySm
                                color: page.account.signInDanger ? Theme.danger : Theme.warning
                                wrapMode: Text.Wrap
                            }

                            AppButton {
                                text: qsTr("重新登录…")
                                variant: "compact"
                                onClicked: page.account.signInAgain()
                            }
                        }

                        GroupRow {
                            visible: page.account.loaded && !page.account.hasSubscriptions
                            divider: page.account.needsSignIn

                            AccessibleText {
                                Layout.fillWidth: true
                                text: qsTr("这个账户还没有订阅")
                                role: Theme.type.bodySm
                                color: Theme.textMuted
                                wrapMode: Text.Wrap
                            }

                            ExternalButton {
                                text: qsTr("选购套餐")
                                variant: "compact"
                                onClicked: page.account.openPlans()
                            }
                        }

                        // The plan and how it stands.
                        GroupRow {
                            visible: page.account.hasSubscriptions
                            divider: page.account.needsSignIn
                            verticalPadding: Theme.space2

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0

                                AccessibleText {
                                    Layout.fillWidth: true
                                    text: page.account.currentName
                                    role: Theme.type.bodyMdStrong
                                    elide: Text.ElideRight
                                }

                                AccessibleText {
                                    Layout.fillWidth: true
                                    text: page.account.currentStatus
                                    role: Theme.type.caption
                                    color: page.toneColor(page.account.currentTone)
                                    elide: Text.ElideRight
                                }
                            }
                        }

                        GroupRow {
                            visible: page.account.hasSubscriptions
                            rowHeight: 0
                            verticalPadding: Theme.space3

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 6

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: Theme.space3

                                    UiText {
                                        Layout.fillWidth: true
                                        text: qsTr("本期流量")
                                    }

                                    AccessibleText {
                                        text: page.account.currentUsage
                                        role: Theme.type.dataMd
                                        color: Theme.textSecondary
                                        Accessible.name: qsTr("%1：%2").arg(qsTr("本期流量")).arg(text)
                                    }
                                }

                                UsageBar {
                                    Layout.fillWidth: true
                                    visible: page.account.currentHasQuota
                                    fraction: page.account.currentUsedFraction
                                }
                            }
                        }

                        ValueRow {
                            visible: page.account.hasSubscriptions
                            label: qsTr("下次重置")
                            value: page.account.currentReset
                        }

                        ValueRow {
                            visible: page.account.hasSubscriptions
                            label: qsTr("到期")
                            value: page.account.currentExpiry
                        }

                        ValueRow {
                            visible: page.account.hasSubscriptions
                            label: qsTr("设备")
                            value: page.account.currentDevices
                        }

                        // The one thing the app changes in billing (D58): it takes effect at
                        // once, and goes back with the reason when the provider refuses.
                        GroupRow {
                            visible: page.account.hasSubscriptions
                            verticalPadding: Theme.space2

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0

                                UiText {
                                    Layout.fillWidth: true
                                    text: qsTr("自动续费")
                                }

                                AccessibleText {
                                    Layout.fillWidth: true
                                    visible: text !== ""
                                    text: page.account.autoRenewError
                                    role: Theme.type.caption
                                    color: Theme.danger
                                    wrapMode: Text.Wrap
                                }
                            }

                            AppSwitch {
                                value: page.account.autoRenew
                                enabled: page.account.canSetAutoRenew
                                Accessible.name: qsTr("自动续费")
                                Accessible.description: page.account.autoRenewError
                                onRequested: on => page.account.setAutoRenew(on)
                            }
                        }

                        GroupRow {
                            visible: page.account.hasSubscriptions
                            rowHeight: Theme.rowSingle

                            ExternalButton {
                                text: qsTr("续费")
                                variant: "compact"
                                onClicked: page.account.renew()
                            }

                            ExternalButton {
                                Layout.leftMargin: -Theme.space1
                                text: qsTr("更换套餐")
                                variant: "compact"
                                onClicked: page.account.openPlans()
                            }

                            Item {
                                Layout.fillWidth: true
                            }
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    visible: page.account.subscriptionCount > 0
                    spacing: Theme.space2

                    SectionLabel {
                        Layout.fillWidth: true
                        text: qsTr("此账户的订阅（%1）").arg(page.account.subscriptionCount)
                    }

                    Group {
                        Layout.fillWidth: true

                        Repeater {
                            model: page.account.subscriptions

                            GroupRow {
                                id: subscriptionRow

                                required property int index
                                required property int remoteId
                                required property string name
                                required property string status
                                required property string tone
                                required property string expiry
                                required property bool inUse
                                required property string action

                                divider: index > 0
                                verticalPadding: Theme.space2

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 0

                                    AccessibleText {
                                        Layout.fillWidth: true
                                        text: subscriptionRow.name
                                        elide: Text.ElideRight
                                    }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 0

                                        AccessibleText {
                                            text: subscriptionRow.status
                                            role: Theme.type.caption
                                            color: page.toneColor(subscriptionRow.tone)
                                        }

                                        AccessibleText {
                                            Layout.fillWidth: true
                                            text: qsTr(" · %1").arg(subscriptionRow.expiry)
                                            role: Theme.type.caption
                                            color: Theme.textMuted
                                            elide: Text.ElideRight
                                        }
                                    }
                                }

                                AccessibleText {
                                    visible: subscriptionRow.inUse
                                    text: qsTr("使用中")
                                    role: Theme.type.bodySm
                                    color: Theme.textMuted
                                }

                                AppButton {
                                    visible: subscriptionRow.action === "use"
                                    enabled: !page.account.busy
                                    text: qsTr("使用")
                                    variant: "compact"
                                    Accessible.name: qsTr("使用 %1").arg(subscriptionRow.name)
                                    onClicked: page.account.use(subscriptionRow.remoteId)
                                }

                                // It cannot be used until it is renewed, as in the sign-in
                                // dialog's list.
                                ExternalButton {
                                    visible: subscriptionRow.action === "renew"
                                    text: qsTr("续费")
                                    accessibleName: qsTr("续费 %1").arg(subscriptionRow.name)
                                    variant: "compact"
                                    onClicked: page.account.renewSubscription(subscriptionRow.remoteId)
                                }
                            }
                        }
                    }
                }
            }

            ColumnLayout {
                Layout.preferredWidth: grid.columnWidth
                Layout.alignment: Qt.AlignTop
                spacing: Theme.space5

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.space2

                    SectionLabel {
                        Layout.fillWidth: true
                        text: qsTr("钱包")
                    }

                    Group {
                        Layout.fillWidth: true

                        GroupRow {
                            visible: page.account.walletNote !== ""
                            divider: false

                            AccessibleText {
                                Layout.fillWidth: true
                                text: page.account.walletNote
                                role: page.account.walletNoteDanger ? Theme.type.caption : Theme.type.bodySm
                                color: page.account.walletNoteDanger ? Theme.danger : Theme.textMuted
                                wrapMode: Text.Wrap
                            }
                        }

                        ValueRow {
                            visible: page.account.walletNote === ""
                            divider: false
                            label: qsTr("余额")
                            value: page.account.walletBalance
                            mono: true
                        }

                        ValueRow {
                            visible: page.account.walletNote === ""
                            label: qsTr("积分")
                            value: page.account.walletPoints
                            mono: true
                        }

                        ValueRow {
                            visible: page.account.walletNote === ""
                            label: qsTr("等级")
                            value: page.account.walletLevel
                        }

                        GroupRow {
                            rowHeight: Theme.rowSingle

                            ExternalButton {
                                text: qsTr("充值")
                                variant: "compact"
                                onClicked: page.account.openWallet()
                            }

                            ExternalButton {
                                Layout.leftMargin: -Theme.space1
                                text: qsTr("明细")
                                accessibleName: qsTr("钱包明细")
                                variant: "compact"
                                onClicked: page.account.openWallet()
                            }

                            Item {
                                Layout.fillWidth: true
                            }
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.space2

                    SectionLabel {
                        Layout.fillWidth: true
                        text: qsTr("套餐")
                    }

                    Group {
                        Layout.fillWidth: true

                        GroupRow {
                            visible: page.account.plansNote !== ""
                            divider: false

                            AccessibleText {
                                Layout.fillWidth: true
                                text: page.account.plansNote
                                role: page.account.plansNoteDanger ? Theme.type.caption : Theme.type.bodySm
                                color: page.account.plansNoteDanger ? Theme.danger : Theme.textMuted
                                wrapMode: Text.Wrap
                            }
                        }

                        Repeater {
                            model: page.account.plans

                            GroupRow {
                                id: planRow

                                required property int index
                                required property string planId
                                required property string name
                                required property string detail
                                required property var prices
                                required property string priceText
                                required property bool soldOut

                                divider: index > 0
                                verticalPadding: Theme.space2

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 0

                                    AccessibleText {
                                        Layout.fillWidth: true
                                        text: planRow.name
                                        elide: Text.ElideRight
                                    }

                                    // The prices in monospace, their periods in the body font
                                    // (DESIGN.md "金额"); a narrow column wraps them.
                                    Flow {
                                        Layout.fillWidth: true
                                        Accessible.role: Accessible.StaticText
                                        Accessible.name: planRow.priceText !== "" ? qsTr("%1 · %2").arg(planRow.detail).arg(planRow.priceText) : planRow.detail

                                        UiText {
                                            text: planRow.detail
                                            role: Theme.type.caption
                                            color: Theme.textMuted
                                        }

                                        Repeater {
                                            model: planRow.prices

                                            Row {
                                                id: price

                                                required property var modelData

                                                UiText {
                                                    text: qsTr(" · ")
                                                    role: Theme.type.caption
                                                    color: Theme.textMuted
                                                }

                                                UiText {
                                                    anchors.baseline: per.baseline
                                                    text: price.modelData.amount
                                                    role: Theme.type.dataSm
                                                    color: Theme.textSecondary
                                                }

                                                UiText {
                                                    id: per

                                                    text: price.modelData.per
                                                    role: Theme.type.caption
                                                    color: Theme.textMuted
                                                }
                                            }
                                        }
                                    }
                                }

                                AccessibleText {
                                    visible: planRow.soldOut
                                    text: qsTr("已售罄")
                                    role: Theme.type.bodySm
                                    color: Theme.textMuted
                                }

                                ExternalButton {
                                    visible: !planRow.soldOut
                                    text: qsTr("选购")
                                    accessibleName: qsTr("选购 %1").arg(planRow.name)
                                    variant: "compact"
                                    onClicked: page.account.buyPlan(planRow.planId)
                                }
                            }
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.space2

                    SectionLabel {
                        Layout.fillWidth: true
                        text: qsTr("订单")
                    }

                    Group {
                        Layout.fillWidth: true

                        GroupRow {
                            visible: page.account.ordersNote !== ""
                            divider: false

                            AccessibleText {
                                Layout.fillWidth: true
                                text: page.account.ordersNote
                                role: page.account.ordersNoteDanger ? Theme.type.caption : Theme.type.bodySm
                                color: page.account.ordersNoteDanger ? Theme.danger : Theme.textMuted
                                wrapMode: Text.Wrap
                            }
                        }

                        Repeater {
                            model: page.account.orders

                            GroupRow {
                                id: orderRow

                                required property int index
                                required property string orderId
                                required property string title
                                required property string detail
                                required property string amount
                                required property string status
                                required property bool pending
                                required property bool waiting

                                divider: index > 0
                                verticalPadding: Theme.space2

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 0

                                    AccessibleText {
                                        Layout.fillWidth: true
                                        text: orderRow.title
                                        elide: Text.ElideRight
                                    }

                                    AccessibleText {
                                        Layout.fillWidth: true
                                        text: orderRow.detail
                                        role: Theme.type.caption
                                        color: Theme.textMuted
                                        elide: Text.ElideRight
                                    }
                                }

                                ColumnLayout {
                                    spacing: 0

                                    AccessibleText {
                                        Layout.alignment: Qt.AlignRight
                                        text: orderRow.amount
                                        role: Theme.type.dataMd
                                        color: Theme.textSecondary
                                    }

                                    AccessibleText {
                                        Layout.alignment: Qt.AlignRight
                                        text: orderRow.status
                                        role: Theme.type.caption
                                        color: orderRow.pending || orderRow.waiting ? Theme.warning : Theme.textMuted
                                    }
                                }

                                ExternalButton {
                                    id: payButton

                                    visible: orderRow.pending
                                    text: qsTr("去支付")
                                    accessibleName: qsTr("去支付 %1").arg(orderRow.title)
                                    variant: "compact"
                                    onClicked: page.account.openOrders()
                                }

                                // The room of 去支付 on the rows without it, so that the amounts
                                // line up; the hidden button still knows its width.
                                Item {
                                    visible: !orderRow.pending && page.account.ordersPending
                                    Layout.preferredWidth: payButton.implicitWidth
                                }
                            }
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.space2

                    SectionLabel {
                        Layout.fillWidth: true
                        text: qsTr("账户与安全")
                    }

                    Group {
                        Layout.fillWidth: true

                        GroupRow {
                            divider: false
                            rowHeight: Theme.rowSingle

                            ExternalButton {
                                text: qsTr("密码与两步验证")
                                variant: "compact"
                                onClicked: page.account.openSecurity()
                            }

                            ExternalButton {
                                Layout.leftMargin: -Theme.space1
                                text: qsTr("登录设备")
                                variant: "compact"
                                onClicked: page.account.openDevices()
                            }

                            Item {
                                Layout.fillWidth: true
                            }
                        }

                        // A single maintenance action, so a plain compact button rather than a
                        // more menu; it asks first (DESIGN.md "账户页").
                        GroupRow {
                            verticalPadding: Theme.space2

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0

                                UiText {
                                    Layout.fillWidth: true
                                    text: qsTr("退出登录")
                                }

                                AccessibleText {
                                    Layout.fillWidth: true
                                    text: qsTr("这个账户的订阅会从本机移除")
                                    role: Theme.type.caption
                                    color: Theme.textMuted
                                    wrapMode: Text.Wrap
                                }
                            }

                            AppButton {
                                id: signOutButton

                                enabled: !page.account.busy
                                text: qsTr("退出登录")
                                variant: "compact"
                                onClicked: page.requestSignOut(signOutButton, Focus.reasonAfter(signOutButton))
                            }
                        }
                    }
                }
            }
        }
    }

    AppMenu {
        id: moreMenu

        AppMenuItem {
            text: qsTr("刷新")
            enabled: !page.account.busy
            onTriggered: page.account.refresh()
        }

        // The menu says in words that it opens the browser, as items have no icons
        // (DESIGN.md "外链按钮").
        AppMenuItem {
            text: qsTr("在用户中心打开")
            onTriggered: page.account.openUserCenter()
        }

        // It asks first, so it has no "…" (DESIGN.md "菜单").
        AppMenuItem {
            text: qsTr("退出登录")
            separatorBefore: true
            destructive: true
            enabled: !page.account.busy
            onTriggered: page.requestSignOut(moreMenu.trigger, moreMenu.triggerFocusReason)
        }
    }

    ConfirmDialog {
        id: signOutDialog

        title: page.account.signOutTitle
        message: page.account.signOutText
        confirmText: qsTr("退出登录")
        destructive: true
        onAccepted: page.account.confirmSignOut()
        onRejected: page.account.cancelSignOut()
    }

    // A label and its value, such as 到期 and its date; money and counts in monospace
    // (DESIGN.md "金额").
    component ValueRow: GroupRow {
        id: valueRow

        property string label
        property string value
        property bool mono

        rowHeight: Theme.rowSingle

        UiText {
            Layout.fillWidth: true
            text: valueRow.label
        }

        AccessibleText {
            text: valueRow.value
            role: valueRow.mono ? Theme.type.dataMd : Theme.type.bodySm
            color: Theme.textSecondary
            Accessible.name: qsTr("%1：%2").arg(valueRow.label).arg(valueRow.value)
        }
    }
}
