"""Private, bounded agent definitions; never an authority for tools or OAuth.

Cooperating writers use one flock and compare the complete file snapshot before
publication. A hostile process of the same UID can still race a replacement;
the existing AI Desktop same-UID trust boundary applies to this configuration.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
from types import MappingProxyType
import unicodedata

from .fs_publish import rename_noreplace
from .execution import CLAUDE_EFFORTS, CODEX_EFFORTS, EFFORTS, MODELS, efforts_for, model_allowed

MAX_BYTES = 65536
MAX_PROFILES = 24
MAX_INSTRUCTIONS = 4096
# The whole system prompt is one argv string for the Claude client, and Linux caps one string at 128 KiB (MAX_ARG_STRLEN).
# This is what remains after the application's own prompt, with a margin. tests/test_user_skills.py guards the sum.
MAX_EXECUTION_INSTRUCTIONS = 104 * 1024
ACCESSES = ("read-only", "changes")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_PROFILE_KEYS = frozenset(("provider", "model", "effort", "instructions", "access", "skills"))
PRESET_SKILLS = MappingProxyType({
    "default": "concise-assistant",
    "diagnostic": "cachyos-diagnostics",
    "programmer": "code-development",
    "reviewer": "code-review",
    "analyst": "project-analysis",
})


class ProfileError(ValueError):
    """A finite local error; never includes saved instructions or secret data."""


def slug(value):
    if type(value) is not str or not 1 <= len(value) <= 48 or not SLUG.fullmatch(value):
        raise ProfileError("name")
    return value


def _text(value, limit):
    try:
        valid = (type(value) is str and len(value.encode("utf-8")) <= limit
                 and not any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp")
                             and c not in "\n\t" for c in value))
    except UnicodeError:
        valid = False
    if not valid:
        raise ProfileError("instructions")
    return value


@dataclass(frozen=True)
class Profile:
    provider: str = "claude"
    model: str = "default"
    effort: str = "default"
    instructions: str = ""
    access: str = "changes"
    skills: tuple = ()

    def __post_init__(self):
        if (type(self.provider) is not str or self.provider not in ("claude", "codex")
                or type(self.model) is not str
                or not model_allowed(self.provider, self.model)
                or type(self.effort) is not str or self.effort not in efforts_for(self.provider)
                or type(self.access) is not str or self.access not in ACCESSES
                or type(self.skills) is not tuple):
            raise ProfileError("profile")
        _text(self.instructions, MAX_INSTRUCTIONS)
        for name in self.skills:
            slug(name)
        if len(set(self.skills)) != len(self.skills):
            raise ProfileError("profile")

    def data(self):
        return {"provider": self.provider, "model": self.model, "effort": self.effort,
                "instructions": self.instructions, "access": self.access, "skills": list(self.skills)}


def _profile(value):
    if type(value) is not dict or set(value) != _PROFILE_KEYS or type(value["skills"]) is not list:
        raise ProfileError("profile")
    return Profile(**{**value, "skills": tuple(value["skills"])})


@dataclass(frozen=True)
class Config:
    active: str
    entries: tuple
    # None preserves the schema-1, per-profile choice until the owner explicitly
    # chooses a service. Thereafter all roles use one service, while the other
    # service's model/effort choices remain private local configuration only.
    service: str | None = None
    provider_settings: tuple = ()

    def __post_init__(self):
        slug(self.active)
        if type(self.entries) is not tuple or not 1 <= len(self.entries) <= MAX_PROFILES:
            raise ProfileError("config")
        names = []
        for item in self.entries:
            if type(item) is not tuple or len(item) != 2 or type(item[1]) is not Profile:
                raise ProfileError("config")
            names.append(slug(item[0]))
        if len(set(names)) != len(names) or "default" not in names or self.active not in names:
            raise ProfileError("config")
        if self.service is not None and (type(self.service) is not str
                                         or self.service not in ("claude", "codex")):
            raise ProfileError("config")
        if (type(self.provider_settings) is not tuple or len(self.provider_settings) > MAX_PROFILES
                or (self.service is None and self.provider_settings)):
            raise ProfileError("config")
        if self.service is not None and any(profile.provider != self.service for _, profile in self.entries):
            raise ProfileError("config")
        saved = set()
        for item in self.provider_settings:
            if type(item) is not tuple or len(item) != 4:
                raise ProfileError("config")
            name, provider, model, effort = item
            slug(name)
            Profile(provider=provider, model=model, effort=effort)
            if name not in names or provider == self.service or (name, provider) in saved:
                raise ProfileError("config")
            saved.add((name, provider))

    @property
    def profiles(self):
        return MappingProxyType(dict(self.entries))

    @property
    def selected_provider(self):
        return self.service or self.profiles[self.active].provider

    def with_provider(self, provider):
        """Explicitly choose one service for every role, retaining both clients' settings.

        This operation never reads account state or modifies network permissions.
        Callers publish it through Store.mutate with their displayed snapshot.
        """
        if type(provider) is not str or provider not in ("claude", "codex"):
            raise ProfileError("provider")
        if self.service == provider:
            return self
        saved = {(name, client): (model, effort)
                 for name, client, model, effort in self.provider_settings}
        entries = []
        for name, selected in self.entries:
            if selected.provider != provider:
                saved[(name, selected.provider)] = (selected.model, selected.effort)
                model, effort = saved.pop((name, provider), ("default", "default"))
                selected = replace(selected, provider=provider, model=model, effort=effort)
            entries.append((name, selected))
        cached = tuple(sorted((name, client, model, effort)
                              for (name, client), (model, effort) in saved.items()))
        return Config(self.active, tuple(entries), provider, cached)

    def put(self, name, profile):
        slug(name)
        if type(profile) is not Profile or (self.service is not None and profile.provider != self.service):
            raise ProfileError("provider")
        values = dict(self.entries)
        values[name] = profile
        return replace(self, entries=tuple(sorted(values.items())))

    def select(self, name):
        slug(name)
        if name not in self.profiles:
            raise ProfileError("missing")
        return replace(self, active=name)

    def copy(self, source, name):
        slug(source)
        slug(name)
        if source not in self.profiles:
            raise ProfileError("missing")
        if name in self.profiles:
            raise ProfileError("exists")
        copied = self.put(name, self.profiles[source])
        saved = self.provider_settings + tuple((name,) + item[1:]
                    for item in self.provider_settings if item[0] == source)
        return replace(copied, provider_settings=tuple(sorted(saved)))

    def remove(self, name):
        if name not in self.profiles:
            raise ProfileError("missing")
        if name in ("default", self.active):
            raise ProfileError("remove")
        return replace(self, entries=tuple(item for item in self.entries if item[0] != name),
                       provider_settings=tuple(item for item in self.provider_settings if item[0] != name))

    def data(self):
        value = {"schema": 1, "active": self.active,
                 "profiles": {name: profile.data() for name, profile in self.entries}}
        if self.service is not None:
            cached = {}
            for name, provider, model, effort in self.provider_settings:
                cached.setdefault(name, {})[provider] = {"model": model, "effort": effort}
            value.update(schema=2, service=self.service, provider_settings=cached)
        return value


def defaults():
    return Config("default", (
        ("default", Profile()),
        ("diagnostic", Profile(access="read-only", instructions="Diagnozuj na podstawie dostępnych dowodów. Opisz przyczynę i niepewność.")),
        ("programmer", Profile(instructions="Przygotuj poprawną zmianę kodu oraz odpowiednie sprawdzenie. Nie twierdź, że zapis lub test już wykonano.")),
        ("reviewer", Profile(access="read-only", instructions="Przejrzyj kod. Podaj konkretne błędy, ich skutki i miejsca występowania.")),
    ))


def prepared(config):
    """Build explicitly requested presets without replacing user configuration.

    Loading defaults or installing a package never calls this function. Missing
    stock profiles are added and only empty Skill selections are filled. The
    complete candidate is checked before the caller publishes it with Store's
    usual snapshot transaction.
    """
    if type(config) is not Config:
        raise ProfileError("config")
    stock = dict(defaults().entries)
    stock["analyst"] = Profile(access="read-only", instructions=(
        "Analizuj projekt na podstawie udostępnionych plików. Oddziel dowody, wnioski i braki danych."))
    entries = dict(config.entries)
    for name, template in stock.items():
        if config.service is not None:
            template = replace(template, provider=config.service)
        selected = entries.get(name, template)
        if not selected.skills:
            selected = replace(selected, skills=(PRESET_SKILLS[name],))
        entries[name] = selected
    candidate = replace(config, entries=tuple(sorted(entries.items())))
    for _, selected in candidate.entries:
        instructions_for(selected)
    return candidate


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError("duplicate")
        result[key] = value
    return result


def _decode(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
        if (type(value) is not dict or type(value.get("schema")) is not int
                or value["schema"] not in (1, 2) or type(value.get("profiles")) is not dict):
            raise ProfileError("config")
        expected = {"schema", "active", "profiles"}
        if value["schema"] == 2:
            expected.update(("service", "provider_settings"))
        if set(value) != expected:
            raise ProfileError("config")
        saved = []
        if value["schema"] == 2:
            if type(value["provider_settings"]) is not dict:
                raise ProfileError("config")
            for name, providers in value["provider_settings"].items():
                if type(providers) is not dict or not providers:
                    raise ProfileError("config")
                for provider, settings in providers.items():
                    if type(settings) is not dict or set(settings) != {"model", "effort"}:
                        raise ProfileError("config")
                    saved.append((name, provider, settings["model"], settings["effort"]))
            if value["service"] is None:
                raise ProfileError("config")
        return Config(value["active"], tuple(sorted((name, _profile(profile))
                      for name, profile in value["profiles"].items())),
                      value.get("service"), tuple(sorted(saved)))
    except (UnicodeError, TypeError, KeyError, ValueError, RecursionError):
        raise ProfileError("config") from None


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory(info, uid):
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid) or info.st_mode & 0o022:
        raise ProfileError("directory")


def _file(info, uid):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ProfileError("file")


@dataclass(frozen=True)
class Snapshot:
    config: Config
    identity: tuple | None
    sha256: str | None


class Store:
    def __init__(self, home=None):
        self.uid = os.getuid()
        if self.uid != os.geteuid() or os.getgid() != os.getegid():
            raise ProfileError("identity")
        self.home = Path(home if home is not None else pwd.getpwuid(self.uid).pw_dir)
        if not self.home.is_absolute() or any(part in (".", "..") for part in self.home.parts):
            raise ProfileError("home")
        self.path = self.home / ".config" / "ai-desktop" / "agents.json"

    @contextmanager
    def _parent(self, create=False):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptors, links = [], []
        try:
            root = os.open("/", flags)
            descriptors.append(root)
            _directory(os.fstat(root), self.uid)
            current = root
            parts = self.path.parent.parts[1:]
            home_parts = len(self.home.parts) - 1
            missing = False
            for number, name in enumerate(parts, 1):
                try:
                    child = os.open(name, flags, dir_fd=current)
                except FileNotFoundError:
                    if not create or number <= home_parts:
                        missing = True
                        break
                    try:
                        os.mkdir(name, 0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    child = os.open(name, flags, dir_fd=current)
                descriptors.append(child)
                info = os.fstat(child)
                _directory(info, self.uid)
                if number >= home_parts and info.st_uid != self.uid:
                    raise ProfileError("owner")
                links.append((current, name, child, (info.st_dev, info.st_ino, info.st_uid,
                                                    info.st_gid, info.st_mode)))
                current = child

            def recheck():
                for parent, name, child, expected in links:
                    for info in (os.fstat(child), os.stat(name, dir_fd=parent, follow_symlinks=False)):
                        _directory(info, self.uid)
                        if (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode) != expected:
                            raise ProfileError("changed")
            recheck()
            yield (None if missing else current), recheck
            recheck()
        except OSError:
            raise ProfileError("access") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _read(self, parent):
        if parent is None:
            return Snapshot(defaults(), None, None)
        try:
            descriptor = os.open("agents.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=parent)
        except FileNotFoundError:
            return Snapshot(defaults(), None, None)
        try:
            before = os.fstat(descriptor)
            _file(before, self.uid)
            if before.st_size > MAX_BYTES:
                raise ProfileError("size")
            raw = bytearray()
            while len(raw) <= MAX_BYTES:
                part = os.read(descriptor, min(8192, MAX_BYTES + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            if len(raw) > MAX_BYTES:
                raise ProfileError("size")
            token = _fingerprint(before)
            if token != _fingerprint(os.fstat(descriptor)) or token != _fingerprint(
                    os.stat("agents.json", dir_fd=parent, follow_symlinks=False)):
                raise ProfileError("changed")
            return Snapshot(_decode(bytes(raw)), token, hashlib.sha256(raw).hexdigest())
        finally:
            os.close(descriptor)

    def load(self):
        try:
            with self._parent() as (parent, _):
                return self._read(parent)
        except OSError:
            raise ProfileError("access") from None

    def mutate(self, operation, *, expected=None):
        """Atomic cooperating-writer transaction; an optional snapshot detects stale UI edits."""
        temporary = None
        try:
            with self._parent(create=True) as (parent, recheck):
                lock = os.open(".agents.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
                               | os.O_NONBLOCK, 0o600, dir_fd=parent)
                try:
                    _file(os.fstat(lock), self.uid)
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if _fingerprint(os.fstat(lock)) != _fingerprint(os.stat(
                            ".agents.lock", dir_fd=parent, follow_symlinks=False)):
                        raise ProfileError("changed")
                    before = self._read(parent)
                    if expected is not None and before != expected:
                        raise ProfileError("changed")
                    config = operation(before.config)
                    if type(config) is not Config:
                        raise ProfileError("config")
                    raw = (json.dumps(config.data(), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
                    if len(raw) > MAX_BYTES:
                        raise ProfileError("size")
                    temporary = ".agents-" + secrets.token_hex(16) + ".tmp"
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                                         | os.O_CLOEXEC, 0o600, dir_fd=parent)
                    try:
                        offset = 0
                        while offset < len(raw):
                            count = os.write(descriptor, raw[offset:])
                            if count <= 0:
                                raise ProfileError("access")
                            offset += count
                        os.fsync(descriptor)
                        if _fingerprint(os.fstat(descriptor)) != _fingerprint(os.stat(
                                temporary, dir_fd=parent, follow_symlinks=False)):
                            raise ProfileError("changed")
                    finally:
                        os.close(descriptor)
                    recheck()
                    if self._read(parent) != before:
                        raise ProfileError("changed")
                    if before.identity is None:
                        rename_noreplace(parent, temporary, parent, "agents.json")
                    else:
                        os.replace(temporary, "agents.json", src_dir_fd=parent, dst_dir_fd=parent)
                    temporary = None
                    os.fsync(parent)
                    return self._read(parent)
                finally:
                    if temporary is not None:
                        try:
                            os.unlink(temporary, dir_fd=parent)
                        except FileNotFoundError:
                            pass
                    os.close(lock)
        except OSError:
            raise ProfileError("access") from None


def _skill(name):
    """Read bounded installed instructions. A Skill never grants extra tools."""
    from . import skills
    slug(name)
    try:
        _, data = skills.inspect_skill(skills.locate(name), name, secure=True)
        return _text(data, MAX_EXECUTION_INSTRUCTIONS)
    except (OSError, ValueError, UnicodeError):
        raise ProfileError("skill") from None


def instructions_for(selected):
    """Materialize the bounded role plus Skill text without changing permissions."""
    if type(selected) is not Profile:
        raise ProfileError("profile")
    parts = [selected.instructions] if selected.instructions else []
    for skill in selected.skills:
        parts.append("Skill " + skill + ":\n" + _skill(skill))
    return _text("\n\n".join(parts), MAX_EXECUTION_INSTRUCTIONS)


def resolve(profile=None, model=None, effort=None, *, store=None):
    """Freeze the selected role and installed Skill bytes before a task starts."""
    config = (store or Store()).load().config
    # A role override selects instructions/access, never a different recipient.
    # Schema-1 files keep their original bytes until an explicit write; this
    # transient view uses their currently active service for every role too.
    config = config.with_provider(config.selected_provider)
    name = config.active if profile is None else slug(profile)
    if name not in config.profiles:
        raise ProfileError("missing")
    selected = config.profiles[name]
    if model is not None or effort is not None:
        selected = replace(selected, **({"model": model} if model is not None else {}),
                           **({"effort": effort} if effort is not None else {}))
    instructions = instructions_for(selected)
    return {"provider": selected.provider, "model": selected.model, "effort": selected.effort,
            "instructions": instructions, "profile": name, "access": selected.access}
