"""System-client host acceptance controls; no installation, network or native calls."""
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import host_system_clients_acceptance as host
from test_accept_candidate_script import tree_snapshot

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/accept_system_clients_candidate.fish"
FISH = shutil.which("fish")


class HostClientAcceptanceTests(unittest.TestCase):
    def inspect(self, provider):
        if provider == "invalid-provider":
            raise ValueError("provider")
        return {"provider": provider, "status": "compatible-untested", "version": "2.1.999",
                "version_checked": True, "protocol_checked": True, "tested": False}

    def test_explicit_opt_in_and_host_guard_precede_inspection(self):
        with patch.object(host, "require_host") as preflight, patch.object(host.client_compat, "inspect") as inspect, \
                contextlib.redirect_stdout(io.StringIO()):
            for args in ([], ["claude"], ["--allow-offline-client-check", "other"],
                         ["--allow-offline-client-check", "claude", "--login"]):
                self.assertEqual(host.main(args), 2)
            preflight.assert_not_called()
            inspect.assert_not_called()
            preflight.side_effect = ValueError("PRIVATE host detail")
            self.assertEqual(host.main(["--allow-offline-client-check", "claude"]), 2)
            inspect.assert_not_called()

    def test_selected_provider_and_local_never_probe_the_other_client(self):
        for selected, providers in (("claude", ["claude"]), ("codex", ["codex"]),
                                    ("both", ["claude", "codex"]), ("local", [])):
            with self.subTest(selected=selected), patch.object(host, "require_host"), \
                    patch.object(host.client_compat, "inspect", side_effect=self.inspect) as inspect, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(host.main(["--allow-offline-client-check", selected]), 0)
                self.assertEqual([c.args[0] for c in inspect.call_args_list], ["invalid-provider", *providers])
                self.assertIn("network=OFF profile=NONE inference=NOT_RUN", out.getvalue())

    def test_refusal_status_is_a_failure_and_never_masked_by_the_other_provider(self):
        for reason in ("client-missing", "client-unsafe", "client-layout", "client-version", "client-protocol", "client-changed", "timeout"):
            def inspect(provider):
                result = self.inspect(provider)
                if provider == "claude":
                    result["status"] = reason
                return result
            with self.subTest(reason=reason), patch.object(host, "require_host"), \
                    patch.object(host.client_compat, "inspect", side_effect=inspect), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(host.main(["--allow-offline-client-check", "both"]), 2)
                self.assertIn("ACCEPTANCE: FAIL", out.getvalue())

    def test_installed_code_is_compared_before_any_native_probe(self):
        import host_analysis_acceptance
        with patch.object(host, "require_host"), \
                patch.object(host_analysis_acceptance, "installed_matches", side_effect=ValueError("PRIVATE")) as compare, \
                patch.object(host.client_compat, "inspect") as inspect, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(host.main(["--allow-offline-client-check", "codex", "--installed"]), 2)
            compare.assert_called_once_with(native=False)
            inspect.assert_not_called()
            self.assertNotIn("PRIVATE", out.getvalue())


class SystemClientScriptTests(unittest.TestCase):
    def test_source_gates_precede_installation_and_isolated_probes(self):
        source = SCRIPT.read_text()
        sequence = ("__aidsc_step SHA_MATCH", "worktree add --detach", "__aidsc_step SCRIPT_MATCH",
                    "__aidsc_logged CI", "__aidsc_logged LOCAL", "__aidsc_step SOURCE_CLEAN",
                    "__aidsc_step METADATA", 'if test "$aid_mode" = --local-only',
                    'if test "$aid_mode" = --allow-install', "__aidsc_step PACKAGE_VERSION",
                    "__aidsc_step PACKAGE_HOOK", "__aidsc_step UPGRADE", "__aidsc_logged SYSTEM_CLIENT")
        offsets = [source.index(item) for item in sequence]
        self.assertEqual(offsets, sorted(offsets))
        self.assertIn("set -l aid_codes $pipestatus", source)
        self.assertIn("set -l aid_cycle $status", source)
        self.assertIn("ORIGINAL_HEAD=%s", source)
        for forbidden in ("fetch_codex_client", "curl", "claude-code-releases", "auth login", "--allow-ai-request", "git push", "git commit"):
            self.assertNotIn(forbidden, source)

    @unittest.skipUnless(FISH, "fish is available in the kernel CI job")
    def test_syntax_help_and_invalid_arguments_have_no_application_side_effects(self):
        subprocess.run([FISH, "--no-config", "--no-execute", str(SCRIPT)], check=True)
        for args, code in ((["--help"], 0), ([], 2), (["--allow-install", "bad", "/tmp", "claude"], 2),
                           (["--local-only", "a" * 40, "relative", "local"], 2),
                           (["--installed-check", "a" * 40, "/tmp", "other"], 2),
                           (["--local-only", "a" * 40, "/tmp", "claude", "../main"], 2)):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as temporary:
                env = {"PATH": temporary, "HOME": temporary}
                subprocess.run([FISH, "--no-config", "-c", "exit 0"], env=env, cwd=temporary, check=True)
                before = tree_snapshot(Path(temporary))
                result = subprocess.run([FISH, "--no-config", str(SCRIPT), *args], env=env, cwd=temporary,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(tree_snapshot(Path(temporary)), before)
