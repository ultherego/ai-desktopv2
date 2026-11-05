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
        if (!account || account.provider !== snapshot.selected || account.selected !== true)
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
