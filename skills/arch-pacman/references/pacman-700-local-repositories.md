---
name: pacman-700-local-repositories
description: Pacman 7.0.0 download-user access failures with a local repository.
tools: pacman
source: https://archlinux.org/news/manual-intervention-for-pacman-700-and-local-repositories-required/
section: Pacman 7.0.0 release intervention published 2024-09-14
checked: 2026-11-06
version: 7.0.0
---

# Pacman 7.0.0 local repository access

This release introduced package downloading under a separate unprivileged user. For a local repository, identify that download user and verify access to the repository files and traversal of the parent directories. A permission error can therefore have a different cause from an unreachable remote mirror.

The release notice describes access through the `alpm` group and the directory execute bit. Review the actual ownership and intended readers before proposing a scoped permission change; do not apply recursive ownership changes to an unidentified path. Review relevant `.pacnew` changes as part of the configuration transition.

This reference is tied to the named release. It is not evidence that every earlier or later pacman version has the same defaults or options. Use the matching installed manual for other versions and verify the original repository transaction after any approved correction.
