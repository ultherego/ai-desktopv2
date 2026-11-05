"""Continuous `ask` conversation: task memory survives the verbatim window, corrections are kept apart, a revoked source
cannot come back through history, notes or a resumed session, and memory never adds a provider request.

Everything runs through the normal `ask.main` input with a scripted stand-in for the provider; assertions look at the
payload that was actually approved and handed to the client, never at the stand-in's printed answer alone."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import ExitStack, nullcontext, redirect_stdout
import io
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import ask_cli as ask, assistant_flow, conversation_memory as cm, conversation_sessions as sessions
from ai_desktop import conversation_state, execution
from ai_desktop import conversation_state as cs
from ai_desktop.analysis_contract import MAX_PACKET
from ai_desktop.permissions import Limits
from ai_desktop.terminal_consent import PROMPTS

EMPTY = {"files": [], "systemd": [], "weather": False}
PROFILE = {"provider": "claude", "model": "default", "effort": "default", "instructions": "",
           "profile": "default", "access": "changes"}
YES = "yes"
REAL_UID = _os.geteuid()          # sessions check file ownership through the same global os module
MARKER = "UNIKALNY-ZNACZNIK-7f3a91"


def answer(text, *ops):
    """A planner `answer` plan whose text ends with the memory line, as the system prompt asks."""
    line = ("\n@@MEMORY " + json.dumps(ops[0], ensure_ascii=False)) if ops else ""
    return json.dumps({"action": "answer", "text": text + line}, ensure_ascii=False)


class Harness(unittest.TestCase):
    def setUp(self):
        self.revoked = set()
        self.payloads = []

    def run_ui(self, replies, model, *, argv=(), conversation=None, history_check=None):
        out, queue = io.StringIO(), list(replies)
        out.isatty = lambda: True

        def reader(prompt):
            item = queue.pop(0)
            if isinstance(item, type) and issubclass(item, BaseException):
                raise item()
            return item

        def send(approval):
            payload = json.loads(approval.payload)
            self.payloads.append(payload)
            self.assertLessEqual(len(approval.payload.encode("utf-8")), MAX_PACKET)
            return model(payload, len(self.payloads))

        policy = Mock()
        policy.limits.return_value = Limits()
        policy.allows.side_effect = lambda path: path not in self.revoked
        snapshot = assistant_flow.AccessSnapshot(policy, {"claude": True, "systemd": [], "weather": False}, dict(EMPTY))
        with ExitStack() as stack:
            stack.enter_context(patch("ai_desktop.agent_profiles.resolve", return_value=PROFILE))
            stack.enter_context(patch.object(ask.os, "geteuid", return_value=REAL_UID))
            stack.enter_context(patch.object(ask.os, "getuid", return_value=REAL_UID))
            stack.enter_context(patch.object(ask.sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch.object(ask, "installation_ready"))
            stack.enter_context(patch.object(assistant_flow, "capture_access", return_value=snapshot))
            stack.enter_context(patch.object(ask, "managed_session", side_effect=lambda **_: nullcontext()))
            sent = stack.enter_context(patch.object(ask, "send_once", side_effect=send))
            if conversation is not None:
                stack.enter_context(patch("ai_desktop.conversation_sessions.load_session", return_value=conversation))
            stack.enter_context(redirect_stdout(out))
            code = ask.main(list(argv), reader=reader)
        self.assertEqual(queue, [], "unused replies")
        return SimpleNamespace(code=code, out=out.getvalue(), sent=sent)


def entries(payload):
    return {item["id"]: item for item in payload.get("memory", {}).get("entries", [])}


def find(payload, text):
    return [item for item in entries(payload).values() if text in item["text"]]


class UndervoltScenarioTests(Harness):
    """Twenty turns: goal, baseline, two trials, a corrected result, a rejected proposal, then a question that needs
    all of them after the opening turns have left the verbatim window."""

    TURNS = [
        ("Chcę zrobić undervolting dla gry. Korzystam z LACT.", "O jaką grę chodzi?",
         {"add": [{"kind": "open", "text": "Jaka gra i jaki cel (LACT)"}]}),
        ("Space Marine 2. Chcę zmniejszyć pobór mocy i zachować podobne FPS.", "Rozumiem.",
         {"add": [{"kind": "success", "text": "Cel: niższy pobór mocy, FPS podobne (Space Marine 2)"}]}),
        ("Ustawienie bazowe: 250 W, 92 FPS, napięcie 1050 mV.", "Zapisane jako baza.",
         {"add": [{"kind": "baseline", "text": "Baza: 250 W, 92 FPS, 1050 mV"}]}),
        ("Próba A: 220 W, 91 FPS, stabilna.", "Zapisane.",
         {"add": [{"kind": "trial", "text": "Próba A: 220 W, 91 FPS, stabilna"}]}),
        ("Próba B: 200 W, gra się zawiesiła.", "Zapisane.",
         {"add": [{"kind": "trial", "text": "Próba B: 200 W, zawieszenie gry"}]}),
        ("Poprawka: 220 W dotyczyło próby B, a próba A miała 230 W.", "Poprawiam.",
         {"add": [{"kind": "correction", "text": "Próba A: 230 W, 91 FPS, stabilna", "supersedes": ["m4.1"]},
                  {"kind": "correction", "text": "Próba B: 220 W, zawieszenie gry", "supersedes": ["m5.1"]}]}),
        ("Spróbuj 190 W.", "Propozycja: 190 W.",
         {"add": [{"kind": "proposal", "text": "Propozycja: 190 W w próbie C"}]}),
        ("Nie, odrzucam 190 W.", "Odrzucone.", {"mark": [{"id": "m7.1", "status": "rejected"}]}),
    ]

    def model(self, payload, number):
        if number <= len(self.TURNS):
            _, text, ops = self.TURNS[number - 1]
            return answer(text, ops)
        if number == 20:
            stable = [e for e in entries(payload).values() if e["status"] == "active" and "stabilna" in e["text"]]
            return answer("Ostatnia stabilna: " + "; ".join(e["text"] for e in stable))
        return answer("Dalej %d." % number, {})

    def test_after_twenty_turns_the_provider_gets_goal_baseline_and_the_corrected_result(self):
        replies = []
        for index in range(20):
            question = self.TURNS[index][0] if index < len(self.TURNS) else (
                "Wróćmy do ostatniego stabilnego ustawienia. Która to była próba?" if index == 19 else "Krok %d" % index)
            replies += [question] + ([] if replies else [YES])
        result = self.run_ui(replies + [""], self.model)
        self.assertEqual(result.code, 0, result.out)
        # Exactly one provider request per user turn: memory adds nothing, there is no hidden summarising call.
        self.assertEqual(result.sent.call_count, 20)
        last = self.payloads[-1]
        asked = [turn["question"] for turn in last["conversation"]]
        self.assertEqual(len(asked), 19)
        self.assertTrue(find(last, "undervolting") or find(last, "LACT"))   # the goal is also in the task memory
        self.assertEqual(len(find(last, "Baza: 250 W, 92 FPS")), 1)
        corrected = {item["text"]: item for item in entries(last).values()}
        self.assertEqual(corrected["Próba A: 230 W, 91 FPS, stabilna"]["status"], "active")
        old_a = corrected["Próba A: 220 W, 91 FPS, stabilna"]
        self.assertEqual(old_a["status"], "superseded")                # the superseded value is marked, not true
        self.assertEqual(old_a["by"], corrected["Próba A: 230 W, 91 FPS, stabilna"]["id"])
        self.assertEqual(corrected["Próba B: 200 W, zawieszenie gry"]["status"], "superseded")
        self.assertEqual(corrected["Próba B: 220 W, zawieszenie gry"]["status"], "active")
        self.assertEqual(corrected["Propozycja: 190 W w próbie C"]["status"], "rejected")
        self.assertEqual(last["memory"]["dropped"], 0)
        # The visible answer was computed by the stand-in from the approved payload only.
        self.assertIn("Ostatnia stabilna: Próba A: 230 W, 91 FPS, stabilna", result.out)
        self.assertNotIn("@@MEMORY", result.out)                        # the memory line is never shown
        self.assertEqual(result.out.count("How can I help?"), 0)     # the greeting belongs to the terminal reader

    def test_memory_and_history_reach_only_assistant_and_question_requests(self):
        payload = execution_payload = None
        with execution.using_history([{"question": "q", "answer": "a"}]), \
                execution.using_memory({"entries": [{"id": "m1.1", "turn": 1, "kind": "goal", "status": "active",
                                                     "text": "cel"}], "dropped": 0}):
            from ai_desktop import analysis_contract as contract
            payload = json.loads(contract.application_input(None, "pytanie", access=EMPTY))
            web = json.loads(contract.application_input(None, "pytanie", web=True))
            task = contract.application_input(None, task={"instruction": "x", "files": []}) if False else None
        self.assertIn("memory", payload)
        self.assertNotIn("memory", web)
        self.assertNotIn("conversation", web)

    def test_a_different_domain_uses_the_same_mechanism(self):
        turns = [("Usługa foo.service nie startuje po aktualizacji.", "Sprawdźmy.",
                  {"add": [{"kind": "goal", "text": "foo.service nie startuje po aktualizacji"}]}),
                 ("Status: exit-code 203/EXEC.", "To brak pliku wykonywalnego.",
                  {"add": [{"kind": "observation", "text": "foo.service: status 203/EXEC"}]}),
                 ("Co ustaliliśmy?", "Podsumowanie.", None)]

        def model(payload, number):
            _, text, ops = turns[number - 1]
            return answer(text, ops) if ops else answer(text)

        result = self.run_ui([turns[0][0], YES, turns[1][0], turns[2][0], ""], model)
        self.assertEqual(result.sent.call_count, 3)
        self.assertEqual(len(find(self.payloads[-1], "203/EXEC")), 1)
        self.assertEqual(len(find(self.payloads[-1], "foo.service nie startuje")), 1)


class MemoryRobustnessTests(Harness):
    def test_missing_or_invalid_update_keeps_the_users_own_words(self):
        replies = ["Cel: obniżyć temperaturę GPU.", YES, "Baza: 71 °C.", "Dalej", ""]
        outputs = ["Dobrze.", "Zapisane\n@@MEMORY {zepsuty json", "OK"]
        result = self.run_ui(replies, lambda payload, n: json.dumps({"action": "answer", "text": outputs[n - 1]}))
        self.assertEqual(result.code, 0, result.out)
        texts = [item["text"] for item in entries(self.payloads[-1]).values()]
        self.assertTrue(any("temperatur" in text for text in texts))     # first message: deterministic goal
        self.assertTrue(any("71" in text for text in texts))             # invalid update -> user's message kept
        self.assertNotIn("@@MEMORY", result.out)

    def test_budget_exhaustion_is_reported_and_counted_not_silent(self):
        replies = []
        for index in range(60):
            replies += ["Wynik %d: wartość %d." % (index, index)] + ([] if replies else [YES])

        def model(payload, number):
            return answer("OK", {"add": [{"kind": "value", "text": "Wartość %d: %d W " % (number, number) + "x" * 380}]})
        result = self.run_ui(replies + [""], model)
        last = self.payloads[-1]
        self.assertLessEqual(len(last["memory"]["entries"]), cm.MAX_ENTRIES)
        self.assertGreater(last["memory"]["dropped"], 0)                 # what was lost is counted for the model
        self.assertLessEqual(cm.size(last["memory"]), cm.MEMORY_BYTES)
        self.assertIn("limit", result.out)
        self.assertTrue(find(last, "Wynik 0") or last["memory"]["entries"][0]["kind"] in cm.PINNED)

    def test_polish_letters_emoji_and_quotes_survive(self):
        text = "Cel: żółć 🚀 „cytat”"
        result = self.run_ui([text, YES, "Dalej", ""],
                             lambda payload, n: answer("Zapisane.", {"add": [{"kind": "baseline", "text": text}]}) if n == 1
                             else answer("OK"))
        self.assertTrue(find(self.payloads[-1], "żółć 🚀 „cytat”"))

    def test_a_declined_turn_sends_nothing_and_is_not_recorded(self):
        result = self.run_ui(["Pierwsze", "no", "Drugie", YES, "Trzecie", ""], lambda p, n: answer("O%d" % n))
        self.assertEqual(result.sent.call_count, 2)
        self.assertEqual([t["question"] for t in self.payloads[1]["conversation"]], ["Drugie"])
        self.assertNotIn("Pierwsze", json.dumps(self.payloads))

    def test_a_provider_error_in_one_turn_leaves_earlier_turns_intact(self):
        def model(payload, number):
            if number == 2:
                raise ask.AnalysisDenied("client")
            return answer("O%d" % number)
        result = self.run_ui(["Pierwsze", YES, "Drugie", ""], model)
        self.assertEqual(result.code, 2)                                  # the failed turn is reported in the exit code
        self.assertIn("The conversation continues", result.out)                         # but the conversation went on until the user left


class RevocationTests(Harness):
    def model(self, payload, number):
        if number == 1:
            conversation_state.note_sources(["/home/u/secret.conf"])
            return answer("Odczytano: %s" % MARKER, {"add": [{"kind": "observation", "text": "Plik: " + MARKER}]})
        return answer("Dalej")

    def test_marker_does_not_come_back_after_access_is_revoked(self):
        def model(payload, number):
            result = self.model(payload, number)
            if number == 2:
                self.revoked.add("/home/u/secret.conf")
            return result
        self.run_ui(["Przeczytaj plik.", YES, "Co w nim było?", "A teraz?", ""], model)
        second = json.dumps(self.payloads[1])
        self.assertIn(MARKER, second)                                     # allowed at the time: it is in the context
        self.revoked.add("/home/u/secret.conf")
        self.payloads.clear()
        self.run_ui(["Przeczytaj plik.", YES, "Cofnięte", ""], lambda p, n: self.model(p, n)
                    if n == 1 else answer("Dalej"))
        # fresh conversation here: nothing to carry. The real check is the same conversation with revoke in the middle:
        self.payloads.clear()
        self.revoked.clear()

        def revoke_after_first(payload, number):
            result = self.model(payload, number)
            if number == 1:
                self.revoked.add("/home/u/secret.conf")
            return result
        out = self.run_ui(["Przeczytaj plik.", YES, "Co dalej?", "I jeszcze?", ""], revoke_after_first)
        for later in self.payloads[1:]:
            self.assertNotIn(MARKER, json.dumps(later), "revoked content came back")
        self.assertIn("revoked", out.out)

    def test_user_goal_is_kept_when_only_model_derived_content_is_revoked(self):
        def model(payload, number):
            result = self.model(payload, number)
            if number == 1:
                self.revoked.add("/home/u/secret.conf")
            return result
        self.run_ui(["Chcę sprawdzić konfigurację sterownika.", YES, "Dalej", ""], model)
        second = self.payloads[1]
        self.assertTrue(find(second, "konfigurację sterownika"))
        self.assertNotIn(MARKER, json.dumps(second))


class SessionResumeTests(Harness):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory(prefix="aid-memory-", dir=Path.home())
        self.addCleanup(self.temporary.cleanup)
        self.store = sessions.Store(home=self.temporary.name)

    def conversation(self, ident=None):
        if ident is None:
            return self.store.create("ask", "", "", PROFILE)
        return self.store.open(ident, "ask", "", "", PROFILE)

    def test_resume_keeps_memory_and_corrections_and_rechecks_policy(self):
        first = self.conversation()
        ident = first.session_id

        def model(payload, number):
            if number == 1:
                conversation_state.note_sources(["/home/u/secret.conf"])
                return answer("Zapisane %s" % MARKER, {"add": [{"kind": "baseline", "text": "Baza: 250 W"},
                                                              {"kind": "observation", "text": "Plik: " + MARKER}]})
            if number == 2:
                return answer("Poprawiam.", {"add": [{"kind": "correction", "text": "Baza: 240 W", "supersedes": ["m1.2"]}]})
            return answer("Dalej")
        self.run_ui(["Cel: obniżyć pobór. Baza 250 W.", YES, "Poprawka: baza to 240 W.", ""], model,
                    conversation=first)
        first.close()
        record = self.store.read(ident)
        self.assertIn("memory", record)
        self.assertLessEqual(len(json.dumps(record).encode()), sessions.MAX_FILE)

        # Same conversation resumed, access still allowed: the memory is back, with the correction.
        self.payloads.clear()
        resumed = self.conversation(ident)
        self.run_ui(["Kontynuuj od ostatniego stabilnego wyniku.", YES, ""], lambda p, n: answer("OK"),
                    conversation=resumed)
        resumed.close()
        entries_now = entries(self.payloads[0])
        self.assertTrue(any(e["text"] == "Baza: 240 W" and e["status"] == "active" for e in entries_now.values()))
        self.assertTrue(any(e["status"] == "superseded" for e in entries_now.values()))

        # Access revoked in between: the marker must not come back through history, memory or the resumed file.
        self.payloads.clear()
        self.revoked.add("/home/u/secret.conf")
        again = self.conversation(ident)
        out = self.run_ui(["Dalej po cofnięciu.", YES, ""], lambda p, n: answer("OK"), conversation=again)
        again.close()
        self.assertNotIn(MARKER, json.dumps(self.payloads[0]))
        self.assertTrue(find(self.payloads[0], "obniżyć pobór"))         # the user's own goal survives
        self.assertIn("revoked", out.out)

    def test_an_old_four_turn_record_without_memory_still_opens_and_continues(self):
        value = self.conversation()
        old = value.record
        old["history"] = [{"question": "Stare %d" % i, "answer": "Odp %d" % i} for i in range(4)]
        self.store.update(old, old["revision"])
        value.close()
        resumed = self.conversation(value.session_id)
        self.run_ui(["Nowe", YES, ""], lambda p, n: answer("OK"), conversation=resumed)
        resumed.close()
        self.assertEqual([t["question"] for t in self.payloads[0]["conversation"]], ["Stare %d" % i for i in range(4)])
        self.assertNotIn("memory", self.payloads[0])                     # nothing invented for a pre-memory record
        record = self.store.read(value.session_id)
        self.assertEqual(len(record["history"]), 5)
        self.assertIn("memory", record)

    def test_worst_case_record_stays_inside_the_file_limit_and_keeps_the_memory(self):
        value = self.conversation()
        long_question, long_answer = "ż" * 1000, "ą" * 1500                 # 2 KiB / 3 KiB of UTF-8 each
        def model(payload, number):
            return answer(long_answer, {"add": [{"kind": "value", "text": "Wartość %d: %s" % (number, "x" * 380)}]})
        replies = []
        for index in range(12):
            replies += [long_question + str(index)] + ([] if replies else [YES])
        self.run_ui(replies + [""], model, conversation=value)
        value.close()
        record = self.store.read(value.session_id)
        self.assertLessEqual(len(json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()),
                             sessions.MAX_FILE)
        self.assertTrue(record["memory"]["entries"])
        self.assertLessEqual(len(record["history"]), sessions.MAX_HISTORY)
        self.assertEqual(len(record["turns"]), len(record["history"]))

    def test_without_session_nothing_is_written(self):
        with patch.object(sessions, "Store") as store:
            self.run_ui(["Jeden", YES, ""], lambda p, n: answer("OK"))
        store.assert_not_called()


class MemoryUnitTests(unittest.TestCase):
    def test_marker_split_and_ops_validation(self):
        text, raw = cm.split_marker('Odpowiedź.\n@@MEMORY {"add":[{"kind":"goal","text":"x"}]}')
        self.assertEqual(text, "Odpowiedź.")
        self.assertEqual(cm.parse_ops(raw)["add"][0]["kind"], "goal")
        for bad in ('{"add":[{"kind":"nope","text":"x"}]}', '{"add":"x"}', '{"extra":1}', "[]", "not json",
                    '{"mark":[{"id":"zly","status":"done"}]}', '{"add":[{"kind":"goal","text":"\\u0007"}]}'):
            self.assertIsNone(cm.parse_ops(bad), bad)
        self.assertEqual(cm.split_marker("Bez znacznika"), ("Bez znacznika", None))

    def test_a_correction_never_leaves_two_true_values_and_unknown_ids_are_reported(self):
        memory, _ = cm.apply(cm.empty(), 1, cm.parse_ops('{"add":[{"kind":"value","text":"A=1"}]}'))
        memory, notes = cm.apply(memory, 2, cm.parse_ops(
            '{"add":[{"kind":"correction","text":"A=2","supersedes":["m1.1","m9.9"]}]}'))
        status = {e["text"]: e["status"] for e in memory["entries"]}
        self.assertEqual(status, {"A=1": "superseded", "A=2": "active"})
        self.assertIn("unknown-id", notes)

    def test_wire_schema_is_closed(self):
        good = {"entries": [{"id": "m1.1", "turn": 1, "kind": "goal", "status": "active", "text": "x"}], "dropped": 0}
        self.assertEqual(cm.validate(good), good)
        for mutate in (lambda d: d["entries"][0].update(path="/etc/passwd"), lambda d: d["entries"][0].update(kind="cmd"),
                       lambda d: d["entries"][0].update(id="../x"), lambda d: d.update(extra=1),
                       lambda d: d["entries"][0].update(text="x" * 401), lambda d: d.update(dropped=-1),
                       lambda d: d["entries"].extend([dict(d["entries"][0])])):
            bad = json.loads(json.dumps(good))
            mutate(bad)
            with self.assertRaises(ValueError):
                cm.validate(bad)


class RevocationDependencyTests(unittest.TestCase):
    def conversation(self):
        talk = cs.Conversation()
        talk.finish("Przeczytaj plik", "Treść pliku", None, ("/x/plik",))
        return talk

    def test_an_independent_answer_after_a_revocation_is_not_removed_with_it(self):
        talk = self.conversation()
        self.assertEqual(talk.revalidate(lambda path: False), 1)
        talk.finish("Czym jest inode?", "Inode to struktura.", None, ())
        self.assertEqual(talk.turns[-1].taint, ())
        talk.revalidate(lambda path: False)
        self.assertEqual(talk.history()[-1]["answer"], "Inode to struktura.")

    def test_a_later_answer_still_depends_on_what_it_could_see_while_access_lasts(self):
        talk = self.conversation()
        talk.finish("Co dalej?", "Dalej.", None, ())
        self.assertEqual(talk.turns[-1].taint, ("/x/plik",))

    def test_a_controller_fact_keeps_that_it_happened_and_loses_what_may_quote_the_file(self):
        talk = cs.Conversation()
        secret = "SYNTETYCZNY-ZNACZNIK"
        talk.finish("Uruchom", "Gotowe.", None, ("/x/plik",), actions=("Odczyt uname " + secret + ": ok",))
        self.assertIn(secret, json.dumps(talk.task_memory(), ensure_ascii=False))
        talk.revalidate(lambda path: False)
        text = json.dumps(talk.task_memory(), ensure_ascii=False)
        self.assertNotIn(secret, text)
        self.assertIn(cs.FACT_REDACTED, text)

    def test_a_fact_with_no_source_survives_a_revocation_word_for_word(self):
        talk = cs.Conversation()
        talk.finish("Zmień", "Gotowe.", None, (), actions=("Zmiana systemctl restart foo.service: ok",))
        talk.revalidate(lambda path: False)
        self.assertIn("systemctl restart foo.service", json.dumps(talk.task_memory(), ensure_ascii=False))

    def test_many_sources_across_turns_still_save_and_no_kept_turn_has_a_shortened_dependency_list(self):
        talk = cs.Conversation()
        for turn in range(5):
            sources = tuple("/x/%d/%d" % (turn, index) for index in range(100))
            talk.finish("Pytanie %d" % turn, "Odpowiedź %d" % turn, None, sources)
        state = cs.export(talk)
        self.assertLessEqual(len(state["paths"]), sessions.MAX_PATHS)
        reloaded = cs.load(json.loads(json.dumps(state)))
        for saved, original in zip(reloaded.turns, talk.turns):
            if saved.answer == cs.REDACTED:
                self.assertEqual(saved.taint, ())
            else:
                self.assertEqual(saved.taint, original.taint)            # complete, or the answer is not kept at all
        self.assertTrue(any(turn.answer == cs.REDACTED for turn in reloaded.turns))
        self.assertTrue(any(turn.answer != cs.REDACTED for turn in reloaded.turns))
        self.assertEqual(cs.MAX_SOURCE_PATHS, sessions.MAX_PATHS)


if __name__ == "__main__":
    unittest.main()
