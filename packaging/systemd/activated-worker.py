"""Fixed installed worker bootstrap; systemd supplies exactly one UID."""
import sys

sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop.e2_socket_activation import worker_main

raise SystemExit(worker_main(sys.argv[1:]))
