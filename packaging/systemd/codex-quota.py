"""Fixed administrative and unprivileged native Codex quota entry points."""
import sys
sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop import codex_quota_admin as admin
from ai_desktop import codex_quota_ipc as ipc

args = sys.argv[1:]
try:
    if len(args) == 2 and args[0] == "serve":
        ipc.serve(ipc.validated_uid(args[1]))
        raise SystemExit(0)
    if len(args) == 2 and admin.valid_action(*args):
        raise SystemExit(admin.administer(*args))
    if len(args) == 4 and args[0] == "worker" and admin.valid_action(args[1], args[3]):
        raise SystemExit(admin.worker(args[1], ipc.validated_uid(args[2]), args[3]))
except (ipc.QuotaError, OSError, ValueError, KeyError):
    pass
raise SystemExit(2)
