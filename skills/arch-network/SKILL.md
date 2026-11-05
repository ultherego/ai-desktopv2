---
name: arch-network
description: Diagnozuje sieć z NetworkManagerem w Arch Linux. Użyj, gdy nie ma połączenia, DNS nie działa, VPN psuje nazwy albo start systemu czeka na sieć.
---

# Sieć: NetworkManager w Arch Linux

## Procedura

1. `nmcli device`: stan urządzeń. `unavailable` przy kablu to zwykle brak kabla, a nie awaria.
2. `nmcli connection show`: profile. Ustawienie automatyczne: `nmcli --fields connection.autoconnect connection show NAZWA`.
3. `journalctl -u NetworkManager -b`: log.
4. Oddziel warstwy: łącze, adres, trasa, DNS. Jeśli adres IP odpowiada, a nazwa nie, problemem jest DNS.
5. Polecenia `ip` i `ss` uruchomione w izolowanym środowisku mogą pokazać tylko `lo`: to nie jest awaria.

## Znane sprawy

- Komunikat o `dbus-org.freedesktop.resolve1.service`: `systemd-resolved` nie działa, a NetworkManager próbuje z niego korzystać.
- **Pełny DNS przez VPN** wymaga warunkowego przekazywania. Sama wtyczka VPN (np. `networkmanager-openvpn`) tego nie załatwia.
- `NetworkManager-wait-online.service` włącza się razem z NetworkManagerem i może opóźniać start. Domyślnie czeka na start NetworkManagera, a nie na łączność.
- Po zmianie konfiguracji: `nmcli general reload`.

## Prywatność i bezpieczeństwo

- Hasła sieci są zapisane jawnym tekstem w `/etc/NetworkManager/system-connections/`. **Nie czytaj tych plików** i nie proś o ich wklejenie.
- `nmcli connection delete`, `modify` i `radio wifi off` zmieniają stan i mogą odciąć dostęp zdalny: opisz je jako plan.

Źródło: Arch Wiki, <https://wiki.archlinux.org/title/NetworkManager>.
