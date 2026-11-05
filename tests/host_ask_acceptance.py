"""Opt-in CachyOS acceptance of general ask and explicitly selected context.

Never included in CI/LOCAL discovery. Native answers remain on the user's TTY;
the harness records only exit codes and service cleanup metadata. No login,
profile inspection, token changes, host-network changes or forced expiry.
"""
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import host_analysis_acceptance as prior
from ai_desktop.terminal_consent import PROMPTS

FLAG = "--allow-subscription-ask-test"
WEATHER_FLAG = "--allow-weather-test"


def reject_question():
    """Only reject a fixed harmless question; never approve native execution."""
    master, slave = pty.openpty()
    process = subprocess.Popen(
        ["/usr/bin/ai-desktop", "ask", "--question", "SYNTHETIC: refuse this question", "--no-context"],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True,
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
            if PROMPTS["ask-transfer"].encode() in output and not answered:
                os.write(master, b"no\n")
                answered = True
            if process.poll() is not None and not select.select([master], [], [], .1)[0]:
                break
        process.wait(timeout=2)
        if (process.returncode != 2 or not answered
                or b"Cancelled. The question was not sent." not in output
                or b"Administrator permissions" in output
                or b"Waiting for " in output):
            raise ValueError("ask-refusal")
        print("HOST ASK CONSENT: PASS no=REFUSED sudo=unused upstream=unused")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args not in ([FLAG], [FLAG, WEATHER_FLAG]):
        print("HOST ASK: REFUSED (explicit subscription flag; weather needs separate opt-in)")
        return 2
    if os.geteuid() <= 0 or not (os.isatty(0) and os.isatty(1)):
        print("HOST ASK: REFUSED (desktop UID and uncaptured TTY required)")
        return 2
    stage, started, passed = "preflight", False, False
    weather = WEATHER_FLAG in args
    try:
        uid, units = prior.preflight()
        stage = "refusal"
        reject_question()
        prior.check_stopped(uid, units)
        stage = "general"
        started = True
        prior.run("/usr/bin/ai-desktop", "ask", "--no-context", "--question",
                  "Wyjaśnij krótko różnicę pomiędzy plikiem a katalogiem.", timeout=420)
        prior.check_stopped(uid, units)
        stage = "systemd"
        prior.run("/usr/bin/ai-desktop", "ask", "--systemd", "both", "--systemd-state", "enabled",
                  "--question", "Pokaż włączone jednostki z otrzymanej listy systemd. "
                  "Odróżnij włączenie przy starcie od bieżącego działania i wskaż ograniczenia listy.", timeout=420)
        prior.check_stopped(uid, units)
        if weather:
            stage = "weather"
            prior.run("/usr/bin/ai-desktop", "ask", "--weather", "Warszawa", "--question",
                      "Jaka jest aktualna pogoda w Warszawie? Podaj czas danych i źródło.", timeout=420)
            prior.check_stopped(uid, units)
        passed = True
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError, KeyboardInterrupt):
        print("HOST ASK: FAIL stage=" + stage)
    finally:
        if started:
            try:
                # Do not manufacture success by stopping anything ourselves.
                prior.check_stopped(uid, units)
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
                passed = False
                print("HOST ASK: FAIL stage=cleanup; use ai-desktop stop")
    if passed:
        print("HOST ASK: PASS general=PASS systemd=PASS weather=" + ("PASS" if weather else "SKIP")
              + " refusal=PASS cleanup=PASS native-errors=NOT-TESTED")
    else:
        print("HOST ASK: FAIL")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
