"""Credential-free host diagnostics preserve the runtime boundary and privacy."""
from contextlib import ExitStack, contextmanager, redirect_stdout
import io
import json
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import host_client_diagnostic as diagnostic
from ai_desktop.analysis_contract import AnalysisDenied
from claude_client_fixtures import compatibility_results
from system_client_fixtures import FakeBinding


ARGS = ["--allow-offline-client-check", "claude"]
PHASES = ("version", "compat-options", "compat-web", "compat-reject", "compat-rpc")


class HostClientDiagnosticTests(unittest.TestCase):
    @contextmanager
    def fixture(self, *, replies=None, during=None):
        values = dict(zip(PHASES, compatibility_results()))
        values.update(replies or {})
        bound = FakeBinding("claude", pass_fds=(17,))
        calls, events, clock = [], [], [0]

        def run(actual, action, directory, **options):
            self.assertIs(actual, bound)
            self.assertEqual(options, {"online": False, "deadline": 20,
                                       "limit": diagnostic.client_compat.MAX_OUTPUT})
            calls.append(action)
            if during:
                during(action, clock)
            reply = values[action]
            if isinstance(reply, Exception):
                raise reply
            return reply

        with ExitStack() as stack:
            host = stack.enter_context(patch.object(diagnostic, "require_host", side_effect=lambda: events.append("host")))
            installed = stack.enter_context(patch.object(diagnostic.host_analysis_acceptance, "installed_matches",
                                                         side_effect=lambda **kwargs: events.append("installed")))
            discovery = stack.enter_context(patch.object(diagnostic.client_sources, "discover",
                side_effect=lambda provider: events.append("discovery") or SimpleNamespace(status="missing", detail="missing", path=None)))
            metadata = stack.enter_context(patch.object(diagnostic, "codex_metadata", return_value=[]))
            ready = stack.enter_context(patch.object(diagnostic.sandbox, "runtime_ready", side_effect=lambda: events.append("runtime")))
            binding = stack.enter_context(patch.object(diagnostic.sandbox.client_binding, "bind", return_value=bound))
            native = stack.enter_context(patch.object(diagnostic.sandbox, "_bound_run", side_effect=run))
            stack.enter_context(patch.object(diagnostic.time, "monotonic", side_effect=lambda: clock[0]))
            stack.enter_context(patch.object(diagnostic.sandbox.account_runtime, "directory", side_effect=AssertionError("no account")))
            stack.enter_context(patch.object(diagnostic.sandbox, "_Proxy", side_effect=AssertionError("no proxy")))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            yield SimpleNamespace(bound=bound, calls=calls, events=events, clock=clock, output=output,
                                  host=host, installed=installed, discovery=discovery, metadata=metadata,
                                  ready=ready, binding=binding, native=native)

    def test_success_checks_guards_then_all_probes_and_closes_binding(self):
        with self.fixture() as fixture:
            self.assertEqual(diagnostic.main(ARGS), 0)
        self.assertEqual(fixture.events, ["host", "installed", "discovery", "runtime"])
        fixture.installed.assert_called_once_with(native=False)
        fixture.discovery.assert_called_once_with("codex")
        fixture.binding.assert_called_once_with("claude")
        self.assertEqual(fixture.calls, list(PHASES))
        self.assertTrue(fixture.bound.closed)
        records = [json.loads(line) for line in fixture.output.getvalue().splitlines()]
        self.assertEqual(records[0], {"kind": "source", "provider": "codex", "status": "missing", "detail": "missing", "path": None})
        self.assertEqual(records[1]["version"], "2.1.312")
        self.assertEqual(records[-1]["status"], "compatible-tested")

    def test_invalid_selector_and_failed_preflight_never_discover_or_run_native(self):
        for args in ([], [ARGS[0]], [ARGS[0], "codex"], [ARGS[0], "both"], ARGS + ["extra"]):
            with self.subTest(args=args), self.fixture() as fixture:
                self.assertEqual(diagnostic.main(args), 2)
                fixture.host.assert_not_called()
                fixture.discovery.assert_not_called()
                fixture.metadata.assert_not_called()
                fixture.binding.assert_not_called()
        for gate in ("host", "installed"):
            with self.subTest(gate=gate), self.fixture() as fixture:
                getattr(fixture, gate).side_effect = ValueError("SECRET")
                self.assertEqual(diagnostic.main(ARGS), 2)
                fixture.discovery.assert_not_called()
                fixture.metadata.assert_not_called()
                fixture.native.assert_not_called()
                self.assertNotIn("SECRET", fixture.output.getvalue())

    def test_host_guard_requires_ordinary_arch_identity(self):
        for uid, euid, release in ((0, 0, 'ID=arch'), (1000, 0, 'ID=arch'), (1000, 1000, 'ID=ubuntu')):
            with patch.object(diagnostic.os, "getuid", return_value=uid), \
                    patch.object(diagnostic.os, "geteuid", return_value=euid), \
                    patch.object(diagnostic.os, "getgid", return_value=1000), \
                    patch.object(diagnostic.os, "getegid", return_value=1000), \
                    patch.object(diagnostic.Path, "read_text", return_value=release):
                with self.subTest(uid=uid, release=release), self.assertRaises(ValueError):
                    diagnostic.require_host()

    def test_protocol_failure_reports_closed_fields_and_offline_baseline(self):
        raw = b'{"loggedIn":"SECRET","authMethod":"SECRET","configDirectory":"/SECRET","SECRET":"SECRET"}'
        with self.fixture(replies={"compat-options": (1, raw, b"SECRET error: unknown option '--restricted'")}) as fixture:
            self.assertEqual(diagnostic.main(ARGS), 2)
        self.assertEqual(fixture.calls, ["version", "compat-options", "compat-rpc"])
        self.assertTrue(fixture.bound.closed)
        text = fixture.output.getvalue()
        self.assertNotIn("SECRET", text)
        records = [json.loads(line) for line in text.splitlines()]
        observed = next(item for item in records if item.get("phase") == "compat-options")
        self.assertEqual((observed["stdout_shape"], observed["loggedIn"], observed["authMethod"], observed["configDirectory"]),
                         ("object", "invalid", "other", "other"))
        self.assertEqual(observed["unknown_flag"], "--restricted")
        self.assertFalse(observed["signed_out_matches"])
        self.assertTrue(records[-1]["signed_out_matches"])

    def test_baseline_is_not_repeated_and_cannot_refresh_deadline(self):
        with self.fixture(replies={"compat-rpc": (1, b"SECRET", b"")}) as fixture:
            self.assertEqual(diagnostic.main(ARGS), 2)
        self.assertEqual(fixture.calls.count("compat-rpc"), 1)
        def overdue(action, clock):
            if action == "compat-options":
                clock[0] = 20
        with self.fixture(replies={"compat-options": (1, b"SECRET", b"")}, during=overdue) as fixture:
            self.assertEqual(diagnostic.main(ARGS), 2)
        self.assertEqual(fixture.calls, ["version", "compat-options"])

    def test_baseline_failure_stops_and_closes_without_retry_or_native_text(self):
        for error in (AnalysisDenied("timeout"), AnalysisDenied("client-changed"), OSError("SECRET")):
            with self.subTest(reason=type(error).__name__), self.fixture(replies={
                    "compat-options": (1, b"SECRET", b""), "compat-rpc": error}) as fixture:
                self.assertEqual(diagnostic.main(ARGS), 2)
            self.assertEqual(fixture.calls, ["version", "compat-options", "compat-rpc"])
            self.assertTrue(fixture.bound.closed)
            self.assertNotIn("SECRET", fixture.output.getvalue())
            self.assertEqual(json.loads(fixture.output.getvalue().splitlines()[-1])["kind"], "baseline")

    def test_boundary_version_cancellation_and_timeout_never_attempt_baseline(self):
        cases = [("version", reply) for reply in ((0, b"0.0.1 (Claude Code)", b""),
                 (0, b"SECRET" * diagnostic.client_compat.MAX_OUTPUT, b""), (0, "SECRET", b""))]
        cases += [("compat-options", AnalysisDenied(reason, stage=stage)) for reason, stage in (
            ("cancelled", "compatibility"), ("timeout", "compatibility"), ("sandbox", "boundary"),
            ("client-changed", "compatibility"), ("client-protocol", "boundary"))]
        for action, reply in cases:
            with self.subTest(action=action, reply=type(reply).__name__), self.fixture(replies={action: reply}) as fixture:
                self.assertEqual(diagnostic.main(ARGS), 2)
            self.assertNotIn("compat-rpc", fixture.calls)
            self.assertTrue(fixture.bound.closed)

    def test_oversized_or_invalid_native_reply_is_not_parsed_or_exposed(self):
        for reply in ((1, b"SECRET" * diagnostic.client_compat.MAX_OUTPUT, b""), (1, "SECRET", b"")):
            with self.subTest(reply_type=type(reply[1]).__name__), self.fixture(replies={"compat-options": reply}) as fixture:
                self.assertEqual(diagnostic.main(ARGS), 2)
            self.assertNotIn("SECRET", fixture.output.getvalue())
            self.assertIn('"reply":', fixture.output.getvalue())

    def test_unknown_flags_json_keys_and_other_native_text_are_never_printed(self):
        for raw in (b"SECRET", b'{"SECRET":1}', b'{"loggedIn":false}\n{"SECRET":1}', b'[{"SECRET":1}]'):
            result = diagnostic.observation("compat-options", (2, raw, b"error: unknown option '--SECRET'"))
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertIsNone(result["unknown_flag"])
            self.assertEqual(result["stderr_kind"], "other")

    def test_codex_known_paths_only_use_metadata_and_bound_environment_paths(self):
        checked = []
        def inspect(path):
            checked.append(path)
            if path == "/synthetic/.local/bin/codex":
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            raise FileNotFoundError()
        with patch.object(diagnostic.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir="/synthetic")), \
                patch.dict(diagnostic.os.environ, {"CODEX_INSTALL_DIR": "/custom/bin", "CODEX_HOME": "/custom/state"}, clear=True), \
                patch.object(diagnostic.os, "lstat", side_effect=inspect), \
                patch.object(diagnostic.os, "readlink", return_value="../../native/codex") as link:
            records = diagnostic.codex_metadata()
        self.assertEqual(checked, ["/synthetic/.local/bin/codex", "/usr/local/bin/codex", "/usr/bin/codex",
                                   "/custom/bin/codex", "/custom/state/packages/standalone/current/bin/codex"])
        link.assert_called_once_with("/synthetic/.local/bin/codex")
        self.assertEqual(records[0]["link_target"], "../../native/codex")
        with patch.object(diagnostic.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir="/synthetic")), \
                patch.dict(diagnostic.os.environ, {"CODEX_INSTALL_DIR": "/SECRET\n", "CODEX_HOME": "/" + "x" * 4096}, clear=True), \
                patch.object(diagnostic.os, "lstat", side_effect=FileNotFoundError()) as inspect:
            records = diagnostic.codex_metadata()
        self.assertEqual(inspect.call_count, 3)
        self.assertNotIn("SECRET", json.dumps(records))
        self.assertEqual([record["status"] for record in records[-2:]], ["invalid-path", "invalid-path"])


if __name__ == "__main__":
    unittest.main()
