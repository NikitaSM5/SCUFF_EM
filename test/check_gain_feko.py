"""Opt-in independent FEKO comparison of a lossy, asymmetric antenna pattern."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from gui.calculation import run_comparison, write_json
from gui.project import default_project


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    config = default_project(directory)
    config.update(cells=[[1, 0, 0], [1, 1, 0], [0, 1, 1]], feed_cell=[1, 1],
                  cell_size_mm=8., mesh_size_mm=2., loss_tangent=.02)
    write_json(directory / "project.json", config)
    run_comparison(config, directory, lambda event, **kw: print(event, kw.get("message", ""), flush=True))
    data = json.loads((directory / "comparison.json").read_text(encoding="utf-8"))
    scuff = json.loads((directory / "scuff/gain.json").read_text(encoding="utf-8"))
    feko = json.loads((directory / "feko/farfield.json").read_text(encoding="utf-8"))[0]
    actual = {(theta, phi): gain for theta, phi, gain in scuff["gain_linear_points"]}
    errors = []
    for theta, phi, db in feko["points"]:
        if db > feko["peak"]["gain_peak_dbi"]-20 and actual[theta, phi] > 0:
            errors.append(10*np.log10(actual[theta, phi])-db)
    report = dict(scuff_peak_dbi=data["scuff"][0]["gain_peak_dbi"],
                  feko_peak_dbi=data["feko"][0]["gain_peak_dbi"],
                  pattern_max_difference_db=max(abs(np.array(errors))),
                  pattern_rms_difference_db=float(np.sqrt(np.mean(np.array(errors)**2))),
                  scuff_upper_efficiency=scuff["radiation_efficiency_upper"])
    write_json(directory / "gain-check.json", report)
    print(json.dumps(report, indent=2), flush=True)
    assert abs(report["scuff_peak_dbi"]-report["feko_peak_dbi"]) < .2
    assert report["pattern_max_difference_db"] < .8
    assert 0 < report["scuff_upper_efficiency"] < 1.02


if __name__ == "__main__":
    main()
