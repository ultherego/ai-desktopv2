"""Run real unit discovery with a private, non-default ambient profile.

Only Store's omitted home argument is redirected. Explicit test homes, profile
resolution, readonly restrictions, identity checks and the suite itself stay
real. No existing account configuration is opened or changed.
"""
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import pwd
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_desktop import agent_profiles as profiles, execution, run_controls
from ai_desktop.analysis_contract import AnalysisDenied

MARKER = "SYNTHETIC ambient readonly profile; never use a personal configuration."


def ambient_config():
    config = profiles.defaults()
    for name, selected in config.entries:
        config = config.put(name, replace(selected, provider="codex", model="default", effort="xhigh",
            instructions=MARKER, access="read-only", skills=("code-review",)))
    config = config.put("ambient-reviewer", profiles.Profile(provider="codex", effort="xhigh",
        instructions=MARKER, access="read-only", skills=("code-review",)))
    return config.select("ambient-reviewer").with_provider("codex")


def snapshot(home, *, include_root=False):
    """Record only the controlled temporary tree, refusing links/special files."""
    records = []
    def visit(directory, depth=0):
        if depth > 8:
            raise ValueError("fixture-depth")
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
        if len(names) > 32:
            raise ValueError("fixture-entries")
        for name in names:
            path = directory / name
            info = path.lstat()
            metadata = (str(path.relative_to(home)), stat.S_IMODE(info.st_mode), info.st_uid,
                        info.st_gid, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)
            if stat.S_ISDIR(info.st_mode):
                records.append(("directory", metadata))
                visit(path, depth + 1)
            elif stat.S_ISREG(info.st_mode) and info.st_size <= profiles.MAX_BYTES:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    content = os.read(descriptor, profiles.MAX_BYTES + 1)
                finally:
                    os.close(descriptor)
                if len(content) > profiles.MAX_BYTES:
                    raise ValueError("fixture-size")
                records.append(("file", metadata, content))
            else:
                raise ValueError("fixture-type")
    if include_root:
        info = home.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("fixture-type")
        records.append(("root", stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid,
                        info.st_nlink, info.st_mtime_ns, info.st_ctime_ns))
    visit(home)
    return tuple(records)


@contextmanager
def ambient_store(home):
    """Use a real Store with a controlled default; never patch resolve/guards."""
    original = profiles.Store
    fixture_home = home
    class FixtureStore(original):
        def __init__(self, home=None):
            # Preserve positional/keyword explicit homes used by integration
            # tests; change only the default path, not Store's validation.
            original.__init__(self, fixture_home if home is None else home)
    with patch.object(profiles, "Store", FixtureStore):
        yield


def check_canary(expected):
    selected = profiles.resolve()
    if (selected != expected or selected["provider"] != "codex"
            or selected["access"] != "read-only" or selected["effort"] != "xhigh"
            or selected["profile"] != "ambient-reviewer" or MARKER not in selected["instructions"]):
        raise ValueError("ambient-canary")
    try:
        with execution.using(selected):
            run_controls.require_changes()
    except AnalysisDenied:
        pass
    else:
        raise ValueError("ambient-readonly")


def run(*, loader=None, runner=None, parent=None):
    loader = unittest.TestLoader() if loader is None else loader
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2) if runner is None else runner
    # Path.home()/HOME are intentionally not authority for the real profile.
    # This is only a new private fixture below a secure passwd-derived home.
    parent = Path(pwd.getpwuid(os.getuid()).pw_dir) if parent is None else Path(parent)
    with tempfile.TemporaryDirectory(prefix="aid-ambient-profile-", dir=parent) as name:
        home = Path(name)
        store = profiles.Store(home)
        store.mutate(lambda _: ambient_config(), expected=store.load())
        expected = profiles.resolve(store=store)
        before = snapshot(home, include_root=True)
        try:
            with ambient_store(home):
                check_canary(expected)
                suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
                result = runner.run(suite)
                check_canary(expected)
        finally:
            if snapshot(home, include_root=True) != before:
                raise ValueError("ambient-config-changed")
        if not result.wasSuccessful():
            print("PROFILE ENVIRONMENT: FAIL unit suite")
            return 1
        print("PROFILE ENVIRONMENT: PASS active=codex-readonly config=unchanged personal-config=unused")
        return 0


def main(argv=None):
    if (sys.argv[1:] if argv is None else list(argv)) != ["--allow-synthetic-profile-test"]:
        print("PROFILE ENVIRONMENT: REFUSED (explicit synthetic-test flag required)")
        return 2
    try:
        return run()
    except (OSError, ValueError):
        print("PROFILE ENVIRONMENT: FAIL fixture integrity")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
