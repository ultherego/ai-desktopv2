"""Fixed service-UID OAuth worker; tokens never cross the desktop IPC."""
import http.server
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.parse import urlsplit

from .analysis_contract import AnalysisDenied
from .analysis_sandbox import HOME, INSTALL, trusted_file


def auth_worker(action, *, details=False):
    from .e2_socket_activation import _worker_identity
    from .siwc_auth import AuthClient, AuthError, AuthStore
    if (type(details) is not bool or not _worker_identity() or not os.isatty(0) or not os.isatty(1)
            or action not in ("login", "logout")):
        print("Codex: odmowa uruchomienia logowania.")
        return 2
    try:
        directory = HOME / "codex"
        if action == "login":
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
        client = AuthClient(AuthStore(directory))
        if action == "logout":
            confirmed = client.logout()
            print("Codex: odłączono profil." if confirmed else "Codex: odłączono lokalnie; nie potwierdzono odwołania u dostawcy.")
            return 0 if confirmed else 2
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
                body = b"AI Desktop: polaczono konto. Mozesz zamknac te karte."
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
            pending = client.begin_login("http://127.0.0.1:%d/auth/callback" % server.server_port)
            print("Połącz subskrypcję ChatGPT z lokalnym AI Desktop. Otwórz adres w przeglądarce:")
            print(pending.authorization_url, flush=True)
            deadline = time.monotonic() + 300
            while not state["done"] and state["requests"] < 16 and time.monotonic() < deadline:
                server.handle_request()
            if not state["success"]:
                print("Codex: logowanie przerwane lub wygasło; nie ponawiam automatycznie.")
                return 2
        if details:
            print("Codex: połączono subskrypcję. Dostępność modelu sprawdzimy przy zatwierdzonym zadaniu.")
        return 0
    except (AuthError, AnalysisDenied, OSError, ValueError, KeyboardInterrupt):
        print("Codex: nie ukończono obsługi konta. Poświadczenia nie są wyświetlane.")
        return 2


def main(action, *, allow=False, details=False):
    if (allow is not True or type(details) is not bool or action not in ("login", "logout") or os.getuid() != 0 or os.geteuid() != 0
            or not os.isatty(0) or not os.isatty(1)):
        print("Codex: wymagana jawna zgoda, administrator i terminal.")
        return 2
    from .analysis_admin import login_command
    from .codex_runtime import runtime_ready
    unit, code = None, 2
    try:
        runtime_ready()
        entry = trusted_file(INSTALL / "codex-auth.py")
        command = login_command(details=details)
        for index, value in enumerate(command):
            if value.startswith("--unit="):
                command[index] = "--unit=ai-desktop-codex-auth-" + secrets.token_hex(8)
                unit = command[index][7:] + ".service"
        command[-2:] = [str(entry), "worker-" + action + ("-verbose" if details else "")]
        code = subprocess.run(command, env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"},
                              close_fds=True, timeout=610, check=False).returncode
    except (AnalysisDenied, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
        print("Codex: logowanie zatrzymano; sprawdź instalację.")
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
                values = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
                if state.returncode != 0 or (values.get("LoadState") != "not-found" and
                        (values.get("ActiveState") not in ("inactive", "failed") or values.get("MainPID") != "0")):
                    code = 2
                    print("Nie potwierdzono zatrzymania procesu logowania Codex.")
            except (OSError, subprocess.SubprocessError):
                code = 2
                print("Nie potwierdzono zatrzymania procesu logowania Codex.")
    return code
