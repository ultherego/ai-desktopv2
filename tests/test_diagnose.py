
import unittest
from unittest.mock import patch

from ai_desktop.diagnose import label, query, main


class DiagnoseTests(unittest.TestCase):
    def test_sanitization(self):
        self.assertEqual(label("x\nsecret"), "x?secret")
        self.assertEqual(len(label("a" * 100)), 48)

    def test_filter_response(self):
        with patch("ai_desktop.diagnose.subprocess.run") as run:
            run.return_value.stdout = "SECRET=abc"
            self.assertEqual(
                query(["findmnt"], {"btrfs"}), "unknown"
            )

    def test_preview_doesnt_call_ai(self):
        with patch(
            "ai_desktop.diagnose.collect",
            return_value={"kernel": "test"},
        ):
            with patch("ai_desktop.diagnose.serialize_report", return_value='{"kernel": "test"}'):
                with patch("ai_desktop.diagnose.ask") as ask:
                    self.assertEqual(main(["preview"]), 0)
                    ask.assert_not_called()

    def test_preview_rejects_unapproved_report(self):
        with patch("ai_desktop.diagnose.collect", return_value={"token": "secret"}):
            with patch("ai_desktop.diagnose.ask") as ask:
                self.assertEqual(main(["preview"]), 1)
                ask.assert_not_called()

    def test_disallow_unexpected_command(self):
        self.assertEqual(main(["unsafe"]), 2)
