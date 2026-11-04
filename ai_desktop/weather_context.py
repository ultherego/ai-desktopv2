"""Explicit, bounded Open-Meteo context; never a general URL fetcher.

The UI obtains consent before calling geocode/current and separately before
transferring their result to Claude. No request happens on import. This module
does not grant the model a browser or modify its egress policy. Each HTTP read
runs in a short-lived process so even a stuck system resolver has a deadline.
"""
from datetime import datetime, timezone
import http.client
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
import ssl
import sys
import unicodedata
from urllib.parse import urlencode


HOSTS = frozenset({"geocoding-api.open-meteo.com", "api.open-meteo.com"})
SOURCE_URL = "https://open-meteo.com/en/docs"
MAX_BODY = 32768
MAX_CANDIDATES = 5
REQUEST_SECONDS = 20
REASONS = frozenset({"input", "network", "timeout", "unavailable", "rate-limit",
                     "response", "not-found", "stale"})
WEATHER_CODES = frozenset({0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65,
                           66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99})
VARIABLES = ("temperature_2m", "apparent_temperature", "precipitation",
             "weather_code", "wind_speed_10m")
UNITS = {"time": "iso8601", "interval": "seconds", "temperature_2m": "°C",
         "apparent_temperature": "°C", "precipitation": "mm",
         "weather_code": "wmo code", "wind_speed_10m": "km/h"}


class WeatherError(ValueError):
    """Only finite reason codes, never an upstream response or exception."""
    def __init__(self, reason="response"):
        self.reason = reason if type(reason) is str and reason in REASONS else "response"
        super().__init__("WEATHER: REFUSED reason=" + self.reason)


def _text(value, *, empty=False, limit=120):
    try:
        valid = (type(value) is str and (empty or bool(value.strip()))
                 and len(value.encode("utf-8")) <= limit
                 and not any(unicodedata.category(c).startswith("C") or c in "\n\r\t" for c in value))
    except UnicodeError:
        valid = False
    if not valid:
        raise WeatherError("input")
    return value


def validate_city(city):
    city = _text(city).strip()
    if len(city) < 2:
        raise WeatherError("input")
    return city


def _number(value, lower, upper):
    if type(value) not in (int, float) or not lower <= value <= upper or not math.isfinite(value):
        raise WeatherError("response")
    return value


def _location(value):
    if type(value) is not dict or set(value) != {
            "id", "name", "country", "admin1", "latitude", "longitude"}:
        raise WeatherError("response")
    if type(value["id"]) is not int or not 0 < value["id"] < 2**63:
        raise WeatherError("response")
    return {"id": value["id"], "name": _text(value["name"]),
            "country": _text(value["country"], empty=True),
            "admin1": _text(value["admin1"], empty=True),
            "latitude": _number(value["latitude"], -90, 90),
            "longitude": _number(value["longitude"], -180, 180)}


def _timestamp(value):
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise WeatherError("response")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise WeatherError("response") from None


def validate_context(value):
    """Pure strict schema used again by the application/IPC consent boundary."""
    if type(value) is not dict or set(value) != {
            "kind", "source", "source_url", "observed_at", "valid_at", "basis", "location", "current"}:
        raise WeatherError("response")
    if (value["kind"] != "weather" or value["source"] != "Open-Meteo"
            or value["source_url"] != SOURCE_URL or value["basis"] != "model"):
        raise WeatherError("response")
    observed, valid = _timestamp(value["observed_at"]), _timestamp(value["valid_at"])
    if not -900 <= (observed - valid).total_seconds() <= 7200:
        raise WeatherError("stale")
    current = value["current"]
    if type(current) is not dict or set(current) != {
            "temperature_c", "apparent_temperature_c", "precipitation_mm",
            "weather_code", "wind_kmh", "interval_seconds"}:
        raise WeatherError("response")
    _number(current["temperature_c"], -100, 80)
    _number(current["apparent_temperature_c"], -130, 100)
    _number(current["precipitation_mm"], 0, 1000)
    _number(current["wind_kmh"], 0, 500)
    if (type(current["weather_code"]) is not int or current["weather_code"] not in WEATHER_CODES
            or type(current["interval_seconds"]) is not int
            or not 0 < current["interval_seconds"] <= 3600):
        raise WeatherError("response")
    return {**value, "location": _location(value["location"]), "current": dict(current)}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WeatherError("response")
        result[key] = value
    return result


