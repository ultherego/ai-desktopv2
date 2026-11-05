# AI Desktop

A terminal assistant for diagnostics, configuration and file work on CachyOS and Arch Linux x86_64. In a terminal, start `ai-desktop`, write a plain sentence, and the program itself collects the data your `~/.config/ai-desktop/permissions.toml` file allows and sends it to the model. Every write shows a full diff and waits for your consent.

The interface and settings are in English, and Claude answers in the language of your question.

Underneath runs the official Claude Code client in the exact version **2.1.312**, which you download yourself. The client works under a separate system account `ai-desktop`, in a Bubblewrap sandbox, and reaches the network only through a filtering proxy. Codex uses a separate subscription adapter. For a local model, the controller can send an approved request to a configured loopback server.

**What this is not:**

- It is not a background service or an autonomous agent. It runs when you ask it something.
- The model gets no shell and no tools. It proposes steps, and the program checks them and runs only what the permissions and your consent allow.
- It does not use an API key and does not switch to another service when something fails.
- It is not certified isolation or a secret scanner. See [SECURITY.md](SECURITY.md).

## Choose where to work

| In a terminal | Result |
|---|---|
| `ai-desktop` | Start or resume a conversation. Outside a terminal the default remains offline status. |
| `ai-desktop agent list` / `ai-desktop agent use local` | Inspect services or choose the default for future conversations. |
| `ai-desktop agents use reviewer` | Select a role and its Skills. A role cannot expand the access policy. |
| `/provider codex` inside `ask` | Review the destination and the context the new recipient would receive. Confirm to continue the same conversation there. |
| `ai-desktop account list` / `ai-desktop usage` | See named accounts and provider-reported quota windows with their observation times. |
| `ai-desktop panel` / `ai-desktop status --json` | Open the interactive panel or read local metadata for a desktop widget. |
| `ai-desktop local models` | Explicitly query the model list of your configured local inference server. |

For local inference, start your own trusted OpenAI-compatible server on `127.0.0.1`, enable `local = true` under `[network]` in your existing policy, then choose `ai-desktop agent use local`. The default port is 8080. Cloud accounts and the local service have independent permissions. There is no automatic recipient switch.

See [agent settings, accounts and sessions](docs/AGENT_MANAGEMENT.md), [verified client installation](docs/AGENT_INSTALL.md) and [Niri shortcuts and the DMS widget](docs/DESKTOP.md).

## Development status

This is a development version, not a 1.0 release.

The current candidate package is **0.1.0-72** (changes: [CHANGELOG.md](CHANGELOG.md)).

What is still missing and what has not been checked is described in [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). Read that file before using this on important data. The plan up to version 1.0: [docs/ROADMAP.md](docs/ROADMAP.md), tasks: [docs/TODO.md](docs/TODO.md).

## Installation on a laptop (CachyOS / Arch x86_64)

The example below installs and connects Claude for a regular user with a terminal and `sudo`. For Codex, use the verified client installer linked above. Local inference uses your running server and requires no subscription login.

```fish
# 1. Dependencies and build tools
sudo pacman -S --needed base-devel git fish python bubblewrap libseccomp sudo python-rich python-cryptography

# 2. Code
git clone https://github.com/ultherego/ai-desktopv2.git ~/ai-desktop
cd ~/ai-desktop/packaging/arch

# 3. Official Claude Code 2.1.312 client (once). With a wrong checksum the file will not be run,
#    and the build in step 4 will refuse: then delete the file and download it again.
curl -fL -o claude-code https://downloads.claude.ai/claude-code-releases/2.1.312/linux-x64/claude
echo '4f4178ce41d83e23341ebfbaecdbcf8ea6bf7baea0690025abe49eeec3a5d01d  claude-code' | sha256sum -c -
and chmod 755 claude-code
and ./claude-code --version  # must print: 2.1.312 (Claude Code)

# 4. Build and install the package (the version in the file name = pkgrel from PKGBUILD)
makepkg --cleanbuild
sudo pacman -U ai-desktop-0.1.0-72-x86_64.pkg.tar.zst
sudo systemctl daemon-reload

# 5. Log in to Claude (interactive, once)
ai-desktop login

# 6. Your access policy: the program reads nothing you do not list here
mkdir -p ~/.config/ai-desktop
cp ~/ai-desktop/config/permissions.example.toml ~/.config/ai-desktop/permissions.toml
chmod 600 ~/.config/ai-desktop/permissions.toml
$EDITOR ~/.config/ai-desktop/permissions.toml     # fill in files.read; deny always wins

# 7. Check and start
ai-desktop doctor
ai-desktop ask --preview      # shows the permissions, without the model and without sending anything
ai-desktop ask                # conversation
```

