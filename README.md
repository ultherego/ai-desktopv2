# AI Desktop

Terminalowy asystent po polsku do diagnozy, konfiguracji i pracy z plikami na CachyOS i Arch Linux x86_64. Uruchamiasz `ai-desktop ask`, piszesz zwykłym zdaniem, a program sam zbiera dane, na które pozwala Twój plik `~/.config/ai-desktop/permissions.toml`, i wysyła je do modelu. Każdy zapis pokazuje pełny diff i czeka na Twoją zgodę.

Pod spodem działa oficjalny klient Claude Code w dokładnej wersji **2.1.312**, który pobierasz sam. Klient pracuje na osobnym koncie systemowym `ai-desktop`, w piaskownicy Bubblewrap, a do sieci wychodzi tylko przez filtrujące proxy. Opcjonalnie można użyć Codexa.

**Czym to nie jest:**

- Nie jest usługą w tle ani autonomicznym agentem. Działa wtedy, gdy o coś zapytasz.
- Model nie dostaje powłoki ani narzędzi. Proponuje kroki, a program sprawdza je i wykonuje tylko to, na co pozwalają uprawnienia i Twoja zgoda.
- Nie używa klucza API i nie przełącza się na inną usługę, gdy coś zawiedzie.
- Nie jest certyfikowaną izolacją ani skanerem sekretów. Zobacz [SECURITY.md](SECURITY.md).

## Stan rozwoju

To wersja rozwojowa, nie wydanie 1.0.

Aktualny pakiet kandydata ma numer **0.1.0-70** (zmiany: [CHANGELOG.md](CHANGELOG.md)).

Czego jeszcze nie ma i co nie zostało sprawdzone, opisuje [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). Przeczytaj ten plik przed użyciem na ważnych danych. Plan do wersji 1.0: [docs/ROADMAP.md](docs/ROADMAP.md), zadania: [docs/TODO.md](docs/TODO.md).

## Instalacja na laptopie (CachyOS / Arch x86_64)

Potrzebujesz zwykłego użytkownika z `sudo`, terminala i konta Claude (subskrypcja). Kroki w `fish`:

```fish
# 1. Zależności i narzędzia budowy
sudo pacman -S --needed base-devel git fish python bubblewrap libseccomp sudo python-rich python-cryptography

# 2. Kod
git clone https://github.com/ultherego/ai-desktopv2.git ~/Projekty/ai-desktop
cd ~/Projekty/ai-desktop/packaging/arch

# 3. Oficjalny klient Claude Code 2.1.312 (jednorazowo). Przy błędnej sumie plik nie zostanie uruchomiony,
#    a budowa w kroku 4 odmówi: usuń wtedy plik i pobierz go ponownie.
curl -fL -o claude-code https://downloads.claude.ai/claude-code-releases/2.1.312/linux-x64/claude
echo '4f4178ce41d83e23341ebfbaecdbcf8ea6bf7baea0690025abe49eeec3a5d01d  claude-code' | sha256sum -c -
and chmod 755 claude-code
and ./claude-code --version  # ma być: 2.1.312 (Claude Code)

# 4. Budowa i instalacja pakietu (wersja w nazwie pliku = pkgrel z PKGBUILD)
makepkg --cleanbuild
sudo pacman -U ai-desktop-0.1.0-70-x86_64.pkg.tar.zst
sudo systemctl daemon-reload

# 5. Logowanie do Claude (interaktywnie, raz)
ai-desktop login

# 6. Twoja polityka dostępu — program nie czyta niczego, czego tu nie wpiszesz
mkdir -p ~/.config/ai-desktop
cp ~/Projekty/ai-desktop/config/permissions.example.toml ~/.config/ai-desktop/permissions.toml
chmod 600 ~/.config/ai-desktop/permissions.toml
$EDITOR ~/.config/ai-desktop/permissions.toml     # uzupełnij files.read; deny zawsze wygrywa

# 7. Sprawdzenie i start
ai-desktop doctor
ai-desktop ask --preview      # pokazuje uprawnienia, bez modelu i bez wysyłania czegokolwiek
ai-desktop ask                # rozmowa
```

