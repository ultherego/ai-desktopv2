#!/usr/bin/fish
# Maintained candidate runner: one worktree; no commits or documentation writes.
# Legacy web opt-in remains supported. Native requests still need terminal consent.
if test (count $argv) -eq 1; and test "$argv[1]" = --help
    printf 'Usage: fish scripts/accept_web_candidate.fish MODE SHA REPO_ROOT [BRANCH]\n'
    printf '  --local-only                 CI + LOCAL; no installation or Claude\n'
    printf '  --allow-install              CI + LOCAL + package installation; no Claude request\n'
    printf '  --allow-codex-install        also fetch pinned official Codex (SHA-256 verified); no OAuth or AI request\n'
    printf '  --allow-cachyos-acceptance    installation + separately confirmed native WebSearch\n'
    printf '  --part-profile-acceptance     installation + bounded native 240/512 KiB benchmark (4 runs, hard request caps)\n'
    printf 'Default branch: feature/ask-web-search. SHA must match its fetched HEAD.\n'
    exit 0
end
if test (count $argv) -lt 3; or test (count $argv) -gt 4; or not contains -- "$argv[1]" --local-only --allow-install --allow-codex-install --allow-cachyos-acceptance --part-profile-acceptance
    printf 'ACCEPTANCE: REFUSED; required: MODE SHA REPO_ROOT [BRANCH]\n'
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
set -l aid_mode "$argv[1]"
set -l aid_branch feature/ask-web-search
if test (count $argv) -eq 4
    set aid_branch "$argv[4]"
end
if not string match -rq '^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$' -- "$aid_branch"; or string match -rq '(^|/)\.|\.\.|/$|//' -- "$aid_branch"
    printf 'ACCEPTANCE: REFUSED invalid branch\n'
    exit 2
end
if not test (id -u) -gt 0; or not test -t 0; or not test -t 1
    printf 'ACCEPTANCE: REFUSED non-root terminal required\n'
    exit 2
end

set -g aid18_results
set -g aid18_web NOT_RUN
set -g aid18_install NOT_RUN

function __aid18_step
    set -l aid_label $argv[1]
    set -e argv[1]
    $argv
    set -l aid_code $status
    set -ga aid18_results "$aid_label=$aid_code"
    if test "$aid_label" = HOST_WEB
        set -g aid18_web "$aid_code"
    else if test "$aid_label" = UPGRADE
        set -g aid18_install "$aid_code"
    end
    printf '%s=%s\n' "$aid_label" "$aid_code"
    return "$aid_code"
end
function __aid18_logged
    set -l aid_label $argv[1]
    set -l aid_log $argv[2]
    set -e argv[1..2]
    $argv 2>&1 | tee "$aid_log"
    set -l aid_codes $pipestatus
    set -ga aid18_results "$aid_label=$aid_codes[1]" "$aid_label"_LOG"=$aid_codes[2]"
    printf '%s=%s LOG=%s\n' "$aid_label" "$aid_codes[1]" "$aid_codes[2]"
    if test "$aid_codes[1]" -ne 0
        return "$aid_codes[1]"
    end
    return "$aid_codes[2]"
end
function __aid18_part_profile --argument-names aid_tree
    # Bounded benchmark of the exact candidate. Every consent stays in the terminal with the user: nothing is piped,
    # typed or answered here. The user's permissions.toml is only READ (check-config); a mismatch stops the run.
    cd "$aid_tree"; or return $status
    __aid18_step PART_PLAN python3 -B scripts/part_benchmark.py plan --limit 45
    or return $status
    set -l aid_data (mktemp -d "$HOME/ai-part-bench-XXXXXXXX")
    or return $status
    printf 'Test data (fresh directory): %s\n' "$aid_data"
    __aid18_step PART_DATA python3 -B scripts/part_benchmark.py write "$aid_data"
    or return $status
    for aid_kib in 240 512
        set -l aid_prompt (printf 'Set in YOUR permissions.toml: part_size = "%sKiB" (this script does not change it), then press Enter: ' $aid_kib)
        read -P "$aid_prompt" aid_ack
        or return 2
        __aid18_step CONFIG_$aid_kib python3 -B scripts/part_benchmark.py check-config $aid_kib
        or return $status
        for aid_label in (python3 -B scripts/part_benchmark.py labels $aid_kib)
            __aid18_step RUN_$aid_label python3 -B scripts/part_benchmark.py execute $aid_label "$aid_data"
            or return $status
        end
    end
    __aid18_step PART_REPORT python3 -B scripts/part_benchmark.py report "$aid_data"
    or return $status
    printf 'Results and meta-logs kept in %s\n' "$aid_data"
    return 0
