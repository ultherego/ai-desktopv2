"""LC0 grant and ledger: budgets, deadline, no retry, honest outcome states, no text in evidence."""
import json
import unittest

from ai_desktop import material_grant as g
from ai_desktop import materials as m


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(raw=None, **overrides):
    material = m.import_bytes(raw if raw is not None else (b"line of synthetic log text\n" * 20000)[:300000], "file")
    clock = Clock()
    fields = dict(material_id=material.material_id, material_sha256=material.sha256,
                  purpose="find the cause of the error", deadline=clock.now + 300)
    fields.update(overrides)
    return material, clock, g.TransferSession(material, g.MaterialGrant(**fields), clock=clock)


class GrantValidationTests(unittest.TestCase):
    def refused(self, code, **overrides):
        with self.assertRaises(g.GrantDenied) as raised:
            make(**overrides)
        self.assertEqual(raised.exception.code, code)

    def test_grant_cannot_enable_internet_writing_or_another_recipient(self):
        self.refused("GRANT_INVALID", internet=True)
        self.refused("GRANT_INVALID", write=True)
        self.refused("GRANT_INVALID", recipient="someone-else")

    def test_purpose_budgets_and_deadline_horizon_are_bounded(self):
        for overrides in ({"purpose": ""}, {"purpose": "   "}, {"purpose": "x" * 513}, {"purpose": 7},
                          {"max_requests": 0}, {"max_requests": 9}, {"max_requests": True},
                          {"max_bytes": 0}, {"max_bytes": -1}, {"max_bytes": True}, {"max_bytes": g.MAX_TOTAL_BYTES + 1},
                          {"deadline": 1000.0 + 601}, {"deadline": "soon"}):
            with self.subTest(overrides=overrides):
                self.refused("GRANT_INVALID", **overrides)

    def test_grant_for_another_material_or_version_is_refused(self):
        self.refused("SOURCE_CHANGED", material_sha256="0" * 64)
        self.refused("SOURCE_CHANGED", material_id="0" * 16)


class PacketTests(unittest.TestCase):
    def test_packet_contains_exactly_the_requested_ranges_with_references(self):
        material, _, session = make()
        packet = session.prepare("Why does it fail?", [(0, 100), (200, 300)])
        payload = json.loads(packet.payload)
        (item,) = payload["context"]
        self.assertEqual((payload["mode"], payload["question"]), ("question", "Why does it fail?"))
        self.assertEqual([f["text"] for f in item["fragments"]],
                         [material.raw[0:100].decode(), material.raw[200:300].decode()])
        self.assertEqual(item["material"], {"id": material.material_id, "version": material.sha256,
                                            "size": material.size})
        from ai_desktop.analysis_contract import AnalysisConsent
        gate = AnalysisConsent(None, question="Why does it fail?", context=list(packet.context))
        self.assertEqual(gate.payload, packet.payload)          # the ledger hash is the transport hash
        self.assertEqual(gate.sha256, packet.sha256)
        self.assertTrue(packet.verify())
        self.assertEqual(packet.data_bytes, 200)
        self.assertEqual([r.start for r in packet.refs], [0, 200])

    def test_tampered_packet_is_not_accepted_for_hand_off(self):
        _, _, session = make()
        packet = session.prepare("q", [(0, 50)])
        forged = g.Packet(packet.packet_id, packet.payload.replace("synthetic", "SECRETS!"), packet.sha256,
                          packet.question, packet.context, packet.refs, packet.data_bytes)
        self.assertFalse(forged.verify())
        with self.assertRaises(g.GrantDenied) as raised:
            session.hand_off(forged)
        self.assertEqual(raised.exception.code, "STATE_INVALID")

    def test_scope_and_size_refusals_happen_before_any_effect(self):
        material, _, session = make()
        bad_ranges = [[], [(0, 10), (0, 10)], [(10, 5)], [(0, material.size + 1)], [(0, 40000)], "x",
                      [(0, 1)] * 9, [(1, 2, 3)], [("a", "b")], None]
        for ranges in bad_ranges:
            with self.subTest(ranges=str(ranges)[:30]):
                with self.assertRaises(g.GrantDenied) as raised:
                    session.prepare("q", ranges)
                self.assertIn(raised.exception.code, ("SCOPE_DENIED", "PACKET_LIMIT"))
        self.assertEqual(session.ledger(), [])
        with self.assertRaises(g.GrantDenied) as raised:
            session.prepare("", [(0, 10)])
        self.assertEqual(raised.exception.code, "PACKET_LIMIT")
        with self.assertRaises(g.GrantDenied):
            session.prepare("q" * 2049, [(0, 10)])

    def test_one_packet_is_limited_to_256_kib_of_data(self):
        _, _, session = make(raw=b"a" * 600000)
        ranges = [(i * 32768, (i + 1) * 32768) for i in range(8)]
        session.prepare("q", ranges)                         # exactly 256 KiB (8 full ranges) is fine
        _, _, session = make(raw=b"a" * 600000)
        with self.assertRaises(g.GrantDenied) as raised:
            session.prepare("q", ranges + [(262144, 262144 + 1)])         # a ninth range
        self.assertEqual(raised.exception.code, "SCOPE_DENIED")


