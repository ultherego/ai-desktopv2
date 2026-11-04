"""Root-owned fixed bootstrap; independent of cwd/PYTHONPATH/user settings."""
import sys

sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop.analysis_session_admin import main

raise SystemExit(main())
