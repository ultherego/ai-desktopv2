import QtQuick
import QtTest
import qs.Common
import qs.Services

Item {
    id: stage
    width: 420
    height: 440

    TestCase {
        name: "WidgetReload"
        when: windowShown
        property string plugin

        function loadWidget(revision) {
            const file = Qt.resolvedUrl(plugin + "Widget.qml");
            return Qt.createComponent(revision ? file + "?revision=" + revision : file,
                                      Component.PreferSynchronous, stage);
        }

        function install(state, phase) {
            console.info("INSTALL_TEST_PACKAGE " + state + " " + phase);
            tryVerify(() => {
                const request = new XMLHttpRequest();
                request.open("GET", Qt.resolvedUrl(state + "/phase"), false);
                request.send();
                return request.responseText === phase;
            }, 10000, "Timed out installing " + state + " / " + phase);
        }

        function visibleButton(item, label) {
            if (!item.visible)
                return null;
            if (item.text === label && typeof item.clicked === "function")
                return item;
            for (let index = 0; index < item.children.length; index++) {
                const found = visibleButton(item.children[index], label);
                if (found)
                    return found;
            }
            return null;
        }

        function clickButton(popout, label) {
            let button;
            tryVerify(() => {
                button = visibleButton(popout, label);
                return button && button.enabled && button.width > 0 && button.height > 0;
            }, 5000, "Expected an enabled " + label + " button");
            waitForRendering(popout);
            mouseClick(button);
        }

        function test_recoverWithoutRestart_data() {
            return [{tag: "missing card", state: "missing", provider: "claude"},
                    {tag: "cached card and script", state: "cached", provider: "codex"}];
        }

        function test_recoverWithoutRestart(data) {
            plugin = data.state + "/plugins/AiDesktop/";
            const old = loadWidget(0);
            compare(old.status, Component.Ready, old.errorString());
            const previous = createTemporaryObject(old, stage);
            verify(previous);
            compare(previous.oldCount, 0); // The old Status.js is in this engine's cache.

            install(data.state, "old-card");
            const previousCard = loadWidget(1);
            if (data.state === "missing") {
                compare(previousCard.status, Component.Error);
                verify(previousCard.errorString().includes("QuotaCard is not a type"), previousCard.errorString());
            } else {
                compare(previousCard.status, Component.Ready, previousCard.errorString());
            }

            install(data.state, "upgrade");
            const updated = loadWidget(2);
            compare(updated.status, Component.Ready, updated.errorString());

            const snapshot = {selected: "claude", role: "diagnostic", model: "default",
                              sessions: [], providers: [{id: data.provider, allowed: true, client_present: true}], accounts: [
                                  {provider: "claude", account: "personal", selected: true,
                                   observed_at: 1799999900, windows: {five_hour: {percent: 19, reset: 1800000900}}}
                              ]};
            const widget = createTemporaryObject(updated, stage, {snapshot, sampledAt: 1800000000});
            verify(widget);
            const popout = createTemporaryObject(widget.popoutContent, stage, {width: 420});
            verify(popout);
            tryVerify(() => findChild(popout, "quotaAccountTitle") !== null);
            compare(findChild(popout, "quotaAccountTitle").text, "Claude  /  personal");
            const gauge = findChild(popout, "quotaFill_five_hour");
            const track = findChild(popout, "quotaTrack_five_hour");
            verify(gauge && track && track.visible && track.width > 0);
            verify(Math.abs(gauge.width - track.width * 0.19) <= 1, "Expected a visible 19% quota bar");

            SessionService.lastLaunched = "";
            const usage = visibleButton(popout, "Usage");
            const accounts = visibleButton(popout, "Accounts & login");
            verify(usage && accounts);
            compare(usage.backgroundColor, Theme.primaryContainer);
            compare(accounts.backgroundColor, Theme.surfaceContainerHigh);
            clickButton(popout, "Accounts & login");
            compare(usage.backgroundColor, Theme.surfaceContainerHigh);
            compare(accounts.backgroundColor, Theme.primaryContainer);
            clickButton(popout, "Sign in…");
            compare(SessionService.lastLaunched, "ai-desktop-login-" + data.provider + ".desktop");
            clickButton(popout, "Usage");
            tryCompare(track, "visible", true);
            clickButton(popout, "Open chat");
            compare(SessionService.lastLaunched, "ai-desktop.desktop");
            clickButton(popout, "Open panel");
            compare(SessionService.lastLaunched, "ai-desktop-panel.desktop");
        }
    }
}
