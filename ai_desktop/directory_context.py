"""Local discovery (list, find) under the captured question's file authority.

This module never opens a regular file. Results are observations, not read grants: every later content read still goes
through the normal file-policy boundary. `deny` wins first: a denied name is dropped before it is even examined, so it is
never stat-ed, counted as a match, named in a diagnostic or sent to the model. Paths in results are relative to the
configured scope root, in the same coordinates as read selectors, so a name is used exactly as returned.
"""
from collections import deque
from dataclasses import dataclass
import json
import os
import stat
import unicodedata

from . import file_context, permissions
from .assistant_contract import file_selections
from .discovery_contract import (KINDS, MAX_DIRECTORIES, MAX_QUERIES, clean_path, clean_query, validate_discovery,
                                 diagnostic_bytes, skill_bytes)

# Directory aliases are real symlinks. They are shown as `alias` entries (with their literal sibling target) only when the
# very same guarded rule that later lets a read pass through them would accept them; everything else is invisible.


@dataclass(frozen=True)
class DiscoverySnapshot:
    policy: object
    document: dict
    # Alias guards of every step so far; a directory can be empty or describe more files than a content read allows.
    guards: tuple
    contents: tuple = ()  # exact-file identities behind bounded content excerpts


class Scan:
    """Directory entries one question may still examine (shared by every list/find step)."""

    def __init__(self, left):
        self.left = left

    def take(self):
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def recheck_discovery(snapshot):
    """Check the policy, aliases and identities behind content excerpts before transfer."""
    from .ask_content import ContentGuard
    if (type(snapshot) is not DiscoverySnapshot or type(snapshot.guards) is not tuple
            or type(snapshot.contents) is not tuple
            or any(type(guard) is not ContentGuard for guard in snapshot.contents)
            or any(type(guard) is not permissions._AliasGuard for guard in snapshot.guards)):
        raise ValueError("files-selection")
    document = validate_discovery(snapshot.document)
    policy = snapshot.policy
    if policy is None:
        if snapshot.guards or snapshot.contents or any(step["op"] != "skill" for step in document["steps"]):
            raise ValueError("files-selection")
        limit = permissions.Limits().discovery_bytes
    elif type(policy) is permissions.Policy:
        limit = policy.limits().discovery_bytes
    else:
        raise ValueError("files-selection")
    if diagnostic_bytes(document) > limit:
        raise ValueError("discovery_bytes")
    from .skill_runtime import MAX_KNOWLEDGE_BYTES
    if skill_bytes(document) > MAX_KNOWLEDGE_BYTES:
        raise ValueError("skills_bytes")
    if policy is None:
        return
    policy.recheck()
    for guard in snapshot.guards:
        if (not permissions._enumerable(policy, guard.alias)
                or not permissions._enumerable(policy, guard.physical)):
            raise ValueError("files-denied")
        permissions._recheck_alias(policy, guard)
    content_keys = {(step["id"], step["path"], step["version"]) for step in document["steps"] if step["op"] == "content"}
    if content_keys != {(guard.ident, guard.relative, guard.version) for guard in snapshot.contents}:
        raise ValueError("files-selection")
    for guard in snapshot.contents:
        if guard.policy is not policy:
            raise ValueError("files-selection")
        guard.recheck()
    policy.recheck()


