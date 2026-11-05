# Agenci, modele, rozmowy i Codex

Agent nigdy nie dodaje uprawnień. O plikach, poleceniach i sieci decyduje `~/.config/ai-desktop/permissions.toml`, a o zapisie Twoja zgoda.

## Ustawienia i logowanie

`ai-desktop settings` otwiera w terminalu menu: strzałki i Enter wybierają, Esc wraca. Menu obejmuje usługę, agenta, model, poziom rozumowania, dostęp, instrukcje i Skille.
Zmiana ustawień nie loguje i nie wysyła niczego.

```fish
ai-desktop settings                                  # menu (także: settings menu)
ai-desktop settings show                             # bieżące ustawienia
ai-desktop settings models                           # dostępne modele i poziomy
ai-desktop settings set --provider claude --model sonnet --effort high
ai-desktop ask --agent reviewer --model opus --effort high
```

`ai-desktop login` otwiera menu: Claude, ChatGPT / Codex albo Anuluj. `login --claude` i `login --codex` pomijają menu. Po udanym logowaniu wybrana usługa staje się aktywna, a nieudane
lub przerwane logowanie jej nie zmienia. Ani `settings`, ani `login` nie zmieniają `permissions.toml`. `--v` pokazuje diagnostykę bez przechwytywania danych logowania.

Usługa jest wspólna dla wszystkich agentów. Po zmianie usługi agent wraca do modelu i poziomu zapisanego dla tej usługi; instrukcje, Skille i dostęp pozostają wspólne.

| Usługa | Modele | Poziomy rozumowania |
|---|---|---|
| Claude | `default`, `best`, `fable`, `opus`, `sonnet`, `haiku`, `opusplan` | `default`, `low`, `medium`, `high`, `xhigh`, `max` |
| Codex | `default` lub identyfikator postaci `[a-z][a-z0-9._-]{0,95}` | `default`, `low`, `medium`, `high`, `xhigh` |

`default` oznacza wybór klienta. Dostępność modelu zależy od konta i klienta: nie ma automatycznego przejścia na inny model lub usługę. Dla `xhigh` i `max` czas oczekiwania na odpowiedź to 260 s zamiast 110 s.

## Agenci

Agent to zapisana rola: usługa, model, poziom, dostęp (`changes` lub `read-only`), instrukcje do 4 KiB i dowolną liczbę Skilli (razem do 104 KiB). Limit to 24 profile. Agent `read-only` nie zapisuje, nie usuwa i nie dostaje
poleceń z listy `change`.

```fish
ai-desktop agents setup                  # dodaje brakujące role i Skille, nie zmienia Twoich ustawień
ai-desktop agents                        # menu w terminalu, poza nim lista
ai-desktop agents list
ai-desktop agents show reviewer
ai-desktop agents create python-review --model opus --effort high --access read-only --instructions 'Sprawdzaj kod Python.'
ai-desktop agents use python-review
ai-desktop agents update python-review --skill code-review
ai-desktop agents update python-review --no-skills
ai-desktop agents copy python-review kopia
ai-desktop agents use default
ai-desktop agents remove python-review   # tylko nieaktywny, nie domyślny
```

Po `agents setup`:

| Profil | Skill | Dostęp |
|---|---|---|
| `default` | `concise-assistant` | `changes` |
| `diagnostic` | `cachyos-diagnostics` | `read-only` |
| `programmer` | `code-development` | `changes` |
| `reviewer` | `code-review` | `read-only` |
| `analyst` | `project-analysis` | `read-only` |

Profile leżą w prywatnym `~/.config/ai-desktop/agents.json`. W `material` i `project` polecenia `/ustawienia`, `/agent NAZWA`, `/model ALIAS` i `/sila POZIOM` zmieniają następne pytanie, gdy nic nie działa. Zmiana usługi wymaga nowej rozmowy.

Program prowadzi tylko jedną rolę na pierwszym planie. Natywni podagenci i serwery MCP klienta są wyłączone.

## Skille

```fish
ai-desktop skills                 # lista; własne oznaczone
ai-desktop skills show NAZWA
ai-desktop skills validate
ai-desktop skills menu            # przypisania aktywnego agenta
ai-desktop skills new NAZWA       # ~/.config/ai-desktop/skills/NAZWA/SKILL.md
```

