# Changelog

## 0.1.0-85: automatic local conversation checkpoints

- Interactive `ask` creates a private local record only after the first completed turn or completed action, then atomically checkpoints every later completed turn. A declined, cancelled or failed first turn that performed no action leaves no empty conversation behind.
- `--no-save` keeps the conversation only in memory and cannot be combined with `--session`. Existing explicit session creation and resume remain available. One-shot `--question` and non-terminal embedded readers are not persisted implicitly.
- The bounded record contains recent conversation state and provenance, never OAuth credentials, reusable grants, write approvals or a native-client session. Resuming still rechecks the current policy, sources, recipient and consent. A failed checkpoint leaves no partial JSON record, reports the local-storage problem and lets the in-memory conversation continue.
- Revoking access removes the dependent content from the saved copy at once. At the limit of 128 saved conversations the program says to delete old ones with `ai-desktop sessions`. Line and paragraph separators in a message no longer stop saving, and leftovers of a killed save are cleaned up.
- This changes local persistence and requires exact-candidate CachyOS CI/LOCAL, package and installed-file acceptance before merge. Full durable transcripts, semantic compaction, streaming and the `-c` shortcut remain unfinished.

## 0.1.0-84: bounded task approvals

- Approve up to five read-only project analysis stages once, with visible request, data and time limits. Further stages keep the same project snapshot, policy and recipient; changes or expiry stop the task. Ctrl+C interrupts it.
- Standalone `task run` declares the exact selected files and test before the first approval, then prepares the proposal and runs that test once without another Go/npm test prompt. The full diff and separate write approval remain mandatory.
- Grants stay in the current process, expire, cannot be reused for another scope or recipe, and are never restored from saved history. Ordinary `ask` and project-edit copy tests retain their existing approvals; the wider S9 flow remains unfinished.
- This candidate changes consent boundaries and requires new CachyOS acceptance before merge. Client registration, OAuth and the accepted Codex widget are unchanged.

## 0.1.0-83: Codex limits inside the widget

- Replace the Codex card's browser fallback with the next applicable account action: check sign-in, connect or reconnect limits, or refresh the existing connection. Show the provider's actual percentages and local reset times in the card; missing readings stay explicitly unknown.
- Refresh limits directly from the card, including an already connected account with no reading yet. Prevent concurrent refreshes and show progress. Opening the widget and its timer continue to read saved local state only.
- Preserve the existing separately approved native quota session and account binding. The candidate-82 client runtime passed CachyOS offline acceptance with Claude 2.1.319 and Codex 0.167.0; this widget follow-up does not change client execution, registration or OAuth protocols.

## 0.1.0-82: native compatibility and standalone package limits

- Separate Claude print-mode option checks from the signed-out account-status check. Exercise the complete production options with explicit unknown-option controls, including first-error ordering, and reject incidental error text or JSONL as parser proof. Preserve the positive offline status contract and strict runtime reply validation.
- Raise the shared native payload total from 512 MiB to 768 MiB: the official Codex 0.167.0 standalone bundle exceeds the old cap. Keep 384 MiB per file and all traversal/ownership checks. Analysis and quota services allow 3 GiB for the retained payload, its read-only namespace copy and native-client memory.
- Keep existing approved Claude registrations and separate account data. A previously refused Codex source can be registered after this application update. New exact-candidate CachyOS acceptance remains required before merge.

## 0.1.0-81: independent clients and explicit registration

- Use independently installed Claude Code or Codex through the existing isolated adapters. Supported system packages work directly; official native/HOME/npm installations use explicit administrator-approved registration and refresh of their native payload. Freeze the selected native executable and client-private resources for one operation, check compatibility offline before account/model access, and refuse changed installations or unsupported protocols.
- Inspect sources with `clients list`; register, refresh or remove an approved runtime copy explicitly. A source update never silently replaces its registered bytes. Remove vendor-client downloading and embedding from application installation. Claude, Codex, both or local-only setups are independent; upgrades retain the existing separate account data, and uninstalling AI Desktop leaves independent clients and separately retained registrations in place.
- Keep ordinary `doctor` and panel status metadata-only. Add explicit `doctor --clients` for the selected provider's offline compatibility check, with a distinction between the tested baseline and newer compatible but untested versions.
- The migrated runtime requires new CachyOS acceptance; acceptance of package 79 is not evidence for these changed boundaries.

## 0.1.0-80: system-client discovery and a single delivery roadmap

