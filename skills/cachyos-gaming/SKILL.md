---
name: cachyos-gaming
description: Diagnozuje uruchamianie i wydajność gier na CachyOS. Użyj przy problemach z grami, Steam, Protonem, Lutris, Heroic, game-performance lub gamemode.
---

# Gry na CachyOS

Najpierw ustal: kartę graficzną i sterownik, grę, wersję Protona, sposób uruchamiania. Sterowniki muszą działać, zanim zaczniesz stroić.

## Procedura

1. Ustal to, co wyżej: kartę i sterownik, grę, wersję Protona, sposób uruchamiania.
2. Sprawdź system plików biblioteki gier oraz zgodność gry i jej zabezpieczeń.
3. Włącz log Protona i przeczytaj go, zanim coś zmienisz.
4. Zmieniaj po jednej rzeczy: najpierw wersję Protona dla tej gry, potem opcje uruchamiania.

## Pakiety

- `cachyos-gaming-meta` i `cachyos-gaming-applications` (Gamescope, GOverlay, MangoHud, Steam, Heroic, Lutris, Faugus). Instalacja także z CachyOS Hello.

## Proton

- **Valve zaleca domyślny Proton jako globalny.** Inną wersję ustawiaj dla gry: Properties, Compatibility.
- `proton-cachyos` jest częścią `cachyos-gaming-meta`. Odmiany `proton-cachyos-slr` i `proton-cachyos-native`; wiki poleca `slr`, także dla gier z EAC i BattlEye.
- NTSync jest włączony; wyłączenie: `PROTON_NO_NTSYNC=1`.
- Zgodność: ProtonDB, Are We Anti-Cheat Yet.
- Nie uruchamiaj Protona z dysku NTFS: Valve tego nie wspiera. Sprawdź system plików biblioteki gier.

## Opcje uruchamiania Steam

`[zmienne] [nakładki] %command% [argumenty]`, np. `game-performance %command%`. Tylko jedno `%command%`: wszystko po pierwszym idzie do gry.

## game-performance

Skrypt CachyOS: na czas gry przełącza profil zasilania na wydajność oraz aktywnego planistę sched-ext na profil gier. Po wyjściu z gry przywraca stan. Na starych procesorach może nie dawać korzyści.

**Nie łącz `gamemode` z `ananicy-cpp`.** Wiki zaleca wtedy zatrzymanie `ananicy-cpp`.

## Logi

- `PROTON_LOG=1 %command%` zapisuje `steam-IDAPLIKACJI.log` w katalogu domowym (inny katalog: `PROTON_LOG_DIR`).
- Lutris: Output debugging info, zmienna `PROTON_LOG=1`.
- Dziennik może zawierać dane konta: przed udostępnieniem należy go przejrzeć.

## Znane sprawy

- Pamięć shaderów: `~/.config/environment.d/gaming.conf`, AMD: `MESA_SHADER_CACHE_MAX_SIZE=12G`, NVIDIA: `__GL_SHADER_DISK_CACHE_SIZE=12000000000`, potem restart.
- Kolorowe paski w H264: `steam steam://unlockh264/` raz, potem restart Steam.
- Zacinanie przy nagrywaniu: `LD_PRELOAD="" %command%` (wyłącza nakładkę Steam).
- umu-launcher dla Lutris i Heroic: `cachyos/umu-launcher`. Własne DXVK i VKD3D nie są wtedy stosowane.
- Gamescope jako sesja: `gamescope-session-cachyos`.
- Jądro czasu rzeczywistego nie poprawia gier.
- `wine-cachyos` jest nastawiony na gry; dla zwykłych programów wiki radzi `wine` lub `wine-staging`.

Źródło: <https://wiki.cachyos.org/configuration/gaming/>.
