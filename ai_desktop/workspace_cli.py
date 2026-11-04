"""CLI for the create-only workspace flow: plan, apply, status, recover.

No model is involved. Content comes only from local files the user names.
`plan` is a dry run; `apply` needs a real terminal and the exact plan hash typed
by the user (approval_ui), then hands the one-time authorization to the E5
executor. Output never contains file contents beyond the approved diff.
"""
import argparse
import os
import stat
import sys

from . import approval_ui
from .typed_plan import MAX_CONTENT_BYTES, PlanDenied, PlanSession
from .workspace_executor import (
    ExecutionDenied, WorkspaceExecutor, inspect_state, recover, status
)

_SOURCE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _read_source(path):
    fd = os.open(path, _SOURCE_FLAGS)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONTENT_BYTES:
            raise ValueError("source")
        raw = os.read(fd, info.st_size + 1)
        if len(raw) != info.st_size:
            raise ValueError("source")
        return raw.decode("utf-8")
    finally:
        os.close(fd)


def _operations(specs):
    operations = []
    for spec in specs:
        relative, separator, source = spec.partition("=")
        if not separator or not relative or not source:
            raise ValueError("spec")
        operations.append({"action": "create_text", "path": relative,
                           "content": _read_source(source)})
    return operations


def _parser():
    parser = argparse.ArgumentParser(prog="ai-desktop workspace")
    actions = parser.add_subparsers(dest="action", required=True)
    for name, help_text in (("plan", "Dry-run: diff i hashe planu, bez zapisu"),
                            ("apply", "Zatwierdz i utworz nowe pliki")):
        sub = actions.add_parser(name, help=help_text)
        sub.add_argument("workspace")
        sub.add_argument("--create", action="append", required=True,
                         metavar="SCIEZKA=PLIK",
                         help="nowy plik w workspace z zawartoscia lokalnego pliku")
    sub = actions.add_parser("status", help="Lista dziennikow operacji (bez tresci)")
    sub.add_argument("workspace")
    sub = actions.add_parser("recover", help="Cofnij przerwane operacje po restarcie")
    sub.add_argument("workspace")
    sub.add_argument("--rollback", action="append", default=[], metavar="PLAN_SHA256",
                     help="dodatkowo cofnij zakonczone zastosowanie tego planu")
    return parser


def main(argv=None, *, input_stream=None, output_stream=None):
    out = sys.stdout
    try:
        args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if os.geteuid() == 0:
        print("WORKSPACE: REFUSED (root)")
        return 2
    try:
        if args.action == "status":
            for item in status(args.workspace):
                print(item["status"], item["plan_sha256"], file=out)
            return 0
        if args.action == "recover":
            result = recover(args.workspace, tuple(args.rollback))
            print("RECOVER:", result["status"], "recovered=%d conflicts=%d invalid=%d" % (
                result["recovered"], result["conflicts"], result["invalid"]), file=out)
            return 0 if result["status"] == "RECOVERED" else 1

        operations = _operations(args.create)
        state = inspect_state(args.workspace,
                              tuple((o["path"], o["content"]) for o in operations))
        if args.action == "plan":
            preview = PlanSession().preview(args.workspace, operations,
                                            state_sha256=state)
            print(approval_ui.render_plan(preview), file=out)
            print("PLAN: DRY-RUN (nic nie zapisano)", file=out)
            return 0

        session, authorization = approval_ui.confirm_plan(
            args.workspace, operations, state_sha256=state,
            input_stream=input_stream, output_stream=output_stream)
        executor = WorkspaceExecutor(args.workspace)
        try:
            result = executor.apply(session, authorization)
        finally:
            executor.close()
        print("APPLY: %s files=%d plan=%s" % (
            result["status"], result["count"], result["plan_sha256"]), file=out)
        return 0
    except approval_ui.ApprovalDenied:
        print("WORKSPACE: ODMOWA (brak zatwierdzenia)", file=out)
        return 2
    except (ExecutionDenied, PlanDenied, ValueError, OSError):
        print("WORKSPACE: REFUSED", file=out)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
