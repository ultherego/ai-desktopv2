---
name: arch-bluetooth
description: Diagnoses a missing Bluetooth controller, pairing problems and regressions after sleep in BlueZ. Use to distinguish missing hardware, a radio block, service state and a problem with one device.
---

# Bluetooth diagnostics

## Scope and access

Establish whether it worked before, whether the failure affects all devices, and whether it began after sleep or an update. Limit device names and addresses to what is necessary; do not ask for pairing keys.

`bluetoothctl` uses D-Bus. Access to the host socket is no guarantee of read-only operation. Run only exact reads from `allow`; do not start the interactive menu, scanning or pairing as an investigation. A denied access or a missing bus means a gap in the observations.

## Investigation and decisions

1. If allowed, check `systemctl status bluetooth.service --no-pager`. For the `failed` state, read a short excerpt of `journalctl -u bluetooth.service -b --no-pager -n 50` from the time of the failure. Having no access to the log does not mean the log is empty.
2. Establish whether the controller is detected: `lsusb` or `lspci -k`, depending on the hardware. `rfkill list` can show a hardware or software block. A missing controller points to hardware, driver and firmware, not to pairing again.
3. An available read of `bluetoothctl show` separates a missing controller, `Powered: no` and a working radio. The error `NotReady` requires establishing this state; it does not prove one specific cause.
4. The radio works but the problem concerns one known device: read only its state through `bluetoothctl info ADDRESS`, using the address established earlier. Distinguish paired, trusted and connected; these are different properties.
5. After wake-up, compare the controller state and the journal with the time of sleep. When pairing and connection work and only audio fails, move on to `arch-audio`, if available.

## Change and verification

Turning on the radio, removing a pairing, `trust`, `pair`, `connect` and a service restart change state. They need a separate plan and consent; warn about disconnecting a keyboard or mouse in use. Do not clear the BlueZ database or move keys between systems as part of a default diagnosis.

Before a change, record the relevant device state. Test the connection and the device's function, and for a regression also a sleep cycle. If a step cannot be undone without pairing again, say so before consent. Capturing with `btmon` needs its own scope and a review of the data.

## Result

State whether the problem concerns detection, a block, the service, the connection or the device's function. Include the needed excerpt of evidence and one further step. End without a fix when there is no reliable host state.

## Sources

- [BlueZ: `bluetoothctl`, reads and control commands](https://github.com/bluez/bluez/blob/master/doc/bluetoothctl.rst).
