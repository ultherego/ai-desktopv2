"""Codex protocol and real private pipes, entirely synthetic and offline."""
import contextlib
import io
import json
import os
from pathlib import Path
import select
import signal
import sys
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

from ai_desktop import analysis_contract as contract
from ai_desktop import codex_protocol as codex


def application(*, execution=None):
    return contract.application_input(None, "Podaj wynik.", context=[], execution=execution).encode()


def record(value):
    return contract.canonical(value).encode()


def notification(method, **params):
    return record({"method": method, "params": params})


def thread_response():
    return {"thread": {"id": "thr_a", "ephemeral": True}, "model": "synthetic-model",
            "modelProvider": codex.PROVIDER, "approvalPolicy": "never", "cwd": "/work",
            "sandbox": {"type": "readOnly", "networkAccess": False}, "instructionSources": []}


def ready(protocol):
    protocol.start()
    protocol.feed(record({"id": 0, "result": {"userAgent": "synthetic"}}))
    protocol.feed(record({"id": 1, "result": thread_response()}))
    protocol.feed(record({"id": 2, "result": {"turn": {
        "id": "turn_a", "status": "inProgress", "items": [], "error": None}}}))


def item_event(protocol, kind="agentMessage", *, ident="item_a", text="Odpowiedź", phase="final_answer"):
    item = {"type": kind, "id": ident}
    if kind == "agentMessage":
        item.update(text=text, phase=phase)
    if kind == "reasoning":
        item.update(summary=["Do not show"], content=[])
    for method, timestamp in (("item/started", "startedAtMs"), ("item/completed", "completedAtMs")):
        protocol.feed(notification(method, threadId="thr_a", turnId="turn_a", item=item, **{timestamp: 123}))


def finish(protocol, status="completed", **turn):
    protocol.feed(notification("turn/completed", threadId="thr_a", turn={
        "id": "turn_a", "status": status, "items": [], "error": None, **turn}))


