"""Administrative Codex account lifecycle without OAuth, sudo or networking."""
from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import analysis_admin, codex_admin, codex_cli, codex_runtime
from ai_desktop.analysis_contract import AnalysisDenied
from ai_desktop.siwc_auth import AuthError


class AccountAdminTests(unittest.TestCase):
    def admin_patches(self, stack, *, native_code=0, active=False, cleanup_error=False):
        stack.enter_context(patch.object(codex_admin.os, "getuid", return_value=0))
        stack.enter_context(patch.object(codex_admin.os, "geteuid", return_value=0))
        stack.enter_context(patch.object(codex_admin.os, "isatty", return_value=True))
        ready = stack.enter_context(patch.object(codex_runtime, "runtime_ready"))
        stack.enter_context(patch.object(codex_admin, "trusted_file", side_effect=lambda path: path))
        stack.enter_context(patch.object(codex_admin.secrets, "token_hex", return_value="a" * 16))
        stack.enter_context(patch("ai_desktop.analysis_admin.login_command", return_value=[
            "/usr/bin/systemd-run", "--unit=ai-desktop-login-original", "--wait", "--collect", "--pty",
            "--service-type=exec", "--property=KillMode=control-group", "--property=User=ai-desktop",
            "/usr/bin/python3", "-I", "-B", "/usr/lib/ai-desktop/analysis-worker.py", "login"]))
        def run(command, **kwargs):
            if command[0] == "/usr/bin/systemd-run":
                self.assertEqual(ready.called, command[-1].startswith("worker-login"))
                return SimpleNamespace(returncode=native_code)
            if command[:2] == ["/usr/bin/systemctl", "stop"]:
                if cleanup_error:
                    raise subprocess.TimeoutExpired(command, 10)
                return SimpleNamespace(returncode=0)
            if command[:2] == ["/usr/bin/systemctl", "show"]:
                return SimpleNamespace(returncode=0, stdout=("LoadState=loaded\nActiveState=active\nMainPID=123\n"
                                                            if active else "LoadState=not-found\n"))
            raise AssertionError("Unexpected subprocess")
        launched = stack.enter_context(patch.object(codex_admin.subprocess, "run", side_effect=run))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return ready, launched

    def test_explicit_flag_root_tty_required_before_runtime_or_process(self):
        cases = ({"allow": False, "uid": 0, "euid": 0, "tty": True},
                 {"allow": True, "uid": 1000, "euid": 1000, "tty": True},
                 {"allow": True, "uid": 1000, "euid": 0, "tty": True},
                 {"allow": True, "uid": 0, "euid": 0, "tty": False})
        for case in cases:
            with self.subTest(case=case), ExitStack() as stack:
                ready, process = self.admin_patches(stack)
                stack.enter_context(patch.object(codex_admin.os, "getuid", return_value=case["uid"]))
                stack.enter_context(patch.object(codex_admin.os, "geteuid", return_value=case["euid"]))
                stack.enter_context(patch.object(codex_admin.os, "isatty", return_value=case["tty"]))
                self.assertEqual(codex_admin.main("login", allow=case["allow"]), 2)
                ready.assert_not_called()
                process.assert_not_called()

    def test_missing_binary_stops_before_systemd_and_credential_worker(self):
        with ExitStack() as stack:
            ready, process = self.admin_patches(stack)
            ready.side_effect = AnalysisDenied("runtime")
            self.assertEqual(codex_admin.main("login", allow=True), 2)
            process.assert_not_called()

    def test_fixed_systemd_command_no_token_or_inherited_environment(self):
        with ExitStack() as stack:
            _, process = self.admin_patches(stack)
            stack.enter_context(patch.dict("os.environ", {"ACCESS_TOKEN": "SECRET", "CODEX_ACCESS_TOKEN": "SECRET", "OPENAI_API_KEY": "SECRET"}))
            self.assertEqual(codex_admin.main("login", allow=True), 0)
            command, kwargs = process.call_args_list[0].args[0], process.call_args_list[0].kwargs
            self.assertEqual(command[-5:], ["/usr/bin/python3", "-I", "-B", "/usr/lib/ai-desktop/codex-auth.py", "worker-login"])
            self.assertIn("--unit=ai-desktop-codex-auth-" + "a" * 16, command)
            self.assertIn("--property=User=ai-desktop", command)
            self.assertIn("--property=KillMode=control-group", command)
            self.assertEqual(kwargs["env"], {"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
            self.assertNotIn("SECRET", repr(process.call_args_list))

    def test_exact_own_unit_stopped_after_success_or_client_error(self):
        for native_code in (0, 2):
            with self.subTest(native_code=native_code), ExitStack() as stack:
                _, process = self.admin_patches(stack, native_code=native_code)
                self.assertEqual(codex_admin.main("logout", allow=True), 0 if native_code == 0 else 2)
                stops = [c.args[0] for c in process.call_args_list if c.args[0][:2] == ["/usr/bin/systemctl", "stop"]]
                self.assertEqual(stops, [["/usr/bin/systemctl", "stop", "ai-desktop-codex-auth-" + "a" * 16 + ".service"]])

    def test_only_semantic_worker_exit_is_mapped_as_local_disconnect(self):
        for native_code, expected in ((codex_admin.worker_exit(5), 5), (4, 2), (5, 2), (6, 2), (203, 2)):
            with self.subTest(code=native_code), ExitStack() as stack:
                self.admin_patches(stack, native_code=native_code)
                self.assertEqual(codex_admin.main("logout", allow=True), expected)
        with ExitStack() as stack:
            self.admin_patches(stack, native_code=codex_admin.worker_exit(5), active=True)
            self.assertEqual(codex_admin.main("logout", allow=True), 2)

    def test_unverified_or_active_cleanup_cannot_report_success(self):
        for kwargs in ({"active": True}, {"cleanup_error": True}):
            with self.subTest(kwargs=kwargs), ExitStack() as stack:
                self.admin_patches(stack, **kwargs)
                self.assertEqual(codex_admin.main("login", allow=True), 2)

    def test_default_success_waits_for_cleanup_without_extra_success_output(self):
        with ExitStack() as stack:
            _, process = self.admin_patches(stack)
            output = sys.stdout
            self.assertEqual(codex_admin.main("login", allow=True), 0)
            self.assertEqual(output.getvalue(), "")
            analysis_admin.login_command.assert_called_once_with(details=False)
            self.assertEqual(process.call_args_list[-1].args[0][1], "show")

    def test_logout_still_runs_when_the_independent_native_client_was_removed(self):
        with ExitStack() as stack:
            ready, process = self.admin_patches(stack)
            ready.side_effect = AnalysisDenied("client-missing")
            self.assertEqual(codex_admin.main("logout", allow=True), 0)
            ready.assert_not_called()
            self.assertEqual(process.call_args_list[0].args[0][-1], "worker-logout")

    def test_verbose_uses_only_fixed_worker_action(self):
        with ExitStack() as stack:
            _, process = self.admin_patches(stack)
            self.assertEqual(codex_admin.main("login", allow=True, details=True), 0)
            analysis_admin.login_command.assert_called_once_with(details=True)
            self.assertEqual(process.call_args_list[0].args[0][-1], "worker-login-verbose")

    def test_invalid_details_never_starts_runtime_or_auth_worker(self):
        for details in (None, 0, 1, "true"):
            with self.subTest(details=details), ExitStack() as stack:
                ready, process = self.admin_patches(stack)
                self.assertEqual(codex_admin.main("login", allow=True, details=details), 2)
                ready.assert_not_called()
                process.assert_not_called()


class AccountCliTests(unittest.TestCase):
    def test_status_is_offline_no_auth_store_or_native_process(self):
        with patch.object(codex_runtime, "runtime_ready") as ready, \
                patch("ai_desktop.siwc_auth.AuthClient", side_effect=AssertionError("no auth")), \
                patch.object(codex_cli.subprocess, "run", side_effect=AssertionError("no process")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(codex_cli.main(["status"]), 0)
            ready.assert_called_once_with()
            self.assertIn("offline", output.getvalue())

    def test_nonroot_login_requires_explicit_flag_before_sudo(self):
        with patch.object(codex_cli.os, "getuid", return_value=1000), \
                patch.object(codex_cli.os, "geteuid", return_value=1000), \
                patch.object(codex_cli.os, "isatty", return_value=True), \
                patch.object(codex_cli.subprocess, "run") as process, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(codex_cli.main(["login"]), 2)
            process.assert_not_called()

    def test_nonroot_missing_binary_avoids_sudo_password_prompt(self):
        with patch.object(codex_cli.os, "getuid", return_value=1000), \
                patch.object(codex_cli.os, "geteuid", return_value=1000), \
                patch.object(codex_cli.os, "isatty", return_value=True), \
                patch.object(codex_runtime, "runtime_ready", side_effect=AnalysisDenied("runtime")), \
                patch.object(codex_cli.subprocess, "run") as process, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(codex_cli.main(["login", "--allow-chatgpt-login"]), 2)
            process.assert_not_called()


class AuthCallbackTests(unittest.TestCase):
    def worker(self, queries, *, terminal_failure=False, details=False):
        pending = SimpleNamespace(authorization_url="https://auth.openai.com/api/accounts/authorize?state=STATE",
                                  state="STATE", used=False)
        client = Mock()
        client.begin_login.return_value = pending
        calls = []
        def finish(_pending, query):
            calls.append(query)
            if query == "state=wrong":
                raise AuthError("state")
            pending.used = True
            if terminal_failure:
                raise AuthError("network")
            return {"id": "account", "plan_usage": True}
        client.finish_login.side_effect = finish
        class FakeServer:
            server_port = 1455
            def __init__(self, address, handler):
                self.handler, self.index = handler, 0
                self_address = address
                if self_address != ("127.0.0.1", 0):
                    raise AssertionError("listener must be loopback only")
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def handle_request(self):
                if self.index >= len(queries):
                    raise AssertionError("terminal OAuth attempt must not keep waiting or retry")
                handler = object.__new__(self.handler)
                handler.path = queries[self.index]
                self.index += 1
                handler.send_error = Mock()
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler.wfile = io.BytesIO()
                handler.do_GET()
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(codex_admin, "HOME", Path(temp)), \
                patch("ai_desktop.analysis_sandbox.HOME", Path(temp)), \
                patch("ai_desktop.analysis_sandbox.service_identity"), \
                patch.object(codex_runtime, "probe_client"), \
                patch("ai_desktop.e2_socket_activation._worker_identity", return_value=True), \
                patch.object(codex_admin.os, "isatty", return_value=True), \
                patch("ai_desktop.siwc_auth.AuthStore"), \
                patch("ai_desktop.siwc_auth.AuthClient", return_value=client), \
                patch.object(codex_admin.http.server, "HTTPServer", FakeServer), \
                redirect_stdout(io.StringIO()) as output:
            code = codex_admin.auth_worker("login", details=details)
        return code, calls, pending, output.getvalue()

    def test_stray_callback_keeps_valid_attempt_and_valid_callback_finishes(self):
        code, calls, pending, _ = self.worker(["/wrong/path?state=STATE", "/auth/callback?state=wrong", "/auth/callback?state=STATE&code=ok"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["state=wrong", "state=STATE&code=ok"])
        self.assertTrue(pending.used)

    def test_consumed_callback_failure_is_terminal_without_reexchange(self):
        code, calls, pending, _ = self.worker(["/auth/callback?state=STATE&code=ok"], terminal_failure=True)
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 1)
        self.assertTrue(pending.used)

    def test_success_summary_is_only_verbose_but_login_url_always_reaches_terminal(self):
        for details in (False, True):
            with self.subTest(details=details):
                code, _, _, text = self.worker(["/auth/callback?state=STATE&code=ok"], details=details)
                self.assertEqual(code, 0)
                self.assertIn("https://auth.openai.com/api/accounts/authorize?state=STATE", text)
                self.assertEqual("Model availability" in text, details)
                self.assertNotIn("Logged in", text)

    def test_worker_refuses_wrong_identity_without_auth_store_or_network(self):
        with patch("ai_desktop.e2_socket_activation._worker_identity", return_value=False), \
                patch("ai_desktop.siwc_auth.AuthStore") as store, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(codex_admin.auth_worker("login"), 2)
            store.assert_not_called()

    def test_incompatible_client_blocks_new_login_before_any_auth_store_or_network(self):
        from contextlib import nullcontext
        with patch("ai_desktop.e2_socket_activation._worker_identity", return_value=True), \
                patch.object(codex_admin.os, "isatty", return_value=True), \
                patch("ai_desktop.analysis_sandbox.service_identity"), \
                patch("ai_desktop.analysis_sandbox.profile_lock", return_value=nullcontext()), \
                patch.object(codex_runtime, "probe_client", side_effect=AnalysisDenied("client-protocol")) as probe, \
                patch("ai_desktop.siwc_auth.AuthStore") as store, \
                patch.object(codex_admin.http.server, "HTTPServer") as server, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(codex_admin.auth_worker("login"), 2)
        probe.assert_called_once_with()
        store.assert_not_called()
        server.assert_not_called()


class InstalledEntrypointTests(unittest.TestCase):
    def test_verbose_accepts_only_closed_action_names(self):
        source = (Path(__file__).resolve().parents[1] / "packaging/systemd/codex-auth.py").read_text()
        for argument in ("login-verbose", "logout-verbose", "worker-login-verbose", "worker-logout-verbose"):
            with self.subTest(argument=argument), ExitStack() as stack:
                admin, worker = Mock(return_value=0), Mock(return_value=0)
                fake = SimpleNamespace(main=admin, auth_worker=worker, worker_exit=codex_admin.worker_exit)
                stack.enter_context(patch.dict(sys.modules, {"ai_desktop.codex_admin": fake}))
                stack.enter_context(patch.object(sys, "argv", ["codex-auth.py", argument]))
                stack.enter_context(patch.object(sys, "path", sys.path[:]))
                with self.assertRaises(SystemExit) as result:
                    exec(compile(source, "codex-auth.py", "exec"), {"__name__": "__main__"})
                self.assertEqual(result.exception.code, 0)
                if argument.startswith("worker-"):
                    worker.assert_called_once_with(argument[7:-8], details=True)
                    admin.assert_not_called()
                else:
                    admin.assert_called_once_with(argument[:-8], allow=True, details=True)
                    worker.assert_not_called()

    def test_only_fixed_actions_are_dispatched(self):
        source = (Path(__file__).resolve().parents[1] / "packaging/systemd/codex-auth.py").read_text()
        for arguments, expected in ((["login"], ("login",)), (["logout"], ("logout",)),
                                    (["worker-login"], ("login",)), (["worker-logout"], ("logout",)),
                                    (["login", "TOKEN_SECRET"], None), (["/tmp/command"], None)):
            with self.subTest(arguments=arguments), ExitStack() as stack:
                admin, worker = Mock(return_value=0), Mock(return_value=0)
                fake = SimpleNamespace(main=admin, auth_worker=worker, worker_exit=codex_admin.worker_exit)
                stack.enter_context(patch.dict(sys.modules, {"ai_desktop.codex_admin": fake}))
                stack.enter_context(patch.object(sys, "argv", ["codex-auth.py", *arguments]))
                stack.enter_context(patch.object(sys, "path", sys.path[:]))
                with self.assertRaises(SystemExit) as result:
                    exec(compile(source, "codex-auth.py", "exec"), {"__name__": "__main__"})
                self.assertEqual(result.exception.code, 0 if expected else 2)
                if expected:
                    if arguments[0].startswith("worker-"):
                        worker.assert_called_once_with(*expected)
                        admin.assert_not_called()
                    else:
                        admin.assert_called_once_with(*expected, allow=True)
                        worker.assert_not_called()
                else:
                    worker.assert_not_called()
                    admin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
