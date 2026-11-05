---
name: arch-systemd
description: Diagnoses systemd unit failures and slow boot and shutdown. Use to establish the cause and prepare a limited unit change with a test.
---

# systemd diagnostics

## Establish the scope

Establish the unit name, the system or user manager, the time of the symptom and the latest change. The `Description=` text in a message need not be the unit name. Do not restart the service before gathering evidence.

## Reading and decisions

Choose only commands matching the available templates: `systemctl --failed --no-pager`, `systemctl status NAME --no-pager`, `systemctl cat NAME` and this unit's journal from the right boot and time. Reading the journal is not allowed by default. The user manager needs its session; its absence in the sandbox does not mean the service has failed.

| Result | What to establish next |
|---|---|
| `not-found` | Verify the name, scope and package; do not create a replacement unit. |
| `masked` | Establish who masked it and why; do not unmask automatically. |
| `failed` or a non-zero process code | Read the first relevant error, `Result` and the configuration together with drop-ins. A non-zero code from `systemctl status` is also information about state. |
| Dependency error | Find the first unmet dependency instead of fixing the dependent service. |
| `active`, but the function does not work | Check the application symptom; an active process does not prove it works properly. |
| Log unavailable or without matching entries | Check the boot, time range and manager; point out the gap in the evidence. |

For a slow start, set `systemd-analyze blame` against `systemd-analyze critical-chain`. A long unit time does not prove it blocked reaching the target. For a slow shutdown, use the journal of the previous boot; do not raise a timeout without establishing what the process was waiting for.

## Change and undo

Before a change, preserve the content and origin of existing overrides. Propose a specific drop-in, its full diff and, separately, the effects of a reload or restart. Do not edit a vendor unit in `/usr`.

Undo must restore exactly the changed file. `systemctl revert NAME` removes all local drop-ins of the unit, can remove a local replacement of its vendor version and unmasks it; it is not an undo of the latest edit. `reset-failed` clears the failed state, not its cause.

After consent, check that the configuration is valid with the right available tool, then the state, new logs and the original symptom. A restart alone is not a test of a fix. With no allowed execution, this is a plan to apply by hand, without declaring success.

## Result

Give the unit and scope, the source error with evidence, the cause or hypothesis, and one next step. State whether the change was only proposed or actually made and verified.

## Source

[systemctl: status, cat, edit and revert](https://freedesktop.org/software/systemd/man/latest/systemctl.html).
