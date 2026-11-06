"""Fixed service-UID OAuth worker; tokens never cross the desktop IPC."""
import http.server
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.parse import urlsplit

from .analysis_contract import AnalysisDenied
from . import account_runtime
from .analysis_sandbox import HOME, INSTALL, trusted_file


# A worker's local outcomes must not collide with an LSB/launcher error returned
# by systemd-run itself. Expose the small CLI vocabulary only at the admin boundary.
_WORKER_RESULTS = {4: 90, 5: 91, 6: 92}


def worker_exit(result):
    return _WORKER_RESULTS.get(result, 0 if result == 0 else 2)


def auth_worker(action, *, details=False):
    from .e2_socket_activation import _worker_identity
    from .siwc_auth import AuthError
    if (type(details) is not bool or not _worker_identity() or not os.isatty(0) or not os.isatty(1)
            or action not in ("login", "logout")):
        print("Codex: refused to start the login.")
        return 2
    from .analysis_sandbox import service_identity, profile_lock
    try:
        service_identity()
        with profile_lock():
            if action == "logout":
                return _account_action("logout", "codex")
            return _codex_login(details)
    except (AuthError, AnalysisDenied, OSError, ValueError, KeyboardInterrupt):
        print("Codex: the account operation did not finish. Credentials are not shown.")
        return 2


