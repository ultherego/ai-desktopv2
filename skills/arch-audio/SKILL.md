---
name: arch-audio
description: Diagnoses missing sound, invisible devices, dropouts and Bluetooth profile switching in the PipeWire/WirePlumber stack. Use to separate a session, device, routing or single-application problem.
---

# Audio diagnostics

## Scope and access

Establish the output or microphone, the affected program, when it occurs and the latest change. First confirm which sound server is in use; do not carry WirePlumber configuration over to a different stack.

Reads of the user session need its sockets. The AI Desktop command sandbox does not expose them: a connection error to the bus or to PipeWire does not prove an audio failure. Do not move a read into `change`, do not mount sockets and do not change the policy on your own. If no source of session state is available, ask for the one result you need from the user's terminal.

## Investigation and decisions

1. When the relevant reads are available, establish the state of `pipewire.service`, `pipewire-pulse.service` and `wireplumber.service` through `systemctl --user status`. An active service does not yet prove correct routing. For a failed unit, read only its log from the time of the error.
2. `wpctl status` shows devices, outputs, inputs and streams. A whole device missing leads to hardware detection and its profile; only a stream missing leads to the application.
3. For an identifier from the current result, use an allowed `wpctl inspect ID` and `wpctl get-volume ID`. The numbers are temporary: do not record them as a permanent identification of a device.
4. The output exists but is muted or a different default is selected: describe the specific difference. Do not change the volume or the default device during a read step.
5. Only one application fails while other playback works: examine its stream, device selection and application limits before proposing a restart of the whole stack.
6. Bluetooth quality drops after the microphone is used: check the active profile and what the call requires. Do not disable automatic switching if the microphone is needed. A pairing problem belongs to `arch-bluetooth`, if that Skill is available.

## Change and verification

First show the current and proposed state, the cost to the microphone or other applications, and the way back. Changes to profile, routing, mute and settings saved through `wpctl` need separate consent. Do not delete the whole configuration or state as a first attempt.

For a file change, establish the WirePlumber version and use the documentation for that version; do not mix the older Lua format with 0.5+ configuration. Test the same output, the same application and, with a headset, the microphone as well. A service restart interrupts active audio and is not in itself a test of a fix.

## Result

Name the layer of the failure, the evidence and the scope: device, profile, stream or application. Without a session, report the limit on what could be observed and one next read; do not diagnose from a sandbox error.

## Sources

- [WirePlumber: `status`, `inspect`, `get-volume`, settings](https://pipewire.pages.freedesktop.org/wireplumber/man/wpctl.html).
- [Configuration and checking the session](https://pipewire.pages.freedesktop.org/wireplumber/daemon/getting_started.html).
