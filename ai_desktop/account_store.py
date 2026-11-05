"""Non-secret account names, selections and numeric usage snapshots in a private desktop store."""
from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
import json
import os
import re

from .agent_profiles import Store, ProfileError, _file, _pairs

CLOUD = ("claude", "codex")
PLANS = frozenset(("free", "go", "plus", "pro", "max", "ultra", "prolite", "promax", "team", "business",
                   "enterprise", "ent26", "edu", "edu_plus", "edu_pro", "sci", "sci_plus", "self_serve_business_prolite",
                   "self_serve_business_usage_based", "enterprise_cbp_automation", "enterprise_cbp_usage_based"))
_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}\Z", re.ASCII)


def named_account(value):
    return type(value) is str and value != "default" and _NAME.fullmatch(value) is not None


def quota(value, *, stored_at=None):
    """The provider supplies only percent/reset. Only the desktop can stamp retained observations."""
    if type(value) is not dict or not value or set(value) - {"five_hour", "seven_day"}:
        raise ValueError("quota")
    for sample in value.values():
        if type(sample) is not dict:
            raise ValueError("quota")
        extra = set(sample) - {"percent", "reset"}
        if extra and (extra != {"observed_at"} or stored_at is None
                      or type(sample["observed_at"]) is not int or not 0 < sample["observed_at"] < stored_at):
            raise ValueError("quota")
        if (not {"percent", "reset"} <= set(sample)
                or type(sample["percent"]) not in (float, int) or not 0 <= sample["percent"] <= 100
                or type(sample["reset"]) is not int or not 0 < sample["reset"] < 2**34):
            raise ValueError("quota")
    return value


