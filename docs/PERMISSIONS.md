# `permissions.toml`: settings reference

File: `~/.config/ai-desktop/permissions.toml`, owned by you, mode `0600`. Template: [`config/permissions.example.toml`](../config/permissions.example.toml). A missing key means "off"; an unknown key or bad value stops the program and names the problem. Details and examples: [README](../README.md#commands-commands).

## Top level

- `version`: `2` (current; `deny` is the only protection for secrets) or `1` (older; adds a built-in list of blocked places).

## `[network]`

All `true`/`false`, default `false`.

- `claude`: requests may go to Claude.
- `codex`: requests may go to Codex.
- `local`: requests may go to a local model on `127.0.0.1`.
- `weather`: weather from Open-Meteo (sends the place name).
- `web`: web search through Claude (sends only your question).

## `[local]`

- `port`: port of the local model, default `8080`.

## `[system]`

- `systemd`: systemd views the model may read: `["system"]`, `["user"]` or both.

## `[files]`: what may be read and written

- `read`: places that may be read, e.g. `"~/"`, `"~/Projects/**"`, `"/etc/"`.
- `write`: places where changes may be saved (each save shows a diff and asks).
- `deny`: never read or written, wins over `read` and `write`, e.g. `"~/.ssh/"`, `"~/.claude/"`, `"~/.codex/"`.

Rule forms: `~/x` one file, `~/x/*` files directly in it, `~/x/` or `~/x/**` the whole tree.

Limits (default → highest allowed):

- `file_size`: largest local text file to inspect, `"32MiB"` → `"64MiB"`.
- `part_size`: one portion of a whole-file analysis, `"240KiB"` → `"512KiB"`.
- `read_files`: files in one context, `128` → `512`.
- `read_file_size`: bytes per file sent, `"2MiB"` → `"8MiB"`.
- `read_total_size`: bytes of all files sent, `"4MiB"` → `"16MiB"`.
- `test_copy_files`: files in the local test copy, `2000` → `20000`.
- `test_copy_file_size`: bytes per file in the test copy, `"8MiB"` → `"32MiB"`.
- `test_copy_total_size`: bytes of the whole test copy, `"64MiB"` → `"128MiB"`.
- `listing_entries`: entries in one directory listing, `128` → `512`.
- `discovery_bytes`: listing and search results sent per question, `"128KiB"` → `"256KiB"`.
- `search_results`: matches in one search, `40` → `200`.
- `search_scan`: directory entries examined per question, `20000` → `100000`.
- `discovery_rounds`: listing/search steps per question, `4` → `8`.
- `plan_corrections`: corrected read plans per question, `2` → `4`.
- `command_rounds`: commands run per question, `24` → `32` (`0` = none).
- `command_output`: output of one command sent to the model, `"8KiB"` → `"128KiB"`.
- `command_timeout`: seconds per command, `20` → `60`.

## `[commands]`: what the model may run

- `allow`: read-only commands, run at once without asking, e.g. `"systemctl status <unit> --no-pager"`. Placeholders: `<unit>` systemd unit, `<int>` number, `<word>` one word.
- `change`: commands that change the system; each needs you to type `yes`, a test from `allow` and an undo.
- `share_processes`: `true` lets `allow` commands see host processes and their command lines. Default `false`.
- `host_sockets`: list of extra sockets under `/run` that `allow` commands may reach (at most 8; never `/run/user`). Default `[]`.

`allow` commands see an empty `/run` except the system D-Bus, the DNS resolver's query socket, journals, systemd and login state, udev data, mounts, `utmp` and NetworkManager state. Anything else needs `host_sockets`:

| Socket | For |
|---|---|
| `/run/docker.sock` | `docker` (root-equivalent: list only if you need it) |
| `/run/podman/podman.sock` | `podman` as root (root-equivalent) |
| `/run/containerd/containerd.sock` | `ctr`, `nerdctl` (root-equivalent) |
| `/run/lactd.sock` | `lact` (GPU settings) |
| `/run/libvirt/libvirt-sock-ro` | `virsh` read-only queries |

Example:

```toml
[commands]
allow = ["docker ps", "lact cli info"]
host_sockets = ["/run/docker.sock", "/run/lactd.sock"]
```

The exact path depends on the package; check it with `ls -l /run`.
