"""Native Qt 5 frontend. The Designer form is loaded at every launch."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    from gui.bootstrap import ensure_gui_python
    ensure_gui_python()

import PyQt5
# QtCore embeds PyQt5.__file__ into qt.conf using a legacy code page.
# Give it the same existing file through its ASCII Windows short path first.
if os.name == "nt":
    import ctypes
    _qt_path = ctypes.create_unicode_buffer(32768)
    _qt_length = ctypes.windll.kernel32.GetShortPathNameW(PyQt5.__file__, _qt_path, len(_qt_path))
    if 0 < _qt_length < len(_qt_path):
        PyQt5.__file__ = _qt_path.value
from PyQt5 import QtCore, QtGui, QtWidgets, uic

from gui.grid import GridEditor
from gui.project import default_project, read_project, validate_project
from gui.feko_settings import FekoSettingsDialog
from feko.config import default_options, validate_options
from gui.comparison import ComparisonTable
from gui.viewer import PatternView
from gui.topology_config import cache_signature, read_cache_manifest
from gui.benchmark import BenchmarkPanel


def write_json(path, data):
    output = QtCore.QSaveFile(str(path))
    payload = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if not output.open(QtCore.QIODevice.WriteOnly):
        raise OSError(output.errorString())
    if output.write(payload) != len(payload) or not output.commit():
        raise OSError(output.errorString())


def worker_command(project_path, engine="scuff"):
    # Match run.cmd and setup-windows.ps1 even when an IDE uses another Python.
    arguments = ["-3.11", "-u", str(Path(__file__).with_name("worker.py")), str(project_path)]
    return "py", arguments + (["--"+engine] if engine in ("feko", "compare", "topology-prepare", "topology-update", "topology-compare", "experiment") else [])


class AntennaWindow(QtWidgets.QMainWindow):
    completed = QtCore.pyqtSignal(bool)

    FIELDS = {"cell_size_mm": "cellSpin", "thickness_mm": "heightSpin",
              "epsilon_r": "epsilonSpin", "loss_tangent": "lossSpin",
              "frequency_ghz": "frequencySpin", "max_unknowns": "limitSpin",
              "timeout_s": "timeoutSpin"}

    def __init__(self):
        super().__init__()
        uic.loadUi(str(Path(__file__).with_name("antenna.ui")), self)
        self._loading = True
        self.project_path = None
        self.dirty = False
        self.process = None
        self.kill_process = None
        self.run_dir = None
        self._buffer = b""
        self._done = None
        self._error = None
        self._runtime = None
        self._engine = "scuff"
        self._feko_options = default_options()
        self._cancelled = False
        self._closing = False
        self._result_stale = True
        self._comparison = {}
        self._patterns = []
        self._view_project = None
        self._viewer = None
        self._topology_cache = None
        self._topology_state = None
        self._topology_manifest = None
        self._experiment_panel = None
        self._experiment_settings = None
        self.comparison = ComparisonTable(self.comparisonTable)
        self.grid = GridEditor(self.gridView)
        self.ga_panel = BenchmarkPanel("ga", self)
        self.accuracy_panel = BenchmarkPanel("accuracy", self)
        self.resultsTabs.addTab(self.ga_panel, "GA: Schur vs FEKO")
        self.resultsTabs.addTab(self.accuracy_panel, "Schur Accuracy")
        for panel in (self.ga_panel, self.accuracy_panel):
            panel.run_requested.connect(lambda p=panel: self.start_experiment(p))
            panel.cancel_requested.connect(self.cancel_run)
        self.grid.changed.connect(self.grid_changed)
        self.grid.message.connect(lambda text: self.statusbar.showMessage(text, 6000))
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_group.addButton(self.metalButton)
        self.mode_group.addButton(self.feedButton)
        self.metalButton.clicked.connect(lambda: setattr(self.grid, "mode", "metal"))
        self.feedButton.clicked.connect(lambda: setattr(self.grid, "mode", "feed"))
        self.undoButton.clicked.connect(self.grid.undo)
        self.redoButton.clicked.connect(self.grid.redo)
        self.clearButton.clicked.connect(self.grid.clear)
        self.fitButton.clicked.connect(self.grid.fit)
        self.nSpin.valueChanged.connect(self.resize_grid)
        for name in self.FIELDS.values():
            getattr(self, name).valueChanged.connect(self.parameters_changed)
        self.meshSpin.valueChanged.connect(self.mark_changed)
        self.autoMeshCheck.toggled.connect(self.parameters_changed)
        self.referenceCheck.toggled.connect(self.mark_changed)
        self.outputEdit.textChanged.connect(self.mark_changed)
        self.browseButton.clicked.connect(self.choose_output)
        self.actionNew.triggered.connect(self.new_project)
        self.actionOpen.triggered.connect(self.open_project)
        self.actionOpenRun.triggered.connect(self.open_run)
        self.actionSave.triggered.connect(self.save_project)
        self.actionSaveAs.triggered.connect(lambda: self.save_project(save_as=True))
        self.actionImportMask.triggered.connect(self.import_mask)
        self.actionExportMask.triggered.connect(self.export_mask)
        self.runButton.clicked.connect(lambda: self.start_run(engine="compare"))
        self.actionFekoOnly.triggered.connect(lambda: self.start_run(engine="feko"))
        self.actionFekoSettings.triggered.connect(self.configure_feko)
        self.fullMatrixButton.clicked.connect(lambda: self.start_run(engine="topology-prepare"))
        self.openMatrixButton.clicked.connect(self.open_topology_cache)
        self.schurButton.clicked.connect(lambda: self.start_run(engine="topology-update"))
        self.topologyFekoButton.clicked.connect(lambda: self.start_run(engine="topology-compare"))
        self.patternCheck.toggled.connect(self.mark_changed)
        self.comparisonFrequency.currentIndexChanged.connect(self.comparison_frequency_changed)
        self.patternFrequency.currentIndexChanged.connect(self.update_view)
        self.patternScale.currentIndexChanged.connect(self.update_view)
        self.showAntennaCheck.toggled.connect(self.update_view)
        self.showPatternCheck.toggled.connect(self.update_view)
        self.resetViewButton.clicked.connect(lambda: self._viewer.reset_view() if self._viewer else None)
        self.resultsTabs.currentChanged.connect(self.tab_changed)
        self.cancelButton.clicked.connect(self.cancel_run)
        self.openResultButton.clicked.connect(self.open_results)
        self.logText.document().setMaximumBlockCount(2000)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.update_elapsed)
        self.undo_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence.Undo, self.gridView, self.grid.undo)
        self.undo_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self.redo_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence.Redo, self.gridView, self.grid.redo)
        self.redo_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self.apply_icons()
        for widget in self.findChildren(QtWidgets.QWidget):
            widget.setToolTip("")
        self.apply_project(default_project(ROOT / "results/gui"))
        self.patternFrequency.setEnabled(False)
        self.showPatternCheck.setEnabled(False)
        self.patternScale.setEnabled(False)
        self._loading = False

    @property
    def busy(self):
        return self.process is not None

    def apply_icons(self):
        style = self.style()
        icons = {"browseButton": "SP_DirOpenIcon",
                 "actionNew": "SP_FileIcon", "actionOpen": "SP_DialogOpenButton",
                 "actionSave": "SP_DialogSaveButton", "actionSaveAs": "SP_DialogSaveButton"}
        for name, icon in icons.items():
            getattr(self, name).setIcon(style.standardIcon(getattr(QtWidgets.QStyle, icon)))

    def project_data(self):
        data = default_project(self.outputEdit.text().strip())
        data.update({key: getattr(self, name).value() for key, name in self.FIELDS.items()})
        data.update(cells=self.grid.snapshot()[0], feed_cell=self.grid.feed,
                    mesh_size_mm=None if self.autoMeshCheck.isChecked() else self.meshSpin.value(),
                    verify_reference=self.referenceCheck.isChecked(), feko=dict(self._feko_options))
        data["feko"]["compute_pattern"] = self.patternCheck.isChecked()
        if self._topology_cache:
            data.update(topology_cache=self._topology_cache, topology_state=self._topology_state)
        if hasattr(self, "ga_panel"):
            data["benchmarks"] = {p.kind: p.settings(data, check=False) for p in (self.ga_panel, self.accuracy_panel)}
        return data

    def apply_project(self, data, path=None):
        validate_project(data)
        self._loading = True
        for key, name in self.FIELDS.items():
            getattr(self, name).setValue(data[key])
        self.autoMeshCheck.setChecked(data["mesh_size_mm"] is None)
        self.meshSpin.setValue(data["mesh_size_mm"] or data["cell_size_mm"]/2)
        self.referenceCheck.setChecked(data["verify_reference"])
        self.outputEdit.setText(data["output_dir"])
        self._feko_options = validate_options(data.get("feko", {}))
        self.patternCheck.setChecked(self._feko_options["compute_pattern"])
        self.grid.load(data["cells"], data["feed_cell"])
        self.parameters_changed()
        self.project_path = Path(path) if path else None
        self.dirty = False
        for panel in (self.ga_panel, self.accuracy_panel):
            if panel.kind in data.get("benchmarks", {}):
                panel.apply_settings(data["benchmarks"][panel.kind])
        self._loading = False
        self.attach_topology(data.get("topology_cache"), data.get("topology_state"))
        self.setWindowTitle(self.title())
        self.invalidate_result()

    def attach_topology(self, path, state=None):
        self._topology_cache = str(Path(path).resolve()) if path else None
        self._topology_state = str(Path(state).resolve()) if state else None
        self._topology_manifest = None
        if path:
            try:
                self._topology_manifest = read_cache_manifest(path)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self.update_topology_controls()

    def update_topology_controls(self):
        manifest = self._topology_manifest
        compatible = bool(manifest and manifest["signature"] == cache_signature(self.project_data()))
        self.fullMatrixButton.setEnabled(not self.busy)
        self.openMatrixButton.setEnabled(not self.busy)
        self.schurButton.setEnabled(not self.busy and compatible)
        self.topologyFekoButton.setEnabled(not self.busy and compatible)
        if compatible:
            n = manifest["unknowns"]
            self.topologyState.setText(f"Полная матрица: {n} × {n}")
        elif manifest:
            self.topologyState.setText("Параметры изменены · нужна новая матрица")
        elif self._topology_cache:
            self.topologyState.setText("Файлы полной матрицы недоступны")
        else:
            self.topologyState.setText("Полная матрица не рассчитана")

    def open_topology_cache(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Открыть полную матрицу", self.outputEdit.text(), "Полная матрица (topology-cache.json)")
        if not path:
            return
        try:
            manifest = read_cache_manifest(path)
            if not self.confirm_discard():
                return
            initial_state = Path(path).parent / "topology-state.json"
            project = dict(manifest["project"], topology_cache=path,
                           topology_state=str(initial_state) if initial_state.is_file() else None)
            self.apply_project(project)
            self.resultsTabs.setCurrentWidget(self.antennaTab)
        except Exception as exc:
            self.show_error(exc)

    def title(self):
        name = self.project_path.name if self.project_path else "Новый проект"
        return f"{'* ' if self.dirty else ''}{name} | SCUFF-EM"

    def invalidate_result(self):
        if not self._result_stale:
            self.resultText.appendPlainText("\nПараметры изменены. Выше приведён предыдущий расчёт.")
            self._result_stale = True
            self.comparisonState.setText("Предыдущий расчёт")
            self.patternState.setText("Предыдущий расчёт")

    def mark_changed(self, *args):
        if not self._loading:
            self.dirty = True
            self.setWindowTitle(self.title())
            self.invalidate_result()
            self.update_topology_controls()

    def parameters_changed(self, *args):
        self.meshSpin.setEnabled(not self.autoMeshCheck.isChecked())
        if self.autoMeshCheck.isChecked():
            self.meshSpin.setValue(self.cellSpin.value()/2)
        extent = len(self.grid.cells)*self.cellSpin.value()
        self.extentLabel.setText(f"{extent:g} × {extent:g} мм")
        self.mark_changed()

    def grid_changed(self):
        self.nSpin.blockSignals(True)
        self.nSpin.setValue(len(self.grid.cells))
        self.nSpin.blockSignals(False)
        count = sum(map(sum, self.grid.cells))
        self.metalCountLabel.setText(f"Металл: {count} / {len(self.grid.cells)**2}")
        self.feedValue.setText(str(self.grid.feed) if self.grid.feed is not None else "Не выбран")
        self.undoButton.setEnabled(bool(self.grid.undo_stack) and not self.busy)
        self.redoButton.setEnabled(bool(self.grid.redo_stack) and not self.busy)
        self.parameters_changed()
        self.accuracy_panel.preview_timer.start()

    def resize_grid(self, n):
        old = len(self.grid.cells)
        cropped = n < old and any(v for r, row in enumerate(self.grid.cells)
                                  for c, v in enumerate(row) if r >= n or c >= n)
        if cropped and QtWidgets.QMessageBox.question(
                self, "Уменьшение сетки", "Металл за новым размером сетки будет удалён. Продолжить?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            self.nSpin.blockSignals(True)
            self.nSpin.setValue(old)
            self.nSpin.blockSignals(False)
            return
        self.grid.resize_grid(n)

    def show_error(self, message):
        QtWidgets.QMessageBox.warning(self, "SCUFF-EM", str(message))

    def confirm_discard(self):
        if not self.dirty:
            return True
        answer = QtWidgets.QMessageBox.question(
            self, "Несохранённый проект", "Сохранить изменения проекта?",
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Save)
        if answer == QtWidgets.QMessageBox.Save:
            return self.save_project()
        return answer == QtWidgets.QMessageBox.Discard

    def new_project(self):
        if self.confirm_discard():
            self.apply_project(default_project(ROOT / "results/gui"))

    def open_project(self):
        if not self.confirm_discard():
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Открыть проект", str(ROOT), "Проект (*.json)")
        if path:
            try:
                self.apply_project(read_project(path), path)
            except Exception as exc:
                self.show_error(exc)

    def save_project(self, checked=False, *, save_as=False):
        path = self.project_path
        if path is None or save_as:
            name, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Сохранить проект", str(path or ROOT / "antenna-project.json"), "Проект (*.json)")
            if not name:
                return False
            path = Path(name)
        try:
            data = validate_project(self.project_data())
            write_json(path, data)
            self.project_path = path
            self.dirty = False
            self.setWindowTitle(self.title())
            return True
        except Exception as exc:
            self.show_error(exc)
            return False

    def open_run(self):
        if not self.confirm_discard():
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Открыть расчёт", self.outputEdit.text(),
                                                       "Сравнение (comparison.json)")
        if path:
            try:
                self.load_run(path)
            except Exception as exc:
                self.show_error(exc)

    def load_run(self, path):
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        validate_project(data["project"], for_run=True)
        if not isinstance(data.get("scuff"), list) or not isinstance(data.get("feko"), list):
            raise ValueError("Некорректный файл сравнения")
        self.apply_project(data["project"])
        self.run_dir = path.parent
        self._result_stale = False
        self.show_comparison(data)
        pattern = self.run_dir / "feko/farfield.json"
        self.load_pattern(pattern if pattern.is_file() and data["feko"] else None, data["project"])
        self.resultText.setPlainText(f"comparison.csv\ncomparison.json\nscuff/\nfeko/\n{self.run_dir}")
        self.openResultButton.setEnabled(True)
        self.runStatus.setText("Результаты загружены" if data["feko"] else "Неполный расчёт")
        self.resultsTabs.setCurrentWidget(self.comparisonTab)

    def choose_output(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Папка результатов", self.outputEdit.text())
        if path:
            self.outputEdit.setText(path)

    def configure_feko(self):
        self._feko_options["compute_pattern"] = self.patternCheck.isChecked()
        dialog = FekoSettingsDialog(self._feko_options, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self._feko_options = dialog.options()
            self.mark_changed()

    def show_comparison(self, data):
        self._comparison = data
        self._view_project = data["project"]
        self.comparisonFrequency.blockSignals(True)
        self.comparisonFrequency.clear()
        for row in data.get("scuff") or data.get("feko", []):
            self.comparisonFrequency.addItem(f"{row['frequency_hz']/1e9:g} ГГц")
        self.comparisonFrequency.blockSignals(False)
        self.comparison.show(data, 0)
        self.comparison_frequency_changed(0)

    def comparison_frequency_changed(self, index):
        self.comparison.show(self._comparison, index)
        rows = self._comparison.get("scuff", [])
        warning = 0 <= index < len(rows) and rows[index].get("passive") is False
        self.comparisonState.setText("SCUFF: |S11| > 1" if warning else "" if self._comparison.get("feko") else "Ожидание FEKO")
        if not warning and not self._comparison.get("feko") and rows and self._comparison.get("project", {}).get("topology_cache"):
            self.comparisonState.setText("SCUFF-EM · фиксированная сетка")
        if not warning and 0 <= index < len(rows) and rows[index].get("gain_error"):
            self.comparisonState.setText("SCUFF: Gain не определён")
        elif not warning and 0 <= index < len(rows) and rows[index].get("gain_warning"):
            self.comparisonState.setText("SCUFF: проверьте Gain")
        feeds = self._comparison.get("feed_models", {})
        if feeds.get("scuff") and feeds.get("feko") and feeds["scuff"] != feeds["feko"]:
            self.comparisonState.setText("Разные модели питания")
        if self._result_stale and self._comparison and not self.busy:
            self.comparisonState.setText("Предыдущий расчёт")
        timing = self._comparison.get("compute_timings_s", {})
        if timing and not self.comparisonState.text():
            self.comparisonState.setText("; ".join(f"{name}: {timing[key]:.4f} с"
                for key, name in (("scuff", "SCUFF/Schur"), ("feko", "FEKO")) if timing.get(key) is not None))
        if 0 <= index < self.patternFrequency.count():
            self.patternFrequency.setCurrentIndex(index)

    def load_pattern(self, path, project):
        self._view_project = project
        self._patterns = json.loads(Path(path).read_text(encoding="utf-8")) if path else []
        self.patternFrequency.blockSignals(True)
        self.patternFrequency.clear()
        for block in self._patterns:
            self.patternFrequency.addItem(f"{block['frequency_hz']/1e9:g} ГГц")
        self.patternFrequency.blockSignals(False)
        self.patternFrequency.setEnabled(bool(self._patterns))
        self.showPatternCheck.setEnabled(bool(self._patterns))
        self.patternScale.setEnabled(bool(self._patterns))
        self.update_view()

    def tab_changed(self, index):
        if self.resultsTabs.widget(index) == self.patternTab:
            if self._viewer is None:
                self._viewer = PatternView(self.patternHost)
                self._viewer.error.connect(self.patternState.setText)
                self.patternHostLayout.addWidget(self._viewer)
            self.update_view()

    def update_view(self, *args):
        index = self.patternFrequency.currentIndex()
        block = self._patterns[index] if 0 <= index < len(self._patterns) else None
        if block:
            self.patternState.setText(f"Gain max: {block['peak']['gain_peak_dbi']:.3f} dBi · θ = 0…90°")
        else:
            self.patternState.setText("ДН не рассчитана")
        if self._comparison and self._result_stale and not self.busy:
            self.patternState.setText("Предыдущий расчёт · " + self.patternState.text())
        if self._viewer:
            self._viewer.update_scene(self._view_project or self.project_data(), block,
                                      self.showAntennaCheck.isChecked(), self.showPatternCheck.isChecked(),
                                      self.patternScale.currentIndex(),
                                      self._comparison.get("feed_models", {}).get("feko", "planar_delta_gap_voltage"))

    def import_mask(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Импорт матрицы клеток", str(ROOT),
                                                     "Матрица клеток (*.csv *.npy)")
        if not path:
            return
        try:
            import numpy as np
            cells = np.load(path, allow_pickle=False) if Path(path).suffix.lower() == ".npy" else np.loadtxt(path, delimiter=",", ndmin=2)
            if cells.ndim != 2 or cells.shape[0] != cells.shape[1] or not np.isin(cells, [0, 1]).all():
                raise ValueError("Ожидается квадратная матрица из 0 и 1.")
            data = self.project_data()
            data.update(cells=cells.astype(int).tolist(), feed_cell=None)
            validate_project(data)
            self.grid.remember()
            self.grid.load(data["cells"], reset_history=False)
        except Exception as exc:
            self.show_error(exc)

    def export_mask(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Экспорт матрицы клеток", str(ROOT / "cells.csv"),
                                                     "CSV (*.csv);;NumPy (*.npy)")
        if path:
            try:
                import numpy as np
                cells = np.asarray(self.grid.cells, dtype=np.uint8)
                if Path(path).suffix.lower() == ".npy":
                    with open(path, "wb") as output:
                        np.save(output, cells, allow_pickle=False)
                else:
                    np.savetxt(path, cells, delimiter=",", fmt="%d")
            except Exception as exc:
                self.show_error(exc)

    def set_busy(self, busy):
        self.parametersScroll.setEnabled(not busy)
        self.gridView.setEnabled(not busy)
        self.fileMenu.setEnabled(not busy)
        self.fekoMenu.setEnabled(not busy)
        for widget in (self.metalButton, self.feedButton, self.clearButton, self.runButton):
            widget.setEnabled(not busy)
        self.undoButton.setEnabled(not busy and bool(self.grid.undo_stack))
        self.redoButton.setEnabled(not busy and bool(self.grid.redo_stack))
        self.cancelButton.setEnabled(busy)
        self.progressBar.setRange(0, 0 if busy else 100)
        self.progressBar.setValue(0)
        self.update_topology_controls()
        for panel in (self.ga_panel, self.accuracy_panel):
            panel.set_busy(busy)

    def start_experiment(self, panel):
        if self.busy:
            return
        try:
            project = validate_project(self.project_data(), for_run=True)
            settings = panel.settings(project)
            if panel.kind == "ga" and settings["ga_implementation"] == "reference_required":
                raise ValueError("Выберите «ThinWireMoM: GA + LS» или отдельный «Новый бинарный GA».")
            if panel.kind == "accuracy" and settings["accuracy_mode"] == "random_base":
                import math
                base_elements = sum(map(sum, project["cells"])) if settings["accuracy_mode"] == "current_additions" else settings["base_elements"]
                empty = len(project["cells"])**2-base_elements
                count = sum(min(math.comb(empty, k), settings["samples_per_k"]) if
                            (settings["single_mode"] if k == 1 else settings["multi_mode"]) == "sample"
                            else math.comb(empty, k) for k in range(1, settings["k_max"]+1))
                if count > settings["exhaustive_threshold"]:
                    if QtWidgets.QMessageBox.question(self, "Большой эксперимент", f"Комбинаций: {count}. Продолжить? Для сокращения выберите Random sample.",
                           QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                        return
                    settings["allow_large_exhaustive"] = True
            self._experiment_panel, self._experiment_settings = panel, settings
            self.start_run(engine="experiment")
        except Exception as exc:
            self.show_error(exc)

    def start_run(self, checked=False, *, engine="scuff"):
        if self.busy:
            return
        try:
            config = validate_project(self.project_data(), for_run=True)
            if engine == "experiment":
                config["experiment"] = self._experiment_settings
            if engine in ("feko", "compare"):
                validate_options(config["feko"], config)
            if engine == "compare":
                config["feko"]["run_solver"] = True
            if engine in ("topology-update", "topology-compare"):
                if not self._topology_manifest or self._topology_manifest["signature"] != cache_signature(config):
                    raise ValueError("Сначала рассчитайте полную матрицу для текущих параметров.")
            if engine == "topology-compare":
                config["feko"]["run_solver"] = True
            base = Path(config["output_dir"]).expanduser().resolve()
            if engine == "experiment":
                base = base / (config["experiment"]["kind"]+"-benchmark")
            directory = base / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            directory.mkdir(parents=True, exist_ok=False)
            write_json(directory / "project.json", config)
            write_json(directory / "run.json", {"status": "running", "engine": engine})
        except Exception as exc:
            self.show_error(exc)
            return
        self.run_dir = directory
        self._run_config = config
        self._comparison = {}
        self._patterns = []
        self.comparison.show({}, 0)
        self.comparisonFrequency.clear()
        self.comparisonState.setText("Расчёт")
        self.load_pattern(None, config)
        self._engine = engine
        self._buffer = b""
        self._done = self._error = None
        self._runtime = None
        self._cancelled = False
        self._result_stale = True
        self._started = time.monotonic()
        self._stage = "Запуск"
        self.resultText.setPlainText("Расчёт выполняется.")
        self.logText.clear()
        self.logText.appendPlainText(str(directory))
        self.openResultButton.setEnabled(True)
        self.resultsTabs.setCurrentWidget(self.logTab)
        if engine == "experiment":
            self._experiment_panel.begin(directory)
            self.resultsTabs.setCurrentWidget(self._experiment_panel)
        process = QtCore.QProcess(self)
        self.process = process
        process.setWorkingDirectory(str(ROOT))
        environment = QtCore.QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUNBUFFERED", "1")
        environment.insert("OPENBLAS_NUM_THREADS", "1")
        process.setProcessEnvironment(environment)
        process.readyReadStandardOutput.connect(self.read_output)
        process.readyReadStandardError.connect(self.read_errors)
        process.finished.connect(self.process_finished)
        process.errorOccurred.connect(self.process_error)
        self.set_busy(True)
        self.timer.start()
        self.update_elapsed()
        program, arguments = worker_command(directory / "project.json", engine)
        process.start(program, arguments)

    def read_output(self):
        if self.process is None:
            return
        chunk = bytes(self.process.readAllStandardOutput())
        if self._engine == "experiment":
            with (self.run_dir / "worker.stdout.log").open("ab") as stream:
                stream.write(chunk)
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except ValueError:
                self.logText.appendPlainText(text)
                continue
            kind = event.get("event")
            if self._engine == "experiment" and self._experiment_panel:
                self._experiment_panel.consume_event(event)
            if kind == "runtime":
                self._runtime = event
                self.logText.appendPlainText(f"Python {event['version']}: {event['executable']}")
            elif kind == "stage":
                self._stage = event["message"]
                self.logText.appendPlainText(self._stage)
                self.update_elapsed()
            elif kind == "log":
                self.logText.appendPlainText(event["message"])
            elif kind == "mesh":
                self.logText.appendPlainText(f"Треугольников: {event['triangles']}; неизвестных: {event['unknowns']}")
            elif kind == "done":
                self._done = event
            elif kind == "comparison":
                self.show_comparison(event["data"])
            elif kind == "topology":
                self.attach_topology(event["cache"], event["state"])
                self.dirty = True
                self.setWindowTitle(self.title())
            elif kind == "error":
                self._error = event["message"]
                self.logText.appendPlainText(self._error)

    def read_errors(self):
        if self.process is not None:
            text = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace").strip()
            if text:
                self.logText.appendPlainText(text)
                if self._engine == "experiment":
                    with (self.run_dir / "worker.stderr.log").open("a", encoding="utf-8") as stream:
                        stream.write(text+"\n")

    def process_error(self, error):
        if error == QtCore.QProcess.FailedToStart:
            self._error = "Не удалось запустить Python 3.11 через py: " + self.process.errorString()
            self.process_finished(-1, QtCore.QProcess.CrashExit)

    def process_finished(self, exit_code, exit_status):
        if self.process is None:
            return
        self.read_output()
        self.read_errors()
        self.timer.stop()
        success = exit_code == 0 and self._done is not None
        if self._engine == "experiment":
            state = self._done["status"] if success else "cancelled" if self._cancelled else "failed"
            success = success and state == "completed"
            text = self._done["message"] if self._done else self._error or "Эксперимент остановлен"
            self.resultText.setPlainText(f"{text}\nconfig.json\nsummary.json\nevaluations.csv\nplots/\n{self.run_dir}")
            self.resultsTabs.setCurrentWidget(self._experiment_panel)
            self._experiment_panel.active = False
            self._experiment_panel.statusLabel.setText(text)
        elif success and (self._engine == "compare" or self._engine.startswith("topology-")):
            state, text = "solved", self._done["message"]
            self.show_comparison(self._done["comparison"])
            self.load_pattern(self._done["pattern_path"], self._run_config)
            self.resultText.setPlainText(f"comparison.csv\ncomparison.json\nscuff/\nfeko/\n{self.run_dir}")
            self._result_stale = False
            self.resultsTabs.setCurrentWidget(self.comparisonTab)
        elif success and self._engine == "feko":
            state = self._done["status"]
            text = self._done["message"]
            lines = [text]
            for sample in self._done.get("summary", []):
                db = "-inf" if sample["s11_db"] is None else f"{sample['s11_db']:.3f}"
                lines.append(f"{sample['frequency_hz']/1e9:g} ГГц: S11 = {db} дБ; |S11| = {sample['s11_magnitude']:.5f}")
                if sample.get("gain_peak_dbi") is not None:
                    lines.append(f"Gain max = {sample['gain_peak_dbi']:.3f} dBi")
            self.resultText.setPlainText("\n".join(lines + self._done["files"] + [self._done["directory"]]))
            if state == "solved":
                self.show_comparison(dict(project=self._run_config, scuff=[], feko=self._done["summary"],
                                          feed_models=dict(feko="planar_delta_gap_voltage")))
                self.load_pattern(Path(self._done["directory"]) / "farfield.json" if self._run_config["feko"]["compute_pattern"] else None, self._run_config)
            self._result_stale = False
            self.resultsTabs.setCurrentWidget(self.resultTab)
        elif success:
            n = self._done["unknowns"]
            residual = self._done["checks"]["relative_residual"]
            timing = self._done.get("timings_s", {})
            if timing:
                self.logText.appendPlainText(
                    f"Сборка M: {timing['assembly']:.3f} с; возбуждение b: {timing['rhs']:.3f} с; "
                    f"LU: {timing['lu']:.4f} с; решение: {timing['solve']:.4f} с")
            self.resultText.setPlainText(f"M: {n} × {n}, complex128\nНевязка: {residual:.3e}\n"
                                        f"M.npy\nsystem.npz: M, b, x, сетка, метаданные\n{self.run_dir}")
            self._result_stale = False
            state, text = "success", "Расчёт завершён"
            self.resultsTabs.setCurrentWidget(self.resultTab)
        elif self._cancelled:
            state, text = "cancelled", "Расчёт остановлен; частичные файлы сохранены"
            self.resultText.setPlainText(text)
        else:
            state = "failed"
            text = self._error or f"Процесс завершился с кодом {exit_code}"
            self.resultText.setPlainText("Ошибка: " + text)
            self.resultsTabs.setCurrentWidget(self.logTab)
        if not success:
            self.comparisonState.setText("Остановлено" if self._cancelled else "Ошибка расчёта")
        try:
            write_json(self.run_dir / "run.json", {"status": state, "exit_code": exit_code,
                                                   "engine": self._engine,
                                                   "elapsed_s": time.monotonic()-self._started,
                                                   "message": text})
        except OSError as exc:
            self.logText.appendPlainText(str(exc))
        self.logText.appendPlainText(text)
        self.runStatus.setText(f"{text if state != 'failed' else 'Ошибка расчёта'} · {time.monotonic()-self._started:.1f} с")
        self.process.deleteLater()
        self.process = None
        self.set_busy(False)
        if success:
            self.progressBar.setValue(100)
        self.completed.emit(success)
        if self._closing:
            self._closing = False
            QtCore.QTimer.singleShot(0, self.close)

    def update_elapsed(self):
        stage = "Остановка" if self._cancelled else self._stage
        self.runStatus.setText(f"{stage} · {int(time.monotonic()-self._started)} с")

    def cancel_run(self, *, force=False):
        if not self.busy or self.kill_process is not None:
            return
        pid = self.process.processId()
        if not pid:
            self.statusbar.showMessage("Процесс запускается. Повторите остановку через секунду.", 3000)
            return
        self._cancelled = True
        self.cancelButton.setEnabled(False)
        if self._engine == "experiment" and not force:
            (self.run_dir / "cancel.flag").touch()
            self._experiment_panel.cancelButton.setEnabled(False)
            owned = self.process
            QtCore.QTimer.singleShot(30000, lambda: self.cancel_run(force=True) if self.process is owned and self.busy else None)
            self.update_elapsed()
            return
        self.update_elapsed()
        if os.name != "nt":
            self.process.kill()
            return
        # Terminate the owned worker AND its native SCUFF child, not just Python.
        killer = QtCore.QProcess(self)
        self.kill_process = killer
        killer.finished.connect(self.kill_finished)
        killer.errorOccurred.connect(self.kill_error)
        executable = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe")
        killer.start(executable, ["/PID", str(pid), "/T", "/F"])

    def kill_error(self, error):
        if error == QtCore.QProcess.FailedToStart:
            self.kill_finished(-1, QtCore.QProcess.CrashExit)

    def kill_finished(self, code, status):
        if self.kill_process is None:
            return
        if code and self.busy:
            self._cancelled = False
            self._closing = False
            self.cancelButton.setEnabled(True)
            self.logText.appendPlainText("Не удалось остановить дерево процессов. Повторите остановку.")
        self.kill_process.deleteLater()
        self.kill_process = None

    def open_results(self):
        if self.run_dir:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.run_dir)))

    def closeEvent(self, event):
        if self.busy:
            answer = QtWidgets.QMessageBox.question(
                self, "Идёт расчёт", "Остановить расчёт и закрыть окно?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
            if answer == QtWidgets.QMessageBox.Yes:
                self._closing = True
                self.cancel_run()
            event.ignore()
            return
        if self.kill_process is not None:
            QtCore.QTimer.singleShot(100, self.close)
            event.ignore()
            return
        if self.confirm_discard():
            event.accept()
        else:
            event.ignore()


def create_application(argv):
    # The wheel's embedded qt.conf can corrupt a Cyrillic Windows user path.
    qt_root = Path(PyQt5.__file__).resolve().parent / "Qt5"
    if os.name == "nt":
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        for name, path in (("QTWEBENGINEPROCESS_PATH", qt_root / "bin/QtWebEngineProcess.exe"),
                           ("QTWEBENGINE_RESOURCES_PATH", qt_root / "resources"),
                           ("QTWEBENGINE_LOCALES_PATH", qt_root / "translations/qtwebengine_locales")):
            length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
            os.environ[name] = buffer.value if 0 < length < len(buffer) else str(path)
    plugins = qt_root / "plugins"
    QtCore.QCoreApplication.addLibraryPath(str(plugins))
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps)
    app = QtWidgets.QApplication(argv)
    app.setFont(QtGui.QFont("Segoe UI", 9))
    return app


def main():
    app = create_application(sys.argv)
    window = AntennaWindow()
    if len(sys.argv) > 1:
        try:
            if Path(sys.argv[1]).name == "comparison.json":
                window.load_run(sys.argv[1])
            else:
                window.apply_project(read_project(sys.argv[1]), sys.argv[1])
        except Exception as exc:
            window.show_error(exc)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
