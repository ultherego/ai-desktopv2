"""Offline startup driver controls, with Python fixtures rather than Codex."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests import ci_codex_config as check


class CodexConfigCheckTests(unittest.TestCase):
    def fixture(self, code):
        return [sys.executable, "-I", "-B", "-c", code]

    def test_opt_in_and_ci_preflight_precede_asset_download_or_execution(self):
        with patch.object(check, "run", side_effect=AssertionError("no runtime")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check.main([]), 2)
        self.assertIn("REFUSED", output.getvalue())
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(check.asset, "prepare", side_effect=AssertionError("no asset")), \
                patch.object(check.subprocess, "Popen", side_effect=AssertionError("no process")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check.main(["--allow-disposable-codex-config-test"]), 2)
        self.assertIn("FAIL reason=preflight", output.getvalue())

    def test_fixed_command_hides_home_network_credentials_and_has_strict_flags(self):
        command = check.command(Path("/private/asset/codex"), Path("/private/fixture"))
        for flag in ("--unshare-net", "--unshare-user", "--unshare-pid", "--clearenv",
                     "--disable-userns", "--assert-userns-disabled", "--strict-config"):
            self.assertIn(flag, command)
        self.assertIn("features.view_image=false", command)
        self.assertNotIn("tools.view_image=false", command)
        self.assertIn("/home/client/.codex", command)
        self.assertNotIn("ACCESS_TOKEN", command)
        self.assertNotIn("OPENAI_API_KEY", command)
        self.assertNotIn("/var/lib/ai-desktop", command)
        self.assertNotIn("/run/egress.sock", command)
        self.assertEqual(command[command.index("--chdir") + 1], "/work")
        self.assertEqual(command.count("--ro-bind"), 4)  # /usr + private passwd/group/ELF

    def test_only_initialize_is_sent_and_stdin_closes_after_response(self):
        code = '''
import json,sys
value=json.loads(sys.stdin.readline())
assert value["id"] == 0 and value["method"] == "initialize"
assert set(value) == {"id","method","params"}
assert set(value["params"]) == {"clientInfo"}
print(json.dumps({"id":0,"result":{"userAgent":"synthetic"}}),flush=True)
assert sys.stdin.read() == ""
'''
        actual = subprocess.Popen
        processes = []
        def spawn(*args, **kwargs):
            self.assertEqual(kwargs["env"], {"PATH": "/usr/bin", "LANG": "C.UTF-8"})
            self.assertTrue(kwargs["start_new_session"])
            process = actual(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(check.subprocess, "Popen", side_effect=spawn):
            check.initialize_only(self.fixture(code), timeout=2)
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].returncode, 0)
        self.assertTrue(all(stream.closed for stream in (
            processes[0].stdin, processes[0].stdout, processes[0].stderr)))

    def notifications(self):
        return [
            {"method": "remoteControl/status/changed", "params": {
                "status": "disabled", "serverName": "synthetic", "installationId": "private-id",
                "environmentId": None}, "emittedAtMs": 123},
            {"method": "configWarning", "params": {
                "summary": "Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces.",
                "details": None}, "emittedAtMs": 124},
        ]

    def transcript(self, notifications, *, before=False):
        initialize = {"id": 0, "result": {"userAgent": "synthetic"}}
        records = notifications + [initialize] if before else [initialize, *notifications]
        text = "\n".join(json.dumps(record) for record in records) + "\n"
        return self.fixture("import sys;sys.stdin.readline();sys.stdout.write(%r);sys.stdout.flush();sys.stdin.read()" % text)

    def test_known_inactive_startup_notifications_and_timestamped_envelopes_are_valid(self):
        for notifications in (self.notifications(), list(reversed(self.notifications()))):
            with self.subTest(order=notifications[0]["method"]):
                check.initialize_only(self.transcript(notifications), timeout=2)
        legacy = [{key: value for key, value in event.items() if key != "emittedAtMs"}
                  for event in self.notifications()]
        check.initialize_only(self.transcript(legacy), timeout=2)

    def test_changed_or_duplicate_startup_events_are_denied(self):
        remote, warning = self.notifications()
        changes = []
        for value in (True, None, -1, 2**63, "123"):
            changes.append([{**remote, "emittedAtMs": value}])
        for state in ("connecting", "connected", "errored"):
            changes.append([{**remote, "params": {**remote["params"], "status": state}}])
        changes.extend((
            [{**remote, "params": {**remote["params"], "environmentId": "private-environment"}}],
            [{**remote, "params": {**remote["params"], "extra": None}}],
            [{**remote, "extra": None}],
            [remote, remote], [warning, warning],
            [{**warning, "params": {**warning["params"], "summary": "PRIVATE warning prose"}}],
            [{**warning, "params": {**warning["params"], "details": "PRIVATE details"}}],
            [{"method": "PRIVATE method", "params": {"PRIVATE-key": "PRIVATE-value"}}],
        ))
        for records in changes:
            with self.subTest(records=records), self.assertRaises(check.StartupDenied) as error:
                check.initialize_only(self.transcript(records), timeout=2)
            self.assertNotIn("PRIVATE", str(error.exception))
            self.assertNotIn("private-id", str(error.exception))
            self.assertIn("phase=after-initialize", str(error.exception))
        with self.assertRaises(check.StartupDenied) as error:
            check.initialize_only(self.transcript([remote], before=True), timeout=2)
        self.assertIn("phase=before-initialize", str(error.exception))

    def test_protocol_failure_probe_names_only_closed_shapes(self):
        records = [{"method": "configWarning", "params": {
            "summary": "PRIVATE summary", "details": "PRIVATE details", "path": "/PRIVATE/path"},
            "emittedAtMs": 123}]
        with self.assertRaises(check.StartupDenied) as error:
            check.initialize_only(self.transcript(records), timeout=2)
        message = str(error.exception)
        self.assertIn("kind=config-warning fields=3 params=3 timestamp=valid", message)
        self.assertNotIn("PRIVATE", message)
        with self.assertRaises(check.StartupDenied) as error:
            check.initialize_only(self.fixture("print('PRIVATE non-json',flush=True)"), timeout=2)
        self.assertIn("check=non-json", str(error.exception))
        self.assertNotIn("PRIVATE", str(error.exception))

    def test_rpc_errors_extra_responses_and_methods_are_refused_without_prose(self):
        cases = [
            {"id": 0, "error": {"message": "PRIVATE"}},
            {"id": True, "result": {}},
            {"id": 1, "result": {}},
            {"method": "account/login/start", "params": {}},
        ]
        for response in cases:
            code = "import sys; sys.stdin.readline(); print(%r,flush=True)" % json.dumps(response)
            with self.subTest(response=response), self.assertRaises(check.StartupDenied) as error:
                check.initialize_only(self.fixture(code), timeout=2)
            self.assertNotIn("PRIVATE", str(error.exception))
        code = 'import sys;sys.stdin.readline();print(\'{"id":0,"result":{}}\\n{"id":0,"result":{}}\',flush=True)'
        with self.assertRaises(check.StartupDenied):
            check.initialize_only(self.fixture(code), timeout=2)

    def test_timeout_and_output_overflow_fail_and_reap_the_child(self):
        actual = subprocess.Popen
        for code, reason, timeout in (("import time;time.sleep(5)", "timeout", .2),
                                      ("import sys;sys.stderr.write('PRIVATE'*30000);sys.stderr.flush()", "limit", 2)):
            processes = []
            def spawn(*args, **kwargs):
                process = actual(*args, **kwargs)
                processes.append(process)
                return process
            with self.subTest(reason=reason), patch.object(check.subprocess, "Popen", side_effect=spawn), \
                    self.assertRaises(check.StartupDenied) as error:
                check.initialize_only(self.fixture(code), timeout=timeout)
            self.assertEqual(error.exception.reason, reason)
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(all(stream.closed for stream in (
                processes[0].stdin, processes[0].stdout, processes[0].stderr)))

    def test_the_offline_turn_stage_passes_only_when_the_exchange_reaches_the_network_layer(self):
        from ai_desktop.analysis_contract import AnalysisDenied
        with patch.object(check, "exchange", side_effect=AnalysisDenied("network", diagnostic="codex-stream-disconnected")):
            check.offline_turn(["x"])                                              # failing AT the network is the expected end
        for error, message in ((AnalysisDenied("authorization", diagnostic="codex-rpc-turn"),
                                "FAIL reason=turn check=refusal=authorization codex-rpc-turn"),
                               (AnalysisDenied("result"), "FAIL reason=turn check=refusal=result codex-unclassified"),
                               (AnalysisDenied("client", diagnostic="codex-rpc-thread"), "refusal=client codex-rpc-thread")):
            with self.subTest(reason=error.reason), patch.object(check, "exchange", side_effect=error):
                with self.assertRaises(check.StartupDenied) as caught:
                    check.offline_turn(["x"])
                self.assertIn(message, str(caught.exception))
        with patch.object(check, "exchange", return_value="an answer without a network"):    # cannot happen offline
            with self.assertRaises(check.StartupDenied):
                check.offline_turn(["x"])

    def test_the_turn_command_adds_only_a_synthetic_token_and_the_startup_command_still_has_none(self):
        plain = check.command(Path("/private/asset/codex"), Path("/private/fixture"))
        turn = check.turn_command(Path("/private/asset/codex"), Path("/private/fixture"))
        self.assertNotIn("ACCESS_TOKEN", plain)
        index = turn.index("ACCESS_TOKEN")
        self.assertEqual(turn[index - 1:index + 2], ["--setenv", "ACCESS_TOKEN", check.FAKE_TOKEN])
        self.assertIn("synthetic", check.FAKE_TOKEN)
        self.assertIn("--unshare-net", turn)                                         # still no network

    def test_clean_exit_without_initialize_is_not_success(self):
        with self.assertRaises(check.StartupDenied):
            check.initialize_only(self.fixture("pass"), timeout=2)

    def test_asset_stays_in_private_temp_tree_and_no_host_install_occurs(self):
        seen = []
        def prepare(*, root):
            root = Path(root)
            seen.append(root)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertTrue((root / "packaging" / "arch").is_dir())
            return root / "packaging" / "arch" / "codex"
        with patch.object(check, "require_ci"), patch.object(check.asset, "prepare", side_effect=prepare), \
                patch.object(check, "initialize_only") as initialize, patch.object(check, "offline_turn") as turn:
            check.run()
        initialize.assert_called_once()
        turn.assert_called_once()                                                    # the stage that catches turn-time notices
        self.assertFalse(seen[0].exists())
        argv = initialize.call_args.args[0]
        self.assertNotIn("sudo", argv)
        self.assertNotIn("pacman", argv)

    def test_failure_output_is_fixed_and_never_prints_exception_details(self):
        for exception in (check.StartupDenied("initialize"), OSError("PRIVATE"),
                          check.asset.AssetDenied("download")):
            with self.subTest(exception=type(exception).__name__), \
                    patch.object(check, "run", side_effect=exception), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(check.main(["--allow-disposable-codex-config-test"]), 2)
            self.assertNotIn("PRIVATE", output.getvalue())
            self.assertEqual(len(output.getvalue().splitlines()), 1)
