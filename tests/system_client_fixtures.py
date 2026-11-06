"""Synthetic bindings for lifecycle unit tests; never resolves a host client."""
from ai_desktop import client_binding


class FakeBinding:
    def __init__(self, provider="codex", *, pass_fds=()):
        self.provider = provider
        self.binary = client_binding.binary(provider)
        self.pass_fds = pass_fds
        self.closed = False
        self.rechecks = 0
        self.failure = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.closed = True

    def recheck(self):
        if self.closed:
            raise AssertionError("binding used after close")
        self.rechecks += 1
        if self.failure is not None:
            raise self.failure

    def mount_args(self):
        self.recheck()
        return ["--dir", "/run/ai-desktop-native"]
