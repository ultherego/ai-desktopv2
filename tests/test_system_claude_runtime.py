"""System-client lifecycle ordering using synthetic replies and no native launch."""
from contextlib import ExitStack, contextmanager, nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import analysis_sandbox as sandbox, analysis_child as child
from ai_desktop.analysis_contract import AnalysisDenied
from claude_client_fixtures import compatibility_results
from system_client_fixtures import FakeBinding


PROBES = ("version", "compat-options", "compat-web", "compat-reject", "compat-rpc")
AUTH = (0, b'{"loggedIn":true,"authMethod":"claude.ai","configDirectory":"/home/client/profile"}', b"")
SIGNED_OUT = (1, b'{"loggedIn":false}', b"")
ANSWER = (0, json.dumps({"type": "result", "subtype": "success", "is_error": False,
                        "num_turns": 1, "permission_denials": [], "result": "synthetic answer"}).encode(), b"")


class SystemClaudeRuntimeTests(unittest.TestCase):
    @contextmanager
    def fixture(self, *, responses=None, during=None, interactive=None):
        bound = FakeBinding("claude", pass_fds=(17, 19))
        events, calls = [], []
        replies = dict(zip(PROBES, compatibility_results("2.1.318")))
        replies.update(responses or {})
        state = {"logged_out": False}
        server = Mock()
        server.begin_phase.return_value.is_set.return_value = False

        def invoke(command, **kwargs):
            action = command[-2]
            calls.append((action, command, kwargs))
            events.append(action)
            self.assertEqual(kwargs["pass_fds"], bound.pass_fds)
            self.assertEqual(kwargs["env"], {"PATH": "/usr/bin", "LANG": "C.UTF-8"})
            mounts = [(command[i], command[i + 1], command[i + 2])
                      for i, option in enumerate(command[:-2]) if option in ("--bind", "--ro-bind")]
            if action in PROBES:
                self.assertNotIn("/run/egress.sock", command)
                self.assertFalse(any(target == sandbox.INNER_PROFILE for _, _, target in mounts))
                self.assertNotIn("input_bytes", kwargs)
                reply = replies[action]
            elif action in ("status", "offline-status"):
                reply = SIGNED_OUT if state["logged_out"] else AUTH
            elif action == "offline-logout":
                state["logged_out"] = True
                reply = (0, b"signed out", b"")
            elif action == "analyze":
                reply = ANSWER
            else:
                raise AssertionError(action)
            if during is not None:
                during(action, bound)
            return reply

        def account(*args, **kwargs):
            events.append("account")
            return sandbox.PROFILE

        def proxy(*args, **kwargs):
            events.append("proxy")
            return nullcontext(server)

        def login(command, **kwargs):
            self.assertEqual(command[-2], "login")
            self.assertEqual(kwargs["pass_fds"], bound.pass_fds)
            self.assertTrue(kwargs["close_fds"])
            events.append("login")
            if interactive is not None:
                interactive(bound)
            return SimpleNamespace(returncode=0)

        with ExitStack() as stack:
            stack.enter_context(patch.object(sandbox, "supervisor_ready"))
            ready = stack.enter_context(patch.object(sandbox, "runtime_ready"))
            stack.enter_context(patch.object(sandbox, "profile_lock", return_value=nullcontext()))
            binding = stack.enter_context(patch.object(sandbox.client_binding, "bind", return_value=bound))
            native = stack.enter_context(patch.object(sandbox, "bounded_process", side_effect=invoke))
            account_lookup = stack.enter_context(patch.object(sandbox.account_runtime, "directory", side_effect=account))
            connected = stack.enter_context(patch.object(sandbox.account_runtime, "connected"))
            proxy_factory = stack.enter_context(patch.object(sandbox, "_Proxy", side_effect=proxy))
            stack.enter_context(patch.object(sandbox.os, "isatty", return_value=True))
            interactive_launch = stack.enter_context(patch.object(sandbox.subprocess, "run", side_effect=login))
            yield SimpleNamespace(bound=bound, events=events, calls=calls, binding=binding, native=native,
                                  account=account_lookup, connected=connected, proxy=proxy_factory,
                                  server=server, interactive=interactive_launch, ready=ready)

    def test_newer_client_probes_precede_account_and_egress_and_binding_is_shared(self):
        with self.fixture() as fixture:
            self.assertEqual(sandbox.run_client("analyze", b"synthetic request"), "synthetic answer")
        self.assertEqual(fixture.events, [*PROBES, "account", "proxy", "status", "analyze"])
        fixture.binding.assert_called_once_with("claude")
        self.assertTrue(fixture.bound.closed)
        fixture.server.shutdown.assert_called_once()
        self.assertEqual(fixture.calls[-1][2]["input_bytes"], b"synthetic request")
        self.assertNotIn("synthetic request", repr([command for _, command, _ in fixture.calls]))

    def test_offline_doctor_probe_has_no_account_lock_profile_or_proxy(self):
        with self.fixture() as fixture, patch.object(sandbox, "profile_lock") as lock:
            result = sandbox.probe_client()
        self.assertEqual((result.version, result.status, result.tested), ("2.1.318", "compatible-untested", False))
        self.assertEqual(fixture.events, list(PROBES))
        fixture.account.assert_not_called()
        fixture.connected.assert_not_called()
        fixture.proxy.assert_not_called()
        lock.assert_not_called()
        self.assertTrue(fixture.bound.closed)

    def test_failed_probe_refuses_before_account_and_proxy(self):
        cases = {
            "version": (0, b"3.0.0 (Claude Code)", b""),
            "compat-options": (2, b"", b"SECRET"),
            "compat-web": (2, b"", b"SECRET"),
            "compat-reject": (0, b"ignored unknown flag", b""),
            "compat-rpc": (0, b'{"loggedIn":true}', b""),
        }
        for action, response in cases.items():
            with self.subTest(action=action), self.fixture(responses={action: response}) as fixture:
                with self.assertRaises(AnalysisDenied) as refused:
                    sandbox.run_client("analyze", b"synthetic request")
                self.assertIn(refused.exception.reason, ("client-version", "client-protocol"))
                self.assertNotIn("SECRET", str(refused.exception))
                self.assertEqual(fixture.events, list(PROBES[:PROBES.index(action) + 1]))
                fixture.account.assert_not_called()
                fixture.proxy.assert_not_called()
                self.assertTrue(fixture.bound.closed)

    def test_source_change_during_any_probe_status_or_analysis_stops_next_phase(self):
        for action in (*PROBES, "status", "analyze"):
            def changed(current, bound):
                if current == action:
                    bound.failure = AnalysisDenied("client-changed")
            with self.subTest(action=action), self.fixture(during=changed) as fixture:
                with self.assertRaises(AnalysisDenied) as refused:
                    sandbox.run_client("analyze", b"synthetic request")
                self.assertEqual(refused.exception.reason, "client-changed")
                self.assertEqual(fixture.events[-1], action)
                self.assertTrue(fixture.bound.closed)
                fixture.connected.assert_not_called()
                if action in PROBES:
                    fixture.account.assert_not_called()
                    fixture.proxy.assert_not_called()
                else:
                    fixture.server.shutdown.assert_called_once()

    def test_source_change_after_successful_probe_is_checked_before_account_lookup(self):
        actual_check = sandbox._check_client
        def checked_then_changed(bound, directory, **kwargs):
            result = actual_check(bound, directory, **kwargs)
            bound.failure = AnalysisDenied("client-changed")
            return result
        with self.fixture() as fixture, patch.object(sandbox, "_check_client", side_effect=checked_then_changed):
            with self.assertRaisesRegex(AnalysisDenied, "client-changed"):
                sandbox.run_client("analyze", b"synthetic request")
        self.assertEqual(fixture.events, list(PROBES))
        fixture.account.assert_not_called()
        fixture.proxy.assert_not_called()
        self.assertTrue(fixture.bound.closed)

    def test_cancellation_and_timeout_in_probe_do_not_open_profile_or_proxy(self):
        for reason in ("cancelled", "timeout", "output-limit"):
            def refused(action, bound):
                raise AnalysisDenied(reason)
            with self.subTest(reason=reason), self.fixture(during=refused) as fixture:
                with self.assertRaises(AnalysisDenied) as error:
                    sandbox.run_client("analyze", b"synthetic request")
                self.assertEqual(error.exception.reason, reason)
                fixture.account.assert_not_called()
                fixture.proxy.assert_not_called()
                self.assertTrue(fixture.bound.closed)

    def test_all_probes_share_one_deadline_and_later_probe_cannot_refresh_it(self):
        # Version and both help checks consume the original 20-second budget.
        # The unknown-flag probe must not receive a fresh timeout afterwards.
        clock = {"now": 100}
        def advance(action, bound):
            clock["now"] += {"version": 7, "compat-options": 12, "compat-web": 1}.get(action, 0)
        with self.fixture(during=advance) as fixture, patch.object(sandbox.time, "monotonic", side_effect=lambda: clock["now"]):
            with self.assertRaises(AnalysisDenied) as refused:
                sandbox.run_client("analyze", b"synthetic request")
        self.assertEqual((refused.exception.stage, refused.exception.reason), ("compatibility", "timeout"))
        self.assertEqual(fixture.events, ["version", "compat-options", "compat-web"])
        self.assertEqual([kwargs["timeout"] for _, _, kwargs in fixture.calls], [20, 13, 1])
        fixture.account.assert_not_called()
        fixture.proxy.assert_not_called()
        fixture.connected.assert_not_called()
        self.assertTrue(fixture.bound.closed)

    def test_expired_budget_during_mount_recheck_prevents_native_launch(self):
        clock = {"now": 100}
        with self.fixture() as fixture, patch.object(sandbox.time, "monotonic", side_effect=lambda: clock["now"]):
            mount = fixture.bound.mount_args
            def slow_mount():
                command = mount()
                clock["now"] += 21
                return command
            with patch.object(fixture.bound, "mount_args", side_effect=slow_mount):
                with self.assertRaisesRegex(AnalysisDenied, "timeout"):
                    sandbox.run_client("analyze", b"synthetic request")
        fixture.native.assert_not_called()
        fixture.account.assert_not_called()
        fixture.proxy.assert_not_called()
        self.assertTrue(fixture.bound.closed)

    def test_last_probe_or_validation_cannot_complete_after_deadline_and_open_account(self):
        for overrun in ("native", "recheck", "validation"):
            clock = {"now": 100}
            def slow_last_probe(action, bound):
                if action == "compat-rpc" and overrun == "native":
                    clock["now"] += 21
                if action == "compat-rpc" and overrun == "recheck":
                    recheck = bound.recheck
                    def slow_recheck():
                        recheck()
                        clock["now"] += 21
                    bound.recheck = slow_recheck
            actual_check = sandbox.client_compat.check
            def slow_validation(provider, run):
                result = actual_check(provider, run)
                if overrun == "validation":
                    clock["now"] += 21
                return result
            with self.subTest(overrun=overrun), self.fixture(during=slow_last_probe) as fixture, \
                    patch.object(sandbox.time, "monotonic", side_effect=lambda: clock["now"]), \
                    patch.object(sandbox.client_compat, "check", side_effect=slow_validation):
                with self.assertRaisesRegex(AnalysisDenied, "timeout"):
                    sandbox.run_client("analyze", b"synthetic request")
                self.assertEqual(fixture.events, list(PROBES))
                fixture.account.assert_not_called()
                fixture.proxy.assert_not_called()
                fixture.connected.assert_not_called()
                self.assertTrue(fixture.bound.closed)

    def test_interactive_login_keeps_binding_and_checks_update_before_connected(self):
        with self.fixture() as fixture:
            self.assertEqual(sandbox.run_client("login"), "claude.ai")
        self.assertEqual(fixture.events, [*PROBES, "account", "proxy", "login", "status"])
        fixture.connected.assert_called_once_with(sandbox.PROFILE)
        fixture.interactive.assert_called_once()
        self.assertTrue(fixture.bound.closed)

        def changed(bound):
            bound.failure = AnalysisDenied("client-changed")
        with self.fixture(interactive=changed) as fixture:
            with self.assertRaisesRegex(AnalysisDenied, "client-changed"):
                sandbox.run_client("login")
        self.assertEqual(fixture.events[-1], "login")
        fixture.connected.assert_not_called()
        fixture.server.shutdown.assert_called_once()
        self.assertTrue(fixture.bound.closed)

    def test_interactive_cancel_closes_binding_and_proxy_without_connecting(self):
        def interrupted(bound):
            raise KeyboardInterrupt()
        with self.fixture(interactive=interrupted) as fixture:
            with self.assertRaisesRegex(AnalysisDenied, "cancelled"):
                sandbox.run_client("login")
        fixture.connected.assert_not_called()
        fixture.server.shutdown.assert_called_once()
        self.assertTrue(fixture.bound.closed)

    def test_offline_logout_uses_one_binding_and_stops_on_source_change(self):
        path = Path("/tmp/synthetic-selected-profile")
        with self.fixture() as fixture:
            self.assertFalse(sandbox.local_claude_session(path, logout=True))
        self.assertEqual(fixture.events, [*PROBES, "offline-status", "offline-logout", "offline-status"])
        fixture.binding.assert_called_once_with("claude")
        fixture.proxy.assert_not_called()
        self.assertTrue(fixture.bound.closed)
        for action in ("compat-rpc", "offline-status", "offline-logout"):
            def changed(current, bound):
                if current == action:
                    bound.failure = AnalysisDenied("client-changed")
            with self.subTest(action=action), self.fixture(during=changed) as fixture:
                with self.assertRaisesRegex(AnalysisDenied, "client-changed"):
                    sandbox.local_claude_session(path, logout=True)
                self.assertEqual(fixture.events[-1], action)
                fixture.proxy.assert_not_called()
                self.assertTrue(fixture.bound.closed)

    def test_namespace_refuses_missing_wrong_or_changed_binding(self):
        for bound in (None, FakeBinding("codex")):
            with self.subTest(bound=bound), self.assertRaisesRegex(AnalysisDenied, "client-unavailable"):
                sandbox.namespace_command("version", Path("/tmp/synthetic"), online=False, bound=bound)
        bound = FakeBinding("claude")
        bound.failure = AnalysisDenied("client-changed")
        with self.assertRaisesRegex(AnalysisDenied, "client-changed"):
            sandbox.namespace_command("status", Path("/tmp/synthetic"), online=True, bound=bound)

    def test_child_probes_exec_without_bridge_or_request_input(self):
        for action in PROBES:
            with self.subTest(action=action), patch.object(child, "check_boundary"), \
                    patch.object(child, "install_mode_filter"), patch.object(child, "_Bridge") as bridge, \
                    patch.object(child, "native_input") as request, \
                    patch.object(child.os, "execve", side_effect=SystemExit(23)) as execute:
                with self.assertRaises(SystemExit) as done:
                    child.main([action, "{}"])
                self.assertEqual(done.exception.code, 23)
                execute.assert_called_once_with(str(sandbox.BINARY), sandbox.client_command(action),
                                                sandbox.client_environment(action))
                bridge.assert_not_called()
                request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
