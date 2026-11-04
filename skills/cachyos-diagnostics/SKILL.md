---
name: cachyos-diagnostics
description: Read-only diagnosis of a Linux system, its services and file systems.
---

# Diagnostyka CachyOS — tylko odczyt

Przed oceną stanu systemu ustal wersję kernela,
sesję graficzną i punkty montowania.

Używaj poleceń diagnostycznych tylko do odczytu:
`uname -r`, `findmnt`, `lsblk`, `systemctl show`,
`systemctl status`, `journalctl` oraz `pacman -Q`.

Nie zmieniaj plików, usług, konfiguracji ani uprawnień.
Nie wykonuj operacji wymagających sudo.
Nie instaluj pakietów i nie restartuj usług.

Wszelkie operacje modyfikujące opisz jako plan
i wymagaj osobnej zgody użytkownika.

Nie odczytuj ani nie ujawniaj sekretów z katalogów
.ssh, .aws, .kube oraz menedżerów haseł.

To instrukcja dla agenta, a nie techniczna
granica jego uprawnień.
