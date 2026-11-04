"""Offline activation contracts; positive UID/PID1 decisions are simulated.

Real Unix packets and fd inheritance, no systemctl start, sudo, host account,
Claude or OAuth. Actual systemd + separate-UID proof is opt-in on CachyOS.
"""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import contextlib
from dataclasses import replace
import errno
import io
import os
from pathlib import Path
import pty
import re
import select
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import cli, e2_handoff_cli, e2_ipc, e2_socket_activation as activation
from test_e2_ipc import authorized, packet
from test_report_policy import report_fixture

ROOT = Path(__file__).resolve().parents[1]


class ActivationTests(unittest.TestCase):
    def test_uid_and_cli_do_not_accept_paths_commands_or_root(self):
        for value in (0, -1, True, "1000", 2**32 - 1):
            with self.subTest(value=value), self.assertRaises(e2_ipc.HandoffDenied):
                activation.socket_path(value)
        with patch.object(activation, "receive_once") as receive:
            for args in ([], ["0"], ["01000"], ["../1000"], ["1000", "sh"], ["-1"]):
                self.assertEqual(activation.worker_main(args), 2)
            receive.assert_not_called()
        with patch.object(cli.e2_handoff_cli, "desktop_main", return_value=0) as desktop:
            self.assertEqual(cli.main(["handoff"]), 0)
            desktop.assert_called_once_with()
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as denied:
                    cli.main(["handoff", "--uid", "1000"])
            self.assertEqual(denied.exception.code, 2)

    def test_protected_path_denies_symlink_mutable_parent_and_wrong_socket(self):
        root = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        sock = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=1000,
                               st_dev=4, st_ino=5)
        with patch.object(activation.os, "lstat", side_effect=[root, root, root, sock]):
            self.assertEqual(activation._trusted_endpoint(1000), (4, 5))
        for index, bad in (
            (0, SimpleNamespace(st_mode=stat.S_IFLNK | 0o755, st_uid=0)),
            (1, SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0)),
            (2, SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=1000)),
            (3, SimpleNamespace(st_mode=stat.S_IFLNK | 0o600, st_uid=1000)),
            (3, SimpleNamespace(st_mode=stat.S_IFSOCK | 0o666, st_uid=1000)),
            (3, SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=1001)),
        ):
            entries = [root, root, root, sock]
            entries[index] = bad
            with self.subTest(index=index, bad=bad), patch.object(
                    activation.os, "lstat", side_effect=entries):
                with self.assertRaises(e2_ipc.HandoffDenied):
                    activation._trusted_endpoint(1000)

    def test_manager_peer_requires_pid1_root_seqpacket_and_exact_endpoint(self):
        channel = Mock(family=socket.AF_UNIX)
        channel.getsockopt.return_value = socket.SOCK_SEQPACKET
        channel.getpeername.return_value = activation.socket_path(1000)
        for peer in ((1, 0, 0), (12, 0, 0), (1, 1000, 1000), (1, 0, 1000)):
            with patch.object(e2_ipc, "_kernel_peer", return_value=peer):
                if peer == (1, 0, 0):
                    activation._manager_peer(channel, 1000)
                else:
                    with self.assertRaises(e2_ipc.HandoffDenied):
                        activation._manager_peer(channel, 1000)
        with patch.object(e2_ipc, "_kernel_peer", return_value=(1, 0, 0)):
            channel.getpeername.return_value = "/tmp/fake.sock"
            with self.assertRaises(e2_ipc.HandoffDenied):
                activation._manager_peer(channel, 1000)
            channel.getpeername.return_value = activation.socket_path(1000)
            channel.getsockopt.return_value = socket.SOCK_STREAM
            with self.assertRaises(e2_ipc.HandoffDenied):
                activation._manager_peer(channel, 1000)

    def test_invalid_authorization_cannot_activate_service(self):
        auth = authorized()
        for value in (None, {}, replace(auth, sha256="0" * 64),
                      replace(auth, purpose="exec")):
            with (self.subTest(value=type(value)),
                  patch.object(activation.e2_account, "account_ready", return_value=True),
                  patch.object(activation, "_trusted_endpoint") as endpoint):
                with self.assertRaises(e2_ipc.HandoffDenied):
                    activation.send_once(value)
                endpoint.assert_not_called()

    def test_real_packets_digest_ack_and_closed_worker(self):
        auth = authorized()
        uid = os.geteuid()
        with tempfile.TemporaryDirectory() as temp, patch.object(
                activation, "SOCKET_DIRECTORY", temp):
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
                listener.bind(activation.socket_path(uid))
                listener.listen(1)
                result = {}
                def run():
                    try:
                        result["digest"] = activation.receive_once(uid)
                    except Exception as exc:
                        result["error"] = type(exc).__name__
                worker_os = SimpleNamespace(geteuid=lambda: uid + 77)
                # Filesystem/PID1/UID setup only is simulated. Packet parsing,
                # message bounds, hashing, ACK and socket IO are the real code.
                with (patch.object(activation, "_activation_listener", return_value=listener),
                      patch.object(activation, "_worker_identity", return_value=True),
                      patch.object(activation, "_confinement_ready", return_value=True),
                      patch.object(activation, "os", worker_os),
                      patch.object(e2_ipc, "_peer_uid", return_value=None)):
                    task = threading.Thread(target=run)
                    task.start()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
                        client.settimeout(2)
                        client.connect(activation.socket_path(uid))
                        self.assertEqual(client.recv(128), activation.READY)
                        client.send(packet(auth))
                        self.assertEqual(client.recv(128), b"ACK1:" + auth.sha256.encode())
                        self.assertEqual(client.recv(1), b"")
                    task.join(3)
                self.assertFalse(task.is_alive())
                self.assertEqual(result, {"digest": auth.sha256})

    def test_client_real_io_refuses_bad_ack_and_truncation(self):
        auth = authorized()
        uid = os.geteuid()
        for reply in (b"ACK1:" + auth.sha256.encode(), b"", b"ACK1:" + b"0" * 64,
                      b"ACK1:" + auth.sha256.encode() + b"EXTRA"):
            with self.subTest(reply_length=len(reply)), tempfile.TemporaryDirectory() as temp:
                with patch.object(activation, "SOCKET_DIRECTORY", temp):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
                        listener.bind(activation.socket_path(uid)); listener.listen(1)
                        received = []
                        def server():
                            with listener.accept()[0] as conn:
                                conn.send(activation.READY)
                                received.append(conn.recv(8192))
                                if reply:
                                    conn.send(reply)
                        task = threading.Thread(target=server)
                        task.start()
                        with (patch.object(activation.e2_account, "account_ready", return_value=True),
                              patch.object(activation, "_trusted_endpoint", return_value=(1, 2)),
                              patch.object(activation, "_manager_peer")):
                            if reply == b"ACK1:" + auth.sha256.encode():
                                self.assertEqual(activation.send_once(auth), auth.sha256)
                            else:
                                with self.assertRaises(e2_ipc.HandoffDenied):
                                    activation.send_once(auth)
                        task.join(3)
                        self.assertFalse(task.is_alive())
                        self.assertEqual(received, [packet(auth)])

    def test_failed_readiness_never_discloses_report(self):
        uid = os.geteuid()
        for ready in (b"WRONG", activation.READY + b"EXTRA", None):
            with tempfile.TemporaryDirectory() as temp, patch.object(
                    activation, "SOCKET_DIRECTORY", temp):
                with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
                    listener.bind(activation.socket_path(uid)); listener.listen(1)
                    received = []
                    def server():
                        with listener.accept()[0] as conn:
                            conn.settimeout(2)
                            if ready is not None:
                                conn.send(ready)
                            received.append(conn.recv(8192))
                    task = threading.Thread(target=server)
                    task.start()
                    with (patch.object(activation.e2_account, "account_ready", return_value=True),
                          patch.object(activation, "_trusted_endpoint", return_value=(1, 2)),
                          patch.object(activation, "_manager_peer"),
                          patch.object(activation, "TIMEOUT", 0.1)):
                        with self.assertRaises(e2_ipc.HandoffDenied):
                            activation.send_once(authorized())
                    task.join(3)
                    self.assertFalse(task.is_alive())
                    self.assertEqual(received, [b""])

    def test_missing_account_root_and_endpoint_replacement_refused(self):
        with patch.object(activation.e2_account, "account_ready", return_value=False), patch.object(
                activation, "_trusted_endpoint") as endpoint:
            with self.assertRaises(e2_ipc.HandoffDenied):
                activation.send_once(authorized())
            endpoint.assert_not_called()
        with patch.object(activation.os, "geteuid", return_value=0):
            with self.assertRaises(e2_ipc.HandoffDenied):
                activation.send_once(authorized())
        conn = Mock()
        conn.__enter__ = Mock(return_value=conn)
        conn.__exit__ = Mock(return_value=False)
        with (patch.object(activation.e2_account, "account_ready", return_value=True),
              patch.object(activation, "_trusted_endpoint", side_effect=[(1, 2), (1, 3)]),
              patch.object(activation, "_manager_peer"),
              patch.object(activation.socket, "socket", return_value=conn)):
            with self.assertRaises(e2_ipc.HandoffDenied):
                activation.send_once(authorized())
            conn.send.assert_not_called()

    def test_worker_refuses_wrong_uid_real_kernel_peer_without_ack(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with left, right:
            left.send(packet(authorized()))
            with self.assertRaises(e2_ipc.HandoffDenied):
                e2_ipc.receive_approved(right, desktop_uid=os.geteuid() + 1)
            left.settimeout(0.01)
            with self.assertRaises(TimeoutError):
                left.recv(1)

    def test_activation_consumes_environment_and_rejects_missing_descriptor(self):
        for env in ({}, {"LISTEN_PID": "1", "LISTEN_FDS": "1", "LISTEN_FDNAMES": "handoff"},
                    {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "2", "LISTEN_FDNAMES": "handoff"},
                    {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "1", "LISTEN_FDNAMES": "other"}):
            with patch.dict(os.environ, env, clear=True), patch.object(
                    activation.socket, "socket") as opened:
                with self.assertRaises(e2_ipc.HandoffDenied):
                    activation._activation_listener(1000)
                opened.assert_not_called()
                self.assertNotIn("LISTEN_PID", os.environ)

    def test_real_fd3_inheritance_validation_and_no_descriptor_leak(self):
        with tempfile.TemporaryDirectory() as temp:
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
                listener.bind(temp + "/1000.sock"); listener.listen(1)
                code = '''
import os, sys
from ai_desktop import e2_socket_activation as a
os.dup2(int(sys.argv[1]), 3)
a.SOCKET_DIRECTORY = sys.argv[2]
os.environ.update(LISTEN_PID=str(os.getpid()), LISTEN_FDS="1", LISTEN_FDNAMES="handoff")
with a._activation_listener(1000) as listener:
    assert listener.fileno() == 3 and not os.get_inheritable(3)
    assert "LISTEN_PID" not in os.environ
try: os.fstat(3)
except OSError: pass
else: raise AssertionError("fd leaked")
'''
                done = subprocess.run([sys.executable, "-B", "-c", code,
                                       str(listener.fileno()), temp], cwd=ROOT,
                                      pass_fds=(listener.fileno(),), capture_output=True,
                                      text=True, timeout=5)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_worker_identity_confinement_timeout_and_receive_failure_are_closed(self):
        with patch.object(activation, "_worker_identity", return_value=False), patch.object(
                activation, "_activation_listener") as inherited:
            with self.assertRaises(e2_ipc.HandoffDenied):
                activation.receive_once(os.geteuid() + 77)
            inherited.assert_not_called()
        with (patch.object(activation, "_worker_identity", return_value=True),
              patch.object(activation, "_confinement_ready", return_value=False),
              patch.object(activation, "_activation_listener") as inherited):
            with self.assertRaises(e2_ipc.HandoffDenied):
                activation.receive_once(os.geteuid() + 77)
            inherited.assert_not_called()
        for error in (OSError("PRIVATE"), ValueError("PRIVATE"), KeyError("PRIVATE")):
            with patch.object(activation, "_worker_identity", side_effect=error):
                self.assertEqual(activation.worker_main([str(os.geteuid() + 77)]), 2)

    def test_tty_hash_consent_before_activation_and_no_transfer_on_refusal(self):
        report = report_fixture()
        def confirm(prompt):
            return "UDOSTEPNIJ " + prompt.split("UDOSTEPNIJ ", 1)[1].split()[0]
        for reader, expected in ((confirm, 0), (lambda _: "TAK", 2),
                                 (lambda _: "UDOSTEPNIJ " + "0" * 64, 2)):
            with (patch.object(e2_handoff_cli.diagnose, "collect", return_value=report),
                  patch.object(sys.stdin, "isatty", return_value=True),
                  contextlib.redirect_stdout(io.StringIO()) as out,
                  patch.object(out, "isatty", return_value=True),
                  patch.object(activation, "send_once", side_effect=lambda a: a.sha256) as send):
                self.assertEqual(e2_handoff_cli.desktop_main(reader=reader), expected)
                self.assertEqual(send.call_count, 1 if expected == 0 else 0)
                self.assertIn("bez Claude i OAuth", out.getvalue())
        with patch.object(sys.stdin, "isatty", return_value=False), patch.object(
                e2_handoff_cli.diagnose, "collect") as collect, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(e2_handoff_cli.desktop_main(reader=confirm), 2)
            collect.assert_not_called()

    def test_real_terminal_consent_and_refusal_in_public_cli(self):
        # Only diagnostics and transport are fixtures. Public parser, actual
        # terminal detection, input, rendering and ConsentSession are real.
        code = '''
import sys
sys.path.insert(0, "tests")
from unittest.mock import patch
from test_report_policy import report_fixture
from ai_desktop import cli, e2_handoff_cli, e2_socket_activation
def sent(auth):
    print("SYNTHETIC TRANSFER", flush=True)
    return auth.sha256
with patch.object(e2_handoff_cli.diagnose, "collect", return_value=report_fixture()), patch.object(e2_socket_activation, "send_once", side_effect=sent):
    raise SystemExit(cli.main(["handoff"]))
'''
        for approved in (True, False):
            master, slave = pty.openpty()
            proc = None
            try:
                proc = subprocess.Popen([sys.executable, "-B", "-c", code], cwd=ROOT,
                                        stdin=slave, stdout=slave, stderr=slave)
                os.close(slave); slave = -1
                seen = b""
                deadline = time.monotonic() + 5
                while b"aby przekazac raport:" not in seen and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            seen += os.read(master, 8192)
                        except OSError:
                            break
                match = re.search(rb"UDOSTEPNIJ ([0-9a-f]{64})", seen)
                self.assertIsNotNone(match, seen.decode(errors="replace"))
                answer = b"UDOSTEPNIJ " + match.group(1) if approved else b"NIE"
                os.write(master, answer + b"\n")
                proc.wait(timeout=5)
                while select.select([master], [], [], 0.1)[0]:
                    try:
                        block = os.read(master, 8192)
                        if not block:
                            break
                        seen += block
                    except OSError:
                        break
                self.assertEqual(proc.returncode, 0 if approved else 2, seen)
                self.assertEqual(b"SYNTHETIC TRANSFER" in seen, approved)
            finally:
                if proc is not None and proc.poll() is None:
                    proc.kill(); proc.wait()
                os.close(master)
                if slave >= 0:
                    os.close(slave)

    def test_expired_or_changed_consent_never_activates_worker(self):
        from ai_desktop.transfer_consent import ConsentSession
        for changed in (False, True):
            report = report_fixture()
            now = [0]
            gate = ConsentSession(clock=lambda: now[0])
            def answer(prompt):
                if changed:
                    report["unexpected"] = "forbidden"
                else:
                    now[0] = 1000
                return "UDOSTEPNIJ " + prompt.split("UDOSTEPNIJ ", 1)[1].split()[0]
            with (patch.object(e2_handoff_cli.diagnose, "collect", return_value=report),
                  patch.object(e2_handoff_cli, "ConsentSession", return_value=gate),
                  patch.object(sys.stdin, "isatty", return_value=True),
                  contextlib.redirect_stdout(io.StringIO()) as out,
                  patch.object(out, "isatty", return_value=True),
                  patch.object(activation, "send_once") as send):
                self.assertEqual(e2_handoff_cli.desktop_main(reader=answer), 2)
                send.assert_not_called()

    def test_effective_confinement_checks_not_just_unit_configuration(self):
        status = "NoNewPrivs: 1\nSeccomp: 2\nCapEff: 0000\nCapBnd: 0000\n"
        mounts = "\n".join("1 0 0:1 / " + path + " ro - " + kind + " source ro"
                           for path, kind in (("/", "ext4"), ("/usr", "ext4"),
                                              ("/etc", "tmpfs"), ("/var", "tmpfs"),
                                              ("/run", "tmpfs"), ("/tmp", "tmpfs")))
        def check(s=status, m=mounts, socket_error=errno.EPERM, home=False):
            with (patch.object(Path, "read_text", side_effect=[s, m]),
                  patch.object(activation.os.path, "lexists", return_value=home),
                  patch.object(activation.os, "access", return_value=False),
                  patch.object(activation.socket, "socket", side_effect=OSError(socket_error, "fixed"))):
                return activation._confinement_ready()
        self.assertTrue(check())
        for s in (status.replace("NoNewPrivs: 1", "NoNewPrivs: 0"),
                  status.replace("CapBnd: 0000", "CapBnd: 0001")):
            self.assertFalse(check(s=s))
        self.assertFalse(check(m=mounts.replace("/var ro", "/var rw")))
        self.assertFalse(check(socket_error=errno.EMFILE))
        self.assertFalse(check(home=True))
        with (patch.object(Path, "read_text", side_effect=[status, mounts]),
              patch.object(activation.os.path, "lexists", return_value=False),
              patch.object(activation.os, "access", return_value=False),
              patch.object(activation.socket, "socket") as socket_allowed):
            self.assertFalse(activation._confinement_ready())
            socket_allowed.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
