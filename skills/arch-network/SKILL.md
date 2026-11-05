---
name: arch-network
description: Diagnoses connections and DNS managed by NetworkManager on Arch Linux. Use for no network, name errors or a VPN regression; separates link state, addressing, routing and name resolution.
---

# Network diagnostics

## Scope and source of observation

Establish whether the problem concerns one program or the whole host, one link or all of them, and whether the user is working remotely. Confirm that NetworkManager is in use; if another tool manages the network, do not change NetworkManager configuration.

The AI Desktop command sandbox does not have the host network. An `ip` or `ss` result may describe only the sandbox, and no connection inside it is expected. `nmcli` uses D-Bus, whose access is also subject to the policy. Do not use `change` to obtain a host read. When the right source is missing, ask for the one result you need from the user's terminal.

## Investigation and decisions

Run only reads that exactly match `allow`, without pipes or a shell.

1. `nmcli general status` and `nmcli device status` show the management state and the devices. `unmanaged` points to finding out who manages it; `unavailable` needs its cause checked, e.g. a radio block, the driver or the cable.
2. For a device established from the result, read `nmcli device show INTERFACE`. Check the link, addresses, gateways and DNS. No address after the link comes up points to profile activation or DHCP, not straight to the DNS server.
3. `nmcli connection show --active` ties the device to the active profile. Do not ask for all profile files or for the option that shows secrets. Limit network names, addresses and domains to the scope of the task.
4. With correct addressing, you need a reliable read of the host route and the result of a connection to a specific target. Success to one IP does not confirm the whole network; no ICMP reply does not by itself prove the service is unreachable.
5. When a connection to the same target works by IP but not by name, check the resolver and the DNS domains and routes of the active VPN. An allowed `NetworkManager` log from the time of the event serves as confirmation; it does not replace link state.

## Change and verification

Do not change DNS, routes, a profile or the radio on the basis of a symptom alone. Do not read `/etc/NetworkManager/system-connections/` looking for passwords, and do not ask for them to be pasted.

Before a change, show the relevant profile, the current values, the proposal and the exact restoration. A restart or switching a profile or VPN can cut off remote access; without a way to regain access, end at the plan. Test the link, addressing, route, DNS and the original application separately. Also check the required VPN behavior, not only a public name.

## Result

Name the first layer with a confirmed problem, the evidence and the limits of the observation. If the evidence is incomplete, give a hypothesis and one deciding read instead of a network reset.

## Sources

- [NetworkManager: reading devices, profiles and statuses](https://networkmanager.dev/docs/api/latest/nmcli.html).
