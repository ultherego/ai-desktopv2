import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import unicodedata
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent / "skills"
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
MAX_SKILL_BYTES = 96 * 1024           # one Skill plus its label and a role text always fits the combined ceiling
MAX_REFERENCE_BYTES = 16 * 1024
MAX_REFERENCES = 8
RESOURCE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)?\Z")


TEMPLATE = """---
name: {name}
description: Briefly: when the model should use this.
---

# {name}

Describe here, in your own words, what the model should know about your system or work.
A Skill is only an instruction: it gives no access to files or commands; permissions.toml decides those.
"""


def user_root() -> Path:
    """The owner's own Skills: never in the repository or the package, never shared."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config/ai-desktop/skills"


def locate(name: str) -> Path:
    """The owner's Skill of this name, otherwise the packaged general one."""
    root = user_root()
    return root if os.path.lexists(root / name / "SKILL.md") else ROOT


def names() -> tuple:
    found = set()
    for root in (ROOT, user_root()):
        if root.is_dir() and not root.is_symlink():
            found.update(entry.name for entry in root.iterdir() if entry.is_dir() or entry.is_symlink())
    return tuple(sorted(found))


def create(name: str) -> Path:
    """Start one private Skill from the template; an existing one is never overwritten."""
    if type(name) is not str or not SLUG.fullmatch(name):
        raise ValueError("Invalid Skill name (lowercase letters, digits and hyphens)")
    directory = user_root() / name
    os.makedirs(directory, mode=0o700, exist_ok=False)
    target = directory / "SKILL.md"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(TEMPLATE.format(name=name))
    return target


def parse_skill(data: str, name: str) -> tuple[str, str]:
    """Validate already bounded Skill text; no tools or file authority are loaded."""
    if not SLUG.fullmatch(name):
        raise ValueError("Invalid Skill name")
    lines = data.splitlines()

    if len(lines) < 5 or lines[0] != "---":
        raise ValueError("Missing frontmatter")

    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("Unclosed frontmatter") from exc

    fields = {}
    for line in lines[1:end]:
        if ":" not in line:
            raise ValueError("Invalid frontmatter field")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key in fields:
            raise ValueError("Repeated frontmatter field")
        fields[key] = value

    if fields.get("name") != name:
        raise ValueError("The name does not match the directory")

    desc = fields.get("description", "").strip("\"")
    if not desc or not "\n".join(lines[end + 1:]).strip():
        raise ValueError("Missing description or content")

    return desc, data


def inspect_skill(root: Path, name: str, *, secure=False) -> tuple[str, str]:
    if type(name) is not str or not SLUG.fullmatch(name):
        raise ValueError("Invalid Skill name")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        root_fd = os.open(root, flags)
    except OSError:
        raise ValueError("Skills directory missing, or a symlink was found") from None
    directory = descriptor = None
    try:
        directory = os.open(name, flags, dir_fd=root_fd)
        if secure and any(os.fstat(fd).st_uid not in (0, os.getuid()) or os.fstat(fd).st_mode & 0o022
                          for fd in (root_fd, directory)):
            raise ValueError("Unsafe Skill directory")
        descriptor = os.open("SKILL.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > MAX_SKILL_BYTES
                or (secure and (before.st_uid not in (0, os.getuid()) or before.st_mode & 0o022))):
            raise ValueError("Invalid SKILL.md file")
        raw = os.read(descriptor, MAX_SKILL_BYTES + 1)
        after = os.fstat(descriptor)
        linked = os.stat("SKILL.md", dir_fd=directory, follow_symlinks=False)
        fingerprint = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                                    info.st_gid, info.st_nlink, info.st_size,
                                    info.st_mtime_ns, info.st_ctime_ns)
        if (len(raw) > MAX_SKILL_BYTES or fingerprint(before) != fingerprint(after)
                or fingerprint(before) != fingerprint(linked)):
            raise ValueError("The Skill changed while being read")
        return parse_skill(raw.decode("utf-8"), name)
    except OSError:
        raise ValueError("SKILL.md missing, or a symlink was found") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
        os.close(root_fd)


