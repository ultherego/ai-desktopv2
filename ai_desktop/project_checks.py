"""A local project test. Only its bounded outcome can become conversation data."""
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import os

from . import permissions, project_scope, task_contract, task_sandbox
from .analysis_contract import AnalysisDenied, terminal_text
from .terminal_consent import confirm


@dataclass(frozen=True)
class Result:
    code: int
    summary: str = ""
    sources: tuple = ()


def current_directory(policy):
    """A configured recursive read scope must already cover cwd. No implicit HOME/project discovery."""
    if type(policy) is not permissions.Policy:
        return None
    try:
        root = os.getcwd()
        if (root in (policy.home, "/", "/home", "/root", "/tmp") or not policy.allows(root + "/_")
                or policy.excludes(root) or not any(s.mode == "tree" and (root == s.base or root.startswith(s.base + "/"))
                                                   for s in policy.scopes())):
            return None
        return root
    except (OSError, ValueError):
        return None


def current(recipe, reader, *, policy=None, permit=None):
    try:
        if policy is None and permit is None:
            policy = permissions.load_policy(optional=True)
        root = current_directory(policy)
        if root is not None:
            project = project_scope.Project(policy, root, scan_limit=policy.limits().search_scan)
            return run(project, recipe, reader, permit=permit)
    except (OSError, ValueError):
        pass
    print("Test refused. Open a project directory already covered by a recursive files.read rule.")
    return Result(2)


def run(project, recipe, reader, *, permit=None):
    from .run_controls import require_changes
    if recipe not in task_contract.TEST_SUITES:
        print("Available offline suites: " + ", ".join(task_contract.TEST_SUITES))
        return Result(2)
    try:
        require_changes()
        if permit is not None:
            permit.check()
        project.scan()
        entries = project.test_selection()
        skipped = sum(value for name, value in project.stats.items() if name not in ("scanned", "indexed"))
        workspace = hashlib.sha256(os.fsencode(project.root)).hexdigest()[:12]
        revision = hashlib.sha256(repr([(e.path, e.identity) for e in entries]).encode()).hexdigest()[:12]
        print("Offline %s on a temporary copy of %d indexed file(s); %d entries excluded from the index." %
              (recipe, len(entries), skipped))
        print("Project: %s (workspace %s, snapshot %s)" % (terminal_text(project.root), workspace, revision))
        print("Network, user HOME and host writes are blocked. Test output stays in this terminal.")
        with permit.waiting() if permit is not None else nullcontext():
            if not confirm("project-test", reader=reader):
                print("Test cancelled.")
                return Result(2)
        if permit is not None:
            permit.check()
        files = project.test_copy(entries)
        executable = [entry.path for entry in entries if entry.identity[2] & 0o100]
        project.policy.recheck()
        if permit is not None:
            permit.check()
        result = task_sandbox.run_copy(files, recipe, executable=executable)
    except EOFError:
        return Result(2)
    except AnalysisDenied:
        print("Test refused by the active profile or consent.")
        return Result(2)
    except (OSError, ValueError) as exc:
        reasons = {"TEST_SIZE": "the complete index must fit the file count, per-file and total read limits",
                   "TEST_CHANGED": "the indexed project changed during the copy",
                   "TEST_FORMAT": "the index includes an unsupported name, binary or protected content"}
        print("Test refused: " + reasons.get(getattr(exc, "code", None), "the policy, files or sandbox are unavailable") + ".")
        return Result(2)
    print("Untrusted local test output:")
    print(terminal_text(result["output"]))
    state = "passed" if result["passed"] else "failed or could not complete"
    if recipe == "npm-test" and result["passed"]:
        state = "script exited 0 (test count unknown)"
    summary = ("%s %s on the tested copy (workspace %s, snapshot %s, %d indexed files, %d entries excluded)." %
               (recipe, state, workspace, revision, len(entries), skipped))
    print(summary)
    return Result(0 if result["passed"] else 2, summary, tuple(project.root + "/" + e.path for e in entries))
