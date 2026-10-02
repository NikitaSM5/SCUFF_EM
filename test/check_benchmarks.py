"""Real GUI/SCUFF/FEKO smoke experiments; uses the explicitly selected new binary GA."""
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gui.app import AntennaWindow, create_application
from gui.project import default_project
from experiments.config import defaults
from PyQt5 import QtCore, QtTest


def wait_until(predicate, timeout=900):
    deadline = time.monotonic()+timeout
    while not predicate() and time.monotonic() < deadline:
        QtTest.QTest.qWait(50)
    if not predicate():
        raise TimeoutError("Benchmark GUI smoke timeout")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    app = create_application([])
    window = AntennaWindow()
    reference = "--reference" in sys.argv
    arguments = [arg for arg in sys.argv[1:] if arg != "--reference"]
    root = ROOT / "results" / (("thinwire-smoke-" if reference else "benchmark-smoke-")+datetime.now().strftime("%Y%m%d-%H%M%S"))
    root.mkdir()
    cache_dir = str(Path(arguments[0]).resolve()) if arguments else str(root / "cache")
    project = default_project(root)
    project.update(cells=[[1, 0], [0, 0]], feed_cell=[0, 0], cell_size_mm=10, mesh_size_mm=5)
    project["feko"].update(angle_step_deg=30, timeout_s=120)
    window.apply_project(project)
    window.show()
    reports = []

    def run(panel, settings):
        panel.apply_settings(settings)
        window.resultsTabs.setCurrentWidget(panel)
        QtTest.QTest.qWait(100)
        QtTest.QTest.mouseClick(panel.runButton, QtCore.Qt.LeftButton)
        assert window.busy, "Experiment did not start"
        print("START", settings["kind"], settings["mode"], window.run_dir, flush=True)
        wait_until(lambda: not window.busy)
        report = json.loads((window.run_dir / "summary.json").read_text())
        print("DONE", report["status"], "Schur", report["solvers"].get("schur"), "FEKO", report["solvers"].get("feko"), flush=True)
        assert report["status"] == "completed", report
        assert (window.run_dir / "evaluations.csv").is_file()
        assert (window.run_dir / "plots.json").is_file()
        records = [json.loads(line) for line in (window.run_dir / "evaluations.jsonl").read_text().splitlines()]
        for record in records:
            assert (record["compute_s"] is None) if record["cached"] else (record["compute_s"] > 0), record
        fresh_feko = [r for r in records if r["backend"] == "feko" and not r["cached"]]
        if len(fresh_feko) > 1:
            sessions = [r["diagnostics"]["session"] for r in fresh_feko]
            assert len({s["pid"] for s in sessions}) == 1, sessions
            assert all(s["reused"] for s in sessions[1:]), sessions
        assert report["solvers"]["feko"]["compute_timing_missing"] == 0
        reports.append(dict(directory=str(window.run_dir), summary=report))
        for width, height in ((1180, 820), (900, 650)):
            window.resize(width, height)
            for page, name in ((panel.settingsTab, "settings"), (panel.progressTab, "progress"), (panel.plotsTab, "plots")):
                panel.pages.setCurrentWidget(page)
                QtTest.QTest.qWait(150)
                assert window.width() == width and window.height() == height
                assert window.grab().save(str(window.run_dir / f"gui-{name}-{width}.png"))
        return window.run_dir

    try:
        accuracy = dict(defaults("accuracy"), base_elements=1, k_max=1, samples_per_k=2,
                        max_changed_fraction=1, cache_dir=cache_dir)
        if not reference:
            run(window.accuracy_panel, accuracy)
        ga = dict(defaults("ga"), population_size=4, generations=3, tournament_size=2,
                  min_elements=1, max_elements=3, ga_implementation="binary_tournament_v1",
                  max_changed_fraction=1, cache_dir=cache_dir)
        if reference:
            ga.update(ga_implementation="thinwire_pixel_v1", initial_elements=2, generations=2, max_elements=4)
        replay_dir = run(window.ga_panel, ga)
        trace_a = json.loads((replay_dir / "ga-trace-schur.json").read_text())
        trace_b = json.loads((replay_dir / "ga-trace-feko.json").read_text())
        assert [r["population"] for r in trace_a["generations"]] == [r["population"] for r in trace_b["generations"]]
        independent_dir = run(window.ga_panel, dict(ga, mode="independent"))
        ta = json.loads((independent_dir / "ga-trace-schur.json").read_text())
        tb = json.loads((independent_dir / "ga-trace-feko.json").read_text())
        assert ta["generations"][0]["population"] == tb["generations"][0]["population"]
        assert reports[-1]["summary"]["preprocessing"]["cache_hit"]
        assert reports[-1]["summary"]["solvers"]["feko"]["cache_hits"] > 0
        (root / "smoke.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print("BENCHMARK SMOKE OK", root, flush=True)
    finally:
        if window.busy:
            window.cancel_run()
            wait_until(lambda: not window.busy, 45)
        window.dirty = False
        window.close()
        app.processEvents()


if __name__ == "__main__":
    main()
