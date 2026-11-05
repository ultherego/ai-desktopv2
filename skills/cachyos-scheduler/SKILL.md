---
name: cachyos-scheduler
description: Planisty sched-ext w CachyOS: scx_loader, scxctl, profile i typowe problemy.
---

# Sched-ext w CachyOS

Sched-ext pozwala uruchomić planistę BPF zamiast domyślnego w jądrze. Zatrzymanie go przywraca domyślny. Nie działa na `linux-cachyos-bmq` i `linux-cachyos-hardened`.

## Stan (tylko odczyt)

- `systemctl status scx_loader.service`, `journalctl -u scx_loader.service -b 0`
- `scxctl get`, `scxctl list`
- pakiety: `pacman -Q scx-scheds scx-tools`

## Sterowanie (wymaga uprawnień administratora albo agenta polkit)

```
scxctl start --sched flash --mode gaming
scxctl switch --sched bpfland --mode gaming
scxctl stop
scxctl restore
```

Uruchomienie bezpośrednie do testu: `sudo scx_rusty`, zatrzymanie: Ctrl+C. Graficznie: CachyOS Kernel Manager albo SCX Manager.

Profile: Auto, Gaming, Power save, Low Latency, Server. Numery trybów scx_loader: 0 domyślny, 1 Gaming, 2 Power Saving, 3 Low Latency, 4 Server.

## Stała konfiguracja

Kolejność szukania pliku: `/etc/scx_loader/config.toml`, `/etc/scx_loader.toml`, `/usr/share/scx_loader/config.toml`, `/usr/share/scx_loader.toml`. Kernel Manager zapisuje `/etc/scx_loader.toml`. Najpierw sprawdź, który plik istnieje.

```toml
default_sched = "scx_bpfland"
default_mode = "Gaming"
```

Włączenie: `sudo systemctl enable --now scx_loader.service`.

## Typowe problemy

- **Nie uruchamiaj razem `scx.service` i `scx_loader`**: `scx_loader` startuje, ale nic nie robi. Własne flagi z `/etc/default/scx` nie przenoszą się same.
- `ananicy-cpp` może powodować zawieszenia planisty. Wiki zaleca wtedy jego wyłączenie.
- Awaria planisty: przyczyną może być `ananicy-cpp`, przeciążenie albo błąd. Log: `journalctl -u scx_loader.service -b 0`. Dokładniejszy log przez `RUST_LOG=trace` w `systemctl edit scx_loader.service`.
- `power-profiles-daemon` CachyOS przełącza profil planisty razem z profilem zasilania, a `game-performance` przełącza na Gaming.
- `scx_tickless` wymaga parametru jądra `nohz_full` i jest eksperymentalny.

Nie obiecuj wzrostu wydajności: zależy od obciążenia. Do porównań służy `cachyos-benchmarker`.

Źródło: <https://wiki.cachyos.org/configuration/sched-ext/>.
