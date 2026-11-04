"""E2 outbound TLS perimeter: fixed Anthropic host, public-IP pinning, no credentials.

Only a HEAD connectivity probe is implemented. There is intentionally no
POST/model invocation, OAuth loader, arbitrary URL or proxy support.
Outbound calls are OFF unless a trusted caller explicitly opts in.
"""
import http.client
import ipaddress
import os
import socket
import ssl
import sys

HOST = "api.anthropic.com"
PATH = "/"
TIMEOUT_SECONDS = 5
MAX_REPLY_BYTES = 1024


class EgressDenied(RuntimeError):
    """Constant, non-sensitive transport refusal."""


def _permitted_addresses(host, resolver=socket.getaddrinfo):
    if type(host) is not str or host != HOST:
        raise EgressDenied("EGRESS: REFUSED")
    try:
        answers = resolver(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        if not answers or len(answers) > 32:
            raise ValueError("no bounded DNS results")
        for family, kind, proto, _, addr in answers:
            if family not in (socket.AF_INET, socket.AF_INET6):
                raise ValueError("unsupported family")
            if kind != socket.SOCK_STREAM or proto != socket.IPPROTO_TCP:
                raise ValueError("unsupported protocol")
            expected_parts = 2 if family == socket.AF_INET else 4
            if type(addr) is not tuple or len(addr) != expected_parts or addr[1] != 443:
                raise ValueError("wrong port")
            if family == socket.AF_INET6 and addr[2:] != (0, 0):
                raise ValueError("unexpected IPv6 scope")
            ip = ipaddress.ip_address(addr[0])
            if not ip.is_global or ip.is_multicast or ip.is_unspecified:
                raise ValueError("non-public target")
        # No second DNS lookup: socket.connect() uses this vetted sockaddr.
        return answers
    except (OSError, ValueError, TypeError, IndexError):
        raise EgressDenied("EGRESS: REFUSED") from None


class _PinnedTLS(http.client.HTTPSConnection):
    """Connect to a pre-vetted IP, then authenticate the fixed hostname via TLS."""

    def connect(self):
        destinations = _permitted_addresses(self.host)
        family, kind, proto, _, sockaddr = destinations[0]
        raw = None
        try:
            raw = socket.socket(family, kind, proto)
            raw.settimeout(TIMEOUT_SECONDS)
            raw.connect(sockaddr)
            # Context validates the chain and hostname; no TLS downgrade.
            self.sock = self._context.wrap_socket(raw, server_hostname=HOST)
        except (OSError, ssl.SSLError, ValueError):
            if raw is not None:
                raw.close()
            raise EgressDenied("EGRESS: REFUSED") from None


def probe(*, allow_network=False, connection_factory=_PinnedTLS):
    """Explicit, credential-free connectivity probe to a fixed HTTPS endpoint.

    There is no model request, OAuth operation, secret or user report sent.
    Expected 4xx statuses do not count as an auth/model success.
    """
    if allow_network is not True:
        raise EgressDenied("EGRESS: REFUSED")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise EgressDenied("EGRESS: REFUSED")
    conn = None
    try:
        conn = connection_factory(HOST, port=443, timeout=TIMEOUT_SECONDS,
                                  context=ssl.create_default_context())
        conn.request("HEAD", PATH, headers={
            "Host": HOST, "User-Agent": "ai-desktop-egress-probe/1",
            "Connection": "close",
        })
        response = conn.getresponse()
        if type(response.status) is not int or not 200 <= response.status < 500:
            raise EgressDenied("EGRESS: REFUSED")
        if 300 <= response.status < 400:
            raise EgressDenied("EGRESS: REFUSED")
        # No redirect follow-up. HEAD carries no report/token; no response body.
        if response.read(MAX_REPLY_BYTES + 1):
            raise EgressDenied("EGRESS: REFUSED")
        return {"tls": "VERIFIED", "endpoint": HOST, "authentication": "NOT_TESTED"}
    except (OSError, ValueError, TypeError, http.client.HTTPException, EgressDenied):
        raise EgressDenied("EGRESS: REFUSED") from None
    finally:
        if conn is not None:
            conn.close()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    # Never expose --allow-network through a CLI. This code does not contact
    # a provider unless an explicit trusted integration is implemented later.
    if args == ["status"]:
        print("EGRESS: OFFLINE (network and OAuth disabled)")
        return 0
    print("EGRESS: REFUSED")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
