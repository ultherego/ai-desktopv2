---
name: arch-amdgpu
description: Diagnoses hangs, artifacts and display problems of AMD graphics cards on Arch Linux. Use once the symptom is known; separates a driver regression, the display path and a hardware fault before settings change.
---

# AMDGPU diagnostics

## Inputs and access

Establish the GPU model, whether it is an APU or a dedicated card, the running kernel version, the exact symptom and when it occurs. Ask about the latest update, a monitor change and tuning only when this data is not already in the conversation.

The commands below are reads that need a matching `allow` rule; logs and files need their own access. A missing permission or a missing file in the sandbox means no observation. Do not run a shell or write through `/sys` to get around a denial.

## Investigation

1. `lspci -k` identifies the PCI device and the driver in use; `uname -r` identifies the kernel. With several GPUs, tie the symptom to a specific device.
2. If the policy allows it, read `journalctl -k -b --no-pager -n 100` from the time of the failure. After a restart you may need the previous boot. A journal that was not preserved does not rule out a hang.
3. In an allowed context, find the DRM device tied to this PCI device and its `hwmon` directory. Do not assume the numbers `card0`, `dri/0` or `hwmon0`. Read only fields that exist and are supported; do not infer damage from the absence of an optional interface.

## Interpretation

- Device absent from PCI: check hardware detection; changing an AMDGPU parameter will not explain this result.
- Different driver or no driver: establish the module's binding to the device and its load errors before assessing performance.
- GPU reset, timeout or firmware error at the time of the symptom: correlate with the load and with a kernel change. The mere presence of the word `amdgpu` does not confirm the cause.
- Flicker on only one output or mode: establish the monitor, connector, refresh rate and VRR; compare with a working mode. Do not immediately treat power management as the cause.
- Assess temperature, VRAM usage and load values together with the time of measurement; a single high value does not prove a failure.

## Change and verification

Change one thing at a time with separate consent, with the starting state and the way to restore it recorded. The parameter `amdgpu.sg_display=0` applies to APUs only; consider it solely for a matching problem described in the kernel documentation. Do not use it as a general fix for dedicated cards.

Do not unlock overclocking features or reset someone else's voltage or clock settings as part of diagnostics. The test is repeating the same scenario and comparing the symptom and the journal. Failing to reproduce the failure in a short trial does not prove a lasting fix.

## Result

Give the device and driver, confirmed observations with their times, the most likely cause and an alternative. When data is missing, end by naming the one read you need, without guessing a fix.

## Sources

- [AMDGPU parameters: `sg_display`, `ppfeaturemask`](https://docs.kernel.org/gpu/amdgpu/module-parameters.html).