Uwagi:

- **Klient Claude (krok 3).** Pakiet nie pobiera go sam. Bez pliku `claude-code` w `packaging/arch/` działają tylko polecenia lokalne, a pytania do modelu są odrzucane. Korzystanie z klienta podlega warunkom Anthropic. Nie dodawaj tego pliku do Gita i go nie rozpowszechniaj.
- **Sudo.** Sesję klienta uruchamia stały pomocnik z prawami administratora, więc `ask`, `material`, `project`, `task run`, `login` i `stop` mogą pytać o hasło `sudo`. Pakiet nie dodaje reguł sudoers ani polkit.
- **Wymagania.** Python 3.12 lub nowszy, systemd, działające przestrzenie nazw bez roota i zwykły terminal: bez niego program nie pyta o zgodę, tylko odmawia.
- **Świeży system.** Te kroki nie były jeszcze powtarzane na zupełnie świeżej instalacji.

## Pierwsze uruchomienie i uprawnienia

`ai-desktop ask --preview` pokazuje dostępy bez modelu i bez odczytu danych. Bez pliku `permissions.toml` działają tylko pytania ogólne: żadnych plików, systemd, poleceń, pogody ani wyszukiwarki.

Przykładowa polityka (`config/permissions.example.toml` to pełny szablon):

```toml
version = 2

[network]
claude = true
codex = false
weather = true       # Open-Meteo
web = false          # wyszukiwanie w Internecie przez Claude

[system]
systemd = ["system", "user"]

[files]
read = ["~/Projekty/moj-projekt/"]
write = ["~/Projekty/moj-projekt/"]
deny = ["~/.ssh/", "~/.gnupg/", "~/.claude/", "~/.config/ai-desktop/"]

[commands]
allow = ["uptime", "systemctl status <unit> --no-pager", "journalctl -b -p err --no-pager -n <int>"]
```

Zasady czytania tego pliku:

- Pole, którego nie ma, oznacza odmowę, także `network.claude`. Nieznany klucz lub wadliwa wartość zatrzymują program, a komunikat wskazuje problem.
- Plik musi należeć do Ciebie i mieć prawa `0600`. Jego katalogi nie mogą być dowiązaniami ani umożliwiać zapisu innym. Limit to 16 KiB, 64 reguły na listę i 128 szablonów poleceń na listę.
- `~/x` to jeden plik, `~/x/*` to pliki bezpośrednio w katalogu, a `~/x/` i `~/x/**` to katalog z podkatalogami. `~/` to cały katalog domowy. Dwie reguły o tym samym zakresie (`~/` i `~/**`) są błędem.
- **`deny` wygrywa i chroni wszystko pod wskazaną ścieżką**, bez względu na zapis: `~/x`, `~/x/`, `~/x/*` i `~/x/**` działają tak samo.
- W `version = 2` nie ma ukrytej listy zabronionych nazw ani wykrywania kluczy prywatnych. **`deny` jest jedyną ochroną sekretów w dozwolonym drzewie.** Szablon zawiera listę popularnych katalogów poświadczeń; możesz ją zmienić, ale nie rozpoznaje ona wszystkich sekretów. Starszy format `version = 1` jest nadal rozpoznawany i ma wbudowane wykluczenia.
- Edycja istniejącego pliku wymaga `read` i `write`. Nowy plik wymaga `write` i istniejącego katalogu: program nie tworzy katalogów. Usuwanie wymaga `write`.

Pola poniżej są opcjonalne. Dopisz je w podanej sekcji, gdy chcesz zmienić wartość domyślną. Program odrzuca wartości spoza zakresu.

