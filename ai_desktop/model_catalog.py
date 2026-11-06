"""Model choices reported by the system Codex client itself, read in a closed sandbox.

The catalog is the client's own (`model/list` of its app-server), so it follows the client instead of a list baked into
this program. The query needs no account: the client runs with an empty private HOME, every namespace unshared (no
network), no credentials, no user data and no tools, and is killed as soon as it answers. The result is a convenience for
menus, never authority: a chosen id is still shape-checked and the client alone decides whether it accepts it.
"""
import json
import os
import math
from pathlib import Path
import tempfile
import time
import sys
import unicodedata

from .analysis_contract import AnalysisDenied
from .analysis_sandbox import _identity_files
from . import client_binding, codex_runtime
from .process_io import bounded_process
from .execution import CODEX_MODEL, MODELS

MAX_ENTRIES = 64
MAX_LABEL = 64
MAX_OUTPUT = 256 * 1024
TIMEOUT = 20.0


class CatalogUnavailable(Exception):
    """No usable catalog; callers fall back to automatic selection and free-text ids."""


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


def fetch(*, run=None, timeout=TIMEOUT):
    """Read a compatible bound system client's catalog, or CatalogUnavailable."""
    if run is None and (os.environ.get("AI_DESKTOP_MODEL_CATALOG") == "off"
                         or "unittest" in sys.modules and os.environ.get("AI_DESKTOP_MODEL_CATALOG") != "on"):
        raise CatalogUnavailable()          # tests and CI must never start the real client
    try:
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= TIMEOUT):
            raise CatalogUnavailable()
        codex_runtime.runtime_ready()
        with client_binding.bind("codex") as binding, tempfile.TemporaryDirectory(prefix="ai-desktop-models-") as temp:
            # The protocol budget starts after the initial immutable binding.
            # Filesystem operations themselves cannot be interrupted here.
            deadline = time.monotonic() + timeout
            def remaining():
                value = deadline - time.monotonic()
                if value <= 0:
                    raise CatalogUnavailable()
                return value
            directory = Path(temp)
            _identity_files(directory)
            codex_runtime.compatibility(directory, binding, timeout=remaining())
            command = codex_runtime.namespace_command("catalog", directory, binding=binding)
            code, output, errors = (run or bounded_process)(command,
                env={"PATH": "/usr/bin", "LANG": "C.UTF-8"}, pass_fds=binding.pass_fds,
                timeout=remaining(), limit=MAX_OUTPUT)
            if code:
                raise CatalogUnavailable()
            binding.recheck()
            result = parse(json.loads(output))
            remaining()
            return result
    except CatalogUnavailable:
        raise
    except (AnalysisDenied, OSError, ValueError, TypeError, KeyError, RecursionError):
        raise CatalogUnavailable() from None
