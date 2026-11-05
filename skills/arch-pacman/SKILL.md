---
name: arch-pacman
description: Diagnoses pacman errors, file conflicts and interrupted updates. Use to identify the cause before planning a repair of the database, packages or cache.
---

# pacman diagnostics

## Inputs

Establish the exact command and message, the package name, the transaction stage, and whether another package manager is running. If the failure followed an update, you need the relevant excerpt of `/var/log/pacman.log`, not a full report of the computer.

Choose reads from the current permissions: `pacman -Qi NAME`, `pacman -Qk NAME`, `pacman -Qo FILE` and `pacman -Si NAME`. The `-Si` query describes the local sync database, whose freshness must be established separately. Do not get around a denial through `sudo`, a shell or a command from the change list.

## Identify the cause

| Observation | Next step |
|---|---|
| Database lock | The `db.lck` file alone does not prove it is orphaned. Establish whether a transaction is active on the host; the sandbox's private process list does not rule one out. Without this, do not propose removing the lock. |
| File conflict | Establish the owner through `-Qo` and the file's origin. Do not delete a package file or use a global `--overwrite`. |
| Invalid signature or corrupt archive | Separate signature, key, system time and a download error. Keep signature verification; do not disable `SigLevel`. |
| 404 or missing package | Verify the name, the enabled repository and the state of the database or mirror. Repeated refreshing will not solve a disabled repository. |
| Interruption after installation started | Establish the packages and completed steps from the log. Do not treat a correct database read as proof that the files are consistent. |
| `local is newer than` | Compare the versions and the package source; a downgrade needs its own justification. |

## Repair plan

Arch requires a consistent full upgrade; do not propose a partial upgrade with only a database refresh. Do not use `-Syyu`, a key reset or cache deletion as an automatic escalation after any error.

Each install, removal, downgrade, unlock or cleanup is a separate system change: state the packages and files, dependencies, risk, consent and the way to recover. An upgrade runs scripts and hooks; do not promise an atomic undo of it.

Before cleaning up, establish which versions are needed for recovery. Do not delete the whole cache; `.pacnew` needs a deliberate merge, and an orphaned package is not necessarily useless to the user.

## Verification and result

Check that the original message is gone, the transaction completed, and the relevant files and the program's function are correct. Give the cause with evidence, the scope of the change actually made and the remaining gaps. Without a safe recovery plan, end at the diagnosis.

## Sources

- [pacman: operations and options](https://man.archlinux.org/man/pacman.8.en).
- [CachyOS-specific errors](https://wiki.cachyos.org/cachyos_basic/faq/).
