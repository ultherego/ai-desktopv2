---
name: arch-bluetooth
description: Diagnozuje Bluetooth z BlueZ w Arch Linux. Użyj, gdy kontroler nie działa, urządzenie nie paruje się albo Bluetooth przestaje działać po uśpieniu.
---

# Bluetooth w Arch Linux

## Procedura

1. Sprawdź usługę: `systemctl status bluetooth`, log: `journalctl -u bluetooth -b`.
2. Sprawdź kontroler: czy istnieje `/sys/class/bluetooth`, czy moduły są załadowane (`lsmod`, zwykle `btusb`, czasem `btrtl`, `btintel`, `btbcm`).
3. Sprawdź stan w `bluetoothctl`: `show`, `devices`.
4. Przy błędzie `org.bluez.Error.NotReady` kontroler może być wyłączony lub zablokowany.
5. Szczegóły: `btmon`, albo `bluetoothd` uruchomiony z `-n -d` po zatrzymaniu usługi.

## Parowanie (robi użytkownik)

`bluetoothctl`, potem `agent on`, `default-agent`, `scan on`, `pair MAC`, `connect MAC`, `trust MAC`. Adresy urządzeń są danymi osobistymi: cytuj je tylko, gdy to konieczne.

## Znane sprawy

- Komputer budzi się przez Bluetooth: w trybie S2idle kontroler pozostaje aktywny. Wiki opisuje usługę wyłączającą go przed uśpieniem, ale wtedy mysz ani klawiatura Bluetooth nie obudzą komputera.
- Dźwięk Bluetooth działa tylko po zalogowaniu, bo PipeWire i PulseAudio to usługi użytkownika: zobacz Skill `arch-audio`.
- Błędy po wybudzeniu: przeczytaj dziennik jądra z tego czasu, zanim zaproponujesz zmianę.
- Dual boot wymaga przeniesienia kluczy parowania między systemami.

Źródło: Arch Wiki, <https://wiki.archlinux.org/title/Bluetooth>.
