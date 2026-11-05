"""Authenticate installed systemd SOCK_SEQPACKET endpoints at fixed paths supplied by the caller."""
import os
from pathlib import Path
import socket
import stat

from .analysis_contract import AnalysisDenied
from . import e2_ipc


def endpoint(path, uid):
    for name in reversed(Path(path).parents):
        info = os.lstat(name)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise AnalysisDenied("endpoint")
    info = os.lstat(path)
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise AnalysisDenied("endpoint")
    return info.st_dev, info.st_ino


def manager_peer(channel, path):
    if (channel.family != socket.AF_UNIX
            or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
            or channel.getpeername() != path
            or e2_ipc._kernel_peer(channel) != (1, 0, 0)):
        raise AnalysisDenied("endpoint")


def listener(path, name):
    env = {key: os.environ.pop(key, None) for key in (
        "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES", "LISTEN_PIDFDID")}
    if (env["LISTEN_PID"] != str(os.getpid()) or env["LISTEN_FDS"] != "1"
            or env["LISTEN_FDNAMES"] != name):
        raise AnalysisDenied("endpoint")
    channel = socket.socket(fileno=3)
    try:
        os.set_inheritable(3, False)
        if (channel.family != socket.AF_UNIX
                or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                or channel.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
                or channel.getsockname() != path):
            raise AnalysisDenied("endpoint")
        return channel
    except BaseException:
        channel.close()
        raise
