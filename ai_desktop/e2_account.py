"""Read-only E2 preflight for the dedicated Claude Linux principal.

No account creation, no OAuth access, no privilege switch and no network.
A positive result only proves *account and home* metadata; the future
trusted launcher must revalidate identities on open descriptors.
"""
import grp
import os
import pwd
import stat

ACCOUNT = "ai-desktop"
HOME = "/var/lib/ai-desktop"
# The private OAuth subdirectory is NOT inspected from the desktop user:
# mode 0700 on HOME deliberately prevents traversing to it.
_NOLOGIN = frozenset({"/usr/bin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false"})


def _private_dir(path, *, uid, gid):
    info = os.lstat(path)  # Never follow a symlink to another user's directory.
    return (stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
            and info.st_uid == uid and info.st_gid == gid)


def account_ready():
    """Fail-closed metadata check. Never inspect .credentials.json or tokens."""
    try:
        # A local caller must not share the future Claude UID or its group.
        if os.geteuid() == 0:
            return False
        user = pwd.getpwnam(ACCOUNT)
        if (user.pw_name != ACCOUNT or type(user.pw_uid) is not int or
                type(user.pw_gid) is not int or user.pw_uid <= 0 or
                user.pw_uid == os.geteuid() or user.pw_gid <= 0 or
                user.pw_gid == os.getegid() or user.pw_gid in os.getgroups() or
                user.pw_dir != HOME or
                user.pw_shell not in _NOLOGIN):
            return False
        group = grp.getgrgid(user.pw_gid)
        if group.gr_name != ACCOUNT or group.gr_mem:
            return False
        # Only metadata of the top-level home is accessible across UIDs.
        # An actual Claude-side worker must separately inspect its own
        # profile with descriptor-relative opens before any login/request.
        return _private_dir(HOME, uid=user.pw_uid, gid=user.pw_gid)
    except (KeyError, OSError, ValueError, TypeError, AttributeError):
        return False
