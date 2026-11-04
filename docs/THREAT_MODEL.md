# Architektura i model zagrożeń

Opis bieżącego kodu, a nie certyfikat izolacji. Tabela w sekcji 5 opisuje wymagane zachowanie, nie wynik zaliczony w każdym środowisku. Zgłaszanie problemów
i zgody: [SECURITY.md](../SECURITY.md). Ograniczenia: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## 1. Co chronimy i przed kim

**Zasoby:** katalog domowy i pliki konfiguracyjne użytkownika, profil logowania (OAuth) klienta w `/var/lib/ai-desktop`, klucze SSH i GPG, gniazda i usługi lokalne,
sieć, pliki projektów oraz decyzja użytkownika o wysłaniu lub zapisie danych.

**Przeciwnik:** prompt injection w pliku, wyniku polecenia, Skillu lub wyniku modelu; błędna lub wroga odpowiedź dostawcy; nieintencjonalne działanie modelu; błędny
JSON; podmiana pliku lub dowiązania w trakcie operacji (symlink, TOCTOU); awaria lub timeout usługi; niepożądany ruch sieciowy.

**Poza zakresem:** root, przejęte jądro, złośliwe `/usr`, wrogi proces tego samego użytkownika, utrata zasilania, fizyczny dostęp. Klient ma dostęp do własnego OAuth.

**Zasada podstawowa:** model i jego odpowiedź nie są źródłem uprawnień. Decyduje polityka użytkownika i kontroler, a przy zapisie także człowiek.

## 2. Architektura

```text
Użytkownik (terminal, TTY)
   │  pytanie, zgody
   ▼
Kontroler ai-desktop (UID użytkownika) ── czyta politykę i pliki, zbiera dane, zapisuje po zgodzie
   │        │            │
   │        │            └─ polecenia allow: bwrap, tylko odczyt, bez sieci
   │        │               polecenia change, test, wycofanie: bez piaskownicy, po "tak"
   │        └─ testy propozycji: bwrap, kopia w tmpfs, seccomp, bez sieci
   │  sudo: stały pomocnik root, tylko start/stop jednostek sesji
   │  gniazdo Unix (0600, właściciel UID użytkownika)
   ▼
Usługa analizy (UID ai-desktop, jednostka systemd z ograniczeniami)
   │  proxy CONNECT poza piaskownicą ──► tylko dozwolone hosty, port 443, adresy publiczne
   ▼        ▲ gniazdo Unix
Oficjalny klient w bwrap (osobne przestrzenie user/mnt/pid/net/ipc/uts)
```

**Kontroler** działa jako zwykły użytkownik i odmawia pracy jako root. Pakiet uruchamia Python z `-I -B` z katalogu należącego do roota.

**Pomocnik sesji** to stały plik uruchamiany przez `sudo`. Tworzy jednostkę przejściową z ograniczeniami i limitem czasu, startuje socket tylko dla wywołującego UID i sprząta
także po śmierci interfejsu. Kanał do niego nie przenosi danych ani poświadczeń. Pakiet nie instaluje reguł sudoers ani polkit, a jednostki są nieaktywne, dopóki sesja ich nie uruchomi.

**Usługa analizy** działa jako `ai-desktop`, bez powłoki logowania i grup dodatkowych. Ma `NoNewPrivileges`, pusty zbiór capabilities, `ProtectSystem=strict`, `ProtectHome`, limity pamięci,
zadań i czasu oraz ograniczoną aktywację gniazda. Przed użyciem sprawdza własną tożsamość, prawa katalogu logowania i właściciela plików programu.

**Klient** to oficjalny plik wykonywalny z katalogu należącego do roota, w dokładnej zgłaszanej wersji 2.1.312. Dostaje czyste środowisko, własny katalog logowania z zapisem, puste HOME i katalog roboczy. Nie ma dostępu do katalogu
domowego użytkownika ani gniazd pulpitu. Tryb zwykły: `--tools ""`, pusta konfiguracja MCP, jedna tura, bez zapisu sesji. Tryb web: wyłącznie `WebSearch`, najwyżej 4 tury i 2 wywołania. Filtr seccomp zakazuje ustawiania bitów SUID/SGID. Nie ma ogólnego filtra wywołań systemowych.

