"""Tests for exclusive private snapshot creation, without live host collection."""
import contextlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import diagnose
from ai_desktop.compare import read_snapshot
from test_report_policy import report_fixture


class PrivateSnapshotSaveTests(unittest.TestCase):
    def invoke(self, args, report=None, root=False):
        payload = report_fixture() if report is None else report
        with (
            patch.object(diagnose.os, "geteuid", return_value=0 if root else 1000),
            patch.object(diagnose, "collect", return_value=payload) as collect,
            patch.object(diagnose, "ask") as ask,
            patch("tempfile.TemporaryDirectory") as tempdir,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            result = diagnose.main(args)
        ask.assert_not_called()
        tempdir.assert_not_called()
        return result, output.getvalue(), collect

    def test_new_snapshot_is_private_valid_and_has_no_values_in_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "snapshot.json"
            code, output, collect = self.invoke(["save", str(dest)])
            self.assertEqual(code, 0)
            collect.assert_called_once_with()
            self.assertEqual(output, "SNAPSHOT SAVE: PASS\n")
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
            self.assertEqual(read_snapshot(dest), report_fixture())
            self.assertTrue(dest.read_bytes().endswith(b"\n"))
            self.assertNotIn("fish", output)

    def test_existing_file_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "existing.json"
            dest.write_bytes(b"IMPORTANT PREEXISTING CONTENT")
            code, output, _ = self.invoke(["save", str(dest)])
            self.assertEqual(code, 2)
            self.assertEqual(dest.read_bytes(), b"IMPORTANT PREEXISTING CONTENT")
            self.assertEqual(output, "SNAPSHOT SAVE: REFUSED\n")

    def test_symlink_target_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "original.json"
            original.write_bytes(b"DO_NOT_OVERWRITE")
            link = Path(tmp) / "link.json"
            link.symlink_to(original)
            code, output, _ = self.invoke(["save", str(link)])
            self.assertEqual(code, 2)
            self.assertEqual(original.read_bytes(), b"DO_NOT_OVERWRITE")
            self.assertTrue(link.is_symlink())
            self.assertNotIn(str(link), output)

    def test_unapproved_report_is_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "reject.json"
            code, output, _ = self.invoke(["save", str(dest)], report={"api_key": "SECRET"})
            self.assertEqual(code, 1)
            self.assertFalse(dest.exists())
            self.assertNotIn("SECRET", output)
            self.assertNotIn("api_key", output)

    def test_missing_parent_never_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "absent" / "snapshot.json"
            code, output, _ = self.invoke(["save", str(dest)])
            self.assertEqual(code, 2)
            self.assertFalse(dest.parent.exists())
            self.assertNotIn(str(dest), output)

    def test_repeated_save_never_overwrites_first_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "first.json"
            self.assertEqual(self.invoke(["save", str(dest)])[0], 0)
            original = dest.read_bytes()
            self.assertEqual(self.invoke(["save", str(dest)])[0], 2)
            self.assertEqual(dest.read_bytes(), original)

    def test_root_denied_before_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "snapshot.json"
            code, _, collect = self.invoke(["save", str(dest)], root=True)
            self.assertEqual(code, 2)
            collect.assert_not_called()
            self.assertFalse(dest.exists())

    def test_invalid_arguments_rejected_before_collection(self):
        for args in (["save"], ["save", "-"], ["save", "--unsafe"],
                     ["save", "one.json", "two.json"], ["preview", "file.json"]):
            with self.subTest(args=args):
                code, _, collect = self.invoke(args)
                self.assertEqual(code, 2)
                collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
