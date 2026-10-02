"""In-memory full matrices and bounded nearest-topology LU/Schur operator states."""
from collections import OrderedDict
from pathlib import Path
import time

import numpy as np
from scipy.linalg import lu_factor, lu_solve

from antenna.schur import solve_topology
from antenna.topology import load_system, active_dofs, antenna_result
from gui.calculation import result_row
from gui.topology_config import read_cache_manifest
from .backends import FitnessBackend
from .storage import file_hash, key, write_json


class SchurFitnessBackend(FitnessBackend):
    name = "schur"

    def __init__(self, project, settings, directory, journal, cancel, manifest):
        start = time.perf_counter()
        manifest = Path(manifest)
        data = read_cache_manifest(manifest)
        fingerprint = dict(full_matrix=file_hash(manifest), refactor_interval=settings["refactor_interval"],
                           condition_limit=settings["condition_limit"], max_changed_fraction=settings["max_changed_fraction"],
                           direct_diagnostic=settings["diagnostic_direct"])
        super().__init__(project, settings, directory, journal, cancel, fingerprint)
        self.full = [load_system(manifest.parent / item["file"]) for item in data["systems"]]
        self.states = OrderedDict()
        self.setup_s = time.perf_counter()-start
        self.preferred_parent = None

    def calculate(self, cells, directory):
        mask = np.asarray(cells, dtype=np.uint8)
        candidates = list(self.states)
        if self.preferred_parent in self.states:
            parent_id = self.preferred_parent
        else:
            parent_id = min(candidates, key=lambda ident: np.count_nonzero(self.states[ident][0] != mask)) if candidates else None
        old_states = self.states[parent_id][1] if parent_id else [None]*len(self.full)
        rows, diagnostics, new_states, artifacts = [], [], [], []
        mapping_s, postprocess_s = 0.0, 0.0
        for index, (full, previous) in enumerate(zip(self.full, old_states)):
            self.cancel()
            started = time.monotonic()
            tick = time.perf_counter()
            target, _ = active_dofs(full, cells)
            mapping_s += time.perf_counter()-tick
            state, x, diag = solve_topology(full.M, full.b, target, previous,
                                          refactor_interval=self.settings["refactor_interval"],
                                          condition_limit=self.settings["condition_limit"],
                                          max_changed_fraction=self.settings["max_changed_fraction"])
            new_states.append(state)
            if diag["method"] == "schur":
                self.stats["schur_updates"] += 1
            elif diag["method"] in ("initial_lu", "lu_fallback"):
                self.stats["refactorizations"] += 1
            self.journal.event("log", message=f"Schur {diag['method']}: +{diag['added_dofs']}/-{diag['removed_dofs']} DOF, "
                               f"r={diag['relative_residual']:.2e}, {diag['solve_s']:.5f} s" + (f"; {diag['reason']}" if diag["reason"] else ""))
            folder = directory / f"frequency-{index:04d}"
            folder.mkdir()
            wrapped = dict(active=state.active, x=x, checks=dict(relative_residual=diag["relative_residual"]),
                           update=dict(diag, elapsed_s=diag["solve_s"]))
            tick = time.perf_counter()
            result = antenna_result(full, cells, wrapped, folder)
            postprocess_s += time.perf_counter()-tick
            if self.settings["diagnostic_direct"]:
                tick = time.perf_counter()
                direct = lu_solve(lu_factor(result.M), result.b)
                diag["direct_s"] = time.perf_counter()-tick
                diag["current_relative_error_vs_direct"] = float(np.linalg.norm(x-direct)/max(np.linalg.norm(direct), 1e-300))
            current_file = folder / "currents.npz"
            np.savez_compressed(current_file, x=x, b=result.b, active=state.active, rwg=result.rwg,
                                vertices_mm=result.vertices_mm, triangles=result.triangles)
            gain, events = {}, []
            tick = time.perf_counter()
            rows.append(result_row(result, self.project, folder,
                                   lambda kind, **data: events.append((kind, data)), started,
                                   persist_gain=False, gain_data=gain))
            postprocess_s += time.perf_counter()-tick
            for kind, data in events:
                self.journal.event(kind, **data)
            if gain:
                write_json(folder/"gain.json", gain)
            diagnostics.append(diag)
            write_json(folder / "diagnostics.json", diag)
            artifacts.append(dict(path=str(current_file), sha256=file_hash(current_file)))
        ident = key(cells)
        self.states[ident] = (mask, new_states)
        self.states.move_to_end(ident)
        while len(self.states) > self.settings["state_cache_size"]:
            self.states.popitem(last=False)
        return dict(rows=rows, diagnostics=diagnostics, artifacts=artifacts, parent_candidate_id=parent_id,
                    timings_s=dict(solve=sum(d["solve_s"] for d in diagnostics), mapping=mapping_s,
                                   postprocessing=postprocess_s,
                                   compute=sum(d["solve_s"] for d in diagnostics)+mapping_s+postprocess_s,
                                   diagnostic_direct=sum(d.get("direct_s", 0) for d in diagnostics)))
