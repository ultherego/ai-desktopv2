---
name: arch-bluetooth
description: Diagnozuje brak kontrolera Bluetooth, problemy parowania i regresje po uśpieniu w BlueZ. Użyj do odróżnienia braku sprzętu, blokady radia, stanu usługi i problemu jednego urządzenia.
---

# Diagnostyka Bluetooth

## Zakres i dostęp

Ustal, czy wcześniej działało, czy awaria dotyczy wszystkich urządzeń i czy zaczęła się po uśpieniu lub aktualizacji. Nazwy i adresy urządzeń ogranicz do niezbędnych danych; nie żądaj kluczy parowania.

`bluetoothctl` korzysta z D-Bus. Dostęp do gniazda hosta nie jest gwarancją operacji tylko do odczytu. Wykonuj jedynie dokładne odczyty z `allow`; nie uruchamiaj interaktywnego menu, skanowania ani parowania jako rozpoznania. Odmowa dostępu lub brak magistrali oznaczają lukę w obserwacjach.

## Rozpoznanie i decyzje

1. Jeśli dozwolone, sprawdź `systemctl status bluetooth.service --no-pager`. Przy stanie `failed` odczytaj krótki fragment `journalctl -u bluetooth.service -b --no-pager -n 50` z czasu awarii. Brak dostępu do logu nie oznacza, że log jest pusty.
2. Ustal, czy kontroler jest wykryty: `lsusb` lub `lspci -k` zależnie od sprzętu. `rfkill list` może wskazać blokadę sprzętową lub programową. Brak kontrolera kieruje do sprzętu, sterownika i firmware, nie do ponownego parowania.
3. Dostępny odczyt `bluetoothctl show` rozdziela brak kontrolera, `Powered: no` i działające radio. Błąd `NotReady` wymaga ustalenia tego stanu, nie dowodzi jednej konkretnej przyczyny.
4. Radio działa, lecz problem dotyczy jednego znanego urządzenia: odczytaj tylko jego stan przez `bluetoothctl info ADRES`, używając adresu ustalonego wcześniej. Rozróżnij sparowane, zaufane i połączone; to różne właściwości.
5. Po wybudzeniu porównaj stan kontrolera i dziennik z czasem uśpienia. Gdy parowanie i połączenie działają, a zawodzi tylko audio, przejdź do dostępnego `arch-audio`.

## Zmiana i sprawdzenie

Włączenie radia, usunięcie parowania, `trust`, `pair`, `connect` i restart usługi zmieniają stan. Wymagają osobnego planu i zgody; ostrzeż o rozłączeniu używanej klawiatury lub myszy. Nie czyść bazy BlueZ i nie przenoś kluczy między systemami w ramach domyślnej diagnozy.

Przed zmianą zapisz właściwy stan urządzenia. Testuj połączenie oraz funkcję urządzenia, a przy regresji także cykl uśpienia. Jeśli kroku nie da się cofnąć bez ponownego parowania, powiedz to przed zgodą. Przechwytywanie `btmon` wymaga odrębnego zakresu i przeglądu danych.

## Wynik

Podaj, czy problem dotyczy wykrywania, blokady, usługi, połączenia czy funkcji urządzenia. Dołącz potrzebny fragment dowodu i jeden dalszy krok. Zakończ bez naprawy, gdy brak wiarygodnego stanu hosta.

## Źródła

- [BlueZ: `bluetoothctl`, odczyty i polecenia sterujące](https://github.com/bluez/bluez/blob/master/doc/bluetoothctl.rst).
