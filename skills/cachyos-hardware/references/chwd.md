---
name: chwd
description: chwd profile inventory and driver changes on different hardware vendors.
tools: chwd
source: https://wiki.cachyos.org/features/chwd/chwd/
section: Usage; Others
checked: 2026-11-06
version: rolling
---

# chwd

Match the installed `chwd` version with its own help before selecting flags. The documented interface distinguishes `--list-installed`, `--list` and `--list-all`; confirm their meaning in the installed release and keep device/profile evidence together.

Listing profiles does not prove that a driver is bound to the current device. Compare PCI/USB identity, loaded driver and relevant kernel package. Support AMD, Intel, NVIDIA and mixed-GPU systems by their actual identifiers, without a default vendor assumption.

`--autoconfigure` (`-a`), install and removal are system changes. Never use autoconfiguration as a harmless hardware probe. Before proposing a driver change, establish module compatibility, package effects, a working recovery path and a separate decision. Verify the original device symptom after application and any required controlled restart.
