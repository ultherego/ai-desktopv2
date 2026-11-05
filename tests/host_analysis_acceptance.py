"""Explicit CachyOS acceptance of the installed native subscription path.

This is NOT a normal CI/LOCAL test. It performs a real model request only after
the public CLI's report-specific terminal consent. Optional login uses the
official client TTY directly: no capture, transcript or credential inspection.
"""
import hashlib
import os
from pathlib import Path
import pty
import select
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_desktop import analysis_service as service
from ai_desktop import analysis_session_admin as session_admin
from ai_desktop.analysis_sandbox import BINARY, INSTALL, runtime_ready, trusted_file
from ai_desktop.terminal_consent import PROMPTS
from ai_desktop.skills import inspect_skill
import host_e2_activated_worker as offline

PACKAGED_SKILLS = ("arch-amdgpu", "arch-audio", "arch-bluetooth", "arch-btrfs", "arch-docker", "arch-network", "arch-pacman", "arch-systemd", "arch-zram",
                   "cachyos-boot-snapshots", "cachyos-diagnostics", "cachyos-gaming", "cachyos-hardware", "cachyos-kernel",
                   "cachyos-packages", "cachyos-scheduler", "code-development", "code-review", "concise-assistant",
                   "desktop-documentation", "project-analysis")


def _installed_skills_match():
    """Check the fixed packaged catalog, including bounded nofollow reads.

    The manifest deliberately refuses a missing source directory rather than
    silently reducing the comparisons to whatever an empty glob returned.
    """
    source_root, target_root = ROOT / "skills", INSTALL / "skills"
    for root in (source_root, target_root):
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022):
            raise ValueError("skill-directory")
        names = set()
        for entry in root.iterdir():
            names.add(entry.name)
            if len(names) > len(PACKAGED_SKILLS):
                raise ValueError("skill-manifest")
        if names != set(PACKAGED_SKILLS):
            raise ValueError("skill-manifest")
    for name in PACKAGED_SKILLS:
        trusted_file(target_root / name / "SKILL.md")
        source = inspect_skill(source_root, name, secure=True)[1]
        target = inspect_skill(target_root, name, secure=True)[1]
        if source != target:
            raise ValueError("installed-skill")


def installed_matches(*, native=True):
    """Installed package equals this tree. native=False skips untracked native
    clients (only the install runner copies them into its worktree); the code, units and
    helpers and every packaged Skill are still compared byte for byte."""
    offline._installed_matches()
    _installed_skills_match()
    mappings = [(p, INSTALL / "ai_desktop" / p.name) for p in (ROOT / "ai_desktop").glob("*.py")]
    mappings += [(ROOT / "packaging/systemd" / name, INSTALL / name)
                 for name in ("analysis-client.py", "analysis-worker.py", "analysis-session.py",
                              "codex-auth.py", "codex-client.py")]
    mappings += [(ROOT / "packaging/systemd" / name, Path("/usr/lib/systemd/system") / name)
                 for name in ("ai-desktop-analysis@.socket", "ai-desktop-analysis@.service")]
    # Only the executable, never desktop/service credential files.
    if native:
        mappings.append((ROOT / "packaging/arch/claude-code", BINARY))
        codex = ROOT / "packaging/arch/codex"
        # Optional staging is explicit: absence means no Codex binary was
        # packaged. lexists catches a dangling symlink and fails closed below.
        if os.path.lexists(codex):
            mappings.append((codex, INSTALL / "codex"))
    for source, target in mappings:
        trusted_file(target)
        if source.is_symlink() or not source.is_file():
            raise ValueError("source")
        with source.open("rb") as a, target.open("rb") as b:
            if hashlib.file_digest(a, "sha256").digest() != hashlib.file_digest(b, "sha256").digest():
                raise ValueError("installed-code")
    runtime_ready()


def run(*args, timeout=30):
    environment = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"}
    # Only the desktop collector receives session metadata. The privileged
    # helper/native worker still construct their own independent environments.
    for key in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "SHELL"):
        if key in os.environ:
            environment[key] = os.environ[key]
    subprocess.run(args, check=True, timeout=timeout, close_fds=True,
                   env=environment)


