# Historia zmian

## Niewydane

- Polskie znaki w komunikatach i pomocy: pytania o zapis i cofnięcie, `task`, `analyze`, `status` i opisy poleceń.
- `ai-desktop task`: gdy `--workspace` wskazuje plik, program mówi to wprost zamiast ogólnej odmowy.
- `ai-desktop task status` i `task rollback` wypisują wpisy od najnowszego, z datą ostatniej zmiany (czas pliku dziennika). Numer 1 to najnowsze zadanie.

## 0.1.0-70 — poprawki

- `ai-desktop sessions`: uszkodzony zapis rozmowy nie ukrywa pozostałych; `sessions list` go wskazuje, a `sessions remove` usuwa.
- `/rejestr` w `project` pokazuje też pliki przekazane do Claude przy edycji.

## 0.1.0-69 — pierwsze wydanie publiczne (wersja rozwojowa)

- `ai-desktop ask`: jedna zgoda na rozmowę, odczyty i polecenia z `permissions.toml` bez dodatkowych pytań, zapis i usuwanie po pełnym diffie lub liście plików i osobnej zgodzie, cofanie zmian.
- `ai-desktop material` do dużych plików i `ai-desktop project` do katalogów projektów.
- Oficjalny klient Claude Code w osobnym koncie systemowym i piaskownicy; opcjonalnie Codex.
- Ogólne Skille w pakiecie, własne w `~/.config/ai-desktop/skills/`.
- Szablon `permissions.toml` zawiera tylko ogólne polecenia, które działają w piaskownicy i nie wysyłają logów ani nazw sieci.
