"""Real installed analysis chain, synthetic client, only disposable Ubuntu CI.

Runs after ci_e2_systemd_activation.py provisioned the isolated account. Never
run on a developer's host: explicit flag + environment + installed-code check.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import pty
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time

import ci_e2_systemd_activation as prior
import host_e2_activated_worker as host
import host_file_access_acceptance as file_fixture
import host_ask_edit_acceptance as edit_fixture
import host_home_policy_acceptance as home_fixture
import host_web_acceptance as web_fixture
from test_report_policy import report_fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_desktop import analysis_service as service, analysis_admin, analysis_session as daily, analysis_session_admin as session_admin
from ai_desktop.analysis_contract import AnalysisConsent, AnalysisDenied
from ai_desktop.terminal_consent import PROMPTS

NAMES = ("ai-desktop-analysis@.socket", "ai-desktop-analysis@.service")
SYSTEMD_READ_MARKER = "SYNTHETIC SYSTEMD COLLECT"
FILE_READ_MARKER = "SYNTHETIC FILE READ"
ASK_SEND_MARKER = "SYNTHETIC ASK SEND"
ASK_SESSION_MARKER = "SYNTHETIC ASK SESSION"
SHIM = r'''
#include <stdlib.h>
#include <unistd.h>
int main(int argc, char **argv) {
    char **args = calloc((size_t)argc + 4, sizeof(char *));
    if (!args) return 2;
    args[0] = "/usr/bin/python3"; args[1] = "-I"; args[2] = "-B";
    args[3] = "/usr/lib/ai-desktop/fixture-client.py";
    for (int i = 1; i < argc; i++) args[i + 3] = argv[i];
    execv(args[0], args);
    return 2;
}
'''


def require_ci():
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux"
            or os.geteuid() <= 0 or os.getuid() != os.geteuid()
            or Path("/proc/1/comm").read_text().strip() != "systemd"):
        raise ValueError("ci-only")
    release = Path("/etc/os-release").read_text().splitlines()
    if "ID=ubuntu" not in release or 'VERSION_ID="24.04"' not in release:
        raise ValueError("platform")
    if pwd.getpwnam("ai-desktop").pw_uid == os.geteuid():
        raise ValueError("identity")
    host._installed_matches()
    for name in ("claude-code", "fixture-client.py", "analysis-worker.py", "analysis-client.py", "analysis-session.py"):
        if os.path.lexists(prior.DEST / name):
            raise ValueError("existing-runtime")
    for name in NAMES:
        if os.path.lexists(prior.UNITS / name):
            raise ValueError("existing-unit")
    if os.path.lexists(service.DIRECTORY):
        raise ValueError("existing-endpoint")


def install(temp, file_home):
    source = temp / "shim.c"
    source.write_text(SHIM)
    prior._run("/usr/bin/cc", "-Wall", "-Werror", "-O2", str(source), "-o", str(temp / "client"))
    prior._run("sudo", "-n", "/usr/bin/install", "-m755", str(temp / "client"), str(prior.DEST / "claude-code"))
    prior._run("sudo", "-n", "/usr/bin/install", "-m644", str(ROOT / "tests/analysis_fixture_diagnostic.py"),
               str(prior.DEST / "fixture-diagnostic.py"))
    for name in ("analysis-worker.py", "analysis-client.py", "analysis-session.py"):
        prior._run("sudo", "-n", "/usr/bin/install", "-m644", str(ROOT / "packaging/systemd" / name), str(prior.DEST))
    for name in NAMES:
        prior._run("sudo", "-n", "/usr/bin/install", "-m644", str(ROOT / "packaging/systemd" / name), str(prior.UNITS / name))
    prior._run("sudo", "-n", "/usr/bin/systemctl", "daemon-reload")
    # The report is synthetic. Systemd collection remains real, marked before
    # the fixed command so refusal/preview can prove there was no read call.
    # CLI, consent, sudo, root lifecycle, PID1, IPC and isolation are production.
    file_data = temp / "file-data"
    file_data.mkdir(mode=0o700)
    fixture = file_fixture.create_fixture(file_home, file_data, systemd=True)
    fixture["ci_control"] = Path(file_home) / "ci-ask-mode"
    file_fixture._new_file(fixture["ci_control"], "normal")
    expected = {"question": file_fixture.QUESTION, "marker": file_fixture.FINAL_MARKER,
                "files": {name: {"bytes": len(content.encode("utf-8")),
                                  "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
                          for name, content in file_fixture.FILE_CONTENTS.items()}}
    template = (ROOT / "tests/analysis_fixture_client.py").read_text()
    if template.count("FILE_FIXTURE = None\n") != 1:
        raise ValueError("fixture-template")
    client = temp / "fixture-client.py"
    edit_expected = {"question": edit_fixture.QUESTION, "files": edit_fixture.FILES,
                     "updated": edit_fixture.EDITED_MAIN, "marker": edit_fixture.TAIL_MARKER}
    if template.count("EDIT_FIXTURE = None\n") != 1:
        raise ValueError("edit-fixture-template")
    home_expected = {"question": home_fixture.QUESTION, "context": home_fixture.CONTEXT,
                     "created": home_fixture.CREATED, "marker": home_fixture.MARKER}
    if template.count("HOME_FIXTURE = None\n") != 1:
        raise ValueError("home-fixture-template")
    if template.count("WEB_FIXTURE = None\n") != 1:
        raise ValueError("web-fixture-template")
    client.write_text(template.replace("FILE_FIXTURE = None\n", "FILE_FIXTURE = " + repr(expected) + "\n")
                     .replace("EDIT_FIXTURE = None\n", "EDIT_FIXTURE = " + repr(edit_expected) + "\n")
                     .replace("HOME_FIXTURE = None\n", "HOME_FIXTURE = " + repr(home_expected) + "\n")
                     .replace("WEB_FIXTURE = None\n", "WEB_FIXTURE = " + repr({"question": web_fixture.QUESTION}) + "\n"))
    prior._run("sudo", "-n", "/usr/bin/install", "-m644", str(client), str(prior.DEST / "fixture-client.py"))
    ui = temp / "fixture-ui.py"
    ui.write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0, '/usr/lib/ai-desktop')\n"
                  "from ai_desktop import cli, analysis_cli, ask_cli, systemd_context, permissions, agent_profiles\n"
                  "analysis_cli.diagnose.collect = lambda: " + repr(report_fixture()) + "\n"
                  "permissions._home = lambda uid: " + repr(str(file_home)) + "\n"
                  "profile_store = agent_profiles.Store\n"
                  "agent_profiles.Store = lambda: profile_store(home=Path(" + repr(str(file_home)) + "))\n"
                  "original_read = permissions.read_selected\n"
                  "def read(*args, **kwargs):\n"
                  "    print(" + repr(FILE_READ_MARKER) + ", flush=True)\n"
                  "    return original_read(*args, **kwargs)\n"
                  "permissions.read_selected = read\n"
                  "scenario = Path(" + repr(str(fixture["ci_control"])) + ").read_text()\n"
                  "assert scenario in ('normal', 'revoke-after-planner')\n"
                  "send_count = 0\n"
                  "original_send, original_session = ask_cli.send_once, ask_cli.managed_session\n"
                  "def send(*args, **kwargs):\n"
                  "    global send_count\n"
                  "    send_count += 1\n"
                  "    print(" + repr(ASK_SEND_MARKER) + ", flush=True)\n"
                  "    answer = original_send(*args, **kwargs)\n"
                  "    if scenario == 'revoke-after-planner' and send_count == 1:\n"
                  "        with Path(" + repr(str(fixture["policy"])) + ").open('a') as policy:\n"
                  "            policy.write('# SYNTHETIC changed after planner\\n')\n"
                  "    return answer\n"
                  "def session(*args, **kwargs):\n"
                  "    print(" + repr(ASK_SESSION_MARKER) + ", flush=True)\n"
                  "    return original_session(*args, **kwargs)\n"
                  "ask_cli.send_once, ask_cli.managed_session = send, session\n"
                  "original_collect = systemd_context.collect\n"
                  "def collect(*args, **kwargs):\n"
                  "    print(" + repr(SYSTEMD_READ_MARKER) + ", flush=True)\n"
                  "    return original_collect(*args, **kwargs)\n"
                  "systemd_context.collect = collect\n"
                  "raise SystemExit(cli.main())\n")
    prior._run("sudo", "-n", "/usr/bin/install", "-m644", str(ui), str(prior.DEST / "fixture-ui.py"))
    return fixture


def mode(temp, value):
    (temp / "mode").write_text(value)
    prior._run("sudo", "-n", "/usr/bin/install", "-o", "ai-desktop", "-g", "ai-desktop", "-m600",
               str(temp / "mode"), "/var/lib/ai-desktop/claude/fixture-mode")
    prior._run("sudo", "-n", "/usr/bin/rm", "-f", "/var/lib/ai-desktop/claude/fixture-hanging")


def daily_clean(uid):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            session_admin.cleaned(uid)
            values = session_admin.properties(session_admin.manager_unit(uid))
            if values.get("ActiveState") in ("inactive", "failed") and values.get("MainPID") == "0":
                prior._run("sudo", "-n", "/usr/bin/test", "!", "-e", str(session_admin.claim_path(uid)))
                return
        except (AnalysisDenied, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(.1)
    raise ValueError("daily-cleanup")


def daily_pty(action, *, expected=0, refusal=False, interrupt=None, reason=None,
              write_refusal=False, entrypoint=None):
    asking = action[:1] == ["ask"] and "--preview" not in action
    analyzing = action[:1] == ["analyze"] and "--preview" not in action
    master, slave = pty.openpty()
    command = (["/usr/bin/python3", "-I", "-B", str(prior.DEST / "fixture-ui.py"), *action]
               if entrypoint is None else ["/usr/bin/python3", "-I", "-B", str(entrypoint)])
    process = subprocess.Popen(command,
                               stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True,
                               env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
    os.close(slave)
    output, answered, interrupted, wrote = bytearray(), False, False, False
    transfers = 0
    try:
        deadline = time.monotonic() + 65
        while time.monotonic() < deadline and len(output) <= 65536:
            if select.select([master], [], [], .1)[0]:
                try:
                    raw = os.read(master, 8192)
                except OSError:
                    break
                if not raw:
                    break
                output.extend(raw)
            transfer_prompt = PROMPTS["ask-question"] if asking else PROMPTS["transfer"]
            if asking and any(PROMPTS[name].encode() in output
                              for name in ("ask-transfer", "systemd-read", "files-read", "weather")):
                raise ValueError("ask-extra-consent")
            if output.count(transfer_prompt.encode()) > transfers:
                if asking and transfers:
                    raise ValueError("ask-repeated-consent")
                transfers += 1
                os.write(master, b"nie\n" if refusal else b"tak\n")
                answered = True
            if any(PROMPTS[action].encode() in output for action in ("write", "rollback")) and not wrote:
                os.write(master, b"nie\n" if write_refusal else b"tak\n")
                wrote = True
            if interrupt and not interrupted and any(marker in output for marker in
                    (b"Analiza trwa;", "Czekam na odpowiedź".encode())):
                marker = subprocess.run(["sudo", "-n", "/usr/bin/test", "-e",
                                         "/var/lib/ai-desktop/claude/fixture-hanging"],
                                        check=False, capture_output=True, timeout=5)
                if marker.returncode == 0:
                    if interrupt == "manager-kill":
                        prior._run("sudo", "-n", "/usr/bin/systemctl", "kill", "--kill-whom=main",
                                   "--signal=SIGKILL", session_admin.manager_unit(os.geteuid()))
                    else:
                        os.kill(process.pid, signal.SIGKILL if interrupt == "ui-kill" else signal.SIGINT)
                    interrupted = True
            if process.poll() is not None and not select.select([master], [], [], .1)[0]:
                break
        process.wait(timeout=3)
        if (process.returncode != expected
                or ((analyzing or asking) and not answered) or (asking and transfers != 1)
                or (interrupt and not interrupted) or b"SYNTHETIC-PRIVATE" in output
                or (reason and ("reason=" + reason).encode() not in output)):
            # This is synthetic CI output only, and only our fixed diagnostics.
            for line in output.splitlines():
                if line.startswith((b"ANALYSIS:", b"ASK:", b"SESSION:", b"SESSION1 ", b"STATUS:", b"TASK",
                                    b"Odmowa", b"Nie uda", b"Wyczerpano", b"Wynik lokalny", b"Tre\xc5\x9b\xc4\x87 plik")):
                    print(line.decode("ascii", "replace")[:120])
            for line in [l for l in output.splitlines() if l.strip()][-6:]:
                print("CI TAIL: " + line.decode("ascii", "replace")[:140])        # synthetic CI output only
            print("CI UI: consent=" + str(answered) + " sudo=" + str(b"Uprawnienia administratora" in output)
                  + " transfer=" + str(b"Analiza trwa;" in output) + " exit=" + str(process.returncode))
            raise ValueError("daily-pty")
        if analyzing:
            if "--details" in action and (expected == 0) != (b"ANALYSIS: PASS" in output):
                raise ValueError("daily-result")
            if refusal and b"Uprawnienia administratora" in output:
                raise ValueError("sudo-before-consent")
        daily_clean(os.geteuid())
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def compact_analysis(output, question):
    """A concise result must retain the exact report/question and consent."""
    required = (b"Raport do analizy:", b'"kernel": "7.3.0-4-cachyos"', b"Pytanie:",
                question.encode(), b"SYNTHETIC ANALYSIS: isolation=PASS proxy-denial=PASS profile=PASS")
    forbidden = (b"INSTRUKCJA APLIKACJI", b"ZASADY I ODBIORCA", b"ANALYSIS: PASS",
                 b"SESSION: STOPPED", b"SHA256", b"\x1b[31m")
    if (any(item not in output for item in required) or any(item in output for item in forbidden)
            or output.count(PROMPTS["transfer"].encode()) != 1
            or re.search(rb"\b[0-9a-f]{64}\b", output)):
        raise ValueError("compact-analysis")


def compact_task(output, *, rollback=False):
    """A short task exposes the full fenced diff and separate decisions."""
    if rollback:
        required = ("Cofnięto zmiany.".encode(), PROMPTS["rollback"].encode())
    else:
        required = (b"=== Zmiany do zapisania ===", b"```diff\n", b"\n```\n", b"--- a/calc.py",
                    b"+++ b/calc.py", b"-    return a - b", b"+    return a + b",
                    b"Zapisano zmiany.", PROMPTS["transfer"].encode(), PROMPTS["write"].encode())
    forbidden = (b"INSTRUKCJA APLIKACJI", b"ZASADY I ODBIORCA", b"Nowa tresc JSON:",
                 b"TASK:", b"TASK TEST:", b"TASK ROLLBACK:", b"SESSION: STOPPED", b"SHA256",
                 b"Sprawdzam zmiany", b"Test na kopii: zaliczony", b"Test obejmuje", b"Zapis obejmie")
    output = output.replace(b"\r\n", b"\n")
    if (any(item not in output for item in required) or any(item in output for item in forbidden)
            or re.search(rb"\b[0-9a-f]{64}\b", output)):
        raise ValueError("compact-task")
    prompts = ("rollback",) if rollback else ("transfer", "write")
    if any(output.count(PROMPTS[prompt].encode()) != 1 for prompt in prompts):
        raise ValueError("compact-task-consent")
    if not rollback and not (output.index(PROMPTS["transfer"].encode())
                            < output.index(b"```diff\n") < output.index(b"--- a/calc.py")
                            < output.index(b"\n```\n") < output.index(PROMPTS["write"].encode())):
        raise ValueError("compact-task-order")


def ask_cycle(temp, fixture):
    """Installed ask and bounded systemd inventory; no external providers."""
    action = ["ask", "--question", "SYNTHETIC: general question"]
    output = daily_pty(action, expected=2, refusal=True)
    if any(marker in output for marker in (b"SYNTHETIC ASK:", b"Uprawnienia administratora",
               ASK_SEND_MARKER.encode(), ASK_SESSION_MARKER.encode(), FILE_READ_MARKER.encode(), SYSTEMD_READ_MARKER.encode())):
        raise ValueError("ask-refusal")
    for scenario, reason in (("expired", "auth-required"), ("network-failure", "client"),
                            ("api-auth", "auth-required"), ("api-rate", "rate-limit"),
                            ("api-unavailable", "provider-unavailable")):
        mode(temp, scenario)
        output = daily_pty(action + ["--details"], expected=2, reason=reason)
        if b"ASK: PASS" in output or b"SYNTHETIC ASK:" in output:
            raise ValueError("ask-error-success")
    mode(temp, "success")
    # Exercise the ordinary no-config question without granting any source.
    # Only the disposable fixture policy is moved; the desktop policy is never
    # consulted by this installed test-only entrypoint.
    saved_policy = fixture["policy"].with_suffix(".saved")
    if saved_policy.exists():
        raise ValueError("fixture-policy-backup-exists")
    fixture["policy"].rename(saved_policy)
    try:
        output = daily_pty(["ask", "--question", "SYNTHETIC: unconfigured question"])
    finally:
        saved_policy.rename(fixture["policy"])
    file_fixture.check_unchanged(fixture)
    if (b"SYNTHETIC ASK: no-config=PASS sources=none isolation=PASS" not in output
            or output.count(ASK_SEND_MARKER.encode()) != 1
            or output.count(ASK_SESSION_MARKER.encode()) != 1
            or FILE_READ_MARKER.encode() in output or SYSTEMD_READ_MARKER.encode() in output):
        raise ValueError("ask-result")
    if any(marker in output for marker in (b"INSTRUKCJA APLIKACJI", b"ZASADY I ODBIORCA",
                                          b"TWOJE PYTANIE", b"ASK: PASS", b"SESSION: STOPPED",
                                          "Dostępy:".encode(), b"Do Claude:",
                                          b"SHA256", b"SYNTHETIC-PRIVATE", b"\x1b[31m")):
        raise ValueError("ask-output")
    systemd_action = ["ask", "--question", "SYNTHETIC: systemd question"]
    for options, expected, refusal in ((["--preview"], 0, False), ([], 2, True)):
        output = daily_pty(systemd_action + options, expected=expected, refusal=refusal)
        if (any(marker in output for marker in (SYSTEMD_READ_MARKER.encode(), b"SYNTHETIC ASK:",
                                               ASK_SEND_MARKER.encode(), ASK_SESSION_MARKER.encode()))
                or output.count(PROMPTS["ask-question"].encode()) != (1 if refusal else 0)):
            raise ValueError("ask-systemd-read-before-consent")
    output = daily_pty(systemd_action)
    if (b"SYNTHETIC ASK: systemd=PASS context=validated isolation=PASS" not in output
            or output.count(SYSTEMD_READ_MARKER.encode()) != 1
            or output.count(ASK_SEND_MARKER.encode()) != 2
            or output.count(ASK_SESSION_MARKER.encode()) != 2
            or "Dostępy:".encode() in output or "Odczyt systemd".encode() in output):
        raise ValueError("ask-systemd-context")
    print("CI ASK: PASS upfront-refusal=PASS question=PASS no-config-sources=NONE compact=PASS systemd=PASS question-consent=ONE preview-no-read=PASS auth=PASS rate=PASS unavailable=PASS recovery=PASS cleanup=PASS native=false upstream=false")


def file_cycle(temp, fixture):
    """Read-only files through the installed chain; synthetic policy and AI."""
    action = ["ask", "--question", file_fixture.QUESTION]
    tail_marker = file_fixture.FINAL_MARKER.encode()
    for options, expected, refusal in ((["--preview"], 0, False), ([], 2, True)):
        output = daily_pty(action + options, expected=expected, refusal=refusal)
        if (any(marker in output for marker in (FILE_READ_MARKER.encode(), tail_marker, b"SYNTHETIC ASK:",
                                               ASK_SEND_MARKER.encode(), ASK_SESSION_MARKER.encode()))
                or output.count(PROMPTS["ask-question"].encode()) != (1 if refusal else 0)):
            raise ValueError("ask-files-read-before-consent")
    mode(temp, "success")
    output = daily_pty(action)
    if (b"SYNTHETIC ASK: files=5 bytes=12175 exact-content=PASS tail-marker=PASS" not in output
            or b"wire-budget=PASS no-host-mount=PASS" not in output or tail_marker not in output
            or output.count(FILE_READ_MARKER.encode()) != 1
            or output.count(ASK_SEND_MARKER.encode()) != 3        # planner, discovery list, final answer: one approval
            or output.count(ASK_SESSION_MARKER.encode()) != 3
            or any(marker in output for marker in ("Dostępy:".encode(), b"Odczyt lokalny", b"Do pytania:", b"Do Claude:"))
            or file_fixture.DENIED_CONTENTS.strip().encode() in output):
        raise ValueError("ask-files-context")
    file_fixture.check_unchanged(fixture)

    try:
        fixture["ci_control"].write_text("revoke-after-planner")
        output = daily_pty(action, expected=2)
        if (FILE_READ_MARKER.encode() in output or b"SYNTHETIC ASK:" in output
                or output.count(ASK_SEND_MARKER.encode()) != 1
                or output.count(ASK_SESSION_MARKER.encode()) != 1
                or fixture["policy"].read_text() == fixture["policy_text"]):
            raise ValueError("ask-files-policy-change")
    finally:
        fixture["ci_control"].write_text("normal")
        fixture["policy"].write_text(fixture["policy_text"])
    file_fixture.check_unchanged(fixture)
    print("CI FILE ACCESS: PASS synthetic-policy=PASS preview-no-read=PASS upfront-refusal=PASS "
          "question-consent=ONE deny=PASS files=5 bytes=12175 exact-content=PASS tail-marker=PASS "
          "wire-budget=PASS no-host-mount=PASS "
          "policy-change=REFUSED revoked-read=NONE source-unchanged=PASS cleanup=PASS native=false upstream=false")


def task_cycle():
    from ai_desktop import task_workspace
    with tempfile.TemporaryDirectory(prefix="ai-task-ci-", dir="/tmp") as root:
        path = Path(root)
        original = "def add(a, b):\n    return a - b\n"
        (path / "calc.py").write_text(original)
        (path / "test_calc.py").write_text("import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n")
        args = ["task", "run", "SYNTHETIC: fix addition", "--workspace", root,
                "--file", "calc.py", "--context", "test_calc.py", "--test", "python-unittest"]
        daily_pty(args, expected=2, refusal=True)
        if (path / "calc.py").read_text() != original or task_workspace.status(root):
            raise ValueError("task-send-refusal")
        diagnostic = daily_pty(args + ["--details"], expected=2, write_refusal=True)
        if b"TASK TEST: PASS check=python-unittest" not in diagnostic or b"Nowa tresc JSON:" not in diagnostic:
            raise ValueError("task-diagnostics")
        if (path / "calc.py").read_text() != original or task_workspace.status(root):
            raise ValueError("task-write-refusal")
        compact_task(daily_pty(args))
        records = task_workspace.status(root)
        if len(records) != 1 or records[0][1] != "applied" or (path / "calc.py").read_text() == original:
            raise ValueError("task-apply")
        compact_task(daily_pty(["task", "rollback", "--workspace", root]), rollback=True)
        if (path / "calc.py").read_text() != original or task_workspace.status(root)[0][1] != "rolled-back":
            raise ValueError("task-undo")
        print("CI TASK: PASS transfer-refusal=PASS write-refusal=PASS test=PASS apply=PASS rollback=PASS compact=PASS details=PASS cleanup=PASS")


def large_task_cycle():
    """Real framed IPC + namespace check + journal for >8 files / >32KiB payloads."""
    from ai_desktop import task_workspace
    with tempfile.TemporaryDirectory(prefix="ai-large-task-ci-", dir="/tmp") as root:
        path = Path(root)
        settings = {"enabled": False, **{"p" + str(i): "padding-value" for i in range(1500)},
                    "tail": "SYNTHETIC_LARGE_TAIL"}
        original = json.dumps(settings, indent=2) + "\n"
        if not 32768 < len(original.encode()) < 65536:
            raise ValueError("large-fixture")
        (path / "settings.json").write_text(original)
        args = ["task", "run", "SYNTHETIC: large JSON edit", "--workspace", root,
                "--file", "settings.json", "--file", "created.json", "--test", "json"]
        for i in range(12):
            name = "context-" + str(i) + ".json"
            (path / name).write_text('{"context": true}\n')
            args.extend(["--context", name])
        output = daily_pty(args)
        if "Zapisano zmiany.".encode() not in output:
            raise ValueError("large-apply-output")
        expected = original.replace('"enabled": false', '"enabled": true')
        if ((path / "settings.json").read_text() != expected
                or (path / "created.json").read_text() != '{"created": true}\n'):
            raise ValueError("large-content")
        daily_pty(["task", "rollback", "--workspace", root])
        if ((path / "settings.json").read_text() != original or (path / "created.json").exists()
                or task_workspace.status(root)[0][1] != "rolled-back"):
            raise ValueError("large-rollback")
        print("CI LARGE TASK: PASS files=14 request-over32KiB=PASS response-over32KiB=PASS "
              "json-test=PASS edit=PASS create=PASS rollback=PASS cleanup=PASS native=false")


def ask_edit_cycle():
    """Run the installed ask bridge and host harness against the synthetic client."""
    from ai_desktop import task_workspace
    actual_home = pwd.getpwuid(os.geteuid()).pw_dir
    with tempfile.TemporaryDirectory(prefix="ai-ask-edit-ci-", dir=actual_home) as home:
        data = Path(home) / "data"
        data.mkdir(mode=0o700)
        fixture = edit_fixture.create_fixture(home, data)
        checked = subprocess.run(edit_fixture.command(fixture, "policy-check"),
            capture_output=True, timeout=10, close_fds=True,
            env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
        if checked.returncode or checked.stdout.strip() != edit_fixture.POLICY_MARKER.encode() or checked.stderr:
            raise ValueError("ask-edit-policy")
        output = daily_pty(["ask"], entrypoint=fixture["script"])
        if edit_fixture.NATIVE_MARKER.encode() not in output:
            raise ValueError("ask-edit-proof")
        edit_fixture.check_state(fixture, applied=True)
        daily_pty(["task", "rollback", "--workspace", str(fixture["project"])])
        edit_fixture.check_state(fixture)
        if task_workspace.status(str(fixture["project"]))[0][1] != "rolled-back":
            raise ValueError("ask-edit-rollback")
        print("CI ASK EDIT: PASS real-policy=PASS question-approvals=1 write-approvals=1 "
              "requests=2 read-files=9 request-over32KiB=PASS test=PASS apply=PASS rollback=PASS "
              "cleanup=PASS native=false oauth=false upstream=false")


def home_policy_cycle():
    """The native host fixture, with production HOME policy and synthetic client."""
    from ai_desktop import task_workspace
    actual_home = pwd.getpwuid(os.geteuid()).pw_dir
    with tempfile.TemporaryDirectory(prefix="ai-home-ci-", dir=actual_home) as home:
        fixture = home_fixture.create_fixture(home)
        checked = subprocess.run(home_fixture.command(fixture, "policy-check"),
            capture_output=True, timeout=10, close_fds=True,
            env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
        if checked.returncode or checked.stdout.strip() != home_fixture.POLICY_MARKER.encode() or checked.stderr:
            raise ValueError("home-policy")
        home_fixture.reject(fixture)
        home_fixture.check_state(fixture)
        if task_workspace.status(home):
            raise ValueError("home-refusal-wrote-journal")
        daily_clean(os.geteuid())
        output = daily_pty(["ask"], entrypoint=fixture["script"])
        if home_fixture.NATIVE_MARKER.encode() not in output:
            raise ValueError("home-proof")
        home_fixture.check_state(fixture, applied=True)
        records = task_workspace.status(home)
        if len(records) != 1 or records[0][1] != "applied":
            raise ValueError("home-journal")
        daily_pty(["task", "rollback", "--workspace", home])
        home_fixture.check_state(fixture)
        if task_workspace.status(home) != [(records[0][0], "rolled-back")]:
            raise ValueError("home-rollback")
    print("CI HOME: PASS real-policy=PASS deny-before-read=PASS upfront-refusal=PASS "
          "question-approvals=1 write-approvals=1 requests=2 read-files=1 unselected=80 denied=2 "
          "test=PASS diff=PASS create=PASS rollback=PASS cleanup=PASS native=false oauth=false upstream=false")


def web_cycle(temp):
    """Same opt-in fixture, real installed isolation, synthetic search events."""
    actual_home = pwd.getpwuid(os.geteuid()).pw_dir
    with tempfile.TemporaryDirectory(prefix="ai-web-ci-", dir=actual_home) as home:
        fixture = web_fixture.create_fixture(home)
        checked = subprocess.run(web_fixture.command(fixture, "policy-check"),
            capture_output=True, timeout=10, close_fds=True,
            env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
        if checked.returncode or checked.stdout.strip() != web_fixture.POLICY_MARKER.encode() or checked.stderr:
            raise ValueError("web-policy")
        web_fixture.reject(fixture)
        web_fixture.check_state(fixture)
        daily_clean(os.geteuid())
        mode(temp, "success")
        output = daily_pty(["ask"], entrypoint=fixture["script"])
        if (web_fixture.NATIVE_MARKER.encode() not in output or b"SYNTHETIC WEB: metadata=PASS" not in output
                or b"https://www.python.org/downloads/" not in output):
            raise ValueError("web-proof")
        web_fixture.check_state(fixture)
        for scenario in ("web-no-results", "web-forged-source", "web-foreign-tool"):
            mode(temp, scenario)
            output = daily_pty(["ask"], entrypoint=fixture["script"], expected=2)
            if web_fixture.NATIVE_MARKER.encode() in output or b"https://" in output or b"SYNTHETIC WEB:" in output:
                raise ValueError("web-invalid-evidence-released")
            web_fixture.check_state(fixture)
        mode(temp, "success")
    print("CI WEB: PASS real-policy=PASS upfront-refusal=PASS question-approvals=1 sessions=2 "
          "local-reads=0 observed-sources=PASS no-results=REFUSED forged-source=REFUSED foreign-tool=REFUSED "
          "cleanup=PASS native=false oauth=false upstream=false")


def concurrent_session():
    """A rejected second launch must not stop the first invocation's socket."""
    argv = ["sudo", "-n", "/usr/bin/python3", "-I", "-B", str(session_admin.BOOTSTRAP), "open"]
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    session = daily.Session(proc)
    try:
        session.start()
        before = service.endpoint(os.geteuid())
        second = subprocess.run(argv, input=b"", capture_output=True, timeout=10, check=False)
        if (second.returncode != 2 or b"SESSION1 REFUSED busy" not in second.stdout
                or service.endpoint(os.geteuid()) != before):
            raise ValueError("concurrent-session")
    finally:
        session.close()
    daily_clean(os.geteuid())


