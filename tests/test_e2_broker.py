"""E2 broker boundary policy tests: no real AI, OAuth, network or host writes."""
import contextlib
import copy
import hashlib
import io
import unittest

from ai_desktop import e2_broker
from ai_desktop.report_policy import serialize_report
from test_report_policy import report_fixture


def digest(report):
    return hashlib.sha256(serialize_report(report).encode("utf-8")).hexdigest()


class E2TrustedBrokerTests(unittest.TestCase):
    def test_authorizes_one_fixed_operation_after_matching_approval_and_probe(self):
        report = report_fixture()
        approved = digest(report)
        calls = []
        def isolated(action, data=None):
            calls.append((action, data))
            if action == "--probe":
                return {"probe": "PASS"}
            self.assertEqual(action, "--inspect")
            self.assertEqual(data, report)
            return {"sha256": approved, "field_count": 8}

        result = e2_broker.prepare(report, approved, process=isolated)
        self.assertEqual(result, {
            "operation": "diagnostic.readonly",
            "target": "claude-analysis",
            "payload_sha256": approved,
        })
        self.assertEqual(calls, [("--probe", None), ("--inspect", report)])
        self.assertNotIn("http", str(result))
        self.assertNotIn("token", str(result))

    def test_consent_digest_mismatch_fails_before_process(self):
        report = report_fixture()
        changed = copy.deepcopy(report)
        changed["shell"] = "bash"
        with self.assertRaises(e2_broker.BrokerDenied):
            e2_broker.prepare(changed, digest(report),
                              process=lambda *a: self.fail("must not run"))

    def test_invalid_consent_and_invalid_report_fail_before_process(self):
        report = report_fixture()
        for approved in (None, True, 12, "0"*63, "Z"*64, "a"*65, ""):
            with self.subTest(value=approved), self.assertRaises(e2_broker.BrokerDenied):
                e2_broker.prepare(report, approved,
                                  process=lambda *a: self.fail("must not run"))
        with self.assertRaises(e2_broker.BrokerDenied):
            e2_broker.prepare({"token": "PRIVATE"}, "a"*64,
                              process=lambda *a: self.fail("must not run"))

    def test_untrusted_response_must_match_exact_schema(self):
        d = "f"*64
        valid = {"version": 1, "operation": "diagnostic.readonly",
                 "payload_sha256": d}
        variants = [
            None, [], "diagnostic.readonly",
            {**valid, "url":"http://127.0.0.1:80"},
            {**valid, "shell": "/bin/sh"},
            {**valid, "headers": {"Authorization": "Bearer SYNTHETIC"}},
            {**valid, "operation": "system.write"},
            {**valid, "operation": "diagnostic.readonly; shell"},
            {**valid, "payload_sha256": "e"*64},
            {**valid, "payload_sha256": ["f"*64]},
            {**valid, "payload_sha256": "F"*64},
            {**valid, "version": True}, {**valid, "version": 2},
            {k:v for k,v in valid.items() if k!="version"},
        ]
        for candidate in variants:
            with self.subTest(candidate=str(candidate)[:90]):
                with self.assertRaises(e2_broker.BrokerDenied):
                    e2_broker.validate_intent(candidate, d)
        self.assertEqual(
            e2_broker.validate_intent(valid, d)["target"], "claude-analysis"
        )

    def test_failing_probe_blocks_inspect_and_any_dispatch(self):
        report = report_fixture()
        calls = []
        def isolated(action, data=None):
            calls.append(action)
            return {"probe": "FAIL"}
        with self.assertRaises(e2_broker.BrokerDenied):
            e2_broker.prepare(report, digest(report), process=isolated)
        self.assertEqual(calls, ["--probe"])

    def test_untrusted_inspect_may_not_change_digest_or_return_arbitrary_fields(self):
        report = report_fixture()
        for output in (
            {"sha256": "e"*64, "field_count": 8},
            {"sha256": digest(report), "field_count": 8, "exec":"/bin/sh"},
            {"sha256": digest(report), "field_count": True},
            {"sha256": digest(report), "field_count": 10},
            {"sha256": digest(report), "field_count": 8, "token":"SECRET"},
            "not json", None,
        ):
            with self.subTest(output=str(output)[:80]):
                def isolated(action, data=None):
                    return {"probe": "PASS"} if action=="--probe" else output
                with self.assertRaises(e2_broker.BrokerDenied):
                    e2_broker.prepare(report, digest(report), process=isolated)

    def test_isolated_process_exception_does_not_leak_secrets(self):
        report = report_fixture()
        def failure(action, data=None):
            raise RuntimeError("SYNTHETIC_PRIVATE_TOKEN")
        with self.assertRaises(e2_broker.BrokerDenied) as denied:
            e2_broker.prepare(report, digest(report), process=failure)
        self.assertEqual(str(denied.exception), "BROKER: REFUSED")
        self.assertIsNone(denied.exception.__cause__)
        self.assertNotIn("SYNTHETIC_PRIVATE_TOKEN", str(denied.exception))

    def test_broker_has_no_public_live_provider_dispatch(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(e2_broker.main(["status"]), 0)
        self.assertEqual(output.getvalue().strip(),
                         "E2 BROKER: OFFLINE ONLY (no OAuth, no network)")
        for args in ([], ["ask"], ["send"], ["connect"],
                     ["status", "https://api.anthropic.com"]):
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(e2_broker.main(args), 2)
                self.assertEqual(output.getvalue().strip(), "E2 BROKER: REFUSED")


if __name__ == "__main__":
    unittest.main()
