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
                   "agents", "agent", "account", "usage", "local", "panel", "sessions", "skills", "login", "doctor", "clients")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ai-desktop",
        description="AI Desktop: controlled tools for CachyOS (full list; short help: ai-desktop --help)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ask", help="Ask a question; optionally attach chosen context", add_help=False)
    commands.add_parser("material", help="Work with one local text file up to 5 MiB", add_help=False)
    commands.add_parser("project", help="Work with a project directory: read-only analysis, changes and deletions only after approval", add_help=False)
    commands.add_parser("settings", help="Service, agent, model, reasoning and Skills", add_help=False)
    commands.add_parser("claude", help="Compatibility: older Claude settings; use settings", add_help=False)
    commands.add_parser("codex", help="Compatibility: Codex administration; log in with login", add_help=False)
    commands.add_parser("clients", help="List, register, refresh or remove independently installed client copies", add_help=False)
    commands.add_parser("agents", help="Create and choose agent profiles", add_help=False)
    commands.add_parser("sessions", help="List and resume saved conversations", add_help=False)
    commands.add_parser("task", help="Change chosen files, test them, approve the diff or undo the save")

    commands.add_parser("audit", help="Local read-only audit")
    diagnostic_status = commands.add_parser("doctor", help="Check requirements without starting AI")
    diagnostic_status.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                                   help="Full requirements report as JSON")
    diagnostic_status.add_argument("--clients", action="store_true",
                                   help="Probe the selected system client offline, without login or AI")
    status = commands.add_parser("status", help="Show offline agent, policy and conversation status")
    status.add_argument("--json", action="store_true", help="Output machine-readable local status")
    commands.add_parser("panel", help="Open the interactive agent panel")
    commands.add_parser("account", help="Manage isolated subscription accounts", add_help=False)
    commands.add_parser("local", help="Query the local model catalog or server status", add_help=False)
    commands.add_parser("usage", help="Show cached subscription usage", add_help=False)
    commands.add_parser("stop", help="Stop or recover your own temporary analysis session")
    analysis = commands.add_parser("analyze", help="Approve a report and get an analysis from the selected provider")
    analysis.add_argument("--preview", action="store_true", help="Offline preview only, without the client or OAuth")
    analysis.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                          help="Full preview of the rules and analysis diagnostics")
    question = analysis.add_mutually_exclusive_group()
    question.add_argument("--question", default="", help="Your own question about the report (up to 2048 UTF-8 bytes)")
    question.add_argument("--ask", action="store_true", help="Type a question in the terminal before the preview and consent")
    analysis.add_argument("--report", help="Saved AI Desktop JSON report, used instead of collecting the current state")
    analysis.add_argument("--format", choices=("auto", "plain", "markdown"), default="auto",
                          help="Reply: terminal formatting, no colors, or raw Markdown")
    from .run_controls import add_arguments
    add_arguments(analysis)
    login = commands.add_parser("login", help="Log in to a provider's default account; named accounts use account login")
    target = login.add_mutually_exclusive_group()
    target.add_argument("--provider", choices=("claude", "codex"), help="Skip the menu; log in and choose the service")
    target.add_argument("--claude", "--allow-claude-login", dest="provider", action="store_const", const="claude",
                        help="Log in and choose Claude")
    target.add_argument("--codex", "--allow-chatgpt-login", dest="provider", action="store_const", const="codex",
                        help="Log in and choose ChatGPT / Codex")
    login.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                       help="Show diagnostics of the login unit")

    validation = commands.add_parser("validate-report", help="Validate an existing JSON file offline")
    validation.add_argument("path", help="Local JSON report, or - for stdin")

    comparison = commands.add_parser("compare", help="Compare two local JSON reports offline")
    comparison.add_argument("before", help="Earlier JSON report, or - for stdin")
    comparison.add_argument("after", help="Newer JSON report, or - for stdin")
    comparison.add_argument("--summary", action="store_true",
                            help="Show only the names of changed fields, without values")
    comparison.add_argument("--exit-on-change", action="store_true",
                            help="Exit with code 3 when the reports are valid but differ")

    diagnostic = commands.add_parser("diagnose", help="Diagnostics and optional AI analysis")
    diagnostic.add_argument("action", choices=("preview", "snapshot", "save", "ask"))
    diagnostic.add_argument("output", nargs="?", help="New JSON file, required for save")

    controlled = commands.add_parser("controller", help="Controlled diagnostics with an isolation gate")
    controlled.add_argument("action", choices=("preview", "approve", "ask"))

    isolated = commands.add_parser("executor", help="Safe offline Bubblewrap executor")
    isolated.add_argument("action", choices=("self-test", "inspect"))

    # Isolated, one-shot local transport only. Neither action escalates UID,
    # starts Claude, performs network IO or writes host files.
    handoff = commands.add_parser("handoff-once", help="Show and approve a report for a separate worker")
    handoff.add_argument("session", help="Random ID of the one-time session")
    commands.add_parser("handoff", help="Approve a report and start the offline worker through systemd")
    worker = commands.add_parser("worker-once", help="Receive one report as the ai-desktop user")
    worker.add_argument("session", help="Random ID of the one-time session")
    worker.add_argument("desktop_uid", type=int, help="Trusted UID of the desktop application")

    commands.add_parser("workspace", help="Plan, approve and save new files in the workspace",
                        add_help=False)

    skill = commands.add_parser("skills", help="Manage your own Skills")
    skill.add_argument("action", choices=("list", "validate", "show", "menu", "new"), nargs="?", default="list")
    skill.add_argument("name", nargs="?")

    for name, description, choices in (
        ("agent", "List or choose a service, or check the isolated Claude client", ("list", "use", "version", "help")),
        ("launcher", "Offline Claude in Bubblewrap", ("version", "help")),
        ("sandbox", "Check Bubblewrap isolation", ("self-test",)),
        ("skill-mount", "Check the isolated Skills mount", ("self-test",)),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("action", choices=choices)

    parser.command_names = frozenset(commands.choices)
    return parser


SHORT_HELP = """AI Desktop: controlled tools for CachyOS

Run ai-desktop in a terminal to start a conversation.
  ask        Ask a question; optionally attach chosen context
  material   Analyze one local text file (up to 5 MiB)
  project    Work with a project directory: read-only analysis; changes and deletions only after your approval
  task       Change chosen files with a test and a full diff; undo and restore
  status     Agent, permissions and live conversations (--json for widgets)
  panel      Interactive agent and account panel
  stop       Stop or recover your own temporary analysis session

  settings   Service, agent, model, reasoning and Skills
  agent      List or choose a service (agent list / agent use NAME)
  agents     Create, copy and choose agent roles
  account    Manage subscription accounts
  usage      Show subscription limits and reset times when available
  sessions   Saved conversations: preview, resume and delete
  skills     List and preview the available Skills
  login      Log in to the default Claude or Codex account
  doctor     Check requirements without starting AI
  clients    Inspect installed clients and manage their isolated copies

Full command list, including internal and developer tools: ai-desktop --help-all
Help for a command: ai-desktop COMMAND --help
"""


def login_menu(*, details=False):
    from .terminal_menu import choose, MenuInputError
    from .login_flow import login_status
    try:
        selected = choose("Log in and choose the active service",
                          ("Claude — subscription", "ChatGPT / Codex — subscription", "Cancel"))
    except MenuInputError:
        login_status("Sign-in failed", "red")
        print("  Run ai-desktop login in an interactive terminal.")
        return 2
    if selected is None or type(selected) is int and selected == 2:
        login_status("Sign-in cancelled", "yellow")
        return 0
    if type(selected) is not int or selected not in (0, 1):
        print("No login method was chosen.")
        return 2
    provider = "claude" if selected == 0 else "codex"
    return main(["login", "--provider", provider] + (["--v"] if details else []))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        argv = ["ask"] if sys.stdin.isatty() and sys.stdout.isatty() else ["status"]
    if argv in (["--help"], ["-h"]):                  # the short, task-oriented help; every command still works
        print(SHORT_HELP, end="")
        return 0
    if argv == ["--help-all"]:                        # the complete listing; text only, no host effects
        print(build_parser().format_help(), end="")
        return 0
    if argv[0] == "agent" and (len(argv) == 1 or argv[1] in ("list", "use", "-h", "--help")):
        from . import agent_panel
        return agent_panel.services(argv[1:] or ["list"])
    if argv[0] == "account":
        from . import account_cli
        return account_cli.main(argv[1:])
    if argv[0] == "local":
        from . import local_provider
        return local_provider.main(argv[1:])
    if argv[0] == "clients":
        from . import clients_cli
        return clients_cli.main(argv[1:])
    if argv[0] == "usage":
        from . import account_cli
        return account_cli.usage_main(argv[1:])
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
        print("Unknown command.", file=sys.stderr)
        if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", argv[0]):
            matches = difflib.get_close_matches(argv[0], PUBLIC_COMMANDS, n=1, cutoff=0.75)
            if matches:
                print("Did you mean: ai-desktop " + matches[0], file=sys.stderr)
        print("Help: ai-desktop --help", file=sys.stderr)
        raise SystemExit(2)
    args = parser.parse_args(argv)

    if args.command == "audit":
        print("\n".join(audit.audit_lines()))
        return 0
    if args.command == "doctor":
        return doctor.main((["--clients"] if args.clients else []) + (["--v"] if args.details else []))
    if args.command == "analyze":
        from . import analysis_cli
        from .agent_profiles import resolve
        from .execution import using
        try:
            selected = resolve(args.agent, args.model, args.effort,
                               **({"provider": args.provider} if args.provider is not None else {}))
            vanilla = (selected == {"provider": "claude", "model": "default", "effort": "default",
                                    "instructions": "", "profile": "default", "access": "changes"})
            with using(None if vanilla else selected):
                return analysis_cli.main(preview=args.preview, question=args.question, ask=args.ask,
                                         report_path=args.report, output_format=args.format, details=args.details)
        except (OSError, ValueError, TypeError, KeyError):
            print("Invalid agent configuration. Check ai-desktop agents list.")
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
        from .login_flow import authenticate_account, login_status
        result = authenticate_account("claude", "default", lambda: analysis_admin.main(allow=True, **options))
        if result == 0:
            login_status("Signed in", "green", "claude", "default")
            print("  Choose the active service from your regular user account.")
        return result
    if args.command == "status":
        from . import agent_panel
        return agent_panel.status(args.json)
    if args.command == "panel":
        from . import agent_panel
        return agent_panel.panel(main)
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

    raise AssertionError("Unknown command")
