"""The real activation harness must not activate anything without opt-in."""
import contextlib
import io
import unittest
from unittest.mock import patch

import host_e2_activated_worker as host


class ActivatedHarnessTests(unittest.TestCase):
    def test_no_flag_refuses_before_any_command_or_ipc(self):
        with (patch.object(host, "_command") as command,
              patch.object(host, "_installed_matches") as files,
              patch.object(host.activation, "send_once") as send,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(host.main([]), 2)
            command.assert_not_called(); files.assert_not_called(); send.assert_not_called()
        self.assertEqual(output.getvalue(),
                         "HOST ACTIVATION: REFUSED (explicit consent flag required)\n")

    def test_preflight_error_is_redacted_and_never_connects(self):
        with (patch.object(host.os, "geteuid", return_value=1000),
              patch.object(host.e2_account, "account_ready", return_value=True),
              patch.object(host, "_command", side_effect=ValueError("PRIVATE")),
              patch.object(host.activation, "send_once") as send,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(host.main(["--allow-activated-worker-test"]), 2)
            send.assert_not_called()
        self.assertEqual(output.getvalue(), "HOST ACTIVATION: REFUSED stage=preflight\n")

    def test_wrong_package_cannot_enter_shared_activation_checks(self):
        for package in ("ai-desktop 0.1.0-4", "ai-desktop 0.1.0-6", "other 0.1.0-5"):
            with (self.subTest(package=package),
                  patch.object(host.os, "geteuid", return_value=1000),
                  patch.object(host, "_command", return_value=package),
                  patch.object(host, "_run_checks") as checks,
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(host.main(["--allow-activated-worker-test"]), 2)
                checks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
