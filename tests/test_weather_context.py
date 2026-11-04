"""Weather boundaries with synthetic DNS, TLS, HTTP and provider data only."""
import copy
from datetime import datetime, timezone
import io
import json
import socket
import unittest
from unittest.mock import Mock, patch

from ai_desktop import weather_context as weather
from ai_desktop.analysis_contract import AnalysisDenied


LOCATION = {"id": 756135, "name": "Warszawa", "country": "Polska", "admin1": "Mazowieckie",
            "latitude": 52.22977, "longitude": 21.01178}
CONTEXT = {"kind": "weather", "source": "Open-Meteo", "source_url": weather.SOURCE_URL,
           "observed_at": "2026-10-30T12:12:00Z", "valid_at": "2026-10-30T12:00:00Z",
           "basis": "model", "location": LOCATION,
           "current": {"temperature_c": 7.2, "apparent_temperature_c": 5.5,
                       "precipitation_mm": 0, "weather_code": 3, "wind_kmh": 11.8,
                       "interval_seconds": 900}}
PROVIDER = {"latitude": 52.23, "longitude": 21.01, "utc_offset_seconds": 0,
            "current_units": weather.UNITS,
            "current": {"time": "2026-10-30T12:00", "interval": 900, "temperature_2m": 7.2,
                        "apparent_temperature": 5.5, "precipitation": 0,
                        "weather_code": 3, "wind_speed_10m": 11.8}}


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 30, 12, 12, tzinfo=timezone.utc)


def dns(ip="8.8.8.8"):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))]


