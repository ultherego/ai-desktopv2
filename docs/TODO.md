# Do zrobienia

Stan: pakiet `0.1.0-70`. Ograniczenia obecnej wersji opisuje [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## Najbliżej
- [x] Lista `task rollback` i `task status` od najnowszych wpisów, z datą. Dziś kolejność wynika ze skrótu planu, więc łatwo wybrać nie to zadanie.
- [x] Jasny błąd, gdy `--workspace` wskazuje plik zamiast katalogu.
- [x] Polskie znaki w komunikatach i pomocy widocznych dla użytkownika (`task`, `cli`, `analyze`, zgody).
- [ ] Polskie znaki w pozostałych miejscach: strona logowania Codex (`codex_admin.py`), komunikaty `executor`/`workspace`, instrukcje dla modelu w `analysis_contract.py` (zmiana wymaga ponownej oceny odpowiedzi modelu).
- [ ] `ai-desktop task prune`: pokazuje stare zakończone wpisy, pyta o zgodę i dopiero wtedy je usuwa. Limit to 128 dzienników na katalog, a po jego osiągnięciu zapis jest blokowany.

## Bezpieczeństwo
- [x] `material` i `project`: ponowne sprawdzenie polityki przed każdym przekazaniem danych (test na prawdziwym systemie przed scaleniem).
- [x] Zapis istniejącego pliku nie nadpisuje równoczesnego zapisu edytora (wymiana `RENAME_EXCHANGE` z porównaniem; test na prawdziwym systemie przed scaleniem).
- [x] `commands.change`: gdy zgoda wygaśnie lub polityka się zmieni w trakcie polecenia, wynik jest pokazany i odnotowany, a program podaje test i wycofanie do uruchomienia ręcznie.
- [ ] README: blok instalacji nie zatrzymuje się po błędnej sumie klienta, a `PKGBUILD` sprawdza tylko nagłówek ELF, nie sumę. Dodać sprawdzenie sumy w `PKGBUILD` i `set -e`/`and` w instrukcji.
- [ ] `ConversationConsent`: wiązanie zgody pomija `deny`, część limitów i `share_processes`.
- [ ] Pamięć rozmowy po cofnięciu dostępu: zredagowana odpowiedź i argumenty poleceń mogą przenosić treść cofniętego pliku.
- [ ] Odciąć w piaskownicy poleceń odczytu gniazda Unix hosta (D-Bus, Docker), bez dawania poleceniom sieci.
- [ ] Sprawdzić ogólny filtr wywołań systemowych dla klienta.
- [ ] Przypiąć klienta sumą kontrolną pliku, nie tylko zgłaszaną wersją.
- [ ] Sprawdzić w CI polecenia z prawdziwym Bubblewrap; dziś te testy są pomijane na runnerze.
- [ ] Sprawdzić warunki Anthropic dla programu uruchamiającego Claude Code z logowaniem subskrypcyjnym.
- [ ] Niezależny przegląd bezpieczeństwa przed wersją 1.0.

## Błędy funkcjonalne
- [ ] Etap edycji (`ask_edit`, `project`) nie dostaje wcześniejszych ustaleń rozmowy, tylko samo „zrób to".
- [ ] Zapis rozmowy odmawia przy 33 zależnych plikach (limit 32), a odczyt dopuszcza 64.
- [ ] Kosz: 129. usunięcie się wykonuje, a potem lista historii kosza przestaje działać.
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
- [ ] Wyniki poleceń rozpoznania w żądaniu edycji.
- [ ] Strumieniowanie odpowiedzi.
- [ ] Jedna sesja i jeden proces klienta na całą rozmowę, z limitem czasu życia.
