"""Opt-in installed file-context acceptance using a temporary synthetic policy.

The test-only entrypoint changes the policy's private HOME lookup, not the
application's guards or any installed file. The real user's configuration,
HOME and OAuth profile are never edited. Two native requests form one file
question; its final reply stays on the user's TTY; only refusal cases (which cannot send to Claude) are captured in memory.
"""
import json
import os
from pathlib import Path
import pty
import select
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import host_analysis_acceptance as prior
from ai_desktop.terminal_consent import PROMPTS

FLAG = "--allow-subscription-file-test"
LOCAL_FLAG = "--allow-configured-file-read"
REFUSAL_MARKER = "SYNTHETIC FILE REFUSAL: upfront=REFUSED requests=0 reads=0 native=false"
NATIVE_MARKER = "HOST FILE NATIVE: PASS approvals=1 requests=2 reads=1 files=5 bytes=12175 final-marker=PASS"
FINAL_MARKER = "AI_DESKTOP_FILE_LIMITS_OK"
FILE_SIZES = (9222, 1096, 1090, 579, 188)


def _sized_text(size, tail):
    prefix = "AI Desktop synthetic fixture.\n"
    return prefix + "x" * (size - len((prefix + tail).encode("utf-8"))) + tail


FILE_CONTENTS = {"notes.txt": _sized_text(FILE_SIZES[0], "\nanswer_marker = " + FINAL_MARKER + "\n"),
                 **{"part-" + str(index) + ".txt": _sized_text(size, "\nsynthetic companion\n")
                    for index, size in enumerate(FILE_SIZES[1:], 2)}}
CONTENTS = FILE_CONTENTS["notes.txt"]
DENIED_CONTENTS = "SYNTHETIC FILE DENIED CONTENT\n"
QUESTION = ("W syntetycznym projekcie aidfixture odczytaj wszystkie pięć plików dopuszczonych przez regułę. "
            "Odpowiedz wyłącznie wartością answer_marker z końca notes.txt. Nie wykonuj zmian.")
POLICY_MARKER = "SYNTHETIC POLICY: PASS allow=PASS deny=PASS"
LOCAL_QUESTION = "Sprawdź rozmiary wybranego kontekstu plikowego bez wysyłania danych."
CONFIGURED_RUNNER = """
import sys
def deny_live_operations(event, args):
    if event.startswith('socket.') or event in (
            'subprocess.Popen', 'os.system', 'os.fork', 'os.posix_spawn', 'os.exec'):
        raise RuntimeError('local read cannot start a network or child process')
sys.addaudithook(deny_live_operations)
sys.path.insert(0, '/usr/lib/ai-desktop')
import ai_desktop
from pathlib import Path
if Path(ai_desktop.__file__).resolve() != Path('/usr/lib/ai-desktop/ai_desktop/__init__.py'):
    raise SystemExit(2)
sys.path.insert(0, sys.argv[1])
import host_file_access_acceptance as harness
raise SystemExit(harness.configured_read(sys.argv[2]))
"""


def fixture_context(fixture):
    return {"kind": "files", "files": [{"path": str(path), "content": content}
            for path, content in sorted(fixture["files"].items(), key=lambda item: str(item[0]))]}


def configured_read(pattern, *, reader=input):
    """Installed-policy read only. No HOME override, model call or transcript."""
    from ai_desktop import permissions
    from ai_desktop.analysis_contract import application_input, canonical, terminal_text, validate_context
    from ai_desktop.terminal_consent import confirm
    stage = "identity"
    try:
        if (os.geteuid() <= 0 or os.getuid() != os.geteuid() or os.getgid() != os.getegid()
                or not (os.isatty(0) and os.isatty(1))):
            raise ValueError("identity")
        stage = "policy"
        policy = permissions.load_policy()
        stage = "scope"
        selected = tuple(scope for scope in policy.scopes() if scope.pattern == pattern)
        if len(selected) != 1:
            raise ValueError("scope")
        print("Local test, nothing is sent. Exact rule: " + terminal_text(json.dumps(selected[0].pattern, ensure_ascii=False)))
        print("The contents stay in the memory of this process; the result shows only numbers and status.")
        stage = "consent"
        if not confirm("files-read", reader=reader):
            raise ValueError("consent")
        policy.recheck()
        stage = "read"
        paths = policy.expand((selected[0].index,))
        item = permissions.read_selected(policy, paths)
        stage = "wire"
        context = validate_context([item])
        payload = application_input(None, question=LOCAL_QUESTION, context=context)
        policy.recheck()
        sizes = [len(file["content"].encode("utf-8")) for file in item["files"]]
        print("HOST FILE LOCAL: PASS files=" + str(len(sizes)) + " bytes=" + str(sum(sizes))
              + " sizes=" + ",".join(map(str, sizes))
              + " context-bytes=" + str(len(canonical(context).encode("utf-8")))
              + " application-bytes=" + str(len(payload.encode("utf-8")))
              + " native-requests=0")
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, EOFError, KeyboardInterrupt):
        print("HOST FILE LOCAL: FAIL stage=" + stage + " native-requests=0")
        return 2


