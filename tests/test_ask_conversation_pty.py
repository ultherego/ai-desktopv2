"""`ai-desktop ask` as a terminal conversation, through a REAL pseudo-terminal and a stand-in provider.

Covers: the greeting appears once, every answer returns to `> `, arrow-key editing and recall, a declined turn that sends
nothing and does not close the conversation, Ctrl-C during a wait that cancels only that turn, idle time that makes no
request, a double Ctrl-D exit, and the terminal settings restored afterwards. A scripted `reader` is not enough for this."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import fcntl
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import termios
import time
import unittest

from ai_desktop.terminal_consent import PROMPTS

ROOT = Path(__file__).resolve().parents[1]
CONSENT = PROMPTS["ask-question"].encode()

SCRIPT = r"""
import json, time
from contextlib import nullcontext
from unittest.mock import Mock, patch
from ai_desktop import ask_cli, cli, assistant_flow, agent_profiles, execution
from ai_desktop.permissions import Limits
profile = {'provider': 'claude', 'model': 'default', 'effort': 'default', 'instructions': '', 'profile': 'default',
           'access': 'changes'}
count = 0
def send(approval):
    global count
    count += 1
    data = json.loads(approval.payload)
    print('SENT %d Q:%s H:%d' % (count, data['question'].replace('\n', '|'), len(data.get('conversation', []))), flush=True)
    if data['question'] == 'WISI':
        print('WAITING', flush=True)
        time.sleep(30)
    return json.dumps({'action': 'answer', 'text': 'Odpowiedz %d.' % count})
policy = Mock(); policy.limits.return_value = Limits()
snapshot = assistant_flow.AccessSnapshot(policy, {'claude': True, 'systemd': [], 'weather': False},
                                         {'files': [], 'systemd': [], 'weather': False})
with patch.object(agent_profiles, 'resolve', return_value=profile), patch.object(ask_cli.os, 'getuid', return_value=1000), \
        patch.object(ask_cli.os, 'geteuid', return_value=1000), \
        patch.object(assistant_flow, 'capture_access', return_value=snapshot), \
        patch.object(ask_cli, 'installation_ready'), patch.object(ask_cli, '_choose_thread', return_value=('new', None)), \
        patch.object(ask_cli, 'managed_session', side_effect=lambda **kwargs: nullcontext()), \
        patch.object(ask_cli, 'send_once', side_effect=send):
    raise SystemExit(cli.main(['ask']))
