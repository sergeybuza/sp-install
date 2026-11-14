"""Контракты метрик, доставки и независимой остановки ступеней; без сети/Docker."""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "highload_staircase", ROOT / "scripts/local/run-highload-staircase.py")
staircase = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staircase)


class OneCheck:
    """Один оборот сторожа без ожидания реального времени."""
    def __init__(self):
        self.calls = 0

    def wait(self, timeout):
        self.calls += 1
        return self.calls > 1


def runner():
    result = staircase.Staircase.__new__(staircase.Staircase)
    result.args = SimpleNamespace(duration=600, ssh="fixture", use_supply_defaults=False)
    result.lock = threading.RLock()
    result.stage = {"level": "30x150", "started_monotonic": 10.0,
                    "deadline_monotonic": 700.0, "api_last_monotonic": 100.0,
                    "stop_reason": None}
    result.last_metric = 100.0
    result.latest = {"cpu_percent": 10.0, "memory_percent": 20.0}
    result.threshold_reason = None
    result.threshold_record = None
    result.monitor_error = False
    result.done = OneCheck()
    result.stop_completed = threading.Event()
    result.control = Mock()
    result.api = Mock()
    result.failures = []
    result.stages = []
    result.monitor = None
    result.child = None
    result.directory = ROOT / "reports/highload-staircase-fixture"
    result.interrupt_child = Mock()
    result.write_state = Mock()
    result.append = Mock()
    result.report = Mock()
    result.final_reason = "preflight"
    return result


