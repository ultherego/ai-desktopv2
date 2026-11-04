"""Durable Btrfs filesystem/subvolume identity for binding saved task history.

`st_dev` of a Btrfs subvolume is an anonymous number allocated at mount time,
so it may change after a remount or reboot while inode numbers inside the
subvolume stay valid. Two read-only, unprivileged ioctls give identifiers that
survive that: the filesystem UUID (BTRFS_IOC_FS_INFO) and the subvolume tree id
and UUID (BTRFS_IOC_GET_SUBVOL_INFO). A snapshot or `cp -a` copy has another
tree id/UUID or other inodes, so it is never mistaken for the original.

This identity only says WHICH subvolume a journal belongs to. It never replaces
the checks of current path, parent, inode, mode and bytes, and it does not
protect against a same-UID process. Other filesystems return None; callers
then keep the strict `st_dev` comparison. Nothing here creates or changes
filesystems or subvolumes and no ioctl needs privileges.
"""
import ctypes
import fcntl
import struct

BTRFS_SUPER_MAGIC = 0x9123683E
_IOC_READ = 2


def _ior(kind, number, size):
    return (_IOC_READ << 30) | (size << 16) | (kind << 8) | number


FS_INFO = _ior(0x94, 31, 1024)       # struct btrfs_ioctl_fs_info_args
SUBVOL_INFO = _ior(0x94, 60, 504)    # struct btrfs_ioctl_get_subvol_info_args
_FSID = slice(16, 32)                # fs_info.fsid
_TREE_ID = 0                         # subvol_info.treeid (u64)
_UUID = slice(296, 312)              # subvol_info.uuid


def _is_btrfs(fd):
    libc = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(256)
    if libc.fstatfs(fd, buffer) != 0:
        return False
    return struct.unpack_from("@l", buffer)[0] & 0xFFFFFFFF == BTRFS_SUPER_MAGIC


def identity(fd):
    """Persistent identity of the subvolume holding `fd`, or None if unknown."""
    try:
        if not _is_btrfs(fd):
            return None
        info, subvol = bytearray(1024), bytearray(504)
        fcntl.ioctl(fd, FS_INFO, info, True)
        fcntl.ioctl(fd, SUBVOL_INFO, subvol, True)
        fsid, uuid = bytes(info[_FSID]), bytes(subvol[_UUID])
        tree = struct.unpack_from("<Q", subvol, _TREE_ID)[0]
    except (OSError, ValueError, TypeError, AttributeError, struct.error):
        return None
    if not any(fsid) or tree == 0:
        return None
    # The top-level subvolume has an all-zero UUID; fsid + tree id still bind it.
    return {"kind": "btrfs", "fsid": fsid.hex(), "tree": tree, "uuid": uuid.hex()}


def valid(value):
    """Strict schema of a stored identity (None means 'not durable')."""
    if value is None:
        return True
    return (type(value) is dict and set(value) == {"kind", "fsid", "tree", "uuid"}
            and value["kind"] == "btrfs"
            and all(type(value[k]) is str and len(value[k]) == 32
                    and all(c in "0123456789abcdef" for c in value[k]) for k in ("fsid", "uuid"))
            and any(c != "0" for c in value["fsid"])
            and type(value["tree"]) is int and 0 < value["tree"] < 2 ** 64)
