"""A policy-bound full project copy with an approved edit overlaid locally.

The model request still contains only its selected files. This object owns
local test inputs and does not expose them or test output as model context.
"""
import copy
import hashlib

from . import copy_contract, permissions, project_scope, task_contract, task_sandbox
from .analysis_contract import canonical, terminal_text
from .terminal_consent import confirm


class PreparedProjectTest:
    def __init__(self, policy, root, workspace, before):
        self._policy = policy
        self._root = permissions.absolute_path(root)
        self._workspace = permissions.absolute_path(workspace)
        if before["workspace"] != self._workspace:
            raise ValueError("project test workspace")
        # A broad policy on HOME must not turn an implicit project check into
        # a recursive HOME copy. The caller supplies the actual project root.
        if (self._root in (policy.home, "/", "/home", "/root", "/tmp")
                or not any(scope.mode == "tree" and
                           (self._root == scope.base or self._root.startswith(scope.base + "/"))
                           for scope in policy.scopes())):
            raise ValueError("project test scope")
        self._before = copy.deepcopy(before)
        self._project = self._scan()
        self._entries = self._project.test_selection()
        self._state = self._signature(self._project)
        self._proof = None

    def _scan(self):
        self._policy.recheck()
        return project_scope.Project(self._policy, self._root,
                                     scan_limit=self._policy.limits().search_scan).scan()

    @staticmethod
    def _signature(project):
        return (project.files, tuple(sorted(project.stats.items())), project.complete, project.reason)

    def check(self):
        """Revalidate the complete original index, before testing or before a write."""
        current = self._scan()
        current.test_selection()
        if self._signature(current) != self._state:
            raise project_scope.ProjectError("TEST_CHANGED")

    def _relative(self, name):
        absolute = self._workspace + "/" + task_contract.path(name)
        if not absolute.startswith(self._root + "/"):
            raise ValueError("edit outside tested project")
        return copy_contract.path(absolute[len(self._root) + 1:])

    def _overlay(self, source, proposed, files):
        if {item["path"] for item in source["files"]} != set(self._before["files"]):
            raise ValueError("project test selection")
        for item in source["files"]:
            original = self._before["files"][item["path"]]["file"]
            expected = task_contract.plain_text(original["content"]) if original else None
            if item["content"] != expected:
                raise ValueError("project test source changed")
        formatted = task_contract.formatted_replacements(proposed,
            {name: item["file"] for name, item in self._before["files"].items()})
        for item in formatted:
            relative = self._relative(item["path"])
            absolute = self._root + "/" + relative
            # Creation needs write authority even though the target was absent
            # from the read index. A denied name must never enter the copy.
            if self._policy.excludes(absolute) or not self._policy.can_write(absolute):
                raise ValueError("project test write denied")
            original = self._before["files"][item["path"]]["file"]
            if original is None:
                if relative in files:
                    raise project_scope.ProjectError("TEST_CHANGED")
            elif relative not in files or files[relative] != original["content"]:
                raise project_scope.ProjectError("TEST_CHANGED")
            files[relative] = item["content"]
        limits = self._policy.limits()
        total = copy_contract.validate_files(files)
        if (len(files) > limits.test_copy_files or total > limits.test_copy_total_size
                or any(len(content.encode("utf-8")) > limits.test_copy_file_size for content in files.values())):
            raise project_scope.TestScopeRefused("The proposed changes exceed the configured local test-copy budget.")

    def run(self, source, proposed_files, check, reader, write_guard):
        """Test the full pinned copy plus proposal; only a successful result is proof."""
        self._proof = None
        if check not in task_contract.TEST_SUITES:
            raise ValueError("project test suite")
        proposed = task_contract.proposal(canonical({"summary": "test", "files": proposed_files}), source)["files"]
        if write_guard is not None:
            write_guard()
        self.check()
        excluded = sum(value for name, value in self._project.stats.items() if name not in ("scanned", "indexed"))
        revision = hashlib.sha256(repr(self._state).encode("utf-8")).hexdigest()[:12]
        print("Offline %s on the complete permitted project copy with the proposed changes: "
              "%d indexed file(s), %d excluded entries, snapshot %s." %
              (check, len(self._entries), excluded, revision))
        print("Project: " + terminal_text(self._root))
        print("Network, user HOME and host writes are blocked. Additional project files and test output stay local.")
        if not confirm("project-test", reader=reader):
            return {"passed": False, "exit_code": 2, "output": "Test cancelled. Nothing was saved.", "check": check}
        if write_guard is not None:
            write_guard()
        self.check()
        files = self._project.test_copy(self._entries)
        self._overlay(source, proposed, files)
        self.check()
        if write_guard is not None:
            write_guard()
        executable = [entry.path for entry in self._entries if entry.identity[2] & 0o100]
        result = task_sandbox.run_copy(files, check, executable=executable)
        self.check()
        if write_guard is not None:
            write_guard()
        if result["passed"]:
            self._proof = (check, hashlib.sha256(canonical(proposed).encode("utf-8")).hexdigest())
        return result
