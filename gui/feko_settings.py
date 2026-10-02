"""Designer-backed FEKO settings dialog."""
from pathlib import Path
from PyQt5 import QtWidgets, uic
from feko.config import ANGLE_STEPS, validate_options


class FekoSettingsDialog(QtWidgets.QDialog):
    def __init__(self, options, parent=None):
        super().__init__(parent)
        uic.loadUi(str(Path(__file__).with_name("feko_settings.ui")), self)
        options = validate_options(options)
        self._compute_pattern = options["compute_pattern"]
        for widget in self.findChildren(QtWidgets.QWidget):
            widget.setToolTip("")
        self.executableEdit.setText(options["cadfeko_exe"])
        self.impedanceSpin.setValue(options["reference_ohm"])
        self.angleCombo.setCurrentIndex(ANGLE_STEPS.index(options["angle_step_deg"]))
        self.timeoutSpin.setValue(options["timeout_s"])
        self.sweepGroup.setChecked(options["sweep_enabled"])
        self.startSpin.setValue(options["start_ghz"])
        self.stopSpin.setValue(options["stop_ghz"])
        self.pointsSpin.setValue(options["frequency_points"])
        self.solveCheck.setChecked(options["run_solver"])
        self.browseButton.clicked.connect(self.choose_executable)
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)

    def options(self):
        return dict(cadfeko_exe=self.executableEdit.text().strip(),
                    reference_ohm=self.impedanceSpin.value(), angle_step_deg=ANGLE_STEPS[self.angleCombo.currentIndex()],
                    timeout_s=self.timeoutSpin.value(), sweep_enabled=self.sweepGroup.isChecked(),
                    start_ghz=self.startSpin.value(), stop_ghz=self.stopSpin.value(),
                    frequency_points=self.pointsSpin.value(), run_solver=self.solveCheck.isChecked(),
                    compute_pattern=self._compute_pattern)

    def choose_executable(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "CADFEKO", self.executableEdit.text(), "CADFEKO (cadfeko.exe)")
        if path:
            self.executableEdit.setText(path)

    def accept(self):
        try:
            validate_options(self.options())
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "FEKO", str(exc))
            return
        super().accept()
