"""Real arrow/Enter settings transactions on a PTY, without client or OAuth."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import contextmanager, ExitStack
import fcntl
import os
from pathlib import Path
import pty
import pwd
import select
import stat
import struct
import termios
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import agent_profiles as profiles, management_cli


class ManagementMenuPTYTests(unittest.TestCase):
    @contextmanager
    def settings(self):
        # Store's real ancestor checks deliberately reject world-writable /tmp.
        with tempfile.TemporaryDirectory(prefix="aid-settings-pty-", dir=pwd.getpwuid(os.geteuid()).pw_dir) as directory:
            home = Path(directory)
            store = profiles.Store(home)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
            original = termios.tcgetattr(slave)
            source = os.fdopen(os.dup(slave), "r", encoding="utf-8")
            target = os.fdopen(os.dup(slave), "w", encoding="utf-8", buffering=1)
            done, result, output, writes = threading.Event(), {}, bytearray(), []
            real_open, real_mkdir = os.open, os.mkdir

            def private_target(path, dir_fd):
                candidate = Path(path)
                if not candidate.is_absolute():
                    parent = Path(os.readlink("/proc/self/fd/%d" % dir_fd)) if dir_fd is not None else Path.cwd()
                    candidate = parent / candidate
                candidate = Path(os.path.abspath(candidate))
                if not candidate.is_relative_to(home):
                    raise AssertionError("settings attempted a write outside its private home")
                writes.append(candidate)

            def open_private(path, flags, mode=0o777, *, dir_fd=None):
                if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                    private_target(path, dir_fd)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            def mkdir_private(path, mode=0o777, *, dir_fd=None):
                private_target(path, dir_fd)
                return real_mkdir(path, mode, dir_fd=dir_fd)

            def run():
                try:
                    result["code"] = management_cli.main([], store=store)
                except BaseException as exc:
                    result["error"] = exc
                finally:
                    done.set()

            with ExitStack() as stack:
                stack.enter_context(patch.object(management_cli.sys, "stdin", source))
                stack.enter_context(patch.object(management_cli.sys, "stdout", target))
                stack.enter_context(patch("socket.socket", side_effect=AssertionError("network")))
                stack.enter_context(patch("subprocess.run", side_effect=AssertionError("native process")))
                stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("native process")))
                stack.enter_context(patch("ai_desktop.analysis_session.administrative", side_effect=AssertionError("auth")))
                stack.enter_context(patch("os.open", side_effect=open_private))
                stack.enter_context(patch("os.mkdir", side_effect=mkdir_private))
                thread = threading.Thread(target=run, daemon=True)
                state = SimpleNamespace(home=home, store=store, master=master, slave=slave, source=source,
                                        target=target, original=original, done=done, result=result, output=output,
                                        writes=writes, thread=thread)
                thread.start()
                try:
                    yield state
                finally:
                    if thread.is_alive():
                        os.write(master, b"\x03")
                        thread.join(timeout=3)
                    target.close()
                    source.close()
                    os.close(slave)
                    os.close(master)
                    if thread.is_alive():
                        raise AssertionError("settings menu thread did not finish")

    def wait_for(self, state, marker, *, start=0):
        deadline = time.monotonic() + 3
        while True:
            found = state.output.find(marker, start)
            if found >= 0:
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([state.master], [], [], remaining)[0]:
                self.fail("missing settings frame: " + repr(marker) + "; " + repr(state.result))
            state.output.extend(os.read(state.master, 65536))

    def frame(self, state, title, *, start=0):
        position = self.wait_for(state, title.encode(), start=start)
        self.wait_for(state, b"Esc cancels", start=position)

    def select_after(self, state, down, title):
        start = len(state.output)
        os.write(state.master, (b"\x1b[B" * down if down >= 0 else b"\x1b[A" * -down) + b"\r")
        self.frame(state, title, start=start)

    def finish(self, state):
        self.assertTrue(state.done.wait(3), "settings menu did not finish")
        state.thread.join(timeout=1)
        self.assertNotIn("error", state.result, repr(state.result.get("error")))
        self.assertEqual(state.result.get("code"), 0)
        self.assertEqual(termios.tcgetattr(state.slave), state.original)

    def test_real_arrows_enter_save_opus_and_max_then_escape_restores_terminal(self):
        with self.settings() as state:
            self.frame(state, "Settings · agent: default")
            self.select_after(state, 2, "Model · claude")
            self.select_after(state, 3, "Settings · agent: default")
            self.select_after(state, 1, "Reasoning level")
            self.select_after(state, 5, "Settings · agent: default")
            os.write(state.master, b"\x1b")
            self.finish(state)
            selected = state.store.load().config.profiles["default"]
            self.assertEqual((selected.model, selected.effort), ("opus", "max"))
            self.assertEqual(stat.S_IMODE(state.store.path.stat().st_mode), 0o600)
            self.assertTrue(state.writes)
            self.assertTrue(all(path.is_relative_to(state.home) for path in state.writes))

    def test_service_and_role_switch_refresh_the_same_real_terminal_menu(self):
        with self.settings() as state:
            self.frame(state, "Settings · agent: default")
            self.select_after(state, 1, "Service · for all agents")
            self.select_after(state, 1, "Settings · agent: default")
            start = len(state.output)
            self.select_after(state, -1, "Choose agent")
            self.wait_for(state, "reviewer · Codex".encode(), start=start)
            self.select_after(state, 3, "Settings · agent: reviewer")
            self.assertEqual(state.store.load().config.selected_provider, "codex")
            self.assertTrue(all(profile.provider == "codex" for _, profile in state.store.load().config.entries))
            self.select_after(state, 1, "Service · for all agents")
            start = len(state.output)
            self.select_after(state, -1, "Settings · agent: reviewer")
            self.wait_for(state, "Service: Claude".encode(), start=start)
            os.write(state.master, b"\x1b")
            self.finish(state)
            config = state.store.load().config
            self.assertEqual((config.active, config.selected_provider), ("reviewer", "claude"))
            self.assertEqual(config.profiles["reviewer"].access, "read-only")

    def test_escape_from_initial_settings_creates_no_configuration(self):
        with self.settings() as state:
            self.frame(state, "Settings · agent: default")
            os.write(state.master, b"\x1b")
            self.finish(state)
            self.assertEqual(list(state.home.iterdir()), [])
            self.assertEqual(state.writes, [])

    def test_default_skill_catalog_opens_in_the_real_terminal_without_writes(self):
        with self.settings() as state:
            self.frame(state, "Settings · agent: default")
            count = len(state.store.load().config.profiles["default"].skills)
            self.assertGreater(count, 3)
            self.select_after(state, 6, "Skills · " + str(count) + " assigned")
            start = len(state.output)
            os.write(state.master, b"\x1b")
            self.frame(state, "Settings · agent: default", start=start)
            os.write(state.master, b"\x1b")
            self.finish(state)
            self.assertEqual(list(state.home.iterdir()), [])
            self.assertEqual(state.writes, [])


if __name__ == "__main__":
    unittest.main()