| Sekcja i pole | Domyślnie | Najwyżej | Znaczenie |
|---|---|---|---|
| `[files] file_size` | `"5MiB"` | `"16MiB"` | największy plik dla `material` |
| `[files] part_size` | `"240KiB"` | `"512KiB"` | porcja przy analizie całego pliku |
| `[files] read_files` | 64 | 128 | plików w jednym kontekście |
| `[files] read_file_size` | `"64KiB"` | `"128KiB"` | bajtów na plik |
| `[files] read_total_size` | `"128KiB"` | `"256KiB"` | bajtów wszystkich plików kontekstu |
| `[files] listing_entries` | 128 | 512 | wpisów w kroku list/find |
| `[files] discovery_bytes` | `"128KiB"` | `"256KiB"` | bajtów kroków rozpoznania wysłanych do modelu w jednym pytaniu |
| `[files] search_results` | 40 | 200 | trafień w kroku find |
| `[files] search_scan` | 20000 | 100000 | wpisów katalogów na pytanie |
| `[files] discovery_rounds` | 4 | 8 | kroków list/find na pytanie |
| `[files] plan_corrections` | 2 | 4 | poprawek planu odczytu na pytanie |
| `[files] command_rounds` | 24 | 32 | poleceń na pytanie (0 wyłącza) |
| `[files] command_output` | `"8KiB"` | `"128KiB"` | bajtów wyniku jednego polecenia |
| `[files] command_timeout` | 20 | 60 | sekund na jedno polecenie |
| `[commands] share_processes` | `false` | | `true`: polecenia widzą procesy hosta i ich linie poleceń |
| `[commands] change` | brak | | szablony zmian: działają na żywym systemie, wymagają `tak`, testu z `allow` i wycofania |

`read_file_size` nie może przekraczać `read_total_size`. Niezależnie od tych pól działają stałe limity: 384 KiB kontekstu JSON i 512 KiB całego żądania. Pliki zmieniane przez model mają limit 64 KiB na plik i 128 KiB łącznie, także gdy limity odczytu podniesiesz.

### Polecenia: `[commands]`

Domyślnie model nie uruchamia niczego. `allow` to lista dokładnych szablonów, bez powłoki. `<unit>` oznacza jedną nazwę jednostki systemd, `<int>` liczbę do 99999, a `<word>` jedno proste słowo. Szablony zaczynające się od powłoki, interpretera, `sudo`, klienta sieciowego lub programu zapisującego (`rm`, `tee`, `git`, `find`, `sed` i podobne) są odrzucane przy wczytaniu polityki.

**Polecenia z `allow` działają bez pytania, a ich wyniki trafiają do modelu.** Usuń z listy to, czego nie chcesz udostępniać, także z szablonu, który zawiera m.in. `journalctl`, `ps aux` i `docker ps`. `deny` nie filtruje wyniku polecenia.

Polecenia z `allow` działają w piaskownicy: system plików tylko do odczytu, ukryte `/home`, `/root` i `/run/user`, bez sieci i bez wejścia. Nie izoluje ona gniazd Unix hosta, więc wpisuj tu tylko polecenia, które niczego nie zmieniają. Część programów nie zobaczy stanu hosta (np. `ip`, `wpctl`, `glxinfo`).

`change` to polecenia, które zmieniają system, np.:

```toml
[commands]
allow = ["systemctl status <unit> --no-pager"]
change = ["systemctl restart <unit>"]
```

Model musi podać zmianę razem z testem z listy `allow` i wycofaniem z listy `change` lub `allow`. Widzisz wszystkie trzy i wpisujesz całe słowo `tak`. Program wykonuje zmianę, potem test. Gdy któreś zawiedzie, pokazuje wycofanie i znów wymaga `tak`: nigdy nie cofa sam. **Zmiana, test i wycofanie działają na żywym systemie, z Twoimi uprawnieniami, bez piaskownicy**, z oczyszczonym środowiskiem, bez wejścia i z limitem czasu.

## Codzienna praca

Wszędzie `--v` pokazuje szczegóły i wysyłane dane, a nie zmienia zgód. `--format plain` wyłącza formatowanie odpowiedzi, `--format markdown` wypisuje źródłowy Markdown (`ask`, `material`, `project`, `task run`).

### `ai-desktop ask`

