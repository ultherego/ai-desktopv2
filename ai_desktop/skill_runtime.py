"""Shared on-demand, local Skills for every task mode; no native model tools.

Only assigned bundles are frozen before consent. Selecting an item cannot read a
new path or run a command. Every transfer rechecks the frozen bundle and role.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import unicodedata

from . import skills
from .e2_ipc import _unique_pairs
from .discovery_contract import CEILING_SKILL_BYTES

MAX_KNOWLEDGE_BYTES = CEILING_SKILL_BYTES
MAX_RESOURCES = 32
MAX_ROUNDS = 4
_active = ContextVar("ai_desktop_skill_registry", default=None)
_wire = ContextVar("ai_desktop_skill_knowledge", default=None)
_versions = ContextVar("ai_desktop_verified_versions", default=None)


def current_versions():
    return dict(_versions.get() or {})


def observe_command(step):
    from .skill_versions import trusted_version_observation
    if step.get("role") == "change" and step.get("status") not in ("declined", "not-allowed", "missing"):
        _versions.set({})
    found = trusted_version_observation(step.get("argv"), step)
    if found:
        _versions.set({**current_versions(), **found})


def version_scope(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _versions.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _versions.reset(token)
    return wrapped


def validate_knowledge(value):
    """Pure worker-side validation; no ambient settings or filesystem reads."""
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"resources"} or type(value["resources"]) is not list:
        raise ValueError("knowledge")
    if not 1 <= len(value["resources"]) <= MAX_RESOURCES:
        raise ValueError("knowledge")
    result, seen = [], set()
    for item in value["resources"]:
        if type(item) is not dict or set(item) != {"name", "status", "text", "sha256"}:
            raise ValueError("knowledge")
        name, status, text, digest = (item[key] for key in ("name", "status", "text", "sha256"))
        if (type(name) is not str or not skills.RESOURCE.fullmatch(name) or name in seen
                or status not in ("ok", "missing") or type(text) is not str
                or len(text.encode()) > skills.MAX_SKILL_BYTES
                or any(c not in "\n\t" and unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in text)
                or type(digest) is not str or digest != hashlib.sha256(text.encode()).hexdigest()
                or status == "missing" and text):
            raise ValueError("knowledge")
        seen.add(name)
        result.append(dict(item))
    document = {"resources": result}
    if len(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()) > MAX_KNOWLEDGE_BYTES:
        raise ValueError("knowledge budget")
    return document


def current_knowledge():
    return validate_knowledge(_wire.get())


@contextmanager
def using_knowledge(value):
    token = _wire.set(validate_knowledge(value))
    try:
        yield
    finally:
        _wire.reset(token)


class Registry:
    def __init__(self, names):
        self.names = tuple(names)
        self.bundles = {name: skills.bundle(name) for name in self.names}
        self.resources = []
        self.rounds = 0
        self.role_check = None

    def recheck(self):
        if self.role_check is not None:
            self.role_check()
        for name, before in self.bundles.items():
            if skills.bundle(name)["sha256"] != before["sha256"]:
                raise ValueError("Skill changed during task")

    def load(self, identifier):
        if type(identifier) is not str or not skills.RESOURCE.fullmatch(identifier):
            return None
        name = identifier.partition("/")[0]
        snapshot = self.bundles.get(name)
        return skills.resource_text(snapshot, identifier, versions=current_versions()) if snapshot is not None else None

    def accept(self, raw):
        """A closed selection message, intercepted before each mode's normal parser."""
        try:
            value = json.loads(raw, object_pairs_hook=_unique_pairs)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return False
        if type(value) is not dict or value.get("action") != "skill":
            return False
        names = value.get("names")
        if (set(value) != {"action", "names"} or type(names) is not list or not 1 <= len(names) <= 4
                or any(type(name) is not str or not skills.RESOURCE.fullmatch(name) for name in names)
                or len(set(names)) != len(names) or self.rounds >= MAX_ROUNDS):
            raise ValueError("Invalid Skill selection")
        seen = {item["name"] for item in self.resources}
        self.recheck()
        additions = []
        for name in names:
            text = self.load(name)
            if name in seen:
                previous = next(item for item in self.resources if item["name"] == name)
                if "/" not in name or previous["text"] == (text or ""):
                    raise ValueError("Repeated Skill selection")
            additions.append({"name": name, "status": "missing" if text is None else "ok", "text": text or "",
                              "sha256": hashlib.sha256((text or "").encode()).hexdigest()})
        document = validate_knowledge({"resources": [item for item in self.resources if item["name"] not in names] + additions})
        self.resources = document["resources"]
        self.rounds += 1
        _wire.set(document)
        return True


def accept_skill_response(raw):
    registry = _active.get()
    if registry is None:
        return False
    return registry.accept(raw)


def check_knowledge():
    registry = _active.get()
    if registry is not None:
        registry.recheck()


def has_catalog():
    registry = _active.get()
    return registry is not None and bool(registry.names)


@contextmanager
def task_scope():
    """Freeze only the role actually selected by the controller before its consent UI."""
    from . import agent_profiles, execution
    selected = execution.current()
    registry = None
    if selected is not None and agent_profiles.CATALOG_HEADER in selected["instructions"]:
        profile = agent_profiles.Store().load().config.profiles.get(selected["profile"])
        if profile is None or agent_profiles.catalog_for(profile) != selected["instructions"]:
            raise ValueError("Skill catalog changed")
        registry = Registry(profile.skills)
        def role_check():
            live = agent_profiles.Store().load().config.profiles.get(selected["profile"])
            if live is None or (live.skills, live.instructions, live.access) != (profile.skills, profile.instructions, profile.access):
                raise ValueError("Skill assignment changed")
        registry.role_check = role_check
    token = _active.set(registry)
    versions_token = _versions.set({})
    try:
        with using_knowledge(None):
            yield
    finally:
        _active.reset(token)
        _versions.reset(versions_token)


def task_skills(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with task_scope():
            return function(*args, **kwargs)
    return wrapped


def recheck_with(check):
    """Bind source and Skill snapshots to the last check before hand-off."""
    def recheck():
        check()
        check_knowledge()
    return recheck
