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

Click the horizontal provider indicator to open **Usage**. If a current five-hour reading exists, the bar shows its percentage. The popout groups each account into a card. Its gauges show independent five-hour and seven-day usage, local reset times, remaining time and the observation timestamp. Live gauges turn yellow at 75% and red at 90%. Expired readings are marked as historical and missing readings have no fill. All accounts are reachable by scrolling; the chat and panel launchers stay below the list.

Open **Accounts & login** to switch the service for future conversations. When a provider has multiple available slots, click a name to select it for new tasks. Choose **Sign in…** under Claude or Codex to open a dedicated terminal picker. The arrow keys select the default login, an available named slot or the option to add an account. Entering a new alias, granting sudo and completing provider authentication can still require interaction. Existing conversations keep their approved recipient. Local inference has no subscription login.

The widget reads the local `ai-desktop status --json` on startup and every 60 seconds. Opening either tab does not initiate authentication or query a provider. Account and service changes require a click. Sign-in uses the packaged launcher for that provider.

Notifications are disabled initially. Enable **Subscription limit notifications** in the plugin settings and choose the percentage used (default 90%). While the widget is running, a current 5-hour or 7-day window for the selected service and account can trigger one DMS toast when a sample meets the threshold. Multiple bars share notification state. Restarting DMS clears that state. To disable the widget, remove it from the bar or disable the plugin in Settings.

The widget uses the [upstream DMS plugin interface](https://github.com/AvengeMedia/DankMaterialShell/tree/c4b6303d72f9fb53d00fbf28f557313469bedc52/quickshell/PLUGINS).
