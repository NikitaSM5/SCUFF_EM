"""Opt-in cancellation smoke with an actual owned CADFEKO process."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from check_benchmarks import wait_until
from gui.app import AntennaWindow, create_application
from gui.project import default_project
from experiments.config import defaults
from PyQt5 import QtCore, QtTest


def cadfeko_pids():
    result = subprocess.run(["powershell", "-NoProfile", "-Command",
                             "@(Get-Process cadfeko -ErrorAction SilentlyContinue).Id"],
                            capture_output=True, text=True, check=True)
    return {int(line) for line in result.stdout.splitlines() if line.strip()}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    app = create_application([])
    window = AntennaWindow()
    root = ROOT / "results" / ("benchmark-cancel-"+datetime.now().strftime("%Y%m%d-%H%M%S"))
    root.mkdir()
    project = default_project(root)
    project.update(cells=[[1, 0], [0, 0]], feed_cell=[0, 0], cell_size_mm=10, mesh_size_mm=5)
    project["feko"].update(compute_pattern=False, timeout_s=120)
    window.apply_project(project)
    window.accuracy_panel.apply_settings(dict(defaults("accuracy"), accuracy_mode="random_base", k_max=1, use_cache=False))
    window.resultsTabs.setCurrentWidget(window.accuracy_panel)
    window.show()
    before = cadfeko_pids()
    try:
        QtTest.QTest.mouseClick(window.accuracy_panel.runButton, QtCore.Qt.LeftButton)
        assert window.busy
        wait_until(lambda: window._stage == "Запуск CADFEKO" or not window.busy, 90)
        assert window.busy, "Stopped before CADFEKO startup"
        QtTest.QTest.qWait(2000)
        owned = cadfeko_pids()-before
        assert owned, "No actual CADFEKO process started"
        QtTest.QTest.mouseClick(window.accuracy_panel.cancelButton, QtCore.Qt.LeftButton)
        wait_until(lambda: not window.busy, 45)
        report = json.loads((window.run_dir / "summary.json").read_text())
        assert report["status"] == "cancelled", report
        assert all(s["failures"] == 0 for s in report["solvers"].values())
        assert not (cadfeko_pids() & owned), "Owned CADFEKO survived cancellation"
        assert (window.run_dir / "evaluations.csv").is_file()
        assert (window.run_dir / "experiment.log").is_file()
        print("REAL CANCELLATION OK", window.run_dir, flush=True)
    finally:
        if window.busy:
            window.cancel_run()
            wait_until(lambda: not window.busy, 45)
        window.dirty = False
        window.close()
        app.processEvents()


if __name__ == "__main__":
    main()
