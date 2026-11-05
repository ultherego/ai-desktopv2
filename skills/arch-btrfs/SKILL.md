---
name: arch-btrfs
description: Diagnozuje system plików Btrfs w Arch Linux. Użyj przy braku miejsca mimo wolnej przestrzeni, błędach danych, scrubie, balance, kompresji, migawkach albo wolnym montowaniu.
---

# Btrfs w Arch Linux

## Brak miejsca

1. `df` i `du` podają złe wartości na Btrfs. Użyj `btrfs filesystem usage /` (wymaga roota) albo `btrfs filesystem df /`.
2. Sprawdź migawki: dane współdzielone z migawką nie zwalniają miejsca po usunięciu pliku.
3. Dużo przydzielonych, a pustych bloków: zaproponuj `btrfs balance start --bg /`, stan: `btrfs balance status /`. Na jednym dysku balance służy głównie do tego.

## Podejrzenie błędów danych

1. Sprawdź `journalctl -k -b` pod kątem `BTRFS`.
2. `btrfs scrub start /`, stan: `btrfs scrub status /`. Sprawdza całe urządzenie, nawet gdy wskażesz podwolumen. Długi scrub może mocno nagrzać dysk NVMe bez dobrego chłodzenia.
3. Co miesiąc: `btrfs-scrub@-.timer` z pakietu `btrfs-progs`.

## `btrfs check`

Działa na odmontowanym systemie plików, a jego naprawa jest wciąż niedojrzała. **Nie proponuj `btrfs check --repair` jako pierwszego kroku.** Najpierw kopia danych i dokumentacja: <https://btrfs.readthedocs.io/>.

## Inne objawy

- Długie montowanie dużego wolumenu: `x-systemd.mount-timeout` w `fstab`.
- Ciągła aktywność dysku w spoczynku: `discard=async` jest domyślne od jądra 6.2; limit w `/sys/fs/btrfs/UUID/discard/iops_limit`.
- `open_ctree failed`: przy kilku dyskach może oznaczać brak jednego z nich.

## Ustawienia

- Kompresja: `compress=zstd` działa na nowe zapisy. Istniejące pliki wymagają `btrfs filesystem defragment -czstd`, co przy migawkach może zwiększyć zajęte miejsce. Rzeczywisty stopień: `compsize`.
- Bazy danych i obrazy maszyn: `chattr +C` na pustym katalogu wyłącza kopiowanie przy zapisie dla nowych plików.
- Migawki nie są rekurencyjne: zagnieżdżony podwolumen będzie w nich pusty. Nie są też kopią zapasową.

Źródło: Arch Wiki, <https://wiki.archlinux.org/title/Btrfs>.
