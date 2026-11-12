#!/usr/bin/env python3
"""Проверка UI и авторизации API. Пароль читается только из окружения."""

import argparse
from html.parser import HTMLParser
import json
import os
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.urls.append(values["src"])
        if tag == "link" and values.get("rel") == "stylesheet" and values.get("href"):
            self.urls.append(values["href"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://192.168.10.107:8080")
    parser.add_argument("--ca-cert", help="Публичный сертификат самоподписанного сервера")
    args = parser.parse_args()
    origin = urlsplit(args.url)
    if origin.scheme not in {"http", "https"} or not origin.netloc or origin.username or origin.password:
        raise ValueError("Нужен HTTP/HTTPS URL без учётных данных")
    if origin.scheme == "http" and args.ca_cert:
        raise ValueError("Публичный сертификат применяется только для HTTPS URL")
    base = args.url.rstrip("/") + "/"
    context = ssl.create_default_context(cafile=args.ca_cert)

    class SameOriginRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, new_url):
            target = urlsplit(new_url)
            if target.scheme != origin.scheme or target.netloc != origin.netloc:
                raise ValueError("Перенаправление за пределы проверяемого узла запрещено")
            return super().redirect_request(request, response, code, message, headers, new_url)

    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), SameOriginRedirect())
    username = os.environ.get("SP_ADMIN_USERNAME", "sysadmin@smart-platform-2026.ru")
    password = os.environ.get("SP_ADMIN_PASSWORD")
    if not password:
        raise ValueError("Задайте SP_ADMIN_PASSWORD в окружении процесса")

    def fetch(path, payload=None, token=None, accept="application/json"):
        headers = {"Accept": accept}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        url = urljoin(base, path)
        if urlsplit(url).netloc != origin.netloc:
            raise ValueError("Проверка ограничена указанным узлом")
        request = Request(url, data=data, headers=headers)
        with opener.open(request, timeout=30) as response:
            final = urlsplit(response.url)
            if final.scheme != origin.scheme or final.netloc != origin.netloc:
                raise ValueError("Ответ перенаправлен за пределы проверяемого узла")
            return response.status, response.headers.get_content_type(), response.read()

    status, content_type, body = fetch("login", accept="text/html")
    if status != 200 or content_type != "text/html":
        raise ValueError("Страница входа не вернула HTML с кодом 200")
    print("OK: HTTPS, сертификат, HTML /login" if origin.scheme == "https" else "OK: HTTP, HTML /login")
    assets = Assets()
    assets.feed(body.decode("utf-8"))
    checked = 0
    for asset in dict.fromkeys(assets.urls):
        url = urlsplit(urljoin(base, asset))
        if url.scheme != origin.scheme or url.netloc != origin.netloc:
            continue
        status, content_type, _ = fetch(asset, accept="*/*")
        if status != 200 or content_type == "text/html":
            raise ValueError("Ресурс UI не загрузился в ожидаемом формате")
        checked += 1
    if checked == 0:
        raise ValueError("В HTML не найдены доступные локальные JS/CSS ресурсы")
    print(f"OK: ресурсы UI ({checked})")

    status, _, body = fetch("api/auth/login", {"username": username, "password": password})
    auth = json.loads(body)
    token = auth.get("token")
    if status != 200 or not isinstance(token, str) or not token:
        raise ValueError("API не вернул токен авторизации")
    status, _, body = fetch("api/auth/user", token=token)
    user = json.loads(body)
    if (
        status != 200
        or not isinstance(user, dict)
        or user.get("email") != username
        or user.get("authority") != "SYS_ADMIN"
        or not isinstance(user.get("id"), dict)
        or not user["id"].get("id")
    ):
        raise ValueError("API не вернул авторизованного пользователя")
    print("OK: API login и получение авторизованного пользователя")


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        # Тело ошибки не печатается: оно может содержать данные запроса.
        print(f"FAIL: HTTP {error.code}", file=sys.stderr)
        sys.exit(1)
    except (ValueError, URLError, OSError) as error:
        # Не печатаем response body, credentials, токены или конфигурацию.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
