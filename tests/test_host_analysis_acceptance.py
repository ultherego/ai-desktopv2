"""Host operations are opt-in and never captured or run by ordinary tests."""
import contextlib
import io
import unittest
from unittest.mock import patch

import host_analysis_acceptance as host


class HostAnalysisGuardTests(unittest.TestCase):
    def test_no_flag_root_or_redirected_terminal_refuses_before_commands(self):
        with patch.object(host, "preflight") as preflight, patch.object(host, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(host.main([]), 2)
            self.assertEqual(host.main(["--allow-claude-login"]), 2)
            for uid, tty in ((0, True), (1000, False)):
                with patch.object(host.os, "geteuid", return_value=uid), patch.object(host.os, "isatty", return_value=tty):
                    self.assertEqual(host.main(["--allow-subscription-analysis-test"]), 2)
            preflight.assert_not_called()
            run.assert_not_called()

    def test_failure_verifies_application_cleanup_without_masking_it(self):
        units = ["ai-desktop-analysis@1000.socket", "ai-desktop-analysis@1000.service"]
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            if args == ("/usr/bin/ai-desktop", "analyze", "--details"):
                raise ValueError("PRIVATE-NATIVE-ERROR")
        out = io.StringIO()
        with patch.object(host.os, "geteuid", return_value=1000), patch.object(host.os, "isatty", return_value=True), \
                patch.object(host, "preflight", return_value=(1000, units)), patch.object(host, "run", side_effect=run), \
                patch.object(host, "reject_incomplete"), patch.object(host, "check_stopped") as stopped, \
                contextlib.redirect_stdout(out):
            self.assertEqual(host.main(["--allow-subscription-analysis-test"]), 2)
        self.assertEqual(stopped.call_count, 2)
        self.assertFalse(any("/usr/bin/systemctl" in call for call in calls))
        self.assertNotIn("PRIVATE", out.getvalue())
        self.assertFalse(any("login" in call for call in calls))

    def test_cleanup_failure_prevents_success_even_after_model_returns(self):
        out = io.StringIO()
        with patch.object(host.os, "geteuid", return_value=1000), patch.object(host.os, "isatty", return_value=True), \
                patch.object(host, "preflight", return_value=(1000, ["socket", "service"])), patch.object(host, "run"), \
                patch.object(host, "reject_incomplete"), patch.object(host, "check_stopped", side_effect=[None, ValueError("PRIVATE")]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(host.main(["--allow-subscription-analysis-test"]), 2)
        self.assertIn("FAIL stage=cleanup", out.getvalue())
        self.assertNotIn("HOST ANALYSIS: PASS", out.getvalue())
        self.assertNotIn("PRIVATE", out.getvalue())
