"""Offline tests for the native-stream probe. No native process or socket."""
import copy
from email.message import Message
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import host_web_stream_probe as probe

SECRET = "PRIVATE token URL question identifier"


def wire(events):
    return ("\n".join(json.dumps(item) for item in events) + "\n").encode()


def observation():
    return {"schema": 1, "boundary": True, "version": probe.VERSION,
        "failure": "none", "native_exit": 0, "requests": 1, "messages": 1,
        "first": "command_lifecycle/queued", "init_index": 1, "init_count": 1,
        "events": ["command_lifecycle/queued", "system/init", "assistant", "result/success"],
        "oauth": False, "upstream": False}


def request():
    headers = Message()
    headers["Authorization"] = "Bearer SYNTHETIC"
    body = {"model": "fixture-model", "tools": [{"name": "WebSearch", "description": "synthetic"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": probe.MARKER}]}],
            "stream": True}
    return headers, body


class WebStreamProbeTests(unittest.TestCase):
    def test_init_before_or_after_lifecycle_is_observed_not_accepted_as_search(self):
        for prefix in ([], [{"type": "command_lifecycle", "state": "queued"},
                            {"type": "command_lifecycle", "state": "started"}]):
            events = prefix + [{"type": "system", "subtype": "init", "session_id": SECRET},
                               {"type": "assistant", "message": SECRET},
                               {"type": "result", "subtype": "success", "result": SECRET}]
            shape = probe.stream_shape(wire(events))
            self.assertEqual(shape["init_index"], len(prefix))
            self.assertEqual(shape["init_count"], 1)
            self.assertNotIn(SECRET, repr(shape))
            self.assertNotIn("sources", shape)

    def test_missing_or_duplicate_init_remains_visible(self):
        self.assertEqual(probe.stream_shape(wire([{"type": "assistant"}]))["init_index"], -1)
        shape = probe.stream_shape(wire([{"type": "system", "subtype": "init"}] * 2))
        self.assertEqual(shape["init_count"], 2)

    def test_known_event_labels_do_not_forward_native_fields(self):
        for subtype in probe.SYSTEM_KINDS:
            event = {"type": "system", "subtype": subtype, "message": SECRET, "uuid": SECRET}
            self.assertEqual(probe.event_name(event), "system/" + subtype)
        for state in probe.STATES:
            self.assertEqual(probe.event_name({"type": "command_lifecycle", "state": state,
                "command_uuid": SECRET}), "command_lifecycle/" + state)
        for name in probe.PLAIN_KINDS:
            self.assertEqual(probe.event_name({"type": name, "content": SECRET}), name)

    def test_unknown_names_and_wrong_types_never_escape(self):
        for event, expected in (({"type": SECRET}, "other"), ({"type": []}, "other"),
                ({"type": "system", "subtype": SECRET}, "system/other"),
                ({"type": "system", "subtype": {}}, "system/other"),
                ({"type": "command_lifecycle", "state": SECRET}, "command_lifecycle/other"),
                ({"type": "result", "subtype": SECRET}, "result/other"),
                ({"type": "result", "is_error": True, "result": SECRET}, "result/error")):
            self.assertEqual(probe.event_name(event), expected)

    def test_raw_stream_is_bounded_strict_json_and_never_printed(self):
        for raw in (b"", b"private", b"[]\n", b"{}\n\n", b'{"type":1,"type":2}',
                    b'{"x": NaN}', b"x" * (probe.MAX_BYTES + 1), wire([{}] * (probe.MAX_EVENTS + 1))):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                probe.stream_shape(raw)

    def test_observation_roundtrip_allows_only_closed_schema(self):
        data = observation()
        self.assertEqual(probe.validate_observation(json.dumps(data).encode()), data)
        for key, value in (("failure", SECRET), ("version", SECRET), ("oauth", True),
                ("upstream", True), ("boundary", False), ("schema", True), ("native_exit", True),
                ("native_exit", 256), ("requests", 33), ("messages", -1), ("messages", True),
                ("first", SECRET), ("events", [SECRET]), ("events", []), ("events", {}),
                ("init_index", True), ("init_index", 9), ("init_count", 9), ("init_count", True)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                probe.validate_observation(json.dumps({**data, key: value}).encode())
        for data in ({**observation(), "secret": SECRET}, [], {"error": SECRET}):
            with self.assertRaises(ValueError):
                probe.validate_observation(json.dumps(data).encode())

    def test_fixture_failure_is_not_misrepresented_as_success(self):
        data = {**observation(), "failure": "fixture", "native_exit": 1, "messages": 0}
        self.assertEqual(probe.validate_observation(json.dumps(data).encode()), data)
        data["failure"] = "none"
        with self.assertRaises(ValueError):
            probe.validate_observation(json.dumps(data).encode())

    def test_observation_rejects_duplicate_fields_and_wrong_root(self):
        for raw in (b"[]", b'{"schema":1,"schema":1}', b"x" * 8193, None):
            with self.subTest(raw=str(raw)[:30]), self.assertRaises(ValueError):
                probe.validate_observation(raw)

    def test_synthetic_request_accepts_only_marker_and_websearch(self):
        headers, body = request()
        for content in (probe.MARKER, [{"type": "text", "text": "fixed reminder"},
                                      {"type": "text", "text": probe.MARKER}]):
            body["messages"][0]["content"] = content
            self.assertEqual(probe.validate_fixture_request("/v1/messages?beta=true", headers,
                json.dumps(body).encode(), model="fixture-model", token="SYNTHETIC"), body)

    def test_fixture_refuses_wrong_credentials_paths_tools_and_messages(self):
        headers, base = request()
        variants = []
        for key, value in (("tools", []), ("tools", [{"name": "Bash"}]),
                           ("tools", [{"name": "WebSearch"}, {"name": "Read"}]),
                           ("model", "other"), ("messages", []), ("stream", "true")):
            variants.append({**base, key: value})
        for content in (SECRET, [{"type": "image", "text": probe.MARKER}], None):
            changed = copy.deepcopy(base)
            changed["messages"][0]["content"] = content
            variants.append(changed)
        for body in variants:
            with self.assertRaises(ValueError):
                probe.validate_fixture_request("/v1/messages", headers, json.dumps(body).encode(),
                                               model="fixture-model", token="SYNTHETIC")
        for path in ("http://example.org", "/oauth", "/v1/messages/other"):
            with self.assertRaises(ValueError):
                probe.validate_fixture_request(path, headers, json.dumps(base).encode(),
                                               model="fixture-model", token="SYNTHETIC")
        for name in ("Cookie", "x-api-key", "Transfer-Encoding", "Authorization"):
            modified = copy.deepcopy(headers)
            modified[name] = SECRET
            with self.assertRaises(ValueError):
                probe.validate_fixture_request("/v1/messages", modified, json.dumps(base).encode(),
                                               model="fixture-model", token="SYNTHETIC")

    def test_native_invocation_reuses_web_flags_but_never_profile_or_uplink(self):
        original = ["/usr/lib/ai-desktop/claude-code", "--restricted", "--safe-mode",
                    "-p", "--tools", "WebSearch", "--output-format", "stream-json"]
        production = SimpleNamespace(BINARY=Path(original[0]), client_command=lambda mode: list(original),
            client_environment=lambda mode: {"HTTP_PROXY": "http://127.0.0.1:18080",
                "HTTPS_PROXY": "http://127.0.0.1:18080", "HOME": "/home/client",
                "CLAUDE_CONFIG_DIR": "/home/client/profile", "CLAUDE_CODE_SAFE_MODE": "1"})
        fixture = SimpleNamespace(MODEL="fixture-model", client_environment=lambda port: {
            "HOME": "/home/model", "CLAUDE_CONFIG_DIR": "/home/model/profile",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:" + str(port), "ANTHROPIC_AUTH_TOKEN": "SYNTHETIC"})
        with patch.dict(probe.os.environ, {"ANTHROPIC_API_KEY": SECRET, "HTTPS_PROXY": SECRET}):
            command, env = probe.native_invocation(production, fixture, 12345)
        self.assertEqual(command, ["/opt/claude", *original[1:], "--model", "fixture-model"])
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn(SECRET, repr(env))
        self.assertNotIn("--bare", command)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/model/profile")

    def test_parent_refuses_without_opt_in_or_as_root_before_importing_runtime(self):
        for args, uid in (([], 1000), ([probe.FLAG], 0), (["--allow-ai-request"], 1000)):
            output = io.StringIO()
            with patch.object(probe.os, "getuid", return_value=uid), \
                 patch.object(probe.os, "geteuid", return_value=uid), \
                 patch.object(probe.sys, "stdout", output):
                self.assertEqual(probe.main(args), 2)
            self.assertIn("REFUSED", output.getvalue())


if __name__ == "__main__":
    unittest.main()