- Add read-only metadata inspection of system Claude Code and Codex to `doctor`, with separate missing, unsafe, unsupported-layout and changed-file outcomes. Inspection does not execute a client, access credentials or certify vendor origin, version or protocol compatibility.
- Record the chosen migration to independently installed system clients, with one shared detection layer, provider-specific compatibility and unchanged separate authentication. There is no planned second bundled/pinned mode. The current packaged-client runtime remains active until the complete migration and host acceptance pass.
- Reconcile README, installation, management, security, limitations and release documentation. Consolidate all work tracking into ROADMAP.md; preserve the accepted candidate-79 results and the outstanding core criteria.

## 0.1.0-79: large-project workflow and shared knowledge

- Separate larger file/context budgets from local test-copy budgets. Inspect large text by search and line ranges in ordinary conversation, and use checked anchored replacements for larger files. Preserve the complete diff, copy test, apply consent and rollback checks.
- Keep a larger bounded conversation window and task memory, with a separate larger saved-record budget. The continuation shortcut and full transcript/compaction work remain follow-up items.
- Load assigned Skills and their needed reference sections on demand in ask, material, project and task. Extend practical Arch/CachyOS procedures using official sources and explicit version applicability.
- Merged in PR #18 after five passing CI jobs and maintainer acceptance on CachyOS; unfinished follow-up criteria remain tracked in ROADMAP.md.

## 0.1.0-78: quota card layout and service selection

- Give the account list more vertical space when the screen allows it. Codex usage and connection buttons wrap inside the account card. The extra heading above the Codex card was removed.
- Click a usage card or focus it and press Enter to choose that service for new conversations. Its buttons keep their own actions. Claude and Codex both expose **Manage accounts…** in the accounts tab, including before sign-in.

## 0.1.0-77: live Codex limits in DankBar

- Connect a separate native Codex account from the widget. The upper-right refresh button and `ai-desktop usage --refresh --provider codex` fetch its current quota windows and reset times without sending a model request.
- The native session is isolated per desktop UID and slot. Connecting is opt-in, opening the widget reads local status, and a terminal action can disconnect the native grant.

## 0.1.0-76: account actions and status in DankBar

- Select Claude or Codex by clicking its card in **Accounts & login**. The refresh icon on both tabs reloads local status and saved quota readings.
- A successful login records the account for the widget. Connected accounts offer sign-out. Accounts from earlier installations can be checked locally. Terminal sign-in results identify the provider and account and use color when supported.
- Preserve the timestamp of each reported quota window across partial updates. When Codex has no readings, open ChatGPT usage settings from the widget.

## 0.1.0-75: open the widget on released DMS 2.x versions

- Use the button colors supported by DMS 2.0 and 2.1 to mark the selected tab. Opening the usage and account tabs works with the released button API.

## 0.1.0-74: load the quota card after a running DMS upgrade

- Move the quota card and its status script into a dedicated directory. When DMS reloads the widget after an upgrade, Qt can discover both files and show the current percentages and reset times.

## 0.1.0-73: graphical subscription usage and login

- The DMS bar can show the current five-hour percentage. The popout shows proportional usage bars with local resets for each reported window.
- Its **Accounts & login** tab can switch the service or account for new tasks and launch the native Claude/Codex sign-in picker. Authentication stays in the approved client flow.

## 0.1.0-72: subscription display and project test feedback

- The terminal panel and `usage` show separate five-hour and seven-day windows, utilization bars and local reset times. Current usage is colored by percentage. Expired readings are labelled with their last recorded value.
- The interactive panel keeps subscription details on a separate page. Returning to the menu restores the dashboard in place; leaving the panel restores the shell.
- An oversized project test reports which copy limits the authorized index exceeded before any tests run.

## 0.1.0-71: shared agent sessions and project checks

- Start a conversation with `ai-desktop`. Choose Claude, Codex or an explicitly allowed loopback model and review what will travel when changing provider in the same conversation.
- List named subscription accounts, inspect their last reported limits and open the offline agent panel or optional Niri/DMS launchers.
- Bind history filtering, Skill selection and delivery to current permissions. Saved answers with unverifiable sources are withheld on resume.
- Preserve agreed context for project edits, show which validation ran and offer fixed test recipes on an isolated copy.
- Require fixed nested command verbs, and verify downloaded clients before an optional private Arch package build.