**Proxy** obsługuje tylko CONNECT do `api.anthropic.com`, `claude.ai`, `platform.claude.com` (przy Codexie osobno `api.openai.com`). Rozwiązuje nazwę raz, sprawdza wszystkie adresy, odrzuca niepubliczne (w tym localhost i adresy metadanych) i łączy na sprawdzony adres. Przed połączeniem wymaga
ClientHello z SNI równym nazwie hosta, odrzuca ECH. Nie używa MITM. Nie widzi treści TLS: nie filtruje ścieżek ani danych.

**Logowanie** odbywa się oficjalnym przepływem klienta w tym samym trybie izolacji, bezpośrednio w terminalu, bez przechwytywania. Program nie czyta tokenów, a przed żądaniem wymaga statusu `authMethod=claude.ai`. Nie ma klucza API ani obejścia. Codex: logowanie przez zaufany komponent poza przestrzenią modelu, krótkotrwały token idzie prywatnym kanałem do procesu klienta; osobny magazyn tokenów `/var/lib/ai-desktop/codex`.

**Pogoda** to kolektor kontrolera do dwóch stałych hostów Open-Meteo. **systemd:** tylko nazwy i stany jednostek.

Odrzucony wariant: broker tokenów ukrywający OAuth przed klientem, kiedyś oznaczony „PREFEROWANY, DO WERYFIKACJI E2”. Nie jest obecną architekturą ani planem awaryjnym. Stare wejścia `diagnose ask` i `controller ask` odmawiają i nie uruchamiają modelu.

## 3. Przepływ danych i zgoda

1. **Polityka.** Kontroler czyta `permissions.toml` tylko z konta systemowego (bez HOME i XDG): właściciel, `0600`, bez dowiązań i zapisu dla innych. Wynik jest związany z dokładnymi bajtami i tożsamością pliku, a zmiana unieważnia operację.
2. **Metadane.** Do modelu idzie pytanie i opis dostępu (wzorce, szablony, źródła), bez `deny`.
3. **Plan.** Odpowiedź modelu jest parsowana jako ścisły JSON (bez powtórzonych kluczy i `NaN`), z zamkniętą listą akcji i limitami. Prosty tekst w rozmowie staje się odpowiedzią bez uprawnień.
4. **Rozpoznanie i odczyt.** Nazwy i treści zbiera kontroler przez deskryptory z `O_NOFOLLOW`, z kontrolą `deny`, właściciela, typu, twardych linków i zmian w trakcie. Odczyt musi mieć oparcie w rozpoznaniu lub tekście użytkownika.
5. **Wysłanie.** Każde żądanie ma własną zgodę wewnętrzną: niezmienny ładunek, SHA-256, nonce i termin ważności, jednorazowe użycie. Okno to 180 s (330 s dla `xhigh`, `max`) na krok i 30 minut na pytanie, bez czasu oczekiwania na użytkownika.
6. **Wynik.** Tekst modelu jest ograniczony i oczyszczony z sekwencji terminala. Wyniki poleceń także.
7. **Zapis.** Propozycja dotyczy tylko plików oznaczonych jako zapisywalne. Test na kopii, pełny diff, zgoda, ponowne sprawdzenie polityki i stanu plików, potem dziennik i publikacja.

**Zasada fail-closed:** brak piaskownicy, zła wersja, wyczerpany budżet, zmiana stanu lub niepotwierdzone zatrzymanie sesji kończą operację odmową. Brak ponowień i fallbacku.

Wynik `--v`, `doctor` i `status` niczego nie autoryzuje. `status` jest offline: nie uruchamia klienta i nie dowodzi logowania.

## 4. Granica poleceń

### `[commands] allow`

**Parsowanie szablonów** (przy wczytaniu polityki): najwyżej 10 tokenów i 120 bajtów, bez znaków powłoki, literały z ograniczonego zestawu. Pierwsze słowo nie może być powłoką, interpreterem, narzędziem eskalacji, klientem sieciowym, programem zapisującym ani uruchamiającym inne programy (`env`, `find`, `git`, `less`, `tar` i inne). Znacznik nie może stać jako pierwszy argument ani przed podkomendą narzędzi typu `systemctl` i `docker`. Literalna ścieżka objęta `deny` jest odrzucana.

**Dopasowanie:** propozycja modelu to lista słów, nigdy tekst dla powłoki. Porównanie token po tokenie, znaczniki z własnym wzorcem. Program rozwiązywany jest w stałych katalogach systemowych: plik i wszystkie katalogi muszą należeć do roota i nie mogą być zapisywalne dla innych.

