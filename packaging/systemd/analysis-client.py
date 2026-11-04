"""Installed, root-owned bootstrap inside the private model namespace."""
import sys
sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop.analysis_child import main
raise SystemExit(main(sys.argv[1:]))
