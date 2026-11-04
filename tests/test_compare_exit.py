"""Offline diff exit codes are opt-in and safe for shell/CI automation."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from ai_desktop.compare import main
from test_report_policy import report_fixture


class CompareExitCodeTests(unittest.TestCase):
    def snapshots(self, directory, changed):
        before = report_fixture()
        after = copy.deepcopy(before)
        if changed:
            after["shell"] = "bash"
        old = Path(directory) / "old.json"
        new = Path(directory) / "new.json"
        old.write_text(json.dumps(before), encoding="utf-8")
        new.write_text(json.dumps(after), encoding="utf-8")
        return old, new

    def invoke(self, args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(args)
        return code, output.getvalue()

    def test_changed_reports_return_three_only_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = self.snapshots(tmp, changed=True)
            code, output = self.invoke(["--exit-on-change", str(old), str(new)])
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(output)["changes"][0]["field"], "shell")
            self.assertEqual(self.invoke([str(old), str(new)])[0], 0)

    def test_unchanged_reports_return_zero_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = self.snapshots(tmp, changed=False)
            code, output = self.invoke(["--exit-on-change", str(old), str(new)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output),
                             {"changed": False, "change_count": 0, "changes": []})

    def test_summary_and_exit_flag_work_in_both_orders_and_hide_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = self.snapshots(tmp, changed=True)
            for options in (["--summary", "--exit-on-change"],
                            ["--exit-on-change", "--summary"]):
                with self.subTest(options=options):
                    code, output = self.invoke(options + [str(old), str(new)])
                    self.assertEqual(code, 3)
                    self.assertEqual(json.loads(output),
                                     {"changed": True, "change_count": 1, "fields": ["shell"]})
                    self.assertNotIn("bash", output)
                    self.assertNotIn("fish", output)

    def test_invalid_report_returns_one_without_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = self.snapshots(tmp, changed=False)
            new.write_text('{"secret":"PRIVATE_VALUE"}', encoding="utf-8")
            code, output = self.invoke(["--exit-on-change", str(old), str(new)])
            self.assertEqual(code, 1)
            self.assertEqual(output.strip(), "COMPARE: REJECTED")
            self.assertNotIn("PRIVATE_VALUE", output)

    def test_unknown_or_duplicate_flags_and_missing_paths_return_two(self):
        for args in (["--exit-on-change"], ["--summary", "--summary", "a", "b"],
                     ["--exit-on-change", "--exit-on-change", "a", "b"],
                     ["--unsafe", "a", "b"], ["--exit-on-change", "a"]):
            with self.subTest(args=args):
                code, output = self.invoke(args)
                self.assertEqual(code, 2)
                self.assertIn("Usage:", output)


if __name__ == "__main__":
    unittest.main()
