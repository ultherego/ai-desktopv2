"""Model-driven read loop over one material: request schema, strict decisions, local execution."""
import copy
import json
import unittest

from ai_desktop import analysis_contract as ac
from ai_desktop import material_loop as ml
from ai_desktop import material_step as ms
from ai_desktop import materials as m

LINES = ["line %04d some filler text for the log" % i for i in range(1, 801)]
LINES[99] = "line 0100 ERROR timeout while talking to the database"
LINES[499] = "line 0500 ERROR retry limit reached"
RAW = ("\n".join(LINES) + "\n").encode()


def loop(history=()):
    return ml.Loop(m.import_bytes(RAW, "file"), history=history)


class RequestSchemaTests(unittest.TestCase):
    def valid(self):
        lp = loop()
        lp._search("ERROR")
        lp._read([(98, 102)])
        return lp, lp.request_input(2, 700000)

    def test_valid_input_passes_and_is_accepted_by_the_transport_on_both_sides(self):
        lp, value = self.valid()
        self.assertIs(ms.validate_input(value), value)
        payload = ac.application_input(None, "Why?", material=value)
        self.assertEqual(json.loads(payload)["mode"], "material")
        raw = payload.encode("utf-8")
        model_input, _ = ac.parse_request(raw)
        self.assertEqual(json.loads(model_input)["material"]["task"]["step"], 2)
        self.assertIn("mode=material", json.loads(payload)["system_prompt"])

    def test_tampering_is_refused_on_both_sides(self):
        _, good = self.valid()
        mutations = {
            "text-changed": lambda v: v["observations"][1].update(text="changed"),
            "hash-changed": lambda v: v["observations"][1]["ref"].update(fragment="0" * 64),
            "other-material": lambda v: v["material"].update(id="0" * 16),
            "step-over-max": lambda v: v["task"].update(step=9),
            "unknown-kind": lambda v: v["observations"].append({"kind": "exec", "cmd": "sh"}),
            "extra-key": lambda v: v.update(extra=1),
            "missing-history": lambda v: v.pop("history"),
            "line-out-of-range": lambda v: v["observations"][0]["hits"][0].update(line=10 ** 6),
            "bad-refusal-code": lambda v: v["observations"].append({"kind": "refused", "action": "read", "code": "x"}),
            "bool-size": lambda v: v["material"].update(size=True),
            "hit-text-int": lambda v: v["observations"][0]["hits"][0].update(text=5),
            "too-many-observations": lambda v: v["observations"].extend({"kind": "dropped", "first": 1, "last": 1} for _ in range(30)),
            "history-too-long": lambda v: v["history"].extend({"question": "q", "answer": "a"} for _ in range(5)),
            "history-bad-turn": lambda v: v["history"].append({"question": "q"}),
        }
        for name, mutate in mutations.items():
            bad = copy.deepcopy(good)
            mutate(bad)
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    ms.validate_input(bad)
                with self.assertRaises(ValueError):
                    ac.application_input(None, "Why?", material=bad)

    def test_data_budget_is_enforced_by_the_schema(self):
        material = m.import_bytes(b"w" * 400000, "file")
        observations = []
        for index in range(9):                                           # 9 x 32 KiB = 288 KiB > 256 KiB
            fragment = m.read_range(material, material.sha256, index * 32768, (index + 1) * 32768)
            observations.append({"kind": "read", "first": 1, "last": 1, "ref": fragment.ref.to_json(),
                                 "text": fragment.text})
        value = {"material": {"id": material.material_id, "version": material.sha256, "size": material.size, "lines": 1},
                 "task": {"step": 1, "max_steps": 8, "bytes_left": 700000}, "observations": observations[:2],
                 "history": []}
        ms.validate_input(value)                                         # 64 KiB is fine
        value["observations"] = observations[:8]
        ms.validate_input(value)                                         # exactly 256 KiB is fine
        value["observations"] = observations
        with self.assertRaises(ValueError):
            ms.validate_input(value)
        self.assertEqual(ms.data_bytes([], [{"question": "ab", "answer": "cde"}]), 5)


