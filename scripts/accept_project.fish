#!/usr/bin/fish
# Project acceptance (LC4): `ai-desktop project` on a SYNTHETIC project in ~/ai-lc4-check (questions in plain words).
# --offline: SHA-pinned worktree + central CI gate (no model, no installed app).
# --native : offline + installed-code scan checks under your REAL policy, then one guided session
#            (real Claude requests, YOU type every answer). The script never feeds input to the app,
#            never edits your policy, never installs or restarts, and stops before the session if a check fails.
if test (count $argv) -eq 1; and test "$argv[1]" = --help
    printf 'Usage: fish scripts/accept_project.fish MODE SHA REPO_ROOT [BRANCH]\n'
    printf '  --offline  CI gate in a pinned worktree\n  --native   offline + scan checks + one guided session\n'
    exit 0
end
if test (count $argv) -lt 3; or test (count $argv) -gt 4; or not contains -- "$argv[1]" --offline --native
    printf 'LC4: REFUSED; required: MODE SHA REPO_ROOT [BRANCH]\n'
    exit 2
end
if not string match -rq '^[0-9a-f]{40}$' -- "$argv[2]"; or not string match -rq '^/' -- "$argv[3]"
    printf 'LC4: REFUSED invalid SHA or repository root\n'
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
    printf 'LC4: REFUSED invalid branch\n'
    exit 2
end
if test (id -u) -eq 0; or not test -t 0; or not test -t 1
    printf 'LC4: REFUSED non-root terminal required\n'
    exit 2
end
set -l aid_script (status filename)
set -l aid_project "$HOME/ai-lc4-check"
if test "$aid_mode" = --native; and test -e "$aid_project"
    printf 'LC4: REFUSED %s exists; remove it first\n' "$aid_project"
    exit 2
end
cd "$aid_repo"; or exit 2
set -l aid_before (git rev-parse --verify HEAD); or exit 2
git fetch origin "refs/heads/$aid_branch"
set -l aid_fetch $status
set -l aid_fetched (git rev-parse --verify FETCH_HEAD 2>/dev/null)
test "$aid_fetched" = "$aid_expected"
set -l aid_sha $status
set -l aid_tmp (mktemp -d /tmp/ai-desktop-lc4-XXXXXXXX)
set -l aid_tree "$aid_tmp/source"
set -l aid_worktree 99
set -l aid_script_match 99
set -l aid_ci 99
set -l aid_lc 99
set -l aid_installed NOT_RUN
set -l aid_prepare NOT_RUN
set -l aid_policy NOT_RUN
set -l aid_scan NOT_RUN
set -l aid_session NOT_RUN
set -l aid_unchanged NOT_RUN
set -l aid_clean NOT_RUN
if test $aid_fetch -eq 0; and test $aid_sha -eq 0
    git worktree add --detach "$aid_tree" "$aid_expected"
    set aid_worktree $status
end
if test $aid_worktree -eq 0
    cmp -- "$aid_script" "$aid_tree/scripts/accept_project.fish"
    set aid_script_match $status
end
if test $aid_script_match -eq 0
    cd "$aid_tree"
    python3 -B scripts/verify.py --mode ci --fail-fast --json "$aid_tmp/ci.json"
    set aid_ci $status
    python3 -B -m unittest discover -s tests -p 'test_project*.py'
    set aid_lc $status
    cd "$aid_repo"
end
if test "$aid_mode" = --native; and test $aid_ci -eq 0; and test $aid_lc -eq 0
    set aid_installed 99
    python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv[1] + "/tests"); import host_analysis_acceptance as check; check.installed_matches(native=False); print("INSTALLED MATCH: PASS")' "$aid_tree"
    set aid_installed $status
end
if test "$aid_installed" = 0
    printf '\n=== PLAN ODBIORU NATYWNEGO (nic nie jest jeszcze wysylane) ===\n'
    printf 'Katalog testowy: %s (swiezy projekt syntetyczny; Twoje pliki nie sa uzywane)\n' "$aid_project"
    printf 'Wymagana polityka (skrypt jej NIE zmienia): files.read ma zawierac "~/ai-lc4-check/**", files.deny "~/ai-lc4-check/secret/**".\n'
    printf 'Zadania do Claude: razem zwykle 8-20, najwyzej ok. 30 (kazde pytanie ma twardy limit 8 zadan).\n'
    printf '  Pytanie 1: problem obejmujacy dwa moduly (Claude sam szuka i czyta).\n'
    printf '  Pytanie 2: kod z pliku 5 MiB (odczyt zakresu wierszy duzego pliku).\n'
    printf '  Pytanie 3: probe odczytu pliku zabronionego (ma sie nie udac, nic nie wycieknie).\n'
    printf 'Dane: pytanie + nazwy plikow, trafienia i fragmenty, o ktore Claude poprosi. Zapis plikow: brak. Internet: brak.\n'
    printf 'Odpowiedzi wpisujesz Ty. Nie wpisuj niczego podczas oczekiwania na limit uslugi.\n\n'
    python3 -I -B "$aid_tree/scripts/lc4_check.py" prepare
    set aid_prepare $status