end
function __aid18_cycle --argument-names aid_expected aid_repo aid_tmp aid_script aid_mode aid_branch
    test (id -u) -gt 0; and test -t 0; and test -t 1; or return 2
    string match -rq '^[0-9a-f]{40}$' -- "$aid_expected"; or return 2
    __aid18_step FETCH git -C "$aid_repo" fetch origin "refs/heads/$aid_branch"
    or return $status
    set -l aid_fetched (git -C "$aid_repo" rev-parse --verify FETCH_HEAD)
    or return $status
    printf 'EXPECTED_SHA=%s\nFETCHED_SHA=%s\n' "$aid_expected" "$aid_fetched"
    __aid18_step SHA_MATCH test "$aid_fetched" = "$aid_expected"
    or return $status
    set -l aid_tree "$aid_tmp/source"
    __aid18_step WORKTREE git -C "$aid_repo" worktree add --detach "$aid_tree" "$aid_expected"
    or return $status
    __aid18_step SCRIPT_MATCH cmp -- "$aid_script" "$aid_tree/scripts/accept_web_candidate.fish"
    or return $status
    cd "$aid_tree"; or return $status
    __aid18_step COMMIT git log -1 --format='%H %s'
    or return $status
    __aid18_logged CI "$aid_tmp/ci.log" python3 -B scripts/verify.py --mode ci --fail-fast --json "$aid_tmp/ci.json"
    or return $status
    __aid18_logged LOCAL "$aid_tmp/local.log" python3 -B scripts/verify.py --mode local --fail-fast --json "$aid_tmp/local.json"
    or return $status
    __aid18_step SOURCE_DIFF git diff --exit-code "$aid_expected" --
    or return $status
    set -l aid_dirty (git status --porcelain --untracked-files=normal)
    or return $status
    __aid18_step SOURCE_CLEAN test -z "$aid_dirty"
    or return $status
    if test "$aid_mode" = --local-only
        return 0
    end
    __aid18_step NATIVE_COPY python3 -I -B -c '
import os, shutil, stat, sys
from pathlib import Path
source, target = Path("/usr/lib/ai-desktop/claude-code"), Path(sys.argv[1])
for path in reversed(source.parents):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit("NATIVE COPY: REFUSED parent")
with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC), "rb") as src:
    before = os.fstat(src.fileno())
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_gid != 0
            or before.st_nlink != 1 or before.st_mode & 0o022 or not before.st_mode & 0o111
            or src.read(4) != b"\x7fELF"):
        raise SystemExit("NATIVE COPY: REFUSED source")
    src.seek(0)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    with os.fdopen(fd, "wb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fchmod(dst.fileno(), 0o755)
        os.fsync(dst.fileno())
    after = os.fstat(src.fileno())
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in fields):
        raise SystemExit("NATIVE COPY: REFUSED changed source")
