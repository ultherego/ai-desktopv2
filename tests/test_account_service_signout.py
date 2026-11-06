"""Explicit account maintenance with synthetic profiles and a mocked service identity."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import account_runtime, analysis_sandbox, codex_admin, siwc_auth
from ai_desktop.analysis_contract import AnalysisDenied
from system_client_fixtures import FakeBinding
from claude_client_fixtures import compatibility_results


class ServiceSignoutTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(codex_admin, "HOME", self.home))
        self.enterContext(patch.object(analysis_sandbox, "HOME", self.home))
        self.enterContext(patch.object(analysis_sandbox, "service_identity"))
        self.enterContext(patch("ai_desktop.e2_socket_activation._worker_identity", return_value=True))
        self.enterContext(patch.object(codex_admin.os, "isatty", return_value=True))
        self.enterContext(patch.object(siwc_auth.AuthClient, "_request", side_effect=AssertionError("no network")))
        self.output = self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(analysis_sandbox, "runtime_ready"))
        self.enterContext(patch.object(analysis_sandbox.client_binding, "bind", side_effect=lambda _: FakeBinding("claude")))
        self.native = self.enterContext(patch.object(analysis_sandbox, "bounded_process", side_effect=self.native_run))

    def native_run(self, command, *, env, timeout, limit, pass_fds, cancelled=None):
        self.assertEqual(env, {"PATH": "/usr/bin", "LANG": "C.UTF-8"})
        self.assertIn("--unshare-net", command)
        self.assertNotIn("/run/egress.sock", command)
        self.assertEqual(pass_fds, ())
        action = command[-2]
        probes = ("version", "compat-options", "compat-web", "compat-reject", "compat-rpc")
        if action in probes:
            self.assertNotIn("--bind", command)
            self.assertFalse(any(command[i + 2] == analysis_sandbox.INNER_PROFILE
                                 for i, value in enumerate(command[:-2]) if value in ("--bind", "--ro-bind")))
            return compatibility_results()[probes.index(action)]
        mounts = [(command[i], command[i + 1]) for i in range(len(command) - 2)
                  if command[i] in ("--ro-bind", "--bind") and command[i + 2] == analysis_sandbox.INNER_PROFILE]
        self.assertEqual(len(mounts), 1)
        flag, path = mounts[0]
        # This stands in for the bound system executable inside the private profile mount.
        entry = Path(path) / ".credentials.json"
        if action == "offline-logout":
            self.assertEqual(flag, "--bind")
            entry.unlink()
            return 0, b"Signed out", b""
        self.assertEqual((action, flag), ("offline-status", "--ro-bind"))
        if entry.is_file() and entry.read_bytes() == b"synthetic credential sentinel":
            return 0, b'{"loggedIn":true,"authMethod":"claude.ai","configDirectory":"/home/client/profile"}', b""
        return 1, b'{"loggedIn":false}', b""


    def action(self, op, provider, name="default", uid="1000"):
        return codex_admin.named_worker(op, uid, provider, name)

    @staticmethod
    def put_secret(path):
        path.mkdir(mode=0o700, exist_ok=True)
        creds = path / ".credentials.json"
        creds.write_bytes(b"synthetic credential sentinel")
        creds.chmod(0o600)
        return creds

    @staticmethod
    def codex_record(path):
        store = siwc_auth.AuthStore(path)
        account_id = siwc_auth._account_id("oaiapp_test", "subject")
        with store.locked(create=True) as (data, save):
            data["active"] = account_id
            data["accounts"][account_id] = {
                "client_id": "oaiapp_test", "subject": "subject", "email": "",
                "id_token": "", "access_token": "ACCESS_FIXTURE", "refresh_token": "REFRESH_FIXTURE",
                "expires_at": 10000, "scopes": sorted(siwc_auth.PLAN_SCOPES),
            }
            save(data)
        return store, account_id

    def test_default_claude_check_no_content_read_and_logout_preserves_config(self):
        profile = self.home / "claude"
        self.assertEqual(self.action("check", "claude"), 4)
        self.assertFalse(profile.exists())
        credential = self.put_secret(profile)
        config = profile / "settings.json"
        config.write_text("keep")
        with patch.object(os, "read", side_effect=AssertionError("do not inspect Claude secrets")):
            self.assertEqual(self.action("check", "claude"), 0)
            self.assertEqual(self.action("logout", "claude"), 0)
            self.assertEqual(self.action("logout", "claude"), 0)
        self.assertFalse(credential.exists())
        self.assertEqual(config.read_text(), "keep")
        self.assertEqual(self.action("check", "claude"), 4)
        self.assertNotIn("sentinel", self.output.getvalue())

    def test_refuses_link_or_permission_changes_and_busy_profile_without_deletion(self):
        profile = self.home / "claude"
        target = self.put_secret(self.home)
        profile.mkdir(mode=0o700)
        entry = profile / ".credentials.json"
        entry.symlink_to(target)
        for op in ("check", "logout"):
            self.assertEqual(self.action(op, "claude"), 2)
        self.assertTrue(entry.is_symlink())
        entry.unlink()
        os.link(target, entry)
        self.assertEqual(self.action("logout", "claude"), 2)
        entry.unlink()
        self.put_secret(profile)
        entry.chmod(0o644)
        self.assertEqual(self.action("logout", "claude"), 2)
        entry.chmod(0o600)
        with analysis_sandbox.profile_lock():
            self.assertEqual(self.action("logout", "claude"), 2)
        self.assertTrue(entry.exists())
        self.assertTrue(target.exists())

    def test_claude_uses_verified_native_state_and_checks_after_logout(self):
        profile = self.home / "claude"
        entry = self.put_secret(profile)
        entry.write_text('{"unrelated":"keep"}')
        self.assertEqual(self.action("check", "claude"), 4)
        self.assertEqual(self.action("logout", "claude"), 0)
        self.assertEqual(entry.read_text(), '{"unrelated":"keep"}')
        good = 0, b'{"loggedIn":true,"authMethod":"claude.ai","configDirectory":"/home/client/profile"}', b""
        for reported in ((1, b"", b""), (1, b"{}", b""),
                         (0, b'{"loggedIn":true,"authMethod":"api-key"}', b""),
                         (0, b'{"loggedIn":true,"authMethod":"claude.ai","configDirectory":"/other"}', b"")):
            self.native.side_effect = compatibility_results() + [reported]
            self.assertEqual(self.action("check", "claude"), 2)
        self.native.side_effect = compatibility_results() + [good, (0, b"success", b""), good]
        self.assertEqual(self.action("logout", "claude"), 2)
        self.native.side_effect = compatibility_results() + [good, (1, b"", b"partial failure"), (1, b'{"loggedIn":false}', b"")]
        self.assertEqual(self.action("logout", "claude"), 0)
        self.native.side_effect = ((0, b"wrong version", b""),)
        self.assertEqual(self.action("check", "claude"), 2)

    def test_named_claude_signout_fences_old_recipient_without_retiring_other_profiles(self):
        legacy = self.home / "claude"
        keep = self.put_secret(legacy)
        with account_runtime.selected(1000, "claude", "work"):
            profile = account_runtime.directory(legacy, login=True)
            removed = self.put_secret(profile)
            self.assertEqual(self.action("check", "claude", "work"), 4)  # interrupted initial login
            account_runtime.connected(profile)
        self.assertEqual(self.action("check", "claude", "work", uid="1001"), 4)
        self.assertEqual(self.action("check", "claude", "work"), 0)
        self.assertEqual(self.action("logout", "claude", "work"), 6)
        self.assertEqual(self.action("logout", "claude", "work"), 6)
        self.assertEqual(self.action("check", "claude", "work"), 6)
        self.assertTrue(keep.exists())
        self.assertFalse(removed.exists())
        with account_runtime.selected(1000, "claude", "work"):
            for login in (True, False):
                with self.assertRaises(AnalysisDenied):
                    account_runtime.directory(legacy, login=login)
        self.assertEqual(self.action("retire", "claude", "default"), 2)
        self.assertEqual(self.action("logout", "claude", "../work"), 2)
        self.assertEqual(self.action("logout", "claude", "default", uid="0"), 2)

    def test_codex_checks_exact_active_slot_and_preserves_identity_on_revocation_failure(self):
        legacy = self.home / "codex"
        default_store, _ = self.codex_record(legacy)
        with account_runtime.selected(1000, "codex", "work"):
            path = account_runtime.directory(legacy, login=True)
            store, original = self.codex_record(path)
            account_runtime.connected(path)
        self.assertEqual(self.action("check", "codex", "work"), 0)
        with patch.object(siwc_auth.AuthClient, "_discovery", side_effect=siwc_auth.AuthError("network")):
            self.assertEqual(self.action("logout", "codex", "work"), 5)
        self.assertEqual(self.action("check", "codex", "work"), 4)
        self.assertEqual(self.action("logout", "codex", "work"), 0)
        self.assertTrue(default_store.status()["accounts"][0]["connected"])
        self.assertEqual(store.status()["active"], original)
        self.assertFalse(store.status()["accounts"][0]["connected"])
        for word in ("subject", "ACCESS_FIXTURE", "REFRESH_FIXTURE", original):
            self.assertNotIn(word, self.output.getvalue())
        with account_runtime.selected(1000, "codex", "work"):
            with self.assertRaises(AnalysisDenied):
                account_runtime.directory(legacy)
            # Stop before a browser/listener or real authentication, verify the immutable registration.
            with patch.object(codex_admin.http.server, "HTTPServer") as server, \
                    patch("ai_desktop.codex_runtime.probe_client"), \
                    patch.object(siwc_auth.AuthClient, "begin_login", side_effect=siwc_auth.AuthError("denied")) as login:
                server.return_value.__enter__.return_value.server_port = 1455
                self.assertEqual(codex_admin.auth_worker("login"), 2)
                self.assertEqual(login.call_args.kwargs, {"account_id": original})
            (path / "accounts.json").unlink()
            with patch.object(codex_admin.http.server, "HTTPServer", side_effect=AssertionError("no new grant")), \
                    patch("ai_desktop.codex_runtime.probe_client"):
                self.assertEqual(codex_admin.auth_worker("login"), 2)
        self.assertEqual(self.action("check", "codex", "work"), 6)
        self.assertEqual(self.action("logout", "codex", "work"), 6)

    def test_default_codex_and_named_retire_never_cross(self):
        legacy = self.home / "codex"
        store, _ = self.codex_record(legacy)
        self.assertEqual(self.action("check", "codex"), 0)
        with account_runtime.selected(1000, "codex", "retired"):
            account_runtime.retire(legacy)
        self.assertEqual(self.action("check", "codex", "retired"), 2)
        self.assertEqual(self.action("logout", "codex", "retired"), 2)
        with patch.object(siwc_auth.AuthClient, "_discovery", side_effect=siwc_auth.AuthError("network")):
            self.assertEqual(codex_admin.auth_worker("logout"), 5)
        self.assertEqual(self.action("check", "codex"), 4)
        self.assertFalse(store.status()["accounts"][0]["connected"])
