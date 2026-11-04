import os
import platform
import shutil
import subprocess
from pathlib import Path

TOOLS = ("git", "python3", "claude", "codex", "opencode", "bwrap", "podman", "snapper")


def read_command(args):
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=8
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"Niedostępne: {type(exc).__name__}"
    if result.returncode != 0:
        return result.returncode, "Nie udało się odczytać danych"
    return 0, result.stdout.strip() or "(pusty wynik)"


def audit_lines():
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "brak")
    session = os.environ.get("XDG_SESSION_TYPE", "brak")
    shell = Path(os.environ.get("SHELL", "brak")).name
    output = [
        "=== AI Desktop: audyt tylko do odczytu ===",
        f"Kernel: {platform.release()}",
        f"Pulpit: {desktop}",
        f"Sesja: {session}",
        f"Shell: {shell}",
        "", "=== NARZĘDZIA ==="
    ]
    for tool in TOOLS:
        present = "TAK" if shutil.which(tool) else "NIE"
        output.append(f"{tool}: {present}")
    output.extend(["", "=== PUNKTY MONTOWANIA ==="])
    for path in ("/", str(Path.home())):
        code, value = read_command(
            ("findmnt", "-T", path, "-n", "-o", "TARGET,SOURCE,FSTYPE")
        )
        output.append(
            f"{path}: {value}" if code == 0 else f"{path}: brak danych"
        )
    output.extend(["", "=== SNAPPER ==="])
    if shutil.which("snapper"):
        code, value = read_command(("snapper", "list-configs"))
        output.append(
            value if code == 0 else "Nie udało się odczytać konfiguracji"
        )
    else:
        output.append("Snapper niedostępny")
    output.extend([
        "", "=== UWAGI ===",
        "Snapshot / nie obejmuje automatycznie osobnego podwoluminu /home.",
        "Snapshot na tym samym dysku nie jest niezależnym backupem.",
        "Obecność bwrap nie dowodzi poprawnej izolacji agenta.",
        "Audyt nie uruchamia AI, nie zapisuje konfiguracji i nie wymaga sudo."
    ])
    return output
