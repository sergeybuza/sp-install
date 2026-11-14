"""API highload: пагинация, метки времени и ошибки без раскрытия данных."""

from contextlib import redirect_stdout
from http.client import IncompleteRead
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("highload_api", ROOT / "scripts/local/highload-api.py")
api_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api_module)
ORIGIN = "http://192.0.2.10:8080"
TOKEN = "fixture-token-only"


class Response:
    status = 200

    def __init__(self, payload, url=ORIGIN + "/api/test", raw=False):
        self.body = io.BytesIO(payload if raw else json.dumps(payload).encode())
        self.url = url

    def read(self, count):
        return self.body.read(count)

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.body.close()


def device(name, identifier=None):
    return {"name": name, "id": {"id": identifier or "id-" + name},
            "additionalInfo": {"lastConnectedGateway": "fixture-gateway"}}


def page(items, pages=1, total=None, has_next=False):
    return {"data": items, "totalPages": pages,
            "totalElements": len(items) if total is None else total, "hasNext": has_next}


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"SP_API_TOKEN": TOKEN}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.api = api_module.PlatformAPI(ORIGIN)

    def test_requests_use_bearer_timeout_and_disabled_proxy(self):
        with patch.object(api_module, "build_opener", wraps=api_module.build_opener) as built:
            api_module.PlatformAPI(ORIGIN)
        handlers = [handler for handler in built.call_args.args if isinstance(handler, ProxyHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].proxies, {})
        with patch.object(self.api.opener, "open", return_value=Response({"ok": True})) as opened:
            self.assertEqual(self.api.request("/api/test"), {"ok": True})
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(opened.call_args.kwargs["timeout"], 8)

    def test_http_network_and_invalid_json_errors_do_not_echo_raw_text(self):
        raw = "fixture-sensitive-content"
        errors = [HTTPError(ORIGIN, 401, raw, {}, io.BytesIO(raw.encode())),
                  URLError(raw), IncompleteRead(raw.encode(), 100)]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                    patch.object(self.api.opener, "open", side_effect=error):
                with self.assertRaises(api_module.APIError) as caught:
                    self.api.request("/api/test")
                self.assertNotIn(raw, str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))
        with patch.object(self.api.opener, "open", return_value=Response(raw.encode(), raw=True)):
            with self.assertRaises(api_module.APIError) as caught:
                self.api.request("/api/test")
            self.assertNotIn(raw, str(caught.exception))

    def test_redirect_and_final_origin_are_checked(self):
        redirect = api_module.SameOriginRedirect(self.api.origin)
        with self.assertRaises(api_module.APIError):
            redirect.redirect_request(Request(ORIGIN + "/api/test"), None, 302, "", {}, "https://example.invalid/")
        with patch.object(self.api.opener, "open", return_value=Response({}, url="https://example.invalid/")):
            with self.assertRaises(api_module.APIError):
                self.api.request("/api/test")

    def test_base_credentials_paths_and_token_header_injection_are_rejected(self):
        for url in ("http://user:pass@192.0.2.10", ORIGIN + "/api", ORIGIN + "?token=fixture"):
            with self.subTest(url=url), self.assertRaises(api_module.APIError):
                api_module.PlatformAPI(url)
        os.environ["SP_API_TOKEN"] = "fixture\r\nHeader: injected"
        with self.assertRaises(api_module.APIError):
            self.api.request("/api/test")


class PaginationTests(unittest.TestCase):
    def test_complete_sorted_pagination_preserves_gateway_record(self):
        api = api_module.PlatformAPI(ORIGIN)
        responses = [page([device("a"), device("b")], pages=2, total=3, has_next=True),
                     page([device("c")], pages=2, total=3)]
        with patch.object(api, "request", side_effect=responses) as request:
            devices = api.devices()
        self.assertEqual(list(devices), ["a", "b", "c"])
        self.assertEqual(devices["a"]["additionalInfo"]["lastConnectedGateway"], "fixture-gateway")
        parameters = [parse_qs(urlsplit(call.args[0]).query) for call in request.call_args_list]
        self.assertEqual([query["page"] for query in parameters], [["0"], ["1"]])
        self.assertEqual(parameters[0]["sortProperty"], ["name"])
        self.assertEqual(parameters[0]["sortOrder"], ["ASC"])

    def test_empty_tenant_is_valid(self):
        api = api_module.PlatformAPI(ORIGIN)
        with patch.object(api, "request", return_value=page([], pages=0)):
            self.assertEqual(api.devices(), {})

    def test_duplicate_stalled_incomplete_and_changed_pages_are_rejected(self):
        cases = ([page([device("a"), device("a")], total=2)],
                 [page([], pages=2, total=1, has_next=True)],
                 [page([device("a")], total=2)],
                 [page([device("a")], pages=2, total=2, has_next=True),
                  page([device("b")], pages=2, total=3)])
        for responses in cases:
            api = api_module.PlatformAPI(ORIGIN)
            with self.subTest(responses=responses), patch.object(api, "request", side_effect=responses):
                with self.assertRaises(api_module.APIError):
                    api.devices()


