"""Real TLS/proxy/namespace acceptance with disposable synthetic data only.

The outer bwrap has no uplink, host HOME, secrets, sockets or writable host bind.
The inner bwrap is the candidate client boundary; the proxy and TLS fixture live
OUTSIDE its PID, mount and net namespaces. All test connections are local to the
outer disposable namespace. Never import this as a production transport.
"""
import contextlib
import ctypes
import errno
import http.client
import json
import os
from pathlib import Path
import socket
import socketserver
import ssl
import subprocess
import sys
import threading

sys.path.insert(0, "/opt")
from ai_desktop.e2_connect_proxy import ConnectProxy, HOSTS, relay
from ai_desktop.e2_subscription_boundary import CHECKS, MODULE_FILES, PHASES, base_command, userns_refused, userns_probe_passed, privileges_locked
from ai_desktop.e2_compat_fixture import bounded_run

PROFILE_MARKER = "SYNTHETIC_PROFILE_NOT_OAUTH"
DATA = b"SYNTHETIC_APPROVED_REPORT"
LAST_PHASE = "metadata"


@contextlib.contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class TLSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            with self.server.context.wrap_socket(self.request, server_side=True) as conn:
                raw = bytearray()
                while not raw.endswith(b"\r\n\r\n") and len(raw) <= 8192:
                    part = conn.recv(1)
                    if not part:
                        return
                    raw.extend(part)
                if raw.startswith(b"GET /redirect "):
                    response = b"HTTP/1.1 302 Found\r\nLocation: https://forbidden.invalid/\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                else:
                    response = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(DATA)).encode() + b"\r\nConnection: close\r\n\r\n" + DATA
                conn.sendall(response)
        except (OSError, ValueError):
            pass


class TLSServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as remote:
                remote.settimeout(3)
                remote.connect("/run/egress.sock")
                relay(self.request, remote)
        except (OSError, ValueError):
            pass


