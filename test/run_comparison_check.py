"""Opt-in end-to-end Qt button check using both installed native solvers."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gui.app import AntennaWindow, create_application
from gui.project import read_project
from PyQt5 import QtCore, QtTest


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("--no-pattern", action="store_true")
    parser.add_argument("--sweep", action="store_true")
    args = parser.parse_args()
    app = create_application([])
    window = AntennaWindow()
    project = read_project(args.project)
    project.setdefault("feko", {})["compute_pattern"] = not args.no_pattern
    if args.sweep:
        project["feko"].update(sweep_enabled=True, start_ghz=3.1, stop_ghz=3.3, frequency_points=3)
    window.apply_project(project)
    window.show()
    check = {"success": False}

    def completed(ok):
        try:
            print(window.logText.toPlainText(), flush=True)
            assert ok, window.resultText.toPlainText()
            assert window._done["engine"] == "compare"
            assert window.comparisonTable.item(0, 1).text() != "—"
            assert window.comparisonTable.item(0, 2).text() != "—"
            assert window.comparisonTable.rowCount() == 2
            if not args.no_pattern:
                assert window.comparisonTable.item(1, 1).text() != "—"
                assert window.comparisonTable.item(1, 2).text() != "—"
            else:
                assert window.comparisonTable.item(1, 1).text() == "—"
                assert window.comparisonTable.item(1, 2).text() == "—"
            for width, height in ((1180, 820), (900, 650)):
                window.resize(width, height)
                QtTest.QTest.qWait(150)
                assert window.width() == width
                for tab, name in ((window.antennaTab, "antenna"), (window.comparisonTab, "comparison")):
                    window.resultsTabs.setCurrentWidget(tab)
                    QtTest.QTest.qWait(100)
                    window.grab().save(str(window.run_dir / f"{name}-{width}.png"))
            window.resize(1180, 820)
            window.resultsTabs.setCurrentWidget(window.patternTab)
            QtCore.QTimer.singleShot(3000, inspect_view)
        except Exception as exc:
            print(repr(exc), flush=True)
            app.exit(1)

    def inspect_view():
        window._viewer.page().runJavaScript("window.viewerInfo ? viewerInfo() : null", inspected)

    def inspected(info):
        try:
            assert info and info["webgl"] and info["calls"] > 0, info
            assert info["feedModel"] == "planar_delta_gap_voltage", info
            assert bool(info["patternPoints"]) != args.no_pattern, info
            for width, height in ((1180, 820), (900, 650)):
                window.resize(width, height)
                QtTest.QTest.qWait(250)
                window._viewer.reset_view()
                QtTest.QTest.qWait(250)
                window.grab().save(str(window.run_dir / f"pattern-{width}.png"))
            window.showPatternCheck.setChecked(False)
            QtTest.QTest.qWait(250)
            window.grab().save(str(window.run_dir / "model-only.png"))
            (window.run_dir / "viewer-check.json").write_text(json.dumps(info, indent=2))
            print("GUI + native solvers + WebGL: OK", window.run_dir, info, flush=True)
            check["success"] = True
            app.exit(0)
        except Exception as exc:
            print(repr(exc), flush=True)
            app.exit(1)

    window.completed.connect(completed)
    QtCore.QTimer.singleShot(100, window.runButton.click)
    code = app.exec_()
    return code if check["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