**Wykonanie:** bez pytania, bez powłoki i wejścia, z czystym środowiskiem, limitem czasu i wyjścia. Piaskownica: `/` tylko do odczytu, tmpfs na `/home`, `/root`, `/run/user`, `/tmp`, bez sieci, IPC i capabilities, osobne PID (chyba że `share_processes = true`, wtedy widać procesy hosta i ich linie poleceń). Wynik jest oczyszczany i idzie do modelu jako niezaufane dane.

**Budżety:** 16 poleceń w planie, `command_rounds` na pytanie, wspólny limit `discovery_bytes`. Brak `bwrap` oznacza brak wykonania.

**Czego ta granica nie daje:** piaskownica nie izoluje gniazd Unix hosta (D-Bus, Docker), więc polecenie z listy może zmienić stan bez pytania. Piaskownica nie ukrywa reszty systemu plików (np. `/etc`). `deny` nie filtruje wyników: dzienniki i listy procesów mogą zawierać dane wrażliwe. Odpowiedzialność za listę spoczywa na użytkowniku.

### `[commands] change`

Szablon jest w `allow` albo w `change`, nigdy w obu, a znacznik nie może stać przed podkomendą. Lista `change` działa tylko przy niepustej liście `allow`. Plan musi podać test (pasujący do `allow`, nie do `change`) i wycofanie (z `change` lub `allow`). Musi być budżet na trzy polecenia, inaczej nic się nie dzieje. Polecenie z listy `change` nie może przejść w grupowym planie odczytu. Agent `read-only` w ogóle nie dostaje tej listy.

Wymagane jest wpisanie całego słowa `tak` po zobaczeniu wszystkich trzech poleceń. Zmiana, test i wycofanie działają **na żywym systemie, bez piaskownicy**, z oczyszczonym środowiskiem (dostępna magistrala użytkownika, bez HOME i sekretów), bez wejścia, z limitami. Niezaliczony test pokazuje wycofanie i wymaga drugiego `tak`. Program nigdy nie cofa sam. Zatwierdzone wycofanie może się nie wykonać, gdy skończy się budżet pytania.

Co to oznacza: po `tak` polecenie ma pełne uprawnienia użytkownika. Znaczniki w szablonie (np. `<unit>`) wybiera model. Test i wycofanie mogą nie przywrócić stanu. Zgoda dotyczy pokazanej treści, a nie jej skutków.

## 5. Zagrożenia

