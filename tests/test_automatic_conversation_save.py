"""Automatic ask checkpoints: local-only opt-out, atomic first publication and bounded failure handling."""
import copy
import io
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import Mock, patch

os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")

from ai_desktop import ask_cli as ask, conversation_sessions as sessions, conversation_state


PROFILE = {"provider": "claude", "model": "default", "effort": "default", "instructions": "",
           "profile": "default", "access": "changes"}


class AtomicFirstCheckpointTests(unittest.TestCase):
    def setUp(self):
        actual = pwd.getpwuid(os.geteuid()).pw_dir
        self.temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="aid-autosave-", dir=actual))
        self.store = sessions.Store(Path(self.temporary))

    @staticmethod
    def exported(question="First question", answer="First answer"):
        talk = conversation_state.Conversation()
        talk.finish(question, answer, None, ())
        return conversation_state.export(talk)

    def test_first_ask_checkpoint_is_the_initial_published_record(self):
        exported = self.exported()
        conversation = self.store.create("ask", execution_config=PROFILE, initial_state=exported)
        self.addCleanup(conversation.close)
        record = self.store.read(conversation.session_id)
        self.assertEqual(record["revision"], 0)
        self.assertEqual(record["history"], exported["history"])
        self.assertEqual(record["memory"], exported["memory"])
        self.assertEqual(record["title"], "First question")

    def test_failed_first_publication_leaves_no_conversation_record(self):
        with patch.object(self.store, "_publish", side_effect=sessions.SessionError("session-file")):
            with self.assertRaises(sessions.SessionError):
                self.store.create("ask", execution_config=PROFILE, initial_state=self.exported())
        self.assertEqual(self.store.list(), [])
        self.assertFalse(any(path.suffix == ".json" for path in self.store.path.iterdir()))

    def test_initial_state_is_complete_ask_state_only(self):
        for kind, state in (("material", self.exported()), ("ask", {"history": []}), ("ask", [])):
            with self.subTest(kind=kind, state=type(state).__name__), self.assertRaises(sessions.SessionError):
                self.store.create(kind, str(Path(self.temporary) / "x") if kind != "ask" else "",
                                  "a" * 64 if kind != "ask" else "", PROFILE, initial_state=state)
        self.assertEqual(self.store.list(), [])


class CheckpointContentTests(AtomicFirstCheckpointTests):
    def test_line_and_paragraph_separators_do_not_stop_saving(self):
        exported = self.exported("Question\u2028second line", "Answer\u2029next paragraph")
        conversation = self.store.create("ask", execution_config=PROFILE, initial_state=exported)
        self.addCleanup(conversation.close)
        record = self.store.read(conversation.session_id)
        self.assertEqual(record["history"], [{"question": "Question\nsecond line", "answer": "Answer\nnext paragraph"}])

    def test_leftovers_of_a_killed_writer_are_removed_on_the_next_save(self):
        conversation = self.store.create("ask", execution_config=PROFILE, initial_state=self.exported())
        self.addCleanup(conversation.close)
        root = Path(self.store.path)
        record = root / (conversation.session_id + ".json")
        # Killed before publishing: private content in a temporary file. Killed between link and unlink: a second link.
        unpublished = root / ".new-0123456789abcdef0123456789abcdef"
        unpublished.write_text('{"history":[{"question":"SYNTHETIC SECRET"}]}')
        unpublished.chmod(0o600)
        linked = root / ".new-fedcba9876543210fedcba9876543210"
        os.link(record, linked)
        self.assertEqual(record.stat().st_nlink, 2)
        talk = conversation_state.Conversation()
        talk.finish("First question", "First answer", None, ())
        talk.finish("Second question", "Second answer", None, ())
        conversation.save_state(conversation_state.export(talk))
        self.assertEqual(sorted(path.name for path in root.iterdir() if path.name.startswith(".new-")), [])
        self.assertEqual(record.stat().st_nlink, 1)
        self.assertEqual(len(self.store.read(conversation.session_id)["history"]), 2)
        self.assertEqual(self.store.damaged(), [])