```fish
ai-desktop ask
ai-desktop ask --question "Dlaczego jednostka nie startuje?"
ai-desktop ask --question "Czym jest inode?" --no-context
```

Bez `--question` to jedna rozmowa: po każdej odpowiedzi wraca `>`. Pusta linia lub dwa Ctrl-D kończą rozmowę, a program pyta, czy ją zapisać. Ctrl-C podczas oczekiwania przerywa tylko bieżącą turę. Wiadomość ma do 128 KiB razem z wklejkami (duża wklejka pojawia się jako znacznik).

Jak działają zgody:

1. **Jedno `Przekazać żądanie do Claude [t/n]:` na rozmowę.** Obejmuje kolejne pytania, odczyty i polecenia z `allow`. Program pyta ponownie, gdy zmieni się `permissions.toml` lub wybrana usługa albo agent.
2. **Odczyty i rozpoznanie bez pytania.** Model listuje i szuka nazw (nie treści), potem czyta to, co znalazł albo co wpisałeś. Zgadnięta ścieżka jest odrzucana. Budżety są w tabeli wyżej.
3. **Polecenia z `allow` bez pytania**, do 16 w jednym kroku modelu i 24 na pytanie (`command_rounds`).
4. **Każdy zapis osobno.** Edycja lub nowy plik może wynikać z wcześniejszego rozpoznania. Program sprawdza propozycję na odizolowanej kopii, pokazuje pełny diff i pyta `[t/n]`. Niezaliczony test blokuje zapis.
5. **Usuwanie osobno.** Program wypisuje pliki i pyta `[t/n]`. Pliki nie są kasowane, tylko trafiają do kosza zadania: najwyżej 8 plików na raz, do 16 MiB każdy.
6. **Zmiany z listy `change`** wymagają wpisania `tak`.

Po zastosowanej zmianie model sam podejmuje następny krok albo kończy odpowiedź, najwyżej 5 automatycznych kroków. Każdy kolejny zapis znów pokazuje diff i pyta. Odrzucenie lub błąd oddaje głos Tobie. Napisz „rób dalej”, aby kontynuować po limicie.

Sprawdzenie na kopii pochodzi z listy: `python-unittest`, `python-syntax`, `json`, `toml`, `bash-syntax`, `fish-syntax`, `text`. Check `text` potwierdza tylko poprawny tekst UTF-8: **składnia i działanie nie są sprawdzane**. Żaden check nie dowodzi poprawności projektu.

Rozmowa pamięta ostatnie 32 wymiany (razem do 128 KiB, najstarsze wypadają pierwsze) i krótką pamięć zadania. Długa historia zużywa limit usługi, bo idzie z każdym pytaniem. Jeśli cofniesz dostęp do pliku, zależne od niego odpowiedzi znikają z pamięci przed kolejnym pytaniem. Limit czasu lub limit usługi kończy rozmowę, a niezapisana historia przepada.

Wyszukiwanie internetowe włączasz przez `web = true`. Jest to osobne żądanie: tylko Twoje pytanie, bez plików i danych lokalnych. Pogoda (`weather = true`) ujawnia nazwę miejscowości i Twój adres IP serwisowi Open-Meteo, a wynik idzie do modelu.

Podczas rozmowy możesz wpisać polecenie zaczynające się od `/`. Po wpisaniu `/` pod wierszem pojawia się lista, strzałki wybierają, Tab uzupełnia. Polecenia działają lokalnie, nic nie wysyłają i nie kasują rozmowy: `/pomoc`, `/model`, `/sila`, `/skills`, `/agent`, `/ustawienia`, `/uprawnienia`, `/koniec`. Po zmianie modelu lub Skilli program raz jeszcze pyta o zgodę na wysyłkę. `/skills` działa w tej rozmowie; na stałe służy `ai-desktop agents`. Pytanie zaczynające się od ścieżki, np. `/etc/fstab co to?`, idzie do modelu normalnie. Aby wysłać pojedyncze słowo z `/`, zacznij od spacji.

