---
name: cachyos-scheduler
description: Diagnoses sched-ext operation and compares a scheduler change on CachyOS. Use for scx_loader, scxctl, a scheduler failure or measuring the effect of scheduling on performance and energy.
---

# Sched-ext: state, diagnosis and trial

## Establish the state

1. Establish the goal and symptom: a failure, latency, or a planned measurement. Without a specific problem, do not propose tuning.
2. Read the running kernel through `uname -r` and the versions of the available tools. The name of a kernel variant is not sufficient evidence of feature support.
3. If the policy and the system view allow it, read `/sys/kernel/sched_ext/state` and `/sys/kernel/sched_ext/root/ops`. `enabled` together with an ops name confirms an active BPF scheduler; `disabled` indicates no active sched-ext. A missing path or a denied read requires distinguishing kernel support from an access limit.
4. Establish the manager and its state: `systemctl status scx_loader.service --no-pager` and, with an available client, `scxctl get` and `scxctl list`. These are examples of reads, run only after matching the policy.

## Interpret the result

- No active scheduler can be intentional. It does not mean the system is broken or does not use a CPU scheduler.
- An active service does not prove a scheduler is loaded. Compare the service state with the kernel's result and the log from the same time.
- A D-Bus client connection error in the sandbox does not prove a service failure on the host. Do not move a read into `change` or widen access to get around it.
- When the scheduler disappeared after an error, read the available excerpt of the manager and kernel journals. The kernel can detach a BPF scheduler after an error. Establish the reason from the log instead of assuming a conflict with a specific program.
- With two management mechanisms or a profile changing on its own, establish which one controls the state and when. Do not disable services merely because they are present.

## Change with measurement

Preserve the baseline: kernel, active scheduler and mode, manager and its configuration. The `scxctl` version decides names and parameters; choose an available variant, without a list of fixed profile numbers or a promise of improvement.

Plan one reversible trial, a metric and an identical load for several repetitions. Include responsiveness, stability and power draw when they are the goal. Control needs separate consent and an application mechanism that actually has access; a Skill does not grant administrator or polkit rights.

Rollback must restore the recorded state. `scxctl restore` restores the default setting from the configuration, which can differ from the state before the trial. On a hang or degradation, end the trial; propose permanent configuration only after measurement. Do not add kernel parameters as part of an ordinary scheduler comparison.

## Result

Give the confirmed state of the kernel and manager, the cause or hypothesis with evidence, the baseline and trial result, and the state of the rollback. With no logs or visibility, end at the specific gap, without a diagnosis from the access message alone.

## Sources

- Kernel, state and behavior of sched-ext: <https://www.kernel.org/doc/html/latest/scheduler/sched-ext.html#switching-to-and-from-sched-ext>.
- CachyOS, management and comparison: <https://wiki.cachyos.org/configuration/sched-ext/>.
