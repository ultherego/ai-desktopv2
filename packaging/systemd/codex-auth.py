"""Installed, fixed root/service entrypoint. Never accepts a path or token."""
import sys
sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop.codex_admin import auth_worker, main, worker_exit

if len(sys.argv) == 2 and sys.argv[1] in ("login", "logout"):
    raise SystemExit(main(sys.argv[1], allow=True))
if len(sys.argv) == 2 and sys.argv[1] in ("login-verbose", "logout-verbose"):
    raise SystemExit(main(sys.argv[1][:-8], allow=True, details=True))
if len(sys.argv) == 2 and sys.argv[1] in ("worker-login", "worker-logout"):
    raise SystemExit(worker_exit(auth_worker(sys.argv[1][7:])))
if len(sys.argv) == 2 and sys.argv[1] in ("worker-login-verbose", "worker-logout-verbose"):
    raise SystemExit(worker_exit(auth_worker(sys.argv[1][7:-8], details=True)))
if len(sys.argv) == 4 and sys.argv[1] in ("account-login", "account-retire", "account-check", "account-logout"):
    raise SystemExit(main(sys.argv[1][8:], allow=True, named=tuple(sys.argv[2:])))
if len(sys.argv) == 5 and sys.argv[1] in ("account-worker-login", "account-worker-retire", "account-worker-check", "account-worker-logout"):
    from ai_desktop.codex_admin import named_worker
    raise SystemExit(worker_exit(named_worker(sys.argv[1][15:], *sys.argv[2:])))
raise SystemExit(2)
