"""Apply mode-bit restrictions only after Bubblewrap has finished setup.

systemd's RestrictSUIDSGID blocks openat2, which modern Bubblewrap requires.
Keep that restriction on the native client, not on the trusted setup process.
Only libseccomp's public ABI is used; no hand-written syscall numbers or BPF.
"""
import ctypes
import errno
import os
from pathlib import Path
import stat

from .analysis_contract import AnalysisDenied


class _Comparison(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


def install_mode_filter():
    """Irreversible, inherited filter; failure prevents native exec or threads."""
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
                  if ":" in line)
    if status.get("NoNewPrivs", "").strip() != "1" or len(list(Path("/proc/self/task").iterdir())) != 1:
        raise AnalysisDenied("sandbox")
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    pointer = ctypes.c_void_p
    lib.seccomp_init.argtypes, lib.seccomp_init.restype = [ctypes.c_uint32], pointer
    lib.seccomp_release.argtypes, lib.seccomp_release.restype = [pointer], None
    lib.seccomp_load.argtypes, lib.seccomp_load.restype = [pointer], ctypes.c_int
    lib.seccomp_attr_set.argtypes = [pointer, ctypes.c_int, ctypes.c_uint32]
    lib.seccomp_attr_set.restype = ctypes.c_int
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [pointer, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(_Comparison)]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    context = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW, native ABI only.
    if not context:
        raise AnalysisDenied("sandbox")
    try:
        # Kill incompatible ABIs; atomically synchronize any concurrent thread.
        if (lib.seccomp_attr_set(context, 2, 0x80000000) != 0
                or lib.seccomp_attr_set(context, 4, 1) != 0):
            raise AnalysisDenied("sandbox")

        def deny(name, error, comparisons=()):
            number = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            array = (_Comparison * len(comparisons))(*(
                _Comparison(arg, 7, mask, value) for arg, mask, value in comparisons))
            if number < 0 or lib.seccomp_rule_add_array(
                    context, 0x00050000 | error, number, len(array), array) != 0:
                raise AnalysisDenied("sandbox")

        # Match both chmod and creation, including O_TMPFILE (no O_CREAT bit).
        modes = (("chmod", 1), ("fchmod", 1), ("fchmodat", 2), ("fchmodat2", 2),
                 ("mkdir", 1), ("mkdirat", 2), ("mknod", 1), ("mknodat", 2), ("creat", 1))
        for bit in (stat.S_ISUID, stat.S_ISGID):
            for name, arg in modes:
                deny(name, errno.EPERM, ((arg, bit, bit),))
            for name, arg in (("open", 1), ("openat", 2)):
                for flag in (os.O_CREAT, os.O_TMPFILE):
                    deny(name, errno.EPERM, ((arg, flag, flag), (arg + 1, bit, bit)))
        # seccomp cannot inspect struct open_how; force clients onto the filtered
        # open/openat ABI, as systemd's RestrictSUIDSGID does. Setup already ended.
        deny("openat2", errno.ENOSYS)
        if lib.seccomp_load(context) != 0:
            raise AnalysisDenied("sandbox")
    finally:
        lib.seccomp_release(context)
