"""No version string or partial probe authorizes profile/network access."""
import json
import unittest
from unittest.mock import patch

from ai_desktop import client_compat as compat
from ai_desktop.analysis_contract import AnalysisDenied


def observations(provider="claude", version=None):
    version = version or {"claude": "2.1.312", "codex": "0.166.0"}[provider]
    text = version + " (Claude Code)\n" if provider == "claude" else "codex-cli " + version + "\n"
    proof = ({"loggedIn": False, "authMethod": "none", "configDirectory": "/home/client/profile"}
             if provider == "claude" else {"initialize": True, "signed_out": True, "models": True})
    status = (1 if provider == "claude" else 0, json.dumps(proof).encode(), b"")
    return {"version": (0, text.encode(), b""), "compat-help": (0, b"Usage: native\n", b""),
            "compat-options": status, "compat-web": status,
            "compat-reject": (2, b"", b"unknown option " + compat.INVALID_OPTION.encode()),
            "compat-rpc": status}


class ClientCompatibilityTests(unittest.TestCase):
    def test_baseline_and_newer_same_major_require_every_probe(self):
        for provider, version, tested in (("claude", "2.1.312", True), ("claude", "2.1.318", False),
                                          ("codex", "0.166.0", True), ("codex", "0.167.0", False)):
            with self.subTest(provider=provider, version=version):
                values, calls = observations(provider, version), []
                def run(action):
                    calls.append(action)
                    return values[action]
                result = compat.check(provider, run)
                self.assertEqual(result.version, version)
                self.assertEqual(result.tested, tested)
                self.assertTrue(result.protocol_checked)
                self.assertEqual(result.status, "compatible-tested" if tested else "compatible-untested")
                self.assertEqual(calls, ["version"] +
                                 (["compat-options", "compat-web"] if provider == "claude" else ["compat-help"]) +
                                 ["compat-reject", "compat-rpc"])

    def test_old_major_or_malformed_version_refuses_before_any_other_probe(self):
        for provider, version in (("claude", "2.1.311"), ("claude", "3.0.0"),
                                  ("codex", "0.165.9"), ("codex", "1.0.0"),
                                  ("claude", "SECRET\x1b[31m"), ("codex", "0.167.0-preview")):
            values, calls = observations(provider, version), []
            def run(action):
                calls.append(action)
                return values[action]
            with self.subTest(provider=provider, version=version), self.assertRaises(AnalysisDenied) as raised:
                compat.check(provider, run)
            self.assertEqual(raised.exception.reason, "client-version")
            self.assertNotIn("SECRET", str(raised.exception))
            self.assertEqual(calls, ["version"])

    def test_missing_capability_and_ignored_unknown_flag_are_refused(self):
        for action, replacement in (("compat-options", (2, b"", b"SECRET unknown option")),
                                     ("compat-options", (0, b"", b"")),
                                     ("compat-web", (1, b"", b"")),
                                     ("compat-reject", (0, b"help ignores bad args", b"")),
                                     ("compat-reject", (-9, b"", b"")),
                                     ("compat-reject", (124, b"", b""))):
            values = observations()
            values[action] = replacement
            with self.subTest(action=action, replacement=replacement), self.assertRaises(AnalysisDenied) as raised:
                compat.check("claude", values.__getitem__)
            self.assertEqual(raised.exception.reason, "client-protocol")
            self.assertNotIn("SECRET", str(raised.exception))

    def test_eager_help_and_rejected_known_option_cannot_prove_claude_capabilities(self):
        for action in ("compat-options", "compat-web"):
            for replacement in ((0, b"Usage: claude [options]\n", b""),
                                (1, b"", b"error: unknown option --restricted"),
                                (2, b"", b"error: unknown option --tools")):
                values, calls = observations(), []
                values[action] = replacement
                def run(phase):
                    calls.append(phase)
                    return values[phase]
                with self.subTest(action=action, replacement=replacement), self.assertRaisesRegex(AnalysisDenied, "client-protocol"):
                    compat.check("claude", run)
                self.assertEqual(calls[-1], action)
                self.assertNotIn("compat-reject", calls)

    def test_negative_control_requires_its_own_unknown_option_and_no_signed_out_json(self):
        for code in (1, 2):
            values = observations()
            values["compat-reject"] = (code, b"", b"error: unknown option " + compat.INVALID_OPTION.encode())
            self.assertTrue(compat.check("claude", values.__getitem__).protocol_checked)
            for output, errors in ((b"", b"error: unknown option --restricted"),
                                   (b"", b"incidental failure"),
                                   (values["compat-rpc"][1], b"ignored " + compat.INVALID_OPTION.encode())):
                values["compat-reject"] = code, output, errors
                with self.subTest(code=code, output=output, errors=errors), self.assertRaisesRegex(AnalysisDenied, "client-protocol"):
                    compat.check("claude", values.__getitem__)

    def test_claude_option_probes_use_full_runtime_flags_without_eager_help(self):
        from ai_desktop.analysis_sandbox import client_command
        for action, mode in (("compat-options", "analyze"), ("compat-web", "web")):
            command = client_command(action)
            self.assertEqual(command, client_command(mode) + ["auth", "status"])
            self.assertIn("-p", command)
            self.assertNotIn("--help", command)
        self.assertEqual(client_command("compat-reject"),
                         client_command("analyze") + [compat.INVALID_OPTION, "auth", "status"])

    def test_boundary_errors_do_not_satisfy_negative_control(self):
        values = observations()
        values["compat-reject"] = (2, b"", b"CLIENT: REFUSED stage=boundary reason=sandbox\n")
        with self.assertRaises(AnalysisDenied) as raised:
            compat.check("claude", values.__getitem__)
        self.assertEqual((raised.exception.reason, raised.exception.stage), ("sandbox", "boundary"))

    def test_empty_profile_status_contract_is_not_an_authentication_attempt(self):
        for data, code in (({"loggedIn": True, "authMethod": "claude.ai", "configDirectory": "/home/client/profile"}, 0),
                           ({"loggedIn": False, "authMethod": "none", "configDirectory": "/home/other"}, 1),
                           ({"loggedIn": 0, "authMethod": "none", "configDirectory": "/home/client/profile"}, 1),
                           ({"loggedIn": False, "authMethod": "api_key", "configDirectory": "/home/client/profile"}, 1)):
            values = observations()
            values["compat-rpc"] = code, json.dumps(data).encode(), b""
            with self.subTest(data=data), self.assertRaisesRegex(AnalysisDenied, "client-protocol"):
                compat.check("claude", values.__getitem__)

    def test_protocol_proof_requires_real_booleans_exact_fields_and_unique_json_keys(self):
        for raw in (b'{}', b'[]', b'{"initialize":1,"signed_out":true,"models":true}',
                    b'{"initialize":true,"signed_out":true,"models":true,"extra":true}',
                    b'{"initialize":true,"initialize":true,"signed_out":true,"models":true}', b'not JSON'):
            values = observations("codex")
            values["compat-rpc"] = 0, raw, b"SECRET"
            with self.subTest(raw=raw), self.assertRaises(AnalysisDenied) as raised:
                compat.check("codex", values.__getitem__)
            self.assertEqual(raised.exception.reason, "client-protocol")
            self.assertNotIn("SECRET", str(raised.exception))

    def test_malformed_or_oversized_runner_output_is_closed(self):
        for value in (None, (0, b"ok"), (False, b"ok", b""), (0, "text", b""),
                      (0, b"x" * (compat.MAX_OUTPUT + 1), b"")):
            with self.subTest(value=type(value)), self.assertRaisesRegex(AnalysisDenied, "client-protocol"):
                compat.check("claude", lambda _: value)

    def test_timeout_cancel_and_changed_installation_propagate_without_retry(self):
        for reason in ("timeout", "cancelled", "client-changed", "output-limit"):
            calls = []
            def run(action):
                calls.append(action)
                raise AnalysisDenied(reason)
            with self.subTest(reason=reason), self.assertRaises(AnalysisDenied) as raised:
                compat.check("claude", run)
            self.assertEqual(raised.exception.reason, reason)
            self.assertEqual(calls, ["version"])

    def test_explicit_inspection_redacts_native_errors(self):
        for module, provider in (("ai_desktop.analysis_sandbox", "claude"), ("ai_desktop.codex_runtime", "codex")):
            with patch(module + ".probe_client", side_effect=OSError("SECRET/home/path")):
                result = compat.inspect(provider)
            self.assertEqual(result["status"], "client-unavailable")
            self.assertFalse(result["version_checked"])
            self.assertFalse(result["protocol_checked"])
            self.assertNotIn("SECRET", json.dumps(result))

    def test_invalid_provider_never_reaches_runner(self):
        with self.assertRaises(ValueError):
            compat.inspect("local")
        with self.assertRaises(AnalysisDenied):
            compat.check("invalid", lambda _: self.fail("runner called"))


if __name__ == "__main__":
    unittest.main()
