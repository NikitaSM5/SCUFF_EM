"""Versioned, portable GUI project data (independent of Qt)."""
import json
import math
from pathlib import Path
from feko.config import default_options, validate_options

FEED_MODEL = "planar_delta_gap_voltage"


def default_project(output_dir):
    return dict(schema_version=1, cells=[[0] * 8 for _ in range(8)], feed_cell=None,
                feed_model=FEED_MODEL,
                cell_size_mm=5.0, thickness_mm=5.0, epsilon_r=4.4, loss_tangent=0.0,
                frequency_ghz=3.2, mesh_size_mm=None, max_unknowns=2000,
                timeout_s=600, verify_reference=False,
                output_dir=str(output_dir), feko=default_options())


def validate_project(data, *, for_run=False):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported project format")
    if data.get("feed_model", FEED_MODEL) != FEED_MODEL:
        raise ValueError("Unsupported feed model")
    cells = data.get("cells")
    if not isinstance(cells, list) or not 1 <= len(cells) <= 128:
        raise ValueError("Grid size must be between 1 and 128")
    n = len(cells)
    if any(not isinstance(row, list) or len(row) != n or
           any(type(v) is not int or v not in (0, 1) for v in row) for row in cells):
        raise ValueError("Expected a square matrix of integer zeros and ones")
    feed = data.get("feed_cell")
    if feed is not None:
        if (not isinstance(feed, (list, tuple)) or len(feed) != 2 or
                any(type(v) is not int or not 0 <= v < n for v in feed) or
                cells[feed[0]][feed[1]] != 1):
            raise ValueError("Feed must reference a metal cell")
    limits = {"cell_size_mm": (0.001, 100000), "thickness_mm": (0.001, 100000),
              "epsilon_r": (0.0001, 10000), "loss_tangent": (0, 100),
              "frequency_ghz": (0.000001, 10000)}
    if data.get("mesh_size_mm") is not None:
        limits["mesh_size_mm"] = (0.0001, 100000)
    for key, (lo, hi) in limits.items():
        v = data.get(key)
        if type(v) not in (int, float) or not math.isfinite(v) or not lo <= v <= hi:
            raise ValueError(f"Invalid {key}: expected {lo} .. {hi}")
    for key, hi in (("max_unknowns", 100000), ("timeout_s", 86400)):
        if type(data.get(key)) is not int or not 1 <= data[key] <= hi:
            raise ValueError(f"Invalid {key}")
    if type(data.get("verify_reference")) is not bool:
        raise ValueError("Invalid verify_reference")
    if not isinstance(data.get("output_dir"), str) or not data["output_dir"].strip():
        raise ValueError("Output directory is required")
    validate_options(data.get("feko", {}))
    if for_run:
        if feed is None:
            raise ValueError("Выберите питающую металлическую клетку инструментом «Порт».")
        for r in range(n - 1):
            for c in range(n - 1):
                a, b = cells[r][c:c+2]
                d, e = cells[r+1][c:c+2]
                if a == e and b == d and a != b:
                    raise ValueError(f"Точечный контакт металла у ({r}, {c}). Добавьте перемычку или зазор.")
    return data


def read_project(path):
    return validate_project(json.loads(Path(path).read_text(encoding="utf-8")))
