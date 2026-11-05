# Known limitations

AI Desktop is a development version, not a 1.0 release. Automated tests do not prove isolation; this list says what is still missing or has not been checked.

## Security
- The sandbox for read commands does not isolate the host's Unix sockets. A template in `[commands] allow` that can change state through D-Bus or Docker is not stopped by the sandbox alone, and such commands run without asking. This applies for example to `docker`, which runs from the sandbox and reaches the daemon on the host, so it is not in the template. Put only strictly read-only commands in `allow`.
- A file's content or a command's output can lead the model to further allowed reads, and their results go to the provider. The boundary is `permissions.toml` and the per-question budgets.
- The Claude Code client has access to its own login profile in the separate `ai-desktop` account. There is no general system call filter for the client.
- The SHA-256 sum of the Claude client is checked when the package is built. After that, on each use, the program only checks the reported version of the root-owned file. The Codex client has its archive sum checked when it is downloaded with `scripts/fetch_codex_client.py`, and `PKGBUILD` checks only the file header for it.
- There is no protection against a hostile process of the same user and no guarantee on power loss.
- The model does not see a BOM or Windows line endings, so it will not answer whether a file has them. A file with mixed line endings can be read but not edited, because the write would not reproduce its bytes. A file with a bare CR in the middle of the text is refused on read as well. The check on the copy works on plain text, not on the bytes with BOM and CRLF.

## Behavior
- Writing an existing file replaces it atomically and refuses the write if an editor changed the file after the last check. A write through a file the editor already held open can still land in the old version right after the comparison, before that version is removed. This requires a file system with `RENAME_EXCHANGE` (Btrfs, ext4, xfs, tmpfs); on others the write is refused.
- If consent expires or permissions change during a `[commands] change` command, the program does not run the test or the undo: it shows them for you to run by hand. Such a turn ends in a refusal, so the result stays on screen but may not reach the conversation memory.
- A truncated task journal blocks writing in that directory until it is checked by hand. The task trash has no automatic cleanup, and the limit is 128 entries per directory.
- A file name in `deny` must be written exactly as it is on disk: the rules do not normalize Unicode characters.
- Within one question, the edit request does not contain the results of earlier commands, and edited files are limited to 64 KiB (128 KiB in total).
- Commands run in a sandbox without network or user session, so for example `ip`, `wpctl` and `glxinfo` do not see the host's state.

## Not checked
- Login expiry and refresh, and real network failures with the official client.
- An independent security review and installation on a fresh system.
- Anthropic's terms for a program that runs Claude Code with a subscription login; you use the client on your own account and at your own judgment.
