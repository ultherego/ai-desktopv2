"""The quota supervisor only carries disposable native auth files for the authenticated UID/slot."""
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_desktop import account_runtime, codex_quota_runtime as quota_runtime, codex_runtime, siwc_auth
from ai_desktop.analysis_contract import AnalysisDenied
from ai_desktop.e2_connect_proxy import CODEX_ACCOUNT_HOSTS, OPENAI_HOSTS, HOSTS, Denied, connect_host


NATIVE = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
          "tokens": {"id_token": "fixture-native-id", "access_token": "fixture-native-access",
                     "refresh_token": "fixture-native-refresh", "account_id": "fixture-native-workspace",
                     "login_owner_id": "fixture-native-owner",
                     "oauth_provenance": {"issuer": "https://auth.openai.com", "client_id": "fixture-native-client"}}}
READING = {"plan": "plus", "windows": {"five_hour": {"percent": 37, "reset": 1794000000}}}


class QuotaRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.home())))
        self.enterContext(patch.object(quota_runtime, "HOME", self.home))
        self.enterContext(patch.object(quota_runtime, "service_identity"))
        self.enterContext(patch.object(quota_runtime, "profile_lock", side_effect=nullcontext))
        self.enterContext(patch.object(codex_runtime, "runtime_ready"))
        self.version = self.enterContext(patch.object(quota_runtime, "_version"))
        self.native_exchange = quota_runtime._exchange
        self.exchange = self.enterContext(patch.object(quota_runtime, "_exchange", side_effect=self.exchange_ok))
        self.siwc = self.enroll("default", 1000)

    def enroll(self, alias, uid):
        with account_runtime.selected(uid, "codex", alias, allow_default=True):
            folder = account_runtime.directory(self.home / "codex", login=True)
            store = siwc_auth.AuthStore(folder)
            with store.locked(create=True) as (state, save):
                account = {"client_id": "oaiapp_fixture", "subject": "synthetic",
                           "email": "fixture@example.invalid", "id_token": "fake-siwc-id",
                           "access_token": "fake-siwc-access", "refresh_token": "fake-siwc-refresh",
                           "expires_at": 1900000000, "scopes": sorted(siwc_auth.PLAN_SCOPES)}
                key = siwc_auth._account_id(account["client_id"], account["subject"])
                state["accounts"][key] = account
                state["active"] = key
                save(state)
            account_runtime.connected(folder)
            return store

    def connect(self, alias="default", uid=1000):
        return quota_runtime.run("connect", uid, alias, challenge=lambda url, code: None)

    def exchange_ok(self, action, work, challenge):
        stage = work / "native-home"
        self.assertEqual([p.name for p in stage.iterdir()], [] if action == "connect" else ["auth.json"])
        if action == "connect":
            quota_runtime._write_json(stage / "auth.json", NATIVE)
            (stage / "config.toml").write_text("not a credential")
            return {"plan": "plus", "windows": {}}
        self.assertEqual(quota_runtime._read_json(stage / "auth.json"), NATIVE)
        return READING

    def test_scope_rotation_and_local_removal(self):
        self.assertFalse(quota_runtime.has_links(1000))
        self.assertEqual(self.connect()["plan"], "plus")
        self.assertTrue(quota_runtime.has_links(1000))
        self.assertEqual(quota_runtime.run("read", 1000, "default"), READING)
        self.assertFalse(quota_runtime.has_links(1001))
        with self.assertRaisesRegex(AnalysisDenied, "auth-required"):
            quota_runtime.run("read", 1001, "default")
        self.enroll("other", 1000)
        self.connect("other", 1000)
        with self.assertRaisesRegex(AnalysisDenied, "auth-required"):
            quota_runtime.run("read", 1001, "other")
        quota_runtime.run("disconnect", 1000, "default")
        self.assertTrue(quota_runtime.has_links(1000))
        self.assertEqual(quota_runtime.run("read", 1000, "other"), READING)
        quota_runtime.run("disconnect", 1000, "other")
        self.assertFalse(quota_runtime.has_links(1000))
        self.assertTrue(self.siwc.status()["accounts"][0]["connected"])
        quota_runtime.run("disconnect", 1001, "default")

    def test_failed_or_replaced_grant_does_not_publish_or_change_owner(self):
        self.connect()
        def fail_rotation(action, work, challenge):
            credential = quota_runtime._read_json(work / "native-home/auth.json")
            credential["tokens"]["account_id"] = "different"
            quota_runtime._write_json(work / "native-home/auth.json", credential)
            return READING
        self.exchange.side_effect = fail_rotation
        with self.assertRaisesRegex(AnalysisDenied, "identity"):
            quota_runtime.run("read", 1000, "default")
        self.exchange.side_effect = self.exchange_ok
        self.assertEqual(quota_runtime.run("read", 1000, "default"), READING)
        self.exchange.side_effect = AnalysisDenied("network")
        with self.assertRaisesRegex(AnalysisDenied, "network"):
            self.connect()
        self.exchange.side_effect = self.exchange_ok
        self.assertEqual(quota_runtime.run("read", 1000, "default"), READING)
        # A new SIWC login builds a fresh account entry. It must not revive the old grant.
        self.enroll("default", 1000)
        self.exchange.reset_mock()
        self.version.reset_mock()
        with self.assertRaisesRegex(AnalysisDenied, "auth-required"):
            quota_runtime.run("read", 1000, "default")
        self.exchange.assert_not_called()
        self.version.assert_not_called()
        self.connect()
        self.assertEqual(quota_runtime.run("read", 1000, "default"), READING)

    def test_provider_failure_after_same_principal_refresh_retains_rotation(self):
        self.connect()
        def rotate(action, work, challenge):
            target = work / "native-home/auth.json"
            credential = quota_runtime._read_json(target)
            credential["tokens"]["refresh_token"] = "new-synthetic-token"
            quota_runtime._write_json(target, credential)
            raise AnalysisDenied("network")
        self.exchange.side_effect = rotate
        with self.assertRaisesRegex(AnalysisDenied, "network"):
            quota_runtime.run("read", 1000, "default")
        saved = quota_runtime._read_json(self.home / "quota-codex-1000/default.json")
        self.assertEqual(saved["credential"]["tokens"]["refresh_token"], "new-synthetic-token")

    def test_cannot_load_other_credentials_or_files(self):
        self.connect()
        stored = self.home / "quota-codex-1000/default.json"
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b"config.toml", stored.read_bytes())
        for alias in ("../../default", "--foo", "", "root/a", "test\x1b"):
            with self.subTest(alias=alias), self.assertRaises(AnalysisDenied):
                quota_runtime.run("read", 1000, alias)
        stored.unlink()
        stored.symlink_to(self.siwc.directory / "accounts.json")
        self.exchange.reset_mock()
        with self.assertRaises(AnalysisDenied):
            quota_runtime.run("read", 1000, "default")
        self.exchange.assert_not_called()
        for data in ({"OPENAI_API_KEY": "sk-test"}, {**NATIVE, "auth_mode": "apikey"},
                     {**NATIVE, "config": "ignored?"}):
            with self.assertRaises(AnalysisDenied):
                quota_runtime._native_account(data)

    def test_nullable_workspace_uses_pinned_native_login_owner(self):
        no_workspace = {**NATIVE, "tokens": {**NATIVE["tokens"], "account_id": None}}
        self.assertIsNone(quota_runtime._native_account(no_workspace)[0])
        with self.assertRaises(AnalysisDenied):
            quota_runtime._native_account({**no_workspace, "tokens": {**no_workspace["tokens"], "login_owner_id": None}})

    def test_service_identity_guard_and_new_network_policy(self):
        with patch.object(quota_runtime, "service_identity", side_effect=AnalysisDenied("identity")):
            for action in ("read", "disconnect"):
                with self.assertRaisesRegex(AnalysisDenied, "identity"):
                    quota_runtime.run(action, 1000, "default")
        for name in ("chatgpt.com", "auth.openai.com"):
            header = f"CONNECT {name}:443 HTTP/1.1\r\nHost: {name}:443\r\n\r\n".encode()
            self.assertEqual(connect_host(header, hosts=CODEX_ACCOUNT_HOSTS), name)
            for other in (OPENAI_HOSTS, HOSTS):
                with self.assertRaises(Denied):
                    connect_host(header, hosts=other)
        header = b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com:443\r\n\r\n"
        with self.assertRaises(Denied):
            connect_host(header, hosts=CODEX_ACCOUNT_HOSTS)
        work = Path("/tmp/synthetic-host-work")
        for action in ("quota-connect", "quota-read"):
            cmd = codex_runtime.namespace_command(action, work)
            self.assertIn("--unshare-net", cmd)
            self.assertIn("--disable-userns", cmd)
            self.assertIn("--clearenv", cmd)
            self.assertIn(str(work / "native-home"), cmd)
            self.assertNotIn(str(codex_runtime.PROFILE), cmd)
            self.assertNotIn("ACCESS_TOKEN", cmd)
            self.assertNotIn(str(Path.home()), cmd)

    def test_untrusted_result_and_terminal_challenge(self):
        with self.assertRaisesRegex(AnalysisDenied, "result"):
            quota_runtime._safe_result({"windows": {}, "plan": "plus", "email": "private"})
        for frame in (b'{"url":"https://example.invalid","code":"SAFE-CODE"}\n',
                      b'{"url":"https://auth.openai.com/codex/device","code":"\\u001bX"}\n'):
            def attempt(*args, stdout_chunk, **kwargs):
                stdout_chunk(frame)
                return 0, frame, b""
            with patch.object(quota_runtime, "_AccountProxy", return_value=nullcontext(Mock())), \
                    patch.object(quota_runtime, "bounded_process", side_effect=attempt), self.assertRaises(AnalysisDenied):
                self.native_exchange("connect", self.home, lambda *args: self.fail("unsafe challenge"))
