"""Disposable loopback backend for the pinned Codex binary inside ci_codex_config's network namespace."""
import base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading


ACCOUNT = "fixture-workspace"
TOKEN = "synthetic-local-usage-access"


def credential():
    # The upstream test fixture uses an unsigned ID token and explicit file credentials.
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    claims = {"email": "quota@example.invalid", "https://api.openai.com/auth": {
        "chatgpt_account_id": ACCOUNT, "chatgpt_user_id": "fixture-user", "chatgpt_plan_type": "plus"}}
    token = part({"alg": "none", "typ": "JWT"}) + "." + part(claims) + ".c3ludGhldGlj"
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": datetime.now(timezone.utc).isoformat(),
            "tokens": {"id_token": token, "access_token": TOKEN, "refresh_token": "synthetic-local-refresh",
                       "login_owner_id": "fixture-login", "account_id": ACCOUNT}}


class QuotaHandler(BaseHTTPRequestHandler):
    def log_message(self, *unused):
        pass

    def do_GET(self):
        # Pinned backend-client distinguishes /backend-api/wham from the compatibility /api/codex route.
        if self.path == "/backend-api/wham/accounts/check":
            self.server.accounts += 1
            data = {"accounts": [{"id": ACCOUNT, "workspace_backend_origin": "https://127.0.0.1",
                                  "account_routing_override": "NO_CONSTRAINT"}]}
        elif self.path == "/backend-api/wham/usage":
            if (self.headers.get("Authorization") != "Bearer " + TOKEN
                    or self.headers.get("chatgpt-account-id") != ACCOUNT):
                self.server.denied = True
                self.send_error(403)
                return
            self.server.usage += 1
            data = {"plan_type": "plus", "account_id": ACCOUNT, "user_id": "fixture-user",
                    "rate_limit": {"allowed": True, "limit_reached": False,
                                   "primary_window": {"used_percent": 31, "limit_window_seconds": 18000,
                                                      "reset_after_seconds": 60, "reset_at": 2000000000},
                                   "secondary_window": {"used_percent": 47, "limit_window_seconds": 604800,
                                                        "reset_after_seconds": 120, "reset_at": 2000500000}}}
        else:
            # An optional background metadata GET may receive the same 404 as upstream's wiremock server.
            self.send_error(404)
            return
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_error(self, code, message=None, explain=None):
        # An attempted inference or any write is a test failure even if the client could recover.
        if self.command != "GET" or "/responses" in self.path or "/completions" in self.path:
            self.server.denied = True
        return super().send_error(code, message, explain)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    home = Path("/home/client/.codex")
    if (Path(__file__) != Path("/opt/quota-backend.py") or Path.cwd() != Path("/work")
            or os.environ.get("CODEX_HOME") != str(home) or not argv or argv[0] != "/opt/codex"
            or {name for _, name in socket.if_nameindex()} != {"lo"} or any(home.iterdir())):
        return 2
    os.umask(0o077)
    with (home / "auth.json").open("x") as stream:
        json.dump(credential(), stream)
    with ThreadingHTTPServer(("127.0.0.1", 0), QuotaHandler) as server:
        server.accounts, server.usage, server.denied = 0, 0, False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}/backend-api"
            # The child inherits the RPC pipes; the fixture never selects a method on its behalf.
            proc = subprocess.run([*argv, "-c", "chatgpt_base_url=" + json.dumps(base)], timeout=23, check=False)
            return 0 if proc.returncode == 0 and server.accounts and server.usage == 1 and not server.denied else 2
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    raise SystemExit(main())
