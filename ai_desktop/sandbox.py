import os
import shutil
import subprocess
import sys
from pathlib import Path

PROBE = chr(10).join([
    "import os, sys",
    "from pathlib import Path",
    "home = Path(os.environ[\"HOST_HOME\"])",
    "checks = {",
    "  \"home hosta ukryty\": not home.exists(),",
    "  \"brak hostowego etc\": not Path(\"/etc/passwd\").exists(),",
    "  \"osobna siec\": os.readlink(\"/proc/self/ns/net\") != os.environ[\"HOST_NETNS\"],",
    "  \"osobna przestrzen uzytkownika\": os.readlink(\"/proc/self/ns/user\") != os.environ[\"HOST_USERNS\"],",
    "  \"usr read-only\": bool(os.statvfs(\"/usr\").f_flag & os.ST_RDONLY),",
    "}",
    "p = Path(\"/home/sandbox/test\")",
    "p.write_text(\"probe\")",
    "checks[\"tmpfs dziala\"] = p.read_text() == \"probe\"",
    "for label, ok in checks.items():",
    "  status = \"PASS\" if ok else \"FAIL\"",
    "  print(f\"{label}: {status}\")",
    "sys.exit(0 if all(checks.values()) else 1)",
])


def build_command(home, netns, userns):
    return [
        "bwrap", "--unshare-user", "--unshare-pid", "--unshare-net",
        "--unshare-ipc", "--unshare-uts", "--die-with-parent",
        "--new-session", "--clearenv", "--setenv", "PATH", "/usr/bin",
        "--setenv", "HOME", "/home/sandbox",
        "--setenv", "HOST_HOME", str(home),
        "--setenv", "HOST_NETNS", netns,
        "--setenv", "HOST_USERNS", userns,
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib", "/lib64",
        "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", "/tmp", "--tmpfs", "/home",
        "--dir", "/home/sandbox", "--dir", "/work",
        "--chdir", "/work", "/usr/bin/python3", "-I", "-B", "-c", PROBE,
    ]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["self-test"]:
        print("Uzycie: python3 -B -m ai_desktop.sandbox self-test")
        return 2
    if not shutil.which("bwrap"):
        print("Nie znaleziono bwrap")
        return 2
    cmd = build_command(
        Path.home(),
        os.readlink("/proc/self/ns/net"),
        os.readlink("/proc/self/ns/user"),
    )
    try:
        result = subprocess.run(cmd, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Blad testu: {type(exc).__name__}")
        return 2
    print("SANDBOX: PASS" if result.returncode == 0 else "SANDBOX: FAIL")
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