class TelemetryTests(unittest.TestCase):
    def test_only_requested_timestamps_are_returned_empty_and_missing_omitted(self):
        payload = {"timeseries": {"a": [{"ts": 1001, "value": "fixture-private"}, {"ts": 1000}],
                                  "b": [], "unexpected": [{"ts": "invalid"}]}, "metadata": {}}
        result = api_module.parse_timeseries(payload, {"a", "b", "missing"})
        self.assertEqual(result, {"a": 1001})
        self.assertNotIn("value", repr(result))

    def test_malformed_wrapper_points_and_timestamps_are_rejected(self):
        cases = ({}, {"timeseries": []}, {"timeseries": {"a": {"ts": 1000}}},
                 *({"timeseries": {"a": [{"ts": timestamp}]}} for timestamp in (None, True, "1000", 0, -1, 2 ** 53)),
                 {"timeseries": {"a": [None]}})
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(api_module.APIError):
                api_module.parse_timeseries(payload, {"a"})

    def test_snapshot_skips_missing_device_and_requests_only_expected_keys(self):
        api = api_module.PlatformAPI(ORIGIN)
        with patch.object(api, "devices", return_value={"present": device("present", "id/escaped")}), \
                patch.object(api, "request", return_value={"timeseries": {"a": [{"ts": 1000, "value": "fixture"}]}}) as request:
            result = api.snapshot({"present": {"a", "b"}, "absent": {"c"}})
        self.assertEqual(result, {"present": {"a": 1000}})
        self.assertIn("id%2Fescaped", request.call_args.args[0])
        self.assertEqual(parse_qs(urlsplit(request.call_args.args[0]).query), {"keys": ["a,b"]})

    def test_long_key_query_reads_latest_without_query_and_filters_expected(self):
        api = api_module.PlatformAPI(ORIGIN)
        expected = {f"param_saw{number}" for number in range(1000)}
        with patch.object(api, "devices", return_value={"present": device("present")}), \
                patch.object(api, "request", return_value={"timeseries": {
                    "param_saw3": [{"ts": 1000, "value": "fixture-private"}],
                    "unexpected": [{"ts": 2000, "value": "fixture-private"}]}}) as request:
            result = api.snapshot({"present": expected})
        self.assertEqual(urlsplit(request.call_args.args[0]).query, "")
        self.assertEqual(result, {"present": {"param_saw3": 1000}})


class AuthenticationTests(unittest.TestCase):
    def test_login_token_remains_in_environment_and_role_is_required(self):
        with patch.dict(os.environ, {"SP_TENANT_USERNAME": "fixture-user", "SP_TENANT_PASSWORD": "fixture-password"}, clear=True):
            api = api_module.PlatformAPI(ORIGIN)
            with patch.object(api, "request", side_effect=[{"token": TOKEN}, {"authority": "TENANT_ADMIN"}]):
                self.assertEqual(api.authenticate(), "TENANT_ADMIN")
            self.assertEqual(os.environ["SP_API_TOKEN"], TOKEN)
            self.assertNotIn(TOKEN, repr(api.__dict__))
            with patch.object(api, "request", return_value={"authority": "SYS_ADMIN"}):
                with self.assertRaises(api_module.APIError):
                    api.authenticate()

    def test_supply_defaults_require_explicit_flag_and_stop_at_tenant_row(self):
        parent = ROOT / "reports/highload-api-tests"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            document = root / "docs/installation/smart-platform-2.0.0.md"
            document.parent.mkdir(parents=True)
            document.write_text("| Администратор (`USER_LOCAL`, `PASSWORD_LOCAL`) | `fixture-user` | `fixture-password` |\n"
                                "| Other (`SYSADMIN_USER_LOCAL`, `SYSADMIN_PASSWORD_LOCAL`) | `other` | `other` |\n")
            api = api_module.PlatformAPI(ORIGIN)
            with patch.object(api_module, "load_supply_defaults") as load:
                with self.assertRaises(api_module.APIError):
                    api.authenticate()
                load.assert_not_called()
            api_module.load_supply_defaults(root)
            self.assertEqual(os.environ["SP_TENANT_USERNAME"], "fixture-user")
            self.assertEqual(os.environ["SP_TENANT_PASSWORD"], "fixture-password")

    def test_cli_prints_only_auth_role_and_counts(self):
        with patch.object(api_module.PlatformAPI, "authenticate", return_value="TENANT_ADMIN"), \
                patch.object(api_module.PlatformAPI, "devices", return_value={
                    "b1_b1_1k_n30_test": device("b1_b1_1k_n30_test"), "other": device("other")}), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(api_module.main(["--url", ORIGIN, "--preflight"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "auth_ok": True, "role": "TENANT_ADMIN", "matching_test_device_count": 1, "total_device_count": 2})


if __name__ == "__main__":
    unittest.main()
