# Do zrobienia

Stan: pakiet `0.1.0-70`. Ograniczenia obecnej wersji opisuje [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## Najbliżej
Etapy i warunki ich ukończenia opisuje [ROADMAP.md](ROADMAP.md).
- [x] `task rollback`: nie nadpisywać ani nie usuwać nowszego zapisu edytora.
- [x] Długie polecenie zmiany: etap edycji odrzuca instrukcję powyżej 2 KiB, choć `ask` przyjmuje 128 KiB.
- [x] Spacja przed `/` jest usuwana, więc słowa z `/` nie da się wysłać jako pytania.
- [x] Końcowa odpowiedź po odczycie pliku dostaje wyniki poleceń i wczytane Skille.
- [x] Pliki z BOM i końcami linii Windows dają się czytać i edytować bez zmiany formatu.
- [x] Wynik wykonanej zmiany zostaje w pamięci mimo błędu usługi.
- [x] Zależność od cofniętego pliku nie przechodzi na niezależne odpowiedzi.
- [x] Zgodne limity szablonów `change` (polityka i kontrakt: 128).
- [x] Wartość opcji nie jest brana za polecenie w szablonach (`systemctl -H host <word>` jest odrzucany).
- [ ] Skill przy pytaniu ogólnym bez `permissions.toml`.
- [x] Dłuższy limit oczekiwania przy najwyższym rozumowaniu i komunikat mówiący, że chodzi o czas.
- [x] Lista `task rollback` i `task status` od najnowszych wpisów, z datą. Dziś kolejność wynika ze skrótu planu, więc łatwo wybrać nie to zadanie.
- [x] Jasny błąd, gdy `--workspace` wskazuje plik zamiast katalogu.
- [x] Polskie znaki w komunikatach i pomocy widocznych dla użytkownika (`task`, `cli`, `analyze`, zgody).
- [ ] Polskie znaki w pozostałych miejscach: strona logowania Codex (`codex_admin.py`), komunikaty `executor`/`workspace`, instrukcje dla modelu w `analysis_contract.py` (zmiana wymaga ponownej oceny odpowiedzi modelu).
- [ ] `ai-desktop task prune`: pokazuje stare zakończone wpisy, pyta o zgodę i dopiero wtedy je usuwa. Limit to 128 dzienników na katalog, a po jego osiągnięciu zapis jest blokowany.

## Bezpieczeństwo
- [x] `material` i `project`: ponowne sprawdzenie polityki przed każdym przekazaniem danych (test na prawdziwym systemie przed scaleniem).
- [x] Zapis istniejącego pliku nie nadpisuje równoczesnego zapisu edytora (wymiana `RENAME_EXCHANGE` z porównaniem; test na prawdziwym systemie przed scaleniem).
- [x] `commands.change`: gdy zgoda wygaśnie lub polityka się zmieni w trakcie polecenia, wynik jest pokazany i odnotowany, a program podaje test i wycofanie do uruchomienia ręcznie.
- [x] Błędna suma klienta: README nie uruchamia pliku, a `PKGBUILD` odmawia budowy.
- [x] `ConversationConsent`: zgoda na rozmowę obejmuje cały `permissions.toml`.
- [x] Pamięć rozmowy po cofnięciu dostępu: fakty kontrolera tracą szczegóły, gdy tura zależała od cofniętego pliku.
- [ ] Odciąć w piaskownicy poleceń odczytu gniazda Unix hosta (D-Bus, Docker), bez dawania poleceniom sieci.
- [ ] Sprawdzić ogólny filtr wywołań systemowych dla klienta.
- [x] Przypiąć klienta Claude sumą kontrolną przy budowie pakietu.
- [ ] Sprawdzić w CI polecenia z prawdziwym Bubblewrap; dziś te testy są pomijane na runnerze.
- [ ] Sprawdzić warunki Anthropic dla programu uruchamiającego Claude Code z logowaniem subskrypcyjnym.
- [ ] Niezależny przegląd bezpieczeństwa przed wersją 1.0.

## Błędy funkcjonalne
- [x] Etap edycji w `ask` dostaje wcześniejsze ustalenia rozmowy, pamięć, wyniki poleceń i Skille.
- [ ] To samo w `project`.
- [x] Zapis rozmowy z wieloma zależnymi plikami: tury, które nie mieszczą się w tabeli źródeł, są redagowane zamiast odmowy zapisu.
- [x] Kosz: 129. usunięcie jest odrzucane przed wykonaniem.
- [x] Skill o największym dopuszczalnym rozmiarze można przypisać do agenta.
- [ ] Codex: poprawne zakończenie logowania/wylogowania bywa zgłaszane jako błąd (`not-found` z kodem 1).
- [ ] Skrypty odbioru: `update_candidate.fish` wymaga `0.1.0-17`; część skryptów odwołuje się do nieistniejących gałęzi.
- [ ] Dokumentacja: `systemctl status` pokazuje wpisy dziennika, a README twierdzi, że domyślne polecenia nie wysyłają logów; README błędnie opisuje zakończenie rozmowy po timeoucie.

## Do sprawdzenia na żywo
- [ ] Instalacja według README na czystym systemie CachyOS lub Arch.
- [ ] Usuwanie do kosza i `task restore`.
- [ ] `/rejestr` w `ai-desktop project` po edycji.
- [ ] Prawdziwe awarie: wygaśnięcie logowania, brak sieci, limit usługi.
- [ ] Codex na prawdziwym koncie.

## Porządki w kodzie
- [ ] Połączyć powielone przepływy `material` i `project`.
- [ ] Podzielić długie funkcje, m.in. `ask_cli.main` i `assistant_flow._run_once`.
- [ ] Ujednolicić sprawdzanie tekstu; dziś różne moduły mają różne zasady.
- [ ] Usunąć z pakietu moduły nieużywane przez zainstalowany program oraz stare skrypty odbioru `scripts/accept_*`.
- [ ] Ukryć lub usunąć wewnętrzne podkomendy widoczne w `--help-all`.

## Możliwe usprawnienia
- [ ] Skille na żądanie także w `material`, `project` i `task`.
- [ ] Skill wczytany w rozmowie dostępny od razu w kolejnych pytaniach, bez dodatkowego wywołania modelu.
- [ ] Wyniki poleceń rozpoznania w żądaniu edycji.
- [ ] Strumieniowanie odpowiedzi.
- [ ] Jedna sesja i jeden proces klienta na całą rozmowę, z limitem czasu życia.
