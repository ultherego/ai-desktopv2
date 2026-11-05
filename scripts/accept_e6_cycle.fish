#!/usr/bin/fish
# E6 full-cycle acceptance on a FRESH synthetic project in ~/ai-e6-check.
# Offline mode: SHA-pinned worktree + central CI gate (includes the fake-model cycle tests).
# Native mode: additionally runs the INSTALLED app; YOU type every answer at its normal
# prompts. This script never feeds input to the app, never installs or restarts anything,
# and checks the files itself instead of trusting printed PASS messages.
if test (count $argv) -eq 1; and test "$argv[1]" = --help
    printf 'Usage: fish scripts/accept_e6_cycle.fish MODE SHA REPO_ROOT [BRANCH]\n'
    printf '  --offline  CI gate in a pinned worktree; no model, no installed app\n'
    printf '  --native   offline + guided run of the installed app (2 real Claude requests)\n'
    printf 'Default branch: main. Native needs INSTALLED MATCH for SHA.\n'
    exit 0
end
if test (count $argv) -lt 3; or test (count $argv) -gt 4; or not contains -- "$argv[1]" --offline --native
    printf 'E6: REFUSED; required: MODE SHA REPO_ROOT [BRANCH]\n'
    exit 2
end
if not string match -rq '^[0-9a-f]{40}$' -- "$argv[2]"; or not string match -rq '^/' -- "$argv[3]"
    printf 'E6: REFUSED invalid SHA or repository root\n'
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
    printf 'E6: REFUSED invalid branch\n'
    exit 2
end
if test (id -u) -eq 0; or not test -t 0; or not test -t 1
    printf 'E6: REFUSED non-root terminal required\n'
    exit 2
end
set -l aid_script (status filename)
set -l aid_project "$HOME/ai-e6-check"
if test "$aid_mode" = --native; and test -e "$aid_project"
    printf 'E6: REFUSED %s exists; remove it with e6_cycle.py cleanup first\n' "$aid_project"
    exit 2
end
cd "$aid_repo"; or exit 2
set -l aid_before (git rev-parse --verify HEAD); or exit 2
git fetch origin "refs/heads/$aid_branch"
set -l aid_fetch $status
set -l aid_fetched (git rev-parse --verify FETCH_HEAD 2>/dev/null)
test "$aid_fetched" = "$aid_expected"
set -l aid_sha $status
set -l aid_tmp (mktemp -d /tmp/ai-desktop-e6-XXXXXXXX)
set -l aid_tree "$aid_tmp/source"
set -l aid_worktree 99
set -l aid_script_match 99
set -l aid_ci 99
set -l aid_e6 99
set -l aid_installed NOT_RUN
set -l aid_prepare NOT_RUN
set -l aid_a NOT_RUN
set -l aid_b NOT_RUN
set -l aid_c NOT_RUN
set -l aid_d NOT_RUN
set -l aid_clean NOT_RUN
if test $aid_fetch -eq 0; and test $aid_sha -eq 0
    git worktree add --detach "$aid_tree" "$aid_expected"
    set aid_worktree $status
end
if test $aid_worktree -eq 0
    cmp -- "$aid_script" "$aid_tree/scripts/accept_e6_cycle.fish"
    set aid_script_match $status
end
if test $aid_script_match -eq 0
    cd "$aid_tree"
    python3 -B scripts/verify.py --mode ci --fail-fast --json "$aid_tmp/ci.json"
    set aid_ci $status
    python3 -B -m unittest discover -s tests -p test_e6_cycle.py
    set aid_e6 $status
    cd "$aid_repo"
end
if test "$aid_mode" = --native; and test $aid_ci -eq 0; and test $aid_e6 -eq 0
    set aid_installed 99
    python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv[1] + "/tests"); import host_analysis_acceptance as check; check.installed_matches(native=False); print("INSTALLED MATCH: PASS")' "$aid_tree"
    set aid_installed $status
end
if test "$aid_installed" = 0
    printf '\n=== NATIVE ACCEPTANCE PLAN (nothing is sent yet) ===\n'
    printf 'Test directory: %s/ws (fresh synthetic data: calc.py, README.txt, docs/notes.txt)\n' "$aid_project"
    printf 'Requests to Claude: 2 (cases B and C). Case A: 0 requests, refused before the session.\n'
    printf 'Data sent per request: instruction + calc.py path + full content of calc.py (about 40 B). Nothing more.\n'
    printf 'Write scope: only %s/ws/calc.py, after a separate question at the diff. Rollback: no requests to Claude.\n' "$aid_project"
    printf 'You type the answers. The script checks the files itself after each case.\n\n'
    python3 -I -B "$aid_tree/scripts/e6_cycle.py" prepare
    set aid_prepare $status
