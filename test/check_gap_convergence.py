"""Opt-in real SCUFF/FEKO mesh study; retains every native input and result."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gui.calculation import run_comparison, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--meshes", nargs="+", type=float, default=[2.5, 1.25, .625])
    args = parser.parse_args()
    project = json.loads(args.project.read_text(encoding="utf-8"))
    results = []
    for mesh in args.meshes:
        config = deepcopy(project)
        config["mesh_size_mm"] = mesh
        config["verify_reference"] = False
        config["feko"] = dict(config.get("feko", {}), sweep_enabled=False, compute_pattern=True)
        directory = args.directory / f"mesh-{mesh:g}"
        directory.mkdir(parents=True, exist_ok=False)
        write_json(directory / "project.json", config)
        run_comparison(config, directory, lambda event, **kw: print(event, kw.get("message", ""), flush=True))
        data = json.loads((directory / "comparison.json").read_text(encoding="utf-8"))
        a, b = data["scuff"][0], data["feko"][0]
        for row in (a, b):
            if not row["passive"]:
                raise AssertionError(f"Non-passive port at mesh {mesh}: {row}")
        za = complex(a["resistance_ohm"], a["reactance_ohm"])
        zb = complex(b["resistance_ohm"], b["reactance_ohm"])
        results.append(dict(mesh_size_mm=mesh, scuff=a, feko=b,
                            relative_impedance_difference=abs(za-zb)/abs(zb)))
        write_json(args.directory / "convergence.json", results)
        print(json.dumps(results[-1], indent=2), flush=True)


if __name__ == "__main__":
    main()
