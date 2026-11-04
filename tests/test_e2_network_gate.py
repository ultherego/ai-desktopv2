"""E2 cross-boundary regression: approved data, isolated probe, fixed TLS target."""
import copy
import hashlib
import unittest
from unittest.mock import Mock

from ai_desktop import e2_broker, e2_egress, e2_network_gate
from ai_desktop.report_policy import serialize_report
from test_report_policy import report_fixture


def approved_digest(report):
    return hashlib.sha256(serialize_report(report).encode()).hexdigest()


class E2NetworkGateTests(unittest.TestCase):
    def setUp(self):
        self.report = report_fixture()
        self.digest = approved_digest(self.report)
        self.calls = []
        self.expected = {
            "tls": "VERIFIED", "endpoint": e2_egress.HOST,
            "authentication": "NOT_TESTED",
        }

    def process(self, action, report=None):
        self.calls.append(("isolated", action))
        if action == "--probe":
            return {"probe": "PASS"}
        self.assertEqual(action, "--inspect")
        self.assertEqual(report, self.report)
        return {"sha256": self.digest, "field_count": 8}

    def network(self, *, allow_network=False):
        self.calls.append(("network", allow_network))
        return self.expected

    def test_approved_request_runs_isolation_before_egress(self):
        result = e2_network_gate.gated_probe(
            self.report, self.digest, allow_network=True,
            process=self.process, network_probe=self.network)
        self.assertEqual(result, self.expected)
        self.assertEqual(self.calls, [
            ("isolated", "--probe"), ("isolated", "--inspect"),
            ("network", True),
        ])

    def test_default_and_non_boolean_approval_never_start_process_or_network(self):
        for state in (False, None, "true", 1, {}, []):
            with self.subTest(state=state), self.assertRaises(e2_broker.BrokerDenied):
                e2_network_gate.gated_probe(
                    self.report, self.digest, allow_network=state,
                    process=lambda *a: self.fail("must not run process"),
                    network_probe=lambda **k: self.fail("must not network"))

    def test_stale_digest_never_causes_egress(self):
        changed = copy.deepcopy(self.report)
        changed["shell"] = "bash"
        with self.assertRaises(e2_broker.BrokerDenied):
            e2_network_gate.gated_probe(
                changed, self.digest, allow_network=True,
                process=lambda *a: self.fail("must not launch"),
                network_probe=lambda **k: self.fail("must not network"))

    def test_invalid_report_denied_without_egress(self):
        with self.assertRaises(e2_broker.BrokerDenied):
            e2_network_gate.gated_probe(
                {"token":"SYNTHETIC_SECRET"}, self.digest, allow_network=True,
                process=lambda *a: self.fail("must not launch"),
                network_probe=lambda **k: self.fail("must not network"))

    def test_failed_probe_prevents_inspect_and_network(self):
        self.calls.clear()
        def bad_probe(action, report=None):
            self.calls.append(action)
            return {"probe": "FAIL"}
        with self.assertRaises(e2_broker.BrokerDenied):
            e2_network_gate.gated_probe(
                self.report, self.digest, allow_network=True,
                process=bad_probe, network_probe=lambda **k:
                    self.fail("must not network"))
        self.assertEqual(self.calls, ["--probe"])

    def test_spoofed_inspection_prevents_egress(self):
        def fake_process(action, report=None):
            return {"probe": "PASS"} if action=="--probe" else {
                "sha256": "0"*64, "field_count": 8
            }
        with self.assertRaises(e2_broker.BrokerDenied):
            e2_network_gate.gated_probe(
                self.report, self.digest, allow_network=True,
                process=fake_process, network_probe=lambda **k:
                    self.fail("must not network"))

    def test_bogus_egress_proof_refused_without_secrets(self):
        for result in (
            {"tls": "FAKE", "endpoint": e2_egress.HOST,
             "authentication": "NOT_TESTED"},
            {"tls": "VERIFIED", "endpoint": "localhost",
             "authentication": "NOT_TESTED"},
            {**self.expected, "token": "SECRET"},
            {"tls": "VERIFIED", "endpoint": e2_egress.HOST,
             "authentication": "AUTHENTICATED"},
        ):
            with self.subTest(result=result), self.assertRaises(e2_broker.BrokerDenied):
                e2_network_gate.gated_probe(
                    self.report, self.digest, allow_network=True,
                    process=self.process, network_probe=lambda **k: result)

    def test_network_failure_does_not_leak_upstream_details(self):
        def bad_network(**kw):
            raise e2_egress.EgressDenied("SECRET_NETWORK_PATH")
        with self.assertRaises(e2_broker.BrokerDenied) as caught:
            e2_network_gate.gated_probe(
                self.report, self.digest, allow_network=True,
                process=self.process, network_probe=bad_network)
        self.assertEqual(str(caught.exception), "BROKER: REFUSED")
        self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
