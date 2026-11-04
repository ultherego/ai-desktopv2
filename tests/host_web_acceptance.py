"""Explicit native WebSearch acceptance; synthetic policy, no local context.

Native answers stay on the uncaptured terminal. Only refusal output is held in
memory, and only fixed proof metadata is printed by the harness. No login,
credential inspection, local file grant, retry or host-network changes.
"""
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
from ai_desktop.terminal_consent import PROMPTS

FLAG = "--allow-subscription-web-test"
QUESTION = ("Sprawdź w Internecie na oficjalnej stronie python.org, jaka jest obecnie najnowsza "
            "stabilna wersja Python. Podaj numer wersji i datę jej wydania w najwyżej dwóch "
            "zdaniach oraz źródło. Wykonaj wyszukiwanie WebSearch; nie korzystaj z danych lokalnych.")
POLICY_MARKER = "WEB POLICY: PASS web=enabled local-sources=disabled"
REFUSAL_MARKER = "WEB REFUSAL: PASS requests=0 sessions=0 local-reads=0"
NATIVE_MARKER = ("WEB NATIVE: PASS question-approvals=1 requests=2 modes=assistant,web "
                 "sessions=2 local-reads=0 web-proof=PASS source=python.org")

BOOTSTRAP = '''import json, sys
from contextlib import contextmanager
from urllib.parse import urlsplit
sys.path.insert(0, '/usr/lib/ai-desktop')
from ai_desktop import cli, permissions, ask_cli, assistant_flow, ask_context, file_context, ask_edit, terminal_consent
from ai_desktop.analysis_contract import parse_request
permissions._home = lambda uid: __HOME__
home = __HOME__
expected_access = {'files': [], 'systemd': [], 'weather': False, 'web': True}
original_read = permissions._read
def only_policy(path, *args, **kwargs):
    assert path == home + '/.config/ai-desktop/permissions.toml' and kwargs.get('policy') is True
    return original_read(path, *args, **kwargs)
permissions._read = only_policy
policy = permissions.load_policy()
assert policy.access() == expected_access
assert policy.sources() == {'claude': True, 'weather': False, 'systemd': [], 'web': True}
if sys.argv[1:] == ['policy-check']:
    policy.recheck()
    print(__POLICY_MARKER__)
    raise SystemExit(0)
if sys.argv[1:] not in ([], ['refusal']):
    raise SystemExit(2)
refusal = sys.argv[1:] == ['refusal']
requests, approvals, sent, entered, exited, proof = [], [], [], [], [], []
original_request, original_input = ask_cli.request, terminal_consent.fresh_input
original_send, original_session = ask_cli.send_once, ask_cli.managed_session
def forbidden(*args, **kwargs):
    raise AssertionError('local collection and editing are forbidden in web acceptance')
assistant_flow.collect = forbidden
file_context.collect = forbidden
permissions.read_selected = forbidden
ask_context._systemd = forbidden
ask_context._weather = forbidden
ask_edit.prepare = forbidden
def observed_input(prompt, **kwargs):
    answer = original_input(prompt, **kwargs)
    if type(answer) is str and answer.strip().casefold() in ('tak', 't'):
        approvals.append(prompt)
    return answer
terminal_consent.fresh_input = observed_input
@contextmanager
def session(*args, **kwargs):
    entered.append(True)
    with original_session(*args, **kwargs):
        yield
    exited.append(True)
ask_cli.managed_session = session
def send(approval):
    payload, digest = parse_request(approval.payload.encode('utf-8'))
    assert digest == approval.sha256
    data = json.loads(payload)
    assert data['question'] == __QUESTION__
    if data['mode'] == 'assistant':
        assert data == {'mode': 'assistant', 'question': __QUESTION__, 'access': expected_access}
        assert not sent and requests == ['assistant']
    else:
        assert data == {'mode': 'web', 'question': __QUESTION__}
        assert sent == ['assistant'] and requests == ['assistant', 'web']
    assert approvals == [terminal_consent.PROMPTS['ask-question']]
    sent.append(data['mode'])
    return original_send(approval)
ask_cli.send_once = send
def request(*values, **kwargs):
    assert values[0] == __QUESTION__
    assert kwargs.get('context') is None and kwargs.get('task') is None and kwargs.get('write_grant') is None
    if kwargs.get('access') is not None:
        assert kwargs['access'] == expected_access and not kwargs.get('web', False) and not requests
        requests.append('assistant')
        response = original_request(*values, **kwargs)
        assert json.loads(response) == {'action': 'web'}
    else:
        assert kwargs.get('web') is True and requests == ['assistant']
        requests.append('web')
        response = original_request(*values, **kwargs)
        # The production web parser requires real WebSearch metadata before
        # releasing this text. Inspect only its appended source section here.
        assert type(response) is str and '\\nŹródła:\\n' in response
        sources = response.rsplit('\\nŹródła:\\n', 1)[1]
        found = False
        for line in sources.splitlines():
            prefix, separator, url = line.rpartition(' — ')
            if separator and prefix.startswith('['):
                if url.startswith('`') and url.endswith('`'):
                    url = url[1:-1]
                parsed = urlsplit(url)
                if (parsed.scheme == 'https' and parsed.hostname in ('python.org', 'www.python.org', 'docs.python.org')
                        and parsed.username is None and parsed.password is None):
                    found = True
        assert found
        proof.append(True)
    return response
ask_cli.request = request
if refusal:
    ask_cli.managed_session = forbidden
    ask_cli.send_once = forbidden
code = cli.main(['ask', '--question', __QUESTION__])
policy.recheck()
if refusal:
    assert code == 2 and not any((requests, approvals, sent, entered, exited, proof))
    print(__REFUSAL_MARKER__)
elif code == 0:
    assert requests == sent == ['assistant', 'web']
    assert approvals == [terminal_consent.PROMPTS['ask-question']]
    assert entered == exited == [True, True] and proof == [True]
    print(__NATIVE_MARKER__)
raise SystemExit(code)
'''


