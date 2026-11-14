#!/usr/bin/env python3
"""На Linux-стенде: только чтение CPU/RAM/swap/диска, поток JSONL в stdout.

Не требует sudo, не устанавливает пакеты и не пишет файлы. Первая запись
появляется после полного интервала. CPU нормирован на всю машину; ожидание
I/O не включено в cpu_percent и публикуется отдельно. Порог и остановку
генератора обеспечивает вызывающий контроллер, а не этот монитор.
"""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time


class MetricError(ValueError):
    """Нельзя получить достоверное измерение; продолжать тест нельзя."""


def parse_cpu(text):
    """Первые восемь счётчиков общей строки cpu, без guest/guest_nice."""
    rows = [line.split() for line in text.splitlines()
            if line.split() and line.split()[0] == "cpu"]
    if len(rows) != 1 or len(rows[0]) < 9:
        raise MetricError("Нет единственной полной агрегированной строки CPU")
    try:
        counters = tuple(int(value) for value in rows[0][1:])
    except ValueError as error:
        raise MetricError("Некорректные счётчики CPU") from error
    if any(value < 0 for value in counters):
        raise MetricError("Отрицательные счётчики CPU")
    # guest уже входит в user, guest_nice — в nice.
    return counters[:8]


def cpu_metrics(previous, current):
    if len(previous) != 8 or len(current) != 8:
        raise MetricError("Неполный набор счётчиков CPU")
    delta = tuple(new - old for old, new in zip(previous, current))
    if any(value < 0 for value in delta):
        raise MetricError("Счётчики CPU уменьшились: интервал недостоверен")
    total = sum(delta)
    if total <= 0:
        raise MetricError("Счётчики CPU не продвинулись")
    idle, iowait = delta[3], delta[4]
    return {
        "cpu_percent": 100.0 * (total - idle - iowait) / total,
        "cpu_idle_percent": 100.0 * idle / total,
        "cpu_iowait_percent": 100.0 * iowait / total,
        "cpu_total_ticks_delta": total,
    }


def parse_memory(text):
    required = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    values = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0].rstrip(":") not in required:
            continue
        key = fields[0].rstrip(":")
        if key in values or fields[0] != key + ":" or len(fields) != 3 or fields[2] != "kB":
            raise MetricError("Некорректное поле памяти " + key)
        try:
            value = int(fields[1])
        except ValueError as error:
            raise MetricError("Некорректное значение памяти " + key) from error
        if value < 0:
            raise MetricError("Отрицательное значение памяти " + key)
        values[key] = value * 1024
    if values.keys() != required:
        raise MetricError("Не хватает обязательных полей RAM/swap")
    total, available = values["MemTotal"], values["MemAvailable"]
    swap_total, swap_free = values["SwapTotal"], values["SwapFree"]
    if total <= 0 or available > total or swap_free > swap_total:
        raise MetricError("Несогласованные значения RAM/swap")
    return {
        "memory_percent": 100.0 * (total - available) / total,
        "memory_total_bytes": total,
        "memory_available_bytes": available,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_total - swap_free,
    }


def disk_metrics(filesystem):
    block_size = filesystem.f_frsize or filesystem.f_bsize
    total, free, available = (filesystem.f_blocks, filesystem.f_bfree,
                              filesystem.f_bavail)
    if block_size <= 0 or total <= 0 or not 0 <= available <= free <= total:
        raise MetricError("Некорректные показатели файловой системы")
    return {
        "disk_total_bytes": total * block_size,
        "disk_free_bytes": free * block_size,
        "disk_available_bytes": available * block_size,
    }


def positive_interval(value):
    try:
        interval = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Нужен положительный интервал в секундах") from error
    if not math.isfinite(interval) or interval <= 0:
        raise argparse.ArgumentTypeError("Нужен конечный положительный интервал")
    return interval


def monitor(interval, disk_path, proc_root):
    cpu_path = proc_root / "stat"
    memory_path = proc_root / "meminfo"
    previous = parse_cpu(cpu_path.read_text(encoding="ascii"))
    previous_time = time.monotonic()
    # Проверяем доступность всех источников до ожидания первого измерения.
    parse_memory(memory_path.read_text(encoding="ascii"))
    disk_metrics(os.statvfs(disk_path))
    while True:
        time.sleep(max(0.0, previous_time + interval - time.monotonic()))
        current = parse_cpu(cpu_path.read_text(encoding="ascii"))
        current_time = time.monotonic()
        actual_interval = current_time - previous_time
        if not math.isfinite(actual_interval) or actual_interval <= 0:
            raise MetricError("Недостоверный монотонный интервал измерения")
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "interval_s": actual_interval,
            **cpu_metrics(previous, current),
            **parse_memory(memory_path.read_text(encoding="ascii")),
            **disk_metrics(os.statvfs(disk_path)),
        }
        print(json.dumps(record, allow_nan=False, separators=(",", ":")), flush=True)
        previous, previous_time = current, current_time


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=positive_interval, default=10.0,
                        help="Интервал измерений в секундах (по умолчанию 10)")
    parser.add_argument("--disk-path", required=True,
                        help="Каталог на проверяемой файловой системе стенда")
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"),
                        help="Каталог Linux procfs (по умолчанию /proc)")
    arguments = parser.parse_args(argv)
    try:
        monitor(arguments.interval, arguments.disk_path, arguments.proc_root)
    except (OSError, UnicodeError, MetricError) as error:
        print("STOP: мониторинг недоступен: " + str(error), file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