def _json(raw):
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_BODY:
            raise WeatherError("response")
        result = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)
        if type(result) is not dict:
            raise WeatherError("response")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise WeatherError("response") from None


def _invalid_constant(_):
    raise WeatherError("response")


def _addresses(host):
    if host not in HOSTS:
        raise WeatherError("input")
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        if not answers or len(answers) > 32:
            raise ValueError()
        for family, kind, protocol, _, address in answers:
            if (family not in (socket.AF_INET, socket.AF_INET6)
                    or kind != socket.SOCK_STREAM or protocol != socket.IPPROTO_TCP
                    or len(address) != (2 if family == socket.AF_INET else 4) or address[1] != 443):
                raise ValueError()
            ip = ipaddress.ip_address(address[0])
            if (ip.version != (4 if family == socket.AF_INET else 6) or not ip.is_global
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                raise ValueError()
            if family == socket.AF_INET6 and (address[2:] != (0, 0) or ip.ipv4_mapped
                    or ip.sixtofour or ip.teredo or ip in ipaddress.ip_network("64:ff9b::/96")):
                raise ValueError()
        return answers
    except (OSError, ValueError, TypeError, IndexError):
        raise WeatherError("network") from None


class _PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        # Validate all DNS answers once and connect only to a numeric sockaddr;
        # TLS still verifies the approved DNS hostname with the system CA store.
        family, kind, protocol, _, address = _addresses(self.host)[0]
        stream = socket.socket(family, kind, protocol)
        try:
            stream.settimeout(5)
            stream.connect(address)
            self.sock = self._context.wrap_socket(stream, server_hostname=self.host)
        except BaseException:
            stream.close()
            raise


def _endpoint(kind, parameters):
    if kind == "geocode" and type(parameters) is dict and set(parameters) == {"city"}:
        city = validate_city(parameters["city"])
        return "geocoding-api.open-meteo.com", "/v1/search?" + urlencode({
            "name": city, "count": MAX_CANDIDATES, "language": "pl", "format": "json"})
    if kind == "current" and type(parameters) is dict and set(parameters) == {"latitude", "longitude"}:
        return "api.open-meteo.com", "/v1/forecast?" + urlencode({
            "latitude": _number(parameters["latitude"], -90, 90),
            "longitude": _number(parameters["longitude"], -180, 180),
            "current": ",".join(VARIABLES), "timezone": "GMT", "timeformat": "iso8601",
            "temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm"})
    raise WeatherError("input")


def _https_json(kind, parameters):
    host, path = _endpoint(kind, parameters)
    connection = _PinnedHTTPS(host, timeout=5, context=ssl.create_default_context())
    try:
        connection.request("GET", path, headers={"Accept": "application/json",
                           "Accept-Encoding": "identity", "Connection": "close",
                           "User-Agent": "AI-Desktop/0.1 weather-context"})
        response = connection.getresponse()
        if response.status != 200:
            raise WeatherError("rate-limit" if response.status == 429 else "unavailable")
        # No redirects, decompression, cookies, proxy or authorization support.
        headers = response.getheaders()
        if sum(len(k) + len(v) for k, v in headers) > 8192:
            raise WeatherError("response")
        names = [key.lower() for key, _ in headers]
        for name in ("content-length", "content-type", "content-encoding", "transfer-encoding"):
            if names.count(name) > 1:
                raise WeatherError("response")
        if (response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json"
                or response.getheader("Content-Encoding", "identity").lower() != "identity"):
            raise WeatherError("response")
        length = response.getheader("Content-Length")
        transfer = response.getheader("Transfer-Encoding")
        if (length is not None and (not length.isascii() or not length.isdigit()
                                   or not 0 < int(length) <= MAX_BODY)):
            raise WeatherError("response")
        if transfer is not None and (transfer.lower() != "chunked" or length is not None):
            raise WeatherError("response")
        return _json(response.read(MAX_BODY + 1))
    except (OSError, http.client.HTTPException):
        raise WeatherError("network") from None
    finally:
        connection.close()


def _request(kind, parameters):
    """Kill the fixed worker after 20s, including DNS/TLS/headers/body reads."""
    _endpoint(kind, parameters)  # Validate before even creating the worker.
    from .analysis_contract import AnalysisDenied
    from .process_io import bounded_process
    bootstrap = ("import sys;sys.path.insert(0," + repr(str(Path(__file__).resolve().parents[1]))
                 + ");from ai_desktop.weather_context import _worker;raise SystemExit(_worker())")
    try:
        code, output, _ = bounded_process(
            [sys.executable, "-I", "-B", "-c", bootstrap],
            env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
            input_bytes=json.dumps({"kind": kind, "parameters": parameters}, allow_nan=False).encode(),
            timeout=REQUEST_SECONDS, limit=MAX_BODY)
        value = _json(output)
        if code != 0:
            raise WeatherError(value.get("error"))
        return value
    except AnalysisDenied as exc:
        raise WeatherError("timeout" if exc.reason == "timeout" else "network") from None
    except OSError:
        raise WeatherError("network") from None


def _worker():
    try:
        value = _json(sys.stdin.buffer.read(1025))
        if set(value) != {"kind", "parameters"}:
            raise WeatherError("input")
        result = _https_json(value["kind"], value["parameters"])
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > MAX_BODY:
            raise WeatherError("response")
        sys.stdout.buffer.write(encoded)
        return 0
    except (WeatherError, ValueError, TypeError, KeyError, RecursionError) as exc:
        reason = exc.reason if isinstance(exc, WeatherError) else "response"
        print(json.dumps({"error": reason}))
        return 2


def geocode(city):
    """Return at most five candidates; the UI must resolve multiple matches."""
    result = _request("geocode", {"city": validate_city(city)})
    rows = result.get("results", [])
    if type(rows) is not list or len(rows) > MAX_CANDIDATES or result.get("error"):
        raise WeatherError("response")
    candidates = []
    try:
        for row in rows:
            if type(row) is not dict:
                raise WeatherError("response")
            candidates.append(_location({"id": row["id"], "name": row["name"],
                "country": row.get("country", ""), "admin1": row.get("admin1", ""),
                "latitude": row["latitude"], "longitude": row["longitude"]}))
    except KeyError:
        raise WeatherError("response") from None
    if len({item["id"] for item in candidates}) != len(candidates):
        raise WeatherError("response")
    if not candidates:
        raise WeatherError("not-found")
    return candidates


def current(candidate):
    """Current model estimate for the explicitly selected place, in UTC."""
    location = _location(candidate)
    result = _request("current", {key: location[key] for key in ("latitude", "longitude")})
    try:
        if (result.get("error") or type(result.get("utc_offset_seconds")) is not int
                or result["utc_offset_seconds"] != 0
                or result.get("current_units") != UNITS):
            raise WeatherError("response")
        latitude = _number(result["latitude"], -90, 90)
        longitude = _number(result["longitude"], -180, 180)
        longitude_distance = abs(longitude - location["longitude"])
        if (abs(latitude - location["latitude"]) > 1.5
                or min(longitude_distance, 360 - longitude_distance) > 1.5):
            raise WeatherError("response")
        values = result["current"]
        valid = values["time"]
        if type(valid) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", valid):
            raise WeatherError("response")
        return validate_context({"kind": "weather", "source": "Open-Meteo", "source_url": SOURCE_URL,
            "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "valid_at": valid + ":00Z", "basis": "model", "location": location,
            "current": {"temperature_c": values["temperature_2m"],
                "apparent_temperature_c": values["apparent_temperature"],
                "precipitation_mm": values["precipitation"], "weather_code": values["weather_code"],
                "wind_kmh": values["wind_speed_10m"], "interval_seconds": values["interval"]}})
    except (KeyError, TypeError):
        raise WeatherError("response") from None
