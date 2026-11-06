"""Desktop native-limits pairing, explicit refresh and isolated local receipt, synthetic only."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import account_cli, codex_account_protocol, codex_limits_cli as limits
from ai_desktop.account_store import Accounts, AccountStore, ProfileError, decode
from ai_desktop.codex_quota_ipc import QuotaError


SAMPLES = {"five_hour": {"percent": 12, "reset": 1400}}
WEEK = {"seven_day": {"percent": 21, "reset": 5100}}


class CodexLimitsCLITests(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory(dir=Path.home()))
        self.store = AccountStore(home)
        self.enterContext(patch.object(account_cli, "AccountStore", return_value=self.store))
        self.store.mutate(lambda s: s.add("codex", "work").logged_in("codex", "default", 100)
                          .logged_in("codex", "work", 101).logged_in("claude", "default", 102))
        ident = self.enterContext(patch.object(limits, "os", wraps=os))
        ident.getuid.return_value = ident.geteuid.return_value = 1001
        ident.isatty.return_value = True
        self.output = self.enterContext(redirect_stdout(StringIO()))
        self.policy = self.enterContext(patch.object(limits, "load_policy")).return_value
        self.policy.provider_allowed.return_value = True
        self.confirm = self.enterContext(patch.object(limits, "fresh_input", return_value="yes"))
        self.admin = self.enterContext(patch.object(limits, "_admin", return_value=0))
        self.read = self.enterContext(patch("ai_desktop.codex_quota_ipc.read_limits",
                                            return_value={"windows": SAMPLES, "plan": "pro"}))
        self.enterContext(patch.object(limits.time, "time", return_value=400))

    def connect(self, alias="default"):
        return account_cli.main(["connect-limits", "codex", alias])

    def refresh(self, alias="default"):
        self.output.seek(0)
        self.output.truncate()
        code = account_cli.usage_main(["--provider", "codex", "--account", alias, "--refresh", "--json"])
        return code, json.loads(self.output.getvalue())

    def row(self, provider="codex", alias="default"):
        return next(row for row in account_cli.panel_rows(store=self.store, now=400)
                    if (row["provider"], row["account"]) == (provider, alias))

    def test_connect_reads_now_and_disconnect_restores_separate_passive_reading(self):
        account_cli.remember("codex", "default", WEEK, plan="plus", now=300, store=self.store)
        self.assertEqual(self.connect(), 0)
        self.admin.assert_called_once_with("connect", "default")
        self.read.assert_called_once_with("default")
        row = self.row()
        self.assertEqual((row["live_limits_connected"], row["quota_source"], row["plan"],
                          row["observed_at"], row["windows"]), (True, "codex_native", "pro", 400, SAMPLES))
        self.assertEqual(self.row("codex", "work")["windows"], {})
        self.assertFalse(self.row("claude")["live_limits_connected"])
        account_cli.remember("codex", "default", SAMPLES, now=410, store=self.store)
        self.assertEqual(self.row()["windows"], SAMPLES)
        self.read.return_value = {"plan": None, "windows": {}}
        code, rows = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["quota_source"], rows[0]["plan"], rows[0]["windows"]),
                         ("codex_native", None, {}))
        self.assertEqual(account_cli.main(["disconnect-limits", "codex", "default"]), 0)
        self.assertEqual(self.admin.call_args.args, ("disconnect", "default"))
        row = self.row()
        self.assertFalse(row["live_limits_connected"])
        self.assertEqual(row["auth_state"], "signed_in")
        self.assertEqual((row["quota_source"], row["plan"], set(row["windows"])),
                         ("provider_event", "plus", {"five_hour", "seven_day"}))

    def test_plain_usage_check_and_timer_inputs_remain_passive(self):
        self.store.mutate(lambda s: s.with_native_limits("default", observed=350, windows=SAMPLES, plan="pro"))
        self.assertEqual(account_cli.usage_main(["--json"]), 0)
        self.assertEqual(self.row()["quota_source"], "codex_native")
        self.read.assert_not_called()
        self.admin.assert_not_called()
        self.confirm.assert_not_called()
        self.policy.provider_allowed.assert_not_called()

    def test_requires_connection_signed_in_active_slot_allowed_network_and_explicit_confirm(self):
        self.assertEqual(self.refresh(), (2, {"error": "not-connected"}))
        self.assertEqual(self.refresh("work"), (2, {"error": "selection-changed"}))
        self.assertEqual(self.connect("work"), 2)
        self.confirm.assert_not_called()
        self.confirm.return_value = "n"
        self.assertEqual(self.connect(), 0)
        self.policy.provider_allowed.return_value = False
        self.assertEqual(self.connect(), 2)
        self.store.mutate(lambda s: s.with_native_limits("default"))
        self.assertEqual(self.refresh(), (2, {"error": "policy"}))
        self.assertTrue(self.row()["live_limits_connected"])
        self.store.mutate(lambda s: s.checked("codex", "default", "signed_out"))
        self.assertEqual(self.refresh(), (2, {"error": "check-sign-in"}))
        self.assertFalse(self.row()["live_limits_connected"])
        self.admin.assert_not_called()
        self.read.assert_not_called()

    def test_failed_pair_keeps_original_link_and_incomplete_initial_read_remains_retryable(self):
        self.admin.return_value = 2
        self.assertEqual(self.connect(), 2)
        self.assertFalse(self.row()["live_limits_connected"])
        self.read.assert_not_called()
        self.admin.return_value = 0
        self.read.side_effect = QuotaError("network")
        self.assertEqual(self.connect(), 3)
        self.assertTrue(self.row()["live_limits_connected"])
        self.assertIsNone(self.row()["observed_at"])
        self.read.side_effect = None
        self.assertEqual(self.refresh()[0], 0)
        self.assertEqual(self.row()["windows"], SAMPLES)
        self.admin.return_value = 2
        self.assertEqual(account_cli.main(["disconnect-limits", "codex", "default"]), 2)
        self.assertTrue(self.row()["live_limits_connected"])

    def test_transient_errors_preserve_measured_windows_and_lost_native_auth_clears_only_native(self):
        self.assertEqual(self.connect(), 0)
        for reason in ("busy", "unavailable", "network", "timeout"):
            with self.subTest(reason=reason):
                self.read.side_effect = QuotaError(reason)
                self.assertEqual(self.refresh(), (2, {"error": reason}))
                self.assertEqual(self.row()["windows"], SAMPLES)
        for reason in ("auth-required", "not-connected"):
            self.store.mutate(lambda s: s.with_native_limits("default", observed=400, windows=SAMPLES))
            self.read.side_effect = QuotaError(reason)
            self.assertEqual(self.refresh(), (2, {"error": reason}))
            self.assertFalse(self.row()["live_limits_connected"])
            self.assertEqual(self.row()["native_limits_cleanup_pending"], reason == "auth-required")
            self.assertEqual(self.row()["auth_state"], "signed_in")

    def test_no_cross_slot_publish_during_selection_change_or_policy_revocation(self):
        self.store.mutate(lambda s: s.with_native_limits("default"))
        def switch(alias):
            self.store.mutate(lambda s: s.select("codex", "work"))
            return {"plan": "pro", "windows": SAMPLES}
        self.read.side_effect = switch
        self.assertEqual(self.refresh(), (2, {"error": "selection-changed"}))
        self.assertEqual(self.row()["windows"], {})
        self.assertEqual(self.row("codex", "work")["windows"], {})
        self.store.mutate(lambda s: s.select("codex", "default"))
        self.read.side_effect = None
        self.policy.recheck.side_effect = ValueError("changed")
        self.assertEqual(self.refresh(), (2, {"error": "policy"}))
        self.assertEqual(self.row()["windows"], {})

    def test_local_schema_legacy_named_alias_and_lifecycle_invalidation(self):
        old = self.store.load().config
        legacy_json = old.data()
        del legacy_json["native_limits"]
        del legacy_json["native_stale"]
        self.assertEqual(decode(json.dumps(legacy_json)), old)
        linked = old.with_native_limits("default").with_native_limits("work", observed=350, windows=WEEK)
        self.assertEqual(decode(json.dumps(linked.data())), linked)
        self.assertEqual(linked.checked("claude", "default", "signed_out").native_limits, linked.native_limits)
        self.assertEqual(linked.checked("codex", "work", "signed_in").native_limits, linked.native_limits)
        for updated in (linked.logged_in("codex", "work", 401), linked.retire("codex", "work"),
                        linked.checked("codex", "work", "signed_out")):
            self.assertEqual([r[0] for r in updated.native_limits], ["default"])
            self.assertEqual(updated.native_stale, ("work",))
            self.assertEqual(decode(json.dumps(updated.data())), updated)
        with self.assertRaises(ProfileError):
            Accounts().with_native_limits("default")
        for records in ((["default", 0, None, SAMPLES],), (["default", 400, "unsafe-plan", {}],),
                        (["default", 400, None, {"token": "omitted"}],),
                        (["default", 400, None, SAMPLES],) * 2, (["bogus", 1, None, {}],)):
            raw = {**legacy_json, "native_limits": records}
            with self.subTest(records=records), self.assertRaises((ProfileError, ValueError)):
                decode(json.dumps(raw))

    def test_stale_link_can_be_replaced_or_cleaned_up_after_logout_and_retirement(self):
        self.assertEqual(self.connect(), 0)
        self.store.mutate(lambda s: s.logged_in("codex", "default", 900))
        self.assertTrue(self.row()["native_limits_cleanup_pending"])
        self.assertIsNone(self.row()["observed_at"])
        self.assertEqual(self.connect(), 0)
        self.assertFalse(self.row()["native_limits_cleanup_pending"])
        self.store.mutate(lambda s: s.with_native_limits("work").retire("codex", "work"))
        self.policy.provider_allowed.return_value = False
        self.assertEqual(account_cli.main(["disconnect-limits", "codex", "work"]), 0)
        self.admin.assert_called_with("disconnect", "work")
        self.assertFalse(self.row("codex", "work")["native_limits_cleanup_pending"])
        self.assertTrue(self.row()["live_limits_connected"])

    def test_reconnect_publishes_both_native_windows_to_widget_without_a_browser(self):
        self.store.mutate(lambda s: s.with_native_limits("default", observed=350, windows=SAMPLES)
                          .logged_in("codex", "default", 399))
        self.assertTrue(self.row()["native_limits_cleanup_pending"])
        self.assertEqual(self.row()["windows"], {})

        def read_native(alias):
            self.assertEqual(alias, "default")
            protocol = codex_account_protocol.AccountProtocol("read")
            protocol.start()
            protocol.feed(json.dumps({"id": 0, "result": {"userAgent": "synthetic"}}).encode())
            protocol.feed(json.dumps({"id": 2, "result": {
                "account": {"type": "chatgpt", "planType": "plus", "email": "private@example.invalid"},
                "requiresOpenaiAuth": True}}).encode())
            protocol.feed(json.dumps({"id": 3, "result": {
                "accountId": "private-workspace",
                "rateLimits": {"limitId": "unrelated", "planType": "free"},
                "rateLimitsByLimitId": {"codex": {"limitId": "codex", "planType": "pro",
                    "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1400},
                    "secondary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 5100}}}
            }}).encode())
            return protocol.finish()

        self.read.side_effect = read_native
        with patch("webbrowser.open") as browser:
            self.assertEqual(self.connect(), 0)
        browser.assert_not_called()
        self.admin.assert_called_once_with("connect", "default")
        self.read.assert_called_once_with("default")
        self.output.seek(0)
        self.output.truncate()
        self.assertEqual(account_cli.usage_main(["--json", "--provider", "codex", "--account", "default"]), 0)
        [row] = json.loads(self.output.getvalue())
        self.assertEqual(row["windows"], {"five_hour": {"percent": 0, "reset": 1400},
                                          "seven_day": {"percent": 100, "reset": 5100}})
        self.assertEqual((row["quota_source"], row["plan"], row["observed_at"]), ("codex_native", "pro", 400))
        self.assertTrue(row["live_limits_connected"])
        self.assertFalse(row["native_limits_cleanup_pending"])
        self.assertEqual(self.row("codex", "work")["windows"], {})
        self.assertNotIn("private", self.output.getvalue())
        self.read.assert_called_once_with("default")  # Displaying the fresh snapshot is passive.

    def test_cancelled_reconnect_keeps_stale_marker_without_reading_or_fabricating_windows(self):
        self.store.mutate(lambda s: s.with_native_limits("default", observed=350, windows=SAMPLES)
                          .logged_in("codex", "default", 399))
        self.confirm.return_value = "n"
        self.assertEqual(self.connect(), 0)
        self.assertTrue(self.row()["native_limits_cleanup_pending"])
        self.assertFalse(self.row()["live_limits_connected"])
        self.assertEqual(self.row()["windows"], {})
        self.assertIsNone(self.row()["observed_at"])
        self.admin.assert_not_called()
        self.read.assert_not_called()

    def test_picker_can_disconnect_retired_or_unrecorded_service_grant(self):
        self.store.mutate(lambda s: s.with_native_limits("work").retire("codex", "work"))
        for known in (False, True):
            def choose(_title, choices):
                if len(choices) == 2 and choices[-1] == "Back":
                    return choices.index("default")
                target = "Disconnect live limits for work" if known else "Remove an unlisted native limits connection…"
                return choices.index(target)
            with patch("ai_desktop.ask_commands._pick", side_effect=choose), patch("ai_desktop.cli.main") as dispatch, \
                    patch.object(account_cli, "_widget_result", side_effect=lambda code: code), \
                    patch.object(account_cli, "os", wraps=os) as identity:
                identity.getuid.return_value = identity.geteuid.return_value = 1001
                identity.isatty.return_value = True
                dispatch.return_value = 0
                self.assertEqual(account_cli.main(["connect", "codex", "--from-widget"]), 0)
                dispatch.assert_called_once_with(["account", "disconnect-limits", "codex", "work" if known else "default"])

    def test_busy_lifecycle_lock_blocks_request_and_noninteractive_admin_is_denied(self):
        self.store.mutate(lambda s: s.with_native_limits("default"))
        with self.store.lifecycle("codex", "default"):
            self.assertEqual(self.refresh(), (2, {"error": "busy"}))
        with patch.object(limits.os, "isatty", return_value=False):
            self.assertEqual(self.connect(), 2)
        self.admin.assert_not_called()
        self.read.assert_not_called()
