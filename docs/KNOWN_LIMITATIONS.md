# Znane ograniczenia

AI Desktop jest wersją rozwojową, nie wydaniem 1.0. Testy automatyczne nie dowodzą izolacji; ta lista mówi, czego jeszcze nie ma lub co nie zostało sprawdzone.

## Bezpieczeństwo
- Piaskownica poleceń odczytu nie izoluje gniazd Unix hosta. Szablon w `[commands] allow`, który może zmieniać stan przez D-Bus lub Dockera, nie jest zatrzymany przez samą piaskownicę, a takie polecenia działają bez pytania. Wpisuj do `allow` tylko polecenia tylko do odczytu.
- Treść pliku lub wynik polecenia może skłonić model do kolejnych dozwolonych odczytów, a ich wyniki trafiają do dostawcy. Granicą są `permissions.toml` i budżety na pytanie.
- Klient Claude Code ma dostęp do własnego profilu logowania w osobnym koncie `ai-desktop`. Nie ma ogólnego filtra wywołań systemowych dla klienta.
- Przypięcie klienta polega na zgłaszanej wersji, nie na sumie kontrolnej pliku w pakiecie.
- Nie ma ochrony przed wrogim procesem tego samego użytkownika ani gwarancji przy utracie zasilania.

## Zachowanie
- Ucięty dziennik zadania blokuje zapis w danym katalogu do czasu ręcznego sprawdzenia. Kosz zadania nie ma automatycznego czyszczenia, a limit to 128 wpisów na katalog.
- Nazwa pliku w `deny` musi być zapisana tak samo jak na dysku: reguły nie normalizują znaków Unicode.
- W jednym pytaniu żądanie edycji nie zawiera wyników wcześniejszych poleceń, a edytowane pliki mają limit 64 KiB (razem 128 KiB).
- Polecenia działają w piaskownicy bez sieci i sesji użytkownika, więc np. `ip`, `wpctl` i `glxinfo` nie widzą stanu hosta.

## Niesprawdzone
- Wygaśnięcie i odświeżenie logowania oraz rzeczywiste awarie sieci z oficjalnym klientem.
- Niezależny przegląd bezpieczeństwa i instalacja na świeżym systemie.
- Warunki Anthropic dla programu uruchamiającego Claude Code z logowaniem subskrypcyjnym; korzystasz z klienta na własnym koncie i według własnej oceny.
