---
name: cachy-chroot
description: Recovery from a Live ISO using cachy-chroot and verified target mounts.
tools: cachy-chroot
source: https://wiki.cachyos.org/features/cachy_chroot/
section: Usage; Troubleshooting; FAQ
checked: 2026-11-06
version: rolling
---

# cachy-chroot recovery

The documented helper is intended for a CachyOS Live ISO. Establish whether observations describe the live environment or the installed system before interpreting versions, mounts or services.

The helper assists with choosing partitions, Btrfs subvolumes and LUKS mappings. It is an interactive recovery operation with privileged mounts, not an ordinary read-only diagnostic command. Do not run it through an unrestricted shell or claim the controller has entered the installed system.

Use the installed helper's version and help, then verify the selected root, encryption mapping, `/boot` and ESP. Do not copy example device names. A package command shown for disposable live media is not a general update recipe for an installed Arch system.

Present a recovery plan with target identity, expected changes, verification and cleanup. Entering a chroot alone does not repair the underlying failure.