Notes:

- **Claude client (step 3).** The package does not download it itself. Without the `claude-code` file in `packaging/arch/` the Claude adapter is unavailable. Selected Codex and local adapters have their own prerequisites. Use of the client is subject to Anthropic's terms. Do not add this file to Git and do not redistribute it.
- **Sudo.** The client session is started by a fixed helper with administrator rights, so `ask`, `material`, `project`, `task run`, `login` and `stop` may ask for your `sudo` password. The package adds no sudoers or polkit rules.
- **Requirements.** Python 3.12 or newer, systemd, working unprivileged namespaces and a regular terminal: without one the program does not ask for consent, it refuses.
- **Fresh system.** These steps have not yet been repeated on a completely fresh installation.

## First run and permissions

`ai-desktop ask --preview` shows the access without the model and without reading data. Without a `permissions.toml` file only general questions work: no files, systemd, commands, weather or web search.

Example policy (`config/permissions.example.toml` is the full template):

```toml
version = 2

[network]
claude = true
codex = false
weather = true       # Open-Meteo
web = false          # web search through Claude

[system]
systemd = ["system", "user"]

[files]
read = ["~/Projects/my-project/"]
write = ["~/Projects/my-project/"]
deny = ["~/.ssh/", "~/.gnupg/", "~/.claude/", "~/.config/ai-desktop/"]

[commands]
allow = ["uptime", "systemctl status <unit> --no-pager", "journalctl -b -p err --no-pager -n <int>"]
```

How to read this file:

- A missing field means refusal, including `network.claude`. An unknown key or a bad value stops the program, and the message points to the problem.
- The file must belong to you and have mode `0600`. Its directories must not be links or writable by others. The limit is 16 KiB, 64 rules per list and 128 command templates per list.
- `~/x` is one file, `~/x/*` is the files directly in the directory, and `~/x/` and `~/x/**` are the directory with its subdirectories. `~/` is the whole home directory. Two rules with the same scope (`~/` and `~/**`) are an error.
- **`deny` wins and protects everything under the given path**, regardless of how it is written: `~/x`, `~/x/`, `~/x/*` and `~/x/**` work the same.
- In `version = 2` there is no hidden list of forbidden names and no detection of private keys. **`deny` is the only protection for secrets in an allowed tree.** The template contains a list of common credential directories; you can change it, but it does not recognize all secrets. The older `version = 1` format is still recognized and has built-in exclusions.
- Editing an existing file requires `read` and `write`. A new file requires `write` and an existing directory: the program does not create directories. Deleting requires `write`.

The fields below are optional. Add them in the given section when you want to change the default value. The program rejects out-of-range values.

