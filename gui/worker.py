"""One calculation per process; stdout carries JSON progress events."""
import json
import importlib
import os
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gui.project import read_project, validate_project


def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields), ensure_ascii=True), flush=True)


def run(project_path, engine="scuff"):
    config = validate_project(read_project(project_path), for_run=True)
    directory = Path(project_path).resolve().parent
    if engine == "experiment":
        from experiments.runner import run_experiment
        run_experiment(config, config["experiment"], directory, emit)
        return
    if engine.startswith("topology-"):
        from gui.topology_workflow import run_topology
        run_topology(config, directory, emit, prepare=engine == "topology-prepare", compare=engine == "topology-compare")
        return
    if engine == "feko":
        from feko.runner import run_feko
        run_feko(config, directory, emit)
        return
    if engine == "compare":
        from gui.calculation import run_comparison
        run_comparison(config, directory, emit)
        return
    for module in ("numpy", "gmsh"):
        try:
            importlib.import_module(module)
        except ImportError as exc:
            raise RuntimeError(
                f"Python {sys.version.split()[0]} ({sys.executable}): не удалось загрузить {module}. "
                "Установите зависимости: py -3.11 -m pip install -r scripts/requirements.txt"
            ) from exc
    from gui.calculation import run_scuff
    emit("done", **run_scuff(config, directory, emit))


if __name__ == "__main__":
    emit("runtime", executable=sys.executable, version=sys.version.split()[0], pid=os.getpid())
    try:
        engines = ("compare", "feko", "topology-prepare", "topology-update", "topology-compare", "experiment")
        run(sys.argv[1], next((name for name in engines if "--"+name in sys.argv[2:]), "scuff"))
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit("error", message=str(exc))
        sys.exit(1)
