---
name: code-development
description: Przygotowuje poprawkę kodu z uzasadnionym testem i weryfikacją. Użyj przy naprawie błędu, zmianie zachowania lub poleceniu „zmień i przetestuj” w udostępnionym projekcie.
---

# Zmiana kodu z weryfikacją

## Ustal kontrakt

1. Wyprowadź z zadania oczekiwane zachowanie: konkretne wejście, obecny wynik i wymagany wynik. Zachowaj ustalenia rozmowy. Dopytaj tylko, gdy brak informacji zmienia poprawność lub zakres zmiany.
2. Odczytaj reguły projektu, manifest, zmieniany kod, jego wywołania i najbliższe testy. Reguły projektu nie zmieniają uprawnień aplikacji. Jeśli brakuje zależności lub pliku, wskaż tę lukę zamiast zakładać jego zawartość.
3. Ustal przyczynę przed poprawką. Jeżeli dostępny wykonawca potrafi odtworzyć błąd na kopii, sprawdź go. W przeciwnym razie opisz dowód z kodu i oznacz reprodukcję jako niewykonaną.

## Przygotuj zmianę

- Zachowaj istniejące API, format danych i styl projektu, chyba że zadanie wymaga ich zmiany. Popraw wspólną przyczynę błędu; sprawdź inne wywołania tego samego mechanizmu.
- Dodaj test konkretnego wejścia lub ryzyka, który odróżnia stare zachowanie od poprawnego. Test ma sprawdzać wynik lub niepożądany skutek, a nie powtarzać implementację. Uwzględnij odmowę lub awarię, jeśli zmiana dotyka granicy dostępu albo zapisu.
- Nie zmieniaj oczekiwań testu tylko po to, aby uzyskać PASS. Nie dokładaj niezależnego refaktoru ani nowych zależności bez potrzeby wynikającej z zadania.

## Dobierz rzeczywiste sprawdzenie

AI Desktop sprawdza wybrane pliki na odizolowanej kopii. Dostępne są:

| Check | Co może potwierdzić |
|---|---|
| `python-unittest` | Testy znalezione w przekazanej kopii, z ograniczeniami środowiska. |
| `python-syntax`, `bash-syntax`, `fish-syntax` | Składnię odpowiedniego języka. |
| `json`, `toml` | Poprawność parsowania danego formatu. |
| `text` | Tylko integralność UTF-8; bez składni i działania. |

Test Python nie może tworzyć procesów, wątków ani gniazd. Kopia nie ma sieci, HOME ani całego projektu, którego nie przekazano. Nie deklaruj uruchomienia `pytest`, `go test`, `npm test` ani instalacji zależności: obecny wykonawca tego nie obsługuje. Wynik składni nie zastępuje testu zachowania. Gdy brakuje odpowiedniego sprawdzenia, nazwij niezweryfikowaną część i ryzyko.

## Przegląd i zakończenie

Przed zapisem sprawdź kompletny diff: wymaganie, niezamierzone zmiany, test i zachowanie formatu plików. Zapis wykonuje kontroler po osobnej zgodzie. Odmowa lub nieudany check kończy tę próbę; nie obchodź ich innym trybem ani poleceniem na hoście.

W wyniku podaj: co zmieniono i dlaczego, sprawdzenie faktycznie wykonane przez kontroler, jego zakres i rezultat, stan zapisu oraz pozostałe ograniczenie. Polecenie cofania cytuj z wyniku kontrolera, nie twórz własnego identyfikatora zadania. Dopóki brak potwierdzenia zapisu, opisuj poprawkę jako propozycję.
