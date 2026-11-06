"""Disposable-CI guards run offline in the normal central gate."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ci_analysis_systemd as driver


class CiAnalysisGuardTests(unittest.TestCase):
    def test_no_flag_does_not_probe_or_install(self):
        with patch.object(driver, "require_ci") as preflight, patch.object(driver, "install") as install, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(driver.main([]), 2)
            self.assertEqual(driver.main(["--allow-disposable-analysis-test", "sh"]), 2)
        preflight.assert_not_called()
        install.assert_not_called()

    def test_non_ci_cannot_install_even_with_flag(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(driver, "install") as install, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(driver.main(["--allow-disposable-analysis-test"]), 2)
        install.assert_not_called()

    def test_preflight_errors_are_redacted(self):
        out = io.StringIO()
        with patch.object(driver, "require_ci", side_effect=ValueError("SECRET")), \
                patch.object(driver, "install") as install, contextlib.redirect_stdout(out):
            self.assertEqual(driver.main(["--allow-disposable-analysis-test"]), 2)
        self.assertNotIn("SECRET", out.getvalue())
        install.assert_not_called()

    def test_fixture_installs_complete_skills_for_all_default_profiles(self):
        from ai_desktop import agent_profiles, skills
        # Profile stores deliberately reject /tmp as a writable ancestor.
        with tempfile.TemporaryDirectory(dir=Path.home()) as temp:
            destination = Path(temp) / "installed"
            home = Path(temp) / "home"
            home.mkdir(mode=0o700)

            def install(*command):
                self.assertEqual(command[:7], ("sudo", "-n", "/usr/bin/install", "-o", "root", "-g", "root"))
                self.assertTrue(Path(command[-1]).is_relative_to(destination))
                # Exercise real copying/modes in a temporary tree. Ordinary CI
                # runs as an unprivileged user; only ownership assignment is
                # omitted here, after asserting the actual CI install uses root.
                subprocess.run([command[2], *command[7:]], check=True, capture_output=True)

            with patch.object(driver.prior, "DEST", destination), patch.object(driver.prior, "_run", side_effect=install):
                driver.install_skills()
            source = driver.ROOT / "skills"
            installed = destination / "skills"
            expected = {path.relative_to(source): path.read_bytes() for path in source.rglob("*.md")}
            self.assertEqual({path.relative_to(installed): path.read_bytes()
                              for path in installed.rglob("*.md")}, expected)
            for path in (installed, *installed.rglob("*")):
                self.assertEqual(path.stat().st_mode & 0o777, 0o755 if path.is_dir() else 0o644)
            with patch.object(skills, "ROOT", installed), patch.object(skills, "user_root", return_value=home / "private-skills"):
                store = agent_profiles.Store(home=home)
                for name, profile in store.load().config.entries:
                    with self.subTest(profile=name):
                        selected = agent_profiles.resolve(profile=name, store=store)
                        for skill in profile.skills:
                            self.assertIn("- " + skill + ":", selected["instructions"])
                            snapshot = skills.bundle(skill)
                            for reference in snapshot["references"]:
                                self.assertIn(skill + "/" + reference + ":", selected["instructions"])
                # Installed-source checks are real: neither missing nor unsafe
                # resources can silently fall back to the source checkout.
                reference = installed / "arch-pacman/references/package-queries.md"
                reference.chmod(0o666)
                with self.assertRaises(agent_profiles.ProfileError):
                    agent_profiles.resolve(store=store)
                reference.chmod(0o644)
                (installed / "concise-assistant/SKILL.md").unlink()
                with self.assertRaises(agent_profiles.ProfileError):
                    agent_profiles.resolve(store=store)

    def test_skill_install_refuses_linked_bundle_before_privileged_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(driver.ROOT / "skills", root / "skills")
            reference = root / "skills/arch-pacman/references/package-queries.md"
            reference.unlink()
            reference.symlink_to(driver.ROOT / "skills/arch-pacman/references/package-queries.md")
            with patch.object(driver, "ROOT", root), patch.object(driver.prior, "_run") as command:
                with self.assertRaises(ValueError):
                    driver.install_skills()
                command.assert_not_called()

    def test_native_fixture_accepts_frozen_default_catalog_and_rejects_changed_controls(self):
        from ai_desktop import agent_profiles, analysis_child, analysis_contract, analysis_sandbox
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory(dir=Path.home()) as home, \
                patch.object(driver.prior, "_run"):
            driver.install(Path(temp), home)
            namespace = {"__name__": "fixture_validator_test"}
            exec(compile((Path(temp) / "fixture-client.py").read_text(), "fixture-client.py", "exec"), namespace)
            validate = namespace["validate_invocation"]
            selected = agent_profiles.resolve(store=agent_profiles.Store(home=Path(home)))
            self.assertEqual(namespace["EXECUTION_FIXTURE"], selected)
            self.assertIn(agent_profiles.CATALOG_HEADER, selected["instructions"])
            sources = [dict(report=driver.report_fixture(), question="SYNTHETIC: analysis question"),
                       dict(report=None, question="SYNTHETIC", context=[]),
                       dict(report=None, question="SYNTHETIC", web=True),
                       dict(report=None, question="SYNTHETIC", access={"files": [], "systemd": [], "weather": False}),
                       dict(report=None, task={"instruction": "SYNTHETIC", "files": [
                           {"path": "file.txt", "content": "SYNTHETIC", "writable": True}]})]
            for controls in (None, selected):
                for source in sources:
                    with self.subTest(controls=controls is not None, source=source.keys()):
                        approved = analysis_contract.application_input(**source, execution=controls)
                        parsed, _ = analysis_contract.parse_request(approved.encode())
                        action = "web" if source.get("web") else "analyze"
                        body, frozen = analysis_child.native_input(action, parsed.encode())
                        self.assertNotIn("execution", json.loads(body))
                        self.assertEqual(frozen, controls)
                        command = analysis_sandbox.client_command(action, frozen)
                        environment = analysis_sandbox.client_environment(action, frozen["effort"] if frozen else None)
                        self.assertEqual(validate(command, environment), action)
            for action in ("version", "status", "login", "offline-logout"):
                self.assertEqual(validate(analysis_sandbox.client_command(action),
                                          analysis_sandbox.client_environment(action)), action)
            command = analysis_sandbox.client_command("analyze", selected)
            environment = analysis_sandbox.client_environment("analyze", selected["effort"])
            for case in ("prompt", "tools", "model", "environment"):
                altered, env = list(command), dict(environment)
                if case == "prompt":
                    altered[-1] += "\nUNAPPROVED"
                elif case == "tools":
                    altered[altered.index("--tools") + 1] = "Bash"
                elif case == "model":
                    altered.extend(["--model", "opus"])
                else:
                    env["ANTHROPIC_API_KEY"] = "SYNTHETIC"
                with self.subTest(case=case), self.assertRaises(AssertionError):
                    validate(altered, env)

    def test_generated_fixture_keeps_policy_guards_and_limits_override_to_home(self):
        # Installation/compiler/systemd calls are intercepted. Only new,
        # synthetic fixture files are written in temporary directories.
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as home, \
                patch.object(driver.prior, "_run") as install:
            fixture = driver.install(Path(temp), home)
            source = (Path(temp) / "fixture-ui.py").read_text()
            compile(source, "fixture-ui.py", "exec")
            self.assertIn("permissions._home = lambda uid:", source)
            self.assertIn("return original_read(*args, **kwargs)", source)
            self.assertNotIn("permissions._identity =", source)
            self.assertNotIn("permissions.load_policy =", source)
            driver.file_fixture.check_unchanged(fixture)
            self.assertTrue(install.called)
            # Load only the generated validator; __main__ and every native
            # namespace/profile/transport operation remain unexecuted.
            client = (Path(temp) / "fixture-client.py").read_text()
            namespace = {"__name__": "fixture_validator_test"}
            exec(compile(client, "fixture-client.py", "exec"), namespace)
            data = {"mode": "question", "question": driver.file_fixture.QUESTION,
                    "context": [driver.file_fixture.fixture_context(fixture)]}
            with patch.object(Path, "exists", return_value=False):
                namespace["validate_file_payload"](data)
                for case in ("truncated", "omitted-file", "changed-tail"):
                    altered = copy.deepcopy(data)
                    if case == "omitted-file":
                        altered["context"][0]["files"].pop()
                    else:
                        selected = next(item for item in altered["context"][0]["files"]
                                        if Path(item["path"]).name == "notes.txt")
                        if case == "truncated":
                            selected["content"] = selected["content"][:4096]
                        else:
                            selected["content"] = selected["content"][:-1] + "X"
                    with self.subTest(case=case), self.assertRaises((AssertionError, ValueError)):
                        namespace["validate_file_payload"](altered)
            home_data = {"mode": "workspace", "task": {"instruction": driver.home_fixture.QUESTION, "files": [
                {"path": "test/context.txt", "content": driver.home_fixture.CONTEXT, "writable": True},
                {"path": "test/hello.py", "content": None, "writable": True}]}}
            namespace["validate_home_payload"](home_data)
            for case in ("unselected", "denied", "changed-content", "existing-new-file"):
                altered = copy.deepcopy(home_data)
                if case in ("unselected", "denied"):
                    altered["task"]["files"].append({"path": "noise/000.txt" if case == "unselected" else "private/secret.txt",
                                                     "content": "unexpected", "writable": True})
                elif case == "changed-content":
                    altered["task"]["files"][0]["content"] = "truncated"
                else:
                    altered["task"]["files"][1]["content"] = "already exists"
                with self.subTest(case=case), self.assertRaises(AssertionError):
                    namespace["validate_home_payload"](altered)
            from ai_desktop.web_search_result import web_text
            from ai_desktop.analysis_contract import AnalysisDenied
            web_data = {"mode": "web", "question": driver.web_fixture.QUESTION}
            namespace["validate_web_payload"](web_data)
            for key in ("context", "access", "task", "report"):
                with self.subTest(key=key), self.assertRaises(AssertionError):
                    namespace["validate_web_payload"]({**web_data, key: {}})
            def transcript(mode="success"):
                return ("\n".join(json.dumps(event) for event in namespace["web_events"](mode)) + "\n").encode()
            answer = web_text(transcript())
            self.assertIn("SYNTHETIC WEB: metadata=PASS", answer)
            self.assertIn("https://www.python.org/downloads/", answer)
            for scenario, reason in (("web-no-results", "web-no-results"),
                                     ("web-forged-source", "result"), ("web-foreign-tool", "result")):
                with self.subTest(scenario=scenario), self.assertRaises(AnalysisDenied) as denied:
                    web_text(transcript(scenario))
                self.assertEqual(denied.exception.reason, reason)

    def test_compact_analysis_check_rejects_missing_data_consent_and_diagnostics(self):
        question = "SYNTHETIC: analysis question"
        output = (b'Report to analyze:\n{"kernel": "0.0.0-synthetic"}\nQuestion:\n'
                  + question.encode() + b"\n" + driver.PROMPTS["transfer"].encode()
                  + b"\nSYNTHETIC ANALYSIS: isolation=PASS proxy-denial=PASS profile=PASS\n")
        driver.compact_analysis(output, question)
        for bad in (output.replace(question.encode(), b""),
                    output.replace(b'"kernel": "0.0.0-synthetic"', b""),
                    output.replace(driver.PROMPTS["transfer"].encode(), b""),
                    output + b"ANALYSIS: PASS\n", output + b"a" * 64):
            with self.subTest(output=bad), self.assertRaises(ValueError):
                driver.compact_analysis(bad, question)

    def test_no_config_case_restores_synthetic_policy_when_question_fails(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as data:
            fixture = driver.file_fixture.create_fixture(home, data)
            reached = []
            def question(action, **kwargs):
                if "SYNTHETIC: unconfigured question" in action:
                    reached.append(True)
                    self.assertFalse(fixture["policy"].exists())
                    raise RuntimeError("synthetic question failure")
                self.assertTrue(fixture["policy"].exists())
                return b""
            with patch.object(driver, "mode"), patch.object(driver, "daily_pty", side_effect=question), \
                    self.assertRaisesRegex(RuntimeError, "synthetic question failure"):
                driver.ask_cycle(Path(data), fixture)
            self.assertEqual(reached, [True])
            self.assertFalse(fixture["policy"].with_suffix(".saved").exists())
            driver.file_fixture.check_unchanged(fixture)

    def test_compact_task_check_retains_fenced_diff_and_two_ordered_consents(self):
        output = (driver.PROMPTS["transfer"].encode()
                  + b"\nTest on a copy: passed (python-unittest); 2 selected file(s)."
                  + b"\n=== Changes to save ===\n```diff\n"
                  + b"--- a/calc.py\n+++ b/calc.py\n-    return a - b\n+    return a + b\n```\n"
                  + driver.PROMPTS["write"].encode() + b"\nChanges saved.\n")
        driver.compact_task(output)
        rollback = driver.PROMPTS["rollback"].encode() + b"\nChanges undone.\n"
        driver.compact_task(rollback, rollback=True)
        for bad in (output.replace(b"-    return a - b", b""),
                    output.replace(b"```diff\n", b""),
                    output.replace(b"\n```", b""),
                    output + b"Test on a copy: passed (python-unittest).\n",
                    output.replace(b"2 selected file(s)", b"1 selected file(s)"),
                    output.replace(driver.PROMPTS["transfer"].encode(), b""),
                    output.replace(driver.PROMPTS["write"].encode(), b"")
                        + driver.PROMPTS["write"].encode() * 2,
                    driver.PROMPTS["write"].encode() + output.replace(driver.PROMPTS["write"].encode(), b""),
                    output + b"TASK: PASS\n", output + b"a" * 64):
            with self.subTest(output=bad), self.assertRaises(ValueError):
                driver.compact_task(bad)
        with self.assertRaises(ValueError):
            driver.compact_task(rollback.replace(driver.PROMPTS["rollback"].encode(), b""), rollback=True)
