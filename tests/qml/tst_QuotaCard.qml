import QtQuick
import QtTest
import "../../packaging/desktop/dms/AiDesktop/usage" as Quota

Item {
    id: scene
    width: 420
    height: 420

    Quota.QuotaCard {
        id: card
        width: scene.width
        sampledAt: 1800000000
        account: ({})
        active: true
        accentColor: "#fab387"
        surfaceColor: "#262637"
        textColor: "#cdd6f4"
        mutedColor: "#a6adc8"
        trackColor: "#45475a"
        successColor: "#a6e3a1"
        warningColor: "#f9e2af"
        dangerColor: "#f38ba8"
        fontFamily: "Sans Serif"
    }

    TestCase {
        name: "QuotaCard"
        when: windowShown

        function setReadings(windows, accountName = "personal") {
            card.account = {
                provider: "claude", account: accountName, selected: true, plan: "max",
                status: "unchecked", observed_at: card.sampledAt - 60, windows
            };
            wait(0);
        }

        function reading(percent, resetAfter = 600) {
            return { percent: percent, reset: card.sampledAt + resetAfter };
        }

        function verifyFill(name, percent) {
            const track = findChild(card, "quotaTrack_" + name);
            const fill = findChild(card, "quotaFill_" + name);
            verify(track && fill, "Missing gauge for " + name);
            verify(track.visible && track.width > 0, "Track is hidden for " + name);
            verify(Math.abs(fill.width - track.width * percent / 100) <= 1,
                   name + " should fill " + percent + "% of " + track.width + " px, got " + fill.width);
            verify(fill.width <= track.width);
        }

        function noCurrentFill(name) {
            const fill = findChild(card, "quotaFill_" + name);
            return !fill || !fill.visible || fill.width === 0;
        }

        function init() {
            scene.width = 420;
        }

        function test_observedValuesUseSeparateProportionalBars() {
            setReadings({five_hour: reading(37), seven_day: reading(81)});
            verifyFill("five_hour", 37);
            verifyFill("seven_day", 81);
            compare(findChild(card, "quotaFill_seven_day").color, card.warningColor);
        }

        function test_renderedGaugeColors() {
            setReadings({five_hour: reading(37), seven_day: reading(90)});
            const first = findChild(card, "quotaTrack_five_hour");
            const second = findChild(card, "quotaTrack_seven_day");
            const image = grabImage(card);
            function colorAt(track, fraction) {
                const p = track.mapToItem(card, track.width * fraction, track.height / 2);
                return image.pixel(Math.floor(p.x), Math.floor(p.y));
            }
            compare(colorAt(first, 0.2), card.successColor);
            compare(colorAt(first, 0.5), card.trackColor);
            compare(colorAt(second, 0.8), card.dangerColor);
            compare(colorAt(second, 0.95), card.trackColor);
        }

        function test_staleWindowKeepsItsObservationTime() {
            const older = card.sampledAt - 7200;
            const weekly = reading(31);
            weekly.observed_at = older;
            setReadings({five_hour: reading(13), seven_day: weekly});
            const stamp = findChild(card, "reportedAt_seven_day");
            verify(stamp.visible);
            compare(stamp.text, "Reported " + Qt.formatDateTime(new Date(older * 1000), "d MMM, hh:mm t"));
            verify(!findChild(card, "reportedAt_five_hour").visible);
        }

        function test_zeroAndExhausted() {
            setReadings({five_hour: reading(0), seven_day: reading(100)});
            verifyFill("five_hour", 0);
            verifyFill("seven_day", 100);
        }

        function test_partialExpiredAndUnavailable() {
            setReadings({five_hour: reading(91, -1), seven_day: reading(5)});
            verify(noCurrentFill("five_hour"), "Expired data looks like current usage");
            verifyFill("seven_day", 5);
            setReadings({five_hour: reading(22)});
            verifyFill("five_hour", 22);
            verify(noCurrentFill("seven_day"), "A missing window has an invented fill");
            setReadings({});
            verify(noCurrentFill("five_hour") && noCurrentFill("seven_day"));
            const emptyState = findChild(card, "emptyUsage");
            verify(emptyState && emptyState.visible, "Missing explicit no-data state");
        }

        function test_narrowCardKeepsGaugesInside() {
            scene.width = 245;
            setReadings({five_hour: reading(12), seven_day: reading(99)}, "a-long-synthetic-account-name");
            verifyFill("five_hour", 12);
            verifyFill("seven_day", 99);
            const track = findChild(card, "quotaTrack_seven_day");
            const right = track.mapToItem(card, track.width, track.height);
            verify(right.x <= card.width && right.y <= card.height,
                   "A gauge overflows the card bounds");
        }
    }
}
