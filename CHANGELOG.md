# Historia zmian

## Niewydane

- `task rollback` nie nadpisuje ani nie usuwa nowszego zapisu edytora: zostawia go i zgłasza konflikt. Wcześniej mógł zgłosić sukces po utracie tej wersji.
- Polecenie zmiany może być długie jak wiadomość w `ask` (128 KiB; było 2 KiB), także w `task run` i `project`.
- Spacja przed `/` wysyła tekst jako pytanie. Pełny kosz odmawia przed usunięciem, a lista odzyskiwania nadal działa.
- `ai-desktop ask`: zgoda na rozmowę obejmuje cały `permissions.toml`. Usunięcie reguły `deny`, zmiana limitu albo `share_processes` wymaga nowej zgody. Wcześniej stara zgoda nadal obowiązywała.
- Instalacja: `PKGBUILD` sprawdza sumę SHA-256 klienta Claude i odmawia budowy przy złym pliku. Blok w README nie uruchamia pliku przy błędnej sumie.
- Nowe Skille `arch-*` oparte na Arch Wiki: pacman, systemd, Btrfs, amdgpu, dźwięk, sieć, Bluetooth, zram, Docker.
- `ai-desktop ask`: Skille ładowane na żądanie. Przy każdym pytaniu idą tylko nazwy i opisy; treść idzie wtedy, gdy model o nią poprosi, a program to pokazuje. Skille CachyOS zaczynają się teraz od procedury.
- Rozumowanie `xhigh` i `max`: model może odpowiadać do 15 minut (było 260 s). Gdy czas minie, komunikat mówi o czasie, a nie o wygasłej zgodzie.
- Za duża wklejka jest odrzucana w całości. Wcześniej jej reszta mogła trafić do następnego wiersza.
- `ai-desktop ask`: wiadomość do 128 KiB razem z wklejkami (było 2 KiB), pamięć 32 ostatnich wymian (było 8), odpowiedź w historii do 8 KiB (było 3 KiB). Za duża wklejka nie kończy już rozmowy. `material` i `project` zachowują swoje limity.
- `ai-desktop ask`: polecenia `/` w trakcie rozmowy, z listą pod wierszem (`/model`, `/sila`, `/skills`, `/agent`, `/ustawienia`, `/uprawnienia`, `/pomoc`, `/koniec`). Menu zaczyna na bieżącej wartości, a długie listy mają strony. Zmiana nie kasuje rozmowy.
- Zapisana rozmowa jest związana z usługą, agentem i poziomem dostępu. Zmiana modelu, rozumowania albo treści Skilla nie odcina jej już od wznowienia.
- Nowe Skille dla CachyOS, każdy o jednej rzeczy: pakiety, jądra, sched-ext, rozruch i migawki, sprzęt, gry oraz rozbudowana diagnostyka. Oparte na wiki CachyOS. Skill `desktop-documentation` kieruje do dokumentacji pulpitu zamiast zgadywać.
- Skille: bez limitu liczby na agenta, do 96 KiB na Skill i 104 KiB razem. Górny limit wynika z ograniczenia długości argumentu w Linuksie.
- **Bezpieczeństwo:** gdy zgoda wygaśnie lub `permissions.toml` zmieni się podczas polecenia z `[commands] change`, wynik wykonanej zmiany jest pokazany i odnotowany, a program mówi wprost, że zmiana została wykonana, i podaje polecenie testu i wycofania. Kolejne kroki nie są uruchamiane i nic więcej nie jest wysyłane. Wcześniej wynik ginął bez śladu.
- `ai-desktop task`/`ask`/`project`: zapis zmiany w istniejącym pliku nie nadpisuje już zapisu zrobionego w tym samym czasie przez edytor. Plik jest wymieniany atomowo (`renameat2` z `RENAME_EXCHANGE`), a wymieniona wersja jest porównywana z tym, co widział plan. Przy różnicy zamiana jest cofana i zapis odrzucany, a wersja edytora zostaje.
- **Bezpieczeństwo:** `material` i `project` sprawdzają `permissions.toml` przed każdym przekazaniem danych do Claude. Zmiana pliku, wyłączenie `network.claude` albo cofnięcie dostępu do czytanych plików zatrzymuje kolejne żądania, także w analizie całego pliku. Wcześniej wystarczyło, że zgoda i pliki zostały wczytane na początku sesji.
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
