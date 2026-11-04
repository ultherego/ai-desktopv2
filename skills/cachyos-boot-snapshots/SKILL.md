---
name: cachyos-boot-snapshots
description: Rozruch i migawki CachyOS: Limine, GRUB, systemd-boot, rEFInd, Snapper, Btrfs Assistant, odzyskiwanie z Live ISO.
---

# Rozruch i migawki CachyOS

Najpierw ustal program rozruchowy i system plików. Nie zakładaj ich: CachyOS obsługuje kilka. Wskazówki: `pacman -Q limine grub systemd refind`, `findmnt /`, `findmnt /boot`.

## Parametry jądra

| Program | Plik | Zastosowanie zmian |
|---|---|---|
| Limine | `/etc/default/limine` (`KERNEL_CMDLINE[default]=`), menu: `/boot/limine.conf` | `sudo limine-mkinitcpio` |
| GRUB | `/etc/default/grub` (`GRUB_CMDLINE_LINUX_DEFAULT`) | `sudo grub-mkconfig -o /boot/grub/grub.cfg` |
| systemd-boot | `/etc/sdboot-manage.conf` (`LINUX_OPTIONS=`) | `sudo sdboot-manage gen` |
| rEFInd | `/boot/refind_linux.conf` | działa od razu |

Wpisy Limine utrzymuje `limine-mkinitcpio-hook`: nie edytuj ich ręcznie. Odczyt `/boot` może wymagać uprawnień administratora.

## Migawki Btrfs

- `snap-pac` robi migawki przed i po operacjach pacmana. Snapper domyślnie obejmuje tylko `/`; podwolumeny, np. `/home`, wymagają osobnych konfiguracji.
- **Migawki nie zastępują kopii zapasowych.** Pomagają po złej aktualizacji, błędzie konfiguracji i przypadkowym usunięciu. Nie pomagają przy awarii dysku ani programu rozruchowego.
- Wiki zaleca trzymać około 10 migawek. Starsze mogą sprawiać kłopoty przy przywracaniu.
- **Migawki z aktualizacją jądra może nie dać się przywrócić.** Zawsze o tym uprzedź.
- Stan: `sudo snapper list`, `sudo snapper get-config`. Usuwanie: `sudo snapper delete NUMER`, potem odświeżenie wpisów rozruchu.
- Graficznie: Btrfs Assistant (`btrfs-assistant-launcher`).

Limine: menu `/etc/limine-snapper-sync.conf` (`MAX_SNAPSHOT_ENTRIES`, `SNAPSHOT_FORMAT_CHOICE`).

## Przywracanie

- Limine: menu rozruchu, Snapshots, wybór migawki i jądra. System startuje tylko do odczytu; „Restore now" i potwierdzenie `yes`.
- GRUB: start z migawki, Btrfs Assistant, Snapper, Browse/Restore, Restore, restart.

## System się nie uruchamia

1. Start z CachyOS Live ISO.
2. `sudo cachy-chroot` (przy Btrfs z ustawieniami CachyOS potwierdź preset; montuje `/boot`, dla GRUB `/boot/efi`).
3. Ponowna instalacja programu rozruchowego:
   - Limine UEFI: `sudo limine-install`; MBR: `sudo limine bios-install /dev/sdX`;
   - GRUB UEFI: `sudo grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=cachyos`;
   - systemd-boot: `sudo bootctl install`; rEFInd: `sudo refind-install`.
4. `sudo pacman -Syu linux-cachyos linux-cachyos-headers`, `exit`, restart.

Czarny ekran na starszych kartach NVIDIA: wiki podaje parametry `plymouth.enable=0 disablehooks=plymouth`.

Polecenia z `/dev/sdX` są niebezpieczne: nazwę dysku ma potwierdzić użytkownik.

Źródła: <https://wiki.cachyos.org/configuration/btrfs_snapshots/>, <https://wiki.cachyos.org/configuration/boot_manager_configuration/>, <https://wiki.cachyos.org/cachyos_basic/faq/>.
