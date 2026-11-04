"""Explicit administrative account lifecycle, separate from inference consent."""
import argparse
import os
import subprocess


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-desktop codex", description="Codex z subskrypcją ChatGPT")
    commands = parser.add_subparsers(dest="action")
    commands.add_parser("status", help="Gotowość offline, bez odczytu OAuth")
    login = commands.add_parser("login", help="Połącz osobny profil z subskrypcją ChatGPT")
    login.add_argument("--allow-chatgpt-login", action="store_true", help="Jawna zgoda na logowanie subskrypcji")
    logout = commands.add_parser("logout", help="Odłącz profil i odwołaj poświadczenie integracji")
    logout.add_argument("--allow-chatgpt-logout", action="store_true", help="Jawna zgoda na odłączenie profilu")
    for command in (login, logout):
        command.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                             help="Pokaż diagnostykę jednostki logowania")
    args = parser.parse_args(argv)
    from .analysis_contract import AnalysisDenied
    from .analysis_sandbox import INSTALL, trusted_file
    try:
        if args.action in (None, "status"):
            from .codex_runtime import runtime_ready, CLIENT_VERSION as CODEX_VERSION
            runtime_ready()
            print("Codex %s: pliki gotowe; konto i limity niesprawdzone offline." % CODEX_VERSION)
            print("Logowanie: ai-desktop login (wybierz ChatGPT / Codex)")
            return 0
        allowed = getattr(args, "allow_chatgpt_" + args.action, False)
        if not allowed or not os.isatty(0) or not os.isatty(1):
            print("Wymagany terminal i jawna flaga --allow-chatgpt-%s." % args.action)
            return 2
        if os.geteuid() == 0:
            from .codex_admin import main as admin
            result = admin(args.action, allow=True, **({"details": True} if args.details else {}))
            if result == 0 and args.action == "login":
                print("Zalogowano: Codex. Usługę wybierz w ustawieniach zwykłego użytkownika.")
            return result
        if os.getuid() != os.geteuid():
            raise AnalysisDenied("identity")
        from .codex_runtime import runtime_ready
        runtime_ready()
        entry = trusted_file(INSTALL / "codex-auth.py")
        action = args.action + ("-verbose" if args.details else "")
        def authenticate():
            return subprocess.run(["/usr/bin/sudo", "--", "/usr/bin/python3", "-I", "-B", str(entry), action],
                                  env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"},
                                  close_fds=True, timeout=650, check=False).returncode
        if args.action == "login":
            from .login_flow import login as complete_login
            return complete_login("codex", authenticate)
        return authenticate()
    except (AnalysisDenied, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
        print("Codex nie jest gotowy. Sprawdź klienta, instalację i uprawnienia; niczego nie ponawiam automatycznie.")
        return 2
