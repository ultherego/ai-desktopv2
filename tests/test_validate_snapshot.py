"""Safe single-file validation reuses the strict report reader."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from ai_desktop import validate_snapshot
from ai_desktop.report_policy import MAX_REPORT_BYTES
from test_report_policy import report_fixture


class ValidateSnapshotTests(unittest.TestCase):
    def invoke(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = validate_snapshot.main(argv)
        return code, output.getvalue()

    def test_valid_snapshot_reports_metadata_only_and_never_modifies_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            report = report_fixture()
            path.write_text(json.dumps(report), encoding="utf-8")
            original = path.read_bytes()
            code, output = self.invoke([str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output), {"valid": True, "field_count": 8})
            self.assertNotIn("fish", output)
            self.assertNotIn("kernel", output)
            self.assertEqual(path.read_bytes(), original)

    def test_extra_secret_field_is_rejected_without_echoing_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            report = report_fixture()
            report["private_token"] = "EXTREMELY_PRIVATE"
            path.write_text(json.dumps(report), encoding="utf-8")
            code, output = self.invoke([str(path)])
            self.assertEqual(code, 1)
            self.assertEqual(output.strip(), "VALIDATE: REJECTED")
            self.assertNotIn("EXTREMELY_PRIVATE", output)
            self.assertNotIn(str(path), output)

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text('{"kernel": "x", "kernel": "y"}', encoding="utf-8")
            self.assertEqual(self.invoke([str(path)])[0], 1)

    def test_malformed_and_invalid_unicode_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            for raw in (b"{broken", b"\xff\xfe", b"null", b"[]"):
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    self.assertEqual(self.invoke([str(path)])[0], 1)

    def test_oversized_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_bytes(b"x" * (MAX_REPORT_BYTES + 1))
            self.assertEqual(self.invoke([str(path)])[0], 1)

    def test_symlink_and_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            target = directory / "real.json"
            target.write_text(json.dumps(report_fixture()), encoding="utf-8")
            link = directory / "link.json"
            link.symlink_to(target)
            self.assertEqual(self.invoke([str(link)])[0], 1)
            self.assertEqual(self.invoke([str(directory)])[0], 1)

    def test_missing_file_is_rejected_without_path_disclosure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            code, output = self.invoke([str(path)])
            self.assertEqual(code, 1)
            self.assertNotIn(str(path), output)

    def test_missing_extra_and_unknown_args_are_usage_errors(self):
        for args in ([], ["a", "b"], ["--unsafe"], ["--help", "file"]):
            with self.subTest(args=args):
                code, output = self.invoke(args)
                self.assertEqual(code, 2)
                self.assertIn("Usage:", output)


if __name__ == "__main__":
    unittest.main()
