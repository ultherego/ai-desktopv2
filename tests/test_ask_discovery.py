"""Multi-step discovery of `ask`: the model finds data by itself, the owner's permissions.toml stays the only authority.

Everything here is offline: a scripted stand-in replaces the model and records the exact consented payloads. Names are
neutral on purpose; no program, desktop environment or distribution is special-cased anywhere in the code under test.
"""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import pwd
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import assistant_contract as plans, assistant_flow as flow, directory_context, execution, permissions
from ai_desktop import ask_discovery, discovery_contract as dc, permissions as p
from ai_desktop.analysis_contract import AnalysisConsent, AnalysisDenied, Approval

QUESTION = "Jakie panele ma skonfigurowane Gizmo?"
ANSWER = "Odpowiedź wyłącznie z odczytanych danych."
ANSWER_PLAN = json.dumps({"action": "answer", "text": ANSWER})
DENIED_WORDS = ("vault", "secret.txt", "token.txt", "permissions.toml", "PRIVATE-DENIED")


def options(**changes):
    return SimpleNamespace(**{"no_context": False, "preview": False, "details": False,
                             "systemd": None, "systemd_state": None, "weather": None, **changes})


def plan(action, *files, **extra):
    return json.dumps({"action": action, "files": list(files), **extra})


def read(*files):
    return json.dumps({"action": "read", "files": list(files), "systemd": [], "weather": None})


def sel(path, ident="f0"):
    return {"id": ident, "path": path}


class Base(unittest.TestCase):
    policy_extra = ""

    def setUp(self):
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="aid-discovery-", dir=real_home)))
        self.uid = os.getuid()
        self.enterContext(patch.object(permissions, "_identity", return_value=(self.uid, str(self.home))))
        self.config = self.home / ".config/ai-desktop/permissions.toml"
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.build_tree()
        self.write_policy(self.policy_extra)
        self.reader = Mock(side_effect=AssertionError("unexpected additional prompt"))
        self.confirm = self.enterContext(patch.object(flow, "confirm", return_value=True))
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.reads = []
        real_read = permissions._read
        def spy(path, *args, **kwargs):
            if str(path) != str(self.config):
                self.reads.append(str(path))
            return real_read(path, *args, **kwargs)
        self.enterContext(patch.object(permissions, "_read", side_effect=spy))

    def write_policy(self, extra="", deny=()):
        denied = ["~/.config/ai-desktop/", "~/.config/vault/", "~/.config/gizmo/token.txt", *deny]
        self.config.write_text('version = 2\n[network]\nclaude = true\ncodex = true\n[files]\n'
                               'read = ["~/.config/**"]\n'
                               'deny = ' + json.dumps(denied) + '\n'
                               + extra, encoding="utf-8")
        self.config.chmod(0o600)

    def file(self, relative, content="x\n"):
        path = self.home / ".config" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def link(self, name, target):
        os.symlink(target, self.home / ".config" / name)

    def build_tree(self):
        self.file("gizmo/settings.conf", "mode=basic\n")
        self.file("gizmo/display/outputs.conf", "output=A\n")
        self.file("gizmo/display/profiles/office.conf", "office=1\n")
        self.file("gizmo/token.txt", "PRIVATE-DENIED token\n")
        self.file("gizmo-suite/panels.json", '{"panels":["top","bottom"]}')
        self.file("gizmo-suite/notes/readme.txt", "notes\n")
        self.file("vault/secret.txt", "PRIVATE-DENIED secret\n")
        self.file("bulk/small.conf", "small\n")
        for index in range(210):
            self.file("bulk/zz-data-%03d.conf" % index, "SYNTHETIC UNUSED\n")
        self.link("GizmoSuite", "gizmo-suite")           # directory alias to a literal sibling
        self.link("loop-a", "loop-b")
        self.link("loop-b", "loop-a")
        self.link("ghost", "does-not-exist")
        self.link("up", "../elsewhere")
        self.link("file-link", "bulk")                    # a link to a directory with a slash-free name is an alias too
        os.symlink("../gizmo/settings.conf", self.home / ".config/gizmo-suite/settings-link")

    def run_flow(self, responses, *, question=QUESTION, provider="claude", before=None, instructions="",
                 access="read-only", skills=None):
        observed, attempts = [], []
        self.wire, self.attempts = observed, attempts
        self.reads.clear()
        replies = iter(responses)

        def request(text, args, reader, **kwargs):
            attempts.append(kwargs)
            permit = kwargs["question_consent"]
            if before is not None:
                before(len(attempts), kwargs)
            fields = {name: kwargs[name] for name in ("access", "context", "discovery", "web") if name in kwargs}
            gate = AnalysisConsent(None, question=text, **fields)
            permit.claim(gate, text, **fields)
            approval = permit.authorize(gate, text, **fields)
            self.assertIs(type(approval), Approval)
            observed.append(json.loads(approval.payload))
            if "discovery" in kwargs:
                text = json.dumps(kwargs["discovery"])
                for marker in ("content", "SYNTHETIC UNUSED", "mode=basic", "output=A", "office=1", "panels\":["):
                    self.assertNotIn(marker, text, "discovery carried file content")
            item = next(replies)
            return item(kwargs.get("discovery")) if callable(item) else item

        profile = {"provider": provider, "model": "default", "effort": "default", "instructions": instructions,
                   "profile": "diagnostic", "access": access}
        with execution.using(profile):
            return flow.run(question, options(), self.reader, request, skills=skills)

    def refused(self, responses, **kwargs):
        with self.assertRaises(flow.file_context.FileReadDenied):
            self.run_flow(responses, **kwargs)
        return self.output.getvalue()

    def steps(self, index):
        return self.wire[index]["discovery"]["steps"]

    def assertNoDeniedData(self):
        text = json.dumps(self.wire, ensure_ascii=False)
        for word in DENIED_WORDS:
            self.assertNotIn(word, text)


