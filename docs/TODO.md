# To do

Status: package `0.1.0-72`. Limitations of the current version are described in [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## Next up
Stages and the conditions for completing them are described in [ROADMAP.md](ROADMAP.md).
- [x] `task rollback`: do not overwrite or delete a newer editor save.
- [x] Long change instruction: the edit stage refuses an instruction above 2 KiB, although `ask` accepts 128 KiB.
- [x] The space before `/` is removed, so a word with `/` cannot be sent as a question.
- [x] The final answer after a file read gets the command results and loaded Skills.
- [x] Files with BOM and Windows line endings can be read and edited without changing their format.
- [x] The result of a completed change stays in memory despite a service error.
- [x] Dependence on a revoked file does not carry over to independent answers.
- [x] Matching `change` template limits (policy and contract: 128).
- [x] An option's value is not taken for a command in templates (`systemctl -H host <word>` is refused).
- [x] Skill for a general question without `permissions.toml`.
- [x] Rebuild of the 21 Skills procedures, independent review of content and scenarios, and regressions for passing the full content (`improve/skills-procedures`; evidence in ROADMAP).
- [ ] Acceptance of the improved Skills procedures in a real conversation on CachyOS after CI of the specific head.
- [x] Longer wait limit at the highest reasoning level and a message saying that it is about time.
- [x] `task rollback` and `task status` list from the newest entries, with a date. Today the order comes from the plan's hash, so it is easy to pick the wrong task.
- [x] A clear error when `--workspace` points to a file instead of a directory.
- [x] The interface, settings, docs and Skills are in English, and Claude answers in the language of the question.
- [ ] `ai-desktop task prune`: shows old finished entries, asks for consent and only then removes them. The limit is 128 journals per directory, and once it is reached writing is blocked.

## Security
- [x] `material` and `project`: re-check the policy before each handover of data (test on a real system before merging).
- [x] Writing an existing file does not overwrite a concurrent editor save (`RENAME_EXCHANGE` swap with comparison; test on a real system before merging).
- [x] `commands.change`: if consent expires or the policy changes during a command, the result is shown and recorded, and the program gives the test and the undo to run by hand.
- [x] Wrong client sum: README does not run the file, and `PKGBUILD` refuses to build.
- [x] `ConversationConsent`: consent for a conversation covers the whole `permissions.toml`.
- [x] Conversation memory after access is revoked: controller facts lose their details when a turn depended on a revoked file.
- [ ] Cut off the host's Unix sockets (D-Bus, Docker) in the sandbox for read commands, without giving commands network access.
- [ ] Check a general system call filter for the client.
- [x] Pin the Claude client with a checksum when the package is built.
- [ ] Check commands with real Bubblewrap in CI; today these tests are skipped on the runner.
- [ ] Check Anthropic's terms for a program that runs Claude Code with a subscription login.
- [ ] Independent security review before version 1.0.

## Functional bugs
- [x] The edit stage in `ask` gets what was settled earlier in the conversation, the memory, command results and Skills.
- [x] The same in `project`.
- [x] Saving a conversation with many dependent files: turns that do not fit in the source table are redacted instead of the save being refused.
- [x] Trash: the 129th deletion is refused before it runs.
- [x] A Skill of the largest allowed size can be assigned to an agent.
- [ ] Codex: a correct end of login/logout is sometimes reported as an error (`not-found` with code 1).
- [ ] Acceptance scripts: `update_candidate.fish` requires `0.1.0-17`; some scripts refer to branches that do not exist.
- [ ] Documentation: `systemctl status` shows journal entries, while README claims that the default commands do not send logs; README wrongly describes how a conversation ends after a timeout.

## To check live
- [ ] Installation according to README on a clean CachyOS or Arch system.
- [ ] Deleting to the trash and `task restore`.
- [ ] `/log` in `ai-desktop project` after an edit.
- [ ] Real failures: login expiry, no network, service limit.
- [ ] Codex on a real account.

## Code cleanup
- [ ] Merge the duplicated `material` and `project` flows.
- [ ] Split long functions, including `ask_cli.main` and `assistant_flow._run_once`.
- [ ] Unify text checking; today different modules have different rules.
- [ ] Remove from the package the modules not used by the installed program, and the old acceptance scripts `scripts/accept_*`.
- [ ] Hide or remove the internal subcommands visible in `--help-all`.

## Possible improvements
- [ ] Skills on demand also in `material`, `project` and `task`.
- [x] A Skill loaded in a conversation available right away in later questions, without an extra model call.
- [ ] Discovery command results in the edit request.
- [ ] Streaming answers.
- [ ] One client session and one client process for the whole conversation, with a lifetime limit.
