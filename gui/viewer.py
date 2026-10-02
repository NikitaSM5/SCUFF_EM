"""Local Three.js view hosted in Qt; it never sends antenna data to the network."""
import json
from pathlib import Path
from PyQt5 import QtCore, QtGui, QtWebEngineWidgets


class PatternView(QtWebEngineWidgets.QWebEngineView):
    error = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setContextMenuPolicy(QtCore.Qt.NoContextMenu)
        self._ready = False
        self._payload = None
        self.loadFinished.connect(self.loaded)
        self.setUrl(QtCore.QUrl.fromLocalFile(str(Path(__file__).with_name("web") / "viewer.html")))

    def loaded(self, success):
        self._ready = success
        if not success:
            self.error.emit("Не удалось загрузить 3D-просмотр")
        elif self._payload:
            self.update_scene(**self._payload)

    def update_scene(self, project, pattern=None, show_antenna=True, show_pattern=True, scale=0,
                     feed_model="planar_delta_gap_voltage"):
        self._payload = dict(project=project, pattern=pattern, show_antenna=show_antenna,
                             show_pattern=show_pattern, scale=scale, feed_model=feed_model)
        if self._ready:
            palette = self.palette()
            payload = dict(self._payload, background=palette.color(QtGui.QPalette.Base).name(),
                           foreground=palette.color(QtGui.QPalette.Text).name())
            self.page().runJavaScript("window.setScene(" + json.dumps(payload, ensure_ascii=True, allow_nan=False) + ")")

    def reset_view(self):
        if self._ready:
            self.page().runJavaScript("window.fitView()")