class SkillsOnDemandTests(Base):
    BODY = "Procedura: krok pierwszy, krok drugi."

    def load(self, name):
        return {"przyklad": self.BODY, "inny": "NIE-WYSYLAC"}.get(name)

    def test_the_body_leaves_only_after_the_model_asks_and_only_that_skill(self):
        result = self.run_flow([json.dumps({"action": "skill", "names": ["przyklad"]}), ANSWER_PLAN],
                               instructions="Katalog: przyklad, inny", skills=self.load)
        self.assertEqual(result, ANSWER)
        first, second = self.wire
        self.assertNotIn(self.BODY, json.dumps(first, ensure_ascii=False))
        self.assertEqual(self.steps(1), [{"op": "skill", "name": "przyklad", "status": "ok", "text": self.BODY}])
        self.assertNotIn("NIE-WYSYLAC", json.dumps(self.wire, ensure_ascii=False))
        self.assertIn("Skill przyklad", self.output.getvalue())

    def test_a_name_outside_the_selection_is_reported_as_missing_without_stopping(self):
        self.run_flow([json.dumps({"action": "skill", "names": ["brak"]}), ANSWER_PLAN], skills=self.load)
        self.assertEqual(self.steps(1), [{"op": "skill", "name": "brak", "status": "missing", "text": ""}])

    def test_asking_repeatedly_is_bounded_and_a_skill_is_sent_once(self):
        again = json.dumps({"action": "skill", "names": ["przyklad"]})
        with self.assertRaises(flow.PlanError):
            self.run_flow([again] * 5, skills=self.load)
        self.assertEqual(sum(step["op"] == "skill" for step in self.steps(-1)), 1)

    def test_a_mode_without_a_loader_refuses_the_action(self):
        with self.assertRaises(flow.PlanError):
            self.run_flow([json.dumps({"action": "skill", "names": ["przyklad"]}), ANSWER_PLAN])

    def test_a_path_named_by_a_loaded_skill_counts_like_a_typed_one_and_deny_still_wins(self):
        ledger = flow.ask_discovery.DiscoveryLedger(p.load_policy(), QUESTION, "")
        ledger.add_skill("przyklad", "Przeczytaj ~/.config/gizmo/settings.conf")
        self.assertIn("~/.config/gizmo/settings.conf", ledger.hints)
        self.assertFalse(p.load_policy().allows(str(self.home) + "/.config/gizmo/token.txt"))

    def test_the_plan_and_the_wire_step_are_closed(self):
        access = {"files": [], "systemd": [], "weather": False}
        for bad in ({"action": "skill"}, {"action": "skill", "names": []}, {"action": "skill", "names": ["../x"]},
                    {"action": "skill", "names": ["a", "a"]}, {"action": "skill", "names": ["a"] * 5},
                    {"action": "skill", "names": ["a"], "path": "x"}):
            with self.assertRaises(plans.PlanError):
                plans.parse_plan(json.dumps(bad), access)
        step = {"op": "skill", "name": "a", "status": "ok", "text": "t"}
        remaining = {"rounds": 1, "corrections": 1, "commands": 1}
        self.assertEqual(dc.validate_discovery({"steps": [step], "remaining": remaining})["steps"], [step])
        for bad in ({**step, "name": "../x"}, {**step, "status": "x"}, {**step, "text": "\x1b[2J"},
                    {**step, "status": "missing"}, {**step, "extra": 1}, {**step, "text": "x" * (96 * 1024 + 1)}):
            with self.assertRaises(ValueError):
                dc.validate_discovery({"steps": [bad], "remaining": remaining})


