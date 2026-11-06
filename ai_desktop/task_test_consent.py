"""One process-local approval for one declared test on a bound local copy.

Only the trusted controller constructs the scope and calls approve after the
owner's explicit yes. This capability never authorizes a live workspace write,
another recipe, or another test attempt, and is not part of saved task state.
"""
import os
import time

from .analysis_contract import AnalysisDenied, CONSENT_CEILING, canonical, consent_seconds
from .execution import current as current_execution
from .task_contract import CHECKS


def _scope_bytes(scope):
    """Freeze JSON without accepting Python values that serialize ambiguously."""
    if type(scope) is not dict or not scope:
        raise ValueError()
    frozen = canonical(scope).encode("utf-8")  # also rejects cycles before walking values
    pending = [scope]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError()
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError()
    return frozen


class CopyTestConsent:
    """An exact scope/recipe, finite deadline and live controller guard."""

    __slots__ = ("_scope", "_recipe", "_execution", "_pid", "_guard", "_clock",
                 "_seconds", "_expires", "_state")

    def __init__(self, scope, recipe, *, guard, clock=None, seconds=None):
        try:
            if (type(recipe) is not str or recipe not in CHECKS or not callable(guard)
                    or (clock is not None and not callable(clock))):
                raise ValueError()
            self._scope = _scope_bytes(scope)
            self._recipe = recipe
            selected = current_execution()
            self._execution = canonical(selected)
            self._seconds = consent_seconds(selected.get("effort") if selected is not None else None) if seconds is None else seconds
            if type(self._seconds) is not int or not 0 < self._seconds <= CONSENT_CEILING:
                raise ValueError()
        except (OSError, ValueError, TypeError, RecursionError):
            raise AnalysisDenied("consent") from None
        self._pid = os.getpid()
        self._guard = guard
        self._clock = time.monotonic if clock is None else clock
        self._expires = None
        self._state = "new"

    @property
    def seconds(self):
        """The duration the controller can show before asking for approval."""
        return self._seconds

    def _check(self):
        if (os.getpid() != self._pid or canonical(current_execution()) != self._execution
                or not self._clock() <= self._expires):
            raise ValueError()

    def approve(self):
        """Called once by the trusted UI, only after its explicit affirmative answer."""
        eligible = self._state == "new"
        self.close()
        try:
            if not eligible:
                raise ValueError()
            self._state = "approving"
            self._expires = self._clock() + self._seconds
            self._check()
            if self._guard() is False:
                raise ValueError()
            self._check()
            if self._state != "approving":
                raise ValueError()
        except Exception:
            self.close()
            raise AnalysisDenied("consent") from None
        except BaseException:
            self.close()
            raise
        self._state = "approved"

    def check(self, scope, recipe):
        """Recheck before a stage without spending or renewing the one allowed test."""
        try:
            if self._state != "approved" or type(recipe) is not str or recipe != self._recipe:
                raise ValueError()
            if _scope_bytes(scope) != self._scope:
                raise ValueError()
            self._check()
            if self._guard() is False:
                raise ValueError()
            self._check()
            if self._state != "approved" or _scope_bytes(scope) != self._scope:
                raise ValueError()
        except Exception:
            self.close()
            raise AnalysisDenied("consent") from None
        except BaseException:
            self.close()
            raise

    def consume(self, scope, recipe):
        """Consume on any attempt; invalid, expired and used grants cannot be retried."""
        try:
            self.check(scope, recipe)
        finally:
            self.close()

    def close(self):
        """Cancellation and success both permanently retire this one-use grant."""
        self._state = "closed"
