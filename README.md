# AI Desktop

A terminal assistant for diagnostics, configuration and file work on CachyOS and Arch Linux x86_64. In a terminal, start `ai-desktop`, write a plain sentence, and the program itself collects the data your `~/.config/ai-desktop/permissions.toml` file allows and sends it to the model. Every write shows a full diff and waits for your consent.

The interface and settings are in English, and the selected model answers in the language of your question.

AI Desktop uses a compatible **independently installed Claude Code or Codex client**. Install only the provider you choose; local inference needs neither. The application package contains adapters and isolation machinery, without vendor executables. Cloud clients run under the separate `ai-desktop` account in Bubblewrap, with network access through a filtering proxy. Local inference uses an approved request to your configured loopback server.

Supported system packages are used directly. Clients installed in your home directory through the official installer or npm are registered explicitly as an administrator-approved copy; AI Desktop does not run a mutable HOME executable under its service account. Before credentials or model input are released, the selected native client and its private resources are bound to the operation and checked in an offline namespace. A compatible client update needs no AI Desktop rebuild; a registered client needs an explicit refresh to approve the new bytes. Your ordinary CLI login is not imported: connect an AI Desktop account separately. See [client installation and compatibility](docs/AGENT_INSTALL.md).

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
| `ai-desktop account list` / `ai-desktop usage` | See accounts and cached quota windows. After connecting native Codex limits in DankBar, click refresh or run `ai-desktop usage --refresh --provider codex` for a fresh reading. |
| `ai-desktop panel` / `ai-desktop status --json` | Open the interactive panel or read local metadata for a desktop widget. |
| `ai-desktop clients list` | Inspect installed sources and registrations without starting a client. |
| `ai-desktop local models` | Explicitly query the model list of your configured local inference server. |

For local inference, start your own trusted OpenAI-compatible server on `127.0.0.1`, enable `local = true` under `[network]` in your existing policy, then choose `ai-desktop agent use local`. The default port is 8080. Cloud accounts and the local service have independent permissions. There is no automatic recipient switch.

See [agent settings, accounts and sessions](docs/AGENT_MANAGEMENT.md), [verified client installation](docs/AGENT_INSTALL.md) and [Niri shortcuts and the DMS widget](docs/DESKTOP.md).

## Development status

This is a development version, not a 1.0 release.

The current candidate package is **0.1.0-84** (changes: [CHANGELOG.md](CHANGELOG.md)).

Candidate 0.1.0-82 migrates the runtime and package to independent clients, including explicitly registered HOME installations. Its exact-head CachyOS upgrade and isolated offline compatibility checks passed with Claude 2.1.319 and Codex 0.167.0; see [the acceptance record](docs/ROADMAP.md). These checks did not perform login or model inference. Candidate 83 improves the Codex usage card: connect or reconnect its limits session and read percentages and reset times directly in the widget. On CachyOS the new card renders and DMS reports it loaded. The maintainer subsequently completed native quota reconnection on the unchanged candidate 83 and confirmed the widget flow works. The earlier `busy` did not recur; its original cause is not established. PR #20 was merged as `700a609` after host acceptance and five passing CI jobs. Candidate 84 adds the bounded task approvals described below; its new acceptance is separate.

`ai-desktop doctor` and `doctor --v` inspect metadata without executing a client. `doctor --clients` explicitly probes the selected cloud client offline, without login, network or a model request. A successful probe checks adapter prerequisites; it does not certify the vendor or prove that authentication and inference work.

What is still missing and what has not been checked is described in [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). Read that file before using this on important data. The only roadmap and progress record is [docs/ROADMAP.md](docs/ROADMAP.md).

## Installation (CachyOS / Arch x86_64)

Keep the Claude/Codex installation you use: supported Arch/AUR packages, official native installers and current npm installations are covered. The example below uses the Arch Codex package. If you already installed through `curl` and a shell, omit `openai-codex` from the package command and register your existing client after installing AI Desktop. Local inference needs no cloud client. See [installation variants and migration](docs/AGENT_INSTALL.md).

