---
name: cachyos-packages
description: Diagnoses CachyOS repositories, mirrors and package signatures. Use for download errors, version differences or judging whether an optimized repository is compatible with the CPU.
---

# CachyOS packages

## Investigation

Establish the exact message, command, package, the time of the latest update and the package source. For file conflicts and a database lock, use the `arch-pacman` procedure, if it is made available. Here, consider what is specific to CachyOS repositories.

Read only the needed, allowed excerpts of `/etc/pacman.conf`, the referenced mirrorlists and the transaction log. Compare `pacman -Qi NAME` with `pacman -Si NAME`, if the templates allow it. Do not ask for sources containing login data; in that case you need a minimal excerpt redacted by the user. Do not read the whole keystore. Missing permissions mean no data for the diagnosis.

## Decisions

| Observation | Further check |
|---|---|
| 404 or version mismatch between mirrors | Establish the freshness of the local database and of the chosen mirror; check the announcements. Only a confirmed mirror problem justifies changing it. |
| Download timeout | Separate an error of one mirror from a connection or DNS problem. A command in a sandbox without network is not a test of host connectivity. |
| Invalid signature | Establish the package, the signing key, the system time and the state of the relevant keyring package. Do not disable signature verification. |
| `local is newer than` | Check whether this is a lagging mirror, a deliberate downgrade in the repository, or a package from another source. The warning alone does not justify a downgrade. |
| Unexpected architecture or CPU instruction | Check the actually enabled repositories and CPU compatibility according to current documentation; do not infer only from the name of the processor family. |

## Possible change

- Changing the mirrorlist, updating the keyring and synchronizing packages are separate plans with a transaction scope and consent. Keep the previous configuration so the specific change can be undone.
- A double `-u` allows package downgrades. Before such a plan, explain why the repository versions are the right ones and which packages may go back.
- Do not give a universal recipe for deleting the keyring or databases or for manually signing a key. When a breach of trust is suspected, stop the installation and verify the current procedure and the key fingerprint in the distribution's source.
- Changing the level of the optimized repositories covers many packages. It needs a separate task, CPU compatibility, a recovery plan and current documentation. Do not carry it out as a fix for a single package.
- Do not add new permissions or commands on your own. If the controller does not support the operation, present a plan for the user's decision.

## Result

Give the compared versions and sources, the confirmed cause or hypothesis, and one next step. After an approved change, check the same package and the original error; a warning disappearing does not prove the application works.

## Sources

- [CachyOS FAQ: pacman errors](https://wiki.cachyos.org/cachyos_basic/faq/).
- [Compatibility and migration of optimized repositories](https://wiki.cachyos.org/features/optimized_repos/#migrating-from-x86-64-v3-to-x86-64-v4-or-znver4).
- [pacman: the sysupgrade option](https://man.archlinux.org/man/pacman.8.en).
