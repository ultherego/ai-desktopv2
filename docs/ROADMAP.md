# Plan do wersji 1.0

**Cel:** uruchamiasz program, rozmawiasz zwykłym językiem, diagnozujesz system i zlecasz zmiany, a każda zmiana czeka na Twoją zgodę i da się ją cofnąć. Przebieg, który ma działać w całości:

> przeanalizuj → ustalmy zmianę → zrób to → przetestuj → cofnij

Ta strona opisuje etapy i warunki ich ukończenia. Pojedyncze zadania są w [TODO.md](TODO.md), ograniczenia bieżącej wersji w [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md), a kroki wydania w [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).

## Jak liczy się ukończenie

Etap jest ukończony, gdy jego warunki są spełnione dla konkretnego commita. Wynik innego commita niczego nie poświadcza. Zielone CI nie wystarcza dla zmian izolacji, uprawnień, systemu plików, sieci i logowania: te wymagają też testu na prawdziwym CachyOS lub Arch, z zapisanym numerem commita.

Stan: **gotowe** (scalone), **w toku** (czeka na test lub scalenie), **do zrobienia**.

## Stan obecny

Działa i jest scalone:

- Rozmowa `ask` z jedną zgodą, 32 wymianami i pamięcią zadania, polecenia `/`, wiadomość do 128 KiB.
- Rozpoznanie katalogów, odczyt plików i polecenia zgodne z `permissions.toml`.
- Zapis po pokazaniu różnic, z testem na kopii, osobną zgodą i dziennikiem. Równoczesny zapis edytora nie jest nadpisywany przy zapisie.
- Skille na żądanie w `ask`, procedury dla CachyOS i Arch.
- Klient na osobnym koncie, w Bubblewrap, z wyjściem przez filtrujące proxy.
- Ochrona gałęzi `main`, pięć wymaganych zadań CI, przypięte akcje.

## Etap 1: bez utraty danych

| Zadanie | Stan |
|---|---|
| Cofanie zmiany nie nadpisuje ani nie usuwa nowszego zapisu edytora, zarówno przy edycji, jak i przy nowym pliku | gotowe |
| Suma SHA-256 klienta sprawdzana przy budowie pakietu | gotowe |
| Zgoda na rozmowę obejmuje cały `permissions.toml` | gotowe |
| Długie polecenie zmiany nie jest odrzucane na etapie edycji (`ask` przyjmuje 128 KiB, edycja 2 KiB) | gotowe |
| Słowo zaczynające się od `/` da się wysłać jako pytanie, zgodnie z komunikatem programu | gotowe |
| 129. usunięcie do kosza jest odrzucane przed wykonaniem | gotowe |
| Edycja pliku z BOM i końcami linii Windows zachowuje jego format, a cofnięcie przywraca te same bajty | gotowe |

**Ukończone, gdy:** testy odtwarzają równoczesny zapis przy cofaniu obu rodzajów zmian, a wersja edytora zostaje na miejscu.

## Etap 2: ciągłość zadania

| Zadanie | Stan |
|---|---|
| Końcowa odpowiedź po odczycie pliku dostaje wyniki poleceń i wczytane Skille | gotowe |
| Etap edycji dostaje ustalenia z rozmowy, pamięć zadania, wyniki rozpoznania i Skille | gotowe |
| To samo w `project` | do zrobienia |
| Skill wczytany w rozmowie jest dostępny w kolejnych pytaniach bez dodatkowego wywołania modelu | do zrobienia |
| Skille na żądanie także w `material`, `project` i `task` | do zrobienia |
| Skill działa też bez `permissions.toml`, gdy pytanie jest ogólne | do zrobienia |
| Procedury 21 Skilli: interpretacja odczytów, warunki zastosowania, zakończenie diagnozy i weryfikacja zmiany | w toku |

**Ukończone, gdy:** przy „ustalmy separator CSV, zrób to” żądanie edycji zawiera ustaloną wartość, co potwierdza test na rzeczywistym generatorze żądań.

## Etap 3: pamięć i cofanie dostępu

| Zadanie | Stan |
|---|---|
| Wynik wykonanej zmiany zostaje w pamięci, gdy następne żądanie do modelu się nie uda | gotowe |
| Niezależna odpowiedź nie jest usuwana tylko dlatego, że wcześniejsza tura zależała od cofniętego pliku | gotowe |
| Treść cofniętego pliku nie przetrwa w faktach kontrolera, np. w argumencie polecenia | gotowe |
| Rozmowa z wieloma zależnymi plikami daje się zapisać | gotowe |

**Ukończone, gdy:** po błędzie usługi model nie proponuje ponownie wykonanej zmiany, a po cofnięciu dostępu w kolejnych żądaniach nie ma danych z tego pliku.

## Etap 4: izolacja i polityka

| Zadanie | Stan |
|---|---|
| Polecenia `allow` bez dostępu do gniazd Unix hosta (D-Bus, Docker) | do zrobienia |
| Ogólny filtr wywołań systemowych dla klienta | do zrobienia |
| Zgodne limity szablonów `change` w polityce i kontrakcie | gotowe |
| Sprawdzenie parsera szablonów pod kątem argumentów opcji | gotowe |
| Polecenia z prawdziwym Bubblewrap także w CI | do zrobienia |

