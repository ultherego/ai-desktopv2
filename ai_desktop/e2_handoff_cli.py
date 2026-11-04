"""Explicit read-only consent UI and constrained one-shot worker CLI.

No AI/OAuth. Worker must be started separately by an approved privileged
supervisor as the dedicated ai-desktop UID. The desktop UI cannot switch UIDs.
"""
import os
import sys

from . import diagnose, e2_ipc, e2_worker_once, e2_socket_activation
from .transfer_consent import ConsentSession, ConsentDenied


def desktop_main(session=None, *, reader=input):
    if os.geteuid() == 0:
        print("HANDOFF: REFUSED")
        return 2
    try:
        if session is None and (not sys.stdin.isatty() or not sys.stdout.isatty()):
            raise ConsentDenied("CONSENT: REFUSED")
        report = diagnose.collect()
        gate = ConsentSession()
        preview = gate.preview(report)
        # The user must see exactly what would be sent before confirming.
        print("=== RAPORT TYLKO DO ODCZYTU ===")
        print(preview.payload)
        print("ODBIORCA:", preview.recipient)
        print("CEL:", preview.purpose)
        print("SHA256:", preview.sha256)
        if session is None:
            print("Tryb offline: tylko odbior raportu przez osobny UID; bez Claude i OAuth.")
        answer = reader("Wpisz UDOSTEPNIJ " + preview.sha256 + " aby przekazac raport: ")
        if answer != "UDOSTEPNIJ " + preview.sha256:
            raise ConsentDenied("CONSENT: REFUSED")
        auth = gate.authorize(report, confirmed_sha256=preview.sha256, nonce=preview.nonce)
        digest = (e2_socket_activation.send_once(auth) if session is None
                  else e2_worker_once.send_once(session, auth))
        print("HANDOFF: PASS sha256=" + digest)
        return 0
    except (EOFError, KeyboardInterrupt, OSError, ValueError,
            e2_ipc.HandoffDenied, ConsentDenied):
        print("HANDOFF: REFUSED")
        return 2


def worker_main(session, desktop_uid):
    try:
        def ready():
            print("WORKER: READY", flush=True)
        digest = e2_worker_once.serve_once(
            session, desktop_uid=desktop_uid, on_ready=ready)
        print("WORKER: RECEIVED sha256=" + digest, flush=True)
        return 0
    except e2_ipc.HandoffDenied:
        print("WORKER: REFUSED", flush=True)
        return 2
