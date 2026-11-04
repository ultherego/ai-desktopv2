"""Model choices reported by the pinned Codex client itself, read in a closed sandbox.

The catalog is the client's own (`model/list` of its app-server), so it follows the client instead of a list baked into
this program. The query needs no account: the client runs with an empty private HOME, every namespace unshared (no
network), no credentials, no user data and no tools, and is killed as soon as it answers. The result is a convenience for
menus, never authority: a chosen id is still shape-checked and the client alone decides whether it accepts it.
"""
import json
import os
import selectors
import signal
import subprocess
import sys
import time
import unicodedata

from .analysis_contract import AnalysisDenied
from .analysis_sandbox import INSTALL, trusted_file
from .execution import CODEX_MODEL, MODELS

BINARY = INSTALL / "codex"
MAX_ENTRIES = 64
MAX_LABEL = 64
MAX_OUTPUT = 256 * 1024
TIMEOUT = 20.0


class CatalogUnavailable(Exception):
    """No usable catalog; callers fall back to automatic selection and free-text ids."""


def command(binary):
    return ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib", "/lib64",
            "--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--dir", "/tmp/home/.codex", "--setenv", "HOME", "/tmp/home", "--setenv", "CODEX_HOME", "/tmp/home/.codex",
            "--setenv", "PATH", "/usr/bin", "--setenv", "LANG", "C.UTF-8", str(binary), "app-server"]


REQUESTS = b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in (
    {"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "AI Desktop", "title": "AI Desktop",
                                                                  "version": "catalog"}}},
    {"method": "initialized", "params": {}},
    {"id": 1, "method": "model/list", "params": {"limit": MAX_ENTRIES, "includeHidden": False}}))


def _label(value, fallback):
    if (type(value) is not str or not value.strip() or len(value) > MAX_LABEL
            or any(unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp") for char in value)):
        return fallback
    return value.strip()


def parse(response):
    """Validate one `model/list` result into ((id, label), ...). Anything unexpected makes the catalog unavailable."""
    data = response.get("data") if type(response) is dict else None
    if type(data) is not list or not data:
        raise CatalogUnavailable()
    seen, entries = set(), []
    for item in data[:MAX_ENTRIES]:
        if type(item) is not dict:
            raise CatalogUnavailable()
        ident = item.get("id")
        if (type(ident) is not str or not CODEX_MODEL.fullmatch(ident) or ident in MODELS or ident in seen
                or item.get("hidden") is True):
            continue
        seen.add(ident)
        entries.append((ident, _label(item.get("displayName"), ident)))
    if not entries:
        raise CatalogUnavailable()
    return tuple(entries)


def _exchange(argv, timeout):
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env={"PATH": "/usr/bin", "LANG": "C.UTF-8"}, start_new_session=True)
    try:
        process.stdin.write(REQUESTS)
        process.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        buffer, deadline = b"", time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not selector.select(left):
                raise CatalogUnavailable()
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise CatalogUnavailable()
            buffer += chunk
            if len(buffer) > MAX_OUTPUT:
                raise CatalogUnavailable()
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                try:
                    value = json.loads(line)
                except ValueError:
                    raise CatalogUnavailable() from None
                if type(value) is dict and value.get("id") == 1:
                    if "result" not in value:
                        raise CatalogUnavailable()
                    return value["result"]
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.stdout.close()
        process.stdin.close()
        process.wait()


def fetch(*, binary=BINARY, run=_exchange, timeout=TIMEOUT):
    """The client's model choices, or CatalogUnavailable. Never raises anything else."""
    if run is _exchange and (os.environ.get("AI_DESKTOP_MODEL_CATALOG") == "off"
                             or "unittest" in sys.modules and os.environ.get("AI_DESKTOP_MODEL_CATALOG") != "on"):
        raise CatalogUnavailable()          # tests and CI must never start the real client
    try:
        trusted_file(binary)
        if not os.access(binary, os.X_OK):
            raise CatalogUnavailable()
        return parse(run(command(binary), timeout))
    except CatalogUnavailable:
        raise
    except (AnalysisDenied, OSError, ValueError, TypeError, KeyError, RecursionError):
        raise CatalogUnavailable() from None
