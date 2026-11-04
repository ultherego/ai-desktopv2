"""Shared installer identity, path and launcher policy; no writes on import."""
import os
import shlex
import stat

from .workspace_executor import _root, ExecutionDenied


class InstallDenied(RuntimeError):
    pass


def _prefix(prefix):
    if type(prefix) is not str:
        raise InstallDenied("INSTALL: REFUSED")
    try:
        fd = _root(prefix)
    except ExecutionDenied:
        raise InstallDenied("INSTALL: REFUSED") from None
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            raise InstallDenied("INSTALL: REFUSED")
        return fd
    except OSError:
        os.close(fd)
        raise InstallDenied("INSTALL: REFUSED") from None
    except BaseException:
        os.close(fd)
        raise


def _wrapper(prefix, *, legacy=False, isolated=True):
    # The caller has already applied _root's strict lexical path validation.
    guard = "" if legacy else (
        "/usr/bin/test -f '" + prefix + "/install-manifest.json' || exit 2\n"
        + "/usr/bin/test ! -e '" + prefix + "/install-journal.json' || exit 2\n"
        + "/usr/bin/test ! -e '" + prefix + "/remove-journal.json' || exit 2\n")
    if legacy or isolated is False:
        # Exact historic v1/v2 bytes remain recognizable for removal/upgrade.
        launch = ("PYTHONPATH='" + prefix
                  + "/lib' exec /usr/bin/python3 -B -m ai_desktop \"$@\"\n")
    else:
        # -I excludes cwd, PYTHONPATH/PYTHONHOME and user site. Add only the
        # selected package, then retain normal -m/argv behavior through runpy.
        script = ("import runpy,sys; sys.path.insert(0, " + repr(prefix + "/lib")
                  + "); runpy.run_module('ai_desktop', run_name='__main__', alter_sys=True)")
        launch = "exec /usr/bin/python3 -I -B -c " + shlex.quote(script) + ' "$@"\n'
    return ("#!/bin/sh\n" + guard + launch).encode("utf-8")


