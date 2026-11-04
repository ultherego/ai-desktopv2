"""Regression tests for the strict JSON-only snapshot action."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from ai_desktop import diagnose
from ai_desktop.compare import compare
from test_report_policy import report_fixture


class SnapshotTests(unittest.TestCase):
    def test_stdout_is_exact_valid_report_without_model_or_file_write(self):
        report = report_fixture()
        with (
            patch.object(diagnose.os, "geteuid", return_value=1000),
            patch.object(diagnose, "collect", return_value=report) as collect,
            patch.object(diagnose, "ask") as ask,
            patch("tempfile.TemporaryDirectory") as tempdir,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = diagnose.main(["snapshot"])
        self.assertEqual(code, 0)
        collect.assert_called_once_with()
        ask.assert_not_called()
        tempdir.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), report)
        self.assertEqual(output.getvalue(), diagnose.serialize_report(report) + "\n")
        self.assertNotIn("PREVIEW:", output.getvalue())

    def test_snapshot_usable_as_before_and_after(self):
        report = report_fixture()
        text = diagnose.serialize_report(report)
        self.assertEqual(compare(json.loads(text), json.loads(text))["change_count"], 0)

    def test_invalid_report_never_leaks_secret(self):
        with (
            patch.object(diagnose.os, "geteuid", return_value=1000),
            patch.object(diagnose, "collect", return_value={"token": "PRIVATE"}),
            patch.object(diagnose, "ask") as ask,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(diagnose.main(["snapshot"]), 1)
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertNotIn('"token"', output.getvalue())
        ask.assert_not_called()

    def test_root_denied_before_collection(self):
        with (
            patch.object(diagnose.os, "geteuid", return_value=0),
            patch.object(diagnose, "collect") as collect,
            patch.object(diagnose, "ask") as ask,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(diagnose.main(["snapshot"]), 2)
        collect.assert_not_called()
        ask.assert_not_called()

    def test_unknown_action_rejected_before_collection(self):
        with (
            patch.object(diagnose, "collect") as collect,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(diagnose.main(["snapshot", "extra"]), 2)
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
