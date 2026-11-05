"""Repeatable, explicitly-scoped AI Desktop verification runner.

CI mode never contacts an AI provider. Local mode adds real Bubblewrap probes.
AI mode is an explicit opt-in and can use Claude subscription quota.
No arbitrary commands are accepted.
"""
import argparse
import ast
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
MAX_OUTPUT = 80
COMPAT_FAILURE_REASONS = frozenset({"internal", "timeout", "output-limit", "process-io",
    "http-parser", "http-method", "handler-error", "request-limit", "body-length",
    "body-read", "endpoint", "auth-header", "unexpected-header", "request-json",
    "request-shape", "request-model", "request-tools", "request-messages",
    "request-content", "request-stream", "no-message", "client-exit", "client-json",
    "client-reply", "client-denial", "client-options", "client-auth", "client-permission",
    "turn-limit", "boundary", "version", "fault-not-observed"})
STEPS = {
    "compat": ("e2-claude-compat",),
    "ci": ("syntax", "unit", "cli", "skills", "doctor", "compare", "compare-summary", "snapshot", "compare-exit", "validate-report", "offline-pipeline", "stdin-pipeline", "snapshot-save", "e1-boundary", "e2-contract", "e2-broker", "e2-egress", "e3-e4-contracts", "e5-e6-contracts", "e7-install", "e7-host-cycle"),
    "local": ("syntax", "unit", "cli", "skills", "doctor", "compare", "compare-summary", "snapshot", "compare-exit", "validate-report", "offline-pipeline", "stdin-pipeline", "snapshot-save", "e1-boundary", "e2-contract", "e2-broker", "e2-egress", "e3-e4-contracts", "e5-e6-contracts", "e7-install", "sandbox", "skill-mount",
              "executor-probe", "executor-inspect", "e2-host-probe", "e5-host-cycle", "e7-host-cycle", "task-host-cycle", "e5-btrfs-binding"),
    "ai": ("syntax", "unit", "cli", "skills", "doctor", "compare", "compare-summary", "snapshot", "compare-exit", "validate-report", "offline-pipeline", "stdin-pipeline", "snapshot-save", "e1-boundary", "e2-contract", "e2-broker", "e2-egress", "e3-e4-contracts", "e5-e6-contracts", "e7-install", "sandbox", "skill-mount",
           "executor-probe", "executor-inspect", "launcher", "agent", "e2-host-probe", "e5-host-cycle", "e7-host-cycle", "controller-preview", "controller-ask"),
}
TIMEOUTS = {"unit": 240, "controller-ask": 180, "e2-claude-compat": 670, "e2-host-probe": 80}


def command_for(step, python=None):
    python = python or sys.executable
    prefix = [python, "-B"]
    commands = {
        # Includes versioned-file conflicts and exact-packet delivery regressions,
        # analysis consent/auth/output/process/activation contracts and
        # offline unit parsing. Never native login or a real model request.
        # host_e2_activated_worker.py is explicit opt-in, never discovered here.
        "unit": prefix + ["-m", "unittest", "discover", "-s", "tests", "-v"],
        "cli": prefix + ["-m", "ai_desktop", "--help"],
        "skills": prefix + ["-m", "ai_desktop", "skills", "validate"],
        "doctor": prefix + ["-m", "ai_desktop", "doctor"],
        "compare": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_compare.py", "-v"],
        "compare-summary": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_compare_summary.py", "-v"],
        "snapshot": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_snapshot.py", "-v"],
        "compare-exit": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_compare_exit.py", "-v"],
        "validate-report": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_validate_snapshot.py", "-v"],
        "offline-pipeline": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_offline_pipeline.py", "-v"],
        "stdin-pipeline": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_stdin_pipeline.py", "-v"],
        "snapshot-save": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_private_snapshot_save.py", "-v"],
        "e1-boundary": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_security_boundary_e1.py", "-v"],
        "e2-contract": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_e2_*boundary*.py", "-v"],
        "e2-broker": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_e2_broker.py", "-v"],
        "e2-egress": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_e2_egress.py", "-v"],
        "e3-e4-contracts": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_e3_e4*.py", "-v"],
        "e5-e6-contracts": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_e5_*.py", "-v"],
        "e7-install": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "test_e7_*.py", "-v"],
        "e7-host-cycle": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "host_e7_lifecycle_checks.py", "-v"],
        "e5-host-cycle": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "host_e5_workspace_checks.py", "-v"],
        "e5-btrfs-binding": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "host_e5_btrfs_binding.py", "-v"],
        "task-host-cycle": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "host_task_checks.py", "-v"],
        "e2-claude-compat": prefix + ["-m", "ai_desktop.e2_client_compat", "self-test", "--allow-claude-compat"],
        "e2-host-probe": prefix + ["-m", "unittest", "discover", "-s", "tests", "-p", "host_e2_*checks.py", "-v"],
        "sandbox": prefix + ["-m", "ai_desktop", "sandbox", "self-test"],
        "skill-mount": prefix + ["-m", "ai_desktop", "skill-mount", "self-test"],
        "executor-probe": prefix + ["-m", "ai_desktop", "executor", "self-test"],
        "executor-inspect": prefix + ["-m", "ai_desktop", "executor", "inspect"],
        "launcher": prefix + ["-m", "ai_desktop", "launcher", "version"],
        "agent": prefix + ["-m", "ai_desktop", "agent", "version"],
        "controller-preview": prefix + ["-m", "ai_desktop", "controller", "preview"],
        "controller-ask": prefix + ["-m", "ai_desktop", "controller", "ask"],
    }
    if step == "syntax":
        return None
    if step not in commands:
        raise ValueError("Unknown verification step")
    return commands[step]


