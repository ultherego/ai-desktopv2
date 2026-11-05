"""Untrusted configured-scope IDs and fresh consent; no host files or Claude."""
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from ai_desktop import file_context, terminal_consent
from ai_desktop.analysis_contract import AnalysisDenied
from ai_desktop.permissions import Scope
from ai_desktop.question_consent import QuestionConsent


SCOPES = (Scope(0, "~/.config/niri/**"), Scope(1, "~/.ssh/config"),
          Scope(2, "/home/alice/Projekty/calculator/**"))
CONTEXT = {"kind": "files", "files": [{"path": "/home/alice/.config/niri/config.kdl",
                                      "content": "PRIVATE FILE CONTENT\n"}]}


def options(**changes):
    return SimpleNamespace(**{"no_context": False, "preview": False, "details": False, **changes})


class FileContextTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.policy = Mock()
        self.policy.scopes.return_value = SCOPES
        self.policy.access.return_value = {"files": [{"id": "f" + str(scope.index), "pattern": scope.pattern}
                                                     for scope in SCOPES], "systemd": [], "weather": False}
        self.policy.expand.return_value = tuple(file["path"] for file in CONTEXT["files"])
        self.policy.expand_selected.return_value = self.policy.expand.return_value
        self.read = self.enterContext(patch.object(file_context.permissions, "read_selected", return_value=CONTEXT))
        self.consent = self.enterContext(patch.object(file_context, "confirm", return_value=True))
        self.reader = Mock(side_effect=AssertionError("unexpected prompt"))

    def assert_no_read(self):
        self.policy.expand.assert_not_called()
        self.policy.expand_selected.assert_not_called()
        self.read.assert_not_called()

    def permit(self, *, clock=None, approved=True):
        snapshot = Mock()
        snapshot.policy = self.policy
        snapshot.sources = {"claude": True, "weather": False, "systemd": []}
        snapshot.access = self.policy.access.return_value
        permit = QuestionConsent("SYNTHETIC question", snapshot, clock=clock)
        if approved:
            permit.approve()
        return permit, snapshot

    def test_approved_question_reads_quietly_without_new_source_consent(self):
        permit, snapshot = self.permit()
        result = file_context.collect(self.policy, ["f0"], options(), self.reader,
                                      question_consent=permit)
        self.assertEqual(result, CONTEXT)
        self.assertIsNot(result, CONTEXT)
        self.read.assert_called_once()
        self.policy.recheck.assert_has_calls([call(), call()])
        snapshot.require_claude.assert_called()
        self.consent.assert_not_called()
        self.reader.assert_not_called()
        self.assertEqual(self.output.getvalue(), "")

    def test_question_details_show_scope_without_additional_confirmation(self):
        permit, _ = self.permit()
        result = file_context.collect(self.policy, ["f0"], options(details=True), self.reader,
                                      question_consent=permit)
        self.assertEqual(result, CONTEXT)
        self.assertIn(SCOPES[0].pattern, self.output.getvalue())
        self.assertIn(CONTEXT["files"][0]["path"], self.output.getvalue())
        self.consent.assert_not_called()
        self.reader.assert_not_called()

    def test_false_or_unapproved_question_permit_never_becomes_skip_consent(self):
        inactive, _ = self.permit(approved=False)
        for permit in (True, False, object(), Mock(), inactive):
            with self.subTest(permit=type(permit).__name__), self.assertRaises(AnalysisDenied) as error:
                file_context.collect(self.policy, ["f0"], options(), self.reader,
                                     question_consent=permit)
            self.assertEqual(error.exception.reason, "consent")
        self.assert_no_read()
        self.policy.recheck.assert_not_called()
        self.consent.assert_not_called()
        self.reader.assert_not_called()
        self.assertEqual(self.output.getvalue(), "")

    def test_question_grant_rejects_different_policy_or_unavailable_scope_before_metadata(self):
        permit, _ = self.permit()
        foreign = Mock()
        with self.assertRaises(AnalysisDenied):
            file_context.collect(foreign, ["f0"], options(), self.reader, question_consent=permit)
        foreign.access.assert_not_called()
        foreign.scopes.assert_not_called()
        foreign.expand.assert_not_called()
        self.assert_no_read()
        self.consent.assert_not_called()
        permit, _ = self.permit()
        self.policy.access.reset_mock()
        with self.assertRaises(AnalysisDenied):
            file_context.collect(self.policy, ["f9"], options(), self.reader, question_consent=permit)
        self.policy.access.assert_not_called()
        self.policy.scopes.assert_not_called()
        self.assert_no_read()

    def test_question_deadline_after_expansion_stops_before_content_read(self):
        clock = [0]
        permit, _ = self.permit(clock=lambda: clock[0])
        def expand(indices):
            clock[0] = 181
            return tuple(file["path"] for file in CONTEXT["files"])
        self.policy.expand.side_effect = expand
        with self.assertRaises(AnalysisDenied) as error:
            file_context.collect(self.policy, ["f0"], options(), self.reader,
                                 question_consent=permit)
        self.assertEqual(error.exception.reason, "consent")
        self.read.assert_not_called()
        self.consent.assert_not_called()
        self.assertEqual(self.output.getvalue(), "")

    def test_question_revocation_after_read_withholds_context_and_private_output(self):
        permit, snapshot = self.permit()
        def read(*args):
            snapshot.sources["claude"] = False
            return CONTEXT
        self.read.side_effect = read
        with self.assertRaises(AnalysisDenied) as error:
            file_context.collect(self.policy, ["f0"], options(), self.reader,
                                 question_consent=permit)
        self.assertEqual(error.exception.reason, "consent")
        self.read.assert_called_once()
        self.consent.assert_not_called()
        self.assertEqual(self.output.getvalue(), "")

    def test_question_permit_does_not_weaken_policy_recheck_or_read_errors(self):
        permit, _ = self.permit()
        self.policy.recheck.side_effect = ValueError("policy-changed")
        with self.assertRaises(file_context.FileReadDenied):
            file_context.collect(self.policy, ["f0"], options(), self.reader,
                                 question_consent=permit)
        self.assert_no_read()
        self.consent.assert_not_called()
        self.assertIn("The access settings or their state", self.output.getvalue())
        self.assertNotIn("PRIVATE FILE CONTENT", self.output.getvalue())

    def test_exact_configured_ids_map_to_scope_indices_after_fresh_read_consent(self):
        actions = Mock()
        actions.attach_mock(self.consent, "consent")
        actions.attach_mock(self.policy.recheck, "recheck")
        actions.attach_mock(self.policy.expand, "expand")
        actions.attach_mock(self.read, "read")
        item = file_context.collect(self.policy, ["f2", "f0"], options(), self.reader)
        self.assertEqual(item, CONTEXT)
        self.assertIsNot(item, CONTEXT)
        self.assertEqual(actions.mock_calls, [call.consent("files-read", reader=self.reader),
            call.recheck(), call.expand((2, 0)), call.read(self.policy, self.policy.expand.return_value), call.recheck()])
        self.assertIn(CONTEXT["files"][0]["path"], self.output.getvalue())
        self.assertIn("1 file", self.output.getvalue())
        self.assertIn("--details", self.output.getvalue())
        self.assertNotIn("PRIVATE FILE CONTENT", self.output.getvalue())
        self.reader.assert_not_called()

    def test_distinct_subsets_of_one_scope_read_only_the_expanded_selected_paths(self):
        selectors = [{"id": "f0", "path": "config.kdl"}, {"id": "f0", "path": "parts/**"}]
        self.policy.scopes.return_value = (Scope(0, "~/.config/niri/**"),)
        self.policy.access.return_value["files"] = [{"id": "f0", "pattern": "~/.config/niri/**"}]
        paths = ("/home/alice/.config/niri/config.kdl", "/home/alice/.config/niri/parts/keys.kdl")
        self.policy.expand_selected.return_value = paths
        bundle = {"kind": "files", "files": [{"path": path, "content": "synthetic"} for path in paths]}
        self.read.return_value = bundle
        permit, _ = self.permit()
        result = file_context.collect(self.policy, selectors, options(), self.reader, question_consent=permit)
        self.assertEqual(result, bundle)
        self.policy.expand_selected.assert_called_once_with(((0, "config.kdl"), (0, "parts/**")))
        self.policy.expand.assert_not_called()
        self.read.assert_called_once_with(self.policy, paths)
        self.consent.assert_not_called()
        self.reader.assert_not_called()
        self.assertEqual(self.output.getvalue(), "")

    def test_mixed_scope_and_relative_selector_wait_for_consent_and_use_captured_indices(self):
        selectors = ["f2", {"id": "f0", "path": "config.kdl"}]
        actions = Mock()
        actions.attach_mock(self.consent, "consent")
        actions.attach_mock(self.policy.recheck, "recheck")
        actions.attach_mock(self.policy.expand_selected, "expand_selected")
        actions.attach_mock(self.read, "read")
        file_context.collect(self.policy, selectors, options(details=True), self.reader)
        self.assertEqual(actions.mock_calls, [call.consent("files-read", reader=self.reader), call.recheck(),
                         call.expand_selected(((2, None), (0, "config.kdl"))),
                         call.read(self.policy, self.policy.expand_selected.return_value), call.recheck()])
        self.policy.expand.assert_not_called()
        self.assertIn('Narrowed "f0": "config.kdl"', self.output.getvalue())
        self.assertNotIn("PRIVATE FILE CONTENT", self.output.getvalue())

    def test_malformed_selector_and_scope_mismatch_refuse_before_consent_or_expansion(self):
        cases = [[{"id": "f0", "path": "../other"}], [{"id": "f0", "path": "/outside"}],
                 [{"id": "f9", "path": "config.kdl"}], [{"id": "f0", "path": "x", "command": "PRIVATE"}],
                 [{"id": "f0", "path": "x"}, {"id": "f0", "path": "x"}]]
        for selected in cases:
            with self.subTest(selected=selected), self.assertRaises(file_context.FileReadDenied):
                file_context.collect(self.policy, selected, options(), self.reader)
        self.policy.access.return_value["files"][0]["pattern"] = "~/DIFFERENT/**"
        with self.assertRaises(file_context.FileReadDenied):
            file_context.collect(self.policy, [{"id": "f0", "path": "config.kdl"}], options(), self.reader)
        self.consent.assert_not_called()
        self.assert_no_read()
        self.assertNotIn("PRIVATE", self.output.getvalue())

    def test_subset_refusal_and_expired_question_never_fall_back_to_whole_scope(self):
        selectors = [{"id": "f0", "path": "config.kdl"}]
        for cause in ("denied", "expired"):
            self.policy.expand_selected.reset_mock()
            now = [0]
            permit, _ = self.permit(clock=lambda: now[0])
            def expand(_):
                if cause == "denied":
                    raise ValueError("files-denied")
                now[0] = 181
                return self.policy.expand.return_value
            self.policy.expand_selected.side_effect = expand
            with self.subTest(cause=cause), self.assertRaises(AnalysisDenied):
                file_context.collect(self.policy, selectors, options(), self.reader, question_consent=permit)
            self.policy.expand_selected.assert_called_once_with(((0, "config.kdl"),))
            self.policy.expand.assert_not_called()
            self.read.assert_not_called()
            self.consent.assert_not_called()

    def test_unknown_duplicate_wrong_typed_and_pathlike_suggestions_fail_before_consent(self):
        for values in ([], ["f3"], ["f0", "f0"], ["f0", "f99"], "f0", {"f0": True},
                       [True], [0], [None], ["f00"], ["f-1"], ["f０"], ["f0\n"],
                       ["/home/alice/.config/niri/config.kdl"], ["$(PRIVATE)"], ["f0"] * 65):
            with self.subTest(values=values):
                with self.assertRaises(AnalysisDenied) as error:
                    file_context.collect(self.policy, values, options(), self.reader)
                self.assertEqual(error.exception.reason, "input")
        self.consent.assert_not_called()
        self.policy.recheck.assert_not_called()
        self.assert_no_read()
        self.assertNotIn("PRIVATE", self.output.getvalue())

    def test_metadata_mismatch_or_absent_access_cannot_expand_authority(self):
        for allowed in ([], [{"id": "f0", "pattern": "~/OTHER"}],
                        [{"id": "f0", "pattern": SCOPES[0].pattern}] * 2,
                        [{"id": "f9", "pattern": SCOPES[0].pattern}],
                        [{"id": "f0", "pattern": SCOPES[0].pattern, "extra": "PRIVATE"}],
                        [{"id": True, "pattern": SCOPES[0].pattern}], None):
            with self.subTest(allowed=allowed):
                self.policy.access.return_value = {"files": allowed}
                with self.assertRaises(AnalysisDenied):
                    file_context.collect(self.policy, ["f0"], options(), self.reader)
        self.consent.assert_not_called()
        self.assert_no_read()

    def test_scope_metadata_requires_unique_exact_integer_indices(self):
        for scopes in ((Scope(True, "~/niri/*"),), (Scope(-1, "~/niri/*"),),
                       (Scope(65, "~/niri/*"),), (Scope(0, None),), (SCOPES[0], SCOPES[0])):
            with self.subTest(scopes=scopes):
                self.policy.scopes.return_value = scopes
                with self.assertRaises(AnalysisDenied):
                    file_context.collect(self.policy, ["f0"], options(), self.reader)
        self.consent.assert_not_called()
        self.assert_no_read()

    def test_preview_and_no_context_never_collect_or_prompt(self):
        for opts in (options(preview=True), options(no_context=True)):
            with self.subTest(opts=opts):
                with self.assertRaises(AnalysisDenied):
                    file_context.collect(self.policy, ["f0"], opts, self.reader)
        self.policy.access.assert_not_called()
        self.policy.scopes.assert_not_called()
        self.policy.recheck.assert_not_called()
        self.consent.assert_not_called()
        self.assert_no_read()

    def test_denied_read_never_expands_or_opens_targets(self):
        self.consent.return_value = False
        with self.assertRaises(AnalysisDenied) as error:
            file_context.collect(self.policy, ["f0"], options(), self.reader)
        self.assertEqual(error.exception.reason, "consent")
        self.policy.recheck.assert_not_called()
        self.assert_no_read()

    def test_eof_and_interruption_never_expand_or_open_targets(self):
        for error in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=error):
                self.consent.side_effect = error
                with self.assertRaises(type(error)):
                    file_context.collect(self.policy, ["f0"], options(), self.reader)
        self.policy.recheck.assert_not_called()
        self.assert_no_read()

    def test_recheck_failure_before_or_after_read_never_returns_partial_context(self):
        for effects in ([ValueError("PRIVATE")], [None, ValueError("PRIVATE")]):
            with self.subTest(effects=effects):
                self.policy.recheck.side_effect = effects
                with self.assertRaises(AnalysisDenied) as error:
                    file_context.collect(self.policy, ["f0"], options(), self.reader)
                self.assertEqual(error.exception.reason, "input")
        self.assertNotIn("PRIVATE", self.output.getvalue())
        self.assertNotIn("For the question:", self.output.getvalue())

    def test_expansion_read_and_bundle_validation_failures_have_no_partial_or_private_output(self):
        for operation in (self.policy.expand, self.read):
            with self.subTest(operation=operation):
                operation.side_effect = ValueError("PRIVATE RAW PATH OR CONTENT")
                with self.assertRaises(AnalysisDenied):
                    file_context.collect(self.policy, ["f0"], options(), self.reader)
                operation.side_effect = None
        self.read.return_value = {"kind": "files", "files": []}
        with self.assertRaises(AnalysisDenied):
            file_context.collect(self.policy, ["f0"], options(), self.reader)
        self.assertNotIn("PRIVATE", self.output.getvalue())
        self.assertNotIn("For the question:", self.output.getvalue())

    def test_collection_refusals_explain_limits_and_causes_without_partial_context(self):
        cases = (
            (self.policy.expand, "files-count", "more than 64 files"),
            (self.policy.expand, "files-entries", "1024 entries per directory or 4096 entries in total"),
            (self.policy.expand, "files-depth", "8 levels"),
            (self.policy.expand, "files-symlink", "symbolic link"),
            (self.policy.expand, "files-empty", "no files that may be read"),
            (self.read, "files-size", "65536 bytes per file"),
            (self.read, "files-total", "131072 bytes"),
            (self.read, "files-owner", "not owned by the current user"),
            (self.read, "files-links", "hard links"),
            (self.read, "files-mode", "setuid or setgid"),
            (self.read, "files-type", "not a regular file"),
            (self.read, "files-text", "UTF-8"),
            (self.read, "files-private-key", "recognized private key header"),
            (self.read, "files-changed", "changed while"),
            (self.read, "files-access", "verify access"),
            (self.policy.recheck, "policy-changed", "The access settings or their state"),
        )
        for operation, reason, expected in cases:
            with self.subTest(reason=reason):
                self.output.seek(0)
                self.output.truncate(0)
                self.read.reset_mock()
                operation.side_effect = ValueError(reason)
                try:
                    with self.assertRaises(file_context.FileReadDenied) as error:
                        file_context.collect(self.policy, ["f0"], options(), self.reader)
                finally:
                    operation.side_effect = None
                self.assertEqual(error.exception.reason, "input")
                output = self.output.getvalue()
                self.assertIn(expected, output)
                self.assertIn("The file contents were not sent to Claude.", output)
                self.assertNotIn("For the question:", output)
                self.assertNotIn("PRIVATE FILE CONTENT", output)
                if operation is not self.read:
                    self.read.assert_not_called()

    def test_arbitrary_error_text_and_misleading_code_prefix_use_safe_fallback(self):
        for error in (ValueError("files-size: PRIVATE\n\x1b[31m"),
                      ValueError("files-size", "PRIVATE"), OSError("files-size"),
                      TypeError("PRIVATE"), ValueError({"PRIVATE": "files-size"})):
            with self.subTest(error=type(error).__name__):
                self.output.seek(0)
                self.output.truncate(0)
                self.read.side_effect = error
                with self.assertRaises(file_context.FileReadDenied):
                    file_context.collect(self.policy, ["f0"], options(details=True), self.reader)
                output = self.output.getvalue()
                self.assertIn("Could not verify access to, or read, the selected files.", output)
                self.assertNotIn("65536", output)
                self.assertNotIn("PRIVATE", output)
                self.assertNotIn("\x1b", output)
                self.assertNotIn("For the question:", output)

    def test_scope_names_are_terminal_escaped_data(self):
        pattern = "~/niri/\x1b]52;c;PRIVATE\x07\npretend approval\u202e/**"
        self.policy.scopes.return_value = (Scope(0, pattern),)
        self.policy.access.return_value = {"files": [{"id": "f0", "pattern": pattern}]}
        self.consent.return_value = False
        with self.assertRaises(AnalysisDenied):
            file_context.collect(self.policy, ["f0"], options(), self.reader)
        output = self.output.getvalue()
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\x07", output)
        self.assertNotIn("\u202e", output)
        self.assertIn("\\u001b", output)
        self.assertIn("\\npretend", output)
        self.assert_no_read()

    def test_actual_confirm_parser_reprompts_then_requires_explicit_yes(self):
        self.consent.side_effect = terminal_consent.confirm
        self.reader.side_effect = ["", "other", "Yes"]
        with patch.object(terminal_consent.sys.stdin, "isatty", return_value=True), \
                patch.object(terminal_consent.sys.stdout, "isatty", return_value=True):
            file_context.collect(self.policy, ["f0"], options(), self.reader)
        self.assertEqual(self.reader.call_count, 3)
        self.reader.assert_has_calls([call(terminal_consent.PROMPTS["files-read"])] * 3)
        self.read.assert_called_once()

    def test_no_tty_cannot_read_even_if_injected_reader_says_yes(self):
        self.consent.side_effect = terminal_consent.confirm
        self.reader.side_effect = ["Yes"]
        with patch.object(terminal_consent.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(AnalysisDenied) as error:
                file_context.collect(self.policy, ["f0"], options(), self.reader)
        self.assertEqual(error.exception.reason, "consent")
        self.reader.assert_not_called()
        self.assert_no_read()


if __name__ == "__main__":
    unittest.main()
