# Architecture and threat model

A description of the current code, not an isolation certificate. The table in section 5 describes the required behavior, not a result passed in every environment. Reporting problems
and consents: [SECURITY.md](../SECURITY.md). Limitations: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## 1. What we protect and from whom

**Assets:** the user's home directory and configuration files, the client's login profile (OAuth) in `/var/lib/ai-desktop`, SSH and GPG keys, local sockets and services,
the network, project files, and the user's decision to send or write data.

**Adversary:** prompt injection in a file, command output, Skill or model output; a wrong or hostile response from the provider; unintended action by the model; invalid
JSON; replacement of a file or symlink during an operation (symlink, TOCTOU); service failure or timeout; unwanted network traffic.

**Out of scope:** root, a compromised kernel, a malicious `/usr`, a hostile process of the same user, power loss, physical access. The client has access to its own OAuth.

**Basic rule:** the model and its response are not a source of permissions. The user's policy and the controller decide, and for writes also a human.

## 2. Architecture

```text
User (terminal, TTY)
   │  question, consents
   ▼
ai-desktop controller (user's UID) ── reads the policy and files, collects data, writes after consent
   │        │            │
   │        │            └─ allow commands: bwrap, read-only, no network
   │        │               change commands, test, undo: no sandbox, after "yes"
   │        └─ tests of proposals: bwrap, copy in tmpfs, seccomp, no network
   │  sudo: fixed root helper, only start/stop of session units
   │  Unix socket (0600, owner is the user's UID)
   ▼
Analysis service (UID ai-desktop, systemd unit with restrictions)
   │  CONNECT proxy outside the sandbox ──► only allowed hosts, port 443, public addresses
   ▼        ▲ Unix socket
Official client in bwrap (separate user/mnt/pid/net/ipc/uts namespaces)
```

**The controller** runs as a regular user and refuses to work as root. The package runs Python with `-I -B` from a root-owned directory.

**The session helper** is a fixed file run through `sudo`. It creates a transient unit with restrictions and a time limit, starts a socket only for the calling UID and cleans up
even after the interface dies. The channel to it carries no data or credentials. The package installs no sudoers or polkit rules, and the units stay inactive until a session starts them.

**The analysis service** runs as `ai-desktop`, with no login shell and no supplementary groups. It has `NoNewPrivileges`, an empty capability set, `ProtectSystem=strict`, `ProtectHome`, limits on memory,
tasks and time, and restricted socket activation. Before use it checks its own identity, the permissions of the login directory and the owner of the program's files.

**The Claude client in the working 0.1.0-79 runtime** is the official executable from a root-owned package-private directory, in the exact reported version 2.1.312. It gets a clean environment, its own writable login directory, and an empty HOME and working directory. It has no access to the user's
home directory or desktop sockets. Normal mode: `--tools ""`, an empty MCP configuration, one turn, no session saving. Web mode: `WebSearch` only, at most 4 turns and 2 calls. A seccomp filter forbids setting SUID/SGID bits. There is no general system call filter.

**The proxy** handles only CONNECT to `api.anthropic.com`, `claude.ai`, `platform.claude.com` (for Codex inference, separately `api.openai.com`; for an explicitly connected native quota session, `auth.openai.com` and `chatgpt.com` on HTTPS). It resolves the name once, checks all addresses, rejects non-public ones (including localhost and metadata addresses) and connects to the checked address. Before connecting it requires
a ClientHello with SNI equal to the host name, and rejects ECH. It does not use MITM. It does not see TLS content: it does not filter paths or data.

**Login** happens through the client's official flow in the same isolation mode, directly in the terminal, without capture. The program does not read tokens, and before a request it requires the status `authMethod=claude.ai`. There is no API key and no workaround. Codex: login through a trusted component outside the model's space, a short-lived token goes through a private channel to the client process; a separate token store `/var/lib/ai-desktop/codex`.

**Weather** is a controller collector to two fixed Open-Meteo hosts. **systemd:** only unit names and states.

Rejected option: a token broker hiding OAuth from the client, once marked "PREFERRED, TO BE VERIFIED IN E2". It is not the current architecture or a fallback plan. The old entry points `diagnose ask` and `controller ask` refuse and do not start the model.

### System-client transition: target, not current runtime

