"""Bounded project sources and replacements, counted as UTF-8 bytes."""
import json
import unittest

from ai_desktop import task_contract as contract


def source(contents):
    return {"instruction": "Synthetic change", "files": [
        {"path": f"file{index}.txt", "content": content, "writable": True}
        for index, content in enumerate(contents)]}


def proposed(contents):
    return {"summary": "Synthetic proposal", "files": [
        {"path": f"file{index}.txt", "content": content}
        for index, content in enumerate(contents)]}


class TaskBudgetTests(unittest.TestCase):
    def test_a_change_may_be_asked_for_in_a_message_as_long_as_ask_accepts(self):
        from ai_desktop.analysis_contract import MAX_MESSAGE_BYTES
        self.assertEqual(contract.MAX_INSTRUCTION, MAX_MESSAGE_BYTES)
        files = [{"path": "a.txt", "content": "x\n", "writable": True}]
        self.assertEqual(len(contract.request({"instruction": "y" * MAX_MESSAGE_BYTES, "files": files})["instruction"]), MAX_MESSAGE_BYTES)
        with self.assertRaises(ValueError):
            contract.request({"instruction": "y" * (MAX_MESSAGE_BYTES + 1), "files": files})

    def test_request_mib_file_limit_including_utf8(self):
        for content in ("a" * contract.MAX_FILE, "ą" * (contract.MAX_FILE // 2)):
            with self.subTest(multibyte=content.startswith("ą")):
                value = source([content])
                self.assertEqual(contract.request(value), value)
                with self.assertRaises(ValueError):
                    contract.request(source([content + "x"]))

    def test_request_mib_total_limit(self):
        contents = ["a" * contract.MAX_FILE, "b" * contract.MAX_FILE]
        value = source(contents)
        self.assertEqual(contract.request(value), value)
        with self.assertRaises(ValueError):
            contract.request(source(contents + ["x"]))

    def test_proposal_mib_file_limit_including_utf8(self):
        before = source(["original"])
        for content in ("a" * contract.MAX_FILE, "ą" * (contract.MAX_FILE // 2)):
            with self.subTest(multibyte=content.startswith("ą")):
                value = proposed([content])
                self.assertEqual(contract.proposal(json.dumps(value, ensure_ascii=False), before), value)
                with self.assertRaises(ValueError):
                    contract.proposal(json.dumps(proposed([content + "x"]), ensure_ascii=False), before)

    def test_proposal_mib_total_limit(self):
        before = source(["original"] * 3)
        contents = ["a" * contract.MAX_FILE, "b" * contract.MAX_FILE]
        value = proposed(contents)
        self.assertEqual(contract.proposal(json.dumps(value), before), value)
        with self.assertRaises(ValueError):
            contract.proposal(json.dumps(proposed(contents + ["x"])), before)

    def test_source_count_and_changed_count_are_distinct_limits(self):
        before = source(["original"] * contract.MAX_FILES)
        self.assertEqual(contract.request(before), before)
        with self.assertRaises(ValueError):
            contract.request(source(["original"] * (contract.MAX_FILES + 1)))
        replacement = proposed(["new"] * contract.MAX_CHANGED_FILES)
        self.assertEqual(contract.proposal(json.dumps(replacement), before), replacement)
        with self.assertRaises(ValueError):
            contract.proposal(json.dumps(proposed(["new"] * (contract.MAX_CHANGED_FILES + 1))), before)

    def test_deep_or_invalid_utf8_json_has_a_closed_refusal(self):
        before = source(["original"])
        deep = '{"summary":"PRIVATE","files":' + "[" * 20000 + "0" + "]" * 20000 + "}"
        # CPython versions differ in how much nesting the C JSON parser
        # accepts. Either parsing or strict flat-file validation must refuse,
        # using a fixed reason without copying the untrusted payload.
        for raw in (deep, b"\xff", '{"PRIVATE":'):
            with self.subTest(kind=type(raw).__name__), self.assertRaises(ValueError) as caught:
                contract.proposal(raw, before)
            self.assertIn(str(caught.exception), ("proposal json", "proposal file"))
            self.assertNotIn("PRIVATE", str(caught.exception))


class TaskPathTests(unittest.TestCase):
    def test_explicit_dotfiles_and_previously_blocked_names_roundtrip(self):
        names = (".config/niri/config.kdl", ".ssh/config", ".local/share/note.txt",
                 ".env", "secrets/token.txt", "backup/copy.txt", ".git/config")
        before = {"instruction": "Change the selected synthetic text", "files": [
            {"path": name, "content": "synthetic original\n", "writable": True}
            for name in names]}
        replacement = {"summary": "Selected changes", "files": [
            {"path": name, "content": "synthetic replacement\n"} for name in names]}
        self.assertEqual(contract.request(before), before)
        self.assertEqual(contract.proposal(json.dumps(replacement), before), replacement)

    def test_journal_and_staging_names_are_reserved_in_every_segment(self):
        for reserved in (".ai-desktop-tasks", ".AI-DESKTOP-TASKS", ".ai-task-",
                         ".ai-task-1234", ".AI-TASK-1234"):
            for name in (reserved, reserved + "/data", "nested/" + reserved + "/data"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    contract.path(name)

    def test_dotfiles_do_not_relax_relative_ascii_depth_or_length_rules(self):
        for name in (".", "..", "a/./b", "a/../b", "../a", "/tmp/a", "a//b", "a/",
                     "-option", ".\u0105", "a\x1b", "a/b/c/d/e/f", "x" * 65,
                     "/".join(["x" * 40] * 4)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                contract.path(name)
        self.assertEqual(contract.path(".config/niri/themes/current/palette"),
                         ".config/niri/themes/current/palette")

    def test_selected_dotfile_still_cannot_authorize_another_path(self):
        before = {"instruction": "Change one selected file", "files": [
            {"path": ".ssh/config", "content": "old", "writable": True},
            {"path": ".env", "content": "context", "writable": False}]}
        for name in (".ssh/other", ".env"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                contract.proposal(json.dumps({"summary": "change", "files": [
                    {"path": name, "content": "new"}]}), before)


class UnchangedProposalTests(unittest.TestCase):
    def raw(self, contents):
        return json.dumps(proposed(contents))

    def test_identical_content_is_its_own_refusal_still_a_value_error(self):
        with self.assertRaises(contract.UnchangedProposal) as raised:
            contract.proposal(self.raw(["same\n"]), source(["same\n"]))
        self.assertIsInstance(raised.exception, ValueError)  # generic handlers still refuse it

    def test_other_refusals_keep_the_generic_error(self):
        selected = source(["a\n"])
        selected["files"].append({"path": "readonly.txt", "content": "x\n", "writable": False})
        cases = [
            {"summary": "s", "files": [{"path": "readonly.txt", "content": "changed\n"}]},   # not writable
            {"summary": "s", "files": [{"path": "other.txt", "content": "new\n"}]},          # not selected
            {"summary": "s", "files": [{"path": "file0.txt", "content": "b\n"},
                                       {"path": "file0.txt", "content": "c\n"}]},            # duplicate
        ]
        for case in cases:
            with self.subTest(case=case["files"][0]["path"]):
                with self.assertRaises(ValueError) as raised:
                    contract.proposal(json.dumps(case), selected)
                self.assertNotIsInstance(raised.exception, contract.UnchangedProposal)

    def test_a_real_change_is_still_accepted(self):
        self.assertEqual(contract.proposal(self.raw(["new\n"]), source(["old\n"]))["files"][0]["content"], "new\n")


class AnchoredProposalTests(unittest.TestCase):
    def proposal(self, original, replacements, *, writable=True):
        before = source([original])
        before["files"][0]["writable"] = writable
        raw = {"summary": "Anchored change", "files": [{"path": "file0.txt", "replacements": replacements}]}
        return contract.proposal(json.dumps(raw), before)

    def test_small_proposal_changes_a_mib_file_without_returning_its_complete_content(self):
        original = "a" * (2 * 1024 * 1024) + "\nunique setting=old\n"
        result = self.proposal(original, [{"old": "unique setting=old", "new": "unique setting=new"}])
        self.assertEqual(result["files"], [{"path": "file0.txt", "content": original[:-4] + "new\n"}])
        self.assertNotIn("replacements", result["files"][0])

    def test_disjoint_anchors_resolve_against_the_original_in_any_order(self):
        self.assertEqual(self.proposal("alpha beta", [{"old": "beta", "new": "alpha"},
                                                       {"old": "alpha", "new": "beta"}])["files"][0]["content"],
                         "beta alpha")
        with self.assertRaises(ValueError):
            self.proposal("alpha", [{"old": "alpha", "new": "beta"}, {"old": "beta", "new": "gamma"}])

    def test_ambiguous_missing_and_overlapping_anchors_refuse(self):
        for original, replacements in (
                ("same same", [{"old": "same", "new": "changed"}]),
                ("aaa", [{"old": "aa", "new": "b"}]),
                ("abcdef", [{"old": "abc", "new": "x"}, {"old": "bcd", "new": "y"}]),
                ("abcdef", [{"old": "missing", "new": "x"}]),
                ("abcdef", [{"old": "", "new": "x"}]),
                ("abcdef", [{"old": "abc", "new": "abc"}]),
                ("abcdef", [{"old": "abc", "new": "x"}, {"old": "abc", "new": "y"}])):
            with self.subTest(original=original, edits=replacements), self.assertRaises(ValueError):
                self.proposal(original, replacements)

    def test_read_only_new_files_and_unknown_edit_fields_refuse(self):
        with self.assertRaises(ValueError):
            self.proposal("before", [{"old": "before", "new": "after"}], writable=False)
        with self.assertRaises(ValueError):
            self.proposal(None, [{"old": "before", "new": "after"}])
        for value in ([], "before", [{"old": "before", "new": "after", "occurrence": 1}],
                      [{"old": "before", "new": "after"}] * (contract.MAX_REPLACEMENTS + 1)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.proposal("before", value)

    def test_anchor_and_result_limits_count_utf8_bytes(self):
        with self.assertRaises(ValueError):
            self.proposal("before", [{"old": "before", "new": "ą" * (contract.MAX_REPLACEMENT_TEXT // 2) + "x"}])
        original = "a" * (contract.MAX_FILE - 1) + "z"
        with self.assertRaises(ValueError):
            self.proposal(original, [{"old": "z", "new": "zz"}])
        anchors = [str(n) + "a" * (contract.MAX_REPLACEMENT_TEXT - 1) for n in range(5)]
        with self.assertRaises(ValueError):
            self.proposal("\n".join(anchors), [{"old": anchor, "new": ""} for anchor in anchors])


if __name__ == "__main__":
    unittest.main()
