---
name: cachyos-boot-snapshots
description: Identifies the boot failure stage and the conditions for restoring a CachyOS snapshot. Use for problems with the bootloader, initramfs, kernel parameters or recovery through a Live ISO.
---

# Boot and snapshots

## Establish the failure stage

Gather the last visible message, the latest change, whether an older entry can boot, and the actual bootloader, UEFI or BIOS mode and mount layout. An installed bootloader package does not prove it is in use. Reads from a Live ISO initially describe that medium, not the installed system.

| Stage | Further diagnosis |
|---|---|
| Firmware does not show the right entry | Check the boot mode and the right ESP; do not install a bootloader blindly. |
| Menu works but does not find the kernel or initramfs | Compare the entry's paths, the contents of the right partition and the packages. |
| Kernel starts but does not mount root | Establish the exact error, device identifiers, encryption and subvolume. With I/O errors, stop the boot repair and protect the data. |
| System reaches services but the user session does not work | Hand over to service or session diagnosis; do not reinstall the bootloader for this reason. |
| Evidence about the stage or disks is missing | End by naming the reads you need. Do not guess device names. |

## Reading and limits

Use only data made available by the controller: entry configuration, the relevant log and a confirmed map of root, `/boot` and the ESP. `findmnt` in the sandbox can show the sandbox's view. Describe tools that need an administrator, an interactive chroot or a restart as a manual plan; lack of access does not justify escalation or a policy change.

## Conditions for restoring a snapshot

1. Confirm the Snapper configuration, the scope of the chosen snapshot and its date relative to the failure. A snapshot does not automatically cover nested subvolumes, the ESP or a separate `/boot`, and it does not replace a backup.
2. Check that the restored root matches the kernel, initramfs and modules. With a kernel update, the result depends on the layout and the restore mechanism; do not promise success or an unconditional impossibility of rollback.
3. State which newer data and configuration will be replaced. Plan how to preserve the current state and a recovery path before consent.

## Change and verification

For a kernel parameter, name the right file for the detected bootloader, the exact diff and the regeneration method that follows from the tool in use. Do not hand-edit generated entries. Undo must restore the previous parameter and the relevant generated files.

Consider reinstalling the bootloader or kernel only after evidence of damage or missing files. It needs confirmation of the devices, mounts, recovery access and separate consent. After the change, check the files and the consistency of the configuration; only a controlled boot confirms it works. Restoring root does not automatically undo changes in the ESP.

## Result

Give the failure stage, the evidence, the scope of the proposed change and the condition for carrying it out. Separate verified configuration from a boot that has not yet been tested.

## Sources

- [Boot manager configuration](https://wiki.cachyos.org/configuration/boot_manager_configuration/).
- [Snapshots and restoring](https://wiki.cachyos.org/configuration/btrfs_snapshots/#restoring-from-a-snapshot).
