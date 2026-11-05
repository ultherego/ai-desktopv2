"""Private desktop receipts with synthetic account-helper outcomes. No authentication or network."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import account_cli, agent_profiles, codex_cli, login_flow
from ai_desktop.account_store import Accounts, AccountStore, decode


class DesktopAccountAuth(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory(dir=Path.home()))
        self.store = AccountStore(temp)
        self.settings = agent_profiles.Store(temp)
        self.output = self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(account_cli, "AccountStore", return_value=self.store))
        self.identity = self.enterContext(patch.object(account_cli, "os", wraps=os))
        self.identity.getuid.return_value = self.identity.geteuid.return_value = 1000
        self.identity.isatty.return_value = True

    def row(self, provider, name):
        return next(row for row in account_cli.panel_rows(store=self.store)
                    if (row["provider"], row["account"]) == (provider, name))

    def test_legacy_default_usage_and_plan_are_not_login_evidence(self):
        windows = {"five_hour": {"percent": 12, "reset": 1500}}
        self.store.mutate(lambda state: state.note("codex", "default", 1000, windows).with_plan(
            "codex", "default", "plus", 1000))
        legacy = self.store.load().config.data()
        del legacy["auth"]
        self.assertEqual(decode(json.dumps(legacy)).auth_status("codex", "default"), "unknown")
        with patch.object(account_cli, "_login", side_effect=AssertionError("passive status started a helper")):
            self.assertEqual(self.row("codex", "default")["auth_state"], "unknown")
        with patch.object(account_cli, "_login", return_value=0) as helper:
            self.assertEqual(account_cli.main(["check", "codex", "default"]), 0)
            helper.assert_called_once_with("codex", "default", action="check")
        row = self.row("codex", "default")
        self.assertEqual((row["auth_state"], row["windows"], row["last_login_at"]), ("signed_in", windows, None))
        self.assertEqual(self.row("claude", "default")["auth_state"], "unknown")

    def test_default_login_records_only_the_authenticated_recipient_and_selects_its_slot(self):
        self.store.mutate(lambda s: s.add("codex", "work").select("codex", "work").logged_in("codex", "work", 999))
        self.assertEqual(login_flow.login("codex", lambda: 0, store=self.settings), 0)
        self.assertEqual(self.store.load().config.active("codex"), "default")
        self.assertEqual(self.row("codex", "default")["auth_state"], "signed_in")
        self.assertIsNotNone(self.row("codex", "default")["last_login_at"])
        self.assertEqual(self.row("codex", "work")["last_login_at"], 999)
        self.assertEqual(self.row("claude", "default")["auth_state"], "unknown")

    def test_concurrent_account_edit_is_preserved_even_after_successful_authentication(self):
        self.store.mutate(lambda s: s.add("codex", "work"))
        def authenticate():
            self.store.mutate(lambda s: s.select("codex", "work"))
            return 0
        self.assertEqual(login_flow.login("codex", authenticate, store=self.settings), 3)
        self.assertEqual(self.store.load().config.active("codex"), "work")
        self.assertEqual(self.settings.load().config.selected_provider, "claude")
        self.assertEqual(self.row("codex", "default")["auth_state"], "signed_in")

    def test_profile_publish_conflict_restores_only_our_default_selection_and_keeps_receipt(self):
        self.store.mutate(lambda s: s.add("codex", "work").select("codex", "work"))
        with patch.object(self.settings, "mutate", side_effect=agent_profiles.ProfileError("changed")):
            self.assertEqual(login_flow.login("codex", lambda: 0, store=self.settings), 3)
        self.assertEqual(self.store.load().config.active("codex"), "work")
        self.assertEqual(self.row("codex", "default")["auth_state"], "signed_in")

    def test_cancel_stale_or_nonterminal_signout_never_clears_the_receipt(self):
        self.store.mutate(lambda s: s.logged_in("claude", "default", 1000))
        with patch.object(account_cli, "_login", return_value=2) as helper:
            with patch("ai_desktop.terminal_consent.fresh_input", return_value="n") as prompt:
                self.assertEqual(account_cli.main(["logout", "claude", "default"]), 0)
                self.assertIn("claude/default", prompt.call_args.args[0])
                helper.assert_not_called()
            with patch("ai_desktop.terminal_consent.fresh_input", return_value="yes"):
                self.assertEqual(account_cli.main(["logout", "claude", "default"]), 2)
                self.assertEqual(account_cli.main(["logout", "codex", "../invalid"]), 2)
            self.identity.isatty.return_value = False
            self.assertEqual(account_cli.main(["logout", "claude", "default"]), 2)
            helper.assert_called_once_with("claude", "default", action="logout")
        self.assertEqual(self.row("claude", "default")["auth_state"], "signed_in")

    def test_uncertain_remote_revocation_clears_local_state_and_only_its_quota(self):
        sample = {"five_hour": {"percent": 18, "reset": 1500}}
        self.store.mutate(lambda s: s.add("codex", "other").logged_in("codex", "default", 1000)
                          .note("codex", "default", 1000, sample).with_plan("codex", "default", "plus", 1000)
                          .logged_in("codex", "other", 1000).note("codex", "other", 1000, sample))
        with patch.object(account_cli, "_login", return_value=5), patch(
                "ai_desktop.terminal_consent.fresh_input", return_value="yes"):
            self.assertEqual(account_cli.main(["logout", "codex", "default"]), 5)
        account_cli.remember("codex", "default", sample, plan="plus", store=self.store, now=1001)
        row = self.row("codex", "default")
        self.assertEqual((row["auth_state"], row["windows"], row["plan"]), ("signed_out", {}, None))
        other = self.row("codex", "other")
        self.assertEqual((other["auth_state"], other["windows"]), ("signed_in", sample))

    def test_existing_direct_codex_logout_reconciles_same_default_slot(self):
        self.store.mutate(lambda s: s.logged_in("codex", "default", 90))
        with patch.object(codex_cli, "os", self.identity), patch("ai_desktop.codex_runtime.runtime_ready"), \
                patch("ai_desktop.analysis_sandbox.trusted_file", side_effect=lambda path: path), \
                patch("ai_desktop.account_store.AccountStore", return_value=self.store), \
                patch.object(codex_cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                patch("ai_desktop.terminal_consent.fresh_input", side_effect=AssertionError("already confirmed")):
            self.assertEqual(codex_cli.main(["logout", "--allow-chatgpt-logout"]), 0)
        self.assertEqual(self.row("codex", "default")["auth_state"], "signed_out")

    def test_fresh_alias_needed_after_named_claude_disconnect_and_live_check(self):
        self.store.mutate(lambda s: s.add("claude", "work").logged_in("claude", "work", 90))
        with patch.object(account_cli, "_login", return_value=6) as helper, patch(
                "ai_desktop.terminal_consent.fresh_input", return_value="y"):
            self.assertEqual(account_cli.main(["logout", "claude", "work"]), 0)
            self.assertIn("new alias", self.output.getvalue())
            self.assertEqual(account_cli.main(["check", "claude", "work"]), 0)
            self.assertEqual(account_cli.main(["login", "claude", "work"]), 2)
            self.assertEqual(helper.call_count, 2)
        self.assertEqual(account_cli.main(["use", "claude", "work"]), 2)
        self.assertTrue(self.row("claude", "work")["requires_new_alias"])
        self.assertFalse(self.row("claude", "default")["requires_new_alias"])

    def test_missing_named_codex_identity_blocks_reuse_after_service_check(self):
        self.store.mutate(lambda s: s.add("codex", "work"))
        with patch.object(account_cli, "_login", return_value=6):
            self.assertEqual(account_cli.main(["check", "codex", "work"]), 0)
        self.assertTrue(self.row("codex", "work")["requires_new_alias"])
        self.assertEqual(account_cli.main(["use", "codex", "work"]), 2)

    def test_busy_slot_blocks_concurrent_helper_but_other_slot_can_be_checked(self):
        with self.store.lifecycle("codex", "default"), patch.object(account_cli, "_login", return_value=4) as helper:
            self.assertEqual(login_flow.login("codex", lambda: self.fail("busy slot"), store=self.settings), 2)
            self.assertEqual(account_cli.main(["check", "codex", "default"]), 2)
            self.assertEqual(account_cli.main(["check", "claude", "default"]), 0)
            helper.assert_called_once_with("claude", "default", action="check")
        self.assertEqual(self.row("codex", "default")["auth_state"], "unknown")
        self.assertEqual(self.row("claude", "default")["auth_state"], "signed_out")

    def test_data_validation_rejects_duplicate_or_foreign_local_state_and_unsafe_lock(self):
        for records in ((("codex", "default", "bad"),), (("local", "default", "signed_in"),),
                        (("codex", "default", "new_alias"),), (("claude", "default", "signed_in"),) * 2):
            with self.subTest(records=records), self.assertRaises(agent_profiles.ProfileError):
                Accounts(auth=records)
        self.store.mutate(lambda state: state)
        target = Path(self.store.home) / "unrelated"
        target.write_text("preserve")
        (self.store.path.parent / ".auth-codex-default.lock").symlink_to(target)
        with patch.object(account_cli, "_login", side_effect=AssertionError("unsafe marker")):
            self.assertEqual(account_cli.main(["check", "codex", "default"]), 2)
        self.assertEqual(target.read_text(), "preserve")
