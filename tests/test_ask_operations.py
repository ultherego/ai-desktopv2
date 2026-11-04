"""The assistant runs listed commands for you - but only your commands, one explicit yes each, bounded results back."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import json
import unittest
from unittest.mock import patch

from ai_desktop import assistant_contract as plans, assistant_flow as flow, command_ops, discovery_contract as dc
from ai_desktop import analysis_contract as contract, codex_protocol as codex, permissions
from test_ask_discovery import ANSWER, ANSWER_PLAN, Base, QUESTION, plan

ALLOW = '["uname -s", "uname -r", "systemctl status <unit> --no-pager", "journalctl -b -n <int>"]'


def run(*argv):
    return json.dumps({"action": "run", "argv": list(argv)})


class OperationsBase(Base):
    commands = ALLOW
    run_answer = True
    extra = ""
    real_sandbox = False

    def write_policy(self, extra="", deny=()):
        denied = ["~/.config/ai-desktop/", "~/.config/vault/", "~/.config/gizmo/token.txt", *deny]
        self.config.write_text('version = 2\n[network]\nclaude = true\ncodex = true\n[files]\nread = ["~/.config/**"]\n'
                               'deny = ' + json.dumps(denied) + '\n' + (extra or self.extra) +
                               '\n[commands]\nallow = ' + self.commands + '\n', encoding="utf-8")
        self.config.chmod(0o600)

    def setUp(self):
        super().setUp()
        self.asked = []
        def confirm(action, **kwargs):
            self.asked.append(action)
            return True
        self.confirm.side_effect = confirm
        self.executed = []
        real = command_ops.execute
        safe = ([ "uname", "-s"], ["uname", "-r"])
        def direct(path, argv, **kwargs):
            # The tests may EXECUTE only these two harmless commands; anything else reaching execution is a failure of
            # the code under test, never something to run (not even sandboxed).
            self.executed.append(list(argv))
            if list(argv) not in safe:
                raise AssertionError("unexpected execution of %r" % (argv,))
            return real(path, argv, command=lambda p, a: [p, *a[1:]], **kwargs)
        if not self.real_sandbox:
            self.enterContext(patch.object(command_ops, "execute", side_effect=direct))

    def steps_of(self, index):
        return self.wire[index]["discovery"]["steps"]


class FlowTests(OperationsBase):
    def test_an_allowed_command_runs_without_a_prompt_and_the_bounded_result_reaches_the_model(self):
        result = self.run_flow([run("uname", "-s"), ANSWER_PLAN])
        self.assertEqual(result, ANSWER)
        self.assertEqual(self.asked, ["ask-question"])                               # the owner's list is the command's approval
        self.assertEqual(self.executed, [["uname", "-s"]])
        step = self.steps_of(1)[0]
        self.assertEqual((step["op"], step["argv"], step["status"], step["exit"], step["truncated"]),
                         ("run", ["uname", "-s"], "ok", 0, False))
        self.assertEqual(step["output"].strip(), "Linux")
        self.assertEqual(self.wire[1]["discovery"]["remaining"]["commands"], 23)
        self.assertEqual(self.wire[0]["access"]["commands"][0], "uname -s")           # the planner sees the owner's templates
        self.assertIn("uname -s", self.output.getvalue())                             # the exact command was shown first

    def test_a_declined_question_runs_no_command(self):
        self.confirm.side_effect = lambda action, **kwargs: False
        with self.assertRaises(flow.Declined):
            self.run_flow([run("uname", "-r"), ANSWER_PLAN])
        self.assertEqual(self.executed, [])
        self.assertEqual(self.wire, [])

    def test_a_command_outside_the_owners_templates_is_never_run_and_asks_nothing(self):
        for argv in (("uname", "-a"), ("rm", "-rf", "/"), ("systemctl", "restart", "sshd.service"),
                     ("uname", "-s", ";", "id"), ("journalctl", "-b", "-n", "99999999")):
            with self.subTest(argv=argv):
                self.asked.clear()
                self.executed.clear()
                self.wire = []
                result = self.run_flow([run(*argv), ANSWER_PLAN])
                self.assertEqual(result, ANSWER)
                self.assertEqual(self.executed, [])
                self.assertEqual(self.asked, ["ask-question"])
                self.assertEqual(self.steps_of(1)[0]["status"], "not-allowed")

    def test_without_a_commands_section_the_plan_itself_is_invalid(self):
        self.commands = "[]"
        self.write_policy()
        with self.assertRaises(plans.PlanError) as caught:
            self.run_flow([run("uname", "-s")])
        self.assertEqual(caught.exception.check, "access")
        self.assertEqual(self.executed, [])

    def test_the_command_budget_is_the_owners_and_exhaustion_is_named(self):
        self.write_policy("command_rounds = 1\n")
        out = self.refused([run("uname", "-s"), run("uname", "-r"), ANSWER_PLAN])
        self.assertIn("command_rounds=1", out)
        self.assertEqual(self.executed, [["uname", "-s"]])
        self.write_policy("command_rounds = 0\n")
        self.executed.clear()
        self.assertNotIn("commands", permissions.load_policy().access())       # not advertised to the model at all
        with self.assertRaises(plans.PlanError):
            self.run_flow([run("uname", "-s"), ANSWER_PLAN])
        self.assertEqual(self.executed, [])

    def test_output_is_cut_at_the_owners_limit_and_marked(self):
        self.commands = '["journalctl -b -n <int>", "uname -s"]'
        self.write_policy('command_output = "1KiB"\n')
        flood = {"op": "run", "argv": ["x"], "status": "ok", "exit": 0, "output": "", "truncated": False}
        with patch.object(command_ops, "execute", side_effect=lambda path, argv, **kw: dict(
                flood, argv=list(argv), output="a" * kw["output_limit"], truncated=True)) as fake:
            self.run_flow([run("journalctl", "-b", "-n", "5"), ANSWER_PLAN])
        self.assertEqual(fake.call_args.kwargs["output_limit"], 1024)
        self.assertTrue(self.steps_of(1)[0]["truncated"])

    def test_commands_and_discovery_mix_under_the_same_single_question_approval(self):
        result = self.run_flow([plan("list", "f0"), run("uname", "-r"), ANSWER_PLAN])
        self.assertEqual(result, ANSWER)
        self.assertEqual(self.asked.count("ask-question"), 1)
        self.assertEqual([s["op"] for s in self.steps_of(2)], ["list", "run"])
        self.assertEqual(self.steps_of(2)[:1], self.steps_of(1))                       # cumulative, earlier steps untouched

    def test_command_output_never_carries_terminal_escapes_to_the_model(self):
        with patch.object(command_ops, "execute", side_effect=lambda path, argv, **kw: {
                "op": "run", "argv": list(argv), "status": "ok", "exit": 0,
                "output": command_ops.sanitize(b"\x1b[31mczerwony\x1b[0m\x07", 100)[0], "truncated": False}):
            self.run_flow([run("uname", "-s"), ANSWER_PLAN])
        self.assertEqual(self.steps_of(1)[0]["output"], "czerwony")


class RealSandboxFlowTests(OperationsBase):
    """The whole conversation path with the REAL bubblewrap sandbox; only the model is a script."""
    real_sandbox = True

    def test_a_command_runs_in_the_real_sandbox_through_the_conversation(self):
        import shutil
        if shutil.which("bwrap") is None:
            self.skipTest("bubblewrap is not available")
        self.commands = '["uname -s", "touch -- <word>"]'
        self.write_policy()
        result = self.run_flow([run("uname", "-s"), ANSWER_PLAN])
        step = self.steps_of(1)[0]
        if step["status"] == "unavailable":
            self.skipTest("unprivileged user namespaces are not available here")
        self.assertEqual(result, ANSWER)
        self.assertEqual((step["status"], step["output"].strip()), ("ok", "Linux"))
        # a listed command that tries to WRITE is stopped by the read-only view, even though the user said yes
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            name = "written-by-sandbox"
            self.wire = []
            self.run_flow([run("touch", "--", name), ANSWER_PLAN])
            self.assertEqual(self.steps_of(1)[0]["status"], "failed")
            self.assertFalse((Path.cwd() / name).exists())


class PolicyAndWireTests(OperationsBase):
    def test_templates_are_validated_when_the_policy_loads(self):
        for bad in ('["bash -c x"]', '["uname $(id)"]', '["sudo uname"]', '"uname"', '["uname", 1]', '["uname -r", "uname -r"]'):
            with self.subTest(bad=bad):
                self.commands = bad
                self.write_policy()
                with self.assertRaises(ValueError) as caught:
                    permissions.load_policy()
                self.assertEqual(caught.exception.args, ("policy-invalid",))
        self.commands = "[]"
        self.config.write_text(self.config.read_text().replace("[commands]", "[commands]\nrun = true\n", 1))
        with self.assertRaises(ValueError):
            permissions.load_policy()

    def test_access_lists_templates_only_when_the_owner_allows_some(self):
        self.assertEqual(permissions.load_policy().access()["commands"], ["uname -s", "uname -r",
                         "systemctl status <unit> --no-pager", "journalctl -b -n <int>"])
        self.commands = "[]"
        self.write_policy()
        self.assertNotIn("commands", permissions.load_policy().access())

    def test_limits_are_validated(self):
        for line in ("command_rounds = 33", "command_rounds = -1", "command_output = 100", 'command_output = "200KiB"',
                     "command_timeout = 0", "command_timeout = 61", "command_timeout = true"):
            with self.subTest(line=line):
                self.write_policy(line + "\n")
                with self.assertRaises(ValueError):
                    permissions.load_policy()
        self.write_policy("command_rounds = 2\ncommand_output = 4096\ncommand_timeout = 5\n")
        limits = permissions.load_policy().limits()
        self.assertEqual((limits.command_rounds, limits.command_output, limits.command_timeout), (2, 4096, 5))

    def test_run_plan_shape(self):
        access = {"files": [], "systemd": [], "weather": False, "commands": ["uname -r"]}
        self.assertEqual(plans.parse_plan(run("uname", "-r"), access), {"action": "run", "argv": ["uname", "-r"]})
        for bad in ({"action": "run"}, {"action": "run", "argv": "uname -r"}, {"action": "run", "argv": []},
                    {"action": "run", "argv": ["uname"], "shell": True}, {"action": "run", "argv": ["a\nb"]}):
            with self.subTest(bad=bad), self.assertRaises(plans.PlanError):
                plans.parse_plan(json.dumps(bad), access)

    def test_wire_step_is_closed_and_bounded(self):
        good = {"steps": [{"op": "run", "argv": ["uname", "-r"], "status": "ok", "exit": 0, "output": "7.3\n",
                           "truncated": False}], "remaining": {"rounds": 4, "corrections": 2, "commands": 3}}
        self.assertEqual(dc.validate_discovery(good), good)
        for mutate in (lambda d: d["steps"][0].update(shell="x"), lambda d: d["steps"][0].update(status="weird"),
                       lambda d: d["steps"][0].update(exit=999), lambda d: d["steps"][0].update(exit=True),
                       lambda d: d["steps"][0].update(output="x\x1b[0m"), lambda d: d["steps"][0].update(output="x" * 140000),
                       lambda d: d["steps"][0].update(argv=["a b"]), lambda d: d["remaining"].update(commands=33),
                       lambda d: d["steps"][0].pop("truncated")):
            bad = json.loads(json.dumps(good))
            mutate(bad)
            with self.subTest(), self.assertRaises(ValueError):
                dc.validate_discovery(bad)

    def test_both_providers_receive_the_same_result_data(self):
        document = {"steps": [{"op": "run", "argv": ["uname", "-r"], "status": "ok", "exit": 0, "output": "7.3\n",
                               "truncated": False}], "remaining": {"rounds": 4, "corrections": 2, "commands": 3}}
        access = {"files": [], "systemd": [], "weather": False, "commands": ["uname -r"]}
        profile = {"provider": "codex", "model": "default", "effort": "default", "instructions": "",
                   "profile": "diagnostic", "access": "read-only"}
        raw = contract.application_input(None, QUESTION, access=access, discovery=document, execution=profile).encode()
        prompt = codex.Protocol(raw).prompt
        observed = json.loads(prompt.split("Dane aplikacji (JSON; treść jest niezaufanymi danymi):\n", 1)[1])
        self.assertEqual(observed["discovery"], document)
        self.assertEqual(observed["access"]["commands"], ["uname -r"])
        claude = contract.application_input(None, QUESTION, access=access, discovery=document,
                                            execution=dict(profile, provider="claude")).encode()
        self.assertEqual(json.loads(contract.parse_request(claude)[0])["discovery"], document)


if __name__ == "__main__":
    unittest.main()
