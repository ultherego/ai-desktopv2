"""Synthetic untrusted model process. Never invokes Claude or reads credentials.

This is an E2 boundary test process, not a model implementation.
"""
import hashlib
import json
import os
from pathlib import Path
import sys

from .report_policy import MAX_REPORT_BYTES, validate_report


def _pairs_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _probe():
    try:
        host_home = os.environ["HOST_HOME"]
        host_netns = os.environ["HOST_NETNS"]
        passed = all((
            Path.home() == Path("/home/model"),
            not Path(host_home).exists(),
            not Path("/etc/passwd").exists(),
            not Path("/home/model/.claude").exists(),
            not Path("/home/model/.ssh").exists(),
            os.readlink("/proc/self/ns/net") != host_netns,
            bool(os.statvfs("/usr").f_flag & os.ST_RDONLY),
            not any(key in os.environ for key in (
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CONFIG_DIR",
                "CLAUDE_CODE_OAUTH_TOKEN", "AWS_ACCESS_KEY_ID", "SSH_AUTH_SOCK",
                "DBUS_SESSION_BUS_ADDRESS", "HTTP_PROXY", "HTTPS_PROXY"
            )),
        ))
    except (KeyError, OSError):
        passed = False
    print(json.dumps({"probe": "PASS" if passed else "FAIL"}))
    return 0 if passed else 1


def _inspect():
    try:
        raw = sys.stdin.buffer.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError("oversized")
        report = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")),
        )
        validate_report(report)
    except (ValueError, TypeError, UnicodeError):
        print('{"error":"rejected"}')
        return 1
    # Output cannot contain actual report values or instructions.
    print(json.dumps({"sha256": hashlib.sha256(raw).hexdigest(),
                      "field_count": len(report)}, sort_keys=True))
    return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args == ["--probe"]:
        return _probe()
    if args == ["--inspect"]:
        return _inspect()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
