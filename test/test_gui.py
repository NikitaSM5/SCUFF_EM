"""Qt interaction tests; set SCUFF_GUI_NATIVE_TESTS=1 for native integration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gui.app import AntennaWindow, create_application, write_json, worker_command
from PyQt5 import QtCore, QtGui, QtTest, QtWidgets
from gui.project import default_project, read_project, validate_project
from gui.feko_settings import FekoSettingsDialog
from feko.config import default_options

APP = create_application([])


def wait_until(predicate, timeout=15):
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        QtTest.QTest.qWait(30)
    if not predicate():
        raise AssertionError("Timed out waiting for GUI/worker")


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="antenna-gui-")
        self.window = AntennaWindow()
        self.window.outputEdit.setText(self.temp.name)
        self.window.show()
        QtTest.QTest.qWait(50)

    def tearDown(self):
        if self.window.busy:
            self.window.cancel_run()
            wait_until(lambda: not self.window.busy, 30)
        wait_until(lambda: self.window.kill_process is None)
        self.window.dirty = False
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()
        self.temp.cleanup()

    def click_cell(self, row, col, button=QtCore.Qt.LeftButton):
        point = self.window.gridView.mapFromScene(QtCore.QPointF((col+.5)*32, (row+.5)*32))
        QtTest.QTest.mouseClick(self.window.gridView.viewport(), button, pos=point)

    def test_toolbar_icons_are_not_overridden(self):
        names = ("metalButton", "feedButton", "undoButton", "redoButton", "clearButton", "fitButton")
        icon = self.window.style().standardIcon(QtWidgets.QStyle.SP_DialogSaveButton)
        for name in names:
            getattr(self.window, name).setIcon(icon)
        self.window.apply_icons()
        for name in names:
            self.assertEqual(getattr(self.window, name).icon().cacheKey(), icon.cacheKey())

    def test_current_addition_mode_controls_and_saved_settings(self):
        panel = self.window.accuracy_panel
        self.window.resultsTabs.setCurrentWidget(panel)
        panel.pages.setCurrentWidget(panel.settingsTab)
        self.assertEqual(panel.accuracy_mode.currentIndex(), 0)
        self.assertEqual(panel.samples_per_k.value(), 100)
        self.assertTrue(panel.base_elements.isHidden())
        self.assertTrue(panel.incremental.isHidden())
        self.assertTrue(panel.exhaustive_threshold.isHidden())
        self.assertEqual(panel.settings(self.window.project_data(), check=False)["accuracy_mode"], "current_additions")
        panel.accuracy_mode.setCurrentIndex(1)
        self.assertFalse(panel.base_elements.isHidden())
        self.assertFalse(panel.incremental.isHidden())
        self.assertFalse(panel.exhaustive_threshold.isHidden())
        self.assertEqual(panel.settings(self.window.project_data(), check=False)["accuracy_mode"], "random_base")
        panel.accuracy_mode.setCurrentIndex(0)
        panel.set_busy(True)
        self.assertFalse(panel.accuracy_mode.isEnabled())
        panel.set_busy(False)

    def test_thinwire_controls_and_saved_settings(self):
        panel = self.window.ga_panel
        self.assertEqual(panel.ga_implementation.currentIndex(), 0)
        self.assertTrue(panel.tournament_size.isHidden())
        self.assertFalse(panel.local_search_depth.isHidden())
        panel.local_search_depth.setValue(7)
        panel.child_fraction.setValue(.25)
        panel.ga_reference.setText("D:/custom/reference")
        settings = panel.settings(self.window.project_data(), check=False)
        panel.reset()
        panel.apply_settings(settings)
        self.assertEqual(panel.local_search_depth.value(), 7)
        self.assertEqual(panel.child_fraction.value(), .25)
        self.assertEqual(panel.ga_reference.text(), "D:/custom/reference")
        panel.ga_implementation.setCurrentIndex(1)
        self.assertFalse(panel.tournament_size.isHidden())
        self.assertTrue(panel.local_search_depth.isHidden())

    def test_draw_feed_erase_and_history(self):
        w = self.window
        self.click_cell(2, 3)
        self.assertEqual(w.grid.cells[2][3], 1)
        QtTest.QTest.mouseClick(w.feedButton, QtCore.Qt.LeftButton)
        self.click_cell(2, 3)
        self.assertEqual(w.grid.feed, (2, 3))
        self.click_cell(1, 1)
        self.assertEqual(w.grid.feed, (2, 3))
        self.click_cell(2, 3, QtCore.Qt.RightButton)
        self.assertIsNone(w.grid.feed)
        self.assertEqual(w.grid.cells[2][3], 0)
        w.grid.undo()
        self.assertEqual(w.grid.feed, (2, 3))
        w.grid.redo()
        self.assertIsNone(w.grid.feed)

    def test_toggle_and_drag(self):
        w = self.window
        self.click_cell(0, 0)
        self.click_cell(0, 0)
        self.assertEqual(w.grid.cells[0][0], 0)
        start = w.gridView.mapFromScene(QtCore.QPointF(16, 48))
        end = w.gridView.mapFromScene(QtCore.QPointF(32*5+16, 48))
        QtTest.QTest.mousePress(w.gridView.viewport(), QtCore.Qt.LeftButton, pos=start)
        QtTest.QTest.mouseMove(w.gridView.viewport(), end, delay=30)
        QtTest.QTest.qWait(50)
        QtTest.QTest.mouseRelease(w.gridView.viewport(), QtCore.Qt.LeftButton, pos=end)
        self.assertEqual(w.grid.cells[1][:6], [1]*6)
        w.grid.undo()
        self.assertEqual(w.grid.cells[1][:6], [0]*6)

    def test_resize_retains_cells_and_undo_restores_feed(self):
        w = self.window
        self.click_cell(7, 7)
        w.grid.feed = (7, 7)
        with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.No):
            w.nSpin.setValue(4)
        self.assertEqual(w.nSpin.value(), 8)
        with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.Yes):
            w.nSpin.setValue(4)
        self.assertIsNone(w.grid.feed)
        w.grid.undo()
        self.assertEqual(w.nSpin.value(), 8)
        self.assertEqual(w.grid.feed, (7, 7))

    def test_project_round_trip(self):
        w = self.window
        self.click_cell(3, 4)
        w.grid.feed = (3, 4)
        w.frequencySpin.setValue(2.45)
        w.autoMeshCheck.setChecked(False)
        w.meshSpin.setValue(.75)
        path = Path(self.temp.name) / "project.json"
        w.project_path = path
        self.assertTrue(w.save_project())
        w.grid.clear()
        w.apply_project(read_project(path), path)
        self.assertEqual(w.grid.feed, (3, 4))
        self.assertEqual(w.frequencySpin.value(), 2.45)
        self.assertEqual(w.meshSpin.value(), .75)
        self.assertFalse(w.dirty)

    def test_legacy_numpy_option_is_ignored(self):
        data = default_project(self.temp.name)
        self.assertNotIn("verify_numpy", data)
        data["verify_numpy"] = True
        self.window.apply_project(data)
        self.assertNotIn("verify_numpy", self.window.project_data())
        self.assertIsNone(self.window.findChild(QtWidgets.QCheckBox, "numpyCheck"))

    def test_validation_and_auto_mesh(self):
        w = self.window
        w.cellSpin.setValue(7)
        self.assertEqual(w.meshSpin.value(), 3.5)
        self.assertFalse(w.meshSpin.isEnabled())
        data = w.project_data()
        with self.assertRaises(ValueError):
            validate_project(data, for_run=True)
        data.update(cells=[[1, 0], [0, 1]], feed_cell=(0, 0))
        with self.assertRaisesRegex(ValueError, "контакт"):
            validate_project(data, for_run=True)
        data["cells"] = [[1, 1], [0, 1]]
        validate_project(data, for_run=True)
        with patch.object(w, "show_error") as error:
            w.start_run()
        self.assertTrue(error.called)
        self.assertFalse(w.busy)

    def test_csv_import_export(self):
        w = self.window
        self.click_cell(2, 3)
        path = str(Path(self.temp.name) / "cells.csv")
        with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(path, "CSV (*.csv)")):
            w.export_mask()
        w.grid.clear()
        with patch.object(QtWidgets.QFileDialog, "getOpenFileName", return_value=(path, "")):
            w.import_mask()
        self.assertEqual(w.grid.cells[2][3], 1)
        self.assertIsNone(w.grid.feed)

    def test_render_sizes(self):
        w = self.window
        self.assertEqual(w.styleSheet(), "")
        self.assertEqual(w.gridView.backgroundBrush().style(), QtCore.Qt.NoBrush)
        cells = [[int(2 <= r <= 5 and 1 <= c <= 6 or r == 6 and c == 3) for c in range(8)] for r in range(8)]
        w.grid.load(cells, (5, 3))
        directory = ROOT / "results/gui-check"
        directory.mkdir(exist_ok=True)
        for width, height in ((1180, 820), (900, 650)):
            w.resize(width, height)
            QtTest.QTest.qWait(100)
            self.assertEqual(w.width(), width)
            self.assertGreater(w.gridView.width(), 390)
            image = w.grab().toImage()
            self.assertFalse(image.isNull())
            self.assertTrue(image.save(str(directory / f"window-{width}x{height}.png")))
            colors = {image.pixelColor(x, y).name() for x in range(0, image.width(), 4)
                      for y in range(0, image.height(), 4)}
            palette = w.gridView.palette()
            self.assertIn(palette.color(QtGui.QPalette.Highlight).name(), colors)
            self.assertIn(palette.color(QtGui.QPalette.HighlightedText).name(), colors)

    def test_worker_start_failure(self):
        w = self.window
        config = default_project(self.temp.name)
        config.update(cells=[[1]], feed_cell=[0, 0])
        w.apply_project(config)
        with patch("gui.app.worker_command", return_value=(str(Path(self.temp.name) / "missing.exe"), [])):
            w.start_run()
        wait_until(lambda: not w.busy)
        self.assertEqual(read_json(w.run_dir / "run.json")["status"], "failed")
        self.assertTrue(w.runButton.isEnabled())

    def test_worker_interpreter_is_independent_of_gui(self):
        with patch.object(sys, "executable", "C:/Python314/pythonw.exe"):
            program, arguments = worker_command(Path(self.temp.name) / "project.json")
        self.assertEqual(program, "py")
        self.assertEqual(arguments[:2], ["-3.11", "-u"])

    def test_topology_buttons_and_cache_invalidation(self):
        from gui.topology_config import cache_signature
        w = self.window
        config = default_project(self.temp.name)
        config.update(cells=[[1, 0], [0, 0]], feed_cell=[0, 0])
        w.apply_project(config)
        self.assertFalse(w.schurButton.isEnabled())
        buttons = (w.fullMatrixButton, w.openMatrixButton, w.schurButton, w.topologyFekoButton)
        for button in buttons:
            self.assertEqual(button.size(), buttons[0].size())
            self.assertTrue(button.icon().isNull())
            self.assertFalse(button.toolTip())
        w._topology_cache = str(Path(self.temp.name) / "topology-cache.json")
        w._topology_manifest = dict(signature=cache_signature(config), unknowns=88)
        w.update_topology_controls()
        self.assertTrue(w.schurButton.isEnabled())
        w.grid.cells[0][1] = 1
        w.grid_changed()
        self.assertTrue(w.schurButton.isEnabled())
        with patch.object(w, "start_run") as start:
            w.schurButton.click()
            start.assert_called_once_with(engine="topology-update")
        with patch.object(w, "start_run") as start:
            w.topologyFekoButton.click()
            start.assert_called_once_with(engine="topology-compare")
        w.frequencySpin.setValue(4)
        self.assertFalse(w.schurButton.isEnabled())
        self.assertFalse(w.topologyFekoButton.isEnabled())
        w.frequencySpin.setValue(config["frequency_ghz"])
        self.assertTrue(w.schurButton.isEnabled())
        w.grid.feed = (0, 1)
        w.grid_changed()
        self.assertFalse(w.schurButton.isEnabled())

    @unittest.skipUnless(os.environ.get("SCUFF_GUI_NATIVE_TESTS") == "1", "Opt-in full matrix GUI workflow")
    def test_topology_prepare_edit_update_and_reopen(self):
        w = self.window
        config = default_project(self.temp.name)
        config.update(cells=[[1, 0], [0, 0]], feed_cell=[0, 0], cell_size_mm=10, mesh_size_mm=5)
        config["feko"]["compute_pattern"] = False
        w.apply_project(config)
        w.fullMatrixButton.click()
        wait_until(lambda: not w.busy, 90)
        self.assertIsNotNone(w._done, w.logText.toPlainText())
        self.assertTrue(w.schurButton.isEnabled())
        cache = w._topology_cache
        first_state = w._topology_state
        w.resultsTabs.setCurrentWidget(w.antennaTab)
        self.click_cell(0, 1)
        w.schurButton.click()
        wait_until(lambda: not w.busy, 60)
        self.assertIsNotNone(w._done, w.logText.toPlainText())
        row = w._comparison["scuff"][0]
        self.assertEqual(row["topology_update"]["method"], "schur")
        self.assertGreater(row["topology_update"]["added_dofs"], 0)
        self.assertNotEqual(first_state, w._topology_state)
        self.assertEqual(cache, w._topology_cache)
        path = w.run_dir / "comparison.json"
        w.load_run(path)
        self.assertEqual(w.grid.cells, [[1, 1], [0, 0]])
        self.assertTrue(w.schurButton.isEnabled())
        with patch.object(QtWidgets.QFileDialog, "getOpenFileName", return_value=(cache, "")):
            w.open_topology_cache()
        self.assertEqual(w.grid.cells, [[1, 0], [0, 0]])
        self.assertTrue(w.schurButton.isEnabled())

    def test_feko_settings_roundtrip_and_render(self):
        options = default_options()
        options.update(reference_ohm=75, sweep_enabled=True)
        dialog = FekoSettingsDialog(options, self.window)
        dialog.show()
        QtTest.QTest.qWait(100)
        self.assertEqual(dialog.options(), options)
        self.assertIsNone(dialog.findChild(QtWidgets.QDoubleSpinBox, "diameterSpin"))
        directory = ROOT / "results/gui-check"
        directory.mkdir(exist_ok=True)
        self.assertTrue(dialog.grab().save(str(directory / "feko-settings.png")))
        dialog.reject()
        self.window._feko_options = options
        path = Path(self.temp.name) / "feko-project.json"
        write_json(path, self.window.project_data())
        self.window.apply_project(read_project(path))
        self.assertEqual(self.window._feko_options, options)

    def test_feko_button_without_installation_exports_script(self):
        w = self.window
        config = default_project(self.temp.name)
        config.update(cells=[[1]], feed_cell=[0, 0])
        config["feko"]["cadfeko_exe"] = str(Path(self.temp.name) / "not-installed/cadfeko.exe")
        w.apply_project(config)
        w.actionFekoOnly.trigger()
        wait_until(lambda: not w.busy, 30)
        self.assertIsNotNone(w._done, w.logText.toPlainText())
        self.assertEqual(w._done["status"], "prepared")
        self.assertEqual(read_json(w.run_dir / "run.json")["status"], "prepared")
        self.assertTrue((w.run_dir / "feko/create_model.lua").is_file())
        self.assertFalse((w.run_dir / "feko/antenna.cfx").exists())

    def test_run_button_selects_comparison_and_buttons_match(self):
        w = self.window
        with patch.object(w, "start_run") as start:
            w.runButton.click()
        start.assert_called_once_with(engine="compare")
        self.assertEqual(w.runButton.size(), w.cancelButton.size())
        self.assertEqual(w.runButton.size(), w.openResultButton.size())
        for widget in w.findChildren(QtWidgets.QWidget):
            self.assertFalse(widget.toolTip(), widget.objectName())
        w.patternCheck.setChecked(False)
        self.assertFalse(w.project_data()["feko"]["compute_pattern"])

    def test_comparison_partial_data_and_stale_results(self):
        w = self.window
        row = dict(frequency_hz=3.2e9, s11_db=-1.2, s11_magnitude=.87, reference_ohm=50)
        data = dict(project=w.project_data(), scuff=[row], feko=[])
        w.show_comparison(data)
        self.assertEqual(w.comparisonTable.item(0, 1).text(), "-1.2000")
        self.assertEqual(w.comparisonTable.item(0, 2).text(), "—")
        data["feko"] = [dict(row, s11_db=-1.1)]
        w.show_comparison(data)
        self.assertEqual(w.comparisonTable.item(0, 3).text(), "0.1000")
        w._result_stale = False
        w.cellSpin.setValue(6)
        self.assertEqual(w.comparisonState.text(), "Предыдущий расчёт")

    def test_comparison_feed_models_are_not_relabelled(self):
        w = self.window
        w._result_stale = False
        data = dict(project=w.project_data(), scuff=[], feko=[],
                    feed_models=dict(scuff="point_port", feko="finite_radius_wire"))
        w.show_comparison(data)
        self.assertEqual(w.comparisonTable.rowCount(), 2)
        self.assertEqual(w.comparisonState.text(), "Разные модели питания")
        data["feed_models"] = dict(scuff="planar_delta_gap_voltage", feko="planar_delta_gap_voltage")
        w.show_comparison(data)
        self.assertNotEqual(w.comparisonState.text(), "Разные модели питания")

    def test_comparison_shows_gain_for_both_solvers(self):
        w = self.window
        data = dict(project=w.project_data(), scuff=[dict(frequency_hz=3.2e9, gain_peak_dbi=4.732)],
                    feko=[dict(frequency_hz=3.2e9, gain_peak_dbi=4.719)])
        w.show_comparison(data)
        self.assertEqual(w.comparisonTable.rowCount(), 2)
        self.assertEqual(w.comparisonTable.item(1, 0).text(), "Gain max, dBi")
        self.assertEqual(w.comparisonTable.item(1, 1).text(), "4.732")
        self.assertEqual(w.comparisonTable.item(1, 2).text(), "4.719")
        self.assertEqual(w.comparisonTable.item(1, 3).text(), "-0.013")

    def test_load_saved_comparison_without_recalculating(self):
        w = self.window
        project = default_project(self.temp.name)
        project.update(cells=[[1]], feed_cell=[0, 0])
        project["feko"]["compute_pattern"] = False
        row = dict(frequency_hz=3.2e9, s11_db=-2, s11_magnitude=.794, passive=True)
        path = Path(self.temp.name) / "comparison.json"
        write_json(path, dict(project=project, scuff=[row], feko=[row]))
        w.load_run(path)
        self.assertFalse(w.busy)
        self.assertFalse(w._result_stale)
        self.assertTrue(w.openResultButton.isEnabled())
        self.assertEqual(w.comparisonFrequency.count(), 1)
        self.assertFalse(w.patternCheck.isChecked())
        self.assertFalse(w.showPatternCheck.isEnabled())
        self.assertEqual(w.resultsTabs.currentWidget(), w.comparisonTab)

    @unittest.skipUnless(os.environ.get("SCUFF_GUI_NATIVE_TESTS") == "1", "Opt-in native worker")
    def test_worker_limit_failure(self):
        w = self.window
        config = default_project(self.temp.name)
        config.update(cells=[[1]], feed_cell=[0, 0], max_unknowns=1)
        w.apply_project(config)
        w.start_run()
        wait_until(lambda: not w.busy, 30)
        self.assertIn("unknowns", w._error)
        self.assertEqual(read_json(w.run_dir / "run.json")["status"], "failed")
        self.assertFalse((w.run_dir / "M.npy").exists())

    @unittest.skipUnless(os.environ.get("SCUFF_GUI_NATIVE_TESTS") == "1", "Opt-in native calculation")
    def test_native_end_to_end(self):
        w = self.window
        config = default_project(ROOT / "results/gui-check/calculations")
        config.update(cells=[[1]], feed_cell=[0, 0], cell_size_mm=10, mesh_size_mm=5)
        w.apply_project(config)
        w.start_run()
        self.assertTrue(w.busy)
        self.assertFalse(w.gridView.isEnabled())
        wait_until(lambda: not w.busy, 90)
        self.assertIsNotNone(w._done, w.logText.toPlainText())
        self.assertTrue(w._runtime["version"].startswith("3.11."), w._runtime)
        import numpy as np
        M = np.load(w.run_dir / "M.npy", allow_pickle=False)
        self.assertEqual(M.shape, (20, 20))
        self.assertLess(w._done["checks"]["relative_residual"], 1e-8)
        self.assertGreater(w._done["timings_s"]["assembly"], 0)
        self.assertIn("Сборка M:", w.logText.toPlainText())
        state = read_json(w.run_dir / "run.json")
        self.assertEqual(state["status"], "success")
        self.assertTrue(w.gridView.isEnabled())
        destination = ROOT / "results/gui-check/native-result.json"
        write_json(destination, dict(shape=list(M.shape), checks=w._done["checks"], run=state))

    @unittest.skipUnless(os.environ.get("SCUFF_GUI_NATIVE_TESTS") == "1", "Opt-in native cancellation")
    def test_cancel_kills_native_child(self):
        w = self.window
        config = default_project(self.temp.name)
        config.update(cells=[[1]*6 for _ in range(6)], feed_cell=[2, 2])
        w.apply_project(config)
        w.start_run()
        wait_until(lambda: w._stage.startswith("SCUFF-EM") or not w.busy, 30)
        self.assertTrue(w.busy, w.logText.toPlainText())
        pid = int(w.process.processId())
        QtTest.QTest.qWait(800)
        worker_pid = w._runtime["pid"]
        command = f"Get-CimInstance Win32_Process -Filter 'ParentProcessId = {worker_pid}' | Select-Object ProcessId,Name | ConvertTo-Json -Compress"
        result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, check=True)
        children = json.loads(result.stdout)
        if isinstance(children, dict):
            children = [children]
        native = [p["ProcessId"] for p in children if p["Name"] == "export_planar.exe"]
        self.assertTrue(native, result.stdout)
        w.cancel_run()
        wait_until(lambda: not w.busy and w.kill_process is None, 30)
        self.assertEqual(read_json(w.run_dir / "run.json")["status"], "cancelled")
        command = f"Get-Process -Id {','.join(map(str, native + [pid, worker_pid]))} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"
        result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True)
        self.assertFalse(result.stdout.strip(), result.stdout)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
