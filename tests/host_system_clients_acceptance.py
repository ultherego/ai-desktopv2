"""Explicit offline CachyOS/Arch system-client acceptance; no OAuth or inference."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_desktop import client_compat


def require_host():
    if os.getuid() <= 0 or os.getuid() != os.geteuid() or os.getgid() != os.getegid() or sys.platform != "linux":
        raise ValueError("identity")
    values = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip('"')
    if values.get("ID") not in ("arch", "cachyos") and "arch" not in values.get("ID_LIKE", "").split():
        raise ValueError("platform")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if (len(args) not in (2, 3) or args[0] != "--allow-offline-client-check"
            or args[1] not in ("claude", "codex", "both", "local")
            or len(args) == 3 and args[2] != "--installed"):
        print("SYSTEM CLIENT ACCEPTANCE: REFUSED explicit offline mode and provider required")
        return 2
    try:
        require_host()
        if len(args) == 3:
            import host_analysis_acceptance
            host_analysis_acceptance.installed_matches(native=False)
        selected = ("claude", "codex") if args[1] == "both" else () if args[1] == "local" else (args[1],)
        # Invalid selectors must fail before constructing a native command.
        try:
            client_compat.inspect("invalid-provider")
        except ValueError:
            pass
        else:
            raise ValueError("selector-refusal")
        passed = True
        for provider in selected:
            result = client_compat.inspect(provider)
            # Only the shared closed diagnostic vocabulary crosses this output.
            print(json.dumps(result, sort_keys=True, ensure_ascii=True))
            if result["status"] not in ("compatible-tested", "compatible-untested"):
                passed = False
        print("SYSTEM CLIENT ACCEPTANCE: " + ("PASS" if passed else "FAIL")
              + " provider=" + args[1] + " selector-refusal=PASS network=OFF profile=NONE inference=NOT_RUN")
        return 0 if passed else 2
    except (OSError, ValueError):
        print("SYSTEM CLIENT ACCEPTANCE: FAIL reason=preflight")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
