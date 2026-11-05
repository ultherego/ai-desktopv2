import QtQuick
import qs.Common
import qs.Services
import qs.DCommon.Widgets
import qs.Modules.Plugins
import "Status.js" as Status

PluginComponent {
    id: root

    layerNamespacePlugin: "ai-desktop"
    popoutWidth: 410
    popoutHeight: 370

    property var snapshot: null
    property real sampledAt: 0
    readonly property string service: snapshot?.selected || "Unavailable"
    readonly property bool notifyUsage: pluginData.notifyUsage === true
    readonly property int usageThreshold: pluginData.usageThreshold ?? 90

    function refresh() {
        const key = "aiDesktop.status." + (widgetInstanceId || "default") + "." + (parentScreen?.name || "screen");
        Proc.runCommand(key, ["/usr/bin/ai-desktop", "status", "--json"], (output, code) => {
            // Exit 2 also carries usable fields when part of the local status is unavailable.
            root.sampledAt = Math.floor(Date.now() / 1000);
            root.snapshot = (code === 0 || code === 2) ? Status.parseStatus(output) : null;
            for (const message of Status.alerts(root.snapshot, root.notifyUsage, root.usageThreshold, root.sampledAt))
                ToastService.showWarning("AI Desktop · subscription usage", message);
        });
    }

    function launch(entryId) {
        const entry = SessionService.resolveDesktopId(entryId);
        if (entry)
            SessionService.launchDesktopEntry(entry);
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
                text: root.service
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

            headerText: "AI Desktop"
            detailsText: root.snapshot ? root.service + " · " + (root.snapshot.role || "Unknown role") + " · "
                                         + (root.snapshot.model || "Unknown model") + " · "
                                         + root.snapshot.sessions.length + " active" : "Local status unavailable"

            ListView {
                width: parent.width
                height: root.popoutHeight - popout.headerHeight - popout.detailsHeight - 84
                clip: true
                spacing: Theme.spacingM
                model: root.snapshot?.accounts || []
                delegate: Column {
                    id: accountDelegate
                    required property var modelData
                    width: ListView.view.width
                    readonly property var readings: Status.accountWindows(modelData, root.sampledAt)
                    spacing: Theme.spacingXS
                    StyledText {
                        textFormat: Text.PlainText
                        text: accountDelegate.modelData.provider + " / " + accountDelegate.modelData.account + (accountDelegate.modelData.selected ? " · selected" : "")
                        color: Theme.surfaceText
                    }
                    StyledText {
                        visible: Boolean(accountDelegate.modelData.plan || accountDelegate.modelData.status)
                        textFormat: Text.PlainText
                        text: [accountDelegate.modelData.plan, accountDelegate.modelData.status].filter(Boolean).join(" · ")
                        color: Theme.surfaceVariantText
                    }
                    Repeater {
                        model: parent.readings
                        StyledText {
                            required property var modelData
                            textFormat: Text.PlainText
                            text: modelData.label + " · " + modelData.percent + "% used · "
                                  + (modelData.current ? "resets " : "expired ")
                                  + Qt.formatDateTime(new Date(modelData.reset * 1000), "yyyy-MM-dd hh:mm")
                            color: Theme.surfaceVariantText
                        }
                    }
                    StyledText {
                        visible: parent.readings.length > 0
                        text: "Observed " + Qt.formatDateTime(new Date(accountDelegate.modelData.observed_at * 1000), "yyyy-MM-dd hh:mm")
                        color: Theme.surfaceVariantText
                    }
                    StyledText {
                        visible: parent.readings.length === 0
                        text: "Usage not reported"
                        color: Theme.surfaceVariantText
                    }
                }
                Text {
                    visible: parent.count === 0
                    text: "No account usage available"
                    color: Theme.surfaceVariantText
                }
            }
            Row {
                spacing: Theme.spacingM
                DButton {
                    text: "Open chat"
                    onClicked: root.launch("ai-desktop.desktop")
                }
                DButton {
                    text: "Open panel"
                    onClicked: root.launch("ai-desktop-panel.desktop")
                }
            }
        }
    }
}
