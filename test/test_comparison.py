"""Port conventions, sequential orchestration, partial results, optional Gain."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from antenna.parameters import from_impedance, from_reflection
from gui.calculation import frequencies, row_compute_time, run_comparison
from gui.project import default_project
from feko.export import render_script
from feko.results import save_results
from feko.runner import validate_outputs


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = default_project(self.root)
        self.project.update(cells=[[1]], feed_cell=[0, 0])

    def test_one_port_identity_and_reference(self):
        matched = from_impedance(50, 3.2e9, 50)
        self.assertEqual(matched["s11_magnitude"], 0)
        self.assertIsNone(matched["s11_db"])
        self.assertEqual(matched["vswr"], 1)
        row = from_impedance(25-10j, 3.2e9, 50)
        self.assertAlmostEqual(row["resistance_ohm"], 25)
        self.assertAlmostEqual(row["reactance_ohm"], -10)
        self.assertIsNone(from_reflection(1, 1e9, 50)["resistance_ohm"])
        self.assertIsNone(from_reflection(1, 1e9, 50)["vswr"])

    def test_shared_frequency_grid(self):
        self.assertEqual(frequencies(self.project), [3.2])
        self.project["feko"].update(sweep_enabled=True, start_ghz=3, stop_ghz=3.4, frequency_points=3)
        self.assertEqual(frequencies(self.project), [3, 3.2, 3.4])

    def test_unknown_compute_time_is_not_zero(self):
        self.assertIsNone(row_compute_time([]))
        self.assertIsNone(row_compute_time([{}]))
        self.assertIsNone(row_compute_time([dict(compute_s=1), {}]))
        self.assertEqual(row_compute_time([dict(compute_s=0), dict(compute_s=2)]), 2)

    def test_comparison_uses_solver_time_not_launcher_time(self):
        row = dict(from_impedance(50, 3.2e9, 50), compute_s=.02, elapsed_s=5)
        events = []
        def feko(config, directory, emit):
            emit("done", status="solved", summary=[row], timings_s=dict(compute=.1, cadfeko=30))
        with patch("feko.runner.find_cadfeko", return_value=Path("cadfeko.exe")), \
                patch("gui.calculation.run_scuff", return_value=dict(summary=[row])), \
                patch("feko.runner.run_feko", side_effect=feko):
            run_comparison(self.project, self.root, lambda event, **fields: events.append(fields))
        self.assertEqual(events[-1]["comparison"]["compute_timings_s"], dict(scuff=.02, feko=.1))

    def test_optional_pattern_export(self):
        self.project["feko"]["compute_pattern"] = False
        self.assertIn("local compute_pattern = false", render_script(self.project))
        (self.root / "antenna.cfx").write_text("test fixture")
        (self.root / "antenna_SParameter.s1p").write_text("# GHz S MA R 50\n3.2 0.5 -20\n")
        validate_outputs(self.root, True, False)
        with self.assertRaises(RuntimeError):
            validate_outputs(self.root, True, True)
        rows = save_results(self.root, False)
        self.assertIsNone(rows[0]["gain_peak_dbi"])
        self.assertFalse((self.root / "farfield.json").exists())

    def test_sequential_workflow_and_failure_preserves_scuff(self):
        order, events = [], []
        row = from_impedance(50, 3.2e9, 50)
        def scuff(*args):
            order.append("scuff")
            return dict(summary=[row])
        def feko(config, directory, emit):
            order.append("feko")
            emit("done", status="solved", summary=[row])
        def emit(event, **data):
            events.append(dict(event=event, **data))
        with patch("feko.runner.find_cadfeko", return_value=Path("cadfeko.exe")), \
                patch("gui.calculation.run_scuff", side_effect=scuff), \
                patch("feko.runner.run_feko", side_effect=feko):
            run_comparison(self.project, self.root, emit)
        self.assertEqual(order, ["scuff", "feko"])
        self.assertEqual([e["event"] for e in events], ["comparison", "done"])
        self.assertEqual(events[-1]["comparison"]["scuff"], [row])
        self.assertEqual(events[-1]["comparison"]["compute_timings_s"], dict(scuff=None, feko=None))
        feeds = events[-1]["comparison"]["feed_models"]
        self.assertEqual(feeds["scuff"], "planar_delta_gap_voltage")
        self.assertEqual(feeds["scuff"], feeds["feko"])
        self.assertTrue((self.root / "comparison.csv").exists())
        with patch("feko.runner.find_cadfeko", return_value=Path("cadfeko.exe")), \
                patch("gui.calculation.run_scuff", side_effect=scuff), \
                patch("feko.runner.run_feko", side_effect=RuntimeError("solver failure")):
            with self.assertRaisesRegex(RuntimeError, "solver failure"):
                run_comparison(self.project, self.root, emit)
        partial = json.loads((self.root / "comparison.json").read_text())
        self.assertTrue(partial["scuff"])
        self.assertFalse(partial["feko"])

    def test_no_cadfeko_does_not_spend_time_on_scuff(self):
        with patch("feko.runner.find_cadfeko", return_value=None), patch("gui.calculation.run_scuff") as scuff:
            with self.assertRaises(RuntimeError):
                run_comparison(self.project, self.root, lambda *args, **kwargs: None)
        scuff.assert_not_called()
