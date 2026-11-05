"""Trusted terminal approval primitives for E3/E4, offline only.

No entrypoint, transport, filesystem inspection or executor is installed here.
The trusted controller calls these functions with its already collected data;
it must still enforce E2 before transfer and E5 before workspace execution.
Supplied streams and clocks are trusted dependencies, never model parameters.
TTY checks prevent pipeline approvals; they are not proof against another
process controlling the same terminal/user account.
"""
import json
import sys
import time

from .transfer_consent import ConsentDenied, ConsentSession
from .typed_plan import PlanDenied, PlanSession


class ApprovalDenied(RuntimeError):
    """Sanitized refusal without report/plan content."""


def _terminal(input_stream, output_stream):
    source = sys.stdin if input_stream is None else input_stream
    target = sys.stderr if output_stream is None else output_stream
    if source.isatty() is not True or target.isatty() is not True:
        raise ApprovalDenied("APPROVAL: REFUSED")
    return source, target


def _confirm(source, target, display, verb, digest):
    # Data is encoded by the caller before display: no ANSI/C1/bidi execution.
    prompt = (display + "\nTo approve, type exactly: " + verb + " " +
              digest + "\nAny other answer means no.\n> ")
    if target.write(prompt) != len(prompt):
        raise ApprovalDenied("APPROVAL: REFUSED")
    target.flush()
    expected = verb + " " + digest + "\n"
    # Bounded read; no strip(), case folding, defaults or automatic retry.
    if source.readline(len(expected) + 1) != expected:
        raise ApprovalDenied("APPROVAL: REFUSED")


def _display_diff(text):
    # Keep line structure readable while rendering untrusted terminal controls,
    # non-ASCII and literal backslashes as unambiguous JSON-style escapes.
    return "".join(char if char == "\n" or 32 <= ord(char) <= 126 and char != "\\"
                   else json.dumps(char, ensure_ascii=True)[1:-1]
                   for char in text)


def confirm_transfer(report, *, input_stream=None, output_stream=None,
                     clock=time.monotonic):
    """Show exact canonical report bytes and obtain one explicit E3 decision.

    Returns the existing offline TransferAuthorization; does not send data.
    A future trusted transport must enforce lifetime/replay at actual dispatch.
    """
    try:
        source, target = _terminal(input_stream, output_stream)
        session = ConsentSession(clock=clock)
        preview = session.preview(report)
        display = ("CONSENT TO SEND DATA (offline; nothing is sent)\n" +
                   "Recipient: " + preview.recipient + "\nPurpose: " + preview.purpose +
                   "\nSHA-256: " + preview.sha256 + "\nExact JSON report:\n" +
                   preview.payload)
        _confirm(source, target, display, "SEND", preview.sha256)
        # Revalidate original object: mutation during the prompt is refused.
        return session.authorize(report, confirmed_sha256=preview.sha256,
                                 nonce=preview.nonce)
    except (ApprovalDenied, ConsentDenied, OSError, ValueError, TypeError,
            AttributeError, EOFError, KeyboardInterrupt):
        raise ApprovalDenied("APPROVAL: REFUSED") from None


def render_plan(preview):
    """Exact, escaped plan text shown for both dry-run and approval."""
    exact = [{"action": "create_text", "path": path, "content": content}
             for path, content in preview.operations]
    return ("CONSENT TO A CREATE-ONLY PLAN (offline; nothing is run)\n" +
            "Workspace: " + preview.workspace + "\nState SHA-256: " +
            preview.state_sha256 + "\nPlan SHA-256: " + preview.sha256 +
            "\nExact operations (JSON strings; escapes are literal):\n" +
            json.dumps(exact, ensure_ascii=True, indent=2) +
            "\nDiff (special characters as JSON escapes):\n" +
            _display_diff(preview.diff))


def confirm_plan(workspace, operations, *, state_sha256, input_stream=None,
                 output_stream=None, clock=time.monotonic):
    """Show E4 plan/diff and return (trusted PlanSession, authorization).

    State hash is provided by the trusted controller; this function performs no
    filesystem inspection or execution. The executor must verify actual state.
    Canonical JSON strings are reversible and escape every terminal control,
    unicode bidi character, backslash and trailing newline in proposed content.
    """
    try:
        source, target = _terminal(input_stream, output_stream)
        session = PlanSession(clock=clock)
        preview = session.preview(workspace, operations, state_sha256=state_sha256)
        display = render_plan(preview)
        _confirm(source, target, display, "CREATE", preview.sha256)
        authorization = session.authorize(
            confirmed_sha256=preview.sha256, nonce=preview.nonce,
            state_sha256=preview.state_sha256)
        return session, authorization
    except (ApprovalDenied, PlanDenied, OSError, ValueError, TypeError,
            AttributeError, EOFError, KeyboardInterrupt):
        raise ApprovalDenied("APPROVAL: REFUSED") from None
