"""A+ namespace experiment. No real client/profile or provider entrypoint.

The sole callable launcher runs the repository's synthetic acceptance fixture.
It deliberately cannot accept a profile path, command, endpoint or credentials.
"""
import errno
import json
import os
from pathlib import Path
import shutil

from .e2_compat_fixture import FixtureDenied, bounded_run, _unique

ROOT = Path(__file__).resolve().parent.parent
# Both disposable namespace layers need this exact import closure. The shared
# process helper also validates native error metadata in the separate COMPAT
# experiment; its classifier dependencies must remain read-only here too.
MODULE_FILES = ("__init__.py", "e2_connect_proxy.py", "e2_subscription_boundary.py",
                "e2_compat_fixture.py", "provider_errors.py", "e2_ipc.py",
                "report_policy.py", "transfer_consent.py", "runtime_health.py")
FILES = tuple("ai_desktop/" + name for name in MODULE_FILES) + ("tests/e2_namespace_fixture.py",)
CHECKS = ("client_privileges", "approved_tls", "profile_visible", "profile_refresh_replace", "host_home",
          "host_secret", "host_socket", "abstract_socket", "host_process",
          "host_loopback", "direct_ipv4", "direct_ipv6", "dns_udp", "dns_tcp",
          "proxy_env_bypass", "unknown_host", "private_ip", "metadata_ip",
          "wrong_port", "plaintext", "wrong_sni", "bad_certificate",
          "redirect_escape", "profile_symlink_escape", "readonly_input",
          "host_tool_effect", "nested_namespace", "proxy_failure")
# Only fixed synthetic phase/check identifiers may cross the failure boundary.
PHASES = ("metadata", "approved-tls", "profile", "host-denials",
          "transport-denials", "redirect", "filesystem", "nested-namespace")


def privileges_locked(status):
    """Verify kernel status, not just bwrap flags: no privilege gain or caps.

    Only /proc/self/status from the tested process may be supplied in the
    sandbox. Synthetic unit tests supply controlled strings. No raw status or
    host-specific values are included in the report.
    """
    if type(status) is not str or len(status) > 32768:
        return False
    required = ("NoNewPrivs", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    values = {}
    for line in status.splitlines():
        key, separator, value = line.partition(":")
        if key in required:
            if not separator or key in values:
                return False
            values[key] = value.strip()
    return (set(values) == set(required) and values["NoNewPrivs"] == "1"
            and all(values[name] == "0000000000000000"
                    for name in required if name != "NoNewPrivs"))


def userns_refused(return_value, err, *, thread_count):
    """Only accept a denied CLONE_NEWUSER probe in the single-threaded child.

    This complements the mandatory bwrap --disable-userns and
    --assert-userns-disabled options. The nested namespace need not expose
    a user.max_user_namespaces value of exactly 1: the clamp belongs to
    its ancestor. EINVAL alone is NOT proof of isolation.
    """
    return (return_value == -1 and type(thread_count) is int
            and thread_count == 1 and err in (
                errno.EPERM, errno.ENOSPC, errno.EACCES, errno.EINVAL,
                getattr(errno, "EUSERS", -1)))



def userns_probe_passed(code, output, errors):
    """Accept only an explicit, clean refusal from the isolated probe process."""
    return (type(code) is int and code == 0 and type(output) is bytes
            and output == b"USERNS_DENIED\n"
            and type(errors) is bytes and errors == b"")



def base_command():
    command = ["/usr/bin/bwrap", "--unshare-user", "--unshare-pid", "--unshare-net",
               "--unshare-ipc", "--unshare-uts", "--die-with-parent", "--new-session",
               "--cap-drop", "ALL", "--clearenv", "--setenv", "PATH", "/usr/bin",
               "--setenv", "HOME", "/home/client", "--setenv", "LANG", "C.UTF-8",
               "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
               "--symlink", "usr/bin", "/sbin", "--symlink", "usr/lib", "/lib",
               "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
               "--tmpfs", "/home", "--dir", "/home/client", "--dir", "/work",
               "--dir", "/etc", "--dir", "/opt", "--dir", "/opt/ai_desktop"]
    # Arch uses /usr/lib; Debian/Ubuntu use /usr/lib64 for the ELF loader.
    command += ["--symlink", "usr/lib64" if Path("/usr/lib64").is_dir() else "usr/lib", "/lib64"]
    return command


def fixture_command(root=ROOT):
    root = Path(root)
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("E2 A+: REFUSED")
    command = base_command()
    for name in FILES:
        source = root / name
        if source.resolve() != source or not source.is_file():
            raise ValueError("E2 A+: REFUSED")
        dest = "/opt/fixture.py" if name.startswith("tests/") else "/opt/" + name
        command += ["--ro-bind", str(source), dest]
    return command + ["--chdir", "/work", "/usr/bin/python3", "-I", "-B", "/opt/fixture.py", "outer"]


def run_synthetic():
    if os.geteuid() == 0 or shutil.which("bwrap", path="/usr/bin") is None:
        raise RuntimeError("E2 A+: REFUSED")
    reason = "fixture"
    try:
        code, out, error = bounded_run(fixture_command(), env={"PATH": "/usr/bin"}, timeout=55, limit=8192)
        if code != 0 and b"bwrap:" in error:
            reason = "namespace-setup"
            if b"Permission denied" in error or b"Operation not permitted" in error:
                reason = "namespace-permission"
        data = json.loads(out, object_pairs_hook=_unique)
        if type(data) is dict:
            if set(data) == {"failure"} and data["failure"] in (
                    "outer-network", "outer-privileges", "certificate", "inner-namespace", "inner-checks", "fixture-exception"):
                reason = data["failure"]
            elif (set(data) == {"failure", "result"} and data["failure"] == "userns-probe"
                  and type(data["result"]) is str
                  and data["result"] in {"allowed", "invalid", "other", "multithreaded", "limit"}):
                reason = "userns-probe:" + data["result"]
            elif (set(data) == {"failure", "phase"} and data["failure"] == "fixture-exception"
                  and type(data["phase"]) is str and data["phase"] in PHASES):
                reason = "fixture-exception:" + data["phase"]
            elif (set(data) == {"failure", "failed_checks"} and data["failure"] == "inner-checks"
                  and type(data["failed_checks"]) is list
                  and data["failed_checks"]
                  and len(data["failed_checks"]) <= len(CHECKS)
                  and all(type(k) is str and k in CHECKS and k != "proxy_failure"
                          for k in data["failed_checks"])
                  and data["failed_checks"] == sorted(set(data["failed_checks"]))):
                reason = "inner-checks:" + ",".join(data["failed_checks"])
        expected = {"schema": 1, "checks": dict.fromkeys(CHECKS, True),
                    "real_client": False, "real_auth": False, "upstream": False}
        if (code != 0 or data != expected or type(data["schema"]) is not int or
                any(v is not True for v in data["checks"].values()) or
                any(data[k] is not False for k in ("real_client", "real_auth", "upstream"))):
            raise ValueError()
        return data
    except (OSError, ValueError, FixtureDenied):
        raise RuntimeError("E2 A+: REFUSED reason=" + reason) from None
