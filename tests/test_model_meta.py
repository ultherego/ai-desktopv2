"""The closed model-metadata channel: client `modelUsage` -> sandbox -> response packet -> `--v` line. Never tokens or free text."""
import json
import unittest

from ai_desktop import analysis_contract as ac
from ai_desktop import analysis_sandbox as sandbox
from ai_desktop import material_cli as mcli

USAGE = {"claude-sonnet-5-5": {"inputTokens": 1200, "outputTokens": 340, "cacheReadInputTokens": 99, "costUSD": 0.5,
                               "contextWindow": 200000, "maxOutputTokens": 64000}}


def native(**extra):
    base = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "permission_denials": [], "result": "wynik"}
    base.update(extra)
    return json.dumps(base).encode()


class ExtractTests(unittest.TestCase):
    def test_only_the_closed_fields_are_taken_and_everything_else_is_dropped(self):
        meta = ac.extract_meta({"modelUsage": USAGE, "session_id": "s", "apiKeySource": "oauth"})
        self.assertEqual(meta, {"models": [{"model": "claude-sonnet-5-5", "input": 1200, "output": 340, "window": 200000}]})
        self.assertNotIn("costUSD", json.dumps(meta))

    def test_absent_or_malformed_usage_gives_none_not_a_guess(self):
        for result in ({}, {"modelUsage": {}}, {"modelUsage": []}, {"modelUsage": {"bad id with space": {}}},
                       {"modelUsage": {"a/b": {}}}, {"modelUsage": {"m" * 65: {}}}, {"modelUsage": {"m1": 3}},
                       {"modelUsage": {"a": {}, "b": {}, "c": {}, "d": {}}}, None, "x"):
            with self.subTest(result=str(result)[:40]):
                self.assertIsNone(ac.extract_meta(result))

    def test_bad_counts_become_unknown_and_never_pass_through(self):
        meta = ac.extract_meta({"modelUsage": {"m1": {"inputTokens": True, "outputTokens": -1, "contextWindow": 1.5}}})
        self.assertEqual(meta["models"][0], {"model": "m1", "input": None, "output": None, "window": None})
        meta = ac.extract_meta({"modelUsage": {"m1": {"inputTokens": 10 ** 12}}})
        self.assertIsNone(meta["models"][0]["input"])

    def test_validate_meta_is_closed(self):
        good = {"models": [{"model": "m1", "input": 1, "output": 2, "window": None}]}
        self.assertEqual(ac.validate_meta(good), good)
        for bad in ({}, {"models": []}, {"models": [{"model": "m1", "input": 1, "output": 2}]},
                    {"models": [dict(good["models"][0], extra=1)]}, dict(good, extra=1),
                    {"models": [dict(good["models"][0], input=True)]}, {"models": [dict(good["models"][0], model="x y")]},
                    {"models": [good["models"][0]] * 4}):
            with self.subTest(bad=str(bad)[:50]), self.assertRaises(ValueError):
                ac.validate_meta(bad)


class SandboxTests(unittest.TestCase):
    def test_model_text_attaches_the_metadata_without_changing_the_text(self):
        text = sandbox.model_text(0, native(modelUsage=USAGE))
        self.assertEqual(text, "wynik")
        self.assertEqual(text.meta["models"][0]["model"], "claude-sonnet-5-5")

    def test_no_usage_means_no_metadata(self):
        self.assertIsNone(sandbox.model_text(0, native()).meta)

    def test_text_that_looks_like_metadata_stays_text(self):
        fake = json.dumps({"models": [{"model": "evil", "input": 1, "output": 1, "window": 1}]})
        text = sandbox.model_text(0, json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                                                 "permission_denials": [], "result": fake}).encode())
        self.assertEqual(str(text), fake)
        self.assertIsNone(text.meta)


