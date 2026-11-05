"""One approved chat completion to an already-running, owner-trusted loopback server."""
import argparse
import http.client
import json
import socket
import threading
import time

from .analysis_contract import (AnalysisDenied, SYSTEM_PROMPT, canonical, request_seconds,
                                result_packet, read_result)
from .e2_ipc import _unique_pairs
from .execution import DEFAULT_LOCAL_PORT, LOCAL_MODEL, valid_local_port
from .permissions import load_policy

HOST = "127.0.0.1"
PATH = "/v1/chat/completions"
MAX_RESPONSE = 2 * 1024 * 1024  # includes JSON, escaped text and optional reasoning


def endpoint(port=DEFAULT_LOCAL_PORT):
    if not valid_local_port(port):
        raise ValueError("local port")
    return f"http://{HOST}:{port}{PATH}"


ENDPOINT = endpoint()


def authorized_policy(port=None):
    """Run on the desktop, whose own policy is authoritative for this recipient."""
    try:
        policy = load_policy(optional=True)
        if (policy is None or not policy.provider_allowed("local")
                or (port is not None and policy.local_port != port)):
            raise AnalysisDenied("authorization")
        policy.recheck()
        return policy
    except (ValueError, OSError):
        raise AnalysisDenied("authorization") from None


def _http(body, port, policy, seconds):
    # Only these two operations exist. The model cannot supply a method, URL or arbitrary HTTP headers.
    method, path, maximum = ("GET", "/v1/models", 64 * 1024) if body is None else ("POST", PATH, MAX_RESPONSE)
    # A socket timeout alone can be kept alive forever by a peer dripping headers or chunk bytes.
    end = time.monotonic() + seconds
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        def expire():
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        stream.settimeout(seconds)
        stream.connect((HOST, port))
        connection = http.client.HTTPConnection(HOST, port)
        connection.auto_open = False
        connection.sock = stream
        timer = threading.Timer(max(0, end - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        try:
            try:
                policy.recheck()
            except (ValueError, OSError):
                raise AnalysisDenied("authorization") from None
            connection.request(method, path, body=body, headers={"Content-Type": "application/json",
                               "Accept": "application/json", "Connection": "close"})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise AnalysisDenied("rate-limit" if response.status == 429 else "provider-unavailable")
                media = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                if media != "application/json" or response.getheader("Content-Encoding", "identity") != "identity":
                    raise AnalysisDenied("result")
                if response.length is not None and response.length > maximum:
                    raise AnalysisDenied("output-limit")
                expected = response.length
                data = response.read(maximum + 1)
                if len(data) > maximum:
                    raise AnalysisDenied("output-limit")
                if expected is not None and expected != len(data):
                    raise AnalysisDenied("result")
                return data
        finally:
            timer.cancel()
            connection.close()
            if time.monotonic() >= end:
                raise AnalysisDenied("timeout")


def complete(parsed, sha256):
    """Accept only data already decoded by the canonical approval-envelope validator."""
    selected = parsed["execution"]
    port = selected.get("local_port", DEFAULT_LOCAL_PORT)
    mode = parsed["mode"]
    if selected["provider"] != "local" or mode == "web":
        raise AnalysisDenied("authorization")
    policy = authorized_policy(port)
    prompt = SYSTEM_PROMPT
    if selected["instructions"]:
        prompt += "\n\nUser profile instructions:\n" + selected["instructions"]
    data = {key: value for key, value in parsed.items() if key != "execution"}
    body = {"messages": [{"role": "system", "content": prompt}, {"role": "user", "content": canonical(data)}],
            "stream": False}
    if selected["model"] != "default":
        body["model"] = selected["model"]
    try:
        raw = _http(canonical(body).encode("utf-8"), port, policy, request_seconds(selected["effort"]))
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
        choices = document["choices"]
        if type(choices) is not list or len(choices) != 1:
            raise ValueError()
        choice = choices[0]
        message = choice["message"]
        if (choice["finish_reason"] != "stop" or choice.get("index") != 0 or message["role"] != "assistant"
                or message.get("tool_calls") or message.get("function_call") or message.get("refusal")
                or type(message["content"]) is not str or not message["content"].strip()):
            raise ValueError()
        packet = result_packet(sha256, text=message["content"], mode=mode, provider="local")
        return str(read_result(packet, sha256, mode=mode, provider="local"))
    except TimeoutError:
        raise AnalysisDenied("timeout") from None
    except (OSError, http.client.HTTPException):
        raise AnalysisDenied("network") from None
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise AnalysisDenied("result") from None


def _models():
    policy = authorized_policy()
    raw = _http(None, policy.local_port, policy, 5)
    result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    data = result["data"]
    if result.get("object") != "list" or type(data) is not list or len(data) > 128:
        raise ValueError("model list")
    names = []
    for item in data:
        if type(item) is not dict or type(item.get("id")) is not str or not LOCAL_MODEL.fullmatch(item["id"]):
            raise ValueError("model id")
        if item["id"] in names:
            raise ValueError("duplicate model")
        names.append(item["id"])
    return policy.local_port, names


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-desktop local",
        description="Explicitly read the configured loopback server's model catalog, without inference.")
    parser.add_argument("action", choices=("status", "models"), nargs="?")
    action = parser.parse_args(argv).action
    if not action:
        parser.print_help()
        return 0
    try:
        port, names = _models()
    except AnalysisDenied as exc:
        if exc.reason == "authorization":
            print("Local server access is blocked. Enable network.local in permissions.toml.")
        else:
            print("Local catalog check failed: " + exc.reason + ".")
        return 2
    except (TimeoutError, OSError, http.client.HTTPException):
        print("The local model server is unavailable.")
        return 2
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        print("Invalid local model catalog. Configure a short model alias in llama-server.")
        return 2
    if action == "status":
        print(f"Local server 127.0.0.1:{port} answered the model catalog request. Advertised models: {len(names)}.")
    if names:
        print("\n".join(names))
    elif action == "models":
        print("No models advertised.")
    return 0