def explicit_stop_session():
    """The public stop command stops a claimed session and is then idempotent."""
    proc = subprocess.Popen(daily.helper_command("open"), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    session = daily.Session(proc)
    try:
        session.start()
        service.endpoint(os.geteuid())
        daily_pty(["stop"])
    finally:
        try:
            session.close()
        except AnalysisDenied as exc:
            # External stop may make systemd-run report the helper's SIGTERM;
            # it must not turn the interrupted analysis into success.
            if exc.reason != "cleanup":
                raise
    daily_clean(os.geteuid())
    daily_pty(["stop"])


def wait_inactive(unit):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if host._property(unit, "ActiveState") in ("inactive", "failed"):
            return
        time.sleep(.1)
    raise ValueError("still-running")


def authorize():
    report = report_fixture()
    gate = AnalysisConsent(report)
    return gate.authorize(report, sha256=gate.sha256, nonce=gate.nonce)


def login_pty(*, accepted, details=True):
    """Public user CLI -> sudo -> admin -> PID1 -> synthetic interactive ELF."""
    command = ["/usr/bin/python3", "-I", "-B", str(prior.DEST / "fixture-ui.py"),
               "login", "--allow-claude-login"] + (["--v"] if details else [])
    master, slave = pty.openpty()
    process = None
    output, answered, unit = bytearray(), False, None
    try:
        process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                   close_fds=True, start_new_session=True,
                                   env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"})
        os.close(slave)
        slave = None
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and len(output) <= 65536:
            if select.select([master], [], [], .1)[0]:
                try:
                    data = os.read(master, 8192)
                except OSError:
                    break
                if not data:
                    break
                output.extend(data)
            match = re.search(rb"Running as unit: (ai-desktop-login-[a-f0-9]{16}\.service)", output)
            if match:
                unit = match[1].decode("ascii")
            if b"SYNTHETIC LOGIN INPUT:" in output and not answered:
                os.write(master, b"SYNTHETIC\n")
                answered = True
            if process.poll() is not None and not select.select([master], [], [], .1)[0]:
                break
        process.wait(timeout=2)
        success = (b"LOGIN: PASS auth=claude.ai" if details else
                   "Zalogowano. Aktywna usługa: Claude.".encode("utf-8"))
        if (not answered or (details and unit is None) or process.returncode != (0 if accepted else 2)
                or (accepted and success not in output)
                or (not accepted and "Zalogowano.".encode("utf-8") in output)
                or (not accepted and b"LOGIN: REFUSED stage=auth-status reason=auth-method" not in output)):
            # Synthetic fixture only, but still expose only fixed diagnostic lines.
            for match in re.findall(rb"(?:LOGIN|CLIENT): REFUSED stage=[a-z-]+ reason=[a-z-]+", output):
                print(match.decode("ascii"))
            raise ValueError("login-pty")
        if details:
            state = subprocess.run(["/usr/bin/systemctl", "show", unit,
                                    "--property=LoadState,ActiveState,MainPID"],
                                   capture_output=True, text=True, check=False, timeout=5)
            values = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
            if (values.get("LoadState") != "not-found"
                    and (values.get("MainPID") != "0" or values.get("ActiveState") not in ("inactive", "failed"))):
                raise ValueError("login-cleanup")
        else:
            if any(value in output for value in (b"Running as unit:", b"disconnect TTY", b"Finished with result:",
                                                 b"Service runtime:", b"Memory peak:", b"LOGIN: PASS")):
                raise ValueError("login-quiet-output")
            # This fixture owns the disposable runner. Quiet operation hides
            # names from the terminal, so check the complete synthetic prefix.
            state = subprocess.run(["/usr/bin/systemctl", "list-units", "--no-legend", "--plain",
                                    "--state=active,activating,deactivating,reloading",
                                    "ai-desktop-login-*.service"],
                                   capture_output=True, text=True, check=False, timeout=5)
            if state.returncode != 0 or state.stdout.strip():
                raise ValueError("login-cleanup")
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        if unit is not None:
            subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "stop", unit],
                           check=False, capture_output=True, timeout=10)
        os.close(master)
        if slave is not None:
            os.close(slave)


