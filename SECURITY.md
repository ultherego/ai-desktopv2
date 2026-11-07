# Security

AI Desktop is a development version. It has not gone through an independent security review. Green tests are not a certificate of isolation.
Full description of the architecture and threats: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). List of limitations:
[docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

## Reporting a problem

Report security vulnerabilities **privately**: on the repository page <https://github.com/ultherego/ai-desktopv2> choose
Security → "Report a vulnerability". Only the project maintainer sees the report. You can open ordinary bugs and questions as a
public issue.

A public issue is visible to everyone, so **do not put sensitive details in it**: tokens, login codes,
the content of private files, `--v` output, a working exploit or data about your computer. Describe the package version, the general symptom and the
fixed error message.

## Local conversation records

Interactive `ask` writes a private bounded checkpoint after the first completed turn or completed action unless `--no-save` is selected. A declined or cancelled first turn publishes no record. The first checkpoint is assembled and validated in memory, then published once through the existing no-follow, owner/mode, lock, temporary-file, `fsync` and no-replace checks; later checkpoints use revision comparison and atomic replacement. A failed publication leaves no partial JSON record and does not hide the answer or stop the in-memory conversation.

The record contains bounded recent questions, answers, task notes and source-provenance metadata. It never contains OAuth credentials, a native-client session, process-local consent grants, write approvals or restored file permissions. Opening it is local; every later provider transfer and source use still checks the current recipient, policy, source identities and fresh conversation consent. Same-UID hostile writers and power loss remain outside the guarantee.

## Changing recipients

A transfer through `/provider` names the new recipient and requires an explicit decision about the bounded conversation history and task notes. File access is re-evaluated for the request. Old saved answers whose source permissions cannot be verified are withheld. Named cloud account slots use a validated local alias and the authenticated desktop UID; the panel reads account metadata and quota observations, never tokens.

Local inference uses an approved HTTP request to numeric IPv4 loopback and a bounded port from the policy. Trust the process listening there. A stopped server can leave its port available to another local process. A local server configured to forward traffic can transmit requests off the machine. The controller does not follow HTTP redirects or use proxy environment variables.

The optional desktop widget displays locally available metadata. The controller's private runtime entries contain the chat identifier, working directory, settings and exchange count. They are visible to the same desktop user. No conversation text is stored in those entries.

The widget passes a validated provider and local alias to a fixed installed launcher for an explicit credential check or logout. The target is confirmed in a terminal before logout. The default slot is shared across desktop users. Named slots are isolated by desktop UID and provider, with a binding that prevents a disconnected name from silently designating another identity. The service owns credential operations under its profile lock. The Claude check runs the bound approved client with read-only access to its profile in an offline namespace. Claude logout has writable access to the chosen profile in an offline namespace. Codex checks the protected local SIWC registration and attempts provider revocation only on explicit logout. The desktop receives a bounded outcome. Its local sign-in record is for display; the execution path checks the service-side credentials separately.

The optional native Codex quota session is a distinct credential. The owner selects its workspace during device-code login. The quota client runs in Bubblewrap with a disposable Codex home and a dedicated CONNECT allowlist for the official auth and ChatGPT backend hosts. The regular model client continues to use its separate SIWC grant. The quota protocol speaks only initialization, device login, account inspection, cancellation and rate-limit reads. Its desktop reply contains validated numerical windows and an allowlisted plan. The service keeps the renewable local quota credential behind its service UID, bound to the desktop UID, local alias and current SIWC registration generation. A matching email is insufficient to establish a matching workspace.

Explicit setup enables the systemd quota socket for that UID. Its path is controlled by root, its mode is 0600, and the service authenticates the kernel peer UID. Any process running as that same desktop UID can request an enrolled slot’s limits. The desktop command checks its local network policy; network enforcement in the worker is the fixed native operation and compiled egress allowlist. Disconnecting removes the local credential. When no native links remain for that UID the administrative action removes the socket activation.

## Independent-client and registration boundary

Candidate 0.1.0-82 uses a compatible independently installed Claude or Codex client. Supported system packages run directly; official native/HOME/npm installations require explicit administrator-approved registration of their native payload. Neither provider is required by the other, and no vendor executable is included in the application package. The new CachyOS upgrade and native offline acceptance is recorded in [ROADMAP.md](docs/ROADMAP.md), separately from old package-private runtime evidence. It does not establish successful login, token renewal or model inference through the migrated runtime.

Direct execution discovery checks fixed system paths, root ownership, safe parent modes and executable format through bounded descriptor-relative inspection. The known AUR Claude native payload under `/opt/claude-code/` is examined without executing its shell wrapper. Arch Codex private helpers/resources under `/usr/lib/openai-codex/` are part of its payload. User PATH and known native/npm layouts may be inspected for registration, but do not authorize service execution. Unknown wrappers, privileged attributes, special files and unsupported layouts are refused.

Registration and refresh require an explicit payload approval followed by a fixed administrator helper. It captures the approved native executable and recognized resources into root-owned `/var/lib/ai-desktop-clients/PROVIDER/payload/`, without executing the source as root, downloading a provider, copying credentials or changing the original installation. Approval covers the captured contents, and mutation invalidates it. The registration manifest remains outside the mounted payload. This is a system-wide trust decision: one registered copy per provider takes precedence over a system package, and an unsafe/incomplete registration cannot silently fall back. A source update does not replace approved bytes; a separately approved refresh is required. Explicit removal removes the registration, not the original client or accounts. Root ownership and digests do not authenticate a vendor.

The supervisor captures the checked native client and supported private resources into bounded sealed memory files. For each sequential namespace phase, Bubblewrap materializes those bytes with explicit permissions into linked canonical paths on private tmpfs, then remounts the payload read-only before execution. This preserves executable-relative resource lookup; native clients do not inherit the snapshot descriptors or receive a writable payload. The capture limits are 384 MiB per file, 768 MiB combined, 4,096 entries and 24 levels; exceeding them refuses the complete operation. Memory includes up to 768 MiB of retained sealed snapshots plus up to 768 MiB of tmpfs files for the current child, within the 3 GiB analysis/quota service ceiling that also includes native-client memory. One binding is retained across compatibility, account and execution phases, and source-state changes refuse subsequent phases. Rechecks alone would leave a pathname race; the immutable snapshots ensure that a replacement after the last check cannot become the executable used by that operation. This does not freeze the root-managed OS libraries already present in the read-only system environment, or protect against a malicious root/kernel.

Version and capability probes run before credentials, model input or an egress bridge are exposed, in an offline namespace with an empty home. The tested baselines are Claude 2.1.312 and Codex 0.166.0. Newer versions in the supported version family must pass the same capability prerequisites and remain explicitly untested. Malformed output, unsupported versions/protocols, missing isolation, output limits and timeouts refuse the operation. A passing probe is not proof of every future reply; strict runtime response validation still applies.

Ordinary `doctor` and panel status remain metadata-only. `doctor --clients` explicitly probes the selected provider offline; it reads no account and performs no AI request. No diagnostic result grants model tools, network policy, desktop sockets or access to the user's ordinary CLI authentication. Existing separate account profiles, namespace restrictions, egress policies and exact write approvals remain mandatory. Application installation/update/removal never downloads or alters an independent client. Registration refresh changes only the explicitly approved runtime copy, and uninstall leaves that copy until explicitly removed. See [installation and migration](docs/AGENT_INSTALL.md).

## Trust boundaries

| Element | Runs as | What it sees and can do |
|---|---|---|
| The `ai-desktop` program (controller) | your regular user, refuses to work as root | Reads the policy and files within its limits, asks for consent, performs approved writes. |
| Session helper | root, through `sudo`, a fixed file from the package | Only starts and stops the units of your session. It gets no questions, data or credentials. |
| Analysis service | system account `ai-desktop`, with no login and no privileges | A restricted systemd unit, holds the proxy and starts the client. |
| Bound approved Claude Code client | the same account, in Bubblewrap | Its own login directory `/var/lib/ai-desktop/claude`, empty HOME and working directory, no tools (except `WebSearch` in web mode), no direct network. |
| Proxy | service, outside the client sandbox | Passes only TLS on port 443 to `api.anthropic.com`, `claude.ai`, `platform.claude.com`, to public addresses. |
| Local model server | your configured loopback service | Receives the approved prompt, history and authorized material. The user who operates the service controls its logging. |
| Model | at the provider | Returns text and proposals. This is untrusted data. |
| Commands from `allow` | your user, in a separate sandbox | Read-only file system, hidden `/home`, `/root`, `/run/user`, no network. Host Unix sockets are not isolated. |
| Commands from `change` | your user, **without a sandbox** | Run on the live system after you type `yes`. |
| Tests on a copy | your user, in a sandbox | A copy in memory, without network, home directory or access to the project. |

The client does not have your home directory mounted. It gets data as text, passed by the controller after the policy check.

The policy `~/.config/ai-desktop/permissions.toml` is the source of permissions for `ask`, `material` and `project`. It must belong to you and have mode `0600`.
It is bound to the question: changing the file midway stops the current operation, and the next question requires new consent. `task run` does not use the policy:
you name the files by hand in the command.

An exhausted budget, a wrong model response, a file change or a missing sandbox end in refusal. The program does not retry a request by itself and does not
switch to another service, a client on the host or an API key.

## What model output can and cannot do

The model's response is checked against a closed list of actions. The response text is only displayed.

**It can propose:**

- listing and searching names in directories from `read`, and bounded content searches and line reads in authorized files;
- reading files from `read`, but only those shown by discovery or typed by you;
- running a command matching your template from `allow`;
- writing or creating a file in `write`, deleting to the trash, a change from the `change` list with a test and an undo;
- a web search when `web = true`, and getting the weather when `weather = true`.

**It cannot:**

- grant itself permissions, change the policy or turn off consent;
- run a shell, an arbitrary program, a command outside the template, or choose a subcommand in a place fixed by the template;
- read or write anything covered by `deny` or lying out of scope;
- save a file without your `y` after the full diff;
- perform a change from `change` without your `yes`;
- point to an arbitrary URL or host.

Important: **the content of a file or the output of a command can lead the model to further allowed reads**, and their results go to the provider. The limit is
`permissions.toml` and the per-question budgets, not the model's common sense. Refuse access to what you do not want to send.

## Consents

| Action | Consent |
|---|---|
| `ask` conversation: questions, reads, discovery, commands from `allow` | One `Send this request to Claude? [y/n]:` per conversation. Again after a change of permissions, service or agent. The consent lives in process memory. |
| Writing or creating a file | Full diff and `[y/n]` every time. |
| Deleting files | A list of files and `[y/n]`. Files go to the task trash. |
| Change from `change` | The change, test and undo shown, typing the whole word `yes`. An undo after a failure also requires `yes`. |
| Undo and restore | A separate `[y/n]`. |
| `material` | Consent per question. Whole-file analysis: one consent per task, valid for 30 minutes. |
| `project` analysis | One approval for at most 5 stages, 40 requests, 10 MiB of project data and 50 minutes. Source, policy, recipient and deadline checks apply before continuation; changed scope stops the task. |
| `task run` | One initial approval covers sending the selected files, bounded preparation and one exact declared test on a selected-file copy, including Go/npm. The full diff and write still need a separate approval. |
| Project suites in `ask`/`project` | Current runtime: a separate approval for the permitted local copy and fixed recipe, before the final write decision. |

Candidate 84 binds project analysis to its declared stage budgets and standalone tasks to their preparation and one declared copy test. It cannot silently add a test, widen data access or reuse approval after a relevant change. Combining preparation, additional edit-data transfer and project-copy tests in ordinary conversations/project edits remains open in S9. Final diff approval, live system changes, deletion and undo remain separate. Candidate 84 passed its separate offline CachyOS acceptance; the evidence and remaining merge approval are recorded in [docs/ROADMAP.md](docs/ROADMAP.md).

The consent prompt clears previously typed characters from the terminal, so pasted text does not approve an action you have not yet seen.
Without a terminal there is no consent: the program refuses. There is no unattended mode. Consent does not protect against another process controlling the same terminal.

After an applied change the model can take the next step without a new question from you (at most 5 steps), but each further write again requires
consent.

## What goes to the provider

The chosen provider (Anthropic, and with Codex OpenAI) receives:

- your question and pastes;
- the fixed instructions, the agent's instructions, assigned Skill metadata and the procedures/references actually loaded, including your own;
- the patterns of `read` and `write` rules, the `allow` and `change` templates, the availability of systemd, weather and the Internet. `deny` rules are not sent;
- names, types and sizes from discovery steps;
- the content of files read with **absolute paths**, which may contain the user name;
- command output, including from `allow` commands run without asking (the `deny` filter does not cover them);
- names and states of systemd units and the weather result;
- the latest conversation exchanges and the task memory.

On top of this comes the technical context of the official client and login data, which the program neither reads nor shows. `--v` shows the program's data, not
a record of all encrypted traffic.

Other network targets: `weather = true` reveals the place name and IP address to the Open-Meteo service. `web = true` sends the question itself for a search on the
provider's side; the question's content may go into search engine queries, and files and local data are not attached.

Data that was sent cannot be withdrawn.

## What we do not guarantee

- **Protection against another process of the same user**, root, a compromised kernel or a malicious system.
- **Protection of the client token against the client itself.** The official client has access to its own login profile. A compromised client could send it to an allowed
  host. The proxy does not see TLS content and does not filter paths or data. There is no general system call filter for the client.
- **Safety of the `allow` list.** The sandbox does not isolate host Unix sockets, so a command able to change state through D-Bus or Docker is not stopped,
  and it runs without asking. List only read-only commands here.
- **Recognition of secrets.** In `version = 2` there is no hidden name filter or private key scanner. A secret in an allowed tree without `deny` can go to the provider.
- **Vendor authenticity or a frozen operating system.** Client snapshots bind the checked executable and supported private resources to an operation. They do not certify the package vendor, snapshot system runtime libraries or protect against a hostile administrator. Metadata-only discovery does not authorize execution.
- **Correctness of changes.** A test on a copy concerns only the chosen check, `text` does not examine syntax or behaviour.
- **Atomicity.** A multi-file task and a package update are not one transaction. There is no guarantee in case of power loss.
- **Truthfulness of the model's answers, search results or sources.**
- **Checks on real failures:** login expiry and refresh and real network failures have not been checked with the official client.

## Rules for people developing the project

Do not add secrets, login files, client binaries or data about a specific computer to the repository. Changes to isolation, permissions, the file system, the network or
login require a test on real CachyOS or Arch and a description of the trade-off in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). The test gate is `scripts/verify.py`.

