"""Защита состояния, изоляции Docker и секретов утилиты highload."""
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("highload_control", ROOT / "scripts/local/highload.py")
load = importlib.util.module_from_spec(spec)
spec.loader.exec_module(load)


def fixture(path, period=1000, duplicate=False):
    ns = load.NS["s"]
    document = ET.Element(f"{{{ns}}}worksheet")
    data = ET.SubElement(document, f"{{{ns}}}sheetData")
    for number in range(1, 4):
        row = ET.SubElement(data, f"{{{ns}}}row", r=str(number))
        values = {"A": "Topic", "C": "Period", "J": "TotalNumberPerTopic"} if number == 1 else {
            "A": f"/devices/dev1/controls/p{1 if duplicate else number - 1}", "C": period, "J": 0}
        for column, value in values.items():
            cell = ET.SubElement(row, f"{{{ns}}}c", r=column + str(number))
            if isinstance(value, str):
                cell.set("t", "inlineStr")
                ET.SubElement(ET.SubElement(cell, f"{{{ns}}}is"), f"{{{ns}}}t").text = value
            else:
                ET.SubElement(cell, f"{{{ns}}}v").text = str(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", ET.tostring(document))
    return load.read_scenario(path)


def containers(scenario):
    return {service: {
        "Id": "owned-" + service, "RestartCount": 0,
        "State": {"Running": True, "Status": "running"},
        "Config": {"Env": ["AGENT_ACCESS_TOKEN=fixture-not-secret"], "Labels": {
            "com.docker.compose.project": load.PROJECT, "com.docker.compose.service": service}},
        "Mounts": [{"Destination": "/etc/pubmqtt/test_sp_local.xlsx", "Source": str(scenario.path)}]
    } for service in load.SERVICES}


def publisher(identifier="a" * 64, running=True, restarting=False):
    return {"id": identifier, "running": running, "restarting": restarting, "labels": {
        "com.docker.compose.project": load.PROJECT,
        "com.docker.compose.service": "sp-pubmqtt"}}


def publisher_json(*items):
    return "\n".join(json.dumps(item) for item in items)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "reports/highload-tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.control = load.Controller(self.root, env={})
        self.scenario = fixture(self.root / "scenario.xlsx")
        self.items = containers(self.scenario)

    def test_path_escape_is_rejected(self):
        with self.assertRaises(load.LoadError):
            load.local_path(self.root, self.root.parent / "foreign.xlsx")

    def test_duplicate_topics_are_rejected(self):
        with self.assertRaises(load.LoadError):
            fixture(self.root / "duplicate.xlsx", duplicate=True)

    def test_saved_state_has_no_token_and_detects_modified_file(self):
        self.control.save(self.scenario)
        self.assertNotIn("fixture-not-secret", self.control.state_file.read_text())
        fixture(self.scenario.path, period=2000)
        with self.assertRaises(load.LoadError):
            self.control.current(self.items)

    def test_same_level_does_not_recreate_or_regenerate(self):
        with patch.object(self.control, "containers", return_value=self.items), \
                patch.object(self.control, "prepare") as prepare, \
                patch.object(self.control, "apply") as apply, redirect_stdout(io.StringIO()):
            self.control.set_level(1, 2, None)
        prepare.assert_not_called()
        apply.assert_not_called()

    def test_failed_apply_restores_previous_without_saving_new_state(self):
        new = fixture(self.root / "new.xlsx", period=2000)
        self.control.save(self.scenario)
        saved = self.control.state_file.read_bytes()
        with patch.object(self.control, "containers", return_value=self.items), \
                patch.object(self.control, "prepare", return_value=new), \
                patch.object(self.control, "apply", side_effect=[load.LoadError("failed"), None]) as apply, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(load.LoadError):
                self.control.set_level(1, 2, 2000)
        self.assertEqual([call.args[0] for call in apply.call_args_list], [new, self.scenario])
        self.assertEqual(self.control.state_file.read_bytes(), saved)

    def test_stopped_load_only_saves_new_level(self):
        for item in self.items.values():
            item["State"]["Running"] = False
        self.control.save(self.scenario)
        new = fixture(self.root / "new.xlsx", period=2000)
        with patch.object(self.control, "containers", return_value=self.items), \
                patch.object(self.control, "prepare", return_value=new), \
                patch.object(self.control, "apply") as apply, redirect_stdout(io.StringIO()):
            self.control.set_level(1, 2, 2000)
        apply.assert_not_called()
        self.assertEqual(self.control.current(self.items).period_ms, 2000)

    def test_foreign_project_is_rejected(self):
        item = self.items["sp-agent"]
        item["Config"]["Labels"]["com.docker.compose.project"] = "another-project"
        with patch.object(self.control, "run", side_effect=["id", json.dumps([item])]):
            with self.assertRaises(load.LoadError):
                self.control.containers()

    def test_stop_only_addresses_owned_containers(self):
        with patch.object(self.control, "containers", return_value=self.items), \
                patch.object(self.control, "run") as run, redirect_stdout(io.StringIO()):
            self.control.stop()
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], [
            "owned-sp-pubmqtt", "owned-sp-agent", "owned-mosquitto-local"])

    def test_apply_passes_token_only_in_environment(self):
        with patch.object(self.control, "run") as run, patch.object(self.control, "check_local"):
            self.control.apply(self.scenario, self.items)
        for call in run.call_args_list:
            self.assertNotIn("fixture-not-secret", " ".join(call.args[0]))
            self.assertEqual(call.kwargs["env"]["AGENT_ACCESS_TOKEN"], "fixture-not-secret")

    def test_error_output_redacts_token(self):
        self.control.secrets.append("fixture-not-secret")
        completed = Mock(returncode=1, stderr="failed fixture-not-secret", stdout="")
        with patch.object(load.subprocess, "run", return_value=completed):
            with self.assertRaises(load.LoadError) as error:
                self.control.run(["docker", "version"])
        self.assertNotIn("fixture-not-secret", str(error.exception))

    def test_redaction_happens_before_output_truncation(self):
        self.control.secrets.append("fixture-not-secret")
        completed = Mock(returncode=1, stderr="x" * 995 + "fixture-not-secret", stdout="")
        with patch.object(load.subprocess, "run", return_value=completed):
            with self.assertRaises(load.LoadError) as error:
                self.control.run(["docker", "version"])
        self.assertNotIn("fixtu", str(error.exception))

    def test_local_check_requires_all_active_topics(self):
        with patch.object(self.control, "containers", return_value=self.items), \
                patch.object(self.control, "run", return_value="/devices/dev1/controls/p1 3\n"):
            with self.assertRaises(load.LoadError):
                self.control.check_local(self.scenario)

    def test_failed_generation_preserves_selection_and_cleans_tempfile(self):
        self.control.save(self.scenario)
        saved = self.control.state_file.read_bytes()
        with patch.object(self.control, "node", return_value="configured-node"), \
                patch.object(self.control, "run", side_effect=load.LoadError("generation failed")):
            with self.assertRaises(load.LoadError):
                self.control.prepare(2, 3, 2000)
        self.assertEqual(saved, self.control.state_file.read_bytes())
        self.assertEqual(list((self.control.state_dir / "scenarios").iterdir()), [])

    def test_emergency_stop_scopes_and_verifies_publisher_without_reading_environment(self):
        state = publisher()
        stopped = publisher(running=False)
        responses = ["a" * 12, publisher_json(state), "", "a" * 12, publisher_json(stopped)]
        output = io.StringIO()
        with patch.object(self.control, "run", side_effect=responses) as run, \
                patch.object(self.control, "containers", side_effect=AssertionError("full inspect")), \
                patch.object(self.control, "current", side_effect=AssertionError("scenario read")), \
                redirect_stdout(output):
            self.control.emergency_stop_publisher()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0], ["docker", "ps", "-aq", "--filter",
            "label=com.docker.compose.project=" + load.PROJECT, "--filter",
            "label=com.docker.compose.service=sp-pubmqtt"])
        self.assertEqual(commands[2], ["docker", "stop", "--time", "0", "a" * 64])
        self.assertEqual(commands[1][0:3], ["docker", "inspect", "--format"])
        self.assertNotIn("Env", commands[1][3])
        self.assertIn("Agent", output.getvalue())

    def test_emergency_stop_validates_all_labels_before_mutation(self):
        for label, foreign in (("com.docker.compose.project", "other-project"),
                               ("com.docker.compose.service", "sp-agent")):
            with self.subTest(label=label):
                foreign_item = publisher("b" * 64)
                foreign_item["labels"][label] = foreign
                with patch.object(self.control, "run", side_effect=["a" * 12 + "\n" + "b" * 12,
                        publisher_json(publisher(), foreign_item)]) as run:
                    with self.assertRaises(load.LoadError):
                        self.control.emergency_stop_publisher()
                self.assertFalse(any(call.args[0][1] == "stop" for call in run.call_args_list))

    def test_emergency_stop_rejects_malformed_inspect_without_disclosing_contents(self):
        malformed = publisher()
        malformed["running"] = "secret-from-docker"
        for output in ("secret-from-docker", publisher_json(None), publisher_json({}),
                       publisher_json(malformed), publisher_json(publisher("b" * 64))):
            with self.subTest(output=output):
                with patch.object(self.control, "run", side_effect=["a" * 12, output]) as run:
                    with self.assertRaises(load.LoadError) as error:
                        self.control.emergency_stop_publisher()
                self.assertNotIn("secret-from-docker", str(error.exception))
                self.assertEqual(len(run.call_args_list), 2)

    def test_emergency_stop_handles_recreated_publisher(self):
        responses = ["a" * 12, publisher_json(publisher()), "",
                     "b" * 12, publisher_json(publisher("b" * 64)), "", ""]
        with patch.object(self.control, "run", side_effect=responses) as run, redirect_stdout(io.StringIO()):
            self.control.emergency_stop_publisher()
        stopped = [call.args[0][-1] for call in run.call_args_list if call.args[0][1] == "stop"]
        self.assertEqual(stopped, ["a" * 64, "b" * 64])

    def test_emergency_stop_does_not_claim_success_when_publisher_keeps_restarting(self):
        def reply(args, **kwargs):
            if args[1] == "ps":
                return "a" * 12
            if args[1] == "inspect":
                return publisher_json(publisher(running=False, restarting=True))
            return ""
        output = io.StringIO()
        with patch.object(self.control, "run", side_effect=reply) as run, redirect_stdout(output):
            with self.assertRaises(load.LoadError):
                self.control.emergency_stop_publisher()
        self.assertEqual(sum(call.args[0][1] == "stop" for call in run.call_args_list), 3)
        self.assertEqual(output.getvalue(), "")

    def test_emergency_stop_verifies_empty_project_without_mutation(self):
        with patch.object(self.control, "run", side_effect=["", ""]) as run, redirect_stdout(io.StringIO()):
            self.control.emergency_stop_publisher()
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ["ps", "ps"])

    def test_emergency_stop_cli_bypasses_lock_and_unavailable_scenario(self):
        self.scenario.path.unlink()
        self.control.state_dir.mkdir(parents=True, exist_ok=True)
        self.control.state_file.write_text("invalid JSON")
        with patch.object(load, "Controller", return_value=self.control), \
                patch.object(self.control, "lock", side_effect=AssertionError("control lock")), \
                patch.object(load, "read_scenario", side_effect=AssertionError("scenario read")), \
                patch.object(self.control, "run", side_effect=["", ""]), redirect_stdout(io.StringIO()):
            self.assertEqual(load.main(["emergency-stop"]), 0)

    def test_emergency_stop_cli_hides_docker_error_and_returns_failure(self):
        output = io.StringIO()
        error_output = io.StringIO()
        with patch.object(load, "Controller", return_value=self.control), \
                patch.object(self.control, "run", side_effect=load.LoadError("unknown-secret-from-docker")), \
                redirect_stdout(output), redirect_stderr(error_output):
            self.assertEqual(load.main(["emergency-stop"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("unknown-secret-from-docker", error_output.getvalue())
        self.assertIn("Docker", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()
