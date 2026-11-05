---
name: arch-systemd
description: Diagnozuje usługi systemd i dziennik w Arch Linux. Użyj, gdy usługa nie startuje, system wolno się uruchamia lub zamyka albo trzeba bezpiecznie zmienić jednostkę.
---

# Systemd w Arch Linux

## Usługa nie działa

1. `systemctl --failed`: lista nieudanych usług.
2. `systemctl status NAZWA`, potem `journalctl -u NAZWA -b`. Usługi użytkownika: dodaj `--user`.
3. `systemctl cat NAZWA`: plik jednostki razem z nadpisaniami. `systemd-delta`: co w systemie nadpisano.
4. Przy krótko żyjącej usłudze `journalctl -u` może być pusty: znajdź PID w `systemctl status` i szukaj po nim.
5. Dopiero wtedy wskaż przyczynę. Oddziel to, co odczytane, od przypuszczeń.

Do zgłoszeń używaj `journalctl -b` bez `-x`. Pełne wyjście jest cenniejsze niż wybrane fragmenty.

## Dziennik podaje opis, nie nazwę

Linia `Failed to start ...` zawiera zwykle `Description=`, a nie nazwę pliku. Nie szukaj pliku po tych słowach: przejrzyj `systemctl list-units --all` albo dziennik z tego czasu.

## Wolny start lub zamknięcie

- `systemd-analyze blame`, `systemd-analyze critical-chain`.
- Długie zamykanie to zwykle usługa, która się nie kończy. Zawieszone zamknięcie lub uśpienie sprawdź przez `systemctl list-jobs`.
- Problemy z rozruchem: najpierw ustal etap (oprogramowanie płyty, program rozruchowy, initramfs, system).

## Zmiana jednostki

- Nie edytuj plików z pakietów: pacman je nadpisze.
- `systemctl edit NAZWA` tworzy nadpisanie w `/etc/systemd/system/NAZWA.d/`. Cofnięcie: `systemctl revert NAZWA`.
- Listę, np. `ExecStart=`, trzeba najpierw wyczyścić pustym wpisem.
- `systemctl mask` jest ryzykowne, bo blokuje też start jako zależność.
- `systemctl reset-failed` tylko czyści status, nie naprawia przyczyny.

Więcej logów z usługi systemd na czas testu: `Environment=SYSTEMD_LOG_LEVEL=debug` przez `systemctl edit --runtime`.

Źródło: Arch Wiki, <https://wiki.archlinux.org/title/Systemd>.
