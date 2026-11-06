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
            if (!item || !item.visible)
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

        function exerciseAccountActions(widget, popout, snapshot, provider, account) {
            const refresh = findChild(popout, "statusRefresh");
            verify(refresh && refresh.visible);
            const corner = refresh.mapToItem(popout, refresh.width, 0);
            verify(corner.x >= popout.width - 1 && corner.y < 40, "Refresh belongs in the top right corner");
            if (provider.id === "claude") {
                for (const state of ["unknown", "signed_out"]) {
                    account.auth_state = state;
                    Proc.statusReply = JSON.stringify(snapshot);
                    widget.refresh();
                    clickButton(popout, "Manage accounts…");
                    compare(SessionService.lastLaunched, "ai-desktop-login-claude.desktop");
                    compare(Proc.changes.length, 0, "Managing another provider must preserve the active service");
                }
                account.auth_state = "unknown";
                Proc.statusReply = JSON.stringify(snapshot);
                widget.refresh();
            }
            clickButton(popout, "Sign in…");
            compare(SessionService.lastLaunched, "ai-desktop-login-" + provider.id + ".desktop");
            clickButton(popout, "Check sign-in…");
            compare(SessionService.lastLaunched, "ai-desktop-check-sign-in.desktop");
            compare(JSON.stringify(SessionService.lastArgs), JSON.stringify([provider.id, account.account]));

            const oldAlias = account.account;
            account.auth_state = "signed_in";
            account.account = provider.id === "claude" ? "default" : "work";
            snapshot.accounts.push({provider: provider.id, account: "spare", selected: false, auth_state: "signed_out"});
            Proc.statusReply = JSON.stringify(snapshot);
            mouseClick(refresh);
            clickButton(popout, "Sign out…");
            compare(SessionService.lastLaunched, "ai-desktop-logout-" + provider.id + ".desktop");
            compare(JSON.stringify(SessionService.lastArgs), JSON.stringify([account.account]));
            SessionService.lastLaunched = "";
            widget.accountAction(provider.id, oldAlias, "logout");
            widget.accountAction(provider.id, "--help", "logout");
            compare(SessionService.lastLaunched, "");
            clickButton(popout, "Manage accounts…");
            compare(SessionService.lastLaunched, "ai-desktop-login-" + provider.id + ".desktop");
            const selected = visibleButton(popout, account.account);
            verify(selected && !selected.enabled);
            mouseClick(selected);
            compare(Proc.changes.length, 0, "Nested buttons must never activate the provider card");
            clickButton(popout, "spare");
            compare(JSON.stringify(Proc.changes), JSON.stringify([["/usr/bin/ai-desktop", "account", "use", provider.id, "spare"]]));
            Proc.changes = [];

            function hit() { return findChild(popout, "chooseService_" + provider.id); }
            tryVerify(() => hit() && hit().enabled);
            mouseClick(hit(), 15, 14);
            compare(JSON.stringify(Proc.changes), JSON.stringify([["/usr/bin/ai-desktop", "agent", "use", provider.id]]));
            hit().forceActiveFocus();
            keyClick(Qt.Key_Return);
            compare(Proc.changes.length, 2);
            provider.allowed = false;
            Proc.statusReply = JSON.stringify(snapshot);
            mouseClick(refresh);
            tryVerify(() => hit() && !hit().enabled);
            mouseClick(hit(), 15, 14);
            compare(Proc.changes.length, 2, "Blocked provider card must ignore clicks");
        }

        function exerciseLiveLimits(widget, popout, snapshot, provider, account, host) {
            const refresh = findChild(popout, "statusRefresh");
            const error = findChild(popout, "liveLimitsError");
            provider.allowed = true;
            Proc.statusReply = JSON.stringify(snapshot);
            widget.refresh();
            clickButton(popout, "Connect live limits…");
            compare(SessionService.lastLaunched, "ai-desktop-codex-limits.desktop");
            compare(JSON.stringify(SessionService.lastArgs), JSON.stringify([account.account]));

            account.live_limits_connected = true;
            Proc.statusReply = JSON.stringify(snapshot);
            host.shouldBeVisible = false;
            host.shouldBeVisible = true;
            widget.refresh();
            compare(Proc.limitReads.length, 0, "Passive refresh cannot fetch a native quota");
            widget.fetchLiveLimits("--help");
            widget.manageLiveLimits("spare", true);
            compare(Proc.limitReads.length, 0);
            compare(SessionService.lastLaunched, "ai-desktop-codex-limits.desktop");

            mouseClick(refresh);
            compare(JSON.stringify(Proc.limitReads), JSON.stringify([
                ["/usr/bin/ai-desktop", "usage", "--refresh", "--provider", "codex", "--account", account.account, "--json"]
            ]));
            verify(!refresh.enabled);
            widget.fetchLiveLimits(account.account);
            compare(Proc.limitReads.length, 1, "In-flight refresh cannot be duplicated");

            account.quota_source = "codex_native";
            const now = Math.floor(Date.now() / 1000);
            account.observed_at = now - 1;
            account.windows = {five_hour: {percent: 41, reset: now + 3600}};
            Proc.statusReply = JSON.stringify(snapshot);
            Proc.finishLimits("unused output", 0);
            tryVerify(() => widget.codexAccount.quota_source === "codex_native" && refresh.enabled);
            verify(!findChild(popout, "nativeQuotaSource_" + account.account),
                   "A native reading should render directly in its account card");
            const track = findChild(popout, "quotaTrack_five_hour");
            const gauge = findChild(popout, "quotaFill_five_hour");
            verify(track && gauge && Math.abs(gauge.width - track.width * 0.41) < 1);

            mouseClick(refresh);
            Proc.finishLimits('{"error":"network","private":"PRIVATE-RAW-ERROR"}', 2);
            tryVerify(() => error.visible && error.text.includes(account.account));
            verify(!error.text.includes("PRIVATE-RAW-ERROR"));
            compare(widget.codexAccount.observed_at, now - 1);
            compare(widget.codexAccount.windows.five_hour.percent, 41);

            // Switching slots while the broker is working must not redirect a response or its error.
            mouseClick(refresh);
            const original = account.account;
            account.account = "other";
            account.windows = {};
            account.quota_source = null;
            account.live_limits_connected = false;
            Proc.statusReply = JSON.stringify(snapshot);
            widget.refresh();
            Proc.finishLimits('{"error":"auth-required"}', 2);
            tryVerify(() => widget.codexAccount.account === "other" && refresh.enabled);
            verify(!error.visible);
            compare(Proc.limitReads[2][6], original);

            provider.allowed = false;
            account.live_limits_connected = true;
            Proc.statusReply = JSON.stringify(snapshot);
            mouseClick(refresh);
            compare(Proc.limitReads.length, 3, "Policy denial prevents a live network request");
            verify(error.visible && error.text.includes("policy"));
            clickButton(popout, "Accounts & login");
            clickButton(popout, "Disconnect live limits…");
            compare(SessionService.lastLaunched, "ai-desktop-codex-limits-disconnect.desktop");
            compare(JSON.stringify(SessionService.lastArgs), JSON.stringify(["other"]));

            // A separately authorized credential must remain removable after SIWC sign-out or policy changes.
            account.live_limits_connected = false;
            account.native_limits_cleanup_pending = true;
            account.auth_state = "signed_out";
            SessionService.lastLaunched = "";
            Proc.statusReply = JSON.stringify(snapshot);
            mouseClick(refresh);
            clickButton(popout, "Disconnect live limits…");
            compare(SessionService.lastLaunched, "ai-desktop-codex-limits-disconnect.desktop");
            compare(JSON.stringify(SessionService.lastArgs), JSON.stringify(["other"]));
            compare(Proc.limitReads.length, 3);
            verify(!visibleButton(popout, "Reconnect live limits…"));

            account.auth_state = "signed_in";
            provider.allowed = true;
            Proc.statusReply = JSON.stringify(snapshot);
            mouseClick(refresh);
            clickButton(popout, "Reconnect live limits…");
            compare(SessionService.lastLaunched, "ai-desktop-codex-limits.desktop");
            verify(visibleButton(popout, "Disconnect live limits…"));

            // An otherwise unavailable current slot still offers access to the picker for a retired link.
            account.auth_state = "signed_out";
            account.native_limits_cleanup_pending = false;
            provider.allowed = false;
            provider.client_present = false;
            snapshot.accounts.push({provider: "codex", account: "retired", status: "retired", native_limits_cleanup_pending: true});
            Proc.statusReply = JSON.stringify(snapshot);
            mouseClick(refresh);
            clickButton(popout, "Manage accounts…");
            compare(SessionService.lastLaunched, "ai-desktop-login-codex.desktop");
            compare(Proc.limitReads.length, 3);
            clickButton(popout, "Usage");
        }

        function verifyButtonInside(card, label) {
            const button = visibleButton(card, label);
            verify(button, label + " should be inside the painted Codex card");
            const top = button.mapToItem(card, 0, 0);
            const bottom = button.mapToItem(card, button.width, button.height);
            verify(top.x > 0 && top.y > 0 && bottom.x < card.width && bottom.y < card.height,
                   label + " should fit inside the card border");
        }

        function exerciseUsageLayout(widget, popout) {
            const now = Math.floor(Date.now() / 1000);
            const alias = "long-synthetic-codex-account-123";
            const codex = {provider: "codex", account: alias, selected: true, auth_state: "signed_in",
                           plan: "pro", observed_at: now, windows: {}};
            const accounts = [codex, {provider: "codex", account: "other", selected: false,
                                      auth_state: "signed_out", windows: {}}];
            for (let index = 0; index < 10; index++)
                accounts.push({provider: "claude", account: "another-" + index, selected: index === 0,
                               auth_state: "signed_in", windows: {}});
            const snapshot = {schema: 1, selected: "claude", role: "diagnostic", model: "default",
                              sessions: [], providers: [{id: "codex", allowed: true, client_present: true}],
                              accounts};
            widget.parentScreen = {height: 1080, width: 1920};
            popout.width = 300;
            Proc.statusReply = JSON.stringify(snapshot);
            widget.refresh();
            const list = findChild(popout, "usageList");
            verify(list);
            tryCompare(list, "count", accounts.length);
            list.positionViewAtBeginning();
            tryVerify(() => list.height > 370,
                      5000, "The usable list viewport should grow beyond the old 340px cap");
            const cardName = "usageCard_codex_" + alias;
            tryVerify(() => findChild(popout, cardName) !== null);
            const card = findChild(popout, cardName);
            verify(card.border && card.border.width > 0 && card.color.a > 0, "Expected the visible card surface");
            verifyButtonInside(card, "View usage in ChatGPT");
            verifyButtonInside(card, "Connect live limits…");

            function surface() { return findChild(popout, "selectUsage_codex_" + alias); }
            tryVerify(() => surface() && surface().enabled);
            Proc.changes = [];
            compare(Qt.platform.pluginName, "offscreen");
            // The offscreen platform acknowledges the URL but cannot launch a browser.
            ignoreWarning("This plugin does not support QPlatformServices::openUrl() for 'https://chatgpt.com/settings/usage'.");
            clickButton(card, "View usage in ChatGPT");
            compare(Proc.changes.length, 0, "The browser link must not activate Codex");
            clickButton(card, "Connect live limits…");
            compare(JSON.stringify(SessionService.lastArgs), JSON.stringify([alias]));
            const connect = visibleButton(card, "Connect live limits…");
            connect.enabled = false;
            mouseClick(connect);
            compare(Proc.changes.length, 0, "The nested button, including when disabled, must consume its own area");
            mouseClick(surface(), 15, 14);
            compare(JSON.stringify(Proc.changes), JSON.stringify([["/usr/bin/ai-desktop", "agent", "use", "codex"]]));
            surface().forceActiveFocus();
            keyClick(Qt.Key_Return);
            compare(Proc.changes.length, 2, "The usage card must also support keyboard selection");
            const other = findChild(popout, "selectUsage_codex_other");
            verify(other);
            mouseClick(other, 15, 14);
            compare(JSON.stringify(Proc.changes[2]), JSON.stringify(["/usr/bin/ai-desktop", "agent", "use", "codex"]));
            compare(Proc.changes.length, 3, "Selecting the service must preserve its selected account");
            snapshot.selected = "codex";
            Proc.statusReply = JSON.stringify(snapshot);
            widget.refresh();
            tryVerify(() => surface() && !surface().enabled);
            mouseClick(surface(), 15, 14);
            compare(Proc.changes.length, 3, "Clicking the active service is a no-op");
            snapshot.selected = "claude";
            snapshot.providers[0].allowed = false;
            Proc.statusReply = JSON.stringify(snapshot);
            widget.refresh();
            tryVerify(() => surface() && !surface().enabled);
            mouseClick(surface(), 15, 14);
            compare(Proc.changes.length, 3, "A blocked Codex account must not activate from Usage");
            snapshot.providers[0].allowed = true;

            // Existing readings also allow connecting while the browser action is hidden.
            codex.windows = {five_hour: {percent: 19, reset: now + 600}};
            codex.native_limits_cleanup_pending = true;
            Proc.statusReply = JSON.stringify(snapshot);
            widget.refresh();
            tryVerify(() => visibleButton(findChild(popout, cardName), "Reconnect live limits…") !== null);
            const measured = findChild(popout, cardName);
            verifyButtonInside(measured, "Reconnect live limits…");
            verify(!visibleButton(measured, "View usage in ChatGPT"));

            const desktopViewport = list.height;
            widget.parentScreen = {height: 480, width: 854};
            tryVerify(() => list.height < desktopViewport && list.contentHeight > list.height);
            const footer = visibleButton(popout, "Open panel");
            verify(footer);
            tryVerify(() => footer.mapToItem(popout, 0, footer.height).y <= 480, 1000,
                      "Footer must fit on a short screen (content " + popout.implicitHeight +
                      ", list " + list.height + ", footer " + footer.mapToItem(popout, 0, footer.height).y + ")");
            const footerBefore = footer.mapToItem(popout, 0, footer.height);
            list.positionViewAtEnd();
            const lastCard = "usageCard_claude_another-9";
            tryVerify(() => findChild(popout, lastCard) !== null);
            const last = findChild(popout, lastCard);
            tryVerify(() => {
                const lastBottom = last.mapToItem(list, 0, last.height).y;
                return lastBottom > 0 && lastBottom <= list.height + 1;
            });
            compare(footer.mapToItem(popout, 0, footer.height).y, footerBefore.y);
            // The attached panel can give DMS less room than the full monitor.
            popout.parentPopout.maxBodyHeight = 330;
            tryVerify(() => footer.mapToItem(popout, 0, footer.height).y + 2 * PopoutMetrics.contentPadding <= 331);
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

            install(data.state, "old-quota");
            const quotaPackage = loadWidget(2);
            compare(quotaPackage.status, Component.Ready, quotaPackage.errorString());
            verify(createTemporaryObject(quotaPackage, stage).oldCard);

            install(data.state, "upgrade");
            const updated = loadWidget(3);
            compare(updated.status, Component.Ready, updated.errorString());

            const now = Math.floor(Date.now() / 1000);
            const account = {provider: data.provider, account: "personal", selected: true, auth_state: "unknown",
                             observed_at: now - 100, windows: {five_hour: {percent: 19, reset: now + 900}}};
            const provider = {id: data.provider, allowed: true, client_present: true};
            const snapshot = {schema: 1, selected: data.provider === "claude" ? "codex" : "claude",
                              role: "diagnostic", model: "default", sessions: [], providers: [provider], accounts: [account]};
            Proc.statusReply = JSON.stringify(snapshot);
            Proc.changes = [];
            Proc.limitReads = [];
            const widget = createTemporaryObject(updated, stage, {snapshot, sampledAt: now});
            verify(widget);
            const popout = createTemporaryObject(widget.popoutContent, stage, {width: 420});
            verify(popout);
            const host = Qt.createQmlObject("import QtQuick; QtObject { property bool shouldBeVisible: false; property real maxBodyHeight: 0 }", popout);
            popout.parentPopout = host;
            const openedAfter = Proc.statusCalls;
            host.shouldBeVisible = true;
            tryVerify(() => Proc.statusCalls > openedAfter, 5000, "Opening the widget should refresh local status");
            tryVerify(() => findChild(popout, "quotaAccountTitle") !== null);
            compare(findChild(popout, "quotaAccountTitle").text, (data.provider === "claude" ? "Claude" : "Codex") + "  /  personal");
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
            exerciseAccountActions(widget, popout, snapshot, provider, account);
            clickButton(popout, "Usage");
            const refresh = findChild(popout, "statusRefresh");
            const before = Proc.statusCalls;
            mouseClick(refresh);
            tryVerify(() => Proc.statusCalls > before);
            if (data.provider === "codex") {
                account.windows = {};
                Proc.statusReply = JSON.stringify(snapshot);
                mouseClick(refresh);
                tryVerify(() => visibleButton(popout, "View usage in ChatGPT") !== null);
                exerciseLiveLimits(widget, popout, snapshot, provider, account, host);
            }
            clickButton(popout, "Open chat");
            compare(SessionService.lastLaunched, "ai-desktop.desktop");
            clickButton(popout, "Open panel");
            compare(SessionService.lastLaunched, "ai-desktop-panel.desktop");
            if (data.provider === "codex")
                exerciseUsageLayout(widget, popout);
        }
    }
}