def main(argv=None):
    if (sys.argv[1:] if argv is None else argv) != ["--allow-disposable-analysis-test"]:
        print("CI ANALYSIS: REFUSED (explicit disposable-CI flag required)")
        return 2
    stage, installed, started, passed = "preflight", False, False, False
    try:
        require_ci()
        uid = os.geteuid()
        units = ["ai-desktop-analysis@" + str(uid) + suffix for suffix in (".socket", ".service")]
        with tempfile.TemporaryDirectory() as temp, \
                tempfile.TemporaryDirectory(prefix="ai-file-policy-ci-", dir=pwd.getpwuid(uid).pw_dir) as file_home:
            temp = Path(temp)
            stage = "install"
            fixture = install(temp, file_home)
            installed = True
            stage = "login-pty"
            mode(temp, "login")
            login_pty(accepted=True)
            stage = "login-pty-quiet"
            login_pty(accepted=True, details=False)
            stage = "login-wrong-auth"
            mode(temp, "wrong-auth")
            login_pty(accepted=False)
            stage = "login-wrong-auth-quiet"
            login_pty(accepted=False, details=False)
            mode(temp, "success")
            started = True
            prior._run("sudo", "-n", "/usr/bin/systemctl", "start", units[0])
            stage = "approved-analysis"
            service.local_readiness()
            result = service.send_once(authorize())
            if result != "SYNTHETIC ANALYSIS: isolation=PASS proxy-denial=PASS profile=PASS\n\\u001b[31m":
                raise ValueError("unexpected-result")
            wait_inactive(units[1])
            stage = "wrong-auth-method"
            mode(temp, "wrong-auth")
            try:
                service.send_once(authorize())
            except AnalysisDenied as exc:
                if exc.reason != "auth-method":
                    raise
            else:
                raise ValueError("auth-accepted")
            wait_inactive(units[1])
            stage = "malformed-request"
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as conn:
                conn.settimeout(5)
                conn.connect(service.socket_path(uid))
                service.manager_peer(conn, uid)
                if service.packet(conn) != service.READY:
                    raise ValueError("not-ready")
                conn.send(b'{"report":"invalid"}')
                try:
                    body = conn.recv(100)
                except ConnectionResetError:
                    body = b""
                if body:
                    raise ValueError("malformed-accepted")
            prior._run("sudo", "-n", "/usr/bin/systemctl", "stop", *units)
            started = False
            stage = "daily-status"
            daily_pty(["status"])
            stage = "daily-preview"
            daily_pty(["analyze", "--preview"])
            stage = "daily-refusal"
            daily_pty(["analyze", "--details"], expected=2, refusal=True, reason="consent")
            stage = "daily-success"
            mode(temp, "success")
            question = "SYNTHETIC: analysis question"
            compact_analysis(daily_pty(["analyze", "--question", question]), question)
            print("CI ANALYSIS UX: PASS compact=PASS report=PASS question=PASS consent=PASS cleanup=PASS")
            stage = "daily-ask"
            ask_cycle(temp, fixture)
            stage = "daily-files"
            file_cycle(temp, fixture)
            stage = "task-cycle"
            task_cycle()
            stage = "large-task"
            large_task_cycle()
            stage = "ask-edit"
            ask_edit_cycle()
            stage = "home-policy"
            home_policy_cycle()
            stage = "web-search"
            web_cycle(temp)
            for fixture, reason in (("expired", "auth-required"), ("network-failure", "client")):
                stage = "daily-" + fixture
                mode(temp, fixture)
                daily_pty(["analyze", "--details"], expected=2, reason=reason)
            for interrupt in ("interrupt", "ui-kill", "manager-kill"):
                stage = "daily-" + interrupt
                mode(temp, "hang")
                daily_pty(["analyze", "--details"], expected=-signal.SIGKILL if interrupt == "ui-kill" else 2, interrupt=interrupt)
            stage = "daily-concurrent"
            concurrent_session()
            stage = "daily-stop"
            explicit_stop_session()
            stage = "daily-recovery"
            mode(temp, "success")
            daily_pty(["analyze", "--details"])
            passed = True
    except (AnalysisDenied, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        # Only fixed vocabulary, never native stderr/status/profile content.
        detail = " reason=" + exc.reason if isinstance(exc, AnalysisDenied) else ""
        if (isinstance(exc, ValueError) and len(exc.args) == 1 and type(exc.args[0]) is str
                and re.fullmatch(r"[a-z0-9-]{1,48}", exc.args[0])):
            detail += " check=" + exc.args[0]            # a fixed slug raised by this script, never native output
        print("CI ANALYSIS: FAIL stage=" + stage + detail)
        if installed and stage.startswith("daily-"):
            # Fixed unit metadata only, never native stderr or profile.
            for unit in (*units, session_admin.manager_unit(uid)):
                result = subprocess.run(["/usr/bin/systemctl", "show", unit,
                                         "--property=ActiveState,SubState,Result,MainPID,ExecMainCode,ExecMainStatus"],
                                        env=session_admin.ENV, capture_output=True, timeout=10, check=False)
                print("CI UNIT: " + unit)
                for line in result.stdout.decode("ascii", "replace").splitlines():
                    if re.fullmatch(r"(?:ActiveState|SubState|Result|MainPID|ExecMainCode|ExecMainStatus)=[a-z0-9-]+", line):
                        print(line)
            result = subprocess.run(["sudo", "-n", "/usr/bin/journalctl", "--no-pager", "-n", "40", "-o", "json",
                                     "--output-fields=SYSLOG_IDENTIFIER,_PID,MESSAGE_ID,UNIT,JOB_TYPE,JOB_RESULT,EXIT_CODE,EXIT_STATUS,CODE_FUNC",
                                     "-u", units[0], "-u", units[1], "-u", session_admin.manager_unit(uid)],
                                    env=session_admin.ENV, capture_output=True, timeout=10, check=False)
            # Only manager event codes from this disposable synthetic fixture.
            # MESSAGE is intentionally absent, along with all client output.
            import json
            for raw in result.stdout.splitlines()[-40:]:
                value = json.loads(raw)
                if value.get("SYSLOG_IDENTIFIER") == "systemd" and value.get("_PID") == "1":
                    fields = {key: item for key, item in value.items()
                              if key in {"MESSAGE_ID", "UNIT", "JOB_TYPE", "JOB_RESULT", "EXIT_CODE", "EXIT_STATUS", "CODE_FUNC"}
                              and type(item) is str and re.fullmatch(r"[a-zA-Z0-9@_.:-]{1,100}", item)}
                    print("CI EVENT: " + json.dumps(fields, sort_keys=True))
        if installed and (stage in ("login-pty", "login-wrong-auth") or
                          (stage == "approved-analysis" and isinstance(exc, AnalysisDenied)
                           and exc.reason in ("version", "sandbox"))):
            # Offline version only, with our synthetic executable, no profile or
            # proxy mounted. The exact production service properties still apply.
            try:
                command = analysis_admin.login_command()[:-5]
                command = ["--unit=ai-desktop-ci-version" if arg.startswith("--unit=ai-desktop-login-")
                           else "--pipe" if arg == "--pty" else arg for arg in command]
                prior._run("sudo", "-n", *command, "/usr/bin/python3", "-I", "-B",
                           str(prior.DEST / "fixture-diagnostic.py"))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                print("CI SYNTHETIC VERSION: diagnostic unavailable")
    finally:
        if started:
            try:
                prior._run("/usr/bin/systemctl", "show", units[1],
                           "--property=ActiveState,Result,ExecMainCode,ExecMainStatus,MainPID")
                prior._run("sudo", "-n", "/usr/bin/systemctl", "stop", *units)
                if (os.path.lexists(service.socket_path(uid))
                        or host._property(units[1], "MainPID") != "0"
                        or any(host._property(unit, "ActiveState") not in ("inactive", "failed") for unit in units)):
                    raise ValueError("cleanup")
            except (OSError, ValueError, subprocess.SubprocessError):
                passed = False
                print("CI ANALYSIS: FAIL stage=cleanup")
    print("CI ANALYSIS: " + ("PASS manager=PID1 separate-uid=PASS mode-filter=PASS login-pty=PASS login-refusal=PASS result=PASS auth-refusal=PASS cleanup=PASS daily=PASS expired=PASS network-failure=PASS interrupt=PASS ui-kill=PASS manager-kill=PASS concurrent=PASS stop=PASS recovery=PASS native=false oauth=false upstream=false" if passed else "FAIL"))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
