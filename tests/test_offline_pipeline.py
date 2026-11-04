"""End-to-end CLI checks over synthetic reports only: no host probes or AI.

This verifies actual process boundaries rather than mocking CLI dispatch.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_report_policy import report_fixture


ROOT = Path(__file__).resolve().parent.parent


def invoke(*args):
    return subprocess.run(
        [sys.executable, "-B", "-m", "ai_desktop", *(str(x) for x in args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


class OfflinePipelineE2ETests(unittest.TestCase):
    def snapshots(self, directory, changed=False):
        before = report_fixture()
        after = copy.deepcopy(before)
        if changed:
            after["shell"] = "bash"
            after["systemd_health"]["failed_units"] = 2
        old = Path(directory) / "old.json"
        new = Path(directory) / "new.json"
        old.write_text(json.dumps(before), encoding="utf-8")
        new.write_text(json.dumps(after), encoding="utf-8")
        return old, new

    def test_validated_snapshots_compare_with_privacy_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = self.snapshots(tmp, changed=True)
            original = (old.read_bytes(), new.read_bytes())
            for snapshot in (old, new):
                validated = invoke("validate-report", snapshot)
                self.assertEqual(validated.returncode, 0, validated.stderr)
                self.assertEqual(json.loads(validated.stdout),
                                 {"field_count": 8, "valid": True})
            diff = invoke("compare", "--summary", "--exit-on-change", old, new)
            self.assertEqual(diff.returncode, 3, diff.stderr)
            self.assertEqual(json.loads(diff.stdout), {
                "changed": True,
                "change_count": 2,
                "fields": ["shell", "systemd_health.failed_units"],
            })
            self.assertNotIn("bash", diff.stdout)
            self.assertNotIn("fish", diff.stdout)
            self.assertEqual((old.read_bytes(), new.read_bytes()), original)

    def test_identical_reports_pass_and_legacy_diff_stays_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = self.snapshots(tmp)
            diff = invoke("compare", "--summary", "--exit-on-change", old, new)
            self.assertEqual(diff.returncode, 0, diff.stderr)
            self.assertEqual(json.loads(diff.stdout),
                             {"changed": False, "change_count": 0, "fields": []})
            legacy = invoke("compare", old, new)
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            self.assertEqual(json.loads(legacy.stdout),
                             {"changed": False, "change_count": 0, "changes": []})

    def test_invalid_snapshot_is_rejected_end_to_end_without_secret_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = self.snapshots(tmp)
            new.write_text('{"private_token":"SENSITIVE_PAYLOAD"}', encoding="utf-8")
            for result in (invoke("validate-report", new),
                           invoke("compare", "--summary", "--exit-on-change", old, new)):
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("SENSITIVE_PAYLOAD", result.stdout + result.stderr)
                self.assertNotIn(str(new), result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_symlink_is_rejected_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = self.snapshots(tmp)
            link = Path(tmp) / "linked.json"
            link.symlink_to(new)
            self.assertEqual(invoke("validate-report", link).returncode, 1)
            self.assertEqual(invoke("compare", "--exit-on-change", old, link).returncode, 1)

    def test_invalid_invocations_never_trigger_host_operations(self):
        for args in (("validate-report",), ("compare", "--exit-on-change"),
                     ("validate-report", "--unsafe")):
            with self.subTest(args=args):
                result = invoke(*args)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