The replacement architecture uses either a compatible system-installed Claude client or a compatible system-installed Codex client. Neither provider is a prerequisite for the other. AI Desktop packages will contain the adapters and isolation machinery, not vendor executables. Candidate 0.1.0-80 adds shared metadata-only discovery to `doctor`; it does not execute the system binary or check its version/protocol. A `present` result is not vendor authentication or runtime readiness. The existing adapters continue to use their current installation paths until explicitly changed and accepted.

Discovery must examine supported absolute system paths, ownership and modes of the executable and its parents, executable format, integrity and version compatibility. Arbitrary `PATH`, HOME binaries and shell/npm wrappers are outside the initial supported set. A future controlled version probe must remain separate from inference and authentication; no such probe runs in this foundation. A pure version-policy comparison against the tested 2.1.312/0.166.0 baselines is not a capability test. Unknown compatibility is a refusal, not permission to try the model.

The execution path must bind its verified client identity to the task and recheck before launching it. A replacement or update during the task must stop further execution. Compatible updates between tasks should not require an AI Desktop rebuild. The service account, separate account profiles, namespaces, restricted tools and egress policy remain separate mandatory boundaries. Discovery must never reuse the user's native CLI credentials or expand mounts to accommodate an untrusted wrapper. Runtime validation must cover the supported installation layout and required helpers, not just the first ELF found.

Risks to test before runtime migration ships: symlink/path substitution, writable parents, a changed executable between validation and launch, a misleading version result, unsupported protocols and a provider-only installation. A passing discovery unit test is not evidence for the migrated authentication or namespace path.

## 3. Data flow and consent

1. **Policy.** The controller reads `permissions.toml` only through the system account (no HOME or XDG): owner, `0600`, no symlinks and no write access for others. The result is bound to the exact bytes and identity of the file, and a change invalidates the operation.
2. **Metadata.** The model receives the question and a description of access (patterns, templates, sources), without `deny`.
3. **Plan.** The model's response is parsed as strict JSON (no repeated keys and no `NaN`), with a closed list of actions and limits. Plain text in a conversation becomes an answer with no permissions.
4. **Discovery and read.** Names and contents are collected by the controller through descriptors with `O_NOFOLLOW`, with checks of `deny`, owner, type, hard links and changes along the way. A read must be backed by discovery or the user's text.
5. **Sending.** Each request has its own internal approval: an immutable payload, SHA-256, a nonce and an expiry time, single use. The window is 180 s (330 s for `xhigh`, `max`) per step and 30 minutes per question, not counting time spent waiting for the user.
6. **Result.** The model's text is limited and cleaned of terminal sequences. Command results too.
7. **Write.** A proposal applies only to files marked as writable. Test on a copy, full diff, consent, re-check of the policy and file state, then journal and publication.

**Fail-closed rule:** a missing sandbox, a wrong version, an exhausted budget, a state change or an unconfirmed session stop end the operation with a refusal. No retries and no fallback.

The next consent design will cover a declared plan, its bounded stages and copy tests with one approval. This must retain the per-request immutable payload, live source/policy checks and budget accounting; it is not blanket consent for later model suggestions. Final diff approval before publication remains separate. The working 0.1.0-79 flow still asks separately for project stages and copy tests.

The output of `--v`, `doctor` and `status` authorizes nothing. `status` is offline: it does not start the client and does not prove login.

## 4. Command boundary

### `[commands] allow`

**Template parsing** (when the policy is loaded): at most 10 tokens and 120 bytes, no shell characters, literals from a limited set. The first word cannot be a shell, an interpreter, an escalation tool, a network client, or a program that writes or runs other programs (`env`, `find`, `git`, `less`, `tar` and others). A placeholder cannot stand as the first argument or before a subcommand of tools like `systemctl` and `docker`. A literal path covered by `deny` is rejected.

**Matching:** the model's proposal is a list of words, never text for a shell. Comparison is token by token, placeholders have their own pattern. The program is resolved in fixed system directories: the file and all directories must be owned by root and must not be writable by others.

**Execution:** without asking, without a shell or input, with a clean environment and limits on time and output. Sandbox: `/` read-only, tmpfs on `/home`, `/root`, `/run/user`, `/tmp`, no network, IPC or capabilities, separate PIDs (unless `share_processes = true`, in which case host processes and their command lines are visible). The output is cleaned and goes to the model as untrusted data.