class WeatherTests(unittest.TestCase):
    def test_city_validation_and_request_parameters_before_any_worker(self):
        with patch("ai_desktop.process_io.bounded_process") as process:
            for city in ("", "a", "x" * 121, "Warszawa\nHost: evil", "\x1b", "\u202eWarszawa", "\ud800", [], None):
                with self.subTest(city=repr(city)), self.assertRaises(weather.WeatherError):
                    weather.geocode(city)
            for kind, params in (("http://localhost", {}), ("current", {"latitude": 10, "longitude": 20, "url": "x"}),
                                 ("current", {"latitude": True, "longitude": 20}),
                                 ("geocode", {"city": "Warszawa", "headers": {}})):
                with self.assertRaises(weather.WeatherError):
                    weather._request(kind, params)
        process.assert_not_called()

    def test_geocoding_returns_bounded_candidates_without_silently_choosing(self):
        second = dict(LOCATION, id=2, country="USA", latitude=38.2, longitude=-93.4)
        upstream = {"results": [dict(LOCATION, postcodes=["PRIVATE"], elevation=120), second]}
        with patch.object(weather, "_request", return_value=upstream) as request:
            result = weather.geocode(" Warszawa ")
        self.assertEqual(result, [LOCATION, second])
        request.assert_called_once_with("geocode", {"city": "Warszawa"})
        self.assertNotIn("PRIVATE", json.dumps(result))
        for value, reason in (({}, "not-found"), ({"results": "wrong"}, "response"),
                              ({"results": [LOCATION] * 6}, "response"),
                              ({"results": [LOCATION, LOCATION]}, "response"),
                              ({"error": True, "reason": "PRIVATE"}, "response")):
            with patch.object(weather, "_request", return_value=value), self.assertRaises(weather.WeatherError) as error:
                weather.geocode("Warszawa")
            self.assertEqual(error.exception.reason, reason)
            self.assertNotIn("PRIVATE", str(error.exception))

    def test_location_names_controls_coordinates_and_shape_rejected(self):
        for field, value in (("name", "x\x1b[2J"), ("latitude", float("nan")),
                             ("longitude", 181), ("id", True), ("country", "x" * 121)):
            with patch.object(weather, "_request", return_value={"results": [dict(LOCATION, **{field: value})]}):
                with self.subTest(field=field), self.assertRaises(weather.WeatherError):
                    weather.geocode("Warszawa")
        with patch.object(weather, "_request") as request, self.assertRaises(weather.WeatherError):
            weather.current(dict(LOCATION, url="https://localhost"))
        request.assert_not_called()

    def test_current_validates_units_time_and_coordinates_then_minimizes(self):
        with patch.object(weather, "datetime", Clock), patch.object(weather, "_request", return_value=PROVIDER) as request:
            result = weather.current(LOCATION)
        self.assertEqual(result, CONTEXT)
        request.assert_called_once_with("current", {"latitude": LOCATION["latitude"], "longitude": LOCATION["longitude"]})
        self.assertEqual(result["basis"], "model")
        self.assertEqual(result["source_url"], "https://open-meteo.com/en/docs")

    def test_current_refuses_missing_wrong_units_wrong_place_and_stale_data(self):
        mutations = [dict(PROVIDER, latitude=0), dict(PROVIDER, longitude=-93),
                     dict(PROVIDER, utc_offset_seconds=3600), dict(PROVIDER, utc_offset_seconds=False),
                     dict(PROVIDER, current_units=dict(weather.UNITS, temperature_2m="°F")),
                     dict(PROVIDER, current={}), dict(PROVIDER, current=[]),
                     dict(PROVIDER, current=dict(PROVIDER["current"], temperature_2m=None)),
                     dict(PROVIDER, current=dict(PROVIDER["current"], time="2026-10-29T12:00")),
                     dict(PROVIDER, current=dict(PROVIDER["current"], time="2026-10-30T13:00"))]
        for value in mutations:
            with patch.object(weather, "datetime", Clock), patch.object(weather, "_request", return_value=value):
                with self.subTest(value=value), self.assertRaises(weather.WeatherError):
                    weather.current(LOCATION)

    def test_strict_context_validation_no_io_and_no_alias_to_input(self):
        with patch.object(weather, "_request") as request:
            result = weather.validate_context(CONTEXT)
        request.assert_not_called()
        self.assertEqual(result, CONTEXT)
        result["current"]["temperature_c"] = 99
        self.assertEqual(CONTEXT["current"]["temperature_c"], 7.2)
        cases = [dict(CONTEXT, source_url="https://localhost"), dict(CONTEXT, command="curl anything"),
                 dict(CONTEXT, valid_at="2026-10-99T12:00:00Z"), dict(CONTEXT, basis="observed"),
                 dict(CONTEXT, current=dict(CONTEXT["current"], temperature_c=10**1000)),
                 dict(CONTEXT, current=dict(CONTEXT["current"], wind_kmh=float("inf"))),
                 dict(CONTEXT, current=dict(CONTEXT["current"], weather_code=True))]
        for value in cases:
            with self.assertRaises(weather.WeatherError):
                weather.validate_context(value)

    def test_json_limits_duplicates_constants_and_wrong_root(self):
        for raw in (b"", b"x" * (weather.MAX_BODY + 1), b'[]', b'{"x":1,"x":2}',
                    b'{"x":NaN}', b'{"x":Infinity}', b'\xff', b'[' * 1000):
            with self.subTest(raw=raw[:20]), self.assertRaises(weather.WeatherError):
                weather._json(raw)

    def test_endpoint_city_is_encoded_and_no_user_can_supply_an_authority(self):
        host, path = weather._endpoint("geocode", {"city": "Warszawa &latitude=0"})
        self.assertEqual(host, "geocoding-api.open-meteo.com")
        self.assertIn("name=Warszawa+%26latitude%3D0", path)
        self.assertIn("count=5", path)
        host, path = weather._endpoint("current", {"latitude": 52.2, "longitude": 21})
        self.assertEqual(host, "api.open-meteo.com")
        self.assertIn("temperature_unit=celsius", path)
        self.assertIn("timezone=GMT", path)
        self.assertNotIn("hourly", path)

    def test_dns_all_answers_must_be_public_without_mapped_or_scoped_ipv6(self):
        for answers in ([], dns("127.0.0.1"), dns("10.1.1.1"), dns() + dns("169.254.169.254"),
                        dns("224.0.0.1"), dns("100.64.0.1"),
                        [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::ffff:8.8.8.8", 443, 0, 0))],
                        [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700:4700::1111", 443, 0, 2))]):
            with patch.object(weather.socket, "getaddrinfo", return_value=answers), self.assertRaises(weather.WeatherError):
                weather._addresses("api.open-meteo.com")
        with patch.object(weather.socket, "getaddrinfo", return_value=dns()) as resolver:
            self.assertEqual(weather._addresses("api.open-meteo.com"), dns())
            with self.assertRaises(weather.WeatherError):
                weather._addresses("localhost")
        resolver.assert_called_once_with("api.open-meteo.com", 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)

    def test_tls_pins_numeric_destination_while_verifying_approved_hostname(self):
        context, stream = Mock(), Mock()
        with patch.object(weather, "_addresses", return_value=dns()), patch.object(weather.socket, "socket", return_value=stream):
            connection = weather._PinnedHTTPS("api.open-meteo.com", context=context)
            connection.connect()
        stream.connect.assert_called_once_with(("8.8.8.8", 443))
        context.wrap_socket.assert_called_once_with(stream, server_hostname="api.open-meteo.com")
        self.assertIs(connection.sock, context.wrap_socket.return_value)

    def response(self, *, status=200, headers=None, body=b'{"results":[]}'):
        headers = headers if headers is not None else [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
        response = Mock(status=status)
        response.getheaders.return_value = headers
        response.getheader.side_effect = lambda key, default=None: next((v for k, v in headers if k.lower() == key.lower()), default)
        response.read.return_value = body
        return response

    def test_http_no_redirect_compression_oversize_or_duplicate_headers(self):
        cases = [(self.response(status=302), "unavailable"), (self.response(status=429), "rate-limit"),
                 (self.response(status=503), "unavailable"),
                 (self.response(headers=[("Content-Type", "text/html")]), "response"),
                 (self.response(headers=[("Content-Type", "application/json"), ("Content-Encoding", "gzip")]), "response"),
                 (self.response(headers=[("Content-Type", "application/json"), ("Content-Length", "999999")]), "response"),
                 (self.response(headers=[("Content-Type", "application/json"), ("Content-Length", "2"), ("content-length", "3")]), "response"),
                 (self.response(headers=[("Content-Type", "application/json"), ("Content-Length", "2"), ("Transfer-Encoding", "chunked")]), "response"),
                 (self.response(body=b"x" * (weather.MAX_BODY + 1)), "response")]
        for response, reason in cases:
            connection = Mock()
            connection.getresponse.return_value = response
            with patch.object(weather, "_PinnedHTTPS", return_value=connection), self.assertRaises(weather.WeatherError) as error:
                weather._https_json("geocode", {"city": "Warszawa"})
            self.assertEqual(error.exception.reason, reason)
            connection.close.assert_called_once()
            self.assertEqual(connection.request.call_count, 1)

    def test_http_success_is_bounded_and_sends_no_credentials(self):
        connection = Mock()
        response = self.response(headers=[("Content-Type", "application/json; charset=utf-8"), ("Transfer-Encoding", "chunked")])
        connection.getresponse.return_value = response
        with patch.object(weather, "_PinnedHTTPS", return_value=connection):
            self.assertEqual(weather._https_json("geocode", {"city": "Warszawa"}), {"results": []})
        response.read.assert_called_once_with(weather.MAX_BODY + 1)
        headers = connection.request.call_args.kwargs["headers"]
        self.assertEqual(set(headers), {"Accept", "Accept-Encoding", "Connection", "User-Agent"})
        self.assertEqual(connection.request.call_args.args[0], "GET")

    def test_worker_is_isolated_environment_bounded_and_error_text_never_leaks(self):
        with patch("ai_desktop.process_io.bounded_process", return_value=(0, b'{"results":[]}', b"SECRET")) as process:
            self.assertEqual(weather._request("geocode", {"city": "Warszawa"}), {"results": []})
        args, kwargs = process.call_args
        self.assertEqual(args[0][1:4], ["-I", "-B", "-c"])
        self.assertEqual(kwargs["env"], {"PATH": "/usr/bin", "LANG": "C.UTF-8"})
        self.assertEqual(kwargs["timeout"], 20)
        self.assertEqual(kwargs["limit"], weather.MAX_BODY)
        for result, expected in (((2, b'{"error":"SECRET"}', b"SECRET"), "response"),
                                 ((2, b'{"error":"rate-limit"}', b""), "rate-limit")):
            with patch("ai_desktop.process_io.bounded_process", return_value=result), self.assertRaises(weather.WeatherError) as error:
                weather._request("geocode", {"city": "Warszawa"})
            self.assertEqual(error.exception.reason, expected)
            self.assertNotIn("SECRET", str(error.exception))
        with patch("ai_desktop.process_io.bounded_process", side_effect=AnalysisDenied("timeout")), self.assertRaises(weather.WeatherError) as error:
            weather._request("geocode", {"city": "Warszawa"})
        self.assertEqual(error.exception.reason, "timeout")

    def test_worker_fixed_grammar_rejects_extra_input_without_request(self):
        stdin = Mock(buffer=io.BytesIO(b'{"kind":"geocode","parameters":{"city":"Warszawa"},"url":"https://localhost"}'))
        stdout = io.StringIO()
        with patch.object(weather.sys, "stdin", stdin), patch.object(weather.sys, "stdout", stdout), patch.object(weather, "_https_json") as fetch:
            self.assertEqual(weather._worker(), 2)
        fetch.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue()), {"error": "input"})


if __name__ == "__main__":
    unittest.main()
