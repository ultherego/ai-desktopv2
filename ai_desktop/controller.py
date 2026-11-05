"""Explicit diagnostic controller: validate, isolate, inspect, then optionally ask AI.

No arbitrary actions are accepted. Live requests fail closed until the E2
subscription boundary has passed acceptance and received explicit approval.
"""
import hashlib
import json
import os
import subprocess
import sys

from . import approval_ui, diagnose, executor
from .report_policy import serialize_report


def prepare():
    """Return one immutable-in-this-call data snapshot and checked worker findings."""
    report = diagnose.collect()
    payload = serialize_report(report)

    if executor._run("--probe") != {"probe": "PASS"}:
        raise RuntimeError("Could not confirm the worker isolation")

    findings = executor.validate_assessment(executor._run("--check", payload))
    return report, payload, findings


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args not in (["preview"], ["approve"], ["ask"]):
        print("Usage: python3 -B -m ai_desktop controller preview|approve|ask")
        return 2
    if os.geteuid() == 0:
        print("CONTROLLER: REFUSED (root)")
        return 2

    try:
        report, payload, findings = prepare()
    except (OSError, RuntimeError, ValueError, TypeError):
        print("CONTROLLER: FAIL (report or worker rejected)")
        return 1

    print("=== REPORT ALLOWED TO BE SENT TO AI ===", flush=True)
    print(payload, flush=True)
    print("=== ISOLATED WORKER ASSESSMENT (LOCAL) ===", flush=True)
    print(json.dumps(findings, indent=2, ensure_ascii=False), flush=True)
    print("REPORT SHA256: " + hashlib.sha256(payload.encode("utf-8")).hexdigest(), flush=True)

    if args == ["preview"]:
        print("CONTROLLER PREVIEW: PASS (no data sent to AI)")
        return 0

    if args == ["approve"]:
        # E3: real terminal consent for exactly this payload. Nothing is sent;
        # the authorization is discarded because no transport exists before E2.
        try:
            authorization = approval_ui.confirm_transfer(report)
        except approval_ui.ApprovalDenied:
            print("CONTROLLER APPROVE: DENIED (not approved)")
            return 2
        print("CONTROLLER APPROVE: APPROVED sha256=" + authorization.sha256 +
              " (no data sent to AI)")
        return 0

    # No legacy host launch: diagnose.ask refuses while E2 is unaccepted.
    try:
        return diagnose.ask(report)
    except (OSError, RuntimeError, ValueError, TypeError, subprocess.TimeoutExpired):
        print("CONTROLLER: FAIL (Claude)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
