# Znane ograniczenia

AI Desktop jest wersją rozwojową, nie wydaniem 1.0. Testy automatyczne nie dowodzą izolacji; ta lista mówi, czego jeszcze nie ma lub co nie zostało sprawdzone.

## Bezpieczeństwo
- Piaskownica poleceń odczytu nie izoluje gniazd Unix hosta. Szablon w `[commands] allow`, który może zmieniać stan przez D-Bus lub Dockera, nie jest zatrzymany przez samą piaskownicę, a takie polecenia działają bez pytania. Dotyczy to np. `docker`, który działa z piaskownicy i sięga do demona na hoście, dlatego nie ma go w szablonie. Wpisuj do `allow` tylko polecenia wyłącznie do odczytu.
- Treść pliku lub wynik polecenia może skłonić model do kolejnych dozwolonych odczytów, a ich wyniki trafiają do dostawcy. Granicą są `permissions.toml` i budżety na pytanie.
- Klient Claude Code ma dostęp do własnego profilu logowania w osobnym koncie `ai-desktop`. Nie ma ogólnego filtra wywołań systemowych dla klienta.
- Suma SHA-256 klienta Claude jest sprawdzana przy budowie pakietu. Potem, przy każdym użyciu, program sprawdza już tylko zgłaszaną wersję pliku należącego do roota. Klient Codex ma sprawdzaną sumę archiwum przy pobieraniu przez `scripts/fetch_codex_client.py`, a `PKGBUILD` sprawdza u niego tylko nagłówek pliku.
- Nie ma ochrony przed wrogim procesem tego samego użytkownika ani gwarancji przy utracie zasilania.

## Zachowanie
- Zapis istniejącego pliku wymienia go atomowo i odrzuca zapis, gdy edytor zmienił plik po ostatnim sprawdzeniu. Zapis do pliku, który edytor już trzymał otwarty, może jeszcze trafić do starej wersji tuż po porównaniu, zanim zostanie usunięta. Wymaga systemu plików z `RENAME_EXCHANGE` (Btrfs, ext4, xfs, tmpfs); na innych zapis jest odrzucany.
- Gdy zgoda wygaśnie lub uprawnienia zmienią się podczas polecenia z `[commands] change`, program nie uruchamia testu ani wycofania: pokazuje je do uruchomienia ręcznie. Taka tura kończy się odmową, więc wynik zostaje na ekranie, ale może nie trafić do pamięci rozmowy.
- Ucięty dziennik zadania blokuje zapis w danym katalogu do czasu ręcznego sprawdzenia. Kosz zadania nie ma automatycznego czyszczenia, a limit to 128 wpisów na katalog.
- Nazwa pliku w `deny` musi być zapisana tak samo jak na dysku: reguły nie normalizują znaków Unicode.
- W jednym pytaniu żądanie edycji nie zawiera wyników wcześniejszych poleceń, a edytowane pliki mają limit 64 KiB (razem 128 KiB).
- Polecenia działają w piaskownicy bez sieci i sesji użytkownika, więc np. `ip`, `wpctl` i `glxinfo` nie widzą stanu hosta.

## Niesprawdzone
- Wygaśnięcie i odświeżenie logowania oraz rzeczywiste awarie sieci z oficjalnym klientem.
- Niezależny przegląd bezpieczeństwa i instalacja na świeżym systemie.
- Warunki Anthropic dla programu uruchamiającego Claude Code z logowaniem subskrypcyjnym; korzystasz z klienta na własnym koncie i według własnej oceny.
