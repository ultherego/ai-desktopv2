"""User instruction -> selected data consent -> proposal -> test -> write consent."""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import sys
import time

from .run_controls import recipient, failure_hint, wait_message
from .analysis_cli import HINTS
from .analysis_contract import (AnalysisConsent, AnalysisDenied, CLIENT_CONTEXT, SYSTEM_PROMPT,
                                CONSENT_CEILING, canonical, consent_seconds, terminal_text)
from .analysis_service import send_once
from .analysis_session import installation_ready, managed_session
from .analysis_terminal import fenced_code, render_answer
from . import task_contract as contract, task_sandbox, task_workspace as workspace
from .task_plan import TaskPlan
from .terminal_consent import confirm, fresh_input
from .execution import UNSET
from .skill_runtime import task_skills, accept_skill_response, recheck_with, MAX_ROUNDS
from .task_test_consent import CopyTestConsent


def build_parser():
    from .run_controls import add_arguments
    parser = argparse.ArgumentParser(prog="ai-desktop task", description="Change selected files and test them in an isolated copy")
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run", help="Instruction -> proposal -> test -> diff -> approve the write")
    add_arguments(run)
    run.add_argument("instruction", help="Task for the selected assistant; the model gets no free terminal")
    run.add_argument("--workspace", required=True, help="A specific project directory (not all of HOME)")
    run.add_argument("--file", action="append", required=True, help="Relative file to edit or create; can be repeated")
    run.add_argument("--context", action="append", default=[], help="Read-only file, for example independent tests")
    run.add_argument("--test", choices=task_sandbox.CHECKS, required=True)
    run.add_argument("--preview", action="store_true", help="Preview the data offline, without AI, tests or writes")
    run.add_argument("--format", choices=("auto", "plain", "markdown"), default="auto")
    status = commands.add_parser("status", help="Read the task journal metadata")
    status.add_argument("--workspace", required=True)
    undo = commands.add_parser("rollback", help="Undo approved writes; refused if the file was edited later")
    undo.add_argument("--workspace", required=True)
    undo.add_argument("--plan", help="Optional ID; without it you pick the task from a list")
    restore = commands.add_parser("restore", help="Restore files deleted in project mode (task trash)")
    restore.add_argument("--workspace", required=True)
    restore.add_argument("--plan", required=True, help="Deletion ID from the message shown after the deletion")
    for command in (run, status, undo, restore):
        command.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                             help="Show full data, IDs and diagnostics")
    return parser


def selection(args):
    home = pwd.getpwuid(os.geteuid()).pw_dir
    if args.workspace == home or str(Path(args.workspace)) != args.workspace:
        raise ValueError("select project")
    before = workspace.snapshot(args.workspace, args.file + args.context)
    source = contract.request({"instruction": args.instruction, "files": [
        {"path": name, "content": contract.plain_text(entry["file"]["content"]) if entry["file"] else None,
         "writable": name in args.file} for name, entry in before["files"].items()]})
    return before, source


def _test_scope(before, source, args):
    """Bind the selected local test inputs without exposing contents in the scope summary."""
    return {"workspace": args.workspace, "copy": "selected-files",
            "snapshot": hashlib.sha256(canonical(before).encode()).hexdigest(),
            "source": hashlib.sha256(canonical(source).encode()).hexdigest(),
            "preparation_requests": MAX_ROUNDS + 1, "test_runs": 1}


