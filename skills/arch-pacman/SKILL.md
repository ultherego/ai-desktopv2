---
name: arch-pacman
description: Diagnozuje błędy pacmana, konflikty plików i przerwane aktualizacje. Użyj do rozpoznania przyczyny przed planem naprawy bazy, pakietów lub pamięci podręcznej.
---

# Diagnostyka pacmana

## Dane wejściowe

Ustal dokładne polecenie i komunikat, nazwę pakietu, etap transakcji oraz czy działa inny menedżer pakietów. Jeśli awaria nastąpiła po aktualizacji, potrzebny jest odpowiedni fragment `/var/log/pacman.log`, nie cały raport komputera.

Odczyty wybieraj z aktualnych uprawnień: `pacman -Qi NAZWA`, `pacman -Qk NAZWA`, `pacman -Qo PLIK` i `pacman -Si NAZWA`. Zapytanie `-Si` opisuje lokalną bazę synchronizacji, której aktualność trzeba ustalić osobno. Odmowy nie obchodź przez `sudo`, powłokę ani polecenie z listy zmian.

## Rozpoznaj przyczynę

| Obserwacja | Następny krok |
|---|---|
| Blokada bazy | Sam plik `db.lck` nie dowodzi osierocenia. Ustal aktywną transakcję na hoście; prywatna lista procesów piaskownicy jej nie wyklucza. Bez tego nie proponuj usuwania blokady. |
| Konflikt pliku | Ustal właściciela przez `-Qo` i pochodzenie pliku. Nie usuwaj pliku pakietu ani nie stosuj globalnego `--overwrite`. |
| Niepoprawny podpis lub uszkodzone archiwum | Rozdziel podpis, klucz, czas systemowy i błąd pobrania. Zachowaj weryfikację podpisów; nie wyłączaj `SigLevel`. |
| 404 lub brak pakietu | Zweryfikuj nazwę, włączone repozytorium i stan bazy lub mirrora. Powtarzane odświeżanie nie rozwiąże wyłączonego repozytorium. |
| Przerwanie po rozpoczęciu instalacji | Ustal pakiety i zakończone kroki z logu. Nie uznawaj poprawnego odczytu bazy za dowód spójności plików. |
| `local is newer than` | Porównaj wersje i źródło pakietu; obniżenie wersji wymaga osobnego uzasadnienia. |

## Plan naprawy

Arch wymaga spójnej pełnej aktualizacji; nie proponuj częściowej aktualizacji z samym odświeżeniem baz. Nie używaj `-Syyu`, resetu kluczy ani kasowania pamięci jako automatycznej eskalacji po dowolnym błędzie.

Każda instalacja, usunięcie, downgrade, odblokowanie lub czyszczenie to osobna zmiana systemu: określ pakiety i pliki, zależności, ryzyko, zgodę i sposób odzyskania. Aktualizacja uruchamia skrypty i hooki; nie obiecuj jej atomowego cofnięcia.

Przed czyszczeniem ustal potrzebne wersje do odzyskania. Nie kasuj całej pamięci podręcznej; `.pacnew` wymaga świadomego scalenia, a pakiet osierocony nie musi być zbędny użytkownikowi.

## Weryfikacja i wynik

Sprawdź, czy pierwotny komunikat zniknął, transakcja się zakończyła i odpowiednie pliki oraz funkcja programu są poprawne. Podaj przyczynę z dowodem, zakres faktycznie wykonanej zmiany i pozostałe braki. Bez bezpiecznego planu odzyskania zakończ na diagnozie.

## Źródła

- [pacman: operacje i opcje](https://man.archlinux.org/man/pacman.8.en).
- [Specyfika błędów CachyOS](https://wiki.cachyos.org/cachyos_basic/faq/).