Skill to instrukcja do 96 KiB, bez narzędzi, skryptów i dodatkowych plików. Pakiet zawiera ogólne Skille dla CachyOS (`cachyos-*`), programowania i analizy oraz `desktop-documentation`, który kieruje do dokumentacji pulpitu; Skille CachyOS celowo nie opisują pulpitów i kompozytorów. Własny Skill leży poza repozytorium i pakietem, a o tej samej nazwie ma pierwszeństwo przed ogólnym.
Nazwa to małe litery, cyfry i myślniki. Nie wpisuj sekretów: treść przypisanego Skilla trafia do modelu. Ścieżka wpisana w Skillu lub instrukcji agenta liczy się jak podana przez Ciebie, więc model może ją przeczytać bez rozpoznania; polityka i `deny` nadal decydują.

Zmiana przypisanych Skilli może wymagać nowej rozmowy, bo zapisana rozmowa jest związana z konfiguracją agenta.

## Rozmowa w `ask`

Uruchom `ai-desktop ask` raz i pracuj dalej: po odpowiedzi wraca `>`. Strzałki edytują wiersz i przywołują wcześniejsze wiadomości. Pusta linia lub dwa Ctrl-D kończą rozmowę. Ctrl-C w trakcie oczekiwania przerywa tylko bieżącą turę. `ask --question "..."` to jedno pytanie.

**Zgoda.** Jedno `Przekazać żądanie do Claude [t/n]:` na rozmowę. Ponownie po zmianie uprawnień, usługi lub agenta. Żyje tylko w pamięci procesu, więc po wznowieniu rozmowy program pyta znowu. Zapisy i zmiany mają własne zgody.

**Pamięć ma dwie warstwy:**

1. Ostatnie 32 wymiany, razem do 128 KiB (pytanie do 128 KiB, odpowiedź do 8 KiB z widocznym ucięciem).
2. Pamięć zadania: do 40 wpisów (po 400 B, razem 10 KiB) z celem, kryterium, próbami, wartościami, korektami i decyzjami. Model proponuje aktualizacje w odpowiedzi, a przy braku limitu najpierw wypadają najstarsze wpisy niechronione. Program sam dopisuje fakty: co uruchomił i z jakim kodem, co zastosował, co odrzucono.

Pamięć nie daje uprawnień. Przy cofnięciu dostępu do pliku zależne od niego odpowiedzi i wpisy modelu są usuwane przed następnym pytaniem, a program o tym informuje. Program nie pamięta tur starszych niż 8, poza tym co trafiło do pamięci zadania.

## Zapisane rozmowy

Domyślnie nic nie jest zapisywane. Na końcu rozmowy `ask` program pyta, czy ją zachować; dopiero `t` tworzy lokalny zapis. Następne `ai-desktop ask` proponuje rozmowy tego samego agenta. `--session new` zapisuje od początku, `--session ID` wznawia (także `material`, `project`).

```fish
ai-desktop sessions          # menu, poza terminalem lista
ai-desktop sessions list
ai-desktop sessions show ID
ai-desktop sessions resume ID
ai-desktop sessions remove ID
```

Zapis leży w `~/.local/state/ai-desktop/conversations` (katalog `0700`, pliki `0600`, do 64 KiB na plik, 128 rozmów). Zawiera ograniczony tekst rozmowy, pamięć i metadane, nie całe materiały, choć odpowiedź może cytować plik. Wznowienie ponownie sprawdza politykę, plik lub projekt i konfigurację agenta. Zgody, autoryzacje zapisu i sesja logowania nie są przywracane. Rozmowa otwarta w innym procesie nie zostanie usunięta. `ai-desktop stop` dotyczy tymczasowej sesji klienta, nie zapisanych rozmów.

## Polecenia i zmiany na żywym systemie