def _codex_login(details):
    from .siwc_auth import AuthClient, AuthError, AuthStore
    legacy = HOME / "codex"
    existing, bound = account_runtime.lifecycle_profile(legacy)
    bound_account = None
    if bound:
        active = _active_codex(AuthStore(existing))
        if active is None:
            raise AuthError("identity")
        if active["connected"] and not account_runtime.reconnecting(existing):
            raise AuthError("busy")
        # Also recover a completed local credential removal that was interrupted before its marker.
        account_runtime.disconnected(existing)
        bound_account = active["id"]
    directory = account_runtime.directory(legacy, login=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    client = AuthClient(AuthStore(directory))

    state = {"done": False, "success": False, "requests": 0}

    class Callback(http.server.BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_GET(self):
            state["requests"] += 1
            parsed = urlsplit(self.path)
            if len(self.path) > 8192 or parsed.path != "/auth/callback" or state["done"]:
                self.send_error(400)
                return
            try:
                client.finish_login(pending, parsed.query)
                state.update(done=True, success=True)
            except AuthError:
                if pending.used:
                    state["done"] = True
                # A stray/wrong-state callback cannot consume a valid login.
                self.send_error(400)
                return
            body = b"AI Desktop: account connected. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Server(http.server.HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(3)
            return connection, address

        def handle_error(self, *unused):
            pass

    with Server(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        pending = client.begin_login("http://127.0.0.1:%d/auth/callback" % server.server_port,
                                     account_id=bound_account)
        print("Connect your ChatGPT subscription to local AI Desktop. Open this address in a browser:")
        print(pending.authorization_url, flush=True)
        deadline = time.monotonic() + 300
        while not state["done"] and state["requests"] < 16 and time.monotonic() < deadline:
            server.handle_request()
        if not state["success"]:
            print("Codex: the login was interrupted or expired; it is not retried automatically.")
            return 2
    account_runtime.connected(directory)
    if details:
        print("Codex: subscription connected. Model availability is checked when you approve a task.")
    return 0


def _active_codex(store):
    status = store.status()
    return next((row for row in status["accounts"] if row["id"] == status["active"]), None)


def _account_action(action, provider, *, named=False):
    """Check or disconnect one explicit local profile while holding the service lock."""
    from .siwc_auth import AuthClient, AuthStore
    path, named_binding = account_runtime.lifecycle_profile(HOME / provider)
    if path is None:
        return 4 if action == "check" else 0
    if action == "check" and named and not named_binding:
        return 4
    if provider == "claude":
        from .analysis_sandbox import local_claude_session
        if named_binding and account_runtime.reconnecting(path):
            return 6  # Disconnection was already verified for this immutable alias.
        account_runtime.validate_claude_path(path)
        present = local_claude_session(path, logout=action == "logout")
        if action == "check":
            if named_binding and not present:
                return 6
            return 0 if present else 4
        if named_binding:
            account_runtime.disconnected(path)
            print("Claude: signed out locally. Use a fresh account name to reconnect.")
            return 6
        print("Claude: signed out locally.")
        return 0

    store = AuthStore(path)
    active = _active_codex(store)
    if action == "check":
        if named_binding and active is None:
            return 6
        if account_runtime.reconnecting(path):
            return 4
        return 0 if active and active["connected"] else 4
    confirmed = active is None or AuthClient(store).logout()
    if named_binding:
        account_runtime.disconnected(path)
    print("Codex: profile disconnected." if confirmed else
          "Codex: disconnected locally; the provider did not confirm the revocation.")
    if named_binding and active is None:
        print("Use a fresh account name to reconnect.")
        return 6
    return 0 if confirmed else 5


def main(action, *, allow=False, details=False, named=None):
    if (allow is not True or type(details) is not bool or action not in ("login", "logout", "check", "retire")
            or (action in ("check", "retire") and not named) or os.getuid() != 0 or os.geteuid() != 0
            or not os.isatty(0) or not os.isatty(1)):
        print("Codex: this needs explicit consent, an administrator and a terminal.")
        return 2
    from .analysis_admin import login_command, login_unit_stopped
    from .codex_runtime import runtime_ready
    unit, code = None, 2
    try:
        if named is not None:
            provider, name = named
            uid = int(os.environ.get("SUDO_UID", ""))
            account_runtime.identity(uid, provider, name, allow_default=action in ("check", "logout"))
        if named is None or action == "login":
            if named and provider == "claude":
                from .analysis_sandbox import runtime_ready as claude_ready
                claude_ready()
            else:
                runtime_ready()
        entry = trusted_file(INSTALL / "codex-auth.py")
        command = login_command(details=details)
        for index, value in enumerate(command):
            if value.startswith("--unit="):
                command[index] = "--unit=ai-desktop-codex-auth-" + secrets.token_hex(8)
                unit = command[index][7:] + ".service"
        command[-2:] = [str(entry), "worker-" + action + ("-verbose" if details else "")]
        if named:
            command[-1:] = ["account-worker-" + action, str(uid), provider, name]
        code = subprocess.run(command, env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"},
                              close_fds=True, timeout=610, check=False).returncode
    except (AnalysisDenied, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
        print("Codex: the login was stopped; check the installation.")
        code = 2
    finally:
        if unit is not None:
            try:
                subprocess.run(["/usr/bin/systemctl", "stop", unit], check=False, timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env={"PATH": "/usr/bin", "LANG": "C.UTF-8"})
                state = subprocess.run(["/usr/bin/systemctl", "show", unit,
                                        "--property=LoadState,ActiveState,MainPID"], check=False, timeout=5,
                                       capture_output=True, text=True,
                                       env={"PATH": "/usr/bin", "LANG": "C.UTF-8"})
                if not login_unit_stopped(state):
                    code = 2
                    print("Could not confirm that the Codex login process stopped.")
            except (OSError, subprocess.SubprocessError):
                code = 2
                print("Could not confirm that the Codex login process stopped.")
    expected = {"check": (4, 6), "logout": (5, 6)}.get(action, ())
    return next((result for result in expected if code == _WORKER_RESULTS[result]),
                0 if code == 0 else 2)


def named_worker(action, uid, provider, name):
    from .e2_socket_activation import _worker_identity
    from .analysis_sandbox import profile_lock, run_client, service_identity
    from .codex_runtime import supervisor_ready as codex_ready
    try:
        if not _worker_identity() or not os.isatty(0) or not os.isatty(1):
            return 2
        if action not in ("login", "retire", "check", "logout"):
            return 2
        with account_runtime.selected(int(uid), provider, name, allow_default=action in ("check", "logout")):
            if action in ("check", "logout"):
                service_identity()
                with profile_lock():
                    return _account_action(action, provider, named=name != "default")
            if action == "retire":
                service_identity()
                with profile_lock():
                    account_runtime.retire(HOME / provider)
                return 0
            if provider == "claude":
                run_client("login")
                return 0
            codex_ready()
            return auth_worker("login")
    except (OSError, ValueError, AnalysisDenied):
        print("The account operation was refused. Check the selected account and any active task.")
        return 2
