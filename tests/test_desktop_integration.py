"""Installed launchers and the optional bar plugin expose only fixed commands."""
import configparser
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "packaging" / "desktop"


class DesktopIntegrationTests(unittest.TestCase):
    def test_both_freedesktop_launchers_require_a_terminal(self):
        for name, action in (("ai-desktop", "ask"), ("ai-desktop-panel", "panel")):
            with self.subTest(name=name):
                entry = configparser.ConfigParser(interpolation=None)
                entry.read(DESKTOP / (name + ".desktop"), encoding="utf-8")
                self.assertTrue(entry.getboolean("Desktop Entry", "Terminal"))
                self.assertEqual(entry["Desktop Entry"]["Exec"], "/usr/bin/ai-desktop " + action)
                self.assertEqual(entry["Desktop Entry"]["Type"], "Application")

    def test_bar_plugin_queries_only_local_status_and_launches_packaged_desktop_ids(self):
        plugin = DESKTOP / "dms" / "AiDesktop"
        manifest = json.loads((plugin / "plugin.json").read_text())
        self.assertEqual(manifest["capabilities"], ["dankbar-widget"])
        self.assertNotIn("network", manifest["permissions"])
        settings = (plugin / manifest["settings"]).read_text()
        widget = (plugin / manifest["component"]).read_text()
        self.assertIn('pluginId: "' + manifest["id"] + '"', settings)
        self.assertIn('defaultValue: false', settings)
        self.assertEqual(widget.count("Proc.runCommand("), 1)
        self.assertIn('["/usr/bin/ai-desktop", "status", "--json"]', widget)
        for item in ("ai-desktop.desktop", "ai-desktop-panel.desktop"):
            self.assertIn('root.launch("' + item + '")', widget)
            self.assertTrue((DESKTOP / item).is_file())
