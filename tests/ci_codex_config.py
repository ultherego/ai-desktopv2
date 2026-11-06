"""Pinned Codex startup in a disposable CI network namespace, without OAuth.

The trusted parent downloads a digest-pinned asset into a private temporary
directory. The native process sees an empty home/work and no network, token,
profile or proxy. The offline SIWC turn must stop at the network boundary,
and native account/read must report the empty profile as signed out. A separate
private namespace provides fake native credentials and a fixture usage response.
Normal verification never executes this opt-in driver.
"""
import json
import os
import re
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_desktop import client_compat, codex_compat, codex_account_protocol as native, codex_protocol
from ai_desktop.analysis_contract import AnalysisDenied, application_input
from ai_desktop.codex_protocol import (CLIENT_VERSION, Protocol, exchange, fixed_command,
                                      notification_fields, startup_notification)
from ai_desktop.e2_ipc import _unique_pairs
from ai_desktop.e2_subscription_boundary import base_command
from ai_desktop.process_io import bounded_process
from scripts import fetch_codex_client as asset

MAX_OUTPUT = 131072
MAX_LINE = 65536


class StartupDenied(ValueError):
    def __init__(self, reason, *, check=None, shape=None):
        self.reason = reason if reason in {"preflight", "pin", "initialize", "protocol", "exit", "timeout", "limit", "turn", "native-account"} else "preflight"
        message = "CI CODEX CONFIG: FAIL reason=" + self.reason
        if type(check) is str and (check in ("empty-line", "non-json", "unexpected-record", "extra-startup-notification")
                                   or self.reason == "native-account" and (check in {"unexpected-result", "request-verification", "fixture-exit"}
                                       or re.fullmatch(r"(?:new|initialize|account|limits|signed-out|done)/(?:network|timeout|result|client|auth-required|authorization|auth-method)", check))
                                   or self.reason == "turn" and re.fullmatch(r"(?:refusal=[a-z-]{1,24} )?codex-[a-z-]{1,40}", check)):
            message += " check=" + check
        # Only closed structural categories/counts can cross this boundary.
        # Native keys, methods, names, IDs, paths and prose are never printed.
        if (type(shape) is dict and set(shape) == {"phase", "kind", "fields", "params", "timestamp", "status", "environment"}
                and all(type(shape[key]) is str for key in ("phase", "kind", "timestamp", "status", "environment"))
                and shape["phase"] in ("before-initialize", "after-initialize")
                and shape["kind"] in ("remote-status", "config-warning", "warning", "deprecation", "rpc", "other")
                and all(type(shape[key]) is int and 0 <= shape[key] <= 32 for key in ("fields", "params"))
                and shape["timestamp"] in ("absent", "valid", "invalid")
                and shape["status"] in ("absent", "disabled", "connecting", "connected", "errored", "other")
                and shape["environment"] in ("absent", "null", "string", "other")):
            message += (" phase=%s kind=%s fields=%d params=%d timestamp=%s status=%s environment=%s" %
                tuple(shape[key] for key in ("phase", "kind", "fields", "params", "timestamp", "status", "environment")))
        super().__init__(message)


def record_shape(value, received):
    """Describe only finite structure; never return an untrusted field value."""
    data = value if type(value) is dict else {}
    params = data.get("params") if type(data.get("params")) is dict else {}
    method = data.get("method")
    kinds = {"remoteControl/status/changed": "remote-status", "configWarning": "config-warning",
             "warning": "warning", "deprecationNotice": "deprecation"}
    kind = kinds.get(method, "other") if type(method) is str else "rpc" if "id" in data else "other"
    timestamp = data.get("emittedAtMs")
    timestamp_kind = ("absent" if "emittedAtMs" not in data else "valid" if type(timestamp) is int
                      and 0 <= timestamp < 2**63 else "invalid")
    status = params.get("status")
    environment = params.get("environmentId")
    return {"phase": "after-initialize" if received else "before-initialize", "kind": kind,
        "fields": min(32, len(data)), "params": min(32, len(params)), "timestamp": timestamp_kind,
        "status": "absent" if "status" not in params else status if type(status) is str
            and status in ("disabled", "connecting", "connected", "errored") else "other",
        "environment": "absent" if "environmentId" not in params else "null" if environment is None
            else "string" if type(environment) is str else "other"}


def require_ci():
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux"
            or os.getuid() <= 0 or os.getuid() != os.geteuid() or os.getgid() != os.getegid()):
        raise StartupDenied("preflight")
    release = Path("/etc/os-release").read_text().splitlines()
    if "ID=ubuntu" not in release or 'VERSION_ID="24.04"' not in release:
        raise StartupDenied("preflight")
    if asset.VERSION != CLIENT_VERSION:
        raise StartupDenied("pin")


def command(binary, directory, *, native_account=False):
    directory = Path(directory)
    return base_command() + [
        "--disable-userns", "--assert-userns-disabled", "--remount-ro", "/proc",
        "--dir", "/home/client/.codex", "--setenv", "CODEX_HOME", "/home/client/.codex",
        "--setenv", "TERM", "dumb", "--setenv", "NO_COLOR", "1",
        "--ro-bind", str(directory / "passwd"), "/etc/passwd",
        "--ro-bind", str(directory / "group"), "/etc/group",
        "--ro-bind", str(binary), "/opt/codex", "--chdir", "/work",
        *(native.fixed_command("/opt/codex") if native_account else fixed_command("/opt/codex")),
    ]