class MetricTests(unittest.TestCase):
    def test_machine_percentages_include_threshold_and_valid_zeroes(self):
        for cpu, memory in ((0, 0), (65, 10), (10, 65), (100, 100)):
            record = {"cpu_percent": cpu, "memory_percent": memory, "interval_s": 10.01}
            validated = staircase.validate_metric(record)
            self.assertEqual(validated["cpu_percent"], cpu)
            self.assertEqual(validated["memory_percent"], memory)

    def test_arbitrary_ssh_output_fields_are_not_persisted_as_metrics(self):
        record = {"cpu_percent": 10, "memory_percent": 20, "interval_s": 10,
                  "unexpected_credentials": "fixture-sensitive-value"}
        validated = staircase.validate_metric(record)
        self.assertNotIn("unexpected_credentials", validated)
        self.assertNotIn("fixture-sensitive-value", repr(validated))

    def test_invalid_metrics_fail_closed(self):
        base = {"cpu_percent": 10.0, "memory_percent": 20.0, "interval_s": 10.0}
        for field, value in (("cpu_percent", True), ("memory_percent", "65"),
                             ("cpu_percent", float("nan")), ("memory_percent", float("inf")),
                             ("cpu_percent", -0.1), ("memory_percent", 100.1),
                             ("interval_s", 0), ("interval_s", 45), ("interval_s", None)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                staircase.validate_metric(dict(base, **{field: value}))
        with self.assertRaises(ValueError):
            staircase.validate_metric({})
        for invalid in (None, [], "not a measurement", 10):
            with self.subTest(record=invalid), self.assertRaises(ValueError):
                staircase.validate_metric(invalid)


class DeliveryTests(unittest.TestCase):
    def test_only_selected_keys_and_post_start_timestamps_count_toward_coverage(self):
        expected = {"device1": {"param_saw3", "param_rand1"}}
        snapshot = {"device1": {"param_saw3": 1100, "param_rand1": 999,
                                "old_signal": 1200, "param_saw3_status_code": 1200},
                    "old_device": {"param_saw3": 1200}}
        summary, _ = staircase.delivery_summary(snapshot, expected, 1000, 1500, {})
        self.assertEqual(summary["expected_keys"], 2)
        self.assertEqual(summary["fresh_keys"], 1)
        self.assertEqual(summary["missing_fresh_keys"], 1)
        self.assertEqual(summary["witnesses"], 1)

    def test_missing_device_is_detected_even_when_other_devices_are_live(self):
        expected = {"device1": {"param_saw3"}, "device2": {"param_saw3", "param_saw4"}}
        summary, current = staircase.delivery_summary(
            {"device1": {"param_saw3": 1500}}, expected, 1000, 1600, {})
        self.assertEqual(summary["missing_fresh_keys"], 2)
        self.assertEqual(summary["missing_witnesses"], 2)
        self.assertEqual(len(current), 3)

    def test_witness_liveness_uses_timestamp_progress_and_allows_other_keys_to_stay_constant(self):
        expected = {"device1": {"param_saw3", "param_saw4", "param_rand1"}}
        first = {"device1": {"param_saw3": 2000, "param_saw4": 2000, "param_rand1": 1000}}
        _, previous = staircase.delivery_summary(first, expected, 1000, 2500, {})
        second = {"device1": {"param_saw3": 12000, "param_saw4": 12000, "param_rand1": 1000}}
        summary, current = staircase.delivery_summary(second, expected, 1000, 12500, previous)
        self.assertEqual(summary["progressed_witnesses"], 2)
        self.assertEqual(summary["missing_fresh_keys"], 0)
        self.assertEqual(summary["max_witness_age_ms"], 500)
        frozen, _ = staircase.delivery_summary(second, expected, 1000, 45000, current)
        self.assertEqual(frozen["progressed_witnesses"], 0)
        self.assertEqual(frozen["max_witness_age_ms"], 33000)

    def test_scenario_without_liveness_witnesses_cannot_pass(self):
        with self.assertRaises(ValueError):
            staircase.delivery_summary({"device1": {"random": 1500}},
                                       {"device1": {"random"}}, 1000, 1600, {})

    def test_future_witness_timestamp_cannot_hide_frozen_delivery(self):
        with self.assertRaises(ValueError):
            staircase.delivery_summary({"device1": {"param_saw3": 1000000}},
                                       {"device1": {"param_saw3"}}, 1000, 1600, {})


class GuardTests(unittest.TestCase):
    def test_backlogged_threshold_then_low_sample_cannot_hide_first_threshold(self):
        subject = runner()
        subject.stop_stage = Mock()
        with patch.object(staircase.time, "monotonic", return_value=100.0):
            subject.accept_metric({"cpu_percent": 65.1, "memory_percent": 20.0, "interval_s": 10.0})
            subject.accept_metric({"cpu_percent": 64.0, "memory_percent": 20.0, "interval_s": 10.0})
            subject.watchdog()
        self.assertEqual(subject.latest["cpu_percent"], 64.0)
        self.assertEqual(subject.threshold_record["cpu_percent"], 65.1)
        self.assertEqual(subject.threshold_record["level"], "30x150")
        subject.stop_stage.assert_called_once_with("cpu_threshold")

    def test_first_memory_threshold_is_not_replaced_by_later_cpu_threshold(self):
        subject = runner()
        subject.stop_stage = Mock()
        with patch.object(staircase.time, "monotonic", return_value=100.0):
            subject.accept_metric({"cpu_percent": 10.0, "memory_percent": 65.1, "interval_s": 10.0})
            subject.accept_metric({"cpu_percent": 66.0, "memory_percent": 20.0, "interval_s": 10.0})
            subject.watchdog()
        self.assertEqual(subject.threshold_record["memory_percent"], 65.1)
        subject.stop_stage.assert_called_once_with("memory_threshold")

    def test_between_stage_threshold_prevents_next_start_even_after_low_sample(self):
        subject = runner()
        subject.stage = None
        subject.validate_devices = Mock()
        subject.accept_metric({"cpu_percent": 65.1, "memory_percent": 20.0, "interval_s": 10.0})
        subject.accept_metric({"cpu_percent": 10.0, "memory_percent": 20.0, "interval_s": 10.0})
        self.assertFalse(subject.run_stage(SimpleNamespace()))
        self.assertEqual(subject.final_reason, "baseline_threshold")
        self.assertEqual(subject.threshold_record["level"], "baseline")
        subject.validate_devices.assert_not_called()

    def test_guard_stops_at_first_threshold_independently_of_api_and_child_completion(self):
        for field, reason in (("cpu_percent", "cpu_threshold"),
                              ("memory_percent", "memory_threshold")):
            with self.subTest(field=field):
                subject = runner()
                subject.latest[field] = 65
                subject.child = Mock()
                subject.child.poll.return_value = None
                subject.stop_stage = Mock()
                with patch.object(staircase.time, "monotonic", return_value=100.0):
                    subject.watchdog()
                subject.stop_stage.assert_called_once_with(reason)
                subject.api.snapshot.assert_not_called()
                subject.child.wait.assert_not_called()

    def test_guard_checks_duration_and_loss_of_resource_or_api_monitoring(self):
        for change, reason in (({"deadline_monotonic": 100.0}, "duration_limit"),
                               ({"last_metric": 74.9}, "monitoring_lost"),
                               ({"monitor_error": True}, "monitoring_lost"),
                               ({"api_last_monotonic": 74.9}, "api_monitoring_lost")):
            with self.subTest(change=change):
                subject = runner()
                for key, value in change.items():
                    if key in {"last_metric", "monitor_error"}:
                        setattr(subject, key, value)
                    else:
                        subject.stage[key] = value
                subject.stop_stage = Mock()
                with patch.object(staircase.time, "monotonic", return_value=100.0):
                    subject.watchdog()
                subject.stop_stage.assert_called_once_with(reason)

    def test_healthy_subthreshold_stage_is_not_stopped(self):
        subject = runner()
        subject.latest = {"cpu_percent": 64.999, "memory_percent": 64.999}
        subject.stop_stage = Mock()
        with patch.object(staircase.time, "monotonic", return_value=100.0):
            subject.watchdog()
        subject.stop_stage.assert_not_called()

    def test_failed_child_cleanup_does_not_skip_emergency_publisher_stop(self):
        subject = runner()
        subject.interrupt_child.side_effect = subprocess.TimeoutExpired("fixture child", 2)
        with redirect_stdout(io.StringIO()):
            subject.stop_stage("cpu_threshold")
        subject.control.emergency_stop_publisher.assert_called_once()
        self.assertTrue(subject.stop_completed.is_set())

    def test_failed_state_write_does_not_skip_emergency_publisher_stop(self):
        subject = runner()
        subject.write_state.side_effect = OSError("fixture full disk")
        with redirect_stdout(io.StringIO()):
            subject.stop_stage("cpu_threshold")
        subject.control.emergency_stop_publisher.assert_called_once()
        self.assertTrue(subject.stop_completed.is_set())

    def test_stop_confirmation_belongs_to_original_stage_after_stage_reference_changes(self):
        subject = runner()
        original = subject.stage
        later = {"level": "40x200", "stop_reason": None}
        subject.control.emergency_stop_publisher.side_effect = lambda: setattr(subject, "stage", later)
        with patch.object(staircase.time, "monotonic", return_value=100.0):
            subject.stop_stage("cpu_threshold")
        self.assertTrue(original.get("publisher_stop_confirmed"))
        self.assertNotIn("publisher_stop_confirmed", later)

    def test_final_full_stop_is_attempted_even_if_emergency_stop_fails(self):
        subject = runner()
        subject.stage = None
        subject.done = threading.Event()
        subject.api.authenticate.side_effect = RuntimeError("fixture failure")
        subject.control.emergency_stop_publisher.side_effect = RuntimeError("fixture failure")
        subject.control.containers.return_value = {}
        with redirect_stdout(io.StringIO()):
            self.assertEqual(subject.run(), 1)
        subject.control.stop.assert_called_once()

    def test_duration_overrun_does_not_authorize_another_stage(self):
        subject = runner()
        subject.validate_devices = Mock()
        subject.control.env = {}
        scenario = SimpleNamespace(devices=30, signals=150, total=4500, sha256="fixture-hash",
                                   topics={"/devices/device1/controls/param_saw3"})
        clock = [100.0]

        def snapshot(expected):
            # Порог времени сработал, но остановка завершилась после 600 секунд.
            clock[0] = 701.0
            subject.stop_stage("duration_limit")
            return {"device1": {"param_saw3": 100000}}

        subject.api.snapshot.side_effect = snapshot
        child = Mock(returncode=0)
        child.poll.return_value = 0
        with patch.object(staircase.subprocess, "Popen", return_value=child), \
                patch.object(staircase.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(staircase.time, "time", return_value=100.0), \
                patch.object(staircase.time, "sleep"), redirect_stdout(io.StringIO()):
            self.assertFalse(subject.run_stage(scenario))
        self.assertTrue(subject.failures)
        self.assertEqual(subject.stages[0]["elapsed_s"], 601.0)


class FinalStatusTests(unittest.TestCase):
    def ready_runner(self):
        subject = runner()
        subject.stage = None
        subject.done = threading.Event()
        subject.start_monitor = Mock(return_value=True)
        subject.prepare = Mock(return_value=SimpleNamespace())
        subject.setup_services = Mock()
        subject.control.containers.return_value = {}
        return subject

    def test_abnormal_final_reasons_exit_nonzero_even_after_confirmed_full_stop(self):
        reasons = ("monitoring_lost", "api_monitoring_lost", "api_monitoring_failed",
                   "generator_start_failed", "incomplete_delivery", "delivery_stalled",
                   "interrupted", "execution_failed", "duration_limit")
        for reason in reasons:
            subject = self.ready_runner()

            def finish(scenario):
                subject.final_reason = reason
                return False

            subject.run_stage = Mock(side_effect=finish)
            with self.subTest(reason=reason), \
                    patch.object(staircase.threading, "Thread"), \
                    patch.object(staircase.time, "monotonic", return_value=100.0), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(subject.run(), 1)
            self.assertEqual(subject.failures, [])
            subject.control.emergency_stop_publisher.assert_called_once()
            subject.control.stop.assert_called_once()

    def test_only_normal_threshold_final_reasons_exit_zero(self):
        for reason in ("cpu_threshold", "memory_threshold", "baseline_threshold"):
            subject = self.ready_runner()

            def finish(scenario):
                subject.final_reason = reason
                return False

            subject.run_stage = Mock(side_effect=finish)
            with self.subTest(reason=reason), \
                    patch.object(staircase.threading, "Thread"), \
                    patch.object(staircase.time, "monotonic", return_value=100.0), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(subject.run(), 0)
            self.assertEqual(subject.failures, [])

    def test_latched_baseline_threshold_skips_all_preparation_and_stages(self):
        subject = self.ready_runner()
        subject.run_stage = Mock()
        subject.accept_metric({"cpu_percent": 65.1, "memory_percent": 20.0, "interval_s": 10.0})
        subject.accept_metric({"cpu_percent": 10.0, "memory_percent": 20.0, "interval_s": 10.0})
        with patch.object(staircase.threading, "Thread"), \
                patch.object(staircase.time, "monotonic", return_value=100.0), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(subject.run(), 0)
        self.assertEqual(subject.final_reason, "baseline_threshold")
        subject.prepare.assert_not_called()
        subject.run_stage.assert_not_called()

    def test_baseline_detection_returns_cleanly_and_full_stop_still_runs(self):
        subject = self.ready_runner()
        subject.start_monitor.return_value = False
        with redirect_stdout(io.StringIO()):
            self.assertEqual(subject.run(), 0)
        self.assertEqual(subject.final_reason, "baseline_threshold")
        self.assertEqual(subject.failures, [])
        subject.prepare.assert_not_called()
        subject.control.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
