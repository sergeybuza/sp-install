#!/usr/bin/env python3
"""Проверка локального sp-mcp по HTTP и подсчёт устройств без изменения платформы."""

import http.client
import json
import sys


class MCP:
    def __init__(self):
        self.session = None
        self.protocol = "2025-06-18"

    def call(self, method, params=None, request_id=None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if request_id is not None:
            payload["id"] = request_id
        connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
        try:
            connection.request("POST", "/mcp", json.dumps(payload).encode(), headers)
            response = connection.getresponse()
            self.session = response.getheader("Mcp-Session-Id") or self.session
            if request_id is None:
                if response.status != 202:
                    raise ValueError("Уведомление initialized не принято")
                return None
            if response.status != 200:
                raise ValueError(f"MCP вернул HTTP {response.status}")
            if "text/event-stream" in response.getheader("Content-Type", ""):
                result = self.read_event(response, request_id)
            else:
                raw = response.read(1048577)
                if len(raw) > 1048576:
                    raise ValueError("Ответ MCP превышает 1 MiB")
                result = json.loads(raw)
            if result.get("id") != request_id or "error" in result:
                raise ValueError("Неуспешный ответ JSON-RPC")
            return result["result"]
        finally:
            connection.close()

    @staticmethod
    def read_event(response, request_id):
        data = []
        total = 0
        while True:
            raw = response.readline(1048577)
            total += len(raw)
            if total > 1048576:
                raise ValueError("Поток MCP превышает 1 MiB")
            if not raw:
                raise ValueError("Поток MCP закрыт до ответа")
            line = raw.decode().rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                event = json.loads("\n".join(data))
                data = []
                if event.get("id") == request_id:
                    return event

    def close(self):
        if not self.session:
            return
        connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=10)
        try:
            connection.request("DELETE", "/mcp", headers={
                "Mcp-Session-Id": self.session,
                "MCP-Protocol-Version": self.protocol,
            })
            if connection.getresponse().status != 200:
                raise ValueError("MCP-сессия не закрыта")
        finally:
            connection.close()


def tool_json(result):
    if result.get("isError"):
        raise ValueError("Операция MCP завершилась ошибкой")
    text = [part["text"] for part in result["content"] if part["type"] == "text"]
    if len(text) != 1:
        raise ValueError("Неожиданный формат результата операции")
    return json.loads(text[0])


def main():
    client = MCP()
    try:
        initialized = client.call("initialize", {
            "protocolVersion": client.protocol,
            "capabilities": {},
            "clientInfo": {"name": "sp-install-check", "version": "1"},
        }, 1)
        if initialized["serverInfo"] != {"name": "smart-platform-mcp", "version": "1.0.0"}:
            raise ValueError("Неожиданная версия сервера")
        if initialized["protocolVersion"] != client.protocol or not client.session:
            raise ValueError("Протокол или заголовок MCP-сессии не подтверждён")
        client.call("notifications/initialized")
        print("OK: initialize, MCP 2025-06-18, smart-platform-mcp 1.0.0")

        tools = client.call("tools/list", request_id=2)["tools"]
        if {tool["name"] for tool in tools} != {"search_tools", "call_tool", "run", "get_result"}:
            raise ValueError("Каталог не соответствует режиму facade")
        print("OK: tools/list, четыре инструмента facade")

        found = tool_json(client.call("tools/call", {
            "name": "search_tools",
            "arguments": {"query": "Сколько устройств на платформе?", "limit": 3},
        }, 3))
        if found.get("clarify") or not any(
            card.get("name") == "entity.count" for card in found.get("cards", [])
        ):
            raise ValueError("Операция entity.count не найдена однозначно")
        count = tool_json(client.call("tools/call", {
            "name": "call_tool",
            "arguments": {"name": "entity.count", "args": {"type": "device"}},
        }, 4))
        if type(count.get("count")) is not int or count["count"] < 0:
            raise ValueError("Платформа не вернула число устройств")
        print(f"OK: search_tools → call_tool → entity.count, устройств: {count['count']}")
    finally:
        client.close()
    print("OK: MCP-сессия закрыта")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
        # Тела ответов и параметры запросов не выводятся.
        print(f"FAIL: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
