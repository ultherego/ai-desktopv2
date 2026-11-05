# Prepare the official clients from a checkout

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
