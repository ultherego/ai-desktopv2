#!/usr/bin/fish
# Material acceptance: `ai-desktop material` on SYNTHETIC files in ~/ai-lc1-check (questions in plain words).
# --offline: SHA-pinned worktree + central CI gate (no model, no installed app).
# --native : offline + file-based refusal checks with the INSTALLED code under your REAL policy,
#            then two guided interactive sessions (real Claude requests, YOU type every answer).
# The script never feeds input to the app, never installs or restarts, and stops before any
# session if an earlier check fails.
if test (count $argv) -eq 1; and test "$argv[1]" = --help
    printf 'Usage: fish scripts/accept_material.fish MODE SHA REPO_ROOT [BRANCH]\n'
    printf '  --offline  CI gate in a pinned worktree\n  --native   offline + refusals + three guided sessions (several requests)\n'
    printf '  --native-c repeat ONLY session C (whole file) in the directory left by an interrupted --native run\n'
    exit 0
end
if test (count $argv) -lt 3; or test (count $argv) -gt 4; or not contains -- "$argv[1]" --offline --native --native-c
    printf 'LC1: REFUSED; required: MODE SHA REPO_ROOT [BRANCH]\n'
    exit 2
end
if not string match -rq '^[0-9a-f]{40}$' -- "$argv[2]"; or not string match -rq '^/' -- "$argv[3]"
    printf 'LC1: REFUSED invalid SHA or repository root\n'
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
    printf 'LC1: REFUSED invalid branch\n'
    exit 2
end
if test (id -u) -eq 0; or not test -t 0; or not test -t 1
    printf 'LC1: REFUSED non-root terminal required\n'
    exit 2
end
set -l aid_script (status filename)
set -l aid_project "$HOME/ai-lc1-check"
if test "$aid_mode" = --native; and test -e "$aid_project"
    printf 'LC1: REFUSED %s exists; remove it first\n' "$aid_project"
    exit 2
end
if test "$aid_mode" = --native-c; and not test -f "$aid_project/medium.log"
    printf 'LC1: REFUSED %s/medium.log missing; --native-c continues an interrupted --native run\n' "$aid_project"
    exit 2
end
cd "$aid_repo"; or exit 2
set -l aid_before (git rev-parse --verify HEAD); or exit 2
git fetch origin "refs/heads/$aid_branch"
set -l aid_fetch $status
set -l aid_fetched (git rev-parse --verify FETCH_HEAD 2>/dev/null)
test "$aid_fetched" = "$aid_expected"
set -l aid_sha $status
set -l aid_tmp (mktemp -d /tmp/ai-desktop-lc1-XXXXXXXX)
set -l aid_tree "$aid_tmp/source"
set -l aid_worktree 99
set -l aid_script_match 99
set -l aid_ci 99
set -l aid_lc 99
set -l aid_installed NOT_RUN
set -l aid_prepare NOT_RUN
set -l aid_refusals NOT_RUN
set -l aid_session NOT_RUN
set -l aid_session_a NOT_RUN
set -l aid_session_c NOT_RUN
set -l aid_unchanged NOT_RUN
set -l aid_clean NOT_RUN
if test $aid_fetch -eq 0; and test $aid_sha -eq 0
    git worktree add --detach "$aid_tree" "$aid_expected"
    set aid_worktree $status
end
if test $aid_worktree -eq 0
    cmp -- "$aid_script" "$aid_tree/scripts/accept_material.fish"
    set aid_script_match $status
end
if test $aid_script_match -eq 0
    cd "$aid_tree"
    python3 -B scripts/verify.py --mode ci --fail-fast --json "$aid_tmp/ci.json"
    set aid_ci $status
    python3 -B -m unittest discover -s tests -p 'test_material*.py'
    set aid_lc $status
    cd "$aid_repo"
end
if contains -- "$aid_mode" --native --native-c; and test $aid_ci -eq 0; and test $aid_lc -eq 0
    set aid_installed 99
    python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv[1] + "/tests"); import host_analysis_acceptance as check; check.installed_matches(native=False); print("INSTALLED MATCH: PASS")' "$aid_tree"
    set aid_installed $status
