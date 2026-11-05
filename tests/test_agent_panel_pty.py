"""Panel screen ownership on a real terminal; all service and account data are synthetic."""
import os
import re
import termios
import unittest
from unittest.mock import patch

from tests import test_ask_conversation_pty as pty_harness

ENTER_SCREEN = b"\x1b[?1049h"
LEAVE_SCREEN = b"\x1b[?1049l"
HINT = "Esc cancels"
SCRIPT = """
from unittest.mock import patch
from ai_desktop import agent_panel
row = {"provider": "codex", "account": "fixture", "selected": False, "plan": "plus",
       "status": "unchecked", "usage": "cached", "observed_at": 1700000000,
       "windows": {"five_hour": {"percent": 37, "reset": 1700000300}}}
data = {"selected": "codex", "role": "fixture-role", "model": "fixture-model",
        "effort": "default", "access": "read-only", "skills": [], "policy": None,
        "providers": [], "accounts": [dict(row, account="primary", selected=True, windows={}), row],
        "sessions": [], "errors": []}
def dispatch(command):
    assert command == ["ask"], command
    print("DISPATCH ask", flush=True)
    return 0
with patch.object(agent_panel, "snapshot", return_value=data):
    raise SystemExit(agent_panel.panel(dispatch))
"""


class AgentPanelPTYTests(unittest.TestCase):
    def home(self):
        env = {"NO_COLOR": "1", "TERM": "xterm-256color", "COLUMNS": "100", "LINES": "36"}
        with patch.object(pty_harness, "SCRIPT", SCRIPT), patch.dict(os.environ, env):
            driver = pty_harness.Driver(self)
        self.addCleanup(driver.close)
        cursor = driver.wait_for(ENTER_SCREEN, timeout=3)
        cursor = driver.wait_for("Actions", cursor, timeout=3)
        return driver, driver.wait_for(HINT, cursor, timeout=3)

    def finish(self, driver):
        self.assertEqual(driver.finish(), 0, driver.text())
        self.assertEqual(termios.tcgetattr(driver.slave), driver.before)
        raw = bytes(driver.output)
        screens = re.split(rb"\x1b\[\?1049([hl])", raw)
        self.assertTrue(screens[1::4])
        self.assertTrue(all(x == b"h" for x in screens[1::4]))
        self.assertEqual(screens[3::4], [b"l"] * len(screens[1::4]))
        shell = re.sub(rb"\x1b\[[?0-9;]*[A-Za-z]", b"", b"".join(screens[0::4]))
        return b"".join(screens[2::4]), shell.strip()

    def test_quota_and_back_replace_home_then_escape_restores_shell(self):
        driver, cursor = self.home()
        self.assertNotIn(b"37%", driver.output)
        driver.send(b"\x1b[B" * 3)
        cursor = driver.wait_for("› Subscription limits", cursor, timeout=3)
        cursor = driver.wait_for(HINT, cursor)
        driver.send(b"\r")
        quota = driver.wait_for("37%", cursor, timeout=3)
        cursor = driver.wait_for("› Back", quota, timeout=3)
        cursor = driver.wait_for(HINT, cursor)
        driver.send(b"\r")
        home = driver.wait_for("Actions", cursor, timeout=3)
        driver.wait_for(HINT, home)
        driver.send(b"\x1b")
        panel, after = self.finish(driver)
        # A fresh screen must separate quota rows from both copies of the home menu.
        first, second = panel.split(b"37%")
        self.assertIn(b"\x1b[2J", first[first.rfind(b"Actions"):])
        self.assertNotIn(b"fixture-role", first.rsplit(b"\x1b[2J", 1)[-1])
        self.assertIn(b"\x1b[2J", second[:second.index(b"fixture-role")])
        self.assertEqual(after, b"")

    def test_start_dispatches_after_restoring_shell(self):
        driver, _ = self.home()
        driver.send(b"\r")
        panel, after = self.finish(driver)
        self.assertNotIn(b"DISPATCH", panel)
        self.assertEqual(after, b"DISPATCH ask")
