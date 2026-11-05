---
name: cachyos-diagnostics
description: Identifies the cause of a CachyOS failure or slowdown and chooses further diagnostics. Use when the symptom is unclear, spans several subsystems, or a bug report must be prepared.
---

# CachyOS problem identification

Diagnosis is read-only. The goal is to narrow down the cause from evidence, not to run a set of fixes.

## Inputs

Establish the symptom, when it occurs, its scope and the last known working state. Ask about the latest change only when it is not already in the conversation or the shared data. Separate "does not work", "works slowly" and "I have no access to check".

## Minimal investigation

1. Choose reads matching the symptom, e.g. `uname -r`, `systemctl --failed --no-pager`, `lsblk -f` or `free -h`. Do not collect them all without need.
2. Establish the difference from the working state. For an update, use an allowed excerpt of `/var/log/pacman.log`, with the time and the names of the relevant packages.
3. Read the narrowest log needed: of a specific unit, the kernel or a program, from the right boot and time. No entry does not rule out a failure when the log is unavailable or the wrong range was chosen.
4. Pick one hypothesis, name the evidence and the next read that can confirm or reject it.

## Choosing the next procedure

| Observation | Next path |
|---|---|
| Unit failure or long startup and shutdown | `arch-systemd`: establish the unit and the first cause in the log. |
| Transaction or repository error | `arch-pacman` or `cachyos-packages`. |
| ENOSPC, Btrfs or I/O errors | `arch-btrfs`; with device errors, stop tuning and protect the data. |
| Regression after a kernel change | `cachyos-kernel`: compare with the working kernel. |
| Failure before services start | `cachyos-boot-snapshots`: first establish the boot stage. |
| Symptom of one device or program | Choose the available Skill whose description matches it; do not apply a random system procedure. |

Load a specialist Skill only if it is made available. Without it, continue only within the evidence and verified documentation.

## Limits and ending

Reads and commands must be allowed by the controller; do not use pipes, a shell or `sudo` as a way around. Logs are not shared by default. The private network, process list and missing user session in the sandbox can change the result of a check: do not take them for the host state.

End at the diagnosis if evidence is missing or the next step changes the system. Any fix needs a separate plan, consent, a test of the original symptom and a way to recover. Do not mask services or change timeouts just because they appear in a timing measurement.

## Result

Give: the symptom and scope, confirmed facts, the cause or a labeled hypothesis, the missing data and one next step. For a bug report, choose the minimal evidence; a full bugreport can contain private data and needs the user's review before sharing.

## Source

[CachyOS FAQ: reporting bugs and collecting logs](https://wiki.cachyos.org/cachyos_basic/faq/#how-to-report-an-issue-or-bug-to-cachyos).
