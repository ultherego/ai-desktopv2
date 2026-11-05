---
name: arch-docker
description: Diagnoses an unavailable Docker daemon and container, network and data usage errors. Use to establish the right daemon and the scope of the failure before changing configuration or removing resources.
---

# Docker diagnostics

## Scope and access boundary

Establish one symptom and scope: daemon, a specific container, network or space. Also establish whether this concerns a system, rootless or remote daemon. The cleaned AI Desktop environment can point to a different endpoint than the user's terminal; do not assume both clients see the same Docker.

The Docker socket allows changing the host despite read-only file mounts. Do not propose a general `docker` template in `allow`. Use only specific reads already allowed by the owner. Do not ask for the credentials file `~/.docker/config.json`, the full container environment or application secrets.

## Investigation and decisions

1. For a confirmed system daemon, check `systemctl status docker.service --no-pager` if available; rootless needs the right user session. A denial or a missing session does not prove the daemon is stopped.
2. An allowed `docker info` confirms a response from the selected endpoint and the actual storage driver. Do not assume the driver from the distribution. Split `Cannot connect` into a wrong endpoint, socket permissions and a daemon that is not running.
3. The daemon runs: pick one container from an allowed `docker ps -a`. The `Exited` state needs an exit code and a log; in itself it does not prove a failure, for example with a one-off job. Limit the log read, e.g. `docker logs --tail 100 NAME`, if the policy admits the exact arguments.
4. The daemon does not start: compare its journal with the allowed configuration. An option set both in `daemon.json` and in the service arguments can cause a conflict; name the specific option and error before any change.
5. For networking, separate host connectivity, container connectivity and DNS. The network view of the AI Desktop sandbox is not the view of the container or the host. For data usage, use an available `docker system df`; without establishing the owner and purpose of the resources, end at the report.

## Change and verification

Do not fix permissions by opening the socket to everyone. Access to a daemon running as root, including through the `docker` group, gives root-level privileges.

A daemon restart can interrupt containers. A configuration change needs the previous bytes preserved, a syntax check and a review of the effects on services. `prune`, removing volumes, changing the driver or the data directory need separate agreement; they have no automatic lossless undo.

Test the original scenario and the state of the specific application, not only the `active` state of the daemon. Do not pull an image or create a test container without a separate decision.

## Result

Give the endpoint and type of daemon, the layer of the problem, the evidence and the smallest next step. Separate the plan from a change already made. Without permissions, do not promise a diagnosis of containers.

## Sources

- [Daemon: endpoint, configuration and network](https://docs.docker.com/engine/daemon/troubleshoot/).
- [Permissions of the `docker` group](https://docs.docker.com/engine/install/linux-postinstall/).
