---
name: arch-network
description: Diagnozuje połączenie i DNS zarządzane przez NetworkManager w Arch Linux. Użyj przy braku sieci, błędach nazw lub regresji VPN, rozdzielając stan łącza, adresację, trasę i rozwiązywanie nazw.
---

# Diagnostyka sieci

## Zakres i źródło obserwacji

Ustal, czy problem dotyczy jednego programu czy całego hosta, jednego łącza czy wszystkich, oraz czy użytkownik pracuje zdalnie. Potwierdź użycie NetworkManagera; jeśli siecią zarządza inne narzędzie, nie zmieniaj konfiguracji NetworkManagera.

Piaskownica poleceń AI Desktop nie ma sieci hosta. Wynik `ip` lub `ss` może opisywać wyłącznie piaskownicę, a brak połączenia w niej jest oczekiwany. `nmcli` korzysta z D-Bus, którego dostęp także podlega polityce. Nie używaj `change` do uzyskania odczytu hosta. Gdy brak właściwego źródła, poproś o jeden potrzebny wynik z terminala użytkownika.

## Rozpoznanie i decyzje

Wykonuj tylko odczyty pasujące dokładnie do `allow`, bez potoków i powłoki.

1. `nmcli general status` oraz `nmcli device status` wskazują stan zarządzania i urządzeń. `unmanaged` kieruje do ustalenia zarządcy; `unavailable` wymaga sprawdzenia przyczyny, np. blokady radia, sterownika lub kabla.
2. Dla urządzenia ustalonego w wyniku odczytaj `nmcli device show INTERFEJS`. Sprawdź łącze, adresy, bramy i DNS. Brak adresu po zestawieniu łącza kieruje do aktywacji profilu lub DHCP, a nie od razu do serwera DNS.
3. `nmcli connection show --active` wiąże urządzenie z aktywnym profilem. Nie proś o wszystkie pliki profili ani opcję pokazującą sekrety. Nazwy sieci, adresy i domeny ogranicz do zakresu zadania.
4. Przy poprawnej adresacji potrzebny jest wiarygodny odczyt trasy hosta i wynik połączenia z konkretnym celem. Sukces do jednego IP nie potwierdza całej sieci; brak odpowiedzi ICMP sam nie dowodzi niedostępności usługi.
5. Gdy połączenie z tym samym celem po IP działa, a po nazwie nie, sprawdź resolver oraz domeny i trasy DNS aktywnego VPN. Dozwolony dziennik `NetworkManager` z czasu zdarzenia służy potwierdzeniu, nie zastępuje stanu łącza.

## Zmiana i sprawdzenie

Nie zmieniaj DNS, tras, profilu ani radia na podstawie samego objawu. Nie czytaj `/etc/NetworkManager/system-connections/` w poszukiwaniu haseł i nie żądaj ich wklejenia.

Przed zmianą pokaż właściwy profil, bieżące wartości, propozycję i dokładne przywrócenie. Restart, przełączenie profilu lub VPN mogą odciąć dostęp zdalny; bez sposobu odzyskania dostępu zakończ na planie. Testuj osobno łącze, adresację, trasę, DNS i pierwotną aplikację. Sprawdź też wymagane działanie VPN, nie tylko publiczną nazwę.

## Wynik

Wskaż pierwszą warstwę z potwierdzonym problemem, dowód i ograniczenia obserwacji. Jeśli dowody są niepełne, podaj hipotezę i jeden odczyt rozstrzygający zamiast resetu sieci.

## Źródła

- [NetworkManager: odczyt urządzeń, profili i statusów](https://networkmanager.dev/docs/api/latest/nmcli.html).
