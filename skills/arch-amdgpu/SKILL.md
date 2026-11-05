---
name: arch-amdgpu
description: Diagnozuje karty AMD ze sterownikiem amdgpu w Arch Linux. Użyj przy zawieszeniach, artefaktach, migotaniu, problemach z dźwiękiem HDMI, monitorowaniu albo zmianie taktowania karty.
---

# AMDGPU w Arch Linux

## Procedura

1. Potwierdź kartę i sterownik: `lspci -k -d ::03xx`.
2. Sprawdź, czy `linux-firmware-amdgpu` jest aktualny: sterownik wymaga najnowszego firmware.
3. Przeczytaj `journalctl -k -b` pod kątem `amdgpu` i `drm`. Po zawieszeniu sprawdź poprzedni rozruch: `journalctl -k -b -1`.
4. Ustal, czy problem pojawił się po zmianie jądra lub po podkręcaniu.
5. Odczytaj stan, jak niżej, i dopiero wtedy proponuj zmianę.

## Odczyt

Plik karty to `/sys/class/drm/card0/device/` (numer karty może być inny):
- `gpu_busy_percent`, `pp_dpm_sclk`, `pp_dpm_mclk`, `mem_info_vram_used`, `mem_info_vram_total`;
- `hwmon/hwmon*/temp1_input`, `pp_od_clk_voltage`, `pp_power_profile_mode`;
- `/sys/kernel/debug/dri/0/amdgpu_pm_info` (wymaga roota).

## Znane objawy

- Artefakty przy odświeżaniu powyżej 60 Hz lub zawieszenia w grach: dynamiczne zarządzanie energią. Obejście: poziom `high` lub `low` w `power_dpm_force_performance_level`. Pomaga też inna wersja jądra.
- Biały lub szary ekran, migotanie po zmianie rozdzielczości: parametr jądra `amdgpu.sg_display=0`.
- Dźwięk zbyt szybki lub jego brak przy 4K przez HDMI: spróbuj HDR lub trybu „Ultra HD” w monitorze.

## Podkręcanie

Odblokowanie wymaga parametru `amdgpu.ppfeaturemask`. Wiki ostrzega, że ustawienie wszystkich bitów (`0xffffffff`) może włączyć niestabilne funkcje: bezpieczniej dodać tylko `0x4000` do bieżącej maski. **Zmiana napięć i limitów grozi niestabilnością.** Zanim uznasz sprzęt za wadliwy, sprawdź, czy karta nie jest podkręcona, i przywróć wartości domyślne: `echo "r"` do `pp_od_clk_voltage`.

Źródło: Arch Wiki, <https://wiki.archlinux.org/title/AMDGPU>.