```fish
# 1. Application build tools and the selected system client (Codex example)
sudo pacman -S --needed base-devel git fish python bubblewrap libseccomp sudo python-rich python-cryptography openai-codex

# 2. Code and application installation; this does not download a provider client
git clone https://github.com/ultherego/ai-desktopv2.git ~/ai-desktop
cd ~/ai-desktop
python3 -B scripts/install_agents.py codex --install
sudo systemctl daemon-reload

# 3. Select and check the client, then connect a separate AI Desktop account
ai-desktop agent use codex
ai-desktop doctor --clients
ai-desktop login --codex

# 4. Your access policy (fresh installation)
mkdir -p ~/.config/ai-desktop
cp ~/ai-desktop/config/permissions.example.toml ~/.config/ai-desktop/permissions.toml
chmod 600 ~/.config/ai-desktop/permissions.toml
$EDITOR ~/.config/ai-desktop/permissions.toml     # enable network.codex; fill in files.read; deny wins

# 5. Check and start
ai-desktop doctor
ai-desktop ask --preview      # permissions, without the model or sending data
ai-desktop ask                # conversation
```

Notes:

- **Client installation.** Choose `claude`, `codex`, `both` or `local` for `install_agents.py`; the same application can be installed before a provider. The helper never downloads, upgrades or removes a client. For a HOME/native/npm installation, use `ai-desktop clients register claude` or `ai-desktop clients register codex`, review the proposed payload and approve the administrator action. `--path /absolute/path` handles a recognized native installation outside automatic discovery. After updating that source, use `clients refresh PROVIDER`. See the [registration and update boundary](docs/AGENT_INSTALL.md).
- **Sudo.** The client session is started by a fixed helper with administrator rights, so `ask`, `material`, `project`, `task run`, `login` and `stop` may ask for your `sudo` password. The package adds no sudoers or polkit rules.
- **Requirements.** Python 3.12 or newer, systemd, working unprivileged namespaces and a regular terminal: without one the program does not ask for consent, it refuses.
- **Host acceptance.** Fresh installation, registration/refresh and upgrade through the new client paths remain release gates; automated CI is not a substitute for CachyOS testing.

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
| `[files] file_size` | `"32MiB"` | `"64MiB"` | largest locally inspected text file |
| `[files] part_size` | `"240KiB"` | `"512KiB"` | portion when analyzing a whole file |
| `[files] read_files` | 128 | 512 | files in one context |
| `[files] read_file_size` | `"2MiB"` | `"8MiB"` | bytes per file |
| `[files] read_total_size` | `"4MiB"` | `"16MiB"` | bytes of all context files |
| `[files] test_copy_files` | 2000 | 20000 | files in the local test copy |
| `[files] test_copy_file_size` | `"8MiB"` | `"32MiB"` | local copy bytes per file |
| `[files] test_copy_total_size` | `"64MiB"` | `"128MiB"` | local copy total, independent of model data |
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

`read_file_size` must not exceed `read_total_size`; the same rule applies to the two test-copy byte settings. Existing explicitly configured smaller values remain in force. The wire ceiling is 34 MiB of serialized context and 36 MiB for a complete request, including JSON escaping. These local ceilings do not guarantee that a selected model accepts that much text. Prefer content search and range reads for large files; selected excerpts carry source hashes and coverage. Up to 64 files can change in one proposal, bounded by 8 MiB per resulting file and 16 MiB combined. Anchored replacements let the model change a small part of a large file without generating its entire replacement. Test-copy-only files are never attached to the model request.

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
2. **Reads and discovery without asking.** The model lists and searches names, then can search content and read bounded line ranges in authorized files. Paths must come from discovery or your text. Source hashes and coverage distinguish excerpts from complete files. The budgets are in the table above.
3. **Commands from `allow` without asking**, up to 16 in one model step and 24 per question (`command_rounds`).
4. **Each write separately.** An edit or a new file may follow from earlier discovery. The program checks the proposal on an isolated copy, shows the full diff and asks `[y/n]`. A project suite uses the complete permitted project copy and currently asks separately before running it. A failed or refused test blocks the write.
5. **Deletion separately.** The program lists the files and asks `[y/n]`. Files are not erased, they go to the task trash: at most 8 files at a time, up to 16 MiB each.
6. **Changes from the `change` list** require typing `yes`.

