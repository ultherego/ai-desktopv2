import QtQuick
import "Status.js" as Status

Rectangle {
    id: card

    required property var account
    required property real sampledAt
    property bool active: false
    property string fontFamily: ""
    property color accentColor: "#fab387"
    property color surfaceColor: "#252536"
    property color textColor: "#cdd6f4"
    property color mutedColor: "#a6adc8"
    property color trackColor: "#45475a"
    property color successColor: "#a6e3a1"
    property color warningColor: "#f9e2af"
    property color dangerColor: "#f38ba8"
    readonly property var readings: Status.accountWindows(account, sampledAt)
    readonly property bool hasUsage: readings.length > 0
    readonly property int inset: 14

    implicitHeight: body.implicitHeight + inset * 2
    height: implicitHeight
    radius: 13
    color: surfaceColor
    border.width: 1
    border.color: active ? Qt.alpha(accentColor, 0.52) : Qt.alpha(mutedColor, 0.24)

    component Caption: Text {
        color: card.mutedColor
        font.family: card.fontFamily
        font.pixelSize: 12
        textFormat: Text.PlainText
        elide: Text.ElideRight
    }

    Column {
        id: body
        x: card.inset
        y: card.inset
        width: card.width - 2 * card.inset
        spacing: 9

        Item {
            width: parent.width
            height: Math.max(accountTitle.implicitHeight, planBadge.height)

            Rectangle {
                id: activeDot
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                width: 7
                height: 7
                radius: 4
                color: card.active ? card.accentColor : card.trackColor
            }
            Text {
                id: accountTitle
                objectName: "quotaAccountTitle"
                anchors.left: activeDot.right
                anchors.leftMargin: 8
                anchors.right: planBadge.left
                anchors.rightMargin: 6
                anchors.verticalCenter: parent.verticalCenter
                text: Status.providerName(card.account.provider) + "  /  " + card.account.account
                font.family: card.fontFamily
                font.pixelSize: 16
                font.weight: Font.DemiBold
                color: card.textColor
                textFormat: Text.PlainText
                elide: Text.ElideRight
            }
            Rectangle {
                id: planBadge
                visible: !!card.account.plan
                width: visible ? Math.min(parent.width * 0.32, planLabel.implicitWidth + 14) : 0
                height: 22
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                radius: height / 2
                color: Qt.alpha(card.accentColor, 0.14)
                Caption {
                    id: planLabel
                    anchors.centerIn: parent
                    width: Math.min(implicitWidth, Math.max(0, parent.width - 12))
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                    text: String(card.account.plan || "").replace(/_/g, " ").toUpperCase()
                    color: card.accentColor
                }
            }
        }

        Repeater {
            model: card.hasUsage ? ["five_hour", "seven_day"] : []
            delegate: Column {
                id: windowRow
                required property string modelData
                readonly property var sample: card.readings.find(r => r.name === modelData) || null
                readonly property bool current: !!sample && sample.current
                readonly property color tone: !current ? card.mutedColor : sample.percent >= 90
                        ? card.dangerColor : sample.percent >= 75 ? card.warningColor : card.successColor
                width: body.width
                spacing: 4

                Item {
                    width: parent.width
                    height: Math.max(windowName.implicitHeight, percent.implicitHeight)
                    Caption {
                        id: windowName
                        anchors.left: parent.left
                        anchors.right: percent.left
                        anchors.verticalCenter: parent.verticalCenter
                        text: windowRow.modelData === "five_hour" ? "5-hour usage" : "7-day usage"
                        color: card.textColor
                        font.weight: Font.Medium
                        font.pixelSize: 13
                    }
                    Caption {
                        id: percent
                        anchors.right: parent.right
                        text: windowRow.sample ? Status.percentText(windowRow.sample.percent) : "Not reported"
                        font.pixelSize: windowRow.sample ? 15 : 12
                        font.weight: Font.DemiBold
                        color: windowRow.tone
                    }
                }
                Rectangle {
                    id: track
                    objectName: "quotaTrack_" + windowRow.modelData
                    visible: windowRow.current
                    width: parent.width
                    height: 6
                    radius: 3
                    color: card.trackColor
                    Rectangle {
                        objectName: "quotaFill_" + windowRow.modelData
                        visible: windowRow.current
                        width: windowRow.current ? track.width * windowRow.sample.percent / 100 : 0
                        height: track.height
                        radius: Math.min(width / 2, height / 2)
                        color: windowRow.tone
                    }
                }
                Caption {
                    visible: !!windowRow.sample
                    width: parent.width
                    wrapMode: Text.Wrap
                    elide: Text.ElideNone
                    text: !windowRow.sample ? "" : (windowRow.current ? "Resets in " +
                          Status.remaining(windowRow.sample.reset - card.sampledAt) : "Last recorded · expired")
                          + "  ·  " + Qt.formatDateTime(new Date(windowRow.sample.reset * 1000), "d MMM, hh:mm t")
                }
            }
        }
        Caption {
            objectName: "emptyUsage"
            width: parent.width
            visible: !card.hasUsage
            text: card.account.status === "retired" ? "Account retired" : "Usage not reported"
        }
        Caption {
            width: parent.width
            visible: card.hasUsage
            text: "Updated " + (card.hasUsage ? Qt.formatDateTime(new Date(card.account.observed_at * 1000), "d MMM, hh:mm t") : "")
            font.pixelSize: 11
        }
    }
}
