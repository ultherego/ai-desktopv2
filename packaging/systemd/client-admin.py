"""Fixed privileged client registration entry point; source paths are data only."""
import sys
sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop.client_registration import admin_main

raise SystemExit(admin_main(sys.argv[1:]))
