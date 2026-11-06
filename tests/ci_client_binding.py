"""Disposable-CI kernel proof for sealed native payloads; no real AI client.

The only root writes are to a freshly created, fixed /usr/lib fixture directory.
Never run this installer on a developer host. The ELF is compiled here from
the synthetic source below and never has credentials or an egress bridge.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
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


FIXTURE = Path("/usr/lib/ai-desktop-client-binding-fixture")
NATIVE = FIXTURE / "bin/codex"
RESOURCE = FIXTURE / "resources/payload"
SOURCE = r'''
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#ifndef MARKER
#define MARKER "ORIGINAL"
#endif
int main(int argc, char **argv) {
  char executable[4096], payload[32] = {0};
  ssize_t size = readlink("/proc/self/exe", executable, sizeof(executable)-1);
  if (argc != 3 || size < 0) return 2;
  executable[size] = 0;
  if (strcmp(executable, argv[2])) {
    size_t expected = strlen(argv[2]);
    if (!strncmp(executable, argv[2], expected) && !strcmp(executable + expected, " (deleted)"))
      fputs("FIXTURE: executable=expected-deleted\n", stderr);
    else if (strstr(executable, "memfd:"))
      fputs("FIXTURE: executable=memfd\n", stderr);
    else fputs("FIXTURE: executable=other\n", stderr);
    return 3;
  }
  for (int fd = 3; fd < 8192; ++fd)
    if (fcntl(fd, F_GETFD) != -1 || errno != EBADF) return 4;
  int fd = open(argv[1], O_RDONLY);
  if (fd < 0 || read(fd, payload, sizeof(payload)-1) != 8) return 5;
  close(fd);
  if (strcmp(payload, "ORIGINAL")) return 6;
  fd = open(argv[1], O_WRONLY);
  if (fd >= 0) { close(fd); return 7; }
  if (errno != EROFS && errno != EACCES) return 8;
  fd = open(argv[2], O_WRONLY);
  if (fd >= 0) { close(fd); return 9; }
  if (errno != EROFS && errno != EACCES) return 10;
  if (chmod(argv[1], 0600) == 0 || errno != EROFS) return 11;
  if (chmod(argv[2], 0700) == 0 || errno != EROFS) return 12;
  puts(MARKER);
  return 0;
}
'''


_STAGES = frozenset(("preflight", "compile", "install", "snapshot", "mount", "first-exec",
                     "update", "updated-exec", "source-recheck", "fd-close", "cleanup"))
_REASONS = frozenset(("assertion", "os-error", "command", "client-missing", "client-unsafe",
                      "client-layout", "client-unavailable", "client-changed", "input", "client",
                      "timeout", "output-limit", "owner", "permissions", "special-mode",
                      "fixture-parent", "other"))
_SITES = frozenset(("none", "root", "usr", "usr-lib"))


def stderr_kind(raw):
    """Finite categories only: never emit process-owned prose or a host path."""
    if raw == b"":
        return "empty"
    for kind in ("expected-deleted", "memfd", "other"):
        if raw == ("FIXTURE: executable=" + kind + "\n").encode("ascii"):
            return "exe-" + kind
    if type(raw) is bytes and raw.startswith(b"bwrap:"):
        for fragment, kind in ((b"Permission denied", "permission"),
                               (b"No such file or directory", "missing-path"),
                               (b"Read-only file system", "readonly"),
                               (b"Failed to bind mount", "bind")):
            if fragment in raw:
                return "bwrap-" + kind
        return "bwrap-setup"
    return "other"


class FixtureFailure(ValueError):
    def __init__(self, stage, *, reason="assertion", code=None, errors=b"", number=None, site="none"):
        self.stage = stage if stage in _STAGES else "preflight"
        self.reason = reason if reason in _REASONS else "other"
        self.code = str(code) if type(code) is int and -255 <= code <= 255 else "none"
        self.stderr = stderr_kind(errors)
        self.errno = errno.errorcode.get(number, "none") if type(number) is int else "none"
        self.site = site if site in _SITES else "none"
        super().__init__(self.stage)

    def diagnostic(self):
        return ("stage=" + self.stage + " reason=" + self.reason + " exit=" + self.code
                + " stderr=" + self.stderr + " errno=" + self.errno + " site=" + self.site)


@contextmanager
def phase(stage):
    try:
        yield
    except FixtureFailure:
        raise
    except AnalysisDenied as exc:
        raise FixtureFailure(stage, reason=exc.reason) from None
    except subprocess.CalledProcessError as exc:
        raise FixtureFailure(stage, reason="command", code=exc.returncode, errors=exc.stderr) from None
    except subprocess.TimeoutExpired:
        raise FixtureFailure(stage, reason="timeout") from None
    except OSError as exc:
        raise FixtureFailure(stage, reason="os-error", number=exc.errno) from None
    except ValueError:
        raise FixtureFailure(stage) from None


def checked_result(stage, result):
    code, output, errors = result
    if (code, output, errors) != (0, b"ORIGINAL\n", b""):
        raise FixtureFailure(stage, code=code, errors=errors)


def require_parent_trust():
    # Hosted /opt may be writable by the runner. Never weaken production
    # ancestor checks or repair an existing directory to accommodate a fixture.
    for site, path in (("root", "/"), ("usr", "/usr"), ("usr-lib", "/usr/lib")):
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise FixtureFailure("preflight", reason="fixture-parent", site=site)
        try:
            clients._trusted(info)
        except clients._Denied as exc:
            raise FixtureFailure("preflight", reason=exc.detail, site=site) from None


def require_ci():
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux"
            or os.getuid() <= 0 or os.getuid() != os.geteuid() or os.getgid() != os.getegid()):
        raise ValueError("CI required")
    release = Path("/etc/os-release").read_text().splitlines()
    if "ID=ubuntu" not in release or 'VERSION_ID="24.04"' not in release:
        raise ValueError("disposable Ubuntu runner required")
    require_parent_trust()
    if FIXTURE.exists() or FIXTURE.is_symlink():
        raise ValueError("fixture already exists")


def checked(argv, *, stage="install"):
    with phase(stage):
        subprocess.run(argv, env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       check=True, timeout=30)


def run():
    with phase("preflight"):
        require_ci()
    created = False
    with tempfile.TemporaryDirectory(prefix="aid-client-binding-") as temporary:
        directory = Path(temporary)
        source = directory / "native.c"
        source.write_text(SOURCE)
        first, second = directory / "first", directory / "second"
        checked(["/usr/bin/cc", "-O2", str(source), "-o", str(first)], stage="compile")
        checked(["/usr/bin/cc", "-O2", '-DMARKER="REPLACED"', str(source), "-o", str(second)], stage="compile")
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
                with phase("snapshot"), binding.bind("codex") as bound:
                    with phase("mount"):
                        command = base_command() + bound.mount_args() + [
                            "--chdir", "/work", bound.binary, str(RESOURCE), str(NATIVE)]
                    with phase("first-exec"):
                        result = bounded_process(command, env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
                            pass_fds=bound.pass_fds, timeout=10, limit=4096)
                        checked_result("first-exec", result)
                    # Freeze mount arguments, then replace both host files. The
                    # already-bound child must still execute/read original bytes.
                    with phase("mount"):
                        command = base_command() + bound.mount_args() + [
                            "--chdir", "/work", bound.binary, str(RESOURCE), str(NATIVE)]
                    checked(["/usr/bin/sudo", "-n", "/usr/bin/install", "-m", "755", str(second), str(NATIVE)], stage="update")
                    checked(["/usr/bin/sudo", "-n", "/usr/bin/install", "-m", "644", str(changed), str(RESOURCE)], stage="update")
                    with phase("updated-exec"):
                        result = bounded_process(command, env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
                            pass_fds=bound.pass_fds, timeout=10, limit=4096)
                        checked_result("updated-exec", result)
                    try:
                        bound.recheck()
                    except AnalysisDenied as exc:
                        if exc.reason != "client-changed":
                            raise FixtureFailure("source-recheck", reason=exc.reason) from None
                    else:
                        raise FixtureFailure("source-recheck")
                    retained = bound.pass_fds
                for descriptor in retained:
                    try:
                        os.fstat(descriptor)
                    except OSError:
                        pass
                    else:
                        raise FixtureFailure("fd-close")
        finally:
            if created:
                checked(["/usr/bin/sudo", "-n", "/usr/bin/rm", "-rf", "--", str(FIXTURE)], stage="cleanup")


def main(argv=None):
    if (sys.argv[1:] if argv is None else list(argv)) != ["--allow-disposable-client-binding-test"]:
        print("CI CLIENT BINDING: REFUSED (explicit disposable-CI flag required)")
        return 2
    try:
        run()
    except FixtureFailure as exc:
        print("CI CLIENT BINDING: FAIL " + exc.diagnostic())
        return 2
    except (AnalysisDenied, OSError, ValueError, subprocess.SubprocessError):
        print("CI CLIENT BINDING: FAIL (synthetic payload boundary)")
        return 2
    print("CI CLIENT BINDING: PASS sealed=PASS layout=PASS readonly=PASS fd-close=PASS update=PASS auth=NONE network=OFF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
