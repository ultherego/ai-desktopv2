"""Credential-free compatibility RPCs and bounded synthetic pipe failures."""
import json
import sys
import unittest
from unittest.mock import patch

from ai_desktop import codex_compat as probe, codex_protocol as rpc, codex_runtime
from ai_desktop.analysis_contract import AnalysisDenied


MODELS = {"data": [{"id": "synthetic-model", "displayName": "Synthetic", "hidden": False}], "nextCursor": None}


def record(ident, result):
    return json.dumps({"id": ident, "result": result}).encode()


def started():
    protocol = probe.ProbeProtocol()
    protocol.start()
    return protocol


def with_account():
    protocol = started()
    protocol.feed(record(0, {"userAgent": "synthetic"}))
    return protocol


def with_models():
    protocol = with_account()
    protocol.feed(record(1, {"account": None, "requiresOpenaiAuth": True}))
    return protocol


class ProbeTests(unittest.TestCase):
    def test_only_initialize_signed_out_account_and_model_catalog_are_requested(self):
        protocol = probe.ProbeProtocol()
        start = json.loads(protocol.start())
        self.assertEqual(start["method"], "initialize")
        messages = [json.loads(row) for row in protocol.feed(record(0, {"userAgent": "synthetic"}))]
        self.assertEqual([row["method"] for row in messages], ["initialized", "account/read"])
        self.assertEqual(messages[1]["params"], {"refreshToken": False})
        message = json.loads(protocol.feed(record(1, {"account": None, "requiresOpenaiAuth": True}))[0])
        self.assertEqual(message, {"id": 2, "method": "model/list", "params": {"limit": 64, "includeHidden": False}})
        self.assertEqual(protocol.feed(record(2, MODELS)), [])
        self.assertTrue(protocol.done)
        self.assertEqual(protocol.finish(), MODELS)
        self.assertEqual(protocol.interrupt(), b"")

    def test_analysis_config_probe_only_initializes_without_an_account_or_model_request(self):
        protocol = probe.ProbeProtocol(native_account=False)
        self.assertEqual(json.loads(protocol.start())["method"], "initialize")
        messages = [json.loads(row) for row in protocol.feed(record(0, {"userAgent": "synthetic"}))]
        self.assertEqual(messages, [])
        self.assertTrue(protocol.done)
        self.assertIsNone(protocol.finish())

    def test_any_authenticated_or_non_chatgpt_empty_account_is_refused(self):
        for result in ({"account": {}, "requiresOpenaiAuth": True},
                       {"account": None, "requiresOpenaiAuth": False}, {"account": None},
                       {"account": None, "requiresOpenaiAuth": True, "token": "synthetic-secret"}):
            with self.subTest(result=result), self.assertRaises(AnalysisDenied):
                with_account().feed(record(1, result))

    def test_wrong_ids_duplicates_nonfinite_data_and_native_requests_are_refused(self):
        records = [b'{"id":true,"result":{}}', b'{"id":0,"id":0,"result":{}}',
                   b'{"id":0,"result":{"userAgent":NaN}}', record(5, {}), b'[]', b'\xff',
                   b'{"id":9,"method":"account/chatgptAuthTokens/refresh","params":{}}',
                   b'{"method":"item/commandExecution/requestApproval","params":{}}']
        for raw in records:
            with self.subTest(raw=raw), self.assertRaises(AnalysisDenied):
                started().feed(raw)

    def test_initialize_format_and_incomplete_exchange_are_refused(self):
        for result in ({}, {"userAgent": 1}, {"userAgent": ""}, {"userAgent": "bad\n"},
                       {"userAgent": "x" * 513}):
            with self.subTest(result=result), self.assertRaises(AnalysisDenied):
                started().feed(record(0, result))
        with self.assertRaises(AnalysisDenied):
            with_models().finish()
        protocol = started()
        with self.assertRaises(AnalysisDenied):
            protocol.start()

    def test_model_shapes_and_limits_are_checked_without_returning_unrelated_fields(self):
        for result in ({}, {"data": []}, {"data": "not-list"}, {"data": [None]},
                       {"data": [{"id": "bad id"}]}, {"data": MODELS["data"] * 2},
                       {"data": MODELS["data"], "nextCursor": "bad\n"},
                       {"data": MODELS["data"], "secret": "do not return"}):
            with self.subTest(result=result), self.assertRaises(AnalysisDenied):
                with_models().feed(record(2, result))
        protocol = with_models()
        protocol.feed(record(2, {"data": [{"id": "synthetic-model", "displayName": "bad\x1b",
                                           "upstreamExtra": "not returned"}]}))
        self.assertEqual(protocol.finish(), {"data": [{"id": "synthetic-model", "displayName": "synthetic-model", "hidden": False}],
                                            "nextCursor": None})

    def test_message_and_line_limits_are_enforced(self):
        protocol = started()
        protocol.messages = protocol.limits.messages
        with self.assertRaisesRegex(AnalysisDenied, "output-limit"):
            protocol.feed(record(0, {"userAgent": "synthetic"}))
        with self.assertRaisesRegex(AnalysisDenied, "output-limit"):
            started().feed(b"x" * (protocol.limits.line_bytes + 1))

    def test_transport_uses_locked_account_config_and_no_token_environment(self):
        with patch.object(probe.rpc, "_exchange", return_value=MODELS) as exchange:
            self.assertEqual(probe.run(str(codex_runtime.BINARY), codex_runtime.client_environment()), MODELS)
        command, protocol = exchange.call_args.args
        self.assertEqual(exchange.call_count, 2)
        analysis = exchange.call_args_list[0].args[0]
        self.assertIn('model_provider="openai_chatgpt_plan"', analysis)
        self.assertIn('model_reasoning_effort="high"', analysis)
        self.assertIn('cli_auth_credentials_store="ephemeral"', analysis)
        self.assertFalse(exchange.call_args_list[0].args[1].native_account)
        self.assertIn("--strict-config", command)
        self.assertIn('features.shell_tool=false', command)
        self.assertNotIn("ACCESS_TOKEN", exchange.call_args.kwargs["env"])
        self.assertTrue(exchange.call_args.kwargs["stop_after_reply"])
        self.assertIsInstance(protocol, probe.ProbeProtocol)
        with self.assertRaises(AnalysisDenied):
            probe.run(str(codex_runtime.BINARY), codex_runtime.client_environment("synthetic-token"))

    def test_analysis_configuration_failure_stops_before_native_account_probe(self):
        with patch.object(probe.rpc, "_exchange", side_effect=AnalysisDenied("client")) as exchange, \
                self.assertRaises(AnalysisDenied):
            probe.run(str(codex_runtime.BINARY), codex_runtime.client_environment())
        exchange.assert_called_once()
        self.assertFalse(exchange.call_args.args[1].native_account)

    def test_real_synthetic_pipes_exercise_sequential_requests_without_inference(self):
        source = '''import json,sys
for line in sys.stdin:
    value=json.loads(line)
    method=value['method']
    if method=='initialized': continue
    if method=='initialize': result={'userAgent':'synthetic'}
    elif method=='account/read':
        assert value['params']=={'refreshToken':False}
        result={'account':None,'requiresOpenaiAuth':True}
    elif method=='model/list': result={'data':[{'id':'synthetic-model','displayName':'Synthetic','hidden':False}],'nextCursor':None}
    else: raise SystemExit(8)
    print(json.dumps({'id':value['id'],'result':result}),flush=True)
'''
        result = rpc._exchange([sys.executable, "-I", "-B", "-c", source], probe.ProbeProtocol(),
                               env={"PATH": "/usr/bin"}, timeout=3, stop_after_reply=True)
        self.assertEqual(result, MODELS)

    def test_unresponsive_synthetic_process_times_out(self):
        with self.assertRaisesRegex(AnalysisDenied, "timeout"):
            rpc._exchange([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"],
                          probe.ProbeProtocol(), env={"PATH": "/usr/bin"}, timeout=.15, stop_after_reply=True)


if __name__ == "__main__":
    unittest.main()