def check_stopped(uid, units):
    if (os.path.lexists(service.socket_path(uid))
            or offline._property(units[1], "MainPID") != "0"
            or any(offline._property(unit, "ActiveState") not in ("inactive", "failed") for unit in units)):
        raise ValueError("cleanup")
    values = session_admin.properties(session_admin.manager_unit(uid))
    if values.get("ActiveState") not in ("inactive", "failed") or values.get("MainPID") != "0":
        raise ValueError("manager-cleanup")


def reject_incomplete():
    """Always refuses; never approves or captures a real login."""
    master, slave = pty.openpty()
    process = subprocess.Popen(["/usr/bin/ai-desktop", "analyze", "--details"], stdin=slave, stdout=slave, stderr=slave,
                               close_fds=True, start_new_session=True,
                               env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
    os.close(slave)
    output, answered = bytearray(), False
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and len(output) <= 32768:
            if select.select([master], [], [], .1)[0]:
                try:
                    raw = os.read(master, 8192)
                except OSError:
                    break
                if not raw:
                    break
                output.extend(raw)
            if PROMPTS["transfer"].encode() in output and not answered:
                os.write(master, b"no\n")
                answered = True
            if process.poll() is not None and not select.select([master], [], [], .1)[0]:
                break
        process.wait(timeout=2)
        if (process.returncode != 2 or not answered or b"ANALYSIS: REFUSED reason=consent" not in output
                or b"Administrator permissions" in output or b"ANALYSIS: PASS" in output):
            raise ValueError("explicit-refusal")
        print("HOST CONSENT: PASS no=REFUSED sudo=unused")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def preflight():
    uid = os.geteuid()
    if (uid <= 0 or uid != os.getuid() or not os.isatty(0) or not os.isatty(1)
            or Path("/proc/1/comm").read_text().strip() != "systemd"):
        raise ValueError("host")
    if offline._command(["/usr/bin/pacman", "-Q", "ai-desktop"]) != "ai-desktop 0.1.0-70":
        raise ValueError("package")
    installed_matches()
    units = ["ai-desktop-analysis@" + str(uid) + suffix for suffix in (".socket", ".service")]
    for unit in units:
        suffix = unit.rsplit(".", 1)[1]
        if (offline._property(unit, "FragmentPath") != "/usr/lib/systemd/system/ai-desktop-analysis@." + suffix
                or offline._property(unit, "DropInPaths") != ""
                or offline._property(unit, "NeedDaemonReload") != "no"
                or offline._property(unit, "ActiveState") not in ("inactive", "failed")
                or offline._property(unit, "UnitFileState") != "static"):
            raise ValueError("unit-policy")
    if os.path.lexists(service.socket_path(uid)):
        raise ValueError("existing-socket")
    check_stopped(uid, units)
    return uid, units


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args not in (["--allow-subscription-analysis-test"],
                    ["--allow-subscription-analysis-test", "--allow-claude-login"]):
        print("HOST ANALYSIS: REFUSED (explicit real subscription test flag required)")
        return 2
    if os.geteuid() <= 0 or not os.isatty(0) or not os.isatty(1):
        print("HOST ANALYSIS: REFUSED (desktop UID and uncaptured TTY required)")
        return 2
    stage, started, passed = "preflight", False, False
    try:
        uid, units = preflight()
        stage = "status"
        run("/usr/bin/ai-desktop", "status")
        stage = "consent-refusal"
        reject_incomplete()
        check_stopped(uid, units)
        if "--allow-claude-login" in args:
            stage = "login"
            run("/usr/bin/ai-desktop", "login", "--allow-claude-login", timeout=800)
        started = True
        stage = "analysis"
        run("/usr/bin/ai-desktop", "analyze", "--details", timeout=420)
        passed = True
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError, KeyboardInterrupt):
        print("HOST ANALYSIS: FAIL stage=" + stage)
    finally:
        if started:
            try:
                # The application must do its own cleanup; the harness cannot
                # manufacture PASS by stopping the units on its behalf.
                check_stopped(uid, units)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                passed = False
                print("HOST ANALYSIS: FAIL stage=cleanup")
    print("HOST ANALYSIS: " + ("PASS native=2.1.312 subscription=claude.ai daily=PASS consent-refusal=PASS result=PASS cleanup=PASS" if passed else "FAIL"))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