def _byte_size(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8"))


def resolve_roots(policy, selections):
    """Typed directory roots: (id, relative, scope, logical, physical, guards). Wildcards and file scopes are refused."""
    selections = file_selections(selections)
    if not 1 <= len(selections) <= MAX_DIRECTORIES:
        raise ValueError("files-selection")
    scopes = {"f" + str(scope.index): scope for scope in file_context._selected(policy, selections)}
    result = []
    for item in selections:
        ident, relative = (item, "") if type(item) is str else (item["id"], item["path"])
        scope = scopes[ident]
        if scope.mode == "exact" or any(char in relative for char in "*?[]{}"):
            raise ValueError("files-selection")
        clean_path(relative, empty=True)
        logical = scope.base if not relative else permissions._absolute(scope.base + "/" + relative)
        rule = policy._read[scope.index]
        if ((relative and not rule.matches(logical))
                or (scope.mode == "children" and relative)
                or not permissions._enumerable(policy, logical)):
            raise ValueError("files-denied")
        physical, guards = permissions._resolve_alias_path(policy, scope.base, logical, directory_scope=True, exact=False)
        if not permissions._enumerable(policy, physical):
            raise ValueError("files-denied")
        result.append((ident, relative, scope, logical, physical, tuple(guards)))
    return result


def _alias_target(policy, directory, name, info, physical, logical):
    """The literal sibling an alias entry points to, or None. Never raises.

    Order matters: the link text is read first, then BOTH spellings of the target are checked against deny/read
    rules, and only then is the target itself examined. A denied target is therefore never stat-ed or named.
    """
    try:
        if policy._version != 2 or info.st_uid != policy.uid or info.st_nlink != 1:
            return None
        permissions._alias_directory(os.fstat(directory), policy.uid)
        target = os.readlink(name, dir_fd=directory)
        if (type(target) is not str or not target or target in (".", "..") or "/" in target
                or len(target.encode("utf-8")) > 255):
            return None
        for base in (physical, logical):
            sibling = permissions._absolute(base + "/" + target)
            if policy._excluded(sibling) or not policy._allowed(sibling):
                return None
        permissions._alias_directory(os.stat(target, dir_fd=directory, follow_symlinks=False), policy.uid)
        return target
    except (OSError, ValueError):
        return None


def _join(parent, name):
    """parent is already a validated absolute path; only the new component needs checking (same rules as _absolute)."""
    if (not name or name in (".", "..") or "/" in name or len(parent.encode("utf-8")) + 1 + len(name.encode("utf-8")) > 512
            or any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in name)
            or any(char in name for char in "$~\\?[]{}*")):
        raise ValueError("files-path")
    return parent + "/" + name


def _children(policy, physical, logical, scope, scan, expect=None, match=None):
    """Sorted, visible children of one directory as (name, entry) pairs, plus whether the scan was cut short.

    Denied names are dropped without any metadata call. Special files, child links that are not valid aliases and
    files that could never pass the later content boundary are invisible.
    """
    cut = False
    identities = {}
    with permissions._open_directory(physical, policy.uid) as (directory, recheck):
        if expect is not None:
            opened = os.fstat(directory)
            if (opened.st_dev, opened.st_ino) != expect:
                raise ValueError("files-changed")      # not the directory the parent listing showed
        names, needs_stat, paths = [], {}, {}
        same = physical == logical
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                try:
                    actual = _join(physical, name)
                    alias = actual if same else _join(logical, name)
                except ValueError:
                    continue                        # a name the path rules cannot express is simply not offered
                # Deny wins first, for both spellings, before any count or metadata call.
                if policy._excluded(actual) or (not same and policy._excluded(alias)):
                    continue
                if not (policy._allowed(actual) and (same or policy._allowed(alias))):
                    continue
                if len(names) >= permissions.MAX_DIRECTORY_ENTRIES or not scan.take():
                    cut = True
                    break
                names.append(name)
                paths[name] = (actual, alias)
                try:
                    # d_type only (no stat): directories and links must be examined, other names only if they match.
                    needs_stat[name] = entry.is_dir(follow_symlinks=False) or entry.is_symlink()
                except OSError:
                    needs_stat[name] = True
        visible = []
        for name in sorted(names):
            actual, alias = paths[name]
            if match is not None and not needs_stat.get(name, True) and not match(name):
                continue                            # a non-matching plain file costs no metadata call at all
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            relative = alias[len(scope.base) + 1:]
            try:
                if stat.S_ISDIR(info.st_mode):
                    if not (permissions._enumerable(policy, actual) and permissions._enumerable(policy, alias)):
                        continue                    # e.g. a subdirectory of a non-recursive `dir/*` scope is not browsable
                    entry = {"path": clean_path(relative), "type": "directory", "size": None}
                    identities[name] = (info.st_dev, info.st_ino)
                elif stat.S_ISREG(info.st_mode):
                    if (not permissions.trusted_owner(info, policy.uid, system=policy._version == 2)
                            or info.st_nlink != 1 or info.st_mode & 0o6000):
                        continue
                    entry = {"path": clean_path(relative), "type": "file", "size": info.st_size}
                elif stat.S_ISLNK(info.st_mode):
                    target = _alias_target(policy, directory, name, info, physical, logical)
                    if target is None or not permissions._enumerable(policy, alias):
                        continue
                    base = relative.rsplit("/", 1)[0] + "/" if "/" in relative else ""
                    entry = {"path": clean_path(relative), "type": "alias", "size": None,
                             "target": clean_path(base + target)}
                else:
                    continue
            except ValueError:
                continue                  # a name the plan contract could not express is simply not offered
            visible.append((name, entry))
        recheck()
    return visible, cut, identities


