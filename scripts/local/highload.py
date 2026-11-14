#!/usr/bin/env python3
"""Управление highload на рабочем компьютере; секреты только в окружении."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "sp-highload-mac"
SERVICES = ("mosquitto-local", "sp-agent", "sp-pubmqtt")
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class LoadError(Exception):
    pass


def local_path(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise LoadError("Путь сценария выходит за пределы этой рабочей копии")
    return path


@dataclass
class Scenario:
    path: Path
    devices: int
    signals: int
    period_ms: int
    topics: frozenset
    sha256: str

    @property
    def total(self):
        return len(self.topics)

    @property
    def level(self):
        return (self.devices, self.signals, self.period_ms)

    def description(self):
        return (f"{self.devices} устройств × {self.signals} сигналов = {self.total}; "
                f"период {self.period_ms} мс, задано около {self.total * 1000 / self.period_ms:g} публикаций/с")


def read_scenario(path):
    try:
        content = path.read_bytes()
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                strings = ["".join(item.itertext()) for item in ET.fromstring(
                    archive.read("xl/sharedStrings.xml")).findall("s:si", NS)]
            document = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            topics, periods, counts = set(), set(), {}
            rows = document.findall("s:sheetData/s:row", NS)
            for index, row in enumerate(rows):
                cells = {}
                for cell in row.findall("s:c", NS):
                    column = re.sub(r"\d", "", cell.attrib["r"])
                    value = cell.findtext("s:v", default="", namespaces=NS)
                    if cell.attrib.get("t") == "s":
                        value = strings[int(value)]
                    elif cell.attrib.get("t") == "inlineStr":
                        value = "".join(cell.find("s:is", NS).itertext())
                    cells[column] = value
                if index == 0:
                    if any(cells.get(column) != title for column, title in {
                            "A": "Topic", "C": "Period", "J": "TotalNumberPerTopic"}.items()):
                        raise LoadError("Заголовки XLSX не соответствуют сценарию PubMQTT")
                    continue
                topic = cells.get("A", "")
                if not topic:
                    continue
                match = re.fullmatch(r"/devices/([^/]+)/controls/([^/]+)", topic)
                if not match or topic in topics:
                    raise LoadError("В XLSX некорректный или повторяющийся топик")
                period = float(cells["C"])
                if not period.is_integer() or period <= 0 or float(cells["J"]) != 0:
                    raise LoadError("Нужны положительный целый период и J=0")
                topics.add(topic)
                periods.add(int(period))
                counts[match[1]] = counts.get(match[1], 0) + 1
        if not counts or len(periods) != 1 or len(set(counts.values())) != 1:
            raise LoadError("Нужен единый период и одинаковое число сигналов на устройство")
        return Scenario(path, len(counts), next(iter(counts.values())), periods.pop(),
                        frozenset(topics), hashlib.sha256(content).hexdigest())
    except (OSError, ValueError, KeyError, IndexError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise LoadError("Не удалось прочитать и проверить XLSX-сценарий") from exc


class Controller:
    def __init__(self, root=ROOT, env=None):
        self.root = root.resolve()
        self.env = dict(os.environ if env is None else env)
        self.state_dir = local_path(self.root, "data/highload")
        self.state_file = local_path(self.root, self.state_dir / "active.json")
        self.compose = ["docker", "compose", "-p", PROJECT, "-f",
                        str(self.root / "deploy/compose/highload-mac.yml")]
        self.secrets = [self.env["AGENT_ACCESS_TOKEN"]] if self.env.get("AGENT_ACCESS_TOKEN") else []

    def run(self, args, env=None, timeout=90):
        try:
            result = subprocess.run(args, cwd=self.root, env=self.env if env is None else env,
                                    capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise LoadError("Не найден исполняемый файл: " + args[0]) from exc
        except subprocess.TimeoutExpired as exc:
            raise LoadError("Истекло время ожидания команды; проверьте status") from exc
        if result.returncode:
            message = result.stderr.strip()
            for secret in self.secrets:
                message = message.replace(secret, "[REDACTED]")
            raise LoadError(f"Команда завершилась кодом {result.returncode}: {message[:1000]}")
        return result.stdout

    def containers(self):
        ids = self.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"]).split()
        if not ids:
            return {}
        try:
            records = json.loads(self.run(["docker", "inspect", *ids]))
        except ValueError as exc:
            raise LoadError("Docker inspect вернул некорректный JSON") from exc
        result = {}
        for item in records:
            labels = item["Config"].get("Labels") or {}
            service = labels.get("com.docker.compose.service")
            if labels.get("com.docker.compose.project") != PROJECT:
                raise LoadError("Docker вернул контейнер другого проекта")
            if service not in SERVICES:
                continue
            if service in result:
                raise LoadError("Найдено несколько контейнеров сервиса " + service)
            result[service] = item
        return result

    def current(self, containers):
        # Смонтированный файл — источник текущего уровня при первом запуске утилиты.
        pub = containers.get("sp-pubmqtt")
        if pub and pub["State"]["Running"]:
            for mount in pub.get("Mounts", []):
                if mount.get("Destination") == "/etc/pubmqtt/test_sp_local.xlsx":
                    scenario = read_scenario(local_path(self.root, mount["Source"]))
                    if self.state_file.exists():
                        try:
                            saved = json.loads(self.state_file.read_text())
                            if (local_path(self.root, saved["scenario"]) == scenario.path
                                    and saved["sha256"] != scenario.sha256):
                                raise LoadError("Смонтированный XLSX изменён после применения; восстановите исходный файл")
                        except (OSError, ValueError, KeyError, TypeError) as exc:
                            raise LoadError("Повреждён файл выбранного уровня") from exc
                    return scenario
        if self.state_file.exists():
            try:
                saved = json.loads(self.state_file.read_text())
                scenario = read_scenario(local_path(self.root, saved["scenario"]))
                if scenario.sha256 != saved["sha256"]:
                    raise LoadError("Сохранённый сценарий изменён; восстановите файл перед запуском")
                return scenario
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise LoadError("Повреждён файл выбранного уровня data/highload/active.json") from exc
        if pub:
            for mount in pub.get("Mounts", []):
                if mount.get("Destination") == "/etc/pubmqtt/test_sp_local.xlsx":
                    return read_scenario(local_path(self.root, mount["Source"]))
        source = self.root / "input/highload/sp-emulation/config/test_sp_local.xlsx"
        return read_scenario(source) if source.exists() else None

    def save(self, scenario):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        data = {"scenario": str(scenario.path.relative_to(self.root)), "sha256": scenario.sha256,
                "devices": scenario.devices, "signals_per_device": scenario.signals,
                "period_ms": scenario.period_ms}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.state_dir, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(self.state_file)

    @contextmanager
    def lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with local_path(self.root, self.state_dir / "control.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LoadError("Другая команда highload ещё выполняется") from exc
            yield

    def node(self):
        if self.env.get("SP_HIGHLOAD_NODE"):
            return self.env["SP_HIGHLOAD_NODE"]
        modules = self.root / "node_modules"
        if modules.is_symlink():
            candidate = modules.resolve().parent / "bin/node"
            if candidate.is_file():
                return str(candidate)
        raise LoadError("Для нового сценария настройте SP_HIGHLOAD_NODE и node_modules из поставляемого runtime; скачивание пакетов не выполняется")

    def prepare(self, devices, signals, period):
        cached = local_path(self.root, self.state_dir / "scenarios" / f"{devices}x{signals}-{period}ms.xlsx")
        legacy = self.root / "deploy/config/highload" / f"test_sp_{devices}x{signals}.xlsx"
        for candidate in (cached, legacy):
            if candidate.exists():
                scenario = read_scenario(local_path(self.root, candidate))
                if scenario.level == (devices, signals, period):
                    return scenario
        cached.parent.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(suffix=".xlsx", dir=cached.parent)
        os.close(descriptor)
        temporary = Path(filename)
        try:
            self.run([self.node(), str(self.root / "scripts/local/generate-highload-scenario.mjs"),
                      "--devices", str(devices), "--signals", str(signals),
                      "--period-ms", str(period), "--output", str(temporary)], timeout=180)
            scenario = read_scenario(temporary)
            if scenario.level != (devices, signals, period):
                raise LoadError("Генератор создал другой уровень нагрузки")
            temporary.replace(cached)
            scenario.path = cached
            return scenario
        finally:
            temporary.unlink(missing_ok=True)

    def compose_env(self, scenario, containers):
        env = dict(self.env)
        if not env.get("AGENT_ACCESS_TOKEN") and containers.get("sp-agent"):
            values = dict(value.split("=", 1) for value in containers["sp-agent"]["Config"]["Env"] if "=" in value)
            env["AGENT_ACCESS_TOKEN"] = values.get("AGENT_ACCESS_TOKEN", "")
        if not env.get("AGENT_ACCESS_TOKEN"):
            raise LoadError("Для первого запуска задайте AGENT_ACCESS_TOKEN в окружении")
        self.secrets.append(env["AGENT_ACCESS_TOKEN"])
        env["HIGHLOAD_SCENARIO"] = str(scenario.path)
        return env

    def apply(self, scenario, containers):
        env = self.compose_env(scenario, containers)
        self.run(self.compose + ["config", "--quiet"], env=env)
        self.run(self.compose + ["up", "-d", "--no-deps", "--force-recreate", "--pull", "never", "sp-pubmqtt"], env=env)
        self.check_local(scenario, timeout=max(10, 3 * scenario.period_ms / 1000 + 3))

    def check_local(self, scenario, timeout=10):
        containers = self.containers()
        if any(not containers.get(service, {}).get("State", {}).get("Running") for service in SERVICES):
            raise LoadError("Для проверки нужны три работающих контейнера; выполните start")
        count = scenario.total * 2
        output = self.run(["docker", "exec", containers["mosquitto-local"]["Id"], "mosquitto_sub",
                           "-h", "127.0.0.1", "-t", "/devices/+/controls/+", "-v",
                           "-C", str(count), "-W", str(math.ceil(timeout))], timeout=timeout + 5)
        observed = {line.split(" ", 1)[0] for line in output.splitlines() if line}
        missing = scenario.topics - observed
        if missing:
            raise LoadError(f"Локальный MQTT не подтвердил {len(missing)} из {scenario.total} топиков")
        return len(output.splitlines())

    def set_level(self, devices, signals, period):
        containers = self.containers()
        previous = self.current(containers)
        period = period if period is not None else previous.period_ms if previous else 1000
        if previous and previous.level == (devices, signals, period):
            self.save(previous)
            print("Уже выбран этот уровень: " + previous.description())
            return
        print(f"Подготовка {devices}×{signals}, период {period} мс…", flush=True)
        scenario = self.prepare(devices, signals, period)
        running = containers.get("sp-pubmqtt", {}).get("State", {}).get("Running", False)
        if running:
            if any(not containers.get(service, {}).get("State", {}).get("Running") for service in SERVICES):
                raise LoadError("Часть контейнеров остановлена; сначала выполните start")
            try:
                self.apply(scenario, containers)
            except LoadError:
                if previous:
                    try:
                        self.apply(previous, self.containers())
                        print("После ошибки восстановлен прежний сценарий.", file=sys.stderr)
                    except LoadError:
                        raise LoadError("Применение и восстановление не завершились; проверьте status и start") from None
                raise
        self.save(scenario)
        print(("Нагрузка изменена: " if running else "Уровень сохранён; запуск командой start: ") + scenario.description())

    def start(self):
        containers = self.containers()
        scenario = self.current(containers)
        if not scenario:
            raise LoadError("Сначала выберите уровень командой set")
        env = self.compose_env(scenario, containers)
        self.run(self.compose + ["config", "--quiet"], env=env)
        self.run(self.compose + ["up", "--wait", "--wait-timeout", "60", "--no-recreate", "--pull", "never",
                                 "mosquitto-local", "sp-agent"], env=env)
        self.run(self.compose + ["up", "--wait", "--wait-timeout", "60", "--no-deps", "--pull", "never", "sp-pubmqtt"], env=env)
        self.check_local(scenario, timeout=max(10, 3 * scenario.period_ms / 1000 + 3))
        self.save(scenario)
        print("Нагрузка запущена: " + scenario.description())

    def stop(self):
        containers = self.containers()
        scenario = self.current(containers)
        if scenario:
            self.save(scenario)
        for service in ("sp-pubmqtt", "sp-agent", "mosquitto-local"):
            item = containers.get(service)
            if item and item["State"]["Running"]:
                self.run(["docker", "stop", "--time", "30", item["Id"]], timeout=40)
        print("Нагрузка остановлена; выбранный уровень сохранён.")

    def status(self):
        containers = self.containers()
        scenario = self.current(containers)
        print("Выбранный уровень: " + scenario.description() if scenario else "Уровень ещё не выбран.")
        for service in SERVICES:
            item = containers.get(service)
            print(f"{service}: {item['State']['Status']}, перезапусков {item['RestartCount']}" if item else f"{service}: не создан")
        print("Статус контейнеров не подтверждает сохранение телеметрии в платформе.")


def positive(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Нужно положительное целое число") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("Нужно положительное целое число")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    set_parser = commands.add_parser("set", help="Выбрать устройства и сигналы на устройство")
    set_parser.add_argument("devices", type=positive)
    set_parser.add_argument("signals", type=positive)
    set_parser.add_argument("--period-ms", type=positive, help="Период; по умолчанию сохраняется текущий")
    for action in ("start", "stop", "status"):
        commands.add_parser(action)
    check_parser = commands.add_parser("check", help="Проверить все активные топики в локальном MQTT")
    check_parser.add_argument("--timeout", type=positive, default=10)
    args = parser.parse_args(argv)
    try:
        controller = Controller()
        with controller.lock():
            if args.action == "set":
                controller.set_level(args.devices, args.signals, args.period_ms)
            elif args.action == "check":
                scenario = controller.current(controller.containers())
                if not scenario:
                    raise LoadError("Сценарий не выбран")
                messages = controller.check_local(scenario, args.timeout)
                print(f"Локальный MQTT: {scenario.total} топиков подтверждены, получено {messages} сообщений.")
                print("Доставка и сохранение в платформе проверяются отдельно через API.")
            else:
                getattr(controller, args.action)()
    except (LoadError, OSError) as exc:
        print("Ошибка: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