class Bridge(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def locked_privileges():
    # The kernel's status file reports the *current* process capabilities.
    # This is not inferred from argv, and no raw status is exported.
    return privileges_locked(Path("/proc/self/status").read_text(encoding="ascii"))


def denied_connect(family, addr, kind=socket.SOCK_STREAM):
    try:
        with socket.socket(family, kind) as conn:
            conn.settimeout(.25)
            if kind == socket.SOCK_DGRAM:
                conn.sendto(b"SYNTHETIC_DNS", addr)
                conn.recv(64)
            else:
                conn.connect(addr)
        return False
    except OSError:
        return True


def tunnel(authority="api.anthropic.com:443", bridge=False):
    if bridge:
        conn = socket.create_connection(("127.0.0.1", 18080), timeout=2)
    else:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(2)
        conn.connect("/run/egress.sock")
    conn.sendall(("CONNECT " + authority + " HTTP/1.1\r\nHost: " + authority + "\r\n\r\n").encode())
    header = bytearray()
    while not header.endswith(b"\r\n\r\n") and len(header) < 8192:
        part = conn.recv(1)
        if not part:
            break
        header.extend(part)
    if bytes(header) != b"HTTP/1.1 200 Connection Established\r\n\r\n":
        conn.close()
        raise OSError("refused")
    return conn


def request(*, sni="api.anthropic.com", trusted=True, path="/", bridge=False):
    context = ssl.create_default_context(cafile="/etc/test-ca.pem" if trusted else None)
    with tunnel(bridge=bridge) as plain:
        with context.wrap_socket(plain, server_hostname=sni) as conn:
            conn.sendall(("GET " + path + " HTTP/1.1\r\nHost: " + sni + "\r\nConnection: close\r\n\r\n").encode())
            response = http.client.HTTPResponse(conn)
            response.begin()
            return response.status, response.getheader("Location"), response.read(1024)


def inaccessible(path):
    try:
        with open(path, "rb") as stream:
            stream.read(1)
        return False
    except OSError:
        return True


def inner():
    global LAST_PHASE
    metadata = json.loads(Path("/etc/test-meta.json").read_text())
    if os.readlink("/proc/self/ns/net") == metadata["outer_net"]:
        return 1
    with serving(Bridge(("127.0.0.1", 18080), BridgeHandler)):
        checks = {"client_privileges": locked_privileges()}
        LAST_PHASE = "approved-tls"
        checks["approved_tls"] = request(bridge=True) == (200, None, DATA)
        LAST_PHASE = "profile"
        profile = Path(os.environ["CLAUDE_CONFIG_DIR"])
        checks["profile_visible"] = (profile / ".credentials.json").read_text() == PROFILE_MARKER
        (profile / "refresh.tmp").write_text("SYNTHETIC_REFRESH")
        os.replace(profile / "refresh.tmp", profile / ".credentials.json")
        checks["profile_refresh_replace"] = (profile / ".credentials.json").read_text() == "SYNTHETIC_REFRESH"
        LAST_PHASE = "host-denials"
        checks["host_home"] = inaccessible("/home/host/private")
        checks["host_secret"] = inaccessible("/lab/host-secret") and inaccessible("/home/host/.claude/.credentials.json")
        checks["host_process"] = (os.readlink("/proc/self/ns/pid") != metadata["outer_pidns"] and
                                  inaccessible("/proc/1/root/lab/host-secret"))
        checks["host_socket"] = denied_connect(socket.AF_UNIX, "/lab/host.sock")
        checks["abstract_socket"] = denied_connect(socket.AF_UNIX, "\0ai-desktop-e2-synthetic")
        checks["host_loopback"] = denied_connect(socket.AF_INET, ("127.0.0.1", metadata["tls_port"]))
        checks["direct_ipv4"] = denied_connect(socket.AF_INET, ("192.0.2.1", 443))
        checks["direct_ipv6"] = denied_connect(socket.AF_INET6, ("2001:db8::1", 443))
        checks["dns_udp"] = denied_connect(socket.AF_INET, ("192.0.2.53", 53), socket.SOCK_DGRAM)
        checks["dns_tcp"] = denied_connect(socket.AF_INET, ("192.0.2.53", 53))
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "*"
        checks["proxy_env_bypass"] = denied_connect(socket.AF_INET, ("127.0.0.1", metadata["tls_port"]))
        LAST_PHASE = "transport-denials"
        for key, authority in (("unknown_host", "forbidden.invalid:443"), ("private_ip", "127.0.0.1:443"),
                               ("metadata_ip", "169.254.169.254:443"), ("wrong_port", "api.anthropic.com:80")):
            try:
                with tunnel(authority):
                    checks[key] = False
            except OSError:
                checks[key] = True
        with tunnel() as conn:
            conn.sendall(b"GET / HTTP/1.1\r\n\r\n")
            try:
                checks["plaintext"] = conn.recv(1) == b""
            except OSError:
                checks["plaintext"] = True
        for key, kwargs in (("wrong_sni", {"sni": "forbidden.invalid"}), ("bad_certificate", {"trusted": False})):
            try:
                request(**kwargs)
                checks[key] = False
            except (OSError, http.client.HTTPException):
                checks[key] = True
        LAST_PHASE = "redirect"
        status, location, _ = request(path="/redirect")
        try:
            with tunnel("forbidden.invalid:443"):
                checks["redirect_escape"] = False
        except OSError:
            checks["redirect_escape"] = status == 302 and location == "https://forbidden.invalid/"
        LAST_PHASE = "filesystem"
        (profile / "escape").symlink_to("/lab/host-secret")
        checks["profile_symlink_escape"] = inaccessible(profile / "escape")
        try:
            Path("/work/approved.txt").write_text("REPLACE")
            checks["readonly_input"] = False
        except OSError:
            checks["readonly_input"] = Path("/work/approved.txt").read_bytes() == DATA
        # Hostile code can execute inside the jail. Its host effects must fail.
        attempt = subprocess.run(["/bin/sh", "-c", "printf BAD > /lab/host-secret"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=2)
        checks["host_tool_effect"] = attempt.returncode != 0
    # The bridge uses transient threads. A negative CLONE_NEWUSER result in
    # that process could be EINVAL solely from multithreading, not isolation.
    # Test a fresh single-threaded interpreter INSIDE the same bwrap jail,
    # with no writable host mounts, shell, secrets, or alternate network.
    LAST_PHASE = "nested-namespace"
    probe = subprocess.run(
        ["/usr/bin/python3", "-I", "-B", "/opt/fixture.py", "userns-probe"],
        stdin=subprocess.DEVNULL, capture_output=True, check=False,
        env={"PATH": "/usr/bin"}, timeout=5,
    )
    checks["nested_namespace"] = userns_probe_passed(
        probe.returncode, probe.stdout, probe.stderr)
    if not checks["nested_namespace"]:
        # Only fixed synthetic markers are admitted. All other output is
        # suppressed; any unexpected result remains a hard failure.
        status = ("allowed" if probe.stdout == b"USERNS_ALLOWED\n" else
                  "multithreaded" if probe.stdout == b"USERNS_MULTITHREADED\n"
                  else "other")
        print(json.dumps({"failure": "userns-probe", "result": status}))
        return 1
    if set(checks) != set(CHECKS) - {"proxy_failure"}:
        return 1
    print(json.dumps({"schema": 1, "checks": checks, "real_client": False, "real_auth": False, "upstream": False}))
    return 0 if all(checks.values()) else 1


def inner_command():
    """Fixed inner argv, kept inspectable without starting either namespace."""
    command = base_command() + ["--disable-userns", "--assert-userns-disabled",
        "--setenv", "CLAUDE_CONFIG_DIR", "/home/client/profile",
        "--setenv", "HTTPS_PROXY", "http://127.0.0.1:18080", "--dir", "/run",
        "--ro-bind", "/lab/proxy.sock", "/run/egress.sock",
        "--bind", "/lab/profile", "/home/client/profile",
        "--ro-bind", "/lab/approved.txt", "/work/approved.txt",
        "--ro-bind", "/lab/ca.pem", "/etc/test-ca.pem",
        "--ro-bind", "/lab/meta.json", "/etc/test-meta.json",
        "--ro-bind", "/opt/fixture.py", "/opt/fixture.py"]
    for name in MODULE_FILES:
        command += ["--ro-bind", "/opt/ai_desktop/" + name, "/opt/ai_desktop/" + name]
    return command + ["--chdir", "/work", "/usr/bin/python3", "-I", "-B", "/opt/fixture.py", "inner"]


def outer():
    # This fixture cannot start networking outside the closed outer namespace.
    if {name for _, name in socket.if_nameindex()} != {"lo"} or Path("/etc/resolv.conf").exists():
        print(json.dumps({"failure": "outer-network"}))
        return 1
    if not locked_privileges():
        print(json.dumps({"failure": "outer-privileges"}))
        return 1
    Path("/lab/profile").mkdir(parents=True, mode=0o700)
    Path("/home/host/.claude").mkdir(parents=True)
    Path("/home/host/private").write_text("HOST_HOME_SENTINEL")
    Path("/home/host/.claude/.credentials.json").write_text("HOST_PROFILE_SENTINEL")
    Path("/lab/host-secret").write_text("HOST_SECRET_SENTINEL")
    Path("/lab/profile/.credentials.json").write_text(PROFILE_MARKER)
    Path("/lab/approved.txt").write_bytes(DATA)
    # Fresh private key only for localhost TLS fixture; never an OAuth token.
    cert = subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                           "-keyout", "/lab/key.pem", "-out", "/lab/ca.pem", "-days", "1",
                           "-subj", "/CN=api.anthropic.com", "-addext", "subjectAltName=DNS:api.anthropic.com"],
                          env={"PATH": "/usr/bin", "OPENSSL_CONF": "/dev/null"},
                          capture_output=True, timeout=10)
    if cert.returncode:
        print(json.dumps({"failure": "certificate"}))
        return 1
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/lab/ca.pem", "/lab/key.pem")
    with contextlib.ExitStack() as stack:
        tls = TLSServer(("127.0.0.1", 0), TLSHandler)
        tls.context = context
        stack.enter_context(serving(tls))
        host_socket = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        host_socket.bind("/lab/host.sock")
        host_socket.listen(1)
        abstract = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        abstract.bind("\0ai-desktop-e2-synthetic")
        abstract.listen(1)
        dialed = []

        def dial_fixture(host):
            if host not in HOSTS:
                raise ValueError()
            dialed.append(host)
            return socket.create_connection(tls.server_address, timeout=2)

        proxy = ConnectProxy("/lab/proxy.sock", dial=dial_fixture)
        proxy_context = serving(proxy)
        proxy_context.__enter__()
        stack.callback(proxy_context.__exit__, None, None, None)
        meta = {"outer_net": os.readlink("/proc/self/ns/net"), "outer_pidns": os.readlink("/proc/self/ns/pid"),
                "tls_port": tls.server_address[1]}
        Path("/lab/meta.json").write_text(json.dumps(meta))
        command = inner_command()
        code, out, error = bounded_run(command, env={"PATH": "/usr/bin"}, timeout=30, limit=8192)
        if code != 0:
            # Only allowlisted synthetic check names/phase identifiers cross
            # this boundary; never forward raw stderr, stdout or exceptions.
            failure = {"failure": "inner-namespace" if b"bwrap:" in error else "inner-checks"}
            if failure["failure"] == "inner-checks":
                try:
                    detail = json.loads(out)
                    if (type(detail) is dict and set(detail) == {"failure", "result"}
                            and detail["failure"] == "userns-probe"
                            and type(detail["result"]) is str
                            and detail["result"] in {"allowed", "invalid", "other", "multithreaded", "limit"}):
                        failure = detail
                    elif (type(detail) is dict and set(detail) == {"failure", "phase"}
                            and detail["failure"] == "fixture-exception"
                            and type(detail["phase"]) is str and detail["phase"] in PHASES):
                        failure = detail
                    elif type(detail) is dict and set(detail) == {
                            "schema", "checks", "real_client", "real_auth", "upstream"}:
                        checks = detail["checks"]
                        if (detail["schema"] == 1 and type(detail["schema"]) is int
                                and detail["real_client"] is False
                                and detail["real_auth"] is False and detail["upstream"] is False
                                and type(checks) is dict and set(checks) == set(CHECKS) - {"proxy_failure"}
                                and all(type(value) is bool for value in checks.values())):
                            failed = sorted(k for k, value in checks.items() if value is False)
                            if failed:
                                failure["failed_checks"] = failed
                except (ValueError, TypeError, UnicodeError):
                    pass
            print(json.dumps(failure))
            return 1
        if Path("/lab/host-secret").read_text() != "HOST_SECRET_SENTINEL":
            return 1
        if not dialed or set(dialed) != {"api.anthropic.com"}:
            return 1
        data = json.loads(out)
        proxy_context.__exit__(None, None, None)
        # The mounted UNIX socket still exists, but the real external service is
        # now stopped. Re-enter the SAME client boundary, no alternative routing.
        down_code, down_out, _ = bounded_run(command[:-1] + ["proxy-down"],
                                             env={"PATH": "/usr/bin"}, timeout=8, limit=1024)
        data["checks"]["proxy_failure"] = down_code == 0 and down_out == b"PROXY_DOWN_DENIED\n"
        print(json.dumps(data))
        return 0 if all(data["checks"].values()) else 1