- `permissions.toml`: the template `systemctl -H host <word>` is rejected, because `<word>` could become a command. After an option outside the list of options without a value, the next word is treated as its value. The `change` list can have 128 templates also in the request to the model (it was 24).
- Rebuilt procedures of 21 Skills: interpreting results, conditions for further steps, recognizing access restrictions and verifying changes. Removed overly broad recipes, among others a full balance as default cleanup and `systemctl revert` as the undo of a single edit.
- Files with a BOM and Windows line endings, e.g. CSV from Excel, can be read and edited. The model and the diff see plain text, and a write keeps the file format. Undo restores exactly the same bytes. Mixed line endings: reading works, editing is rejected.
- `ai-desktop ask`: the result of a performed change stays in memory when the model's next answer does not arrive, so the model will not propose it again. After access is revoked, new independent answers stay, and details of actions dependent on that file disappear. A conversation with many files always saves.
- `ai-desktop ask`: the final answer gets command output and loaded Skills, not only files read. "We agreed on the change, do it" works: the edit stage gets the conversation, task memory, command output and Skills.
- `task rollback` does not overwrite or delete a newer editor save: it leaves it and reports a conflict. Earlier it could report success after losing that version.
- A change request can be as long as a message in `ask` (128 KiB; it was 2 KiB), also in `task run` and `project`.
- A space before `/` sends the text as a question. A full trash refuses before deleting, and the recovery list still works.
- `ai-desktop ask`: consent for a conversation covers the whole `permissions.toml`. Removing a `deny` rule, changing a limit or `share_processes` requires new consent. Earlier the old consent stayed valid.
- Installation: `PKGBUILD` checks the SHA-256 of the Claude client and refuses to build with a wrong file. The README block does not run the file with a wrong checksum.
- New `arch-*` Skills based on the Arch Wiki: pacman, systemd, Btrfs, amdgpu, audio, network, Bluetooth, zram, Docker.
- `ai-desktop ask`: Skills loaded on demand. Only names and descriptions go with each question; the content goes when the model asks for it, and the program shows that. CachyOS Skills now start with the procedure.
- Reasoning `xhigh` and `max`: the model can answer for up to 15 minutes (it was 260 s). When the time runs out, the message talks about time, not about an expired consent.
- A paste that is too large is rejected whole. Earlier its remainder could end up in the next line.
- `ai-desktop ask`: a message up to 128 KiB including pastes (it was 2 KiB), memory of the last 32 exchanges (it was 8), an answer in history up to 8 KiB (it was 3 KiB). A paste that is too large no longer ends the conversation. `material` and `project` keep their limits.
- `ai-desktop ask`: `/` commands during a conversation, with a list under the line (`/model`, `/effort`, `/skills`, `/agent`, `/settings`, `/permissions`, `/help`, `/exit`). The menu starts on the current value, and long lists have pages. A change does not erase the conversation.
- A saved conversation is tied to the service, agent and access level. A change of model, reasoning or Skill content no longer cuts it off from resuming.
- New Skills for CachyOS, each about one thing: packages, kernels, sched-ext, boot and snapshots, hardware, games and extended diagnostics. Based on the CachyOS wiki. The `desktop-documentation` Skill points to the desktop documentation instead of guessing.
- Skills: no limit on the number per agent, up to 96 KiB per Skill and 104 KiB in total. The upper limit follows from the argument length restriction in Linux.
- **Security:** when consent expires or `permissions.toml` changes during a command from `[commands] change`, the result of the performed change is shown and recorded, and the program says plainly that the change was performed and gives the test and undo commands. Further steps are not run and nothing more is sent. Earlier the result was lost without a trace.
- `ai-desktop task`/`ask`/`project`: saving a change to an existing file no longer overwrites a save made at the same time by an editor. The file is exchanged atomically (`renameat2` with `RENAME_EXCHANGE`), and the exchanged version is compared with what the plan saw. On a difference the exchange is reverted and the write rejected, and the editor's version stays.
- **Security:** `material` and `project` check `permissions.toml` before each hand-over of data to Claude. A file change, turning off `network.claude` or revoking access to files being read stops further requests, also in whole-file analysis. Earlier it was enough that the consent and files were loaded at the start of the session.
- Polish diacritics in messages and help: the save and undo prompts, `task`, `analyze`, `status` and command descriptions.
- `ai-desktop task`: when `--workspace` points to a file, the program says so plainly instead of a general refusal.
- `ai-desktop task status` and `task rollback` list entries from the newest, with the date of the last change (journal file time). Number 1 is the newest task.

## 0.1.0-70: fixes

- `ai-desktop sessions`: a damaged saved conversation does not hide the others; `sessions list` points to it and `sessions remove` deletes it.
- `/log` in `project` also shows files handed to Claude during editing.

## 0.1.0-69: first public release (development version)

- `ai-desktop ask`: one consent per conversation, reads and commands from `permissions.toml` without extra questions, writing and deleting after a full diff or list of files and a separate consent, undo of changes.
- `ai-desktop material` for large files and `ai-desktop project` for project directories.
- The official Claude Code client in a separate system account and sandbox; optionally Codex.
- General Skills in the package, your own in `~/.config/ai-desktop/skills/`.
- The `permissions.toml` template contains only general commands that work in the sandbox and do not send logs or network names.