Opis `[commands]` w `permissions.toml` jest w [README](../README.md#polecenia-commands), a granice w [modelu zagrożeń](THREAT_MODEL.md#4-granica-poleceń). W skrócie: `allow` działa w piaskownicy bez pytania, do 16 poleceń w kroku i `command_rounds` na pytanie. `change` działa na żywym systemie po wpisaniu `tak`, z testem i wycofaniem. W `systemctl --user` piaskownica nie ma dostępu do sesji użytkownika, więc takie polecenia mogą nic nie zwrócić.

## Samodzielne rozpoznanie w `ask`

Nie musisz podawać plików. Model w jednej zgodzie wykonuje skończony ciąg kroków:

1. `list`: elementy (nazwa, typ, rozmiar) do czterech katalogów.
2. `find`: wyszukiwanie po fragmencie nazwy, bez treści.
3. `read`: tylko ścieżki pokazane w krokach wyżej albo wpisane przez Ciebie. Zgadnięta ścieżka to `discovery-required`.

`deny` działa pierwsze: nazwa objęta `deny` nie jest wymieniana, liczona ani wysyłana. Odmowy mają stałe kody i nie zawierają tekstu systemu. Przekroczenie budżetu nazywa klucz; program nie wysyła wybranej części jako całości. Budżety są w tabeli w [README](../README.md#pierwsze-uruchomienie-i-uprawnienia). Każdy krok i poprawka to dodatkowe żądanie do modelu.

W `version = 2` nie ma ukrytej listy nazw. Jeśli `read` obejmuje `~/.config/ai-desktop/` i nie ma tego w `deny`, model może tam czytać (szablon to wyklucza).

**Dowiązania w `version = 2`.** Dowiązanie jest śledzone tylko gdy jest jedno, należy do Ciebie, wskazuje zwykłą nazwę katalogu obok (bez `/`, `.`, `..`), katalog nadrzędny nie jest zapisywalny dla innych, a dowiązanie i cel są dozwolone. Stan jest sprawdzany przy odczycie i przed każdym wysłaniem. Pętle, łańcuchy, dowiązania do plików i pliki specjalne to `files-symlink`. Ten wyjątek nie dotyczy polityki, `material`, `project` ani zapisu.

## Codex przez subskrypcję ChatGPT

Codex jest opcjonalny i osobno włączany. Traktuj go jako eksperymentalny. Pakiet używa oficjalnego klienta **Codex 0.166.0** ([wydanie](https://github.com/openai/codex/releases/tag/rust-v0.166.0)).

```fish
cd ~/Projekty/ai-desktop
python3 -B scripts/fetch_codex_client.py    # pobiera archiwum, sprawdza SHA-256, nie uruchamia klienta
# przebuduj i zainstaluj pakiet
ai-desktop codex status
ai-desktop login --codex
ai-desktop ask --agent reviewer
ai-desktop codex logout --allow-chatgpt-logout
```

W `[network]` ustaw `codex = true`: pozwolenia Claude i Codexa są osobne, a polityka nie jest zmieniana za Ciebie. Skrypt zapisuje `packaging/arch/codex` i nie nadpisuje istniejącego pliku.

Logowanie to oficjalny przepływ „Sign in with ChatGPT” obsługiwany przez zaufany komponent konta usługi. Nie kopiuje istniejących poświadczeń i nie używa klucza API. Tokeny leżą w `/var/lib/ai-desktop/codex` i zostają po odinstalowaniu pakietu, chyba że wylogujesz; wylogowanie próbuje też cofnąć poświadczenie i mówi, jeśli nie ma potwierdzenia.

Klient dostaje puste `HOME` i `CODEX_HOME`, pusty katalog roboczy i brak zamontowanego magazynu tokenów. Krótkotrwały token idzie prywatnym kanałem do procesu klienta i nie trafia do pytania ani argumentów. Proxy przepuszcza tylko `api.openai.com`. Powłoka, agenci, MCP, wykrywanie Skilli i wyszukiwarka klienta są wyłączone. Nieoczekiwane zdarzenie narzędzia, nieudana tura lub przekroczony budżet kończą żądanie bez ponawiania. Odrzucenie zdarzenia nie cofnie skutku, który już nastąpił, dlatego izolacja jądra pozostaje konieczna.

Lista modeli w menu pochodzi od klienta, uruchamianego w zamkniętej piaskownicy bez sieci i konta. Nieznany identyfikator jest odrzucany przez klienta jako `codex-model-unavailable`. Codex nie ma wyszukiwania internetowego: wybór tej usługi usuwa tę możliwość.

## Starsze polecenia

`claude` i `codex` są wypisane w `ai-desktop --help-all`. Do codziennej pracy używaj `settings` i `login`. `doctor` pokazuje krótkie metadane, a `doctor --v` raport JSON; nie sprawdza logowania ani działania modelu.