After an applied change the model takes the next step by itself or finishes the answer, at most 5 automatic steps. Each further write again shows the diff and asks. A refusal or error hands the turn back to you. Type `continue` to go on after the limit.

In a normal conversation, ask to run the current project's tests or type `/test pytest`, `/test python-unittest`, `/test go-test` or `/test npm-test`. Start AI Desktop in a project already covered by a recursive `files.read` rule and use a profile that permits execution. Before each run the controller shows the current directory, a snapshot identifier and the number of indexed and excluded files. It asks before running the fixed recipe on a temporary copy. Raw test output stays in the terminal. The conversation remembers the result for that snapshot. Go and npm require a systemd user manager with delegated CPU, memory and PID controllers and run without downloading dependencies.

The check on the copy comes from a list: `python-unittest`, `pytest`, `go-test`, `npm-test`, `python-syntax`, `json`, `toml`, `bash-syntax`, `fish-syntax`, `text`. The `text` check only confirms valid UTF-8 text: **syntax and behaviour are not checked**. No check proves the project is correct.

A conversation sends up to 64 recent exchanges within a 512 KiB history budget, with up to 32 KiB per answer, and up to 96 structured task notes within 64 KiB. The in-memory window is 128 turns; saved records keep the recent 64 and bounded notes in at most 16 MiB. This is bounded working context, not an unlimited transcript or automatic semantic compaction. A long history uses up the service limit, because it goes with every question. If you revoke access to a file, the answers that depend on it disappear from memory before the next question. In interactive `ask`, a timeout or service limit returns you to the conversation without retrying the question; earlier context stays in memory. Unsaved context is still lost if the process exits.

You turn on web search with `web = true`. It is a separate request: only your question, without files or local data. Weather (`weather = true`) reveals the place name and your IP address to the Open-Meteo service, and the result goes to the model.

During a conversation you can type a command starting with `/`. After you type `/` a list appears under the line, arrows select, Tab completes. Commands work locally, send nothing and do not erase the conversation: `/help`, `/model`, `/effort`, `/skills`, `/agent`, `/settings`, `/permissions`, `/exit`. After a change of model or Skills the program asks once more for consent to send. `/skills` works in this conversation; use `ai-desktop agents` to make it permanent. A question starting with a path, e.g. `/etc/fstab what is it?`, goes to the model normally. To send a single word starting with `/`, start with a space.

### `ai-desktop material FILE`

One UTF-8 text file up to `file_size`, covered by `files.read`. You ask in a plain sentence; nothing is sent before the question.

- A file up to 200 KiB goes to the model whole.
- With a larger one the model asks for searches and line reads, at most 8 requests per question, up to 256 KiB per request and 2 MiB in total. You see each step, and the rest of the file is not sent.
- "Analyze the whole file": the program splits the file into up to 512 parts, shows the request and byte budgets, and asks once for the whole task. The consent expires after 30 minutes. `part_size` is configurable up to 512 KiB. Coverage is counted by the program, and an interrupted analysis is marked as partial. Required Skills must be loaded before the fixed map/reduce plan starts.
- The answer gives the lines the model really received. Session memory keeps up to 16 questions and bounded answers.
- No writing, no Internet and no running code. Refusals cover among others an empty file, a link, control characters and invalid UTF-8.

Local commands that send nothing: `/find TEXT`, `/show A-B`, `/log`, `/exit`. `/ask A-B QUESTION` narrows the question to those lines. The service accepts 4 session starts per minute, so the program waits between steps by itself.

### `ai-desktop project DIRECTORY`

