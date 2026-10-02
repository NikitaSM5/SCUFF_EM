"""Repeat an existing calculation on its unchanged mesh and compare outputs."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from antenna import load_geometry, solve_antenna, save_matrix, save_result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    baseline = args.baseline.resolve()
    output = args.output or ROOT / "results/performance" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output.mkdir(parents=True, exist_ok=False)
    old = json.loads((baseline / "calculation/summary.json").read_text(encoding="utf-8"))
    geometry = load_geometry(baseline / "geometry")
    start = time.perf_counter()
    result = solve_antenna(geometry, old["frequency_ghz"], work_dir=output / "calculation")
    save_matrix(result.M, output / "M.npy")
    save_result(result, output / "system.npz")
    report = dict(baseline=str(baseline), output=str(output.resolve()),
                  total_wall_s=time.perf_counter()-start,
                  old_process_s=old["process_wall_s"], new_process_s=result.metadata["process_wall_s"],
                  speedup=old["process_wall_s"]/result.metadata["process_wall_s"],
                  old_timings=old["timings_s"], new_timings=result.metadata["timings_s"],
                  checks=result.metadata["checks"], relative_differences={})
    with np.load(baseline / "system.npz", allow_pickle=False) as previous:
        for name in ("M", "b", "x"):
            report["relative_differences"][name] = float(np.linalg.norm(getattr(result, name)-previous[name]) / np.linalg.norm(previous[name]))
        for name in ("vertices_mm", "triangles", "rwg"):
            np.testing.assert_array_equal(getattr(result, name), previous[name])
        report["unchanged_mesh_and_basis"] = True
    text = json.dumps(report, indent=2, allow_nan=False)
    (output / "comparison.json").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
