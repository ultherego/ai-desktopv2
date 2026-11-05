"""Opt-in native ask editing against a private synthetic project only.

One question authorizes two native requests; writing the reviewed diff and
rolling it back each require fresh manual consent. No real policy is replaced.
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

FLAG = "--allow-subscription-ask-edit-test"
TAIL_MARKER = "AI_DESKTOP_EDIT_CONTEXT_TAIL_OK"
MAIN_PREFIX = "def add(a, b):\n    return a - b\n\n# PRESERVE_SYNTHETIC_PADDING\n"
MAIN_SUFFIX = "\n# PRESERVE_SYNTHETIC_END\n"


def original_main():
    source, index = MAIN_PREFIX, 1
    while True:
        line = "# Preserve synthetic row " + str(index).zfill(3) + ": unchanged fixture context.\n"
        if len(source + line + MAIN_SUFFIX) + 2 > 9222:
            break
        source += line
        index += 1
    remaining = 9222 - len(source + MAIN_SUFFIX)
    return source + "#" + "." * (remaining - 2) + "\n" + MAIN_SUFFIX


ORIGINAL_MAIN = original_main()
EDITED_MAIN = ORIGINAL_MAIN.replace("return a - b", "return a + b")
LARGE_SUFFIX = "\nresult_marker = " + TAIL_MARKER + "\n"
LARGE_CONTEXT = "x" * (40960 - len(LARGE_SUFFIX)) + LARGE_SUFFIX
TEST_SOURCE = '''import unittest
from pathlib import Path
from main import add
class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
    def test_new_file(self):
        expected = Path('context/01.txt').read_text().splitlines()[-1].split('=', 1)[1].strip()
        self.assertEqual(Path('result.txt').read_text(), expected + '\\n')
'''
FILES = {"main.py": ORIGINAL_MAIN, "tests/__init__.py": "", "tests/test_main.py": TEST_SOURCE,
         "context/01.txt": LARGE_CONTEXT,
         **{"context/0" + str(index) + ".txt": "SYNTHETIC CONTEXT " + str(index) + "\n" for index in range(2, 7)}}
DENIED = "SYNTHETIC DENIED FILE; MUST STAY UNCHANGED\n"
QUESTION = ("W syntetycznym projekcie aideditfixture odczytaj cały dozwolony zakres, także pliki context/ i tests/. "
            "Napraw main.py: zamień wyłącznie return a - b na return a + b; zachowaj wszystkie pozostałe bajty. "
            "Utwórz result.txt w istniejącym katalogu projektu: wartość result_marker z ostatniej linii "
            "context/01.txt i jeden znak nowej linii. Zmień tylko main.py i result.txt. "
            "Przetestuj przez python-unittest. Pokaż diff przed zgodą na zapis.")
POLICY_MARKER = "ASK EDIT POLICY: PASS read=PASS write=PASS deny=PASS readonly=PASS"
REFUSAL_MARKER = "ASK EDIT REFUSAL: PASS requests=0 tests=0 diffs=0 writes=0"
NATIVE_MARKER = "ASK EDIT NATIVE: PASS question-approvals=1 write-approvals=1 requests=2 source-files=9 request-over-32KiB=PASS test=PASS diff=PASS apply=PASS"


BOOTSTRAP = '''import json, sys
sys.path.insert(0, '/usr/lib/ai-desktop')
from ai_desktop import cli, permissions, ask_cli, task_cli, terminal_consent
from ai_desktop.analysis_contract import application_input
permissions._home = lambda uid: __HOME__
expected = __EXPECTED__
project = __PROJECT__
replacement = __REPLACEMENT__
if sys.argv[1:] == ['policy-check']:
    policy = permissions.load_policy()
    paths = [project + '/' + name for name in expected]
    assert policy.select(paths) == tuple(paths)
    assert policy.can_write(project + '/main.py', index=0)
    assert policy.can_write(project + '/result.txt', index=0)
    assert not policy.can_write(project + '/tests/test_main.py', index=0)
    assert not policy.can_write(project + '/denied.txt', index=0)
    assert policy.write_workspace(0) == project
    task_cli.workspace.snapshot(project, [*expected, 'result.txt'], must_be_absent=['result.txt'])
    try:
        policy.select([project + '/denied.txt'])
    except ValueError:
        pass
    else:
        raise SystemExit(2)
    policy.recheck()
    print(__POLICY_MARKER__)
    raise SystemExit(0)
if sys.argv[1:] not in ([], ['refusal']):
    raise SystemExit(2)
refusal = sys.argv[1:] == ['refusal']
requests, approvals, tests, diffs, writes, large = [], [], [], [], [], []
original_request, original_input = ask_cli.request, terminal_consent.fresh_input
original_test, original_diff, original_apply = task_cli.task_sandbox.run, task_cli.show_diff, task_cli.workspace.apply
def inspect_source(source):
    files = source['files']
    assert len(files) == len(expected) + 1
    assert len({item['path'] for item in files}) == len(files)
    for item in files:
        if item['path'] == 'result.txt':
            assert item['content'] is None and item['writable'] is True
        else:
            assert item['path'] in expected and item['content'] == expected[item['path']]
            assert item['writable'] is (item['path'] == 'main.py')
def observed_input(prompt, **kwargs):
    answer = original_input(prompt, **kwargs)
    if type(answer) is str and answer.strip().casefold() in ('yes', 'y'):
        approvals.append(prompt)
    return answer
terminal_consent.fresh_input = observed_input
def request(*values, **kwargs):
    if kwargs.get('access') is not None:
        requests.append('assistant')
    else:
        source = kwargs.get('task')
        inspect_source(source)
        large.append(len(application_input(None, task=source).encode('utf-8')) > 32768)
        requests.append('workspace')
    return original_request(*values, **kwargs)
ask_cli.request = request
def test(source, proposed, check):
    inspect_source(source)
    assert check == 'python-unittest'
    assert {item['path']: item['content'] for item in proposed} == replacement
    result = original_test(source, proposed, check)
    tests.append(result.get('passed') is True)
    return result
task_cli.task_sandbox.run = test
def diff(plan, **kwargs):
    data = plan.data()
    assert {item['path']: item['content'] for item in data['proposal']['files']} == replacement
    original_diff(plan, **kwargs)
    diffs.append(True)
task_cli.show_diff = diff
def apply(plan, approval, **kwargs):
    assert tests == [True] and diffs == [True]
    assert approvals == [terminal_consent.PROMPTS['ask-question'], terminal_consent.PROMPTS['write']]
    assert plan.data()['before']['workspace'] == project
    original_apply(plan, approval, **kwargs)
    writes.append(True)
task_cli.workspace.apply = apply
if refusal:
    def forbidden(*values, **kwargs):
        raise AssertionError('native, tests and writes forbidden after upfront refusal')
    ask_cli.managed_session = forbidden
    ask_cli.send_once = forbidden
    task_cli.task_sandbox.run = forbidden
    task_cli.workspace.apply = forbidden
code = cli.main(['ask', '--question', __QUESTION__])
if refusal:
    assert code == 2 and not any((requests, approvals, tests, diffs, writes, large))
    print(__REFUSAL_MARKER__)
elif code == 0:
    if (requests != ['assistant', 'workspace'] or large != [True] or tests != [True]
            or diffs != [True] or writes != [True]
            or approvals != [terminal_consent.PROMPTS['ask-question'], terminal_consent.PROMPTS['write']]):
        raise SystemExit(2)
    print(__NATIVE_MARKER__)
raise SystemExit(code)
'''


def create_fixture(home, data):
    home, data = Path(home), Path(data)
    for directory in (home, data):
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("fixture-root")
    (home / ".config").mkdir(mode=0o700)
    config = home / ".config/ai-desktop"
    config.mkdir(mode=0o700)
    project = data / "aideditfixture"
    project.mkdir(mode=0o700)
    (project / "context").mkdir(mode=0o700)
    (project / "tests").mkdir(mode=0o700)
    for name, content in FILES.items():
        _new_file(project / name, content)
    _new_file(project / "denied.txt", DENIED)
    policy = ("version = 1\n[files]\nread = [" + json.dumps(str(project) + "/**") + "]\nwrite = ["
              + json.dumps(str(project) + "/*") + "]\ndeny = [" + json.dumps(str(project / "denied.txt"))
              + "]\n[network]\nclaude = true\nweather = false\n")
    _new_file(config / "permissions.toml", policy)
    bootstrap = BOOTSTRAP
    replacements = {"HOME": str(home), "EXPECTED": FILES, "PROJECT": str(project),
                    "REPLACEMENT": {"main.py": EDITED_MAIN, "result.txt": TAIL_MARKER + "\n"},
                    "POLICY_MARKER": POLICY_MARKER, "REFUSAL_MARKER": REFUSAL_MARKER,
                    "NATIVE_MARKER": NATIVE_MARKER, "QUESTION": QUESTION}
    for key, value in replacements.items():
        bootstrap = bootstrap.replace("__" + key + "__", repr(value))
    script = home / "ask-edit-fixture.py"
    _new_file(script, bootstrap)
    return {"home": home, "project": project, "script": script,
            "policy": config / "permissions.toml", "policy_text": policy}


def command(fixture, *args):
    return ["/usr/bin/python3", "-I", "-B", str(fixture["script"]), *args]


def check_state(fixture, *, applied=False):
    project = fixture["project"]
    for name, expected in {**FILES, "denied.txt": DENIED}.items():
        if applied and name == "main.py":
            expected = EDITED_MAIN
        path = project / name
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or path.read_text(encoding="utf-8") != expected):
            raise ValueError("fixture-changed")
    result = project / "result.txt"
    if applied:
        info = result.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or result.read_text() != TAIL_MARKER + "\n"):
            raise ValueError("created-file")
    elif os.path.lexists(result):
        raise ValueError("unexpected-file")
    if fixture["policy"].read_text() != fixture["policy_text"]:
        raise ValueError("policy-changed")
    # All source paths stay fixed; the only permitted extra is the private journal.
    actual = set()
    for path in project.rglob("*"):
        relative = path.relative_to(project)
        if ".ai-desktop-tasks" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError("unexpected-link")
        if path.is_dir():
            if relative.as_posix() not in ("tests", "context"):
                raise ValueError("unexpected-directory")
        else:
            actual.add(relative.as_posix())
    expected = set(FILES) | {"denied.txt"} | ({"result.txt"} if applied else set())
    if actual != expected:
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
                or TAIL_MARKER.encode() in output or DENIED.strip().encode() in output
                or PROMPTS["write"].encode() in output):
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
        print("HOST ASK EDIT: REFUSED (explicit flag, desktop UID and uncaptured TTY required)")
        return 2
    stage, started, passed = "preflight", False, False
    try:
        uid, units = prior.preflight()
        desktop_home = pwd.getpwuid(uid).pw_dir
        with tempfile.TemporaryDirectory(prefix="ai-edit-policy-", dir=desktop_home) as home, \
                tempfile.TemporaryDirectory(prefix="ai-edit-data-", dir=home) as data:
            stage = "fixture"
            fixture = create_fixture(home, data)
            stage = "policy"
            checked = subprocess.run(command(fixture, "policy-check"), capture_output=True, timeout=10,
                close_fds=True, env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
            if checked.returncode or checked.stdout.strip() != POLICY_MARKER.encode() or checked.stderr:
                raise ValueError("policy")
            stage = "upfront-refusal"
            reject(fixture)
            check_state(fixture)
            if task_workspace.status(str(fixture["project"])):
                raise ValueError("refusal-wrote-journal")
            prior.check_stopped(uid, units)
            stage = "native-edit"
            print("The test uses nine synthetic files, including one of 40 KiB; it does not change your configuration.")
            print("Approve one question, then, after the test and the full diff, approve the save separately. Then confirm the undo.")
            started = True
            prior.run(*command(fixture), timeout=600)
            stage = "applied"
            check_state(fixture, applied=True)
            records = task_workspace.status(str(fixture["project"]))
            if len(records) != 1 or records[0][1] != "applied":
                raise ValueError("journal")
            prior.check_stopped(uid, units)
            stage = "rollback"
            prior.run("/usr/bin/ai-desktop", "task", "rollback", "--workspace", str(fixture["project"]), timeout=240)
            check_state(fixture)
            if task_workspace.status(str(fixture["project"])) != [(records[0][0], "rolled-back")]:
                raise ValueError("rollback")
            prior.check_stopped(uid, units)
            stage = "fixture-cleanup"
        passed = True
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError, KeyboardInterrupt):
        print("HOST ASK EDIT: FAIL stage=" + stage)
    finally:
        if started:
            try:
                prior.check_stopped(uid, units)
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
                passed = False
                print("HOST ASK EDIT: FAIL stage=cleanup; use ai-desktop stop")
    print("HOST ASK EDIT: " + ("PASS upfront-refusal=PASS question-approvals=1 write-approvals=1 native-requests=2 "
        "read-files=9 request-over-32KiB=PASS test=PASS diff=PASS apply=PASS rollback=PASS cleanup=PASS actual-user-policy=NOT-TESTED"
        if passed else "FAIL"))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
