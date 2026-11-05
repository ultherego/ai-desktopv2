# Release checklist

For project maintainers. Tick items for a specific commit: the result of another commit attests nothing.

## Version

- [ ] The `pkgver-pkgrel` number in `packaging/arch/PKGBUILD` is new.
- [ ] The same number is in README (the line about the current package and the file name in the install block), at the top of `CHANGELOG.md`,
      in `scripts/accept_web_candidate.fish` and in `tests/host_analysis_acceptance.py`.
- [ ] The client version in README and in `ai_desktop/analysis_contract.py` is the same, and the checksum in README and `PKGBUILD` is the same and matches the downloaded file.
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

## Repository content

- [ ] No secrets, login files, client binaries or data about a specific computer or person.
- [ ] Links in the documentation lead to existing files.
- [ ] `docs/KNOWN_LIMITATIONS.md`, `SECURITY.md` and `docs/THREAT_MODEL.md` describe the current behavior.
- [ ] Before distribution, the Claude Code terms and the subscription usage rules have been checked.

## Publishing

- [ ] A version below 1.0 is marked as a development version.
- [ ] The release tag and description point to the exact commit.
