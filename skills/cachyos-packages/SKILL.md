---
name: cachyos-packages
description: Pacman i repozytoria CachyOS: błędy aktualizacji, klucze, serwery lustrzane, repozytoria v3, v4 i znver4.
---

# Pakiety i repozytoria CachyOS

## Repozytoria

CachyOS ma pakiety Archa przebudowane pod poziomy procesora: `x86-64-v3`, `x86-64-v4` i Zen 4/5. Bloki w `/etc/pacman.conf`:

- v3: `[cachyos-v3]`, `[cachyos-core-v3]`, `[cachyos-extra-v3]`;
- v4: `[cachyos-v4]`, `[cachyos-core-v4]`, `[cachyos-extra-v4]`;
- Zen 4/5: `[cachyos-znver4]`, `[cachyos-core-znver4]`, `[cachyos-extra-znver4]`;
- zawsze też `[cachyos]`, `[core]`, `[extra]`, `[multilib]`.

v4 i znver4 używają `/etc/pacman.d/cachyos-v4-mirrorlist`. Przed oceną najpierw odczytaj `/etc/pacman.conf`, nie zakładaj poziomu.

Sprawdzenie procesora:
- `/lib/ld-linux-x86-64.so.2 --help | grep supported` (dla v4 szukaj `x86-64-v4 (supported, searched)`);
- `gcc -march=native -Q --help=target 2>&1 | grep -Po "^\s+-march=\s+\K(\w+)$"` (wynik `znver4` lub `znver5` oznacza Zen 4/5).

Procesory Intel od 12. generacji mogą zgłaszać v4, ale nie obsługują AVX-512: traktuj je jako v3.

Repozytorium `[cachyos]` zawiera własnego `pacman`. Szukanie pakietów: <https://packages.cachyos.org/>. Zmiana poziomu to przebudowa całego systemu i odrębna procedura z <https://wiki.cachyos.org/features/optimized_repos/>: nigdy jako efekt uboczny.

## Błędy pacmana (według wiki)

| Komunikat | Co zaproponować po kolei |
|---|---|
| `signature is invalid` | `sudo pacman -Syu`, potem `sudo cachyos-rate-mirrors`, potem reset kluczy |
| `404 Not Found` | `sudo pacman -Syu`, ponów instalację |
| `unable to lock database` | sprawdź, czy nie działa inny pacman; dopiero wtedy `sudo rm /var/lib/pacman/db.lck` |
| `failed retrieving file`, `Connection timed out` | `sudo cachyos-rate-mirrors`, potem `sudo pacman -Syu` |
| `local is newer than` | `sudo pacman -Syuu` (dopuszcza obniżenie wersji) |
| `failed to commit transaction (conflicting files)` | ustal właściciela pliku, usuń konflikt świadomie; nie używaj `--overwrite` odruchowo |
| `could not remove` | czyszczenie pamięci pakietów w CachyOS Hello |

CachyOS Hello, karta Troubleshooting, ma przyciski: Reset keyrings, Clear package cache, Remove db lock.

Pełny reset kluczy to operacja ostateczna:
```
sudo rm -rf /etc/pacman.d/gnupg/
sudo pacman-key --init
sudo pacman-key --populate
sudo pacman-key --recv-keys F3B607488DB35A47 --keyserver keyserver.ubuntu.com
sudo pacman-key --lsign-key F3B607488DB35A47
sudo rm -R /var/lib/pacman/sync
```

## Serwery lustrzane

`sudo cachyos-rate-mirrors` testuje serwery i zapisuje `/etc/pacman.d/cachyos-mirrorlist`. Warto po spowolnieniach aktualizacji i błędach pobierania.

## Dobre praktyki

- Aktualizuj cały system (`pacman -Syu`), nie częściowo. Polecenia aktualizacji i usuwania to zmiany systemu: opisuj je jako plan.
- Pamac według wiki może uszkodzić klucze; zalecany jest `pacman`, a z graficznych CachyOS Package Installer.
- Ostrożnie z AUR: użytkownik powinien przeczytać PKGBUILD.
- Przed aktualizacją sprawdź ogłoszenia o znanych problemach wymagających ręcznej interwencji.
- `pacman -Qk NAZWA` sprawdza pliki pakietu. Tylko odczyt: `pacman -Q`, `-Qi`, `-Qs`, `-Qu`.

Źródła: <https://wiki.cachyos.org/cachyos_basic/faq/>, <https://wiki.cachyos.org/features/optimized_repos/>.