def list_step(policy, selections, after, limits, scan, byte_budget, permit):
    """List the immediate children of up to four directories. `after` pages a single directory by entry path."""
    roots = resolve_roots(policy, selections)
    if after is not None:
        clean_path(after)
        if len(roots) != 1:
            raise ValueError("files-selection")
    step = {"op": "list", "directories": [], "truncated": False}
    guards, emitted, used = [], 0, 0
    for ident, relative, scope, logical, physical, root_guards in roots:
        guards.extend(root_guards)
        permit.check()
        out = {"id": ident, "path": relative, "entries": [], "truncated": False, "next": None}
        step["directories"].append(out)
        children, cut, _ = _children(policy, physical, logical, scope, scan)
        if after is not None:
            prefix = (relative + "/") if relative else ""
            if not after.startswith(prefix) or "/" in after[len(prefix):]:
                raise ValueError("files-selection")
            children = [(name, entry) for name, entry in children if name > after[len(prefix):]]
        for position, (name, entry) in enumerate(children):
            cost = _byte_size(entry) + 1
            if emitted >= limits.listing_entries or used + cost > byte_budget:
                out["truncated"] = True
                out["next"] = out["entries"][-1]["path"] if out["entries"] else None
                break
            out["entries"].append(entry)
            emitted += 1
            used += cost
        if cut:
            # The scan stopped early, so the names seen are not a prefix of the true name order: paging from them
            # would silently skip entries. Report the cut honestly and offer no continuation point.
            out["truncated"] = True
            out["next"] = None
        if out["truncated"]:
            step["truncated"] = True
    return step, tuple(guards)


def find_step(policy, selection, queries, kind, limits, scan, byte_budget, permit):
    """Breadth-first search by name below ONE directory root; matches are metadata only, content is never read."""
    if (type(queries) is not list or not 1 <= len(queries) <= MAX_QUERIES or type(kind) is not str or kind not in KINDS):
        raise ValueError("files-selection")
    queries = list({item.casefold(): item for item in reversed([clean_query(i) for i in queries])}.values())[::-1]
    folded = [item.casefold() for item in queries]
    (ident, relative, scope, logical, physical, root_guards), = resolve_roots(policy, [selection])
    step = {"op": "find", "id": ident, "path": relative, "queries": queries, "kind": kind, "matches": [],
            "truncated": False, "incomplete": False}
    used, pending, seen = 0, deque([(physical, logical, 0, None)]), 0
    while pending:
        permit.check()
        here_physical, here_logical, depth, expect = pending.popleft()
        if (policy._excluded(here_physical) or policy._excluded(here_logical)
                or not permissions._enumerable(policy, here_physical)):
            continue
        try:
            children, cut, identities = _children(policy, here_physical, here_logical, scope, scan, expect,
                                                        match=lambda text: any(query in text.casefold() for query in folded))
        except ValueError as error:
            # A swapped or re-linked subdirectory (different inode than the parent listing showed, or now a link) is
            # a security signal and stops the question with a NON-recoverable code; only a vanished or unreadable
            # one is reported as an incomplete search.
            if depth == 0:
                raise
            if error.args in (("files-changed",), ("files-symlink",)):
                raise ValueError("files-changed") from None
            step["incomplete"] = True      # a subdirectory that vanished or turned unreadable is reported, not hidden
            continue
        if cut:
            step["incomplete"] = True
        for name, entry in children:
            seen += 1
            if entry["type"] == "directory" and depth < permissions.MAX_DEPTH:
                pending.append((permissions._absolute(here_physical + "/" + name),
                                permissions._absolute(here_logical + "/" + name), depth + 1, identities.get(name)))
            elif entry["type"] == "directory":
                step["incomplete"] = True
            if kind != "any" and entry["type"] != kind and not (kind == "directory" and entry["type"] == "alias"):
                continue
            if not any(query in name.casefold() for query in folded):
                continue
            cost = _byte_size(entry) + 1
            if len(step["matches"]) >= limits.search_results or used + cost > byte_budget:
                step["truncated"] = True
                pending.clear()
                break
            step["matches"].append(entry)
            used += cost
        if scan.left <= 0 and pending:
            step["incomplete"] = True
            break
    if step["incomplete"]:
        step["truncated"] = True
    return step, tuple(root_guards)