| Section and field | Default | At most | Meaning |
|---|---|---|---|
| `[files] file_size` | `"5MiB"` | `"16MiB"` | largest file for `material` |
| `[files] part_size` | `"240KiB"` | `"512KiB"` | portion when analyzing a whole file |
| `[files] read_files` | 64 | 128 | files in one context |
| `[files] read_file_size` | `"64KiB"` | `"128KiB"` | bytes per file |
| `[files] read_total_size` | `"128KiB"` | `"256KiB"` | bytes of all context files |
| `[files] listing_entries` | 128 | 512 | entries in a list/find step |
| `[files] discovery_bytes` | `"128KiB"` | `"256KiB"` | bytes of discovery steps sent to the model in one question |
| `[files] search_results` | 40 | 200 | matches in a find step |
| `[files] search_scan` | 20000 | 100000 | directory entries per question |
| `[files] discovery_rounds` | 4 | 8 | list/find steps per question |
| `[files] plan_corrections` | 2 | 4 | read plan corrections per question |
| `[files] command_rounds` | 24 | 32 | commands per question (0 turns them off) |
| `[files] command_output` | `"8KiB"` | `"128KiB"` | bytes of the output of one command |
| `[files] command_timeout` | 20 | 60 | seconds per command |
| `[commands] share_processes` | `false` | | `true`: commands see host processes and their command lines |
| `[commands] change` | none | | change templates: they run on the live system and require `yes`, a test from `allow` and an undo |

`read_file_size` must not exceed `read_total_size`. Regardless of these fields, fixed limits apply: 384 KiB of JSON context and 512 KiB for the whole request. Files changed by the model are limited to 64 KiB per file and 128 KiB in total, even if you raise the read limits.

### Commands: `[commands]`

By default the model runs nothing. `allow` is a list of exact templates, without a shell. `<unit>` means one systemd unit name, `<int>` a number up to 99999, and `<word>` one simple word. Templates starting with a shell, an interpreter, `sudo`, a network client or a writing program (`rm`, `tee`, `git`, `find`, `sed` and similar) are rejected when the policy is loaded.

For Docker, Btrfs and nmcli, give the complete command path before a placeholder, for example `docker container inspect <word>`. Spell out unknown subcommands and plugin arguments literally. Host Unix sockets may still be reachable, so review the operation itself before adding it to `allow`.

**Commands from `allow` run without asking, and their output goes to the model.** Remove from the list what you do not want to share, including any extra `journalctl`, `ps aux` or `docker ps` rules you add. `deny` does not filter command output.

Commands from `allow` run in a sandbox: read-only file system, hidden `/home`, `/root` and `/run/user`, no network and no input. It does not isolate host Unix sockets, so only list commands that change nothing. Some programs will not see the host state (e.g. `ip`, `wpctl`, `glxinfo`).

A placeholder must not stand where a tool's command could be. Write an option that takes a value together with it (`--output=json`) or give the command further on: `systemctl -H host restart <unit>` is valid, and `systemctl -H host <word>` is not.

`change` holds commands that change the system, e.g.:

```toml
[commands]
allow = ["systemctl status <unit> --no-pager"]
change = ["systemctl restart <unit>"]
```

The model must give the change together with a test from the `allow` list and an undo from the `change` or `allow` list. You see all three and type the whole word `yes`. The program runs the change, then the test. When either fails, it shows the undo and again requires `yes`: it never undoes anything by itself. **The change, test and undo run on the live system, with your permissions, without a sandbox**, with a cleaned environment, no input and a time limit.

## Daily work

Everywhere, `--v` shows details and the data being sent, and does not change consents. `--format plain` turns off answer formatting, `--format markdown` prints the source Markdown (`ask`, `material`, `project`, `task run`).

### `ai-desktop ask`

```fish
ai-desktop ask
ai-desktop ask --question "Why does the unit not start?"
ai-desktop ask --question "What is an inode?" --no-context
```

Without `--question` this is one conversation: after each answer `>` comes back. An empty line or two Ctrl-D end the conversation, and the program asks whether to save it. Ctrl-C while waiting stops only the current turn. A message can be up to 128 KiB including pastes (a large paste appears as a placeholder).

How consents work:

