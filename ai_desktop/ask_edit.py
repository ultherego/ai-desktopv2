"""Adapt one configured edit proposal to the existing tested task executor.

The model selects a configured write scope, never an arbitrary workspace.
Existing data must also be readable; creation grants never read old contents.
"""
from dataclasses import dataclass
import json
import shlex
from types import SimpleNamespace

from . import conversation_state, file_context, permissions, task_contract, task_workspace, workspace_trash
from .analysis_contract import AnalysisDenied, canonical, terminal_text
from .assistant_contract import parse_plan
from .question_consent import QuestionConsent
from .terminal_style import size_label
from .workspace_executor import ExecutionDenied


class EditDenied(AnalysisDenied):
    """A local edit selection or its bound state was refused."""


class CreateCollision(EditDenied):
    """A validated creation path exists; never includes its contents."""

    def __init__(self, path, *, workspace):
        self.path = task_contract.path(path)
        # Local UI metadata only: never add this root to the model question.
        self.workspace = permissions._absolute(workspace)
        super().__init__("input")


@dataclass(frozen=True)
class CompletedTask:
    code: int


@dataclass(frozen=True)
class PreparedEdit:
    before: dict
    source: dict
    check: str
    workspace: str
    write_grant: object


class WriteGrant:
    def __init__(self, permit, snapshot, index, before, source, read_paths, targets):
        if type(permit) is not QuestionConsent or snapshot.policy is None:
            raise EditDenied("consent")
        self._permit, self._snapshot, self._policy = permit, snapshot, snapshot.policy
        self._index = index
        self._before, self._source = before, source
        self._policy_state = canonical({"access": snapshot.access, "sources": snapshot.sources})
        self._read_paths, self._targets = tuple(read_paths), tuple(targets)
        self._state = self._binding()

    def _binding(self):
        return canonical({"before": self._before, "source": self._source,
                          "index": self._index, "read_paths": self._read_paths, "targets": self._targets})

    def check(self):
        """Write approval has its own TTL; policy and targets remain bound."""
        try:
            if (self._snapshot.policy is not self._policy
                    or canonical({"access": self._snapshot.access, "sources": self._snapshot.sources}) != self._policy_state
                    or self._binding() != self._state):
                raise ValueError()
            self._snapshot.require_claude()
            if self._read_paths and self._policy.select(self._read_paths, limit=self._policy.limits().read_files) != self._read_paths:
                raise ValueError()
            if self._policy.require_write(self._targets, index=self._index) != self._targets:
                raise ValueError()
            task_workspace.unchanged(self._before)
            self._snapshot.require_claude()
            if (self._snapshot.policy is not self._policy or self._binding() != self._state
                    or canonical({"access": self._snapshot.access, "sources": self._snapshot.sources}) != self._policy_state):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError, AttributeError, ExecutionDenied):
            raise EditDenied("input") from None

    def require_transfer(self, permit, source):
        if permit is not self._permit or canonical(source) != canonical(self._source):
            raise EditDenied("consent")
        permit.require_write_scope(self._policy, "w" + str(self._index))
        if source["instruction"] != permit.question:
            raise EditDenied("consent")
        self.check()
        permit.check()


def prepare(plan, snapshot, args, reader, *, question_consent, expected_versions=None):
    from .run_controls import require_changes
    require_changes()
    """Snapshot only approved readable data and absent writable new targets."""
    try:
        plan = parse_plan(canonical(plan), snapshot.access)
        if plan["action"] != "edit" or type(question_consent) is not QuestionConsent:
            raise ValueError()
        policy = snapshot.policy
        question_consent.require_write_scope(policy, plan["write"])
        index = int(plan["write"][1:])
        scopes = {"w" + str(scope.index): scope for scope in policy.write_scopes()}
        scope = scopes[plan["write"]]
        advertised = {item["id"]: item["pattern"] for item in snapshot.access.get("write", [])}
        if scope.pattern != advertised[plan["write"]]:
            raise ValueError()
        workspace = policy.write_workspace(index)
        detached = task_workspace._can_apply(workspace)
        if args.details and type(detached) is int and detached:
            print(task_workspace.DETACHED_HISTORY_HINT)
        read_paths, contents = (), {}
        if plan["files"]:
            question_consent.require_files(policy, plan["files"])
            read_paths = file_context.resolve_paths(policy, plan["files"])
            if any(not path.startswith(workspace + "/") for path in read_paths):
                raise ValueError()
            question_consent.check()
            bundle = permissions.read_selected(policy, read_paths)
            conversation_state.note_sources([file["path"] for file in bundle["files"]])
            contents = {item["path"]: item["content"] for item in bundle["files"]}
            if set(contents) != set(read_paths):
                raise ValueError()
        question_consent.check()
        existing = [task_contract.path(path[len(workspace) + 1:]) for path in read_paths]
        if expected_versions is not None and (type(expected_versions) is not dict or set(expected_versions) != set(existing)):
            raise ValueError("expected versions")
        created = list(plan["create"])
        names = existing + created
        if not names or len(set(names)) != len(names) or len(names) > task_contract.MAX_FILES:
            raise ValueError()
        if created:
            policy.require_write(tuple(workspace + "/" + name for name in created), index=index)
        try:
            before = task_workspace.snapshot(workspace, names, must_be_absent=created)
        except task_workspace.NewTargetExists as exc:
            # Only a typed, pre-read collision may lead to a user clarification.
            # All other refusals still fail closed without a model retry.
            raise CreateCollision(exc.path, workspace=workspace) from None
        files, targets = [], []
        for name in names:
            path = workspace + "/" + name
            old = before["files"][name]["file"]
            if name in created:
                if old is not None:
                    raise ValueError()
                content, writable = None, True
            else:
                if old is None or old["content"] != contents[path]:
                    raise ValueError()
                if expected_versions is not None and old["sha256"] != expected_versions[name]:
                    raise ValueError("changed since project approval")
                content = contents[path]
                writable = policy.can_write(path, index=index)
            if writable:
                targets.append(path)
            files.append({"path": name, "content": content, "writable": writable})
        source = task_contract.request({"instruction": question_consent.question, "files": files})
        grant = WriteGrant(question_consent, snapshot, index, before, source, read_paths, targets)
        grant.require_transfer(question_consent, source)
        return PreparedEdit(before, source, plan["check"], workspace, grant)
    except (EditDenied, AnalysisDenied):
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ExecutionDenied):
        raise EditDenied("input") from None


