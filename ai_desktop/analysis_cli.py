"""One useful read-only flow: preview -> explicit consent -> analysis -> text."""
import json
import os
import sys

from . import diagnose
from .analysis_contract import (AnalysisConsent, AnalysisDenied, CLIENT_CONTEXT, CLIENT_VERSION,
                                POLICY, SYSTEM_PROMPT, MAX_QUESTION_BYTES, validate_question)
from .analysis_service import send_once
from .analysis_session import installation_ready, managed_session
from .analysis_terminal import render_answer
from .compare import read_snapshot
from .report_policy import serialize_report
from .terminal_consent import confirm, fresh_input
from .run_controls import recipient, failure_hint, label, wait_message
from .execution import UNSET
from .terminal_style import rule

ANSWER_SEPARATOR = "─" * 48

HINTS = {
    "network": "Could not make the allowed connection. Check your network and the service; then run the command again and approve the send.",
    "rate-limit": "The service rejected the request because of a limit. Wait, then run the command again and approve it again; nothing is resent automatically.",
    "provider-unavailable": "The Claude service reported a temporary error. Try later by running the command again and approving it again.",
    "runtime": "The isolated runtime is incomplete. Run ai-desktop doctor --clients.",
    "version": "The system client did not pass its isolated compatibility check.",
    "endpoint": "Could not start or connect the session. Check ai-desktop status; to recover: ai-desktop stop.",
    "auth-required": "Connect your account: ai-desktop login (choose Claude). Then start a new analysis and approve the new preview.",
    "auth-method": "A claude.ai subscription login in a separate profile is required; other methods are rejected.",
    "busy": "The session or profile is busy, or an unfinished cycle remains. Check ai-desktop status; to recover your own session: ai-desktop stop.",
    "timeout": "The time limit was exceeded. The result is not confirmed; there is no automatic retry.",
    "client": "The client did not finish the analysis. Check your network access and subscription. There is no automatic retry or switch to the API.",
    "consent": "The send was not approved or the approval expired. On a new analysis, check the preview and type yes (y) to approve or no (n) to decline. The report was not sent.",
    "authorization": "Could not get permission to start a separate session. The report was not sent.",
    "cleanup": "Could not confirm that the session stopped. Run ai-desktop stop; do not resend automatically.",
    "cancelled": "The analysis was stopped. Data already sent cannot be taken back; there is no automatic retry.",
}


def main(*, preview=False, question="", ask=False, report_path=None, output_format="auto", details=False, reader=input):
    submitted = False
    shown_execution = UNSET
    try:
        if os.geteuid() == 0 or os.getuid() != os.geteuid():
            raise AnalysisDenied("identity")
        if (not preview or ask) and (not sys.stdin.isatty() or not sys.stdout.isatty()):
            raise AnalysisDenied("consent")
        if (type(details) is not bool or output_format not in ("auto", "plain", "markdown")
                or (report_path is not None and (type(report_path) is not str or report_path == "-"))
                or (ask and question)):
            raise AnalysisDenied("input")
        validate_question(question)
        if not preview:
            installation_ready()
        report = read_snapshot(report_path) if report_path is not None else diagnose.collect()
        if ask:
            prompt = "Question about the report (Enter = general analysis): "
            question = fresh_input(prompt, max_bytes=MAX_QUESTION_BYTES) if reader is input else reader(prompt)
        gate = AnalysisConsent(report, question=question)
        envelope = json.loads(gate.payload)
        # Show the recipient of these exact prepared bytes. Account login does
        # not select a provider, and later ambient settings must not relabel
        # the preview or the answer/error of an already prepared request.
        shown_execution = envelope.get("execution")
        print(label(config=shown_execution))
        print("=== REPORT TO ANALYZE ===" if details else "Report to analyze:")
        print(serialize_report(envelope["report"]))
        print("=== YOUR QUESTION ===" if details else "Question:")
        print(question if question else "General analysis of the report.")
        if details:
            print("=== APPLICATION INSTRUCTIONS ===")
            print(envelope["system_prompt"])
            print("=== RULES AND RECIPIENT ===")
            print(envelope["client_context"])
            print("Client:", envelope["client_version"], "Rules:", envelope["policy"])
            print("SHA256 of the report, question, instructions and rules:", gate.sha256)
        else:
            print(recipient("Claude will receive the data above and the fixed application instructions. The result makes no changes.",
                            config=shown_execution))
        if preview:
            if details:
                print("PREVIEW: PASS (offline; no client started and no profile access)")
            else:
                print("Offline preview — nothing was sent.")
            return 0
        if not confirm("transfer", reader=reader, config=shown_execution):
            if details:
                raise AnalysisDenied("consent")
            print("Cancelled. The report was not sent.")
            return 2
        gate.check(report, question=question, sha256=gate.sha256, nonce=gate.nonce)
        with managed_session(**({} if details else {"quiet": True})):
            # sudo/setup can take time: recheck the same immutable preview and
            # TTL immediately before transfer; never automatically renew consent.
            approval = gate.authorize(report, question=question, sha256=gate.sha256, nonce=gate.nonce)
            waiting = wait_message(config=shown_execution)
            if details:
                waiting += " A report already sent cannot be taken back."
            print(waiting, flush=True)
            submitted = True
            result = send_once(approval)
        print("=== RESULT FROM " + recipient(config=shown_execution).upper() + " — suggestions to review, no changes made ===" if details
              else "\n" + rule() + "\n")
        print(render_answer(result, output_format=output_format))
        if details:
            print("ANALYSIS: PASS sha256=" + gate.sha256)
            print("SESSION: STOPPED")
        else:
            print("\n" + rule() + "\n")
        return 0
    except AnalysisDenied as exc:
        if details:
            print(str(exc))
        print(failure_hint(exc.reason, HINTS.get(exc.reason, "The analysis was stopped by a safety check; the system was not changed."),
                           config=shown_execution,
                           **({"diagnostic": exc.diagnostic} if exc.diagnostic is not None else {})))
        return 2
    except (EOFError, KeyboardInterrupt):
        if details:
            print("ANALYSIS: CANCELLED (no automatic retry; data already sent cannot be taken back)")
        else:
            print("Cancelled.")
            if submitted:
                print("Data already sent cannot be taken back; the analysis will not be retried automatically.")
        return 2
    except (OSError, ValueError, TypeError):
        print("ANALYSIS: REFUSED reason=input" if details
              else "Could not prepare the analysis. Check the report and the question.")
        return 2
