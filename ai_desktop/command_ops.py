"""Operations the assistant may run for you - only the commands YOU list in permissions.toml, only after YOU say yes.

The owner's `[commands] allow` is a list of argv *templates*. A model proposal is an argv list (never a shell string);
it is matched token by token against the templates, resolved to an absolute, root-owned program, shown to the user
verbatim, and run only after a fresh consent for that exact command. It runs without a shell, with a clean environment,
no stdin, a time limit and an output limit, inside a read-only, network-less `bwrap` view of the system. The result is
sanitised, bounded data that goes back to the model; it is never authority for anything.

Nothing about a particular program is built in: the only code-level refusals are interpreters, shells, privilege
escalation, network clients and obvious writers as the FIRST word of a template, because those turn "run this command"
into "run anything".
"""
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time
import unicodedata

MAX_TEMPLATES = 128
MAX_TOKENS = 10
MAX_TOKEN_BYTES = 100
MAX_TEMPLATE_BYTES = 120
SEARCH_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
STDERR_BYTES = 2048
FORBIDDEN_PROGRAMS = frozenset((
    "sh", "bash", "dash", "zsh", "fish", "ksh", "csh", "tcsh", "ash", "busybox", "env", "xargs", "find", "nice", "nohup",
    "timeout", "watch", "script", "flock", "stdbuf", "setsid", "chroot", "unshare", "nsenter", "bwrap", "strace", "ltrace",
    "gdb", "python", "python3", "perl", "ruby", "node", "nodejs", "php", "lua", "tclsh", "awk", "gawk", "mawk", "sed",
    "sudo", "su", "doas", "pkexec", "run0", "machinectl", "ssh", "scp", "sftp", "nc", "ncat", "netcat", "socat", "telnet",
    "curl", "wget", "ftp", "rsync", "rm", "rmdir", "mv", "cp", "dd", "tee", "chmod", "chown", "chgrp", "ln", "truncate",
    "shred", "mount", "umount", "kill", "pkill", "killall", "reboot", "poweroff", "shutdown", "halt", "install",
    "systemd-run", "systemd-nspawn", "systemd-cat", "busctl", "loginctl", "dbus-send", "gdbus", "runuser", "login",
    "ionice", "taskset", "chrt", "setpriv", "capsh", "time", "git", "less", "more", "man", "tar", "vi", "vim", "nano", "ed",
    "ex", "view", "emacs", "pager", "ld", "ld.so", "ld-linux", "exec", "eval", "command", "builtin", "source",
))
_PROGRAM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
_LITERAL = re.compile(r"[A-Za-z0-9._+=:,@/%-]{1,100}\Z")
_PLACEHOLDERS = {
    "<unit>": re.compile(r"[A-Za-z0-9:_@.][A-Za-z0-9:_.@-]{0,99}\.(?:service|socket|timer|target|mount|path|slice|scope|device|swap)\Z"),
    "<int>": re.compile(r"(?:[1-9][0-9]{0,4}|0)\Z"),
    "<word>": re.compile(r"[A-Za-z0-9._:][A-Za-z0-9._:-]{0,63}\Z"),
}
# Tools whose first non-option word is a SUBCOMMAND: a placeholder must never stand there, or the model could pick
# `restart` for `systemctl --user <word> x`.
SUBCOMMAND_TOOLS = frozenset(("systemctl", "docker", "nmcli", "btrfs", "snapper", "bluetoothctl", "resolvectl",
                              "powerprofilesctl", "pactl", "wpctl", "lact", "networkctl", "timedatectl", "hostnamectl",
                              "localectl", "journalctl"))
# Options of those tools that never take a value. Short forms mean different things across tools (`-f` forces in
# systemctl and names fields in nmcli), so only the unambiguous ones are listed.
_STANDALONE_OPTIONS = frozenset(("--user", "--system", "--global", "--runtime", "--no-pager", "--no-legend",
                                 "--no-ask-password", "--no-block", "--no-reload", "--no-wall", "--now", "--all", "--full",
                                 "--quiet", "-q", "--force", "--failed", "--plain"))
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")


