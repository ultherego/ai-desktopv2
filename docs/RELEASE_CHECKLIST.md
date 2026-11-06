# Release checklist

For project maintainers. Tick items for a specific commit: the result of another commit attests nothing.

Candidate 82 evidence: on `0f0bcb6a0b77e52fcf29b46d4f8e1c56d112659f`, CachyOS CI/LOCAL, upgrade 81 to 82, retained Claude registration, complete Codex registration and both native offline probes passed. All five GitHub jobs also passed for this SHA. These results did not exercise login, token renewal or model inference; see [ROADMAP.md](ROADMAP.md) for scope and later changes. Unchecked wider release requirements below remain open.

PR #20 subsequently merged as `700a609` after the candidate-83 widget/quota flow was accepted; its own main/push CI passed five jobs. Candidate 84 passed focused offline CachyOS CI/LOCAL, build, upgrade and installed-file checks on `457118d5e3b1d5224ecdb00b88a847ab6c7ccc65`, which also passed five GitHub jobs. Its new mandatory host scenario exercises a real Python copy test, scoped approval, write and separate rollback with a synthetic provider. Go/npm execution is proved separately by the real CI fixtures; the host summary alone does not prove optional toolchains ran. Explicit PR #21 merge approval remains outstanding. It does not require repeating unchanged native client registration/compatibility checks.

## Version

- [ ] The `pkgver-pkgrel` number in `packaging/arch/PKGBUILD` is new.
- [ ] The same number is in README (the line about the current package), at the top of `CHANGELOG.md`,
      in `scripts/accept_web_candidate.fish` and in `tests/host_analysis_acceptance.py`.
- [ ] Document the supported native system, official installer, HOME and npm layouts plus registration/refresh behavior, minimum versions, tested baselines and capability requirements. Ordinary `doctor` remains metadata-only; `doctor --clients` must remain an explicit selected-provider offline probe without account access.
- [ ] New runtime acceptance covers upgrade from package 79/80 and confirms that existing accounts/data survive; do not apply the old host acceptance to the migrated runtime.
- [ ] The entry in `CHANGELOG.md` describes the changes in plain language.

## Tests

- [ ] `python3 -B scripts/verify.py --mode ci --fail-fast` passes on this commit.
- [ ] GitHub Actions are green on this commit.
- [ ] `python3 -B scripts/verify.py --mode local --fail-fast` passes on CachyOS or Arch.
- [ ] Any change to isolation, permissions, the file system, network or login has been checked on a real system.

## Package

- [ ] Build in a clean checkout with `makepkg --cleanbuild` in `packaging/arch`; the same application package is built for every provider selection. Legacy ignored client files must not be read, packaged or used.
- [ ] The package for distribution does not contain `claude-code` or `codex`.
- [ ] Install, update and `pacman -R` done according to README; the list of remaining data is correct.
- [ ] `ai-desktop doctor`, `ai-desktop status` and one real question through `ai-desktop ask` work.

## System-client and task-flow migration gates

Client gates apply before merging/releasing the migrated runtime (S8); the consent-flow gate applies when implementing S9. Metadata-only discovery and a successful compatibility probe alone cannot close either scope.

- [ ] Claude-only and Codex-only installations work independently; neither pulls in the other client. Test system packages, official curl installers, current npm, custom native paths and local-only setup before provider installation.
- [ ] Registration/refresh approval binds the complete payload digest; the fixed administrator helper does not execute the source. Source mutation, symlink retargeting, truncated copies and tampered metadata fail closed. An unapproved source update does not replace registered bytes.
- [ ] System-wide registration priority, explicit removal and retained original client/account data are covered in installation and upgrade acceptance.
- [ ] The distributed application package contains no vendor executable and performs no hidden client download.
- [ ] Unsafe ownership/modes, path substitution, unrecognized wrappers, unsupported version families and incompatible capabilities fail before model data or credentials are released.
- [ ] The approved operation uses one bound executable and client-private resource snapshot; replacement or an update during the operation refuses further phases. Test symlink retargeting and in-place changes, not just an atomic file rename.
- [ ] A tested compatible client update between tasks works without rebuilding AI Desktop; registered clients use an explicit approved refresh.
- [x] On candidate-82 head `0f0bcb6`, native Claude 2.1.319 rejects the appended invalid option after the full analyze/web option sets, demonstrates first-error ordering with two invalid options, and independently returns signed-out `auth status` JSON in an empty offline profile. Print and auth modes are not combined; help-only or synthetic parser tests are not this native evidence.
- [ ] Separate authentication, restricted tools, model transport, namespace and egress checks pass through the actual migrated runtime on CachyOS.
- [ ] A declared plan/stage/copy-test approval cannot authorize an undeclared operation; the final diff still requires a separate write decision.
- [x] Candidate 84 (offline acceptance on `457118d`): one project approval covers only the declared stage/request/data/time budgets; a changed file (including within a stage), manifest, policy, recipient or expired scope stops the next transfer or automatic edit handoff. Ctrl+C stops continuation.
- [x] Candidate 84 (offline acceptance on `457118d`): standalone task approval shows the selected workspace/files and exact test before execution; Go/npm run once without a duplicate prompt. Changed scope/recipe, replay and failure refuse, while the full diff, write refusal and rollback remain independent. Ordinary ask/project-edit test approvals are unchanged.
- [ ] Cleanup removes only proven-unused experiments. Production modules, fixtures and checks are not removed merely because their names start with `e2_`.

## Repository content

- [ ] No secrets, login files, client binaries or data about a specific computer or person.
- [ ] Links in the documentation lead to existing files.
- [ ] `docs/KNOWN_LIMITATIONS.md`, `SECURITY.md` and `docs/THREAT_MODEL.md` describe the current behavior.
- [ ] Before distribution, the Claude Code terms and the subscription usage rules have been checked.

## Publishing

- [ ] A version below 1.0 is marked as a development version.
- [ ] The release tag and description point to the exact commit.
