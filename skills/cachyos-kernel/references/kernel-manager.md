---
name: kernel-manager
description: CachyOS Kernel Manager installation, custom builds and sched-ext controls.
tools: cachyos-kernel-manager, linux-cachyos, scx_loader
source: https://wiki.cachyos.org/features/kernel_manager/
section: Installing a Kernel; Building a custom CachyOS Kernel; Sched-ext Framework Management
checked: 2026-11-06
version: rolling
---

# Kernel Manager

Record the installed Kernel Manager version, running kernel and relevant kernel/module package versions. Its repository install/remove interface and its custom CachyOS kernel build interface are different operations. Do not assume that a generic Arch kernel can be configured through the CachyOS custom-build workflow.

The documented sched-ext controls use `scx_loader`; control needs a functioning polkit agent. A GUI or D-Bus error from AI Desktop's isolated command environment does not establish failure in the desktop session.

Before a kernel change, identify headers, DKMS or prebuilt modules, the boot manager and generated initramfs paths. Preserve a working boot entry. Successful package installation proves neither a successful next boot nor that the kernel is running now.

For scheduler controls, use the available scheduler procedure and the installed manager's configuration. Match names and profiles to actual capabilities; do not copy the wiki's example flags into another version.
