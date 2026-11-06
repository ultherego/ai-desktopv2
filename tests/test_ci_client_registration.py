"""Privileged synthetic registration is unreachable outside its explicit CI fixture."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import ci_client_registration as driver


class ClientRegistrationCiGuardTests(unittest.TestCase):
    def test_missing_flag_and_non_ci_run_do_not_execute_any_command(self):
        with patch.object(driver, "checked") as execute, patch.dict(driver.os.environ, {}, clear=True), \
                redirect_stdout(io.StringIO()) as output:
            for arguments in ([], ["--allow-install"], [driver.FLAG, "extra"], [driver.FLAG]):
                self.assertEqual(driver.main(arguments), 2)
            execute.assert_not_called()
        self.assertNotIn("Traceback", output.getvalue())

    def test_root_mixed_ids_and_existing_fixture_refuse_before_any_command(self):
        with patch.dict(driver.os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux"}), \
                patch.object(driver.os, "getuid", return_value=1001), patch.object(driver.os, "geteuid", return_value=1001), \
                patch.object(driver.os, "getgid", return_value=1001), patch.object(driver.os, "getegid", return_value=1001), \
                patch.object(driver, "ubuntu"), patch.object(driver, "checked") as execute, \
                patch.object(driver.os.path, "lexists", return_value=True), redirect_stdout(io.StringIO()):
            self.assertEqual(driver.main([driver.FLAG]), 2)
            for identity in ("getuid", "geteuid", "getegid"):
                with patch.object(driver.os, identity, return_value=0):
                    self.assertEqual(driver.main([driver.FLAG]), 2)
            execute.assert_not_called()

    def test_root_action_refuses_in_checkout_even_as_root_before_source_or_cache_access(self):
        with patch.object(driver.os, "getuid", return_value=0), patch.object(driver.os, "geteuid", return_value=0), \
                patch.object(driver, "ubuntu") as platform, patch.object(driver, "checked") as execute, \
                patch.object(driver, "_trusted") as trusted, redirect_stdout(io.StringIO()):
            self.assertEqual(driver.main(["--root-fixture", "register", "a" * 64]), 2)
        platform.assert_not_called()
        trusted.assert_not_called()
        execute.assert_not_called()

    def test_root_action_rejects_paths_commands_or_invalid_digest_before_context(self):
        cases = ([], ["shell", "a" * 64], ["register", "/tmp/source"], ["remove", "a" * 64, "b" * 64],
                 ["refresh", "a" * 64], ["refresh", "a" * 64, "invalid"], ["register", "a" * 64, "/bin/sh"])
        with patch.object(driver, "root_context") as context:
            for arguments in cases:
                self.assertEqual(driver.root_action(arguments), 2)
            context.assert_not_called()

    def test_privileged_command_is_fixed_and_contains_only_action_and_reviewed_hashes(self):
        with patch.object(driver, "checked", return_value=b'{"provider":"codex","status":"registered","digest":"' + b"a" * 64 + b'"}\n') as run:
            driver.privileged("refresh", "a" * 64, "b" * 64)
        run.assert_called_once_with(["/usr/bin/sudo", "-n", "--", "/usr/bin/python3", "-I", "-B", str(driver.HELPER),
                                     "--root-fixture", "refresh", "a" * 64, "b" * 64], expected=0)


if __name__ == "__main__":
    unittest.main()
