"""Native account-only RPC, entirely synthetic: no installed client, login or network."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from ai_desktop import codex_account_protocol as accounts, codex_protocol as rpc
from ai_desktop.analysis_contract import AnalysisDenied
from ai_desktop.codex_runtime import client_environment


LOGIN = "176edcb1-d306-4bf5-84e5-31d40b026e51"
WINDOW = {"usedPercent": 23, "windowDurationMins": 300, "resetsAt": 1880002600}


def record(values):
    return json.dumps(values).encode()


def reply(state, ident, data):
    return [json.loads(item) for item in state.feed(record({"id": ident, "result": data}))]


def notify(state, method, data):
    return [json.loads(item) for item in state.feed(record({"method": method, "params": data, "emittedAtMs": 12}))]


def begin(action, *, challenge=None):
    state = accounts.AccountProtocol(action, challenge=challenge)
    assert json.loads(state.start())["method"] == "initialize"
    outgoing = reply(state, 0, {"userAgent": "synthetic"})
    assert outgoing[0] == {"method": "initialized", "params": {}}
    assert outgoing[1]["method"] == ("account/read" if action == "read" else "account/login/start")
    return state


def active(state):
    return reply(state, 2, {"account": {"type": "chatgpt", "email": "private@example.invalid",
                                        "planType": "pro", "workspaceName": "private"}, "requiresOpenaiAuth": True})


def challenge_reply(state, url=accounts.DEVICE_URL, code="AB23-4ZQ8", login_id=LOGIN):
    return reply(state, 1, {"type": "chatgptDeviceCode", "loginId": login_id,
                           "verificationUrl": url, "userCode": code})


class AccountProtocolTests(unittest.TestCase):
    def test_device_challenge_completes_only_after_matching_event_and_confirmed_native_account(self):
        show = Mock()
        state = begin("connect", challenge=show)
        self.assertEqual(challenge_reply(state), [])
        show.assert_called_once_with(accounts.DEVICE_URL, "AB23-4ZQ8")
        self.assertEqual(json.loads(state.interrupt()), {"id": 99, "method": "account/login/cancel",
                                                       "params": {"loginId": LOGIN}})
        self.assertEqual(notify(state, "account/updated", {"authMode": "chatgpt", "planType": "pro"}), [])
        self.assertFalse(state.done)
        self.assertEqual(notify(state, "account/login/completed",
                                {"loginId": LOGIN, "success": True, "error": None, "onboardingEntrypoint": None}),
                         [{"id": 2, "method": "account/read", "params": {"refreshToken": False}}])
        self.assertEqual(active(state), [])
        self.assertEqual(state.finish(), {"windows": {}, "plan": "pro"})
        self.assertEqual(state.interrupt(), b"")
        self.assertTrue(state.done)

    def test_untrusted_device_addresses_codes_and_login_ids_are_not_displayed(self):
        for url, code, login_id in (
            ("https://auth.openai.com.evil.invalid/codex/device", "AB23-4ZQ8", LOGIN),
            (accounts.DEVICE_URL + "?token=private", "AB23-4ZQ8", LOGIN),
            ("http://auth.openai.com/codex/device", "AB23-4ZQ8", LOGIN),
            (accounts.DEVICE_URL, "fake\x1b[41m", LOGIN),
            (accounts.DEVICE_URL, "a" * 33, LOGIN),
            (accounts.DEVICE_URL, "ABCD", "not-a-uuid"),
        ):
            with self.subTest(url=url, code=code), self.assertRaises(AnalysisDenied):
                show = Mock()
                state = begin("connect", challenge=show)
                challenge_reply(state, url, code, login_id)
            show.assert_not_called()

    def test_login_error_or_swapped_id_cannot_connect(self):
        for ident, success, error, reason in (
            (LOGIN, False, "private provider text", "auth-required"),
            ("00000000-0000-4000-8000-000000000000", True, None, "result"),
            (LOGIN, True, "private provider text", "result"),
        ):
            state = begin("connect", challenge=Mock())
            challenge_reply(state)
            with self.assertRaises(AnalysisDenied) as caught:
                notify(state, "account/login/completed", {"loginId": ident, "success": success, "error": error})
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("private", str(caught.exception))
            self.assertFalse(state.done)

    def test_only_the_main_pool_is_published_with_real_window_measurements(self):
        state = begin("read")
        self.assertEqual(active(state), [{"id": 3, "method": "account/rateLimits/read",
                                        "params": {"excludeResetCreditDetails": True}}])
        main = {"limitId": "codex", "planType": "plus", "secondary": WINDOW,
                "primary": {**WINDOW, "usedPercent": 8, "windowDurationMins": 10080}}
        reply(state, 3, {"rateLimits": {"limitId": "different"}, "rateLimitsByLimitId": {"codex": main},
                         "rateLimitResetCredits": {"private": "metadata"}, "accountId": "private",
                         "rateLimitUpsell": {"title": "private"}})
        self.assertEqual(state.finish(), {"plan": "plus", "windows": {
            "five_hour": {"percent": 23, "reset": 1880002600},
            "seven_day": {"percent": 8, "reset": 1880002600}}})

    def test_empty_legacy_or_non_main_pool_is_not_a_fresh_zero(self):
        for data in ({"rateLimits": {"limitId": "codex", "primary": None}},
                     {"rateLimits": {"limitId": "other", "primary": WINDOW}},
                     {"rateLimits": {"primary": WINDOW}, "rateLimitsByLimitId": {"aux": {"primary": WINDOW}}},
                     {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 0}}}):
            with self.subTest(data=data):
                state = begin("read")
                active(state)
                reply(state, 3, data)
                self.assertEqual(state.finish(), {"windows": {}, "plan": "pro"})
        state = begin("read")
        active(state)
        reply(state, 3, {"rateLimits": {"primary": {**WINDOW, "usedPercent": 0}}})
        self.assertEqual(state.finish()["windows"]["five_hour"]["percent"], 0)

    def test_no_session_and_wrong_auth_mode_require_device_login(self):
        for account, reason in ((None, "auth-required"), ({"type": "apiKey"}, "auth-method")):
            state = begin("read")
            with self.assertRaises(AnalysisDenied) as caught:
                reply(state, 2, {"requiresOpenaiAuth": True, "account": account})
            self.assertEqual(caught.exception.reason, reason)
        state = begin("read")
        with self.assertRaises(AnalysisDenied) as caught:
            reply(state, 2, {"requiresOpenaiAuth": False, "account": None})
        self.assertEqual(caught.exception.reason, "result")

    def test_ignore_only_expected_metadata_and_never_use_unrequested_observations(self):
        state = begin("read")
        notify(state, "configWarning", {"summary": rpc.NESTED_USERNS_WARNING, "details": None})
        notify(state, "account/rateLimits/updated", {"rateLimits": {"primary": WINDOW}})
        with self.assertRaises(AnalysisDenied):
            state.finish()
        for data in (
            {"id": 9, "method": "account/chatgptAuthTokens/refresh", "params": {}},
            {"method": "execCommand", "params": {}},
            {"result": {}, "id": 3},
            {"id": 2, "error": {"code": -32001, "message": "sensitive text"}},
        ):
            with self.subTest(data=data), self.assertRaises(AnalysisDenied) as caught:
                state.feed(record(data))
            self.assertNotIn("sensitive", str(caught.exception))

    def test_logout_notice_invalidates_pending_quota_response(self):
        state = begin("read")
        active(state)
        with self.assertRaises(AnalysisDenied) as error:
            notify(state, "account/updated", {"authMode": None, "planType": None})
        self.assertEqual(error.exception.reason, "auth-required")

    def test_record_budgets_and_invalid_nested_data_are_refused(self):
        state = begin("read")
        active(state)
        for raw in (b'{"id":3,"result":{"rateLimits":{"primary":NaN}}}', b'{"id":2,"id":3,"result":{}}',
                    record({"id": 3, "result": {"rateLimits": {"primary": {"usedPercent": True}}}})):
            with self.subTest(raw=raw), self.assertRaises(AnalysisDenied):
                state.feed(raw)
        for raw in (b"", b"0" * (state.limits.line_bytes + 1)):
            with self.assertRaises(AnalysisDenied) as caught:
                state.feed(raw)
            self.assertEqual(caught.exception.reason, "output-limit")
        state.messages = state.limits.messages
        with self.assertRaises(AnalysisDenied):
            state.feed(b"{}")

    def test_fixed_native_mode_and_environment_cannot_fall_back_to_siwc_or_keys(self):
        command = accounts.fixed_command("/opt/codex")
        self.assertEqual(command[:5], ["/opt/codex", "--strict-config", "app-server", "--listen", "stdio://"])
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertIn('cli_auth_credentials_store="file"', command)
        self.assertIn('features.shell_tool=false', command)
        self.assertNotIn("env_key", str(command))
        with self.assertRaises(AnalysisDenied):
            accounts.run("/opt/codex", "read", {**client_environment(), "ACCESS_TOKEN": "fake"})
        with self.assertRaises(AnalysisDenied):
            accounts.run(Path("/opt/codex"), "turn", client_environment())
        with patch.object(rpc, "_exchange", return_value={"plan": "pro", "windows": {}}) as transport:
            self.assertEqual(accounts.run(Path("/opt/codex"), "read", client_environment())["plan"], "pro")
            self.assertEqual(transport.call_args.args[0], command)
            self.assertEqual(transport.call_args.kwargs["timeout"], 18)

    def test_bounded_pipe_terminates_actual_child_after_read(self):
        program = """
import json,sys
def read(method):
    value=json.loads(sys.stdin.readline())
    assert value['method']==method
    return value
def send(ident,result):
    print(json.dumps({'id':ident,'result':result}), flush=True)
send(read('initialize')['id'], {'userAgent':'synthetic'})
read('initialized')
send(read('account/read')['id'], {'account':{'type':'chatgpt','planType':'free'},'requiresOpenaiAuth':True})
send(read('account/rateLimits/read')['id'], {'rateLimits':{'primary':None}})
assert sys.stdin.read()==''
"""
        result = rpc._exchange([sys.executable, "-I", "-c", program], accounts.AccountProtocol("read"),
                               env={}, timeout=5)
        self.assertEqual(result, {"windows": {}, "plan": "free"})
