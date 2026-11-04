---
name: desktop-documentation
description: Pulpit i kompozytor: najpierw ustal, co działa, potem opieraj się na jego dokumentacji, a nie na pamięci.
---

# Pulpit i kompozytor: korzystaj z dokumentacji

Skille CachyOS nie opisują pulpitów ani kompozytorów, bo CachyOS może działać z wieloma. Nie przenoś wskazówek z jednego na drugi i nie zakładaj żadnego z nich.

## Ustal, co działa

- `echo $XDG_CURRENT_DESKTOP`, `echo $XDG_SESSION_TYPE` (jeśli program ma do nich dostęp), a inaczej zapytaj.
- `pacman -Qs NAZWA`, `systemctl --user list-units`.
- Wersja programu ma znaczenie: pulpity często zmieniają opcje między wydaniami.

## Dokumentacja

- KDE Plasma: <https://docs.kde.org/>, <https://userbase.kde.org/>
- GNOME: <https://help.gnome.org/>
- Hyprland: <https://wiki.hypr.land/>
- Sway: <https://github.com/swaywm/sway/wiki>
- niri: <https://github.com/YaLTeR/niri/wiki>
- Xfce: <https://docs.xfce.org/>
- Inne i ogólnie: <https://wiki.archlinux.org/title/Desktop_environment>

## Jak pracować

1. Gdy potrzebujesz opcji lub polecenia pulpitu, oprzyj się na tej dokumentacji. Jeśli wyszukiwanie w Internecie jest dostępne, użyj go. Jeśli nie, powiedz, że nie masz dostępu do dokumentacji, podaj adres i zaznacz, co pochodzi z pamięci i wymaga sprawdzenia.
2. Czytaj rzeczywistą konfigurację użytkownika, jeśli program na to pozwala, zamiast zgadywać ścieżki.
3. Jeśli program ma polecenie sprawdzające konfigurację, użyj go przed zaproponowaniem restartu.
4. Dzienniki pulpitu zwykle są w `journalctl --user -b`. Usługi sesji nie są widoczne w `systemctl` bez `--user`.

Polecenia uruchamiane przez program mogą nie widzieć sesji graficznej i zgłaszać błąd, choć pulpit działa. Nie uznawaj tego za awarię: poproś o uruchomienie polecenia w terminalu użytkownika.