1. **One `Send this request to Claude? [y/n]:` per conversation.** It covers further questions, reads and commands from `allow`. The program asks again when `permissions.toml` or the chosen service or agent changes.
2. **Reads and discovery without asking.** The model lists and searches names (not content), then reads what it found or what you typed. A guessed path is rejected. The budgets are in the table above.
3. **Commands from `allow` without asking**, up to 16 in one model step and 24 per question (`command_rounds`).
4. **Each write separately.** An edit or a new file may follow from earlier discovery. The program checks the proposal on an isolated copy, shows the full diff and asks `[y/n]`. A failed test blocks the write.
5. **Deletion separately.** The program lists the files and asks `[y/n]`. Files are not erased, they go to the task trash: at most 8 files at a time, up to 16 MiB each.
6. **Changes from the `change` list** require typing `yes`.

After an applied change the model takes the next step by itself or finishes the answer, at most 5 automatic steps. Each further write again shows the diff and asks. A refusal or error hands the turn back to you. Type `continue` to go on after the limit.

In a normal conversation, ask to run the current project's tests or type `/test pytest`, `/test python-unittest`, `/test go-test` or `/test npm-test`. Start AI Desktop in a project already covered by a recursive `files.read` rule and use a profile that permits execution. Before each run the controller shows the current directory, a snapshot identifier and the number of indexed and excluded files. It asks before running the fixed recipe on a temporary copy. Raw test output stays in the terminal. The conversation remembers the result for that snapshot. Go and npm require a systemd user manager with delegated CPU, memory and PID controllers and run without downloading dependencies.

The check on the copy comes from a list: `python-unittest`, `pytest`, `go-test`, `npm-test`, `python-syntax`, `json`, `toml`, `bash-syntax`, `fish-syntax`, `text`. The `text` check only confirms valid UTF-8 text: **syntax and behaviour are not checked**. No check proves the project is correct.

A conversation remembers the last 32 exchanges (up to 128 KiB in total, the oldest drop out first) and a short task memory. A long history uses up the service limit, because it goes with every question. If you revoke access to a file, the answers that depend on it disappear from memory before the next question. A timeout or service limit ends the conversation, and unsaved history is lost.

You turn on web search with `web = true`. It is a separate request: only your question, without files or local data. Weather (`weather = true`) reveals the place name and your IP address to the Open-Meteo service, and the result goes to the model.

During a conversation you can type a command starting with `/`. After you type `/` a list appears under the line, arrows select, Tab completes. Commands work locally, send nothing and do not erase the conversation: `/help`, `/model`, `/effort`, `/skills`, `/agent`, `/settings`, `/permissions`, `/exit`. After a change of model or Skills the program asks once more for consent to send. `/skills` works in this conversation; use `ai-desktop agents` to make it permanent. A question starting with a path, e.g. `/etc/fstab what is it?`, goes to the model normally. To send a single word starting with `/`, start with a space.

### `ai-desktop material FILE`

One UTF-8 text file up to `file_size`, covered by `files.read`. You ask in a plain sentence; nothing is sent before the question.

- A file up to 200 KiB goes to the model whole.
- With a larger one the model asks for searches and line reads, at most 8 requests per question, up to 256 KiB per request and 2 MiB in total. You see each step, and the rest of the file is not sent.
- "Analyze the whole file": the program splits the file into parts, shows a plan and asks once for the whole task. The consent expires after 30 minutes. At most 24 parts, so a larger file needs a larger `part_size`. Coverage is counted by the program, and an interrupted analysis is marked as partial.
- The answer gives the lines the model really received. Session memory is the last 4 questions and answers.
- No writing, no Internet and no running code. Refusals cover among others an empty file, a link, control characters and invalid UTF-8.

Local commands that send nothing: `/find TEXT`, `/show A-B`, `/log`, `/exit`. `/ask A-B QUESTION` narrows the question to those lines. The service accepts 4 session starts per minute, so the program waits between steps by itself.

### `ai-desktop project DIRECTORY`

