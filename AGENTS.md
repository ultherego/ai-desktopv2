# AGENTS.md: rules for assistants developing this repository

These are instructions for developing this code, not for making changes on someone's computer.

- **Security:** [SECURITY.md](SECURITY.md) and [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). Green CI is not a certificate of isolation. Record limitations in [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). Stages are in [docs/ROADMAP.md](docs/ROADMAP.md), and tasks in [docs/TODO.md](docs/TODO.md): tick them off in the same PR.
- **Tests:** every function has tests in `tests/` and passes `scripts/verify.py --mode ci`.
- **Changes to isolation, permissions, the file system, the network or login** require a test on real CachyOS/Arch before merging.
- **No data about a specific computer or person** in code, Skills, tests or documentation. The user's own Skills are in `~/.config/ai-desktop/skills/`.
- **No secrets, client binaries or login data** in the repository.
- **Writes and system changes** always go through the user's consent; the model does not get arbitrary tools.
