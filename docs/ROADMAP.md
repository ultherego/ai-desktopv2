# Plan for version 1.0

**Goal:** you start the program, talk in plain language, diagnose the system and ask for changes, and every change waits for your consent and can be undone. The flow that must work end to end:

> analyze → let's agree on the change → do it → test it → undo

This page is the authoritative roadmap and progress record, including the agreed follow-up scope below. [TODO.md](TODO.md) is a legacy task inventory, not a separate roadmap. Current limitations are in [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md), and release checks are in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).

## How completion counts

A stage is complete when its conditions are met for a specific commit. The result of another commit attests nothing. Green CI is not enough for changes to isolation, permissions, the file system, network and login: these also require a test on real CachyOS or Arch, with the commit number recorded.

Existing stage tables use **done**, **in progress** and **to do**. Follow-up entries use **TODO**, **IN PROGRESS**, **READY FOR TEST**, **BLOCKED**, **DONE** or **DEFERRED**. A planned acceptance target is not evidence that a feature exists; **DONE** requires implementation, the applicable checks and merge.

## Current state

Working and merged:

- `ask` conversation with one consent, a context window of up to 32 exchanges and task memory, `/` commands, messages up to 128 KiB. The window is not a 32-turn conversation limit.
- Directory discovery, file reads and commands according to `permissions.toml`.
- Writing after showing the diff, with a test on a copy, separate consent and a journal. A concurrent editor save is not overwritten on write.
- Skills on demand in `ask`, procedures for CachyOS and Arch.
- Client on a separate account, in Bubblewrap, with egress through a filtering proxy.
- Protection of the `main` branch, five required CI jobs, pinned actions.
- The interface, settings, docs and Skills are in English, and Claude answers in the language of the question.

## Stage 1: no data loss

| Task | Status |
|---|---|
| Undoing a change does not overwrite or delete a newer editor save, both for an edit and for a new file | done |
| SHA-256 sum of the client checked when the package is built | done |
| Consent for a conversation covers the whole `permissions.toml` | done |
| A long change instruction is not refused at the edit stage (`ask` accepts 128 KiB, the edit stage 2 KiB) | done |
| A word starting with `/` can be sent as a question, as the program's message says | done |
| The 129th deletion to the trash is refused before it runs | done |
| Editing a file with BOM and Windows line endings keeps its format, and undo restores the same bytes | done |

**Complete when:** tests reproduce a concurrent save while undoing both kinds of change, and the editor's version stays in place.

## Stage 2: task continuity

