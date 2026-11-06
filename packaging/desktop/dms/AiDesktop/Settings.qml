import QtQuick
import qs.Modules.Plugins

PluginSettings {
    pluginId: "aiDesktop"

    ToggleSetting {
        settingKey: "notifyUsage"
        label: "Subscription limit notifications"
        description: "Show a toast for the active account once per window while the widget is running."
        defaultValue: false
    }

    SliderSetting {
        settingKey: "usageThreshold"
        label: "Notify when usage reaches"
        minimum: 1
        maximum: 100
        defaultValue: 90
        unit: "%"
    }
}
