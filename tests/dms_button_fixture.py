"""Use the release DButton and its actual Qt template, with synthetic theme and effects."""
from pathlib import Path
import shutil


def install_released_buttons(modules: Path, checkout: Path):
    widgets = modules / "qs/DCommon/Widgets"
    for name in ("DButton.qml", "StyledButton.qml"):
        shutil.copyfile(checkout / "DCommon/Widgets" / name, widgets / name)

    common = modules / "qs/DCommon/Common"
    common.mkdir(parents=True, exist_ok=True)
    decorations = {
        "FocusRing": "Rectangle {}",
        "DSpinner": "Item { property int size; property int strokeWidth; property color color; property bool running }",
        "StateLayer": """Item {
            property Item control
            property bool disabled
            property color stateColor
            property bool enableRipple
            property int transitionDuration
            property var transitionCurve
            property var tooltipText
            property string tooltipSide
        }""",
    }
    for name, body in decorations.items():
        (widgets / f"{name}.qml").write_text("import QtQuick\n" + body + "\n")
    with (widgets / "qmldir").open("a") as file:
        for name in ("StyledButton", *decorations):
            file.write(f"{name} 1.0 {name}.qml\n")

    (common / "qmldir").write_text(
        "module qs.DCommon.Common\nsingleton Style 1.0 Style.qml\nsingleton I18n 1.0 I18n.qml\nDAnim 1.0 DAnim.qml\n"
    )
    (common / "DAnim.qml").write_text("import QtQuick\nNumberAnimation {}\n")
    (common / "I18n.qml").write_text("pragma Singleton\nimport QtQuick\nQtObject { property bool isRtl: false }\n")
    (common / "Style.qml").write_text("""pragma Singleton
import QtQuick
QtObject {
    enum AnimationSpeed { None, Normal }
    function buttonRadius(width, height, buttonHeight, pressed, round) { return height / 2; }
    property var expressiveCurves: ({standard: [0,0,1,1,1,1], expressiveEffects: [0,0,1,1,1,1], expressiveFastSpatial: [0,0,1,1,1,1]})
    property var expressiveDurations: ({expressiveEffects: 0, expressiveFastSpatial: 0})
    property bool enableRippleEffects: false
    property bool reduceMotion: true
    property int currentAnimationSpeed: 0
    property int buttonHeightM: 44
    property int buttonHeightS: 36
    property int buttonHeightXS: 28
    property int buttonMinWidth: 64
    property int cornerRadiusM: 8
    property int focusRingOffset: 2
    property int fontSizeLarge: 16
    property int fontSizeMedium: 14
    property int fontWeightMedium: Font.Medium
    property int iconSize: 20
    property int iconSizeMedium: 16
    property int outlineWidthFocused: 2
    property real pressScale: 1
    property int spacingXL: 16
    property int spacingL: 12
    property int spacingM: 8
    property int spacingS: 6
    property int spacingXS: 4
    property color buttonBg: "white"
    property color buttonText: "black"
    property color focusRingColor: "blue"
    readonly property color onSurface_10: Qt.rgba(0, 0, 0, 0.10)
    readonly property color onSurface_12: Qt.rgba(0, 0, 0, 0.12)
    readonly property color onSurface_38: Qt.rgba(0, 0, 0, 0.38)
}
""")