@dataclass(frozen=True)
class Accounts:
    # tuples of (service, name, selected, measured_at, samples), with no tokens or provider identifiers
    rows: tuple = ()
    retired: tuple = ()
    # A successful explicit login is historical evidence; the live credential might subsequently expire.
    logins: tuple = ()
    plans: tuple = ()
    # Locally checked credentials, independent of account selection and cached quota.
    auth: tuple = ()
    # Native Codex limits are a separate source: alias, observation time, plan, complete windows.
    # An entry with time 0 is an opt-in receipt awaiting its first successful observation.
    native_limits: tuple = ()
    # The service may still hold a separate credential after the SIWC slot changes.
    native_stale: tuple = ()

    def __post_init__(self):
        if len(self.rows) > 32:
            raise ProfileError("accounts")
        seen, selected = set(), set()
        for service, name, active, observed, samples in self.rows:
            if (service not in CLOUD or (name != "default" and not named_account(name))
                    or (service, name) in seen or type(active) is not bool or (active and service in selected)
                    or type(observed) is not int or not 0 <= observed < 2**34
                    or (observed == 0) != (samples is None)):
                raise ProfileError("accounts")
            if samples is not None:
                quota(samples, stored_at=observed)
                if all("observed_at" in sample for sample in samples.values()):
                    raise ProfileError("accounts")
            seen.add((service, name))
            if active:
                selected.add(service)

        if (len(set(self.retired)) != len(self.retired) or any(key not in seen for key in self.retired)
                or any(not named_account(name) for _, name in self.retired)):
            raise ProfileError("accounts")
        if (len({(p, n) for p, n, _ in self.logins}) != len(self.logins)
                or any(p not in CLOUD or n not in self.items(p) or type(t) is not int or not 0 < t < 2**34 for p, n, t in self.logins)):
            raise ProfileError("accounts")

        if (len({(p, n) for p, n, _, _ in self.plans}) != len(self.plans)
                or any(p not in CLOUD or n not in self.items(p) or plan not in PLANS
                       or type(t) is not int or not 0 < t < 2**34 for p, n, plan, t in self.plans)):
            raise ProfileError("accounts")

        if (len({(p, n) for p, n, _ in self.auth}) != len(self.auth)
                or any(p not in CLOUD or n not in self.items(p)
                       or status not in ("signed_in", "signed_out", "new_alias")
                       or (status == "new_alias" and not named_account(n))
                       for p, n, status in self.auth)):
            raise ProfileError("accounts")

        native_names = set()
        for name, observed, plan, windows in self.native_limits:
            if (name in native_names or name not in self.items("codex") or ("codex", name) in self.retired
                    or self.auth_status("codex", name) != "signed_in" or type(observed) is not int
                    or not 0 <= observed < 2**34 or (plan is not None and plan not in PLANS)
                    or type(windows) is not dict or (observed == 0 and (plan is not None or windows))):
                raise ProfileError("accounts")
            if windows:
                quota(windows)
            native_names.add(name)
        if (len(set(self.native_stale)) != len(self.native_stale)
                or any(name not in self.items("codex") or name in native_names for name in self.native_stale)):
            raise ProfileError("accounts")

    def without_native_limits(self, name):
        return replace(self, native_limits=tuple(row for row in self.native_limits if row[0] != name),
                       native_stale=tuple(n for n in self.native_stale if n != name))

    def invalidate_native_limits(self, name):
        if not any(row[0] == name for row in self.native_limits):
            return self
        return replace(self, native_limits=tuple(row for row in self.native_limits if row[0] != name),
                       native_stale=self.native_stale + (name,))

    def with_native_limits(self, name, *, observed=0, plan=None, windows=None):
        retained = tuple(row for row in self.native_limits if row[0] != name)
        snapshot = (name, observed, plan, {} if windows is None else windows)
        return replace(self, native_limits=retained + (snapshot,),
                       native_stale=tuple(n for n in self.native_stale if n != name))

    def auth_status(self, provider, name):
        for p, n, status in self.auth:
            if (p, n) == (provider, name):
                return status
        # Versions before the auth field kept successful named login receipts.
        return "signed_in" if any(row[:2] == (provider, name) for row in self.logins) else "unknown"

    def checked(self, provider, name, status, *, clear_usage=False):
        key = (provider, name)
        if provider not in CLOUD or name not in self.items(provider) or key in self.retired:
            raise ProfileError("account-missing")
        retained = tuple(row for row in self.auth if row[:2] != key)
        if status == "signed_in" and not clear_usage:
            return replace(self, auth=retained + ((provider, name, status),))
        rows = tuple((p, n, selected, 0, None) if (p, n) == key else (p, n, selected, t, samples)
                     for p, n, selected, t, samples in self.rows)
        state = self.invalidate_native_limits(name) if provider == "codex" else self
        return replace(state, rows=rows, plans=tuple(row for row in self.plans if row[:2] != key),
                       auth=retained + ((provider, name, status),))

    def with_plan(self, service, name, plan, observed):
        previous = next((row for row in self.plans if row[:2] == (service, name)), None)
        if previous is not None and previous[3] > observed:
            return self
        retained = tuple(r for r in self.plans if r[:2] != (service, name))
        return replace(self, plans=retained + ((service, name, plan, observed),))

    def logged_in(self, provider, name, when):
        retained = tuple(r for r in self.logins if r[:2] != (provider, name))
        # A fresh login can connect a different subscription in the default slot.
        connected = self.checked(provider, name, "signed_in", clear_usage=True)
        return replace(connected, logins=retained + ((provider, name, when),))

    def retire(self, provider, name):
        key = (provider, name)
        if not named_account(name) or name not in self.items(provider):
            raise ProfileError("account-missing")
        state = self.invalidate_native_limits(name) if provider == "codex" else self
        return replace(state, retired=tuple(sorted(set(self.retired) | {key})))

    def active(self, service):
        return next((name for provider, name, active, _, _ in self.rows if service == provider and active), "default")

    def items(self, service):
        return sorted({"default"} | {name for provider, name, *_ in self.rows if provider == service})

    def add(self, service, name):
        if not named_account(name) or name in self.items(service):
            raise ProfileError("account-exists")
        return replace(self, rows=self.rows + ((service, name, False, 0, None),))

    def select(self, service, name):
        if (service not in CLOUD or name not in self.items(service) or (service, name) in self.retired
                or self.auth_status(service, name) == "new_alias"):
            raise ProfileError("account-missing")
        return replace(self, rows=tuple((p, n, n == name if p == service else active, t, s)
                              for p, n, active, t, s in self.rows))

    def note(self, service, name, observed, samples):
        if name not in self.items(service):
            raise ProfileError("account-missing")
        if type(observed) is not int or not 0 < observed < 2**34:
            raise ValueError("quota")
        previous = next((row for row in self.rows if row[:2] == (service, name)), None)
        measured = {window: (observed, sample) for window, sample in quota(samples).items()}
        if previous and previous[4]:
            for window, sample in previous[4].items():
                when = sample.get("observed_at", previous[3])
                if window not in measured or when > observed:
                    measured[window] = (when, sample)
        newest = max(when for when, _ in measured.values())
        windows = {}
        for window, (when, sample) in measured.items():
            windows[window] = {"percent": sample["percent"], "reset": sample["reset"]}
            if when != newest:
                windows[window]["observed_at"] = when
        retained = tuple(row for row in self.rows if row[:2] != (service, name))
        return replace(self, rows=retained + ((service, name, self.active(service) == name, newest, windows),))

    def data(self):
        return {"schema": 1, "rows": list(self.rows), "retired": self.retired, "logins": self.logins, "plans": self.plans,
                "auth": self.auth, "native_limits": self.native_limits, "native_stale": self.native_stale}


