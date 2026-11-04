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
    def test_request_64k_file_limit_including_utf8(self):
        for content in ("a" * 65536, "ą" * 32768):
            with self.subTest(multibyte=content.startswith("ą")):
                value = source([content])
                self.assertEqual(contract.request(value), value)
                with self.assertRaises(ValueError):
                    contract.request(source([content + "x"]))

    def test_request_128k_total_limit(self):
        contents = ["a" * 65536, "b" * 65536]
        value = source(contents)
        self.assertEqual(contract.request(value), value)
        with self.assertRaises(ValueError):
            contract.request(source(contents + ["x"]))

    def test_proposal_64k_file_limit_including_utf8(self):
        before = source(["original"])
        for content in ("a" * 65536, "ą" * 32768):
            with self.subTest(multibyte=content.startswith("ą")):
                value = proposed([content])
                self.assertEqual(contract.proposal(json.dumps(value, ensure_ascii=False), before), value)
                with self.assertRaises(ValueError):
                    contract.proposal(json.dumps(proposed([content + "x"]), ensure_ascii=False), before)

    def test_proposal_128k_total_limit(self):
        before = source(["original"] * 3)
        contents = ["a" * 65536, "b" * 65536]
        value = proposed(contents)
        self.assertEqual(contract.proposal(json.dumps(value), before), value)
        with self.assertRaises(ValueError):
            contract.proposal(json.dumps(proposed(contents + ["x"])), before)

    def test_source_count_and_changed_count_are_distinct_limits(self):
        before = source(["original"] * 64)
        self.assertEqual(contract.request(before), before)
        with self.assertRaises(ValueError):
            contract.request(source(["original"] * 65))
        replacement = proposed(["new"] * 32)
        self.assertEqual(contract.proposal(json.dumps(replacement), before), replacement)
        with self.assertRaises(ValueError):
            contract.proposal(json.dumps(proposed(["new"] * 33)), before)

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


if __name__ == "__main__":
    unittest.main()
