"""ThinWireMoM GeneticAlgorithmOperators adapted from wire edges to metal cells.

Preserved: top-parent all-pairs complementary crossover, offspring-only mutation,
minimum-distance assignment and probabilistic crowding. Geometry repair operates
on four-neighbour pixels; invalid/bounded masks revert explicitly to their parent.
"""
from collections import deque
from itertools import combinations
import math
import random

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.spatial.distance import cdist

from .domain import valid


def neighbours(i, n):
    r, c = divmod(i, n)
    return sorted(rr*n+cc for rr, cc in ((r-1, c), (r+1, c), (r, c-1), (r, c+1))
                  if 0 <= rr < n and 0 <= cc < n)


def component(active, feed, n):
    seen, todo = {feed}, [feed]
    while todo:
        for j in neighbours(todo.pop(), n):
            if j in active and j not in seen:
                seen.add(j)
                todo.append(j)
    return seen


def repair(cells, feed_cell, connect):
    n = len(cells)
    feed = feed_cell[0]*n+feed_cell[1]
    active = {i for i, v in enumerate(np.asarray(cells).ravel()) if v} | {feed}
    reached = component(active, feed, n)
    if not connect:
        active = reached
    else:
        # Same shortest-path repair policy as makeGraphConnected, on cell adjacency.
        while reached != active:
            queue, prev = deque(sorted(reached)), {i: None for i in reached}
            target = None
            while queue and target is None:
                i = queue.popleft()
                for j in neighbours(i, n):
                    if j in prev:
                        continue
                    prev[j] = i
                    if j in active and j not in reached:
                        target = j
                        break
                    queue.append(j)
            while target is not None:
                active.add(target)
                target = prev[target]
            reached = component(active, feed, n)
    return [[int(r*n+c in active) for c in range(n)] for r in range(n)]


def admissible(cells, project, settings):
    return valid(cells, project["feed_cell"], connected=True, min_elements=settings["min_elements"],
                 max_elements=settings["max_elements"] or len(cells)**2)


def crossover(a, b, project, settings, rng):
    aa, bb = np.asarray(a), np.asarray(b)
    picks = np.array([rng.random() < .5 for _ in range(aa.size)]).reshape(aa.shape)
    children, notes = [], []
    for raw, fallback in ((np.where(picks, aa, bb), a), (np.where(picks, bb, aa), b)):
        connect = rng.random() < settings["crossover_connect_probability"]
        child = repair(raw.tolist(), project["feed_cell"], connect)
        accepted = admissible(child, project, settings)
        children.append(child if accepted else [row[:] for row in fallback])
        notes.append(dict(connect=connect, rejected=not accepted))
    return children, notes


def mutation(cells, project, settings, rng):
    n = len(cells)
    active = {i for i, v in enumerate(np.asarray(cells).ravel()) if v}
    feed = project["feed_cell"][0]*n+project["feed_cell"][1]
    removing = rng.random() < settings["remove_method_probability"]
    picked, connect = None, False
    if removing:
        options = sorted(active-{feed})
        if options:
            picked = rng.choice(options)
            active.remove(picked)
        connect = rng.random() < settings["remove_connect_probability"]
    else:
        # Adapt the source's shuffled active-vertex / random incident-edge growth.
        order = sorted(active)
        rng.shuffle(order)
        for i in order:
            options = [j for j in neighbours(i, n) if j not in active]
            if options:
                picked = rng.choice(options)
                active.add(picked)
                break
    child = [[int(r*n+c in active) for c in range(n)] for r in range(n)]
    if removing:
        child = repair(child, project["feed_cell"], connect)
    accepted = admissible(child, project, settings)
    return (child if accepted else [row[:] for row in cells]), dict(
        operation="remove" if removing else "add", cell=picked, connect=connect, rejected=not accepted)


def offspring(population, weights, project, settings, seed, crossover_rate):
    rng = random.Random(seed)
    count = min(len(population), math.ceil(crossover_rate*len(population)))
    rank = sorted(range(len(population)), key=lambda i: (-weights[i], i))[:count]
    pairs = list(combinations(rank, 2))
    pair_seeds = [rng.randrange(2**63) for _ in pairs]
    selected = rng.sample(range(2*len(pairs)), math.ceil(2*len(pairs)*settings["offspring_mutation_rate"]))
    mutation_seeds = [rng.randrange(2**63) for _ in selected]
    children, notes = [], []
    # Separate operation streams prevent geometry-dependent draws from shifting later mutations.
    for (i, j), operation_seed in zip(pairs, pair_seeds):
        pair, detail = crossover(population[i], population[j], project, settings, random.Random(operation_seed))
        children.extend(pair)
        notes.extend(dict(parents=[i, j], crossover=d, crossover_seed=operation_seed) for d in detail)
    for i, operation_seed in zip(selected, mutation_seeds):
        children[i], notes[i]["mutation"] = mutation(children[i], project, settings, random.Random(operation_seed))
        notes[i]["mutation_seed"] = operation_seed
    return children, notes


def selection(parents, children, draws):
    """Minimum total Hamming assignment, then fc/(fc+fp); not tournament/elitism."""
    if not children:
        return list(parents), []
    a = np.array([r["cells"] for r in parents], dtype=np.int8).reshape(len(parents), -1)
    b = np.array([r["cells"] for r in children], dtype=np.int8).reshape(len(children), -1)
    distance = np.rint(cdist(a, b, metric="hamming")*a.shape[1]).astype(np.int64)
    pi, ci = linear_sum_assignment(distance)
    winners, notes = [], []
    # MATLAB matchpairs emits column-ordered pairs; RNG draws follow that order.
    for draw_index, (i, j) in enumerate(sorted(zip(pi.tolist(), ci.tolist()), key=lambda pair: pair[1])):
        fp, fc = parents[i]["fitness"], children[j]["fitness"]
        if not np.isfinite(fp+fc) or min(fp, fc) < 0:
            raise ValueError("ThinWireMoM crowding requires finite nonnegative fitness")
        probability = fc/(fc+fp) if fc+fp else .5
        choose_child = draws[draw_index] < probability
        winners.append(children[j] if choose_child else parents[i])
        notes.append(dict(parent=i, child=j, distance=int(distance[i, j]), probability=probability,
                          draw=draws[draw_index], child_wins=choose_child))
    matched = set(pi)
    winners.extend(p for i, p in enumerate(parents) if i not in matched)
    return winners, notes


def local_candidates(cells, project, settings):
    n = len(cells)
    active = {i for i, v in enumerate(np.asarray(cells).ravel()) if v}
    feed = project["feed_cell"][0]*n+project["feed_cell"][1]
    additions = sorted({j for i in active for j in neighbours(i, n)}-active)
    candidates, changes = [], []
    for operation, ids in (("add", additions), ("remove", sorted(active-{feed}))):
        for i in ids:
            candidate = [row[:] for row in cells]
            r, c = divmod(i, n)
            candidate[r][c] = int(operation == "add")
            if admissible(candidate, project, settings):
                candidates.append(candidate)
                changes.append(dict(operation=operation, cell=[r, c]))
    return candidates, changes
