# Release checklist

For project maintainers. Tick items for a specific commit: the result of another commit attests nothing.

## Version

- [ ] The `pkgver-pkgrel` number in `packaging/arch/PKGBUILD` is new.
- [ ] The same number is in README (the line about the current package and the file name in the install block), at the top of `CHANGELOG.md`,
      in `scripts/accept_web_candidate.fish` and in `tests/host_analysis_acceptance.py`.
- [ ] During the 0.1.0-79 transition, the documented legacy client version matches the adapter and the documented checksum matches the private build path. Do not remove working installation instructions until replacement runtime acceptance is complete.
- [ ] For the system-client migration, document the supported system paths and tested adapter compatibility. Candidate 0.1.0-80 discovery must remain metadata-only: no client execution, version/protocol check or automatic activation. Do not call a discovered executable runtime-ready before the runtime uses the verified identity.
- [ ] The entry in `CHANGELOG.md` describes the changes in plain language.

## Tests

- [ ] `python3 -B scripts/verify.py --mode ci --fail-fast` passes on this commit.
- [ ] GitHub Actions are green on this commit.
- [ ] `python3 -B scripts/verify.py --mode local --fail-fast` passes on CachyOS or Arch.
- [ ] Any change to isolation, permissions, the file system, network or login has been checked on a real system.

## Package

- [ ] Build in a clean checkout: `AI_DESKTOP_PUBLIC_ARTIFACT=1 makepkg --cleanbuild` in `packaging/arch`. This variable refuses
      to build when a client file is next to it.
- [ ] The package for distribution does not contain `claude-code` or `codex`.
- [ ] Install, update and `pacman -R` done according to README; the list of remaining data is correct.
- [ ] `ai-desktop doctor`, `ai-desktop status` and one real question through `ai-desktop ask` work.

## System-client and task-flow migration gates

Client gates apply before switching the runtime (S8); the consent-flow gate applies when implementing S9. Discovery/preflight alone cannot close either scope.

- [ ] Claude-only and Codex-only installations work independently; neither pulls in the other client.
- [ ] The distributed application package contains no vendor executable and performs no hidden client download.
- [ ] Unsafe ownership/modes, path substitution, wrappers and incompatible or unknown versions fail before model data or credentials are released.
- [ ] The approved task uses one verified executable identity; replacement or an update during the task refuses further execution.
- [ ] A tested compatible client update between tasks works without rebuilding AI Desktop.
- [ ] Separate authentication, restricted tools, model transport, namespace and egress checks pass through the actual migrated runtime on CachyOS.
- [ ] A declared plan/stage/copy-test approval cannot authorize an undeclared operation; the final diff still requires a separate write decision.
- [ ] Cleanup removes only proven-unused experiments. Production modules, fixtures and checks are not removed merely because their names start with `e2_`.

## Repository content

- [ ] No secrets, login files, client binaries or data about a specific computer or person.
- [ ] Links in the documentation lead to existing files.
- [ ] `docs/KNOWN_LIMITATIONS.md`, `SECURITY.md` and `docs/THREAT_MODEL.md` describe the current behavior.
- [ ] Before distribution, the Claude Code terms and the subscription usage rules have been checked.

## Publishing

- [ ] A version below 1.0 is marked as a development version.
- [ ] The release tag and description point to the exact commit.
