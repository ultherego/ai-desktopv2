"""Non-secret account names, selections and numeric usage snapshots in a private desktop store."""
from dataclasses import dataclass, replace
import json
import re

from .agent_profiles import Store, ProfileError, _pairs

CLOUD = ("claude", "codex")
_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}\Z", re.ASCII)


def named_account(value):
    return type(value) is str and value != "default" and _NAME.fullmatch(value) is not None


def quota(value):
    """Only recognized time windows and finite numeric provider readings cross the trust boundary."""
    if type(value) is not dict or not value or set(value) - {"five_hour", "seven_day"}:
        raise ValueError("quota")
    for sample in value.values():
        if (type(sample) is not dict or set(sample) != {"percent", "reset"}
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
                quota(samples)
            seen.add((service, name))
            if active:
                selected.add(service)

        if (len(set(self.retired)) != len(self.retired) or any(key not in seen for key in self.retired)
                or any(not named_account(name) for _, name in self.retired)):
            raise ProfileError("accounts")
        if (len({(p, n) for p, n, _ in self.logins}) != len(self.logins)
                or any((p, n) not in seen or type(t) is not int or not 0 < t < 2**34 for p, n, t in self.logins)):
            raise ProfileError("accounts")

    def logged_in(self, provider, name, when):
        retained = tuple(r for r in self.logins if r[:2] != (provider, name))
        return replace(self, logins=retained + ((provider, name, when),))

    def retire(self, provider, name):
        key = (provider, name)
        if not named_account(name) or name not in self.items(provider):
            raise ProfileError("account-missing")
        return replace(self, retired=tuple(sorted(set(self.retired) | {key})))

    def active(self, service):
        return next((name for provider, name, active, _, _ in self.rows if service == provider and active), "default")

    def items(self, service):
        return sorted({"default"} | {name for provider, name, *_ in self.rows if provider == service})

    def add(self, service, name):
        if not named_account(name) or name in self.items(service):
            raise ProfileError("account-exists")
        return replace(self, rows=self.rows + ((service, name, False, 0, None),))

    def select(self, service, name):
        if service not in CLOUD or name not in self.items(service) or (service, name) in self.retired:
            raise ProfileError("account-missing")
        return replace(self, rows=tuple((p, n, n == name if p == service else active, t, s)
                              for p, n, active, t, s in self.rows))

    def note(self, service, name, observed, samples):
        if name not in self.items(service):
            raise ProfileError("account-missing")
        previous = next((row for row in self.rows if row[:2] == (service, name)), None)
        if previous is not None and previous[3] > observed:
            return self
        retained = tuple(row for row in self.rows if row[:2] != (service, name))
        return replace(self, rows=retained + ((service, name, self.active(service) == name, observed, quota(samples)),))

    def data(self):
        return {"schema": 1, "rows": list(self.rows), "retired": self.retired, "logins": self.logins}


def decode(raw):
    value = json.loads(raw, object_pairs_hook=_pairs)
    if (type(value) is not dict or set(value) != {"schema", "rows", "retired", "logins"} or type(value["schema"]) is not int
            or value["schema"] != 1 or type(value["rows"]) is not list
            or any(type(row) is not list or len(row) != 5 for row in value["rows"])
            or any(type(value[k]) is not list or len(value[k]) > 32
                   or any(type(row) is not list or len(row) != size for row in value[k])
                   for k, size in (("retired", 2), ("logins", 3)))):
        raise ProfileError("accounts")
    try:
        return Accounts(*(tuple(tuple(row) for row in value[key]) for key in ("rows", "retired", "logins")))
    except (TypeError, ValueError):
        raise ProfileError("accounts") from None


class AccountStore(Store):
    filename = "accounts.json"
    record_type = Accounts
    empty = staticmethod(Accounts)
    decode = staticmethod(decode)


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
