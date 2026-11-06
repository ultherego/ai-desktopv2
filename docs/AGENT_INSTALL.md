# Client installation, registration and compatibility

Candidate **0.1.0-81** uses independently installed clients. The application package contains adapters and isolation machinery, not vendor executables. Choose **Claude Code**, **Codex**, both, or local inference with neither. This runtime and registration migration still needs its own CachyOS acceptance; the package-79 result does not cover it.

## Supported installations

| Installation | Native source | How AI Desktop uses it |
|---|---|---|
| Claude Arch/AUR package | Native `/usr/bin/claude`, a supported package symlink, or the maintained `claude-code` package's `/opt/claude-code/bin/claude` payload | Direct inspection and binding of the root-owned native payload; its known shell wrapper is never executed |
| Codex Arch package | `/usr/bin/codex` and `/usr/lib/openai-codex/`, including native helpers and resources | Direct inspection and binding of the root-owned package payload |
| Claude official native installer | `~/.local/bin/claude`, normally linked into `~/.local/share/claude/versions/` | Explicit registration of the native executable |
| Codex official standalone installer | `~/.local/bin/codex`, normally linked through `~/.codex/packages/standalone/current/` to a versioned release | Explicit registration of the recognized native release bundle, including helpers/resources |
| Current npm installation, including a custom prefix or nvm | Provider package and native platform dependency under that prefix's `lib/node_modules`; command under `bin` | Resolve the known package's native payload without executing Node or the wrapper, then register it |
| Other native/custom-prefix installation | Explicit absolute path to a recognized native executable or Codex bundle | `clients register PROVIDER --path /absolute/path`; unknown wrappers or resource layouts are refused |
| Local inference | Your configured loopback server | Neither cloud client nor registration is required |

