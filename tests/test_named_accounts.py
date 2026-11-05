"""Real private desktop transactions and fake native transports. No service, network, login or model."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import account_cli as cli, account_runtime as runtime, agent_profiles, analysis_sandbox, execution
from ai_desktop.account_store import AccountStore, Accounts, codex_windows
from ai_desktop.analysis_contract import AnalysisDenied, read_result, result_packet
from tests.test_codex_rate_limits import rate_message, record, turn_ready


class NamedAccounts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = AccountStore(self.home)

    def test_actual_cli_add_use_pins_requests_and_legacy_profiles_stay_readable(self):
        old = agent_profiles.resolve(store=agent_profiles.Store(self.home))
        with patch.object(cli, "AccountStore", return_value=self.store), patch.object(cli, "_login") as login, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["add", "claude", "work"]), 0)
            self.assertEqual(cli.main(["use", "claude", "work"]), 0)
            frozen = execution.validate(agent_profiles.resolve(store=agent_profiles.Store(self.home)))
            self.assertEqual(frozen["account"], "work")
            self.assertEqual(cli.main(["use", "claude", "default"]), 0)
            self.assertEqual(cli.main(["use", "codex", "work"]), 2)
            self.assertEqual(cli.main(["add", "claude", "work"]), 2)
            self.assertEqual(cli.main(["login", "claude", "../bad"]), 2)
            self.assertEqual(agent_profiles.resolve(store=agent_profiles.Store(self.home)), old)
            self.assertEqual(frozen["account"], "work")
            login.assert_not_called()
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
            self.assertFalse((self.store.path.parent / "agents.json").exists())
            login.return_value = 2
            self.assertEqual(cli.main(["add", "codex", "other", "--login"]), 2)
            self.assertEqual(self.store.load().config.active("codex"), "default")
            login.assert_called_once_with("codex", "other")

    def test_alias_isolated_by_peer_and_provider_refuses_missing_and_symlink(self):
        claude, codex = self.home / "claude", self.home / "codex"
        self.assertEqual(runtime.directory(claude), claude)
        with runtime.selected(1000, "claude", "work"):
            with self.assertRaises(AnalysisDenied):
                runtime.directory(claude)
            path = runtime.directory(claude, login=True)
            with self.assertRaises(AnalysisDenied):
                runtime.directory(claude)
            runtime.connected(path)
            self.assertEqual(runtime.directory(claude), path)
            with self.assertRaises(AnalysisDenied):
                runtime.directory(claude, login=True)
            with self.assertRaises(AnalysisDenied):
                runtime.directory(codex)
        with runtime.selected(1001, "claude", "work"), self.assertRaises(AnalysisDenied):
            runtime.directory(claude)
        with runtime.selected(1000, "codex", "work"):
            second = runtime.directory(codex, login=True)
            self.assertNotEqual(path, second)
            second.rmdir()
            second.symlink_to(path, target_is_directory=True)
            with self.assertRaises(AnalysisDenied):
                runtime.directory(codex, login=True)
        for bad in ("../work", "", "default", "work/other", "Work"):
            with self.assertRaises(AnalysisDenied), runtime.selected(1000, "codex", bad):
                self.fail("invalid slot accepted")

    def test_claude_stream_saves_only_numeric_quota_and_ignores_model_text(self):
        final = {"type": "result", "session_id": "test", "subtype": "success", "num_turns": 1,
                 "is_error": False, "permission_denials": [], "result": '{"quota":{"injected":1}}'}
        rate = {"type": "rate_limit_event", "session_id": "test", "rate_limit_info": {"status": "allowed",
                "unifiedWindows": {"five_hour": {"utilization": 0.37, "resetsAt": 1800000000}},
                "secret": "native-irrelevant"}}
        encode = lambda seq: b"\n".join(json.dumps(item).encode() for item in seq)
        native = analysis_sandbox.model_text(0, encode([rate, final]))
        expected = {"five_hour": {"percent": 37, "reset": 1800000000}}
        self.assertEqual(native.meta, {"models": [], "quota": expected})
        packet = result_packet("a"*64, text=native, meta=native.meta, mode="question")
        self.assertNotIn(b"native-irrelevant", packet)
        self.assertEqual(read_result(packet, "a"*64, mode="question").meta, native.meta)
        self.assertIsNone(analysis_sandbox.model_text(0, encode([final])).meta)
        with self.assertRaises(AnalysisDenied):
            analysis_sandbox.model_text(0, encode([{**rate, "session_id": "other"}, final]))

    def test_codex_quota_is_per_account_and_expiry_is_displayed(self):
        limits = {"primary": {"usedPercent": 70, "windowDurationMins": 300, "resetsAt": 1000},
                  "secondary": {"usedPercent": 3, "windowDurationMins": 10080, "resetsAt": 4000}}
        client = turn_ready()
        client.feed(record(rate_message(limits)))
        expected = codex_windows(limits)
        self.assertEqual(client.meta["quota"], expected)
        self.assertEqual(codex_windows({**limits, "limitId": "other-pool"}), {})
        self.store.mutate(lambda state: state.add("codex", "work"))
        cli.remember("codex", "work", expected, store=self.store, now=500)
        cli.remember("codex", "work", {"five_hour": {"percent": 30, "reset": 600}}, store=self.store, now=400)
        rows = cli.panel_rows(store=self.store, now=2000)
        work = next(row for row in rows if row["account"] == "work")
        self.assertIn("window expired", work["usage"])
        self.assertEqual(work["windows"], expected)
        self.assertEqual(work["observed_at"], 500)
        self.assertTrue(all(r["windows"] == {} and r["observed_at"] is None for r in rows if r != work))
        with self.assertRaises(ValueError):
            Accounts().note("codex", "default", 123, {"five_hour": {"percent": 101, "reset": 1000}})

    def test_explicit_retire_removes_only_selected_profile_and_blocks_historical_requests(self):
        target = self.home / "keep"
        target.write_text("preserved")
        with runtime.selected(1000, "claude", "old"):
            path = runtime.directory(self.home / "claude", login=True)
            (path / "secret").write_text("synthetic-token")
            (path / "link").symlink_to(target)
            runtime.connected(path)
            runtime.retire(self.home / "claude")
            runtime.retire(self.home / "claude")
            self.assertEqual(target.read_text(), "preserved")
            self.assertFalse(path.exists())
            for login in (False, True):
                with self.assertRaises(AnalysisDenied):
                    runtime.directory(self.home / "claude", login=login)
        with self.assertRaises(AnalysisDenied):
            runtime.retire(self.home / "claude")
        with patch.object(cli, "AccountStore", return_value=self.store), patch.object(cli, "_login", return_value=0) as admin, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["add", "claude", "old", "--login"]), 0)
            self.assertEqual(cli.main(["use", "claude", "old"]), 0)
            self.assertEqual(cli.main(["retire", "claude", "old"]), 2)
            self.assertEqual(admin.call_count, 1)
            self.assertEqual(cli.main(["retire", "claude", "old", "--confirm-retire"]), 0)
            admin.assert_called_with("claude", "old", action="retire")
            self.assertEqual(cli.main(["login", "claude", "old"]), 2)
            self.assertEqual(cli.main(["use", "claude", "old"]), 2)
            self.assertEqual(cli.main(["add", "claude", "old"]), 2)
            row = next(r for r in cli.panel_rows(store=self.store) if r["account"] == "old")
            self.assertTrue(row["selected"], "retiring cannot silently switch to another account")
            self.assertEqual(row["status"], "retired")
            self.assertIsNotNone(row["last_login_at"])

    def test_named_slot_never_accepts_agent_supplied_or_implicit_provider(self):
        for provider in ("local", "other"):
            data = {"provider": provider, "model": "default", "effort": "default", "access": "changes",
                    "instructions": "", "profile": "default", "account": "work"}
            with self.assertRaises(ValueError):
                execution.validate(data)
