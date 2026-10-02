"""Real FEKO session reuse and solver-only timing smoke, without a GUI."""
from datetime import datetime
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.config import defaults, validate
from experiments.feko_backend import FekoFitnessBackend
from experiments.storage import Journal, write_json
from gui.project import default_project


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    root = ROOT/"results"/("feko-session-smoke-"+datetime.now().strftime("%Y%m%d-%H%M%S"))
    root.mkdir()
    project = default_project(root)
    project.update(cells=[[1, 1], [0, 0]], feed_cell=[0, 0], cell_size_mm=10, mesh_size_mm=5)
    project["feko"].update(angle_step_deg=30, timeout_s=120)
    settings = validate(dict(defaults("accuracy"), k_max=1, use_cache=False), project)

    def event(kind, **data):
        if kind in ("stage", "error", "log"):
            print(data["message"], flush=True)

    backend = FekoFitnessBackend(project, settings, root, Journal(root, event), lambda: None)
    started = time.perf_counter()
    try:
        first = backend.evaluate(project["cells"], force=True)
        second = backend.evaluate([[1, 1], [1, 1]], force=True)
        third = backend.evaluate(project["cells"], force=True)
        pid = first["diagnostics"]["session"]["pid"]
        assert all(r["compute_s"] is not None and r["compute_s"] > 0 for r in (first, second, third))
        assert second["diagnostics"]["session"]["pid"] == pid == third["diagnostics"]["session"]["pid"]
        assert not first["diagnostics"]["session"]["reused"]
        assert second["diagnostics"]["session"]["reused"] and third["diagnostics"]["session"]["reused"]
        assert second["timings_s"]["cad_startup"] == third["timings_s"]["cad_startup"] == 0
        for field in ("s11_real", "s11_imag", "gain_peak_dbi"):
            assert abs(first["rows"][0][field]-third["rows"][0][field]) < 1e-8
        write_json(root/"smoke.json", dict(records=[first, second, third], wall_s=time.perf_counter()-started))
        print("FIRST/SECOND/THIRD", [(r["compute_s"], r["evaluation_s"]) for r in (first, second, third)], flush=True)
    finally:
        backend.close()
    assert all(s.process.poll() is not None for s in backend.sessions.sessions)
    print("FEKO SESSION SMOKE OK", root, flush=True)


if __name__ == "__main__":
    main()
