"""Opt-in native HOME-scope acceptance, using only a private synthetic HOME.

Two native requests share one question approval. The reviewed new file and its
rollback each need manual consent. The desktop's actual policy is never opened.
"""
import json
import os
from pathlib import Path
import pty
import pwd
import select
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import host_analysis_acceptance as prior
from host_file_access_acceptance import _new_file
from ai_desktop import task_workspace
from ai_desktop.terminal_consent import PROMPTS

FLAG = "--allow-subscription-home-test"
MARKER = "AI_DESKTOP_HOME_CONTEXT_OK"
CONTEXT = "result_marker = " + MARKER + "\n"
CREATED = "# " + MARKER + '\nprint("Hello World")\n'
NOISE = {"noise/" + str(index).zfill(3) + ".txt": "UNSELECTED_SYNTHETIC_" + str(index) + "\n"
         for index in range(80)}
DENIED = {"private/secret.txt": "DENIED_PRIVATE_SYNTHETIC\n", ".ssh/fixture.txt": "DENIED_SSH_SYNTHETIC\n"}
QUESTION = ("W moim HOME odczytaj wyłącznie test/context.txt. Utwórz test/hello.py w istniejącym katalogu test. "
            "Plik ma dokładnie dwie linie: pierwsza to komentarz # i spacja oraz wartość result_marker "
            'z odczytanego pliku; druga to print("Hello World"). Zakończ obie linie znakiem nowej linii. '
            "Nie zmieniaj context.txt. Nie odczytuj innych plików ani katalogów. "
            "Sprawdź python-syntax i pokaż pełny diff przed zgodą na zapis.")
POLICY_MARKER = "HOME POLICY: PASS home-scope=PASS narrow-selection=PASS deny-before-read=PASS"
REFUSAL_MARKER = "HOME REFUSAL: PASS requests=0 reads=0 tests=0 writes=0"
NATIVE_MARKER = ("HOME NATIVE: PASS question-approvals=1 write-approvals=1 requests=2 "
                 "read-files=1 unselected=80 denied=2 test=PASS diff=PASS create=PASS")