**Budgets:** 16 commands in a plan, `command_rounds` per question, a shared `discovery_bytes` limit. No `bwrap` means no execution.

**What this boundary does not give:** the sandbox does not isolate the host's Unix sockets (D-Bus, Docker), so a command from the list can change state without asking. The sandbox does not hide the rest of the file system (for example `/etc`). `deny` does not filter output: journals and process lists can contain sensitive data. Responsibility for the list rests with the user.

### `[commands] change`

A template is in `allow` or in `change`, never in both, and a placeholder cannot stand before a subcommand. The `change` list works only with a non-empty `allow` list. The plan must give a test (matching `allow`, not `change`) and an undo (from `change` or `allow`). There must be budget for three commands, otherwise nothing happens. A command from the `change` list cannot pass in a grouped read plan. A `read-only` agent does not get this list at all.

Typing the whole word `yes` is required after seeing all three commands. The change, test and undo run **on the live system, without a sandbox**, with a cleaned environment (user bus available, no HOME and no secrets), without input, with limits. A failed test shows the undo and requires a second `yes`. The program never undoes on its own. An approved undo may not run if the question's budget runs out.

What this means: after `yes` the command has the user's full permissions. Placeholders in the template (for example `<unit>`) are chosen by the model. The test and undo may not restore the state. Consent applies to the content shown, not to its effects.

### Project suites and scope checks

Python checks run against authorized copies with a private network, no host sockets and a syscall filter. Go and npm can run children. The controller creates a new user scope and reads the effective cgroup v2 PID, CPU, memory and swap ceilings before running Bubblewrap. An unavailable manager or controller aborts the suite. A regular project cannot name its own host command or remove a limit. An npm test can decide to return success without running assertions; the controller reports the script result accordingly.

### Conversation recipients

A provider, account slot and local port, where applicable, are part of the recipient identity. A saved thread can be transferred through the controller after checking its current policy and obtaining a new decision. A local server receives only the approved request. Numeric loopback constrains the connection destination; whoever can serve that port controls how its contents are processed. Account status and desktop widgets consume observations without exporting, interpreting or polling OAuth secrets.

## 5. Threats

| ID | Attack or failure | Required behavior |
|---|---|---|
| T01 | Unexpected field, repeated key, malicious payload | Rejection before sending, no raw logs |
| T02 | Payload replaced after consent | SHA-256 and nonce mismatch, refusal |
| T03 | Client access to the home directory, `.ssh`, other people's credentials | No mounting. Exception: the client's own OAuth profile. Approved text is not a mount |
| T04 | Write on the host, in the host's `/tmp`, starting a shell through the client | No tools, no writable mounts outside the profile |
| T05 | Host socket, D-Bus, SSH agent in the client | No access and no forwarding |
| T06 | Arbitrary traffic, localhost, metadata address, redirect, DNS rebinding | Blocked outside the host list. A redirect within the list remains possible |
| T07 | Tokens exposed in a question, error, response | The controller does not read tokens and does not pass on raw errors. No token protection against a compromised client |
| T08 | Network error, expired OAuth, no `bwrap` | Fail-closed, fixed message, no fallback to the host |
| T09 | Malicious instruction in model output | Text does not become a command or a write |
| T10 | Symlink, path escape, TOCTOU in the task directory | No escape outside the scope; a state change is a refusal. Exception on read: a directory symlink in `version = 2` under the conditions in [AGENT_MANAGEMENT.md](AGENT_MANAGEMENT.md) |
| T11 | Plan or file changed after approval | State mismatch, refusal |
| T12 | Interruption or timeout during a write | Journal before the write, state visible in `task status`, undo after consent, no guarantee on power loss |
| T13 | Invalid response or error from the offline executor | Refusal, the program does not start the model from this path |
| T14 | Prompt injection through `SKILL.md` | Skill only from the package or the user's directory, no foreign owner and no write access for others, size limit. Gives no permissions. Content is not filtered |
| T15 | WebSearch without `web = true` or after a policy change | Refusal before the client, no tools in other modes |
| T16 | Files or systemd data in a search | Web mode gets only the question and fixed instructions |
| T17 | Another tool, excess turns, forged result or source URL | Restricted client configuration and strict control of the stream and sources |
| T18 | Model proposes a command outside the list or with a different argument | Exact match, otherwise it does not run |
| T19 | Model picks a dangerous subcommand through a placeholder | A placeholder cannot stand before a subcommand |
| T20 | Replaced program in a user directory | Only root's files in root's directories |
| T21 | A command from `allow` changes state through a Unix socket | **Not blocked.** The user is responsible for the list |
| T22 | Command output carries data out to the provider | **Intended.** The boundary is the list and the output limit, `deny` does not filter output |
| T23 | Injection urges a live change | A change only from the `change` list, after `yes`. There is no default list |
| T24 | Change without a test or undo, a test that changes state | Refusal before execution |
| T25 | Bypassing `deny` with another spelling or an entry lower down | `deny` covers everything under the path; names filtered out before sending |
| T26 | Undo overwrites a later edit | Comparison of bytes and mode, a conflict keeps the user's version |
| T27 | Restoring from the trash to a taken name | Publication without overwriting, otherwise a conflict |
| T28 | Consent from a conversation stays valid after permissions change | Consent bound to access, sources and service, otherwise a new question |
| T29 | Pasted text approves a further action | Terminal buffer cleared before the question, no protection against another process on this TTY |
| T30 | Data returns after access is revoked | Dependent answers and memory are removed before the next turn |