def syntax_check():
    checked = 0
    for folder in ("ai_desktop", "tests", "scripts"):
        path = ROOT / folder
        if not path.is_dir():
            return 1, f"Missing source directory: {folder}"
        for file in sorted(path.rglob("*.py")):
            if file.is_symlink():
                return 1, f"Symlink not accepted: {file.relative_to(ROOT)}"
            try:
                ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
            except (SyntaxError, UnicodeError, OSError) as exc:
                return 1, f"{file.relative_to(ROOT)}: {type(exc).__name__}"
            checked += 1
    return 0, f"Parsed {checked} Python files"


def execute(step, runner=subprocess.run):
    start = time.monotonic()
    if step == "syntax":
        code, output = syntax_check()
    else:
        try:
            done = runner(
                command_for(step),
                cwd=ROOT,
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=TIMEOUTS.get(step, 50),
            )
            code = done.returncode
            output = (done.stdout + "\n" + done.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            code, output = 1, type(exc).__name__
    result = {
        "step": step,
        "passed": code == 0,
        "exit_code": code,
        "seconds": round(time.monotonic() - start, 3),
    }
    return result, output


def run(mode, ai_consent=False, fail_fast=False, writer=print, runner=subprocess.run, compat_consent=False):
    if mode not in STEPS:
        raise ValueError("Unknown verification mode")
    if mode == "compat" and compat_consent is not True:
        raise ValueError("Compat mode requires --allow-claude-compat (local fake gateway, no model/OAuth)")
    if compat_consent and mode != "compat":
        raise ValueError("--allow-claude-compat is only valid in compat mode")
    if mode == "ai" and not ai_consent:
        raise ValueError("AI mode requires --allow-ai-request")
    if mode != "ci" and hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ValueError("Live checks must not run as root")

    results = []
    for step in STEPS[mode]:
        result, output = execute(step, runner=runner)
        results.append(result)
        writer(f"[{'PASS' if result['passed'] else 'FAIL'}] {step} "
               f"(exit {result['exit_code']}, {result['seconds']:.3f}s)")
        if mode == "compat" and re.fullmatch(
                r"COMPAT: (?:REFUSED phase=(?:setup|version|reply|deny|rate-limit|unavailable|disconnect|timeout|recovery-(?:deny|rate|unavailable|disconnect|timeout))|PASS version=2\.1\.312 isolated-loopback reply=PASS denial=PASS rate-limit=PASS unavailable=PASS disconnect=PASS timeout-stop=PASS recovery=5/5 real_auth=false upstream=false)", output):
            writer(output)  # Strict metadata vocabulary only; never native logs.
        if mode == "compat":
            detail = re.fullmatch(
                r"COMPAT: REFUSED phase=(?:setup|version|reply|deny|rate-limit|unavailable|disconnect|timeout|recovery-(?:deny|rate|unavailable|disconnect|timeout)) reason=([a-z-]+) "
                r"exit=(?:none|-?[0-9]{1,3}) requests=[0-9]{1,2} messages=[0-9]{1,2} "
                r"input_messages=[0-9]{1,2} text_blocks=[0-9]{1,2} marker=[01] exact=[01] "
                r"result_type=[01] success_subtype=[01] is_error=[01] "
                r"api_status=(?:none|other|401|429|500|502|503|504|529)", output)
            if detail is not None and detail.group(1) in COMPAT_FAILURE_REASONS:
                writer(output)
        if not result["passed"]:
            # Never echo raw AI prompts, secrets or diagnostic output to a log.
            # A traceback tail can omit the first failure when several integrations break.
            if mode == "ci" and step == "unit":
                identifiers = re.findall(r"^(?:FAIL|ERROR): (test_[A-Za-z0-9_]+) \(([A-Za-z0-9_.]+)\)", output, re.M)
                for name, owner in identifiers[:40]:
                    writer("Failed test: " + owner + "." + name)
            # Only 'ci' static/unit checks are echoed for debugging.
            if mode == "ci" and step in ("syntax", "unit", "cli", "skills", "doctor", "compare", "compare-summary", "snapshot", "compare-exit", "validate-report", "offline-pipeline", "stdin-pipeline", "snapshot-save", "e1-boundary", "e2-contract", "e2-broker", "e2-egress", "e3-e4-contracts", "e5-e6-contracts", "e7-install", "e7-host-cycle"):
                writer("\n".join(output.splitlines()[-MAX_OUTPUT:]))
            if fail_fast:
                break
    writer(f"RESULT: {sum(r['passed'] for r in results)}/{len(results)} stages PASS")
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI Desktop verification runner")
    parser.add_argument("--mode", choices=tuple(STEPS), default="ci")
    parser.add_argument("--allow-ai-request", action="store_true",
                        help="Explicitly permit a live Claude request with --mode ai")
    parser.add_argument("--allow-claude-compat", action="store_true",
                        help="Explicitly run native Claude only against a secret-free isolated local fixture")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--json", metavar="FILE",
                        help="Write metadata-only results; no logs or diagnostic data")
    args = parser.parse_args(argv)

    try:
        results = run(args.mode, ai_consent=args.allow_ai_request,
                      fail_fast=args.fail_fast, compat_consent=args.allow_claude_compat)
    except ValueError as exc:
        parser.error(str(exc))

    success = all(result["passed"] for result in results) and len(results) == len(STEPS[args.mode])
    if args.json:
        path = Path(args.json)
        payload = json.dumps(
            {"mode": args.mode, "passed": success, "checks": results},
            ensure_ascii=False, indent=2,
        ) + "\n"
        # O_EXCL prevents overwrites, including symlink targets and race
        # substitutions after a separate path.is_symlink() check.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except OSError:
            print("Verification JSON destination must not exist and parent must exist")
            return 2
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
        except OSError:
            print("Verification JSON write failed")
            return 2
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

