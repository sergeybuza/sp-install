#!/usr/bin/env python3
"""На Mac: ступени highload с независимым SSH-контролем и остановкой.

Сырые значения телеметрии и секреты не сохраняются. Результаты конкретного
запуска находятся только в reports/. Доступ к стенду нужен через SSH alias;
контейнерами платформы этот скрипт не управляет.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def levels():
    yield from ((30, 150), (40, 200), (50, 250), (60, 300), (70, 400))
    devices, signals = 80, 500
    while True:
        yield devices, signals
        devices, signals = devices + 10, signals + 100


def expected_topics(scenario):
    result = {}
    for topic in scenario.topics:
        _, _, device, _, key = topic.split("/")
        result.setdefault(device, set()).add(key)
    return result


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def validate_metric(record):
    if not isinstance(record, dict):
        raise ValueError("Некорректная запись метрик SSH")
    for name in ("cpu_percent", "memory_percent", "interval_s"):
        value = record.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Некорректные метрики SSH")
    if not 0 <= record["cpu_percent"] <= 100 or not 0 <= record["memory_percent"] <= 100:
        raise ValueError("Некорректный диапазон метрик SSH")
    if not 5 <= record["interval_s"] <= 20:
        raise ValueError("Нарушен интервал измерений SSH")
    allowed = {"timestamp_utc", "interval_s", "cpu_percent", "cpu_idle_percent", "cpu_iowait_percent",
               "cpu_total_ticks_delta", "memory_percent", "memory_total_bytes", "memory_available_bytes",
               "swap_total_bytes", "swap_used_bytes", "disk_total_bytes", "disk_free_bytes", "disk_available_bytes"}
    return {key: value for key, value in record.items() if key in allowed}


def metric_threshold_reason(record):
    if record["cpu_percent"] >= 65:
        return "cpu_threshold"
    if record["memory_percent"] >= 65:
        return "memory_threshold"
    return None


def delivery_summary(snapshot, expected, started_ms, now_ms, previous):
    total = sum(map(len, expected.values()))
    fresh = sum(snapshot.get(device, {}).get(key, 0) >= started_ms
                for device, keys in expected.items() for key in keys)
    witness_ages, witnesses_missing, progressed = [], 0, 0
    current = {}
    for device, keys in expected.items():
        # saw3/4 меняются в исходном сценарии; сравниваем timestamp, не value.
        witnesses = keys & {"param_saw3", "param_saw4"}
        if not witnesses:
            raise ValueError("В сценарии нет сигналов контроля живости")
        for key in witnesses:
            timestamp = snapshot.get(device, {}).get(key, 0)
            if timestamp > now_ms + 5000:
                raise ValueError("Временная метка сигнала опережает часы Mac")
            current[(device, key)] = timestamp
            if timestamp < started_ms:
                witnesses_missing += 1
            else:
                witness_ages.append(now_ms - timestamp)
            if timestamp > previous.get((device, key), 0):
                progressed += 1
    return {
        "expected_keys": total, "fresh_keys": fresh, "missing_fresh_keys": total - fresh,
        "witnesses": len(current), "missing_witnesses": witnesses_missing,
        "progressed_witnesses": progressed,
        "max_witness_age_ms": max(witness_ages) if witness_ages else None,
    }, current


class Staircase:
    def __init__(self, args):
        self.args = args
        self.control_module = module("staircase_control", "scripts/local/highload.py")
        self.api_module = module("staircase_api", "scripts/local/highload-api.py")
        self.control = self.control_module.Controller()
        self.api = self.api_module.PlatformAPI(args.url)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.directory = ROOT / "reports/stand-192.168.10.107" / (stamp + "-highload")
        self.directory.mkdir(parents=True, exist_ok=False)
        self.lock = threading.RLock()
        self.stage = None
        self.child = None
        self.monitor = None
        self.last_metric = 0.0
        self.latest = None
        self.threshold_reason = None
        self.threshold_record = None
        self.monitor_error = False
        self.done = threading.Event()
        self.stop_completed = threading.Event()
        self.stages = []
        self.started = utc()
        self.final_reason = "preflight"
        self.failures = []
        self.write_state()

    def append(self, filename, record):
        with self.lock:
            with (self.directory / filename).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")

    def write_state(self):
        data = {"started_utc": self.started, "updated_utc": utc(), "reason": self.final_reason,
                "stage": self.stage, "stages": self.stages, "failures": self.failures,
                "threshold_reason": self.threshold_reason, "threshold_record": self.threshold_record}
        temporary = self.directory / "state.tmp"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.directory / "state.json")

    def save_state_best_effort(self):
        try:
            self.write_state()
        except OSError:
            if "Не удалось записать состояние отчёта" not in self.failures:
                self.failures.append("Не удалось записать состояние отчёта")

    def accept_metric(self, record):
        record = validate_metric(record)
        with self.lock:
            self.latest, self.last_metric = record, time.monotonic()
            record = dict(record, received_utc=utc(),
                          level=self.stage["level"] if self.stage else "baseline")
            reason = metric_threshold_reason(record)
            if reason and self.threshold_reason is None:
                # SSH может выдать накопленные строки быстрее сторожа. Первое
                # превышение сохраняется даже после следующего низкого замера
                # и между ступенями; сброса в рамках серии нет.
                self.threshold_reason = reason
                self.threshold_record = dict(record)
                self.save_state_best_effort()
            self.append("metrics.jsonl", record)

    def start_monitor(self):
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                   "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                   self.args.ssh, "python3 -u - --interval 10 --disk-path /opt/smart-platform"]
        self.monitor = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, cwd=ROOT)
        self.monitor.stdin.write((ROOT / "scripts/remote/highload-monitor.py").read_text())
        self.monitor.stdin.close()

        def read():
            try:
                for line in self.monitor.stdout:
                    self.accept_metric(json.loads(line))
                if not self.done.is_set():
                    self.monitor_error = True
            except (ValueError, OSError, TypeError):
                self.monitor_error = True

        threading.Thread(target=read, daemon=True).start()
        deadline = time.monotonic() + 25
        while not self.latest:
            if self.monitor_error or time.monotonic() >= deadline:
                raise RuntimeError("SSH baseline недоступен")
            time.sleep(0.1)
        return not (self.threshold_reason or metric_threshold_reason(self.latest))

    def interrupt_child(self):
        child = self.child
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)
            except ProcessLookupError:
                pass

    def stop_stage(self, reason):
        with self.lock:
            if not self.stage or self.stage.get("stop_reason"):
                return
            stage = self.stage
            stage["stop_reason"] = reason
            stage["stop_requested_utc"] = utc()
            try:
                self.interrupt_child()
            except (OSError, subprocess.SubprocessError):
                self.failures.append("Завершение управляющего процесса не подтверждено")
            self.save_state_best_effort()
        try:
            self.control.emergency_stop_publisher()
            with self.lock:
                stage["publisher_stopped_utc"] = utc()
                stage["elapsed_s"] = round(time.monotonic() - stage["started_monotonic"], 3)
                stage["publisher_stop_confirmed"] = True
                if stage["elapsed_s"] > self.args.duration:
                    self.failures.append("Остановка публикаций подтверждена позже лимита ступени")
                self.save_state_best_effort()
        except Exception:
            with self.lock:
                self.failures.append("Аварийная остановка PubMQTT не подтверждена")
                stage["publisher_stop_confirmed"] = False
                self.save_state_best_effort()
        finally:
            self.stop_completed.set()

    def watchdog(self):
        while not self.done.wait(0.1):
            with self.lock:
                stage = self.stage
                if not stage or stage.get("stop_reason"):
                    continue
                now = time.monotonic()
                reason = self.threshold_reason or metric_threshold_reason(self.latest)
                if not reason:
                    if self.monitor_error or now - self.last_metric > 25:
                        reason = "monitoring_lost"
                    elif now >= stage["deadline_monotonic"]:
                        reason = "duration_limit"
                    elif stage.get("api_last_monotonic") and now - stage["api_last_monotonic"] > 25:
                        reason = "api_monitoring_lost"
            if reason:
                self.stop_stage(reason)

    def prepare(self, devices, signals):
        scenario = self.control.prepare(devices, signals, 1000)
        self.control.save(scenario)
        return scenario

    def setup_services(self, scenario):
        items = self.control.containers()
        if any(item["State"]["Running"] for item in items.values()):
            raise RuntimeError("Начальный highload должен быть остановлен")
        env = self.control.compose_env(scenario, items)
        # Прежние контейнеры были из другой рабочей копии; не читаем её файлы.
        # Пересоздание привязывает только текущие конфиги, сохраняя токен в env.
        for service in ("mosquitto-local", "sp-agent"):
            self.control.run(self.control.compose + ["up", "--wait", "--wait-timeout", "60",
                             "--force-recreate", "--no-deps", "--pull", "never", service], env=env)

    def validate_devices(self, expected):
        records = self.api.devices()
        gateway = records.get("highload-mac-20261114", {}).get("id", {}).get("id")
        if not gateway:
            raise RuntimeError("Не найден существующий тестовый шлюз")
        for name in expected:
            if name in records:
                owner = records[name].get("additionalInfo", {}).get("lastConnectedGateway")
                if owner != gateway:
                    raise RuntimeError("Конфликт имени тестового устройства")

    def run_stage(self, scenario):
        if self.threshold_reason or metric_threshold_reason(self.latest):
            self.final_reason = "baseline_threshold"
            return False
        expected = expected_topics(scenario)
        self.validate_devices(expected)
        if self.monitor_error or time.monotonic() - self.last_metric > 25:
            raise RuntimeError("Мониторинг потерян перед запуском ступени")
        if self.threshold_reason or metric_threshold_reason(self.latest):
            self.final_reason = "baseline_threshold"
            return False
        now = time.monotonic()
        with self.lock:
            self.stage = {"level": f"{scenario.devices}x{scenario.signals}", "devices": scenario.devices,
                          "signals_per_device": scenario.signals, "period_ms": 1000,
                          "requested_publications_s": scenario.total, "started_utc": utc(),
                          "started_monotonic": now,
                          # Резерв 10 секунд для остановки Docker входит в лимит.
                          "deadline_monotonic": now + max(0, self.args.duration - 10),
                          "api_last_monotonic": now, "scenario_sha256": scenario.sha256,
                          "stop_reason": None}
            self.write_state()
            self.stop_completed.clear()
            self.child = subprocess.Popen([sys.executable, str(ROOT / "scripts/local/highload.py"), "start"],
                                           cwd=ROOT, env=self.control.env, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, start_new_session=True)
        started_ms = int(time.time() * 1000)
        previous = {}
        last_progress = {}
        next_api = time.monotonic()
        while not self.stage.get("stop_reason"):
            if self.child.poll() is not None and self.child.returncode != 0:
                self.stop_stage("generator_start_failed")
                break
            if time.monotonic() >= next_api:
                try:
                    snapshot = self.api.snapshot(expected)
                    summary, current = delivery_summary(snapshot, expected, started_ms,
                                                        int(time.time() * 1000), previous)
                    for key, timestamp in current.items():
                        if timestamp > previous.get(key, 0):
                            last_progress[key] = time.monotonic()
                    previous = current
                    with self.lock:
                        self.stage["api_last_monotonic"] = time.monotonic()
                        self.stage["delivery"] = summary
                        self.append("delivery.jsonl", dict(summary, timestamp_utc=utc(), level=self.stage["level"]))
                        self.write_state()
                    age = time.monotonic() - now
                    if age > 60 and summary["missing_fresh_keys"]:
                        self.stop_stage("incomplete_delivery")
                    elif age > 30 and (summary["missing_witnesses"]
                          or summary["max_witness_age_ms"] is None
                          or summary["max_witness_age_ms"] > 30000
                          or any(time.monotonic() - last_progress.get(key, now) > 30 for key in current)):
                        self.stop_stage("delivery_stalled")
                except Exception:
                    self.stop_stage("api_monitoring_failed")
                next_api = time.monotonic() + 10
            time.sleep(0.1)
        # watchdog мог ещё выполнять Docker stop; не объявляем завершение раньше.
        self.stop_completed.wait(timeout=120)
        try:
            self.interrupt_child()
        except (OSError, subprocess.SubprocessError):
            self.failures.append("Завершение управляющего процесса не подтверждено")
        with self.lock:
            stage = dict(self.stage)
            self.stages.append(stage)
            self.final_reason = stage["stop_reason"]
            self.stage = None
            self.write_state()
        self.report()
        print(f"{stage['level']}: {stage['stop_reason']}, {stage.get('elapsed_s', 0):.1f} с", flush=True)
        if not stage.get("publisher_stop_confirmed"):
            raise RuntimeError("Остановка публикаций не подтверждена")
        return stage["stop_reason"] == "duration_limit" and not self.failures

    def report(self):
        metrics = []
        path = self.directory / "metrics.jsonl"
        if path.exists():
            metrics = [json.loads(line) for line in path.read_text().splitlines()]
        lines = ["# Ступенчатый нагрузочный тест Смарт Платформы", "",
                 f"Начало UTC: {self.started}. Стенд: 192.168.10.107, SSH: {self.args.ssh}.", "",
                 "Генератор работает на Mac. Период сигналов — 1000 мс. CPU и ОЗУ всей Ubuntu "
                 "измеряются через SSH каждые 10 секунд. Порог остановки — первое CPU ≥65% или ОЗУ ≥65%. "
                 "RAM вычисляется через MemAvailable; iowait в CPU busy не входит и записывается отдельно.", "",
                 "Каждая ступень ограничена 600 секундами от запуска управляющей команды, включая запуск контейнера и локальную проверку. "
                 "Stop запрашивается за 10 секунд до лимита, чтобы включить задержку Docker в эти 600 секунд. "
                 "Время отправки stop и подтверждения остановки фиксируется отдельно. Потеря мониторинга, "
                 "ошибка API, остановка генератора или неполная доставка прекращают серию.", "",
                 "| Ступень | Задано публикаций/с | Длительность, с | Замеры | CPU сред./p95/max, % | "
                 "ОЗУ сред./max, % | Свежие ключи | Причина остановки |", "|---|---:|---:|---:|---|---|---|---|"]
        for stage in self.stages:
            rows = [r for r in metrics if r["level"] == stage["level"]]
            cpus = [r["cpu_percent"] for r in rows]
            ram = [r["memory_percent"] for r in rows]
            cpu_text = (f"{statistics.mean(cpus):.2f}/{percentile(cpus, .95):.2f}/{max(cpus):.2f}" if cpus else "нет данных")
            ram_text = f"{statistics.mean(ram):.2f}/{max(ram):.2f}" if ram else "нет данных"
            delivery = stage.get("delivery", {})
            lines.append(f"| {stage['level']} | {stage['requested_publications_s']} | {stage.get('elapsed_s', 0):.2f} | "
                         f"{len(rows)} | {cpu_text} | {ram_text} | {delivery.get('fresh_keys', 0)}/{delivery.get('expected_keys', '?')} | {stage['stop_reason']} |")
        lines += ["", f"Итоговая причина завершения: `{self.final_reason}`.", "",
                  "Заданная частота — частота PubMQTT, а не измеренная скорость записи в БД. "
                  "Agent применяет апертуру и отправку пакетами; total_topic=5000 не изменялся. "
                  "Проверка API учитывает все активные ключи и timestamp saw3/saw4 каждого устройства. "
                  "Начальная доставка получает до 60 секунд, живость — допустимый возраст до 30 секунд. "
                  "Неизменность значения сигнала или статус-кода не считается отсутствием доставки.", "",
                  "Полные временные ряды ресурсов — metrics.jsonl; санитизированные счётчики доставки — delivery.jsonl; "
                  "моменты запуска и остановки — state.json. Значения телеметрии, пароли и токены в отчёт не включаются.", "",
                  "Проверка не определяет промышленную производительность и не измеряет скорость записи БД. "
                  "При остановке до достижения 65% предел CPU/ОЗУ не установлен."]
        if self.failures:
            lines += ["", "Проблемы: " + "; ".join(self.failures)]
        (self.directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run(self):
        print("REPORT_DIR=" + str(self.directory), flush=True)
        try:
            self.api.authenticate(use_supply_defaults=self.args.use_supply_defaults)
            if not self.start_monitor():
                self.final_reason = "baseline_threshold"
            else:
                threading.Thread(target=self.watchdog, daemon=True).start()
                initial = True
                for devices, signals in levels():
                    if self.threshold_reason or metric_threshold_reason(self.latest):
                        self.final_reason = "baseline_threshold"
                        break
                    if self.monitor_error or time.monotonic() - self.last_metric > 25:
                        raise RuntimeError("Мониторинг потерян между ступенями")
                    print(f"Подготовка {devices}x{signals}; публикации остановлены", flush=True)
                    scenario = self.prepare(devices, signals)
                    if initial:
                        self.setup_services(scenario)
                        initial = False
                    if not self.run_stage(scenario):
                        break
        except KeyboardInterrupt:
            self.final_reason = "interrupted"
            self.stop_stage("interrupted")
        except Exception as error:
            # Исключения subprocess/HTTP могут содержать credentials; не выводим их.
            self.final_reason = "execution_failed"
            self.failures.append("Ошибка выполнения: " + type(error).__name__)
            self.stop_stage("execution_failed")
        finally:
            self.done.set()
            try:
                self.interrupt_child()
            except (OSError, subprocess.SubprocessError):
                self.failures.append("Завершение управляющего процесса не подтверждено")
            try:
                self.control.emergency_stop_publisher()
            except Exception:
                self.failures.append("Независимая финальная остановка PubMQTT не подтверждена")
            try:
                self.control.stop()
            except Exception:
                # Остановка не должна зависеть от читаемости XLSX/state.json.
                try:
                    items = self.control.containers()
                    identifiers = [item["Id"] for item in items.values()
                                   if item["State"]["Running"] or item["State"].get("Restarting")]
                    if identifiers:
                        self.control.run(["docker", "stop", "--time", "0", *identifiers], timeout=15)
                except Exception:
                    self.failures.append("Резервная остановка highload не подтверждена")
            try:
                items = self.control.containers()
                if any(item["State"]["Running"] or item["State"].get("Restarting") for item in items.values()):
                    raise RuntimeError("Контейнеры не остановлены")
            except Exception:
                self.failures.append("Финальная остановка highload не подтверждена")
            if self.monitor is not None:
                self.monitor.terminate()
                try:
                    self.monitor.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.monitor.kill()
            self.write_state()
            self.report()
        normal_reasons = {"cpu_threshold", "memory_threshold", "baseline_threshold"}
        return 0 if not self.failures and self.final_reason in normal_reasons else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh", default="irobo")
    parser.add_argument("--url", default="http://192.168.10.107:8080")
    parser.add_argument("--use-supply-defaults", action="store_true")
    parser.add_argument("--duration", type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.duration <= 600:
        parser.error("Длительность должна быть от 1 до 600 секунд")
    if not args.ssh.replace("-", "").replace("_", "").isalnum() or args.ssh.startswith("-"):
        parser.error("Нужен SSH alias без параметров")
    return Staircase(args).run()


if __name__ == "__main__":
    sys.exit(main())
