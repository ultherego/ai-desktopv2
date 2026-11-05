"""Local Sign in with ChatGPT credentials for a trusted auth worker.

This module is never entered by model output. It does not start a browser or a
listener, inherit an existing Codex login, or make a request on import. The
caller owns the loopback listener and the separate service-UID boundary.

Protocol: developers.openai.com/siwc/token-sharing-open-source/sign-in and
profiles-and-sessions (reviewed 2026-11-02). Unsupported signing algorithms
fail closed; RS256 signatures use python-cryptography, imported lazily.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import base64
import fcntl
import hashlib
import hmac
import http.client
import json
import math
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import time
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit
import uuid

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
JWKS = ISSUER + "/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
CALLBACK_PATH = "/auth/callback"
APP_NAME = "AI Desktop"
SCOPES = frozenset({"openid", "profile", "email", "offline_access", "resource.invoke", "chatgpt.tokens.use.direct"})
PLAN_SCOPES = frozenset({"resource.invoke", "chatgpt.tokens.use.direct"})
MAX_HTTP_BYTES = 128 * 1024
MAX_STORE_BYTES = 256 * 1024
MAX_ACCOUNTS = 8
_CLIENT_ID = re.compile(r"oaiapp_[A-Za-z0-9_-]{1,128}\Z")
_B64 = re.compile(r"[A-Za-z0-9_-]+\Z")
_REASONS = frozenset({"storage", "conflict", "busy", "network", "response", "identity", "state", "expired", "denied", "scope", "auth-required", "dependency", "input"})


class AuthError(ValueError):
    def __init__(self, reason):
        self.reason = reason if reason in _REASONS else "response"
        super().__init__("ChatGPT auth: " + self.reason)


class Secret(str):
    """Strings usable in a child environment, redacted in incidental repr()."""
    def __repr__(self):
        return "<redacted>"


@contextmanager
def _closed(reason):
    try:
        yield
    except AuthError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError, http.client.HTTPException):
        raise AuthError(reason) from None


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise AuthError("response")
            result[key] = value
        return result
    with _closed("response"):
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_HTTP_BYTES:
            raise AuthError("response")
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(AuthError("response")))
        if type(result) is not dict:
            raise AuthError("response")
        return result


def _text(value, limit=16384, *, empty=False):
    if (type(value) is not str or (not value and not empty)
            or len(value.encode("utf-8")) > limit
            or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value)):
        raise AuthError("response")
    return value


def _credential(value):
    value = _text(value)
    if any(not 33 <= ord(character) <= 126 for character in value):
        raise AuthError("response")
    return value


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text):
    if type(text) is not str or len(text) > 65536 or not _B64.fullmatch(text):
        raise AuthError("identity")
    with _closed("identity"):
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _metadata_url(value):
    _text(value, 512)
    part = urlsplit(value)
    if (part.scheme != "https" or part.netloc != "auth.openai.com" or not part.path.startswith("/")
            or part.query or part.fragment or part.username is not None or part.password is not None):
        raise AuthError("response")
    return value


def https_request(method, url, body=None, headers=None):
    """Fixed-origin bounded HTTPS; never follows redirects or inherited proxies.

    A deployment may inject the trusted worker's provider-specific transport.
    No token or HTTP body is retained in an exception or diagnostic.
    """
    with _closed("network"):
        _metadata_url(url)
        if method not in ("GET", "POST") or body is not None and (type(body) is not bytes or len(body) > MAX_HTTP_BYTES):
            raise AuthError("input")
        connection = http.client.HTTPSConnection("auth.openai.com", timeout=10, context=ssl.create_default_context())
        try:
            connection.request(method, urlsplit(url).path, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read(MAX_HTTP_BYTES + 1)
            if len(raw) > MAX_HTTP_BYTES:
                raise AuthError("response")
            return response.status, raw
        finally:
            connection.close()


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class AuthStore:
    """Descriptor-based, private, atomic credential rotation under one lock.

    `directory` is supplied by trusted installed code, never by a request.
    Hostile processes of the same UID remain outside the A+ guarantee.
    """
    def __init__(self, directory, uid=None):
        self.directory = Path(directory)
        self.uid = os.geteuid() if uid is None else uid
        if (not self.directory.is_absolute() or ".." in self.directory.parts
                or type(self.uid) is not int or self.uid != os.geteuid()):
            raise AuthError("storage")

    def _directory(self, create=False):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open("/", flags)
        try:
            for index, component in enumerate(self.directory.parts[1:]):
                last = index == len(self.directory.parts) - 2
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not (create and last):
                        raise
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                info = os.fstat(child)
                sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                if (info.st_uid not in {0, self.uid}
                        or info.st_mode & 0o022 and not sticky_root
                        or last and (info.st_uid != self.uid or stat.S_IMODE(info.st_mode) != 0o700)):
                    os.close(child)
                    raise AuthError("storage")
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _read(self, directory):
        try:
            descriptor = os.open("accounts.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None, None
        try:
            before = os.fstat(descriptor)
            self._secure_file(before)
            raw = bytearray()
            while len(raw) <= MAX_STORE_BYTES:
                part = os.read(descriptor, min(16384, MAX_STORE_BYTES + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            if len(raw) > MAX_STORE_BYTES or _file_identity(before) != _file_identity(os.fstat(descriptor)):
                raise AuthError("storage")
            with _closed("storage"):
                value = json.loads(bytes(raw), object_pairs_hook=_unique_pairs)
            self._validate(value)
            return value, _file_identity(before)
        finally:
            os.close(descriptor)

    def _secure_file(self, info):
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != self.uid
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise AuthError("storage")

    def _validate(self, value):
        if (type(value) is not dict or set(value) != {"schema", "host_id", "active", "accounts"}
                or type(value["schema"]) is not int or value["schema"] != 1
                or type(value["accounts"]) is not dict or len(value["accounts"]) > MAX_ACCOUNTS):
            raise AuthError("storage")
        try:
            if str(uuid.UUID(value["host_id"][9:])) != value["host_id"][9:] or not value["host_id"].startswith("urn:uuid:"):
                raise AuthError("storage")
        except (ValueError, TypeError, KeyError, AttributeError):
            raise AuthError("storage") from None
        if value["active"] is not None and value["active"] not in value["accounts"]:
            raise AuthError("storage")
        for account_id, account in value["accounts"].items():
            _validate_account(account)
            if account_id != _account_id(account["client_id"], account["subject"]):
                raise AuthError("storage")

    def _write(self, directory, value, expected):
        self._validate(value)
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
        if len(raw) > MAX_STORE_BYTES:
            raise AuthError("storage")
        current, identity = self._read(directory)
        if identity != expected:
            raise AuthError("conflict")
        name = ".accounts-" + secrets.token_hex(16)
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        try:
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise AuthError("storage")
                offset += written
            os.fsync(descriptor)
            _, identity = self._read(directory)
            if identity != expected:
                raise AuthError("conflict")
            if expected is None:
                # A new name must never replace a concurrent creation.
                os.link(name, "accounts.json", src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                os.unlink(name, dir_fd=directory)
            else:
                os.replace(name, "accounts.json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(descriptor)
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass

    @contextmanager
    def locked(self, *, create=False):
        with _closed("storage"):
            directory = self._directory(create)
            lock = None
            try:
                lock = os.open("accounts.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=directory)
                self._secure_file(os.fstat(lock))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise AuthError("busy") from None
                value, identity = self._read(directory)
                if value is None:
                    value = {"schema": 1, "host_id": "urn:uuid:" + str(uuid.uuid4()), "active": None, "accounts": {}}
                    if create:
                        self._write(directory, value, identity)
                        value, identity = self._read(directory)
                def save(updated):
                    nonlocal identity
                    self._write(directory, updated, identity)
                    _, identity = self._read(directory)
                yield value, save
            finally:
                if lock is not None:
                    os.close(lock)
                os.close(directory)

    def status(self):
        try:
            descriptor = self._directory()
            os.close(descriptor)
        except FileNotFoundError:
            return {"active": None, "accounts": []}
        except (OSError, ValueError, TypeError):
            raise AuthError("storage") from None
        with _closed("storage"):
            with self.locked() as (state, _):
                return {"active": state["active"], "accounts": [_safe_account(key, account) for key, account in sorted(state["accounts"].items())]}


def _unique_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise AuthError("storage")
        result[key] = value
    return result


def _account_id(client_id, subject):
    return hashlib.sha256((ISSUER + "\0" + client_id + "\0" + subject).encode()).hexdigest()[:24]


def _account_fingerprint(account):
    return hashlib.sha256(json.dumps(account, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_account(account):
    fields = {"client_id", "subject", "email", "id_token", "access_token", "refresh_token", "expires_at", "scopes"}
    if type(account) is not dict or set(account) != fields:
        raise AuthError("storage")
    if type(account["client_id"]) is not str or not _CLIENT_ID.fullmatch(account["client_id"]):
        raise AuthError("storage")
    for name in ("subject", "email", "id_token", "access_token", "refresh_token"):
        _text(account[name], 16384, empty=name != "subject")
    for name in ("id_token", "access_token", "refresh_token"):
        if account[name]:
            _credential(account[name])
    if (type(account["expires_at"]) not in {int, float} or not math.isfinite(account["expires_at"])
            or type(account["scopes"]) is not list or len(account["scopes"]) > 32
            or len(set(account["scopes"])) != len(account["scopes"])):
        raise AuthError("storage")
    for scope in account["scopes"]:
        _text(scope, 128)


def _safe_account(account_id, account):
    return {"id": account_id, "email": account["email"], "connected": bool(account["access_token"]),
            "plan_usage": PLAN_SCOPES.issubset(account["scopes"])}


@dataclass(repr=False)
class PendingLogin:
    authorization_url: str = field(repr=False)
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    redirect_uri: str = field(repr=False)
    host_id: str = field(repr=False)
    client_id: str | None = field(repr=False)
    subject: str | None = field(repr=False)
    expires_at: float = field(repr=False)
    account_fingerprint: str | None = field(default=None, repr=False)
    issued_client_id: str | None = field(default=None, repr=False)
    used: bool = field(default=False, repr=False)


class AuthClient:
    def __init__(self, store, *, transport=https_request, clock=time.time):
        self.store, self.transport, self.clock = store, transport, clock

    def _request(self, method, url, form=None, *, empty=False):
        with _closed("network"):
            body = urlencode(form).encode("ascii") if form is not None else None
            headers = {"Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            status, raw = self.transport(method, _metadata_url(url), body, headers)
            if type(status) is not int or status != 200:
                raise AuthError("network")
            if empty:
                if type(raw) is not bytes or len(raw) > MAX_HTTP_BYTES:
                    raise AuthError("response")
                return None
            return _json(raw)

    def _discovery(self):
        result = self._request("GET", DISCOVERY)
        if (result.get("issuer") != ISSUER or result.get("authorization_endpoint") != AUTHORIZE
                or result.get("token_endpoint") != TOKEN or result.get("jwks_uri") != JWKS):
            raise AuthError("identity")
        return result

    def begin_login(self, redirect_uri, *, account_id=None, new_account=False):
        """The trusted caller must already be listening on this exact URI."""
        with _closed("input"):
            parts = urlsplit(redirect_uri)
            if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.path != CALLBACK_PATH
                    or parts.username is not None or parts.password is not None or parts.query or parts.fragment
                    or parts.netloc != "127.0.0.1:" + str(parts.port) or not 1024 <= parts.port <= 65535):
                raise AuthError("input")
            with self.store.locked(create=True) as (state, _):
                selected = None if new_account else account_id or state["active"]
                account = state["accounts"].get(selected) if selected else None
                if selected is not None and account is None:
                    raise AuthError("input")
                host_id = state["host_id"]
                if account is None and len(state["accounts"]) >= MAX_ACCOUNTS:
                    raise AuthError("storage")
                client_id = account["client_id"] if account else None
                subject = account["subject"] if account else None
                fingerprint = _account_fingerprint(account) if account else None
            verifier, nonce, random_state = secrets.token_urlsafe(48), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            query = {"client_id": client_id or "dynamic_agent_client", "ext_agent_host_id": host_id,
                     "response_type": "code", "redirect_uri": redirect_uri, "scope": " ".join(sorted(SCOPES)),
                     "resource": RESOURCE, "state": random_state, "nonce": nonce,
                     "code_challenge_method": "S256", "code_challenge": _b64(hashlib.sha256(verifier.encode("ascii")).digest())}
            if client_id is None:
                query["agent_name_hint"] = APP_NAME
            # Do not put retained ID tokens in a displayed/loggable URL. The
            # supported selector flow without id_token_hint remains available.
            return PendingLogin(AUTHORIZE + "?" + urlencode(query), random_state, nonce, verifier,
                                redirect_uri, host_id, client_id, subject, self.clock() + 600,
                                account_fingerprint=fingerprint)

    def _identity(self, token, client_id, nonce=None, access_token=None):
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError:
            raise AuthError("dependency") from None
        with _closed("identity"):
            token = _text(token)
            parts = token.split(".")
            if len(parts) != 3:
                raise AuthError("identity")
            header, claims = _json(_unb64(parts[0])), _json(_unb64(parts[1]))
            if (header.get("alg") != "RS256" or type(header.get("kid")) is not str
                    or len(header["kid"]) > 256 or "crit" in header or "jku" in header or "x5u" in header):
                raise AuthError("identity")
            self._discovery()
            keys = self._request("GET", JWKS).get("keys")
            if type(keys) is not list or not 1 <= len(keys) <= 32:
                raise AuthError("identity")
            matches = [k for k in keys if type(k) is dict and k.get("kid") == header["kid"]]
            if len(matches) != 1:
                raise AuthError("identity")
            key = matches[0]
            if key.get("kty") != "RSA" or key.get("alg", "RS256") != "RS256" or key.get("use", "sig") != "sig":
                raise AuthError("identity")
            modulus, exponent = int.from_bytes(_unb64(key["n"]), "big"), int.from_bytes(_unb64(key["e"]), "big")
            if not 2048 <= modulus.bit_length() <= 8192 or exponent != 65537:
                raise AuthError("identity")
            public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
            try:
                public_key.verify(_unb64(parts[2]), (parts[0] + "." + parts[1]).encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
            except InvalidSignature:
                raise AuthError("identity") from None
            now, audience = self.clock(), claims.get("aud")
            valid_audience = audience == client_id or (type(audience) is list and client_id in audience
                and all(type(a) is str for a in audience) and len(audience) <= 8)
            if (claims.get("iss") != ISSUER or not valid_audience
                    or type(claims.get("exp")) not in {int, float} or not math.isfinite(claims["exp"])
                    or type(claims.get("iat")) not in {int, float} or not math.isfinite(claims["iat"])
                    or claims["exp"] <= now - 5 or claims["iat"] > now + 5
                    or claims["exp"] <= claims["iat"]
                    or "nbf" in claims and (type(claims["nbf"]) not in {int, float}
                                             or not math.isfinite(claims["nbf"]) or claims["nbf"] > now + 5)
                    or type(audience) is list and len(audience) > 1 and claims.get("azp") != client_id
                    or "azp" in claims and claims["azp"] != client_id):
                raise AuthError("identity")
            if nonce is not None and (type(claims.get("nonce")) is not str or not hmac.compare_digest(claims["nonce"], nonce)):
                raise AuthError("identity")
            if "at_hash" in claims:
                if type(access_token) is not str:
                    raise AuthError("identity")
                expected_hash = _b64(hashlib.sha256(access_token.encode("ascii")).digest()[:16])
                if type(claims["at_hash"]) is not str or not hmac.compare_digest(expected_hash, claims["at_hash"]):
                    raise AuthError("identity")
            _text(claims.get("sub"), 512)
            if "email" in claims:
                _text(claims["email"], 512)
            return claims

    def _tokens(self, response, *, previous=None):
        if response.get("token_type") != "Bearer":
            raise AuthError("response")
        access = _credential(response.get("access_token"))
        refresh = _credential(response.get("refresh_token", previous["refresh_token"] if previous else None))
        expires = response.get("expires_in")
        if type(expires) is not int or not 0 < expires <= 7 * 24 * 3600:
            raise AuthError("response")
        if "scope" in response:
            scope = _text(response["scope"], 4096)
            scopes = scope.split(" ")
            if not all(scopes) or len(scopes) > 32 or len(set(scopes)) != len(scopes):
                raise AuthError("scope")
        elif previous is not None:
            scopes = previous["scopes"][:]
        else:
            raise AuthError("scope")
        if not PLAN_SCOPES.issubset(scopes):
            raise AuthError("scope")
        return {"access_token": access, "refresh_token": refresh, "expires_at": self.clock() + expires, "scopes": sorted(scopes)}

    def finish_login(self, pending, callback_query):
        with _closed("response"):
            if type(pending) is not PendingLogin or pending.used:
                raise AuthError("state")
            if self.clock() > pending.expires_at:
                pending.used = True
                raise AuthError("expired")
            _text(callback_query, 8192)
            if callback_query.startswith("?"):
                callback_query = callback_query[1:]
            fields = _unique_pairs(parse_qsl(callback_query, keep_blank_values=True, strict_parsing=True, max_num_fields=8))
            if (type(fields.get("state")) is not str or not hmac.compare_digest(fields["state"], pending.state)):
                raise AuthError("state")
            # Unauthenticated stray loopback requests must not consume the
            # valid pending flow. Once state matches, success OR failure is
            # terminal; never exchange a returned code twice.
            pending.used = True
            if "error" in fields:
                raise AuthError("denied")
            if set(fields) - {"code", "state", "client_id", "scope"}:
                raise AuthError("response")
            code = _text(fields.get("code"), 4096)
            client_id = fields.get("client_id", pending.client_id)
            if (type(client_id) is not str or not _CLIENT_ID.fullmatch(client_id)
                    or pending.client_id is not None and client_id != pending.client_id):
                raise AuthError("identity")
            # Retain only in this pending attempt if code exchange fails. A
            # fresh authorization may reuse it without registering a new app;
            # it becomes a saved identity only after JWT verification.
            pending.issued_client_id = client_id
            tokens = self._request("POST", TOKEN, {"grant_type": "authorization_code", "client_id": client_id,
                                   "code": code, "code_verifier": pending.verifier,
                                   "redirect_uri": pending.redirect_uri, "resource": RESOURCE})
            identity = self._identity(tokens.get("id_token"), client_id, pending.nonce,
                                      access_token=tokens.get("access_token"))
            if pending.subject is not None and identity["sub"] != pending.subject:
                raise AuthError("identity")
            account = {"client_id": client_id, "subject": identity["sub"], "email": identity.get("email", ""),
                       "id_token": tokens["id_token"], **self._tokens(tokens)}
            account_id = _account_id(client_id, identity["sub"])
            with self.store.locked() as (state, save):
                if state["host_id"] != pending.host_id:
                    raise AuthError("conflict")
                if pending.subject is not None:
                    selected = state["accounts"].get(account_id)
                    if (selected is None or selected["subject"] != pending.subject
                            or _account_fingerprint(selected) != pending.account_fingerprint):
                        raise AuthError("conflict")
                elif account_id in state["accounts"] or len(state["accounts"]) >= MAX_ACCOUNTS:
                    raise AuthError("conflict")
                state["accounts"][account_id] = account
                state["active"] = account_id
                save(state)
            return _safe_account(account_id, account)

    def restart_login(self, pending, redirect_uri):
        """Explicit fresh authorization after code-exchange failure, not retry."""
        if (type(pending) is not PendingLogin or not pending.used
                or type(pending.issued_client_id) is not str
                or not _CLIENT_ID.fullmatch(pending.issued_client_id)):
            raise AuthError("state")
        selected = _account_id(pending.issued_client_id, pending.subject) if pending.subject is not None else None
        fresh = self.begin_login(redirect_uri, account_id=selected, new_account=selected is None)
        if fresh.host_id != pending.host_id:
            raise AuthError("conflict")
        query = dict(parse_qsl(fresh.authorization_url.split("?", 1)[1]))
        query["client_id"] = pending.issued_client_id
        query.pop("agent_name_hint", None)
        fresh.authorization_url = AUTHORIZE + "?" + urlencode(query)
        fresh.client_id, fresh.subject = pending.issued_client_id, pending.subject
        fresh.account_fingerprint = pending.account_fingerprint
        return fresh

    def select_account(self, account_id):
        with self.store.locked() as (state, save):
            if account_id not in state["accounts"]:
                raise AuthError("input")
            state["active"] = account_id
            save(state)
            return _safe_account(account_id, state["accounts"][account_id])

    def access_token(self):
        """One internal credential read/refresh, no inference retry or output."""
        with self.store.locked() as (state, save):
            account = state["accounts"].get(state["active"])
            if account is None or not account["access_token"] or not PLAN_SCOPES.issubset(account["scopes"]):
                raise AuthError("auth-required")
            if account["expires_at"] <= self.clock() + 60:
                tokens = self._request("POST", TOKEN, {"grant_type": "refresh_token", "client_id": account["client_id"],
                                      "refresh_token": account["refresh_token"], "resource": RESOURCE})
                updated = {**account, **self._tokens(tokens, previous=account)}
                if "id_token" in tokens:
                    identity = self._identity(tokens["id_token"], account["client_id"],
                                              access_token=tokens.get("access_token"))
                    if identity["sub"] != account["subject"]:
                        raise AuthError("identity")
                    updated["id_token"] = tokens["id_token"]
                state["accounts"][state["active"]] = updated
                # Atomic save happens before a replacement token is exposed.
                save(state)
                account = updated
            return Secret(account["access_token"])

    def logout(self):
        """Stop local usage even if remote revocation cannot be confirmed."""
        with self.store.locked() as (state, save):
            account = state["accounts"].get(state["active"])
            if account is None or not any(account[k] for k in ("access_token", "refresh_token", "id_token")):
                return True
            confirmed = not account["refresh_token"]
            if account["refresh_token"]:
                try:
                    endpoint = self._discovery().get("revocation_endpoint")
                    self._request("POST", _metadata_url(endpoint), {"client_id": account["client_id"],
                        "token": account["refresh_token"], "token_type_hint": "refresh_token"}, empty=True)
                    confirmed = True
                except AuthError:
                    confirmed = False
            for name in ("access_token", "refresh_token", "id_token"):
                account[name] = ""
            account["expires_at"] = 0
            save(state)
            return confirmed
