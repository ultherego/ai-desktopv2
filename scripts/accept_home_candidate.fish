#!/usr/bin/fish
# Explicit CachyOS acceptance only. Invoke through a SHA-pinned bootstrap.
# This script builds/installs the candidate and performs two subscription
# requests authorized together. CI checks syntax and the no-opt-in refusal.
if test (count $argv) -ne 3; or test "$argv[1]" != --allow-cachyos-acceptance
    printf 'ACCEPTANCE: REFUSED; required: --allow-cachyos-acceptance SHA REPO_ROOT\n'
    exit 2
end
if not string match -rq '^[0-9a-f]{40}$' -- "$argv[2]"
    printf 'ACCEPTANCE: REFUSED invalid SHA\n'
    exit 2
end
if not string match -rq '^/' -- "$argv[3]"
    printf 'ACCEPTANCE: REFUSED absolute repository root required\n'
    exit 2
end
if not test (id -u) -gt 0; or not test -t 0; or not test -t 1
    printf 'ACCEPTANCE: REFUSED non-root terminal required\n'
    exit 2
end

function __aid16_step
    set -l aid_label $argv[1]
    set -e argv[1]
    $argv
    set -l aid_code $status
    printf '%s=%s\n' "$aid_label" "$aid_code"
    return "$aid_code"
end
function __aid16_logged
    set -l aid_label $argv[1]
    set -l aid_log $argv[2]
    set -e argv[1..2]
    $argv 2>&1 | tee "$aid_log"
    set -l aid_codes $pipestatus
    printf '%s=%s LOG=%s\n' "$aid_label" "$aid_codes[1]" "$aid_codes[2]"
    if test "$aid_codes[1]" -ne 0
        return "$aid_codes[1]"
    end
    return "$aid_codes[2]"
end
function __aid16_cycle --argument-names aid_expected aid_repo aid_tmp
    test (id -u) -gt 0; and test -t 0; and test -t 1; or return 2
    string match -rq '^[0-9a-f]{40}$' -- "$aid_expected"; or return 2
    __aid16_step FETCH git -C "$aid_repo" fetch origin refs/heads/feature/owner-permissions
    or return $status
    set -l aid_fetched (git -C "$aid_repo" rev-parse --verify FETCH_HEAD)
    or return $status
    printf 'EXPECTED_SHA=%s\nFETCHED_SHA=%s\n' "$aid_expected" "$aid_fetched"
    __aid16_step SHA_MATCH test "$aid_fetched" = "$aid_expected"
    or return $status
    set -l aid_tree "$aid_tmp/source"
    __aid16_step WORKTREE git -C "$aid_repo" worktree add --detach "$aid_tree" "$aid_expected"
    or return $status
    cd "$aid_tree"; or return $status
    __aid16_step COMMIT git log -1 --format='%H %s'
    or return $status
    # Native clients are independently installed system packages; never copy them into this build.
    __aid16_logged CI "$aid_tmp/ci.log" python3 -B scripts/verify.py --mode ci --fail-fast --json "$aid_tmp/ci.json"
    or return $status
    __aid16_logged LOCAL "$aid_tmp/local.log" python3 -B scripts/verify.py --mode local --fail-fast --json "$aid_tmp/local.json"
    or return $status
    __aid16_step SOURCE_DIFF git diff --exit-code "$aid_expected" --
    or return $status
    set -l aid_dirty (git status --porcelain --untracked-files=normal)
    or return $status
    __aid16_step SOURCE_CLEAN test -z "$aid_dirty"
    or return $status
    cd "$aid_tree/packaging/arch"; or return $status
    __aid16_logged BUILD "$aid_tmp/build.log" makepkg --cleanbuild --clean --noconfirm
    or return $status
    set -l aid_packages (makepkg --packagelist)
    or return $status
    __aid16_step ONE_PACKAGE test (count $aid_packages) -eq 1
    or return $status
    set -l aid_package "$aid_packages[1]"
    test -f "$aid_package"; and not test -L "$aid_package"; or return 2
    set -l aid_package_info (/usr/bin/pacman -Qp "$aid_package")
    or return $status
    __aid16_step PACKAGE_VERSION test "$aid_package_info" = 'ai-desktop 0.1.0-16'
    or return $status
    bsdtar -xOf "$aid_package" .INSTALL > "$aid_tmp/package.install"
    or return $status
    __aid16_step PACKAGE_HOOK cmp "$aid_tree/packaging/arch/ai-desktop.install" "$aid_tmp/package.install"
    or return $status
    __aid16_step BUILT_SOURCE git -C "$aid_tree" diff --exit-code "$aid_expected" --
    or return $status
    __aid16_step UPGRADE sudo /usr/bin/pacman -U "$aid_package"
    or return $status
    __aid16_step RELOAD sudo /usr/bin/systemctl daemon-reload
    or return $status
    cd "$aid_tree"; or return $status
    __aid16_step INSTALLED python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv[1] + "/tests"); import host_analysis_acceptance as check; check.installed_matches(); print("INSTALLED MATCH: PASS")' "$aid_tree"
    or return $status
    # No tee: consent to the question, the full diff, the write and the rollback stay on the TTY.
    printf '\nNew acceptance: a HOME grant, one named file and a new hello.py; two Claude requests.\n'
    printf 'Approve the write after the test/diff, then the undo, separately. Your policy is not changed.\n'
    __aid16_step HOST_HOME python3 -B tests/host_home_policy_acceptance.py --allow-subscription-home-test
    or return $status
    return 0
end

set -l aid_expected "$argv[2]"
cd "$argv[3]"
or begin
    printf 'ENTER=FAIL CYCLE=2 RETURN=2\n'
    exit 2
end
set -l aid_repo (pwd -P)
set -l aid_top (git rev-parse --show-toplevel)
if test "$aid_top" != "$aid_repo"
    printf 'SETUP=FAIL CYCLE=2 RETURN=0\n'
    exit 2
end
set -l aid_before (git rev-parse --verify HEAD)
or begin
    printf 'SETUP=FAIL CYCLE=2 RETURN=0\n'
    exit 2
end
set -l aid_tmp (mktemp -d /tmp/ai-desktop-home-XXXXXXXX)
if test $status -ne 0; or test -z "$aid_tmp"
    printf 'SETUP=FAIL CYCLE=2 RETURN=0\n'
    exit 2
end
__aid16_cycle "$aid_expected" "$aid_repo" "$aid_tmp"
set -l aid_cycle $status
cd "$aid_repo"
set -l aid_return $status
if test "$aid_return" -eq 0
    set -l aid_after (git rev-parse --verify HEAD)
    __aid16_step MAIN_UNCHANGED test "$aid_after" = "$aid_before"
    set -l aid_main_check $status
    if test "$aid_cycle" -eq 0; and test "$aid_main_check" -ne 0
        set aid_cycle "$aid_main_check"
    end
end
printf '\nCYCLE=%s RETURN=%s\n' "$aid_cycle" "$aid_return"
printf 'Logs and worktree kept: %s\n' "$aid_tmp"
printf 'The HOST HOME conversation was not saved; the test uses only synthetic data and policy.\n'
if test "$aid_cycle" -ne 0
    exit "$aid_cycle"
end
exit "$aid_return"