end
if test "$aid_prepare" = 0
    set -l aid_ws "$aid_project/ws"
    set -l aid_requests 0
    set -l aid_stop ""
    printf '\n--- A: transfer refused ---\n'
    printf '>>> AT THE QUESTION ABOUT SENDING DATA TYPE EXACTLY: n   (y/yes sends the data to Claude) <<<\n'
    /usr/bin/ai-desktop task run "Add a one-sentence docstring to the add function." --workspace "$aid_ws" --file calc.py --test python-syntax
    set -l aid_a_cli $status
    python3 -I -B "$aid_tree/scripts/e6_cycle.py" check-unchanged
    set -l aid_a_check $status
    test $aid_a_cli -eq 2; and test $aid_a_check -eq 0
    set aid_a $status
    if test $aid_a -ne 0
        set aid_stop A
    end
    if test -z "$aid_stop"
        printf '\n--- B: write refused at the diff ---\n'
        printf '>>> SENDING DATA: y   SAVING CHANGES (at the diff): n <<<\n'
        set aid_requests (math $aid_requests + 1)
        /usr/bin/ai-desktop task run "Add a one-sentence docstring to the add function." --workspace "$aid_ws" --file calc.py --test python-syntax
        set -l aid_b_cli $status
        python3 -I -B "$aid_tree/scripts/e6_cycle.py" check-unchanged
        set -l aid_b_check $status
        test $aid_b_cli -eq 2; and test $aid_b_check -eq 0
        set aid_b $status
        if test $aid_b -ne 0
            set aid_stop B
        end
    end
    if test -z "$aid_stop"
        printf '\n--- C: successful write ---\n'
        printf '>>> SENDING DATA: y   SAVING CHANGES (at the diff): y <<<\n'
        set aid_requests (math $aid_requests + 1)
        /usr/bin/ai-desktop task run "Add a one-sentence docstring to the add function." --workspace "$aid_ws" --file calc.py --test python-syntax
        set -l aid_c_cli $status
        python3 -I -B "$aid_tree/scripts/e6_cycle.py" check-written
        set -l aid_c_check $status
        test $aid_c_cli -eq 0; and test $aid_c_check -eq 0
        set aid_c $status
        if test $aid_c -ne 0
            set aid_stop C
        end
    end
    if test -z "$aid_stop"
        printf '\n--- D: undo ---\n'
        printf '>>> AT THE QUESTION ABOUT UNDO TYPE: y <<<\n'
        /usr/bin/ai-desktop task rollback --workspace "$aid_ws"
        set -l aid_d_cli $status
        python3 -I -B "$aid_tree/scripts/e6_cycle.py" check-rolled-back
        set -l aid_d_check $status
        test $aid_d_cli -eq 0; and test $aid_d_check -eq 0
        set aid_d $status
        if test $aid_d -eq 0
            python3 -I -B "$aid_tree/scripts/e6_cycle.py" cleanup
            set aid_clean $status
        else
            set aid_stop D
        end
    end
    if test -n "$aid_stop"
        printf '\nSTOPPED after case %s: further requests to Claude were NOT sent.\n' "$aid_stop"
        printf 'Directory %s kept for inspection. If a change was saved, undo it: ai-desktop task rollback --workspace %s\n' "$aid_project" "$aid_ws"
        printf 'After checking, remove the directory (cleanup refuses until the write is undone) and repeat the whole acceptance.\n'
    end
    printf 'Requests to Claude sent by this script (outside A): %s of 2 planned.\n' $aid_requests
end
cd "$aid_repo"
set -l aid_back $status
set -l aid_after (git rev-parse --verify HEAD)
test "$aid_after" = "$aid_before"
set -l aid_main $status
if test $aid_worktree -eq 0
    git worktree remove --force "$aid_tree"
end
set -l aid_wt_cleanup $status
rmdir "$aid_tmp" 2>/dev/null
printf '\n=== RESULTS ===\n'
printf 'MODE=%s FETCH=%s SHA_MATCH=%s WORKTREE=%s SCRIPT_MATCH=%s\n' "$aid_mode" $aid_fetch $aid_sha $aid_worktree $aid_script_match
printf 'CI=%s E6_OFFLINE=%s INSTALLED=%s PREPARE=%s\n' $aid_ci $aid_e6 $aid_installed $aid_prepare
printf 'A_TRANSFER_REFUSED=%s B_WRITE_REFUSED=%s C_WRITE_OK=%s D_ROLLBACK_OK=%s CLEANUP=%s\n' $aid_a $aid_b $aid_c $aid_d $aid_clean
printf 'MAIN_UNCHANGED=%s BACK=%s WORKTREE_CLEANUP=%s\n' $aid_main $aid_back $aid_wt_cleanup
printf 'Expected: all 0; in --offline mode the native fields show NOT_RUN (that is not a native PASS).\n'
if test "$aid_mode" = --offline
    test $aid_ci -eq 0; and test $aid_e6 -eq 0; and test $aid_main -eq 0; and test $aid_back -eq 0; and exit 0
    exit 1
end
test $aid_ci -eq 0; and test $aid_e6 -eq 0; and test "$aid_installed" = 0; and test "$aid_prepare" = 0
and test "$aid_a" = 0; and test "$aid_b" = 0; and test "$aid_c" = 0; and test "$aid_d" = 0; and test "$aid_clean" = 0
and test $aid_main -eq 0; and test $aid_back -eq 0; and exit 0
exit 1
