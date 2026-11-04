"""Model controls cross the consent boundary without becoming model tools."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import contextlib
import io
import json
import hashlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_desktop import analysis_child as child
from ai_desktop import analysis_contract as contract
from ai_desktop import analysis_sandbox as sandbox
from ai_desktop import execution, run_controls, agent_profiles
from ai_desktop import codex_child, codex_protocol
from ai_desktop import material_grant, material_loop, materials, project_step
from ai_desktop.assistant_flow import AccessSnapshot
from ai_desktop.question_consent import QuestionConsent
from ai_desktop.process_io import bounded_process
from test_report_policy import report_fixture


def controls(**changes):
    return dict(dict(provider="claude", model="sonnet", effort="high", instructions="Sprawdzaj przyczyny i dowody.",
                     profile="reviewer", access="changes"), **changes)


def changed(**changes):
    return dict(controls(), **changes)


class ExecutionValidationTests(unittest.TestCase):
    def test_run_flags_accept_extended_efforts_and_invalid_provider_never_enters_task(self):
        calls = []
        @run_controls.configured
        def task(argv):
            calls.append((argv, execution.current()))
            return 0
        for provider, effort in (("claude", "xhigh"), ("claude", "max"), ("codex", "xhigh")):
            selected = changed(provider=provider, model="default", effort=effort)
            with self.subTest(provider=provider, effort=effort), \
                    patch.object(agent_profiles, "resolve", return_value=selected) as resolve, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(task(["--effort", effort, "remaining"]), 0)
            resolve.assert_called_once_with(None, None, effort)
            self.assertEqual(calls[-1], (["remaining"], selected))
        previous = list(calls)
        with patch.object(agent_profiles, "resolve", return_value=changed(
                provider="codex", model="default", effort="max")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(task(["--effort", "max"]), 2)
        self.assertEqual(calls, previous)
        self.assertIsNone(execution.current())

    def test_provider_specific_effort_choices_match_profiles_and_refuse_autonomy_modes(self):
        for provider in execution.PROVIDERS:
            self.assertEqual(execution.efforts_for(provider), agent_profiles.efforts_for(provider))
            for effort in execution.efforts_for(provider):
                selected = changed(provider=provider, model="default", effort=effort)
                self.assertEqual(execution.validate(selected), selected)
        for provider, effort in (("codex", "max"), ("claude", "ultracode"), ("codex", "ultracode"),
                                 ("claude", "xhigh --tools Bash")):
            with self.subTest(provider=provider, effort=effort), self.assertRaises(ValueError):
                execution.validate(changed(provider=provider, model="default", effort=effort))

    def test_snapshot_is_closed_bounded_and_refuses_native_command_options(self):
        good = controls()
        self.assertEqual(execution.validate(good), good)
        cases = [[], "sonnet", {**good, "tools": "Bash"}, {k: v for k, v in good.items() if k != "access"}]
        for key, value in (("provider", "openai"), ("model", "sonnet --tools Bash"),
                           ("model", "../model"), ("effort", "ultracode"), ("effort", True),
                           ("profile", "../reviewer"), ("profile", "Review"), ("profile", "x" * 65),
                           ("access", "bypass"), ("instructions", "x" * 8193),
                           ("instructions", "\x1b[31m"), ("instructions", "\u202e"),
                           ("instructions", "\ud800")):
            cases.append(dict(good, **{key: value}))
        for bad in cases:
            with self.subTest(bad=repr(bad)[:100]), self.assertRaisesRegex(ValueError, "execution"):
                execution.validate(bad)
        self.assertIsNone(execution.validate(None))
        self.assertEqual(execution.validate(changed(instructions="ż" * 4096))["instructions"], "ż" * 4096)

    def test_context_scopes_copy_settings_and_restore_after_failure(self):
        self.assertIsNone(execution.current())
        source = controls()
        with execution.using(source) as selected:
            source["model"] = "haiku"
            selected["model"] = "opus"
            execution.current()["model"] = "default"
            self.assertEqual(execution.current()["model"], "sonnet")
            with self.assertRaises(RuntimeError):
                with execution.using(changed(model="opus")):
                    self.assertEqual(execution.current()["model"], "opus")
                    raise RuntimeError()
            self.assertEqual(execution.current()["model"], "sonnet")
        self.assertIsNone(execution.current())


class ExecutionConsentTests(unittest.TestCase):
    def test_extended_effort_roundtrip_and_approval_remain_exact(self):
        source = dict(report=None, question="synthetic", context=[])
        for provider, effort in (("claude", "xhigh"), ("claude", "max"), ("codex", "xhigh")):
            selected = changed(provider=provider, model="default", effort=effort)
            with self.subTest(provider=provider, effort=effort), execution.using(selected):
                gate = contract.AnalysisConsent(**source)
                approval = gate.authorize(**source, sha256=gate.sha256, nonce=gate.nonce)
                parsed, _ = contract.parse_request(approval.payload.encode())
                self.assertEqual(json.loads(parsed)["execution"], selected)
            with execution.using(dict(selected, effort="high")):
                with self.assertRaises(contract.AnalysisDenied):
                    gate.authorize(**source, sha256=gate.sha256, nonce=gate.nonce)

    def test_absent_controls_preserve_bytes_and_worker_never_inherits_ui_context(self):
        source = dict(report=None, question="synthetic", context=[])
        original = contract.application_input(**source)
        with execution.using(controls()):
            self.assertEqual(contract.application_input(**source, execution=None), original)
            parsed, _ = contract.parse_request(original.encode())
            self.assertNotIn("execution", json.loads(parsed))
        self.assertEqual(contract.application_input(**source), original)

    def test_all_request_modes_bind_controls_and_strip_them_before_native_stdin(self):
        material = materials.import_bytes(b"synthetic\n" * 100, "file")
        loop = material_loop.Loop(material)
        sources = [dict(report=report_fixture()), dict(report=None, question="synthetic", context=[]),
                   dict(report=None, question="synthetic", web=True),
                   dict(report=None, question="synthetic", access={"files": [], "systemd": [], "weather": False}),
                   dict(report=None, task={"instruction": "synthetic", "files": [
                       {"path": "file.txt", "content": "x", "writable": True}]}),
                   dict(report=None, question="synthetic", material=loop.request_input(1, 10000)),
                   dict(report=None, question="synthetic", project={
                       "project": {"id": "a" * 16, "files": 0, "complete": True, "progress": "", "stage": 1,
                                   "visited": [], "skipped": {key: 0 for key in project_step.SKIPPED_KEYS}},
                       "task": {"step": 1, "max_steps": 8, "bytes_left": 10000}, "observations": [], "history": []})]
        for source in sources:
            with self.subTest(source=source.keys()), execution.using(controls()):
                gate = contract.AnalysisConsent(**source)
                approval = gate.authorize(**source, sha256=gate.sha256, nonce=gate.nonce)
                parsed, sha = contract.parse_request(approval.payload.encode())
                self.assertEqual(sha, approval.sha256)
                data = json.loads(parsed)
                self.assertEqual(data["execution"], controls())
                body, selected = child.native_input("web" if source.get("web") else "analyze", parsed.encode())
                self.assertEqual(selected, controls())
                self.assertNotIn("execution", json.loads(body))
                self.assertNotIn("Sprawdzaj przyczyny", body.decode())

    def test_each_control_is_part_of_the_approval_and_changed_selection_is_refused(self):
        source = dict(report=None, question="synthetic", context=[])
        variants = [changed(model="opus"), changed(effort="low"), changed(profile="writer"),
                    changed(instructions="new instructions"), changed(access="read-only"),
                    changed(provider="codex", model="default")]
        with execution.using(controls()):
            original = contract.application_input(**source)
        for config in variants:
            with execution.using(controls()):
                gate = contract.AnalysisConsent(**source)
            with self.subTest(config=config), execution.using(config):
                self.assertNotEqual(contract.application_input(**source), original)
                with self.assertRaisesRegex(contract.AnalysisDenied, "reason=consent"):
                    gate.authorize(**source, sha256=gate.sha256, nonce=gate.nonce)

    def test_unknown_wire_controls_and_read_only_workspace_refuse_before_native(self):
        source = dict(report=None, question="synthetic", context=[])
        raw = json.loads(contract.application_input(**source, execution=controls()))
        for controls_value in (None, {**controls(), "args": ["--tools", "Bash"]}, changed(model="../model")):
            bad = dict(raw, execution=controls_value)
            with self.subTest(controls=controls_value), self.assertRaises(contract.AnalysisDenied):
                contract.parse_request(contract.canonical(bad).encode())
        task = {"instruction": "synthetic", "files": [{"path": "file.txt", "content": "x", "writable": True}]}
        with self.assertRaises(ValueError):
            contract.application_input(None, task=task, execution=changed(access="read-only"))
        body = contract.canonical({"mode": "workspace", "task": task, "execution": changed(access="read-only")}).encode()
        with self.assertRaises(contract.AnalysisDenied):
            child.native_input("analyze", body)

    def test_material_ledger_uses_the_same_bound_packet_as_the_send_gate(self):
        material = materials.import_bytes(b"synthetic\n" * 100, "file")
        grant = material_grant.MaterialGrant(material.material_id, material.sha256, "synthetic", 10)
        with execution.using(controls()):
            session = material_grant.TransferSession(material, grant, clock=lambda: 0)
            packet = session.prepare("synthetic", [(0, 20)])
            gate = contract.AnalysisConsent(None, question="synthetic", context=list(packet.context))
            self.assertEqual((packet.payload, packet.sha256), (gate.payload, gate.sha256))
            self.assertEqual(json.loads(packet.payload)["execution"], controls())

    def test_question_consent_cannot_change_controls_between_planner_and_answer(self):
        policy = Mock(spec=("recheck", "select"))
        access = {"files": [], "systemd": [], "weather": False}
        snapshot = AccessSnapshot(policy, {"claude": True, "systemd": [], "weather": False}, access)
        with execution.using(controls()):
            permit = QuestionConsent("synthetic", snapshot)
            permit.approve()
            gate = contract.AnalysisConsent(None, question="synthetic", access=access)
            permit.claim(gate, "synthetic", access=access)
            permit.authorize(gate, "synthetic", access=access)
        with execution.using(changed(model="opus")):
            answer_gate = contract.AnalysisConsent(None, question="synthetic", context=[])
            with self.assertRaisesRegex(contract.AnalysisDenied, "reason=consent"):
                permit.claim(answer_gate, "synthetic", context=[])

    def test_material_grant_cannot_switch_recipient_or_profile_at_prepare_or_handoff(self):
        material = materials.import_bytes(b"synthetic\n" * 100, "file")
        grant = material_grant.MaterialGrant(material.material_id, material.sha256, "synthetic", 10)
        for stage in ("prepare", "prepare-step", "handoff"):
            with execution.using(controls()):
                session = material_grant.TransferSession(material, grant, clock=lambda: 0)
                packet = session.prepare("synthetic", [(0, 20)]) if stage == "handoff" else None
            with self.subTest(stage=stage), execution.using(changed(model="opus")):
                with self.assertRaisesRegex(material_grant.GrantDenied, "GRANT_INVALID"):
                    if stage == "handoff":
                        session.hand_off(packet)
                    elif stage == "prepare-step":
                        session.prepare_step("synthetic", material_loop.Loop(material).request_input(1, 10000), 0)
                    else:
                        session.prepare("synthetic", [(0, 20)])
                self.assertEqual(session.requests_handed_off(), 0)
                if packet:
                    self.assertEqual(session.ledger()[0]["state"], "cancelled")

    def test_codex_material_and_project_grants_keep_the_selected_recipient_through_handoff(self):
        material = materials.import_bytes(b"synthetic\n" * 100, "file")
        config = changed(provider="codex", model="default")
        loop = material_loop.Loop(material)
        project = {
            "project": {"id": material.material_id, "files": 0, "complete": True, "progress": "", "stage": 1,
                        "visited": [], "skipped": {key: 0 for key in project_step.SKIPPED_KEYS}},
            "task": {"step": 1, "max_steps": 8, "bytes_left": 10000}, "observations": [], "history": []}
        with execution.using(config):
            for project_mode in (False, True):
                with self.subTest(project=project_mode):
                    grant = material_grant.MaterialGrant(material.material_id, material.sha256, "synthetic", 10)
                    self.assertEqual(grant.recipient, material_grant.CODEX_RECIPIENT)
                    self.assertEqual((grant.max_requests, grant.max_bytes),
                                     (material_grant.MAX_REQUESTS, material_grant.MAX_TOTAL_BYTES))
                    session = material_grant.TransferSession(material, grant, clock=lambda: 0)
                    packet = session.prepare_step("synthetic", project if project_mode else loop.request_input(1, 10000),
                                                  0 if project_mode else loop.data_bytes(), project=project_mode)
                    session.hand_off(packet)
                    parsed, _ = contract.parse_request(packet.payload.encode())
                    self.assertEqual(json.loads(parsed)["execution"]["provider"], "codex")
                    self.assertEqual(json.loads(parsed)["mode"], "project" if project_mode else "material")
                    self.assertEqual(session.requests_handed_off(), 1)
                    self.assertEqual(session.requests_left(), material_grant.MAX_REQUESTS - 1)

    def test_grants_for_the_other_provider_are_refused_without_transfer(self):
        material = materials.import_bytes(b"synthetic\n", "file")
        with execution.using(None):
            claude_grant = material_grant.MaterialGrant(material.material_id, material.sha256, "synthetic", 10)
            self.assertEqual(claude_grant.recipient, material_grant.RECIPIENT)
        with execution.using(changed(provider="codex", model="default")):
            codex_grant = material_grant.MaterialGrant(material.material_id, material.sha256, "synthetic", 10)
            with self.assertRaisesRegex(material_grant.GrantDenied, "GRANT_INVALID"):
                material_grant.TransferSession(material, claude_grant, clock=lambda: 0)
        with execution.using(controls()):
            with self.assertRaisesRegex(material_grant.GrantDenied, "GRANT_INVALID"):
                material_grant.TransferSession(material, codex_grant, clock=lambda: 0)


class ExecutionNativeTests(unittest.TestCase):
    def test_extended_claude_efforts_forward_without_changing_tools_or_runtime_limits(self):
        for action in ("analyze", "web"):
            for effort in ("xhigh", "max"):
                with self.subTest(action=action, effort=effort):
                    selected = controls(effort=effort)
                    raw = contract.application_input(None, question="synthetic", context=[], execution=selected).encode()
                    body, frozen = child.native_input("analyze", raw)
                    self.assertEqual(frozen, selected)
                    self.assertNotIn("execution", json.loads(body))
                    command = sandbox.client_command(action, frozen)
                    baseline = sandbox.client_command(action, controls())
                    index = command.index("--effort") + 1
                    self.assertEqual(command[index], effort)
                    normalized = list(command)
                    normalized[index] = "high"
                    self.assertEqual(normalized, baseline)
        with self.assertRaises(contract.AnalysisDenied):
            sandbox.client_command("analyze", controls(effort="ultracode"))

    def test_codex_xhigh_survives_private_input_and_closed_native_config(self):
        selected = changed(provider="codex", model="default", effort="xhigh")
        packet = json.loads(contract.application_input(None, question="synthetic", context=[], execution=selected))
        with execution.using(selected):
            token, request, frozen = codex_child.private_input(contract.canonical({
                "token": "synthetic-token-" + "x" * 40, "request": packet}).encode())
            command = codex_protocol.fixed_command("/synthetic/codex", model=frozen["model"], effort=frozen["effort"])
        self.assertEqual(frozen, selected)
        self.assertEqual(json.loads(request)["execution"], selected)
        self.assertEqual(token, "synthetic-token-" + "x" * 40)
        self.assertIn('model_reasoning_effort="xhigh"', command)
        packet["execution"]["effort"] = "max"
        with self.assertRaises(contract.AnalysisDenied):
            codex_child.private_input(contract.canonical({"token": token, "request": packet}).encode())

    def test_only_model_effort_and_role_change_native_command(self):
        for action in ("analyze", "web"):
            with self.subTest(action=action):
                fixed = sandbox.client_command(action)
                command = sandbox.client_command(action, controls())
                self.assertEqual(command[command.index("--model") + 1], "sonnet")
                self.assertEqual(command[command.index("--effort") + 1], "high")
                self.assertIn("Sprawdzaj przyczyny", command[command.index("--system-prompt") + 1])
                for flag in ("--tools", "--max-turns", "--permission-mode", "--mcp-config", "--setting-sources"):
                    self.assertEqual(command[command.index(flag) + 1], fixed[fixed.index(flag) + 1])
                self.assertNotIn("--agents", command)
                self.assertNotIn("--agent", command)
                self.assertNotIn("--plugin-dir", command)
                plain = sandbox.client_command(action, changed(model="default", effort="default", instructions=""))
                self.assertEqual(plain, fixed)
        for action in ("login", "status", "version"):
            with self.subTest(action=action), self.assertRaises(contract.AnalysisDenied):
                sandbox.client_command(action, controls())
        with self.assertRaises(contract.AnalysisDenied):
            sandbox.client_command("analyze", changed(provider="codex", model="default"))

    def test_child_bounds_input_and_rejects_invalid_controls_before_bridge_or_client(self):
        cases = [b"", b"[]", b"x" * (contract.MAX_PACKET_LARGE + 1),
                 b'{"mode":"web","mode":"web"}',
                 contract.canonical({"mode": "web", "execution": changed(provider="codex", model="default")}).encode(),
                 b'{"mode":"question","question":"synthetic"}']
        for raw in cases:
            errors = io.StringIO()
            with self.subTest(raw=raw[:50]), patch.object(child, "check_boundary"), \
                    patch.object(child, "install_mode_filter"), \
                    patch.object(child.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))), \
                    patch.object(child, "_Bridge") as bridge, patch.object(child.subprocess, "run") as run, \
                    contextlib.redirect_stderr(errors):
                self.assertEqual(child.main(["web", "{}"]), 2)
                bridge.assert_not_called()
                run.assert_not_called()
            self.assertEqual(errors.getvalue(), "CLIENT: REFUSED stage=input reason=input\n")

    def test_child_passes_only_data_to_native_and_keeps_outer_output_bounds(self):
        data = {"mode": "question", "question": "synthetic", "context": [], "execution": controls()}
        raw = contract.canonical(data).encode()
        bridge = Mock()
        with patch.object(child, "check_boundary"), patch.object(child, "install_mode_filter"), \
                patch.object(child.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))), \
                patch.object(child, "_Bridge", return_value=contextlib.nullcontext(bridge)), \
                patch.object(child.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(child.main(["analyze", "{}"]), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0], sandbox.client_command("analyze", controls()))
        self.assertEqual(json.loads(kwargs["input"]), {"mode": "question", "question": "synthetic", "context": []})
        self.assertEqual(kwargs["timeout"], 110)
        self.assertNotIn("capture_output", kwargs)
        self.assertNotIn("stdout", kwargs)
        self.assertNotIn("stderr", kwargs)
        bridge.shutdown.assert_called_once()

    def test_real_large_pipes_strip_controls_without_backpressure_deadlock(self):
        # A synthetic native client emits before reading. Real pipes exercise
        # the bootstrap's new stdin boundary and its existing outer limits;
        # no credentials, namespaces, network or official client are involved.
        value = {"mode": "material", "material": {"synthetic": "x" * 600000}, "execution": controls()}
        raw = contract.canonical(value).encode()
        expected = contract.canonical({k: v for k, v in value.items() if k != "execution"}).encode()
        native = ("import hashlib,sys; sys.stdout.buffer.write(b'x'*60000); sys.stdout.flush(); "
                  "raw=sys.stdin.buffer.read(); sys.stderr.write(hashlib.sha256(raw).hexdigest())")
        bootstrap = (
            "import contextlib,sys; from types import SimpleNamespace; "
            "from ai_desktop import analysis_child as c; "
            "c.check_boundary=lambda _:None; c.install_mode_filter=lambda:None; "
            "c._Bridge=lambda *_:contextlib.nullcontext(SimpleNamespace(serve_forever=lambda:None,shutdown=lambda:None)); "
            "c.client_command=lambda action,selected=None:[sys.executable,'-I','-c'," + repr(native) + "]; "
            "raise SystemExit(c.main(['analyze','{}']))")
        code, output, errors = bounded_process([sys.executable, "-B", "-c", bootstrap], env={"PATH": "/usr/bin"},
                                              input_bytes=raw, input_limit=contract.MAX_PACKET_LARGE,
                                              timeout=5, limit=100000)
        self.assertEqual((code, len(output), errors), (0, 60000, hashlib.sha256(expected).hexdigest().encode()))


class ConversationControlsTests(unittest.TestCase):
    def test_idle_effort_hints_and_selection_use_the_active_provider(self):
        for provider in execution.PROVIDERS:
            selected = changed(provider=provider, model="default")
            output = io.StringIO()
            with self.subTest(provider=provider), execution.using(selected), contextlib.redirect_stdout(output):
                self.assertTrue(run_controls.conversation_command("/ustawienia"))
                self.assertIn("|".join(execution.efforts_for(provider)), output.getvalue())
                self.assertTrue(run_controls.conversation_command("/sila xhigh"))
                self.assertEqual(execution.current()["effort"], "xhigh")
                self.assertTrue(run_controls.conversation_command("/sila max"))
                self.assertEqual(execution.current()["effort"], "max" if provider == "claude" else "xhigh")

    def test_history_is_closed_copied_and_scoped(self):
        history = [{"question": "poprzednie pytanie", "answer": "poprzednia odpowiedź"}]
        self.assertEqual(execution.current_history(), [])
        with execution.using_history(history) as selected:
            selected[0]["answer"] = "changed"
            history[0]["question"] = "changed"
            execution.current_history()[0]["answer"] = "changed"
            self.assertEqual(execution.current_history(), [
                {"question": "poprzednie pytanie", "answer": "poprzednia odpowiedź"}])
        self.assertEqual(execution.current_history(), [])
        for bad in (None, (), {}, [{"question": "q"}], [{"question": "q", "answer": "a", "tools": "Bash"}],
                    [{"question": "q", "answer": "a"}] * 9,
                    [{"question": "x" * 2049, "answer": "a"}],
                    [{"question": "q", "answer": "x" * 3137}],
                    [{"question": "q", "answer": "\x1b[31m"}],
                    [{"question": "q", "answer": "\ud800"}]):
            with self.subTest(bad=repr(bad)[:60]), self.assertRaisesRegex(ValueError, "conversation"):
                execution.validate_history(bad)

    def test_history_roundtrip_is_bound_only_to_assistant_and_question(self):
        history = [{"question": "q", "answer": "a"}]
        with execution.using_history(history):
            for kwargs in (dict(context=[]), dict(access={"files": [], "systemd": [], "weather": False})):
                source = dict(report=None, question="synthetic", **kwargs)
                gate = contract.AnalysisConsent(**source)
                approval = gate.authorize(**source, sha256=gate.sha256, nonce=gate.nonce)
                parsed, _ = contract.parse_request(approval.payload.encode())
                self.assertEqual(json.loads(parsed)["conversation"], history)
                body, _ = child.native_input("analyze", parsed.encode())
                self.assertEqual(json.loads(body)["conversation"], history)
            for source in (dict(report=report_fixture()), dict(report=None, question="synthetic", web=True),
                           dict(report=None, task={"instruction": "synthetic", "files": [
                               {"path": "file.txt", "content": "x", "writable": True}]})):
                payload = contract.application_input(**source)
                self.assertNotIn("conversation", json.loads(payload))
                parsed, _ = contract.parse_request(payload.encode())
                self.assertNotIn("conversation", json.loads(parsed))
                tampered = dict(json.loads(payload), conversation=history)
                with self.assertRaises(contract.AnalysisDenied):
                    contract.parse_request(contract.canonical(tampered).encode())

    def test_worker_does_not_inherit_ambient_history_and_changes_revoke_question_consent(self):
        source = dict(report=None, question="synthetic", context=[])
        original = contract.application_input(**source)
        policy = Mock(spec=("recheck", "select"))
        snapshot = AccessSnapshot(policy, {"claude": True, "systemd": [], "weather": False},
                                  {"files": [], "systemd": [], "weather": False})
        with execution.using_history([{"question": "q", "answer": "a"}]):
            parsed, _ = contract.parse_request(original.encode())
            self.assertNotIn("conversation", json.loads(parsed))
            permit = QuestionConsent("synthetic", snapshot)
            permit.approve()
            gate = contract.AnalysisConsent(**source)
        with execution.using_history([{"question": "q", "answer": "changed"}]):
            with self.assertRaises(contract.AnalysisDenied):
                gate.authorize(**source, sha256=gate.sha256, nonce=gate.nonce)
            with self.assertRaises(contract.AnalysisDenied):
                permit.check()