class PacketTests(unittest.TestCase):
    SHA = "a" * 64

    def test_metadata_round_trips_next_to_the_text_in_an_ok_response(self):
        meta = ac.extract_meta({"modelUsage": USAGE})
        packet = ac.result_packet(self.SHA, text="Odpowiedź.", mode="material", meta=meta)
        self.assertEqual(json.loads(packet)["meta"], meta)
        text = ac.read_result(packet, self.SHA, mode="material")
        self.assertEqual((str(text), text.meta), ("Odpowiedź.", meta))

    def test_a_response_without_metadata_is_unchanged(self):
        packet = ac.result_packet(self.SHA, text="Odpowiedź.", mode="material")
        self.assertNotIn(b'"meta"', packet)
        self.assertIsNone(ac.read_result(packet, self.SHA, mode="material").meta)

    def test_metadata_is_refused_on_a_refusal_in_web_mode_and_when_malformed(self):
        meta = ac.extract_meta({"modelUsage": USAGE})
        with self.assertRaises(ac.AnalysisDenied):
            ac.result_packet(self.SHA, reason="timeout", mode="material", meta=meta)
        with self.assertRaises(ac.AnalysisDenied):
            ac.result_packet(self.SHA, text="x", mode="web", meta=meta)
        with self.assertRaises(ac.AnalysisDenied):
            ac.result_packet(self.SHA, text="x", mode="material", meta={"models": [{"model": "m"}]})

    def test_a_tampered_packet_is_rejected_by_the_reader(self):
        meta = ac.extract_meta({"modelUsage": USAGE})
        value = json.loads(ac.result_packet(self.SHA, text="x", mode="material", meta=meta))
        for mutate in (lambda v: v["meta"]["models"][0].update(token="sk-secret"), lambda v: v.update(extra=1),
                       lambda v: v["meta"]["models"][0].update(model="a b"), lambda v: v.update(status="refused", reason="timeout", text="")):
            bad = json.loads(json.dumps(value))
            mutate(bad)
            with self.subTest(str(mutate.__code__.co_consts[-1:])), self.assertRaises(ac.AnalysisDenied):
                ac.read_result(ac.canonical(bad).encode(), self.SHA, mode="material")


class LineTests(unittest.TestCase):
    def test_the_verbose_line_names_the_model_or_says_it_is_unavailable(self):
        meta = ac.extract_meta({"modelUsage": USAGE})
        self.assertEqual(mcli.Session.model_line(meta), "MODEL: claude-sonnet-5-5 · okno 200000 · tokeny we/wy 1200/340")
        self.assertEqual(mcli.Session.model_line(None), "MODEL: niedostępny (klient nie podał metadanych)")


class BoundaryTests(unittest.TestCase):
    """send_once must hand callers a plain `str` (strict `type(x) is str` consumers such as render_answer), metadata apart."""

    @staticmethod
    def material_payload():
        from ai_desktop import material_whole as whole, materials
        mat = materials.import_bytes(b"alpha\nbeta\n" * 50, "file")
        plan = whole.WholePlan(mat)
        return ac.application_input(None, "SYNTHETIC: analysis question", material=plan.map_input(0, 1, 4096))

    def run_send_once(self, payload, packet):
        from unittest.mock import MagicMock, patch
        from ai_desktop import analysis_service as service
        approval = ac.Approval(payload, ac.digest(payload))
        conn = MagicMock()
        conn.__enter__.return_value = conn
        with patch.object(service.socket, "socket", return_value=conn), patch.object(service.e2_account, "account_ready", return_value=True), \
                patch.object(service, "manager_peer"), patch.object(service, "endpoint", return_value="e"), \
                patch.object(service, "packet", return_value=service.READY), patch.object(service, "send_message"), \
                patch.object(service, "receive_message", return_value=packet), patch.object(service.os, "getuid", return_value=1000), \
                patch.object(service.os, "geteuid", return_value=1000), patch.object(service, "socket_path", return_value="/x"):
            return service, service.send_once(approval)

    def test_a_material_answer_arrives_as_a_plain_str_with_the_metadata_kept_apart(self):
        from ai_desktop.analysis_terminal import render_answer
        meta = ac.extract_meta({"modelUsage": USAGE})
        payload = self.material_payload()
        service, text = self.run_send_once(payload, ac.result_packet(ac.digest(payload), text="Odpowiedź.", mode="material", meta=meta))
        self.assertIs(type(text), str)
        self.assertEqual((text, service.send_once.last_meta), ("Odpowiedź.", meta))
        render_answer(text)                                                   # a strict consumer accepts it

    def test_an_answer_without_metadata_clears_the_previous_one(self):
        meta = ac.extract_meta({"modelUsage": USAGE})
        payload = self.material_payload()
        self.run_send_once(payload, ac.result_packet(ac.digest(payload), text="Pierwsza.", mode="material", meta=meta))
        service, text = self.run_send_once(payload, ac.result_packet(ac.digest(payload), text="Druga.", mode="material"))
        self.assertEqual((text, service.send_once.last_meta), ("Druga.", None))

    def test_the_strict_consumer_rejects_the_subclass_which_is_why_the_boundary_converts(self):
        from ai_desktop.analysis_terminal import render_answer
        with self.assertRaises((TypeError, ValueError, ac.AnalysisDenied)):
            render_answer(ac.ModelText("x", None))
        render_answer(str(ac.ModelText("x", None)))


if __name__ == "__main__":
    unittest.main()
