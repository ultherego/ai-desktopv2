"""Real subprocess tests of bounded stdin-based diagnostic processing.

Inputs are synthetic JSON; never call host collectors or Claude.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ai_desktop.report_policy import MAX_REPORT_BYTES, serialize_report
from test_report_policy import report_fixture


ROOT = Path(__file__).resolve().parent.parent


def invoke(*args, payload=""):
    return subprocess.run(
        [sys.executable, "-B", "-m", "ai_desktop", *(str(a) for a in args)],
        cwd=ROOT,
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


class StdinPipelineTests(unittest.TestCase):
    def test_validator_reads_stdin_without_echoing_content(self):
        payload = serialize_report(report_fixture())
        result = invoke("validate-report", "-", payload=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"field_count": 8, "valid": True})
        self.assertNotIn("0.0.0-synthetic", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_compare_old_file_to_stdin_and_redact_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = report_fixture()
            after = copy.deepcopy(before)
            after["shell"] = "bash"
            old = Path(tmp) / "old.json"
            old.write_text(serialize_report(before), encoding="utf-8")
            original = old.read_bytes()
            result = invoke("compare", "--summary", "--exit-on-change", old, "-",
                            payload=serialize_report(after))
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {"changed": True, "change_count": 1, "fields": ["shell"]})
            self.assertNotIn("fish", result.stdout)
            self.assertNotIn("bash", result.stdout)
            self.assertEqual(old.read_bytes(), original)

    def test_compare_stdin_to_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = report_fixture()
            file = Path(tmp) / "new.json"
            file.write_text(serialize_report(report), encoding="utf-8")
            result = invoke("compare", "--exit-on-change", "-", file,
                            payload=serialize_report(report))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {"changed": False, "change_count": 0, "changes": []})

    def test_invalid_stdin_does_not_leak_data_or_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "good.json"
            report.write_text(serialize_report(report_fixture()), encoding="utf-8")
            for payload in ('{"token":"SUPER_PRIVATE"}', '{"kernel":1,"kernel":2}',
                            'not-json', "[]", "null", '{"x":NaN}', ""):
                with self.subTest(payload=payload):
                    for args in (("validate-report", "-"),
                                 ("compare", "--summary", "--exit-on-change", report, "-")):
                        result = invoke(*args, payload=payload)
                        self.assertEqual(result.returncode, 1, args)
                        self.assertNotIn("SUPER_PRIVATE", result.stdout + result.stderr)
                        self.assertNotIn(str(report), result.stdout + result.stderr)
                        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_oversized_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "good.json"
            report.write_text(serialize_report(report_fixture()), encoding="utf-8")
            payload = "X" * (MAX_REPORT_BYTES + 100)
            self.assertEqual(invoke("validate-report", "-", payload=payload).returncode, 1)
            self.assertEqual(invoke("compare", report, "-", payload=payload).returncode, 1)

    def test_two_stdin_operands_are_rejected_without_reading(self):
        result = invoke("compare", "-", "-", payload=serialize_report(report_fixture()))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stdout)

    def test_legacy_file_input_and_default_semantics_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = report_fixture()
            old = Path(tmp) / "old.json"
            new = Path(tmp) / "new.json"
            old.write_text(serialize_report(report), encoding="utf-8")
            after = copy.deepcopy(report)
            after["shell"] = "bash"
            new.write_text(serialize_report(after), encoding="utf-8")
            self.assertEqual(invoke("validate-report", old).returncode, 0)
            result = invoke("compare", old, new)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["change_count"], 1)


if __name__ == "__main__":
    unittest.main()
