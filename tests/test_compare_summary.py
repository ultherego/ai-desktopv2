"""Redacted offline comparison output; no AI, host probes, or writes."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from ai_desktop.compare import compare, main, summarize
from test_report_policy import report_fixture


class CompareSummaryTests(unittest.TestCase):
    def test_values_are_absent(self):
        before = report_fixture()
        after = copy.deepcopy(before)
        after["shell"] = "bash"
        after["systemd_health"]["failed_units"] = 2
        result = summarize(before, after)
        self.assertEqual(result, {
            "changed": True,
            "change_count": 2,
            "fields": ["shell", "systemd_health.failed_units"],
        })
        serialized = json.dumps(result)
        self.assertNotIn("bash", serialized)
        self.assertNotIn("fish", serialized)

    def test_identical_reports_have_no_fields(self):
        report = report_fixture()
        self.assertEqual(
            summarize(report, report),
            {"changed": False, "change_count": 0, "fields": []},
        )

    def test_cli_outputs_metadata_only(self):
        with tempfile.TemporaryDirectory() as dirname:
            old = Path(dirname) / "old.json"
            new = Path(dirname) / "new.json"
            before = report_fixture()
            after = copy.deepcopy(before)
            after["kernel"] = "a-secret-shaped-but-allowed-label"
            old.write_text(json.dumps(before), encoding="utf-8")
            new.write_text(json.dumps(after), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--summary", str(old), str(new)]), 0)
            data = json.loads(output.getvalue())
            self.assertEqual(data["fields"], ["kernel"])
            self.assertEqual(data["change_count"], 1)
            self.assertNotIn("a-secret-shaped-but-allowed-label", output.getvalue())
            self.assertNotIn("0.0.0-synthetic", output.getvalue())

    def test_default_cli_still_includes_values(self):
        before = report_fixture()
        after = copy.deepcopy(before)
        after["shell"] = "bash"
        self.assertEqual(compare(before, after)["changes"][0], {
            "field": "shell", "before": "fish", "after": "bash"
        })

    def test_invalid_report_rejected_before_summary(self):
        before = report_fixture()
        after = copy.deepcopy(before)
        after["secret"] = "VERY_PRIVATE"
        with self.assertRaises(ValueError):
            summarize(before, after)

    def test_invalid_command_flags_rejected(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--summary"]), 2)
            self.assertEqual(main(["--summary", "a", "b", "c"]), 2)
            self.assertEqual(main(["--unsafe", "a", "b"]), 2)


if __name__ == "__main__":
    unittest.main()
