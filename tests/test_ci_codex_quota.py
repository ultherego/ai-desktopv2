"""Disposable quota fixtures refuse existing registered clients before writes."""
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

import ci_codex_quota as fixture


class CodexQuotaFixtureGuardTests(unittest.TestCase):
    def test_existing_registration_or_dangling_link_refuses_before_any_mutation(self):
        registered = "/var/lib/ai-desktop-clients/codex"
        for present_flag in ("-e", "-L"):
            calls, terminal = [], Mock()
            def checked(*argv):
                calls.append(argv)
                if argv[-2:] == (present_flag, registered):
                    raise subprocess.CalledProcessError(1, argv)
            with self.subTest(present_flag=present_flag), \
                    patch.dict(fixture.os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux"}), \
                    patch.object(fixture.os, "getuid", return_value=1000), \
                    patch.object(fixture.os, "geteuid", return_value=1000), \
                    patch.object(fixture.Path, "read_text", return_value="systemd\n"), \
                    patch.object(fixture.prior, "_run", side_effect=checked), \
                    self.assertRaises(subprocess.CalledProcessError):
                fixture.run_cycle(Path("/tmp/synthetic-quota"), "unused", terminal)
            self.assertEqual(calls[-1], ("sudo", "-n", "/usr/bin/test", "!", present_flag, registered))
            self.assertTrue(all(command[:3] == ("sudo", "-n", "/usr/bin/test") for command in calls))
            terminal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
