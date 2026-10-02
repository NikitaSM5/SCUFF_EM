"""Read-only, frequency-indexed table of like-for-like port quantities."""
from PyQt5 import QtCore, QtWidgets


METRICS = [("S11, дБ", "s11_db", 4), ("Gain max, dBi", "gain_peak_dbi", 3)]


class ComparisonTable:
    def __init__(self, table):
        self.table = table
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["Параметр", "SCUFF-EM", "FEKO", "Δ FEKO − SCUFF"])
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        table.setRowCount(len(METRICS))
        self.show({}, 0)

    def show(self, data, index):
        a = data.get("scuff", [])
        b = data.get("feko", [])
        a = a[index] if 0 <= index < len(a) else {}
        b = b[index] if 0 <= index < len(b) else {}
        rows = []
        for title, key, digits in METRICS:
            left, right = a.get(key), b.get(key)
            difference = right-left if left is not None and right is not None else None
            values = ["—" if v is None else f"{v:.{digits}f}" for v in (left, right, difference)]
            if key == "s11_db":
                if a.get("s11_magnitude") == 0:
                    values[0] = "−∞"
                if b.get("s11_magnitude") == 0:
                    values[1] = "−∞"
            rows.append([title, *values])
        for row, values in enumerate(rows):
            for col, text in enumerate(values):
                item = QtWidgets.QTableWidgetItem(text)
                item.setTextAlignment((QtCore.Qt.AlignLeft if col == 0 else QtCore.Qt.AlignHCenter) | QtCore.Qt.AlignVCenter)
                self.table.setItem(row, col, item)
        self.table.resizeRowsToContents()
        for row in range(len(rows)):
            self.table.setRowHeight(row, max(38, self.table.rowHeight(row)))
