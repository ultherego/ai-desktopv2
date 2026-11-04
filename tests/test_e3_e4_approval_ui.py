"""Offline terminal consent regressions, synthetic streams and data only."""
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from ai_desktop.approval_ui import ApprovalDenied, confirm_plan, confirm_transfer
from ai_desktop.report_policy import serialize_report
from ai_desktop.typed_plan import PlanDenied
from test_report_policy import report_fixture


class TerminalOutput(io.StringIO):
    def isatty(self):
        return True


class TerminalInput:
    def __init__(self, output, verb, response=None, before_read=None):
        self.output = output
        self.verb = verb
        self.response = response
        self.before_read = before_read
        self.reads = 0

    def isatty(self):
        return True

    def readline(self, limit):
        self.reads += 1
        if self.before_read:
            self.before_read()
        if isinstance(self.response, BaseException):
            raise self.response
        if self.response is not None:
            return self.response[:limit]
        # Echo the digest from the actual displayed prompt, not a second plan.
        digest = self.output.getvalue().split(
            "wpisz dokladnie: " + self.verb + " ", 1)[1].split("\n", 1)[0]
        return (self.verb + " " + digest + "\n")[:limit]


class ApprovalUITests(unittest.TestCase):
    def setUp(self):
        self.workspace = "/home/tester/Projekty/synthetic-workspace"
        self.ops = [{"action": "create_text", "path": "notes/new.txt",
                     "content": "SYNTHETIC\n"}]
        self.state = "a" * 64

    def streams(self, verb, **kwargs):
        output = TerminalOutput()
        return TerminalInput(output, verb, **kwargs), output

    def plan(self, source, target, **kwargs):
        return confirm_plan(self.workspace, self.ops, state_sha256=self.state,
                            input_stream=source, output_stream=target, **kwargs)

    def test_transfer_shows_and_authorizes_exact_snapshot(self):
        report = report_fixture()
        source, target = self.streams("PRZEKAZ")
        authorization = confirm_transfer(
            report, input_stream=source, output_stream=target)
        payload = serialize_report(report)
        self.assertEqual(authorization.payload, payload)
        self.assertEqual(authorization.sha256, hashlib.sha256(payload.encode()).hexdigest())
        self.assertIn(payload, target.getvalue())
        self.assertIn("Odbiorca: claude-analysis", target.getvalue())
        self.assertIn("Cel: readonly-diagnostics", target.getvalue())
        self.assertEqual(source.reads, 1)

    def test_plan_shows_workspace_state_exact_content_and_diff(self):
        source, target = self.streams("UTWORZ")
        session, authorization = self.plan(source, target)
        shown = target.getvalue()
        self.assertIn(self.workspace, shown)
        self.assertIn(self.state, shown)
        self.assertIn(authorization.plan_sha256, shown)
        self.assertIn(json.dumps(self.ops, ensure_ascii=True, indent=2), shown)
        self.assertIn("+++ b/notes/new.txt", shown)
        self.assertIn("\n+SYNTHETIC\n", shown)
        self.assertIs(session.consume_for_executor(authorization), authorization)
        with self.assertRaises(PlanDenied):
            session.consume_for_executor(authorization)

    def test_non_tty_input_or_output_refuses_without_disclosure(self):
        for kind in ("source", "target"):
            source, target = self.streams("PRZEKAZ")
            if kind == "source":
                source = io.StringIO("yes\n")
            else:
                target = io.StringIO()
            with self.subTest(kind=kind), self.assertRaises(ApprovalDenied):
                confirm_transfer(report_fixture(), input_stream=source,
                                 output_stream=target)
            self.assertEqual(target.getvalue(), "")

    def test_all_ambiguous_responses_are_denied_without_retry(self):
        for response in ("", "\n", "yes\n", "tak\n", "UTWORZ " + "0"*64 + "\n",
                         "PRZEKAZ " + "0"*64 + "\n", "x"*10000,
                         EOFError(), KeyboardInterrupt()):
            for mode, verb in (("plan", "UTWORZ"), ("transfer", "PRZEKAZ")):
                source, target = self.streams(verb, response=response)
                with self.subTest(mode=mode, response=str(response)[:30]):
                    with self.assertRaisesRegex(ApprovalDenied, "^APPROVAL: REFUSED$"):
                        if mode == "plan":
                            self.plan(source, target)
                        else:
                            confirm_transfer(report_fixture(), input_stream=source,
                                             output_stream=target)
                    self.assertEqual(source.reads, 1)

    def test_exact_response_required_no_space_case_or_eof_normalization(self):
        for transform in (lambda x: x.rstrip("\n"), lambda x: " " + x,
                          lambda x: x.upper(), lambda x: x + "x",
                          lambda x: x.replace("\n", " \n")):
            source, target = self.streams("UTWORZ")
            original = source.readline
            source.readline = lambda limit, f=transform: f(original(limit))
            with self.subTest(transform=transform), self.assertRaises(ApprovalDenied):
                self.plan(source, target)

    def test_expiration_while_waiting_refuses_both_approvals(self):
        for mode, verb in (("plan", "UTWORZ"), ("transfer", "PRZEKAZ")):
            now = [100.0]
            source, target = self.streams(
                verb, before_read=lambda: now.__setitem__(0, 281.0))
            with self.subTest(mode=mode), self.assertRaises(ApprovalDenied):
                if mode == "plan":
                    self.plan(source, target, clock=lambda: now[0])
                else:
                    confirm_transfer(report_fixture(), input_stream=source,
                                     output_stream=target, clock=lambda: now[0])

    def test_expired_plan_handoff_consumed_even_after_clock_goes_back(self):
        now = [100.0]
        source, target = self.streams("UTWORZ")
        session, authorization = self.plan(source, target, clock=lambda: now[0])
        now[0] = 281.0
        with self.assertRaises(PlanDenied):
            session.consume_for_executor(authorization)
        now[0] = 100.0
        with self.assertRaises(PlanDenied):
            session.consume_for_executor(authorization)

    def test_report_mutation_while_waiting_refuses(self):
        report = report_fixture()
        source, target = self.streams(
            "PRZEKAZ", before_read=lambda: report.__setitem__("shell", "bash"))
        with self.assertRaises(ApprovalDenied):
            confirm_transfer(report, input_stream=source, output_stream=target)

    def test_operations_mutation_does_not_change_displayed_plan(self):
        source, target = self.streams(
            "UTWORZ", before_read=lambda: self.ops[0].__setitem__("content", "CHANGED"))
        _, authorization = self.plan(source, target)
        self.assertEqual(authorization.operations, (("notes/new.txt", "SYNTHETIC\n"),))
        self.assertNotIn("CHANGED", target.getvalue())

    def test_unicode_terminal_controls_are_escaped_in_exact_json(self):
        self.ops[0]["content"] = "test\u202e\u009b\u200d\\u202e\n"
        source, target = self.streams("UTWORZ")
        _, authorization = self.plan(source, target)
        self.assertEqual(authorization.operations[0][1], self.ops[0]["content"])
        self.assertIn(json.dumps(self.ops, ensure_ascii=True, indent=2), target.getvalue())
        self.assertTrue(target.getvalue().isascii())

    def test_invalid_input_never_prompts(self):
        for mode in ("transfer", "plan"):
            source, target = self.streams("UTWORZ")
            with self.subTest(mode=mode), self.assertRaises(ApprovalDenied):
                if mode == "transfer":
                    confirm_transfer({"secret": "SYNTHETIC"}, input_stream=source,
                                     output_stream=target)
                else:
                    self.ops[0]["content"] = "\x1b[2J"
                    self.plan(source, target)
            self.assertEqual(target.getvalue(), "")
            self.assertEqual(source.reads, 0)

    def test_broken_or_short_output_refuses_before_read_or_authorization(self):
        for failure in (OSError("SYNTHETIC"), 0):
            source, target = self.streams("UTWORZ")
            opts = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with patch.object(target, "write", **opts):
                with self.assertRaises(ApprovalDenied):
                    self.plan(source, target)
            self.assertEqual(source.reads, 0)


if __name__ == "__main__":
    unittest.main()
