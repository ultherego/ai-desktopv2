---
name: cachyos-boot-snapshots
description: Rozpoznaje etap awarii rozruchu i warunki przywrócenia migawki CachyOS. Użyj przy problemach z bootloaderem, initramfs, parametrami jądra lub odzyskiwaniem przez Live ISO.
---

# Rozruch i migawki

## Ustal etap awarii

Zbierz ostatni widoczny komunikat, ostatnią zmianę, możliwość uruchomienia starszego wpisu oraz faktyczny bootloader, tryb UEFI lub BIOS i układ montowania. Zainstalowany pakiet bootloadera nie dowodzi, że jest używany. Odczyty z Live ISO opisują początkowo ten nośnik, nie zainstalowany system.

| Etap | Dalsza diagnoza |
|---|---|
| Firmware nie pokazuje właściwego wpisu | Sprawdź tryb rozruchu i właściwą ESP; nie instaluj bootloadera w ciemno. |
| Menu działa, lecz nie znajduje jądra lub initramfs | Porównaj ścieżki wpisu, zawartość właściwej partycji i pakiety. |
| Jądro startuje, lecz nie montuje root | Ustal dokładny błąd, identyfikatory urządzeń, szyfrowanie i podwolumen. Przy błędach I/O zatrzymaj naprawę rozruchu i chroń dane. |
| System osiąga usługi, ale sesja użytkownika nie działa | Przekaż diagnozę usług lub sesji; nie reinstaluj z tego powodu bootloadera. |
| Brakuje dowodów o etapie lub dyskach | Zakończ wskazaniem potrzebnych odczytów. Nie zgaduj nazw urządzeń. |

## Odczyt i ograniczenia

Używaj tylko danych udostępnionych przez kontroler: konfiguracji wpisów, właściwego logu i potwierdzonej mapy root, `/boot` oraz ESP. `findmnt` w piaskownicy może pokazać jej widok. Narzędzia wymagające administratora, interaktywnego chroot lub restartu opisuj jako ręczny plan; brak dostępu nie uzasadnia eskalacji ani zmiany polityki.

## Warunki przywrócenia migawki

1. Potwierdź konfigurację Snappera, zakres wybranej migawki i datę względem awarii. Migawka nie obejmuje automatycznie zagnieżdżonych podwolumenów, ESP ani osobnego `/boot` i nie zastępuje backupu.
2. Sprawdź zgodność przywracanego root z jądrem, initramfs i modułami. Przy aktualizacji jądra wynik zależy od układu i mechanizmu przywracania; nie obiecuj powodzenia ani bezwarunkowej niemożliwości rollbacku.
3. Wskaż, jakie nowsze dane i konfiguracje zostaną zastąpione. Zaplanuj zachowanie obecnego stanu i drogę odzyskania przed zgodą.

## Zmiana i weryfikacja

Przy parametrze jądra wskaż właściwy plik dla wykrytego bootloadera, dokładny diff i sposób regeneracji wynikający z używanego narzędzia. Nie edytuj ręcznie generowanych wpisów. Cofnięcie ma przywrócić poprzedni parametr i właściwe wygenerowane pliki.

Reinstalację bootloadera lub jądra rozważaj dopiero po dowodzie uszkodzenia albo brakujących plików. Wymaga potwierdzenia urządzeń, montowań, dostępu do odzyskania i osobnej zgody. Po zmianie sprawdź pliki i spójność konfiguracji; dopiero kontrolowany rozruch potwierdza działanie. Przywrócenie root nie cofa automatycznie zmian w ESP.

## Wynik

Podaj etap awarii, dowód, zakres proponowanej zmiany i warunek jej wykonania. Oddziel sprawdzoną konfigurację od nieprzetestowanego jeszcze rozruchu.

## Źródła

- [Konfiguracja programów rozruchowych](https://wiki.cachyos.org/configuration/boot_manager_configuration/).
- [Migawki i przywracanie](https://wiki.cachyos.org/configuration/btrfs_snapshots/#restoring-from-a-snapshot).
