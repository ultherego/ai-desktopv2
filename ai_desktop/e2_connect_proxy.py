"""Candidate A+ transport boundary: CONNECT + TLS, never an OAuth broker.

No CLI, credential handling, TLS interception or automatic activation. Only the
synthetic namespace acceptance harness starts this server in the current release.
The client must verify the server certificate; this proxy pins the destination
and checks ClientHello SNI, not encrypted HTTP paths, payloads or redirects.
"""
import ipaddress
import selectors
import socket
import socketserver
import threading
import time

HOSTS = frozenset({"api.anthropic.com", "claude.ai", "platform.claude.com"})
OPENAI_HOSTS = frozenset({"api.openai.com"})
# Separate native account RPC namespace. Device auth/token renewal and account limits.
CODEX_ACCOUNT_HOSTS = frozenset({"auth.openai.com", "chatgpt.com"})
HEADER_LIMIT = 8192
HELLO_LIMIT = 65536
STREAM_LIMIT = 4 * 1024 * 1024
# Analysis carries JSON inside the native client's HTTPS protocol. This
# standalone transport is also mounted in the minimal namespace fixture: do
# not import the controller's contract here. A regression checks this ceiling
# against two request/response envelopes plus TLS overhead. Account operations
# keep the smaller default; neither limit comes from model data.
ANALYSIS_STREAM_LIMIT = 96 * 1024 * 1024
TIME_LIMIT = 20
CONNECTION_LIMIT = 64


class Denied(ValueError):
    def __init__(self):
        super().__init__("E2 CONNECT: REFUSED")


def checked_stream_limit(value):
    if type(value) is not int or value not in (STREAM_LIMIT, ANALYSIS_STREAM_LIMIT):
        raise Denied()
    return value


def _fixed_hosts(hosts):
    # Only these compiled policies are possible. This parameter never comes from
    # a request, profile, environment variable or model output.
    if type(hosts) is not frozenset or hosts not in (HOSTS, OPENAI_HOSTS, CODEX_ACCOUNT_HOSTS):
        raise Denied()
    return hosts


def connect_host(raw, *, hosts=HOSTS):
    """Parse a deliberately small CONNECT grammar; nothing is forwarded."""
    hosts = _fixed_hosts(hosts)
    if not raw.endswith(b"\r\n\r\n") or len(raw) > HEADER_LIMIT:
        raise Denied()
    try:
        lines = raw.decode("ascii").split("\r\n")
        method, authority, version = lines[0].split(" ")
        if method != "CONNECT" or version != "HTTP/1.1":
            raise Denied()
        if authority not in {host + ":443" for host in hosts}:
            raise Denied()
        headers = {}
        for line in lines[1:-2]:
            name, value = line.split(":", 1)
            if name != name.strip() or not name or any(ord(c) < 32 for c in line):
                raise Denied()
            name, value = name.lower(), value.strip(" ")
            if name in headers or name not in {"host", "proxy-connection", "connection", "user-agent"}:
                raise Denied()
            headers[name] = value
        if headers.get("host") != authority:
            raise Denied()
        return authority[:-4]
    except (ValueError, UnicodeError):
        raise Denied() from None


