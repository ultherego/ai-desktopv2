import QtQuick
import qs.Common
import qs.Services
import qs.DCommon.Widgets
import qs.Modules.Plugins
import "usage" as Quota
import "usage/Status.js" as Status

PluginComponent {
    id: root

    layerNamespacePlugin: "ai-desktop"
    popoutWidth: 420
    popoutHeight: 440

    property var snapshot: null
    property real sampledAt: 0
    property bool switching: false
    property bool refreshing: false
    property bool refreshQueued: false
    readonly property var activeAccount: Status.currentAccount(snapshot, service)
    readonly property var shortWindow: Status.accountWindows(activeAccount, sampledAt).find(w => w.name === "five_hour" && w.current) || null
    readonly property string service: snapshot?.selected || "Unavailable"
    readonly property bool notifyUsage: pluginData.notifyUsage === true
    readonly property int usageThreshold: pluginData.usageThreshold ?? 90

    component ChoiceButton: DButton {
        property bool highlightedChoice: false
        backgroundColor: highlightedChoice ? Theme.primaryContainer : Theme.surfaceContainerHigh
        textColor: highlightedChoice ? Theme.onPrimaryContainer : Theme.surfaceText
    }

    function refresh() {
        if (refreshing) {
            refreshQueued = true;
            return;
        }
        refreshing = true;
        const key = "aiDesktop.status." + (widgetInstanceId || "default") + "." + (parentScreen?.name || "screen");
        Proc.runCommand(key, ["/usr/bin/ai-desktop", "status", "--json"], (output, code) => {
            // Exit 2 also carries usable fields when part of the local status is unavailable.
            root.sampledAt = Math.floor(Date.now() / 1000);
            root.snapshot = (code === 0 || code === 2) ? Status.parseStatus(output) : null;
            for (const message of Status.alerts(root.snapshot, root.notifyUsage, root.usageThreshold, root.sampledAt))
                ToastService.showWarning("AI Desktop · subscription usage", message);
            root.refreshing = false;
            if (root.refreshQueued) {
                root.refreshQueued = false;
                Qt.callLater(root.refresh);
            }
        });
    }

    function changeSelection(args, key) {
        if (switching)
            return;
        switching = true;
        Proc.runCommand("aiDesktop.select." + (widgetInstanceId || "default") + "." + (parentScreen?.name || "screen") + "." + key,
                        ["/usr/bin/ai-desktop"].concat(args), (_output, code) => {
            switching = false;
            if (code !== 0)
                ToastService.showError("AI Desktop", "Could not change the selection.");
            refresh();
        });
    }

    function selectService(provider) {
        if (Status.canSelectService(snapshot, provider))
            changeSelection(["agent", "use", provider], "service");
    }

    function selectAccount(provider, account) {
        if (Status.canSelectAccount(snapshot, provider, account))
            changeSelection(["account", "use", provider, account], "account");
    }

    function accountAction(provider, name, action) {
        const selected = Status.currentAccount(snapshot, provider);
        if (!["claude", "codex"].includes(provider) || selected?.account !== name ||
            typeof name !== "string" || name !== name.trim() || !/^[a-z][a-z0-9-]{0,31}$/.test(name))
            return;
        if (action === "check" && selected.auth_state === "unknown")
            launch("ai-desktop-check-sign-in.desktop", [provider, name]);
        if (action === "logout" && selected.auth_state === "signed_in") {
            if (provider === "claude")
                launch("ai-desktop-logout-claude.desktop", [name]);
            else
                launch("ai-desktop-logout-codex.desktop", [name]);
        }
    }

    function manageAccounts(provider) {
        if (provider === "claude")
            launch("ai-desktop-login-claude.desktop");
        else if (provider === "codex")
            launch("ai-desktop-login-codex.desktop");
    }

    function launch(entryId, args) {
        const entry = SessionService.resolveDesktopId(entryId);
        if (entry)
            SessionService.launchDesktopEntry(entry, false, args || []);
        else
            ToastService.showError("AI Desktop", "The packaged terminal launcher is unavailable.");
    }

    Timer {
        interval: 60000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refresh()
    }

    horizontalBarPill: Component {
        Row {
            spacing: Theme.spacingXS
            DIcon {
                name: "terminal"
                size: root.iconSize
                color: Theme.primary
            }
            StyledText {
                anchors.verticalCenter: parent.verticalCenter
                text: Status.providerName(root.service)
                      + (root.shortWindow ? "  ·  5h " + Status.percentText(root.shortWindow.percent) : "")
                textFormat: Text.PlainText
                font.pixelSize: root.textSize
                color: Theme.surfaceText
            }
        }
    }
    verticalBarPill: Component {
        DIcon {
            name: "terminal"
            size: root.iconSize
            color: Theme.primary
        }
    }

    popoutContent: Component {
        PopoutComponent {
            id: popout

            property bool showAccounts: false
            readonly property int listLimit: Math.min(340, (root.parentScreen?.height || 800) * 0.50)

            readonly property bool opened: parentPopout?.shouldBeVisible === true
            onOpenedChanged: if (opened) root.refresh()

            headerText: "AI Desktop"
            headerActions: Component {
                DButton {
                    objectName: "statusRefresh"
                    iconName: "refresh"
                    enabled: !root.refreshing
                    minimumWidth: 36
                    horizontalPadding: 8
                    tooltipText: "Refresh local status and limits"
                    Accessible.name: "Refresh local status and limits"
                    onClicked: root.refresh()
                }
            }
            detailsText: root.snapshot ? [Status.providerName(root.service), root.snapshot.role, root.snapshot.model].filter(Boolean).join("  ·  ")
                                         + (root.snapshot.sessions.length ? "  ·  " + root.snapshot.sessions.length + " active" : "")
                                       : "Local status unavailable"

            Column {
                width: parent.width
                spacing: Theme.spacingM

                Row {
                    spacing: Theme.spacingS
                    ChoiceButton {
                        text: "Usage"
                        highlightedChoice: !popout.showAccounts
                        onClicked: popout.showAccounts = false
                    }
                    ChoiceButton {
                        text: "Accounts & login"
                        highlightedChoice: popout.showAccounts
                        onClicked: popout.showAccounts = true
                    }
                }

                ListView {
                    id: usageList
                    visible: !popout.showAccounts && count > 0
                    width: parent.width
                    height: Math.min(popout.listLimit, Math.max(0, contentHeight))
                    spacing: Theme.spacingS
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    model: root.snapshot?.accounts || []
                    delegate: Quota.QuotaCard {
                        required property var modelData
                        width: ListView.view.width
                        account: modelData
                        sampledAt: root.sampledAt
                        active: modelData.provider === root.service && modelData.selected && modelData.status !== "retired"
                        fontFamily: Theme.fontFamily
                        accentColor: Theme.primary
                        surfaceColor: Theme.surfaceContainer
                        textColor: Theme.surfaceText
                        mutedColor: Theme.surfaceVariantText
                        trackColor: Theme.outlineVariant
                        successColor: Theme.success
                        warningColor: Theme.warning
                        dangerColor: Theme.error
                        emptyUsageAction: Component {
                            ChoiceButton {
                                objectName: "chatgptUsageLink"
                                text: "View usage in ChatGPT"
                                iconName: "open_in_new"
                                tooltipText: "Opens the account signed in to your browser"
                                onClicked: Qt.openUrlExternally("https://chatgpt.com/settings/usage")
                            }
                        }
                    }
                }
                StyledText {
                    visible: !popout.showAccounts && !usageList.count
                    width: parent.width
                    text: root.snapshot ? "No subscription accounts available" : "Local status unavailable"
                    color: Theme.surfaceVariantText
                    textFormat: Text.PlainText
                }

                ListView {
                    id: serviceList
                    visible: popout.showAccounts
                    width: parent.width
                    height: Math.min(popout.listLimit, Math.max(0, contentHeight))
                    spacing: Theme.spacingS
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    model: (root.snapshot?.providers || []).filter(p => p.id === "claude" || p.id === "codex"
                                                                    || (p.id === "local" && (p.allowed || root.service === "local")))
                    delegate: Rectangle {
                        id: serviceCard
                        required property var modelData
                        readonly property string provider: modelData.id
                        readonly property var selectedAccount: Status.currentAccount(root.snapshot, provider)
                        readonly property bool signedIn: selectedAccount?.auth_state === "signed_in"
                        readonly property var accounts: (root.snapshot?.accounts || []).filter(a => a.provider === provider && a.status !== "retired")
                        width: ListView.view.width
                        height: serviceBody.implicitHeight + 24
                        radius: 13
                        color: Theme.surfaceContainer
                        border.color: chooseService.activeFocus || chooseService.containsMouse ? Theme.primary : provider === root.service ? Qt.alpha(Theme.primary, 0.52) : Qt.alpha(Theme.surfaceVariantText, 0.24)

                        MouseArea {
                            id: chooseService
                            objectName: "chooseService_" + serviceCard.provider
                            anchors.fill: parent
                            enabled: !root.switching && Status.canSelectService(root.snapshot, serviceCard.provider)
                            activeFocusOnTab: true
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            Accessible.role: Accessible.Button
                            Accessible.name: "Use " + Status.providerName(serviceCard.provider)
                            Accessible.onPressAction: root.selectService(serviceCard.provider)
                            function overButton(row, x, y) {
                                const point = mapToItem(row, x, y);
                                return row.visible && row.childAt(point.x, point.y) !== null;
                            }

                            onClicked: mouse => {
                                // Disabled account buttons also own their area on the card.
                                if (!overButton(accountChoices, mouse.x, mouse.y) && !overButton(accountActions, mouse.x, mouse.y))
                                    root.selectService(serviceCard.provider);
                            }
                            Keys.onPressed: event => {
                                if (event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                                    root.selectService(serviceCard.provider);
                                    event.accepted = true;
                                }
                            }
                        }

                        Column {
                            id: serviceBody
                            x: 14
                            y: 12
                            width: parent.width - 28
                            spacing: Theme.spacingS

                            StyledText {
                                width: parent.width
                                text: Status.providerName(serviceCard.provider) + (serviceCard.provider === root.service ? "   ·   Active service" : "")
                                font.weight: Font.DemiBold
                                color: Theme.surfaceText
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                            }
                            StyledText {
                                width: parent.width
                                text: serviceCard.provider === "local" ? serviceCard.modelData.endpoint
                                      : (serviceCard.selectedAccount ? "Account: " + serviceCard.selectedAccount.account : "No active account")
                                        + (serviceCard.modelData.allowed === false ? "   ·   Blocked by policy" : "")
                                color: Theme.surfaceVariantText
                                textFormat: Text.PlainText
                                elide: Text.ElideRight
                            }
                            Flow {
                                id: accountChoices
                                width: parent.width
                                spacing: Theme.spacingXS
                                visible: serviceCard.accounts.length > 1
                                Repeater {
                                    model: serviceCard.accounts.length > 1 ? serviceCard.accounts : []
                                    ChoiceButton {
                                        required property var modelData
                                        text: modelData.account
                                        maximumWidth: serviceBody.width
                                        highlightedChoice: modelData.selected
                                        enabled: !root.switching && !modelData.selected
                                        onClicked: root.selectAccount(serviceCard.provider, modelData.account)
                                    }
                                }
                            }
                            Flow {
                                id: accountActions
                                width: parent.width
                                spacing: Theme.spacingS
                                DButton {
                                    visible: serviceCard.provider !== "local"
                                    enabled: !root.switching && (serviceCard.signedIn || serviceCard.modelData.client_present === true)
                                    text: serviceCard.signedIn ? "Sign out…" : serviceCard.selectedAccount?.requires_new_alias ? "Add account…" : "Sign in…"
                                    iconName: serviceCard.signedIn ? "logout" : "login"
                                    onClicked: {
                                        if (serviceCard.signedIn)
                                            root.accountAction(serviceCard.provider, serviceCard.selectedAccount.account, "logout");
                                        else
                                            root.manageAccounts(serviceCard.provider);
                                    }
                                }
                                ChoiceButton {
                                    visible: serviceCard.selectedAccount?.auth_state === "unknown"
                                    enabled: !root.switching
                                    text: "Check sign-in…"
                                    onClicked: root.accountAction(serviceCard.provider, serviceCard.selectedAccount.account, "check")
                                }
                                ChoiceButton {
                                    visible: serviceCard.signedIn
                                    enabled: !root.switching
                                    text: "Manage accounts…"
                                    onClicked: root.manageAccounts(serviceCard.provider)
                                }
                            }
                        }
                    }
                }
                StyledText {
                    visible: popout.showAccounts && !serviceList.count
                    width: parent.width
                    text: "Service settings unavailable"
                    color: Theme.surfaceVariantText
                }
                Flow {
                    width: parent.width
                    spacing: Theme.spacingS
                    DButton {
                        text: "Open chat"
                        iconName: "chat"
                        onClicked: root.launch("ai-desktop.desktop")
                    }
                    ChoiceButton {
                        text: "Open panel"
                        iconName: "terminal"
                        onClicked: root.launch("ai-desktop-panel.desktop")
                    }
                }
            }
        }
    }
}