def _reference_files(root, name, *, secure=True):
    """Read optional one-level references through descriptors, never following links."""
    if type(name) is not str or not SLUG.fullmatch(name):
        raise ValueError("Invalid Skill name")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors = []
    try:
        descriptors.append(os.open(root, flags))
        descriptors.append(os.open(name, flags, dir_fd=descriptors[-1]))
        try:
            descriptors.append(os.open("references", flags, dir_fd=descriptors[-1]))
        except FileNotFoundError:
            return {}
        if secure and any(os.fstat(fd).st_uid not in (0, os.getuid()) or os.fstat(fd).st_mode & 0o022
                          for fd in descriptors):
            raise ValueError("Unsafe reference directory")
        directory = descriptors[-1]
        files = sorted(os.listdir(directory))
        if len(files) > MAX_REFERENCES or any(not n.endswith(".md") or not SLUG.fullmatch(n[:-3]) for n in files):
            raise ValueError("Invalid reference catalog")
        result = {}
        for filename in files:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
            try:
                before = os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_REFERENCE_BYTES
                        or secure and (before.st_uid not in (0, os.getuid()) or before.st_mode & 0o022)):
                    raise ValueError("Unsafe reference file")
                raw = os.read(fd, MAX_REFERENCE_BYTES + 1)
                fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_nlink,
                                          s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                if (len(raw) > MAX_REFERENCE_BYTES or fingerprint(before) != fingerprint(os.fstat(fd))
                        or fingerprint(before) != fingerprint(os.stat(filename, dir_fd=directory, follow_symlinks=False))):
                    raise ValueError("Reference changed")
                result[filename[:-3]] = parse_reference(raw.decode("utf-8"), filename[:-3])
            finally:
                os.close(fd)
        if files != sorted(os.listdir(directory)):
            raise ValueError("Reference catalog changed")
        return result
    except OSError:
        raise ValueError("Unsafe reference path") from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def parse_reference(data, name):
    """Source provenance is data, never a claim that rolling documentation matches this host."""
    if type(data) is not str or len(data.encode()) > MAX_REFERENCE_BYTES:
        raise ValueError("Invalid reference")
    if any(c not in "\n\t" and unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in data):
        raise ValueError("Invalid reference")
    lines = data.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("Missing reference metadata")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError("Missing reference metadata") from None
    fields = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not separator or not value or key in fields or len(value.encode()) > 1024:
            raise ValueError("Invalid reference metadata")
        fields[key] = value
    if set(fields) != {"name", "description", "tools", "source", "section", "checked", "version"} or fields["name"] != name:
        raise ValueError("Invalid reference metadata")
    if not SLUG.fullmatch(name):
        raise ValueError("Invalid reference name")
    source = urlsplit(fields["source"])
    if source.scheme != "https" or not source.hostname or source.username or source.password or source.port not in (None, 443):
        raise ValueError("Invalid reference source")
    datetime.date.fromisoformat(fields["checked"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+:_-]{0,63}", fields["version"]):
        raise ValueError("Invalid reference version")
    tools = [tool.strip() for tool in fields["tools"].split(",")]
    if not 1 <= len(tools) <= 16 or any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9+_.-]{0,63}", tool) for tool in tools):
        raise ValueError("Invalid reference tools")
    content = "\n".join(lines[end + 1:]).strip()
    if not content:
        raise ValueError("Empty reference")
    return {**fields, "tools": tools, "content": content, "sha256": hashlib.sha256(data.encode()).hexdigest()}


def bundle(name, *, root=None):
    """One shared bounded snapshot for every CLI; the private override replaces the entire package."""
    root = locate(name) if root is None else root
    description, text = inspect_skill(root, name, secure=True)
    references = _reference_files(root, name)
    # Re-read the main file after the references so mixed revisions cannot pass unnoticed.
    if inspect_skill(root, name, secure=True) != (description, text):
        raise ValueError("Skill changed")
    fingerprint = hashlib.sha256(json.dumps({"text": text, "references": references}, sort_keys=True,
                                           ensure_ascii=False).encode()).hexdigest()
    return {"name": name, "description": description, "text": text, "references": references,
            "sha256": fingerprint}


