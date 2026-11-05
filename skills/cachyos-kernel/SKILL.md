---
name: cachyos-kernel
description: Diagnoses regressions after a CachyOS kernel change and the compatibility of its modules. Use for choosing a variant, initramfs or DKMS errors, or when comparing with a working kernel.
---

# CachyOS kernel and modules

## Establish a reference point

Establish the symptom, the last working kernel, the time of the update and whether a restart was done. Read `uname -r` and allowed package queries, e.g. `pacman -Qs linux-cachyos` and `pacman -Qi NAME`. The running kernel can be older than the installed one; this difference alone before a restart does not prove a failure.

Do not choose a variant on the promise of performance alone. Check package names, availability and features in current documentation and repositories, instead of assuming an unchanging catalog of variants.

## Interpretation

| Observation | Next step |
|---|---|
| Symptom occurs only on the new kernel | Compare the same conditions on the preserved working kernel; collect the relevant journal. A coincidence in time is a hypothesis of a regression. |
| `module not found` when building initramfs | Establish the exact module and the kernel the error concerns; check the files of the right package. Do not remove the module from the configuration just to hide the error. |
| DKMS module was not built | Check for compatible headers and the specific build log. Do not assume a precompiled module package fits every variant. |
| Module load or signature error | Separate a version mismatch, a missing module and signature policy. Do not disable Secure Boot as the first fix. |
| System does not reach the point of starting services | Apply the boot identification procedure, e.g. `cachyos-boot-snapshots`, if available. |
| No log or no way to compare | Give the hypothesis and the missing evidence; do not declare a confirmed regression. |

Read kernel logs only from the right time and within the scope made available by the controller. Do not get around a denial through `sudo` or a free shell.

## Change and test

Before an install or removal, establish the dependent modules, headers for DKMS and the mechanism that generates initramfs and bootloader entries. Keep at least one verified kernel and the ability to select it at boot. Do not remove the working variant during a trial.

Present a separate plan with packages, changes in `/boot`, consent and a recovery path. After execution, check the result of the hooks and the availability of the entries, and after a controlled restart `uname -r`, the state of the needed modules and the original symptom. Installing a package alone does not prove the new kernel is running.

Undo needs compatible kernel, initramfs, module and configuration files. A root snapshot may not cover `/boot` or the ESP. If the controller cannot safely perform the operation, end at a plan for the user.

## Result

Give the running and installed kernels, the state of the relevant module, the evidence for the diagnosis, the test performed and one next step. Do not promise a performance improvement without comparing the same load.

## Sources

- [Variants and precompiled modules](https://wiki.cachyos.org/features/kernel/#variants).
- [Scope of snapshot restoring](https://wiki.cachyos.org/configuration/btrfs_snapshots/).