end
if test "$aid_mode" = --native-c; and test "$aid_installed" = 0
    printf '\n=== PLAN: TYLKO SESJA C (nic nie jest jeszcze wysylane) ===\n'
    printf 'Prosba o caly plik medium.log: zwykle 4-5 zadan, caly plik (ok. 266 KiB) zostanie wyslany w 3 czesciach. Zapis plikow: brak. Internet: brak.\n'
    printf 'Odmowy plikow oraz sesje A i B zaliczono w przerwanym przebiegu --native; ten tryb ich nie powtarza.\n'
    printf '\n--- Sesja C: log ok. 200 KB, prosba o caly plik ---\n'
    printf '  Przeanalizuj caly plik i powiedz, jakie bledy w nim wystepuja.   >>> tak (plan), tak (etap 1)\n'
    printf '  Oczekiwane: 3 czesci, pokrycie 3 z 3 (100%%), wnioski z kodami E-100 i E-2900. /rejestr, /koniec\n\n'
    /usr/bin/ai-desktop material "$aid_project/medium.log" --v
    set aid_session_c $status
    python3 -I -B "$aid_tree/scripts/lc1_check.py" check-unchanged
    set aid_unchanged $status
    if test $aid_unchanged -eq 0
        python3 -I -B "$aid_tree/scripts/lc1_check.py" cleanup
        set aid_clean $status
    else
        printf 'Katalog %s zachowano do ogledzin.\n' "$aid_project"
    end
end
if test "$aid_mode" = --native; and test "$aid_installed" = 0
    printf '\n=== PLAN ODBIORU NATYWNEGO (nic nie jest jeszcze wysylane) ===\n'
    printf 'Katalog testowy: %s (swieze pliki syntetyczne; Twoje pliki nie sa uzywane)\n' "$aid_project"
    printf 'Zadania do Claude: razem zwykle 12-18, najwyzej ok. 30 (kazde pytanie ma twardy limit 8 zadan).\n'
    printf '  Sesja A, maly plik (1 KiB): 1 zadanie, wysylany jest CALY ten plik.\n'
    printf '  Sesja B, log 1 MiB (limit pliku to 5 MiB): Claude sam wybiera fragmenty (wyszukiwanie i odczyt tego jednego pliku), do 8 zadan na pytanie;\n'
    printf '            odmowa zgody = 0 zadan; pytanie nastepcze: po najwyzej 8.\n'
    printf '  Sesja C, log ok. 200 KB: prosba o caly plik = 1 zadanie planujace + 3 czesci + 1 podsumowanie (5), caly plik zostanie wyslany.\n'
    printf 'Dane: pytanie + tylko fragmenty, o ktore Claude poprosi (w sesji B nigdy caly 1 MiB). Zapis plikow: brak. Internet: brak.\n'
    printf 'Odpowiedzi wpisujesz Ty. Pytania zadajesz zwyklymi zdaniami (bez /pytaj i numerow wierszy).\n\n'
    python3 -I -B "$aid_tree/scripts/lc1_check.py" prepare
    set aid_prepare $status
end
if test "$aid_prepare" = 0
    printf '\n--- Odmowy plikow na ZAINSTALOWANYM kodzie i Twojej polityce (bez modelu) ---\n'
    python3 -I -B "$aid_tree/scripts/lc1_check.py" refusals --lib /usr/lib/ai-desktop
    set aid_refusals $status
