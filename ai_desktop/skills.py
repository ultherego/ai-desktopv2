import argparse
import os
from pathlib import Path
import pwd
import re
import stat

ROOT = Path(__file__).resolve().parent.parent / "skills"
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
MAX_SKILL_BYTES = 96 * 1024           # one Skill plus its label and a role text always fits the combined ceiling


TEMPLATE = """---
name: {name}
description: Krótko: kiedy model ma z tego korzystać.
---

# {name}

Opisz tu własnymi słowami to, co model ma wiedzieć o Twoim systemie lub pracy.
Skill to tylko instrukcja: nie daje dostępu do plików ani poleceń, o nich decyduje permissions.toml.
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
        raise ValueError("Niepoprawna nazwa skilla (małe litery, cyfry i myślniki)")
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
        raise ValueError("Niepoprawna nazwa skilla")
    lines = data.splitlines()

    if len(lines) < 5 or lines[0] != "---":
        raise ValueError("Brak frontmatter")

    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("Niezamkniety frontmatter") from exc

    fields = {}
    for line in lines[1:end]:
        if ":" not in line:
            raise ValueError("Niepoprawne pole frontmatter")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key in fields:
            raise ValueError("Powtorzone pole frontmatter")
        fields[key] = value

    if fields.get("name") != name:
        raise ValueError("Nazwa nie odpowiada katalogowi")

    desc = fields.get("description", "").strip("\"")
    if not desc or not "\n".join(lines[end + 1:]).strip():
        raise ValueError("Brak opisu lub tresci")

    return desc, data


def inspect_skill(root: Path, name: str, *, secure=False) -> tuple[str, str]:
    if type(name) is not str or not SLUG.fullmatch(name):
        raise ValueError("Niepoprawna nazwa skilla")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        root_fd = os.open(root, flags)
    except OSError:
        raise ValueError("Brak katalogu Skills lub wykryto symlink") from None
    directory = descriptor = None
    try:
        directory = os.open(name, flags, dir_fd=root_fd)
        if secure and any(os.fstat(fd).st_uid not in (0, os.getuid()) or os.fstat(fd).st_mode & 0o022
                          for fd in (root_fd, directory)):
            raise ValueError("Niebezpieczny katalog Skill")
        descriptor = os.open("SKILL.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > MAX_SKILL_BYTES
                or (secure and (before.st_uid not in (0, os.getuid()) or before.st_mode & 0o022))):
            raise ValueError("Niepoprawny plik SKILL.md")
        raw = os.read(descriptor, MAX_SKILL_BYTES + 1)
        after = os.fstat(descriptor)
        linked = os.stat("SKILL.md", dir_fd=directory, follow_symlinks=False)
        fingerprint = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                                    info.st_gid, info.st_nlink, info.st_size,
                                    info.st_mtime_ns, info.st_ctime_ns)
        if (len(raw) > MAX_SKILL_BYTES or fingerprint(before) != fingerprint(after)
                or fingerprint(before) != fingerprint(linked)):
            raise ValueError("Skill zmienił się podczas odczytu")
        return parse_skill(raw.decode("utf-8"), name)
    except OSError:
        raise ValueError("Brak SKILL.md lub wykryto symlink") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
        os.close(root_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI Desktop Skills")
    parser.add_argument("action", choices=["list", "validate", "show", "menu", "new"], nargs="?", default="list")
    parser.add_argument("name", nargs="?")
    args = parser.parse_args(argv)

    if args.action == "menu":
        if args.name is not None:
            parser.error("menu nie przyjmuje nazwy skilla")
        from .management_cli import main as manage
        return manage(["skills"])

    if args.action == "new":
        if not args.name:
            parser.error("new wymaga nazwy")
        try:
            print("Utworzono: " + str(create(args.name)))
            print("Przypisz go agentowi: ai-desktop agents")
        except FileExistsError:
            print("SKILL: FAIL: taki Skill już istnieje")
            return 1
        except (ValueError, OSError) as exc:
            print(f"SKILL: FAIL: {exc}")
            return 1
        return 0

    if args.action == "show":
        if not args.name:
            parser.error("show wymaga nazwy")
        try:
            print(inspect_skill(locate(args.name), args.name)[1])
        except (ValueError, OSError) as exc:
            print(f"SKILL: FAIL: {exc}")
            return 1
        return 0

    if args.name is not None:
        parser.error("Nieoczekiwana nazwa skilla")

    if not ROOT.is_dir() or ROOT.is_symlink():
        print("SKILLS: FAIL")
        return 1

    failed = False
    for name in names():
        root = locate(name)
        mark = " (własny)" if root != ROOT else ""
        try:
            desc, _ = inspect_skill(root, name)
            print(f"{name}{mark}: {desc}")
        except (ValueError, OSError) as exc:
            print(f"{name}{mark}: FAIL: {exc}")
            failed = True

    if args.action == "validate":
        print("SKILLS: FAIL" if failed else "SKILLS: PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