class FullFlowTests(Base):
    def test_model_finds_the_alias_directory_lists_it_and_reads_one_exact_file_under_one_approval(self):
        result = self.run_flow([
            plan("find", "f0", queries=["gizmo"], kind="directory"),
            plan("list", sel("GizmoSuite")),
            read(sel("GizmoSuite/panels.json")),
            ANSWER])
        self.assertEqual(result, ANSWER)
        self.assertEqual([body["mode"] for body in self.wire], ["assistant", "assistant", "assistant", "question"])
        self.assertNotIn("discovery", self.wire[0])
        found = [entry["path"] + ":" + entry["type"] for entry in self.steps(1)[0]["matches"]]
        self.assertEqual(found, ["GizmoSuite:alias", "gizmo:directory", "gizmo-suite:directory"])
        self.assertEqual(self.steps(2)[:1], self.steps(1))                 # cumulative, earlier steps untouched
        self.assertEqual([entry["path"] for entry in self.steps(2)[1]["directories"][0]["entries"]],
                         ["GizmoSuite/notes", "GizmoSuite/panels.json"] + ["GizmoSuite/settings-link"][:0])
        self.assertEqual(self.wire[2]["discovery"]["remaining"], {"rounds": 2, "corrections": 2, "commands": 24})
        files = self.wire[3]["context"][0]["files"]
        self.assertEqual([item["path"] for item in files], [str(self.home / ".config/gizmo-suite/panels.json")])
        self.assertEqual(self.confirm.call_count, 1)                       # ONE consent for the whole finite flow
        self.reader.assert_not_called()
        self.assertEqual([p for p in self.reads], [str(self.home / ".config/gizmo-suite/panels.json")])
        self.assertNoDeniedData()

    def test_a_different_structure_is_handled_by_the_same_generic_steps(self):
        # No special names: search by a fragment, narrow to a deeper directory, pick the file from that listing.
        result = self.run_flow([
            plan("find", "f0", queries=["office"], kind="file"),
            read(sel("gizmo/display/profiles/office.conf")),
            ANSWER])
        self.assertEqual(result, ANSWER)
        self.assertEqual([m["path"] for m in self.steps(1)[0]["matches"]], ["gizmo/display/profiles/office.conf"])
        self.assertEqual(self.wire[2]["context"][0]["files"][0]["content"], "office=1\n")

    def test_both_providers_use_the_same_contract_and_data(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                self.output.truncate(0)
                result = self.run_flow([plan("list", "f0"), read(sel("gizmo/**")), ANSWER],
                                       provider=provider)
                self.assertEqual(result, ANSWER)
                self.assertEqual({body["execution"]["provider"] for body in self.wire}, {provider})
                self.assertEqual(self.steps(1)[0]["op"], "list")

class GroundingTests(Base):
    def test_guess_then_discover_then_read_recovers_with_a_correction(self):
        result = self.run_flow([read(sel("gizmo/settings.conf")), plan("list", sel("gizmo")),
                                read(sel("gizmo/settings.conf")), ANSWER])
        self.assertEqual(result, ANSWER)
        self.assertEqual(self.steps(1), [{"op": "refusal", "code": "discovery-required",
                                          "selectors": [sel("gizmo/settings.conf")], "limit": None}])
        self.assertEqual(self.wire[1]["discovery"]["remaining"], {"rounds": 4, "corrections": 1, "commands": 24})

    def test_a_path_the_user_typed_needs_no_discovery(self):
        result = self.run_flow([read(sel("gizmo/settings.conf")), ANSWER],
                               question="Pokaż gizmo/settings.conf proszę")
        self.assertEqual(result, ANSWER)
        self.assertEqual(len(self.wire), 2)

class LargeDirectoryTests(Base):
    def setUp(self):
        super().setUp()
        self.policy = permissions.load_policy()
        self.permit = SimpleNamespace(check=lambda: None)

    def page(self, after=None):
        selection = [sel("bulk")]
        step, _ = directory_context.list_step(self.policy, selection, after, self.policy.limits(),
                                              directory_context.Scan(4096), 60000, self.permit)
        return step["directories"][0], step

    def test_listing_is_paged_by_name_without_gaps_or_duplicates(self):
        first, step = self.page()
        self.assertTrue(first["truncated"] and step["truncated"])
        self.assertEqual(len(first["entries"]), 128)
        second, _ = self.page(first["next"])
        self.assertFalse(second["truncated"])
        self.assertIsNone(second["next"])
        names = [entry["path"] for entry in first["entries"] + second["entries"]]
        self.assertEqual(len(names), 211)
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(set(names)), 211)
        self.assertEqual(names[0], "bulk/small.conf")

    def test_paging_in_the_flow_costs_rounds_and_carries_next(self):
        result = self.run_flow([plan("list", sel("bulk")),
                                lambda doc: plan("list", sel("bulk"), after=doc["steps"][0]["directories"][0]["next"]),
                                ANSWER_PLAN])
        self.assertEqual(result, ANSWER)
        self.assertEqual(len(self.steps(1)[0]["directories"][0]["entries"]), 128)
        self.assertEqual(len(self.steps(2)[1]["directories"][0]["entries"]), 83)
        self.assertEqual(self.wire[2]["discovery"]["remaining"]["rounds"], 2)

    def test_owner_limits_bound_the_page_the_scan_and_the_results(self):
        self.write_policy("listing_entries = 16\nsearch_results = 5\nsearch_scan = 100\n")
        policy = permissions.load_policy()
        self.assertEqual((policy.limits().listing_entries, policy.limits().search_results, policy.limits().search_scan),
                         (16, 5, 100))
        step, _ = directory_context.list_step(policy, [sel("bulk")], None, policy.limits(),
                                              directory_context.Scan(policy.limits().search_scan), 60000, self.permit)
        self.assertEqual(len(step["directories"][0]["entries"]), 16)
        self.assertTrue(step["directories"][0]["truncated"])
        scan = directory_context.Scan(policy.limits().search_scan)
        found, _ = directory_context.find_step(policy, "f0", ["zz-data"], "file", policy.limits(), scan, 60000, self.permit)
        self.assertTrue(found["truncated"])
        self.assertLessEqual(len(found["matches"]), 5)

    def test_a_cut_scan_offers_no_paging_point_because_the_order_is_unknown(self):
        step, _ = directory_context.list_step(self.policy, [sel("bulk")], None, self.policy.limits(),
                                              directory_context.Scan(30), 60000, self.permit)
        out = step["directories"][0]
        self.assertTrue(out["truncated"] and step["truncated"])
        self.assertIsNone(out["next"])

    def test_an_exhausted_scan_budget_is_reported_as_incomplete_never_as_empty(self):
        policy = permissions.load_policy()
        found, _ = directory_context.find_step(policy, "f0", ["zz-data-209"], "file", policy.limits(),
                                               directory_context.Scan(30), 60000, self.permit)
        self.assertTrue(found["incomplete"] and found["truncated"])
        self.assertEqual(found["matches"], [])


