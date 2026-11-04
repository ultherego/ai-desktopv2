"""Installed bootstrap for the separate, explicitly enabled analysis service."""
import sys
sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop.analysis_service import worker_main
raise SystemExit(worker_main(sys.argv[1:]))