"""


from unittest.mock import patch as _patch
_COLOUR = None


def setUpModule():
    # These tests check editing and flow by exact bytes; colour has its own tests in test_terminal_theme.py.
    global _COLOUR
    _COLOUR = _patch.dict(os.environ, {"NO_COLOR": "1"})
    _COLOUR.start()


def tearDownModule():
    _COLOUR.stop()


class Driver:
    def __init__(self, test):
        self.test = test
        self.master, self.slave = pty.openpty()
        self.before = termios.tcgetattr(self.slave)
        # The child gets the pty as its CONTROLLING terminal, so Ctrl-C reaches it as SIGINT exactly like in a shell.
        def own_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        self.process = subprocess.Popen([sys.executable, "-B", "-c", SCRIPT], cwd=ROOT, stdin=self.slave,
                                        stdout=self.slave, stderr=self.slave, close_fds=True,
                                        preexec_fn=own_terminal)
        self.output = bytearray()

    def pump(self, seconds=0.05):
        if select.select([self.master], [], [], seconds)[0]:
            try:
                raw = os.read(self.master, 8192)
            except OSError:
                return False
            if not raw:
                return False
            self.output.extend(raw)
        return True

    def text(self):
        return self.output.replace(b"\r\n", b"\n").decode("utf-8", "replace")

    def wait_for(self, marker, start=0, timeout=10):
        marker = marker if isinstance(marker, bytes) else marker.encode()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            index = self.output.replace(b"\r\n", b"\n").find(marker, start)
            if index >= 0:
                return index + len(marker)
            if not self.pump(0.05) and self.process.poll() is not None:
                break
        self.test.fail("missing %r in output: %s" % (marker, self.text()[-3500:]))

    def send(self, data):
        os.write(self.master, data if isinstance(data, bytes) else data.encode())

    def finish(self, timeout=5):
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            self.pump(0.05)
        for _ in range(20):
            self.pump(0.01)
        return self.process.returncode

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


class ConversationPtyTests(unittest.TestCase):
    def test_a_full_terminal_conversation(self):
        driver = Driver(self)
        self.addCleanup(driver.close)
        mark = driver.wait_for("W czym mogę pomóc?")
        driver.wait_for("> ", mark)
        driver.send("Pierwsze pytanie\r")
        mark = driver.wait_for(CONSENT, mark)
        driver.send(b"tak\n")
        mark = driver.wait_for("SENT 1 Q:Pierwsze pytanie H:0", mark)
        mark = driver.wait_for("Odpowiedz 1.", mark)
        mark = driver.wait_for("> ", mark)                                   # the next prompt is back at once

        # Arrow-key editing: "Drugi pytanie" -> move left over " pytanie" and insert the missing letter.
        driver.send("Drugi pytanie")
        driver.send(b"\x1b[D" * 8)
        driver.send("e")
        driver.send(b"\x1b[F")
        driver.send(b"\r")
        mark = driver.wait_for("SENT 2 Q:Drugie pytanie H:1", mark)          # no second consent in one conversation
        mark = driver.wait_for("> ", mark)

        # Up arrow recalls the edited message of this session; it is sent with the earlier turns as history.
        driver.send(b"\x1b[A")
        driver.send(b"\r")
        mark = driver.wait_for("SENT 3 Q:Drugie pytanie H:2", mark)
        mark = driver.wait_for("> ", mark)

        # Ctrl-C while waiting for the provider cancels THIS turn only, then the prompt is back.
        driver.send("WISI\r")
        mark = driver.wait_for("WAITING", mark)
        driver.send(b"\x03")
        mark = driver.wait_for("Anulowano", mark)
        mark = driver.wait_for("> ", mark)
        self.assertIsNone(driver.process.poll(), driver.text()[-800:])

        # Idle at the prompt: nothing is sent and nothing is read.
        counts = driver.text().count("SENT ")
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            driver.pump(0.05)
        self.assertEqual(driver.text().count("SENT "), counts)
        self.assertIsNone(driver.process.poll())

        # A later ordinary turn still sees the completed turns (the interrupted one is not among them).
        driver.send("Trzecie\r")
        mark = driver.wait_for("SENT 5 Q:Trzecie H:3", mark)
        mark = driver.wait_for("> ", mark)

        # Double Ctrl-D ends the conversation cleanly.
        driver.send(b"\x04")
        mark = driver.wait_for("jeszcze raz Ctrl-D", mark)
        mark = driver.wait_for("> ", mark)
        driver.send(b"\x04")
        driver.wait_for("Zapisać tę rozmowę", mark)                         # consent to keep the thread, default: no
        driver.send(b"nie\n")
        self.assertEqual(driver.finish(), 0, driver.text()[-800:])
        self.assertEqual(driver.text().count("W czym mogę pomóc?"), 1)         # the greeting appears once
        self.assertEqual(termios.tcgetattr(driver.slave), driver.before)       # terminal settings restored

    def test_a_multi_line_paste_is_one_message_and_an_oversized_paste_is_explained(self):
        driver = Driver(self)
        self.addCleanup(driver.close)
        mark = driver.wait_for("W czym mogę pomóc?")
        mark = driver.wait_for("> ", mark)
        start, end = b"\x1b[200~", b"\x1b[201~"
        output = "Startup finished in 13.339s (firmware) + 10.671s (userspace) = 36.610s\ngraphical.target reached after 10.622s"
        driver.send(b"Wynik: " + start + output.encode() + end + b"\r")
        mark = driver.wait_for(CONSENT, mark)
        driver.send(b"tak\n")
        # both lines arrive as ONE message, in order, with the text typed around the paste
        driver.wait_for("SENT 1 Q:Wynik: Startup finished in 13.339s (firmware) + 10.671s (userspace) = 36.610s|"
                        "graphical.target reached after 10.622s H:0", mark)
        mark = driver.wait_for("> ", mark)
        big = ("linia z logu o dosyć długiej treści\n" * 80).encode()                 # > 2048 B
        driver.send(start + big + end + b"\r")
        mark = driver.wait_for("limit to 2048 B", mark)
        mark = driver.wait_for("> ", mark)
        self.assertEqual(driver.text().count("SENT "), 1)                          # nothing was sent for the oversized one
        driver.send(b"\x04")
        mark = driver.wait_for("jeszcze raz Ctrl-D", mark)
        mark = driver.wait_for("> ", mark)
        driver.send(b"\x04")
        driver.wait_for("Zapisać tę rozmowę", mark)
        driver.send(b"nie\n")
        self.assertEqual(driver.finish(), 0, driver.text()[-800:])

    def test_typing_a_slash_lists_commands_tab_completes_and_a_control_is_not_sent(self):
        driver = Driver(self)
        self.addCleanup(driver.close)
        mark = driver.wait_for("W czym mogę pomóc?")
        mark = driver.wait_for("> ", mark)
        driver.send("/mo")
        mark = driver.wait_for("zmień model", mark)
        driver.send(b"\t")
        driver.send("haiku\r")
        mark = driver.wait_for("model: haiku", mark)
        mark = driver.wait_for("poprosi o zgodę", mark)
        mark = driver.wait_for("> ", mark)
        driver.send("Pytanie po zmianie\r")
        mark = driver.wait_for(CONSENT, mark)
        driver.send(b"tak\n")
        mark = driver.wait_for("SENT 1 Q:Pytanie po zmianie H:0", mark)       # the control itself never reached the model
        mark = driver.wait_for("> ", mark)
        driver.send("/ko\r")                                                   # Enter runs what the list has highlighted

    def test_a_new_message_cancels_the_intention_to_exit(self):
        driver = Driver(self)
        self.addCleanup(driver.close)
        mark = driver.wait_for("W czym mogę pomóc?")
        mark = driver.wait_for("> ", mark)
        driver.send("Pytanie\r")
        mark = driver.wait_for(CONSENT, mark)
        driver.send(b"tak\n")
        mark = driver.wait_for("> ", driver.wait_for("Odpowiedz 1.", mark))
        driver.send(b"\x04")
        mark = driver.wait_for("jeszcze raz Ctrl-D", mark)
        mark = driver.wait_for("> ", mark)
        driver.send("Dalej\r")                                               # typing a message clears the pending exit
        mark = driver.wait_for("> ", driver.wait_for("Odpowiedz 2.", mark))
        driver.send(b"\x04")
        mark = driver.wait_for("jeszcze raz Ctrl-D", mark)
        driver.wait_for("> ", mark)
        self.assertIsNone(driver.process.poll(), "one Ctrl-D after a new message must only warn")
        driver.send(b"\r")                                                   # empty line ends the conversation
        driver.wait_for("Zapisać tę rozmowę", mark)
        driver.send(b"nie\n")
        self.assertEqual(driver.finish(), 0, driver.text()[-800:])


if __name__ == "__main__":
    unittest.main()
