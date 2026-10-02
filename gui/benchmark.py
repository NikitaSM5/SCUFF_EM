"""Qt views for experiments; numerical work remains in the existing process worker."""
import json
import math
from pathlib import Path
import shutil

from PyQt5 import QtCore, QtGui, QtWidgets, uic

from experiments.config import defaults, validate
from gui.grid import GridEditor

COMBOS = dict(objective=["reflection", "gain", "realized_gain", "reference_gain"], mode=["replay", "independent"],
              accuracy_mode=["current_additions", "random_base"],
              single_mode=["exhaustive", "sample"], multi_mode=["exhaustive", "sample"],
              ga_implementation=["thinwire_pixel_v1", "binary_tournament_v1", "reference_required"])


class BenchmarkPanel(QtWidgets.QWidget):
    run_requested = QtCore.pyqtSignal()
    cancel_requested = QtCore.pyqtSignal()

    def __init__(self, kind, host):
        super().__init__(host)
        self.kind, self.host = kind, host
        uic.loadUi(str(Path(__file__).with_name(kind+"_benchmark.ui")), self)
        self.directory = None
        self.active = False
        self.values = {}
        self.best_grid = GridEditor(self.bestView)
        self.bestView.setEnabled(False)
        self.metricsTable.setColumnCount(2)
        self.metricsTable.setHorizontalHeaderLabels(["Показатель", "Значение"])
        self.metricsTable.verticalHeader().hide()
        self.metricsTable.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.logText.document().setMaximumBlockCount(4000)
        self.runButton.clicked.connect(self.run_requested)
        self.cancelButton.clicked.connect(self.cancel_requested)
        self.resetButton.clicked.connect(self.reset)
        self.folderButton.clicked.connect(self.open_folder)
        self.exportButton.clicked.connect(self.export_csv)
        self.loadButton.clicked.connect(self.load_config)
        self.plotChoice.currentIndexChanged.connect(self.show_plot)
        self.preview_timer = QtCore.QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(150)
        self.preview_timer.timeout.connect(self.preview_space)
        for name in defaults(kind):
            widget = getattr(self, name, None)
            if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                widget.valueChanged.connect(lambda: self.preview_timer.start())
                widget.valueChanged.connect(host.mark_changed)
            elif isinstance(widget, QtWidgets.QComboBox):
                widget.currentIndexChanged.connect(lambda: self.preview_timer.start())
                widget.currentIndexChanged.connect(host.mark_changed)
            elif isinstance(widget, QtWidgets.QCheckBox):
                widget.toggled.connect(host.mark_changed)
            elif isinstance(widget, QtWidgets.QLineEdit):
                widget.textChanged.connect(host.mark_changed)
        self.apply_settings(defaults(kind))
        if kind == "ga":
            self.ga_implementation.currentIndexChanged.connect(self.algorithm_controls)
            self.algorithm_controls()
        else:
            self.accuracy_mode.currentIndexChanged.connect(self.accuracy_controls)
            self.accuracy_controls()
        self.set_busy(False)

    def apply_settings(self, settings):
        data = dict(defaults(self.kind), **settings)
        if self.kind == "accuracy" and settings and "accuracy_mode" not in settings:
            data["accuracy_mode"] = "random_base"
        for name, value in data.items():
            widget = getattr(self, name, None)
            if name in COMBOS and isinstance(widget, QtWidgets.QComboBox):
                widget.setCurrentIndex(COMBOS[name].index(value))
            elif isinstance(widget, QtWidgets.QCheckBox):
                widget.setChecked(value)
            elif isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                widget.setValue(value)
            elif isinstance(widget, QtWidgets.QLineEdit):
                widget.setText(value)
        if self.kind == "ga":
            self.algorithm_controls()
        else:
            self.accuracy_controls()

    def accuracy_controls(self):
        current = self.accuracy_mode.currentIndex() == 0
        for name in ("base_elements", "single_mode", "multi_mode", "incremental", "objective",
                     "refactor_interval", "max_changed_fraction", "state_cache_size", "diagnostic_direct", "diagnostic_scuff",
                     "exhaustive_threshold"):
            getattr(self, name).setVisible(not current)
            getattr(self, name+"Label").setVisible(not current)
        self.use_cacheLabel.setText("Кэш полной матрицы" if current else "Кэш расчётов")
        self.samples_per_kLabel.setText("Вариантов на каждое K" if current else "Выборка на k")
        self.runButton.setText("Рассчитать")
        self.preview_timer.start()

    def algorithm_controls(self):
        reference = self.ga_implementation.currentIndex() == 0
        for name in ("mutation_probability", "crossover_probability", "tournament_size", "elitism"):
            getattr(self, name).setVisible(not reference)
            getattr(self, name+"Label").setVisible(not reference)
        for name in ("initial_elements", "child_fraction", "offspring_mutation_rate", "remove_connect_probability",
                     "remove_method_probability", "crossover_connect_probability", "local_search_depth",
                     "ls_ready_percent", "subpopulation_ga_steps", "full_population_ga_steps", "ga_reference"):
            getattr(self, name).setVisible(reference)
            getattr(self, name+"Label").setVisible(reference)
        self.generationsLabel.setText("Итерации GA + LS" if reference else "Поколения")

    def settings(self, project, *, check=True):
        data = defaults(self.kind)
        for name in data:
            widget = getattr(self, name, None)
            if name in COMBOS and isinstance(widget, QtWidgets.QComboBox):
                data[name] = COMBOS[name][widget.currentIndex()]
            elif isinstance(widget, QtWidgets.QCheckBox):
                data[name] = widget.isChecked()
            elif isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                data[name] = widget.value()
            elif isinstance(widget, QtWidgets.QLineEdit):
                data[name] = widget.text().strip()
        return validate(data, project) if check else data

    def preview_space(self):
        if self.kind != "accuracy" or not hasattr(self.host, "grid"):
            return
        current = self.accuracy_mode.currentIndex() == 0
        empty = len(self.host.grid.cells)**2-(sum(map(sum, self.host.grid.cells)) if current else self.base_elements.value())
        if empty < 0:
            self.spaceLabel.setText("Базовых клеток больше размера сетки")
            return
        def fmt(value):
            return str(value) if value < 10**12 else f"~10^{int(math.log10(value))}"
        ks = list(range(1, min(4, self.k_max.value())+1))
        if self.k_max.value() > 4:
            ks.append(self.k_max.value())
        entries = []
        for k in ks:
            count = math.comb(empty, k)
            sampled = current or (self.single_mode.currentIndex() if k == 1 else self.multi_mode.currentIndex()) == 1
            planned = min(count, self.samples_per_k.value()) if sampled else count
            limit = "до" if current else "случаев"
            entries.append(f"+{k}: C({empty},{k}) = {fmt(count)}, {limit} {fmt(planned)}")
        self.spaceLabel.setText("; ".join(entries))

    def set_busy(self, busy):
        self.settingsScroll.setEnabled(not busy)
        if self.kind == "accuracy":
            self.accuracy_mode.setEnabled(not busy)
        for button in (self.runButton, self.resetButton, self.loadButton):
            button.setEnabled(not busy)
        self.cancelButton.setEnabled(busy and self.active)
        self.folderButton.setEnabled(self.directory is not None)
        self.exportButton.setEnabled(self.directory is not None and (self.directory / "evaluations.csv").is_file())

    def begin(self, directory):
        self.directory = Path(directory)
        self.active = True
        self.values.clear()
        self.logText.clear()
        self.plotChoice.clear()
        self.plotImage.clear()
        self.progressBar.setRange(0, 0)
        self.statusLabel.setText("Подготовка")
        self.pages.setCurrentWidget(self.progressTab)
        self.set_busy(True)

    def consume_event(self, event):
        kind = event["event"]
        if kind in ("stage", "log", "error"):
            self.logText.appendPlainText(event["message"])
            if kind != "log":
                self.statusLabel.setText(event["message"])
        elif kind == "evaluation":
            self.values.update(Backend=event["backend"], Evaluations=event["stats"]["evaluations"],
                               **{"Last fitness": event["fitness"], "Расчёт, с": "Кэш" if event["cached"] else event.get("compute_s"),
                                  "Полное время, с": event["evaluation_s"], "Best fitness": event.get("best_fitness"),
                                  "Средний расчёт, с": event.get("mean_compute_s")})
            for key, value in event["stats"].items():
                self.values[event["backend"]+" "+key] = value
            if event.get("best_cells"):
                self.best_grid.load(event["best_cells"], self.host.grid.feed)
        elif kind == "experiment_progress":
            self.values.update(Elapsed=event["elapsed_s"], Best=event.get("best_fitness"), Mean=event.get("mean_fitness"), Median=event.get("median_fitness"))
            if "generation" in event:
                self.values["Generation"] = f"{event['generation']} / {event['generations']}"
                self.values["Расчёт поколения, с"] = event.get("generation_compute_s")
                self.values["Полное время поколения, с"] = event["generation_s"]
            else:
                self.values["Series"] = event["group"]
                self.values["Added cells"] = event["k"]
            self.progressBar.setRange(0, event["total"])
            self.progressBar.setValue(event["evaluated"])
            if event.get("best_cells"):
                self.best_grid.load(event["best_cells"], self.host.grid.feed)
        elif kind == "live_plot":
            self.add_plot(Path(event["path"]), "Live")
        elif kind == "experiment_done":
            self.active = False
            self.statusLabel.setText({"completed": "Завершено", "cancelled": "Остановлено", "failed": "Ошибка"}[event["status"]])
            self.progressBar.setRange(0, 100)
            self.progressBar.setValue(100 if event["status"] == "completed" else 0)
            for name in event["plots"]:
                self.add_plot(self.directory / "plots" / name, Path(name).stem)
            for backend, values in event["summary"]["solvers"].items():
                for key in ("compute_s", "online_s", "end_to_end_s", "cache_hit_rate", "failures"):
                    self.values[backend+" "+key] = values[key]
            self.values["Ускорение FEKO / Schur"] = event["summary"].get("cold_pair_speedup", {}).get("mean")
            self.set_busy(False)
        self.metricsTable.setRowCount(len(self.values))
        for row, (name, value) in enumerate(self.values.items()):
            self.metricsTable.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
            self.metricsTable.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{value:.6g}" if isinstance(value, float) else str(value if value is not None else "")))

    def add_plot(self, path, title):
        existing = self.plotChoice.findText(title)
        if existing < 0:
            self.plotChoice.addItem(title, str(path))
        else:
            self.plotChoice.setItemData(existing, str(path))
        self.show_plot()

    def show_plot(self, *args):
        path = self.plotChoice.currentData()
        if path:
            pixmap = QtGui.QPixmap(path)
            self.plotImage.setPixmap(pixmap.scaled(self.plotImage.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "plotChoice"):
            self.show_plot()

    def reset(self):
        self.apply_settings(defaults(self.kind))
        self.logText.clear()
        self.values.clear()
        self.metricsTable.setRowCount(0)
        self.plotChoice.clear()
        self.plotImage.clear()
        self.progressBar.setValue(0)
        self.statusLabel.setText("Готово")
        self.directory = None
        n = len(self.host.grid.cells)
        self.best_grid.load([[0]*n for _ in range(n)])
        self.set_busy(False)

    def open_folder(self):
        if self.directory:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.directory)))

    def export_csv(self):
        if not self.directory:
            return
        source = self.directory / "addition-summary.csv"
        if not source.is_file():
            source = self.directory / "evaluations.csv"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Экспорт результатов", str(source), "CSV (*.csv)")
        if path:
            try:
                if Path(path).resolve() != source.resolve():
                    shutil.copyfile(source, path)
            except OSError as exc:
                self.host.show_error(exc)

    def load_config(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Повторить эксперимент", str(self.directory or self.host.outputEdit.text()), "Experiment (config.json)")
        if path:
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                if data["experiment"]["kind"] != self.kind:
                    raise ValueError("Выбрана конфигурация другого типа эксперимента")
                validate(data["experiment"], data["project"])
                if self.host.confirm_discard():
                    self.host.apply_project(data["project"])
                    self.apply_settings(data["experiment"])
                    self.pages.setCurrentWidget(self.settingsTab)
            except Exception as exc:
                self.host.show_error(exc)
