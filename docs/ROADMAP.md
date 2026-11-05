# Plan for version 1.0

**Goal:** you start the program, talk in plain language, diagnose the system and ask for changes, and every change waits for your consent and can be undone. The flow that must work end to end:

> analyze → let's agree on the change → do it → test it → undo

This page describes the stages and the conditions for completing them. Individual tasks are in [TODO.md](TODO.md), limitations of the current version in [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md), and the release steps in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).

## How completion counts

A stage is complete when its conditions are met for a specific commit. The result of another commit attests nothing. Green CI is not enough for changes to isolation, permissions, the file system, network and login: these also require a test on real CachyOS or Arch, with the commit number recorded.

Status: **done** (merged), **in progress** (waiting for a test or merge), **to do**.

## Current state

Working and merged:

- `ask` conversation with one consent, 32 exchanges and task memory, `/` commands, messages up to 128 KiB.
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
| Skills on demand also in `material`, `project` and `task` | to do |
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
| One client session per conversation, streaming answers | to consider |

## Conditions for the 1.0 release

1. Stages 1–5 complete.
2. The whole flow from the top of this page passed on CachyOS for one commit, also on a fresh install according to README.
3. Real failures checked: login expiry, no network, service limit.
4. Independent security review.
5. Anthropic's terms checked for a program that runs Claude Code with a subscription login.
6. Release checklist ticked for this commit.

## Progress log

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
- **Next step:** reload the installed fix on CachyOS, verify that **Usage** renders and **Accounts & login** opens the intended picker, then obtain the maintainer's explicit merge decision. A real provider login requires a separate decision.

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
