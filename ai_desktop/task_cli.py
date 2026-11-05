"""User instruction -> selected data consent -> proposal -> test -> write consent."""
import argparse
import difflib
import json
import os
from pathlib import Path
import pwd
import shlex
import sys
import time

from .run_controls import recipient, failure_hint, wait_message
from .analysis_cli import HINTS
from .analysis_contract import AnalysisConsent, AnalysisDenied, CLIENT_CONTEXT, SYSTEM_PROMPT, terminal_text
from .analysis_service import send_once
from .analysis_session import installation_ready, managed_session
from .analysis_terminal import fenced_code, render_answer
from . import task_contract as contract, task_sandbox, task_workspace as workspace
from .task_plan import TaskPlan
from .terminal_consent import confirm, fresh_input
from .execution import UNSET


def build_parser():
    from .run_controls import add_arguments
    parser = argparse.ArgumentParser(prog="ai-desktop task", description="Zmiana wybranych plikow i test w odizolowanej kopii")
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run", help="Polecenie -> propozycja -> test -> diff -> zgoda na zapis")
    add_arguments(run)
    run.add_argument("instruction", help="Zadanie dla wybranego asystenta, bez dowolnego terminala modelu")
    run.add_argument("--workspace", required=True, help="Konkretny katalog projektu (nie cały HOME)")
    run.add_argument("--file", action="append", required=True, help="Względny plik do edycji lub utworzenia; powtarzalne")
    run.add_argument("--context", action="append", default=[], help="Plik tylko do odczytu, np. niezależne testy")
    run.add_argument("--test", choices=task_sandbox.CHECKS, required=True)
    run.add_argument("--preview", action="store_true", help="Podgląd danych offline, bez AI, testów i zapisów")
    run.add_argument("--format", choices=("auto", "plain", "markdown"), default="auto")
    status = commands.add_parser("status", help="Odczytaj metadane dziennika zadania")
    status.add_argument("--workspace", required=True)
    undo = commands.add_parser("rollback", help="Cofnij zatwierdzone zapisy; odmowa przy późniejszej edycji")
    undo.add_argument("--workspace", required=True)
    undo.add_argument("--plan", help="Opcjonalny identyfikator; bez niego wybierzesz zadanie z listy")
    restore = commands.add_parser("restore", help="Przywróć pliki usunięte w trybie project (kosz zadania)")
    restore.add_argument("--workspace", required=True)
    restore.add_argument("--plan", required=True, help="Identyfikator usunięcia z komunikatu po usunięciu")
    for command in (run, status, undo, restore):
        command.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                             help="Pokaż pełne dane, identyfikatory i diagnostykę")
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
    gate = AnalysisConsent(None, task=source)
    selected = json.loads(gate.payload).get("execution")
    # Presentation only: authorization still checks the live execution binding.
    if display_state is not None:
        display_state["execution"] = selected
    if args.details:
        print("=== WYBRANE DANE DO %s ===" % recipient(config=selected).upper())
        print(json.dumps(source, ensure_ascii=False, indent=2))
        print("=== INSTRUKCJA APLIKACJI ===\n" + SYSTEM_PROMPT)
        print("=== ZASADY I ODBIORCA ===\n" + json.loads(gate.payload)["client_context"])
        print("SHA256:", gate.sha256)
    else:
        print("Zadanie: " + source["instruction"])
        print(recipient("Do Claude: instrukcja, wybrane ścieżki i pełna treść istniejących plików:", config=selected))
        for item in source["files"]:
            action = ("utworzenie" if item["content"] is None else "edycja") if item["writable"] else "tylko odczyt"
            size = 0 if item["content"] is None else len(item["content"].encode("utf-8"))
            print("  " + item["path"] + " — " + action + ", " + str(size) + " B")
        print("Pełny podgląd danych: --v.")
    if args.details:
        print("Test na kopii:", args.test, "(bez sieci, HOME i zapisow do projektu)")
    if args.preview:
        print("TASK PREVIEW: PASS sha256=" + gate.sha256 if args.details
              else "Podgląd gotowy. Niczego nie wysłano ani nie zapisano.")
        return 0
    installation_ready()
    if not confirm("transfer", reader=reader, config=selected):
        raise AnalysisDenied("consent")
    gate.check(None, task=source, sha256=gate.sha256, nonce=gate.nonce)
    workspace.unchanged(before)
    with managed_session(**({} if args.details else {"quiet": True})):
        approval = gate.authorize(None, task=source, sha256=gate.sha256, nonce=gate.nonce)
        workspace.unchanged(before)
        print(wait_message(config=selected), flush=True)
        response = send_once(approval)
    if args.details:
        print("SESSION: STOPPED")
    return finish_task(before, source, response, args, reader, config=selected)


