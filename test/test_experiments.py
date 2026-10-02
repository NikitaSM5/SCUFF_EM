"""Deterministic algorithms and complex Schur solve operators, without external solvers."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from antenna.schur import solve_topology, Factor
from experiments.config import defaults, validate
from experiments.domain import random_antenna, additions, spaces
from experiments.ga import definition, breed, IMPLEMENTATION, run_ga
from experiments.metrics import discrepancy, fitness
from experiments.storage import Journal, canonical, key, CalculationCache
from experiments.reporting import online_wall, plots
from gui.project import default_project


class FactorSchurTests(unittest.TestCase):
    def test_add_remove_replace_chain(self):
        rng = np.random.default_rng(72)
        M = rng.normal(size=(30, 30))+1j*rng.normal(size=(30, 30))+25*np.eye(30)
        b = rng.normal(size=30)+1j*rng.normal(size=30)
        state = None
        for active in ([0, 2, 5, 8, 9], [0, 2, 5, 8, 9, 13], [0, 2, 5, 8, 9, 13, 14, 16],
                       [0, 5, 8, 9, 13, 14, 16], [0, 5, 9, 13, 16, 23, 29]):
            state, x, d = solve_topology(M, b, active, state, max_changed_fraction=1)
            np.testing.assert_allclose(x, np.linalg.solve(M[np.ix_(state.active, state.active)], b[state.active]), rtol=1e-11, atol=1e-13)
            self.assertFalse(hasattr(state, "Y"))
        self.assertEqual(d["method"], "schur")

    def test_condition_and_fallback(self):
        with self.assertRaises(np.linalg.LinAlgError):
            Factor(np.diag([1+0j, 1e-15]))
        M = np.array([[0, 1, 1], [1, 0, 1], [1, 1, 2]], dtype=complex)
        b = np.ones(3)
        old, _, _ = solve_topology(M, b, [0, 1])
        state, x, diag = solve_topology(M, b, [0, 2], old, max_changed_fraction=1)
        self.assertEqual(diag["method"], "lu_fallback")
        np.testing.assert_allclose(x, np.linalg.solve(M[np.ix_([0, 2], [0, 2])], b[[0, 2]]))

    def test_periodic_rebase(self):
        M = np.eye(10, dtype=complex)+.1
        b = np.ones(10)
        old, _, _ = solve_topology(M, b, [0, 1, 2])
        old, _, _ = solve_topology(M, b, [0, 1, 2, 3], old, refactor_interval=2)
        _, _, diag = solve_topology(M, b, [0, 1, 2, 3, 4], old, refactor_interval=2)
        self.assertEqual(diag["reason"], "periodic_refactorization")


class ExperimentLogicTests(unittest.TestCase):
    def setUp(self):
        self.project = default_project(".")
        self.project.update(cells=[[0, 0, 0], [0, 1, 0], [0, 0, 0]], feed_cell=[1, 1])
        self.settings = dict(defaults("ga"), population_size=4, generations=3, ga_implementation=IMPLEMENTATION,
                             min_elements=2, max_elements=5, tournament_size=2)

    def test_initial_population_and_random_tape_identical(self):
        a = definition(self.project, self.settings)
        b = definition(self.project, self.settings)
        self.assertEqual(canonical(a), canonical(b))
        pop = a["initial_population"]
        ca, ta = breed(pop, [4, 3, 2, 1], a["decisions"][0], self.project, self.settings)
        cb, tb = breed(pop, [40, 30, 20, 10], b["decisions"][0], self.project, self.settings)
        self.assertEqual(ca, cb)
        self.assertEqual(ta, tb)

    def test_ga_requires_explicit_new_algorithm_choice(self):
        with self.assertRaises(ValueError):
            definition(self.project, dict(self.settings, ga_implementation="reference_required"))

    def test_ga_validation_ignores_hidden_addition_count(self):
        project = dict(self.project, cells=[[1]], feed_cell=[0, 0])
        validate(dict(defaults("ga"), ga_implementation=IMPLEMENTATION), project)

    def test_parallel_timing_counts_overlap_once(self):
        rows = [dict(started_s=0, online_s=5), dict(started_s=2, online_s=4),
                dict(started_s=10, online_s=1)]
        self.assertEqual(online_wall(rows), 7)
        self.assertEqual(online_wall([]), 0)

    def test_paired_plot_export_and_per_k_scatter(self):
        row = dict(s11_magnitude=.2, resistance_ohm=45, gain_peak_dbi=1)
        a = dict(backend="schur", rows=[row], fitness=-.04, online_s=.01, cached=False)
        b = dict(a, backend="feko", online_s=1)
        pair = dict(schur=a, feko=b, group="independent", k=1,
                    errors=[dict(s11_complex=dict(relative=.1, absolute=.02))])
        with tempfile.TemporaryDirectory() as temp:
            names = plots(temp, dict(pairs=[pair]),
                          dict(preprocessing=dict(elapsed_s=.5), backend_setup_s={}), [a, b])
            self.assertIn("scatter-independent-k-001.png", names)
            self.assertIn("paired-cumulative-time.png", names)
            self.assertTrue(all((Path(temp)/"plots"/name).stat().st_size > 1000 for name in names))

    def test_additions_are_exhaustive_and_sampling_reproducible(self):
        base = self.project["cells"]
        cases = list(additions(base, [1, 1], 1, 1, "exhaustive", "sample", 42))
        self.assertEqual(len(cases), 8)
        self.assertEqual(sum(c["valid"] for c in cases), 4)
        a = list(additions(base, [1, 1], 2, 3, "sample", "sample", 17))
        b = list(additions(base, [1, 1], 2, 3, "sample", "sample", 17))
        self.assertEqual(a, b)
        self.assertEqual(spaces(base, 2, 3, "exhaustive", "sample")[1]["combinations"], 28)

    def test_relative_metric_is_finite_at_zero_and_signed(self):
        self.assertEqual(discrepancy(-2, -1, .01)["relative"], 1)
        self.assertEqual(discrepancy(1e-4j, 0, 1e-3)["relative"], .1)

    def test_cache_identity_and_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = CalculationCache(temp, "test")
            cache.put(dict(mask=[1, 0], frequency=1), dict(rows=[1], artifacts=[]))
            self.assertIsNotNone(cache.get(dict(mask=[1, 0], frequency=1)))
            self.assertIsNone(cache.get(dict(mask=[1, 0], frequency=2)))

    def test_locked_replay_identical_candidates_with_different_fitness(self):
        # Test doubles are confined to tests; production always calls the real solvers.
        class Backend:
            def __init__(self, name):
                self.name, self.records, self.stats = name, [], {}
            def evaluate(self, cells, force=False, context=None):
                value = sum((r+1)*(c+1)*v for r, row in enumerate(cells) for c, v in enumerate(row))
                value = value if self.name == "schur" else -value
                row = dict(frequency_hz=3.2e9, resistance_ohm=50, reactance_ohm=0, s11_real=.1, s11_imag=0)
                result = dict(candidate_id=key(cells), cells=copy.deepcopy(cells), fitness=value, rows=[row], online_s=.1)
                self.records.append(result)
                return result
            def evaluate_many(self, cells, contexts):
                return [self.evaluate(c, context=x) for c, x in zip(cells, contexts)]
        with tempfile.TemporaryDirectory() as temp:
            a, b = Backend("schur"), Backend("feko")
            outcome = run_ga(self.project, self.settings, Path(temp), a, b, Journal(temp, lambda *a, **k: None), lambda: None, lambda data: None)
            self.assertEqual([r["cells"] for r in a.records], [r["cells"] for r in b.records[:-1]])
            self.assertIsNone(outcome["divergence_generation"])
            self.assertEqual(len(b.records), len(a.records)+1)


if __name__ == "__main__":
    unittest.main()
