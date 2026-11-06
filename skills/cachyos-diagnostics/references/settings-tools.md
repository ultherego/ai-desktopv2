---
name: settings-tools
description: Diagnosing CachyOS Settings helpers, kernel summaries, memory reports and bug reports.
tools: cachyos-settings, kerver, topmem, cachyos-bugreport.sh, paste-cachyos
source: https://wiki.cachyos.org/features/cachyos_settings/
section: Scripts and Tools; Creation of overrides
checked: 2026-11-06
version: rolling
---

# CachyOS Settings helpers

Establish the installed `cachyos-settings` package version and the specific helper's available options before using it. Treat this rolling source as orientation, not proof of the installed implementation.

- `kerver` summarizes kernel and scheduler information. Compare it with the running kernel and the relevant package; do not infer that an installed kernel is active.
- `topmem` reports process memory, swap and KSM. Its process view inside a sandbox may differ from the desktop host.
- `cachyos-bugreport.sh` collects a broad report; `paste-cachyos` sends content to a paste service. Neither is a passive minimal diagnostic read. Collection, local output and external sharing need their own scope and decision.
- Settings include sysctl and udev configuration. Read the installed files and effective values before proposing an override; the wiki's example values are not a universal tuning preset.

Choose one helper only when its output answers the current question. Keep exact version, observation and uncertainty in the result.
