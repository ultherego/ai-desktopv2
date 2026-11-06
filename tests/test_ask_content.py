"""Actual ask request generation for bounded excerpts from large local sources."""
import copy
import json
import os
from unittest.mock import patch

from ai_desktop import analysis_contract as wire, ask_content, assistant_contract, assistant_flow as flow
from ai_desktop import conversation_state, discovery_contract, execution, permissions
from ai_desktop.question_consent import ConversationConsent
from test_ask_discovery import Base, options, sel, ANSWER, ANSWER_PLAN


class AskContentTests(Base):
    def run_flow(self, responses, *, question="Read large.log", before=None, consent=None):
        self.wire = []
        replies = iter(responses)

        def request(text, args, reader, **kwargs):
            if before is not None:
                before(len(self.wire) + 1, kwargs)
            fields = {name: kwargs[name] for name in ("access", "context", "discovery", "web") if name in kwargs}
            gate = wire.AnalysisConsent(None, question=text, **fields)
            permit = kwargs["question_consent"]
            permit.claim(gate, text, **fields)
            approval = permit.authorize(gate, text, **fields)
            decoded, digest = wire.parse_request(approval.payload.encode())
            self.assertEqual(digest, approval.sha256)
            self.wire.append(json.loads(decoded))
            reply = next(replies)
            return reply(self.wire[-1]) if callable(reply) else reply

        profile = {"provider": "claude", "model": "default", "effort": "default", "instructions": "",
                   "profile": "diagnostic", "access": "read-only"}
        with execution.using(profile):
            return flow.run(question, options(), self.reader, request, conversation_consent=consent)

    @staticmethod
    def search(path="large.log", needle="UNIQUE-MARKER", cursor=0):
        return json.dumps({"action": "search-content", "file": sel(path), "needle": needle, "cursor": cursor})

    @staticmethod
    def ranges(path="large.log", first=1, last=1):
        return json.dumps({"action": "read-range", "file": sel(path), "first": first, "last": last})

    def test_32mib_file_is_searched_and_read_without_sending_the_whole_file(self):
        size = 32 * 1024 * 1024
        suffix = b"\nUNIQUE-MARKER verified=yes\n"
        path = self.home / ".config/large.log"
        path.write_bytes(b"x" * (size - len(suffix)) + suffix)

        def followup(request):
            step = request["discovery"]["steps"][-1]
            self.assertEqual(step["size"], size)
            self.assertEqual(step["operation"], "search")
            self.assertTrue(step["search"]["complete"])
            self.assertFalse(step["complete"])
            self.assertIn("UNIQUE-MARKER", step["fragments"][0]["text"])
            return self.ranges(first=2, last=2)

        with conversation_state.recording() as sources:
            answer = self.run_flow([self.search(), followup, ANSWER_PLAN])
        self.assertEqual(answer, ANSWER)
        self.assertEqual(self.confirm.call_count, 1)
        self.assertEqual(list(sources), [str(path)])
        final = self.wire[-1]["discovery"]["steps"][-1]
        self.assertEqual(final["fragments"][0]["text"], "UNIQUE-MARKER verified=yes\n")
        self.assertFalse(final["complete"])
        self.assertLess(sum(len(json.dumps(request).encode()) for request in self.wire), 65536)

    def test_selected_excerpts_survive_into_the_final_answer_request(self):
        self.file("large.log", "one\ntwo\nthree\n")
        self.file("other.txt", "additional facts\n")
        final_read = json.dumps({"action": "read", "files": [sel("other.txt")], "systemd": [], "weather": None})
        answer = self.run_flow([self.ranges(first=2, last=2), final_read, ANSWER], question="Read large.log and other.txt")
        self.assertEqual(answer, ANSWER)
        request = self.wire[-1]
        self.assertEqual(request["mode"], "question")
        self.assertEqual(request["discovery"]["steps"][0]["fragments"][0]["text"], "two\n")
        self.assertEqual(request["context"][0]["files"][0]["content"], "additional facts\n")

    def test_exact_file_scope_is_usable_without_guessing_a_relative_path(self):
        self.file("large.log", "complete file\n")
        text = self.config.read_text().replace('read = ["~/.config/**"]', 'read = ["~/.config/large.log"]')
        self.config.write_text(text)
        request = json.dumps({"action": "read-range", "file": "f0", "first": 1, "last": 10})
        self.run_flow([request, ANSWER_PLAN], question="Read the available file")
        step = self.wire[-1]["discovery"]["steps"][-1]
        self.assertEqual(step["path"], "")
        self.assertTrue(step["complete"])
        self.assertEqual(step["fragments"][0]["text"], "complete file\n")

    def test_allowed_directory_alias_records_both_spellings_and_detects_retargeting(self):
        relative = "GizmoSuite/panels.json"
        with conversation_state.recording() as sources:
            self.run_flow([self.ranges(relative), ANSWER_PLAN], question="Read " + relative)
        self.assertEqual(set(sources), {str(self.home / ".config/GizmoSuite/panels.json"),
                                       str(self.home / ".config/gizmo-suite/panels.json")})

        def retarget(number, kwargs):
            if number == 2:
                alias = self.home / ".config/GizmoSuite"
                alias.unlink()
                alias.symlink_to("gizmo")

        with self.assertRaises(wire.AnalysisDenied):
            self.run_flow([self.ranges(relative), ANSWER_PLAN], question="Read " + relative, before=retarget)
        self.assertEqual(len(self.wire), 1)

    def test_denied_path_is_not_opened_and_no_excerpt_leaves(self):
        denied = self.file("large.log", "PRIVATE-DENIED")
        self.write_policy(deny=[str(denied)])
        with self.assertRaises(flow.file_context.FileReadDenied):
            self.run_flow([self.ranges()])
        self.assertNotIn(str(denied), self.reads)
        self.assertEqual(len(self.wire), 1)

    def test_symlink_and_hardlink_cannot_be_read_as_content(self):
        source = self.file("source.log", "UNIQUE-MARKER")
        link = self.home / ".config/large.log"
        os.symlink(source, link)
        # A recoverable symlink refusal can be described, but has no content.
        self.run_flow([self.ranges(), ANSWER_PLAN])
        self.assertEqual(self.wire[-1]["discovery"]["steps"][-1]["op"], "refusal")
        link.unlink()
        os.link(source, link)
        with self.assertRaises(flow.file_context.FileReadDenied):
            self.run_flow([self.ranges()])
        self.assertEqual(len(self.wire), 1)

    def test_source_change_after_read_blocks_the_next_handoff(self):
        path = self.file("large.log", "original\n")

        def mutate(number, kwargs):
            if number == 2:
                path.write_text("changed\n")

        with self.assertRaises(wire.AnalysisDenied):
            self.run_flow([self.ranges(), ANSWER_PLAN], before=mutate)
        self.assertEqual(len(self.wire), 1)

    def test_unobserved_path_requires_discovery_and_cannot_be_guessed(self):
        self.file("large.log", "UNIQUE-MARKER")
        self.run_flow([self.ranges(), ANSWER_PLAN], question="Find the missing setting")
        refusal = self.wire[-1]["discovery"]["steps"][-1]
        self.assertEqual(refusal["code"], "discovery-required")
        self.assertNotIn("UNIQUE-MARKER", json.dumps(self.wire))

    def test_owner_excerpt_budget_is_not_bypassed_by_search(self):
        self.file("large.log", "x" * 2048 + "UNIQUE-MARKER" + "x" * 2048)
        self.write_policy('read_file_size = "1KiB"\nread_total_size = "1KiB"\n')
        # A search snippet fits, but its repeated copies still consume the
        # same per-question excerpt budget.
        self.run_flow([self.search(), self.search(), ANSWER_PLAN])
        steps = self.wire[-1]["discovery"]["steps"]
        self.assertEqual(steps[0]["op"], "content")
        self.assertEqual(steps[1]["op"], "refusal")
        self.assertEqual(steps[1]["code"], "files-total")

    def test_search_pagination_and_multibyte_fragment_hashes_are_exact(self):
        self.file("large.log", ("Zażółć UNIQUE-MARKER\n" * 25))

        def next_page(request):
            step = request["discovery"]["steps"][-1]
            self.assertFalse(step["search"]["complete"])
            self.assertEqual(len(step["fragments"]), 20)
            for fragment in step["fragments"]:
                self.assertEqual(ask_content._hash(fragment["text"].encode()), fragment["sha256"])
            return self.search(cursor=step["search"]["next"])

        self.run_flow([self.search(), next_page, ANSWER_PLAN])
        step = self.wire[-1]["discovery"]["steps"][-1]
        self.assertTrue(step["search"]["complete"])
        self.assertEqual(len(step["fragments"]), 5)

    def test_worker_refuses_forged_fragment_hash_or_complete_flag(self):
        self.file("large.log", "one\ntwo\n")
        self.run_flow([self.ranges(first=2, last=2), ANSWER_PLAN])
        document = self.wire[-1]["discovery"]
        for field in ("hash", "complete", "unknown"):
            changed = copy.deepcopy(document)
            step = changed["steps"][0]
            if field == "hash":
                step["fragments"][0]["sha256"] = "0" * 64
            elif field == "complete":
                step["complete"] = True
            else:
                step["run"] = "arbitrary command"
            with self.subTest(field=field), self.assertRaises(ValueError):
                discovery_contract.validate_discovery(changed)

    def test_plan_rejects_directory_wildcard_invalid_cursor_and_unknown_fields(self):
        access = permissions.load_policy().access()
        valid = json.loads(self.search())
        for update in ({"file": "f0"}, {"file": sel("large/**")}, {"cursor": -1}, {"cursor": True},
                       {"needle": ""}, {"needle": "a\nb"}, {"command": "anything"}):
            with self.subTest(update=update), self.assertRaises(assistant_contract.PlanError):
                assistant_contract.parse_plan(json.dumps({**valid, **update}), access)
        for first, last in ((True, 1), (0, 1), (2, 1)):
            with self.assertRaises(assistant_contract.PlanError):
                assistant_contract.parse_plan(self.ranges(first=first, last=last), access)
