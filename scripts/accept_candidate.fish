#!/usr/bin/fish
# Explicit CachyOS acceptance only. Invoke through a SHA-pinned bootstrap.
# This script builds/installs the candidate and performs two subscription
# requests authorized together. CI checks syntax and the no-opt-in refusal.
if test (count $argv) -ne 4; or test "$argv[1]" != --allow-cachyos-acceptance
    printf 'ACCEPTANCE: REFUSED; required: --allow-cachyos-acceptance SHA REPO_ROOT CONFIGURED_PATTERN\n'
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
if test -z "$argv[4]"
    printf 'ACCEPTANCE: REFUSED configured scope required\n'
    exit 2
end
if not test (id -u) -gt 0; or not test -t 0; or not test -t 1
    printf 'ACCEPTANCE: REFUSED non-root terminal required\n'
    exit 2
end

function __aid14_step
    set -l aid_label $argv[1]
    set -e argv[1]
    $argv
    set -l aid_code $status
    printf '%s=%s\n' "$aid_label" "$aid_code"
    return "$aid_code"
end
function __aid14_logged
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
function __aid14_cycle --argument-names aid_expected aid_repo aid_tmp aid_scope
    test (id -u) -gt 0; and test -t 0; and test -t 1; or return 2
    string match -rq '^[0-9a-f]{40}$' -- "$aid_expected"; or return 2
    __aid14_step FETCH git -C "$aid_repo" fetch origin refs/heads/fix/ask-file-read-errors
    or return $status
    set -l aid_fetched (git -C "$aid_repo" rev-parse --verify FETCH_HEAD)
    or return $status
    printf 'EXPECTED_SHA=%s\nFETCHED_SHA=%s\n' "$aid_expected" "$aid_fetched"
    __aid14_step SHA_MATCH test "$aid_fetched" = "$aid_expected"
    or return $status
    set -l aid_tree "$aid_tmp/source"
    __aid14_step WORKTREE git -C "$aid_repo" worktree add --detach "$aid_tree" "$aid_expected"
    or return $status
    cd "$aid_tree"; or return $status
    __aid14_step COMMIT git log -1 --format='%H %s'
    or return $status
    __aid14_step NATIVE_COPY python3 -I -B -c '
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
    __aid14_step NATIVE_MATCH cmp /usr/lib/ai-desktop/claude-code "$aid_tree/packaging/arch/claude-code"
    or return $status
    __aid14_logged CI "$aid_tmp/ci.log" python3 -B scripts/verify.py --mode ci --fail-fast --json "$aid_tmp/ci.json"
    or return $status
    __aid14_logged LOCAL "$aid_tmp/local.log" python3 -B scripts/verify.py --mode local --fail-fast --json "$aid_tmp/local.json"
    or return $status
    __aid14_step SOURCE_DIFF git diff --exit-code "$aid_expected" --
    or return $status
    set -l aid_dirty (git status --porcelain --untracked-files=normal)
    or return $status
    __aid14_step SOURCE_CLEAN test -z "$aid_dirty"
    or return $status
    cd "$aid_tree/packaging/arch"; or return $status
    __aid14_logged BUILD "$aid_tmp/build.log" makepkg --cleanbuild --clean --noconfirm
    or return $status
    set -l aid_packages (makepkg --packagelist)
    or return $status
    __aid14_step ONE_PACKAGE test (count $aid_packages) -eq 1
    or return $status
    set -l aid_package "$aid_packages[1]"
    test -f "$aid_package"; and not test -L "$aid_package"; or return 2
    set -l aid_package_info (/usr/bin/pacman -Qp "$aid_package")
    or return $status
    __aid14_step PACKAGE_VERSION test "$aid_package_info" = 'ai-desktop 0.1.0-14'
    or return $status
    bsdtar -xOf "$aid_package" .INSTALL > "$aid_tmp/package.install"
    or return $status
    __aid14_step PACKAGE_HOOK cmp "$aid_tree/packaging/arch/ai-desktop.install" "$aid_tmp/package.install"
    or return $status
    __aid14_step BUILT_SOURCE git -C "$aid_tree" diff --exit-code "$aid_expected" --
    or return $status
    __aid14_step UPGRADE sudo /usr/bin/pacman -U "$aid_package"
    or return $status
    __aid14_step RELOAD sudo /usr/bin/systemctl daemon-reload
    or return $status
    cd "$aid_tree"; or return $status
    __aid14_step INSTALLED python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv[1] + "/tests"); import host_analysis_acceptance as check; check.installed_matches(); print("INSTALLED MATCH: PASS")' "$aid_tree"
    or return $status
    # No tee: the real local read and the conversation need fresh TTY consents.
    __aid14_step HOST_FILE_LOCAL python3 -B tests/host_file_access_acceptance.py --allow-configured-file-read "$aid_scope"
    or return $status
    printf '\nNew acceptance: 2 Claude requests: selection and answer about five synthetic files (12175 B).\n'
    printf 'One consent approves the whole synthetic question. Real files stay local.\n'
    __aid14_step HOST_FILE python3 -B tests/host_file_access_acceptance.py --allow-subscription-file-test
    or return $status
    return 0
end

set -l aid_expected "$argv[2]"
set -l aid_scope "$argv[4]"
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
set -l aid_tmp (mktemp -d /tmp/ai-desktop-permissions-XXXXXXXX)
if test $status -ne 0; or test -z "$aid_tmp"
    printf 'SETUP=FAIL CYCLE=2 RETURN=0\n'
    exit 2
end
__aid14_cycle "$aid_expected" "$aid_repo" "$aid_tmp" "$aid_scope"
set -l aid_cycle $status
cd "$aid_repo"
set -l aid_return $status
if test "$aid_return" -eq 0
    set -l aid_after (git rev-parse --verify HEAD)
    __aid14_step MAIN_UNCHANGED test "$aid_after" = "$aid_before"
    set -l aid_main_check $status
    if test "$aid_cycle" -eq 0; and test "$aid_main_check" -ne 0
        set aid_cycle "$aid_main_check"
    end
end
printf '\nCYCLE=%s RETURN=%s\n' "$aid_cycle" "$aid_return"
printf 'Logs and worktree kept: %s\n' "$aid_tmp"
printf 'Neither the local read content nor the HOST FILE conversation was saved; Claude mode uses only synthetic data.\n'
if test "$aid_cycle" -ne 0
    exit "$aid_cycle"
end
exit "$aid_return"