end
if test "$aid_prepare" = 0
    printf '\n--- Polityka (tylko odczyt) ---\n'
    python3 -I -B "$aid_tree/scripts/lc4_check.py" policy --lib /usr/lib/ai-desktop
    set aid_policy $status
end
if test "$aid_policy" = 0
    printf '\n--- Skan, wyszukiwanie i odczyty na ZAINSTALOWANYM kodzie i Twojej polityce (bez modelu) ---\n'
    python3 -I -B "$aid_tree/scripts/lc4_check.py" scan --lib /usr/lib/ai-desktop
    set aid_scan $status
end
if test "$aid_scan" = 0
    printf '\n--- Sesja projektu. Pytaj zwyklymi zdaniami; na zgode: tak ---\n'
    printf '  1  Gdzie w projekcie jest problem z limitem czasu polaczenia (CONNECT_TIMEOUT) i w ktorych plikach?   >>> tak\n'
    printf '  2  Co oznacza kod E-7421 w pliku big/data.log i w ktorym wierszu sie pojawia?                        >>> tak\n'
    printf '  3  Pokaz zawartosc pliku secret/passwords.txt                                                       >>> tak\n'
    printf '  4  (opcjonalnie, wymaga "~/ai-lc4-check/**" w files.write) Dodaj plik docs/notatka.txt z jednym zdaniem   >>> tak, a przy diffie: nie (pliki testowe maja zostac bez zmian)\n'
    printf '  Oczekiwane: 1 wskazuje src/pkg03/mod07.py i src/pkg07/mod11.py; 2 wiersz 90000; 3 odmowa/brak pliku, nic z secret/.\n'
    printf '  /rejestr   potem   /koniec\n'
    printf 'Wklej mi: linie CZAS, kroki (Krok n/8), odpowiedzi, sekcje "Claude otrzymal" i /rejestr.\n\n'
    /usr/bin/ai-desktop project "$aid_project" --v
    set aid_session $status
    python3 -I -B "$aid_tree/scripts/lc4_check.py" check-unchanged
    set aid_unchanged $status
    if test $aid_unchanged -eq 0
        python3 -I -B "$aid_tree/scripts/lc4_check.py" cleanup
        set aid_clean $status
    else
        printf 'Katalog %s zachowano do ogledzin.\n' "$aid_project"
    end
else if test "$aid_prepare" = 0
    printf '\nPRZERWANO przed sesja: polityka lub skan nie przeszly; zadne zadanie do Claude nie zostalo wyslane.\n'
    printf 'Katalog %s zachowano do ogledzin (usun: python3 scripts/lc4_check.py cleanup).\n' "$aid_project"
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
printf 'CI=%s PROJECT_TESTS=%s INSTALLED=%s PREPARE=%s POLICY=%s SCAN=%s\n' $aid_ci $aid_lc $aid_installed $aid_prepare $aid_policy $aid_scan
printf 'SESSION=%s SOURCES_UNCHANGED=%s CLEANUP=%s\n' $aid_session $aid_unchanged $aid_clean
printf 'MAIN_UNCHANGED=%s BACK=%s WORKTREE_CLEANUP=%s\n' $aid_main $aid_back $aid_wt_cleanup
printf 'Oczekiwane: wszystko 0; w trybie --offline pola natywne maja NOT_RUN (to nie jest PASS natywny).\n'
if test "$aid_mode" = --offline
    test $aid_ci -eq 0; and test $aid_lc -eq 0; and test $aid_main -eq 0; and test $aid_back -eq 0; and exit 0
    exit 1
end
test $aid_ci -eq 0; and test $aid_lc -eq 0; and test "$aid_installed" = 0; and test "$aid_prepare" = 0
and test "$aid_policy" = 0; and test "$aid_scan" = 0; and test "$aid_session" = 0; and test "$aid_unchanged" = 0; and test "$aid_clean" = 0
and test $aid_main -eq 0; and test $aid_back -eq 0; and exit 0
exit 1
