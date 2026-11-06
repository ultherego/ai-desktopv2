"""Synthetic Codex executable body; only the disposable CI harness installs its ELF launcher."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop.codex_account_protocol import DEVICE_URL, fixed_command
from ai_desktop.codex_protocol import CLIENT_VERSION, fixed_command as analysis_command
from ai_desktop.codex_runtime import BINARY, INNER_HOME, client_environment
from ai_desktop.e2_subscription_boundary import privileges_locked


def send(message):
    print(json.dumps(message), flush=True)


def main():
    assert os.geteuid() > 0 and privileges_locked(Path("/proc/self/status").read_text())
    assert Path.cwd() == Path("/work") and dict(os.environ) == client_environment()
    assert not any(Path(p).exists() for p in ("/var/lib/ai-desktop", "/run/user", "/home/runner", "/root"))
    interfaces = Path("/proc/net/dev").read_text().splitlines()[2:]
    assert {row.split(":")[0].strip() for row in interfaces} <= {"lo"}
    home = Path(INNER_HOME)
    if sys.argv[1:] == ["--version"]:
        assert not any(home.iterdir()) and not Path("/run/egress.sock").exists()
        print("codex-cli " + CLIENT_VERSION)
        return 0

    for rejected in (False, True):
        expected = fixed_command(str(BINARY)) + (["--ai-desktop-invalid-compatibility-option"] if rejected else []) + ["--help"]
        if [str(BINARY), *sys.argv[1:]] == expected:
            assert not any(home.iterdir()) and not Path("/run/egress.sock").exists()
            print("Synthetic app-server help")
            return 2 if rejected else 0

    offline = not Path("/run/egress.sock").exists()
    expected = [fixed_command(str(BINARY))]
    if offline:
        expected.append(analysis_command(str(BINARY), effort="high"))
    assert [str(BINARY), *sys.argv[1:]] in expected
    if offline:
        assert not any(home.iterdir())
    else:
        assert Path("/run/egress.sock").is_socket()
    return serve(home, offline=offline)


def serve(home, *, offline=False):
    assert not ({p.name for p in home.iterdir()} - {"auth.json"})
    credential = home / "auth.json"
    login_id = "11111111-2222-4333-8444-555555555555"
    for line in sys.stdin:
        data = json.loads(line)
        method = data["method"]
        if method == "initialized":
            continue
        if method == "initialize":
            result = {"userAgent": "synthetic"}
        elif method == "account/login/start":
            assert not offline
            assert not credential.exists() and data["params"] == {"type": "chatgptDeviceCode"}
            result = {"type": "chatgptDeviceCode", "verificationUrl": DEVICE_URL,
                      "userCode": "TEST-CODE", "loginId": login_id}
            credential.write_text(json.dumps({
                "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                "tokens": {"id_token": "synthetic-id", "access_token": "synthetic-access",
                           "refresh_token": "synthetic-refresh", "account_id": "synthetic-workspace"},
                "last_refresh": "2026-01-01T00:00:00Z",
            }))
            credential.chmod(0o600)
            send({"id": data["id"], "result": result})
            send({"method": "account/login/completed",
                  "params": {"loginId": login_id, "success": True, "error": None}})
            continue
        elif method == "account/read":
            assert data["params"] == {"refreshToken": False}
            if offline:
                assert not credential.exists()
                result = {"account": None, "requiresOpenaiAuth": True}
            else:
                assert json.loads(credential.read_text())["tokens"]["access_token"] == "synthetic-access"
                result = {"account": {"type": "chatgpt", "planType": "pro", "email": None}, "requiresOpenaiAuth": True}
        elif method == "model/list":
            assert offline and data["params"] == {"limit": 64, "includeHidden": False}
            result = {"data": [{"id": "synthetic-model", "displayName": "Synthetic", "hidden": False}], "nextCursor": None}
        elif method == "account/rateLimits/read":
            assert not offline
            assert credential.exists() and data["params"] == {"excludeResetCreditDetails": True}
            result = {"rateLimits": {"limitId": "codex", "planType": "pro",
                       "primary": {"usedPercent": 31, "windowDurationMins": 300, "resetsAt": 2000000000},
                       "secondary": {"usedPercent": 47, "windowDurationMins": 10080, "resetsAt": 2000100000}}}
        else:
            raise ValueError("unsupported fixture method")
        send({"id": data["id"], "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
