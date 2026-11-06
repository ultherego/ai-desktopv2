---
name: cachy-update
description: Cachy-Update checks, tray visibility and updates are separate behaviors.
tools: cachy-update, arch-update, systemd
source: https://github.com/CachyOS/cachy-update
section: Usage; Documentation; Modify the check cycle
checked: 2026-11-06
version: rolling
---

# Cachy-Update

Read the installed package version, supported `cachy-update --help` output and relevant installed units. Some integration names retain `arch-update`; do not rename a unit from the application's display name.

Distinguish three states: periodic checking, the graphical tray and an actual package transaction. The documented check schedule uses the user unit `arch-update.timer`; tray startup may use XDG Autostart. A hidden tray therefore does not prove that checks stopped. GNOME tray support can require an extension.

Inspect the actual timer and overrides before describing its schedule. Follow the installed release's help and man pages when flags differ from this rolling README. Do not copy a schedule example as the user's effective configuration.

Enabling, disabling or starting a check can change local state or use the network. Installing updates additionally changes packages and runs hooks. Present the intended scope separately; do not uninstall the program or change every user's units to disable one user's check.