## 6. Writing, undo and deletion

Writing goes through a single executor: directory lock, identity check, `.ai-desktop-tasks` journal (`0700`, files `0600`) with the old and new content before publication, writing a temporary file, check, publication. New files are published without overwriting; a taken name is a refusal. The paths `.ai-desktop-tasks` and `.ai-task-*` are reserved for the program, also in `version = 2`.

Undo compares bytes and mode. A conflict is kept in the journal and visible, but does not block new writes as long as no leftovers remain in the directory. Unresolved temporary files and unfinished tasks do block them. Limit of 128 journals. The journal is bound to the directory; history from another device or a replaced directory is unlinked and is not used for undo.

Deletion moves to `.ai-desktop-tasks/trash-*` after consent bound to the exact plan (identities, sizes, modes, SHA-256), valid for 180 s. At most 8 files of 16 MiB each. Restoring only to free names.

## 7. What the evidence does not cover

Synthetic tests use a mock client and do not prove the behavior of the real client or OAuth refresh. There is no test for a hostile process of the same UID or for power loss. There is no independent review. The result of one commit or computer does not attest another. Changing these boundaries requires describing the trade-off here and a test on a real system.

### Larger task data and knowledge (0.1.0-79)

The model wire ceiling (36 MiB) accounts for up to 16 MiB selected UTF-8 data plus JSON escaping and bounded metadata; it is not a new file grant or proof of provider token capacity. Generated workspace text has a separate 4.25 MiB ceiling; anchored replacements can reconstruct larger results locally. Complete restored bytes, including BOM/CRLF, remain subject to edit and rollback bounds.

Analysis opts into a 96 MiB aggregate TLS tunnel budget, including both directions and protocol overhead. Account/login tunnels retain the 4 MiB default. The limit is compiled into the minimal proxy module so its sandbox does not gain controller dependencies. Existing destination, SNI, timeout and connection checks remain in force.

Local project-test copies have an independent contract: default 2,000 files / 8 MiB per file / 64 MiB combined; hard 20,000 / 32 MiB / 128 MiB. The copy pipe is opt-in and unavailable to ordinary model calls. The sandbox bounds memory to 2 GiB, writable work to 160 MiB and individual files to 32 MiB. This increases resource consumption; the existing namespace/cgroup fixtures and the recorded 0.1.0-79 CachyOS acceptance cover that implementation, not a future runtime migration. Copy membership is never added to model context simply because tests need it.

On-demand procedure and reference selection in all modes is controller-managed. The knowledge envelope is canonically bound to consent and reconstructed without inheriting a worker's ambient catalog. Bundled references have source/version provenance and hashes but remain untrusted evidence. Package-version evidence comes only from a successful, untruncated exact allowed query and is reset with the task. No new command grant, network route, desktop detection/download or shell tool is implied by loading documentation.
