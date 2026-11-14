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


if __name__ == "__main__":
    unittest.main()