def resource_text(snapshot, identifier, *, versions=None):
    """Select a frozen procedure/reference using only broker-provided version facts."""
    if type(identifier) is not str or not RESOURCE.fullmatch(identifier):
        return None
    skill, separator, reference = identifier.partition("/")
    if skill != snapshot["name"]:
        return None
    if not separator:
        return snapshot["text"]
    item = snapshot["references"].get(reference)
    if item is None:
        return None
    from .skill_versions import reference_compatibility
    versions = versions if type(versions) is dict else {}
    state = reference_compatibility(item, versions)
    observed = ", ".join(tool + " " + versions[tool] for tool in item["tools"]
                         if type(versions.get(tool)) is str) or "unknown"
    header = ("Reference: " + identifier + "\nSource: " + item["source"] + "\nSection: " + item["section"]
              + "\nChecked: " + item["checked"] + "\nDocument version: " + item["version"]
              + "\nPackaged reference SHA256: " + item["sha256"]
              + "\nTools: " + ", ".join(item["tools"])
              + "\nObserved package upstream versions: " + observed)
    if state in ("unknown", "mismatch"):
        alternatives = [snapshot["name"] + "/" + name for name, candidate in snapshot["references"].items()
                        if candidate["version"] == "rolling" and set(candidate["tools"]) & set(item["tools"])]
        return (header + "\nCompatibility: " + state + ". Version-specific content withheld. "
                "Obtain the installed package version through an allowed pacman -Q PACKAGE command in this task. "
                "Do not infer a version from user prose, a previous session or a document title."
                + ("\nGeneral references available: " + ", ".join(alternatives) if alternatives else ""))
    if state == "matched":
        compatibility = ("matched: the document version equals the upstream version observed by an allowed "
                         "package query in this task. Package patches and individual flags still need the installed help.")
    else:
        compatibility = ("rolling: not verified for the installed tool version. A rolling source is general guidance, "
                         "not an exact version match. Obtain version/capability evidence through allowed commands "
                         "before applying version-dependent flags.")
    return header + "\nCompatibility: " + compatibility + "\n\n" + item["content"]



def main(argv=None):
    parser = argparse.ArgumentParser(description="AI Desktop Skills")
    parser.add_argument("action", choices=["list", "validate", "show", "menu", "new"], nargs="?", default="list")
    parser.add_argument("name", nargs="?")
    args = parser.parse_args(argv)

    if args.action == "menu":
        if args.name is not None:
            parser.error("menu does not take a Skill name")
        from .management_cli import main as manage
        return manage(["skills"])

    if args.action == "new":
        if not args.name:
            parser.error("new requires a name")
        try:
            print("Created: " + str(create(args.name)))
            print("Assign it to an agent: ai-desktop agents")
        except FileExistsError:
            print("SKILL: FAIL: this Skill already exists")
            return 1
        except (ValueError, OSError) as exc:
            print(f"SKILL: FAIL: {exc}")
            return 1
        return 0

    if args.action == "show":
        if not args.name:
            parser.error("show requires a name")
        try:
            print(inspect_skill(locate(args.name), args.name)[1])
        except (ValueError, OSError) as exc:
            print(f"SKILL: FAIL: {exc}")
            return 1
        return 0

    if args.name is not None:
        parser.error("Unexpected Skill name")

    if not ROOT.is_dir() or ROOT.is_symlink():
        print("SKILLS: FAIL")
        return 1

    failed = False
    for name in names():
        root = locate(name)
        mark = " (your own)" if root != ROOT else ""
        try:
            inspected = bundle(name, root=root)
            desc = inspected["description"]
            print(f"{name}{mark}: {desc}")
        except (ValueError, OSError) as exc:
            print(f"{name}{mark}: FAIL: {exc}")
            failed = True

    if args.action == "validate":
        print("SKILLS: FAIL" if failed else "SKILLS: PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
