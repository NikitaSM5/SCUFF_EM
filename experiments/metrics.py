"""Physical objectives and signed/complex-aware comparison metrics."""
import numpy as np


OBJECTIVES = {"reflection": "Min reflected power", "gain": "Max Gain", "realized_gain": "Max realised Gain",
              "reference_gain": "Fitness ThinWireMoM"}


def fitness(rows, objective):
    if objective == "reflection":
        return -max(row["s11_magnitude"]**2 for row in rows)
    gains = [row.get("gain_peak_dbi") for row in rows]
    if any(v is None for v in gains):
        raise ValueError("Selected objective requires a valid Gain at every frequency")
    linear = [10**(v/10) for v in gains]
    if objective == "realized_gain":
        linear = [g*max(0, 1-row["s11_magnitude"]**2) for g, row in zip(linear, rows)]
    elif objective == "reference_gain":
        weighted = []
        for g, row in zip(linear, rows):
            z = abs(complex(row["resistance_ohm"], row["reactance_ohm"]))
            z0 = row.get("reference_ohm", 50.0)
            gamma = (z-z0)/(z+z0)
            weighted.append(g*(1-gamma**2))
        linear = weighted
    elif objective != "gain":
        raise ValueError("Unknown objective")
    return min(linear)


def distribution(values):
    values = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    if not len(values):
        return dict(count=0, mean=None, median=None, p95=None, max=None)
    return dict(count=len(values), mean=float(np.mean(values)), median=float(np.median(values)),
                p95=float(np.percentile(values, 95)), max=float(np.max(values)))


def discrepancy(value, reference, floor):
    absolute = float(abs(value-reference))
    return dict(absolute=absolute, relative=absolute/max(float(abs(reference)), floor), reference_floor=floor)


def compare_rows(schur, reference):
    if len(schur) != len(reference):
        raise ValueError("Solver frequency count mismatch")
    result = []
    for a, b in zip(schur, reference):
        if abs(a["frequency_hz"]-b["frequency_hz"]) > max(1, a["frequency_hz"]*1e-7):
            raise ValueError("Solver frequency mismatch")
        za, zb = (complex(row["resistance_ohm"], row["reactance_ohm"]) for row in (a, b))
        sa, sb = (complex(row["s11_real"], row["s11_imag"]) for row in (a, b))
        block = dict(frequency_hz=a["frequency_hz"], impedance=discrepancy(za, zb, 1.0),
                     s11_complex=discrepancy(sa, sb, 1e-3),
                     reflected_power=discrepancy(abs(sa)**2, abs(sb)**2, 1e-6))
        if a.get("gain_peak_dbi") is not None and b.get("gain_peak_dbi") is not None:
            block["gain_linear"] = discrepancy(10**(a["gain_peak_dbi"]/10), 10**(b["gain_peak_dbi"]/10), 1e-6)
            block["gain_db_absolute"] = abs(a["gain_peak_dbi"]-b["gain_peak_dbi"])
        result.append(block)
    return result
