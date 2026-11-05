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

## Changing recipients

A transfer through `/provider` names the new recipient and requires an explicit decision about the bounded conversation history and task notes. File access is re-evaluated for the request. Old saved answers whose source permissions cannot be verified are withheld. Named cloud account slots use a validated local alias and the authenticated desktop UID; the panel reads account metadata and quota observations, never tokens.

Local inference uses an approved HTTP request to numeric IPv4 loopback and a bounded port from the policy. Trust the process listening there. A stopped server can leave its port available to another local process. A local server configured to forward traffic can transmit requests off the machine. The controller does not follow HTTP redirects or use proxy environment variables.

The optional desktop widget displays locally available metadata. The controller's private runtime entries contain the chat identifier, working directory, settings and exchange count. They are visible to the same desktop user. No conversation text is stored in those entries.

The widget passes a validated provider and local alias to a fixed installed launcher for an explicit credential check or logout. The target is confirmed in a terminal before logout. The default slot is shared across desktop users. Named slots are isolated by desktop UID and provider, with a binding that prevents a disconnected name from silently designating another identity. The service owns credential operations under its profile lock. The Claude check runs the pinned native client with read-only access to its profile in an offline namespace. Claude logout has writable access to the chosen profile in an offline namespace. Codex checks the protected local SIWC registration and attempts provider revocation only on explicit logout. The desktop receives a bounded outcome. Its local sign-in record is for display; the execution path checks the service-side credentials separately.

## Trust boundaries

| Element | Runs as | What it sees and can do |
|---|---|---|
| The `ai-desktop` program (controller) | your regular user, refuses to work as root | Reads the policy and files within its limits, asks for consent, performs approved writes. |
| Session helper | root, through `sudo`, a fixed file from the package | Only starts and stops the units of your session. It gets no questions, data or credentials. |
| Analysis service | system account `ai-desktop`, with no login and no privileges | A restricted systemd unit, holds the proxy and starts the client. |
| Claude Code 2.1.312 client | the same account, in Bubblewrap | Its own login directory `/var/lib/ai-desktop/claude`, empty HOME and working directory, no tools (except `WebSearch` in web mode), no direct network. |
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

- listing and searching **names** in directories from `read`, within budgets;
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
| `material`, `project` | Consent per question. Whole-file analysis: one consent per task, valid for 30 minutes. Next project stage: a separate `[y/n]`. |
| `task run` | Sending and writing separately. |

The consent prompt clears previously typed characters from the terminal, so pasted text does not approve an action you have not yet seen.
Without a terminal there is no consent: the program refuses. There is no unattended mode. Consent does not protect against another process controlling the same terminal.

After an applied change the model can take the next step without a new question from you (at most 5 steps), but each further write again requires
consent.

## What goes to the provider

The chosen provider (Anthropic, and with Codex OpenAI) receives:

- your question and pastes;
- the fixed instructions of the program and the agent's instructions with the content of assigned Skills, including your own;
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
- **Immutability of the client.** The reported version is checked, not the checksum of the file in the package. You check the checksum when downloading.
- **Correctness of changes.** A test on a copy concerns only the chosen check, `text` does not examine syntax or behaviour.
- **Atomicity.** A multi-file task and a package update are not one transaction. There is no guarantee in case of power loss.
- **Truthfulness of the model's answers, search results or sources.**
- **Checks on real failures:** login expiry and refresh and real network failures have not been checked with the official client.

## Rules for people developing the project

Do not add secrets, login files, client binaries or data about a specific computer to the repository. Changes to isolation, permissions, the file system, the network or
login require a test on real CachyOS or Arch and a description of the trade-off in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). The test gate is `scripts/verify.py`.
