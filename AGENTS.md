# AGENTS.md: rules for assistants developing this repository

These are instructions for developing this code, not for making changes on someone's computer.

- **Security:** [SECURITY.md](SECURITY.md) and [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). Green CI is not a certificate of isolation. Record limitations in [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). [docs/ROADMAP.md](docs/ROADMAP.md) is the only roadmap and progress record: update the existing criterion and progress log in the same PR. Do not create a competing TODO/status inventory.
- **Tests:** every function has tests in `tests/` and passes `scripts/verify.py --mode ci`.
- **Changes to isolation, permissions, the file system, the network or login** require a test on real CachyOS/Arch before merging.
- **No data about a specific computer or person** in code, Skills, tests or documentation. The user's own Skills are in `~/.config/ai-desktop/skills/`.
- **No secrets, client binaries or login data** in the repository.
- **Writes and system changes** always go through the user's consent; the model does not get arbitrary tools.
- **Client delivery decision:** migrate to independently installed system Claude Code or Codex, with shared trusted-path discovery and provider-specific compatibility checks. Do not introduce a second bundled/pinned delivery mode. During migration, distinguish the implemented diagnostic foundation from the still-active packaged-client runtime; remove legacy client preparation only with the complete tested replacement.
- **Client trust:** never select an executable from ambient PATH, a user-writable location or a model-supplied path. Metadata, provenance, version compatibility and successful isolation are separate facts. Bind a verified client and its required resources through all phases of a task; an update must not silently change the executable between checks and credential use. Keep the separate login and existing sandbox boundaries.
- **Usability:** reuse consent within the explicitly approved scope; final host writes still require the exact diff/plan approval. Finish persistence, task continuity and the shared Skills/documentation flow before optional desktop features. The package target is one general package selecting Skills for all installed environments, never a DMS-only package split.
- **Cleanup:** identify production imports before moving old `e2_*` experiments. Several helpers still enforce active runtime boundaries. Preserve meaningful regressions while separating fixtures from production code.
