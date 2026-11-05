---
name: arch-zram
description: Wyjaśnia i diagnozuje pamięć wymiany zram w Arch Linux. Użyj przy pytaniach o zram, zswap, zużycie pamięci wymiany albo hibernację.
---

# Zram w Arch Linux

## Procedura

1. Odczytaj stan: `zramctl`, `swapon --show`, `free -h`.
2. Sprawdź, czy zswap jest włączony: `/sys/module/zswap/parameters/enabled`.
3. Ustal objaw: brak pamięci, zacinanie czy tylko pytanie o wartości.
4. Sprawdź, skąd pochodzi konfiguracja, zanim coś zmienisz: `zram-generator`, reguła udev czy ustawienia dystrybucji.

## Najważniejsze zasady

- Włączony zswap nie pozwala skutecznie używać zram, bo działa przed nim jako pamięć podręczna. Wyłącz zswap: `zswap.enabled=0`.
- **Hibernacja na zram nie jest możliwa**, także z urządzeniem zapasowym. Potrzebna jest zwykła pamięć wymiany.
- Urządzenie zram nie rezerwuje pamięci z góry: zajmuje ją dopiero po zapisaniu danych. Duży rozmiar nie oznacza utraconej pamięci.
- Przy zstd można przyjąć ostrożnie współczynnik 1:2, a typowo około 1:3.

## Konfiguracja

`zram-generator` korzysta z mechanizmów systemd. Ręcznie na czas sesji: `zramctl` i `mkswap`, cofnięcie: `swapoff /dev/zram0`, `modprobe -r zram`. Zmiana stała to zmiana systemu: opisz ją jako plan.

Źródło: Arch Wiki, <https://wiki.archlinux.org/title/Zram>.