@task_skills
def run_task(args, reader, *, display_state=None):
    from .run_controls import require_changes
    require_changes()
    before, source = selection(args)
    if not args.preview:
        # Do not spend a model request on a task that known history/recovery
        # state already forbids; apply repeats this check under its lock.
        detached = workspace._can_apply(args.workspace)
        if type(detached) is int and detached:
            print(workspace.DETACHED_HISTORY_HINT)
    source_check = recheck_with(lambda: workspace.unchanged(before))
    gate = AnalysisConsent(None, task=source, source_check=source_check)
    selected = json.loads(gate.payload).get("execution")
    # Presentation only: authorization still checks the live execution binding.
    if display_state is not None:
        display_state["execution"] = selected
    if args.details:
        print("=== SELECTED DATA FOR %s ===" % recipient(config=selected).upper())
        print(json.dumps(source, ensure_ascii=False, indent=2))
        print("=== APPLICATION INSTRUCTIONS ===\n" + SYSTEM_PROMPT)
        print("=== RULES AND RECIPIENT ===\n" + json.loads(gate.payload)["client_context"])
        print("SHA256:", gate.sha256)
    else:
        print("Task: " + source["instruction"])
        print(recipient("To Claude: the instruction, the selected paths and the full content of existing files:", config=selected))
        for item in source["files"]:
            action = ("create" if item["content"] is None else "edit") if item["writable"] else "read-only"
            size = 0 if item["content"] is None else len(item["content"].encode("utf-8"))
            print("  " + item["path"] + " — " + action + ", " + str(size) + " B")
        print("Full data preview: --v.")
    seconds = min(CONSENT_CEILING, (MAX_ROUNDS + 1) * consent_seconds(
        selected.get("effort") if selected is not None else None))
    test_consent = CopyTestConsent(_test_scope(before, source, args), args.test,
                                  guard=source_check, seconds=seconds)
    print("Workspace: " + terminal_text(args.workspace))
    print("Declared test: %s, once on a copy of the %d selected file(s)." % (args.test, len(source["files"])))
    print("Preparation: at most %d model requests within %d minutes; no network, user HOME or host writes in the test." %
          (MAX_ROUNDS + 1, (test_consent.seconds + 59) // 60))
    print("Saving still requires your approval after the full diff.")
    if args.preview:
        print("TASK PREVIEW: PASS sha256=" + gate.sha256 if args.details
              else "Preview ready. Nothing was sent or saved.")
        return 0
    installation_ready()
    if not confirm("task-scope", reader=reader, config=selected):
        raise AnalysisDenied("consent")
    test_consent.approve()
    try:
        gate.check(None, task=source, sha256=gate.sha256, nonce=gate.nonce)
        workspace.unchanged(before)
        with managed_session(**({} if args.details else {"quiet": True})):
            print(wait_message(config=selected), flush=True)
            for number in range(MAX_ROUNDS + 1):
                test_consent.check(_test_scope(before, source, args), args.test)
                approval = gate.authorize(None, task=source, sha256=gate.sha256, nonce=gate.nonce)
                response = send_once(approval)
                test_consent.check(_test_scope(before, source, args), args.test)
                if not accept_skill_response(response):
                    break
                if number == MAX_ROUNDS:
                    raise AnalysisDenied("consent")
                gate = AnalysisConsent(None, task=source, source_check=source_check)
        if args.details:
            print("SESSION: STOPPED")
        return finish_task(before, source, response, args, reader, config=selected, test_consent=test_consent)
    finally:
        test_consent.close()


def finish_task(before, source, response, args, reader, *, write_guard=None, config=UNSET, project_test=None,
                test_consent=None):
    """Shared proposal review; sending data never authorizes a filesystem write.

    ask supplies a policy-bound guard, repeated after confirmation and inside
    the executor lock. The standalone task flow retains its explicit selection.
    """
    source_guard = write_guard
    if project_test is not None:
        def write_guard():
            if source_guard is not None:
                source_guard()
            project_test.check()
    if write_guard is not None:
        write_guard()
    try:
        proposed = contract.proposal(response, source)
    except contract.UnchangedProposal:
        print("No change was proposed: the file already has this content. Nothing was saved.")
        if args.details:
            print("TASK: REFUSED reason=unchanged; project unchanged")
        return 2
    workspace.unchanged(before)
    print("\n" + ("=== PROPOSAL FROM %s ===" % recipient(config=config).upper() if args.details else "Proposal:"))
    print(render_answer(proposed["summary"], output_format=args.format))
    if args.details:
        print("=== TEST ON AN ISOLATED COPY ===", flush=True)
        print("Checking the changes on an isolated copy…", flush=True)
    if test_consent is not None:
        if type(test_consent) is not CopyTestConsent or project_test is not None:
            raise AnalysisDenied("consent")
        test_consent.consume(_test_scope(before, source, args), args.test)
    elif args.test in contract.MULTIPROCESS and project_test is None:
        print("Run %s on the selected files with a transient user cgroup, without network or host writes." % args.test)
        if not confirm("project-test", reader=reader):
            print("Test cancelled. Nothing was saved.")
            return 2
    originals = {path: info["file"] for path, info in before["files"].items() if info["file"] is not None
                 and info["file"]["content"] != contract.plain_text(info["file"]["content"])}
    if project_test is not None:
        result = project_test.run(source, proposed["files"], args.test, reader, source_guard)
    else:
        result = task_sandbox.run(source, proposed["files"], args.test, **({"originals": originals} if originals else {}))
    if args.details or not result["passed"]:
        print("Untrusted test output:")
        print(terminal_text(result["output"]))
        print("=== END OF TEST OUTPUT ===" if args.details else "End of test output.")
    if args.test == "text":
        print("UTF-8 text integrity: " + ("confirmed" if result["passed"] else "not confirmed") + ".")
        print("The syntax and behavior of this configuration or program were not checked.")
        if args.details:
            print("TASK TEXT INTEGRITY: " + ("PASS" if result["passed"] else "FAIL") + " syntax=NOT-TESTED behavior=NOT-TESTED")
    elif args.details:
        print("TASK TEST: " + ("PASS" if result["passed"] else "FAIL") + " check=" + args.test)
        print("Test on a copy: " + ("passed" if result["passed"] else "failed") + " (" + args.test + ").")
    else:
        print("Test on a copy: %s (%s); %s." % (
            "passed" if result["passed"] else "failed", args.test,
            "complete permitted project with proposed changes" if project_test is not None
            else "%d selected file(s)" % len(source["files"])))
        if args.test not in contract.TEST_SUITES:
            print("This checks syntax only; project tests were not run.")
    if args.test == "npm-test" and result["passed"]:
        print("The npm test script exited successfully. Its assertion count is unknown.")
    if not result["passed"]:
        print("TASK: REFUSED reason=test; project unchanged" if args.details
              else "The changes were not saved. Fix the task or the test and try again.")
        return 2
    if write_guard is not None:
        write_guard()
    workspace.unchanged(before)
    plan = TaskPlan(before, source, proposed, args.test)
    show_diff(plan, details=args.details)
    if args.details:
        if args.test != "text":
            print("The test covers the permitted project copy and proposed changes." if project_test is not None
                  else "The test covers only the selected files; it does not prove the whole project works.")
        print("Only the changes shown will be saved, and you can undo them.")
        print("Private undo copies are kept in .ai-desktop-tasks; they are never run on the host.")
        print("Plan SHA256:", plan.sha256)
    if not confirm("write", reader=reader):
        print("TASK: CANCELLED; project unchanged" if args.details else "Save cancelled. The project is unchanged.")
        return 2
    # The UI approves this exact immutable plan; the internal token never comes
    # from model text. The executor still consumes its nonce and expiry once.
    if write_guard is not None:
        write_guard()
    approval = plan.authorize("SAVE " + plan.sha256)
    try:
        workspace.apply(plan, approval, **({"write_guard": write_guard} if write_guard is not None else {}))
    except BaseException:
        print("The save was interrupted. Check ai-desktop task status and task rollback for this directory.")
        raise
    print("TASK: PASS test=" + args.test if args.details else "Changes saved.")
    print("Undo: " + shlex.join(["ai-desktop", "task", "rollback", "--workspace", args.workspace]))
    return 0


def show_diff(plan, *, details=False):
    """Print every changed line verbatim, including complete new files and EOF."""
    print("\n=== Changes to save ===")
    data = plan.data()
    rendered = []
    def lines(content):
        # Only LF terminates source lines. Unicode paragraph separators remain
        # exact file content and must not create false end-of-file markers.
        parts = content.split("\n")
        return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    for item in data["proposal"]["files"]:
        old = data["before"]["files"][item["path"]]["file"]
        old_content = contract.plain_text(old["content"]) if old else ""
        difference = difflib.unified_diff(lines(old_content), lines(item["content"]),
            fromfile="a/" + item["path"] if old else "/dev/null",
            tofile="b/" + item["path"])
        for line in difference:
            rendered.append(line if line.endswith("\n") else line + "\n")
            if not line.endswith("\n"):
                rendered.append("\\ No newline at end of file\n")
        if old and old["content"] != old_content:
            rendered.append("The file keeps its Windows line endings and BOM, if it has them.\n")
        if old is None and not item["content"]:
            rendered.append("--- /dev/null\n+++ b/" + item["path"] + "\nNew empty file (0 B).\n")
    print(fenced_code("".join(rendered), language="diff"))
    if details:
        for item in data["proposal"]["files"]:
            print("New content as JSON: " + item["path"])
            print(fenced_code(json.dumps(item["content"], ensure_ascii=False), language="json"))
    print()


def newest_first(records):
    """Newest entry first, so number 1 is the task the owner most likely means; equal times fall back to the plan ID."""
    return sorted(records, key=lambda record: (-record.get("modified", 0), record["plan"]))


def _when(record):
    if "modified" not in record:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(record["modified"])) + " · "


def show_record(record, number=None, *, details=False):
    prefix = str(number) + ". " if number is not None else ""
    states = {"applied": "saved", "rolled-back": "undone", "prepared": "save not finished",
              "conflict": "conflict — needs checking", "trashed": "moved to trash", "restored": "restored"}
    if record.get("kind") == "trash" and record["status"] == "prepared":
        states["prepared"] = "deletion not finished — check the task trash"
    states["detached"] = "unlinked history — undo is blocked"
    print(prefix + _when(record) + "State: " + (record["status"] if details else states.get(record["status"], record["status"])))
    if record["status"] == "detached":
        print("  " + workspace.DETACHED_HISTORY_HINT)
    for item in record["files"]:
        action = "Restore original" if item["action"] == "restore" else "Delete created file"
        print("  " + action + ": " + item["path"])
    if details:
        print("  Plan: " + record["plan"])
        print("  Journal SHA256: " + record["journal_sha256"])


def restore_deleted(args, reader):
    """Undo one deletion made by `ai-desktop project`: files return only to free names, never over newer files."""
    from . import workspace_trash
    print("Restoring files deleted in: " + json.dumps(args.workspace, ensure_ascii=False))
    print("Files go back only where the name is free; nothing will be overwritten.")
    if not confirm("restore", reader=reader):
        print("Cancelled. Nothing was restored.")
        return 2
    ok = workspace_trash.restore(args.workspace, args.plan)
    print("Deleted files restored." if ok else "Conflict: not all files were restored (a name is taken or the trash changed). The journal was kept.")
    return 0 if ok else 2


def rollback_task(args, records, reader):
    detached = [record for record in records if record["status"] == "detached"]
    if detached:
        print(workspace.DETACHED_HISTORY_HINT)
        if args.plan and any(record["plan"] == args.plan for record in detached):
            return 2
    candidates = [record for record in records if record["status"] != "detached" and
                  (record["plan"] == args.plan if args.plan else record["status"] != "rolled-back")]
    if not candidates:
        if args.plan:
            raise ValueError("unknown plan")
        print("No linked tasks to undo." if detached else "No tasks to undo in this directory.")
        return 2 if detached else 0
    expires = time.monotonic() + 180
    if len(candidates) > 1 or args.details:
        print("=== TASKS TO UNDO ===" if args.details else "Tasks to undo:")
        for index, record in enumerate(candidates, 1):
            show_record(record, index, details=args.details)
    if len(candidates) == 1:
        record = candidates[0]
    else:
        select_reader = fresh_input if reader is input else reader
        while True:
            answer = select_reader("Choose a task number, or type n/no to cancel: ").strip()
            if time.monotonic() > expires:
                raise ValueError("expired consent")
            if answer.casefold() in ("n", "no"):
                print("TASK ROLLBACK: CANCELLED; files unchanged" if args.details else "Cancelled. The files are unchanged.")
                return 2
            if answer in {str(index) for index in range(1, len(candidates) + 1)}:
                break
            print("Enter a number from 1 to " + str(len(candidates)) + " or n/no.")
        record = candidates[int(answer) - 1]
    if args.details:
        print("=== SELECTED UNDO ===")
    print("Undo in: " + json.dumps(args.workspace, ensure_ascii=False))
    show_record(record, details=args.details)
    print("This restores the original bytes and mode, or deletes the created file. Later changes cause a conflict.")
    if not confirm("rollback", reader=reader):
        print("TASK ROLLBACK: CANCELLED; files unchanged" if args.details else "Cancelled. The files are unchanged.")
        return 2
    if time.monotonic() > expires:
        raise ValueError("expired consent")
    ok = workspace.rollback(args.workspace, record["plan"], expected_journal=record["journal_sha256"])
    print(("TASK ROLLBACK: " + ("PASS" if ok else "CONFLICT (journal kept)")) if args.details
          else ("Changes undone." if ok else "Not undone: the file changed after the save, so its current version was kept. The journal was kept. Check: "
                + ", ".join(json.dumps(item["path"], ensure_ascii=False) for item in record["files"][:8])))
    return 0 if ok else 2


from .run_controls import configured


@configured
def main(argv=None, *, reader=input):
    args = build_parser().parse_args(argv)
    display_state = {"execution": UNSET}
    try:
        if os.geteuid() == 0 or os.getuid() != os.geteuid():
            raise ValueError("identity")
        if (args.action in ("rollback", "restore") or (args.action == "run" and not args.preview)) and not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise ValueError("tty")
        if os.path.exists(args.workspace) and not os.path.isdir(args.workspace):
            print("TASK: REFUSED; workspace-not-directory" if args.details
                  else "--workspace must point to a directory, but this is a file. Give the project directory; choose files with --file and --context.")
            return 2
        if args.action == "run":
            return run_task(args, reader, display_state=display_state)
        if args.action == "restore":
            return restore_deleted(args, reader)
        records = workspace.recovery_records(args.workspace)
        if args.action == "status":
            from . import workspace_trash
            import shlex
            records.extend(workspace_trash.recovery_records(args.workspace))
            records = newest_first(records)
            for index, record in enumerate(records, 1):
                show_record(record, index, details=args.details)
                if record.get("kind") == "trash" and record["status"] != "restored":
                    print("  Restore: " + shlex.join(["ai-desktop", "task", "restore", "--workspace", args.workspace,
                                                         "--plan", record["plan"]]))
            if args.details:
                print("TASK STATUS: PASS records=" + str(len(records)))
            elif not records:
                print("No tasks in this directory.")
            return 0
        return rollback_task(args, newest_first(records), reader)
    except workspace.JournalLimit:
        print("The limit of 128 saved tasks was reached. Nothing new was saved; history and undo are still available.")
    except AnalysisDenied as exc:
        if args.details:
            print(str(exc))
        print(failure_hint(exc.reason, HINTS.get(exc.reason, "The task was stopped by a safety check."),
                           config=display_state["execution"],
                           **({"diagnostic": exc.diagnostic} if exc.diagnostic is not None else {})))
    except (EOFError, KeyboardInterrupt):
        print("TASK: CANCELLED; no automatic retry" if args.details else "Cancelled. The task will not be retried automatically.")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError):
        print("TASK: REFUSED; check the file selection, model output, test and task status" if args.details
              else "The task was not finished. Check the file selection and ai-desktop task status.")
    return 2
