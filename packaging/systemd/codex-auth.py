"""Installed, fixed root/service entrypoint. Never accepts a path or token."""
import sys
sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop.codex_admin import auth_worker, main

if len(sys.argv) == 2 and sys.argv[1] in ("login", "logout"):
    raise SystemExit(main(sys.argv[1], allow=True))
if len(sys.argv) == 2 and sys.argv[1] in ("login-verbose", "logout-verbose"):
    raise SystemExit(main(sys.argv[1][:-8], allow=True, details=True))
if len(sys.argv) == 2 and sys.argv[1] in ("worker-login", "worker-logout"):
    raise SystemExit(auth_worker(sys.argv[1][7:]))
if len(sys.argv) == 2 and sys.argv[1] in ("worker-login-verbose", "worker-logout-verbose"):
    raise SystemExit(auth_worker(sys.argv[1][7:-8], details=True))
raise SystemExit(2)
