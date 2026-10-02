"""Run with: py -3.11 -m unittest discover -s test -p test_antenna_api.py -v"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from antenna import Substrate, create_planar_antenna, load_geometry, solve_antenna, save_matrix, save_result
from antenna.geometry import _validate_grid
from antenna.solver import AntennaResult, _verify


class InputTests(unittest.TestCase):
    def test_invalid_masks(self):
        for grid in ([], [[1, 0]], [[0]], [[2]], [[float("nan")]], [[0.5]], [["1"]]):
            with self.subTest(grid=grid), self.assertRaises(ValueError):
                _validate_grid(grid, (0, 0))

    def test_invalid_feeds(self):
        for feed in ((-1, 0), (1, 0), (0.1, 0), (True, 0), (0,), (0, 0, 0), None, "00"):
            with self.subTest(feed=feed), self.assertRaises(ValueError):
                _validate_grid([[1]], feed)

    def test_diagonal_contacts(self):
        for grid in ([[1, 0], [0, 1]], [[0, 1], [1, 0]]):
            with self.assertRaisesRegex(ValueError, "Point-only"):
                _validate_grid(grid, tuple(np.argwhere(grid)[0]))

    def test_substrate_validation(self):
        for kwargs in ({"thickness_mm": 0}, {"epsilon_r": -1}, {"loss_tangent": -0.1},
                       {"epsilon_r": float("nan")}, {"loss_tangent": float("inf")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Substrate(**kwargs)


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scuff-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def create(self, grid=None, feed=(0, 0), name="geometry", **kwargs):
        return create_planar_antenna([[1]] if grid is None else grid, 2, feed,
                                     self.root / name, **kwargs)

    def test_connected_pixels_and_feed_coordinates(self):
        geometry = self.create([[1, 1], [1, 1]], feed=(1, 0))
        self.assertEqual(geometry.metadata["feed_position_mm"], [-1, -1, 0])
        self.assertAlmostEqual(geometry.metadata["mesh"]["area_mm2"], 16)
        self.assertGreater(geometry.unknowns, 0)
        self.assertGreaterEqual(geometry.metadata["mesh"]["port_edges"], 1)
        self.assertEqual(geometry.metadata["feed"]["gap_mm"], 0)
        self.assertEqual(geometry.metadata["feed"]["voltage_v"], 1)
        self.assertTrue(geometry.port_file.read_text().startswith("DELTA_GAP "))
        self.assertEqual(load_geometry(geometry.directory).metadata, geometry.metadata)

    def test_holes_and_separate_islands(self):
        masks = [([[1, 1, 1], [1, 0, 1], [1, 1, 1]], 8),
                 ([[1, 0, 1], [0, 0, 0], [0, 0, 0]], 2)]
        for i, (mask, count) in enumerate(masks):
            geometry = self.create(mask, name=str(i))
            self.assertAlmostEqual(geometry.metadata["mesh"]["area_mm2"], count*4)

    def test_loss_sign_and_height(self):
        geometry = self.create(substrate=Substrate(5, 4.4, 0.01))
        definition = geometry.substrate_file.read_text()
        self.assertIn("+0.044", definition)
        self.assertIn("-5 GROUNDPLANE", definition)

    def test_changed_inputs_are_rejected(self):
        geometry = self.create()
        geometry.port_file.write_text("modified")
        with self.assertRaisesRegex(ValueError, "changed"):
            load_geometry(geometry.directory)

    def test_no_overwrite(self):
        self.create()
        with self.assertRaises(FileExistsError):
            self.create()

    def test_mesh_refinement_increases_unknowns(self):
        coarse = self.create(name="coarse", mesh_size_mm=1)
        fine = self.create(name="fine", mesh_size_mm=0.5)
        self.assertGreater(fine.unknowns, coarse.unknowns)

    def test_size_limit_before_process_launch(self):
        geometry = self.create()
        with patch("antenna.solver.subprocess.run") as process:
            with self.assertRaisesRegex(ValueError, "limit"):
                solve_antenna(geometry, 3.2, max_unknowns=1)
            process.assert_not_called()

    def test_invalid_frequency_before_process_launch(self):
        geometry = self.create()
        for frequency in (0, -1, float("nan"), float("inf")):
            with self.subTest(frequency=frequency), self.assertRaises(ValueError):
                solve_antenna(geometry, frequency)

    def test_residual_check_without_second_solve(self):
        geometry = self.create()
        native = {"triangles": geometry.metadata["mesh"]["triangles"],
                  "port_edges": geometry.metadata["mesh"]["port_edges"],
                  "feed_model": "planar_delta_gap_voltage"}
        with patch("numpy.linalg.solve", side_effect=AssertionError("No second solve allowed")):
            checks = _verify(np.eye(2), np.ones(2), np.ones(2), native, geometry)
            self.assertEqual(checks, {"relative_residual": 0.0})
            with self.assertRaisesRegex(RuntimeError, "residual"):
                _verify(np.eye(2), np.ones(2), np.zeros(2), native, geometry)

    def test_matrix_storage_exact_and_no_overwrite(self):
        matrix = np.array([[1+2j, 3-4j], [3-4j, 5+6j]])
        path = save_matrix(matrix, self.root / "nested/M.npy")
        np.testing.assert_array_equal(np.load(path, allow_pickle=False), matrix)
        with self.assertRaises(FileExistsError):
            save_matrix(matrix, path)
        with self.assertRaises(ValueError):
            save_matrix(matrix, self.root / "M.csv")
        with self.assertRaises(ValueError):
            save_matrix(np.zeros((2, 3)), path)
        with self.assertRaises(ValueError):
            save_matrix([[np.inf]], path)

    def test_result_storage_without_pickle(self):
        result = AntennaResult(np.eye(2, dtype=complex), np.ones(2), np.ones(2),
                               np.zeros((4, 3)), np.zeros((2, 3), dtype=int),
                               np.zeros((2, 6), dtype=int), {"success": True}, self.root)
        path = save_result(result, self.root / "system.npz")
        with np.load(path, allow_pickle=False) as data:
            np.testing.assert_array_equal(data["M"], result.M)
            self.assertEqual(json.loads(str(data["metadata_json"])), result.metadata)
            self.assertEqual(data["rwg"].shape, (2, 6))
        with self.assertRaises(FileExistsError):
            save_result(result, path)


if __name__ == "__main__":
    unittest.main()
