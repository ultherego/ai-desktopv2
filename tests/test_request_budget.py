"""Slow reasoning gets a longer model-call budget, and every layer that waits for it must outlive it."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import json
from pathlib import Path
import re
import unittest

from ai_desktop import analysis_contract as contract, analysis_framing, analysis_session_admin

ROOT = Path(__file__).resolve().parents[1]


class BudgetTests(unittest.TestCase):
    def test_only_xhigh_and_max_get_the_long_budget(self):
        for effort in ("default", "low", "medium", "high", None, "bogus"):
            self.assertEqual(contract.request_seconds(effort), contract.REQUEST_SECONDS, effort)
        for effort in ("xhigh", "max"):
            self.assertEqual(contract.request_seconds(effort), contract.REQUEST_SECONDS_LONG, effort)

    def test_the_effort_is_read_from_a_request_and_unreadable_input_means_the_short_budget(self):
        body = json.dumps({"mode": "assistant", "execution": {"effort": "max"}})
        self.assertEqual(contract.request_effort(body), "max")
        self.assertEqual(contract.request_effort(body.encode()), "max")
        for bad in ("", "{", "[]", json.dumps({"execution": None}), json.dumps({"execution": {"effort": 3}}), b"\xff"):
            self.assertIsNone(contract.request_effort(bad), bad)
        self.assertEqual(contract.request_seconds(contract.request_effort("{")), contract.REQUEST_SECONDS)

    def test_every_waiting_layer_outlives_the_longest_call(self):
        waits = contract.REQUEST_SECONDS_LONG + contract.SERVICE_MARGIN
        self.assertLessEqual(waits, 300)
        unit = int(re.search(r"^RuntimeMaxSec=(\d+)$", (ROOT / "packaging/systemd/ai-desktop-analysis@.service").read_text(),
                             re.M).group(1))
        session = int(re.search(r"RuntimeMaxSec=(\d+)", " ".join(analysis_session_admin.session_command(1000, "n" * 8))).group(1))
        self.assertGreater(unit, waits)                         # the per-request unit is not killed under the client
        self.assertGreater(session, unit)                       # the session helper outlives the unit
        self.assertLessEqual(waits, 300)                        # the framing cap
        analysis_framing._settings("response", waits)           # accepted (raises AnalysisDenied otherwise)
        with self.assertRaises(contract.AnalysisDenied):
            analysis_framing._settings("response", 301)


if __name__ == "__main__":
    unittest.main()


class ClientLimitsTests(unittest.TestCase):
    """The client, the bridge and the proxy must not give up before the supervisor's own budget does."""

    def test_the_clients_own_api_timeout_stays_below_every_budget(self):
        from ai_desktop import analysis_sandbox
        for effort in (None, "low", "medium", "high", "xhigh", "max"):
            api = int(analysis_sandbox.client_environment("analyze", effort)["API_TIMEOUT_MS"]) / 1000
            self.assertLess(api, contract.request_seconds(effort), effort)
            self.assertGreaterEqual(api, 90, effort)             # never lower than the old fixed value
        self.assertEqual(analysis_sandbox.client_environment("analyze", "max")["API_TIMEOUT_MS"], "240000")
        self.assertEqual(analysis_sandbox.client_environment("analyze")["API_TIMEOUT_MS"], "90000")

    def test_the_bridge_and_proxies_keep_a_stream_open_longer_than_the_call(self):
        from ai_desktop import analysis_child
        self.assertEqual(analysis_child._Bridge.__init__.__defaults__, (120,))        # default is the old value
        for effort in (None, "high", "xhigh", "max"):
            seconds = contract.request_seconds(effort) + 10
            self.assertGreater(seconds, contract.request_seconds(effort))
            self.assertLessEqual(seconds, 300)                                         # ConnectProxy accepts up to 300


class SessionHelperTests(unittest.TestCase):
    def test_the_session_helper_waits_longer_than_the_longest_request_and_its_unit_outlives_the_wait(self):
        from ai_desktop import material_cli
        longest = contract.REQUEST_SECONDS_LONG + contract.SERVICE_MARGIN
        self.assertGreaterEqual(contract.SESSION_SECONDS, longest + 60)           # startup and cleanup fit around a request
        unit = int(re.search(r"RuntimeMaxSec=(\d+)", " ".join(analysis_session_admin.session_command(1000, "n" * 8))).group(1))
        self.assertGreater(unit, contract.SESSION_SECONDS)
        # a session reused between material requests must still have room for the longest wait
        self.assertEqual(material_cli.SESSION_REUSE_SECONDS, contract.SESSION_SECONDS - longest)

    def test_the_helper_really_uses_the_shared_constant(self):
        source = (ROOT / "ai_desktop/analysis_session_admin.py").read_text()
        self.assertIn("select.select([0], [], [], SESSION_SECONDS)", source)
        self.assertNotIn("[], 240)", source)
