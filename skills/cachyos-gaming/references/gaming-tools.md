---
name: gaming-tools
description: CachyOS gaming metapackages, Proton, Wine, game-performance and process-priority conflicts.
tools: cachyos-gaming-meta, cachyos-gaming-applications, proton-cachyos, proton-cachyos-slr, wine-cachyos-opt, game-performance, gamemode, ananicy-cpp
source: https://wiki.cachyos.org/configuration/gaming/
section: Essential Packages; Proton-CachyOS; Wine-CachyOS; Performance and Misc Tips
checked: 2026-11-06
version: rolling
---

# CachyOS gaming tools

Inspect installed versions and the per-game runner before suggesting changes. `cachyos-gaming-meta` groups gaming dependencies; `cachyos-gaming-applications` groups launchers and tools. Neither proves that a driver or title works.

Distinguish Proton-CachyOS variants from Wine-CachyOS. `wine-cachyos-opt` permits a separate Wine installation; do not replace the system Wine or global Steam default for a single-game trial. Verify flags in the selected runner's release documentation.

`game-performance` temporarily changes power-profile behavior around a launched process. Adding it to launch options is a state-changing trial. Preserve the complete prior launch string and verify the original profile after exit.

The guide warns that GameMode and `ananicy-cpp` can both adjust process priority. Check what is active and what each rule does before a trial; do not stop a service automatically.

Use MangoHud or Gamescope only when relevant and available. Compare repeatable frame-time, launch or stability measurements. An installed package or a single FPS value is not proof of improvement.
