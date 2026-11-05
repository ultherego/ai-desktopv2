---
name: arch-docker
description: Diagnozuje Dockera w Arch Linux. Użyj, gdy demon nie startuje, kontenery nie mają sieci, kończy się miejsce albo brakuje uprawnień do polecenia docker.
---

# Docker w Arch Linux

## Procedura

1. Sprawdź usługę: `systemctl status docker`, log: `journalctl -u docker -b`.
2. Sprawdź demona: `docker info`, sterownik: `docker info -f '{{ .DriverStatus }}'`.
3. Sprawdź kontenery: `docker ps`, `docker logs NAZWA`. Logi mogą zawierać dane aplikacji: cytuj tylko to, co potrzebne.
4. Ustal, czy problem dotyczy demona, jednego kontenera, czy sieci.

`docker.service` startuje przy rozruchu, a `docker.socket` przy pierwszym użyciu. Mostki `docker0` i `br-*` są normalne.

## Bezpieczeństwo

- **Grupa `docker` daje uprawnienia równe rootowi.** Uprzedź, zanim to zaproponujesz.
- Nie wystawiaj API na TCP bez zabezpieczeń: daje pełną kontrolę nad komputerem.
- Polecenia kasujące (`docker rm`, `docker system prune`, `docker volume rm`) usuwają dane: opisz je jako plan.

## Znane sprawy

- Brak sieci w kontenerach przy `systemd-networkd`: nadpisuje przekazywanie IP. `IPForward` trzeba ustawić w profilu sieci. Może też blokować `nftables` (`nft list ruleset`).
- Po restarcie `systemd-networkd` lub `iptables` może być potrzebny restart Dockera.
- `fork failed: Resource temporarily unavailable`: limit procesów; `TasksMax=infinity` w nadpisaniu `docker.service`.
- Sterownik: domyślny `overlay2` wystarcza w większości przypadków. Na Btrfs możliwy jest sterownik `btrfs`.
- Inne miejsce danych: `data-root` w `/etc/docker/daemon.json`. Opcja podana i tam, i we flagach usługi uniemożliwia start.

Źródło: Arch Wiki, <https://wiki.archlinux.org/title/Docker>.
