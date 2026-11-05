---
name: arch-btrfs
description: Diagnoses Btrfs space usage and errors. Use for ENOSPC, unclear space consumption, checksum errors or judging whether balance or scrub is needed.
---

# Btrfs diagnostics

## Establish the scope

Establish the mount point, the number of devices, the symptom and the latest change. Do not assume the problem concerns `/`. Snapshots and subvolumes can share data; reading `df` or `du` is not enough to explain the whole allocation.

## Reading and interpretation

Use only reads allowed by the controller. `btrfs filesystem usage MOUNTPOINT`, `btrfs filesystem show MOUNTPOINT`, `btrfs device stats MOUNTPOINT` and a narrow excerpt of the kernel journal may need permissions or data from the user. The sandbox's view of mounts does not prove the host layout. A denied access is missing evidence, not a Btrfs failure.

| Observation | Next step |
|---|---|
| Large difference between allocated and used space | Separate data, metadata and unallocated space; establish the profiles and the share of snapshots. The difference alone does not justify balance. |
| Deleting files does not free space | Check whether their extents remain in snapshots; present the scope of any deletion separately. |
| ENOSPC with free space in `df` | Check the free working space and metadata allocation; do not start a full balance. |
| I/O errors, missing device, growing error counters | Stop tuning; the priority is a copy of the data and a diagnosis of the devices. Do not reset the counters. |
| No data about layout or profile | End the diagnosis by naming the missing read; do not guess a fix. |

## Possible change

- Choose balance according to the established cause. The filter `usage=0` concerns empty block groups; other thresholds need a justification based on usage. A full balance moves the data and metadata of the whole filesystem, loads I/O and needs working space. `--bg` does not limit this scope.
- Scrub verifies checksums and can repair from a good replica. It does not replace a full check of the structures. Even a scrub with `-r` on a filesystem mounted for writing can cause writes; do not present it as an ordinary read.
- Do not propose `btrfs check --repair` or a profile change as a general fix. Do not defragment the whole tree to recover space: it can break extent sharing.
- Every operation needs a separate plan, consent, an assessment of the load and a test of the result. Interrupting a balance does not undo relocation already done; do not promise a rollback that does not exist.

## Result

Give the cause or hypothesis, the evidence from usage and errors, and one next step. After an approved operation, compare the same reads and the operation's status; the exit code alone does not prove the problem is solved.

## Sources

- [Balance: filters, cost and ENOSPC](https://btrfs.readthedocs.io/en/latest/btrfs-balance.html#filters).
- [Scrub: scope of checking and repair](https://btrfs.readthedocs.io/en/latest/btrfs-scrub.html#description).
