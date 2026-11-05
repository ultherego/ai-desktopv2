---
name: cachyos-hardware
description: Diagnozuje sterowniki i ustawienia sprzętu w CachyOS. Użyj przy pytaniach o chwd, sterowniki kart graficznych, zasilanie procesora, planistę dysków albo ZRam.
---

# Sprzęt w CachyOS

Najpierw odczytaj sprzęt: `lspci -k`, `lscpu`, `lsmod`, `sensors`. Nie zakładaj producenta procesora ani karty.

## Procedura

1. Odczytaj sprzęt i załadowane sterowniki. Wybieraj dalej tylko sekcje pasujące do tego sprzętu.
2. Ustal objaw. Bez objawu lub prośby nie proponuj strojenia.
3. Zmianę przetestuj przez `/sys`, jeśli się da, bo znika po restarcie. Stałą zaproponuj dopiero potem, z planem wycofania.

## chwd

Narzędzie CachyOS do wykrywania sprzętu i instalacji sterowników:
- `chwd --list-all`: dostępne profile; `chwd --list-installed`: zainstalowane;
- `sudo chwd -a`: konfiguracja automatyczna;
- `sudo chwd -i PROFIL`, `sudo chwd -r PROFIL`: instalacja i usunięcie profilu.

Profile obejmują m.in. `amd`, `intel`, `nvidia-open-dkms`, `virtualmachine`, `handheld`. Zmiana sterownika to zmiana systemu.

## Procesory AMD

- Tryb `amd_pstate`: parametr jądra `active` (domyślny EPP), `guided`, `passive`. Stan: `/sys/devices/system/cpu/amd_pstate/status`.
- Ustawienie EPP: `/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference`.
- Układy z 3D V-Cache i dwiema grupami rdzeni (klasa 7950X3D): opcja CPPC w BIOS-ie na „Driver", tryb w `/sys/bus/platform/drivers/amd_x3d_vcache/AMDI0101:00/amd_x3d_mode`. Wiki opisuje to dla tej klasy układów.
- `power-profiles-daemon` CachyOS wyłącza doładowanie rdzeni w profilu oszczędzania.
- `thermald` jest tylko dla Intela.

## Karty NVIDIA

- Moduły w jądrze CachyOS: zobacz Skill `cachyos-kernel`.
- GSP firmware: `options nvidia NVreg_EnableGpuFirmware=0` w `/etc/modprobe.d/nvidia-gsp.conf`, potem `sudo mkinitcpio -P`. Nie działa z otwartymi modułami.
- Spadek wydajności DX12 na NVIDIA według wiki nie wynika z CachyOS i nie ma znanego obejścia.

## Dyski

Planista ADIOS jest w `linux-cachyos`. Nie jest domyślny, bo według wiki nie jest uznany za dość stabilny. Wybór stały przez `/etc/udev/rules.d/60-ioschedulers.rules`. Stan: `/sys/block/DYSK/queue/scheduler`.

## ZRam i Zswap

CachyOS domyślnie używa ZRam. Przejście na Zswap to kilka kroków (parametry jądra, plik wymiany, reguła udev, `mkinitcpio`) z <https://wiki.cachyos.org/configuration/general_system_tweaks/>: wykonuj tylko na prośbę i z planem wycofania.

## Zasady

Opisane tu strojenie nie jest konieczne. Nie proponuj go bez objawu lub prośby. Zmiany przez `/sys` znikają po restarcie: to dobry sposób na test przed zmianą stałą.

Źródła: <https://wiki.cachyos.org/features/chwd/chwd/>, <https://wiki.cachyos.org/configuration/general_system_tweaks/>.