The official [Claude setup guide](https://code.claude.com/docs/en/setup) documents the native installer and current npm packages; current npm installs a native runtime, so npm is not categorically unsupported. The official [Codex installation guide](https://learn.chatgpt.com/docs/codex/cli), [standalone installer source](https://github.com/openai/codex/blob/main/scripts/install/install.sh) and [npm entry point](https://github.com/openai/codex/blob/main/codex-cli/bin/codex.js) describe its layouts. See also [npm prefix behavior](https://docs.npmjs.com/cli/using-npm/config/), the [Arch Codex file list](https://archlinux.org/packages/extra/x86_64/openai-codex/files/) and the [Claude AUR maintainer's recipe](https://github.com/fabifont/claude-code-aur/blob/main/PKGBUILD). These references were checked on 2026-11-06; installed files and runtime probes still determine compatibility.

Review and install the provider through your chosen trusted source. AI Desktop never runs a provider installer, package manager or updater for you. Shell aliases and functions are not executable sources. Arbitrary shell/JavaScript wrappers and older JavaScript-only clients are not executed under the service account; when automatic discovery cannot resolve a known native layout, use an explicit native path. Merely finding a command in your PATH does not authorize it.

Current Codex packages keep `bin/codex`, `codex-package.json`, `codex-path/` and `codex-resources/` in one relocatable bundle; registration preserves the supported helpers and resources. Older npm layouts with `vendor/TARGET/codex/codex` plus `path/rg`, and older standalone layouts with `release/codex` plus `codex-resources/`, are refused rather than copied incompletely or relocated by guesswork. Update Codex through the installer/package manager you already use, then register its current native bundle. A pacman reinstall is not required. This boundary follows the [minimum-version npm entry point](https://github.com/openai/codex/blob/rust-v0.166.0/codex-cli/bin/codex.js), [installer conversion code](https://github.com/openai/codex/blob/rust-v0.166.0/scripts/install/install.sh) and [runtime resource lookup](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/install-context/src/lib.rs).

## Install AI Desktop and register an existing client

The application can be installed before a provider. From a checkout owned by your regular user:

```fish
python3 -B scripts/install_agents.py codex --install
```

Use `claude`, `both` or `local` instead as appropriate. Without `--install`, the helper reports metadata. It never downloads or changes provider clients. The same general application package is built for every selection. Ignored legacy `packaging/arch/claude-code` and `packaging/arch/codex` files are never embedded or used. `scripts/fetch_codex_client.py` is retained only for explicitly invoked reproducible test fixtures.

For a client installed through `curl` and a shell, npm or another user-owned native installation:

```fish
ai-desktop clients list
ai-desktop clients register claude
# Or: ai-desktop clients register codex
# Custom location: ai-desktop clients register codex --path /absolute/path/to/codex
ai-desktop agent use claude
ai-desktop doctor --clients
ai-desktop login
```

Registration shows the selected native payload and binds approval to its contents, including required resources. A fixed installed administrator helper captures the approved files into `/var/lib/ai-desktop-clients/PROVIDER/payload/`. It never executes the source as root. The manifest stays outside the runtime payload. The original installation, its configuration and its login are not copied or modified. Your ordinary CLI authentication does not become an AI Desktop account; the separate login remains necessary.

This copy is an explicit trust decision: it does not prove vendor authenticity. Writable source files can be registered only through this approval and capture path; they are never run directly as `ai-desktop`. Unsafe file types, privileged attributes, unexpected layouts, mutation during capture and size-limit violations refuse registration. Registration does not bypass the offline compatibility probe before account/model access.

Registrations are **system-wide**, with one active copy per provider. They take priority over a supported system package. A corrupt or incomplete registration is refused rather than silently bypassed. Review a replacement knowing it affects all users of that provider. Remove a registration explicitly to return to the system package, without removing the original client or account data:

```fish
ai-desktop clients remove claude
```

## Inspect and check compatibility

```fish
ai-desktop clients list
ai-desktop doctor
ai-desktop doctor --v
ai-desktop doctor --clients
ai-desktop doctor --clients --v
```

`clients list` inspects discovery and registration metadata, including whether a source needs registration or refresh. Ordinary `doctor` and the panel inspect the selected runtime metadata without executing a client or loading credentials. They distinguish system and registered sources. Runtime states are `present`, `missing`, `unsafe`, `unsupported`, `changed` and `unavailable`. `present` means native metadata passed; it does not prove a complete installation or compatibility. Version and protocol remain unchecked.

`doctor --clients` explicitly probes the **selected** cloud provider in an offline namespace with a fresh empty home and no account profile, proxy or model request. It reports the shared runtime compatibility result and returns nonzero on refusal. Selecting local inference does not require or probe a cloud CLI.

Candidate 81 currently has a reported native CachyOS `client-protocol` refusal after successful Claude registration. Login uses the same prerequisite, so a repeated login is not a remedy. From the reviewed source checkout, `python3 -B tests/host_client_diagnostic.py --allow-offline-client-check claude` first verifies that the installed application matches the checkout, then diagnoses the same fixed probes in their credential-free namespace. It reports only bounded phase/exit/format summaries, a validated version and closed field/error classifications, never native stdout/stderr or account data. On a protocol refusal it may compare the standalone signed-out status probe within the original deadline and bound installation. Exit 2 preserves the refusal; obtaining diagnostics is not successful compatibility acceptance. The separate Codex source metadata helps distinguish an absent launcher from a missing target without executing Codex. No reinstall, re-registration or credential change is required to collect this evidence. Track the result in [ROADMAP.md](ROADMAP.md) before changing the compatibility contract.

| Adapter | Minimum version | Supported version family | Test baseline |
|---|---|---|---|
| Claude Code | 2.1.312 | 2.x | 2.1.312 |
| Codex | 0.166.0 | 0.x | 0.166.0 |

Version text alone is insufficient. Required command/configuration capabilities and protocol prerequisites must also pass the bounded offline probe. A baseline passing those checks is `compatible-tested`; a newer version in the supported family can be `compatible-untested`. That label does not claim every behavior of the new release was tested. The Claude option probe uses the actual adapter options with signed-out `auth status` in an empty offline profile and an unknown-option negative control; `--help` alone is not evidence that those options are accepted. Its actual parser behavior still needs native CachyOS acceptance. Actual replies remain subject to strict validation. Older versions, a different version family, invalid output, incompatible capabilities, missing isolation and timeouts refuse access before credentials or model input are released.

## Updates and stable operations

Update the original client through its own installation method. Supported system packages are selected afresh for each operation. A registered copy retains its approved bytes after the original source updates; approve a replacement explicitly:

```fish
ai-desktop clients list
ai-desktop clients refresh claude
# Or: ai-desktop clients refresh codex
ai-desktop doctor --clients
```

The application does not silently trust a new source digest or download a replacement. A compatible provider update needs no AI Desktop rebuild, application version pin or bundled fallback.

Each operation freezes the selected native executable and supported private helpers/resources into sealed snapshots. Each sequential child receives the same bytes at linked canonical paths on private tmpfs, remounted read-only before execution, so native resource lookup keeps working. No snapshot descriptors are inherited by the client. Limits are 384 MiB per file, 512 MiB combined, 4,096 entries and 24 directory levels. Exceeding them refuses the operation rather than omitting resources. Memory includes up to 512 MiB of retained snapshots plus up to 512 MiB of child tmpfs files. Analysis and native quota services have a 2 GiB ceiling including both copies and client memory; project-test-copy budgets are separate. The same bytes are used across compatibility, account and execution phases. A detected change to the selected system installation or registered runtime copy returns `client-changed` before following phases. Updating the original HOME source does not mutate an already registered copy.

The root-managed operating-system libraries remain a distinct baseline and are not frozen by the client snapshot. The mechanism does not prove vendor provenance or protect against root/kernel compromise.

## Migration from packages 79/80

1. Use your independently installed provider: a supported system package or an existing native/HOME/npm installation.
2. Upgrade AI Desktop. Its package no longer owns vendor executables; pacman removes the old application-owned files through the ordinary package manifest update.
3. If needed, register the existing native client explicitly, inspect `clients list`, select the provider and run `doctor --clients`.
4. Keep existing policy, account profiles, conversations and task journals. Reconnect only when the actual account flow reports that it is needed.

Do not manually delete or copy OAuth files to complete this migration. The separate account boundary remains in place. No fallback uses a user's executable without registration or an old embedded file. Ignored source-checkout leftovers are not packaged.

Application upgrade and uninstall leave independent provider installations and existing account data intact. Registered copies are retained separately from package-owned files; use `clients remove PROVIDER` before removing the application if you want to remove a registration. That action does not uninstall the provider or erase accounts.