print("Native client copied from the accepted package; not run, no OAuth.")
' "$aid_tree/packaging/arch/claude-code"
    or return $status
    __aid18_step NATIVE_MATCH cmp /usr/lib/ai-desktop/claude-code "$aid_tree/packaging/arch/claude-code"
    or return $status
    if test "$aid_mode" = --allow-codex-install
        cd "$aid_tree"; or return $status
        __aid18_step CODEX_ASSET python3 -B scripts/fetch_codex_client.py
        or return $status
    end
    cd "$aid_tree/packaging/arch"; or return $status
    __aid18_logged BUILD "$aid_tmp/build.log" makepkg --cleanbuild --clean --noconfirm
    or return $status
    set -l aid_packages (makepkg --packagelist)
    or return $status
    __aid18_step ONE_PACKAGE test (count $aid_packages) -eq 1
    or return $status
    set -l aid_package "$aid_packages[1]"
    test -f "$aid_package"; and not test -L "$aid_package"; or return 2
    set -l aid_package_info (/usr/bin/pacman -Qp "$aid_package")
    or return $status
    __aid18_step PACKAGE_VERSION test "$aid_package_info" = 'ai-desktop 0.1.0-80'
    or return $status
    bsdtar -xOf "$aid_package" .INSTALL > "$aid_tmp/package.install"
    or return $status
    __aid18_step PACKAGE_HOOK cmp "$aid_tree/packaging/arch/ai-desktop.install" "$aid_tmp/package.install"
    or return $status
    __aid18_step BUILT_SOURCE git -C "$aid_tree" diff --exit-code "$aid_expected" --
    or return $status
    __aid18_step UPGRADE sudo /usr/bin/pacman -U "$aid_package"
    or return $status
    __aid18_step RELOAD sudo /usr/bin/systemctl daemon-reload
    or return $status
    cd "$aid_tree"; or return $status
    __aid18_step INSTALLED python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv[1] + "/tests"); import host_analysis_acceptance as check; check.installed_matches(); print("INSTALLED MATCH: PASS")' "$aid_tree"
    or return $status
    if contains -- "$aid_mode" --allow-install --allow-codex-install
        return 0
    end
    if test "$aid_mode" = --part-profile-acceptance
        __aid18_part_profile "$aid_tree"
        return $status
    end
    # Keep native answers and human consent on the original TTY, without tee.
    printf "\nNew acceptance: one question about Python, two Claude requests with WebSearch; no local data.\n"
    __aid18_step HOST_WEB python3 -B tests/host_web_acceptance.py --allow-subscription-web-test
    or return $status
    return 0
end

set -l aid_script (realpath -- (status filename))
or exit 2
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
set -l aid_tmp (mktemp -d /tmp/ai-desktop-web-XXXXXXXX)
if test $status -ne 0; or test -z "$aid_tmp"
    printf 'SETUP=FAIL CYCLE=2 RETURN=0\n'
    exit 2
end
__aid18_cycle "$aid_expected" "$aid_repo" "$aid_tmp" "$aid_script" "$aid_mode" "$aid_branch"
set -l aid_cycle $status
cd "$aid_repo"
set -l aid_return $status
if test "$aid_return" -eq 0
    set -l aid_after (git rev-parse --verify HEAD)
    __aid18_step MAIN_UNCHANGED test "$aid_after" = "$aid_before"
    set -l aid_main_check $status
    if test "$aid_cycle" -eq 0; and test "$aid_main_check" -ne 0
        set aid_cycle "$aid_main_check"
    end
end
printf '\n=== RESULTS ===\n'
printf '%s\n' $aid18_results
printf 'MODE=%s UPGRADE=%s HOST_WEB=%s\n' "$aid_mode" "$aid18_install" "$aid18_web"
printf '\nCYCLE=%s RETURN=%s\n' "$aid_cycle" "$aid_return"
printf 'Logs and worktree kept: %s\n' "$aid_tmp"
printf 'The HOST WEB conversation was not saved; the real permissions.toml stays unchanged.\n'
printf 'The tests do not change documentation and do not commit, push or merge.\n'
if test "$aid_cycle" -ne 0
    exit "$aid_cycle"
end
exit "$aid_return"
