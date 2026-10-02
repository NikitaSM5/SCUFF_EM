"""Select the same Windows Python runtime as run.cmd before importing Qt."""
import os
from pathlib import Path
import subprocess
import sys


RELAUNCH_MARKER = "SCUFF_GUI_PYTHON_RELAUNCHED"


def ensure_gui_python():
    if os.name != "nt" or sys.version_info[:2] == (3, 11):
        return
    if os.environ.get(RELAUNCH_MARKER) == "1":
        raise SystemExit("GUI requires Python 3.11; the Python launcher selected another version.")

    env = os.environ.copy()
    env[RELAUNCH_MARKER] = "1"
    # Do not carry another interpreter's standard-library override into 3.11.
    env.pop("PYTHONHOME", None)
    command = ["py", "-3.11", str(Path(__file__).with_name("app.py")), *sys.argv[1:]]
    try:
        code = subprocess.call(command, env=env)
    except OSError as exc:
        raise SystemExit(
            "Cannot start Python 3.11. Install it with the Windows Python launcher, "
            "then run: py -3.11 -m pip install -r gui/requirements.txt"
        ) from exc
    raise SystemExit(code)
