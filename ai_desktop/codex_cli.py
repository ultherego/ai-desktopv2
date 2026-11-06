"""Explicit administrative account lifecycle, separate from inference consent."""
import argparse
import os
import subprocess

from .login_flow import authenticate_account, login_status


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-desktop codex", description="Codex with a ChatGPT subscription")
    commands = parser.add_subparsers(dest="action")
    commands.add_parser("status", help="Offline readiness, without reading OAuth")
    login = commands.add_parser("login", help="Connect a separate profile to a ChatGPT subscription")
    login.add_argument("--allow-chatgpt-login", action="store_true", help="Explicit consent to log in with the subscription")
    logout = commands.add_parser("logout", help="Disconnect the profile and revoke the integration credential")
    logout.add_argument("--allow-chatgpt-logout", action="store_true", help="Explicit consent to disconnect the profile")
    for command in (login, logout):
        command.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                             help="Show diagnostics of the login unit")
    args = parser.parse_args(argv)
    from .analysis_contract import AnalysisDenied
    from .analysis_sandbox import INSTALL, trusted_file
    try:
        if args.action in (None, "status"):
            from .codex_runtime import runtime_ready
            runtime_ready()
            print("System Codex: files ready; version, protocol, account and limits are not checked offline.")
            print("Log in: ai-desktop login (choose ChatGPT / Codex)")
            return 0
        allowed = getattr(args, "allow_chatgpt_" + args.action, False)
        if not allowed or not os.isatty(0) or not os.isatty(1):
            if args.action == "login":
                login_status("Sign-in failed", "red", "codex", "default")
            print("This needs a terminal and the explicit flag --allow-chatgpt-%s." % args.action)
            return 2
        if os.geteuid() == 0:
            from .codex_admin import main as admin
            options = {"details": True} if args.details else {}
            if args.action == "login":
                result = authenticate_account("codex", "default", lambda: admin("login", allow=True, **options))
                if result == 0:
                    login_status("Signed in", "green", "codex", "default")
                    print("  Choose the active service from your regular user account.")
                return result
            return admin(args.action, allow=True, **options)
        if os.getuid() != os.geteuid():
            raise AnalysisDenied("identity")
        from .codex_runtime import runtime_ready
        if args.action == "login":
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
        from .account_cli import _check_or_logout
        from .account_store import AccountStore
        return _check_or_logout(AccountStore(), "codex", "default", "logout", authorized=authenticate)
    except (AnalysisDenied, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
        if args.action == "login":
            login_status("Sign-in failed", "red", "codex", "default")
        print("Codex is not ready. Check the client, the installation and permissions; nothing is retried automatically.")
        return 2