## Large files and shared knowledge

File read, excerpt, generated edit, history and local test-copy budgets are separate. A larger local copy does not authorize sending its files to a provider. Only selected model inputs cross the model transport; copy-only source and test output stay local. The copy worker has a separate bounded input pipe, filesystem size and memory ceiling. These defaults can consume materially more memory than the earlier small-copy implementation; refusal must happen before a partial copy is tested as complete.

A large-file content search or range read returns only bounded excerpts with source identities, hashes and coverage. Each following handoff rechecks the policy and source state. An anchored edit must uniquely match non-overlapping fragments of the approved original. The controller reconstructs the result, checks file and total byte budgets after restoring line endings, then uses the existing diff, copy test, consent, source-state check and rollback. A model proposal cannot authorize its own write.

Skills and their local references are frozen before task consent and rechecked before sending. Selected knowledge has its own bounded, hash-bound envelope for material/project/task, and a separate budget within ask discovery. It cannot grant tools or permissions. Reference version claims come only from strict results of an already-authorized package query; free text or model-supplied versions are not trusted observations. Rolling references are dated snapshots, not version compatibility proof. Public web requests do not inherit this local knowledge envelope. This implementation does not add automatic remote documentation downloads or native client Skill tools.

## Bounded task approvals

Candidate 84 can reuse one explicit approval for at most five read-only project stages, under a fixed snapshot, policy, recipient and total request/data/time budget. Standalone `task run` can approve preparation and one exact declared test on its selected-file copy together. These grants are process-local, expire and cannot authorize a different recipe or input scope; test grants are consumed once. Source/recipient checks still run before transfer and test execution. The final immutable diff needs its own write approval. No grant survives saved-session resume, and ordinary conversation approval does not authorize running a project test. New consent behavior requires CachyOS acceptance before merge.
