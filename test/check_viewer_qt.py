"""Native Qt viewport resize, render and screenshot regression check."""
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gui.app import create_application, AntennaWindow
from PyQt5 import QtCore


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    args = parser.parse_args()
    directory = Path(args.directory)
    data = json.loads((directory / "comparison.json").read_text(encoding="utf-8"))
    app = create_application([])
    window = AntennaWindow()
    window.apply_project(data["project"])
    window.show_comparison(data)
    window.load_pattern(directory / "feko/farfield.json", data["project"])
    window.show()
    window.resultsTabs.setCurrentWidget(window.patternTab)
    sizes = iter([(1180, 820), (900, 650)])

    def next_size():
        try:
            window.resize(*next(sizes))
            QtCore.QTimer.singleShot(1800, capture)
        except StopIteration:
            app.quit()

    def capture():
        window._viewer.reset_view()
        QtCore.QTimer.singleShot(500, lambda: window._viewer.page().runJavaScript(
            "({width:innerWidth,height:innerHeight,info:window.viewerInfo()})", inspected))

    def inspected(result):
        try:
            print(window._viewer.width(), window._viewer.height(), result, flush=True)
            assert result["width"] == window._viewer.width()
            assert result["height"] == window._viewer.height()
            assert result["info"]["calls"] > 5
            window.grab().save(str(directory / f"qt-pattern-{window.width()}.png"))
            next_size()
        except Exception as exc:
            print(repr(exc), flush=True)
            app.exit(1)

    QtCore.QTimer.singleShot(1800, next_size)
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