def configured_mode(pattern):
    try:
        prior.installed_matches()
        result = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", CONFIGURED_RUNNER,
                                 str(ROOT / "tests"), pattern], timeout=180, close_fds=True,
                                env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
        return 0 if result.returncode == 0 else 2
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError, KeyboardInterrupt):
        print("HOST FILE LOCAL: FAIL stage=installed-helper native-requests=0")
        return 2


def runtime_directory(uid):
    """Require the existing protected desktop runtime; create nothing here."""
    if type(uid) is not int or uid <= 0:
        raise ValueError("fixture-identity")
    for path in (Path("/"), Path("/run"), Path("/run/user")):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("fixture-runtime")
    path = Path("/run/user") / str(uid)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("fixture-runtime")
    return path


def _new_file(path, content):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def create_fixture(home, data, *, systemd=False):
    """Only fresh private roots supplied by this harness or disposable CI."""
    home, data = Path(home), Path(data)
    for path in (home, data):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("fixture-directory")
    config = home / ".config" / "ai-desktop"
    (home / ".config").mkdir(mode=0o700)
    config.mkdir(mode=0o700)
    project = data / "aidfixture"
    project.mkdir(mode=0o700)
    allowed, denied = project / "notes.txt", project / "denied.txt"
    files = {project / name: content for name, content in FILE_CONTENTS.items()}
    for path, content in files.items():
        _new_file(path, content)
    _new_file(denied, DENIED_CONTENTS)
    expected = fixture_context({"files": files})
    policy = ("version = 1\n\n[files]\nread = [" + json.dumps(str(project) + "/*")
              + "]\ndeny = [" + json.dumps(str(denied)) + "]\n"
              + "\n[network]\nclaude = true\nweather = false\n"
              + ("\n[system]\nsystemd = [\"system\"]\n" if systemd else ""))
    policy_path = config / "permissions.toml"
    _new_file(policy_path, policy)
    script = home / "file-fixture.py"
    bootstrap = ("import sys, json\n"
                 "sys.path.insert(0, '/usr/lib/ai-desktop')\n"
                 "from ai_desktop import cli, permissions, ask_cli, file_context, terminal_consent\n"
                 "permissions._home = lambda uid: " + repr(str(home)) + "\n"
                 "if sys.argv[1:] == ['policy-check']:\n"
                 "    policy = permissions.load_policy()\n"
                 "    assert policy.select([" + repr(str(allowed)) + "]) == (" + repr(str(allowed)) + ",)\n"
                 "    try:\n"
                 "        policy.select([" + repr(str(denied)) + "])\n"
                 "    except ValueError:\n"
                 "        pass\n"
                 "    else:\n"
                 "        raise SystemExit(2)\n"
                 "    policy.recheck()\n"
                 "    print(" + repr(POLICY_MARKER) + ")\n"
                 "    raise SystemExit(0)\n"
                 "if sys.argv[1:] not in ([], ['--preview'], ['refusal']):\n"
                 "    raise SystemExit(2)\n"
                 "refusal = sys.argv[1:] == ['refusal']\n"
                 "reads, requests, final_answers, approvals = [], [], [], []\n"
                 "expected = " + repr(expected) + "\n"
                 "original_collect, original_request = file_context.collect, ask_cli.request\n"
                 "original_input = terminal_consent.fresh_input\n"
                 "def observed_input(prompt, **kwargs):\n"
                 "    answer = original_input(prompt, **kwargs)\n"
                 "    if type(answer) is str and answer.strip().casefold() in ('yes', 'y'):\n"
                 "        approvals.append(prompt)\n"
                 "    return answer\n"
                 "terminal_consent.fresh_input = observed_input\n"
                 "def collect(*values, **kwargs):\n"
                 "    item = original_collect(*values, **kwargs)\n"
                 "    assert item == expected\n"
                 "    reads.append(True)\n"
                 "    return item\n"
                 "file_context.collect = collect\n"
                 "def request(question, args, reader, *, context=None, access=None, check, state=None, question_consent=None, discovery=None):\n"
                 "    requests.append('assistant' if access is not None else 'question')\n"
                 "    if access is None:\n"
                 "        assert context == [expected]\n"
                 "    extra_observation = {} if discovery is None else {'discovery': discovery}\n"
                 "    answer = original_request(question, args, reader, context=context, access=access, check=check, state=state, question_consent=question_consent, **extra_observation)\n"
                 "    if access is None:\n"
                 "        final_answers.append(type(answer) is str and answer.count(" + repr(FINAL_MARKER) + ") == 1)\n"
                 "    return answer\n"
                 "ask_cli.request = request\n"
                 "if refusal:\n"
                 "    def no_native(*values, **kwargs):\n"
                 "        raise AssertionError('native forbidden in synthetic refusal')\n"
                 "    ask_cli.managed_session = no_native\n"
                 "    ask_cli.send_once = no_native\n"
                 "extra = ['--details'] if refusal else sys.argv[1:]\n"
                 "code = cli.main(['ask', '--question', " + repr(QUESTION) + "] + extra)\n"
                 "if refusal:\n"
                 "    assert code == 2 and not requests and not reads and not approvals\n"
                 "    print(" + repr(REFUSAL_MARKER) + ")\n"
                 "elif code == 0 and extra != ['--preview']:\n"
                 "    if (requests != ['assistant', 'assistant', 'question'] or len(reads) != 1 or final_answers != [True]\n"
                 "            or approvals != [terminal_consent.PROMPTS['ask-question']]):\n"
                 "        raise SystemExit(2)\n"
                 "    print(" + repr(NATIVE_MARKER) + ")\n"
                 "raise SystemExit(code)\n")
    _new_file(script, bootstrap)
    return {"script": script, "allowed": allowed, "denied": denied, "policy": policy_path,
            "policy_text": policy, "home": home, "files": files}


