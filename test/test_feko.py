"""Exporter and process-protocol tests; these do not validate the FEKO solver/API."""
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from feko.config import default_options, validate_options
from feko.export import build_model_data, export_model, render_script
from feko.runner import find_cadfeko, read_status, run_feko, validate_outputs, wait_for_saved_exit
from feko.results import read_s11, read_gain_peaks, save_results
from gui.project import default_project
from antenna import create_planar_antenna


class FekoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="feko-export-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = default_project(self.root)
        self.project.update(cells=[[0, 1, 0], [1, 1, 1], [0, 1, 0]], feed_cell=[0, 1])
        self.events = []

    def emit(self, event, **fields):
        self.events.append(dict(event=event, **fields))

    def test_geometry_coordinates_and_area(self):
        data = build_model_data(self.project)
        self.assertEqual(len(data["rectangles"]), 10)
        self.assertEqual(data["feed_position_mm"], [0, 5, 0])
        self.assertEqual(sum(r["width"]*r["depth"] for r in data["rectangles"]), 125)
        self.assertEqual(data["rectangles"][0]["y"], 2.5)
        self.assertEqual(data["thickness_mm"], 5)
        self.assertEqual(data["frequency_hz"], 3.2e9)

    def test_feed_definition_matches_scuff(self):
        data = build_model_data(self.project)
        geometry = create_planar_antenna(self.project["cells"], self.project["cell_size_mm"],
                                          self.project["feed_cell"], self.root / "scuff")
        self.assertEqual(data["feed"], geometry.metadata["feed"])
        self.assertEqual(data["feed"]["positive_side"], "+x")
        self.assertEqual(data["feed"]["reference"], "differential_between_planar_halves")

    def test_geometry_preserves_holes_and_islands(self):
        self.project.update(cells=[[1, 1, 1], [1, 0, 1], [1, 1, 1]], feed_cell=[0, 0])
        self.assertEqual(len(build_model_data(self.project)["rectangles"]), 16)
        self.project.update(cells=[[1, 0, 1], [0, 0, 0], [0, 0, 0]], feed_cell=[0, 0])
        self.assertEqual(len(build_model_data(self.project)["rectangles"]), 4)

    def test_legacy_project_and_defaults(self):
        self.project.pop("feko")
        self.assertEqual(build_model_data(self.project)["options"], default_options())

    def test_invalid_inputs(self):
        for key, value in (("reference_ohm", float("nan")),
                           ("frequency_points", 1), ("angle_step_deg", 7), ("run_solver", 1)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_options({key: value})
        with self.assertRaises(ValueError):
            validate_options({"sweep_enabled": True, "stop_ghz": 1})
        self.project["feko"]["feed_diameter_mm"] = 5
        self.assertNotIn("feed_diameter_mm", build_model_data(self.project)["options"])

    def test_requests_and_frequency_parameters(self):
        script = render_script(self.project)
        for fragment in ("GroundBottom=cf.Enums.GroundBottomTypeEnum.PEC", "geometry:Union(parts)",
                         "AddEdgePort({positive}, {negative})", "AddMultiportSParameter({port})", "TouchstoneExportEnabled = true",
                         "FarFieldRequestTypeEnum.Gain", "ASCIIEnabled = true", "local sweep = false",
                         "local feed_width = 5", "local z0 = 50", "application.Launcher:RunFEKO()"):
            self.assertIn(fragment, script)
        self.assertNotIn("AddWirePort", script)
        self.assertNotIn("AddLine", script)
        self.project["feko"].update(sweep_enabled=True, start_ghz=1.5, stop_ghz=2.5, frequency_points=21)
        script = render_script(self.project)
        self.assertIn("local sweep = true", script)
        self.assertIn("local f_start = 1500000000", script)
        self.assertIn("local f_count = 21", script)

    def test_export_no_overwrite_and_no_fake_results(self):
        script = export_model(self.project, self.root / "export")
        self.assertTrue(script.is_file())
        self.assertFalse((script.parent / "antenna.cfx").exists())
        self.assertEqual(json.loads((script.parent / "model.json").read_text())["api_target"], "CADFEKO 2022.2 / 2024")
        with self.assertRaises(FileExistsError):
            export_model(self.project, script.parent)

    def test_script_has_valid_lua51_syntax(self):
        runtime = ROOT / ".tools/feko-test-runtime"
        if not runtime.is_dir():
            self.skipTest("Optional test runtime: pip install --target .tools/feko-test-runtime lupa==2.6")
        sys.path.insert(0, str(runtime))
        self.addCleanup(lambda: sys.path.remove(str(runtime)))
        from lupa.lua51 import LuaRuntime
        lua = LuaRuntime()
        compile_only = lua.eval("function(source) local f, err = loadstring(source); assert(f, err); return true end")
        self.assertTrue(compile_only(render_script(self.project)))
        self.project["feko"]["sweep_enabled"] = True
        self.assertTrue(compile_only(render_script(self.project)))

    def test_missing_cadfeko_only_prepares(self):
        with patch("feko.runner.find_cadfeko", return_value=None), patch("feko.runner.subprocess.Popen") as start:
            run_feko(self.project, self.root, self.emit)
        start.assert_not_called()
        self.assertEqual(self.events[-1]["status"], "prepared")
        self.assertFalse((self.root / "feko/antenna.cfx").exists())

    def test_configured_executable_is_not_silently_replaced(self):
        with patch("feko.runner.shutil.which") as lookup:
            self.assertIsNone(find_cadfeko(str(self.root / "missing/cadfeko.exe")))
        lookup.assert_not_called()
        with self.assertRaises(ValueError):
            find_cadfeko(str(self.root / "other.exe"))

    def test_finds_nonstandard_installation_from_registry(self):
        executable = self.root / "custom/feko/bin/cadfeko.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        with patch("feko.runner.shutil.which", return_value=None), \
                patch("feko.runner.installed_roots", return_value=[self.root / "custom"]), \
                patch.dict("os.environ", {}, clear=True):
            self.assertEqual(find_cadfeko(), executable.resolve())

    def test_s11_formats(self):
        path = self.root / "sample.s1p"
        for mode, pair in (("MA", "0.5 90"), ("RI", "0 0.5"), ("DB", "-6.020599913 90")):
            path.write_text(f"! test\n# GHz S {mode} R 50\n3.2 {pair}\n")
            row = read_s11(path)[0]
            self.assertEqual(row["frequency_hz"], 3.2e9)
            self.assertAlmostEqual(row["s11_magnitude"], .5)
            self.assertAlmostEqual(row["s11_phase_deg"], 90)
        path.write_text("# Hz S RI R 50\n3200000000 0 0\n")
        self.assertIsNone(read_s11(path)[0]["s11_db"])
        path.write_text("# Hz S RI R 50\n3200000000 nan 0\n")
        with self.assertRaises(ValueError):
            read_s11(path)

    def test_multifrequency_gain_summary(self):
        s11 = self.root / "antenna_SParameter.s1p"
        farfield = self.root / "antenna_Gain_UpperHemisphere.ffe"
        s11.write_text("# GHz S MA R 50\n3.2 .5 10\n3.3 .25 20\n")
        farfield.write_text("##File Type: Far Field\n"
                            "#Frequency: 3.2e9\n#Coordinate System: Spherical\n#Result Type: Gain\n"
                            '## "Theta" "Phi"\n0 0 0 0 0 0 -10 -10 -7\n60 90 0 0 0 0 1 1 4\n'
                            "#Frequency: 3.3e9\n#Coordinate System: Spherical\n#Result Type: Gain\n"
                            "40 180 0 0 0 0 2 2 5\n")
        rows = save_results(self.root)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["gain_peak_dbi"], 4)
        self.assertEqual(rows[0]["theta_deg"], 60)
        self.assertEqual(rows[0]["angular_samples"], 2)
        self.assertEqual(rows[1]["gain_peak_dbi"], 5)
        self.assertTrue((self.root / "summary.csv").is_file())
        self.assertEqual(json.loads((self.root / "summary.json").read_text())["samples"], rows)
        s11.write_text("# GHz S MA R 50\n3.2 .5 10\n3.4 .25 20\n")
        with self.assertRaisesRegex(ValueError, "frequencies"):
            save_results(self.root)
        farfield.write_text("#Frequency: 3.2e9\n#Result Type: Directivity\n")
        with self.assertRaises(ValueError):
            read_gain_peaks(farfield)

    def test_partial_status_and_missing_outputs(self):
        self.assertIsNone(read_status(self.root))
        (self.root / "feko_status.txt").write_text("done", encoding="utf-8")
        self.assertIsNone(read_status(self.root))
        (self.root / "feko_status.txt").write_text("failed\nerror\nTest error\n", encoding="utf-8")
        self.assertEqual(read_status(self.root), ("failed", "error", "Test error"))
        with self.assertRaises(RuntimeError):
            validate_outputs(self.root, True)
        (self.root / "antenna.cfx").write_text("TEST-ONLY PLACEHOLDER")
        with self.assertRaises(RuntimeError):
            validate_outputs(self.root, True)
        self.assertEqual(validate_outputs(self.root, False), ["antenna.cfx"])

    def test_finished_calculation_waits_for_saved_cad_exit(self):
        process = Mock()
        process.poll.return_value = None
        with patch("feko.runner.find_cadfeko", return_value=Path("C:/cadfeko.exe")), \
                patch("feko.runner.subprocess.Popen", return_value=process) as start, \
                patch("feko.runner.read_status", return_value=("done", "solved", "")), \
                patch("feko.runner.save_results", return_value=[]), \
                patch("feko.runner.validate_outputs", return_value=["antenna.cfx", "antenna.s1p", "antenna.ffe"]):
            run_feko(self.project, self.root, self.emit)
        self.assertEqual(start.call_args.args[0][1], "--run-script")
        self.assertIn("--non-interactive", start.call_args.args[0])
        process.wait.assert_called_once_with(timeout=15)
        self.assertEqual(self.events[-1]["status"], "solved")

    def test_model_only_keeps_cad_window_open(self):
        process = Mock()
        self.project["feko"]["run_solver"] = False
        with patch("feko.runner.find_cadfeko", return_value=Path("C:/cadfeko.exe")), \
                patch("feko.runner.subprocess.Popen", return_value=process) as start, \
                patch("feko.runner.read_status", return_value=("done", "model_created", "")), \
                patch("feko.runner.validate_outputs", return_value=["antenna.cfx"]), \
                patch("feko.runner.stop_tree") as stop:
            run_feko(self.project, self.root, self.emit)
        process.wait.assert_not_called()
        self.assertNotIn("--non-interactive", start.call_args.args[0])
        stop.assert_not_called()
        self.assertEqual(self.events[-1]["status"], "model_created")

    def test_saved_exit_timeout_only_stops_owned_process(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("cadfeko", 15)
        with patch("feko.runner.stop_tree") as stop:
            wait_for_saved_exit(process)
        stop.assert_called_once_with(process)

    def test_lua_exits_only_after_successful_calculation(self):
        script = render_script(self.project)
        self.assertIn("elseif run_solver then", script)
        self.assertGreater(script.index(":Exit()"), script.index('status("done", "solved")'))
        self.assertGreater(script.index('status("done", "solved")'), script.rindex('application:SaveAs("antenna.cfx")'))

    def test_failed_script_stops_owned_process(self):
        process = Mock()
        with patch("feko.runner.find_cadfeko", return_value=Path("C:/cadfeko.exe")), \
                patch("feko.runner.subprocess.Popen", return_value=process), \
                patch("feko.runner.read_status", return_value=("failed", "error", "API test failure")), \
                patch("feko.runner.stop_tree") as stop:
            with self.assertRaisesRegex(RuntimeError, "API test failure"):
                run_feko(self.project, self.root, self.emit)
        stop.assert_called_once_with(process)
        self.assertFalse(any(event["event"] == "done" for event in self.events))

    def test_timeout_stops_owned_process(self):
        process = Mock()
        process.poll.return_value = None
        self.project["feko"]["timeout_s"] = 10
        with patch("feko.runner.find_cadfeko", return_value=Path("C:/cadfeko.exe")), \
                patch("feko.runner.subprocess.Popen", return_value=process), \
                patch("feko.runner.read_status", return_value=None), \
                patch("feko.runner.time.monotonic", side_effect=iter(range(0, 1100, 11))), \
                patch("feko.runner.stop_tree") as stop:
            with self.assertRaises(TimeoutError):
                run_feko(self.project, self.root, self.emit)
        stop.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