class FakeConversation:
    def __init__(self, state):
        self.session_id = "a" * 24
        self.saved = copy.deepcopy(state)
        self.save_calls = 0
        self.closed = False

    def state(self):
        return copy.deepcopy(self.saved)

    def save_state(self, state):
        self.save_calls += 1
        self.saved = copy.deepcopy(state)

    def close(self):
        self.closed = True


class FakeStore:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []
        self.conversation = None

    def create(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.failure is not None:
            raise self.failure
        self.conversation = FakeConversation(kwargs["initial_state"])
        return self.conversation


class AutomaticAskSaveTests(unittest.TestCase):
    def run_ask(self, argv, messages, answers, *, store=None):
        output = io.StringIO()
        output.isatty = lambda: True
        store = store or FakeStore()
        chooser = Mock(return_value=("new", None))
        presence = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch("ai_desktop.agent_profiles.resolve", return_value=PROFILE))
            stack.enter_context(patch.object(ask.os, "geteuid", return_value=1000))
            stack.enter_context(patch.object(ask.os, "getuid", return_value=1000))
            stack.enter_context(patch.object(ask.sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch.object(ask, "Presence", return_value=presence))
            stack.enter_context(patch.object(ask, "_choose_thread", chooser))
            stack.enter_context(patch.object(ask, "_read_message", side_effect=list(messages)))
            flow = stack.enter_context(patch.object(ask.assistant_flow, "run",
                                                       side_effect=answers if callable(answers) else list(answers)))
            store_type = stack.enter_context(patch("ai_desktop.conversation_sessions.Store", return_value=store))
            stack.enter_context(redirect_stdout(output))
            code = ask.main(argv, reader=input)
        return code, output.getvalue(), store, chooser, store_type, flow, presence

    def test_default_interactive_ask_saves_first_turn_atomically_and_updates_later(self):
        code, output, store, chooser, _, flow, presence = self.run_ask(
            [], ["First", "Second", ""], ["Answer one", "Answer two"])
        self.assertEqual(code, 0, output)
        chooser.assert_called_once_with()
        self.assertEqual(flow.call_count, 2)
        self.assertEqual(len(store.calls), 1)
        args, kwargs = store.calls[0]
        self.assertEqual(args[:3], ("ask", "", ""))
        self.assertIsNone(args[3])  # unchanged default Claude wire is stored as the legacy default binding
        self.assertEqual(kwargs["initial_state"]["history"],
                         [{"question": "First", "answer": "Answer one"}])
        self.assertEqual(store.conversation.save_calls, 1)
        self.assertEqual([item["question"] for item in store.conversation.saved["history"]], ["First", "Second"])
        self.assertTrue(store.conversation.closed)
        self.assertIn("saved locally", output)
        presence.start.assert_called_once_with()
        presence.close.assert_called_once_with()

    def test_no_save_never_lists_or_writes_threads(self):
        code, output, store, chooser, store_type, flow, _ = self.run_ask(
            ["--no-save"], ["Question", ""], ["Answer"])
        self.assertEqual(code, 0, output)
        self.assertEqual(flow.call_count, 1)
        chooser.assert_not_called()
        store_type.assert_not_called()
        self.assertEqual(store.calls, [])
        self.assertNotIn("saved locally", output)

    def test_empty_or_interrupted_start_creates_no_record(self):
        store = FakeStore()
        code, _, store, _, store_type, flow, _ = self.run_ask([], [KeyboardInterrupt()], [], store=store)
        self.assertEqual(code, 2)
        self.assertEqual(flow.call_count, 0)
        store_type.assert_not_called()
        self.assertEqual(store.calls, [])

    def test_one_shot_question_is_not_saved_implicitly(self):
        code, output, store, chooser, store_type, flow, _ = self.run_ask(
            ["--question", "One shot"], [], ["Answer"])
        self.assertEqual(code, 0, output)
        self.assertEqual(flow.call_count, 1)
        chooser.assert_not_called()
        store_type.assert_not_called()
        self.assertEqual(store.calls, [])

    def test_failed_automatic_create_is_reported_once_and_the_conversation_continues(self):
        store = FakeStore(sessions.SessionError("session-file"))
        code, output, store, _, _, flow, _ = self.run_ask(
            [], ["First", "Second", ""], ["Answer one", "Answer two"], store=store)
        self.assertEqual(code, 0, output)
        self.assertEqual(flow.call_count, 2)
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(output.count("Could not start automatic local saving"), 1)

    def test_the_session_limit_says_how_to_make_room(self):
        store = FakeStore(sessions.SessionError("session-limit"))
        code, output, store, _, _, flow, _ = self.run_ask([], ["First", ""], ["Answer one"], store=store)
        self.assertEqual(code, 0, output)
        self.assertIn("you already have %d saved conversations" % sessions.MAX_SESSIONS, output)
        self.assertIn("ai-desktop sessions", output)
        self.assertNotIn("may be full", output)

    def test_revoked_content_leaves_the_saved_copy_before_the_next_request(self):
        talk = conversation_state.Conversation()
        talk.finish("Read the file", "SYNTHETIC FILE CONTENT", None, ("/home/example/notes.txt",))
        conversation = FakeConversation(conversation_state.export(talk))
        snapshot = Mock(policy=Mock(allows=lambda path: False))
        with redirect_stdout(io.StringIO()) as output, ask._turn_context(talk, snapshot, conversation):
            # The checkpoint is written before anything is sent, so a failed turn or an exit cannot keep it on disk.
            self.assertEqual(conversation.save_calls, 1)
        self.assertNotIn("SYNTHETIC FILE CONTENT", str(conversation.saved))
        self.assertIn("revoked", output.getvalue())

    def test_a_failed_redaction_save_tells_how_to_remove_the_copy(self):
        talk = conversation_state.Conversation()
        talk.finish("Read the file", "SYNTHETIC FILE CONTENT", None, ("/home/example/notes.txt",))
        conversation = Mock(save_state=Mock(side_effect=sessions.SessionError("session-file")))
        snapshot = Mock(policy=Mock(allows=lambda path: False))
        with redirect_stdout(io.StringIO()) as output, ask._turn_context(talk, snapshot, conversation):
            pass
        self.assertIn("Could not remove it from the saved conversation", output.getvalue())

    def test_actions_completed_before_a_service_failure_are_checkpointed(self):
        def fail_after_action(*unused_args, **unused_kwargs):
            conversation_state.note_action("Command uptime: ok, code 0")
            raise ask.AnalysisDenied("network")

        code, output, store, _, _, flow, _ = self.run_ask(
            [], ["Run the check", ""], fail_after_action)
        self.assertEqual(code, 2, output)
        self.assertEqual(flow.call_count, 1)
        self.assertEqual(len(store.calls), 1)
        state = store.calls[0][1]["initial_state"]
        self.assertEqual(state["history"][0]["question"], "Run the check")
        self.assertIn("answer did not arrive", state["history"][0]["answer"])
        self.assertTrue(any("Command uptime: ok" in entry["text"] for entry in state["memory"]["entries"]))

    def test_no_save_and_explicit_session_are_rejected_before_store_access(self):
        code, output, store, chooser, store_type, flow, _ = self.run_ask(
            ["--no-save", "--session", "new"], [], [], store=FakeStore())
        self.assertEqual(code, 2)
        self.assertIn("cannot be combined", output)
        chooser.assert_not_called()
        store_type.assert_not_called()
        self.assertEqual(flow.call_count, 0)

    def test_injected_reader_remains_non_persistent(self):
        output = io.StringIO()
        output.isatty = lambda: True
        queue = ["Question", "yes", ""]
        store = FakeStore()
        with ExitStack() as stack:
            stack.enter_context(patch("ai_desktop.agent_profiles.resolve", return_value=PROFILE))
            stack.enter_context(patch.object(ask.os, "geteuid", return_value=1000))
            stack.enter_context(patch.object(ask.os, "getuid", return_value=1000))
            stack.enter_context(patch.object(ask.sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch.object(ask.assistant_flow, "run", return_value="Answer"))
            store_type = stack.enter_context(patch("ai_desktop.conversation_sessions.Store", return_value=store))
            stack.enter_context(redirect_stdout(output))
            code = ask.main([], reader=lambda prompt: queue.pop(0))
        self.assertEqual(code, 0, output.getvalue())
        store_type.assert_not_called()
        self.assertEqual(store.calls, [])


if __name__ == "__main__":
    unittest.main()