def userns_probe():
    """Fixed negative syscall probe; invoked only inside the inner namespace."""
    thread_count = len(list(Path("/proc/self/task").iterdir()))
    if threading.active_count() != 1 or thread_count != 1:
        print("USERNS_MULTITHREADED")
        return 1
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.unshare(0x10000000)  # CLONE_NEWUSER
    if userns_refused(result, ctypes.get_errno(), thread_count=thread_count):
        print("USERNS_DENIED")
        return 0
    print("USERNS_ALLOWED" if result == 0 else "USERNS_UNEXPECTED")
    return 1


def proxy_down():
    with serving(Bridge(("127.0.0.1", 18080), BridgeHandler)):
        try:
            request(bridge=True)
        except OSError:
            if denied_connect(socket.AF_INET, ("192.0.2.1", 443)):
                print("PROXY_DOWN_DENIED")
                return 0
    return 1


if __name__ == "__main__":
    try:
        result = (outer() if sys.argv[1:] == ["outer"]
                  else inner() if sys.argv[1:] == ["inner"]
                  else proxy_down() if sys.argv[1:] == ["proxy-down"]
                  else userns_probe() if sys.argv[1:] == ["userns-probe"]
                  else 2)
    except Exception:
        # Synthetic fixture only; no raw exception data or user-specific paths.
        failure = {"failure": "fixture-exception"}
        if sys.argv[1:] == ["inner"] and LAST_PHASE in PHASES:
            failure["phase"] = LAST_PHASE
        print(json.dumps(failure))
        result = 1
    raise SystemExit(result)
