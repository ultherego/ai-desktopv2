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
    printf '\n=== PLAN ODBIORU NATYWNEGO (nic nie jest jeszcze wysylane) ===\n'
    printf 'Katalog testowy: %s/ws (swieze dane syntetyczne: calc.py, README.txt, docs/notes.txt)\n' "$aid_project"
    printf 'Zadania do Claude: 2 (przypadki B i C). Przypadek A: 0 zadan, odmowa przed sesja.\n'
    printf 'Wysylane dane na zadanie: instrukcja + sciezka calc.py + pelna tresc calc.py (ok. 40 B). Nic wiecej.\n'
    printf 'Zakres zapisu: tylko %s/ws/calc.py, po osobnym pytaniu przy diffie. Rollback: bez zadan do Claude.\n' "$aid_project"
    printf 'Odpowiedzi wpisujesz Ty. Skrypt sprawdza pliki sam po kazdym przypadku.\n\n'
    python3 -I -B "$aid_tree/scripts/e6_cycle.py" prepare
    set aid_prepare $status
end
if test "$aid_prepare" = 0
    set -l aid_ws "$aid_project/ws"
    set -l aid_requests 0
    set -l aid_stop ""
    printf '\n--- A: odmowa transferu ---\n'
    printf '>>> NA PYTANIE O WYSLANIE DANYCH WPISZ DOKLADNIE: nie   (t/tak wysle dane do Claude) <<<\n'
    /usr/bin/ai-desktop task run "Dodaj jednozdaniowy docstring do funkcji add." --workspace "$aid_ws" --file calc.py --test python-syntax
    set -l aid_a_cli $status
    python3 -I -B "$aid_tree/scripts/e6_cycle.py" check-unchanged
    set -l aid_a_check $status
    test $aid_a_cli -eq 2; and test $aid_a_check -eq 0
    set aid_a $status
    if test $aid_a -ne 0
        set aid_stop A
    end
    if test -z "$aid_stop"
        printf '\n--- B: odmowa zapisu przy diffie ---\n'
        printf '>>> WYSLANIE DANYCH: tak   ZAPIS ZMIAN (przy diffie): nie <<<\n'
        set aid_requests (math $aid_requests + 1)
        /usr/bin/ai-desktop task run "Dodaj jednozdaniowy docstring do funkcji add." --workspace "$aid_ws" --file calc.py --test python-syntax
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
        printf '\n--- C: poprawny zapis ---\n'
        printf '>>> WYSLANIE DANYCH: tak   ZAPIS ZMIAN (przy diffie): tak <<<\n'
        set aid_requests (math $aid_requests + 1)
        /usr/bin/ai-desktop task run "Dodaj jednozdaniowy docstring do funkcji add." --workspace "$aid_ws" --file calc.py --test python-syntax
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
        printf '\n--- D: cofniecie ---\n'
        printf '>>> NA PYTANIE O COFNIECIE WPISZ: tak <<<\n'
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
        printf '\nPRZERWANO po przypadku %s: kolejne zadania do Claude NIE zostaly wyslane.\n' "$aid_stop"
        printf 'Katalog %s zachowano do ogledzin. Jesli zapisano zmiane, cofnij: ai-desktop task rollback --workspace %s\n' "$aid_project" "$aid_ws"
        printf 'Po sprawdzeniu usun katalog (cleanup odmawia, dopoki zapis nie jest cofniety) i powtorz caly odbior.\n'
    end
    printf 'Zadania do Claude wyslane przez ten skrypt (poza A): %s z 2 planowanych.\n' $aid_requests
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
printf '\n=== WYNIKI ===\n'
printf 'MODE=%s FETCH=%s SHA_MATCH=%s WORKTREE=%s SCRIPT_MATCH=%s\n' "$aid_mode" $aid_fetch $aid_sha $aid_worktree $aid_script_match
printf 'CI=%s E6_OFFLINE=%s INSTALLED=%s PREPARE=%s\n' $aid_ci $aid_e6 $aid_installed $aid_prepare
printf 'A_TRANSFER_REFUSED=%s B_WRITE_REFUSED=%s C_WRITE_OK=%s D_ROLLBACK_OK=%s CLEANUP=%s\n' $aid_a $aid_b $aid_c $aid_d $aid_clean
printf 'MAIN_UNCHANGED=%s BACK=%s WORKTREE_CLEANUP=%s\n' $aid_main $aid_back $aid_wt_cleanup
printf 'Oczekiwane: wszystko 0; w trybie --offline pola natywne maja NOT_RUN (to nie jest PASS natywny).\n'
if test "$aid_mode" = --offline
    test $aid_ci -eq 0; and test $aid_e6 -eq 0; and test $aid_main -eq 0; and test $aid_back -eq 0; and exit 0
    exit 1
end
test $aid_ci -eq 0; and test $aid_e6 -eq 0; and test "$aid_installed" = 0; and test "$aid_prepare" = 0
and test "$aid_a" = 0; and test "$aid_b" = 0; and test "$aid_c" = 0; and test "$aid_d" = 0; and test "$aid_clean" = 0
and test $aid_main -eq 0; and test $aid_back -eq 0; and exit 0
exit 1