| ID | Atak lub awaria | Wymagane zachowanie |
|---|---|---|
| T01 | Nieoczekiwane pole, powtórzony klucz, złośliwy ładunek | Odrzucenie przed wysłaniem, bez surowych logów |
| T02 | Podmiana ładunku po zgodzie | Niezgodny SHA-256 i nonce, odmowa |
| T03 | Dostęp klienta do katalogu domowego, `.ssh`, cudzych poświadczeń | Brak montowania. Wyjątek: własny profil OAuth klienta. Zatwierdzony tekst to nie montowanie |
| T04 | Zapis na hoście, w `/tmp` hosta, uruchomienie powłoki przez klienta | Brak narzędzi, brak zapisywalnych montowań poza profilem |
| T05 | Gniazdo hosta, D-Bus, agent SSH w kliencie | Brak dostępu i przekierowania |
| T06 | Dowolny ruch, localhost, adres metadanych, przekierowanie, DNS rebinding | Blokada poza listą hostów. Przekierowanie w obrębie listy pozostaje możliwe |
| T07 | Ujawnienie tokenów w pytaniu, błędzie, odpowiedzi | Kontroler nie czyta tokenów i nie przekazuje surowych błędów. Bez ochrony tokenu przed przejętym klientem |
| T08 | Błąd sieci, wygasłe OAuth, brak `bwrap` | Fail-closed, stały komunikat, bez fallbacku na hosta |
| T09 | Złośliwa instrukcja w wyniku modelu | Tekst nie staje się poleceniem ani zapisem |
| T10 | Dowiązanie, wyjście ze ścieżki, TOCTOU w katalogu zadania | Brak wyjścia poza zakres; zmiana stanu to odmowa. Wyjątek przy odczycie: dowiązanie katalogu w `version = 2` na warunkach z [AGENT_MANAGEMENT.md](AGENT_MANAGEMENT.md) |
| T11 | Zmiana planu lub pliku po zatwierdzeniu | Niezgodny stan, odmowa |
| T12 | Przerwanie lub timeout w trakcie zapisu | Dziennik przed zapisem, stan widoczny w `task status`, cofanie po zgodzie, bez gwarancji przy utracie zasilania |
| T13 | Niepoprawna odpowiedź lub błąd wykonawcy offline | Odmowa, program nie uruchamia modelu z tej ścieżki |
| T14 | Prompt injection przez `SKILL.md` | Skill tylko z pakietu lub katalogu użytkownika, bez obcego właściciela i zapisu dla innych, limit rozmiaru. Nie daje uprawnień. Treść nie jest filtrowana |
| T15 | WebSearch bez `web = true` lub po zmianie polityki | Odmowa przed klientem, brak narzędzi w innych trybach |
| T16 | Pliki lub dane systemd w wyszukiwaniu | Tryb web dostaje tylko pytanie i stałe instrukcje |
| T17 | Inne narzędzie, nadmiar tur, podszyty wynik lub URL źródła | Ograniczona konfiguracja klienta i ścisła kontrola strumienia i źródeł |
| T18 | Model proponuje polecenie spoza listy lub z innym argumentem | Dokładne dopasowanie, inaczej nie uruchamia |
| T19 | Model wybiera groźną podkomendę przez znacznik | Znacznik nie może stać przed podkomendą |
| T20 | Podmieniony program w katalogu użytkownika | Tylko pliki roota w katalogach roota |
| T21 | Polecenie z `allow` zmienia stan przez gniazdo Unix | **Nie jest zablokowane.** Odpowiedzialność za listę |
| T22 | Wynik polecenia wynosi dane do dostawcy | **Zamierzone.** Granicą jest lista i limit wyjścia, `deny` nie filtruje wyniku |
| T23 | Injection nakłania do zmiany na żywo | Zmiana tylko z listy `change`, po `tak`. Nie ma domyślnej listy |
| T24 | Zmiana bez testu lub wycofania, test zmieniający stan | Odmowa przed wykonaniem |
| T25 | Obejście `deny` inną pisownią lub wpisem niżej | `deny` obejmuje wszystko pod ścieżką; nazwy odfiltrowane przed wysłaniem |
| T26 | Cofnięcie nadpisuje późniejszą edycję | Porównanie bajtów i trybu, konflikt zachowuje wersję użytkownika |
| T27 | Przywrócenie z kosza na zajętą nazwę | Publikacja bez nadpisywania, inaczej konflikt |
| T28 | Zgoda z rozmowy obowiązuje po zmianie uprawnień | Zgoda związana z dostępem, źródłami i usługą, inaczej nowe pytanie |
| T29 | Wklejony tekst zatwierdza kolejne działanie | Bufor terminala czyszczony przed pytaniem, bez ochrony przed innym procesem na tym TTY |
| T30 | Nawrót danych po cofnięciu dostępu | Zależne odpowiedzi i pamięć są usuwane przed kolejną turą |

## 6. Zapis, cofanie i usuwanie

Zapis idzie przez jeden wykonawcę: blokada katalogu, kontrola tożsamości, dziennik `.ai-desktop-tasks` (`0700`, pliki `0600`) ze starą i nową treścią przed publikacją, zapis pliku tymczasowego, kontrola, publikacja. Nowe pliki publikowane są bez nadpisywania; zajęta nazwa to odmowa. Ścieżki `.ai-desktop-tasks` i `.ai-task-*` są zarezerwowane dla programu, także w `version = 2`.

Cofanie porównuje bajty i tryb. Konflikt jest zachowany w dzienniku i widoczny, ale nie blokuje nowych zapisów, o ile w katalogu nie ma pozostałości. Blokują je nierozstrzygnięte pliki tymczasowe i niedokończone zadania. Limit 128 dzienników. Dziennik jest związany z katalogiem; historia z innego urządzenia lub podmienionego katalogu jest niepowiązana i nie służy do cofania.

Usuwanie przenosi do `.ai-desktop-tasks/trash-*` po zgodzie związanej z dokładnym planem (tożsamości, rozmiary, tryby, SHA-256) ważnej 180 s. Najwyżej 8 plików po 16 MiB. Przywracanie tylko na wolne nazwy.

## 7. Czego dowody nie obejmują

Testy syntetyczne używają atrapy klienta i nie dowodzą zachowania prawdziwego klienta ani odświeżania OAuth. Nie ma testu na wrogi proces tego samego UID ani utratę zasilania. Nie ma niezależnego przeglądu. Wynik jednego commita lub komputera nie poświadcza innego. Zmiana tych granic wymaga opisu kompromisu tutaj i testu na prawdziwym systemie.