def command(fixture, *extra):
    return ["/usr/bin/python3", "-I", "-B", str(fixture["script"]), *extra]


def check_unchanged(fixture):
    for path, expected in (*fixture["files"].items(), (fixture["denied"], DENIED_CONTENTS),
                           (fixture["policy"], fixture["policy_text"])):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or path.read_text(encoding="utf-8") != expected):
            raise ValueError("fixture-changed")


def reject(fixture):
    """Refuse the whole question before any file read or native request."""
    master, slave = pty.openpty()
    process = subprocess.Popen(command(fixture, "refusal"), stdin=slave, stdout=slave, stderr=slave,
                               close_fds=True, start_new_session=True,
                               env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
    os.close(slave)
    output, answered = bytearray(), False
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and len(output) <= 32768:
            if select.select([master], [], [], .1)[0]:
                try:
                    raw = os.read(master, 8192)
                except OSError:
                    break
                if not raw:
                    break
                output.extend(raw)
            if PROMPTS["ask-question"].encode() in output and not answered:
                os.write(master, b"no\n")
                answered = True
            if process.poll() is not None and not select.select([master], [], [], .1)[0]:
                break
        process.wait(timeout=2)
        if (process.returncode != 2 or not answered
                or REFUSAL_MARKER.encode() not in output
                or DENIED_CONTENTS.strip().encode() in output
                or b"Administrator permissions" in output
                or b"Waiting for " in output
                or FINAL_MARKER.encode() in output
                or PROMPTS["files-read"].encode() in output
                or PROMPTS["ask-transfer"].encode() in output):
            raise ValueError("file-refusal")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        configured = (len(args) == 2 and args[0] == LOCAL_FLAG and type(args[1]) is str
                      and 0 < len(args[1].encode("utf-8")) <= 512)
    except UnicodeError:
        configured = False
    if args != [FLAG] and not configured:
        print("HOST FILE: REFUSED (explicit subscription-file or configured-read flag required)")
        return 2
    if (os.geteuid() <= 0 or os.getuid() != os.geteuid()
            or not (os.isatty(0) and os.isatty(1))):
        print("HOST FILE: REFUSED (desktop UID and uncaptured TTY required)")
        return 2
    if configured:
        return configured_mode(args[1])
    stage, started, passed = "preflight", False, False
    try:
        uid, units = prior.preflight()
        runtime = runtime_directory(uid)
        with tempfile.TemporaryDirectory(prefix="ai-file-policy-", dir=runtime) as home, \
                tempfile.TemporaryDirectory(prefix="ai-file-data-", dir="/tmp") as data:
            stage = "fixture"
            fixture = create_fixture(home, data)
            stage = "policy"
            checked = subprocess.run(command(fixture, "policy-check"), capture_output=True,
                                     check=False, timeout=10, close_fds=True,
                                     env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
            if checked.returncode or checked.stdout.strip() != POLICY_MARKER.encode() or checked.stderr:
                raise ValueError("policy")
            stage = "question-refusal"
            reject(fixture)
            prior.check_stopped(uid, units)
            stage = "native-request"
            print("The test uses only a temporary configuration and the synthetic project aidfixture.")
            print("One approval covers the whole question: choosing the data and the answer after the read, two Claude requests in all.")
            print("Five files: 9222 + 1096 + 1090 + 579 + 188 = 12175 bytes; the answer stays on the terminal.")
            started = True
            prior.run(*command(fixture), timeout=420)
            stage = "unchanged"
            check_unchanged(fixture)
            prior.check_stopped(uid, units)
            stage = "fixture-cleanup"
        passed = True
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError, KeyboardInterrupt):
        print("HOST FILE: FAIL stage=" + stage)
    finally:
        if started:
            try:
                prior.check_stopped(uid, units)
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
                passed = False
                print("HOST FILE: FAIL stage=cleanup; use ai-desktop stop")
    if passed:
        print("HOST FILE: PASS synthetic-policy=PASS question-refusal=PASS approvals=1 "
              "native-requests=2 selected-read=PASS files=5 bytes=12175 final-marker=PASS "
              "source-unchanged=PASS cleanup=PASS actual-user-policy=NOT-TESTED")
    else:
        print("HOST FILE: FAIL")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
