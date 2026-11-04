"""The generic planner can propose only finite configured reads, without I/O."""
import copy
import json
import unittest
from unittest.mock import patch

from ai_desktop import analysis_contract, assistant_contract as contract
from test_report_policy import report_fixture


ACCESS = {"files": [{"id": "f0", "pattern": "~/.config/niri/**"},
                    {"id": "f63", "pattern": "~/Projekty/example/**"}],
          "systemd": ["system", "user"], "weather": True}
EMPTY = {"files": [], "systemd": [], "weather": False}
READ = {"action": "read", "files": ["f0"],
        "systemd": [{"scope": "system", "view": "enabled"}], "weather": "Warszawa"}
WRITE_ACCESS = dict(ACCESS, write=[{"id": "w0", "pattern": "~/Projekty/example/*"}])
EDIT = {"action": "edit", "write": "w0", "files": ["f63"], "create": ["new.txt"], "check": "text"}


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AssistantContractTests(unittest.TestCase):
    def test_access_labels_are_copied_without_filesystem_or_network_access(self):
        source = copy.deepcopy(ACCESS)
        with patch("builtins.open", side_effect=AssertionError("unexpected read")), \
                patch("os.open", side_effect=AssertionError("unexpected read")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("unexpected network")):
            result = contract.validate_access(source)
        self.assertEqual(result, source)
        result["files"][0]["pattern"] = "other"
        result["systemd"].clear()
        self.assertEqual(source, ACCESS)
        self.assertEqual(contract.validate_access(EMPTY), EMPTY)

    def test_access_rejects_unknown_capabilities_duplicates_and_controlled_labels(self):
        cases = [None, [], dict(ACCESS, command="cat anything"), dict(ACCESS, weather=1),
                 dict(ACCESS, files={}), dict(ACCESS, systemd="system"),
                 dict(ACCESS, systemd=["system", "system"]), dict(ACCESS, systemd=["remote"]),
                 dict(ACCESS, files=[ACCESS["files"][0]] * 2)]
        for item in ({"id": "f01", "pattern": "x"}, {"id": "f64", "pattern": "x"},
                     {"id": 0, "pattern": "x"}, {"id": "f1", "pattern": "\x1b[2J"},
                     {"id": "f1", "pattern": "x\n"}, {"id": "f1", "pattern": "\u202e"},
                     {"id": "f1", "pattern": "\ud800"}, {"id": "f1", "pattern": ""},
                     {"id": "f1", "pattern": "x", "writable": True}):
            cases.append(dict(ACCESS, files=[item]))
        for value in cases:
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                contract.validate_access(value)

    def test_access_count_pattern_and_total_byte_limits_are_independent(self):
        full = dict(EMPTY, files=[{"id": f"f{i}", "pattern": f"~/p{i}/*"} for i in range(64)])
        self.assertEqual(contract.validate_access(full), full)
        for value in (dict(full, files=full["files"] + [{"id": "f64", "pattern": "x"}]),
                      dict(EMPTY, files=[{"id": "f0", "pattern": "ą" * 257}]),
                      dict(EMPTY, files=[{"id": f"f{i}", "pattern": "x" * 512} for i in range(64)])):
            with self.assertRaises(ValueError):
                contract.validate_access(value)
        edge = dict(EMPTY, files=[{"id": "f63", "pattern": "ą" * 256}])
        self.assertEqual(contract.validate_access(edge), edge)

    def test_answer_or_configured_read_plan_is_data_not_an_operation(self):
        answer = {"action": "answer", "text": "Nie mam danych bieżących.\n\tMogę wyjaśnić pojęcie."}
        with patch("subprocess.Popen", side_effect=AssertionError("unexpected operation")), \
                patch("os.open", side_effect=AssertionError("unexpected file read")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("unexpected network")):
            self.assertEqual(contract.parse_plan(wire(answer), EMPTY), answer)
            result = contract.parse_plan(wire(READ), ACCESS)
        self.assertEqual(result, READ)
        result["systemd"][0]["view"] = "inactive"
        self.assertEqual(READ["systemd"][0]["view"], "enabled")
        for view in contract.SYSTEMD_VIEWS:
            plan = dict(READ, systemd=[{"scope": "user", "view": view}])
            self.assertEqual(contract.parse_plan(wire(plan), ACCESS), plan)

    def test_web_plan_is_exact_opt_in_and_cannot_smuggle_a_query_or_local_context(self):
        permitted = dict(ACCESS, web=True)
        self.assertEqual(contract.validate_access(permitted), permitted)
        self.assertEqual(contract.parse_plan('{"action":"web"}', permitted), {"action": "web"})
        for access in (ACCESS, dict(ACCESS, web=False)):
            with self.subTest(access=access), self.assertRaises(ValueError):
                contract.parse_plan('{"action":"web"}', access)
        for value in (None, 0, 1, "true", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                contract.validate_access(dict(ACCESS, web=value))
        for extra in ({"question": "other"}, {"query": "private context"}, {"url": "http://localhost"},
                      {"files": ["f0"]}, {"context": []}, {"weather": None}, {"write": "w0"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                contract.parse_plan(wire(dict(action="web", **extra)), permitted)

    def test_optional_write_metadata_is_detached_and_cannot_be_confused_with_read_ids(self):
        result = contract.validate_access(WRITE_ACCESS)
        self.assertEqual(result, WRITE_ACCESS)
        result["write"][0]["pattern"] = "elsewhere"
        self.assertEqual(WRITE_ACCESS["write"][0]["pattern"], "~/Projekty/example/*")
        for writes in ([], None, "w0", [{"id": "f0", "pattern": "~/p/*"}],
                       [{"id": "w64", "pattern": "~/p/*"}], [{"id": "w01", "pattern": "~/p/*"}],
                       [{"id": "w0", "pattern": "~/p/*"}] * 2,
                       [{"id": "w0", "pattern": "~/p/*", "execute": True}]):
            with self.subTest(writes=writes), self.assertRaises(ValueError):
                contract.validate_access(dict(ACCESS, write=writes))

    def test_edit_plan_requires_one_configured_write_scope_and_finite_literal_selection(self):
        with patch("os.open", side_effect=AssertionError("unexpected read")), \
                patch("subprocess.Popen", side_effect=AssertionError("unexpected execution")):
            self.assertEqual(contract.parse_plan(wire(EDIT), WRITE_ACCESS), EDIT)
            for check in contract.EDIT_CHECKS:
                plan = dict(EDIT, check=check)
                self.assertEqual(contract.parse_plan(wire(plan), WRITE_ACCESS), plan)
            create_only = dict(EDIT, files=[])
            self.assertEqual(contract.parse_plan(wire(create_only), WRITE_ACCESS), create_only)
        cases = [dict(EDIT, write="w1"), dict(EDIT, write=["w0"]), dict(EDIT, write="f63"),
                 dict(EDIT, files=["f1"]), dict(EDIT, files=["f63", "f63"]),
                 dict(EDIT, create=["/etc/target"]), dict(EDIT, create=["../target"]),
                 dict(EDIT, create=["target", "target"]), dict(EDIT, create=["a"] * 33),
                 dict(EDIT, files=[], create=[]), dict(EDIT, check="sh -c anything"),
                 dict(EDIT, command="anything"), dict(EDIT, delete=["old.txt"])]
        for plan in cases:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                contract.parse_plan(wire(plan), WRITE_ACCESS)
        with self.assertRaises(ValueError):
            contract.parse_plan(wire(EDIT), ACCESS)
    def test_read_plan_cannot_expand_beyond_advertised_capabilities(self):
        for plan in (dict(READ, files=["f1"]),
                     dict(READ, files=["/etc/passwd"]),
                     dict(READ, systemd=[{"scope": "remote", "view": "all"}])):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                contract.parse_plan(wire(plan), ACCESS)
        for access in (dict(ACCESS, files=[]), dict(ACCESS, systemd=[]), dict(ACCESS, weather=False)):
            with self.subTest(access=access), self.assertRaises(ValueError):
                contract.parse_plan(wire(READ), access)
        only_files = dict(READ, systemd=[], weather=None)
        self.assertEqual(contract.parse_plan(wire(only_files), dict(EMPTY, files=ACCESS["files"])), only_files)

    def test_narrow_selectors_are_detached_data_in_read_and_edit_plans(self):
        selectors = [{"id": "f0", "path": "test/hello.py"},
                     {"id": "f0", "path": ".config/niri/**"},
                     {"id": "f0", "path": "folder/*"}, "f63"]
        for template in (READ, EDIT):
            with self.subTest(action=template["action"]), \
                    patch("os.open", side_effect=AssertionError("unexpected read")), \
                    patch("socket.getaddrinfo", side_effect=AssertionError("unexpected network")), \
                    patch("subprocess.Popen", side_effect=AssertionError("unexpected process")):
                plan = dict(template, files=selectors)
                result = contract.parse_plan(wire(plan), WRITE_ACCESS)
                self.assertEqual(result, plan)
                self.assertEqual(contract.selected_file_ids(result["files"]), ("f0", "f63"))
                result["files"][0]["path"] = "changed"
                self.assertEqual(selectors[0]["path"], "test/hello.py")
        self.assertEqual(contract.parse_plan(wire(READ), ACCESS), READ)

    def test_relative_selectors_refuse_traversal_expansion_controls_and_extra_authority(self):
        paths = ["", "/etc/passwd", "~/file", "$HOME/file", "../file", "x/../file", ".", "..",
                 "./file", "x/./file", "x//file", "x/", "*", "**", "x/**/file", "x/a*", "x/***",
                 "x/\\u002e", "x\\file", "x/?", "x/[ab]", "x/{a,b}", "x\nPRIVATE", "x\tPRIVATE",
                 "x\x1bPRIVATE", "x\u202e", "\ud800", "ą" * 257, None, True, []]
        invalid = [{"id": "f0", "path": path} for path in paths]
        invalid += [{"id": "f1", "path": "x"}, {"id": "f64", "path": "x"},
                    {"id": "f00", "path": "x"}, {"id": "w0", "path": "x"},
                    {"id": [], "path": "x"}, {"id": "f0"}, {"path": "x"},
                    {"id": "f0", "path": "x", "write": True},
                    {"id": "f0", "path": "x", "command": "PRIVATE"}]
        with patch("os.open", side_effect=AssertionError("unexpected read")):
            for selection in invalid:
                with self.subTest(selection=repr(selection)), self.assertRaises(ValueError) as error:
                    contract.parse_plan(wire(dict(READ, files=[selection])), ACCESS)
                self.assertEqual(str(error.exception), "assistant-plan")
        legal = {"id": "f0", "path": "ą" * 256}
        self.assertEqual(contract.parse_plan(wire(dict(READ, files=[legal])), ACCESS)["files"], [legal])

    def test_duplicate_selector_refusal_and_eight_selection_limit_use_full_selector_identity(self):
        selectors = [{"id": "f0", "path": f"folder/file{index}.txt"} for index in range(8)]
        self.assertEqual(contract.parse_plan(wire(dict(READ, files=selectors)), ACCESS)["files"], selectors)
        self.assertEqual(contract.selected_file_ids(selectors), ("f0",))
        for bad in ([selectors[0], dict(selectors[0])], ["f0", "f0"],
                    selectors + [{"id": "f0", "path": "folder/ninth.txt"}]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                contract.parse_plan(wire(dict(READ, files=bad)), ACCESS)
        mixed = ["f0", selectors[0]]
        self.assertEqual(contract.parse_plan(wire(dict(READ, files=mixed)), ACCESS)["files"], mixed)
        raw = '{"action":"read","files":[{"id":"f0","path":"a"},{"id":"f0","path":"\\u0061"}],"systemd":[],"weather":null}'
        with self.assertRaises(ValueError):
            contract.parse_plan(raw, ACCESS)

    def test_prompt_describes_narrowing_without_claiming_a_new_grant(self):
        prompt = analysis_contract.SYSTEM_PROMPT
        for marker in ('{\"id\":\"f0\",\"path\":\"katalog/plik.txt\"}', 'files=[]',
                       "NIE ZGADUJ", "bez tworzenia katalogow", "nigdy"):
            self.assertIn(marker, prompt)

    def test_read_plan_requires_exact_keys_nonempty_selection_and_unique_scopes(self):
        cases = [dict(READ, command="rm anything"), dict(READ, files="f0"), dict(READ, files=["f0", "f0"]),
                 dict(READ, systemd=[{"scope": "system", "view": "active", "command": "anything"}]),
                 dict(READ, systemd=[{"scope": "system", "view": "all"}] * 2),
                 dict(READ, systemd=[{"scope": "user", "view": "restart"}]),
                 dict(READ, files=[], systemd=[], weather=None),
                 {"action": "read", "files": ["f0"]}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                contract.parse_plan(wire(value), ACCESS)
        access = dict(EMPTY, files=[{"id": f"f{i}", "pattern": f"~/p{i}"} for i in range(9)])
        plan = {"action": "read", "files": [f"f{i}" for i in range(8)], "systemd": [], "weather": None}
        self.assertEqual(contract.parse_plan(wire(plan), access), plan)
        plan["files"].append("f8")
        with self.assertRaises(ValueError):
            contract.parse_plan(wire(plan), access)

    def test_weather_is_a_bounded_city_label_not_an_arbitrary_provider_request(self):
        for city in (True, {}, "", " ", "a", "ą" * 61, "Warszawa\nPRIVATE", "\u202eWarszawa"):
            with self.subTest(city=city), self.assertRaises(ValueError):
                contract.parse_plan(wire(dict(READ, weather=city)), ACCESS)
        with self.assertRaises(ValueError):
            contract.parse_plan(wire(dict(READ, url="https://elsewhere.example")), ACCESS)

    def test_json_duplicates_constants_wrappers_controls_and_limits_are_closed(self):
        cases = [None, b"{}", "", "[]", "null", "{}", "```json\n{}\n```", wire(READ) + " extra",
                 '{"action":"answer","text":"a","text":"PRIVATE"}',
                 '{"action":"read","files":[],"systemd":[{"scope":"system","scope":"user","view":"all"}],"weather":null}',
                 '{"action":"answer","text":NaN}', '{"action":"answer","text":Infinity}',
                 '{"action":"answer","text":"\\u001b[2J"}',
                 wire({"action": "answer", "text": " "}),
                 wire({"action": "answer", "text": "text", "files": ["f0"]}),
                 "[" * 1100 + "]" * 1100, "x" * 16385,
                 wire({"action": "answer", "text": "ą" * 8192})]
        for raw in cases:
            with self.subTest(raw=repr(raw)[:90]), self.assertRaises(ValueError) as error:
                contract.parse_plan(raw, ACCESS)
            self.assertEqual(str(error.exception), "assistant-plan")

    def test_assistant_request_roundtrip_includes_only_question_and_access(self):
        gate = analysis_contract.AnalysisConsent(None, question="Sprawdź konfigurację", access=ACCESS)
        approval = gate.authorize(None, question="Sprawdź konfigurację", access=ACCESS,
                                  sha256=gate.sha256, nonce=gate.nonce)
        decoded, sha = analysis_contract.parse_request(approval.payload.encode())
        self.assertEqual(json.loads(decoded), {"mode": "assistant", "question": "Sprawdź konfigurację",
                                             "access": ACCESS})
        self.assertEqual(sha, approval.sha256)
        value = json.loads(approval.payload)
        self.assertEqual((value["schema"], value["policy"]), (7, "a-plus-assistant-v7"))
        for key in ("context", "report", "task"):
            self.assertNotIn(key, value)

    def test_access_changes_expiry_and_replay_require_fresh_consent(self):
        for field, value in (("files", []), ("systemd", ["user"]), ("weather", False)):
            access = copy.deepcopy(ACCESS)
            gate = analysis_contract.AnalysisConsent(None, question="Pytanie", access=access)
            access[field] = value
            with self.subTest(field=field), self.assertRaises(analysis_contract.AnalysisDenied):
                gate.authorize(None, question="Pytanie", access=access, sha256=gate.sha256, nonce=gate.nonce)
            with self.assertRaises(analysis_contract.AnalysisDenied):
                gate.authorize(None, question="Pytanie", access=ACCESS, sha256=gate.sha256, nonce=gate.nonce)
        now = [0]
        gate = analysis_contract.AnalysisConsent(None, question="Pytanie", access=ACCESS, clock=lambda: now[0])
        now[0] = 181
        with self.assertRaises(analysis_contract.AnalysisDenied):
            gate.authorize(None, question="Pytanie", access=ACCESS, sha256=gate.sha256, nonce=gate.nonce)

    def test_assistant_mode_cannot_mix_data_modes_or_reuse_final_answer_consent(self):
        for report, options in ((report_fixture(), {}), (None, {"context": []}),
                                (None, {"task": {"instruction": "x", "files": []}})):
            with self.assertRaises(ValueError):
                analysis_contract.AnalysisConsent(report, question="Pytanie", access=ACCESS, **options)
        for question in ("", " "):
            with self.assertRaises(ValueError):
                analysis_contract.AnalysisConsent(None, question=question, access=ACCESS)
        gate = analysis_contract.AnalysisConsent(None, question="Pytanie", access=ACCESS)
        with self.assertRaises(analysis_contract.AnalysisDenied):
            gate.authorize(None, question="Pytanie", context=[], sha256=gate.sha256, nonce=gate.nonce)
        original = json.loads(gate.payload)
        for key, value in (("context", []), ("mode", "question"), ("schema", 3), ("policy", "other")):
            with self.subTest(key=key), self.assertRaises(analysis_contract.AnalysisDenied):
                analysis_contract.parse_request(analysis_contract.canonical(dict(original, **{key: value})).encode())


if __name__ == "__main__":
    unittest.main()