def read_header(stream, *, hosts=HOSTS):
    # Do not consume TLS bytes following the header (also handles fragmentation).
    deadline = time.monotonic() + 3
    raw = bytearray()
    while not raw.endswith(b"\r\n\r\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Denied()
        stream.settimeout(remaining)
        part = stream.recv(1)
        if not part or len(raw) >= HEADER_LIMIT:
            raise Denied()
        raw.extend(part)
    return connect_host(bytes(raw), hosts=hosts)


def public_addresses(host, resolver=None, *, hosts=HOSTS):
    if host not in _fixed_hosts(hosts):
        raise Denied()
    resolver = resolver or socket.getaddrinfo
    try:
        answers = resolver(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        if not answers or len(answers) > 32:
            raise Denied()
        for family, kind, proto, _, addr in answers:
            if family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM or proto != socket.IPPROTO_TCP:
                raise Denied()
            if len(addr) != (2 if family == socket.AF_INET else 4) or addr[1] != 443:
                raise Denied()
            ip = ipaddress.ip_address(addr[0])
            if (ip.version != (4 if family == socket.AF_INET else 6) or
                    not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                raise Denied()
            if family == socket.AF_INET6 and (
                    addr[2:] != (0, 0) or ip.ipv4_mapped or ip.sixtofour or ip.teredo or
                    ip in ipaddress.ip_network("64:ff9b::/96")):
                raise Denied()
        return answers
    except (OSError, ValueError, TypeError, IndexError):
        raise Denied() from None


def open_public(host, *, hosts=HOSTS):
    """Resolve once, validate ALL results, connect using a numeric sockaddr."""
    family, kind, proto, _, addr = public_addresses(host, hosts=hosts)[0]
    stream = socket.socket(family, kind, proto)
    try:
        stream.settimeout(3)
        stream.connect(addr)
        return stream
    except OSError:
        stream.close()
        raise Denied() from None


def _exact(stream, count, deadline):
    out = bytearray()
    while len(out) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Denied()
        stream.settimeout(remaining)
        data = stream.recv(count - len(out))
        if not data:
            raise Denied()
        out.extend(data)
    return bytes(out)


def _vector(data, pos, width):
    if pos + width > len(data):
        raise Denied()
    size = int.from_bytes(data[pos:pos + width], "big")
    start, end = pos + width, pos + width + size
    if end > len(data):
        raise Denied()
    return data[start:end], end


def check_hello(body, host):
    """Check a complete TLS ClientHello; reject hidden/ambiguous destinations."""
    if len(body) < 34 or body[:2] != b"\x03\x03":
        raise Denied()
    session, pos = _vector(body, 34, 1)
    ciphers, pos = _vector(body, pos, 2)
    compression, pos = _vector(body, pos, 1)
    extensions, pos = _vector(body, pos, 2)
    if len(session) > 32 or not ciphers or len(ciphers) % 2 or compression != b"\x00" or pos != len(body):
        raise Denied()
    seen, name, pos = set(), None, 0
    while pos < len(extensions):
        if pos + 4 > len(extensions):
            raise Denied()
        kind = int.from_bytes(extensions[pos:pos + 2], "big")
        value, pos = _vector(extensions, pos + 2, 2)
        if kind in seen or kind == 0xfe0d:  # ECH defeats plaintext SNI checking.
            raise Denied()
        seen.add(kind)
        if kind == 0:
            names, end = _vector(value, 0, 2)
            if end != len(value) or not names or names[0] != 0:
                raise Denied()
            name, end = _vector(names, 1, 2)
            if end != len(names):
                raise Denied()
    if name != host.encode("ascii"):
        raise Denied()


def read_hello(stream, host):
    deadline = time.monotonic() + 3
    wire, handshake = bytearray(), bytearray()
    for _ in range(16):
        header = _exact(stream, 5, deadline)
        size = int.from_bytes(header[3:], "big")
        if header[0] != 22 or header[1:3] not in (b"\x03\x01", b"\x03\x03") or not 0 < size <= 16384:
            raise Denied()
        if len(wire) + size + 5 > HELLO_LIMIT:
            raise Denied()
        record = _exact(stream, size, deadline)
        wire.extend(header + record)
        handshake.extend(record)
        if len(handshake) >= 4:
            length = int.from_bytes(handshake[1:4], "big")
            if handshake[0] != 1 or length > HELLO_LIMIT - 4:
                raise Denied()
            if len(handshake) >= length + 4:
                if len(handshake) != length + 4:
                    raise Denied()
                check_hello(bytes(handshake[4:]), host)
                return bytes(wire)
    raise Denied()


def relay(left, right, *, seconds=TIME_LIMIT, limit=STREAM_LIMIT):
    """Bounded bidirectional stream; buffers, total traffic and lifetime capped."""
    deadline, total = time.monotonic() + seconds, 0
    for stream in (left, right):
        stream.setblocking(False)
    pending = {left: bytearray(), right: bytearray()}
    eof = set()
    with selectors.DefaultSelector() as selector:
        while len(eof) != 2 or any(pending.values()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Denied()
            for stream, peer in ((left, right), (right, left)):
                events = 0
                if stream not in eof and len(pending[peer]) < 65536:
                    events |= selectors.EVENT_READ
                if pending[stream]:
                    events |= selectors.EVENT_WRITE
                if stream in [key.fileobj for key in selector.get_map().values()]:
                    selector.unregister(stream)
                if events:
                    selector.register(stream, events, peer)
            for key, events in selector.select(min(remaining, .2)):
                stream, peer = key.fileobj, key.data
                if events & selectors.EVENT_WRITE:
                    try:
                        sent = stream.send(pending[stream])
                    except BlockingIOError:
                        sent = 0
                    del pending[stream][:sent]
                    if not pending[stream] and peer in eof:
                        stream.shutdown(socket.SHUT_WR)
                if events & selectors.EVENT_READ:
                    try:
                        data = stream.recv(16384)
                    except BlockingIOError:
                        continue
                    total += len(data)
                    if total > limit:
                        raise Denied()
                    if data:
                        pending[peer].extend(data)
                    else:
                        eof.add(stream)
                        if not pending[peer]:
                            peer.shutdown(socket.SHUT_WR)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        remote = None
        try:
            self.request.settimeout(3)
            host = read_header(self.request, hosts=getattr(self.server, "hosts", HOSTS))
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            hello = read_hello(self.request, host)
            # Not even DNS occurs before the authority and SNI are accepted.
            remote = self.server.dial(host)
            remote.sendall(hello)
            relay(self.request, remote, seconds=self.server.stream_seconds,
                  limit=getattr(self.server, "stream_limit", STREAM_LIMIT))
        except (OSError, ValueError):
            # No request, hostname, TLS bytes, credential or exception text logs.
            pass
        finally:
            if remote is not None:
                remote.close()


class ConnectProxy(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, path, *, dial, stream_seconds=TIME_LIMIT, hosts=HOSTS, stream_limit=STREAM_LIMIT):
        if type(stream_seconds) is not int or not 1 <= stream_seconds <= 1200:
            raise Denied()
        self.stream_seconds = stream_seconds
        self.stream_limit = checked_stream_limit(stream_limit)
        self.hosts = _fixed_hosts(hosts)
        self.dial = dial  # Trusted policy, never accepted from sandbox data.
        self.remaining = CONNECTION_LIMIT
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(path, _Handler)

    def verify_request(self, request, client_address):
        if self.remaining <= 0 or not self.slots.acquire(blocking=False):
            return False
        self.remaining -= 1
        return True

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass
