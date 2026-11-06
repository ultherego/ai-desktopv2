---
name: package-queries
description: pacman version-specific query options and configuration-file maintenance.
tools: pacman
source: https://man.archlinux.org/man/pacman.8.en
section: Operations; Query Options; Handling Config Files
checked: 2026-11-06
version: rolling
---

# Installed package evidence

Prefer the installed pacman manual and operation-specific help when the rolling web manual differs. Record pacman's version and the package version separately; one does not identify the other.

`-Q` queries installed packages, whereas `-Si` reads the local synchronization database. Querying that database does not refresh it. `-Qi` describes a package; `-Qk` checks package-owned file presence, and stronger checking has additional metadata requirements. A successful presence check does not prove runtime correctness.

Use `-Qo` for the owner of a conflicting path. Keep locally modified configuration and package-provided replacements distinct. Review `.pacnew` and `.pacsave` content through a scoped diff rather than overwriting configuration from memory.

Before any transaction, inspect the relevant dependencies, origin and full-update requirements. A successful query never authorizes an install, downgrade, cleanup or database change.
