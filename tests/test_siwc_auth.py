"""Real private-file and signed-token tests; no OpenAI account or network."""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ai_desktop import siwc_auth as auth


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class FakeTransport:
    def __init__(self, key):
        numbers = key.public_key().public_numbers()
        self.jwk = {"kty": "RSA", "alg": "RS256", "kid": "known", "use": "sig",
                    "n": b64(numbers.n.to_bytes(256, "big")), "e": b64(numbers.e.to_bytes(3, "big"))}
        self.discovery = {"issuer": auth.ISSUER, "authorization_endpoint": auth.AUTHORIZE,
                          "token_endpoint": auth.TOKEN, "jwks_uri": auth.JWKS,
                          "revocation_endpoint": auth.ISSUER + "/api/accounts/oauth/revoke"}
        self.tokens, self.requests, self.fail_token = {}, [], False
        self.fail_revoke = False

    def __call__(self, method, url, body, headers):
        self.requests.append((method, url, parse_qs(body.decode()) if body else None))
        if url == auth.DISCOVERY:
            result = self.discovery
        elif url == auth.JWKS:
            result = {"keys": [self.jwk]}
        elif url == auth.TOKEN:
            if self.fail_token:
                return 401, b'{"error":"SECRET_NATIVE_ERROR"}'
            result = self.tokens
        elif url.endswith("/revoke"):
            return (503 if self.fail_revoke else 200), b""
        else:
            raise AssertionError("unexpected endpoint")
        return 200, json.dumps(result).encode()


class SignInTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "codex"
        self.store = auth.AuthStore(self.directory)
        self.transport = FakeTransport(self.key)
        self.now = 1000000.0
        self.client = auth.AuthClient(self.store, transport=self.transport, clock=lambda: self.now)

    def token(self, pending, **changes):
        claims = {"iss": auth.ISSUER, "aud": ["oaiapp_test"], "iat": self.now,
                  "exp": self.now + 3600, "sub": "subject-A", "email": "user@example.test",
                  "nonce": pending.nonce}
        claims.update(changes)
        header = b64(json.dumps({"alg": "RS256", "kid": "known"}).encode())
        payload = b64(json.dumps(claims).encode())
        signature = self.key.sign((header + "." + payload).encode(), padding.PKCS1v15(), hashes.SHA256())
        return header + "." + payload + "." + b64(signature)

    def pending(self, **kwargs):
        return self.client.begin_login("http://127.0.0.1:1455/auth/callback", **kwargs)

    def response(self, pending, **claims):
        self.transport.tokens = {"token_type": "Bearer", "access_token": "ACCESS_SECRET",
                                 "refresh_token": "REFRESH_SECRET", "id_token": self.token(pending, **claims),
                                 "expires_in": 3600, "scope": " ".join(sorted(auth.SCOPES))}

    def callback(self, pending, **changes):
        values = {"state": pending.state, "code": "CODE_SECRET", "client_id": "oaiapp_test"}
        values.update(changes)
        return urlencode(values)

    def sign_in(self):
        pending = self.pending()
        self.response(pending)
        return self.client.finish_login(pending, self.callback(pending))

    def test_status_missing_does_not_create_anything(self):
        self.assertEqual(self.store.status(), {"active": None, "accounts": []})
        self.assertFalse(self.directory.exists())

    def test_native_quota_binding_lasts_one_explicit_login_and_stays_private(self):
        self.sign_in()
        with self.assertRaises(auth.AuthError):
            self.store.quota_binding()
        first = self.store.quota_binding(enroll=True)
        self.assertEqual(self.store.quota_binding(), first)
        self.assertNotIn(first, repr(self.store.status()))
        self.assertTrue(self.client.logout())
        with self.assertRaises(auth.AuthError):
            self.store.quota_binding(enroll=True)
        self.sign_in()
        with self.assertRaises(auth.AuthError):
            self.store.quota_binding()
        self.assertNotEqual(self.store.quota_binding(enroll=True), first)

    def test_registration_has_pkce_nonce_state_scopes_and_stable_host(self):
        first, second = self.pending(), self.pending()
        one = parse_qs(first.authorization_url.split("?", 1)[1])
        two = parse_qs(second.authorization_url.split("?", 1)[1])
        self.assertEqual(one["client_id"], ["dynamic_agent_client"])
        self.assertEqual(one["agent_name_hint"], ["AI Desktop"])
        self.assertEqual(one["ext_agent_host_id"], two["ext_agent_host_id"])
        self.assertNotEqual(one["state"], two["state"])
        self.assertNotEqual(one["nonce"], two["nonce"])
        self.assertEqual(one["code_challenge"], [b64(hashlib.sha256(first.verifier.encode()).digest())])
        self.assertEqual(set(one["scope"][0].split()), auth.SCOPES)
        self.assertNotIn(first.verifier, repr(first))
        self.assertNotIn(first.state, repr(first))

    def test_exact_loopback_callback_required(self):
        for uri in ("http://localhost:1455/auth/callback", "http://127.0.0.1:1455/callback",
                    "https://127.0.0.1:1455/auth/callback", "http://user@127.0.0.1:1455/auth/callback",
                    "http://127.0.0.1:1455/auth/callback?x=y", "http://127.0.0.1:80/auth/callback"):
            with self.subTest(uri=uri), self.assertRaises(auth.AuthError):
                self.client.begin_login(uri)

    def test_success_stores_verified_registration_and_returns_no_tokens(self):
        result = self.sign_in()
        self.assertTrue(result["plan_usage"])
        self.assertEqual(result["email"], "user@example.test")
        self.assertNotIn("SECRET", repr(result))
        self.assertEqual(str(self.client.access_token()), "ACCESS_SECRET")
        self.assertNotIn("ACCESS_SECRET", repr(self.client.access_token()))
        self.assertEqual((self.directory / "accounts.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        exchange = next(r for r in self.transport.requests if r[1] == auth.TOKEN)[2]
        self.assertEqual(exchange["client_id"], ["oaiapp_test"])
        self.assertEqual(exchange["resource"], [auth.RESOURCE])
        self.assertNotIn("client_secret", exchange)

    def test_callback_one_use_and_wrong_state_performs_no_network(self):
        pending = self.pending()
        self.response(pending)
        with self.assertRaises(auth.AuthError) as error:
            self.client.finish_login(pending, self.callback(pending, state="WRONG_SECRET"))
        self.assertEqual(error.exception.reason, "state")
        self.assertEqual(self.transport.requests, [])
        self.assertFalse(pending.used)
        self.assertTrue(self.client.finish_login(pending, self.callback(pending))["connected"])
        with self.assertRaises(auth.AuthError):
            self.client.finish_login(pending, self.callback(pending))

    def test_expired_pending_and_denial_do_not_exchange(self):
        pending = self.pending()
        self.now += 601
        with self.assertRaises(auth.AuthError) as error:
            self.client.finish_login(pending, self.callback(pending))
        self.assertEqual(error.exception.reason, "expired")
        pending = self.pending()
        with self.assertRaises(auth.AuthError) as error:
            self.client.finish_login(pending, urlencode({"state": pending.state, "error": "access_denied"}))
        self.assertEqual(error.exception.reason, "denied")
        self.assertEqual(self.transport.requests, [])

    def test_duplicate_fields_and_missing_issued_client_id_refused(self):
        pending = self.pending()
        with self.assertRaises(auth.AuthError):
            self.client.finish_login(pending, self.callback(pending) + "&state=other")
        pending = self.pending()
        with self.assertRaises(auth.AuthError):
            self.client.finish_login(pending, urlencode({"state": pending.state, "code": "CODE_SECRET"}))
        self.assertEqual(self.transport.requests, [])

    def test_issuer_audience_nonce_subject_expiry_iat_checked_after_signature(self):
        for change in ({"iss": "https://attacker.test"}, {"aud": ["oaiapp_other"]},
                       {"nonce": "WRONG_NONCE"}, {"sub": ""}, {"exp": self.now - 6},
                       {"iat": self.now + 6}, {"nbf": self.now + 6},
                       {"aud": ["oaiapp_test", "other"], "azp": "other"}):
            with self.subTest(change=change):
                pending = self.pending()
                self.response(pending, **change)
                with self.assertRaises(auth.AuthError):
                    self.client.finish_login(pending, self.callback(pending))
        self.assertEqual(self.store.status()["accounts"], [])

    def test_bad_signature_unknown_key_and_algorithm_none_refused(self):
        pending = self.pending()
        self.response(pending)
        token = self.transport.tokens["id_token"]
        self.transport.tokens["id_token"] = token[:-5] + "XXXXX"
        with self.assertRaises(auth.AuthError):
            self.client.finish_login(pending, self.callback(pending))
        pending = self.pending()
        self.response(pending)
        self.transport.jwk["kid"] = "other"
        with self.assertRaises(auth.AuthError):
            self.client.finish_login(pending, self.callback(pending))
        pending = self.pending()
        self.response(pending)
        self.transport.tokens["id_token"] = b64(b'{"alg":"none","kid":"known"}') + "." + token.split(".")[1] + ".XXX"
        with self.assertRaises(auth.AuthError):
            self.client.finish_login(pending, self.callback(pending))

    def test_access_token_hash_is_checked_if_present(self):
        pending = self.pending()
        self.response(pending, at_hash="incorrect")
        with self.assertRaises(auth.AuthError):
            self.client.finish_login(pending, self.callback(pending))
        pending = self.pending()
        self.response(pending, at_hash=b64(hashlib.sha256(b"ACCESS_SECRET").digest()[:16]))
        self.assertTrue(self.client.finish_login(pending, self.callback(pending))["connected"])

    def test_identity_does_not_authorize_missing_plan_scope(self):
        pending = self.pending()
        self.response(pending)
        self.transport.tokens["scope"] = "openid profile email offline_access resource.invoke"
        with self.assertRaises(auth.AuthError) as error:
            self.client.finish_login(pending, self.callback(pending))
        self.assertEqual(error.exception.reason, "scope")
        self.assertEqual(self.store.status()["accounts"], [])

    def test_returning_login_uses_issued_id_and_rejects_swapped_registration(self):
        result = self.sign_in()
        pending = self.pending()
        query = parse_qs(pending.authorization_url.split("?", 1)[1])
        self.assertEqual(query["client_id"], ["oaiapp_test"])
        self.assertNotIn("agent_name_hint", query)
        self.assertNotIn("id_token_hint", query)
        self.response(pending)
        with self.assertRaises(auth.AuthError):
            self.client.finish_login(pending, self.callback(pending, client_id="oaiapp_other"))
        pending = self.pending()
        self.response(pending, sub="other-account")
        with self.assertRaises(auth.AuthError):
            self.client.finish_login(pending, self.callback(pending))
        self.assertEqual(self.store.status()["active"], result["id"])

    def test_concurrent_returning_login_refuses_overwrite_of_rotated_record(self):
        self.sign_in()
        pending = self.pending()
        self.response(pending)
        with self.store.locked() as (state, save):
            state["accounts"][state["active"]]["refresh_token"] = "NEWER_REFRESH"
            save(state)
        with self.assertRaises(auth.AuthError) as error:
            self.client.finish_login(pending, self.callback(pending))
        self.assertEqual(error.exception.reason, "conflict")

    def test_refresh_is_serialized_and_rotates_tokens_before_return(self):
        self.sign_in()
        self.now += 3550
        self.transport.tokens = {"token_type": "Bearer", "access_token": "ROTATED_ACCESS",
                                 "refresh_token": "ROTATED_REFRESH", "expires_in": 3600}
        self.assertEqual(str(self.client.access_token()), "ROTATED_ACCESS")
        with self.store.locked() as (state, _):
            account = state["accounts"][state["active"]]
            self.assertEqual(account["refresh_token"], "ROTATED_REFRESH")
            self.assertTrue(auth.PLAN_SCOPES.issubset(account["scopes"]))
        exchanges = [r[2] for r in self.transport.requests if r[1] == auth.TOKEN]
        self.assertEqual(exchanges[-1]["refresh_token"], ["REFRESH_SECRET"])
        self.assertNotIn("scope", exchanges[-1])
        count = len(exchanges)
        self.client.access_token()
        self.assertEqual(len([r for r in self.transport.requests if r[1] == auth.TOKEN]), count)
        with self.store.locked():
            with self.assertRaises(auth.AuthError) as error:
                self.client.access_token()
            self.assertEqual(error.exception.reason, "busy")

    def test_failed_refresh_does_not_replace_or_leak_tokens(self):
        self.sign_in()
        before = (self.directory / "accounts.json").read_bytes()
        self.now += 3550
        self.transport.fail_token = True
        with self.assertRaises(auth.AuthError) as error:
            self.client.access_token()
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual((self.directory / "accounts.json").read_bytes(), before)

    def test_failed_exchange_can_start_fresh_authorization_with_issued_id(self):
        pending = self.pending()
        self.response(pending)
        self.transport.fail_token = True
        with self.assertRaises(auth.AuthError):
            self.client.finish_login(pending, self.callback(pending))
        self.assertEqual(self.store.status()["accounts"], [])
        fresh = self.client.restart_login(pending, "http://127.0.0.1:2455/auth/callback")
        query = parse_qs(fresh.authorization_url.split("?", 1)[1])
        self.assertEqual(query["client_id"], ["oaiapp_test"])
        self.assertNotIn("agent_name_hint", query)
        self.assertNotEqual(fresh.state, pending.state)
        self.transport.fail_token = False
        self.response(fresh)
        self.assertTrue(self.client.finish_login(fresh, self.callback(fresh))["connected"])

    def test_credentials_must_be_nonempty_visible_ascii(self):
        for value in ("", "injected\nheader", "token with spaces", "nieascii-ą"):
            pending = self.pending()
            self.response(pending)
            self.transport.tokens["access_token"] = value
            with self.subTest(value=value), self.assertRaises(auth.AuthError):
                self.client.finish_login(pending, self.callback(pending))

    def test_failed_rotation_save_does_not_expose_new_access_token(self):
        self.sign_in()
        self.now += 3550
        self.transport.tokens = {"token_type": "Bearer", "access_token": "ROTATED_ACCESS",
                                 "refresh_token": "ROTATED_REFRESH", "expires_in": 3600}
        with patch.object(self.store, "_write", side_effect=auth.AuthError("storage")):
            with self.assertRaises(auth.AuthError):
                self.client.access_token()

    def test_logout_revokes_and_clears_only_tokens_preserving_registration(self):
        account = self.sign_in()
        self.assertTrue(self.client.logout())
        status = self.store.status()
        self.assertEqual(status["active"], account["id"])
        self.assertFalse(status["accounts"][0]["connected"])
        with self.assertRaises(auth.AuthError) as error:
            self.client.access_token()
        self.assertEqual(error.exception.reason, "auth-required")
        self.assertEqual(parse_qs(self.pending().authorization_url.split("?", 1)[1])["client_id"], ["oaiapp_test"])

    def test_failed_remote_revocation_still_ends_local_session(self):
        self.sign_in()
        self.transport.fail_revoke = True
        self.assertFalse(self.client.logout())
        with self.assertRaises(auth.AuthError):
            self.client.access_token()

    def test_untrusted_metadata_does_not_redirect_tokens(self):
        self.sign_in()
        self.transport.discovery["revocation_endpoint"] = "https://attacker.test/steal"
        self.assertFalse(self.client.logout())
        self.assertFalse(any("attacker" in r[1] for r in self.transport.requests))

    def test_accounts_remain_separate_and_explicit_selection(self):
        first = self.sign_in()
        pending = self.pending(new_account=True)
        self.response(pending, sub="subject-B", aud=["oaiapp_second"])
        second = self.client.finish_login(pending, self.callback(pending, client_id="oaiapp_second"))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.status()["accounts"]), 2)
        self.assertEqual(self.client.select_account(first["id"])["id"], first["id"])
        with self.assertRaises(auth.AuthError):
            self.client.select_account("unknown")

    def test_unsafe_storage_symlink_hardlink_modes_and_overflow_refused(self):
        self.sign_in()
        path = self.directory / "accounts.json"
        original = path.read_bytes()
        path.chmod(0o644)
        with self.assertRaises(auth.AuthError):
            self.store.status()
        path.chmod(0o600)
        other = self.directory / "other.json"
        os.link(path, other)
        with self.assertRaises(auth.AuthError):
            self.store.status()
        other.unlink()
        path.unlink()
        other.write_bytes(original)
        path.symlink_to(other)
        with self.assertRaises(auth.AuthError):
            self.store.status()
        path.unlink()
        path.write_bytes(b"x" * (auth.MAX_STORE_BYTES + 1))
        path.chmod(0o600)
        with self.assertRaises(auth.AuthError):
            self.store.status()

    def test_directory_and_lock_symlinks_refused(self):
        self.directory.symlink_to(self.temp.name, target_is_directory=True)
        with self.assertRaises(auth.AuthError):
            self.pending()
        self.directory.unlink()
        self.directory.mkdir(mode=0o700)
        (self.directory / "accounts.lock").symlink_to(Path(self.temp.name) / "outside")
        with self.assertRaises(auth.AuthError):
            self.pending()

    def test_response_duplicate_fields_and_unbounded_http_fail_closed(self):
        for raw in (b'{"x":1,"x":2}', b"{}" * auth.MAX_HTTP_BYTES, b'{"x":NaN}'):
            with self.subTest(size=len(raw)), self.assertRaises(auth.AuthError):
                auth._json(raw)

    def test_incomplete_http_body_exception_cannot_expose_credential_bytes(self):
        pending = self.pending()
        self.response(pending)
        def incomplete(*_args):
            raise http.client.IncompleteRead(b"ACCESS_SECRET", 10)
        self.client.transport = incomplete
        with self.assertRaises(auth.AuthError) as error:
            self.client.finish_login(pending, self.callback(pending))
        self.assertEqual(error.exception.reason, "network")
        self.assertNotIn("SECRET", str(error.exception))


if __name__ == "__main__":
    unittest.main()
