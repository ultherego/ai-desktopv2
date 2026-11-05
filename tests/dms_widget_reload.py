"""Run the real widget through a DMS-style package upgrade in a single Qt engine."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from dms_button_fixture import install_released_buttons


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "packaging/desktop/dms/AiDesktop"


def write_dms_modules(root):
    # Only DMS-owned types are stand-ins. The widget, cards, JS and Qt loader are real.
    modules = {
        "qs.Services": {
            "SessionService": ("singleton", """QtObject {
                property string lastLaunched: ""
                function resolveDesktopId(id) { return {id: id}; }
                function launchDesktopEntry(entry) { lastLaunched = entry.id; }
            }"""),
        },
        "qs.Common": {"Proc": ("singleton", "QtObject { function runCommand(key, args, callback) {} }"),
                      "Theme": ("singleton", """QtObject {
            property int spacingXS: 4
            property int spacingS: 8
            property int spacingM: 12
            property string fontFamily: "Sans Serif"
            property color primary: "blue"
            property color primaryContainer: "navy"
            property color onPrimaryContainer
            Component.onCompleted: onPrimaryContainer = "lightblue"
            property color surfaceContainer: "black"
            property color surfaceContainerHigh: "darkgray"
            property color surfaceText: "white"
            property color surfaceVariantText: "gray"
            property color outlineVariant: "gray"
            property color success: "green"
            property color warning: "yellow"
            property color error: "red"
        }""")},
        "qs.DCommon.Widgets": {
            "DIcon": ("", "Text { property string name; property real size }"),
            "StyledText": ("", "Text {}"),
            # Keep the basic API available on the supported DMS installations.
            # Declaring optional properties here previously hid a live load error.
            "DButton": ("", """MouseArea {
                property string iconName
                property string text
                property color backgroundColor
                property color textColor
                property real maximumWidth: Infinity
                implicitWidth: Math.min(maximumWidth, label.implicitWidth + 20)
                implicitHeight: 28
                Text { id: label; text: parent.text }
            }"""),
        },
        "qs.Modules.Plugins": {
            "PluginComponent": ("", """Item {
                property string layerNamespacePlugin
                property real popoutWidth
                property real popoutHeight
                property var pluginData: ({})
                property var parentScreen: null
                property string widgetInstanceId: "test"
                property real iconSize: 18
                property real textSize: 14
                property Component horizontalBarPill
                property Component verticalBarPill
                property Component popoutContent
            }"""),
            "PopoutComponent": ("", "Column { property string headerText; property string detailsText }"),
        },
    }
    for module, types in modules.items():
        directory = root.joinpath(*module.split("."))
        directory.mkdir(parents=True)
        entries = ["module " + module]
        for name, (modifier, body) in types.items():
            entries.append(f"{modifier} {name} 1.0 {name}.qml".strip())
            pragma = "pragma Singleton\n" if modifier else ""
            (directory / f"{name}.qml").write_text(pragma + "import QtQuick\n" + body + "\n")
        (directory / "qmldir").write_text("\n".join(entries) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pyside", action="store_true", help="use PySide's Qt Quick Test runner")
    group.add_argument("runner", nargs="?", help="path to qmltestrunner")
    parser.add_argument("--dms-common", type=Path, help="load DButton and its base from this DMS dependency checkout")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="dms-widget-reload-") as temp:
        root = Path(temp)
        installs = {}
        for state in ("missing", "cached"):
            system = root / state / "share" / "AiDesktop"
            installs[state] = system
            system.mkdir(parents=True)
            (root / state / "phase").write_text("initial")
            user = root / state / "plugins"
            user.mkdir()
            (user / "AiDesktop").symlink_to(system, target_is_directory=True)
            (system / "Status.js").write_text(".pragma library\nfunction accountWindows() { return []; }\n")
            (system / "Widget.qml").write_text(
                'import QtQuick\nimport "Status.js" as Status\n'
                'Item { property int oldCount: Status.accountWindows().length }\n'
            )
            if state == "cached":
                # The previous package used these URLs for both the card and its helper.
                current_card, = PLUGIN.rglob("QuotaCard.qml")
                shutil.copyfile(current_card, system / "QuotaCard.qml")

        shutil.copyfile(ROOT / "tests/fixtures/dms/tst_WidgetReload.qml", root / "tst_WidgetReload.qml")
        write_dms_modules(root / "modules")
        env = dict(os.environ, QML_XHR_ALLOW_FILE_READ="1", QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
        if args.dms_common:
            install_released_buttons(root / "modules", args.dms_common)
        if args.pyside:
            runner = [sys.executable, "-B", "-c",
                      'import sys; from PySide6.QtQuickTest import QUICK_TEST_MAIN; '
                      'sys.exit(QUICK_TEST_MAIN("WidgetReload", sys.argv))']
        else:
            runner = [args.runner]
        command = runner + ["-import", str(root / "modules"), "-input", str(root / "tst_WidgetReload.qml")]
        with subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT) as qt:
            for line in qt.stdout:
                print(line, end="", flush=True)
                request = re.search(r"\bINSTALL_TEST_PACKAGE (missing|cached) (old-card|upgrade)$", line)
                if request:
                    state, phase = request.groups()
                    system = installs[state]
                    if phase == "old-card":
                        (system / "Widget.qml").write_text(
                            "import QtQuick\nItem { Component { QuotaCard { account: ({}); sampledAt: 0 } } }\n"
                        )
                    else:
                        # Match a package upgrade, including removal of the old file names.
                        for name in ("QuotaCard.qml", "Status.js"):
                            if not (PLUGIN / name).exists():
                                (system / name).unlink(missing_ok=True)
                        shutil.copytree(PLUGIN, system, dirs_exist_ok=True)
                    (root / state / "phase").write_text(phase)
            if qt.wait():
                raise SystemExit(qt.returncode)


if __name__ == "__main__":
    main()
