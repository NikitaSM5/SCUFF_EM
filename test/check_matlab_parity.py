"""Compare Python operators with JSON produced by check_thinwire_reference.m."""
import json
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.config import defaults
from experiments.metrics import fitness
from experiments.reference_operators import crossover, selection


def main(path):
    data = json.loads(Path(path).read_text())
    a = [[1, 1, 0], [0, 0, 0], [0, 0, 0]]
    b = [[1, 1, 1], [0, 0, 0], [0, 0, 0]]
    draws = iter(data["crossoverDraws"])

    class Draws:
        def random(self):
            return next(draws)

    children, _ = crossover(a, b, dict(feed_cell=[0, 0]),
                             dict(defaults("ga"), crossover_connect_probability=0), Draws())
    np.testing.assert_array_equal(np.asarray(children).reshape(2, 9), data["children"])
    parents = [dict(cells=a, fitness=3), dict(cells=b, fitness=9)]
    children = [dict(cells=b, fitness=1), dict(cells=a, fitness=1)]
    winners, _ = selection(parents, children, data["selectionDraws"])
    np.testing.assert_array_equal(np.array([r["cells"] for r in winners]).reshape(2, 9), data["winners"])
    np.testing.assert_array_equal([r["fitness"] for r in winners], data["winnerFitness"])
    value = fitness([dict(gain_peak_dbi=10*math.log10(2), resistance_ohm=30, reactance_ohm=40,
                           reference_ohm=50)], "reference_gain")
    np.testing.assert_allclose(value, data["fitness"], rtol=1e-14)
    print("MATLAB/PYTHON PARITY OK: complementary crossover, assignment/crowding, magnitude-Z fitness")


if __name__ == "__main__":
    main(sys.argv[1])
