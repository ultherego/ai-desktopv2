"""Sparse subscription observations retain the provider's values and their own timestamps."""
import json
import unittest

from ai_desktop.account_store import Accounts, ProfileError, decode
from ai_desktop.analysis_contract import validate_meta
from tests.test_codex_rate_limits import rate_message, record, turn_ready


class SparseQuotaTests(unittest.TestCase):
    def test_upgrade_older_store_and_merge_out_of_order_per_account(self):
        initial = {"five_hour": {"percent": 2, "reset": 700},
                   "seven_day": {"percent": 4, "reset": 900}}
        state = decode(json.dumps({"schema": 1, "rows": [["codex", "default", True, 400, initial]]}))
        latest = {"seven_day": {"percent": 7, "reset": 960}}
        state = state.note("codex", "default", 800, latest)
        first_retain = {"percent": 2, "reset": 700, "observed_at": 400}
        self.assertEqual(state.rows[0][3:], (800, {"five_hour": first_retain, **latest}))
        # A delayed response may update a stale 5h value but never replace a newer 7d value.
        state = state.note("codex", "default", 600, {
            "five_hour": {"percent": 3, "reset": 750}, "seven_day": {"percent": 1, "reset": 920}})
        self.assertEqual(state.rows[0][3:], (800, {
            "five_hour": {"percent": 3, "reset": 750, "observed_at": 600}, **latest}))
        state = state.add("codex", "work").note("codex", "work", 850, latest)
        self.assertEqual(state.rows[1][3:], (850, latest))
        self.assertEqual(decode(json.dumps(state.data())), state)

    def test_only_store_can_supply_observation_times(self):
        value = {"models": [], "quota": {"five_hour": {"percent": 3, "reset": 750, "observed_at": 600}}}
        with self.assertRaises(ValueError):
            validate_meta(value)
        state = Accounts()
        with self.assertRaises(ValueError):
            state.note("codex", "default", 800, value["quota"])
        for stamp in (True, 0, -1, 801, "600"):
            windows = {"seven_day": {"percent": 1, "reset": 950},
                       "five_hour": {"percent": 5, "reset": 900, "observed_at": stamp}}
            with self.subTest(stamp=stamp), self.assertRaises(ProfileError):
                decode(json.dumps({"schema": 1, "rows": [["codex", "default", True, 800, windows]]}))

    def test_pinned_plans_survive_without_fabricating_quota(self):
        for plan in ("ent26", "sci", "sci_plus"):
            protocol = turn_ready()
            protocol.feed(record(rate_message({"limitId": "codex", "planType": plan})))
            self.assertEqual(protocol.meta["plan"], plan)
            self.assertNotIn("quota", protocol.meta)
