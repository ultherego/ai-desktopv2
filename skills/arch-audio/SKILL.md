---
name: arch-audio
description: Diagnozuje brak dźwięku, niewidoczne urządzenia, przerwy i zmianę profilu Bluetooth w stosie PipeWire/WirePlumber. Użyj, gdy trzeba rozdzielić problem sesji, urządzenia, routingu i pojedynczej aplikacji.
---

# Diagnostyka dźwięku

## Zakres i dostęp

Ustal wyjście lub mikrofon, program dotknięty problemem, moment wystąpienia i ostatnią zmianę. Najpierw potwierdź używany serwer dźwięku; nie przenoś konfiguracji WirePlumbera na inny stos.

Odczyty sesji użytkownika wymagają jej gniazd. Piaskownica poleceń AI Desktop ich nie udostępnia: błąd połączenia z magistralą lub PipeWire nie dowodzi awarii dźwięku. Nie przenoś odczytu do `change`, nie montuj gniazd i nie zmieniaj polityki samodzielnie. Jeśli nie ma dostępnego źródła stanu sesji, poproś o jeden potrzebny wynik z terminala użytkownika.

## Rozpoznanie i decyzje

1. Gdy odpowiednie odczyty są dostępne, ustal stan `pipewire.service`, `pipewire-pulse.service` i `wireplumber.service` przez `systemctl --user status`. Aktywna usługa nie dowodzi jeszcze poprawnego routingu. Przy nieudanej jednostce czytaj tylko jej dziennik z czasu błędu.
2. `wpctl status` pokazuje urządzenia, wyjścia, wejścia i strumienie. Brak całego urządzenia prowadzi do wykrywania sprzętu i jego profilu; brak tylko strumienia prowadzi do aplikacji.
3. Dla identyfikatora z aktualnego wyniku użyj dozwolonego `wpctl inspect ID` i `wpctl get-volume ID`. Numery są chwilowe: nie zapisuj ich jako stałej identyfikacji urządzenia.
4. Wyjście istnieje, ale jest wyciszone lub wybrano inne domyślne: opisz konkretną różnicę. Nie zmieniaj głośności ani domyślnego urządzenia w kroku odczytu.
5. Tylko jedna aplikacja zawodzi przy działającym innym odtwarzaniu: zbadaj jej strumień, wybór urządzenia i ograniczenia aplikacji, zanim zaproponujesz restart całego stosu.
6. Pogorszenie jakości Bluetooth po użyciu mikrofonu: sprawdź aktywny profil i wymagania rozmowy. Nie wyłączaj automatycznego przełączania, jeśli mikrofon jest potrzebny. Problem parowania należy do `arch-bluetooth`, jeśli ten Skill jest dostępny.

## Zmiana i sprawdzenie

Najpierw pokaż bieżący i proponowany stan, koszt dla mikrofonu/innych aplikacji oraz cofnięcie. Zmiany profilu, routingu, wyciszenia i ustawień zapisanych przez `wpctl` wymagają osobnej zgody. Nie kasuj całej konfiguracji ani stanu jako pierwszej próby.

Dla zmiany pliku ustal wersję WirePlumbera i użyj dokumentacji tej wersji; nie mieszaj starszego formatu Lua z konfiguracją 0.5+. Testuj to samo wyjście, tę samą aplikację, a przy zestawie słuchawkowym również mikrofon. Restart usługi przerywa aktywny dźwięk i nie jest testem naprawy sam w sobie.

## Wynik

Wskaż warstwę awarii, dowód oraz zakres: urządzenie, profil, strumień lub aplikacja. Przy braku sesji zgłoś ograniczenie obserwacji i jeden następny odczyt; nie diagnozuj na podstawie błędu piaskownicy.

## Źródła

- [WirePlumber: `status`, `inspect`, `get-volume`, ustawienia](https://pipewire.pages.freedesktop.org/wireplumber/man/wpctl.html).
- [Konfiguracja i sprawdzanie sesji](https://pipewire.pages.freedesktop.org/wireplumber/daemon/getting_started.html).
