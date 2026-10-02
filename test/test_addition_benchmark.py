"""Independent current-base sampling, cold measurements and flat mean/error CSVs."""
import copy
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from antenna.parameters import from_impedance
from experiments.addition_benchmark import prepare_addition_cases, run_current_additions, save_addition_tables
from experiments.addition_sampling import constraint_cases
from experiments.config import defaults, validate, ExperimentCancelled
from experiments.domain import sample_valid_additions, valid
from experiments.metrics import compare_rows
from experiments.storage import Journal, key
from gui.project import default_project


class AdditionBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = default_project(self.root)
        self.project.update(cells=[[1, 1, 0], [0, 0, 0], [0, 0, 0]], feed_cell=[0, 0])
        self.settings = validate(dict(defaults("accuracy"), k_max=2, samples_per_k=3), self.project)

    def test_defaults_and_current_geometry_validation(self):
        self.assertEqual(defaults("accuracy")["samples_per_k"], 100)
        self.assertEqual(self.settings["accuracy_mode"], "current_additions")
        self.assertFalse(self.settings["incremental"])
        self.assertEqual(self.settings["max_changed_fraction"], 1)
        disabled = copy.deepcopy(self.project)
        disabled["feko"]["compute_pattern"] = False
        with self.assertRaisesRegex(ValueError, "Gmax"):
            validate(self.settings, disabled)
        with self.assertRaises(ValueError):
            validate(dict(self.settings, k_max=8), self.project)
        old = dict(self.settings)
        old.pop("accuracy_mode")
        self.assertEqual(validate(old, self.project)["accuracy_mode"], "random_base")

    def test_sampling_unique_valid_reproducible_and_unchanged_base(self):
        base = copy.deepcopy(self.project["cells"])
        for k in (1, 2, 3):
            cases, plan = sample_valid_additions(base, [0, 0], k, 100, 42)
            repeat, same_plan = sample_valid_additions(base, [0, 0], k, 100, 42)
            self.assertEqual(cases, repeat)
            self.assertEqual(plan, same_plan)
            self.assertEqual(len({key(c["cells"]) for c in cases}), len(cases))
            for case in cases:
                self.assertTrue(valid(case["cells"], [0, 0]))
                self.assertEqual(sum(map(sum, case["cells"])), 2+k)
                self.assertEqual(case["cells"][0][:2], [1, 1])
        self.assertEqual(base, self.project["cells"])
        self.assertLess(plan["planned"], 100)

    def test_hundred_cases_when_space_is_large_enough(self):
        base = [[0]*11 for _ in range(11)]
        base[0][0] = 1
        for k in (1, 2, 3):
            cases, plan = sample_valid_additions(base, [0, 0], k, 100, 7)
            self.assertEqual(len(cases), 100)
            self.assertEqual(plan["planned"], 100)

    def test_sampling_cancellation(self):
        def cancel():
            raise ExperimentCancelled()
        with self.assertRaises(ExperimentCancelled):
            sample_valid_additions(self.project["cells"], [0, 0], 1, 100, 42, cancel=cancel)

    def test_threshold_does_not_reduce_requested_count(self):
        definition = prepare_addition_cases(self.project, dict(self.settings, exhaustive_threshold=1), self.root,
                                            Journal(self.root, lambda *a, **k: None), lambda: None)
        self.assertEqual([p["planned"] for p in definition["spaces"]], [3, 3])

    def test_insufficient_unique_cases_stop_before_matrix_or_feko(self):
        from experiments.runner import run_experiment
        project = copy.deepcopy(self.project)
        project.update(cells=[[1, 0], [0, 0]])
        with patch("experiments.runner.FekoFitnessBackend") as backend, \
                patch("experiments.runner.prepare") as matrix:
            report = run_experiment(project, dict(defaults("accuracy"), k_max=1, samples_per_k=10),
                                    self.root, lambda *a, **k: None)
        self.assertEqual(report["status"], "failed")
        self.assertIn("допустимых всего 2", report["error"])
        backend.assert_not_called()
        matrix.assert_not_called()

    def test_constraint_enumeration_matches_exhaustive_geometry_checks(self):
        base = self.project["cells"]
        positions = [(r, c) for r, row in enumerate(base) for c, v in enumerate(row) if not v]
        for connected in (False, True):
            for k in (1, 2, 3):
                expected, _ = sample_valid_additions(base, [0, 0], k, 100, 42, connected)
                actual, exhausted, calls = constraint_cases(base, [0, 0], k, positions, set(), 100,
                                                            connected, lambda: None)
                self.assertTrue(exhausted)
                self.assertEqual({key(c["cells"]) for c in actual}, {key(c["cells"]) for c in expected})

    def test_exact_completion_when_growth_does_not_find_variants(self):
        base = [[0]*7 for _ in range(7)]
        base[0][0] = 1
        with patch("experiments.addition_sampling._grow", return_value=None):
            cases, plan = sample_valid_additions(base, [0, 0], 3, 10, 42, True)
        self.assertEqual(len(cases), 10)
        self.assertGreater(plan["constraint_calls"], 0)
        self.assertFalse(plan["sampling_limit_reached"])
        self.assertTrue(all(valid(c["cells"], [0, 0], connected=True) for c in cases))

    def test_large_space_reports_proven_exhaustion_not_a_search_limit(self):
        base = [[0]*7 for _ in range(7)]
        base[0][0] = 1
        cases, plan = sample_valid_additions(base, [0, 0], 3, 100, 42, True)
        self.assertLess(len(cases), 100)
        self.assertTrue(plan["complete_enumeration"])
        self.assertGreater(plan["constraint_calls"], 0)
        self.assertFalse(plan["sampling_limit_reached"])

    def test_connected_strip_six_and_seven_additions_always_fill_count(self):
        base = [[0]*9 for _ in range(9)]
        base[4][1:8] = [1]*7
        for k in (6, 7):
            cases, plan = sample_valid_additions(base, [4, 4], k, 100, 42, True)
            self.assertEqual(len(cases), 100)
            self.assertEqual(len({key(c["cells"]) for c in cases}), 100)
            self.assertTrue(all(valid(c["cells"], [4, 4], connected=True) for c in cases))
            self.assertEqual(plan["planned"], 100)
            self.assertFalse(plan["sampling_limit_reached"])

    def test_cancel_retains_flat_partial_results(self):
        row = dict(from_impedance(40+5j, 3.2e9, 50), gain_peak_dbi=2)
        record = dict(rows=[row], compute_s=.01, online_s=1, fitness=-.1,
                      diagnostics=[dict(method="schur")], cached=False)
        schur = Mock(states={}, stats={}, preferred_parent=None)
        def evaluate(cells, **kwargs):
            schur.states[key(cells)] = object()
            return record
        schur.evaluate.side_effect = evaluate
        feko = Mock(stats={})
        feko.evaluate.return_value = dict(record, compute_s=.2)
        done = []
        def cancel():
            if done:
                raise ExperimentCancelled()
        with self.assertRaises(ExperimentCancelled):
            run_current_additions(self.project, self.settings, self.root, schur, feko,
                                  Journal(self.root, lambda *a, **k: None), cancel, lambda data: done.append(data))
        with (self.root / "addition-cases.csv").open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)
        self.assertIsNone(schur.preferred_parent)

    def test_current_base_and_forced_independent_evaluations(self):
        baseline = key(self.project["cells"])
        calls = []
        class Backend:
            def __init__(self, name):
                self.name, self.states, self.stats, self.preferred_parent = name, {}, {}, None
            def evaluate(inner, cells, *, force=False, context=None):
                parent = inner.preferred_parent
                if inner.name == "schur" and context.get("role") != "base_setup":
                    self.assertEqual(parent, baseline)
                    self.assertIn(baseline, inner.states)
                self.assertTrue(force)
                ident = key(cells)
                inner.states[ident] = object()
                if context.get("role") != "base_setup":
                    inner.states.pop(baseline, None)
                calls.append((inner.name, copy.deepcopy(cells), context))
                row = dict(from_impedance(40+5j, 3.2e9, 50), gain_peak_dbi=2)
                return dict(rows=[row], compute_s=.01 if inner.name == "schur" else .2,
                            online_s=1, fitness=-.1, diagnostics=[dict(method="schur")], cached=False)
        schur, feko = Backend("schur"), Backend("feko")
        outcome = run_current_additions(self.project, self.settings, self.root, schur, feko,
                                       Journal(self.root, lambda *a, **k: None), lambda: None, lambda data: None)
        self.assertEqual(calls[0][1], self.project["cells"])
        self.assertEqual(calls[0][2]["role"], "base_setup")
        self.assertEqual(len(outcome["pairs"]), 6)
        self.assertFalse(any(context["k"] == 0 for name, cells, context in calls if name == "feko"))
        self.assertIsNone(schur.preferred_parent)
        self.assertTrue((self.root / "addition-summary.csv").is_file())

    def test_flat_means_errors_and_empty_partial_headers(self):
        pairs = []
        for index, (ga, gb) in enumerate(((1, 2), (3, 2))):
            a = dict(from_impedance(40+5j, 3.2e9, 50), gain_peak_dbi=ga)
            b = dict(from_impedance(50+5j, 3.2e9, 50), gain_peak_dbi=gb)
            pairs.append(dict(k=1, case=index+1, candidate_id=str(index),
                              schur=dict(rows=[a], compute_s=.01*(index+1), diagnostics=[dict(method="schur")]),
                              feko=dict(rows=[b], compute_s=.2*(index+1)), errors=compare_rows([a], [b])))
        means = save_addition_tables(self.root, pairs, [dict(k=1, requested=100)], [3.2e9])
        self.assertAlmostEqual(means[0]["scuff_time_mean_s"], .015)
        self.assertAlmostEqual(means[0]["feko_time_mean_s"], .3)
        self.assertEqual(means[0]["gmax_error_db_mean"], 1)
        self.assertEqual(means[0]["gmax_scuff_mean_dbi"], means[0]["gmax_feko_mean_dbi"])
        self.assertEqual(means[0]["cases"], 2)
        with (self.root / "addition-cases.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2)
        self.assertIn("s11_error_relative", rows[0])
        save_addition_tables(self.root, [], [dict(k=1, requested=100)], [3.2e9])
        self.assertIn("added_elements", (self.root / "addition-cases.csv").read_text(encoding="utf-8-sig"))