def finish_task(before, source, response, args, reader, *, write_guard=None, config=UNSET):
    """Shared proposal review; sending data never authorizes a filesystem write.

    ask supplies a policy-bound guard, repeated after confirmation and inside
    the executor lock. The standalone task flow retains its explicit selection.
    """
    if write_guard is not None:
        write_guard()
    try:
        proposed = contract.proposal(response, source)
    except contract.UnchangedProposal:
        print("Nie zaproponowano zmiany: plik ma już taką treść. Nic nie zapisano.")
        if args.details:
            print("TASK: REFUSED reason=unchanged; projekt niezmieniony")
        return 2
    workspace.unchanged(before)
    print("\n" + ("=== PROPOZYCJA %s ===" % recipient(config=config).upper() if args.details else "Propozycja:"))
    print(render_answer(proposed["summary"], output_format=args.format))
    if args.details:
        print("=== TEST NA ODIZOLOWANEJ KOPII ===", flush=True)
        print("Sprawdzam zmiany na odizolowanej kopii…", flush=True)
    result = task_sandbox.run(source, proposed["files"], args.test)
    if args.details or not result["passed"]:
        print("Niezaufany output testu:")
        print(terminal_text(result["output"]))
        print("=== KONIEC OUTPUTU TESTU ===" if args.details else "Koniec wyniku testu.")
    if args.test == "text":
        print("Integralność tekstu UTF-8: " + ("potwierdzona" if result["passed"] else "niepotwierdzona") + ".")
        print("Nie sprawdzono składni ani działania tej konfiguracji lub programu.")
        if args.details:
            print("TASK TEXT INTEGRITY: " + ("PASS" if result["passed"] else "FAIL") + " syntax=NOT-TESTED behavior=NOT-TESTED")
    elif args.details:
        print("TASK TEST: " + ("PASS" if result["passed"] else "FAIL") + " check=" + args.test)
        print("Test na kopii: " + ("zaliczony" if result["passed"] else "niezaliczony") + " (" + args.test + ").")
    elif not result["passed"]:
        print("Test na kopii: niezaliczony (" + args.test + ").")
    if not result["passed"]:
        print("TASK: REFUSED reason=test; projekt niezmieniony" if args.details
              else "Nie zapisano zmian. Popraw zadanie lub test i spróbuj ponownie.")
        return 2
    if write_guard is not None:
        write_guard()
    workspace.unchanged(before)
    plan = TaskPlan(before, source, proposed, args.test)
    show_diff(plan, details=args.details)
    if args.details:
        if args.test != "text":
            print("Test obejmuje tylko wybrane pliki; nie potwierdza poprawności całego projektu.")
        print("Zapis obejmie tylko pokazane zmiany; będzie można je cofnąć.")
        print("Prywatne kopie cofania w .ai-desktop-tasks; bez uruchamiania ich na hoście.")
        print("SHA256 planu:", plan.sha256)
    if not confirm("write", reader=reader):
        print("TASK: CANCELLED; projekt niezmieniony" if args.details else "Anulowano zapis. Projekt bez zmian.")
        return 2
    # The UI approves this exact immutable plan; the internal token never comes
    # from model text. The executor still consumes its nonce and expiry once.
    if write_guard is not None:
        write_guard()
    approval = plan.authorize("ZAPISZ " + plan.sha256)
    try:
        workspace.apply(plan, approval, **({"write_guard": write_guard} if write_guard is not None else {}))
    except BaseException:
        print("Zapis przerwany. Sprawdź ai-desktop task status i task rollback dla tego katalogu.")
        raise
    print("TASK: PASS test=" + args.test if args.details else "Zapisano zmiany.")
    print("Cofnięcie: " + shlex.join(["ai-desktop", "task", "rollback", "--workspace", args.workspace]))
    return 0


def show_diff(plan, *, details=False):
    """Print every changed line verbatim, including complete new files and EOF."""
    print("\n=== Zmiany do zapisania ===")
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
                rendered.append("\\ Brak znaku nowej linii na końcu pliku\n")
        if old and old["content"] != old_content:
            rendered.append("Plik zachowa swoje końce linii Windows i znak BOM, jeśli je ma.\n")
        if old is None and not item["content"]:
            rendered.append("--- /dev/null\n+++ b/" + item["path"] + "\nNowy pusty plik (0 B).\n")
    print(fenced_code("".join(rendered), language="diff"))
    if details:
        for item in data["proposal"]["files"]:
            print("Nowa tresc JSON: " + item["path"])
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
    states = {"applied": "zapisano", "rolled-back": "cofnięto", "prepared": "zapis niedokończony",
              "conflict": "konflikt — wymaga sprawdzenia", "trashed": "usunięto do kosza", "restored": "przywrócono"}
    if record.get("kind") == "trash" and record["status"] == "prepared":
        states["prepared"] = "usuwanie niedokończone — sprawdź kosz zadania"
    states["detached"] = "historia niepowiązana — cofnięcie zablokowane"
    print(prefix + _when(record) + "Stan: " + (record["status"] if details else states.get(record["status"], record["status"])))
    if record["status"] == "detached":
        print("  " + workspace.DETACHED_HISTORY_HINT)
    for item in record["files"]:
        action = "Przywróć oryginał" if item["action"] == "restore" else "Usuń utworzony plik"
        print("  " + action + ": " + item["path"])
    if details:
        print("  Plan: " + record["plan"])
        print("  SHA256 dziennika: " + record["journal_sha256"])


