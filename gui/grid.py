"""Editable pixel mask hosted by a standard Designer QGraphicsView."""
import copy
import math

from PyQt5 import QtCore, QtGui, QtWidgets


class GridItem(QtWidgets.QGraphicsItem):
    CELL = 32

    def __init__(self, editor):
        super().__init__()
        self.editor = editor

    def boundingRect(self):
        side = len(self.editor.cells) * self.CELL
        return QtCore.QRectF(-30, -30, side + 36, side + 36)

    def paint(self, painter, option, widget=None):
        cells = self.editor.cells
        n, s = len(cells), self.CELL
        palette = self.editor.view.palette()
        painter.fillRect(QtCore.QRectF(0, 0, n*s, n*s), palette.brush(QtGui.QPalette.Base))
        for r, row in enumerate(cells):
            for c, metal in enumerate(row):
                if metal:
                    painter.fillRect(QtCore.QRectF(c*s, r*s, s, s), palette.brush(QtGui.QPalette.Highlight))
        pen = QtGui.QPen(palette.color(QtGui.QPalette.Mid))
        pen.setCosmetic(True)
        painter.setPen(pen)
        for i in range(n + 1):
            painter.drawLine(QtCore.QLineF(i*s, 0, i*s, n*s))
            painter.drawLine(QtCore.QLineF(0, i*s, n*s, i*s))
        painter.setPen(palette.color(QtGui.QPalette.Text))
        painter.setFont(QtGui.QFont("Segoe UI", 8))
        for i in range(n):
            painter.drawText(QtCore.QRectF(i*s, -27, s, 24), QtCore.Qt.AlignCenter, str(i))
            painter.drawText(QtCore.QRectF(-28, i*s, 24, s), QtCore.Qt.AlignCenter, str(i))
        if self.editor.feed is not None:
            r, c = self.editor.feed
            center = QtCore.QPointF((c + .5)*s, (r + .5)*s)
            painter.setPen(QtGui.QPen(palette.color(QtGui.QPalette.HighlightedText), 1.6))
            painter.drawLine(QtCore.QLineF(center.x(), r*s+2, center.x(), (r+1)*s-2))
            painter.drawLine(center + QtCore.QPointF(-9, 0), center + QtCore.QPointF(9, 0))
            painter.drawLine(center + QtCore.QPointF(9, 0), center + QtCore.QPointF(5, -4))
            painter.drawLine(center + QtCore.QPointF(9, 0), center + QtCore.QPointF(5, 4))


class GridEditor(QtCore.QObject):
    changed = QtCore.pyqtSignal()
    message = QtCore.pyqtSignal(str)

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.cells = [[0]*8 for _ in range(8)]
        self.feed = None
        self.mode = "metal"
        self.undo_stack, self.redo_stack = [], []
        self.stroke_value = None
        self.last_cell = None
        self.auto_fit = True
        self.scene = QtWidgets.QGraphicsScene(self)
        self.item = GridItem(self)
        self.scene.addItem(self.item)
        view.setScene(self.scene)
        view.setRenderHint(QtGui.QPainter.Antialiasing)
        view.setMouseTracking(True)
        view.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        view.viewport().installEventFilter(self)
        self.redraw(fit=True)

    def snapshot(self):
        return copy.deepcopy(self.cells), self.feed

    def remember(self):
        self.undo_stack.append(self.snapshot())
        self.undo_stack = self.undo_stack[-100:]
        self.redo_stack.clear()

    def load(self, cells, feed=None, *, reset_history=True):
        self.item.prepareGeometryChange()
        self.cells = copy.deepcopy(cells)
        self.feed = tuple(feed) if feed is not None else None
        self.stroke_value = self.last_cell = None
        if reset_history:
            self.undo_stack.clear()
            self.redo_stack.clear()
        self.redraw(fit=True)
        self.changed.emit()

    def redraw(self, *, fit=False):
        self.scene.setSceneRect(self.item.boundingRect())
        self.item.update()
        if fit:
            self.fit()

    def fit(self):
        self.auto_fit = True
        self.view.fitInView(self.item.boundingRect(), QtCore.Qt.KeepAspectRatio)

    def resize_grid(self, n):
        if n == len(self.cells):
            return
        self.remember()
        cells = [[self.cells[r][c] if r < len(self.cells) and c < len(self.cells) else 0
                  for c in range(n)] for r in range(n)]
        feed = self.feed if self.feed is None or max(self.feed) < n else None
        self.load(cells, feed, reset_history=False)

    def clear(self):
        if not any(map(any, self.cells)):
            return
        self.remember()
        self.load([[0]*len(self.cells) for _ in self.cells], reset_history=False)

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.snapshot())
            self.load(*self.undo_stack.pop(), reset_history=False)

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.snapshot())
            self.load(*self.redo_stack.pop(), reset_history=False)

    def cell_at(self, position):
        point = self.view.mapToScene(position)
        r, c = math.floor(point.y()/32), math.floor(point.x()/32)
        return (r, c) if 0 <= r < len(self.cells) and 0 <= c < len(self.cells) else None

    def paint_cell(self, cell):
        r, c = cell
        if self.cells[r][c] != self.stroke_value:
            self.cells[r][c] = self.stroke_value
            if self.feed == cell and not self.stroke_value:
                self.feed = None
            self.item.update()
            self.changed.emit()

    def extend_stroke(self, cell):
        if cell is None:
            self.last_cell = None
            return
        # Interpolate fast mouse moves so a painted stroke has no skipped cells.
        previous = self.last_cell or cell
        steps = max(abs(cell[0]-previous[0]), abs(cell[1]-previous[1]), 1)
        for i in range(steps + 1):
            self.paint_cell(tuple(round(previous[j] + (cell[j]-previous[j])*i/steps) for j in (0, 1)))
        self.last_cell = cell

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind not in (QtCore.QEvent.Resize, QtCore.QEvent.Wheel,
                        QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseMove,
                        QtCore.QEvent.MouseButtonRelease):
            return False
        if kind == QtCore.QEvent.Resize and self.auto_fit:
            QtCore.QTimer.singleShot(0, self.fit)
        if not self.view.isEnabled():
            return False
        if kind == QtCore.QEvent.Wheel:
            self.auto_fit = False
            factor = 1.2 if event.angleDelta().y() > 0 else 1/1.2
            scale = self.view.transform().m11() * factor
            if .05 <= scale <= 12:
                self.view.scale(factor, factor)
            return True
        if kind == QtCore.QEvent.MouseButtonPress:
            cell = self.cell_at(event.pos())
            if cell is None or event.button() not in (QtCore.Qt.LeftButton, QtCore.Qt.RightButton):
                return False
            if self.mode == "feed" and event.button() == QtCore.Qt.LeftButton:
                if not self.cells[cell[0]][cell[1]]:
                    self.message.emit("Порт можно поставить только на металл.")
                elif self.feed != cell:
                    self.remember()
                    self.feed = cell
                    self.item.update()
                    self.changed.emit()
                return True
            self.remember()
            self.stroke_value = 0 if event.button() == QtCore.Qt.RightButton else 1 - self.cells[cell[0]][cell[1]]
            self.last_cell = cell
            self.paint_cell(cell)
            self.changed.emit()
            return True
        if kind == QtCore.QEvent.MouseMove and self.stroke_value is not None:
            self.extend_stroke(self.cell_at(event.pos()))
            return True
        if kind == QtCore.QEvent.MouseButtonRelease and self.stroke_value is not None:
            self.extend_stroke(self.cell_at(event.pos()))
            self.stroke_value = self.last_cell = None
            return True
        return super().eventFilter(watched, event)
