import contextlib
import io
import unittest
from unittest.mock import patch

import host_assistant_acceptance as host


class HostAssistantGuards(unittest.TestCase):
    def test_flag_root_and_tty_refuse_before_any_host_or_model(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(host.prior, "preflight") as ready, patch.object(host.prior, "run") as run:
            self.assertEqual(host.main([]), 2)
            for uid, tty in ((0, True), (1000, False)):
                with patch.object(host.os, "geteuid", return_value=uid), patch.object(host.os, "isatty", return_value=tty):
                    self.assertEqual(host.main(["--allow-subscription-assistant-test"]), 2)
            ready.assert_not_called(); run.assert_not_called()

    def test_cleanup_failure_or_command_error_cannot_report_success(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(host.os, "geteuid", return_value=1000), \
                patch.object(host.os, "isatty", return_value=True), patch.object(host.prior, "preflight", return_value=(1000, ["socket", "service"])), \
                patch.object(host.prior, "reject_incomplete"), patch.object(host.prior, "run", side_effect=ValueError("PRIVATE")) as run, \
                patch.object(host.prior, "check_stopped", side_effect=ValueError("PRIVATE")):
            self.assertEqual(host.main(["--allow-subscription-assistant-test"]), 2)
        self.assertNotIn("HOST ASSISTANT: PASS", output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())
        run.assert_called_once_with("/usr/bin/ai-desktop", "analyze", "--question",
            "Ktore obserwacje wymagaja dalszego sprawdzenia? Podaj kroki i nie wykonuj zmian.", timeout=420)
