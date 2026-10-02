"""Integration tests: set SCUFF_RUN_NATIVE_TESTS=1, then run unittest discovery."""
import json
import os
from pathlib import Path
import tempfile
import unittest

import numpy as np

from antenna import Substrate, create_planar_antenna, solve_antenna, save_matrix, save_result


@unittest.skipUnless(os.environ.get("SCUFF_RUN_NATIVE_TESTS") == "1", "Opt-in native SCUFF calculations")
class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="scuff native ")
        cls.root = Path(cls.temp.name)
        cls.geometry = create_planar_antenna([[1]], 10, (0, 0), cls.root / "geometry")
        cls.result = solve_antenna(cls.geometry, 3.2, work_dir=cls.root / "run",
                                  verify_reference=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_reference_and_basis(self):
        result = self.result
        self.assertTrue(result.metadata["success"])
        self.assertLess(max(result.metadata["checks"].values()), 1e-8)
        self.assertNotIn("numpy_relative_residual", result.metadata["checks"])
        self.assertNotIn("numpy_solution_difference", result.metadata["checks"])
        self.assertEqual(result.rwg.shape, (self.geometry.unknowns, 6))
        self.assertTrue((result.rwg[:, :4] >= 0).all())
        self.assertTrue((result.rwg[:, :4] < len(result.vertices_mm)).all())
        self.assertTrue((result.rwg[:, 4:] < len(result.triangles)).all())
        self.assertTrue(np.allclose(result.vertices_mm[:, 2], 0))
        self.assertEqual(len(result.metadata["input_impedance_ohm"]), 2)
        self.assertAlmostEqual(result.metadata["omega"], 2*np.pi*3.2/299.792458, places=14)
        self.assertLess(result.metadata["checks"]["repeat_impedance_difference"], 1e-8)

    def test_voltage_gap_current_and_passivity(self):
        result = self.result
        vertices, rwg = result.vertices_mm, result.rwg
        port = self.geometry.metadata["feed"]
        start, end = np.array(port["start_mm"]), np.array(port["end_mm"])
        endpoints = vertices[rwg[:, :2]]
        selected = (np.isclose(endpoints[:, :, 0], start[0]).all(axis=1) &
                    (endpoints[:, :, 1] >= start[1]-1e-9).all(axis=1) &
                    (endpoints[:, :, 1] <= end[1]+1e-9).all(axis=1))
        lengths = np.linalg.norm(endpoints[:, 1]-endpoints[:, 0], axis=1)
        signs = np.where(vertices[rwg[:, 2], 0] < start[0], 1, -1)
        weights = selected*lengths*signs
        impedance = complex(*result.metadata["input_impedance_ohm"])
        self.assertEqual(result.metadata["port_voltage_V"], [1, 0])
        self.assertAlmostEqual(abs(complex(*result.metadata["port_current_A"])*impedance-1), 0, places=10)
        self.assertGreater(impedance.real, 0)
        self.assertAlmostEqual(abs(impedance*np.dot(weights, result.x)-1), 0, places=10)
        np.testing.assert_allclose(result.b, -weights/376.73031346177, rtol=1e-8, atol=1e-15)
        self.assertAlmostEqual(lengths[selected].sum(), port["width_mm"])

    def test_electromagnetic_scale_invariance(self):
        geometry = create_planar_antenna([[1]], 20, (0, 0), self.root / "scaled-geometry",
                                         substrate=Substrate(10, 4.4, 0))
        result = solve_antenna(geometry, 1.6, work_dir=self.root / "scaled-run")
        np.testing.assert_allclose(result.metadata["input_impedance_ohm"],
                                   self.result.metadata["input_impedance_ohm"], rtol=1e-5)

    def test_repeat(self):
        repeat = solve_antenna(self.geometry, 3.2, work_dir=self.root / "repeat")
        for name in ("M", "b", "x"):
            np.testing.assert_array_equal(getattr(repeat, name), getattr(self.result, name))

    def test_long_strip_bounds_cover_all_panels(self):
        cells = [[int(c == 2) for c in range(6)] for r in range(6)]
        geometry = create_planar_antenna(cells, 5, (0, 2), self.root / "strip-geometry")
        result = solve_antenna(geometry, 3.2, work_dir=self.root / "strip-run", verify_reference=True)
        used = result.vertices_mm[np.unique(result.triangles)]
        bounds = np.array([used.min(axis=0), used.max(axis=0)])
        np.testing.assert_allclose(result.metadata["surface_bounds_mm"], bounds, rtol=0, atol=1e-12)
        diagonal = np.linalg.norm(bounds[1, :2] - bounds[0, :2])
        self.assertAlmostEqual(result.metadata["rho_range_mm"][1], diagonal)
        self.assertAlmostEqual(diagonal, np.hypot(5, 30))
        self.assertLess(max(result.metadata["checks"].values()), 1e-8)
        repeat = solve_antenna(geometry, 3.2, work_dir=self.root / "strip-repeat")
        for name in ("M", "b", "x"):
            np.testing.assert_array_equal(getattr(result, name), getattr(repeat, name))

    def test_frequency_changes_matrix(self):
        other = solve_antenna(self.geometry, 4.0, work_dir=self.root / "frequency")
        self.assertGreater(np.linalg.norm(other.M - self.result.M) / np.linalg.norm(self.result.M), 1e-3)

    def test_dielectric_and_ground_height_affect_matrix(self):
        for index, substrate in enumerate((Substrate(5, 2.2, 0), Substrate(8, 4.4, 0), Substrate(5, 4.4, 0.01))):
            geometry = create_planar_antenna([[1]], 10, (0, 0), self.root / f"geometry-{index}",
                                             substrate=substrate)
            other = solve_antenna(geometry, 3.2, work_dir=self.root / f"substrate-{index}")
            np.testing.assert_array_equal(other.vertices_mm, self.result.vertices_mm)
            self.assertGreater(np.linalg.norm(other.M - self.result.M) / np.linalg.norm(self.result.M), 1e-5)

    def test_saved_system_and_matrix(self):
        matrix_path = save_matrix(self.result.M, self.root / "M.npy")
        system_path = save_result(self.result, self.root / "system.npz")
        np.testing.assert_array_equal(np.load(matrix_path, allow_pickle=False), self.result.M)
        with np.load(system_path, allow_pickle=False) as data:
            for key in ("M", "b", "x"):
                np.testing.assert_array_equal(data[key], getattr(self.result, key))
            self.assertTrue(json.loads(str(data["metadata_json"]))["success"])

    def test_timeout_is_reported(self):
        directory = self.root / "timeout"
        with self.assertRaises(Exception) as caught:
            solve_antenna(self.geometry, 3.2, work_dir=directory, timeout_s=0.00001)
        import subprocess
        self.assertIsInstance(caught.exception, subprocess.TimeoutExpired)
        summary = json.loads((directory / "summary.json").read_text())
        self.assertFalse(summary["success"])
        self.assertIn("timed out", summary["error"])


if __name__ == "__main__":
    unittest.main()
