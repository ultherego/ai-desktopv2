"""Fixed inventory commands and bounded contexts; no real host systemctl."""
import copy
import json
import unittest
from unittest.mock import patch

from ai_desktop import systemd_context as context
from ai_desktop.analysis_contract import AnalysisDenied


class SystemdContextTests(unittest.TestCase):
    def collect(self, output=b"", *, code=0, scope="system", view="installed", error=None):
        with patch.object(context.os, "getuid", return_value=1000), \
                patch.object(context.os, "geteuid", return_value=1000), \
                patch.object(context, "bounded_process", return_value=(code, output, b"SECRET STDERR"),
                             side_effect=error) as process:
            value = context.collect(scope, view)
        return value, process

    def test_installed_files_include_static_and_templates_without_claiming_active(self):
        value, process = self.collect(b"z@.service static -\na.service enabled enabled\nb.timer enabled-runtime enabled\n")
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["listing"], "unit-files")
        self.assertEqual(value["total"], 3)
        self.assertFalse(value["truncated"])
        self.assertEqual(value["items"][0], {"name": "a.service", "state": "enabled", "substate": None})
        self.assertEqual([x["name"] for x in value["items"]], ["a.service", "b.timer", "z@.service"])
        argv = process.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/systemctl")
        self.assertIn("--system", argv)
        self.assertIn("list-unit-files", argv)
        self.assertIn("--no-ask-password", argv)
        self.assertIn("--no-pager", argv)
        self.assertIn("--full", argv)
        self.assertEqual(process.call_args.kwargs["timeout"], 5)
        self.assertEqual(process.call_args.kwargs["limit"], context.MAX_OUTPUT_BYTES)
        self.assertNotIn("shell", process.call_args.kwargs)

    def test_enabled_means_unit_file_enablement_and_active_means_runtime_state(self):
        enabled, p1 = self.collect(b"a.service enabled enabled\nb.timer enabled-runtime enabled\n", view="enabled")
        active, p2 = self.collect(b"a.service loaded active exited SECRET DESCRIPTION\n", view="active")
        self.assertIn("--state=enabled,enabled-runtime", p1.call_args.args[0])
        self.assertIn("--state=active", p2.call_args.args[0])
        self.assertIn("list-units", p2.call_args.args[0])
        self.assertEqual(enabled["listing"], "unit-files")
        self.assertEqual(active["listing"], "units")
        self.assertEqual(active["items"], [{"name": "a.service", "state": "active", "substate": "exited"}])
        self.assertNotIn("SECRET", json.dumps(active))

    def test_all_loaded_units_and_failed_have_distinct_views(self):
        all_units, p1 = self.collect(b"a.service loaded inactive dead\nb.service loaded failed failed Secret\n", view="all")
        failed, p2 = self.collect(b"b.service loaded failed failed Secret\n", view="failed")
        self.assertEqual(all_units["total"], 2)
        self.assertEqual(failed["total"], 1)
        self.assertIn("--all", p1.call_args.args[0])
        self.assertNotIn("--state=failed", p1.call_args.args[0])
        self.assertIn("--state=failed", p2.call_args.args[0])

    def test_inactive_is_a_runtime_view_and_rejects_other_states(self):
        inactive, process = self.collect(b"a.service loaded inactive dead\n", view="inactive")
        self.assertEqual(inactive["listing"], "units")
        self.assertEqual(inactive["items"], [{"name": "a.service", "state": "inactive", "substate": "dead"}])
        self.assertIn("--state=inactive", process.call_args.args[0])
        mismatch, _ = self.collect(b"a.service loaded active exited\n", view="inactive")
        self.assertEqual(mismatch["status"], "invalid")
        changed = copy.deepcopy(inactive)
        changed["items"][0]["state"] = "active"
        with self.assertRaises(ValueError):
            context.validate_context(changed)

    def test_user_bus_is_derived_from_uid_and_inherited_environment_is_excluded(self):
        with patch.dict(context.os.environ, {"DBUS_SESSION_BUS_ADDRESS": "tcp:host=evil", "SYSTEMD_UNIT_PATH": "/evil", "LD_PRELOAD": "/evil", "PAGER": "/evil"}):
            _, process = self.collect(scope="user")
        env = process.call_args.kwargs["env"]
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/1000/bus")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertNotIn("SYSTEMD_UNIT_PATH", env)
        self.assertNotIn("LD_PRELOAD", env)
        self.assertNotIn("PAGER", env)
        self.assertIn("--user", process.call_args.args[0])

    def test_failed_commands_do_not_claim_empty_success_or_publish_stderr(self):
        for kwargs, status in (({"code": 1}, "unavailable"),
                               ({"error": OSError("SECRET")}, "unavailable"),
                               ({"error": AnalysisDenied("timeout")}, "timeout"),
                               ({"error": AnalysisDenied("output-limit")}, "output-limit")):
            with self.subTest(status=status):
                value, _ = self.collect(b"a.service enabled enabled\n", **kwargs)
                self.assertEqual(value["status"], status)
                self.assertIsNone(value["total"])
                self.assertEqual(value["items"], [])
                self.assertFalse(value["truncated"])
                self.assertNotIn("SECRET", json.dumps(value))

    def test_successfully_empty_is_explicitly_distinct_from_unavailable(self):
        empty, _ = self.collect(view="failed")
        self.assertEqual(empty["status"], "ok")
        self.assertEqual(empty["total"], 0)
        self.assertFalse(empty["truncated"])

    def test_malformed_duplicate_unexpected_filtered_and_control_names_fail_closed(self):
        cases = [
            (b"a.service disabled disabled\n", "enabled"),
            (b"a.service loaded inactive dead\n", "active"),
            (b"a.service enabled enabled\na.service enabled enabled\n", "installed"),
            (b"a.service\n", "installed"),
            (b"../secret.service enabled enabled\n", "installed"),
            (b"a\x1b.service enabled enabled\n", "installed"),
            (b"a.service loaded active \x1b[31mdead\n", "all"),
            (b"\xff\n", "installed"),
            (b"a.service future-state -\n", "installed"),
        ]
        for raw, view in cases:
            with self.subTest(raw=raw, view=view):
                value, _ = self.collect(raw, view=view)
                self.assertEqual(value["status"], "invalid")
                self.assertIsNone(value["total"])
                self.assertEqual(value["items"], [])

    def test_escaped_names_remain_literal_and_templates_are_preserved(self):
        value, _ = self.collect(b"foo\\x2dbar@.service static -\n")
        self.assertEqual(value["items"][0]["name"], "foo\\x2dbar@.service")

    def test_entry_and_payload_caps_have_total_and_explicit_truncation(self):
        for length in (5, 240):
            raw = b"".join(("u%04d%s.service enabled enabled\n" % (i, "a" * length)).encode() for i in range(160))
            value, _ = self.collect(raw)
            self.assertEqual(value["total"], 160)
            self.assertTrue(value["truncated"])
            self.assertLessEqual(len(value["items"]), context.MAX_ITEMS)
            self.assertLessEqual(context._encoded_size(value), context.MAX_CONTEXT_BYTES)

    def test_two_maximum_scopes_fit_shared_budget_with_weather_headroom(self):
        from ai_desktop.analysis_contract import MAX_CONTEXT_BYTES, validate_context
        raw = b"".join(("u%04d%s.service enabled enabled\n" % (i, "a" * 240)).encode() for i in range(160))
        scopes = [self.collect(raw, scope=scope)[0] for scope in ("system", "user")]
        self.assertTrue(all(item["truncated"] for item in scopes))
        validate_context(scopes)
        self.assertLessEqual(context._encoded_size(scopes), MAX_CONTEXT_BYTES - 3000)

    def test_root_setuid_and_unknown_scope_or_view_do_not_spawn(self):
        with patch.object(context, "bounded_process") as process:
            for uid, euid in ((0, 0), (1000, 1001)):
                with patch.object(context.os, "getuid", return_value=uid), patch.object(context.os, "geteuid", return_value=euid):
                    with self.assertRaises(ValueError):
                        context.collect()
            for scope, view in (("evil", "all"), ("system", "; reboot"), ([], "all"), ("system", [])):
                with self.assertRaises(ValueError):
                    context.collect(scope, view)
            process.assert_not_called()

    def test_wire_validator_rejects_inconsistent_forged_or_oversized_context(self):
        good, _ = self.collect(b"a.service enabled enabled\n")
        mutations = [("total", True), ("total", 0), ("total", 2), ("source", "shell"),
                     ("scope", "remote"), ("observed_at", "2026-99-99T00:00:00Z"),
                     ("status", "unavailable"), ("listing", "units"), ("truncated", 1),
                     ("extra", "secret")]
        for key, replacement in mutations:
            changed = copy.deepcopy(good)
            changed[key] = replacement
            with self.subTest(key=key), self.assertRaises((ValueError, TypeError)):
                context.validate_context(changed)
        for key, replacement in (("name", "\x1bsecret.service"), ("state", []),
                                 ("substate", "active"), ("extra", "secret")):
            changed = copy.deepcopy(good)
            changed["items"][0][key] = replacement
            with self.subTest(key=key), self.assertRaises((ValueError, TypeError)):
                context.validate_context(changed)


if __name__ == "__main__":
    unittest.main()
