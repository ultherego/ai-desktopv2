"""A change on the live system: strong consent for the exact change, its test and its undo; undo only after a second yes."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_desktop import assistant_contract as plans, command_ops, conversation_state as cs, discovery_contract as dc
from ai_desktop import permissions, terminal_consent
from ai_desktop.analysis_contract import AnalysisDenied
import test_ask_operations as ops
from test_ask_discovery import ANSWER, ANSWER_PLAN

ALLOW = '["uname -s", "systemctl status <unit> --no-pager"]'
CHANGE = '["systemctl restart <unit>", "systemctl enable <unit>"]'


def change(argv, test, undo):
    return json.dumps({"action": "run", "argv": argv, "test": test, "undo": undo})


RESTART = ["systemctl", "restart", "foo.service"]
STATUS = ["systemctl", "status", "foo.service", "--no-pager"]
REVERT = ["systemctl", "enable", "foo.service"]


class ChangeBase(ops.OperationsBase):
    commands = ALLOW
    real_sandbox = True                       # no default executor patch: this class installs its own fake

    def write_policy(self, extra="", deny=()):
        denied = ["~/.config/ai-desktop/", "~/.config/vault/", "~/.config/gizmo/token.txt", *deny]
        self.config.write_text('version = 2\n[network]\nclaude = true\ncodex = true\n[files]\nread = ["~/.config/**"]\n'
                               'deny = ' + json.dumps(denied) + '\n' + (extra or self.extra) +
                               '\n[commands]\nallow = ' + self.commands + '\nchange = ' + self.change + '\n' + self.share_line, encoding="utf-8")
        self.config.chmod(0o600)

    change = CHANGE
    share_line = ""
    access = "changes"                         # these tests exercise a profile that is allowed to change things

    def run_flow(self, responses, **kwargs):
        return super().run_flow(responses, **{"access": self.access, **kwargs})

    def setUp(self):
        super().setUp()
        self.change_answer, self.undo_answer = True, True
        self.results = {}
        self.during = {}

        def confirm(action, **kwargs):
            self.asked.append(action)
            return {"run-change": self.change_answer, "run-undo": self.undo_answer}.get(action, True)
        self.confirm.side_effect = confirm
        self.calls = []

        def fake(path, argv, *, output_limit, timeout, command=None, host=False, role="read"):
            self.calls.append((list(argv), host, role))
            if tuple(argv) in self.during:
                self.during[tuple(argv)]()
            code, text = self.results.get(tuple(argv), (0, "ok\n"))
            return {"op": "run", "argv": list(argv), "status": "ok" if code == 0 else "failed", "exit": code,
                    "output": text, "truncated": False, "role": role}
        self.enterContext(patch.object(command_ops, "execute", side_effect=fake))
        self.enterContext(patch.object(command_ops, "resolve", side_effect=lambda name: "/usr/bin/" + name))

    def facts(self, responses):
        with cs.recording() as recorder:
            result = self.run_flow(responses)
        return result, recorder.actions

    def steps(self, index=-1):
        return [step for step in self.wire[index]["discovery"]["steps"] if step["op"] == "run"]


class ChangeFlowTests(ChangeBase):
    def test_a_successful_change_runs_change_then_test_on_the_host_after_one_strong_yes(self):
        result, facts = self.facts([change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual(result, ANSWER)
        self.assertEqual(self.asked, ["ask-question", "run-change"])
        self.assertEqual(self.calls, [(RESTART, True, "change"), (STATUS, True, "test")])
        self.assertEqual([(s["role"], s["status"], s["argv"]) for s in self.steps()],
                         [("change", "ok", RESTART), ("test", "ok", STATUS)])
        self.assertEqual(self.wire[1]["discovery"]["remaining"]["commands"], 22)        # 24 rounds - change - test
        self.assertEqual(facts, ["Change systemctl restart foo.service: ok, code 0",
                                 "Test after change systemctl status foo.service --no-pager: ok, code 0"])
        shown = self.output.getvalue()
        for text in ("SYSTEM CHANGE", "change ", "systemctl restart foo.service", "test after change ", "undo ",
                     "systemctl enable foo.service", "Test after change passed"):
            self.assertIn(text, shown)
        self.assertEqual(self.wire[0]["access"]["change_commands"], ["systemctl restart <unit>", "systemctl enable <unit>"])

    def revoke(self):
        self.config.write_text(self.config.read_text(encoding="utf-8") + "\n# changed while the command ran\n", encoding="utf-8")

    def interrupted(self, responses):
        with cs.recording() as recorder, self.assertRaises(AnalysisDenied) as denied:
            self.run_flow(responses)
        self.assertEqual(denied.exception.reason, "consent")
        return recorder.actions

    def test_access_revoked_while_the_change_runs_keeps_its_result_and_names_the_undo(self):
        self.during[tuple(RESTART)] = self.revoke
        facts = self.interrupted([change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual(self.calls, [(RESTART, True, "change")])                   # nothing further runs on the host
        self.assertEqual(len(self.wire), 1)                                         # and nothing further is sent
        self.assertEqual(facts, ["Change systemctl restart foo.service: ok, code 0",
                                 "Change systemctl restart foo.service: stopped after it ran, further steps not run"])
        shown = self.output.getvalue()
        for text in ("The change WAS MADE", "Test: systemctl status foo.service", "You can run the undo yourself: systemctl enable foo.service"):
            self.assertIn(text, shown)
        self.assertNotIn("run-undo", self.asked)

    def test_access_revoked_during_the_test_keeps_both_results(self):
        self.during[tuple(STATUS)] = self.revoke
        facts = self.interrupted([change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual([role for _, _, role in self.calls], ["change", "test"])
        self.assertEqual(len(self.wire), 1)
        self.assertEqual(facts[:2], ["Change systemctl restart foo.service: ok, code 0",
                                     "Test after change systemctl status foo.service --no-pager: ok, code 0"])
        self.assertIn("The change WAS MADE", self.output.getvalue())

    def test_consent_lost_before_the_change_starts_reports_no_change(self):
        with patch("ai_desktop.question_consent.QuestionConsent.waiting", side_effect=self.lapse_on_enter):
            with cs.recording(), self.assertRaises(AnalysisDenied):
                self.run_flow([change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual(self.calls, [])
        self.assertNotIn("WAS MADE", self.output.getvalue())

    @staticmethod
    def lapse_on_enter(*args, **kwargs):
        raise AnalysisDenied("consent")

    def test_a_failed_test_offers_the_undo_and_runs_it_only_after_a_second_yes(self):
        self.results[tuple(STATUS)] = (3, "inactive\n")
        result, facts = self.facts([change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual(self.asked, ["ask-question", "run-change", "run-undo"])
        self.assertEqual([role for _, _, role in self.calls], ["change", "test", "undo"])
        self.assertEqual(self.calls[-1][0], REVERT)
        self.assertEqual([s["role"] for s in self.steps()], ["change", "test", "undo"])
        self.assertEqual(self.steps()[1]["status"], "failed")
        self.assertTrue(any(f.startswith("Undo systemctl enable foo.service: ok") for f in facts))

    def test_a_declined_undo_is_not_run_and_is_recorded(self):
        self.results[tuple(STATUS)] = (3, "inactive\n")
        self.undo_answer = False
        result, facts = self.facts([change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual([role for _, _, role in self.calls], ["change", "test"])
        self.assertIn("Undo systemctl enable foo.service: declined by the user, not run", facts)
        self.assertIn("The system may still be changed", self.output.getvalue())

    def test_a_failed_change_skips_the_test_and_offers_the_undo(self):
        self.results[tuple(RESTART)] = (1, "Failed to restart\n")
        self.facts([change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual([role for _, _, role in self.calls], ["change", "undo"])

    def test_declining_the_change_runs_nothing(self):
        self.change_answer = False
        result, facts = self.facts([change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.steps()[0]["status"], "declined")
        self.assertEqual(self.steps()[0]["role"], "change")
        self.assertEqual(facts, ["Change systemctl restart foo.service: declined by the user, not run"])

    def test_without_a_valid_test_or_undo_nothing_is_changed_and_nothing_is_asked(self):
        for test, undo in (([ "uname", "-r"], REVERT), (STATUS, ["rm", "-rf", "/"]), (RESTART, REVERT), (STATUS, STATUS[:2])):
            with self.subTest(test=test, undo=undo):
                self.asked.clear()
                self.calls.clear()
                self.wire = []
                self.facts([change(RESTART, test, undo), ANSWER_PLAN])
                self.assertEqual(self.calls, [])
                self.assertNotIn("run-change", self.asked)
                self.assertEqual(self.steps()[0]["status"], "not-allowed")

    def test_a_change_outside_the_owners_change_list_is_not_run(self):
        self.facts([change(["systemctl", "stop", "foo.service"], STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.steps()[0]["status"], "not-allowed")

    def test_there_must_be_room_for_change_test_and_undo_before_anything_is_changed(self):
        self.write_policy("command_rounds = 2\n")
        out = self.refused([change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertIn("command_rounds=2", out)
        self.assertEqual(self.calls, [])

    def test_if_the_change_cannot_even_start_there_is_no_test_and_no_undo_offered(self):
        with patch.object(command_ops, "resolve", return_value=None):
            result, facts = self.facts([change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual(self.calls, [])
        self.assertNotIn("run-undo", self.asked)
        self.assertEqual([s["role"] for s in self.steps()], ["change"])

    def test_there_must_be_output_room_for_all_three_steps_before_anything_is_changed(self):
        self.write_policy("discovery_bytes = 2048\n")
        self.refused([change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual(self.calls, [])

    def test_a_read_command_never_runs_on_the_host_and_never_asks_the_strong_question(self):
        self.facts([ops.run("uname", "-s"), ANSWER_PLAN])
        self.assertEqual(self.calls, [(["uname", "-s"], False, "read")])
        self.assertEqual(self.asked, ["ask-question"])

    def test_test_and_undo_on_a_read_command_are_refused(self):
        self.facts([change(["uname", "-s"], STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual(self.calls, [])


class StrongConsentTests(unittest.TestCase):
    def ask(self, action, answers):
        replies, prompts = iter(answers), []
        with patch.object(terminal_consent.sys.stdin, "isatty", return_value=True), \
                patch.object(terminal_consent.sys.stdout, "isatty", return_value=True):
            result = terminal_consent.confirm(action, reader=lambda prompt: prompts.append(prompt) or next(replies))
        return result, prompts

    def test_only_the_whole_word_approves_a_change_or_an_undo(self):
        for action in ("run-change", "run-undo"):
            self.assertEqual(self.ask(action, ["y", "Y", "yes"])[0], True)
            self.assertEqual(len(self.ask(action, ["y", "Y", "yes"])[1]), 3)       # 'y' did not approve
            self.assertEqual(len(self.ask(action, ["tak", "t", "yes"])[1]), 3)     # nor do the old Polish words
            self.assertEqual(self.ask(action, ["", "n"])[0], False)
        self.assertEqual(self.ask("ask-question", ["y"])[0], True)                   # the transfer: 'y' still works
        self.assertEqual(self.ask("ask-question", ["tak", "t", "nie", "n"]), (False, [terminal_consent.PROMPTS["ask-question"]] * 4))

    def test_a_change_prompt_does_not_name_a_provider_so_it_cannot_be_mistaken_for_a_transfer(self):
        self.assertNotIn("Claude", terminal_consent.PROMPTS["run-change"])
        self.assertIn("CHANGE", terminal_consent.PROMPTS["run-change"])


class PolicyAndWireTests(ChangeBase):
    def test_change_templates_are_validated_and_must_not_overlap_the_read_list(self):
        for bad in ('["bash -c x"]', '["systemctl <unit>"]', '["sudo systemctl restart x"]', '"x"'):
            self.change = bad
            self.write_policy()
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                permissions.load_policy()
        self.change = '["uname -s"]'
        self.write_policy()
        with self.assertRaises(ValueError):
            permissions.load_policy()

    def test_change_commands_are_advertised_only_with_a_read_list_and_a_budget(self):
        self.change = CHANGE
        self.write_policy()
        self.assertIn("change_commands", permissions.load_policy().access())
        self.write_policy("command_rounds = 0\n")
        self.assertNotIn("change_commands", permissions.load_policy().access())
        self.commands = "[]"
        self.write_policy()
        self.assertNotIn("change_commands", permissions.load_policy().access())

    def test_plan_shape_for_a_change(self):
        access = {"files": [], "systemd": [], "weather": False, "commands": ["uname -s"],
                  "change_commands": ["systemctl restart <unit>"]}
        parsed = plans.parse_plan(change(RESTART, STATUS, REVERT), access)
        self.assertEqual((parsed["argv"], parsed["test"], parsed["undo"]), (RESTART, STATUS, REVERT))
        for bad in ({"action": "run", "argv": RESTART, "test": STATUS},
                    {"action": "run", "argv": RESTART, "test": STATUS, "undo": "x"},
                    {"action": "run", "argv": RESTART, "test": [], "undo": REVERT}):
            with self.subTest(bad=bad), self.assertRaises(plans.PlanError):
                plans.parse_plan(json.dumps(bad), access)
        plain = dict(access)
        del plain["change_commands"]
        with self.assertRaises(plans.PlanError):
            plans.parse_plan(change(RESTART, STATUS, REVERT), plain)

    def test_the_wire_step_carries_a_closed_role(self):
        step = {"op": "run", "argv": ["uname", "-s"], "status": "ok", "exit": 0, "output": "", "truncated": False}
        doc = {"steps": [dict(step, role="change")], "remaining": {"rounds": 4, "corrections": 2, "commands": 1}}
        self.assertEqual(dc.validate_discovery(doc), doc)
        for role in ("root", "", None, 1):
            with self.subTest(role=role), self.assertRaises(ValueError):
                dc.validate_discovery({"steps": [dict(step, role=role)], "remaining": doc["remaining"]})


class HostModeTests(unittest.TestCase):
    def test_host_mode_has_a_clean_environment_with_the_user_bus_and_no_home_or_secrets(self):
        _os.environ["AI_DESKTOP_SECRET_PROBE"] = "must-not-leak"
        try:
            step = command_ops.execute(command_ops.resolve("printenv"), ["printenv"], output_limit=4000, timeout=5,
                                       host=True, role="test")
        finally:
            del _os.environ["AI_DESKTOP_SECRET_PROBE"]
        self.assertEqual((step["status"], step["role"]), ("ok", "test"))
        self.assertNotIn("must-not-leak", step["output"])
        self.assertNotIn("HOME=", step["output"])
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%d/bus" % _os.geteuid(), step["output"])


if __name__ == "__main__":
    unittest.main()


class StrictChangeTemplateTests(unittest.TestCase):
    def test_a_placeholder_before_the_verb_of_a_change_is_refused(self):
        for text in ("systemctl --user <word> foo.service", "systemctl <unit>", "systemctl -- <word>"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                command_ops.parse_template(text, strict=True)
        self.assertEqual(command_ops.parse_template("systemctl --user restart <unit>", strict=True),
                         ("systemctl", "--user", "restart", "<unit>"))
        command_ops.parse_template("pacman -Qi <word>")                # reading templates keep the old rule outside subcommand tools


class ReadOnlyProfileTests(ChangeBase):
    def test_a_read_only_profile_is_given_neither_write_scopes_nor_change_commands(self):
        self.access = "read-only"
        self.facts([ANSWER_PLAN])
        access = self.wire[0]["access"]
        self.assertNotIn("change_commands", access)
        self.assertNotIn("write", access)
        self.assertEqual(access["commands"], ["uname -s", "systemctl status <unit> --no-pager"])


class ShareProcessesTests(ChangeBase):
    def test_the_option_is_off_by_default_and_must_be_a_boolean(self):
        self.write_policy()
        self.assertIs(permissions.load_policy().share_processes(), False)
        self.share_line = "share_processes = true\n"
        self.write_policy()
        self.assertIs(permissions.load_policy().share_processes(), True)
        for bad in ('"yes"', "1", "[]"):
            self.share_line = "share_processes = %s\n" % bad
            self.write_policy()
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                permissions.load_policy()

    def test_the_default_sandbox_isolates_pids_and_the_shared_one_does_not_but_keeps_every_other_restriction(self):
        with patch.object(command_ops.shutil, "which", return_value="/usr/bin/bwrap"):
            private = command_ops.sandbox_command("/usr/bin/ps", ["ps"])
            shared = command_ops.sandbox_command("/usr/bin/ps", ["ps"], share_processes=True)
        self.assertIn("--unshare-pid", private)
        self.assertIn("--proc", private)
        self.assertNotIn("--unshare-pid", shared)
        self.assertNotIn("--proc", shared)
        for flag in ("--unshare-net", "--unshare-ipc", "--cap-drop", "--clearenv", "--die-with-parent", "--new-session"):
            self.assertIn(flag, shared)
        self.assertEqual(shared[shared.index("--ro-bind"):shared.index("--ro-bind") + 3], ["--ro-bind", "/", "/"])
        self.assertEqual(shared[-1], "/usr/bin/ps")                     # the program stays last

    def test_a_read_command_uses_the_shared_view_only_when_the_owner_asked(self):
        views = []

        def fake(path, argv, *, output_limit, timeout, command=None, host=False, role="read"):
            views.append(command)
            return {"op": "run", "argv": list(argv), "status": "ok", "exit": 0, "output": "x\n", "truncated": False,
                    "role": role}
        for line, expected in (("", False), ("share_processes = true\n", True)):
            views.clear()
            self.share_line = line
            self.write_policy()
            with patch.object(command_ops, "execute", side_effect=fake):
                self.facts([ops.run("uname", "-s"), ANSWER_PLAN])
            self.assertEqual(views[0] is not None, expected)


class DefaultExampleTests(ChangeBase):
    def test_the_shipped_example_policy_loads_with_its_starter_command_list(self):
        from pathlib import Path
        example = Path(__file__).resolve().parents[1] / "config/permissions.example.toml"
        self.config.write_bytes(example.read_bytes())
        self.config.chmod(0o600)
        policy = permissions.load_policy()
        self.assertGreater(len(policy.commands()), 24)
        self.assertEqual(len(policy.access()["commands"]), len(policy.commands()))
        self.assertFalse(policy.share_processes())

    def test_the_raised_ceilings_are_accepted_and_nothing_above_them(self):
        self.write_policy('command_output = "128KiB"\ndiscovery_bytes = "256KiB"\n')
        limits = permissions.load_policy().limits()
        self.assertEqual((limits.command_output, limits.discovery_bytes), (131072, 262144))
        for line in ('command_output = "129KiB"', 'discovery_bytes = "257KiB"'):
            self.write_policy(line + "\n")
            with self.subTest(line=line), self.assertRaises(ValueError):
                permissions.load_policy()

    def test_more_templates_than_the_ceiling_are_refused(self):
        self.commands = json.dumps(["uname -s"] + ["ps -p %d" % i for i in range(128)])
        self.write_policy()
        with self.assertRaises(ValueError):
            permissions.load_policy()


class PolicyProblemTests(ChangeBase):
    def problem(self, extra="", commands=None, change=None, text=None):
        if text is not None:
            self.config.write_text(text, encoding="utf-8")
            self.config.chmod(0o600)
        else:
            if commands is not None:
                self.commands = commands
            if change is not None:
                self.change = change
            self.write_policy(extra)
        with self.assertRaises(ValueError) as caught:
            permissions.load_policy()
        self.assertEqual(caught.exception.args, ("policy-invalid",))
        return " | ".join(getattr(caught.exception, "problem", ()))

    def test_duplicate_command_templates_are_all_named(self):
        text = self.problem(commands='["uname -s", "uname -r", "uname -s", "uname -r"]')
        self.assertIn("Repeated template in commands.allow: uname -s", text)
        self.assertIn("uname -r", text)

    def test_other_defects_are_named_by_key_and_entry_without_a_path(self):
        self.assertIn("commands.share_processes", self.problem(text=
                      'version = 2\n[files]\nread = []\ndeny = []\n[commands]\nshare_processes = "yes"\nallow = ["uname -s"]\n'))
        self.assertIn("Unknown key in [files]: bogus", self.problem(text='version = 2\n[files]\nread = []\ndeny = []\nbogus = 1\n'))
        self.assertIn("version", self.problem(text='version = 3\n[files]\nread = []\ndeny = []\n'))
        self.assertIn("syntax error", self.problem(text='version = 2\n[files\n'))
        self.assertIn("files.command_output", self.problem(extra='command_output = "900KiB"\n'))
        self.assertIn("Invalid template in commands.allow: bash -c x", self.problem(commands='["bash -c x"]'))
        text = self.problem(commands='["uname -s", "uname -s"]')
        self.assertNotIn("/", text.replace("/etc", ""))

    def test_the_ask_screen_shows_the_red_headline_then_the_problem(self):
        from ai_desktop import assistant_flow
        flow_error = assistant_flow.AccessDenied("policy-invalid", ("Repeated template in commands.allow: lscpu",))
        self.assertEqual(flow_error.reason, "policy-invalid")
        self.assertEqual(flow_error.problem, ("Repeated template in commands.allow: lscpu",))
        self.write_policy()
        self.commands = '["uname -s", "uname -s"]'
        self.write_policy()
        with self.assertRaises(assistant_flow.AccessDenied) as caught:
            assistant_flow.capture_access()
        self.assertTrue(any("uname -s" in line for line in caught.exception.problem))


class ApprovedUndoTests(ChangeBase):
    def test_an_approved_undo_runs_even_when_the_byte_budget_is_gone(self):
        self.write_policy('discovery_bytes = 8192\n')
        self.results = {tuple(RESTART): (0, "y\n" * 4000), tuple(STATUS): (3, "x\n" * 4000)}
        result, _ = self.facts([change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual(result, ANSWER)
        self.assertEqual([call[2] for call in self.calls], ["change", "test", "undo"])
        steps = self.steps()
        self.assertEqual([step["exit"] for step in steps], [0, 3, 0])
        self.assertTrue(steps[0]["truncated"] and steps[1]["truncated"])
        self.assertLessEqual(dc.document_bytes(self.wire[1]["discovery"]), 8192)

    def test_when_command_metadata_alone_exceeds_the_limit_it_cannot_be_transferred(self):
        from ai_desktop.ask_discovery import DiscoveryLedger
        self.write_policy('discovery_bytes = 2048\n')
        ledger = DiscoveryLedger(permissions.load_policy(), "diagnose")
        for _ in range(20):
            ledger.add_run({"op": "run", "role": "read", "argv": ["uname", "-s"], "exit": 0,
                            "status": "ok", "output": "", "truncated": False})
        with self.assertRaisesRegex(ValueError, "discovery_bytes"):
            ledger.snapshot()


class MissingProgramTests(ChangeBase):
    def test_a_listed_program_that_is_not_installed_is_reported_as_missing_not_refused(self):
        with patch.object(command_ops, "resolve", return_value=None):
            _, facts = self.facts([ops.run("uname", "-s"), ANSWER_PLAN])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.steps()[0]["status"], "missing")
        self.assertIn("this program is not on the system", facts[0])


class AutomaticReadsTests(ChangeBase):
    def test_read_commands_never_prompt_and_stay_in_the_sandbox(self):
        self.facts([ops.run("uname", "-s"), ops.run("systemctl", "status", "foo.service", "--no-pager"), ANSWER_PLAN])
        self.assertEqual(self.asked, ["ask-question"])
        self.assertEqual([call[0] for call in self.calls],
                         [["uname", "-s"], ["systemctl", "status", "foo.service", "--no-pager"]])
        self.assertTrue(all(host is False for _, host, _ in self.calls))

    def test_automatic_reads_never_cover_a_change(self):
        self.facts([ops.run("uname", "-s"), change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual(self.asked, ["ask-question", "run-change"])
        self.assertIn(("systemctl restart foo.service".split(), True, "change"),
                      [(call[0], call[1], call[2]) for call in self.calls])

    def test_a_declined_change_is_not_run_after_automatic_reads(self):
        self.change_answer = False
        self.facts([ops.run("uname", "-s"), change(RESTART, STATUS, REVERT), ANSWER_PLAN])
        self.assertEqual([call[0] for call in self.calls], [["uname", "-s"]])


class CommandBatchTests(ChangeBase):
    def batch(self, *commands):
        return json.dumps({"action": "run", "commands": [list(words) for words in commands]})

    def test_a_batch_runs_every_command_before_one_next_planner_round(self):
        self.facts([self.batch(["uname", "-s"], ["systemctl", "status", "foo.service", "--no-pager"]), ANSWER_PLAN])
        self.assertEqual(self.asked, ["ask-question"])
        self.assertEqual(len(self.wire), 2)                                    # first plan + one round with both results
        self.assertEqual([s["argv"][0] for s in self.steps()], ["uname", "systemctl"])
        self.assertTrue(all(host is False for _, host, _ in self.calls))

    def test_a_change_inside_a_batch_is_refused_and_nothing_prompts(self):
        self.facts([self.batch(["uname", "-s"], RESTART), ANSWER_PLAN])
        self.assertEqual(self.asked, ["ask-question"])
        self.assertEqual([call[0] for call in self.calls], [["uname", "-s"]])
        self.assertEqual([s["status"] for s in self.steps()], ["ok", "not-allowed"])

    def test_a_command_outside_the_list_inside_a_batch_is_not_run(self):
        self.facts([self.batch(["uname", "-a"], ["uname", "-s"]), ANSWER_PLAN])
        self.assertEqual([call[0] for call in self.calls], [["uname", "-s"]])

    def test_a_batch_stops_at_the_owners_command_budget(self):
        self.write_policy("command_rounds = 1\n")
        self.facts([self.batch(["uname", "-s"], ["uname", "-s"], ["uname", "-s"]), ANSWER_PLAN])
        self.assertEqual(len(self.calls), 1)

    def test_a_delete_plan_fits_what_the_trash_accepts(self):
        from ai_desktop import assistant_contract, workspace_trash
        self.assertEqual(assistant_contract.MAX_DELETE_FILES, workspace_trash.MAX_DELETE)
        access = {"files": [], "systemd": [], "weather": False, "write": [{"id": "w0", "pattern": "~/**"}]}
        names = ["%d.py" % i for i in range(workspace_trash.MAX_DELETE + 1)]
        with self.assertRaises(assistant_contract.PlanError):
            assistant_contract.parse_plan(json.dumps({"action": "delete", "write": "w0", "paths": names}), access)
        good = {"action": "delete", "write": "w0", "paths": names[:-1]}
        self.assertEqual(len(assistant_contract.parse_plan(json.dumps(good), access)["paths"]), workspace_trash.MAX_DELETE)

    def test_batch_shape_is_closed(self):
        from ai_desktop import assistant_contract
        access = {"files": [], "systemd": [], "weather": False, "commands": ["uname -s"]}
        good = {"action": "run", "commands": [["uname", "-s"]] * 16}
        self.assertEqual(len(assistant_contract.parse_plan(json.dumps(good), access)["commands"]), 16)
        for bad in ({"action": "run", "commands": [["uname", "-s"]] * 17}, {"action": "run", "commands": []},
                    {"action": "run", "commands": "uname -s"}, {"action": "run", "commands": [["uname"]], "argv": ["uname"]},
                    {"action": "run", "commands": [["uname"]], "test": ["uname"], "undo": ["uname"]},
                    {"action": "run", "commands": [["a b"]]}):
            with self.subTest(bad=bad), self.assertRaises(assistant_contract.PlanError):
                assistant_contract.parse_plan(json.dumps(bad), access)


class WordAnswerTests(unittest.TestCase):
    def ask(self, action, answers):
        replies = iter(answers)
        with patch.object(terminal_consent.sys.stdin, "isatty", return_value=True), \
                patch.object(terminal_consent.sys.stdout, "isatty", return_value=True):
            return terminal_consent.confirm(action, reader=lambda prompt: next(replies))

    def test_w_is_not_an_answer_to_any_prompt(self):
        self.assertNotIn("run-command", terminal_consent.PROMPTS)
        for action in ("run-change", "run-undo"):
            self.assertIs(self.ask(action, ["w", "yes"]), True)       # asked again
        self.assertIs(self.ask("ask-question", ["w", "y"]), True)
        self.assertIs(self.ask("write", ["w", "n"]), False)


class ReviewFindingsTests(ChangeBase):
    def test_a_placeholder_never_stands_where_a_subcommand_goes_in_a_reading_template(self):
        for bad in ("systemctl --user <word> demo.service", "docker <word>", "nmcli <word> status", "btrfs <word> usage /"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                command_ops.parse_template(bad)
        for good in ("systemctl --user status <unit> --no-pager", "docker ps", "journalctl -b -u <unit> --no-pager -n <int>",
                     "pacman -Qi <word>", "last -n <int>"):
            command_ops.parse_template(good)

    def test_a_template_naming_a_denied_path_makes_the_policy_invalid_with_a_clear_problem(self):
        self.commands = json.dumps(["uname -s", "cat %s/.config/vault/secret.txt" % self.home])
        self.write_policy()
        with self.assertRaises(ValueError) as caught:
            permissions.load_policy()
        self.assertEqual(caught.exception.args, ("policy-invalid",))
        self.assertIn("files.deny", " ".join(caught.exception.problem))
        self.commands = '["uname -s", "cat /etc/hostname"]'            # an allowed, not denied path stays fine
        self.write_policy()
        permissions.load_policy()

    def test_word_cannot_smuggle_a_path(self):
        self.assertIsNone(command_ops.match((command_ops.parse_template("pacman -Qi <word>"),), ["pacman", "-Qi", "/etc/shadow"]) or None)
        self.assertIsNone(command_ops.match((command_ops.parse_template("pacman -Qi <word>"),), ["pacman", "-Qi", "../x"]) or None)

    def test_more_sources_than_the_table_keeps_are_stored_redacted_not_silently_shortened(self):
        conversation = cs.Conversation()
        sources = ["/home/u/f%d" % index for index in range(cs.MAX_SOURCE_PATHS + 1)]
        conversation.finish("pytanie", "odpowiedź zależna od wielu plików", None, sources)
        record = cs.export(conversation)
        self.assertTrue(record["turns"][0]["redacted"])
        self.assertEqual(record["turns"][0]["taint"], [])
        self.assertNotIn("zależna", json.dumps(record["history"], ensure_ascii=False))
        reloaded = cs.load(record)
        self.assertIn(cs.REDACTED if hasattr(cs, "REDACTED") else "", json.dumps(cs.export(reloaded), ensure_ascii=False))

    def test_the_session_launcher_waits_longer_than_the_helper_it_launches(self):
        from ai_desktop import analysis_contract as contract
        source = (Path(__file__).resolve().parents[1] / "ai_desktop/analysis_session_admin.py").read_text()
        self.assertIn("timeout=SESSION_SECONDS + 60", source)
        self.assertNotIn("timeout=290", source)
        self.assertGreater(contract.SESSION_SECONDS + 60, contract.SESSION_SECONDS + 20)     # beyond the unit's RuntimeMaxSec