class DenyAndAliasTests(Base):
    def setUp(self):
        super().setUp()
        self.policy = permissions.load_policy()
        self.permit = SimpleNamespace(check=lambda: None)

    def test_denied_names_are_not_listed_found_or_counted(self):
        step, _ = directory_context.list_step(self.policy, ["f0", sel("gizmo")], None, self.policy.limits(),
                                              directory_context.Scan(4096), 60000, self.permit)
        text = json.dumps(step)
        for word in ("vault", "token", "ai-desktop/", "permissions"):
            self.assertNotIn(word, text)
        for query in ("token", "secret", "vault", "permissions", "ai-desk"):
            found, _ = directory_context.find_step(self.policy, "f0", [query], "any", self.policy.limits(),
                                                   directory_context.Scan(4096), 60000, self.permit)
            self.assertEqual(found["matches"], [], query)
            self.assertFalse(found["incomplete"], query)

    def test_denied_directory_or_file_cannot_be_listed_or_read_even_when_the_user_typed_it(self):
        out = self.refused([read(sel("vault/secret.txt")), ANSWER], question="Pokaż vault/secret.txt")
        self.assertIn("nie jest dozwolony", out)
        self.assertEqual(len(self.wire), 1)                              # a security refusal stops: no correction round
        out = self.refused([plan("list", sel("vault")), ANSWER])
        self.assertEqual(len(self.wire), 1)
        self.assertEqual(self.reads, [])
        self.assertNoDeniedData()

    def test_alias_entries_show_only_valid_literal_sibling_directory_links(self):
        step, _ = directory_context.list_step(self.policy, ["f0"], None, self.policy.limits(),
                                              directory_context.Scan(4096), 60000, self.permit)
        entries = {entry["path"]: entry for entry in step["directories"][0]["entries"]}
        self.assertEqual(entries["GizmoSuite"], {"path": "GizmoSuite", "type": "alias", "size": None, "target": "gizmo-suite"})
        self.assertEqual(entries["file-link"]["target"], "bulk")
        for name in ("loop-a", "loop-b", "ghost", "up"):
            self.assertNotIn(name, entries)                              # loops, broken and escaping links are invisible

    def test_a_file_link_under_an_alias_is_not_followed(self):
        step, _ = directory_context.list_step(self.policy, [sel("GizmoSuite")], None, self.policy.limits(),
                                              directory_context.Scan(4096), 60000, self.permit)
        self.assertNotIn("settings-link", json.dumps(step))
        self.write_policy("plan_corrections = 0\n")
        out = self.refused([read(sel("GizmoSuite/settings-link")), ANSWER],
                           question="Pokaż GizmoSuite/settings-link")
        self.assertIn("dowiązanie symboliczne", out)

    def test_alias_loops_broken_and_escaping_links_are_refused_with_a_recoverable_code(self):
        for name in ("loop-a/x", "ghost/x", "up/x"):
            with self.subTest(name=name):
                self.output.truncate(0)
                self.write_policy("plan_corrections = 0\n")
                out = self.refused([read(sel(name)), ANSWER], question="Pokaż " + name)
                self.assertIn("Odmowa odczytu plików", out)
                self.assertEqual(self.reads, [])

    def test_the_alias_is_rechecked_and_a_swap_stops_the_question(self):
        def swap(number, kwargs):
            if number == 3:                                              # before the third request is authorized
                (self.home / ".config/GizmoSuite").unlink()
                self.link("GizmoSuite", "gizmo")
        with self.assertRaises(AnalysisDenied):
            self.run_flow([plan("list", sel("GizmoSuite")), plan("list", sel("GizmoSuite")), ANSWER], before=swap)
        self.assertEqual(len(self.wire), 2)


class NarrowingAndCorrectionTests(Base):
    def test_overbroad_read_is_refused_with_the_owner_limit_and_the_model_narrows_it(self):
        result = self.run_flow([plan("list", "f0"), read(sel("bulk/**")),
                                plan("list", sel("bulk")), read(sel("bulk/small.conf")), ANSWER])
        self.assertEqual(result, ANSWER)
        refusal = self.steps(2)[1]
        self.assertEqual(refusal, {"op": "refusal", "code": "files-count", "selectors": [sel("bulk/**")], "limit": 64})
        self.assertNotIn("Traceback", self.output.getvalue())
        self.assertEqual(self.wire[-1]["context"][0]["files"][0]["content"], "small\n")
        self.assertEqual(len(self.wire[-1]["context"][0]["files"]), 1)

    def test_correction_budget_is_owner_managed_and_exhaustion_is_explained_without_a_partial_send(self):
        self.write_policy("plan_corrections = 0\n")
        out = self.refused([plan("list", "f0"), read(sel("bulk/**")), ANSWER])
        self.assertIn("plan_corrections=0", out)
        self.assertIn("read_files=64", out)
        self.assertEqual([body["mode"] for body in self.wire], ["assistant", "assistant"])
        self.assertEqual(self.reads, [])

    def test_a_raised_read_limit_helps_but_never_beyond_the_ceiling(self):
        self.write_policy("read_files = 128\nplan_corrections = 0\n")
        out = self.refused([plan("list", "f0"), read(sel("bulk/**")), ANSWER])
        self.assertIn("read_files=128", out)             # 211 files: still refused, ceiling is 128
        self.write_policy("")
        self.assertEqual(self.run_flow([plan("list", "f0"), read(sel("gizmo/**")), ANSWER]), ANSWER)
        self.assertEqual(len(self.wire[-1]["context"][0]["files"]), 3)

    def test_the_plan_may_not_switch_to_web_or_edit_after_discovery(self):
        with self.assertRaises(plans.PlanError):
            self.run_flow([plan("list", "f0"), json.dumps({"action": "web"})])


