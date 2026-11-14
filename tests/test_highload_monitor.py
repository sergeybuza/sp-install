"""Разбор Linux-метрик и отказ при недостоверных данных, без чтения /proc."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "highload_monitor", ROOT / "scripts/remote/highload-monitor.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


CPU_BEFORE = "cpu 100 10 20 500 30 5 5 0 40 2\ncpu0 1 1 1 1 1 1 1 1\n"
CPU_AFTER = "cpu 500 10 220 750 130 30 30 0 200 5\n"
MEMORY = """MemTotal: 1000 kB
MemFree: 10 kB
MemAvailable: 350 kB
Cached: 340 kB
SwapTotal: 200 kB
SwapFree: 150 kB
"""


class CpuMetricsTests(unittest.TestCase):
    def test_aggregate_is_normalized_and_guest_is_not_counted_twice(self):
        result = monitor.cpu_metrics(monitor.parse_cpu(CPU_BEFORE),
                                     monitor.parse_cpu(CPU_AFTER))
        self.assertEqual(result["cpu_total_ticks_delta"], 1000)
        self.assertEqual(result["cpu_percent"], 65.0)
        self.assertEqual(result["cpu_idle_percent"], 25.0)
        self.assertEqual(result["cpu_iowait_percent"], 10.0)

    def test_all_busy_and_all_idle(self):
        zero = (0,) * 8
        self.assertEqual(monitor.cpu_metrics(zero, (400, 0, 0, 0, 0, 0, 0, 0))["cpu_percent"], 100)
        self.assertEqual(monitor.cpu_metrics(zero, (0, 0, 0, 400, 0, 0, 0, 0))["cpu_percent"], 0)

    def test_missing_short_duplicate_noninteger_negative_cpu_are_rejected(self):
        cases = ("cpu0 1 2 3 4 5 6 7 8\n", "cpu 1 2 3 4\n",
                 CPU_BEFORE + CPU_AFTER, "cpu 1 2 3 4 5 6 7 invalid\n",
                 "cpu 1 2 3 4 -1 6 7 8\n")
        for text in cases:
            with self.subTest(text=text), self.assertRaises(monitor.MetricError):
                monitor.parse_cpu(text)

    def test_counter_reset_stall_and_incomplete_delta_are_rejected(self):
        cases = (((10,) * 8, (9,) * 8), ((0,) * 8, (0,) * 8), ((0,), (1,)))
        for before, after in cases:
            with self.subTest(before=before), self.assertRaises(monitor.MetricError):
                monitor.cpu_metrics(before, after)


class MemoryMetricsTests(unittest.TestCase):
    def test_ram_uses_available_instead_of_free_and_converts_kib(self):
        result = monitor.parse_memory(MEMORY)
        self.assertEqual(result["memory_percent"], 65)
        self.assertEqual(result["memory_total_bytes"], 1000 * 1024)
        self.assertEqual(result["memory_available_bytes"], 350 * 1024)
        self.assertEqual(result["swap_total_bytes"], 200 * 1024)
        self.assertEqual(result["swap_used_bytes"], 50 * 1024)

    def test_no_swap_is_valid(self):
        result = monitor.parse_memory(MEMORY.replace("200 kB", "0 kB").replace("150 kB", "0 kB"))
        self.assertEqual(result["swap_used_bytes"], 0)

    def test_missing_duplicate_wrong_unit_and_invalid_numbers_are_rejected(self):
        cases = (MEMORY.replace("MemAvailable: 350 kB\n", ""),
                 MEMORY + "MemTotal: 1000 kB\n", MEMORY.replace("350 kB", "350 MB"),
                 MEMORY.replace("350 kB", "NaN kB"), MEMORY.replace("350 kB", "-1 kB"))
        for text in cases:
            with self.subTest(text=text), self.assertRaises(monitor.MetricError):
                monitor.parse_memory(text)

    def test_inconsistent_memory_and_swap_are_rejected(self):
        cases = (MEMORY.replace("1000 kB", "0 kB"),
                 MEMORY.replace("350 kB", "1001 kB"), MEMORY.replace("150 kB", "201 kB"))
        for text in cases:
            with self.subTest(text=text), self.assertRaises(monitor.MetricError):
                monitor.parse_memory(text)


class DiskMetricsTests(unittest.TestCase):
    def test_available_space_excludes_reserved_blocks(self):
        result = monitor.disk_metrics(SimpleNamespace(
            f_frsize=1024, f_bsize=4096, f_blocks=1000, f_bfree=100, f_bavail=80))
        self.assertEqual(result, {"disk_total_bytes": 1024000,
                                  "disk_free_bytes": 102400,
                                  "disk_available_bytes": 81920})

    def test_zero_fragment_size_uses_block_size(self):
        result = monitor.disk_metrics(SimpleNamespace(
            f_frsize=0, f_bsize=4096, f_blocks=1000, f_bfree=100, f_bavail=0))
        self.assertEqual(result["disk_available_bytes"], 0)
        self.assertEqual(result["disk_total_bytes"], 4096000)

    def test_invalid_disk_counters_are_rejected(self):
        for blocks, free, available in ((0, 0, 0), (100, 101, 80), (100, 80, 81), (100, 80, -1)):
            with self.subTest(blocks=blocks, free=free, available=available), \
                    self.assertRaises(monitor.MetricError):
                monitor.disk_metrics(SimpleNamespace(
                    f_frsize=1024, f_bsize=4096, f_blocks=blocks, f_bfree=free, f_bavail=available))


class MetricFailureTests(unittest.TestCase):
    def test_unavailable_or_invalid_metrics_exit_nonzero_without_json(self):
        for error in (OSError("источник недоступен"), monitor.MetricError("метрика недостоверна")):
            output, errors = io.StringIO(), io.StringIO()
            with self.subTest(error=type(error).__name__), \
                    patch.object(monitor, "monitor", side_effect=error), \
                    redirect_stdout(output), redirect_stderr(errors):
                result = monitor.main(["--disk-path", "fixture-filesystem"])
            self.assertEqual(result, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("STOP:", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
