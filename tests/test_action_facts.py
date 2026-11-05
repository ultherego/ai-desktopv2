"""What the controller really did (commands run or refused, changes applied or not) is written to the conversation's
task memory by the program itself, so later turns - 'test it', 'undo it' - rest on facts, not on the model's recollection."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import json
import unittest

from ai_desktop import ask_edit, conversation_state as cs
import test_ask_operations as ops
from test_ask_discovery import ANSWER, ANSWER_PLAN


class MemoryFactTests(unittest.TestCase):
    def test_controller_facts_become_done_observations_and_keep_only_that_they_happened_after_revocation(self):
        talk = cs.Conversation()
        talk.finish("Zrób test", "Wykonano", None, ["/etc/secret.conf"], actions=["Polecenie uname -s: ok, kod 0"])
        facts = [e for e in talk.memory["entries"] if e["status"] == "done"]
        self.assertEqual([e["text"] for e in facts], ["Polecenie uname -s: ok, kod 0"])
        self.assertEqual(facts[0]["kind"], "observation")
        saved = cs.load(cs.export(talk))                                     # also when it was saved before the revocation
        talk.revalidate(lambda path: False)                                  # the file the turn read is revoked
        # An argument the model chose may quote that file, so the words go; the fact that something ran stays.
        self.assertEqual([e["text"] for e in talk.memory["entries"] if e["status"] == "done"], [cs.FACT_REDACTED])
        loaded = cs.load(cs.export(talk))
        self.assertEqual([e["text"] for e in loaded.memory["entries"] if e["status"] == "done"], [cs.FACT_REDACTED])
        saved.revalidate(lambda path: False)
        self.assertEqual([e["text"] for e in saved.memory["entries"] if e["status"] == "done"], [cs.FACT_REDACTED])

    def test_note_action_is_bounded_and_ignores_empty_text_and_calls_outside_a_turn(self):
        cs.note_action("poza turą")                                          # no recorder: silently nothing
        with cs.recording() as recorder:
            for index in range(20):
                cs.note_action("akcja %d" % index)
            cs.note_action("")
            cs.note_action(None)
        self.assertEqual(len(recorder.actions), 8)
        self.assertEqual(list(recorder), [])                                 # the list itself stays the file sources


class ChangeFactTests(unittest.TestCase):
    PROPOSAL = json.dumps({"summary": "Dodaj  wskaźnik\nVPN", "files": [{"path": "waybar/config", "content": "SECRET-BODY"},
                                                                       {"path": "waybar/style.css", "content": "x"}]})

    def test_an_applied_change_names_files_and_check_but_never_the_content(self):
        text = ask_edit._change_fact(self.PROPOSAL, 0, "text")
        self.assertIn("Zastosowano zmianę", text)
        self.assertIn("waybar/config, waybar/style.css", text)
        self.assertIn("Dodaj wskaźnik VPN", text)
        self.assertNotIn("SECRET-BODY", text)

    def test_a_refused_or_cancelled_change_is_recorded_as_not_applied(self):
        text = ask_edit._change_fact(self.PROPOSAL, 2, "text")
        self.assertIn("NIE została zastosowana", text)
        self.assertIn("kod 2", text)

    def test_a_malformed_proposal_still_gives_a_truthful_fact(self):
        for bad in ("nie json", "[]", json.dumps({"summary": "x"})):
            self.assertIn("Zmiana NIE została zastosowana", ask_edit._change_fact(bad, 2, "text"))
            self.assertIn("Zastosowano zmianę", ask_edit._change_fact(bad, 0, "text"))


class CommandFactTests(ops.OperationsBase):
    def facts(self, responses):
        with cs.recording() as recorder:
            self.run_flow(responses)
        return recorder.actions

    def test_a_run_command_is_recorded_with_its_real_outcome(self):
        self.assertEqual(self.facts([ops.run("uname", "-s"), ANSWER_PLAN]), ["Polecenie uname -s: ok, kod 0"])

    def test_refused_commands_are_recorded_as_not_run(self):
        self.assertEqual(self.facts([ops.run("uname", "-a"), ANSWER_PLAN]),
                         ["Polecenie uname -a: poza listą, nie uruchomiono"])




class ThroughTheConversationTests(unittest.TestCase):
    """The fact reaches the NEXT request's task memory through the real ask loop."""

    def test_the_next_turn_carries_the_executed_action_as_a_done_observation(self):
        import test_ask_memory as memory_tests
        harness = memory_tests.Harness()
        harness.setUp()
        def model(payload, number):
            if number == 1:
                cs.note_action("Polecenie systemd-analyze: ok, kod 0")     # as the flow does after a real run
                return memory_tests.answer("Zmierzone.")
            return memory_tests.answer("Dalej.")
        harness.run_ui(["Zmierz start", memory_tests.YES, "A teraz co?", memory_tests.YES, ""], model)
        facts = [e for e in harness.payloads[1]["memory"]["entries"] if e["status"] == "done"]
        harness.assertEqual([e["text"] for e in facts], ["Polecenie systemd-analyze: ok, kod 0"])


if __name__ == "__main__":
    unittest.main()
