// Run the widget's actual QML JavaScript on synthetic local snapshots.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const url = new URL("../packaging/desktop/dms/AiDesktop/Status.js", import.meta.url);
const source = readFileSync(url, "utf8").replace(/^\.pragma library\s*/, "");
const widget = vm.createContext({});
vm.runInContext(source, widget);
const now = 1800000000;
const sample = {provider: "codex", account: "personal", selected: true, observed_at: now - 60,
                windows: {five_hour: {percent: 95, reset: now + 600}, seven_day: {percent: 70, reset: now + 86400}}};
function status(account = sample, selected = "codex") {
    return widget.parseStatus(JSON.stringify({schema: 1, selected, sessions: [], accounts: [account]}));
}
assert.equal(widget.parseStatus("unavailable"), null);
assert.equal(widget.parseStatus('{"schema":1}'), null);
assert.equal(widget.parseStatus('{"schema":2,"accounts":[],"sessions":[]}'), null);
assert.equal(widget.accountWindows({windows: {}, observed_at: null}, now).length, 0);
assert.equal(widget.accountWindows({...sample, observed_at: now + 1}, now).length, 0);
assert.equal(widget.accountWindows({...sample, windows: {five_hour: {percent: 101, reset: now + 60}}}, now).length, 0);
assert.equal(widget.accountWindows(sample, now + 600)[0].current, false);
assert.equal(widget.alerts(status(), false, 90, now).length, 0);
assert.equal(widget.alerts(status(sample, "local"), true, 90, now).length, 0);
assert.equal(widget.alerts(status({...sample, selected: false}), true, 90, now).length, 0);
assert.equal(widget.alerts(status(), true, 96, now).length, 0);
assert.deepEqual(Array.from(widget.alerts(status(), true, 95, now)), ["codex / personal · 5 h · 95% used"]);
assert.equal(widget.alerts(status(), true, 95, now + 60).length, 0);
assert.equal(widget.alerts(status(), true, 95, now + 601).length, 0);
const next = {...sample, observed_at: now + 610, windows: {five_hour: {percent: 96, reset: now + 2000}}};
assert.deepEqual(Array.from(widget.alerts(status(next), true, 95, now + 620)), ["codex / personal · 5 h · 96% used"]);
console.log("Desktop usage, expiry and notification checks passed.");
