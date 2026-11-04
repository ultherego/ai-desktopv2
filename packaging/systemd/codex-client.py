"""Installed, root-owned Codex bootstrap in an empty private model namespace."""
import sys
sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop.codex_child import main
raise SystemExit(main(sys.argv[1:]))