| Task | Status |
|---|---|
| The final answer after a file read gets the command results and loaded Skills | done |
| The edit stage gets what was settled in the conversation, the task memory, discovery results and Skills | done |
| The same in `project` | in progress (PR #18, host acceptance pending) |
| A Skill loaded in a conversation is available in later questions without an extra model call | in progress (PR #18, host acceptance pending) |
| Skills on demand also in `material`, `project` and `task` | to do — follow-up S4 |
| A Skill also works without `permissions.toml` when the question is general | in progress (PR #18, host acceptance pending) |
| Procedures of the 21 Skills: reading interpretation, conditions for use, ending a diagnosis and verifying a change | in progress |

**Complete when:** for "let's settle on the CSV separator, do it" the edit request contains the settled value, as confirmed by a test on the real request generator.

## Stage 3: memory and revoking access

| Task | Status |
|---|---|
| The result of a completed change stays in memory when the next request to the model fails | done |
| An independent answer is not removed just because an earlier turn depended on a revoked file | done |
| The content of a revoked file does not survive in the controller's facts, for example in a command argument | done |
| A conversation with many dependent files can be saved | done |

**Complete when:** after a service error the model does not propose a completed change again, and after access is revoked later requests contain no data from that file.

## Stage 4: isolation and policy

| Task | Status |
|---|---|
| `allow` commands without access to the host's Unix sockets (D-Bus, Docker) | to do |
| General system call filter for the client | to do |
| Matching `change` template limits in the policy and the contract | done |
| Checking the template parser for option arguments | done |
| Commands with real Bubblewrap also in CI | to do |

**Decisions to make:** the client has access to its own login profile. The current architecture trusts the official client on this point. Before 1.0 this must be consciously accepted or changed.

## Stage 5: change and test

| Task | Status |
|---|---|
| Project tests (for example `pytest`, `go test`, `npm test`) on an isolated copy, not only seven fixed checks | in progress (PR #18, host acceptance pending) |
| A clear message about what a given check confirms and what it does not | in progress (PR #18, host acceptance pending) |
| A longer time limit for `high`, if long messages exceed it | to check |

## Stage 6: cleanup before release

| Task | Status |
|---|---|
| A new `pkgrel` number with every change, so the package version tells releases apart | to do |
| Working acceptance scripts or their removal (`update_candidate.fish` requires an old version) | to do |
| Removal of modules and commands not used by the installed program | to do |
| Merging the duplicated `material` and `project` flows | to do |
| `task prune` with a list of entries and consent | to do |
| One client session per conversation, streaming answers | to do — follow-up S2; lifecycle and revocation checks required |

## Agreed follow-up scope — 2026-11-06

This section records the requested work after the installed DankBar layout check. It extends the existing stages; it does not close them or create a second release plan. The implementation baseline is package `0.1.0-78`, commit `0cc4be81e16f02b33eb6760dd2fa58de7d6f74fb`; the inspected PR head `55233872e1ef916efa8b6c2576f8c862040e1c1d` adds only the acceptance record. The S3–S5 implementation is now in progress; the delivery order below follows the updated core-work priority. Core work stays within E3–E7; optional desktop additions are deferred until those criteria are stable.

Release boundary: S3–S5 are the immediate implementation queue; the remaining S0–S6 items are recorded follow-up work, not an automatic expansion of every v1.0 release gate. The existing conditions below remain authoritative; relevant behavior changed by an item must meet those conditions before shipping. S7 is explicitly outside the current MVP. Recording a requested item neither promises a release date nor requires completing optional work before a stable core release.

| Order / ID | Epic and priority | Deliverable | Status | Depends on |
|---|---|---|---|---|
| 1 / S3 | E3–E6 / P0 | Large-file analysis, many files, anchored edits and independent test-copy budgets | IN PROGRESS | Existing read/plan/executor; bounded context work from S2 |
| 2 / S4 | E3, E6 / P0 | Shared on-demand Skills and documentation loader in every mode | IN PROGRESS | Existing role, source and recipient authorization |
| 3 / S5 | E3 / P0 | General Arch/CachyOS procedures and version-aware references | IN PROGRESS | S4 and official documentation |
| Later / S2 | E3, E6 / P0 | Durable transcript, compaction and streaming beyond the bounded context improvements | TODO | Stable core task flow |
| Later / S1 | E7, E3 / P1 | `ai-desktop -c` and ordinary-chat autosaving | DEFERRED | Stable analyze → edit → test → undo flow |
| Later / S0 | E7 / P1 | One universal package with managed environment-specific Skills | DEFERRED | Stable core and shared loader |
| Later / S6 | E4–E7 / P1 | Six CachyOS Hello integrations with explicit state and controls | TODO | S5, typed host operations and host acceptance |
| Later / S7 | E7, R4 / P2 | Project launch actions, crash entry point, OCR/dictation and terminal layouts | DEFERRED | Stable core and environment adapters |

### S0 — universal package and environment-specific Skills

The requested delivery is **one general AI Desktop package**, not a separate optional DMS package. General Linux/Arch/CachyOS knowledge belongs to the core. Desktop and shell Skills are managed packs selected for the environments actually installed on the target system. KDE-only must not fetch DMS knowledge or dependencies. Detect desktop/compositor and shell separately: DMS is a shell which may accompany Niri or another compatible compositor, not an alternative name for the desktop. Core conversation works in a terminal without a graphical session.

Use installed package ownership and available Wayland/X11 session descriptors as evidence for all installed environments; active-session variables only identify which one is currently in use. Never infer that the inactive desktops were removed. Resolve per-user shells/configuration for the intended desktop user; an installer invoked through `su` must not use the invoking terminal's session as proof. The detector returns a normalized installed set and active session separately, with explicit unknown/ambiguous state.

Persist the last successfully reconciled environment-set fingerprint, owned pack versions and hashes. Only a real change of the installed set initiates automatic pack synchronization. Repeated starts and a normal AI Desktop upgrade with the same set must not re-download packs. On KDE → GNOME replacement, install/verify the GNOME pack, then retire only the managed KDE pack; keep general knowledge and user-created/customized Skills. With two or three installed environments keep the union of their packs, activating the relevant metadata for each task/session. Reuse verified cached identical packs. A changed active login alone must not cause download churn. Updating pack content without a changed environment set is an explicit refresh action, not a hidden repeated download.

Use a trusted versioned pack manifest, content integrity checks and an atomic managed-set update. A failed/offline fetch retains the last verified set and reports that reconciliation is pending, without claiming the new environment is covered. Do not issue nested pacman transactions from a package hook. Package installation can mark reconciliation as needed; the bounded per-user manager performs it under the established download policy. Dependency packages unrelated to the installed environment must not be pulled in. Do not reset credentials or overwrite user configuration.

Acceptance: KDE alone; GNOME alone; Niri with/without DMS; two/three simultaneous desktops; adding/removing a desktop; KDE → GNOME replacement; switching the active login without changing installed environments; ordinary upgrade and repeated launches causing zero re-downloads; offline/corrupt cache; interrupted reconciliation; user overrides; the intended user when installed through `su`. Verify the requested set, download counts and retained/retired managed files. This design is recorded now; automatic environment-pack reconciliation remains deferred until the core task flow is accepted.

### S1 — direct continuation and saving

Add `ai-desktop -c` (`--continue`) to open the last-used saved conversation of the current user immediately, with no picker or session ID. Preserve its kind (`ask`, `material` or `project`), workspace/material, provider, exact account or local endpoint, role, model and access mode. Display a compact identity/title line. Do not change the global default service or silently transfer history to another recipient. Use explicit last-used ordering; resuming without a new question must count as use.

Save during ordinary conversations, atomically after completed turns and completed actions, including when the next model response fails. Provide an explicit non-saving mode and a clear local-storage setting. A new session must survive terminal failure up to its last durable checkpoint; do not rely on a save question at exit. Saved content contains no OAuth credentials, reusable write approvals or restored grants. Current policy and source permissions are checked again on resume; entering the conversation is local, and data transfer retains the ordinary session consent.

Acceptance: continue the newest thread across restarts; changed global provider/account settings; named accounts and local endpoints; missing/retired account; removed workspace; no saved thread; corrupt/incompatible record; active thread in another process; interruption during save; full storage. Empty storage must give a clear message and a fresh prompt, never pretend to restore a thread. A corrupt or busy latest thread must not silently select an older one. Existing explicit session commands stay compatible.

### S2 — durable history and bounded model context

Separate the local transcript, task state and the context assembled for one model request. Preserve the transcript independently of the recent-message window. Keep goals, accepted decisions, rejected proposals, completed changes, test results, source identities and the next task in structured state. Compact old context into a provenance-aware summary when the model budget approaches its limit; retrieve relevant earlier records when needed. Add a context/compaction view so the user can inspect what was summarized or omitted. Persistence and retention limits must be configurable and visible; never discard older history silently to fit a 1 MiB record.

The pre-S3 baseline retains at most 64 turns in memory, sends up to 32 pairs with an approximately 128 KiB history budget, shortens answer history to about 8 KiB and keeps only 40 short memory entries. Material/project history is narrower still. Replace these unrelated truncation paths with a shared manager; also remove or explicitly handle the current turn-number ceiling of 9999. Native-client session reuse and streaming must stay behind the existing broker. Summaries cannot restore revoked data or privileges. On recipient/policy changes, rebuild the permitted context; if a native session retains revoked content, discard that native context before any further request.

Acceptance: a synthetic conversation of at least 200 turns survives exit and `-c`, preserves an early corrected decision, distinguishes a rejected proposal from an applied change and does not repeat an already completed action after an error. Test source revocation after compaction, account/provider changes, corrupted summaries, token/byte budget exhaustion, multibyte input, cancelled streaming and timeout cleanup. The transcript retains the original evidence even when its model-facing summary is lossy. Do not equate a surviving Tmux process with durable conversation storage.

### S3 — large files and projects through ordinary conversation

Expose indexed discovery, content search and range reads from the ordinary conversation, reusing the existing project/material machinery. The user should not need to choose a different command merely because a text file is larger. Maintain a reusable index of permitted paths and hashes; invalidate changed/deleted files and recheck authorization before reading. Denied paths must not leak through indexes, counts or search output. Treat binary or unsupported files explicitly.

Separate budgets for local file size, scanned bytes, excerpts sent to the model, local test copies, changed files and patch/rollback data. The pre-S3 `ask` defaults (64 files, 64 KiB per file, 128 KiB combined), its ceilings (128 files, 128 KiB per file, 256 KiB combined), material import (5 MiB default, 16 MiB maximum), project index (20,000 files), and the much smaller edit/test-copy ceilings must not contradict one another. The pre-S3 whole-material path allows 24 parts: accepting a 16 MiB import does not prove that all of it fits 24 parts of at most 512 KiB. Fix planning, coverage and reporting together.

Initial acceptance targets, not current capabilities: analyze a 32 MiB text file through bounded search/range reads; work across a permitted 20,000-file index; edit and undo a 2 MiB text file; test a realistic multi-file project using a separate configurable copy budget larger than the model payload. Set copy/edit ceilings from measured resource use and enforce them consistently in the controller, worker, transport and executor. Whole-file analysis must report actual coverage and use staged synthesis where necessary. An answer based on selected excerpts must not claim the entire file was examined.

For writes, use precise patches against verified source bytes, preserve encoding/newlines and existing race/symlink protections, and show the complete reviewable change before approval. Acceptance covers multiple affected files, large UTF-8 lines, interrupted scans, index invalidation, excluded generated trees, each budget boundary, resource limits, failed project tests, partial-operation recovery and a concurrent editor change during apply/undo. Larger limits alone must not bypass `deny`, the plan hash or rollback checks.

### S4 — one Skills loader and documentation in the same task

Use one catalog and loader in `ask`, `material`, `project` and `task`: metadata first, the matching short procedure on demand, then only required sections of `references/`. The present `ask` loader already selects Skills, checks assignments/hashes and reuses recent procedures; the other modes still inject all assigned bodies. Preserve the useful checks while removing that inconsistency. Keep general system procedures separate from repository-development instructions and local machine facts. Skills do not grant tools, run embedded commands, install scripts or widen permissions.

Support versioned source metadata: official URL and section, verification date, applicable tool version or documentation commit, and content hash. Reuse validated procedures and references across turns and resume; re-read or invalidate them when contents, assignments, recipient or policy change. Make available/assigned/loaded state and reasons for non-selection inspectable. Ensure the default setup exposes relevant general diagnostic metadata without requiring unexplained manual setup, while respecting customized roles. Allocate separate predictable budgets for procedure text, references and diagnostic results.

Add a bounded public-documentation read that returns evidence to the current task: detect the installed version, retrieve the appropriate documentation, compare it with permitted local facts and continue the diagnosis. Today's separate final `web` answer cannot complete that flow. Queries must contain only approved public technical terms, not private paths, configuration or log contents. Check destination/redirect policy, response size and provenance; remote documents are untrusted data and cannot change authority. Offline or stale sources must be identified without claiming live verification.

Acceptance: identical selective loading in all four modes; a task using more than four Skills, saved and resumed; source/hash changes and removed assignments; unavailable documentation and unsupported versions; malicious remote instructions and oversized responses; revoked local data after a document lookup; unrelated procedures absent from requests. Exercise the actual request generator, not only Markdown structure. One packaged catalog serves both Claude and Codex through the controlled adapters, without enabling their unrestricted native Skill discovery.

### S5 — general Arch/CachyOS knowledge and tools

Extend the existing procedures instead of duplicating them. Detect `os-release`, installed packages, executable versions and actual session capabilities. A CachyOS kernel or repository alone does not identify the whole installation. Common Linux/Arch guidance is the foundation; CachyOS and desktop-specific references load only when applicable. Public Skills contain no particular person's paths, hardware profile or account data.

| Existing subject area | Coverage to add or complete |
|---|---|
| Packages and repositories | CachyOS Hello/Welcome, Package Installer, `cachyos-rate-mirrors`, Cachy-Update, full-update rules, signatures, optimized repositories, pacnew/pacsave and mirror freshness |
| Kernel, scheduling and memory | Kernel Manager, SCX Manager, `scxctl`, `scx_loader`, `kerver`, `topmem`, `cachyos-settings`, CPU/I/O schedulers, pressure and provider defaults versus local overrides |
| Hardware and graphics | Existing `chwd` support, versioned profiles, AMD/Intel/NVIDIA and multiple GPUs; distinguish listing from driver installation |
| Gaming | `cachyos-gaming-meta`, gaming applications, Proton-CachyOS, Wine-CachyOS, `game-performance`, MangoHud/Gamescope, per-game logs and measured changes; verify GameMode/ananicy interactions |
| Storage and recovery | Btrfs Assistant, Snapper, `snap-pac`, supported GRUB/Limine/systemd-boot integrations and `cachy-chroot` only in the appropriate recovery environment; snapshots are not backups |
| Desktop session | Detect compositor and shell independently, including Niri, Hyprland, KDE, DMS and Noctalia; validate which user's session IPC describes |
| Diagnostics and reports | Minimal logs matched to the symptom; documented behavior of `cachyos-bugreport.sh` and `paste-cachyos`; distinguish local collection, privileged writes and uploading |
| Hello services and tweaks | All six integrations in S6, with their effects, current state, prerequisites, conflicts, verification and recovery |

Provide a machine-readable catalog of installed, supported operations with versions and read/change classification. Compare documented syntax with the installed tool's help and behavior instead of inventing flags. A missing executable or inaccessible host view is a capability gap, not proof the host service is broken. Official documentation, packaged manuals and upstream code are evidence; stale or conflicting examples must be reconciled against the applicable version. Each procedure includes inputs, interpretation, stopping conditions, a separately approved change if supported, verification of the original symptom and recovery limits.

### S6 — CachyOS Hello service controls

Make these integrations usable through ordinary conversation and the existing management interface, with no DMS dependency. Initial mapping is verified against [CachyOS Welcome's tweak definitions](https://github.com/CachyOS/CachyOS-Welcome/blob/efd467041a9accb461bc3bf8a096a6627f795293/src/tweak.rs); the implementation must detect the installed version rather than assume every system has the same packages or units.

| Feature | Expected integration | Required behavior |
|---|---|---|
| profile-sync-daemon | Package `profile-sync-daemon`; user `psd.service` and dependent `psd-resync.timer` | Explain and inspect browser-profile synchronization to RAM, status and configuration; controlled on/off and autostart for the intended user. Check browser state, available RAM and recovery prerequisites before first activation. Follow upstream start/stop and sync requirements, await successful unsync/recovery on stop, and never delete profiles or treat browser credential contents as diagnostic input. Test with disposable synthetic profiles. |
| ananicy-cpp | Package `ananicy-cpp`; system `ananicy-cpp.service` | Explain rule-driven process priorities, inspect active rules/service and competing managers, support controlled on/off and autostart, then verify the intended effect. Stopping the daemon is not evidence that priorities already applied to running processes were restored. |
| Cachy-Update | Package `cachy-update`; user `arch-update.timer` and `arch-update.service`, plus tray/autostart | Expose on/off for scheduled checks and the tray separately, with a clear combined option if wanted. Verify the intended user's timer, any check already in progress and graphical autostart. Enabling notifications must not install updates, remove packages or clean caches. |
| systemd-oomd | System `systemd-oomd.service`; inspect the installed systemd implementation and activation paths | Inspect cgroup v2/PSI support, memory accounting, swap, ManagedOOM policy and monitored cgroups; explain on/off and verify active, enabled and effective state, including socket activation where present. Distinguish this service from kernel OOM handling and other OOM managers. Killing a process cannot be undone by re-enabling a service. |
| bpftune | System `bpftune.service`; Welcome currently maps installation to `bpftune-git` | Detect the actual provider/package and kernel/BPF requirements, inspect tuners and changed parameters, support controlled on/off and autostart. Record a baseline and verify what stopping restores; do not promise complete rollback of runtime tuning without evidence. |
| Bluetooth | Package `bluez`; system `bluetooth.service`; adapter/rfkill state separately | Support requested radio on/off, service on/off and autostart with clear scope. Detect soft/hard blocks and multiple adapters; verify the actual adapter state and connected-device impact. Switching off must not unpair devices or remove configuration. |

Shared acceptance: show installed/missing, active/inactive/failed, enabled/disabled/masked, unavailable/unknown and effective state separately where applicable. Explain each feature before proposing a change. An off operation does not uninstall its package. Installing a missing package is a separate approved action. User operations target the correct user's manager, never `--global` or every user. Runtime start/stop and persistent enable/disable must be explicit in the plan; inherited defaults and activation paths must be accounted for.

Feature references: [profile-sync-daemon](https://github.com/graysky2/profile-sync-daemon) and its [user service](https://github.com/graysky2/profile-sync-daemon/blob/master/init/psd.service), [ananicy-cpp](https://github.com/CachyOS/ananicy-cpp), [Cachy-Update](https://github.com/CachyOS/cachy-update) and [CachyOS post-install instructions](https://wiki.cachyos.org/configuration/post_install_setup/), [systemd-oomd](https://www.freedesktop.org/software/systemd/man/latest/systemd-oomd.html), [bpftune](https://github.com/oracle/bpftune/blob/main/docs/bpftune.rst) and its [service](https://github.com/oracle/bpftune/blob/main/src/bpftune.service), and [BlueZ adapter API](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Adapter.rst). Check installed-version behavior before implementing a control. In particular, stopping bpftune is not equivalent to using its explicit rollback mode.

Execution uses typed, allowlisted operations with the existing plan/diff, state preconditions, hash-bound approval, audit and verification. Capture the previous state and restore only the authorized change when recovery is valid. Refusal, failed elevation, missing polkit/user session, unavailable units, masked services, concurrent state changes and timeout must never be reported as success. No arbitrary model shell, automatic tuning, raw browser-profile reads or automatic report uploads. Host-control implementation requires synthetic success/refusal/failure tests and actual CachyOS acceptance before merge. Adding this plan does not authorize running any of these operations on a user's computer.

### S7 — optional Omarchy-inspired entry points

Keep these recorded as **DEFERRED**, rather than expanding the release gate before the core work is complete:

- Open a file or folder in AI Desktop with its selected workspace and initial task; optional file-manager and Niri shortcuts preserve the normal policy and review flow.
- Diagnose a crashed application from a clicked notification or explicit request, using process identity, time and bounded journal/coredump metadata. A watcher is opt-in; raw core memory is not sent automatically.
- OCR of a deliberately selected screenshot and optional dictation, with a preview before transfer, declared local/cloud processing and optional dependencies.
- Optional Kitty/Tmux layouts with editor, conversation and diff. A persistent terminal process remains distinct from saved history and does not authorize unattended actions.
- Optional component installation and one clear owner of client updates. Keep tested client versions and compatibility checks; do not import Omarchy's broad auto-approval modes or add many providers as part of this scope. Local-model lifecycle/catalog expansion remains R4.

### Source and verification policy for this scope

Reference baseline checked on 2026-11-06: [Omarchy AI](https://omarchy.org/manual/ai/), [Omarchy task guides](https://github.com/omacom/omarchy/blob/d5b32d24d37af8f0376a67d98531bb2da629824b/default/agents/skills/omarchy/SKILL.md), [terminal layouts](https://omarchy.org/manual/terminal/), [OCR and dictation](https://omarchy.org/manual/text-extraction-dictation/), [Claude continuation](https://code.claude.com/docs/en/cli-reference), [context compaction](https://code.claude.com/docs/en/how-claude-code-works), and [Agent Skills specification](https://agentskills.io/specification). Omarchy delegates long-conversation behavior to native clients; its launcher is not a replacement for AI Desktop's context manager or broker.

Distribution references: [CachyOS applications](https://wiki.cachyos.org/cachyos_basic/why_cachyos/), [settings and tools](https://wiki.cachyos.org/features/cachyos_settings/), [Kernel Manager](https://wiki.cachyos.org/features/kernel_manager/), [sched-ext](https://wiki.cachyos.org/configuration/sched-ext/), [chwd](https://wiki.cachyos.org/features/chwd/chwd/), [gaming](https://wiki.cachyos.org/configuration/gaming/), [snapshots](https://wiki.cachyos.org/configuration/btrfs_snapshots/), [Niri](https://wiki.cachyos.org/configuration/desktop_environments/niri/), [Arch system maintenance](https://wiki.archlinux.org/title/System_maintenance), and installed-version upstream manuals. These links are starting points for versioned procedures, not proof of the current host state.

Each implementation updates this roadmap with its own PR/SHA, tests, CI, remaining limitations and one next step. Add meaningful regressions to the existing `scripts/verify.py` gates and Python 3.12/3.13/3.14 Actions jobs, creating a new stage only for a distinct risk. Synthetic tests must cover success, refusal and failure without OAuth or real model requests. Changes to persistence, isolation, networking, host services and writes require applicable real CachyOS checks and explicit merge approval; Ubuntu CI is not that evidence. Keep documentation-only results separate from implementation acceptance.

**Next step:** implement S0, the split Arch packages and target-system DMS selection, including upgrade coverage. S1–S6 remain TODO and S7 remains DEFERRED until their own work and evidence are recorded.

## Conditions for the 1.0 release

1. Stages 1–5 complete.
2. The whole flow from the top of this page passed on CachyOS for one commit, also on a fresh install according to README.
3. Real failures checked: login expiry, no network, service limit.
4. Independent security review.
5. Anthropic's terms checked for a program that runs Claude Code with a subscription login.
6. Release checklist ticked for this commit.

## Progress log

### 2026-11-06 — E3–E6 / S3–S5: complete project work before desktop extras

- **IN PROGRESS**, candidate `0.1.0-79`, continuing PR #18 from `0473e9990e89c5355edc2ba406d9e0d8bc21dfc3`; verified `main` remains `4c1e81c31d9e1b6033e21a444b1fe370224c9ce6`. The maintainer prioritizes a usable analyze → edit → test → undo flow. `-c` and desktop refinements are deferred. S0 now specifies one universal package and reconciliation of Skills for all installed environments, with no repeated downloads for an unchanged set; no desktop-pack downloader is implemented in this candidate.
- S3 implementation: default selected reads 128 files / 2 MiB per file / 4 MiB combined; configurable ceilings 512 / 8 MiB / 16 MiB. Local text import defaults to 32 MiB, ceiling 64 MiB. Ordinary ask can search content and read bounded line ranges with versioned coverage; the entire large file is not sent. Anchored replacements reconstruct larger edits locally with uniqueness, overlap, format and restored-byte budget checks. Copy budgets are independent: 2,000 files / 8 MiB / 64 MiB defaults, 20,000 / 32 MiB / 128 MiB ceilings. Suite-based project edits test the proposed overlay in the full supported authorized project copy; copy-only files stay out of model input.
- Bounded context groundwork from S2: 64 recent turns within 512 KiB, answers up to 32 KiB, 96 task notes within 64 KiB, 2,048 revocable source identities and saved records up to 16 MiB. Listing sessions retains summaries rather than all large records. This does not implement unlimited transcripts, semantic compaction, native session reuse, streaming or `-c`.
- S4 implementation: shared frozen bundle loader and model-selected procedures/references in ask/material/project/task, separate knowledge budget, hash/assignment rechecks, explicit unknown/mismatched version behavior and worker-side envelope validation. Existing saved role selections remain authoritative; new defaults expose the general catalog. Public web tasks do not inherit local knowledge. Live documentation retrieval and persisted loaded-resource reuse after resume are still open.
- S5 implementation: existing general Skills extended with 11 source-backed reference sections for Arch/CachyOS tools, including a concrete version-specific Pacman reference. Versions come only from successful exact allowed package queries. Rolling wiki snapshots retain an explicit non-version-matched label. No personal machine facts, automatic package/service changes or new command grants are included.
- Evidence so far: 94 transport/contract/provider tests passed locally, including >2 MiB data through actual canonical request/Codex reconstruction and knowledge-consent invalidation; targeted file/copy, large-material, context, source-guard and reference tests passed. New fixtures cover 32 MiB source inspection with small delivered excerpts, large anchored edits and rollback, separate multi-file copies, version mismatch and revocation. Real copy/namespace fixtures use the existing local/CI stages. The executor maps only UID 0; ordinary-user lifecycle tests refuse here by design, so this is not a full local gate PASS. GitHub CI and real CachyOS acceptance are pending for the integrated commit.
- Remaining gates: integrated non-root CI, real CachyOS tests of file/namespace/resource boundaries and explicit maintainer merge approval. No real model, OAuth operation or host configuration change was performed. **Next step:** run and fix the complete candidate CI, then execute the existing host gate with the added large-project fixtures on CachyOS.


### 2026-11-06 — E3–E7: agreed continuation, file, Skills and CachyOS integration scope

- **Status: TODO** for S0–S6; **DEFERRED** for S7. The requested scope is recorded in this roadmap with delivery order, dependencies, acceptance targets, official sources and remaining host checks. This is a planning/documentation change, not implementation evidence.
- Baseline: `main` remains `4c1e81c31d9e1b6033e21a444b1fe370224c9ce6`; [PR #18](https://github.com/ultherego/ai-desktopv2/pull/18), branch `fix/conversation-project-review`, was inspected at `55233872e1ef916efa8b6c2576f8c862040e1c1d`. Its [five completed CI jobs passed](https://github.com/ultherego/ai-desktopv2/actions/runs/42170506262); it remains a draft with no merge approval. Those checks do not validate the new planned functionality.
- Recorded: optional DMS packaging; `-c` with incremental saving; long-session history/compaction/streaming; consistent larger-file, edit and test-copy budgets; on-demand Skills and versioned documentation in all modes; general Arch/CachyOS tools; profile-sync-daemon, ananicy-cpp, Cachy-Update, systemd-oomd, bpftune and Bluetooth controls; deferred desktop entry points, crash diagnosis, OCR/dictation and terminal layouts.
- Review evidence: targeted code inspection of the `-78` session, file and Skills paths; official Omarchy/CachyOS/upstream documentation; exact Hello service mapping from CachyOS Welcome `efd467041a9accb461bc3bf8a096a6627f795293`. No AI request, OAuth operation, host service control or package change was performed. Package version remains `0.1.0-78`.
- Documentation validation: scope coverage, section/status consistency, table structure, relative links, code fences and a private-data scan passed; an independent review checked completeness and release-scope clarity. Full implementation tests were not repeated for prose-only edits. Results for the documentation commit are tracked separately in [PR checks](https://github.com/ultherego/ai-desktopv2/pull/18/checks).
- Remaining: implement and verify each entry before changing its status; apply the existing CachyOS and explicit-approval merge requirements to changes affecting persistence, network, permissions or writes. Optional R4 work does not block the current MVP.
- **Next step:** S0 — implement split Arch packages and target-system DMS selection, with fresh-install and combined-package upgrade tests.

### 2026-11-05 — E3–E6: desktop agents and the authorized change-and-test flow

**Status: READY FOR TEST.** [PR #18](https://github.com/ultherego/ai-desktopv2/pull/18), branch `fix/conversation-project-review`, based on [`4c1e81c`](https://github.com/ultherego/ai-desktopv2/commit/4c1e81c31d9e1b6033e21a444b1fe370224c9ce6) after #15 and #17. Candidate `0.1.0-71`.

- Start chat directly, select Claude/Codex/loopback, switch recipients explicitly in a running or saved conversation, inspect what will be shared, or keep the previous recipient. The local endpoint has its own grant. A policy change invalidates the authorization; old answers with unknown provenance retire from outgoing history.
- Named cloud slots, offline quota observations, terminal panel and Niri/DMS integration share the same selection. [Installation](AGENT_INSTALL.md), [account and local configuration](AGENT_MANAGEMENT.md) and [desktop setup](DESKTOP.md) describe the entry points. A desktop status query cannot log into an account.
- Skills obey the owner's wire budget. Setup assigns the diagnostic procedures to an empty diagnostic role; reuse requires a current assignment, matching content and an authorized recipient. Nested commands require literal verbs. Project edits carry the context the user approved, including earlier agreements.
- Project test recipes exercise isolated copies. The approved policy and snapshot are checked again before reuse. Go and npm additionally require a resource-limited user scope. Test failures or incomplete input prevent a successful result; host acceptance must cover the real toolchain and resource limits.
- Evidence: source-table migration [`677e803`](https://github.com/ultherego/ai-desktopv2/commit/677e8034fe1b7b52595e16411e932b979297f801) passed [five CI jobs](https://github.com/ultherego/ai-desktopv2/actions/runs/42084707209). On [`679af76`](https://github.com/ultherego/ai-desktopv2/commit/679af7649b0e9a9fc604d89b8f08380e7aed10f6), [both kernel jobs](https://github.com/ultherego/ai-desktopv2/actions/runs/42086512600) passed real success and refusal paths for pytest, Go and npm. Unit jobs exposed the oversized undo transcript, a mismatched test-user identity and a changed failure label. Follow-up changes address them; the complete updated tree is evaluated in [the PR checks](https://github.com/ultherego/ai-desktopv2/pull/18/checks). Offline component tests use synthetic accounts, files and HTTP fixtures. Full local verification in the development container is limited by UID 0; local tests did not use real credentials, native inference or the desktop compositor.
- CachyOS host evidence for [`59e6060`](https://github.com/ultherego/ai-desktopv2/commit/59e6060740179bf7f26f58b60301201adcb183b7): the maintainer reported CI 21/21, LOCAL 29/29 with the project suites required, 60/60 targeted tests and the desktop widget JavaScript checks passing. Final exit codes: `CI=0 LOCAL=0 CASES=0 WIDGET=0`. The [five GitHub jobs](https://github.com/ultherego/ai-desktopv2/actions/runs/42088672140) also passed for that commit.
- The maintainer opened the installed 0.1.0-71 panel and viewed provider-reported limits on CachyOS. User feedback identified a dense quota summary, repeated dashboard redraws and an ambiguous refusal for oversized project copies.
- E7 follow-up, candidate `0.1.0-72`, **READY FOR TEST**. Human quota views split windows, color usage and show local resets; an interactive limits page returns without adding dashboards to shell history. Project preflight reports the specific limits it encounters. The acceptance evidence above belongs to the earlier code commit. Unit and PTY regressions run in the existing CI gate.
- Installed-terminal acceptance for the `0.1.0-72` changes in [`4417402`](https://github.com/ultherego/ai-desktopv2/commit/44174025faab32cdc8a15449c0b858d18f401f10): the maintainer confirmed that `usage` displayed separate quota windows with local reset times, the panel returned to the shell and opened a conversation, declining the send cancelled the request, and `/test python-unittest` on an oversized checkout reported each exceeded limit and stopped before running tests. The [five GitHub jobs](https://github.com/ultherego/ai-desktopv2/actions/runs/42094361059) passed on the implementation commit. This host report covers the installed terminal and refusal path; a real local-model request and desktop widget display are still unverified on the host.
- The maintainer confirmed that the previous widget opened on a live DMS bar. Candidate `0.1.0-73` addresses the follow-up requests for graphical quota windows and click-to-open account controls. The popout uses provider-reported data, local reset times and theme colors for live values. Discrete desktop entries launch the existing per-provider account picker; the optional service/account selectors apply to future requests.
- **Status: READY FOR TEST.** Coverage includes synthetic quota values, expired and absent windows, graphical fill geometry and colors, selection guards and packaged launch targets. These tests run without a real subscription.
- CachyOS feedback on the installed widget from [`23a0d38`](https://github.com/ultherego/ai-desktopv2/commit/23a0d38a2683dc5cdd3b751e201e3a1eda9553ac): DMS reported `QuotaCard is not a type` on loading a revisioned widget URL. The [five CI jobs](https://github.com/ultherego/ai-desktopv2/actions/runs/42102034964) passed on that SHA, but their cold-start and isolated card tests missed the upgrade in a running shell.
- E7 follow-up, candidate `0.1.0-74`, **READY FOR TEST**: load the packaged card and its status script from a new `quota/` directory. A running Qt 6.11 engine can cache an old directory listing as well as previously compiled scripts and cards. On Qt 6.4.2 and 6.8.3 the warm-engine regression reproduced both the missing-card error and a stale account label on the previous code. A separate Qt 6.11.3 run reproduced the reported error during an upgrade from the actual `-72` sources to a complete `-73` installation. With the fix, the real widget loads from a symlinked plugin directory, shows the account title and draws its 19% bar after a revisioned reload. DMS services in this test are synthetic.
- The next CachyOS load reached the widget buttons and failed on the unknown `variant` property. [CI for `bf1da15`](https://github.com/ultherego/ai-desktopv2/actions/runs/42105459322) passed all five jobs with a button test double that accepted this property. Loading the same widget with the actual DMS 2.1 button reproduced the host error.
- E7 follow-up, package `0.1.0-75`, **READY FOR TEST**: style the selected tab with the released DMS button colors. Offscreen checks with the actual controls from DMS 2.0.0 and 2.1.0 rendered both tabs and exercised both providers’ sign-in shortcuts with synthetic launch services. CI now uses the pinned 2.1.0 button on Qt 6.11.3, checks the active tab highlight, clicks the buttons, and verifies which desktop entry the widget requests.
- On CachyOS the maintainer opened **Usage** and **Accounts & login** from the installed widget, saw both Claude quota meters, and confirmed that terminal Codex login succeeded. The widget still advertised sign-in. The default login path selected the provider without recording the account login. The [five CI jobs for the previous candidate](https://github.com/ultherego/ai-desktopv2/actions/runs/42107917083) passed on `313c312`.
- E7 follow-up, candidate `0.1.0-76`, **READY FOR TEST**. Record local sign-in outcomes, reconcile older installations with a deliberate offline check, and offer confirmed sign-out for the displayed slot. The widget can select a service by card click and refresh both tabs. Synthetic tests cover default and named account receipts, offline status, refusal, sign-out and simultaneous operations. The disposable systemd gate exercises the installed account helper with a synthetic slot. Offscreen Qt tests exercised refresh, clicks on released DMS 2.0/2.1 buttons and nested account actions, as well as the widget upgrade in a running engine. Terminal tests cover colored results, plain text and dismissing the dedicated result window. A Codex login by itself supplies no numeric quota windows. The installed widget and colored terminal results still need maintainer acceptance.
- The installed widget for [0.1.0-76](https://github.com/ultherego/ai-desktopv2/commit/7561bb28b9216d8b1e846db371a8c6c0ccce55c1) showed the refresh action, cached Claude windows and the link to ChatGPT usage on CachyOS. [All five CI jobs passed](https://github.com/ultherego/ai-desktopv2/actions/runs/42114027214). The screenshot established the visible state; actual remote quotas were not available through the chat sign-in alone.
- E7 follow-up, candidate `0.1.0-77`, **READY FOR TEST**. Explicitly connect a native Codex limits session for an existing local slot and read provider percentages and reset timestamps on demand. The credential, workspace selection, narrow systemd endpoint and snapshots are separate from chat inference. Automated coverage includes protocol denials, synthetic account reads, socket authentication, packaging, and clicks in the released DMS controls. The [first five implementation checks passed](https://github.com/ultherego/ai-desktopv2/actions/runs/42121203317). A follow-up probe using Codex 0.166.0 against a loopback-only usage fixture exposed a timeout after the RPC had completed. The account transport now bounds shutdown separately after a validated reply and still rejects malformed trailing records.
- The maintainer reported the live-limits feature working on CachyOS after installation of [`a8344fe`](https://github.com/ultherego/ai-desktopv2/commit/a8344feb2861649942a2914cab2612c29efb2ade). [All five checks](https://github.com/ultherego/ai-desktopv2/actions/runs/42122940122) passed for that candidate.
- E7 follow-up, package `0.1.0-78`, **READY FOR TEST**. The account list can grow taller within the available DMS popout space. Codex actions fit inside the usage card. Both tabs support selecting the service from its card, and the accounts tab offers management for both cloud providers regardless of known sign-in state. Card selection preserves the account alias. An offscreen upgrade from `-77` passed with the released DMS 2.0 and 2.1 controls. With many accounts the list grew from a 340 px cap to 432 px. The test kept the footer accessible on a 480 px screen and clicked both the card and its separate actions.
- 2026-11-06 — CachyOS visual acceptance for installed `0.1.0-78`, built from [`0cc4be8`](https://github.com/ultherego/ai-desktopv2/commit/0cc4be81e16f02b33eb6760dd2fa58de7d6f74fb): the maintainer accepted the adjusted widget appearance. The earlier IPC error was attributed to invoking DMS outside the desktop user's graphical session after changing users in a terminal; package installation had succeeded. This report confirms the installed layout, without adding new click-path or full-gate results. [All five GitHub jobs passed](https://github.com/ultherego/ai-desktopv2/actions/runs/42126509638) for the installed implementation. This documentation update does not change its code or package version. The wider E7 work remains **IN PROGRESS**; PR #18 is still a draft and unmerged.
- **Next step:** implement the requested optional Arch packaging: produce `ai-desktop` and `ai-desktop-dms` from one `PKGBUILD`, and let the installer detect DMS on the target system when selecting packages. Without DMS, install only the independent core CLI. Acceptance must cover systems with and without DMS and an upgrade from the combined package. Merge still awaits explicit maintainer approval.

### 2026-11-05 — E3: quality of the Skills procedures

- **Status: READY FOR TEST.** Branch `improve/skills-procedures`, base `ba82f88c43eea65c7a3f5fe85a2a6639a5ed0d08`.
- Change trail: [PR #16](https://github.com/ultherego/ai-desktopv2/pull/16), commit with procedures and regressions [`872859d`](https://github.com/ultherego/ai-desktopv2/commit/872859d1c2e77a61a71ae4ffe258d0722ef9b7b1). The results of the required CI jobs are attributed to the current head of this PR.
- Scope: improvement of all 21 existing `SKILL.md` files, without changing names, agent assignments, permissions or the execution mechanism.
- Criterion: a procedure names the facts needed, how to interpret results, the stop conditions and the outcome of the diagnosis; a change requires a separate plan, test, consent and restoration of a specific state.
- Risks under review: a full balance without justification, a general `systemctl revert`, key reset and downgrade without diagnosis, boot repair without establishing the failure stage, and mistaking an isolated view for a host failure.
- Local evidence: 33/33 tests PASS (`test_skills`: 7, `test_user_skills`: 13, `test_prepared_skill_analysis`: 1, `SkillsOnDemandTests`: 12), no skips; validation of 21/21 contents and Python syntax PASS. The real contents of the packaged Skills go through consent and the final request with a file; the full set of contents fits in the edit request also in the modes that send all assigned Skills. Regressions are run by the existing `unit` stage of the central gate and the GitHub Actions matrix, with no separate gate.
- Content review: an independent reviewer assessed all 21 procedures with no significant blocking remarks. Six synthetic scenarios checked decisions concerning APU/dGPU, a missing audio session, the need for a balance, keeping existing drop-ins, an unestablished boot stage and the meaning of `text: PASS`. Neither the official client nor the diagnostic commands from the procedures were run.
- CI for `c53e51514a3c07e58b43c75c8110c51a616a8302`: **5/5 required jobs PASS**, [run 42060801397](https://github.com/ultherego/ai-desktopv2/actions/runs/42060801397): Python 3.12, 3.13, 3.14 and two versions of Bubblewrap. This is evidence for this SHA, not CachyOS acceptance. The full local gate was not repeated in a UID 0 environment, where tests requiring a regular user refuse to run.
- Not finished: checking usefulness in a real conversation on CachyOS. Format validation, scenario review and a transport test do not confirm that a repair works on the host. The remaining criteria of stage 2 and E3 stay open.
- **Next step:** after CI passes, assess the procedures during ordinary read-only diagnostics on CachyOS, without performing the suggested system changes.