def initialize_only(argv, *, timeout=25):
    """Keep stdin open until the exact initialize response, then require EOF.

    Bounded private stdout/stderr never become public diagnostics. The argv
    argument is a trusted test seam; the CLI always builds the fixed namespace
    command above and exposes no destination or command override.
    """
    if type(timeout) not in (int, float) or isinstance(timeout, bool) or not 0 < timeout <= 30:
        raise StartupDenied("preflight")
    raw = application_input(None, "SYNTHETIC offline startup", context=[]).encode()
    initial = Protocol(raw).start()
    process, received, startup_events = None, False, set()
    pending, buffered = bytearray(initial), bytearray()
    total, deadline = 0, time.monotonic() + timeout
    try:
        process = subprocess.Popen(argv, env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, start_new_session=True, bufsize=0)
        with selectors.DefaultSelector() as selector:
            for stream, kind, event in ((process.stdin, "in", selectors.EVENT_WRITE),
                                        (process.stdout, "out", selectors.EVENT_READ),
                                        (process.stderr, "err", selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event, kind)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StartupDenied("timeout")
                for key, _ in selector.select(min(.1, remaining)):
                    if key.data == "in":
                        try:
                            count = os.write(key.fileobj.fileno(), pending)
                        except BlockingIOError:
                            continue
                        if count <= 0:
                            raise StartupDenied("initialize")
                        del pending[:count]
                        if not pending:
                            selector.unregister(key.fileobj)
                        continue
                    try:
                        block = os.read(key.fileobj.fileno(), 8192)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(block)
                    if total > MAX_OUTPUT:
                        raise StartupDenied("limit")
                    if key.data == "err":
                        continue
                    buffered.extend(block)
                    while b"\n" in buffered:
                        record, _, tail = buffered.partition(b"\n")
                        buffered[:] = tail
                        if not record:
                            raise StartupDenied("protocol", check="empty-line")
                        if len(record) > MAX_LINE:
                            raise StartupDenied("limit")
                        try:
                            value = json.loads(record, object_pairs_hook=_unique_pairs,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                        except (ValueError, TypeError, UnicodeError, RecursionError):
                            raise StartupDenied("protocol", check="non-json") from None
                        shape = record_shape(value, received)
                        if received:
                            try:
                                method, params = notification_fields(value)
                                kind = startup_notification(method, params)
                            except (AnalysisDenied, ValueError, TypeError, UnicodeError):
                                raise StartupDenied("protocol", check="unexpected-record", shape=shape) from None
                            if kind not in ("remote-status", "nested-userns-warning"):
                                raise StartupDenied("protocol", check="unexpected-record", shape=shape)
                            if kind in startup_events:
                                raise StartupDenied("protocol", check="extra-startup-notification", shape=shape)
                            startup_events.add(kind)
                            continue
                        if (type(value) is not dict or set(value) != {"id", "result"}
                                or type(value["id"]) is not int or value["id"] != 0
                                or type(value["result"]) is not dict):
                            raise StartupDenied("initialize", check="unexpected-record", shape=shape)
                        received = True
                        process.stdin.close()
                    if len(buffered) > MAX_LINE:
                        raise StartupDenied("limit")
            if buffered or not received:
                raise StartupDenied("initialize")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StartupDenied("timeout")
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise StartupDenied("timeout") from None
            if process.returncode != 0:
                raise StartupDenied("exit")
    except StartupDenied:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise StartupDenied("initialize") from None
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


FAKE_TOKEN = "ci-synthetic-not-a-credential-0123456789"


def turn_command(binary, directory):
    """The same fixed namespace command plus a SYNTHETIC token (never a credential), still without any network."""
    argv = command(binary, directory)
    index = argv.index("--chdir")
    argv[index:index] = ["--setenv", "ACCESS_TOKEN", FAKE_TOKEN]
    return argv


def offline_turn(argv, *, timeout=60):
    """thread/start and turn/start against the REAL client with no network.

    The exchange must get all the way to the network layer and fail THERE (reason `network`). Any refusal before that
    - an unknown warning, notification or server request - is a compatibility bug that initialize-only cannot see: the
    first native Codex run failed on exactly such a notice. Nothing is sent to any provider; no account is involved.
    """
    raw = application_input(None, "SYNTHETIC offline turn", context=[]).encode()
    try:
        exchange(argv, Protocol(raw), env={"PATH": "/usr/bin", "LANG": "C.UTF-8"}, timeout=timeout)
    except AnalysisDenied as error:
        if error.reason == "network":
            return
        check = (error.diagnostic if type(error.diagnostic) is str else "codex-unclassified")
        raise StartupDenied("turn", check="refusal=%s %s" % (error.reason if re.fullmatch(r"[a-z-]{1,24}", error.reason) else "other", check)) from None
    raise StartupDenied("turn")


def offline_native_account(argv):
    """The real pinned build must accept the account config and report its empty native store."""
    protocol = native.AccountProtocol("read")
    try:
        codex_protocol._exchange(argv, protocol, env={"PATH": "/usr/bin", "LANG": "C.UTF-8"}, timeout=20)
    except AnalysisDenied as exc:
        if exc.reason == "auth-required" and protocol.state == "signed-out":
            return
        raise StartupDenied("native-account") from None
    raise StartupDenied("native-account")


def compatibility_probe(binary, directory):
    """Exercise the production offline compatibility adapter against the genuine fixture."""
    argv = command(binary, directory, native_account=True)
    native_start = argv.index("--chdir") + 2
    env = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
    def run(action):
        if action == "compat-rpc":
            # Match both production adapters: SIWC analysis must load its own
            # strict provider/ephemeral-store configuration without any token.
            analysis = command(binary, directory) + ["-c", 'model_reasoning_effort="high"']
            codex_protocol._exchange(analysis, codex_compat.ProbeProtocol(native_account=False), env=env,
                                     timeout=20, stop_after_reply=True)
            codex_protocol._exchange(argv, codex_compat.ProbeProtocol(), env=env,
                                     timeout=20, stop_after_reply=True)
            return 0, b'{"initialize":true,"signed_out":true,"models":true}', b""
        if action == "version":
            selected = argv[:native_start] + ["/opt/codex", "--version"]
        elif action in ("compat-help", "compat-reject"):
            selected = argv + ([client_compat.INVALID_OPTION] if action == "compat-reject" else []) + ["--help"]
        else:
            raise StartupDenied("preflight")
        return bounded_process(selected, env=env, timeout=20, limit=client_compat.MAX_OUTPUT)
    try:
        result = client_compat.check("codex", run)
        if result.status != "compatible-tested":
            raise StartupDenied("pin")
    except AnalysisDenied:
        raise StartupDenied("protocol") from None


def fixture_native_quota(binary, directory):
    """Exercise genuine v0.166.0 account and quota RPCs against a synthetic, loopback-only backend."""
    argv = command(binary, directory, native_account=True)
    pos = argv.index("--chdir")
    argv[pos:] = ["--ro-bind", str(ROOT / "tests/ci_codex_quota_backend.py"), "/opt/quota-backend.py",
                  "--ro-bind", str(ROOT / "ai_desktop"), "/opt/ai_desktop",
                  "--chdir", "/work", "/usr/bin/python3", "-I", "-B", "/opt/quota-backend.py"]
    fixture = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=25,
                             env={"PATH": "/usr/bin", "LANG": "C.UTF-8"})
    if fixture.returncode:
        # The helper prints only a fixed protocol stage and reason. Discard everything else
        # (including native/host stderr) rather than let account data into a CI log.
        clue = fixture.stdout.strip()
        if re.fullmatch(rb"(?:new|initialize|account|limits|signed-out|done)/(?:network|timeout|result|client|auth-required|authorization|auth-method)", clue):
            raise StartupDenied("native-account", check=clue.decode())
        if clue == b"request-verification":
            raise StartupDenied("native-account", check="request-verification")
        raise StartupDenied("native-account", check="fixture-exit")
    expected = {"plan": "plus", "windows": {"five_hour": {"percent": 31, "reset": 2000000000},
                                             "seven_day": {"percent": 47, "reset": 2000500000}}}
    try:
        result = json.loads(fixture.stdout, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise StartupDenied("native-account", check="unexpected-result") from None
    if result != expected:
        raise StartupDenied("native-account", check="unexpected-result")


def run():
    require_ci()
    with tempfile.TemporaryDirectory(prefix="aid-codex-config-") as temporary:
        directory = Path(temporary)
        (directory / "packaging").mkdir(mode=0o700)
        (directory / "packaging" / "arch").mkdir(mode=0o700)
        binary = asset.prepare(root=directory)
        (directory / "passwd").write_text("client:x:%d:%d::/home/client:/usr/bin/false\n" %
            (os.getuid(), os.getgid()))
        (directory / "group").write_text("client:x:%d:\n" % os.getgid())
        initialize_only(command(binary, directory))
        compatibility_probe(binary, directory)
        offline_native_account(command(binary, directory, native_account=True))
        fixture_native_quota(binary, directory)
        offline_turn(turn_command(binary, directory))


def main(argv=None):
    if (sys.argv[1:] if argv is None else list(argv)) != ["--allow-disposable-codex-config-test"]:
        print("CI CODEX CONFIG: REFUSED (explicit disposable-CI flag required)")
        return 2
    try:
        run()
    except StartupDenied as error:
        print(str(error))
        return 2
    except (asset.AssetDenied, OSError, ValueError, subprocess.SubprocessError):
        print("CI CODEX CONFIG: FAIL reason=preflight")
        return 2
    print("CI CODEX CONFIG: PASS pinned=0.166.0 strict-config=PASS initialize=PASS system-compatibility=PASS network=OFF auth=NONE native-account=signed-out native-quota=31%/47%(loopback-fixture) turn=PASS(offline, stops at network) inference=NOT_RUN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
