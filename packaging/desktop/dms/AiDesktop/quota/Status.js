.pragma library

// Shared across the user's bars for the lifetime of this DMS process. Nothing is persisted.
var notifiedUntil = new Map()
var windows = [["five_hour", "5 h"], ["seven_day", "7 d"]]

function parseStatus(raw) {
    try {
        if (typeof raw !== "string" || raw.length > 1024 * 1024)
            return null
        var row = JSON.parse(raw)
        return row && row.schema === 1 && Array.isArray(row.accounts) && Array.isArray(row.sessions) ? row : null
    } catch (error) {
        return null
    }
}

function accountWindows(account, now) {
    var result = []
    if (!account || !account.windows || !Number.isInteger(account.observed_at) || account.observed_at <= 0 || account.observed_at > now)
        return result
    for (var i = 0; i < windows.length; ++i) {
        var name = windows[i][0]
        var item = account.windows[name]
        if (!item || !Number.isFinite(item.percent) || item.percent < 0 || item.percent > 100 ||
                !Number.isInteger(item.reset) || item.reset <= account.observed_at || !Number.isFinite(new Date(item.reset * 1000).getTime()))
            continue
        result.push({name: name, label: windows[i][1], percent: item.percent, reset: item.reset, current: item.reset > now})
    }
    return result
}

function alerts(snapshot, enabled, threshold, now) {
    notifiedUntil.forEach(function (end, key) {
        if (end <= now)
            notifiedUntil.delete(key)
    })
    if (!snapshot || !enabled || !Number.isFinite(threshold) || threshold < 1 || threshold > 100)
        return []
    var messages = []
    for (var account of snapshot.accounts) {
        if (!account || account.provider !== snapshot.selected || account.selected !== true || account.status === "retired")
            continue
        for (var item of accountWindows(account, now)) {
            var key = JSON.stringify([account.provider, account.account, item.name, item.reset])
            if (!item.current || item.percent < threshold || notifiedUntil.has(key))
                continue
            notifiedUntil.set(key, item.reset)
            messages.push(account.provider + " / " + account.account + " · " + item.label + " · " + item.percent + "% used")
        }
    }
    return messages
}

function providerName(id) {
    return id === "claude" ? "Claude" : id === "codex" ? "Codex" : id === "local" ? "Local" : "Unavailable";
}

function currentAccount(snapshot, provider) {
    return snapshot?.accounts?.find(a => a.provider === provider && a.selected && a.status !== "retired") || null;
}

function canSelectService(snapshot, provider) {
    if (!snapshot || !["claude", "codex", "local"].includes(provider) || snapshot.selected === provider)
        return false;
    const entry = snapshot.providers?.find(p => p.id === provider);
    return !!entry && entry.allowed !== false && (provider === "local" ? entry.allowed === true : entry.client_present === true);
}

function canSelectAccount(snapshot, provider, name) {
    return ["claude", "codex"].includes(provider) && typeof name === "string"
        && name === name.trim() && /^[a-z][a-z0-9-]{0,31}$/.test(name)
        && !!snapshot?.accounts?.some(a => a.provider === provider && a.account === name && !a.selected && a.status !== "retired");
}

function percentText(value) {
    if (value > 99.9 && value < 100)
        return ">99.9%";
    if (value > 0 && value < 0.1)
        return "<0.1%";
    return Number(value.toFixed(1)) + "%";
}

function remaining(seconds) {
    if (seconds < 60)
        return "<1m";
    const minutes = Math.floor(seconds / 60), hours = Math.floor(minutes / 60);
    if (hours >= 24)
        return Math.floor(hours / 24) + "d " + (hours % 24) + "h";
    if (hours)
        return hours + "h " + (minutes % 60) + "m";
    return minutes + "m";
}