The project editor receives the authorized conversation and observations behind the proposed edit. Type `/test pytest`, `/test python-unittest`, `/test go-test` or `/test npm-test` to run a fixed suite on an isolated copy of the authorized index. The controller requests approval for the copy. On a size refusal it reports the measured file count or byte total, the relevant policy setting and the fixed copy ceiling (20,000 files, 32 MiB per file and 128 MiB total); defaults are 2,000 files, 8 MiB per file and 64 MiB total, configured separately with `test_copy_*`. A refused copy runs no tests. The test output stays in your terminal. Go and npm need a delegated systemd user scope with verified cgroup v2 limits and the corresponding local toolchain. Recipes never install dependencies.


One directory covered by `files.read`. The scan is local and repeated before each question. Limits: 20,000 files, 100,000 entries, depth
16. Entries from `deny`, links, special files and `.git` are skipped and counted.

- **Reading.** One consent covers at most five stages: 40 requests, 10 MiB of project data and 50 minutes in total. Each stage retains its own 8-request, 2 MiB and 10-minute ceiling. File names, matches and fragments are sent. Further stages continue automatically within the same approved project snapshot, policy and recipient; a change or expiry stops the task. Ctrl+C interrupts it.
- **Changes.** This mode is not read-only: after a request for a change the model proposes files, and from there the same flow as in `ask` applies (consent to send, test on a copy, diff, separate consent to write, undo). This needs a `files.write` entry with the directory and subdirectories covering the project, e.g. `~/Projects/**`. Edits support up to 64 changed files, 5 path levels and 8 MiB per resulting file, within the owner’s lower read limits. The local test copy supports paths up to 16 levels deep independently of editable targets.
- **Deletion.** A list of files, `[y/n]`, the task trash and a printed restore command.
- Project suites can run on the approved copy. The project mode has no general-purpose command runner or web search.

Local commands: `/files [DIR]`, `/search TEXT`, `/refresh`, `/log`, `/test RECIPE`, `/exit`. Large pastes (up to 32 KiB) appear as a placeholder and go to the model as data. Enter sends, and Alt+Enter breaks the line. Two Ctrl+C within 3 seconds end the session.

### `ai-desktop task`

A change to exactly the files you name, without `permissions.toml`: the choice is yours.

```fish
ai-desktop task run "Fix the addition in calc.py" --workspace /path/to/project \
    --file calc.py --context test_calc.py --test python-unittest
```

`--file` may be changed, `--context` is read-only. The directory must be given as an absolute path and must not be the whole home directory. Limits: 512 selected input files, 64 changed files, 8 MiB per file and 16 MiB in total, instruction up to 128 KiB. Small unique anchored replacements are applied against the original bytes; missing, repeated or overlapping anchors are refused. Tests run on a copy, without network or home directory. Python tests cannot create processes, threads or sockets, so not every test suite will work here. `--preview` shows the data without the model or a test. The initial scope approval covers preparing the proposal and running the declared test once on the selected-file copy, including Go/npm. The summary shows the workspace, files, recipe and finite preparation budget. Changed files, recipient, scope or recipe invalidate the approval. The full diff and final write decision remain separate; no failed test authorizes a write.

### Task approval scope and remaining work

Candidate 84 implements one bounded approval for project analysis stages and combines standalone task preparation with its declared test. It does not turn ordinary conversation consent into permission to execute project tests. The combined project-edit/data-transfer/copy-test flow, ordinary-chat persistence and shared task continuation remain S9/S1/S2 work. All final writes and deletion/undo operations retain their separate decisions; saved conversations never restore these process-local grants.

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

A Skill is a procedure, not a permission. The shared loader starts with assigned names and descriptions in all four modes: `ask`, `material`, `project` and `task`. The model requests needed procedures and individual bundled references; unrelated bodies stay out of its request. Bundles are frozen and rechecked for changes before handoff. References include official source URLs, a checked date, version applicability and a content hash; a rolling wiki snapshot must not be described as documentation verified for an installed version. Skill knowledge has a separate bounded budget from file-discovery output. Native client tools and automatic native Skill discovery remain disabled. Files and commands are decided by `permissions.toml`. The package contains only general Skills; what concerns your computer you keep on your side:

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

## Codex through a system package

