#!/usr/bin/env python3
"""На Mac: ограниченное чтение API для highload, без сохранения телеметрии.

Авторизация берётся из окружения. Поставочные реквизиты читаются только
с явным --use-supply-defaults. Токен не записывается в файлы и не печатается.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class APIError(RuntimeError):
    """Безопасное сообщение без ответа API, URL с секретом и реквизитов."""


def origin_of(url):
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or any(ord(character) < 33 for character in url)):
            raise ValueError
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        return parts.scheme, parts.hostname.lower(), port if port is not None else (443 if parts.scheme == "https" else 80)
    except (TypeError, ValueError):
        raise APIError("Некорректный адрес API") from None


class SameOriginRedirect(HTTPRedirectHandler):
    def __init__(self, origin):
        super().__init__()
        self.origin = origin

    def redirect_request(self, request, response, code, message, headers, new_url):
        if origin_of(urljoin(request.full_url, new_url)) != self.origin:
            raise APIError("Перенаправление за пределы API запрещено")
        return super().redirect_request(request, response, code, message, headers, new_url)


def load_supply_defaults(root=ROOT):
    """Читает только строку USER_LOCAL/PASSWORD_LOCAL, прекращая чтение на ней."""
    path = root / "docs/installation/smart-platform-2.0.0.md"
    if not path.resolve().is_relative_to(root.resolve()):
        raise APIError("Документ поставочных реквизитов вне рабочей копии")
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if "`USER_LOCAL`" not in line or "`PASSWORD_LOCAL`" not in line:
                    continue
                columns = line.split("|")
                if len(columns) < 5:
                    break
                username, password = (columns[index].strip().strip("`") for index in (2, 3))
                if not username or not password:
                    break
                os.environ["SP_TENANT_USERNAME"] = username
                os.environ["SP_TENANT_PASSWORD"] = password
                return
    except (OSError, UnicodeError):
        raise APIError("Не удалось прочитать разрешённую строку реквизитов поставки") from None
    raise APIError("Разрешённая строка реквизитов поставки не найдена")


def parse_timeseries(payload, expected_keys):
    if not isinstance(payload, dict) or not isinstance(payload.get("timeseries"), dict):
        raise APIError("Некорректная обёртка телеметрии")
    result = {}
    for key in expected_keys:
        if key not in payload["timeseries"]:
            continue
        points = payload["timeseries"][key]
        if not isinstance(points, list):
            raise APIError("Некорректный список точек телеметрии")
        timestamps = []
        for point in points:
            if (not isinstance(point, dict) or type(point.get("ts")) is not int
                    or not 0 < point["ts"] <= 2 ** 53 - 1):
                raise APIError("Некорректная временная метка телеметрии")
            timestamps.append(point["ts"])
        if timestamps:
            result[key] = max(timestamps)
    return result


class PlatformAPI:
    def __init__(self, url, timeout=8):
        self.origin = origin_of(url)
        parts = urlsplit(url)
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise APIError("Нужен корневой адрес API без пути и параметров")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise APIError("Некорректный тайм-аут API")
        self.base_url = url.rstrip("/")
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), SameOriginRedirect(self.origin))

    def request(self, path, payload=None, authenticated=True, deadline=None):
        if not isinstance(path, str) or not path.startswith("/api/") or "#" in path:
            raise APIError("Недопустимый путь API")
        url = self.base_url + path
        if origin_of(url) != self.origin:
            raise APIError("Запрос за пределы API запрещён")
        timeout = self.timeout
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise APIError("Истекло время раунда API")
        headers = {"Accept": "application/json"}
        if authenticated:
            token = os.environ.get("SP_API_TOKEN", "")
            if not token or any(not 33 <= ord(character) <= 126 for character in token):
                raise APIError("Не задан допустимый SP_API_TOKEN")
            headers["Authorization"] = "Bearer " + token
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            request = Request(url, data=data, headers=headers)
            with self.opener.open(request, timeout=timeout) as response:
                if origin_of(response.geturl()) != self.origin:
                    raise APIError("Ответ перенаправлен за пределы API")
                if response.status != 200:
                    code = response.status if type(response.status) is int else 0
                    raise APIError(f"HTTP {code}")
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise APIError("Ответ API превышает допустимый размер")
            return json.loads(body)
        except HTTPError as error:
            code = error.code if type(error.code) is int else 0
            error.close()
            raise APIError(f"HTTP {code}") from None
        except APIError:
            raise
        except (URLError, OSError, HTTPException, ValueError, UnicodeError):
            raise APIError("Не удалось получить корректный JSON от API") from None

    def authenticate(self, use_supply_defaults=False):
        if not os.environ.get("SP_API_TOKEN"):
            if os.environ.get("SP_TENANT_USERNAME") or os.environ.get("SP_TENANT_PASSWORD"):
                username_key, password_key = "SP_TENANT_USERNAME", "SP_TENANT_PASSWORD"
            elif os.environ.get("SP_ADMIN_USERNAME") or os.environ.get("SP_ADMIN_PASSWORD"):
                username_key, password_key = "SP_ADMIN_USERNAME", "SP_ADMIN_PASSWORD"
            elif use_supply_defaults:
                load_supply_defaults()
                username_key, password_key = "SP_TENANT_USERNAME", "SP_TENANT_PASSWORD"
            else:
                raise APIError("Задайте токен либо пару реквизитов API в окружении")
            if not os.environ.get(username_key) or not os.environ.get(password_key):
                raise APIError("Неполная пара реквизитов API в окружении")
            auth = self.request("/api/auth/login", {
                "username": os.environ[username_key], "password": os.environ[password_key],
            }, authenticated=False)
            if (not isinstance(auth, dict) or not isinstance(auth.get("token"), str)
                    or not auth["token"] or any(not 33 <= ord(c) <= 126 for c in auth["token"])):
                raise APIError("API не вернул допустимый токен")
            os.environ["SP_API_TOKEN"] = auth["token"]
        user = self.request("/api/auth/user")
        if not isinstance(user, dict) or user.get("authority") != "TENANT_ADMIN":
            raise APIError("Нужна роль TENANT_ADMIN")
        return "TENANT_ADMIN"

    def devices(self, deadline=None):
        result, page, expected_total, expected_pages = {}, 0, None, None
        while True:
            payload = self.request("/api/tenant/devices?" + urlencode({
                "pageSize": 100, "page": page, "sortProperty": "name", "sortOrder": "ASC",
            }), deadline=deadline)
            if (not isinstance(payload, dict) or not isinstance(payload.get("data"), list)
                    or type(payload.get("totalPages")) is not int or payload["totalPages"] < 0
                    or type(payload.get("totalElements")) is not int or payload["totalElements"] < 0
                    or type(payload.get("hasNext")) is not bool):
                raise APIError("Некорректная страница устройств")
            if page == 0:
                expected_total, expected_pages = payload["totalElements"], payload["totalPages"]
            if payload["totalElements"] != expected_total or payload["totalPages"] != expected_pages:
                raise APIError("Список устройств изменился во время пагинации")
            for device in payload["data"]:
                if (not isinstance(device, dict) or not isinstance(device.get("name"), str)
                        or not device["name"] or not isinstance(device.get("id"), dict)
                        or not isinstance(device["id"].get("id"), str) or not device["id"]["id"]):
                    raise APIError("Некорректная запись устройства")
                if device["name"] in result:
                    raise APIError("Повторное имя устройства в пагинации")
                result[device["name"]] = device
            page += 1
            if payload["hasNext"]:
                if not payload["data"] or page >= expected_pages or len(result) >= expected_total:
                    raise APIError("Пагинация устройств не продвигается")
            else:
                if len(result) != expected_total or page != max(1, expected_pages):
                    raise APIError("Получен неполный список устройств")
                return result

    def snapshot(self, expected):
        if (not isinstance(expected, dict) or any(
                not isinstance(name, str) or not isinstance(keys, (set, frozenset))
                or any(not isinstance(key, str) or not key for key in keys)
                for name, keys in expected.items())):
            raise APIError("Некорректный набор ожидаемых сигналов")
        deadline = time.monotonic() + 10
        devices = self.devices(deadline=deadline)

        def read_device(name, keys):
            device_id = quote(devices[name]["id"]["id"], safe="")
            path = f"/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries"
            if not keys:
                return name, {}
            query = urlencode({"keys": ",".join(sorted(keys))})
            # Большой CSV может превысить лимит URL сервера. Тогда читаем
            # latest целиком и всё равно возвращаем только ожидаемые метки.
            if len(query.encode("ascii")) <= 4000:
                path += "?" + query
            payload = self.request(path, deadline=deadline)
            return name, parse_timeseries(payload, keys)

        executor = ThreadPoolExecutor(max_workers=4)
        futures = [executor.submit(read_device, name, keys)
                   for name, keys in expected.items() if name in devices]
        result = {}
        try:
            for future in as_completed(futures, timeout=max(0, deadline - time.monotonic())):
                name, timestamps = future.result()
                result[name] = timestamps
        except FuturesTimeout:
            raise APIError("Истекло время раунда API") from None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://192.168.10.107:8080")
    parser.add_argument("--preflight", action="store_true", required=True)
    parser.add_argument("--use-supply-defaults", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        api = PlatformAPI(arguments.url)
        role = api.authenticate(use_supply_defaults=arguments.use_supply_defaults)
        devices = api.devices()
        print(json.dumps({
            "auth_ok": True, "role": role,
            "matching_test_device_count": sum(bool(re.fullmatch(r"b1_b1_1k_n\d+_test", name)) for name in devices),
            "total_device_count": len(devices),
        }), flush=True)
    except APIError as error:
        print("STOP: " + str(error), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