BOOTSTRAP = '''import os, sys
sys.path.insert(0, '/usr/lib/ai-desktop')
from ai_desktop import cli, permissions, ask_cli, task_cli, terminal_consent
permissions._home = lambda uid: __HOME__
home, expected, created = __HOME__, __CONTEXT__, __CREATED__
if sys.argv[1:] == ['policy-check']:
    policy = permissions.load_policy()
    assert policy.write_workspace(0) == home
    original_read, original_scandir = permissions._read, os.scandir
    def only_policy(path, *args, **kwargs):
        assert path == home + '/.config/ai-desktop/permissions.toml' and kwargs.get('policy') is True
        return original_read(path, *args, **kwargs)
    def no_enumeration(*args, **kwargs):
        raise AssertionError('enumeration forbidden for exact selected paths')
    permissions._read, os.scandir = only_policy, no_enumeration
    try:
        assert policy.expand_selected([(0, 'test/context.txt')]) == (home + '/test/context.txt',)
        for name in ('private/secret.txt', '.ssh/fixture.txt'):
            try:
                policy.expand_selected([(0, name)])
            except ValueError:
                pass
            else:
                raise SystemExit(2)
    finally:
        permissions._read, os.scandir = original_read, original_scandir
    task_cli.workspace.snapshot(home, ['test/context.txt', 'test/hello.py'], must_be_absent=['test/hello.py'])
    policy.recheck()
    print(__POLICY_MARKER__)
    raise SystemExit(0)
if sys.argv[1:] not in ([], ['refusal']):
    raise SystemExit(2)
refusal = sys.argv[1:] == ['refusal']
requests, approvals, reads, tests, diffs, writes = [], [], [], [], [], []
original_request, original_input = ask_cli.request, terminal_consent.fresh_input
original_read = permissions.read_selected
original_test, original_diff, original_apply = task_cli.task_sandbox.run, task_cli.show_diff, task_cli.workspace.apply
def inspect_source(source):
    files = source['files']
    assert len(files) == 2
    assert {item['path']: (item['content'], item['writable']) for item in files} == {
        'test/context.txt': (expected, True), 'test/hello.py': (None, True)}
def observed_input(prompt, **kwargs):
    answer = original_input(prompt, **kwargs)
    if type(answer) is str and answer.strip().casefold() in ('yes', 'y'):
        approvals.append(prompt)
    return answer
terminal_consent.fresh_input = observed_input
def read(policy, paths):
    assert tuple(paths) == (home + '/test/context.txt',)
    reads.append(True)
    return original_read(policy, paths)
permissions.read_selected = read
def request(*values, **kwargs):
    if kwargs.get('access') is not None:
        access = kwargs['access']
        assert access['files'] == [{'id': 'f0', 'pattern': '~/'}]
        assert access['write'] == [{'id': 'w0', 'pattern': '~/'}]
        requests.append('assistant')
    else:
        inspect_source(kwargs.get('task'))
        requests.append('workspace')
    return original_request(*values, **kwargs)
ask_cli.request = request
def test(source, proposed, check):
    inspect_source(source)
    assert check == 'python-syntax'
    assert proposed == [{'path': 'test/hello.py', 'content': created}]
    result = original_test(source, proposed, check)
    tests.append(result.get('passed') is True)
    return result
task_cli.task_sandbox.run = test
def diff(plan, **kwargs):
    assert plan.data()['proposal']['files'] == [{'path': 'test/hello.py', 'content': created}]
    original_diff(plan, **kwargs)
    diffs.append(True)
task_cli.show_diff = diff
def apply(plan, approval, **kwargs):
    assert tests == [True] and diffs == [True]
    assert approvals == [terminal_consent.PROMPTS['ask-question'], terminal_consent.PROMPTS['write']]
    assert plan.data()['before']['workspace'] == home
    original_apply(plan, approval, **kwargs)
    writes.append(True)
task_cli.workspace.apply = apply
if refusal:
    def forbidden(*values, **kwargs):
        raise AssertionError('native, reads, tests and writes forbidden after upfront refusal')
    ask_cli.managed_session = forbidden
    ask_cli.send_once = forbidden
    permissions.read_selected = forbidden
    task_cli.task_sandbox.run = forbidden
    task_cli.workspace.apply = forbidden
code = cli.main(['ask', '--question', __QUESTION__])
if refusal:
    assert code == 2 and not any((requests, approvals, reads, tests, diffs, writes))
    print(__REFUSAL_MARKER__)
elif code == 0:
    if (requests != ['assistant', 'workspace'] or reads != [True] or tests != [True]
            or diffs != [True] or writes != [True]
            or approvals != [terminal_consent.PROMPTS['ask-question'], terminal_consent.PROMPTS['write']]):
        raise SystemExit(2)
    print(__NATIVE_MARKER__)
raise SystemExit(code)
'''


def create_fixture(home):
    home = Path(home)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("fixture-root")
    for name in (".config", ".config/ai-desktop", "test", "noise", "private", ".ssh"):
        (home / name).mkdir(mode=0o700)
    source = {"test/context.txt": CONTEXT, **NOISE, **DENIED}
    policy = ('version = 2\n[files]\nread = ["~/"]\nwrite = ["~/"]\n'
              'deny = ["~/private/", "~/.ssh/"]\n[network]\nclaude = true\nweather = false\n')
    source[".config/ai-desktop/permissions.toml"] = policy
    bootstrap = BOOTSTRAP
    for key, value in {"HOME": str(home), "CONTEXT": CONTEXT, "CREATED": CREATED, "QUESTION": QUESTION,
                       "POLICY_MARKER": POLICY_MARKER, "REFUSAL_MARKER": REFUSAL_MARKER,
                       "NATIVE_MARKER": NATIVE_MARKER}.items():
        bootstrap = bootstrap.replace("__" + key + "__", repr(value))
    source["home-fixture.py"] = bootstrap
    for name, content in source.items():
        _new_file(home / name, content)
    return {"home": home, "script": home / "home-fixture.py", "source": source}


def command(fixture, *args):
    return ["/usr/bin/python3", "-I", "-B", str(fixture["script"]), *args]


