"""Generate a pixel antenna, solve it, and save M.npy plus the complete system.npz.

Run from any directory: py -3.11 test/run_planar.py
For a custom grid: --cells mask.csv --feed ROW COLUMN --cell-mm 5 --frequency-ghz 3.2
"""
import argparse
from datetime import datetime
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from antenna import Substrate, create_planar_antenna, solve_antenna, save_matrix, save_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cells", type=Path, help="Square numeric 0/1 matrix: .npy or comma-separated .csv")
    parser.add_argument("--feed", nargs=2, type=int, metavar=("ROW", "COLUMN"), help="Zero-based feed cell")
    parser.add_argument("--cell-mm", type=float, default=5.0)
    parser.add_argument("--mesh-mm", type=float)
    parser.add_argument("--frequency-ghz", type=float, default=3.2)
    parser.add_argument("--height-mm", type=float, default=5.0)
    parser.add_argument("--epsilon-r", type=float, default=4.4)
    parser.add_argument("--loss-tangent", type=float, default=0.0)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--verify-reference", action="store_true")
    args = parser.parse_args()
    if args.cells:
        if args.feed is None:
            parser.error("--feed ROW COLUMN is required with --cells")
        if args.cells.suffix.lower() == ".npy":
            cells = np.load(args.cells, allow_pickle=False)
        elif args.cells.suffix.lower() == ".csv":
            cells = np.loadtxt(args.cells, delimiter=",", ndmin=2)
        else:
            parser.error("--cells must be .csv or .npy")
        feed = tuple(args.feed)
    else:
        cells = [[0, 1, 0], [1, 1, 1], [0, 1, 0]]
        feed = tuple(args.feed) if args.feed else (1, 1)
    out = (args.out or ROOT / "results/planar-demo" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")).resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        parser.error(f"--out must be an empty directory: {out}")
    substrate = Substrate(args.height_mm, args.epsilon_r, args.loss_tangent)
    geometry = create_planar_antenna(cells, args.cell_mm, feed, out / "geometry",
                                     substrate=substrate, mesh_size_mm=args.mesh_mm)
    print(f"Geometry: {geometry.metadata['mesh']}", flush=True)
    result = solve_antenna(geometry, args.frequency_ghz, work_dir=out / "calculation",
                           verify_reference=args.verify_reference)
    print("Matrix:", save_matrix(result.M, out / "M.npy"))
    print("Full system:", save_result(result, out / "system.npz"))
    print(f"M shape: {result.M.shape}; b, x shape: {result.b.shape}")
    print("Checks:", result.metadata["checks"])


if __name__ == "__main__":
    main()
