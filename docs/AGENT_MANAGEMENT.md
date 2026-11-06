# Agents, models, conversations and Codex

An agent never adds permissions. Files, commands and network are decided by `~/.config/ai-desktop/permissions.toml`, and writing by your consent.

## Independently installed clients

Candidate 0.1.0-81 uses independently installed Claude or Codex clients through the existing isolated account flows below. The application package contains no vendor executables. System packages work directly; official native/HOME/npm installations need `ai-desktop clients register PROVIDER`. After updating that source, `clients refresh PROVIDER` approves the new bytes. Registration is system-wide and takes priority over the provider package until explicitly removed. Ordinary `doctor` and panel status inspect metadata only; `ai-desktop doctor --clients` explicitly checks the selected client's compatibility offline without a profile, network or model request. The new execution boundary still requires its own CachyOS acceptance. See [client installation, compatibility and migration](AGENT_INSTALL.md).

## Settings and login

`ai-desktop settings` opens a menu in the terminal: arrows and Enter select, Esc goes back. The menu covers the service, agent, model, reasoning level, access, instructions and Skills.
Changing settings does not log in or send anything.

```fish
ai-desktop settings                                  # menu (also: settings menu)
ai-desktop settings show                             # current settings
ai-desktop settings models                           # available models and levels
ai-desktop settings set --provider claude --model sonnet --effort high
ai-desktop ask --agent reviewer --model opus --effort high
```

`ai-desktop login` opens a menu: Claude, ChatGPT / Codex or Cancel. `login --claude` and `login --codex` skip the menu. After a successful login the chosen service becomes active, while a failed
or interrupted login does not change it. Neither `settings` nor `login` changes `permissions.toml`. `--v` shows diagnostics without capturing login data.

The service is shared by all agents. After a service change, an agent returns to the model and level saved for that service; instructions, Skills and access stay shared.

| Service | Models | Reasoning levels |
|---|---|---|
| Claude | `default`, `best`, `fable`, `opus`, `sonnet`, `haiku`, `opusplan` | `default`, `low`, `medium`, `high`, `xhigh`, `max` |
| Codex | `default` or an identifier of the form `[a-z][a-z0-9._-]{0,95}` | `default`, `low`, `medium`, `high`, `xhigh` |

`default` means the client's choice. Model availability depends on the account and the client: there is no automatic switch to another model or service. For `xhigh` and `max` the wait for an answer is 900 s instead of 110 s.

## Agents

An agent is a saved role: service, model, level, access (`changes` or `read-only`), instructions up to 4 KiB and assigned Skills (role plus metadata catalog up to 104 KiB; selected procedures/references use a separate 192 KiB budget). The limit is 24 profiles. A `read-only` agent does not write, does not delete and does not get
commands from the `change` list.

```fish
ai-desktop agents setup                  # adds missing roles and Skills, does not change your settings
ai-desktop agents                        # menu in the terminal, a list outside it
ai-desktop agents list
ai-desktop agents show reviewer
ai-desktop agents create python-review --model opus --effort high --access read-only --instructions 'Review Python code.'
ai-desktop agents use python-review
ai-desktop agents update python-review --skill code-review
ai-desktop agents update python-review --no-skills
ai-desktop agents copy python-review python-copy
ai-desktop agents use default
ai-desktop agents remove python-review   # only inactive, not the default
```

New configurations expose the default, diagnostic, programmer and reviewer catalogs immediately. Explicit `agents setup` adds missing predefined roles and fills empty predefined selections; it preserves non-empty custom assignments and the other role fields. The resulting presets are:

| Profile | Skill | Access |
|---|---|---|
| `default` | 21 general Arch/CachyOS, diagnostics and development Skills, including `concise-assistant` | `changes` |
| `diagnostic` | CachyOS diagnostic, desktop documentation and Arch procedures (17 for an empty selection) | `read-only` |
| `programmer` | `code-development` | `changes` |
| `reviewer` | `code-review` | `read-only` |
| `analyst` | `project-analysis` | `read-only` |

Profiles are in the private `~/.config/ai-desktop/agents.json`. In `material` and `project` the commands `/settings`, `/agent NAME`, `/model ALIAS` and `/effort LEVEL` change the next question when nothing is running. In `ask`, `/provider NAME` can transfer the current conversation with explicit consent.

The program runs only one role in the foreground. The client's native subagents and MCP servers are disabled.

## Skills

