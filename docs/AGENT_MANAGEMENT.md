# Agents, models, conversations and Codex

An agent never adds permissions. Files, commands and network are decided by `~/.config/ai-desktop/permissions.toml`, and writing by your consent.

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

`default` means the client's choice. Model availability depends on the account and the client: there is no automatic switch to another model or service. For `xhigh` and `max` the wait for an answer is 260 s instead of 110 s.

## Agents

An agent is a saved role: service, model, level, access (`changes` or `read-only`), instructions up to 4 KiB and any number of Skills (up to 104 KiB in total). The limit is 24 profiles. A `read-only` agent does not write, does not delete and does not get
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

After `agents setup`:

| Profile | Skill | Access |
|---|---|---|
| `default` | `concise-assistant` | `changes` |
| `diagnostic` | `cachyos-diagnostics` | `read-only` |
| `programmer` | `code-development` | `changes` |
| `reviewer` | `code-review` | `read-only` |
| `analyst` | `project-analysis` | `read-only` |

Profiles are in the private `~/.config/ai-desktop/agents.json`. In `material` and `project` the commands `/settings`, `/agent NAME`, `/model ALIAS` and `/effort LEVEL` change the next question when nothing is running. Changing the service requires a new conversation.

The program runs only one role in the foreground. The client's native subagents and MCP servers are disabled.

## Skills

```fish
ai-desktop skills                 # list; your own are marked
ai-desktop skills show NAME
ai-desktop skills validate
ai-desktop skills menu            # assignments of the active agent
ai-desktop skills new NAME        # ~/.config/ai-desktop/skills/NAME/SKILL.md
```

A Skill is an instruction of up to 96 KiB, without tools, scripts or additional files. The package contains general Skills for CachyOS (`cachyos-*`) and Arch Linux (`arch-*`), for programming and analysis, and `desktop-documentation`, which points to desktop documentation; the CachyOS Skills deliberately do not describe desktops and compositors. Your own Skill lives outside the repository and the package, and with the same name it takes priority over the general one.
A name consists of lowercase letters, digits and hyphens. Do not put secrets in it: the content of an assigned Skill goes to the model. A path written in a Skill or in an agent's instructions counts as given by you, so the model can read it without discovery; the policy and `deny` still decide.

Changing assigned Skills may require a new conversation, because a saved conversation is tied to the agent's configuration.

## Conversation in `ask`

Run `ai-desktop ask` once and keep working: after an answer, `>` comes back. Arrows edit the line and recall earlier messages. An empty line or two Ctrl-D end the conversation. Ctrl-C while waiting stops only the current turn. `ask --question "..."` is a single question.

**Consent.** One `Send this request to Claude? [y/n]:` per conversation. Asked again after a change of permissions, service or agent. It lives only in the process's memory, so after a conversation is resumed the program asks again. Writes and changes have their own consents.

**Memory has two layers:**

1. The last 32 exchanges, up to 128 KiB in total (question up to 128 KiB, answer up to 8 KiB with visible truncation).
2. Task memory: up to 40 entries (400 B each, 10 KiB in total) with the goal, criterion, attempts, values, corrections and decisions. The model proposes updates in its answer, and when out of room the oldest unprotected entries are dropped first. The program adds facts itself: what it ran and with what exit code, what it applied, what was refused.

Memory gives no permissions. When access to a file is revoked, the answers and model entries that depend on it are removed before the next question, and the program says so. The program does not remember turns older than 8, apart from what went into task memory.

## Saved conversations

By default nothing is saved. At the end of an `ask` conversation the program asks whether to keep it; only `y` or `yes` creates a local save. The next `ai-desktop ask` offers conversations of the same agent. `--session new` saves from the start, `--session ID` resumes (also `material`, `project`).

```fish
ai-desktop sessions          # menu, a list outside the terminal
ai-desktop sessions list
ai-desktop sessions show ID
ai-desktop sessions resume ID
ai-desktop sessions remove ID
```

