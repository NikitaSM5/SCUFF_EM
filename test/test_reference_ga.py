"""Reference algorithm semantics on pixel masks; fake physics only in these tests."""
import copy
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.config import defaults, validate
from experiments.ga import run_ga
from experiments.metrics import fitness
from experiments.reference_ga import definition, validate_replay, Trajectory
from experiments.reference_operators import crossover, mutation, offspring, selection, repair, local_candidates
from experiments.storage import key, canonical, Journal
from gui.project import default_project


class Backend:
    def __init__(self, name):
        self.name, self.records, self.stats = name, [], {}

    def evaluate(self, cells, force=False, context=None):
        weight = sum((i+1)*v for i, v in enumerate(v for row in cells for v in row))
        value = float(weight if self.name == "schur" else 50-weight)
        row = dict(frequency_hz=3.2e9, resistance_ohm=50, reactance_ohm=0, s11_real=.1, s11_imag=0)
        result = dict(candidate_id=key(cells), cells=copy.deepcopy(cells), fitness=value,
                      rows=[row], online_s=.1, context=context or {})
        self.records.append(result)
        return result

    def evaluate_many(self, cells, contexts):
        return [self.evaluate(c, context=x) for c, x in zip(cells, contexts)]


class ReferenceGATests(unittest.TestCase):
    def setUp(self):
        self.project = default_project(".")
        self.project.update(cells=[[1, 0], [0, 0]], feed_cell=[0, 0])
        self.settings = dict(defaults("ga"), population_size=4, generations=2,
                             initial_elements=2, local_search_depth=1, child_fraction=.5)
        self.reference = patch("experiments.reference_ga.reference_metadata", return_value={"test_fixture": True})
        self.reference.start()
        self.addCleanup(self.reference.stop)

    def test_definition_is_shared_byte_for_byte(self):
        self.assertEqual(canonical(definition(self.project, self.settings)), canonical(definition(self.project, self.settings)))
        data = definition(self.project, self.settings)
        self.assertEqual(data["early_count"], 2)
        self.assertEqual(data["crossover_rate"], .5)
        self.assertTrue(all(sum(map(sum, c)) == 2 for c in data["initial_population"]))

    def test_all_pairs_produce_two_complementary_children(self):
        a, b = [[1, 1], [0, 0]], [[1, 0], [1, 0]]
        children, notes = offspring([a, b, a, b], [4, 3, 2, 1], self.project,
                                    dict(self.settings, offspring_mutation_rate=0), 11, .75)
        self.assertEqual(len(children), 6)
        self.assertEqual([r["parents"] for r in notes], [[0, 1], [0, 1], [0, 2], [0, 2], [1, 2], [1, 2]])
        self.assertTrue(all(c[0][0] == 1 for c in children))

    def test_mutates_only_selected_children_not_parents(self):
        population = [[[1, 1], [1, 1]]]*4
        original = copy.deepcopy(population)
        children, notes = offspring(population, [4, 3, 2, 1], self.project,
                                    dict(self.settings, offspring_mutation_rate=.5, remove_method_probability=1,
                                         remove_connect_probability=0), 12, .75)
        self.assertEqual(population, original)
        self.assertEqual(sum("mutation" in n for n in notes), 3)
        self.assertTrue(all(c[0][0] == 1 for c in children))

    def test_operation_substreams_do_not_shift_with_geometry(self):
        settings = dict(self.settings, offspring_mutation_rate=.5)
        _, a = offspring([[[1, 0], [0, 0]]]*4, [4, 3, 2, 1], self.project, settings, 21, .75)
        _, b = offspring([[[1, 1], [1, 1]]]*4, [4, 3, 2, 1], self.project, settings, 21, .75)
        self.assertEqual([(r["crossover_seed"], r.get("mutation_seed")) for r in a],
                         [(r["crossover_seed"], r.get("mutation_seed")) for r in b])

    def test_assignment_and_crowding_not_elitist(self):
        a, b = [[1, 1], [0, 0]], [[1, 0], [1, 0]]
        parents = [dict(cells=a, fitness=3), dict(cells=b, fitness=9)]
        children = [dict(cells=b, fitness=1), dict(cells=a, fitness=1)]
        winners, notes = selection(parents, children, [.2, .2])
        self.assertEqual([(r["parent"], r["child"]) for r in notes], [(1, 0), (0, 1)])
        self.assertIs(winners[0], parents[1])
        self.assertIs(winners[1], children[1])
        scaled = [dict(r, fitness=10*r["fitness"]) for r in parents+children]
        _, scaled_notes = selection(scaled[:2], scaled[2:], [.2, .2])
        self.assertEqual(notes, scaled_notes)
        with self.assertRaises(ValueError):
            selection([dict(cells=a, fitness=-1)], children[:1], [.5])

    def test_repair_and_local_neighbourhood_protect_feed(self):
        cells = [[1, 0, 1], [0, 0, 0], [0, 0, 0]]
        self.assertEqual(sum(map(sum, repair(cells, [0, 0], False))), 1)
        self.assertEqual(repair(cells, [0, 0], True)[0], [1, 1, 1])
        candidates, changes = local_candidates([[1, 1], [0, 0]], self.project, self.settings)
        self.assertTrue(all(c[0][0] == 1 for c in candidates))
        self.assertTrue(any(c["operation"] == "remove" for c in changes))

    def test_original_magnitude_impedance_fitness_is_distinct(self):
        row = dict(gain_peak_dbi=0, resistance_ohm=0, reactance_ohm=50, reference_ohm=50, s11_magnitude=1)
        self.assertEqual(fitness([row], "reference_gain"), 1)
        self.assertEqual(fitness([row], "realized_gain"), 0)
        with self.assertRaises(ValueError):
            validate(dict(self.settings, objective="reflection"), self.project)

    def test_replay_includes_every_local_search_and_offspring_evaluation(self):
        with tempfile.TemporaryDirectory() as temp:
            a, b = Backend("schur"), Backend("feko")
            outcome = run_ga(self.project, self.settings, Path(temp), a, b,
                             Journal(temp, lambda *a, **k: None), lambda: None, lambda data: None)
            self.assertEqual([r["cells"] for r in a.records], [r["cells"] for r in b.records[:-1]])
            self.assertIsNone(outcome["divergence_generation"])
            trace = json.loads((Path(temp)/"ga-trace-schur.json").read_text())
            self.assertEqual(len(trace["generations"]), 3)
            self.assertTrue(any("ls-" in batch["contexts"][0]["phase"] for batch in trace["batches"] if batch["contexts"]))
            validate_replay(trace, self.project, self.settings)
            trace["generations"][-1]["batch_end"] += 1
            with self.assertRaises(ValueError):
                validate_replay(trace, self.project, self.settings)

    def test_independent_uses_same_initial_population_and_can_diverge(self):
        with tempfile.TemporaryDirectory() as temp:
            a, b = Backend("schur"), Backend("feko")
            outcome = run_ga(self.project, dict(self.settings, mode="independent"), Path(temp), a, b,
                             Journal(temp, lambda *a, **k: None), lambda: None, lambda data: None)
            self.assertEqual([r["cells"] for r in a.records[:4]], [r["cells"] for r in b.records[:4]])
            self.assertIsNotNone(outcome["divergence_generation"])


if __name__ == "__main__":
    unittest.main()