```fish
ai-desktop skills                 # list; your own are marked
ai-desktop skills show NAME
ai-desktop skills validate
ai-desktop skills menu            # assignments of the active agent
ai-desktop skills new NAME        # ~/.config/ai-desktop/skills/NAME/SKILL.md
```

A Skill is a procedure of up to 96 KiB, without executable tools or scripts. It may include bounded documentation under `references/`, loaded individually on demand. The package contains general Skills for CachyOS (`cachyos-*`) and Arch Linux (`arch-*`), for programming and analysis, and `desktop-documentation`, which points to desktop documentation; the CachyOS Skills deliberately do not describe desktops and compositors. Your own Skill lives outside the repository and the package, and with the same name it takes priority over the general one.
A name consists of lowercase letters, digits and hyphens. Do not put secrets in it: assigned metadata goes to the model, and a selected procedure or reference sends its content. A path written in the main Skill procedure or in an agent's instructions counts as given by you, so the model can read it without discovery; the policy and `deny` still decide.

Reference documents do not ground local paths or grant reads. New configurations expose the general catalog; existing saved assignments, including an empty list, are preserved.

Changing assigned Skills may require a new conversation, because a saved conversation is tied to the agent's configuration. Loaded resources can be reused in a running conversation after revalidation, but their loaded state is not restored from a saved session. Version-specific reference text is withheld when the installed version is unknown or mismatched; rolling references remain explicitly unverified for that version.

## Conversation in `ask`

Run `ai-desktop ask` once and keep working: after an answer, `>` comes back. Arrows edit the line and recall earlier messages. An empty line or two Ctrl-D end the conversation. Ctrl-C while waiting stops only the current turn. `ask --question "..."` is a single question.

**Consent.** One `Send this request to Claude? [y/n]:` per conversation. Asked again after a change of permissions, service or agent. It lives only in the process's memory, so after a conversation is resumed the program asks again. Writes and changes have their own consents. Current project suites also ask before running the permitted local copy. The next flow will approve a declared plan, its stages and copy tests once, with separate final diff/write approval; this simplification is not implemented by client discovery.

**Memory has two layers:**

1. Up to 64 recent exchanges within 512 KiB (question up to 128 KiB, answer up to 32 KiB with visible truncation).
2. Task memory: up to 96 entries (1 KiB each, 64 KiB in total) with the goal, criterion, attempts, values, corrections and decisions. The model proposes updates in its answer, and when out of room the oldest unprotected entries are dropped first. The program adds facts itself: what it ran and with what exit code, what it applied, what was refused.

Memory gives no permissions. When access to a file is revoked, the answers and model entries that depend on it are removed before the next question, and the program says so. Only the latest 64 exchanges can be included verbatim in a request.

## Saved conversations

Interactive `ask` saves locally by default after the first completed turn or completed action. The first checkpoint is published as one complete private record; declining or cancelling before a completed turn creates no empty file. Each later completed turn updates that record atomically. The next `ai-desktop ask` offers conversations of the same agent. Use `--no-save` for a temporary in-memory conversation. `--session new` creates an explicit record, and `--session ID` resumes one (also for `material` and `project`). `--no-save` and `--session` cannot be combined.

```fish
ai-desktop sessions          # menu, a list outside the terminal
ai-desktop sessions list
ai-desktop sessions show ID
ai-desktop sessions resume ID
ai-desktop sessions remove ID
```

The save is in `~/.local/state/ai-desktop/conversations` (directory `0700`, files `0600`, up to 16 MiB per file, 128 conversations). It contains bounded recent text, task memory and source-provenance metadata, not whole materials, although an answer may quote a file. It contains no OAuth credentials, reusable grants, write authorizations or native-client session. Resuming re-checks the policy, the file or project, recipient and agent configuration. A storage or integrity failure leaves the current answer on screen and the conversation in memory; only the last successful checkpoint survives exit. A conversation open in another process will not be removed. `ai-desktop stop` concerns the temporary client session, not saved conversations.

## Commands and changes on the live system

