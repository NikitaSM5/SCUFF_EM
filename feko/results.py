"""Read the one-port Touchstone and spherical Gain exports requested by our Lua."""
import cmath
import csv
import json
import math
from pathlib import Path
from antenna.parameters import from_reflection


def read_s11(path):
    unit, kind, mode, marker, reference = "GHZ", "S", "MA", "R", "50"
    rows = []
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("!", 1)[0].strip()
        if not line:
            continue
        if line.startswith("#"):
            unit, kind, mode, marker, reference = line[1:].upper().split()
            continue
        if kind != "S" or marker != "R" or mode not in ("MA", "DB", "RI"):
            raise ValueError("Unsupported one-port Touchstone format")
        scale = {"HZ": 1, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}[unit]
        frequency, first, second = map(float, line.split())
        if not all(math.isfinite(v) for v in (frequency, first, second, float(reference))):
            raise ValueError("Non-finite S11 data")
        if mode == "RI":
            value = complex(first, second)
        else:
            value = cmath.rect(10**(first/20) if mode == "DB" else first, math.radians(second))
        rows.append(from_reflection(value, frequency*scale, float(reference)))
    if not rows:
        raise ValueError("S11 export contains no samples")
    return rows


def read_farfield(path):
    blocks, block = [], None
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("#Frequency:"):
            block = dict(frequency_hz=float(line.split(":", 1)[1]), peak=None, samples=0, points=[])
            blocks.append(block)
        elif line.startswith("#Coordinate System:") and line.split(":", 1)[1].strip() != "Spherical":
            raise ValueError("Expected spherical far-field coordinates")
        elif line.startswith("#Result Type:") and line.split(":", 1)[1].strip() != "Gain":
            raise ValueError("Expected Gain, not Directivity or RCS")
        elif line and not line.startswith(("#", "*")):
            values = list(map(float, line.split()))
            if block is None or len(values) != 9 or not all(math.isfinite(v) for v in values):
                raise ValueError("Invalid far-field Gain sample")
            theta, phi, gain = values[0], values[1], values[8]
            block["samples"] += 1
            block["points"].append([theta, phi, gain])
            if block["peak"] is None or gain > block["peak"]["gain_peak_dbi"]:
                block["peak"] = dict(gain_peak_dbi=gain, theta_deg=theta, phi_deg=phi)
    if not blocks or any(b["peak"] is None for b in blocks):
        raise ValueError("Gain export contains an empty frequency block")
    return blocks


def read_gain_peaks(path):
    return [{k: v for k, v in block.items() if k != "points"} for block in read_farfield(path)]


def save_results(directory, compute_pattern=True):
    directory = Path(directory)
    s11 = read_s11(directory / "antenna_SParameter.s1p")
    peaks = read_farfield(directory / "antenna_Gain_UpperHemisphere.ffe") if compute_pattern else [None]*len(s11)
    if len(s11) != len(peaks):
        raise ValueError("S11 and Gain have different frequency counts")
    rows = []
    for reflection, farfield in zip(s11, peaks):
        if farfield is None:
            rows.append(dict(reflection, gain_peak_dbi=None, theta_deg=None, phi_deg=None, angular_samples=0))
            continue
        if not math.isclose(reflection["frequency_hz"], farfield["frequency_hz"], rel_tol=1e-7):
            raise ValueError("S11 and Gain frequencies do not match")
        rows.append(dict(reflection, **farfield["peak"], angular_samples=farfield["samples"]))
    if compute_pattern:
        (directory / "farfield.json").write_text(json.dumps(peaks, allow_nan=False), encoding="utf-8")
    document = dict(gain_definition="Gain, not realised gain; maximum over sampled upper hemisphere",
                    s11_zero_db_representation="null means zero magnitude (-infinity dB)", samples=rows)
    (directory / "summary.json").write_text(json.dumps(document, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    with (directory / "summary.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return rows
