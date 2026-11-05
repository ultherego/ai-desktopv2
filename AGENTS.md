# AGENTS.md — zasady dla asystentów rozwijających repozytorium

To instrukcja dla rozwoju tego kodu, nie do wykonywania zmian na czyimś komputerze.

- **Bezpieczeństwo:** [SECURITY.md](SECURITY.md) i [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). Zielone CI nie jest certyfikatem izolacji. Ograniczenia zapisuj w [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). Etapy są w [docs/ROADMAP.md](docs/ROADMAP.md), a zadania w [docs/TODO.md](docs/TODO.md): odhaczaj je w tym samym PR.
- **Testy:** każda funkcja ma testy w `tests/` i przechodzi `scripts/verify.py --mode ci`.
- **Zmiany izolacji, uprawnień, systemu plików, sieci lub logowania** wymagają testu na prawdziwym CachyOS/Arch przed scaleniem.
- **Żadnych danych konkretnego komputera ani osoby** w kodzie, Skillach, testach i dokumentacji. Własne Skille użytkownika są w `~/.config/ai-desktop/skills/`.
- **Żadnych sekretów, binarek klienta ani danych logowania** w repozytorium.
- **Zapis i zmiany systemu** zawsze przechodzą przez zgodę użytkownika; model nie dostaje arbitralnych narzędzi.
