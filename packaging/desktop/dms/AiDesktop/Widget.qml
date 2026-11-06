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
    popoutHeight: Math.min(540, (parentScreen?.height || 800) * 0.8)

    property var snapshot: null
    property real sampledAt: 0
    property bool switching: false
    property bool refreshing: false
    property bool refreshQueued: false
    property string fetchingLimitsFor: ""
    property string liveErrorAccount: ""
    property string liveErrorText: ""
    readonly property var codexAccount: Status.currentAccount(snapshot, "codex")
    readonly property var codexProvider: snapshot?.providers?.find(p => p.id === "codex")
    readonly property bool codexAvailable: codexProvider?.allowed === true && codexProvider?.client_present === true
    readonly property var activeAccount: Status.currentAccount(snapshot, service)
    readonly property bool activeNativeQuota: service === "codex" && activeAccount?.quota_source === "codex_native"
    readonly property var shortWindow: Status.accountWindows(activeAccount, sampledAt).find(w => w.name === "five_hour" && w.current) || null
    readonly property string service: snapshot?.selected || "Unavailable"
    readonly property bool notifyUsage: pluginData.notifyUsage === true
    readonly property int usageThreshold: pluginData.usageThreshold ?? 90

    component ChoiceButton: DButton {
        property bool highlightedChoice: false
        backgroundColor: highlightedChoice ? Theme.primaryContainer : Theme.surfaceContainerHigh
        textColor: highlightedChoice ? Theme.onPrimaryContainer : Theme.surfaceText
    }

    component ServiceHitArea: MouseArea {
        required property string provider
        property list<Item> buttonRows
        signal choose()
        anchors.fill: parent
        activeFocusOnTab: true
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        Accessible.role: Accessible.Button
        Accessible.name: "Use " + Status.providerName(provider)
        Accessible.onPressAction: if (enabled) choose()

        onClicked: mouse => {
            // Disabled buttons keep their hit area too. Only the card itself switches services.
            for (let index = 0; index < buttonRows.length; ++index) {
                const row = buttonRows[index];
                const point = mapToItem(row, mouse.x, mouse.y);
                if (row.visible && row.childAt(point.x, point.y) !== null)
                    return;
            }
            choose();
        }
        Keys.onPressed: event => {
            if (event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                choose();
                event.accepted = true;
            }
        }
    }

    function refresh(liveTarget) {
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
                ToastService.showWarning(root.activeNativeQuota ? "AI Desktop · native Codex limits" : "AI Desktop · subscription usage", message);
            root.refreshing = false;
            if (typeof liveTarget === "string" && root.codexAccount?.account === liveTarget &&
                root.codexAccount.live_limits_connected === true) {
                if (root.codexAvailable) {
                    root.fetchLiveLimits(liveTarget);
                } else {
                    root.liveErrorAccount = liveTarget;
                    root.liveErrorText = root.codexProvider?.allowed === true ? "The Codex client is unavailable." :
                                         "Live limits are blocked by policy.";
                }
            }
            if (root.refreshQueued) {
                root.refreshQueued = false;
                Qt.callLater(root.refresh);
            }
        });
    }

    function validAlias(name) {
        return typeof name === "string" && name === name.trim() && /^[a-z][a-z0-9-]{0,31}$/.test(name);
    }

    function fetchLiveLimits(name) {
        if (!validAlias(name) || fetchingLimitsFor || switching || !codexAvailable ||
            codexAccount?.account !== name || codexAccount.live_limits_connected !== true)
            return;
        fetchingLimitsFor = name;
        liveErrorAccount = "";
        const key = "aiDesktop.limits." + (widgetInstanceId || "default") + "." + (parentScreen?.name || "screen");
        Proc.runCommand(key, ["/usr/bin/ai-desktop", "usage", "--refresh", "--provider", "codex", "--account", name, "--json"],
                        (output, code) => {
            root.fetchingLimitsFor = "";
            if (code !== 0) {
                let reason = "";
                try {
                    if (typeof output === "string" && output.length < 1024)
                        reason = JSON.parse(output).error;
                } catch (_) {}
                const messages = {
                    "auth-required": "Reconnect live limits for this account.",
                    "not-connected": "Connect live limits for this account.",
                    "identity": "Reconnect with the intended Codex account.",
                    "policy": "Live limits are blocked by policy.",
                    "check-sign-in": "Check sign-in in Accounts & login, then try again.",
                    "busy": "Another account action is running. Try again."
                };
                root.liveErrorText = typeof reason === "string" && Object.prototype.hasOwnProperty.call(messages, reason) ?
                                     messages[reason] : "Could not refresh live limits. Try again.";
                root.liveErrorAccount = name;
            }
            // Only the CLI stores the result for this exact slot. Always read back the current selection.
            Qt.callLater(root.refresh);
        }, 0, 90000, root);
    }

    function manageLiveLimits(name, disconnect) {
        if (!validAlias(name) || switching || fetchingLimitsFor || codexAccount?.account !== name)
            return;
        const connected = codexAccount.live_limits_connected === true;
        if (disconnect && (connected || codexAccount.native_limits_cleanup_pending === true)) {
            liveErrorAccount = "";
            launch("ai-desktop-codex-limits-disconnect.desktop", [name]);
        } else if (!disconnect && !connected && codexAccount.auth_state === "signed_in" && codexAvailable) {
            liveErrorAccount = "";
            launch("ai-desktop-codex-limits.desktop", [name]);
        }
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
            !validAlias(name))
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
                      + (root.shortWindow ? (root.activeNativeQuota ? "  ·  CLI 5h " : "  ·  5h ") + Status.percentText(root.shortWindow.percent) : "")
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
            readonly property real availableHeight: parentPopout?.maxBodyHeight > 0
                ? parentPopout.maxBodyHeight : (root.parentScreen?.height || 800) * 0.8
            readonly property real controlsHeight: headerHeight + detailsHeight + 2 * PopoutMetrics.contentPadding
                + tabs.height + footer.height + 2 * content.spacing
                + (liveError.visible ? liveError.height + content.spacing : 0)
            readonly property real listLimit: Math.max(0, Math.min(432, availableHeight - controlsHeight))

            readonly property bool opened: parentPopout?.shouldBeVisible === true
            onOpenedChanged: if (opened) root.refresh()

            headerText: "AI Desktop"
            headerActions: Component {
                DButton {
                    objectName: "statusRefresh"
                    iconName: "refresh"
                    enabled: !root.refreshing && !root.switching && !root.fetchingLimitsFor
                    busy: !!root.fetchingLimitsFor
                    minimumWidth: 36
                    horizontalPadding: 8
                    tooltipText: "Refresh local status and linked Codex limits"
                    Accessible.name: tooltipText
                    onClicked: root.refresh(root.codexAccount?.account)
                }
            }
            detailsText: root.snapshot ? [Status.providerName(root.service), root.snapshot.role, root.snapshot.model].filter(Boolean).join("  ·  ")
                                         + (root.snapshot.sessions.length ? "  ·  " + root.snapshot.sessions.length + " active" : "")
                                       : "Local status unavailable"

            Column {
                id: content
                width: parent.width
                spacing: Theme.spacingM

                Row {
                    id: tabs
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

                StyledText {
                    id: liveError
                    objectName: "liveLimitsError"
                    width: parent.width
                    visible: !!root.liveErrorAccount && root.liveErrorAccount === root.codexAccount?.account
                    text: visible ? "Codex / " + root.liveErrorAccount + " · " + root.liveErrorText : ""
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: Theme.error
                }

                ListView {
                    id: usageList
                    objectName: "usageList"
                    visible: !popout.showAccounts && count > 0
                    width: parent.width
                    height: Math.min(popout.listLimit, Math.max(0, contentHeight))
                    spacing: Theme.spacingS
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    model: root.snapshot?.accounts || []
                    delegate: Quota.QuotaCard {
                        id: usageCard
                        required property var modelData
                        readonly property bool showUsageLink: account.provider === "codex" && !hasUsage && account.status !== "retired"
                        readonly property bool showConnect: account.provider === "codex" && account.selected === true &&
                            account.auth_state === "signed_in" && !account.live_limits_connected &&
                            account.status !== "retired" && root.codexAvailable
                        objectName: "usageCard_" + account.provider + "_" + account.account
                        width: ListView.view.width
                        height: implicitHeight + (actions.visible ? actions.implicitHeight + Theme.spacingS : 0)
                        account: modelData
                        sampledAt: root.sampledAt
                        active: account.provider === root.service && account.selected === true && account.status !== "retired"
                        fontFamily: Theme.fontFamily
                        accentColor: Theme.primary
                        surfaceColor: Theme.surfaceContainer
                        textColor: Theme.surfaceText
                        mutedColor: Theme.surfaceVariantText
                        trackColor: Theme.outlineVariant
                        successColor: Theme.success
                        warningColor: Theme.warning
                        dangerColor: Theme.error
                        border.color: chooseUsage.activeFocus || chooseUsage.containsMouse ? accentColor
                                      : active ? Qt.alpha(accentColor, 0.52) : Qt.alpha(mutedColor, 0.24)

                        ServiceHitArea {
                            id: chooseUsage
                            objectName: "selectUsage_" + provider + "_" + usageCard.account.account
                            provider: usageCard.account.provider
                            enabled: usageCard.account.status !== "retired" && !root.switching &&
                                     Status.canSelectService(root.snapshot, provider)
                            buttonRows: [actions]
                            onChoose: root.selectService(provider)
                        }
                        Flow {
                            id: actions
                            x: usageCard.inset
                            anchors.bottom: parent.bottom
                            anchors.bottomMargin: usageCard.inset
                            width: parent.width - 2 * usageCard.inset
                            visible: usageCard.showUsageLink || usageCard.showConnect
                            spacing: Theme.spacingS

                            ChoiceButton {
                                id: usageLink
                                objectName: "chatgptUsageLink"
                                visible: usageCard.showUsageLink
                                maximumWidth: parent.width
                                text: "View usage in ChatGPT"
                                iconName: "open_in_new"
                                tooltipText: "Opens the account signed in to your browser"
                                onClicked: Qt.openUrlExternally("https://chatgpt.com/settings/usage")
                            }
                            ChoiceButton {
                                id: connectLimits
                                visible: usageCard.showConnect
                                enabled: !root.switching && !root.fetchingLimitsFor
                                maximumWidth: parent.width
                                text: usageCard.account.native_limits_cleanup_pending === true ? "Reconnect live limits…" : "Connect live limits…"
                                onClicked: root.manageLiveLimits(usageCard.account.account, false)
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
                        readonly property bool linked: selectedAccount?.live_limits_connected === true
                        readonly property bool cleanupPending: selectedAccount?.native_limits_cleanup_pending === true
                        readonly property var accounts: (root.snapshot?.accounts || []).filter(a => a.provider === provider && a.status !== "retired")
                        width: ListView.view.width
                        height: serviceBody.implicitHeight + 24
                        radius: 13
                        color: Theme.surfaceContainer
                        border.color: chooseService.activeFocus || chooseService.containsMouse ? Theme.primary : provider === root.service ? Qt.alpha(Theme.primary, 0.52) : Qt.alpha(Theme.surfaceVariantText, 0.24)

                        ServiceHitArea {
                            id: chooseService
                            objectName: "chooseService_" + provider
                            provider: serviceCard.provider
                            enabled: !root.switching && Status.canSelectService(root.snapshot, provider)
                            buttonRows: [accountChoices, accountActions]
                            onChoose: root.selectService(serviceCard.provider)
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
                                    visible: serviceCard.provider === "codex" && !serviceCard.linked && serviceCard.signedIn && root.codexAvailable
                                    enabled: !root.switching && !root.fetchingLimitsFor
                                    text: serviceCard.cleanupPending ? "Reconnect live limits…" : "Connect live limits…"
                                    tooltipText: "Link a native Codex account for reading limits"
                                    onClicked: root.manageLiveLimits(serviceCard.selectedAccount.account, false)
                                }
                                ChoiceButton {
                                    visible: serviceCard.provider === "codex" && (serviceCard.linked || serviceCard.cleanupPending)
                                    enabled: !root.switching && !root.fetchingLimitsFor
                                    text: "Disconnect live limits…"
                                    tooltipText: "Remove the native Codex connection for this slot"
                                    onClicked: root.manageLiveLimits(serviceCard.selectedAccount.account, true)
                                }
                                ChoiceButton {
                                    visible: serviceCard.provider === "claude" || serviceCard.provider === "codex"
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
                    id: footer
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