The description of `[commands]` in `permissions.toml` is in [README](../README.md#commands-commands), and the boundaries are in the [threat model](THREAT_MODEL.md#4-command-boundary). In short: `allow` runs in the sandbox without asking, up to 16 commands per step and `command_rounds` per question. `change` runs on the live system after you type `yes`, with a test and an undo. With `systemctl --user` the sandbox has no access to the user session, so such commands may return nothing.

## Automatic discovery in `ask`

You do not have to name files. Within one consent the model carries out a finite series of steps:

1. `list`: entries (name, type, size) from up to four directories.
2. `find`: search by a fragment of the name, without content.
3. `search-content` / `read-range`: bounded content matches or line ranges from authorized, grounded paths, with hashes and coverage.
4. `read`: selected files from paths shown above or typed by you. A guessed path is `discovery-required`.

`deny` acts first: a name covered by `deny` is not listed, counted or sent. Refusals have fixed codes and contain no system text. Exceeding a budget names the key; the program does not send a selected part as if it were the whole. The budgets are in the table in [README](../README.md#first-run-and-permissions). Every step and correction is an additional request to the model.

In `version = 2` there is no hidden list of names. If `read` covers `~/.config/ai-desktop/` and it is not in `deny`, the model can read there (the template rules this out).

**Symlinks in `version = 2`.** A symlink is followed only if there is one, it belongs to you, it points to a plain directory name next to it (no `/`, `.`, `..`), the parent directory is not writable by others, and both the symlink and the target are allowed. The state is checked on read and before each send. Loops, chains, symlinks to files and special files are `files-symlink`. This exception does not apply to the policy, `material`, `project` or writing.

## Codex through a ChatGPT subscription

Codex is selected and enabled independently of Claude. Treat this adapter as experimental. Install the system `openai-codex` package independently, then use its shared compatibility check and the existing AI Desktop account flow:

```fish
ai-desktop agent use codex
ai-desktop doctor --clients
ai-desktop codex status
ai-desktop login --codex
ai-desktop ask --agent reviewer
ai-desktop codex logout --allow-chatgpt-logout
```

In `[network]` set `codex = true`: the permissions for Claude and Codex are separate, and the policy is not changed for you. Compatible client updates do not require rebuilding AI Desktop. Registered sources require an explicit `clients refresh codex`; otherwise the previously approved copy remains selected. Native client or resource replacement during an operation refuses further phases; begin a new operation after the package update. SIWC account checks/logout and native quota disconnection remain available if the native client has been removed.

Login is the official "Sign in with ChatGPT" flow handled by a trusted component of the service account. It does not copy existing credentials and does not use an API key. Tokens are in `/var/lib/ai-desktop/codex` and remain after the package is uninstalled unless you log out; logging out also tries to revoke the credential and says so if there is no confirmation.

The client gets an empty `HOME` and `CODEX_HOME`, an empty working directory and no mounted token store. A short-lived token goes through a private channel to the client process and does not reach the question or the arguments. The proxy lets through only `api.openai.com`. The client's shell, agents, MCP, Skill detection and search are disabled. An unexpected tool event, a failed turn or an exceeded budget ends the request without a retry. Rejecting an event will not undo an effect that has already happened, so kernel isolation remains necessary.

The list of models in the menu comes from the client, run in a closed sandbox with no network or account. An unknown identifier is rejected by the client as `codex-model-unavailable`. Codex has no web search: choosing this service removes that capability.

## Older commands

`claude` and `codex` are listed in `ai-desktop --help-all`. For daily work use `settings` and `login`. `doctor` shows brief metadata, and `doctor --v` a JSON report; it does not check login or whether the model works.

## Local llama.cpp server

Use an already-running, trusted `llama-server` on the same computer. AI Desktop sends OpenAI-compatible chat completions to `http://127.0.0.1:8080/v1/chat/completions` by default. In the existing `permissions.toml`, enable `local` under `[network]`. The optional `[local]` section selects an unprivileged numeric port (1024–65535). Keep the existing `[files]` rules.

```toml
[network]
local = true

[local]
port = 8080
```

Select the service with `ai-desktop settings set --provider local`, or use `ai-desktop ask --provider local` for one conversation. The `default` model uses the model served by a single-model server. If your server uses an alias, select it with `ai-desktop settings set --provider local --model my-model`. Use an alias of up to 96 letters, digits, dots, underscores or hyphens, beginning with a letter or digit. `ai-desktop doctor` shows the configured address and policy while offline. To actively query the enabled loopback server, run `ai-desktop local models` or `ai-desktop local status`. Both read only `/v1/models`. A successful check confirms that the server returned a model catalog; some servers also advertise unloaded models.

The approval names the recipient and port. The controller rechecks the permission file immediately before releasing the text. It sends the application instructions and the approved request body. Responses are untrusted text processed by the same operation, test and write checks as a remote response. The local adapter rejects native tool calls and redirects. There is no cloud fallback, OAuth, key lookup, server installation or automatic startup. External weather collection, if separately enabled and requested, still contacts the weather service. WebSearch is unavailable on this provider.

The server is a separate, owner-managed program. Its launch options, file access, logging and outbound network are outside AI Desktop's sandbox. Trust extends to whoever can bind the configured TCP port: another local user could claim it while your server is stopped. Use this on a machine you control with a dedicated loopback server, never a reverse proxy that forwards to another recipient. Servers requiring API keys, TLS, streaming-only responses or a remote host are unsupported.

### Separate subscription accounts

The existing subscription for each service is named `default`. Register a named account, complete its private browser login, then select it for new tasks:

```fish
ai-desktop account add claude work --login
ai-desktop account add codex personal --login
ai-desktop account use claude work
ai-desktop account list
ai-desktop usage --json
```

Without `--login`, `add` only registers the name. Run `ai-desktop account login claude work` later. In a terminal, `account add` and `account use` can prompt for missing choices. Selecting an account changes new requests for that provider. The original login command operates on the legacy default subscription. Named slots are separate for each desktop UID and provider. The authenticated caller determines the UID. The service mounts only the selected Claude profile or reads only the selected Codex auth store. The selected name is part of the approved request. An already connected name cannot be relinked to a different subscription.

To replace an expired named login, connect and select a fresh name. To **permanently disable** the old name, run `ai-desktop account retire claude work --confirm-retire` from your terminal. This records a permanent local tombstone and removes only that named profile's local credentials. It also blocks saved conversations bound to that name from reaching the provider. Revoke any remaining remote grant or session through the provider. The legacy default profile has its own lifecycle.

### Sign-in state and disconnection

A successful sign-in records the provider and local account alias for the desktop. Terminal output identifies the account and distinguishes success, cancellation and failure, including in plain text when colors are disabled. A login completed on an earlier release may show an unknown state. To check an existing profile, use **Check sign-in…** in the widget or run `ai-desktop account check codex default` (replace the provider and alias as appropriate). This explicit action checks local credentials under the service account; if the check cannot complete, the previous state is retained. The displayed state describes the local connection. The provider can subsequently expire or revoke a session.

Use **Sign out…** or `ai-desktop account logout PROVIDER ALIAS` to disconnect a specific slot after confirming the target. The legacy default profile is shared by desktop users of this installation. Use a fresh named slot when switching identities across conversations. A named Claude slot closes on logout. To sign in again, add another alias. A named Codex slot can reconnect to the same verified account when its identity binding is available. If Codex signs out locally but cannot confirm remote revocation, use ChatGPT settings to disconnect the app.

### Quota readings

The home panel shows the selected account for the active service. Choose **Subscription limits** to browse all accounts and **Back** to return. `ai-desktop usage --provider claude` prints the same structured windows. Reset and observation times in terminal views use your local time zone. Live percentages use the terminal's green, yellow (75%) and red (90%) colors. Use `NO_COLOR=1` for plain text. JSON keeps the original timestamps and UTC summary for consumers.

By default the terminal and widget read local observations. An approved conversation may deliver native quota events. A partial event preserves the other window and its observation time. Expired readings are labelled as historical.

For an on-demand Codex account snapshot, use the native quota connection for a signed-in local slot. These fish commands use the default slot:

```fish
ai-desktop account connect-limits codex default
ai-desktop usage --refresh --provider codex --account default
ai-desktop usage --provider codex
```

The first command asks for confirmation and opens the native Codex device login. Choose the intended workspace in the browser. The setup enables a dedicated socket for your desktop UID, including after reboot. Subsequent manual refreshes use that socket. The second command performs an authenticated `account/rateLimits/read` and updates the local snapshot. The third displays the cached result. A current permission policy allowing Codex is required for connecting and requesting a reading. An explicit disconnect can still remove the local credential when network access is disabled.

The CLI labels the linked reading **Native Codex limits**. Its values apply to the account and workspace chosen during device login. AI Desktop's Sign in with ChatGPT session authorizes conversations separately. The two workspaces can differ, including when the same email address is used. Passive conversation observations and native account snapshots are kept separately. Missing windows stay unknown. For app-specific usage, open [ChatGPT settings](https://chatgpt.com/settings/usage).

Run `ai-desktop account disconnect-limits codex default` to remove the separate local grant. Disconnecting the last retained native slot stops and disables its per-user socket. A change to the conversation sign-in invalidates the native association. The widget or Codex account picker offers an explicit cleanup action for a retained native credential, even if the chat slot was signed out or retired.