The project editor receives the authorized conversation and observations behind the proposed edit. Type `/test pytest`, `/test python-unittest`, `/test go-test` or `/test npm-test` to run a fixed suite on an isolated copy of the authorized index. The controller requests approval for the copy. On a size refusal it reports the measured file count or byte total, the relevant policy setting and the fixed copy ceiling (64 files, 64 KiB per file and 128 KiB total). A refused copy runs no tests. The test output stays in your terminal. Go and npm need a delegated systemd user scope with verified cgroup v2 limits and the corresponding local toolchain. Recipes never install dependencies.


One directory covered by `files.read`. The scan is local and repeated before each question. Limits: 20,000 files, 100,000 entries, depth
16. Entries from `deny`, links, special files and `.git` are skipped and counted.

- **Reading.** One consent per question. The model asks for names, a content search and line reads, at most 8 requests and 2 MiB of data. File names, matches and fragments are sent. With a large project you approve further stages (up to 5) one by one.
- **Changes.** This mode is not read-only: after a request for a change the model proposes files, and from there the same flow as in `ask` applies (consent to send, test on a copy, diff, separate consent to write, undo). This needs a `files.write` entry with the directory and subdirectories covering the project, e.g. `~/Projects/**`. Restrictions: 8 files, 5 path levels, 64 KiB per file.
- **Deletion.** A list of files, `[y/n]`, the task trash and a printed restore command.
- Project suites can run on the approved copy. The project mode has no general-purpose command runner or web search.

Local commands: `/files [DIR]`, `/search TEXT`, `/refresh`, `/log`, `/test RECIPE`, `/exit`. Large pastes (up to 32 KiB) appear as a placeholder and go to the model as data. Enter sends, and Alt+Enter breaks the line. Two Ctrl+C within 3 seconds end the session.

### `ai-desktop task`

A change to exactly the files you name, without `permissions.toml`: the choice is yours.

```fish
ai-desktop task run "Fix the addition in calc.py" --workspace /path/to/project \
    --file calc.py --context test_calc.py --test python-unittest
```

`--file` may be changed, `--context` is read-only. The directory must be given as an absolute path and must not be the whole home directory. Limits: 64 input files, 32 proposed, 64 KiB per file, 128 KiB in total, instruction up to 128 KiB. Tests run on a copy, without network or home directory. Python tests cannot create processes, threads or sockets, so not every test suite will work here. `--preview` shows the data without the model or a test. There are two consents: to send and to write.

### Undo and restore

After a write the program prints the ready command.

```fish
ai-desktop task status   --workspace DIRECTORY              # states, including the trash
ai-desktop task rollback --workspace DIRECTORY              # undo a write, separate consent
ai-desktop task restore  --workspace DIRECTORY --plan ID    # bring deleted files back
```

- Undo compares **bytes and file mode**, so an editor saving the same content does not block it. When the file was changed later, undo refuses and leaves your version.
- Such a conflict shows in `task status` and can be retried, but it does not block other writes. New writes are blocked only by unresolved temporary files and unfinished tasks.
- Restore works only on free names and never overwrites a newer file.
- The journal lives in `.ai-desktop-tasks` in the task directory, contains the old and new content of files and has private permissions. The limit is 128 journals per directory, with no automatic cleanup. Do not delete it to get around a refusal, because you lose the ability to undo.
- The journal is tied to the directory: a moved or replaced directory may give a safe refusal, and history from another place is marked as unlinked and is not used for undo.

### Which commands are in the template

The `allow` list in the template is general commands that work in the sandbox on any system and do not send logs, network names or login data. They run without asking, and their output goes to the model. Deliberately not in it:

- `journalctl` with log content, `dmesg`, `who`, `last`, `id`: logs and data about users;
- `nmcli`, `bluetoothctl`: network and device names;
- `docker`: reaches the daemon on the host despite the sandbox;
- `ip`, `ss`, `ps`, `top`, `df`, `findmnt`: in the sandbox they show its view, not the system state;
- `systemctl --user`, `pactl`, `wpctl`, `glxinfo`: they have no access to the user session.