class BudgetTests(Base):
    def test_round_budget_stops_the_flow_with_the_key_name(self):
        self.write_policy("discovery_rounds = 1\n")
        out = self.refused([plan("list", "f0"), plan("list", sel("gizmo")), ANSWER])
        self.assertIn("discovery_rounds=1", out)
        self.assertEqual(len(self.wire), 2)

    def test_the_budget_is_shared_per_question_across_list_and_find(self):
        self.write_policy("discovery_rounds = 2\n")
        out = self.refused([plan("list", "f0"), plan("find", "f0", queries=["gizmo"], kind="any"),
                            plan("list", sel("gizmo")), ANSWER])
        self.assertIn("discovery_rounds=2", out)

    def test_scan_budget_ends_discovery_softly_and_tells_the_model(self):
        self.write_policy("search_scan = 100\n")
        result = self.run_flow([plan("find", "f0", queries=["zz-data"], kind="file"),
                                plan("list", "f0"), ANSWER_PLAN])
        self.assertEqual(result, ANSWER)
        last = self.steps(2)[-1]
        self.assertEqual((last["op"], last["code"], last["limit"]), ("refusal", "discovery-budget", 100))
        self.assertTrue(self.steps(1)[0]["incomplete"])
        self.assertEqual(self.wire[2]["discovery"]["remaining"]["rounds"], 0)

    def test_nothing_is_sent_when_the_policy_is_missing(self):
        self.config.unlink()
        with self.assertRaises(Exception):
            self.run_flow([plan("list", "f0"), ANSWER])


