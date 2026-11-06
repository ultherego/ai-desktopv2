---
name: scheduler-tools
description: scx_loader, scxctl and SCX Manager profiles selected against installed capabilities.
tools: scx_loader, scxctl, scx-manager, cachyos-kernel-manager
source: https://wiki.cachyos.org/configuration/sched-ext/
section: How to Launch and Manage the Scheduler; scx_loader Configuration; Configuration and performance testing
checked: 2026-11-06
version: rolling
---

# Scheduler tools and profiles

Record the installed manager/client versions and the active kernel. Establish whether management comes from `scx_loader`, a standalone scheduler service or an installed graphical SCX manager. Do not infer the backend or executable solely from a window title.

Select commands and profiles from the installed client's help and available scheduler list. The rolling guide's scheduler catalog changes; an absent profile is not a reason to invent flags or replace the manager.

Separate the kernel's active scheduler from the service state and configured boot default. A profile may also follow power-profile changes. Diagnose competing control paths before considering a service change.

For a trial, preserve the exact starting scheduler, mode and settings. Use repeated measurements under the same load. A restore-to-default action can differ from restoring the starting state. Record both the change and recovery result; do not promise a universal gaming or latency gain.
