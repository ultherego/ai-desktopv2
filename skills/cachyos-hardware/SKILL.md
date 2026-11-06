---
name: cachyos-hardware
description: Identifies hardware and driver binding on CachyOS and routes diagnostics to the right area. Use for a missing device, a chwd error or a regression after a driver change, without automatic tuning.
---

# Hardware identification on CachyOS

## Goal and access

Establish one device or symptom: not detected, wrong driver, an error after an update, or performance under a specific load. Use what the conversation has established; do not assume the CPU or card vendor or the installation method.

Diagnosis is read-only within the limits of `permissions.toml`. Run commands one at a time and only when their exact arguments match `allow`. A cleaned environment, no access to a log or session, and sandbox limits are no evidence of a hardware failure.

## Minimal investigation

1. For PCI devices use an available `lspci -k`, for USB `lsusb`, for the CPU `lscpu`, and for the kernel version `uname -r`. Choose the reads the symptom needs instead of collecting a whole report.
2. Tie the device identifier to the driver in use. An installed package alone does not prove the module is active for this device.
3. For a problem after an update, establish the running and installed kernels and the time of the error. Analyze the available journal within this range. A new package without a restart does not mean the new kernel is in use.
4. When the question concerns chwd, an allowed `chwd --list-installed` shows installed profiles and `chwd --list-all` available ones. The list of available profiles does not confirm that they were applied or that they suit every card.

## Choosing the next path

- Device absent from the relevant listing: establish detection, the connection and initialization errors; do not start with driver tuning.
- Device visible, right driver missing: check support for the model, module compatibility with the kernel and the specific load error.
- AMD GPU with the amdgpu driver: use `arch-amdgpu`, if available. Another GPU needs the documentation of its driver and model.
- Module problem after a kernel change: available `cachyos-kernel`; audio: `arch-audio`; Bluetooth: `arch-bluetooth`; memory pressure: `arch-zram`.
- Performance or temperatures: reproduce the load and read the available `sensors`, power profiles and measurements over time. Without a scope and a reference point, do not propose changes to the CPU, disk scheduler or voltages.

When the needed Skill is not selected, do not pretend to have loaded it. State the scope of further diagnostics and the source or observation you need.

## Change and verification

Automatic chwd configuration, a driver swap, a kernel parameter and a write to `/sys` are system changes. A setting being short-lived does not make it safe. First confirm compatibility, the scope of effects, the starting state and the way back; a separate decision by the user is needed. When changing a driver or boot, establish a working path to recover the system.

The test is the device being detected again, the correct driver, and a repeat of the original scenario. A correct package installation alone does not confirm a fix.

## Result

Give the device, driver and kernel, the confirmed stage of the failure and the missing evidence. End with one next step in the relevant area, without a bundle of unrelated optimizations.

## Sources

- [CachyOS: identification and chwd profiles](https://wiki.cachyos.org/features/chwd/chwd/).

## Tool references

Load the matching reference when the task concerns these tools. Verify the installed version and help; a rolling source is not a verified version match.

- [chwd inventory and change boundaries](references/chwd.md).