**Decyzje do podjęcia:** klient ma dostęp do własnego profilu logowania. Obecna architektura ufa oficjalnemu klientowi w tym zakresie. Przed 1.0 trzeba to świadomie przyjąć albo zmienić.

## Etap 5: zmiana i test

| Zadanie | Stan |
|---|---|
| Testy projektu (np. `pytest`, `go test`, `npm test`) na odizolowanej kopii, a nie tylko siedem stałych sprawdzeń | do zrobienia |
| Jasny komunikat, co dane sprawdzenie potwierdza, a czego nie | do zrobienia |
| Dłuższy limit czasu dla `high`, jeśli długie wiadomości go przekraczają | do sprawdzenia |

## Etap 6: porządki przed wydaniem

| Zadanie | Stan |
|---|---|
| Nowy numer `pkgrel` przy każdej zmianie, tak aby wersja pakietu odróżniała wydania | do zrobienia |
| Działające skrypty odbioru albo ich usunięcie (`update_candidate.fish` wymaga starej wersji) | do zrobienia |
| Usunięcie modułów i poleceń nieużywanych przez zainstalowany program | do zrobienia |
| Połączenie powielonych przepływów `material` i `project` | do zrobienia |
| `task prune` z listą wpisów i zgodą | do zrobienia |
| Jedna sesja klienta na rozmowę, strumieniowanie odpowiedzi | do rozważenia |

## Warunki wydania 1.0

1. Etapy 1–5 ukończone.
2. Cały przebieg z początku tej strony zaliczony na CachyOS dla jednego commita, także na świeżej instalacji według README.
3. Prawdziwe awarie sprawdzone: wygaśnięcie logowania, brak sieci, limit usługi.
4. Niezależny przegląd bezpieczeństwa.
5. Sprawdzone warunki Anthropic dla programu uruchamiającego Claude Code z logowaniem subskrypcyjnym.
6. Lista kontrolna wydania odhaczona dla tego commita.

## Dziennik realizacji

### 2026-11-05 — E3: jakość procedur Skills

- **Status: READY FOR TEST.** Gałąź `improve/skills-procedures`, baza `ba82f88c43eea65c7a3f5fe85a2a6639a5ed0d08`.
- Ślad zmian: [PR #16](https://github.com/ultherego/ai-desktopv2/pull/16), commit procedur i regresji [`872859d`](https://github.com/ultherego/ai-desktopv2/commit/872859d1c2e77a61a71ae4ffe258d0722ef9b7b1). Wyniki wymaganych zadań CI są przypisane do aktualnego head tego PR.
- Zakres: poprawa wszystkich 21 istniejących `SKILL.md`, bez zmiany nazw, przypisań agentów, uprawnień ani mechanizmu wykonania.
- Kryterium: procedura wskazuje potrzebne fakty, interpretację wyników, warunki zatrzymania i wynik diagnozy; zmiana wymaga odrębnego planu, testu, zgody i przywrócenia konkretnego stanu.
- Ryzyka podlegające przeglądowi: pełny balance bez uzasadnienia, ogólne `systemctl revert`, reset kluczy i downgrade bez diagnozy, naprawa rozruchu bez ustalenia etapu awarii oraz pomylenie izolowanego widoku z awarią hosta.
- Dowody lokalne: 33/33 testy PASS (`test_skills`: 7, `test_user_skills`: 13, `test_prepared_skill_analysis`: 1, `SkillsOnDemandTests`: 12), bez pominięć; walidacja 21/21 treści i składni Python PASS. Rzeczywiste treści pakietowych Skilli przechodzą przez zgodę i końcowe żądanie z plikiem; komplet treści mieści się w żądaniu edycji także w trybach wysyłających wszystkie przypisane Skille. Regresje uruchamia istniejący etap `unit` centralnej bramy i macierz GitHub Actions, bez osobnej bramy.
- Przegląd treści: niezależny recenzent ocenił komplet 21 procedur bez istotnych uwag blokujących. Sześć syntetycznych scenariuszy sprawdziło decyzje dotyczące APU/dGPU, braku sesji audio, potrzeby balance, zachowania istniejących drop-inów, nieustalonego etapu rozruchu i znaczenia `text: PASS`. Nie uruchamiano oficjalnego klienta ani poleceń diagnostycznych z procedur.
- CI dla `c53e51514a3c07e58b43c75c8110c51a616a8302`: **5/5 wymaganych zadań PASS**, [run 42060801397](https://github.com/ultherego/ai-desktopv2/actions/runs/42060801397): Python 3.12, 3.13, 3.14 oraz dwie wersje Bubblewrap. To dowód tego SHA, nie odbiór CachyOS. Pełnej bramy lokalnej nie powtarzano w środowisku UID 0, w którym testy wymagające zwykłego użytkownika odmawiają.
- Nieukończone: sprawdzenie użyteczności w rzeczywistej rozmowie na CachyOS. Walidacja formatu, przegląd scenariuszy i test transportu nie potwierdzają skuteczności naprawy na hoście. Pozostałe kryteria etapu 2 i E3 pozostają otwarte.
- **Następny krok:** po zaliczeniu CI ocenić procedury podczas zwykłej diagnostyki read-only na CachyOS, bez wykonywania sugerowanych zmian systemowych.
