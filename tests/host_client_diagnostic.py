"""Explicit Claude compatibility diagnosis on Arch/CachyOS, without credentials.

Runs only the installed application's fixed offline probes. Native output is
classified into closed fields; no native prose, JSON keys or paths are printed.
The separate Codex source summary is read-only metadata, never execution.
"""
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_desktop import analysis_sandbox as sandbox, client_compat, client_sources, system_clients
from ai_desktop.analysis_contract import AnalysisDenied, REASONS, STAGES
from ai_desktop.e2_ipc import _unique_pairs
import host_analysis_acceptance
from host_system_clients_acceptance import require_host


def emit(record):
    print(json.dumps(record, sort_keys=True, ensure_ascii=True), flush=True)


def codex_metadata():
    """Inspect only known launcher names; never traverse or read their files."""
    home = pwd.getpwuid(os.getuid()).pw_dir
    paths = [("user-local", home + "/.local/bin/codex"), ("usr-local", "/usr/local/bin/codex"),
             ("system", "/usr/bin/codex")]
    install = os.environ.get("CODEX_INSTALL_DIR", "")
    if install.startswith("/"):
        paths.append(("install-dir", install.rstrip("/") + "/codex"))
    codex_home = os.environ.get("CODEX_HOME") or home + "/.codex"
    paths.append(("standalone", codex_home.rstrip("/") + "/packages/standalone/current/bin/codex"))
    result = []
    for label, path in paths:
        record = {"kind": "source-path", "provider": "codex", "location": label}
        if (not path.startswith("/") or len(os.fsencode(path)) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in path)):
            result.append({**record, "status": "invalid-path"})
            continue
        record["path"] = path
        try:
            info = os.lstat(path)
            record.update(status="present", file_type="symlink" if stat.S_ISLNK(info.st_mode) else "file"
                          if stat.S_ISREG(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "other")
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                if len(os.fsencode(target)) <= 4096 and not any(ord(char) < 32 or ord(char) == 127 for char in target):
                    record["link_target"] = target
                else:
                    record["link_status"] = "invalid-target"
        except FileNotFoundError:
            record["status"] = "missing"
        except OSError:
            record["status"] = "unavailable"
        result.append(record)
    return result


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RecursionError):
        return None


def observation(action, value):
    """Summarize bounded bytes without ever returning provider-owned strings."""
    result = {"kind": "probe", "provider": "claude", "phase": action}
    if (type(value) is not tuple or len(value) != 3 or type(value[0]) is not int
            or any(type(item) is not bytes for item in value[1:])):
        return {**result, "reply": "invalid"}
    code, output, errors = value
    result.update(exit=code, stdout_bytes=len(output), stderr_bytes=len(errors))
    if len(output) + len(errors) > client_compat.MAX_OUTPUT:
        return {**result, "reply": "oversized"}
    data = _json(output)
    shape = "empty" if not output.strip() else "non-json"
    if data is not None:
        shape = "object" if type(data) is dict else "array" if type(data) is list else "scalar"
    elif output.strip() == b"null":
        shape = "scalar"
    elif 1 < len(lines := output.splitlines()) <= 128 and all(_json(line) is not None for line in lines):
        shape = "jsonl"
    fields = data if type(data) is dict else {}
    logged = ("missing" if "loggedIn" not in fields else "false" if fields["loggedIn"] is False
              else "true" if fields["loggedIn"] is True else "invalid")
    result.update(stdout_shape=shape, loggedIn=logged,
                  authMethod="missing" if "authMethod" not in fields else "none" if fields["authMethod"] == "none" else "other",
                  configDirectory="missing" if "configDirectory" not in fields else "expected"
                  if fields["configDirectory"] == sandbox.INNER_PROFILE else "other",
                  signed_out_matches=code == 1 and client_compat._claude_signed_out(output))
    flags = {value for mode in ("analyze", "web") for value in sandbox.client_command(mode)
             if re.fullmatch(r"--?[A-Za-z][A-Za-z0-9-]*", value)} | {
                 client_compat.INVALID_OPTION, client_compat.FIRST_INVALID_OPTION}
    unknown = re.search(rb"(?:unknown|unrecognized|unexpected|invalid) (?:option|argument)[:\s]+['\"]?(--?[A-Za-z][A-Za-z0-9-]*)",
                        errors, re.IGNORECASE)
    flag = unknown.group(1).decode("ascii") if unknown else None
    flag = flag if flag in flags else None
    error = ("boundary" if b"CLIENT: REFUSED " in errors else "bwrap" if b"bwrap:" in errors
             else "empty" if not errors else "unknown-option" if flag else "other")
    result.update(stderr_kind=error, unknown_flag=flag)
    if action == "version":
        version = system_clients.compatibility("claude", output)
        result.update(version_status=version.status, version=version.version)
    return result


def failure(action, exc, *, kind="compatibility"):
    return {"kind": kind, "status": "failed", "phase": action,
            "reason": exc.reason if isinstance(exc, AnalysisDenied) and exc.reason in REASONS else "runtime",
            "stage": exc.stage if isinstance(exc, AnalysisDenied) and exc.stage in STAGES else "internal"}


def diagnose():
    sandbox.runtime_ready()
    with sandbox.client_binding.bind("claude") as bound, sandbox.tempfile.TemporaryDirectory(prefix="ai-client-diagnostic-") as temp:
        directory = Path(temp)
        sandbox._identity_files(directory)
        deadline = time.monotonic() + 20
        seen = []

        def run(action):
            seen.append(action)
            result = sandbox._bound_run(bound, action, directory, online=False, deadline=deadline,
                                        limit=client_compat.MAX_OUTPUT)
            emit(observation(action, result))
            return result

        try:
            outcome = client_compat.check("claude", run)
            if time.monotonic() >= deadline:
                raise AnalysisDenied("timeout", stage="compatibility")
        except (AnalysisDenied, OSError, ValueError) as exc:
            emit(failure(seen[-1] if seen else "binding", exc))
            # A parser/schema mismatch needs a baseline without print flags.
            # Do not continue after a boundary, version, timeout or cancellation
            # refusal, and never refresh the operation's original deadline.
            if (isinstance(exc, AnalysisDenied) and exc.reason == "client-protocol"
                    and exc.stage in ("internal", "compatibility") and seen and seen[-1] != "version"
                    and "compat-rpc" not in seen and time.monotonic() < deadline):
                try:
                    run("compat-rpc")
                except (AnalysisDenied, OSError, ValueError) as baseline:
                    emit(failure("compat-rpc", baseline, kind="baseline"))
            return 2
        emit({"kind": "compatibility", "status": outcome.status, "version": outcome.version})
        return 0


def main(argv=None):
    if list(sys.argv[1:] if argv is None else argv) != ["--allow-offline-client-check", "claude"]:
        emit({"kind": "diagnostic", "status": "refused", "reason": "selector"})
        return 2
    try:
        require_host()
        host_analysis_acceptance.installed_matches(native=False)
    except (OSError, ValueError, AnalysisDenied):
        emit({"kind": "diagnostic", "status": "refused", "reason": "preflight"})
        return 2
    source = client_sources.discover("codex")
    emit({"kind": "source", "provider": "codex", "status": source.status,
          "detail": source.detail, "path": source.path})
    try:
        for record in codex_metadata():
            emit(record)
    except (OSError, ValueError, KeyError):
        emit({"kind": "source-path", "provider": "codex", "status": "unavailable"})
    try:
        return diagnose()
    except (AnalysisDenied, OSError, ValueError) as exc:
        emit(failure("setup", exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
