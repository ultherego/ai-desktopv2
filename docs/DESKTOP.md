# Terminal launchers, Niri bindings and the DMS bar

The Arch package installs **AI Desktop** and **AI Desktop Panel** as desktop entries. Find them in your application launcher. Both request a terminal and start the same installed `ai-desktop ask` or `ai-desktop panel` you use in fish. DMS uses its configured terminal to open these entries.

## Niri with kitty

The package installs an example at `/usr/share/ai-desktop/niri-bindings.kdl`. Pick two free shortcuts and add the corresponding lines inside your existing Niri `binds` block. The example uses `Mod+Shift+A` for chat and `Mod+Shift+P` for the panel. Validate your edited configuration with `niri validate`. Installing AI Desktop does not edit the compositor configuration.

## Optional DMS bar widget

With the package installed, link its widget into the directory DMS scans. These fish commands honor `XDG_CONFIG_HOME` and leave an existing plugin of the same name in place.

```fish
set -l dms_config ~/.config
if set -q XDG_CONFIG_HOME; and test -n "$XDG_CONFIG_HOME"
    set dms_config "$XDG_CONFIG_HOME"
end
set -l dms_plugins "$dms_config/dms/plugins"
mkdir -p -- "$dms_plugins"; and ln -sT -- /usr/share/ai-desktop/dms/AiDesktop "$dms_plugins/AiDesktop"
```

Enable **AI Desktop** under **Settings → Plugins**, then add the widget in the DankBar editor. Restart the shell with `dms restart` if it has already cached the plugin directory. The panel opens when you click the bar widget. **Open chat** and **Open panel** use the installed desktop entries.

After a package update, run `dms ipc call plugins reload aiDesktop` to refresh the installed widget. If version `0.1.0-73` reports `QuotaCard is not a type`, check that both `Widget.qml` and `QuotaCard.qml` are readable in the DMS plugin directory. With the packaged files in place, `dms restart` starts a fresh QML engine. Version `0.1.0-74` imports the card explicitly and uses fresh component and script URLs so a running shell can load the new layout.

On DMS 2.0 and 2.1, use package `0.1.0-75` or later. If a previous build reports an unknown button property named `variant`, install the updated package and reload the widget.

Click the horizontal provider indicator to open **Usage**. If a current five-hour reading exists, the bar shows its percentage. The popout groups each account into a card. Its gauges show independent five-hour and seven-day usage, local reset times, remaining time and the observation timestamp. Live gauges turn yellow at 75% and red at 90%. Expired readings are marked as historical and missing readings have no fill. All accounts are reachable by scrolling; the chat and panel launchers stay below the list.

In **Accounts & login**, click the Claude or Codex card to use that service for new conversations. Click an account name to select that slot. The controls inside the card act on the displayed provider and account. The terminal shows the outcome and the account name. For actions opened from the widget, press Enter to close the result. **Sign in…** opens the account picker. A confirmed login changes the action to **Sign out…**; the terminal names the target and asks for confirmation before disconnecting it. **Manage accounts…** opens the picker again. For an account connected on an older release, click **Check sign-in…** once to check its existing isolated profile. An administrator prompt may appear. After the check, use the refresh icon in the top right if the widget is still open.

The refresh icon works on both tabs. With the optional native limits connection, an explicit click reads the current usage windows of the selected Codex slot, including while Claude is active for chat. Opening the popout and its regular timer read local status. Claude usage continues to reflect the last reported observation.

To set up live Codex limits:

1. In **Accounts & login**, check the Codex slot is signed in. For an existing login that still says **Check sign-in…**, run that check first.
2. Click **Connect live limits…**, confirm the named slot and approve the administrator prompt. The terminal opens an official device sign-in flow. Open the displayed address, enter the short code and choose the ChatGPT account and workspace whose native Codex limits you want to see. Enable device-code login in your personal security settings or ask your workspace administrator if the provider has disabled it.
3. Return to **Usage** and click the upper-right refresh icon. **Native Codex limits** identifies that separate session. Only the windows returned by Codex receive numbers. A network failure leaves the previous measurement and its timestamp visible.
4. To remove the native session locally, choose **Disconnect live limits…**. After chat sign-out or loss of the native connection, the account picker also lets you remove saved native limits credentials. **View usage in ChatGPT** opens the usage settings for the account signed in to the browser.

[Codex device authentication](https://learn.chatgpt.com/docs/auth#login-on-headless-devices) · [ChatGPT usage settings](https://chatgpt.com/settings/usage).

Notifications are disabled initially. Enable **Subscription limit notifications** in the plugin settings and choose the percentage used (default 90%). While the widget is running, a current 5-hour or 7-day window for the selected service and account can trigger one DMS toast when a sample meets the threshold. Multiple bars share notification state. Restarting DMS clears that state. To disable the widget, remove it from the bar or disable the plugin in Settings.

The widget follows the DMS 2.x plugin interface and uses the color properties provided by the released button. [DMS 2.1 plugin examples](https://github.com/AvengeMedia/DankMaterialShell/tree/7627111c3ebd402662283f5f4a93223cda9e0a9d/quickshell/PLUGINS) · [DMS 2.1 button implementation](https://github.com/duxlabs-dev/dux-qml-common/blob/be3e16ca62866a749b34cfefe1aaed81cfe21e94/DCommon/Widgets/DButton.qml).
