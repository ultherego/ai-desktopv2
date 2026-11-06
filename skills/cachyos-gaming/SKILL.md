---
name: cachyos-gaming
description: Diagnoses CachyOS games, Steam, Proton-CachyOS, Wine, Lutris, Heroic, gaming metapackages and game-performance. Use for launching, crashes or a measured performance trial.
---

# Game diagnostics and measuring a change

## Gather minimal context

Establish the game, how it is installed and launched, the version of the game and of the compatibility layer, the card and the driver. Ask about the exact symptom and the latest change if the conversation does not show them. Distinguish failure to launch, a crash after some time and worse performance. Do not start with system tuning.

## Choose a path

| Observation | Next step |
|---|---|
| Game does not launch | Check the message, the game's requirements and support for its protection mechanisms in the game's sources; compare the compatibility layer version. |
| One specific title exits | Read the log from this attempt and match the time of the error; distinguish a relevant error from repeated warnings. |
| Problems affect many games | Check the common driver, resources, game library and the latest update; use the right selected Skill. |
| Smoothness drops over time | Compare load, temperatures and memory with the moment the symptom occurs. Swap use alone does not prove the cause. |
| Symptom occurs only with an overlay or recording | Plan a trial without one add-on, keeping the other settings. |

Reading files and logs needs access in the policy. The command sandbox usually does not see the game session; a device or process missing from its view does not prove it is missing on the host. Do not read account data and do not ask for the whole game directory or a full report without need.

## Trial and comparison

1. Record the baseline: graphics settings, resolution, FPS limit, compatibility layer version, launch options and a reproducible scene. Choose a metric matching the goal: crash, launch time, frame time, FPS or power draw.
2. Choose one change that follows from the evidence. Keep the previous value and the way to restore it. Do not change several parameters globally, and do not delete the prefix or cache as a routine test.
3. After separate consent to the change, run the same trial in comparable conditions. Separate the first run with shader compilation from later ones; compare several repetitions. One favorable number without measurement conditions does not prove an improvement.
4. On a crash or degradation, propose restoring the exact baseline. Do not make a change permanent on the basis of an incomplete measurement.

For a justified diagnostic trial, Proton offers the Steam launch option `PROTON_LOG=1 %command%`. Setting it and creating the log are a change; do not pretend the application did them. Before reading, select the needed excerpt, and after the trial restore the earlier options. Check parameter availability in the documentation for the version in use.

## Result

Give the scope of the problem, the evidence, a baseline/trial table with measurement conditions, the state of application and the result. If the trial did not take place on the live game, call it not performed. Do not promise a performance gain on the basis of a package or scheduler name.

## Sources

- CachyOS, logs and launch options: <https://wiki.cachyos.org/configuration/gaming/>.
- Proton, runtime parameters: <https://github.com/ValveSoftware/Proton#runtime-config-options>.

## Tool references

Load the matching reference when the task concerns these tools. Verify the installed version and help; a rolling source is not a verified version match.

- [CachyOS gaming packages, runners and wrappers](references/gaming-tools.md).
