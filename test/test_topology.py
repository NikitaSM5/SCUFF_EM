"""Block updates against independent solves, including non-symmetric complex systems."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from antenna.topology import (active_dofs, antenna_result, load_state, load_system,
                              save_state, triangle_cells, update_solution, write_subset_mesh)
from antenna.geometry import AntennaGeometry, _sha256
from antenna import solve_antenna
from gui.project import default_project
from gui.topology_config import cache_signature
from gui.topology_workflow import run_topology


class SchurTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(41)
        self.M = rng.normal(size=(18, 18))+1j*rng.normal(size=(18, 18))+20*np.eye(18)
        self.b = rng.normal(size=18)+1j*rng.normal(size=18)

    def assert_solution(self, state):
        active = state["active"]
        matrix = self.M[np.ix_(active, active)]
        np.testing.assert_allclose(state["x"], np.linalg.solve(matrix, self.b[active]), rtol=1e-11, atol=1e-13)
        np.testing.assert_allclose(state["Y"], np.linalg.inv(matrix), rtol=1e-11, atol=1e-13)

    def test_arbitrary_add_remove_mixed_and_unchanged(self):
        state = None
        for target in ([0, 3, 7, 12], [0, 3, 5, 7, 12, 17], [0, 5, 12], [0, 8, 12, 16], [16, 12, 0, 8]):
            state = update_solution(self.M, self.b, target, state)
            self.assert_solution(state)
        self.assertEqual(state["update"]["method"], "unchanged")

    def test_removing_all_old_indices_then_adding(self):
        state = update_solution(self.M, self.b, [0, 1, 2])
        state = update_solution(self.M, self.b, [14, 15], state)
        self.assert_solution(state)

    def test_periodic_rebase_and_drift_recovery(self):
        state = update_solution(self.M, self.b, [0, 3, 7])
        state["steps"] = 19
        state = update_solution(self.M, self.b, [0, 3, 7, 9], state)
        self.assertEqual(state["update"]["method"], "lu_rebase")
        state["Y"] *= 1.01
        state = update_solution(self.M, self.b, [0, 3, 7, 9], state)
        self.assertEqual(state["update"]["method"], "lu_rebase")
        self.assert_solution(state)

    def test_singular_intermediate_block_falls_back(self):
        matrix = np.array([[0, 1, 1], [1, 0, 1], [1, 1, 2]], dtype=complex)
        b = np.ones(3, dtype=complex)
        state = update_solution(matrix, b, [0, 1])
        state = update_solution(matrix, b, [0, 2], state)
        self.assertEqual(state["update"]["method"], "lu_rebase")
        np.testing.assert_allclose(state["x"], np.linalg.solve(matrix[np.ix_([0, 2], [0, 2])], b[[0, 2]]))

    def test_invalid_indices_and_state_hash(self):
        for indices in ([], [0, 0], [-1], [18]):
            with self.assertRaises(ValueError):
                update_solution(self.M, self.b, indices)
        state = update_solution(self.M, self.b, [0, 3])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.npz"
            save_state(state, path, "abc")
            self.assert_solution(load_state(path, "abc"))
            with self.assertRaises(ValueError):
                load_state(path, "other")

    def test_cache_signature_tracks_physics_not_drawing_or_postprocessing(self):
        config = default_project(".")
        config.update(cells=[[1, 0], [0, 0]], feed_cell=[0, 0])
        signature = cache_signature(config)
        changed = copy.deepcopy(config)
        changed["cells"][0][1] = 1
        changed["feko"].update(compute_pattern=False, reference_ohm=75)
        self.assertEqual(signature, cache_signature(changed))
        for key in ("frequency_ghz", "cell_size_mm", "thickness_mm", "epsilon_r", "loss_tangent"):
            changed = copy.deepcopy(config)
            changed[key] += .1
            self.assertNotEqual(signature, cache_signature(changed))


@unittest.skipUnless(os.environ.get("SCUFF_RUN_NATIVE_TESTS") == "1", "Opt-in SCUFF topology integration")
class NativeTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="scuff-topology-")
        cls.root = Path(cls.temp.name)
        cls.config = default_project(cls.root)
        cls.config.update(cells=[[1, 0], [0, 0]], feed_cell=[0, 0], cell_size_mm=10, mesh_size_mm=5)
        cls.config["feko"]["compute_pattern"] = False
        cls.events = []
        cls.prepared = cls.root / "prepare"
        cls.prepared.mkdir()
        run_topology(cls.config, cls.prepared, lambda event, **kw: cls.events.append(dict(event=event, **kw)), prepare=True)
        cls.manifest = json.loads((cls.prepared / "topology-cache.json").read_text())
        cls.full = load_system(cls.prepared / cls.manifest["systems"][0]["file"])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_add_remove_and_same_mesh_native_reassembly(self):
        config = dict(self.config, cells=[[1, 1], [0, 0]], topology_cache=str(self.prepared / "topology-cache.json"),
                      topology_state=str(self.prepared / "topology-state.json"))
        folder = self.root / "add"
        folder.mkdir()
        with patch("gui.topology_workflow.solve_antenna", side_effect=AssertionError("Must not reassemble")):
            run_topology(config, folder, lambda *args, **kwargs: None)
        result = load_system(folder / "scuff/frequency-0000/system.npz")
        self.assertEqual(result.metadata["topology_update"]["method"], "schur")
        self.assertGreater(result.metadata["topology_update"]["added_dofs"], 1)
        mesh_dir = self.root / "native-geometry"
        shutil.copytree(self.prepared / "full/geometry", mesh_dir)
        write_subset_mesh(self.full, config["cells"], mesh_dir / "metal.msh")
        metadata = copy.deepcopy(self.full.metadata["geometry"])
        metadata["cells"] = config["cells"]
        metadata["mesh"].update(unknowns=len(result.x), triangles=len(result.triangles))
        metadata["sha256"] = {name: _sha256(mesh_dir / name) for name in metadata["sha256"]}
        geometry = AntennaGeometry(mesh_dir, metadata)
        native = solve_antenna(geometry, config["frequency_ghz"], work_dir=self.root / "native")
        # Match by oriented support geometry, not native edge enumeration.
        def keys(r):
            return [tuple(r.vertices_mm[row[:4]].ravel()) for row in r.rwg]
        native_keys = {key: i for i, key in enumerate(keys(native))}
        order = [native_keys[key] for key in keys(result)]
        # SCUFF rebuilds its substrate interpolator for the smaller bounding box;
        # compare norms, not relative errors of near-zero matrix entries.
        matrix_error = np.linalg.norm(result.M-native.M[np.ix_(order, order)])/np.linalg.norm(result.M)
        current_error = np.linalg.norm(result.x-native.x[order])/np.linalg.norm(result.x)
        z = complex(*result.metadata["input_impedance_ohm"])
        native_z = complex(*native.metadata["input_impedance_ohm"])
        impedance_error = abs(z-native_z)/abs(native_z)
        print(f"Native subset: M={matrix_error:.3e}, x={current_error:.3e}, Z={impedance_error:.3e}")
        self.assertLess(matrix_error, 1e-3)
        self.assertLess(current_error, 1e-3)
        self.assertLess(impedance_error, 1e-3)
        config.update(cells=self.config["cells"], topology_state=str(folder / "topology-state.json"))
        removed_folder = self.root / "remove"
        removed_folder.mkdir()
        run_topology(config, removed_folder, lambda *args, **kwargs: None)
        removed = load_system(removed_folder / "scuff/frequency-0000/system.npz")
        self.assertEqual(removed.metadata["topology_update"]["method"], "schur")
        self.assertGreater(removed.metadata["topology_update"]["removed_dofs"], 1)

    def test_support_cells_and_feed_protection(self):
        rc = triangle_cells(self.full)
        self.assertEqual(set(map(tuple, rc)), {(0, 0), (0, 1), (1, 0), (1, 1)})
        with self.assertRaises(ValueError):
            active_dofs(self.full, [[0, 1], [0, 0]])
        with self.assertRaises(ValueError):
            active_dofs(self.full, [[1, 0], [0, 1]])

    def test_parameter_mismatch_is_rejected(self):
        config = dict(self.config, frequency_ghz=4, topology_cache=str(self.prepared / "topology-cache.json"))
        with self.assertRaisesRegex(ValueError, "Параметры изменены"):
            run_topology(config, self.root / "mismatch", lambda *args, **kwargs: None)

    def test_resource_limit_prevents_assembly(self):
        config = dict(self.config, max_unknowns=1)
        with patch("gui.topology_workflow.solve_antenna", side_effect=AssertionError("Limit must be checked first")):
            with self.assertRaisesRegex(ValueError, "Лимит неизвестных"):
                run_topology(config, self.root / "limit", lambda *args, **kwargs: None, prepare=True)

    def test_cache_and_state_integrity(self):
        from gui.topology_workflow import digest, load_previous
        with self.assertRaisesRegex(ValueError, "не соответствует"):
            load_previous(self.prepared / "topology-state.json", "wrong-hash", 1)
        manifest = self.root / "bad-cache.json"
        data = copy.deepcopy(self.manifest)
        data["systems"][0]["file"] = "prepare/"+data["systems"][0]["file"]
        data["systems"][0]["sha256"] = "wrong-hash"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        config = dict(self.config, topology_cache=str(manifest))
        with self.assertRaisesRegex(ValueError, "изменена или повреждена"):
            run_topology(config, self.root / "bad-run", lambda *args, **kwargs: None)

    def test_multiple_frequencies_survive_updates(self):
        config = copy.deepcopy(self.config)
        config["feko"].update(sweep_enabled=True, start_ghz=3.1, stop_ghz=3.3, frequency_points=2)
        folder = self.root / "sweep"
        folder.mkdir()
        run_topology(config, folder, lambda *args, **kwargs: None, prepare=True)
        config.update(cells=[[1, 1], [0, 0]], topology_cache=str(folder / "topology-cache.json"),
                      topology_state=str(folder / "topology-state.json"))
        edited = self.root / "sweep-edit"
        edited.mkdir()
        run_topology(config, edited, lambda *args, **kwargs: None)
        rows = json.loads((edited / "comparison.json").read_text())["scuff"]
        self.assertEqual([row["frequency_hz"] for row in rows], [3.1e9, 3.3e9])
        self.assertTrue(all(row["topology_update"]["method"] == "schur" for row in rows))


if __name__ == "__main__":
    unittest.main()
