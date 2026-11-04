import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from ai_desktop.compare import compare, main, read_snapshot
from test_report_policy import report_fixture


class CompareTests(unittest.TestCase):
    def test_identical(self):
        report = report_fixture()
        self.assertEqual(compare(report, copy.deepcopy(report)),
                         {"changed": False, "change_count": 0, "changes": []})

    def test_changes_are_deterministic(self):
        before = report_fixture()
        after = copy.deepcopy(before)
        after["shell"] = "bash"
        after["systemd_health"]["failed_units"] = 2
        result = compare(before, after)
        self.assertEqual(result["change_count"], 2)
        self.assertEqual([item["field"] for item in result["changes"]],
                         ["shell", "systemd_health.failed_units"])

    def test_invalid_report_fails_closed(self):
        before = report_fixture()
        after = copy.deepcopy(before)
        after["secret"] = "PRIVATE"
        with self.assertRaises(ValueError):
            compare(before, after)

    def test_duplicate_keys_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text('{"kernel":"x","kernel":"y"}')
            with self.assertRaises(ValueError):
                read_snapshot(path)

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real.json"
            real.write_text(json.dumps(report_fixture()))
            link = Path(tmp) / "link.json"
            link.symlink_to(real)
            with self.assertRaises((OSError, ValueError)):
                read_snapshot(link)

    def test_oversized_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.json"
            path.write_bytes(b"x" * 5000)
            with self.assertRaises(ValueError):
                read_snapshot(path)

    def test_regular_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "valid.json"
            report = report_fixture()
            path.write_text(json.dumps(report))
            self.assertEqual(read_snapshot(path), report)

    def test_missing_arguments(self):
        self.assertEqual(main([]), 2)


if __name__ == "__main__":
    unittest.main()
