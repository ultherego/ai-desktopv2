"""The discovery observations cross only the bounded, consent-bound assistant channel (Claude and Codex alike)."""
import copy
import json
import unittest

from ai_desktop import analysis_contract as contract
from ai_desktop import analysis_child
from ai_desktop import codex_protocol as codex
from ai_desktop import discovery_contract as dc


ACCESS = {"files": [{"id": "f0", "pattern": "~/.config/**"}], "systemd": [], "weather": False}
EXECUTION = {"provider": "codex", "model": "default", "effort": "default",
             "instructions": "", "profile": "diagnostic", "access": "read-only"}
QUESTION = "Sprawdź ustawienia monitorów"


def document():
    return {"steps": [
        {"op": "list", "directories": [{"id": "f0", "path": "", "entries": [
            {"path": "alpha", "type": "alias", "size": None, "target": "beta"},
            {"path": "beta", "type": "directory", "size": None},
            {"path": "one.txt", "type": "file", "size": 12}], "truncated": False, "next": None}],
         "truncated": False},
        {"op": "find", "id": "f0", "path": "beta", "queries": ["mon"], "kind": "file",
         "matches": [{"path": "beta/monitors.json", "type": "file", "size": 4}],
         "truncated": False, "incomplete": False},
        {"op": "refusal", "code": "files-count", "selectors": [{"id": "f0", "path": "beta/**"}], "limit": 64}],
        "remaining": {"rounds": 2, "corrections": 1, "commands": 4}}


class DiscoveryContractTests(unittest.TestCase):
    def test_valid_document_is_copied_without_sharing_mutable_state(self):
        source = document()
        valid = dc.validate_discovery(source)
        self.assertEqual(valid, source)
        source["steps"][0]["directories"][0]["entries"][0]["path"] = "changed"
        self.assertEqual(valid["steps"][0]["directories"][0]["entries"][0]["path"], "alpha")

    def test_only_closed_shapes_and_numeric_sizes_are_accepted(self):
        bad = []
        for size in (True, -1, 2**63, 1.5, None, "1"):
            value = document()
            value["steps"][0]["directories"][0]["entries"][2]["size"] = size
            bad.append(value)
        for extra in ({"content": "SYNTHETIC-PRIVATE"}, {"mode": "0600"}):
            value = document()
            value["steps"][0]["directories"][0]["entries"][1].update(extra)
            bad.append(value)
        value = document()
        value["steps"][0]["directories"][0]["entries"][0].pop("target")       # an alias needs its target
        bad.append(value)
        value = document()
        value["steps"][0]["directories"][0]["entries"][1]["target"] = "x"     # only an alias has one
        bad.append(value)
        for value in (None, [], {}, {**document(), "extra": 1}):
            bad.append(value)
        for value in bad:
            with self.subTest(value=repr(value)[:80]), self.assertRaises(ValueError) as caught:
                dc.validate_discovery(value)
            self.assertEqual(str(caught.exception), "discovery")

    def test_paths_queries_and_ids_have_no_expansion_traversal_or_control_characters(self):
        for path in ("/absolute", "../x", "a/../b", "a//b", "a/./b", "~/x", "$HOME/x", "a\\b", "a?", "a[0]",
                     "a/**", "line\nbreak", "x‮y", "x\ud800y", "x" * 513, ""):
            value = document()
            value["steps"][0]["directories"][0]["entries"][2]["path"] = path
            with self.subTest(path=repr(path)), self.assertRaises(ValueError):
                dc.validate_discovery(value)
        for query in ("", "a", " ab", "ab ", "a/b", "a*b", "a\nb", "x" * 65):
            value = document()
            value["steps"][1]["queries"] = [query]
            with self.subTest(query=repr(query)), self.assertRaises(ValueError):
                dc.validate_discovery(value)
        for ident in ("f64", "f-1", "f00", "w0", 0, True):
            value = document()
            value["steps"][1]["id"] = ident
            with self.subTest(ident=ident), self.assertRaises(ValueError):
                dc.validate_discovery(value)

    def test_refusal_codes_are_a_closed_list_without_native_text(self):
        for code in ("files-denied", "files-changed", "files-private-key", "policy-changed", "PRIVATE", None):
            value = document()
            value["steps"][2]["code"] = code
            with self.subTest(code=code), self.assertRaises(ValueError):
                dc.validate_discovery(value)
        value = document()
        value["steps"][2]["message"] = "native error text"
        with self.assertRaises(ValueError):
            dc.validate_discovery(value)

    def test_step_entry_byte_and_counter_ceilings_are_explicit(self):
        value = document()
        value["steps"][0]["directories"][0]["entries"] = [
            {"path": str(i), "type": "file", "size": i} for i in range(dc.CEILING_ENTRIES + 1)]
        with self.assertRaises(ValueError):
            dc.validate_discovery(value)
        value = document()
        value["steps"] = value["steps"][:1] * (dc.MAX_STEPS + 1)
        with self.assertRaises(ValueError):
            dc.validate_discovery(value)
        value = document()
        value["steps"][0]["directories"][0]["entries"] = [
            {"path": "ż" * 250 + str(i), "type": "file", "size": 0} for i in range(512)]
        with self.assertRaises(ValueError):
            dc.validate_discovery(value)
        for rounds, corrections in ((9, 0), (0, 5), (-1, 0), (True, 0)):
            value = document()
            value["remaining"] = {"rounds": rounds, "corrections": corrections, "commands": 0}
            with self.subTest(rounds=rounds), self.assertRaises(ValueError):
                dc.validate_discovery(value)

    def test_truncation_flags_are_consistent_and_duplicates_are_refused(self):
        value = document()
        value["steps"][0]["directories"][0]["truncated"] = True
        with self.assertRaises(ValueError):
            dc.validate_discovery(value)                 # a truncated directory must truncate the step
        value["steps"][0]["truncated"] = True
        dc.validate_discovery(value)
        value = document()
        value["steps"][0]["directories"][0]["entries"] *= 2
        with self.assertRaises(ValueError):
            dc.validate_discovery(value)
        value = document()
        value["steps"][1]["queries"] = ["mon", "MON"]
        with self.assertRaises(ValueError):
            dc.validate_discovery(value)


