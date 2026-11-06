---
name: package-tools
description: CachyOS Hello, Package Installer and mirror ranking responsibilities.
tools: cachyos-hello, cachyos-packageinstaller, cachyos-rate-mirrors
source: https://wiki.cachyos.org/cachyos_basic/why_cachyos/
section: CachyOS Custom Applications
checked: 2026-11-06
version: rolling
---

# CachyOS package tools

Identify which installed application performed the transaction and its version. A GUI label, package name and executable name can differ; inspect the package's file list rather than guessing a command.

CachyOS Hello, also called Welcome, groups package actions and system tweaks. CachyOS Package Installer is a graphical package selection interface. `cachyos-rate-mirrors` ranks Arch and CachyOS mirrors; running it can replace mirror configuration and is a change, not a connectivity-only probe.

For a failure, keep the exact package-manager error and transaction stage. Compare the relevant installed package, configured repository and local sync database. A frontend does not remove pacman's consistency and signature requirements.

Before a proposed mirror change, record the affected lists and baseline. After consent and execution, check the chosen repository and original download failure. Do not reset keyrings or clear all cache merely because Hello exposes those buttons.