You can add them on your own system if you consciously want to. A program not installed on the system is skipped.

## Your own Skills

The packaged Skills lead from a symptom through the needed reads and interpretation of results to an established cause or a clearly stated lack of data. They take the sandbox restrictions into account: lack of access to the session or network is not proof of a host failure. A proposed fix requires a separate plan, consent and a check of the result; loading a procedure runs nothing.

A Skill is an instruction for the model of up to 96 KiB, not a permission. An agent can have any number of Skills, but together with its role up to 104 KiB: the whole instruction goes to the Claude client as one argument, and Linux limits that to 128 KiB. In `ask`, the initial catalog contains names and descriptions. The program shows `Skill NAME` when the model requests a procedure. On subsequent turns it can include a recently used procedure once its assignment, content, recipient and current byte budget have been checked. In `material`, `project` and `task` the content of the assigned Skills goes with each request. A Skill should describe how to do something, not what your computer looks like: the model reads the facts itself when needed. Files and commands are decided by `permissions.toml`. The package contains only general Skills; what concerns your computer you keep on your side:

```fish
ai-desktop skills new my-system      # creates ~/.config/ai-desktop/skills/my-system/SKILL.md
ai-desktop skills                    # list of available Skills
ai-desktop skills show my-system     # preview
ai-desktop agents                    # assign a Skill to an agent
```

Your own Skill with the name of a general one takes priority. **Do not put secrets or data about yourself in it: the description and content of a Skill may go to the model.** A path written in a Skill counts as one given by you, so the model can read it without discovery; `deny` still decides.

## Agents and models

An agent is a saved set: service, model, reasoning level, access (`changes` or `read-only`), instructions and Skills. An agent adds no permissions. A `read-only` agent does not write, does not delete and does not get `change` commands.

```fish
ai-desktop settings                                 # settings menu
ai-desktop settings models                          # available models and levels
ai-desktop settings set --model sonnet --effort high
ai-desktop agents setup                             # ready-made roles and Skills, does not change yours
ai-desktop ask --agent reviewer --model opus --effort high
ai-desktop sessions                                 # saved conversations
```

Details: [docs/AGENT_MANAGEMENT.md](docs/AGENT_MANAGEMENT.md).

## Codex (optional)

Without this section everything runs on Claude. Treat this path as experimental.

```fish
cd ~/ai-desktop
python3 -B scripts/fetch_codex_client.py     # downloads Codex 0.166.0 and checks the checksum; no login and no running
# then rebuild and install the package as in step 4 of the installation
ai-desktop codex status
ai-desktop login --codex                     # log in with a ChatGPT subscription and choose the service
```

In `permissions.toml` set `codex = true` in `[network]`: the Claude and Codex permissions are separate, and Codex is blocked by default. Codex has no web search. Disconnecting the account: `ai-desktop codex logout --allow-chatgpt-logout`.

## Updating

```fish
ai-desktop stop
ai-desktop status
cd ~/ai-desktop
git pull
cd packaging/arch
makepkg --cleanbuild
sudo pacman -U ai-desktop-VERSION-x86_64.pkg.tar.zst    # VERSION = pkgver-pkgrel from PKGBUILD
sudo systemctl daemon-reload
ai-desktop status
```

Do not update during a task. An update is not atomic: files are replaced one after another and there is no automatic package rollback. After an interrupted update install the same file again and check `pacman -Qkk ai-desktop`. The package does not change `permissions.toml`, the login profile or the journals. An unknown or damaged file format means refusal, not migration; do not fix versions or hashes by hand.

## Uninstalling

