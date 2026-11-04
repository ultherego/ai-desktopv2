"""Schema-only diagnostics for an isolated synthetic stream, never production."""
import copy
import io
import json
import unittest
from unittest.mock import patch

import host_web_stream_probe as probe

SECRET = "lowercase_secret_value_never_exported"


def events(subtype="startup_notice"):
    return [
        {"type": "system", "subtype": subtype, "session_id": SECRET,
         "uuid": SECRET, "payload": {"token": SECRET, "nested": [SECRET]},
         "text": SECRET, "flag": True, "count": 1, "delay": 0.5, "nothing": None},
        {"type": "system", "subtype": "init", "session_id": SECRET},
        {"type": "assistant", "message": SECRET},
        {"type": "result", "subtype": "success", "result": SECRET},
    ]


def shape(value=None):
    return probe.stream_shape(("\n".join(json.dumps(e) for e in (events() if value is None else value)) + "\n").encode())


def observation(value=None):
    return {"schema": 1, "boundary": True, "version": probe.VERSION,
            "failure": "none", "native_exit": 0, "requests": 1, "messages": 1,
            "oauth": False, "upstream": False, **shape(value)}


def validate(value):
    return probe.validate_observation(json.dumps(value).encode())


class WebStreamHeaderTests(unittest.TestCase):
    def test_subtype_and_top_level_types_survive_without_values(self):
        result = shape()
        self.assertEqual(result["first"], "system/other")
        header = result["preinit"]
        self.assertEqual(header["subtype"], "startup_notice")
        self.assertEqual(header["fields"], {"type": "str", "subtype": "str", "session_id": "str",
            "uuid": "str", "payload": "dict", "text": "str", "flag": "bool", "count": "int",
            "delay": "float", "nothing": "null"})
        self.assertEqual(header["session"], "same")
        self.assertEqual(header["redacted_fields"], 0)
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn("token", header["fields"])
        self.assertNotIn("nested", header["fields"])
        self.assertEqual(validate(observation()), observation())

    def test_subtype_values_outside_identifier_grammar_are_redacted(self):
        for value in (None, True, 1, [], {}, "", "https://private.example", "/private/path",
                      "hello world", "\x1b[31m", "x\ny", "żółć", "a" * 49, "x-y", "9invalid"):
            with self.subTest(value=value):
                result = shape(events(value))
                self.assertEqual(result["preinit"]["subtype"], "redacted")
                validate(observation(events(value)))
                self.assertNotIn(SECRET, repr(result))
        for value in ("a", "_schema", "camelCase_2", "a" * 48):
            self.assertEqual(shape(events(value))["preinit"]["subtype"], value)

    def test_field_names_are_bounded_and_values_never_descended(self):
        value = events()
        value[0]["https://private.example"] = SECRET
        value[0]["unsafe\nname"] = SECRET
        value[0]["a" * 49] = SECRET
        value[0]["items"] = [{SECRET: SECRET}]
        result = shape(value)
        self.assertEqual(result["preinit"]["redacted_fields"], 3)
        self.assertEqual(result["preinit"]["fields"]["items"], "list")
        for hidden in (SECRET, "https://private.example", "unsafe", "a" * 49):
            self.assertNotIn(hidden, repr(result))
        validate(observation(value))

    def test_session_relation_never_prints_either_identifier(self):
        for current, expected in ((SECRET, "same"), ("another_private_value", "different"),
                                   (None, "invalid"), (True, "invalid"), ([], "invalid"),
                                   ("", "invalid"), ("a" * 257, "invalid")):
            value = events()
            value[0]["session_id"] = current
            self.assertEqual(shape(value)["preinit"]["session"], expected)
        value = events()
        del value[0]["session_id"]
        self.assertEqual(shape(value)["preinit"]["session"], "missing")
        value = events()
        value[1]["session_id"] = None
        self.assertEqual(shape(value)["preinit"]["session"], "invalid")

    def test_no_header_when_not_a_unique_later_init_or_first_not_system(self):
        for value in (events()[1:], [events()[0]], events() + [events()[1]],
                      [{"type": "command_lifecycle", "state": "queued"}, *events()[1:]]):
            with self.subTest(value=value):
                self.assertNotIn("preinit", shape(value))

    def test_parent_rejects_forged_header_shapes_and_values(self):
        base = observation()
        variants = [None, [], {}, {**base["preinit"], "extra": SECRET}]
        for key, bad in (("subtype", SECRET + " with spaces"), ("subtype", True),
                         ("session", SECRET), ("session", []), ("redacted_fields", True),
                         ("redacted_fields", -1), ("redacted_fields", 33),
                         ("fields", []), ("fields", {}), ("fields", {"type": "int"}),
                         ("fields", {"type": "str", "unsafe\nkey": "str"}),
                         ("fields", {"type": "str", "subtype": "str", "safe": SECRET}),
                         ("fields", {"type": "str", "subtype": []})):
            variants.append({**base["preinit"], key: bad})
        for header in variants:
            with self.subTest(header=header), self.assertRaises(ValueError):
                validate({**base, "preinit": header})

    def test_parent_rejects_header_on_failed_fixture_or_mismatched_event(self):
        base = observation()
        for update in ({"failure": "fixture"}, {"native_exit": 1}, {"messages": 0}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate({**base, **update})
        value = events()
        value[0]["subtype"] = "status"
        data = observation(value)
        self.assertEqual(data["first"], "system/status")
        validate(data)
        data["preinit"]["subtype"] = "startup_notice"
        with self.assertRaises(ValueError):
            validate(data)
        for value in (events()[1:], events() + [events()[1]],
                      [{"type": "assistant"}, *events()[1:]]):
            data = observation(value)
            data["preinit"] = base["preinit"]
            with self.assertRaises(ValueError):
                validate(data)

    def test_header_field_budget_and_total_observation_budget_remain_bounded(self):
        value = events()
        value[0].update({f"field_{i}": SECRET for i in range(probe.MAX_HEADER_FIELDS)})
        with self.assertRaises(ValueError):
            shape(value)
        data = observation()
        data["preinit"]["redacted_fields"] = probe.MAX_HEADER_FIELDS
        with self.assertRaises(ValueError):
            validate(data)
        with self.assertRaises(ValueError):
            probe.validate_observation(b" " * 8193)

    def test_terminal_formatter_revalidates_and_never_prints_native_values(self):
        output = io.StringIO()
        with patch.object(probe.sys, "stdout", output):
            probe.print_preinit(shape()["preinit"])
        text = output.getvalue()
        self.assertIn("PREINIT_SUBTYPE=startup_notice", text)
        self.assertIn("PREINIT_SESSION=same", text)
        self.assertIn("payload:dict", text)
        self.assertNotIn(SECRET, text)
        bad = copy.deepcopy(shape()["preinit"])
        bad["subtype"] = "unsafe\nlog"
        output = io.StringIO()
        with patch.object(probe.sys, "stdout", output), self.assertRaises(ValueError):
            probe.print_preinit(bad)
        self.assertEqual(output.getvalue(), "")

    def test_diagnostics_cannot_claim_search_acceptance(self):
        data = validate(observation())
        self.assertEqual(data["events"], ["system/other", "system/init", "assistant", "result/success"])
        for prohibited in ("search_pass", "sources", "answer", "oauth_token", "raw"):
            with self.subTest(key=prohibited), self.assertRaises(ValueError):
                validate({**data, prohibited: SECRET})


if __name__ == "__main__":
    unittest.main()
