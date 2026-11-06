"""History sent on resume and saved again after permissions change."""
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest

from ai_desktop import analysis_contract, conversation_sessions, conversation_state, execution


class SessionMigrationTests(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory(dir=pwd.getpwuid(os.geteuid()).pw_dir))
        self.store = conversation_sessions.Store(Path(home))

    def test_legacy_answers_with_untracked_sources_never_enter_the_next_request_or_new_record(self):
        question = "Which setting did I ask about?"
        old_answer = "Synthetic private file excerpt"
        with self.store.create("ask") as legacy:
            legacy.append(question, old_answer)
            session_id = legacy.session_id

        with self.store.open(session_id, "ask") as resumed:
            talk = conversation_state.load(resumed.state())
            with execution.using_history(talk.history()):
                request = analysis_contract.application_input(None, "Continue", context=[])
            self.assertNotIn(old_answer, request)
            self.assertEqual(json.loads(request)["conversation"][0]["question"], question)
            talk.finish("Continue", "New answer", None, ())
            resumed.save_state(conversation_state.export(talk))

        saved = self.store.read(session_id)
        self.assertNotIn(old_answer, json.dumps(saved))
        self.assertEqual(conversation_state.load(saved).history(), [
            {"question": question, "answer": conversation_state.REDACTED},
            {"question": "Continue", "answer": "New answer"},
        ])

    def test_after_256_source_revocations_a_new_allowed_source_can_be_saved_and_revoked(self):
        talk = conversation_state.Conversation()
        for batch in range(4):
            paths = ["/synthetic/batch-%d/%d" % (batch, index) for index in range(64)]
            talk.finish("Read batch %d" % batch, "Private batch result", None, paths)
            talk.revalidate(lambda path: False)
        allowed = "/synthetic/current"
        talk.finish("Read current file", "Current result", None, [allowed])

        with self.store.create("ask") as saved:
            saved.save_state(conversation_state.export(talk))
            session_id = saved.session_id

        with self.store.open(session_id, "ask") as resumed:
            state = resumed.state()
            self.assertEqual(state["paths"], [allowed])
            self.assertNotIn("Private batch result", json.dumps(state))
            loaded = conversation_state.load(state)
            self.assertEqual(loaded.history()[-1]["answer"], "Current result")
            self.assertEqual(loaded.revalidate(lambda path: False), 1)
            self.assertEqual(loaded.history()[-1]["answer"], conversation_state.REDACTED)
