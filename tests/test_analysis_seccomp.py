"""Real kernel mode-bit denials, including inherited filters across exec."""
import errno
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from ai_desktop import analysis_seccomp
from ai_desktop.analysis_contract import AnalysisDenied

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = r'''
import ctypes, errno, os, pathlib, stat, subprocess, sys, tempfile
from ai_desktop.analysis_seccomp import install_mode_filter
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
assert libc.prctl(38, 1, 0, 0, 0) == 0  # PR_SET_NO_NEW_PRIVS
with tempfile.TemporaryDirectory() as temp:
    root = pathlib.Path(temp)
    path = root / 'file'
    path.write_text('SYNTHETIC')
    fd = os.open(path, os.O_RDWR)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    # Before filter, this UID can change these bits on its own file.
    path.chmod(0o4700)
    assert path.stat().st_mode & stat.S_ISUID
    path.chmod(0o600)
    sec = ctypes.CDLL('libseccomp.so.2')
    sec.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    sec.seccomp_syscall_resolve_name.restype = ctypes.c_int
    openat2 = sec.seccomp_syscall_resolve_name(b'openat2')
    fchmodat2 = sec.seccomp_syscall_resolve_name(b'fchmodat2')
    assert openat2 >= 0 and fchmodat2 >= 0
    install_mode_filter()
    def refused(call):
        try:
            call()
        except OSError as exc:
            assert exc.errno == errno.EPERM
        else:
            raise AssertionError('mode-bit accepted')
    for bit in (stat.S_ISUID, stat.S_ISGID):
        refused(lambda: path.chmod(0o700 | bit))
        refused(lambda: os.fchmod(fd, 0o700 | bit))
        refused(lambda: os.chmod('file', 0o700 | bit, dir_fd=directory))
        refused(lambda: os.mkdir(root / 'blocked-dir', 0o700 | bit))
        refused(lambda: os.mkdir('blocked-dir', 0o700 | bit, dir_fd=directory))
        refused(lambda: os.mknod(root / 'blocked-node', stat.S_IFREG | 0o600 | bit))
        refused(lambda: os.open(root / 'blocked-file', os.O_CREAT | os.O_WRONLY, 0o600 | bit))
        refused(lambda: os.open('blocked-file', os.O_CREAT | os.O_WRONLY, 0o600 | bit, dir_fd=directory))
        refused(lambda: os.open(root, os.O_TMPFILE | os.O_RDWR, 0o600 | bit))
        ctypes.set_errno(0)
        assert libc.syscall(ctypes.c_long(fchmodat2), ctypes.c_int(directory),
                            ctypes.c_char_p(b'file'), ctypes.c_uint(0o700 | bit), ctypes.c_int(0)) == -1
        assert ctypes.get_errno() == errno.EPERM
    # Normal files/directories and profile-style atomic replacement still work.
    path.chmod(0o600)
    os.fchmod(fd, 0o600)
    (root / 'allowed-dir').mkdir(0o700)
    (root / 'replacement').write_text('UPDATED')
    (root / 'replacement').replace(path)
    assert path.read_text() == 'UPDATED'
    assert not (root / 'blocked-dir').exists() and not (root / 'blocked-file').exists()
    class How(ctypes.Structure):
        _fields_ = [('flags', ctypes.c_uint64), ('mode', ctypes.c_uint64), ('resolve', ctypes.c_uint64)]
    how = How(os.O_RDONLY, 0, 0)
    ctypes.set_errno(0)
    assert libc.syscall(ctypes.c_long(openat2), ctypes.c_int(-100), ctypes.c_char_p(os.fsencode(path)),
                        ctypes.byref(how), ctypes.c_size_t(ctypes.sizeof(how))) == -1
    assert ctypes.get_errno() == errno.ENOSYS
    inherited = subprocess.run([sys.executable, '-I', '-B', '-c',
        'import os,sys,errno\ntry: os.chmod(sys.argv[1],0o4700)\nexcept OSError as e: sys.exit(0 if e.errno==errno.EPERM else 3)\nelse: sys.exit(4)', str(path)],
        check=False, capture_output=True, timeout=5)
    assert inherited.returncode == 0
    os.close(fd)
    os.close(directory)
print('MODE FILTER: PASS chmod=create=exec=denied normal-write=allowed openat2=ENOSYS')
'''


class AnalysisSeccompTests(unittest.TestCase):
    def test_real_kernel_refusal_and_exec_inheritance_with_normal_profile_writes(self):
        done = subprocess.run([sys.executable, '-B', '-c', SCRIPT], cwd=ROOT,
                              capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('MODE FILTER: PASS', done.stdout)

    def test_missing_nnp_or_existing_threads_refuses_before_library_load(self):
        with patch.object(analysis_seccomp.ctypes, 'CDLL') as load:
            for nnp, threads in (('0', 1), ('1', 2)):
                with patch.object(Path, 'read_text', return_value='NoNewPrivs:\t' + nnp), \
                        patch.object(Path, 'iterdir', return_value=iter(range(threads))), \
                        self.assertRaises(AnalysisDenied):
                    analysis_seccomp.install_mode_filter()
        load.assert_not_called()

    def test_missing_syscall_or_filter_load_failure_never_succeeds(self):
        for syscall, loaded in ((-1, 0), (90, -errno.EPERM)):
            library = Mock()
            library.seccomp_init.return_value = 123
            library.seccomp_attr_set.return_value = 0
            library.seccomp_syscall_resolve_name.return_value = syscall
            library.seccomp_rule_add_array.return_value = 0
            library.seccomp_load.return_value = loaded
            with patch.object(Path, 'read_text', return_value='NoNewPrivs:\t1'), \
                    patch.object(Path, 'iterdir', return_value=iter([1])), \
                    patch.object(analysis_seccomp.ctypes, 'CDLL', return_value=library), \
                    self.assertRaises(AnalysisDenied):
                analysis_seccomp.install_mode_filter()
            library.seccomp_release.assert_called_once_with(123)
