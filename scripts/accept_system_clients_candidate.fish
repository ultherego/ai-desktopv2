#!/usr/bin/fish
# Complete S8 offline acceptance. No provider install, OAuth, login or inference.
if test (count $argv) -eq 1; and test "$argv[1]" = --help
    printf 'Usage: fish scripts/accept_system_clients_candidate.fish MODE SHA REPO_ROOT PROVIDER [BRANCH]\n'
    printf 'Modes: --local-only (CI + LOCAL + metadata), --installed-check (also isolated client probes), --allow-install (install AI Desktop only, then probes).\n'
    printf 'Providers: claude, codex, both, local. Default branch: feature/system-client-runtime.\n'
    printf 'Clients must be installed independently; HOME/npm sources need explicit ai-desktop clients register before isolated probes.\n'
    printf 'This runner never registers clients or reads credentials. No real model requests are used.\n'
    exit 0
end
if test (count $argv) -lt 4; or test (count $argv) -gt 5; or not contains -- "$argv[1]" --local-only --installed-check --allow-install
    printf 'SYSTEM CLIENT ACCEPTANCE: REFUSED required MODE SHA REPO_ROOT PROVIDER [BRANCH]\n'
    exit 2
end
if not string match -rq '^[0-9a-f]{40}$' -- "$argv[2]"; or not string match -rq '^/' -- "$argv[3]"; or not contains -- "$argv[4]" claude codex both local
    printf 'SYSTEM CLIENT ACCEPTANCE: REFUSED invalid SHA, repository or provider\n'
    exit 2
end
set -l aid_branch feature/system-client-runtime
if test (count $argv) -eq 5
    set aid_branch "$argv[5]"
end
if not string match -rq '^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$' -- "$aid_branch"; or string match -rq '(^|/)\.|\.\.|/$|//' -- "$aid_branch"
    printf 'SYSTEM CLIENT ACCEPTANCE: REFUSED invalid branch\n'
    exit 2
end
if not test (id -u) -gt 0; or not test -t 0; or not test -t 1
    printf 'SYSTEM CLIENT ACCEPTANCE: REFUSED non-root terminal required\n'
    exit 2
end
set -g aidsc_results
set -g aidsc_native NOT_RUN
set -g aidsc_install NOT_RUN
function __aidsc_step
    set -l aid_label $argv[1]
    set -e argv[1]
    $argv
    set -l aid_code $status
    set -ga aidsc_results "$aid_label=$aid_code"
    if test "$aid_label" = SYSTEM_CLIENT
        set -g aidsc_native "$aid_code"
    else if test "$aid_label" = UPGRADE
        set -g aidsc_install "$aid_code"
    end
    return "$aid_code"
end
function __aidsc_logged
    set -l aid_label $argv[1]
    set -l aid_log $argv[2]
    set -e argv[1..2]
    $argv 2>&1 | tee "$aid_log"
    set -l aid_codes $pipestatus
    set -ga aidsc_results "$aid_label=$aid_codes[1]" "$aid_label"_LOG"=$aid_codes[2]"
    if test "$aid_codes[1]" -ne 0
        return "$aid_codes[1]"
    end
    return "$aid_codes[2]"