class PlanContractTests(unittest.TestCase):
    access = {"files": [{"id": "f0", "pattern": "~/.config/**"}, {"id": "f1", "pattern": "~/project/"},
                        {"id": "f2", "pattern": "~/one.txt"}], "systemd": [], "weather": False}

    def parse(self, value):
        return plans.parse_plan(json.dumps(value), self.access)

    def test_list_find_and_paging_shapes(self):
        self.assertEqual(self.parse({"action": "list", "files": ["f0"]})["action"], "list")
        self.assertEqual(self.parse({"action": "list", "files": [sel("a")], "after": "a/b"})["after"], "a/b")
        found = self.parse({"action": "find", "files": ["f0"], "queries": ["abc", "de"], "kind": "any"})
        self.assertEqual(found["queries"], ["abc", "de"])

    def test_invalid_discovery_plans_are_refused(self):
        bad = [{"action": "list", "files": ["f2"]}, {"action": "list", "files": [sel("a/*")]},
               {"action": "list", "files": ["f0", "f1"], "after": "a"}, {"action": "list", "files": ["f0"], "after": "../x"},
               {"action": "list", "files": ["f0"], "command": "ls"},
               {"action": "find", "files": ["f0", "f1"], "queries": ["abc"], "kind": "any"},
               {"action": "find", "files": ["f0"], "queries": ["a"], "kind": "any"},
               {"action": "find", "files": ["f0"], "queries": ["a/b"], "kind": "any"},
               {"action": "find", "files": ["f0"], "queries": ["abc"] * 5, "kind": "any"},
               {"action": "find", "files": ["f0"], "queries": ["abc"], "kind": "symlink"},
               {"action": "find", "files": ["f0"], "queries": ["abc"]},
               {"action": "find", "files": ["f2"], "queries": ["abc"], "kind": "any"}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(plans.PlanError):
                self.parse(value)


class LimitsConfigurationTests(Base):
    def test_defaults_equal_the_former_constants_and_policy_without_keys_is_unchanged(self):
        limits = permissions.load_policy().limits()
        self.assertEqual(limits, permissions.Limits())
        self.assertEqual((limits.read_files, limits.read_file_size, limits.read_total_size), (64, 65536, 131072))

    def test_valid_values_are_parsed_including_size_strings(self):
        self.write_policy('read_files = 3\nread_file_size = "2KiB"\nread_total_size = 8192\nlisting_entries = 32\n'
                          'discovery_bytes = "8KiB"\nsearch_results = 7\nsearch_scan = 500\ndiscovery_rounds = 2\n'
                          'plan_corrections = 1\n')
        limits = permissions.load_policy().limits()
        self.assertEqual((limits.read_files, limits.read_file_size, limits.read_total_size, limits.listing_entries,
                          limits.discovery_bytes, limits.search_results, limits.search_scan, limits.discovery_rounds,
                          limits.plan_corrections), (3, 2048, 8192, 32, 8192, 7, 500, 2, 1))

    def test_invalid_values_name_only_the_key_and_never_the_value(self):
        for line, key in (("read_files = 0", "read_files"), ("read_files = 129", "read_files"),
                          ('read_files = "3"', "read_files"), ("read_files = true", "read_files"),
                          ("read_file_size = 1", "read_file_size"), ('read_total_size = "9MiB"', "read_total_size"),
                          ("discovery_rounds = 9", "discovery_rounds"), ("plan_corrections = -1", "plan_corrections"),
                          ("search_scan = 99", "search_scan"), ("listing_entries = 513", "listing_entries"),
                          ("read_file_size = 65536\nread_total_size = 2048", "read_file_size")):
            with self.subTest(line=line):
                self.write_policy(line + "\n")
                with self.assertRaises(ValueError) as caught:
                    permissions.load_policy()
                self.assertEqual(caught.exception.args, ("policy-invalid",))
                self.assertEqual(caught.exception.setting, key)
        self.write_policy("unknown_limit = 3\n")
        with self.assertRaises(ValueError):
            permissions.load_policy()

    def test_the_owner_read_limits_are_enforced_at_read_time(self):
        self.write_policy("read_files = 2\nplan_corrections = 0\n")
        out = self.refused([plan("list", sel("gizmo")), read(sel("gizmo/**")), ANSWER])
        self.assertIn("read_files=2", out)
        self.write_policy('read_file_size = "1KiB"\nread_total_size = "1KiB"\nplan_corrections = 0\n')
        self.file("gizmo/display/outputs.conf", "x" * 2048)
        self.output.truncate(0)
        out = self.refused([read(sel("gizmo/display/outputs.conf")), ANSWER],
                           question="Pokaż gizmo/display/outputs.conf")
        self.assertIn("read_file_size=1024", out)


if __name__ == "__main__":
    unittest.main()


class RootOwnedSystemFilesTests(Base):
    """System configuration (root-owned, not writable by others) is readable under a version 2 policy that lists it."""

    def write_policy(self, extra="", deny=(), version=2):
        self.config.write_text('version = %d\n\n[files]\nread = ["/etc/**"]\ndeny = ["~/.config/ai-desktop/"]\n\n'
                               '[network]\nclaude = true\n' % version, encoding="utf-8")
        self.config.chmod(0o600)

    def test_trusted_owner_rules(self):
        info = lambda uid, mode: SimpleNamespace(st_uid=uid, st_mode=mode)
        mine = os.getuid()
        self.assertTrue(permissions.trusted_owner(info(mine, 0o100644), mine, system=False))
        self.assertTrue(permissions.trusted_owner(info(0, 0o100644), mine, system=True))
        self.assertFalse(permissions.trusted_owner(info(0, 0o100644), mine, system=False))       # version 1: unchanged
        self.assertFalse(permissions.trusted_owner(info(0, 0o100666), mine, system=True))        # world-writable: never
        self.assertFalse(permissions.trusted_owner(info(0, 0o100664), mine, system=True))        # group-writable: never
        self.assertFalse(permissions.trusted_owner(info(12345, 0o100644), mine, system=True))    # somebody else: never

    def test_a_root_owned_system_file_is_listed_found_and_read_under_version_2(self):
        policy = permissions.load_policy()
        permit = SimpleNamespace(check=lambda: None)
        found, _ = directory_context.find_step(policy, "f0", ["passwd"], "file", policy.limits(),
                                               directory_context.Scan(20000), 60000, permit)
        self.assertIn("passwd", [item["path"] for item in found["matches"]])
        selection = policy.expand_selected(((0, "passwd"),))
        content = permissions.read_selected(policy, selection)["files"][0]["content"]
        self.assertIn("root:", content)

    def test_version_1_keeps_requiring_the_user_as_owner(self):
        self.write_policy(version=1)
        with self.assertRaises(ValueError):
            policy = permissions.load_policy()
            permissions.read_selected(policy, policy.expand_selected(((0, "passwd"),)))

    def test_the_policy_file_itself_still_must_belong_to_the_user(self):
        # the relaxation is for files the policy lets the assistant read, never for the policy loader
        self.assertEqual(permissions.load_policy()._version, 2)
        with patch.object(permissions.os, "geteuid", return_value=os.getuid()):
            self.config.chmod(0o644)
            with self.assertRaises(ValueError):
                permissions.load_policy()
class ReviewFindingsTests(Base):
    """Regressions for the independent review of the discovery work."""

    def test_raised_read_limit_survives_alias_rechecks(self):
        self.write_policy("read_files = 128\n")
        policy = permissions.load_policy()
        self.file("gizmo-suite/many/x.conf", "x")
        for index in range(70):
            self.file("gizmo-suite/many/f%02d.conf" % index, "x")
        selection = policy.expand_selected(((0, "GizmoSuite/many/**"),))
        self.assertGreater(len(selection.paths), 64)
        self.assertEqual(permissions.recheck_selection(policy, selection), selection.paths)

    def test_queries_differing_only_by_case_are_merged_not_fatal(self):
        access = {"files": [{"id": "f0", "pattern": "~/.config/**"}], "systemd": [], "weather": False}
        parsed = plans.parse_plan(json.dumps({"action": "find", "files": ["f0"], "queries": ["Config", "config", "panels"],
                                              "kind": "any"}), access)
        self.assertEqual(parsed["queries"], ["Config", "panels"])
        result = self.run_flow([json.dumps({"action": "find", "files": ["f0"], "queries": ["Gizmo", "gizmo"],
                                            "kind": "directory"}), ANSWER_PLAN])
        self.assertEqual(result, ANSWER)

    def test_user_typed_path_must_match_on_boundaries_not_as_a_substring(self):
        ledger = ask_discovery.DiscoveryLedger(permissions.load_policy(), "Opisz dokumenty i descriptor")
        for guess in ("doc", "src", "scri", "descr"):
            with self.subTest(guess=guess), self.assertRaises(ask_discovery.Recoverable):
                ledger.ground([sel(guess)])
        typed = ask_discovery.DiscoveryLedger(permissions.load_policy(), "Pokaż gizmo/settings.conf, proszę. Albo ~/.config/bulk")
        typed.ground([sel("gizmo/settings.conf"), sel("bulk")])

    def test_entry_and_byte_totals_cannot_break_the_wire_contract_at_the_ceilings(self):
        self.write_policy("listing_entries = 512\ndiscovery_bytes = \"64KiB\"\ndiscovery_rounds = 8\nplan_corrections = 4\n")
        for index in range(600):
            self.file("wide/n%03d.conf" % index, "x")
        responses = [plan("list", "f0")] + [plan("list", sel("wide"))] * 7 + [ANSWER_PLAN]
        try:
            self.run_flow(responses)
        except flow.file_context.FileReadDenied:
            pass                                    # an explained stop is fine; an unexplained ValueError is not
        for body in self.wire:
            self.assertLessEqual(sum(len(d["entries"]) for s in body.get("discovery", {"steps": []})["steps"]
                                     if s["op"] == "list" for d in s["directories"]), 1024)

    def test_denied_names_do_not_consume_the_scan_or_make_a_listing_look_truncated(self):
        for index in range(200):
            self.file("vault/h%03d.txt" % index, "PRIVATE-DENIED")
        policy = permissions.load_policy()
        scan = directory_context.Scan(40)
        step, _ = directory_context.list_step(policy, ["f0"], None, policy.limits(), scan, 60000,
                                              SimpleNamespace(check=lambda: None))
        self.assertGreater(scan.left, 0)
        self.assertNotIn("vault", json.dumps(step))

    def test_a_subdirectory_swapped_for_a_link_during_find_stops_the_search(self):
        policy = permissions.load_policy()
        real = directory_context._children
        def swapped(pol, physical, logical, scope, scan, expect=None, match=None):
            if physical.endswith("/gizmo/display"):
                raise ValueError("files-symlink")
            return real(pol, physical, logical, scope, scan, expect, match)
        with patch.object(directory_context, "_children", side_effect=swapped):
            with self.assertRaises(ValueError):
                directory_context.find_step(policy, "f0", ["office"], "file", policy.limits(),
                                            directory_context.Scan(4096), 60000, SimpleNamespace(check=lambda: None))

    def test_the_real_catalog_client_is_never_started_under_unittest(self):
        from ai_desktop import model_catalog
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_DESKTOP_MODEL_CATALOG", None)
            with self.assertRaises(model_catalog.CatalogUnavailable):
                model_catalog.fetch()


class ExternalReviewTests(Base):
    """Regressions for the second (external) review of 0.1.0-46."""

    def test_denied_alias_target_is_neither_named_nor_examined(self):
        self.file("real/hidden/secret.conf", "PRIVATE-DENIED")
        self.file("real/plain.conf", "plain")
        os.symlink("hidden", self.home / ".config/real/mirror")      # real/mirror -> hidden (sibling of mirror)
        os.symlink("real", self.home / ".config/public")             # public -> real
        self.write_policy(deny=["~/.config/public/hidden/"])
        policy = permissions.load_policy()
        calls = []
        real_stat = os.stat
        def spy(path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and str(path) == "hidden":
                calls.append(str(path))
            return real_stat(path, *args, **kwargs)
        with patch.object(os, "stat", side_effect=spy):
            step, _ = directory_context.list_step(policy, [sel("public")], None, policy.limits(),
                                                  directory_context.Scan(4096), 60000, SimpleNamespace(check=lambda: None))
        self.assertNotIn("hidden", json.dumps(step))
        self.assertNotIn("mirror", json.dumps(step))
        self.assertEqual(calls, [])

    def test_a_typed_path_grounds_only_the_same_path_not_a_same_named_file_elsewhere(self):
        self.file("nested/logs.conf", "nested")
        self.file("logs.conf", "root")
        self.write_policy("plan_corrections = 0\n")
        out = self.refused([read(sel("logs.conf")), ANSWER], question="Pokaż nested/logs.conf")
        self.assertEqual(self.reads, [])
        self.assertTrue(self.wire == [] or all("context" not in body for body in self.wire))

    def test_grounding_is_case_sensitive(self):
        self.file("Public.conf", "A")
        self.file("public.conf", "B")
        self.write_policy("plan_corrections = 0\n")
        self.refused([read(sel("public.conf")), ANSWER], question="Pokaż Public.conf")
        self.assertEqual(self.reads, [])

    def test_a_whole_directory_scope_needs_a_listing_first(self):
        self.write_policy("plan_corrections = 0\n")
        self.refused([read("f0"), ANSWER])
        self.assertEqual(self.reads, [])
        self.assertEqual(self.run_flow([plan("list", "f0"), read(sel("gizmo/**")), ANSWER]), ANSWER)

    def test_a_subdirectory_replaced_after_it_was_listed_stops_the_question(self):
        real = directory_context._children
        state = {"done": False}
        def replace_then_open(pol, physical, logical, scope, scan, expect=None, match=None):
            if physical.endswith("/gizmo/display") and not state["done"]:
                state["done"] = True
                base = self.home / ".config/gizmo"
                (base / "display").rename(base / "display.old")
                (base / "display").mkdir()
                (base / "display/outputs.conf").write_text("substituted", encoding="utf-8")
            return real(pol, physical, logical, scope, scan, expect, match)
        self.write_policy("plan_corrections = 0\n")
        with patch.object(directory_context, "_children", side_effect=replace_then_open):
            out = self.refused([plan("find", "f0", queries=["outputs"], kind="file"), ANSWER_PLAN])
        self.assertTrue(state["done"])
        self.assertEqual(len(self.wire), 1)                          # no second request carried the substituted result

    def test_the_owner_byte_budget_is_respected_by_refusal_documents(self):
        self.write_policy('discovery_bytes = 2048\n')
        guesses = [sel("aa%02d/%s" % (i, "x" * 120)) for i in range(4)]
        result = self.run_flow([read(*guesses), ANSWER_PLAN])
        self.assertEqual(result, ANSWER)
        size = len(json.dumps(self.wire[1]["discovery"], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        self.assertLessEqual(size, 2048)
        small = self.run_flow([plan("list", "f0"), ANSWER_PLAN])        # a normal list is not starved by the reserve
        self.assertEqual(small, ANSWER)


class PolicyV1Tests(Base):
    """The CI fixture uses a version 1 policy with a non-recursive `dir/*` scope: the same discovery-first flow must work."""

    def write_policy(self, extra="", deny=()):
        self.config.write_text('version = 1\n\n[files]\nread = ["%s/.config/gizmo/*"]\ndeny = ["%s/.config/gizmo/token.txt"]\n\n'
                               '[network]\nclaude = true\nweather = false\n' % (self.home, self.home), encoding="utf-8")
        self.config.chmod(0o600)

    def test_list_then_read_whole_children_scope_then_answer(self):
        result = self.run_flow([plan("list", "f0"), read("f0"), ANSWER])
        self.assertEqual(result, ANSWER)
        listed = [entry["path"] for entry in self.steps(1)[0]["directories"][0]["entries"]]
        self.assertEqual(listed, ["settings.conf"])                       # token.txt denied, display/ is a directory
        self.assertNotIn("token.txt", json.dumps(self.wire))
        self.assertEqual([body["mode"] for body in self.wire], ["assistant", "assistant", "question"])
        self.assertEqual(len(self.wire[2]["context"][0]["files"]), 1)
        self.assertEqual(self.confirm.call_count, 1)

    def test_reading_the_whole_scope_without_listing_is_refused_and_can_be_corrected(self):
        result = self.run_flow([read("f0"), plan("list", "f0"), read("f0"), ANSWER])
        self.assertEqual(result, ANSWER)
        self.assertEqual(self.steps(1)[0]["code"], "discovery-required")


class SearchCostTests(Base):
    def test_non_matching_plain_files_cost_no_metadata_call(self):
        policy = permissions.load_policy()
        stats = []
        real_stat = os.stat
        def spy(path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None:
                stats.append(str(path))
            return real_stat(path, *args, **kwargs)
        with patch.object(os, "stat", side_effect=spy):
            found, _ = directory_context.find_step(policy, "f0", ["small"], "file", policy.limits(),
                                                   directory_context.Scan(4096), 60000, SimpleNamespace(check=lambda: None))
        self.assertEqual([m["path"] for m in found["matches"]], ["bulk/small.conf"])
        self.assertFalse([name for name in stats if name.startswith("zz-data")])

    def test_one_broad_find_cannot_use_more_than_a_quarter_of_the_scan_budget(self):
        for part in range(7):                                        # directories are capped at 1024 entries each
            for index in range(900):
                self.file("wide/d%d/n%03d.conf" % (part, index), "x")
        policy = permissions.load_policy()
        ledger = ask_discovery.DiscoveryLedger(policy, QUESTION)
        step = ledger.run({"action": "find", "files": ["f0"], "queries": ["no-such-name"], "kind": "any"},
                          SimpleNamespace(check=lambda: None))
        self.assertTrue(step["incomplete"])
        self.assertEqual(ledger.scan.left, policy.limits().search_scan - policy.limits().search_scan // 4)
        self.assertGreater(ledger.scan.left, 0)                    # a narrower follow-up is still possible

    def test_default_scan_budget_and_ceiling(self):
        self.assertEqual(permissions.Limits().search_scan, 20000)
        self.assertEqual(permissions.LIMIT_SPECS["search_scan"][2], 100000)


class InstructionPathHintTests(Base):
    """Paths the owner writes into the agent's instructions or Skills count as named by the user, never as permission."""

    def test_a_path_named_in_the_instructions_needs_no_discovery(self):
        hint = "Znane lokalizacje: konfiguracja gizmo to ~/.config/gizmo/settings.conf (odczytuj tylko ją)."
        result = self.run_flow([read(sel("gizmo/settings.conf")), ANSWER], instructions=hint)
        self.assertEqual(result, ANSWER)
        self.assertEqual(len(self.wire), 2)                                       # no list, no correction round
        self.assertEqual(self.wire[1]["context"][0]["files"][0]["content"], "mode=basic\n")
        self.assertEqual(self.wire[0]["execution"]["instructions"], hint)

    def test_the_same_path_without_the_hint_is_still_refused_until_discovered(self):
        self.write_policy("plan_corrections = 0\n")
        self.refused([read(sel("gizmo/settings.conf")), ANSWER])
        self.assertEqual(self.reads, [])

    def test_a_hint_never_widens_the_policy_and_never_beats_deny(self):
        self.write_policy("plan_corrections = 0\n")
        out = self.refused([read(sel("vault/secret.txt")), ANSWER],
                           instructions="Sekret: ~/.config/vault/secret.txt oraz ~/.config/gizmo/token.txt")
        self.assertIn("nie jest dozwolony", out)
        self.assertEqual(self.reads, [])

    def test_the_hint_must_match_on_path_boundaries(self):
        ledger = ask_discovery.DiscoveryLedger(permissions.load_policy(), QUESTION,
                                               "pliki: ~/.config/gizmo/settings.conf.bak")
        with self.assertRaises(ask_discovery.Recoverable):
            ledger.ground([sel("gizmo/settings.conf")])                           # a longer name is not this path
