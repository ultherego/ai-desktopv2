"""Explicit side-by-side upgrade: previous installation is always read-only.

No active-pointer/PATH switching, in-place replacement or state/OAuth migration.
A candidate is a separate private prefix; rollback removes that candidate only.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path

from . import install_user as installer
from . import install_lifecycle as lifecycle
from .install_policy import InstallDenied, _prefix, _wrapper


def _pair(previous, candidate):
    if type(previous) is not str or type(candidate) is not str:
        raise InstallDenied("INSTALL: REFUSED")
    old, new = Path(previous), Path(candidate)
    if old == new or old in new.parents or new in old.parents:
        raise InstallDenied("INSTALL: REFUSED")


def _previous_state(prefix):
    fd = _prefix(prefix)
    try:
        state = lifecycle._inspect(fd, prefix, "uninstall")
        # A partial installation/removal is not a usable rollback destination.
        names = {item["path"] for item in state["files"]} & lifecycle.METADATA
        if names != {lifecycle.MANIFEST}:
            raise InstallDenied("INSTALL: REFUSED")
        fact, raw = lifecycle._read(fd, lifecycle.MANIFEST, metadata=True)
        expected = next(item for item in state["files"] if item["path"] == lifecycle.MANIFEST)
        if fact != {k: v for k, v in expected.items() if k != "path"}:
            raise InstallDenied("INSTALL: REFUSED")
        source_sha = json.loads(raw, object_pairs_hook=lifecycle._unique)["sha256"]
        # This is an inventory for retention, never an instruction to delete old.
        launcher = next(item for item in state["files"] if item["path"] == "bin/ai-desktop")
        isolated = launcher["sha256"] == hashlib.sha256(_wrapper(prefix)).hexdigest()
        return {"prefix": prefix, "access": "read-only", "root": state["root"],
                "files": state["files"], "directories": state["directories"],
                "source_sha256": source_sha, "launcher_isolated": isolated}
    finally:
        os.close(fd)


def preview(previous, candidate, action="upgrade"):
    try:
        _pair(previous, candidate)
        if action not in ("upgrade", "rollback-upgrade"):
            raise InstallDenied("INSTALL: REFUSED")
        old = _previous_state(previous)
        if action == "rollback-upgrade" and old["launcher_isolated"] is not True:
            # Retain a staged safe candidate if the fallback has a historic
            # cwd/PYTHONPATH-sensitive launcher. Never silently rewrite old.
            raise InstallDenied("INSTALL: REFUSED")
        fd = _prefix(candidate)
        try:
            root = lifecycle._directory(fd)
            if (root["dev"], root["ino"]) == (old["root"]["dev"], old["root"]["ino"]):
                raise InstallDenied("INSTALL: REFUSED")
            if action == "upgrade":
                if os.listdir(fd):
                    raise InstallDenied("INSTALL: REFUSED")
                source = installer.preview()
                if source["sha256"] == old["source_sha256"]:
                    raise InstallDenied("INSTALL: REFUSED")
                target = {"prefix": candidate, "root": root, "source": source}
            else:
                target = lifecycle._inspect(fd, candidate, "uninstall")
        finally:
            os.close(fd)
        plan = {"schema": 1, "action": action, "previous": old, "candidate": target,
                "activation": "explicit-candidate-launcher-only"}
        return {**plan, "sha256": lifecycle._digest(plan)}
    except (OSError, ValueError, TypeError, RecursionError, KeyError):
        raise InstallDenied("INSTALL: REFUSED") from None


def apply(previous, candidate, confirmed_sha256, action="upgrade"):
    try:
        if not lifecycle._hex(confirmed_sha256):
            raise InstallDenied("INSTALL: REFUSED")
        plan = preview(previous, candidate, action)
        if not hmac.compare_digest(plan["sha256"], confirmed_sha256):
            raise InstallDenied("INSTALL: REFUSED")
        target = plan["candidate"]
        if action == "upgrade":
            # The installer reopens the selected path. Bind that actual fd to
            # the approved identity before its first write, not just its name.
            installer.install(candidate, target["source"]["sha256"],
                expected_prefix_identity=(target["root"]["dev"], target["root"]["ino"]))
            return {"status": "STAGED", "previous_retained": True,
                    "rollback_supported": plan["previous"]["launcher_isolated"],
                    "launcher": candidate + "/bin/ai-desktop",
                    "source_sha256": target["source"]["sha256"]}
        # remove() reopens and hashes the candidate too, preserving its existing
        # journal/retry behavior. Invalid old state above never falls back here.
        lifecycle.remove(candidate, target["sha256"], "uninstall")
        return {"status": "ROLLED_BACK", "previous_retained": True,
                "launcher": previous + "/bin/ai-desktop", "candidate_prefix_retained": True}
    except (OSError, ValueError, TypeError, RecursionError, KeyError):
        raise InstallDenied("INSTALL: REFUSED") from None
