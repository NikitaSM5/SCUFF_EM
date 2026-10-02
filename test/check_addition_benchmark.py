"""Real Qt/current-base/SCUFF-Schur/FEKO smoke with flat CSV verification."""
import csv
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from check_benchmarks import wait_until
from experiments.config import defaults
from experiments.storage import key
from gui.app import AntennaWindow, create_application
from gui.project import default_project
from PyQt5 import QtCore, QtTest


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    app = create_application([])
    window = AntennaWindow()
    root = ROOT / "results" / ("addition-smoke-"+datetime.now().strftime("%Y%m%d-%H%M%S"))
    root.mkdir()
    project = default_project(root)
    project.update(cells=[[1, 1, 0], [0, 0, 0], [0, 0, 0]], feed_cell=[0, 0],
                   cell_size_mm=10, mesh_size_mm=5)
    project["feko"].update(compute_pattern=True, angle_step_deg=30, timeout_s=120)
    window.apply_project(project)
    panel = window.accuracy_panel
    panel.apply_settings(dict(defaults("accuracy"), k_max=2, samples_per_k=3))
    window.resultsTabs.setCurrentWidget(panel)
    window.show()
    try:
        QtTest.QTest.mouseClick(panel.runButton, QtCore.Qt.LeftButton)
        assert window.busy
        print("START", window.run_dir, flush=True)
        wait_until(lambda: not window.busy)
        directory = window.run_dir
        report = json.loads((directory / "summary.json").read_text())
        assert report["status"] == "completed", report
        definition = json.loads((directory / "addition-definition.json").read_text())
        assert definition["base"] == project["cells"]
        with (directory / "addition-cases.csv").open(encoding="utf-8-sig", newline="") as stream:
            cases = list(csv.DictReader(stream))
        with (directory / "addition-summary.csv").open(encoding="utf-8-sig", newline="") as stream:
            means = list(csv.DictReader(stream))
        assert len(cases) == 6 and len(means) == 2, (cases, means)
        assert all(row["schur_method"] == "schur" for row in cases), cases
        records = [json.loads(line) for line in (directory / "evaluations.jsonl").read_text().splitlines()]
        baseline = key(project["cells"])
        feko = [r for r in records if r["backend"] == "feko"]
        assert len({r["diagnostics"]["session"]["pid"] for r in feko}) == 1
        for record in records:
            assert not record["cached"] and record["compute_s"] > 0
            if record["backend"] == "schur" and record["context"].get("role") != "base_setup":
                assert record["parent_candidate_id"] == baseline, record
        assert report["solvers"]["schur"]["evaluations"] == 6
        assert report["base_setup_compute_s"] > 0
        for row in means:
            group = [r for r in cases if r["added_elements"] == row["added_elements"]]
            assert int(row["cases"]) == 3
            for field, average in (("scuff_time_s", "scuff_time_mean_s"), ("feko_time_s", "feko_time_mean_s"),
                                   ("gmax_error_db", "gmax_error_db_mean"), ("s11_error_db", "s11_error_db_mean")):
                expected = statistics.mean(float(r[field]) for r in group)
                assert abs(float(row[average])-expected) < 1e-10
        for width, height in ((1180, 820), (900, 650)):
            window.resize(width, height)
            for page, label in ((panel.settingsTab, "settings"), (panel.progressTab, "progress")):
                panel.pages.setCurrentWidget(page)
                QtTest.QTest.qWait(100)
                assert window.width() == width and window.height() == height
                assert window.grab().save(str(directory / f"gui-{label}-{width}.png"))
        print("ADDITION BENCHMARK OK", directory, flush=True)
        print("MEANS", means, flush=True)
    finally:
        if window.busy:
            window.cancel_run()
            wait_until(lambda: not window.busy, 45)
        window.dirty = False
        window.close()
        app.processEvents()


if __name__ == "__main__":
    main()
