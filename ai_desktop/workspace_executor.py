"""E5 controlled create-only workspace executor, never invoked by AI.

Caller must present an E4 authorization from the SAME trusted PlanSession.
Only freshly created text files are permitted, O_EXCL/O_NOFOLLOW; no shell,
delete/overwrite or system operations. Audit metadata is stored inside the
explicitly chosen workspace. No host request is made on import.

Important: cannot defend against a separate malicious process with the same
UID deliberately renaming opened directories: that threat remains out of scope.
"""
import hashlib
import json
import os
import pwd
import secrets
import stat

from . import workspace_journal as journal
from .typed_plan import PlanDenied, PlanSession, _valid_workspace

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class _IncompleteWrite(OSError):
    """Private recovery evidence for a failed exclusive write and cleanup."""

    def __init__(self, inode, digest):
        super().__init__("incomplete exclusive write")
        self.inode = inode
        self.digest = digest


class ExecutionDenied(RuntimeError):
    """Refusal with no path or secret values."""


def _root(workspace):
    """Traverse from '/', rejecting *every* symlink, not just final path."""
    try:
        _valid_workspace(workspace)
        if os.geteuid() == 0:
            raise ExecutionDenied("EXECUTOR: REFUSED")
        # Every writer, including the legacy workspace CLI and installer,
        # must select a project/prefix rather than the user's entire HOME.
        # Environment HOME is caller-controlled and cannot move this boundary.
        home = pwd.getpwuid(os.geteuid()).pw_dir
        if (type(home) is not str or not os.path.isabs(home)
                or workspace == os.path.normpath(home)):
            raise ExecutionDenied("EXECUTOR: REFUSED")
        fd = os.open("/", _DIR_FLAGS)
        try:
            for component in workspace.split("/")[1:]:
                nxt = os.open(component, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = nxt
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise ExecutionDenied("EXECUTOR: REFUSED")
            if info.st_mode & 0o022:
                raise ExecutionDenied("EXECUTOR: REFUSED")
            return fd
        except BaseException:
            os.close(fd)
            raise
    except (OSError, PlanDenied, ValueError, TypeError, KeyError):
        raise ExecutionDenied("EXECUTOR: REFUSED") from None


def _parent(root_fd, relative):
    fd = os.dup(root_fd)
    try:
        parts = relative.split("/")
        for component in parts[:-1]:
            nxt = os.open(component, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
            st = os.fstat(fd)
            if st.st_uid != os.geteuid() or st.st_mode & 0o022:
                raise ExecutionDenied("EXECUTOR: REFUSED")
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def _state(root_fd, operations, workspace):
    """Snapshot of target directory identity + absence of all new paths."""
    current = os.fstat(root_fd)
    facts = {"workspace": workspace, "root": [current.st_dev, current.st_ino],
             "targets": []}
    for relative, _ in operations:
        fd, name = _parent(root_fd, relative)
        try:
            st = os.fstat(fd)
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ExecutionDenied("EXECUTOR: REFUSED")
            facts["targets"].append([relative, st.st_dev, st.st_ino])
        finally:
            os.close(fd)
    canonical = json.dumps(facts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def inspect_state(workspace, operations):
    """Read-only fingerprint for E4 review, not a filesystem mutation."""
    fd = _root(workspace)
    try:
        return _state(fd, operations, workspace)
    except (OSError, ValueError, TypeError, PlanDenied):
        raise ExecutionDenied("EXECUTOR: REFUSED") from None
    finally:
        os.close(fd)


def _remove_verified(fd, name, accept):
    """Unlink name only if it is a single-link regular file accepted by
    accept(info, content). Returns True when retired (removed or absent)."""
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                         os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(source)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_size > 4096):
                return False
            content = os.read(source, info.st_size + 1)
            if not accept(info, content):
                return False
            os.unlink(name, dir_fd=fd)
            return True
        finally:
            os.close(source)
    except FileNotFoundError:
        # Already absent: retire this entry, never delete a later arrival.
        return True
    except OSError:
        return False


class WorkspaceExecutor:
    def __init__(self, workspace):
        self.workspace = workspace
        self._created = []
        self._audit = []
        self._applied = False
        self._rolled_back = False

    @staticmethod
    def _write_exclusive(fd, name, content):
        raw = content.encode("utf-8")
        dest = os.open(name, _FILE_FLAGS, 0o600, dir_fd=fd)
        inode = None
        written = 0
        try:
            inode = os.fstat(dest).st_ino
            view = memoryview(raw)
            while view:
                count = os.write(dest, view)
                if count <= 0:
                    raise OSError("short write")
                written += count
                view = view[count:]
            os.fsync(dest)
            os.fsync(fd)  # make the new directory entry durable too
            return inode
        except BaseException:
            # Cleanup goes through the SAME identity/content/link checks as
            # normal rollback. Never unlink directly after a write error.
            # If initial fstat failed, inode=None deliberately cannot match:
            # retain an unresolved entry rather than guess a file identity.
            raise _IncompleteWrite(
                inode, hashlib.sha256(raw[:written]).digest()) from None
        finally:
            os.close(dest)

    def _audit_entry(self, rootfd, kind, plan_sha, count):
        # Unique immutable evidence file, never append or overwrite user data.
        audit_name = ".ai-desktop-audit-" + secrets.token_hex(16) + ".json"
        audit = {"schema": 1, "event": kind, "plan_sha256": plan_sha,
                 "objects": count}
        content = json.dumps(audit, sort_keys=True) + "\n"
        # Reserve recovery ownership BEFORE any write; descriptor exhaustion
        # must not leave an audit file that cannot be tracked for cleanup.
        auditfd = os.dup(rootfd)
        try:
            self._write_exclusive(auditfd, audit_name, content)
        except _IncompleteWrite as exc:
            self._created.append((auditfd, audit_name, exc.inode, exc.digest))
            raise
        except BaseException:
            os.close(auditfd)
            raise
        else:
            os.close(auditfd)
        self._audit.append(audit_name)

    def _undo(self):
        pending = []
        for fd, name, inode, digest in reversed(self._created):
            def ours(info, content, inode=inode, digest=digest):
                return (info.st_ino == inode and
                        hashlib.sha256(content).digest() == digest)
            if _remove_verified(fd, name, ours):
                os.close(fd)
            else:
                pending.append((fd, name, inode, digest))
        # Preserve original creation order so retries still run in reverse order.
        self._created = list(reversed(pending))
        return not self._created

    def _mark(self, status):
        """Durably record the journal status after files reached it."""
        self._journal["status"] = status
        journal.write(self._root_fd, self._journal_name, self._journal)

    def apply(self, session, authorization):
        if self._applied or self._rolled_back:
            raise ExecutionDenied("EXECUTOR: REFUSED")
        self._applied = True
        try:
            if type(session) is not PlanSession:
                raise ExecutionDenied("EXECUTOR: REFUSED")
            approved = session.consume_for_executor(authorization)
            if approved.workspace != self.workspace or not approved.operations:
                raise ExecutionDenied("EXECUTOR: REFUSED")
            fd = _root(self.workspace)
            try:
                if _state(fd, approved.operations, self.workspace) != approved.state_sha256:
                    raise ExecutionDenied("EXECUTOR: REFUSED")
                self._plan_hash = approved.plan_sha256
                self._root_fd = fd
                try:
                    # An interrupted earlier apply must be recovered first.
                    for name in journal.list_names(fd):
                        if journal.read(fd, name)["status"] == "pending":
                            raise ExecutionDenied("EXECUTOR: REFUSED")
                    self._journal_name = journal.new_name()
                    self._journal = journal.new_document(
                        approved.plan_sha256, approved.operations)
                    journal.write(fd, self._journal_name, self._journal)
                    for index, (relative, content) in enumerate(approved.operations):
                        parent, name = _parent(fd, relative)
                        try:
                            inode = self._write_exclusive(parent, name, content)
                        except _IncompleteWrite as exc:
                            self._created.append((parent, name, exc.inode, exc.digest))
                            raise
                        except BaseException:
                            os.close(parent)
                            raise
                        self._created.append((
                            parent, name, inode,
                            hashlib.sha256(content.encode("utf-8")).digest()
                        ))
                        self._journal["entries"][index]["inode"] = inode
                        journal.write(fd, self._journal_name, self._journal)
                    self._audit_entry(fd, "applied", approved.plan_sha256,
                                      len(self._created))
                    self._mark("applied")
                except BaseException:
                    if not self._undo():
                        raise ExecutionDenied("EXECUTOR: ROLLBACK FAILED") from None
                    try:
                        self._mark("rolled_back")
                    except (journal.JournalError, AttributeError):
                        pass  # recover() will retire the now-absent entries
                    raise
                return {"status": "APPLIED", "count": len(self._created),
                        "plan_sha256": approved.plan_sha256}
            except BaseException:
                # An incomplete automatic rollback must remain recoverable.
                if not self._created:
                    os.close(fd)
                    if hasattr(self, "_root_fd"):
                        del self._root_fd
                raise
        except ExecutionDenied:
            raise
        except (OSError, ValueError, TypeError, PlanDenied, journal.JournalError):
            raise ExecutionDenied("EXECUTOR: REFUSED") from None

    def rollback(self):
        """Retryable in-memory rollback, file identity checked before each unlink."""
        if not self._applied or not hasattr(self, "_root_fd") or self._rolled_back:
            raise ExecutionDenied("EXECUTOR: REFUSED")
        try:
            if not self._undo():
                raise ExecutionDenied("EXECUTOR: ROLLBACK FAILED")
            self._audit_entry(self._root_fd, "rolled_back", self._plan_hash, 0)
            self._mark("rolled_back")
            self._rolled_back = True
            os.close(self._root_fd)
            del self._root_fd
            return {"status": "ROLLED_BACK", "plan_sha256": self._plan_hash}
        except (OSError, journal.JournalError):
            raise ExecutionDenied("EXECUTOR: REFUSED") from None

    def close(self):
        """Release descriptors and abandon in-memory retry; does not undo files."""
        if hasattr(self, "_root_fd"):
            os.close(self._root_fd)
            del self._root_fd
        for fd, _, _, _ in self._created:
            try:
                os.close(fd)
            except OSError:
                pass
        self._created.clear()



def _retire_entry(root_fd, entry):
    """Remove one journaled file only if it is verifiably the one we created."""
    wanted = entry["content"].encode("utf-8")
    inode = entry["inode"]

    def ours(info, content):
        # Without a journaled inode there is no proof of identity: a file that
        # appeared under this name may be the user's. Keep it (conflict) and
        # let the user decide; only an absent file is retired automatically.
        return (inode is not None and info.st_uid == os.geteuid() and
                info.st_ino == inode and content == wanted)

    try:
        parent, name = _parent(root_fd, entry["path"])
    except FileNotFoundError:
        return True  # directory gone, so is the file
    except (OSError, ExecutionDenied):
        return False
    try:
        return _remove_verified(parent, name, ours)
    finally:
        os.close(parent)


def recover(workspace, rollback_plans=()):
    """Retire files of interrupted (pending) applies after a restart.

    Completed applies are reversed only when their plan SHA-256 is listed in
    rollback_plans (explicit user choice). Anything that is not byte-for-byte what was created is kept and
    reported as a conflict; the journal then stays for a later retry.
    Returns counts only, never paths or contents.
    """
    root_fd = _root(workspace)
    result = {"status": "RECOVERED", "recovered": 0, "conflicts": 0, "invalid": 0}
    audit = WorkspaceExecutor(workspace)
    try:
        try:
            names = journal.list_names(root_fd)
        except journal.JournalError:
            raise ExecutionDenied("EXECUTOR: REFUSED") from None
        for name in names:
            try:
                doc = journal.read(root_fd, name)
            except journal.JournalError:
                result["invalid"] += 1
                continue
            if doc["status"] == "rolled_back" or (
                    doc["status"] == "applied" and
                    doc["plan_sha256"] not in rollback_plans):
                continue
            clean = True
            for entry in reversed(doc["entries"]):
                clean = _retire_entry(root_fd, entry) and clean
            try:
                if not clean:
                    raise ExecutionDenied("EXECUTOR: ROLLBACK FAILED")
                audit._audit_entry(root_fd, "recovered", doc["plan_sha256"], 0)
                doc["status"] = "rolled_back"
                journal.write(root_fd, name, doc)
            except (ExecutionDenied, OSError, journal.JournalError):
                result["conflicts"] += 1
            else:
                result["recovered"] += 1
        if result["conflicts"] or result["invalid"]:
            result["status"] = "INCOMPLETE"
        return result
    finally:
        audit.close()
        os.close(root_fd)


def status(workspace):
    """Journal summary for the CLI: plan hash and state only, no contents."""
    root_fd = _root(workspace)
    try:
        items = []
        for name in journal.list_names(root_fd):
            try:
                doc = journal.read(root_fd, name)
            except journal.JournalError:
                items.append({"plan_sha256": None, "status": "invalid"})
            else:
                items.append({"plan_sha256": doc["plan_sha256"],
                              "status": doc["status"], "files": len(doc["entries"])})
        return items
    except journal.JournalError:
        raise ExecutionDenied("EXECUTOR: REFUSED") from None
    finally:
        os.close(root_fd)