def delete_files(plan, snapshot, reader):
    """Move the listed files to the task's recoverable trash after the owner's yes. Nothing is sent to the model."""
    from .run_controls import require_changes
    from .terminal_consent import confirm
    require_changes()
    policy = snapshot.policy
    try:
        if policy is None or plan["action"] != "delete" or plan["write"] not in {
                item["id"] for item in snapshot.access.get("write", [])}:
            raise ValueError()
        index = int(plan["write"][1:])
        workspace = policy.write_workspace(index)
        names = [task_contract.path(name) for name in plan["paths"]]
        policy.require_write(tuple(workspace + "/" + name for name in names), index=index)
        prepared = workspace_trash.prepare(workspace, names)
        sizes = [entry["size"] for entry in prepared.data()["entries"]]
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ExecutionDenied):
        raise EditDenied("input") from None
    print()
    print("Do usunięcia w " + terminal_text(workspace) + ":")
    for name, size in zip(names, sizes):
        print("  " + terminal_text(name) + " (" + size_label(size) + ")")
    summary = ", ".join(names[:4])
    if not confirm("project-delete", reader=reader):
        print("Anulowano. Nic nie usunięto.")
        conversation_state.note_action("Usunięcie NIE zostało wykonane (odrzucone przez użytkownika): " + summary)
        return CompletedTask(2)
    restore = shlex.join(["ai-desktop", "task", "restore", "--workspace", workspace, "--plan", prepared.data()["plan"]])
    try:
        policy.recheck()
        plan_id = workspace_trash.execute(prepared, prepared.authorize())
    except workspace_trash.TrashRecoveryRequired:
        print("Usuwanie przerwane; dane i dziennik zachowano. Stan: "
              + shlex.join(["ai-desktop", "task", "status", "--workspace", workspace]))
        print("Przywrócenie: " + restore)
        conversation_state.note_action("Usuwanie przerwane, wymaga sprawdzenia: " + summary)
        return CompletedTask(2)
    except KeyboardInterrupt:
        print("\nPrzerwano usuwanie. Przywrócenie: " + restore)
        conversation_state.note_action("Usuwanie przerwane, wymaga sprawdzenia: " + summary)
        return CompletedTask(2)
    except (OSError, ValueError, ExecutionDenied):
        raise EditDenied("input") from None
    print("Usunięto %d (w koszu zadania)." % len(names))
    print("Przywrócenie: " + shlex.join(["ai-desktop", "task", "restore", "--workspace", workspace, "--plan", plan_id]))
    conversation_state.note_action("Usunięto do kosza zadania, można przywrócić: " + summary)
    return CompletedTask(0)


def finish(prepared, response, args, reader):
    from .task_cli import finish_task
    values = dict(vars(args), workspace=prepared.workspace, test=prepared.check)
    try:
        code = finish_task(prepared.before, prepared.source, response, SimpleNamespace(**values), reader,
                           write_guard=prepared.write_grant.check)
    except (ExecutionDenied, RecursionError):
        raise EditDenied("input") from None
    conversation_state.note_action(_change_fact(response, code, prepared.check))
    return CompletedTask(code)


def _change_fact(response, code, check):
    """What really happened to a proposed change, from the controller's own result (never from the model's words)."""
    try:
        proposal = json.loads(response)
        files = ", ".join(str(item["path"]) for item in proposal["files"][:4])
        summary = " ".join(str(proposal["summary"]).split())[:120]
    except (ValueError, TypeError, KeyError, AttributeError):
        files, summary = "?", ""
    if code == 0:
        return ("Zastosowano zmianę przez ask (test %s zaliczony, można ją cofnąć): %s [%s]" % (check, summary, files)).strip()
    return "Zmiana NIE została zastosowana (kod %s; anulowana, odrzucona albo test niezaliczony): %s [%s]" % (code, summary, files)
