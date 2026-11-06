---
name: snapshot-tools
description: Btrfs Assistant, Snapper and package snapshots before selecting recovery.
tools: btrfs-assistant, snapper, snap-pac, btrfs-progs
source: https://wiki.cachyos.org/configuration/btrfs_snapshots/
section: Recovery Cases; Managing snapshots; Restoring from a Snapshot
checked: 2026-11-06
version: rolling
---

# Snapshot tools

Identify the filesystem, Snapper configuration, relevant subvolumes and the actual boot manager. Btrfs Assistant offers graphical snapshot management and Snapper the command-line mechanism. Package hooks such as `snap-pac` are useful only when installed and configured; do not assume an update created a usable snapshot.

Inspect the selected snapshot's scope and date. Root, home, nested subvolumes, `/boot` and the ESP may have different coverage. Snapshot presence is not a backup and does not establish that boot files and modules can be restored together.

Choose the recovery instructions for the detected boot manager and installed tool versions. Preserve the current state, explain which later data would be replaced, and require a deliberate restore decision. A successful command is not evidence of a successful boot; record that verification separately.