class DecisionTests(unittest.TestCase):
    def ok(self, text, lines=800):
        return ms.parse_decision(text, lines)

    def test_the_four_closed_actions_are_accepted(self):
        self.assertEqual(self.ok('{"action":"search","needle":"ERROR"}'), {"action": "search", "needle": "ERROR"})
        self.assertEqual(self.ok('{"action":"read","ranges":[{"first":3,"last":9},{"first":20,"last":20}]}')["ranges"],
                         [(3, 9), (20, 20)])
        self.assertEqual(self.ok('{"action":"answer","text":"Wynik.\\nDruga linia.","sources":[{"first":1,"last":2}]}')["sources"],
                         [(1, 2)])
        self.assertEqual(self.ok('{"action":"ask_user","text":"Jak to uruchomiłeś?"}')["action"], "ask_user")
        self.assertEqual(self.ok('{"action":"whole"}'), {"action": "whole"})

    def test_everything_else_is_refused_without_repair(self):
        bad = ['', 'not json', '{"action":"whole","x":1}', '{"action":"exec","cmd":"ls"}',
               '{"action":"search"}', '{"action":"search","needle":""}', '{"action":"search","needle":"x\\u0000"}',
               '{"action":"search","needle":"%s"}' % ("a" * 129), '{"action":"read","ranges":[]}',
               '{"action":"read","ranges":[{"first":0,"last":3}]}', '{"action":"read","ranges":[{"first":5,"last":3}]}',
               '{"action":"read","ranges":[{"first":1,"last":801}]}', '{"action":"read","ranges":[{"first":"1","last":2}]}',
               '{"action":"read","ranges":[{"first":1,"last":2},{"first":1,"last":2}]}',
               '{"action":"read","ranges":[{"first":1,"last":1}]*5}',
               '{"action":"answer","text":"x"}', '{"action":"answer","text":"","sources":[]}',
               '{"action":"answer","text":"x","sources":[{"first":9,"last":2}]}',
               '{"action":"answer","text":"x\\u001b[0m","sources":[]}',
               '{"action":"ask_user","text":"%s"}' % ("q" * 4200), '{"action":"search","action":"read"}',
               '{"action":"search","needle":NaN}', '[]', 'null', '{"action":"read","ranges":[{"first":true,"last":2}]}']
        for text in bad:
            with self.subTest(text=text[:50]):
                with self.assertRaises(ValueError):
                    self.ok(text)
        with self.assertRaises(ValueError):
            ms.parse_decision(None, 5)
        with self.assertRaises(ValueError):
            ms.parse_decision('{"action":"answer","text":"%s","sources":[]}' % ("a" * 20000), 5)


