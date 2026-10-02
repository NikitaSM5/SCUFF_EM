"""Solver-runtime parsing, cache accounting, and owned-session cancellation."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from feko.timing import read_solver_timing, available_solver_timing
from feko.session import CadSession
from experiments.config import ExperimentCancelled, defaults, validate
from experiments.backends import FitnessBackend
from experiments.storage import Journal
from experiments.timing import compute_value, compute_total, compute_speedup
from gui.project import default_project


class SolverTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_last_cumulative_runtime_not_cpu_or_sum_of_reports(self):
        path = self.root/"antenna.out"
        path.write_text("SUMMARY OF REQUIRED TIMES IN SECONDS\n"
                        " Calculation of matrix elements          0.050 0.020\n"
                        " total times:                            0.600 0.100\n"
                        "SUMMARY OF REQUIRED TIMES IN SECONDS\n"
                        " Calculation of matrix elements          0.120 0.040\n"
                        " Solution of the system of linear equations 0.070 0.030\n"
                        " Calculation of far field                0.060 0.015\n"
                        " total times:                            1.200 0.200\n")
        result = read_solver_timing(path)
        self.assertEqual(result["compute"], .2)
        self.assertEqual(result["solver_cpu"], 1.2)
        self.assertEqual(result["linear_solve"], .03)
        self.assertEqual(result["assembly"], .04)

    def test_missing_or_incomplete_time_is_not_guessed(self):
        self.assertIsNone(available_solver_timing(self.root)["compute"])
        (self.root/"antenna.out").write_text("SUMMARY OF REQUIRED TIMES IN SECONDS\n other 1 2\n")
        with self.assertRaises(ValueError):
            read_solver_timing(self.root/"antenna.out")
        self.assertIn("timing_error", available_solver_timing(self.root))

    def test_speedup_excludes_cache_and_launch_overhead(self):
        schur = dict(cached=False, compute_s=.01, online_s=1)
        feko = dict(cached=False, compute_s=.2, online_s=30)
        self.assertEqual(compute_speedup(schur, feko), 20)
        hit = dict(feko, cached=True, original_compute_s=.2)
        self.assertIsNone(compute_speedup(schur, hit))
        self.assertIsNone(compute_value(hit))
        self.assertAlmostEqual(compute_total([schur, feko, hit]), .21)

    def test_backend_does_not_charge_historical_compute_time_on_cache_hit(self):
        class Backend(FitnessBackend):
            name = "timing-test"
            def calculate(self, cells, directory):
                return dict(rows=[dict(s11_magnitude=.5)], timings_s=dict(compute=.2), artifacts=[], diagnostics=[])
        project = default_project(self.root)
        project.update(cells=[[1, 0], [0, 0]], feed_cell=[0, 0])
        settings = validate(dict(defaults("accuracy"), k_max=1), project)
        backend = Backend(project, settings, self.root, Journal(self.root, lambda *a, **kw: None), lambda: None, {})
        fresh = backend.evaluate(project["cells"])
        hit = backend.evaluate(project["cells"])
        self.assertEqual(fresh["compute_s"], .2)
        self.assertIsNone(hit["compute_s"])
        self.assertEqual(hit["original_compute_s"], .2)

    def test_session_start_cancel_stops_only_owned_process(self):
        session = CadSession(Path("C:/cadfeko.exe"), self.root/"session")
        process = Mock()
        process.poll.return_value = None
        with patch("feko.session.subprocess.Popen", return_value=process), \
                patch("feko.session.stop_tree") as stop:
            def cancel():
                raise ExperimentCancelled()
            with self.assertRaises(ExperimentCancelled):
                session.start(lambda *a, **kw: None, cancel, 120)
        stop.assert_called_once_with(process)
        self.assertIsNone(session.log)


if __name__ == "__main__":
    unittest.main()
