"""CachyOS-only E2 host checks. Never uses real Claude, OAuth or live system data.

This filename intentionally does not match unittest default test*.py glob.
The local verification gate invokes it explicitly; Ubuntu CI must not run it.
"""
import hashlib
import unittest

from ai_desktop import e2_boundary
from ai_desktop.report_policy import serialize_report
from test_report_policy import report_fixture


class E2RealSandboxHostChecks(unittest.TestCase):
    def test_actual_isolated_process_reports_pass(self):
        self.assertEqual(e2_boundary.execute("--probe"), {"probe": "PASS"})

    def test_real_isolated_process_accepts_only_approved_synthetic_json(self):
        report = report_fixture()
        payload = serialize_report(report)
        self.assertEqual(e2_boundary.execute("--inspect", report), {
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "field_count": 8,
        })

    def test_invalid_synthetic_data_denied_before_start(self):
        with self.assertRaises(RuntimeError):
            e2_boundary.execute("--inspect", {"token": "SYNTHETIC_ONLY"})


if __name__ == "__main__":
    unittest.main()
