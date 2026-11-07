"""The whole saved thread and `ai-desktop -c`: every turn is kept for reading back, apart from the bounded request window."""
import io
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import Mock, patch

os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")

from ai_desktop import ask_cli as ask, cli, conversation_sessions as sessions, conversation_state, session_cli
from ai_desktop.execution import MAX_HISTORY

PROFILE = {"provider": "claude", "model": "default", "effort": "default", "instructions": "",
           "profile": "default", "access": "changes"}


def long_conversation(turns=200):
    talk = conversation_state.Conversation()
    for number in range(1, turns + 1):
        talk.finish("SYNTHETIC question %d" % number, "SYNTHETIC answer %d" % number, None, ())
    return talk


class StoreCase(unittest.TestCase):
    def setUp(self):
        actual = pwd.getpwuid(os.geteuid()).pw_dir
        self.temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="aid-thread-", dir=actual))
        self.store = sessions.Store(Path(self.temporary))


class WholeThreadTests(StoreCase):
    def test_two_hundred_turns_survive_a_save_and_reload_while_requests_stay_bounded(self):
        talk = long_conversation(200)
        conversation = self.store.create("ask", execution_config=PROFILE, initial_state=conversation_state.export(talk))
        self.addCleanup(conversation.close)
        loaded = conversation_state.load(self.store.read(conversation.session_id))
        self.assertEqual(len(loaded.transcript), 200)
        self.assertEqual(loaded.transcript[0]["question"], "SYNTHETIC question 1")
        self.assertEqual(loaded.transcript[-1]["answer"], "SYNTHETIC answer 200")
        self.assertLessEqual(len(loaded.history()), MAX_HISTORY)
        self.assertNotIn("\"SYNTHETIC question 1\"", json.dumps(loaded.history()))
        self.assertEqual(loaded.number, 200)
        loaded.finish("SYNTHETIC question 201", "SYNTHETIC answer 201", None, ())
        self.assertEqual([entry["n"] for entry in loaded.transcript[-2:]], [200, 201])

    def test_turns_still_in_the_request_window_are_not_written_twice(self):
        talk = long_conversation(80)
        exported = conversation_state.export(talk)
        in_window = [item for item in exported["transcript"] if item["window"]]
        self.assertEqual(len(in_window), len(exported["history"]))
        self.assertTrue(all(item["question"] == item["answer"] == "" for item in in_window))
        self.assertEqual(json.dumps(exported).count("SYNTHETIC answer 80"), 1)
        loaded = conversation_state.load(exported)
        self.assertEqual([entry["answer"] for entry in loaded.transcript],
                         ["SYNTHETIC answer %d" % number for number in range(1, 81)])

    def test_revoking_a_source_withholds_an_old_answer_outside_the_request_window(self):
        talk = conversation_state.Conversation()
        talk.finish("Read the notes", "SYNTHETIC SECRET FROM NOTES", None, ("/home/example/notes.txt",))
        for number in range(150):
            talk.turns.clear() if number == 0 else None    # the request window has long forgotten turn 1
            talk.finish("SYNTHETIC later %d" % number, "SYNTHETIC answer", None, ())
        self.assertFalse(any(turn.n == 1 for turn in talk.turns))
        removed = talk.revalidate(lambda path: path != "/home/example/notes.txt")
        self.assertGreaterEqual(removed, 1)
        exported = json.dumps(conversation_state.export(talk))
        self.assertNotIn("SYNTHETIC SECRET FROM NOTES", exported)
        self.assertTrue(talk.transcript[0]["redacted"])
        self.assertEqual(talk.transcript[0]["question"], "Read the notes")

    def test_an_automatic_next_step_is_shown_as_such_but_the_model_still_sees_the_program_text(self):
        talk = conversation_state.Conversation()
        talk.finish("Delete a.py", "Moved to trash", None, ())
        question = ask.CONTINUE_PREFIX + "Delete a.py"
        talk.finish(question, "Done", None, (), automatic=True, shown="Delete a.py")
        self.assertEqual(talk.turns[-1].question, question)
        self.assertEqual(talk.transcript[-1]["question"], "Delete a.py")
        self.assertTrue(talk.transcript[-1]["automatic"])
        output = io.StringIO()
        with redirect_stdout(output):
            session_cli.print_thread(talk.transcript, output_format="plain")
        self.assertIn("> Delete a.py", output.getvalue())
        self.assertIn("started automatically", output.getvalue())
        self.assertNotIn(ask.CONTINUE_PREFIX, output.getvalue())

    def test_a_record_without_a_thread_shows_its_request_window(self):
        legacy = {"history": [{"question": "Old question", "answer": "Old answer"}]}
        loaded = conversation_state.load(legacy)
        self.assertEqual([(e["question"], e["redacted"]) for e in loaded.transcript], [("Old question", True)])

    def test_the_record_refuses_a_malformed_thread(self):
        exported = conversation_state.export(long_conversation(2))
        for change in (lambda t: t[0].pop("automatic"), lambda t: t[0].update(taint=[99]),
                       lambda t: t[0].update(redacted=True, taint=[0]) if exported["paths"] else t[0].update(n=0),
                       lambda t: t[0].update(answer="bad text")):
            broken = json.loads(json.dumps(exported))
            change(broken["transcript"])
            with self.subTest(change=change), self.assertRaises(sessions.SessionError):
                self.store.create("ask", execution_config=PROFILE, initial_state=broken)

    def test_a_record_that_grows_too_large_drops_the_oldest_read_back_turns_first(self):
        talk = conversation_state.Conversation()
        for number in range(100):
            talk.finish("SYNTHETIC question %d" % number, ("answer %d " % number) * 2000, None, ())
        with patch.object(sessions, "MAX_FILE", 1700 * 1024):
            conversation = self.store.create("ask", execution_config=PROFILE,
                                             initial_state=conversation_state.export(talk))
            self.addCleanup(conversation.close)
            record = self.store.read(conversation.session_id)
        self.assertLess(len(record["transcript"]), 100)
        self.assertGreater(len(record["transcript"]), MAX_HISTORY)
        self.assertEqual(conversation_state.load(record).transcript[-1]["question"], "SYNTHETIC question 99")
        self.assertEqual(len(record["history"]), MAX_HISTORY)


