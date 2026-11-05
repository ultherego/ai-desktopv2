---
name: cachyos-kernel
description: Pomaga wybrać i zmienić jądro CachyOS oraz naprawić jego moduły. Użyj przy pytaniach o warianty jądra, Kernel Manager, moduły NVIDIA lub ZFS albo przy błędach po zmianie jądra.
---

# Jądra CachyOS

Najpierw ustal stan: `uname -r` i `pacman -Qs linux-cachyos`. Jądro działające może być inne niż zainstalowane po aktualizacji bez restartu.

## Procedura

1. Ustal, czego użytkownik potrzebuje i dlaczego. Bez konkretnej potrzeby zostań przy `linux-cachyos`.
2. Odczytaj działające i zainstalowane jądra oraz moduły.
3. Sprawdź ograniczenia wybranego wariantu, np. brak sched-ext.
4. Zaproponuj zmianę jako plan i zostaw drugie, działające jądro.

## Warianty

- `linux-cachyos`: domyślne i zalecane, gdy nie ma powodu wybierać inaczej (1000 Hz, Clang ThinLTO, AutoFDO).
- `linux-cachyos-bore`: planista BORE.
- `linux-cachyos-eevdf`: dostrojony planista domyślny.
- `linux-cachyos-lts`: najnowsze LTS z BORE, mało poprawek. Dobre jako jądro zapasowe.
- `linux-cachyos-bmq`: planista BMQ. **Bez sched-ext.**
- `linux-cachyos-hardened`: utwardzone, wyraźnie wolniejsze. **Bez sched-ext.**
- `linux-cachyos-deckify`: domyślne na urządzeniach przenośnych. Inne jądra nie są tam wspierane.
- `linux-cachyos-server`: 300 Hz, bez wywłaszczania, EEVDF.
- `linux-cachyos-rt-bore`: czas rzeczywisty. Wiki wprost: nie poprawia wydajności w grach.
- `linux-cachyos-rc`: najnowsze z gałęzi głównej, do testów.

Nazwy pakietów towarzyszących mają postać `NAZWA-headers`, `NAZWA-nvidia`, `NAZWA-nvidia-open`, `NAZWA-zfs`, `NAZWA-dbg`.

## Moduły

Jądra CachyOS mają gotowe moduły NVIDIA (zamknięte i otwarte) oraz ZFS, więc pakiety `-dkms` zwykle nie są potrzebne. Błąd `ERROR: module not found: 'nvidia'` przy budowie initramfs oznacza zwykle brak modułów dla któregoś z zainstalowanych jąder. Sprawdź, które jądra są zainstalowane i czy mają moduły.

## CachyOS Kernel Manager

Program graficzny. Instalacja lub usunięcie: zaznacz jądro i naciśnij Execute. Własna kompilacja: Configure, a potem Build kernel; pliki i pamięć podręczna są w `~/.cache/cachyos-km/`. Buduje tylko warianty CachyOS, nie zwykłe jądro Archa. Zarządza też planistami sched-ext i wymaga działającego agenta polkit.

## Zasady zmian

- Zmiana jądra to zmiana systemu: opisz jako plan i zaproponuj, by zostawić drugie, działające jądro (np. `linux-cachyos-lts`).
- Instalacja jądra bez `-headers` psuje moduły budowane przez DKMS, jeśli są używane.
- Po aktualizacji jądra rollback migawki może być niemożliwy: zobacz Skill `cachyos-boot-snapshots`.
- Parametry jądra zależą od programu rozruchowego: zobacz ten sam Skill.

Źródła: <https://wiki.cachyos.org/features/kernel/>, <https://wiki.cachyos.org/features/kernel_manager/>.