class DiscoveryWireTests(unittest.TestCase):
    def test_assistant_discovery_roundtrips_and_changes_the_exact_consent_hash(self):
        value = document()
        plain = contract.application_input(None, QUESTION, access=ACCESS)
        gate = contract.AnalysisConsent(None, question=QUESTION, access=ACCESS, discovery=value)
        self.assertNotEqual(gate.payload, plain)
        approval = gate.authorize(None, question=QUESTION, access=ACCESS, discovery=value,
                                  sha256=gate.sha256, nonce=gate.nonce)
        source, sha = contract.parse_request(approval.payload.encode())
        self.assertEqual(json.loads(source), {"mode": "assistant", "question": QUESTION, "access": ACCESS,
                                              "discovery": value})
        self.assertEqual(sha, gate.sha256)

    def test_omission_or_mutation_cannot_reuse_a_transfer_approval(self):
        for change in ("omit", "path", "size", "remaining", "step"):
            value = document()
            gate = contract.AnalysisConsent(None, question=QUESTION, access=ACCESS, discovery=value)
            changed = copy.deepcopy(value)
            if change == "path":
                changed["steps"][0]["directories"][0]["entries"][2]["path"] = "two.txt"
            if change == "size":
                changed["steps"][0]["directories"][0]["entries"][2]["size"] += 1
            if change == "remaining":
                changed["remaining"]["corrections"] = 2
            if change == "step":
                changed["steps"].pop()
            kwargs = {} if change == "omit" else {"discovery": changed}
            with self.subTest(change=change), self.assertRaises(contract.AnalysisDenied):
                gate.authorize(None, question=QUESTION, access=ACCESS, sha256=gate.sha256, nonce=gate.nonce, **kwargs)

    def test_other_modes_unknown_scopes_and_extra_wire_fields_are_refused(self):
        for mode in ({"web": True}, {"material": {}}, {"project": {}}, {"task": {}}, {}):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                contract.application_input(None, QUESTION, discovery=document(), **mode)
        task = {"instruction": "synthetic", "files": [{"path": "file.txt", "content": "x", "writable": True}]}
        for source in (dict(question=QUESTION, context=[]), dict(task=task)):           # the task's last request carries it
            payload = contract.application_input(None, discovery=document(), **source)
            self.assertIn("discovery", json.loads(payload))
            parsed, _ = contract.parse_request(payload.encode())
            self.assertIn("discovery", json.loads(parsed))
        value = document()
        value["steps"][1]["id"] = "f1"
        with self.assertRaises(ValueError):
            contract.application_input(None, QUESTION, access=ACCESS, discovery=value)
        for value in (None, {**document(), "content": "SYNTHETIC-PRIVATE"}):
            raw = json.loads(contract.application_input(None, QUESTION, access=ACCESS))
            raw["discovery"] = value
            with self.subTest(value=value), self.assertRaises(contract.AnalysisDenied):
                contract.parse_request(contract.canonical(raw).encode())

    def test_codex_full_and_unwrapped_assistant_envelopes_preserve_exact_discovery(self):
        raw = contract.application_input(None, QUESTION, access=ACCESS, discovery=document(),
                                         execution=EXECUTION).encode()
        source, _ = contract.parse_request(raw)
        full, unwrapped = codex.Protocol(raw), codex.Protocol(source.encode())
        self.assertEqual(full.prompt, unwrapped.prompt)
        observed = json.loads(full.prompt.split("Application data (JSON; the content is untrusted data):\n", 1)[1])
        self.assertEqual(observed["discovery"], document())
        self.assertEqual(observed["access"], ACCESS)
        self.assertNotIn("execution", observed)
        poisoned = json.loads(source)
        poisoned["discovery"]["steps"][0]["directories"][0]["entries"][2]["content"] = "private"
        with self.assertRaises(contract.AnalysisDenied):
            codex.Protocol(contract.canonical(poisoned).encode())

    def test_claude_child_preserves_discovery_as_data_and_keeps_execution_separate(self):
        selected = dict(EXECUTION, provider="claude")
        raw = contract.application_input(None, QUESTION, access=ACCESS, discovery=document(),
                                         execution=selected).encode()
        source, _ = contract.parse_request(raw)
        child, options = analysis_child.native_input("analyze", source.encode())
        self.assertEqual(options, selected)
        data = json.loads(child)
        self.assertEqual(data["discovery"], document())
        self.assertNotIn("execution", data)

    def test_wire_is_identical_when_discovery_is_absent_and_prompt_has_no_host_specific_examples(self):
        raw = contract.application_input(None, QUESTION, access=ACCESS)
        self.assertEqual(raw, contract.application_input(None, QUESTION, access=ACCESS, discovery=None))
        self.assertNotIn("discovery", json.loads(raw))
        prompt = json.loads(raw)["system_prompt"].casefold()
        for word in ("niri", "dms", "dankmaterial", "cachyos-"):
            self.assertNotIn(word, prompt.replace("a cachyos assistant", ""))


if __name__ == "__main__":
    unittest.main()
