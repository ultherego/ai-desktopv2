---
name: arch-amdgpu
description: Diagnozuje zawieszenia, artefakty i problemy wyświetlania kart AMD w Arch Linux. Użyj po ustaleniu objawu; odróżnia regresję sterownika, ścieżkę obrazu i problem sprzętowy przed zmianą ustawień.
---

# Diagnostyka AMDGPU

## Dane wejściowe i dostęp

Ustal model GPU, APU czy karta dedykowana, wersję działającego jądra, dokładny objaw i moment jego wystąpienia. Zapytaj o ostatnią aktualizację, zmianę monitora i strojenie tylko wtedy, gdy tych danych nie ma w rozmowie.

Polecenia poniżej są odczytami wymagającymi odpowiedniej reguły `allow`; logi i pliki wymagają własnego dostępu. Brak uprawnień lub pliku w piaskownicy oznacza brak obserwacji. Nie uruchamiaj powłoki ani zapisu przez `/sys` w celu obejścia odmowy.

## Rozpoznanie

1. `lspci -k` identyfikuje urządzenie PCI i używany sterownik; `uname -r` identyfikuje jądro. Przy kilku GPU powiąż objaw z konkretnym urządzeniem.
2. Jeśli polityka pozwala, odczytaj `journalctl -k -b --no-pager -n 100` z czasu awarii. Po restarcie potrzebny może być poprzedni rozruch. Brak zachowanego dziennika nie wyklucza zawieszenia.
3. W dozwolonym kontekście znajdź urządzenie DRM powiązane z tym PCI i jego katalog `hwmon`. Nie zakładaj numerów `card0`, `dri/0` ani `hwmon0`. Czytaj tylko istniejące, obsługiwane pola; nie wnioskuj z braku opcjonalnego interfejsu o uszkodzeniu.

## Interpretacja

- Brak urządzenia w PCI: sprawdź wykrywanie sprzętu; zmiana parametru AMDGPU nie wyjaśni tego wyniku.
- Inny sterownik lub brak sterownika: ustal powiązanie modułu z urządzeniem i błędy jego ładowania, zanim oceniasz wydajność.
- Reset GPU, timeout lub błąd firmware w czasie objawu: skoreluj z obciążeniem i zmianą jądra. Sama obecność słowa `amdgpu` nie potwierdza przyczyny.
- Migotanie tylko jednego wyjścia lub trybu: ustal monitor, złącze, odświeżanie i VRR; porównaj z działającym trybem. Nie uznawaj od razu zarządzania energią za przyczynę.
- Wartości temperatury, zajętości VRAM i obciążenia oceniaj razem z czasem pomiaru; pojedyncza wysoka wartość nie dowodzi awarii.

## Zmiana i sprawdzenie

Zmieniaj jedną rzecz po osobnej zgodzie, z zapisanym stanem wyjściowym i sposobem przywrócenia. Parametr `amdgpu.sg_display=0` dotyczy tylko APU; rozważaj go wyłącznie przy pasującym problemie opisanym w dokumentacji jądra. Nie stosuj go jako ogólnej naprawy kart dedykowanych.

Nie odblokowuj funkcji podkręcania ani nie resetuj cudzych ustawień napięcia lub zegarów w ramach diagnostyki. Testem jest powtórzenie tego samego scenariusza i porównanie objawu oraz dziennika. Brak odtworzenia awarii w krótkiej próbie nie dowodzi trwałej naprawy.

## Wynik

Podaj urządzenie i sterownik, potwierdzone obserwacje z czasem, najbardziej prawdopodobną przyczynę oraz alternatywę. Gdy brakuje danych, zakończ wskazaniem jednego potrzebnego odczytu, bez zgadywania naprawy.

## Źródła

- [Parametry AMDGPU: `sg_display`, `ppfeaturemask`](https://docs.kernel.org/gpu/amdgpu/module-parameters.html).