def restore_deleted(args, reader):
    """Undo one deletion made by `ai-desktop project`: files return only to free names, never over newer files."""
    from . import workspace_trash
    print("Przywrócenie plików usuniętych w: " + json.dumps(args.workspace, ensure_ascii=False))
    print("Pliki wrócą na swoje miejsca tylko tam, gdzie nazwa jest wolna; nic nie zostanie nadpisane.")
    if not confirm("restore", reader=reader):
        print("Anulowano. Nic nie przywrócono.")
        return 2
    ok = workspace_trash.restore(args.workspace, args.plan)
    print("Przywrócono usunięte pliki." if ok else "Konflikt: nie przywrócono wszystkich plików (nazwa zajęta lub kosz zmieniony). Dziennik zachowano.")
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
        print("Brak powiązanych zadań do cofnięcia." if detached else "Brak zadań do cofnięcia w tym katalogu.")
        return 2 if detached else 0
    expires = time.monotonic() + 180
    if len(candidates) > 1 or args.details:
        print("=== ZADANIA DO COFNIECIA ===" if args.details else "Zadania do cofnięcia:")
        for index, record in enumerate(candidates, 1):
            show_record(record, index, details=args.details)
    if len(candidates) == 1:
        record = candidates[0]
    else:
        select_reader = fresh_input if reader is input else reader
        while True:
            answer = select_reader("Wybierz numer zadania lub wpisz n/nie, aby anulować: ").strip()
            if time.monotonic() > expires:
                raise ValueError("expired consent")
            if answer.casefold() in ("n", "nie"):
                print("TASK ROLLBACK: CANCELLED; pliki niezmienione" if args.details else "Anulowano. Pliki bez zmian.")
                return 2
            if answer in {str(index) for index in range(1, len(candidates) + 1)}:
                break
            print("Podaj numer od 1 do " + str(len(candidates)) + " albo n/nie.")
        record = candidates[int(answer) - 1]
    if args.details:
        print("=== WYBRANE COFNIECIE ===")
    print("Cofnięcie w: " + json.dumps(args.workspace, ensure_ascii=False))
    show_record(record, details=args.details)
    print("Przywróci oryginalne bajty i tryb albo usunie utworzony plik. Późniejsze zmiany powodują konflikt.")
    if not confirm("rollback", reader=reader):
        print("TASK ROLLBACK: CANCELLED; pliki niezmienione" if args.details else "Anulowano. Pliki bez zmian.")
        return 2
    if time.monotonic() > expires:
        raise ValueError("expired consent")
    ok = workspace.rollback(args.workspace, record["plan"], expected_journal=record["journal_sha256"])
    print(("TASK ROLLBACK: " + ("PASS" if ok else "CONFLICT (dziennik zachowany)")) if args.details
          else ("Cofnięto zmiany." if ok else "Nie cofnięto: plik zmieniono po zapisie, więc zostawiono jego obecną wersję. Dziennik zachowano. Sprawdź: "
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
                  else "--workspace musi wskazywać katalog, a to jest plik. Podaj katalog projektu; pliki wybierasz przez --file i --context.")
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
                    print("  Przywrócenie: " + shlex.join(["ai-desktop", "task", "restore", "--workspace", args.workspace,
                                                         "--plan", record["plan"]]))
            if args.details:
                print("TASK STATUS: PASS records=" + str(len(records)))
            elif not records:
                print("Brak zadań w tym katalogu.")
            return 0
        return rollback_task(args, newest_first(records), reader)
    except workspace.JournalLimit:
        print("Osiągnięto limit 128 zapisanych zadań. Nowego zapisu nie wykonano; historia i cofanie pozostają dostępne.")
    except AnalysisDenied as exc:
        if args.details:
            print(str(exc))
        print(failure_hint(exc.reason, HINTS.get(exc.reason, "Zadanie zatrzymane przez bramę bezpieczeństwa."),
                           config=display_state["execution"],
                           **({"diagnostic": exc.diagnostic} if exc.diagnostic is not None else {})))
    except (EOFError, KeyboardInterrupt):
        print("TASK: CANCELLED; brak automatycznego ponowienia" if args.details else "Anulowano. Zadanie nie zostanie ponowione automatycznie.")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError):
        print("TASK: REFUSED; sprawdz wybor plikow, wynik modelu, test i task status" if args.details
              else "Nie ukończono zadania. Sprawdź wybór plików i ai-desktop task status.")
    return 2