The save is in `~/.local/state/ai-desktop/conversations` (directory `0700`, files `0600`, up to 64 KiB per file, 128 conversations). It contains the limited text of the conversation, memory and metadata, not whole materials, although an answer may quote a file. Resuming re-checks the policy, the file or project and the agent's configuration. Consents, write authorizations and the login session are not restored. A conversation open in another process will not be removed. `ai-desktop stop` concerns the temporary client session, not saved conversations.

## Commands and changes on the live system

The description of `[commands]` in `permissions.toml` is in [README](../README.md#commands-commands), and the boundaries are in the [threat model](THREAT_MODEL.md#4-command-boundary). In short: `allow` runs in the sandbox without asking, up to 16 commands per step and `command_rounds` per question. `change` runs on the live system after you type `yes`, with a test and an undo. With `systemctl --user` the sandbox has no access to the user session, so such commands may return nothing.

## Automatic discovery in `ask`

You do not have to name files. Within one consent the model carries out a finite series of steps:

1. `list`: entries (name, type, size) from up to four directories.
2. `find`: search by a fragment of the name, without content.
3. `read`: only paths shown in the steps above or typed by you. A guessed path is `discovery-required`.

`deny` acts first: a name covered by `deny` is not listed, counted or sent. Refusals have fixed codes and contain no system text. Exceeding a budget names the key; the program does not send a selected part as if it were the whole. The budgets are in the table in [README](../README.md#first-run-and-permissions). Every step and correction is an additional request to the model.

In `version = 2` there is no hidden list of names. If `read` covers `~/.config/ai-desktop/` and it is not in `deny`, the model can read there (the template rules this out).

**Symlinks in `version = 2`.** A symlink is followed only if there is one, it belongs to you, it points to a plain directory name next to it (no `/`, `.`, `..`), the parent directory is not writable by others, and both the symlink and the target are allowed. The state is checked on read and before each send. Loops, chains, symlinks to files and special files are `files-symlink`. This exception does not apply to the policy, `material`, `project` or writing.

## Codex through a ChatGPT subscription

Codex is optional and enabled separately. Treat it as experimental. The package uses the official **Codex 0.166.0** client ([release](https://github.com/openai/codex/releases/tag/rust-v0.166.0)).

```fish
cd ~/ai-desktop
python3 -B scripts/fetch_codex_client.py    # downloads the archive, checks SHA-256, does not run the client
# rebuild and install the package
ai-desktop codex status
ai-desktop login --codex
ai-desktop ask --agent reviewer
ai-desktop codex logout --allow-chatgpt-logout
```

In `[network]` set `codex = true`: the permissions for Claude and Codex are separate, and the policy is not changed for you. The script writes `packaging/arch/codex` and does not overwrite an existing file.

Login is the official "Sign in with ChatGPT" flow handled by a trusted component of the service account. It does not copy existing credentials and does not use an API key. Tokens are in `/var/lib/ai-desktop/codex` and remain after the package is uninstalled unless you log out; logging out also tries to revoke the credential and says so if there is no confirmation.

The client gets an empty `HOME` and `CODEX_HOME`, an empty working directory and no mounted token store. A short-lived token goes through a private channel to the client process and does not reach the question or the arguments. The proxy lets through only `api.openai.com`. The client's shell, agents, MCP, Skill detection and search are disabled. An unexpected tool event, a failed turn or an exceeded budget ends the request without a retry. Rejecting an event will not undo an effect that has already happened, so kernel isolation remains necessary.

The list of models in the menu comes from the client, run in a closed sandbox with no network or account. An unknown identifier is rejected by the client as `codex-model-unavailable`. Codex has no web search: choosing this service removes that capability.

## Older commands

`claude` and `codex` are listed in `ai-desktop --help-all`. For daily work use `settings` and `login`. `doctor` shows brief metadata, and `doctor --v` a JSON report; it does not check login or whether the model works.