class BudgetAndStateTests(unittest.TestCase):
    def run_one(self, session, start=0):
        packet = session.prepare("q", [(start, start + 100)])
        session.hand_off(packet)
        session.record_answer(packet)
        return packet

    def test_ninth_request_is_refused_before_it_is_prepared(self):
        _, _, session = make()
        for i in range(8):
            self.run_one(session, i * 100)
        with self.assertRaises(g.GrantDenied) as raised:
            session.prepare("q", [(900, 1000)])
        self.assertEqual(raised.exception.code, "BUDGET_EXHAUSTED")
        self.assertEqual(session.requests_handed_off(), 8)

    def test_prepared_but_unsent_packets_reserve_budget_and_cancel_releases_it(self):
        _, _, session = make(max_requests=2)
        first = session.prepare("q", [(0, 10)])
        session.prepare("q", [(10, 20)])
        with self.assertRaises(g.GrantDenied) as raised:
            session.prepare("q", [(20, 30)])
        self.assertEqual(raised.exception.code, "BUDGET_EXHAUSTED")
        session.cancel(first)
        session.prepare("q", [(20, 30)])
        self.assertEqual(session.requests_handed_off(), 0)  # nothing was handed over

    def test_total_data_budget_is_enforced(self):
        _, _, session = make(raw=b"a" * 600000, max_bytes=g.PACKET_BYTES)
        session.prepare("q", [(i * 32768, (i + 1) * 32768) for i in range(4)])        # 128 KiB
        session.prepare("q", [(i * 32768, (i + 1) * 32768) for i in range(4, 8)])     # +128 KiB = the 256 KiB budget
        with self.assertRaises(g.GrantDenied) as raised:
            session.prepare("q", [(262144, 262145)])             # one more byte
        self.assertEqual(raised.exception.code, "BUDGET_EXHAUSTED")

    def test_deadline_is_checked_at_prepare_and_again_right_before_hand_off(self):
        _, clock, session = make()
        packet = session.prepare("q", [(0, 10)])
        clock.now += 301
        with self.assertRaises(g.GrantDenied) as raised:
            session.hand_off(packet)
        self.assertEqual(raised.exception.code, "GRANT_EXPIRED")
        self.assertEqual(session.ledger()[0]["state"], "cancelled")
        self.assertEqual(session.requests_handed_off(), 0)
        with self.assertRaises(g.GrantDenied) as raised:
            session.prepare("q", [(0, 10)])
        self.assertEqual(raised.exception.code, "GRANT_EXPIRED")

    def test_a_packet_is_handed_off_at_most_once_and_never_retried(self):
        _, _, session = make()
        packet = session.prepare("q", [(0, 10)])
        session.hand_off(packet)
        for action in (session.hand_off, session.cancel):
            with self.assertRaises(g.GrantDenied) as raised:
                action(packet)
            self.assertEqual(raised.exception.code, "STATE_INVALID")
        session.record_unknown(packet)
        with self.assertRaises(g.GrantDenied):
            session.record_answer(packet)

    def test_unknown_outcome_counts_as_sent_and_is_not_reported_as_not_sent(self):
        _, _, session = make(max_requests=1)
        packet = session.prepare("q", [(0, 10)])
        session.hand_off(packet)
        session.record_unknown(packet)
        self.assertEqual(session.requests_handed_off(), 1)
        self.assertEqual(session.ledger()[0]["state"], "unknown")
        with self.assertRaises(g.GrantDenied) as raised:
            session.prepare("q", [(10, 20)])
        self.assertEqual(raised.exception.code, "BUDGET_EXHAUSTED")

    def test_answer_cannot_be_recorded_before_hand_off(self):
        _, _, session = make()
        packet = session.prepare("q", [(0, 10)])
        with self.assertRaises(g.GrantDenied):
            session.record_answer(packet)
        with self.assertRaises(g.GrantDenied):
            session.record_unknown(packet)

    def test_only_handed_off_references_count_as_delivered(self):
        material, _, session = make()
        packet = session.prepare("q", [(0, 10)])
        self.assertEqual(session.delivered, set())
        with self.assertRaises(m.MaterialError):
            m.check_ref(material, packet.refs[0], session.delivered)   # prepared is not delivered
        session.hand_off(packet)
        self.assertEqual(m.check_ref(material, packet.refs[0], session.delivered).ref, packet.refs[0])


class LedgerEvidenceTests(unittest.TestCase):
    def test_ledger_has_hashes_sizes_ranges_and_states_but_never_text(self):
        material, _, session = make(raw=b"CONFIDENTIAL-TOKEN-1234 appears in this synthetic log\n" * 50)
        packet = session.prepare("What is CONFIDENTIAL?", [(0, 200)])
        session.hand_off(packet)
        session.record_answer(packet)
        ledger = session.ledger()
        text = json.dumps(ledger)
        for needle in ("CONFIDENTIAL", "synthetic log", "What is"):
            self.assertNotIn(needle, text)
        entry = ledger[0]
        self.assertEqual((entry["seq"], entry["state"], entry["bytes"], entry["ranges"]), (1, "answered", 200, [[0, 200]]))
        self.assertEqual(entry["sha256"], packet.sha256)
        self.assertEqual(entry["fragment_sha256"], [m.sha256(material.raw[0:200])])
        self.assertIsNotNone(entry["handed_off_at"])

    def test_ledger_is_a_copy_and_cannot_be_edited_from_outside(self):
        _, _, session = make()
        packet = session.prepare("q", [(0, 10)])
        session.ledger()[0]["state"] = "answered"
        self.assertEqual(session.ledger()[0]["state"], "prepared")
        with self.assertRaises(g.GrantDenied):
            session.record_answer(packet)


if __name__ == "__main__":
    unittest.main()
