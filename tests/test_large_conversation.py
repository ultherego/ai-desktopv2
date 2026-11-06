"""Bounded long conversations, real saved records, and source revocation after resume."""
import json
from pathlib import Path
import pwd
import os
import tempfile
import unittest

from ai_desktop import conversation_memory as memory
from ai_desktop import conversation_sessions as sessions
from ai_desktop import conversation_state as state
from ai_desktop import execution


class LargeConversationTests(unittest.TestCase):
    def store(self):
        home = self.enterContext(tempfile.TemporaryDirectory(dir=pwd.getpwuid(os.geteuid()).pw_dir))
        return sessions.Store(Path(home))

    def test_long_conversation_keeps_recent_sixty_four_turns_and_original_goal(self):
        talk = state.Conversation()
        for turn in range(150):
            talk.finish("Original goal" if turn == 0 else "Question %d" % turn,
                        "Answer %d" % turn, None, ())
        self.assertEqual(len(talk.turns), 128)
        history = execution.validate_history(talk.history())
        self.assertEqual(len(history), 64)
        self.assertEqual(history[0]["question"], "Question 86")
        self.assertEqual(history[-1]["answer"], "Answer 149")
        self.assertIn("Original goal", [entry["text"] for entry in talk.task_memory()["entries"]])
        self.assertGreater(talk.task_memory()["dropped"], 0)
        self.assertEqual(state.export(talk)["title"], "Original goal")

    def test_unicode_history_is_bounded_by_bytes_and_newest_turn_is_whole(self):
        talk = state.Conversation()
        question, answer = "ą" * 4096, "🚀" * 4096
        for unused in range(80):
            talk.finish(question, answer, None, ())
        history = execution.validate_history(talk.history())
        self.assertEqual(len(history), execution.HISTORY_TOTAL_BYTES // (8192 + 16384))
        self.assertEqual(history[-1], {"question": question, "answer": answer})
        self.assertLessEqual(sum(len(pair[key].encode()) for pair in history for key in pair),
                             execution.HISTORY_TOTAL_BYTES)

    def test_new_baselines_cannot_evict_the_active_goal(self):
        talk = state.Conversation()
        talk.finish("Original goal", "Recorded", None, ())
        for number in range(memory.MAX_ENTRIES + 8):
            ops = memory.parse_ops(json.dumps({"add": [{"kind": "baseline", "text": "Baseline %d" % number}]}))
            talk.finish("Next", "Recorded", ops, ())
        self.assertIn("Original goal", [entry["text"] for entry in talk.task_memory()["entries"]])
        self.assertGreater(talk.task_memory()["dropped"], 0)

    def test_wire_validator_enforces_aggregate_limit_even_for_individually_valid_turns(self):
        turns = [{"question": "q" * 4096, "answer": "a" * 4096} for unused in range(64)]
        self.assertEqual(execution.validate_history(turns), turns)
        turns[-1]["answer"] += "ą"
        with self.assertRaisesRegex(ValueError, "conversation"):
            execution.validate_history(turns)

    def test_large_saved_archive_is_separate_from_bounded_request_window(self):
        talk = state.Conversation()
        question, answer = "q" * (128 * 1024), "a" * (32 * 1024)
        for unused in range(64):
            talk.finish(question, answer, None, ())
        store = self.store()
        with store.create("ask") as session:
            session.save_state(state.export(talk))
            ident = session.session_id
        saved = store.read(ident)
        self.assertEqual(len(saved["history"]), 64)
        self.assertGreater((store.path / (ident + ".json")).stat().st_size, 1024 * 1024)
        self.assertLessEqual((store.path / (ident + ".json")).stat().st_size, sessions.MAX_FILE)
        restored = state.load(saved)
        self.assertEqual(restored.history()[-1], {"question": question, "answer": answer})
        self.assertEqual(len(execution.validate_history(restored.history())), 3)
        summaries = store.summaries()
        self.assertEqual(summaries[0]["history_count"], 64)
        self.assertNotIn("history", summaries[0])
        self.assertNotIn("memory", summaries[0])
        self.assertLess(len(json.dumps(summaries).encode()), 1024)

    def test_two_thousand_sources_survive_resume_then_revoke_answers_and_derived_notes(self):
        paths = ["/synthetic/file-%d.txt" % number for number in range(2048)]
        talk = state.Conversation()
        ops = memory.parse_ops(json.dumps({"add": [{"kind": "observation", "text": "private-marker"}]}))
        talk.finish("Review project", "private-marker", ops, paths)
        talk.finish("Continue", "dependent-marker", None, ())
        store = self.store()
        with store.create("ask") as session:
            session.save_state(state.export(talk))
            ident = session.session_id
        with store.open(ident, "ask") as session:
            restored = state.load(session.state())
            self.assertEqual(set(restored.turns[-1].taint), set(paths))
            restored.revalidate(lambda path: path != paths[1024])
            session.save_state(state.export(restored))
        serialized = json.dumps(store.read(ident))
        self.assertNotIn("private-marker", serialized)
        self.assertNotIn("dependent-marker", serialized)
        self.assertIn("Review project", serialized)

    def test_source_overflow_omits_complete_dependencies_and_counts_omitted_notes(self):
        talk = state.Conversation()
        ops = memory.parse_ops(json.dumps({"add": [{"kind": "observation", "text": "private-marker"}]}))
        talk.finish("Goal", "private-marker", ops,
                    ["/synthetic/%d" % index for index in range(state.MAX_SOURCE_PATHS + 1)])
        exported = state.export(talk)
        self.assertNotIn("private-marker", json.dumps(exported))
        self.assertGreater(exported["memory"]["dropped"], 0)
        self.assertTrue(exported["turns"][0]["redacted"])

    def test_turn_ten_thousand_resumes_and_does_not_reuse_memory_ids(self):
        talk = state.Conversation()
        talk.number = 9998
        talk.finish("Initial goal", "Answer", None, ())
        talk.finish("New finding", "Answer", None, ())
        store = self.store()
        with store.create("ask") as session:
            session.save_state(state.export(talk))
            ident = session.session_id
        restored = state.load(store.read(ident))
        restored.finish("Continue", "Answer", None, ())
        self.assertEqual(restored.number, 10001)
        entries = memory.validate(restored.memory)["entries"]
        self.assertEqual(len({entry["id"] for entry in entries}), len(entries))
        # Even a record whose verbatim history was dropped must retain its monotonic note counter.
        memory_only = state.export(restored)
        memory_only.update(history=[], turns=[])
        self.assertEqual(state.load(memory_only).number, 10001)


if __name__ == "__main__":
    unittest.main()
