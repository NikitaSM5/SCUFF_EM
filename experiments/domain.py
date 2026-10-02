"""Pixel encoding, fixed feed and reproducible valid geometry generation."""
import itertools
import math
import random

from antenna.geometry import _validate_grid


def valid(cells, feed, *, connected=False, min_elements=1, max_elements=None):
    try:
        grid, _ = _validate_grid(cells, feed)
    except ValueError:
        return False
    count = int(grid.sum())
    if not min_elements <= count <= (grid.size if max_elements is None else max_elements):
        return False
    if connected:
        seen, todo = {tuple(feed)}, [tuple(feed)]
        while todo:
            r, c = todo.pop()
            for p in ((r-1, c), (r+1, c), (r, c-1), (r, c+1)):
                if 0 <= p[0] < len(grid) and 0 <= p[1] < len(grid) and grid[p] and p not in seen:
                    seen.add(p)
                    todo.append(p)
        if len(seen) != count:
            return False
    return True


def random_antenna(n, feed, count, seed, connected=False):
    if not 1 <= count <= n*n:
        raise ValueError("Initial element count must be inside the grid")
    rng = random.Random(seed)
    positions = [p for p in itertools.product(range(n), repeat=2) if p != tuple(feed)]
    for attempt in range(10000):
        cells = [[0]*n for _ in range(n)]
        cells[feed[0]][feed[1]] = 1
        if connected:
            for _ in range(count-1):
                allowed = []
                for r, c in positions:
                    if cells[r][c]:
                        continue
                    cells[r][c] = 1
                    if valid(cells, feed, connected=True):
                        allowed.append((r, c))
                    cells[r][c] = 0
                if not allowed:
                    break
                r, c = rng.choice(allowed)
                cells[r][c] = 1
        else:
            for r, c in rng.sample(positions, count-1):
                cells[r][c] = 1
        if sum(map(sum, cells)) == count and valid(cells, feed, connected=connected):
            return cells
    raise ValueError("Cannot generate a valid antenna under the selected constraints in 10000 attempts")


def spaces(base, k_max, sample_count, single_mode, multi_mode):
    empty = len(base)**2-sum(map(sum, base))
    return [dict(k=k, combinations=math.comb(empty, k),
                 planned=min(math.comb(empty, k), sample_count) if (single_mode if k == 1 else multi_mode) == "sample" else math.comb(empty, k))
            for k in range(1, min(k_max, empty)+1)]


def additions(base, feed, k_max, sample_count, single_mode, multi_mode, seed, connected=False):
    positions = [(r, c) for r, row in enumerate(base) for c, v in enumerate(row) if not v]
    for item in spaces(base, k_max, sample_count, single_mode, multi_mode):
        k = item["k"]
        if item["planned"] == item["combinations"]:
            combinations = itertools.combinations(positions, k)
        else:
            rng = random.Random(f"{seed}:{k}")
            chosen = set()
            while len(chosen) < item["planned"]:
                chosen.add(tuple(sorted(rng.sample(positions, k))))
            combinations = sorted(chosen)
        for added in combinations:
            cells = [row[:] for row in base]
            for r, c in added:
                cells[r][c] = 1
            yield dict(k=k, added=list(added), cells=cells, valid=valid(cells, feed, connected=connected))


def incremental(base, feed, k_max, seed, connected=False):
    rng = random.Random(seed)
    cells = [row[:] for row in base]
    for k in range(1, k_max+1):
        allowed = [item for item in additions(cells, feed, 1, 1, "exhaustive", "exhaustive", seed, connected) if item["valid"]]
        if not allowed:
            return
        item = rng.choice(allowed)
        cells = item["cells"]
        yield dict(item, k=k)


def sample_valid_additions(base, feed, k, count, seed, connected=False, *, cancel=lambda: None):
    """Unique admissible masks, each independently added to the unchanged base."""
    from .addition_sampling import sample
    return sample(base, feed, k, count, seed, connected, cancel=cancel)