class CodexProtocolTests(unittest.TestCase):
    def test_fixed_command_has_no_token_or_provider_fallback(self):
        command = codex.fixed_command("/usr/lib/ai-desktop/codex", effort="high")
        self.assertEqual(command[:5], ["/usr/lib/ai-desktop/codex", "--strict-config", "app-server", "--listen", "stdio://"])
        for field in ('model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"',
                      'model_providers.openai_chatgpt_plan.base_url="https://api.openai.com/v1"',
                      'model_providers.openai_chatgpt_plan.request_max_retries=0',
                      'model_providers.openai_chatgpt_plan.stream_max_retries=0',
                      'features.shell_tool=false', 'agents.enabled=false', 'features.multi_agent=false',
                      'features.view_image=false',
                      'mcp_servers={}', 'features.hooks=false', 'web_search="disabled"',
                      'skills.bundled.enabled=false', 'features.skip_host_skill_discovery=true'):
            self.assertIn(field, command)
        self.assertFalse(any(part.startswith("model=") for part in command))
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertNotIn("OPENAI_API_KEY", " ".join(command))
        for path in ("codex", "/a/../codex", "/a//codex", "/a/./codex", "/a\0codex"):
            with self.subTest(path=path), self.assertRaises(contract.AnalysisDenied):
                codex.fixed_command(path)
        for model in ("--bad", 'model";shell', "", "x" * 97):
            with self.subTest(model=model), self.assertRaises(contract.AnalysisDenied):
                codex.fixed_command("/codex", model=model)

    def test_image_disable_uses_the_pinned_strict_config_schema(self):
        # Independent projection of the official 0.166.0 schema, not a fake
        # client accepting whatever flags this implementation happens to emit:
        # https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/config.schema.json
        # ToolsToml.additionalProperties=false and properties are exactly these
        # three fields; view_image is a boolean in the root features object.
        # Native loader/mod.rs validates -c overrides under --strict-config.
        pinned_tool_fields = {"experimental_request_user_input", "update_plan", "web_search"}
        for effort in codex.EFFORTS:
            with self.subTest(effort=effort):
                command = codex.fixed_command("/usr/lib/ai-desktop/codex", effort=effort)
                overrides = [command[index + 1] for index, item in enumerate(command) if item == "-c"]
                parsed = tomllib.loads("\n".join(overrides))
                self.assertLessEqual(set(parsed.get("tools", {})), pinned_tool_fields)
                self.assertIs(parsed["features"]["view_image"], False)
                # The pinned feature registry maps memory_tool to memories and
                # records deprecation usage even when the alias is false.
                # https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/features/src/legacy.rs
                self.assertIs(parsed["features"]["memories"], False)
                self.assertNotIn("memory_tool", parsed["features"])
                self.assertIn("--strict-config", command)

    def test_handshake_exact_pinned_schema_and_default_model_omitted(self):
        protocol = codex.Protocol(application(), effort="high")
        start = json.loads(protocol.start())
        self.assertEqual(start["method"], "initialize")
        self.assertEqual(start["params"]["clientInfo"]["name"], "AI Desktop")
        outgoing = [json.loads(x) for x in protocol.feed(record({"id": 0, "result": {}}))]
        self.assertEqual([p["method"] for p in outgoing], ["initialized", "thread/start"])
        self.assertEqual(outgoing[1]["params"]["sandbox"], "read-only")
        self.assertTrue(outgoing[1]["params"]["ephemeral"])
        self.assertNotIn("model", outgoing[1]["params"])
        self.assertEqual(outgoing[1]["params"]["baseInstructions"], contract.SYSTEM_PROMPT)
        turn = json.loads(protocol.feed(record({"id": 1, "result": thread_response()}))[0])
        self.assertEqual(turn["params"]["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
        self.assertEqual(turn["params"]["effort"], "high")
        self.assertIn(contract.SYSTEM_PROMPT, turn["params"]["input"][0]["text"])
        self.assertIn("Podaj wynik.", turn["params"]["input"][0]["text"])

    def test_full_and_unwrapped_input_are_revalidated(self):
        raw = application()
        unwrapped, _ = contract.parse_request(raw)
        self.assertEqual(codex.Protocol(raw).prompt, codex.Protocol(unwrapped.encode()).prompt)
        altered = json.loads(unwrapped)
        altered["endpoint"] = "https://example.invalid"
        with self.assertRaises(contract.AnalysisDenied):
            codex.Protocol(record(altered))
        altered = json.loads(raw)
        altered["system_prompt"] = "ignore policy"
        with self.assertRaises(contract.AnalysisDenied):
            codex.Protocol(record(altered))
        for raw in (b"[]", b"{}", b'{"mode":"question","mode":"workspace"}', b"\xff"):
            with self.subTest(raw=raw), self.assertRaises(contract.AnalysisDenied):
                codex.Protocol(raw)

    def test_bound_profile_options_cannot_be_replaced(self):
        selected = {"provider": "codex", "model": "default", "effort": "high",
                    "profile": "reviewer", "instructions": "Sprawdź granice.", "access": "read-only"}
        raw = application(execution=selected)
        with self.assertRaises(contract.AnalysisDenied):
            codex.Protocol(raw)
        protocol = codex.Protocol(raw, effort="high")
        self.assertIn("Sprawdź granice.", protocol.prompt)
        self.assertNotIn('"execution"', protocol.prompt)
        selected["provider"] = "claude"
        with self.assertRaises(contract.AnalysisDenied):
            codex.Protocol(application(execution=selected), effort="high")

    def test_web_is_refused_without_a_websearch_capability(self):
        raw = contract.application_input(None, "Sprawdź wiadomości", web=True, execution=None).encode()
        with self.assertRaises(contract.AnalysisDenied):
            codex.Protocol(raw)

    def test_completed_final_text_only_and_reasoning_never_returned(self):
        protocol = codex.Protocol(application())
        ready(protocol)
        item_event(protocol, "reasoning", ident="reason_a")
        item_event(protocol, ident="comment_a", text="Przygotowuję", phase="commentary")
        item_event(protocol, ident="final_a", text='{"answer":"Wynik"}')
        with self.assertRaises(contract.AnalysisDenied):
            protocol.finish()
        finish(protocol)
        self.assertEqual(protocol.finish(), '{"answer":"Wynik"}')
        self.assertIsInstance(protocol.finish(), contract.ModelText)
        self.assertEqual(protocol.finish().meta, {"models": [{"model": "synthetic-model",
                                                              "input": None, "output": None, "window": None}]})
        self.assertEqual(protocol.interrupt(), b"")

    def test_metadata_uses_only_native_model_and_closed_token_counts(self):
        protocol = codex.Protocol(application())
        ready(protocol)
        counts = {"cachedInputTokens": 0, "inputTokens": 23, "outputTokens": 7,
                  "reasoningOutputTokens": 1, "totalTokens": 30}
        protocol.feed(notification("thread/tokenUsage/updated", threadId="thr_a", turnId="turn_a",
                                   tokenUsage={"last": counts, "total": counts, "modelContextWindow": 123456}))
        item_event(protocol)
        finish(protocol)
        self.assertEqual(protocol.finish().meta, {"models": [{"model": "synthetic-model",
                                                              "input": 23, "output": 7, "window": 123456}]})
        for bad in (True, -1, 1.0, 10**9 + 1):
            protocol = codex.Protocol(application())
            ready(protocol)
            altered = dict(counts, inputTokens=bad)
            with self.subTest(bad=bad), self.assertRaises(contract.AnalysisDenied):
                protocol.feed(notification("thread/tokenUsage/updated", threadId="thr_a", turnId="turn_a",
                                           tokenUsage={"last": altered, "total": altered}))

    def test_thread_policy_echo_is_enforced_before_any_turn(self):
        for field, value in (("approvalPolicy", "on-request"), ("cwd", "/home"),
                             ("modelProvider", "openai"), ("sandbox", {"type": "dangerFullAccess"}),
                             ("instructionSources", ["/work/AGENTS.md"])):
            protocol = codex.Protocol(application())
            protocol.start()
            protocol.feed(record({"id": 0, "result": {}}))
            data = thread_response()
            data[field] = value
            with self.subTest(field=field), self.assertRaises(contract.AnalysisDenied):
                protocol.feed(record({"id": 1, "result": data}))

    def test_wrong_response_ids_duplicate_keys_and_nonfinite_json_fail(self):
        for data in (record({"id": 1, "result": {}}), b'{"id":0,"id":0,"result":{}}',
                     b'{"id":0,"result":{"n":NaN}}', record({"id": False, "result": {}}),
                     record({"id": 0, "result": {}, "extra": 1})):
            protocol = codex.Protocol(application())
            protocol.start()
            with self.subTest(data=data), self.assertRaises(contract.AnalysisDenied):
                protocol.feed(data)

    def test_all_server_tool_or_auth_requests_get_a_safe_refusal(self):
        for method in ("item/tool/call", "item/commandExecution/requestApproval",
                       "mcpServer/elicitation/request", "account/chatgptAuthTokens/refresh"):
            protocol = codex.Protocol(application())
            ready(protocol)
            with self.subTest(method=method), self.assertRaises(codex.RequestDenied) as failure:
                protocol.feed(record({"id": "req_a", "method": method, "params": {"secret": "do-not-show"}}))
            self.assertEqual(failure.exception.reason, "authorization")
            self.assertEqual(json.loads(failure.exception.reply)["id"], "req_a")
            self.assertNotIn("do-not-show", str(failure.exception))
            self.assertNotIn(b"do-not-show", failure.exception.reply)

    def test_native_tool_items_unknown_methods_and_cross_thread_output_fail(self):
        data = [notification("item/started", threadId="thr_a", turnId="turn_a", startedAtMs=0,
                             item={"id": "tool_a", "type": kind}) for kind in
                ("commandExecution", "fileChange", "mcpToolCall", "collabAgentToolCall", "webSearch", "imageView")]
        data += [notification("item/agentMessage/delta", threadId="thr_b", turnId="turn_a", itemId="none", delta="x"),
                 notification("unknown/future/tool", threadId="thr_a", turnId="turn_a")]
        for raw in data:
            protocol = codex.Protocol(application())
            ready(protocol)
            with self.subTest(raw=raw), self.assertRaises(contract.AnalysisDenied):
                protocol.feed(raw)

    def test_incomplete_failed_interrupted_duplicate_or_mismatched_turn_fails(self):
        for status in ("failed", "interrupted", "inProgress", "completed"):
            protocol = codex.Protocol(application())
            ready(protocol)
            with self.subTest(status=status), self.assertRaises(contract.AnalysisDenied):
                finish(protocol, status)
        protocol = codex.Protocol(application())
        ready(protocol)
        item_event(protocol)
        with self.assertRaises(contract.AnalysisDenied):
            finish(protocol, id="other_turn")
        protocol = codex.Protocol(application())
        ready(protocol)
        item_event(protocol)
        finish(protocol)
        with self.assertRaises(contract.AnalysisDenied):
            finish(protocol)

    def test_line_message_wire_item_and_final_text_budgets(self):
        with self.assertRaises(ValueError):
            codex.Limits(messages=4097)
        with self.assertRaises(ValueError):
            codex.Limits(line_bytes=True)
        protocol = codex.Protocol(application(), limits=codex.Limits(line_bytes=10))
        protocol.start()
        with self.assertRaises(contract.AnalysisDenied) as failure:
            protocol.feed(b"x" * 11)
        self.assertEqual(failure.exception.reason, "output-limit")
        protocol = codex.Protocol(application(), limits=codex.Limits(messages=1))
        protocol.start()
        protocol.feed(record({"id": 0, "result": {}}))
        with self.assertRaises(contract.AnalysisDenied) as failure:
            protocol.feed(record({"id": 1, "result": thread_response()}))
        self.assertEqual(failure.exception.reason, "output-limit")
        protocol = codex.Protocol(application(), limits=codex.Limits(wire_bytes=10))
        protocol.start()
        with self.assertRaises(contract.AnalysisDenied) as failure:
            protocol.feed(record({"id": 0, "result": {}}))
        self.assertEqual(failure.exception.reason, "output-limit")
        protocol = codex.Protocol(application(), limits=codex.Limits(items=1))
        ready(protocol)
        item_event(protocol, "reasoning")
        with self.assertRaises(contract.AnalysisDenied):
            item_event(protocol, ident="second")
        protocol = codex.Protocol(application())
        ready(protocol)
        with self.assertRaises(contract.AnalysisDenied) as failure:
            item_event(protocol, text="x" * (protocol.output_limit + 1))
        self.assertEqual(failure.exception.reason, "output-limit")


_SERVER = r'''
import json, os, sys, time
def read():
    return json.loads(sys.stdin.buffer.readline())
def send(value):
    sys.stdout.buffer.write((json.dumps(value)+"\n").encode()); sys.stdout.buffer.flush()
assert read()["method"] == "initialize"
send({"id":0,"result":{"userAgent":"offline-synthetic"}})
assert read()["method"] == "initialized"
thread=read(); assert thread["method"] == "thread/start"
assert thread["params"]["sandbox"] == "read-only"
send({"id":1,"result":{"thread":{"id":"thr_a","ephemeral":True},"model":"synthetic-model","modelProvider":"openai_chatgpt_plan",
"approvalPolicy":"never","cwd":"/work","sandbox":{"type":"readOnly","networkAccess":False},"instructionSources":[]}})
turn=read(); assert turn["method"] == "turn/start"
send({"id":2,"result":{"turn":{"id":"turn_a","status":"inProgress","items":[],"error":None}}})
MODE=os.environ.get("FIXTURE_MODE", "success")
if MODE == "tool":
    send({"id":"request_a","method":"item/tool/call","params":{"arguments":{"secret":"do-not-print"}}})
    read(); time.sleep(30)
if MODE == "flood":
    sys.stdout.write("x"*9000); sys.stdout.flush(); time.sleep(30)
if MODE == "stderr":
    sys.stderr.write("private-credential"*1000); sys.stderr.flush(); time.sleep(30)
if MODE == "hang":
    time.sleep(30)
item={"id":"message_a","type":"agentMessage","text":"syntetyczna odpowiedź","phase":"final_answer"}
send({"method":"item/started","params":{"threadId":"thr_a","turnId":"turn_a","item":item,"startedAtMs":100}})
send({"method":"item/completed","params":{"threadId":"thr_a","turnId":"turn_a","item":item,"completedAtMs":101}})
if MODE == "eof":
    sys.exit(0)
send({"method":"turn/completed","params":{"threadId":"thr_a","turn":{"id":"turn_a","status":"completed","items":[],"error":None}}})
assert sys.stdin.buffer.read() == b""
if MODE == "trailing":
    sys.stdout.write("junk\n"); sys.stdout.flush()
sys.exit(1 if MODE == "exit" else 0)
'''


class CodexPipeTests(unittest.TestCase):
    def exchange(self, mode="success", **kwargs):
        return codex.exchange([sys.executable, "-I", "-u", "-c", _SERVER],
                              codex.Protocol(application(), limits=kwargs.pop("limits", None)),
                              env={"FIXTURE_MODE": mode, "ACCESS_TOKEN": "synthetic-not-a-real-token"},
                              timeout=kwargs.pop("timeout", 3), **kwargs)

    def test_real_process_complete_and_no_console_output(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = self.exchange()
        self.assertEqual(result, "syntetyczna odpowiedź")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_real_process_partial_nonzero_and_trailing_output_are_failures(self):
        for mode in ("eof", "exit", "trailing", "tool"):
            with self.subTest(mode=mode), self.assertRaises(contract.AnalysisDenied):
                self.exchange(mode)

    def test_real_process_flood_and_stderr_are_bounded(self):
        for mode, limits in (("flood", codex.Limits(line_bytes=1024)),
                             ("stderr", codex.Limits(stderr_bytes=1024))):
            start = time.monotonic()
            with self.subTest(mode=mode), self.assertRaises(contract.AnalysisDenied) as failure:
                self.exchange(mode, limits=limits)
            self.assertEqual(failure.exception.reason, "output-limit")
            self.assertNotIn("private-credential", str(failure.exception))
            self.assertLess(time.monotonic() - start, 2)

    def test_real_process_timeout_and_cancel_stop_promptly(self):
        start = time.monotonic()
        with self.assertRaises(contract.AnalysisDenied) as failure:
            self.exchange("hang", timeout=.15)
        self.assertEqual(failure.exception.reason, "timeout")
        self.assertLess(time.monotonic() - start, 2)
        start = time.monotonic()
        with self.assertRaises(contract.AnalysisDenied) as failure:
            self.exchange("hang", cancelled=lambda: time.monotonic() - start > .15)
        self.assertEqual(failure.exception.reason, "cancelled")
        self.assertLess(time.monotonic() - start, 2)

    def test_cancellation_before_start_never_spawns(self):
        with self.assertRaises(contract.AnalysisDenied) as failure:
            codex.exchange(["/does/not/exist"], codex.Protocol(application()), env={}, cancelled=lambda: True)
        self.assertEqual(failure.exception.reason, "cancelled")

    def test_process_group_is_killed_even_when_a_descendant_keeps_pipes_open(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "heartbeat"
            readers = []
            code = ("import os,sys,time\n"
                    "p=os.fork()\n"
                    "if p:\n"
                    " time.sleep(30)\n"
                    "else:\n"
                    " def publish():\n"
                    "  with open(sys.argv[1]+'.next', 'w') as heartbeat:\n"
                    "   heartbeat.write(str(time.monotonic_ns()))\n"
                    "  os.replace(sys.argv[1]+'.next', sys.argv[1])\n"
                    " publish()\n"
                    " os.write(sys.stdout.fileno(), b'R')\n"
                    " while True:\n"
                    "  publish()\n"
                    "  time.sleep(.01)\n")
            real_popen = codex.subprocess.Popen

            def start_ready(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                try:
                    # Start the driver's timeout only once the real descendant
                    # has published its first complete heartbeat. Scheduling a
                    # slow CI interpreter must not become a startup race here.
                    reader = os.dup(process.stdout.fileno())
                    self.addCleanup(os.close, reader)
                    readers.append(reader)
                    os.set_blocking(reader, False)
                    if not select.select([reader], [], [], 3)[0]:
                        self.fail("synthetic descendant did not become ready")
                    self.assertEqual(os.read(reader, 1), b"R")
                    self.assertTrue(path.exists())
                    return process
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()
                    raise

            with patch.object(codex.subprocess, "Popen", side_effect=start_ready):
                with self.assertRaises(contract.AnalysisDenied) as failure:
                    codex.exchange([sys.executable, "-I", "-u", "-c", code, str(path)],
                                   codex.Protocol(application()), env={}, timeout=.2)
            self.assertEqual(failure.exception.reason, "timeout")
            # SIGKILL delivery to the descendant can finish after wait(parent).
            # EOF on our independent pipe reader proves it has died, without
            # confusing an in-flight final rename with a surviving process.
            # A live descendant keeps its stdout writer open and fails the test.
            self.assertTrue(select.select(readers, [], [], 3)[0], "descendant kept stdout open")
            self.assertEqual(os.read(readers[0], 1), b"")
            heartbeat = path.read_text()
            self.assertTrue(heartbeat.isdecimal())
            time.sleep(.05)
            self.assertEqual(path.read_text(), heartbeat)


if __name__ == "__main__":
    unittest.main()
