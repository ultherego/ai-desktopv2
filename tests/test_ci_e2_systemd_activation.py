"""The privileged CI driver must be unreachable in ordinary host tests."""
import contextlib
import io
import subprocess
import unittest
from unittest.mock import patch

import ci_e2_systemd_activation as ci


class SystemdCIDriverTests(unittest.TestCase):
    def test_no_flag_never_inspects_or_changes_host(self):
        with (patch.object(ci, "_require_ci") as preflight,
              patch.object(ci, "_run") as command,
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(ci.main([]), 2)
            preflight.assert_not_called()
            command.assert_not_called()

    def test_non_ci_and_root_refuse_before_commands(self):
        for env, uid in (({}, 1000), ({"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux"}, 0)):
            with (self.subTest(uid=uid), patch.dict(ci.os.environ, env, clear=True),
                  patch.object(ci.os, "geteuid", return_value=uid),
                  patch.object(ci, "_run") as command,
                  patch.object(ci.host, "_run_checks") as activate,
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(ci.main(["--allow-disposable-systemd-test"]), 2)
                command.assert_not_called()
                activate.assert_not_called()

    def test_failure_stops_only_the_tested_instance_and_stays_failed(self):
        for start_fails in (False, True):
            calls = []

            def command(*args):
                calls.append(args)
                if start_fails and "start" in args:
                    raise subprocess.CalledProcessError(1, args)

            with (self.subTest(start_fails=start_fails),
                  patch.object(ci, "_require_ci", return_value=1000),
                  patch.object(ci, "_install"), patch.object(ci, "_run", side_effect=command),
                  patch.object(ci.host, "_run_checks", return_value=2),
                  patch.object(ci, "_stopped", return_value=True),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(ci.main(["--allow-disposable-systemd-test"]), 2)
            stops = [call for call in calls if "stop" in call]
            self.assertEqual(stops, [("sudo", "-n", "/usr/bin/systemctl", "stop",
                                     "ai-desktop-handoff@1000.socket", "ai-desktop-handoff@1000.service")])

    def test_cleanup_failure_cannot_report_success(self):
        with (patch.object(ci, "_require_ci", return_value=1000),
              patch.object(ci, "_install"), patch.object(ci, "_run"),
              patch.object(ci.host, "_run_checks", return_value=0),
              patch.object(ci, "_stopped", return_value=False),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(ci.main(["--allow-disposable-systemd-test"]), 2)
        self.assertNotIn("CI SYSTEMD: PASS", output.getvalue())


if __name__ == "__main__":
    unittest.main()