Choose Codex independently of Claude. The adapter remains experimental. Install `openai-codex` through your package manager, or register your existing native/npm installation with `ai-desktop clients register codex`, then use the existing separate account flow:

```fish
ai-desktop agent use codex
ai-desktop doctor --clients
ai-desktop codex status
ai-desktop login --codex
```

In `permissions.toml` set `codex = true` in `[network]`: the Claude and Codex permissions are separate, and Codex is blocked by default. Codex has no web search. Disconnecting the account: `ai-desktop codex logout --allow-chatgpt-logout`. Removing the native client does not prevent disconnecting an existing SIWC account or a native quota grant.

## Updating

Update the application and your Claude/Codex installations independently. A registered copy stays unchanged when its source updates; `ai-desktop clients list` exposes the source state, and `ai-desktop clients refresh PROVIDER` asks you to approve the replacement payload. Registration applies to the whole AI Desktop installation, so changing it affects every user of that provider. It takes priority over the system package until explicitly removed with `ai-desktop clients remove PROVIDER`. The adapter checks supported version ranges and required capabilities before account or model access. A newer client may pass those checks while remaining explicitly untested; an incompatible version or protocol is refused. One operation uses a fixed executable/resource snapshot, and a detected source change refuses further phases. Finish an operation before upgrading a client.

When upgrading from 0.1.0-79/80, use a supported independent client: a system package or an explicitly registered existing installation. The application can be installed before registration. The new application package no longer owns or runs its old private vendor files; pacman handles removal of files from the old package manifest. Existing account profiles, policy, sessions and journals are retained. Ignored copies left in the source checkout are not packaged or used. Do not copy credentials into a user CLI profile or delete them to complete the migration.

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
| Independently installed Claude/Codex clients | Their original system or user locations; not removed with AI Desktop |
| Administrator-approved client copies | `/var/lib/ai-desktop-clients/`; remove explicitly with `clients remove PROVIDER` before uninstalling if no longer needed |
| The `ai-desktop` system account | system |
| Claude and Codex login profiles | `/var/lib/ai-desktop/claude`, `/var/lib/ai-desktop/codex` |
| Policy, agents, your own Skills | `~/.config/ai-desktop/` |
| Saved conversations | `~/.local/state/ai-desktop/conversations/` |
| Task journals and trash | `.ai-desktop-tasks` in every directory where something was saved |
| Your project files and files changed by the program | in place |

The program has no "remove everything" command. You remove this data yourself and at your own risk; erasing `.ai-desktop-tasks` takes away undo and restore, and files from the trash disappear with it. Terminal history, system logs and data on the provider's side are not cleaned.

## Data and privacy

The provider receives: your question, the instructions of the program and the agent, assigned Skill metadata and the procedures/references actually loaded, the `read` and `write` patterns and command templates, names from discovery, the content and **absolute paths** of files read, command output, systemd and weather data and the conversation memory. `deny` rules are not sent. The official client adds its own technical context, so `--v` is not a record of all traffic. The full list: [SECURITY.md](SECURITY.md).

- Data that was sent cannot be withdrawn: neither stopping, nor undo, nor uninstalling removes it from the provider.
- A conversation is saved locally only after your `y` at the end or with `--session new`, in private files. An answer may quote files it read.
- The terminal may keep the scrollback, and `--question` goes to the shell history. Do not attach `--v` output to public reports.

## When something does not work

| Symptom | What to do |
|---|---|
| `client-missing` or `doctor` reports a missing client | Run `ai-desktop clients list`; install a provider independently or register an existing HOME/native/npm installation. Rebuilding AI Desktop does not install a client. |
| `client-unsafe` / `client-layout` | Check the source and supported native layout. HOME installations require registration. Arbitrary shell/JavaScript wrappers are not executed; point `clients register` at a recognized native payload. |
| `client-version` / `client-protocol` | Run `ai-desktop doctor --clients` and use a compatible client version. Refresh a registered copy after updating its source. Do not edit version pins or bypass the adapter check. |
| `client-changed` | Finish the update or registration refresh, then start a new operation so its selected payload is checked from the beginning. |
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
