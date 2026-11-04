"""Offline cross-epic contract regressions. No provider, filesystem or shell."""
import copy
import hashlib
import unittest

from ai_desktop.transfer_consent import ConsentDenied, ConsentSession
from ai_desktop.typed_plan import PlanDenied, PlanSession
from test_report_policy import report_fixture


class E3TransferConsentTests(unittest.TestCase):
    def test_exact_preview_and_single_confirmation(self):
        report = report_fixture()
        clock = lambda: 100.
        gate = ConsentSession(clock=clock)
        p = gate.preview(report)
        self.assertEqual(p.sha256, hashlib.sha256(p.payload.encode()).hexdigest())
        self.assertEqual(p.recipient, "claude-analysis")
        self.assertEqual(p.purpose, "readonly-diagnostics")
        a = gate.authorize(report, confirmed_sha256=p.sha256, nonce=p.nonce)
        self.assertEqual(a.payload, p.payload)
        with self.assertRaises(ConsentDenied):
            gate.authorize(report, confirmed_sha256=p.sha256, nonce=p.nonce)

    def test_refusal_on_mismatched_payload_and_replay(self):
        report = report_fixture()
        changed = copy.deepcopy(report)
        changed["shell"] = "bash"
        g = ConsentSession()
        p = g.preview(report)
        with self.assertRaises(ConsentDenied):
            g.authorize(changed, confirmed_sha256=p.sha256, nonce=p.nonce)
        with self.assertRaises(ConsentDenied):
            g.authorize(report, confirmed_sha256=p.sha256, nonce=p.nonce)

    def test_wrong_purpose_nonce_hash_expired_eof(self):
        for mutation in (
            {"purpose": "system.write"},
            {"recipient": "attacker"},
            {"confirmed_sha256": "0"*64},
            {"nonce": "wrong"},
        ):
            g = ConsentSession(clock=lambda: 100)
            report = report_fixture()
            p = g.preview(report)
            opts = {"confirmed_sha256": p.sha256, "nonce": p.nonce}
            opts.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ConsentDenied):
                g.authorize(report, **opts)
        g = ConsentSession(clock=lambda: 1000)
        with self.assertRaises(ConsentDenied):
            g.authorize(report_fixture(), confirmed_sha256="0"*64, nonce="x")
        now = [100]
        g = ConsentSession(clock=lambda: now[0])
        p = g.preview(report_fixture())
        now[0] = 1000
        with self.assertRaises(ConsentDenied):
            g.authorize(report_fixture(), confirmed_sha256=p.sha256, nonce=p.nonce)

    def test_invalid_input_denied_without_prompt_or_ai(self):
        with self.assertRaises(ConsentDenied):
            ConsentSession().preview({"private": "secret"})
        with self.assertRaises(ConsentDenied):
            ConsentSession().preview(None)
        g = ConsentSession()
        p = g.preview(report_fixture())
        with self.assertRaises(ConsentDenied):
            g.preview(report_fixture())
        for opts in ({"confirmed_sha256": True, "nonce": p.nonce},
                     {"confirmed_sha256": p.sha256, "nonce": None}):
            new = ConsentSession()
            p2 = new.preview(report_fixture())
            with self.assertRaises(ConsentDenied):
                new.authorize(report_fixture(), **{
                    "confirmed_sha256": opts["confirmed_sha256"],
                    "nonce": opts["nonce"] if opts["nonce"] is None else p2.nonce
                })


