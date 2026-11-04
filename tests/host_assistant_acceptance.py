"""Opt-in native E3/R2 acceptance. Interactive consent, synthetic workspace only."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import host_analysis_acceptance as prior
from ai_desktop import task_workspace


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args != ["--allow-subscription-assistant-test"] or os.geteuid() == 0 or not (os.isatty(0) and os.isatty(1)):
        print("HOST ASSISTANT: REFUSED (explicit flag, desktop UID and uncaptured TTY required)")
        return 2
    stage, started, passed = "preflight", False, False
    try:
        uid, units = prior.preflight()
        stage = "analysis-refusal"
        prior.reject_incomplete()
        stage = "question-and-rendering"
        started = True
        prior.run("/usr/bin/ai-desktop", "analyze", "--question",
                  "Ktore obserwacje wymagaja dalszego sprawdzenia? Podaj kroki i nie wykonuj zmian.", timeout=420)
        prior.check_stopped(uid, units)
        with tempfile.TemporaryDirectory(prefix="ai-task-acceptance-", dir="/tmp") as root:
            path = Path(root)
            original = "def add(a, b):\n    return a - b\n"
            tests = "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n"
            (path / "calc.py").write_text(original)
            (path / "test_calc.py").write_text(tests)
            stage = "native-task"
            prior.run("/usr/bin/ai-desktop", "task", "run",
                      "Napraw funkcje add: powinna dodawac dwie liczby. Zmien tylko calc.py. Testy sa kontekstem tylko do odczytu.",
                      "--workspace", root, "--file", "calc.py", "--context", "test_calc.py",
                      "--test", "python-unittest", timeout=420)
            prior.check_stopped(uid, units)
            records = task_workspace.status(root)
            if len(records) != 1 or records[0][1] != "applied" or (path / "calc.py").read_text() == original or (path / "test_calc.py").read_text() != tests:
                raise ValueError("task-result")
            stage = "rollback"
            prior.run("/usr/bin/ai-desktop", "task", "rollback", "--workspace", root, timeout=240)
            if (path / "calc.py").read_text() != original or task_workspace.status(root)[0][1] != "rolled-back":
                raise ValueError("rollback")
        prior.check_stopped(uid, units)
        passed = True
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError, KeyboardInterrupt):
        print("HOST ASSISTANT: FAIL stage=" + stage)
    finally:
        if started:
            try:
                prior.check_stopped(uid, units)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                passed = False
                print("HOST ASSISTANT: cleanup not confirmed; use ai-desktop stop")
    print("HOST ASSISTANT: " + ("PASS question=PASS native-task=PASS test=PASS apply=PASS rollback=PASS cleanup=PASS ui=default" if passed else "FAIL"))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
