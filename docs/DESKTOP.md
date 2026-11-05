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

The widget reads `ai-desktop status --json` at startup and every 60 seconds. It displays the selected service, role, model, number of active conversations and the last reported usage per account. Reset and observation times use your local time zone. Missing data displays **Usage not reported**. A past reset displays **expired** until the provider reports a new window during ordinary use. Reading status does not log in, query providers or refresh quota data.

Notifications are disabled initially. Enable **Subscription limit notifications** in the plugin settings and choose the percentage used (default 90%). While the widget is running, a current 5-hour or 7-day window for the selected service and account can trigger one DMS toast when a sample meets the threshold. Multiple bars share notification state. Restarting DMS clears that state. To disable the widget, remove it from the bar or disable the plugin in Settings.

The widget uses the [upstream DMS plugin interface](https://github.com/AvengeMedia/DankMaterialShell/tree/c4b6303d72f9fb53d00fbf28f557313469bedc52/quickshell/PLUGINS).
