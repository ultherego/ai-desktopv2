# Client installation and runtime transition

## What candidate 0.1.0-80 adds

```fish
ai-desktop doctor
ai-desktop doctor --v
```

The ordinary output and detailed JSON include metadata-only discovery for both system clients. The possible states are `present`, `missing`, `unsafe`, `unsupported`, `changed` and `unavailable`. `present` means a supported native ELF file passed the path/ownership/mode checks. It does not establish vendor authenticity, version compatibility or a working client protocol. The report marks the system client inactive, with version and protocol unchecked.

No system client is executed, no login is read or changed, and no network request is made by this discovery. The working runtime accepted in 0.1.0-79 remains in use. Its tested version baselines are Claude 2.1.312 and Codex 0.166.0; a pure version-policy comparison can label another version untested, but it cannot prove runtime compatibility or enable a client.

## System-installed clients: agreed target

AI Desktop will use a compatible system installation of **Claude or Codex**. One is sufficient; choosing Codex must not require Claude. Vendor executables will not be bundled in AI Desktop packages, and private package preparation will no longer be the normal installation route. This transition begins with shared discovery and preflight. The existing runtime, authentication and packaging are not migrated merely because discovery can identify a client.

The shared boundary must:

- inspect only explicitly supported absolute system paths, with root-owned files and parents that are not writable by other users;
- validate the executable format and integrity, then determine the version and adapter compatibility through a controlled probe;
- reject unknown or incompatible clients with an actionable result, without sending a question or falling back to another service;
- bind the selected executable identity to a task and recheck it before execution, refusing replacement or an update during that task;
- keep the existing isolated service account, separate authentication profiles, restricted model tools and controlled network access.

The initial system-client boundary does not accept arbitrary `PATH` results, binaries in a user's HOME (including a per-user `~/.local` Claude installation), shell/npm wrappers or a user-selected executable. Detection never imports the user's existing CLI login. A supported system installation and a separately connected AI Desktop account are different prerequisites.

Discovering an ELF does not validate its entire installation. For example, the [Arch `openai-codex` package file list](https://archlinux.org/packages/extra/x86_64/openai-codex/files/) includes `/usr/bin/codex`, a binary under `/usr/lib/openai-codex/bin/` and additional helpers. The future runtime must account for the supported package layout and required assets without opening broader filesystem access.

Compatible client updates and AI Desktop updates will be independent. Compatibility describes a tested adapter protocol, not a promise that every future client version works. Installation instructions for supported system package layouts will replace the section below only after runtime migration, packaging tests and CachyOS acceptance. Do not move vendor binaries into guessed paths to bypass a refusal.

## Transition: prepare clients for the retained runtime

The following is the working 0.1.0-79 preparation method, retained by candidate 0.1.0-80. It is not the target system-client installation method.

On x86_64 CachyOS or Arch, run the following from the root of your AI Desktop checkout as its regular owner. Select `claude`, `codex`, or `both`.

```fish
python3 -B scripts/install_agents.py both --install
```

The helper asks for confirmation before downloading or invoking `/usr/bin/makepkg --cleanbuild -si`. Arch's ordinary dependency and package installation prompts follow. Install the Arch `base-devel` group first. Prepare the client files alone, with no package operation, by omitting `--install`.

```fish
python3 -B scripts/install_agents.py claude
```

| Selection | Verified source | Existing file |
|---|---|---|
| `claude` | Official Anthropic Linux x64 ELF, version 2.1.312. The SHA-256 must match the pin in the package recipe. | An identical regular `packaging/arch/claude-code` is reused without downloading. |
| `codex` | Official OpenAI archive, version 0.166.0. The existing Codex preparation tool checks the pinned archive SHA-256 and extracts its one expected ELF. | The helper fetches the pinned archive to compare the actual ELF bytes even for an existing `packaging/arch/codex`. |
| `both` | Both checks run before publishing the selected files. | A mismatch stops before package installation. |

Partial downloads stay in a private staging directory. Publishing a verified file never replaces an occupied name. If another process creates that name during preparation, rerun the helper after inspecting the collision. Existing invalid files and symlinks require manual review. If both clients are in the checkout, choose `both --install` so both are checked before inclusion.

The helper uses a fresh package output directory and checks the selected clients against the bytes staged in the private package. It leaves verified source assets in `packaging/arch/` for subsequent builds. It does not execute either vendor client, log into an account, send an AI request, or change network permissions. The resulting local package may contain vendor binaries and is for the owner's private use. Run `ai-desktop login` separately when ready to choose an account.