### `ai-desktop material PLIK`

Jeden plik tekstowy UTF-8 do `file_size`, objęty `files.read`. Pytasz zwykłym zdaniem; przed pytaniem nic nie jest wysyłane.

- Plik do 200 KiB trafia do modelu w całości.
- Przy większym model prosi o wyszukania i odczyt wierszy, najwyżej 8 żądań na pytanie, do 256 KiB na żądanie i 2 MiB łącznie. Widzisz każdy krok, a reszta pliku nie jest wysyłana.
- „Przeanalizuj cały plik”: program dzieli plik na części, pokazuje plan i pyta raz o całe zadanie. Zgoda wygasa po 30 minutach. Najwyżej 24 części, więc większy plik wymaga większego `part_size`. Pokrycie liczy program, a przerwana analiza jest oznaczona jako częściowa.
- Odpowiedź podaje wiersze, które model naprawdę dostał. Pamięć sesji to 4 ostatnie pytania i odpowiedzi.
- Bez zapisu, Internetu i uruchamiania kodu. Odmowa dotyczy m.in. pustego pliku, dowiązania, znaków sterujących i niepoprawnego UTF-8.

Polecenia lokalne, które nic nie wysyłają: `/znajdz TEKST`, `/pokaz A-B`, `/rejestr`, `/koniec`. `/pytaj A-B PYTANIE` zawęża pytanie do wierszy. Usługa przyjmuje 4 uruchomienia sesji na minutę, więc program sam czeka między krokami.

### `ai-desktop project KATALOG`

Jeden katalog objęty `files.read`. Skan jest lokalny i powtarzany przed każdym pytaniem. Limity: 20 000 plików, 100 000 wpisów, głębokość
16. Pomijane i liczone są wpisy z `deny`, dowiązania, pliki specjalne i `.git`.

- **Czytanie.** Jedna zgoda na pytanie. Model prosi o nazwy, wyszukiwanie w treści i odczyt wierszy, najwyżej 8 żądań i 2 MiB danych. Nazwy plików, trafienia i fragmenty są wysyłane. Przy dużym projekcie kolejne etapy (do 5) zatwierdzasz po jednym.
- **Zmiany.** Tryb nie jest tylko do odczytu: po prośbie o zmianę model proponuje pliki, a dalej działa ten sam tryb co w `ask` (zgoda na przekazanie, test na kopii, diff, osobna zgoda na zapis, cofanie). Potrzebny jest wpis `files.write` z katalogiem i podkatalogami obejmujący projekt, np. `~/Projekty/**`. Ograniczenia: 8 plików, 5 poziomów ścieżki, 64 KiB na plik.
- **Usuwanie.** Lista plików, `[t/n]`, kosz zadania i wypisane polecenie przywracania.
- `project` nie uruchamia poleceń i nie korzysta z Internetu.

Polecenia lokalne: `/pliki [KATALOG]`, `/szukaj TEKST`, `/odswiez`, `/rejestr`, `/koniec`. Duże wklejki (do 32 KiB) pojawiają się jako znacznik i idą do modelu jako dane. Enter wysyła, a Alt+Enter łamie wiersz. Dwa Ctrl+C w ciągu 3 sekund kończą sesję.

### `ai-desktop task`

Zmiana dokładnie wskazanych plików, bez `permissions.toml`: wybór należy do Ciebie.

```fish
ai-desktop task run "Popraw dodawanie w calc.py" --workspace /sciezka/do/projektu \
    --file calc.py --context test_calc.py --test python-unittest
```

`--file` może być zmieniony, `--context` jest tylko do odczytu. Katalog musi być podany ścieżką bezwzględną i nie może być całym katalogiem domowym. Limity: 64 pliki wejściowe, 32 proponowane, 64 KiB na plik, 128 KiB łącznie, instrukcja do 128 KiB. Testy biegną na kopii, bez sieci i katalogu domowego. Testy Python nie mogą tworzyć procesów, wątków ani gniazd, więc nie każdy zestaw testów tu zadziała. `--preview` pokazuje dane bez modelu i testu. Są dwie zgody: na wysłanie i na zapis.

