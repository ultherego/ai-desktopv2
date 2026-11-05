---
name: arch-zram
description: Explains and diagnoses zram, zswap and swap usage on Arch Linux. Use for memory pressure, ineffective compression, a configuration error or a question about hibernation.
---

# zram and swap diagnostics

## Inputs and access

Establish whether there is an out-of-memory condition, stuttering, a device startup error, or only values that are hard to understand. Do not propose tuning without a symptom or a goal. Run reads only within the scope the policy makes available; a denial does not mean the device is absent.

## Investigation

1. If allowed, read `zramctl`, `swapon --show` and `free -h`. Tie together devices, priorities and usage; do not assume the number `zram0`.
2. For a detected device, read the available statistics in `/sys/block/DEVICE/mm_stat`. The kernel documentation defines the field order; do not guess units or names from position without checking the version.
3. The state of `/sys/module/zswap/parameters/enabled` decides whether the additional zswap cache is active. Also establish where the actual swap is located.
4. Find the source of the configuration: a systemd generator, a udev rule or distribution configuration. Read only existing, allowed files; do not layer a second management mechanism over the first.

## Interpretation

- A large `DISKSIZE` means logical capacity, not RAM reserved in that amount.
- `orig_data_size` describes data before compression; `compr_data_size` the compressed data; `mem_used_total` the actual cost including overhead. Assess the measurement of the same device instead of assuming a fixed compression ratio.
- Used swap with memory available does not prove a failure. Tie the symptom to a repeatable load, memory pressure and any OOM event.
- Zswap intercepts pages before the swap device. Its coexistence with zram needs an assessment of overhead and goal; do not disable it automatically just because both mechanisms are present.
- Hibernation needs persistent space for the image. zram alone will not keep it after power-off; hibernation diagnostics need a separate check of the device and the resume configuration.

## Change and verification

Changing the size, algorithm, priority or activation of swap changes system state. First preserve the configuration and establish that a return is possible. `swapoff` can need a significant amount of free memory and can end in an error; do not treat it as a harmless undo and do not propose mass disabling of swap under memory pressure.

After separate consent, compare the same load, RAM usage, compression cost and the symptom. An active service or a valid configuration file alone does not confirm an improvement.

## Result

Give the configuration mechanism, the active devices, the interpretation of the measurements and their link to the symptom. When there is no evidence of a problem, end with an explanation of the values; when observations are missing, name the one measurement you need.

## Sources

- [Kernel: zram, statistics and deactivation](https://docs.kernel.org/admin-guide/blockdev/zram.html).
- [Kernel: the role of zswap](https://docs.kernel.org/admin-guide/mm/zswap.html).
- [util-linux: `swapoff`, error codes](https://man7.org/linux/man-pages/man8/swapon.8.html).
