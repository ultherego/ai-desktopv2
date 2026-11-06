"""Service-owned native Codex quota sessions. Model inference never mounts these credentials."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading

from . import account_runtime, codex_runtime
from .account_store import PLANS, named_account, quota
from .analysis_child import _Bridge, check_boundary
from .analysis_contract import AnalysisDenied, canonical
from .analysis_sandbox import HOME, _identity_files, child_failure, failure_stage, profile_lock, service_identity
from .analysis_seccomp import install_mode_filter
from .codex_protocol import CLIENT_VERSION
from .e2_connect_proxy import CODEX_ACCOUNT_HOSTS, ConnectProxy, open_public
from .e2_ipc import _unique_pairs
from .process_io import bounded_process
from .siwc_auth import AuthError, AuthStore


MAX_CREDENTIAL = 128 * 1024
_SESSION_ENV = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}


def _private_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_gid != os.getegid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise AnalysisDenied("identity")


def _read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        _private_file(before)
        if not 0 < before.st_size <= MAX_CREDENTIAL:
            raise AnalysisDenied("identity")
        raw = stream.read(MAX_CREDENTIAL + 1)
        if len(raw) != before.st_size or os.fstat(fd).st_ctime_ns != before.st_ctime_ns:
            raise AnalysisDenied("identity")
    try:
        return json.loads(raw, object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError, RecursionError):
        raise AnalysisDenied("identity") from None


def _sync_dir(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path, value):
    raw = canonical(value).encode("utf-8")
    if len(raw) > MAX_CREDENTIAL:
        raise AnalysisDenied("output-limit")
    fd, stage = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, path)
        _sync_dir(path.parent)
    finally:
        Path(stage).unlink(missing_ok=True)


def _native_account(document):
    # Only the pinned native credential format can survive the disposable CODEX_HOME.
    # Tokens and identifiers are never emitted, logged or compared with desktop metadata.
    if (type(document) is not dict or set(document) - {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}
            or document.get("auth_mode") != "chatgpt" or document.get("OPENAI_API_KEY") is not None):
        raise AnalysisDenied("auth-required")
    tokens = document.get("tokens")
    if (type(tokens) is not dict or set(tokens) - {
            "id_token", "access_token", "refresh_token", "account_id", "oauth_provenance", "login_owner_id"}
            or any(type(tokens.get(field)) is not str or not 0 < len(tokens[field]) <= 32768
                   or not all(33 <= ord(c) <= 126 for c in tokens[field])
                   for field in ("id_token", "access_token", "refresh_token"))):
        raise AnalysisDenied("identity")
    account = tokens.get("account_id")
    if account is not None and (type(account) is not str or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", account)):
        raise AnalysisDenied("identity")
    owner = tokens.get("login_owner_id")
    if ((owner is None and account is None) or
            (owner is not None and (type(owner) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", owner)))):
        raise AnalysisDenied("identity")
    if "last_refresh" in document and (type(document["last_refresh"]) is not str or len(document["last_refresh"]) > 100):
        raise AnalysisDenied("identity")
    provenance = tokens.get("oauth_provenance")
    if provenance is not None and (type(provenance) is not dict or set(provenance) != {"issuer", "client_id"}
            or provenance["issuer"] != "https://auth.openai.com" or type(provenance["client_id"]) is not str
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", provenance["client_id"])):
        raise AnalysisDenied("identity")
    return account, owner, provenance


def _binding(uid, alias, *, enroll=False):
    try:
        with account_runtime.selected(uid, "codex", alias, allow_default=True):
            profile = account_runtime.directory(HOME / "codex")
            account_runtime._private_directory(profile)
            return AuthStore(profile).quota_binding(enroll=enroll)
    except FileNotFoundError:
        raise AnalysisDenied("auth-required") from None
    except AuthError as error:
        raise AnalysisDenied("busy" if error.reason == "busy" else "auth-required") from None


def _owner_directory(uid):
    account_runtime.identity(uid, "codex", "default", allow_default=True)
    return HOME / f"quota-codex-{uid}"


@contextmanager
def _locked(directory, *, create):
    account_runtime._private_directory(directory, create=create)
    fd = os.open(directory / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        _private_file(os.fstat(fd))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AnalysisDenied("busy") from None
        yield
    finally:
        os.close(fd)


def has_links(uid):
    """Whether this service retains a quota session for this desktop user, including stale links."""
    service_identity()
    directory = _owner_directory(uid)
    try:
        account_runtime._private_directory(directory)
    except FileNotFoundError:
        return False
    with os.scandir(directory) as files:
        for f in files:
            if f.name.endswith(".json") and (f.name == "default.json" or named_account(f.name[:-5])):
                _private_file(f.stat(follow_symlinks=False))
                return True
    return False


def _receipt(path, generation):
    try:
        data = _read_json(path)
    except FileNotFoundError:
        raise AnalysisDenied("auth-required") from None
    if type(data) is not dict or set(data) != {"generation", "credential"} or data["generation"] != generation:
        raise AnalysisDenied("auth-required")
    _native_account(data["credential"])
    return data["credential"]


def _safe_result(value):
    if type(value) is not dict or set(value) != {"windows", "plan"} or type(value["windows"]) is not dict:
        raise AnalysisDenied("result")
    if value["plan"] is not None and (type(value["plan"]) is not str or value["plan"] not in PLANS):
        raise AnalysisDenied("result")
    if value["windows"]:
        try:
            quota(value["windows"])
        except ValueError:
            raise AnalysisDenied("result") from None
    return value


def _version(directory):
    code, out, err = bounded_process(codex_runtime.namespace_command("version", directory), env=_SESSION_ENV,
                                     timeout=10, limit=4096)
    if code or out.strip() != ("codex-cli " + CLIENT_VERSION).encode("ascii"):
        raise child_failure(err, "version") if code else AnalysisDenied("version")


class _AccountProxy(ConnectProxy):
    daemon_threads = True
    block_on_close = False


def _exchange(action, directory, challenge):
    from .codex_account_protocol import validate_challenge
    delivered = False
    first = bytearray()

    def on_stdout(block):
        nonlocal delivered
        if delivered:
            return
        first.extend(block)
        if b"\n" not in first:
            return
        row = json.loads(bytes(first).split(b"\n", 1)[0], object_pairs_hook=_unique_pairs)
        if type(row) is not dict or set(row) != {"url", "code"}:
            raise AnalysisDenied("result")
        validate_challenge(row["url"], row["code"])
        challenge(row["url"], row["code"])
        delivered = True
        first.clear()

    timeout = 330 if action == "connect" else 25
    def dial(host):
        return open_public(host, hosts=CODEX_ACCOUNT_HOSTS)
    with _AccountProxy(str(directory / "egress.sock"), dial=dial, hosts=CODEX_ACCOUNT_HOSTS, stream_seconds=timeout) as proxy:
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            cmd = codex_runtime.namespace_command("quota-" + action, directory)
            code, out, err = bounded_process(cmd, env=_SESSION_ENV, timeout=timeout, limit=8192,
                                             stdout_chunk=on_stdout if action == "connect" else None)
        finally:
            proxy.shutdown()
            thread.join(timeout=2)
    if code:
        raise child_failure(err, "client")
    lines = out.splitlines()
    if len(lines) != (2 if action == "connect" else 1) or (action == "connect" and not delivered):
        raise AnalysisDenied("result")
    return _safe_result(json.loads(lines[-1], object_pairs_hook=_unique_pairs))


def quota_child(action, metadata):
    """Fixed RPC processor in the kernel-validated native client namespace."""
    from . import codex_account_protocol
    check_boundary(metadata)
    home = Path(codex_runtime.INNER_HOME)
    names = {p.name for p in home.iterdir()}
    if (names != ({"auth.json"} if action == "quota-read" else set()) or any(Path("/work").iterdir())
            or Path("/home/client/profile").exists()):
        raise AnalysisDenied("sandbox")
    install_mode_filter()
    def present(url, code):
        codex_account_protocol.validate_challenge(url, code)
        print(canonical({"url": url, "code": code}), flush=True)
    with _Bridge(320 if action == "quota-connect" else 24) as bridge:
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            result = codex_account_protocol.run(str(codex_runtime.BINARY), action.removeprefix("quota-"),
                                                codex_runtime.client_environment(),
                                                challenge=present if action == "quota-connect" else None)
            print(canonical(_safe_result(result)), flush=True)
            return 0
        finally:
            bridge.shutdown()
            thread.join(timeout=2)


def run(action, uid, alias, *, challenge=None):
    """Return safe native quota data for the exact authenticated desktop UID and local alias."""
    account_runtime.identity(uid, "codex", alias, allow_default=True)
    if action not in ("connect", "read", "disconnect") or ((action == "connect") != callable(challenge)):
        raise AnalysisDenied("input")
    service_identity()
    directory = _owner_directory(uid)
    path = directory / (alias + ".json")
    if action != "connect":
        try:
            account_runtime._private_directory(directory)
        except FileNotFoundError:
            if action == "disconnect":
                return None
            raise AnalysisDenied("auth-required") from None
    with failure_stage("profile"), _locked(directory, create=action == "connect"):
        if action == "disconnect":
            try:
                _private_file(path.lstat())
                path.unlink()
                _sync_dir(directory)
            except FileNotFoundError:
                pass
            return None
        with profile_lock():
            generation = _binding(uid, alias, enroll=action == "connect")
        old = _receipt(path, generation) if action == "read" else None
        # Refuse absent, stale and foreign grants before even starting --version.
        # No credential enters the namespace until the pinned version passes.
        codex_runtime.runtime_ready()
        with tempfile.TemporaryDirectory(prefix="ai-desktop-quota-") as temp:
            work = Path(temp)
            _identity_files(work)
            _version(work)
            staging = work / "native-home"
            staging.mkdir(mode=0o700)
            if action == "read":
                _write_json(staging / "auth.json", old)
            try:
                result = _exchange(action, work, challenge)
            except AnalysisDenied:
                if action != "read":
                    raise
                # Refresh tokens may rotate before the account backend refuses a
                # read. Preserve a same-account rotation even in that case.
                _commit_rotation(path, staging, uid, alias, generation, old)
                raise
            if action == "connect":
                credential = _read_json(staging / "auth.json")
                _native_account(credential)
                with profile_lock():
                    if _binding(uid, alias) != generation:
                        raise AnalysisDenied("auth-required")
                    _write_json(path, {"generation": generation, "credential": credential})
            else:
                _commit_rotation(path, staging, uid, alias, generation, old)
            return result


def _commit_rotation(path, staging, uid, alias, generation, previous):
    credential = _read_json(staging / "auth.json")
    if _native_account(previous) != _native_account(credential):
        raise AnalysisDenied("identity")
    # This per-UID lock still serializes all readers/writers of the saved
    # native credential. Preserve rotation under the original grant even if
    # a concurrent SIWC operation prevents us from releasing the reading.
    if credential != previous:
        _write_json(path, {"generation": generation, "credential": credential})
    with profile_lock():
        if _binding(uid, alias) != generation:
            raise AnalysisDenied("auth-required")