### Cofanie i przywracanie

Po zapisie program wypisuje gotowe polecenie.

```fish
ai-desktop task status   --workspace KATALOG              # stany, także kosz
ai-desktop task rollback --workspace KATALOG              # cofnięcie zapisu, osobna zgoda
ai-desktop task restore  --workspace KATALOG --plan ID    # powrót usuniętych plików
```

- Cofanie porównuje **bajty i tryb pliku**, więc zapis tej samej treści przez edytor go nie blokuje. Gdy plik później zmieniono, cofanie odmawia i zostawia Twoją wersję.
- Taki konflikt widać w `task status` i można go ponowić, ale nie blokuje innych zapisów. Nowe zapisy blokują tylko nierozstrzygnięte pliki tymczasowe i niedokończone zadania.
- Przywracanie działa tylko na wolne nazwy, nigdy nie nadpisuje nowszego pliku.
- Dziennik mieści się w `.ai-desktop-tasks` w katalogu zadania, zawiera starą i nową treść plików i ma prawa prywatne. Limit to 128 dzienników na katalog, bez automatycznego czyszczenia. Nie usuwaj go, aby obejść odmowę, bo tracisz możliwość cofnięcia.
- Dziennik jest związany z katalogiem: przeniesiony lub zastąpiony katalog może dać bezpieczną odmowę, a historia z innego miejsca jest oznaczana jako niepowiązana i nie służy do cofania.

### Które polecenia są w szablonie

Lista `allow` w szablonie to ogólne polecenia, które działają w piaskownicy na każdym systemie i nie wysyłają logów, nazw sieci ani danych o logowaniach. Są one uruchamiane bez pytania, a ich wyniki trafiają do modelu. Celowo nie ma w niej:

- `journalctl` z treścią logów, `dmesg`, `who`, `last`, `id`: logi i dane o użytkownikach;
- `nmcli`, `bluetoothctl`: nazwy sieci i urządzeń;
- `docker`: sięga do demona na hoście mimo piaskownicy;
- `ip`, `ss`, `ps`, `top`, `df`, `findmnt`: w piaskownicy pokazują jej widok, nie stan systemu;
- `systemctl --user`, `pactl`, `wpctl`, `glxinfo`: nie mają dostępu do sesji użytkownika.

Możesz dopisać je u siebie, jeśli świadomie tego chcesz. Program niezainstalowany w systemie jest pomijany.

## Własne Skille

Skill to instrukcja dla modelu do 96 KiB, nie uprawnienie. Agent może mieć dowolnie wiele Skilli, ale razem z rolą do 104 KiB: cała instrukcja idzie do klienta Claude jako jeden argument, a Linux ogranicza go do 128 KiB. W `ask` przy każdym pytaniu idą tylko nazwy i opisy przypisanych Skilli. Treść idzie dopiero wtedy, gdy model o nią poprosi, a program pokazuje to wierszem `Skill NAZWA`. W `material`, `project` i `task` treść przypisanych Skilli idzie przy każdym żądaniu. Skill ma opisywać, jak coś zrobić, a nie jak wygląda Twój komputer: fakty model odczyta sam, gdy będą potrzebne. O plikach i poleceniach decyduje `permissions.toml`. Pakiet zawiera tylko ogólne Skille; to, co dotyczy Twojego komputera, trzymasz u siebie:

```fish
ai-desktop skills new moj-system      # tworzy ~/.config/ai-desktop/skills/moj-system/SKILL.md
ai-desktop skills                     # lista dostępnych
ai-desktop skills show moj-system     # podgląd
ai-desktop agents                     # przypisanie Skilla do agenta
```

Własny Skill o nazwie ogólnego ma pierwszeństwo. **Nie wpisuj do niego sekretów ani danych o sobie: opis i treść Skilla mogą trafić do modelu.** Ścieżka wpisana w Skillu liczy się jak podana przez Ciebie, więc model może ją przeczytać bez rozpoznania; `deny` nadal decyduje.

