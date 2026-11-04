"""Atomic publication to a free Linux directory entry; never fall back to replacement."""
import ctypes
import errno
import os

_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    _RENAMEAT2.restype = ctypes.c_int


def rename_noreplace(src_dir, src, dst_dir, dst):
    """Move through verified directory FDs only if the destination is still absent."""
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is required for safe publication")
    if _RENAMEAT2(src_dir, os.fsencode(src), dst_dir, os.fsencode(dst), 1) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), dst)
