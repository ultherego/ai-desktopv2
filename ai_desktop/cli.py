"""Single, explicit command dispatcher for AI Desktop.

No system probes or Claude sessions are started merely by importing this module.
"""
import argparse
import difflib
import os
import re
import sys

from . import __version__
from . import agent, audit, compare, controller, diagnose, doctor, e2_handoff_cli, executor, launcher, sandbox, skill_mount, skills, validate_snapshot, workspace_cli

PUBLIC_COMMANDS = ("ask", "material", "project", "task", "status", "stop", "settings",
                   "agents", "sessions", "skills", "login", "doctor")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ai-desktop",
        description="AI Desktop: kontrolowane narzędzia dla CachyOS (pełna lista; krótka pomoc: ai-desktop --help)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ask", help="Zadaj pytanie; opcjonalnie dołącz wybrany kontekst", add_help=False)
    commands.add_parser("material", help="Pracuj z jednym lokalnym plikiem tekstowym do 5 MiB", add_help=False)
    commands.add_parser("project", help="Pracuj z katalogiem projektu: analiza do odczytu, zmiany i usuwanie dopiero po zatwierdzeniu", add_help=False)
    commands.add_parser("settings", help="Usługa, agent, model, rozumowanie i Skills", add_help=False)
    commands.add_parser("claude", help="Zgodność: starsze ustawienia Claude; użyj settings", add_help=False)
    commands.add_parser("codex", help="Zgodność: administracja Codex; logowanie przez login", add_help=False)
    commands.add_parser("agents", help="Twórz i wybieraj profile agentów", add_help=False)
    commands.add_parser("sessions", help="Lista i wznawianie zapisanych rozmów", add_help=False)
    commands.add_parser("task", help="Zmien wybrane pliki, przetestuj, zatwierdz diff lub cofnij zapis")

    commands.add_parser("audit", help="Lokalny audyt tylko do odczytu")
    diagnostic_status = commands.add_parser("doctor", help="Sprawdzenie wymagań bez uruchamiania AI")
    diagnostic_status.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                                   help="Pełny raport wymagań w JSON")
    commands.add_parser("status", help="Gotowosc analizy offline, bez klienta i OAuth")
    commands.add_parser("stop", help="Zatrzymaj lub odzyskaj wlasna tymczasowa sesje analizy")
    analysis = commands.add_parser("analyze", help="Zatwierdz raport i odbierz analize subskrypcji Claude")
    analysis.add_argument("--preview", action="store_true", help="Tylko podglad offline, bez klienta i OAuth")
    analysis.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                          help="Pełny podgląd zasad i diagnostyka analizy")
    question = analysis.add_mutually_exclusive_group()
    question.add_argument("--question", default="", help="Wlasne pytanie o raport (do 2048 bajtow UTF-8)")
    question.add_argument("--ask", action="store_true", help="Wpisz pytanie w terminalu przed podgladem i zgoda")
    analysis.add_argument("--report", help="Zapisany raport JSON AI Desktop; zamiast zbierania biezacego stanu")
    analysis.add_argument("--format", choices=("auto", "plain", "markdown"), default="auto",
                          help="Odpowiedz: formatowanie terminala, bez kolorow lub surowy Markdown")
    from .run_controls import add_arguments
    add_arguments(analysis)
    login = commands.add_parser("login", help="Logowanie i wybór aktywnej usługi Claude lub Codex")
    target = login.add_mutually_exclusive_group()
    target.add_argument("--provider", choices=("claude", "codex"), help="Pomiń menu; zaloguj i wybierz usługę")
    target.add_argument("--claude", "--allow-claude-login", dest="provider", action="store_const", const="claude",
                        help="Zaloguj i wybierz Claude")
    target.add_argument("--codex", "--allow-chatgpt-login", dest="provider", action="store_const", const="codex",
                        help="Zaloguj i wybierz ChatGPT / Codex")
    login.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                       help="Pokaż diagnostykę jednostki logowania")

    validation = commands.add_parser("validate-report", help="Waliduj istniejacy plik JSON offline")
    validation.add_argument("path", help="Lokalny raport JSON lub - dla stdin")

    comparison = commands.add_parser("compare", help="Porownaj dwa lokalne raporty JSON offline")
    comparison.add_argument("before", help="Poprzedni raport JSON lub - dla stdin")
    comparison.add_argument("after", help="Nowszy raport JSON lub - dla stdin")
    comparison.add_argument("--summary", action="store_true",
                            help="Pokaz tylko nazwy zmienionych pol, bez wartosci")
    comparison.add_argument("--exit-on-change", action="store_true",
                            help="Zakoncz kodem 3, gdy raporty sa poprawne, ale sie roznia")

    diagnostic = commands.add_parser("diagnose", help="Diagnostyka i opcjonalna analiza AI")
    diagnostic.add_argument("action", choices=("preview", "snapshot", "save", "ask"))
    diagnostic.add_argument("output", nargs="?", help="Nowy plik JSON wymagany dla save")

    controlled = commands.add_parser("controller", help="Kontrolowana diagnostyka z bramą izolacji")
    controlled.add_argument("action", choices=("preview", "approve", "ask"))

    isolated = commands.add_parser("executor", help="Bezpieczny wykonawca offline Bubblewrap")
    isolated.add_argument("action", choices=("self-test", "inspect"))

    # Isolated, one-shot local transport only. Neither action escalates UID,
    # starts Claude, performs network IO or writes host files.
    handoff = commands.add_parser("handoff-once", help="Pokaz i zatwierdz raport do odrebnego workera")
    handoff.add_argument("session", help="Losowy identyfikator jednorazowej sesji")
    commands.add_parser("handoff", help="Zatwierdz raport i aktywuj offline worker przez systemd")
    worker = commands.add_parser("worker-once", help="Odbierz jeden raport jako uzytkownik ai-desktop")
    worker.add_argument("session", help="Losowy identyfikator jednorazowej sesji")
    worker.add_argument("desktop_uid", type=int, help="Zaufany UID aplikacji pulpitu")

    commands.add_parser("workspace", help="Plan, zatwierdzenie i zapis nowych plikow w workspace",
                        add_help=False)

    skill = commands.add_parser("skills", help="Zarządzanie własnymi Skills")
    skill.add_argument("action", choices=("list", "validate", "show", "menu", "new"), nargs="?", default="list")
    skill.add_argument("name", nargs="?")

    for name, description, choices in (
        ("agent", "Offline Claude z izolowanym Skill", ("version", "help")),
        ("launcher", "Offline Claude w Bubblewrap", ("version", "help")),
        ("sandbox", "Weryfikacja izolacji Bubblewrap", ("self-test",)),
        ("skill-mount", "Weryfikacja izolowanego montowania Skills", ("self-test",)),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("action", choices=choices)

    parser.command_names = frozenset(commands.choices)
    return parser


SHORT_HELP = """AI Desktop: kontrolowane narzędzia dla CachyOS

Codzienna praca:
  ask        Zadaj pytanie; opcjonalnie dołącz wybrany kontekst
  material   Analiza jednego lokalnego pliku tekstowego (do 5 MiB)
  project    Praca z katalogiem projektu: analiza do odczytu; zmiany i usuwanie dopiero po Twoim zatwierdzeniu
  task       Zmiana wybranych plików z testem i pełnym diffem; cofanie i przywracanie
  status     Gotowość analizy offline, bez klienta i OAuth
  stop       Zatrzymaj lub odzyskaj własną tymczasową sesję analizy

Konfiguracja i diagnostyka:
  settings   Usługa, agent, model, rozumowanie i Skills
  agents     Tworzenie, kopiowanie i wybór profili agentów
  sessions   Zapisane rozmowy: podgląd, wznowienie i usuwanie
  skills     Lista i podgląd dostępnych Skills
  login      Logowanie i wybór Claude lub ChatGPT / Codex
  doctor     Sprawdzenie wymagań bez uruchamiania AI

Pełna lista poleceń, także narzędzia wewnętrzne i deweloperskie: ai-desktop --help-all
Pomoc polecenia: ai-desktop POLECENIE --help
"""


def login_menu(*, details=False):
    from .terminal_menu import choose, MenuInputError
    try:
        selected = choose("Logowanie i wybór aktywnej usługi",
                          ("Claude — subskrypcja", "ChatGPT / Codex — subskrypcja", "Anuluj"))
    except MenuInputError:
        print("Nie udało się odczytać wyboru. Uruchom w interaktywnym terminalu: ai-desktop login")
        return 2
    if selected is None or type(selected) is int and selected == 2:
        print("Anulowano logowanie.")
        return 0
    if type(selected) is not int or selected not in (0, 1):
        print("Nie wybrano sposobu logowania.")
        return 2
    provider = "claude" if selected == 0 else "codex"
    print("Logowanie: " + ("Claude" if selected == 0 else "ChatGPT / Codex"))
    return main(["login", "--provider", provider] + (["--v"] if details else []))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        argv = ["status"]
    if argv in (["--help"], ["-h"]):                  # the short, task-oriented help; every command still works
        print(SHORT_HELP, end="")
        return 0
    if argv == ["--help-all"]:                        # the complete listing; text only, no host effects
        print(build_parser().format_help(), end="")
        return 0
    if argv[:1] == ["workspace"]:
        return workspace_cli.main(argv[1:])
    if argv[:1] == ["settings"]:
        from . import management_cli
        return management_cli.main(argv[1:], prog="ai-desktop settings", provider_neutral=True)
    if argv[:1] in (["claude"], ["agents"]):
        from . import management_cli
        return management_cli.main((["agents"] if argv[0] == "agents" else []) + argv[1:])
    if argv[:1] == ["codex"]:
        from .codex_cli import main as codex_main
        return codex_main(argv[1:])
    if argv[:1] == ["sessions"]:
        from . import session_cli
        return session_cli.main(argv[1:], dispatch=main)
    if argv[:1] == ["task"]:
        from . import task_cli
        return task_cli.main(argv[1:])
    if argv[:1] == ["ask"]:
        from . import ask_cli
        return ask_cli.main(argv[1:])
    if argv[:1] == ["material"]:
        from . import material_cli
        return material_cli.main(argv[1:])
    if argv[:1] == ["project"]:
        from . import project_cli
        return project_cli.main(argv[1:])
    parser = build_parser()
    if argv and not argv[0].startswith("-") and argv[0] not in parser.command_names:
        # Never echo untrusted terminal text or execute a guessed command.
        print("Nieznane polecenie.", file=sys.stderr)
        if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", argv[0]):
            matches = difflib.get_close_matches(argv[0], PUBLIC_COMMANDS, n=1, cutoff=0.75)
            if matches:
                print("Podpowiedź: ai-desktop " + matches[0], file=sys.stderr)
        print("Pomoc: ai-desktop --help", file=sys.stderr)
        raise SystemExit(2)
    args = parser.parse_args(argv)

    if args.command == "audit":
        print("\n".join(audit.audit_lines()))
        return 0
    if args.command == "doctor":
        return doctor.main(["--v"] if args.details else [])
    if args.command == "analyze":
        from . import analysis_cli
        from .agent_profiles import resolve
        from .execution import using
        try:
            selected = resolve(args.agent, args.model, args.effort)
            vanilla = (selected == {"provider": "claude", "model": "default", "effort": "default",
                                    "instructions": "", "profile": "default", "access": "changes"})
            with using(None if vanilla else selected):
                return analysis_cli.main(preview=args.preview, question=args.question, ask=args.ask,
                                         report_path=args.report, output_format=args.format, details=args.details)
        except (OSError, ValueError, TypeError, KeyError):
            print("Niepoprawna konfiguracja agenta. Sprawdź ai-desktop agents list.")
            return 2
    if args.command == "login":
        if args.provider is None:
            return login_menu(details=args.details)
        if args.provider == "codex":
            from .codex_cli import main as codex_main
            return codex_main(["login", "--allow-chatgpt-login"] + (["--v"] if args.details else []))
        options = {"details": True} if args.details else {}
        if os.geteuid() != 0:
            from .analysis_session import administrative
            from .login_flow import login
            return login("claude", lambda: administrative("login", allow=True, **options))
        from . import analysis_admin
        result = analysis_admin.main(allow=True, **options)
        if result == 0:
            print("Zalogowano: Claude. Usługę wybierz w ustawieniach zwykłego użytkownika.")
        return result
    if args.command == "status":
        from .analysis_session import status
        return status()
    if args.command == "stop":
        from .analysis_session import administrative
        return administrative("stop")
    if args.command == "validate-report":
        return validate_snapshot.main([args.path])
    if args.command == "compare":
        params = []
        if args.summary:
            params.append("--summary")
        if args.exit_on_change:
            params.append("--exit-on-change")
        return compare.main(params + [args.before, args.after])
    if args.command == "diagnose":
        params = [args.action]
        if args.output is not None:
            params.append(args.output)
        return diagnose.main(params)
    if args.command == "controller":
        return controller.main([args.action])
    if args.command == "handoff-once":
        return e2_handoff_cli.desktop_main(args.session)
    if args.command == "handoff":
        return e2_handoff_cli.desktop_main()
    if args.command == "worker-once":
        return e2_handoff_cli.worker_main(args.session, args.desktop_uid)
    if args.command == "executor":
        return executor.main([args.action])
    if args.command == "skills":
        params = [args.action]
        if args.name is not None:
            params.append(args.name)
        return skills.main(params)
    if args.command == "agent":
        return agent.main([args.action])
    if args.command == "launcher":
        return launcher.main([args.action])
    if args.command == "sandbox":
        return sandbox.main([args.action])
    if args.command == "skill-mount":
        return skill_mount.main([args.action])

    raise AssertionError("Nieznane polecenie")
