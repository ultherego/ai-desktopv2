# Do zrobienia

Stan: pakiet `0.1.0-70`. Ograniczenia obecnej wersji opisuje [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## Najbliżej
- [x] Lista `task rollback` i `task status` od najnowszych wpisów, z datą. Dziś kolejność wynika ze skrótu planu, więc łatwo wybrać nie to zadanie.
- [x] Jasny błąd, gdy `--workspace` wskazuje plik zamiast katalogu.
- [x] Polskie znaki w komunikatach i pomocy widocznych dla użytkownika (`task`, `cli`, `analyze`, zgody).
- [ ] Polskie znaki w pozostałych miejscach: strona logowania Codex (`codex_admin.py`), komunikaty `executor`/`workspace`, instrukcje dla modelu w `analysis_contract.py` (zmiana wymaga ponownej oceny odpowiedzi modelu).
- [ ] `ai-desktop task prune`: pokazuje stare zakończone wpisy, pyta o zgodę i dopiero wtedy je usuwa. Limit to 128 dzienników na katalog, a po jego osiągnięciu zapis jest blokowany.

## Bezpieczeństwo
- [ ] Odciąć w piaskownicy poleceń odczytu gniazda Unix hosta (D-Bus, Docker), bez dawania poleceniom sieci.
- [ ] Sprawdzić ogólny filtr wywołań systemowych dla klienta.
- [ ] Przypiąć klienta sumą kontrolną pliku, nie tylko zgłaszaną wersją.
- [ ] Sprawdzić w CI polecenia z prawdziwym Bubblewrap; dziś te testy są pomijane na runnerze.
- [ ] Sprawdzić warunki Anthropic dla programu uruchamiającego Claude Code z logowaniem subskrypcyjnym.
- [ ] Niezależny przegląd bezpieczeństwa przed wersją 1.0.

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
- [ ] Wyniki poleceń rozpoznania w żądaniu edycji.
- [ ] Strumieniowanie odpowiedzi.
- [ ] Jedna sesja i jeden proces klienta na całą rozmowę, z limitem czasu życia.
