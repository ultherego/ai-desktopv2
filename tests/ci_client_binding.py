"""Disposable-CI kernel proof for sealed native payloads; no real AI client.

The only root writes are to a freshly created, fixed /opt fixture directory.
Never run this installer on a developer host. The ELF is compiled here from
the synthetic source below and never has credentials or an egress bridge.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_desktop import client_binding as binding, system_clients as clients
from ai_desktop.analysis_contract import AnalysisDenied
from ai_desktop.e2_subscription_boundary import base_command
from ai_desktop.process_io import bounded_process


FIXTURE = Path("/opt/ai-desktop-client-binding-fixture")
NATIVE = FIXTURE / "bin/codex"
RESOURCE = FIXTURE / "resources/payload"
SOURCE = r'''
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#ifndef MARKER
#define MARKER "ORIGINAL"
#endif
int main(int argc, char **argv) {
  char executable[4096], payload[32] = {0};
  ssize_t size = readlink("/proc/self/exe", executable, sizeof(executable)-1);
  if (argc != 3 || size < 0) return 2;
  executable[size] = 0;
  if (strcmp(executable, argv[2])) return 3;
  for (int fd = 3; fd < 8192; ++fd)
    if (fcntl(fd, F_GETFD) != -1 || errno != EBADF) return 4;
  int fd = open(argv[1], O_RDONLY);
  if (fd < 0 || read(fd, payload, sizeof(payload)-1) != 8) return 5;
  close(fd);
  if (strcmp(payload, "ORIGINAL")) return 6;
  fd = open(argv[1], O_WRONLY);
  if (fd >= 0) { close(fd); return 7; }
  if (errno != EROFS && errno != EACCES) return 8;
  puts(MARKER);
  return 0;
}
'''


def require_ci():
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux"
            or os.getuid() <= 0 or os.getuid() != os.geteuid() or os.getgid() != os.getegid()):
        raise ValueError("CI required")
    release = Path("/etc/os-release").read_text().splitlines()
    if "ID=ubuntu" not in release or 'VERSION_ID="24.04"' not in release:
        raise ValueError("disposable Ubuntu runner required")
    if FIXTURE.exists() or FIXTURE.is_symlink():
        raise ValueError("fixture already exists")


def checked(argv):
    subprocess.run(argv, env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   check=True, timeout=30)


def run():
    require_ci()
    created = False
    with tempfile.TemporaryDirectory(prefix="aid-client-binding-") as temporary:
        directory = Path(temporary)
        source = directory / "native.c"
        source.write_text(SOURCE)
        first, second = directory / "first", directory / "second"
        checked(["/usr/bin/cc", "-O2", str(source), "-o", str(first)])
        checked(["/usr/bin/cc", "-O2", '-DMARKER="REPLACED"', str(source), "-o", str(second)])
        original, changed = directory / "original", directory / "changed"
        original.write_bytes(b"ORIGINAL")
        changed.write_bytes(b"REPLACED")
        try:
            # mkdir (not mkdir -p) atomically claims this unique fixture root.
            checked(["/usr/bin/sudo", "-n", "/usr/bin/mkdir", "-m", "755", str(FIXTURE)])
            created = True
            checked(["/usr/bin/sudo", "-n", "/usr/bin/install", "-d", "-m", "755",
                     str(NATIVE.parent), str(RESOURCE.parent)])
            checked(["/usr/bin/sudo", "-n", "/usr/bin/install", "-m", "755", str(first), str(NATIVE)])
            checked(["/usr/bin/sudo", "-n", "/usr/bin/install", "-m", "644", str(original), str(RESOURCE)])
            with patch.object(clients, "_CANDIDATES", {"codex": str(NATIVE), "claude": "/usr/bin/claude"}), \
                    patch.object(clients, "_PACKAGE_ROOTS", {"codex": (str(FIXTURE),), "claude": ()}):
                with binding.bind("codex") as bound:
                    command = base_command() + bound.mount_args() + [
                        "--chdir", "/work", bound.binary, str(RESOURCE), str(NATIVE)]
                    result = bounded_process(command, env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
                        pass_fds=bound.pass_fds, timeout=10, limit=4096)
                    if result != (0, b"ORIGINAL\n", b""):
                        raise ValueError("bound execution, resource, readonly or fd boundary")
                    # Freeze mount arguments, then replace both host files. The
                    # already-bound child must still execute/read original bytes.
                    command = base_command() + bound.mount_args() + [
                        "--chdir", "/work", bound.binary, str(RESOURCE), str(NATIVE)]
                    checked(["/usr/bin/sudo", "-n", "/usr/bin/install", "-m", "755", str(second), str(NATIVE)])
                    checked(["/usr/bin/sudo", "-n", "/usr/bin/install", "-m", "644", str(changed), str(RESOURCE)])
                    result = bounded_process(command, env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
                        pass_fds=bound.pass_fds, timeout=10, limit=4096)
                    if result != (0, b"ORIGINAL\n", b""):
                        raise ValueError("source replacement changed bound bytes")
                    try:
                        bound.recheck()
                    except AnalysisDenied as exc:
                        if exc.reason != "client-changed":
                            raise ValueError("wrong changed-client result") from None
                    else:
                        raise ValueError("source change not detected")
                    retained = bound.pass_fds
                for descriptor in retained:
                    try:
                        os.fstat(descriptor)
                    except OSError:
                        pass
                    else:
                        raise ValueError("snapshot descriptor leaked")
        finally:
            if created:
                checked(["/usr/bin/sudo", "-n", "/usr/bin/rm", "-rf", "--", str(FIXTURE)])


def main(argv=None):
    if (sys.argv[1:] if argv is None else list(argv)) != ["--allow-disposable-client-binding-test"]:
        print("CI CLIENT BINDING: REFUSED (explicit disposable-CI flag required)")
        return 2
    try:
        run()
    except (AnalysisDenied, OSError, ValueError, subprocess.SubprocessError):
        print("CI CLIENT BINDING: FAIL (synthetic payload boundary)")
        return 2
    print("CI CLIENT BINDING: PASS sealed=PASS layout=PASS readonly=PASS fd-close=PASS update=PASS auth=NONE network=OFF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
