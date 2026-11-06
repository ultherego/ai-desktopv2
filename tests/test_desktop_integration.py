"""Installed launchers and the optional bar plugin expose only fixed commands."""
import configparser
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "packaging" / "desktop"


class DesktopIntegrationTests(unittest.TestCase):
    def test_freedesktop_launchers_use_fixed_argv_in_a_terminal(self):
        for name, action in (("ai-desktop", "ask"), ("ai-desktop-panel", "panel"),
                             ("ai-desktop-login-claude", "account connect claude --from-widget"),
                             ("ai-desktop-login-codex", "account connect codex --from-widget"),
                             ("ai-desktop-logout-claude", "account logout claude --from-widget"),
                             ("ai-desktop-logout-codex", "account logout codex --from-widget"),
                             ("ai-desktop-check-sign-in", "account check --from-widget"),
                             ("ai-desktop-codex-limits", "account connect-limits codex --from-widget"),
                             ("ai-desktop-codex-limits-disconnect", "account disconnect-limits codex --from-widget")):
            with self.subTest(name=name):
                entry = configparser.ConfigParser(interpolation=None)
                entry.read(DESKTOP / (name + ".desktop"), encoding="utf-8")
                self.assertTrue(entry.getboolean("Desktop Entry", "Terminal"))
                self.assertEqual(entry["Desktop Entry"]["Exec"], "/usr/bin/ai-desktop " + action)
                self.assertEqual(entry["Desktop Entry"]["Type"], "Application")

    def test_bar_uses_only_status_and_guarded_selection_commands_and_fixed_launchers(self):
        plugin = DESKTOP / "dms" / "AiDesktop"
        manifest = json.loads((plugin / "plugin.json").read_text())
        self.assertEqual(manifest["capabilities"], ["dankbar-widget"])
        self.assertNotIn("network", manifest["permissions"])
        settings = (plugin / manifest["settings"]).read_text()
        widget = (plugin / manifest["component"]).read_text()
        self.assertIn('pluginId: "' + manifest["id"] + '"', settings)
        self.assertIn('defaultValue: false', settings)
        self.assertEqual(widget.count("Proc.runCommand("), 3)
        self.assertEqual(widget.count("changeSelection("), 3)
        self.assertIn('["/usr/bin/ai-desktop"].concat(args)', widget)
        self.assertIn('if (Status.canSelectService(snapshot, provider))\n            changeSelection(["agent", "use", provider]', widget)
        self.assertIn('if (Status.canSelectAccount(snapshot, provider, account))\n            changeSelection(["account", "use", provider, account]', widget)
        self.assertIn("root.selectService(serviceCard.provider)", widget)
        self.assertIn("onClicked: root.selectAccount(serviceCard.provider, modelData.account)", widget)
        self.assertIn("onTriggered: root.refresh()", widget)
        self.assertNotIn("sudo", widget)
        self.assertIn('["/usr/bin/ai-desktop", "status", "--json"]', widget)
        self.assertIn('["/usr/bin/ai-desktop", "usage", "--refresh", "--provider", "codex", "--account", name, "--json"]', widget)
        for item in ("ai-desktop.desktop", "ai-desktop-panel.desktop",
                     "ai-desktop-login-claude.desktop", "ai-desktop-login-codex.desktop"):
            self.assertIn('launch("' + item + '")', widget)
            self.assertTrue((DESKTOP / item).is_file())
        for name in ("ai-desktop-check-sign-in", "ai-desktop-logout-claude", "ai-desktop-logout-codex",
                     "ai-desktop-codex-limits", "ai-desktop-codex-limits-disconnect"):
            self.assertIn(f'launch("{name}.desktop", [', widget)
            self.assertTrue((DESKTOP / (name + ".desktop")).is_file())
        self.assertEqual(widget.count("Qt.openUrlExternally("), 1)
        self.assertIn('onClicked: Qt.openUrlExternally("https://chatgpt.com/settings/usage")', widget)
