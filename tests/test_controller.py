import contextlib
import io
import json
import unittest
from unittest.mock import call, patch

from ai_desktop import controller, executor
from test_report_policy import report_fixture


class ControllerTests(unittest.TestCase):
    def test_preparation_is_ordered_and_uses_one_snapshot(self):
        report = report_fixture()
        payload = json.dumps(report)
        findings = {"signals": ["btrfs_low_space"], "unknown": []}
        with (
            patch.object(controller.diagnose, "collect", return_value=report) as collect,
            patch.object(controller, "serialize_report", return_value=payload) as serialize,
            patch.object(controller.executor, "_run",
                         side_effect=[{"probe": "PASS"}, findings]) as isolated,
        ):
            got_report, got_payload, got_findings = controller.prepare()
        self.assertIs(got_report, report)
        self.assertEqual(got_payload, payload)
        self.assertEqual(got_findings, findings)
        collect.assert_called_once_with()
        serialize.assert_called_once_with(report)
        self.assertEqual(isolated.call_args_list, [
            call("--probe"), call("--check", payload),
        ])

    def test_bad_probe_blocks_worker_and_ai(self):
        with (
            patch.object(controller.diagnose, "collect", return_value=report_fixture()),
            patch.object(controller.executor, "_run",
                         return_value={"probe": "FAIL"}) as run,
            patch.object(controller.diagnose, "ask") as ask,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(controller.main(["ask"]), 1)
        run.assert_called_once_with("--probe")
        ask.assert_not_called()

    def test_unknown_report_field_prevents_any_sandbox_call(self):
        with (
            patch.object(controller.diagnose, "collect", return_value={"token": "PRIVATE"}),
            patch.object(controller.executor, "_run") as run,
            patch.object(controller.diagnose, "ask") as ask,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(controller.main(["ask"]), 1)
        self.assertNotIn("PRIVATE", output.getvalue())
        run.assert_not_called()
        ask.assert_not_called()

    def test_untrusted_worker_output_blocks_ai(self):
        with (
            patch.object(controller.diagnose, "collect", return_value=report_fixture()),
            patch.object(controller.executor, "_run", side_effect=[
                {"probe": "PASS"},
                {"signals": [{"execute": "shell"}], "unknown": []},
            ]),
            patch.object(controller.diagnose, "ask") as ask,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(controller.main(["ask"]), 1)
        ask.assert_not_called()

    def test_preview_does_not_call_claude(self):
        report = report_fixture()
        findings = {"signals": ["btrfs_low_space"], "unknown": []}
        with (
            patch.object(controller, "prepare",
                         return_value=(report, json.dumps(report), findings)) as prep,
            patch.object(controller.diagnose, "ask") as ask,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(controller.main(["preview"]), 0)
        self.assertIn("CONTROLLER PREVIEW: PASS", output.getvalue())
        self.assertIn("btrfs_low_space", output.getvalue())
        self.assertIn("REPORT SHA256:", output.getvalue())
        prep.assert_called_once_with()
        ask.assert_not_called()

    def test_ask_uses_exact_prepared_report_once(self):
        report = report_fixture()
        findings = {"signals": [], "unknown": []}
        with (
            patch.object(controller, "prepare",
                         return_value=(report, json.dumps(report), findings)),
            patch.object(controller.diagnose, "ask", return_value=0) as ask,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(controller.main(["ask"]), 0)
        ask.assert_called_once_with(report)
        self.assertIn("REPORT SHA256:", output.getvalue())

    def test_nonzero_claude_exit_propagates(self):
        report = report_fixture()
        with (
            patch.object(controller, "prepare",
                         return_value=(report, json.dumps(report), {
                             "signals": [], "unknown": []
                         })),
            patch.object(controller.diagnose, "ask", return_value=7),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(controller.main(["ask"]), 7)

    def test_claude_timeout_returns_failure(self):
        import subprocess
        report = report_fixture()
        with (
            patch.object(controller, "prepare",
                         return_value=(report, json.dumps(report), {
                             "signals": [], "unknown": []
                         })),
            patch.object(controller.diagnose, "ask",
                         side_effect=subprocess.TimeoutExpired("claude", 120)),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(controller.main(["ask"]), 1)
        self.assertIn("CONTROLLER: FAIL", output.getvalue())

    def test_refuses_root_before_collecting(self):
        with (
            patch.object(controller.os, "geteuid", return_value=0),
            patch.object(controller.diagnose, "collect") as collect,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(controller.main(["preview"]), 2)
        collect.assert_not_called()

    def test_rejects_unapproved_action(self):
        with (
            patch.object(controller, "prepare") as prepare,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(controller.main(["execute"]), 2)
        prepare.assert_not_called()

    def test_timeout_or_missing_bwrap_fail_closed(self):
        with (
            patch.object(controller.diagnose, "collect", return_value=report_fixture()),
            patch.object(controller.executor, "_run",
                         side_effect=RuntimeError("cannot run bwrap")),
            patch.object(controller.diagnose, "ask") as ask,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(controller.main(["ask"]), 1)
        ask.assert_not_called()


class ExecutorAssessmentValidationTests(unittest.TestCase):
    def test_valid_assessment(self):
        data = {"signals": ["btrfs_low_space"], "unknown": []}
        self.assertIs(executor.validate_assessment(data), data)

    def test_non_string_values_rejected_without_typeerror(self):
        for bad in ([{"command": "bash"}], [None], [3], [["nested"]]):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError):
                    executor.validate_assessment({"signals": bad, "unknown": []})

    def test_duplicate_and_unexpected_fields_rejected(self):
        for response in (
            {"signals": ["btrfs_low_space", "btrfs_low_space"], "unknown": []},
            {"signals": [], "unknown": [], "raw": "/home/private"},
            {"signals": "btrfs_low_space", "unknown": []},
        ):
            with self.subTest(response=response):
                with self.assertRaises(RuntimeError):
                    executor.validate_assessment(response)


if __name__ == "__main__":
    unittest.main()