## Agenci i modele

Agent to zapisany zestaw: usługa, model, poziom rozumowania, dostęp (`changes` lub `read-only`), instrukcje i Skille. Agent nie dodaje uprawnień. Agent `read-only` nie zapisuje, nie usuwa i nie dostaje poleceń `change`.

```fish
ai-desktop settings                                 # menu ustawień
ai-desktop settings models                          # dostępne modele i poziomy
ai-desktop settings set --model sonnet --effort high
ai-desktop agents setup                             # gotowe role i Skille, nie zmienia Twoich
ai-desktop ask --agent reviewer --model opus --effort high
ai-desktop sessions                                 # zapisane rozmowy
```

Szczegóły: [docs/AGENT_MANAGEMENT.md](docs/AGENT_MANAGEMENT.md).

## Codex (opcjonalnie)

Bez tej sekcji wszystko działa na Claude. Tę ścieżkę traktuj jako eksperymentalną.

```fish
cd ~/Projekty/ai-desktop
python3 -B scripts/fetch_codex_client.py     # pobiera Codex 0.166.0 i sprawdza sumę; bez logowania i uruchamiania
# potem przebuduj i zainstaluj pakiet jak w krokach 4 instalacji
ai-desktop codex status
ai-desktop login --codex                     # logowanie subskrypcją ChatGPT i wybór usługi
```

W `permissions.toml` ustaw `codex = true` w `[network]`: pozwolenia Claude i Codexa są osobne, a Codex jest domyślnie zablokowany. Codex nie ma wyszukiwania internetowego. Odłączenie konta: `ai-desktop codex logout --allow-chatgpt-logout`.

## Aktualizacja

```fish
ai-desktop stop
ai-desktop status
cd ~/Projekty/ai-desktop
git pull
cd packaging/arch
makepkg --cleanbuild
sudo pacman -U ai-desktop-WERSJA-x86_64.pkg.tar.zst    # WERSJA = pkgver-pkgrel z PKGBUILD
sudo systemctl daemon-reload
ai-desktop status
```

Nie aktualizuj w trakcie zadania. Aktualizacja nie jest atomowa: pliki są podmieniane po kolei i nie ma automatycznego wycofania pakietu. Po przerwanej aktualizacji zainstaluj ponownie ten sam plik i sprawdź `pacman -Qkk ai-desktop`. Pakiet nie zmienia `permissions.toml`, profilu logowania ani dzienników. Nieznany lub uszkodzony format pliku oznacza odmowę, nie migrację; nie poprawiaj wersji ani hashy ręcznie.

## Odinstalowanie

1. Zakończ sesję: `ai-desktop stop`, potem `ai-desktop status`.
2. Sprawdź `ai-desktop task status --workspace KATALOG` dla używanych katalogów. Odinstalowanie nie cofa zapisanych zmian.
3. `sudo pacman -R ai-desktop`, potem `sudo systemctl daemon-reload`.

**Na dysku zostaje:**

| Co | Gdzie |
|---|---|
| Konto systemowe `ai-desktop` | system |
| Profile logowania Claude i Codex | `/var/lib/ai-desktop/claude`, `/var/lib/ai-desktop/codex` |
| Polityka, agenci, własne Skille | `~/.config/ai-desktop/` |
| Zapisane rozmowy | `~/.local/state/ai-desktop/conversations/` |
| Dzienniki i kosz zadań | `.ai-desktop-tasks` w każdym katalogu, w którym coś zapisano |
| Twoje pliki projektów i pliki zmienione przez program | na miejscu |

Program nie ma polecenia „usuń wszystko”. Usuwasz te dane sam i na własną odpowiedzialność; skasowanie `.ai-desktop-tasks` odbiera cofanie i przywracanie, a pliki z kosza znikają razem z nim. Historia terminala, logi systemowe i dane po stronie dostawcy nie są czyszczone.

## Dane i prywatność

