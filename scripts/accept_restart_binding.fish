#!/usr/bin/fish
# E5 restart acceptance: prepare BEFORE and verify AFTER a natural restart.
# One SHA-pinned worktree per call; tests the INSTALLED package. Never installs,
# restarts, commits or touches ~/hello.py or any older private journal.
if test (count $argv) -eq 1; and test "$argv[1]" = --help
    printf 'Usage: fish scripts/accept_restart_binding.fish MODE SHA REPO_ROOT [BRANCH]\n'
    printf '  --prepare   new schema-3 task on a synthetic file in ~/ai-restart-check (before restart)\n'
    printf '  --verify    after a restart: binding, file state, controlled rollback (bytes + mode)\n'
    printf '  --cleanup   remove ~/ai-restart-check after verify rolled the task back\n'
    printf 'Default branch: main. SHA must match its fetched HEAD and the installed package.\n'
    printf 'Exit: 0 ok, 1 check failed, 2 refused, 3 no restart detected yet.\n'
    exit 0
end
if test (count $argv) -lt 3; or test (count $argv) -gt 4; or not contains -- "$argv[1]" --prepare --verify --cleanup
    printf 'RESTART: REFUSED; required: MODE SHA REPO_ROOT [BRANCH]\n'
    exit 2
end
if not string match -rq '^[0-9a-f]{40}$' -- "$argv[2]"; or not string match -rq '^/' -- "$argv[3]"
    printf 'RESTART: REFUSED invalid SHA or repository root\n'
    exit 2
end
set -l aid_mode "$argv[1]"
set -l aid_expected "$argv[2]"
set -l aid_repo "$argv[3]"
set -l aid_branch main
if test (count $argv) -eq 4
    set aid_branch "$argv[4]"
end
if not string match -rq '^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$' -- "$aid_branch"; or string match -rq '(^|/)\.|\.\.|/$|//' -- "$aid_branch"
    printf 'RESTART: REFUSED invalid branch\n'
    exit 2
end
if test (id -u) -eq 0; or not test -t 0; or not test -t 1
    printf 'RESTART: REFUSED non-root terminal required\n'
    exit 2
end
set -l aid_script (status filename)
cd "$aid_repo"; or exit 2
set -l aid_before (git rev-parse --verify HEAD); or exit 2
git fetch origin "refs/heads/$aid_branch"
set -l aid_fetch $status
set -l aid_fetched (git rev-parse --verify FETCH_HEAD 2>/dev/null)
test "$aid_fetched" = "$aid_expected"
set -l aid_sha $status
set -l aid_tmp (mktemp -d /tmp/ai-desktop-restart-XXXXXXXX)
set -l aid_tmpcode $status
set -l aid_tree "$aid_tmp/source"
set -l aid_worktree 99
set -l aid_script_match 99
set -l aid_installed 99
set -l aid_helper 99
if test $aid_fetch -eq 0; and test $aid_sha -eq 0; and test $aid_tmpcode -eq 0
    git worktree add --detach "$aid_tree" "$aid_expected"
    set aid_worktree $status
end
if test $aid_worktree -eq 0
    cmp -- "$aid_script" "$aid_tree/scripts/accept_restart_binding.fish"
    set aid_script_match $status
end
if test $aid_script_match -eq 0
    python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv[1] + "/tests"); import host_analysis_acceptance as check; check.installed_matches(native=False); print("INSTALLED MATCH: PASS")' "$aid_tree"
    set aid_installed $status
end
if test $aid_installed -eq 0
    # The helper imports the INSTALLED package; the SHA-pinned worktree only supplies the helper.
    python3 -I -B "$aid_tree/scripts/restart_binding.py" (string sub -s 3 -- "$aid_mode") --lib /usr/lib/ai-desktop
    set aid_helper $status
end
cd "$aid_repo"
set -l aid_back $status
set -l aid_after (git rev-parse --verify HEAD)
test "$aid_after" = "$aid_before"
set -l aid_main $status
if test $aid_worktree -eq 0
    git worktree remove --force "$aid_tree"
end
set -l aid_cleanup $status
rmdir "$aid_tmp" 2>/dev/null
printf '\n=== WYNIKI ===\n'
printf 'MODE=%s SHA_MATCH=%s FETCH=%s WORKTREE=%s SCRIPT_MATCH=%s\n' "$aid_mode" $aid_sha $aid_fetch $aid_worktree $aid_script_match
printf 'INSTALLED=%s HELPER=%s MAIN_UNCHANGED=%s BACK=%s WORKTREE_CLEANUP=%s\n' $aid_installed $aid_helper $aid_main $aid_back $aid_cleanup
printf 'Komputer nie byl restartowany przez skrypt; ~/hello.py i dawne dzienniki nie sa dotykane.\n'
test $aid_helper -eq 0; and test $aid_main -eq 0; and test $aid_back -eq 0; and exit 0
test $aid_helper -ne 99; and test $aid_helper -ne 0; and exit $aid_helper
exit 2