class LoopTests(unittest.TestCase):
    def test_search_reports_line_numbers_and_truncated_text_at_character_boundaries(self):
        lines = ["zażółć " * 100 + " NEEDLE " + "日本語" * 100] + ["plain"] * 3
        lp = ml.Loop(m.import_bytes(("\n".join(lines) + "\n").encode(), "file"))
        note = lp.execute({"action": "search", "needle": "NEEDLE"})
        (obs,) = lp.observations
        self.assertTrue(note.endswith(": 1 match"), note)
        self.assertEqual(obs["hits"][0]["line"], 1)
        self.assertLessEqual(len(obs["hits"][0]["text"].encode()), ml.SEARCH_SNIPPET)
        obs["hits"][0]["text"].encode("utf-8")      # valid text, never a cut character
        self.assertTrue(obs["complete"])

    def test_read_returns_exact_text_with_verified_references_and_tracks_delivery(self):
        lp = loop()
        lp.execute({"action": "read", "ranges": [(99, 101)]})
        (obs,) = lp.observations
        self.assertEqual(obs["text"], "\n".join(LINES[98:101]) + "\n")
        self.assertEqual((obs["first"], obs["last"]), (99, 101))
        ms.validate_input(lp.request_input(1, 700000))
        self.assertEqual(lp.delivered_summary(), ([], 0))
        lp.mark_handed_off(lp.request_input(1, 700000))
        self.assertEqual(lp.delivered_summary()[0], [[99, 101]])
        self.assertTrue(lp.repeated({"action": "read", "ranges": [(99, 101)]}))
        self.assertFalse(lp.repeated({"action": "read", "ranges": [(99, 102)]}))

    def test_a_single_action_embedded_in_a_short_sentence_is_unwrapped_but_not_repaired(self):
        self.assertEqual(ms.parse_decision('Najpierw wyszukam. {"action":"search","needle":"x"}', 100),
                         {"action": "search", "needle": "x"})
        for raw in ('{"action":"search","needle":"x"} i {"action":"search","needle":"y"}', 'Najpierw {"action":"search"',
                    'Tekst {"action":"search","needle":"x","extra":1}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                ms.parse_decision(raw, 100)

    def test_bad_requests_become_closed_refusals_not_exceptions(self):
        lp = loop()
        lp.execute({"action": "read", "ranges": [(900, 950)]})
        lp.execute({"action": "search", "needle": "bad\x1b"})
        self.assertEqual([(o["action"], o["code"]) for o in lp.observations],
                         [("read", "RANGE_INVALID"), ("search", "NEEDLE_INVALID")])
        self.assertEqual(lp.executed, set())               # refused actions are not "progress"
        ms.validate_input(lp.request_input(1, 1000))

    def test_an_end_past_the_last_line_is_cut_to_it_and_the_refusal_is_explained(self):
        lp = loop()
        note = lp.execute({"action": "read", "ranges": [(790, 900)]})
        (obs,) = lp.observations
        self.assertEqual((obs["first"], obs["last"]), (790, 800))
        lp.mark_handed_off(lp.request_input(1, 700000))
        self.assertEqual(lp.delivered_summary()[0], [[790, 800]])
        explained = lp.execute({"action": "read", "ranges": [(900, 950)]})
        self.assertEqual(explained, "lines 900–950 — refused: line range outside the file (the file has 800 lines)")
        self.assertEqual(lp.observations[-1], {"kind": "refused", "action": "read", "code": "RANGE_INVALID"})
        self.assertNotIn("asked for refused", note + explained)

    def test_a_read_larger_than_one_request_is_refused_whole(self):
        lines = ["x" * 99] * 3500
        lp = ml.Loop(m.import_bytes(("\n".join(lines) + "\n").encode(), "file"))
        lp.execute({"action": "read", "ranges": [(1, 3500)]})       # ~350 KB
        self.assertEqual(lp.observations, [{"kind": "refused", "action": "read", "code": "TOO_LARGE"}])
        self.assertEqual(lp.delivered, [])

    def test_oldest_read_texts_are_dropped_visibly_to_keep_the_packet_in_budget(self):
        lines = ["y" * 99] * 4000
        lp = ml.Loop(m.import_bytes(("\n".join(lines) + "\n").encode(), "file"))
        for first in (1, 701, 1401, 2101):
            lp.execute({"action": "read", "ranges": [(first, first + 699)]})     # ~70 KB each
            lp.mark_handed_off(lp.request_input(1, 700000))
        kinds = [o["kind"] for o in lp.observations]
        self.assertIn("dropped", kinds)
        self.assertLessEqual(lp.data_bytes(), ms.DATA_BYTES)
        self.assertEqual(kinds[-1], "read")                  # the newest text survives
        ms.validate_input(lp.request_input(3, 500000))
        self.assertEqual(len(lp.delivered), 12)              # (4 reads x 3 chunks) delivery history is kept even when text is dropped

    def test_claimed_sources_must_be_covered_by_delivered_lines(self):
        lp = loop()
        lp.execute({"action": "read", "ranges": [(99, 101), (500, 500)]})
        lp.mark_handed_off(lp.request_input(1, 700000))
        confirmed, rejected = lp.confirm_sources([(99, 100), (500, 500), (98, 100), (300, 301)])
        self.assertEqual(confirmed, [(99, 100), (500, 500)])
        self.assertEqual(rejected, [(98, 100), (300, 301)])

    def test_whole_preload_only_for_small_materials(self):
        small = ml.Loop(m.import_bytes(b"one\ntwo\nthree\n", "file"))
        self.assertTrue(small.preload_whole())
        self.assertEqual(small.observations[0]["text"], "one\ntwo\nthree\n")
        big = ml.Loop(m.import_bytes(b"z" * 400000, "file"))
        self.assertFalse(big.preload_whole())
        self.assertEqual(big.observations, [])

    def test_range_preload_reports_failure_for_an_oversize_or_invalid_scope(self):
        lp = loop()
        self.assertTrue(lp.preload_range(10, 20))
        self.assertFalse(loop().preload_range(500, 900))

    def test_history_is_bounded_and_shortened_visibly(self):
        turn = ml.history_turn("q", "a" * 5000)
        self.assertTrue(turn["answer"].endswith("…[shortened]"))
        self.assertLessEqual(len(turn["answer"].encode()), ms.HISTORY_TEXT + 40)
        lp = ml.Loop(m.import_bytes(RAW, "file"), history=[{"question": "q%d" % i, "answer": "a"} for i in range(9)])
        self.assertEqual(len(lp.history), ms.MAX_HISTORY)
        self.assertEqual(lp.history[-1]["question"], "q8")


if __name__ == "__main__":
    unittest.main()