def create_fixture(home):
    home = Path(home)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("fixture-root")
    for name in (".config", ".config/ai-desktop"):
        (home / name).mkdir(mode=0o700)
    source = {".config/ai-desktop/permissions.toml":
              'version = 2\n[network]\nclaude = true\nweb = true\nweather = false\n'
              '[system]\nsystemd = []\n[files]\nread = []\nwrite = []\ndeny = []\n'}
    bootstrap = BOOTSTRAP
    for key, value in {"HOME": str(home), "QUESTION": QUESTION, "POLICY_MARKER": POLICY_MARKER,
                       "REFUSAL_MARKER": REFUSAL_MARKER, "NATIVE_MARKER": NATIVE_MARKER}.items():
        bootstrap = bootstrap.replace("__" + key + "__", repr(value))
    source["web-fixture.py"] = bootstrap
    for name, content in source.items():
        _new_file(home / name, content)
    return {"home": home, "script": home / "web-fixture.py", "source": source}


def command(fixture, *args):
    return ["/usr/bin/python3", "-I", "-B", str(fixture["script"]), *args]


def check_state(fixture):
    home = fixture["home"]
    for name, content in fixture["source"].items():
        path = home / name
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or path.read_text(encoding="utf-8") != content):
            raise ValueError("fixture-changed")
    actual = set()
    for path in home.rglob("*"):
        relative = path.relative_to(home).as_posix()
        if path.is_symlink():
            raise ValueError("unexpected-link")
        if path.is_dir():
            if relative not in (".config", ".config/ai-desktop"):
                raise ValueError("unexpected-directory")
        else:
            actual.add(relative)
    if actual != set(fixture["source"]):
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
                os.write(master, b"nie\n")
                answered = True
            if process.poll() is not None and not select.select([master], [], [], .1)[0]:
                break
        process.wait(timeout=2)
        if (process.returncode != 2 or not answered or REFUSAL_MARKER.encode() not in output
                or NATIVE_MARKER.encode() in output):
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
        print("HOST WEB: REFUSED (explicit flag, desktop UID and uncaptured TTY required)")
        return 2
    stage, started, passed = "preflight", False, False
    try:
        uid, units = prior.preflight()
        with tempfile.TemporaryDirectory(prefix="ai-web-policy-", dir=pwd.getpwuid(uid).pw_dir) as home:
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
            prior.check_stopped(uid, units)
            stage = "native-web"
            print("Test używa syntetycznej polityki: tylko Claude i WebSearch; Twoja konfiguracja pozostaje nietknięta.")
            print("Jedna zgoda obejmuje dobór źródła i wyszukiwanie; dwa zapytania Claude, bez danych lokalnych.")
            started = True
            prior.run(*command(fixture), timeout=600)
            stage = "source-unchanged"
            check_state(fixture)
            stage = "cleanup"
            prior.check_stopped(uid, units)
            stage = "fixture-cleanup"
        passed = True
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError, KeyboardInterrupt):
        print("HOST WEB: FAIL stage=" + stage)
    finally:
        if started:
            try:
                prior.check_stopped(uid, units)
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
                passed = False
                print("HOST WEB: FAIL stage=cleanup; use ai-desktop stop")
    print("HOST WEB: " + ("PASS policy=PASS upfront-refusal=PASS question-approvals=1 native-requests=2 "
        "local-reads=0 web-proof=PASS sources=PASS source-unchanged=PASS cleanup=PASS actual-user-policy=NOT-TESTED"
        if passed else "FAIL"))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
