"""Interactive `ask` loop: in-memory continuity, a fresh consent per turn, no persistence by default."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import ExitStack, nullcontext, redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import ask_cli as ask, assistant_flow, execution
from ai_desktop.permissions import Limits
from ai_desktop.terminal_consent import PROMPTS

EMPTY = {"files": [], "systemd": [], "weather": False}
PROFILE = {"provider": "claude", "model": "default", "effort": "default", "instructions": "",
           "profile": "default", "access": "changes"}
Q = ask.QUESTION_PROMPT
C = PROMPTS["ask-question"]


class FakeConversation:
    def __init__(self):
        self.session_id, self.saved, self.closed = "S1", {"history": []}, False

    @property
    def items(self):
        return [(t["question"], t["answer"]) for t in self.saved["history"]]

    def state(self):
        return self.saved

    def save_state(self, exported):
        self.saved = exported

    def close(self):
        self.closed = True


class ConversationLoopTests(unittest.TestCase):
    def run_ui(self, argv, replies, *, conversation=None, accesses=None):
        """`replies` is the exact reader script; an exception class is raised instead of returned."""
        out, histories, prompts, queue = io.StringIO(), [], [], list(replies)
        out.isatty = lambda: True

        def reader(prompt):
            prompts.append(prompt)
            item = queue.pop(0)
            if isinstance(item, type) and issubclass(item, BaseException):
                raise item()
            return item

        def send(approval):
            histories.append(execution.current_history())
            return json.dumps({"action": "answer", "text": "odpowiedz %d" % len(histories)})
        policy = Mock()
        policy.limits.return_value = Limits()
        snapshots = [assistant_flow.AccessSnapshot(policy, {"claude": True, "systemd": [], "weather": item["weather"]}, dict(item))
                     for item in (accesses or [EMPTY])]
        def capture():
            return snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]
        with ExitStack() as stack:
            stack.enter_context(patch("ai_desktop.agent_profiles.resolve", return_value=PROFILE))
            stack.enter_context(patch.object(ask.os, "geteuid", return_value=1000))
            stack.enter_context(patch.object(ask.os, "getuid", return_value=1000))
            stack.enter_context(patch.object(ask.sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch.object(ask, "installation_ready"))
            stack.enter_context(patch.object(assistant_flow, "capture_access", side_effect=capture))
            stack.enter_context(patch.object(ask, "managed_session", side_effect=lambda **_: nullcontext()))
            sent = stack.enter_context(patch.object(ask, "send_once", side_effect=send))
            if conversation is not None:
                stack.enter_context(patch("ai_desktop.conversation_sessions.load_session", return_value=conversation))
            stack.enter_context(redirect_stdout(out))
            code = ask.main(argv, reader=reader)
        self.assertEqual(queue, [], "unused replies")
        return SimpleNamespace(code=code, out=out.getvalue(), sent=sent, histories=histories, prompts=prompts)

    def test_prompt_returns_and_history_is_carried_under_one_consent_for_the_conversation(self):
        r = self.run_ui([], ["Chce undervolt do gry, uzywam LACT", "tak", "Jakie sa ustawienia?",
                             "Sprawdz wynik", ""])
        self.assertEqual(r.code, 0, r.out)
        self.assertEqual(r.prompts, [Q, C, Q, Q, Q])
        self.assertEqual(r.sent.call_count, 3)
        self.assertEqual(list(r.histories[0]), [])
        self.assertEqual([t["question"] for t in r.histories[1]], ["Chce undervolt do gry, uzywam LACT"])
        self.assertEqual([t["question"] for t in r.histories[2]],
                         ["Chce undervolt do gry, uzywam LACT", "Jakie sa ustawienia?"])
        self.assertEqual(r.histories[2][0]["answer"], "odpowiedz 1")
        later = json.loads(r.sent.call_args_list[2].args[0].payload)
        self.assertEqual(later["question"], "Sprawdz wynik")
        self.assertEqual(len({c.args[0].payload for c in r.sent.call_args_list}), 3)
        self.assertNotIn("Rozmowa trwa", r.out)

    def test_declining_sends_nothing_grants_nothing_and_keeps_the_conversation(self):
        r = self.run_ui([], ["Pierwsze", "nie", "Drugie", "tak", "Trzecie", ""])
        self.assertEqual(r.prompts, [Q, C, Q, C, Q, Q])                     # a no is not remembered as a yes
        self.assertEqual(r.sent.call_count, 2)
        self.assertEqual([t["question"] for t in r.histories[1]], ["Drugie"])

    def test_changed_permissions_ask_again_within_the_conversation(self):
        widened = dict(EMPTY, weather=True)
        r = self.run_ui([], ["Jeden", "tak", "Dwa", "Trzy", "tak", "Cztery", ""], accesses=[EMPTY, EMPTY, widened])
        self.assertEqual(r.prompts, [Q, C, Q, Q, C, Q, Q])
        self.assertEqual(r.sent.call_count, 4)

    def test_a_local_refusal_loses_only_that_turn(self):
        for refusal, text in ((ask.ask_edit.EditDenied("input"), "Nie zapisano zmian"),
                              (ask.FileReadDenied("input"), "")):
            with self.subTest(refusal=type(refusal).__name__), \
                    patch.object(assistant_flow, "run", side_effect=[refusal, "Dalej działam"]):
                r = self.run_ui([], ["Utworz plik", "Drugie", ""])
                self.assertEqual(r.code, 0, r.out)
                self.assertIn(text, r.out)
                self.assertIn("Dalej działam", r.out)
                self.assertEqual(r.prompts, [Q, Q, Q])

    def test_a_service_failure_keeps_the_conversation_and_is_not_retried(self):
        for reason in ("timeout", "network", "rate-limit", "provider-unavailable"):
            with self.subTest(reason=reason), \
                    patch.object(assistant_flow, "run", side_effect=[ask.AnalysisDenied(reason), "Dalej działam"]):
                r = self.run_ui([], ["Jeden", "Dwa", ""])
                self.assertEqual(r.code, 0, r.out)
                self.assertIn("Rozmowa trwa", r.out)
                self.assertIn("Dalej działam", r.out)
                self.assertEqual(r.prompts, [Q, Q, Q])

    def test_an_untrusted_failure_still_ends_the_conversation(self):
        for reason in ("cleanup", "sandbox", "identity", "consent"):
            with self.subTest(reason=reason), patch.object(assistant_flow, "run", side_effect=ask.AnalysisDenied(reason)):
                r = self.run_ui([], ["Jeden"])
                self.assertEqual(r.code, 2)
                self.assertEqual(r.prompts, [Q])

    def test_a_turn_that_cannot_be_stored_does_not_end_the_conversation(self):
        from ai_desktop.conversation_sessions import SessionError
        conv = FakeConversation()
        conv.save_state = Mock(side_effect=SessionError("x"))
        r = self.run_ui(["--session", "new"], ["Jeden", "tak", "Dwa", ""], conversation=conv)
        self.assertEqual(r.sent.call_count, 2)
        self.assertIn("Nie udało się zapisać tej tury", r.out)

    def test_an_empty_answer_does_not_break_the_next_turn(self):
        with patch.object(assistant_flow, "run", side_effect=["", "Druga"]):
            r = self.run_ui([], ["Jeden", "Dwa", ""])
        self.assertIn("Druga", r.out)

    def test_a_local_refusal_still_fails_a_one_shot_question(self):
        with patch.object(assistant_flow, "run", side_effect=ask.ask_edit.EditDenied("input")):
            r = self.run_ui(["--question", "Utworz plik"], [])
        self.assertEqual(r.code, 2)
        self.assertIn("Nie zapisano zmian", r.out)

    def test_declining_changed_permissions_sends_nothing(self):
        widened = dict(EMPTY, weather=True)
        r = self.run_ui([], ["Jeden", "tak", "Dwa", "nie", ""], accesses=[EMPTY, widened])
        self.assertEqual(r.sent.call_count, 1)

    def test_history_window_stays_at_eight_turns_and_the_conversation_does_not_end(self):
        replies = []
        for i in range(11):
            replies += ["Pytanie %d" % i] + ([] if i else ["tak"])
        r = self.run_ui([], replies + [""])
        self.assertEqual(r.sent.call_count, 11)
        self.assertEqual([t["question"] for t in r.histories[10]], ["Pytanie %d" % i for i in range(2, 10)])

    def test_exit_by_empty_line_or_double_eof_or_interrupt_is_clean(self):
        for ending in (["", ], [EOFError, EOFError], [KeyboardInterrupt],
                       [EOFError, "Dalej", EOFError, EOFError]):
            with self.subTest(ending=ending):
                r = self.run_ui([], ["Pytanie", "tak"] + ending)
                self.assertEqual(r.code, 0, r.out)
                self.assertNotIn("Anulowano", r.out)

    def test_interrupting_one_turn_returns_to_the_prompt_and_keeps_earlier_turns(self):
        calls = []
        def run_with_interrupt(argv, replies):
            out, queue = io.StringIO(), list(replies)
            out.isatty = lambda: True
            policy = Mock(); policy.limits.return_value = Limits()
            snapshot = assistant_flow.AccessSnapshot(policy, {"claude": True, "systemd": [], "weather": False}, dict(EMPTY))
            def send(approval):
                calls.append(len(execution.current_history()))
                if len(calls) == 2:
                    raise KeyboardInterrupt()
                return json.dumps({"action": "answer", "text": "ok"})
            with ExitStack() as stack:
                stack.enter_context(patch("ai_desktop.agent_profiles.resolve", return_value=PROFILE))
                stack.enter_context(patch.object(ask.os, "geteuid", return_value=1000))
                stack.enter_context(patch.object(ask.os, "getuid", return_value=1000))
                stack.enter_context(patch.object(ask.sys.stdin, "isatty", return_value=True))
                stack.enter_context(patch.object(ask, "installation_ready"))
                stack.enter_context(patch.object(assistant_flow, "capture_access", return_value=snapshot))
                stack.enter_context(patch.object(ask, "managed_session", side_effect=lambda **_: nullcontext()))
                stack.enter_context(patch.object(ask, "send_once", side_effect=send))
                stack.enter_context(redirect_stdout(out))
                return ask.main(argv, reader=lambda prompt: queue.pop(0)), out.getvalue()
        code, text = run_with_interrupt([], ["Jeden", "tak", "Dwa", "Trzy", ""])
        self.assertEqual(code, 0, text)
        self.assertEqual(calls, [0, 1, 1])             # the interrupted turn is not part of the third turn's history
        self.assertIn("Ta tura została przerwana", text)

    def test_a_failed_cleanup_after_an_interrupt_ends_the_command_with_the_real_error(self):
        from ai_desktop.analysis_contract import AnalysisDenied
        class BrokenCleanup:
            def __enter__(self):
                return self
            def __exit__(self, *unused):
                raise AnalysisDenied("session")
        out = io.StringIO(); out.isatty = lambda: True
        policy = Mock(); policy.limits.return_value = Limits()
        snapshot = assistant_flow.AccessSnapshot(policy, {"claude": True, "systemd": [], "weather": False}, dict(EMPTY))
        queue = ["Jeden", "tak", "NIE-POWINNO-BYC-CZYTANE"]
        with ExitStack() as stack:
            stack.enter_context(patch("ai_desktop.agent_profiles.resolve", return_value=PROFILE))
            stack.enter_context(patch.object(ask.os, "geteuid", return_value=1000))
            stack.enter_context(patch.object(ask.os, "getuid", return_value=1000))
            stack.enter_context(patch.object(ask.sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch.object(ask, "installation_ready"))
            stack.enter_context(patch.object(assistant_flow, "capture_access", return_value=snapshot))
            stack.enter_context(patch.object(ask, "managed_session", side_effect=lambda **_: BrokenCleanup()))
            stack.enter_context(patch.object(ask, "send_once", side_effect=KeyboardInterrupt))
            stack.enter_context(redirect_stdout(out))
            code = ask.main([], reader=lambda prompt: queue.pop(0))
        self.assertEqual(code, 2)
        self.assertEqual(queue, ["NIE-POWINNO-BYC-CZYTANE"])   # the loop did not return to the prompt

    def test_a_bad_plan_loses_only_that_turn_and_prose_is_shown_as_an_answer(self):
        replies = {1: json.dumps({"action": "read", "files": [], "systemd": [{"scope": "nope", "view": "x"}],
                                  "weather": None}),
                   2: "To jest zwykla odpowiedz prozą, a nie plan JSON.",
                   3: json.dumps({"action": "answer", "text": "trzecia"})}
        counter = []
        r = self.run_ui([], ["Jeden", "tak", "Dwa", "Trzy", ""])
        self.assertEqual(r.code, 0, r.out)
        # run_ui's stand-in always answers JSON; drive the planner replies explicitly instead:
        out = io.StringIO(); out.isatty = lambda: True
        queue = ["Jeden", "tak", "Dwa", "Trzy", ""]
        policy = Mock(); policy.limits.return_value = Limits()
        snapshot = assistant_flow.AccessSnapshot(policy, {"claude": True, "systemd": ["system"], "weather": False},
                                                 {"files": [], "systemd": ["system"], "weather": False})
        def send(approval):
            counter.append(execution.current_history())
            return replies[len(counter)]
        with ExitStack() as stack:
            stack.enter_context(patch("ai_desktop.agent_profiles.resolve", return_value=PROFILE))
            stack.enter_context(patch.object(ask.os, "geteuid", return_value=1000))
            stack.enter_context(patch.object(ask.os, "getuid", return_value=1000))
            stack.enter_context(patch.object(ask.sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch.object(ask, "installation_ready"))
            stack.enter_context(patch.object(assistant_flow, "capture_access", return_value=snapshot))
            stack.enter_context(patch.object(ask, "managed_session", side_effect=lambda **_: nullcontext()))
            stack.enter_context(patch.object(ask, "send_once", side_effect=send))
            stack.enter_context(redirect_stdout(out))
            code = ask.main([], reader=lambda prompt: queue.pop(0))
        text = out.getvalue()
        self.assertEqual(queue, [])
        self.assertEqual(len(counter), 3)                                   # the conversation went on after the bad plan
        self.assertIn("Model zwrócił niepoprawny plan", text)               # turn 1: reported, not recorded
        self.assertIn("zwykla odpowiedz prozą", text)                       # turn 2: prose shown as an answer
        self.assertEqual([t["question"] for t in counter[2]], ["Dwa"])      # turn 3 sees only the completed turn
        self.assertEqual(code, 0)

    def test_prose_is_an_answer_only_inside_the_conversation_and_never_when_it_looks_like_a_plan(self):
        from ai_desktop.assistant_contract import PlanError
        access = {"files": [], "systemd": [], "weather": False}
        with self.assertRaises(PlanError) as strict:                          # one-shot ask stays strict
            assistant_flow._plan("zwykla proza", access)
        self.assertEqual(strict.exception.check, "json")
        token = assistant_flow.lenient_prose.set(True)
        try:
            self.assertEqual(assistant_flow._plan("zwykla proza", access), {"action": "answer", "text": "zwykla proza"})
            for broken in ('{"action": "read", ', "[1, 2", '```json\n{"action": "read"}', ""):
                with self.subTest(broken=broken), self.assertRaises(PlanError):
                    assistant_flow._plan(broken, access)
        finally:
            assistant_flow.lenient_prose.reset(token)

    def test_a_plan_inside_prose_is_used_and_a_broken_one_is_never_shown_as_an_answer(self):
        from ai_desktop.assistant_contract import PlanError
        access = {"files": [{"id": "f0", "pattern": "~/.config/**"}], "systemd": [], "weather": False}
        token = assistant_flow.lenient_prose.set(True)
        try:
            mixed = 'Odkryty katalog; trzeba go wylistować.\n\n{"action":"list","files":[{"id":"f0","path":"lact"}]}'
            self.assertEqual(assistant_flow._plan(mixed, access),
                             {"action": "list", "files": [{"id": "f0", "path": "lact"}]})
            for bad in ('Zaraz: {"action":"list","files":[', 'Mój "action" to lista, ale bez JSON',
                        'Tekst {"action":"launch-missiles"}',
                        'Oto: {"action":"answer","text":"a","text":"b"}',
                        'Oto:\n```json\n{"action":"answer","text":"a"}\n```', 'Oto {"action":"answer","text":NaN}'):
                with self.subTest(bad=bad), self.assertRaises(PlanError):
                    assistant_flow._plan(bad, access)
        finally:
            assistant_flow.lenient_prose.reset(token)

    def test_question_flag_is_one_shot_and_does_not_loop(self):
        r = self.run_ui(["--question", "Co to DNS?"], ["tak"])
        self.assertEqual(r.prompts, [C])
        self.assertEqual(r.sent.call_count, 1)
        self.assertNotIn("Rozmowa trwa", r.out)

    def test_session_flag_saves_each_turn_and_feeds_history(self):
        conv = FakeConversation()
        r = self.run_ui(["--session", "new"], ["Jeden", "tak", "Dwa", ""], conversation=conv)
        self.assertEqual([q for q, _ in conv.items], ["Jeden", "Dwa"])
        self.assertEqual([t["question"] for t in r.histories[1]], ["Jeden"])
        self.assertTrue(conv.closed)
        self.assertIn("zapis lokalny", r.out)

    def test_no_session_means_nothing_is_persisted(self):
        with patch("ai_desktop.conversation_sessions.Store") as store:
            r = self.run_ui([], ["Jeden", "tak", "Dwa", ""])
        store.assert_not_called()
        self.assertEqual(r.sent.call_count, 2)


if __name__ == "__main__":
    unittest.main()