1. End the session: `ai-desktop stop`, then `ai-desktop status`.
2. Check `ai-desktop task status --workspace DIRECTORY` for the directories you used. Uninstalling does not undo saved changes.
3. `sudo pacman -R ai-desktop`, then `sudo systemctl daemon-reload`.

**What stays on disk:**

| What | Where |
|---|---|
| The `ai-desktop` system account | system |
| Claude and Codex login profiles | `/var/lib/ai-desktop/claude`, `/var/lib/ai-desktop/codex` |
| Policy, agents, your own Skills | `~/.config/ai-desktop/` |
| Saved conversations | `~/.local/state/ai-desktop/conversations/` |
| Task journals and trash | `.ai-desktop-tasks` in every directory where something was saved |
| Your project files and files changed by the program | in place |

The program has no "remove everything" command. You remove this data yourself and at your own risk; erasing `.ai-desktop-tasks` takes away undo and restore, and files from the trash disappear with it. Terminal history, system logs and data on the provider's side are not cleaned.

## Data and privacy

The provider receives: your question, the instructions of the program and the agent (with assigned Skills), the `read` and `write` patterns and command templates, names from discovery, the content and **absolute paths** of files read, command output, systemd and weather data and the conversation memory. `deny` rules are not sent. The official client adds its own technical context, so `--v` is not a record of all traffic. The full list: [SECURITY.md](SECURITY.md).

- Data that was sent cannot be withdrawn: neither stopping, nor undo, nor uninstalling removes it from the provider.
- A conversation is saved locally only after your `y` at the end or with `--session new`, in private files. An answer may quote files it read.
- The terminal may keep the scrollback, and `--question` goes to the shell history. Do not attach `--v` output to public reports.

## When something does not work

| Symptom | What to do |
|---|---|
| `The trusted Claude Code 2.1.312 client is missing` | The package was built without `claude-code` or with another version. Repeat steps 3 and 4 of the installation. |
| `doctor` says `Claude client: missing` | The package has no `claude-code` file. Add it according to step 3 of the installation and build the package again. |
| `status`: `auth=unchecked` | This is normal: `status` and `doctor` do not check login or network. |
| Request to log in | `ai-desktop login`, then a new question. |
| Service limit, network error, timeout | Nothing is retried by itself. Wait and ask the question again. The waiting time is 110 s, and for `xhigh` and `max` 15 minutes. |
| Session busy or unconfirmed stop | `ai-desktop status`, then `ai-desktop stop` (stops only your session). |
| `Invalid permissions.toml` | The message points to the key. Also check mode `0600`. |
| `Claude is blocked` | Set `claude = true` in `[network]`. |
| Read refused | Check `read` and `deny`. A link, special file, foreign owner or hard link means refusal. The message gives the limit. |
| `Nothing was saved` | Check `read` and `write` and whether the directory exists; then `task status`. |
| A write refuses after an interrupted task | Temporary files were left. `task status`, then `task rollback`. Do not erase the journal. |
| Limit of 128 tasks | The write will not be performed. No command cleans the history: review it by hand. |

## Tests

```fish
python3 -B scripts/verify.py --mode ci --fail-fast     # syntax, unit tests, no AI provider
python3 -B scripts/verify.py --mode local --fail-fast  # additionally real Bubblewrap probes on this computer
```

`--json NEW_FILE` saves stage names and exit codes, without logs and without overwriting. The `compat` and `ai` modes require explicit flags (`--allow-claude-compat`, `--allow-ai-request`), and `ai` uses your subscription. Green tests do not prove isolation on your computer.

## Documentation

- [SECURITY.md](SECURITY.md): reporting problems, trust boundaries, what is not guaranteed.
- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md): architecture and threats.
- [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md): known limitations.
- [docs/AGENT_MANAGEMENT.md](docs/AGENT_MANAGEMENT.md): agents, models, conversations, Codex.
- [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md): list for release maintainers.
- [CHANGELOG.md](CHANGELOG.md) and [LICENSE](LICENSE).
