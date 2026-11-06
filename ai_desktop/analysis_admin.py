"""Explicit administrative login only, never arbitrary commands."""
import configparser
import os
from pathlib import Path
import secrets
import subprocess
import sys

from .analysis_contract import AnalysisDenied
from .analysis_sandbox import INSTALL, runtime_ready, trusted_file, failure_stage

UNIT = Path("/usr/lib/systemd/system/ai-desktop-analysis@.service")


def login_command(*, details=False):
    if type(details) is not bool:
        raise AnalysisDenied("input")
    trusted_file(UNIT)
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read_string(UNIT.read_text())
    command = ["/usr/bin/systemd-run", "--unit=ai-desktop-login-" + secrets.token_hex(8), "--wait", "--collect", "--pty",
               "--service-type=exec", "--property=RuntimeMaxSec=600"]
    if not details:
        command.append("--quiet")
    omitted = {"ExecStart", "StandardInput", "StandardOutput", "StandardError", "RuntimeMaxSec", "Type"}
    for key, value in parser["Service"].items():
        if key not in omitted:
            command.append("--property=" + key + "=" + value)
    return command + ["/usr/bin/python3", "-I", "-B", str(INSTALL / "analysis-worker.py"),
                      "login-verbose" if details else "login"]


def login_unit_stopped(state):
    """A collected unit may be reported as not-found with rc=1; contradictory state never proves cleanup."""
    values = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
    if values.get("LoadState") == "not-found":
        return (state.returncode in (0, 1) and values.get("ActiveState", "inactive") == "inactive"
                and values.get("MainPID", "0") == "0")
    return (state.returncode == 0 and values.get("ActiveState") in ("inactive", "failed")
            and values.get("MainPID") == "0")


def main(*, allow=False, details=False):
    # The explicit flag, root and real terminal are required before any command,
    # runtime binary, credential access or network request can be started.
    if (allow is not True or type(details) is not bool or os.getuid() != 0 or os.geteuid() != 0
            or not os.isatty(0) or not os.isatty(1)):
        print("LOGIN: REFUSED (root, TTY and --allow-claude-login required)")
        return 2
    unit, passed = None, False
    environment = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"}
    try:
        with failure_stage("runtime"):
            runtime_ready()
        if details:
            print("Separate claude.ai login for ai-desktop. Open the client's address by hand in your browser.")
            print("Do not paste the login code or tokens into a chat or test logs.", flush=True)
        with failure_stage("manager"):
            command = login_command(details=details)
            unit = next(arg[len("--unit="):] for arg in command if arg.startswith("--unit=")) + ".service"
            done = subprocess.run(command, env=environment,
                                  check=False, close_fds=True, timeout=610)
        passed = done.returncode == 0
        if not passed:
            print("LOGIN: REFUSED stage=manager reason=client", flush=True)
    except (AnalysisDenied, OSError, ValueError, configparser.Error, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        failure = exc if isinstance(exc, AnalysisDenied) else AnalysisDenied("internal")
        print("LOGIN: REFUSED stage=" + failure.stage + " reason=" + failure.reason, flush=True)
    finally:
        if unit is not None:
            # Stop only this invocation's unpredictable unit, even if its TTY
            # wrapper was interrupted. Never stop a different login session.
            try:
                subprocess.run(["/usr/bin/systemctl", "stop", unit], env=environment,
                               check=False, capture_output=True, timeout=10)
                state = subprocess.run(["/usr/bin/systemctl", "show", unit,
                                        "--property=LoadState,ActiveState,MainPID"], env=environment,
                                       check=False, capture_output=True, text=True, timeout=5)
                if not login_unit_stopped(state):
                    passed = False
                    print("LOGIN: REFUSED cleanup=incomplete")
            except (OSError, ValueError, subprocess.SubprocessError):
                passed = False
                print("LOGIN: REFUSED cleanup=unverified")
    return 0 if passed else 2
