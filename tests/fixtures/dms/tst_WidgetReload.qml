import QtQuick
import QtTest

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

        function test_recoverWithoutRestart_data() {
            return [{tag: "missing card", state: "missing"},
                    {tag: "cached card and script", state: "cached"}];
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
                              sessions: [], providers: [], accounts: [
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
        }
    }
}
