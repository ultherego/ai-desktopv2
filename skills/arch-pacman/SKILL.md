---
name: arch-pacman
description: Rozwiązuje błędy pacmana i aktualizacji w Arch Linux. Użyj przy konfliktach plików, uszkodzonych pakietach, blokadzie bazy, błędach pobierania, przerwanej aktualizacji albo sprzątaniu pamięci pakietów.
---

# Pacman w Arch Linux

## Procedura

1. Przeczytaj dokładny komunikat. Jeśli problem zaczął się po aktualizacji, sprawdź `/var/log/pacman.log`.
2. Odczytaj stan. Tylko odczyt: `pacman -Qi NAZWA`, `pacman -Qo /ścieżka/pliku`, `pacman -Qk NAZWA`, `pacman -Qdt` (osierocone), `pacman -Si NAZWA`.
3. Dobierz krok z sekcji poniżej i opisz go jako plan.
4. Arch wspiera tylko pełne aktualizacje. Nie proponuj częściowych (`pacman -Sy NAZWA`).

## Błędy

- **`failed to commit transaction (conflicting files)`**: pacman celowo nie nadpisuje plików. Ustal właściciela przez `pacman -Qo` i skąd plik się wziął. Program zainstalowany ręcznie, np. przez `make install`, trzeba odinstalować.
- **`invalid or corrupted package`**: usuń częściowe pobrania `find /var/cache/pacman/pkg/ -iname "*.part" -delete`. Tę samą przyczynę może mieć nieaktualny `archlinux-keyring`.
- **`unable to lock database`**: blokada `/var/lib/pacman/db.lck` może zostać po przerwaniu. Usuń ją dopiero wtedy, gdy na pewno nie działa żaden pacman.
- **`Not found in sync db`, `Target not found`, `Failed retrieving file`**: sprawdź, czy pakiet istnieje i czy jego repozytorium jest włączone (np. `multilib`). Potem `pacman -Syu`, a jeśli to nie pomoże, `pacman -Syyu`.

## Przerwana aktualizacja i system, który nie startuje

1. Uruchom nośnik instalacyjny, zamontuj system i ESP, wejdź przez `arch-chroot`.
2. W `/var/log/pacman.log` znajdź pakiety z przerwanej operacji.
3. Powtórz dokładnie tę aktualizację: `pacman -Syu` z pełną listą tych pakietów. Dzięki temu uruchomią się właściwe skrypty.

## Sprzątanie

- `paccache -r` (pakiet `pacman-contrib`) zostawia trzy ostatnie wersje. `paccache.timer` robi to co tydzień.
- `pacman -Sc` usuwa pakiety niezainstalowane. `pacman -Scc` czyści wszystko i uniemożliwia powrót do wersji z pamięci: uprzedź o tym.
- Pliki `.pacnew` to nowe wersje konfiguracji, których pacman nie nadpisał. Scala je użytkownik.

Źródło: Arch Wiki, <https://wiki.archlinux.org/title/Pacman>.
