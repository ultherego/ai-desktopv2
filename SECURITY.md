# Bezpieczeństwo

AI Desktop jest wersją rozwojową. Nie przeszedł niezależnego przeglądu bezpieczeństwa. Zielone testy nie są certyfikatem izolacji.
Pełny opis architektury i zagrożeń: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). Lista ograniczeń:
[docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

## Zgłaszanie problemu

Luki bezpieczeństwa zgłaszaj **prywatnie**: na stronie repozytorium <https://github.com/ultherego/ai-desktopv2> wybierz
Security → „Report a vulnerability”. Zgłoszenie widzi tylko opiekun projektu. Zwykłe błędy i pytania możesz otworzyć jako
publiczne zgłoszenie (issue).

Publiczne zgłoszenie jest widoczne dla wszystkich, więc **nie umieszczaj w nim szczegółów wrażliwych**: tokenów, kodów logowania,
treści prywatnych plików, wyniku `--v`, działającego exploita ani danych swojego komputera. Opisz wersję pakietu, ogólny objaw i
stały komunikat błędu.

## Granice zaufania

| Element | Działa jako | Co widzi i co może |
|---|---|---|
| Program `ai-desktop` (kontroler) | Twój zwykły użytkownik, odmawia pracy jako root | Czyta politykę i pliki w jej granicach, pyta o zgodę, wykonuje zatwierdzone zapisy. |
| Pomocnik sesji | root, przez `sudo`, stały plik z pakietu | Tylko uruchamia i zatrzymuje jednostki Twojej sesji. Nie dostaje pytań, danych ani poświadczeń. |
| Usługa analizy | konto systemowe `ai-desktop`, bez logowania i bez uprawnień | Jednostka systemd z ograniczeniami, trzyma proxy i uruchamia klienta. |
| Klient Claude Code 2.1.312 | to samo konto, w Bubblewrap | Własny katalog logowania `/var/lib/ai-desktop/claude`, puste HOME i katalog roboczy, bez narzędzi (poza `WebSearch` w trybie web), bez bezpośredniej sieci. |
| Proxy | usługa, poza piaskownicą klienta | Przepuszcza tylko TLS na porcie 443 do `api.anthropic.com`, `claude.ai`, `platform.claude.com`, na adresy publiczne. |
| Model | u dostawcy | Zwraca tekst i propozycje. To niezaufane dane. |
| Polecenia z `allow` | Twój użytkownik, w osobnej piaskownicy | System plików tylko do odczytu, ukryte `/home`, `/root`, `/run/user`, bez sieci. Gniazda Unix hosta nie są izolowane. |
| Polecenia z `change` | Twój użytkownik, **bez piaskownicy** | Działają na żywym systemie po wpisaniu `tak`. |
| Testy na kopii | Twój użytkownik, w piaskownicy | Kopia w pamięci, bez sieci, katalogu domowego i dostępu do projektu. |

Klient nie ma zamontowanego Twojego katalogu domowego. Dane dostaje jako tekst, przekazany przez kontroler po sprawdzeniu polityki.

Polityka `~/.config/ai-desktop/permissions.toml` jest źródłem uprawnień dla `ask`, `material` i `project`. Musi należeć do Ciebie i mieć prawa `0600`.
Jest wiązana z pytaniem: zmiana pliku w trakcie przerywa bieżącą operację, a kolejne pytanie wymaga nowej zgody. `task run` nie korzysta z polityki:
pliki wskazujesz ręcznie w poleceniu.

Wyczerpanie budżetu, błędna odpowiedź modelu, zmiana pliku lub brak piaskownicy kończą się odmową. Program nie ponawia żądania sam i nie
przechodzi na inną usługę, klienta na hoście ani klucz API.

## Co może, a czego nie może wynik modelu

Odpowiedź modelu jest sprawdzana według zamkniętej listy akcji. Tekst odpowiedzi jest tylko wyświetlany.

**Może zaproponować:**

- listowanie i wyszukiwanie **nazw** w katalogach z `read`, w ramach budżetów;
- odczyt plików z `read`, ale tylko takich, które pokazało rozpoznanie albo które wpisałeś;
- uruchomienie polecenia pasującego do Twojego szablonu z `allow`;
- zapis lub utworzenie pliku w `write`, usunięcie do kosza, zmianę z listy `change` z testem i wycofaniem;
- wyszukiwanie w Internecie, gdy `web = true`, oraz pobranie pogody, gdy `weather = true`.

**Nie może:**

- nadać sobie uprawnień, zmienić polityki ani wyłączyć zgody;
- uruchomić powłoki, dowolnego programu, polecenia spoza szablonu ani wybrać podkomendy w miejscu ustalonym przez szablon;
- odczytać ani zapisać niczego objętego `deny` lub leżącego poza zakresem;
- zapisać pliku bez Twojego `t` po pełnym diffie;
- wykonać zmiany z `change` bez Twojego `tak`;
- wskazać dowolnego adresu URL ani hosta.

Ważne: **treść pliku lub wynik polecenia może skłonić model do kolejnych dozwolonych odczytów**, a ich wyniki trafiają do dostawcy. Granicą są
`permissions.toml` i budżety na pytanie, nie zdrowy rozsądek modelu. Odmów dostępu do tego, czego nie chcesz wysłać.

## Zgody

| Działanie | Zgoda |
|---|---|
| Rozmowa `ask`: pytania, odczyty, rozpoznanie, polecenia z `allow` | Jedno `Przekazać żądanie do Claude [t/n]:` na rozmowę. Ponownie po zmianie uprawnień, usługi lub agenta. Zgoda żyje w pamięci procesu. |
| Zapis lub utworzenie pliku | Pełny diff i `[t/n]` za każdym razem. |
| Usunięcie plików | Lista plików i `[t/n]`. Pliki trafiają do kosza zadania. |
| Zmiana z `change` | Pokazana zmiana, test i wycofanie, wpisanie całego słowa `tak`. Wycofanie po błędzie też wymaga `tak`. |
| Cofnięcie i przywrócenie | Osobne `[t/n]`. |
| `material`, `project` | Zgoda na pytanie. Analiza całego pliku: jedna zgoda na zadanie, ważna 30 minut. Kolejny etap projektu: osobne `[t/n]`. |
| `task run` | Osobno wysłanie i zapis. |

Pytanie o zgodę czyści wcześniej wpisane znaki z terminala, więc wklejony tekst nie zatwierdza działania, którego jeszcze nie widziałeś.
Bez terminala zgody nie ma: program odmawia. Nie ma trybu bez nadzoru. Zgoda nie chroni przed innym procesem sterującym tym samym terminalem.

Po zastosowanej zmianie model może podjąć następny krok bez nowego pytania od Ciebie (najwyżej 5 kroków), ale każdy kolejny zapis znów wymaga
zgody.

## Co trafia do dostawcy

Wybrany dostawca (Anthropic, a przy Codexie OpenAI) dostaje:

- Twoje pytanie i wklejki;
- stałe instrukcje programu oraz instrukcje agenta z treścią przypisanych Skilli, także własnych;
- wzorce reguł `read` i `write`, szablony `allow` i `change`, dostępność systemd, pogody i Internetu. Reguły `deny` nie są wysyłane;
- nazwy, typy i rozmiary z kroków rozpoznania;
- treść czytanych plików z **bezwzględnymi ścieżkami**, które mogą zawierać nazwę użytkownika;
- wyniki poleceń, także tych z `allow` uruchamianych bez pytania (filtr `deny` ich nie obejmuje);
- nazwy i stany jednostek systemd oraz wynik pogody;
- ostatnie wymiany rozmowy i pamięć zadania.

Do tego dochodzi kontekst techniczny oficjalnego klienta i dane logowania, których program nie czyta i nie pokazuje. `--v` pokazuje dane programu, nie
zapis całego szyfrowanego ruchu.

Inne cele sieciowe: `weather = true` ujawnia nazwę miejscowości i adres IP serwisowi Open-Meteo. `web = true` wysyła samo pytanie do wyszukiwania po
stronie dostawcy; treść pytania może trafić do zapytań wyszukiwarki, a pliki i dane lokalne nie są dołączane.

Wysłanych danych nie można wycofać.

## Czego nie gwarantujemy

- **Ochrony przed innym procesem tego samego użytkownika**, rootem, przejętym jądrem ani złośliwym systemem.
- **Ochrony tokenu klienta przed samym klientem.** Oficjalny klient ma dostęp do własnego profilu logowania. Przejęty klient mógłby wysłać go do dozwolonego
  hosta. Proxy nie widzi treści TLS i nie filtruje ścieżek ani danych. Nie ma ogólnego filtra wywołań systemowych dla klienta.
- **Bezpieczeństwa listy `allow`.** Piaskownica nie izoluje gniazd Unix hosta, więc polecenie mogące zmieniać stan przez D-Bus lub Dockera nie jest zatrzymane,
  a działa bez pytania. Wpisuj tu tylko polecenia tylko do odczytu.
- **Rozpoznawania sekretów.** W `version = 2` nie ma ukrytego filtra nazw ani skanera kluczy prywatnych. Sekret w dozwolonym drzewie bez `deny` może trafić do dostawcy.
- **Niezmienności klienta.** Sprawdzana jest zgłaszana wersja, a nie suma kontrolna pliku w pakiecie. Sumę sprawdzasz przy pobieraniu.
- **Poprawności zmian.** Test na kopii dotyczy tylko wybranego sprawdzenia, `text` nie bada składni ani działania.
- **Atomowości.** Zadanie wieloplikowe i aktualizacja pakietu nie są jedną transakcją. Nie ma gwarancji przy utracie zasilania.
- **Prawdziwości odpowiedzi modelu, wyników wyszukiwania ani źródeł.**
- **Sprawdzenia na prawdziwych awariach:** wygaśnięcie i odświeżenie logowania oraz rzeczywiste awarie sieci nie zostały sprawdzone z oficjalnym klientem.

## Zasady dla osób rozwijających projekt

Nie dodawaj do repozytorium sekretów, plików logowania, binarek klienta ani danych konkretnego komputera. Zmiany izolacji, uprawnień, systemu plików, sieci lub
logowania wymagają testu na prawdziwym CachyOS lub Arch oraz opisu kompromisu w [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). Bramą testów jest `scripts/verify.py`.
