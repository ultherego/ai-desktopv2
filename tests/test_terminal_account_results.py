"""Controller-owned login status and the lifetime of terminals opened from DankBar."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_desktop import account_cli, agent_profiles, login_flow
from ai_desktop.account_store import AccountStore


class TerminalStream(io.StringIO):
    def isatty(self):
        return True


class TerminalAccountResultsTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.home())))
        self.store = AccountStore(self.home)
        self.enterContext(patch.object(account_cli, "AccountStore", return_value=self.store))

    def test_interactive_login_outputs_colored_outcome_and_account_but_no_native_exception(self):
        with patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True):
            for outcome, label, color, expected in ((0, "Signed in", "32", 0), (7, "Sign-in failed", "31", 7),
                                                   (KeyboardInterrupt(), "Sign-in cancelled", "33", 2),
                                                   (OSError("untrusted-provider-output"), "Sign-in failed", "31", 2)):
                authenticate = (Mock(side_effect=outcome) if isinstance(outcome, BaseException)
                                else Mock(return_value=outcome))
                with self.subTest(label=label, expected=expected), redirect_stdout(TerminalStream()) as out:
                    self.assertEqual(login_flow.login("codex", authenticate, store=agent_profiles.Store(self.home)), expected)
                self.assertIn("\x1b[" + color + ";1m" + label + "\x1b[0m  Codex / default", out.getvalue())
                self.assertNotIn("untrusted-provider-output", out.getvalue())
                authenticate.assert_called_once_with()

    def test_plain_named_login_identifies_target_without_assuming_it_was_selected(self):
        for stream, env in ((TerminalStream(), {"TERM": "xterm", "NO_COLOR": ""}),
                            (TerminalStream(), {"TERM": "dumb"}), (io.StringIO(), {"TERM": "xterm"})):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), redirect_stdout(stream), \
                    patch.object(account_cli, "_login", return_value=0):
                if "work" not in self.store.load().config.items("claude"):
                    self.store.mutate(lambda state: state.add("claude", "work"))
                self.assertEqual(account_cli.main(["login", "claude", "work"]), 0)
            self.assertIn("Signed in  Claude / work", stream.getvalue())
            self.assertNotIn("\x1b", stream.getvalue())
            self.assertNotIn("Selected for new tasks", stream.getvalue())
            self.assertEqual(self.store.load().config.active("claude"), "default")

    def test_widget_keeps_login_check_and_logout_results_visible_and_preserves_return_code(self):
        with patch.object(account_cli, "os", wraps=os) as identity, \
                patch("ai_desktop.ask_commands._pick", return_value=0), \
                patch("ai_desktop.cli.main", return_value=3) as login, \
                patch.object(account_cli, "_check_or_logout", return_value=5) as account_action, \
                patch("ai_desktop.terminal_consent.fresh_input", return_value="") as wait:
            identity.getuid.return_value = identity.geteuid.return_value = 1000
            identity.isatty.return_value = True
            self.assertEqual(account_cli.main(["connect", "codex", "--from-widget"]), 3)
            login.assert_called_once_with(["login", "--provider", "codex"])
            self.assertEqual(account_cli.main(["logout", "claude", "--from-widget", "default"]), 5)
            self.assertEqual(account_cli.main(["check", "--from-widget", "codex", "default"]), 5)
            self.assertEqual(wait.call_count, 3)
            self.assertEqual(account_action.call_count, 2)
            self.assertEqual(account_cli.main(["check", "codex", "default"]), 5)
            self.assertEqual(wait.call_count, 3, "ordinary CLI must return directly to the user's shell")

    def test_closing_widget_picker_before_login_does_not_require_another_key(self):
        with patch.object(account_cli, "os", wraps=os) as identity, \
                patch("ai_desktop.ask_commands._pick", return_value=None), \
                patch("ai_desktop.terminal_consent.fresh_input", side_effect=AssertionError("unexpected pause")), \
                patch("ai_desktop.cli.main", side_effect=AssertionError("unexpected account action")):
            identity.getuid.return_value = identity.geteuid.return_value = 1000
            identity.isatty.return_value = True
            self.assertEqual(account_cli.main(["connect", "codex", "--from-widget"]), 0)