def _require_literal_verb(tokens):
    """No placeholder may stand where the verb could be. After an option not known to stand alone, the next word may be
    its value (`systemctl -H host <word>`), so the verb is looked for one word further on."""
    maybe_value = False
    for token in tokens[1:]:
        if token in _PLACEHOLDERS:
            raise ValueError("command-template")
        if token.startswith("-"):
            maybe_value = "=" not in token and token not in _STANDALONE_OPTIONS
        elif maybe_value:
            maybe_value = False
        else:
            return


def parse_template(text, *, strict=False):
    """A validated template as a tuple of tokens, or ValueError. Called while loading the owner's policy."""
    if type(text) is not str or not text.strip() or len(text.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise ValueError("command-template")
    tokens = tuple(text.split())
    if not 1 <= len(tokens) <= MAX_TOKENS or " ".join(tokens) != text:
        raise ValueError("command-template")
    program = tokens[0]
    if not _PROGRAM.fullmatch(program) or program.casefold() in FORBIDDEN_PROGRAMS:
        raise ValueError("command-template")
    for token in tokens[1:]:
        if token not in _PLACEHOLDERS and not _LITERAL.fullmatch(token):
            raise ValueError("command-template")
    if len(tokens) > 1 and tokens[1] in _PLACEHOLDERS:
        raise ValueError("command-template")      # the first argument (usually the verb) is always the owner's literal
    if tokens and os.path.basename(tokens[0]) in SUBCOMMAND_TOOLS and os.path.basename(tokens[0]) != "journalctl":
        _require_literal_verb(tokens)
    if strict:
        # A template with side effects: the verb - the first word that is not an option - must be the owner's literal,
        # so no placeholder may stand in front of it (`systemctl --user <word> x` would let the model choose it).
        _require_literal_verb(tokens)
    return tokens


def parse_templates(items, *, strict=False):
    if type(items) is not list or len(items) > MAX_TEMPLATES:
        raise ValueError("command-template")
    result = tuple(parse_template(item, strict=strict) for item in items)
    if len(set(result)) != len(result):
        raise ValueError("command-template")
    return result


def clean_argv(value):
    """A model-proposed argv: a short list of plain words. No interpretation, no expansion."""
    if (type(value) is not list or not 1 <= len(value) <= MAX_TOKENS
            or any(type(item) is not str or not item or len(item.encode("utf-8")) > MAX_TOKEN_BYTES
                   or any(unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp", "Zs") for char in item)
                   for item in value)):
        raise ValueError("command-argv")
    return list(value)


def match(templates, argv):
    """True when argv fits one of the owner's templates exactly (literals equal, placeholders validated)."""
    for template in templates:
        if len(template) == len(argv) and all(
                (_PLACEHOLDERS[token].fullmatch(word) if token in _PLACEHOLDERS else token == word)
                for token, word in zip(template, argv)):
            return True
    return False


def resolve(program, search=SEARCH_PATH):
    """Absolute path of a bare program name in fixed system directories, or None. The file (after symlinks) and
    every directory above it must be root-owned and not writable by group/other: a user-writable path never runs."""
    if type(program) is not str or not _PROGRAM.fullmatch(program):
        return None
    for directory in search:
        candidate = os.path.join(directory, program)
        try:
            real = os.path.realpath(candidate, strict=True)
            if os.path.basename(real).casefold() in FORBIDDEN_PROGRAMS:
                continue
            info = os.stat(real)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                    or not os.access(real, os.X_OK)):
                continue
            parts = real.split("/")[1:-1]
            current = ""
            for part in parts:
                current += "/" + part
                parent = os.stat(current)
                if parent.st_uid != 0 or parent.st_mode & 0o022:
                    raise OSError()
            return real
        except OSError:
            continue
    return None


def sanitize(raw, limit):
    """(text, truncated): decoded leniently, terminal escapes and control characters removed, cut at a byte limit."""
    text = _ANSI.sub("", raw.decode("utf-8", "replace")).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(char for char in text
                   if char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf", "Cs", "Zl", "Zp"))
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", "ignore"), True


def sandbox_command(path, argv, *, share_processes=False):
    """Read-only, network-less view of the system for ONE command; no shell, clean environment.
    share_processes (owner opt-in) keeps the host's process table visible: the read-only bind of / already carries the
    host /proc, so the private PID namespace and its own /proc are simply not created."""
    bwrap = shutil.which("bwrap", path="/usr/bin:/bin")
    if bwrap is None:
        return None
    isolation = [] if share_processes else ["--proc", "/proc"]
    return [bwrap, "--ro-bind", "/", "/", *isolation, "--dev", "/dev", "--tmpfs", "/tmp",
            "--tmpfs", "/home", "--tmpfs", "/root", "--tmpfs", "/run/user",
            "--unshare-net", "--unshare-ipc", *([] if share_processes else ["--unshare-pid"]),
            "--unshare-uts", "--die-with-parent", "--new-session",
            "--cap-drop", "ALL", "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LANG", "C.UTF-8",
            "--setenv", "LC_ALL", "C.UTF-8", "--setenv", "TERM", "dumb", "--setenv", "NO_COLOR", "1",
            "--setenv", "SYSTEMD_COLORS", "0", "--setenv", "SYSTEMD_PAGER", "", "--setenv", "PAGER", "cat",
            "--setenv", "HOME", "/tmp",
            "--chdir", "/", path, *argv[1:]]


def host_environment():
    """Clean environment for a change/test/undo command that must reach the user's own services: no secrets, no HOME."""
    uid = os.geteuid()
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1",
            "SYSTEMD_COLORS": "0", "SYSTEMD_PAGER": "", "PAGER": "cat", "XDG_RUNTIME_DIR": "/run/user/%d" % uid,
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/%d/bus" % uid}


def host_command(path, argv):
    return [path, *argv[1:]]


def execute(path, argv, *, output_limit, timeout, command=None, host=False, role="read"):
    """Run one approved command. Returns the closed result step; never raises for a failing command."""
    argv_list = list(argv)
    command = command or (host_command if host else sandbox_command)
    full = command(path, argv_list)
    if full is None:
        return {"op": "run", "argv": argv_list, "status": "unavailable", "exit": None, "output": "", "truncated": False,
                "role": role}
    started = time.monotonic()
    try:
        process = subprocess.Popen(full, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=host_environment() if host else {"PATH": "/usr/bin:/bin"},
                                   start_new_session=True, close_fds=True, cwd="/")
    except OSError:
        return {"op": "run", "argv": argv_list, "status": "unavailable", "exit": None, "output": "", "truncated": False,
                "role": role}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "out")
    selector.register(process.stderr, selectors.EVENT_READ, "err")
    buffers = {"out": bytearray(), "err": bytearray()}
    caps = {"out": output_limit + 1, "err": STDERR_BYTES + 1}
    status, deadline, stopped, capped = "ok", started + timeout, False, False
    code = None
    try:
        while selector.get_map() and not stopped:
            left = deadline - time.monotonic()
            if left <= 0:
                status = "timeout"
                break
            for key, _ in selector.select(min(left, 0.5)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                room = caps[key.data] - len(buffers[key.data])
                buffers[key.data].extend(chunk[:max(room, 0)])
                if len(buffers[key.data]) >= caps[key.data] and key.data == "out":
                    if host:
                        capped = True      # a change is never killed halfway for being talkative: drain, keep the cap
                    else:
                        stopped = True     # enough: more would only be cut off, so the command is stopped
                        break
        if status == "ok" and not stopped:
            # Both pipes are closed; a command may still be running (it closed its output on purpose): give it the time
            # it has left instead of calling a late success a failure.
            try:
                code = process.wait(timeout=max(deadline - time.monotonic(), 0.01))
            except subprocess.TimeoutExpired:
                status = "timeout"
    finally:
        selector.close()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        for stream in (process.stdout, process.stderr):
            stream.close()
        waited = process.wait()
        code = waited if code is None else code
    text, cut = sanitize(bytes(buffers["out"]), output_limit)
    error, _ = sanitize(bytes(buffers["err"]), STDERR_BYTES)
    cut = cut or stopped or capped                    # a stopped command produced more than was kept, whatever sanitising removed
    if status == "ok" and not stopped and code != 0:
        status = "failed"
    if status in ("ok", "failed") and not stopped and code != 0 and error.strip():
        joined = text + ("\n" if text and not text.endswith("\n") else "") + "[stderr] " + error.strip()
        text, more = sanitize(joined.encode("utf-8"), output_limit)           # cut by BYTES, never by characters
        cut = cut or more
    exit_code = code if status in ("ok", "failed") and not stopped and -255 <= code <= 255 else None
    return {"op": "run", "argv": argv_list, "status": status, "exit": exit_code, "output": text, "truncated": cut,
            "role": role}
