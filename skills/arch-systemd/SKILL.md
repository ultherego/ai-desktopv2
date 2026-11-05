---
name: arch-systemd
description: Diagnozuje awarie jednostek systemd oraz wolny rozruch i zamykanie. Użyj do ustalenia przyczyny i przygotowania ograniczonej zmiany jednostki z testem.
---

# Diagnostyka systemd

## Ustal zakres

Ustal nazwę jednostki, menedżer systemowy lub użytkownika, czas objawu i ostatnią zmianę. Opis `Description=` w komunikacie nie musi być nazwą jednostki. Nie restartuj usługi przed zebraniem dowodów.

## Odczyt i decyzje

Wybieraj tylko polecenia pasujące do udostępnionych szablonów: `systemctl --failed --no-pager`, `systemctl status NAZWA --no-pager`, `systemctl cat NAZWA` oraz dziennik tej jednostki z właściwego rozruchu i czasu. Odczyt dziennika nie jest domyślnie dozwolony. Dla menedżera użytkownika potrzebna jest jego sesja; jej brak w piaskownicy nie oznacza awarii usługi.

| Wynik | Co ustalić dalej |
|---|---|
| `not-found` | Zweryfikuj nazwę, zakres i pakiet; nie twórz zastępczej jednostki. |
| `masked` | Ustal, kto i po co ją zamaskował; nie odmaskowuj automatycznie. |
| `failed` lub niezerowy kod procesu | Odczytaj pierwszy istotny błąd, `Result` i konfigurację wraz z drop-inami. Niezerowy kod `systemctl status` jest też informacją o stanie. |
| Błąd zależności | Znajdź pierwszą niespełnioną zależność, zamiast naprawiać usługę zależną. |
| `active`, lecz funkcja nie działa | Sprawdź objaw aplikacji; aktywny proces nie dowodzi jej sprawności. |
| Log niedostępny lub bez pasujących wpisów | Sprawdź rozruch, przedział czasu i menedżera; wskaż lukę w dowodach. |

Przy wolnym starcie zestaw `systemd-analyze blame` z `systemd-analyze critical-chain`. Długi czas jednostki nie dowodzi, że blokowała osiągnięcie celu. Przy wolnym zamykaniu użyj dziennika poprzedniego rozruchu; nie zwiększaj timeoutu bez ustalenia, na co proces czekał.

## Zmiana i cofanie

Przed zmianą zachowaj treść i pochodzenie istniejących nadpisań. Zaproponuj konkretny drop-in, jego pełny diff i oddzielnie skutki przeładowania lub restartu. Nie edytuj jednostki dostawcy w `/usr`.

Cofnięcie ma przywrócić dokładnie zmieniony plik. `systemctl revert NAZWA` usuwa wszystkie lokalne drop-iny jednostki, może usunąć lokalne zastąpienie jej wersji dostawcy i odmaskowuje ją; nie jest cofnięciem ostatniej edycji. `reset-failed` czyści stan błędu, a nie jego przyczynę.

Po zgodzie sprawdź poprawność konfiguracji właściwym dostępnym narzędziem, następnie stan, nowe logi i pierwotny objaw. Sam restart nie jest testem naprawy. Brak dozwolonego wykonania oznacza plan do ręcznego zastosowania, bez deklarowania sukcesu.

## Wynik

Podaj jednostkę i zakres, błąd źródłowy z dowodem, przyczynę lub hipotezę oraz jeden następny krok. Zaznacz, czy zmiana była tylko zaproponowana, czy rzeczywiście wykonana i sprawdzona.

## Źródło

[systemctl: status, cat, edit i revert](https://freedesktop.org/software/systemd/man/latest/systemctl.html).
