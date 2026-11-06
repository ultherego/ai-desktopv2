"""Disposable kernel-CI proof of real user-to-root native registration.

Only synthetic bytes in a fresh runner-HOME directory and a freshly claimed
fixed /opt fixture are used. The native ELF is compiled but never executed.
The root fixture command accepts no paths or commands: its source UID/path is
frozen in root-owned fixture metadata, and its cache is fixed below FIXTURE.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path("/opt/ai-desktop-client-registration-fixture")
HELPER = FIXTURE / "driver.py"
CACHE = FIXTURE / "cache"
ENV = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
FLAG = "--allow-disposable-client-registration-test"
HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
GUARD_REASONS = frozenset({"root-fixture-only", "disposable-platform", "caller", "fixture-ownership",
                           "fixture-metadata", "fixture-source"})
ROOT_REASONS = frozenset({"already-registered", "authorization", "busy", "changed-cache", "cleanup-incomplete",
                         "input", "not-registered", "published-cleanup-incomplete", "registration-changed",
                         "source-changed", "source-unsafe", "source-unsupported", "source-unavailable", "source-missing",
                         "storage", "unsafe-cache", "unsafe-source", "guard-access", "guard-missing", "guard-io",
                         "guard-import", "guard-input", "unrecognized"}) | frozenset("guard-" + value for value in GUARD_REASONS)
NATIVE_SOURCE = r'''
#include <stdio.h>
#ifndef MARKER
#define MARKER "ORIGINAL"
#endif
int main(void) { puts(MARKER); return 0; }
'''


def ubuntu():
    release = Path("/etc/os-release").read_text().splitlines()
    if "ID=ubuntu" not in release or 'VERSION_ID="24.04"' not in release:
        raise ValueError("disposable-platform")


def require_ci():
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux"
            or os.getuid() <= 0 or os.getuid() != os.geteuid() or os.getgid() != os.getegid()):
        raise ValueError("disposable-ci")
    ubuntu()
    if os.path.lexists(FIXTURE):
        raise ValueError("existing-fixture")


def invoke(arguments):
    return subprocess.run(arguments, env=ENV, stdin=subprocess.DEVNULL, capture_output=True,
                          check=False, timeout=60)


def checked(arguments, *, expected=0):
    result = invoke(arguments)
    if result.returncode != expected:
        raise ValueError("fixture-command")
    return result.stdout


def _trusted(path, *, directory=False):
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        wanted = stat.S_ISDIR if part != path or directory else stat.S_ISREG
        if (not wanted(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or info.st_mode & (stat.S_ISUID | stat.S_ISGID)):
            raise ValueError("fixture-ownership")


def root_context():
    """The only privileged entry point, usable only from the installed fixture."""
    if os.getuid() != 0 or os.geteuid() != 0 or Path(__file__).absolute() != HELPER:
        raise ValueError("root-fixture-only")
    ubuntu()
    identity = os.environ.get("SUDO_UID", "")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", identity):
        raise ValueError("caller")
    uid = int(identity)
    record = pwd.getpwuid(uid)
    if record.pw_uid != uid or record.pw_name == "ai-desktop":
        raise ValueError("caller")
    _trusted(HELPER)
    metadata = FIXTURE / "fixture.json"
    _trusted(metadata)
    if metadata.stat().st_size > 4096:
        raise ValueError("fixture-metadata")
    value = json.loads(metadata.read_text())
    if type(value) is not dict or set(value) != {"uid", "directory", "runner"} or value["runner"] != "github-ubuntu-24.04":
        raise ValueError("fixture-metadata")
    home = Path(record.pw_dir)
    directory = Path(value["directory"])
    if (type(value["uid"]) is not int or value["uid"] != uid or not directory.is_absolute()
            or directory.parent != home or not re.fullmatch(r"aid-client-registration-[A-Za-z0-9_-]+", directory.name)
            or directory.is_symlink() or directory.lstat().st_uid != uid
            or stat.S_IMODE(directory.lstat().st_mode) != 0o700):
        raise ValueError("fixture-source")
    app = FIXTURE / "app"
    _trusted(app / "ai_desktop", directory=True)
    for path in (app / "ai_desktop").glob("*.py"):
        _trusted(path)
    sys.path.insert(0, str(app))
    from ai_desktop import client_registration
    client_registration.CACHE = CACHE
    return client_registration, uid, directory / "bin/codex"


def root_action(arguments):
    if (len(arguments) not in (2, 3) or arguments[0] not in ("register", "refresh", "remove")
            or not HASH.fullmatch(arguments[1])
            or (arguments[0] == "refresh") != (len(arguments) == 3)
            or len(arguments) == 3 and not HASH.fullmatch(arguments[2])):
        return 2
    try:
        registration, uid, source = root_context()
        try:
            result = registration.apply(arguments[0], "codex", uid=uid,
                                        source=None if arguments[0] == "remove" else str(source),
                                        expected_digest=arguments[1],
                                        previous_digest=arguments[2] if len(arguments) == 3 else None)
        except registration.RegistrationError as error:
            print("ROOT FIXTURE: REFUSED reason=" + (error.reason if error.reason in ROOT_REASONS else "unrecognized"))
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, ImportError) as error:
        if isinstance(error, ValueError) and str(error) in GUARD_REASONS:
            reason = "guard-" + str(error)
        elif isinstance(error, PermissionError):
            reason = "guard-access"
        elif isinstance(error, FileNotFoundError):
            reason = "guard-missing"
        elif isinstance(error, OSError):
            reason = "guard-io"
        elif isinstance(error, ImportError):
            reason = "guard-import"
        else:
            reason = "guard-input"
        print("ROOT FIXTURE: REFUSED reason=" + reason)
        return 2


def privileged(action, digest, previous=None, *, reason=None):
    if (action not in ("register", "refresh", "remove") or not HASH.fullmatch(digest)
            or previous is not None and not HASH.fullmatch(previous)
            or reason is not None and reason not in ROOT_REASONS):
        raise ValueError("fixture-action")
    arguments = ["/usr/bin/sudo", "-n", "--", "/usr/bin/python3", "-I", "-B", str(HELPER),
                 "--root-fixture", action, digest]
    if previous is not None:
        arguments.append(previous)
    completed = invoke(arguments)
    output = completed.stdout
    observed = "unrecognized"
    if type(output) is bytes and len(output) <= 192:
        for candidate in ROOT_REASONS:
            if output == ("ROOT FIXTURE: REFUSED reason=" + candidate + "\n").encode():
                observed = candidate
                break
    code = completed.returncode if type(completed.returncode) is int and -128 <= completed.returncode <= 255 else "invalid"
    print("CI REGISTRATION ROOT: action=" + action + " expected=" + (reason or "success")
          + " exit=" + str(code) + " reason=" + observed, flush=True)
    if completed.returncode != (2 if reason is not None else 0):
        raise ValueError("fixture-command")
    if reason is not None:
        if output != ("ROOT FIXTURE: REFUSED reason=" + reason + "\n").encode():
            raise ValueError("wrong-refusal")
        return None
    result = json.loads(output)
    if result != {"provider": "codex", "status": "removed" if action == "remove" else "registered", "digest": digest}:
        raise ValueError("registration-result")
    return result


def contents(directory):
    """Review the synthetic source without changing it, including alias spelling."""
    return {str(path.relative_to(directory)): ("link", os.readlink(path)) if path.is_symlink()
            else ("file", hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_uid, stat.S_IMODE(path.stat().st_mode))
            for path in directory.rglob("*") if path.is_symlink() or path.is_file()}


def verify_registered(native_bytes, resource_bytes, digest, uid, source):
    registered = CACHE / "codex"
    expected = {"manifest.json": 0o644, "payload": 0o755, "payload/bin": 0o755,
                "payload/bin/codex": 0o555, "payload/codex-package.json": 0o444,
                "payload/codex-resources": 0o755, "payload/codex-resources/payload": 0o444}
    if {str(path.relative_to(registered)) for path in registered.rglob("*")} != set(expected):
        raise ValueError("registered-closure")
    for path, mode in [(CACHE, 0o755), (registered, 0o755), *[(registered / name, mode) for name, mode in expected.items()]]:
        info = path.lstat()
        if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != mode or path.is_symlink():
            raise ValueError("real-root-ownership")
    if (registered / "payload/bin/codex").read_bytes() != native_bytes or (registered / "payload/codex-resources/payload").read_bytes() != resource_bytes:
        raise ValueError("registered-bytes")
    manifest = json.loads((registered / "manifest.json").read_text())
    if manifest["digest"] != digest or manifest["source_uid"] != uid or manifest["source"] != str(source):
        raise ValueError("registered-manifest")
    for target in (registered / "payload/bin/codex", registered / "payload/codex-resources/payload"):
        try:
            with target.open("wb") as handle:
                handle.write(b"SHOULD-NOT-BE-WRITABLE")
        except PermissionError:
            pass
        else:
            raise ValueError("caller-could-change-root-copy")


def run():
    require_ci()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from ai_desktop import client_sources as sources
    uid = os.getuid()
    home = Path(pwd.getpwuid(uid).pw_dir)
    created = False
    with tempfile.TemporaryDirectory(prefix="aid-client-registration-", dir=home) as temporary:
        directory = Path(temporary)
        native = directory / "release/bin/codex"
        resource = directory / "release/codex-resources/payload"
        native.parent.mkdir(parents=True)
        resource.parent.mkdir()
        resource.write_bytes(b"ORIGINAL")
        (native.parent.parent / "codex-package.json").write_text('{"version":"fixture"}')
        source = directory / "bin/codex"
        source.parent.mkdir()
        source.symlink_to("../release/bin/codex")
        # This unrelated synthetic secret must never enter the registered copy.
        (directory / "synthetic-login.json").write_bytes(b"SYNTHETIC-PRESERVE-NOT-A-TOKEN")
        code = directory / "fixture.c"
        code.write_text(NATIVE_SOURCE)
        replacement = directory / "replacement"
        checked(["/usr/bin/cc", "-O2", str(code), "-o", str(native)])
        checked(["/usr/bin/cc", "-O2", '-DMARKER="UPDATED"', str(code), "-o", str(replacement)])
        metadata = directory / "fixture.json"
        metadata.write_text(json.dumps({"uid": uid, "directory": str(directory), "runner": "github-ubuntu-24.04"}))
        modules = sorted((ROOT / "ai_desktop").glob("*.py"))
        if not modules or any(path.is_symlink() or not path.is_file() for path in modules):
            raise ValueError("fixture-source-code")
        try:
            checked(["/usr/bin/sudo", "-n", "/usr/bin/mkdir", "-m", "755", str(FIXTURE)])
            created = True
            checked(["/usr/bin/sudo", "-n", "/usr/bin/install", "-d", "-m", "755", str(FIXTURE / "app/ai_desktop")])
            checked(["/usr/bin/sudo", "-n", "/usr/bin/install", "-m", "644", *map(str, modules), str(FIXTURE / "app/ai_desktop")])
            checked(["/usr/bin/sudo", "-n", "/usr/bin/install", "-m", "644", str(Path(__file__)), str(HELPER)])
            checked(["/usr/bin/sudo", "-n", "/usr/bin/install", "-m", "644", str(metadata), str(FIXTURE / "fixture.json")])
            first = sources.discover("codex", str(source))
            if first.status != "present" or first.layout != "codex-bundle":
                raise ValueError("source-discovery")
            before = contents(directory)
            original = native.read_bytes()
            privileged("register", "0" * 64, reason="source-changed")
            if (CACHE / "codex").exists():
                raise ValueError("rejected-source-published")
            privileged("register", first.digest)
            verify_registered(original, b"ORIGINAL", first.digest, uid, source)
            if contents(directory) != before:
                raise ValueError("original-source-modified")
            shutil.copyfile(replacement, native)
            resource.write_bytes(b"UPDATED")
            second = sources.discover("codex", str(source))
            if second.status != "present" or first.digest == second.digest:
                raise ValueError("updated-source-discovery")
            verify_registered(original, b"ORIGINAL", first.digest, uid, source)
            after_update = contents(directory)
            privileged("refresh", second.digest, "0" * 64, reason="registration-changed")
            privileged("refresh", first.digest, first.digest, reason="source-changed")
            verify_registered(original, b"ORIGINAL", first.digest, uid, source)
            privileged("refresh", second.digest, first.digest)
            verify_registered(native.read_bytes(), b"UPDATED", second.digest, uid, source)
            privileged("remove", first.digest, reason="registration-changed")
            verify_registered(native.read_bytes(), b"UPDATED", second.digest, uid, source)
            privileged("remove", second.digest)
            if set(CACHE.iterdir()) != {CACHE / ".lock"} or contents(directory) != after_update:
                raise ValueError("remove-preservation")
        finally:
            if created:
                checked(["/usr/bin/sudo", "-n", "/usr/bin/rm", "-rf", "--", str(FIXTURE)])


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--root-fixture":
        return root_action(arguments[1:])
    if arguments != [FLAG]:
        print("CI CLIENT REGISTRATION: REFUSED explicit disposable-CI flag required")
        return 2
    try:
        run()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        detail = (" check=" + str(error) if isinstance(error, ValueError)
                  and re.fullmatch(r"[a-z-]{1,64}", str(error)) else "")
        print("CI CLIENT REGISTRATION: FAIL synthetic registration boundary" + detail)
        return 2
    print("CI CLIENT REGISTRATION: PASS root-owner=PASS readonly=PASS source-change=REFUSED refresh-cas=PASS "
          "remove-cas=PASS original-preserved=PASS auth=NONE native=NOT_RUN network=OFF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
