"""Solver-work comparisons exclude lookup hits and whole-process overhead."""
def compute_value(record):
    return None if record.get("cached", False) else record.get("compute_s")


def compute_total(records):
    return sum(value for record in records if (value := compute_value(record)) is not None)


def compute_speedup(schur, feko):
    a, b = compute_value(schur), compute_value(feko)
    return b/a if a is not None and a > 0 and b is not None else None
