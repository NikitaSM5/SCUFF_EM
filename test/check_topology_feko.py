"""Opt-in real Qt workflow: full matrix, cell edits, Schur, FEKO and saved UI captures."""
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gui.app import AntennaWindow, create_application
from gui.project import default_project
from PyQt5 import QtCore, QtTest


def wait_until(predicate, timeout=300):
    deadline = time.monotonic()+timeout
    while not predicate() and time.monotonic() < deadline:
        QtTest.QTest.qWait(30)
    if not predicate():
        raise TimeoutError("GUI topology check timed out")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    app = create_application([])
    window = AntennaWindow()
    directory = ROOT / "results" / ("topology-gui-check-"+datetime.now().strftime("%Y%m%d-%H%M%S"))
    directory.mkdir()
    config = default_project(directory)
    config.update(cells=[[0, 0, 0], [0, 1, 0], [0, 0, 0]], feed_cell=[1, 1],
                  mesh_size_mm=1.25, timeout_s=300)
    config["feko"]["angle_step_deg"] = 15
    window.apply_project(config)
    window.show()
    QtTest.QTest.qWait(100)
    runs = []

    def calculate(button, method=None):
        assert button.isEnabled()
        QtTest.QTest.mouseClick(button, QtCore.Qt.LeftButton)
        wait_until(lambda: not window.busy)
        print(window.logText.toPlainText(), flush=True)
        assert window._done is not None, window.resultText.toPlainText()
        row = window._comparison["scuff"][0]
        if method:
            assert row["topology_update"]["method"] == method, row
        runs.append(str(window.run_dir))

    def toggle(row, col):
        window.resultsTabs.setCurrentWidget(window.antennaTab)
        QtTest.QTest.qWait(50)
        point = window.gridView.mapFromScene(QtCore.QPointF((col+.5)*32, (row+.5)*32))
        QtTest.QTest.mouseClick(window.gridView.viewport(), QtCore.Qt.LeftButton, pos=point)

    try:
        calculate(window.fullMatrixButton, "initial_lu")
        toggle(1, 2)
        calculate(window.schurButton, "schur")
        toggle(0, 1)
        toggle(1, 2)
        calculate(window.schurButton, "schur")
        # Return to the editor so the actual FEKO button is visible.
        window.resultsTabs.setCurrentWidget(window.antennaTab)
        calculate(window.topologyFekoButton, "unchanged")
        scuff, feko = window._comparison["scuff"][0], window._comparison["feko"][0]
        assert scuff["gain_peak_dbi"] is not None and feko["gain_peak_dbi"] is not None
        assert abs(scuff["gain_peak_dbi"]-feko["gain_peak_dbi"]) < .3, (scuff, feko)
        assert abs(scuff["s11_db"]-feko["s11_db"]) < .1, (scuff, feko)
        for width, height in ((1180, 820), (900, 650)):
            window.resize(width, height)
            for tab, name in ((window.antennaTab, "antenna"), (window.comparisonTab, "comparison")):
                window.resultsTabs.setCurrentWidget(tab)
                QtTest.QTest.qWait(100)
                assert window.size().width() == width and window.size().height() == height
                buttons = [window.fullMatrixButton, window.openMatrixButton, window.schurButton, window.topologyFekoButton]
                for button in buttons:
                    assert button.size() == buttons[0].size()
                    assert button.fontMetrics().horizontalAdvance(button.text())+16 <= button.width()
                window.grab().save(str(directory / f"{name}-{width}.png"))
        window.resultsTabs.setCurrentWidget(window.patternTab)
        info = []
        QtTest.QTest.qWait(3000)
        window._viewer.page().runJavaScript("window.viewerInfo ? viewerInfo() : null", info.append)
        wait_until(lambda: bool(info), 10)
        assert info[0] and info[0]["webgl"] and info[0]["patternPoints"] > 0, info
        window.grab().save(str(directory / "pattern.png"))
        record = dict(runs=runs, scuff=scuff, feko=feko, viewer=info[0])
        (directory / "check.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        print("Topology GUI + Schur + FEKO: OK", directory, flush=True)
        print(json.dumps(dict(scuff_gain=scuff["gain_peak_dbi"], feko_gain=feko["gain_peak_dbi"],
                              scuff_s11=scuff["s11_db"], feko_s11=feko["s11_db"])), flush=True)
    finally:
        if window.busy:
            window.cancel_run()
            wait_until(lambda: not window.busy, 30)
        window.dirty = False
        window.close()
        app.processEvents()


if __name__ == "__main__":
    main()
