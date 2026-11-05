---
name: arch-zram
description: Wyjaśnia i diagnozuje zram, zswap oraz wykorzystanie pamięci wymiany w Arch Linux. Użyj przy presji pamięci, nieskutecznej kompresji, błędzie konfiguracji albo pytaniu o hibernację.
---

# Diagnostyka zram i pamięci wymiany

## Dane wejściowe i dostęp

Ustal, czy występuje brak pamięci, zacinanie, błąd uruchomienia urządzenia czy jedynie niezrozumiałe wartości. Nie proponuj strojenia bez objawu lub celu. Odczyty wykonuj wyłącznie w dostępnym zakresie polityki; odmowa nie oznacza braku urządzenia.

## Rozpoznanie

1. Jeśli dozwolone, odczytaj `zramctl`, `swapon --show` i `free -h`. Powiąż urządzenia, priorytety i wykorzystanie; nie zakładaj numeru `zram0`.
2. Dla wykrytego urządzenia odczytaj dostępne statystyki w `/sys/block/URZADZENIE/mm_stat`. Dokumentacja jądra określa kolejność pól; nie zgaduj jednostek ani nazw na podstawie pozycji bez sprawdzenia wersji.
3. Stan `/sys/module/zswap/parameters/enabled` rozstrzyga, czy działa dodatkowy cache zswap. Ustal także, gdzie znajduje się rzeczywista pamięć wymiany.
4. Znajdź źródło konfiguracji: generator systemd, reguła udev lub konfiguracja dystrybucji. Czytaj tylko istniejące, dozwolone pliki; nie nakładaj drugiego mechanizmu zarządzania na pierwszy.

## Interpretacja

- Duże `DISKSIZE` oznacza pojemność logiczną, nie pamięć RAM zarezerwowaną w tej wielkości.
- `orig_data_size` opisuje dane przed kompresją; `compr_data_size` dane skompresowane; `mem_used_total` rzeczywisty koszt wraz z narzutem. Oceniaj pomiar tego samego urządzenia, zamiast zakładać stały współczynnik kompresji.
- Zajęty swap przy dostępnej pamięci nie dowodzi awarii. Powiąż objaw z powtarzalnym obciążeniem, presją pamięci i ewentualnym zdarzeniem OOM.
- Zswap przechwytuje strony przed urządzeniem wymiany. Jego współistnienie z zram wymaga oceny narzutu i celu; nie wyłączaj go automatycznie na podstawie samej obecności obu mechanizmów.
- Hibernacja wymaga trwałego miejsca na obraz. Sam zram nie zachowa go po wyłączeniu zasilania; diagnostyka hibernacji wymaga osobnego sprawdzenia urządzenia i konfiguracji wznowienia.

## Zmiana i sprawdzenie

Zmiana rozmiaru, algorytmu, priorytetu lub aktywacji swapu zmienia stan systemu. Najpierw zachowaj konfigurację i ustal możliwość powrotu. `swapoff` może wymagać znacznej ilości wolnej pamięci i zakończyć się błędem; nie traktuj go jako nieszkodliwego cofnięcia ani nie proponuj masowego wyłączenia swapu przy presji pamięci.

Po osobnej zgodzie porównaj to samo obciążenie, zużycie RAM, koszt kompresji i objaw. Sama aktywność usługi lub poprawny plik konfiguracyjny nie potwierdzają poprawy.

## Wynik

Podaj mechanizm konfiguracji, aktywne urządzenia, interpretację pomiarów i związek z objawem. Gdy nie ma dowodu problemu, zakończ wyjaśnieniem wartości; gdy brakuje obserwacji, wskaż jeden potrzebny pomiar.

## Źródła

- [Jądro: zram, statystyki i dezaktywacja](https://docs.kernel.org/admin-guide/blockdev/zram.html).
- [Jądro: rola zswap](https://docs.kernel.org/admin-guide/mm/zswap.html).
- [util-linux: `swapoff`, kody błędów](https://man7.org/linux/man-pages/man8/swapon.8.html).
