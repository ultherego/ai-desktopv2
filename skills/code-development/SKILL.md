---
name: code-development
description: Prepares a code fix with a justified test and verification. Use for fixing a bug, changing behavior or a "change and test" request in a shared project.
---

# Code change with verification

## Establish the contract

1. Derive the expected behavior from the task: a specific input, the current result and the required result. Keep what the conversation has established. Ask only when missing information changes the correctness or scope of the change.
2. Read the project rules, the manifest, the code being changed, its callers and the nearest tests. Project rules do not change the application's permissions. If a dependency or file is missing, point out this gap instead of assuming its content.
3. Establish the cause before the fix. If the available executor can reproduce the bug on a copy, check it. Otherwise describe the evidence from the code and mark the reproduction as not performed.

## Prepare the change

- Keep the existing API, data format and project style unless the task requires changing them. Fix the shared cause of the bug; check other callers of the same mechanism.
- Add a test of the specific input or risk that distinguishes the old behavior from the correct one. The test must check a result or an unwanted effect, not repeat the implementation. Include a denial or failure if the change touches an access or write boundary.
- Do not change test expectations just to get a PASS. Do not add an unrelated refactor or new dependencies without a need that follows from the task.

## Choose a real check

AI Desktop checks selected files on an isolated copy. Available:

| Check | What it can confirm |
|---|---|
| `python-unittest` | Tests found in the supplied copy, within the environment's limits. |
| `python-syntax`, `bash-syntax`, `fish-syntax` | The syntax of the respective language. |
| `json`, `toml` | Correct parsing of the given format. |
| `text` | UTF-8 integrity only; not syntax or behavior. |

A Python test cannot create processes, threads or sockets. The copy has no network, no HOME and none of the project that was not supplied. Do not claim to have run `pytest`, `go test`, `npm test` or a dependency installation: the current executor does not support these. A syntax result does not replace a behavior test. When a suitable check is missing, name the unverified part and the risk.

## Review and completion

Before writing, check the complete diff: the requirement, unintended changes, the test and preservation of the file format. The controller does the writing after separate consent. A denial or a failed check ends this attempt; do not get around them with another mode or a command on the host.

In the result, give: what was changed and why, the check actually run by the controller, its scope and outcome, the write state and the remaining limit. Quote the undo command from the controller's result; do not make up your own task identifier. Until a write is confirmed, describe the fix as a proposal.