def decode(raw):
    value = json.loads(raw, object_pairs_hook=_pairs)
    if (type(value) is not dict or not {"schema", "rows"} <= set(value)
            or set(value) - {"schema", "rows", "retired", "logins", "plans", "auth", "native_limits", "native_stale"} or type(value["schema"]) is not int
            or value["schema"] != 1 or type(value["rows"]) is not list
            or any(type(row) is not list or len(row) != 5 for row in value["rows"])
            or type(value.get("native_stale", [])) is not list or len(value.get("native_stale", [])) > 33
            or any(type(value.get(k, [])) is not list or len(value.get(k, [])) > 34
                   or any(type(row) is not list or len(row) != size for row in value.get(k, []))
                   for k, size in (("retired", 2), ("logins", 3), ("plans", 4), ("auth", 3), ("native_limits", 4)))):
        raise ProfileError("accounts")
    try:
        return Accounts(*(tuple(tuple(row) for row in value.get(key, []))
                          for key in ("rows", "retired", "logins", "plans", "auth", "native_limits")),
                        native_stale=tuple(value.get("native_stale", [])))
    except (TypeError, ValueError):
        raise ProfileError("accounts") from None


class AccountStore(Store):
    filename = "accounts.json"
    record_type = Accounts
    empty = staticmethod(Accounts)
    decode = staticmethod(decode)

    @contextmanager
    def lifecycle(self, provider, name):
        """Serialize a cooperating desktop's credential operation and its matching receipt."""
        if provider not in CLOUD or (name != "default" and not named_account(name)):
            raise ProfileError("account-name")
        with self._parent(create=True) as (parent, recheck):
            filename = f".auth-{provider}-{name}.lock"
            fd = os.open(filename, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
            try:
                _file(os.fstat(fd), self.uid)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ProfileError("busy") from None
                if not os.path.samestat(os.fstat(fd), os.stat(filename, dir_fd=parent, follow_symlinks=False)):
                    raise ProfileError("changed")
                recheck()
                yield
            finally:
                os.close(fd)


def claude_windows(info):
    """Pick numeric entries from a native rate_limit_event. Model text is never passed here."""
    output = {}
    windows = info.get("unifiedWindows")
    if type(windows) is not dict:
        windows = {}
    for kind in ("five_hour", "seven_day"):
        source = windows.get(kind, info if info.get("rateLimitType") == kind else None)
        if type(source) is not dict:
            continue
        fraction, reset = source.get("utilization"), source.get("resetsAt")
        if type(fraction) in (int, float) and 0 <= fraction <= 1 and type(reset) is int and 0 < reset < 2**34:
            output[kind] = {"percent": round(100 * fraction, 3), "reset": reset}
    return output


def codex_windows(info):
    """Only the main Codex pool with measured 5h/7d windows is suitable for a subscription display."""
    output = {}
    if info.get("limitId") not in (None, "codex"):
        return output
    for key in ("primary", "secondary"):
        source = info.get(key)
        if type(source) is not dict:
            continue
        kind = {300: "five_hour", 10080: "seven_day"}.get(source.get("windowDurationMins"))
        pct, reset = source.get("usedPercent"), source.get("resetsAt")
        if kind and type(pct) is int and 0 <= pct <= 100 and type(reset) is int and 0 < reset < 2**34:
            output[kind] = {"percent": pct, "reset": reset}
    return output
