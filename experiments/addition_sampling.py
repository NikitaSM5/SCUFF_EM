"""Construct valid additions; use HiGHS feasibility to complete or exhaust a space."""
import itertools
import math
import random

from .domain import valid


def _case(base, added, k):
    cells = [row[:] for row in base]
    for r, c in added:
        cells[r][c] = 1
    return dict(k=k, added=list(sorted(added)), cells=cells, valid=True)


def _local_contact_ok(cells, r, c):
    n = len(cells)
    for i in range(max(0, r-1), min(r+1, n-1)):
        for j in range(max(0, c-1), min(c+1, n-1)):
            a, b = cells[i][j], cells[i][j+1]
            d, e = cells[i+1][j], cells[i+1][j+1]
            if abs(a+e-b-d) > 1:
                return False
    return True


def _grow(base, positions, k, rng, connected, cancel):
    cells = [row[:] for row in base]
    added = []
    n = len(cells)
    for _ in range(k):
        cancel()
        allowed = []
        for r, c in positions:
            if cells[r][c]:
                continue
            if connected and not any(0 <= i < n and 0 <= j < n and cells[i][j]
                                     for i, j in ((r-1, c), (r+1, c), (r, c-1), (r, c+1))):
                continue
            cells[r][c] = 1
            if _local_contact_ok(cells, r, c):
                allowed.append((r, c))
            cells[r][c] = 0
        if not allowed:
            return None
        r, c = rng.choice(allowed)
        cells[r][c] = 1
        added.append((r, c))
    return tuple(sorted(added))


def constraint_cases(base, feed, k, positions, excluded, count, connected, cancel):
    """Enumerate feasible binary masks, never treating a timeout as exhaustion."""
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix, vstack

    n, m = len(base), len(positions)
    indices = {p: i for i, p in enumerate(positions)}
    edges = []
    if connected:
        for i, (r, c) in enumerate(positions):
            touches_base = False
            for p in ((r-1, c), (r+1, c), (r, c-1), (r, c+1)):
                if p in indices:
                    edges.append((i, indices[p]))
                elif 0 <= p[0] < n and 0 <= p[1] < n and base[p[0]][p[1]]:
                    touches_base = True
            if touches_base:
                edges.append((-1, i))
    width = m+len(edges)
    values, row_ids, columns, lower, upper = [], [], [], [], []

    def add_row(coefficients, lo, hi):
        row = len(lower)
        for col, value in coefficients.items():
            if value:
                row_ids.append(row)
                columns.append(col)
                values.append(value)
        lower.append(lo)
        upper.append(hi)

    add_row(dict.fromkeys(range(m), 1), k, k)
    for r in range(n-1):
        for c in range(n-1):
            coefficients, constant = {}, 0
            # |NW + SE - NE - SW| <= 1 excludes both point-only contacts.
            for p, sign in (((r, c), 1), ((r+1, c+1), 1), ((r, c+1), -1), ((r+1, c), -1)):
                if p in indices:
                    coefficients[indices[p]] = sign
                else:
                    constant += sign*base[p[0]][p[1]]
            add_row(coefficients, -1-constant, 1-constant)
    if connected:
        balances = [{i: -1} for i in range(m)]
        # Each new metal cell consumes one unit of flow from the connected base.
        for offset, (source, target) in enumerate(edges):
            flow = m+offset
            balances[target][flow] = 1
            add_row({flow: 1, target: -k}, -np.inf, 0)
            if source >= 0:
                balances[source][flow] = -1
                add_row({flow: 1, source: -k}, -np.inf, 0)
        for balance in balances:
            add_row(balance, 0, 0)
    matrix = coo_matrix((values, (row_ids, columns)), shape=(len(lower), width)).tocsc()
    bounds = Bounds(np.zeros(width), np.r_[np.ones(m), np.full(len(edges), k)])
    integrality = np.r_[np.ones(m, dtype=int), np.zeros(len(edges), dtype=int)]
    forbidden = list(sorted(excluded))
    found, calls, time_limit = [], 0, .5
    while len(found) < count:
        cancel()
        cut_rows, cut_cols = [], []
        for row, added in enumerate(forbidden):
            for p in added:
                cut_rows.append(row)
                cut_cols.append(indices[p])
        cuts = coo_matrix((np.ones(len(cut_cols)), (cut_rows, cut_cols)), shape=(len(forbidden), width)).tocsc()
        constraint = LinearConstraint(vstack((matrix, cuts), format="csc"),
                                      np.r_[lower, np.full(len(forbidden), -np.inf)],
                                      np.r_[upper, np.full(len(forbidden), k-1)])
        # A zero objective asks for any feasible antenna, not an optimal geometry.
        result = milp(np.zeros(width), integrality=integrality, bounds=bounds, constraints=constraint,
                      options=dict(time_limit=time_limit))
        calls += 1
        cancel()
        if result.status == 2:
            return found, True, calls
        if result.status == 1:
            time_limit = min(30, time_limit*2)
            continue
        if result.status != 0 or result.x is None:
            raise RuntimeError("Addition constraint solver failed: "+str(result.message))
        added = tuple(sorted(positions[i] for i in range(m) if result.x[i] > .5))
        case = _case(base, added, k)
        if len(added) != k or added in forbidden or not valid(case["cells"], feed, connected=connected):
            raise RuntimeError("Addition constraint solver returned an invalid or repeated geometry")
        forbidden.append(added)
        found.append(case)
        time_limit = .5
    return found, False, calls


def sample(base, feed, k, count, seed, connected=False, *, cancel=lambda: None):
    cancel()
    if not valid(base, feed, connected=connected):
        raise ValueError("Invalid base antenna for addition sampling")
    positions = [(r, c) for r, row in enumerate(base) for c, value in enumerate(row) if not value]
    if type(k) is not int or not 1 <= k <= len(positions) or type(count) is not int or count < 1:
        raise ValueError("Invalid addition count or sample size")
    combinations = math.comb(len(positions), k)
    wanted = min(count, combinations)
    rng = random.Random(f"{seed}:current-additions:{k}")
    cases, attempted, exhausted, calls = [], 0, False, 0
    if combinations <= 10000:
        choices = list(itertools.combinations(positions, k))
        rng.shuffle(choices)
        for added in choices:
            cancel()
            attempted += 1
            case = _case(base, added, k)
            if valid(case["cells"], feed, connected=connected):
                cases.append(case)
                if len(cases) == wanted:
                    break
        exhausted = attempted == combinations
        algorithm = "exhaustive_shuffle"
    else:
        seen = set()
        for _ in range(max(64, count*8)):
            cancel()
            attempted += 1
            added = _grow(base, positions, k, rng, connected, cancel)
            if added is not None and added not in seen:
                seen.add(added)
                cases.append(_case(base, added, k))
                if len(cases) == wanted:
                    break
        if len(cases) < wanted:
            rng.shuffle(positions)
            remainder, exhausted, calls = constraint_cases(base, feed, k, positions, seen, wanted-len(cases), connected, cancel)
            cases.extend(remainder)
        algorithm = "constructive_growth_with_exact_constraints"
    return cases, dict(k=k, combinations=combinations, requested=count, planned=len(cases), attempted=attempted,
                       complete_enumeration=exhausted, sampling_limit_reached=False, algorithm=algorithm,
                       uniform_sample=combinations <= 10000, constraint_calls=calls)