Do dostawcy trafia: Twoje pytanie, instrukcje programu i agenta (z przypisanymi Skillami), wzorce `read` i `write` oraz szablony poleceń, nazwy z rozpoznania, treść i **bezwzględne ścieżki** czytanych plików, wyniki poleceń, dane systemd i pogody oraz pamięć rozmowy. Reguły `deny` nie są wysyłane. Oficjalny klient dodaje własny kontekst techniczny, więc `--v` nie jest zapisem całego ruchu. Pełna lista: [SECURITY.md](SECURITY.md).

- Wysłanych danych nie można wycofać: ani przerwanie, ani cofnięcie, ani odinstalowanie nie usuwa ich u dostawcy.
- Rozmowa jest zapisywana lokalnie tylko po Twoim „tak” na końcu albo z `--session new`, w plikach prywatnych. Odpowiedź może cytować przeczytane pliki.
- Terminal może zachować przewijaną treść, a `--question` trafia do historii powłoki. Nie dołączaj wyniku `--v` do publicznych zgłoszeń.

## Gdy coś nie działa

| Objaw | Co zrobić |
|---|---|
| `Brak zaufanego klienta Claude Code 2.1.312` | Pakiet zbudowano bez `claude-code` albo z inną wersją. Powtórz kroki 3 i 4 instalacji. |
| `doctor` pisze „Klient Claude: brak” | W pakiecie nie ma pliku `claude-code`. Dodaj go według kroku 3 instalacji i zbuduj pakiet ponownie. |
| `status`: `auth=unchecked` | To normalne: `status` i `doctor` nie sprawdzają logowania ani sieci. |
| Prośba o logowanie | `ai-desktop login`, potem nowe pytanie. |
| Limit usługi, błąd sieci, przekroczony czas | Nic nie jest ponawiane samo. Odczekaj i zadaj pytanie ponownie. Czas oczekiwania to 110 s, a dla `xhigh` i `max` 15 minut. |
| Sesja zajęta lub niepotwierdzone zatrzymanie | `ai-desktop status`, potem `ai-desktop stop` (zatrzymuje tylko Twoją sesję). |
| `Niepoprawny permissions.toml` | Komunikat wskazuje klucz. Sprawdź też prawa `0600`. |
| `Claude jest zablokowany` | Ustaw `claude = true` w `[network]`. |
| Odmowa odczytu | Sprawdź `read` i `deny`. Dowiązanie, plik specjalny, obcy właściciel lub twardy link to odmowa. Limit podaje komunikat. |
| `Nie zapisano zmian` | Sprawdź `read` i `write` oraz czy katalog istnieje; potem `task status`. |
| Zapis odmawia po przerwanym zadaniu | Zostały pliki tymczasowe. `task status`, potem `task rollback`. Nie kasuj dziennika. |
| Limit 128 zadań | Zapis nie zostanie wykonany. Historii nie czyści żadne polecenie: oceń ją ręcznie. |

## Testy

```fish
python3 -B scripts/verify.py --mode ci --fail-fast     # składnia, testy jednostkowe, bez dostawcy AI
python3 -B scripts/verify.py --mode local --fail-fast  # dodatkowo prawdziwe próby Bubblewrap na tym komputerze
```

`--json NOWY_PLIK` zapisuje nazwy etapów i kody zakończenia, bez logów i bez nadpisywania. Tryby `compat` i `ai` wymagają jawnych flag (`--allow-claude-compat`, `--allow-ai-request`), a `ai` zużywa subskrypcję. Zielone testy nie dowodzą izolacji na Twoim komputerze.

## Dokumentacja

- [SECURITY.md](SECURITY.md): zgłaszanie problemów, granice zaufania, co nie jest gwarantowane.
- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md): architektura i zagrożenia.
- [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md): znane ograniczenia.
- [docs/AGENT_MANAGEMENT.md](docs/AGENT_MANAGEMENT.md): agenci, modele, rozmowy, Codex.
- [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md): lista dla opiekunów wydania.
- [CHANGELOG.md](CHANGELOG.md) i [LICENSE](LICENSE).