class E4TypedPlanTests(unittest.TestCase):
    def setUp(self):
        self.workspace = "/home/tester/Projekty/test-workspace"
        self.state = "a"*64
        self.op = [{"action": "create_text", "path": "notes/new.txt",
                    "content": "synthetic\n"}]

    def test_diff_and_typed_approval_one_use(self):
        gate = PlanSession(clock=lambda: 100)
        p = gate.preview(self.workspace, self.op, state_sha256=self.state)
        self.assertIn("+++ b/notes/new.txt", p.diff)
        self.assertIn("+synthetic", p.diff)
        self.assertNotIn("shell", repr(p.operations))
        auth = gate.authorize(confirmed_sha256=p.sha256, nonce=p.nonce,
                              state_sha256=self.state)
        self.assertEqual(auth.plan_sha256, p.sha256)
        self.assertEqual(auth.operations, (("notes/new.txt", "synthetic\n"),))
        with self.assertRaises(PlanDenied):
            gate.authorize(confirmed_sha256=p.sha256, nonce=p.nonce,
                           state_sha256=self.state)

    def test_denied_unsafe_workspace_and_relative_paths(self):
        for root in ("/", "/etc", "/root", "/tmp", "relative/work",
                     "/home/tester/.ssh", "/home/tester/../etc",
                     "/home//test", "/home/tester/backups", "/home/t/.config"):
            with self.subTest(root=root), self.assertRaises(PlanDenied):
                PlanSession().preview(root, self.op, state_sha256=self.state)
        for path in ("/etc/passwd", "../x", "notes/../../etc/passwd",
                     "notes//x", ".git/config", ".ssh/key", "a\\b",
                     "a/.", "a/..", "notes/secrets/key"):
            with self.subTest(path=path), self.assertRaises(PlanDenied):
                PlanSession().preview(self.workspace, [
                    {**self.op[0], "path": path}], state_sha256=self.state)

    def test_denied_untyped_operations_and_broken_payload(self):
        for op in (
            {"action": "execute", "path": "good.txt", "content": "x"},
            {"action": "delete", "path": "good.txt", "content": "x"},
            {"action": "create_text", "path": "good.txt", "content": "x", "sudo": True},
            {"action": "create_text", "path": "good.txt", "content": "\x00"},
            {"action": "create_text", "path": "good.txt", "content": "x"*4097},
            {"action": "create_text", "path": "good.txt", "content": 4},
        ):
            with self.subTest(op=str(op)[:60]), self.assertRaises(PlanDenied):
                PlanSession().preview(self.workspace, [op], state_sha256=self.state)
        for ops in ([], self.op * 5, [self.op[0], self.op[0]], "invalid"):
            with self.subTest(ops=type(ops)), self.assertRaises(PlanDenied):
                PlanSession().preview(self.workspace, ops, state_sha256=self.state)

    def test_multiple_create_operations_produce_separate_diffs(self):
        additional = {"action": "create_text", "path": "notes/another.txt",
                      "content": "second\\n"}
        plan = PlanSession().preview(
            self.workspace, self.op + [additional], state_sha256=self.state)
        self.assertIn("+++ b/notes/new.txt", plan.diff)
        self.assertIn("+++ b/notes/another.txt", plan.diff)
        self.assertEqual(plan.diff.count("--- /dev/null"), 2)
        self.assertEqual(len(plan.operations), 2)

    def test_stale_plan_state_ttl_and_nonce_fail_closed(self):
        now = [100]
        g = PlanSession(clock=lambda: now[0])
        p = g.preview(self.workspace, self.op, state_sha256=self.state)
        with self.assertRaises(PlanDenied):
            g.authorize(confirmed_sha256=p.sha256, nonce=p.nonce, state_sha256="b"*64)
        with self.assertRaises(PlanDenied):
            g.authorize(confirmed_sha256=p.sha256, nonce=p.nonce, state_sha256=self.state)
        g = PlanSession(clock=lambda: now[0])
        p = g.preview(self.workspace, self.op, state_sha256=self.state)
        now[0] = 1000
        with self.assertRaises(PlanDenied):
            g.authorize(confirmed_sha256=p.sha256, nonce=p.nonce, state_sha256=self.state)

    def test_no_execute_method_or_filesystem_write(self):
        self.assertFalse(hasattr(PlanSession, "execute"))
        self.assertFalse(hasattr(PlanSession, "write"))
        with self.assertRaises(PlanDenied):
            PlanSession().preview(self.workspace, self.op, state_sha256="invalid")

    def test_cross_epic_preview_only_both_gates(self):
        consent = ConsentSession()
        report = report_fixture()
        seen = consent.preview(report)
        self.assertEqual(consent.authorize(
            report, confirmed_sha256=seen.sha256, nonce=seen.nonce
        ).sha256, seen.sha256)
        plan = PlanSession()
        proposed = plan.preview(self.workspace, self.op, state_sha256=self.state)
        self.assertNotEqual(proposed.sha256, seen.sha256)
        self.assertEqual(plan.authorize(
            confirmed_sha256=proposed.sha256, nonce=proposed.nonce,
            state_sha256=self.state,
        ).workspace, self.workspace)


if __name__ == "__main__":
    unittest.main()