class ContinueTests(StoreCase):
    def make(self, question, kind="ask"):
        talk = conversation_state.Conversation()
        talk.finish(question, "SYNTHETIC answer", None, ())
        conversation = self.store.create(kind, execution_config=PROFILE, initial_state=conversation_state.export(talk))
        conversation.close()
        return conversation.session_id

    def test_continue_resumes_the_most_recently_updated_conversation(self):
        older = self.make("Older")
        newer = self.make("Newer")
        record = self.store.read(older)
        record["history"].append({"question": "Later", "answer": "SYNTHETIC answer"})
        record["turns"].append({"n": 2, "taint": [], "redacted": False})
        with patch.object(sessions.time, "time", return_value=record["updated"] + 100):
            self.store.update(record, record["revision"])
        dispatch = Mock(return_value=0)
        self.assertEqual(session_cli.continue_latest(store=self.store, dispatch=dispatch), 0)
        argv = dispatch.call_args.args[0]
        self.assertEqual(argv[:3], ["ask", "--session", older])
        self.assertNotIn(newer, argv)

    def test_continue_without_saved_conversations_starts_a_new_one(self):
        dispatch = Mock(return_value=0)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(session_cli.continue_latest(store=self.store, dispatch=dispatch), 0)
        dispatch.assert_called_once_with(["ask"])
        self.assertIn("No saved conversation yet", output.getvalue())

    def test_dash_c_takes_no_other_options(self):
        for argv in (["-c", "--agent", "x"], ["ask", "-c", "question"]):
            with self.subTest(argv=argv), patch.object(session_cli, "continue_latest") as resumed, \
                    redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
                self.assertEqual(cli.main(argv), 2)
                resumed.assert_not_called()

    def test_dash_c_routes_to_continue(self):
        for argv, kind in ((["-c"], None), (["--continue"], None), (["ask", "-c"], "ask")):
            with self.subTest(argv=argv), patch.object(session_cli, "continue_latest", return_value=0) as resumed:
                self.assertEqual(cli.main(argv), 0)
                self.assertEqual(resumed.call_args.args, (kind,))


class ResumedAskShowsTheThreadTests(unittest.TestCase):
    def test_a_resumed_conversation_prints_its_whole_thread_before_the_prompt(self):
        state = conversation_state.export(long_conversation(70))
        conversation = Mock(session_id="a" * 24, state=Mock(return_value=state))
        output = io.StringIO()
        output.isatty = lambda: True
        with ExitStack() as stack:
            stack.enter_context(patch("ai_desktop.agent_profiles.resolve", return_value=PROFILE))
            stack.enter_context(patch.object(ask.os, "geteuid", return_value=1000))
            stack.enter_context(patch.object(ask.os, "getuid", return_value=1000))
            stack.enter_context(patch.object(ask.sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch.object(ask, "Presence", return_value=Mock()))
            stack.enter_context(patch.object(ask, "_read_message", side_effect=[KeyboardInterrupt()]))
            stack.enter_context(patch("ai_desktop.conversation_sessions.load_session", return_value=conversation))
            stack.enter_context(redirect_stdout(output))
            ask.main(["--session", "a" * 24, "--format", "plain"], reader=input)
        text = output.getvalue()
        self.assertIn("Earlier in this conversation (70 turns)", text)
        self.assertIn("> SYNTHETIC question 1\n", text)
        self.assertIn("SYNTHETIC answer 70", text)


if __name__ == "__main__":
    unittest.main()
