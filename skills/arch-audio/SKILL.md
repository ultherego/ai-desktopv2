---
name: arch-audio
description: Diagnozuje dźwięk PipeWire i WirePlumber w Arch Linux. Użyj, gdy nie ma dźwięku, urządzenie nie jest widoczne, dźwięk się przerywa albo słuchawki Bluetooth zmieniają profil.
---

# Dźwięk: PipeWire w Arch Linux

## Procedura

1. Sprawdź usługi użytkownika: `systemctl --user status pipewire pipewire-pulse wireplumber`.
2. Przeczytaj log: `journalctl --user -u pipewire -u pipewire-pulse -u wireplumber -b`.
3. Sprawdź serwer: `pactl info`. Powinno być `Server Name: PulseAudio (on PipeWire ...)`.
4. Sprawdź wyjścia: `pactl list sinks` (stan, wyciszenie, nazwa) i `wpctl status`.
5. Ustal, czy problem dotyczy jednego programu, czy wszystkich, i dopiero wtedy wskaż przyczynę.

Te polecenia wymagają sesji użytkownika. Jeśli program uruchamia polecenia bez niej, zawiodą mimo działającego dźwięku: poproś o uruchomienie w terminalu.

## Konfiguracja

- Domyślne ustawienia WirePlumbera: `/usr/share/wireplumber`. Własne fragmenty dodawaj w `/etc/wireplumber` lub `~/.config/wireplumber`, nie edytuj plików z pakietu.
- Od WirePlumbera 0.5 konfiguracja jest w `.conf`, a nie `.lua`: starsze poradniki są nieaktualne.
- `pipewire-pulse` zastępuje PulseAudio. Sprawdź, czy nie działa równolegle `pulseaudio.service`.

## Znane sprawy

- Słuchawki Bluetooth przełączają się na gorszy profil przy mikrofonie: wyłączenie `wpctl settings --save bluetooth.autoswitch-to-headset-profile false`.
- Opóźnienie dla jednego programu: `PIPEWIRE_LATENCY="128/48000" program`.
- Dźwięk przez HDMI z karty AMD: zobacz Skill `arch-amdgpu`.

Źródło: Arch Wiki, <https://wiki.archlinux.org/title/PipeWire>.