end
if test "$aid_refusals" = 0
    printf '\n--- Sesja A: maly plik. Wpisz zwyklym zdaniem, potem na pytanie o wyslanie: tak ---\n'
    printf '  Przeanalizuj ten plik i wskaz mozliwe bledy.        >>> tak   (cale small.py, 1 zadanie)\n'
    printf '  /rejestr   potem   /koniec\n\n'
    /usr/bin/ai-desktop material "$aid_project/small.py" --v
    set aid_session_a $status
    printf '\n--- Sesja B: duzy log 1 MiB. Pytaj zwyklymi zdaniami ---\n'
    printf '  1  Co oznaczaja wpisy ERROR-MARKER w tym logu?        >>> najpierw: nie  (0 zadan)\n'
    printf '  2  to samo pytanie jeszcze raz                          >>> tak   (Claude sam szuka i czyta)\n'
    printf '  3  Wyjasnij pierwszy punkt poprzedniej odpowiedzi       >>> tak   (pamiec rozmowy)\n'
    printf '  (wiecej pytan nie trzeba; /rejestr, /koniec)\n'
    printf '  /rejestr   potem   /koniec\n'
    printf 'Wklej mi: /rejestr z obu sesji, linie CZAS, liste krokow (Krok n/8) i odpowiedzi.\n\n'
    /usr/bin/ai-desktop material "$aid_project/log1m.txt" --v
    set aid_session $status
    printf '\n--- Sesja C: log ok. 200 KB, prosba o caly plik ---\n'
    printf '  Przeanalizuj caly plik i powiedz, jakie bledy w nim wystepuja.   >>> tak (plan), tak (etap 1)\n'
    printf '  Oczekiwane: 3 czesci, pokrycie 3 z 3 (100%%), wnioski z kodami E-100 i E-2900. /rejestr, /koniec\n\n'
    /usr/bin/ai-desktop material "$aid_project/medium.log" --v
    set aid_session_c $status
    python3 -I -B "$aid_tree/scripts/lc1_check.py" check-unchanged
    set aid_unchanged $status
    if test $aid_unchanged -eq 0
        python3 -I -B "$aid_tree/scripts/lc1_check.py" cleanup
        set aid_clean $status
    else
        printf 'Katalog %s zachowano do ogledzin.\n' "$aid_project"
    end
else if test "$aid_prepare" = 0
    printf '\nPRZERWANO przed sesja: weryfikacja odmow/polityki nie przeszla; zadne zadanie do Claude nie zostalo wyslane.\n'
    printf 'Katalog %s zachowano do ogledzin (usun: python3 scripts/lc1_check.py cleanup).\n' "$aid_project"
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
printf 'CI=%s LC_TESTS=%s INSTALLED=%s PREPARE=%s REFUSALS=%s\n' $aid_ci $aid_lc $aid_installed $aid_prepare $aid_refusals
printf 'SESSION_A=%s SESSION_B=%s SESSION_C=%s SOURCES_UNCHANGED=%s CLEANUP=%s\n' $aid_session_a $aid_session $aid_session_c $aid_unchanged $aid_clean
printf 'MAIN_UNCHANGED=%s BACK=%s WORKTREE_CLEANUP=%s\n' $aid_main $aid_back $aid_wt_cleanup
printf 'Oczekiwane: wszystko 0; w trybie --offline pola natywne maja NOT_RUN (to nie jest PASS natywny).\n'
if test "$aid_mode" = --offline
    test $aid_ci -eq 0; and test $aid_lc -eq 0; and test $aid_main -eq 0; and test $aid_back -eq 0; and exit 0
    exit 1
end
if test "$aid_mode" = --native-c
    test $aid_ci -eq 0; and test $aid_lc -eq 0; and test "$aid_installed" = 0; and test "$aid_session_c" = 0
    and test "$aid_unchanged" = 0; and test "$aid_clean" = 0; and test $aid_main -eq 0; and test $aid_back -eq 0; and exit 0
    exit 1
end
test $aid_ci -eq 0; and test $aid_lc -eq 0; and test "$aid_installed" = 0; and test "$aid_prepare" = 0
and test "$aid_refusals" = 0; and test "$aid_session_a" = 0; and test "$aid_session" = 0; and test "$aid_session_c" = 0; and test "$aid_unchanged" = 0; and test "$aid_clean" = 0
and test $aid_main -eq 0; and test $aid_back -eq 0; and exit 0
exit 1
