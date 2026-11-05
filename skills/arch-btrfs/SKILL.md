---
name: arch-btrfs
description: Diagnozuje zajętość i błędy Btrfs. Użyj przy ENOSPC, niejasnym zużyciu miejsca, błędach sum kontrolnych albo ocenie potrzeby balance lub scrub.
---

# Diagnostyka Btrfs

## Ustal zakres

Ustal punkt montowania, liczbę urządzeń, objaw i ostatnią zmianę. Nie zakładaj, że problem dotyczy `/`. Migawki i podwolumeny mogą współdzielić dane; odczyt `df` lub `du` nie wystarcza do wyjaśnienia całej alokacji.

## Odczyt i interpretacja

Wykorzystuj tylko odczyty dozwolone przez kontroler. `btrfs filesystem usage PUNKT`, `btrfs filesystem show PUNKT`, `btrfs device stats PUNKT` i wąski fragment dziennika jądra mogą wymagać uprawnień lub danych od użytkownika. Widok montowań piaskownicy nie dowodzi układu hosta. Odmowa dostępu jest brakującym dowodem, nie awarią Btrfs.

| Obserwacja | Następny krok |
|---|---|
| Duża różnica między przydzielonym a użytym miejscem | Rozdziel dane, metadane i nieprzydzieloną przestrzeń; ustal profile i udział migawek. Sama różnica nie uzasadnia balance. |
| Usunięcie plików nie zwalnia miejsca | Sprawdź, czy ich extenty pozostają w migawkach; przedstaw zakres ewentualnego usuwania osobno. |
| ENOSPC przy wolnym miejscu w `df` | Sprawdź wolne miejsce robocze i alokację metadanych; nie uruchamiaj pełnego balance. |
| Błędy I/O, brak urządzenia, rosnące liczniki błędów | Zatrzymaj strojenie; priorytetem są kopia danych i diagnoza urządzeń. Nie zeruj liczników. |
| Brak danych o układzie lub profilu | Zakończ diagnozę wskazaniem brakującego odczytu; nie zgaduj naprawy. |

## Ewentualna zmiana

- Balance dobieraj do ustalonej przyczyny. Filtr `usage=0` dotyczy pustych grup bloków; inne progi wymagają uzasadnienia na podstawie zajętości. Pełny balance przenosi dane i metadane całego systemu plików, obciąża I/O i potrzebuje miejsca roboczego. `--bg` nie ogranicza tego zakresu.
- Scrub sprawdza sumy kontrolne i może naprawiać z poprawnej repliki. Nie zastępuje pełnej kontroli struktur. Nawet scrub z `-r` na systemie zamontowanym do zapisu może powodować zapisy; nie przedstawiaj go jako zwykłego odczytu.
- Nie proponuj `btrfs check --repair` ani zmiany profili jako ogólnej naprawy. Nie defragmentuj całego drzewa w celu odzyskania miejsca: może zerwać współdzielenie extentów.
- Każda operacja wymaga osobnego planu, zgody, oceny obciążenia i testu wyniku. Przerwanie balance nie cofa już wykonanej relokacji; nie obiecuj rollbacku, którego nie ma.

## Wynik

Podaj przyczynę lub hipotezę, dowody z zajętości i błędów oraz jeden następny krok. Po zatwierdzonej operacji porównaj te same odczyty i status operacji; sam kod zakończenia nie dowodzi rozwiązania problemu.

## Źródła

- [Balance: filtry, koszt i ENOSPC](https://btrfs.readthedocs.io/en/latest/btrfs-balance.html#filters).
- [Scrub: zakres kontroli i naprawy](https://btrfs.readthedocs.io/en/latest/btrfs-scrub.html#description).
