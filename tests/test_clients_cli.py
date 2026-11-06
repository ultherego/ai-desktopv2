"""Client registration review and privileged argument boundary; no host sudo or clients."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import clients_cli as cli
from ai_desktop.analysis_contract import AnalysisDenied

OLD = "1" * 64
NEW = "2" * 64


class ClientRegistrationCliTests(unittest.TestCase):
    def setUp(self):
        for name in ("getuid", "geteuid", "getgid", "getegid"):
            self.enterContext(patch.object(cli.os, name, return_value=1000))
        self.output = self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(redirect_stderr(io.StringIO()))
        self.enterContext(patch.object(cli.sys.stdin, "isatty", return_value=True))
        self.enterContext(patch.object(self.output, "isatty", return_value=True))
        self.answer = self.enterContext(patch.object(cli, "fresh_input", return_value="yes"))
        self.metadata = self.enterContext(patch.object(cli, "registration_status", return_value={"status": "missing"}))
        self.source = SimpleNamespace(status="present", path="/home/user/.local/bin/claude", layout="standalone-native",
                                      digest=NEW, files_count=2, total_bytes=8192)
        self.discover = self.enterContext(patch.object(cli, "discover", return_value=self.source))
        self.trusted = self.enterContext(patch.object(cli, "trusted_file", return_value=Path("/usr/lib/ai-desktop/client-admin.py")))
        self.run = self.enterContext(patch.object(cli.subprocess, "run", return_value=SimpleNamespace(returncode=0)))
        self.spawn = self.enterContext(patch.object(cli.subprocess, "Popen", side_effect=AssertionError("no native execution")))

    def registered(self, *, path="/home/user/custom/claude"):
        self.metadata.return_value = {"status": "registered", "digest": OLD, "path": path, "source_uid": 1000}

    def test_register_previews_complete_digest_then_uses_fixed_isolated_root_helper(self):
        events = []
        self.answer.side_effect = lambda prompt: events.append(("review", self.output.getvalue(), prompt)) or "y"
        self.run.side_effect = lambda *args, **kwargs: events.append(("sudo", args, kwargs)) or SimpleNamespace(returncode=0)
        self.source.path = "/home/user/client; $(touch NEVER)\x1b[2J"
        self.assertEqual(cli.main(["register", "claude", "--path", self.source.path]), 0)
        self.discover.assert_called_once_with("claude", self.source.path)
        self.assertEqual([event[0] for event in events], ["review", "sudo"])
        self.assertIn(NEW, events[0][1])
        self.assertIn("files=2; bytes=8192", events[0][1])
        self.assertIn("system-wide", events[0][2])
        self.assertIn("all users", events[0][2])
        self.assertNotIn("\x1b", events[0][1])
        self.trusted.assert_called_once_with(cli.INSTALL / "client-admin.py")
        self.assertEqual(events[1][1][0], ["/usr/bin/sudo", "--", "/usr/bin/python3", "-I", "-B",
                         "/usr/lib/ai-desktop/client-admin.py", "--allow-client-registration", "register",
                         "--provider", "claude", "--source", self.source.path, "--expected-digest", NEW])
        self.assertEqual(events[1][2], {"env": {"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"},
                                      "close_fds": True, "check": False, "timeout": 180})
        self.spawn.assert_not_called()

    def test_fresh_answer_cancellation_and_nonterminal_never_invoke_privileged_helper(self):
        for answer in ("n", "no"):
            with self.subTest(answer=answer):
                self.answer.return_value = answer
                self.assertEqual(cli.main(["register", "codex"]), 2)
        for stream in (cli.sys.stdin, self.output):
            with patch.object(stream, "isatty", return_value=False):
                self.answer.reset_mock()
                self.assertEqual(cli.main(["register", "codex"]), 2)
                self.answer.assert_not_called()
        self.run.assert_not_called()
        self.trusted.assert_not_called()

    def test_invalid_answer_is_not_consent_and_requires_fresh_terminal_read(self):
        self.answer.side_effect = ["", "approve", "n"]
        self.assertEqual(cli.main(["register", "claude"]), 2)
        self.assertEqual(self.answer.call_count, 3)
        self.run.assert_not_called()

    def test_root_and_mixed_identity_fail_before_source_or_registration_read(self):
        for method, value in (("getuid", 0), ("geteuid", 0), ("getegid", 0)):
            with self.subTest(method=method), patch.object(cli.os, method, return_value=value):
                self.assertEqual(cli.main(["register", "claude"]), 2)
        self.metadata.assert_not_called()
        self.discover.assert_not_called()
        self.run.assert_not_called()

    def test_invalid_sources_and_metadata_never_reach_confirmation_or_sudo(self):
        for state in ("missing", "unsafe", "unsupported", "changed", "unavailable"):
            self.source.status = state
            self.assertEqual(cli.main(["register", "claude"]), 2)
        self.source.status = "present"
        for digest in (None, "", OLD.upper().replace("1", "A"), "../anything", "2" * 63):
            self.source.digest = digest
            self.assertEqual(cli.main(["register", "claude"]), 2)
        self.source.digest = NEW
        self.source.path = "relative"
        self.assertEqual(cli.main(["register", "claude"]), 2)
        for state in ("unsafe", "unavailable"):
            self.metadata.return_value = {"status": state}
            self.assertEqual(cli.main(["register", "claude"]), 2)
        self.answer.assert_not_called()
        self.run.assert_not_called()

    def test_register_existing_and_refresh_missing_refuse_without_source_read(self):
        self.registered()
        self.assertEqual(cli.main(["register", "claude"]), 2)
        self.metadata.return_value = {"status": "missing"}
        self.assertEqual(cli.main(["refresh", "claude"]), 2)
        self.discover.assert_not_called()
        self.answer.assert_not_called()
        self.run.assert_not_called()

    def test_legacy_codex_layout_explains_independent_update_without_running_installer(self):
        self.source.status, self.source.detail = "unsupported", "legacy-layout"
        self.assertEqual(cli.main(["register", "codex"]), 2)
        self.assertIn("Update the independently installed Codex with its original installer", self.output.getvalue())
        self.assertNotIn("select --path", self.output.getvalue())
        self.answer.assert_not_called()
        self.run.assert_not_called()

    def test_refresh_uses_original_source_and_binds_both_old_and_new_payloads(self):
        self.registered()
        self.assertEqual(cli.main(["refresh", "claude"]), 0)
        self.discover.assert_called_once_with("claude", "/home/user/custom/claude")
        self.assertIn("Replacing registered digest: " + OLD, self.output.getvalue())
        command = self.run.call_args.args[0]
        self.assertEqual(command[7:], ["refresh", "--provider", "claude", "--source", self.source.path,
                                      "--expected-digest", NEW, "--previous-digest", OLD])
        self.discover.reset_mock()
        self.assertEqual(cli.main(["refresh", "claude", "--path", "/custom/new-client"]), 0)
        self.discover.assert_called_once_with("claude", "/custom/new-client")

    def test_remove_binds_existing_digest_and_never_reads_or_executes_source(self):
        self.registered()
        self.assertEqual(cli.main(["remove", "codex"]), 0)
        self.assertEqual(self.run.call_args.args[0][7:], ["remove", "--provider", "codex", "--expected-digest", OLD])
        self.assertIn("login data remain", self.output.getvalue())
        self.discover.assert_not_called()
        self.run.reset_mock()
        self.answer.reset_mock()
        self.metadata.return_value = {"status": "missing"}
        self.assertEqual(cli.main(["remove", "codex"]), 0)
        self.run.assert_not_called()
        self.answer.assert_not_called()

    def test_corrupt_registered_digest_refuses_before_refresh_or_remove_review(self):
        self.registered()
        self.metadata.return_value["digest"] = "bad"
        for action in ("refresh", "remove"):
            self.assertEqual(cli.main([action, "claude"]), 2)
        self.answer.assert_not_called()
        self.discover.assert_not_called()
        self.run.assert_not_called()

    def test_untrusted_admin_helper_is_refused_and_private_exception_details_are_hidden(self):
        self.trusted.side_effect = AnalysisDenied("runtime")
        self.assertEqual(cli.main(["register", "claude"]), 2)
        self.run.assert_not_called()
        self.trusted.side_effect = OSError("PRIVATE helper path")
        self.assertEqual(cli.main(["register", "claude"]), 2)
        self.assertNotIn("PRIVATE", self.output.getvalue())
        self.run.assert_not_called()

    def test_helper_failure_timeout_and_cancel_do_not_retry(self):
        self.run.return_value = SimpleNamespace(returncode=1)
        self.assertEqual(cli.main(["register", "claude"]), 2)
        self.run.assert_called_once()
        self.run.reset_mock()
        self.run.side_effect = subprocess.TimeoutExpired("PRIVATE", 180)
        self.assertEqual(cli.main(["register", "claude"]), 2)
        self.run.assert_called_once()
        self.run.reset_mock()
        self.answer.side_effect = KeyboardInterrupt()
        self.assertEqual(cli.main(["register", "claude"]), 2)
        self.run.assert_not_called()
        self.assertNotIn("PRIVATE", self.output.getvalue())

    def test_list_uses_registered_custom_source_for_refresh_and_never_executes(self):
        self.registered()
        with patch("ai_desktop.system_clients.inspect", return_value=SimpleNamespace(status="present")):
            self.assertEqual(cli.main(["list"]), 0)
        self.assertEqual([call.args for call in self.discover.call_args_list],
                         [("claude", "/home/user/custom/claude"), ("codex", "/home/user/custom/claude")])
        self.assertIn("clients refresh claude", self.output.getvalue())
        self.assertIn("Metadata only", self.output.getvalue())
        self.run.assert_not_called()
        self.spawn.assert_not_called()
        self.answer.assert_not_called()

    def test_list_never_compares_foreign_registered_source_to_callers_unrelated_default(self):
        self.registered(path=None)
        with patch("ai_desktop.system_clients.inspect", return_value=SimpleNamespace(status="present")):
            self.assertEqual(cli.main(["list"]), 0)
        self.assertNotIn("clients refresh", self.output.getvalue())
        self.assertNotIn("Registered source:", self.output.getvalue())
        self.run.assert_not_called()

    def test_public_dispatch_and_help_do_not_probe_native_clients(self):
        from ai_desktop.cli import main
        with patch.object(cli, "main", return_value=7) as delegate:
            self.assertEqual(main(["clients", "register", "codex", "--path", "/my/client"]), 7)
            delegate.assert_called_once_with(["register", "codex", "--path", "/my/client"])
        with self.assertRaises(SystemExit) as stopped:
            cli.main(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        self.discover.assert_not_called()
        self.metadata.assert_not_called()
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