end
function __aidsc_cycle --argument-names aid_mode aid_sha aid_repo aid_provider aid_branch aid_tmp aid_script
    __aidsc_step FETCH git -C "$aid_repo" fetch origin "refs/heads/$aid_branch"
    or return $status
    set -l aid_fetched (git -C "$aid_repo" rev-parse --verify FETCH_HEAD)
    or return $status
    __aidsc_step SHA_MATCH test "$aid_fetched" = "$aid_sha"
    or return $status
    set -l aid_tree "$aid_tmp/source"
    __aidsc_step WORKTREE git -C "$aid_repo" worktree add --detach "$aid_tree" "$aid_sha"
    or return $status
    __aidsc_step SCRIPT_MATCH cmp -- "$aid_script" "$aid_tree/scripts/accept_system_clients_candidate.fish"
    or return $status
    cd "$aid_tree"; or return $status
    __aidsc_step COMMIT git log -1 --oneline
    or return $status
    __aidsc_logged CI "$aid_tmp/ci.log" python3 -B scripts/verify.py --mode ci --fail-fast --json "$aid_tmp/ci.json"
    or return $status
    __aidsc_logged LOCAL "$aid_tmp/local.log" python3 -B scripts/verify.py --mode local --fail-fast --json "$aid_tmp/local.json"
    or return $status
    __aidsc_step SOURCE_DIFF git diff --exit-code "$aid_sha" --
    or return $status
    set -l aid_dirty (git status --porcelain --untracked-files=normal)
    or return $status
    __aidsc_step SOURCE_CLEAN test -z "$aid_dirty"
    or return $status
    __aidsc_step METADATA python3 -B scripts/install_agents.py "$aid_provider"
    or return $status
    if test "$aid_mode" = --local-only
        return 0
    end
    if test "$aid_mode" = --allow-install
        cd "$aid_tree/packaging/arch"; or return $status
        __aidsc_logged BUILD "$aid_tmp/build.log" makepkg --cleanbuild --clean --noconfirm
        or return $status
        set -l aid_packages (makepkg --packagelist)
        or return $status
        __aidsc_step ONE_PACKAGE test (count $aid_packages) -eq 1
        or return $status
        set -l aid_package "$aid_packages[1]"
        test -f "$aid_package"; and not test -L "$aid_package"; or return 2
        set -l aid_package_info (/usr/bin/pacman -Qp "$aid_package")
        or return $status
        __aidsc_step PACKAGE_VERSION test "$aid_package_info" = 'ai-desktop 0.1.0-88'
        or return $status
        bsdtar -xOf "$aid_package" .INSTALL > "$aid_tmp/package.install"
        or return $status
        __aidsc_step PACKAGE_HOOK cmp "$aid_tree/packaging/arch/ai-desktop.install" "$aid_tmp/package.install"
        or return $status
        __aidsc_step SOURCE_DIFF git -C "$aid_tree" diff --exit-code "$aid_sha" --
        or return $status
        __aidsc_step UPGRADE sudo /usr/bin/pacman -U "$aid_package"
        or return $status
        __aidsc_step RELOAD sudo /usr/bin/systemctl daemon-reload
        or return $status
    end
    cd "$aid_tree"; or return $status
    __aidsc_logged SYSTEM_CLIENT "$aid_tmp/system-client.log" python3 -B tests/host_system_clients_acceptance.py --allow-offline-client-check "$aid_provider" --installed
    set -l aid_probe $status
    set -g aidsc_native "$aid_probe"
    return "$aid_probe"
end
set -l aid_script (realpath -- (status filename))
or exit 2
cd "$argv[3]"; or exit 2
set -l aid_repo (pwd -P)
set -l aid_top (git rev-parse --show-toplevel)
or exit 2
if test "$aid_repo" != "$aid_top"
    printf 'SYSTEM CLIENT ACCEPTANCE: REFUSED repository root required\n'
    exit 2
end
set -l aid_before (git rev-parse --verify HEAD)
or exit 2
set -l aid_tmp (mktemp -d /tmp/ai-desktop-system-clients-XXXXXXXX)
or exit 2
__aidsc_cycle "$argv[1]" "$argv[2]" "$aid_repo" "$argv[4]" "$aid_branch" "$aid_tmp" "$aid_script"
set -l aid_cycle $status
cd "$aid_repo"
set -l aid_return $status
set -l aid_after (git rev-parse --verify HEAD)
set -l aid_head $status
if test "$aid_head" -eq 0; and test "$aid_before" != "$aid_after"
    set aid_head 2
end
printf '\n%s\n' $aidsc_results
printf 'CYCLE=%s RETURN=%s ORIGINAL_HEAD=%s INSTALL=%s SYSTEM_CLIENT=%s\n' "$aid_cycle" "$aid_return" "$aid_head" "$aidsc_install" "$aidsc_native"
printf 'Logs and detached test worktree: %s\n' "$aid_tmp"
if test "$aid_cycle" -ne 0
    exit "$aid_cycle"
end
if test "$aid_return" -ne 0; or test "$aid_head" -ne 0
    exit 2
end
exit 0