def check_state(fixture, *, applied=False):
    home = fixture["home"]
    expected = dict(fixture["source"])
    if applied:
        expected["test/hello.py"] = CREATED
    for name, content in expected.items():
        path = home / name
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or path.read_text(encoding="utf-8") != content):
            raise ValueError("fixture-changed")
    actual = set()
    for path in home.rglob("*"):
        relative = path.relative_to(home)
        if ".ai-desktop-tasks" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError("unexpected-link")
        if path.is_dir():
            if relative.as_posix() not in (".config", ".config/ai-desktop", "test", "noise", "private", ".ssh"):
                raise ValueError("unexpected-directory")
        else:
            actual.add(relative.as_posix())
    if actual != set(expected):
        raise ValueError("unexpected-files")


def reject(fixture):
    master, slave = pty.openpty()
    process = subprocess.Popen(command(fixture, "refusal"), stdin=slave, stdout=slave, stderr=slave,
        close_fds=True, start_new_session=True, env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
    os.close(slave)
    output, answered = bytearray(), False
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and len(output) <= 32768:
            if select.select([master], [], [], .1)[0]:
                try:
                    block = os.read(master, 8192)
                except OSError:
                    break
                if not block:
                    break
                output.extend(block)
            if PROMPTS["ask-question"].encode() in output and not answered:
                os.write(master, b"no\n")
                answered = True
            if process.poll() is not None and not select.select([master], [], [], .1)[0]:
                break
        process.wait(timeout=2)
        if (process.returncode != 2 or not answered or REFUSAL_MARKER.encode() not in output
                or MARKER.encode() in output or PROMPTS["write"].encode() in output):
            raise ValueError("refusal")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if (args != [FLAG] or os.geteuid() <= 0 or os.getuid() != os.geteuid()
            or not (os.isatty(0) and os.isatty(1))):
        print("HOST HOME: REFUSED (explicit flag, desktop UID and uncaptured TTY required)")
        return 2
    stage, started, passed = "preflight", False, False
    try:
        uid, units = prior.preflight()
        with tempfile.TemporaryDirectory(prefix="ai-home-policy-", dir=pwd.getpwuid(uid).pw_dir) as home:
            stage = "fixture"
            fixture = create_fixture(home)
            stage = "policy"
            checked = subprocess.run(command(fixture, "policy-check"), capture_output=True, timeout=10,
                close_fds=True, env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
            if checked.returncode or checked.stdout.strip() != POLICY_MARKER.encode() or checked.stderr:
                raise ValueError("policy")
            stage = "upfront-refusal"
            reject(fixture)
            check_state(fixture)
            if task_workspace.status(str(fixture["home"])):
                raise ValueError("refusal-wrote-journal")
            prior.check_stopped(uid, units)
            stage = "native-create"
            print("The test uses a private synthetic HOME; your configuration stays untouched.")
            print("Approve the question, then the save after the test and the diff, and finally the undo.")
            started = True
            prior.run(*command(fixture), timeout=600)
            stage = "applied"
            check_state(fixture, applied=True)
            records = task_workspace.status(str(fixture["home"]))
            if len(records) != 1 or records[0][1] != "applied":
                raise ValueError("journal")
            prior.check_stopped(uid, units)
            stage = "rollback"
            prior.run("/usr/bin/ai-desktop", "task", "rollback", "--workspace", str(fixture["home"]), timeout=240)
            check_state(fixture)
            if task_workspace.status(str(fixture["home"])) != [(records[0][0], "rolled-back")]:
                raise ValueError("rollback")
            prior.check_stopped(uid, units)
            stage = "fixture-cleanup"
        passed = True
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError, KeyboardInterrupt):
        print("HOST HOME: FAIL stage=" + stage)
    finally:
        if started:
            try:
                prior.check_stopped(uid, units)
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
                passed = False
                print("HOST HOME: FAIL stage=cleanup; use ai-desktop stop")
    print("HOST HOME: " + ("PASS policy=PASS upfront-refusal=PASS question-approvals=1 write-approvals=1 "
        "native-requests=2 read-files=1 unselected=80 denied=2 test=PASS diff=PASS create=PASS rollback=PASS "
        "cleanup=PASS actual-user-policy=NOT-TESTED" if passed else "FAIL"))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
